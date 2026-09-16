"""Linux process and fence behavior for the container command wrapper."""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from utils.runtime_fence import FenceError, runtime_fence

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="container command cleanup requires POSIX signals"
)

_DEADLINE_SECONDS = 15
_POLL_SECONDS = 0.05
_INTERRUPTED_EXIT_CODE = 130


def _run_container_exec(
    workspace_root: Path, command: list[str]
) -> subprocess.Popen[str]:
    """Start the wrapper in a separate process with one temporary fence root."""
    wrapper = (
        "from pathlib import Path; "
        "from scripts.container_exec import main; "
        "raise SystemExit(main(sys.argv[2:], workspace_root=Path(sys.argv[1])))"
    )
    return subprocess.Popen(  # noqa: S603 # nosec B603
        [
            sys.executable,
            "-c",
            f"import sys; {wrapper}",
            str(workspace_root),
            "--",
            *command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_path(path: Path) -> None:
    """Wait for the command fixture to report that its descendant started."""
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(_POLL_SECONDS)
    pytest.fail(f"timed out waiting for {path}")  # ty: ignore[invalid-argument-type]


def _wait_for_exit(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect wrapper output without allowing a hung cleanup to stall the test."""
    try:
        return process.communicate(timeout=_DEADLINE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=_DEADLINE_SECONDS)
        pytest.fail(
            f"container_exec did not stop: stdout={stdout!r} stderr={stderr!r}"  # ty: ignore[invalid-argument-type]
        )


def _pid_is_gone(pid: int) -> bool:
    """Return whether a known fixture process has exited."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _wait_for_pid_exit(pid: int) -> None:
    """Bound the assertion that the explicitly owned descendant was cleaned up."""
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if _pid_is_gone(pid):
            return
        time.sleep(_POLL_SECONDS)
    pytest.fail(
        f"owned descendant {pid} survived container_exec shutdown"  # ty: ignore[invalid-argument-type]
    )


def test_container_exec_releases_fence_after_normal_command(tmp_path: Path) -> None:
    """A successful command releases the temporary workspace fence."""
    process = _run_container_exec(
        tmp_path, [sys.executable, "-c", "print('container-exec-success')"]
    )
    stdout, stderr = _wait_for_exit(process)

    assert process.returncode == 0, stderr
    assert stdout == "container-exec-success\n"
    with runtime_fence(tmp_path, exclusive=True):
        pass


def test_sigterm_cleans_owned_descendant_then_releases_fence(tmp_path: Path) -> None:
    """SIGTERM interrupts the wrapper and cleans up only its owned process group."""
    descendant_pid_file = tmp_path / "owned-descendant.pid"
    command_code = (
        "import os, pathlib, signal, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    unrelated = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    process = _run_container_exec(
        tmp_path,
        [sys.executable, "-c", command_code, str(descendant_pid_file)],
    )
    descendant_pid: int | None = None
    try:
        _wait_for_path(descendant_pid_file)
        descendant_pid = int(descendant_pid_file.read_text(encoding="utf-8"))
        with (
            pytest.raises(FenceError, match="busy"),
            runtime_fence(tmp_path, exclusive=True),
        ):
            pytest.fail(
                "container_exec released its live runtime fence"  # ty: ignore[invalid-argument-type]
            )

        process.send_signal(signal.SIGTERM)
        _stdout, stderr = _wait_for_exit(process)

        assert process.returncode == _INTERRUPTED_EXIT_CODE, stderr
        _wait_for_pid_exit(descendant_pid)
        assert unrelated.poll() is None
        with runtime_fence(tmp_path, exclusive=True):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=_DEADLINE_SECONDS)
        if descendant_pid is not None and not _pid_is_gone(descendant_pid):
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGKILL)
        if unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=_DEADLINE_SECONDS)
