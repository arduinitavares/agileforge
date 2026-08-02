"""Persisted planning transitions, transaction, and idempotency tests."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn, TypedDict, Unpack, get_args

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

import services.agent_workbench.roadmap_phase as roadmap_phase_module
import services.agent_workbench.sprint_phase as sprint_phase_module
import services.agent_workbench.story_phase as story_phase_module
import services.sprint_input as sprint_input_module
import services.story_dependencies as story_dependencies_module
import workflow.handlers.planning as planning_handlers
from models.core import (
    Product,
    Sprint,
    SprintStory,
    Task,
    Team,
    UserStory,
    UserStoryDependency,
)
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
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.clock import FixedClock
from workflow.contracts import (
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
EXPECTED_REQUEST_VARIANT_COUNT = 44
EXPECTED_PLANNING_REQUEST_COUNT = 9
REPAIRED_STORY_POINTS = 3
EXPECTED_DEPENDENCY_STORY_COUNT = 3
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


def _replace_authority_and_backlog(engine: Engine, project_id: int) -> None:
    """Accept a replacement authority and Backlog, obsoleting prior lineage."""
    authority_artifact = _authority_artifact()
    with Session(engine) as session:
        current_spec = session.exec(
            select(SpecRegistry).where(
                col(SpecRegistry.product_id) == project_id,
                col(SpecRegistry.status) == "approved",
            )
        ).one()
        current_spec.status = "superseded"
        session.add(current_spec)
        replacement_spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:task-11-replacement-spec",
            content='{"scope":"task-11-replacement"}',
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="operator@example.com",
        )
        session.add(replacement_spec)
        session.flush()
        assert replacement_spec.spec_version_id is not None
        replacement_authority = CompiledSpecAuthority(
            spec_version_id=replacement_spec.spec_version_id,
            compiler_version=authority_artifact.compiler_version,
            prompt_hash="c" * 64,
            compiled_at=EVALUATED_AT,
            compiled_artifact_json=authority_artifact.model_copy(
                update={"prompt_hash": "c" * 64}
            ).model_dump_json(),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(replacement_authority)
        session.flush()
        assert replacement_authority.authority_id is not None
        authority_fingerprint = pending_authority_fingerprint(replacement_authority)
        assert authority_fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=project_id,
                spec_version_id=replacement_spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT,
                rationale="Accepted replacement authority.",
                compiler_version=replacement_authority.compiler_version,
                prompt_hash=replacement_authority.prompt_hash,
                spec_hash=replacement_spec.spec_hash,
                pending_authority_id=replacement_authority.authority_id,
                authority_fingerprint=authority_fingerprint,
                review_fingerprint="sha256:replacement-review",
                terminal_decision_key="task-11-replacement-authority",
            )
        )
        old_backlog = session.exec(
            select(BacklogArtifact)
            .where(col(BacklogArtifact.project_id) == project_id)
            .order_by(col(BacklogArtifact.version_number).desc())
        ).first()
        assert old_backlog is not None
        assert old_backlog.backlog_artifact_id is not None
        replacement_content = _backlog_content("Plan replacement authority work")
        replacement_fingerprint = canonical_hash(replacement_content)
        replacement_backlog = BacklogArtifact(
            project_id=project_id,
            authority_id=replacement_authority.authority_id,
            authority_fingerprint=authority_fingerprint,
            version_number=old_backlog.version_number + 1,
            canonical_content_json=canonical_json(replacement_content),
            content_fingerprint=replacement_fingerprint,
            supersedes_backlog_artifact_id=old_backlog.backlog_artifact_id,
            created_by="operator@example.com",
            created_at=EVALUATED_AT,
        )
        session.add(replacement_backlog)
        session.flush()
        assert replacement_backlog.backlog_artifact_id is not None
        session.add(
            BacklogArtifactDecision(
                project_id=project_id,
                backlog_artifact_id=replacement_backlog.backlog_artifact_id,
                artifact_fingerprint=replacement_fingerprint,
                decision="accepted",
                rationale="Accepted replacement Backlog.",
                reviewer="operator@example.com",
                idempotency_key="seed-replacement-backlog",
                decided_at=EVALUATED_AT,
            )
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
                "items": list(requirements),
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


def _record_and_accept_roadmap(
    domain: WorkflowDomain,
    project_id: int,
    *,
    requirements: tuple[str, ...] = ("Plan immutable work",),
) -> int:
    position = domain.position(project_id)
    content = _roadmap_content(*requirements)
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
    domain: WorkflowDomain,
    project_id: int,
    *,
    requirement: str = "Plan immutable work",
    idempotency_suffix: str = "",
) -> tuple[int, int]:
    position = domain.position(project_id)
    requirement_id = " ".join(requirement.strip().lower().split())
    instance_key = f"requirement:{requirement_id}"
    generate = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story.generate"
        and item.instance_key == instance_key
    )
    assert generate.instance_key is not None
    roadmap_reference = next(
        item for item in generate.fact_references if item.fact_type == "roadmap"
    )
    content = _story_content(requirement)
    recorded = domain.transition(
        RecordStoryDraft(
            **_guards(position, "planning.story.generate", generate.instance_key),
            idempotency_key=f"record-story{idempotency_suffix}",
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
            idempotency_key=f"accept-story{idempotency_suffix}",
            requirement_id=requirement_id,
            story_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Story content is complete.",
        )
    )
    assert accepted.ok is True
    return artifact_id, story_id


def _record_sprint_plan_draft(
    engine: Engine,
    domain: WorkflowDomain,
    project_id: int,
    story_id: int,
    **options: Unpack[_SprintDraftOptions],
) -> tuple[int, int, str, JsonObject]:
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
        session.add(Team(name=team_name))
        session.commit()
        snapshot = WorkflowFactRepository(session).load(project_id)
    candidate_fingerprint = candidate_set_fingerprint(
        snapshot.stories,
        snapshot.story_dependencies,
    )
    plan = _sprint_plan(story_id)
    position = domain.position(project_id)
    recorded = domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key=idempotency_key,
            team_name=team_name,
            selected_story_ids=(story_id,),
            canonical_task_plan=plan,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=candidate_fingerprint,
        )
    )
    assert recorded.ok is True
    return (
        _output_int(recorded, "sprint_plan_artifact_id"),
        _output_int(recorded, "sprint_id"),
        candidate_fingerprint,
        plan,
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
    stories = tuple(item for item in snapshot.stories if item.sprint_candidate)
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
    project_id = _seed_accepted_backlog(engine)
    with Session(engine) as session:
        stories = [
            UserStory(
                product_id=project_id,
                title=f"Story {index}",
                source_requirement=f"requirement-{index}",
                refinement_slot=1,
                story_origin="refined",
                is_refined=True,
                story_points=3,
                rank=f"1.{index}",
            )
            for index in range(1, 4)
        ]
        session.add_all(stories)
        foreign_project = Product(
            name="Foreign dependency Project",
            origin="greenfield",
        )
        session.add(foreign_project)
        session.flush()
        assert foreign_project.product_id is not None
        foreign_story = UserStory(
            product_id=foreign_project.product_id,
            title="Foreign Story",
            source_requirement="foreign-requirement",
            refinement_slot=1,
            story_origin="refined",
            is_refined=True,
            story_points=3,
            rank="1.1",
        )
        session.add(foreign_story)
        session.flush()
        story_ids = tuple(
            sorted(story.story_id for story in stories if story.story_id is not None)
        )
        assert len(story_ids) == EXPECTED_DEPENDENCY_STORY_COUNT
        assert foreign_story.story_id is not None
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
                    product_id=project_id,
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
        return project_id, foreign_story.story_id, story_ids, edges


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


def test_story_request_instance_key_is_exact_requirement_id() -> None:
    """Derive the exact requirement-scoped Story instance key."""
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
    """Keep planning mutation transaction ownership in WorkflowDomain."""
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
    """Persist immutable Roadmap and Story artifacts with append-only reviews."""
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
    """Bind dependency and readiness transitions to exact current Stories."""
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
    """Bind Sprint plan review and start to exact plan and candidate facts."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="plan-review-dependencies",
    )
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
    """Reject Sprint start after Story or dependency facts change."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="stale-plan-dependencies",
    )
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


def test_stale_authority_roadmap_review_writes_no_terminal_decision(
    engine: Engine,
) -> None:
    """Reject a persisted Roadmap review after authority and Backlog replacement."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    backlog_reference = next(
        item
        for item in _decision(
            position, "planning.roadmap.generate"
        ).fact_references
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

    _replace_authority_and_backlog(engine, project_id)
    current = domain.position(project_id)
    review = _decision(current, "planning.roadmap.review")
    assert review.category is NodeCategory.INVALID
    assert review.reason_code == "ROADMAP_REVIEW_SOURCE_STALE"
    rejected = domain.transition(
        DecideRoadmap(
            **_guards(current, "planning.roadmap.review"),
            idempotency_key="reject-stale-authority-roadmap-review",
            roadmap_artifact_id=roadmap_id,
            artifact_fingerprint=canonical_hash(content),
            decision="accepted",
            rationale="This stale review must not persist.",
        )
    )
    assert rejected.ok is False
    assert rejected.error is not None
    assert rejected.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        decisions = session.exec(
            select(RoadmapArtifactDecision).where(
                col(RoadmapArtifactDecision.roadmap_artifact_id) == roadmap_id
            )
        ).all()
        assert decisions == []


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
    requirement_id = generate.instance_key.removeprefix("requirement:")
    story_content = _story_content()
    recorded_story = domain.transition(
        RecordStoryDraft(
            **_guards(position, "planning.story.generate", generate.instance_key),
            idempotency_key="stale-story-draft",
            requirement_id=requirement_id,
            roadmap_artifact_id=roadmap_id,
            roadmap_artifact_fingerprint=canonical_hash(_roadmap_content()),
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
        f"requirement:{requirement_id}",
    )
    assert review.category is NodeCategory.INVALID
    assert review.reason_code == "STORY_REVIEW_SOURCE_STALE"
    rejected = domain.transition(
        DecideStory(
            **_guards(
                current,
                "planning.story.review",
                f"requirement:{requirement_id}",
            ),
            idempotency_key="reject-stale-story-review",
            requirement_id=requirement_id,
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


def test_stale_sprint_plan_review_writes_no_terminal_decision(engine: Engine) -> None:
    """Reject Sprint-plan review after candidate readiness changes."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    plan_id, _sprint_id, _candidate_fingerprint, plan = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Stale Review Team",
        idempotency_key="stale-review-plan-draft",
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
            plan_fingerprint=canonical_hash(plan),
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
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    plan_id, sprint_id, candidate_fingerprint, plan = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Task Tamper Team",
        idempotency_key="task-tamper-plan-draft",
    )
    review_position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(review_position, "planning.sprint.review"),
            idempotency_key="accept-task-tamper-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=canonical_hash(plan),
            decision="accepted",
            rationale="Review exact task content.",
        )
    )
    assert accepted.ok is True
    start_position = domain.position(project_id)
    with Session(engine) as session:
        before = WorkflowFactRepository(session).load(project_id)
        task = session.exec(select(Task).where(col(Task.story_id) == story_id)).one()
        task.description = "Tampered after plan review"
        task.metadata_json = serialize_task_metadata(
            TaskMetadata(
                task_kind="testing",
                artifact_targets=["unreviewed artifact"],
                workstream_tags=["tampered"],
            )
        )
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
            sprint_plan_artifact_id=plan_id,
            sprint_id=sprint_id,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=candidate_fingerprint,
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    with Session(engine) as session:
        sprint = session.get(Sprint, sprint_id)
        assert sprint is not None
        assert sprint.status is SprintStatus.PLANNED


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
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name=f"Metadata Validation {len(metadata_json)}",
        idempotency_key=f"metadata-validation-{len(metadata_json)}",
    )
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

    def fail_after_flush(
        session: Session,
        *,
        inputs: roadmap_phase_module.RecordRoadmapDraftInput,
    ) -> NoReturn:
        original(session, inputs=inputs)
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
    """Preserve exact retry and replay behavior after rollback."""
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
    """Reject cyclic dependency review without persisting edges."""
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
    """Reject unknown planning transition request shapes."""
    adapter = TypeAdapter(TransitionRequest)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "planning_escape_hatch", "action": {}})
