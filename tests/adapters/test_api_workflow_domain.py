"""API adapter tests for exact typed workflow requests."""

from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session

import api as api_module
from api import (
    AuthorityDecisionApiRequest,
    CreateProjectRequest,
    build_authority_decision_request,
    build_create_project_command,
)
from cli.workflow_commands import COMMAND_PREFIXES
from models.workflow import WorkflowTransitionReceipt
from repositories.workflow import WorkflowFactRepository
from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    AuthorityCompileRequest,
    AuthorityRepairInputService,
    AuthorityRepairRequest,
    AuthorityReviewRequest,
    AuthorityReviewSelectionService,
    CreateProjectCommand,
    DeliveryActionInputService,
    DeliveryActionRequest,
    ProductGoalLifecycleServices,
    ProductGoalResponseRequest,
    SprintPlanningInputService,
    SprintPlanningRequest,
    VisionResponseRequest,
    WorkflowDomainPort,
)
from services.contracts.backlog import InputSchema as BacklogInput
from services.contracts.roadmap import RoadmapBuilderInput
from services.contracts.sprint import SprintPlannerInput
from services.contracts.story import UserStoryWriterInput
from services.node_attempt_replay import NodeAttemptReplayQuery, TransitionReplayQuery
from tests.adapters.test_command_renderer import position_fixture
from tests.workflow.test_execution_transitions import (
    _complete_execution_sprint_with_unselected_story,
)
from tests.workflow.test_planning_transitions import (
    _apply_current_dependencies,
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _seed_accepted_backlog,
)
from tests.workflow.test_planning_transitions import (
    _domain as planning_domain,
)
from workflow.contracts import (
    FactReference,
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.planning import candidate_set_fingerprint
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import DecideAuthority, StartNodeAttempt, TransitionRequest

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from adapters.adk.recipes import AdkRecipeRegistry

PROJECT_ID = 41


def test_create_project_request_accepts_only_semantic_fields() -> None:
    """Keep project creation free of origin and caller-derived state."""
    request = CreateProjectRequest(
        name="MyFinance",
        description="Local household finance",
        repository_path="/Users/aaat/myfinance",
        idempotency_key="create-myfinance-1",
        actor="acceptance-agent",
    )

    assert request.model_dump() == {
        "name": "MyFinance",
        "description": "Local household finance",
        "repository_path": "/Users/aaat/myfinance",
        "idempotency_key": "create-myfinance-1",
        "actor": "acceptance-agent",
    }
    with pytest.raises(ValidationError):
        CreateProjectRequest.model_validate(
            {
                **request.model_dump(),
                "graph_version": "agileforge.workflow.v2",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "greenfield"),
        ("changed_by", "legacy-actor"),
        ("fact_fingerprint", "facts"),
        ("repository_metadata", {"head_sha": "caller-owned"}),
        ("compiler_input", {"spec": "caller-owned"}),
        ("model_id", "caller/model"),
    ],
)
def test_create_project_api_rejects_unknown_or_internal_fields(
    field: str,
    value: object,
) -> None:
    """Reject hidden guards, retired inputs, and caller-derived evidence."""
    payload: dict[str, object] = {
        "name": "MyFinance",
        "description": "Local household finance",
        "repository_path": None,
        "idempotency_key": "create-myfinance-1",
        "actor": "acceptance-agent",
        field: value,
    }

    response = TestClient(api_module.app).post("/api/projects", json=payload)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "payload", "internal_field"),
    [
        (
            "/api/projects/41/vision/respond",
            {
                "text": "A durable product direction.",
                "idempotency_key": "vision-41",
                "actor": "operator",
            },
            "decision_fingerprint",
        ),
        (
            "/api/projects/41/repository",
            {
                "path": "/Users/aaat/project",
                "idempotency_key": "repository-41",
                "actor": "operator",
            },
            "repository_metadata",
        ),
        (
            "/api/projects/41/repository",
            {
                "path": "/Users/aaat/project",
                "idempotency_key": "repository-41",
                "actor": "operator",
            },
            "binding_fingerprint",
        ),
        (
            "/api/projects/41/authority/compile",
            {
                "idempotency_key": "compile-41",
                "actor": "operator",
            },
            "compiler_input",
        ),
        (
            "/api/projects/41/authority/compile",
            {
                "idempotency_key": "compile-41",
                "actor": "operator",
            },
            "model_id",
        ),
        (
            "/api/projects/41/vision/review",
            {
                "decision": "accepted",
                "rationale": "Direction is correct.",
                "idempotency_key": "vision-review-41",
                "actor": "operator",
            },
            "artifact_fingerprint",
        ),
    ],
)
def test_semantic_api_models_reject_internal_fields(
    path: str,
    payload: dict[str, object],
    internal_field: str,
) -> None:
    """Reject every caller-owned guard, repository fact, or compiler payload."""
    response = TestClient(api_module.app).post(
        path,
        json={**payload, internal_field: {"caller": "owned"}},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "decision"),
    [
        ("/api/projects/41/vision/review", "accepted"),
        ("/api/projects/41/goals/review", "accepted"),
        ("/api/projects/41/specifications/review", "accepted"),
        ("/api/projects/41/authority/decision", "accepted"),
        ("/api/projects/41/goals/complete", None),
        ("/api/projects/41/goals/abandon", None),
    ],
)
@pytest.mark.parametrize("rationale", ["", "   \t"])
def test_semantic_decision_api_rejects_blank_rationale(
    path: str,
    decision: str | None,
    rationale: str,
) -> None:
    """Reject empty semantic decision reasons at the HTTP validation boundary."""
    payload = {
        "rationale": rationale,
        "idempotency_key": "decision-41",
        "actor": "operator",
    }
    if decision is not None:
        payload["decision"] = decision

    response = TestClient(api_module.app).post(path, json=payload)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class _FakeApiApplication:
    def __init__(
        self,
        *,
        position: WorkflowPosition | None = None,
        transition_result: TransitionResult | None = None,
    ) -> None:
        self.position_calls: list[int] = []
        self.requests: list[object] = []
        self._position = position or position_fixture()
        self._transition_result = transition_result

    def position(self, *, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return self._position

    def transition(self, request: TransitionRequest) -> TransitionResult:
        self.requests.append(request)
        return self._transition_result or TransitionResult(
            ok=True,
            position=self._position,
        )

    def run_agentic_action(
        self,
        request: AgenticActionRequest,
    ) -> TransitionResult:
        self.requests.append(request)
        return self._transition_result or TransitionResult(
            ok=True,
            position=self._position,
        )

    def generate_backlog(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def generate_roadmap(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def generate_story(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def generate_sprint(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def _record_delivery_request(self, request: object) -> TransitionResult:
        self.requests.append(request)
        return self._transition_result or TransitionResult(
            ok=True,
            position=self._position,
        )

    def decide_authority(self, request: AuthorityReviewRequest) -> TransitionResult:
        self.requests.append(request)
        return self._transition_result or TransitionResult(
            ok=True,
            position=self._position,
        )


class _BoundaryDomain:
    def __init__(self, position: WorkflowPosition) -> None:
        self._position = position
        self.position_calls: list[int] = []

    def position(self, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return self._position

    def transition(self, request: TransitionRequest) -> TransitionResult:
        pytest.fail(f"unexpected transition: {request}")

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        pytest.fail(
            "unexpected persisted input load: "
            f"{project_id}:{attempt_id}:{attempt_fingerprint}"
        )


class _VisionInput:
    def __init__(self, replay: TransitionResult | None = None) -> None:
        self.replay_result = replay
        self.replay_queries: list[NodeAttemptReplayQuery] = []

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        self.replay_queries.append(query)
        return self.replay_result

    def replay_transition(self, query: TransitionReplayQuery) -> None:
        del query

    def build(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject:
        return {
            "project_id": project_id,
            "decision": decision.decision_fingerprint,
            "user_response": user_text,
        }


class _ProductGoalInput:
    def __init__(self, replay: TransitionResult) -> None:
        self.replay_result = replay
        self.replay_queries: list[NodeAttemptReplayQuery] = []

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult:
        self.replay_queries.append(query)
        return self.replay_result

    def replay_transition(self, query: TransitionReplayQuery) -> None:
        del query

    def build(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject:
        del project_id, decision, user_text
        pytest.fail("replayed Goal response rebuilt current input")


class _DiscoverySelection:
    def resolve_specification_supersedes(self, project_id: int) -> None:
        del project_id


class _AuthorityInput:
    def __init__(self, replay: TransitionResult) -> None:
        self.replay_result = replay
        self.replay_queries: list[NodeAttemptReplayQuery] = []

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult:
        self.replay_queries.append(query)
        return self.replay_result

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        compiler_model: str,
    ) -> JsonObject:
        del project_id, decision, compiler_model
        pytest.fail("replayed Authority compile rebuilt current input")


class _DeliveryInput:
    def __init__(
        self,
        payload: JsonObject | None,
        replay: TransitionResult | None = None,
    ) -> None:
        self.payload = payload
        self.replay_result = replay
        self.replay_queries: list[NodeAttemptReplayQuery] = []
        self.build_calls: list[tuple[int, str, str]] = []

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        self.replay_queries.append(query)
        return self.replay_result

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        node_id: str,
    ) -> JsonObject | None:
        self.build_calls.append((project_id, decision.decision_fingerprint, node_id))
        return self.payload


class _CapturingApplication(AgileForgeApplication):
    def __init__(self, domain: _BoundaryDomain) -> None:
        super().__init__(workflow_domain=domain, vision_interview_input=_VisionInput())
        self.agent_requests: list[AgenticActionRequest] = []

    def run_agentic_action(self, request: AgenticActionRequest) -> TransitionResult:
        self.agent_requests.append(request)
        return TransitionResult(ok=True)


class _CapturingDeliveryApplication(AgileForgeApplication):
    def __init__(
        self,
        domain: _BoundaryDomain,
        delivery_input: _DeliveryInput,
    ) -> None:
        super().__init__(
            workflow_domain=domain,
            delivery_action_input=delivery_input,
        )
        self.agent_requests: list[AgenticActionRequest] = []

    def run_agentic_action(self, request: AgenticActionRequest) -> TransitionResult:
        self.agent_requests.append(request)
        return TransitionResult(ok=True)


class _CapturingSprintApplication(AgileForgeApplication):
    def __init__(
        self,
        domain: WorkflowDomainPort,
        sprint_input: SprintPlanningInputService,
    ) -> None:
        super().__init__(
            workflow_domain=domain,
            sprint_planning_input=sprint_input,
        )
        self.agent_requests: list[AgenticActionRequest] = []

    def run_agentic_action(self, request: AgenticActionRequest) -> TransitionResult:
        self.agent_requests.append(request)
        return TransitionResult(ok=True)


def _vision_position(*decisions: NodeDecision) -> WorkflowPosition:
    return WorkflowPosition(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-vision",
        evaluated_at=datetime(2026, 8, 9, tzinfo=UTC),
        available_nodes=tuple(item.node_id for item in decisions),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=decisions,
    )


def _vision_decision(fingerprint: str) -> NodeDecision:
    return NodeDecision(
        node_id="vision.interview",
        child_graph_id="vision",
        request_kind="record_vision_interview_turn",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_INTERVIEW_REQUIRED",
        decision_fingerprint=fingerprint,
    )


def _delivery_decision(
    *,
    node_id: str,
    request_kind: str,
    instance_key: str | None = None,
) -> NodeDecision:
    return NodeDecision(
        node_id=node_id,
        instance_key=instance_key,
        child_graph_id="delivery",
        request_kind=request_kind,
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="DELIVERY_REQUIRED",
        decision_fingerprint=f"decision-{node_id}",
    )


def test_semantic_application_resolves_vision_guards_once() -> None:
    """Prepare one exact guarded attempt from one current position read."""
    decision = _vision_decision("decision-vision")
    domain = _BoundaryDomain(_vision_position(decision))
    application = _CapturingApplication(domain)

    result = application.respond_to_vision(
        VisionResponseRequest(
            project_id=PROJECT_ID,
            text="A durable product direction.",
            idempotency_key="vision-41",
            actor="operator",
        )
    )

    assert result.ok is True
    assert domain.position_calls == [PROJECT_ID]
    assert len(application.agent_requests) == 1
    guarded = application.agent_requests[0]
    assert guarded.graph_version == "agileforge.workflow.v2"
    assert guarded.fact_fingerprint == "facts-vision"
    assert guarded.decision_fingerprint == "decision-vision"


def test_semantic_application_rejects_ambiguous_vision_decisions() -> None:
    """Return a structured conflict instead of choosing between decisions."""
    domain = _BoundaryDomain(
        _vision_position(_vision_decision("decision-a"), _vision_decision("decision-b"))
    )
    application = _CapturingApplication(domain)

    result = application.respond_to_vision(
        VisionResponseRequest(
            project_id=PROJECT_ID,
            text="A durable product direction.",
            idempotency_key="vision-41",
            actor="operator",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE
    assert domain.position_calls == [PROJECT_ID]
    assert application.agent_requests == []


def test_semantic_vision_response_replays_before_advanced_position() -> None:
    """Recover a Vision receipt before rejecting its now-advanced position."""
    replayed = TransitionResult(
        ok=True, replayed=True, applied_node_id="vision.interview"
    )
    domain = _BoundaryDomain(_vision_position())
    vision_input = _VisionInput(replay=replayed)
    application = AgileForgeApplication(
        workflow_domain=domain,
        vision_interview_input=vision_input,
    )

    result = application.respond_to_vision(
        VisionResponseRequest(
            project_id=PROJECT_ID,
            text="  A durable product direction.  ",
            idempotency_key="vision-41",
            actor="operator",
        )
    )

    assert result == replayed
    assert domain.position_calls == []
    assert vision_input.replay_queries[0].user_text == "A durable product direction."
    assert vision_input.replay_queries[0].graph_version is None


def test_semantic_product_goal_response_replays_before_advanced_position() -> None:
    """Recover a Goal receipt before rejecting its now-advanced position."""
    replayed = TransitionResult(
        ok=True, replayed=True, applied_node_id="goal.interview"
    )
    domain = _BoundaryDomain(_vision_position())
    goal_input = _ProductGoalInput(replayed)
    application = AgileForgeApplication(
        workflow_domain=domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=goal_input,
            discovery_selection=_DiscoverySelection(),
        ),
    )

    result = application.respond_to_product_goal(
        ProductGoalResponseRequest(
            project_id=PROJECT_ID,
            text="  A measurable future state.  ",
            idempotency_key="goal-41",
            actor="operator",
        )
    )

    assert result == replayed
    assert domain.position_calls == []
    assert goal_input.replay_queries[0].user_text == "A measurable future state."
    assert goal_input.replay_queries[0].fact_fingerprint is None


def test_semantic_authority_compile_replays_before_advanced_position() -> None:
    """Recover a compile receipt before rejecting its now-advanced position."""
    replayed = TransitionResult(
        ok=True,
        replayed=True,
        applied_node_id="authority.compile",
    )
    domain = _BoundaryDomain(_vision_position())
    authority_input = _AuthorityInput(replayed)
    application = AgileForgeApplication(
        workflow_domain=domain,
        authority_compilation_input=authority_input,
    )

    result = application.compile_authority(
        AuthorityCompileRequest(
            project_id=PROJECT_ID,
            idempotency_key="compile-41",
            actor="operator",
        )
    )

    assert result == replayed
    assert domain.position_calls == []
    assert authority_input.replay_queries[0].decision_fingerprint is None


def test_semantic_authority_review_replays_or_conflicts_before_advanced_position(
    engine: "Engine",
) -> None:
    """Resolve exact Authority review idempotency before current graph position."""
    stored = DecideAuthority(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-authority-review",
        decision_fingerprint="decision-authority-review",
        instance_key="authority:17",
        idempotency_key="authority-review-41",
        actor="operator",
        pending_authority_id=17,
        authority_fingerprint="sha256:authority-17",
        review_fingerprint="sha256:review-17",
        decision="accepted",
        rationale="Authority is complete.",
    )
    persisted = TransitionResult(ok=True, applied_node_id="authority.review")
    _store_completed_receipt(engine, stored, persisted)
    domain = _BoundaryDomain(_vision_position())
    application = AgileForgeApplication(
        workflow_domain=domain,
        authority_review_selection=AuthorityReviewSelectionService(engine=engine),
    )

    replay = application.decide_authority(
        AuthorityReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Authority is complete.",
            idempotency_key="authority-review-41",
            actor="operator",
        )
    )
    conflict = application.decide_authority(
        AuthorityReviewRequest(
            project_id=PROJECT_ID,
            decision="rejected",
            rationale="Authority needs repair.",
            idempotency_key="authority-review-41",
            actor="operator",
        )
    )

    assert replay == persisted.model_copy(update={"replayed": True})
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert domain.position_calls == []


def test_semantic_authority_repair_replays_or_conflicts_before_advanced_position(
    engine: "Engine",
) -> None:
    """Resolve exact Authority repair idempotency before current graph position."""
    stored = StartNodeAttempt(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-authority-repair",
        decision_fingerprint="decision-authority-repair",
        idempotency_key="authority-repair-41",
        actor="operator",
        target_node_id="authority.repair",
        target_instance_key="authority:17",
        normalized_input={"source_authority_id": 17},
        model_id="fake/authority-repair",
        execution_settings={"timeout_seconds": 120, "max_attempts": 2},
        lease_seconds=300,
    )
    persisted = TransitionResult(ok=True, applied_node_id="authority.repair")
    _store_completed_receipt(engine, stored, persisted)
    domain = _BoundaryDomain(_vision_position())
    application = AgileForgeApplication(
        workflow_domain=domain,
        authority_repair_input=AuthorityRepairInputService(engine=engine),
    )

    replay = application.repair_authority(
        AuthorityRepairRequest(
            project_id=PROJECT_ID,
            idempotency_key="authority-repair-41",
            actor="operator",
        )
    )
    conflict = application.repair_authority(
        AuthorityRepairRequest(
            project_id=PROJECT_ID,
            idempotency_key="authority-repair-41",
            actor="different-operator",
        )
    )

    assert replay == persisted.model_copy(update={"replayed": True})
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert domain.position_calls == []


def _store_completed_receipt(
    engine: "Engine",
    request: TransitionRequest,
    result: TransitionResult,
) -> None:
    request_payload = request.model_dump(mode="json")
    with Session(engine) as session:
        session.add(
            WorkflowTransitionReceipt(
                request_kind=request.kind,
                idempotency_key=request.idempotency_key,
                request_fingerprint=canonical_hash(request_payload),
                request_json=canonical_json(request_payload),
                result_json=canonical_json(result.model_dump(mode="json")),
                started_at=datetime(2026, 8, 9, tzinfo=UTC),
                completed_at=datetime(2026, 8, 9, tzinfo=UTC),
            )
        )
        session.commit()


@pytest.mark.parametrize(
    ("method_name", "node_id", "request_kind", "instance_key"),
    [
        ("generate_backlog", "backlog.generate", "record_backlog_draft", None),
        (
            "generate_roadmap",
            "planning.roadmap.generate",
            "record_roadmap_draft",
            None,
        ),
        (
            "generate_story",
            "planning.story.generate",
            "record_story_draft",
            "requirement:REQ-1",
        ),
    ],
)
def test_retained_delivery_actions_use_host_prepared_input(
    method_name: str,
    node_id: str,
    request_kind: str,
    instance_key: str | None,
) -> None:
    """Run retained delivery agents without caller-owned model input."""
    decision = _delivery_decision(
        node_id=node_id,
        request_kind=request_kind,
        instance_key=instance_key,
    )
    domain = _BoundaryDomain(_vision_position(decision))
    delivery_input = _DeliveryInput({"prepared_by": "host"})
    application = _CapturingDeliveryApplication(domain, delivery_input)
    request = DeliveryActionRequest(
        project_id=PROJECT_ID,
        instance_key=instance_key,
        idempotency_key=f"{node_id}-41",
        actor="operator",
    )

    result = getattr(application, method_name)(request)

    assert result.ok is True
    assert delivery_input.build_calls == [
        (PROJECT_ID, decision.decision_fingerprint, node_id)
    ]
    assert len(application.agent_requests) == 1
    assert application.agent_requests[0].input_payload == {"prepared_by": "host"}
    assert application.agent_requests[0].node_id == node_id


def test_story_replay_uses_the_caller_requested_requirement_selector() -> None:
    """Bind semantic Story replay to its exact requested requirement instance."""
    conflict = TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message="The idempotency key belongs to another requirement.",
        ),
    )
    domain = _BoundaryDomain(_vision_position())
    delivery_input = _DeliveryInput(None, replay=conflict)
    application = _CapturingDeliveryApplication(domain, delivery_input)

    result = application.generate_story(
        DeliveryActionRequest(
            project_id=PROJECT_ID,
            instance_key="requirement:REQ-B",
            idempotency_key="story-shared-key",
            actor="operator",
        )
    )

    assert result == conflict
    assert domain.position_calls == []
    assert delivery_input.replay_queries[0].instance_key == "requirement:REQ-B"
    assert application.agent_requests == []


def test_sprint_generation_fails_closed_without_host_capacity_input(
    engine: "Engine",
) -> None:
    """Require explicit or durable metrics capacity before model execution."""
    domain, project_id, _story_id = _sprint_ready_project(engine)
    application = _CapturingSprintApplication(
        domain,
        SprintPlanningInputService(engine=engine),
    )
    request = SprintPlanningRequest(
        project_id=project_id,
        team_name="Platform",
        idempotency_key="sprint-41",
        actor="operator",
    )

    result = application.generate_sprint(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.SPRINT_CAPACITY_REQUIRED
    assert application.agent_requests == []


def _sprint_ready_project(
    engine: "Engine",
) -> tuple[WorkflowDomainPort, int, int]:
    project_id = _seed_accepted_backlog(engine)
    domain = planning_domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _artifact_id, story_id = _record_and_accept_story(domain, project_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="review-sprint-dependencies",
    )
    return domain, project_id, story_id


def test_explicit_sprint_capacity_locks_exact_durable_cohort(
    engine: "Engine",
) -> None:
    """Persist host capacity and exact current candidates before model execution."""
    domain, project_id, story_id = _sprint_ready_project(engine)
    application = _CapturingSprintApplication(
        domain,
        SprintPlanningInputService(engine=engine),
    )
    capacity_points = 3

    result = application.generate_sprint(
        SprintPlanningRequest(
            project_id=project_id,
            guidance="Prioritize durable replay.",
            selected_story_ids=(story_id,),
            max_story_points=capacity_points,
            include_task_decomposition=False,
            team_name="Platform",
            idempotency_key="sprint-explicit",
            actor="operator",
        )
    )

    assert result.ok is True
    assert len(application.agent_requests) == 1
    envelope = application.agent_requests[0].input_payload
    planner_input = SprintPlannerInput.model_validate(envelope["planner_input"])
    assert [item.story_id for item in planner_input.available_stories] == [story_id]
    assert planner_input.capacity_points == capacity_points
    assert planner_input.capacity_source == "user_override"
    assert planner_input.user_context == "Prioritize durable replay."
    assert envelope["capacity_points"] == capacity_points
    assert envelope["capacity_source"] == "user_override"
    assert envelope["locked_story_ids"] == [story_id]
    assert envelope["requested_story_ids"] == [story_id]
    assert envelope["team_name"] == "Platform"
    assert envelope["include_task_decomposition"] is False
    assert envelope["guidance"] == "Prioritize durable replay."
    assert isinstance(envelope["candidate_set_fingerprint"], str)


def test_completed_sprint_metrics_supply_host_capacity(engine: "Engine") -> None:
    """Use completed Story points when current durable metrics recommend capacity."""
    _domain, project_id, _sprint_id, _story_id, future_story_id, *_rest = (
        _complete_execution_sprint_with_unselected_story(engine)
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    candidates = tuple(item for item in snapshot.stories if item.sprint_candidate)
    previous_plan = max(
        (
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
        ),
        key=lambda item: item.artifact_id,
    )
    decision = _delivery_decision(
        node_id="planning.sprint.plan",
        request_kind="record_sprint_plan",
    ).model_copy(
        update={
            "fact_references": (
                FactReference(
                    fact_type="sprint_plan",
                    fact_id=str(previous_plan.artifact_id),
                    fingerprint=previous_plan.artifact_fingerprint,
                ),
                FactReference(
                    fact_type="candidate_set",
                    fact_id=str(project_id),
                    fingerprint=candidate_set_fingerprint(
                        candidates,
                        snapshot.story_dependencies,
                    ),
                ),
            )
        }
    )

    envelope = SprintPlanningInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
        request=SprintPlanningRequest(
            project_id=project_id,
            selected_story_ids=(future_story_id,),
            team_name="Platform",
            idempotency_key="sprint-metrics-capacity",
            actor="operator",
        ),
    )

    assert isinstance(envelope, dict)
    planner_input = SprintPlannerInput.model_validate(envelope["planner_input"])
    assert planner_input.capacity_source == "project_metrics"
    assert planner_input.capacity_points > 0
    assert envelope["requested_max_story_points"] is None
    assert envelope["locked_story_ids"] == [future_story_id]
    assert "completed Sprints" in planner_input.capacity_basis


def test_invalid_manual_sprint_selection_fails_before_model(
    engine: "Engine",
) -> None:
    """Reject non-candidate manual Story IDs deterministically."""
    domain, project_id, _story_id = _sprint_ready_project(engine)
    application = _CapturingSprintApplication(
        domain,
        SprintPlanningInputService(engine=engine),
    )

    result = application.generate_sprint(
        SprintPlanningRequest(
            project_id=project_id,
            selected_story_ids=(999_999,),
            max_story_points=3,
            team_name="Platform",
            idempotency_key="sprint-invalid-selection",
            actor="operator",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert result.error.blockers[0].code == "SPRINT_SELECTION_INVALID"
    assert application.agent_requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_story_points": 0},
        {"selected_story_ids": (7, 7)},
        {"selected_story_ids": (-1,)},
    ],
)
def test_invalid_sprint_semantics_fail_validation(
    overrides: dict[str, object],
) -> None:
    """Reject invalid capacity and manual selection before durable reads."""
    with pytest.raises(ValidationError):
        SprintPlanningRequest.model_validate(
            {
                "project_id": PROJECT_ID,
                "max_story_points": 3,
                "team_name": "Platform",
                "idempotency_key": "sprint-invalid",
                "actor": "operator",
                **overrides,
            }
        )


def test_delivery_input_service_builds_from_durable_facts(engine: "Engine") -> None:
    """Prepare every callable delivery model contract from persisted lineage."""
    project_id = _seed_accepted_backlog(engine)
    domain = planning_domain(engine)
    service = DeliveryActionInputService(engine=engine)
    roadmap_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "planning.roadmap.generate"
    )
    backlog_decision = NodeDecision(
        node_id="backlog.generate",
        child_graph_id="backlog",
        request_kind="record_backlog_draft",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="BACKLOG_CORRECTION_AVAILABLE",
        decision_fingerprint="decision-backlog-correction",
        fact_references=tuple(
            reference
            for reference in roadmap_decision.fact_references
            if reference.fact_type in {"authority", "backlog", "product_goal"}
        ),
    )

    backlog_payload = service.build(
        project_id=project_id,
        decision=backlog_decision,
        node_id="backlog.generate",
    )
    roadmap_payload = service.build(
        project_id=project_id,
        decision=roadmap_decision,
        node_id="planning.roadmap.generate",
    )
    _record_and_accept_roadmap(domain, project_id)
    story_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "planning.story.generate"
    )
    story_payload = service.build(
        project_id=project_id,
        decision=story_decision,
        node_id="planning.story.generate",
    )

    assert BacklogInput.model_validate(backlog_payload).prior_backlog_state != (
        "NO_HISTORY"
    )
    assert RoadmapBuilderInput.model_validate(roadmap_payload).backlog_items
    assert UserStoryWriterInput.model_validate(story_payload).parent_requirement == (
        "Plan immutable work"
    )
    assert (
        service.build(
            project_id=project_id,
            decision=story_decision.model_copy(
                update={
                    "node_id": "planning.sprint.plan",
                    "fact_references": (
                        FactReference(
                            fact_type="candidate_set",
                            fact_id=str(project_id),
                            fingerprint="candidate-set",
                        ),
                    ),
                }
            ),
            node_id="planning.sprint.plan",
        )
        is None
    )


_AGENTIC_NODE_IDS = {
    "compile_authority": "authority.compile",
    "repair_authority": "authority.repair",
    "record_backlog_draft": "backlog.generate",
    "record_roadmap_draft": "planning.roadmap.generate",
    "record_story_draft": "planning.story.generate",
}


def _all_request_kinds_position() -> WorkflowPosition:
    decisions = tuple(
        NodeDecision(
            node_id=_AGENTIC_NODE_IDS.get(kind, f"test.{index}"),
            instance_key=f"instance:{index}",
            child_graph_id="test",
            request_kind=kind,
            category=NodeCategory.AVAILABLE,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="TEST_AVAILABLE",
            decision_fingerprint=f"decision-{index}",
        )
        for index, kind in enumerate(COMMAND_PREFIXES)
    )
    return WorkflowPosition(
        project_id=41,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-all",
        evaluated_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        available_nodes=tuple(item.node_id for item in decisions),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=decisions,
    )


def test_api_project_request_uses_semantic_business_input() -> None:
    """Translate exact API fields into the Project lifecycle command."""
    request = build_create_project_command(
        CreateProjectRequest(
            name="Example",
            description="Example project",
            repository_path="/Users/aaat/example",
            idempotency_key="open-api-41",
            actor="dashboard-user",
        )
    )

    assert request == CreateProjectCommand(
        name="Example",
        description="Example project",
        repository_path="/Users/aaat/example",
        idempotency_key="open-api-41",
        actor="dashboard-user",
    )


def test_api_authority_decision_accepts_semantic_choice_only() -> None:
    """Keep authority identity and review fingerprints host-owned."""
    payload = AuthorityDecisionApiRequest(
        idempotency_key="accept-api-41",
        actor="dashboard-user",
        correlation_id="corr-api-41",
        decision="accepted",
        rationale="Reviewed",
    )

    request = build_authority_decision_request(41, payload)

    assert isinstance(request, AuthorityReviewRequest)
    assert request.model_dump() == {
        "project_id": 41,
        "decision": "accepted",
        "rationale": "Reviewed",
        "idempotency_key": "accept-api-41",
        "actor": "dashboard-user",
        "correlation_id": "corr-api-41",
    }


def test_api_adapter_does_not_import_legacy_routing_authority() -> None:
    """Keep the production API free of old routing imports."""
    source = (Path(__file__).parents[2] / "api.py").read_text()
    assert "from services.workflow import WorkflowService" not in source
    assert "ReadOnlySessionReader" not in source


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/41/project/abandon",
        "/api/projects/41/brownfield/curate",
        "/api/projects/41/brownfield/baseline/record",
        "/api/projects/41/brownfield/inventory/record",
        "/api/projects/41/brownfield/spec/decide",
        "/api/projects/41/scope/register",
        "/api/projects/41/scope/extension/start",
        "/api/projects/41/scope/extension/prd/record",
        "/api/projects/41/scope/extension/spec/decide",
        "/api/projects/41/discovery/prd/record",
        "/api/projects/41/discovery/prd/decide",
        "/api/projects/41/vision/generate",
        "/api/projects/41/vision/decide",
    ],
)
def test_retired_mutation_api_routes_are_absent(path: str) -> None:
    """Do not preserve HTTP compatibility for retired lifecycle concepts."""
    response = TestClient(api_module.app).post(path, json={})

    assert response.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/41/backlog/generate",
        "/api/projects/41/roadmap/generate",
        "/api/projects/41/story/generate",
    ],
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_payload", {"caller": "owned"}),
        ("model_id", "caller/model"),
    ],
)
def test_retained_delivery_api_rejects_model_owned_input(
    path: str,
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep model input preparation and model selection inside the host."""
    payload: dict[str, object] = {
        "idempotency_key": "delivery-41",
        "actor": "operator",
        field: value,
    }

    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=_all_request_kinds_position()),
    )

    response = TestClient(api_module.app).post(path, json=payload)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "method_name"),
    [
        ("/api/projects/41/backlog/generate", "generate_backlog"),
        ("/api/projects/41/roadmap/generate", "generate_roadmap"),
        ("/api/projects/41/story/generate", "generate_story"),
    ],
)
def test_retained_delivery_api_calls_host_prepared_application_method(
    path: str,
    method_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route metadata-only delivery requests through semantic methods."""
    application = _FakeApiApplication(position=_all_request_kinds_position())
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        path,
        json={"idempotency_key": "delivery-41", "actor": "operator"},
    )

    assert response.status_code == HTTPStatus.OK
    assert len(application.requests) == 1
    assert isinstance(application.requests[0], DeliveryActionRequest)
    assert method_name.startswith("generate_")


def test_retained_non_agentic_delivery_api_uses_semantic_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep retained review commands callable without an input_payload envelope."""
    application = _FakeApiApplication(position=_all_request_kinds_position())
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        "/api/projects/41/backlog/decide",
        json={
            "idempotency_key": "backlog-review-41",
            "actor": "operator",
            "semantic_input": {
                "backlog_artifact_id": 7,
                "artifact_fingerprint": "backlog-7",
                "decision": "accepted",
                "rationale": "Reviewed",
            },
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert len(application.requests) == 1
    request = cast("TransitionRequest", application.requests[0])
    assert request.kind == "decide_backlog"


def test_semantic_sprint_generation_api_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose only task-specific Sprint planning semantics over HTTP."""
    application = _FakeApiApplication(position=_all_request_kinds_position())
    monkeypatch.setattr(api_module, "_application", lambda: application)
    payload = {
        "user_input": "Prioritize replay safety.",
        "selected_story_ids": [7, 9],
        "max_story_points": 8,
        "include_task_decomposition": False,
        "team_name": "Platform",
        "idempotency_key": "sprint-41",
        "actor": "operator",
    }

    response = TestClient(api_module.app).post(
        "/api/projects/41/sprint/generate",
        json=payload,
    )

    assert response.status_code == HTTPStatus.OK
    assert len(application.requests) == 1
    request = cast("SprintPlanningRequest", application.requests[0])
    assert request.guidance == payload["user_input"]
    assert request.selected_story_ids == (7, 9)

    for extra_field in (
        "model_id",
        "input_payload",
        "candidate_set_fingerprint",
        "graph_version",
        "sprint_duration_days",
        "team_velocity_assumption",
    ):
        rejected = TestClient(api_module.app).post(
            "/api/projects/41/sprint/generate",
            json={**payload, extra_field: "caller-owned"},
        )
        assert rejected.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_retained_non_agentic_delivery_api_rejects_input_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject the removed generic payload field on retained positioned routes."""
    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=_all_request_kinds_position()),
    )

    response = TestClient(api_module.app).post(
        "/api/projects/41/backlog/decide",
        json={
            "idempotency_key": "backlog-review-41",
            "actor": "operator",
            "input_payload": {"decision": "accepted"},
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_position_endpoint_delegates_once_and_state_endpoint_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose only the graph position route and make one authority query."""
    application = _FakeApiApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    response = client.get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"] == position_fixture().model_dump(mode="json")
    assert application.position_calls == [41]
    assert client.get("/api/projects/41/state").status_code == HTTPStatus.NOT_FOUND


def test_position_advertises_only_executable_semantic_api_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exclude CLI-only and unavailable actions from the HTTP projection."""
    position = _all_request_kinds_position()
    application = _FakeApiApplication(position=position)
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    response = client.get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    actions = response.json()["actions"]
    expected = (
        set(api_module.SEMANTIC_API_PATHS)
        | set(api_module.DELIVERY_API_PATHS)
        | set(api_module.POSITIONED_API_PATHS)
    )
    assert {item["request_kind"] for item in actions} == expected
    assert all(item["endpoint"].startswith("/") is False for item in actions)
    assert "record_sprint_plan" in expected
    assert all("decision_fingerprint" not in item for item in actions)


def test_position_advertises_waiting_vision_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose the semantic Vision review while the graph waits for a decision."""
    decision = NodeDecision(
        node_id="vision.review",
        child_graph_id="vision",
        request_kind="decide_vision_review",
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_REVIEW_REQUIRED",
        decision_fingerprint="decision-vision-review",
    )
    position = _vision_position(decision).model_copy(
        update={
            "available_nodes": (),
            "waiting_nodes": (decision.node_id,),
        }
    )
    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=position),
    )

    response = TestClient(api_module.app).get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["actions"] == [
        {
            "node_id": "vision.review",
            "instance_key": None,
            "request_kind": "decide_vision_review",
            "endpoint": "vision/review",
            "transport": "semantic",
        }
    ]


def test_position_advertises_waiting_authority_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose the semantic Authority review while the graph waits for a decision."""
    decision = NodeDecision(
        node_id="authority.review",
        instance_key="authority:17",
        child_graph_id="authority",
        request_kind="decide_authority",
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="AUTHORITY_REVIEW_REQUIRED",
        decision_fingerprint="decision-authority-review",
    )
    position = _vision_position(decision).model_copy(
        update={
            "available_nodes": (),
            "waiting_nodes": (decision.node_id,),
        }
    )
    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=position),
    )

    response = TestClient(api_module.app).get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["actions"] == [
        {
            "node_id": "authority.review",
            "instance_key": "authority:17",
            "request_kind": "decide_authority",
            "endpoint": "authority/decision",
            "transport": "semantic",
        }
    ]


def test_position_omits_ambiguous_unselectable_semantic_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advertise duplicate actions only when the API accepts their exact selector."""
    vision_decisions = tuple(
        _vision_decision(f"decision-vision-{index}").model_copy(
            update={"instance_key": f"after-turn:{index}"}
        )
        for index in range(2)
    )
    story_decisions = tuple(
        _delivery_decision(
            node_id="planning.story.generate",
            request_kind="record_story_draft",
            instance_key=f"requirement:req-{index}",
        ).model_copy(update={"decision_fingerprint": f"decision-story-{index}"})
        for index in range(2)
    )
    decisions = (*vision_decisions, *story_decisions)
    position = _vision_position(*decisions)
    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=position),
    )

    response = TestClient(api_module.app).get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    actions = response.json()["actions"]
    assert [item["request_kind"] for item in actions] == [
        "record_story_draft",
        "record_story_draft",
    ]
    assert [item["instance_key"] for item in actions] == [
        "requirement:req-0",
        "requirement:req-1",
    ]


def test_structured_conflict_advertises_actions_for_returned_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Include replacement controls with a stale-position response."""
    position = _all_request_kinds_position()
    result = TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="stale",
        ),
    )
    application = _FakeApiApplication(
        position=position,
        transition_result=result,
    )
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    response = client.post(
        "/api/projects/41/authority/decision",
        json={
            "idempotency_key": "stale-api-41",
            "actor": "dashboard-user",
            "decision": "accepted",
            "rationale": "Reviewed",
        },
    )

    assert response.status_code == HTTPStatus.CONFLICT
    detail = response.json()["detail"]
    assert detail["position"] == position.model_dump(mode="json")
    assert {item["request_kind"] for item in detail["actions"]} == (
        set(api_module.SEMANTIC_API_PATHS)
        | set(api_module.DELIVERY_API_PATHS)
        | set(api_module.POSITIONED_API_PATHS)
    )


def test_agentic_application_retry_reaches_durable_start_receipt_when_stale() -> None:
    """Bypass fresh-position preflight for an exact transport replay key."""
    stale_position = WorkflowPosition(
        project_id=41,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-after-completion",
        evaluated_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        available_nodes=(),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=True,
        decisions=(),
    )
    prior_result = TransitionResult(
        ok=True,
        replayed=True,
        applied_node_id="vision.generate",
        output={"vision_artifact_id": 17},
        position=stale_position,
    )

    class ReplayDomain:
        def __init__(self) -> None:
            self.requests: list[TransitionRequest] = []

        def position(self, project_id: int) -> WorkflowPosition:
            assert project_id == PROJECT_ID
            return stale_position

        def transition(self, request: TransitionRequest) -> TransitionResult:
            self.requests.append(request)
            return prior_result

        def load_persisted_attempt_input(
            self,
            *,
            project_id: int,
            attempt_id: int,
            attempt_fingerprint: str,
        ) -> JsonObject:
            pytest.fail(
                "replayed action unexpectedly loaded persisted attempt input "
                f"{project_id}:{attempt_id}:{attempt_fingerprint}"
            )

    class NoRecipeRegistry:
        def require(self, node_id: str) -> None:
            pytest.fail(f"replay looked up recipe {node_id}")

    domain = ReplayDomain()
    application = AgileForgeApplication(
        workflow_domain=domain,
        recipe_registry=cast("AdkRecipeRegistry", NoRecipeRegistry()),
    )

    result = application.run_agentic_action(
        AgenticActionRequest(
            project_id=41,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="facts-before-completion",
            decision_fingerprint="decision-vision",
            node_id="vision.generate",
            input_payload={"prompt": "draft"},
            model_id="offline/model",
            idempotency_key="dashboard-vision-41",
            actor="dashboard-user",
        )
    )

    assert result == prior_result
    assert len(domain.requests) == 1
    assert isinstance(domain.requests[0], StartNodeAttempt)


def test_authority_endpoint_submits_exact_typed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit only the semantic authority choice to the application boundary."""
    application = _FakeApiApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    response = client.post(
        "/api/projects/41/authority/decision",
        json={
            "idempotency_key": "accept-api-41",
            "actor": "dashboard-user",
            "correlation_id": "corr-api-41",
            "decision": "accepted",
            "rationale": "Reviewed",
        },
    )

    assert response.status_code == HTTPStatus.OK
    request = cast("AuthorityReviewRequest", application.requests[0])
    assert isinstance(request, AuthorityReviewRequest)
    assert request.actor == "dashboard-user"
