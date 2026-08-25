"""Single-project root graph contract and persisted journey tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from unittest.mock import patch

from git import Repo
from pydantic import TypeAdapter
from sqlmodel import Session, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project, Task
from repositories.workflow import WorkflowFactRepository
from services.contracts.sprint import SprintPlannerOutput
from services.specification_authoring_input import SpecificationStructuringInputService
from services.specs import story_validation_service as story_validation_service_module
from tests.workflow.test_planning_transitions import _select_for_sprint
from tests.workflow.test_product_discovery_transitions import (
    _record_binding,
    _register_source,
    _repository,
)
from utils.agileforge_spec_profile_v2 import (
    SpecificationPayload,
    canonical_spec_hash,
    canonical_spec_json,
)
from utils.task_metadata import parse_task_metadata
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowPosition,
)
from workflow.definitions.planning import (
    story_dependency_source_fingerprint,
)
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.definitions.root import ROOT_GRAPH, project_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    ApplyStoryDependencies,
    CloseSprint,
    CloseStory,
    CompleteSpecificationStructuring,
    CompleteTask,
    DecideBacklog,
    DecideProductGoalReview,
    DecideRoadmap,
    DecideSpecification,
    DecideSprintPlan,
    DecideStory,
    DecideVisionReview,
    FulfillProductGoal,
    GenerateVisionBootstrap,
    RecordBacklogDraft,
    RecordPostSprintTriage,
    RecordProductGoalInterviewTurn,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    ReviewSprint,
    StartNodeAttempt,
    StartSprint,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


JOURNEY_AT = datetime(2026, 8, 9, 12, tzinfo=UTC)
_JSON_OBJECT = TypeAdapter(JsonObject)
_GOLD_SPECIFICATION_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "issue_210"
    / "gold"
    / "canonical-specification.json"
)
_GOLD_SPECIFICATION_HASH = (
    "sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
)
_GOLD_LIFECYCLE_SPEC_ITEM_IDS = ("DATA.001", "REQ.001")


class _RequestGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    actor: str


class _InstanceRequestGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str
    actor: str


@dataclass
class _JourneyClock:
    now_value: datetime

    def now(self) -> datetime:
        current = self.now_value
        self.now_value += timedelta(seconds=1)
        return current

    def advance(self, delta: timedelta) -> None:
        self.now_value += delta


@dataclass(frozen=True)
class _Journey:
    engine: Engine
    domain: WorkflowDomain
    clock: _JourneyClock
    project_id: int


class _ProviderFreeRegistry:
    def require(self, node_id: str) -> object:
        if node_id not in ROOT_GRAPH.agentic_node_ids:
            raise LookupError(node_id)
        return object()


def _decision(
    position: WorkflowPosition,
    node_id: str,
    instance_key: str | None = None,
) -> NodeDecision:
    matches = tuple(
        item
        for item in position.decisions
        if item.node_id == node_id
        and (instance_key is None or item.instance_key == instance_key)
    )
    assert len(matches) == 1, tuple(
        (item.node_id, item.instance_key, item.category, item.reason_code)
        for item in position.decisions
    )
    return matches[0]


def _assert_next(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    category: NodeCategory,
    instance_key: str | None = None,
) -> tuple[WorkflowPosition, NodeDecision]:
    position = domain.position(project_id)
    decision = _decision(position, node_id, instance_key)
    assert decision.category is category
    assert decision.recommendation_kind is RecommendationKind.REQUIRED
    if category is NodeCategory.AVAILABLE:
        assert node_id in position.available_nodes
    elif category is NodeCategory.WAITING:
        assert node_id in position.waiting_nodes
    return position, decision


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
    }


def _instance_guards(
    position: WorkflowPosition,
    node_id: str,
    instance_key: str,
) -> _InstanceRequestGuards:
    guards = _guards(position, node_id, instance_key)
    selected_instance = guards["instance_key"]
    assert selected_instance is not None
    return {
        "project_id": guards["project_id"],
        "graph_version": guards["graph_version"],
        "fact_fingerprint": guards["fact_fingerprint"],
        "decision_fingerprint": guards["decision_fingerprint"],
        "instance_key": selected_instance,
        "actor": guards["actor"],
    }


def _reference(decision: NodeDecision, fact_type: str) -> tuple[int, str]:
    reference = next(
        item for item in decision.fact_references if item.fact_type == fact_type
    )
    return int(reference.fact_id), reference.fingerprint


def _output_int(result: TransitionResult, key: str) -> int:
    value = result.output[key]
    assert isinstance(value, int)
    return value


def _first_output_int(result: TransitionResult, key: str) -> int:
    values = result.output[key]
    assert isinstance(values, tuple)
    value = values[0]
    assert isinstance(value, int)
    return value


def _specification_payload() -> SpecificationPayload:
    return SpecificationPayload.model_validate(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.lifecycle-journey",
            "title": "Lifecycle journey",
            "summary": "Deliver one persisted lifecycle increment.",
            "problem_statement": "Every semantic boundary must survive reload.",
            "items": [
                {
                    "id": "REQ.lifecycle.persist",
                    "type": "REQ",
                    "level": "MUST",
                    "title": "Persist lifecycle facts",
                    "statement": (
                        "Every persisted lifecycle fact MUST include project_id."
                    ),
                    "verification": "system-test",
                    "acceptance": ["The journey reaches post-Sprint triage."],
                }
            ],
        }
    )


def _gold_specification_payload() -> tuple[SpecificationPayload, str]:
    """Load the exact canonical String Calculator delivery root."""
    canonical = _GOLD_SPECIFICATION_PATH.read_text(encoding="utf-8")
    return SpecificationPayload.model_validate_json(canonical), canonical


def _backlog_content(
    requirements: tuple[str, ...],
    *,
    spec_item_ids: tuple[str, ...] = ("REQ.lifecycle.persist",),
) -> JsonObject:
    return {
        "backlog_items": [
            {
                "backlog_item_id": f"PBI-{index:06d}",
                "priority": index,
                "requirement": requirement,
                "spec_item_ids": list(spec_item_ids),
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


def _roadmap_content(requirements: tuple[str, ...]) -> JsonObject:
    return {
        "roadmap_releases": [
            {
                "release_name": "Lifecycle release",
                "theme": "Persistence",
                "focus_area": "Technical Foundation",
                "backlog_item_ids": [
                    f"PBI-{index:06d}"
                    for index, _requirement in enumerate(requirements, start=1)
                ],
                "reasoning": "Deliver the accepted requirements in order.",
            }
        ],
        "roadmap_summary": "Deliver the persisted lifecycle increment.",
        "is_complete": True,
        "clarifying_questions": [],
    }


def _story_content(
    requirement: str,
    ordinal: int,
    *,
    spec_item_ids: tuple[str, ...] = ("REQ.lifecycle.persist",),
) -> JsonObject:
    del requirement
    item: JsonObject = {
        "story_item_id": "US-0001",
        "story_title": f"Persist lifecycle boundary {ordinal}",
        "statement": (
            "As an operator, I want durable lifecycle facts, so that "
            "workflow routing survives restarts."
        ),
        "persona": "operator",
        "acceptance_criteria": ["Verify the persisted semantic boundary."],
        "spec_item_ids": list(spec_item_ids),
        "invest_assessment": {
            "independent": {
                "result": "pass",
                "rationale": "Delivers self-contained increment.",
                "evidence": "No unbuilt dependencies.",
            },
            "negotiable": {
                "result": "pass",
                "rationale": "Implementation details open to refinement.",
                "evidence": "Focuses on user outcome.",
            },
            "valuable": {
                "result": "pass",
                "rationale": "Directly delivers user capability.",
                "evidence": "Addresses requirement.",
            },
            "estimable": {
                "result": "pass",
                "rationale": "Scope is clear and bounded.",
                "evidence": "Discrete criteria.",
            },
            "small": {
                "result": "pass",
                "rationale": "Sized for single iteration.",
                "evidence": "Effort is M.",
            },
            "testable": {
                "result": "pass",
                "rationale": "Verifiable pass/fail criteria.",
                "evidence": "Observable verification steps.",
            },
        },
        "estimated_effort": "M",
        "effort_rationale": "Moderate persistence and lifecycle routing scope.",
        "order_rationale": f"Story {ordinal} follows accepted Backlog priority.",
        "produced_artifacts": ["workflow records"],
        "research_caveats": [],
        "dependency_candidates": [],
    }
    return {
        "story_items": [{"item": item, "item_fingerprint": canonical_hash(item)}],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _sprint_plan(
    selected_story_id: int,
    deferred_story_id: int,
    *,
    spec_item_ids: tuple[str, ...] = ("REQ.lifecycle.persist",),
) -> JsonObject:
    del deferred_story_id
    return {
        "sprint_goal": "Persist the first lifecycle increment.",
        "selected_stories": [
            {
                "story_id": selected_story_id,
                "story_item_id": "US-0001",
                "tasks": [
                    {
                        "description": "Implement persisted lifecycle boundary",
                        "relevant_spec_item_ids": list(spec_item_ids),
                        "task_kind": "implementation",
                        "artifact_targets": ["workflow lifecycle"],
                        "workstream_tags": ["workflow"],
                        "checklist_items": ["Run focused tests"],
                    }
                ],
                "reason_for_selection": "Deliver the highest priority increment.",
            }
        ],
    }


def _new_journey(engine: Engine) -> _Journey:
    with Session(engine) as session:
        project = Project(name="Persisted v2 lifecycle journey")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    clock = _JourneyClock(JOURNEY_AT)
    return _Journey(
        engine=engine,
        domain=WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=clock,
            adk_recipe_registry=_ProviderFreeRegistry(),
            specification_source_check=lambda _project_id, _input: None,
            specification_registration_check=lambda _prepared: None,
        ),
        clock=clock,
        project_id=project_id,
    )


def _vision_evidence(project_id: int) -> JsonObject:
    item = {
        "evidence_id": "project:metadata",
        "kind": "project_metadata",
        "relative_path": None,
        "content_fingerprint": canonical_hash({"project_id": project_id}),
        "trust": "operator_provided",
        "content": {"project_id": project_id},
        "truncated": False,
    }
    payload = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [item],
        "warnings": [],
    }
    return _JSON_OBJECT.validate_python(
        {**payload, "evidence_fingerprint": canonical_hash(payload)}
    )


def _component_basis(components: JsonObject) -> tuple[JsonObject, ...]:
    """Attribute every complete Journey component to its bounded evidence."""
    return tuple(
        {
            "component": component,
            "source_kinds": ["evidence"],
            "evidence_ids": ["project:metadata"],
            "assumption_ids": [],
        }
        for component in components
    )


def _accept_initial_vision(journey: _Journey) -> None:
    domain = journey.domain
    project_id = journey.project_id
    evidence = _vision_evidence(project_id)
    evidence_fingerprint = str(evidence["evidence_fingerprint"])
    components: JsonObject = {
        "project_name": "Persisted lifecycle",
        "target_user": "Operators",
        "problem": "Workflow state can drift",
        "product_category": "Delivery tool",
        "key_benefit": "Durable semantic routing",
        "competitors": "Manual checklists",
        "differentiator": "Typed persisted facts",
    }
    position, bootstrap = _assert_next(
        domain, project_id, "vision.bootstrap", NodeCategory.AVAILABLE
    )
    started = domain.transition(
        StartNodeAttempt(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=bootstrap.decision_fingerprint,
            idempotency_key="journey-vision-start",
            actor="operator@example.com",
            target_node_id="vision.bootstrap",
            target_instance_key=None,
            normalized_input={
                "operation": "bootstrap",
                "evidence_fingerprint": evidence_fingerprint,
            },
            model_id="fake/vision",
            execution_settings={"timeout_seconds": 1.0, "max_attempts": 1},
            lease_seconds=60,
        )
    )
    assert started.ok is True
    recorded = domain.transition(
        GenerateVisionBootstrap(
            **_guards(position, "vision.bootstrap"),
            idempotency_key="journey-vision-record",
            operation="bootstrap",
            evidence=evidence,
            evidence_fingerprint=evidence_fingerprint,
            evidence_warnings=(),
            repository_binding_id=None,
            updated_components=components,
            project_vision_statement="A durable product delivery lifecycle.",
            is_complete=True,
            clarifying_questions=(),
            component_basis=_component_basis(components),
            assumptions=(),
            conflicts=(),
            attempt_id=_output_int(started, "attempt_id"),
            attempt_fingerprint=str(started.output["attempt_fingerprint"]),
        )
    )
    assert recorded.ok is True
    position, review = _assert_next(
        domain, project_id, "vision.review", NodeCategory.WAITING
    )
    vision_id, vision_fingerprint = _reference(review, "vision")
    accepted = domain.transition(
        DecideVisionReview(
            **_guards(position, "vision.review"),
            idempotency_key="journey-vision-accept",
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            decision="accepted",
            rationale="The Vision defines a durable product direction.",
        )
    )
    assert accepted.ok is True


def _accept_initial_goal(journey: _Journey) -> None:
    domain = journey.domain
    project_id = journey.project_id
    position, interview = _assert_next(
        domain, project_id, "goal.interview", NodeCategory.AVAILABLE
    )
    started = domain.transition(
        StartNodeAttempt(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=interview.decision_fingerprint,
            idempotency_key="journey-goal-start",
            actor="operator@example.com",
            target_node_id="goal.interview",
            target_instance_key=None,
            normalized_input={"user_response": "Define the first Product Goal."},
            model_id="fake/goal",
            execution_settings={"timeout_seconds": 1.0, "max_attempts": 1},
            lease_seconds=60,
        )
    )
    assert started.ok is True
    recorded = domain.transition(
        RecordProductGoalInterviewTurn(
            **_guards(position, "goal.interview"),
            idempotency_key="journey-goal-record",
            user_text="Define the first Product Goal.",
            updated_components={
                "valuable_future_state": "One lifecycle increment is durable",
                "beneficiary": "Operators",
                "value": "Reliable delivery state",
                "success_signals": ["A Sprint reaches persisted triage"],
                "boundaries": ["No provider calls"],
            },
            product_goal_statement="Persist one complete delivery increment.",
            is_complete=True,
            clarifying_questions=(),
            attempt_id=_output_int(started, "attempt_id"),
            attempt_fingerprint=str(started.output["attempt_fingerprint"]),
        )
    )
    assert recorded.ok is True
    position, review = _assert_next(
        domain, project_id, "goal.review", NodeCategory.WAITING
    )
    goal_id, goal_fingerprint = _reference(review, "product_goal")
    accepted = domain.transition(
        DecideProductGoalReview(
            **_guards(position, "goal.review"),
            idempotency_key="journey-goal-accept",
            product_goal_artifact_id=goal_id,
            product_goal_fingerprint=goal_fingerprint,
            decision="accepted",
            rationale="The Goal is measurable and bounded.",
        )
    )
    assert accepted.ok is True


def _accept_specification(
    journey: _Journey,
    tmp_path: Path,
    *,
    payload: SpecificationPayload | None = None,
    include_context: bool = False,
) -> tuple[int, str]:
    domain = journey.domain
    project_id = journey.project_id
    probe = GitPythonRepositoryProbe()
    repository = _repository(tmp_path, name="journey-specification-source")
    if include_context:
        (repository / "CONTEXT.md").write_text(
            "# Exact registered context\n\nRetain source provenance.\n",
            encoding="utf-8",
        )
        with Repo(repository) as source_repository:
            source_repository.index.add(["CONTEXT.md"])
            source_repository.index.commit("register source context")
    _record_binding(
        journey.engine,
        project_id=project_id,
        repository=repository,
        probe=probe,
        inspected_at=journey.clock.now_value - timedelta(seconds=1),
    )
    position, _ = _assert_next(
        domain,
        project_id,
        "specification.source.register",
        NodeCategory.AVAILABLE,
    )
    registered = _register_source(
        journey.engine,
        domain,
        project_id=project_id,
        repository_probe=probe,
        key="journey-specification-source",
    )
    assert registered.ok is True
    position, structurer = _assert_next(
        domain, project_id, "specification.structure", NodeCategory.AVAILABLE
    )
    started = domain.transition(
        StartNodeAttempt(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=structurer.decision_fingerprint,
            idempotency_key="journey-specification-start",
            actor="operator@example.com",
            target_node_id="specification.structure",
            target_instance_key=structurer.instance_key,
            normalized_input=SpecificationStructuringInputService(
                engine=journey.engine,
                repository_probe=probe,
            ).build(
                project_id=project_id,
                decision=structurer,
            ),
            model_id="fake/specification-structurer",
            execution_settings={"temperature": 0},
            lease_seconds=60,
        )
    )
    assert started.ok is True
    structured = domain.transition(
        CompleteSpecificationStructuring(
            **_guards(position, "specification.structure"),
            idempotency_key="journey-specification-complete",
            attempt_id=_output_int(started, "attempt_id"),
            attempt_fingerprint=str(started.output["attempt_fingerprint"]),
            payload=payload or _specification_payload(),
        )
    )
    assert structured.ok is True
    specification_hash = str(structured.output["payload_fingerprint"])
    position, review = _assert_next(
        domain, project_id, "specification.review", NodeCategory.WAITING
    )
    candidate_id, candidate_fingerprint = _reference(review, "specification_candidate")
    accepted = domain.transition(
        DecideSpecification(
            **_guards(position, "specification.review"),
            idempotency_key="journey-specification-accept",
            specification_candidate_id=candidate_id,
            candidate_fingerprint=candidate_fingerprint,
            decision="accepted",
            rationale="The specification covers the Goal increment.",
        )
    )
    assert accepted.ok is True
    position, backlog_decision = _assert_next(
        domain, project_id, "backlog.generate", NodeCategory.AVAILABLE
    )
    spec_version_id, registered_hash = _reference(backlog_decision, "specification")
    assert registered_hash == specification_hash
    return spec_version_id, specification_hash


def _accept_backlog(
    journey: _Journey,
    requirements: tuple[str, ...],
    *,
    spec_item_ids: tuple[str, ...] = ("REQ.lifecycle.persist",),
) -> None:
    domain = journey.domain
    project_id = journey.project_id
    content = _backlog_content(requirements, spec_item_ids=spec_item_ids)
    position, generate = _assert_next(
        domain, project_id, "backlog.generate", NodeCategory.AVAILABLE
    )
    goal_id, goal_fingerprint = _reference(generate, "product_goal")
    spec_version_id, spec_hash = _reference(generate, "specification")
    recorded = domain.transition(
        RecordBacklogDraft(
            **_guards(position, "backlog.generate"),
            idempotency_key="journey-backlog",
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
            product_goal_artifact_id=goal_id,
            product_goal_fingerprint=goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    position, review = _assert_next(
        domain, project_id, "backlog.review", NodeCategory.WAITING
    )
    backlog_id, backlog_fingerprint = _reference(review, "backlog")
    accepted = domain.transition(
        DecideBacklog(
            **_guards(position, "backlog.review"),
            idempotency_key="journey-backlog-accept",
            backlog_artifact_id=backlog_id,
            artifact_fingerprint=backlog_fingerprint,
            decision="accepted",
            rationale="The Backlog preserves Goal and Specification lineage.",
        )
    )
    assert accepted.ok is True


def _accept_roadmap(journey: _Journey, requirements: tuple[str, ...]) -> None:
    domain = journey.domain
    project_id = journey.project_id
    content = _roadmap_content(requirements)
    position, generate = _assert_next(
        domain,
        project_id,
        "planning.roadmap.generate",
        NodeCategory.AVAILABLE,
    )
    backlog_id, backlog_fingerprint = _reference(generate, "backlog")
    recorded = domain.transition(
        RecordRoadmapDraft(
            **_guards(position, "planning.roadmap.generate"),
            idempotency_key="journey-roadmap",
            backlog_artifact_id=backlog_id,
            backlog_artifact_fingerprint=backlog_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    position, review = _assert_next(
        domain,
        project_id,
        "planning.roadmap.review",
        NodeCategory.WAITING,
    )
    roadmap_id, roadmap_fingerprint = _reference(review, "roadmap")
    accepted = domain.transition(
        DecideRoadmap(
            **_guards(position, "planning.roadmap.review"),
            idempotency_key="journey-roadmap-accept",
            roadmap_artifact_id=roadmap_id,
            artifact_fingerprint=roadmap_fingerprint,
            decision="accepted",
            rationale="The Roadmap sequences both accepted requirements.",
        )
    )
    assert accepted.ok is True


def _accept_stories(
    journey: _Journey,
    requirements: tuple[str, ...],
    *,
    spec_item_ids: tuple[str, ...] = ("REQ.lifecycle.persist",),
) -> tuple[int, ...]:
    story_ids: list[int] = []
    for ordinal, requirement in enumerate(requirements, start=1):
        backlog_item_id = f"PBI-{ordinal:06d}"
        instance_key = f"backlog_item:{backlog_item_id}"
        position, generate = _assert_next(
            journey.domain,
            journey.project_id,
            "planning.story.generate",
            NodeCategory.AVAILABLE,
            instance_key,
        )
        roadmap_id, roadmap_fingerprint = _reference(generate, "roadmap")
        backlog_id, backlog_fingerprint = _reference(generate, "backlog")
        content = _story_content(
            requirement,
            ordinal,
            spec_item_ids=spec_item_ids,
        )
        recorded = journey.domain.transition(
            RecordStoryDraft(
                **_guards(position, "planning.story.generate", instance_key),
                idempotency_key=f"journey-story-{ordinal}",
                backlog_item_id=backlog_item_id,
                source_backlog_artifact_id=backlog_id,
                source_backlog_artifact_fingerprint=backlog_fingerprint,
                roadmap_artifact_id=roadmap_id,
                roadmap_artifact_fingerprint=roadmap_fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
            )
        )
        assert recorded.ok is True
        position, review = _assert_next(
            journey.domain,
            journey.project_id,
            "planning.story.review",
            NodeCategory.WAITING,
            instance_key,
        )
        artifact_id, fingerprint = _reference(review, "story")
        accepted = journey.domain.transition(
            DecideStory(
                **_guards(position, "planning.story.review", instance_key),
                idempotency_key=f"journey-story-{ordinal}-accept",
                backlog_item_id=backlog_item_id,
                story_artifact_id=artifact_id,
                artifact_fingerprint=fingerprint,
                decision="accepted",
                rationale="The Story set covers its Roadmap requirement.",
            )
        )
        assert accepted.ok is True
        story_id = _first_output_int(accepted, "activated_story_ids")
        with patch.object(
            story_validation_service_module,
            "get_engine",
            return_value=journey.engine,
        ):
            validation = (
                story_validation_service_module.validate_story_with_specification(
                    {"story_id": story_id}
                )
            )
        assert validation["ready_for_sprint"] is True
        story_ids.append(story_id)
    return tuple(story_ids)


def _review_dependencies_and_start_sprint(
    journey: _Journey,
    story_ids: tuple[int, ...],
    *,
    spec_item_ids: tuple[str, ...] = ("REQ.lifecycle.persist",),
) -> int:
    _select_for_sprint(journey.engine, story_ids[0])
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "planning.story_dependencies",
        NodeCategory.AVAILABLE,
    )
    with Session(journey.engine) as session:
        snapshot = WorkflowFactRepository(session).load(journey.project_id)
    selected_scope = tuple(
        item
        for item in snapshot.stories
        if item.structurally_eligible and item.sprint_selection_state == "selected"
    )
    assert tuple(item.story_id for item in selected_scope) == (story_ids[0],)
    dependencies = journey.domain.transition(
        ApplyStoryDependencies(
            **_guards(position, "planning.story_dependencies"),
            idempotency_key="journey-dependencies",
            selected_story_ids=(story_ids[0],),
            reviewed_edges=(),
            source_fingerprint=story_dependency_source_fingerprint(selected_scope),
        )
    )
    assert dependencies.ok is True
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "planning.sprint.plan",
        NodeCategory.AVAILABLE,
    )
    with Session(journey.engine) as session:
        snapshot = WorkflowFactRepository(session).load(journey.project_id)
    candidates = tuple(item for item in snapshot.stories if item.sprint_candidate)
    assert tuple(item.story_id for item in candidates) == (story_ids[0],)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    content = _sprint_plan(
        story_ids[0],
        story_ids[1],
        spec_item_ids=spec_item_ids,
    )
    recorded = journey.domain.transition(
        RecordSprintPlan(
            **_guards(position, "planning.sprint.plan"),
            idempotency_key="journey-sprint-plan",
            team_name="Lifecycle Team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(content),
        )
    )
    assert recorded.ok is True
    plan_id = _output_int(recorded, "sprint_plan_artifact_id")
    plan_fingerprint = str(recorded.output["plan_fingerprint"])
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "planning.sprint.review",
        NodeCategory.WAITING,
    )
    accepted = journey.domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="journey-sprint-plan-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="The plan fits one increment of capacity.",
        )
    )
    assert accepted.ok is True
    sprint_id = _output_int(accepted, "activated_sprint_id")
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "planning.sprint.start",
        NodeCategory.AVAILABLE,
    )
    started = journey.domain.transition(
        StartSprint(
            **_guards(position, "planning.sprint.start"),
            idempotency_key="journey-sprint-start",
        )
    )
    assert started.ok is True
    return sprint_id


def _complete_sprint_and_triage(
    journey: _Journey,
    sprint_id: int,
    story_id: int,
) -> None:
    with Session(journey.engine) as session:
        task = session.exec(select(Task).where(Task.story_id == story_id)).one()
        assert task.task_id is not None
        task_id = task.task_id
    task_instance = f"task:{task_id}"
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "execution.task.complete",
        NodeCategory.AVAILABLE,
        task_instance,
    )
    completed = journey.domain.transition(
        CompleteTask(
            **_instance_guards(
                position,
                "execution.task.complete",
                task_instance,
            ),
            idempotency_key="journey-task-complete",
            task_id=task_id,
            outcome_summary="Persisted the lifecycle boundary.",
            artifact_refs=("workflow lifecycle",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    )
    assert completed.ok is True
    story_instance = f"story:{story_id}"
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "execution.story.close",
        NodeCategory.AVAILABLE,
        story_instance,
    )
    closed_story = journey.domain.transition(
        CloseStory(
            **_instance_guards(
                position,
                "execution.story.close",
                story_instance,
            ),
            idempotency_key="journey-story-close",
            story_id=story_id,
            resolution="Completed",
            delivered="One persisted lifecycle increment.",
            evidence="The provider-free journey reached execution.",
            known_gaps="The second accepted Story remains for another Sprint.",
        )
    )
    assert closed_story.ok is True
    position, review = _assert_next(
        journey.domain,
        journey.project_id,
        "execution.sprint.review",
        NodeCategory.WAITING,
    )
    review_fingerprint = next(
        item.fingerprint
        for item in review.fact_references
        if item.fact_type == "sprint_review"
    )
    reviewed = journey.domain.transition(
        ReviewSprint(
            **_guards(position, "execution.sprint.review"),
            idempotency_key="journey-sprint-review",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    )
    assert reviewed.ok is True
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "execution.sprint.close",
        NodeCategory.AVAILABLE,
    )
    journey.clock.advance(timedelta(minutes=1))
    closed = journey.domain.transition(
        CloseSprint(
            **_guards(position, "execution.sprint.close"),
            idempotency_key="journey-sprint-close",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    )
    assert closed.ok is True
    triage_instance = f"sprint:{sprint_id}"
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "execution.post_sprint_triage",
        NodeCategory.AVAILABLE,
        triage_instance,
    )
    assert "goal.fulfill" not in position.available_nodes
    assert "goal.abandon" not in position.available_nodes
    assert "goal.interview" not in position.available_nodes
    triage = journey.domain.transition(
        RecordPostSprintTriage(
            **_instance_guards(
                position, "execution.post_sprint_triage", triage_instance
            ),
            idempotency_key="journey-triage",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={
                "learning": "The Goal and Specification lineage remained stable."
            },
        )
    )
    assert triage.ok is True


def _assert_next_cycle_and_fulfill_goal(journey: _Journey) -> None:
    with Session(journey.engine) as session:
        snapshot = WorkflowFactRepository(session).load(journey.project_id)
    completed_sprint_ids = {
        sprint.sprint_id for sprint in snapshot.sprints if sprint.status == "completed"
    }
    next_story = next(
        item
        for item in snapshot.stories
        if not any(sprint_id in completed_sprint_ids for sprint_id in item.sprint_ids)
    )
    _select_for_sprint(journey.engine, next_story.story_id)
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "planning.story_dependencies",
        NodeCategory.AVAILABLE,
    )
    with Session(journey.engine) as session:
        snapshot = WorkflowFactRepository(session).load(journey.project_id)
    selected_scope = tuple(
        item
        for item in snapshot.stories
        if item.structurally_eligible
        and item.sprint_selection_state == "selected"
        and not any(sprint_id in completed_sprint_ids for sprint_id in item.sprint_ids)
    )
    reviewed = journey.domain.transition(
        ApplyStoryDependencies(
            **_guards(position, "planning.story_dependencies"),
            idempotency_key="journey-next-sprint-dependencies",
            selected_story_ids=tuple(item.story_id for item in selected_scope),
            reviewed_edges=(),
            source_fingerprint=story_dependency_source_fingerprint(selected_scope),
        )
    )
    assert reviewed.ok is True
    position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "planning.sprint.plan",
        NodeCategory.AVAILABLE,
    )
    assert "specification.source.register" in position.available_nodes
    assert "specification.structure" not in position.available_nodes
    assert "goal.fulfill" in position.available_nodes
    assert "goal.abandon" in position.available_nodes
    assert "vision.interview" not in position.available_nodes
    goal_id, goal_fingerprint = _reference(
        _decision(position, "goal.fulfill"), "product_goal"
    )
    fulfilled = journey.domain.transition(
        FulfillProductGoal(
            **_guards(position, "goal.fulfill"),
            idempotency_key="journey-goal-fulfilled",
            product_goal_artifact_id=goal_id,
            product_goal_fingerprint=goal_fingerprint,
            rationale="The first durable increment was delivered and triaged.",
        )
    )
    assert fulfilled.ok is True
    next_position, _ = _assert_next(
        journey.domain,
        journey.project_id,
        "goal.interview",
        NodeCategory.AVAILABLE,
    )
    assert "vision.interview" not in next_position.available_nodes


def test_root_graph_has_exact_v2_lifecycle_order() -> None:
    """Expose one product lifecycle in the approved order."""
    assert ROOT_GRAPH.graph_version == "agileforge.workflow.v2"
    assert tuple(child.child_graph_id for child in ROOT_GRAPH.root.children) == (
        "vision",
        "product_goal",
        "specification",
        "backlog",
        "planning",
        "execution",
    )


def test_provider_free_persisted_v2_journey_reaches_triage_and_next_goal(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Persist every semantic boundary from Vision through post-Sprint triage."""
    journey = _new_journey(engine)
    _accept_initial_vision(journey)
    _accept_initial_goal(journey)
    _spec_version_id, _specification_hash = _accept_specification(journey, tmp_path)
    requirements = (
        "Persist the primary lifecycle boundary",
        "Carry remaining work into another Sprint",
    )
    _accept_backlog(journey, requirements)
    _accept_roadmap(journey, requirements)
    story_ids = _accept_stories(journey, requirements)
    sprint_id = _review_dependencies_and_start_sprint(journey, story_ids)
    _complete_sprint_and_triage(journey, sprint_id, story_ids[0])
    _assert_next_cycle_and_fulfill_goal(journey)


def test_provider_free_persisted_gold_lifecycle_preserves_data_contract(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Persist the exact gold root through Backlog, Story, Sprint, and start."""
    gold_payload, canonical_gold = _gold_specification_payload()
    assert canonical_spec_json(gold_payload) == canonical_gold
    assert canonical_spec_hash(gold_payload) == _GOLD_SPECIFICATION_HASH
    journey = _new_journey(engine)
    _accept_initial_vision(journey)
    _accept_initial_goal(journey)
    spec_version_id, spec_hash = _accept_specification(
        journey,
        tmp_path,
        payload=gold_payload,
        include_context=True,
    )
    assert spec_hash == _GOLD_SPECIFICATION_HASH
    requirements = (
        "Define the supported Number List data contract.",
        "Expose the public calculator operation.",
    )
    _accept_backlog(
        journey,
        requirements,
        spec_item_ids=_GOLD_LIFECYCLE_SPEC_ITEM_IDS,
    )
    _accept_roadmap(journey, requirements)
    story_ids = _accept_stories(
        journey,
        requirements,
        spec_item_ids=_GOLD_LIFECYCLE_SPEC_ITEM_IDS,
    )
    sprint_id = _review_dependencies_and_start_sprint(
        journey,
        story_ids,
        spec_item_ids=_GOLD_LIFECYCLE_SPEC_ITEM_IDS,
    )

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(journey.project_id)
        accepted = accepted_current_spec(snapshot)
        assert accepted is not None
        assert (accepted.spec_version_id, accepted.spec_hash) == (
            spec_version_id,
            _GOLD_SPECIFICATION_HASH,
        )
        assert all(
            item.spec_item_ids == _GOLD_LIFECYCLE_SPEC_ITEM_IDS
            for item in snapshot.backlog_items
        )
        active_stories = tuple(
            item for item in snapshot.stories if item.story_id in story_ids
        )
        assert len(active_stories) == len(story_ids)
        assert all(
            item.spec_item_ids == _GOLD_LIFECYCLE_SPEC_ITEM_IDS
            and item.accepted_spec_version_id == spec_version_id
            and item.accepted_spec_hash == _GOLD_SPECIFICATION_HASH
            for item in active_stories
        )
        task = session.exec(select(Task).where(Task.story_id == story_ids[0])).one()
        metadata = parse_task_metadata(task.metadata_json)
        assert metadata.relevant_spec_item_ids == _GOLD_LIFECYCLE_SPEC_ITEM_IDS
        assert (metadata.spec_version_id, metadata.spec_hash) == (
            spec_version_id,
            _GOLD_SPECIFICATION_HASH,
        )
        assert snapshot.sprints[0].sprint_id == sprint_id
        assert all(
            "authority" not in item.artifact_type for item in snapshot.phase_artifacts
        )
        assert all(
            "authority" not in item.artifact_type
            for item in snapshot.planning_artifacts
        )
