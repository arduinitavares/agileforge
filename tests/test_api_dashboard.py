"""API tests for deterministic setup-first dashboard endpoints."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pytest
from fastapi.testclient import TestClient

import api as api_module

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_TEMP_REDIRECT = 307
HTTP_UNPROCESSABLE = 422
HTTP_SERVER_ERROR = 500


class _StateContext(Protocol):
    """Minimal tool context shape used by dashboard route shims."""

    state: dict[str, object]


def test_api_uses_public_spec_lifecycle_wrapper() -> None:
    """Confirm the API uses the public spec lifecycle wrapper."""
    assert (
        api_module.link_spec_to_product.__module__ == "services.specs.lifecycle_service"
    )


@dataclass
class DummyProduct:
    """Simple in-memory product used by dashboard API tests."""

    product_id: int
    name: str
    description: str | None = None
    vision: str | None = None
    spec_file_path: str | None = None
    compiled_authority_json: str | None = None


class DummyProductRepository:
    """Tiny repository double for dashboard route tests."""

    def __init__(self) -> None:
        """Initialize the in-memory product list."""
        self.products = []

    def get_all(self) -> list[DummyProduct]:
        """Return all known products."""
        return list(self.products)

    def get_by_id(self, product_id: int) -> DummyProduct | None:
        """Return the product matching the provided ID."""
        for product in self.products:
            if product.product_id == product_id:
                return product
        return None

    def create(
        self,
        name: str,
        description: str | None = None,
    ) -> DummyProduct:
        """Create and store a new in-memory product."""
        product = DummyProduct(
            product_id=len(self.products) + 1,
            name=name,
            description=description,
        )
        self.products.append(product)
        return product


class DummyWorkflowService:
    """Workflow-state double used by dashboard route tests."""

    def __init__(self) -> None:
        """Initialize the in-memory workflow state store."""
        self.states: dict[str, dict[str, object]] = {}
        self.single_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    async def initialize_session(self, session_id: str | None = None) -> str:
        """Create a session with the setup-required FSM state."""
        sid = str(session_id or "generated")
        self.states[sid] = {"fsm_state": "SETUP_REQUIRED"}
        return sid

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return a shallow copy of the stored session state."""
        self.single_calls.append(str(session_id))
        return dict(self.states.get(str(session_id), {}))

    def get_session_statuses(
        self,
        session_ids: list[str],
    ) -> dict[str, dict[str, object]]:
        """Return state snapshots for the provided session IDs."""
        normalized = [str(session_id) for session_id in session_ids]
        self.batch_calls.append(normalized)
        return {
            session_id: dict(self.states.get(session_id, {}))
            for session_id in normalized
        }

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Merge a partial state update into the stored session state."""
        sid = str(session_id)
        current = dict(self.states.get(sid, {}))
        current.update(partial_update)
        self.states[sid] = current

    def migrate_legacy_setup_state(self) -> int:
        """Normalize legacy routing-mode sessions to setup-required."""
        migrated = 0
        for sid, payload in self.states.items():
            if payload.get("fsm_state") == "ROUTING_MODE":
                self.states[sid]["fsm_state"] = "SETUP_REQUIRED"
                migrated += 1
        return migrated


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, DummyProductRepository, DummyWorkflowService]:
    repo = DummyProductRepository()
    workflow = DummyWorkflowService()

    monkeypatch.setattr(api_module, "product_repo", repo)
    monkeypatch.setattr(api_module, "workflow_service", workflow)

    def fake_select_project(
        product_id: int,
        context: _StateContext,
    ) -> dict[str, object]:
        product = repo.get_by_id(product_id)
        if not product:
            return {"success": False, "error": "missing"}
        context.state["active_project"] = {
            "product_id": product_id,
            "name": product.name,
            "vision": product.vision,
            "spec_file_path": product.spec_file_path,
        }
        return {"success": True}

    def fake_link_spec_to_product(
        params: dict[str, object],
        tool_context: _StateContext | None = None,
    ) -> dict[str, object]:
        product_id = params["product_id"]
        assert isinstance(product_id, int | str)
        product = repo.get_by_id(int(product_id))
        assert product is not None
        spec_path = params["spec_path"]
        assert isinstance(spec_path, str)

        if "invalid" in spec_path.lower():
            if tool_context:
                tool_context.state["setup_error"] = "invalid spec path"
            return {
                "success": True,
                "compile_success": False,
                "compile_error": "invalid spec path",
                "failure_artifact_id": "setup-artifact-1",
                "failure_stage": "output_validation",
                "failure_summary": "SPEC_COMPILATION_FAILED: invalid spec path",
                "raw_output_preview": '{"invalid": true}',
                "has_full_artifact": True,
            }

        product.spec_file_path = spec_path
        product.compiled_authority_json = '{"ok": true}'

        if tool_context:
            tool_context.state["pending_spec_path"] = spec_path
            tool_context.state["pending_spec_content"] = "SPEC"
            tool_context.state["compiled_authority_cached"] = '{"ok": true}'

        return {
            "success": True,
            "compile_success": True,
            "spec_path": spec_path,
        }

    monkeypatch.setattr(api_module, "select_project", fake_select_project)
    monkeypatch.setattr(api_module, "link_spec_to_product", fake_link_spec_to_product)

    async def fake_run_vision_agent_from_state(
        state: dict[str, object],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, object]:
        del project_id
        return {
            "success": True,
            "input_context": {
                "user_raw_text": user_input or "",
                "prior_vision_state": "NO_HISTORY",
                "specification_content": state.get("pending_spec_content", "SPEC"),
                "compiled_authority": state.get(
                    "compiled_authority_cached", '{"ok": true}'
                ),
            },
            "output_artifact": {
                "updated_components": {
                    "project_name": "Vision Project",
                    "target_user": None,
                    "problem": None,
                    "product_category": None,
                    "key_benefit": None,
                    "competitors": None,
                    "differentiator": None,
                },
                "product_vision_statement": "Draft",
                "is_complete": False,
                "clarifying_questions": ["Need details"],
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    monkeypatch.setattr(
        api_module, "run_vision_agent_from_state", fake_run_vision_agent_from_state
    )

    return TestClient(api_module.app), repo, workflow


class FakeAuthorityApplication:
    """Application facade double for dashboard authority route tests."""

    def __init__(self, workflow: DummyWorkflowService | None = None) -> None:
        """Initialize captured request state."""
        self.workflow = workflow
        self.accept_requests: list[object] = []
        self.reject_requests: list[object] = []

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, object]:
        """Return a review packet containing a dashboard review token."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "include_spec": include_spec,
                "output_format": output_format,
                "summary": {"omission_assessment": "complete"},
                "guard_tokens": {
                    "review_token": "agileforge.authority_review.v1:sha256:test"
                },
            },
            "warnings": [],
            "errors": [],
        }

    def authority_accept(self, request: object) -> dict[str, object]:
        """Capture an authority accept request."""
        self.accept_requests.append(request)
        return {
            "ok": True,
            "data": {
                "project_id": request.project_id,
                "setup_status": "passed",
                "fsm_state": "VISION_INTERVIEW",
            },
            "warnings": [],
            "errors": [],
        }

    def authority_reject(self, request: object) -> dict[str, object]:
        """Capture an authority reject request and keep setup locked."""
        self.reject_requests.append(request)
        if self.workflow is not None:
            self.workflow.update_session_status(
                str(request.project_id),
                {
                    "setup_status": "authority_rejected",
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_error": request.reason,
                },
            )
        return {
            "ok": True,
            "data": {
                "project_id": request.project_id,
                "setup_status": "authority_rejected",
                "fsm_state": "SETUP_REQUIRED",
                "reason": request.reason,
            },
            "warnings": [],
            "errors": [],
        }


def _install_fake_authority_application(
    monkeypatch: pytest.MonkeyPatch,
    app: FakeAuthorityApplication,
) -> None:
    """Patch API authority routes to use a fake application facade."""
    monkeypatch.setattr(
        api_module,
        "AgentWorkbenchApplication",
        lambda: app,
        raising=False,
    )


def test_get_dashboard_config_returns_setup_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the setup-first workflow state in dashboard config."""
    client, _, _ = _build_client(monkeypatch)

    response = client.get("/api/dashboard/config")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    steps = payload["data"]["workflow_steps"]

    assert steps[0]["id"] == "setup"
    assert steps[0]["states"] == ["SETUP_REQUIRED"]


def test_root_redirects_to_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect the root endpoint to the dashboard."""
    client, _, _ = _build_client(monkeypatch)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == HTTP_TEMP_REDIRECT
    assert response.headers["location"] == "/dashboard"


def test_create_project_requires_spec_file_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject project creation without a specification file path."""
    client, _, _ = _build_client(monkeypatch)

    response = client.post("/api/projects", json={"name": "Alpha"})
    assert response.status_code == HTTP_UNPROCESSABLE


def test_create_project_success_advances_to_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advance to vision interviewing after successful project setup."""
    client, _, workflow = _build_client(monkeypatch)

    response = client.post(
        "/api/projects",
        json={"name": "Project Alpha", "spec_file_path": __file__},
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["setup_status"] == "passed"
    assert payload["data"]["fsm_state"] == "VISION_INTERVIEW"
    assert payload["data"]["vision_auto_run"]["attempted"] is True
    assert payload["data"]["vision_auto_run"]["success"] is True
    assert payload["data"]["vision_auto_run"]["is_complete"] is False

    assert workflow.states["1"]["fsm_state"] == "VISION_INTERVIEW"


def test_get_projects_uses_batch_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use batch workflow status lookup when listing projects."""
    client, repo, workflow = _build_client(monkeypatch)

    repo.products = [
        DummyProduct(
            product_id=1,
            name="Alpha",
            spec_file_path=__file__,
            compiled_authority_json='{"ok": true}',
        ),
        DummyProduct(product_id=2, name="Beta", description="Second project"),
    ]
    workflow.states = {
        "1": {"fsm_state": "VISION_INTERVIEW", "setup_status": "passed"},
        "2": {"fsm_state": "SETUP_REQUIRED", "setup_status": "failed"},
    }

    response = client.get("/api/projects")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert [item["id"] for item in payload["data"]] == [1, 2]
    assert payload["data"][0]["fsm_state"] == "VISION_INTERVIEW"
    assert payload["data"][1]["summary"] == "Second project"
    assert workflow.batch_calls == [["1", "2"]]
    assert workflow.single_calls == []


def test_create_project_returns_500_when_repository_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return 500 when the repository returns a product without an ID."""
    client, repo, _workflow = _build_client(monkeypatch)

    @dataclass
    class BrokenDummyProduct:
        """Broken product double that simulates a missing primary key."""

        product_id: int | None
        name: str
        description: str | None = None
        vision: str | None = None
        spec_file_path: str | None = None
        compiled_authority_json: str | None = None

    def create_without_id(
        name: str,
        description: str | None = None,
    ) -> BrokenDummyProduct:
        product = BrokenDummyProduct(
            product_id=None,
            name=name,
            description=description,
        )
        repo.products.append(cast("DummyProduct", product))
        return product

    monkeypatch.setattr(repo, "create", create_without_id)

    response = client.post(
        "/api/projects",
        json={"name": "Broken Project", "spec_file_path": __file__},
    )

    assert response.status_code == HTTP_SERVER_ERROR
    assert response.json()["detail"] == "Failed to create project"


def test_create_project_setup_fail_and_retry_same_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry setup on the same project after a setup-phase failure."""
    client, repo, _workflow = _build_client(monkeypatch)

    create_response = client.post(
        "/api/projects",
        json={"name": "Project Retry", "spec_file_path": "invalid/path.md"},
    )
    assert create_response.status_code == HTTP_OK

    create_payload = create_response.json()
    assert create_payload["data"]["setup_status"] == "failed"
    assert create_payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert create_payload["data"]["vision_auto_run"]["attempted"] is False

    product = repo.get_by_id(create_payload["data"]["id"])
    assert product is not None

    retry_response = client.post(
        f"/api/projects/{product.product_id}/setup/retry",
        json={"spec_file_path": __file__},
    )
    assert retry_response.status_code == HTTP_OK

    retry_payload = retry_response.json()
    assert retry_payload["data"]["setup_status"] == "passed"
    assert retry_payload["data"]["fsm_state"] == "VISION_INTERVIEW"


def test_get_project_state_preserves_specific_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve setup failure metadata in the project-state endpoint."""
    client, _repo, _workflow = _build_client(monkeypatch)

    create_response = client.post(
        "/api/projects",
        json={"name": "Project Retry", "spec_file_path": "invalid/path.md"},
    )
    assert create_response.status_code == HTTP_OK

    project_id = create_response.json()["data"]["id"]

    state_response = client.get(f"/api/projects/{project_id}/state")
    assert state_response.status_code == HTTP_OK

    payload = state_response.json()
    assert payload["data"]["setup_status"] == "failed"
    assert payload["data"]["setup_error"] == "invalid spec path"
    assert payload["data"]["setup_failure_artifact_id"] == "setup-artifact-1"
    assert payload["data"]["setup_failure_stage"] == "output_validation"
    assert payload["data"]["setup_has_full_artifact"] is True


def test_project_state_preserves_authority_pending_review_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep pending authority review as setup-required, not failed setup."""
    client, repo, workflow = _build_client(monkeypatch)

    product = repo.create("Pending Authority")
    product.spec_file_path = __file__
    workflow.states[str(product.product_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "authority_pending_review",
    }

    response = client.get(f"/api/projects/{product.product_id}/state")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert payload["data"]["setup_status"] == "authority_pending_review"
    assert payload["data"]["setup_error"] is None
    assert payload["data"]["setup_failure_summary"] is None


def test_dashboard_authority_review_endpoint_returns_review_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the pending authority review token through the dashboard API."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.get(f"/api/projects/{product.product_id}/authority/review")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["guard_tokens"]["review_token"].startswith(
        "agileforge.authority_review.v1:sha256:"
    )


def test_dashboard_accept_requires_review_token_or_full_guard_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject dashboard accept requests without a token or full guards."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/accept",
        json={},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert fake_app.accept_requests == []
    assert (
        response.json()["detail"]["errors"][0]["code"]
        == "AUTHORITY_GUARD_INCOMPLETE"
    )


def test_dashboard_accept_rejects_fingerprint_only_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject authority-fingerprint-only dashboard decision guards."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/accept",
        json={"expected_authority_fingerprint": "sha256:test"},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert fake_app.accept_requests == []
    assert (
        response.json()["detail"]["errors"][0]["code"]
        == "AUTHORITY_GUARD_INCOMPLETE"
    )


def test_dashboard_reject_records_reason_and_keeps_vision_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send rejection reason through the app facade and keep setup locked."""
    client, repo, workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication(workflow=workflow)
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")
    reason = "The generated invariant omits the audit trail requirement."

    response = client.post(
        f"/api/projects/{product.product_id}/authority/reject",
        json={
            "review_token": "agileforge.authority_review.v1:sha256:test",
            "reason": reason,
        },
    )

    assert response.status_code == HTTP_OK
    assert response.json()["data"]["reason"] == reason
    assert len(fake_app.reject_requests) == 1
    request = fake_app.reject_requests[0]
    assert request.reason == reason
    assert request.policy == "dashboard_manual"
    assert request.actor_mode == "dashboard-human"
    assert workflow.states[str(product.product_id)]["fsm_state"] == "SETUP_REQUIRED"
    assert (
        workflow.states[str(product.product_id)]["setup_status"]
        == "authority_rejected"
    )
    assert workflow.states[str(product.product_id)]["setup_error"] == reason


def test_dashboard_reject_empty_reason_returns_request_boundary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an empty authority rejection reason before app service dispatch."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/reject",
        json={
            "review_token": "agileforge.authority_review.v1:sha256:test",
            "reason": "",
        },
    )

    assert response.status_code == HTTP_UNPROCESSABLE
    assert fake_app.reject_requests == []
    errors = response.json()["detail"]
    assert errors[0]["loc"] == ["body", "reason"]
    assert errors[0]["type"] == "string_too_short"


def test_dashboard_pending_review_copy_is_not_project_setup_required() -> None:
    """Keep the dashboard pending review panel copy authority-specific."""
    html = Path("frontend/project.html").read_text()
    marker = 'id="authority-review-card"'

    assert marker in html
    review_card = html[html.index(marker) : html.index(marker) + 1200]
    assert "Pending Authority Review" in review_card
    assert "Project Setup Required" not in review_card


def test_state_forces_setup_required_when_product_missing_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force setup-required when a stored product has no persisted spec."""
    client, repo, workflow = _build_client(monkeypatch)

    product = repo.create("Legacy")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "VISION_REVIEW",
        "setup_status": "passed",
    }

    response = client.get(f"/api/projects/{product.product_id}/state")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert payload["data"]["setup_status"] == "failed"


def test_create_project_auto_vision_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record vision auto-run failure metadata during project creation."""
    client, _, workflow = _build_client(monkeypatch)

    async def failing_auto_vision(
        state: dict[str, object],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, object]:
        del project_id
        return {
            "success": False,
            "input_context": {
                "user_raw_text": user_input or "",
                "prior_vision_state": "NO_HISTORY",
                "specification_content": state.get("pending_spec_content", "SPEC"),
                "compiled_authority": state.get(
                    "compiled_authority_cached", '{"ok": true}'
                ),
            },
            "output_artifact": {
                "error": "VISION_GENERATION_FAILED",
                "message": "provider error",
                "is_complete": False,
                "clarifying_questions": [],
            },
            "is_complete": None,
            "error": "provider error",
            "failure_artifact_id": "vision-auto-failure",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider error",
            "raw_output_preview": '{"partial": true}',
            "has_full_artifact": True,
        }

    monkeypatch.setattr(api_module, "run_vision_agent_from_state", failing_auto_vision)

    response = client.post(
        "/api/projects",
        json={"name": "Project Auto Fail", "spec_file_path": __file__},
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["data"]["setup_status"] == "passed"
    assert payload["data"]["fsm_state"] == "VISION_INTERVIEW"
    assert payload["data"]["vision_auto_run"]["attempted"] is True
    assert payload["data"]["vision_auto_run"]["success"] is False
    assert payload["data"]["vision_auto_run"]["is_complete"] is None
    assert (
        payload["data"]["vision_auto_run"]["failure_artifact_id"]
        == "vision-auto-failure"
    )

    history = workflow.states["1"]["vision_attempts"]
    assert isinstance(history, list)
    assert len(history) == 1
    first_attempt = history[0]
    assert isinstance(first_attempt, dict)
    first_attempt = cast("dict[str, object]", first_attempt)
    assert first_attempt["trigger"] == "auto_setup_transition"
    assert first_attempt["is_complete"] is False
    assert first_attempt["failure_artifact_id"] == "vision-auto-failure"


def test_create_project_setup_failure_exposes_failure_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose setup failure metadata on the project-creation response."""
    client, _, workflow = _build_client(monkeypatch)

    response = client.post(
        "/api/projects",
        json={"name": "Project Retry", "spec_file_path": "invalid/path.md"},
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["data"]["setup_status"] == "failed"
    assert payload["data"]["failure_artifact_id"] == "setup-artifact-1"
    assert payload["data"]["failure_stage"] == "output_validation"
    assert payload["data"]["has_full_artifact"] is True
    assert workflow.states["1"]["setup_failure_artifact_id"] == "setup-artifact-1"
