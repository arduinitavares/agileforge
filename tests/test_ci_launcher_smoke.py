"""Tests for the repository-owned macOS launcher smoke command."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # nosec B404
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cli.ci_launcher_smoke_runtime as smoke_runtime
import scripts.ci_launcher_smoke as smoke
from cli.dev_profiles import profile_paths
from cli.dev_server import DashboardConfig, UIChild

if TYPE_CHECKING:
    from typing import TextIO

SCRIPT_PATH = Path(smoke.__file__).resolve()
_SHA = "a" * 40
_PORT = 18_765
_CHILD_PID = 43_210


@dataclass(slots=True)
class FakeProcess:
    """Minimal tracked launcher group."""

    pid: int = 32_100
    returncode: int | None = None

    def poll(self) -> int | None:
        """Return the configured status."""
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Return the status or model a bounded timeout."""
        if self.returncode is None:
            raise subprocess.TimeoutExpired(
                cmd="ui",
                timeout=0.0 if timeout is None else timeout,
            )
        return self.returncode

    def terminate(self) -> None:
        """Stop the fake process gracefully."""
        self.returncode = 0

    def kill(self) -> None:
        """Stop the fake process forcibly."""
        self.returncode = -9


@dataclass(slots=True)
class FakeRuntime:
    """Publish exact launcher envelopes through an injectable boundary."""

    checkout: Path
    profile: str
    expected_sha: str = _SHA
    invalid_command: str | None = None
    endpoint_after_stop: bool = False
    calls: list[tuple[str, ...]] = field(default_factory=list)
    process: FakeProcess = field(default_factory=FakeProcess)
    child_profile: str = ""
    child_pid: int | None = None
    now: float = 0.0

    def __post_init__(self) -> None:
        """Derive the deterministic ephemeral profile identity."""
        self.child_profile = f"{self.profile}.ui-0123456789abcdef"

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
    ) -> smoke_runtime.CommandResult:
        """Return one deterministic launcher envelope."""
        assert cwd == self.checkout
        self.calls.append(arguments)
        if arguments[0] == "git":
            return smoke_runtime.CommandResult(0, f"{self.expected_sha}\n")
        command = arguments[1]
        if command == "init":
            self._create_profile(self.profile)
            payload = self._init_payload()
        elif command == "info":
            payload = self._info_payload()
        elif command == "cli":
            payload = self._cli_payload()
        else:
            raise AssertionError(arguments)
        output = "not-json" if command == self.invalid_command else json.dumps(payload)
        return smoke_runtime.CommandResult(0, output)

    def start_ui(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        stdout: TextIO,
        stderr: TextIO,
    ) -> FakeProcess:
        """Create the fake ephemeral profile and publish readiness JSON."""
        del stderr
        assert cwd == self.checkout
        self.calls.append(arguments)
        self._create_profile(self.child_profile)
        stdout.write(json.dumps(self._ui_payload()))
        stdout.flush()
        return self.process

    def wait_ready(
        self,
        process: smoke_runtime.ManagedProcess,
        result: smoke.UiReadyResult,
        expected_sha: str,
    ) -> DashboardConfig:
        """Return exact fake child identity."""
        assert process is self.process
        assert result.port == _PORT
        assert expected_sha == self.expected_sha
        self.child_pid = _CHILD_PID
        child = profile_paths(self.checkout, self.child_profile)
        return DashboardConfig(
            status="ready",
            process_id=_CHILD_PID,
            checkout_root=self.checkout,
            commit=self.expected_sha,
            business_database=child.business_database,
            trace_database=child.trace_database,
        )

    def stop_ui(self, process: smoke_runtime.ManagedProcess) -> None:
        """Stop the fake launcher group."""
        assert process is self.process
        self.process.returncode = 0
        self.child_pid = None

    def process_exists(self, process_id: int) -> bool:
        """Return whether the fake UI child remains."""
        return self.child_pid == process_id

    def group_exists(self, process_group_id: int) -> bool:
        """Return whether the fake launcher group remains."""
        return process_group_id == self.process.pid and self.process.poll() is None

    def endpoint_reachable(self, port: int) -> bool:
        """Return configured post-stop endpoint state."""
        assert port == _PORT
        return self.endpoint_after_stop

    def monotonic(self) -> float:
        """Return the fake monotonic clock."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake monotonic clock."""
        self.now += seconds

    def _create_profile(self, profile: str) -> None:
        root = profile_paths(self.checkout, profile).root
        root.mkdir(parents=True)
        (root / "profile.json").write_text("{}\n", encoding="utf-8")

    def _profile_payload(self) -> dict[str, object]:
        paths = profile_paths(self.checkout, self.profile)
        return {
            "schema_version": "1",
            "name": self.profile,
            "mode": "acceptance",
            "checkout": {
                "root": str(self.checkout),
                "branch": "test",
                "commit": self.expected_sha,
            },
            "expected_commit": self.expected_sha,
            "graph_version": "workflow-graph-v1",
            "python_version": "3.13.0",
            "uv_version": "0.10.12",
            "business_database": str(paths.business_database),
            "trace_database": str(paths.trace_database),
            "model_config_path": str(self.checkout / "config" / "models.yaml"),
            "model_config_sha256": "b" * 64,
            "schema_source_sha256": "c" * 64,
            "created_at": "2026-08-03T00:00:00Z",
            "last_used_at": "2026-08-03T00:00:01Z",
        }

    def _init_payload(self) -> dict[str, object]:
        return {
            "status": "initialized",
            "profile": self._profile_payload(),
            "schema": {
                "valid": True,
                "tables": ["projects", "spec_registry", "workflow_events"],
            },
        }

    def _info_payload(self) -> dict[str, object]:
        return {
            "validation_status": "valid",
            "current_commit": self.expected_sha,
            "profile": self._profile_payload(),
            "schema": {
                "valid": True,
                "tables": ["projects", "spec_registry", "workflow_events"],
            },
        }

    def _cli_payload(self) -> dict[str, object]:
        paths = profile_paths(self.checkout, self.profile)
        return {
            "checkout": str(self.checkout),
            "commit": self.expected_sha,
            "profile": self.profile,
            "profile_mode": "acceptance",
            "business_database": str(paths.business_database),
            "trace_database": str(paths.trace_database),
            "command": ["project", "list"],
            "exit_code": 0,
            "result": {"ok": True, "data": []},
        }

    def _ui_payload(self) -> dict[str, object]:
        paths = profile_paths(self.checkout, self.child_profile)
        return {
            "status": "ready",
            "url": f"http://127.0.0.1:{_PORT}/dashboard",
            "host": "127.0.0.1",
            "port": _PORT,
            "checkout": str(self.checkout),
            "commit": self.expected_sha,
            "profile": self.child_profile,
            "profile_mode": "acceptance",
            "ephemeral": True,
            "business_database": str(paths.business_database),
            "trace_database": str(paths.trace_database),
        }


@pytest.fixture
def fake_profile_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace safe profile reset with an equivalent temporary-tree remover."""

    def remove(checkout: Path, profile: str, confirmation: str) -> tuple[Path, ...]:
        assert confirmation == profile
        root = profile_paths(checkout, profile).root
        shutil.rmtree(root)
        return (root,)

    monkeypatch.setattr(smoke_runtime, "reset_profile", remove)


def _request(profile: str, expected_sha: str = _SHA) -> smoke.SmokeRequest:
    return smoke.SmokeRequest(profile, expected_sha)


def test_smoke_invokes_exact_commands_and_cleans_owned_profiles(
    tmp_path: Path,
    fake_profile_reset: None,
) -> None:
    """Run exact init/info/cli/non-reload UI commands with one identity."""
    del fake_profile_reset
    profile = "ci-smoke-unit"
    runtime = FakeRuntime(tmp_path, profile)
    profiles = smoke_runtime.LocalProfiles.create(tmp_path, profile)

    result = smoke.run_smoke(tmp_path, _request(profile), runtime, profiles)

    launcher = "./agileforge-dev"
    assert runtime.calls == [
        ("git", "-C", str(tmp_path), "rev-parse", "HEAD"),
        (
            launcher,
            "init",
            "--profile",
            profile,
            "--mode",
            "acceptance",
            "--expect-sha",
            _SHA,
            "--json",
        ),
        (launcher, "info", "--profile", profile, "--json"),
        (
            launcher,
            "cli",
            "--profile",
            profile,
            "--json",
            "--",
            "project",
            "list",
        ),
        (
            launcher,
            "ui",
            "--profile",
            profile,
            "--ephemeral",
            "--port",
            "auto",
            "--json",
            "--ready-timeout",
            "30",
        ),
    ]
    assert result == smoke.SmokeResult(profile, _SHA)
    assert not profile_paths(tmp_path, profile).root.exists()
    assert not profile_paths(tmp_path, runtime.child_profile).root.exists()


def test_pre_identity_failure_preserves_preexisting_and_foreign_profiles(
    tmp_path: Path,
    fake_profile_reset: None,
) -> None:
    """Clean acquired state using the pre-launch profile-root snapshot."""
    del fake_profile_reset
    profile = "ci-smoke-fault"
    preexisting = f"{profile}.ui-preexisting"
    foreign = "foreign-profile"
    for name in (preexisting, foreign):
        profile_paths(tmp_path, name).root.mkdir(parents=True)
    runtime = FakeRuntime(tmp_path, profile)
    profiles = smoke_runtime.LocalProfiles.create(tmp_path, profile)

    class InjectedError(RuntimeError):
        """Fail after process-group acquisition and child profile creation."""

    with pytest.raises(InjectedError):
        smoke.run_smoke(
            tmp_path,
            _request(profile),
            runtime,
            profiles,
            lambda _process: (_ for _ in ()).throw(InjectedError()),
        )

    assert runtime.process.poll() == 0
    assert not profile_paths(tmp_path, profile).root.exists()
    assert not profile_paths(tmp_path, runtime.child_profile).root.exists()
    assert profile_paths(tmp_path, preexisting).root.is_dir()
    assert profile_paths(tmp_path, foreign).root.is_dir()


def test_head_and_json_failures_are_fixed_and_cleanup_parent(
    tmp_path: Path,
    fake_profile_reset: None,
) -> None:
    """Reject exact-SHA and typed-envelope failures with safe errors."""
    del fake_profile_reset
    profile = "ci-smoke-errors"
    runtime = FakeRuntime(tmp_path, profile)
    profiles = smoke_runtime.LocalProfiles.create(tmp_path, profile)
    with pytest.raises(smoke.SmokeError, match=smoke.ErrorCode.HEAD.value):
        smoke.run_smoke(tmp_path, _request(profile, "d" * 40), runtime, profiles)
    assert runtime.calls == [("git", "-C", str(tmp_path), "rev-parse", "HEAD")]

    runtime = FakeRuntime(tmp_path, profile, invalid_command="info")
    profiles = smoke_runtime.LocalProfiles.create(tmp_path, profile)
    with pytest.raises(smoke.SmokeError, match=smoke.ErrorCode.OUTPUT.value):
        smoke.run_smoke(tmp_path, _request(profile), runtime, profiles)
    assert not profile_paths(tmp_path, profile).root.exists()


def test_surviving_endpoint_fails_cleanup_and_profiles_are_removed(
    tmp_path: Path,
    fake_profile_reset: None,
) -> None:
    """Reject a loopback endpoint that survives process-group shutdown."""
    del fake_profile_reset
    profile = "ci-smoke-endpoint"
    runtime = FakeRuntime(tmp_path, profile, endpoint_after_stop=True)
    profiles = smoke_runtime.LocalProfiles.create(tmp_path, profile)

    with pytest.raises(smoke.SmokeError, match=smoke.ErrorCode.CLEANUP.value):
        smoke.run_smoke(tmp_path, _request(profile), runtime, profiles)

    assert not profile_paths(tmp_path, profile).root.exists()
    assert not profile_paths(tmp_path, runtime.child_profile).root.exists()


def test_safe_environment_excludes_credentials(tmp_path: Path) -> None:
    """Pass no provider, GitHub, proxy, or index credentials to launchers."""
    sensitive = uuid.uuid4().hex
    environment = smoke_runtime.safe_environment(
        {
            "PATH": "/usr/bin",
            "HOME": str(tmp_path),
            "OPEN_ROUTER_API_KEY": sensitive,
            "GITHUB_TOKEN": sensitive,
            "HTTPS_PROXY": f"https://user:{sensitive}@proxy.invalid",
            "UV_INDEX_URL": f"https://user:{sensitive}@index.invalid/simple",
        }
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == str(tmp_path)
    assert sensitive not in repr(environment)


def test_process_group_maps_existing_stop_policy_to_term_and_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal the exact process group while reusing dev_server stop policy."""
    sent: list[tuple[int, int]] = []

    class UnderlyingProcess:
        pid = 999
        returncode: int | None = None

        def poll(self) -> int | None:
            """Return the configured status."""
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            """Return the configured status."""
            del timeout
            return self.returncode or 0

        def terminate(self) -> None:
            """Satisfy the managed-process boundary."""

        def kill(self) -> None:
            """Satisfy the managed-process boundary."""

    process = UnderlyingProcess()
    group = smoke_runtime.ProcessGroup(process=process)
    monkeypatch.setattr(
        smoke_runtime.os,
        "killpg",
        lambda group_id, selected: sent.append((group_id, selected)),
    )

    group.terminate()
    group.kill()

    assert sent == [(999, signal.SIGTERM), (999, getattr(signal, "SIGKILL", 9))]


def test_runtime_delegates_bounded_stop_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass the process group to the existing finite TERM/KILL policy."""
    observed: list[tuple[UIChild, float]] = []

    def capture_stop(child: UIChild, *, timeout: float) -> None:
        observed.append((child, timeout))

    monkeypatch.setattr(smoke_runtime, "stop_ui", capture_stop)
    process = FakeProcess()

    smoke_runtime.LocalRuntime({}).stop_ui(process)

    child, timeout = observed[0]
    assert child.process is process
    assert timeout == smoke_runtime.STOP_TIMEOUT_SECONDS


def test_main_redacts_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Emit only a fixed safe error at the executable boundary."""

    def fail(*_args: object, **_kwargs: object) -> None:
        message = "must-not-leak-provider-secret"
        raise RuntimeError(message)

    monkeypatch.setattr(smoke, "run_smoke", fail)
    status = smoke.main(["--profile", "ci-safe", "--expect-sha", _SHA])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert captured.err == "ci launcher smoke failed: internal failure\n"


def _current_sha(checkout: Path) -> str:
    git = shutil.which("git")
    assert git is not None
    return subprocess.run(  # noqa: S603  # nosec B603
        (git, "-C", str(checkout), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_real_script_runs_complete_launcher_lifecycle() -> None:
    """Execute the repository command against the real attached launcher."""
    checkout = SCRIPT_PATH.parents[1]
    profile = f"ci-real-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    expected_sha = _current_sha(checkout)
    uv = shutil.which("uv")
    assert uv is not None
    completed = subprocess.run(  # noqa: S603  # nosec B603
        (
            uv,
            "run",
            "--locked",
            "python",
            "scripts/ci_launcher_smoke.py",
            "--profile",
            profile,
            "--expect-sha",
            expected_sha,
        ),
        cwd=checkout,
        env={**os.environ, "OPEN_ROUTER_API_KEY": "must-not-reach-launcher"},
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "status": "passed",
        "profile": profile,
        "expected_sha": expected_sha,
    }
    base = profile_paths(checkout, profile).root.parent
    assert not (base / profile).exists()
    assert not any(base.glob(f"{profile}.ui-*"))


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_real_pre_identity_failure_cleans_process_group_and_profiles() -> None:
    """Fail after real child-profile creation and prove complete cleanup."""
    checkout = SCRIPT_PATH.parents[1]
    profile = f"ci-fault-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    expected_sha = _current_sha(checkout)
    base = profile_paths(checkout, profile).root.parent
    prefix = f"{profile}.ui-"

    class InjectedError(RuntimeError):
        """Stop before the launcher readiness identity is consumed."""

    def fail_after_child(_process: smoke_runtime.ManagedProcess) -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if any(path.name.startswith(prefix) for path in base.iterdir()):
                raise InjectedError
            time.sleep(0.05)
        pytest.fail("launcher did not create an ephemeral profile")

    with pytest.raises(InjectedError):
        smoke.run_smoke(
            checkout,
            _request(profile, expected_sha),
            acquired_hook=fail_after_child,
        )

    assert not (base / profile).exists()
    assert not any(base.glob(f"{profile}.ui-*"))
