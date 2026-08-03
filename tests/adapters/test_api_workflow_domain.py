"""API adapter tests for exact typed workflow requests."""

from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

import api as api_module
from api import (
    AGENTIC_API_PATHS,
    POSITIONED_API_PATHS,
    AuthorityDecisionApiRequest,
    CreateProjectRequest,
    PositionedTransitionApiRequest,
    build_authority_decision_request,
    build_positioned_transition_request,
    build_project_shell_request,
)
from cli.workflow_commands import AGENTIC_REQUEST_KINDS, COMMAND_PREFIXES
from services.application import AgenticActionRequest, AgileForgeApplication
from tests.adapters.test_command_renderer import position_fixture
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.requests import (
    DecideAuthority,
    DecideVision,
    OpenProjectShell,
    StartNodeAttempt,
    TransitionRequest,
)

if TYPE_CHECKING:
    from adapters.adk.recipes import AdkRecipeRegistry

PROJECT_ID = 41


class _FakeApiApplication:
    def __init__(
        self,
        *,
        position: WorkflowPosition | None = None,
        transition_result: TransitionResult | None = None,
    ) -> None:
        self.position_calls: list[int] = []
        self.requests: list[TransitionRequest] = []
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
        graph_version="agileforge.workflow.v1",
        fact_fingerprint="facts-all",
        evaluated_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        available_nodes=tuple(item.node_id for item in decisions),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=decisions,
    )


def test_api_project_shell_request_uses_explicit_origin() -> None:
    """Translate explicit API origin into OpenProjectShell."""
    request = build_project_shell_request(
        CreateProjectRequest(
            name="Example",
            origin="greenfield",
            idempotency_key="open-api-41",
            changed_by="dashboard-user",
            correlation_id="corr-api-41",
        )
    )

    assert request == OpenProjectShell(
        name="Example",
        origin="greenfield",
        idempotency_key="open-api-41",
        actor="dashboard-user",
        correlation_id="corr-api-41",
    )


def test_api_authority_decision_copies_all_guards() -> None:
    """Copy all API guards into DecideAuthority."""
    payload = AuthorityDecisionApiRequest(
        graph_version="agileforge.workflow.v1",
        expected_fact_fingerprint="facts-41",
        expected_decision_fingerprint="decision-review",
        idempotency_key="accept-api-41",
        changed_by="dashboard-user",
        correlation_id="corr-api-41",
        pending_authority_id=23,
        authority_fingerprint="authority-23",
        review_fingerprint="review-23",
        decision="accepted",
        rationale="Reviewed",
    )

    request = build_authority_decision_request(41, payload)

    assert isinstance(request, DecideAuthority)
    assert request.model_dump() == {
        "kind": "decide_authority",
        "project_id": 41,
        "graph_version": "agileforge.workflow.v1",
        "fact_fingerprint": "facts-41",
        "decision_fingerprint": "decision-review",
        "idempotency_key": "accept-api-41",
        "actor": "dashboard-user",
        "correlation_id": "corr-api-41",
        "instance_key": None,
        "attempt_id": None,
        "attempt_fingerprint": None,
        "pending_authority_id": 23,
        "authority_fingerprint": "authority-23",
        "review_fingerprint": "review-23",
        "decision": "accepted",
        "rationale": "Reviewed",
    }


def test_api_adapter_does_not_import_legacy_routing_authority() -> None:
    """Keep the production API free of old routing imports."""
    source = (Path(__file__).parents[2] / "api.py").read_text()
    assert "from services.workflow import WorkflowService" not in source
    assert "ReadOnlySessionReader" not in source


def test_task_specific_api_routes_cover_every_rendered_mutation() -> None:
    """Expose a fixed API route for every graph-authored mutation command."""
    assert set(POSITIONED_API_PATHS) | set(AGENTIC_API_PATHS) | {
        "decide_authority"
    } == set(COMMAND_PREFIXES)
    assert set(AGENTIC_API_PATHS) == set(AGENTIC_REQUEST_KINDS)


def test_positioned_api_builder_uses_fixed_kind_and_all_guards() -> None:
    """Build one non-agentic request without accepting an action string."""
    payload = PositionedTransitionApiRequest(
        graph_version="agileforge.workflow.v1",
        expected_fact_fingerprint="facts-41",
        expected_decision_fingerprint="decision-vision-review",
        idempotency_key="vision-review-41",
        changed_by="dashboard-user",
        correlation_id="corr-api-41",
        input_payload={
            "vision_artifact_id": 12,
            "artifact_fingerprint": "vision-12",
            "decision": "accepted",
            "rationale": "Reviewed",
        },
    )

    request = build_positioned_transition_request(41, "decide_vision", payload)

    assert isinstance(request, DecideVision)
    assert request.fact_fingerprint == "facts-41"
    assert request.decision_fingerprint == "decision-vision-review"
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
    assert {
        (
            item["node_id"],
            item["instance_key"],
            item["decision_fingerprint"],
        )
        for item in actions
    } == {
        (item.node_id, item.instance_key, item.decision_fingerprint)
        for item in position.decisions
    }


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
            "graph_version": "agileforge.workflow.v1",
            "expected_fact_fingerprint": "facts-old",
            "expected_decision_fingerprint": "decision-old",
            "idempotency_key": "stale-api-41",
            "changed_by": "dashboard-user",
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
        graph_version="agileforge.workflow.v1",
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
            graph_version="agileforge.workflow.v1",
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
    """Submit one fully guarded authority request through transition only."""
    application = _FakeApiApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    response = client.post(
        "/api/projects/41/authority/decision",
        json={
            "graph_version": "agileforge.workflow.v1",
            "expected_fact_fingerprint": "facts-41",
            "expected_decision_fingerprint": "decision-review",
            "idempotency_key": "accept-api-41",
            "changed_by": "dashboard-user",
            "correlation_id": "corr-api-41",
            "pending_authority_id": 23,
            "authority_fingerprint": "authority-23",
            "review_fingerprint": "review-23",
            "decision": "accepted",
            "rationale": "Reviewed",
        },
    )

    assert response.status_code == HTTPStatus.OK
    request = cast("DecideAuthority", application.requests[0])
    assert isinstance(request, DecideAuthority)
    assert request.decision_fingerprint == "decision-review"
