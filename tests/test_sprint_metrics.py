# tests/test_sprint_metrics.py
"""Tests for read-only Sprint metrics projection."""
# ruff: noqa: PLR2004

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

from services.phases import sprint_metrics
from services.phases.sprint_metrics import build_sprint_metrics

JsonDict = dict[str, object]


def _completed_sprint(
    sprint_id: int,
    *,
    completed_at: object,
    story_points_completed: object,
    elapsed_seconds: object,
    **overrides: object,
) -> JsonDict:
    payload: JsonDict = {
        "sprint_id": sprint_id,
        "goal": f"Sprint {sprint_id} goal",
        "status": "Completed",
        "started_at": "2026-06-09T09:00:00Z",
        "completed_at": completed_at,
        "start_date": "2026-06-11",
        "end_date": "2026-06-25",
        "story_count": 2,
        "completed_story_count": 2,
        "task_count": 4,
        "completed_task_count": 4,
        "story_points_planned": story_points_completed,
        "story_points_completed": story_points_completed,
        "elapsed_seconds": elapsed_seconds,
        "workflow_event_count": 1,
        "workflow_event_duration_seconds": elapsed_seconds,
        "turn_count": 5,
        "history_fidelity": "derived",
        "unestimated_completed_story_count": 0,
    }
    payload.update(overrides)
    return payload


def test_no_completed_sprints_returns_insufficient_history() -> None:
    """Verify no-history payload avoids fake recommendation fallback."""
    metrics = build_sprint_metrics(project_id=7, completed_sprints=[])

    assert metrics["project_id"] == 7
    assert metrics["status"] == "insufficient_history"
    assert metrics["completed_sprints"] == []
    assert metrics["summary"]["completed_sprint_count"] == 0
    assert metrics["summary"]["total_elapsed_seconds"] is None
    assert metrics["summary"]["points_per_hour"] is None
    assert metrics["recommendation"]["recommended_next_sprint_points"] is None
    assert metrics["recommendation"]["basis"] == "insufficient_history"
    assert metrics["recommendation"]["source_sprint_ids"] == []
    assert metrics["recommendation"]["source_completed_points"] == []


def test_completed_sprints_aggregate_and_recommend_from_newest_three() -> None:
    """Verify aggregate metrics and newest-three recommendation math."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                19,
                completed_at="2026-06-09T10:00:00Z",
                story_points_completed=10,
                elapsed_seconds=7200,
            ),
            _completed_sprint(
                21,
                completed_at="2026-06-11T10:00:00Z",
                story_points_completed=8,
                elapsed_seconds=1800,
            ),
            _completed_sprint(
                20,
                completed_at="2026-06-10T10:00:00Z",
                story_points_completed=5,
                elapsed_seconds=3600,
            ),
            _completed_sprint(
                22,
                completed_at="2026-06-11T10:00:00Z",
                story_points_completed=9,
                elapsed_seconds=1800,
            ),
        ],
    )

    assert metrics["status"] == "ready"
    assert [row["sprint_id"] for row in metrics["completed_sprints"]] == [
        22,
        21,
        20,
        19,
    ]

    summary = metrics["summary"]
    assert summary["completed_sprint_count"] == 4
    assert summary["completed_story_count"] == 8
    assert summary["completed_task_count"] == 16
    assert summary["completed_story_points"] == 32
    assert summary["total_elapsed_seconds"] == 14400
    assert summary["average_points_per_sprint"] == 8
    assert summary["median_points_per_sprint"] == 8.5
    assert summary["average_elapsed_seconds_per_sprint"] == 3600
    assert summary["points_per_hour"] == 8
    assert summary["sprints_with_elapsed_time_count"] == 4
    assert summary["unestimated_completed_story_count"] == 0

    recommendation = metrics["recommendation"]
    assert recommendation["recommended_next_sprint_points"] == 7
    assert recommendation["basis"] == "last_3_completed_sprints_average"
    assert recommendation["source_sprint_ids"] == [22, 21, 20]
    assert recommendation["source_completed_points"] == [9, 8, 5]
    assert recommendation["sample_size"] == 3


def test_recommendation_uses_nearest_integer_half_up_rounding() -> None:
    """Verify recommendation rounds nearest integer with half-up behavior."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                31,
                completed_at="2026-06-11T11:00:00Z",
                story_points_completed=9,
                elapsed_seconds=3600,
            ),
            _completed_sprint(
                30,
                completed_at="2026-06-10T11:00:00Z",
                story_points_completed=8,
                elapsed_seconds=3600,
            ),
        ],
    )

    assert metrics["recommendation"]["source_completed_points"] == [9, 8]
    assert metrics["recommendation"]["recommended_next_sprint_points"] == 9


def test_recommendation_samples_true_newest_sprints_across_timezones() -> None:
    """Verify sorting uses normalized instants instead of raw timestamp strings."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                50,
                completed_at="2026-06-11T08:00:00+00:00",
                story_points_completed=3,
                elapsed_seconds=3600,
            ),
            _completed_sprint(
                53,
                completed_at="not-a-date",
                story_points_completed=20,
                elapsed_seconds=3600,
            ),
            _completed_sprint(
                51,
                completed_at="2026-06-11T13:00:00Z",
                story_points_completed=5,
                elapsed_seconds=3600,
            ),
            _completed_sprint(
                54,
                completed_at=None,
                story_points_completed=40,
                elapsed_seconds=3600,
            ),
            _completed_sprint(
                52,
                completed_at="2026-06-11T10:30:00-03:00",
                story_points_completed=7,
                elapsed_seconds=3600,
            ),
        ],
    )

    assert [row["sprint_id"] for row in metrics["completed_sprints"]] == [
        52,
        51,
        50,
        54,
        53,
    ]
    assert metrics["recommendation"]["source_sprint_ids"] == [52, 51, 50]
    assert metrics["recommendation"]["source_completed_points"] == [7, 5, 3]
    assert metrics["recommendation"]["recommended_next_sprint_points"] == 5


def test_elapsed_seconds_accepts_mixed_datetime_and_string_inputs() -> None:
    """Verify aware datetimes and timestamp strings normalize consistently."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                61,
                completed_at="2026-06-11T10:30:00+00:00",
                story_points_completed=5,
                elapsed_seconds=None,
                started_at=datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
            )
        ],
    )

    assert metrics["status"] == "ready"
    assert metrics["completed_sprints"][0]["elapsed_seconds"] == 5400
    assert metrics["data_quality_warnings"] == []


def test_non_integral_numeric_values_are_not_truncated() -> None:
    """Verify only integral floats and integral numeric strings become ints."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                71,
                completed_at="2026-06-11T10:00:00Z",
                story_points_completed=7.9,
                elapsed_seconds=3600,
                completed_story_count="2.5",
                completed_task_count="4.0",
                story_points_planned="8.1",
                task_count=4.0,
                workflow_event_count="1.5",
            )
        ],
    )

    row = metrics["completed_sprints"][0]
    assert row["story_points_completed"] == 0
    assert row["story_points_planned"] == 0
    assert row["completed_story_count"] == 0
    assert row["completed_task_count"] == 4
    assert row["task_count"] == 4
    assert row["workflow_event_count"] == 0
    assert metrics["recommendation"]["recommended_next_sprint_points"] == 0


def test_workflow_event_duration_preserves_finite_fractional_values() -> None:
    """Verify workflow duration accepts finite floats without integer coercion."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                75,
                completed_at="2026-06-11T10:00:00Z",
                story_points_completed=5,
                elapsed_seconds=3600,
                workflow_event_duration_seconds=3.756,
            ),
            _completed_sprint(
                74,
                completed_at="2026-06-10T10:00:00Z",
                story_points_completed=5,
                elapsed_seconds=3600,
                workflow_event_duration_seconds="inf",
            ),
        ],
    )

    rows = metrics["completed_sprints"]
    assert rows[0]["workflow_event_duration_seconds"] == 3.76
    assert rows[1]["workflow_event_duration_seconds"] is None


def test_non_finite_elapsed_seconds_are_invalid_and_json_safe() -> None:
    """Verify non-finite elapsed values never appear in metrics output."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                81,
                completed_at="2026-06-11T10:00:00Z",
                story_points_completed=3,
                elapsed_seconds="NaN",
            ),
            _completed_sprint(
                82,
                completed_at="2026-06-11T09:00:00Z",
                story_points_completed=5,
                elapsed_seconds="inf",
            ),
            _completed_sprint(
                83,
                completed_at="2026-06-11T08:00:00Z",
                story_points_completed=8,
                elapsed_seconds="-inf",
            ),
        ],
    )

    assert metrics["status"] == "partial_history"
    assert [
        row["elapsed_seconds"] for row in metrics["completed_sprints"]
    ] == [None, None, None]
    assert metrics["summary"]["total_elapsed_seconds"] is None
    assert metrics["summary"]["average_elapsed_seconds_per_sprint"] is None
    assert metrics["summary"]["points_per_hour"] is None
    assert {
        (warning["code"], warning["sprint_id"])
        for warning in metrics["data_quality_warnings"]
    } == {
        ("SPRINT_ELAPSED_TIME_INVALID", 81),
        ("SPRINT_ELAPSED_TIME_INVALID", 82),
        ("SPRINT_ELAPSED_TIME_INVALID", 83),
    }
    json.dumps(metrics, allow_nan=False)


def test_data_quality_warnings_are_structured() -> None:
    """Verify partial source data produces structured quality warnings."""
    metrics = build_sprint_metrics(
        project_id=3,
        completed_sprints=[
            _completed_sprint(
                41,
                completed_at="2026-06-11T10:00:00Z",
                story_points_completed=3,
                elapsed_seconds=None,
                started_at=None,
            ),
            _completed_sprint(
                42,
                completed_at="2026-06-11T08:00:00Z",
                story_points_completed=5,
                elapsed_seconds=-3600,
                started_at="2026-06-11T09:00:00Z",
            ),
            _completed_sprint(
                43,
                completed_at="2026-06-11T07:00:00Z",
                story_points_completed=8,
                elapsed_seconds=3600,
                workflow_event_count=2,
                turn_count=None,
            ),
            _completed_sprint(
                44,
                completed_at="2026-06-11T06:00:00Z",
                story_points_completed=0,
                elapsed_seconds=3600,
                unestimated_completed_story_count=2,
            ),
        ],
    )

    assert metrics["status"] == "partial_history"
    assert metrics["summary"]["unestimated_completed_story_count"] == 2
    warning_keys = {
        (warning["code"], warning["sprint_id"])
        for warning in metrics["data_quality_warnings"]
    }
    assert warning_keys == {
        ("SPRINT_ELAPSED_TIME_UNAVAILABLE", 41),
        ("SPRINT_ELAPSED_TIME_INVALID", 42),
        ("WORKFLOW_EVENT_TURN_COUNT_UNAVAILABLE", 43),
        ("COMPLETED_STORY_POINTS_MISSING", 44),
    }
    assert all(
        isinstance(warning["message"], str) and warning["message"]
        for warning in metrics["data_quality_warnings"]
    )


def test_token_metrics_use_unavailable_contract() -> None:
    """Verify token metrics expose the explicit unavailable contract."""
    metrics = build_sprint_metrics(project_id=7, completed_sprints=[])

    assert metrics["token_metrics"] == {
        "status": "unavailable",
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "estimated_cost_usd": None,
        "reason": "Token usage is not yet captured in durable AgileForge records.",
    }


def test_sprint_metrics_module_has_no_database_or_provider_imports() -> None:
    """Verify the projection module has no DB, API, or provider imports."""
    module_path = Path(sprint_metrics.__file__)
    module_ast = ast.parse(module_path.read_text())

    imported_modules: set[str] = set()
    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = (
        "agile_sqlmodel",
        "api",
        "db",
        "fastapi",
        "repositories",
        "routers",
        "sqlmodel",
    )
    assert not {
        module_name
        for module_name in imported_modules
        if module_name == "provider" or module_name.startswith(forbidden_prefixes)
    }
