"""POSIX descriptor adapter for secure Vision repository evidence reads."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.vision_evidence import VisionEvidenceWarning
from services.vision_evidence_reader import (
    RepositoryEvidenceCapability,
    RepositoryEvidenceCapabilityError,
    RepositoryEvidenceChangedError,
)

if TYPE_CHECKING:
    from types import TracebackType

    from services.vision_evidence_reader import RepositoryEvidenceBinding

_OPEN_SUPPORTS_DIR_FD: bool = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD: bool = os.stat in os.supports_dir_fd
_O_NOFOLLOW: object = getattr(os, "O_NOFOLLOW", None)
_O_DIRECTORY: object = getattr(os, "O_DIRECTORY", None)
_O_NONBLOCK: object = getattr(os, "O_NONBLOCK", None)
_CHANGED_DURING_READ = "Approved evidence file changed while it was read."
_WORKTREE_CHANGED_BEFORE_READ = (
    "Repository worktree changed before evidence files were read."
)


@dataclass(frozen=True)
class _PosixEvidenceBinding:
    """No-op token preserving the shared bind-before-resolve contract."""

    def close(self) -> None:
        """POSIX already binds identity during descriptor traversal."""


@dataclass
class _PosixEvidenceWorktree:
    """Retain the verified POSIX worktree descriptor across evidence reads."""

    root_descriptor: int
    _closed: bool = False

    def __enter__(self) -> _PosixEvidenceWorktree:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        """Close the retained root exactly once."""
        if not self._closed:
            os.close(self.root_descriptor)
            self._closed = True

    def bind(
        self,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> RepositoryEvidenceBinding:
        """Return a no-op binding without changing POSIX traversal semantics."""
        del source_path, warnings
        return _PosixEvidenceBinding()

    def read(
        self,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
        byte_limit: int,
        binding: RepositoryEvidenceBinding,
    ) -> bytes | None:
        """Read one approved source through descriptor-relative traversal."""
        del binding
        opened = PosixRepositoryEvidenceReader._open_descriptor(
            self.root_descriptor,
            resolved_path,
            source_path,
            warnings,
        )
        if opened is None:
            return None
        descriptor, parent_descriptor, leaf_name = opened
        try:
            try:
                before = os.fstat(descriptor)
                content = bytearray()
                while len(content) < byte_limit:
                    chunk = os.read(descriptor, byte_limit - len(content))
                    if not chunk:
                        break
                    content.extend(chunk)
                after = os.fstat(descriptor)
                current = os.stat(
                    leaf_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise RepositoryEvidenceChangedError(_CHANGED_DURING_READ) from exc
        finally:
            os.close(descriptor)
            os.close(parent_descriptor)
        if not (
            self._file_identity(before) == self._file_identity(after)
            and self._file_identity(before) == self._file_identity(current)
        ):
            raise RepositoryEvidenceChangedError(_CHANGED_DURING_READ)
        return bytes(content)

    @staticmethod
    def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
        """Return metadata used to detect replacement or modification."""
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )


class PosixRepositoryEvidenceReader:
    """Read evidence with descriptor-relative POSIX traversal."""

    def capability(self, worktree: Path) -> RepositoryEvidenceCapability:
        """Report whether required descriptor operations are present."""
        del worktree
        required = (
            _O_NOFOLLOW,
            _O_DIRECTORY,
            _O_NONBLOCK,
        )
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in required
        ):
            return RepositoryEvidenceCapability(
                available=False,
                code="REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
                message="Platform cannot safely open repository evidence.",
            )
        if not _OPEN_SUPPORTS_DIR_FD or not _STAT_SUPPORTS_DIR_FD:
            return RepositoryEvidenceCapability(
                available=False,
                code="REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
                message="Platform lacks secure directory-relative file operations.",
            )
        return RepositoryEvidenceCapability(available=True)

    def open(self, worktree: Path) -> _PosixEvidenceWorktree:
        """Open and retain the verified worktree directory."""
        capability = self.capability(worktree)
        if not capability.available:
            raise RepositoryEvidenceCapabilityError(
                capability.message or "Repository evidence is unavailable."
            )
        no_follow = _O_NOFOLLOW
        directory = _O_DIRECTORY
        if not isinstance(no_follow, int) or not isinstance(directory, int):
            raise RepositoryEvidenceCapabilityError(
                capability.message or "Repository evidence is unavailable."
            )
        try:
            descriptor = os.open(worktree, os.O_RDONLY | no_follow | directory)
        except OSError as exc:
            raise RepositoryEvidenceChangedError(_WORKTREE_CHANGED_BEFORE_READ) from exc
        return _PosixEvidenceWorktree(root_descriptor=descriptor)

    @staticmethod
    def _open_descriptor(
        root_descriptor: int,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> tuple[int, int, str] | None:
        """Open every relative path component without following symbolic links."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if not isinstance(no_follow, int) or not isinstance(directory, int):
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Platform cannot safely open approved evidence files.",
                )
            )
            return None
        relative = Path(resolved_path)
        parts = relative.parts
        if (
            relative.is_absolute()
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
        ):
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence path is not repository-relative.",
                )
            )
            return None
        parent_descriptor = PosixRepositoryEvidenceReader._open_parent_descriptor(
            root_descriptor,
            parts[:-1],
            source_path,
            no_follow | directory,
            warnings,
        )
        if parent_descriptor is None:
            return None
        leaf_name = parts[-1]
        if not PosixRepositoryEvidenceReader._component_is_safe(
            parent_descriptor,
            leaf_name,
            source_path,
            warnings,
        ):
            os.close(parent_descriptor)
            return None
        descriptor = PosixRepositoryEvidenceReader()._open_regular_leaf(
            parent_descriptor,
            leaf_name,
            source_path,
            no_follow,
            warnings,
        )
        if descriptor is None:
            os.close(parent_descriptor)
            return None
        return descriptor, parent_descriptor, leaf_name

    def _open_regular_leaf(
        self,
        parent_descriptor: int,
        leaf_name: str,
        source_path: str,
        no_follow: int,
        warnings: list[VisionEvidenceWarning],
    ) -> int | None:
        """Open one nonblocking leaf and retain only regular files."""
        del self
        nonblock = getattr(os, "O_NONBLOCK", None)
        if not isinstance(nonblock, int) or isinstance(nonblock, bool) or nonblock <= 0:
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Platform cannot safely open approved evidence files.",
                )
            )
            return None
        try:
            descriptor = os.open(
                leaf_name,
                os.O_RDONLY | no_follow | nonblock,
                dir_fd=parent_descriptor,
            )
        except OSError:
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence file could not be opened.",
                )
            )
            return None
        try:
            opened_stat = os.fstat(descriptor)
        except OSError:
            os.close(descriptor)
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence file could not be inspected.",
                )
            )
            return None
        if not stat.S_ISREG(opened_stat.st_mode):
            os.close(descriptor)
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence source is not a regular file.",
                )
            )
            return None
        return descriptor

    @staticmethod
    def _open_parent_descriptor(
        root_descriptor: int,
        components: tuple[str, ...],
        relative_path: str,
        directory_flags: int,
        warnings: list[VisionEvidenceWarning],
    ) -> int | None:
        """Traverse intermediate directories relative to the retained root."""
        try:
            parent_descriptor = os.dup(root_descriptor)
        except OSError:
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=relative_path,
                    message="Approved evidence traversal could not be started.",
                )
            )
            return None
        for component in components:
            if not PosixRepositoryEvidenceReader._component_is_safe(
                parent_descriptor,
                component,
                relative_path,
                warnings,
            ):
                os.close(parent_descriptor)
                return None
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | directory_flags,
                    dir_fd=parent_descriptor,
                )
            except OSError:
                os.close(parent_descriptor)
                warnings.append(
                    _warning(
                        code="EVIDENCE_UNREADABLE",
                        source=relative_path,
                        message="Approved evidence directory could not be opened.",
                    )
                )
                return None
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        return parent_descriptor

    @staticmethod
    def _component_is_safe(
        parent_descriptor: int,
        component: str,
        relative_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> bool:
        """Reject missing, unreadable, and symbolic-link path components."""
        try:
            component_stat = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        except OSError:
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=relative_path,
                    message="Approved evidence path could not be inspected.",
                )
            )
            return False
        if not stat.S_ISLNK(component_stat.st_mode):
            return True
        warnings.append(
            _warning(
                code="SYMLINK_ESCAPE",
                source=relative_path,
                message="Approved evidence path contains a symbolic link.",
            )
        )
        return False


def _warning(*, code: str, source: str, message: str) -> VisionEvidenceWarning:
    return VisionEvidenceWarning(code=code, source=source, message=message)


__all__ = ["PosixRepositoryEvidenceReader"]
