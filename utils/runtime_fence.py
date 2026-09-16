# utils/runtime_fence.py
"""Nonblocking POSIX lifetime fencing for durable AgileForge state."""

from __future__ import annotations

import errno
import importlib
import os
import stat
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_LOCK_NAME = ".agileforge-runtime.lock"
_PRIVATE_MODE = 0o600
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class _Fcntl(Protocol):
    LOCK_EX: int
    LOCK_SH: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None:
        """Apply one POSIX advisory lock operation."""


def _fcntl_module() -> _Fcntl:
    try:
        module = importlib.import_module("fcntl")
    except ImportError as error:  # pragma: no cover - exercised on Windows
        message = "safe runtime fencing is unsupported on this platform"
        raise FenceError(message) from error
    return cast("_Fcntl", module)


class FenceError(RuntimeError):
    """Raised when a safe runtime fence cannot be acquired."""


def _canonical_root(root: Path) -> Path:
    candidate = root.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        message = f"runtime fence root is unavailable: {candidate}"
        raise FenceError(message) from error
    if stat.S_ISLNK(metadata.st_mode) or resolved != candidate:
        message = f"runtime fence root must not contain a symlink: {candidate}"
        raise FenceError(message)
    if not stat.S_ISDIR(metadata.st_mode):
        message = f"runtime fence root must be a directory: {candidate}"
        raise FenceError(message)
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        message = f"runtime fence root is not owned by this user: {candidate}"
        raise FenceError(message)
    return resolved


def _open_lock(root: Path) -> int:
    if not _O_DIRECTORY or not _O_NOFOLLOW:
        message = "safe runtime fencing is unsupported on this platform"
        raise FenceError(message)
    directory_flags = os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as error:
        message = f"runtime fence root cannot be opened safely: {root}"
        raise FenceError(message) from error
    try:
        lock_flags = os.O_RDWR | os.O_CREAT | _O_CLOEXEC | _O_NOFOLLOW
        try:
            lock_fd = os.open(
                _LOCK_NAME,
                lock_flags,
                _PRIVATE_MODE,
                dir_fd=directory_fd,
            )
        except OSError as error:
            detail = "symlink" if error.errno == errno.ELOOP else "unsafe path"
            message = f"runtime fence lock {detail}: {root / _LOCK_NAME}"
            raise FenceError(message) from error
    finally:
        os.close(directory_fd)

    metadata = os.fstat(lock_fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(lock_fd)
        message = f"runtime fence lock is not a regular file: {root / _LOCK_NAME}"
        raise FenceError(message)
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        os.close(lock_fd)
        message = f"runtime fence lock is not owned by this user: {root / _LOCK_NAME}"
        raise FenceError(message)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(lock_fd)
        message = f"runtime fence lock permissions are unsafe: {root / _LOCK_NAME}"
        raise FenceError(message)
    return lock_fd


@contextmanager
def runtime_fence(root: Path, *, exclusive: bool = False) -> Iterator[None]:
    """Acquire one shared or exclusive nonblocking lifetime fence.

    Shared fences are held by ordinary runtimes. Maintenance operations use an
    exclusive fence. The lock inode remains on disk so deleting and recreating a
    lock path can never split ownership between two inode generations.
    """
    fcntl = _fcntl_module()
    canonical_root = _canonical_root(root)
    lock_fd = _open_lock(canonical_root)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        try:
            fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                message = f"runtime fence acquisition failed: {canonical_root}"
                raise FenceError(message) from error
            mode = "maintenance" if exclusive else "runtime"
            message = f"runtime fence is busy for {mode}: {canonical_root}"
            raise FenceError(message) from error
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
