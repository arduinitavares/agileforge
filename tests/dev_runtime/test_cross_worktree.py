"""Real-process proof that linked worktrees keep runtime profiles isolated."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.request import urlopen

import psutil
import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2026-08-03T10:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-08-03T10:00:00+00:00",
}
_WORKTREE_COUNT = 2
_HTTP_OK = 200


@dataclass(frozen=True, slots=True)
class UiIsolationExpected:
    """Expected identities for two concurrent worktree dashboards."""

    worktrees: tuple[Path, Path]
    commits: tuple[str, str]
    profiles: tuple[dict[str, object], dict[str, object]]


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = list(arguments)
    if os.name == "nt":
        target = Path(argv[0])
        if target.is_file() and target.suffix.lower() not in {".exe", ".bat", ".cmd"}:
            posix_path = target.resolve().as_posix()
            if len(posix_path) > 1 and posix_path[1] == ":":
                posix_path = f"/{posix_path[0].lower()}{posix_path[2:]}"
            argv = ["sh", posix_path, *argv[1:]]
    completed = subprocess.run(  # noqa: S603  # nosec B603
        argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, (
        f"command failed: {arguments!r}\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed


def _git(
    checkout: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
) -> str:
    result = _run(
        ("git", "-C", str(checkout), *arguments),
        cwd=checkout,
        env=env,
    )
    return result.stdout.strip()


def _profile_snapshot(profile_root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(profile_root)): path.read_bytes()
        for path in sorted(profile_root.rglob("*"))
        if path.is_file()
    }


def _launcher_environment(fake_bin: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "AGILEFORGE_DB_URL",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        "MODEL_CONFIG_PATH",
        "OPEN_ROUTER_API_KEY",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "UV_OFFLINE": "1",
            "UV_NO_PROGRESS": "1",
        }
    )
    return environment


def _launcher(
    worktree: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return _run((str(worktree / "agileforge-dev"), *arguments), cwd=worktree, env=env)


def _commit_launcher_fixtures(source_root: Path, clone: Path) -> tuple[str, str]:
    _run(
        ("git", "clone", "--no-hardlinks", str(source_root), str(clone)),
        cwd=clone.parent,
    )
    _git(clone, "config", "user.name", "Cross Worktree Tests")
    _git(clone, "config", "user.email", "cross-worktree@example.invalid")
    copied_files = (
        ".python-version",
        "api.py",
        "agileforge-dev",
        "cli/dev_checks.py",
        "cli/dev_main.py",
        "cli/dev_profiles.py",
        "cli/dev_server.py",
        "cli/main.py",
        "frontend/__init__.py",
        "pyproject.toml",
        "services/application.py",
        "utils/runtime_controls.py",
        "uv.lock",
    )
    for relative in copied_files:
        shutil.copy2(source_root / relative, clone / relative)

    fixture = clone / "tests" / "dev_runtime" / "cross_worktree_fixture.txt"
    fixture.write_text("launcher fixture one\n", encoding="utf-8")
    _git(
        clone,
        "add",
        *copied_files,
        str(fixture.relative_to(clone)),
    )
    _git(
        clone,
        "commit",
        "-m",
        "test: add first launcher fixture",
        env={**os.environ, **_COMMIT_ENVIRONMENT},
    )
    commit_one = _git(clone, "rev-parse", "HEAD")

    fixture.write_text("launcher fixture two\n", encoding="utf-8")
    _git(clone, "add", str(fixture.relative_to(clone)))
    _git(
        clone,
        "commit",
        "-m",
        "test: advance launcher fixture",
        env={
            **os.environ,
            **_COMMIT_ENVIRONMENT,
            "GIT_AUTHOR_DATE": "2026-08-03T10:01:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-03T10:01:00+00:00",
        },
    )
    return commit_one, _git(clone, "rev-parse", "HEAD")


def _fake_path_shim(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_agileforge = fake_bin / "agileforge"
    fake_agileforge.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'PATH shim must not run' >&2\nexit 97\n",
        encoding="utf-8",
    )
    fake_agileforge.chmod(0o700)
    if os.name == "nt":
        fake_cmd = fake_bin / "agileforge.cmd"
        fake_cmd.write_text(
            "@echo PATH shim must not run 1>&2\n@exit /b 97\n",
            encoding="utf-8",
        )
    return fake_bin


def _start_ui_launcher(
    worktree: Path,
    *,
    env: Mapping[str, str],
) -> subprocess.Popen[str]:
    launcher = worktree / "agileforge-dev"
    argv = [
        str(launcher),
        "ui",
        "--profile",
        "local",
        "--port",
        "auto",
        "--json",
        "--ready-timeout",
        "30",
    ]
    if os.name == "nt":
        posix_path = launcher.resolve().as_posix()
        if len(posix_path) > 1 and posix_path[1] == ":":
            posix_path = f"/{posix_path[0].lower()}{posix_path[2:]}"
        argv = ["sh", posix_path, *argv[1:]]
    return subprocess.Popen(  # noqa: S603  # nosec B603
        argv,
        cwd=worktree,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _launcher_readiness(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line, f"launcher exited before readiness with status {process.poll()}"
    return cast("dict[str, object]", json.loads(line))


def _dashboard_config(port: int) -> dict[str, object]:
    url = f"http://127.0.0.1:{port}/api/dashboard/config"
    with urlopen(url, timeout=5) as response:  # noqa: S310  # nosec B310
        assert response.status == _HTTP_OK
        return cast("dict[str, object]", json.loads(response.read()))


def _stop_launcher(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        if os.name == "nt":
            children: list[psutil.Process] = []
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
            process.terminate()
            for child in children:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    child.terminate()
            _gone, alive = psutil.wait_procs(children, timeout=5)
            for stubborn in alive:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    stubborn.kill()
        else:
            process.send_signal(signal.SIGINT)
    try:
        process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
    if os.name == "nt":
        assert process.returncode in (0, 1)
    else:
        assert process.returncode == 0


def _assert_process_gone(process_id: int, *, attempts: int = 50) -> None:
    access_denied_error: psutil.AccessDenied | None = None
    for _attempt in range(attempts):
        try:
            if not psutil.pid_exists(process_id):
                return
            proc = psutil.Process(process_id)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        except psutil.AccessDenied as error:
            access_denied_error = error
        if attempts > 1:
            time.sleep(0.05)
    if access_denied_error is not None:
        message = (
            f"process absence could not be verified: "
            f"{process_id} ({access_denied_error})"
        )
        pytest.fail(
            message  # ty: ignore[invalid-argument-type]
        )
    pytest.fail(
        f"dashboard child still exists: {process_id}"  # ty: ignore[invalid-argument-type]
    )


def _assert_cli_isolation(
    expected: UiIsolationExpected,
    *,
    env: Mapping[str, str],
) -> None:
    """Prove each production CLI changes only its own profile tree."""
    root_one = Path(str(expected.profiles[0]["business_database"])).parent
    root_two = Path(str(expected.profiles[1]["business_database"])).parent
    snapshot_two = _profile_snapshot(root_two)
    cli_one = _launcher(
        expected.worktrees[0],
        ("cli", "--profile", "local", "--", "project", "list"),
        env=env,
    )
    assert json.loads(cli_one.stdout)["ok"] is True
    assert f"Checkout: {expected.worktrees[0].resolve()}" in cli_one.stderr
    assert f"Commit: {expected.commits[0]}" in cli_one.stderr
    assert "PATH shim must not run" not in cli_one.stderr
    assert _profile_snapshot(root_two) == snapshot_two

    snapshot_one = _profile_snapshot(root_one)
    cli_two = _launcher(
        expected.worktrees[1],
        ("cli", "--profile", "local", "--", "project", "list"),
        env=env,
    )
    assert json.loads(cli_two.stdout)["ok"] is True
    assert f"Checkout: {expected.worktrees[1].resolve()}" in cli_two.stderr
    assert f"Commit: {expected.commits[1]}" in cli_two.stderr
    assert "PATH shim must not run" not in cli_two.stderr
    assert _profile_snapshot(root_one) == snapshot_one


def _assert_concurrent_ui_isolation(
    expected: UiIsolationExpected,
    *,
    env: Mapping[str, str],
    launchers: list[subprocess.Popen[str]],
    process_ids: list[int],
) -> None:
    """Run and inspect both local dashboards while cleanup remains caller-owned."""
    launchers.extend(_start_ui_launcher(path, env=env) for path in expected.worktrees)
    readiness_one, readiness_two = (
        _launcher_readiness(process) for process in launchers
    )
    port_one = int(cast("int", readiness_one["port"]))
    port_two = int(cast("int", readiness_two["port"]))
    assert port_one != port_two

    config_one = _dashboard_config(port_one)
    config_two = _dashboard_config(port_two)
    process_ids.extend(
        (
            int(cast("int", config_one["process_id"])),
            int(cast("int", config_two["process_id"])),
        )
    )
    assert config_one["status"] == config_two["status"] == "ready"
    assert {
        str(config_one["checkout_root"]),
        str(config_two["checkout_root"]),
    } == {str(path.resolve()) for path in expected.worktrees}
    assert {str(config_one["commit"]), str(config_two["commit"])} == set(
        expected.commits
    )
    assert config_one["business_database"] != config_two["business_database"]
    assert config_one["trace_database"] != config_two["trace_database"]
    assert config_one["business_database"] == expected.profiles[0]["business_database"]
    assert config_two["business_database"] == expected.profiles[1]["business_database"]


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_same_profile_name_is_fully_isolated_across_linked_worktrees(
    tmp_path: Path,
) -> None:
    """Run each committed launcher against only its own profile and databases."""
    source_root = Path(__file__).parents[2]
    clone = tmp_path / "clone"
    worktree_one = tmp_path / "worktree-one"
    worktree_two = tmp_path / "worktree-two"
    fake_bin = _fake_path_shim(tmp_path)
    commit_one, commit_two = _commit_launcher_fixtures(source_root, clone)
    assert commit_one != commit_two
    assert (clone / ".python-version").read_text(encoding="utf-8") == "3.13.15\n"
    assert not (clone / ".venv").exists()

    added_worktrees: list[Path] = []
    ui_launchers: list[subprocess.Popen[str]] = []
    dashboard_process_ids: list[int] = []
    try:
        for path, commit in (
            (worktree_one, commit_one),
            (worktree_two, commit_two),
        ):
            _git(clone, "worktree", "add", "--detach", str(path), commit)
            added_worktrees.append(path)

        environment = _launcher_environment(fake_bin)
        for path in added_worktrees:
            _launcher(
                path,
                ("init", "--profile", "local", "--json"),
                env=environment,
            )

        info_one = json.loads(
            _launcher(
                worktree_one,
                ("info", "--profile", "local", "--json"),
                env=environment,
            ).stdout
        )
        info_two = json.loads(
            _launcher(
                worktree_two,
                ("info", "--profile", "local", "--json"),
                env=environment,
            ).stdout
        )

        profile_one = cast("dict[str, object]", info_one["profile"])
        profile_two = cast("dict[str, object]", info_two["profile"])
        checkout_one = cast("dict[str, object]", profile_one["checkout"])
        checkout_two = cast("dict[str, object]", profile_two["checkout"])
        roots = {str(checkout_one["root"]), str(checkout_two["root"])}
        commits = {str(info_one["current_commit"]), str(info_two["current_commit"])}
        profile_roots = {
            str(Path(str(profile_one["business_database"])).parent),
            str(Path(str(profile_two["business_database"])).parent),
        }
        assert roots == {str(worktree_one.resolve()), str(worktree_two.resolve())}
        assert commits == {commit_one, commit_two}
        assert len(profile_roots) == _WORKTREE_COUNT
        assert profile_one["business_database"] != profile_two["business_database"]
        assert profile_one["trace_database"] != profile_two["trace_database"]

        expected = UiIsolationExpected(
            worktrees=(worktree_one, worktree_two),
            commits=(commit_one, commit_two),
            profiles=(profile_one, profile_two),
        )
        _assert_cli_isolation(expected, env=environment)

        _assert_concurrent_ui_isolation(
            expected,
            env=environment,
            launchers=ui_launchers,
            process_ids=dashboard_process_ids,
        )
    finally:
        for process in reversed(ui_launchers):
            _stop_launcher(process)
        for path in reversed(added_worktrees):
            _run(
                ("git", "-C", str(clone), "worktree", "remove", "--force", str(path)),
                cwd=clone,
            )
        _run(("git", "-C", str(clone), "worktree", "prune"), cwd=clone)

    for process_id in dashboard_process_ids:
        _assert_process_gone(process_id)


def _cleanup_synthetic_process(
    process: subprocess.Popen[str],
    *,
    descendants: Sequence[psutil.Process] = (),
    child_pid: int | None = None,
) -> None:
    """Clean up synthetic parent and all owned descendants even if parsing failed."""
    targets = list(descendants)
    if not targets and process.poll() is None:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            targets = psutil.Process(process.pid).children(recursive=True)

    for descendant in targets:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            descendant.kill()

    if child_pid is not None:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            if psutil.pid_exists(child_pid):
                psutil.Process(child_pid).kill()

    if process.poll() is None:
        with contextlib.suppress(Exception):
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows-specific process tree cleanup test",
)
def test_stop_launcher_cleans_owned_child_process_tree() -> None:
    """Verify _stop_launcher terminates launcher and owned child process tree."""
    child_script = "import time; time.sleep(60)"
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    descendants: list[psutil.Process] = []
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            descendants = psutil.Process(process.pid).children(recursive=True)

        child_pid = int(line.strip())
        assert psutil.pid_exists(child_pid)

        _stop_launcher(process)
        assert process.poll() is not None
        _assert_process_gone(child_pid)
    finally:
        _cleanup_synthetic_process(
            process,
            descendants=descendants,
            child_pid=child_pid,
        )


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows-specific process tree cleanup test",
)
def test_synthetic_process_cleanup_cleans_descendants_when_pid_parsing_fails() -> None:
    """Verify descendants and parent are cleaned up even when PID parsing fails."""
    child_script = "import time; time.sleep(60)"
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
        "print('MALFORMED_PID', flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    descendants: list[psutil.Process] = []
    parsing_failed = False
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            descendants = psutil.Process(process.pid).children(recursive=True)

        try:
            child_pid = int(line.strip())
        except ValueError:
            parsing_failed = True

        assert parsing_failed is True
        assert len(descendants) >= 1
        for descendant in descendants:
            assert psutil.pid_exists(descendant.pid)
    finally:
        _cleanup_synthetic_process(
            process,
            descendants=descendants,
            child_pid=child_pid,
        )

    assert process.poll() is not None
    for descendant in descendants:
        _assert_process_gone(descendant.pid)


def test_assert_process_gone_fails_when_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AccessDenied must not be treated as successful process absence."""

    def fake_process(_pid: int) -> psutil.Process:
        raise psutil.AccessDenied(pid=12345)

    monkeypatch.setattr(psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(psutil, "Process", fake_process)

    with pytest.raises(
        pytest.fail.Exception, match="process absence could not be verified"
    ):
        _assert_process_gone(12345, attempts=1)
