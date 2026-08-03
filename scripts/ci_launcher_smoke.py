"""Exercise the checkout-local developer launcher for macOS CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter, ValidationError

from cli.ci_launcher_smoke_runtime import (
    POLL_SECONDS,
    READY_TIMEOUT_SECONDS,
    STOP_TIMEOUT_SECONDS,
    LocalProfiles,
    LocalRuntime,
    Profiles,
    Runtime,
    safe_environment,
)
from cli.dev_main import CliResult, InfoResult, InitResult, UiReadyResult
from workflow.contracts import JsonObject

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cli.dev_server import ManagedProcess

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_JSON_OBJECT = TypeAdapter(JsonObject)


class ErrorCode(StrEnum):
    """Fixed non-sensitive executable failures."""

    CLEANUP = "cleanup failed"
    COMMAND = "launcher command failed"
    HEAD = "HEAD does not match expected SHA"
    INTERNAL = "internal failure"
    OUTPUT = "invalid launcher output"
    PROFILE = "invalid or existing profile"
    UI = "UI lifecycle failed"


class SmokeError(RuntimeError):
    """One fixed launcher smoke failure."""

    def __init__(self, code: ErrorCode) -> None:
        """Store one safe error code."""
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class SmokeRequest:
    """Identity for one acceptance-profile smoke."""

    profile: str
    expected_sha: str


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """Verified identity returned after complete cleanup."""

    profile: str
    expected_sha: str


def _run_json[ModelT: BaseModel](
    runtime: Runtime,
    checkout_root: Path,
    arguments: tuple[str, ...],
    model: type[ModelT],
) -> ModelT:
    try:
        result = runtime.run(arguments, cwd=checkout_root)
        if result.exit_code != 0:
            raise SmokeError(ErrorCode.COMMAND)
        payload = _JSON_OBJECT.validate_json(result.stdout)
        if model in (InitResult, InfoResult) and "schema" in payload:
            payload = {
                **payload,
                "schema_validation": payload["schema"],
            }
            del payload["schema"]
        return model.model_validate(payload)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(ErrorCode.COMMAND) from error
    except ValidationError as error:
        raise SmokeError(ErrorCode.OUTPUT) from error


def _verify_reads(
    runtime: Runtime,
    checkout_root: Path,
    request: SmokeRequest,
) -> None:
    launcher = "./agileforge-dev"
    init = _run_json(
        runtime,
        checkout_root,
        (
            launcher,
            "init",
            "--profile",
            request.profile,
            "--mode",
            "acceptance",
            "--expect-sha",
            request.expected_sha,
            "--json",
        ),
        InitResult,
    )
    info = _run_json(
        runtime,
        checkout_root,
        (launcher, "info", "--profile", request.profile, "--json"),
        InfoResult,
    )
    cli = _run_json(
        runtime,
        checkout_root,
        (
            launcher,
            "cli",
            "--profile",
            request.profile,
            "--json",
            "--",
            "project",
            "list",
        ),
        CliResult,
    )
    valid = (
        init.status == "initialized"
        and init.profile.name == request.profile
        and init.profile.expected_commit == request.expected_sha
        and info.current_commit == request.expected_sha
        and info.profile.expected_commit == request.expected_sha
        and cli.commit == request.expected_sha
        and cli.profile == request.profile
        and cli.command == ("project", "list")
        and cli.exit_code == 0
        and cli.result.get("ok") is True
    )
    if not valid:
        raise SmokeError(ErrorCode.OUTPUT)


def _wait_for_ui_json(
    runtime: Runtime,
    process: ManagedProcess,
    output: Path,
) -> UiReadyResult:
    deadline = runtime.monotonic() + READY_TIMEOUT_SECONDS
    while runtime.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeError(ErrorCode.UI)
        try:
            raw = output.read_text(encoding="utf-8")
            if raw:
                return UiReadyResult.model_validate_json(raw)
        except (OSError, ValidationError):
            pass
        runtime.sleep(POLL_SECONDS)
    raise SmokeError(ErrorCode.UI)


def _verify_stopped(
    runtime: Runtime,
    process: ManagedProcess,
    child_process_id: int | None,
    port: int | None,
) -> None:
    launcher_stopped = process.poll() == 0
    group_stopped = not runtime.group_exists(process.pid)
    child_stopped = child_process_id is None or not runtime.process_exists(
        child_process_id
    )
    endpoint_stopped = port is None
    if port is not None:
        deadline = runtime.monotonic() + STOP_TIMEOUT_SECONDS
        while runtime.monotonic() < deadline:
            if not runtime.endpoint_reachable(port):
                endpoint_stopped = True
                break
            runtime.sleep(POLL_SECONDS)
    if not all((launcher_stopped, group_stopped, child_stopped, endpoint_stopped)):
        raise SmokeError(ErrorCode.CLEANUP)


def _noop(_process: ManagedProcess) -> None:
    """Default test hook after process-group acquisition."""


def _run_ui(
    runtime: Runtime,
    checkout_root: Path,
    request: SmokeRequest,
    acquired_hook: Callable[[ManagedProcess], None],
) -> None:
    process: ManagedProcess | None = None
    child_process_id: int | None = None
    port: int | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="agileforge-ci-launcher-") as temp:
            output = Path(temp) / "ui.json"
            log = Path(temp) / "ui.log"
            with (
                output.open("w", encoding="utf-8") as stdout,
                log.open("w", encoding="utf-8") as stderr,
            ):
                process = runtime.start_ui(
                    (
                        "./agileforge-dev",
                        "ui",
                        "--profile",
                        request.profile,
                        "--ephemeral",
                        "--port",
                        "auto",
                        "--json",
                        "--ready-timeout",
                        "30",
                    ),
                    cwd=checkout_root,
                    stdout=stdout,
                    stderr=stderr,
                )
                acquired_hook(process)
                ready = _wait_for_ui_json(runtime, process, output)
                if (
                    ready.checkout != checkout_root
                    or ready.commit != request.expected_sha
                    or ready.profile_mode.value != "acceptance"
                    or not ready.ephemeral
                    or not ready.profile.startswith(f"{request.profile}.ui-")
                ):
                    raise SmokeError(ErrorCode.OUTPUT)
                port = ready.port
                child_process_id = runtime.wait_ready(
                    process,
                    ready,
                    request.expected_sha,
                ).process_id
    finally:
        if process is not None:
            runtime.stop_ui(process)
            _verify_stopped(runtime, process, child_process_id, port)


def run_smoke(
    checkout_root: Path,
    request: SmokeRequest,
    runtime: Runtime | None = None,
    profiles: Profiles | None = None,
    acquired_hook: Callable[[ManagedProcess], None] = _noop,
) -> SmokeResult:
    """Run one exact launcher lifecycle and always clean owned profiles."""
    root = checkout_root.resolve(strict=True)
    if _COMMIT_PATTERN.fullmatch(request.expected_sha) is None:
        raise SmokeError(ErrorCode.HEAD)
    selected_runtime = runtime or LocalRuntime(safe_environment(os.environ))
    try:
        selected_profiles = profiles or LocalProfiles.create(root, request.profile)
        selected_profiles.ensure_parent_absent()
    except (OSError, ValueError) as error:
        raise SmokeError(ErrorCode.PROFILE) from error
    try:
        head = selected_runtime.run(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            cwd=root,
        )
        if head.exit_code != 0 or head.stdout.strip() != request.expected_sha:
            raise SmokeError(ErrorCode.HEAD)
        _verify_reads(selected_runtime, root, request)
        selected_profiles.snapshot_before_ui()
        _run_ui(selected_runtime, root, request, acquired_hook)
    finally:
        try:
            selected_profiles.cleanup()
        except (OSError, ValueError) as error:
            raise SmokeError(ErrorCode.CLEANUP) from error
    return SmokeResult(request.profile, request.expected_sha)


def build_parser() -> argparse.ArgumentParser:
    """Build the fixed CI smoke parser."""
    parser = argparse.ArgumentParser(prog="ci_launcher_smoke.py")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expect-sha", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the smoke with fixed safe executable output."""
    arguments = build_parser().parse_args(argv)
    try:
        result = run_smoke(
            Path(__file__).resolve().parents[1],
            SmokeRequest(arguments.profile, arguments.expect_sha),
        )
    except SmokeError as error:
        sys.stderr.write(f"ci launcher smoke failed: {error.code.value}\n")
        return 1
    except Exception:  # noqa: BLE001 - redact executable-boundary failures.
        sys.stderr.write(f"ci launcher smoke failed: {ErrorCode.INTERNAL.value}\n")
        return 1
    sys.stdout.write(
        json.dumps(
            {
                "status": "passed",
                "profile": result.profile,
                "expected_sha": result.expected_sha,
            },
            separators=(",", ":"),
        )
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
