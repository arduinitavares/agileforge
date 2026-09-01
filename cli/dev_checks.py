"""Ordered, uv-owned repository quality checks."""

from __future__ import annotations

import subprocess  # nosec B404
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

MAX_SUMMARY_CHARACTERS = 4_000
type CheckStageName = Literal[
    "lock",
    "python-quality",
    "frontend-tests",
    "whitespace",
    "distributions",
]


@dataclass(frozen=True, slots=True)
class CheckStage:
    """One immutable quality stage."""

    name: CheckStageName
    command: tuple[str, ...]
    artifact_paths: tuple[str, ...] = ()


CHECK_STAGES: tuple[CheckStage, ...] = (
    CheckStage(name="lock", command=("uv", "lock", "--check")),
    CheckStage(
        name="python-quality",
        command=("pyrepo-check", "--python", "3.13.15", "--all"),
    ),
    CheckStage(
        name="frontend-tests",
        command=(
            "node",
            "--test",
            "tests/test_workflow_position_display.mjs",
            "tests/test_create_project_modal_required_fields.mjs",
            "tests/test_vision_interview_ui.mjs",
        ),
    ),
    CheckStage(name="whitespace", command=("git", "diff", "--check")),
    CheckStage(
        name="distributions",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/verify_distribution.py",
        ),
        artifact_paths=("scripts/verify_distribution.py",),
    ),
)


@dataclass(frozen=True, slots=True)
class CheckCommandResult:
    """Raw result from one fixed-argv child command."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class CheckRunner(Protocol):
    """Execute quality commands without a shell."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        capture_output: bool,
        env: Mapping[str, str] | None = None,
    ) -> CheckCommandResult:
        """Run one fixed command."""
        ...


@dataclass(frozen=True, slots=True)
class SubprocessCheckRunner:
    """Run fixed quality commands through subprocess."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        capture_output: bool,
        env: Mapping[str, str] | None = None,
    ) -> CheckCommandResult:
        """Run one stage, inheriting output unless capture was requested."""
        completed = subprocess.run(  # noqa: S603  # nosec B603
            arguments,
            cwd=cwd,
            env=None if env is None else dict(env),
            check=False,
            capture_output=capture_output,
            text=True,
        )
        return CheckCommandResult(
            command=arguments,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


@dataclass(frozen=True, slots=True)
class CheckStageResult:
    """Bounded result for one completed quality stage."""

    name: CheckStageName
    command: tuple[str, ...]
    exit_code: int
    elapsed_seconds: float
    stdout_summary: str
    stderr_summary: str
    artifact_paths: tuple[str, ...]

    def to_json_object(self) -> dict[str, object]:
        """Return a stable JSON-compatible stage payload."""
        return {
            "name": self.name,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "elapsed_seconds": self.elapsed_seconds,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "artifact_paths": list(self.artifact_paths),
        }


@dataclass(frozen=True, slots=True)
class RepositoryCheckResult:
    """Typed aggregate result for the repository quality gate."""

    command: Literal["check"]
    exit_code: int
    elapsed_seconds: float
    failed_stage: CheckStageName | None
    stages: tuple[CheckStageResult, ...]

    def to_json_object(self) -> dict[str, object]:
        """Return a stable JSON-compatible result payload."""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "elapsed_seconds": self.elapsed_seconds,
            "failed_stage": self.failed_stage,
            "stages": [stage.to_json_object() for stage in self.stages],
        }


def _bounded_summary(value: str) -> str:
    return value[-MAX_SUMMARY_CHARACTERS:]


def run_repository_checks(
    checkout_root: Path,
    *,
    runner: CheckRunner | None = None,
    json_output: bool,
    monotonic: Callable[[], float] = time.monotonic,
) -> RepositoryCheckResult:
    """Run the repository gate in fixed order and stop at first failure."""
    command_runner = runner or SubprocessCheckRunner()
    started = monotonic()
    stage_results: list[CheckStageResult] = []
    failed_stage: CheckStageName | None = None
    exit_code = 0

    for stage in CHECK_STAGES:
        stage_started = monotonic()
        command_result = command_runner.run(
            stage.command,
            cwd=checkout_root,
            capture_output=json_output,
        )
        stage_results.append(
            CheckStageResult(
                name=stage.name,
                command=command_result.command,
                exit_code=command_result.exit_code,
                elapsed_seconds=monotonic() - stage_started,
                stdout_summary=_bounded_summary(command_result.stdout),
                stderr_summary=_bounded_summary(command_result.stderr),
                artifact_paths=stage.artifact_paths,
            )
        )
        if command_result.exit_code != 0:
            failed_stage = stage.name
            exit_code = command_result.exit_code
            break

    return RepositoryCheckResult(
        command="check",
        exit_code=exit_code,
        elapsed_seconds=monotonic() - started,
        failed_stage=failed_stage,
        stages=tuple(stage_results),
    )
