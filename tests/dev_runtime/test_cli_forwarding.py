"""Tests for production CLI forwarding through validated runtime profiles."""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from git import Git

from cli import dev_main
from cli.dev_profiles import initialize_profile_record, profile_environment

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


_RAW_FAILURE_EXIT = 7
_JSON_FAILURE_EXIT = 9


@dataclass(frozen=True, slots=True)
class FixedClock:
    """Return one deterministic timestamp."""

    value: datetime

    def now(self) -> datetime:
        """Return the fixed timestamp."""
        return self.value


@dataclass(slots=True)
class DevCliRunner:
    """Capture Git and production CLI child invocations."""

    checkout: Path
    child_result: dev_main.CommandResult
    calls: list[tuple[tuple[str, ...], Path, dict[str, str] | None]] = field(
        default_factory=list
    )

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> dev_main.CommandResult:
        """Return deterministic Git provenance or the configured child result."""
        copied_env = None if env is None else dict(env)
        self.calls.append((arguments, cwd, copied_env))
        if arguments == ("git", "-C", str(self.checkout), "rev-parse", "HEAD"):
            return dev_main.CommandResult(
                arguments=arguments,
                exit_code=0,
                stdout=f"{_git(self.checkout, 'rev-parse', 'HEAD')}\n",
            )
        return self.child_result


def _git(checkout: Path, *arguments: str) -> str:
    output = Git().execute(command=["git", "-C", str(checkout), *arguments])
    return cast("str", output).strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a minimal profile-compatible Git checkout."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-b", "feature/cli-forwarding")
    _git(root, "config", "user.name", "CLI Forwarding Tests")
    _git(root, "config", "user.email", "cli-forwarding@example.invalid")
    (root / "config").mkdir()
    (root / "models").mkdir()
    (root / "agile_sqlmodel.py").write_text("SCHEMA = 1\n", encoding="utf-8")
    (root / "config" / "models.yaml").write_text(
        "models:\n  default: fixture\n",
        encoding="utf-8",
    )
    (root / "models" / "core.py").write_text("SCHEMA = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    profile = initialize_profile_record(
        root,
        "local",
        now=datetime(2026, 8, 3, 10, tzinfo=UTC),
    )
    with sqlite3.connect(profile.business_database) as connection:
        for table in dev_main.EXPECTED_BUSINESS_TABLES:
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
    return root


def _runner(
    checkout: Path,
    *,
    exit_code: int = 0,
    stdout: str = '{"ok": true, "projects": []}\n',
    stderr: str = "",
) -> DevCliRunner:
    arguments = (sys.executable, "-m", "cli.main", "project", "list")
    return DevCliRunner(
        checkout=checkout,
        child_result=dev_main.CommandResult(
            arguments=arguments,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        ),
    )


def _clock() -> FixedClock:
    return FixedClock(datetime(2026, 8, 3, 10, 1, tzinfo=UTC))


def test_cli_forwarding_ignores_path_selected_agileforge(
    checkout: Path,
) -> None:
    """Execute the current Python module and preserve arguments after ``--``."""
    runner = _runner(checkout)

    exit_code = dev_main.main(
        [
            "cli",
            "--profile",
            "local",
            "--",
            "project",
            "show",
            "--project-id",
            "41",
        ],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    assert exit_code == 0
    child_argv, child_cwd, _child_env = runner.calls[-1]
    assert child_argv == (
        sys.executable,
        "-m",
        "cli.main",
        "project",
        "show",
        "--project-id",
        "41",
    )
    assert "/Users/aaat/.local/bin/agileforge" not in child_argv
    assert child_cwd == checkout.resolve()


def test_cli_forwarding_installs_only_profile_environment(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace parent runtime controls with validated profile-owned values."""
    monkeypatch.setenv("AGILEFORGE_DB_URL", "parent-business")
    monkeypatch.setenv("AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL", "parent-trace")
    monkeypatch.setenv("MODEL_CONFIG_PATH", "parent-models")
    monkeypatch.setenv("DATABASE_URL", "parent-database")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "parent-secret")
    runner = _runner(checkout)

    assert (
        dev_main.main(
            ["cli", "--profile", "local", "--", "project", "list"],
            checkout_root=checkout,
            runner=runner,
            clock=_clock(),
        )
        == 0
    )

    child_environment = runner.calls[-1][2]
    profile = dev_main.load_profile(checkout, "local")
    assert child_environment == profile_environment(profile)


def test_cli_secrets_file_allows_only_provider_key_and_parent_wins(
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import one allowlisted key without accepting runtime controls."""
    file_value = "file-provider-secret"
    parent_value = "parent-provider-secret"
    secrets_file = tmp_path / "provider.env"
    secrets_file.write_text(
        "\n".join(
            (
                f"OPEN_ROUTER_API_KEY={file_value}",
                "AGILEFORGE_DB_URL=forbidden-business",
                "MODEL_CONFIG_PATH=forbidden-models",
                "DATABASE_URL=forbidden-database",
                "AWS_SECRET_ACCESS_KEY=forbidden-cloud-secret",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", parent_value)
    runner = _runner(checkout)

    assert (
        dev_main.main(
            [
                "cli",
                "--profile",
                "local",
                "--secrets-file",
                str(secrets_file),
                "--",
                "project",
                "list",
            ],
            checkout_root=checkout,
            runner=runner,
            clock=_clock(),
        )
        == 0
    )

    child_environment = runner.calls[-1][2]
    assert child_environment is not None
    assert child_environment["OPEN_ROUTER_API_KEY"] == parent_value
    serialized = json.dumps(child_environment)
    assert file_value not in serialized
    for forbidden in (
        "forbidden-business",
        "forbidden-models",
        "forbidden-database",
        "forbidden-cloud-secret",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_cli_rejects_non_regular_secrets_file(
    checkout: Path,
    tmp_path: Path,
    kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse indirect or non-file secret sources before child execution."""
    target = tmp_path / "target.env"
    if kind == "symlink":
        target.write_text("OPEN_ROUTER_API_KEY=not-loaded\n", encoding="utf-8")
        selected = tmp_path / "linked.env"
        selected.symlink_to(target)
    else:
        selected = tmp_path / "secrets"
        selected.mkdir()
    runner = _runner(checkout)

    exit_code = dev_main.main(
        [
            "cli",
            "--profile",
            "local",
            "--secrets-file",
            str(selected),
            "--json",
            "--",
            "project",
            "list",
        ],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    assert exit_code == 1
    assert len(runner.calls) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "regular file" in payload["error"]
    assert "not-loaded" not in json.dumps(payload)


def test_cli_raw_mode_preserves_child_streams_and_exit_code(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep production stdout byte-for-byte and add provenance to stderr."""
    production_stdout = '{"ok": false, "error": "missing"}\n'
    runner = _runner(
        checkout,
        exit_code=_RAW_FAILURE_EXIT,
        stdout=production_stdout,
        stderr="production diagnostic\n",
    )

    exit_code = dev_main.main(
        ["cli", "--profile", "local", "--", "project", "list"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    captured = capsys.readouterr()
    commit = _git(checkout, "rev-parse", "HEAD")
    assert exit_code == _RAW_FAILURE_EXIT
    assert captured.out == production_stdout
    assert f"Checkout: {checkout.resolve()}" in captured.err
    assert f"Commit: {commit}" in captured.err
    assert "Profile: local (development)" in captured.err
    assert "production diagnostic" in captured.err


def test_cli_json_mode_wraps_provenance_and_redacts_secret(
    checkout: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emit one combined object without credential values or child diagnostics."""
    credential_value = "provider-secret-must-not-leak"
    secrets_file = tmp_path / "provider.env"
    secrets_file.write_text(
        f"OPEN_ROUTER_API_KEY={credential_value}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
    runner = _runner(
        checkout,
        exit_code=_JSON_FAILURE_EXIT,
        stdout=json.dumps({"ok": False, "detail": f"failed with {credential_value}"}),
        stderr=f"child echoed {credential_value}\n",
    )

    exit_code = dev_main.main(
        [
            "cli",
            "--profile",
            "local",
            "--secrets-file",
            str(secrets_file),
            "--json",
            "--",
            "project",
            "list",
        ],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    profile = dev_main.load_profile(checkout, "local")
    assert exit_code == _JSON_FAILURE_EXIT
    assert captured.err == ""
    assert payload == {
        "checkout": str(checkout.resolve()),
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "profile": "local",
        "profile_mode": "development",
        "business_database": str(profile.business_database),
        "trace_database": str(profile.trace_database),
        "command": ["project", "list"],
        "exit_code": _JSON_FAILURE_EXIT,
        "result": {"ok": False, "detail": "failed with [REDACTED]"},
    }
    assert credential_value not in captured.out
    assert credential_value not in captured.err
    assert credential_value not in " ".join(runner.calls[-1][0])


def test_cli_json_mode_rejects_non_object_stdout_without_echoing_it(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail closed when production stdout is not one JSON object."""
    runner = _runner(checkout, stdout='["unexpected-sensitive-output"]\n')

    exit_code = dev_main.main(
        ["cli", "--profile", "local", "--json", "--", "project", "list"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unexpected-sensitive-output" not in captured.out
    assert json.loads(captured.out)["status"] == "error"
