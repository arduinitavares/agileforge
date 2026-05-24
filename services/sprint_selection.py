"""Pure Sprint selection policy helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_VELOCITY_STORY_LIMITS: dict[str, int] = {
    "Low": 3,
    "Medium": 5,
    "High": 7,
}
_DEFAULT_STORY_LIMIT = 5
_RANK_PRIORITY_BASE = 100


class SprintSelectionError(ValueError):
    """Raised when AgileForge cannot produce a safe locked Sprint selection."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the structured Sprint selection error."""
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class SprintSelectionResult:
    """Deterministic Sprint selection result."""

    mode: str
    selected_rows: list[dict[str, Any]]
    selected_story_ids: list[int]
    excluded_story_ids: list[int]
    story_points_used: int
    max_story_points: int | None
    team_velocity_assumption: str
    story_limit: int
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _SelectionPolicy:
    max_story_points: int | None
    team_velocity_assumption: str
    story_limit: int


def derive_parent_group(priority: int | None) -> int | None:
    """Return the parent group encoded by rank-style priority."""
    if priority is None or priority < _RANK_PRIORITY_BASE:
        return None
    return priority // _RANK_PRIORITY_BASE


def derive_group_slot(priority: int | None) -> int | None:
    """Return the child slot encoded by rank-style priority."""
    if priority is None or priority < _RANK_PRIORITY_BASE:
        return None
    slot = priority % _RANK_PRIORITY_BASE
    return slot or None


def select_sprint_story_rows(
    rows: list[dict[str, Any]],
    *,
    team_velocity_assumption: str,
    max_story_points: int | None,
    selected_story_ids: list[int],
) -> SprintSelectionResult:
    """Select the locked Sprint cohort before the LLM runs."""
    story_limit = _VELOCITY_STORY_LIMITS.get(
        team_velocity_assumption,
        _DEFAULT_STORY_LIMIT,
    )
    policy = _SelectionPolicy(
        max_story_points=max_story_points,
        team_velocity_assumption=team_velocity_assumption,
        story_limit=story_limit,
    )

    if selected_story_ids:
        return _select_manual(
            rows=rows,
            selected_story_ids=selected_story_ids,
            policy=policy,
        )

    return _select_auto(
        rows=rows,
        policy=policy,
    )


def _select_manual(
    *,
    rows: list[dict[str, Any]],
    selected_story_ids: list[int],
    policy: _SelectionPolicy,
) -> SprintSelectionResult:
    by_id = _rows_by_story_id(rows)
    invalid_selected_ids = [
        story_id for story_id in selected_story_ids if story_id not in by_id
    ]
    if invalid_selected_ids:
        raise SprintSelectionError(
            code="SPRINT_SELECTION_INVALID",
            message="Some selected_story_ids are not sprint candidate stories.",
            details={"invalid_selected_ids": invalid_selected_ids},
        )

    selected_rows = [by_id[story_id] for story_id in selected_story_ids]
    return _result(
        mode="manual",
        selected_rows=selected_rows,
        all_rows=rows,
        policy=policy,
    )


def _select_auto(
    *,
    rows: list[dict[str, Any]],
    policy: _SelectionPolicy,
) -> SprintSelectionResult:
    selected_rows: list[dict[str, Any]] = []
    used_points = 0

    for row in rows:
        row_points = _story_points(row)
        if row_points <= 0:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_UNSIZED_STORY",
                message="Sprint selection requires positive story_points.",
                details={"story_id": row.get("story_id")},
            )
        if len(selected_rows) >= policy.story_limit:
            break
        if (
            policy.max_story_points is not None
            and used_points + row_points > policy.max_story_points
        ):
            if not selected_rows:
                raise SprintSelectionError(
                    code="SPRINT_SELECTION_CAPACITY_BLOCKED",
                    message=(
                        "The highest-priority story exceeds the explicit Sprint "
                        "capacity. Increase --max-story-points or split the story."
                    ),
                    details={
                        "blocking_story_id": row.get("story_id"),
                        "story_points": row_points,
                        "max_story_points": policy.max_story_points,
                    },
                )
            break
        selected_rows.append(row)
        used_points += row_points

    if not selected_rows:
        raise SprintSelectionError(
            code="SPRINT_SELECTION_EMPTY",
            message="Sprint selection produced no stories.",
            details={},
        )

    return _result(
        mode="auto",
        selected_rows=selected_rows,
        all_rows=rows,
        policy=policy,
    )


def _rows_by_story_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {
        int(row["story_id"]): row
        for row in rows
        if isinstance(row, dict) and row.get("story_id") is not None
    }


def _story_points(row: dict[str, Any]) -> int:
    return int(row.get("story_points") or 0)


def _result(
    *,
    mode: str,
    selected_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    policy: _SelectionPolicy,
) -> SprintSelectionResult:
    selected_ids = [int(row["story_id"]) for row in selected_rows]
    selected_id_set = set(selected_ids)
    excluded_ids = [
        int(row["story_id"])
        for row in all_rows
        if int(row["story_id"]) not in selected_id_set
    ]
    return SprintSelectionResult(
        mode=mode,
        selected_rows=selected_rows,
        selected_story_ids=selected_ids,
        excluded_story_ids=excluded_ids,
        story_points_used=sum(_story_points(row) for row in selected_rows),
        max_story_points=policy.max_story_points,
        team_velocity_assumption=policy.team_velocity_assumption,
        story_limit=policy.story_limit,
    )
