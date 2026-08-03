"""Managed loopback-only dashboard child processes."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast
from urllib.request import urlopen

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from workflow.contracts import JsonObject

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


class UIReadinessError(RuntimeError):
    """Dashboard child did not become ready within its contract."""


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
    process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
        arguments,
        cwd=checkout_root,
        env=dict(environment),
        stdout=sys.stderr,
    )
    return UIChild(process=process, port=port)


def _read_ready_payload(child: UIChild, *, timeout: float) -> JsonObject:
    url = f"{child.url}{READINESS_PATH}"
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed loopback URL
        if response.status != _HTTP_OK:
            message = f"dashboard readiness returned HTTP {response.status}"
            raise UIReadinessError(message)
        decoded = json.loads(response.read().decode("utf-8"))
    if not isinstance(decoded, dict) or decoded.get("status") != "ready":
        message = "dashboard readiness returned an invalid payload"
        raise UIReadinessError(message)
    return cast("JsonObject", decoded)


def wait_for_readiness(
    child: UIChild,
    *,
    timeout: float,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> JsonObject:
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
                timeout=min(1.0, remaining),
            )
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
