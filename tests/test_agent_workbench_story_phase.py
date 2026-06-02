"""Tests for agent workbench Story phase runner."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlmodel import select

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlmodel import Session

from models.core import (
    Product,
    Sprint,
    SprintStory,
    Task,
    Team,
    UserStory,
    UserStoryDependency,
)
from models.enums import SprintStatus, TaskStatus, WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench.story_phase import (
    StoryPhaseRunner,
    _invalidate_unsaved_sprint_working_set,
    _repair_story_readiness_rows,
)
from services.phases.story_service import StoryPhaseError

PROJECT_ID = 2
EXPECTED_REQUIREMENT_COUNT = 2
EXPECTED_FAILURE_ATTEMPT_COUNT = 2
OWNED_SPRINT_ID = 42


def test_invalidate_unsaved_sprint_working_set_clears_draft_state() -> None:
    """Unsaved Sprint draft state is cleared after upstream Story changes."""
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_attempts": [{"attempt_id": "sprint-attempt-1"}],
        "sprint_last_input_context": {"available_stories": []},
        "sprint_plan_assessment": {"attempt_id": "sprint-attempt-1"},
        "sprint_saved_at": "2026-05-24T10:00:00Z",
        "sprint_planner_owner_sprint_id": None,
    }

    _invalidate_unsaved_sprint_working_set(
        state,
        reason="story_dependencies_applied",
        now_iso="2026-05-25T12:00:00Z",
    )

    assert state["fsm_state"] == "SPRINT_SETUP"
    assert state["sprint_attempts"] == []
    assert state["sprint_last_input_context"] is None
    assert state["sprint_plan_assessment"] is None
    assert state["sprint_saved_at"] is None
    assert state["sprint_planner_owner_sprint_id"] is None
    assert state["sprint_invalidated_reason"] == "story_dependencies_applied"
    assert state["sprint_invalidated_at"] == "2026-05-25T12:00:00Z"


def test_invalidate_unsaved_sprint_working_set_preserves_owned_state() -> None:
    """Persisted Sprint ownership keeps planner state intact."""
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_attempts": [{"attempt_id": "sprint-attempt-1"}],
        "sprint_last_input_context": {"available_stories": []},
        "sprint_plan_assessment": {"attempt_id": "sprint-attempt-1"},
        "sprint_saved_at": "2026-05-24T10:00:00Z",
        "sprint_planner_owner_sprint_id": OWNED_SPRINT_ID,
    }

    _invalidate_unsaved_sprint_working_set(
        state,
        reason="story_dependencies_applied",
        now_iso="2026-05-25T12:00:00Z",
    )

    assert state["fsm_state"] == "SPRINT_DRAFT"
    assert state["sprint_attempts"] == [{"attempt_id": "sprint-attempt-1"}]
    assert state["sprint_last_input_context"] == {"available_stories": []}
    assert state["sprint_plan_assessment"] == {"attempt_id": "sprint-attempt-1"}
    assert state["sprint_saved_at"] == "2026-05-24T10:00:00Z"
    assert state["sprint_planner_owner_sprint_id"] == OWNED_SPRINT_ID
    assert "sprint_invalidated_reason" not in state
    assert "sprint_invalidated_at" not in state


class _FakeProductRepo:
    """Fake product repo with setup-passed project data."""

    def __init__(self, *, roadmap: str | None = None) -> None:
        self.roadmap = roadmap

    def get_by_id(self, product_id: int) -> SimpleNamespace:
        """Return a product-like object."""
        return SimpleNamespace(
            product_id=product_id,
            name="Cartola",
            spec_file_path="specs/spec.json",
            compiled_authority_json='{"authority": true}',
            vision="A clear saved vision.",
            roadmap=self.roadmap,
        )


class _FakeWorkflowService:
    """Fake workflow service with persisted Story session state."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "fsm_state": "STORY_INTERVIEW",
            "setup_status": "passed",
            "pending_spec_content": "SPEC CONTENT",
            "compiled_authority_cached": "AUTHORITY JSON",
            "roadmap_releases": [
                {
                    "release_name": "Milestone 1",
                    "theme": "Foundation",
                    "focus_area": "Core workflow",
                    "reasoning": "First slice",
                    "items": ["Choose weekly squad", "Review match result"],
                }
            ],
            "story_saved": {"Choose weekly squad": True},
            "story_attempts": {"Review match result": [{"attempt_id": "attempt-1"}]},
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


def _seed_dependency_rows(session: Session) -> tuple[int, int]:
    product = Product(product_id=PROJECT_ID, name="Cartola")
    first = UserStory(
        product_id=PROJECT_ID,
        title="Capture market data",
        story_description="As a manager, I want captured market data.",
        acceptance_criteria="- Verify capture.",
        source_requirement="live recommendation",
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=2,
        rank="101",
    )
    second = UserStory(
        product_id=PROJECT_ID,
        title="Generate recommendation",
        story_description="As a manager, I want a recommendation.",
        acceptance_criteria="- Verify recommendation.",
        source_requirement="live recommendation",
        refinement_slot=2,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=3,
        rank="102",
    )
    session.add(product)
    session.add(first)
    session.add(second)
    session.commit()
    session.refresh(first)
    session.refresh(second)
    assert first.story_id is not None
    assert second.story_id is not None
    session.add(
        UserStoryDependency(
            product_id=PROJECT_ID,
            dependent_story_id=second.story_id,
            prerequisite_story_id=first.story_id,
            status="proposed",
            source="story_writer",
            confidence="explicit",
            reason="Recommendation needs market data first.",
        )
    )
    session.commit()
    return second.story_id, first.story_id


def _seed_manual_dependency_stories(session: Session) -> tuple[int, int]:
    product = Product(product_id=PROJECT_ID, name="Cartola")
    prerequisite = UserStory(
        product_id=PROJECT_ID,
        title="Capture market data",
        story_description="As an operator, I want market capture.",
        acceptance_criteria="- Verify market capture.",
        source_requirement="market capture",
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=3,
        rank="201",
    )
    dependent = UserStory(
        product_id=PROJECT_ID,
        title="Run live workflow",
        story_description="As an operator, I want the full live workflow.",
        acceptance_criteria="- Verify workflow uses captured data.",
        source_requirement="live workflow",
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=3,
        rank="102",
    )
    session.add_all([product, prerequisite, dependent])
    session.commit()
    session.refresh(prerequisite)
    session.refresh(dependent)
    assert prerequisite.story_id is not None
    assert dependent.story_id is not None
    return dependent.story_id, prerequisite.story_id


def test_story_pending_returns_grouped_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story pending returns roadmap requirements grouped by milestone."""

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        del tool_context
        return {"success": True, "project_id": product_id}

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.pending(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["total_count"] == EXPECTED_REQUIREMENT_COUNT
    assert result["data"]["saved_count"] == 1
    assert result["data"]["grouped_items"][0]["theme"] == "Foundation"
    assert result["data"]["grouped_items"][0]["requirements"] == [
        {
            "requirement": "Choose weekly squad",
            "status": "Saved",
            "attempt_count": 0,
        },
        {
            "requirement": "Review match result",
            "status": "Pending",
            "attempt_count": 1,
        },
    ]


def test_story_pending_hydrates_roadmap_from_product_json_when_state_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story pending hydrates Product.roadmap when workflow releases are absent."""
    workflow_service = _FakeWorkflowService()
    workflow_service.state.pop("roadmap_releases")
    persisted_roadmap = json.dumps(
        [
            {
                "release_name": "Milestone 2",
                "theme": "Insights",
                "focus_area": "Results",
                "reasoning": "Next slice",
                "items": ["Review match result"],
            }
        ]
    )

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        del tool_context
        return {"success": True, "project_id": product_id}

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(roadmap=persisted_roadmap),
        workflow_service=workflow_service,
    )

    result = runner.pending(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["total_count"] == 1
    assert result["data"]["grouped_items"][0]["requirements"] == [
        {
            "requirement": "Review match result",
            "status": "Pending",
            "attempt_count": 1,
        }
    ]


def test_story_generate_hydrates_spec_authority_and_roadmap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story generate must pass spec, authority, and Roadmap context to agent."""
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "HYDRATED SPEC"
        state["compiled_authority_cached"] = "HYDRATED AUTHORITY"
        return {"success": True, "project_id": product_id}

    async def fake_run_story_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> dict[str, Any]:
        captured["state"] = dict(state)
        captured["project_id"] = project_id
        captured["parent_requirement"] = parent_requirement
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {
                "technical_spec": state.get("pending_spec_content"),
                "compiled_authority": state.get("compiled_authority_cached"),
                "global_roadmap_context": state.get("roadmap_releases"),
            },
            "output_artifact": {
                "parent_requirement": parent_requirement,
                "user_stories": [
                    {
                        "story_title": "Pick a squad",
                        "statement": (
                            "As a manager, I want to choose a weekly squad, "
                            "so that I can compete."
                        ),
                        "acceptance_criteria": ["A squad can be submitted."],
                        "invest_score": "High",
                        "estimated_effort": "S",
                        "produced_artifacts": [],
                    }
                ],
                "is_complete": False,
                "clarifying_questions": ["Which constraints apply?"],
            },
            "classification": "reusable_content_result",
            "draft_kind": "incomplete_draft",
            "is_reusable": True,
            "is_complete": False,
            "request_payload": {},
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.run_story_agent_from_state",
        fake_run_story_agent_from_state,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
        user_input="draft story",
    )

    assert result["ok"] is True
    assert captured["project_id"] == PROJECT_ID
    assert captured["parent_requirement"] == "Review match result"
    assert captured["user_input"] is None
    assert captured["state"]["pending_spec_content"] == "HYDRATED SPEC"
    assert captured["state"]["compiled_authority_cached"] == "HYDRATED AUTHORITY"
    assert captured["state"]["roadmap_releases"][0]["items"] == [
        "Choose weekly squad",
        "Review match result",
    ]
    assert result["data"]["output_artifact"]["parent_requirement"] == (
        "Review match result"
    )
    assert "data" not in result["data"]


def test_story_generate_blocks_stale_downstream_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story generate returns existing failure envelope when backlog is stale."""
    captured: dict[str, Any] = {"agent_calls": 0}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        del tool_context
        return {"success": True, "project_id": product_id}

    async def fake_run_story_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, parent_requirement, user_input
        captured["agent_calls"] += 1
        return {
            "success": True,
            "input_context": {},
            "output_artifact": {
                "parent_requirement": "Review match result",
                "user_stories": [],
                "is_complete": False,
                "clarifying_questions": [],
            },
            "classification": "reusable_content_result",
            "draft_kind": "incomplete_draft",
            "is_reusable": True,
            "is_complete": False,
            "request_payload": {},
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.run_story_agent_from_state",
        fake_run_story_agent_from_state,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state.update(
        {
            "downstream_backlog_stale": True,
            "stale_backlog_reason": "backlog refinement changed",
            "stale_since_backlog_attempt_id": "backlog-attempt-7",
        }
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )

    result = runner.generate(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
        user_input="draft story",
    )

    assert result["ok"] is False
    assert result["data"] is None
    assert result["warnings"] == []
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert "downstream backlog is stale" in result["errors"][0]["message"]
    assert "backlog refinement changed" in result["errors"][0]["message"]
    assert "backlog-attempt-7" in result["errors"][0]["message"]
    assert captured["agent_calls"] == 0
    assert workflow_service.state["downstream_backlog_stale"] is True
    assert workflow_service.state["stale_backlog_reason"] == (
        "backlog refinement changed"
    )
    assert workflow_service.state["stale_since_backlog_attempt_id"] == (
        "backlog-attempt-7"
    )


def test_story_generate_blocks_active_reset_stale_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story generation remains blocked by active-reset stale markers."""
    captured: dict[str, Any] = {"agent_calls": 0}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        del tool_context
        return {"success": True, "project_id": product_id}

    async def fake_run_story_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, parent_requirement, user_input
        captured["agent_calls"] += 1
        return {
            "success": True,
            "input_context": {},
            "output_artifact": {
                "parent_requirement": "Review match result",
                "user_stories": [],
                "is_complete": False,
                "clarifying_questions": [],
            },
            "classification": "reusable_content_result",
            "draft_kind": "incomplete_draft",
            "is_reusable": True,
            "is_complete": False,
            "request_payload": {},
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.run_story_agent_from_state",
        fake_run_story_agent_from_state,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state.update(
        {
            "downstream_backlog_stale": True,
            "stale_backlog_reason": "active_backlog_reset",
            "stale_since_backlog_attempt_id": "backlog-attempt-12",
            "active_backlog_reset_attempt_id": "backlog-attempt-12",
        }
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )

    result = runner.generate(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
        user_input="draft story",
    )

    assert result["ok"] is False
    assert result["data"] is None
    assert result["warnings"] == []
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert "downstream backlog is stale" in result["errors"][0]["message"]
    assert "active_backlog_reset" in result["errors"][0]["message"]
    assert "backlog-attempt-12" in result["errors"][0]["message"]
    assert captured["agent_calls"] == 0
    assert workflow_service.state["downstream_backlog_stale"] is True
    assert workflow_service.state["stale_backlog_reason"] == "active_backlog_reset"
    assert workflow_service.state["stale_since_backlog_attempt_id"] == (
        "backlog-attempt-12"
    )


def test_story_retry_blocks_stale_downstream_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story retry returns existing failure envelope when backlog is stale."""
    captured: dict[str, Any] = {"agent_calls": 0}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        del tool_context
        return {"success": True, "project_id": product_id}

    async def fake_run_story_agent_request(
        request_payload: dict[str, Any],
        *,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        del request_payload, project_id, parent_requirement
        captured["agent_calls"] += 1
        return {
            "success": True,
            "input_context": {},
            "output_artifact": {
                "parent_requirement": "Review match result",
                "user_stories": [],
                "is_complete": False,
                "clarifying_questions": [],
            },
            "classification": "reusable_content_result",
            "draft_kind": "incomplete_draft",
            "is_reusable": True,
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.run_story_agent_request",
        fake_run_story_agent_request,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state.update(
        {
            "downstream_backlog_stale": True,
            "stale_backlog_reason": "backlog refinement changed",
            "stale_since_backlog_attempt_id": "backlog-attempt-7",
            "interview_runtime": {
                "story": {
                    "Review match result": {
                        "request_projection": {
                            "request_snapshot_id": "request-1",
                            "payload": {"parent_requirement": "Review match result"},
                            "included_feedback_ids": [],
                        },
                        "attempt_history": [
                            {
                                "attempt_id": "attempt-1",
                                "classification": ("nonreusable_provider_failure"),
                                "retryable": True,
                                "output_artifact": {"error": "STORY_GENERATION_FAILED"},
                            }
                        ],
                    }
                }
            },
        }
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )

    result = runner.retry(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
    )

    assert result["ok"] is False
    assert result["data"] is None
    assert result["warnings"] == []
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert "downstream backlog is stale" in result["errors"][0]["message"]
    assert "backlog refinement changed" in result["errors"][0]["message"]
    assert "backlog-attempt-7" in result["errors"][0]["message"]
    assert captured["agent_calls"] == 0
    assert workflow_service.state["downstream_backlog_stale"] is True
    assert workflow_service.state["stale_backlog_reason"] == (
        "backlog refinement changed"
    )
    assert workflow_service.state["stale_since_backlog_attempt_id"] == (
        "backlog-attempt-7"
    )


def test_story_generate_returns_failure_envelope_for_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story runtime failures must be returned as mutation failure envelopes."""

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "HYDRATED SPEC"
        state["compiled_authority_cached"] = "HYDRATED AUTHORITY"
        return {"success": True, "project_id": product_id}

    async def fake_run_story_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, parent_requirement, user_input
        return {
            "success": False,
            "error": "provider rejected model",
            "failure_stage": "invocation_exception",
            "failure_artifact_id": "story-failure-1",
            "failure_summary": "provider rejected model",
            "classification": "nonreusable_provider_failure",
            "draft_kind": None,
            "is_reusable": False,
            "is_complete": None,
            "request_payload": {},
            "output_artifact": {
                "error": "STORY_GENERATION_FAILED",
                "is_complete": False,
            },
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.run_story_agent_from_state",
        fake_run_story_agent_from_state,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
    )

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["project_id"] == PROJECT_ID
    assert result["errors"][0]["details"]["parent_requirement"] == (
        "Review match result"
    )
    assert result["errors"][0]["details"]["failure_stage"] == "invocation_exception"
    assert result["errors"][0]["details"]["failure_artifact_id"] == "story-failure-1"
    assert result["errors"][0]["details"]["attempt_count"] == (
        EXPECTED_FAILURE_ATTEMPT_COUNT
    )
    assert result["errors"][0]["details"]["fsm_state"] == "STORY_INTERVIEW"


def test_story_save_passes_guard_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story save passes attempt, fingerprint, state, and idempotency guards."""
    captured: dict[str, Any] = {}

    async def fake_save_story_draft(**kwargs: object) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "parent_requirement": kwargs["parent_requirement"],
            "attempt_id": kwargs["attempt_id"],
            "artifact_fingerprint": kwargs["expected_artifact_fingerprint"],
            "fsm_state": "STORY_PERSISTENCE",
            "data": {"save_result": {"success": True}},
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.save_story_draft",
        fake_save_story_draft,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.save(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
        attempt_id="attempt-1",
        expected_artifact_fingerprint="sha256:abc",
        expected_state="STORY_REVIEW",
        idempotency_key="save-key",
    )

    assert result["ok"] is True
    assert captured["project_id"] == PROJECT_ID
    assert captured["parent_requirement"] == "Review match result"
    assert captured["attempt_id"] == "attempt-1"
    assert captured["expected_artifact_fingerprint"] == "sha256:abc"
    assert captured["expected_state"] == "STORY_REVIEW"
    assert captured["idempotency_key"] == "save-key"
    assert captured["save_stories_tool"].__name__ == "save_stories_tool"
    assert result["ok"] is True
    assert result["data"]["save_result"] == {"success": True}
    assert result["data"]["attempt_id"] == "attempt-1"
    assert result["data"]["artifact_fingerprint"] == "sha256:abc"
    assert result["data"]["fsm_state"] == "STORY_PERSISTENCE"
    assert "data" not in result["data"]


def test_story_dependency_propose_creates_guarded_attempt(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Story dependency propose creates a fingerprinted review attempt."""
    _seed_dependency_rows(session)
    engine = session.get_bind()
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        lambda: engine,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state["fsm_state"] = "SPRINT_SETUP"
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )

    result = runner.dependency_propose(
        project_id=PROJECT_ID,
        expected_state="SPRINT_SETUP",
        idempotency_key="dep-propose-2-001",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["attempt_id"].startswith("story-dependencies-")
    assert data["artifact_fingerprint"].startswith("sha256:")
    assert data["proposed_edge_count"] >= 1
    assert (
        workflow_service.state["story_dependency_attempts"][0]["attempt_id"]
        == (data["attempt_id"])
    )


def test_story_dependency_propose_accepts_manual_reviewed_edges(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Manual edges should enter the reviewed dependency proposal artifact."""
    dependent_story_id, prerequisite_story_id = _seed_manual_dependency_stories(session)
    engine = session.get_bind()
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        lambda: engine,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state["fsm_state"] = "SPRINT_SETUP"
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )

    result = runner.dependency_propose(
        project_id=PROJECT_ID,
        expected_state="SPRINT_SETUP",
        idempotency_key="dep-propose-manual-001",
        manual_edges=[f"{dependent_story_id}:{prerequisite_story_id}"],
    )

    assert result["ok"] is True
    manual_edges = [
        edge
        for edge in result["data"]["edges"]
        if edge["dependent_story_id"] == dependent_story_id
        and edge["prerequisite_story_id"] == prerequisite_story_id
    ]
    assert manual_edges == [
        {
            "dependency_id": None,
            "dependent_story_id": dependent_story_id,
            "dependent_story_title": "Run live workflow",
            "prerequisite_story_id": prerequisite_story_id,
            "prerequisite_story_title": "Capture market data",
            "status": "proposed",
            "source": "manual_review",
            "confidence": "reviewed",
            "reason": "Manual reviewed dependency edge from CLI.",
            "selected": True,
        }
    ]


def test_story_dependency_apply_promotes_reviewed_attempt_and_invalidates_sprint(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Story dependency apply promotes edges and clears unsaved Sprint draft state."""
    dependent_story_id, prerequisite_story_id = _seed_dependency_rows(session)
    engine = session.get_bind()
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        lambda: engine,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state["fsm_state"] = "SPRINT_SETUP"
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )
    proposal = runner.dependency_propose(
        project_id=PROJECT_ID,
        expected_state="SPRINT_SETUP",
        idempotency_key="dep-propose-apply-001",
    )["data"]
    workflow_service.state.update(
        {
            "fsm_state": "SPRINT_DRAFT",
            "sprint_attempts": [{"attempt_id": "sprint-attempt-4"}],
            "sprint_last_input_context": {"available_stories": []},
            "sprint_plan_assessment": {"attempt_id": "sprint-attempt-4"},
            "sprint_saved_at": None,
            "sprint_planner_owner_sprint_id": None,
        }
    )

    result = runner.dependency_apply(
        project_id=PROJECT_ID,
        attempt_id=proposal["attempt_id"],
        expected_artifact_fingerprint=proposal["artifact_fingerprint"],
        expected_state="SPRINT_DRAFT",
        idempotency_key="dep-apply-2-001",
    )

    assert result["ok"] is True
    assert workflow_service.state["fsm_state"] == "SPRINT_SETUP"
    assert workflow_service.state["sprint_attempts"] == []
    assert workflow_service.state["sprint_last_input_context"] is None
    assert workflow_service.state["sprint_plan_assessment"] is None
    assert workflow_service.state["sprint_saved_at"] is None
    assert workflow_service.state["sprint_planner_owner_sprint_id"] is None
    assert (
        workflow_service.state["sprint_invalidated_reason"]
        == "story_dependencies_applied"
    )
    assert isinstance(workflow_service.state["sprint_invalidated_at"], str)
    session.expire_all()
    edge = session.exec(
        select(UserStoryDependency).where(
            UserStoryDependency.dependent_story_id == dependent_story_id,
            UserStoryDependency.prerequisite_story_id == prerequisite_story_id,
        )
    ).one()
    assert edge.status == "active"
    assert edge.source == "manual_review"
    assert edge.confidence == "reviewed"
    events = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.STORY_DEPENDENCIES_APPLIED
        )
    ).all()
    assert len(events) == 1


def test_story_dependency_apply_promotes_manual_edges_in_sprint_view(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Reviewed manual edges can repair an active Sprint before work starts."""
    dependent_story_id, prerequisite_story_id = _seed_manual_dependency_stories(session)
    team = Team(name="Cartola Team")
    session.add(team)
    session.commit()
    assert team.team_id is not None
    sprint = Sprint(
        product_id=PROJECT_ID,
        team_id=team.team_id,
        goal="Live workflow",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.commit()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=dependent_story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=prerequisite_story_id),
            Task(
                story_id=dependent_story_id,
                description="Design live workflow",
                status=TaskStatus.TO_DO,
            ),
        ]
    )
    session.commit()
    engine = session.get_bind()
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        lambda: engine,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state["fsm_state"] = "SPRINT_VIEW"
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )
    proposal = runner.dependency_propose(
        project_id=PROJECT_ID,
        expected_state="SPRINT_VIEW",
        idempotency_key="dep-propose-active-001",
        manual_edges=[f"{dependent_story_id}:{prerequisite_story_id}"],
    )["data"]

    result = runner.dependency_apply(
        project_id=PROJECT_ID,
        attempt_id=proposal["attempt_id"],
        expected_artifact_fingerprint=proposal["artifact_fingerprint"],
        expected_state="SPRINT_VIEW",
        idempotency_key="dep-apply-active-001",
    )

    assert result["ok"] is True
    session.expire_all()
    edge = session.exec(
        select(UserStoryDependency).where(
            UserStoryDependency.dependent_story_id == dependent_story_id,
            UserStoryDependency.prerequisite_story_id == prerequisite_story_id,
        )
    ).one()
    assert edge.status == "active"
    assert edge.source == "manual_review"
    assert edge.confidence == "reviewed"


def test_story_dependency_propose_blocks_manual_edge_after_dependent_work_starts(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Active Sprint dependency repair must not change started dependent stories."""
    dependent_story_id, prerequisite_story_id = _seed_manual_dependency_stories(session)
    session.add(
        Task(
            story_id=dependent_story_id,
            description="Started live workflow task",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    session.commit()
    engine = session.get_bind()
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        lambda: engine,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state["fsm_state"] = "SPRINT_VIEW"
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )

    result = runner.dependency_propose(
        project_id=PROJECT_ID,
        expected_state="SPRINT_VIEW",
        idempotency_key="dep-propose-started-001",
        manual_edges=[f"{dependent_story_id}:{prerequisite_story_id}"],
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert result["errors"][0]["details"]["reason_code"] == (
        "MANUAL_DEPENDENCY_DEPENDENT_WORK_STARTED"
    )


def test_story_complete_passes_guard_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story complete passes expected state and idempotency guard fields."""
    captured: dict[str, Any] = {}

    async def fake_complete_story_phase(**kwargs: object) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "fsm_state": "SPRINT_SETUP",
            "coverage": {"saved": 2, "merged": 0, "total": 2},
            "idempotency_key": kwargs["idempotency_key"],
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.complete_story_phase",
        fake_complete_story_phase,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.complete(
        project_id=PROJECT_ID,
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-key",
    )

    assert result["ok"] is True
    assert captured["expected_state"] == "STORY_PERSISTENCE"
    assert captured["idempotency_key"] == "complete-key"


def test_story_reopen_runner_passes_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story reopen passes expected state and idempotency guard fields."""
    captured: dict[str, Any] = {}

    async def fake_reopen_story_requirement(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        load_state = cast(
            "Callable[[], Awaitable[dict[str, Any]]]",
            kwargs["load_state"],
        )
        save_state = cast(
            "Callable[[dict[str, Any]], None]",
            kwargs["save_state"],
        )
        state = await load_state()
        state["fsm_state"] = "STORY_INTERVIEW"
        state["fsm_state_entered_at"] = "2026-05-25T13:00:00Z"
        save_state(state)
        return {
            "parent_requirement": kwargs["parent_requirement"],
            "fsm_state": "STORY_INTERVIEW",
            "idempotency_key": kwargs["idempotency_key"],
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.reopen_story_requirement",
        fake_reopen_story_requirement,
    )
    workflow_service = _FakeWorkflowService()
    workflow_service.state.update(
        {
            "fsm_state": "SPRINT_SETUP",
            "sprint_attempts": [{"attempt_id": "sprint-attempt-5"}],
            "sprint_last_input_context": {"available_stories": []},
            "sprint_plan_assessment": {"attempt_id": "sprint-attempt-5"},
            "sprint_saved_at": None,
            "sprint_planner_owner_sprint_id": None,
        }
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow_service,
    )

    async def fake_load_story_state(
        _session_id: str,
        _project_id: int,
        _product: Product,
    ) -> dict[str, Any]:
        return dict(workflow_service.state)

    monkeypatch.setattr(runner, "_load_story_state", fake_load_story_state)

    result = runner.reopen(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
        expected_state="SPRINT_SETUP",
        idempotency_key="reopen-story-review-match",
    )

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "STORY_INTERVIEW"
    assert captured["expected_state"] == "SPRINT_SETUP"
    assert captured["idempotency_key"] == "reopen-story-review-match"
    assert workflow_service.state["fsm_state"] == "STORY_INTERVIEW"
    assert workflow_service.state["fsm_state_entered_at"] == "2026-05-25T13:00:00Z"
    assert workflow_service.state["sprint_attempts"] == []
    assert workflow_service.state["sprint_last_input_context"] is None
    assert workflow_service.state["sprint_plan_assessment"] is None
    assert workflow_service.state["sprint_invalidated_reason"] == "story_reopened"


def test_story_repair_readiness_runner_passes_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story readiness repair passes expected state and idempotency guards."""
    captured: dict[str, Any] = {}

    async def fake_repair_story_readiness(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "project_id": kwargs["project_id"],
            "fsm_state": "SPRINT_SETUP",
            "idempotency_key": kwargs["idempotency_key"],
            "repair_result": {"repaired_count": 1, "story_ids": [66]},
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.repair_story_readiness",
        fake_repair_story_readiness,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.repair_readiness(
        project_id=PROJECT_ID,
        expected_state="SPRINT_SETUP",
        idempotency_key="repair-story-readiness-2",
    )

    assert result["ok"] is True
    assert result["data"]["repair_result"] == {
        "repaired_count": 1,
        "story_ids": [66],
    }
    assert captured["expected_state"] == "SPRINT_SETUP"
    assert captured["idempotency_key"] == "repair-story-readiness-2"


def test_repair_story_readiness_rows_block_missing_refined_row(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story readiness repair must not silently skip missing DB rows."""
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        session.get_bind,
    )
    product = Product(product_id=PROJECT_ID, name="Cartola")
    session.add(product)
    team = Team(name="Cartola Team")
    session.add(team)
    session.commit()

    request = {
        "project_id": PROJECT_ID,
        "items": [
            {
                "parent_requirement": "Choose weekly squad",
                "slot": 1,
                "story_points": 5,
                "rank": "101",
            }
        ],
    }

    with pytest.raises(StoryPhaseError) as excinfo:
        _repair_story_readiness_rows(request)

    assert excinfo.value.status_code == 409  # noqa: PLR2004
    assert "could not find active refined story" in excinfo.value.detail


def test_repair_story_readiness_rows_rechecks_open_sprint_links(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story readiness repair must recheck sprint safety in the write session."""
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        session.get_bind,
    )
    product = Product(product_id=PROJECT_ID, name="Cartola")
    session.add(product)
    team = Team(name="Cartola Team")
    session.add(team)
    story = UserStory(
        product_id=PROJECT_ID,
        title="Choose weekly squad",
        story_description="As a manager, I want a squad.",
        acceptance_criteria="- Verify squad choice.",
        source_requirement="choose_weekly_squad",
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=None,
        rank=None,
    )
    session.add(story)
    session.flush()
    assert team.team_id is not None
    sprint = Sprint(
        goal="Plan live MVP",
        start_date=date(2026, 5, 25),
        end_date=date(2026, 6, 8),
        status=SprintStatus.PLANNED,
        started_at=datetime(2026, 5, 25, 9, 0, tzinfo=UTC),
        product_id=PROJECT_ID,
        team_id=team.team_id,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    assert story.story_id is not None
    session.add(
        SprintStory(
            sprint_id=sprint.sprint_id,
            story_id=story.story_id,
        )
    )
    session.commit()

    request = {
        "project_id": PROJECT_ID,
        "items": [
            {
                "parent_requirement": "Choose weekly squad",
                "slot": 1,
                "story_points": 5,
                "rank": "101",
            }
        ],
    }

    with pytest.raises(StoryPhaseError) as excinfo:
        _repair_story_readiness_rows(request)

    assert excinfo.value.status_code == 409  # noqa: PLR2004
    assert "unsafe after Sprint work exists" in excinfo.value.detail


def test_repair_story_readiness_rows_rechecks_completed_sprint_links(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story readiness repair must block any existing sprint linkage."""
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        session.get_bind,
    )
    product = Product(product_id=PROJECT_ID, name="Cartola")
    session.add(product)
    team = Team(name="Cartola Team")
    session.add(team)
    story = UserStory(
        product_id=PROJECT_ID,
        title="Choose weekly squad",
        story_description="As a manager, I want a squad.",
        acceptance_criteria="- Verify squad choice.",
        source_requirement="choose_weekly_squad",
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=None,
        rank=None,
    )
    session.add(story)
    session.flush()
    assert team.team_id is not None
    sprint = Sprint(
        goal="Completed live MVP",
        start_date=date(2026, 5, 25),
        end_date=date(2026, 6, 8),
        status=SprintStatus.COMPLETED,
        started_at=datetime(2026, 5, 25, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 6, 8, 18, 0, tzinfo=UTC),
        product_id=PROJECT_ID,
        team_id=team.team_id,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    assert story.story_id is not None
    session.add(
        SprintStory(
            sprint_id=sprint.sprint_id,
            story_id=story.story_id,
        )
    )
    session.commit()

    request = {
        "project_id": PROJECT_ID,
        "items": [
            {
                "parent_requirement": "Choose weekly squad",
                "slot": 1,
                "story_points": 5,
                "rank": "101",
            }
        ],
    }

    with pytest.raises(StoryPhaseError) as excinfo:
        _repair_story_readiness_rows(request)

    assert excinfo.value.status_code == 409  # noqa: PLR2004
    assert "unsafe after Sprint work exists" in excinfo.value.detail


def test_story_complete_hydrates_roadmap_from_product_json_before_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story complete must not complete against an empty missing-roadmap set."""
    workflow_service = _FakeWorkflowService()
    workflow_service.state.pop("roadmap_releases")
    workflow_service.state["fsm_state"] = "STORY_PERSISTENCE"
    workflow_service.state["story_saved"] = {}
    persisted_roadmap = json.dumps(
        {
            "roadmap_releases": [
                {
                    "release_name": "Milestone 2",
                    "theme": "Insights",
                    "focus_area": "Results",
                    "reasoning": "Next slice",
                    "items": ["Review match result"],
                }
            ]
        }
    )

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        del tool_context
        return {"success": True, "project_id": product_id}

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(roadmap=persisted_roadmap),
        workflow_service=workflow_service,
    )

    result = runner.complete(
        project_id=PROJECT_ID,
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-key",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert "0 of 1 roadmap requirements" in result["errors"][0]["message"]


def test_story_generate_hydrates_roadmap_from_product_json_when_state_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story generate hydrates persisted Product.roadmap when state lacks releases."""
    captured: dict[str, Any] = {}
    workflow_service = _FakeWorkflowService()
    workflow_service.state.pop("roadmap_releases")
    persisted_roadmap = json.dumps(
        {
            "roadmap_releases": [
                {
                    "release_name": "Milestone 2",
                    "theme": "Insights",
                    "focus_area": "Results",
                    "reasoning": "Next slice",
                    "items": ["Review match result"],
                }
            ]
        }
    )

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "HYDRATED SPEC"
        state["compiled_authority_cached"] = "HYDRATED AUTHORITY"
        return {"success": True, "project_id": product_id}

    async def fake_run_story_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> dict[str, Any]:
        del project_id, user_input
        captured["state"] = dict(state)
        return {
            "success": True,
            "input_context": {},
            "output_artifact": {
                "parent_requirement": parent_requirement,
                "user_stories": [],
                "is_complete": False,
                "clarifying_questions": [],
            },
            "classification": "reusable_content_result",
            "draft_kind": "incomplete_draft",
            "is_reusable": True,
            "is_complete": False,
            "request_payload": {},
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.run_story_agent_from_state",
        fake_run_story_agent_from_state,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(roadmap=persisted_roadmap),
        workflow_service=workflow_service,
    )

    result = runner.generate(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
    )

    assert result["ok"] is True
    assert captured["state"]["roadmap_releases"] == [
        {
            "release_name": "Milestone 2",
            "theme": "Insights",
            "focus_area": "Results",
            "reasoning": "Next slice",
            "items": ["Review match result"],
        }
    ]
