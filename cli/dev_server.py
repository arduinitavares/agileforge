"""Managed loopback-only dashboard child processes."""

from __future__ import annotations

import json
import socket
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.request import urlopen

if TYPE_CHECKING:
    from collections.abc import Mapping
LOOPBACK_HOST = "127.0.0.1"
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


@dataclass(frozen=True, slots=True)
class UIChild:
    """One tracked uvicorn child and its selected loopback port."""

    process: ManagedProcess
    port: int

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


def start_ui(
    *,
    checkout_root: Path,
    environment: Mapping[str, str],
    port: int,
    reload: bool,
) -> UIChild:
    """Start one fixed-argv uvicorn child in its validated checkout."""
    arguments = (
        sys.executable,
        "-m",
        "uvicorn",
        "api:app",
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(port),
    )
    if reload:
        arguments = (*arguments, "--reload")
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        arguments,
        cwd=checkout_root,
        env=dict(environment),
        stdout=sys.stderr,
    )
    return UIChild(process=process, port=port)


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
    if (
        not isinstance(process_id, int)
        or isinstance(process_id, bool)
        or process_id <= 0
        or not isinstance(commit, str)
        or (
            launch_nonce is not None
            and (not isinstance(launch_nonce, str) or not launch_nonce)
        )
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
    if child.process.poll() is not None:
        return
    child.process.terminate()
    try:
        child.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.process.kill()
        child.process.wait(timeout=timeout)
