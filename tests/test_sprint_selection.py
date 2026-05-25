"""Tests for pure Sprint selection policy helpers."""

from __future__ import annotations

import pytest

from services.sprint_selection import (
    SprintSelectionError,
    derive_group_slot,
    derive_parent_group,
    select_sprint_story_rows,
)

EXPECTED_PARENT_GROUP = 10
EXPECTED_GROUP_SLOT = 2
EXPECTED_CAPACITY_POINTS_USED = 4
EXPECTED_MANUAL_POINTS_USED = 3
EXPECTED_DEPENDENCY_POINTS_USED = 4
RANK_PRIORITY_BASE = 100
STORY_COUNT_WITH_EXCESS = 9


def _row(story_id: int, priority: int, points: int) -> dict[str, object]:
    return {
        "story_id": story_id,
        "story_title": f"Story {story_id}",
        "priority": priority,
        "story_points": points,
    }


def test_derive_priority_group_metadata_from_rank_priority() -> None:
    """Verify rank-style priority values expose parent group and child slot."""
    assert derive_parent_group(101) == 1
    assert derive_group_slot(101) == 1
    assert derive_parent_group(1002) == EXPECTED_PARENT_GROUP
    assert derive_group_slot(1002) == EXPECTED_GROUP_SLOT


def test_auto_selection_uses_priority_prefix_and_capacity() -> None:
    """Verify auto mode selects the priority prefix within explicit capacity."""
    rows = [_row(66, 101, 1), _row(85, 102, 3), _row(67, 201, 3)]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=4,
        selected_story_ids=[],
    )

    assert [row["story_id"] for row in result.selected_rows] == [66, 85]
    assert result.mode == "auto"
    assert result.story_points_used == EXPECTED_CAPACITY_POINTS_USED
    assert result.excluded_story_ids == [67]


@pytest.mark.parametrize(
    ("velocity", "expected_count"),
    [("Low", 3), ("Medium", 5), ("High", 7)],
)
def test_auto_selection_applies_velocity_story_limit(
    velocity: str,
    expected_count: int,
) -> None:
    """Verify auto mode bounds selected story count by velocity assumption."""
    rows = [
        _row(story_id, RANK_PRIORITY_BASE + story_id, 1)
        for story_id in range(1, STORY_COUNT_WITH_EXCESS)
    ]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption=velocity,
        max_story_points=20,
        selected_story_ids=[],
    )

    assert result.selected_story_ids == list(range(1, expected_count + 1))
    assert result.story_limit == expected_count
    assert result.excluded_story_ids == list(
        range(expected_count + 1, STORY_COUNT_WITH_EXCESS)
    )


def test_auto_selection_stops_instead_of_skipping_over_capacity_story() -> None:
    """Verify auto mode stops at an over-capacity story after selecting a prefix."""
    rows = [_row(1, 101, 2), _row(2, 102, 5), _row(3, 103, 1)]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="High",
        max_story_points=3,
        selected_story_ids=[],
    )

    assert [row["story_id"] for row in result.selected_rows] == [1]
    assert result.excluded_story_ids == [2, 3]


def test_auto_selection_blocks_when_first_story_exceeds_explicit_capacity() -> None:
    """Verify auto mode hard-blocks when the first story exceeds capacity."""
    rows = [_row(1, 101, 5), _row(2, 102, 1)]

    with pytest.raises(SprintSelectionError) as exc_info:
        select_sprint_story_rows(
            rows,
            team_velocity_assumption="Low",
            max_story_points=3,
            selected_story_ids=[],
        )

    assert exc_info.value.code == "SPRINT_SELECTION_CAPACITY_BLOCKED"
    assert exc_info.value.details["blocking_story_id"] == 1


def test_manual_selection_preserves_explicit_story_order() -> None:
    """Verify manual mode preserves the selected_story_ids order."""
    rows = [_row(1, 101, 2), _row(2, 102, 3), _row(3, 201, 1)]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=3,
        selected_story_ids=[3, 1],
    )

    assert [row["story_id"] for row in result.selected_rows] == [3, 1]
    assert result.mode == "manual"
    assert result.story_points_used == EXPECTED_MANUAL_POINTS_USED


def test_manual_selection_raises_structured_error_for_missing_story_id() -> None:
    """Verify manual mode reports invalid selected_story_ids with details."""
    rows = [_row(1, 101, 2), _row(2, 102, 3)]

    with pytest.raises(SprintSelectionError) as exc_info:
        select_sprint_story_rows(
            rows,
            team_velocity_assumption="Medium",
            max_story_points=5,
            selected_story_ids=[2, 9],
        )

    assert exc_info.value.code == "SPRINT_SELECTION_INVALID"
    assert exc_info.value.details["invalid_selected_ids"] == [9]


def _dep_row(
    story_id: int,
    priority: int,
    points: int,
    *,
    blocked_by: list[object] | None = None,
) -> dict[str, object]:
    return {
        "story_id": story_id,
        "story_title": f"Story {story_id}",
        "priority": priority,
        "story_points": points,
        "blocked_by_story_ids": blocked_by or [],
        "prerequisite_story_ids": blocked_by or [],
        "dependency_status": "blocked" if blocked_by else "ready",
    }


def test_auto_selection_promotes_prerequisite_before_dependent() -> None:
    """Verify auto mode promotes candidate prerequisites ahead of dependents."""
    rows = [
        _dep_row(85, 101, 3, blocked_by=[66]),
        _dep_row(66, 201, 1),
        _dep_row(79, 301, 2),
    ]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=4,
        selected_story_ids=[],
    )

    assert result.selected_story_ids == [66, 85]
    assert result.story_points_used == EXPECTED_DEPENDENCY_POINTS_USED
    assert result.dependency_promoted_story_ids == [66]
    assert result.dependency_closed is True


def test_auto_selection_promotes_transitive_prerequisites() -> None:
    """Verify auto mode promotes the full transitive prerequisite chain."""
    rows = [
        _dep_row(30, 101, 2, blocked_by=[20]),
        _dep_row(20, 201, 2, blocked_by=[10]),
        _dep_row(10, 301, 1),
    ]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=5,
        selected_story_ids=[],
    )

    assert result.selected_story_ids == [10, 20, 30]
    assert result.dependency_promoted_story_ids == [10, 20]


def test_auto_selection_ignores_unparseable_candidate_prerequisite_ids() -> None:
    """Verify malformed prerequisite IDs are ignored instead of crashing."""
    rows = [
        _dep_row(85, 101, 3, blocked_by=["not-an-id", None, 66]),
        _dep_row(66, 201, 1),
    ]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=4,
        selected_story_ids=[],
    )

    assert result.selected_story_ids == [66, 85]
    assert result.dependency_promoted_story_ids == [66]
