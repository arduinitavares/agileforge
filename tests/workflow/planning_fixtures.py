"""Reusable persisted planning fixture helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict
from unittest.mock import patch

from pydantic import TypeAdapter
from sqlmodel import Session

from models.core import Project, UserStory
from repositories.workflow import WorkflowFactRepository
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    InvestDimensionAssessment,
    StoryInvestAssessment,
    StoryItemEnvelope,
)
from services.specs import story_validation_service as story_validation_service_module
from services.story_sprint_selection import (
    StorySprintSelectionRequest,
    apply_story_sprint_selection_with_receipt_in_session,
    story_sprint_selection_fact_in_session,
)
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    TransitionResult,
    WorkflowPosition,
)
from workflow.definitions.planning import (
    story_dependency_source_fingerprint,
)
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import (
    ApplyStoryDependencies,
    DecideRoadmap,
    DecideStory,
    RecordRoadmapDraft,
    RecordStoryDraft,
)
from workflow.requests.planning import ReviewedDependencyEdge

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.domain import WorkflowDomain

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
_JSON_OBJECT = TypeAdapter(JsonObject)


class _RequestGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    actor: str
    correlation_id: str


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
    backlog_item_id: str | None = None,
    idempotency_suffix: str = "",
) -> tuple[int, int]:
    position = domain.position(project_id)
    generate = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story.generate"
        and item.category is NodeCategory.AVAILABLE
        and item.reason_code != "STORY_CORRECTION_AVAILABLE"
        and (
            backlog_item_id is None
            or any(
                reference.fact_type == "backlog_item"
                and reference.fact_id == backlog_item_id
                for reference in item.fact_references
            )
        )
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
    completed_sprint_ids = {
        sprint.sprint_id for sprint in snapshot.sprints if sprint.status == "completed"
    }
    stories = tuple(
        item
        for item in snapshot.stories
        if item.structurally_eligible
        and item.sprint_selection_state == "selected"
        and not any(sprint_id in completed_sprint_ids for sprint_id in item.sprint_ids)
    )
    reviewed_edges = tuple(
        ReviewedDependencyEdge(
            dependent_story_id=edge.dependent_story_id,
            prerequisite_story_id=edge.prerequisite_story_id,
            reason=edge.reason or "Reviewed dependency.",
        )
        for edge in snapshot.story_dependencies
        if edge.status == "active"
        and edge.dependent_story_id in {item.story_id for item in stories}
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


def _select_for_sprint(engine: Engine, story_id: int) -> None:
    """Select one eligible fixture Story without replaying a lifecycle-locked no-op."""
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        current = story_sprint_selection_fact_in_session(session, story=story)
        if current.selection_state == "selected":
            return
        apply_story_sprint_selection_with_receipt_in_session(
            session,
            StorySprintSelectionRequest(
                project_id=story.project_id,
                story_id=story_id,
                intent="select",
                expected_state_fingerprint=current.state_fingerprint,
                idempotency_key=f"select-planning-story-{story_id}",
                actor="operator@example.com",
            ),
        )
        session.commit()


# Stable shared entry points for fixture consumers.
seed_accepted_backlog = _seed_accepted_backlog
planning_decision = _decision
planning_guards = _guards
planning_output_int = _output_int
planning_output_first_int = _output_first_int
record_and_accept_roadmap = _record_and_accept_roadmap
record_and_accept_story = _record_and_accept_story
apply_current_dependencies = _apply_current_dependencies
select_for_sprint = _select_for_sprint
validate_story_structurally = _validate_story_structurally
