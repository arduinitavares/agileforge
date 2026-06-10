"""Post-sprint triage validation and fingerprint helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, NoReturn, TypeVar, cast

from services.agent_workbench.fingerprints import canonical_hash

TRIAGE_SCHEMA_VERSION: Final[str] = "agileforge.post_sprint_triage.v1"

VALID_TRIAGE_IMPACTS: Final[frozenset[str]] = frozenset(
    {"none", "task", "story", "roadmap", "backlog", "multiple"}
)
VALID_AFFECTED_LAYERS: Final[frozenset[str]] = frozenset(
    {"task", "story", "roadmap", "backlog"}
)
TRIAGE_IMPACT_FIELDS_INVALID: Final[str] = "TRIAGE_IMPACT_FIELDS_INVALID"
TRIAGE_REQUIRED_FIELD_MISSING: Final[str] = "TRIAGE_REQUIRED_FIELD_MISSING"

_T = TypeVar("_T")


class PostSprintTriageValidationError(ValueError):
    """Raised when a post-sprint triage payload is structurally invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code


def build_triage_payload(
    *,
    project_id: int,
    sprint_id: int,
    impact: str,
    affected_requirements: Sequence[str],
    affected_task_ids: Sequence[int | str],
    affected_story_ids: Sequence[int | str],
    affected_backlog_item_ids: Sequence[int | str],
    affected_roadmap_item_ids: Sequence[int | str],
    affected_layers: Sequence[str],
    learning_summary: str,
    decision_reason: str,
    idempotency_key: str,
    replace_existing: bool,
    recorded_at: str,
    recorded_by: str,
) -> dict[str, object]:
    """Return a normalized post-sprint triage payload with stable fingerprints."""
    normalized_impact = _normalize_impact(impact)
    normalized_requirements = _normalize_text_list(affected_requirements)
    normalized_task_ids = _normalize_id_list(
        affected_task_ids,
        field_name="affected_task_ids",
    )
    normalized_story_ids = _normalize_id_list(
        affected_story_ids,
        field_name="affected_story_ids",
    )
    normalized_backlog_item_ids = _normalize_id_list(
        affected_backlog_item_ids,
        field_name="affected_backlog_item_ids",
    )
    normalized_roadmap_item_ids = _normalize_id_list(
        affected_roadmap_item_ids,
        field_name="affected_roadmap_item_ids",
    )
    normalized_layers = _normalize_layers(affected_layers)
    normalized_learning_summary = _required_text(
        learning_summary,
        field_name="learning_summary",
    )
    normalized_decision_reason = _required_text(
        decision_reason,
        field_name="decision_reason",
    )
    normalized_idempotency_key = _required_text(
        idempotency_key,
        field_name="idempotency_key",
    )
    normalized_recorded_at = _required_text(recorded_at, field_name="recorded_at")
    normalized_recorded_by = _required_text(recorded_by, field_name="recorded_by")

    _validate_impact_fields(
        impact=normalized_impact,
        affected_requirements=normalized_requirements,
        affected_task_ids=normalized_task_ids,
        affected_story_ids=normalized_story_ids,
        affected_backlog_item_ids=normalized_backlog_item_ids,
        affected_roadmap_item_ids=normalized_roadmap_item_ids,
        affected_layers=normalized_layers,
    )

    request_payload = {
        "project_id": project_id,
        "sprint_id": sprint_id,
        "impact": normalized_impact,
        "affected_requirements": normalized_requirements,
        "affected_task_ids": normalized_task_ids,
        "affected_story_ids": normalized_story_ids,
        "affected_backlog_item_ids": normalized_backlog_item_ids,
        "affected_roadmap_item_ids": normalized_roadmap_item_ids,
        "affected_layers": normalized_layers,
        "learning_summary": normalized_learning_summary,
        "decision_reason": normalized_decision_reason,
        "idempotency_key": normalized_idempotency_key,
        "replace_existing": replace_existing,
    }
    payload: dict[str, object] = {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        **request_payload,
        "recorded_at": normalized_recorded_at,
        "recorded_by": normalized_recorded_by,
        "request_fingerprint": canonical_hash(request_payload),
    }
    payload["triage_fingerprint"] = canonical_hash(payload)
    return payload


def current_triage_for_latest_sprint(
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return stored triage only when it belongs to the latest completed sprint."""
    latest_sprint_id = _coerce_state_int(state.get("latest_completed_sprint_id"))
    if latest_sprint_id is None:
        return None

    triage = state.get("post_sprint_triage")
    if not isinstance(triage, Mapping):
        return None

    triage_sprint_id = _coerce_state_int(triage.get("sprint_id"))
    if triage_sprint_id != latest_sprint_id:
        return None
    return dict(cast("Mapping[str, Any]", triage))


def post_sprint_triage_required(state: Mapping[str, Any]) -> bool:
    """Return whether the latest completed sprint still needs triage."""
    workflow_state = str(state.get("fsm_state", "")).strip().upper()
    if workflow_state != "SPRINT_COMPLETE":
        return False
    if _coerce_state_int(state.get("latest_completed_sprint_id")) is None:
        return False
    return current_triage_for_latest_sprint(state) is None


def _normalize_impact(impact: str) -> str:
    normalized = impact.strip().lower()
    if normalized not in VALID_TRIAGE_IMPACTS:
        _raise_invalid_impact_fields(f"Invalid triage impact {impact!r}.")
    return normalized


def _normalize_text_list(values: Sequence[str]) -> list[str]:
    return sorted(
        _dedupe(
            value.strip()
            for value in values
            if value.strip()
        ),
        key=str.casefold,
    )


def _normalize_id_list(
    values: Sequence[int | str],
    *,
    field_name: str,
) -> list[int]:
    return sorted(
        _dedupe(
            _normalize_id(value, field_name=field_name)
            for value in values
        )
    )


def _normalize_id(value: int | str, *, field_name: str) -> int:
    if isinstance(value, bool):
        _raise_invalid_impact_fields(f"{field_name} contains a boolean id.")
    if isinstance(value, int):
        normalized = value
    else:
        stripped = value.strip()
        if not stripped.isdigit():
            _raise_invalid_impact_fields(f"{field_name} contains a non-integer id.")
        normalized = int(stripped)
    if normalized <= 0:
        _raise_invalid_impact_fields(f"{field_name} must contain positive ids.")
    return normalized


def _normalize_layers(values: Sequence[str]) -> list[str]:
    normalized_layers = [
        value.strip().lower()
        for value in values
        if value.strip()
    ]
    invalid_layers = sorted(set(normalized_layers) - VALID_AFFECTED_LAYERS)
    if invalid_layers:
        _raise_invalid_impact_fields(
            f"Invalid affected layers: {', '.join(invalid_layers)}."
        )
    return sorted(_dedupe(normalized_layers))


def _required_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise PostSprintTriageValidationError(
            TRIAGE_REQUIRED_FIELD_MISSING,
            f"{field_name} is required.",
        )
    return normalized


def _validate_impact_fields(
    *,
    impact: str,
    affected_requirements: Sequence[str],
    affected_task_ids: Sequence[int],
    affected_story_ids: Sequence[int],
    affected_backlog_item_ids: Sequence[int],
    affected_roadmap_item_ids: Sequence[int],
    affected_layers: Sequence[str],
) -> None:
    has_any_affected_field = any(
        (
            affected_requirements,
            affected_task_ids,
            affected_story_ids,
            affected_backlog_item_ids,
            affected_roadmap_item_ids,
        )
    )
    if impact == "none":
        if has_any_affected_field or affected_layers:
            _raise_invalid_impact_fields(
                "impact=none cannot include affected fields or layers."
            )
        return

    if impact == "multiple":
        if len(affected_layers) < 2:
            _raise_invalid_impact_fields(
                "impact=multiple requires at least two affected layers."
            )
        return

    if impact == "task" and not (affected_task_ids or affected_requirements):
        _raise_invalid_impact_fields(
            "impact=task requires affected task ids or requirements."
        )
    if impact == "story" and not (affected_story_ids or affected_requirements):
        _raise_invalid_impact_fields(
            "impact=story requires affected story ids or requirements."
        )
    if impact == "backlog" and not (
        affected_backlog_item_ids or affected_requirements
    ):
        _raise_invalid_impact_fields(
            "impact=backlog requires affected backlog item ids or requirements."
        )
    if impact == "roadmap" and not (
        affected_roadmap_item_ids or affected_requirements
    ):
        _raise_invalid_impact_fields(
            "impact=roadmap requires affected roadmap item ids or requirements."
        )


def _coerce_state_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _dedupe(values: Iterable[_T]) -> list[_T]:
    deduped: list[_T] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _raise_invalid_impact_fields(message: str) -> NoReturn:
    raise PostSprintTriageValidationError(TRIAGE_IMPACT_FIELDS_INVALID, message)
