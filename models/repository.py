"""Persisted immutable repository provenance observations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel

from workflow.fingerprints import canonical_hash


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
    status_entries_json: str = Field(default="[]", sa_type=Text)
    remotes_json: str = Field(sa_type=Text)
    warnings_json: str = Field(sa_type=Text)
    probe_version: str
    inspected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    supersedes_repository_binding_id: int | None = Field(default=None, index=True)
    recorded_by: str = Field(index=True)


def repository_binding_fingerprint(binding: RepositoryBinding) -> str:
    """Fingerprint one exact durable immutable repository observation."""
    if binding.repository_binding_id is None:
        message = "Repository binding fingerprint requires a durable identity."
        raise ValueError(message)
    return canonical_hash(
        {
            "repository_binding_id": binding.repository_binding_id,
            "project_id": binding.project_id,
            "worktree_path": binding.worktree_path,
            "common_git_dir": binding.common_git_dir,
            "head_sha": binding.head_sha,
            "branch_name": binding.branch_name,
            "detached_head": binding.detached_head,
            "dirty": binding.dirty,
            "status_fingerprint": binding.status_fingerprint,
            "status_entries_json": binding.status_entries_json,
            "remotes_json": binding.remotes_json,
            "warnings_json": binding.warnings_json,
            "probe_version": binding.probe_version,
            "inspected_at": binding.inspected_at,
            "supersedes_repository_binding_id": (
                binding.supersedes_repository_binding_id
            ),
            "recorded_by": binding.recorded_by,
        }
    )


__all__ = ["RepositoryBinding", "repository_binding_fingerprint"]
