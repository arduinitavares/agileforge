"""Tests for task execution service."""

from types import SimpleNamespace

import pytest

from agile_sqlmodel import TaskAcceptanceResult, TaskStatus
from models.core import Task
from services.task_execution_service import (
    TaskExecutionServiceError,
    _completion_metadata,
    get_task_execution_history,
    record_task_execution,
)
from utils.task_metadata import TaskMetadata, serialize_task_metadata


def test_get_task_execution_history_skips_logs_without_primary_key() -> None:
    """Verify get task execution history skips logs without primary key."""
    task = SimpleNamespace(task_id=7, story_id=11, status=TaskStatus.TO_DO)
    sprint = SimpleNamespace(sprint_id=3, project_id=2)
    sprint_story = SimpleNamespace(sprint_id=3, story_id=11)
    malformed_log = SimpleNamespace(
        log_id=None,
        task_id=7,
        sprint_id=3,
        old_status="To Do",
        new_status="In Progress",
        outcome_summary=None,
        artifact_refs_json=None,
        acceptance_result="not_checked",
        notes="broken row",
        changed_by="tester",
        changed_at="2026-03-31T00:00:00Z",
    )

    payload = get_task_execution_history(
        project_id=2,
        sprint_id=3,
        task_id=7,
        load_task=lambda: task,
        load_sprint=lambda: sprint,
        load_sprint_story=lambda current_task: sprint_story,  # noqa: ARG005
        load_logs=lambda: [malformed_log],
    )

    assert payload["success"] is True
    assert payload["history"] == []
    assert payload["latest_entry"] is None


def test_get_task_execution_history_rejects_cross_project_sprint() -> None:
    """Verify get task execution history rejects cross project sprint."""
    task = SimpleNamespace(task_id=7, story_id=11, status=TaskStatus.TO_DO)
    sprint = SimpleNamespace(sprint_id=3, project_id=99)

    with pytest.raises(TaskExecutionServiceError) as exc_info:
        get_task_execution_history(
            project_id=2,
            sprint_id=3,
            task_id=7,
            load_task=lambda: task,
            load_sprint=lambda: sprint,
            load_sprint_story=lambda current_task: None,  # noqa: ARG005
            load_logs=list,
        )

    assert exc_info.value.status_code == 404  # noqa: PLR2004
    assert exc_info.value.detail == "Sprint not found in this project"


def test_record_task_execution_rejects_non_executable_tasks() -> None:
    """Verify record task execution rejects non executable tasks."""
    task = SimpleNamespace(
        task_id=7,
        story_id=11,
        status=TaskStatus.TO_DO,
        metadata_json='{"checklist_items":[]}',
    )
    sprint = SimpleNamespace(sprint_id=3, project_id=2)
    sprint_story = SimpleNamespace(sprint_id=3, story_id=11)

    with pytest.raises(TaskExecutionServiceError) as exc_info:
        record_task_execution(
            project_id=2,
            sprint_id=3,
            task_id=7,
            new_status=TaskStatus.IN_PROGRESS,
            outcome_summary=None,
            artifact_refs=None,
            notes="Starting work now.",
            acceptance_result=None,
            changed_by=None,
            load_task=lambda: task,
            load_sprint=lambda: sprint,
            load_sprint_story=lambda current_task: sprint_story,  # noqa: ARG005
            load_logs=list,
            parse_task_metadata=lambda _raw: SimpleNamespace(checklist_items=[]),
            persist_execution_log=lambda *args, **kwargs: None,  # noqa: ARG005
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == "Task has no executable checklist items."


def test_record_task_execution_normalizes_artifact_refs_and_returns_history() -> None:
    """Verify record task execution normalizes artifact refs and returns history."""
    task = SimpleNamespace(
        task_id=7,
        story_id=11,
        status=TaskStatus.TO_DO,
        metadata_json='{"checklist_items":["step"]}',
    )
    sprint = SimpleNamespace(sprint_id=3, project_id=2)
    sprint_story = SimpleNamespace(sprint_id=3, story_id=11)
    persisted: dict[str, object] = {}

    log_entry = SimpleNamespace(
        log_id=5,
        task_id=7,
        sprint_id=3,
        old_status=TaskStatus.TO_DO,
        new_status=TaskStatus.DONE,
        outcome_summary="Finished mock up.",
        artifact_refs_json='["file1.txt","file2.txt"]',
        acceptance_result=TaskAcceptanceResult.FULLY_MET,
        notes="done",
        changed_by="manual-ui",
        changed_at="2026-03-31T00:00:00Z",
    )

    payload = record_task_execution(
        project_id=2,
        sprint_id=3,
        task_id=7,
        new_status=TaskStatus.DONE,
        outcome_summary="Finished mock up.",
        artifact_refs=[" file1.txt ", "file2.txt", "file1.txt", ""],
        notes="done",
        acceptance_result=TaskAcceptanceResult.FULLY_MET,
        changed_by=None,
        load_task=lambda: task,
        load_sprint=lambda: sprint,
        load_sprint_story=lambda current_task: sprint_story,  # noqa: ARG005
        load_logs=lambda: [log_entry],
        parse_task_metadata=lambda _raw: SimpleNamespace(checklist_items=["step"]),
        persist_execution_log=lambda **kwargs: persisted.update(kwargs),
    )

    assert task.status == TaskStatus.DONE
    assert persisted["old_status"] == TaskStatus.TO_DO
    assert persisted["new_status"] == TaskStatus.DONE
    assert persisted["artifact_refs_json"] == '["file1.txt", "file2.txt"]'
    assert persisted["changed_by"] == "manual-ui"
    assert payload["current_status"] == TaskStatus.DONE
    assert payload["latest_entry"].artifact_refs == ["file1.txt", "file2.txt"]


def test_completion_coverage_error_preserves_text_and_stored_order() -> None:
    """A coverage rejection lists canonical text without normalizing its spacing."""
    metadata = TaskMetadata(
        spec_version_id=7,
        spec_hash="sha256:" + "a" * 64,
        sprint_plan_stream_id="SPS-" + "b" * 32,
        sprint_plan_artifact_id=11,
        sprint_plan_fingerprint="sha256:" + "c" * 64,
        relevant_spec_item_ids=("REQ.coverage",),
        task_kind="test",
        artifact_targets=(),
        workstream_tags=(),
        checklist_items=("Zulu criterion", "Confirm  A=B=C ✅"),
    )
    task = Task(story_id=11, metadata_json=serialize_task_metadata(metadata))
    with pytest.raises(TaskExecutionServiceError) as rejected:
        _completion_metadata(task, {"Zulu criterion": "met", "Confirm A=B=C ✅": "met"})
    assert rejected.value.status_code == 409  # noqa: PLR2004
    assert rejected.value.detail == (
        "Checklist result must cover every executable checklist item.\n"
        "Valid checklist items:\n1. Zulu criterion\n2. Confirm  A=B=C ✅"
    )
