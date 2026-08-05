"""Persisted immutable repository provenance observations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


class RepositoryBinding(SQLModel, table=True):
    """One immutable repository probe bound to a single Project."""

    __tablename__ = "repository_bindings"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "repository_binding_id",
            name="uq_repository_binding_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "status_fingerprint",
            "inspected_at",
            name="uq_repository_binding_project_fingerprint_inspected_at",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_repository_binding_id"],
            [
                "repository_bindings.project_id",
                "repository_bindings.repository_binding_id",
            ],
            name="fk_repository_binding_supersedes",
        ),
    )

    repository_binding_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    worktree_path: str = Field(sa_type=Text)
    common_git_dir: str = Field(sa_type=Text)
    head_sha: str = Field(index=True, min_length=40, max_length=40)
    branch_name: str | None = Field(default=None)
    detached_head: bool
    dirty: bool
    status_fingerprint: str = Field(index=True)
    remotes_json: str = Field(sa_type=Text)
    warnings_json: str = Field(sa_type=Text)
    probe_version: str
    inspected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    supersedes_repository_binding_id: int | None = Field(default=None, index=True)
    recorded_by: str = Field(index=True)
