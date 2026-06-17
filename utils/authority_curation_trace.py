"""Durable trace artifacts for authority curation workflows."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from utils.failure_artifacts import LOGS_DIR

if TYPE_CHECKING:
    from pathlib import Path

TRACE_SCHEMA_VERSION = "agileforge.authority_curation_trace.v1"
MAX_TRACE_STRING_CHARS = 500
TRACE_DIR: Path = LOGS_DIR / "traces" / "authority_curation"
TRACE_STEP_FAILED_MESSAGE = "Authority curation trace step failed."
HASH_LIKE_RE = re.compile(
    r"(?:sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}|[0-9a-f]{32,128})",
    re.IGNORECASE,
)

APPROVED_TRACE_STEPS: frozenset[str] = frozenset(
    {
        "mutation_lease_acquired",
        "guard_validation_started",
        "guard_validation_completed",
        "guard_validation_failed",
        "curation_attempt_create_started",
        "curation_attempt_create_completed",
        "curation_attempt_create_failed",
        "workflow_curating_status_started",
        "workflow_curating_status_completed",
        "workflow_curating_status_failed",
        "input_load_started",
        "input_load_completed",
        "input_load_failed",
        "adk_invocation_started",
        "adk_invocation_completed",
        "adk_invocation_failed",
        "adk_gate_parse_started",
        "adk_gate_parse_completed",
        "adk_gate_parse_failed",
        "diff_validation_started",
        "diff_validation_completed",
        "diff_validation_failed",
        "candidate_publication_started",
        "candidate_publication_completed",
        "candidate_publication_failed",
        "workflow_pending_review_started",
        "workflow_pending_review_completed",
        "workflow_pending_review_failed",
        "mutation_finalize_started",
        "mutation_finalize_completed",
        "mutation_finalize_failed",
        "recovery_classification_started",
        "recovery_classification_completed",
        "recovery_classification_failed",
    }
)
APPROVED_TRACE_STATUSES: frozenset[str] = frozenset(
    {"started", "completed", "failed", "skipped"}
)
TRACE_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "spec_version_id",
        "source_authority_id",
        "source_authority_fingerprint",
        "feedback_attempt_id",
        "requested_model_id",
        "compiler_version",
        "prompt_hash",
        "event_count",
        "candidate_authority_id",
        "candidate_authority_fingerprint",
        "failure_stage",
        "validation_error_count",
        "untargeted_change_count",
        "curation_attempt_id",
    }
)
ERROR_KEYS: frozenset[str] = frozenset(
    {"code", "message", "retryable", "failure_artifact_id", "details"}
)
ERROR_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "failure_stage",
        "validation_error_count",
        "untargeted_change_count",
        "current_step",
        "candidate_authority_id",
        "candidate_authority_fingerprint",
    }
)
HASH_LIKE_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "source_authority_fingerprint",
        "candidate_authority_fingerprint",
        "prompt_hash",
    }
)

type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


def trace_artifact_id(mutation_event_id: int) -> str:
    """Return the stable trace artifact id for a mutation event."""
    return f"authority_curation_trace-{mutation_event_id}"


def trace_artifact_path(mutation_event_id: int) -> Path:
    """Return the JSONL trace path for a mutation event."""
    return TRACE_DIR / f"{trace_artifact_id(mutation_event_id)}.jsonl"


def append_trace_event(  # noqa: PLR0913
    *,
    mutation_event_id: int,
    project_id: int,
    step: str,
    status: str,
    curation_attempt_id: str | None = None,
    correlation_id: str | None = None,
    attributes: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Append one sanitized trace event and return the persisted payload."""
    _validate_step(step)
    _validate_status(status)

    event: dict[str, JsonValue] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_artifact_id": trace_artifact_id(mutation_event_id),
        "recorded_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "mutation_event_id": mutation_event_id,
        "project_id": project_id,
        "step": step,
        "status": status,
        "curation_attempt_id": _sanitize_optional_string(curation_attempt_id),
        "correlation_id": _sanitize_optional_string(correlation_id),
        "attributes": _sanitize_mapping(
            values=attributes,
            allowed_keys=TRACE_ATTRIBUTE_KEYS,
        ),
    }
    sanitized_error = _sanitize_error(error)
    if sanitized_error is not None:
        event["error"] = sanitized_error

    path = trace_artifact_path(mutation_event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as trace_file:
        trace_file.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def read_trace_events(mutation_event_id: int) -> list[dict[str, Any]]:
    """Read trace events for a mutation event from its JSONL artifact."""
    events, _invalid_event_count = _read_trace_events_with_invalid_count(
        mutation_event_id=mutation_event_id
    )
    return events


def _read_trace_events_with_invalid_count(
    *,
    mutation_event_id: int,
) -> tuple[list[dict[str, Any]], int]:
    """Read valid trace objects and count corrupt or non-object JSONL records."""
    path = trace_artifact_path(mutation_event_id)
    if not path.exists():
        return [], 0
    events: list[dict[str, Any]] = []
    invalid_event_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_event_count += 1
            continue
        if not isinstance(event, dict):
            invalid_event_count += 1
            continue
        events.append(event)
    return events, invalid_event_count


def summarize_trace(mutation_event_id: int) -> dict[str, JsonValue]:
    """Return a bounded summary for one authority curation trace."""
    events, invalid_event_count = _read_trace_events_with_invalid_count(
        mutation_event_id=mutation_event_id
    )
    last_event = events[-1] if events else {}
    summary: dict[str, JsonValue] = {
        "trace_artifact_id": trace_artifact_id(mutation_event_id),
        "mutation_event_id": mutation_event_id,
        "event_count": len(events),
        "invalid_event_count": invalid_event_count,
        "last_trace_step": _string_value(last_event.get("step")),
        "last_trace_status": _string_value(last_event.get("status")),
        "candidate_published": False,
    }

    for event in events:
        if (
            event.get("step") == "candidate_publication_completed"
            and event.get("status") == "completed"
        ):
            summary["candidate_published"] = True
        attributes = event.get("attributes")
        if isinstance(attributes, dict):
            _copy_summary_attribute(
                summary=summary,
                attributes=attributes,
                key="candidate_authority_id",
            )
            _copy_summary_attribute(
                summary=summary,
                attributes=attributes,
                key="candidate_authority_fingerprint",
            )
    return summary


@contextmanager
def trace_step(  # noqa: PLR0913
    *,
    mutation_event_id: int,
    project_id: int,
    step: str,
    completed_step: str,
    failed_step: str | None = None,
    curation_attempt_id: str | None = None,
    correlation_id: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """Trace a started event plus completed or failed terminal event."""
    append_trace_event(
        mutation_event_id=mutation_event_id,
        project_id=project_id,
        step=step,
        status="started",
        curation_attempt_id=curation_attempt_id,
        correlation_id=correlation_id,
        attributes=attributes,
    )
    try:
        yield
    except Exception as exc:
        with suppress(Exception):
            append_trace_event(
                mutation_event_id=mutation_event_id,
                project_id=project_id,
                step=failed_step or _default_failed_step(step),
                status="failed",
                curation_attempt_id=curation_attempt_id,
                correlation_id=correlation_id,
                attributes=attributes,
                error={
                    "code": type(exc).__name__,
                    "message": TRACE_STEP_FAILED_MESSAGE,
                    "retryable": False,
                    "details": {"current_step": step},
                },
            )
        raise
    else:
        with suppress(Exception):
            append_trace_event(
                mutation_event_id=mutation_event_id,
                project_id=project_id,
                step=completed_step,
                status="completed",
                curation_attempt_id=curation_attempt_id,
                correlation_id=correlation_id,
                attributes=attributes,
            )


def _validate_step(step: str) -> None:
    if step not in APPROVED_TRACE_STEPS:
        message = f"unknown authority curation trace step: {step}"
        raise ValueError(message)


def _validate_status(status: str) -> None:
    if status not in APPROVED_TRACE_STATUSES:
        message = f"unknown authority curation trace status: {status}"
        raise ValueError(message)


def _default_failed_step(step: str) -> str:
    if step.endswith("_started"):
        candidate = f"{step.removesuffix('_started')}_failed"
        if candidate in APPROVED_TRACE_STEPS:
            return candidate
    return step


def _sanitize_error(error: Mapping[str, object] | None) -> dict[str, JsonValue] | None:
    if error is None:
        return None
    sanitized: dict[str, JsonValue] = {}
    for key in ERROR_KEYS - {"details"}:
        if key in error:
            sanitized_value = _sanitize_value(error[key])
            if sanitized_value is not None or error[key] is None:
                sanitized[key] = sanitized_value
    details = error.get("details")
    detail_values = _sanitize_mapping(
        values=cast("Mapping[str, object]", details)
        if isinstance(details, Mapping)
        else None,
        allowed_keys=ERROR_DETAIL_KEYS,
    )
    if detail_values:
        sanitized["details"] = detail_values
    return sanitized


def _sanitize_mapping(
    *,
    values: Mapping[str, object] | None,
    allowed_keys: frozenset[str],
) -> dict[str, JsonValue]:
    if values is None:
        return {}
    sanitized: dict[str, JsonValue] = {}
    for key, value in values.items():
        if key not in allowed_keys:
            continue
        sanitized_value = _sanitize_mapping_value(key=key, value=value)
        if sanitized_value is not None or value is None:
            sanitized[key] = sanitized_value
    return sanitized


def _sanitize_mapping_value(*, key: str, value: object) -> JsonValue | None:
    if key in HASH_LIKE_ATTRIBUTE_KEYS:
        return _sanitize_hash_like_value(value)
    return _sanitize_value(value)


def _sanitize_hash_like_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped_value = value.strip()
    if HASH_LIKE_RE.fullmatch(stripped_value):
        return stripped_value.lower()
    return None


def _sanitize_value(value: object) -> JsonValue | None:
    if value is None:
        return None
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:MAX_TRACE_STRING_CHARS]
    return None


def _sanitize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:MAX_TRACE_STRING_CHARS]


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _copy_summary_attribute(
    *,
    summary: dict[str, JsonValue],
    attributes: dict[str, Any],
    key: str,
) -> None:
    value = attributes.get(key)
    if isinstance(value, bool | int | float | str) or value is None:
        summary[key] = value
