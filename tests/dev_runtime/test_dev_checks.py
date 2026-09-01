"""Tests for the ordered repository quality gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cli.dev_checks import (
    MAX_SUMMARY_CHARACTERS,
    CheckCommandResult,
    run_repository_checks,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


_EXPECTED_COMMANDS = (
    ("uv", "lock", "--check"),
    ("pyrepo-check", "--python", "3.13.15", "--all"),
    (
        "node",
        "--test",
        "tests/test_workflow_position_display.mjs",
        "tests/test_create_project_modal_required_fields.mjs",
        "tests/test_vision_interview_ui.mjs",
    ),
    ("git", "diff", "--check"),
    ("uv", "run", "--locked", "python", "scripts/verify_distribution.py"),
)
_FRONTEND_FAILURE = 7
_QUALITY_FAILURE = 9


@dataclass(slots=True)
class FixedRunner:
    """Record fixed argv calls and return configured statuses."""

    exit_codes: tuple[int, ...] = ()
    output_size: int = 0
    calls: list[tuple[tuple[str, ...], Path, bool]] = field(default_factory=list)

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        capture_output: bool,
        env: Mapping[str, str] | None = None,
    ) -> CheckCommandResult:
        """Return one deterministic command result."""
        assert env is None
        self.calls.append((arguments, cwd, capture_output))
        index = len(self.calls) - 1
        exit_code = self.exit_codes[index] if index < len(self.exit_codes) else 0
        output = "x" * self.output_size
        return CheckCommandResult(
            command=arguments,
            exit_code=exit_code,
            stdout=output,
            stderr=output,
        )


def test_repository_checks_run_in_fixed_fail_fast_order(tmp_path: Path) -> None:
    """Run only the immutable argv stages and stop at the first failure."""
    runner = FixedRunner(exit_codes=(0, 0, _FRONTEND_FAILURE, 0, 0))

    result = run_repository_checks(tmp_path, runner=runner, json_output=False)

    assert [call[0] for call in runner.calls] == list(_EXPECTED_COMMANDS[:3])
    assert all(call[1] == tmp_path for call in runner.calls)
    assert all(call[2] is False for call in runner.calls)
    assert result.command == "check"
    assert result.exit_code == _FRONTEND_FAILURE
    assert result.failed_stage == "frontend-tests"
    assert result.elapsed_seconds >= 0
    assert [stage.command for stage in result.stages] == list(_EXPECTED_COMMANDS[:3])


def test_repository_checks_capture_bounded_json_summaries(tmp_path: Path) -> None:
    """Capture bounded diagnostics in JSON mode without changing status."""
    runner = FixedRunner(
        exit_codes=(0, _QUALITY_FAILURE),
        output_size=MAX_SUMMARY_CHARACTERS + 50,
    )

    result = run_repository_checks(tmp_path, runner=runner, json_output=True)
    payload = result.to_json_object()

    assert result.exit_code == _QUALITY_FAILURE
    assert result.failed_stage == "python-quality"
    assert all(call[2] is True for call in runner.calls)
    quality_stage = result.stages[1]
    assert len(quality_stage.stdout_summary) == MAX_SUMMARY_CHARACTERS
    assert len(quality_stage.stderr_summary) == MAX_SUMMARY_CHARACTERS
    assert json.loads(json.dumps(payload))["exit_code"] == _QUALITY_FAILURE


def test_repository_checks_report_distribution_artifact_paths(tmp_path: Path) -> None:
    """Identify the source paths owned by the distribution verification stage."""
    result = run_repository_checks(tmp_path, runner=FixedRunner(), json_output=True)

    assert result.exit_code == 0
    assert result.failed_stage is None
    assert [stage.command for stage in result.stages] == list(_EXPECTED_COMMANDS)
    assert result.stages[-1].artifact_paths == ("scripts/verify_distribution.py",)
