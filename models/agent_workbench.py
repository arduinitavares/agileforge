"""Agent workbench persistence models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(UTC)


class DiscoveryChallengeArtifact(SQLModel, table=True):
    """Saved Scope Discovery challenge artifact."""

    __tablename__: ClassVar[str] = "discovery_challenge_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_discovery_challenge_project_idempotency",
        ),
    )

    challenge_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    producer: str = Field(index=True)
    readiness: str = Field(index=True)
    original_idea: str = Field(sa_type=Text)
    content_json: str = Field(sa_type=Text)
    artifact_fingerprint: str = Field(index=True)
    request_hash: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    changed_by: str = Field(default="cli-agent", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)


class DiscoveryPrd(SQLModel, table=True):
    """Saved Scope Discovery PRD."""

    __tablename__: ClassVar[str] = "discovery_prds"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_discovery_prd_project_idempotency",
        ),
    )

    prd_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    challenge_artifact_id: int = Field(
        foreign_key="discovery_challenge_artifacts.challenge_artifact_id",
        index=True,
    )
    producer: str = Field(index=True)
    status: str = Field(index=True)
    version: str = Field(index=True)
    title: str = Field(index=True)
    content_json: str = Field(sa_type=Text)
    supersedes_prd_id: int | None = Field(
        default=None,
        foreign_key="discovery_prds.prd_id",
        index=True,
    )
    artifact_fingerprint: str = Field(index=True)
    request_hash: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    reviewed_by: str | None = Field(default=None, index=True)
    review_notes: str | None = Field(default=None, sa_type=Text)
    reviewed_at: datetime | None = Field(default=None, nullable=True)
    review_request_hash: str | None = Field(default=None, index=True)
    review_idempotency_key: str | None = Field(default=None, index=True)
    changed_by: str = Field(default="cli-agent", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)


class DiscoverySpecAmendmentDraft(SQLModel, table=True):
    """Saved Scope Discovery Spec Amendment Draft."""

    __tablename__: ClassVar[str] = "discovery_spec_amendment_drafts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_discovery_spec_amendment_project_idempotency",
        ),
    )

    spec_amendment_draft_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    prd_id: int = Field(foreign_key="discovery_prds.prd_id", index=True)
    challenge_artifact_id: int = Field(
        foreign_key="discovery_challenge_artifacts.challenge_artifact_id",
        index=True,
    )
    status: str = Field(index=True)
    amendment_file: str = Field(sa_type=Text)
    content_json: str = Field(sa_type=Text)
    validation_json: str = Field(sa_type=Text)
    artifact_fingerprint: str = Field(index=True)
    request_hash: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    base_spec_version_id: int | None = Field(default=None, index=True)
    base_spec_hash: str | None = Field(default=None, index=True)
    amended_spec_hash: str | None = Field(default=None, index=True)
    reviewed_by: str | None = Field(default=None, index=True)
    review_notes: str | None = Field(default=None, sa_type=Text)
    reviewed_at: datetime | None = Field(default=None, nullable=True)
    review_request_hash: str | None = Field(default=None, index=True)
    review_idempotency_key: str | None = Field(default=None, index=True)
    changed_by: str = Field(default="cli-agent", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)
