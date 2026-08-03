"""API tests for deleting story requirements and resetting story runtime state."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from agile_sqlmodel import (
    Product,
    Sprint,
    SprintStatus,
    SprintStory,
    StoryCompletionLog,
    StoryStatus,
    Task,
    TaskExecutionLog,
    TaskStatus,
    UserStory,
)
from models.core import Team
from services.story_linkage import normalize_requirement_key
from tests import legacy_api as api_module

HTTP_OK = 200
SPRINT_COUNT = 2
STORY_COUNT = 3
STORY_LOGS_PER_STORY = 2
TASKS_PER_STORY = 3
EXPECTED_ATTEMPT_HISTORY_COUNT = 2


@dataclass
class DeleteStoryData:
    """Persisted identifiers used by the delete-story route test."""

    product_id: int
    parent_requirement: str
    story_ids: list[int]
    task_ids: list[int]


def _require_id(value: int | None, label: str) -> int:
    assert value is not None, f"{label} should be persisted before use"
    return value


def _create_product(session: Session) -> Product:
    product = Product(name=f"Test Product {uuid.uuid4()}", description="Test")
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _create_team(session: Session) -> Team:
    team = Team(name=f"Test Team {uuid.uuid4()}")
    session.add(team)
    session.commit()
    session.refresh(team)
    return team


def _create_sprints(
    session: Session,
    *,
    product_id: int,
    team_id: int,
) -> list[Sprint]:
    today = datetime.now(UTC).date()
    sprints = [
        Sprint(
            product_id=product_id,
            team_id=team_id,
            start_date=today,
            end_date=today,
            status=SprintStatus.PLANNED,
        )
        for _ in range(SPRINT_COUNT)
    ]
    session.add_all(sprints)
    session.commit()
    for sprint in sprints:
        session.refresh(sprint)
    return sprints


def _create_stories(
    session: Session,
    *,
    product_id: int,
    parent_requirement: str,
) -> list[UserStory]:
    normalized_parent_requirement = normalize_requirement_key(parent_requirement)
    stories = [
        UserStory(
            product_id=product_id,
            title=f"Test Story {index}",
            source_requirement=normalized_parent_requirement,
            status=StoryStatus.TO_DO,
        )
        for index in range(STORY_COUNT)
    ]
    session.add_all(stories)
    session.commit()
    for story in stories:
        session.refresh(story)
    return stories


def _create_story_supporting_records(
    session: Session,
    *,
    stories: list[UserStory],
    sprints: list[Sprint],
) -> list[Task]:
    sprint_mappings: list[SprintStory] = []
    logs: list[StoryCompletionLog] = []
    tasks: list[Task] = []

    for story in stories:
        story_id = _require_id(story.story_id, "story_id")
        sprint_mappings.extend(
            SprintStory(
                sprint_id=_require_id(sprint.sprint_id, "sprint_id"),
                story_id=story_id,
            )
            for sprint in sprints
        )

        logs.extend(
            StoryCompletionLog(
                story_id=story_id,
                old_status=StoryStatus.TO_DO,
                new_status=StoryStatus.IN_PROGRESS,
            )
            for _ in range(STORY_LOGS_PER_STORY)
        )

        tasks.extend(
            Task(
                story_id=story_id,
                description="Task",
                status=TaskStatus.TO_DO,
            )
            for _ in range(TASKS_PER_STORY)
        )

    session.add_all(sprint_mappings)
    session.add_all(logs)
    session.add_all(tasks)
    session.commit()
    for task in tasks:
        session.refresh(task)
    return tasks


def _create_task_execution_logs(
    session: Session,
    *,
    tasks: list[Task],
    sprint_id: int,
) -> None:
    task_execution_logs = [
        TaskExecutionLog(
            task_id=_require_id(task.task_id, "task_id"),
            sprint_id=sprint_id,
            new_status=TaskStatus.IN_PROGRESS,
        )
        for task in tasks
    ]
    session.add_all(task_execution_logs)
    session.commit()


def _count_stories(session: Session, story_ids: list[int]) -> int:
    return len(
        session.exec(
            select(UserStory).where(col(UserStory.story_id).in_(story_ids))
        ).all()
    )


def _count_sprint_links(session: Session, story_ids: list[int]) -> int:
    return len(
        session.exec(
            select(SprintStory).where(col(SprintStory.story_id).in_(story_ids))
        ).all()
    )


def _count_story_logs(session: Session, story_ids: list[int]) -> int:
    return len(
        session.exec(
            select(StoryCompletionLog).where(
                col(StoryCompletionLog.story_id).in_(story_ids)
            )
        ).all()
    )


def _count_tasks(session: Session, story_ids: list[int]) -> int:
    return len(
        session.exec(select(Task).where(col(Task.story_id).in_(story_ids))).all()
    )


def _count_task_logs(session: Session, task_ids: list[int]) -> int:
    return len(
        session.exec(
            select(TaskExecutionLog).where(col(TaskExecutionLog.task_id).in_(task_ids))
        ).all()
    )


@pytest.fixture
def setup_test_data(session: Session) -> DeleteStoryData:
    """Create story-linked records that can be deleted in a single request."""
    product = _create_product(session)
    team = _create_team(session)
    product_id = _require_id(product.product_id, "product_id")
    team_id = _require_id(team.team_id, "team_id")
    sprints = _create_sprints(session, product_id=product_id, team_id=team_id)
    parent_requirement = f"Test Req {uuid.uuid4()}"
    stories = _create_stories(
        session,
        product_id=product_id,
        parent_requirement=parent_requirement,
    )
    tasks = _create_story_supporting_records(
        session,
        stories=stories,
        sprints=sprints,
    )
    _create_task_execution_logs(
        session,
        tasks=tasks,
        sprint_id=_require_id(sprints[0].sprint_id, "sprint_id"),
    )
    return DeleteStoryData(
        product_id=product_id,
        parent_requirement=parent_requirement,
        story_ids=[_require_id(story.story_id, "story_id") for story in stories],
        task_ids=[_require_id(task.task_id, "task_id") for task in tasks],
    )


@pytest.mark.asyncio
async def test_delete_project_story(
    session: Session,
    setup_test_data: DeleteStoryData,
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
) -> None:
    """Delete all story artifacts for a requirement and reset the runtime state."""
    monkeypatch.setattr(api_module, "get_engine", lambda: engine)
    monkeypatch.setattr(api_module.product_repo, "_get_session", lambda: session)
    monkeypatch.setattr("agile_sqlmodel.get_engine", lambda: engine)

    product_id = setup_test_data.product_id
    parent_requirement = setup_test_data.parent_requirement
    story_ids = setup_test_data.story_ids
    task_ids = setup_test_data.task_ids

    mock_state: dict[str, Any] = {
        "story_saved": {parent_requirement: True},
        "story_outputs": {parent_requirement: {"data": "some artifact"}},
        "story_attempts": {
            parent_requirement: [
                {
                    "created_at": "2026-03-28T10:00:00Z",
                    "trigger": "manual_refine",
                    "input_context": {},
                    "output_artifact": {"data": "some artifact"},
                    "is_complete": True,
                    "failure_artifact_id": None,
                    "failure_stage": None,
                    "failure_summary": None,
                    "raw_output_preview": None,
                    "has_full_artifact": False,
                }
            ]
        },
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "created_at": "2026-03-28T10:00:00Z",
                            "trigger": "manual_refine",
                            "request_snapshot_id": "request-1",
                            "draft_basis_attempt_id": None,
                            "included_feedback_ids": ["feedback-1"],
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "output_artifact": {
                                "data": "some artifact",
                                "is_complete": True,
                            },
                            "failure_artifact_id": None,
                            "failure_stage": None,
                            "failure_summary": None,
                            "raw_output_preview": None,
                            "has_full_artifact": False,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                        "updated_at": "2026-03-28T10:00:00Z",
                    },
                    "feedback_projection": {
                        "items": [
                            {
                                "feedback_id": "feedback-1",
                                "text": "keep it smaller",
                                "created_at": "2026-03-28T09:59:00Z",
                                "status": "absorbed",
                                "absorbed_by_attempt_id": "attempt-1",
                            }
                        ],
                        "next_feedback_sequence": 1,
                    },
                    "request_projection": {
                        "request_snapshot_id": "request-1",
                        "payload": {"parent_requirement": parent_requirement},
                        "request_hash": "hash-1",
                        "created_at": "2026-03-28T10:00:00Z",
                        "draft_basis_attempt_id": None,
                        "included_feedback_ids": ["feedback-1"],
                        "context_version": "story-runtime.v1",
                    },
                }
            }
        },
        "another_req": "should not be touched",
    }
    saved_state_calls: list[dict[str, Any]] = []

    async def mock_ensure_session(_sid: str) -> dict[str, Any]:
        return mock_state

    def mock_save_session_state(_sid: str, state: dict[str, Any]) -> None:
        saved_state_calls.append(state.copy())

    monkeypatch.setattr(api_module, "_ensure_session", mock_ensure_session)
    monkeypatch.setattr(api_module, "_save_session_state", mock_save_session_state)

    assert _count_stories(session, story_ids) == len(story_ids)
    assert _count_sprint_links(session, story_ids) > 0
    assert _count_story_logs(session, story_ids) > 0
    assert _count_tasks(session, story_ids) == len(task_ids)
    assert _count_task_logs(session, task_ids) > 0

    async with AsyncClient(
        transport=httpx.ASGITransport(app=api_module.app),
        base_url="http://test",
    ) as client:
        response = await client.delete(
            f"/api/projects/{product_id}/story",
            params={"parent_requirement": f"  {parent_requirement}  "},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["status"] == "success"
    assert response.json()["parent_requirement"] == parent_requirement
    assert response.json()["data"]["deleted_count"] == len(story_ids)

    assert _count_stories(session, story_ids) == 0
    assert _count_sprint_links(session, story_ids) == 0
    assert _count_story_logs(session, story_ids) == 0
    assert _count_tasks(session, story_ids) == 0
    assert _count_task_logs(session, task_ids) == 0

    assert len(saved_state_calls) == 1
    final_state = saved_state_calls[0]

    assert parent_requirement not in final_state["story_saved"]
    assert parent_requirement not in final_state["story_outputs"]
    assert len(final_state["story_attempts"][parent_requirement]) == 1
    assert (
        final_state["story_attempts"][parent_requirement][0]["trigger"]
        == "manual_refine"
    )

    runtime = final_state["interview_runtime"]["story"][parent_requirement]
    assert runtime["draft_projection"] == {}
    assert runtime["request_projection"] == {}
    assert runtime["feedback_projection"]["items"] == []
    assert len(runtime["attempt_history"]) == EXPECTED_ATTEMPT_HISTORY_COUNT
    reset_attempt = runtime["attempt_history"][-1]
    assert reset_attempt["trigger"] == "reset"
    assert reset_attempt["classification"] == "reset_marker"
    assert "state reset by user" in reset_attempt["summary"]
    assert "another_req" in final_state
