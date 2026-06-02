"""Tests for roadmap phase service."""

from types import SimpleNamespace
from typing import Any, Never

import pytest

from orchestrator_agent.agent_tools.roadmap_builder.tools import SaveRoadmapToolInput
from services.agent_workbench.fingerprints import canonical_hash
from services.phases.roadmap_service import (
    RoadmapPhaseError,
    ensure_roadmap_attempts,
    generate_roadmap_draft,
    get_roadmap_history,
    record_roadmap_attempt,
    roadmap_state_from_complete,
    save_roadmap_draft,
    set_roadmap_fsm_state,
)

JsonDict = dict[str, Any]


def _complete_roadmap_artifact(
    *,
    items: list[str] | None = None,
    is_complete: bool = True,
    clarifying_questions: list[str] | None = None,
) -> JsonDict:
    return {
        "roadmap_releases": [
            {
                "release_name": "Milestone 1",
                "theme": "Foundation",
                "focus_area": "Technical Foundation",
                "items": items or ["Seed backlog item"],
                "reasoning": "Start here",
            }
        ],
        "roadmap_summary": "Final roadmap",
        "is_complete": is_complete,
        "clarifying_questions": clarifying_questions or [],
    }


def _state_for_guarded_save(
    *,
    artifact: JsonDict | None = None,
    backlog_items: list[JsonDict] | None = None,
) -> JsonDict:
    output_artifact = dict(artifact or _complete_roadmap_artifact())
    artifact_fingerprint = canonical_hash(
        {"phase": "roadmap", "output_artifact": output_artifact}
    )
    assessment = dict(output_artifact)
    assessment["attempt_id"] = "roadmap-attempt-1"
    assessment["artifact_fingerprint"] = artifact_fingerprint
    return {
        "fsm_state": "ROADMAP_REVIEW",
        "backlog_items": backlog_items or [{"requirement": "Seed backlog item"}],
        "product_roadmap_assessment": assessment,
        "roadmap_attempts": [
            {
                "attempt_id": "roadmap-attempt-1",
                "artifact_fingerprint": artifact_fingerprint,
                "output_artifact": output_artifact,
            }
        ],
    }


def _fingerprint_from_state(state: JsonDict) -> str:
    fingerprint = state["product_roadmap_assessment"]["artifact_fingerprint"]
    assert isinstance(fingerprint, str)
    return fingerprint


def test_record_roadmap_attempt_updates_working_state() -> None:
    """Verify record roadmap attempt updates working state."""
    state: JsonDict = {}

    count = record_roadmap_attempt(
        state,
        trigger="manual_refine",
        input_context={"user_raw_text": "refine"},
        output_artifact={
            "roadmap_releases": [{"release_name": "M1"}],
            "is_complete": False,
        },
        is_complete=False,
        failure_meta={"failure_stage": "output_validation"},
        created_at="2026-04-04T00:00:00Z",
    )

    assert count == 1
    assert state["roadmap_last_input_context"] == {"user_raw_text": "refine"}
    assert (
        state["product_roadmap_assessment"]["roadmap_releases"][0]["release_name"]
        == "M1"
    )
    assert state["roadmap_releases"][0]["release_name"] == "M1"
    assert state["roadmap_attempts"][0]["failure_stage"] == "output_validation"


def test_roadmap_state_from_complete_maps_to_review_and_interview() -> None:
    """Verify roadmap state from complete maps to review and interview."""
    assert roadmap_state_from_complete(True) == "ROADMAP_REVIEW"
    assert roadmap_state_from_complete(False) == "ROADMAP_INTERVIEW"


def test_set_roadmap_fsm_state_updates_state() -> None:
    """Verify set roadmap fsm state updates state."""
    state: JsonDict = {}

    next_state = set_roadmap_fsm_state(
        state,
        is_complete=True,
        now_iso=lambda: "2026-04-04T00:00:00Z",
    )

    assert next_state == "ROADMAP_REVIEW"
    assert state["fsm_state"] == "ROADMAP_REVIEW"
    assert state["fsm_state_entered_at"] == "2026-04-04T00:00:00Z"


def test_set_roadmap_fsm_state_preserves_story_phase_states() -> None:
    """Verify set roadmap fsm state preserves story phase states."""
    state: JsonDict = {"fsm_state": " story_review "}

    next_state = set_roadmap_fsm_state(
        state,
        is_complete=False,
        now_iso=lambda: "2026-04-04T00:00:00Z",
    )

    assert next_state == "STORY_REVIEW"
    assert state["fsm_state"] == "STORY_REVIEW"
    assert "fsm_state_entered_at" not in state


def test_set_roadmap_fsm_state_preserves_sprint_modify_state() -> None:
    """Verify set roadmap fsm state preserves sprint modify state."""
    state: JsonDict = {"fsm_state": " sprint_modify "}

    next_state = set_roadmap_fsm_state(
        state,
        is_complete=True,
        now_iso=lambda: "2026-04-04T00:00:00Z",
    )

    assert next_state == "SPRINT_MODIFY"
    assert state["fsm_state"] == "SPRINT_MODIFY"
    assert "fsm_state_entered_at" not in state


def test_ensure_roadmap_attempts_returns_existing_list() -> None:
    """Verify ensure roadmap attempts returns existing list."""
    attempts = [{"created_at": "2026-04-04T00:00:00Z"}]
    state: JsonDict = {"roadmap_attempts": attempts}

    assert ensure_roadmap_attempts(state) is attempts


@pytest.mark.asyncio
async def test_generate_roadmap_draft_blocks_stale_downstream_backlog() -> None:
    """Verify roadmap generation stops when downstream backlog is stale."""
    state: JsonDict = {
        "fsm_state": "VISION_PERSISTENCE",
        "downstream_backlog_stale": True,
        "stale_backlog_reason": "backlog refinement changed",
        "stale_since_backlog_attempt_id": "backlog-attempt-7",
    }
    saved: JsonDict = {}
    captured: JsonDict = {"agent_calls": 0}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_roadmap_agent_from_state(
        _state: object, **_kwargs: object
    ) -> JsonDict:
        captured["agent_calls"] += 1
        return {
            "success": True,
            "input_context": {},
            "output_artifact": {
                "roadmap_releases": [],
                "roadmap_summary": "Draft roadmap",
                "is_complete": False,
                "clarifying_questions": [],
            },
            "is_complete": False,
            "error": None,
        }

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await generate_roadmap_draft(
            project_id=7,
            load_state=load_state,
            save_state=save_state,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            run_roadmap_agent=fake_run_roadmap_agent_from_state,
            user_input=None,
        )

    message = exc_info.value.detail
    assert "downstream backlog is stale" in message
    assert "backlog refinement changed" in message
    assert "backlog-attempt-7" in message
    assert captured["agent_calls"] == 0
    assert "state" not in saved
    assert state["downstream_backlog_stale"] is True
    assert state["stale_backlog_reason"] == "backlog refinement changed"
    assert state["stale_since_backlog_attempt_id"] == "backlog-attempt-7"


@pytest.mark.asyncio
async def test_generate_roadmap_allows_active_reset_stale_marker() -> None:
    """Roadmap generation is the reset stale-exit path."""
    state: JsonDict = {
        "fsm_state": "BACKLOG_PERSISTENCE",
        "downstream_backlog_stale": True,
        "stale_backlog_reason": "active_backlog_reset",
        "stale_since_backlog_attempt_id": "backlog-attempt-12",
        "active_backlog_reset_attempt_id": "backlog-attempt-12",
    }
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id, user_input
        return {
            "success": True,
            "input_context": {},
            "output_artifact": _complete_roadmap_artifact(is_complete=True),
            "is_complete": True,
            "error": None,
        }

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        now_iso=lambda: "2026-06-02T12:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input=None,
    )

    assert payload["fsm_state"] == "ROADMAP_REVIEW"
    assert payload["attempt_count"] == 1
    assert payload["attempt_id"] == "roadmap-attempt-1"
    assert saved["state"]["roadmap_attempts"][0]["is_complete"] is True
    assert saved["state"]["downstream_backlog_stale"] is True
    assert saved["state"]["stale_backlog_reason"] == "active_backlog_reset"
    assert saved["state"]["stale_since_backlog_attempt_id"] == "backlog-attempt-12"


@pytest.mark.asyncio
async def test_generate_roadmap_draft_allows_empty_input_on_first_attempt() -> None:
    """Verify generate roadmap draft allows empty input on first attempt."""
    state: JsonDict = {"fsm_state": "VISION_PERSISTENCE"}
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        captured["state"] = state
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {"user_input": user_input or ""},
            "output_artifact": {
                "roadmap_releases": [{"release_name": "M1"}],
                "roadmap_summary": "Draft roadmap",
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

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input=None,
    )

    assert captured["user_input"] == ""
    assert payload["trigger"] == "auto_transition"
    assert payload["fsm_state"] == "ROADMAP_INTERVIEW"
    assert payload["attempt_count"] == 1
    assert payload["attempt_id"] == "roadmap-attempt-1"
    assert str(payload["artifact_fingerprint"]).startswith("sha256:")
    assert saved["state"]["roadmap_attempts"][0]["trigger"] == "auto_transition"
    assert saved["state"]["roadmap_attempts"][0]["attempt_id"] == ("roadmap-attempt-1")


@pytest.mark.asyncio
async def test_generate_roadmap_draft_forces_incomplete_when_questions_remain() -> None:
    """Open clarifying questions must prevent a complete Roadmap review state."""
    state: JsonDict = {"fsm_state": "BACKLOG_PERSISTENCE"}
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id, user_input
        return {
            "success": True,
            "input_context": {},
            "output_artifact": _complete_roadmap_artifact(
                is_complete=True,
                clarifying_questions=["Which launch milestone is first?"],
            ),
            "is_complete": True,
            "error": None,
        }

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=load_state,
        save_state=lambda updated: saved.update({"state": dict(updated)}),
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input=None,
    )

    assert payload["is_complete"] is False
    assert payload["fsm_state"] == "ROADMAP_INTERVIEW"
    assert payload["output_artifact"]["is_complete"] is False
    assert payload["attempt_id"] == "roadmap-attempt-1"
    assert saved["state"]["product_roadmap_assessment"]["attempt_id"] == (
        "roadmap-attempt-1"
    )


@pytest.mark.asyncio
async def test_generate_roadmap_draft_requires_feedback_after_first_attempt() -> None:
    """Verify generate roadmap draft requires feedback after first attempt."""
    state: JsonDict = {
        "fsm_state": "ROADMAP_INTERVIEW",
        "roadmap_attempts": [{"created_at": "2026-04-03T00:00:00Z"}],
        "product_roadmap_assessment": _complete_roadmap_artifact(is_complete=False),
    }

    async def load_state() -> JsonDict:
        return state

    async def fake_run_roadmap_agent_from_state(**_kwargs: object) -> Never:
        msg = "runner should not be called"
        raise AssertionError(msg)

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await generate_roadmap_draft(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            run_roadmap_agent=fake_run_roadmap_agent_from_state,
            user_input="   ",
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert exc_info.value.detail == (
        "User input is required to refine an existing roadmap."
    )


@pytest.mark.asyncio
async def test_generate_roadmap_draft_allows_plain_retry_after_failure() -> None:
    """A failed Roadmap runtime attempt is not a refinable draft."""
    state: JsonDict = {
        "fsm_state": "ROADMAP_INTERVIEW",
        "roadmap_attempts": [
            {
                "created_at": "2026-04-03T00:00:00Z",
                "failure_stage": "invocation_exception",
                "output_artifact": {
                    "error": "ROADMAP_GENERATION_FAILED",
                    "is_complete": False,
                    "clarifying_questions": [],
                },
            }
        ],
        "product_roadmap_assessment": {
            "error": "ROADMAP_GENERATION_FAILED",
            "is_complete": False,
            "clarifying_questions": [],
        },
    }

    async def load_state() -> JsonDict:
        return state

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id
        return {
            "success": True,
            "input_context": {"user_input": user_input or ""},
            "output_artifact": _complete_roadmap_artifact(is_complete=False),
            "is_complete": False,
            "error": None,
        }

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input=None,
    )

    assert payload["roadmap_run_success"] is True
    assert payload["trigger"] == "auto_transition"
    assert payload["attempt_count"] == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_generate_roadmap_draft_failed_run_cannot_mark_complete() -> None:
    """Verify generate roadmap draft failed run cannot mark complete."""
    state: JsonDict = {"fsm_state": "VISION_PERSISTENCE"}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
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

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input="complete roadmap",
    )

    assert payload["roadmap_run_success"] is False
    assert payload["is_complete"] is False
    assert payload["fsm_state"] == "ROADMAP_INTERVIEW"


@pytest.mark.asyncio
async def test_get_roadmap_history_returns_count_and_items() -> None:
    """Verify get roadmap history returns count and items."""
    state: JsonDict = {
        "roadmap_attempts": [
            {"created_at": "2026-04-03T00:00:00Z", "trigger": "manual_refine"}
        ]
    }

    payload = await get_roadmap_history(load_state=lambda: _async_value(state))

    assert payload["count"] == 1
    assert payload["items"][0]["trigger"] == "manual_refine"


@pytest.mark.asyncio
async def test_get_roadmap_history_defaults_to_empty_list() -> None:
    """Verify get roadmap history defaults to empty list."""
    payload = await get_roadmap_history(load_state=lambda: _async_value({}))

    assert payload["count"] == 0
    assert payload["items"] == []


@pytest.mark.asyncio
async def test_save_roadmap_draft_requires_assessment_dict() -> None:
    """Verify save roadmap draft requires assessment dict."""

    async def hydrate_context() -> object:
        return SimpleNamespace(state={"fsm_state": "ROADMAP_REVIEW"})

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint="sha256:" + "a" * 64,
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == "No roadmap draft available to save"


@pytest.mark.asyncio
async def test_save_roadmap_draft_requires_complete_assessment() -> None:
    """Verify save roadmap draft requires complete assessment."""
    state = _state_for_guarded_save(
        artifact=_complete_roadmap_artifact(is_complete=False)
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint=_fingerprint_from_state(state),
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Roadmap cannot be saved until is_complete is true"
    )


@pytest.mark.asyncio
async def test_save_roadmap_draft_rejects_invalid_session_data() -> None:
    """Verify save roadmap draft rejects invalid session data."""
    state = _state_for_guarded_save(
        artifact={
            "roadmap_summary": "Draft",
            "is_complete": True,
        }
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint=_fingerprint_from_state(state),
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )

    assert exc_info.value.status_code == 500  # noqa: PLR2004
    assert exc_info.value.detail.startswith("Invalid roadmap data in session: ")


@pytest.mark.asyncio
async def test_save_roadmap_draft_persists_persistence_state() -> None:
    """Verify save roadmap draft persists persistence state."""
    state = _state_for_guarded_save()
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    def fake_save_roadmap_tool(
        roadmap_input: SaveRoadmapToolInput,
        tool_context: object,
    ) -> JsonDict:
        captured["roadmap_input"] = roadmap_input
        captured["tool_context"] = tool_context
        return {
            "success": True,
            "product_id": roadmap_input.product_id,
            "message": "saved",
        }

    payload = await save_roadmap_draft(
        project_id=7,
        attempt_id="roadmap-attempt-1",
        expected_artifact_fingerprint=_fingerprint_from_state(state),
        expected_state="ROADMAP_REVIEW",
        idempotency_key="save-roadmap-1",
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_roadmap_tool=fake_save_roadmap_tool,
    )

    assert payload["fsm_state"] == "ROADMAP_PERSISTENCE"
    assert payload["save_result"]["success"] is True
    assert captured["roadmap_input"].roadmap_data.is_complete is True
    assert captured["roadmap_input"].idempotency_key == "save-roadmap-1"
    assert saved["state"]["fsm_state"] == "ROADMAP_PERSISTENCE"
    assert saved["state"]["roadmap_saved_at"] == "2026-04-04T00:00:00Z"
    assert (
        saved["state"]["roadmap_save_idempotency_keys"]["save-roadmap-1"]["attempt_id"]
        == "roadmap-attempt-1"
    )


@pytest.mark.asyncio
async def test_save_roadmap_draft_rejects_stale_attempt_guard() -> None:
    """Roadmap save must be tied to the reviewed draft fingerprint."""
    state = _state_for_guarded_save()

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint="sha256:" + "b" * 64,
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )

    assert exc_info.value.detail.startswith("Roadmap save guard mismatch")


@pytest.mark.asyncio
async def test_save_roadmap_draft_rejects_remaining_questions() -> None:
    """Roadmap save must fail closed when questions remain."""
    state = _state_for_guarded_save(
        artifact=_complete_roadmap_artifact(
            is_complete=True,
            clarifying_questions=["Which milestone owns analytics?"],
        )
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint=_fingerprint_from_state(state),
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )

    assert exc_info.value.detail == "Roadmap cannot be saved while questions remain"


@pytest.mark.asyncio
async def test_save_roadmap_draft_rejects_missing_backlog_coverage() -> None:
    """Every active Backlog item must appear exactly once in the Roadmap."""
    state = _state_for_guarded_save(
        backlog_items=[
            {"requirement": "Seed backlog item"},
            {"requirement": "Projection dashboard"},
        ]
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint=_fingerprint_from_state(state),
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )

    assert "missing=['Projection dashboard']" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_roadmap_draft_rejects_duplicate_backlog_coverage() -> None:
    """A Roadmap cannot schedule the same Backlog item twice."""
    state = _state_for_guarded_save(
        artifact=_complete_roadmap_artifact(
            items=["Seed backlog item", "Seed backlog item"]
        )
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint=_fingerprint_from_state(state),
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )

    assert "duplicate=['Seed backlog item']" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_roadmap_draft_replays_idempotency_key() -> None:
    """Repeated Roadmap save with the same key must replay without tool mutation."""
    replay = {
        "fsm_state": "ROADMAP_PERSISTENCE",
        "attempt_id": "roadmap-attempt-1",
        "idempotent_replay": True,
    }
    state: JsonDict = {
        "roadmap_save_idempotency_keys": {"save-roadmap-1": replay},
    }

    async def hydrate_context() -> object:
        return SimpleNamespace(state=state)

    payload = await save_roadmap_draft(
        project_id=7,
        attempt_id="roadmap-attempt-1",
        expected_artifact_fingerprint="sha256:" + "a" * 64,
        expected_state="ROADMAP_REVIEW",
        idempotency_key="save-roadmap-1",
        save_state=lambda _state: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_roadmap_tool=_fake_save_roadmap_tool,
    )

    assert payload == replay


@pytest.mark.asyncio
async def test_save_roadmap_draft_translates_save_failure() -> None:
    """Verify save roadmap draft translates save failure."""
    state = _state_for_guarded_save()

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    def fake_save_roadmap_tool(
        _roadmap_input: SaveRoadmapToolInput,
        _tool_context: object,
    ) -> JsonDict:
        return {"success": False, "error": "roadmap save failed"}

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint=_fingerprint_from_state(state),
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_roadmap_tool=fake_save_roadmap_tool,
        )

    assert exc_info.value.status_code == 500  # noqa: PLR2004
    assert exc_info.value.detail == "roadmap save failed"


async def _async_value[T](value: T) -> T:
    return value


def _fake_save_roadmap_tool(*_args: object, **_kwargs: object) -> Never:
    msg = "save_roadmap_tool should not be called"
    raise AssertionError(msg)
