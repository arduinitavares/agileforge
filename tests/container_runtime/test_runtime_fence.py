"""Real-process tests for the durable-state maintenance fence."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from typing import Protocol

import pytest

from utils.runtime_fence import FenceError, runtime_fence

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="maintenance fencing requires POSIX flock",
)
_EXCLUSIVE_ACQUIRED_MESSAGE = "exclusive fence acquired while shared holder was live"
_SHARED_ACQUIRED_MESSAGE = "shared fence acquired while maintenance was live"


class _Event(Protocol):
    def set(self) -> None:
        """Signal the event."""

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the event."""


class _Process(Protocol):
    exitcode: int | None

    def start(self) -> None:
        """Start the process."""

    def join(self, timeout: float | None = None) -> None:
        """Join the process."""

    def is_alive(self) -> bool:
        """Return whether the process is live."""


def _hold_fence(
    root: str,
    exclusive: bool,
    ready: _Event,
    release: _Event,
) -> None:
    with runtime_fence(Path(root), exclusive=exclusive):
        ready.set()
        release.wait(timeout=10)


def _acquire_then_exit(
    root: str,
    ready: _Event,
) -> None:
    with runtime_fence(Path(root), exclusive=True):
        ready.set()
        os._exit(0)


def _holder(
    root: Path,
    *,
    exclusive: bool,
) -> tuple[
    _Process,
    _Event,
]:
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_fence,
        args=(str(root), exclusive, ready, release),
    )
    process.start()
    assert ready.wait(timeout=10), "child did not acquire maintenance fence"
    return process, release


def _stop_holder(
    process: _Process,
    release: _Event,
) -> None:
    release.set()
    process.join(timeout=10)
    assert process.exitcode == 0


def test_shared_holder_prevents_exclusive_maintenance(tmp_path: Path) -> None:
    """Removing nonblocking exclusive flock would let maintenance race a reader."""
    process, release = _holder(tmp_path, exclusive=False)
    try:
        with (
            pytest.raises(FenceError, match="busy"),
            runtime_fence(tmp_path, exclusive=True),
        ):
            pytest.fail(_EXCLUSIVE_ACQUIRED_MESSAGE)  # ty: ignore[invalid-argument-type]
    finally:
        _stop_holder(process, release)


def test_exclusive_holder_prevents_new_shared_runtime(tmp_path: Path) -> None:
    """Changing startup to a blocking/shared-only lock would admit a live runtime."""
    process, release = _holder(tmp_path, exclusive=True)
    try:
        with pytest.raises(FenceError, match="busy"), runtime_fence(tmp_path):
            pytest.fail(_SHARED_ACQUIRED_MESSAGE)  # ty: ignore[invalid-argument-type]
    finally:
        _stop_holder(process, release)


def test_multiple_shared_runtime_holders_are_allowed(tmp_path: Path) -> None:
    """Making ordinary runtime ownership exclusive would break parent/child use."""
    process, release = _holder(tmp_path, exclusive=False)
    try:
        with runtime_fence(tmp_path):
            assert process.is_alive()
    finally:
        _stop_holder(process, release)


def test_dead_process_releases_fence(tmp_path: Path) -> None:
    """Leaking a lock after process death would require unsafe manual recovery."""
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    process = context.Process(
        target=_acquire_then_exit,
        args=(str(tmp_path), ready),
    )
    process.start()
    assert ready.wait(timeout=10)
    process.join(timeout=10)
    assert process.exitcode == 0

    with runtime_fence(tmp_path, exclusive=True):
        pass


def test_symlinked_lock_path_is_rejected(tmp_path: Path) -> None:
    """Following a pre-planted lock symlink would fence the wrong inode."""
    target = tmp_path / "outside.lock"
    target.write_bytes(b"")
    (tmp_path / ".agileforge-runtime.lock").symlink_to(target)

    with pytest.raises(FenceError, match="symlink"), runtime_fence(tmp_path):
        pytest.fail("symlinked lock was accepted")  # ty: ignore[invalid-argument-type]


def test_symlinked_root_is_rejected(tmp_path: Path) -> None:
    """Resolving a caller-supplied root alias would hide its real lock scope."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(FenceError, match="symlink"), runtime_fence(alias):
        pytest.fail("symlinked root was accepted")  # ty: ignore[invalid-argument-type]
