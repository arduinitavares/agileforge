"""Tests for agent workbench Story phase runner."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from services.agent_workbench.story_phase import StoryPhaseRunner

PROJECT_ID = 2
EXPECTED_REQUIREMENT_COUNT = 2
EXPECTED_FAILURE_ATTEMPT_COUNT = 2


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


def test_story_pending_returns_grouped_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story pending returns roadmap requirements grouped by milestone."""

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
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

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
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

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
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


def test_story_generate_returns_failure_envelope_for_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story runtime failures must be returned as mutation failure envelopes."""

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
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
        return {
            "parent_requirement": kwargs["parent_requirement"],
            "fsm_state": "STORY_INTERVIEW",
            "idempotency_key": kwargs["idempotency_key"],
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.reopen_story_requirement",
        fake_reopen_story_requirement,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

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

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
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

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
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
