"""Tests for backlog phase service."""

from types import SimpleNamespace
from typing import Any, Never

import pytest

from orchestrator_agent.agent_tools.backlog_primer.schemes import BacklogItem
from orchestrator_agent.agent_tools.backlog_primer.tools import SaveBacklogInput
from services.phases.backlog_service import (
    BacklogPhaseError,
    _backlog_artifact_fingerprint,
    _backlog_items_for_persistence,
    backlog_state_from_complete,
    ensure_backlog_attempts,
    generate_backlog_draft,
    get_backlog_history,
    record_backlog_attempt,
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
async def test_save_backlog_draft_requires_complete_assessment() -> None:
    """Verify save backlog draft requires complete assessment."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [{"title": "Seed backlog item"}],
            "is_complete": False,
        }
    )
    expected_fingerprint = state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]

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
    expected_fingerprint = state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]

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
    expected_fingerprint = state["product_backlog_assessment"][
        "artifact_fingerprint"
    ]
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
async def test_save_backlog_draft_strips_host_annotations_before_tool() -> None:
    """Host-derived annotations must not reach BacklogItem persistence validation."""
    artifact: JsonDict = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Harden captain-aware optimizer contract",
                "authority_ref": "REQ.captain-aware-optimization",
                "capability_hint": "Captain-Aware Squad Optimizer",
                "as_built_annotation": {
                    "schema_version": "agileforge.brownfield_annotation.v1",
                    "source": "host_derived",
                    "match_tier": "exact",
                    "match_basis": ["authority_ref"],
                },
                "value_driver": "Strategic",
                "justification": "As-Built evidence indicates existing behavior.",
                "estimated_effort": "M",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
        "brownfield_warnings": [],
    }
    state = _review_state_for_artifact(artifact)
    captured: JsonDict = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> JsonDict:
        del tool_context
        captured["backlog_input"] = backlog_input
        return {"success": True, "product_id": backlog_input.product_id}

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
        save_backlog_tool=fake_save_backlog_tool,
    )

    saved_items = captured["backlog_input"].backlog_items
    assert len(saved_items) == 1
    assert "as_built_annotation" not in saved_items[0]
    assert saved_items[0]["authority_ref"] == "REQ.captain-aware-optimization"
    BacklogItem.model_validate(saved_items[0])


def test_backlog_items_for_persistence_validates_against_backlog_item_schema() -> None:
    """Stripped brownfield items must satisfy the persistence BacklogItem contract."""
    annotated_item: JsonDict = {
        "priority": 1,
        "requirement": "Harden captain-aware optimizer contract",
        "authority_ref": "REQ.captain-aware-optimization",
        "capability_hint": "Captain-Aware Squad Optimizer",
        "as_built_annotation": {
            "schema_version": "agileforge.brownfield_annotation.v1",
            "source": "host_derived",
        },
        "value_driver": "Strategic",
        "justification": "As-Built evidence indicates existing behavior.",
        "estimated_effort": "M",
    }
    persisted = _backlog_items_for_persistence([annotated_item])
    BacklogItem.model_validate(persisted[0])


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
