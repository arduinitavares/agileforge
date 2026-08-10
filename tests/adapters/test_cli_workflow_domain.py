"""CLI adapter tests for the WorkflowDomain cutover."""

import importlib
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from cli import main as cli_main
from cli.workflow_commands import (
    workflow_next,
    workflow_position,
)
from tests.adapters.test_command_renderer import position_fixture
from workflow.contracts import WorkflowPosition

if TYPE_CHECKING:
    from services.application import AuthorityFeedbackRequest

SPRINT_CAPACITY_POINTS = 8


@pytest.mark.parametrize(
    "command",
    [
        (
            "agileforge project create --name MyFinance"
            ' --description "Local household finance"'
            " --repository-path /Users/aaat/myfinance"
            " --idempotency-key create-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge vision respond --project-id 1"
            ' --text "The target user manages household finances and needs reliable'
            ' movement reconciliation." --idempotency-key vision-myfinance-1'
            " --actor acceptance-agent"
        ),
        "agileforge vision status --project-id 1",
        (
            "agileforge vision review --project-id 1 --decision accepted"
            ' --rationale "The product direction is accurate."'
            " --idempotency-key vision-review-myfinance-1"
            " --actor acceptance-agent"
        ),
        (
            "agileforge goal respond --project-id 1"
            ' --text "The first valuable future state is reliable Beobank statement'
            ' reconciliation for the household operator."'
            " --idempotency-key goal-myfinance-1 --actor acceptance-agent"
        ),
        "agileforge goal status --project-id 1",
        (
            "agileforge goal review --project-id 1 --decision accepted"
            ' --rationale "The outcome and success signals are correct."'
            " --idempotency-key goal-review-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge repository attach --project-id 1"
            " --path /Users/aaat/myfinance"
            " --idempotency-key attach-myfinance-1 --actor acceptance-agent"
        ),
        "agileforge repository status --project-id 1",
        (
            "agileforge repository refresh --project-id 1"
            " --idempotency-key refresh-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge discovery record --project-id 1"
            " --file /tmp/agileforge-acceptance/discovery.json"
            " --idempotency-key discovery-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge specification record --project-id 1"
            " --file /tmp/agileforge-acceptance/specification.json"
            " --idempotency-key spec-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge specification review --project-id 1 --decision accepted"
            ' --rationale "Desired behavior is correct."'
            " --idempotency-key spec-review-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge goal complete --project-id 1"
            ' --rationale "The accepted success signals were achieved."'
            " --idempotency-key goal-complete-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge goal abandon --project-id 1"
            ' --rationale "The outcome is no longer worth pursuing."'
            " --idempotency-key goal-abandon-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge authority feedback --project-id 1"
            ' --feedback "Narrow the identity invariant."'
            " --idempotency-key authority-feedback-myfinance-1"
            " --actor acceptance-agent"
        ),
    ],
)
def test_semantic_lifecycle_commands_parse(command: str) -> None:
    """Accept every concrete Task 7 lifecycle command without hidden guards."""
    parsed = cli_main.build_parser().parse_args(shlex.split(command)[1:])

    assert not hasattr(parsed, "graph_version")
    assert not hasattr(parsed, "expected_fact_fingerprint")
    assert not hasattr(parsed, "changed_by")


class _AuthorityFeedbackApplication:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def record_authority_feedback(self, request: object) -> object:
        self.requests.append(request)
        return cli_main.TransitionResult(ok=True)


def test_authority_feedback_cli_strips_text_before_calling_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Send only the human feedback text and metadata through the CLI boundary."""
    application = _AuthorityFeedbackApplication()

    exit_code = cli_main.main(
        [
            "authority",
            "feedback",
            "--project-id",
            "41",
            "--feedback",
            "  Narrow the identity invariant.  ",
            "--idempotency-key",
            "feedback-cli-41",
            "--actor",
            "operator",
        ],
        application=application,
    )

    assert exit_code == 0
    request = cast("AuthorityFeedbackRequest", application.requests[0])
    assert request.feedback == "Narrow the identity invariant."
    assert not hasattr(request, "pending_authority_id")
    assert '"ok": true' in capsys.readouterr().out


def test_authority_feedback_cli_returns_structured_invalid_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject whitespace-only feedback without invoking the application."""
    application = _AuthorityFeedbackApplication()
    invalid_input_exit_code = 2

    exit_code = cli_main.main(
        [
            "authority",
            "feedback",
            "--project-id",
            "41",
            "--feedback",
            "  \t",
            "--idempotency-key",
            "feedback-cli-41",
            "--actor",
            "operator",
        ],
        application=application,
    )

    assert exit_code == invalid_input_exit_code
    assert application.requests == []
    assert '"ok": false' in capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        "project abandon",
        "project initial-spec --project-id 41",
        "brownfield curate",
        "scope register",
        "scope extension start",
        "discovery challenge record",
        "discovery prd record",
        "discovery spec record",
        "vision generate",
        "vision decide",
    ],
)
def test_retired_cli_parser_branches_are_absent(command: str) -> None:
    """Reject retired command families at parser selection."""
    with pytest.raises(ValueError, match="invalid choice"):
        cli_main.build_parser().parse_args(shlex.split(command))


@pytest.mark.parametrize(
    "group",
    ["backlog", "roadmap", "story"],
)
def test_retained_agentic_commands_parse_without_model_owned_input(group: str) -> None:
    """Accept host-prepared delivery commands with transport metadata only."""
    parsed = cli_main.build_parser().parse_args(
        [
            group,
            "generate",
            "--project-id",
            "41",
            "--idempotency-key",
            f"{group}-41",
            "--actor",
            "operator",
        ]
    )

    assert not hasattr(parsed, "input_file")
    assert not hasattr(parsed, "model_id")


@pytest.mark.parametrize("flag", ["--input-file", "--model-id"])
def test_removed_agentic_cli_flags_fail_parser_validation(flag: str) -> None:
    """Reject model-owned input and model overrides at the CLI parser."""
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(
            [
                "backlog",
                "generate",
                "--project-id",
                "41",
                "--idempotency-key",
                "backlog-41",
                "--actor",
                "operator",
                flag,
                "caller-owned",
            ]
        )


def test_semantic_sprint_generation_command_parses() -> None:
    """Accept only operator-owned Sprint planning semantics."""
    parsed = cli_main.build_parser().parse_args(
        [
            "sprint",
            "generate",
            "--project-id",
            "41",
            "--input",
            "Prioritize durable replay.",
            "--selected-story-ids",
            "7",
            "9",
            "--max-story-points",
            str(SPRINT_CAPACITY_POINTS),
            "--no-task-decomposition",
            "--team-name",
            "Platform",
            "--idempotency-key",
            "sprint-41",
            "--actor",
            "operator",
        ]
    )

    assert parsed.user_input == "Prioritize durable replay."
    assert parsed.selected_story_ids == [7, 9]
    assert parsed.max_story_points == SPRINT_CAPACITY_POINTS
    assert parsed.include_task_decomposition is False
    assert parsed.team_name == "Platform"
    assert not hasattr(parsed, "model_id")


@pytest.mark.parametrize(
    "flag",
    ["--sprint-duration-days", "--team-velocity-assumption", "--model-id"],
)
def test_removed_sprint_generation_flags_fail_parser_validation(flag: str) -> None:
    """Reject calendar, velocity, and model controls at the Sprint CLI boundary."""
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(
            [
                "sprint",
                "generate",
                "--project-id",
                "41",
                "--team-name",
                "Platform",
                "--idempotency-key",
                "sprint-41",
                "--actor",
                "operator",
                flag,
                "caller-owned",
            ]
        )


def test_version_does_not_compose_production_application(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print installed package version before application composition."""
    version_module = importlib.import_module("services.agent_workbench.version")

    def fail_composition() -> None:
        message = "production application must not be composed"
        raise AssertionError(message)

    monkeypatch.setattr(cli_main, "production_application", fail_composition)

    with pytest.raises(SystemExit) as error:
        cli_main.main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out == f"{version_module.agileforge_version()}\n"


class _FakeApplication:
    def __init__(self, position: WorkflowPosition) -> None:
        self._position = position
        self.position_calls: list[int] = []

    def position(self, *, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return self._position


def test_workflow_next_reads_position_once() -> None:
    """Render workflow-next from exactly one domain position query."""
    application = _FakeApplication(position_fixture())

    payload = workflow_next(application=application, project_id=41)

    assert application.position_calls == [41]
    assert [item["node_id"] for item in payload["commands"]] == [
        "authority.compile",
        "authority.repair",
    ]


def test_workflow_position_can_include_optional_decisions() -> None:
    """Include optional re-entry only when explicitly requested."""
    application = _FakeApplication(position_fixture())

    payload = workflow_position(
        application=application,
        project_id=41,
        include_optional=True,
    )

    assert application.position_calls == [41]
    decisions = cast("list[dict[str, object]]", payload["decisions"])
    assert "scope_extension.start" in {
        cast("str", item["node_id"]) for item in decisions
    }


def test_cli_adapter_has_no_repository_or_legacy_routing_imports() -> None:
    """Keep CLI adapters free of persistence and old routing dependencies."""
    source = (Path(__file__).parents[2] / "cli" / "workflow_commands.py").read_text()
    assert "repositories" not in source
    assert "services.workflow" not in source
