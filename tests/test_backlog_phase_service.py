"""Tests for backlog phase service."""

import copy
from types import SimpleNamespace
from typing import Any, Never, Protocol, cast
from unittest.mock import patch

import pytest
from sqlmodel import Session as SqlSession
from sqlmodel import SQLModel, create_engine, select

from agile_sqlmodel import Product, UserStory
from models.enums import StoryStatus
from orchestrator_agent.agent_tools.backlog_primer.tools import (
    INTERNAL_BACKLOG_SAVE_OPTIONS_AUTHORITY,
    INTERNAL_BACKLOG_SAVE_OPTIONS_KEY,
    SaveBacklogInput,
)
from orchestrator_agent.agent_tools.backlog_primer.tools import (
    save_backlog_tool as real_save_backlog_tool,
)
from services.agent_workbench.backlog_active_reset import (
    ActiveBacklogResetRequest,
    reset_request_fingerprint,
)
from services.agent_workbench.backlog_refinement_events import (
    BacklogRefinementApprovalRequest,
)
from services.phases.backlog_service import (
    BacklogPhaseError,
    _backlog_artifact_fingerprint,
    backlog_state_from_complete,
    ensure_backlog_attempts,
    generate_backlog_draft,
    get_backlog_history,
    import_backlog_refinement,
    mark_backlog_refinement_approved,
    preview_backlog_refinement,
    record_backlog_attempt,
    record_backlog_refinement,
    reset_active_backlog,
    save_backlog_draft,
    set_backlog_fsm_state,
)

JsonDict = dict[str, Any]


class _StatefulToolContext(Protocol):
    state: dict[str, Any]


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
        "value_driver": "Strategic",
        "justification": "Keeps backlog review evidence current.",
        "estimated_effort": "M",
        "technical_note": "Use existing phase service state.",
    }
    item.update(overrides)
    return item


def _savable_backlog_item(**overrides: object) -> JsonDict:
    item: JsonDict = {
        "priority": 1,
        "requirement": "Verify existing backlog persistence",
        "authority_ref": "REQ.backlog.persistence",
        "capability_hint": "Backlog",
        "value_driver": "Strategic",
        "justification": "Keeps reviewed backlog saves aligned.",
        "estimated_effort": "M",
        "technical_note": "Use current phase service save flow.",
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


def _authority_ref_change_operations_payload(
    state: JsonDict,
    *,
    new_authority_ref: str,
) -> JsonDict:
    source_attempt = state["backlog_attempts"][0]
    return {
        "source_attempt_id": source_attempt["attempt_id"],
        "source_artifact_fingerprint": source_attempt["artifact_fingerprint"],
        "authority_fingerprint": "sha256:authority",
        "as_built_cache_fingerprint": "sha256:as-built",
        "operations": [
            {
                "operation_id": "op-authority-ref-change",
                "operation_type": "authority_ref_change",
                "source_item_ids": ["item-001"],
                "source_item_fingerprints": ["AUTO_SOURCE_ITEM_FINGERPRINT"],
                "result_item_ids": ["item-001"],
                "old_authority_ref": "REQ.backlog.refinement",
                "new_authority_ref": new_authority_ref,
                "rationale": "Move the item to a supported authority ref.",
                "requested_by": "po",
            }
        ],
    }


def _add_refinement_supported_authority_refs(state: JsonDict) -> None:
    state["compiled_authority_cached"] = {
        "invariants": [
            {
                "id": "INV-backlog-supported",
                "parameters": {"source_item_id": "REQ.backlog.supported"},
            }
        ],
        "source_map": [
            {"source_item_id": "REQ.backlog.source-map"},
        ],
    }
    state["as_built_assessment_cached"] = {
        "capability_assessments": [
            {
                "authority_ref": "REQ.backlog.as-built",
                "invariant_refs": ["INV-backlog-as-built"],
            }
        ]
    }


def _split_operations_with_stale_annotation(
    state: JsonDict,
    *,
    stale_authority_ref: str,
) -> JsonDict:
    source_attempt = state["backlog_attempts"][0]
    first_result = _refinement_source_item(
        requirement="Validate backlog refinement source identity",
        justification="Confirm source identity stays host-owned.",
    )
    second_result = _refinement_source_item(
        requirement="Document backlog refinement approval boundary",
        justification="Confirm approval stays host-owned.",
    )
    for item in (first_result, second_result):
        item["as_built_annotation"] = {
            "schema_version": "agileforge.brownfield_annotation.v1",
            "selected": {"authority_ref": stale_authority_ref},
        }
    return {
        "source_attempt_id": source_attempt["attempt_id"],
        "source_artifact_fingerprint": source_attempt["artifact_fingerprint"],
        "authority_fingerprint": "sha256:authority",
        "as_built_cache_fingerprint": "sha256:as-built",
        "operations": [
            {
                "operation_id": "op-split",
                "operation_type": "split",
                "source_item_ids": ["item-001"],
                "source_item_fingerprints": ["AUTO_SOURCE_ITEM_FINGERPRINT"],
                "result_item_ids": ["item-001-a", "item-001-b"],
                "result_items": [first_result, second_result],
                "rationale": "Split imported review feedback into two refined items.",
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
async def test_generate_records_scope_extension_authority_blocker() -> None:
    """Pending extension authority should persist as a Backlog runtime failure."""
    state: JsonDict = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "authority_pending_review",
        "scope_extension_context": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 11,
            "base_spec_hash": "sha256:base",
            "amended_spec_version_id": 12,
            "amended_spec_hash": "sha256:amended",
            "added_source_item_ids": ["REQ.reporting-export"],
        },
    }
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_backlog_agent(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id, user_input
        message = (
            "AUTHORITY_REVIEW_REQUIRED: scope extension authority must be accepted "
            "before backlog generation."
        )
        return {
            "success": False,
            "input_context": {
                "generation_mode": "scope_extension",
                "scope_extension": {
                    "amended_spec_version_id": 12,
                    "added_source_item_ids": ["REQ.reporting-export"],
                },
                "authority_scope_filter": {
                    "source_item_ids": ["REQ.reporting-export"]
                },
            },
            "output_artifact": {
                "error": "AUTHORITY_REVIEW_REQUIRED",
                "message": message,
                "is_complete": False,
                "clarifying_questions": [],
                "failure_stage": "authority_review_required",
                "failure_summary": message,
            },
            "is_complete": None,
            "error": message,
            "failure_artifact_id": "backlog-failure-1",
            "failure_stage": "authority_review_required",
            "failure_summary": message,
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

    assert payload["backlog_run_success"] is False
    assert payload["failure_stage"] == "authority_review_required"
    assert payload["fsm_state"] == "SETUP_REQUIRED"
    assert saved["state"]["fsm_state"] == "SETUP_REQUIRED"
    attempt = saved["state"]["backlog_attempts"][0]
    assert attempt["failure_stage"] == "authority_review_required"
    assert attempt["input_context"]["generation_mode"] == "scope_extension"
    assert "AUTHORITY_REVIEW_REQUIRED" in attempt["failure_summary"]


@pytest.mark.asyncio
async def test_scope_extension_existing_backlog_skips_feedback_gate() -> None:
    """Existing Backlog rows are read-only context for scope-extension generation."""
    state: JsonDict = {
        "fsm_state": "VISION_PERSISTENCE",
        "setup_status": "passed",
        "backlog_items": [{"requirement": "Existing backlog item"}],
        "scope_extension_context": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 11,
            "base_spec_hash": "sha256:base",
            "amended_spec_version_id": 12,
            "amended_spec_hash": "sha256:amended",
            "added_source_item_ids": ["REQ.reporting-export"],
        },
    }
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
            "input_context": {
                "generation_mode": "scope_extension",
                "scope_extension": {
                    "amended_spec_version_id": 12,
                    "added_source_item_ids": ["REQ.reporting-export"],
                },
                "authority_scope_filter": {
                    "source_item_ids": ["REQ.reporting-export"]
                },
            },
            "output_artifact": {
                "backlog_items": [_savable_backlog_item()],
                "is_complete": True,
                "clarifying_questions": [],
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

    assert captured["user_input"] == ""
    assert payload["backlog_run_success"] is True
    assert payload["fsm_state"] == "BACKLOG_REVIEW"
    assert saved["state"]["backlog_attempts"][0]["input_context"][
        "generation_mode"
    ] == "scope_extension"


@pytest.mark.asyncio
async def test_scope_extension_saved_context_requires_feedback_gate() -> None:
    """Consumed scope-extension context must not bypass normal refinement feedback."""
    state: JsonDict = {
        "fsm_state": "VISION_PERSISTENCE",
        "setup_status": "passed",
        "backlog_items": [{"requirement": "Existing backlog item"}],
        "scope_extension_context": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 11,
            "base_spec_hash": "sha256:base",
            "amended_spec_version_id": 12,
            "amended_spec_hash": "sha256:amended",
            "added_source_item_ids": ["REQ.reporting-export"],
            "backlog_extension_saved_at": "2026-04-04T00:00:00Z",
        },
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
            user_input=None,
        )

    assert "Feedback is required" in exc_info.value.detail


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
    assert recorded_attempt["refinement_saveable"] is False
    assert saved_state["product_backlog_assessment"]["refinement_saveable"] is False
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
async def test_record_backlog_refinement_discards_stale_brownfield_metadata() -> None:
    """Refinement records host-owned brownfield metadata from current state only."""
    state = _refinement_source_state()
    source_artifact = state["backlog_attempts"][0]["output_artifact"]
    source_item = source_artifact["backlog_items"][0]
    source_item["as_built_annotation"] = {
        "schema_version": "agileforge.brownfield_annotation.v1",
        "selected": {"authority_ref": "REQ.stale"},
    }
    source_artifact["brownfield_warnings"] = [
        {
            "code": "possible_mapping",
            "item_index": 0,
            "severity": "review",
            "match_tier": "fuzzy",
            "message": "stale warning",
        }
    ]
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
    state["backlog_attempts"][0]["artifact_fingerprint"] = source_fingerprint
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    await record_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=lambda updated: saved.update({"state": copy.deepcopy(updated)}),
        operations_payload=_refinement_operations_payload(state),
        expected_source_fingerprint=source_fingerprint,
        expected_state="SPRINT_COMPLETE",
        idempotency_key="refine-record-strip-brownfield",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    refined_artifact = saved["state"]["product_backlog_assessment"]
    refined_item = refined_artifact["backlog_items"][0]
    assert "as_built_annotation" not in refined_item
    assert refined_artifact["brownfield_warnings"] == []


@pytest.mark.asyncio
async def test_record_refinement_fingerprint_ignores_stale_inbound_annotation() -> None:
    """Refined fingerprints are based on host-rederived metadata, not proposer data."""

    async def record_with_stale_annotation(stale_authority_ref: str) -> JsonDict:
        state = _refinement_source_state()
        saved: JsonDict = {}

        async def load_state() -> JsonDict:
            return state

        payload = await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda updated: saved.update({"state": copy.deepcopy(updated)}),
            operations_payload=_split_operations_with_stale_annotation(
                state,
                stale_authority_ref=stale_authority_ref,
            ),
            expected_source_fingerprint=state["backlog_attempts"][0][
                "artifact_fingerprint"
            ],
            expected_state="SPRINT_COMPLETE",
            idempotency_key=f"refine-record-strip-{stale_authority_ref}",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )
        return {
            "payload": payload,
            "artifact": saved["state"]["product_backlog_assessment"],
        }

    first = await record_with_stale_annotation("REQ.stale-one")
    second = await record_with_stale_annotation("REQ.stale-two")

    assert first["artifact"]["backlog_items"] == second["artifact"]["backlog_items"]
    assert first["artifact"]["artifact_fingerprint"] == (
        second["artifact"]["artifact_fingerprint"]
    )
    assert first["payload"]["artifact_fingerprint"] == (
        second["payload"]["artifact_fingerprint"]
    )
    assert [
        item["item_fingerprint"] for item in first["artifact"]["backlog_items"]
    ] == [item["item_fingerprint"] for item in second["artifact"]["backlog_items"]]


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
async def test_record_backlog_refinement_rejects_downstream_expected_state() -> None:
    """Refine-record is limited to sprint-complete or backlog-review states."""
    state = _refinement_source_state()
    state["fsm_state"] = "ROADMAP_REVIEW"
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    with pytest.raises(BacklogPhaseError) as exc_info:
        await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda updated: saved.update({"state": copy.deepcopy(updated)}),
            operations_payload=_refinement_operations_payload(state),
            expected_source_fingerprint=state["backlog_attempts"][0][
                "artifact_fingerprint"
            ],
            expected_state="ROADMAP_REVIEW",
            idempotency_key="refine-record-downstream-state",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "expected_state is invalid" in exc_info.value.detail
    assert saved == {}
    assert state["fsm_state"] == "ROADMAP_REVIEW"


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
async def test_record_backlog_refinement_uses_as_built_authority_fallback() -> None:
    """Record validates stale authority against As-Built authority metadata."""
    state = _refinement_source_state()
    state.pop("compiled_authority_fingerprint")
    state["as_built_assessment_cache_meta"] = {
        "assessment_fingerprint": "sha256:as-built",
        "authority_fingerprint": "sha256:authority",
    }
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
            idempotency_key="refine-record-authority-fallback-mismatch",
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
async def test_record_backlog_refinement_rejects_unsupported_authority_ref() -> None:
    """Record validates authority_ref changes against host-owned authority refs."""
    state = _refinement_source_state()
    _add_refinement_supported_authority_refs(state)
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    with pytest.raises(BacklogPhaseError) as exc_info:
        await record_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda updated: saved.update({"state": copy.deepcopy(updated)}),
            operations_payload=_authority_ref_change_operations_payload(
                state,
                new_authority_ref="REQ.unsupported",
            ),
            expected_source_fingerprint=state["backlog_attempts"][0][
                "artifact_fingerprint"
            ],
            expected_state="SPRINT_COMPLETE",
            idempotency_key="refine-record-unsupported-authority",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "unsupported authority ref: REQ.unsupported" in exc_info.value.detail
    assert saved == {}
    assert len(state["backlog_attempts"]) == 1
    assert state["fsm_state"] == "SPRINT_COMPLETE"


@pytest.mark.asyncio
async def test_record_backlog_refinement_accepts_supported_authority_ref() -> None:
    """Record allows authority_ref changes backed by compiled authority."""
    state = _refinement_source_state()
    _add_refinement_supported_authority_refs(state)
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    payload = await record_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=lambda updated: saved.update({"state": copy.deepcopy(updated)}),
        operations_payload=_authority_ref_change_operations_payload(
            state,
            new_authority_ref="REQ.backlog.supported",
        ),
        expected_source_fingerprint=state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        expected_state="SPRINT_COMPLETE",
        idempotency_key="refine-record-supported-authority",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    assert payload["attempt_id"] == "backlog-attempt-2"
    refined_item = saved["state"]["product_backlog_assessment"]["backlog_items"][0]
    assert refined_item["authority_ref"] == "REQ.backlog.supported"


@pytest.mark.asyncio
async def test_import_backlog_refinement_records_source_then_refined_attempt() -> None:
    """Import records the supplied source before recording refined operations."""
    state: JsonDict = {
        "fsm_state": "SPRINT_COMPLETE",
        "compiled_authority_fingerprint": "sha256:authority",
        "as_built_assessment_cache_meta": {"assessment_fingerprint": "sha256:as-built"},
    }
    saved: JsonDict = {}
    save_calls: list[JsonDict] = []
    source_artifact: JsonDict = {
        "backlog_items": [_refinement_source_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
    edited_artifact: JsonDict = {
        "backlog_items": [
            {
                **_refinement_source_item(
                    requirement="Verify imported backlog refinement workflow"
                ),
                "source_item_id": "item-001",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved_state = copy.deepcopy(updated)
        save_calls.append(saved_state)
        state.clear()
        state.update(saved_state)
        saved["state"] = saved_state

    payload = await import_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        source_artifact=source_artifact,
        edited_artifact=edited_artifact,
        expected_source_fingerprint=source_fingerprint,
        idempotency_key="refine-import-1",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    attempts = saved["state"]["backlog_attempts"]
    assert payload["trigger"] == "refine-import"
    assert payload["attempt_id"] == "backlog-attempt-2"
    assert len(save_calls) == 1
    assert attempts[0]["trigger"] == "refine_import_source"
    assert attempts[0]["attempt_kind"] == "imported_preview_source"
    assert attempts[0]["artifact_fingerprint"] == source_fingerprint
    assert attempts[0]["output_artifact"]["backlog_items"][0]["item_id"] == "item-001"
    assert attempts[1]["trigger"] == "refine_import"
    assert attempts[1]["attempt_kind"] == "import_refinement"
    assert attempts[1]["source_attempt_id"] == "backlog-attempt-1"
    assert (
        saved["state"]["product_backlog_assessment"]["backlog_items"][0]["requirement"]
        == "Verify imported backlog refinement workflow"
    )
    assert "refine-import-1" in saved["state"]["backlog_refine_import_idempotency_keys"]


@pytest.mark.asyncio
async def test_import_backlog_refinement_uses_as_built_authority_fallback() -> None:
    """Import works when real state keeps authority fingerprint in As-Built meta."""
    state: JsonDict = {
        "fsm_state": "SPRINT_COMPLETE",
        "as_built_assessment_cache_meta": {
            "assessment_fingerprint": "sha256:as-built",
            "authority_fingerprint": "sha256:authority",
        },
    }
    saved: JsonDict = {}
    source_artifact: JsonDict = {
        "backlog_items": [_refinement_source_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
    edited_artifact: JsonDict = {
        "backlog_items": [
            {
                **_refinement_source_item(
                    requirement="Verify imported backlog refinement workflow"
                ),
                "source_item_id": "item-001",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved_state = copy.deepcopy(updated)
        state.clear()
        state.update(saved_state)
        saved["state"] = saved_state

    payload = await import_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        source_artifact=source_artifact,
        edited_artifact=edited_artifact,
        expected_source_fingerprint=source_fingerprint,
        idempotency_key="refine-import-authority-fallback",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    refined_attempt = saved["state"]["backlog_attempts"][1]
    assert payload["attempt_id"] == "backlog-attempt-2"
    assert refined_attempt["operation_set"]["authority_fingerprint"] == (
        "sha256:authority"
    )
    assert (
        saved["state"]["product_backlog_assessment"]["backlog_items"][0]["requirement"]
        == "Verify imported backlog refinement workflow"
    )


@pytest.mark.asyncio
async def test_import_backlog_refinement_resolves_clarifying_questions() -> None:
    """Import can record PO question answers and complete a refined artifact."""
    state: JsonDict = {
        "fsm_state": "BACKLOG_REVIEW",
        "compiled_authority_fingerprint": "sha256:authority",
        "as_built_assessment_cache_meta": {"assessment_fingerprint": "sha256:as-built"},
    }
    saved: JsonDict = {}
    source_artifact: JsonDict = {
        "backlog_items": [
            _refinement_source_item(
                priority=index,
                requirement=f"Refined backlog item {index}",
            )
            for index in range(1, 11)
        ],
        "is_complete": False,
        "clarifying_questions": [
            "Which risk tolerance should gate promotion?",
            "Should strict fixture fail-closed mode be in this slice?",
        ],
    }
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
    edited_artifact: JsonDict = {
        "backlog_items": [
            {
                **_refinement_source_item(
                    priority=index,
                    requirement=f"Refined backlog item {index}",
                ),
                "source_item_id": f"item-{index:03d}",
            }
            for index in range(1, 11)
        ],
        "is_complete": False,
        "clarifying_questions": [],
    }

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved_state = copy.deepcopy(updated)
        state.clear()
        state.update(saved_state)
        saved["state"] = saved_state

    payload = await import_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        source_artifact=source_artifact,
        edited_artifact=edited_artifact,
        expected_source_fingerprint=source_fingerprint,
        idempotency_key="refine-import-resolve-questions",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    assessment = saved["state"]["product_backlog_assessment"]
    assert payload["attempt_id"] == "backlog-attempt-2"
    assert assessment["clarifying_questions"] == []
    assert assessment["is_complete"] is True


@pytest.mark.asyncio
async def test_import_backlog_refinement_rejects_unsupported_authority_ref() -> None:
    """Edited artifacts cannot import unsupported authority_ref changes."""
    state: JsonDict = {
        "fsm_state": "SPRINT_COMPLETE",
        "compiled_authority_fingerprint": "sha256:authority",
        "as_built_assessment_cache_meta": {"assessment_fingerprint": "sha256:as-built"},
    }
    _add_refinement_supported_authority_refs(state)
    original_state = copy.deepcopy(state)
    source_artifact: JsonDict = {
        "backlog_items": [_refinement_source_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
    edited_artifact: JsonDict = {
        "backlog_items": [
            {
                **_refinement_source_item(authority_ref="REQ.unsupported"),
                "source_item_id": "item-001",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    with pytest.raises(BacklogPhaseError) as exc_info:
        await import_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda updated: saved.update({"state": copy.deepcopy(updated)}),
            source_artifact=source_artifact,
            edited_artifact=edited_artifact,
            expected_source_fingerprint=source_fingerprint,
            idempotency_key="refine-import-unsupported-authority",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "unsupported authority ref: REQ.unsupported" in exc_info.value.detail
    assert saved == {}
    assert state == original_state


@pytest.mark.asyncio
async def test_import_backlog_refinement_rejects_stale_or_ambiguous_input() -> None:
    """Import fails closed on source guard mismatch or ambiguous edited files."""
    source_artifact: JsonDict = {
        "backlog_items": [_refinement_source_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
    edited_artifact: JsonDict = {
        "backlog_items": [
            {
                **_refinement_source_item(
                    requirement="Verify imported backlog refinement workflow"
                ),
                "source_item_id": "item-001",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    async def load_state() -> JsonDict:
        return {
            "fsm_state": "SPRINT_COMPLETE",
            "compiled_authority_fingerprint": "sha256:authority",
            "as_built_assessment_cache_meta": {
                "assessment_fingerprint": "sha256:as-built"
            },
        }

    with pytest.raises(BacklogPhaseError) as stale:
        await import_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            source_artifact=source_artifact,
            edited_artifact=edited_artifact,
            expected_source_fingerprint="sha256:stale",
            idempotency_key="refine-import-1",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    ambiguous_artifact = {"backlog_items": [_refinement_source_item()]}
    with pytest.raises(BacklogPhaseError) as ambiguous:
        await import_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            source_artifact=source_artifact,
            edited_artifact=ambiguous_artifact,
            expected_source_fingerprint=source_fingerprint,
            idempotency_key="refine-import-1",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "source artifact fingerprint" in stale.value.detail
    assert "ambiguous" in ambiguous.value.detail


@pytest.mark.asyncio
async def test_import_backlog_refinement_ambiguous_input_does_not_mutate_state() -> (
    None
):
    """Ambiguous import validates before appending source attempts."""
    state: JsonDict = {
        "fsm_state": "SPRINT_COMPLETE",
        "compiled_authority_fingerprint": "sha256:authority",
        "as_built_assessment_cache_meta": {"assessment_fingerprint": "sha256:as-built"},
    }
    original_state = copy.deepcopy(state)
    save_calls: list[JsonDict] = []
    source_artifact: JsonDict = {
        "backlog_items": [_refinement_source_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        save_calls.append(copy.deepcopy(updated))

    with pytest.raises(BacklogPhaseError) as exc_info:
        await import_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=save_state,
            source_artifact=source_artifact,
            edited_artifact={"backlog_items": [_refinement_source_item()]},
            expected_source_fingerprint=_backlog_artifact_fingerprint(source_artifact),
            idempotency_key="refine-import-ambiguous-state",
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )

    assert "ambiguous" in exc_info.value.detail
    assert save_calls == []
    assert state == original_state


@pytest.mark.asyncio
async def test_import_backlog_refinement_idempotency_conflict_does_not_mutate() -> None:
    """Changed import request with same key is rejected before source append."""
    state: JsonDict = {
        "fsm_state": "SPRINT_COMPLETE",
        "compiled_authority_fingerprint": "sha256:authority",
        "as_built_assessment_cache_meta": {"assessment_fingerprint": "sha256:as-built"},
    }
    source_artifact: JsonDict = {
        "backlog_items": [_refinement_source_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        updated_snapshot = copy.deepcopy(updated)
        state.clear()
        state.update(updated_snapshot)

    first_edited_artifact: JsonDict = {
        "backlog_items": [
            {
                **_refinement_source_item(
                    requirement="Verify imported backlog refinement workflow"
                ),
                "source_item_id": "item-001",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    second_edited_artifact: JsonDict = {
        "backlog_items": [
            {
                **_refinement_source_item(
                    requirement="Verify changed import refinement workflow"
                ),
                "source_item_id": "item-001",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    await import_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        source_artifact=source_artifact,
        edited_artifact=first_edited_artifact,
        expected_source_fingerprint=source_fingerprint,
        idempotency_key="refine-import-conflict-1",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )
    state_after_first = copy.deepcopy(state)

    with pytest.raises(BacklogPhaseError) as exc_info:
        await import_backlog_refinement(
            project_id=7,
            load_state=load_state,
            save_state=save_state,
            source_artifact={
                **source_artifact,
                "clarifying_questions": ["Changed source envelope."],
            },
            edited_artifact=second_edited_artifact,
            expected_source_fingerprint=_backlog_artifact_fingerprint(
                {
                    **source_artifact,
                    "clarifying_questions": ["Changed source envelope."],
                }
            ),
            idempotency_key="refine-import-conflict-1",
            now_iso=lambda: "2026-06-01T00:00:01Z",
        )

    assert "idempotency key" in exc_info.value.detail
    assert state == state_after_first


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
async def test_save_backlog_draft_projects_refined_items_before_tool() -> None:
    """Save projects refined artifacts to Product Backlog-compatible items."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Verify existing backlog persistence",
                    "authority_ref": "REQ.backlog.persistence",
                    "capability_hint": "Backlog",
                    "value_driver": "Strategic",
                    "justification": "Keeps reviewed backlog saves aligned.",
                    "estimated_effort": "M",
                    "technical_note": "Use current phase service save flow.",
                    "item_id": "item-001",
                    "item_fingerprint": "sha256:item",
                    "as_built_annotation": {
                        "schema_version": "agileforge.brownfield_annotation.v1"
                    },
                    "classification": "verification",
                }
            ],
            "backlog_intake_items": [
                {
                    "priority": 2,
                    "requirement": "Clarify unsupported authority gap",
                    "authority_ref": "REQ.backlog.gap",
                    "capability_hint": "Backlog",
                    "value_driver": "Strategic",
                    "justification": "Captures unsupported intake for review.",
                    "estimated_effort": "S",
                    "technical_note": None,
                    "classification": "authority_gap_intake",
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    captured: JsonDict = {}
    saved: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        del tool_context
        captured["backlog_items"] = backlog_input.backlog_items
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
        idempotency_key="save-backlog-projected",
        save_state=lambda updated: saved.update({"state": dict(updated)}),
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert payload["save_result"]["success"] is True
    assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert captured["backlog_items"] == [
        {
            "priority": 1,
            "requirement": "Verify existing backlog persistence",
            "authority_ref": "REQ.backlog.persistence",
            "capability_hint": "Backlog",
            "value_driver": "Strategic",
            "justification": "Keeps reviewed backlog saves aligned.",
            "estimated_effort": "M",
            "technical_note": "Use current phase service save flow.",
        }
    ]


@pytest.mark.asyncio
async def test_save_backlog_draft_blocks_unapproved_refined_attempt() -> None:
    """A refined attempt is not saveable until host approval is bound."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    state["backlog_attempts"][0]["attempt_kind"] = "refinement"
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=expected_fingerprint,
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-unapproved-refinement",
            save_state=lambda _updated: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )

    assert "approval" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_save_backlog_draft_allows_host_approved_refined_attempt() -> None:
    """A refined attempt can save after approval binds to the exact artifact."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    state["backlog_attempts"][0].update(
        {
            "attempt_kind": "refinement",
            "refinement_saveable": True,
            "refinement_approval": {
                "approval_id": "approval:abc",
                "approved_artifact_fingerprint": expected_fingerprint,
            },
        }
    )
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        del tool_context
        captured["items"] = backlog_input.backlog_items
        return {"success": True, "saved_count": len(backlog_input.backlog_items)}

    payload = await save_backlog_draft(
        project_id=7,
        project_name="Backlog Project",
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=expected_fingerprint,
        expected_state="BACKLOG_REVIEW",
        idempotency_key="save-approved-refinement",
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert payload["save_result"]["success"] is True
    assert len(captured["items"]) == 1


@pytest.mark.asyncio
async def test_save_backlog_draft_blocks_unapproved_imported_refined_attempt() -> None:
    """An imported refined attempt has the same approval gate as refinements."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    state["backlog_attempts"][0]["attempt_kind"] = "import_refinement"
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=expected_fingerprint,
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-unapproved-import-refinement",
            save_state=lambda _updated: None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )

    assert "approval" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_save_backlog_draft_allows_approved_imported_refined_attempt() -> None:
    """Host approval makes an imported refined attempt saveable."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    state["backlog_attempts"][0].update(
        {
            "attempt_kind": "import_refinement",
            "operation_set_fingerprint": "sha256:ops",
        }
    )
    approval_result = mark_backlog_refinement_approved(
        state,
        request=BacklogRefinementApprovalRequest(
            project_id=7,
            attempt_id="backlog-attempt-1",
            operation_set_fingerprint="sha256:ops",
            approved_artifact_fingerprint=expected_fingerprint,
            approved_operation_ids=["op-import"],
            idempotency_key="approve-import-refinement",
        ),
        approval={
            "approval_id": "approval:import",
            "request_fingerprint": "sha256:approval-request",
        },
    )
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        del tool_context
        captured["items"] = backlog_input.backlog_items
        return {"success": True, "saved_count": len(backlog_input.backlog_items)}

    payload = await save_backlog_draft(
        project_id=7,
        project_name="Backlog Project",
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=expected_fingerprint,
        expected_state="BACKLOG_REVIEW",
        idempotency_key="save-approved-import-refinement",
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert approval_result["marked_saveable"] is True
    assert state["backlog_attempts"][0]["refinement_saveable"] is True
    assert state["product_backlog_assessment"]["refinement_saveable"] is True
    assert payload["save_result"]["success"] is True
    assert len(captured["items"]) == 1


@pytest.mark.asyncio
async def test_approval_does_not_make_incomplete_refinement_saveable() -> None:
    """Approval is recorded, but incomplete refined artifacts are not saveable."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": False,
            "clarifying_questions": ["Which risk tolerance should gate promotion?"],
        }
    )
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    state["backlog_attempts"][0].update(
        {
            "attempt_kind": "import_refinement",
            "operation_set_fingerprint": "sha256:ops",
        }
    )

    approval_result = mark_backlog_refinement_approved(
        state,
        request=BacklogRefinementApprovalRequest(
            project_id=7,
            attempt_id="backlog-attempt-1",
            operation_set_fingerprint="sha256:ops",
            approved_artifact_fingerprint=expected_fingerprint,
            approved_operation_ids=["op-import"],
            idempotency_key="approve-incomplete-import-refinement",
        ),
        approval={
            "approval_id": "approval:incomplete",
            "request_fingerprint": "sha256:approval-request",
        },
    )

    assert approval_result["marked_saveable"] is False
    assert state["backlog_attempts"][0]["refinement_approved"] is True
    assert state["backlog_attempts"][0]["refinement_saveable"] is False
    assert (
        state["backlog_attempts"][0]["refinement_approval"]["approval_id"]
        == "approval:incomplete"
    )
    assert state["product_backlog_assessment"]["refinement_approved"] is True
    assert state["product_backlog_assessment"]["refinement_saveable"] is False


@pytest.mark.asyncio
async def test_save_backlog_draft_persists_persistence_state() -> None:
    """Verify save backlog draft persists persistence state."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
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
    assert not hasattr(captured["backlog_input"], "append_only")
    assert not hasattr(captured["backlog_input"], "story_origin")
    assert not hasattr(captured["backlog_input"], "accepted_spec_version_id")
    assert (
        "_agileforge_internal_backlog_save_options"
        not in captured["tool_context"].state
    )
    assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert saved["state"]["backlog_saved_at"] == "2026-04-04T00:00:00Z"
    assert (
        saved["state"]["backlog_save_idempotency_keys"]["save-backlog-1"]["fsm_state"]
        == "BACKLOG_PERSISTENCE"
    )


@pytest.mark.asyncio
async def test_save_backlog_draft_masks_stale_internal_backlog_save_options() -> None:
    """Normal Backlog saves must hide stale host-only append options."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": True,
        }
    )
    state[INTERNAL_BACKLOG_SAVE_OPTIONS_KEY] = {
        "authorized_by": INTERNAL_BACKLOG_SAVE_OPTIONS_AUTHORITY,
        "append_only": True,
        "story_origin": "scope_extension",
        "accepted_spec_version_id": None,
    }
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        context = cast("_StatefulToolContext", tool_context)
        captured["internal_options_visible"] = (
            INTERNAL_BACKLOG_SAVE_OPTIONS_KEY in context.state
        )
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
        idempotency_key="save-backlog-stale-internal-options",
        save_state=lambda updated: saved.update({"state": dict(updated)}),
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert payload["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert captured["internal_options_visible"] is False
    assert INTERNAL_BACKLOG_SAVE_OPTIONS_KEY not in saved["state"]


@pytest.mark.asyncio
async def test_save_backlog_draft_internal_backlog_save_options_guarded() -> None:
    """Stale host-only append options must not bypass replacement blocking."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item(requirement="Replacement backlog")],
            "is_complete": True,
        }
    )
    state[INTERNAL_BACKLOG_SAVE_OPTIONS_KEY] = {
        "authorized_by": INTERNAL_BACKLOG_SAVE_OPTIONS_AUTHORITY,
        "append_only": True,
        "story_origin": "scope_extension",
        "accepted_spec_version_id": None,
    }
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    saved: JsonDict = {}
    test_engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(test_engine)
    with SqlSession(test_engine) as session:
        session.add(Product(name="Backlog Project"))
        session.commit()
        session.add(
            UserStory(
                product_id=1,
                title="Progressed seed backlog",
                status=StoryStatus.TO_DO,
                story_description="Already refined.",
                acceptance_criteria="- Verify existing story",
                source_requirement="progressed-seed-backlog",
                refinement_slot=1,
                story_origin="backlog_seed",
                is_refined=True,
                is_superseded=False,
            )
        )
        session.commit()

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with (
        patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ),
        pytest.raises(BacklogPhaseError) as exc_info,
    ):
        await save_backlog_draft(
            project_id=1,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=expected_fingerprint,
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-stale-internal-options-guard",
            save_state=lambda updated: saved.update({"state": dict(updated)}),
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=real_save_backlog_tool,
        )

    assert exc_info.value.detail == "BACKLOG_REPLACEMENT_BLOCKED"
    assert saved == {}
    with SqlSession(test_engine) as session:
        rows = session.exec(select(UserStory).where(UserStory.product_id == 1)).all()
    assert [row.title for row in rows] == ["Progressed seed backlog"]


@pytest.mark.asyncio
async def test_save_backlog_draft_scope_extension_uses_internal_options() -> None:
    """Scope-extension saves must authorize append/provenance outside tool input."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [
                _savable_backlog_item(
                    requirement="Add new reporting export",
                    authority_ref="REQ.reporting-export",
                )
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    input_context = {
        "generation_mode": "scope_extension",
        "scope_extension": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 11,
            "base_spec_hash": "sha256:base",
            "amended_spec_version_id": 12,
            "amended_spec_hash": "sha256:amended",
            "added_source_item_ids": ["REQ.reporting-export"],
        },
        "authority_scope_filter": {"source_item_ids": ["REQ.reporting-export"]},
    }
    state["scope_extension_context"] = dict(input_context["scope_extension"])
    state["backlog_attempts"][0]["input_context"] = input_context
    state["backlog_last_input_context"] = input_context
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    captured: JsonDict = {}
    saved: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        context = cast("_StatefulToolContext", tool_context)
        captured["backlog_input"] = backlog_input
        captured["tool_context"] = tool_context
        captured["internal_options"] = dict(
            context.state["_agileforge_internal_backlog_save_options"]
        )
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
        idempotency_key="save-scope-extension-backlog",
        save_state=lambda updated: saved.update({"state": dict(updated)}),
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    backlog_input = captured["backlog_input"]
    assert payload["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert not hasattr(backlog_input, "append_only")
    assert captured["internal_options"] == {
        "authorized_by": "services.phases.backlog_service",
        "append_only": True,
        "story_origin": "scope_extension",
        "accepted_spec_version_id": 12,
    }
    assert (
        "_agileforge_internal_backlog_save_options"
        not in captured["tool_context"].state
    )
    assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert (
        "_agileforge_internal_backlog_save_options"
        not in saved["state"]
    )
    assert (
        saved["state"]["scope_extension_context"]["backlog_extension_saved_at"]
        == "2026-04-04T00:00:00Z"
    )
    assert (
        saved["state"]["scope_extension_context"]["backlog_extension_attempt_id"]
        == "backlog-attempt-1"
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
async def test_reset_active_backlog_requires_next_cycle_origin() -> None:
    """Reset-active is only valid for next-cycle refinement review."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    state["backlog_review_origin"] = "initial_backlog"
    state["backlog_attempts"][0].update(
        {
            "attempt_kind": "import_refinement",
            "refinement_saveable": True,
            "refinement_approval": {
                "approval_id": "approval:reset",
                "approved_artifact_fingerprint": state["product_backlog_assessment"][
                    "artifact_fingerprint"
                ],
            },
        }
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await reset_active_backlog(
            project_id=7,
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=state["product_backlog_assessment"][
                "artifact_fingerprint"
            ],
            expected_state="BACKLOG_REVIEW",
            reset_reason="pre-brownfield reset",
            archive_all_active_stories=True,
            idempotency_key="reset-active-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-06-02T12:00:00Z",
            hydrate_context=hydrate_context,
            reset_rows=lambda _request: {"success": True},
            replacement_blocked=lambda _project_id: True,
        )

    assert "RESET_WRONG_REVIEW_ORIGIN" in exc_info.value.detail


def _reset_ready_state(
    *,
    attempt_kind: str | None = "refinement",
    approved: bool = True,
    artifact_overrides: JsonDict | None = None,
) -> JsonDict:
    """Return a reviewed next-cycle reset candidate state."""
    artifact: JsonDict = {
        "backlog_items": [_savable_backlog_item()],
        "is_complete": True,
        "clarifying_questions": [],
    }
    if artifact_overrides is not None:
        artifact.update(artifact_overrides)
    state = _review_state_for_artifact(artifact)
    state["backlog_review_origin"] = "next_cycle_refinement"
    expected_fingerprint = state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]
    if attempt_kind is not None:
        state["backlog_attempts"][0]["attempt_kind"] = attempt_kind
    if approved:
        state["backlog_attempts"][0].update(
            {
                "refinement_saveable": True,
                "refinement_approval": {
                    "approval_id": "approval:reset",
                    "approved_artifact_fingerprint": expected_fingerprint,
                },
            }
        )
    return state


@pytest.mark.asyncio
async def test_reset_active_backlog_updates_state_after_success() -> None:
    """Reset-active persists active reset metadata after row reset succeeds."""
    state = _reset_ready_state()
    expected_fingerprint = state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def reset_rows(request: object) -> JsonDict:
        captured["request"] = request
        return {"success": True, "archived_count": 2, "inserted_count": 1}

    payload = await reset_active_backlog(
        project_id=7,
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=expected_fingerprint,
        expected_state="BACKLOG_REVIEW",
        reset_reason="pre-brownfield reset",
        archive_all_active_stories=True,
        idempotency_key="reset-active-1",
        save_state=lambda updated: saved.update({"state": dict(updated)}),
        now_iso=lambda: "2026-06-02T12:00:00Z",
        hydrate_context=hydrate_context,
        reset_rows=reset_rows,
        replacement_blocked=lambda _project_id: True,
    )

    request = captured["request"]
    assert request.project_id == 7  # noqa: PLR2004
    assert request.attempt_id == "backlog-attempt-1"
    assert request.expected_artifact_fingerprint == expected_fingerprint
    assert request.reset_reason == "pre-brownfield reset"
    assert request.archive_all_active_stories is True
    assert request.idempotency_key == "reset-active-1"
    assert payload["reset_result"]["success"] is True
    assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert saved["state"]["backlog_saved_at"] == "2026-06-02T12:00:00Z"
    assert saved["state"]["downstream_backlog_stale"] is True
    assert saved["state"]["stale_backlog_reason"] == "active_backlog_reset"
    assert (
        saved["state"]["stale_since_backlog_attempt_id"]
        == "backlog-attempt-1"
    )
    assert saved["state"]["active_backlog_reset_at"] == "2026-06-02T12:00:00Z"
    assert (
        saved["state"]["active_backlog_reset_attempt_id"]
        == "backlog-attempt-1"
    )
    assert "backlog_items" not in saved["state"]


@pytest.mark.asyncio
async def test_reset_active_backlog_refuses_when_save_would_not_be_blocked() -> None:
    """Reset-active is only valid when the normal replacement guard blocks save."""
    state = _reset_ready_state()
    expected_fingerprint = state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]
    reset_calls: list[object] = []
    saved: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=copy.deepcopy(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await reset_active_backlog(
            project_id=7,
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=expected_fingerprint,
            expected_state="BACKLOG_REVIEW",
            reset_reason="pre-brownfield reset",
            archive_all_active_stories=True,
            idempotency_key="reset-active-1",
            save_state=lambda updated: saved.update({"state": dict(updated)}),
            now_iso=lambda: "2026-06-02T12:00:00Z",
            hydrate_context=hydrate_context,
            reset_rows=lambda request: reset_calls.append(request) or {"success": True},
            replacement_blocked=lambda _project_id: False,
        )

    assert "RESET_NOT_REQUIRED" in exc_info.value.detail
    assert reset_calls == []
    assert saved == {}


@pytest.mark.asyncio
async def test_reset_active_backlog_replays_same_key_after_persistence_state() -> None:
    """Same reset request replays after first call moves to persistence state."""
    current_state = _reset_ready_state()
    expected_fingerprint = current_state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]
    reset_calls: list[object] = []
    replacement_checks: list[str] = []

    async def hydrate_context() -> object:
        return SimpleNamespace(state=copy.deepcopy(current_state), session_id="7")

    def save_state(updated: JsonDict) -> None:
        current_state.clear()
        current_state.update(copy.deepcopy(updated))

    def reset_rows(request: object) -> JsonDict:
        reset_calls.append(request)
        if len(reset_calls) == 1:
            return {"success": True, "idempotent_replay": False, "created_count": 1}
        return {"success": True, "idempotent_replay": True, "created_count": 1}

    def replacement_blocked(_project_id: int) -> bool:
        fsm_state = str(current_state["fsm_state"])
        replacement_checks.append(fsm_state)
        assert fsm_state != "BACKLOG_PERSISTENCE"
        return True

    first = await reset_active_backlog(
        project_id=7,
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=expected_fingerprint,
        expected_state="BACKLOG_REVIEW",
        reset_reason="pre-brownfield reset",
        archive_all_active_stories=True,
        idempotency_key="reset-active-1",
        save_state=save_state,
        now_iso=lambda: "2026-06-02T12:00:00Z",
        hydrate_context=hydrate_context,
        reset_rows=reset_rows,
        replacement_blocked=replacement_blocked,
    )
    second = await reset_active_backlog(
        project_id=7,
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=expected_fingerprint,
        expected_state="BACKLOG_REVIEW",
        reset_reason="pre-brownfield reset",
        archive_all_active_stories=True,
        idempotency_key="reset-active-1",
        save_state=save_state,
        now_iso=lambda: "2026-06-02T12:00:01Z",
        hydrate_context=hydrate_context,
        reset_rows=reset_rows,
        replacement_blocked=replacement_blocked,
    )

    assert first["reset_result"]["idempotent_replay"] is False
    assert second["reset_result"]["idempotent_replay"] is True
    assert len(reset_calls) == 2  # noqa: PLR2004
    assert replacement_checks == ["BACKLOG_REVIEW"]
    assert current_state["fsm_state"] == "BACKLOG_PERSISTENCE"


@pytest.mark.asyncio
async def test_reset_active_backlog_recovers_state_from_committed_reset_event() -> None:
    """Same reset request repairs state after DB reset but state save fails."""
    state = _reset_ready_state()
    expected_fingerprint = state["product_backlog_assessment"]["artifact_fingerprint"]
    saved: JsonDict = {}
    replay_calls: list[ActiveBacklogResetRequest] = []
    reset_calls: list[ActiveBacklogResetRequest] = []

    async def hydrate_context() -> object:
        return SimpleNamespace(state=copy.deepcopy(state), session_id="7")

    def save_state(updated: JsonDict) -> None:
        saved["state"] = copy.deepcopy(updated)

    def reset_replay(request: ActiveBacklogResetRequest) -> JsonDict | None:
        replay_calls.append(request)
        return {"success": True, "idempotent_replay": True, "created_count": 1}

    payload = await reset_active_backlog(
        project_id=7,
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=expected_fingerprint,
        expected_state="BACKLOG_REVIEW",
        reset_reason="pre-brownfield reset",
        archive_all_active_stories=True,
        idempotency_key="reset-active-1",
        save_state=save_state,
        now_iso=lambda: "2026-06-02T12:00:01Z",
        hydrate_context=hydrate_context,
        reset_rows=lambda request: reset_calls.append(request) or {"success": True},
        reset_replay=reset_replay,
        replacement_blocked=lambda _project_id: False,
    )

    assert payload["reset_result"]["idempotent_replay"] is True
    assert len(replay_calls) == 1
    assert reset_calls == []
    assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert saved["state"]["active_backlog_reset_attempt_id"] == "backlog-attempt-1"
    assert (
        saved["state"]["active_backlog_reset_request_fingerprint"]
        == reset_request_fingerprint(replay_calls[0])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "attempt_id", "fingerprint", "expected_detail"),
    [
        (
            _reset_ready_state(),
            "missing-attempt",
            None,
            "RESET_ATTEMPT_NOT_FOUND",
        ),
        (
            _reset_ready_state(),
            "backlog-attempt-1",
            "sha256:wrong",
            "RESET_ARTIFACT_FINGERPRINT_MISMATCH",
        ),
        (
            _reset_ready_state(attempt_kind="generation"),
            "backlog-attempt-1",
            None,
            "RESET_NOT_REFINEMENT_ATTEMPT",
        ),
        (
            _reset_ready_state(approved=False),
            "backlog-attempt-1",
            None,
            "RESET_ATTEMPT_NOT_APPROVED",
        ),
        (
            _reset_ready_state(
                artifact_overrides={
                    "is_complete": False,
                    "clarifying_questions": [],
                }
            ),
            "backlog-attempt-1",
            None,
            "RESET_ATTEMPT_INCOMPLETE",
        ),
    ],
)
async def test_reset_active_backlog_uses_reset_specific_failure_codes(
    state: JsonDict,
    attempt_id: str,
    fingerprint: str | None,
    expected_detail: str,
) -> None:
    """Reset-active guard failures surface reset-specific error codes."""
    expected_fingerprint = fingerprint or state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]

    async def hydrate_context() -> object:
        return SimpleNamespace(state=copy.deepcopy(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await reset_active_backlog(
            project_id=7,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_fingerprint,
            expected_state="BACKLOG_REVIEW",
            reset_reason="pre-brownfield reset",
            archive_all_active_stories=True,
            idempotency_key="reset-active-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-06-02T12:00:00Z",
            hydrate_context=hydrate_context,
            reset_rows=lambda _request: {"success": True},
            replacement_blocked=lambda _project_id: True,
        )

    assert expected_detail in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_backlog_draft_blocks_unmatched_authority_ref_warning() -> None:
    """Save gate blocks only the closed list of brownfield blocker warnings."""
    artifact: JsonDict = {
        "backlog_items": [_savable_backlog_item()],
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
        "backlog_items": [_savable_backlog_item()],
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
