"""Pure Sprint selection policy helpers."""

from __future__ import annotations

from collections.abc import Iterable
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
    dependency_closed: bool = True
    dependency_edges: list[dict[str, int]] = field(default_factory=list)
    dependency_promoted_story_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class _SelectionPolicy:
    max_story_points: int | None
    team_velocity_assumption: str
    story_limit: int


@dataclass(frozen=True)
class _DependencyCohort:
    story_ids: list[int]
    rows: list[dict[str, Any]]
    story_points: int


@dataclass(frozen=True)
class _ResultDependencyMetadata:
    edges: list[dict[str, int]] = field(default_factory=list)
    promoted_story_ids: list[int] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


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
    edges = _candidate_dependency_edges(rows)
    invalid_selected_ids = [
        story_id for story_id in selected_story_ids if story_id not in by_id
    ]
    if invalid_selected_ids:
        raise SprintSelectionError(
            code="SPRINT_SELECTION_INVALID",
            message="Some selected_story_ids are not sprint candidate stories.",
            details={"invalid_selected_ids": invalid_selected_ids},
        )

    seen_ids: set[int] = set()
    duplicate_ids = []
    for story_id in selected_story_ids:
        if story_id in seen_ids and story_id not in duplicate_ids:
            duplicate_ids.append(story_id)
        seen_ids.add(story_id)
    if duplicate_ids:
        raise SprintSelectionError(
            code="SPRINT_SELECTION_DUPLICATE",
            message="Manual Sprint selection contains duplicate story IDs.",
            details={"duplicate_selected_ids": duplicate_ids},
        )

    selected_id_set = set(selected_story_ids)
    priority_index = {
        story_id: (index, index) for index, story_id in enumerate(selected_story_ids)
    }
    for dependent_id in selected_story_ids:
        missing_prerequisites = sorted(edges.get(dependent_id, set()) - selected_id_set)
        if missing_prerequisites:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_DEPENDENCY_MISSING",
                message="Manual Sprint selection omits required prerequisite stories.",
                details={
                    "dependent_story_id": dependent_id,
                    "missing_prerequisite_story_ids": missing_prerequisites,
                },
            )

    reordered_ids = _topological_story_order(
        selected_id_set,
        edges=edges,
        priority_index=priority_index,
    )
    warnings = []
    if reordered_ids != selected_story_ids:
        warnings.append(
            {
                "code": "SPRINT_SELECTION_MANUAL_REORDERED",
                "message": (
                    "Manual Sprint selection was reordered to satisfy dependencies."
                ),
                "requested_story_ids": selected_story_ids,
                "selected_story_ids": reordered_ids,
            }
        )

    selected_rows = [by_id[story_id] for story_id in reordered_ids]
    return _result(
        mode="manual",
        selected_rows=selected_rows,
        all_rows=rows,
        policy=policy,
        dependency_metadata=_ResultDependencyMetadata(
            edges=_edge_payloads(edges, reordered_ids),
            warnings=warnings,
        ),
    )


def _select_auto(
    *,
    rows: list[dict[str, Any]],
    policy: _SelectionPolicy,
) -> SprintSelectionResult:
    by_id = _rows_by_story_id(rows)
    edges = _candidate_dependency_edges(rows)
    priority_index = _priority_index(rows)
    selected_rows: list[dict[str, Any]] = []
    selected_id_set: set[int] = set()
    promoted_ids: list[int] = []
    used_points = 0

    for row in sorted(rows, key=lambda item: priority_index[int(item["story_id"])]):
        story_id = int(row["story_id"])
        if story_id in selected_id_set:
            continue

        cohort = _dependency_cohort(
            story_id,
            selected_id_set=selected_id_set,
            by_id=by_id,
            edges=edges,
            priority_index=priority_index,
        )

        if len(selected_rows) + len(cohort.rows) > policy.story_limit:
            if not selected_rows:
                _raise_story_limit_blocked(
                    story_id=story_id,
                    required_story_ids=cohort.story_ids,
                    policy=policy,
                )
            break
        if (
            policy.max_story_points is not None
            and used_points + cohort.story_points > policy.max_story_points
        ):
            if not selected_rows:
                _raise_capacity_blocked(
                    story_id=story_id,
                    required_story_ids=cohort.story_ids,
                    story_points=cohort.story_points,
                    policy=policy,
                )
            break

        for cohort_id, cohort_row in zip(cohort.story_ids, cohort.rows, strict=True):
            selected_rows.append(cohort_row)
            selected_id_set.add(cohort_id)
            if cohort_id != story_id:
                promoted_ids.append(cohort_id)
        used_points += cohort.story_points

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
        dependency_metadata=_ResultDependencyMetadata(
            edges=_edge_payloads(
                edges,
                [int(row["story_id"]) for row in selected_rows],
            ),
            promoted_story_ids=promoted_ids,
        ),
    )


def _dependency_cohort(
    story_id: int,
    *,
    selected_id_set: set[int],
    by_id: dict[int, dict[str, Any]],
    edges: dict[int, set[int]],
    priority_index: dict[int, tuple[int, int]],
) -> _DependencyCohort:
    closure = _dependency_closure(story_id, edges=edges)
    cohort_ids = [
        candidate_story_id
        for candidate_story_id in _topological_story_order(
            closure,
            edges=edges,
            priority_index=priority_index,
        )
        if candidate_story_id not in selected_id_set
    ]
    cohort_rows = [by_id[candidate_story_id] for candidate_story_id in cohort_ids]
    return _DependencyCohort(
        story_ids=cohort_ids,
        rows=cohort_rows,
        story_points=_cohort_story_points(cohort_rows),
    )


def _cohort_story_points(rows: list[dict[str, Any]]) -> int:
    story_points = 0
    for row in rows:
        row_points = _story_points(row)
        if row_points <= 0:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_UNSIZED_STORY",
                message="Sprint selection requires positive story_points.",
                details={"story_id": row.get("story_id")},
            )
        story_points += row_points
    return story_points


def _raise_story_limit_blocked(
    *,
    story_id: int,
    required_story_ids: list[int],
    policy: _SelectionPolicy,
) -> None:
    raise SprintSelectionError(
        code="SPRINT_SELECTION_STORY_LIMIT_BLOCKED",
        message=(
            "The highest-priority dependency-closed story cohort exceeds "
            "the velocity story limit."
        ),
        details={
            "blocking_story_id": story_id,
            "required_story_ids": required_story_ids,
            "story_limit": policy.story_limit,
        },
    )


def _raise_capacity_blocked(
    *,
    story_id: int,
    required_story_ids: list[int],
    story_points: int,
    policy: _SelectionPolicy,
) -> None:
    raise SprintSelectionError(
        code="SPRINT_SELECTION_CAPACITY_BLOCKED",
        message=(
            "The highest-priority dependency-closed story cohort exceeds the "
            "explicit Sprint capacity. Increase --max-story-points or split "
            "the story."
        ),
        details={
            "blocking_story_id": story_id,
            "required_story_ids": required_story_ids,
            "story_points": story_points,
            "max_story_points": policy.max_story_points,
        },
    )


def _candidate_dependency_edges(rows: list[dict[str, Any]]) -> dict[int, set[int]]:
    candidate_ids = {
        int(row["story_id"])
        for row in rows
        if isinstance(row, dict) and row.get("story_id") is not None
    }
    edges: dict[int, set[int]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("story_id") is None:
            continue
        story_id = int(row["story_id"])
        blocked_by_ids = row.get("blocked_by_story_ids") or []
        edges[story_id] = _candidate_prerequisite_ids(blocked_by_ids, candidate_ids)
    return edges


def _candidate_prerequisite_ids(
    blocked_by_ids: object,
    candidate_ids: set[int],
) -> set[int]:
    if not isinstance(blocked_by_ids, Iterable) or isinstance(blocked_by_ids, str):
        return set()

    prerequisite_ids: set[int] = set()
    for prerequisite_id in blocked_by_ids:
        parsed_id = _parse_int(prerequisite_id)
        if parsed_id is not None and parsed_id in candidate_ids:
            prerequisite_ids.add(parsed_id)
    return prerequisite_ids


def _parse_int(value: object) -> int | None:
    if not isinstance(value, str | int | float | bytes | bytearray):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _priority_index(rows: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    return {
        int(row["story_id"]): (int(row.get("priority") or 0), index)
        for index, row in enumerate(rows)
        if isinstance(row, dict) and row.get("story_id") is not None
    }


def _dependency_closure(story_id: int, *, edges: dict[int, set[int]]) -> set[int]:
    closure: set[int] = set()
    visiting: set[int] = set()

    def visit(current_story_id: int) -> None:
        if current_story_id in visiting:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_DEPENDENCY_CYCLE",
                message="Sprint selection dependency graph contains a cycle.",
                details={"story_id": current_story_id},
            )
        if current_story_id in closure:
            return
        visiting.add(current_story_id)
        for prerequisite_id in edges.get(current_story_id, set()):
            visit(prerequisite_id)
        visiting.remove(current_story_id)
        closure.add(current_story_id)

    visit(story_id)
    return closure


def _topological_story_order(
    story_ids: set[int],
    *,
    edges: dict[int, set[int]],
    priority_index: dict[int, tuple[int, int]],
) -> list[int]:
    ordered_ids: list[int] = []
    visited_ids: set[int] = set()
    visiting_ids: set[int] = set()

    def sort_key(candidate_story_id: int) -> tuple[int, int]:
        return priority_index.get(candidate_story_id, (0, candidate_story_id))

    def visit(current_story_id: int) -> None:
        if current_story_id in visiting_ids:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_DEPENDENCY_CYCLE",
                message="Sprint selection dependency graph contains a cycle.",
                details={"story_id": current_story_id},
            )
        if current_story_id in visited_ids:
            return
        visiting_ids.add(current_story_id)
        for prerequisite_id in sorted(edges.get(current_story_id, set()), key=sort_key):
            if prerequisite_id in story_ids:
                visit(prerequisite_id)
        visiting_ids.remove(current_story_id)
        visited_ids.add(current_story_id)
        ordered_ids.append(current_story_id)

    for story_id in sorted(story_ids, key=sort_key):
        visit(story_id)

    return ordered_ids


def _edge_payloads(
    edges: dict[int, set[int]],
    selected_ids: list[int],
) -> list[dict[str, int]]:
    selected_id_set = set(selected_ids)
    selected_index = {story_id: index for index, story_id in enumerate(selected_ids)}
    payloads: list[dict[str, int]] = []
    for dependent_id in sorted(
        selected_id_set,
        key=lambda story_id: selected_index[story_id],
    ):
        payloads.extend(
            {
                "dependent_story_id": dependent_id,
                "prerequisite_story_id": prerequisite_id,
            }
            for prerequisite_id in sorted(
                edges.get(dependent_id, set()),
                key=lambda story_id: selected_index.get(story_id, len(selected_ids)),
            )
            if prerequisite_id in selected_id_set
        )
    return payloads


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
    dependency_metadata: _ResultDependencyMetadata | None = None,
) -> SprintSelectionResult:
    dependency_metadata = dependency_metadata or _ResultDependencyMetadata()
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
        warnings=dependency_metadata.warnings,
        dependency_edges=dependency_metadata.edges,
        dependency_promoted_story_ids=dependency_metadata.promoted_story_ids,
    )
