"""Tests for managed worktree-local dashboard processes."""

from __future__ import annotations

import importlib
import json
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from git import Git

from cli.dev_profiles import (
    initialize_profile_record,
    profile_paths,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

_EXPECTED_TABLES = ("projects", "spec_registry", "workflow_events")
_AUTO_PORTS = (8101, 8102)
_EXPECTED_POLL_ATTEMPTS = 2
_MAX_PORT_ATTEMPTS = 3
_SELECTED_PORT = 43210
_UI_PORT = 8123


def _module() -> ModuleType:
    return importlib.import_module("cli.dev_server")


def _main_module() -> ModuleType:
    return importlib.import_module("cli.dev_main")


def _git(checkout: Path, *arguments: str) -> str:
    output = Git().execute(command=["git", "-C", str(checkout), *arguments])
    return cast("str", output).strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a minimal checkout containing profile fingerprint inputs."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-b", "feature/dev-server")
    _git(root, "config", "user.name", "Developer Server Tests")
    _git(root, "config", "user.email", "dev-server@example.invalid")
    (root / "config").mkdir()
    (root / "models").mkdir()
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    (root / "config" / "models.yaml").write_text(
        "models:\n  default: fixture\n",
        encoding="utf-8",
    )
    (root / "agile_sqlmodel.py").write_text(
        "raise AssertionError('the injected runner owns this test')\n",
        encoding="utf-8",
    )
    (root / "models" / "core.py").write_text("SCHEMA = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root


@dataclass(slots=True)
class FakeProcess:
    """Record lifecycle calls for one managed child."""

    pid: int = 1234
    returncode: int | None = None
    waits: list[float | None] = field(default_factory=list)
    terminated: int = 0
    killed: int = 0
    timeout_once: bool = False

    def poll(self) -> int | None:
        """Return the configured child status."""
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Record waits and optionally force the kill fallback."""
        self.waits.append(timeout)
        if self.timeout_once:
            self.timeout_once = False
            assert timeout is not None
            raise subprocess.TimeoutExpired(cmd="uvicorn", timeout=timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self) -> None:
        """Record graceful termination."""
        self.terminated += 1

    def kill(self) -> None:
        """Record forced termination."""
        self.killed += 1


@dataclass(frozen=True, slots=True)
class FakeUIChild:
    """Managed child shape used by launcher-level tests."""

    process: FakeProcess
    port: int

    @property
    def url(self) -> str:
        """Return the fake child origin."""
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True, slots=True)
class FakeResponse:
    """Minimal successful dashboard-config response."""

    payload: bytes
    status: int = 200

    def __enter__(self) -> FakeResponse:
        """Open the fake response context."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the fake response context."""

    def read(self) -> bytes:
        """Return the configured response body."""
        return self.payload


@dataclass(slots=True)
class BootstrapRunner:
    """Emulate profile bootstrap and current-commit lookup."""

    checkout: Path

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> object:
        """Run one fixed developer command."""
        module = _main_module()
        if arguments == ("uv", "lock", "--check"):
            return module.CommandResult(arguments=arguments, exit_code=0)
        if arguments == ("uv", "--version"):
            return module.CommandResult(
                arguments=arguments,
                exit_code=0,
                stdout="uv 0.8.0\n",
            )
        if arguments == (
            "git",
            "-C",
            str(self.checkout),
            "rev-parse",
            "HEAD",
        ):
            return module.CommandResult(
                arguments=arguments,
                exit_code=0,
                stdout=f"{_git(self.checkout, 'rev-parse', 'HEAD')}\n",
            )
        if arguments == (sys.executable, str(self.checkout / "agile_sqlmodel.py")):
            assert cwd == self.checkout
            assert env is not None
            business_path = Path(env["AGILEFORGE_DB_URL"].removeprefix("sqlite:///"))
            with sqlite3.connect(business_path) as connection:
                for table in _EXPECTED_TABLES:
                    connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
            return module.CommandResult(arguments=arguments, exit_code=0)
        message = f"unexpected command: {arguments!r}"
        raise AssertionError(message)


def _create_profile(checkout: Path, name: str = "local") -> None:
    profile = initialize_profile_record(
        checkout,
        name,
        now=datetime(2026, 8, 3, 12, tzinfo=UTC),
    )
    with sqlite3.connect(profile.business_database) as connection:
        for table in _EXPECTED_TABLES:
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')


def test_select_loopback_port_binds_only_ipv4_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never probe wildcard or externally reachable interfaces."""
    bindings: list[tuple[str, int]] = []

    class FakeSocket:
        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, address: tuple[str, int]) -> None:
            bindings.append(address)

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", _SELECTED_PORT)

    monkeypatch.setattr(_module(), "_open_loopback_socket", FakeSocket)

    assert _module().select_loopback_port() == _SELECTED_PORT
    assert bindings == [("127.0.0.1", 0)]


def test_select_loopback_port_retries_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry transient local bind failures without looping forever."""
    attempts = 0

    class FailingSocket:
        def __enter__(self) -> FailingSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, _address: tuple[str, int]) -> None:
            nonlocal attempts
            attempts += 1
            message = "busy"
            raise OSError(message)

    monkeypatch.setattr(_module(), "_open_loopback_socket", FailingSocket)

    with pytest.raises(OSError, match="unable to select a loopback port"):
        _module().select_loopback_port(max_attempts=_MAX_PORT_ATTEMPTS)

    assert attempts == _MAX_PORT_ATTEMPTS


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_select_loopback_port_returns_an_available_local_port() -> None:
    """Select a real port that can immediately be rebound on loopback."""
    port = _module().select_loopback_port()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))


def test_start_ui_uses_fixed_uvicorn_arguments_and_exact_environment(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
) -> None:
    """Start one non-reloading child with no shell or inherited environment."""
    process = FakeProcess()
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_popen(
        arguments: tuple[str, ...],
        **options: object,
    ) -> FakeProcess:
        calls.append((arguments, options))
        return process

    monkeypatch.setattr(_module().subprocess, "Popen", fake_popen)
    environment = {"AGILEFORGE_DB_URL": "sqlite:////tmp/business.sqlite3"}

    child = _module().start_ui(
        checkout_root=checkout,
        environment=environment,
        port=_UI_PORT,
        reload=False,
    )

    assert child.process is process
    assert child.port == _UI_PORT
    assert calls == [
        (
            (
                sys.executable,
                "-m",
                "uvicorn",
                "api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(_UI_PORT),
            ),
            {
                "cwd": checkout,
                "env": environment,
                "stdout": sys.stderr,
            },
        )
    ]


def test_start_ui_adds_reload_only_for_foreground_mode(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
) -> None:
    """Use uvicorn's foreground reload supervisor only when requested."""
    arguments_seen: tuple[str, ...] | None = None

    def fake_popen(arguments: tuple[str, ...], **_options: object) -> FakeProcess:
        nonlocal arguments_seen
        arguments_seen = arguments
        return FakeProcess()

    monkeypatch.setattr(_module().subprocess, "Popen", fake_popen)

    _module().start_ui(
        checkout_root=checkout,
        environment={},
        port=_UI_PORT,
        reload=True,
    )

    assert arguments_seen is not None
    assert arguments_seen[-1] == "--reload"


def test_wait_for_readiness_returns_valid_dashboard_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return only a valid ready payload from the fixed local endpoint."""
    child = _module().UIChild(process=FakeProcess(), port=_UI_PORT)
    payload = {
        "status": "ready",
        "checkout_root": "/workspace/checkout",
        "commit": "a" * 40,
        "business_database": "/workspace/business.sqlite3",
        "trace_database": "/workspace/trace.sqlite3",
    }
    urls: list[str] = []

    def fake_urlopen(url: str, *, timeout: float) -> FakeResponse:
        assert timeout > 0
        urls.append(url)
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(_module(), "urlopen", fake_urlopen)

    assert _module().wait_for_readiness(child, timeout=1) == payload
    assert urls == [f"http://127.0.0.1:{_UI_PORT}/api/dashboard/config"]


def test_wait_for_readiness_times_out_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop polling at the caller's finite deadline."""
    child = _module().UIChild(process=FakeProcess(), port=_UI_PORT)
    moments = iter((0.0, 0.0, 0.5, 0.5, 1.0))
    attempts = 0

    def fail_urlopen(_url: str, *, timeout: float) -> FakeResponse:
        nonlocal attempts
        assert timeout > 0
        attempts += 1
        message = "not ready"
        raise OSError(message)

    monkeypatch.setattr(_module(), "urlopen", fail_urlopen)
    monkeypatch.setattr(_module().time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(_module().time, "sleep", lambda _seconds: None)

    with pytest.raises(_module().UIReadinessError, match="timed out"):
        _module().wait_for_readiness(child, timeout=1, poll_interval=0.01)

    assert attempts == _EXPECTED_POLL_ATTEMPTS


def test_stop_ui_terminates_then_kills_only_after_timeout() -> None:
    """Escalate one tracked child after a finite graceful wait."""
    graceful = FakeProcess()
    stubborn = FakeProcess(timeout_once=True)

    _module().stop_ui(_module().UIChild(process=graceful, port=8001), timeout=2)
    _module().stop_ui(_module().UIChild(process=stubborn, port=8002), timeout=2)

    assert (graceful.terminated, graceful.killed, graceful.waits) == (1, 0, [2])
    assert (stubborn.terminated, stubborn.killed, stubborn.waits) == (
        1,
        1,
        [2, 2],
    )


def test_ui_json_readiness_preserves_normal_profile_across_restarts(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reuse a validated development database and emit one readiness object."""
    _create_profile(checkout)
    profile = profile_paths(checkout, "local")
    with sqlite3.connect(profile.business_database) as connection:
        connection.execute("INSERT INTO projects (id) VALUES (41)")
    started_environments: list[dict[str, str]] = []

    def fake_start_ui(**options: object) -> object:
        environment = cast("Mapping[str, str]", options["environment"])
        started_environments.append(dict(environment))
        return FakeUIChild(process=FakeProcess(), port=_UI_PORT)

    monkeypatch.setattr(_main_module(), "start_ui", fake_start_ui)
    monkeypatch.setattr(
        _main_module(),
        "wait_for_readiness",
        lambda *_args, **_kwargs: {"status": "ready"},
    )
    monkeypatch.setattr(_main_module(), "stop_ui", lambda *_args, **_kwargs: None)
    runner = BootstrapRunner(checkout)

    for _attempt in range(2):
        assert (
            _main_module().main(
                [
                    "ui",
                    "--profile",
                    "local",
                    "--port",
                    str(_UI_PORT),
                    "--json",
                ],
                checkout_root=checkout,
                runner=runner,
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ready"
        assert payload["profile"] == "local"
        assert payload["port"] == _UI_PORT

    assert len(started_environments) == _EXPECTED_POLL_ATTEMPTS
    assert started_environments[0] == started_environments[1]
    with sqlite3.connect(profile.business_database) as connection:
        assert connection.execute("SELECT id FROM projects").fetchall() == [(41,)]
    assert profile.root.exists()


def test_auto_port_retries_failed_child_startup_with_a_fixed_bound(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Replace a raced auto port while keeping every attempted child managed."""
    _create_profile(checkout)
    selected_ports = iter(_AUTO_PORTS)
    started: list[int] = []
    stopped: list[int] = []

    monkeypatch.setattr(
        _main_module(),
        "select_loopback_port",
        lambda: next(selected_ports),
    )

    def fake_start_ui(**options: object) -> object:
        port = cast("int", options["port"])
        started.append(port)
        return FakeUIChild(process=FakeProcess(), port=port)

    def readiness(child: FakeUIChild, *, timeout: float) -> dict[str, str]:
        assert timeout > 0
        if child.port == _AUTO_PORTS[0]:
            message = "address raced"
            raise _module().UIReadinessError(message)
        return {"status": "ready"}

    monkeypatch.setattr(_main_module(), "start_ui", fake_start_ui)
    monkeypatch.setattr(_main_module(), "wait_for_readiness", readiness)
    monkeypatch.setattr(
        _main_module(),
        "stop_ui",
        lambda child: stopped.append(child.port),
    )

    status = _main_module().main(
        ["ui", "--profile", "local", "--port", "auto", "--json"],
        checkout_root=checkout,
        runner=BootstrapRunner(checkout),
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out)["port"] == _AUTO_PORTS[1]
    assert started == list(_AUTO_PORTS)
    assert stopped == list(_AUTO_PORTS)


def test_interrupt_during_readiness_stops_the_tracked_child(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
) -> None:
    """Keep cleanup active for interrupts that arrive before attachment."""
    _create_profile(checkout)
    child = FakeUIChild(process=FakeProcess(), port=_UI_PORT)
    stopped: list[int] = []

    monkeypatch.setattr(_main_module(), "start_ui", lambda **_options: child)

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_main_module(), "wait_for_readiness", interrupt)
    monkeypatch.setattr(
        _main_module(),
        "stop_ui",
        lambda tracked: stopped.append(tracked.port),
    )

    try:
        status: int | str = _main_module().main(
            ["ui", "--profile", "local", "--port", str(_UI_PORT)],
            checkout_root=checkout,
            runner=BootstrapRunner(checkout),
        )
    except KeyboardInterrupt:
        status = "propagated"

    assert status == 0
    assert stopped == [_UI_PORT]


@pytest.mark.parametrize("readiness_fails", [False, True])
def test_ephemeral_ui_removes_only_its_unique_child_profile(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    readiness_fails: bool,
) -> None:
    """Clean acceptance state on success and failure without touching the parent."""
    _create_profile(checkout)
    parent = profile_paths(checkout, "local")
    child_database: Path | None = None

    def fake_start_ui(**options: object) -> object:
        nonlocal child_database
        environment = cast("Mapping[str, str]", options["environment"])
        child_database = Path(
            environment["AGILEFORGE_DB_URL"].removeprefix("sqlite:///")
        )
        assert child_database != parent.business_database
        assert child_database.exists()
        return _module().UIChild(process=FakeProcess(), port=8124)

    def readiness(*_args: object, **_kwargs: object) -> dict[str, str]:
        if readiness_fails:
            message = "forced failure"
            raise _module().UIReadinessError(message)
        return {"status": "ready"}

    stopped: list[int] = []
    monkeypatch.setattr(_main_module(), "start_ui", fake_start_ui)
    monkeypatch.setattr(_main_module(), "wait_for_readiness", readiness)
    monkeypatch.setattr(
        _main_module(),
        "stop_ui",
        lambda child: stopped.append(cast("int", child.port)),
    )

    status = _main_module().main(
        [
            "ui",
            "--profile",
            "local",
            "--ephemeral",
            "--port",
            "8124",
            "--json",
        ],
        checkout_root=checkout,
        runner=BootstrapRunner(checkout),
    )

    assert status == (1 if readiness_fails else 0)
    assert child_database is not None
    assert not child_database.parent.exists()
    assert parent.root.exists()
    assert stopped == [8124]
