"""Event and audit-log SQLModel classes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel

from models.enums import (
    StoryResolution,
    StoryStatus,
    TaskAcceptanceResult,
    TaskStatus,
    WorkflowEventType,
)


class TaskExecutionLog(SQLModel, table=True):
    """Audit trail for task execution progress and outcome."""

    __tablename__ = "task_execution_logs"  # type: ignore[assignment]

    log_id: int | None = Field(default=None, primary_key=True)
    old_status: TaskStatus | None = Field(default=None)
    new_status: TaskStatus = Field(nullable=False)
    outcome_summary: str | None = Field(default=None, sa_type=Text)
    artifact_refs_json: str | None = Field(default=None, sa_type=Text)
    acceptance_result: TaskAcceptanceResult = Field(
        default=TaskAcceptanceResult.NOT_CHECKED
    )
    notes: str | None = Field(default=None, sa_type=Text)
    changed_by: str = Field(default="manual-ui", max_length=100)
    changed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    task_id: int = Field(foreign_key="tasks.task_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)


class StoryCompletionLog(SQLModel, table=True):
    """Audit trail for story status changes."""

    __tablename__ = "story_completion_logs"  # type: ignore[assignment]

    log_id: int | None = Field(default=None, primary_key=True)
    story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    old_status: StoryStatus
    new_status: StoryStatus
    resolution: StoryResolution | None = Field(default=None)
    delivered: str | None = Field(default=None, sa_type=Text)
    evidence: str | None = Field(default=None, sa_type=Text)
    known_gaps: str | None = Field(default=None, sa_type=Text)
    follow_ups_created: str | None = Field(default=None, sa_type=Text)
    changed_by: str | None = Field(default=None)
    changed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )


class WorkflowEvent(SQLModel, table=True):
    """Workflow event metrics and audit history."""

    __tablename__ = "workflow_events"  # type: ignore[assignment]
    event_id: int | None = Field(default=None, primary_key=True)
    event_type: WorkflowEventType = Field(nullable=False, index=True)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    duration_seconds: float | None = Field(default=None)
    turn_count: int | None = Field(default=None)
    project_id: int | None = Field(default=None, foreign_key="projects.project_id")
    sprint_id: int | None = Field(default=None, foreign_key="sprints.sprint_id")
    event_metadata: str | None = Field(default=None, sa_type=Text)
