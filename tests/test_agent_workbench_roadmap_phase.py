"""Tests for agent workbench Roadmap phase runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from services.agent_workbench.roadmap_phase import RoadmapPhaseRunner


class _FakeProductRepo:
    """Fake product repo with setup-passed project data."""

    def get_by_id(self, product_id: int) -> SimpleNamespace:
        """Return a product-like object."""
        return SimpleNamespace(
            product_id=product_id,
            name="Cartola",
            spec_file_path="specs/spec.json",
            compiled_authority_json='{"authority": true}',
            vision="A clear saved vision.",
        )


class _FakeWorkflowService:
    """Fake workflow service with persisted session state."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "fsm_state": "ROADMAP_INTERVIEW",
            "setup_status": "passed",
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Choose weekly squad",
                    "value_driver": "Strategic",
                    "justification": "Core value",
                    "estimated_effort": "M",
                }
            ],
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


def test_roadmap_generate_hydrates_vision_spec_authority_and_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roadmap generate must pass Vision, spec, authority, and Backlog to agent."""
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
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
                "product_vision": state["product_vision_assessment"][
                    "product_vision_statement"
                ],
                "technical_spec": state.get("pending_spec_content"),
                "compiled_authority": state.get("compiled_authority_cached"),
                "backlog_items": state.get("backlog_items"),
                "prior_roadmap_state": "NO_HISTORY",
                "user_input": user_input or "",
            },
            "output_artifact": {
                "roadmap_releases": [
                    {
                        "release_name": "Milestone 1",
                        "theme": "Foundation",
                        "focus_area": "Technical Foundation",
                        "items": ["Choose weekly squad"],
                        "reasoning": "Start here",
                    }
                ],
                "roadmap_summary": "Draft roadmap",
                "is_complete": False,
                "clarifying_questions": ["Which milestone first?"],
            },
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )
    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2, user_input="draft roadmap")

    assert result["ok"] is True
    assert captured["state"]["pending_spec_content"] == "SPEC CONTENT"
    assert captured["state"]["compiled_authority_cached"] == "AUTHORITY JSON"
    assert captured["state"]["backlog_items"][0]["requirement"] == (
        "Choose weekly squad"
    )
    assert result["data"]["input_context"]["technical_spec"] == "SPEC CONTENT"
    assert result["data"]["input_context"]["compiled_authority"] == "AUTHORITY JSON"
    assert result["data"]["input_context"]["backlog_items"][0]["requirement"] == (
        "Choose weekly squad"
    )


def test_roadmap_generate_returns_failure_envelope_for_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roadmap runtime failures must be loud to agent-facing CLI callers."""

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": False,
            "error": "ROADMAP_GENERATION_FAILED",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider rejected model",
            "failure_artifact_id": "roadmap-failure-1",
            "has_full_artifact": True,
            "input_context": {
                "product_vision": "A clear saved vision.",
                "technical_spec": "SPEC CONTENT",
                "compiled_authority": "AUTHORITY JSON",
                "backlog_items": [],
                "prior_roadmap_state": "NO_HISTORY",
                "user_input": "",
            },
            "output_artifact": {
                "is_complete": False,
                "error": "ROADMAP_GENERATION_FAILED",
                "failure_summary": "provider rejected model",
            },
            "is_complete": False,
        }

    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )
    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["roadmap_run_success"] is False
    assert result["errors"][0]["details"]["failure_stage"] == "invocation_exception"
