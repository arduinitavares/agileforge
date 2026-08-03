"""UV-owned developer runtime commands for worktree-local profiles."""

from __future__ import annotations

import argparse
import os
import platform
import sqlite3
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol, cast

from git import Git
from git.exc import GitCommandError
from pydantic import BaseModel, ConfigDict, Field

from cli.dev_profiles import (
    ProfileMode,
    ProfileRuntimeMetadata,
    RuntimeProfile,
    finalize_profile_record,
    load_profile,
    prepare_profile_record,
    profile_environment,
    profile_paths,
    reset_profile,
    resolve_checkout_root,
    touch_profile_last_used,
)
from utils.cli_output import emit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

EXPECTED_BUSINESS_TABLES: tuple[str, ...] = (
    "projects",
    "spec_registry",
    "workflow_events",
)
FORBIDDEN_BUSINESS_TABLES: tuple[str, ...] = (
    "products",
    "sessions",
    "cli_mutation_ledger",
)
_MAX_PORT = 65_535
_MIN_UV_VERSION_PARTS = 2
_GIT_COMMIT_LENGTH = 40


class ExitCode(IntEnum):
    """Stable developer launcher process statuses."""

    SUCCESS = 0
    ERROR = 1
    USAGE = 2


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result from one fixed external command."""

    arguments: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """Execute fixed argv without a shell."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run one command and capture its result."""
        ...


class Clock(Protocol):
    """Supply timezone-aware command timestamps."""

    def now(self) -> datetime:
        """Return the current time."""
        ...


@dataclass(frozen=True, slots=True)
class SubprocessCommandRunner:
    """Production command runner using fixed argv and captured output."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run one child process without invoking a shell."""
        command = Git(working_dir=str(cwd))
        if env is not None:
            command.update_environment(**dict(env))
        try:
            output = command.execute(command=list(arguments))
        except GitCommandError as error:
            return CommandResult(
                arguments=arguments,
                exit_code=_git_error_status(error),
                stdout=error.stdout,
                stderr=error.stderr,
            )
        return CommandResult(
            arguments=arguments,
            exit_code=0,
            stdout=cast("str", output),
        )


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Production UTC clock."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(tz=UTC)


class SchemaValidation(BaseModel):
    """Read-only verification result for one business database."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: Literal[True] = True
    tables: tuple[str, ...]
    expected_tables: tuple[str, ...] = EXPECTED_BUSINESS_TABLES
    forbidden_tables: tuple[str, ...] = FORBIDDEN_BUSINESS_TABLES


class InitResult(BaseModel):
    """Stable successful result for profile initialization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["initialized", "existing"]
    profile: RuntimeProfile
    schema_validation: SchemaValidation = Field(serialization_alias="schema")


class InfoResult(BaseModel):
    """Complete redacted provenance and current validation state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validation_status: Literal["valid"] = "valid"
    current_commit: str
    profile: RuntimeProfile
    schema_validation: SchemaValidation = Field(serialization_alias="schema")


class ErrorResult(BaseModel):
    """Stable JSON error result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["error"] = "error"
    exit_code: Literal[1] = 1
    error: str


class DeveloperCommandError(RuntimeError):
    """Expected developer command failure."""


class SchemaVerificationError(DeveloperCommandError):
    """Business database does not satisfy the hard-break schema contract."""


def _git_error_status(error: GitCommandError) -> int:
    return error.status if isinstance(error.status, int) else ExitCode.ERROR


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        message = "value must be greater than zero"
        raise argparse.ArgumentTypeError(message)
    return parsed


def _port(value: str) -> str:
    if value == "auto":
        return value
    try:
        parsed = int(value)
    except ValueError as error:
        message = "port must be 'auto' or an integer from 1 through 65535"
        raise argparse.ArgumentTypeError(message) from error
    if not 1 <= parsed <= _MAX_PORT:
        message = "port must be 'auto' or an integer from 1 through 65535"
        raise argparse.ArgumentTypeError(message)
    return str(parsed)


def _add_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True, help="Runtime profile name")


def build_parser() -> argparse.ArgumentParser:
    """Build the uv-owned developer command parser."""
    parser = argparse.ArgumentParser(prog="agileforge-dev")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Initialize runtime state")
    _add_profile_argument(init_parser)
    init_parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in ProfileMode),
        default=ProfileMode.DEVELOPMENT.value,
    )
    init_parser.add_argument("--expect-sha")
    init_parser.add_argument("--json", action="store_true")

    info_parser = commands.add_parser("info", help="Show validated provenance")
    _add_profile_argument(info_parser)
    info_parser.add_argument("--json", action="store_true")

    cli_parser = commands.add_parser("cli", help="Run the product CLI")
    _add_profile_argument(cli_parser)
    cli_parser.add_argument("--secrets-file", type=Path)
    cli_parser.add_argument("--json", action="store_true")
    cli_parser.add_argument("agileforge_arguments", nargs=argparse.REMAINDER)

    ui_parser = commands.add_parser("ui", help="Run the local dashboard")
    _add_profile_argument(ui_parser)
    ui_parser.add_argument("--secrets-file", type=Path)
    ui_parser.add_argument("--ephemeral", action="store_true")
    ui_parser.add_argument("--port", type=_port, default="auto")
    ui_parser.add_argument("--reload", action="store_true")
    ui_parser.add_argument("--json", action="store_true")
    ui_parser.add_argument("--ready-timeout", type=_positive_float, default=15.0)

    commands.add_parser("check", help="Run the repository quality gate")

    reset_parser = commands.add_parser("reset", help="Remove owned runtime state")
    _add_profile_argument(reset_parser)
    reset_parser.add_argument("--confirm", dest="confirmation", required=True)
    return parser


def _require_success(result: CommandResult, *, label: str) -> CommandResult:
    if result.exit_code == 0:
        return result
    message = f"{label} failed with exit code {result.exit_code}"
    raise DeveloperCommandError(message)


def _current_uv_version(runner: CommandRunner, checkout_root: Path) -> str:
    result = _require_success(
        runner.run(("uv", "--version"), cwd=checkout_root),
        label="uv version lookup",
    )
    parts = result.stdout.split()
    if len(parts) < _MIN_UV_VERSION_PARTS or parts[0] != "uv":
        message = "uv version lookup returned an invalid response"
        raise DeveloperCommandError(message)
    return parts[1]


def _verify_business_schema(database: Path) -> SchemaValidation:
    try:
        metadata = database.lstat()
    except FileNotFoundError as error:
        message = f"business database was not created: {database}"
        raise SchemaVerificationError(message) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        message = f"business database must be a regular file: {database}"
        raise SchemaVerificationError(message)

    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
    except sqlite3.Error as error:
        message = f"business database verification failed: {database}"
        raise SchemaVerificationError(message) from error

    tables = tuple(str(row[0]) for row in rows)
    missing = sorted(set(EXPECTED_BUSINESS_TABLES).difference(tables))
    forbidden = sorted(set(FORBIDDEN_BUSINESS_TABLES).intersection(tables))
    if missing or forbidden:
        details: list[str] = []
        if missing:
            details.append(f"missing tables: {', '.join(missing)}")
        if forbidden:
            details.append(f"forbidden tables: {', '.join(forbidden)}")
        message = f"business schema is invalid ({'; '.join(details)})"
        raise SchemaVerificationError(message)
    return SchemaValidation(tables=tables)


def _validate_exact_existing_profile(
    profile: RuntimeProfile,
    *,
    requested_mode: ProfileMode,
    expected_commit: str | None,
    uv_version: str,
) -> None:
    mismatches: list[str] = []
    if profile.mode is not requested_mode:
        mismatches.append("mode")
    if profile.expected_commit != expected_commit:
        mismatches.append("expected commit")
    if profile.python_version != platform.python_version():
        mismatches.append("Python version")
    if profile.uv_version != uv_version:
        mismatches.append("uv version")
    if mismatches:
        message = f"existing profile does not exactly match: {', '.join(mismatches)}"
        raise DeveloperCommandError(message)


@dataclass(frozen=True, slots=True)
class InitRequest:
    """Validated inputs for one initialization attempt."""

    profile_name: str
    mode: ProfileMode
    expected_commit: str | None


def _initialize_profile(
    *,
    checkout_root: Path,
    request: InitRequest,
    runner: CommandRunner,
    clock: Clock,
) -> InitResult:
    _require_success(
        runner.run(("uv", "lock", "--check"), cwd=checkout_root),
        label="uv lock check",
    )
    uv_version = _current_uv_version(runner, checkout_root)
    paths = profile_paths(checkout_root, request.profile_name)
    try:
        paths.root.lstat()
    except FileNotFoundError:
        root_exists = False
    else:
        root_exists = True

    if root_exists:
        profile = load_profile(checkout_root, request.profile_name)
        _validate_exact_existing_profile(
            profile,
            requested_mode=request.mode,
            expected_commit=request.expected_commit,
            uv_version=uv_version,
        )
        validation = _verify_business_schema(profile.business_database)
        return InitResult(
            status="existing",
            profile=profile,
            schema_validation=validation,
        )

    profile = prepare_profile_record(
        checkout_root,
        request.profile_name,
        request.mode,
        request.expected_commit,
        runtime=ProfileRuntimeMetadata(
            now=clock.now(),
            uv_version=uv_version,
        ),
    )
    finalized = False
    try:
        environment = {**os.environ, **profile_environment(profile)}
        bootstrap_arguments = (
            sys.executable,
            str(checkout_root / "agile_sqlmodel.py"),
        )
        _require_success(
            runner.run(
                bootstrap_arguments,
                cwd=checkout_root,
                env=environment,
            ),
            label="schema bootstrap",
        )
        validation = _verify_business_schema(profile.business_database)
        if profile.trace_database.exists():
            message = "schema bootstrap created the reserved trace database"
            raise SchemaVerificationError(message)
        finalize_profile_record(profile)
        finalized = True
        return InitResult(
            status="initialized",
            profile=profile,
            schema_validation=validation,
        )
    finally:
        if not finalized:
            reset_profile(
                checkout_root,
                request.profile_name,
                request.profile_name,
            )


def _current_commit(runner: CommandRunner, checkout_root: Path) -> str:
    arguments = ("git", "-C", str(checkout_root), "rev-parse", "HEAD")
    result = _require_success(
        runner.run(arguments, cwd=checkout_root),
        label="current commit lookup",
    )
    commit = result.stdout.strip()
    if len(commit) != _GIT_COMMIT_LENGTH or any(
        character not in "0123456789abcdef" for character in commit
    ):
        message = "current commit lookup returned an invalid commit"
        raise DeveloperCommandError(message)
    return commit


def _profile_info(
    *,
    checkout_root: Path,
    profile_name: str,
    runner: CommandRunner,
    clock: Clock,
) -> InfoResult:
    profile = load_profile(checkout_root, profile_name)
    validation = _verify_business_schema(profile.business_database)
    current_commit = _current_commit(runner, checkout_root)
    touched = touch_profile_last_used(
        checkout_root,
        profile_name,
        now=clock.now(),
    )
    return InfoResult(
        current_commit=current_commit,
        profile=touched,
        schema_validation=validation,
    )


def _print_json(payload: BaseModel) -> None:
    emit(payload.model_dump_json(indent=2, by_alias=True))


def _emit_init(result: InitResult, *, json_output: bool) -> None:
    if json_output:
        _print_json(result)
        return
    emit(f"Profile {result.profile.name}: {result.status}")
    emit(f"Checkout: {result.profile.checkout.root}")
    emit(f"Business database: {result.profile.business_database}")


def _emit_info(result: InfoResult, *, json_output: bool) -> None:
    if json_output:
        _print_json(result)
        return
    emit(f"Profile: {result.profile.name} ({result.profile.mode.value})")
    emit(f"Checkout: {result.profile.checkout.root}")
    emit(f"Current commit: {result.current_commit}")
    emit(f"Business database: {result.profile.business_database}")
    emit(f"Trace database: {result.profile.trace_database}")
    emit("Validation: valid")


def _emit_error(error: Exception, *, json_output: bool) -> None:
    if json_output:
        _print_json(ErrorResult(error=str(error)))
        return
    emit(f"error: {error}", file=sys.stderr)


def _unsupported_command(command: str) -> NoReturn:
    message = f"{command} is not implemented by this task"
    raise DeveloperCommandError(message)


def main(
    argv: Sequence[str] | None = None,
    *,
    checkout_root: Path | None = None,
    runner: CommandRunner | None = None,
    clock: Clock | None = None,
) -> int:
    """Run one developer command and return its process exit code."""
    arguments = build_parser().parse_args(argv)
    command_runner = runner or SubprocessCommandRunner()
    command_clock = clock or SystemClock()
    json_output = bool(getattr(arguments, "json", False))
    try:
        root = resolve_checkout_root(checkout_root or Path(__file__).parent)
        if arguments.command == "init":
            result = _initialize_profile(
                checkout_root=root,
                request=InitRequest(
                    profile_name=arguments.profile,
                    mode=ProfileMode(arguments.mode),
                    expected_commit=arguments.expect_sha,
                ),
                runner=command_runner,
                clock=command_clock,
            )
            _emit_init(result, json_output=json_output)
            return ExitCode.SUCCESS
        if arguments.command == "info":
            result = _profile_info(
                checkout_root=root,
                profile_name=arguments.profile,
                runner=command_runner,
                clock=command_clock,
            )
            _emit_info(result, json_output=json_output)
            return ExitCode.SUCCESS
        if arguments.command == "reset":
            removed = reset_profile(
                root,
                arguments.profile,
                arguments.confirmation,
            )
            emit(f"Removed profile {arguments.profile}:")
            for path in removed:
                emit(path)
            return ExitCode.SUCCESS
        _unsupported_command(arguments.command)
    except (GitCommandError, OSError, ValueError, DeveloperCommandError) as error:
        _emit_error(error, json_output=json_output)
        return ExitCode.ERROR


if __name__ == "__main__":
    raise SystemExit(main())
