"""API-facing request/response schemas with ORM-coupled enum dependencies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.enums import StoryResolution, TaskAcceptanceResult, TaskStatus

_DATETIME_TYPE = datetime


class WorkflowPositionGuards(BaseModel):
    """Exact graph position guards shared by API mutation payloads."""

    model_config = ConfigDict(extra="forbid")
    graph_version: str = Field(min_length=1)
    expected_fact_fingerprint: str = Field(min_length=1)
    expected_decision_fingerprint: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    changed_by: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = Field(default=None, min_length=1)


class _TaskExecutionDetailsRequiredError(ValueError):
    """Raised when a task status update omits all actionable details."""

    def __init__(self) -> None:
        super().__init__("Must provide outcome_summary, artifact_refs, or notes.")


class _TaskExecutionOutcomeRequiredError(ValueError):
    """Raised when a Done task update omits the outcome summary."""

    def __init__(self) -> None:
        super().__init__("An outcome_summary is required when marking a task Done.")


class _TaskAcceptanceRecordedError(ValueError):
    """Raised when a Done task update uses a placeholder acceptance result."""

    def __init__(self) -> None:
        super().__init__(
            "An acceptance_result must be recorded when mapping a task Done."
        )


class _TaskAcceptanceExplicitError(ValueError):
    """Raised when a Done task update omits acceptance_result entirely."""

    def __init__(self) -> None:
        super().__init__(
            "An explicit acceptance_result is required when marking a task Done."
        )


class _StoryCloseCompletionNotesError(ValueError):
    """Raised when a story-close request omits completion notes."""

    def __init__(self) -> None:
        super().__init__("completion_notes is required to close a story")


class TaskExecutionWriteRequest(BaseModel):
    """Payload for updating a task's status and logging its outcome."""

    new_status: TaskStatus
    outcome_summary: str | None = None
    artifact_refs: list[str] | None = None
    acceptance_result: TaskAcceptanceResult | None = None
    notes: str | None = None
    changed_by: str | None = None

    @model_validator(mode="after")
    def validate_execution_rules(self) -> TaskExecutionWriteRequest:
        """Normalize execution details and enforce valid status transitions."""
        if self.artifact_refs is not None:
            self.artifact_refs = [
                ref.strip() for ref in self.artifact_refs if ref and ref.strip()
            ]

        has_outcome = bool(self.outcome_summary and self.outcome_summary.strip())
        has_artifacts = bool(self.artifact_refs)
        has_notes = bool(self.notes and self.notes.strip())

        if not (has_outcome or has_artifacts or has_notes) and (
            self.new_status != TaskStatus.TO_DO
        ):
            raise _TaskExecutionDetailsRequiredError

        if self.new_status == TaskStatus.DONE:
            if not has_outcome:
                raise _TaskExecutionOutcomeRequiredError
            if self.acceptance_result == TaskAcceptanceResult.NOT_CHECKED:
                raise _TaskAcceptanceRecordedError
            if not self.acceptance_result:
                self.acceptance_result = TaskAcceptanceResult.NOT_CHECKED
                raise _TaskAcceptanceExplicitError

        return self


class TaskExecutionLogEntry(BaseModel):
    """Single persisted task execution log entry returned by the API."""

    log_id: int
    task_id: int
    sprint_id: int
    old_status: TaskStatus | None
    new_status: TaskStatus
    outcome_summary: str | None
    artifact_refs: list[str]
    acceptance_result: TaskAcceptanceResult
    notes: str | None
    changed_by: str
    changed_at: datetime


class TaskExecutionReadResponse(BaseModel):
    """Task execution response including the latest entry and history."""

    success: bool
    task_id: int
    sprint_id: int
    current_status: TaskStatus
    latest_entry: TaskExecutionLogEntry | None = None
    history: list[TaskExecutionLogEntry] = Field(default_factory=list)


class StoryTaskProgressSummary(BaseModel):
    """Aggregated task progress used to determine story close readiness."""

    total_tasks: int
    done_tasks: int
    cancelled_tasks: int
    all_actionable_tasks_done: bool


class StoryCloseReadResponse(BaseModel):
    """Story-close readiness payload returned by the API."""

    success: bool
    story_id: int
    sprint_id: int
    current_status: str
    resolution: StoryResolution | None = None
    completion_notes: str | None = None
    evidence_links: str | None = None
    completed_at: datetime | None = None
    readiness: StoryTaskProgressSummary
    close_eligible: bool
    ineligible_reason: str | None = None


class StoryCloseWriteRequest(BaseModel):
    """Request payload for closing a story and recording completion data."""

    resolution: StoryResolution
    completion_notes: str = Field(min_length=1)
    evidence_links: list[str] | None = None
    known_gaps: str | None = None
    follow_up_notes: str | None = None
    changed_by: str | None = Field(default="manual-ui")

    @model_validator(mode="after")
    def validate_story_close(self) -> StoryCloseWriteRequest:
        """Normalize evidence links and require non-empty completion notes."""
        if self.evidence_links is not None:
            self.evidence_links = [
                ref.strip() for ref in self.evidence_links if ref and ref.strip()
            ]
        if not self.completion_notes or not self.completion_notes.strip():
            raise _StoryCloseCompletionNotesError
        return self


class SprintCloseStorySummary(BaseModel):
    """Per-story completion summary included in sprint close readiness."""

    story_id: int
    story_title: str
    story_status: str
    total_tasks: int
    done_tasks: int
    cancelled_tasks: int
    completion_state: Literal["completed", "unfinished"]


class SprintCloseReadiness(BaseModel):
    """Aggregated sprint close readiness information."""

    completed_story_count: int
    open_story_count: int
    unfinished_story_ids: list[int] = Field(default_factory=list)
    stories: list[SprintCloseStorySummary] = Field(default_factory=list)


class SprintCloseReadResponse(BaseModel):
    """Sprint-close readiness payload returned by the API."""

    success: bool
    sprint_id: int
    current_status: str
    completed_at: datetime | None = None
    readiness: SprintCloseReadiness
    close_eligible: bool
    ineligible_reason: str | None = None
    history_fidelity: Literal["snapshotted", "derived"] = "derived"
    close_snapshot: dict[str, Any] | None = None


class SprintCloseWriteRequest(BaseModel):
    """Request payload for closing a sprint and recording follow-up notes."""

    completion_notes: str = Field(min_length=1)
    follow_up_notes: str | None = None
    changed_by: str | None = Field(default="manual-ui")
