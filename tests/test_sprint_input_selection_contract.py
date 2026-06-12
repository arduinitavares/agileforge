"""Regression tests for Sprint input and selection integration."""

from __future__ import annotations

from services import sprint_input


def test_prepare_sprint_input_uses_selector_without_velocity_story_limit() -> None:
    """Sprint input should call the capacity-only selector contract."""
    expected_capacity_points = 2

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 101,
                    "story_title": "First tiny story",
                    "priority": 1,
                    "story_points": 1,
                },
                {
                    "story_id": 102,
                    "story_title": "Second tiny story",
                    "priority": 2,
                    "story_points": 1,
                },
            ],
        }

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        capacity_points=expected_capacity_points,
        capacity_source="user_override",
        capacity_basis="2 points selected for the integration regression",
        max_story_points=expected_capacity_points,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fake_fetch_sprint_candidates,
    )

    assert prepared["success"] is True
    assert prepared["selected_story_ids"] == [101, 102]
    assert (
        prepared["selection_policy"]["story_points_used"] == expected_capacity_points
    )
    assert prepared["selection_policy"]["capacity_points"] == expected_capacity_points
    assert "story_limit" not in prepared["selection_policy"]
