"""API tests for roadmap generation, history, and save flow."""

from dataclasses import dataclass
from typing import Never, Protocol, cast

import pytest
from fastapi.testclient import TestClient

import api as api_module
from orchestrator_agent.agent_tools.roadmap_builder.tools import SaveRoadmapToolInput

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409
HTTP_TEAPOT = 418
EXPECTED_HISTORY_COUNT = 2


class _StateContext(Protocol):
    """Minimal tool context shape used by the route shims."""

    state: dict[str, object]


@dataclass
class DummyProduct:
    """Simple in-memory product used by roadmap flow tests."""

    product_id: int
    name: str
    description: str | None = None
    vision: str | None = None
    spec_file_path: str | None = None
    compiled_authority_json: str | None = None


class DummyProductRepository:
    """Tiny repository double for roadmap API tests."""

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
    """Workflow-state double used by the roadmap route tests."""

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

    async def fake_run_roadmap_agent_from_state(
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
                    "user_input": user_input or "",
                    "product_vision": "A clear vision",
                    "technical_spec": "SPEC",
                    "compiled_authority": '{"ok": true}',
                    "prior_roadmap_state": "NO_HISTORY",
                },
                "output_artifact": {
                    "error": "ROADMAP_GENERATION_FAILED",
                    "message": "provider timeout",
                    "is_complete": False,
                    "clarifying_questions": [],
                },
                "is_complete": None,
                "error": "provider timeout",
                "failure_artifact_id": "roadmap-failure-1",
                "failure_stage": "invocation_exception",
                "failure_summary": "provider timeout",
                "raw_output_preview": '{"partial": true}',
                "has_full_artifact": True,
            }

        is_complete = normalized.startswith("complete")
        return {
            "success": True,
            "input_context": {
                "user_input": user_input or "",
                "product_vision": "A clear vision",
                "technical_spec": "SPEC",
                "compiled_authority": '{"ok": true}',
                "prior_roadmap_state": "NO_HISTORY",
            },
            "output_artifact": {
                "roadmap_releases": [
                    {
                        "release_name": "Milestone 1",
                        "theme": "Foundation",
                        "focus_area": "Technical Foundation",
                        "items": ["Seed backlog item"],
                        "reasoning": "Start here",
                    }
                ],
                "roadmap_summary": "Draft roadmap",
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
        "run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )

    def fake_save_roadmap_tool(
        roadmap_input: SaveRoadmapToolInput,
        tool_context: object | None,
    ) -> dict[str, object]:
        del tool_context
        return {
            "success": True,
            "product_id": roadmap_input.product_id,
            "message": "saved",
            "releases_count": len(roadmap_input.roadmap_data.roadmap_releases),
        }

    monkeypatch.setattr(api_module, "save_roadmap_tool", fake_save_roadmap_tool)

    return TestClient(api_module.app), repo, workflow


def _seed_backlog_persisted_project(
    repo: DummyProductRepository,
    workflow: DummyWorkflowService,
) -> int:
    """Create a project that is ready to enter the roadmap phase."""
    product = repo.create("Roadmap Project")
    product.spec_file_path = __file__
    product.compiled_authority_json = '{"ok": true}'
    workflow.states[str(product.product_id)] = {
        "fsm_state": "BACKLOG_PERSISTENCE",
        "product_vision_assessment": {
            "product_vision_statement": "A clear vision",
            "is_complete": True,
        },
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Seed backlog item",
                "value_driver": "Strategic",
                "justification": "Start here",
                "estimated_effort": "M",
            }
        ],
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
    }
    return product.product_id


def _roadmap_save_body(payload: dict[str, object]) -> dict[str, object]:
    data = payload["data"]
    assert isinstance(data, dict)
    return {
        "attempt_id": data["attempt_id"],
        "expected_artifact_fingerprint": data["artifact_fingerprint"],
        "expected_state": "ROADMAP_REVIEW",
        "idempotency_key": "save-roadmap-api-1",
    }


def test_roadmap_generate_allows_empty_input_on_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow an empty first roadmap-generation request during auto-transition."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    response = client.post(f"/api/projects/{project_id}/roadmap/generate", json={})
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["trigger"] == "auto_transition"
    assert payload["data"]["fsm_state"] == "ROADMAP_INTERVIEW"
    project_state = workflow.states[str(project_id)]
    roadmap_attempts = project_state.get("roadmap_attempts")
    assert isinstance(roadmap_attempts, list)
    first_attempt = roadmap_attempts[0]
    assert isinstance(first_attempt, dict)
    first_attempt = cast("dict[str, object]", first_attempt)
    assert first_attempt["trigger"] == "auto_transition"


def test_roadmap_generate_requires_feedback_after_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require feedback after the first empty roadmap-generation attempt."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    first_response = client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={},
    )
    assert first_response.status_code == HTTP_OK

    second_response = client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={},
    )
    assert second_response.status_code == HTTP_BAD_REQUEST
    assert second_response.json()["detail"] == (
        "User input is required to refine an existing roadmap."
    )


def test_roadmap_generate_translates_phase_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate roadmap phase errors into the matching HTTP response."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    async def failing_service(**_kwargs: object) -> Never:
        error_message = "service boom"
        raise api_module.RoadmapPhaseError(error_message, status_code=HTTP_TEAPOT)

    monkeypatch.setattr(
        api_module,
        "generate_roadmap_draft_service",
        failing_service,
    )

    response = client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={"user_input": "complete this"},
    )
    assert response.status_code == HTTP_TEAPOT
    assert response.json()["detail"] == "service boom"


def test_roadmap_generate_failed_run_cannot_mark_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the roadmap phase incomplete when the generator reports failure."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    async def failed_runtime(
        state: dict[str, object],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, object]:
        del state, project_id
        return {
            "success": False,
            "input_context": {"user_input": user_input or ""},
            "output_artifact": {
                "error": "ROADMAP_GENERATION_FAILED",
                "message": "provider timeout",
                "is_complete": True,
                "clarifying_questions": [],
            },
            "is_complete": True,
            "error": "provider timeout",
            "failure_artifact_id": "roadmap-failure-1",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider timeout",
            "raw_output_preview": '{"partial": true}',
            "has_full_artifact": True,
        }

    monkeypatch.setattr(
        api_module,
        "run_roadmap_agent_from_state",
        failed_runtime,
    )

    response = client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={"user_input": "complete this roadmap"},
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["data"]["roadmap_run_success"] is False
    assert payload["data"]["is_complete"] is False
    assert payload["data"]["fsm_state"] == "ROADMAP_INTERVIEW"


def test_roadmap_history_endpoint_returns_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return stored roadmap-generation attempts from the history endpoint."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={},
    )
    client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={"user_input": "complete this roadmap"},
    )

    response = client.get(f"/api/projects/{project_id}/roadmap/history")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["count"] == EXPECTED_HISTORY_COUNT
    assert payload["data"]["items"][0]["trigger"] == "auto_transition"


def test_roadmap_save_succeeds_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist roadmap data once the roadmap assessment is complete."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    generate_response = client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={"user_input": "complete this roadmap"},
    )

    response = client.post(
        f"/api/projects/{project_id}/roadmap/save",
        json=_roadmap_save_body(generate_response.json()),
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["fsm_state"] == "ROADMAP_PERSISTENCE"
    assert workflow.states[str(project_id)]["fsm_state"] == "ROADMAP_PERSISTENCE"


def test_roadmap_save_rejects_incomplete_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject roadmap persistence until the assessment is complete."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    workflow.states[str(project_id)]["product_roadmap_assessment"] = {
        "roadmap_releases": [
            {
                "release_name": "Milestone 1",
                "theme": "Foundation",
                "focus_area": "Technical Foundation",
                "items": ["Seed backlog item"],
                "reasoning": "Start here",
            }
        ],
        "roadmap_summary": "Draft roadmap",
        "is_complete": False,
        "clarifying_questions": ["Need more detail"],
        "attempt_id": "roadmap-attempt-1",
        "artifact_fingerprint": "sha256:" + "a" * 64,
    }
    workflow.states[str(project_id)]["roadmap_attempts"] = [
        {
            "attempt_id": "roadmap-attempt-1",
            "artifact_fingerprint": "sha256:" + "a" * 64,
        }
    ]
    workflow.states[str(project_id)]["fsm_state"] = "ROADMAP_REVIEW"

    response = client.post(
        f"/api/projects/{project_id}/roadmap/save",
        json={
            "attempt_id": "roadmap-attempt-1",
            "expected_artifact_fingerprint": "sha256:" + "a" * 64,
            "expected_state": "ROADMAP_REVIEW",
            "idempotency_key": "save-roadmap-api-1",
        },
    )
    assert response.status_code == HTTP_CONFLICT
    assert response.json()["detail"] == (
        "Roadmap cannot be saved until is_complete is true"
    )


def test_roadmap_save_requires_review_attempt_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject unguarded Roadmap save requests at the API boundary."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_backlog_persisted_project(repo, workflow)

    client.post(
        f"/api/projects/{project_id}/roadmap/generate",
        json={"user_input": "complete this roadmap"},
    )

    response = client.post(f"/api/projects/{project_id}/roadmap/save", json={})

    assert response.status_code == 422  # noqa: PLR2004
