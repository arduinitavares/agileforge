# ruff: noqa: PLR2004
"""CLI transport coverage for the explicit Sprint retry lifecycle."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import cli.main as cli_main
from tests.adapters.sprint_retry_fixtures import (
    durable_rows,
    event_row_count,
    preserves_existing_rows,
    receipt_row_count,
    retry_row_count,
    retry_transport_fixture,
)

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine


def test_cli_retry_preview_and_apply_use_the_exact_preview_binding(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing retry commands would fail this preview/apply/replay domain contract."""
    fixture = retry_transport_fixture(engine)
    source = fixture.source
    before = durable_rows(engine)

    preview_exit = cli_main.main(
        [
            "sprint",
            "retry-preview",
            "--project-id",
            str(source.project_id),
            "--sprint-id",
            str(source.source_sprint_id),
        ],
        application=fixture.application,
    )

    assert preview_exit == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ok"] is True
    data = preview["data"]
    assert data["project_id"] == source.project_id
    assert data["sprint_id"] == source.source_sprint_id
    assert data["story_ids"] == [source.first_story_id, source.second_story_id]
    assert data["task_ids"] == [source.first_task_id, source.second_task_id]
    assert data["blockers"] == []
    assert isinstance(data["expected_state_fingerprint"], str)
    assert durable_rows(engine) == before

    command = [
        "sprint",
        "retry",
        "--project-id",
        str(source.project_id),
        "--sprint-id",
        str(source.source_sprint_id),
        "--confirm",
        "--expected-state-fingerprint",
        data["expected_state_fingerprint"],
        "--rationale",
        "Fresh evidence is required for this retry.",
        "--actor",
        "owner@example.com",
        "--idempotency-key",
        "cli-retry-transport",
    ]

    missing_confirmation = [item for item in command if item != "--confirm"]
    invalid_rationale = [
        " " if item == "Fresh evidence is required for this retry." else item
        for item in command
    ]
    invalid_actor = [" " if item == "owner@example.com" else item for item in command]
    for invalid_command in (
        missing_confirmation,
        invalid_rationale,
        invalid_actor,
    ):
        invalid_exit = cli_main.main(invalid_command, application=fixture.application)
        assert invalid_exit == 2
        assert json.loads(capsys.readouterr().out)["ok"] is False
        assert durable_rows(engine) == before

    applied_exit = cli_main.main(command, application=fixture.application)

    assert applied_exit == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] is True
    assert applied["output"]["status"] == "Planned"
    retry_attempt_id = applied["output"]["retry_attempt_id"]
    assert isinstance(retry_attempt_id, int)
    after_apply = durable_rows(engine)
    assert preserves_existing_rows(before, after_apply)
    assert retry_row_count(after_apply, "sprint_retry_attempts") == 1
    assert retry_row_count(after_apply, "sprint_retry_story_states") == 2
    assert retry_row_count(after_apply, "sprint_retry_task_states") == 2
    assert event_row_count(after_apply) == event_row_count(before) + 1
    assert receipt_row_count(after_apply) == receipt_row_count(before) + 1

    replay_exit = cli_main.main(command, application=fixture.application)

    assert replay_exit == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["replayed"] is True
    assert {key: value for key, value in replayed.items() if key != "replayed"} == {
        key: value for key, value in applied.items() if key != "replayed"
    }
    assert durable_rows(engine) == after_apply


def test_cli_start_binds_one_exact_planned_retry(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The existing start command must turn the supplied retry binding Active."""
    fixture = retry_transport_fixture(engine)
    source = fixture.source
    preview_exit = cli_main.main(
        [
            "sprint",
            "retry-preview",
            "--project-id",
            str(source.project_id),
            "--sprint-id",
            str(source.source_sprint_id),
        ],
        application=fixture.application,
    )
    assert preview_exit == 0
    fingerprint = json.loads(capsys.readouterr().out)["data"][
        "expected_state_fingerprint"
    ]
    retry_exit = cli_main.main(
        [
            "sprint",
            "retry",
            "--project-id",
            str(source.project_id),
            "--sprint-id",
            str(source.source_sprint_id),
            "--confirm",
            "--expected-state-fingerprint",
            fingerprint,
            "--rationale",
            "Fresh evidence is required for this retry.",
            "--idempotency-key",
            "cli-retry-start",
            "--actor",
            "owner@example.com",
        ],
        application=fixture.application,
    )
    assert retry_exit == 0
    retry_attempt_id = json.loads(capsys.readouterr().out)["output"]["retry_attempt_id"]

    start_prefix = [
        "sprint",
        "start",
        "--project-id",
        str(source.project_id),
        "--instance-key",
    ]
    before_start = durable_rows(engine)
    blank_actor_exit = cli_main.main(
        [
            *start_prefix,
            f"retry:{retry_attempt_id}:sprint:{source.source_sprint_id}",
            "--idempotency-key",
            "cli-retry-start-blank-actor",
            "--actor",
            "  ",
        ],
        application=fixture.application,
    )
    assert blank_actor_exit == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert durable_rows(engine) == before_start
    for rejected_instance_key, idempotency_key in (
        (f"retry:{retry_attempt_id + 99}:sprint:{source.source_sprint_id}", "unknown"),
        (f"retry:{retry_attempt_id}:sprint:{source.source_sprint_id + 99}", "wrong"),
        ("retry:0:sprint:0", "malformed"),
    ):
        rejected_exit = cli_main.main(
            [
                *start_prefix,
                rejected_instance_key,
                "--idempotency-key",
                f"cli-retry-start-{idempotency_key}",
                "--actor",
                "owner@example.com",
            ],
            application=fixture.application,
        )
        assert rejected_exit == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False
        assert durable_rows(engine) == before_start

    start_command = [
        *start_prefix,
        f"retry:{retry_attempt_id}:sprint:{source.source_sprint_id}",
        "--idempotency-key",
        "cli-retry-start-binding",
        "--actor",
        "owner@example.com",
    ]
    start_exit = cli_main.main(start_command, application=fixture.application)

    assert start_exit == 0
    started = json.loads(capsys.readouterr().out)
    assert started["output"] == {
        "retry_attempt_id": retry_attempt_id,
        "status": "Active",
    }
    after_start = durable_rows(engine)

    replay_exit = cli_main.main(start_command, application=fixture.application)
    assert replay_exit == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["replayed"] is True
    assert {key: value for key, value in replayed.items() if key != "replayed"} == {
        key: value for key, value in started.items() if key != "replayed"
    }
    assert durable_rows(engine) == after_start

    stale_exit = cli_main.main(
        [
            *start_prefix,
            f"retry:{retry_attempt_id}:sprint:{source.source_sprint_id}",
            "--idempotency-key",
            "cli-retry-start-stale",
            "--actor",
            "owner@example.com",
        ],
        application=fixture.application,
    )
    assert stale_exit == 1
    assert durable_rows(engine) == after_start
