"""Browser-free regression tests for the lifecycle fake's Goal projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tests.e2e.test_single_project_lifecycle_ui import FakeLifecycle, JsonObject


@pytest.fixture
def lifecycle() -> FakeLifecycle:
    """Build an accepted Vision without any Goal answer or browser fixture."""
    return FakeLifecycle(
        repositories={},
        vision_candidate={"statement": "Give teams a reviewable lifecycle."},
        vision_accepted=True,
    )


@pytest.fixture
def starter_questions() -> JsonObject:
    """Load the single starter fixture consumed by projection and UI tests."""
    fixture_path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "product_goal_starter_questions.json"
    )
    return cast(
        "JsonObject",
        json.loads(fixture_path.read_text(encoding="utf-8")),
    )


def test_initial_goal_projection_exposes_shared_starters_without_a_turn(
    lifecycle: FakeLifecycle,
    starter_questions: JsonObject,
) -> None:
    """Initial questions must be shared starters, never a fabricated follow-up."""
    projection = lifecycle._goal_projection()

    assert projection["effective_questions"] == starter_questions
    assert projection["latest_questions"] == []
    assert projection["transcript"] == []
    assert lifecycle.goal_transcript == []


def test_goal_projection_exposes_last_incomplete_turn_questions(
    lifecycle: FakeLifecycle,
) -> None:
    """The selected last turn supplies the ordered generated follow-ups."""
    lifecycle.goal_transcript = [
        {
            "goal_number": 1,
            "revision_number": 1,
            "turn_number": 1,
            "user_text": "Start with a reviewable pilot.",
            "questions": ["Which team will use the pilot?"],
        },
        {
            "goal_number": 1,
            "revision_number": 1,
            "turn_number": 2,
            "user_text": "Use the operations team.",
            "questions": [
                "What observable result proves the pilot succeeded?",
                "Which boundary keeps the pilot focused?",
            ],
        },
    ]

    projection = lifecycle._goal_projection()

    assert projection["effective_questions"] == {
        "questions": [
            "What observable result proves the pilot succeeded?",
            "Which boundary keeps the pilot focused?",
        ],
        "source": "generated",
    }
    assert projection["latest_questions"] == [
        "What observable result proves the pilot succeeded?",
        "Which boundary keeps the pilot focused?",
    ]
    assert projection["transcript"] == lifecycle.goal_transcript


@pytest.mark.parametrize("state", ["no_vision", "pending_review", "active_goal"])
def test_unavailable_goal_interview_exposes_null_effective_questions(
    lifecycle: FakeLifecycle,
    state: str,
) -> None:
    """No Vision, pending review, and accepted Goals offer no interview prompts."""
    if state == "no_vision":
        lifecycle.vision_accepted = False
    else:
        lifecycle.goal_candidate = {"statement": "Deliver the reviewable pilot."}
        lifecycle.goal_accepted = state == "active_goal"

    projection = lifecycle._goal_projection()

    assert projection["effective_questions"] is None
    assert projection["latest_questions"] == []
    assert projection["transcript"] == []
