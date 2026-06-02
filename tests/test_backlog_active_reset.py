"""Tests for active backlog reset DB mutation."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from models.core import Product, Sprint, SprintStory, Task, Team, UserStory
from models.enums import SprintStatus, StoryStatus, TaskStatus, WorkflowEventType
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from services.agent_workbench.backlog_active_reset import (
    ActiveBacklogResetError,
    ActiveBacklogResetRequest,
    ActiveBacklogResetReusedKeyError,
    reset_active_backlog_rows,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

ARCHIVED_STORY_COUNT = 2
CREATED_STORY_COUNT = 2
STORY_COUNT_AFTER_IDEMPOTENT_REPLAY = 3


def _engine() -> Engine:
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


def _approved_item(
    priority: int,
    requirement: str,
    *,
    estimated_effort: str = "M",
) -> dict[str, Any]:
    return {
        "priority": priority,
        "requirement": requirement,
        "authority_ref": f"REQ.{priority}",
        "capability_hint": requirement,
        "value_driver": "Strategic",
        "justification": f"Reviewed item {priority}",
        "estimated_effort": estimated_effort,
        "technical_note": "Imported from approved refined backlog.",
        "item_id": f"host-item-{priority}",
        "item_fingerprint": f"sha256:item-{priority}",
        "as_built_annotation": {
            "schema_version": "agileforge.brownfield_annotation.v1"
        },
    }


def _request(**overrides: object) -> ActiveBacklogResetRequest:
    payload: dict[str, object] = {
        "project_id": 1,
        "attempt_id": "backlog-attempt-12",
        "expected_artifact_fingerprint": "sha256:artifact",
        "expected_state": "BACKLOG_REVIEW",
        "reset_reason": "pre-brownfield backlog reset",
        "archive_all_active_stories": True,
        "idempotency_key": "reset-active-1",
        "approved_artifact_fingerprint": "sha256:approved-artifact",
        "artifact": {
            "is_complete": True,
            "clarifying_questions": [],
            "backlog_items": [
                _approved_item(
                    1,
                    "Validate captain-aware contract",
                    estimated_effort="S",
                ),
                _approved_item(
                    2,
                    "Build post-round review artifact",
                    estimated_effort="XL",
                ),
            ],
            "backlog_intake_items": [
                _approved_item(3, "Discover authority gap"),
            ],
        },
        "now": datetime(2026, 6, 2, 12, tzinfo=UTC),
    }
    payload.update(overrides)
    return ActiveBacklogResetRequest.model_validate(payload)


def _seed_active_story(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(Product(name="Cartola"))
        session.commit()
        session.add(
            UserStory(
                product_id=1,
                title="Old story",
                status=StoryStatus.TO_DO,
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.commit()


def _assert_reset_left_story_untouched(engine: Engine) -> None:
    with Session(engine) as session:
        story = session.exec(select(UserStory)).one()
        assert story.is_superseded is False
        assert story.archived_reason is None
        assert story.archived_at is None
        assert story.archived_by is None
        assert story.archive_reset_attempt_id is None
        assert story.archive_previous_status is None
        assert session.exec(select(WorkflowEvent)).all() == []


def test_reset_active_archives_all_active_rows_and_preserves_history() -> None:  # noqa: PLR0915
    """Reset soft-archives active rows and leaves historical links queryable."""
    engine = _engine()
    with Session(engine) as session:
        product = Product(name="Cartola")
        team = Team(name="Delivery Team")
        session.add(product)
        session.add(team)
        session.commit()
        session.refresh(product)
        session.refresh(team)
        assert product.product_id == 1
        assert team.team_id is not None

        old_done = UserStory(
            product_id=1,
            title="Old delivered story",
            status=StoryStatus.DONE,
            story_origin="backlog_seed",
            is_refined=True,
            is_superseded=False,
            acceptance_criteria="- Done",
            completed_at=datetime(2026, 5, 28, 12, tzinfo=UTC),
        )
        old_todo = UserStory(
            product_id=1,
            title="Old residue story",
            status=StoryStatus.TO_DO,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        session.add(old_done)
        session.add(old_todo)
        session.commit()
        session.refresh(old_done)
        session.refresh(old_todo)
        assert old_done.story_id is not None

        sprint = Sprint(
            product_id=1,
            team_id=team.team_id,
            goal="First sprint",
            start_date=date(2026, 5, 25),
            end_date=date(2026, 5, 29),
            status=SprintStatus.COMPLETED,
            close_snapshot_json='{"closed": true}',
        )
        session.add(sprint)
        session.commit()
        session.refresh(sprint)
        assert sprint.sprint_id is not None
        session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=old_done.story_id))
        task = Task(
            story_id=old_done.story_id,
            description="Implement old delivered story",
            status=TaskStatus.DONE,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        assert task.task_id is not None
        session.add(
            StoryCompletionLog(
                story_id=old_done.story_id,
                old_status=StoryStatus.IN_PROGRESS,
                new_status=StoryStatus.DONE,
                changed_by="po",
            )
        )
        session.add(
            TaskExecutionLog(
                task_id=task.task_id,
                sprint_id=sprint.sprint_id,
                old_status=TaskStatus.IN_PROGRESS,
                new_status=TaskStatus.DONE,
                changed_by="agent",
            )
        )
        session.commit()

    result = reset_active_backlog_rows(engine, _request())

    assert result["success"] is True
    assert result["idempotent_replay"] is False
    assert result["archived_count"] == ARCHIVED_STORY_COUNT
    assert result["created_count"] == CREATED_STORY_COUNT
    with Session(engine) as session:
        rows = session.exec(
            select(UserStory)
            .where(UserStory.product_id == 1)
            .order_by(cast("Any", UserStory.story_id))
        ).all()
        archived = [
            row for row in rows if row.archived_reason == "active_backlog_reset"
        ]
        active = [row for row in rows if not row.is_superseded]
        assert [row.title for row in active] == [
            "Validate captain-aware contract",
            "Build post-round review artifact",
        ]
        assert {row.title for row in archived} == {
            "Old delivered story",
            "Old residue story",
        }
        done = next(row for row in archived if row.title == "Old delivered story")
        assert done.status == StoryStatus.DONE
        assert done.archive_previous_status == StoryStatus.DONE.value
        assert done.archive_reset_attempt_id == "backlog-attempt-12"
        assert done.archived_by == "po"
        expected_archived_at = datetime(2026, 6, 2, 12, tzinfo=UTC).replace(
            tzinfo=None
        )
        assert done.archived_at == expected_archived_at
        assert done.superseded_by_story_id is None
        assert session.exec(select(SprintStory)).first() is not None
        sprint = session.exec(select(Sprint)).one()
        assert sprint.close_snapshot_json == '{"closed": true}'
        assert session.exec(select(StoryCompletionLog)).first() is not None
        assert session.exec(select(Task)).first() is not None
        assert session.exec(select(TaskExecutionLog)).first() is not None
        event = session.exec(select(WorkflowEvent)).one()
        assert event.event_type == WorkflowEventType.BACKLOG_SAVED
        metadata = json.loads(event.event_metadata or "{}")
        assert metadata == {
            "action": "active_backlog_reset",
            "project_id": 1,
            "attempt_id": "backlog-attempt-12",
            "artifact_fingerprint": "sha256:artifact",
            "approved_artifact_fingerprint": "sha256:approved-artifact",
            "reset_reason": "pre-brownfield backlog reset",
            "archived_story_ids": [1, 2],
            "created_story_ids": [3, 4],
            "archived_count": 2,
            "created_count": 2,
            "idempotency_key": "reset-active-1",
            "request_fingerprint": metadata["request_fingerprint"],
        }
        assert metadata["request_fingerprint"].startswith("sha256:")


def test_reset_active_creates_seed_rows_from_host_stripped_artifact() -> None:
    """New active rows come from BacklogItem-compatible projected artifact data."""
    engine = _engine()
    with Session(engine) as session:
        session.add(Product(name="Cartola"))
        session.commit()
        session.add(
            UserStory(
                product_id=1,
                title="Old story",
                status=StoryStatus.TO_DO,
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.commit()

    result = reset_active_backlog_rows(engine, _request(archived_by="system-po"))

    assert result["success"] is True
    with Session(engine) as session:
        active = session.exec(
            select(UserStory)
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(cast("Any", UserStory.story_id))
        ).all()
        assert [story.story_origin for story in active] == [
            "backlog_seed",
            "backlog_seed",
        ]
        assert [story.is_refined for story in active] == [False, False]
        assert [story.rank for story in active] == ["1", "2"]
        assert [story.story_points for story in active] == [1, 8]
        assert [story.story_description for story in active] == [
            "Reviewed item 1",
            "Reviewed item 2",
        ]
        assert [story.source_requirement for story in active] == [
            "validate captain-aware contract",
            "build post-round review artifact",
        ]
        assert [story.refinement_slot for story in active] == [1, 2]
        archived = session.exec(
            select(UserStory).where(UserStory.archived_reason == "active_backlog_reset")
        ).one()
        assert archived.archived_by == "system-po"


def test_reset_active_rejects_empty_projected_backlog_before_mutation() -> None:
    """Empty savable projections fail before archive fields or events are written."""
    engine = _engine()
    _seed_active_story(engine)

    with pytest.raises(ActiveBacklogResetError, match="RESET_BACKLOG_ITEMS_EMPTY"):
        reset_active_backlog_rows(
            engine,
            _request(
                artifact={
                    "is_complete": True,
                    "clarifying_questions": [],
                    "backlog_items": [],
                }
            ),
        )

    _assert_reset_left_story_untouched(engine)


def test_reset_active_rejects_invalid_projected_item_before_mutation() -> None:
    """Invalid BacklogItem projection fails before archive fields are written."""
    engine = _engine()
    _seed_active_story(engine)

    invalid_item = _approved_item(1, "Invalid projected item")
    invalid_item["estimated_effort"] = "banana"

    with pytest.raises(ValidationError):
        reset_active_backlog_rows(
            engine,
            _request(
                artifact={
                    "is_complete": True,
                    "clarifying_questions": [],
                    "backlog_items": [invalid_item],
                }
            ),
        )

    _assert_reset_left_story_untouched(engine)


def test_reset_active_idempotency_replays_same_request() -> None:
    """Same key and same fingerprint replays without duplicate rows."""
    engine = _engine()
    with Session(engine) as session:
        session.add(Product(name="Cartola"))
        session.commit()
        session.add(
            UserStory(
                product_id=1,
                title="Old story",
                status=StoryStatus.TO_DO,
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.commit()

    request = _request()
    first = reset_active_backlog_rows(engine, request)
    second = reset_active_backlog_rows(engine, request)

    assert first["success"] is True
    assert second["success"] is True
    assert second["idempotent_replay"] is True
    with Session(engine) as session:
        rows = session.exec(select(UserStory)).all()
        events = session.exec(select(WorkflowEvent)).all()
        assert len(rows) == STORY_COUNT_AFTER_IDEMPOTENT_REPLAY
        assert len(events) == 1


def test_reset_active_idempotency_conflict_fails() -> None:
    """Same key with different request fingerprint is rejected."""
    engine = _engine()
    with Session(engine) as session:
        session.add(Product(name="Cartola"))
        session.commit()
        session.add(
            UserStory(
                product_id=1,
                title="Old story",
                status=StoryStatus.TO_DO,
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.commit()

    reset_active_backlog_rows(engine, _request())
    with pytest.raises(ActiveBacklogResetReusedKeyError):
        reset_active_backlog_rows(
            engine,
            _request(reset_reason="different PO reset reason"),
        )
