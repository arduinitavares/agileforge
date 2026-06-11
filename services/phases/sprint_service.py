"""Sprint phase state helpers.

This module owns sprint planner working-state mutation and normalization while
the HTTP handlers remain in ``api.py`` during the transitional extraction.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeGuard, cast

from orchestrator_agent.agent_tools.sprint_planner_tool.schemes import (
    SprintPlannerOutput,
)
from orchestrator_agent.agent_tools.sprint_planner_tool.tools import (
    SaveSprintPlanInput,
)
from orchestrator_agent.fsm.states import OrchestratorState
from services.agent_workbench.fingerprints import canonical_hash
from services.phases import workflow_state
from services.sprint_input import load_sprint_candidates
from services.sprint_runtime import PUBLIC_TASK_KIND_VALUES

VALID_SPRINT_GENERATION_STATES = {
    OrchestratorState.STORY_PERSISTENCE.value,
    OrchestratorState.SPRINT_SETUP.value,
    OrchestratorState.SPRINT_DRAFT.value,
    OrchestratorState.SPRINT_PERSISTENCE.value,
}
VALID_FSM_STATES = {state.value for state in OrchestratorState}


class SprintPhaseError(Exception):
    """Domain-level sprint phase error for router translation."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 409,
        details: dict[str, Any] | None = None,
        remediation: Sequence[str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.details = dict(details or {})
        self.remediation = list(remediation or [])


def _status_key(status: object) -> str | None:
    value = getattr(status, "value", status)
    if value is None:
        return None
    return str(value).strip().lower()


def _status_value(status: object) -> str | None:
    value = getattr(status, "value", status)
    if value is None:
        return None
    return str(value).strip()


def _snapshot_story_payload(story: Any) -> dict[str, Any]:
    if hasattr(story, "model_dump"):
        return dict(story.model_dump(mode="json"))
    if isinstance(story, dict):
        return dict(story)
    return dict(vars(story))


def ensure_sprint_attempts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return workflow_state.ensure_phase_attempts(
        state,
        attempts_key="sprint_attempts",
    )


def sprint_state_from_complete(is_complete: bool) -> str:
    return workflow_state.sprint_state_from_complete(is_complete)


def set_sprint_fsm_state(
    state: dict[str, Any],
    *,
    is_complete: bool,
    now_iso: Callable[[], str],
) -> str:
    return workflow_state.set_sprint_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
    )


def reset_sprint_planner_working_set(state: dict[str, Any]) -> None:
    state["sprint_attempts"] = []
    state["sprint_last_input_context"] = None
    state["sprint_plan_assessment"] = None
    state["sprint_saved_at"] = None
    state["sprint_planner_owner_sprint_id"] = None


def _load_sprint_candidates_for_state(
    project_id: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Load sprint candidates filtered by any completed Story scope."""
    return load_sprint_candidates(
        project_id,
        story_completion_scope=state.get("story_completion_scope"),
    )


def reset_stale_saved_sprint_planner_working_set(
    state: dict[str, Any],
    *,
    current_planned_sprint_id: int | None,
) -> bool:
    owner_sprint_id = state.get("sprint_planner_owner_sprint_id")
    if owner_sprint_id is None:
        return False

    if owner_sprint_id == current_planned_sprint_id:
        return False

    reset_sprint_planner_working_set(state)
    return True


def _normalize_sprint_validation_error(
    error: object, allowed_task_kinds: str
) -> str | None:
    hint: str | None = None
    if isinstance(error, str):
        trimmed = error.strip()
        if trimmed:
            described_task_kind = re.match(
                r"Task '(?P<description>[^']+)' has invalid task_kind\.",
                trimmed,
            )
            unsupported_task_kind = re.match(
                r"Unsupported task_kind '(?P<value>[^']+)'\.",
                trimmed,
            )
            if described_task_kind and "other" in trimmed:
                hint = (
                    f"Task '{described_task_kind.group('description')}' has "
                    f"invalid task_kind. Use one of: {allowed_task_kinds}."
                )
            elif unsupported_task_kind:
                hint = (
                    f"Unsupported task_kind "
                    f"'{unsupported_task_kind.group('value').strip()}'. "
                    f"Use one of: {allowed_task_kinds}."
                )
            elif "task_kind" in trimmed and "other" in trimmed:
                hint = f"Task has invalid task_kind. Use one of: {allowed_task_kinds}."
            else:
                hint = trimmed
    elif isinstance(error, dict):
        error_dict = cast("dict[str, object]", error)
        loc = error_dict.get("loc")
        if isinstance(loc, (list, tuple)) and loc and loc[-1] == "task_kind":
            input_value = error_dict.get("input")
            if isinstance(input_value, str) and input_value.strip():
                hint = (
                    f"Unsupported task_kind '{input_value.strip()}'. "
                    f"Use one of: {allowed_task_kinds}."
                )
            else:
                hint = f"Task has invalid task_kind. Use one of: {allowed_task_kinds}."
        else:
            msg = error_dict.get("msg")
            if isinstance(msg, str):
                trimmed = msg.strip()
                hint = trimmed or None

    return hint


def _normalize_sprint_validation_errors(
    validation_errors: object,
) -> list[str]:
    if not isinstance(validation_errors, list):
        return []

    hints: list[str] = []
    allowed_task_kinds = ", ".join(PUBLIC_TASK_KIND_VALUES)
    for error in validation_errors:
        hint = _normalize_sprint_validation_error(error, allowed_task_kinds)
        if hint and hint not in hints:
            hints.append(hint)
    return hints


def normalize_sprint_output_artifact(
    output_artifact: dict[str, Any] | None,
) -> dict[str, Any]:
    artifact = dict(output_artifact or {})
    if (
        "validation_errors" not in artifact
        and artifact.get("error") != "SPRINT_GENERATION_FAILED"
    ):
        return artifact
    artifact["validation_errors"] = _normalize_sprint_validation_errors(
        artifact.get("validation_errors")
    )
    return artifact


def normalize_sprint_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(attempt)
    output_artifact = normalized.get("output_artifact")
    if isinstance(output_artifact, dict):
        normalized["output_artifact"] = normalize_sprint_output_artifact(
            output_artifact
        )
    return normalized


def record_sprint_attempt(
    state: dict[str, Any],
    *,
    trigger: str,
    input_context: dict[str, Any],
    output_artifact: dict[str, Any] | None,
    is_complete: bool,
    failure_meta: dict[str, Any],
    created_at: str,
) -> int:
    normalized_output_artifact = normalize_sprint_output_artifact(output_artifact)
    return workflow_state.record_phase_attempt(
        state,
        attempts_key="sprint_attempts",
        last_input_context_key="sprint_last_input_context",
        assessment_key="sprint_plan_assessment",
        trigger=trigger,
        input_context=input_context,
        output_artifact=normalized_output_artifact,
        is_complete=is_complete,
        created_at=created_at,
        failure_source=dict(failure_meta),
    )


async def generate_sprint_plan(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    current_planned_sprint_id: int | None,
    now_iso: Callable[[], str],
    run_sprint_agent: Callable[..., Awaitable[dict[str, Any]]],
    failure_meta_builder: Callable[
        [dict[str, object] | None, str | None], dict[str, object]
    ],
    team_velocity_assumption: str,
    sprint_duration_days: int,
    max_story_points: int | None,
    include_task_decomposition: bool,
    selected_story_ids: list[int] | None,
    user_input: str | None,
    load_candidates: Callable[[], dict[str, Any]] | None = None,
    allow_completed_sprint_generation: bool = False,
) -> dict[str, Any]:
    state = await load_state()
    try:
        workflow_state.assert_downstream_backlog_not_stale(state)
    except workflow_state.DownstreamBacklogStaleError as exc:
        raise SprintPhaseError(str(exc)) from exc

    reset_stale_saved_sprint_planner_working_set(
        state,
        current_planned_sprint_id=current_planned_sprint_id,
    )

    current_state = state.get("fsm_state")
    valid_generation_states = set(VALID_SPRINT_GENERATION_STATES)
    if allow_completed_sprint_generation:
        valid_generation_states.add(OrchestratorState.SPRINT_COMPLETE.value)
    if current_state not in valid_generation_states:
        raise SprintPhaseError(
            f"Invalid phase for sprint generation (state: {current_state})"
        )

    candidates_payload = (
        load_candidates()
        if load_candidates is not None
        else _load_sprint_candidates_for_state(project_id, state)
    )
    readiness = candidates_payload.get("readiness")
    if isinstance(readiness, dict) and readiness.get("status") == "blocked":
        codes = ", ".join(str(code) for code in readiness.get("blocking_codes", []))
        raise SprintPhaseError(
            f"Sprint candidates are not planning-ready: {codes}",
            status_code=409,
        )

    attempts = ensure_sprint_attempts(state)
    has_attempts = len(attempts) > 0
    previous_assessment = state.get("sprint_plan_assessment")
    previous_complete_assessment = (
        dict(previous_assessment)
        if _is_complete_sprint_assessment(previous_assessment)
        else None
    )
    previous_input_context = state.get("sprint_last_input_context")

    sprint_result = await run_sprint_agent(
        state,
        project_id=project_id,
        team_velocity_assumption=team_velocity_assumption,
        sprint_duration_days=sprint_duration_days,
        max_story_points=max_story_points,
        include_task_decomposition=include_task_decomposition,
        selected_story_ids=selected_story_ids,
        user_input=user_input,
    )
    normalized_output_artifact = normalize_sprint_output_artifact(
        cast("dict[str, Any] | None", sprint_result.get("output_artifact"))
    )
    artifact_fingerprint = _sprint_artifact_fingerprint(normalized_output_artifact)
    failure_meta = dict(
        failure_meta_builder(
            cast("dict[str, object]", sprint_result),
            cast("str | None", sprint_result.get("error")),
        )
    )

    is_complete = bool(sprint_result.get("is_complete", False))
    attempt_count = record_sprint_attempt(
        state,
        trigger="manual_refine" if has_attempts else "auto_transition",
        input_context=cast("dict[str, Any]", sprint_result.get("input_context") or {}),
        output_artifact=cast(
            "dict[str, Any] | None", sprint_result.get("output_artifact")
        ),
        is_complete=is_complete,
        failure_meta=failure_meta,
        created_at=now_iso(),
    )
    attempt_id = f"sprint-attempt-{attempt_count}"
    _attach_attempt_guards(
        state,
        attempt_id=attempt_id,
        artifact_fingerprint=artifact_fingerprint,
    )
    _attach_attempt_source_fingerprint(
        state,
        source_fingerprint=sprint_result.get("source_fingerprint"),
    )

    preserve_reviewed_draft = (
        has_attempts
        and not bool(sprint_result.get("success"))
        and previous_complete_assessment is not None
    )
    if preserve_reviewed_draft:
        state["sprint_plan_assessment"] = previous_complete_assessment
        if isinstance(previous_input_context, dict):
            state["sprint_last_input_context"] = dict(previous_input_context)
        state["fsm_state"] = OrchestratorState.SPRINT_DRAFT.value
        next_state = OrchestratorState.SPRINT_DRAFT.value
    else:
        next_state = set_sprint_fsm_state(
            state,
            is_complete=is_complete,
            now_iso=now_iso,
        )
    save_state(state)

    return {
        "is_complete": is_complete,
        "sprint_run_success": bool(sprint_result.get("success")),
        "error": sprint_result.get("error"),
        "trigger": "manual_refine" if has_attempts else "auto_transition",
        "input_context": sprint_result.get("input_context"),
        "output_artifact": normalized_output_artifact,
        "attempt_count": attempt_count,
        "attempt_id": attempt_id,
        "artifact_fingerprint": artifact_fingerprint,
        **failure_meta,
        "fsm_state": next_state,
    }


async def get_sprint_history(
    *,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    current_planned_sprint_id: int | None,
) -> dict[str, Any]:
    state = await load_state()
    if reset_stale_saved_sprint_planner_working_set(
        state,
        current_planned_sprint_id=current_planned_sprint_id,
    ):
        save_state(state)

    attempts = ensure_sprint_attempts(state)
    normalized_attempts = [
        normalize_sprint_attempt(attempt)
        for attempt in attempts
        if isinstance(attempt, dict)
    ]
    restored_draft = _restore_latest_complete_sprint_draft(state, normalized_attempts)
    if normalized_attempts != attempts or restored_draft:
        state["sprint_attempts"] = normalized_attempts
        save_state(state)

    return {
        "items": normalized_attempts,
        "count": len(normalized_attempts),
    }


async def reset_sprint_planner(
    *,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    current_planned_sprint_id: int | None,
) -> dict[str, Any]:
    if current_planned_sprint_id is not None:
        raise SprintPhaseError(
            "A planned sprint already exists. Modify it instead of creating another."
        )

    state = await load_state()
    reset_sprint_planner_working_set(state)
    save_state(state)

    return {
        "items": [],
        "count": 0,
    }


def list_saved_sprints(
    *,
    load_sprints: Callable[[], Sequence[Any]],
    build_runtime_summary: Callable[[Sequence[Any]], dict[str, Any]],
    serialize_sprint_list_item: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    sprints = list(load_sprints())
    runtime_summary = build_runtime_summary(sprints)
    items = [serialize_sprint_list_item(sprint, runtime_summary) for sprint in sprints]
    return {
        "items": items,
        "count": len(items),
        "runtime_summary": runtime_summary,
    }


def append_sprint_execution_history(
    history: dict[str, Any],
    *,
    execution_items: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Return Sprint history with additive execution-history fields."""
    attempt_items = history.get("items")
    if not isinstance(attempt_items, list):
        attempt_items = []
    execution_rows = list(execution_items)
    return {
        **history,
        "attempt_items": attempt_items,
        "attempt_count": len(attempt_items),
        "execution_items": execution_rows,
        "execution_count": len(execution_rows),
    }


def get_saved_sprint_detail(
    *,
    load_sprint: Callable[[], Any | None],
    load_sprints: Callable[[], Sequence[Any]],
    build_runtime_summary: Callable[[Sequence[Any]], dict[str, Any]],
    serialize_sprint_detail: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    sprint = load_sprint()
    if not sprint:
        raise SprintPhaseError("Sprint not found", status_code=404)

    sprints = list(load_sprints())
    runtime_summary = build_runtime_summary(sprints)
    return {
        "sprint": serialize_sprint_detail(sprint, runtime_summary),
        "runtime_summary": runtime_summary,
    }


def get_sprint_close_readiness(
    *,
    sprint_id: int,
    load_sprint: Callable[[], Any | None],
    build_readiness: Callable[[Any], Any],
    history_fidelity: Callable[[Any], str],
    load_close_snapshot: Callable[[Any], dict[str, Any] | None],
) -> dict[str, Any]:
    sprint = load_sprint()
    if not sprint:
        raise SprintPhaseError("Sprint not found", status_code=404)

    readiness = build_readiness(sprint)
    sprint_status = _status_key(getattr(sprint, "status", None))
    open_story_count = int(getattr(readiness, "open_story_count", 0) or 0)
    close_eligible = sprint_status == "active" and open_story_count == 0
    if close_eligible:
        ineligible_reason = None
    elif sprint_status == "completed":
        ineligible_reason = "Sprint is already completed."
    elif sprint_status == "active":
        ineligible_reason = "Sprint has unfinished stories."
    else:
        ineligible_reason = "Only active sprints can be closed."

    return {
        "success": True,
        "sprint_id": sprint_id,
        "current_status": _status_value(getattr(sprint, "status", None)) or "",
        "completed_at": getattr(sprint, "completed_at", None),
        "readiness": readiness,
        "close_eligible": close_eligible,
        "ineligible_reason": ineligible_reason,
        "history_fidelity": history_fidelity(sprint),
        "close_snapshot": load_close_snapshot(sprint),
    }


def close_sprint(
    *,
    sprint_id: int,
    completion_notes: str,
    follow_up_notes: str | None,
    load_sprint: Callable[[], Any | None],
    build_readiness: Callable[[Any], Any],
    now_iso: Callable[[], str],
    persist_closed_sprint: Callable[[dict[str, Any]], Any | None],
) -> dict[str, Any]:
    sprint = load_sprint()
    if not sprint:
        raise SprintPhaseError("Sprint not found", status_code=404)

    if _status_key(getattr(sprint, "status", None)) != "active":
        raise SprintPhaseError(
            "Only active sprints can be closed.",
            status_code=409,
        )

    readiness = build_readiness(sprint)
    if int(getattr(readiness, "open_story_count", 0) or 0) > 0:
        raise SprintPhaseError(
            "Sprint has unfinished stories.",
            status_code=409,
        )
    snapshot = {
        "closed_at": now_iso(),
        "completion_notes": completion_notes,
        "follow_up_notes": follow_up_notes,
        "completed_story_count": readiness.completed_story_count,
        "open_story_count": readiness.open_story_count,
        "unfinished_story_ids": readiness.unfinished_story_ids,
        "stories": [
            _snapshot_story_payload(story)
            for story in getattr(readiness, "stories", [])
        ],
    }

    closed_sprint = persist_closed_sprint(snapshot)
    if not closed_sprint:
        raise SprintPhaseError("Sprint not found", status_code=404)

    return {
        "success": True,
        "sprint_id": sprint_id,
        "current_status": "Completed",
        "completed_at": getattr(closed_sprint, "completed_at", None),
        "readiness": readiness,
        "close_eligible": False,
        "ineligible_reason": None,
        "history_fidelity": "snapshotted",
        "close_snapshot": snapshot,
    }


async def save_sprint_plan(  # noqa: C901
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    current_planned_sprint_id: int | None,
    now_iso: Callable[[], str],
    hydrate_context: Callable[[str, int], Awaitable[Any]],
    build_tool_context: Callable[[Any], Any],
    save_plan_tool: Callable[[SaveSprintPlanInput, Any], dict[str, Any]],
    team_name: str,
    sprint_start_date: str,
    attempt_id: str | None = None,
    expected_artifact_fingerprint: str | None = None,
    expected_state: str | None = None,
    idempotency_key: str | None = None,
    load_candidates: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state = await load_state()
    if reset_stale_saved_sprint_planner_working_set(
        state,
        current_planned_sprint_id=current_planned_sprint_id,
    ):
        save_state(state)

    replay = _sprint_save_replay(state, idempotency_key)
    if replay is not None:
        return replay

    assessment = state.get("sprint_plan_assessment")
    if not isinstance(assessment, dict):
        raise SprintPhaseError("No sprint draft available to save")

    if _guarded_save_requested(
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
        expected_state=expected_state,
        idempotency_key=idempotency_key,
    ):
        _assert_save_expected_state(state, expected_state)
        _assert_save_guards(
            state=state,
            assessment=assessment,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
        )
    if not bool(assessment.get("is_complete", False)):
        raise SprintPhaseError("Sprint cannot be saved until is_complete is true")

    _assert_latest_complete_sprint_attempt(
        state=state,
        attempt_id=attempt_id,
        assessment=assessment,
    )
    _assert_sprint_source_current(
        project_id=project_id,
        assessment=assessment,
        state=state,
        load_candidates=load_candidates,
    )

    normalized_team_name = team_name.strip()
    normalized_start_date = sprint_start_date.strip()
    if not normalized_team_name:
        raise SprintPhaseError("team_name is required", status_code=422)
    if not normalized_start_date:
        raise SprintPhaseError("sprint_start_date is required", status_code=422)

    assessment_payload = dict(assessment)
    assessment_payload.pop("is_complete", None)
    assessment_payload.pop("attempt_id", None)
    assessment_payload.pop("artifact_fingerprint", None)
    assessment_payload.pop("source_fingerprint", None)

    try:
        sprint_data = SprintPlannerOutput.model_validate(assessment_payload)
    except Exception as exc:
        raise SprintPhaseError(
            f"Invalid sprint data in session: {exc!s}",
            status_code=500,
        ) from exc

    session_id = str(project_id)
    context = await hydrate_context(session_id, project_id)
    context.state["sprint_plan"] = sprint_data.model_dump(exclude_none=True)

    result = save_plan_tool(
        SaveSprintPlanInput(
            product_id=project_id,
            team_id=None,
            team_name=normalized_team_name,
            sprint_start_date=normalized_start_date,
            sprint_duration_days=sprint_data.duration_days,
        ),
        build_tool_context(context),
    )

    if not result.get("success"):
        status_code = (
            409 if result.get("error_code") == "STORY_ALREADY_IN_OPEN_SPRINT" else 500
        )
        raise SprintPhaseError(
            result.get("error", "Failed to save sprint plan"),
            status_code=status_code,
        )

    state["fsm_state"] = OrchestratorState.SPRINT_PERSISTENCE.value
    state["fsm_state_entered_at"] = now_iso()
    state["sprint_saved_at"] = now_iso()
    state["sprint_planner_owner_sprint_id"] = result.get("sprint_id")

    payload = {
        "fsm_state": OrchestratorState.SPRINT_PERSISTENCE.value,
        "save_result": result,
        "attempt_id": attempt_id,
        "artifact_fingerprint": expected_artifact_fingerprint,
        "idempotency_key": idempotency_key,
    }
    if idempotency_key is not None:
        _record_sprint_save_replay(state, idempotency_key, payload)
    save_state(state)
    return payload


def _sprint_artifact_fingerprint(output_artifact: dict[str, Any]) -> str:
    return canonical_hash({"phase": "sprint", "output_artifact": output_artifact})


def _is_complete_sprint_assessment(value: object) -> TypeGuard[dict[str, Any]]:
    if not isinstance(value, dict):
        return False
    assessment = cast("dict[str, Any]", value)
    return assessment.get("is_complete") is True


def _restore_latest_complete_sprint_draft(
    state: dict[str, Any],
    attempts: Sequence[dict[str, Any]],
) -> bool:
    if state.get("fsm_state") != OrchestratorState.SPRINT_SETUP.value:
        return False
    if _is_complete_sprint_assessment(state.get("sprint_plan_assessment")):
        return False

    for attempt in reversed(attempts):
        if attempt.get("is_complete") is not True:
            continue
        output_artifact = attempt.get("output_artifact")
        if not isinstance(output_artifact, dict):
            continue
        restored = dict(output_artifact)
        attempt_id = attempt.get("attempt_id")
        artifact_fingerprint = attempt.get("artifact_fingerprint")
        if isinstance(attempt_id, str) and attempt_id:
            restored["attempt_id"] = attempt_id
        if isinstance(artifact_fingerprint, str) and artifact_fingerprint:
            restored["artifact_fingerprint"] = artifact_fingerprint
        restored["is_complete"] = True
        state["sprint_plan_assessment"] = restored
        input_context = attempt.get("input_context")
        if isinstance(input_context, dict):
            state["sprint_last_input_context"] = dict(input_context)
        state["fsm_state"] = OrchestratorState.SPRINT_DRAFT.value
        return True
    return False


def _attach_attempt_guards(
    state: dict[str, Any],
    *,
    attempt_id: str,
    artifact_fingerprint: str,
) -> None:
    attempts = ensure_sprint_attempts(state)
    if attempts:
        attempts[-1]["attempt_id"] = attempt_id
        attempts[-1]["artifact_fingerprint"] = artifact_fingerprint
        output_artifact = attempts[-1].get("output_artifact")
        if isinstance(output_artifact, dict):
            output_artifact["attempt_id"] = attempt_id
            output_artifact["artifact_fingerprint"] = artifact_fingerprint

    assessment = state.get("sprint_plan_assessment")
    if isinstance(assessment, dict):
        assessment["attempt_id"] = attempt_id
        assessment["artifact_fingerprint"] = artifact_fingerprint


def _attach_attempt_source_fingerprint(
    state: dict[str, Any],
    *,
    source_fingerprint: object,
) -> None:
    if not isinstance(source_fingerprint, str) or not source_fingerprint.strip():
        return
    normalized_source_fingerprint = source_fingerprint.strip()
    state["sprint_candidate_source_fingerprint"] = normalized_source_fingerprint

    attempts = ensure_sprint_attempts(state)
    if attempts:
        attempts[-1]["source_fingerprint"] = normalized_source_fingerprint

    assessment = state.get("sprint_plan_assessment")
    if isinstance(assessment, dict):
        assessment["source_fingerprint"] = normalized_source_fingerprint


def _sprint_save_replay(
    state: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any] | None:
    if idempotency_key is None:
        return None
    saves = state.get("sprint_save_idempotency_keys")
    if not isinstance(saves, dict):
        return None
    payload = saves.get(idempotency_key)
    return dict(payload) if isinstance(payload, dict) else None


def _guarded_save_requested(
    *,
    attempt_id: str | None,
    expected_artifact_fingerprint: str | None,
    expected_state: str | None,
    idempotency_key: str | None,
) -> bool:
    return (
        attempt_id is not None
        or expected_artifact_fingerprint is not None
        or expected_state is not None
        or idempotency_key is not None
    )


def _record_sprint_save_replay(
    state: dict[str, Any],
    idempotency_key: str,
    payload: dict[str, Any],
) -> None:
    saves = state.get("sprint_save_idempotency_keys")
    if not isinstance(saves, dict):
        saves = {}
    saves[idempotency_key] = dict(payload)
    state["sprint_save_idempotency_keys"] = saves


def _sprint_start_replay(
    state: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    starts = state.get("sprint_start_idempotency_keys")
    if not isinstance(starts, dict):
        return None
    payload = starts.get(idempotency_key)
    return dict(payload) if isinstance(payload, dict) else None


def _record_sprint_start_replay(
    state: dict[str, Any],
    idempotency_key: str,
    payload: dict[str, Any],
) -> None:
    starts = state.get("sprint_start_idempotency_keys")
    if not isinstance(starts, dict):
        starts = {}
    starts[idempotency_key] = dict(payload)
    state["sprint_start_idempotency_keys"] = starts


def _normalize_fsm_state(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in VALID_FSM_STATES:
            return normalized
    return OrchestratorState.SETUP_REQUIRED.value


def _assert_sprint_start_expected_state(
    state: dict[str, Any],
    expected_state: str,
) -> None:
    if expected_state != OrchestratorState.SPRINT_PERSISTENCE.value:
        raise SprintPhaseError("Sprint start expected_state must be SPRINT_PERSISTENCE")
    fsm_state = _normalize_fsm_state(state.get("fsm_state"))
    if fsm_state != expected_state:
        raise SprintPhaseError(
            f"Sprint start stale state: expected {expected_state}, got {fsm_state}",
        )


def _assert_save_expected_state(
    state: dict[str, Any],
    expected_state: str | None,
) -> None:
    if expected_state != OrchestratorState.SPRINT_DRAFT.value:
        raise SprintPhaseError("Sprint save expected_state must be SPRINT_DRAFT")
    fsm_state = _normalize_fsm_state(state.get("fsm_state"))
    if fsm_state != expected_state:
        raise SprintPhaseError(
            f"Sprint save stale state: expected {expected_state}, got {fsm_state}",
        )


def _assert_save_guards(
    *,
    state: dict[str, Any],
    assessment: dict[str, Any],
    attempt_id: str | None,
    expected_artifact_fingerprint: str | None,
) -> None:
    selected_attempt = _find_sprint_attempt(state, attempt_id)
    if (
        not attempt_id
        or not expected_artifact_fingerprint
        or assessment.get("attempt_id") != attempt_id
        or assessment.get("artifact_fingerprint") != expected_artifact_fingerprint
        or selected_attempt is None
        or selected_attempt.get("artifact_fingerprint") != expected_artifact_fingerprint
    ):
        raise SprintPhaseError(
            "Sprint save guard mismatch: draft attempt or artifact fingerprint "
            "does not match the reviewed Sprint draft.",
        )


def _assert_latest_complete_sprint_attempt(
    *,
    state: dict[str, Any],
    attempt_id: str | None,
    assessment: dict[str, Any],
) -> None:
    attempts = ensure_sprint_attempts(state)
    latest_attempt: object = attempts[-1] if attempts else None
    expected_attempt_id = attempt_id or assessment.get("attempt_id")
    if (
        not isinstance(latest_attempt, dict)
        or latest_attempt.get("attempt_id") != expected_attempt_id
        or latest_attempt.get("artifact_fingerprint")
        != assessment.get("artifact_fingerprint")
        or latest_attempt.get("is_complete") is not True
    ):
        raise SprintPhaseError(
            "Sprint save requires the latest complete Sprint attempt.",
        )


def _assert_sprint_source_current(
    *,
    project_id: int,
    assessment: dict[str, Any],
    state: dict[str, Any],
    load_candidates: Callable[[], dict[str, Any]] | None,
) -> None:
    draft_source = assessment.get("source_fingerprint") or state.get(
        "sprint_candidate_source_fingerprint"
    )
    if not isinstance(draft_source, str):
        return

    candidates_payload = (
        load_candidates()
        if load_candidates is not None
        else _load_sprint_candidates_for_state(project_id, state)
    )
    current_source = candidates_payload.get("source_fingerprint")
    if not isinstance(current_source, str):
        raise SprintPhaseError(
            "Story/dependency source could not be verified; regenerate the "
            "Sprint draft before saving.",
        )
    if current_source != draft_source:
        raise SprintPhaseError(
            "Story/dependency source changed; regenerate the Sprint draft before "
            "saving.",
        )


def _find_sprint_attempt(
    state: dict[str, Any],
    attempt_id: str | None,
) -> dict[str, Any] | None:
    if not attempt_id:
        return None
    attempts = ensure_sprint_attempts(state)
    for attempt in attempts:
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def start_saved_sprint(
    *,
    project_id: int,
    sprint_id: int,
    load_sprint: Callable[[], Any | None],
    load_other_active: Callable[[], Any | None],
    persist_started_sprint: Callable[[], Any | None],
    build_runtime_summary: Callable[[], dict[str, Any]],
    serialize_sprint: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    sprint = load_sprint()
    if not sprint:
        raise SprintPhaseError("Sprint not found", status_code=404)

    other_active = load_other_active()
    if other_active:
        raise SprintPhaseError("Another sprint is already active for this project.")

    sprint_status = _status_key(getattr(sprint, "status", None))
    if sprint_status == "completed":
        raise SprintPhaseError(
            "Completed sprints cannot be restarted.",
        )

    if sprint_status == "active" and getattr(sprint, "started_at", None) is not None:
        runtime_summary = build_runtime_summary()
        return {
            "sprint": serialize_sprint(
                sprint,
                runtime_summary,
            )
        }

    started_sprint = persist_started_sprint()
    if not started_sprint:
        raise SprintPhaseError("Sprint not found", status_code=404)

    runtime_summary = build_runtime_summary()
    return {
        "sprint": serialize_sprint(
            started_sprint,
            runtime_summary,
        )
    }


def start_sprint_execution(
    *,
    project_id: int,
    sprint_id: int | None,
    expected_state: str,
    idempotency_key: str,
    load_state: Callable[[], dict[str, Any]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    resolve_sprint_id: Callable[[], int | None],
    load_sprint: Callable[[int], Any | None],
    load_other_active: Callable[[int], Any | None],
    persist_started_sprint: Callable[[int], Any | None],
    build_runtime_summary: Callable[[], dict[str, Any]],
    serialize_sprint: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Start the reviewed Sprint and synchronize workbench FSM state."""
    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise SprintPhaseError("Sprint start idempotency_key is required")

    state = load_state()
    replay = _sprint_start_replay(state, normalized_key)
    if replay is not None:
        return replay

    _assert_sprint_start_expected_state(state, expected_state)

    resolved_sprint_id = sprint_id or resolve_sprint_id()
    if resolved_sprint_id is None:
        raise SprintPhaseError("No planned Sprint is available to start.")

    data = start_saved_sprint(
        project_id=project_id,
        sprint_id=resolved_sprint_id,
        load_sprint=lambda: load_sprint(resolved_sprint_id),
        load_other_active=lambda: load_other_active(resolved_sprint_id),
        persist_started_sprint=lambda: persist_started_sprint(resolved_sprint_id),
        build_runtime_summary=build_runtime_summary,
        serialize_sprint=serialize_sprint,
    )
    sprint_payload = data.get("sprint")
    payload_sprint_id = (
        sprint_payload.get("id")
        if isinstance(sprint_payload, dict)
        and isinstance(sprint_payload.get("id"), int)
        else resolved_sprint_id
    )

    started_at = now_iso()
    state["fsm_state"] = OrchestratorState.SPRINT_VIEW.value
    state["fsm_state_entered_at"] = started_at
    state["active_sprint_id"] = payload_sprint_id
    state["sprint_started_at"] = started_at

    payload = {
        "project_id": project_id,
        "sprint_id": payload_sprint_id,
        "fsm_state": OrchestratorState.SPRINT_VIEW.value,
        "idempotency_key": normalized_key,
        **data,
    }
    _record_sprint_start_replay(state, normalized_key, payload)
    save_state(state)
    return payload
