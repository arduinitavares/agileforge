"""API adapter tests for exact typed workflow requests."""

from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api as api_module
from api import (
    AuthorityDecisionApiRequest,
    CreateProjectRequest,
    PositionedTransitionApiRequest,
    build_authority_decision_request,
    build_create_project_command,
    build_positioned_transition_request,
)
from cli.workflow_commands import COMMAND_PREFIXES
from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    AuthorityReviewRequest,
    CreateProjectCommand,
    VisionResponseRequest,
)
from tests.adapters.test_command_renderer import position_fixture
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.requests import DecideVision, StartNodeAttempt, TransitionRequest

if TYPE_CHECKING:
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
    def replay(self, query: object) -> None:
        del query

    def replay_transition(self, query: object) -> None:
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


class _CapturingApplication(AgileForgeApplication):
    def __init__(self, domain: _BoundaryDomain) -> None:
        super().__init__(workflow_domain=domain, vision_interview_input=_VisionInput())
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


_AGENTIC_NODE_IDS = {
    "record_brownfield_spec_draft": "onboarding.brownfield.curation",
    "compile_authority": "authority.compile",
    "repair_authority": "authority.repair",
    "record_vision_draft": "vision.generate",
    "record_backlog_draft": "backlog.generate",
    "record_roadmap_draft": "planning.roadmap.generate",
    "record_story_draft": "planning.story.generate",
    "record_sprint_plan": "planning.sprint.plan",
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


def test_positioned_api_builder_uses_one_current_decision() -> None:
    """Prepare retained delivery guards from one current position."""
    payload = PositionedTransitionApiRequest(
        idempotency_key="vision-review-41",
        actor="dashboard-user",
        correlation_id="corr-api-41",
        input_payload={
            "vision_artifact_id": 12,
            "artifact_fingerprint": "vision-12",
            "decision": "accepted",
            "rationale": "Reviewed",
        },
    )

    position = _all_request_kinds_position()
    decision = next(
        item for item in position.decisions if item.request_kind == "decide_vision"
    )
    request = build_positioned_transition_request(
        41,
        "decide_vision",
        payload,
        position,
        decision,
    )

    assert isinstance(request, DecideVision)
    assert request.fact_fingerprint == position.fact_fingerprint
    assert request.decision_fingerprint == decision.decision_fingerprint
    assert request.actor == "dashboard-user"


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


def test_position_advertises_one_fixed_route_for_every_available_request_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render one exact executable API action for every closed request kind."""
    position = _all_request_kinds_position()
    application = _FakeApiApplication(position=position)
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    response = client.get("/api/projects/41/position")

    assert response.status_code == HTTPStatus.OK
    actions = response.json()["actions"]
    assert len(actions) == len(COMMAND_PREFIXES)
    assert {item["request_kind"] for item in actions} == set(COMMAND_PREFIXES)
    assert all(item["endpoint"].startswith("/") is False for item in actions)
    assert {(item["node_id"], item["instance_key"]) for item in actions} == {
        (item.node_id, item.instance_key) for item in position.decisions
    }
    assert all("decision_fingerprint" not in item for item in actions)


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
        "/api/projects/41/vision/decide",
        json={
            "idempotency_key": "stale-api-41",
            "actor": "dashboard-user",
            "instance_key": None,
            "input_payload": {
                "vision_artifact_id": 7,
                "artifact_fingerprint": "vision-7",
                "decision": "accepted",
                "rationale": "Reviewed",
            },
        },
    )

    assert response.status_code == HTTPStatus.CONFLICT
    detail = response.json()["detail"]
    assert detail["position"] == position.model_dump(mode="json")
    assert len(detail["actions"]) == len(COMMAND_PREFIXES)


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
