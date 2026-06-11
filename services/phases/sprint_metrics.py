# services/phases/sprint_metrics.py
"""Pure Sprint metrics projection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite
from statistics import median
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

OLDEST_COMPLETED_AT: datetime = datetime(1, 1, 1, tzinfo=UTC).replace(tzinfo=None)

TOKEN_METRICS_UNAVAILABLE: dict[str, Any] = {
    "status": "unavailable",
    "prompt_tokens": None,
    "completion_tokens": None,
    "total_tokens": None,
    "estimated_cost_usd": None,
    "reason": "Token usage is not yet captured in durable AgileForge records.",
}


def build_sprint_metrics(
    *,
    project_id: int,
    completed_sprints: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return read-only Sprint metrics and planning recommendation."""
    rows: list[dict[str, Any]] = []
    unestimated_completed_story_counts: list[int] = []
    warnings: list[dict[str, Any]] = []

    for sprint in sorted(completed_sprints, key=_sort_key, reverse=True):
        row, row_warnings, unestimated_completed_story_count = (
            _build_completed_sprint_row(sprint)
        )
        rows.append(row)
        unestimated_completed_story_counts.append(unestimated_completed_story_count)
        warnings.extend(row_warnings)

    summary = _build_summary(
        rows,
        unestimated_completed_story_counts=unestimated_completed_story_counts,
    )
    recommendation = _build_recommendation(rows)

    if not rows:
        status = "insufficient_history"
    elif warnings:
        status = "partial_history"
    else:
        status = "ready"

    return {
        "project_id": project_id,
        "status": status,
        "summary": summary,
        "recommendation": recommendation,
        "completed_sprints": rows,
        "token_metrics": dict(TOKEN_METRICS_UNAVAILABLE),
        "data_quality_warnings": warnings,
    }


def _sort_key(sprint: Mapping[str, Any]) -> tuple[datetime, int]:
    return (
        _parse_datetime(sprint.get("completed_at")) or OLDEST_COMPLETED_AT,
        _to_int(sprint.get("sprint_id")) or 0,
    )


def _build_completed_sprint_row(
    sprint: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    sprint_id = _to_int(sprint.get("sprint_id")) or 0
    elapsed_seconds, elapsed_warning = _elapsed_seconds(sprint)
    story_points_completed = _to_int(sprint.get("story_points_completed")) or 0
    story_points_planned = _to_int(sprint.get("story_points_planned")) or 0
    workflow_event_count = _to_int(sprint.get("workflow_event_count")) or 0
    turn_count = _to_int(sprint.get("turn_count"))
    unestimated_completed_story_count = (
        _to_int(sprint.get("unestimated_completed_story_count")) or 0
    )

    warnings: list[dict[str, Any]] = []
    if elapsed_warning is not None:
        warnings.append(_warning(code=elapsed_warning, sprint_id=sprint_id))
    if turn_count is None and workflow_event_count > 0:
        warnings.append(
            _warning(
                code="WORKFLOW_EVENT_TURN_COUNT_UNAVAILABLE",
                sprint_id=sprint_id,
            )
        )
    if (
        sprint.get("story_points_completed") is None
        or unestimated_completed_story_count > 0
    ):
        warnings.append(
            _warning(code="COMPLETED_STORY_POINTS_MISSING", sprint_id=sprint_id)
        )

    row = {
        "sprint_id": sprint_id,
        "goal": sprint.get("goal"),
        "status": sprint.get("status"),
        "started_at": sprint.get("started_at"),
        "completed_at": sprint.get("completed_at"),
        "start_date": sprint.get("start_date"),
        "end_date": sprint.get("end_date"),
        "story_count": _to_int(sprint.get("story_count")) or 0,
        "completed_story_count": _to_int(sprint.get("completed_story_count")) or 0,
        "task_count": _to_int(sprint.get("task_count")) or 0,
        "completed_task_count": _to_int(sprint.get("completed_task_count")) or 0,
        "story_points_planned": story_points_planned,
        "story_points_completed": story_points_completed,
        "elapsed_seconds": elapsed_seconds,
        "workflow_event_count": workflow_event_count,
        "workflow_event_duration_seconds": _whole_number_or_two_decimals(
            _to_float(sprint.get("workflow_event_duration_seconds"))
        ),
        "turn_count": turn_count,
        "history_fidelity": sprint.get("history_fidelity"),
    }
    return row, warnings, unestimated_completed_story_count


def _build_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    unestimated_completed_story_counts: Sequence[int],
) -> dict[str, Any]:
    completed_points = [_to_int(row.get("story_points_completed")) or 0 for row in rows]
    elapsed_values = [
        elapsed
        for row in rows
        if (elapsed := _to_float(row.get("elapsed_seconds"))) is not None
    ]
    total_elapsed_seconds = sum(elapsed_values) if elapsed_values else None
    completed_story_points = sum(completed_points)

    return {
        "completed_sprint_count": len(rows),
        "completed_story_count": sum(
            _to_int(row.get("completed_story_count")) or 0 for row in rows
        ),
        "completed_task_count": sum(
            _to_int(row.get("completed_task_count")) or 0 for row in rows
        ),
        "completed_story_points": completed_story_points,
        "total_elapsed_seconds": _whole_number_or_two_decimals(total_elapsed_seconds),
        "average_points_per_sprint": _mean_or_none(completed_points),
        "median_points_per_sprint": (
            _whole_number_or_two_decimals(median(completed_points))
            if completed_points
            else None
        ),
        "average_elapsed_seconds_per_sprint": _mean_or_none(elapsed_values),
        "points_per_hour": _points_per_hour(
            completed_story_points=completed_story_points,
            total_elapsed_seconds=total_elapsed_seconds,
        ),
        "sprints_with_elapsed_time_count": len(elapsed_values),
        "unestimated_completed_story_count": sum(unestimated_completed_story_counts),
    }


def _build_recommendation(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "recommended_next_sprint_points": None,
            "basis": "insufficient_history",
            "source_sprint_ids": [],
            "source_completed_points": [],
            "sample_size": 0,
            "explanation": (
                "Complete at least one Sprint or provide an explicit manual "
                "Sprint capacity."
            ),
        }

    sample = list(rows[:3])
    source_completed_points = [
        _to_int(row.get("story_points_completed")) or 0 for row in sample
    ]
    recommended = _round_half_up(
        Decimal(sum(source_completed_points)) / Decimal(len(source_completed_points))
    )

    return {
        "recommended_next_sprint_points": max(0, recommended),
        "basis": f"last_{len(sample)}_completed_sprints_average",
        "source_sprint_ids": [_to_int(row.get("sprint_id")) or 0 for row in sample],
        "source_completed_points": source_completed_points,
        "sample_size": len(sample),
        "explanation": (
            "Recommended from the rounded average of the last "
            f"{len(sample)} completed Sprints."
        ),
    }


def _elapsed_seconds(
    sprint: Mapping[str, Any],
) -> tuple[int | float | None, str | None]:
    started_at = sprint.get("started_at")
    completed_at = sprint.get("completed_at")
    if started_at is None or completed_at is None:
        return None, "SPRINT_ELAPSED_TIME_UNAVAILABLE"

    started_dt = _parse_datetime(started_at)
    completed_dt = _parse_datetime(completed_at)
    if started_dt is None or completed_dt is None or completed_dt < started_dt:
        return None, "SPRINT_ELAPSED_TIME_INVALID"

    raw_elapsed = sprint.get("elapsed_seconds")
    if raw_elapsed is None:
        return int((completed_dt - started_dt).total_seconds()), None

    elapsed = _to_float(raw_elapsed)
    if elapsed is None or elapsed < 0:
        return None, "SPRINT_ELAPSED_TIME_INVALID"
    return _whole_number_or_two_decimals(elapsed), None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _warning(*, code: str, sprint_id: int) -> dict[str, Any]:
    messages = {
        "SPRINT_ELAPSED_TIME_UNAVAILABLE": (
            "Sprint elapsed time is unavailable because started_at or "
            "completed_at is missing."
        ),
        "SPRINT_ELAPSED_TIME_INVALID": (
            "Sprint elapsed time is invalid because completed_at is before "
            "started_at or elapsed_seconds is negative."
        ),
        "WORKFLOW_EVENT_TURN_COUNT_UNAVAILABLE": (
            "Workflow Event turn counts are unavailable for this Sprint."
        ),
        "COMPLETED_STORY_POINTS_MISSING": (
            "One or more completed Stories linked to this Sprint are missing "
            "point estimates."
        ),
    }
    return {
        "code": code,
        "sprint_id": sprint_id,
        "message": messages[code],
    }


def _mean_or_none(values: Sequence[int | float]) -> int | float | None:
    if not values:
        return None
    return _whole_number_or_two_decimals(sum(values) / len(values))


def _points_per_hour(
    *,
    completed_story_points: int,
    total_elapsed_seconds: int | float | None,
) -> int | float | None:
    if total_elapsed_seconds is None or total_elapsed_seconds <= 0:
        return None
    return _whole_number_or_two_decimals(
        completed_story_points / (total_elapsed_seconds / 3600)
    )


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _whole_number_or_two_decimals(value: int | float | None) -> int | float | None:
    if value is None:
        return None
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def _to_int(value: object) -> int | None:
    result: int | None = None
    if value is None or isinstance(value, bool):
        return result
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if value.is_integer():
            result = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            numeric_value = Decimal(value.strip())
        except InvalidOperation:
            pass
        else:
            if (
                numeric_value.is_finite()
                and numeric_value == numeric_value.to_integral()
            ):
                result = int(numeric_value)
    return result


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    result: float
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            result = float(value)
        except ValueError:
            return None
    else:
        return None
    if isfinite(result):
        return result
    return None
