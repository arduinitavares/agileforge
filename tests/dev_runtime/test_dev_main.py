"""Tests for the uv-owned developer launcher and profile commands."""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import unittest.mock
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from git import Git
from git.exc import GitCommandError
from sqlmodel import create_engine

from api import _checkout_commit, _runtime_provenance, _selected_git_executable
from cli.dev_checks import CheckCommandResult
from cli.dev_profiles import initialize_profile_record
from models.db import ensure_business_db_ready

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

_COMMIT_HASH_LENGTH = 40
_EXPECTED_TABLES = {"projects", "spec_registry", "workflow_events"}
_FORBIDDEN_TABLES = {"products", "sessions", "cli_" + "mutation" + "_ledger"}
_DEFAULT_READY_TIMEOUT = 15.0
_USAGE_EXIT_CODE = 2
_HOSTILE_UV_CONTROLS = (
    "UV_CONFIG_FILE",
    "UV_ENV_FILE",
    "UV_FROZEN",
    "UV_ISOLATED",
    "UV_LOCKED",
    "UV_MANAGED_PYTHON",
    "UV_NO_CONFIG",
    "UV_NO_MANAGED_PYTHON",
    "UV_NO_PROJECT",
    "UV_NO_SYNC",
    "UV_PROJECT",
    "UV_PROJECT_ENVIRONMENT",
    "UV_PYTHON",
    "UV_WORKING_DIR",
    "UV_WORKING_DIRECTORY",
)
_HOSTILE_SOURCE_CONTROLS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "UV_NO_EDITABLE",
    "VIRTUAL_ENV",
)
_WIN_ERROR_PRIVILEGE_NOT_HELD = 1314
_GIT_EXECUTABLE_FILE_MODE = "100755"


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
    argv = list(arguments)
    if os.name == "nt":
        target = Path(argv[0])
        if target.is_file() and target.suffix.lower() not in {".exe", ".bat", ".cmd"}:
            posix_path = target.resolve().as_posix()
            if len(posix_path) > 1 and posix_path[1] == ":":
                posix_path = f"/{posix_path[0].lower()}{posix_path[2:]}"
            argv = ["sh", posix_path, *argv[1:]]
    try:
        output = command.execute(command=argv)
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
            connection = sqlite3.connect(business_path)
            try:
                with connection:
                    for table in sorted(self.schema_tables):
                        connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
            finally:
                connection.close()
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
    info_args = parser.parse_args(
        [
            "info",
            "--profile",
            "local",
            "--secrets-file",
            "secrets.env",
            "--json",
        ]
    )
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
    assert info_args.secrets_file == Path("secrets.env")
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

    if os.name == "nt":
        file_mode = _git(launcher.parent, "ls-files", "-s", "agileforge-dev")
        assert file_mode.startswith(_GIT_EXECUTABLE_FILE_MODE)
    else:
        assert stat.S_IMODE(launcher.stat().st_mode) & stat.S_IXUSR
    assert (launcher.parent / ".python-version").read_text(
        encoding="utf-8"
    ) == "3.13.15\n"
    assert 'requires-python = ">=3.13.15,<3.14"' in (
        launcher.parent / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert execution_lines == [
        (
            'exec uv --directory "$ROOT" run --locked --exact --python '
            '3.13.15 agileforge-dev "$@"'
        )
    ]
    for forbidden in ("branch", "worktree", "database", "profile", "port"):
        assert forbidden not in source.lower()
    assert "$HOME" not in source

    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    fake_uv = bin_directory / "uv"
    hostile_controls = (*_HOSTILE_UV_CONTROLS, *_HOSTILE_SOURCE_CONTROLS)
    control_checks = "\n".join(
        f'[ "${{{name}+set}}" != set ] || printf "leaked:{name}\\n"'
        for name in hostile_controls
    )
    harmless_controls = {
        "SSL_CERT_FILE": "preserved",
        "UV_CACHE_DIR": "preserved",
        "UV_NATIVE_TLS": "preserved",
        "UV_OFFLINE": "preserved",
    }
    harmless_checks = "\n".join(
        f'[ "${{{name}}}" = preserved ] || printf "erased:{name}\\n"'
        for name in harmless_controls
    )
    fake_uv.write_text(
        f'#!/bin/sh\n{control_checks}\n{harmless_checks}\nprintf "%s\\n" "$@"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    hostile_environment = dict.fromkeys(hostile_controls, "hostile")
    exit_code, stdout, _stderr = _run_process(
        (str(launcher), "--help"),
        cwd=tmp_path,
        env={
            **os.environ,
            **hostile_environment,
            **harmless_controls,
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert exit_code == 0
    output_lines = stdout.splitlines()
    assert output_lines[0] == "--directory"
    reported_root = output_lines[1]
    if os.name == "nt":
        msys_match = re.match(r"^/([a-zA-Z])/(.*)$", reported_root)
        if msys_match:
            drive, rest = msys_match.groups()
            reported_root = f"{drive.upper()}:/{rest}"
    assert Path(reported_root).resolve() == launcher.parent.resolve()
    assert output_lines[2:] == [
        "run",
        "--locked",
        "--exact",
        "--python",
        "3.13.15",
        "agileforge-dev",
        "--help",
    ]


def test_bootstrap_rejects_real_hostile_uv_project_and_environment(
    tmp_path: Path,
) -> None:
    """Use this checkout's locked environment despite hostile caller uv controls."""
    module_path = Path(cast("str", _module().__file__))
    launcher = module_path.parents[1] / "agileforge-dev"
    hostile_project = tmp_path / "hostile-project"
    hostile_project.mkdir()
    (hostile_project / "pyproject.toml").write_text(
        "[project]\nname = 'hostile'\nversion = '0'\n",
        encoding="utf-8",
    )
    hostile_environment = tmp_path / "hostile-environment"
    hostile_bin = hostile_environment / "bin"
    hostile_bin.mkdir(parents=True)
    (hostile_environment / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\n",
        encoding="utf-8",
    )
    hostile_command = hostile_bin / "agileforge-dev"
    hostile_command.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'HOSTILE_UV_ENVIRONMENT'\n",
        encoding="utf-8",
    )
    hostile_command.chmod(0o700)
    hostile_config = tmp_path / "hostile-uv.toml"
    hostile_config.write_text("", encoding="utf-8")
    hostile_dotenv = tmp_path / "hostile.env"
    hostile_dotenv.write_text("HOSTILE_DOTENV=1\n", encoding="utf-8")
    environment = {
        **os.environ,
        **dict.fromkeys(_HOSTILE_UV_CONTROLS, "hostile"),
        "UV_CONFIG_FILE": str(hostile_config),
        "UV_ENV_FILE": str(hostile_dotenv),
        "UV_FROZEN": "1",
        "UV_ISOLATED": "1",
        "UV_NO_PROJECT": "1",
        "UV_NO_SYNC": "1",
        "UV_PROJECT": str(hostile_project),
        "UV_PROJECT_ENVIRONMENT": str(hostile_environment),
        "UV_PYTHON": str(tmp_path / "hostile-python"),
        "UV_WORKING_DIR": str(hostile_project),
        "UV_WORKING_DIRECTORY": str(hostile_project),
    }

    exit_code, stdout, stderr = _run_process(
        (str(launcher), "--help"),
        cwd=tmp_path,
        env=environment,
    )

    assert exit_code == 0, stderr
    assert "usage: agileforge-dev" in stdout
    assert "HOSTILE_UV_ENVIRONMENT" not in stdout


def test_bootstrap_rejects_real_shadow_module_source_controls(
    tmp_path: Path,
) -> None:
    """Import this checkout's dev CLI despite hostile Python source controls."""
    module_path = Path(cast("str", _module().__file__))
    launcher = module_path.parents[1] / "agileforge-dev"
    shadow_root = tmp_path / "shadow-source"
    shadow_cli = shadow_root / "cli"
    shadow_cli.mkdir(parents=True)
    (shadow_cli / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "shadow-imported"
    (shadow_cli / "dev_main.py").write_text(
        (
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['SHADOW_IMPORT_MARKER']).write_text('imported')\n"
            "def main():\n"
            "    print('HOSTILE_SHADOW_DEV_MAIN')\n"
            "    return 71\n"
        ),
        encoding="utf-8",
    )
    hostile_environment = {
        **os.environ,
        "PYTHONPATH": str(shadow_root),
        "UV_NO_EDITABLE": "1",
        "SHADOW_IMPORT_MARKER": str(marker),
    }

    exit_code, stdout, stderr = _run_process(
        (str(launcher), "--help"),
        cwd=tmp_path,
        env=hostile_environment,
    )

    assert exit_code == 0, stderr
    assert "usage: agileforge-dev" in stdout
    assert "HOSTILE_SHADOW_DEV_MAIN" not in stdout
    assert not marker.exists()


def test_bootstrap_rejects_symlinked_entrypoint(tmp_path: Path) -> None:
    """Refuse delegation when the invoked bootstrap path is a symlink."""
    module_path = Path(cast("str", _module().__file__))
    launcher = module_path.parents[1] / "agileforge-dev"
    linked_launcher = tmp_path / "linked-launcher"
    try:
        linked_launcher.symlink_to(launcher)
    except OSError as error:
        if getattr(error, "winerror", None) == _WIN_ERROR_PRIVILEGE_NOT_HELD:
            pytest.skip(
                "Windows SeCreateSymbolicLinkPrivilege not held"  # ty: ignore[too-many-positional-arguments]
            )
        raise

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

    class FailingCheckRunner:
        def run(
            self,
            arguments: tuple[str, ...],
            *,
            cwd: Path,
            capture_output: bool,
            env: Mapping[str, str] | None = None,
        ) -> CheckCommandResult:
            assert cwd == checkout
            assert capture_output is False
            assert env is None
            return CheckCommandResult(command=arguments, exit_code=1)

    monkeypatch.setattr(module, "resolve_checkout_root", capture_anchor)

    assert (
        module.main(
            ["check"],
            check_runner=FailingCheckRunner(),
            clock=_clock(),
        )
        == 1
    )
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


def test_init_schema_bootstrap_receives_only_profile_environment(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exclude credentials and parent runtime controls from schema bootstrap."""
    module = _module()
    parent_values = {
        "OPEN_ROUTER_API_KEY": "provider-secret",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",  # nosec B105
        "CUSTOM_CREDENTIAL": "custom-secret",
        "DATABASE_URL": "parent-database",
        "AGILEFORGE_DB_URL": "parent-business-database",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": "parent-trace-database",
        "MODEL_CONFIG_PATH": "parent-model-config",
        "PYTHONPATH": "parent-python-path",
        "UV_PROJECT_ENVIRONMENT": "parent-uv-environment",
    }
    for key, value in parent_values.items():
        monkeypatch.setenv(key, value)
    runner = FakeRunner(checkout)

    exit_code = module.main(
        ["init", "--profile", "sanitized", "--json"],
        checkout_root=checkout,
        runner=runner,
        clock=_clock(),
    )

    assert exit_code == 0
    capsys.readouterr()
    schema_environment = runner.calls[-1][2]
    paths = module.profile_paths(checkout, "sanitized")
    expected_environment = {
        "AGILEFORGE_DB_URL": f"sqlite:///{paths.business_database.as_posix()}",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
            f"sqlite:///{paths.trace_database.as_posix()}"
        ),
        "AGILEFORGE_LAUNCHER_CHILD": "1",
        "MODEL_CONFIG_PATH": str(checkout / "config" / "models.yaml"),
        "SPECIFICATION_STRUCTURER_MAX_TOKENS": "32768",
    }
    if os.name == "nt":
        expected_environment["SystemRoot"] = os.environ["SYSTEMROOT"]
        temp_directory = str(paths.root.resolve())
        expected_environment["TEMP"] = temp_directory
        expected_environment["TMP"] = temp_directory
    expected_environment["GIT_PYTHON_GIT_EXECUTABLE"] = (
        module._resolve_git_executable()
    )
    assert schema_environment is not None
    assert schema_environment == expected_environment
    for secret_value in parent_values.values():
        assert secret_value not in schema_environment.values()


def test_command_runner_does_not_inherit_parent_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass an explicit child environment without GitPython parent merging."""
    module = _module()
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("AGILEFORGE_DB_URL", "must-not-reach-child")
    probe = (
        "import os; "
        "print('OPEN_ROUTER_API_KEY' in os.environ or "
        "'AGILEFORGE_DB_URL' in os.environ)"
    )

    result = module.SubprocessCommandRunner().run(
        (sys.executable, "-c", probe),
        cwd=tmp_path,
        env={"SAFE_BOOTSTRAP_VALUE": "1"},
    )

    assert result.exit_code == 0
    assert result.stdout == "False\n"


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report complete non-secret provenance plus current validation state."""
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
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
    assert payload["configured_models"] == [{"role": "default", "model_id": "fixture"}]
    assert payload["provider_credentials"] == {"OPEN_ROUTER_API_KEY": False}
    paths = module.profile_paths(checkout, "local")
    assert payload["child_runtime_environment"] == {
        "AGILEFORGE_DB_URL": f"sqlite:///{paths.business_database.as_posix()}",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
            f"sqlite:///{paths.trace_database.as_posix()}"
        ),
        "AGILEFORGE_LAUNCHER_CHILD": "1",
        "MODEL_CONFIG_PATH": str(checkout / "config" / "models.yaml"),
        "SPECIFICATION_STRUCTURER_MAX_TOKENS": 32_768,
    }
    serialized = json.dumps(payload).lower()
    assert serialized.count("open_router_api_key") == 1
    for secret_field in ('"password"', '"access_token"', '"api_token"'):
        assert secret_field not in serialized


def test_info_rejects_profile_created_by_a_different_python_runtime(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail closed instead of reporting stale interpreter provenance."""
    module = _module()
    assert (
        module.main(
            ["init", "--profile", "legacy-python", "--json"],
            checkout_root=checkout,
            runner=FakeRunner(checkout),
            clock=_clock(),
        )
        == 0
    )
    capsys.readouterr()
    manifest = module.profile_paths(checkout, "legacy-python").manifest
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["python_version"] = "3.12.13"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = module.main(
        ["info", "--profile", "legacy-python", "--json"],
        checkout_root=checkout,
        runner=FakeRunner(checkout),
        clock=_clock(minute=1),
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "exit_code": 1,
        "error": "profile Python version does not match the current runtime",
    }


def test_info_secrets_file_reports_presence_without_credential_value(
    checkout: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse descriptor-safe secret precedence and emit only a presence boolean."""
    module = _module()
    assert (
        module.main(
            ["init", "--profile", "preflight", "--json"],
            checkout_root=checkout,
            runner=FakeRunner(checkout),
            clock=_clock(),
        )
        == 0
    )
    capsys.readouterr()
    credential = "preflight-provider-value-must-not-leak"
    secrets_file = tmp_path / "provider.env"
    secrets_file.write_text(
        f"OPEN_ROUTER_API_KEY={credential}\nMODEL_CONFIG_PATH=forbidden\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)

    exit_code = module.main(
        [
            "info",
            "--profile",
            "preflight",
            "--secrets-file",
            str(secrets_file),
            "--json",
        ],
        checkout_root=checkout,
        runner=FakeRunner(checkout),
        clock=_clock(minute=1),
    )

    captured = capsys.readouterr()
    if not hasattr(os, "O_NOFOLLOW"):
        assert exit_code == 1
        assert "secrets file must be a regular file" in captured.out
        assert credential not in captured.out
        assert credential not in captured.err
        return

    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["provider_credentials"] == {"OPEN_ROUTER_API_KEY": True}
    assert credential not in captured.out
    assert credential not in captured.err
    assert payload["child_runtime_environment"]["MODEL_CONFIG_PATH"] == str(
        checkout / "config" / "models.yaml"
    )


def test_real_launcher_child_ignores_checkout_dotenv_controls_and_credentials(
    checkout: Path,
) -> None:
    """Prove the real child import cannot load checkout dotenv values."""
    module = _module()
    source_root = Path(cast("str", module.__file__)).parents[1]
    local_utils = checkout / "utils"
    local_utils.mkdir()
    (local_utils / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(source_root / "utils" / "runtime_config.py", local_utils)
    shutil.copy2(source_root / "utils" / "runtime_controls.py", local_utils)
    credential = "checkout-dotenv-provider-must-not-leak"
    poison_values = (
        credential,
        "sqlite:///dotenv-business.sqlite3",
        "sqlite:///dotenv-trace.sqlite3",
        "dotenv-models.yaml",
        "true",
    )
    (checkout / ".env").write_text(
        "\n".join(
            (
                f"OPEN_ROUTER_API_KEY={credential}",
                "AGILEFORGE_DB_URL=sqlite:///dotenv-business.sqlite3",
                "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL=sqlite:///dotenv-trace.sqlite3",
                "MODEL_CONFIG_PATH=dotenv-models.yaml",
                "RELAX_ZDR_FOR_TESTS=true",
            )
        ),
        encoding="utf-8",
    )
    profile = initialize_profile_record(checkout, "real-child")
    child_environment = module._launcher_child_environment(profile)
    probe = (
        "import json, os; import utils.runtime_config; "
        "print(json.dumps({"
        "'business': os.environ.get('AGILEFORGE_DB_URL'), "
        "'trace': os.environ.get('AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL'), "
        "'model': os.environ.get('MODEL_CONFIG_PATH'), "
        "'provider_present': 'OPEN_ROUTER_API_KEY' in os.environ, "
        "'relax_present': 'RELAX_ZDR_FOR_TESTS' in os.environ}))"
    )

    result = module.SubprocessCommandRunner().run(
        (sys.executable, "-c", probe),
        cwd=checkout,
        env=child_environment,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload == {
        "business": child_environment["AGILEFORGE_DB_URL"],
        "trace": child_environment["AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL"],
        "model": child_environment["MODEL_CONFIG_PATH"],
        "provider_present": False,
        "relax_present": False,
    }
    for poison in poison_values:
        assert poison not in result.stdout
        assert poison not in result.stderr


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


def test_real_launcher_child_environment_preserves_windows_systemroot(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve Windows SystemRoot for child asyncio while isolating credentials."""
    module = _module()
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "must-not-leak-provider-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak-aws-secret")
    monkeypatch.setenv("CUSTOM_PARENT_CONTROL", "parent-control-value")
    monkeypatch.setenv("RELAX_ZDR_FOR_TESTS", "true")

    profile = module.prepare_profile_record(
        checkout, "asyncio-child", module.ProfileMode.DEVELOPMENT
    )
    child_environment = module._launcher_child_environment(profile)

    assert child_environment["AGILEFORGE_LAUNCHER_CHILD"] == "1"
    assert "AGILEFORGE_DB_URL" in child_environment
    assert "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL" in child_environment
    assert "MODEL_CONFIG_PATH" in child_environment
    assert "SPECIFICATION_STRUCTURER_MAX_TOKENS" in child_environment
    assert "GIT_PYTHON_GIT_EXECUTABLE" in child_environment

    assert "OPEN_ROUTER_API_KEY" not in child_environment
    assert "AWS_SECRET_ACCESS_KEY" not in child_environment
    assert "CUSTOM_PARENT_CONTROL" not in child_environment
    assert "RELAX_ZDR_FOR_TESTS" not in child_environment

    paths = module.profile_paths(checkout, "asyncio-child")
    if os.name == "nt":
        assert "SystemRoot" in child_environment
        assert child_environment["SystemRoot"] == os.environ["SYSTEMROOT"]
        assert "TEMP" in child_environment
        assert "TMP" in child_environment
        assert (
            child_environment["TEMP"]
            == child_environment["TMP"]
            == str(paths.root.resolve())
        )
    else:
        assert "SystemRoot" not in child_environment
        assert "TEMP" not in child_environment
        assert "TMP" not in child_environment

    validated = module.ChildRuntimeEnvironment.model_validate(child_environment)
    dumped = validated.model_dump(by_alias=True)
    assert set(dumped.keys()) == {
        "AGILEFORGE_DB_URL",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        "AGILEFORGE_LAUNCHER_CHILD",
        "MODEL_CONFIG_PATH",
        "SPECIFICATION_STRUCTURER_MAX_TOKENS",
    }
    assert "SystemRoot" not in dumped
    assert "GIT_PYTHON_GIT_EXECUTABLE" not in dumped
    assert "TEMP" not in dumped
    assert "TMP" not in dumped

    probe = (
        "import json, os\n"
        "import asyncio\n"
        "print(json.dumps({\n"
        "    'system_root': os.environ.get('SYSTEMROOT'),\n"
        "    'child_marker': os.environ.get('AGILEFORGE_LAUNCHER_CHILD'),\n"
        "    'provider_present': 'OPEN_ROUTER_API_KEY' in os.environ,\n"
        "    'aws_present': 'AWS_SECRET_ACCESS_KEY' in os.environ,\n"
        "    'custom_present': 'CUSTOM_PARENT_CONTROL' in os.environ,\n"
        "}))\n"
    )

    runner = module.SubprocessCommandRunner()
    result = runner.run(
        (sys.executable, "-c", probe),
        cwd=checkout,
        env=child_environment,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["child_marker"] == "1"
    assert payload["provider_present"] is False
    assert payload["aws_present"] is False
    assert payload["custom_present"] is False
    if os.name == "nt":
        assert payload["system_root"] == os.environ["SYSTEMROOT"]


def test_real_launcher_child_environment_configures_windows_temp_for_sqlite_spill(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure child environment sets writable TEMP/TMP on Windows for sort spills."""
    module = _module()
    for k, v in {
        "OPEN_ROUTER_API_KEY": "must-not-leak-provider-secret",
        # Test-only AWS credential sentinel: the child environment must remove it.
        "AWS_SECRET_ACCESS_KEY": "must-not-leak-aws-secret",  # nosec B105
        "CUSTOM_PARENT_CONTROL": "parent-control-value",
        "USERPROFILE": r"C:\Users\untrusted_parent",
        "LOCALAPPDATA": r"C:\Users\untrusted_parent\AppData\Local",
    }.items():
        monkeypatch.setenv(k, v)

    profile = module.prepare_profile_record(
        checkout, "sqlite-temp-spill", module.ProfileMode.DEVELOPMENT
    )
    child_environment = module._launcher_child_environment(profile)

    probe = (
        "import json, os, sqlite3, sys\n"
        "from pathlib import Path\n"
        "temp_val = os.environ.get('TEMP')\n"
        "tmp_val = os.environ.get('TMP')\n"
        "results = {\n"
        "    'temp': temp_val,\n"
        "    'tmp': tmp_val,\n"
        "    'provider_present': 'OPEN_ROUTER_API_KEY' in os.environ,\n"
        "    'aws_present': 'AWS_SECRET_ACCESS_KEY' in os.environ,\n"
        "    'custom_present': 'CUSTOM_PARENT_CONTROL' in os.environ,\n"
        "    'userprofile_present': 'USERPROFILE' in os.environ,\n"
        "    'localappdata_present': 'LOCALAPPDATA' in os.environ,\n"
        "    'can_create_temp_file': False,\n"
        "    'sqlite_spill_ok': False,\n"
        "    'sqlite_error': None,\n"
        "    'sqlite_errorcode': None,\n"
        "    'sqlite_errorname': None,\n"
        "}\n"
        "if temp_val and tmp_val and temp_val == tmp_val:\n"
        "    temp_dir = Path(temp_val)\n"
        "    if temp_dir.is_dir():\n"
        "        test_file = temp_dir / f'agileforge_temp_test_{os.getpid()}.tmp'\n"
        "        try:\n"
        "            test_file.write_text('writable', encoding='utf-8')\n"
        "            if test_file.read_text(encoding='utf-8') == 'writable':\n"
        "                results['can_create_temp_file'] = True\n"
        "        finally:\n"
        "            if test_file.exists():\n"
        "                test_file.unlink()\n"
        "conn = sqlite3.connect(':memory:')\n"
        "conn.execute('PRAGMA temp_store = FILE')\n"
        "ts_row = conn.execute('PRAGMA temp_store').fetchone()\n"
        "results['temp_store_value'] = ts_row[0] if ts_row else None\n"
        "conn.execute('CREATE TABLE t (id INT, payload TEXT)')\n"
        "query = 'SELECT id, payload FROM t ORDER BY payload DESC'\n"
        "plan = conn.execute(f'EXPLAIN QUERY PLAN {query}').fetchall()\n"
        "results['temp_btree_used'] = any(\n"
        "    'USE TEMP B-TREE FOR ORDER BY' in str(r) for r in plan\n"
        ")\n"
        "large_payload = 'x' * 65536\n"
        "for i in range(100):\n"
        "    conn.execute('INSERT INTO t VALUES (?, ?)', (i, large_payload + str(i)))\n"
        "try:\n"
        "    rows = conn.execute(query).fetchall()\n"
        "    results['sqlite_spill_ok'] = len(rows) == 100\n"
        "except sqlite3.OperationalError as exc:\n"
        "    results['sqlite_error'] = str(exc)\n"
        "    results['sqlite_errorcode'] = getattr(exc, 'sqlite_errorcode', None)\n"
        "    results['sqlite_errorname'] = getattr(exc, 'sqlite_errorname', None)\n"
        "finally:\n"
        "    conn.close()\n"
        "print(json.dumps(results))\n"
    )

    runner = module.SubprocessCommandRunner()
    result = runner.run(
        (sys.executable, "-c", probe),
        cwd=checkout,
        env=child_environment,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    for key in (
        "provider_present",
        "aws_present",
        "custom_present",
        "userprofile_present",
        "localappdata_present",
    ):
        assert payload[key] is False

    assert payload["temp_store_value"] == 1
    assert payload["temp_btree_used"] is True
    assert payload["sqlite_spill_ok"] is True, (
        f"SQLite sort spill failed: code={payload['sqlite_errorcode']} "
        f"name={payload['sqlite_errorname']} msg={payload['sqlite_error']}"
    )

    paths = module.profile_paths(checkout, "sqlite-temp-spill")
    if os.name == "nt":
        expected_root = str(paths.root.resolve())
        assert payload["sqlite_errorcode"] is None
        assert payload["can_create_temp_file"] is True
        assert "TEMP" in child_environment
        assert "TMP" in child_environment
        assert (
            child_environment["TEMP"]
            == child_environment["TMP"]
            == expected_root
        )
        assert payload["temp"] == payload["tmp"] == expected_root
        assert Path(expected_root).is_absolute()
        assert Path(expected_root).is_dir()
    else:
        assert "TEMP" not in child_environment
        assert "TMP" not in child_environment

    validated = module.ChildRuntimeEnvironment.model_validate(child_environment)
    dumped = validated.model_dump(by_alias=True)
    assert set(dumped.keys()) == {
        "AGILEFORGE_DB_URL",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        "AGILEFORGE_LAUNCHER_CHILD",
        "MODEL_CONFIG_PATH",
        "SPECIFICATION_STRUCTURER_MAX_TOKENS",
    }
    assert "SystemRoot" not in dumped
    assert "GIT_PYTHON_GIT_EXECUTABLE" not in dumped
    if os.name == "nt":
        assert "TEMP" not in dumped
        assert "TMP" not in dumped
        assert validated.temp is not None
        assert validated.tmp is not None
        assert validated.temp == validated.tmp
        assert Path(validated.temp).is_absolute()
    else:
        assert validated.temp is None
        assert validated.tmp is None


def test_resolve_git_executable_validates_path_and_rejects_untrusted_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate that Git resolution enforces absolute, existing, executable paths."""
    module = _module()

    # Case 1: Untrusted relative override is rejected
    monkeypatch.setenv("GIT_PYTHON_GIT_EXECUTABLE", "git")
    with pytest.raises(
        module.DeveloperCommandError,
        match="configured git executable is not a valid executable file: git",
    ):
        module._resolve_git_executable()

    # Case 2: Nonexistent path is rejected
    nonexistent = tmp_path / "does-not-exist" / "git.exe"
    monkeypatch.setenv("GIT_PYTHON_GIT_EXECUTABLE", str(nonexistent))
    with pytest.raises(
        module.DeveloperCommandError,
        match="configured git executable is not a valid executable file",
    ):
        module._resolve_git_executable()

    # Case 3: Non-file directory is rejected
    directory_path = tmp_path / "git_dir"
    directory_path.mkdir()
    monkeypatch.setenv("GIT_PYTHON_GIT_EXECUTABLE", str(directory_path))
    with pytest.raises(
        module.DeveloperCommandError,
        match="configured git executable is not a valid executable file",
    ):
        module._resolve_git_executable()

    # Case 4: Default resolution without override resolves real git from PATH
    monkeypatch.delenv("GIT_PYTHON_GIT_EXECUTABLE", raising=False)
    resolved = module._resolve_git_executable()
    assert Path(resolved).is_file()
    assert Path(resolved).is_absolute()
    assert os.access(resolved, os.X_OK)


def test_is_writable_directory_cleanup_safety(tmp_path: Path) -> None:
    """Verify write probe creates, writes, cleans up, and does not truncate."""
    module = _module()
    test_dir = tmp_path / "probe_cleanup_test"
    test_dir.mkdir()

    existing_file = test_dir / "keep_me.txt"
    existing_file.write_text("precious data", encoding="utf-8")

    assert module._is_writable_directory(test_dir) is True
    assert existing_file.read_text(encoding="utf-8") == "precious data"
    assert list(test_dir.iterdir()) == [existing_file]

    original_tf = module.tempfile.TemporaryFile

    def failing_tempfile(*args: object, **kwargs: object) -> object:
        f = original_tf(*args, **kwargs)
        f.close()
        msg = "simulated write failure"
        raise OSError(msg)

    with unittest.mock.patch.object(
        module.tempfile, "TemporaryFile", side_effect=failing_tempfile
    ):
        assert module._is_writable_directory(test_dir) is False

    assert existing_file.read_text(encoding="utf-8") == "precious data"
    assert list(test_dir.iterdir()) == [existing_file]


def test_launcher_child_environment_ignores_ambient_temp_and_uses_profile_root(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure parent TEMP/TMP/TMPDIR are ignored and gettempdir is not called."""
    module = _module()
    for k, v in {
        "TEMP": r"\\hostile-server\share\temp",
        "TMP": "relative/untrusted",
        "TMPDIR": r"C:\CON",
        "OPEN_ROUTER_API_KEY": "secret-provider-key",
        # Test-only AWS credential sentinel: the child environment must remove it.
        "AWS_SECRET_ACCESS_KEY": "secret-aws-key",  # nosec B105
        "CUSTOM_PARENT_CONTROL": "hostile-control",
        "USERPROFILE": r"C:\Users\untrusted",
        "LOCALAPPDATA": r"C:\Users\untrusted\AppData\Local",
    }.items():
        monkeypatch.setenv(k, v)

    def bomb_gettempdir() -> str:
        msg = "BOMB: tempfile.gettempdir() must not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(module.tempfile, "gettempdir", bomb_gettempdir)

    profile = module.prepare_profile_record(
        checkout, "temp-isolation", module.ProfileMode.DEVELOPMENT
    )
    paths = module.profile_paths(checkout, "temp-isolation")

    env = module._launcher_child_environment(profile)

    if os.name == "nt":
        expected_root = str(paths.root.resolve())
        assert env["TEMP"] == expected_root
        assert env["TMP"] == expected_root

        probe = (
            "import os, tempfile\n"
            "temp_val = os.environ['TEMP']\n"
            "with tempfile.TemporaryFile(dir=temp_val) as f:\n"
            "    f.write(b'isolated')\n"
            "    f.flush()\n"
            "print('ok')\n"
        )
        runner = module.SubprocessCommandRunner()
        res = runner.run((sys.executable, "-c", probe), cwd=checkout, env=env)
        assert res.exit_code == 0
        assert res.stdout.strip() == "ok"
    else:
        assert "TEMP" not in env
        assert "TMP" not in env

    for absent in (
        "OPEN_ROUTER_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "CUSTOM_PARENT_CONTROL",
        "USERPROFILE",
        "LOCALAPPDATA",
    ):
        assert absent not in env

    validated = module.ChildRuntimeEnvironment.model_validate(env)
    dumped = validated.model_dump(by_alias=True)
    assert "TEMP" not in dumped
    assert "TMP" not in dumped


def test_real_launcher_child_environment_executes_git_cli_and_dashboard_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute GitPython, CLI, and dashboard provenance in child environment."""
    module = _module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Tester")
    _git(repo_root, "config", "user.email", "tester@example.invalid")
    (repo_root / "config").mkdir()
    models_config = Path.cwd() / "config" / "models.yaml"
    (repo_root / "config" / "models.yaml").write_text(
        models_config.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo_root / "agile_sqlmodel.py").write_text("pass\n", encoding="utf-8")
    (repo_root / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "init")

    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "must-not-leak-provider-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak-aws-secret")
    monkeypatch.setenv("CUSTOM_PARENT_CONTROL", "parent-control-value")

    database_path = tmp_path / "synthetic_business.sqlite3"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    ensure_business_db_ready(engine)
    engine.dispose()

    profile = module.prepare_profile_record(
        repo_root, "temp-exec", module.ProfileMode.DEVELOPMENT
    )
    child_environment = module._launcher_child_environment(profile)
    child_environment["AGILEFORGE_DB_URL"] = f"sqlite:///{database_path.as_posix()}"

    database_target = Path(
        child_environment["AGILEFORGE_DB_URL"].removeprefix("sqlite:///")
    ).resolve()
    assert database_target.is_relative_to(tmp_path.resolve())
    child_environment["ALLOW_PROD_DB_IN_TEST"] = "1"

    assert "GIT_PYTHON_GIT_EXECUTABLE" in child_environment
    assert Path(child_environment["GIT_PYTHON_GIT_EXECUTABLE"]).is_file()
    assert "OPEN_ROUTER_API_KEY" not in child_environment
    assert "AWS_SECRET_ACCESS_KEY" not in child_environment

    probe = (
        "import pytest_socket\n"
        "pytest_socket.disable_socket()\n"
        "import json, os\n"
        "from pathlib import Path\n"
        "from git import Repo\n"
        "from api import _checkout_commit, _runtime_provenance, get_dashboard_config\n"
        "root = Path(os.getcwd())\n"
        "repo = Repo(root)\n"
        "head_sha = repo.head.commit.hexsha\n"
        "commit = _checkout_commit(root)\n"
        "provenance = _runtime_provenance(root)\n"
        "dashboard_config = get_dashboard_config()\n"
        "print(json.dumps({\n"
        "    'git_python_imported': True,\n"
        "    'head_sha': head_sha,\n"
        "    'commit': commit,\n"
        "    'provenance': provenance,\n"
        "    'dashboard_commit': dashboard_config.commit,\n"
        "    'dashboard_db': str(dashboard_config.business_database),\n"
        "    'provider_present': 'OPEN_ROUTER_API_KEY' in os.environ,\n"
        "    'aws_present': 'AWS_SECRET_ACCESS_KEY' in os.environ,\n"
        "}))\n"
    )

    runner = module.SubprocessCommandRunner()
    probe_result = runner.run(
        (sys.executable, "-c", probe),
        cwd=repo_root,
        env=child_environment,
    )
    assert probe_result.exit_code == 0, f"probe stderr:\n{probe_result.stderr}"
    probe_payload = json.loads(probe_result.stdout)
    assert probe_payload["git_python_imported"] is True
    assert len(probe_payload["head_sha"]) == _COMMIT_HASH_LENGTH
    assert probe_payload["commit"] == probe_payload["head_sha"]
    assert probe_payload["provenance"] == probe_payload["head_sha"]
    assert len(probe_payload["dashboard_commit"]) == _COMMIT_HASH_LENGTH
    assert probe_payload["dashboard_db"] == str(database_path)
    assert probe_payload["provider_present"] is False
    assert probe_payload["aws_present"] is False

    cli_probe = (
        "import pytest_socket\n"
        "pytest_socket.disable_socket()\n"
        "import sys\n"
        "from cli.main import main\n"
        "sys.argv = ['agileforge', 'project', 'list']\n"
        "sys.exit(main())\n"
    )
    cli_result = runner.run(
        (sys.executable, "-c", cli_probe),
        cwd=repo_root,
        env=child_environment,
    )
    assert cli_result.exit_code == 0, f"cli stderr:\n{cli_result.stderr}"
    cli_payload = json.loads(cli_result.stdout)
    assert cli_payload["ok"] is True
    assert cli_payload["data"]["count"] == 0


def test_selected_git_executable_reuses_gitpython_and_ignores_later_environment_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later changes to GIT_PYTHON_GIT_EXECUTABLE do not alter API git execution."""
    current_gitpython = getattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", None)
    assert isinstance(current_gitpython, str)
    assert current_gitpython
    assert _selected_git_executable() == current_gitpython

    monkeypatch.setenv("GIT_PYTHON_GIT_EXECUTABLE", "nonexistent-git-override")
    assert _selected_git_executable() == current_gitpython

    checkout_root = Path(__file__).resolve().parents[2]
    commit = _checkout_commit(checkout_root)
    assert len(commit) == _COMMIT_HASH_LENGTH
    provenance = _runtime_provenance(checkout_root)
    assert provenance == commit
