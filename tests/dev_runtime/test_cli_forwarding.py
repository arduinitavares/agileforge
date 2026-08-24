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
    from os import PathLike
    from pathlib import Path
    from typing import IO


_RAW_FAILURE_EXIT = 7
_JSON_FAILURE_EXIT = 9
_INVALID_CHILD_EXIT = 13
_INVALID_CHILD_RESULT = {"ok": False, "error": "invalid_production_cli_output"}
_CREDENTIAL_ARGUMENT_ERROR = "forwarded CLI arguments contain provider credential"


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


def _string_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for child in value for item in _string_values(child))
    if isinstance(value, dict):
        keys = tuple(key for key in value if isinstance(key, str))
        values = tuple(
            item for child in value.values() for item in _string_values(child)
        )
        return (*keys, *values)
    return ()


def _assert_invalid_child_envelope(
    payload: dict[str, object],
    *,
    checkout: Path,
    child_exit_code: int,
) -> None:
    profile = dev_main.load_profile(checkout, "local")
    assert payload == {
        "checkout": str(checkout.resolve()),
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "profile": "local",
        "profile_mode": "development",
        "business_database": str(profile.business_database),
        "trace_database": str(profile.trace_database),
        "command": ["project", "list"],
        "exit_code": child_exit_code,
        "result": _INVALID_CHILD_RESULT,
    }


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
    forbidden_user_shim = "/Users/aaat/" + ".local/bin/" + "agileforge"
    assert forbidden_user_shim not in child_argv
    assert child_cwd == checkout.resolve()


def test_cli_forwarding_installs_only_profile_environment(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace parent runtime controls with validated profile-owned values."""
    monkeypatch.setenv("AGILEFORGE_DB_URL", "parent-business")
    monkeypatch.setenv("AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL", "parent-trace")
    monkeypatch.setenv("MODEL_CONFIG_PATH", "parent-models")
    monkeypatch.setenv("SPECIFICATION_STRUCTURER_MAX_TOKENS", "24576")
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
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
    assert child_environment is not None
    profile = dev_main.load_profile(checkout, "local")
    assert child_environment == {
        **profile_environment(profile),
        "AGILEFORGE_LAUNCHER_CHILD": "1",
    }
    assert child_environment["SPECIFICATION_STRUCTURER_MAX_TOKENS"] == "24576"


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


def test_cli_reads_secrets_from_one_no_follow_descriptor(
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep parsing the opened inode when the selected pathname is swapped."""
    selected_value = "selected-provider-value"
    alternate_value = "alternate-provider-value"
    selected = tmp_path / "provider.env"
    alternate = tmp_path / "alternate.env"
    selected.write_text(
        f"OPEN_ROUTER_API_KEY={selected_value}\n",
        encoding="utf-8",
    )
    alternate.write_text(
        f"OPEN_ROUTER_API_KEY={alternate_value}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
    real_dotenv_values = dev_main.dotenv_values
    swap_count = 0

    def swap_then_parse(
        dotenv_path: str | PathLike[str] | None = None,
        stream: IO[str] | None = None,
        verbose: bool = False,
        interpolate: bool = True,
        encoding: str | None = "utf-8",
    ) -> dict[str, str | None]:
        nonlocal swap_count
        swap_count += 1
        selected.unlink()
        selected.symlink_to(alternate)
        return real_dotenv_values(
            dotenv_path=dotenv_path,
            stream=stream,
            verbose=verbose,
            interpolate=interpolate,
            encoding=encoding,
        )

    monkeypatch.setattr(dev_main, "dotenv_values", swap_then_parse)
    runner = _runner(checkout)

    exit_code = dev_main.main(
        [
            "cli",
            "--profile",
            "local",
            "--secrets-file",
            str(selected),
            "--",
            "project",
            "list",
        ],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    child_environment = runner.calls[-1][2]
    assert exit_code == 0
    assert swap_count == 1
    assert child_environment is not None
    assert child_environment["OPEN_ROUTER_API_KEY"] == selected_value
    assert alternate_value not in child_environment.values()


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


@pytest.mark.parametrize("json_output", [False, True], ids=["raw", "json"])
@pytest.mark.parametrize("embedded", [False, True], ids=["exact", "embedded"])
def test_cli_rejects_credential_bearing_forwarded_arguments(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    json_output: bool,
    embedded: bool,
) -> None:
    """Reject secret argv before constructing or spawning the production child."""
    credential_value = 'argument"\\\nsecret-\u00e7'
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", credential_value)
    forwarded_value = (
        f"prefix={credential_value}:suffix" if embedded else credential_value
    )
    runner = _runner(checkout)
    arguments = ["cli", "--profile", "local"]
    if json_output:
        arguments.append("--json")
    arguments.extend(("--", "project", "show", forwarded_value))

    exit_code = dev_main.main(
        arguments,
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert len(runner.calls) == 1
    assert all(
        credential_value not in argument
        for call_arguments, _cwd, _environment in runner.calls
        for argument in call_arguments
    )
    assert credential_value not in captured.out
    assert credential_value not in captured.err
    if json_output:
        assert captured.err == ""
        payload = json.loads(captured.out)
        assert payload == {
            "status": "error",
            "exit_code": 1,
            "error": _CREDENTIAL_ARGUMENT_ERROR,
        }
        assert "command" not in payload
    else:
        assert captured.out == ""
        assert captured.err == f"error: {_CREDENTIAL_ARGUMENT_ERROR}\n"


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


def test_cli_json_mode_recursively_redacts_escaped_credential_values(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redact structured strings even when JSON escaping changes their bytes."""
    credential_value = 'quote"backslash\\newline\nnon-ascii-\u00e7'
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", credential_value)
    runner = _runner(
        checkout,
        stdout=json.dumps(
            {
                "ok": True,
                "direct": credential_value,
                f"direct-key-{credential_value}": "direct-key-value",
                "nested": {
                    "items": [
                        f"before {credential_value} after",
                        {f"nested-key-{credential_value}": credential_value},
                    ],
                    "collision": {
                        credential_value: "discarded-on-collision",
                        "[REDACTED]": "deterministic-last-value",
                    },
                },
            }
        ),
    )

    exit_code = dev_main.main(
        ["cli", "--profile", "local", "--json", "--", "project", "list"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    result = cast("dict[str, object]", payload["result"])
    assert exit_code == 0
    assert captured.err == ""
    assert result == {
        "ok": True,
        "direct": "[REDACTED]",
        "direct-key-[REDACTED]": "direct-key-value",
        "nested": {
            "items": [
                "before [REDACTED] after",
                {"nested-key-[REDACTED]": "[REDACTED]"},
            ],
            "collision": {"[REDACTED]": "deterministic-last-value"},
        },
    }
    assert all(credential_value not in value for value in _string_values(payload))


def test_cli_json_mode_rejects_non_object_stdout_without_echoing_it(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail closed when production stdout is not one JSON object."""
    runner = _runner(
        checkout,
        exit_code=_INVALID_CHILD_EXIT,
        stdout='["unexpected-sensitive-output"]\n',
    )

    exit_code = dev_main.main(
        ["cli", "--profile", "local", "--json", "--", "project", "list"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert "unexpected-sensitive-output" not in captured.out
    _assert_invalid_child_envelope(
        json.loads(captured.out),
        checkout=checkout,
        child_exit_code=_INVALID_CHILD_EXIT,
    )


def test_cli_json_mode_discards_secret_bearing_parse_errors(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return fixed provenance without retaining malformed stdout exceptions."""
    credential_value = 'malformed"\\\nsecret-\u00e7'
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", credential_value)
    raw_stdout = f'{{"ok": true, "detail": "{credential_value}'
    runner = _runner(
        checkout,
        exit_code=_INVALID_CHILD_EXIT,
        stdout=raw_stdout,
    )
    emitted_errors: list[Exception] = []
    original_emit_error = dev_main._emit_error

    def capture_error(error: Exception, *, json_output: bool) -> None:
        emitted_errors.append(error)
        original_emit_error(error, json_output=json_output)

    monkeypatch.setattr(dev_main, "_emit_error", capture_error)

    exit_code = dev_main.main(
        ["cli", "--profile", "local", "--json", "--", "project", "list"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert emitted_errors == []
    assert captured.err == ""
    assert raw_stdout not in captured.out
    assert credential_value not in captured.out
    _assert_invalid_child_envelope(
        json.loads(captured.out),
        checkout=checkout,
        child_exit_code=_INVALID_CHILD_EXIT,
    )
