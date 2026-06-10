"""Post-sprint triage validation and workflow-state helpers."""

from __future__ import annotations

from copy import deepcopy
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
TRIAGE_STORED_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "project_id",
    "sprint_id",
    "impact",
    *TRIAGE_AFFECTED_FIELDS,
    "affected_layers",
    "learning_summary",
    "decision_reason",
    "idempotency_key",
    "replace_existing",
    "recorded_at",
    "recorded_by",
    "request_fingerprint",
    "triage_fingerprint",
)
TRIAGE_IMPACT_FIELDS_INVALID: Final[str] = "TRIAGE_IMPACT_FIELDS_INVALID"
TRIAGE_REQUIRED_FIELD_MISSING: Final[str] = "TRIAGE_REQUIRED_FIELD_MISSING"
TRIAGE_FIELD_INVALID: Final[str] = "TRIAGE_FIELD_INVALID"


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
    replace_existing: bool | str,
    recorded_at: str,
    recorded_by: str,
) -> dict[str, Any]:
    """Return a normalized post-sprint triage payload with stable fingerprints."""
    normalized_project_id = _required_positive_int(project_id, field_name="project_id")
    normalized_sprint_id = _required_positive_int(sprint_id, field_name="sprint_id")
    normalized_impact = _normalize_impact(impact)
    normalized_fields: dict[str, list[str] | list[int]] = {
        "affected_requirements": _normalize_text_list(
            affected_requirements,
            field_name="affected_requirements",
        ),
        "affected_task_ids": _normalize_positive_int_list(
            affected_task_ids,
            field_name="affected_task_ids",
        ),
        "affected_story_ids": _normalize_positive_int_list(
            affected_story_ids,
            field_name="affected_story_ids",
        ),
        "affected_backlog_item_ids": _normalize_text_list(
            affected_backlog_item_ids,
            field_name="affected_backlog_item_ids",
        ),
        "affected_roadmap_item_ids": _normalize_text_list(
            affected_roadmap_item_ids,
            field_name="affected_roadmap_item_ids",
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
    normalized_idempotency_key = _required_text(
        idempotency_key,
        field_name="idempotency_key",
    )
    normalized_recorded_at = _required_text(recorded_at, field_name="recorded_at")
    normalized_recorded_by = _required_text(recorded_by, field_name="recorded_by")
    normalized_replace_existing = _normalize_replace_existing(replace_existing)

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
        "project_id": normalized_project_id,
        "sprint_id": normalized_sprint_id,
        "impact": normalized_impact,
        **normalized_fields,
        "affected_layers": normalized_layers,
        "learning_summary": normalized_learning_summary,
        "decision_reason": normalized_decision_reason,
        "idempotency_key": normalized_idempotency_key,
        "replace_existing": normalized_replace_existing,
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
    latest_completed_sprint_id = _positive_int_or_none(
        state.get("latest_completed_sprint_id")
    )
    if latest_completed_sprint_id is None:
        return None

    triage = state.get("post_sprint_triage")
    if not isinstance(triage, dict):
        return None
    if _positive_int_or_none(triage.get("sprint_id")) != latest_completed_sprint_id:
        return None
    if not _is_valid_stored_triage(triage):
        return None
    return deepcopy(triage)


def post_sprint_triage_required(state: dict[str, Any]) -> bool:
    """Return whether the latest completed sprint still needs triage."""
    if state.get("fsm_state") != "SPRINT_COMPLETE":
        return False
    if _positive_int_or_none(state.get("latest_completed_sprint_id")) is None:
        return False
    return current_triage_for_latest_sprint(state) is None


def _normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_text_list(
    values: list[object] | None,
    *,
    field_name: str,
) -> list[str]:
    values = _affected_list_or_empty(values, field_name=field_name)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = _normalize_text(value)
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _normalize_positive_int_list(
    values: list[object] | None,
    *,
    field_name: str,
) -> list[int]:
    values = _affected_list_or_empty(values, field_name=field_name)
    normalized: list[int] = []
    seen: set[int] = set()
    for value in values:
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
    if not isinstance(value, (str, bytes, bytearray)) and value != normalized:
        return None
    if normalized <= 0:
        return None
    return normalized


def _required_positive_int(value: object, *, field_name: str) -> int:
    normalized = _positive_int_or_none(value)
    if normalized is not None:
        return normalized
    raise PostSprintTriageValidationError(
        code=TRIAGE_REQUIRED_FIELD_MISSING,
        message=f"{field_name} must be a positive integer.",
        details={"field": field_name},
        remediation=[f"Provide a positive integer {field_name} value."],
    )


def _normalize_impact(impact: object) -> str:
    if not isinstance(impact, str):
        _raise_invalid_impact_fields(
            "Unknown post-sprint triage impact.",
            details={
                "impact": impact,
                "valid_impacts": sorted(VALID_TRIAGE_IMPACTS),
            },
            remediation=[
                "Use one of: none, task, story, roadmap, backlog, multiple.",
            ],
        )
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
    values = _affected_list_or_empty(values, field_name="affected_layers")
    normalized_layers: list[str] = []
    seen: set[str] = set()
    for value in values:
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
    return sorted(normalized_layers)


def _affected_list_or_empty(
    values: list[object] | None,
    *,
    field_name: str,
) -> list[object]:
    if values is None:
        return []
    if isinstance(values, list):
        return values
    _raise_invalid_impact_fields(
        f"{field_name} must be a list or null.",
        details={
            "field": field_name,
            "received_type": type(values).__name__,
        },
        remediation=[f"Provide {field_name} as a list or null."],
    )


def _normalize_replace_existing(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise PostSprintTriageValidationError(
        code=TRIAGE_FIELD_INVALID,
        message="replace_existing must be a boolean.",
        details={"field": "replace_existing"},
        remediation=["Provide replace_existing as true or false."],
    )


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

    if impact != "multiple" and affected_layers:
        _raise_invalid_impact_fields(
            "affected_layers can only be used when impact=multiple.",
            details={
                "impact": impact,
                "affected_layers": affected_layers,
            },
            remediation=[
                "Clear affected_layers or set impact=multiple with at least two layers.",
            ],
        )

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


def _is_valid_stored_triage(triage: dict[str, Any]) -> bool:
    if set(triage) != set(TRIAGE_STORED_REQUIRED_FIELDS):
        return False
    if triage.get("schema_version") != TRIAGE_SCHEMA_VERSION:
        return False
    impact = triage.get("impact")
    if not isinstance(impact, str) or impact not in VALID_TRIAGE_IMPACTS:
        return False
    if not _stored_fingerprint_matches(triage, "request_fingerprint"):
        return False
    if not _stored_fingerprint_matches(triage, "triage_fingerprint"):
        return False

    try:
        project_id = _stored_positive_int(triage, "project_id")
        sprint_id = _stored_positive_int(triage, "sprint_id")
        affected_requirements = _stored_normalized_text_list(
            triage,
            "affected_requirements",
        )
        affected_task_ids = _stored_normalized_int_list(triage, "affected_task_ids")
        affected_story_ids = _stored_normalized_int_list(triage, "affected_story_ids")
        affected_backlog_item_ids = _stored_normalized_text_list(
            triage,
            "affected_backlog_item_ids",
        )
        affected_roadmap_item_ids = _stored_normalized_text_list(
            triage,
            "affected_roadmap_item_ids",
        )
        affected_layers = _stored_normalized_layers(triage, "affected_layers")
        learning_summary = _stored_required_text(triage, "learning_summary")
        decision_reason = _stored_required_text(triage, "decision_reason")
        idempotency_key = _stored_required_text(triage, "idempotency_key")
        recorded_at = _stored_required_text(triage, "recorded_at")
        recorded_by = _stored_required_text(triage, "recorded_by")
        replace_existing = triage["replace_existing"]
        if not isinstance(replace_existing, bool):
            return False

        _validate_impact_fields(
            impact=impact,
            affected_requirements=affected_requirements,
            affected_task_ids=affected_task_ids,
            affected_story_ids=affected_story_ids,
            affected_backlog_item_ids=affected_backlog_item_ids,
            affected_roadmap_item_ids=affected_roadmap_item_ids,
            affected_layers=affected_layers,
            decision_reason=decision_reason,
        )
    except (KeyError, PostSprintTriageValidationError, TypeError):
        return False

    request_fingerprint_payload: dict[str, Any] = {
        "project_id": project_id,
        "sprint_id": sprint_id,
        "impact": impact,
        "affected_requirements": affected_requirements,
        "affected_task_ids": affected_task_ids,
        "affected_story_ids": affected_story_ids,
        "affected_backlog_item_ids": affected_backlog_item_ids,
        "affected_roadmap_item_ids": affected_roadmap_item_ids,
        "affected_layers": affected_layers,
        "learning_summary": learning_summary,
        "decision_reason": decision_reason,
        "idempotency_key": idempotency_key,
        "replace_existing": replace_existing,
    }
    if triage["request_fingerprint"] != canonical_hash(request_fingerprint_payload):
        return False

    payload_without_triage_fingerprint: dict[str, Any] = {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        **request_fingerprint_payload,
        "recorded_at": recorded_at,
        "recorded_by": recorded_by,
        "request_fingerprint": triage["request_fingerprint"],
    }
    if triage["triage_fingerprint"] != canonical_hash(
        payload_without_triage_fingerprint
    ):
        return False
    return True


def _stored_fingerprint_matches(triage: dict[str, Any], field_name: str) -> bool:
    fingerprint = triage.get(field_name)
    return isinstance(fingerprint, str) and fingerprint.startswith("sha256:")


def _stored_positive_int(triage: dict[str, Any], field_name: str) -> int:
    value = triage[field_name]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{field_name} must be a positive integer.")
    return value


def _stored_required_text(triage: dict[str, Any], field_name: str) -> str:
    value = triage[field_name]
    if not isinstance(value, str) or value != value.strip() or not value:
        raise TypeError(f"{field_name} must be normalized non-empty text.")
    return value


def _stored_normalized_text_list(
    triage: dict[str, Any],
    field_name: str,
) -> list[str]:
    value = triage[field_name]
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list.")
    seen: set[str] = set()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or item != item.strip() or not item:
            raise TypeError(f"{field_name} must contain normalized text.")
        if item in seen:
            raise TypeError(f"{field_name} must not contain duplicates.")
        seen.add(item)
        normalized.append(item)
    return normalized


def _stored_normalized_int_list(
    triage: dict[str, Any],
    field_name: str,
) -> list[int]:
    value = triage[field_name]
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list.")
    seen: set[int] = set()
    normalized: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise TypeError(f"{field_name} must contain positive integers.")
        if item in seen:
            raise TypeError(f"{field_name} must not contain duplicates.")
        seen.add(item)
        normalized.append(item)
    return normalized


def _stored_normalized_layers(
    triage: dict[str, Any],
    field_name: str,
) -> list[str]:
    value = _stored_normalized_text_list(triage, field_name)
    if value != sorted(value):
        raise TypeError(f"{field_name} must be sorted.")
    for item in value:
        if item.lower() != item or item not in VALID_AFFECTED_LAYERS:
            raise TypeError(f"{field_name} must contain normalized affected layers.")
    return value


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
