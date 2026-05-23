"""API tests for backlog generation, history, and save flow."""

from dataclasses import dataclass
from typing import Never, Protocol, cast

import pytest
from fastapi.testclient import TestClient

import api as api_module
from orchestrator_agent.agent_tools.backlog_primer.tools import SaveBacklogInput

HTTP_OK = 200
HTTP_CONFLICT = 409
HTTP_TEAPOT = 418
EXPECTED_HISTORY_COUNT = 2


class _StateContext(Protocol):
    """Minimal tool context shape used by the route shims."""

    state: dict[str, object]


@dataclass
class DummyProduct:
    """Simple in-memory product used by backlog flow tests."""

    product_id: int
    name: str
    description: str | None = None
    vision: str | None = None
    spec_file_path: str | None = None
    compiled_authority_json: str | None = None


class DummyProductRepository:
    """Tiny repository double for backlog API tests."""

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
    """Workflow-state double used by the backlog route tests."""

    def __init__(self) -> None:
        """Initialize the in-memory workflow state store."""
        self.states: dict[str, dict[str, object]] = {}

    async def initialize_session(self, session_id: str | None = None) -> str:
        """Create a session with the setup-required FSM state."""
        sid = str(session_id or "generated")
        self.states[sid] = {"fsm_state": "SETUP_REQUIRED"}
        return sid

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return a shallow copy of the stored session state."""
        return dict(self.states.get(str(session_id), {}))

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
        """Pretend there are no legacy sessions to migrate."""
        return 0


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
        context.state.setdefault("pending_spec_content", "SPEC")
        context.state.setdefault("compiled_authority_cached", '{"ok": true}')
        return {"success": True}

    monkeypatch.setattr(api_module, "select_project", fake_select_project)

    async def fake_run_backlog_agent_from_state(
        state: dict[str, object],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, object]:
        del state, project_id
        normalized = (user_input or "").lower().strip()
        if normalized == "force-runtime-error":
            return {
                "success": False,
                "input_context": {
                    "user_raw_text": user_input or "",
                    "product_vision_statement": "A clear vision",
                    "technical_spec": "SPEC",
                    "compiled_authority": '{"ok": true}',
                    "prior_backlog_state": "NO_HISTORY",
                },
                "output_artifact": {
                    "error": "BACKLOG_GENERATION_FAILED",
                    "message": "provider timeout",
                    "is_complete": False,
                    "clarifying_questions": [],
                },
                "is_complete": None,
                "error": "provider timeout",
                "failure_artifact_id": "backlog-failure-1",
                "failure_stage": "invocation_exception",
                "failure_summary": "provider timeout",
                "raw_output_preview": '{"partial": true}',
                "has_full_artifact": True,
            }

        is_complete = normalized.startswith("complete")
        return {
            "success": True,
            "input_context": {
                "user_raw_text": user_input or "",
                "product_vision_statement": "A clear vision",
                "technical_spec": "SPEC",
                "compiled_authority": '{"ok": true}',
                "prior_backlog_state": "NO_HISTORY",
            },
            "output_artifact": {
                "backlog_items": [{"title": "Seed backlog item"}],
                "is_complete": is_complete,
                "clarifying_questions": [] if is_complete else ["Need more detail"],
            },
            "is_complete": is_complete,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    monkeypatch.setattr(
        api_module,
        "run_backlog_agent_from_state",
        fake_run_backlog_agent_from_state,
    )

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object | None,
    ) -> dict[str, object]:
        del tool_context
        return {
            "success": True,
            "product_id": backlog_input.product_id,
            "saved_count": len(backlog_input.backlog_items),
            "message": "saved",
        }

    monkeypatch.setattr(api_module, "save_backlog_tool", fake_save_backlog_tool)

    return TestClient(api_module.app), repo, workflow


def _seed_vision_persisted_project(
    repo: DummyProductRepository, workflow: DummyWorkflowService
) -> int:
    product = repo.create("Backlog Project")
    product.spec_file_path = __file__
    product.compiled_authority_json = '{"ok": true}'
    workflow.states[str(product.product_id)] = {
        "fsm_state": "VISION_PERSISTENCE",
        "product_vision_assessment": {
            "product_vision_statement": "A clear vision",
            "is_complete": True,
        },
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
    }
    return product.product_id


def test_backlog_generate_allows_empty_input_on_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow an empty first backlog-generation request during auto-transition."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_vision_persisted_project(repo, workflow)

    response = client.post(f"/api/projects/{project_id}/backlog/generate", json={})
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["trigger"] == "auto_transition"
    assert payload["data"]["fsm_state"] == "BACKLOG_INTERVIEW"
    project_state = workflow.states[str(project_id)]
    backlog_attempts = project_state.get("backlog_attempts")
    assert isinstance(backlog_attempts, list)
    first_attempt = backlog_attempts[0]
    assert isinstance(first_attempt, dict)
    first_attempt = cast("dict[str, object]", first_attempt)
    assert first_attempt["trigger"] == "auto_transition"


def test_backlog_generate_requires_feedback_after_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require feedback after the first empty backlog-generation attempt."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_vision_persisted_project(repo, workflow)

    first_response = client.post(
        f"/api/projects/{project_id}/backlog/generate",
        json={},
    )
    assert first_response.status_code == HTTP_OK

    second_response = client.post(
        f"/api/projects/{project_id}/backlog/generate",
        json={},
    )
    assert second_response.status_code == HTTP_CONFLICT
    assert "Feedback is required" in second_response.json()["detail"]


def test_backlog_generate_translates_phase_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate backlog phase errors into the matching HTTP response."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_vision_persisted_project(repo, workflow)

    async def failing_service(**_kwargs: object) -> Never:
        error_message = "service boom"
        raise api_module.BacklogPhaseError(error_message, status_code=HTTP_TEAPOT)

    monkeypatch.setattr(
        api_module,
        "generate_backlog_draft_service",
        failing_service,
    )

    response = client.post(
        f"/api/projects/{project_id}/backlog/generate",
        json={"user_input": "complete this"},
    )
    assert response.status_code == HTTP_TEAPOT
    assert response.json()["detail"] == "service boom"


def test_backlog_history_endpoint_returns_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return stored backlog-generation attempts from the history endpoint."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_vision_persisted_project(repo, workflow)

    client.post(
        f"/api/projects/{project_id}/backlog/generate",
        json={},
    )
    client.post(
        f"/api/projects/{project_id}/backlog/generate",
        json={"user_input": "complete this now"},
    )

    response = client.get(f"/api/projects/{project_id}/backlog/history")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["count"] == EXPECTED_HISTORY_COUNT
    assert all(
        item.get("trigger") == "auto_transition"
        for item in payload["data"]["items"][:1]
    )


def test_backlog_save_succeeds_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist backlog items once the backlog assessment is complete."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_vision_persisted_project(repo, workflow)

    generate_response = client.post(
        f"/api/projects/{project_id}/backlog/generate",
        json={"user_input": "complete this backlog"},
    )
    draft = generate_response.json()["data"]

    response = client.post(
        f"/api/projects/{project_id}/backlog/save",
        json={
            "attempt_id": draft["attempt_id"],
            "expected_artifact_fingerprint": draft["artifact_fingerprint"],
            "expected_state": "BACKLOG_REVIEW",
            "idempotency_key": "save-backlog-api-1",
        },
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert workflow.states[str(project_id)]["fsm_state"] == "BACKLOG_PERSISTENCE"


def test_backlog_save_rejects_incomplete_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject backlog saving while the assessment is still incomplete."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_vision_persisted_project(repo, workflow)

    workflow.states[str(project_id)]["product_backlog_assessment"] = {
        "backlog_items": [{"title": "Seed backlog item"}],
        "is_complete": False,
        "attempt_id": "backlog-attempt-1",
        "artifact_fingerprint": "sha256:test",
    }
    workflow.states[str(project_id)]["backlog_attempts"] = [
        {
            "attempt_id": "backlog-attempt-1",
            "artifact_fingerprint": "sha256:test",
            "output_artifact": {
                "backlog_items": [{"title": "Seed backlog item"}],
                "is_complete": False,
            },
        }
    ]
    workflow.states[str(project_id)]["fsm_state"] = "BACKLOG_REVIEW"

    response = client.post(
        f"/api/projects/{project_id}/backlog/save",
        json={
            "attempt_id": "backlog-attempt-1",
            "expected_artifact_fingerprint": "sha256:test",
            "expected_state": "BACKLOG_REVIEW",
            "idempotency_key": "save-backlog-api-2",
        },
    )
    assert response.status_code == HTTP_CONFLICT
