"""CLI boundary tests for semantic Vision bootstrap."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest

from cli import main as cli_main
from services.application import VisionBootstrapRequest
from workflow.contracts import (
    JsonObject,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)

PROJECT_ID = 41


def _position() -> WorkflowPosition:
    return WorkflowPosition(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="sha256:facts",
        evaluated_at=datetime(2026, 8, 10, tzinfo=UTC),
        available_nodes=(),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=(),
    )


class _CapturingApplication:
    def __init__(self) -> None:
        self.requests: list[VisionBootstrapRequest] = []

    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult:
        self.requests.append(request)
        return TransitionResult(ok=True, applied_node_id="vision.bootstrap")


class _CapabilityFailureApplication:
    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult:
        del request
        return TransitionResult(
            ok=False,
            error=WorkflowError(
                code=WorkflowErrorCode.REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE,
                message="Repository evidence is unavailable.",
            ),
        )


class _LostWorktreeApplication:
    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult:
        del request
        return TransitionResult(
            ok=False,
            error=WorkflowError(
                code=WorkflowErrorCode.REPOSITORY_PROVENANCE_STALE,
                message="Repository provenance could not be refreshed.",
            ),
        )


class _PureReads:
    def project_show(self, *, project_id: int) -> JsonObject:
        return {"ok": True, "data": {"project_id": project_id}}

    def vision_status(self, *, project_id: int) -> JsonObject:
        return {"ok": True, "data": {"project_id": project_id, "current": None}}


class _PureApplication:
    def __init__(self) -> None:
        self.reads = _PureReads()
        self.position_calls: list[int] = []
        self.bootstrap_calls = 0

    def position(self, *, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return _position()

    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult:
        del request
        self.bootstrap_calls += 1
        pytest.fail(
            "read command invoked Vision bootstrap"  # ty: ignore[invalid-argument-type]
        )


def test_vision_bootstrap_cli_forwards_transport_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Forward semantic bootstrap metadata through the argparse command."""
    application = _CapturingApplication()

    exit_code = cli_main.main(
        [
            "vision",
            "bootstrap",
            "--project-id",
            str(PROJECT_ID),
            "--idempotency-key",
            "vision-bootstrap-41",
            "--actor",
            "operator",
            "--correlation-id",
            "corr-41",
        ],
        application=application,
    )

    assert exit_code == 0
    request = application.requests[0]
    assert isinstance(request, VisionBootstrapRequest)
    assert request.model_dump() == {
        "project_id": PROJECT_ID,
        "idempotency_key": "vision-bootstrap-41",
        "actor": "operator",
        "correlation_id": "corr-41",
    }
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_vision_bootstrap_cli_returns_the_closed_capability_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Match the API capability code without invoking a provider transport."""
    exit_code = cli_main.main(
        [
            "vision",
            "bootstrap",
            "--project-id",
            str(PROJECT_ID),
            "--idempotency-key",
            "vision-capability-41",
            "--actor",
            "operator",
        ],
        application=_CapabilityFailureApplication(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["error"]["code"] == (
        WorkflowErrorCode.REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE.value
    )


def test_vision_bootstrap_cli_preserves_lost_worktree_provenance_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Do not relabel a lost bound checkout as capability unavailable."""
    exit_code = cli_main.main(
        [
            "vision",
            "bootstrap",
            "--project-id",
            str(PROJECT_ID),
            "--idempotency-key",
            "lost-worktree-41",
            "--actor",
            "operator",
        ],
        application=_LostWorktreeApplication(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["error"]["code"] == (
        WorkflowErrorCode.REPOSITORY_PROVENANCE_STALE.value
    )


@pytest.mark.parametrize(
    "flag",
    [
        "--graph-version",
        "--fact-fingerprint",
        "--decision-fingerprint",
        "--evidence-fingerprint",
        "--vision-evidence-snapshot-id",
        "--repository-binding-id",
        "--supersession-id",
        "--mode",
        "--operation",
        "--repository-path",
        "--repository-head-sha",
        "--model-id",
    ],
)
def test_vision_bootstrap_cli_rejects_internal_flags(flag: str) -> None:
    """Reject caller-owned graph, evidence, repository, and model flags."""
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(
            [
                "vision",
                "bootstrap",
                "--project-id",
                str(PROJECT_ID),
                "--idempotency-key",
                "vision-bootstrap-41",
                "--actor",
                "operator",
                flag,
                "caller-owned",
            ]
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["project", "show", "--project-id", str(PROJECT_ID)],
        ["vision", "status", "--project-id", str(PROJECT_ID)],
        ["workflow", "position", "--project-id", str(PROJECT_ID)],
        ["workflow", "next", "--project-id", str(PROJECT_ID)],
    ],
)
def test_cli_read_commands_do_not_invoke_vision_bootstrap(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep CLI project, Vision, and workflow reads side-effect free."""
    application = _PureApplication()

    exit_code = cli_main.main(argv, application=application)

    assert exit_code == 0
    assert application.bootstrap_calls == 0
    assert json.loads(capsys.readouterr().out)


def test_retained_vision_commands_still_parse() -> None:
    """Retain status, respond, review, and revision command parsing."""
    parser = cli_main.build_parser()
    commands = (
        ["vision", "status", "--project-id", str(PROJECT_ID)],
        [
            "vision",
            "respond",
            "--project-id",
            str(PROJECT_ID),
            "--text",
            "Focus on operators.",
            "--idempotency-key",
            "vision-respond-41",
            "--actor",
            "operator",
        ],
        [
            "vision",
            "review",
            "--project-id",
            str(PROJECT_ID),
            "--decision",
            "accepted",
            "--rationale",
            "The direction is correct.",
            "--idempotency-key",
            "vision-review-41",
            "--actor",
            "operator",
        ],
        [
            "vision",
            "revision",
            "--project-id",
            str(PROJECT_ID),
            "--reason",
            "The product intent changed.",
            "--idempotency-key",
            "vision-revision-41",
            "--actor",
            "operator",
        ],
    )

    for argv in commands:
        parsed = parser.parse_args(argv)
        assert cast("int", parsed.project_id) == PROJECT_ID
