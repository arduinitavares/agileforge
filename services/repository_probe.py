"""Immutable contracts for deterministic repository provenance probes."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field

from workflow.contracts import FrozenModel

if TYPE_CHECKING:
    from pathlib import Path


class RepositoryProbeErrorCode(StrEnum):
    """Closed failures produced while inspecting a Git worktree."""

    PATH_MISSING = "PATH_MISSING"
    PATH_NOT_DIRECTORY = "PATH_NOT_DIRECTORY"
    NOT_GIT_WORKTREE = "NOT_GIT_WORKTREE"
    GIT_METADATA_UNREADABLE = "GIT_METADATA_UNREADABLE"
    UNBORN_HEAD = "UNBORN_HEAD"
    REPOSITORY_CHANGED_DURING_PROBE = "REPOSITORY_CHANGED_DURING_PROBE"
    MALFORMED_PATH = "MALFORMED_PATH"


_ERROR_MESSAGES: dict[RepositoryProbeErrorCode, str] = {
    RepositoryProbeErrorCode.PATH_MISSING: "Repository path does not exist.",
    RepositoryProbeErrorCode.PATH_NOT_DIRECTORY: "Repository path is not a directory.",
    RepositoryProbeErrorCode.NOT_GIT_WORKTREE: "Repository path is not a Git worktree.",
    RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE: "Git metadata could not be read.",
    RepositoryProbeErrorCode.UNBORN_HEAD: (
        "Repository HEAD does not reference a commit."
    ),
    RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE: (
        "Repository HEAD changed during the probe."
    ),
    RepositoryProbeErrorCode.MALFORMED_PATH: "Repository path is malformed.",
}


class RepositoryProbeError(RuntimeError):
    """Typed closed failure raised by a repository probe."""

    def __init__(self, code: RepositoryProbeErrorCode, path: str) -> None:
        """Store the closed code and normalized path without inspecting either."""
        self.code = code
        self.path = path
        super().__init__(_ERROR_MESSAGES[code])


class RepositoryStatusEntry(FrozenModel):
    """One normalized Git status observation."""

    area: Literal["index", "worktree", "untracked"]
    change: Literal["added", "modified", "deleted", "renamed", "type_changed"]
    path: str
    previous_path: str | None = None


class RepositoryProbeWarning(FrozenModel):
    """A non-fatal repository observation warning."""

    code: Literal["DIRTY_WORKTREE"]
    message: str


class RepositoryProbeResult(FrozenModel):
    """Immutable repository identity and status captured by one probe."""

    worktree_path: str
    common_git_dir: str
    head_sha: str
    branch_name: str | None
    detached_head: bool
    dirty: bool
    status_entries: tuple[RepositoryStatusEntry, ...]
    status_fingerprint: str
    remotes: tuple[str, ...]
    probe_version: Literal["agileforge.repository-probe.v1"]
    inspected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    warnings: tuple[RepositoryProbeWarning, ...]


class RepositoryProbe(Protocol):
    """Read-only boundary for repository provenance inspection."""

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        """Inspect one Git worktree without altering it."""
