"""Tests for agent workbench Sprint phase runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from services.agent_workbench import sprint_phase as sprint_phase_module
from services.agent_workbench.sprint_phase import SprintPhaseRunner
from services.phases import sprint_service

if TYPE_CHECKING:
    import pytest

JsonDict = dict[str, Any]


class _FakeProductRepository:
    def get_by_id(self, product_id: int) -> object:
        """Return a lightweight product sentinel."""
        return SimpleNamespace(product_id=product_id, name="Product")


class _FakeWorkflowService:
    def __init__(self) -> None:
        self.state: JsonDict = {"fsm_state": "SPRINT_SETUP"}

    def get_session_status(self, _session_id: str) -> JsonDict:
        """Return current workflow state."""
        return self.state

    async def initialize_session(self, *, session_id: str) -> None:
        """Initialize fake state."""
        self.state = {"fsm_state": "SPRINT_SETUP", "session_id": session_id}

    def update_session_status(self, _session_id: str, state: JsonDict) -> None:
        """Persist fake state."""
        self.state = dict(state)


def test_sprint_runner_generate_wraps_keyword_only_failure_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint runner should not pass keyword-only failure metadata positionally."""

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        return {
            "success": True,
            "input_context": {"available_stories": []},
            "output_artifact": {"is_complete": True},
            "is_complete": True,
            "error": None,
        }

    monkeypatch.setattr(
        sprint_phase_module,
        "run_sprint_agent_from_state",
        fake_run_sprint_agent,
    )
    monkeypatch.setattr(
        sprint_service,
        "load_sprint_candidates",
        lambda _project_id: {
            "success": True,
            "count": 1,
            "stories": [{"story_id": 1}],
            "readiness": {"status": "ready"},
        },
    )
    monkeypatch.setattr(
        SprintPhaseRunner,
        "_current_planned_sprint_id",
        lambda _self, _project_id: None,
    )

    runner = SprintPhaseRunner(
        product_repo=_FakeProductRepository(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=7)

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_DRAFT"
    assert result["data"]["attempt_id"] == "sprint-attempt-1"
