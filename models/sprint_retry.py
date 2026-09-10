"""Append-only persistence for isolated retries of an existing Sprint."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.schema import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for immutable retry records."""
    return datetime.now(UTC)


class SprintRetryAttempt(SQLModel, table=True):
    """One immutable retry identity and its independent lifecycle."""

    __tablename__ = "sprint_retry_attempts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "retry_attempt_id", name="uq_retry_attempt_project"
        ),
        UniqueConstraint(
            "project_id", "sprint_id", "retry_attempt_id", name="uq_retry_attempt_scope"
        ),
        UniqueConstraint(
            "project_id", "sprint_id", "ordinal", name="uq_retry_attempt_ordinal"
        ),
        UniqueConstraint(
            "project_id",
            "sprint_id",
            "predecessor_retry_attempt_id",
            name="uq_retry_attempt_predecessor",
        ),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "predecessor_retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_attempt_predecessor",
        ),
        CheckConstraint("ordinal >= 2", name="ck_retry_attempt_ordinal"),
        CheckConstraint(
            "status IN ('Planned', 'Active', 'Completed')",
            name="ck_retry_attempt_status",
        ),
        Index(
            "uq_retry_attempt_original_predecessor",
            "project_id",
            "sprint_id",
            unique=True,
            sqlite_where=text("predecessor_retry_attempt_id IS NULL"),
        ),
        Index(
            "uq_retry_attempt_live_project",
            "project_id",
            unique=True,
            sqlite_where=text("status IN ('Planned', 'Active')"),
        ),
    )

    retry_attempt_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    ordinal: int
    predecessor_retry_attempt_id: int | None = Field(default=None, index=True)
    contract_fingerprint: str = Field(index=True)
    created_by: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    creation_fingerprint: str = Field(index=True)
    creation_receipt_key: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    status: str = Field(default="Planned", index=True)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)


class SprintRetryStoryState(SQLModel, table=True):
    """Attempt-local status for one immutable Story subject."""

    __tablename__ = "sprint_retry_story_states"
    __table_args__ = (
        UniqueConstraint("retry_attempt_id", "story_id", name="uq_retry_story_state"),
        UniqueConstraint(
            "project_id",
            "sprint_id",
            "retry_attempt_id",
            "story_id",
            name="uq_retry_story_state_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_story_state_attempt",
        ),
    )

    sprint_retry_story_state_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    status: str = Field(index=True)


class SprintRetryTaskState(SQLModel, table=True):
    """Attempt-local status for one immutable Task subject."""

    __tablename__ = "sprint_retry_task_states"
    __table_args__ = (
        UniqueConstraint("retry_attempt_id", "task_id", name="uq_retry_task_state"),
        UniqueConstraint(
            "project_id",
            "sprint_id",
            "retry_attempt_id",
            "task_id",
            name="uq_retry_task_state_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_task_state_attempt",
        ),
    )

    sprint_retry_task_state_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    task_id: int = Field(foreign_key="tasks.task_id", index=True)
    status: str = Field(index=True)


class SprintRetryStart(SQLModel, table=True):
    """Immutable start decision for one retry attempt."""

    __tablename__ = "sprint_retry_starts"
    __table_args__ = (
        UniqueConstraint("retry_attempt_id", name="uq_retry_start_attempt"),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_start_attempt",
        ),
    )

    sprint_retry_start_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    contract_fingerprint: str = Field(index=True)
    decision_fingerprint: str = Field(index=True)
    started_by: str = Field(index=True)
    started_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintRetryTaskEvidence(SQLModel, table=True):
    """Immutable Task evidence bound to one retry attempt and Task state."""

    __tablename__ = "sprint_retry_task_evidence"
    __table_args__ = (
        UniqueConstraint("retry_attempt_id", "task_id", name="uq_retry_task_evidence"),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_task_evidence_attempt",
        ),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id", "task_id"],
            [
                "sprint_retry_task_states.project_id",
                "sprint_retry_task_states.sprint_id",
                "sprint_retry_task_states.retry_attempt_id",
                "sprint_retry_task_states.task_id",
            ],
            name="fk_retry_task_evidence_state",
        ),
        CheckConstraint(
            "acceptance_result IN ('partially_met', 'fully_met')",
            name="ck_retry_task_evidence_acceptance",
        ),
    )

    sprint_retry_task_evidence_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    task_id: int = Field(foreign_key="tasks.task_id", index=True)
    outcome_summary: str = Field(sa_type=Text)
    artifact_refs_json: str = Field(sa_type=Text)
    acceptance_result: str = Field(index=True)
    checklist_result_json: str = Field(sa_type=Text)
    evidence_fingerprint: str = Field(index=True)
    completed_by: str = Field(index=True)
    completed_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintRetryStoryClosure(SQLModel, table=True):
    """Immutable Story closure bound to one retry attempt and Story state."""

    __tablename__ = "sprint_retry_story_closures"
    __table_args__ = (
        UniqueConstraint("retry_attempt_id", "story_id", name="uq_retry_story_closure"),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_story_closure_attempt",
        ),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id", "story_id"],
            [
                "sprint_retry_story_states.project_id",
                "sprint_retry_story_states.sprint_id",
                "sprint_retry_story_states.retry_attempt_id",
                "sprint_retry_story_states.story_id",
            ],
            name="fk_retry_story_closure_state",
        ),
    )

    sprint_retry_story_closure_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    completion_fingerprint: str = Field(index=True)
    resolution: str
    delivered: str = Field(sa_type=Text)
    evidence: str = Field(sa_type=Text)
    known_gaps: str = Field(sa_type=Text)
    closed_by: str = Field(index=True)
    closed_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintRetryReview(SQLModel, table=True):
    """Immutable review fact for one retry attempt."""

    __tablename__ = "sprint_retry_reviews"
    __table_args__ = (
        UniqueConstraint("retry_attempt_id", name="uq_retry_review_attempt"),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_review_attempt",
        ),
    )

    sprint_retry_review_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    review_fingerprint: str = Field(index=True)
    reviewed_by: str = Field(index=True)
    reviewed_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintRetryClosure(SQLModel, table=True):
    """Immutable closure fact for one retry attempt."""

    __tablename__ = "sprint_retry_closures"
    __table_args__ = (
        UniqueConstraint("retry_attempt_id", name="uq_retry_closure_attempt"),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_closure_attempt",
        ),
    )

    sprint_retry_closure_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    review_fingerprint: str = Field(index=True)
    close_fingerprint: str = Field(index=True)
    closed_by: str = Field(index=True)
    closed_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintRetryTriage(SQLModel, table=True):
    """Append-only retry-local post-Sprint triage and correction history."""

    __tablename__ = "sprint_retry_triage"
    __table_args__ = (
        UniqueConstraint(
            "retry_attempt_id",
            "sprint_retry_triage_id",
            name="uq_retry_triage_identity",
        ),
        UniqueConstraint(
            "retry_attempt_id",
            "supersedes_sprint_retry_triage_id",
            name="uq_retry_triage_correction",
        ),
        ForeignKeyConstraint(
            ["project_id", "sprint_id", "retry_attempt_id"],
            [
                "sprint_retry_attempts.project_id",
                "sprint_retry_attempts.sprint_id",
                "sprint_retry_attempts.retry_attempt_id",
            ],
            name="fk_retry_triage_attempt",
        ),
        ForeignKeyConstraint(
            ["retry_attempt_id", "supersedes_sprint_retry_triage_id"],
            [
                "sprint_retry_triage.retry_attempt_id",
                "sprint_retry_triage.sprint_retry_triage_id",
            ],
            name="fk_retry_triage_supersedes",
        ),
        CheckConstraint(
            "impact IN ('none', 'backlog', 'specification')",
            name="ck_retry_triage_impact",
        ),
        Index(
            "uq_retry_triage_original",
            "retry_attempt_id",
            unique=True,
            sqlite_where=text("supersedes_sprint_retry_triage_id IS NULL"),
        ),
    )

    sprint_retry_triage_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    retry_attempt_id: int = Field(index=True)
    impact: str = Field(index=True)
    canonical_payload_json: str = Field(sa_type=Text)
    payload_fingerprint: str = Field(index=True)
    supersedes_sprint_retry_triage_id: int | None = Field(default=None, index=True)
    recorded_by: str = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)
