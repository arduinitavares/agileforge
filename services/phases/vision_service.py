"""Vision phase application service helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from orchestrator_agent.agent_tools.product_vision_tool.tools import (
    SaveVisionInput,
)
from orchestrator_agent.fsm.states import OrchestratorState
from services.phases import workflow_state


class VisionPhaseError(Exception):
    """Domain-level vision phase error for router translation."""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def vision_state_from_complete(is_complete: bool) -> str:
    return workflow_state.phase_state_from_complete(
        is_complete,
        review_state=OrchestratorState.VISION_REVIEW.value,
        interview_state=OrchestratorState.VISION_INTERVIEW.value,
    )


def vision_fingerprint(statement: str) -> str:
    """Return a stable fingerprint for saved Vision text."""
    return f"sha256:{hashlib.sha256(statement.encode('utf-8')).hexdigest()}"


def ensure_vision_attempts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return workflow_state.ensure_phase_attempts(
        state,
        attempts_key="vision_attempts",
    )


def record_vision_attempt(
    state: dict[str, Any],
    *,
    trigger: str,
    input_context: dict[str, Any],
    output_artifact: dict[str, Any],
    is_complete: bool,
    failure_meta: dict[str, Any] | None = None,
    now_iso: Callable[[], str],
) -> int:
    return workflow_state.record_phase_attempt(
        state,
        attempts_key="vision_attempts",
        last_input_context_key="vision_last_input_context",
        assessment_key="product_vision_assessment",
        trigger=trigger,
        input_context=input_context,
        output_artifact=output_artifact,
        is_complete=is_complete,
        created_at=now_iso(),
        failure_source=failure_meta,
        failure_summary_fallback=(failure_meta or {}).get("error"),
        mirrored_output_field="updated_components",
        mirrored_state_key="vision_components",
        mirrored_output_types=(dict,),
    )


def set_vision_fsm_state(
    state: dict[str, Any],
    *,
    is_complete: bool,
    now_iso: Callable[[], str],
) -> str:
    return workflow_state.set_phase_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
        review_state=OrchestratorState.VISION_REVIEW.value,
        interview_state=OrchestratorState.VISION_INTERVIEW.value,
    )


async def generate_vision_draft(
    *,
    project_id: int,
    setup_blocker: str | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    run_vision_agent: Callable[..., Awaitable[dict[str, Any]]],
    user_input: str | None,
) -> dict[str, Any]:
    if setup_blocker:
        raise VisionPhaseError(f"Setup required: {setup_blocker}")

    state = await load_state()
    has_refinable_draft = isinstance(state.get("vision_components"), dict)
    normalized_user_input = (user_input or "").strip()
    if has_refinable_draft and not normalized_user_input:
        raise VisionPhaseError(
            "Feedback is required for Vision refinement attempts",
        )
    trigger = "manual_refine" if normalized_user_input else "initial_generate"

    vision_result = await run_vision_agent(
        state,
        project_id=project_id,
        user_input=normalized_user_input,
    )
    is_complete = (
        bool(vision_result.get("is_complete"))
        if vision_result.get("success")
        else False
    )

    attempt_count = record_vision_attempt(
        state,
        trigger=trigger,
        input_context=vision_result.get("input_context") or {},
        output_artifact=vision_result.get("output_artifact") or {},
        is_complete=is_complete,
        failure_meta=vision_result,
        now_iso=now_iso,
    )
    next_state = set_vision_fsm_state(
        state,
        is_complete=is_complete,
        now_iso=now_iso,
    )
    save_state(state)

    return {
        "fsm_state": next_state,
        "is_complete": is_complete,
        "vision_run_success": bool(vision_result.get("success")),
        "error": vision_result.get("error"),
        "trigger": trigger,
        "input_context": vision_result.get("input_context"),
        "output_artifact": vision_result.get("output_artifact"),
        "attempt_count": attempt_count,
        "model_info": vision_result.get("model_info"),
        **workflow_state.failure_meta(
            vision_result, fallback_summary=vision_result.get("error")
        ),
    }


async def get_vision_history(
    *,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    state = await load_state()
    attempts = ensure_vision_attempts(state)
    return {
        "items": attempts,
        "count": len(attempts),
    }


async def save_vision_draft(
    *,
    project_id: int,
    project_name: str,
    setup_blocker: str | None,
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    hydrate_context: Callable[[], Awaitable[Any]],
    build_tool_context: Callable[[Any], Any],
    save_vision_tool: Callable[[SaveVisionInput, Any], dict[str, Any]],
) -> dict[str, Any]:
    if setup_blocker:
        raise VisionPhaseError(f"Setup required: {setup_blocker}")

    context = await hydrate_context()
    assessment = context.state.get("product_vision_assessment")
    if not isinstance(assessment, dict):
        raise VisionPhaseError("No vision draft available to save")

    if not bool(assessment.get("is_complete", False)):
        raise VisionPhaseError("Vision cannot be saved until is_complete is true")

    statement = assessment.get("product_vision_statement")
    if not isinstance(statement, str) or not statement.strip():
        raise VisionPhaseError("Vision statement is empty")

    result = save_vision_tool(
        SaveVisionInput(
            product_id=project_id,
            project_name=project_name,
            product_vision_statement=statement,
        ),
        build_tool_context(context),
    )

    if not result.get("success"):
        raise VisionPhaseError(
            result.get("error", "Failed to save vision"),
            status_code=500,
        )

    context.state["fsm_state"] = OrchestratorState.VISION_PERSISTENCE.value
    context.state["fsm_state_entered_at"] = now_iso()
    context.state["vision_saved_at"] = now_iso()
    context.state["setup_status"] = "passed"
    save_state(context.state)

    return {
        "fsm_state": OrchestratorState.VISION_PERSISTENCE.value,
        "save_result": result,
        "saved_vision": statement,
        "vision_fingerprint": vision_fingerprint(statement),
    }
