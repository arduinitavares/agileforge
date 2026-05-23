"""Tests for vision phase service."""

import hashlib
from types import SimpleNamespace
from typing import Any, Never

import pytest

from orchestrator_agent.agent_tools.product_vision_tool.tools import SaveVisionInput
from services.phases.vision_service import (
    VisionPhaseError,
    ensure_vision_attempts,
    generate_vision_draft,
    get_vision_history,
    record_vision_attempt,
    save_vision_draft,
    set_vision_fsm_state,
    vision_state_from_complete,
)

JsonDict = dict[str, Any]
SECOND_ATTEMPT_COUNT = 2


def test_record_vision_attempt_updates_state_and_failure_metadata() -> None:
    """Verify record vision attempt updates state and failure metadata."""
    state: JsonDict = {}

    count = record_vision_attempt(
        state,
        trigger="manual_refine",
        input_context={"user_raw_text": ""},
        output_artifact={
            "updated_components": {"project_name": "Vision Project"},
            "product_vision_statement": "A clear vision.",
            "is_complete": False,
        },
        is_complete=False,
        failure_meta={
            "failure_artifact_id": "vision-failure-1",
            "failure_stage": "invalid_json",
            "failure_summary": "Vision response is not valid JSON",
            "raw_output_preview": '{"broken": true}',
            "has_full_artifact": True,
        },
        now_iso=lambda: "2026-04-04T00:00:00Z",
    )

    assert count == 1
    assert state["vision_attempts"][0]["created_at"] == "2026-04-04T00:00:00Z"
    assert state["vision_last_input_context"] == {"user_raw_text": ""}
    assert state["product_vision_assessment"]["product_vision_statement"] == (
        "A clear vision."
    )
    assert state["vision_components"] == {"project_name": "Vision Project"}
    assert state["vision_attempts"][0]["failure_artifact_id"] == "vision-failure-1"


def test_vision_state_from_complete_maps_to_review_and_interview() -> None:
    """Verify vision state from complete maps to review and interview."""
    assert vision_state_from_complete(True) == "VISION_REVIEW"
    assert vision_state_from_complete(False) == "VISION_INTERVIEW"


def test_set_vision_fsm_state_updates_state() -> None:
    """Verify set vision fsm state updates state."""
    state: JsonDict = {}

    next_state = set_vision_fsm_state(
        state,
        is_complete=True,
        now_iso=lambda: "2026-04-04T00:00:00Z",
    )

    assert next_state == "VISION_REVIEW"
    assert state["fsm_state"] == "VISION_REVIEW"
    assert state["fsm_state_entered_at"] == "2026-04-04T00:00:00Z"


def test_ensure_vision_attempts_returns_existing_list() -> None:
    """Verify ensure vision attempts returns existing list."""
    attempts = [{"created_at": "2026-04-04T00:00:00Z"}]
    state: JsonDict = {"vision_attempts": attempts}

    assert ensure_vision_attempts(state) is attempts


@pytest.mark.asyncio
async def test_generate_vision_draft_allows_empty_input_on_first_attempt() -> None:
    """Verify generate vision draft allows empty input on first attempt."""
    state: JsonDict = {"fsm_state": "VISION_INTERVIEW"}
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_vision_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        captured["state"] = state
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {
                "user_raw_text": user_input or "",
                "prior_vision_state": "NO_HISTORY",
                "specification_content": "SPEC",
                "compiled_authority": '{"ok": true}',
            },
            "output_artifact": {
                "updated_components": {"project_name": "Vision Project"},
                "product_vision_statement": "Draft vision",
                "is_complete": False,
                "clarifying_questions": ["Need more detail"],
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "model_info": {
                "agent_name": "product_vision_tool",
                "model_id": "openrouter/openai/gpt-5.4-mini",
            },
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_vision_draft(
        project_id=7,
        setup_blocker=None,
        load_state=load_state,
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_vision_agent=fake_run_vision_agent_from_state,
        user_input=None,
    )

    assert captured["user_input"] == ""
    assert payload["fsm_state"] == "VISION_INTERVIEW"
    assert payload["is_complete"] is False
    assert payload["attempt_count"] == 1
    assert payload["trigger"] == "initial_generate"
    assert payload["model_info"] == {
        "agent_name": "product_vision_tool",
        "model_id": "openrouter/openai/gpt-5.4-mini",
    }
    assert saved["state"]["vision_attempts"][0]["trigger"] == "initial_generate"


@pytest.mark.asyncio
async def test_generate_vision_draft_requires_feedback_after_first_attempt() -> None:
    """Verify generate vision draft requires feedback after first attempt."""
    state: JsonDict = {
        "fsm_state": "VISION_INTERVIEW",
        "vision_attempts": [{"created_at": "2026-04-03T00:00:00Z"}],
        "vision_components": {"project_name": "Vision Project"},
    }

    async def load_state() -> JsonDict:
        return state

    async def fake_run_vision_agent_from_state(**_kwargs: object) -> Never:
        msg = "runner should not be called"
        raise AssertionError(msg)

    with pytest.raises(VisionPhaseError) as exc_info:
        await generate_vision_draft(
            project_id=7,
            setup_blocker=None,
            load_state=load_state,
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            run_vision_agent=fake_run_vision_agent_from_state,
            user_input="   ",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "Feedback is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_generate_vision_draft_allows_empty_retry_after_failed_attempt() -> None:
    """Failed runtime attempts must not force fake refinement input."""
    state: JsonDict = {
        "fsm_state": "VISION_INTERVIEW",
        "vision_attempts": [
            {
                "created_at": "2026-04-03T00:00:00Z",
                "failure_stage": "invocation_exception",
                "failure_summary": "provider rejected model",
                "is_complete": False,
            }
        ],
    }
    captured: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_vision_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {
                "user_raw_text": user_input or "",
                "prior_vision_state": "NO_HISTORY",
                "specification_content": "SPEC",
                "compiled_authority": '{"ok": true}',
            },
            "output_artifact": {
                "updated_components": {"project_name": "Vision Project"},
                "product_vision_statement": "Draft vision",
                "is_complete": False,
                "clarifying_questions": ["Need more detail"],
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_vision_draft(
        project_id=7,
        setup_blocker=None,
        load_state=load_state,
        save_state=lambda _state: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_vision_agent=fake_run_vision_agent_from_state,
        user_input=None,
    )

    assert captured["user_input"] == ""
    assert payload["attempt_count"] == SECOND_ATTEMPT_COUNT
    assert payload["trigger"] == "initial_generate"


@pytest.mark.asyncio
async def test_generate_vision_draft_marks_user_input_as_manual_refine() -> None:
    """User feedback refinements should be marked distinctly from initial runs."""
    state: JsonDict = {
        "fsm_state": "VISION_REVIEW",
        "vision_components": {"project_name": "Vision Project"},
    }
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_vision_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id
        return {
            "success": True,
            "input_context": {
                "user_raw_text": user_input or "",
                "prior_vision_state": '{"project_name": "Vision Project"}',
                "specification_content": "SPEC",
                "compiled_authority": '{"ok": true}',
            },
            "output_artifact": {
                "updated_components": {"project_name": "Vision Project"},
                "product_vision_statement": "Updated vision",
                "is_complete": True,
            },
            "is_complete": True,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_vision_draft(
        project_id=7,
        setup_blocker=None,
        load_state=load_state,
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_vision_agent=fake_run_vision_agent_from_state,
        user_input="Tighten the scope.",
    )

    assert payload["trigger"] == "manual_refine"
    assert saved["state"]["vision_attempts"][0]["trigger"] == "manual_refine"


@pytest.mark.asyncio
async def test_get_vision_history_returns_count_and_items() -> None:
    """Verify get vision history returns count and items."""
    state: JsonDict = {
        "vision_attempts": [
            {"created_at": "2026-04-03T00:00:00Z", "trigger": "manual_refine"}
        ]
    }

    payload = await get_vision_history(load_state=lambda: _async_value(state))

    assert payload["count"] == 1
    assert payload["items"][0]["trigger"] == "manual_refine"


@pytest.mark.asyncio
async def test_save_vision_draft_requires_complete_assessment() -> None:
    """Verify save vision draft requires complete assessment."""
    state: JsonDict = {
        "product_vision_assessment": {
            "product_vision_statement": "Draft",
            "is_complete": False,
        }
    }

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(VisionPhaseError) as exc_info:
        await save_vision_draft(
            project_id=7,
            project_name="Vision Project",
            setup_blocker=None,
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_vision_tool=_fake_save_vision_tool,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "is_complete is true" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_vision_draft_persists_persistence_state() -> None:
    """Verify save vision draft persists persistence state."""
    state = {
        "product_vision_assessment": {
            "product_vision_statement": "Final vision",
            "is_complete": True,
        },
        "setup_status": "failed",
    }
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        context = SimpleNamespace(state=dict(state), session_id="7")
        context.state["pending_spec_content"] = "SPEC"
        return context

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    def fake_save_vision_tool(
        vision_input: SaveVisionInput,
        tool_context: object,
    ) -> JsonDict:
        captured["vision_input"] = vision_input
        captured["tool_context"] = tool_context
        return {
            "success": True,
            "product_id": vision_input.product_id,
            "project_name": vision_input.project_name,
            "message": "saved",
        }

    payload = await save_vision_draft(
        project_id=7,
        project_name="Vision Project",
        setup_blocker=None,
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_vision_tool=fake_save_vision_tool,
    )

    assert payload["fsm_state"] == "VISION_PERSISTENCE"
    assert payload["save_result"]["success"] is True
    assert payload["saved_vision"] == "Final vision"
    assert payload["vision_fingerprint"] == (
        "sha256:" + hashlib.sha256(b"Final vision").hexdigest()
    )
    assert captured["vision_input"].product_vision_statement == "Final vision"
    assert saved["state"]["fsm_state"] == "VISION_PERSISTENCE"
    assert saved["state"]["vision_saved_at"] == "2026-04-04T00:00:00Z"


async def _async_value[T](value: T) -> T:
    return value


def _fake_save_vision_tool(*_args: object, **_kwargs: object) -> Never:
    msg = "save_vision_tool should not be called"
    raise AssertionError(msg)
