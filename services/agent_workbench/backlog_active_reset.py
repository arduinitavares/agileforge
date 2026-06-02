"""DB mutation for explicit active backlog reset."""

from __future__ import annotations

import json
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from models.core import Sprint, SprintStory, Task, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from orchestrator_agent.agent_tools.backlog_primer.schemes import BacklogItem
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key
from services.agent_workbench.fingerprints import canonical_hash
from services.phases.backlog_refinement import project_savable_backlog_items

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_RESET_NOT_REQUIRED = "RESET_NOT_REQUIRED"
_RESET_IDEMPOTENCY_CONFLICT = "RESET_IDEMPOTENCY_CONFLICT"
_RESET_BACKLOG_ITEMS_EMPTY = "RESET_BACKLOG_ITEMS_EMPTY"
_RESET_HISTORY_PRESERVATION_FAILED = "RESET_HISTORY_PRESERVATION_FAILED"


class ActiveBacklogResetError(RuntimeError):
    """Base reset-active mutation error."""


class ActiveBacklogResetReusedKeyError(ActiveBacklogResetError):
    """Raised when an idempotency key is reused with different inputs."""


class ActiveBacklogResetRequest(BaseModel):
    """Host-validated active backlog reset request."""

    model_config = ConfigDict(extra="forbid")

    project_id: int
    attempt_id: str = Field(min_length=1)
    expected_artifact_fingerprint: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    reset_reason: str = Field(min_length=1)
    archive_all_active_stories: bool
    idempotency_key: str = Field(min_length=1)
    approved_artifact_fingerprint: str = Field(min_length=1)
    artifact: dict[str, Any]
    now: datetime
    archived_by: str = "po"


def reset_request_fingerprint(request: ActiveBacklogResetRequest) -> str:
    """Return deterministic reset request fingerprint stored in event metadata."""
    return canonical_hash(
        {
            "command": "agileforge backlog reset-active",
            "project_id": request.project_id,
            "attempt_id": request.attempt_id,
            "expected_artifact_fingerprint": request.expected_artifact_fingerprint,
            "expected_state": request.expected_state,
            "reset_reason": request.reset_reason,
            "archive_all_active_stories": request.archive_all_active_stories,
            "approved_artifact_fingerprint": request.approved_artifact_fingerprint,
        }
    )


def replay_active_backlog_reset(
    engine: Engine,
    request: ActiveBacklogResetRequest,
) -> dict[str, Any] | None:
    """Return a committed reset-active replay without mutating rows."""
    request_fingerprint = reset_request_fingerprint(request)
    with Session(engine) as session:
        return _reset_replay(
            session,
            project_id=request.project_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_fingerprint,
        )


def reset_active_backlog_rows(
    engine: Engine,
    request: ActiveBacklogResetRequest,
) -> dict[str, Any]:
    """Soft-archive active stories and create seed rows from approved artifact."""
    request_fingerprint = reset_request_fingerprint(request)
    with Session(engine) as session:
        replay = _reset_replay(
            session,
            project_id=request.project_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay

        projected_items = project_savable_backlog_items(request.artifact)
        if not projected_items:
            raise ActiveBacklogResetError(_RESET_BACKLOG_ITEMS_EMPTY)
        validated_items = [
            BacklogItem.model_validate(raw_item) for raw_item in projected_items
        ]

        history_before = _reset_history_snapshot(session)
        active_stories = session.exec(
            select(UserStory)
            .where(UserStory.product_id == request.project_id)
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(cast("Any", UserStory.story_id))
        ).all()
        if not active_stories:
            raise ActiveBacklogResetError(_RESET_NOT_REQUIRED)

        try:
            archived_story_ids: list[int] = []
            for story in active_stories:
                story.is_superseded = True
                story.superseded_by_story_id = None
                story.archived_reason = "active_backlog_reset"
                story.archived_at = request.now
                story.archived_by = request.archived_by
                story.archive_reset_attempt_id = request.attempt_id
                story.archive_previous_status = _story_status_value(story.status)
                if story.story_id is not None:
                    archived_story_ids.append(story.story_id)
                session.add(story)

            created_story_ids: list[int] = []
            for item in validated_items:
                story = _story_from_backlog_item(request.project_id, item)
                session.add(story)
                session.flush()
                if story.story_id is not None:
                    created_story_ids.append(story.story_id)

            metadata = {
                "action": "active_backlog_reset",
                "project_id": request.project_id,
                "attempt_id": request.attempt_id,
                "artifact_fingerprint": request.expected_artifact_fingerprint,
                "approved_artifact_fingerprint": request.approved_artifact_fingerprint,
                "reset_reason": request.reset_reason,
                "archived_story_ids": archived_story_ids,
                "created_story_ids": created_story_ids,
                "archived_count": len(archived_story_ids),
                "created_count": len(created_story_ids),
                "idempotency_key": request.idempotency_key,
                "request_fingerprint": request_fingerprint,
            }
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.BACKLOG_SAVED,
                    product_id=request.project_id,
                    timestamp=request.now,
                    event_metadata=json.dumps(metadata, sort_keys=True),
                )
            )
            session.flush()
            _assert_reset_history_preserved(
                before=history_before,
                after=_reset_history_snapshot(session),
                created_story_count=len(created_story_ids),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        return {"success": True, "idempotent_replay": False, **metadata}


def _story_from_backlog_item(product_id: int, item: BacklogItem) -> UserStory:
    effort = str(item.estimated_effort).strip().upper()
    return UserStory(
        title=item.requirement,
        product_id=product_id,
        status=StoryStatus.TO_DO,
        rank=str(item.priority),
        story_points=_story_points_from_effort(effort),
        story_description=item.justification,
        acceptance_criteria=None,
        source_requirement=normalize_requirement_key(item.requirement),
        refinement_slot=item.priority,
        story_origin="backlog_seed",
        is_refined=False,
        is_superseded=False,
    )


def _story_points_from_effort(effort: str) -> int | None:
    mapped_points = {
        "S": 1,
        "M": 3,
        "L": 5,
        "XL": 8,
        "LOW": 1,
        "MEDIUM": 3,
        "HIGH": 5,
    }.get(effort)
    if mapped_points is not None:
        return mapped_points
    if effort.isdigit():
        return int(effort)
    return None


def _reset_replay(
    session: Session,
    *,
    project_id: int,
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.product_id == project_id)
        .where(WorkflowEvent.event_type == WorkflowEventType.BACKLOG_SAVED)
    ).all()
    for event in events:
        metadata = _json_object(event.event_metadata)
        if metadata.get("action") != "active_backlog_reset":
            continue
        if metadata.get("idempotency_key") != idempotency_key:
            continue
        if metadata.get("request_fingerprint") != request_fingerprint:
            raise ActiveBacklogResetReusedKeyError(_RESET_IDEMPOTENCY_CONFLICT)
        replay = dict(metadata)
        replay["success"] = True
        replay["idempotent_replay"] = True
        return replay
    return None


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if isinstance(decoded, dict):
        return decoded
    return {}


def _reset_history_snapshot(session: Session) -> dict[str, int]:
    """Return table counts that reset-active must never reduce."""
    return {
        "user_stories": len(session.exec(select(UserStory.story_id)).all()),
        "sprints": len(session.exec(select(Sprint.sprint_id)).all()),
        "sprint_stories": len(session.exec(select(SprintStory.story_id)).all()),
        "tasks": len(session.exec(select(Task.task_id)).all()),
        "story_completion_logs": len(
            session.exec(select(StoryCompletionLog.log_id)).all()
        ),
        "task_execution_logs": len(session.exec(select(TaskExecutionLog.log_id)).all()),
        "workflow_events": len(session.exec(select(WorkflowEvent.event_id)).all()),
    }


def _assert_reset_history_preserved(
    *,
    before: dict[str, int],
    after: dict[str, int],
    created_story_count: int,
) -> None:
    """Fail closed when reset-active removed history rows before commit."""
    expected_minimums = dict(before)
    expected_minimums["user_stories"] = before["user_stories"] + created_story_count
    expected_minimums["workflow_events"] = before["workflow_events"] + 1
    for table, minimum_count in expected_minimums.items():
        if after.get(table, -1) < minimum_count:
            raise ActiveBacklogResetError(_RESET_HISTORY_PRESERVATION_FAILED)


def _story_status_value(status: StoryStatus | str) -> str:
    return status.value if isinstance(status, StoryStatus) else str(status)
