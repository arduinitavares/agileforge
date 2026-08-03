"""Tests for the uv-owned developer launcher and profile commands."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from git import Git
from git.exc import GitCommandError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

_EXPECTED_TABLES = {"projects", "spec_registry", "workflow_events"}
_FORBIDDEN_TABLES = {"products", "sessions", "cli_mutation_ledger"}
_DEFAULT_READY_TIMEOUT = 15.0
_USAGE_EXIT_CODE = 2


def _module() -> ModuleType:
    return importlib.import_module("cli.dev_main")


def _git(checkout: Path, *arguments: str) -> str:
    output = Git().execute(command=["git", "-C", str(checkout), *arguments])
    return cast("str", output).strip()


def _run_process(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    command = Git(working_dir=str(cwd))
    if env is not None:
        command.update_environment(**dict(env))
    try:
        output = command.execute(command=list(arguments))
    except GitCommandError as error:
        status = error.status if isinstance(error.status, int) else 1
        return status, error.stdout, error.stderr
    return 0, cast("str", output), ""


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a minimal checkout containing profile fingerprint inputs."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-b", "feature/dev-main")
    _git(root, "config", "user.name", "Developer Runtime Tests")
    _git(root, "config", "user.email", "dev-runtime@example.invalid")
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


@dataclass(frozen=True, slots=True)
class FixedClock:
    """Return one deterministic timestamp."""

    value: datetime

    def now(self) -> datetime:
        """Return the fixed timestamp."""
        return self.value


@dataclass(slots=True)
class FakeRunner:
    """Record commands and emulate uv plus the schema bootstrap."""

    checkout: Path
    schema_tables: set[str] = field(default_factory=lambda: set(_EXPECTED_TABLES))
    schema_exit_code: int = 0
    calls: list[tuple[tuple[str, ...], Path, dict[str, str] | None]] = field(
        default_factory=list
    )
    manifest_seen_during_schema: bool = False

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> object:
        """Emulate one fixed command."""
        module = _module()
        copied_env = None if env is None else dict(env)
        self.calls.append((arguments, cwd, copied_env))
        if arguments == ("uv", "lock", "--check"):
            return module.CommandResult(arguments=arguments, exit_code=0)
        if arguments == ("uv", "--version"):
            return module.CommandResult(
                arguments=arguments,
                exit_code=0,
                stdout="uv 0.8.0\n",
            )
        if arguments == ("git", "-C", str(self.checkout), "rev-parse", "HEAD"):
            return module.CommandResult(
                arguments=arguments,
                exit_code=0,
                stdout=f"{_git(self.checkout, 'rev-parse', 'HEAD')}\n",
            )
        if arguments == (sys.executable, str(self.checkout / "agile_sqlmodel.py")):
            assert copied_env is not None
            business_url = copied_env["AGILEFORGE_DB_URL"]
            trace_url = copied_env["AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL"]
            assert business_url != trace_url
            business_path = Path(business_url.removeprefix("sqlite:///"))
            trace_path = Path(trace_url.removeprefix("sqlite:///"))
            self.manifest_seen_during_schema = (
                business_path.parent / "profile.json"
            ).exists()
            assert not trace_path.exists()
            with sqlite3.connect(business_path) as connection:
                for table in sorted(self.schema_tables):
                    connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
            return module.CommandResult(
                arguments=arguments,
                exit_code=self.schema_exit_code,
                stderr=(
                    "OPEN_ROUTER_API_KEY=must-not-leak\n"
                    if self.schema_exit_code
                    else ""
                ),
            )
        message = f"unexpected command: {arguments!r}"
        raise AssertionError(message)


def _clock(*, minute: int = 0) -> FixedClock:
    return FixedClock(datetime(2026, 8, 3, 12, minute, tzinfo=UTC))


def test_parser_exposes_required_commands_and_options() -> None:
    """Expose the complete designed command surface without production imports."""
    parser = _module().build_parser()

    init_args = parser.parse_args(
        [
            "init",
            "--profile",
            "ci",
            "--mode",
            "acceptance",
            "--expect-sha",
            "a" * 40,
            "--json",
        ]
    )
    info_args = parser.parse_args(["info", "--profile", "local", "--json"])
    cli_args = parser.parse_args(
        [
            "cli",
            "--profile",
            "local",
            "--secrets-file",
            "secrets.env",
            "--json",
            "--",
            "workflow",
            "next",
        ]
    )
    ui_args = parser.parse_args(
        [
            "ui",
            "--profile",
            "local",
            "--secrets-file",
            "secrets.env",
            "--ephemeral",
            "--port",
            "auto",
            "--reload",
            "--json",
            "--ready-timeout",
            "15",
        ]
    )
    check_args = parser.parse_args(["check"])
    reset_args = parser.parse_args(
        ["reset", "--profile", "local", "--confirm", "local"]
    )

    assert init_args.command == "init"
    assert init_args.mode == "acceptance"
    assert init_args.expect_sha == "a" * 40
    assert init_args.json is True
    assert info_args.command == "info"
    assert info_args.json is True
    assert cli_args.command == "cli"
    assert cli_args.secrets_file == Path("secrets.env")
    assert cli_args.json is True
    assert cli_args.agileforge_arguments[-2:] == ["workflow", "next"]
    assert ui_args.command == "ui"
    assert ui_args.ephemeral is True
    assert ui_args.port == "auto"
    assert ui_args.reload is True
    assert ui_args.ready_timeout == _DEFAULT_READY_TIMEOUT
    assert check_args.command == "check"
    assert reset_args.command == "reset"
    assert reset_args.confirmation == "local"


def test_parser_uses_standard_structured_usage_exit_code() -> None:
    """Return argparse's stable usage status for incomplete commands."""
    with pytest.raises(SystemExit) as error:
        _module().build_parser().parse_args(["info"])

    assert error.value.code == _USAGE_EXIT_CODE


def test_bootstrap_is_executable_canonical_and_uv_owned(tmp_path: Path) -> None:
    """Delegate from the launcher's checkout even when caller CWD differs."""
    module_path = Path(cast("str", _module().__file__))
    launcher = module_path.parents[1] / "agileforge-dev"
    source = launcher.read_text(encoding="utf-8")
    execution_lines = [line for line in source.splitlines() if line.startswith("exec ")]

    assert stat.S_IMODE(launcher.stat().st_mode) & stat.S_IXUSR
    assert execution_lines == [
        'exec uv --directory "$ROOT" run --locked agileforge-dev "$@"'
    ]
    for forbidden in ("branch", "worktree", "database", "profile", "port", "home"):
        assert forbidden not in source.lower()

    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    fake_uv = bin_directory / "uv"
    fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_uv.chmod(0o700)
    exit_code, stdout, _stderr = _run_process(
        (str(launcher), "--help"),
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_directory}:{os.environ['PATH']}"},
    )

    assert exit_code == 0
    assert stdout.splitlines() == [
        "--directory",
        str(launcher.parent.resolve()),
        "run",
        "--locked",
        "agileforge-dev",
        "--help",
    ]


def test_bootstrap_rejects_symlinked_entrypoint(tmp_path: Path) -> None:
    """Refuse delegation when the invoked bootstrap path is a symlink."""
    module_path = Path(cast("str", _module().__file__))
    launcher = module_path.parents[1] / "agileforge-dev"
    linked_launcher = tmp_path / "linked-launcher"
    linked_launcher.symlink_to(launcher)

    exit_code, _stdout, stderr = _run_process(
        (str(linked_launcher), "--help"),
        cwd=tmp_path,
    )

    assert exit_code != 0
    assert "symlink" in stderr.lower()


def test_import_does_not_load_production_database_or_application() -> None:
    """Keep production imports lazy until a validated environment is installed."""
    module_path = Path(cast("str", _module().__file__))
    exit_code, _stdout, stderr = _run_process(
        (
            sys.executable,
            "-c",
            (
                "import sys; import cli.dev_main; "
                "assert 'models.db' not in sys.modules; "
                "assert 'services.application' not in sys.modules"
            ),
        ),
        cwd=module_path.parents[1],
    )

    assert exit_code == 0, stderr


def test_main_resolves_default_checkout_from_module_directory(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a directory, not the Python source file, as the Git anchor."""
    module = _module()
    captured: list[Path] = []

    def capture_anchor(anchor: Path) -> Path:
        captured.append(anchor)
        return checkout

    monkeypatch.setattr(module, "resolve_checkout_root", capture_anchor)

    assert module.main(["check"], runner=FakeRunner(checkout), clock=_clock()) == 1
    assert captured == [Path(cast("str", module.__file__)).parent]


def test_main_returns_structured_checkout_resolution_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Convert checkout validation failures into the stable JSON error status."""
    module = _module()

    def fail_resolution(_anchor: Path) -> Path:
        message = "invalid checkout"
        raise ValueError(message)

    monkeypatch.setattr(module, "resolve_checkout_root", fail_resolution)

    exit_code = module.main(["info", "--profile", "local", "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "exit_code": 1,
        "error": "invalid checkout",
    }


def test_init_bootstraps_and_verifies_before_manifest(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Publish a manifest only after the isolated business schema is valid."""
    module = _module()
    runner = FakeRunner(checkout)

    exit_code = module.main(
        ["init", "--profile", "local", "--json"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    assert exit_code == 0
    assert [call[0] for call in runner.calls] == [
        ("uv", "lock", "--check"),
        ("uv", "--version"),
        (sys.executable, str(checkout / "agile_sqlmodel.py")),
    ]
    assert all(call[1] == checkout for call in runner.calls)
    assert runner.manifest_seen_during_schema is False
    paths = module.profile_paths(checkout, "local")
    assert paths.manifest.is_file()
    assert paths.business_database.is_file()
    assert not paths.trace_database.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "initialized"
    assert payload["profile"]["name"] == "local"
    assert payload["schema"]["valid"] is True


@pytest.mark.parametrize(
    ("tables", "schema_exit_code"),
    [(_EXPECTED_TABLES, 9), ({"projects", "products"}, 0)],
)
def test_init_failure_removes_only_newly_claimed_state(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    tables: set[str],
    schema_exit_code: int,
) -> None:
    """Roll back the root claimed by this init on bootstrap or verification failure."""
    module = _module()
    runner = FakeRunner(
        checkout,
        schema_tables=set(tables),
        schema_exit_code=schema_exit_code,
    )

    exit_code = module.main(
        ["init", "--profile", "broken", "--json"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    assert exit_code == 1
    assert not module.profile_paths(checkout, "broken").root.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 1
    assert "must-not-leak" not in json.dumps(payload)


def test_init_refuses_preexisting_incomplete_root_without_cleanup(
    checkout: Path,
) -> None:
    """Never adopt or remove a profile root this invocation did not create."""
    module = _module()
    paths = module.profile_paths(checkout, "stale")
    paths.root.mkdir(parents=True)
    sentinel = paths.root / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    exit_code = module.main(
        ["init", "--profile", "stale"],
        checkout_root=checkout,
        runner=FakeRunner(checkout),
        clock=_clock(),
    )

    assert exit_code == 1
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_init_is_idempotent_only_for_exact_existing_profile(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reuse an exact valid profile without rerunning schema bootstrap."""
    module = _module()
    first_runner = FakeRunner(checkout)
    assert (
        module.main(
            ["init", "--profile", "local", "--json"],
            checkout_root=checkout,
            runner=first_runner,
            clock=_clock(),
        )
        == 0
    )
    capsys.readouterr()
    second_runner = FakeRunner(checkout)

    second_exit = module.main(
        ["init", "--profile", "local", "--json"],
        checkout_root=checkout,
        runner=second_runner,
        clock=_clock(minute=1),
    )

    assert second_exit == 0
    assert [call[0] for call in second_runner.calls] == [
        ("uv", "lock", "--check"),
        ("uv", "--version"),
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "existing"

    mismatch_exit = module.main(
        [
            "init",
            "--profile",
            "local",
            "--mode",
            "acceptance",
            "--expect-sha",
            _git(checkout, "rev-parse", "HEAD"),
        ],
        checkout_root=checkout,
        runner=FakeRunner(checkout),
        clock=_clock(minute=2),
    )
    assert mismatch_exit == 1
    assert module.profile_paths(checkout, "local").root.is_dir()


def test_info_json_is_complete_redacted_and_validated(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report complete non-secret provenance plus current validation state."""
    module = _module()
    init_runner = FakeRunner(checkout)
    assert (
        module.main(
            ["init", "--profile", "local", "--json"],
            checkout_root=checkout,
            runner=init_runner,
            clock=_clock(),
        )
        == 0
    )
    capsys.readouterr()

    exit_code = module.main(
        ["info", "--profile", "local", "--json"],
        checkout_root=checkout,
        runner=FakeRunner(checkout),
        clock=_clock(minute=1),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["validation_status"] == "valid"
    assert payload["current_commit"] == _git(checkout, "rev-parse", "HEAD")
    assert payload["profile"]["name"] == "local"
    assert set(payload["schema"]["tables"]) >= _EXPECTED_TABLES
    serialized = json.dumps(payload).lower()
    for secret_key in ("api_key", "password", "token"):
        assert secret_key not in serialized


def test_reset_requires_exact_confirmation_and_reports_removed_paths(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Delegate destructive cleanup only after exact name confirmation."""
    module = _module()
    assert (
        module.main(
            ["init", "--profile", "remove", "--json"],
            checkout_root=checkout,
            runner=FakeRunner(checkout),
            clock=_clock(),
        )
        == 0
    )
    capsys.readouterr()

    assert (
        module.main(
            ["reset", "--profile", "remove", "--confirm", "wrong"],
            checkout_root=checkout,
            runner=FakeRunner(checkout),
            clock=_clock(minute=1),
        )
        == 1
    )
    assert module.profile_paths(checkout, "remove").root.is_dir()
    capsys.readouterr()

    assert (
        module.main(
            ["reset", "--profile", "remove", "--confirm", "remove"],
            checkout_root=checkout,
            runner=FakeRunner(checkout),
            clock=_clock(minute=2),
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "profile.json" in output
    assert not module.profile_paths(checkout, "remove").root.exists()


def test_schema_contract_names_removed_tables() -> None:
    """Keep hard-break table exclusions explicit in the developer runtime."""
    module = _module()

    assert set(module.EXPECTED_BUSINESS_TABLES) == _EXPECTED_TABLES
    assert set(module.FORBIDDEN_BUSINESS_TABLES) == _FORBIDDEN_TABLES
