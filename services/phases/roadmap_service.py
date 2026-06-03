"""Roadmap phase application service helpers."""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, cast

from orchestrator_agent.agent_tools.roadmap_builder.schemes import (
    RoadmapBuilderOutput,
)
from orchestrator_agent.agent_tools.roadmap_builder.tools import (
    SaveRoadmapToolInput,
)
from orchestrator_agent.fsm.states import OrchestratorState
from services.agent_workbench.fingerprints import canonical_hash
from services.phases import workflow_state

_PRESERVED_ROADMAP_STATES = {
    OrchestratorState.ROADMAP_PERSISTENCE.value,
    OrchestratorState.STORY_INTERVIEW.value,
    OrchestratorState.STORY_REVIEW.value,
    OrchestratorState.STORY_PERSISTENCE.value,
    OrchestratorState.SPRINT_SETUP.value,
    OrchestratorState.SPRINT_DRAFT.value,
    OrchestratorState.SPRINT_PERSISTENCE.value,
    OrchestratorState.SPRINT_VIEW.value,
    OrchestratorState.SPRINT_LIST.value,
    OrchestratorState.SPRINT_UPDATE_STORY.value,
    OrchestratorState.SPRINT_MODIFY.value,
    OrchestratorState.SPRINT_COMPLETE.value,
}
_VALID_FSM_STATES = {state.value for state in OrchestratorState}


class RoadmapPhaseError(Exception):
    """Domain-level roadmap phase error for router translation."""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _normalize_fsm_state(value: str | None) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in _VALID_FSM_STATES:
            return normalized
    return OrchestratorState.SETUP_REQUIRED.value


def _non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _active_reset_stale_attempt_id(state: dict[str, Any]) -> str | None:
    stale_attempt_id = _non_empty_string(state.get("stale_since_backlog_attempt_id"))
    reset_attempt_id = _non_empty_string(state.get("active_backlog_reset_attempt_id"))
    if (
        state.get("downstream_backlog_stale") is True
        and state.get("stale_backlog_reason") == "active_backlog_reset"
        and stale_attempt_id is not None
        and stale_attempt_id == reset_attempt_id
    ):
        return reset_attempt_id
    return None


def roadmap_state_from_complete(is_complete: bool) -> str:
    return workflow_state.phase_state_from_complete(
        is_complete,
        review_state=OrchestratorState.ROADMAP_REVIEW.value,
        interview_state=OrchestratorState.ROADMAP_INTERVIEW.value,
    )


def ensure_roadmap_attempts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return workflow_state.ensure_phase_attempts(
        state,
        attempts_key="roadmap_attempts",
    )


def _active_reset_provenance(state: dict[str, Any]) -> dict[str, str]:
    reset_attempt_id = _active_reset_stale_attempt_id(state)
    if reset_attempt_id is None:
        return {}

    provenance = {
        "active_backlog_reset_attempt_id": reset_attempt_id,
        "stale_since_backlog_attempt_id": reset_attempt_id,
    }
    reset_at = _non_empty_string(state.get("active_backlog_reset_at"))
    if reset_at is not None:
        provenance["active_backlog_reset_at"] = reset_at
    return provenance


def record_roadmap_attempt(
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
        attempts_key="roadmap_attempts",
        last_input_context_key="roadmap_last_input_context",
        assessment_key="product_roadmap_assessment",
        trigger=trigger,
        input_context=input_context,
        output_artifact=output_artifact,
        is_complete=is_complete,
        created_at=created_at,
        failure_source=failure_meta,
        mirrored_output_field="roadmap_releases",
        mirrored_state_key="roadmap_releases",
        mirrored_output_types=(list,),
    )


def set_roadmap_fsm_state(
    state: dict[str, Any],
    *,
    is_complete: bool,
    now_iso: Callable[[], str],
) -> str:
    current_state = _normalize_fsm_state(state.get("fsm_state"))
    return workflow_state.set_phase_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
        review_state=OrchestratorState.ROADMAP_REVIEW.value,
        interview_state=OrchestratorState.ROADMAP_INTERVIEW.value,
        current_state=current_state,
        preserved_states=_PRESERVED_ROADMAP_STATES,
        persist_current_state=True,
    )


async def generate_roadmap_draft(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    run_roadmap_agent: Callable[..., Awaitable[dict[str, Any]]],
    user_input: str | None,
) -> dict[str, Any]:
    state = await load_state()
    try:
        workflow_state.assert_downstream_backlog_not_stale_for_roadmap(state)
    except workflow_state.DownstreamBacklogStaleError as exc:
        raise RoadmapPhaseError(str(exc)) from exc

    has_refinable_draft = _has_refinable_roadmap_draft(state)
    normalized_user_input = (user_input or "").strip()
    if has_refinable_draft and not normalized_user_input:
        raise RoadmapPhaseError(
            "User input is required to refine an existing roadmap.",
            status_code=400,
        )

    roadmap_result = await run_roadmap_agent(
        state,
        project_id=project_id,
        user_input=normalized_user_input,
    )
    output_artifact = dict(roadmap_result.get("output_artifact") or {})
    is_complete = _effective_roadmap_completion(
        roadmap_result,
        output_artifact,
    )
    output_artifact["is_complete"] = is_complete
    artifact_fingerprint = _roadmap_artifact_fingerprint(output_artifact)

    input_context = dict(roadmap_result.get("input_context") or {})
    input_context.update(_active_reset_provenance(state))

    attempt_count = record_roadmap_attempt(
        state,
        trigger="manual_refine" if normalized_user_input else "auto_transition",
        input_context=input_context,
        output_artifact=output_artifact,
        is_complete=is_complete,
        failure_meta=roadmap_result,
        created_at=now_iso(),
    )
    attempt_id = f"roadmap-attempt-{attempt_count}"
    _attach_attempt_guards(
        state,
        attempt_id=attempt_id,
        artifact_fingerprint=artifact_fingerprint,
    )
    next_state = set_roadmap_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
    )
    save_state(state)

    return {
        "fsm_state": next_state,
        "is_complete": is_complete,
        "roadmap_run_success": bool(roadmap_result.get("success")),
        "error": roadmap_result.get("error"),
        "trigger": "manual_refine" if normalized_user_input else "auto_transition",
        "input_context": input_context,
        "output_artifact": output_artifact,
        "attempt_count": attempt_count,
        "attempt_id": attempt_id,
        "artifact_fingerprint": artifact_fingerprint,
        **workflow_state.failure_meta(
            roadmap_result, fallback_summary=roadmap_result.get("error")
        ),
    }


async def get_roadmap_history(
    *,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    state = await load_state()
    attempts = ensure_roadmap_attempts(state)
    return {
        "items": attempts,
        "count": len(attempts),
    }


async def save_roadmap_draft(
    *,
    project_id: int,
    attempt_id: str,
    expected_artifact_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    hydrate_context: Callable[[], Awaitable[Any]],
    build_tool_context: Callable[[Any], Any],
    save_roadmap_tool: Callable[[SaveRoadmapToolInput, Any], dict[str, Any]],
) -> dict[str, Any]:
    context = await hydrate_context()
    replay = _roadmap_save_replay(context.state, idempotency_key)
    if replay is not None:
        if _replay_matches_request(
            replay,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
        ):
            selected_attempt = _find_roadmap_attempt(context.state, attempt_id)
            if selected_attempt is not None and _maybe_clear_active_reset_stale_marker(
                context.state,
                selected_attempt=selected_attempt,
                now=now_iso(),
                clear_source="roadmap_save_replay",
            ):
                save_state(context.state)
        return replay

    _assert_save_expected_state(context.state, expected_state)
    assessment = context.state.get("product_roadmap_assessment")
    if not isinstance(assessment, dict):
        raise RoadmapPhaseError("No roadmap draft available to save")
    selected_attempt = _assert_save_guards(
        state=context.state,
        assessment=assessment,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )

    if not bool(assessment.get("is_complete", False)):
        raise RoadmapPhaseError("Roadmap cannot be saved until is_complete is true")
    if _has_clarifying_questions(assessment):
        raise RoadmapPhaseError("Roadmap cannot be saved while questions remain")

    roadmap_assessment = {
        key: value
        for key, value in assessment.items()
        if key not in {"attempt_id", "artifact_fingerprint"}
    }
    try:
        roadmap_data = RoadmapBuilderOutput.model_validate(roadmap_assessment)
    except Exception as exc:  # pylint: disable=broad-except
        raise RoadmapPhaseError(
            f"Invalid roadmap data in session: {exc!s}",
            status_code=500,
        ) from exc

    _assert_exact_backlog_coverage(context.state, roadmap_data)

    result = save_roadmap_tool(
        SaveRoadmapToolInput(
            product_id=project_id,
            roadmap_data=roadmap_data,
            idempotency_key=idempotency_key,
        ),
        build_tool_context(context),
    )
    if inspect.isawaitable(result):
        result = await result
    result = cast("dict[str, Any]", result)

    if not result.get("success"):
        raise RoadmapPhaseError(
            result.get("error", "Failed to save roadmap"),
            status_code=500,
        )

    saved_at = now_iso()
    context.state["fsm_state"] = OrchestratorState.ROADMAP_PERSISTENCE.value
    context.state["fsm_state_entered_at"] = saved_at
    context.state["roadmap_saved_at"] = saved_at
    _maybe_clear_active_reset_stale_marker(
        context.state,
        selected_attempt=selected_attempt,
        now=saved_at,
        clear_source="roadmap_save",
    )

    payload = {
        "fsm_state": OrchestratorState.ROADMAP_PERSISTENCE.value,
        "save_result": result,
        "attempt_id": attempt_id,
        "artifact_fingerprint": expected_artifact_fingerprint,
        "idempotency_key": idempotency_key,
    }
    _record_roadmap_save_replay(context.state, idempotency_key, payload)
    save_state(context.state)

    return payload


def _effective_roadmap_completion(
    roadmap_result: dict[str, Any],
    output_artifact: dict[str, Any],
) -> bool:
    """Return completion after enforcing runtime consistency rules."""
    if not roadmap_result.get("success"):
        return False
    if _has_clarifying_questions(output_artifact):
        return False
    return bool(roadmap_result.get("is_complete"))


def _has_refinable_roadmap_draft(state: dict[str, Any]) -> bool:
    assessment = state.get("product_roadmap_assessment")
    if isinstance(assessment, dict) and isinstance(
        assessment.get("roadmap_releases"), list
    ):
        return True
    return isinstance(state.get("roadmap_releases"), list)


def _has_clarifying_questions(artifact: dict[str, Any]) -> bool:
    questions = artifact.get("clarifying_questions")
    return isinstance(questions, list) and any(
        isinstance(question, str) and bool(question.strip()) for question in questions
    )


def _roadmap_artifact_fingerprint(output_artifact: dict[str, Any]) -> str:
    return canonical_hash({"phase": "roadmap", "output_artifact": output_artifact})


def _attach_attempt_guards(
    state: dict[str, Any],
    *,
    attempt_id: str,
    artifact_fingerprint: str,
) -> None:
    attempts = ensure_roadmap_attempts(state)
    if attempts:
        attempts[-1]["attempt_id"] = attempt_id
        attempts[-1]["artifact_fingerprint"] = artifact_fingerprint
        output_artifact = attempts[-1].get("output_artifact")
        if isinstance(output_artifact, dict):
            output_artifact["attempt_id"] = attempt_id
            output_artifact["artifact_fingerprint"] = artifact_fingerprint

    assessment = state.get("product_roadmap_assessment")
    if isinstance(assessment, dict):
        assessment["attempt_id"] = attempt_id
        assessment["artifact_fingerprint"] = artifact_fingerprint


def _roadmap_save_replay(
    state: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    saves = state.get("roadmap_save_idempotency_keys")
    if not isinstance(saves, dict):
        return None
    payload = saves.get(idempotency_key)
    return dict(payload) if isinstance(payload, dict) else None


def _replay_matches_request(
    payload: dict[str, Any],
    *,
    attempt_id: str,
    expected_artifact_fingerprint: str,
) -> bool:
    return (
        payload.get("attempt_id") == attempt_id
        and payload.get("artifact_fingerprint") == expected_artifact_fingerprint
    )


def _record_roadmap_save_replay(
    state: dict[str, Any],
    idempotency_key: str,
    payload: dict[str, Any],
) -> None:
    saves = state.get("roadmap_save_idempotency_keys")
    if not isinstance(saves, dict):
        saves = {}
    saves[idempotency_key] = dict(payload)
    state["roadmap_save_idempotency_keys"] = saves


def _assert_save_expected_state(state: dict[str, Any], expected_state: str) -> None:
    if expected_state != OrchestratorState.ROADMAP_REVIEW.value:
        raise RoadmapPhaseError(
            "Roadmap save expected_state must be ROADMAP_REVIEW",
        )
    fsm_state = _normalize_fsm_state(cast("str | None", state.get("fsm_state")))
    if fsm_state != expected_state:
        raise RoadmapPhaseError(
            f"Roadmap save stale state: expected {expected_state}, got {fsm_state}",
        )


def _assert_save_guards(
    *,
    state: dict[str, Any],
    assessment: dict[str, Any],
    attempt_id: str,
    expected_artifact_fingerprint: str,
) -> dict[str, Any]:
    current_attempt_id = assessment.get("attempt_id")
    current_fingerprint = assessment.get("artifact_fingerprint")
    selected_attempt = _find_roadmap_attempt(state, attempt_id)
    if (
        current_attempt_id != attempt_id
        or current_fingerprint != expected_artifact_fingerprint
        or selected_attempt is None
        or selected_attempt.get("artifact_fingerprint") != expected_artifact_fingerprint
    ):
        raise RoadmapPhaseError(
            "Roadmap save guard mismatch: draft attempt or artifact fingerprint "
            "does not match the reviewed Roadmap draft.",
        )
    return selected_attempt


def _find_roadmap_attempt(
    state: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    attempts = ensure_roadmap_attempts(state)
    for attempt in attempts:
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def _maybe_clear_active_reset_stale_marker(
    state: dict[str, Any],
    *,
    selected_attempt: dict[str, Any],
    now: str,
    clear_source: str,
) -> bool:
    reset_attempt_id = _active_reset_stale_attempt_id(state)
    if reset_attempt_id is None:
        return False

    if not _roadmap_attempt_matches_active_reset(
        state,
        selected_attempt=selected_attempt,
        reset_attempt_id=reset_attempt_id,
    ):
        return False

    state["downstream_backlog_stale"] = False
    state.pop("stale_backlog_reason", None)
    state.pop("stale_since_backlog_attempt_id", None)
    state["active_backlog_stale_cleared_at"] = now
    state["active_backlog_stale_cleared_by"] = clear_source
    return True


def _roadmap_attempt_matches_active_reset(
    state: dict[str, Any],
    *,
    selected_attempt: dict[str, Any],
    reset_attempt_id: str,
) -> bool:
    input_context = selected_attempt.get("input_context")
    if isinstance(input_context, dict):
        attempt_reset_id = _non_empty_string(
            input_context.get("active_backlog_reset_attempt_id")
        )
        if attempt_reset_id is not None:
            return attempt_reset_id == reset_attempt_id

    return _legacy_roadmap_attempt_is_after_active_reset(
        state,
        selected_attempt=selected_attempt,
    )


def _legacy_roadmap_attempt_is_after_active_reset(
    state: dict[str, Any],
    *,
    selected_attempt: dict[str, Any],
) -> bool:
    reset_at = _non_empty_string(state.get("active_backlog_reset_at"))
    roadmap_saved_at = _non_empty_string(state.get("roadmap_saved_at"))
    attempt_created_at = _non_empty_string(selected_attempt.get("created_at"))
    if reset_at is None or roadmap_saved_at is None or attempt_created_at is None:
        return False

    return attempt_created_at >= reset_at and roadmap_saved_at >= reset_at


def _assert_exact_backlog_coverage(
    state: dict[str, Any],
    roadmap_data: RoadmapBuilderOutput,
) -> None:
    expected_items = _active_backlog_requirement_names(state)
    scheduled_items = [
        item.strip()
        for release in roadmap_data.roadmap_releases
        for item in release.items
        if isinstance(item, str) and item.strip()
    ]
    expected_counts = Counter(expected_items)
    scheduled_counts = Counter(scheduled_items)
    missing = sorted(
        item
        for item, count in expected_counts.items()
        if scheduled_counts[item] < count
    )
    unknown = sorted(
        item
        for item, count in scheduled_counts.items()
        if expected_counts[item] < count
    )
    duplicate = sorted(item for item, count in scheduled_counts.items() if count > 1)
    if missing or unknown or duplicate:
        raise RoadmapPhaseError(
            "Roadmap coverage mismatch: "
            f"missing={missing}, unknown={unknown}, duplicate={duplicate}",
        )


def _active_backlog_requirement_names(state: dict[str, Any]) -> list[str]:
    backlog_items = state.get("backlog_items")
    if not isinstance(backlog_items, list) or not backlog_items:
        raise RoadmapPhaseError("Roadmap save requires active Backlog items")

    names: list[str] = []
    for item in backlog_items:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("requirement") or item.get("title")
        if isinstance(raw_name, str) and raw_name.strip():
            names.append(raw_name.strip())
    if not names:
        raise RoadmapPhaseError("Roadmap save requires active Backlog items")
    return names


__all__ = [
    "RoadmapPhaseError",
    "ensure_roadmap_attempts",
    "generate_roadmap_draft",
    "get_roadmap_history",
    "record_roadmap_attempt",
    "roadmap_state_from_complete",
    "save_roadmap_draft",
    "set_roadmap_fsm_state",
]
