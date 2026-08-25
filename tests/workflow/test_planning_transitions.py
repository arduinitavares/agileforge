"""Persisted planning transitions, transaction, and idempotency tests."""

from __future__ import annotations

import ast
import concurrent.futures
import inspect
import json
import threading
from datetime import UTC, datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Literal,
    NoReturn,
    TypedDict,
    Unpack,
    cast,
    get_args,
)
from unittest.mock import patch

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, SQLModel, col, create_engine, select

import services.story_dependencies as story_dependencies_module
from models.core import (
    Project,
    ProjectTeam,
    Sprint,
    SprintStory,
    Task,
    Team,
    UserStory,
    UserStoryDependency,
)
from models.enums import SprintStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import (
    BacklogArtifact,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintStart,
    StoryArtifact,
    StoryArtifactDecision,
    StoryDependencyReview,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.sprint import SprintPlannerOutput
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    InvestDimensionAssessment,
    StoryInvestAssessment,
    StoryItemEnvelope,
)
from services.specs import story_validation_service as story_validation_service_module
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.task_metadata import parse_task_metadata
from workflow.clock import FixedClock
from workflow.contracts import (
    Blocker,
    JsonObject,
    NodeCategory,
    NodeDecision,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    planning_graph,
    readiness_fingerprint,
    story_dependency_source_fingerprint,
)
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import (
    ApplyStoryDependencies,
    DecideRoadmap,
    DecideSprintPlan,
    DecideStory,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RepairStoryReadiness,
    StartSprint,
    TransitionRequest,
)
from workflow.requests.base import PositionedRequest
from workflow.requests.planning import ReviewedDependencyEdge, StoryReadinessUpdate

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.agent_workbench.sprint_phase import SprintStartInput
    from services.planning_artifact_content import SprintPlanEnvelope
    from services.planning_lineage import Decision

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
EXPECTED_REQUEST_VARIANT_COUNT = 33
EXPECTED_PLANNING_REQUEST_COUNT = 9
REPAIRED_STORY_POINTS = 3
EXPECTED_DEPENDENCY_STORY_COUNT = 3
_JSON_OBJECT = TypeAdapter(JsonObject)
PLANNING_REQUESTS = (
    RecordRoadmapDraft,
    DecideRoadmap,
    RecordStoryDraft,
    DecideStory,
    ApplyStoryDependencies,
    RepairStoryReadiness,
    RecordSprintPlan,
    DecideSprintPlan,
    StartSprint,
)
CALLER_SESSION_FUNCTIONS = {
    story_dependencies_module: {"apply_story_dependencies_in_session"},
}


class _RequestGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    actor: str
    correlation_id: str


class _DependencyEdgePayload(TypedDict):
    dependent_story_id: int
    prerequisite_story_id: int
    reason: str


class _SprintDraftOptions(TypedDict):
    team_name: str
    idempotency_key: str


def _copy_dependency_edge(item: _DependencyEdgePayload) -> _DependencyEdgePayload:
    return {
        "dependent_story_id": item["dependent_story_id"],
        "prerequisite_story_id": item["prerequisite_story_id"],
        "reason": item["reason"],
    }


class _ForcedPlanningError(RuntimeError):
    """Controlled transition failure used to prove rollback behavior."""


def _backlog_content(*requirements: str) -> JsonObject:
    return {
        "backlog_items": [
            {
                "backlog_item_id": f"PBI-{index:06d}",
                "priority": index,
                "requirement": requirement,
                "spec_item_ids": [f"REQ.planning-{index}"],
                "value_driver": "Strategic",
                "justification": f"Deliver {requirement}.",
                "estimated_effort": "M",
                "technical_note": None,
            }
            for index, requirement in enumerate(requirements, start=1)
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _specification_content(*requirements: str) -> str:
    return canonical_json(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.task-7-planning",
            "title": "Task 7 planning contract",
            "summary": "Persist immutable Backlog and Roadmap artifacts.",
            "problem_statement": "Planning needs exact reviewed parent lineage.",
            "items": [
                {
                    "id": f"REQ.planning-{index}",
                    "type": "REQ",
                    "title": requirement,
                    "statement": f"Deliver {requirement} through reviewed planning.",
                    "level": "MUST",
                    "verification": "acceptance-test",
                    "acceptance": [
                        f"The Roadmap references {requirement} exactly once."
                    ],
                }
                for index, requirement in enumerate(requirements, start=1)
            ],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
        }
    )


def _seed_accepted_backlog(
    engine: Engine,
    *,
    requirements: tuple[str, ...] = ("Plan immutable work",),
) -> int:
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_decision_in_session,
        record_backlog_draft_in_session,
    )

    with Session(engine) as session:
        project = Project(name=f"Task 11 {requirements!r}")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content=_specification_content(*requirements),
            recorded_at=EVALUATED_AT - timedelta(minutes=20),
        )
        spec = lineage.spec
        assert spec.spec_version_id is not None
        content = _backlog_content(*requirements)
        fingerprint = canonical_hash(content)
        backlog = record_backlog_draft_in_session(
            session,
            project_id=project.project_id,
            spec_version_id=spec.spec_version_id,
            spec_hash=spec.spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=content,
            content_fingerprint=fingerprint,
            supersedes_backlog_artifact_id=None,
            artifact_id=(project.project_id * 100) + 1,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        assert backlog.backlog_artifact_id is not None
        record_backlog_decision_in_session(
            session,
            artifact=backlog,
            decision="accepted",
            rationale="Accepted backlog.",
            reviewer="operator@example.com",
            idempotency_key="seed-backlog",
            decided_at=EVALUATED_AT,
        )
        session.commit()
        return project.project_id


def _replace_specification_and_backlog(engine: Engine, project_id: int) -> None:
    """Accept a replacement direct Specification and independent Backlog root."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_decision_in_session,
        record_backlog_draft_in_session,
    )

    with Session(engine) as session:
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content=_specification_content("Plan replacement direct-Spec work"),
            recorded_at=EVALUATED_AT - timedelta(minutes=10),
        )
        replacement_spec = lineage.spec
        assert replacement_spec.spec_version_id is not None
        replacement_content = _backlog_content("Plan replacement direct-Spec work")
        replacement_fingerprint = canonical_hash(replacement_content)
        replacement_backlog = record_backlog_draft_in_session(
            session,
            project_id=project_id,
            spec_version_id=replacement_spec.spec_version_id,
            spec_hash=replacement_spec.spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=replacement_content,
            content_fingerprint=replacement_fingerprint,
            supersedes_backlog_artifact_id=None,
            artifact_id=202,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        assert replacement_backlog.backlog_artifact_id is not None
        record_backlog_decision_in_session(
            session,
            artifact=replacement_backlog,
            decision="accepted",
            rationale="Accepted replacement Backlog.",
            reviewer="operator@example.com",
            idempotency_key="seed-replacement-backlog",
            decided_at=EVALUATED_AT,
        )
        session.commit()


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=planning_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _decision(
    position: WorkflowPosition,
    node_id: str,
    instance_key: str | None = None,
) -> NodeDecision:
    return next(
        item
        for item in position.decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )


def _guards(
    position: WorkflowPosition,
    node_id: str,
    instance_key: str | None = None,
) -> _RequestGuards:
    decision = _decision(position, node_id, instance_key)
    return {
        "project_id": position.project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "instance_key": decision.instance_key,
        "actor": "operator@example.com",
        "correlation_id": "task-11",
    }


def _output_int(result: TransitionResult, key: str) -> int:
    value = result.output[key]
    assert isinstance(value, int)
    return value


def _output_first_int(result: TransitionResult, key: str) -> int:
    value = result.output[key]
    assert isinstance(value, tuple)
    first = value[0]
    assert isinstance(first, int)
    return first


def _roadmap_content(
    *requirements: str,
) -> JsonObject:
    if not requirements:
        requirements = ("Plan immutable work",)
    return {
        "roadmap_releases": [
            {
                "release_name": "Milestone 1",
                "theme": "Planning",
                "focus_area": "Technical Foundation",
                "backlog_item_ids": [
                    f"PBI-{index:06d}"
                    for index, _requirement in enumerate(requirements, start=1)
                ],
                "reasoning": "Build durable planning facts first.",
            }
        ],
        "roadmap_summary": "Deliver the accepted backlog in dependency order.",
        "is_complete": True,
        "clarifying_questions": [],
    }


def _invest_assessment() -> StoryInvestAssessment:
    return StoryInvestAssessment(
        independent=InvestDimensionAssessment(
            result="pass",
            rationale="Delivers self-contained increment.",
            evidence="No unbuilt dependencies.",
        ),
        negotiable=InvestDimensionAssessment(
            result="pass",
            rationale="Implementation details open to refinement.",
            evidence="Focuses on user outcome.",
        ),
        valuable=InvestDimensionAssessment(
            result="pass",
            rationale="Directly delivers user capability.",
            evidence="Addresses requirement.",
        ),
        estimable=InvestDimensionAssessment(
            result="pass",
            rationale="Scope is clear and bounded.",
            evidence="Discrete criteria.",
        ),
        small=InvestDimensionAssessment(
            result="pass",
            rationale="Sized for single iteration.",
            evidence="Effort is M.",
        ),
        testable=InvestDimensionAssessment(
            result="pass",
            rationale="Verifiable pass/fail criteria.",
            evidence="Observable verification steps.",
        ),
    )


def _story_content(
    requirement: str = "Plan immutable work",
    *,
    spec_item_id: str | None = None,
) -> JsonObject:
    resolved_spec_item_id = spec_item_id or (
        "REQ.planning-2"
        if requirement == "Validate planning work"
        else "REQ.planning-1"
    )
    item = CanonicalStoryItem(
        story_item_id="US-0001",
        story_title=f"Story for {requirement}",
        statement=(
            "As an operator, I want durable planning facts, so that routing "
            "survives restarts."
        ),
        persona="operator",
        acceptance_criteria=("Verify that planning survives restart.",),
        spec_item_ids=(resolved_spec_item_id,),
        invest_assessment=_invest_assessment(),
        estimated_effort="M",
        effort_rationale="Moderate complexity storage routine.",
        order_rationale="First priority calculation.",
        produced_artifacts=("planning records",),
        research_caveats=(),
        dependency_candidates=(),
    )
    output = CanonicalStoryOutput(
        story_items=(
            StoryItemEnvelope(
                item=item,
                item_fingerprint=canonical_hash(item.model_dump(mode="json")),
            ),
        ),
        is_complete=True,
        clarifying_questions=(),
    )
    return _JSON_OBJECT.validate_python(output.model_dump(mode="json"))


def _sprint_plan(story_id: int) -> JsonObject:
    return {
        "sprint_goal": "Persist planning workflow facts.",
        "selected_stories": [
            {
                "story_id": story_id,
                "story_item_id": "US-0001",
                "tasks": [
                    {
                        "description": "Implement planning persistence",
                        "relevant_spec_item_ids": ["REQ.planning-1"],
                        "task_kind": "implementation",
                        "artifact_targets": ["planning workflow handler"],
                        "workstream_tags": ["workflow"],
                        "checklist_items": ["Run focused tests"],
                    }
                ],
                "reason_for_selection": "Required for durable routing.",
            }
        ],
    }


def _record_and_accept_roadmap(  # noqa: PLR0913
    domain: WorkflowDomain,
    project_id: int,
    *,
    requirements: tuple[str, ...] = ("Plan immutable work",),
    idempotency_suffix: str = "",
    roadmap_summary: str | None = None,
    supersedes_roadmap_artifact_id: int | None = None,
) -> int:
    position = domain.position(project_id)
    content = _roadmap_content(*requirements)
    if roadmap_summary is not None:
        content["roadmap_summary"] = roadmap_summary
    backlog_reference = _decision(
        position,
        "planning.roadmap.generate",
    ).fact_references[0]
    recorded = domain.transition(
        RecordRoadmapDraft(
            **_guards(position, "planning.roadmap.generate"),
            idempotency_key=f"record-roadmap{idempotency_suffix}",
            backlog_artifact_id=int(backlog_reference.fact_id),
            backlog_artifact_fingerprint=backlog_reference.fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_roadmap_artifact_id=supersedes_roadmap_artifact_id,
        )
    )
    assert recorded.ok is True
    artifact_id = _output_int(recorded, "roadmap_artifact_id")
    fingerprint = str(recorded.output["content_fingerprint"])
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideRoadmap(
            **_guards(position, "planning.roadmap.review"),
            idempotency_key=f"accept-roadmap{idempotency_suffix}",
            roadmap_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Roadmap covers the accepted backlog.",
        )
    )
    assert accepted.ok is True
    return artifact_id


def _accepted_backlog(engine: Engine, project_id: int) -> BacklogArtifact:
    with Session(engine) as session:
        artifact = session.exec(
            select(BacklogArtifact).where(col(BacklogArtifact.project_id) == project_id)
        ).one()
        session.expunge(artifact)
        return artifact


@pytest.mark.parametrize(
    ("backlog_item_ids", "message"),
    [
        ((), "every parent Backlog item exactly once"),
        (("PBI-000001", "PBI-000001"), "duplicate backlog item ID"),
        (("PBI-000002",), "unknown backlog item ID"),
    ],
)
def test_roadmap_rejects_invalid_exact_backlog_coverage_before_persistence(
    engine: Engine,
    backlog_item_ids: tuple[str, ...],
    message: str,
) -> None:
    """Roadmap persistence resolves IDs against one exact accepted Backlog."""
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDraftInput,
        record_roadmap_draft_in_session,
    )

    project_id = _seed_accepted_backlog(engine)
    backlog = _accepted_backlog(engine, project_id)
    content = _roadmap_content()
    releases = content["roadmap_releases"]
    assert isinstance(releases, list)
    release = _JSON_OBJECT.validate_python(releases[0])
    release["backlog_item_ids"] = list(backlog_item_ids)
    releases[0] = release
    with Session(engine) as session:
        with pytest.raises(ValueError, match=message):
            record_roadmap_draft_in_session(
                session,
                inputs=RecordRoadmapDraftInput(
                    project_id=project_id,
                    backlog_artifact_id=int(backlog.backlog_artifact_id or 0),
                    backlog_artifact_fingerprint=backlog.content_fingerprint,
                    canonical_content=content,
                    content_fingerprint=canonical_hash(content),
                    supersedes_roadmap_artifact_id=None,
                    actor="operator@example.com",
                    recorded_at=EVALUATED_AT,
                ),
            )
        assert session.exec(select(RoadmapArtifact)).all() == []


@pytest.mark.parametrize(
    "mutation",
    ["incomplete", "empty", "wrong_hash", "extra_field", "is_complete_int"],
)
def test_roadmap_rejects_noncanonical_or_incomplete_content_without_rows(
    engine: Engine,
    mutation: str,
) -> None:
    """Malformed Roadmap content fails before a durable row is added."""
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDraftInput,
        record_roadmap_draft_in_session,
    )

    project_id = _seed_accepted_backlog(engine)
    backlog = _accepted_backlog(engine, project_id)
    content = _roadmap_content()
    fingerprint = canonical_hash(content)
    if mutation == "incomplete":
        content["is_complete"] = False
        fingerprint = canonical_hash(content)
    elif mutation == "empty":
        content["roadmap_releases"] = []
        fingerprint = canonical_hash(content)
    elif mutation == "wrong_hash":
        fingerprint = "sha256:" + "0" * 64
    elif mutation == "extra_field":
        content["provider_metadata"] = "not canonical host output"
        fingerprint = canonical_hash(content)
    else:
        content["is_complete"] = 1
        fingerprint = canonical_hash(content)

    with Session(engine) as session:
        with pytest.raises((ValidationError, ValueError)):
            record_roadmap_draft_in_session(
                session,
                inputs=RecordRoadmapDraftInput(
                    project_id=project_id,
                    backlog_artifact_id=int(backlog.backlog_artifact_id or 0),
                    backlog_artifact_fingerprint=backlog.content_fingerprint,
                    canonical_content=content,
                    content_fingerprint=fingerprint,
                    supersedes_roadmap_artifact_id=None,
                    actor="operator@example.com",
                    recorded_at=EVALUATED_AT,
                ),
            )
        assert session.exec(select(RoadmapArtifact)).all() == []


def test_roadmap_a_feedback_b_accepted_c_is_append_only_and_lineage_current(
    engine: Engine,
) -> None:
    """Only an accepted transitive descendant displaces accepted Roadmap A."""
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDecisionInput,
        RecordRoadmapDraftInput,
        record_roadmap_decision_in_session,
        record_roadmap_draft_in_session,
    )
    from services.planning_lineage import (  # noqa: PLC0415
        ArtifactLineageNode,
        select_current_accepted_artifact,
    )

    project_id = _seed_accepted_backlog(engine)
    backlog = _accepted_backlog(engine, project_id)
    backlog_id = int(backlog.backlog_artifact_id or 0)
    key = (project_id, backlog_id, backlog.content_fingerprint)
    feedback_version = 2
    with Session(engine) as session:
        artifacts: list[RoadmapArtifact] = []
        for index, (summary, decision) in enumerate(
            (
                ("Accepted A", "accepted"),
                ("Feedback B", "feedback"),
                ("Accepted C", "accepted"),
            ),
            start=1,
        ):
            content = _roadmap_content()
            content["roadmap_summary"] = summary
            artifact = record_roadmap_draft_in_session(
                session,
                inputs=RecordRoadmapDraftInput(
                    project_id=project_id,
                    backlog_artifact_id=backlog_id,
                    backlog_artifact_fingerprint=backlog.content_fingerprint,
                    canonical_content=content,
                    content_fingerprint=canonical_hash(content),
                    supersedes_roadmap_artifact_id=(
                        None if not artifacts else artifacts[-1].roadmap_artifact_id
                    ),
                    actor="operator@example.com",
                    recorded_at=EVALUATED_AT + timedelta(seconds=index),
                ),
            )
            record_roadmap_decision_in_session(
                session,
                inputs=RecordRoadmapDecisionInput(
                    artifact=artifact,
                    decision=decision,
                    rationale=f"Review {summary}.",
                    reviewer="operator@example.com",
                    idempotency_key=f"roadmap-{index}-{decision}",
                    decided_at=EVALUATED_AT + timedelta(seconds=index, milliseconds=1),
                ),
            )
            artifacts.append(artifact)
            if index == feedback_version:
                nodes = tuple(
                    ArtifactLineageNode(
                        artifact_id=int(item.roadmap_artifact_id or 0),
                        chain_key=key,
                        version_number=item.version_number,
                        supersedes_artifact_id=item.supersedes_roadmap_artifact_id,
                        decision=("accepted" if item is artifacts[0] else "feedback"),
                    )
                    for item in artifacts
                )
                assert (
                    select_current_accepted_artifact(nodes, chain_key=key).artifact_id
                    == artifacts[0].roadmap_artifact_id
                )
        session.commit()

        decisions = session.exec(
            select(RoadmapArtifactDecision).where(
                col(RoadmapArtifactDecision.project_id) == project_id
            )
        ).all()
        decision_by_id = {item.roadmap_artifact_id: item.decision for item in decisions}
        nodes = tuple(
            ArtifactLineageNode(
                artifact_id=int(item.roadmap_artifact_id or 0),
                chain_key=key,
                version_number=item.version_number,
                supersedes_artifact_id=item.supersedes_roadmap_artifact_id,
                decision=cast(
                    "Decision",
                    decision_by_id[int(item.roadmap_artifact_id or 0)],
                ),
            )
            for item in artifacts
        )
        assert [item.version_number for item in artifacts] == [1, 2, 3]
        assert (
            select_current_accepted_artifact(nodes, chain_key=key).artifact_id
            == artifacts[2].roadmap_artifact_id
        )
        stored_a = session.get(RoadmapArtifact, artifacts[0].roadmap_artifact_id)
        assert stored_a is not None
        assert stored_a.canonical_content_json == canonical_json(
            {**_roadmap_content(), "roadmap_summary": "Accepted A"}
        )
        assert session.exec(select(UserStory)).all() == []
        assert (
            session.exec(
                select(WorkflowEvent).where(
                    col(WorkflowEvent.event_type) == WorkflowEventType.ROADMAP_SAVED
                )
            ).all()
            == []
        )


def _validate_story_structurally(engine: Engine, story_id: int) -> None:
    """Run the real provider-free Task 9 action for one accepted fixture Story."""
    with patch.object(
        story_validation_service_module,
        "get_engine",
        return_value=engine,
    ):
        validation = story_validation_service_module.validate_story_with_specification(
            {"story_id": story_id}
        )
    assert validation["success"] is True
    assert validation["ready_for_sprint"] is True
    assert validation["semantic_review_state"] == "not_requested"


def _record_and_accept_story(  # noqa: PLR0913
    engine: Engine,
    domain: WorkflowDomain,
    project_id: int,
    *,
    requirement: str = "Plan immutable work",
    spec_item_id: str | None = None,
    idempotency_suffix: str = "",
) -> tuple[int, int]:
    position = domain.position(project_id)
    generate = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story.generate"
        and item.category is NodeCategory.AVAILABLE
        and item.reason_code != "STORY_CORRECTION_AVAILABLE"
    )
    assert generate.instance_key is not None
    backlog_item_reference = next(
        item for item in generate.fact_references if item.fact_type == "backlog_item"
    )
    backlog_reference = next(
        item for item in generate.fact_references if item.fact_type == "backlog"
    )
    roadmap_reference = next(
        item for item in generate.fact_references if item.fact_type == "roadmap"
    )
    content = _story_content(requirement, spec_item_id=spec_item_id)
    recorded = domain.transition(
        RecordStoryDraft(
            **_guards(position, "planning.story.generate", generate.instance_key),
            idempotency_key=f"record-story{idempotency_suffix}",
            backlog_item_id=backlog_item_reference.fact_id,
            source_backlog_artifact_id=int(backlog_reference.fact_id),
            source_backlog_artifact_fingerprint=backlog_reference.fingerprint,
            roadmap_artifact_id=int(roadmap_reference.fact_id),
            roadmap_artifact_fingerprint=roadmap_reference.fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    artifact_id = _output_int(recorded, "story_artifact_id")
    fingerprint = str(recorded.output["content_fingerprint"])
    assert recorded.output["story_item_ids"] == ("US-0001",)
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideStory(
            **_guards(
                position,
                "planning.story.review",
                generate.instance_key,
            ),
            idempotency_key=f"accept-story{idempotency_suffix}",
            backlog_item_id=backlog_item_reference.fact_id,
            story_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Story content is complete.",
        )
    )
    assert accepted.ok is True
    story_id = _output_first_int(accepted, "activated_story_ids")
    _validate_story_structurally(engine, story_id)
    return artifact_id, story_id


def _record_sprint_plan_draft(
    engine: Engine,
    domain: WorkflowDomain,
    project_id: int,
    story_id: int,
    **options: Unpack[_SprintDraftOptions],
) -> tuple[int, str, JsonObject, str]:
    """Record one persisted Sprint-plan draft and return exact bindings."""
    team_name = options["team_name"]
    idempotency_key = options["idempotency_key"]
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key=f"{idempotency_key}-dependencies",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    candidate_fingerprint = candidate_set_fingerprint(
        snapshot.stories,
        snapshot.story_dependencies,
    )
    plan = _sprint_plan(story_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    position = domain.position(project_id)
    recorded = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key=idempotency_key,
            team_name=team_name,
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan),
        )
    )
    assert recorded.ok is True
    with Session(engine) as session:
        assert session.exec(select(Team).where(Team.name == team_name)).first() is None
        assert session.exec(select(Sprint)).first() is None
        assert session.exec(select(SprintStory)).first() is None
        assert session.exec(select(Task)).first() is None
    return (
        _output_int(recorded, "sprint_plan_artifact_id"),
        candidate_fingerprint,
        plan,
        str(recorded.output["plan_fingerprint"]),
    )


def _apply_current_dependencies(
    engine: Engine,
    domain: WorkflowDomain,
    project_id: int,
    *,
    idempotency_key: str,
) -> None:
    """Persist review of the current candidate dependency semantics."""
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    stories = tuple(
        item
        for item in snapshot.stories
        if item.structurally_eligible and item.sprint_selection_state == "selected"
    )
    reviewed_edges = tuple(
        ReviewedDependencyEdge(
            dependent_story_id=edge.dependent_story_id,
            prerequisite_story_id=edge.prerequisite_story_id,
            reason=edge.reason or "Reviewed dependency.",
        )
        for edge in snapshot.story_dependencies
        if edge.status == "active"
    )
    position = domain.position(project_id)
    applied = domain.transition(
        ApplyStoryDependencies(
            **_guards(position, "planning.story_dependencies"),
            idempotency_key=idempotency_key,
            selected_story_ids=tuple(item.story_id for item in stories),
            reviewed_edges=reviewed_edges,
            source_fingerprint=story_dependency_source_fingerprint(stories),
        )
    )
    assert applied.ok is True


def _seed_dependency_review_rows(
    engine: Engine,
) -> tuple[int, int, tuple[int, ...], tuple[_DependencyEdgePayload, ...]]:
    """Persist canonical reviewed dependency rows plus a foreign Story identity."""
    requirements = (
        "Plan immutable work",
        "Validate planning work",
        "Deliver planning work",
    )
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id, requirements=requirements)
    story_ids = tuple(
        _record_and_accept_story(
            engine,
            domain,
            project_id,
            requirement=requirement,
            spec_item_id=f"REQ.planning-{index}",
            idempotency_suffix=f"-dependency-{index}",
        )[1]
        for index, requirement in enumerate(requirements, start=1)
    )
    foreign_project_id = _seed_accepted_backlog(
        engine,
        requirements=("Foreign dependency work",),
    )
    foreign_domain = _domain(engine)
    _record_and_accept_roadmap(
        foreign_domain,
        foreign_project_id,
        requirements=("Foreign dependency work",),
        idempotency_suffix="-foreign-dependency",
    )
    _foreign_artifact_id, foreign_story_id = _record_and_accept_story(
        engine,
        foreign_domain,
        foreign_project_id,
        requirement="Foreign dependency work",
        spec_item_id="REQ.planning-1",
        idempotency_suffix="-foreign-dependency",
    )
    with Session(engine) as session:
        assert len(story_ids) == EXPECTED_DEPENDENCY_STORY_COUNT
        edges: tuple[_DependencyEdgePayload, ...] = (
            {
                "dependent_story_id": story_ids[1],
                "prerequisite_story_id": story_ids[0],
                "reason": "Second requires first.",
            },
            {
                "dependent_story_id": story_ids[2],
                "prerequisite_story_id": story_ids[1],
                "reason": "Third requires second.",
            },
        )
        session.add_all(
            [
                UserStoryDependency(
                    project_id=project_id,
                    dependent_story_id=edge["dependent_story_id"],
                    prerequisite_story_id=edge["prerequisite_story_id"],
                    status="active",
                    source="manual_review",
                    confidence="reviewed",
                    reason=edge["reason"],
                    created_at=EVALUATED_AT,
                    updated_at=EVALUATED_AT,
                )
                for edge in edges
            ]
        )
        session.commit()
        snapshot = WorkflowFactRepository(session).load(project_id)
        source_fingerprint = story_dependency_source_fingerprint(snapshot.stories)
        session.add(
            StoryDependencyReview(
                project_id=project_id,
                selected_story_ids_json=canonical_json(list(story_ids)),
                reviewed_edges_json=canonical_json(list(edges)),
                source_fingerprint=source_fingerprint,
                dependency_fingerprint=canonical_hash(list(edges)),
                reviewed_by="operator@example.com",
                reviewed_at=EVALUATED_AT,
            )
        )
        session.commit()
        return project_id, foreign_story_id, story_ids, edges


def test_closed_union_adds_exactly_nine_typed_planning_requests() -> None:
    """Add exactly the nine approved planning request variants."""
    variants = get_args(TransitionRequest.__value__)
    assert len(variants) == EXPECTED_REQUEST_VARIANT_COUNT
    assert set(PLANNING_REQUESTS).issubset(set(variants))
    assert len(set(PLANNING_REQUESTS)) == EXPECTED_PLANNING_REQUEST_COUNT


@pytest.mark.parametrize("request_type", PLANNING_REQUESTS)
def test_planning_requests_inherit_positioned_guard_without_expected_state(
    request_type: type[PositionedRequest],
) -> None:
    """Require common positioned guards and forbid expected_state."""
    assert issubclass(request_type, PositionedRequest)
    assert "expected_state" not in request_type.model_fields


def test_story_request_instance_key_is_exact_backlog_item_id() -> None:
    """Derive the exact immutable Backlog-item Story instance key."""
    payload = {
        "project_id": 1,
        "graph_version": "graph",
        "fact_fingerprint": "facts",
        "decision_fingerprint": "decision",
        "idempotency_key": "story",
        "actor": "operator",
        "backlog_item_id": "PBI-000001",
        "source_backlog_artifact_id": 10,
        "source_backlog_artifact_fingerprint": "backlog",
        "roadmap_artifact_id": 1,
        "roadmap_artifact_fingerprint": "roadmap",
        "canonical_content": _story_content(),
        "content_fingerprint": "story",
    }
    request = RecordStoryDraft.model_validate(payload)
    assert request.decision_instance_key() == "backlog_item:PBI-000001"
    with pytest.raises(ValidationError):
        RecordStoryDraft.model_validate(
            {**payload, "instance_key": "backlog_item:PBI-000002"}
        )


def test_planning_service_mutations_use_only_caller_owned_session() -> None:
    """Keep planning mutation transaction ownership in WorkflowDomain."""
    import services.agent_workbench.roadmap_phase as roadmap_phase_module  # noqa: PLC0415
    import services.agent_workbench.sprint_phase as sprint_phase_module  # noqa: PLC0415
    import services.agent_workbench.story_phase as story_phase_module  # noqa: PLC0415

    forbidden_calls = {"commit", "rollback", "close"}
    caller_session_functions = {
        **CALLER_SESSION_FUNCTIONS,
        roadmap_phase_module: {
            "record_roadmap_draft_in_session",
            "record_roadmap_decision_in_session",
        },
        story_phase_module: {
            "record_story_draft_in_session",
            "record_story_decision_in_session",
            "repair_story_readiness_in_session",
        },
        sprint_phase_module: {
            "record_sprint_plan_in_session",
            "record_sprint_plan_decision_in_session",
            "start_sprint_in_session",
        },
    }
    for module, function_names in caller_session_functions.items():
        for function_name in function_names:
            function = getattr(module, function_name)
            signature = inspect.signature(function)
            assert "session" in signature.parameters
            tree = ast.parse(inspect.getsource(function))
            for node in ast.walk(tree):
                if isinstance(node, ast.With):
                    assert "Session(" not in ast.unparse(node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in forbidden_calls
            assert ("fsm" + "_state") not in inspect.getsource(function)
            assert "expected_state" not in inspect.getsource(function)


def test_task_7_phase_mutations_use_only_caller_owned_session() -> None:
    """Backlog and Roadmap phase helpers retain caller transaction ownership."""
    import services.agent_workbench.backlog_phase as backlog_phase_module  # noqa: PLC0415
    import services.agent_workbench.roadmap_phase as roadmap_phase_module  # noqa: PLC0415

    functions = (
        backlog_phase_module.record_backlog_draft_in_session,
        backlog_phase_module.record_backlog_decision_in_session,
        roadmap_phase_module.record_roadmap_draft_in_session,
        roadmap_phase_module.record_roadmap_decision_in_session,
    )
    forbidden_calls = {"commit", "rollback", "close"}
    for function in functions:
        assert "session" in inspect.signature(function).parameters
        tree = ast.parse(inspect.getsource(function))
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                assert "Session(" not in ast.unparse(node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls


def test_roadmap_and_story_transitions_persist_immutable_reviewed_artifacts(
    engine: Engine,
) -> None:
    """Persist immutable Roadmap and Story artifacts with append-only reviews."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    roadmap_id = _record_and_accept_roadmap(domain, project_id)
    story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)

    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        story_artifact = session.get(StoryArtifact, story_artifact_id)
        story = session.get(UserStory, story_id)
        assert roadmap is not None
        assert story_artifact is not None
        assert story is not None
        assert story.source_story_artifact_id == story_artifact_id
        assert story.source_story_item_id == "US-0001"
        assert story.acceptance_criteria_json == canonical_json(
            ["Verify that planning survives restart."]
        )
        assert (
            session.exec(
                select(RoadmapArtifactDecision).where(
                    col(RoadmapArtifactDecision.roadmap_artifact_id) == roadmap_id
                )
            )
            .one()
            .decision
            == "accepted"
        )
        assert (
            session.exec(
                select(StoryArtifactDecision).where(
                    col(StoryArtifactDecision.story_artifact_id) == story_artifact_id
                )
            )
            .one()
            .decision
            == "accepted"
        )


def test_dependency_and_readiness_transitions_bind_exact_current_story_facts(
    engine: Engine,
) -> None:
    """Bind dependency and readiness transitions to exact current Stories."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    source_fingerprint = story_dependency_source_fingerprint(snapshot.stories)
    position = domain.position(project_id)
    applied = domain.transition(
        ApplyStoryDependencies(
            **_guards(position, "planning.story_dependencies"),
            idempotency_key="apply-dependencies",
            selected_story_ids=(story_id,),
            reviewed_edges=(),
            source_fingerprint=source_fingerprint,
        )
    )
    assert applied.ok is True

    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_points = REPAIRED_STORY_POINTS
        story.rank = "0"
        session.add(story)
        session.commit()
    _validate_story_structurally(engine, story_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    expected_readiness = readiness_fingerprint(snapshot.stories)
    position = domain.position(project_id)
    readiness = _decision(position, "planning.story_readiness")
    sprint_plan = _decision(position, "planning.sprint.plan")
    assert readiness.category is NodeCategory.AVAILABLE
    assert sprint_plan.category is NodeCategory.BLOCKED
    assert any(blocker.code == "STORY_RANK_INVALID" for blocker in sprint_plan.blockers)
    repaired = domain.transition(
        RepairStoryReadiness(
            **_guards(position, "planning.story_readiness"),
            idempotency_key="repair-readiness",
            story_ids=(story_id,),
            repairs=(
                StoryReadinessUpdate(story_id=story_id, story_points=3, rank="101"),
            ),
            expected_readiness_fingerprint=expected_readiness,
        )
    )
    assert repaired.ok is True
    with Session(engine) as session:
        review = session.exec(
            select(StoryDependencyReview).where(
                col(StoryDependencyReview.project_id) == project_id
            )
        ).one()
        assert review.selected_story_ids_json == canonical_json([story_id])
        repaired_story = session.get(UserStory, story_id)
        assert repaired_story is not None
        assert repaired_story.story_points == REPAIRED_STORY_POINTS
        assert repaired_story.rank == "101"


def test_story_readiness_persistence_rejects_invalid_rank_before_mutation(
    engine: Engine,
) -> None:
    """Reject an invalid durable rank before changing any Story planning values."""
    import services.agent_workbench.story_phase as story_phase_module  # noqa: PLC0415

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)

    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        original = (story.story_points, story.rank, story.updated_at)

        with pytest.raises(ValueError, match="canonical positive base-10"):
            story_phase_module.repair_story_readiness_in_session(
                session,
                project_id=project_id,
                repairs=((story_id, 5, "01"),),
                repaired_at=EVALUATED_AT + timedelta(minutes=1),
            )

        session.refresh(story)
        assert (story.story_points, story.rank, story.updated_at) == original


def test_sprint_plan_review_and_start_bind_exact_plan_and_candidate_set(
    engine: Engine,
) -> None:
    """Bind Sprint plan review and start to exact plan and candidate facts."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _current_candidate_set, _plan, plan_fingerprint = (
        _record_sprint_plan_draft(
            engine,
            domain,
            project_id,
            story_id,
            team_name="Task 11 Team",
            idempotency_key="record-sprint-plan",
        )
    )
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="accept-sprint-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Plan is feasible.",
        )
    )
    assert accepted.ok is True
    sprint_id = _output_int(accepted, "activated_sprint_id")
    position = domain.position(project_id)
    started = domain.transition(
        StartSprint(
            **_guards(position, "planning.sprint.start"),
            idempotency_key="start-sprint",
        )
    )
    assert started.ok is True
    with Session(engine) as session:
        artifact = session.get(SprintPlanArtifact, plan_id)
        sprint = session.get(Sprint, sprint_id)
        assert artifact is not None
        assert artifact.selected_story_ids_json == canonical_json([story_id])
        assert sprint is not None
        assert sprint.status is SprintStatus.ACTIVE
        assert (
            session.exec(
                select(SprintPlanArtifactDecision).where(
                    col(SprintPlanArtifactDecision.sprint_plan_artifact_id) == plan_id
                )
            )
            .one()
            .decision
            == "accepted"
        )
        assert (
            session.exec(
                select(SprintStory).where(col(SprintStory.sprint_id) == sprint_id)
            )
            .one()
            .story_id
            == story_id
        )
        task = session.exec(select(Task).where(col(Task.story_id) == story_id)).one()
        assert task.metadata_json is not None
        assert "implementation" in task.metadata_json


def test_accepted_unstarted_revision_replaces_only_current_projection(
    engine: Engine,
) -> None:
    """Keep decision A as history while accepted C replaces its planned rows."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
    )
    plan_a_id, _candidate, plan, plan_a_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Revision Team",
        idempotency_key="revision-plan-a",
    )
    position = domain.position(project_id)
    accepted_a = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="revision-accept-a",
            sprint_plan_artifact_id=plan_a_id,
            plan_fingerprint=plan_a_fingerprint,
            decision="accepted",
            rationale="Accept A.",
        )
    )
    assert accepted_a.ok is True
    sprint_id = _output_int(accepted_a, "activated_sprint_id")
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    plan["sprint_goal"] = "Revised immutable goal."
    position = domain.position(project_id)
    recorded_c = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key="revision-plan-c",
            team_name="Revision Team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan),
        )
    )
    assert recorded_c.ok is True
    plan_c_id = _output_int(recorded_c, "sprint_plan_artifact_id")
    plan_c_fingerprint = str(recorded_c.output["plan_fingerprint"])
    position = domain.position(project_id)
    accepted_c = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="revision-accept-c",
            sprint_plan_artifact_id=plan_c_id,
            plan_fingerprint=plan_c_fingerprint,
            decision="accepted",
            rationale="Accept C.",
        )
    )
    assert accepted_c.ok is True
    assert _output_int(accepted_c, "activated_sprint_id") == sprint_id
    with Session(engine) as session:
        decisions = session.exec(
            select(SprintPlanArtifactDecision).where(
                col(SprintPlanArtifactDecision.project_id) == project_id
            )
        ).all()
        assert [item.sprint_plan_artifact_id for item in decisions] == [
            plan_a_id,
            plan_c_id,
        ]
        assert all(item.activated_sprint_id == sprint_id for item in decisions)
        sprint = session.get(Sprint, sprint_id)
        assert sprint is not None
        assert sprint.goal == "Revised immutable goal."
        task = session.exec(select(Task).where(Task.story_id == story_id)).one()
        metadata = parse_task_metadata(task.metadata_json)
        assert metadata.sprint_plan_artifact_id == plan_c_id
        assert metadata.sprint_plan_fingerprint == plan_c_fingerprint


def test_sprint_accepted_feedback_accepted_chain_replaces_current_projection(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Persist A -> feedback B -> accepted C before one Sprint is started."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_a_id, _candidate, plan_b, plan_a_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Feedback Chain Team",
        idempotency_key="feedback-chain-plan-a",
    )
    position = domain.position(project_id)
    accepted_a = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="feedback-chain-accept-a",
            sprint_plan_artifact_id=plan_a_id,
            plan_fingerprint=plan_a_fingerprint,
            decision="accepted",
            rationale="Accept A before review feedback.",
        )
    )
    assert accepted_a.ok is True
    sprint_id = _output_int(accepted_a, "activated_sprint_id")
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    plan_b["sprint_goal"] = "Feedback revision B."
    position = domain.position(project_id)
    recorded_b = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key="feedback-chain-plan-b",
            team_name="Feedback Chain Team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan_b),
        )
    )
    assert recorded_b.ok is True
    plan_b_id = _output_int(recorded_b, "sprint_plan_artifact_id")
    plan_b_fingerprint = str(recorded_b.output["plan_fingerprint"])
    position = domain.position(project_id)
    feedback_b = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="feedback-chain-feedback-b",
            sprint_plan_artifact_id=plan_b_id,
            plan_fingerprint=plan_b_fingerprint,
            decision="feedback",
            rationale="Revise the goal before starting.",
        )
    )
    assert feedback_b.ok is True
    with Session(engine) as session:
        after_feedback = WorkflowFactRepository(session).load(project_id)
        plans = {
            item.artifact_id: item
            for item in after_feedback.planning_artifacts
            if item.artifact_type == "sprint_plan"
        }
        assert plans[plan_a_id].status == "accepted"
        assert plans[plan_b_id].status == "feedback"
    plan_b["sprint_goal"] = "Accepted replacement C."
    position = domain.position(project_id)
    recorded_c = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key="feedback-chain-plan-c",
            team_name="Feedback Chain Team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan_b),
        )
    )
    assert recorded_c.ok is True
    plan_c_id = _output_int(recorded_c, "sprint_plan_artifact_id")
    plan_c_fingerprint = str(recorded_c.output["plan_fingerprint"])
    position = domain.position(project_id)
    accepted_c = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="feedback-chain-accept-c",
            sprint_plan_artifact_id=plan_c_id,
            plan_fingerprint=plan_c_fingerprint,
            decision="accepted",
            rationale="Accept the corrected current plan.",
        )
    )
    assert accepted_c.ok is True
    assert _output_int(accepted_c, "activated_sprint_id") == sprint_id
    with Session(engine) as session:
        after_acceptance = WorkflowFactRepository(session).load(project_id)
        plans = {
            item.artifact_id: item
            for item in after_acceptance.planning_artifacts
            if item.artifact_type == "sprint_plan"
        }
        assert plans[plan_a_id].status == "superseded"
        assert plans[plan_b_id].status == "feedback"
        assert plans[plan_c_id].status == "accepted"
        plan_c = session.get(SprintPlanArtifact, plan_c_id)
        sprint = session.get(Sprint, sprint_id)
        assert plan_c is not None
        assert plan_c.supersedes_sprint_plan_artifact_id == plan_b_id
        assert sprint is not None
        assert sprint.goal == "Accepted replacement C."
        task = session.exec(select(Task).where(Task.story_id == story_id)).one()
        metadata = parse_task_metadata(task.metadata_json)
        assert metadata.sprint_plan_artifact_id == plan_c_id
        assert metadata.sprint_plan_fingerprint == plan_c_fingerprint
    position = domain.position(project_id)
    started = domain.transition(
        StartSprint(
            **_guards(position, "planning.sprint.start"),
            idempotency_key="feedback-chain-start-c",
        )
    )
    assert started.ok is True
    with Session(engine) as session:
        start = session.exec(select(SprintStart)).one()
        assert start.sprint_plan_artifact_id == plan_c_id


@pytest.mark.parametrize("successor_decision", ["feedback", "rejected"])
def test_accepted_plan_remains_startable_after_nonaccepted_successor(
    engine: Engine,
    successor_decision: Literal["feedback", "rejected"],
) -> None:
    """Start accepted A when physical successor B was not accepted."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_a_id, _candidate, plan_b, plan_a_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Accepted Ancestor Team",
        idempotency_key=f"accepted-ancestor-{successor_decision}-a",
    )
    review_position = domain.position(project_id)
    accepted_a = domain.transition(
        DecideSprintPlan(
            **_guards(review_position, "planning.sprint.review"),
            idempotency_key=f"accepted-ancestor-{successor_decision}-accept-a",
            sprint_plan_artifact_id=plan_a_id,
            plan_fingerprint=plan_a_fingerprint,
            decision="accepted",
            rationale="Keep A as the accepted transitive leaf.",
        )
    )
    assert accepted_a.ok is True
    sprint_id = _output_int(accepted_a, "activated_sprint_id")
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    plan_b["sprint_goal"] = f"Physical {successor_decision} successor B."
    plan_position = domain.position(project_id)
    recorded_b = domain.transition(
        RecordSprintPlan(
            **_guards(plan_position, "planning.sprint.plan"),
            idempotency_key=f"accepted-ancestor-{successor_decision}-b",
            team_name="Accepted Ancestor Team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan_b),
        )
    )
    assert recorded_b.ok is True
    plan_b_id = _output_int(recorded_b, "sprint_plan_artifact_id")
    plan_b_fingerprint = str(recorded_b.output["plan_fingerprint"])
    successor_position = domain.position(project_id)
    decided_b = domain.transition(
        DecideSprintPlan(
            **_guards(successor_position, "planning.sprint.review"),
            idempotency_key=f"accepted-ancestor-{successor_decision}-decide-b",
            sprint_plan_artifact_id=plan_b_id,
            plan_fingerprint=plan_b_fingerprint,
            decision=successor_decision,
            rationale=f"Record {successor_decision} on B.",
        )
    )
    assert decided_b.ok is True
    start_position = domain.position(project_id)
    start_decision = _decision(start_position, "planning.sprint.start")
    assert start_decision.category is NodeCategory.AVAILABLE
    assert start_decision.reason_code == "SPRINT_READY_TO_START"

    started = domain.transition(
        StartSprint(
            **_guards(start_position, "planning.sprint.start"),
            idempotency_key=f"accepted-ancestor-{successor_decision}-start-a",
        )
    )

    assert started.ok is True
    assert _output_int(started, "sprint_id") == sprint_id
    with Session(engine) as session:
        start = session.exec(select(SprintStart)).one()
        assert start.sprint_plan_artifact_id == plan_a_id


@pytest.mark.parametrize("decision_value", ["feedback", "rejected"])
def test_sprint_plan_nonacceptance_writes_no_operational_projection(
    engine: Engine,
    decision_value: Literal["feedback", "rejected"],
) -> None:
    """Persist only a null-activation decision for feedback or rejection."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
    )
    plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Decision Only Team",
        idempotency_key=f"decision-only-{decision_value}-draft",
    )
    position = domain.position(project_id)
    result = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key=f"decision-only-{decision_value}",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision=decision_value,
            rationale=f"Record {decision_value} without activation.",
        )
    )
    assert result.ok is True
    assert result.output["activated_sprint_id"] is None
    with Session(engine) as session:
        decision = session.exec(select(SprintPlanArtifactDecision)).one()
        assert decision.decision == decision_value
        assert decision.activated_sprint_id is None
        assert session.exec(select(Team)).all() == []
        assert session.exec(select(ProjectTeam)).all() == []
        assert session.exec(select(Sprint)).all() == []
        assert session.exec(select(SprintStory)).all() == []
        assert session.exec(select(Task)).all() == []
        assert session.exec(select(SprintStart)).all() == []


def test_sprint_plan_order_is_retrievable_after_restart(engine: Engine) -> None:
    """Keep plan order in the immutable artifact while membership stays exact."""
    requirements = ("Plan immutable work", "Validate planning work")
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id, requirements=requirements)
    first_story_id = _record_and_accept_story(engine, domain, project_id)[1]
    second_story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
        requirement=requirements[1],
        idempotency_suffix="-ordered-second",
    )[1]
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="ordered-plan-dependencies",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    ordered_story_ids = (second_story_id, first_story_id)
    plan: JsonObject = {
        "sprint_goal": "Preserve reviewed Story order.",
        "selected_stories": [
            {
                "story_id": story_id,
                "story_item_id": "US-0001",
                "tasks": [
                    {
                        "description": f"Implement ordered Story {story_id}",
                        "relevant_spec_item_ids": [spec_item_id],
                        "task_kind": "implementation",
                        "artifact_targets": ["planning workflow handler"],
                        "workstream_tags": ["workflow"],
                        "checklist_items": ["Run focused tests"],
                    }
                ],
                "reason_for_selection": "Keep exact reviewed order.",
            }
            for story_id, spec_item_id in (
                (second_story_id, "REQ.planning-2"),
                (first_story_id, "REQ.planning-1"),
            )
        ],
    }
    position = domain.position(project_id)
    recorded = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key="record-reversed-order-plan",
            team_name="Ordered Plan Team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan),
        )
    )
    assert recorded.ok is True
    plan_id = _output_int(recorded, "sprint_plan_artifact_id")
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="accept-reversed-order-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=str(recorded.output["plan_fingerprint"]),
            decision="accepted",
            rationale="Accept the exact reviewed order.",
        )
    )
    assert accepted.ok is True
    sprint_id = _output_int(accepted, "activated_sprint_id")
    with Session(engine) as restarted_session:
        restarted = WorkflowFactRepository(restarted_session).load(project_id)
        plan_fact = next(
            item
            for item in restarted.planning_artifacts
            if item.artifact_type == "sprint_plan" and item.artifact_id == plan_id
        )
        memberships = restarted_session.exec(
            select(SprintStory).where(SprintStory.sprint_id == sprint_id)
        ).all()
    assert plan_fact.selected_story_ids == ordered_story_ids
    assert {item.story_id for item in memberships} == set(ordered_story_ids)


def test_sprint_acceptance_failure_after_projection_rolls_back_every_row(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back Team, Sprint, membership, Task, and decision as one unit."""
    import services.agent_workbench.sprint_phase as sprint_phase_module  # noqa: PLC0415

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Rollback Team",
        idempotency_key="rollback-plan-draft",
    )
    original = sprint_phase_module._replace_operational_projection

    def fail_after_projection(  # noqa: PLR0913
        session: Session,
        *,
        artifact: SprintPlanArtifact,
        envelope: SprintPlanEnvelope,
        prior_sprint: Sprint | None,
        team_id: int,
        activated_at: datetime,
    ) -> Sprint:
        original(
            session,
            artifact=artifact,
            envelope=envelope,
            prior_sprint=prior_sprint,
            team_id=team_id,
            activated_at=activated_at,
        )
        message = "forced failure after complete projection"
        raise ValueError(message)

    monkeypatch.setattr(
        sprint_phase_module,
        "_replace_operational_projection",
        fail_after_projection,
    )
    position = domain.position(project_id)
    failed = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="rollback-plan-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Exercise atomic rollback.",
        )
    )
    assert failed.ok is False
    with Session(engine) as session:
        assert session.exec(select(Team)).all() == []
        assert session.exec(select(ProjectTeam)).all() == []
        assert session.exec(select(Sprint)).all() == []
        assert session.exec(select(SprintStory)).all() == []
        assert session.exec(select(Task)).all() == []
        assert session.exec(select(SprintPlanArtifactDecision)).all() == []


def test_sprint_start_failure_after_audit_write_rolls_back_every_row(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back Sprint mutation, start audit, and event as one unit."""
    import services.agent_workbench.sprint_phase as sprint_phase_module  # noqa: PLC0415

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Start Rollback Team",
        idempotency_key="start-rollback-draft",
    )
    review_position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(review_position, "planning.sprint.review"),
            idempotency_key="start-rollback-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Prepare rollback start.",
        )
    )
    assert accepted.ok is True
    sprint_id = _output_int(accepted, "activated_sprint_id")
    original = sprint_phase_module.start_sprint_in_session

    def fail_after_start(session: Session, inputs: SprintStartInput) -> Sprint:
        original(session, inputs)
        message = "forced failure after SprintStart audit"
        raise ValueError(message)

    monkeypatch.setattr(
        sprint_phase_module, "start_sprint_in_session", fail_after_start
    )
    position = domain.position(project_id)
    failed = domain.transition(
        StartSprint(
            **_guards(position, "planning.sprint.start"),
            idempotency_key="start-rollback-transition",
        )
    )
    assert failed.ok is False
    with Session(engine) as session:
        sprint = session.get(Sprint, sprint_id)
        assert sprint is not None
        assert sprint.status is SprintStatus.PLANNED
        assert sprint.started_at is None
        assert session.exec(select(SprintStart)).all() == []
        assert (
            session.exec(
                select(WorkflowEvent).where(
                    WorkflowEvent.event_type == WorkflowEventType.SPRINT_STARTED
                )
            ).all()
            == []
        )


def test_concurrent_distinct_sprint_drafts_serialize_to_one_leaf(
    tmp_path: Path,
) -> None:
    """Use BEGIN IMMEDIATE so one stale guarded draft cannot fork the stream."""
    race_engine = create_engine(
        f"sqlite:///{tmp_path / 'task-10-draft-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(race_engine)
    try:
        project_id = _seed_accepted_backlog(race_engine)
        domain = _domain(race_engine)
        _record_and_accept_roadmap(domain, project_id)
        _artifact_id, story_id = _record_and_accept_story(
            race_engine,
            domain,
            project_id,
        )
        _apply_current_dependencies(
            race_engine,
            domain,
            project_id,
            idempotency_key="draft-race-dependencies",
        )
        with Session(race_engine) as session:
            snapshot = WorkflowFactRepository(session).load(project_id)
        specification = accepted_current_spec(snapshot)
        assert specification is not None
        position = domain.position(project_id)
        barrier = threading.Barrier(2)

        def record(index: int) -> TransitionResult:
            plan = _sprint_plan(story_id)
            plan["sprint_goal"] = f"Concurrent Sprint plan {index}."
            request = RecordSprintPlan(
                **_guards(position, "planning.sprint.plan"),
                idempotency_key=f"concurrent-sprint-plan-{index}",
                team_name="Concurrent Draft Team",
                spec_version_id=specification.spec_version_id,
                spec_hash=specification.spec_hash,
                planner_output=SprintPlannerOutput.model_validate(plan),
            )
            barrier.wait()
            return domain.transition(request)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(record, (1, 2)))
        successes = [item for item in results if item.ok]
        failures = [item for item in results if not item.ok]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error is not None
        assert failures[0].error.code is WorkflowErrorCode.STALE_POSITION
        with Session(race_engine) as session:
            artifacts = session.exec(select(SprintPlanArtifact)).all()
            assert len(artifacts) == 1
            assert artifacts[0].version_number == 1
            assert session.exec(select(SprintPlanArtifactDecision)).all() == []
            assert session.exec(select(Team)).all() == []
            assert session.exec(select(Sprint)).all() == []
    finally:
        race_engine.dispose()


def test_concurrent_sprint_decisions_have_one_complete_winner(tmp_path: Path) -> None:
    """Serialize competing decisions without leaving partial activation rows."""
    race_engine = create_engine(
        f"sqlite:///{tmp_path / 'task-10-decision-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(race_engine)
    try:
        project_id = _seed_accepted_backlog(race_engine)
        domain = _domain(race_engine)
        _record_and_accept_roadmap(domain, project_id)
        _artifact_id, story_id = _record_and_accept_story(
            race_engine,
            domain,
            project_id,
        )
        plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
            race_engine,
            domain,
            project_id,
            story_id,
            team_name="Concurrent Decision Team",
            idempotency_key="decision-race-draft",
        )
        position = domain.position(project_id)
        barrier = threading.Barrier(2)

        def decide(
            decision_value: Literal["accepted", "rejected"],
        ) -> TransitionResult:
            request = DecideSprintPlan(
                **_guards(position, "planning.sprint.review"),
                idempotency_key=f"concurrent-decision-{decision_value}",
                sprint_plan_artifact_id=plan_id,
                plan_fingerprint=plan_fingerprint,
                decision=decision_value,
                rationale=f"Competing {decision_value} decision.",
            )
            barrier.wait()
            return domain.transition(request)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(decide, ("accepted", "rejected")))
        successes = [item for item in results if item.ok]
        failures = [item for item in results if not item.ok]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error is not None
        assert failures[0].error.code in {
            WorkflowErrorCode.STALE_POSITION,
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        }
        with Session(race_engine) as session:
            decisions = session.exec(select(SprintPlanArtifactDecision)).all()
            assert len(decisions) == 1
            operational_counts = (
                len(session.exec(select(Team)).all()),
                len(session.exec(select(ProjectTeam)).all()),
                len(session.exec(select(Sprint)).all()),
                len(session.exec(select(SprintStory)).all()),
                len(session.exec(select(Task)).all()),
            )
            if decisions[0].decision == "accepted":
                assert operational_counts == (1, 1, 1, 1, 1)
                assert decisions[0].activated_sprint_id is not None
            else:
                assert operational_counts == (0, 0, 0, 0, 0)
                assert decisions[0].activated_sprint_id is None
    finally:
        race_engine.dispose()


def test_concurrent_sprint_starts_create_one_exact_start(tmp_path: Path) -> None:
    """Serialize same-Sprint starts into one durable SprintStart identity."""
    race_engine = create_engine(
        f"sqlite:///{tmp_path / 'task-10-start-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(race_engine)
    try:
        project_id = _seed_accepted_backlog(race_engine)
        domain = _domain(race_engine)
        _record_and_accept_roadmap(domain, project_id)
        _artifact_id, story_id = _record_and_accept_story(
            race_engine,
            domain,
            project_id,
        )
        plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
            race_engine,
            domain,
            project_id,
            story_id,
            team_name="Concurrent Start Team",
            idempotency_key="start-race-draft",
        )
        review_position = domain.position(project_id)
        accepted = domain.transition(
            DecideSprintPlan(
                **_guards(review_position, "planning.sprint.review"),
                idempotency_key="start-race-accept",
                sprint_plan_artifact_id=plan_id,
                plan_fingerprint=plan_fingerprint,
                decision="accepted",
                rationale="Prepare concurrent start.",
            )
        )
        assert accepted.ok is True
        sprint_id = _output_int(accepted, "activated_sprint_id")
        position = domain.position(project_id)
        barrier = threading.Barrier(2)

        def start(index: int) -> TransitionResult:
            request = StartSprint(
                **_guards(position, "planning.sprint.start"),
                idempotency_key=f"concurrent-start-{index}",
            )
            barrier.wait()
            return domain.transition(request)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, (1, 2)))
        successes = [item for item in results if item.ok]
        failures = [item for item in results if not item.ok]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error is not None
        assert failures[0].error.code in {
            WorkflowErrorCode.STALE_POSITION,
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        }
        with Session(race_engine) as session:
            starts = session.exec(select(SprintStart)).all()
            assert len(starts) == 1
            assert starts[0].sprint_id == sprint_id
            sprint = session.get(Sprint, sprint_id)
            assert sprint is not None
            assert sprint.status is SprintStatus.ACTIVE
    finally:
        race_engine.dispose()


def test_story_or_dependency_change_rejects_stale_plan_start(engine: Engine) -> None:
    """Reject Sprint start after Story or dependency facts change."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate_fingerprint, _plan, plan_fingerprint = (
        _record_sprint_plan_draft(
            engine,
            domain,
            project_id,
            story_id,
            team_name="Stale Plan Team",
            idempotency_key="stale-record-plan",
        )
    )
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="stale-accept-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Plan accepted before story drift.",
        )
    )
    assert accepted.ok is True
    stale_position = domain.position(project_id)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_points = 5
        session.add(story)
        session.commit()
    result = domain.transition(
        StartSprint(
            **_guards(stale_position, "planning.sprint.start"),
            idempotency_key="stale-start",
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION


def test_stale_specification_roadmap_review_writes_no_terminal_decision(
    engine: Engine,
) -> None:
    """Reject a persisted Roadmap review after direct-Spec/Backlog replacement."""
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDecisionInput,
        record_roadmap_decision_in_session,
    )
    from services.specs.accepted_specification import (  # noqa: PLC0415
        AcceptedSpecificationIntegrityError,
    )

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    backlog_reference = next(
        item
        for item in _decision(position, "planning.roadmap.generate").fact_references
        if item.fact_type == "backlog"
    )
    content = _roadmap_content()
    recorded = domain.transition(
        RecordRoadmapDraft(
            **_guards(position, "planning.roadmap.generate"),
            idempotency_key="stale-authority-roadmap-draft",
            backlog_artifact_id=int(backlog_reference.fact_id),
            backlog_artifact_fingerprint=backlog_reference.fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    roadmap_id = _output_int(recorded, "roadmap_artifact_id")

    _replace_specification_and_backlog(engine, project_id)
    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        assert roadmap is not None
        with pytest.raises(
            AcceptedSpecificationIntegrityError,
            match="current accepted Specification",
        ):
            record_roadmap_decision_in_session(
                session,
                inputs=RecordRoadmapDecisionInput(
                    artifact=roadmap,
                    decision="accepted",
                    rationale="This stale review must not persist.",
                    reviewer="operator@example.com",
                    idempotency_key="reject-stale-spec-roadmap-review",
                    decided_at=EVALUATED_AT,
                ),
            )
        session.rollback()
    with Session(engine) as session:
        decisions = session.exec(
            select(RoadmapArtifactDecision).where(
                col(RoadmapArtifactDecision.roadmap_artifact_id) == roadmap_id
            )
        ).all()
        assert decisions == []


def test_roadmap_decision_rejects_formatting_only_stored_corruption(
    engine: Engine,
) -> None:
    """A decision never accepts or rewrites noncanonical stored Roadmap bytes."""
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDecisionInput,
        record_roadmap_decision_in_session,
    )

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    backlog_reference = next(
        item
        for item in _decision(position, "planning.roadmap.generate").fact_references
        if item.fact_type == "backlog"
    )
    content = _roadmap_content()
    recorded = domain.transition(
        RecordRoadmapDraft(
            **_guards(position, "planning.roadmap.generate"),
            idempotency_key="record-roadmap-for-corruption-check",
            backlog_artifact_id=int(backlog_reference.fact_id),
            backlog_artifact_fingerprint=backlog_reference.fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    roadmap_id = _output_int(recorded, "roadmap_artifact_id")
    corrupted = json.dumps(content, indent=2, sort_keys=True)
    assert corrupted != canonical_json(content)
    assert canonical_hash(json.loads(corrupted)) == canonical_hash(content)

    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        assert roadmap is not None
        roadmap.canonical_content_json = corrupted
        session.add(roadmap)
        session.commit()

        with pytest.raises(ValueError, match="canonical"):
            record_roadmap_decision_in_session(
                session,
                inputs=RecordRoadmapDecisionInput(
                    artifact=roadmap,
                    decision="accepted",
                    rationale="Formatting corruption must fail closed.",
                    reviewer="operator@example.com",
                    idempotency_key="reject-noncanonical-roadmap",
                    decided_at=EVALUATED_AT,
                ),
            )

        stored = session.get(RoadmapArtifact, roadmap_id)
        assert stored is not None
        assert stored.canonical_content_json == corrupted
        assert (
            session.exec(
                select(RoadmapArtifactDecision).where(
                    col(RoadmapArtifactDecision.roadmap_artifact_id) == roadmap_id
                )
            ).all()
            == []
        )


def test_stale_story_review_writes_no_terminal_decision(engine: Engine) -> None:
    """Reject Story review after its exact source Roadmap is superseded."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    roadmap_id = _record_and_accept_roadmap(domain, project_id)
    position = domain.position(project_id)
    generate = next(
        item for item in position.decisions if item.node_id == "planning.story.generate"
    )
    assert generate.instance_key is not None
    backlog_item = next(
        item for item in generate.fact_references if item.fact_type == "backlog_item"
    )
    backlog = next(
        item for item in generate.fact_references if item.fact_type == "backlog"
    )
    roadmap = next(
        item for item in generate.fact_references if item.fact_type == "roadmap"
    )
    story_content = _story_content()
    recorded_story = domain.transition(
        RecordStoryDraft(
            **_guards(position, "planning.story.generate", generate.instance_key),
            idempotency_key="stale-story-draft",
            backlog_item_id=backlog_item.fact_id,
            source_backlog_artifact_id=int(backlog.fact_id),
            source_backlog_artifact_fingerprint=backlog.fingerprint,
            roadmap_artifact_id=int(roadmap.fact_id),
            roadmap_artifact_fingerprint=roadmap.fingerprint,
            canonical_content=story_content,
            content_fingerprint=canonical_hash(story_content),
        )
    )
    assert recorded_story.ok is True
    story_artifact_id = _output_int(recorded_story, "story_artifact_id")

    position = domain.position(project_id)
    roadmap_generate = _decision(position, "planning.roadmap.generate")
    backlog_reference = next(
        item for item in roadmap_generate.fact_references if item.fact_type == "backlog"
    )
    replacement_content = _roadmap_content("Plan corrected work")
    replacement_content["roadmap_summary"] = "Corrected accepted roadmap."
    replacement = domain.transition(
        RecordRoadmapDraft(
            **_guards(position, "planning.roadmap.generate"),
            idempotency_key="replacement-roadmap-draft",
            backlog_artifact_id=int(backlog_reference.fact_id),
            backlog_artifact_fingerprint=backlog_reference.fingerprint,
            canonical_content=replacement_content,
            content_fingerprint=canonical_hash(replacement_content),
            supersedes_roadmap_artifact_id=roadmap_id,
        )
    )
    assert replacement.ok is True
    replacement_id = _output_int(replacement, "roadmap_artifact_id")
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideRoadmap(
            **_guards(position, "planning.roadmap.review"),
            idempotency_key="accept-replacement-roadmap",
            roadmap_artifact_id=replacement_id,
            artifact_fingerprint=canonical_hash(replacement_content),
            decision="accepted",
            rationale="Accept corrected Roadmap.",
        )
    )
    assert accepted.ok is True

    current = domain.position(project_id)
    review = _decision(
        current,
        "planning.story.review",
        generate.instance_key,
    )
    assert review.category is NodeCategory.INVALID
    assert review.reason_code == "STORY_REVIEW_SOURCE_STALE"
    rejected = domain.transition(
        DecideStory(
            **_guards(
                current,
                "planning.story.review",
                generate.instance_key,
            ),
            idempotency_key="reject-stale-story-review",
            backlog_item_id=backlog_item.fact_id,
            story_artifact_id=story_artifact_id,
            artifact_fingerprint=canonical_hash(story_content),
            decision="accepted",
            rationale="This stale Story review must not persist.",
        )
    )
    assert rejected.ok is False
    with Session(engine) as session:
        decisions = session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.story_artifact_id) == story_artifact_id
            )
        ).all()
        assert decisions == []


def test_story_input_uses_replacement_roadmap_with_same_chain_prior_story(
    engine: Engine,
) -> None:
    """Build a successor from current Roadmap B and exact accepted Story A."""
    from services.application import DeliveryActionInputService  # noqa: PLC0415

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    roadmap_a_id = _record_and_accept_roadmap(domain, project_id)
    story_a_id, _story_row_id = _record_and_accept_story(engine, domain, project_id)
    correction = _decision(
        domain.position(project_id),
        "planning.story.generate",
        "backlog_item:PBI-000001",
    )
    assert correction.reason_code == "STORY_CORRECTION_AVAILABLE"
    roadmap_b_id = _record_and_accept_roadmap(
        domain,
        project_id,
        idempotency_suffix="-replacement",
        roadmap_summary="Accepted replacement Roadmap for the same PBI.",
        supersedes_roadmap_artifact_id=roadmap_a_id,
    )
    with Session(engine) as session:
        roadmap_b = session.get(RoadmapArtifact, roadmap_b_id)
        assert roadmap_b is not None
        roadmap_b_fingerprint = roadmap_b.content_fingerprint
    successor = correction.model_copy(
        update={
            "reason_code": "STORY_GENERATION_REQUIRED",
            "recommendation_kind": "required",
            "fact_references": tuple(
                reference.model_copy(
                    update={
                        "fact_id": str(roadmap_b_id),
                        "fingerprint": roadmap_b_fingerprint,
                    }
                )
                if reference.fact_type == "roadmap"
                else reference
                for reference in correction.fact_references
            ),
        }
    )

    prepared = DeliveryActionInputService(engine=engine).build(
        project_id=project_id,
        decision=successor,
        node_id="planning.story.generate",
    )

    assert isinstance(prepared, dict)
    assert prepared["roadmap_artifact_id"] == roadmap_b_id
    assert prepared["roadmap_artifact_fingerprint"] == roadmap_b_fingerprint
    assert prepared["supersedes_story_artifact_id"] == story_a_id


def test_accepted_story_can_be_replaced_after_same_backlog_roadmap_acceptance(
    engine: Engine,
) -> None:
    """Accept Story C under Roadmap B while keeping Sprint pin to Story A."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    roadmap_a_id = _record_and_accept_roadmap(domain, project_id)
    story_a_artifact_id, story_a_id = _record_and_accept_story(
        engine, domain, project_id
    )
    with Session(engine) as session:
        team = Team(name="Roadmap replacement pin")
        session.add(team)
        session.flush()
        assert team.team_id is not None
        sprint = Sprint(
            project_id=project_id,
            team_id=team.team_id,
            status=SprintStatus.ACTIVE,
        )
        session.add(sprint)
        session.flush()
        assert sprint.sprint_id is not None
        sprint_id = sprint.sprint_id
        session.add(SprintStory(sprint_id=sprint_id, story_id=story_a_id))
        session.commit()

    roadmap_b_id = _record_and_accept_roadmap(
        domain,
        project_id,
        idempotency_suffix="-story-successor",
        roadmap_summary="Roadmap B still covers the accepted Backlog and PBI.",
        supersedes_roadmap_artifact_id=roadmap_a_id,
    )
    position = domain.position(project_id)
    successor = _decision(
        position,
        "planning.story.generate",
        "backlog_item:PBI-000001",
    )
    assert successor.category is NodeCategory.AVAILABLE
    assert successor.reason_code == "STORY_GENERATION_REQUIRED"
    references = {
        reference.fact_type: reference for reference in successor.fact_references
    }
    content = _story_content("Plan corrected immutable work")
    recorded = domain.transition(
        RecordStoryDraft(
            **_guards(position, "planning.story.generate", successor.instance_key),
            idempotency_key="record-story-after-roadmap-replacement",
            backlog_item_id="PBI-000001",
            source_backlog_artifact_id=int(references["backlog"].fact_id),
            source_backlog_artifact_fingerprint=references["backlog"].fingerprint,
            roadmap_artifact_id=int(references["roadmap"].fact_id),
            roadmap_artifact_fingerprint=references["roadmap"].fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_story_artifact_id=story_a_artifact_id,
        )
    )
    assert recorded.ok is True
    story_c_artifact_id = _output_int(recorded, "story_artifact_id")
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideStory(
            **_guards(
                position,
                "planning.story.review",
                successor.instance_key,
            ),
            idempotency_key="accept-story-after-roadmap-replacement",
            backlog_item_id="PBI-000001",
            story_artifact_id=story_c_artifact_id,
            artifact_fingerprint=canonical_hash(content),
            decision="accepted",
            rationale="Accept complete Story C under Roadmap B.",
        )
    )
    assert accepted.ok is True

    with Session(engine) as session:
        artifact_c = session.get(StoryArtifact, story_c_artifact_id)
        story_a = session.get(UserStory, story_a_id)
        assert artifact_c is not None
        assert artifact_c.version_number == 2  # noqa: PLR2004
        assert artifact_c.supersedes_story_artifact_id == story_a_artifact_id
        assert artifact_c.roadmap_artifact_id == roadmap_b_id
        assert story_a is not None
        assert story_a.is_superseded is True
        assert (
            session.exec(
                select(SprintStory).where(
                    col(SprintStory.sprint_id) == sprint_id,
                    col(SprintStory.story_id) == story_a_id,
                )
            ).one_or_none()
            is not None
        )


def test_stale_sprint_plan_review_writes_no_terminal_decision(engine: Engine) -> None:
    """Reject Sprint-plan review after candidate readiness changes."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate_fingerprint, _plan, plan_fingerprint = (
        _record_sprint_plan_draft(
            engine,
            domain,
            project_id,
            story_id,
            team_name="Stale Review Team",
            idempotency_key="stale-review-plan-draft",
        )
    )
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_points = 5
        session.add(story)
        session.commit()

    current = domain.position(project_id)
    review = _decision(current, "planning.sprint.review")
    assert review.category is NodeCategory.INVALID
    assert review.reason_code == "SPRINT_PLAN_REVIEW_SOURCE_STALE"
    rejected = domain.transition(
        DecideSprintPlan(
            **_guards(current, "planning.sprint.review"),
            idempotency_key="reject-stale-sprint-review",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="This stale Sprint review must not persist.",
        )
    )
    assert rejected.ok is False
    with Session(engine) as session:
        decisions = session.exec(
            select(SprintPlanArtifactDecision).where(
                col(SprintPlanArtifactDecision.sprint_plan_artifact_id) == plan_id
            )
        ).all()
        assert decisions == []


def test_task_description_and_metadata_tamper_blocks_sprint_start(
    engine: Engine,
) -> None:
    """Bind reviewed Sprint start to exact persisted task semantics."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate_fingerprint, _plan, plan_fingerprint = (
        _record_sprint_plan_draft(
            engine,
            domain,
            project_id,
            story_id,
            team_name="Task Tamper Team",
            idempotency_key="task-tamper-plan-draft",
        )
    )
    review_position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(review_position, "planning.sprint.review"),
            idempotency_key="accept-task-tamper-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Review exact task content.",
        )
    )
    assert accepted.ok is True
    sprint_id = _output_int(accepted, "activated_sprint_id")
    start_position = domain.position(project_id)
    with Session(engine) as session:
        before = WorkflowFactRepository(session).load(project_id)
        task = session.exec(select(Task).where(col(Task.story_id) == story_id)).one()
        task.description = "Tampered after plan review"
        session.add(task)
        session.commit()
        after = WorkflowFactRepository(session).load(project_id)
    assert before.model_dump(mode="json") != after.model_dump(mode="json")
    current = domain.position(project_id)
    start = _decision(current, "planning.sprint.start")
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_TASK_CONTENT_STALE"

    result = domain.transition(
        StartSprint(
            **_guards(start_position, "planning.sprint.start"),
            idempotency_key="start-after-task-tamper",
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    with Session(engine) as session:
        sprint = session.get(Sprint, sprint_id)
        assert sprint is not None
        assert sprint.status is SprintStatus.PLANNED


def test_public_start_reports_different_active_sprint(engine: Engine) -> None:
    """Expose the stable ACTIVE_SPRINT_EXISTS error at the application boundary."""
    from services.application import (  # noqa: PLC0415
        AgileForgeApplication,
        PlanningActionSelectionService,
        SprintStartRequest,
    )

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Public Active Sprint Team",
        idempotency_key="public-active-sprint-draft",
    )
    review_position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(review_position, "planning.sprint.review"),
            idempotency_key="public-active-sprint-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Prepare a planned Sprint.",
        )
    )
    assert accepted.ok is True
    with Session(engine) as session:
        team = session.exec(select(Team)).one()
        assert team.team_id is not None
        session.add(
            Sprint(
                project_id=project_id,
                team_id=team.team_id,
                status=SprintStatus.ACTIVE,
            )
        )
        session.commit()
    application = AgileForgeApplication(
        workflow_domain=domain,
        planning_action_selection=PlanningActionSelectionService(engine=engine),
    )

    result = application.start_sprint(
        SprintStartRequest(
            project_id=project_id,
            idempotency_key="public-active-sprint-start",
            actor="operator@example.com",
            correlation_id="task-10-active-sprint",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.ACTIVE_SPRINT_EXISTS
    assert result.error.message == (
        "Another Sprint is already active for this Project. Close it before "
        "starting this Sprint."
    )


@pytest.mark.parametrize("with_unrelated_active_sprint", [False, True])
def test_public_start_preserves_stale_specification_error(
    engine: Engine,
    with_unrelated_active_sprint: bool,
) -> None:
    """Map an exact stale start decision instead of collapsing it to unavailable."""
    from services.application import (  # noqa: PLC0415
        AgileForgeApplication,
        PlanningActionSelectionService,
        SprintStartRequest,
    )

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Public Stale Specification Team",
        idempotency_key="public-stale-specification-draft",
    )
    review_position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(review_position, "planning.sprint.review"),
            idempotency_key="public-stale-specification-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Prepare an old-Spec planned Sprint.",
        )
    )
    assert accepted.ok is True
    if with_unrelated_active_sprint:
        with Session(engine) as session:
            team = session.exec(select(Team)).one()
            assert team.team_id is not None
            session.add(
                Sprint(
                    project_id=project_id,
                    team_id=team.team_id,
                    status=SprintStatus.ACTIVE,
                )
            )
            session.commit()
    _replace_specification_and_backlog(engine, project_id)
    position = domain.position(project_id)
    graph_decision = _decision(position, "planning.sprint.start")
    assert graph_decision.category is NodeCategory.INVALID
    assert graph_decision.reason_code == "STALE_SPECIFICATION"
    application = AgileForgeApplication(
        workflow_domain=domain,
        planning_action_selection=PlanningActionSelectionService(engine=engine),
    )

    result = application.start_sprint(
        SprintStartRequest(
            project_id=project_id,
            idempotency_key=(
                "public-stale-specification-start-active"
                if with_unrelated_active_sprint
                else "public-stale-specification-start"
            ),
            actor="operator@example.com",
            correlation_id="task-10-stale-specification",
        )
    )

    message = "Sprint start requires the current accepted Specification."
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION
    assert result.error.message == message
    assert result.error.blockers == (
        Blocker(code="STALE_SPECIFICATION", message=message),
    )


def test_public_start_blocks_current_plan_while_exact_older_sprint_is_active(
    engine: Engine,
) -> None:
    """Treat an exact older active Sprint as availability, not target conflict."""
    from services.application import (  # noqa: PLC0415
        AgileForgeApplication,
        PlanningActionSelectionService,
        SprintStartRequest,
    )

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _old_story_artifact_id, old_story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
    )
    old_plan_id, _candidate, _plan, old_plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        old_story_id,
        team_name="Older Active Sprint Team",
        idempotency_key="older-active-sprint-draft",
    )
    review_position = domain.position(project_id)
    old_accepted = domain.transition(
        DecideSprintPlan(
            **_guards(review_position, "planning.sprint.review"),
            idempotency_key="older-active-sprint-accept",
            sprint_plan_artifact_id=old_plan_id,
            plan_fingerprint=old_plan_fingerprint,
            decision="accepted",
            rationale="Activate the older exact Sprint.",
        )
    )
    assert old_accepted.ok is True
    old_sprint_id = _output_int(old_accepted, "activated_sprint_id")
    start_position = domain.position(project_id)
    old_started = domain.transition(
        StartSprint(
            **_guards(start_position, "planning.sprint.start"),
            idempotency_key="older-active-sprint-start",
        )
    )
    assert old_started.ok is True
    assert _output_int(old_started, "sprint_id") == old_sprint_id

    _replace_specification_and_backlog(engine, project_id)
    replacement_requirement = "Plan replacement direct-Spec work"
    _record_and_accept_roadmap(
        domain,
        project_id,
        requirements=(replacement_requirement,),
        idempotency_suffix="-current-lineage",
    )
    _new_story_artifact_id, new_story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
        requirement=replacement_requirement,
        idempotency_suffix="-current-lineage",
    )
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="current-lineage-dependencies",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    current_plan = SprintPlannerOutput.model_validate(_sprint_plan(new_story_id))
    plan_position = domain.position(project_id)
    current_recorded = domain.transition(
        RecordSprintPlan(
            **_guards(plan_position, "planning.sprint.plan"),
            idempotency_key="current-lineage-sprint-draft",
            team_name="Current Planned Sprint Team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=current_plan,
        )
    )
    assert current_recorded.ok is True
    current_plan_id = _output_int(current_recorded, "sprint_plan_artifact_id")
    current_plan_fingerprint = str(current_recorded.output["plan_fingerprint"])
    current_review_position = domain.position(project_id)
    current_accepted = domain.transition(
        DecideSprintPlan(
            **_guards(current_review_position, "planning.sprint.review"),
            idempotency_key="current-lineage-sprint-accept",
            sprint_plan_artifact_id=current_plan_id,
            plan_fingerprint=current_plan_fingerprint,
            decision="accepted",
            rationale="Materialize the current planned Sprint.",
        )
    )
    assert current_accepted.ok is True
    current_sprint_id = _output_int(current_accepted, "activated_sprint_id")
    assert current_sprint_id != old_sprint_id

    position = domain.position(project_id)
    graph_decision = _decision(position, "planning.sprint.start")
    assert graph_decision.category is NodeCategory.BLOCKED
    assert graph_decision.reason_code == "ACTIVE_SPRINT_EXISTS"
    application = AgileForgeApplication(
        workflow_domain=domain,
        planning_action_selection=PlanningActionSelectionService(engine=engine),
    )

    result = application.start_sprint(
        SprintStartRequest(
            project_id=project_id,
            idempotency_key="current-lineage-sprint-start",
            actor="operator@example.com",
            correlation_id="task-10-current-lineage-active-block",
        )
    )

    message = (
        "Another Sprint is already active for this Project. Close it before "
        "starting this Sprint."
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.ACTIVE_SPRINT_EXISTS
    assert result.error.message == message
    assert result.error.blockers == (
        Blocker(code="ACTIVE_SPRINT_EXISTS", message=message),
    )


@pytest.mark.parametrize(
    "metadata_json",
    [
        "{",
        (
            '{"artifact_targets": [], "checklist_items": [], '
            '"relevant_invariant_ids": [], "task_kind": "other", '
            '"version": "task_metadata.v1", "workstream_tags": []}'
        ),
    ],
)
def test_task_metadata_load_requires_valid_canonical_json(
    engine: Engine,
    metadata_json: str,
) -> None:
    """Fail fact loading for malformed or noncanonical persisted task metadata."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name=f"Metadata Validation {len(metadata_json)}",
        idempotency_key=f"metadata-validation-{len(metadata_json)}",
    )
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key=f"metadata-accept-{len(metadata_json)}",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Materialize exact task metadata.",
        )
    )
    assert accepted.ok is True
    with Session(engine) as session:
        task = session.exec(select(Task).where(col(Task.story_id) == story_id)).one()
        task.metadata_json = metadata_json
        session.add(task)
        session.commit()
        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(project_id)


@pytest.mark.parametrize(
    "corruption",
    [
        "malformed_json",
        "fingerprint_mismatch",
        "duplicate_edge",
        "reversed_order",
        "unknown_endpoint",
        "cross_project_endpoint",
        "semantic_cycle",
    ],
)
def test_dependency_review_load_rejects_persisted_corruption(
    engine: Engine,
    corruption: str,
) -> None:
    """Reject malformed, forged, noncanonical, foreign, and cyclic reviews."""
    project_id, foreign_story_id, story_ids, canonical_edges = (
        _seed_dependency_review_rows(engine)
    )
    with Session(engine) as session:
        review = session.exec(
            select(StoryDependencyReview).where(
                col(StoryDependencyReview.project_id) == project_id
            )
        ).one()
        edges = [_copy_dependency_edge(item) for item in canonical_edges]
        if corruption == "malformed_json":
            review.reviewed_edges_json = "{"
        elif corruption == "fingerprint_mismatch":
            review.dependency_fingerprint = "sha256:tampered"
        elif corruption == "duplicate_edge":
            edges.append(_copy_dependency_edge(edges[0]))
            review.reviewed_edges_json = canonical_json(edges)
            review.dependency_fingerprint = canonical_hash(edges)
        elif corruption == "reversed_order":
            edges.reverse()
            review.reviewed_edges_json = canonical_json(edges)
            review.dependency_fingerprint = canonical_hash(edges)
        elif corruption == "unknown_endpoint":
            edges[0]["dependent_story_id"] = max(story_ids) + 10_000
            review.reviewed_edges_json = canonical_json(edges)
            review.dependency_fingerprint = canonical_hash(edges)
        elif corruption == "cross_project_endpoint":
            edges[0]["dependent_story_id"] = foreign_story_id
            selected = tuple(sorted((*story_ids, foreign_story_id)))
            review.selected_story_ids_json = canonical_json(list(selected))
            review.reviewed_edges_json = canonical_json(edges)
            review.dependency_fingerprint = canonical_hash(edges)
        else:
            edges = [
                {
                    "dependent_story_id": story_ids[0],
                    "prerequisite_story_id": story_ids[1],
                    "reason": "First requires second.",
                },
                {
                    "dependent_story_id": story_ids[1],
                    "prerequisite_story_id": story_ids[0],
                    "reason": "Second requires first.",
                },
            ]
            review.reviewed_edges_json = canonical_json(edges)
            review.dependency_fingerprint = canonical_hash(edges)
        session.add(review)
        session.commit()
        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(project_id)


def test_contradictory_terminal_planning_decision_fails_fact_conflict(
    engine: Engine,
) -> None:
    """Reject contradictory terminal decisions as a workflow fact conflict."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    backlog_reference = _decision(
        position, "planning.roadmap.generate"
    ).fact_references[0]
    content = _roadmap_content()
    recorded = domain.transition(
        RecordRoadmapDraft(
            **_guards(position, "planning.roadmap.generate"),
            idempotency_key="conflict-record-roadmap",
            backlog_artifact_id=int(backlog_reference.fact_id),
            backlog_artifact_fingerprint=backlog_reference.fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    roadmap_id = _output_int(recorded, "roadmap_artifact_id")
    position = domain.position(project_id)
    guards = _guards(position, "planning.roadmap.review")
    first = domain.transition(
        DecideRoadmap(
            **guards,
            idempotency_key="first-roadmap-decision",
            roadmap_artifact_id=roadmap_id,
            artifact_fingerprint=canonical_hash(content),
            decision="accepted",
            rationale="Accept.",
        )
    )
    assert first.ok is True
    second = domain.transition(
        DecideRoadmap(
            **guards,
            idempotency_key="second-roadmap-decision",
            roadmap_artifact_id=roadmap_id,
            artifact_fingerprint=canonical_hash(content),
            decision="rejected",
            rationale="Contradiction.",
        )
    )
    assert second.ok is False
    assert second.error is not None
    assert second.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


def test_handler_failure_after_flush_rolls_back_business_audit_and_receipt(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back business rows, audit, and receipt after a flushed failure."""
    import services.agent_workbench.roadmap_phase as roadmap_phase_module  # noqa: PLC0415

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    backlog_reference = _decision(
        position, "planning.roadmap.generate"
    ).fact_references[0]
    content = _roadmap_content()
    request = RecordRoadmapDraft(
        **_guards(position, "planning.roadmap.generate"),
        idempotency_key="rollback-roadmap",
        backlog_artifact_id=int(backlog_reference.fact_id),
        backlog_artifact_fingerprint=backlog_reference.fingerprint,
        canonical_content=content,
        content_fingerprint=canonical_hash(content),
    )
    original = roadmap_phase_module.record_roadmap_draft_in_session

    def fail_after_flush(
        session: Session,
        *,
        inputs: roadmap_phase_module.RecordRoadmapDraftInput,
    ) -> NoReturn:
        original(session, inputs=inputs)
        raise _ForcedPlanningError

    monkeypatch.setattr(
        roadmap_phase_module, "record_roadmap_draft_in_session", fail_after_flush
    )
    with pytest.raises(_ForcedPlanningError):
        domain.transition(request)
    with Session(engine) as session:
        assert session.exec(select(RoadmapArtifact)).all() == []
        assert (
            session.exec(
                select(WorkflowEvent).where(
                    col(WorkflowEvent.event_type) == WorkflowEventType.ROADMAP_SAVED
                )
            ).all()
            == []
        )
        assert (
            session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.idempotency_key) == "rollback-roadmap"
                )
            ).all()
            == []
        )


def test_retry_and_replay_are_exact_after_rollback(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve exact retry and replay behavior after rollback."""
    import services.agent_workbench.roadmap_phase as roadmap_phase_module  # noqa: PLC0415

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    backlog_reference = _decision(
        position, "planning.roadmap.generate"
    ).fact_references[0]
    content = _roadmap_content()
    request = RecordRoadmapDraft(
        **_guards(position, "planning.roadmap.generate"),
        idempotency_key="retry-roadmap",
        backlog_artifact_id=int(backlog_reference.fact_id),
        backlog_artifact_fingerprint=backlog_reference.fingerprint,
        canonical_content=content,
        content_fingerprint=canonical_hash(content),
    )
    original = roadmap_phase_module.record_roadmap_draft_in_session

    def fail_first_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise _ForcedPlanningError

    monkeypatch.setattr(
        roadmap_phase_module,
        "record_roadmap_draft_in_session",
        fail_first_call,
    )
    with pytest.raises(_ForcedPlanningError):
        domain.transition(request)
    monkeypatch.setattr(
        roadmap_phase_module,
        "record_roadmap_draft_in_session",
        original,
    )
    first = domain.transition(request)
    replay = domain.transition(request)
    assert first.ok is True
    assert replay.ok is True
    assert replay.replayed is True
    assert replay.output == first.output

    changed_content = _roadmap_content()
    changed_content["roadmap_summary"] = "Different semantic input"
    conflict = domain.transition(
        request.model_copy(
            update={
                "canonical_content": changed_content,
                "content_fingerprint": canonical_hash(changed_content),
            }
        )
    )
    assert conflict.ok is False
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


def test_concurrent_distinct_roadmap_requests_serialize_at_one_chain_head(
    tmp_path: Path,
) -> None:
    """One SQLite writer wins; the second observes stale committed facts."""
    race_engine = create_engine(
        f"sqlite:///{tmp_path / 'task-7-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(race_engine)
    try:
        project_id = _seed_accepted_backlog(race_engine)
        domain = _domain(race_engine)
        position = domain.position(project_id)
        backlog_reference = _decision(
            position,
            "planning.roadmap.generate",
        ).fact_references[0]
        barrier = threading.Barrier(2)

        def record(index: int) -> TransitionResult:
            content = _roadmap_content()
            content["roadmap_summary"] = f"Concurrent candidate {index}"
            request = RecordRoadmapDraft(
                **_guards(position, "planning.roadmap.generate"),
                idempotency_key=f"concurrent-roadmap-{index}",
                backlog_artifact_id=int(backlog_reference.fact_id),
                backlog_artifact_fingerprint=backlog_reference.fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
            )
            barrier.wait()
            return domain.transition(request)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(record, (1, 2)))

        successes = [result for result in results if result.ok]
        failures = [result for result in results if not result.ok]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error is not None
        assert failures[0].error.code in {
            WorkflowErrorCode.STALE_POSITION,
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        }
        with Session(race_engine) as session:
            artifacts = session.exec(select(RoadmapArtifact)).all()
            decisions = session.exec(select(RoadmapArtifactDecision)).all()
            assert len(artifacts) == 1
            assert artifacts[0].version_number == 1
            assert decisions == []
            assert (
                session.exec(
                    select(WorkflowEvent).where(
                        col(WorkflowEvent.event_type) == WorkflowEventType.ROADMAP_SAVED
                    )
                ).all()
                == []
            )
    finally:
        race_engine.dispose()


def test_apply_dependencies_rejects_cycle_without_persisting_edges(
    engine: Engine,
) -> None:
    """Reject cyclic dependency review without persisting edges."""
    project_id = _seed_accepted_backlog(
        engine,
        requirements=("Plan immutable work", "Validate planning work"),
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain,
        project_id,
        requirements=("Plan immutable work", "Validate planning work"),
    )
    _first_artifact, first_story_id = _record_and_accept_story(
        engine, domain, project_id
    )
    _second_artifact, second_story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
        requirement="Validate planning work",
        idempotency_suffix="-second",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    position = domain.position(project_id)
    result = domain.transition(
        ApplyStoryDependencies(
            **_guards(position, "planning.story_dependencies"),
            idempotency_key="cycle-dependencies",
            selected_story_ids=tuple(sorted((first_story_id, second_story_id))),
            reviewed_edges=(
                ReviewedDependencyEdge(
                    dependent_story_id=first_story_id,
                    prerequisite_story_id=second_story_id,
                    reason="First depends on second.",
                ),
                ReviewedDependencyEdge(
                    dependent_story_id=second_story_id,
                    prerequisite_story_id=first_story_id,
                    reason="Second depends on first.",
                ),
            ),
            source_fingerprint=story_dependency_source_fingerprint(snapshot.stories),
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert session.exec(select(StoryDependencyReview)).all() == []


def test_transition_adapter_rejects_unknown_request_shape() -> None:
    """Reject unknown planning transition request shapes."""
    adapter = TypeAdapter(TransitionRequest)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "planning_escape_hatch", "action": {}})
