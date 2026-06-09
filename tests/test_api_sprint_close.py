"""API tests for sprint close readiness and persistence flows."""

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

import api as api_module
from agile_sqlmodel import (
    Sprint,
    SprintStatus,
    StoryStatus,
    Task,
    TaskStatus,
    UserStory,
    WorkflowEvent,
    WorkflowEventType,
)
from tests.test_api_sprint_flow import _build_client, _seed_saved_sprint

HTTP_OK = 200


def test_build_sprint_close_readiness_handles_unsaved_story_ids() -> None:
    """Treat unsaved stories as open work without adding fake unfinished IDs."""
    readiness = api_module._build_sprint_close_readiness(
        [
            UserStory(
                product_id=1,
                title="Draft story",
                status=StoryStatus.TO_DO,
            )
        ]
    )

    assert readiness.completed_story_count == 0
    assert readiness.open_story_count == 1
    assert readiness.unfinished_story_ids == []
    assert readiness.stories[0].story_id == 0


def test_story_task_progress_accepts_task_sequences() -> None:
    """Accept tuple-based task sequences when computing actionable progress."""
    total, done, cancelled, all_done = api_module._story_task_progress(
        (
            Task(
                description="Task 1",
                status=TaskStatus.TO_DO,
                metadata_json='{"checklist_items":["step"]}',
                story_id=1,
            ),
        )
    )

    assert total == 1
    assert done == 0
    assert cancelled == 0
    assert all_done is False


def test_get_sprint_close_returns_guidance_for_non_active_sprint(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return close guidance for planned sprints without allowing completion."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=False,
        created_title="Planned Sprint",
    )

    response = client.get(f"/api/projects/{project_id}/sprints/{sprint_id}/close")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["close_eligible"] is False
    assert payload["ineligible_reason"] == "Only active sprints can be closed."
    assert payload["history_fidelity"] == "derived"


def test_post_sprint_close_persists_snapshot_and_completion_event(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist the close snapshot and emit the sprint-completed workflow event."""
    client, repo, workflow = _build_client(monkeypatch)
    project_id, sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=True,
        created_title="Closable Sprint",
    )

    story = session.exec(
        select(UserStory).where(UserStory.product_id == project_id)
    ).first()
    assert story is not None
    story.status = StoryStatus.DONE
    story.completed_at = datetime.now(UTC)
    session.add(story)
    session.commit()

    response = client.post(
        f"/api/projects/{project_id}/sprints/{sprint_id}/close",
        json={
            "completion_notes": "Closed after review.",
            "follow_up_notes": "Carry remaining backlog forward manually.",
        },
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["current_status"] == SprintStatus.COMPLETED.value
    assert payload["close_eligible"] is False
    assert payload["ineligible_reason"] is None
    assert payload["history_fidelity"] == "snapshotted"
    assert payload["close_snapshot"]["completion_notes"] == "Closed after review."
    assert (
        payload["close_snapshot"]["follow_up_notes"]
        == "Carry remaining backlog forward manually."
    )

    sprint = session.get(Sprint, sprint_id)
    assert sprint is not None
    assert sprint.status == SprintStatus.COMPLETED
    assert sprint.completed_at is not None
    assert sprint.close_snapshot_json is not None

    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_COMPLETED
        )
    ).first()
    assert event is not None
    assert workflow.states[str(project_id)]["fsm_state"] == "SPRINT_COMPLETE"
    assert workflow.states[str(project_id)]["active_sprint_id"] is None
    assert workflow.states[str(project_id)]["latest_completed_sprint_id"] == sprint_id
    assert workflow.states[str(project_id)]["sprint_completed_at"] is not None

    detail_response = client.get(f"/api/projects/{project_id}/sprints/{sprint_id}")
    assert detail_response.status_code == HTTP_OK
    detail_payload = detail_response.json()["data"]["sprint"]
    assert detail_payload["history_fidelity"] == "snapshotted"
    assert (
        detail_payload["close_snapshot"]["completion_notes"] == "Closed after review."
    )
