"""Regression tests for Sprint input and selection integration."""

from __future__ import annotations

from services import sprint_input


def test_prepare_sprint_input_uses_selector_without_velocity_story_limit() -> None:
    """Sprint input should call the capacity-only selector contract."""

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
        team_velocity_assumption="High",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=2,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fake_fetch_sprint_candidates,
    )

    assert prepared["success"] is True
    assert prepared["selected_story_ids"] == [101, 102]
    assert prepared["selection_policy"]["story_points_used"] == 2
    assert "team_velocity_assumption" not in prepared["selection_policy"]
    assert "story_limit" not in prepared["selection_policy"]
