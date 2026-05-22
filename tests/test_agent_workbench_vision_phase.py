"""Tests for agent workbench Vision phase runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.agent_workbench.vision_phase import VisionPhaseRunner


class _FakeProductRepo:
    """Fake product repo with setup-passed project data."""

    def get_by_id(self, product_id: int) -> SimpleNamespace:
        """Return a product-like object."""
        return SimpleNamespace(
            product_id=product_id,
            name="Cartola",
            spec_file_path="specs/spec.json",
            compiled_authority_json='{"authority": true}',
        )


class _FakeWorkflowService:
    """Fake workflow service with persisted session state."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "fsm_state": "VISION_INTERVIEW",
            "setup_status": "passed",
        }

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current state."""
        del session_id
        return dict(self.state)

    async def initialize_session(self, session_id: str) -> str:
        """No-op session initialization."""
        return session_id

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Persist state updates."""
        del session_id
        self.state.update(partial_update)


def test_vision_generate_hydrates_spec_and_authority_before_agent(
    monkeypatch: object,
) -> None:
    """Vision generate must pass spec and accepted authority to the agent."""
    captured: dict[str, Any] = {}

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_vision_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        captured["state"] = dict(state)
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {
                "specification_content": state.get("pending_spec_content"),
                "compiled_authority": state.get("compiled_authority_cached"),
                "user_raw_text": user_input or "",
                "prior_vision_state": "NO_HISTORY",
            },
            "output_artifact": {
                "is_complete": False,
                "clarifying_questions": ["Who is the primary operator?"],
            },
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.vision_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.vision_phase.run_vision_agent_from_state",
        fake_run_vision_agent_from_state,
    )
    runner = VisionPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2, user_input="draft vision")

    assert result["ok"] is True
    assert captured["state"]["pending_spec_content"] == "SPEC CONTENT"
    assert captured["state"]["compiled_authority_cached"] == "AUTHORITY JSON"
    assert result["data"]["input_context"]["specification_content"] == "SPEC CONTENT"
    assert result["data"]["input_context"]["compiled_authority"] == "AUTHORITY JSON"


def test_vision_generate_returns_failure_envelope_for_runtime_failure(
    monkeypatch: object,
) -> None:
    """Vision runtime failures must be loud to agent-facing CLI callers."""

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_vision_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": False,
            "error": "VISION_GENERATION_FAILED",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider rejected model",
            "failure_artifact_id": "vision-failure-1",
            "has_full_artifact": True,
            "input_context": {
                "specification_content": "SPEC CONTENT",
                "compiled_authority": "AUTHORITY JSON",
                "user_raw_text": "",
                "prior_vision_state": "NO_HISTORY",
            },
            "output_artifact": {
                "is_complete": False,
                "error": "VISION_GENERATION_FAILED",
                "failure_summary": "provider rejected model",
            },
            "is_complete": False,
        }

    monkeypatch.setattr(
        "services.agent_workbench.vision_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.vision_phase.run_vision_agent_from_state",
        fake_run_vision_agent_from_state,
    )
    runner = VisionPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["vision_run_success"] is False
    assert result["errors"][0]["details"]["failure_stage"] == "invocation_exception"
