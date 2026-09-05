"""Tests for managed worktree-local dashboard processes."""

from __future__ import annotations

import importlib
import json
import signal
import socket
import sqlite3
import subprocess  # nosec B404
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

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
_SECOND_UI_PORT = 8124
_LAUNCH_NONCE = "launcher-nonce"
_UI_LAUNCH_NONCE_ENV = "AGILEFORGE_UI_LAUNCH_NONCE"


class HandoffFault(BaseException):
    """Deterministic non-Exception fault injected at an ownership handoff."""


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
    (root / ".gitignore").write_text(".agileforge/\n", encoding="utf-8")
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
            connection = sqlite3.connect(business_path)
            try:
                with connection:
                    for table in _EXPECTED_TABLES:
                        connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
            finally:
                connection.close()
            return module.CommandResult(arguments=arguments, exit_code=0)
        message = f"unexpected command: {arguments!r}"
        raise AssertionError(message)


def _create_profile(checkout: Path, name: str = "local") -> None:
    profile = initialize_profile_record(
        checkout,
        name,
        now=datetime(2026, 8, 3, 12, tzinfo=UTC),
    )
    connection = sqlite3.connect(profile.business_database)
    try:
        with connection:
            for table in _EXPECTED_TABLES:
                connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
    finally:
        connection.close()


def _runtime_identity(
    checkout: Path,
    *,
    process_id: int | None,
) -> object:
    paths = profile_paths(checkout, "local")
    return _module().ExpectedUIRuntime(
        checkout_root=checkout.resolve(),
        commit=_git(checkout, "rev-parse", "HEAD"),
        business_database=paths.business_database,
        trace_database=paths.trace_database,
        process_id=process_id,
        launch_nonce=_LAUNCH_NONCE,
    )


def _ready_payload(
    checkout: Path,
    *,
    process_id: int,
) -> dict[str, object]:
    paths = profile_paths(checkout, "local")
    return {
        "status": "ready",
        "process_id": process_id,
        "checkout_root": str(checkout.resolve()),
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "business_database": str(paths.business_database),
        "trace_database": str(paths.trace_database),
        "launch_nonce": _LAUNCH_NONCE,
    }


def _inject_handoff(fault_kind: str) -> None:
    if fault_kind == "sigint":
        signal.raise_signal(signal.SIGINT)
        return
    if fault_kind == "sigterm":
        signal.raise_signal(signal.SIGTERM)
        return
    raise HandoffFault


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
    checkout: Path,
) -> None:
    """Return only a valid ready payload from the fixed local endpoint."""
    process = FakeProcess()
    child = _module().UIChild(process=process, port=_UI_PORT)
    payload = _ready_payload(checkout, process_id=process.pid)
    urls: list[str] = []

    def fake_urlopen(url: str, *, timeout: float) -> FakeResponse:
        assert timeout > 0
        urls.append(url)
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(_module(), "urlopen", fake_urlopen)

    config = _module().wait_for_readiness(
        child,
        expected=_runtime_identity(checkout, process_id=process.pid),
        timeout=1,
    )

    assert config.process_id == process.pid
    assert (
        config.business_database == profile_paths(checkout, "local").business_database
    )
    assert urls == [f"http://127.0.0.1:{_UI_PORT}/api/dashboard/config"]


def test_wait_for_readiness_times_out_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
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
        _module().wait_for_readiness(
            child,
            expected=_runtime_identity(checkout, process_id=child.process.pid),
            timeout=1,
            poll_interval=0.01,
        )

    assert attempts == _EXPECTED_POLL_ATTEMPTS


def test_wait_for_readiness_rejects_runtime_mismatch_without_repolling(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    tmp_path: Path,
) -> None:
    """Treat a ready response from another runtime as a terminal attempt failure."""
    child = _module().UIChild(process=FakeProcess(), port=_UI_PORT)
    foreign_root = tmp_path / "foreign-checkout"
    foreign_root.mkdir()
    payload = _ready_payload(checkout, process_id=child.process.pid)
    payload["checkout_root"] = str(foreign_root.resolve())
    attempts = 0

    def fake_urlopen(_url: str, *, timeout: float) -> FakeResponse:
        nonlocal attempts
        assert timeout > 0
        attempts += 1
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(_module(), "urlopen", fake_urlopen)

    with pytest.raises(_module().UIRuntimeMismatchError, match="identity mismatch"):
        _module().wait_for_readiness(
            child,
            expected=_runtime_identity(checkout, process_id=child.process.pid),
            timeout=10,
        )

    assert attempts == 1


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
    connection = sqlite3.connect(profile.business_database)
    try:
        with connection:
            connection.execute("INSERT INTO projects (id) VALUES (41)")
    finally:
        connection.close()
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
    first_nonce = started_environments[0].pop(_UI_LAUNCH_NONCE_ENV)
    second_nonce = started_environments[1].pop(_UI_LAUNCH_NONCE_ENV)
    assert first_nonce != second_nonce
    assert started_environments[0] == started_environments[1]
    assert started_environments[0]["AGILEFORGE_LAUNCHER_CHILD"] == "1"
    connection = sqlite3.connect(profile.business_database)
    try:
        assert connection.execute("SELECT id FROM projects").fetchall() == [(41,)]
    finally:
        connection.close()
    assert profile.root.exists()


def test_json_reload_is_rejected_before_any_process_spawn(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject unstable JSON/reload process identity before profile or child setup."""
    process_calls: list[object] = []

    def fail_start(**options: object) -> object:
        process_calls.append(options)
        raise AssertionError

    monkeypatch.setattr(_main_module(), "start_ui", fail_start)

    status = _main_module().main(
        ["ui", "--profile", "missing", "--reload", "--json"],
        checkout_root=checkout,
        runner=BootstrapRunner(checkout),
    )

    assert status == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "exit_code": 1,
        "error": "--json cannot be combined with --reload",
    }
    assert process_calls == []


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

    def readiness(
        child: FakeUIChild,
        *,
        expected: object,
        timeout: float,
    ) -> dict[str, str]:
        assert expected is not None
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


def test_auto_port_never_advertises_a_foreign_ready_runtime(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a different AgileForge runtime already serving the selected port."""
    _create_profile(checkout)
    selected_ports = iter((_UI_PORT, _SECOND_UI_PORT))
    children: dict[int, FakeUIChild] = {}
    foreign_root = tmp_path / "foreign-checkout"
    foreign_root.mkdir()
    foreign_payload = {
        "status": "ready",
        "process_id": 9988,
        "checkout_root": str(foreign_root.resolve()),
        "commit": "b" * 40,
        "business_database": str(foreign_root / "business.sqlite3"),
        "trace_database": str(foreign_root / "trace.sqlite3"),
        "launch_nonce": "foreign-launcher-nonce",
    }
    monkeypatch.setattr(
        _main_module().secrets,
        "token_hex",
        lambda _bytes: _LAUNCH_NONCE,
    )

    monkeypatch.setattr(
        _main_module(),
        "select_loopback_port",
        lambda: next(selected_ports),
    )

    def fake_start_ui(**options: object) -> FakeUIChild:
        port = cast("int", options["port"])
        child = FakeUIChild(process=FakeProcess(pid=9000 + port), port=port)
        children[port] = child
        return child

    def fake_urlopen(url: str, *, timeout: float) -> FakeResponse:
        assert timeout > 0
        port = urlsplit(url).port
        assert port is not None
        payload = (
            foreign_payload
            if port == _UI_PORT
            else _ready_payload(
                checkout,
                process_id=children[_SECOND_UI_PORT].process.pid,
            )
        )
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(_main_module(), "start_ui", fake_start_ui)
    monkeypatch.setattr(_module(), "urlopen", fake_urlopen)

    status = _main_module().main(
        [
            "ui",
            "--profile",
            "local",
            "--port",
            "auto",
            "--json",
            "--ready-timeout",
            "0.001",
        ],
        checkout_root=checkout,
        runner=BootstrapRunner(checkout),
    )

    advertised = json.loads(capsys.readouterr().out)
    assert status == 0
    assert advertised["port"] == _SECOND_UI_PORT
    assert advertised["business_database"] != foreign_payload["business_database"]
    assert children[_UI_PORT].process.terminated == 1


def test_explicit_reload_rejects_same_profile_foreign_server_nonce(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse a same-checkout/profile server occupying a new reload launch port."""
    _create_profile(checkout)
    process = FakeProcess()
    child = FakeUIChild(process=process, port=_UI_PORT)
    started_environment: dict[str, str] = {}
    monkeypatch.setattr(
        _main_module().secrets,
        "token_hex",
        lambda _bytes: "new-reload-launch",
    )

    def fake_start_ui(**options: object) -> FakeUIChild:
        environment = cast("Mapping[str, str]", options["environment"])
        started_environment.update(environment)
        return child

    foreign_payload = _ready_payload(checkout, process_id=9988)
    foreign_payload["launch_nonce"] = "foreign-existing-launch"
    monkeypatch.setattr(_main_module(), "start_ui", fake_start_ui)
    monkeypatch.setattr(
        _module(),
        "urlopen",
        lambda _url, **_options: FakeResponse(json.dumps(foreign_payload).encode()),
    )

    status = _main_module().main(
        ["ui", "--profile", "local", "--port", str(_UI_PORT), "--reload"],
        checkout_root=checkout,
        runner=BootstrapRunner(checkout),
    )

    captured = capsys.readouterr()
    assert status == 1
    assert started_environment[_UI_LAUNCH_NONCE_ENV] == "new-reload-launch"
    assert "Dashboard ready" not in captured.out
    assert "identity mismatch" in captured.err
    assert process.terminated == 1


@pytest.mark.parametrize("fault_kind", ["sigint", "sigterm", "base"])
def test_ephemeral_profile_handoff_fault_removes_only_the_child_profile(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    fault_kind: str,
) -> None:
    """Own unique profile cleanup before publishing the initialized profile."""
    _create_profile(checkout)
    parent = profile_paths(checkout, "local")
    child_name = "local.ui-handoff"
    child = profile_paths(checkout, child_name)
    spawned: list[object] = []

    monkeypatch.setattr(
        _main_module(),
        "_ephemeral_profile_name",
        lambda _name: child_name,
    )
    monkeypatch.setattr(
        _main_module(),
        "_profile_handoff",
        lambda _profile: _inject_handoff(fault_kind),
        raising=False,
    )
    monkeypatch.setattr(
        _main_module(),
        "start_ui",
        lambda **options: spawned.append(options),
    )

    try:
        result: int | str = _main_module().main(
            ["ui", "--profile", "local", "--ephemeral", "--port", str(_UI_PORT)],
            checkout_root=checkout,
            runner=BootstrapRunner(checkout),
        )
    except HandoffFault:
        result = "fault"

    assert result == ("fault" if fault_kind == "base" else 0)
    assert not child.root.exists()
    assert parent.root.exists()
    assert spawned == []


@pytest.mark.parametrize("fault_kind", ["sigint", "sigterm", "base"])
def test_ui_child_handoff_fault_stops_only_the_acquired_child(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    fault_kind: str,
) -> None:
    """Own child cleanup before publishing the started process."""
    _create_profile(checkout)
    parent = profile_paths(checkout, "local")
    process = FakeProcess()
    child = FakeUIChild(process=process, port=_UI_PORT)

    monkeypatch.setattr(_main_module(), "start_ui", lambda **_options: child)
    monkeypatch.setattr(
        _main_module(),
        "_ui_child_handoff",
        lambda _child: _inject_handoff(fault_kind),
        raising=False,
    )
    monkeypatch.setattr(
        _main_module(),
        "wait_for_readiness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )

    try:
        result: int | str = _main_module().main(
            ["ui", "--profile", "local", "--port", str(_UI_PORT)],
            checkout_root=checkout,
            runner=BootstrapRunner(checkout),
        )
    except HandoffFault:
        result = "fault"

    assert result == ("fault" if fault_kind == "base" else 0)
    assert process.terminated == 1
    assert process.poll() == 0
    assert parent.root.exists()


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
