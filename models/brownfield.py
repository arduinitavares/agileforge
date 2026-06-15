"""Brownfield product-spec curation persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel

_TOOL_VERSION = "brownfield-curation.v1"


class BrownfieldSourceArtifact(SQLModel, table=True):
    """Persisted source artifact captured for brownfield curation."""

    __tablename__ = "brownfield_source_artifacts"  # type: ignore[assignment]

    pk: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id")
    attempt_id: str
    artifact_fingerprint: str = Field(index=True)
    source_kind: str = Field(default="source_file")
    source_file_path: str | None = Field(default=None, sa_type=Text)
    source_sha256: str | None = Field(default=None, index=True)
    content_preview: str | None = Field(default=None, sa_type=Text)
    status: str = Field(default="complete")
    request_hash: str = Field(index=True)
    warning_metadata_json: str = Field(default="[]", sa_type=Text)
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    tool_version: str = Field(default=_TOOL_VERSION)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "attempt_id",
            name="unique_brownfield_source_artifact_attempt",
        ),
    )


class BrownfieldScanAttempt(SQLModel, table=True):
    """Persisted repository scan attempt for brownfield curation."""

    __tablename__ = "brownfield_scan_attempts"  # type: ignore[assignment]

    pk: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id")
    attempt_id: str
    artifact_fingerprint: str
    source_attempt_id: str | None = Field(default=None, index=True)
    source_fingerprint: str = Field(index=True)
    repo_path: str = Field(sa_type=Text)
    repo_commit: str | None = Field(default=None, index=True)
    repo_dirty: bool = Field(default=False, index=True)
    file_manifest_json: str = Field(default="[]", sa_type=Text)
    implementation_facts_json: str = Field(default="[]", sa_type=Text)
    status: str = Field(default="complete")
    request_hash: str = Field(index=True)
    warning_metadata_json: str = Field(default="[]", sa_type=Text)
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    tool_version: str = Field(default=_TOOL_VERSION)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "attempt_id",
            name="unique_brownfield_scan_attempt",
        ),
    )


class BrownfieldSpecDraftAttempt(SQLModel, table=True):
    """Persisted specification draft attempt for brownfield curation."""

    __tablename__ = "brownfield_spec_draft_attempts"  # type: ignore[assignment]

    pk: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id")
    attempt_id: str
    artifact_fingerprint: str
    origin: str = Field(index=True)
    status: str = Field(default="complete", index=True)
    source_fingerprint: str = Field(index=True)
    scan_attempt_id: str = Field(index=True)
    scan_fingerprint: str = Field(index=True)
    parent_draft_attempt_id: str | None = Field(default=None, index=True)
    spec_hash: str | None = Field(default=None, index=True)
    curated_spec_json: str | None = Field(default=None, sa_type=Text)
    imported_file_path: str | None = Field(default=None, sa_type=Text)
    request_hash: str = Field(index=True)
    user_input_hash: str | None = Field(default=None, index=True)
    warning_metadata_json: str = Field(default="[]", sa_type=Text)
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    tool_version: str = Field(default=_TOOL_VERSION)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "attempt_id",
            name="unique_brownfield_spec_draft_attempt",
        ),
    )


class BrownfieldSpecApproval(SQLModel, table=True):
    """Persisted specification approval attempt for brownfield curation."""

    __tablename__ = "brownfield_spec_approvals"  # type: ignore[assignment]

    pk: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id")
    approval_attempt_id: str = Field(index=True)
    approval_fingerprint: str = Field(index=True, unique=True)
    draft_attempt_id: str = Field(index=True)
    draft_fingerprint: str = Field(index=True)
    scan_fingerprint: str = Field(index=True)
    source_fingerprint: str = Field(index=True)
    spec_hash: str = Field(index=True)
    spec_version_id: int | None = Field(default=None, index=True)
    managed_spec_file_path: str | None = Field(default=None, sa_type=Text)
    mutation_event_id: int | None = Field(default=None, index=True)
    status: str = Field(default="started")
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    tool_version: str = Field(default=_TOOL_VERSION, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )
