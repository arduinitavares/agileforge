"""Backlog phase application service helpers."""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from orchestrator_agent.agent_tools.backlog_primer.schemes import (
    BacklogItem,
    OutputSchema,
)
from orchestrator_agent.agent_tools.backlog_primer.tools import SaveBacklogInput
from orchestrator_agent.fsm.states import OrchestratorState
from services.agent_workbench.fingerprints import canonical_hash
from services.backlog_runtime import (
    build_backlog_input_context,
    derive_brownfield_annotations,
)
from services.phases import workflow_state
from services.phases.backlog_refinement import (
    AmbiguousRefinementDiffError,
    AuthorityRefChangeOperation,
    BacklogRefinementError,
    BacklogRefinementOperationSet,
    apply_refinement_operations,
    assign_item_identity,
    canonical_operations_fingerprint,
    normalize_refined_artifact,
    operations_from_edited_artifact,
    project_savable_backlog_items,
)

if TYPE_CHECKING:
    from services.agent_workbench.backlog_refinement_events import (
        BacklogRefinementApprovalRequest,
    )

VALID_BACKLOG_GENERATION_STATES = {
    OrchestratorState.VISION_PERSISTENCE.value,
    OrchestratorState.BACKLOG_INTERVIEW.value,
    OrchestratorState.BACKLOG_REVIEW.value,
    OrchestratorState.BACKLOG_PERSISTENCE.value,
    OrchestratorState.ROADMAP_INTERVIEW.value,
}
VALID_FSM_STATES = {state.value for state in OrchestratorState}
VALID_BACKLOG_REFINEMENT_RECORD_STATES = {
    OrchestratorState.SPRINT_COMPLETE.value,
    OrchestratorState.BACKLOG_REVIEW.value,
}
REFINED_ATTEMPT_KINDS = {"refinement", "import_refinement"}
BACKLOG_ARTIFACT_FINGERPRINT_METADATA_KEYS = {
    "attempt_id",
    "artifact_fingerprint",
    "refinement_approved",
    "refinement_saveable",
    "refinement_approval",
}
BACKLOG_RUNTIME_DIAGNOSTIC_KEYS: tuple[str, ...] = ()
AUTO_SOURCE_ITEM_FINGERPRINT = "AUTO_SOURCE_ITEM_FINGERPRINT"
REFINE_RECORD_IDEMPOTENCY_REUSED_MESSAGE = (
    "Backlog refinement idempotency key reused with different request"
)
COMPILED_AUTHORITY_STATE_KEYS = ("compiled_authority_cached", "compiled_authority_json")
COMPILED_AUTHORITY_REF_COLLECTION_KEYS = (
    "items",
    "invariants",
    "source_map",
    "requirement_candidates",
    "authority_mappings",
)
COMPILED_AUTHORITY_REF_KEYS = (
    "id",
    "authority_ref",
    "authority_item_id",
    "candidate_id",
    "invariant_id",
    "source_item_id",
    "target_id",
)


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


async def preview_backlog_refinement(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    operations_payload: dict[str, Any],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """Apply canonical refinement operations without mutating phase state."""
    state = await load_state()
    prepared = _prepare_backlog_refinement(
        state=state,
        operations_payload=operations_payload,
    )
    _ = now_iso
    return _backlog_refinement_payload(
        project_id=project_id,
        state=state,
        prepared=prepared,
        trigger="refine_preview",
        attempt_id=None,
        attempt_count=None,
        persisted=False,
    )


async def record_backlog_refinement(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    operations_payload: dict[str, Any],
    expected_source_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """Record a canonical refined backlog attempt and move to review."""
    state = await load_state()
    prepared = _prepare_backlog_refinement(
        state=state,
        operations_payload=operations_payload,
    )
    request_fingerprint = _backlog_refine_record_request_fingerprint(
        project_id=project_id,
        state=state,
        prepared=prepared,
        expected_source_fingerprint=expected_source_fingerprint,
        expected_state=expected_state,
    )
    replay = _backlog_refine_record_replay(
        state,
        idempotency_key,
        request_fingerprint,
    )
    if replay is not None:
        return replay

    _assert_refinement_source_fingerprint(
        expected_source_fingerprint=expected_source_fingerprint,
        source_artifact_fingerprint=cast(
            "str",
            prepared["source_artifact_fingerprint"],
        ),
    )
    _assert_refinement_expected_state(state, expected_state)

    attempt_count = record_backlog_attempt(
        state,
        trigger="refine_record",
        input_context={
            "source_attempt_id": prepared["source_attempt_id"],
            "source_artifact_fingerprint": prepared["source_artifact_fingerprint"],
            "operation_set_fingerprint": prepared["operation_set_fingerprint"],
            "operation_set": prepared["operation_set_payload"],
            "idempotency_key": idempotency_key,
        },
        output_artifact=prepared["output_artifact"],
        is_complete=bool(prepared["output_artifact"].get("is_complete")),
        created_at=now_iso(),
    )
    attempt_id = f"backlog-attempt-{attempt_count}"
    artifact_fingerprint = cast("str", prepared["artifact_fingerprint"])
    _attach_attempt_guards(
        state,
        attempt_id=attempt_id,
        artifact_fingerprint=artifact_fingerprint,
    )
    recorded_attempt = ensure_backlog_attempts(state)[-1]
    recorded_attempt.update(
        {
            "attempt_kind": "refinement",
            "refinement_saveable": False,
            "source_attempt_id": prepared["source_attempt_id"],
            "source_artifact_fingerprint": prepared["source_artifact_fingerprint"],
            "operation_set_fingerprint": prepared["operation_set_fingerprint"],
            "operation_set": copy.deepcopy(prepared["operation_set_payload"]),
        }
    )
    assessment = state.get("product_backlog_assessment")
    if isinstance(assessment, dict):
        assessment["refinement_saveable"] = False

    state["fsm_state"] = OrchestratorState.BACKLOG_REVIEW.value
    state["fsm_state_entered_at"] = now_iso()
    if _normalize_fsm_state(expected_state) == OrchestratorState.SPRINT_COMPLETE.value:
        state["backlog_review_origin"] = "next_cycle_refinement"
        state["downstream_backlog_stale"] = True
        state["stale_backlog_reason"] = "refined_backlog_recorded"
        state["stale_since_backlog_attempt_id"] = attempt_id

    payload = _backlog_refinement_payload(
        project_id=project_id,
        state=state,
        prepared=prepared,
        trigger="refine_record",
        attempt_id=attempt_id,
        attempt_count=attempt_count,
        persisted=False,
    )
    payload["idempotency_key"] = idempotency_key
    payload["request_fingerprint"] = request_fingerprint
    _record_backlog_refine_record_replay(
        state,
        idempotency_key,
        request_fingerprint,
        payload,
    )
    save_state(state)
    return payload


async def import_backlog_refinement(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    source_artifact: dict[str, Any],
    edited_artifact: dict[str, Any],
    expected_source_fingerprint: str,
    idempotency_key: str,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """Record a source artifact and refined attempt from deterministic edits."""
    source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
    _assert_refinement_source_fingerprint(
        expected_source_fingerprint=expected_source_fingerprint,
        source_artifact_fingerprint=source_fingerprint,
    )
    state = await load_state()
    source_attempt = _find_refine_import_source_attempt(state, source_fingerprint)
    authority_fingerprint = _refinement_authority_fingerprint(state)
    as_built_cache_fingerprint = _as_built_cache_fingerprint(state)
    expected_state = _refine_import_expected_state(state, source_attempt)
    request_fingerprint = _backlog_refine_import_request_fingerprint(
        project_id=project_id,
        source_artifact_fingerprint=source_fingerprint,
        edited_artifact=edited_artifact,
        expected_source_fingerprint=expected_source_fingerprint,
        expected_state=expected_state,
        authority_fingerprint=authority_fingerprint,
        as_built_cache_fingerprint=as_built_cache_fingerprint,
    )
    replay = _backlog_refine_import_replay(
        state,
        idempotency_key,
        request_fingerprint,
    )
    if replay is not None:
        return replay

    draft_state = copy.deepcopy(state)
    source_attempt = _find_refine_import_source_attempt(
        draft_state,
        source_fingerprint,
    )
    source_attempt_id = (
        str(source_attempt["attempt_id"])
        if source_attempt is not None
        else f"backlog-attempt-{_next_backlog_attempt_count(draft_state)}"
    )
    canonical_source = assign_item_identity(
        copy.deepcopy(source_artifact),
        source_attempt_id=source_attempt_id,
        source_artifact_fingerprint=source_fingerprint,
    )
    try:
        operation_set = operations_from_edited_artifact(
            canonical_source,
            cast("dict[str, object]", edited_artifact),
            authority_fingerprint=authority_fingerprint,
            as_built_cache_fingerprint=as_built_cache_fingerprint,
        )
    except AmbiguousRefinementDiffError as exc:
        raise BacklogPhaseError(f"Backlog refinement import ambiguous: {exc}") from exc

    if source_attempt is None:
        attempt_count = record_backlog_attempt(
            draft_state,
            trigger="refine_import_source",
            input_context={
                "idempotency_key": idempotency_key,
                "expected_state": expected_state,
                "source_artifact_fingerprint": source_fingerprint,
            },
            output_artifact=copy.deepcopy(canonical_source),
            is_complete=bool(canonical_source.get("is_complete")),
            created_at=now_iso(),
        )
        source_attempt_id = f"backlog-attempt-{attempt_count}"
        _attach_attempt_guards(
            draft_state,
            attempt_id=source_attempt_id,
            artifact_fingerprint=source_fingerprint,
        )
        source_attempt = ensure_backlog_attempts(draft_state)[-1]
        source_attempt["attempt_kind"] = "imported_preview_source"
    else:
        source_output_artifact = source_attempt.get("output_artifact")
        if isinstance(source_output_artifact, dict):
            canonical_source = copy.deepcopy(source_output_artifact)

    payload = await record_backlog_refinement(
        project_id=project_id,
        load_state=lambda: _loaded_state(draft_state),
        save_state=lambda _state: None,
        operations_payload=operation_set.model_dump(mode="json"),
        expected_source_fingerprint=source_fingerprint,
        expected_state=expected_state,
        idempotency_key=idempotency_key,
        now_iso=now_iso,
    )
    refined_attempt = _find_backlog_attempt(draft_state, str(payload.get("attempt_id")))
    if isinstance(refined_attempt, dict):
        refined_attempt["trigger"] = "refine_import"
        refined_attempt["attempt_kind"] = "import_refinement"
    payload["trigger"] = "refine-import"
    payload["attempt_kind"] = "import_refinement"
    payload["request_fingerprint"] = request_fingerprint
    _record_backlog_refine_import_replay(
        draft_state,
        idempotency_key,
        request_fingerprint,
        payload,
    )
    save_state(draft_state)
    return payload


async def _loaded_state(state: dict[str, Any]) -> dict[str, Any]:
    return state


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
    _assert_refined_attempt_saveable(
        state=context.state,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    _assert_brownfield_save_gate(assessment)

    if not bool(assessment.get("is_complete", False)):
        raise BacklogPhaseError("Backlog cannot be saved until is_complete is true")
    if _has_clarifying_questions(assessment):
        raise BacklogPhaseError("Backlog cannot be saved while questions remain")

    items = project_savable_backlog_items(assessment)
    if len(items) == 0:
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


def _assert_refinement_expected_state(
    state: dict[str, Any],
    expected_state: str,
) -> None:
    normalized_expected_state = _normalize_fsm_state(expected_state)
    fsm_state = _normalize_fsm_state(cast("str | None", state.get("fsm_state")))
    if normalized_expected_state not in VALID_BACKLOG_REFINEMENT_RECORD_STATES:
        raise BacklogPhaseError("Backlog refinement expected_state is invalid")
    if fsm_state != normalized_expected_state:
        raise BacklogPhaseError(
            "Backlog refinement stale state: "
            f"expected {normalized_expected_state}, got {fsm_state}",
        )


def _backlog_refine_record_request_fingerprint(
    *,
    project_id: int,
    state: dict[str, Any],
    prepared: dict[str, Any],
    expected_source_fingerprint: str,
    expected_state: str,
) -> str:
    as_built_meta = state.get("as_built_assessment_cache_meta")
    as_built_cache_fingerprint = (
        as_built_meta.get("assessment_fingerprint")
        if isinstance(as_built_meta, dict)
        else None
    )
    return canonical_hash(
        {
            "command": "agileforge.backlog.refine_record",
            "project_id": project_id,
            "source_attempt_id": prepared["source_attempt_id"],
            "source_artifact_fingerprint": prepared["source_artifact_fingerprint"],
            "operation_set": prepared["operation_set_payload"],
            "operation_set_fingerprint": prepared["operation_set_fingerprint"],
            "expected_source_fingerprint": expected_source_fingerprint,
            "expected_state": _normalize_fsm_state(expected_state),
            "authority_fingerprint": _refinement_authority_fingerprint(state),
            "as_built_cache_fingerprint": as_built_cache_fingerprint,
        }
    )


def _backlog_refine_import_request_fingerprint(
    *,
    project_id: int,
    source_artifact_fingerprint: str,
    edited_artifact: dict[str, Any],
    expected_source_fingerprint: str,
    expected_state: str,
    authority_fingerprint: str,
    as_built_cache_fingerprint: str,
) -> str:
    return canonical_hash(
        {
            "command": "agileforge.backlog.refine_import",
            "project_id": project_id,
            "source_artifact_fingerprint": source_artifact_fingerprint,
            "edited_artifact_fingerprint": canonical_hash(
                {
                    "phase": "backlog",
                    "edited_artifact": edited_artifact,
                }
            ),
            "expected_source_fingerprint": expected_source_fingerprint,
            "expected_state": _normalize_fsm_state(expected_state),
            "authority_fingerprint": authority_fingerprint,
            "as_built_cache_fingerprint": as_built_cache_fingerprint,
        }
    )


def _refine_import_expected_state(
    state: dict[str, Any],
    source_attempt: dict[str, Any] | None,
) -> str:
    if source_attempt is None:
        return _normalize_fsm_state(cast("str | None", state.get("fsm_state")))
    input_context = source_attempt.get("input_context")
    if isinstance(input_context, dict) and isinstance(
        input_context.get("expected_state"),
        str,
    ):
        return str(input_context["expected_state"])
    return _normalize_fsm_state(cast("str | None", state.get("fsm_state")))


def _prepare_backlog_refinement(
    *,
    state: dict[str, Any],
    operations_payload: dict[str, Any],
    expected_source_fingerprint: str | None = None,
) -> dict[str, Any]:
    source_attempt_id = str(operations_payload.get("source_attempt_id") or "")
    source_attempt = _find_backlog_attempt(state, source_attempt_id)
    if source_attempt is None:
        raise BacklogPhaseError("Backlog refinement source attempt not found")

    source_artifact = source_attempt.get("output_artifact")
    if not isinstance(source_artifact, dict):
        raise BacklogPhaseError("Backlog refinement source artifact is missing")

    source_artifact_fingerprint = str(
        operations_payload.get("source_artifact_fingerprint") or ""
    )
    attempt_fingerprint = str(source_attempt.get("artifact_fingerprint") or "")
    if expected_source_fingerprint is not None:
        _assert_refinement_source_fingerprint(
            expected_source_fingerprint=expected_source_fingerprint,
            source_artifact_fingerprint=source_artifact_fingerprint,
        )
    _assert_refinement_source_fingerprint(
        expected_source_fingerprint=source_artifact_fingerprint,
        source_artifact_fingerprint=attempt_fingerprint,
    )
    if source_attempt.get("attempt_kind") != "imported_preview_source":
        recomputed_source_fingerprint = _backlog_artifact_fingerprint(source_artifact)
        _assert_refinement_source_fingerprint(
            expected_source_fingerprint=source_artifact_fingerprint,
            source_artifact_fingerprint=recomputed_source_fingerprint,
        )

    source_with_identity = assign_item_identity(
        cast("dict[str, object]", source_artifact),
        source_attempt_id=source_attempt_id,
        source_artifact_fingerprint=source_artifact_fingerprint,
    )
    resolved_payload = _resolve_auto_source_item_fingerprints(
        operations_payload,
        source_with_identity,
    )
    operation_set = _validate_refinement_operation_set(
        resolved_payload,
        state=state,
    )
    supported_authority_refs = _supported_refinement_authority_refs(
        state,
        operation_set,
    )
    operation_set_fingerprint = canonical_operations_fingerprint(operation_set)
    try:
        refined_artifact = normalize_refined_artifact(
            apply_refinement_operations(
                source_with_identity,
                operation_set,
                supported_authority_refs=supported_authority_refs,
            )
        )
        refined_artifact = _derive_refined_brownfield_metadata(
            refined_artifact,
            state=state,
        )
        refined_artifact = normalize_refined_artifact(refined_artifact)
    except BacklogRefinementError as exc:
        raise BacklogPhaseError(f"Backlog refinement failed: {exc}") from exc

    artifact_fingerprint = _backlog_artifact_fingerprint(refined_artifact)
    return {
        "source_attempt_id": source_attempt_id,
        "source_artifact_fingerprint": source_artifact_fingerprint,
        "operation_set": operation_set,
        "operation_set_payload": operation_set.model_dump(mode="json"),
        "operation_set_fingerprint": operation_set_fingerprint,
        "output_artifact": refined_artifact,
        "artifact_fingerprint": artifact_fingerprint,
    }


def _strip_inbound_brownfield_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    sanitized = copy.deepcopy(artifact)
    sanitized.pop("brownfield_warnings", None)
    raw_items = sanitized.get("backlog_items")
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            raw_item.pop("as_built_annotation", None)
            raw_item.pop("brownfield_warnings", None)
            raw_item.pop("capability_name", None)
            raw_item.pop("as_built_status", None)
            raw_item.pop("recommended_backlog_treatment", None)
    return sanitized


def _output_model_for_brownfield_annotation(
    artifact: dict[str, Any],
) -> tuple[OutputSchema, dict[int, int]]:
    raw_items = artifact.get("backlog_items")
    if not isinstance(raw_items, list):
        raw_items = []
    projected_items: list[dict[str, Any]] = []
    original_index_by_projected_index: dict[int, int] = {}
    for original_index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, Any]", raw_item)
        if item.get("classification") == "authority_gap_intake":
            continue
        item_payload = {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key in BacklogItem.model_fields
        }
        projected_items.append(BacklogItem.model_validate(item_payload).model_dump())
        original_index_by_projected_index[len(projected_items) - 1] = original_index
    output_model = OutputSchema.model_validate(
        {
            "backlog_items": projected_items,
            "is_complete": bool(artifact.get("is_complete")),
            "clarifying_questions": artifact.get("clarifying_questions") or [],
        }
    )
    return output_model, original_index_by_projected_index


def _derive_refined_brownfield_metadata(
    artifact: dict[str, Any],
    *,
    state: dict[str, Any],
) -> dict[str, Any]:
    refined = _strip_inbound_brownfield_metadata(artifact)
    try:
        output_model, original_index_by_projected_index = (
            _output_model_for_brownfield_annotation(refined)
        )
        annotation_result = derive_brownfield_annotations(
            output_model=output_model,
            input_context=build_backlog_input_context(state, user_input=None),
        )
    except (TypeError, ValidationError, ValueError) as exc:
        raise BacklogPhaseError(
            f"Backlog refinement brownfield annotation failed: {exc}",
        ) from exc

    raw_items = refined.get("backlog_items")
    if isinstance(raw_items, list):
        for projected_index, annotation in (
            annotation_result.annotations_by_index.items()
        ):
            original_index = original_index_by_projected_index.get(projected_index)
            if original_index is None:
                continue
            try:
                item = raw_items[original_index]
            except IndexError:
                continue
            if isinstance(item, dict):
                item["as_built_annotation"] = annotation.model_dump(
                    exclude_none=False
                )
    refined["brownfield_warnings"] = [
        warning.model_dump(exclude_none=False)
        for warning in annotation_result.warnings
    ]
    return refined


def _assert_refinement_source_fingerprint(
    *,
    expected_source_fingerprint: str,
    source_artifact_fingerprint: str,
) -> None:
    if expected_source_fingerprint != source_artifact_fingerprint:
        raise BacklogPhaseError(
            "Backlog refinement source artifact fingerprint mismatch",
        )


def _item_fingerprints_by_id(source_artifact: dict[str, object]) -> dict[str, str]:
    items = source_artifact.get("backlog_items")
    if not isinstance(items, list):
        return {}
    item_fingerprints: dict[str, str] = {}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, Any]", raw_item)
        item_id = item.get("item_id")
        item_fingerprint = item.get("item_fingerprint")
        if item_id and item_fingerprint:
            item_fingerprints[str(item_id)] = str(item_fingerprint)
    return item_fingerprints


def _resolve_auto_source_item_fingerprints(
    operations_payload: dict[str, Any],
    source_artifact: dict[str, object],
) -> dict[str, Any]:
    resolved_payload = copy.deepcopy(operations_payload)
    item_fingerprints = _item_fingerprints_by_id(source_artifact)
    operations = resolved_payload.get("operations")
    if not isinstance(operations, list):
        return resolved_payload
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        source_item_ids = operation.get("source_item_ids")
        source_item_fingerprints = operation.get("source_item_fingerprints")
        if not isinstance(source_item_ids, list) or not isinstance(
            source_item_fingerprints,
            list,
        ):
            continue
        resolved_fingerprints: list[Any] = []
        for index, fingerprint in enumerate(source_item_fingerprints):
            if fingerprint != AUTO_SOURCE_ITEM_FINGERPRINT:
                resolved_fingerprints.append(fingerprint)
                continue
            if index >= len(source_item_ids):
                raise BacklogPhaseError(
                    "Backlog refinement cannot resolve AUTO_SOURCE_ITEM_FINGERPRINT",
                )
            source_item_id = str(source_item_ids[index])
            resolved_fingerprint = item_fingerprints.get(source_item_id)
            if resolved_fingerprint is None:
                raise BacklogPhaseError(
                    "Backlog refinement source item fingerprint not found",
                )
            resolved_fingerprints.append(resolved_fingerprint)
        operation["source_item_fingerprints"] = resolved_fingerprints
    return resolved_payload


def _validate_refinement_operation_set(
    operations_payload: dict[str, Any],
    *,
    state: dict[str, Any],
) -> BacklogRefinementOperationSet:
    try:
        operation_set = BacklogRefinementOperationSet.model_validate(operations_payload)
    except ValueError as exc:
        raise BacklogPhaseError(
            f"Backlog refinement operation set invalid: {exc}"
        ) from exc

    authority_fingerprint = _refinement_authority_fingerprint(state)
    if (
        authority_fingerprint
        and operation_set.authority_fingerprint != authority_fingerprint
    ):
        raise BacklogPhaseError("Backlog refinement authority fingerprint mismatch")

    as_built_meta = state.get("as_built_assessment_cache_meta")
    if isinstance(as_built_meta, dict):
        as_built_fingerprint = as_built_meta.get("assessment_fingerprint")
        if (
            isinstance(as_built_fingerprint, str)
            and operation_set.as_built_cache_fingerprint != as_built_fingerprint
        ):
            raise BacklogPhaseError(
                "Backlog refinement as-built cache fingerprint mismatch"
            )
    return operation_set


def _supported_refinement_authority_refs(
    state: dict[str, Any],
    operation_set: BacklogRefinementOperationSet,
) -> set[str] | None:
    if not _operation_set_requires_authority_refs(operation_set):
        return None

    refs: set[str] = set()
    for state_key in COMPILED_AUTHORITY_STATE_KEYS:
        _collect_compiled_authority_refs(state.get(state_key), refs)

    active_project = state.get("active_project")
    if isinstance(active_project, dict):
        for state_key in COMPILED_AUTHORITY_STATE_KEYS:
            _collect_compiled_authority_refs(active_project.get(state_key), refs)

    _collect_as_built_authority_refs(state.get("as_built_assessment_cached"), refs)
    if not refs:
        raise BacklogPhaseError(
            "Backlog refinement authority refs unavailable for authority_ref_change",
        )
    return refs


def _operation_set_requires_authority_refs(
    operation_set: BacklogRefinementOperationSet,
) -> bool:
    return any(
        isinstance(operation, AuthorityRefChangeOperation)
        and operation.new_authority_ref is not None
        for operation in operation_set.operations
    )


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    return None


def _add_authority_ref(refs: set[str], value: object) -> None:
    if isinstance(value, str) and value.strip():
        refs.add(value.strip())


def _collect_authority_ref_keys(value: object, refs: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if key in COMPILED_AUTHORITY_REF_KEYS:
                _add_authority_ref(refs, nested_value)
            _collect_authority_ref_keys(nested_value, refs)
        return
    if isinstance(value, list):
        for item in value:
            _collect_authority_ref_keys(item, refs)


def _collect_compiled_authority_refs(value: object, refs: set[str]) -> None:
    authority = _json_object(value)
    if authority is None:
        return
    for collection_key in COMPILED_AUTHORITY_REF_COLLECTION_KEYS:
        _collect_authority_ref_keys(authority.get(collection_key), refs)


def _collect_as_built_authority_refs(value: object, refs: set[str]) -> None:
    assessment = _json_object(value)
    if assessment is None:
        return
    raw_capabilities = assessment.get("capability_assessments")
    if not isinstance(raw_capabilities, list):
        return
    for raw_capability in raw_capabilities:
        if not isinstance(raw_capability, dict):
            continue
        _add_authority_ref(refs, raw_capability.get("authority_ref"))
        invariant_refs = raw_capability.get("invariant_refs")
        if not isinstance(invariant_refs, list):
            continue
        for invariant_ref in invariant_refs:
            _add_authority_ref(refs, invariant_ref)


def _as_built_cache_fingerprint(state: dict[str, Any]) -> str:
    as_built_meta = state.get("as_built_assessment_cache_meta")
    if isinstance(as_built_meta, dict):
        assessment_fingerprint = as_built_meta.get("assessment_fingerprint")
        if isinstance(assessment_fingerprint, str):
            return assessment_fingerprint
    return ""


def _refinement_authority_fingerprint(state: dict[str, Any]) -> str:
    authority_fingerprint = state.get("compiled_authority_fingerprint")
    if isinstance(authority_fingerprint, str) and authority_fingerprint.strip():
        return authority_fingerprint.strip()

    as_built_meta = state.get("as_built_assessment_cache_meta")
    if isinstance(as_built_meta, dict):
        as_built_authority_fingerprint = as_built_meta.get("authority_fingerprint")
        if (
            isinstance(as_built_authority_fingerprint, str)
            and as_built_authority_fingerprint.strip()
        ):
            return as_built_authority_fingerprint.strip()
    return ""


def _backlog_refinement_payload(
    *,
    project_id: int,
    state: dict[str, Any],
    prepared: dict[str, Any],
    trigger: str,
    attempt_id: str | None,
    attempt_count: int | None,
    persisted: bool,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "fsm_state": _normalize_fsm_state(cast("str | None", state.get("fsm_state"))),
        "trigger": trigger,
        "attempt_id": attempt_id,
        "attempt_count": attempt_count,
        "persisted": persisted,
        "source_attempt_id": prepared["source_attempt_id"],
        "source_artifact_fingerprint": prepared["source_artifact_fingerprint"],
        "operation_set_fingerprint": prepared["operation_set_fingerprint"],
        "output_artifact": prepared["output_artifact"],
        "artifact_fingerprint": prepared["artifact_fingerprint"],
        "is_complete": bool(prepared["output_artifact"].get("is_complete")),
    }


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
    for metadata_key in BACKLOG_ARTIFACT_FINGERPRINT_METADATA_KEYS:
        normalized_artifact.pop(metadata_key, None)
    return canonical_hash({"phase": "backlog", "output_artifact": normalized_artifact})


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


def _backlog_refine_record_replay(
    state: dict[str, Any],
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    records = state.get("backlog_refine_record_idempotency_keys")
    if not isinstance(records, dict):
        return None
    record = records.get(idempotency_key)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise BacklogPhaseError(
            "Backlog refinement idempotency key replay is invalid",
        )
    if record.get("request_fingerprint") != request_fingerprint:
        raise BacklogPhaseError(REFINE_RECORD_IDEMPOTENCY_REUSED_MESSAGE)
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise BacklogPhaseError(
            "Backlog refinement idempotency key replay payload is invalid",
        )
    return copy.deepcopy(payload)


def _backlog_refine_import_replay(
    state: dict[str, Any],
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    records = state.get("backlog_refine_import_idempotency_keys")
    if not isinstance(records, dict):
        return None
    record = records.get(idempotency_key)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise BacklogPhaseError(
            "Backlog refinement import idempotency key replay is invalid",
        )
    if record.get("request_fingerprint") != request_fingerprint:
        raise BacklogPhaseError(REFINE_RECORD_IDEMPOTENCY_REUSED_MESSAGE)
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise BacklogPhaseError(
            "Backlog refinement import idempotency key replay payload is invalid",
        )
    return copy.deepcopy(payload)


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


def _record_backlog_refine_record_replay(
    state: dict[str, Any],
    idempotency_key: str,
    request_fingerprint: str,
    payload: dict[str, Any],
) -> None:
    records = state.get("backlog_refine_record_idempotency_keys")
    if not isinstance(records, dict):
        records = {}
    records[idempotency_key] = {
        "request_fingerprint": request_fingerprint,
        "payload": copy.deepcopy(payload),
    }
    state["backlog_refine_record_idempotency_keys"] = records


def _record_backlog_refine_import_replay(
    state: dict[str, Any],
    idempotency_key: str,
    request_fingerprint: str,
    payload: dict[str, Any],
) -> None:
    records = state.get("backlog_refine_import_idempotency_keys")
    if not isinstance(records, dict):
        records = {}
    records[idempotency_key] = {
        "request_fingerprint": request_fingerprint,
        "payload": copy.deepcopy(payload),
    }
    state["backlog_refine_import_idempotency_keys"] = records


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
        or _backlog_artifact_fingerprint(assessment) != expected_artifact_fingerprint
        or _backlog_artifact_fingerprint(selected_artifact)
        != expected_artifact_fingerprint
    ):
        raise BacklogPhaseError(
            "Backlog save guard mismatch: draft attempt or artifact fingerprint "
            "does not match the reviewed Backlog draft.",
        )


def _assert_refined_attempt_saveable(
    *,
    state: dict[str, Any],
    attempt_id: str,
    expected_artifact_fingerprint: str,
) -> None:
    selected_attempt = _find_backlog_attempt(state, attempt_id)
    if (
        selected_attempt is None
        or selected_attempt.get("attempt_kind") not in REFINED_ATTEMPT_KINDS
    ):
        return
    approval = selected_attempt.get("refinement_approval")
    if selected_attempt.get("refinement_saveable") is not True or not isinstance(
        approval,
        dict,
    ):
        raise BacklogPhaseError(
            "APPROVAL_REQUIRED: refined backlog attempt requires host-recorded "
            "PO approval before save.",
        )
    if (
        approval.get("approved_artifact_fingerprint")
        != expected_artifact_fingerprint
        or approval.get("approval_id") is None
    ):
        raise BacklogPhaseError(
            "APPROVAL_FINGERPRINT_MISMATCH: refined backlog approval does not "
            "match the reviewed artifact fingerprint.",
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


def mark_backlog_refinement_approved(
    state: dict[str, Any],
    *,
    request: BacklogRefinementApprovalRequest,
    approval: dict[str, Any],
) -> dict[str, Any]:
    """Mark a recorded refined attempt as saveable after host approval."""
    if request.attempt_id is None:
        return {"marked_saveable": False}
    selected_attempt = _find_backlog_attempt(state, request.attempt_id)
    if selected_attempt is None:
        raise BacklogPhaseError("Backlog refinement approval attempt not found")
    if selected_attempt.get("attempt_kind") not in REFINED_ATTEMPT_KINDS:
        raise BacklogPhaseError("Backlog refinement approval target is not refined")
    if selected_attempt.get("artifact_fingerprint") != (
        request.approved_artifact_fingerprint
    ):
        raise BacklogPhaseError("APPROVAL_FINGERPRINT_MISMATCH")
    if (
        request.operation_set_fingerprint is not None
        and selected_attempt.get("operation_set_fingerprint")
        != request.operation_set_fingerprint
    ):
        raise BacklogPhaseError("Backlog refinement approval operation mismatch")

    approval_id = approval.get("approval_id")
    request_fingerprint = approval.get("request_fingerprint")
    if not isinstance(approval_id, str) or not isinstance(request_fingerprint, str):
        raise BacklogPhaseError("Backlog refinement approval response is invalid")
    approval_binding: dict[str, Any] = {
        "approval_id": approval_id,
        "request_fingerprint": request_fingerprint,
        "approved_artifact_fingerprint": request.approved_artifact_fingerprint,
        "approved_operation_ids": list(request.approved_operation_ids),
        "approved_by": request.approved_by,
        "approval_source": request.approval_source,
    }
    selected_artifact = selected_attempt.get("output_artifact")
    is_saveable = (
        isinstance(selected_artifact, dict)
        and selected_artifact.get("is_complete") is True
        and not _has_clarifying_questions(selected_artifact)
    )
    selected_attempt["refinement_approved"] = True
    selected_attempt["refinement_saveable"] = is_saveable
    selected_attempt["refinement_approval"] = approval_binding

    assessment = state.get("product_backlog_assessment")
    if (
        isinstance(assessment, dict)
        and assessment.get("attempt_id") == request.attempt_id
        and assessment.get("artifact_fingerprint")
        == request.approved_artifact_fingerprint
    ):
        assessment["refinement_approved"] = True
        assessment["refinement_saveable"] = is_saveable
        assessment["refinement_approval"] = copy.deepcopy(approval_binding)
    return {
        "marked_saveable": is_saveable,
        "attempt_id": request.attempt_id,
        "approval_id": approval_id,
    }


def _find_backlog_attempt(
    state: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    attempts = ensure_backlog_attempts(state)
    for attempt in attempts:
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def _find_refine_import_source_attempt(
    state: dict[str, Any],
    source_artifact_fingerprint: str,
) -> dict[str, Any] | None:
    attempts = state.get("backlog_attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        if (
            attempt.get("attempt_kind")
            in {"imported_preview_source", "refine_import_source"}
            and attempt.get("artifact_fingerprint") == source_artifact_fingerprint
        ):
            return attempt
    return None


def _next_backlog_attempt_count(state: dict[str, Any]) -> int:
    attempts = state.get("backlog_attempts")
    return len(attempts) + 1 if isinstance(attempts, list) else 1


__all__ = [
    "BacklogPhaseError",
    "backlog_state_from_complete",
    "ensure_backlog_attempts",
    "generate_backlog_draft",
    "get_backlog_history",
    "import_backlog_refinement",
    "mark_backlog_refinement_approved",
    "preview_backlog_draft",
    "preview_backlog_refinement",
    "record_backlog_attempt",
    "record_backlog_refinement",
    "save_backlog_draft",
    "set_backlog_fsm_state",
]
