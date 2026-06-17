"""Authority curation persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import Index, UniqueConstraint
from sqlalchemy.sql import text
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(UTC)


class AuthorityFeedbackAttempt(SQLModel, table=True):
    """Structured feedback recorded against one authority candidate."""

    __tablename__ = "authority_feedback_attempts"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "feedback_attempt_id",
            name="uq_authority_feedback_project_attempt",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_authority_feedback_project_idempotency",
        ),
        Index("ix_authority_feedback_project_status", "project_id", "status"),
        Index("ix_authority_feedback_source_authority", "source_authority_id"),
    )

    feedback_row_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id")
    feedback_attempt_id: str
    source_authority_id: int
    source_authority_fingerprint: str
    feedback_fingerprint: str
    status: str = Field(
        default="recorded",
        sa_column_kwargs={"server_default": text("'recorded'")},
    )
    has_blocking_feedback: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": text("0")},
    )
    feedback_json: str = Field(sa_type=Text)
    request_hash: str
    idempotency_key: str
    changed_by: str = Field(
        default="cli-agent",
        sa_column_kwargs={"server_default": text("'cli-agent'")},
    )
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)


class AuthorityCurationAttempt(SQLModel, table=True):
    """ADK-backed curation attempt for one authority candidate."""

    __tablename__ = "authority_curation_attempts"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "curation_attempt_id",
            name="uq_authority_curation_project_attempt",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_authority_curation_project_idempotency",
        ),
        Index("ix_authority_curation_project_status", "project_id", "status"),
        Index("ix_authority_curation_mutation_event_id", "mutation_event_id"),
        Index("ix_authority_curation_source_authority", "source_authority_id"),
        Index(
            "uq_authority_curation_running_authority",
            "project_id",
            "source_authority_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
    )

    curation_row_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id")
    mutation_event_id: int | None = Field(default=None)
    curation_attempt_id: str
    source_authority_id: int
    source_authority_fingerprint: str
    spec_version_id: int
    feedback_attempt_id: str
    status: str = Field(
        default="running",
        sa_column_kwargs={"server_default": text("'running'")},
    )
    max_iterations: int = Field(
        default=2,
        sa_column_kwargs={"server_default": text("2")},
    )
    iteration_count: int = Field(
        default=0,
        sa_column_kwargs={"server_default": text("0")},
    )
    compiler_model: str | None = Field(default=None)
    candidate_authority_id: int | None = Field(default=None)
    candidate_authority_fingerprint: str | None = Field(default=None)
    request_json: str = Field(
        default="{}",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'{}'")},
    )
    candidate_lineage_json: str = Field(
        default="{}",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'{}'")},
    )
    diff_summary_json: str = Field(
        default="{}",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'{}'")},
    )
    lineage_json: str = Field(
        default="{}",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'{}'")},
    )
    quality_report_json: str = Field(
        default="{}",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'{}'")},
    )
    contract_version: str = Field(
        default="authority_curation.v1",
        sa_column_kwargs={"server_default": text("'authority_curation.v1'")},
    )
    menu_fingerprint: str | None = Field(default=None)
    selection_fingerprint: str | None = Field(default=None)
    rejected_selection_json: str = Field(
        default="{}",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'{}'")},
    )
    overlay_json: str = Field(
        default="{}",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'{}'")},
    )
    failure_artifact_id: str | None = Field(default=None)
    request_hash: str
    idempotency_key: str
    changed_by: str = Field(
        default="cli-agent",
        sa_column_kwargs={"server_default": text("'cli-agent'")},
    )
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)
