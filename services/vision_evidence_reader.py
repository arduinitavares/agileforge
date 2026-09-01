"""Platform-neutral secure repository evidence reader contract."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from services.contracts.vision_evidence import VisionEvidenceWarning


@dataclass(frozen=True)
class RepositoryEvidenceCapability:
    """Closed provider-free capability result for one repository worktree."""

    available: bool
    code: str | None = None
    message: str | None = None


class RepositoryEvidenceWorktree(Protocol):
    """Retain one trusted worktree anchor across all approved reads."""

    def read(
        self,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
        byte_limit: int,
    ) -> bytes | None:
        """Read one approved target or append an optional-source warning."""
        ...


class RepositoryEvidenceReader(Protocol):
    """Open one platform-specific retained worktree traversal."""

    def capability(self, worktree: Path) -> RepositoryEvidenceCapability:
        """Return whether the worktree satisfies this reader's safety contract."""
        ...

    def open(
        self,
        worktree: Path,
    ) -> AbstractContextManager[RepositoryEvidenceWorktree]:
        """Retain one trusted root until every approved read is complete."""
        ...


class RepositoryEvidenceCapabilityError(RuntimeError):
    """Raised when the platform or filesystem cannot meet the safety contract."""


class RepositoryEvidenceChangedError(RuntimeError):
    """Raised when a retained repository object changes during collection."""


def repository_evidence_reader() -> RepositoryEvidenceReader:
    """Select one platform adapter without importing Windows code on POSIX."""
    if sys.platform == "win32":
        from services.vision_evidence_windows import (  # noqa: PLC0415
            WindowsRepositoryEvidenceReader,
        )

        return WindowsRepositoryEvidenceReader()
    from services.vision_evidence_posix import (  # noqa: PLC0415
        PosixRepositoryEvidenceReader,
    )

    return PosixRepositoryEvidenceReader()


__all__ = [
    "RepositoryEvidenceCapability",
    "RepositoryEvidenceCapabilityError",
    "RepositoryEvidenceChangedError",
    "RepositoryEvidenceReader",
    "RepositoryEvidenceWorktree",
    "repository_evidence_reader",
]
