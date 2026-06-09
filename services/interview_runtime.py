"""Runtime helpers for failure-aware interview state projections."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Self, TypedDict, Unpack, cast

INTERVIEW_RUNTIME_KEY = "interview_runtime"
STORY_PHASE = "story"


class InterviewRuntimeTypeError(TypeError):
    """Raised when interview runtime state has an invalid container shape."""

    @classmethod
    def runtime_root_must_be_dict(cls, key: str) -> Self:
        """Build the error raised when the runtime root is not a dict."""
        return cls(f"{key} must be a dict")

    @classmethod
    def phase_bucket_must_be_dict(cls, phase: str) -> Self:
        """Build the error raised when a phase bucket is not a dict."""
        return cls(f"{phase} runtime bucket must be a dict")

    @classmethod
    def subject_projection_must_be_dict(cls) -> Self:
        """Build the error raised when the subject projection is not a dict."""
        return cls("interview subject projection must be a dict")

    @classmethod
    def feedback_projection_must_be_dict(cls) -> Self:
        """Build the error raised when feedback projection is not a dict."""
        return cls("feedback_projection must be a dict")

    @classmethod
    def feedback_items_must_be_list(cls) -> Self:
        """Build the error raised when feedback items are not a list."""
        return cls("feedback_projection.items must be a list")

    @classmethod
    def attempt_history_must_be_list(cls) -> Self:
        """Build the error raised when attempt history is not a list."""
        return cls("attempt_history must be a list")

    @classmethod
    def draft_projection_must_be_dict(cls) -> Self:
        """Build the error raised when draft projection is not a dict."""
        return cls("draft_projection must be a dict")


class _RequestProjectionOptions(TypedDict):
    request_snapshot_id: str
    payload: dict[str, Any]
    request_hash: str
    created_at: object
    draft_basis_attempt_id: str | None
    included_feedback_ids: list[str]
    context_version: str


def _require_dict(
    value: object,
    *,
    error: InterviewRuntimeTypeError,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error
    return cast("dict[str, Any]", value)


def _require_list(
    value: object,
    *,
    error: InterviewRuntimeTypeError,
) -> list[Any]:
    if not isinstance(value, list):
        raise error
    return cast("list[Any]", value)


def _runtime_root(state: dict[str, Any]) -> dict[str, Any]:
    runtime = state.setdefault(INTERVIEW_RUNTIME_KEY, {})
    return _require_dict(
        runtime,
        error=InterviewRuntimeTypeError.runtime_root_must_be_dict(
            INTERVIEW_RUNTIME_KEY
        ),
    )


def _phase_bucket(state: dict[str, Any], phase: str) -> dict[str, Any]:
    runtime = _runtime_root(state)
    bucket = runtime.setdefault(phase, {})
    return _require_dict(
        bucket,
        error=InterviewRuntimeTypeError.phase_bucket_must_be_dict(phase),
    )


def _empty_projection(phase: str, subject_key: str) -> dict[str, Any]:
    return {
        "phase": phase,
        "subject_key": subject_key,
        "attempt_history": [],
        "draft_projection": {},
        "resolution_projection": {},
        "feedback_projection": {"items": [], "next_feedback_sequence": 0},
        "request_projection": {},
    }


def _normalize_feedback_projection(projection: dict[str, Any]) -> dict[str, Any]:
    feedback_projection = projection.setdefault("feedback_projection", {})
    if not isinstance(feedback_projection, dict):
        feedback_projection = {}
        projection["feedback_projection"] = feedback_projection
    items = feedback_projection.get("items")
    if not isinstance(items, list):
        feedback_projection["items"] = []
    derived_sequence = _derive_feedback_sequence_floor(feedback_projection["items"])
    current_sequence = feedback_projection.get("next_feedback_sequence")
    if not isinstance(current_sequence, int) or current_sequence < derived_sequence:
        feedback_projection["next_feedback_sequence"] = derived_sequence
    return feedback_projection


def _derive_feedback_sequence_floor(items: list[Any]) -> int:
    max_suffix = 0
    for entry in items:
        if not isinstance(entry, dict):
            continue
        feedback_id = entry.get("feedback_id")
        if not isinstance(feedback_id, str):
            continue
        match = re.fullmatch(r"feedback-(\d+)", feedback_id)
        if not match:
            continue
        max_suffix = max(max_suffix, int(match.group(1)))
    return max_suffix


def ensure_interview_subject(
    state: dict[str, Any],
    *,
    phase: str,
    subject_key: str,
) -> dict[str, Any]:
    """Ensure a subject runtime exists and normalize its nested containers."""
    phase_bucket = _phase_bucket(state, phase)
    projection = phase_bucket.setdefault(
        subject_key,
        _empty_projection(phase, subject_key),
    )
    projection = _require_dict(
        projection,
        error=InterviewRuntimeTypeError.subject_projection_must_be_dict(),
    )

    projection["phase"] = phase
    projection["subject_key"] = subject_key
    attempt_history = projection.get("attempt_history")
    if not isinstance(attempt_history, list):
        projection["attempt_history"] = []
    draft_projection = projection.get("draft_projection")
    if not isinstance(draft_projection, dict):
        projection["draft_projection"] = {}
    resolution_projection = projection.get("resolution_projection")
    if not isinstance(resolution_projection, dict):
        projection["resolution_projection"] = {}
    request_projection = projection.get("request_projection")
    if not isinstance(request_projection, dict):
        projection["request_projection"] = {}
    _normalize_feedback_projection(projection)
    return projection


def set_request_projection(
    runtime: dict[str, Any],
    **options: Unpack[_RequestProjectionOptions],
) -> dict[str, Any]:
    """Store the latest request snapshot for the subject runtime."""
    request_projection = {
        "request_snapshot_id": options["request_snapshot_id"],
        "payload": deepcopy(options["payload"]),
        "request_hash": options["request_hash"],
        "created_at": options["created_at"],
        "draft_basis_attempt_id": options["draft_basis_attempt_id"],
        "included_feedback_ids": list(options["included_feedback_ids"]),
        "context_version": options["context_version"],
    }
    runtime["request_projection"] = request_projection
    return request_projection


def append_feedback_entry(
    runtime: dict[str, Any],
    text: str,
    created_at: object,
    feedback_id: str | None = None,
    *,
    feedback_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a feedback entry and allocate a stable feedback identifier."""
    feedback_projection = _normalize_feedback_projection(runtime)
    items = _require_list(
        feedback_projection["items"],
        error=InterviewRuntimeTypeError.feedback_items_must_be_list(),
    )
    sequence = int(feedback_projection["next_feedback_sequence"]) + 1
    feedback_projection["next_feedback_sequence"] = sequence
    generated_id = feedback_id or f"feedback-{sequence}"
    entry = {
        "feedback_id": generated_id,
        "text": text,
        "created_at": created_at,
        "status": "unabsorbed",
        "absorbed_by_attempt_id": None,
    }
    if isinstance(feedback_quality, dict):
        entry["feedback_quality"] = dict(feedback_quality)
    items.append(entry)
    return entry


def mark_feedback_absorbed(
    runtime: dict[str, Any],
    *,
    feedback_ids: list[str],
    attempt_id: str,
) -> list[dict[str, Any]]:
    """Mark matching feedback items as absorbed by the given attempt."""
    feedback_projection = _require_dict(
        runtime.get("feedback_projection") or {},
        error=InterviewRuntimeTypeError.feedback_projection_must_be_dict(),
    )
    items = _require_list(
        feedback_projection.get("items") or [],
        error=InterviewRuntimeTypeError.feedback_items_must_be_list(),
    )

    absorbed_items: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        if entry.get("feedback_id") in feedback_ids:
            entry["status"] = "absorbed"
            entry["absorbed_by_attempt_id"] = attempt_id
            absorbed_items.append(entry)
    return absorbed_items


def append_attempt(
    runtime: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    """Append a deep-copied attempt record to the runtime history."""
    stored_attempt = deepcopy(attempt)
    attempts = _require_list(
        runtime.setdefault("attempt_history", []),
        error=InterviewRuntimeTypeError.attempt_history_must_be_list(),
    )
    attempts.append(stored_attempt)
    return stored_attempt


def promote_reusable_draft(
    runtime: dict[str, Any],
    *,
    attempt_id: str,
    kind: str,
    is_complete: bool,
    updated_at: object,
) -> dict[str, Any]:
    """Promote an attempt to the latest reusable draft projection."""
    draft_projection = _require_dict(
        runtime.setdefault("draft_projection", {}),
        error=InterviewRuntimeTypeError.draft_projection_must_be_dict(),
    )
    draft_projection["latest_reusable_attempt_id"] = attempt_id
    draft_projection["kind"] = kind
    draft_projection["is_complete"] = is_complete
    draft_projection["updated_at"] = updated_at
    return draft_projection


def reset_subject_working_set(
    runtime: dict[str, Any],
    *,
    created_at: object,
    summary: str,
) -> dict[str, Any]:
    """Clear mutable projections and append a reset marker to history."""
    runtime["request_projection"] = {}
    feedback_projection = _normalize_feedback_projection(runtime)
    feedback_projection["items"] = []
    runtime["draft_projection"] = {}
    runtime["resolution_projection"] = {}

    attempts = _require_list(
        runtime.setdefault("attempt_history", []),
        error=InterviewRuntimeTypeError.attempt_history_must_be_list(),
    )
    reset_attempt = {
        "attempt_id": f"reset-marker-{len(attempts) + 1}",
        "created_at": created_at,
        "trigger": "reset",
        "classification": "reset_marker",
        "is_reusable": False,
        "retryable": False,
        "summary": summary,
        "output_artifact": None,
    }
    attempts.append(reset_attempt)
    return runtime


def _classify_story_attempt(attempt: dict[str, Any]) -> str:
    output_artifact = attempt.get("output_artifact")
    if (
        isinstance(output_artifact, dict)
        and output_artifact.get("user_stories")
        and not attempt.get("error")
    ):
        return "reusable_content_result"
    if attempt.get("failure_stage") == "invocation_exception":
        return "nonreusable_provider_failure"
    return "nonreusable_schema_failure"


def _normalized_legacy_attempt(
    attempt: dict[str, Any],
    *,
    attempt_id: str,
) -> dict[str, Any]:
    classification = _classify_story_attempt(attempt)
    is_reusable = classification == "reusable_content_result"
    legacy_attempt: dict[str, Any] = {
        "attempt_id": attempt_id,
        "created_at": attempt.get("created_at"),
        "trigger": "legacy",
        "request_snapshot_id": attempt.get("request_snapshot_id"),
        "draft_basis_attempt_id": attempt.get("draft_basis_attempt_id"),
        "included_feedback_ids": list(attempt.get("included_feedback_ids") or []),
        "classification": classification,
        "is_reusable": is_reusable,
        "retryable": False,
        "draft_kind": None,
        "output_artifact": deepcopy(attempt.get("output_artifact")),
        "failure_stage": attempt.get("failure_stage"),
        "failure_artifact_id": attempt.get("failure_artifact_id"),
        "failure_summary": attempt.get("failure_summary") or attempt.get("error"),
        "raw_output_preview": attempt.get("raw_output_preview")
        or attempt.get("raw_output")
        or attempt.get("partial_output"),
    }
    if is_reusable:
        legacy_attempt["draft_kind"] = (
            "complete_draft" if bool(attempt.get("is_complete")) else "incomplete_draft"
        )
    return legacy_attempt


def hydrate_story_runtime_from_legacy(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any]:
    """Hydrate story interview runtime from legacy ``story_attempts`` state."""
    runtime = ensure_interview_subject(
        state,
        phase=STORY_PHASE,
        subject_key=parent_requirement,
    )
    if runtime.get("attempt_history"):
        return runtime

    legacy_attempts = state.get("story_attempts") or {}
    if not isinstance(legacy_attempts, dict):
        return runtime

    attempts = legacy_attempts.get(parent_requirement) or []
    if not isinstance(attempts, list):
        return runtime

    attempt_history = _require_list(
        runtime.setdefault("attempt_history", []),
        error=InterviewRuntimeTypeError.attempt_history_must_be_list(),
    )
    for index, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict):
            continue
        normalized_attempt = _normalized_legacy_attempt(
            attempt,
            attempt_id=f"legacy-{index}",
        )
        attempt_history.append(normalized_attempt)
        if normalized_attempt["classification"] == "reusable_content_result":
            runtime["draft_projection"] = {
                "latest_reusable_attempt_id": normalized_attempt["attempt_id"],
                "kind": normalized_attempt["draft_kind"],
                "is_complete": bool(attempt.get("is_complete")),
                "updated_at": normalized_attempt.get("created_at"),
            }

    return runtime
