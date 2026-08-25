"""API adapter tests for exact typed workflow requests."""

from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

import api as api_module
import services.application as application_module
from api import (
    CreateProjectRequest,
    SprintPlanningApiRequest,
    build_create_project_command,
)
from cli.workflow_commands import COMMAND_PREFIXES
from models.core import Project, UserStory
from models.workflow import (
    BacklogArtifact,
    RoadmapArtifact,
    SprintPlanArtifact,
    StoryArtifact,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactRepository
from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    BacklogReviewRequest,
    CreateProjectCommand,
    DeliveryActionInputService,
    DeliveryActionRequest,
    DeliveryReviewSelectionService,
    ExpectedPlanningReviewBinding,
    ProductGoalLifecycleServices,
    ProductGoalResponseRequest,
    ProductGoalReviewRequest,
    RoadmapReviewRequest,
    SpecificationReviewRequest,
    SpecificationSourceRegistrationRequest,
    SpecificationStructuringRequest,
    SprintPlanningInputService,
    SprintPlanningRequest,
    SprintPlanReviewRequest,
    StoryCorrectionRequest,
    StoryReviewRequest,
    VisionBootstrapRequest,
    VisionResponseRequest,
    VisionReviewRequest,
    WorkflowDomainPort,
)
from services.contracts.backlog import BacklogBuilderInput
from services.contracts.roadmap import RoadmapBuilderInput
from services.contracts.sprint import SprintPlannerInput
from services.contracts.story import UserStoryWriterInput
from services.node_attempt_replay import (
    DurableTransitionReplayService,
    NodeAttemptReplayQuery,
    TransitionReplayQuery,
)
from services.product_goal_interview_input import ProductGoalInterviewInputService
from services.read_projections import DurableReadProjectionService
from tests.adapters.test_command_renderer import position_fixture
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.test_execution_transitions import (
    EVALUATED_AT as EXECUTION_EVALUATED_AT,
)
from tests.workflow.test_execution_transitions import (
    _complete_execution_sprint_with_unselected_story,
    _complete_task,
)
from tests.workflow.test_execution_transitions import (
    _domain as execution_domain,
)
from tests.workflow.test_execution_transitions import (
    _guards as execution_guards,
)
from tests.workflow.test_planning_transitions import (
    _apply_current_dependencies,
    _guards,
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _record_sprint_plan_draft,
    _seed_accepted_backlog,
    _select_for_sprint,
    _validate_story_structurally,
)
from tests.workflow.test_planning_transitions import (
    _domain as planning_domain,
)
from workflow.clock import FixedClock
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
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    selected_scope_stories,
    story_dependency_source_fingerprint,
)
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import (
    ApplyStoryDependencies,
    CloseSprint,
    CloseStory,
    CompleteTask,
    DecideBacklog,
    DecideRoadmap,
    DecideSpecification,
    DecideSprintPlan,
    DecideStory,
    RecordPostSprintTriage,
    ReviewSprint,
    StartNodeAttempt,
    TransitionRequest,
)
from workflow.requests.planning import ReviewedDependencyEdge

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from adapters.adk.recipes import AdkRecipeRegistry
    from workflow.requests.base import PositionedRequest

PROJECT_ID = 41
DELIVERY_ARTIFACT_ID = 7
DELIVERY_ARTIFACT_FINGERPRINT = "sha256:artifact-7"
type DeliveryReviewRequest = (
    BacklogReviewRequest
    | RoadmapReviewRequest
    | SprintPlanReviewRequest
    | StoryReviewRequest
)


@dataclass(frozen=True)
class _StatusReadApplication:
    """Expose the real durable status projection to API route tests."""

    reads: DurableReadProjectionService


def test_vision_and_goal_status_endpoints_expose_durable_interview_contract(
    engine: "Engine",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP status reads preserve the structured empty interview contract."""
    with Session(engine) as session:
        project = Project(name="API interview status")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id
    application = _StatusReadApplication(
        reads=DurableReadProjectionService(engine=engine)
    )
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    vision = client.get(f"/api/projects/{project_id}/vision/status")
    goal = client.get(f"/api/projects/{project_id}/goals/status")

    assert vision.status_code == HTTPStatus.OK
    assert vision.json()["data"] == {
        "bootstrap_available": True,
        "current": None,
        "draft": None,
        "transcript": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_NOT_ACCEPTED",
    }
    assert goal.status_code == HTTPStatus.OK
    assert goal.json()["data"] == {
        "accepted_vision": None,
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": None,
        "stale_reason": "GOAL_NOT_ACTIVE",
    }


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
            "/api/projects/41/specifications/source",
            {
                "source_path": "SPECIFICATION.md",
                "preparation_capability": "grill-with-docs",
                "idempotency_key": "specification-41",
                "actor": "operator",
            },
            "canonical_content",
        ),
        (
            "/api/projects/41/specifications/structure",
            {
                "idempotency_key": "specification-41",
                "actor": "operator",
            },
            "content_ref",
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
    headers = (
        {"X-AgileForge-Expected-Decision": "sha256:position"}
        if path
        in {
            "/api/projects/41/specifications/source",
            "/api/projects/41/specifications/structure",
        }
        else {}
    )
    response = TestClient(api_module.app).post(
        path,
        headers=headers,
        json={**payload, internal_field: {"caller": "owned"}},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "decision"),
    [
        ("/api/projects/41/vision/review", "accepted"),
        ("/api/projects/41/goals/review", "accepted"),
        ("/api/projects/41/specifications/review", "accepted"),
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


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("/api/projects/41/backlog/decide", {}),
        ("/api/projects/41/roadmap/decide", {}),
        (
            "/api/projects/41/story/decide",
            {},
        ),
        ("/api/projects/41/sprint/decide", {}),
    ],
)
def test_delivery_review_api_rejects_whitespace_rationale(
    path: str,
    extra: dict[str, object],
) -> None:
    """Reject normalized-empty reasons at every delivery review route."""
    response = TestClient(api_module.app).post(
        path,
        headers=_planning_review_headers(path),
        json={
            "decision": "accepted",
            "rationale": "  \t",
            "idempotency_key": "delivery-review-41",
            "actor": "operator",
            **extra,
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize("selected_story_ids", [(0,), (-1,), (7, 7)])
def test_sprint_request_model_rejects_invalid_story_ids(
    selected_story_ids: tuple[int, ...],
) -> None:
    """Reject non-positive and duplicate Story IDs in the HTTP request model."""
    with pytest.raises(ValidationError):
        SprintPlanningApiRequest(
            selected_story_ids=list(selected_story_ids),
            team_name="Platform",
            idempotency_key="sprint-41",
            actor="operator",
        )


@pytest.mark.parametrize("selected_story_ids", [[0], [-1], [7, 7]])
def test_sprint_api_returns_422_for_invalid_story_ids(
    selected_story_ids: list[int],
) -> None:
    """Keep invalid manual Story selection out of application execution."""
    response = TestClient(
        api_module.app,
        raise_server_exceptions=False,
    ).post(
        "/api/projects/41/sprint/generate",
        json={
            "selected_story_ids": selected_story_ids,
            "team_name": "Platform",
            "idempotency_key": "sprint-41",
            "actor": "operator",
        },
    )

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

    def decide_backlog(
        self,
        request: object,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
        del expected
        return self._record_delivery_request(request)

    def decide_roadmap(
        self,
        request: object,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
        del expected
        return self._record_delivery_request(request)

    def decide_story(
        self,
        request: object,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
        del expected
        return self._record_delivery_request(request)

    def decide_sprint_plan(
        self,
        request: object,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
        del expected
        return self._record_delivery_request(request)

    def apply_story_dependencies(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def repair_story_readiness(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def start_sprint(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def complete_task(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def close_story(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def review_sprint(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def close_sprint(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def record_post_sprint_triage(self, request: object) -> TransitionResult:
        return self._record_delivery_request(request)

    def _record_delivery_request(self, request: object) -> TransitionResult:
        self.requests.append(request)
        return self._transition_result or TransitionResult(
            ok=True,
            position=self._position,
        )

    def review_vision(self, request: VisionReviewRequest) -> TransitionResult:
        return self._record_delivery_request(request)

    def review_product_goal(
        self,
        request: ProductGoalReviewRequest,
    ) -> TransitionResult:
        return self._record_delivery_request(request)

    def review_specification(
        self,
        request: SpecificationReviewRequest,
    ) -> TransitionResult:
        return self._record_delivery_request(request)

    def register_specification_source(
        self,
        request: SpecificationSourceRegistrationRequest,
    ) -> TransitionResult:
        return self._record_delivery_request(request)

    def structure_specification(
        self,
        request: SpecificationStructuringRequest,
    ) -> TransitionResult:
        return self._record_delivery_request(request)


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

    def build_bootstrap(
        self,
        project_id: int,
        decision: NodeDecision,
    ) -> JsonObject:
        return {
            "project_id": project_id,
            "decision": decision.decision_fingerprint,
            "operation": "bootstrap",
        }

    def build_clarification(
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

    def build_story_correction(
        self,
        *,
        project_id: int,
        decisions: tuple[NodeDecision, ...],
        request: StoryCorrectionRequest,
    ) -> tuple[NodeDecision, JsonObject] | WorkflowError | None:
        """Supply no correction payload in tests that only exercise generation."""
        _ = project_id, decisions, request
        return None


class _DeliveryReviewSelection:
    def __init__(
        self,
        *,
        identity: tuple[int, str] | None,
        replay: TransitionResult | None = None,
    ) -> None:
        self.identity = identity
        self.replay_result = replay
        self.replay_queries: list[TransitionReplayQuery] = []
        self.identity_calls: list[tuple[int, str, str]] = []

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        self.replay_queries.append(query)
        return self.replay_result

    def review_identity(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        fact_type: str,
    ) -> tuple[int, str] | None:
        self.identity_calls.append(
            (project_id, decision.decision_fingerprint, fact_type)
        )
        return self.identity


class _PlanningActionSelection:
    def __init__(self, replay: TransitionResult | None = None) -> None:
        self.replay_result = replay
        self.replay_queries: list[TransitionReplayQuery] = []
        self.prepare_calls: list[tuple[str, int, str, object]] = []

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        self.replay_queries.append(query)
        return self.replay_result

    def prepare_story_dependencies(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        selected_story_ids: tuple[int, ...],
    ) -> str:
        self.prepare_calls.append(
            (
                "apply_story_dependencies",
                project_id,
                decision.decision_fingerprint,
                selected_story_ids,
            )
        )
        return "dependency-source-current"

    def prepare_story_readiness(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        repair_story_ids: tuple[int, ...],
    ) -> tuple[tuple[int, ...], str]:
        self.prepare_calls.append(
            (
                "repair_story_readiness",
                project_id,
                decision.decision_fingerprint,
                repair_story_ids,
            )
        )
        return (7, 9), "readiness-current"

    def prepare_sprint_start(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int, str, str]:
        self.prepare_calls.append(
            ("start_sprint", project_id, decision.decision_fingerprint, None)
        )
        return 29, 31, "plan-current", "candidates-current"


class _ExecutionActionSelection:
    def __init__(self, replay: TransitionResult | None = None) -> None:
        self.replay_result = replay
        self.replay_queries: list[TransitionReplayQuery] = []
        self.prepare_calls: list[tuple[str, int, str]] = []

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        self.replay_queries.append(query)
        return self.replay_result

    def prepare_task_completion(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int]:
        self.prepare_calls.append(
            ("complete_task", project_id, decision.decision_fingerprint)
        )
        return 7, 31

    def prepare_story_close(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int, str]:
        self.prepare_calls.append(
            ("close_story", project_id, decision.decision_fingerprint)
        )
        return 9, 31, "story-completion-current"

    def prepare_sprint_review(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str]:
        self.prepare_calls.append(
            ("review_sprint", project_id, decision.decision_fingerprint)
        )
        return 31, "sprint-review-current"

    def prepare_sprint_close(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str, str]:
        self.prepare_calls.append(
            ("close_sprint", project_id, decision.decision_fingerprint)
        )
        return 31, "sprint-review-current", "sprint-close-current"

    def prepare_post_sprint_triage(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str]:
        self.prepare_calls.append(
            (
                "record_post_sprint_triage",
                project_id,
                decision.decision_fingerprint,
            )
        )
        return 31, "sprint-close-current"


class _CapturingTransitionDomain(_BoundaryDomain):
    def __init__(self, position: WorkflowPosition) -> None:
        super().__init__(position)
        self.requests: list[TransitionRequest] = []

    def transition(self, request: TransitionRequest) -> TransitionResult:
        self.requests.append(request)
        positioned = cast("PositionedRequest", request)
        return TransitionResult(ok=True, applied_node_id=positioned.decision_node_id())


class _CapturingApplication(AgileForgeApplication):
    def __init__(self, domain: _BoundaryDomain) -> None:
        super().__init__(workflow_domain=domain, vision_input=_VisionInput())
        self.agent_requests: list[AgenticActionRequest] = []

    def run_agentic_action(self, request: AgenticActionRequest) -> TransitionResult:
        self.agent_requests.append(request)
        return TransitionResult(ok=True)


class _CapturingProductGoalApplication(AgileForgeApplication):
    def __init__(self, domain: _BoundaryDomain) -> None:
        super().__init__(
            workflow_domain=domain,
            product_goal_services=ProductGoalLifecycleServices(
                interview_input=_ProductGoalInput(),
            ),
        )
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


def _vision_decision(
    fingerprint: str,
    *,
    instance_key: str | None = None,
) -> NodeDecision:
    return NodeDecision(
        node_id="vision.interview",
        instance_key=instance_key,
        child_graph_id="vision",
        request_kind="record_vision_interview_turn",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_CLARIFICATION_REQUIRED",
        decision_fingerprint=fingerprint,
    )


def _product_goal_decision(
    fingerprint: str,
    *,
    instance_key: str | None = None,
) -> NodeDecision:
    return NodeDecision(
        node_id="goal.interview",
        instance_key=instance_key,
        child_graph_id="product_goal",
        request_kind="record_product_goal_interview_turn",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="PRODUCT_GOAL_CLARIFICATION_REQUIRED",
        decision_fingerprint=fingerprint,
    )


def _vision_bootstrap_decision(fingerprint: str) -> NodeDecision:
    return NodeDecision(
        node_id="vision.bootstrap",
        child_graph_id="vision",
        request_kind="generate_vision_bootstrap",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_BOOTSTRAP_REQUIRED",
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


def _planning_review_headers(path: str) -> dict[str, str]:
    """Supply the machine-only binding captured by the review read."""
    headers = {"X-AgileForge-Expected-Decision": "decision-shown"}
    if path.endswith("/story/decide"):
        headers["X-AgileForge-Expected-Instance"] = "backlog_item:PBI-000007"
    return headers


@pytest.mark.parametrize(
    "case",
    [
        (
            "apply_story_dependencies",
            "StoryDependenciesApplyRequest",
            "apply_story_dependencies",
            "planning.story_dependencies",
            {
                "selected_story_ids": (7, 9),
                "reviewed_edges": (
                    {
                        "dependent_story_id": 9,
                        "prerequisite_story_id": 7,
                        "reason": "Story 9 requires Story 7.",
                    },
                ),
            },
            {
                "selected_story_ids": [7, 9],
                "reviewed_edges": [
                    {
                        "dependent_story_id": 9,
                        "prerequisite_story_id": 7,
                        "reason": "Story 9 requires Story 7.",
                    }
                ],
                "source_fingerprint": "dependency-source-current",
            },
            {
                "selected_story_ids": [7, 9],
                "reviewed_edges": [
                    {
                        "dependent_story_id": 9,
                        "prerequisite_story_id": 7,
                        "reason": "Story 9 requires Story 7.",
                    }
                ],
            },
        ),
        (
            "repair_story_readiness",
            "StoryReadinessRepairRequest",
            "repair_story_readiness",
            "planning.story_readiness",
            {
                "repairs": (
                    {"story_id": 7, "story_points": 3, "rank": "101"},
                    {"story_id": 9, "story_points": 5, "rank": "102"},
                )
            },
            {
                "story_ids": [7, 9],
                "repairs": [
                    {"story_id": 7, "story_points": 3, "rank": "101"},
                    {"story_id": 9, "story_points": 5, "rank": "102"},
                ],
                "expected_readiness_fingerprint": "readiness-current",
            },
            {
                "repairs": [
                    {"story_id": 7, "story_points": 3, "rank": "101"},
                    {"story_id": 9, "story_points": 5, "rank": "102"},
                ]
            },
        ),
        (
            "start_sprint",
            "SprintStartRequest",
            "start_sprint",
            "planning.sprint.start",
            {},
            {},
            {},
        ),
    ],
)
def test_planning_action_application_derives_internal_guards(
    case: tuple[
        str,
        str,
        str,
        str,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ],
) -> None:
    """Build guarded workflow requests only after semantic replay and selection."""
    (
        method_name,
        request_type_name,
        request_kind,
        node_id,
        request_fields,
        expected_internal,
        expected_operator_input,
    ) = case
    decision = _delivery_decision(node_id=node_id, request_kind=request_kind)
    position = _vision_position(decision)
    domain = _CapturingTransitionDomain(position)
    selection = _PlanningActionSelection()
    application = AgileForgeApplication(
        workflow_domain=domain,
        planning_action_selection=selection,
    )
    request_type = cast(
        "type[BaseModel]", getattr(application_module, request_type_name)
    )
    request = request_type(
        project_id=PROJECT_ID,
        idempotency_key=f"{request_kind}-41",
        actor="operator",
        **request_fields,
    )

    result = getattr(application, method_name)(request)

    assert result.ok is True
    assert domain.position_calls == [PROJECT_ID]
    assert len(domain.requests) == 1
    internal = domain.requests[0].model_dump(mode="json")
    assert internal["kind"] == request_kind
    for field, expected in expected_internal.items():
        assert internal[field] == expected
    assert selection.replay_queries[0].operator_input == expected_operator_input


@pytest.mark.parametrize(
    ("method_name", "request_type_name", "request_kind", "request_fields"),
    [
        (
            "apply_story_dependencies",
            "StoryDependenciesApplyRequest",
            "apply_story_dependencies",
            {"selected_story_ids": (7,), "reviewed_edges": ()},
        ),
        (
            "repair_story_readiness",
            "StoryReadinessRepairRequest",
            "repair_story_readiness",
            {"repairs": ({"story_id": 7, "story_points": 3, "rank": "101"},)},
        ),
        ("start_sprint", "SprintStartRequest", "start_sprint", {}),
    ],
)
def test_planning_action_application_replays_before_current_position(
    method_name: str,
    request_type_name: str,
    request_kind: str,
    request_fields: dict[str, object],
) -> None:
    """Return exact replay/conflict results before reading advanced graph state."""
    replayed = TransitionResult(ok=True, replayed=True, applied_node_id="advanced")
    selection = _PlanningActionSelection(replay=replayed)
    domain = _BoundaryDomain(_vision_position())
    application = AgileForgeApplication(
        workflow_domain=domain,
        planning_action_selection=selection,
    )
    request_type = cast(
        "type[BaseModel]", getattr(application_module, request_type_name)
    )
    request = request_type(
        project_id=PROJECT_ID,
        idempotency_key=f"{request_kind}-41",
        actor="operator",
        **request_fields,
    )

    result = getattr(application, method_name)(request)

    assert result == replayed
    assert domain.position_calls == []
    assert selection.prepare_calls == []


@pytest.mark.parametrize(
    "case",
    [
        (
            "complete_task",
            "CompleteTaskRequest",
            "complete_task",
            "execution.task.complete",
            "task:7",
            {
                "instance_key": "task:7",
                "outcome_summary": "Implemented semantic execution.",
                "artifact_refs": ("services/application.py",),
                "acceptance_result": "fully_met",
                "checklist_result": {"Focused tests": "passed"},
            },
            {"task_id": 7},
        ),
        (
            "close_story",
            "CloseStoryRequest",
            "close_story",
            "execution.story.close",
            "story:9",
            {
                "instance_key": "story:9",
                "resolution": "Completed",
                "delivered": "Semantic execution transport.",
                "evidence": "Focused tests pass.",
                "known_gaps": "None.",
            },
            {"story_id": 9},
        ),
        (
            "review_sprint",
            "SprintReviewRequest",
            "review_sprint",
            "execution.sprint.review",
            "sprint:31",
            {"instance_key": "sprint:31"},
            {"sprint_id": 31, "review_fingerprint": "sprint-review-current"},
        ),
        (
            "close_sprint",
            "SprintCloseRequest",
            "close_sprint",
            "execution.sprint.close",
            "sprint:31",
            {"instance_key": "sprint:31"},
            {"sprint_id": 31, "review_fingerprint": "sprint-review-current"},
        ),
        (
            "record_post_sprint_triage",
            "PostSprintTriageRequest",
            "record_post_sprint_triage",
            "execution.post_sprint_triage",
            "sprint:31",
            {
                "instance_key": "sprint:31",
                "impact": "backlog",
                "canonical_payload": {"summary": "Follow-up required."},
            },
            {"sprint_id": 31},
        ),
    ],
)
def test_execution_action_application_derives_internal_identity(
    case: tuple[
        str,
        str,
        str,
        str,
        str,
        dict[str, object],
        dict[str, object],
    ],
) -> None:
    """Build exact execution requests from semantic input and durable selection."""
    (
        method_name,
        request_type_name,
        request_kind,
        node_id,
        instance_key,
        request_fields,
        expected_internal,
    ) = case
    decision = _delivery_decision(
        node_id=node_id,
        request_kind=request_kind,
        instance_key=instance_key,
    )
    domain = _CapturingTransitionDomain(_vision_position(decision))
    selection = _ExecutionActionSelection()
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=selection,
    )
    request_type = cast(
        "type[BaseModel]", getattr(application_module, request_type_name)
    )
    request = request_type(
        project_id=PROJECT_ID,
        idempotency_key=f"{request_kind}-41",
        actor="operator",
        **request_fields,
    )

    result = getattr(application, method_name)(request)

    assert result.ok is True
    assert domain.position_calls == [PROJECT_ID]
    assert len(domain.requests) == 1
    internal = domain.requests[0].model_dump(mode="json")
    assert internal["kind"] == request_kind
    assert internal["instance_key"] == instance_key
    for field, expected in expected_internal.items():
        assert internal[field] == expected
    assert len(selection.replay_queries) == 1


@pytest.mark.parametrize(
    ("method_name", "request_type_name", "request_kind", "request_fields"),
    [
        (
            "complete_task",
            "CompleteTaskRequest",
            "complete_task",
            {
                "instance_key": "task:7",
                "outcome_summary": "Implemented semantic execution.",
                "artifact_refs": ("services/application.py",),
                "acceptance_result": "fully_met",
                "checklist_result": {"Focused tests": "passed"},
            },
        ),
        (
            "close_story",
            "CloseStoryRequest",
            "close_story",
            {
                "instance_key": "story:9",
                "resolution": "Completed",
                "delivered": "Semantic execution transport.",
                "evidence": "Focused tests pass.",
                "known_gaps": "None.",
            },
        ),
        (
            "review_sprint",
            "SprintReviewRequest",
            "review_sprint",
            {"instance_key": "sprint:31"},
        ),
        (
            "close_sprint",
            "SprintCloseRequest",
            "close_sprint",
            {"instance_key": "sprint:31"},
        ),
        (
            "record_post_sprint_triage",
            "PostSprintTriageRequest",
            "record_post_sprint_triage",
            {
                "instance_key": "sprint:31",
                "impact": "none",
                "canonical_payload": {"summary": "No follow-up."},
            },
        ),
    ],
)
def test_execution_action_application_replays_before_current_position(
    method_name: str,
    request_type_name: str,
    request_kind: str,
    request_fields: dict[str, object],
) -> None:
    """Return an exact replay or changed-input conflict before position reads."""
    replayed = TransitionResult(ok=True, replayed=True, applied_node_id="advanced")
    selection = _ExecutionActionSelection(replay=replayed)
    domain = _BoundaryDomain(_vision_position())
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=selection,
    )
    request_type = cast(
        "type[BaseModel]", getattr(application_module, request_type_name)
    )
    request = request_type(
        project_id=PROJECT_ID,
        idempotency_key=f"{request_kind}-41",
        actor="operator",
        **request_fields,
    )

    result = getattr(application, method_name)(request)

    assert result == replayed
    assert domain.position_calls == []
    assert selection.prepare_calls == []


@pytest.mark.parametrize(
    ("request_type_name", "request_fields"),
    [
        (
            "CompleteTaskRequest",
            {
                "instance_key": None,
                "outcome_summary": "Done.",
                "artifact_refs": ("result",),
                "acceptance_result": "fully_met",
                "checklist_result": {"Tests": "passed"},
            },
        ),
        (
            "CompleteTaskRequest",
            {
                "instance_key": "task:7",
                "outcome_summary": "Done.",
                "artifact_refs": (),
                "acceptance_result": "fully_met",
                "checklist_result": {"Tests": "passed"},
            },
        ),
        (
            "CompleteTaskRequest",
            {
                "instance_key": "task:7",
                "outcome_summary": "Done.",
                "artifact_refs": ("result",),
                "acceptance_result": "fully_met",
                "checklist_result": {"Tests": 1},
            },
        ),
        (
            "CloseStoryRequest",
            {
                "instance_key": "story:9",
                "resolution": "Completed",
                "delivered": " ",
                "evidence": "Tests.",
                "known_gaps": "None.",
            },
        ),
        ("SprintReviewRequest", {"instance_key": None}),
        ("SprintCloseRequest", {"instance_key": None}),
        (
            "PostSprintTriageRequest",
            {
                "instance_key": None,
                "impact": "none",
                "canonical_payload": {},
            },
        ),
    ],
)
def test_execution_application_requests_are_strict(
    request_type_name: str,
    request_fields: dict[str, object],
) -> None:
    """Reject null selectors, blank evidence, and untyped semantic mappings."""
    request_type = cast(
        "type[BaseModel]", getattr(application_module, request_type_name)
    )

    with pytest.raises(ValidationError):
        request_type(
            project_id=PROJECT_ID,
            idempotency_key="invalid-execution-41",
            actor="operator",
            **request_fields,
        )


@pytest.mark.parametrize(
    ("method_name", "stored", "public_request", "changed_input"),
    [
        (
            "complete_task",
            CompleteTask(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-task",
                decision_fingerprint="decision-task",
                instance_key="task:7",
                idempotency_key="complete-task-replay-41",
                actor="operator",
                task_id=7,
                outcome_summary="Original outcome.",
                artifact_refs=("services/application.py",),
                acceptance_result="fully_met",
                checklist_result={"Focused tests": "passed"},
            ),
            application_module.CompleteTaskRequest(
                project_id=PROJECT_ID,
                instance_key="task:7",
                outcome_summary="Original outcome.",
                artifact_refs=("services/application.py",),
                acceptance_result="fully_met",
                checklist_result={"Focused tests": "passed"},
                idempotency_key="complete-task-replay-41",
                actor="operator",
            ),
            {"outcome_summary": "Changed outcome."},
        ),
        (
            "close_story",
            CloseStory(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-story",
                decision_fingerprint="decision-story",
                instance_key="story:9",
                idempotency_key="close-story-replay-41",
                actor="operator",
                story_id=9,
                resolution="Completed",
                delivered="Semantic transport delivered.",
                evidence="Focused tests pass.",
                known_gaps="None.",
            ),
            application_module.CloseStoryRequest(
                project_id=PROJECT_ID,
                instance_key="story:9",
                resolution="Completed",
                delivered="Semantic transport delivered.",
                evidence="Focused tests pass.",
                known_gaps="None.",
                idempotency_key="close-story-replay-41",
                actor="operator",
            ),
            {"evidence": "Changed evidence."},
        ),
        (
            "review_sprint",
            ReviewSprint(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-review",
                decision_fingerprint="decision-review",
                instance_key="sprint:31",
                idempotency_key="review-sprint-replay-41",
                actor="operator",
                sprint_id=31,
                review_fingerprint="sprint-review-current",
            ),
            application_module.SprintReviewRequest(
                project_id=PROJECT_ID,
                instance_key="sprint:31",
                idempotency_key="review-sprint-replay-41",
                actor="operator",
            ),
            {"instance_key": "sprint:32"},
        ),
        (
            "close_sprint",
            CloseSprint(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-close",
                decision_fingerprint="decision-close",
                instance_key="sprint:31",
                idempotency_key="close-sprint-replay-41",
                actor="operator",
                sprint_id=31,
                review_fingerprint="sprint-review-current",
            ),
            application_module.SprintCloseRequest(
                project_id=PROJECT_ID,
                instance_key="sprint:31",
                idempotency_key="close-sprint-replay-41",
                actor="operator",
            ),
            {"instance_key": "sprint:32"},
        ),
        (
            "record_post_sprint_triage",
            RecordPostSprintTriage(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-triage",
                decision_fingerprint="decision-triage",
                instance_key="sprint:31",
                idempotency_key="triage-sprint-replay-41",
                actor="operator",
                sprint_id=31,
                impact="none",
                canonical_payload={"summary": "No follow-up."},
            ),
            application_module.PostSprintTriageRequest(
                project_id=PROJECT_ID,
                instance_key="sprint:31",
                impact="none",
                canonical_payload={"summary": "No follow-up."},
                idempotency_key="triage-sprint-replay-41",
                actor="operator",
            ),
            {"impact": "backlog"},
        ),
    ],
)
def test_execution_action_changed_retry_conflicts_before_position(
    engine: "Engine",
    method_name: str,
    stored: TransitionRequest,
    public_request: BaseModel,
    changed_input: dict[str, object],
) -> None:
    """Replay exact public semantics and reject changed input before position reads."""
    _store_completed_receipt(engine, stored, TransitionResult(ok=True))
    domain = _BoundaryDomain(_vision_position())
    service_type = application_module.ExecutionActionSelectionService
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=service_type(engine=engine),
    )
    method = getattr(application, method_name)
    replay = method(public_request)
    conflict = method(public_request.model_copy(update=changed_input))

    assert replay.replayed is True
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert domain.position_calls == []


def test_execution_selection_derives_all_current_durable_identities(
    engine: "Engine",
) -> None:
    """Resolve Task, Story, active Sprint, terminal hashes, and closure identity."""

    def with_extra_reference(decision: NodeDecision) -> NodeDecision:
        return decision.model_copy(
            update={
                "fact_references": (
                    *decision.fact_references,
                    FactReference(
                        fact_type="unexpected_guard",
                        fact_id="99",
                        fingerprint="caller-owned",
                    ),
                )
            }
        )

    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    domain = execution_domain(engine)
    service_type = application_module.ExecutionActionSelectionService
    service = service_type(engine=engine)
    task_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "execution.task.complete"
    )
    assert service.prepare_task_completion(
        project_id=project_id,
        decision=task_decision,
    ) == (task_id, sprint_id)
    assert (
        service.prepare_task_completion(
            project_id=project_id,
            decision=with_extra_reference(task_decision),
        )
        is None
    )
    assert domain.transition(_complete_task(domain, project_id, task_id)).ok

    story_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "execution.story.close"
    )
    story_target = service.prepare_story_close(
        project_id=project_id,
        decision=story_decision,
    )
    assert story_target is not None
    assert story_target[:2] == (story_id, sprint_id)
    assert story_target[2]
    assert (
        service.prepare_story_close(
            project_id=project_id,
            decision=with_extra_reference(story_decision),
        )
        is None
    )
    assert domain.transition(
        CloseStory(
            **execution_guards(
                domain,
                project_id,
                "execution.story.close",
                f"story:{story_id}",
            ),
            instance_key=f"story:{story_id}",
            idempotency_key="selection-close-story",
            story_id=story_id,
            resolution="Completed",
            delivered="Semantic transport delivered.",
            evidence="Focused tests pass.",
            known_gaps="None.",
        )
    ).ok

    review_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "execution.sprint.review"
    )
    review_target = service.prepare_sprint_review(
        project_id=project_id,
        decision=review_decision,
    )
    assert review_target is not None
    assert review_target[0] == sprint_id
    assert (
        service.prepare_sprint_review(
            project_id=project_id,
            decision=with_extra_reference(review_decision),
        )
        is None
    )
    assert domain.transition(
        ReviewSprint(
            **execution_guards(
                domain,
                project_id,
                "execution.sprint.review",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="selection-review-sprint",
            sprint_id=sprint_id,
            review_fingerprint=review_target[1],
        )
    ).ok

    close_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "execution.sprint.close"
    )
    close_target = service.prepare_sprint_close(
        project_id=project_id,
        decision=close_decision,
    )
    assert close_target is not None
    assert close_target[:2] == (sprint_id, review_target[1])
    assert close_target[2]
    assert (
        service.prepare_sprint_close(
            project_id=project_id,
            decision=with_extra_reference(close_decision),
        )
        is None
    )
    assert domain.transition(
        CloseSprint(
            **execution_guards(
                domain,
                project_id,
                "execution.sprint.close",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="selection-close-sprint",
            sprint_id=sprint_id,
            review_fingerprint=review_target[1],
        )
    ).ok

    triage_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "execution.post_sprint_triage"
    )
    assert service.prepare_post_sprint_triage(
        project_id=project_id,
        decision=triage_decision,
    ) == (sprint_id, close_target[2])
    assert (
        service.prepare_post_sprint_triage(
            project_id=project_id,
            decision=with_extra_reference(triage_decision),
        )
        is None
    )
    assert domain.transition(
        RecordPostSprintTriage(
            **execution_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="selection-triage-sprint",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    ).ok
    correction_decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "execution.post_sprint_triage"
        and item.instance_key == f"sprint:{sprint_id}"
    )
    assert service.prepare_post_sprint_triage(
        project_id=project_id,
        decision=correction_decision,
    ) == (sprint_id, close_target[2])


@pytest.mark.parametrize(
    ("request_type_name", "request_fields"),
    [
        (
            "StoryDependenciesApplyRequest",
            {"selected_story_ids": (0,), "reviewed_edges": ()},
        ),
        (
            "StoryDependenciesApplyRequest",
            {"selected_story_ids": (7, 7), "reviewed_edges": ()},
        ),
        (
            "StoryReadinessRepairRequest",
            {"repairs": ({"story_id": 0, "story_points": 3, "rank": "101"},)},
        ),
        (
            "StoryReadinessRepairRequest",
            {"repairs": ({"story_id": 7, "story_points": 0, "rank": "101"},)},
        ),
        (
            "StoryReadinessRepairRequest",
            {"repairs": ({"story_id": 7, "story_points": 3, "rank": "  "},)},
        ),
        (
            "StoryReadinessRepairRequest",
            {
                "repairs": (
                    {"story_id": 7, "story_points": 3, "rank": "101"},
                    {"story_id": 7, "story_points": 5, "rank": "102"},
                )
            },
        ),
    ],
)
def test_planning_application_requests_reject_invalid_story_semantics(
    request_type_name: str,
    request_fields: dict[str, object],
) -> None:
    """Enforce IDs, points, ranks, and uniqueness at the application boundary."""
    request_type = cast(
        "type[BaseModel]", getattr(application_module, request_type_name)
    )

    with pytest.raises(ValidationError):
        request_type(
            project_id=PROJECT_ID,
            idempotency_key="invalid-planning-41",
            actor="operator",
            **request_fields,
        )


@pytest.mark.parametrize("rank", ["0", "-1", "1.1", "high", " 1", "01", "+1"])
def test_story_readiness_application_rejects_noncanonical_rank(rank: str) -> None:
    """Require one canonical positive base-10 rank before application mutation."""
    with pytest.raises(ValidationError, match="canonical positive base-10"):
        application_module.StoryReadinessRepairRequest.model_validate(
            {
                "project_id": PROJECT_ID,
                "repairs": ({"story_id": 7, "story_points": 3, "rank": rank},),
                "idempotency_key": "invalid-rank-41",
                "actor": "operator",
            }
        )


def test_planning_selection_derives_dependency_and_readiness_guards(
    engine: "Engine",
) -> None:
    """Bind dependency and repair semantics to exact current Story facts."""
    project_id = _seed_accepted_backlog(engine)
    domain = planning_domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    _select_for_sprint(engine, story_id)
    service_type = application_module.PlanningActionSelectionService
    service = service_type(engine=engine)
    position = domain.position(project_id)
    dependency_decision = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story_dependencies"
    )

    source_fingerprint = service.prepare_story_dependencies(
        project_id=project_id,
        decision=dependency_decision,
        selected_story_ids=(story_id,),
    )

    assert isinstance(source_fingerprint, str)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_points = 3
        story.rank = "0"
        session.add(story)
        session.commit()
    _validate_story_structurally(engine, story_id)
    position = domain.position(project_id)
    readiness_decision = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story_readiness"
    )

    readiness_target = service.prepare_story_readiness(
        project_id=project_id,
        decision=readiness_decision,
        repair_story_ids=(story_id,),
    )

    assert readiness_target is not None
    assert readiness_target[0] == (story_id,)
    assert isinstance(readiness_target[1], str)
    assert (
        service.prepare_story_readiness(
            project_id=project_id,
            decision=readiness_decision,
            repair_story_ids=(story_id + 1,),
        )
        is None
    )


def test_application_repairs_durable_invalid_story_rank_and_replays(
    engine: "Engine",
) -> None:
    """Repair a non-null invalid rank through the public application boundary."""
    project_id = _seed_accepted_backlog(engine)
    domain = planning_domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    _select_for_sprint(engine, story_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="application-readiness-dependencies",
    )
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_points = 3
        story.rank = "0"
        session.add(story)
        session.commit()
    _validate_story_structurally(engine, story_id)

    application = AgileForgeApplication(
        workflow_domain=domain,
        planning_action_selection=application_module.PlanningActionSelectionService(
            engine=engine
        ),
    )
    request = application_module.StoryReadinessRepairRequest(
        project_id=project_id,
        idempotency_key="application-repair-invalid-rank",
        actor="operator",
        repairs=(
            application_module.StoryReadinessRepair(
                story_id=story_id,
                story_points=5,
                rank="101",
            ),
        ),
    )

    applied = application.repair_story_readiness(request)

    assert applied.ok is True
    with Session(engine) as session:
        repaired_story = session.get(UserStory, story_id)
        assert repaired_story is not None
        assert (repaired_story.story_points, repaired_story.rank) == (5, "101")
        repaired_at = repaired_story.updated_at

    replayed = application.repair_story_readiness(request)

    assert replayed == applied.model_copy(update={"replayed": True})
    with Session(engine) as session:
        replayed_story = session.get(UserStory, story_id)
        assert replayed_story is not None
        assert (
            replayed_story.story_points,
            replayed_story.rank,
            replayed_story.updated_at,
        ) == (5, "101", repaired_at)


def test_planning_selection_derives_sprint_start_from_accepted_current_plan(
    engine: "Engine",
) -> None:
    """Resolve plan, Sprint, and candidate fingerprints from durable plan facts."""
    project_id = _seed_accepted_backlog(engine)
    domain = planning_domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate_fingerprint, _plan, plan_fingerprint = (
        _record_sprint_plan_draft(
            engine,
            domain,
            project_id,
            story_id,
            team_name="Planning Selection Team",
            idempotency_key="selection-sprint-plan",
        )
    )
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="selection-accept-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Plan is current.",
        )
    )
    assert accepted.ok is True
    application = AgileForgeApplication(
        workflow_domain=domain,
        planning_action_selection=application_module.PlanningActionSelectionService(
            engine=engine
        ),
    )
    started = application.start_sprint(
        application_module.SprintStartRequest(
            project_id=project_id,
            idempotency_key="selection-start-sprint",
            actor="operator",
        )
    )

    assert started.ok is True
    assert started.applied_node_id == "planning.sprint.start"


@dataclass(frozen=True)
class _DeliveryReviewCase:
    method_name: str
    request_type: type[BaseModel]
    internal_type: type[object]
    node_id: str
    request_kind: str
    fact_type: str
    identity_field: str
    fingerprint_field: str
    instance_key: str | None = None


@pytest.mark.parametrize(
    "case",
    [
        _DeliveryReviewCase(
            method_name="decide_backlog",
            request_type=BacklogReviewRequest,
            internal_type=DecideBacklog,
            node_id="backlog.review",
            request_kind="decide_backlog",
            fact_type="backlog",
            identity_field="backlog_artifact_id",
            fingerprint_field="artifact_fingerprint",
        ),
        _DeliveryReviewCase(
            method_name="decide_roadmap",
            request_type=RoadmapReviewRequest,
            internal_type=DecideRoadmap,
            node_id="planning.roadmap.review",
            request_kind="decide_roadmap",
            fact_type="roadmap",
            identity_field="roadmap_artifact_id",
            fingerprint_field="artifact_fingerprint",
        ),
        _DeliveryReviewCase(
            method_name="decide_story",
            request_type=StoryReviewRequest,
            internal_type=DecideStory,
            node_id="planning.story.review",
            request_kind="decide_story",
            fact_type="story",
            identity_field="story_artifact_id",
            fingerprint_field="artifact_fingerprint",
            instance_key="backlog_item:PBI-000007",
        ),
        _DeliveryReviewCase(
            method_name="decide_sprint_plan",
            request_type=SprintPlanReviewRequest,
            internal_type=DecideSprintPlan,
            node_id="planning.sprint.review",
            request_kind="decide_sprint_plan",
            fact_type="sprint_plan",
            identity_field="sprint_plan_artifact_id",
            fingerprint_field="plan_fingerprint",
        ),
    ],
)
def test_semantic_delivery_reviews_derive_internal_guards(
    case: _DeliveryReviewCase,
) -> None:
    """Build each retained internal review from one exact semantic selection."""
    decision = NodeDecision(
        node_id=case.node_id,
        instance_key=case.instance_key,
        child_graph_id="delivery",
        request_kind=case.request_kind,
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="DELIVERY_REVIEW_REQUIRED",
        decision_fingerprint=f"decision-{case.request_kind}",
        fact_references=(
            FactReference(
                fact_type=case.fact_type,
                fact_id=str(DELIVERY_ARTIFACT_ID),
                fingerprint=DELIVERY_ARTIFACT_FINGERPRINT,
            ),
        ),
    )
    domain = _CapturingTransitionDomain(_vision_position(decision))
    selection = _DeliveryReviewSelection(
        identity=(DELIVERY_ARTIFACT_ID, DELIVERY_ARTIFACT_FINGERPRINT)
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        delivery_review_selection=selection,
    )
    request_values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "decision": "accepted",
        "rationale": "  Reviewed current artifact.  ",
        "idempotency_key": f"{case.request_kind}-41",
        "actor": "operator",
    }
    result = getattr(application, case.method_name)(
        case.request_type.model_validate(request_values),
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint=f"decision-{case.request_kind}",
            instance_key=case.instance_key,
        ),
    )

    assert result.ok is True
    assert domain.position_calls == [PROJECT_ID]
    assert len(domain.requests) == 1
    guarded = domain.requests[0]
    assert isinstance(guarded, case.internal_type)
    assert getattr(guarded, case.identity_field) == DELIVERY_ARTIFACT_ID
    assert getattr(guarded, case.fingerprint_field) == DELIVERY_ARTIFACT_FINGERPRINT
    assert (
        cast(
            "DecideBacklog | DecideRoadmap | DecideStory | DecideSprintPlan",
            guarded,
        ).rationale
        == "Reviewed current artifact."
    )
    assert selection.identity_calls == [
        (PROJECT_ID, f"decision-{case.request_kind}", case.fact_type)
    ]
    assert selection.replay_queries[0].operator_input == {
        "decision": "accepted",
        "rationale": "Reviewed current artifact.",
        **(
            {"instance_key": case.instance_key} if case.instance_key is not None else {}
        ),
    }


def test_semantic_application_resolves_bootstrap_guards_once() -> None:
    """Prepare a zero-input bootstrap attempt from one position read."""
    decision = _vision_bootstrap_decision("decision-vision")
    domain = _BoundaryDomain(_vision_position(decision))
    application = _CapturingApplication(domain)

    result = application.bootstrap_vision(
        VisionBootstrapRequest(
            project_id=PROJECT_ID,
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
    assert guarded.node_id == "vision.bootstrap"
    assert guarded.input_payload["operation"] == "bootstrap"


def test_semantic_application_rejects_ambiguous_bootstrap_decisions() -> None:
    """Return a structured conflict instead of choosing between decisions."""
    domain = _BoundaryDomain(
        _vision_position(
            _vision_bootstrap_decision("decision-a"),
            _vision_bootstrap_decision("decision-b"),
        )
    )
    application = _CapturingApplication(domain)

    result = application.bootstrap_vision(
        VisionBootstrapRequest(
            project_id=PROJECT_ID,
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
        vision_input=vision_input,
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


def test_semantic_vision_response_forwards_graph_selected_instance_key() -> None:
    """Start the exact repeated Vision interview instance selected by the graph."""
    decision = _vision_decision(
        "decision-vision-after-turn-1",
        instance_key="after-turn:1",
    )
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
    assert len(application.agent_requests) == 1
    assert application.agent_requests[0].instance_key == "after-turn:1"


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


def test_semantic_product_goal_response_forwards_graph_selected_instance_key() -> None:
    """Start the exact repeated Goal interview instance selected by the graph."""
    decision = _product_goal_decision(
        "decision-goal-after-turn-1",
        instance_key="after-turn:1",
    )
    domain = _BoundaryDomain(_vision_position(decision))
    application = _CapturingProductGoalApplication(domain)

    result = application.respond_to_product_goal(
        ProductGoalResponseRequest(
            project_id=PROJECT_ID,
            text="Deliver one measurable outcome.",
            idempotency_key="goal-41",
            actor="operator",
        )
    )

    assert result.ok is True
    assert len(application.agent_requests) == 1
    assert application.agent_requests[0].instance_key == "after-turn:1"


def test_semantic_vision_review_rejects_a_replaced_candidate() -> None:
    """A browser review cannot decide the replacement for the Vision it saw."""
    current_fingerprint = "sha256:vision-current"
    decision = NodeDecision(
        node_id="vision.review",
        child_graph_id="vision",
        request_kind="decide_vision_review",
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_REVIEW_REQUIRED",
        decision_fingerprint="decision-vision-review",
        fact_references=(
            FactReference(
                fact_type="vision",
                fact_id="17",
                fingerprint=current_fingerprint,
            ),
        ),
    )
    domain = _CapturingTransitionDomain(_vision_position(decision))
    application = AgileForgeApplication(
        workflow_domain=domain,
        vision_input=_VisionInput(),
    )

    result = application.review_vision(
        VisionReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Reviewed the candidate shown in the browser.",
            expected_candidate_fingerprint="sha256:vision-replaced",
            idempotency_key="vision-review-stale-41",
            actor="dashboard-ui",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert domain.requests == []


def test_semantic_product_goal_review_rejects_a_replaced_candidate() -> None:
    """A stale Product Goal review fails without deciding the current candidate."""
    decision = NodeDecision(
        node_id="goal.review",
        child_graph_id="product_goal",
        request_kind="decide_product_goal_review",
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="PRODUCT_GOAL_REVIEW_REQUIRED",
        decision_fingerprint="decision-goal-review",
        fact_references=(
            FactReference(
                fact_type="product_goal",
                fact_id="21",
                fingerprint="sha256:goal-current",
            ),
        ),
    )
    domain = _CapturingTransitionDomain(_vision_position(decision))
    application = AgileForgeApplication(
        workflow_domain=domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=_ProductGoalInput(TransitionResult(ok=True)),
        ),
    )

    result = application.review_product_goal(
        ProductGoalReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Reviewed the candidate shown in the browser.",
            expected_candidate_fingerprint="sha256:goal-replaced",
            idempotency_key="goal-review-stale-41",
            actor="dashboard-ui",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert domain.requests == []


def test_semantic_specification_review_rejects_a_replaced_candidate() -> None:
    """A stale Specification review cannot select a newer candidate."""
    decision = NodeDecision(
        node_id="specification.review",
        child_graph_id="specification",
        request_kind="decide_specification",
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPECIFICATION_REVIEW_REQUIRED",
        decision_fingerprint="decision-specification-review",
        fact_references=(
            FactReference(
                fact_type="specification_candidate",
                fact_id="31",
                fingerprint="sha256:specification-current",
            ),
        ),
    )
    domain = _CapturingTransitionDomain(_vision_position(decision))
    application = AgileForgeApplication(
        workflow_domain=domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=_ProductGoalInput(TransitionResult(ok=True)),
        ),
    )

    result = application.review_specification(
        SpecificationReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Reviewed the candidate shown in the browser.",
            expected_candidate_fingerprint="sha256:specification-replaced",
            idempotency_key="specification-review-stale-41",
            actor="dashboard-ui",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert domain.requests == []


def test_semantic_specification_review_replay_binds_exact_candidate(
    engine: "Engine",
) -> None:
    """A prior decision cannot be replayed onto a replacement candidate."""
    stored = DecideSpecification(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-specification-review",
        decision_fingerprint="decision-specification-review",
        idempotency_key="specification-review-41",
        actor="operator",
        specification_candidate_id=31,
        candidate_fingerprint="sha256:specification-a",
        decision="accepted",
        rationale="Specification is complete.",
    )
    persisted = TransitionResult(ok=True, applied_node_id="specification.review")
    _store_completed_receipt(engine, stored, persisted)
    domain = _BoundaryDomain(_vision_position())
    application = AgileForgeApplication(
        workflow_domain=domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=ProductGoalInterviewInputService(engine=engine),
        ),
    )

    result = application.review_specification(
        SpecificationReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Specification is complete.",
            expected_candidate_fingerprint="sha256:specification-b",
            idempotency_key="specification-review-41",
            actor="operator",
        )
    )

    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
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


def test_transition_replay_compares_structured_semantics_in_json_form(
    engine: "Engine",
) -> None:
    """Replay tuple/model semantics exactly and conflict on changed dependency edges."""
    stored = ApplyStoryDependencies(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-dependencies",
        decision_fingerprint="decision-dependencies",
        idempotency_key="dependencies-replay-41",
        actor="operator",
        selected_story_ids=(7, 9),
        reviewed_edges=(
            ReviewedDependencyEdge(
                dependent_story_id=9,
                prerequisite_story_id=7,
                reason="Story 9 requires Story 7.",
            ),
        ),
        source_fingerprint="dependency-source-current",
    )
    persisted = TransitionResult(ok=True, applied_node_id="planning.story_dependencies")
    _store_completed_receipt(engine, stored, persisted)
    service = DurableTransitionReplayService(engine=engine)
    query = TransitionReplayQuery(
        request_kind="apply_story_dependencies",
        project_id=PROJECT_ID,
        idempotency_key="dependencies-replay-41",
        actor="operator",
        operator_input={
            "selected_story_ids": [7, 9],
            "reviewed_edges": [
                {
                    "dependent_story_id": 9,
                    "prerequisite_story_id": 7,
                    "reason": "Story 9 requires Story 7.",
                }
            ],
        },
    )

    replay = service.replay(query)
    conflict = service.replay(
        query.model_copy(
            update={
                "operator_input": {
                    **query.operator_input,
                    "reviewed_edges": [],
                }
            }
        )
    )

    assert replay == persisted.model_copy(update={"replayed": True})
    assert conflict is not None
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


@pytest.mark.parametrize(
    ("method_name", "request_type", "stored", "instance_key"),
    [
        (
            "decide_backlog",
            BacklogReviewRequest,
            DecideBacklog(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-backlog-review",
                decision_fingerprint="decision-backlog-review",
                idempotency_key="backlog-review-replay",
                actor="operator",
                backlog_artifact_id=7,
                artifact_fingerprint="sha256:backlog-7",
                decision="accepted",
                rationale="Reviewed artifact.",
            ),
            None,
        ),
        (
            "decide_roadmap",
            RoadmapReviewRequest,
            DecideRoadmap(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-roadmap-review",
                decision_fingerprint="decision-roadmap-review",
                idempotency_key="roadmap-review-replay",
                actor="operator",
                roadmap_artifact_id=8,
                artifact_fingerprint="sha256:roadmap-8",
                decision="accepted",
                rationale="Reviewed artifact.",
            ),
            None,
        ),
        (
            "decide_story",
            StoryReviewRequest,
            DecideStory(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-story-review",
                decision_fingerprint="decision-story-review",
                instance_key="backlog_item:PBI-000001",
                idempotency_key="story-review-replay",
                actor="operator",
                backlog_item_id="PBI-000001",
                story_artifact_id=9,
                artifact_fingerprint="sha256:story-9",
                decision="accepted",
                rationale="Reviewed artifact.",
            ),
            "backlog_item:PBI-000001",
        ),
        (
            "decide_sprint_plan",
            SprintPlanReviewRequest,
            DecideSprintPlan(
                project_id=PROJECT_ID,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="facts-sprint-review",
                decision_fingerprint="decision-sprint-review",
                idempotency_key="sprint-review-replay",
                actor="operator",
                sprint_plan_artifact_id=10,
                plan_fingerprint="sha256:sprint-10",
                decision="accepted",
                rationale="Reviewed artifact.",
            ),
            None,
        ),
    ],
)
def test_semantic_delivery_review_replays_before_advanced_position(
    engine: "Engine",
    method_name: str,
    request_type: type[BaseModel],
    stored: TransitionRequest,
    instance_key: str | None,
) -> None:
    """Replay all four exact semantic review retries before position reads."""
    persisted = TransitionResult(
        ok=True,
        applied_node_id=cast("PositionedRequest", stored).decision_node_id(),
    )
    _store_completed_receipt(engine, stored, persisted)
    domain = _BoundaryDomain(_vision_position())
    application = AgileForgeApplication(
        workflow_domain=domain,
        delivery_review_selection=DeliveryReviewSelectionService(engine=engine),
    )
    request_values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "decision": "accepted",
        "rationale": "  Reviewed artifact.  ",
        "idempotency_key": stored.idempotency_key,
        "actor": "operator",
    }
    result = getattr(application, method_name)(
        request_type.model_validate(request_values),
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint=cast("PositionedRequest", stored).decision_fingerprint,
            instance_key=instance_key,
        ),
    )

    assert result == persisted.model_copy(update={"replayed": True})
    assert domain.position_calls == []


def test_semantic_delivery_review_changed_operator_input_conflicts(
    engine: "Engine",
) -> None:
    """Bind same-key reuse to decision, rationale, and exact Story selector."""
    backlog = DecideBacklog(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-backlog-review",
        decision_fingerprint="decision-backlog-review",
        idempotency_key="backlog-review-conflict",
        actor="operator",
        backlog_artifact_id=7,
        artifact_fingerprint="sha256:backlog-7",
        decision="accepted",
        rationale="Reviewed artifact.",
    )
    story = DecideStory(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-story-review",
        decision_fingerprint="decision-story-review",
        instance_key="backlog_item:PBI-000001",
        idempotency_key="story-review-conflict",
        actor="operator",
        backlog_item_id="PBI-000001",
        story_artifact_id=9,
        artifact_fingerprint="sha256:story-9",
        decision="accepted",
        rationale="Reviewed artifact.",
    )
    persisted = TransitionResult(ok=True)
    _store_completed_receipt(engine, backlog, persisted)
    _store_completed_receipt(engine, story, persisted)
    domain = _BoundaryDomain(_vision_position())
    application = AgileForgeApplication(
        workflow_domain=domain,
        delivery_review_selection=DeliveryReviewSelectionService(engine=engine),
    )

    changed_decision = application.decide_backlog(
        BacklogReviewRequest(
            project_id=PROJECT_ID,
            decision="rejected",
            rationale="Reviewed artifact.",
            idempotency_key=backlog.idempotency_key,
            actor="operator",
        ),
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint=backlog.decision_fingerprint,
            instance_key=None,
        ),
    )
    changed_rationale = application.decide_backlog(
        BacklogReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Different rationale.",
            idempotency_key=backlog.idempotency_key,
            actor="operator",
        ),
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint=backlog.decision_fingerprint,
            instance_key=None,
        ),
    )
    changed_selector = application.decide_story(
        StoryReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Reviewed artifact.",
            idempotency_key=story.idempotency_key,
            actor="operator",
        ),
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint=story.decision_fingerprint,
            instance_key="backlog_item:PBI-000002",
        ),
    )
    removed_selector = DurableTransitionReplayService(engine=engine).replay(
        TransitionReplayQuery(
            request_kind="decide_story",
            project_id=PROJECT_ID,
            idempotency_key=story.idempotency_key,
            actor="operator",
            operator_input={
                "decision": "accepted",
                "rationale": "Reviewed artifact.",
            },
        )
    )

    for result in (
        changed_decision,
        changed_rationale,
        changed_selector,
        removed_selector,
    ):
        assert result is not None
        assert result.error is not None
        assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert domain.position_calls == []


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
            "backlog_item:PBI-000001",
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
            instance_key="backlog_item:PBI-000002",
            idempotency_key="story-shared-key",
            actor="operator",
        )
    )

    assert result == conflict
    assert domain.position_calls == []
    assert delivery_input.replay_queries[0].instance_key == "backlog_item:PBI-000002"
    assert application.agent_requests == []


def test_story_generation_rejects_omitted_selector_before_wildcard_selection() -> None:
    """Do not select the sole Story generation decision without its selector."""
    decision = _delivery_decision(
        node_id="planning.story.generate",
        request_kind="record_story_draft",
        instance_key="backlog_item:PBI-000001",
    )
    domain = _BoundaryDomain(_vision_position(decision))
    delivery_input = _DeliveryInput({"prepared_by": "host"})
    application = _CapturingDeliveryApplication(domain, delivery_input)

    result = application.generate_story(
        DeliveryActionRequest(
            project_id=PROJECT_ID,
            idempotency_key="story-without-selector",
            actor="operator",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE
    assert domain.position_calls == []
    assert delivery_input.build_calls == []


def test_sprint_generation_fails_closed_without_host_capacity_input(
    engine: "Engine",
) -> None:
    """Require explicit or durable metrics capacity before model execution."""
    domain, project_id, story_id = _sprint_ready_project(engine)
    application = _CapturingSprintApplication(
        domain,
        SprintPlanningInputService(engine=engine),
    )
    request = SprintPlanningRequest(
        project_id=project_id,
        selected_story_ids=(story_id,),
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
    _artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    _select_for_sprint(engine, story_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="review-sprint-dependencies",
    )
    return domain, project_id, story_id


def test_delivery_review_selection_verifies_each_durable_artifact(
    engine: "Engine",
) -> None:
    """Resolve graph references only when their durable artifact rows match."""
    project_id = _seed_accepted_backlog(engine)
    domain = planning_domain(engine)
    roadmap_id = _record_and_accept_roadmap(domain, project_id)
    story_artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    sprint_plan_id, _sprint_id, _candidate_fingerprint, _plan = (
        _record_sprint_plan_draft(
            engine,
            domain,
            project_id,
            story_id,
            team_name="Delivery Review Team",
            idempotency_key="record-delivery-review-sprint",
        )
    )
    with Session(engine) as session:
        backlog = session.exec(
            select(BacklogArtifact).where(BacklogArtifact.project_id == project_id)
        ).one()
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        story = session.get(StoryArtifact, story_artifact_id)
        sprint_plan = session.get(SprintPlanArtifact, sprint_plan_id)
    assert backlog.backlog_artifact_id is not None
    assert roadmap is not None
    assert story is not None
    assert sprint_plan is not None
    rows = (
        (
            "backlog",
            backlog.backlog_artifact_id,
            backlog.content_fingerprint,
            None,
        ),
        ("roadmap", roadmap_id, roadmap.content_fingerprint, None),
        (
            "story",
            story_artifact_id,
            story.content_fingerprint,
            f"backlog_item:{story.backlog_item_id}",
        ),
        ("sprint_plan", sprint_plan_id, sprint_plan.plan_fingerprint, None),
    )
    selection = DeliveryReviewSelectionService(engine=engine)

    for fact_type, artifact_id, fingerprint, instance_key in rows:
        decision = NodeDecision(
            node_id=f"test.{fact_type}",
            instance_key=instance_key,
            child_graph_id="delivery",
            request_kind=f"decide_{fact_type}",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="DELIVERY_REVIEW_REQUIRED",
            decision_fingerprint=f"decision-{fact_type}",
            fact_references=(
                FactReference(
                    fact_type=fact_type,
                    fact_id=str(artifact_id),
                    fingerprint=fingerprint,
                ),
            ),
        )
        assert selection.review_identity(
            project_id=project_id,
            decision=decision,
            fact_type=fact_type,
        ) == (artifact_id, fingerprint)

    story_decision = NodeDecision(
        node_id="planning.story.review",
        instance_key="backlog_item:wrong",
        child_graph_id="planning",
        request_kind="decide_story",
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="STORY_REVIEW_REQUIRED",
        decision_fingerprint="decision-story-wrong",
        fact_references=(
            FactReference(
                fact_type="story",
                fact_id=str(story_artifact_id),
                fingerprint=story.content_fingerprint,
            ),
        ),
    )
    assert (
        selection.review_identity(
            project_id=project_id,
            decision=story_decision,
            fact_type="story",
        )
        is None
    )


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
    assert envelope["guidance"] == "Prioritize durable replay."
    assert isinstance(envelope["candidate_set_fingerprint"], str)


def test_completed_sprint_metrics_supply_host_capacity(engine: "Engine") -> None:
    """Use completed Story points when current durable metrics recommend capacity."""
    domain, project_id, _sprint_id, _story_id, future_story_id, *_rest = (
        _complete_execution_sprint_with_unselected_story(engine)
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=EXECUTION_EVALUATED_AT),
    )
    completed_sprint_id = _sprint_id
    triaged = domain.transition(
        RecordPostSprintTriage(
            **execution_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{completed_sprint_id}",
            ),
            instance_key=f"sprint:{completed_sprint_id}",
            idempotency_key="metrics-capacity-triage",
            sprint_id=completed_sprint_id,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    )
    assert triaged.ok is True
    _select_for_sprint(engine, future_story_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="metrics-capacity-future-story-dependencies",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    assert [
        story.story_id for story in snapshot.stories if story.sprint_candidate
    ] == [future_story_id]
    backlog = next(
        item
        for item in snapshot.phase_artifacts
        if item.artifact_type == "backlog" and item.status == "accepted"
    )
    assert backlog.spec_version_id is not None
    assert backlog.spec_hash is not None
    assert backlog.product_goal_artifact_id is not None
    assert backlog.product_goal_fingerprint is not None
    candidates = tuple(item for item in snapshot.stories if item.sprint_candidate)
    decision = _delivery_decision(
        node_id="planning.sprint.plan",
        request_kind="record_sprint_plan",
    ).model_copy(
        update={
            "fact_references": (
                FactReference(
                    fact_type="specification",
                    fact_id=str(backlog.spec_version_id),
                    fingerprint=backlog.spec_hash,
                ),
                FactReference(
                    fact_type="product_goal",
                    fact_id=str(backlog.product_goal_artifact_id),
                    fingerprint=backlog.product_goal_fingerprint,
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


def test_completed_triaged_sprint_exposes_future_dependency_review(
    engine: "Engine",
) -> None:
    """Reopen dependency review only for the next active selected Story scope."""
    domain, project_id, sprint_id, _story_id, future_story_id, *_rest = (
        _complete_execution_sprint_with_unselected_story(engine)
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=EXECUTION_EVALUATED_AT),
    )
    triaged = domain.transition(
        RecordPostSprintTriage(
            **execution_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="future-scope-dependency-triage",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    )
    assert triaged.ok is True
    _select_for_sprint(engine, future_story_id)

    decisions = tuple(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "planning.story_dependencies"
    )
    assert decisions, domain.position(project_id).model_dump(mode="json")
    decision = decisions[0]

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "STORY_DEPENDENCY_REVIEW_REQUIRED"


def test_next_sprint_dependency_apply_uses_only_projected_current_scope(
    engine: "Engine",
) -> None:
    """Completed-Sprint intent stays historical while only the future scope writes."""
    domain, project_id, sprint_id, completed_story_id, future_story_id, *_rest = (
        _complete_execution_sprint_with_unselected_story(engine)
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=EXECUTION_EVALUATED_AT),
    )
    triaged = domain.transition(
        RecordPostSprintTriage(
            **execution_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="next-scope-authority-triage",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    )
    assert triaged.ok is True
    _select_for_sprint(engine, future_story_id)

    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story_dependencies"
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    current_scope = selected_scope_stories(snapshot)
    assert [item.story_id for item in current_scope] == [future_story_id]
    completed_fact = next(
        item for item in snapshot.stories if item.story_id == completed_story_id
    )
    assert completed_fact.sprint_selection_state == "selected"

    selection = application_module.PlanningActionSelectionService(engine=engine)
    source_fingerprint = selection.prepare_story_dependencies(
        project_id=project_id,
        decision=decision,
        selected_story_ids=(future_story_id,),
    )
    assert source_fingerprint == story_dependency_source_fingerprint(current_scope)
    assert (
        selection.prepare_story_dependencies(
            project_id=project_id,
            decision=decision,
            selected_story_ids=(completed_story_id, future_story_id),
        )
        is None
    )

    application = AgileForgeApplication(
        workflow_domain=domain,
        read_projection=DurableReadProjectionService(engine=engine),
        planning_action_selection=selection,
    )
    with patch("api._application", return_value=application):
        response = TestClient(api_module.app).post(
            f"/api/projects/{project_id}/story/dependencies/apply",
            json={
                "selected_story_ids": [future_story_id],
                "reviewed_edges": [],
                "idempotency_key": "next-scope-authority-apply",
                "actor": "operator@example.com",
            },
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"]["selected_story_ids"] == [future_story_id]


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
    assert result.error.blockers[0].code == "SPRINT_CANDIDATE_SET_STALE"
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
            if reference.fact_type in {"specification", "backlog", "product_goal"}
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

    assert isinstance(backlog_payload, dict)
    assert isinstance(roadmap_payload, dict)
    assert isinstance(story_payload, dict)
    assert (
        BacklogBuilderInput.model_validate(
            backlog_payload["builder_input"]
        ).prior_backlog_state
        != "NO_HISTORY"
    )
    assert RoadmapBuilderInput.model_validate(
        roadmap_payload["builder_input"]
    ).backlog_items
    assert (
        UserStoryWriterInput.model_validate(
            story_payload["writer_input"]
        ).parent_backlog_item_id
        == "PBI-000001"
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
    "record_backlog_draft": "backlog.generate",
    "record_roadmap_draft": "planning.roadmap.generate",
    "record_story_draft": "planning.story.generate",
}


_REQUEST_KIND_FACT_REFERENCE_ROWS = {
    "complete_task": (("task", "7", "task-7"),),
    "close_story": (("story_completion", "9", "story-completion-9"),),
    "review_sprint": (("sprint_review", "31", "sprint-review-31"),),
    "close_sprint": (
        ("sprint", "31", "sprint-31"),
        ("sprint_review", "31", "sprint-review-31"),
        ("sprint_close", "31", "sprint-close-31"),
    ),
    "record_post_sprint_triage": (("sprint_closure", "31", "sprint-close-31"),),
    "apply_story_dependencies": (
        ("story_dependency_source", "41", "dependency-source-41"),
    ),
    "repair_story_readiness": (("story_readiness", "41", "readiness-41"),),
    "start_sprint": (
        ("sprint_plan", "29", "plan-29"),
        ("candidate_set", "41", "candidates-41"),
        ("sprint_plan_tasks", "31", "tasks-31"),
    ),
}


def _request_kind_fact_references(kind: str) -> tuple[FactReference, ...]:
    return tuple(
        FactReference(
            fact_type=fact_type,
            fact_id=fact_id,
            fingerprint=fingerprint,
        )
        for fact_type, fact_id, fingerprint in _REQUEST_KIND_FACT_REFERENCE_ROWS.get(
            kind,
            (),
        )
    )


def _request_kind_instance_key(kind: str, index: int) -> str:
    if kind == "complete_task":
        return "task:7"
    if kind == "close_story":
        return "story:9"
    if kind in {"review_sprint", "close_sprint", "record_post_sprint_triage"}:
        return "sprint:31"
    return f"instance:{index}"


def _all_request_kinds_position() -> WorkflowPosition:
    decisions = tuple(
        NodeDecision(
            node_id=_AGENTIC_NODE_IDS.get(kind, f"test.{index}"),
            instance_key=_request_kind_instance_key(kind, index),
            child_graph_id="test",
            request_kind=kind,
            category=NodeCategory.AVAILABLE,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="TEST_AVAILABLE",
            decision_fingerprint=f"decision-{index}",
            fact_references=_request_kind_fact_references(kind),
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


def test_api_adapter_does_not_import_legacy_routing() -> None:
    """Keep the production API free of old routing imports."""
    source = (Path(__file__).parents[2] / "api.py").read_text()
    assert "from services.workflow import WorkflowService" not in source
    assert "ReadOnlySessionReader" not in source


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
    ("path", "method_name", "extra"),
    [
        ("/api/projects/41/backlog/generate", "generate_backlog", {}),
        ("/api/projects/41/roadmap/generate", "generate_roadmap", {}),
        (
            "/api/projects/41/story/generate",
            "generate_story",
            {"instance_key": "backlog_item:PBI-000007"},
        ),
    ],
)
def test_retained_delivery_api_calls_host_prepared_application_method(
    path: str,
    method_name: str,
    extra: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route metadata-only delivery requests through semantic methods."""
    application = _FakeApiApplication(position=_all_request_kinds_position())
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        path,
        json={"idempotency_key": "delivery-41", "actor": "operator", **extra},
    )

    assert response.status_code == HTTPStatus.OK
    assert len(application.requests) == 1
    assert isinstance(application.requests[0], DeliveryActionRequest)
    assert method_name.startswith("generate_")


@pytest.mark.parametrize(
    ("path", "payload", "headers"),
    [
        (
            "/api/projects/41/story/generate",
            {"idempotency_key": "story-generate-41", "actor": "operator"},
            {},
        ),
        (
            "/api/projects/41/story/generate",
            {
                "instance_key": "  ",
                "idempotency_key": "story-generate-41",
                "actor": "operator",
            },
            {},
        ),
        (
            "/api/projects/41/story/decide",
            {
                "decision": "accepted",
                "rationale": "Reviewed current Story.",
                "idempotency_key": "story-review-41",
                "actor": "operator",
            },
            {"X-AgileForge-Expected-Decision": "decision-shown"},
        ),
        (
            "/api/projects/41/story/decide",
            {
                "instance_key": "  ",
                "decision": "accepted",
                "rationale": "Reviewed current Story.",
                "idempotency_key": "story-review-41",
                "actor": "operator",
            },
            {
                "X-AgileForge-Expected-Decision": "decision-shown",
                "X-AgileForge-Expected-Instance": "backlog_item:PBI-000007",
            },
        ),
    ],
)
def test_story_api_rejects_missing_or_caller_owned_selector(
    path: str,
    payload: dict[str, object],
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject missing generated-story selectors and caller-owned review selectors."""
    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=_all_request_kinds_position()),
    )

    response = TestClient(api_module.app).post(path, headers=headers, json=payload)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "request_type_name", "extra"),
    [
        ("/api/projects/41/backlog/decide", "BacklogReviewRequest", {}),
        ("/api/projects/41/roadmap/decide", "RoadmapReviewRequest", {}),
        (
            "/api/projects/41/story/decide",
            "StoryReviewRequest",
            {},
        ),
        ("/api/projects/41/sprint/decide", "SprintPlanReviewRequest", {}),
    ],
)
def test_delivery_review_api_uses_task_specific_semantic_request(
    path: str,
    request_type_name: str,
    extra: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route each review without exposing artifact identity or fingerprints."""
    application = _FakeApiApplication(position=_all_request_kinds_position())
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        path,
        headers=_planning_review_headers(path),
        json={
            "decision": "accepted",
            "rationale": "  Reviewed current artifact.  ",
            "idempotency_key": "delivery-review-41",
            "actor": "operator",
            **extra,
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert len(application.requests) == 1
    request = application.requests[0]
    assert type(request).__name__ == request_type_name
    review_request = cast("DeliveryReviewRequest", request)
    assert review_request.rationale == "Reviewed current artifact."
    assert not hasattr(request, "artifact_fingerprint")
    assert not hasattr(request, "plan_fingerprint")


@pytest.mark.parametrize(
    ("path", "base_payload", "internal_field"),
    [
        (
            "/api/projects/41/backlog/decide",
            {},
            "backlog_artifact_id",
        ),
        (
            "/api/projects/41/backlog/decide",
            {},
            "artifact_fingerprint",
        ),
        (
            "/api/projects/41/roadmap/decide",
            {},
            "roadmap_artifact_id",
        ),
        (
            "/api/projects/41/story/decide",
            {},
            "story_artifact_id",
        ),
        (
            "/api/projects/41/story/decide",
            {},
            "semantic_input",
        ),
        (
            "/api/projects/41/sprint/decide",
            {},
            "sprint_plan_artifact_id",
        ),
        (
            "/api/projects/41/sprint/decide",
            {},
            "plan_fingerprint",
        ),
        (
            "/api/projects/41/sprint/decide",
            {},
            "decision_fingerprint",
        ),
    ],
)
def test_delivery_review_api_rejects_caller_owned_guards(
    path: str,
    base_payload: dict[str, object],
    internal_field: str,
) -> None:
    """Reject artifact IDs, all fingerprints, and generic semantic envelopes."""
    response = TestClient(api_module.app).post(
        path,
        headers=_planning_review_headers(path),
        json={
            "decision": "accepted",
            "rationale": "Reviewed current artifact.",
            "idempotency_key": "delivery-review-41",
            "actor": "operator",
            **base_payload,
            internal_field: "caller-owned",
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "payload", "request_type_name"),
    [
        (
            "/api/projects/41/story/dependencies/apply",
            {
                "selected_story_ids": [7, 9],
                "reviewed_edges": [
                    {
                        "dependent_story_id": 9,
                        "prerequisite_story_id": 7,
                        "reason": "Story 9 requires Story 7.",
                    }
                ],
            },
            "StoryDependenciesApplyRequest",
        ),
        (
            "/api/projects/41/story/readiness/repair",
            {
                "repairs": [
                    {"story_id": 7, "story_points": 3, "rank": "101"},
                    {"story_id": 9, "story_points": 5, "rank": "102"},
                ]
            },
            "StoryReadinessRepairRequest",
        ),
        (
            "/api/projects/41/sprint/start",
            {},
            "SprintStartRequest",
        ),
    ],
)
def test_planning_action_api_uses_task_specific_semantic_requests(
    path: str,
    payload: dict[str, object],
    request_type_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route operator semantics without caller-owned durable identities."""
    application = _FakeApiApplication(position=_all_request_kinds_position())
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        path,
        json={
            **payload,
            "idempotency_key": "planning-action-41",
            "actor": "operator",
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert len(application.requests) == 1
    request = application.requests[0]
    assert type(request).__name__ == request_type_name
    assert not hasattr(request, "graph_version")
    assert not hasattr(request, "fact_fingerprint")
    assert not hasattr(request, "decision_fingerprint")


@pytest.mark.parametrize(
    ("path", "payload", "internal_field", "value"),
    [
        (
            "/api/projects/41/story/dependencies/apply",
            {"selected_story_ids": [7], "reviewed_edges": []},
            "source_fingerprint",
            "caller-owned",
        ),
        (
            "/api/projects/41/story/dependencies/apply",
            {"selected_story_ids": [7], "reviewed_edges": []},
            "graph_version",
            "caller-owned",
        ),
        (
            "/api/projects/41/story/readiness/repair",
            {"repairs": [{"story_id": 7, "story_points": 3, "rank": "101"}]},
            "story_ids",
            [7],
        ),
        (
            "/api/projects/41/story/readiness/repair",
            {"repairs": [{"story_id": 7, "story_points": 3, "rank": "101"}]},
            "expected_readiness_fingerprint",
            "caller-owned",
        ),
        ("/api/projects/41/sprint/start", {}, "sprint_plan_artifact_id", 29),
        ("/api/projects/41/sprint/start", {}, "plan_fingerprint", "caller-owned"),
        ("/api/projects/41/sprint/start", {}, "candidate_set_fingerprint", "owned"),
    ],
)
def test_planning_action_api_rejects_internal_fields(
    path: str,
    payload: dict[str, object],
    internal_field: str,
    value: object,
) -> None:
    """Return 422 for generic envelopes, guards, fingerprints, and artifact IDs."""
    response = TestClient(api_module.app).post(
        path,
        json={
            **payload,
            "idempotency_key": "planning-action-41",
            "actor": "operator",
            internal_field: value,
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/projects/41/story/dependencies/apply",
            {"selected_story_ids": [0], "reviewed_edges": []},
        ),
        (
            "/api/projects/41/story/dependencies/apply",
            {"selected_story_ids": [7, 7], "reviewed_edges": []},
        ),
        (
            "/api/projects/41/story/readiness/repair",
            {"repairs": [{"story_id": 0, "story_points": 3, "rank": "101"}]},
        ),
        (
            "/api/projects/41/story/readiness/repair",
            {"repairs": [{"story_id": 7, "story_points": 0, "rank": "101"}]},
        ),
        (
            "/api/projects/41/story/readiness/repair",
            {"repairs": [{"story_id": 7, "story_points": 3, "rank": "  "}]},
        ),
        (
            "/api/projects/41/story/readiness/repair",
            {
                "repairs": [
                    {"story_id": 7, "story_points": 3, "rank": "101"},
                    {"story_id": 7, "story_points": 5, "rank": "102"},
                ]
            },
        ),
    ],
)
def test_planning_action_api_rejects_invalid_story_semantics(
    path: str,
    payload: dict[str, object],
) -> None:
    """Reject invalid IDs, points, ranks, and duplicate semantic selections."""
    response = TestClient(api_module.app).post(
        path,
        json={
            **payload,
            "idempotency_key": "planning-action-41",
            "actor": "operator",
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "payload", "request_type_name"),
    [
        (
            "/api/projects/41/sprint/task/complete",
            {
                "instance_key": "task:7",
                "outcome_summary": "Implemented semantic execution.",
                "artifact_refs": ["services/application.py"],
                "acceptance_result": "fully_met",
                "checklist_result": {"Focused tests": "passed"},
            },
            "CompleteTaskRequest",
        ),
        (
            "/api/projects/41/story/close",
            {
                "instance_key": "story:9",
                "resolution": "Completed",
                "delivered": "Semantic execution transport.",
                "evidence": "Focused tests pass.",
                "known_gaps": "None.",
            },
            "CloseStoryRequest",
        ),
        (
            "/api/projects/41/sprint/review",
            {"instance_key": "sprint:31"},
            "SprintReviewRequest",
        ),
        (
            "/api/projects/41/sprint/close",
            {"instance_key": "sprint:31"},
            "SprintCloseRequest",
        ),
        (
            "/api/projects/41/sprint/triage",
            {
                "instance_key": "sprint:31",
                "impact": "backlog",
                "canonical_payload": {"summary": "Follow-up required."},
            },
            "PostSprintTriageRequest",
        ),
    ],
)
def test_execution_action_api_uses_strict_semantic_requests(
    path: str,
    payload: dict[str, object],
    request_type_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route execution semantics without caller-owned IDs or fingerprints."""
    application = _FakeApiApplication(position=_all_request_kinds_position())
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        path,
        json={
            **payload,
            "idempotency_key": "execution-action-41",
            "actor": "operator",
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert len(application.requests) == 1
    request = application.requests[0]
    assert type(request).__name__ == request_type_name
    assert not hasattr(request, "graph_version")
    assert not hasattr(request, "fact_fingerprint")
    assert not hasattr(request, "decision_fingerprint")
    assert not hasattr(request, "sprint_id")


@pytest.mark.parametrize(
    ("path", "payload", "internal_field", "value"),
    [
        (
            "/api/projects/41/sprint/task/complete",
            {
                "instance_key": "task:7",
                "outcome_summary": "Implemented semantic execution.",
                "artifact_refs": ["services/application.py"],
                "acceptance_result": "fully_met",
                "checklist_result": {"Focused tests": "passed"},
            },
            "task_id",
            7,
        ),
        (
            "/api/projects/41/story/close",
            {
                "instance_key": "story:9",
                "resolution": "Completed",
                "delivered": "Semantic execution transport.",
                "evidence": "Focused tests pass.",
                "known_gaps": "None.",
            },
            "completion_fingerprint",
            "caller-owned",
        ),
        (
            "/api/projects/41/sprint/review",
            {"instance_key": "sprint:31"},
            "sprint_id",
            31,
        ),
        (
            "/api/projects/41/sprint/close",
            {"instance_key": "sprint:31"},
            "review_fingerprint",
            "owned",
        ),
        (
            "/api/projects/41/sprint/triage",
            {
                "instance_key": "sprint:31",
                "impact": "none",
                "canonical_payload": {"summary": "No follow-up."},
            },
            "closure_fingerprint",
            "caller-owned",
        ),
        (
            "/api/projects/41/sprint/review",
            {"instance_key": "sprint:31"},
            "semantic_input",
            {},
        ),
        (
            "/api/projects/41/sprint/close",
            {"instance_key": "sprint:31"},
            "graph_version",
            "caller-owned",
        ),
        (
            "/api/projects/41/sprint/triage",
            {
                "instance_key": "sprint:31",
                "impact": "none",
                "canonical_payload": {"summary": "No follow-up."},
            },
            "model_id",
            "caller/model",
        ),
    ],
)
def test_execution_action_api_rejects_internal_fields(
    path: str,
    payload: dict[str, object],
    internal_field: str,
    value: object,
) -> None:
    """Reject graph IDs, fact IDs, and fingerprints at every HTTP boundary."""
    response = TestClient(api_module.app).post(
        path,
        json={
            **payload,
            internal_field: value,
            "idempotency_key": "execution-action-41",
            "actor": "operator",
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/projects/41/sprint/task/complete",
            {
                "instance_key": "task:7",
                "outcome_summary": "  ",
                "artifact_refs": ["services/application.py"],
                "acceptance_result": "fully_met",
                "checklist_result": {"Focused tests": "passed"},
            },
        ),
        (
            "/api/projects/41/sprint/task/complete",
            {
                "instance_key": "task:7",
                "outcome_summary": "Implemented semantic execution.",
                "artifact_refs": ["services/application.py"],
                "acceptance_result": "fully_met",
                "checklist_result": {"Focused tests": 1},
            },
        ),
        (
            "/api/projects/41/story/close",
            {
                "instance_key": "story:9",
                "resolution": "Completed",
                "delivered": " ",
                "evidence": "Focused tests pass.",
                "known_gaps": "None.",
            },
        ),
        (
            "/api/projects/41/sprint/triage",
            {
                "instance_key": "sprint:31",
                "impact": "invalid",
                "canonical_payload": {},
            },
        ),
    ],
)
def test_execution_action_api_rejects_invalid_semantics(
    path: str,
    payload: dict[str, object],
) -> None:
    """Reject blank text, untyped checklist values, and invalid triage impact."""
    response = TestClient(api_module.app).post(
        path,
        json={
            **payload,
            "idempotency_key": "execution-action-41",
            "actor": "operator",
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


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


def test_generic_positioned_api_transport_is_removed() -> None:
    """Delete the public generic positioned model, path map, and route installer."""
    source = (Path(__file__).parents[2] / "api.py").read_text()

    assert not hasattr(api_module, "PositionedTransitionApiRequest")
    assert not hasattr(api_module, "POSITIONED_API_PATHS")
    assert "_positioned_route" not in source
    assert "build_positioned_transition_request" not in source


def test_position_endpoint_delegates_once_and_state_endpoint_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose only the graph position route and make one application query."""
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
    expected = set(api_module.SEMANTIC_API_PATHS) | set(api_module.DELIVERY_API_PATHS)
    assert {item["request_kind"] for item in actions} == expected
    assert all(item["endpoint"].startswith("/") is False for item in actions)
    assert "record_sprint_plan" in expected
    assert all("decision_fingerprint" not in item for item in actions)


def test_position_advertises_optional_specification_source_reentry_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose source re-entry without making all optional work immediate."""
    source = NodeDecision(
        node_id="specification.source.register",
        child_graph_id="specification",
        request_kind="register_specification_source",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        reason_code="SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE",
        decision_fingerprint="source-reentry",
    )
    unrelated = NodeDecision(
        node_id="vision.bootstrap",
        child_graph_id="vision",
        request_kind="generate_vision_bootstrap",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        reason_code="VISION_OPTIONAL_REENTRY",
        decision_fingerprint="vision-reentry",
    )
    position = _all_request_kinds_position().model_copy(
        update={
            "available_nodes": (source.node_id, unrelated.node_id),
            "decisions": (source, unrelated),
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
            "node_id": "specification.source.register",
            "instance_key": None,
            "request_kind": "register_specification_source",
            "endpoint": "specifications/source",
            "transport": "semantic",
        }
    ]


def test_position_advertises_both_specification_feedback_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project Feedback as two semantic human actions with no durable IDs."""
    source = NodeDecision(
        node_id="specification.source.register",
        child_graph_id="specification",
        request_kind="register_specification_source",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        reason_code="SPECIFICATION_FEEDBACK_SOURCE_REVISION_AVAILABLE",
        decision_fingerprint="feedback-source-revision",
    )
    retry = NodeDecision(
        node_id="specification.structure",
        child_graph_id="specification",
        request_kind="structure_specification",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPECIFICATION_FEEDBACK_RETRY_AVAILABLE",
        decision_fingerprint="feedback-same-source-retry",
    )
    position = _all_request_kinds_position().model_copy(
        update={
            "available_nodes": (source.node_id, retry.node_id),
            "decisions": (source, retry),
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
            "node_id": "specification.source.register",
            "instance_key": None,
            "request_kind": "register_specification_source",
            "endpoint": "specifications/source",
            "transport": "semantic",
        },
        {
            "node_id": "specification.structure",
            "instance_key": None,
            "request_kind": "structure_specification",
            "endpoint": "specifications/structure",
            "transport": "semantic",
        },
    ]


@pytest.mark.parametrize(
    "request_kind",
    [
        "apply_story_dependencies",
        "repair_story_readiness",
        "start_sprint",
    ],
)
def test_position_does_not_advertise_malformed_planning_actions(
    request_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppress actions whose graph decision omits required durable references."""
    decision = _delivery_decision(
        node_id=f"test.{request_kind}",
        request_kind=request_kind,
    )
    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=_vision_position(decision)),
    )

    response = TestClient(api_module.app).get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["actions"] == []


@pytest.mark.parametrize(
    "request_kind",
    [
        "complete_task",
        "close_story",
        "review_sprint",
        "close_sprint",
        "record_post_sprint_triage",
    ],
)
def test_position_does_not_advertise_malformed_execution_actions(
    request_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppress execution actions without an exact selector and durable references."""
    decision = _delivery_decision(
        node_id=f"test.{request_kind}",
        request_kind=request_kind,
    )
    monkeypatch.setattr(
        api_module,
        "_application",
        lambda: _FakeApiApplication(position=_vision_position(decision)),
    )

    response = TestClient(api_module.app).get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["actions"] == []


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
            instance_key=f"backlog_item:PBI-{index:06d}",
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
        "backlog_item:PBI-000000",
        "backlog_item:PBI-000001",
    ]


def test_position_omits_unique_selectorless_story_review_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not advertise a Story review action without its required selector."""
    decision = _delivery_decision(
        node_id="planning.story.review",
        request_kind="decide_story",
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
    assert response.json()["actions"] == []


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
        "/api/projects/41/backlog/decide",
        headers={"X-AgileForge-Expected-Decision": "decision-backlog"},
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
        set(api_module.SEMANTIC_API_PATHS) | set(api_module.DELIVERY_API_PATHS)
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
        applied_node_id="vision.interview",
        output={"vision_interview_turn_id": 17},
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
            node_id="vision.interview",
            input_payload={"prompt": "draft"},
            model_id="offline/model",
            idempotency_key="dashboard-vision-41",
            actor="dashboard-user",
        )
    )

    assert result == prior_result
    assert len(domain.requests) == 1
    assert isinstance(domain.requests[0], StartNodeAttempt)


def test_specification_source_endpoint_binds_semantic_file_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit paths with the shown position while capture remains host-owned."""
    application = _FakeApiApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        "/api/projects/41/specifications/source",
        headers={"X-AgileForge-Expected-Decision": "sha256:source-position"},
        json={
            "source_path": "SPECIFICATION.md",
            "adr_paths": ["docs/adr/0002.md", "docs/adr/0001.md"],
            "preparation_capability": "grill-with-docs",
            "idempotency_key": "source-api-41",
            "actor": "dashboard-user",
            "correlation_id": "corr-api-41",
        },
    )

    assert response.status_code == HTTPStatus.OK
    request = application.requests[0]
    assert isinstance(request, SpecificationSourceRegistrationRequest)
    assert request.expected_decision_fingerprint == "sha256:source-position"
    assert request.model_dump(mode="json") == {
        "project_id": PROJECT_ID,
        "source_path": "SPECIFICATION.md",
        "preparation_capability": "grill-with-docs",
        "adr_paths": ["docs/adr/0001.md", "docs/adr/0002.md"],
        "idempotency_key": "source-api-41",
        "actor": "dashboard-user",
        "correlation_id": "corr-api-41",
    }

    missing_guard = TestClient(api_module.app).post(
        "/api/projects/41/specifications/source",
        json={
            "source_path": "SPECIFICATION.md",
            "preparation_capability": "grill-with-docs",
            "idempotency_key": "source-api-unguarded-41",
            "actor": "dashboard-user",
        },
    )
    assert missing_guard.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    "capability",
    [None, "caller-owned"],
)
def test_specification_source_endpoint_requires_exact_preparation_attestation(
    capability: str | None,
) -> None:
    """Never infer external preparation or accept an unknown capability."""
    payload = {
        "source_path": "SPECIFICATION.md",
        "idempotency_key": "source-api-41",
        "actor": "dashboard-user",
    }
    if capability is not None:
        payload["preparation_capability"] = capability

    response = TestClient(api_module.app).post(
        "/api/projects/41/specifications/source",
        headers={"X-AgileForge-Expected-Decision": "sha256:source-position"},
        json=payload,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_specification_structure_endpoint_binds_the_rendered_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep host-selected input while guarding the position shown by the UI."""
    application = _FakeApiApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        "/api/projects/41/specifications/structure",
        headers={
            "X-AgileForge-Expected-Decision": "sha256:structure-position",
        },
        json={
            "idempotency_key": "structure-api-41",
            "actor": "dashboard-user",
            "correlation_id": "corr-api-41",
        },
    )

    assert response.status_code == HTTPStatus.OK
    request = application.requests[0]
    assert isinstance(request, SpecificationStructuringRequest)
    assert request.expected_decision_fingerprint == "sha256:structure-position"
    assert request.model_dump(mode="json") == {
        "project_id": PROJECT_ID,
        "idempotency_key": "structure-api-41",
        "actor": "dashboard-user",
        "correlation_id": "corr-api-41",
    }

    missing_guard = TestClient(api_module.app).post(
        "/api/projects/41/specifications/structure",
        json={
            "idempotency_key": "structure-api-unguarded-41",
            "actor": "dashboard-user",
        },
    )
    assert missing_guard.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_retired_specification_author_endpoint_is_not_registered() -> None:
    """Hard-break the retired combined authoring transport."""
    response = TestClient(api_module.app).post(
        "/api/projects/41/specifications/author",
        json={
            "idempotency_key": "author-api-41",
            "actor": "dashboard-user",
        },
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/41/vision/review",
        "/api/projects/41/goals/review",
        "/api/projects/41/specifications/review",
    ],
)
def test_browser_review_header_forwards_hidden_candidate_expectation(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward browser review identity without adding it to semantic JSON."""
    application = _FakeApiApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    response = client.post(
        path,
        headers={"X-AgileForge-Expected-Candidate": "sha256:candidate-shown"},
        json={
            "idempotency_key": "review-api-41",
            "actor": "dashboard-ui",
            "decision": "accepted",
            "rationale": "Reviewed the candidate shown in the browser.",
        },
    )

    assert response.status_code == HTTPStatus.OK
    request = application.requests[0]
    assert isinstance(
        request,
        (
            VisionReviewRequest,
            ProductGoalReviewRequest,
            SpecificationReviewRequest,
        ),
    )
    assert request.expected_candidate_fingerprint == "sha256:candidate-shown"


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/41/specifications/review",
    ],
)
def test_exact_review_requires_transport_captured_candidate(path: str) -> None:
    """Reject a decision that is not bound to the packet the transport showed."""
    response = TestClient(api_module.app).post(
        path,
        json={
            "idempotency_key": "review-api-41",
            "actor": "dashboard-ui",
            "decision": "accepted",
            "rationale": "Reviewed the candidate shown in the browser.",
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_browser_review_expectation_is_absent_from_public_api_schema() -> None:
    """Keep the browser-only guard out of semantic JSON and API documentation."""
    schema = api_module.app.openapi()
    for path in (
        "/api/projects/{project_id}/vision/review",
        "/api/projects/{project_id}/goals/review",
        "/api/projects/{project_id}/specifications/review",
    ):
        operation = schema["paths"][path]["post"]
        parameters = operation.get("parameters", [])
        assert all(
            item.get("name") != "X-AgileForge-Expected-Candidate" for item in parameters
        )
    review_properties = schema["components"]["schemas"]["ReviewApiRequest"][
        "properties"
    ]
    assert "expected_candidate_fingerprint" not in review_properties


# Retained Task 11 planning-review binding coverage.
def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _items(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast("list[object]", value)


class _FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, ExpectedPlanningReviewBinding | None]] = []

    def backlog_review(self, project_id: int) -> JsonObject:
        return _review_result("backlog", project_id)

    def roadmap_review(self, project_id: int) -> JsonObject:
        return _review_result("roadmap", project_id)

    def story_reviews(self, project_id: int) -> JsonObject:
        result = _review_result("story", project_id)
        data = result["data"]
        assert isinstance(data, dict)
        result["data"] = {"items": [data]}
        return result

    def sprint_plan_review(self, project_id: int) -> JsonObject:
        return _review_result("sprint_plan", project_id)

    def decide_backlog(
        self, request: object, *, expected: ExpectedPlanningReviewBinding
    ) -> TransitionResult:
        return self._record("backlog", request, expected)

    def decide_roadmap(
        self, request: object, *, expected: ExpectedPlanningReviewBinding
    ) -> TransitionResult:
        return self._record("roadmap", request, expected)

    def decide_story(
        self, request: object, *, expected: ExpectedPlanningReviewBinding
    ) -> TransitionResult:
        return self._record("story", request, expected)

    def decide_sprint_plan(
        self, request: object, *, expected: ExpectedPlanningReviewBinding
    ) -> TransitionResult:
        return self._record("sprint_plan", request, expected)

    def _record(
        self,
        phase: str,
        request: object,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
        self.calls.append((phase, request, expected))
        return TransitionResult(ok=True)


def _review_result(phase: str, project_id: int) -> JsonObject:
    return {
        "ok": True,
        "data": {
            "binding": {
                "decision_fingerprint": f"decision-{phase}",
                "instance_key": "backlog_item:PBI-000001" if phase == "story" else None,
            },
            "review": {"phase": phase, "project_id": project_id},
        },
        "warnings": [],
        "errors": [],
    }


@pytest.fixture
def fake_application(monkeypatch: pytest.MonkeyPatch) -> _FakeApplication:
    """Install one typed application fake for the exact API route tests."""
    application = _FakeApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    return application


@pytest.mark.parametrize(
    ("path", "phase"),
    [
        ("/api/projects/41/backlog/review", "backlog"),
        ("/api/projects/41/roadmap/review", "roadmap"),
        ("/api/projects/41/story/reviews", "story"),
        ("/api/projects/41/sprint/plan/review", "sprint_plan"),
    ],
)
def test_review_gets_return_application_selected_content(
    fake_application: _FakeApplication,
    path: str,
    phase: str,
) -> None:
    """GET routes expose the captured binding beside exact review content."""
    assert fake_application is not None
    response = TestClient(api_module.app).get(path)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["status"] == "success"
    data = payload["data"]
    selected = data["items"][0] if phase == "story" else data
    assert list(selected) == ["binding", "review"]
    assert selected["review"]["phase"] == phase


@pytest.mark.parametrize(
    ("path", "phase"),
    [
        ("/api/projects/41/backlog/decide", "backlog"),
        ("/api/projects/41/roadmap/decide", "roadmap"),
        ("/api/projects/41/sprint/decide", "sprint_plan"),
    ],
)
def test_unique_review_posts_require_scalar_decision_header(
    fake_application: _FakeApplication,
    path: str,
    phase: str,
) -> None:
    """Unique phase bodies remain semantic and bind only through one header."""
    client = TestClient(api_module.app)
    body = {
        "decision": "accepted",
        "rationale": "Reviewed exact evidence.",
        "idempotency_key": f"accept-{phase}",
        "actor": "operator",
    }
    missing = client.post(path, json=body)
    assert missing.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    response = client.post(
        path,
        headers={"X-AgileForge-Expected-Decision": f"decision-{phase}"},
        json=body,
    )
    assert response.status_code == HTTPStatus.OK
    recorded_phase, request, binding = fake_application.calls[-1]
    assert recorded_phase == phase
    assert isinstance(
        request,
        (BacklogReviewRequest, RoadmapReviewRequest, SprintPlanReviewRequest),
    )
    assert request.model_dump()["decision"] == "accepted"
    assert binding == ExpectedPlanningReviewBinding(
        decision_fingerprint=f"decision-{phase}",
        instance_key=None,
    )


def test_story_post_requires_independent_decision_and_instance_headers(
    fake_application: _FakeApplication,
) -> None:
    """Story transport reconstructs both hidden binding components from headers."""
    client = TestClient(api_module.app)
    path = "/api/projects/41/story/decide"
    body = {
        "decision": "accepted",
        "rationale": "Reviewed Story evidence.",
        "idempotency_key": "accept-story",
        "actor": "operator",
    }
    assert client.post(path, json=body).status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert (
        client.post(
            path,
            headers={"X-AgileForge-Expected-Decision": "decision-story"},
            json=body,
        ).status_code
        == HTTPStatus.UNPROCESSABLE_ENTITY
    )
    response = client.post(
        path,
        headers={
            "X-AgileForge-Expected-Decision": "decision-story",
            "X-AgileForge-Expected-Instance": "backlog_item:PBI-000001",
        },
        json=body,
    )
    assert response.status_code == HTTPStatus.OK
    _phase, request, binding = fake_application.calls[-1]
    assert isinstance(request, StoryReviewRequest)
    assert "instance_key" not in request.model_dump()
    assert binding == ExpectedPlanningReviewBinding(
        decision_fingerprint="decision-story",
        instance_key="backlog_item:PBI-000001",
    )


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        (
            "/api/projects/41/backlog/decide",
            {"X-AgileForge-Expected-Decision": "   "},
        ),
        (
            "/api/projects/41/story/decide",
            {
                "X-AgileForge-Expected-Decision": "   ",
                "X-AgileForge-Expected-Instance": "backlog_item:PBI-000001",
            },
        ),
        (
            "/api/projects/41/story/decide",
            {
                "X-AgileForge-Expected-Decision": "decision-story",
                "X-AgileForge-Expected-Instance": "   ",
            },
        ),
    ],
)
def test_review_binding_headers_reject_whitespace_at_transport(
    fake_application: _FakeApplication,
    path: str,
    headers: dict[str, str],
) -> None:
    """Invalid hidden bindings return 422 without application calls or a 500."""
    response = TestClient(api_module.app, raise_server_exceptions=False).post(
        path,
        headers=headers,
        json={
            "decision": "accepted",
            "rationale": "Reviewed exact evidence.",
            "idempotency_key": "reject-whitespace",
            "actor": "operator",
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert not fake_application.calls


def test_story_body_rejects_machine_identity(
    fake_application: _FakeApplication,
) -> None:
    """Artifact, fingerprint, and instance fields cannot enter semantic JSON."""
    response = TestClient(api_module.app).post(
        "/api/projects/41/story/decide",
        headers={
            "X-AgileForge-Expected-Decision": "decision-story",
            "X-AgileForge-Expected-Instance": "backlog_item:PBI-000001",
        },
        json={
            "decision": "accepted",
            "rationale": "Reviewed.",
            "idempotency_key": "accept-story",
            "actor": "operator",
            "instance_key": "backlog_item:PBI-000001",
        },
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert not fake_application.calls


def test_openapi_has_no_removed_compatibility_routes_or_body_fields() -> None:
    """The hard break publishes only direct-Spec review routes and semantic bodies."""
    schema = api_module.app.openapi()
    paths = schema["paths"]
    assert all("auth" + "ority" not in path.lower() for path in paths)
    assert "/api/projects/{project_id}/story/reviews" in paths
    story_schema = schema["components"]["schemas"]["StoryReviewApiRequest"]
    properties = story_schema["properties"]
    for forbidden in ("instance_key", "artifact_id", "fingerprint"):
        assert forbidden not in properties


class _Domain:
    def __init__(self, position: WorkflowPosition) -> None:
        self.current = position
        self.position_calls = 0
        self.transitions: list[object] = []

    def position(self, project_id: int) -> WorkflowPosition:
        assert project_id == self.current.project_id
        self.position_calls += 1
        return self.current

    def transition(self, request: TransitionRequest) -> TransitionResult:
        self.transitions.append(request)
        return TransitionResult(ok=True, position=self.current)

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        raise AssertionError(
            (project_id, attempt_id, attempt_fingerprint),
        )


class _Selection:
    def __init__(self, replay: TransitionResult | None = None) -> None:
        self.replay = replay
        self.replay_calls = 0

    def replay_transition(self, query: object) -> TransitionResult | None:
        del query
        self.replay_calls += 1
        return self.replay

    def review_identity(self, **_kwargs: object) -> tuple[int, str]:
        return 7, "sha256:" + "a" * 64


class _Reads:
    def backlog_review(
        self, *, project_id: int, backlog_artifact_id: int
    ) -> JsonObject:
        return _selected_projection(project_id, backlog_artifact_id, "backlog")

    def story_review(self, *, project_id: int, story_artifact_id: int) -> JsonObject:
        return _selected_projection(project_id, story_artifact_id, "story")


def _selected_projection(project_id: int, artifact_id: int, phase: str) -> JsonObject:
    return {
        "ok": True,
        "data": {
            "phase": phase,
            "project": project_id,
            "artifact": artifact_id,
        },
        "warnings": [],
        "errors": [],
    }


def _position(
    node_id: str, request_kind: str, instance_key: str | None
) -> WorkflowPosition:
    decision = NodeDecision(
        node_id=node_id,
        child_graph_id="planning",
        request_kind=request_kind,
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="REVIEW_REQUIRED",
        instance_key=instance_key,
        decision_fingerprint="decision-shown",
    )
    return WorkflowPosition(
        project_id=41,
        graph_version="agileforge.workflow.v1",
        fact_fingerprint="facts-current",
        evaluated_at=datetime(2026, 8, 21, tzinfo=UTC),
        available_nodes=(node_id,),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=(decision,),
    )


@pytest.mark.parametrize(
    ("method", "request_type", "node_id", "request_kind", "instance_key"),
    [
        (
            "decide_backlog",
            BacklogReviewRequest,
            "backlog.review",
            "decide_backlog",
            None,
        ),
        (
            "decide_roadmap",
            RoadmapReviewRequest,
            "planning.roadmap.review",
            "decide_roadmap",
            None,
        ),
        (
            "decide_story",
            StoryReviewRequest,
            "planning.story.review",
            "decide_story",
            "backlog_item:PBI-000001",
        ),
        (
            "decide_sprint_plan",
            SprintPlanReviewRequest,
            "planning.sprint.review",
            "decide_sprint_plan",
            None,
        ),
    ],
)
def test_planning_review_binding_rejects_replacement_before_write(
    method: str,
    request_type: type,
    node_id: str,
    request_kind: str,
    instance_key: str | None,
) -> None:
    """Show A then decide against B returns STALE_POSITION and writes nothing."""
    domain = _Domain(_position(node_id, request_kind, instance_key))
    application = AgileForgeApplication(
        workflow_domain=domain,
        delivery_review_selection=_Selection(),
    )
    command = request_type(
        project_id=41,
        decision="accepted",
        rationale="Reviewed exact evidence.",
        idempotency_key=f"accept-{request_kind}",
        actor="operator",
    )
    result = getattr(application, method)(
        command,
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint="decision-replaced",
            instance_key=instance_key,
        ),
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_POSITION"
    assert not domain.transitions


def test_story_binding_checks_instance_independently() -> None:
    """Matching decision with a different repeated instance cannot select a Story."""
    domain = _Domain(
        _position(
            "planning.story.review",
            "decide_story",
            "backlog_item:PBI-000001",
        )
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        delivery_review_selection=_Selection(),
    )
    result = application.decide_story(
        StoryReviewRequest(
            project_id=41,
            decision="accepted",
            rationale="Reviewed exact evidence.",
            idempotency_key="accept-story",
            actor="operator",
        ),
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint="decision-shown",
            instance_key="backlog_item:PBI-000002",
        ),
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_POSITION"
    assert not domain.transitions


def test_exact_receipt_replay_precedes_position_and_binding_checks() -> None:
    """Authoritative idempotency replay wins even after the graph advances."""
    replay = TransitionResult(ok=True, applied_node_id="backlog.review")
    selection = _Selection(replay)
    domain = _Domain(_position("backlog.review", "decide_backlog", None))
    application = AgileForgeApplication(
        workflow_domain=domain,
        delivery_review_selection=selection,
    )
    result = application.decide_backlog(
        BacklogReviewRequest(
            project_id=41,
            decision="accepted",
            rationale="Reviewed exact evidence.",
            idempotency_key="accept-backlog",
            actor="operator",
        ),
        expected=ExpectedPlanningReviewBinding(
            decision_fingerprint="obsolete-after-replay",
            instance_key=None,
        ),
    )
    assert result is replay
    assert selection.replay_calls == 1
    assert domain.position_calls == 0
    assert not domain.transitions


def test_application_review_read_returns_exact_selected_content_and_binding() -> None:
    """Read one current Backlog review with its captured binding."""
    domain = _Domain(_position("backlog.review", "decide_backlog", None))
    application = AgileForgeApplication(
        workflow_domain=domain,
        read_projection=cast("Any", _Reads()),
        delivery_review_selection=_Selection(),
    )

    result = application.backlog_review(41)

    assert result == {
        "ok": True,
        "data": {
            "binding": {
                "decision_fingerprint": "decision-shown",
                "instance_key": None,
            },
            "review": {"phase": "backlog", "project": 41, "artifact": 7},
        },
        "warnings": [],
        "errors": [],
    }


def test_application_review_read_distinguishes_absence_from_conflict() -> None:
    """Only zero current candidates receives the dashboard absence code."""
    empty = _position("backlog.review", "decide_backlog", None).model_copy(
        update={"decisions": ()}
    )
    application = AgileForgeApplication(
        workflow_domain=_Domain(empty),
        read_projection=cast("Any", _Reads()),
        delivery_review_selection=_Selection(),
    )

    absent = application.backlog_review(41)

    errors = _items(absent["errors"])
    assert _object(errors[0])["code"] == "PLANNING_REVIEW_NOT_AVAILABLE"

    decision = _position("backlog.review", "decide_backlog", None).decisions[0]
    conflicting = empty.model_copy(update={"decisions": (decision, decision)})
    conflict_application = AgileForgeApplication(
        workflow_domain=_Domain(conflicting),
        read_projection=cast("Any", _Reads()),
        delivery_review_selection=_Selection(),
    )
    conflict = conflict_application.backlog_review(41)
    conflict_errors = _items(conflict["errors"])
    assert _object(conflict_errors[0])["code"] == "WORKFLOW_FACT_CONFLICT"


def test_application_story_review_reads_are_stably_ordered() -> None:
    """Order independently selected Story reviews by their captured identity."""
    later = _position(
        "planning.story.review",
        "decide_story",
        "backlog_item:PBI-000002",
    ).decisions[0]
    earlier = later.model_copy(
        update={
            "instance_key": "backlog_item:PBI-000001",
            "decision_fingerprint": "decision-earlier",
        }
    )
    position = _position(
        "planning.story.review",
        "decide_story",
        "backlog_item:PBI-000002",
    ).model_copy(update={"decisions": (later, earlier)})
    application = AgileForgeApplication(
        workflow_domain=_Domain(position),
        read_projection=cast("Any", _Reads()),
        delivery_review_selection=_Selection(),
    )

    result = application.story_reviews(41)

    assert result["ok"] is True
    data = _object(result["data"])
    items = _items(data["items"])
    assert [_object(_object(item)["binding"])["instance_key"] for item in items] == [
        "backlog_item:PBI-000001",
        "backlog_item:PBI-000002",
    ]
