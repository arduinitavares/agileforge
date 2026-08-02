"""Persisted planning transitions, transaction, and idempotency tests."""

# ruff: noqa: D103

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn, TypedDict, get_args

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

import services.agent_workbench.roadmap_phase as roadmap_phase_module
import services.agent_workbench.sprint_phase as sprint_phase_module
import services.agent_workbench.story_phase as story_phase_module
import services.sprint_input as sprint_input_module
import services.story_dependencies as story_dependencies_module
import workflow.handlers.planning as planning_handlers
from models.core import Product, Sprint, SprintStory, Task, Team, UserStory
from models.enums import SprintStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    StoryArtifact,
    StoryArtifactDecision,
    StoryDependencyReview,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactRepository
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.clock import FixedClock
from workflow.contracts import (
    JsonObject,
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
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
EXPECTED_REQUEST_VARIANT_COUNT = 30
EXPECTED_PLANNING_REQUEST_COUNT = 9
REPAIRED_STORY_POINTS = 3
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
    story_dependencies_module: {"apply_story_dependencies_in_session"},
    sprint_input_module: {"candidate_set_in_session"},
}


class _RequestGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    actor: str
    correlation_id: str


class _ForcedPlanningError(RuntimeError):
    """Controlled transition failure used to prove rollback behavior."""


def _authority_artifact() -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Planning"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="b" * 64,
    )


def _backlog_content(*requirements: str) -> JsonObject:
    return {
        "backlog_items": [
            {
                "priority": index,
                "requirement": requirement,
                "authority_ref": f"REQ.{index}",
                "capability_hint": None,
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


def _seed_accepted_backlog(
    engine: Engine,
    *,
    requirements: tuple[str, ...] = ("Plan immutable work",),
) -> int:
    authority_artifact = _authority_artifact()
    with Session(engine) as session:
        project = Product(name=f"Task 11 {requirements!r}", origin="greenfield")
        session.add(project)
        session.flush()
        assert project.product_id is not None
        spec = SpecRegistry(
            product_id=project.product_id,
            spec_hash="sha256:task-11-spec",
            content='{"scope":"task-11"}',
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="operator@example.com",
        )
        session.add(spec)
        session.flush()
        assert spec.spec_version_id is not None
        authority = CompiledSpecAuthority(
            spec_version_id=spec.spec_version_id,
            compiler_version=authority_artifact.compiler_version,
            prompt_hash=authority_artifact.prompt_hash,
            compiled_at=EVALUATED_AT,
            compiled_artifact_json=authority_artifact.model_dump_json(),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.flush()
        assert authority.authority_id is not None
        authority_fingerprint = pending_authority_fingerprint(authority)
        assert authority_fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=project.product_id,
                spec_version_id=spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT,
                rationale="Accepted for planning.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority.authority_id,
                authority_fingerprint=authority_fingerprint,
                review_fingerprint="sha256:review",
                terminal_decision_key="task-11-authority",
            )
        )
        content = _backlog_content(*requirements)
        fingerprint = canonical_hash(content)
        backlog = BacklogArtifact(
            project_id=project.product_id,
            authority_id=authority.authority_id,
            authority_fingerprint=authority_fingerprint,
            version_number=1,
            canonical_content_json=canonical_json(content),
            content_fingerprint=fingerprint,
            created_by="operator@example.com",
            created_at=EVALUATED_AT,
        )
        session.add(backlog)
        session.flush()
        assert backlog.backlog_artifact_id is not None
        session.add(
            BacklogArtifactDecision(
                project_id=project.product_id,
                backlog_artifact_id=backlog.backlog_artifact_id,
                artifact_fingerprint=fingerprint,
                decision="accepted",
                rationale="Accepted backlog.",
                reviewer="operator@example.com",
                idempotency_key="seed-backlog",
                decided_at=EVALUATED_AT,
            )
        )
        session.commit()
        return project.product_id


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


def _roadmap_content(requirement: str = "Plan immutable work") -> JsonObject:
    return {
        "roadmap_releases": [
            {
                "release_name": "Milestone 1",
                "theme": "Planning",
                "focus_area": "Technical Foundation",
                "items": [requirement],
                "reasoning": "Build durable planning facts first.",
            }
        ],
        "roadmap_summary": "Deliver the accepted backlog in dependency order.",
        "is_complete": True,
        "clarifying_questions": [],
    }


def _story_content(requirement: str = "Plan immutable work") -> JsonObject:
    return {
        "parent_requirement": requirement,
        "user_stories": [
            {
                "story_title": "Persist planning facts",
                "statement": (
                    "As an operator, I want durable planning facts, so that routing "
                    "survives restarts."
                ),
                "acceptance_criteria": ["Verify that planning survives restart."],
                "invest_score": "High",
                "estimated_effort": "M",
                "produced_artifacts": ["planning records"],
                "research_caveats": [],
                "dependency_candidates": [],
            }
        ],
        "quality_schema_version": "agileforge.story_quality.v1",
        "coverage_status": "complete",
        "remaining_scope": [],
        "quality_findings": [],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _sprint_plan(story_id: int) -> JsonObject:
    return {
        "sprint_goal": "Persist planning workflow facts.",
        "sprint_number": 1,
        "selected_stories": [
            {
                "story_id": story_id,
                "story_title": "Persist planning facts",
                "tasks": [
                    {
                        "description": "Implement planning persistence",
                        "task_kind": "implementation",
                        "artifact_targets": ["planning workflow handler"],
                        "workstream_tags": ["workflow"],
                        "relevant_invariant_ids": [],
                        "checklist_items": ["Run focused tests"],
                    }
                ],
                "reason_for_selection": "Required for durable routing.",
            }
        ],
        "deselected_stories": [],
        "capacity_analysis": {
            "capacity_points": 3,
            "capacity_source": "user_override",
            "capacity_basis": "One medium story.",
            "selected_count": 1,
            "story_points_used": 3,
            "remaining_capacity_points": 0,
            "commitment_note": "The selected scope is achievable.",
            "reasoning": "The plan fits the supplied capacity.",
        },
    }


def _record_and_accept_roadmap(domain: WorkflowDomain, project_id: int) -> int:
    position = domain.position(project_id)
    content = _roadmap_content()
    backlog_reference = _decision(
        position,
        "planning.roadmap.generate",
    ).fact_references[0]
    recorded = domain.transition(
        RecordRoadmapDraft(
            **_guards(position, "planning.roadmap.generate"),
            idempotency_key="record-roadmap",
            backlog_artifact_id=int(backlog_reference.fact_id),
            backlog_artifact_fingerprint=backlog_reference.fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    artifact_id = _output_int(recorded, "roadmap_artifact_id")
    fingerprint = str(recorded.output["content_fingerprint"])
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideRoadmap(
            **_guards(position, "planning.roadmap.review"),
            idempotency_key="accept-roadmap",
            roadmap_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Roadmap covers the accepted backlog.",
        )
    )
    assert accepted.ok is True
    return artifact_id


def _record_and_accept_story(
    domain: WorkflowDomain, project_id: int
) -> tuple[int, int]:
    position = domain.position(project_id)
    generate = next(
        item for item in position.decisions if item.node_id == "planning.story.generate"
    )
    assert generate.instance_key is not None
    requirement_id = generate.instance_key.removeprefix("requirement:")
    roadmap_reference = next(
        item for item in generate.fact_references if item.fact_type == "roadmap"
    )
    content = _story_content()
    recorded = domain.transition(
        RecordStoryDraft(
            **_guards(position, "planning.story.generate", generate.instance_key),
            idempotency_key="record-story",
            requirement_id=requirement_id,
            roadmap_artifact_id=int(roadmap_reference.fact_id),
            roadmap_artifact_fingerprint=roadmap_reference.fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    artifact_id = _output_int(recorded, "story_artifact_id")
    story_id = _output_first_int(recorded, "story_ids")
    fingerprint = str(recorded.output["content_fingerprint"])
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideStory(
            **_guards(
                position,
                "planning.story.review",
                f"requirement:{requirement_id}",
            ),
            idempotency_key="accept-story",
            requirement_id=requirement_id,
            story_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Story content is complete.",
        )
    )
    assert accepted.ok is True
    return artifact_id, story_id


def test_closed_union_adds_exactly_nine_typed_planning_requests() -> None:
    variants = get_args(TransitionRequest.__value__)
    assert len(variants) == EXPECTED_REQUEST_VARIANT_COUNT
    assert set(PLANNING_REQUESTS).issubset(set(variants))
    assert len(set(PLANNING_REQUESTS)) == EXPECTED_PLANNING_REQUEST_COUNT


@pytest.mark.parametrize("request_type", PLANNING_REQUESTS)
def test_planning_requests_inherit_positioned_guard_without_expected_state(
    request_type: type[PositionedRequest],
) -> None:
    assert issubclass(request_type, PositionedRequest)
    assert "expected_state" not in request_type.model_fields


def test_story_request_instance_key_is_exact_requirement_id() -> None:
    payload = {
        "project_id": 1,
        "graph_version": "graph",
        "fact_fingerprint": "facts",
        "decision_fingerprint": "decision",
        "idempotency_key": "story",
        "actor": "operator",
        "requirement_id": "req-a",
        "roadmap_artifact_id": 1,
        "roadmap_artifact_fingerprint": "roadmap",
        "canonical_content": _story_content(),
        "content_fingerprint": "story",
    }
    request = RecordStoryDraft.model_validate(payload)
    assert request.decision_instance_key() == "requirement:req-a"
    with pytest.raises(ValidationError):
        RecordStoryDraft.model_validate(
            {**payload, "instance_key": "requirement:req-b"}
        )


def test_planning_service_mutations_use_only_caller_owned_session() -> None:
    forbidden_calls = {"commit", "rollback", "close"}
    for module, function_names in CALLER_SESSION_FUNCTIONS.items():
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
            assert "fsm_state" not in inspect.getsource(function)
            assert "expected_state" not in inspect.getsource(function)


def test_roadmap_and_story_transitions_persist_immutable_reviewed_artifacts(
    engine: Engine,
) -> None:
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    roadmap_id = _record_and_accept_roadmap(domain, project_id)
    story_artifact_id, story_id = _record_and_accept_story(domain, project_id)

    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        story_artifact = session.get(StoryArtifact, story_artifact_id)
        story = session.get(UserStory, story_id)
        assert roadmap is not None
        assert story_artifact is not None
        assert story is not None
        assert story.source_requirement == "plan immutable work"
        assert story.is_refined is True
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
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
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
        story.story_points = None
        story.rank = None
        session.add(story)
        session.commit()
        snapshot = WorkflowFactRepository(session).load(project_id)
    expected_readiness = readiness_fingerprint(snapshot.stories)
    position = domain.position(project_id)
    repaired = domain.transition(
        RepairStoryReadiness(
            **_guards(position, "planning.story_readiness"),
            idempotency_key="repair-readiness",
            story_ids=(story_id,),
            repairs=(
                StoryReadinessUpdate(story_id=story_id, story_points=3, rank="1.1"),
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
        assert repaired_story.rank == "1.1"


def test_sprint_plan_review_and_start_bind_exact_plan_and_candidate_set(
    engine: Engine,
) -> None:
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    with Session(engine) as session:
        team = Team(name="Task 11 Team")
        session.add(team)
        session.commit()
        snapshot = WorkflowFactRepository(session).load(project_id)
    current_candidate_set = candidate_set_fingerprint(
        snapshot.stories,
        snapshot.story_dependencies,
    )
    plan = _sprint_plan(story_id)
    position = domain.position(project_id)
    recorded = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key="record-sprint-plan",
            team_name="Task 11 Team",
            selected_story_ids=(story_id,),
            canonical_task_plan=plan,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=current_candidate_set,
        )
    )
    assert recorded.ok is True
    plan_id = _output_int(recorded, "sprint_plan_artifact_id")
    sprint_id = _output_int(recorded, "sprint_id")
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="accept-sprint-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=canonical_hash(plan),
            decision="accepted",
            rationale="Plan is feasible.",
        )
    )
    assert accepted.ok is True
    position = domain.position(project_id)
    started = domain.transition(
        StartSprint(
            **_guards(position, "planning.sprint.start"),
            idempotency_key="start-sprint",
            sprint_plan_artifact_id=plan_id,
            sprint_id=sprint_id,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=current_candidate_set,
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


def test_story_or_dependency_change_rejects_stale_plan_start(engine: Engine) -> None:
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    with Session(engine) as session:
        session.add(Team(name="Stale Plan Team"))
        session.commit()
        snapshot = WorkflowFactRepository(session).load(project_id)
    current_candidate_set = candidate_set_fingerprint(
        snapshot.stories,
        snapshot.story_dependencies,
    )
    plan = _sprint_plan(story_id)
    position = domain.position(project_id)
    recorded = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key="stale-record-plan",
            team_name="Stale Plan Team",
            selected_story_ids=(story_id,),
            canonical_task_plan=plan,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=current_candidate_set,
        )
    )
    assert recorded.ok is True
    plan_id = _output_int(recorded, "sprint_plan_artifact_id")
    sprint_id = _output_int(recorded, "sprint_id")
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="stale-accept-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=canonical_hash(plan),
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
            sprint_plan_artifact_id=plan_id,
            sprint_id=sprint_id,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=current_candidate_set,
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION


def test_contradictory_terminal_planning_decision_fails_fact_conflict(
    engine: Engine,
) -> None:
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
    original = planning_handlers.record_roadmap_draft_in_session

    def fail_after_flush(  # noqa: PLR0913
        session: Session,
        *,
        project_id: int,
        backlog_artifact_id: int,
        backlog_artifact_fingerprint: str,
        canonical_content: JsonObject,
        content_fingerprint: str,
        supersedes_roadmap_artifact_id: int | None,
        actor: str,
        recorded_at: datetime,
    ) -> NoReturn:
        original(
            session,
            project_id=project_id,
            backlog_artifact_id=backlog_artifact_id,
            backlog_artifact_fingerprint=backlog_artifact_fingerprint,
            canonical_content=canonical_content,
            content_fingerprint=content_fingerprint,
            supersedes_roadmap_artifact_id=supersedes_roadmap_artifact_id,
            actor=actor,
            recorded_at=recorded_at,
        )
        raise _ForcedPlanningError

    monkeypatch.setattr(
        planning_handlers, "record_roadmap_draft_in_session", fail_after_flush
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
    original = planning_handlers.record_roadmap_draft_in_session

    def fail_first_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise _ForcedPlanningError

    monkeypatch.setattr(
        planning_handlers,
        "record_roadmap_draft_in_session",
        fail_first_call,
    )
    with pytest.raises(_ForcedPlanningError):
        domain.transition(request)
    monkeypatch.setattr(planning_handlers, "record_roadmap_draft_in_session", original)
    first = domain.transition(request)
    replay = domain.transition(request)
    assert first.ok is True
    assert replay.ok is True
    assert replay.replayed is True
    assert replay.output == first.output


def test_apply_dependencies_rejects_cycle_without_persisting_edges(
    engine: Engine,
) -> None:
    project_id = _seed_accepted_backlog(
        engine,
        requirements=("Plan immutable work", "Validate planning work"),
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _first_artifact, first_story_id = _record_and_accept_story(domain, project_id)
    position = domain.position(project_id)
    second_generate = next(
        item for item in position.decisions if item.node_id == "planning.story.generate"
    )
    second_requirement_id = str(second_generate.instance_key).removeprefix(
        "requirement:"
    )
    roadmap_reference = next(
        item for item in second_generate.fact_references if item.fact_type == "roadmap"
    )
    second_content = _story_content("Validate planning work")
    second_recorded = domain.transition(
        RecordStoryDraft(
            **_guards(
                position, "planning.story.generate", second_generate.instance_key
            ),
            idempotency_key="record-second-story",
            requirement_id=second_requirement_id,
            roadmap_artifact_id=int(roadmap_reference.fact_id),
            roadmap_artifact_fingerprint=roadmap_reference.fingerprint,
            canonical_content=second_content,
            content_fingerprint=canonical_hash(second_content),
        )
    )
    assert second_recorded.ok is True
    second_artifact_id = _output_int(second_recorded, "story_artifact_id")
    second_story_id = _output_first_int(second_recorded, "story_ids")
    position = domain.position(project_id)
    assert domain.transition(
        DecideStory(
            **_guards(
                position,
                "planning.story.review",
                f"requirement:{second_requirement_id}",
            ),
            idempotency_key="accept-second-story",
            requirement_id=second_requirement_id,
            story_artifact_id=second_artifact_id,
            artifact_fingerprint=canonical_hash(second_content),
            decision="accepted",
            rationale="Accept second story.",
        )
    ).ok
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
    adapter = TypeAdapter(TransitionRequest)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "planning_escape_hatch", "action": {}})
