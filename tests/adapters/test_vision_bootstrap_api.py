"""HTTP boundary tests for semantic Vision bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

import api as api_module
from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    VisionBootstrapRequest,
)
from services.vision_evidence import (
    VisionEvidenceCollectionError,
    VisionEvidenceErrorCode,
)
from services.vision_evidence_reader import RepositoryEvidenceCapability
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)

if TYPE_CHECKING:
    from services.node_attempt_replay import (
        NodeAttemptReplayQuery,
        TransitionReplayQuery,
    )
    from workflow.requests import TransitionRequest

PROJECT_ID = 41


def _bootstrap_decision() -> NodeDecision:
    return NodeDecision(
        node_id="vision.bootstrap",
        child_graph_id="vision",
        request_kind="generate_vision_bootstrap",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_BOOTSTRAP_REQUIRED",
        decision_fingerprint="sha256:vision-bootstrap",
    )


def _position(*decisions: NodeDecision) -> WorkflowPosition:
    return WorkflowPosition(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="sha256:facts",
        evaluated_at=datetime(2026, 8, 10, tzinfo=UTC),
        available_nodes=tuple(item.node_id for item in decisions),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=decisions,
    )


class _BoundaryDomain:
    def __init__(self, position: WorkflowPosition) -> None:
        self._position = position
        self.position_calls: list[int] = []

    def position(self, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return self._position

    def transition(self, request: TransitionRequest) -> TransitionResult:
        pytest.fail(
            f"unexpected transition: {request}"  # ty: ignore[invalid-argument-type]
        )

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        pytest.fail(
            "unexpected persisted input load: "
            f"{project_id}:{attempt_id}:{attempt_fingerprint}"  # ty: ignore[invalid-argument-type]
        )


class _VisionInput:
    def __init__(
        self,
        *,
        replay_after_first: bool = False,
        failure_code: VisionEvidenceErrorCode | None = None,
        capability_available: bool = True,
    ) -> None:
        self.replay_after_first = replay_after_first
        self.failure_code = failure_code
        self.capability_available = capability_available
        self.replay_queries: list[NodeAttemptReplayQuery] = []
        self.build_calls = 0

    def bootstrap_capability(self, project_id: int) -> RepositoryEvidenceCapability:
        del project_id
        if self.capability_available:
            return RepositoryEvidenceCapability(available=True)
        return RepositoryEvidenceCapability(
            available=False,
            code="REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
            message="Repository evidence is unavailable.",
        )

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        self.replay_queries.append(query)
        if self.replay_after_first and len(self.replay_queries) > 1:
            return TransitionResult(
                ok=True,
                replayed=True,
                applied_node_id="vision.bootstrap",
            )
        return None

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        del query
        return None

    def build_bootstrap(
        self,
        project_id: int,
        decision: NodeDecision,
    ) -> JsonObject:
        del project_id, decision
        self.build_calls += 1
        if self.failure_code is not None:
            raise VisionEvidenceCollectionError(
                self.failure_code,
                "Repository evidence changed before Vision bootstrap.",
            )
        return {"request": {"operation": "bootstrap"}}

    def build_clarification(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject:
        del project_id, decision, user_text
        pytest.fail(
            "bootstrap must not build clarification input"  # ty: ignore[invalid-argument-type]
        )


class _BootstrapApplication(AgileForgeApplication):
    def __init__(self, domain: _BoundaryDomain, vision_input: _VisionInput) -> None:
        super().__init__(workflow_domain=domain, vision_input=vision_input)
        self.execution_calls: list[AgenticActionRequest] = []

    def run_agentic_action(self, request: AgenticActionRequest) -> TransitionResult:
        self.execution_calls.append(request)
        return TransitionResult(ok=True, applied_node_id="vision.bootstrap")


class _CapturingApplication:
    def __init__(self) -> None:
        self.requests: list[VisionBootstrapRequest] = []

    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult:
        self.requests.append(request)
        return TransitionResult(ok=True, applied_node_id="vision.bootstrap")


class _PureReads:
    def project_show(self, *, project_id: int) -> JsonObject:
        return {"ok": True, "data": {"project_id": project_id}}

    def vision_status(self, *, project_id: int) -> JsonObject:
        return {"ok": True, "data": {"project_id": project_id, "current": None}}


class _PureReadApplication:
    def __init__(self) -> None:
        self.reads = _PureReads()
        self.position_calls: list[int] = []
        self.bootstrap_calls = 0

    def position(self, *, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return _position(_bootstrap_decision())

    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult:
        del request
        self.bootstrap_calls += 1
        pytest.fail(
            "read route invoked Vision bootstrap"  # ty: ignore[invalid-argument-type]
        )


class _LockedPureReadApplication(_PureReadApplication):
    def vision_bootstrap_capability(
        self,
        *,
        project_id: int,
    ) -> RepositoryEvidenceCapability:
        del project_id
        return RepositoryEvidenceCapability(
            available=False,
            code="REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
            message="Repository evidence is unavailable.",
        )


def test_bootstrap_post_forwards_only_mutation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward only validated caller-owned mutation metadata."""
    application = _CapturingApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap",
        json={
            "idempotency_key": "vision-bootstrap-41",
            "actor": "operator",
            "correlation_id": "corr-41",
        },
    )

    assert response.status_code == HTTPStatus.OK
    request = application.requests[0]
    assert isinstance(request, VisionBootstrapRequest)
    assert request.model_dump() == {
        "project_id": PROJECT_ID,
        "idempotency_key": "vision-bootstrap-41",
        "actor": "operator",
        "correlation_id": "corr-41",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"actor": "operator"},
        {"idempotency_key": "vision-bootstrap-41"},
        {"idempotency_key": "", "actor": "operator"},
        {"idempotency_key": "vision-bootstrap-41", "actor": ""},
        {
            "idempotency_key": "vision-bootstrap-41",
            "actor": "operator",
            "correlation_id": "",
        },
        {"idempotency_key": 41, "actor": "operator"},
        {"idempotency_key": "vision-bootstrap-41", "actor": 41},
        {
            "idempotency_key": "vision-bootstrap-41",
            "actor": "operator",
            "correlation_id": 41,
        },
    ],
)
def test_bootstrap_post_rejects_malformed_mutation_metadata(
    payload: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject missing, blank, or wrongly typed transport metadata."""
    application = _CapturingApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap",
        json=payload,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert application.requests == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("graph_version", "agileforge.workflow.v2"),
        ("fact_fingerprint", "sha256:facts"),
        ("decision_fingerprint", "sha256:decision"),
        ("evidence_fingerprint", "sha256:evidence"),
        ("vision_evidence_snapshot_id", 7),
        ("repository_binding_id", 11),
        ("supersession_id", 13),
        ("mode", "bootstrap"),
        ("operation", "bootstrap"),
        ("repository_path", "/caller/owned"),
        ("repository_head_sha", "deadbeef"),
        ("model_id", "caller/model"),
    ],
)
def test_bootstrap_post_rejects_internal_or_repository_fields(
    field: str,
    value: object,
) -> None:
    """Reject graph, evidence, repository, and model-owned body fields."""
    response = TestClient(api_module.app).post(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap",
        json={
            "idempotency_key": "vision-bootstrap-41",
            "actor": "operator",
            field: value,
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_bootstrap_post_replays_same_key_without_second_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay before graph preparation or another paid execution boundary."""
    domain = _BoundaryDomain(_position(_bootstrap_decision()))
    vision_input = _VisionInput(replay_after_first=True)
    application = _BootstrapApplication(domain, vision_input)
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)
    payload = {
        "idempotency_key": "vision-bootstrap-replay-41",
        "actor": "operator",
    }

    first = client.post(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap",
        json=payload,
    )
    second = client.post(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap",
        json=payload,
    )

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    assert second.json()["data"]["replayed"] is True
    assert len(application.execution_calls) == 1
    assert domain.position_calls == [PROJECT_ID]


@pytest.mark.parametrize(
    "failure_code",
    tuple(VisionEvidenceErrorCode),
)
def test_bootstrap_preflight_failure_uses_transport_error_without_execution(
    failure_code: VisionEvidenceErrorCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map repository preflight failures without invoking the agent runner."""
    domain = _BoundaryDomain(_position(_bootstrap_decision()))
    application = _BootstrapApplication(
        domain,
        _VisionInput(failure_code=failure_code),
    )
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap",
        json={
            "idempotency_key": f"vision-bootstrap-{failure_code.value}",
            "actor": "operator",
        },
    )

    expected_status = (
        HTTPStatus.NOT_FOUND
        if failure_code is VisionEvidenceErrorCode.PROJECT_NOT_FOUND
        else HTTPStatus.CONFLICT
    )
    assert response.status_code == expected_status
    assert response.json()["detail"]["error"]["code"] == failure_code.value
    assert application.execution_calls == []


def test_bootstrap_capability_failure_stops_before_input_and_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject unsupported evidence collection before input or provider work."""
    domain = _BoundaryDomain(_position(_bootstrap_decision()))
    vision_input = _VisionInput(capability_available=False)
    application = _BootstrapApplication(domain, vision_input)
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).post(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap",
        json={"idempotency_key": "capability-41", "actor": "operator"},
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"]["error"]["code"] == (
        WorkflowErrorCode.REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE.value
    )
    assert vision_input.build_calls == 0
    assert application.execution_calls == []


def test_bootstrap_get_is_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Vision bootstrap unavailable through a read HTTP method."""
    application = _CapturingApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).get(
        f"/api/projects/{PROJECT_ID}/vision/bootstrap"
    )

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert application.requests == []


def test_project_vision_reads_and_position_do_not_invoke_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep project, status, and workflow position reads side-effect free."""
    application = _PureReadApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    responses = (
        client.get(f"/api/projects/{PROJECT_ID}/vision/status"),
        client.get(f"/api/projects/{PROJECT_ID}"),
        client.get(f"/api/projects/{PROJECT_ID}/position"),
    )

    assert all(response.status_code == HTTPStatus.OK for response in responses)
    assert application.bootstrap_calls == 0
    assert responses[-1].json()["actions"] == [
        {
            "node_id": "vision.bootstrap",
            "instance_key": None,
            "request_kind": "generate_vision_bootstrap",
            "endpoint": "vision/bootstrap",
            "transport": "semantic",
        }
    ]


def test_position_locks_bootstrap_when_evidence_capability_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose a visible but non-executable UI action with the closed reason."""
    application = _LockedPureReadApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).get(f"/api/projects/{PROJECT_ID}/position")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["actions"] == [
        {
            "node_id": "vision.bootstrap",
            "instance_key": None,
            "request_kind": "generate_vision_bootstrap",
            "endpoint": "vision/bootstrap",
            "transport": "semantic",
            "availability": "locked",
            "reason_code": "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
        }
    ]


def test_bootstrap_openapi_body_is_strict_mutation_metadata() -> None:
    """Publish only strict mutation metadata in the bootstrap body schema."""
    schema = api_module.app.openapi()
    operation = schema["paths"]["/api/projects/{project_id}/vision/bootstrap"]["post"]
    body_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body_name = cast("str", body_ref).rsplit("/", maxsplit=1)[-1]
    body_schema = schema["components"]["schemas"][body_name]

    assert set(body_schema["properties"]) == {
        "idempotency_key",
        "actor",
        "correlation_id",
    }
    assert body_schema["additionalProperties"] is False
