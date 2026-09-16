"""Managed dashboard listeners with local identity probes and owned processes."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess  # nosec B404
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast
from urllib.request import urlopen

from utils.secret_redaction import forward_redacted_output

if TYPE_CHECKING:
    from collections.abc import Mapping
LOOPBACK_HOST = "127.0.0.1"
# The container listener is reachable only through explicit loopback publication.
CONTAINER_HOST = "0.0.0.0"  # noqa: S104  # nosec B104
READINESS_PATH = "/api/dashboard/config"
_DEFAULT_PORT_ATTEMPTS = 5
_DEFAULT_STOP_TIMEOUT = 5.0
_DEFAULT_POLL_INTERVAL = 0.05
_HTTP_OK = 200


class ManagedProcess(Protocol):
    """Process operations required by the dashboard lifecycle."""

    pid: int
    returncode: int | None

    def poll(self) -> int | None:
        """Return the child status when it has exited."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Wait for child exit."""
        ...

    def terminate(self) -> None:
        """Request graceful child termination."""
        ...

    def kill(self) -> None:
        """Force child termination."""
        ...


class _SpawnOptions(TypedDict, total=False):
    start_new_session: bool
    stderr: int


def _signal_group(group_id: int, number: int) -> None:
    if not hasattr(os, "killpg"):
        message = "owned process groups require POSIX signals"
        raise RuntimeError(message)
    os.killpg(group_id, number)


@dataclass(frozen=True, slots=True)
class PosixProcessGroup:
    """Retain ownership of a new session after its original leader exits."""

    process: ManagedProcess

    @property
    def pid(self) -> int:
        """Return the acquired process-group identifier."""
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        """Return the leader's exit status."""
        return self.process.returncode

    def _exists(self) -> bool:
        try:
            _signal_group(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def poll(self) -> int | None:
        """Remain live while a member of the acquired group still exists."""
        result = self.process.poll()
        return None if self._exists() else result

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the leader normally, or for complete shutdown when bounded."""
        if timeout is None:
            return self.process.wait()
        deadline = time.monotonic() + timeout
        while self._exists():
            self.process.poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    cmd="dashboard process group", timeout=timeout
                )
            time.sleep(min(_DEFAULT_POLL_INTERVAL, remaining))
        return self.process.wait(timeout=max(0.0, deadline - time.monotonic()))

    def _signal(self, number: int) -> None:
        with suppress(ProcessLookupError):
            _signal_group(self.pid, number)

    def terminate(self) -> None:
        """Request shutdown of every owned process."""
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        """Escalate only for the acquired process group."""
        if not hasattr(signal, "SIGKILL"):
            message = "owned process groups require POSIX signals"
            raise RuntimeError(message)
        self._signal(signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class UIChild:
    """One tracked uvicorn child and its selected loopback port."""

    process: ManagedProcess
    port: int
    output_thread: Thread | None = None

    @property
    def url(self) -> str:
        """Return the child dashboard origin."""
        return f"http://{LOOPBACK_HOST}:{self.port}"


@dataclass(frozen=True, slots=True)
class ExpectedUIRuntime:
    """Exact runtime identity required from the readiness endpoint."""

    checkout_root: Path
    commit: str
    business_database: Path
    trace_database: Path
    process_id: int | None
    launch_nonce: str
    state_id: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """Parsed non-secret identity returned by one dashboard process."""

    status: Literal["ready"]
    process_id: int
    checkout_root: Path
    commit: str
    business_database: Path
    trace_database: Path
    launch_nonce: str | None
    state_id: str | None = None


class UIReadinessError(RuntimeError):
    """Dashboard child did not become ready within its contract."""


class UIRuntimeMismatchError(UIReadinessError):
    """Dashboard readiness came from a different runtime."""


def _open_loopback_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def select_loopback_port(*, max_attempts: int = _DEFAULT_PORT_ATTEMPTS) -> int:
    """Select an available IPv4 loopback port with bounded retries."""
    if max_attempts <= 0:
        message = "max_attempts must be greater than zero"
        raise ValueError(message)
    last_error: OSError | None = None
    for _attempt in range(max_attempts):
        try:
            with _open_loopback_socket() as listener:
                listener.bind((LOOPBACK_HOST, 0))
                _host, port = listener.getsockname()
                return int(port)
        except OSError as error:
            last_error = error
    message = f"unable to select a loopback port after {max_attempts} attempts"
    raise OSError(message) from last_error


def _ui_python(
    environment: Mapping[str, str], *, reload: bool
) -> tuple[str, dict[str, str]]:
    """Own the serving interpreter directly for Windows venv non-reload UI.

    CPython 3.13 multiprocessing uses this same base-executable/launcher
    pair to avoid the Windows venv redirector while retaining the venv.
    Keep reload on its existing supervisor/redirector cleanup path.
    """
    child_environment = dict(environment)
    executable = sys.executable
    if sys.platform != "win32" or reload:
        return executable, child_environment
    if not isinstance(executable, str) or not executable:
        message = "Windows UI requires a valid Python executable"
        raise UIReadinessError(message)
    base_executable = getattr(sys, "_base_executable", None)
    if not isinstance(base_executable, str) or not base_executable:
        message = "Windows UI requires a valid base Python executable"
        raise UIReadinessError(message)
    for value in (executable, base_executable):
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            message = "Windows UI requires absolute existing Python executables"
            raise UIReadinessError(message)
    if os.path.normcase(executable) == os.path.normcase(base_executable):
        return executable, child_environment
    child_environment["__PYVENV_LAUNCHER__"] = executable
    return base_executable, child_environment


def start_ui(  # noqa: PLR0913
    *,
    checkout_root: Path,
    environment: Mapping[str, str],
    port: int,
    reload: bool,
    host: str = LOOPBACK_HOST,
    owned_process_group: bool = False,
    redact_values: tuple[str, ...] = (),
) -> UIChild:
    """Start one fixed-argv uvicorn child in its validated checkout."""
    if host not in {LOOPBACK_HOST, CONTAINER_HOST}:
        message = "host must be 127.0.0.1 or the explicit container host 0.0.0.0"
        raise ValueError(message)
    if owned_process_group and os.name != "posix":
        message = "owned process groups require a POSIX runtime"
        raise ValueError(message)
    executable, child_environment = _ui_python(environment, reload=reload)
    arguments = (
        executable,
        "-m",
        "uvicorn",
        "api:app",
        "--host",
        host,
        "--port",
        str(port),
    )
    if reload:
        arguments = (*arguments, "--reload")
    options: _SpawnOptions = {"start_new_session": True} if owned_process_group else {}
    if redact_values:
        options["stderr"] = subprocess.STDOUT
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        arguments,
        cwd=checkout_root,
        env=child_environment,
        stdout=subprocess.PIPE if redact_values else sys.stderr,
        **options,
    )
    managed: ManagedProcess = (
        PosixProcessGroup(process) if owned_process_group else process
    )
    child = UIChild(process=managed, port=port)
    if redact_values:
        try:
            if process.stdout is None:
                message = "supervised output pipe is unavailable"
                raise RuntimeError(message)  # noqa: TRY301 - cleanup owns the child
            output_thread = Thread(
                target=forward_redacted_output,
                args=(process.stdout, sys.stderr, redact_values),
                daemon=True,
                name="agileforge-redacted-output",
            )
            output_thread.start()
            child = UIChild(process=managed, port=port, output_thread=output_thread)
        except BaseException:
            stop_ui(child)
            raise
    return child


def _required_path(payload: dict[str, object], key: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str):
        message = "dashboard readiness returned an invalid payload"
        raise UIReadinessError(message)
    path = Path(value)
    if not path.is_absolute():
        message = "dashboard readiness returned an invalid payload"
        raise UIReadinessError(message)
    return path


def _parse_dashboard_config(payload: object) -> DashboardConfig:
    if not isinstance(payload, dict):
        message = "dashboard readiness returned an invalid payload"
        raise UIReadinessError(message)
    config_payload = cast("dict[str, object]", payload)
    if config_payload.get("status") != "ready":
        message = "dashboard readiness returned an invalid payload"
        raise UIReadinessError(message)
    process_id = config_payload.get("process_id")
    commit = config_payload.get("commit")
    launch_nonce = config_payload.get("launch_nonce")
    state_id = config_payload.get("state_id")
    if (
        not isinstance(process_id, int)
        or isinstance(process_id, bool)
        or process_id <= 0
        or not isinstance(commit, str)
        or (
            launch_nonce is not None
            and (not isinstance(launch_nonce, str) or not launch_nonce)
        )
        or (state_id is not None and (not isinstance(state_id, str) or not state_id))
    ):
        message = "dashboard readiness returned an invalid payload"
        raise UIReadinessError(message)
    return DashboardConfig(
        status="ready",
        process_id=process_id,
        checkout_root=_required_path(config_payload, "checkout_root"),
        commit=commit,
        business_database=_required_path(config_payload, "business_database"),
        trace_database=_required_path(config_payload, "trace_database"),
        launch_nonce=launch_nonce,
        state_id=state_id,
    )


def _validate_runtime_identity(
    config: DashboardConfig,
    expected: ExpectedUIRuntime,
) -> None:
    try:
        canonical_checkout = config.checkout_root.resolve(strict=True)
    except OSError:
        canonical_checkout = None
    matches = (
        canonical_checkout is not None
        and config.checkout_root == canonical_checkout
        and canonical_checkout == expected.checkout_root
        and config.commit == expected.commit
        and config.business_database == expected.business_database
        and config.trace_database == expected.trace_database
        and (expected.process_id is None or config.process_id == expected.process_id)
        and config.launch_nonce == expected.launch_nonce
        and config.state_id == expected.state_id
    )
    if not matches:
        message = "dashboard readiness identity mismatch"
        raise UIRuntimeMismatchError(message)


def _read_ready_payload(
    child: UIChild,
    *,
    expected: ExpectedUIRuntime,
    timeout: float,
) -> DashboardConfig:
    url = f"{child.url}{READINESS_PATH}"
    with urlopen(  # noqa: S310  # nosec B310
        url,
        timeout=timeout,
    ) as response:
        if response.status != _HTTP_OK:
            message = f"dashboard readiness returned HTTP {response.status}"
            raise UIReadinessError(message)
        decoded = json.loads(response.read().decode("utf-8"))
    config = _parse_dashboard_config(decoded)
    _validate_runtime_identity(config, expected)
    return config


def wait_for_readiness(
    child: UIChild,
    *,
    expected: ExpectedUIRuntime,
    timeout: float,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> DashboardConfig:
    """Poll the fixed readiness endpoint until success, exit, or timeout."""
    if timeout <= 0:
        message = "readiness timeout must be greater than zero"
        raise ValueError(message)
    if poll_interval <= 0:
        message = "poll interval must be greater than zero"
        raise ValueError(message)

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = f"dashboard readiness timed out after {timeout:g} seconds"
            raise UIReadinessError(message)
        child_status = child.process.poll()
        if child_status is not None:
            message = f"dashboard child exited before readiness: {child_status}"
            raise UIReadinessError(message)
        try:
            return _read_ready_payload(
                child,
                expected=expected,
                timeout=min(1.0, remaining),
            )
        except UIRuntimeMismatchError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, UIReadinessError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                message = f"dashboard readiness timed out after {timeout:g} seconds"
                raise UIReadinessError(message) from None
            time.sleep(min(poll_interval, remaining))


def stop_ui(child: UIChild, *, timeout: float = _DEFAULT_STOP_TIMEOUT) -> None:
    """Terminate one tracked child and escalate only after a finite wait."""
    if timeout <= 0:
        message = "stop timeout must be greater than zero"
        raise ValueError(message)
    try:
        if child.process.poll() is None:
            child.process.terminate()
            try:
                child.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.process.kill()
                child.process.wait(timeout=timeout)
    finally:
        output_thread = getattr(child, "output_thread", None)
        if output_thread is not None:
            output_thread.join(timeout=timeout)
