"""API adapter tests for exact typed workflow requests."""

from http import HTTPStatus
from pathlib import Path
from typing import cast

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
from tests.adapters.test_command_renderer import position_fixture
from workflow.contracts import TransitionResult, WorkflowPosition
from workflow.requests import (
    DecideAuthority,
    DecideVision,
    OpenProjectShell,
    TransitionRequest,
)


class _FakeApiApplication:
    def __init__(self) -> None:
        self.position_calls: list[int] = []
        self.requests: list[TransitionRequest] = []

    def position(self, *, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return position_fixture()

    def transition(self, request: TransitionRequest) -> TransitionResult:
        self.requests.append(request)
        return TransitionResult(ok=True, position=position_fixture())


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
