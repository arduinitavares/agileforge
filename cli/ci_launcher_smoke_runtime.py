"""OS and profile ownership adapters for the CI launcher smoke."""

from __future__ import annotations

import os
import signal
import socket
import subprocess  # nosec B404
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from cli.dev_profiles import profile_paths, reset_profile
from cli.dev_server import (
    LOOPBACK_HOST,
    DashboardConfig,
    ExpectedUIRuntime,
    ManagedProcess,
    UIChild,
    stop_ui,
    wait_for_readiness,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import TextIO

    from cli.dev_main import UiReadyResult

COMMAND_TIMEOUT_SECONDS = 120.0
READY_TIMEOUT_SECONDS = 35.0
STOP_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.05
_SAFE_ENVIRONMENT_NAMES = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "UV_CACHE_DIR",
    "UV_PYTHON_DOWNLOADS",
    "UV_PYTHON_INSTALL_DIR",
)
_SIGNAL_TERM = int(signal.SIGTERM)
_SIGNAL_KILL = int(getattr(signal, "SIGKILL", 9))


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured fixed-command result."""

    exit_code: int
    stdout: str


class Runtime(Protocol):
    """Injectable command, process-group, readiness, and timing boundary."""

    def run(self, arguments: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Run one bounded fixed command."""
        ...

    def start_ui(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        stdout: TextIO,
        stderr: TextIO,
    ) -> ManagedProcess:
        """Start the launcher in a new process group."""
        ...

    def wait_ready(
        self,
        process: ManagedProcess,
        result: UiReadyResult,
        expected_sha: str,
    ) -> DashboardConfig:
        """Validate the existing dashboard readiness contract."""
        ...

    def stop_ui(self, process: ManagedProcess) -> None:
        """Apply the existing bounded stop policy to the process group."""
        ...

    def process_exists(self, process_id: int) -> bool:
        """Return whether one process remains."""
        ...

    def group_exists(self, process_group_id: int) -> bool:
        """Return whether one process group remains."""
        ...

    def endpoint_reachable(self, port: int) -> bool:
        """Return whether one loopback endpoint remains reachable."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic timestamp."""
        ...

    def sleep(self, seconds: float) -> None:
        """Pause a bounded poll."""
        ...


class Profiles(Protocol):
    """Injectable exact profile ownership boundary."""

    def ensure_parent_absent(self) -> None:
        """Reject a pre-existing parent profile."""
        ...

    def snapshot_before_ui(self) -> None:
        """Record roots that predate UI launch."""
        ...

    def cleanup(self) -> None:
        """Remove only owned parent and new matching children."""
        ...


def _target_exists(
    identifier: int,
    *,
    signal_target: Callable[[int, int], None],
) -> bool:
    try:
        signal_target(identifier, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(process_group_id: int, selected_signal: int) -> None:
    signal_group = cast(
        "Callable[[int, int], None] | None",
        getattr(os, "killpg", None),
    )
    if signal_group is None:
        message = "process-group signaling is unavailable"
        raise RuntimeError(message)
    signal_group(process_group_id, selected_signal)


@dataclass(slots=True)
class ProcessGroup:
    """Adapt one new-session launcher to the existing UI stop interface."""

    process: ManagedProcess

    @property
    def pid(self) -> int:
        """Return the process-group identifier."""
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        """Return the launcher status."""
        return self.process.returncode

    def poll(self) -> int | None:
        """Remain active while any tracked process-group member exists."""
        status = self.process.poll()
        if status is not None and self.group_exists():
            return None
        return status

    def wait(self, timeout: float | None = None) -> int:
        """Wait for launcher reaping and complete group exit."""
        limit = STOP_TIMEOUT_SECONDS if timeout is None else timeout
        deadline = time.monotonic() + limit
        while self.group_exists():
            self.process.poll()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd="agileforge-dev ui", timeout=limit)
            time.sleep(POLL_SECONDS)
        return self.process.wait(timeout=max(0.0, deadline - time.monotonic()))

    def terminate(self) -> None:
        """Send TERM to the complete launcher process group."""
        _signal_group(self.pid, _SIGNAL_TERM)

    def kill(self) -> None:
        """Send KILL to the complete launcher process group."""
        _signal_group(self.pid, _SIGNAL_KILL)

    def group_exists(self) -> bool:
        """Return whether the exact process group remains."""
        return _target_exists(self.pid, signal_target=_signal_group)


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """Production subprocess and loopback boundary."""

    environment: Mapping[str, str]

    def run(self, arguments: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Run one bounded fixed command with captured output."""
        completed = subprocess.run(  # noqa: S603  # nosec B603
            arguments,
            cwd=cwd,
            env=dict(self.environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return CommandResult(completed.returncode, completed.stdout)

    def start_ui(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        stdout: TextIO,
        stderr: TextIO,
    ) -> ManagedProcess:
        """Start one attached non-reload launcher in a new process group."""
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            arguments,
            cwd=cwd,
            env=dict(self.environment),
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        return ProcessGroup(process)

    def wait_ready(
        self,
        process: ManagedProcess,
        result: UiReadyResult,
        expected_sha: str,
    ) -> DashboardConfig:
        """Delegate readiness validation to dev_server."""
        return wait_for_readiness(
            UIChild(process=process, port=result.port),
            expected=ExpectedUIRuntime(
                checkout_root=result.checkout,
                commit=expected_sha,
                business_database=result.business_database,
                trace_database=result.trace_database,
                process_id=None,
                launch_nonce=result.launch_nonce,
            ),
            timeout=READY_TIMEOUT_SECONDS,
        )

    def stop_ui(self, process: ManagedProcess) -> None:
        """Delegate finite TERM/KILL escalation to dev_server."""
        stop_ui(UIChild(process=process, port=0), timeout=STOP_TIMEOUT_SECONDS)

    def process_exists(self, process_id: int) -> bool:
        """Return whether one process remains."""
        return _target_exists(process_id, signal_target=os.kill)

    def group_exists(self, process_group_id: int) -> bool:
        """Return whether one process group remains."""
        return _target_exists(process_group_id, signal_target=_signal_group)

    def endpoint_reachable(self, port: int) -> bool:
        """Probe only the verified IPv4 loopback port."""
        try:
            with socket.create_connection((LOOPBACK_HOST, port), timeout=0.5):
                return True
        except OSError:
            return False

    def monotonic(self) -> float:
        """Return a monotonic timestamp."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Pause one bounded poll."""
        time.sleep(seconds)


@dataclass(slots=True)
class LocalProfiles:
    """Own one parent and only newly created matching UI profiles."""

    checkout_root: Path
    parent: str
    initial: frozenset[Path]
    before_ui: frozenset[Path]

    @classmethod
    def create(cls, checkout_root: Path, parent: str) -> LocalProfiles:
        """Snapshot profile roots before any command creates state."""
        base = profile_paths(checkout_root, parent).root.parent
        initial = cls._snapshot(base)
        return cls(checkout_root, parent, initial, initial)

    @staticmethod
    def _snapshot(base: Path) -> frozenset[Path]:
        if not base.exists():
            return frozenset()
        return frozenset(base.iterdir())

    @property
    def parent_root(self) -> Path:
        """Return the exact parent profile root."""
        return profile_paths(self.checkout_root, self.parent).root

    def ensure_parent_absent(self) -> None:
        """Reject state not created by this smoke."""
        if self.parent_root in self.initial or self.parent_root.exists():
            message = "parent profile already exists"
            raise ValueError(message)

    def snapshot_before_ui(self) -> None:
        """Record roots that predate UI process acquisition."""
        self.before_ui = self._snapshot(self.parent_root.parent)

    def cleanup(self) -> None:
        """Remove new matching children and the owned parent."""
        prefix = f"{self.parent}.ui-"
        current = self._snapshot(self.parent_root.parent)
        children = sorted(
            root.name
            for root in current.difference(self.before_ui)
            if root.name.startswith(prefix)
        )
        for child in children:
            reset_profile(self.checkout_root, child, child)
        if self.parent_root not in self.initial and self.parent_root.exists():
            reset_profile(self.checkout_root, self.parent, self.parent)


def safe_environment(parent: Mapping[str, str]) -> dict[str, str]:
    """Copy only non-credential runtime values into launcher children."""
    environment = {
        name: parent[name] for name in _SAFE_ENVIRONMENT_NAMES if parent.get(name)
    }
    environment.setdefault("PATH", os.defpath)
    environment["NO_COLOR"] = "1"
    environment["UV_NO_PROGRESS"] = "1"
    return environment
