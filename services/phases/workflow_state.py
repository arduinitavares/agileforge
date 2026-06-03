"""Shared workflow state helpers for phase services."""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection
from typing import Any

from orchestrator_agent.fsm.states import OrchestratorState


class DownstreamBacklogStaleError(RuntimeError):
    """Raised when downstream generation is blocked by a stale backlog marker."""

    def __init__(self, *, reason: object, attempt_id: object) -> None:
        """Initialize the stale-backlog guard message."""
        super().__init__(
            f"downstream backlog is stale (reason: {reason}; attempt: {attempt_id})"
        )


def assert_downstream_backlog_not_stale(state: dict[str, Any]) -> None:
    """Block downstream generation while coarse backlog stale markers are set."""
    if state.get("downstream_backlog_stale") is not True:
        return

    reason = state.get("stale_backlog_reason") or "unknown"
    attempt_id = state.get("stale_since_backlog_attempt_id") or "unknown"
    raise DownstreamBacklogStaleError(
        reason=reason,
        attempt_id=attempt_id,
    )


def _non_empty_string(value: object) -> str | None:
    """Return stripped string values only when non-empty."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def assert_downstream_backlog_not_stale_for_roadmap(state: dict[str, Any]) -> None:
    """Block stale backlog except reset marker whose exit path is roadmap."""
    if state.get("downstream_backlog_stale") is not True:
        return

    stale_attempt_id = _non_empty_string(state.get("stale_since_backlog_attempt_id"))
    reset_attempt_id = _non_empty_string(state.get("active_backlog_reset_attempt_id"))
    fsm_state = state.get("fsm_state")
    reset_roadmap_states = {
        OrchestratorState.BACKLOG_PERSISTENCE.value,
        OrchestratorState.ROADMAP_INTERVIEW.value,
        OrchestratorState.ROADMAP_REVIEW.value,
    }
    if (
        fsm_state in reset_roadmap_states
        and state.get("stale_backlog_reason") == "active_backlog_reset"
        and stale_attempt_id is not None
        and stale_attempt_id == reset_attempt_id
    ):
        return

    assert_downstream_backlog_not_stale(state)


def failure_meta(
    source: dict[str, Any] | None,
    *,
    fallback_summary: str | None = None,
) -> dict[str, Any]:
    payload = source or {}
    return {
        "failure_artifact_id": payload.get("failure_artifact_id"),
        "failure_stage": payload.get("failure_stage"),
        "failure_summary": payload.get("failure_summary") or fallback_summary,
        "raw_output_preview": payload.get("raw_output_preview"),
        "has_full_artifact": bool(payload.get("has_full_artifact", False)),
    }


def phase_state_from_complete(
    is_complete: bool,
    *,
    review_state: str,
    interview_state: str,
) -> str:
    return review_state if is_complete else interview_state


def sprint_state_from_complete(is_complete: bool) -> str:
    return phase_state_from_complete(
        is_complete,
        review_state=OrchestratorState.SPRINT_DRAFT.value,
        interview_state=OrchestratorState.SPRINT_SETUP.value,
    )


def ensure_phase_attempts(
    state: dict[str, Any],
    *,
    attempts_key: str,
) -> list[dict[str, Any]]:
    attempts = state.get(attempts_key)
    if not isinstance(attempts, list):
        attempts = []
    return attempts


def record_phase_attempt(
    state: dict[str, Any],
    *,
    attempts_key: str,
    last_input_context_key: str,
    assessment_key: str,
    trigger: str,
    input_context: dict[str, Any],
    output_artifact: dict[str, Any],
    is_complete: bool,
    created_at: str,
    failure_source: dict[str, Any] | None = None,
    failure_summary_fallback: str | None = None,
    mirrored_output_field: str | None = None,
    mirrored_state_key: str | None = None,
    mirrored_output_types: tuple[type, ...] | None = None,
) -> int:
    attempts = ensure_phase_attempts(state, attempts_key=attempts_key)
    normalized_output_artifact = copy.deepcopy(output_artifact)
    normalized_input_context = copy.deepcopy(input_context)
    attempts.append(
        {
            "created_at": created_at,
            "trigger": trigger,
            "input_context": normalized_input_context,
            "output_artifact": normalized_output_artifact,
            "is_complete": is_complete,
            **failure_meta(
                failure_source,
                fallback_summary=failure_summary_fallback,
            ),
        }
    )
    state[attempts_key] = attempts
    state[last_input_context_key] = copy.deepcopy(normalized_input_context)
    state[assessment_key] = copy.deepcopy(normalized_output_artifact)

    if mirrored_output_field and mirrored_state_key:
        mirrored_value = normalized_output_artifact.get(mirrored_output_field)
        if mirrored_output_types is None:
            if mirrored_value is not None:
                state[mirrored_state_key] = copy.deepcopy(mirrored_value)
        elif isinstance(mirrored_value, mirrored_output_types):
            state[mirrored_state_key] = copy.deepcopy(mirrored_value)

    return len(attempts)


def set_phase_fsm_state(
    state: dict[str, Any],
    *,
    is_complete: bool,
    now_iso: Callable[[], str],
    review_state: str,
    interview_state: str,
    current_state: str | None = None,
    preserved_states: Collection[str] | None = None,
    persist_current_state: bool = False,
) -> str:
    if (
        current_state is not None
        and preserved_states is not None
        and current_state in preserved_states
    ):
        if persist_current_state:
            state["fsm_state"] = current_state
        return current_state

    next_state = phase_state_from_complete(
        is_complete,
        review_state=review_state,
        interview_state=interview_state,
    )
    state["fsm_state"] = next_state
    state["fsm_state_entered_at"] = now_iso()
    return next_state


def set_sprint_fsm_state(
    state: dict[str, Any],
    *,
    is_complete: bool,
    now_iso: Callable[[], str],
) -> str:
    return set_phase_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
        review_state=OrchestratorState.SPRINT_DRAFT.value,
        interview_state=OrchestratorState.SPRINT_SETUP.value,
    )
