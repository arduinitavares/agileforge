"""API tests for vision interview endpoints."""

from dataclasses import dataclass
from pathlib import Path
from typing import Never, Protocol, cast

import pytest
from fastapi.testclient import TestClient

import api as api_module
from orchestrator_agent.agent_tools.product_vision_tool.tools import SaveVisionInput
from utils import failure_artifacts

COMPILED_AUTHORITY_JSON = '{"schema_version":"agileforge.compiled_authority.v2"}'


@dataclass
class DummyProduct:
    """Test helper for dummy product."""

    product_id: int
    name: str
    description: str | None = None
    vision: str | None = None
    spec_file_path: str | None = None
    compiled_authority_json: str | None = None


class StateContext(Protocol):
    """Minimal context protocol with mutable ADK state."""

    state: dict[str, object]


class DummyProductRepository:
    """Test helper for dummy product repository."""

    def __init__(self) -> None:
        """Initialize the test helper."""
        self.products: list[DummyProduct] = []

    def get_all(self) -> list[DummyProduct]:
        """Return get all."""
        return list(self.products)

    def get_by_id(self, product_id: int) -> DummyProduct | None:
        """Return get by id."""
        for product in self.products:
            if product.product_id == product_id:
                return product
        return None

    def create(self, name: str, description: str | None = None) -> DummyProduct:
        """Return create."""
        product = DummyProduct(
            product_id=len(self.products) + 1,
            name=name,
            description=description,
        )
        self.products.append(product)
        return product


class DummyWorkflowService:
    """Test helper for dummy workflow service."""

    def __init__(self) -> None:
        """Initialize the test helper."""
        self.states: dict[str, dict[str, object]] = {}

    async def initialize_session(self, session_id: str | None = None) -> str:
        """Return initialize session."""
        sid = str(session_id or "generated")
        self.states[sid] = {"fsm_state": "SETUP_REQUIRED"}
        return sid

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return get session status."""
        return dict(self.states.get(str(session_id), {}))

    def update_session_status(
        self, session_id: str, partial_update: dict[str, object]
    ) -> None:
        """Return update session status."""
        sid = str(session_id)
        current = dict(self.states.get(sid, {}))
        current.update(partial_update)
        self.states[sid] = current

    def migrate_legacy_setup_state(self) -> int:
        """Return migrate legacy setup state."""
        return 0


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, DummyProductRepository, DummyWorkflowService]:
    repo = DummyProductRepository()
    workflow = DummyWorkflowService()

    monkeypatch.setattr(api_module, "product_repo", repo)
    monkeypatch.setattr(api_module, "workflow_service", workflow)

    def fake_select_project(
        product_id: int, context: StateContext
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
        context.state.setdefault("compiled_authority_cached", COMPILED_AUTHORITY_JSON)
        return {"success": True}

    monkeypatch.setattr(api_module, "select_project", fake_select_project)

    async def fake_run_vision_agent_from_state(
        state: dict[str, object], *, project_id: int, user_input: str | None
    ) -> dict[str, object]:
        del project_id
        normalized = (user_input or "").lower().strip()
        if normalized == "force-runtime-error":
            return {
                "success": False,
                "input_context": {
                    "user_raw_text": user_input or "",
                    "prior_vision_state": "NO_HISTORY",
                    "specification_content": state.get("pending_spec_content", "SPEC"),
                    "compiled_authority": state.get(
                        "compiled_authority_cached", COMPILED_AUTHORITY_JSON
                    ),
                },
                "output_artifact": {
                    "error": "VISION_GENERATION_FAILED",
                    "message": "provider timeout",
                    "is_complete": False,
                    "clarifying_questions": [],
                },
                "is_complete": None,
                "error": "provider timeout",
                "failure_artifact_id": "vision-failure-1",
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
                "prior_vision_state": "NO_HISTORY",
                "specification_content": state.get("pending_spec_content", "SPEC"),
                "compiled_authority": state.get(
                    "compiled_authority_cached", COMPILED_AUTHORITY_JSON
                ),
            },
            "output_artifact": {
                "updated_components": {
                    "project_name": "Vision Project",
                    "target_user": "Users",
                    "problem": "Problem",
                    "product_category": "App",
                    "key_benefit": "Benefit",
                    "competitors": "Competitors",
                    "differentiator": "Diff",
                },
                "product_vision_statement": f"Statement for: {user_input}",
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
        api_module, "run_vision_agent_from_state", fake_run_vision_agent_from_state
    )

    def fake_save_vision_tool(
        vision_input: SaveVisionInput, tool_context: object
    ) -> dict[str, object]:
        del tool_context
        return {
            "success": True,
            "product_id": vision_input.product_id,
            "project_name": vision_input.project_name,
            "message": "saved",
        }

    monkeypatch.setattr(api_module, "save_vision_tool", fake_save_vision_tool)

    return TestClient(api_module.app), repo, workflow


def _seed_setup_passed_project(
    repo: DummyProductRepository, workflow: DummyWorkflowService
) -> int:
    product = repo.create("Vision Project")
    product.spec_file_path = __file__
    product.compiled_authority_json = COMPILED_AUTHORITY_JSON
    workflow.states[str(product.product_id)] = {
        "fsm_state": "VISION_INTERVIEW",
        "setup_status": "passed",
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": COMPILED_AUTHORITY_JSON,
    }
    return product.product_id


def test_generate_first_attempt_allows_empty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generate first attempt allows empty input."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={},
    )
    assert response.status_code == 200  # noqa: PLR2004

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["trigger"] == "initial_generate"
    assert payload["data"]["is_complete"] is False
    assert payload["data"]["fsm_state"] == "VISION_INTERVIEW"

    history = workflow.states[str(project_id)]["vision_attempts"]
    assert isinstance(history, list)
    assert len(history) == 1
    first_attempt = cast("dict[str, object]", history[0])
    assert first_attempt["trigger"] == "initial_generate"


def test_generate_refine_requires_feedback_after_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generate refine requires feedback after first attempt."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    first_response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={},
    )
    assert first_response.status_code == 200  # noqa: PLR2004

    second_response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={},
    )
    assert second_response.status_code == 409  # noqa: PLR2004
    assert "Feedback is required" in second_response.json()["detail"]


def test_generate_incomplete_stays_in_interview_and_records_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generate incomplete stays in interview and records history."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "needs refinement"},
    )
    assert response.status_code == 200  # noqa: PLR2004

    payload = response.json()
    assert payload["data"]["is_complete"] is False
    assert payload["data"]["fsm_state"] == "VISION_INTERVIEW"

    history = workflow.states[str(project_id)]["vision_attempts"]
    assert isinstance(history, list)
    assert len(history) == 1
    first_attempt = cast("dict[str, object]", history[0])
    assert first_attempt["trigger"] == "manual_refine"


def test_generate_complete_moves_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify generate complete moves to review."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "complete this vision"},
    )
    assert response.status_code == 200  # noqa: PLR2004

    payload = response.json()
    assert payload["data"]["is_complete"] is True
    assert payload["data"]["fsm_state"] == "VISION_REVIEW"


def test_generate_error_records_attempt_and_stays_interview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generate error records attempt and stays interview."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "force-runtime-error"},
    )
    assert response.status_code == 200  # noqa: PLR2004

    payload = response.json()
    assert payload["data"]["vision_run_success"] is False
    assert payload["data"]["is_complete"] is False
    assert payload["data"]["fsm_state"] == "VISION_INTERVIEW"

    history = workflow.states[str(project_id)]["vision_attempts"]
    assert isinstance(history, list)
    assert len(history) == 1
    first_attempt = cast("dict[str, object]", history[0])
    assert first_attempt["is_complete"] is False
    assert first_attempt["failure_artifact_id"] == "vision-failure-1"
    assert first_attempt["raw_output_preview"] == '{"partial": true}'


def test_generate_error_history_stays_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify generate error history stays compact."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "force-runtime-error"},
    )
    assert response.status_code == 200  # noqa: PLR2004

    history_response = client.get(f"/api/projects/{project_id}/vision/history")
    assert history_response.status_code == 200  # noqa: PLR2004

    item = history_response.json()["data"]["items"][0]
    assert item["failure_artifact_id"] == "vision-failure-1"
    assert item["failure_stage"] == "invocation_exception"
    assert item["failure_summary"] == "provider timeout"
    assert item["raw_output_preview"] == '{"partial": true}'
    assert item["has_full_artifact"] is True
    assert "raw_output" not in item["output_artifact"]


def test_generate_route_translates_vision_phase_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generate route translates vision phase error."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    async def failing_service(**_kwargs: object) -> Never:
        msg = "service boom"
        raise api_module.VisionPhaseError(msg, status_code=418)

    monkeypatch.setattr(
        api_module,
        "generate_vision_draft_service",
        failing_service,
    )

    response = client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "complete this"},
    )
    assert response.status_code == 418  # noqa: PLR2004
    assert response.json()["detail"] == "service boom"


def test_vision_history_endpoint_returns_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify vision history endpoint returns attempts."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={},
    )
    client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "complete pass"},
    )

    response = client.get(f"/api/projects/{project_id}/vision/history")
    assert response.status_code == 200  # noqa: PLR2004

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["count"] == 2  # noqa: PLR2004
    items = payload["data"]["items"]
    assert [item.get("trigger") for item in items] == [
        "initial_generate",
        "manual_refine",
    ]


def test_save_vision_rejects_when_latest_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify save vision rejects when latest is incomplete."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "still incomplete"},
    )

    response = client.post(f"/api/projects/{project_id}/vision/save")
    assert response.status_code == 409  # noqa: PLR2004


def test_save_vision_succeeds_when_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify save vision succeeds when complete."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)

    client.post(
        f"/api/projects/{project_id}/vision/generate",
        json={"user_input": "complete this now"},
    )

    response = client.post(f"/api/projects/{project_id}/vision/save")
    assert response.status_code == 200  # noqa: PLR2004

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["fsm_state"] == "VISION_PERSISTENCE"
    assert workflow.states[str(project_id)]["fsm_state"] == "VISION_PERSISTENCE"


def test_debug_failure_endpoint_returns_full_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify debug failure endpoint returns full artifact."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)
    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        failure_artifacts, "FAILURES_DIR", tmp_path / "logs" / "failures"
    )

    persisted = failure_artifacts.write_failure_artifact(
        phase="vision",
        project_id=project_id,
        failure_stage="invalid_json",
        failure_summary="Vision response is not valid JSON",
        raw_output='{"broken": ',
        context={"input_context": {"user_raw_text": ""}},
        model_info={"model_id": "openai/gpt-5-mini"},
    )

    artifact_id = persisted["metadata"]["failure_artifact_id"]
    response = client.get(f"/api/projects/{project_id}/debug/failures/{artifact_id}")
    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["artifact_id"] == artifact_id
    assert payload["raw_output"] == '{"broken": '


def test_debug_failure_endpoint_rejects_other_project_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify debug failure endpoint rejects other project artifact."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_setup_passed_project(repo, workflow)
    other_project = repo.create("Other")
    workflow.states[str(other_project.product_id)] = {
        "fsm_state": "VISION_INTERVIEW",
        "setup_status": "passed",
    }

    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        failure_artifacts, "FAILURES_DIR", tmp_path / "logs" / "failures"
    )
    persisted = failure_artifacts.write_failure_artifact(
        phase="vision",
        project_id=other_project.product_id,
        failure_stage="invalid_json",
        failure_summary="Vision response is not valid JSON",
        raw_output='{"broken": ',
    )

    artifact_id = persisted["metadata"]["failure_artifact_id"]
    response = client.get(f"/api/projects/{project_id}/debug/failures/{artifact_id}")
    assert response.status_code == 404  # noqa: PLR2004
