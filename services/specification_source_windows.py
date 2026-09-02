"""Strict Windows source capture using the shared native handle primitives."""

from __future__ import annotations

import ntpath
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from services.vision_evidence_reader import (
    RepositoryEvidenceCapabilityError,
    RepositoryEvidenceChangedError,
)
from services.vision_evidence_windows import (
    WindowsRepositoryEvidenceReader,
    _WindowsCapabilityError,
    _WindowsNativeError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from services.vision_evidence_windows import _FileIdentity, _WindowsApi

_DIRECTORY_ATTRIBUTE = 0x10
_REPARSE_ATTRIBUTE = 0x400
_MISSING_ERRORS = frozenset({2, 3})


class UnsafeWindowsSourceError(RuntimeError):
    """A selected path is unsafe or does not name the exact requested entry."""


@dataclass(frozen=True)
class WindowsSourceBytes:
    """Exact bounded bytes and the native identity used for duplicate checks."""

    content: bytes
    volume: int
    file_id: int


@dataclass(frozen=True)
class _RetainedComponent:
    handle: int
    identity: _FileIdentity
    final_path: str


@dataclass(frozen=True)
class WindowsSpecificationSourceWorktree:
    """Read exact non-reparse selections relative to one retained root."""

    api: _WindowsApi
    root_handle: int
    root_final_path: str

    def capture(self, relative_path: str, byte_limit: int) -> WindowsSourceBytes | None:
        """Retain and revalidate the complete directory chain around each read."""
        parts = _exact_parts(relative_path)
        with ExitStack() as handles:
            chain = self._open_chain(handles, parts)
            if chain is None:
                return None
            leaf = chain[-1]
            try:
                content = self.api.read(leaf.handle, byte_limit + 1)
                self._verify_chain(chain, parts)
            except (_WindowsNativeError, UnsafeWindowsSourceError) as error:
                message = "Selected Windows source changed while it was read."
                raise RepositoryEvidenceChangedError(message) from error
            return WindowsSourceBytes(
                content=content,
                volume=leaf.identity.volume_serial,
                file_id=int.from_bytes(leaf.identity.file_id, "little"),
            )

    def _open_chain(
        self, handles: ExitStack, parts: tuple[str, ...]
    ) -> tuple[_RetainedComponent, ...] | None:
        parent = self.root_handle
        chain: list[_RetainedComponent] = []
        for index, part in enumerate(parts):
            directory = index < len(parts) - 1
            try:
                handle = self.api.open_relative(parent, part, directory=directory)
            except _WindowsNativeError as error:
                if not directory and error.error_code in _MISSING_ERRORS:
                    return None
                message = "Selected Windows source cannot be opened safely."
                raise UnsafeWindowsSourceError(message) from error
            handles.callback(self.api.close, handle, native=True)
            attributes = self.api.attributes(handle).FileAttributes
            if attributes & _REPARSE_ATTRIBUTE or (
                bool(attributes & _DIRECTORY_ATTRIBUTE) != directory
            ):
                message = "Selected Windows source has a reparse or wrong file type."
                raise UnsafeWindowsSourceError(message)
            final_path = self.api.final_path(handle)
            # Case-folding or path normalization would accept alternate spellings.
            if final_path != ntpath.join(self.root_final_path, *parts[: index + 1]):
                message = "Selected Windows source spelling aliases another entry."
                raise UnsafeWindowsSourceError(message)
            chain.append(
                _RetainedComponent(handle, self.api.identity(handle), final_path)
            )
            parent = handle
        return tuple(chain)

    def _verify_chain(
        self, chain: tuple[_RetainedComponent, ...], parts: tuple[str, ...]
    ) -> None:
        with ExitStack() as handles:
            current = self._open_chain(handles, parts)
            if current is None:
                message = "Selected Windows source disappeared during capture."
                raise RepositoryEvidenceChangedError(message)
            for retained, reopened in zip(chain, current, strict=True):
                if (
                    self.api.identity(retained.handle) != retained.identity
                    or self.api.final_path(retained.handle) != retained.final_path
                    or reopened.identity != retained.identity
                    or reopened.final_path != retained.final_path
                ):
                    message = "Selected Windows source identity changed during capture."
                    raise RepositoryEvidenceChangedError(message)


@contextmanager
def open_windows_source_worktree(
    worktree: Path,
) -> Iterator[WindowsSpecificationSourceWorktree]:
    """Reuse root capability and replacement checks without Vision source policy."""
    try:
        with WindowsRepositoryEvidenceReader().open(worktree) as root:
            yield WindowsSpecificationSourceWorktree(
                api=root.root.api,
                root_handle=root.root.value,
                root_final_path=root.root_final_path,
            )
    except (_WindowsCapabilityError, _WindowsNativeError) as error:
        message = "Windows cannot safely capture source files on this filesystem."
        raise RepositoryEvidenceCapabilityError(message) from error


def _exact_parts(relative_path: str) -> tuple[str, ...]:
    relative = PurePosixPath(relative_path)
    if (
        not relative.parts
        or relative.is_absolute()
        or relative.as_posix() != relative_path
        or ".." in relative.parts
        or "\\" in relative_path
        or ":" in relative_path
        or ntpath.isreserved(relative_path)
    ):
        message = "Selected Windows source must use exact repository-relative names."
        raise UnsafeWindowsSourceError(message)
    return relative.parts
