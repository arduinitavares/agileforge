"""Tests for backlog phase service."""

import copy
from types import SimpleNamespace
from typing import Any, Never

import pytest

from orchestrator_agent.agent_tools.backlog_primer.tools import SaveBacklogInput
from services.phases.backlog_service import (
    BacklogPhaseError,
    _backlog_artifact_fingerprint,
    backlog_state_from_complete,
    ensure_backlog_attempts,
    generate_backlog_draft,
    get_backlog_history,
    preview_backlog_refinement,
    record_backlog_attempt,
    record_backlog_refinement,
    save_backlog_draft,
    set_backlog_fsm_state,
)

JsonDict = dict[str, Any]


def _review_state_for_artifact(output_artifact: JsonDict) -> JsonDict:
    """Return a BACKLOG_REVIEW state guarded by the artifact fingerprint."""
    artifact_fingerprint = _backlog_artifact_fingerprint(output_artifact)
    guarded_artifact = {
        **output_artifact,
        "attempt_id": "backlog-attempt-1",
        "artifact_fingerprint": artifact_fingerprint,
    }
    return {
        "fsm_state": "BACKLOG_REVIEW",
        "product_backlog_assessment": guarded_artifact,
        "backlog_attempts": [
            {
                "attempt_id": "backlog-attempt-1",
                "artifact_fingerprint": artifact_fingerprint,
                "output_artifact": guarded_artifact,
            }
        ],
    }


def _refinement_source_item(**overrides: object) -> JsonDict:
    item: JsonDict = {
        "priority": 1,
        "requirement": "Verify current backlog workflow",
        "authority_ref": "REQ.backlog.refinement",
        "capability_hint": "Backlog",
        "value_driver": "Operational confidence",
        "justification": "Keeps backlog review evidence current.",
        "estimated_effort": "M",
        "technical_note": "Use existing phase service state.",
    }
    item.update(overrides)
    return item


def _refinement_source_state() -> JsonDict:
    output_artifact: JsonDict = {
        "backlog_items": [_refinement_source_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }
    artifact_fingerprint = _backlog_artifact_fingerprint(output_artifact)
    return {
        "fsm_state": "SPRINT_COMPLETE",
        "compiled_authority_fingerprint": "sha256:authority",
        "as_built_assessment_cache_meta": {"assessment_fingerprint": "sha256:as-built"},
        "backlog_attempts": [
            {
                "attempt_id": "backlog-attempt-1",
                "attempt_kind": "generation",
                "artifact_fingerprint": artifact_fingerprint,
                "output_artifact": output_artifact,
                "trigger": "auto_transition",
            }
        ],
    }


def _refinement_operations_payload(
    state: JsonDict,
    *,
    source_item_fingerprints: list[str] | None = None,
    source_artifact_fingerprint: str | None = None,
) -> JsonDict:
    source_attempt = state["backlog_attempts"][0]
    return {
        "source_attempt_id": source_attempt["attempt_id"],
        "source_artifact_fingerprint": source_artifact_fingerprint
        or source_attempt["artifact_fingerprint"],
        "authority_fingerprint": "sha256:authority",
        "as_built_cache_fingerprint": "sha256:as-built",
        "operations": [
            {
                "operation_id": "op-retitle",
                "operation_type": "retitle",
                "source_item_ids": ["item-001"],
                "source_item_fingerprints": source_item_fingerprints
                or ["AUTO_SOURCE_ITEM_FINGERPRINT"],
                "result_item_ids": ["item-001"],
                "new_requirement": "Verify canonical backlog refinement workflow",
                "rationale": "Retitle as a canonical refinement.",
                "requested_by": "po",
            }
        ],
    }


def test_backlog_state_from_complete_maps_to_review_and_interview() -> None:
    """Verify backlog state from complete maps to review and interview."""
    assert backlog_state_from_complete(True) == "BACKLOG_REVIEW"
    assert backlog_state_from_complete(False) == "BACKLOG_INTERVIEW"


def test_record_backlog_attempt_updates_working_state() -> None:
    """Verify record backlog attempt updates working state."""
    state: JsonDict = {}

    count = record_backlog_attempt(
        state,
        trigger="manual_refine",
        input_context={"user_raw_text": "refine"},
        output_artifact={
            "backlog_items": [{"title": "Seed backlog item"}],
            "is_complete": False,
        },
        is_complete=False,
        failure_meta={"failure_stage": "output_validation"},
        created_at="2026-04-04T00:00:00Z",
    )

    assert count == 1
    assert state["backlog_last_input_context"] == {"user_raw_text": "refine"}
    assert state["product_backlog_assessment"]["backlog_items"][0]["title"] == (
        "Seed backlog item"
    )
    assert state["backlog_items"][0]["title"] == "Seed backlog item"
    assert state["backlog_attempts"][0]["failure_stage"] == "output_validation"


def test_set_backlog_fsm_state_updates_state() -> None:
    """Verify set backlog fsm state updates state."""
    state: JsonDict = {}

    next_state = set_backlog_fsm_state(
        state,
        is_complete=True,
        now_iso=lambda: "2026-04-04T00:00:00Z",
    )

    assert next_state == "BACKLOG_REVIEW"
    assert state["fsm_state"] == "BACKLOG_REVIEW"
    assert state["fsm_state_entered_at"] == "2026-04-04T00:00:00Z"


def test_ensure_backlog_attempts_returns_existing_list() -> None:
    """Verify ensure backlog attempts returns existing list."""
    attempts = [{"created_at": "2026-04-04T00:00:00Z"}]
    state: JsonDict = {"backlog_attempts": attempts}

    assert ensure_backlog_attempts(state) is attempts


@pytest.mark.asyncio
async def test_generate_backlog_draft_allows_empty_input_on_first_attempt() -> None:
    """Verify generate backlog draft allows empty input on first attempt."""
    state: JsonDict = {"fsm_state": "VISION_PERSISTENCE"}
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_backlog_agent(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        captured["state"] = state
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {"user_input": user_input or ""},
            "output_artifact": {
                "backlog_items": [{"title": "Seed backlog item"}],
                "is_complete": False,
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_backlog_draft(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_backlog_agent=fake_run_backlog_agent,
        user_input=None,
    )

    assert captured["user_input"] == ""
    assert payload["trigger"] == "auto_transition"
    assert payload["fsm_state"] == "BACKLOG_INTERVIEW"
    assert payload["attempt_count"] == 1
    assert saved["state"]["backlog_attempts"][0]["trigger"] == "auto_transition"


@pytest.mark.asyncio
async def test_generate_backlog_draft_requires_feedback_after_first_attempt() -> None:
    """Verify generate backlog draft requires feedback after first attempt."""
    state: JsonDict = {
        "fsm_state": "BACKLOG_INTERVIEW",
        "backlog_attempts": [{"created_at": "2026-04-03T00:00:00Z"}],
        "product_backlog_assessment": {
            "backlog_items": [{"title": "Seed backlog item"}],
            "is_complete": False,
        },
        "backlog_items": [{"title": "Seed backlog item"}],
    }

    async def load_state() -> JsonDict:
        return state

    async def fake_run_backlog_agent(**_kwargs: object) -> Never:
        msg = "runner should not be called"
        raise AssertionError(msg)

    with pytest.raises(BacklogPhaseError) as exc_info:
        await generate_backlog_draft(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            run_backlog_agent=fake_run_backlog_agent,
            user_input="   ",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "Feedback is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_generate_backlog_draft_allows_empty_retry_after_failed_attempt() -> None:
    """Failed runtime attempts must not force fake refinement input."""
    state: JsonDict = {
        "fsm_state": "BACKLOG_INTERVIEW",
        "backlog_attempts": [
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

    async def fake_run_backlog_agent(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {"user_input": user_input or ""},
            "output_artifact": {
                "backlog_items": [{"title": "Seed backlog item"}],
                "is_complete": False,
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_backlog_draft(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_backlog_agent=fake_run_backlog_agent,
        user_input=None,
    )

    assert captured["user_input"] == ""
    assert payload["attempt_count"] == 2  # noqa: PLR2004
    assert payload["trigger"] == "auto_transition"


@pytest.mark.asyncio
async def test_generate_backlog_draft_forces_incomplete_when_questions_remain() -> None:
    """Clarifying questions keep Backlog in interview despite complete output."""
    state: JsonDict = {"fsm_state": "BACKLOG_INTERVIEW"}
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_backlog_agent(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id, user_input
        return {
            "success": True,
            "input_context": {"user_input": ""},
            "output_artifact": {
                "backlog_items": [{"title": "Seed backlog item"}],
                "is_complete": True,
                "clarifying_questions": ["Which objective is primary?"],
            },
            "is_complete": True,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_backlog_draft(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_backlog_agent=fake_run_backlog_agent,
        user_input=None,
    )

    assert payload["is_complete"] is False
    assert payload["fsm_state"] == "BACKLOG_INTERVIEW"
    assert payload["output_artifact"]["is_complete"] is False
    assert payload["attempt_id"] == "backlog-attempt-1"
    assert payload["artifact_fingerprint"].startswith("sha256:")
    attempt = saved["state"]["backlog_attempts"][0]
    assert attempt["attempt_id"] == payload["attempt_id"]
    assert attempt["artifact_fingerprint"] == payload["artifact_fingerprint"]
    assert saved["state"]["product_backlog_assessment"]["is_complete"] is False


@pytest.mark.asyncio
async def test_generate_backlog_draft_rejects_setup_required_state() -> None:
    """Verify generate backlog draft rejects setup required state."""
    state: JsonDict = {"fsm_state": "SETUP_REQUIRED"}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_backlog_agent(**_kwargs: object) -> Never:
        msg = "runner should not be called"
        raise AssertionError(msg)

    with pytest.raises(BacklogPhaseError) as exc_info:
        await generate_backlog_draft(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            run_backlog_agent=fake_run_backlog_agent,
            user_input="input",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "Setup required before backlog" in exc_info.value.detail


@pytest.mark.asyncio
async def test_generate_backlog_draft_normalizes_legacy_fsm_state() -> None:
    """Verify generate backlog draft normalizes legacy fsm state."""
    state: JsonDict = {"fsm_state": " backlog_interview "}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_backlog_agent(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id
        return {
            "success": True,
            "input_context": {"user_input": user_input or ""},
            "output_artifact": {
                "backlog_items": [{"title": "Seed backlog item"}],
                "is_complete": False,
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_backlog_draft(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_backlog_agent=fake_run_backlog_agent,
        user_input="refine",
    )

    assert payload["fsm_state"] == "BACKLOG_INTERVIEW"


@pytest.mark.asyncio
async def test_get_backlog_history_returns_count_and_items() -> None:
    """Verify get backlog history returns count and items."""
    state: JsonDict = {
        "backlog_attempts": [
            {"created_at": "2026-04-03T00:00:00Z", "trigger": "manual_refine"}
        ]
    }

    payload = await get_backlog_history(load_state=lambda: _async_value(state))

    assert payload["count"] == 1
    assert payload["items"][0]["trigger"] == "manual_refine"


@pytest.mark.asyncio
async def test_preview_backlog_refinement_does_not_mutate_state() -> None:
    """Preview applies refinement operations without recording an attempt."""
    state = _refinement_source_state()
    original_state = copy.deepcopy(state)

    async def load_state() -> JsonDict:
        return state

    payload = await preview_backlog_refinement(
        project_id=7,
        load_state=load_state,
        operations_payload=_refinement_operations_payload(state),
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    assert state == original_state
    assert payload["persisted"] is False
    assert payload["attempt_id"] is None
    assert payload["project_id"] == 7  # noqa: PLR2004
    assert payload["output_artifact"]["backlog_items"][0]["requirement"] == (
        "Verify canonical backlog refinement workflow"
    )
    assert payload["artifact_fingerprint"] == _backlog_artifact_fingerprint(
        payload["output_artifact"]
    )
    assert payload["operation_set_fingerprint"].startswith("sha256:")


@pytest.mark.asyncio
async def test_record_backlog_refinement_sets_active_draft_and_review_state() -> None:
    """Recording a next-cycle refinement creates a reviewed draft and stale marker."""
    state = _refinement_source_state()
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = copy.deepcopy(updated)

    payload = await record_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        operations_payload=_refinement_operations_payload(state),
        expected_source_fingerprint=state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        expected_state="SPRINT_COMPLETE",
        idempotency_key="refine-record-1",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    saved_state = saved["state"]
    assert payload["persisted"] is False
    assert payload["idempotency_key"] == "refine-record-1"
    assert payload["attempt_id"] == "backlog-attempt-2"
    assert saved_state["fsm_state"] == "BACKLOG_REVIEW"
    assert saved_state["backlog_review_origin"] == "next_cycle_refinement"
    assert saved_state["downstream_backlog_stale"] is True
    assert saved_state["stale_backlog_reason"] == "refined_backlog_recorded"
    assert saved_state["stale_since_backlog_attempt_id"] == payload["attempt_id"]
    assert (
        saved_state["product_backlog_assessment"]["attempt_id"]
        == (payload["attempt_id"])
    )
    assert (
        saved_state["product_backlog_assessment"]["artifact_fingerprint"]
        == (payload["artifact_fingerprint"])
    )
    assert (
        saved_state["product_backlog_assessment"]["backlog_items"][0]["requirement"]
        == "Verify canonical backlog refinement workflow"
    )
    recorded_attempt = saved_state["backlog_attempts"][-1]
    assert recorded_attempt["trigger"] == "refine_record"
    assert recorded_attempt["attempt_kind"] == "refinement"
    assert recorded_attempt["source_attempt_id"] == "backlog-attempt-1"
    assert (
        recorded_attempt["source_artifact_fingerprint"]
        == (state["backlog_attempts"][0]["artifact_fingerprint"])
    )
    assert (
        recorded_attempt["operation_set_fingerprint"]
        == (payload["operation_set_fingerprint"])
    )


@pytest.mark.asyncio
async def test_record_backlog_refinement_replays_same_idempotency_key() -> None:
    """Same refine-record idempotency key and request replays the first payload."""
    state = _refinement_source_state()
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        updated_snapshot = copy.deepcopy(updated)
        state.clear()
        state.update(updated_snapshot)
        saved["state"] = updated_snapshot

    request = {
        "project_id": 7,
        "load_state": load_state,
        "save_state": save_state,
        "operations_payload": _refinement_operations_payload(state),
        "expected_source_fingerprint": state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        "expected_state": "SPRINT_COMPLETE",
        "idempotency_key": "refine-record-1",
        "now_iso": lambda: "2026-06-01T00:00:00Z",
    }

    first = await record_backlog_refinement(**request)
    second = await record_backlog_refinement(**request)

    assert second == first
    assert len(saved["state"]["backlog_attempts"]) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_backlog_refinement_rejects_changed_idempotency_request() -> None:
    """Same refine-record idempotency key with changed inputs fails closed."""
    state = _refinement_source_state()
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        updated_snapshot = copy.deepcopy(updated)
        state.clear()
        state.update(updated_snapshot)
        saved["state"] = updated_snapshot

    first_payload = _refinement_operations_payload(state)
    await record_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        operations_payload=first_payload,
        expected_source_fingerprint=state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        expected_state="SPRINT_COMPLETE",
        idempotency_key="refine-record-1",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )
    changed_payload = copy.deepcopy(first_payload)
    changed_payload["operations"][0]["new_requirement"] = (
        "Verify changed backlog refinement workflow"
    )

    with pytest.raises(BacklogPhaseError) as exc_info:
        await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=save_state,
            operations_payload=changed_payload,
            expected_source_fingerprint=state["backlog_attempts"][0][
                "artifact_fingerprint"
            ],
            expected_state="SPRINT_COMPLETE",
            idempotency_key="refine-record-1",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "idempotency key" in exc_info.value.detail
    assert len(saved["state"]["backlog_attempts"]) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_backlog_refinement_resolves_auto_source_fingerprint() -> None:
    """AUTO_SOURCE_ITEM_FINGERPRINT resolves from source and is not stored."""
    state = _refinement_source_state()
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    payload = await record_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=lambda updated: saved.update({"state": copy.deepcopy(updated)}),
        operations_payload=_refinement_operations_payload(state),
        expected_source_fingerprint=state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        expected_state="SPRINT_COMPLETE",
        idempotency_key="refine-record-1",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    operation_set = saved["state"]["backlog_attempts"][-1]["operation_set"]
    source_fingerprints = operation_set["operations"][0]["source_item_fingerprints"]
    provenance = saved["state"]["product_backlog_assessment"]["backlog_items"][0][
        "refinement_provenance"
    ]
    assert source_fingerprints != ["AUTO_SOURCE_ITEM_FINGERPRINT"]
    assert source_fingerprints == provenance["source_item_fingerprints"]
    assert source_fingerprints[0].startswith("sha256:")
    assert payload["operation_set_fingerprint"].startswith("sha256:")


@pytest.mark.asyncio
async def test_record_backlog_refinement_rejects_stale_guards() -> None:
    """Record fails closed on stale expected state or source artifact guard."""
    state = _refinement_source_state()

    async def load_state() -> JsonDict:
        return state

    with pytest.raises(BacklogPhaseError) as stale_state:
        await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            operations_payload=_refinement_operations_payload(state),
            expected_source_fingerprint=state["backlog_attempts"][0][
                "artifact_fingerprint"
            ],
            expected_state="BACKLOG_REVIEW",
            idempotency_key="refine-record-1",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    with pytest.raises(BacklogPhaseError) as stale_fingerprint:
        await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            operations_payload=_refinement_operations_payload(state),
            expected_source_fingerprint="sha256:stale",
            expected_state="SPRINT_COMPLETE",
            idempotency_key="refine-record-1",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "stale state" in stale_state.value.detail
    assert "source artifact fingerprint" in stale_fingerprint.value.detail


@pytest.mark.asyncio
async def test_record_backlog_refinement_rejects_authority_mismatch() -> None:
    """Record fails closed when operation authority fingerprint is stale."""
    state = _refinement_source_state()
    original_attempt_count = len(state["backlog_attempts"])
    operations_payload = _refinement_operations_payload(state)
    operations_payload["authority_fingerprint"] = "sha256:stale-authority"

    async def load_state() -> JsonDict:
        return state

    def save_state(_updated: JsonDict) -> None:
        msg = "save_state should not be called"
        raise AssertionError(msg)

    with pytest.raises(BacklogPhaseError) as exc_info:
        await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=save_state,
            operations_payload=operations_payload,
            expected_source_fingerprint=state["backlog_attempts"][0][
                "artifact_fingerprint"
            ],
            expected_state="SPRINT_COMPLETE",
            idempotency_key="refine-record-authority-mismatch",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "authority fingerprint" in exc_info.value.detail
    assert len(state["backlog_attempts"]) == original_attempt_count
    assert state["fsm_state"] == "SPRINT_COMPLETE"


@pytest.mark.asyncio
async def test_record_backlog_refinement_rejects_as_built_mismatch() -> None:
    """Record fails closed when operation as-built cache fingerprint is stale."""
    state = _refinement_source_state()
    original_attempt_count = len(state["backlog_attempts"])
    operations_payload = _refinement_operations_payload(state)
    operations_payload["as_built_cache_fingerprint"] = "sha256:stale-as-built"

    async def load_state() -> JsonDict:
        return state

    def save_state(_updated: JsonDict) -> None:
        msg = "save_state should not be called"
        raise AssertionError(msg)

    with pytest.raises(BacklogPhaseError) as exc_info:
        await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=save_state,
            operations_payload=operations_payload,
            expected_source_fingerprint=state["backlog_attempts"][0][
                "artifact_fingerprint"
            ],
            expected_state="SPRINT_COMPLETE",
            idempotency_key="refine-record-as-built-mismatch",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "as-built cache fingerprint" in exc_info.value.detail
    assert len(state["backlog_attempts"]) == original_attempt_count
    assert state["fsm_state"] == "SPRINT_COMPLETE"


@pytest.mark.asyncio
async def test_save_backlog_draft_requires_complete_assessment() -> None:
    """Verify save backlog draft requires complete assessment."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [{"title": "Seed backlog item"}],
            "is_complete": False,
        }
    )
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(BacklogPhaseError) as exc_info:
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=expected_fingerprint,
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "is_complete is true" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_backlog_draft_rejects_empty_items() -> None:
    """Verify save backlog draft rejects empty items."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [],
            "is_complete": True,
        }
    )
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state))

    with pytest.raises(BacklogPhaseError) as exc_info:
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=expected_fingerprint,
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "Backlog items are empty" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_backlog_draft_persists_persistence_state() -> None:
    """Verify save backlog draft persists persistence state."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [{"title": "Seed backlog item"}],
            "is_complete": True,
        }
    )
    state["setup_status"] = "failed"
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        captured["backlog_input"] = backlog_input
        captured["tool_context"] = tool_context
        return {
            "success": True,
            "product_id": backlog_input.product_id,
            "saved_count": len(backlog_input.backlog_items),
        }

    payload = await save_backlog_draft(
        project_id=7,
        project_name="Backlog Project",
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=expected_fingerprint,
        expected_state="BACKLOG_REVIEW",
        idempotency_key="save-backlog-1",
        save_state=save_state,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert payload["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert payload["save_result"]["success"] is True
    assert captured["backlog_input"].product_id == 7  # noqa: PLR2004
    assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert saved["state"]["backlog_saved_at"] == "2026-04-04T00:00:00Z"
    assert (
        saved["state"]["backlog_save_idempotency_keys"]["save-backlog-1"]["fsm_state"]
        == "BACKLOG_PERSISTENCE"
    )


@pytest.mark.asyncio
async def test_save_backlog_draft_rejects_stale_attempt_guard() -> None:
    """Backlog save must target the exact reviewed draft attempt."""
    state = {
        "fsm_state": "BACKLOG_REVIEW",
        "product_backlog_assessment": {
            "backlog_items": [{"title": "Seed backlog item"}],
            "is_complete": True,
            "artifact_fingerprint": "sha256:current",
            "attempt_id": "backlog-attempt-2",
        },
        "backlog_attempts": [
            {
                "attempt_id": "backlog-attempt-2",
                "artifact_fingerprint": "sha256:current",
                "output_artifact": {
                    "backlog_items": [{"title": "Seed backlog item"}],
                    "is_complete": True,
                },
            }
        ],
    }

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint="sha256:old",
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )

    assert "Backlog save guard mismatch" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_backlog_draft_blocks_unmatched_authority_ref_warning() -> None:
    """Save gate blocks only the closed list of brownfield blocker warnings."""
    artifact: JsonDict = {
        "backlog_items": [{"title": "Seed backlog item"}],
        "is_complete": True,
        "clarifying_questions": [],
        "brownfield_warnings": [
            {
                "code": "asserted_authority_ref_unmatched",
                "item_index": 0,
                "severity": "block_on_save",
                "match_tier": "none",
                "authority_ref": None,
                "invariant_refs": [],
                "message": "Unmatched authority ref.",
                "details": {},
            }
        ],
    }
    state = _review_state_for_artifact(artifact)

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=state["product_backlog_assessment"][
                "artifact_fingerprint"
            ],
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )

    assert "asserted_authority_ref_unmatched" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_backlog_draft_allows_nonblocking_brownfield_warnings() -> None:
    """Fuzzy/conflict/disagreement warnings are PO review inputs, not save blocks."""
    artifact: JsonDict = {
        "backlog_items": [{"title": "Seed backlog item"}],
        "is_complete": True,
        "clarifying_questions": [],
        "brownfield_warnings": [
            {
                "code": "possible_mapping",
                "item_index": 0,
                "severity": "review",
                "match_tier": "fuzzy",
                "authority_ref": None,
                "invariant_refs": [],
                "message": "Possible As-Built mapping.",
                "details": {},
            }
        ],
    }
    state = _review_state_for_artifact(artifact)
    saved: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        del tool_context
        return {
            "success": True,
            "product_id": backlog_input.product_id,
            "saved_count": len(backlog_input.backlog_items),
        }

    payload = await save_backlog_draft(
        project_id=7,
        project_name="Backlog Project",
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=state["product_backlog_assessment"][
            "artifact_fingerprint"
        ],
        expected_state="BACKLOG_REVIEW",
        idempotency_key="save-backlog-1",
        save_state=lambda updated: saved.update({"state": dict(updated)}),
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert payload["save_result"]["success"] is True
    assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"


@pytest.mark.asyncio
async def test_save_backlog_draft_recomputes_artifact_fingerprint() -> None:
    """Save must reject tampered annotated artifacts, not only copied guards."""
    artifact: JsonDict = {
        "backlog_items": [{"title": "Seed backlog item"}],
        "is_complete": True,
        "clarifying_questions": [],
        "brownfield_warnings": [],
    }
    state = _review_state_for_artifact(artifact)
    state["product_backlog_assessment"]["brownfield_warnings"].append(
        {
            "code": "possible_mapping",
            "item_index": 0,
            "severity": "review",
            "match_tier": "fuzzy",
            "authority_ref": None,
            "invariant_refs": [],
            "message": "Tampered after fingerprint.",
            "details": {},
        }
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=state["product_backlog_assessment"][
                "artifact_fingerprint"
            ],
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )

    assert "artifact fingerprint" in exc_info.value.detail


async def _async_value[T](value: T) -> T:
    return value


def _fake_save_backlog_tool(*_args: object, **_kwargs: object) -> Never:
    msg = "save_backlog_tool should not be called"
    raise AssertionError(msg)
