"""Post-sprint triage validation and workflow-state helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from services.agent_workbench.fingerprints import canonical_hash

TRIAGE_SCHEMA_VERSION: Final[str] = "agileforge.post_sprint_triage.v1"
VALID_TRIAGE_IMPACTS: Final[frozenset[str]] = frozenset(
    {"none", "task", "story", "roadmap", "backlog", "multiple"}
)
VALID_AFFECTED_LAYERS: Final[frozenset[str]] = frozenset(
    {"task", "story", "roadmap", "backlog"}
)
TRIAGE_AFFECTED_FIELDS: Final[tuple[str, ...]] = (
    "affected_requirements",
    "affected_task_ids",
    "affected_story_ids",
    "affected_backlog_item_ids",
    "affected_roadmap_item_ids",
)
TRIAGE_IMPACT_FIELDS_INVALID: Final[str] = "TRIAGE_IMPACT_FIELDS_INVALID"
TRIAGE_REQUIRED_FIELD_MISSING: Final[str] = "TRIAGE_REQUIRED_FIELD_MISSING"


@dataclass(frozen=True)
class PostSprintTriageValidationError(ValueError):
    code: str
    message: str
    details: dict[str, Any]
    remediation: list[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", (self.message,))


def build_triage_payload(
    *,
    project_id: int,
    sprint_id: int,
    impact: str,
    affected_requirements: list[object] | None,
    affected_task_ids: list[object] | None,
    affected_story_ids: list[object] | None,
    affected_backlog_item_ids: list[object] | None,
    affected_roadmap_item_ids: list[object] | None,
    affected_layers: list[object] | None,
    learning_summary: str,
    decision_reason: str,
    idempotency_key: str,
    replace_existing: bool,
    recorded_at: str,
    recorded_by: str,
) -> dict[str, Any]:
    """Return a normalized post-sprint triage payload with stable fingerprints."""
    normalized_impact = _normalize_impact(impact)
    normalized_fields: dict[str, list[str] | list[int]] = {
        "affected_requirements": _normalize_text_list(affected_requirements),
        "affected_task_ids": _normalize_positive_int_list(affected_task_ids),
        "affected_story_ids": _normalize_positive_int_list(affected_story_ids),
        "affected_backlog_item_ids": _normalize_positive_int_list(
            affected_backlog_item_ids
        ),
        "affected_roadmap_item_ids": _normalize_positive_int_list(
            affected_roadmap_item_ids
        ),
    }
    normalized_layers = _normalize_affected_layers(affected_layers)
    normalized_learning_summary = _required_text(
        learning_summary,
        field_name="learning_summary",
    )
    normalized_decision_reason = _required_text(
        decision_reason,
        field_name="decision_reason",
    )
    normalized_idempotency_key = _normalize_text(idempotency_key)
    normalized_recorded_at = _normalize_text(recorded_at)
    normalized_recorded_by = _normalize_text(recorded_by)

    _validate_impact_fields(
        impact=normalized_impact,
        affected_requirements=normalized_fields["affected_requirements"],
        affected_task_ids=normalized_fields["affected_task_ids"],
        affected_story_ids=normalized_fields["affected_story_ids"],
        affected_backlog_item_ids=normalized_fields["affected_backlog_item_ids"],
        affected_roadmap_item_ids=normalized_fields["affected_roadmap_item_ids"],
        affected_layers=normalized_layers,
        decision_reason=normalized_decision_reason,
    )

    request_fingerprint_payload: dict[str, Any] = {
        "project_id": project_id,
        "sprint_id": sprint_id,
        "impact": normalized_impact,
        **normalized_fields,
        "affected_layers": normalized_layers,
        "learning_summary": normalized_learning_summary,
        "decision_reason": normalized_decision_reason,
        "idempotency_key": normalized_idempotency_key,
        "replace_existing": bool(replace_existing),
    }
    payload: dict[str, Any] = {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        **request_fingerprint_payload,
        "recorded_at": normalized_recorded_at,
        "recorded_by": normalized_recorded_by,
    }
    payload["request_fingerprint"] = canonical_hash(request_fingerprint_payload)
    payload["triage_fingerprint"] = canonical_hash(payload)
    return payload


def current_triage_for_latest_sprint(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return stored triage only when it belongs to the latest completed sprint."""
    latest_completed_sprint_id = state.get("latest_completed_sprint_id")
    if latest_completed_sprint_id is None:
        return None

    triage = state.get("post_sprint_triage")
    if not isinstance(triage, dict):
        return None
    if triage.get("sprint_id") != latest_completed_sprint_id:
        return None
    return triage


def post_sprint_triage_required(state: dict[str, Any]) -> bool:
    """Return whether the latest completed sprint still needs triage."""
    if state.get("fsm_state") != "SPRINT_COMPLETE":
        return False
    if state.get("latest_completed_sprint_id") is None:
        return False
    return current_triage_for_latest_sprint(state) is None


def _normalize_text(value: object) -> str:
    return str(value).strip()


def _normalize_text_list(values: list[object] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        if value is None:
            continue
        text = _normalize_text(value)
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _normalize_positive_int_list(values: list[object] | None) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        item_id = _positive_int_or_none(value)
        if item_id is None or item_id in seen:
            continue
        normalized.append(item_id)
        seen.add(item_id)
    return normalized


def _positive_int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if normalized <= 0:
        return None
    return normalized


def _normalize_impact(impact: str) -> str:
    normalized = impact.strip().lower()
    if normalized not in VALID_TRIAGE_IMPACTS:
        _raise_invalid_impact_fields(
            "Unknown post-sprint triage impact.",
            details={
                "impact": normalized,
                "valid_impacts": sorted(VALID_TRIAGE_IMPACTS),
            },
            remediation=[
                "Use one of: none, task, story, roadmap, backlog, multiple.",
            ],
        )
    return normalized


def _normalize_affected_layers(values: list[object] | None) -> list[str]:
    normalized_layers: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        if value is None:
            continue
        layer = _normalize_text(value).lower()
        if not layer or layer in seen:
            continue
        normalized_layers.append(layer)
        seen.add(layer)

    invalid_layers = [
        layer for layer in normalized_layers if layer not in VALID_AFFECTED_LAYERS
    ]
    if invalid_layers:
        _raise_invalid_impact_fields(
            "Affected layers must use known post-sprint layers.",
            details={
                "affected_layers": normalized_layers,
                "invalid_layers": invalid_layers,
                "valid_layers": sorted(VALID_AFFECTED_LAYERS),
            },
            remediation=[
                "Use affected_layers from: task, story, roadmap, backlog.",
            ],
        )
    return normalized_layers


def _required_text(value: object, *, field_name: str) -> str:
    normalized = _normalize_text(value)
    if normalized:
        return normalized
    raise PostSprintTriageValidationError(
        code=TRIAGE_REQUIRED_FIELD_MISSING,
        message=f"{field_name} is required.",
        details={"field": field_name},
        remediation=[f"Provide a non-empty {field_name} value."],
    )


def _validate_impact_fields(
    *,
    impact: str,
    affected_requirements: list[str] | list[int],
    affected_task_ids: list[str] | list[int],
    affected_story_ids: list[str] | list[int],
    affected_backlog_item_ids: list[str] | list[int],
    affected_roadmap_item_ids: list[str] | list[int],
    affected_layers: list[str],
    decision_reason: str,
) -> None:
    affected_field_values = {
        "affected_requirements": affected_requirements,
        "affected_task_ids": affected_task_ids,
        "affected_story_ids": affected_story_ids,
        "affected_backlog_item_ids": affected_backlog_item_ids,
        "affected_roadmap_item_ids": affected_roadmap_item_ids,
    }

    if impact == "none":
        if any(affected_field_values.values()) or affected_layers:
            _raise_invalid_impact_fields(
                "impact=none cannot include affected fields or layers.",
                details={
                    "impact": impact,
                    "affected_fields": affected_field_values,
                    "affected_layers": affected_layers,
                },
                remediation=[
                    "Clear all affected fields when impact is none.",
                ],
            )
        return

    if impact == "task":
        if not (affected_task_ids or affected_story_ids or affected_requirements):
            _raise_missing_impact_fields(
                impact,
                "impact=task requires affected task ids, story ids, or requirements.",
                affected_field_values,
                affected_layers,
            )
        return

    if impact == "story":
        if not (affected_story_ids or affected_requirements):
            _raise_missing_impact_fields(
                impact,
                "impact=story requires affected story ids or requirements.",
                affected_field_values,
                affected_layers,
            )
        return

    if impact == "roadmap":
        if not (affected_roadmap_item_ids or affected_requirements):
            _raise_missing_impact_fields(
                impact,
                "impact=roadmap requires affected roadmap item ids or requirements.",
                affected_field_values,
                affected_layers,
            )
        return

    if impact == "backlog":
        if not (affected_backlog_item_ids or decision_reason):
            _raise_missing_impact_fields(
                impact,
                "impact=backlog requires affected backlog item ids or a decision reason.",
                affected_field_values,
                affected_layers,
            )
        return

    if impact == "multiple" and len(affected_layers) < 2:
        _raise_invalid_impact_fields(
            "impact=multiple requires at least two structured affected layers.",
            details={
                "impact": impact,
                "affected_layers": affected_layers,
            },
            remediation=[
                "Provide at least two affected_layers values.",
            ],
        )


def _raise_missing_impact_fields(
    impact: str,
    message: str,
    affected_fields: dict[str, list[str] | list[int]],
    affected_layers: list[str],
) -> None:
    _raise_invalid_impact_fields(
        message,
        details={
            "impact": impact,
            "affected_fields": affected_fields,
            "affected_layers": affected_layers,
        },
        remediation=[
            "Provide structured affected fields for the selected impact.",
        ],
    )


def _raise_invalid_impact_fields(
    message: str,
    *,
    details: dict[str, Any],
    remediation: list[str],
) -> None:
    raise PostSprintTriageValidationError(
        code=TRIAGE_IMPACT_FIELDS_INVALID,
        message=message,
        details=details,
        remediation=remediation,
    )
