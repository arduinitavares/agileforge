"""Backlog phase application service helpers."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from orchestrator_agent.agent_tools.backlog_primer.tools import SaveBacklogInput
from orchestrator_agent.fsm.states import OrchestratorState
from services.agent_workbench.fingerprints import canonical_hash
from services.phases import workflow_state

VALID_BACKLOG_GENERATION_STATES = {
    OrchestratorState.VISION_PERSISTENCE.value,
    OrchestratorState.BACKLOG_INTERVIEW.value,
    OrchestratorState.BACKLOG_REVIEW.value,
    OrchestratorState.BACKLOG_PERSISTENCE.value,
    OrchestratorState.ROADMAP_INTERVIEW.value,
}
VALID_FSM_STATES = {state.value for state in OrchestratorState}
BACKLOG_RUNTIME_DIAGNOSTIC_KEYS: tuple[str, ...] = ()


class BacklogPhaseError(Exception):
    """Domain-level backlog phase error for router translation."""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _normalize_fsm_state(value: str | None) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in VALID_FSM_STATES:
            return normalized
    return OrchestratorState.SETUP_REQUIRED.value


def backlog_state_from_complete(is_complete: bool) -> str:
    return workflow_state.phase_state_from_complete(
        is_complete,
        review_state=OrchestratorState.BACKLOG_REVIEW.value,
        interview_state=OrchestratorState.BACKLOG_INTERVIEW.value,
    )


def ensure_backlog_attempts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return workflow_state.ensure_phase_attempts(
        state,
        attempts_key="backlog_attempts",
    )


def record_backlog_attempt(
    state: dict[str, Any],
    *,
    trigger: str,
    input_context: dict[str, Any],
    output_artifact: dict[str, Any],
    is_complete: bool,
    created_at: str,
    failure_meta: dict[str, Any] | None = None,
) -> int:
    return workflow_state.record_phase_attempt(
        state,
        attempts_key="backlog_attempts",
        last_input_context_key="backlog_last_input_context",
        assessment_key="product_backlog_assessment",
        trigger=trigger,
        input_context=input_context,
        output_artifact=output_artifact,
        is_complete=is_complete,
        created_at=created_at,
        failure_source=failure_meta,
        mirrored_output_field="backlog_items",
        mirrored_state_key="backlog_items",
        mirrored_output_types=(list,),
    )


def _backlog_runtime_diagnostics(source: dict[str, Any]) -> dict[str, Any]:
    """Return bounded runtime diagnostics that should survive phase wrapping."""
    return {
        key: source[key]
        for key in BACKLOG_RUNTIME_DIAGNOSTIC_KEYS
        if source.get(key) is not None
    }


def set_backlog_fsm_state(
    state: dict[str, Any],
    *,
    is_complete: bool,
    now_iso: Callable[[], str],
) -> str:
    return workflow_state.set_phase_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
        review_state=OrchestratorState.BACKLOG_REVIEW.value,
        interview_state=OrchestratorState.BACKLOG_INTERVIEW.value,
    )


async def generate_backlog_draft(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    run_backlog_agent: Callable[..., Awaitable[dict[str, Any]]],
    user_input: str | None,
) -> dict[str, Any]:
    state = await load_state()
    fsm_state = _normalize_fsm_state(state.get("fsm_state"))
    if fsm_state == OrchestratorState.SETUP_REQUIRED.value:
        raise BacklogPhaseError("Setup required before backlog")

    if fsm_state not in VALID_BACKLOG_GENERATION_STATES:
        raise BacklogPhaseError(f"Invalid FSM State for backlog: {fsm_state}")

    assessment = state.get("product_backlog_assessment")
    has_refinable_draft = isinstance(state.get("backlog_items"), list) or (
        isinstance(assessment, dict)
        and isinstance(assessment.get("backlog_items"), list)
    )
    normalized_user_input = (user_input or "").strip()
    if has_refinable_draft and not normalized_user_input:
        raise BacklogPhaseError(
            "Feedback is required for Backlog refinement attempts",
        )

    backlog_result = await run_backlog_agent(
        state,
        project_id=project_id,
        user_input=normalized_user_input,
    )
    output_artifact = dict(backlog_result.get("output_artifact") or {})
    is_complete = _effective_backlog_completion(backlog_result, output_artifact)
    output_artifact["is_complete"] = is_complete
    artifact_fingerprint = _backlog_artifact_fingerprint(output_artifact)

    attempt_count = record_backlog_attempt(
        state,
        trigger="manual_refine" if normalized_user_input else "auto_transition",
        input_context=backlog_result.get("input_context") or {},
        output_artifact=output_artifact,
        is_complete=is_complete,
        failure_meta=backlog_result,
        created_at=now_iso(),
    )
    attempt_id = f"backlog-attempt-{attempt_count}"
    _attach_attempt_guards(
        state,
        attempt_id=attempt_id,
        artifact_fingerprint=artifact_fingerprint,
    )
    next_state = set_backlog_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
    )
    save_state(state)

    return {
        "fsm_state": next_state,
        "is_complete": is_complete,
        "backlog_run_success": bool(backlog_result.get("success")),
        "error": backlog_result.get("error"),
        "trigger": "manual_refine" if normalized_user_input else "auto_transition",
        "input_context": backlog_result.get("input_context"),
        "output_artifact": output_artifact,
        "attempt_count": attempt_count,
        "attempt_id": attempt_id,
        "artifact_fingerprint": artifact_fingerprint,
        **workflow_state.failure_meta(
            backlog_result, fallback_summary=backlog_result.get("error")
        ),
        **_backlog_runtime_diagnostics(backlog_result),
    }


async def preview_backlog_draft(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    run_backlog_agent: Callable[..., Awaitable[dict[str, Any]]],
    user_input: str | None,
) -> dict[str, Any]:
    """Generate a Backlog preview without recording attempts or changing state."""
    state = await load_state()
    fsm_state = _normalize_fsm_state(state.get("fsm_state"))
    if fsm_state == OrchestratorState.SETUP_REQUIRED.value:
        raise BacklogPhaseError("Setup required before backlog")

    normalized_user_input = (user_input or "").strip()
    backlog_result = await run_backlog_agent(
        state,
        project_id=project_id,
        user_input=normalized_user_input,
    )
    output_artifact = dict(backlog_result.get("output_artifact") or {})
    is_complete = _effective_backlog_completion(backlog_result, output_artifact)
    output_artifact["is_complete"] = is_complete
    artifact_fingerprint = _backlog_artifact_fingerprint(output_artifact)

    return {
        "fsm_state": fsm_state,
        "is_complete": is_complete,
        "backlog_run_success": bool(backlog_result.get("success")),
        "error": backlog_result.get("error"),
        "trigger": "preview",
        "input_context": backlog_result.get("input_context"),
        "output_artifact": output_artifact,
        "attempt_count": None,
        "attempt_id": None,
        "artifact_fingerprint": artifact_fingerprint,
        "persisted": False,
        **workflow_state.failure_meta(
            backlog_result, fallback_summary=backlog_result.get("error")
        ),
        **_backlog_runtime_diagnostics(backlog_result),
    }


async def get_backlog_history(
    *,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    state = await load_state()
    attempts = ensure_backlog_attempts(state)
    return {
        "items": attempts,
        "count": len(attempts),
    }


async def save_backlog_draft(
    *,
    project_id: int,
    project_name: str,
    attempt_id: str,
    expected_artifact_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    hydrate_context: Callable[[], Awaitable[Any]],
    build_tool_context: Callable[[Any], Any],
    save_backlog_tool: Callable[
        [SaveBacklogInput, Any],
        dict[str, Any] | Awaitable[dict[str, Any]],
    ],
) -> dict[str, Any]:
    _ = project_name
    context = await hydrate_context()
    replay = _backlog_save_replay(context.state, idempotency_key)
    if replay is not None:
        return replay

    _assert_save_expected_state(context.state, expected_state)
    assessment = context.state.get("product_backlog_assessment")
    if not isinstance(assessment, dict):
        raise BacklogPhaseError("No backlog draft available to save")
    _assert_save_guards(
        state=context.state,
        assessment=assessment,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    _assert_brownfield_save_gate(assessment)

    if not bool(assessment.get("is_complete", False)):
        raise BacklogPhaseError("Backlog cannot be saved until is_complete is true")
    if _has_clarifying_questions(assessment):
        raise BacklogPhaseError("Backlog cannot be saved while questions remain")

    items = assessment.get("backlog_items")
    if not isinstance(items, list) or len(items) == 0:
        raise BacklogPhaseError("Backlog items are empty")

    result = save_backlog_tool(
        SaveBacklogInput(
            product_id=project_id,
            backlog_items=items,
            idempotency_key=idempotency_key,
        ),
        build_tool_context(context),
    )
    if inspect.isawaitable(result):
        result = await result
    result = cast("dict[str, Any]", result)

    if not result.get("success"):
        raise BacklogPhaseError(
            result.get("error", "Failed to save backlog"),
            status_code=500,
        )

    context.state["fsm_state"] = OrchestratorState.BACKLOG_PERSISTENCE.value
    context.state["fsm_state_entered_at"] = now_iso()
    context.state["backlog_saved_at"] = now_iso()

    payload = {
        "fsm_state": OrchestratorState.BACKLOG_PERSISTENCE.value,
        "save_result": result,
        "attempt_id": attempt_id,
        "artifact_fingerprint": expected_artifact_fingerprint,
        "idempotency_key": idempotency_key,
    }
    _record_backlog_save_replay(context.state, idempotency_key, payload)
    save_state(context.state)

    return payload


def _effective_backlog_completion(
    backlog_result: dict[str, Any],
    output_artifact: dict[str, Any],
) -> bool:
    """Return completion after enforcing runtime consistency rules."""
    if not backlog_result.get("success"):
        return False
    if _has_clarifying_questions(output_artifact):
        return False
    return bool(backlog_result.get("is_complete"))


def _has_clarifying_questions(artifact: dict[str, Any]) -> bool:
    questions = artifact.get("clarifying_questions")
    return isinstance(questions, list) and any(
        isinstance(question, str) and bool(question.strip()) for question in questions
    )


def _backlog_artifact_fingerprint(output_artifact: dict[str, Any]) -> str:
    normalized_artifact = copy.deepcopy(output_artifact)
    normalized_artifact.pop("attempt_id", None)
    normalized_artifact.pop("artifact_fingerprint", None)
    return canonical_hash(
        {"phase": "backlog", "output_artifact": normalized_artifact}
    )


def _attach_attempt_guards(
    state: dict[str, Any],
    *,
    attempt_id: str,
    artifact_fingerprint: str,
) -> None:
    attempts = ensure_backlog_attempts(state)
    if attempts:
        attempts[-1]["attempt_id"] = attempt_id
        attempts[-1]["artifact_fingerprint"] = artifact_fingerprint
        output_artifact = attempts[-1].get("output_artifact")
        if isinstance(output_artifact, dict):
            output_artifact["attempt_id"] = attempt_id
            output_artifact["artifact_fingerprint"] = artifact_fingerprint

    assessment = state.get("product_backlog_assessment")
    if isinstance(assessment, dict):
        assessment["attempt_id"] = attempt_id
        assessment["artifact_fingerprint"] = artifact_fingerprint


def _backlog_save_replay(
    state: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    saves = state.get("backlog_save_idempotency_keys")
    if not isinstance(saves, dict):
        return None
    payload = saves.get(idempotency_key)
    return dict(payload) if isinstance(payload, dict) else None


def _record_backlog_save_replay(
    state: dict[str, Any],
    idempotency_key: str,
    payload: dict[str, Any],
) -> None:
    saves = state.get("backlog_save_idempotency_keys")
    if not isinstance(saves, dict):
        saves = {}
    saves[idempotency_key] = dict(payload)
    state["backlog_save_idempotency_keys"] = saves


def _assert_save_expected_state(state: dict[str, Any], expected_state: str) -> None:
    if expected_state != OrchestratorState.BACKLOG_REVIEW.value:
        raise BacklogPhaseError(
            "Backlog save expected_state must be BACKLOG_REVIEW",
        )
    fsm_state = _normalize_fsm_state(cast("str | None", state.get("fsm_state")))
    if fsm_state != expected_state:
        raise BacklogPhaseError(
            f"Backlog save stale state: expected {expected_state}, got {fsm_state}",
        )


def _assert_save_guards(
    *,
    state: dict[str, Any],
    assessment: dict[str, Any],
    attempt_id: str,
    expected_artifact_fingerprint: str,
) -> None:
    current_attempt_id = assessment.get("attempt_id")
    current_fingerprint = assessment.get("artifact_fingerprint")
    selected_attempt = _find_backlog_attempt(state, attempt_id)
    selected_artifact = (
        selected_attempt.get("output_artifact")
        if isinstance(selected_attempt, dict)
        else None
    )
    if (
        current_attempt_id != attempt_id
        or current_fingerprint != expected_artifact_fingerprint
        or selected_attempt is None
        or selected_attempt.get("artifact_fingerprint") != expected_artifact_fingerprint
        or not isinstance(selected_artifact, dict)
        or _backlog_artifact_fingerprint(assessment)
        != expected_artifact_fingerprint
        or _backlog_artifact_fingerprint(selected_artifact)
        != expected_artifact_fingerprint
    ):
        raise BacklogPhaseError(
            "Backlog save guard mismatch: draft attempt or artifact fingerprint "
            "does not match the reviewed Backlog draft.",
        )


def _assert_brownfield_save_gate(assessment: dict[str, Any]) -> None:
    warnings = assessment.get("brownfield_warnings")
    if not isinstance(warnings, list):
        return
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        if warning.get("code") == "asserted_authority_ref_unmatched":
            raise BacklogPhaseError(
                "Backlog save blocked by brownfield warning: "
                "asserted_authority_ref_unmatched",
            )


def _find_backlog_attempt(
    state: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    attempts = ensure_backlog_attempts(state)
    for attempt in attempts:
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


__all__ = [
    "BacklogPhaseError",
    "backlog_state_from_complete",
    "ensure_backlog_attempts",
    "generate_backlog_draft",
    "get_backlog_history",
    "preview_backlog_draft",
    "record_backlog_attempt",
    "save_backlog_draft",
    "set_backlog_fsm_state",
]
