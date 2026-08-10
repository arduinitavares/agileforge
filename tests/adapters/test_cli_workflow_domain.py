"""CLI adapter tests for the WorkflowDomain cutover."""

import importlib
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from cli import main as cli_main
from cli.workflow_commands import workflow_next
from tests.adapters.test_command_renderer import position_fixture
from workflow.contracts import WorkflowPosition

if TYPE_CHECKING:
    from services.application import AuthorityFeedbackRequest, PostSprintTriageRequest

SPRINT_CAPACITY_POINTS = 8
ARGUMENT_ERROR_EXIT_CODE = 2
_SEMANTIC_TEXT_COMMANDS = (
    (
        "vision respond --project-id 41 --text {value} "
        "--idempotency-key vision-text-41 --actor operator",
        "text",
    ),
    (
        "vision review --project-id 41 --decision accepted --rationale {value} "
        "--idempotency-key vision-review-41 --actor operator",
        "rationale",
    ),
    (
        "vision revision --project-id 41 --reason {value} "
        "--idempotency-key vision-revision-41 --actor operator",
        "reason",
    ),
    (
        "goal respond --project-id 41 --text {value} "
        "--idempotency-key goal-text-41 --actor operator",
        "text",
    ),
    (
        "goal review --project-id 41 --decision accepted --rationale {value} "
        "--idempotency-key goal-review-41 --actor operator",
        "rationale",
    ),
    (
        "goal complete --project-id 41 --rationale {value} "
        "--idempotency-key goal-complete-41 --actor operator",
        "rationale",
    ),
    (
        "specification review --project-id 41 --decision accepted "
        "--rationale {value} --idempotency-key specification-review-41 "
        "--actor operator",
        "rationale",
    ),
    (
        "authority decide --project-id 41 --decision accepted --rationale {value} "
        "--idempotency-key authority-review-41 --actor operator",
        "rationale",
    ),
    (
        "backlog decide --project-id 41 --decision accepted --rationale {value} "
        "--idempotency-key backlog-review-41 --actor operator",
        "rationale",
    ),
    (
        "roadmap decide --project-id 41 --decision accepted --rationale {value} "
        "--idempotency-key roadmap-review-41 --actor operator",
        "rationale",
    ),
    (
        "story decide --project-id 41 --instance-key requirement:req-7 "
        "--decision accepted --rationale {value} "
        "--idempotency-key story-review-41 --actor operator",
        "rationale",
    ),
    (
        "sprint decide --project-id 41 --decision accepted --rationale {value} "
        "--idempotency-key sprint-review-41 --actor operator",
        "rationale",
    ),
)


@pytest.mark.parametrize(
    "command",
    [
        (
            "agileforge vision bootstrap --project-id 1"
            " --idempotency-key vision-bootstrap-myfinance-1"
            " --actor acceptance-agent"
        ),
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


class _SemanticTextApplication:
    """Capture any semantic text mutation without durable side effects."""

    _METHODS = frozenset(
        {
            "begin_vision_revision",
            "decide_authority",
            "respond_to_product_goal",
            "respond_to_vision",
            "resolve_product_goal",
            "review_product_goal",
            "review_specification",
            "review_vision",
            "decide_backlog",
            "decide_roadmap",
            "decide_sprint_plan",
            "decide_story",
        }
    )

    def __init__(self) -> None:
        self.requests: list[object] = []

    def __getattr__(self, name: str) -> object:
        if name not in self._METHODS:
            raise AttributeError(name)

        def capture(request: object) -> object:
            self.requests.append(request)
            return cli_main.TransitionResult(ok=True)

        return capture


@pytest.mark.parametrize(("command", "field"), _SEMANTIC_TEXT_COMMANDS)
def test_semantic_text_cli_strips_before_application_call(
    command: str,
    field: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Persist canonical human text from every Task 7 CLI mutation."""
    application = _SemanticTextApplication()

    exit_code = cli_main.main(
        shlex.split(command.format(value=shlex.quote("  Canonical text.  "))),
        application=application,
    )

    assert exit_code == 0
    assert getattr(application.requests[0], field) == "Canonical text."
    assert '"ok": true' in capsys.readouterr().out


@pytest.mark.parametrize(("command", "field"), _SEMANTIC_TEXT_COMMANDS)
def test_semantic_text_cli_rejects_whitespace_before_application_call(
    command: str,
    field: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject normalized-empty human text before invoking the application."""
    del field
    application = _SemanticTextApplication()
    invalid_input_exit_code = 2

    exit_code = cli_main.main(
        shlex.split(command.format(value=shlex.quote("  \t"))),
        application=application,
    )

    assert exit_code == invalid_input_exit_code
    assert application.requests == []
    assert '"ok": false' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("group", "extra"),
    [
        ("backlog", []),
        ("roadmap", []),
        ("story", ["--instance-key", "requirement:req-7"]),
    ],
)
def test_retained_agentic_commands_parse_without_model_owned_input(
    group: str,
    extra: list[str],
) -> None:
    """Accept host-prepared delivery commands with transport metadata only."""
    parsed = cli_main.build_parser().parse_args(
        [
            group,
            "generate",
            "--project-id",
            "41",
            *extra,
            "--idempotency-key",
            f"{group}-41",
            "--actor",
            "operator",
        ]
    )

    assert not hasattr(parsed, "input_file")
    assert not hasattr(parsed, "model_id")


def test_story_generation_requires_exact_instance_selector() -> None:
    """Refuse to select one repeated Story generation decision implicitly."""
    with pytest.raises(ValueError, match="--instance-key"):
        cli_main.build_parser().parse_args(
            [
                "story",
                "generate",
                "--project-id",
                "41",
                "--idempotency-key",
                "story-41",
                "--actor",
                "operator",
            ]
        )


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
                "--instance-key",
                "sprint:31",
                "--idempotency-key",
                "backlog-41",
                "--actor",
                "operator",
                flag,
                "caller-owned",
            ]
        )


@pytest.mark.parametrize(
    ("group", "extra"),
    [
        ("backlog", []),
        ("roadmap", []),
        ("story", ["--instance-key", "requirement:req-7"]),
        ("sprint", []),
    ],
)
def test_delivery_review_commands_use_semantic_flags_without_request_file(
    group: str,
    extra: list[str],
) -> None:
    """Parse four task-specific reviews without artifact request files."""
    parsed = cli_main.build_parser().parse_args(
        [
            group,
            "decide",
            "--project-id",
            "41",
            *extra,
            "--decision",
            "accepted",
            "--rationale",
            "Reviewed current artifact.",
            "--idempotency-key",
            f"{group}-review-41",
            "--actor",
            "operator",
        ]
    )

    assert parsed.decision == "accepted"
    assert parsed.rationale == "Reviewed current artifact."
    assert not hasattr(parsed, "request_file")


@pytest.mark.parametrize("group", ["backlog", "roadmap", "story", "sprint"])
def test_delivery_review_commands_reject_request_file(group: str) -> None:
    """Remove the generic request-file contract from all delivery reviews."""
    extra = ["--instance-key", "requirement:req-7"] if group == "story" else []
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(
            [
                group,
                "decide",
                "--project-id",
                "41",
                *extra,
                "--decision",
                "accepted",
                "--rationale",
                "Reviewed current artifact.",
                "--idempotency-key",
                f"{group}-review-41",
                "--actor",
                "operator",
                "--request-file",
                "review.json",
            ]
        )


def test_story_review_requires_exact_instance_selector() -> None:
    """Refuse to choose between repeated Story review decisions implicitly."""
    with pytest.raises(ValueError, match="--instance-key"):
        cli_main.build_parser().parse_args(
            [
                "story",
                "decide",
                "--project-id",
                "41",
                "--decision",
                "accepted",
                "--rationale",
                "Reviewed current artifact.",
                "--idempotency-key",
                "story-review-41",
                "--actor",
                "operator",
            ]
        )


@pytest.mark.parametrize(
    ("command", "request_type_name"),
    [
        (
            "story dependencies apply --project-id 41 "
            "--story-id 7 --story-id 9 "
            "--dependency '9:7:Story 9 requires Story 7.' "
            "--idempotency-key dependencies-41 --actor operator",
            "StoryDependenciesApplyRequest",
        ),
        (
            "story readiness repair --project-id 41 "
            "--repair 7:3:101 --repair 9:5:102 "
            "--idempotency-key readiness-41 --actor operator",
            "StoryReadinessRepairRequest",
        ),
        (
            "sprint start --project-id 41 --idempotency-key start-41 --actor operator",
            "SprintStartRequest",
        ),
    ],
)
def test_planning_action_commands_use_task_specific_semantics(
    command: str,
    request_type_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parse and dispatch the four planning actions without request files."""

    class PlanningActionApplication:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def __getattr__(self, name: str) -> object:
            if name not in {
                "apply_story_dependencies",
                "repair_story_readiness",
                "start_sprint",
            }:
                raise AttributeError(name)

            def capture(request: object) -> object:
                self.requests.append(request)
                return cli_main.TransitionResult(ok=True)

            return capture

    application = PlanningActionApplication()

    exit_code = cli_main.main(shlex.split(command), application=application)

    assert exit_code == 0
    assert len(application.requests) == 1
    request = application.requests[0]
    assert type(request).__name__ == request_type_name
    assert not hasattr(request, "graph_version")
    assert not hasattr(request, "fact_fingerprint")
    assert not hasattr(request, "request_file")
    assert '"ok": true' in capsys.readouterr().out


@pytest.mark.parametrize(
    "repairs",
    [
        ["0:3:101"],
        ["7:0:101"],
        ["7:3:"],
        ["7:3:101", "7:5:102"],
        ["7:3:0"],
        ["7:3:-1"],
        ["7:3:1.1"],
        ["7:3:high"],
        ["7:3:01"],
        ["7:3:+1"],
    ],
)
def test_story_readiness_cli_rejects_invalid_repairs(
    repairs: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject invalid IDs, points, blank ranks, and duplicate Story repairs."""

    class UncalledApplication:
        def repair_story_readiness(self, request: object) -> object:
            pytest.fail(f"invalid repair reached application: {request}")

    arguments = [
        "story",
        "readiness",
        "repair",
        "--project-id",
        "41",
    ]
    for repair in repairs:
        arguments.extend(("--repair", repair))
    arguments.extend(
        (
            "--idempotency-key",
            "readiness-41",
            "--actor",
            "operator",
        )
    )

    argparse_error_exit_code = 2
    assert (
        cli_main.main(arguments, application=UncalledApplication())
        == argparse_error_exit_code
    )
    assert '"ok": false' in capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        "story dependencies apply --project-id 41 --story-id 7 "
        "--request-file request.json --idempotency-key dependencies-41 "
        "--actor operator",
        "story readiness repair --project-id 41 --repair 7:3:101 "
        "--request-file request.json --idempotency-key readiness-41 "
        "--actor operator",
        "sprint start --project-id 41 --request-file request.json "
        "--idempotency-key start-41 --actor operator",
    ],
)
def test_planning_action_commands_reject_request_file(command: str) -> None:
    """Remove generic JSON request files from planning actions."""
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(shlex.split(command))


class _ExecutionActionApplication:
    """Capture any strict execution or triage semantic request."""

    _METHODS = frozenset(
        {
            "close_sprint",
            "close_story",
            "complete_task",
            "record_post_sprint_triage",
            "review_sprint",
        }
    )

    def __init__(self) -> None:
        self.requests: list[object] = []

    def __getattr__(self, name: str) -> object:
        if name not in self._METHODS:
            raise AttributeError(name)

        def capture(request: object) -> object:
            self.requests.append(request)
            return cli_main.TransitionResult(ok=True)

        return capture


@pytest.mark.parametrize(
    ("arguments", "request_type_name"),
    [
        (
            [
                "sprint",
                "task",
                "complete",
                "--project-id",
                "41",
                "--instance-key",
                "task:7",
                "--outcome-summary",
                "Implemented semantic execution.",
                "--artifact-ref",
                "services/application.py",
                "--acceptance-result",
                "fully_met",
                "--checklist-item",
                "Focused tests=passed",
                "--idempotency-key",
                "complete-task-41",
                "--actor",
                "operator",
            ],
            "CompleteTaskRequest",
        ),
        (
            [
                "story",
                "close",
                "--project-id",
                "41",
                "--instance-key",
                "story:9",
                "--resolution",
                "Completed",
                "--delivered",
                "Semantic execution transport.",
                "--evidence",
                "Focused tests pass.",
                "--known-gaps",
                "None.",
                "--idempotency-key",
                "close-story-41",
                "--actor",
                "operator",
            ],
            "CloseStoryRequest",
        ),
        (
            [
                "sprint",
                "review",
                "--project-id",
                "41",
                "--instance-key",
                "sprint:31",
                "--idempotency-key",
                "review-sprint-41",
                "--actor",
                "operator",
            ],
            "SprintReviewRequest",
        ),
        (
            [
                "sprint",
                "close",
                "--project-id",
                "41",
                "--instance-key",
                "sprint:31",
                "--idempotency-key",
                "close-sprint-41",
                "--actor",
                "operator",
            ],
            "SprintCloseRequest",
        ),
    ],
)
def test_execution_action_commands_use_task_specific_semantics(
    arguments: list[str],
    request_type_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parse and dispatch strict execution commands without request files."""
    application = _ExecutionActionApplication()

    exit_code = cli_main.main(arguments, application=application)

    assert exit_code == 0
    assert len(application.requests) == 1
    request = application.requests[0]
    assert type(request).__name__ == request_type_name
    assert not hasattr(request, "sprint_id")
    assert not hasattr(request, "request_file")
    assert '"ok": true' in capsys.readouterr().out


def test_post_sprint_triage_cli_reads_only_semantic_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read the triage artifact as canonical_payload, not a workflow request."""
    payload_path = tmp_path / "triage.json"
    payload_path.write_text('{"summary":"Follow-up required."}', encoding="utf-8")
    application = _ExecutionActionApplication()

    exit_code = cli_main.main(
        [
            "sprint",
            "triage",
            "--project-id",
            "41",
            "--instance-key",
            "sprint:31",
            "--impact",
            "backlog",
            "--file",
            str(payload_path),
            "--idempotency-key",
            "triage-41",
            "--actor",
            "operator",
        ],
        application=application,
    )

    assert exit_code == 0
    request = cast("PostSprintTriageRequest", application.requests[0])
    assert type(request).__name__ == "PostSprintTriageRequest"
    assert request.canonical_payload == {"summary": "Follow-up required."}
    assert not hasattr(request, "sprint_id")
    assert '"ok": true' in capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        "sprint task complete --project-id 41 --instance-key task:7 "
        "--outcome-summary done --artifact-ref result --acceptance-result fully_met "
        "--checklist-item check=passed --request-file request.json "
        "--idempotency-key complete-41 --actor operator",
        "story close --project-id 41 --instance-key story:9 "
        "--resolution done --delivered delivered --evidence tested "
        "--known-gaps none --request-file request.json "
        "--idempotency-key story-41 --actor operator",
        "sprint review --project-id 41 --instance-key sprint:31 "
        "--request-file request.json "
        "--idempotency-key review-41 --actor operator",
        "sprint close --project-id 41 --instance-key sprint:31 "
        "--request-file request.json "
        "--idempotency-key close-41 --actor operator",
        "sprint triage --project-id 41 --instance-key sprint:31 --impact none "
        "--file triage.json --request-file request.json "
        "--idempotency-key triage-41 --actor operator",
    ],
)
def test_execution_action_commands_reject_request_file(command: str) -> None:
    """Remove generic workflow request files from all execution commands."""
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(shlex.split(command))


@pytest.mark.parametrize(
    "command",
    [
        "sprint task complete --project-id 41 --outcome-summary done "
        "--artifact-ref result --acceptance-result fully_met "
        "--checklist-item check=passed --idempotency-key complete-41 "
        "--actor operator",
        "story close --project-id 41 --resolution Completed --delivered done "
        "--evidence tested --known-gaps none --idempotency-key story-41 "
        "--actor operator",
        "sprint review --project-id 41 --idempotency-key review-41 --actor operator",
        "sprint close --project-id 41 --idempotency-key close-41 --actor operator",
        "sprint triage --project-id 41 --impact none --file triage.json "
        "--idempotency-key triage-41 --actor operator",
    ],
)
def test_execution_action_commands_require_exact_instance_key(command: str) -> None:
    """Require an explicit non-null selector for every execution action."""
    with pytest.raises(ValueError, match="--instance-key"):
        cli_main.build_parser().parse_args(shlex.split(command))


@pytest.mark.parametrize(
    "checklist_items",
    [
        ["missing-separator"],
        [" =passed"],
        ["check= "],
        ["check=passed", "check=failed"],
    ],
)
def test_complete_task_cli_rejects_invalid_checklist_map(
    checklist_items: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject malformed, blank, or duplicate KEY=VALUE checklist entries."""
    application = _ExecutionActionApplication()
    arguments = [
        "sprint",
        "task",
        "complete",
        "--project-id",
        "41",
        "--instance-key",
        "task:7",
        "--outcome-summary",
        "Done.",
        "--artifact-ref",
        "result",
        "--acceptance-result",
        "fully_met",
    ]
    for item in checklist_items:
        arguments.extend(("--checklist-item", item))
    arguments.extend(
        (
            "--idempotency-key",
            "complete-41",
            "--actor",
            "operator",
        )
    )

    assert cli_main.main(arguments, application=application) == ARGUMENT_ERROR_EXIT_CODE
    assert application.requests == []
    assert '"ok": false' in capsys.readouterr().out


def test_generic_cli_transition_transport_is_removed() -> None:
    """Delete generic request-file installation and guarded dispatch helpers."""
    source = (Path(__file__).parents[2] / "cli" / "main.py").read_text()

    assert not hasattr(cli_main, "_run_transition")
    assert not hasattr(cli_main, "_guarded_payload")
    assert "_install_transition_commands" not in source
    assert "_add_transition_leaf" not in source


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


def test_cli_adapter_has_no_repository_or_legacy_routing_imports() -> None:
    """Keep CLI adapters free of persistence and old routing dependencies."""
    source = (Path(__file__).parents[2] / "cli" / "workflow_commands.py").read_text()
    assert "repositories" not in source
    assert "services.workflow" not in source
