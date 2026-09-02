"""Platform-neutral secure repository evidence reader contract."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from services.contracts.vision_evidence import VisionEvidenceWarning


type RepositoryEvidenceCapabilityCode = Literal[
    "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"
]


@dataclass(frozen=True)
class RepositoryEvidenceCapability:
    """Closed provider-free capability result for one repository worktree."""

    available: bool
    code: RepositoryEvidenceCapabilityCode | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        """Reject contradictory or open-ended capability projections."""
        if self.available:
            if self.code is not None or self.message is not None:
                message = "Available repository capability cannot carry an error."
                raise ValueError(message)
            return
        if (
            self.code != "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"
            or not self.message
        ):
            message = "Unavailable repository capability requires its closed reason."
            raise ValueError(message)


class RepositoryEvidenceWorktree(Protocol):
    """Retain one trusted worktree anchor across all approved reads."""

    def bind(
        self,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> RepositoryEvidenceBinding:
        """Bind one logical source before resolving its compatible target."""
        ...

    def read(
        self,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
        byte_limit: int,
        binding: RepositoryEvidenceBinding,
    ) -> bytes | None:
        """Read one approved target or append an optional-source warning."""
        ...


class RepositoryEvidenceBinding(Protocol):
    """Retain a source identity across policy resolution and target opening."""

    @property
    def present_at_bind(self) -> bool:
        """Return whether the logical source existed when binding completed."""
        ...

    @property
    def resolution_bound(self) -> bool:
        """Return whether resolved_path came from the retained reader boundary."""
        ...

    @property
    def resolved_path(self) -> str | None:
        """Return the safely resolved repository-relative target when available."""
        ...

    def close(self) -> None:
        """Release the retained source identity exactly once."""
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
    "RepositoryEvidenceBinding",
    "RepositoryEvidenceCapability",
    "RepositoryEvidenceCapabilityCode",
    "RepositoryEvidenceCapabilityError",
    "RepositoryEvidenceChangedError",
    "RepositoryEvidenceReader",
    "RepositoryEvidenceWorktree",
    "repository_evidence_reader",
]
