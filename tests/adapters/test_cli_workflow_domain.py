"""CLI adapter tests for the WorkflowDomain cutover."""

import importlib
import io
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from cli import main as cli_main
from cli.main import (
    _invest_assessment_lines,
    _story_item_lines,
    build_parser,
    main,
)
from cli.workflow_commands import workflow_next
from services.application import ExpectedPlanningReviewBinding
from tests.adapters.test_command_renderer import position_fixture
from workflow.contracts import TransitionResult, WorkflowPosition

if TYPE_CHECKING:
    from services.application import (
        PostSprintTriageRequest,
        SpecificationReviewRequest,
        SpecificationSourceRegistrationRequest,
        SpecificationStructuringRequest,
    )

SPRINT_CAPACITY_POINTS = 8
ARGUMENT_ERROR_EXIT_CODE = 2
PROJECT_ID = 41
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
        "story decide --project-id 41 --decision accepted --rationale {value} "
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
            "agileforge specification source register --project-id 1"
            " --source-path specification.md"
            " --preparation-capability grill-with-docs"
            " --adr-path docs/adr/0001-record-format.md"
            " --adr-path docs/adr/0002-reconciliation.md"
            " --idempotency-key spec-myfinance-1 --actor acceptance-agent"
        ),
        (
            "agileforge specification structure --project-id 1"
            " --idempotency-key structure-myfinance-1 --actor acceptance-agent"
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
    ],
)
def test_semantic_lifecycle_commands_parse(command: str) -> None:
    """Accept every concrete Task 7 lifecycle command without hidden guards."""
    parsed = cli_main.build_parser().parse_args(shlex.split(command)[1:])

    assert not hasattr(parsed, "graph_version")
    assert not hasattr(parsed, "expected_fact_fingerprint")
    assert not hasattr(parsed, "changed_by")


class _SpecificationPreparationApplication:
    """Capture the two host-prepared Specification commands."""

    def __init__(self) -> None:
        self.registered: list[object] = []
        self.structured: list[object] = []

    def register_specification_source(self, request: object) -> object:
        self.registered.append(request)
        return cli_main.TransitionResult(ok=True)

    def structure_specification(self, request: object) -> object:
        self.structured.append(request)
        return cli_main.TransitionResult(ok=True)


def test_specification_source_register_cli_sends_only_human_paths_and_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep source bytes, Context state, lineage, and identity host-owned."""
    application = _SpecificationPreparationApplication()

    exit_code = cli_main.main(
        [
            "specification",
            "source",
            "register",
            "--project-id",
            "41",
            "--source-path",
            "specification.md",
            "--preparation-capability",
            "grill-with-docs",
            "--adr-path",
            "docs/adr/0002.md",
            "--adr-path",
            "docs/adr/0001.md",
            "--idempotency-key",
            "source-cli-41",
            "--actor",
            "operator",
            "--correlation-id",
            "correlation-41",
        ],
        application=application,
    )

    assert exit_code == 0
    assert len(application.registered) == 1
    request = cast("SpecificationSourceRegistrationRequest", application.registered[0])
    assert request.project_id == PROJECT_ID
    assert request.source_path == "specification.md"
    assert request.adr_paths == ("docs/adr/0001.md", "docs/adr/0002.md")
    assert request.preparation_capability == "grill-with-docs"
    assert request.idempotency_key == "source-cli-41"
    assert request.actor == "operator"
    assert request.correlation_id == "correlation-41"
    for hidden in (
        "context_state",
        "source_fingerprint",
        "repository_binding_id",
        "accepted_vision_artifact_id",
        "accepted_product_goal_artifact_id",
        "bundle",
    ):
        assert not hasattr(request, hidden)
    assert '"ok": true' in capsys.readouterr().out


def test_specification_structure_cli_sends_only_transport_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Let the host select the registered source and all producer evidence."""
    application = _SpecificationPreparationApplication()

    exit_code = cli_main.main(
        [
            "specification",
            "structure",
            "--project-id",
            "41",
            "--idempotency-key",
            "structure-cli-41",
            "--actor",
            "operator",
            "--correlation-id",
            "correlation-41",
        ],
        application=application,
    )

    assert exit_code == 0
    assert len(application.structured) == 1
    request = cast("SpecificationStructuringRequest", application.structured[0])
    assert request.model_dump(mode="json") == {
        "project_id": PROJECT_ID,
        "idempotency_key": "structure-cli-41",
        "actor": "operator",
        "correlation_id": "correlation-41",
    }
    assert '"ok": true' in capsys.readouterr().out


def test_retired_specification_author_command_is_not_parseable() -> None:
    """Make the Specification preparation split a CLI hard break."""
    with pytest.raises(ValueError, match="invalid choice"):
        cli_main.build_parser().parse_args(
            [
                "specification",
                "author",
                "--project-id",
                "41",
                "--idempotency-key",
                "retired-author-41",
                "--actor",
                "operator",
            ]
        )


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--context-present", "true"),
        ("--source-fingerprint", "sha256:caller-owned"),
        ("--source-json", "source.json"),
        ("--bundle-json", "bundle.json"),
        ("--input-file", "source.json"),
        ("--repository-binding-id", "7"),
        ("--specification-source-id", "9"),
        ("--accepted-vision-artifact-id", "11"),
        ("--accepted-product-goal-artifact-id", "13"),
    ],
)
def test_specification_source_register_rejects_host_owned_flags(
    flag: str,
    value: str,
) -> None:
    """Keep source capture state and derived identities out of CLI syntax."""
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(
            [
                "specification",
                "source",
                "register",
                "--project-id",
                "41",
                "--source-path",
                "specification.md",
                "--preparation-capability",
                "grill-with-docs",
                "--idempotency-key",
                "source-41",
                "--actor",
                "operator",
                flag,
                value,
            ]
        )


@pytest.mark.parametrize(
    "capability_args",
    [(), ("--preparation-capability", "caller-owned")],
)
def test_specification_source_register_requires_exact_preparation_attestation(
    capability_args: tuple[str, ...],
) -> None:
    """The CLI never invents an external preparation attestation."""
    with pytest.raises(ValueError, match="preparation-capability"):
        cli_main.build_parser().parse_args(
            [
                "specification",
                "source",
                "register",
                "--project-id",
                "41",
                "--source-path",
                "specification.md",
                "--idempotency-key",
                "source-41",
                "--actor",
                "operator",
                *capability_args,
            ]
        )


class _SemanticTextApplication:
    """Capture any semantic text mutation without durable side effects."""

    _METHODS = frozenset(
        {
            "begin_vision_revision",
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
        self.reads = _SpecificationReviewReads()

    def backlog_review(self, _project_id: int) -> dict[str, object]:
        """Return one current Backlog review for planning-decision text tests."""
        return _unique_review()

    def roadmap_review(self, _project_id: int) -> dict[str, object]:
        """Return one current Roadmap review for planning-decision text tests."""
        response = _unique_review()
        data = cast("dict[str, object]", response["data"])
        data["review"] = _planning_review("roadmap")
        return response

    def story_reviews(self, _project_id: int) -> dict[str, object]:
        """Return one current Story review for planning-decision text tests."""
        response = _unique_review()
        data = cast("dict[str, object]", response["data"])
        return {
            "ok": True,
            "data": {
                "items": [
                    {
                        "binding": data["binding"],
                        "review": _planning_review("story"),
                    }
                ]
            },
            "warnings": [],
            "errors": [],
        }

    def sprint_plan_review(self, _project_id: int) -> dict[str, object]:
        """Return one current Sprint review for planning-decision text tests."""
        response = _unique_review()
        data = cast("dict[str, object]", response["data"])
        data["review"] = _planning_review("sprint_plan")
        return response

    def __getattr__(self, name: str) -> object:
        if name not in self._METHODS:
            raise AttributeError(name)

        def capture(request: object, **_keywords: object) -> object:
            self.requests.append(request)
            return cli_main.TransitionResult(ok=True)

        return capture


class _SpecificationReviewReads:
    """Return the exact packet identity captured by the CLI transport."""

    def specification_review(self, *, project_id: int) -> dict[str, object]:
        assert project_id == PROJECT_ID
        return {
            "ok": True,
            "data": {
                "candidate": {
                    "candidate_fingerprint": "sha256:candidate-shown",
                }
            },
        }


@pytest.mark.parametrize(("command", "field"), _SEMANTIC_TEXT_COMMANDS)
def test_semantic_text_cli_strips_before_application_call(
    command: str,
    field: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist canonical human text from every Task 7 CLI mutation."""
    application = _SemanticTextApplication()
    if command.startswith("specification review"):
        monkeypatch.setattr(
            cli_main,
            "_confirm_specification_review",
            lambda _packet, *, decision: decision == "accepted",
        )
    if command.startswith(
        ("backlog decide", "roadmap decide", "story decide", "sprint decide")
    ):
        monkeypatch.setattr(
            cli_main,
            "_confirm_planning_review",
            lambda _review, *, decision: decision == "accepted",
        )

    exit_code = cli_main.main(
        shlex.split(command.format(value=shlex.quote("  Canonical text.  "))),
        application=application,
    )

    assert exit_code == 0
    assert getattr(application.requests[0], field) == "Canonical text."
    if field == "rationale" and command.startswith("specification review"):
        request = cast("SpecificationReviewRequest", application.requests[0])
        assert request.expected_candidate_fingerprint == "sha256:candidate-shown"
    assert '"ok": true' in capsys.readouterr().out


def test_specification_review_displays_packet_before_human_confirmation(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI decision cannot silently bind to a candidate the human never saw."""
    application = _SemanticTextApplication()
    monkeypatch.setattr("sys.stdin.readline", lambda: "no\n")

    exit_code = cli_main.main(
        shlex.split(
            "specification review --project-id 41 --decision accepted "
            "--rationale reviewed --idempotency-key review-41 --actor operator"
        ),
        application=application,
    )

    captured = capsys.readouterr()
    assert exit_code == ARGUMENT_ERROR_EXIT_CODE
    assert application.requests == []
    assert "Exact Specification review packet" in captured.err
    assert "sha256:candidate-shown" in captured.err
    assert "cancelled" in captured.out


@pytest.mark.parametrize(("command", "field"), _SEMANTIC_TEXT_COMMANDS)
def test_semantic_text_cli_rejects_whitespace_before_application_call(
    command: str,
    field: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject normalized-empty human text before invoking the application."""
    del field
    application = _SemanticTextApplication()
    invalid_input_exit_code = 2
    if command.startswith(
        ("backlog decide", "roadmap decide", "story decide", "sprint decide")
    ):
        monkeypatch.setattr(
            cli_main,
            "_confirm_planning_review",
            lambda _review, *, decision: decision == "accepted",
        )

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
        ("story", []),
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
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(
            [
                group,
                "decide",
                "--project-id",
                "41",
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


def test_story_review_uses_no_caller_owned_instance_selector() -> None:
    """The CLI body is semantic; Story identity is captured from review data."""
    parsed = cli_main.build_parser().parse_args(
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

    assert parsed.decision == "accepted"
    assert not hasattr(parsed, "instance_key")
    with pytest.raises(ValueError, match="unrecognized arguments"):
        cli_main.build_parser().parse_args(
            [
                "story",
                "decide",
                "--project-id",
                "41",
                "--instance-key",
                "story:caller-owned",
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
    assert not hasattr(parsed, "include_task_decomposition")
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
        "planning.backlog.review",
        "planning.roadmap.review",
        "planning.story.review",
        "planning.sprint.review",
    ]


def test_cli_adapter_has_no_repository_or_legacy_routing_imports() -> None:
    """Keep CLI adapters free of persistence and old routing dependencies."""
    source = (Path(__file__).parents[2] / "cli" / "workflow_commands.py").read_text()
    assert "repositories" not in source
    assert "services.workflow" not in source


# Retained Task 11 planning-review coverage.
def _unique_review() -> dict[str, object]:
    return {
        "ok": True,
        "data": {
            "binding": {
                "decision_fingerprint": "decision-secret",
                "instance_key": None,
            },
            "review": _planning_review("backlog"),
        },
        "warnings": [],
        "errors": [],
    }


def _evidence() -> list[dict[str, object]]:
    return [
        {
            "spec_item_id": "REQ.hidden",
            "title": "Reliable balances",
            "statement": "Reconcile balances before delivery.",
            "level": "MUST",
            "acceptance_criteria": ["All accounts reconcile."],
            "verification_method": "acceptance-test",
        }
    ]


def _backlog_item() -> dict[str, object]:
    return {
        "backlog_item_id": "PBI-000001",
        "priority": 1,
        "requirement": "Household balances",
        "value_driver": "Customer Satisfaction",
        "justification": "Users need trusted balances.",
        "estimated_effort": "M",
        "technical_note": None,
        "specification_evidence": _evidence(),
    }


def _planning_review(phase: str) -> dict[str, object]:
    common: dict[str, object] = {
        "schema_version": "agileforge.planning-artifact-review.v1",
        "phase": phase,
        "project_id": 41,
        "lineage": {"specification": {"spec_hash": "sha256:hidden"}},
        "review": {"state": "pending"},
    }
    if phase == "backlog":
        candidate = {
            "backlog_items": [_backlog_item()],
            "is_complete": True,
            "clarifying_questions": [],
        }
    elif phase == "roadmap":
        candidate = {
            "roadmap_summary": "Reconcile accounts first.",
            "roadmap_releases": [
                {
                    "release_name": "Trusted balances",
                    "theme": "Confidence",
                    "focus_area": "User Value",
                    "reasoning": "Deliver the core requirement first.",
                    "backlog_items": [_backlog_item()],
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
    elif phase == "story":
        candidate = {
            "story_items": [
                {
                    "story_title": "Reconcile balances",
                    "statement": "As a household, I want trusted balances.",
                    "persona": "household",
                    "acceptance_criteria": ["All accounts reconcile."],
                    "invest_assessment": {
                        "independent": {
                            "result": "pass",
                            "rationale": "Delivers self-contained increment.",
                            "evidence": "No unbuilt dependencies.",
                        },
                        "negotiable": {
                            "result": "pass",
                            "rationale": "Implementation details open to refinement.",
                            "evidence": "Focuses on user outcome.",
                        },
                        "valuable": {
                            "result": "pass",
                            "rationale": "Directly delivers user capability.",
                            "evidence": "Addresses requirement.",
                        },
                        "estimable": {
                            "result": "pass",
                            "rationale": "Scope is clear and bounded.",
                            "evidence": "Discrete criteria.",
                        },
                        "small": {
                            "result": "pass",
                            "rationale": "Sized for single iteration.",
                            "evidence": "Effort is M.",
                        },
                        "testable": {
                            "result": "pass",
                            "rationale": "Verifiable pass/fail criteria.",
                            "evidence": "Observable verification steps.",
                        },
                    },
                    "estimated_effort": "M",
                    "story_points": 3,
                    "rank": "101",
                    "order": 1,
                    "produced_artifacts": ["Balance report"],
                    "research_caveats": [],
                    "dependency_candidates": [],
                    "specification_evidence": _evidence(),
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
        common["lineage"] = {"backlog_item": _backlog_item()}
    else:
        candidate = {
            "team_name": "Balance team",
            "sprint_goal": "Ship trusted balances.",
            "selected_stories": [
                {
                    "title": "Reconcile balances",
                    "statement": "As a household, I want trusted balances.",
                    "persona": "household",
                    "acceptance_criteria": ["All accounts reconcile."],
                    "specification_evidence": _evidence(),
                    "reason_for_selection": "Highest customer value.",
                    "tasks": [
                        {
                            "description": "Implement reconciliation",
                            "task_kind": "implementation",
                            "artifact_targets": ["service"],
                            "workstream_tags": ["balances"],
                            "checklist_items": ["Run acceptance test"],
                            "specification_evidence": _evidence(),
                        }
                    ],
                }
            ],
        }
    common["candidate"] = candidate
    return common


@dataclass
class _Application:
    writes: list[tuple[object, ExpectedPlanningReviewBinding]] = field(
        default_factory=list
    )
    story_review_override: dict[str, object] | None = None

    def backlog_review(self, _project_id: int) -> dict[str, object]:
        return _unique_review()

    def roadmap_review(self, _project_id: int) -> dict[str, object]:
        result = _unique_review()
        data = result["data"]
        assert isinstance(data, dict)
        cast("dict[str, object]", data)["review"] = _planning_review("roadmap")
        return result

    def sprint_plan_review(self, _project_id: int) -> dict[str, object]:
        result = _unique_review()
        data = result["data"]
        assert isinstance(data, dict)
        cast("dict[str, object]", data)["review"] = _planning_review("sprint_plan")
        return result

    def story_reviews(self, _project_id: int) -> dict[str, object]:
        return {
            "ok": True,
            "data": {
                "items": [
                    {
                        "binding": {
                            "decision_fingerprint": "story-decision-secret",
                            "instance_key": "story-instance-secret",
                        },
                        "review": (
                            self.story_review_override
                            if self.story_review_override is not None
                            else _planning_review("story")
                        ),
                    }
                ]
            },
            "warnings": [],
            "errors": [],
        }

    def decide_backlog(
        self, request: object, *, expected: ExpectedPlanningReviewBinding
    ) -> TransitionResult:
        self.writes.append((request, expected))
        return TransitionResult(ok=True, applied_node_id="planning.backlog.review")

    def decide_story(
        self, request: object, *, expected: ExpectedPlanningReviewBinding
    ) -> TransitionResult:
        self.writes.append((request, expected))
        return TransitionResult(ok=True, applied_node_id="planning.story.review")


def _decision_argv(phase: str) -> list[str]:
    return [
        phase,
        "decide",
        "--project-id",
        "41",
        "--decision",
        "accepted",
        "--rationale",
        "Reviewed exact evidence.",
        "--idempotency-key",
        f"{phase}-review-41",
        "--actor",
        "operator",
    ]


def test_planning_review_read_commands_are_exact() -> None:
    """Expose each retained planning-review read command with its project ID."""
    parser = build_parser()

    for argv in (
        ["backlog", "review", "--project-id", "41"],
        ["roadmap", "review", "--project-id", "41"],
        ["story", "reviews", "--project-id", "41"],
        ["sprint", "plan-review", "--project-id", "41"],
    ):
        assert parser.parse_args(argv).project_id == PROJECT_ID


def test_planning_review_read_hides_machine_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Render review reads as human evidence without machine-only bindings."""
    application = _Application()

    assert (
        main(
            ["backlog", "review", "--project-id", "41"],
            application=application,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Household balances" in output
    assert "Specification evidence" in output
    assert "Level: MUST" in output
    assert "Verification: acceptance-test" in output
    assert "schema_version" not in output
    assert "lineage" not in output
    assert "{" not in output
    assert "decision-secret" not in output
    assert "sha256:hidden" not in output


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["backlog", "review", "--project-id", "41"],
            "Requirement: Household balances",
        ),
        (["roadmap", "review", "--project-id", "41"], "Release: Trusted balances"),
        (["story", "reviews", "--project-id", "41"], "Story: Reconcile balances"),
        (
            ["sprint", "plan-review", "--project-id", "41"],
            "Sprint goal: Ship trusted balances.",
        ),
    ],
)
def test_planning_review_reads_use_phase_specific_human_labels(
    argv: list[str],
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep each retained review read labelled for its human planning phase."""
    application = _Application()

    assert main(argv, application=application) == 0

    output = capsys.readouterr().out
    assert expected in output
    assert "Specification evidence" in output
    assert "schema_version" not in output
    assert "artifact_fingerprint" not in output
    assert "{" not in output


def test_backlog_decision_displays_evidence_and_keeps_binding_hidden(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show backlog evidence while retaining the decision binding internally."""
    application = _Application()
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))

    assert main(_decision_argv("backlog"), application=application) == 0

    captured = capsys.readouterr()
    assert "Household balances" in captured.err
    assert "Reconcile balances before delivery." in captured.err
    assert "Specification evidence" in captured.err
    assert "schema_version" not in captured.err
    assert "{" not in captured.err
    assert "decision-secret" not in captured.err
    assert "sha256:hidden" not in captured.err
    assert len(application.writes) == 1
    assert application.writes[0][1].decision_fingerprint == "decision-secret"
    assert application.writes[0][1].instance_key is None


def test_cancelled_planning_review_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Abort a declined planning-review decision before it writes state."""
    application = _Application()
    monkeypatch.setattr("sys.stdin", io.StringIO("no\n"))

    assert (
        main(_decision_argv("backlog"), application=application)
        == ARGUMENT_ERROR_EXIT_CODE
    )
    assert application.writes == []


def test_story_decision_uses_hidden_instance_without_cli_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolve the story instance internally rather than exposing a CLI flag."""
    application = _Application()
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))

    assert main(_decision_argv("story"), application=application) == 0

    captured = capsys.readouterr()
    assert "Reconcile balances" in captured.err
    assert "story-instance-secret" not in captured.err
    assert application.writes[0][1].instance_key == "story-instance-secret"
    assert "instance-key" not in build_parser().format_help()


def test_cli_source_has_no_retired_operator_surface() -> None:
    """Keep removed operator terminology out of the live CLI source."""
    source = Path("cli/main.py").read_text(encoding="utf-8").casefold()
    assert "auth" + "ority" not in source
    assert "invar" + "iant" not in source


def test_invest_assessment_lines_formats_valid_assessment() -> None:
    """Format all six INVEST dimensions with uppercase results and evidence."""
    assessment = {
        "independent": {
            "result": "pass",
            "rationale": "Self-contained logic.",
            "evidence": "No unbuilt dependencies.",
        },
        "negotiable": {
            "result": "pass",
            "rationale": "Refinable approach.",
            "evidence": "Focuses on user outcome.",
        },
        "valuable": {
            "result": "pass",
            "rationale": "Direct benefit.",
            "evidence": "Traces to REQ.001.",
        },
        "estimable": {
            "result": "pass",
            "rationale": "Clear boundaries.",
            "evidence": "Discrete criteria.",
        },
        "small": {
            "result": "concern",
            "rationale": "Near upper size bound.",
            "evidence": "Effort is M.",
        },
        "testable": {
            "result": "pass",
            "rationale": "Deterministic pass/fail.",
            "evidence": "Explicit verification criteria.",
        },
    }
    lines = _invest_assessment_lines(assessment, indent="  ")
    assert lines[0] == "  INVEST assessment:"
    assert (
        "    - Independent [PASS]: Self-contained logic. "
        "(Evidence: No unbuilt dependencies.)"
    ) in lines
    assert (
        "    - Small [CONCERN]: Near upper size bound. "
        "(Evidence: Effort is M.)"
    ) in lines
    assert (
        "    - Testable [PASS]: Deterministic pass/fail. "
        "(Evidence: Explicit verification criteria.)"
    ) in lines


def test_invest_assessment_lines_handles_missing_and_malformed_assessments() -> None:
    """Emit explicit diagnostics for missing assessment or incomplete dimensions."""
    # Non-dict assessment
    missing = _invest_assessment_lines(None, indent="  ")
    assert len(missing) == 1
    assert "INVEST assessment: [MALFORMED / MISSING]" in missing[0]

    # Incomplete assessment with missing dimension and invalid result
    malformed = {
        "independent": {
            "result": "pass",
            "rationale": "Valid.",
            "evidence": "Valid.",
        },
        "negotiable": {
            "result": "invalid_result",
            "rationale": "Valid.",
            "evidence": "Valid.",
        },
        "valuable": {
            "result": "pass",
            "rationale": "",
            "evidence": "Valid.",
        },
    }
    lines = _invest_assessment_lines(malformed, indent="  ")
    assert "INVEST assessment:" in lines[0]
    assert any("Independent [PASS]" in line for line in lines)
    assert any("Negotiable [INVALID]" in line for line in lines)
    assert any("Valuable [INVALID]" in line for line in lines)
    assert any("Estimable [MISSING]" in line for line in lines)
    assert any("Small [MISSING]" in line for line in lines)
    assert any("Testable [MISSING]" in line for line in lines)

    # Malformed types regression: integer rationale, dict evidence
    type_malformed = {
        "independent": {
            "result": "pass",
            "rationale": 123,
            "evidence": {"source": "REQ.1"},
        },
        "negotiable": {
            "result": "PASS",
            "rationale": "Valid.",
            "evidence": "Valid.",
        },
        "valuable": {
            "result": "pass",
            "rationale": "Valid.",
            "evidence": "Valid.",
            "extra_key": True,
        },
    }
    type_lines = _invest_assessment_lines(type_malformed, indent="  ")
    assert any("Independent [INVALID]" in line for line in type_lines)
    assert any("Negotiable [INVALID]" in line for line in type_lines)
    assert any("Valuable [INVALID]" in line for line in type_lines)


def test_story_item_lines_formats_missing_invest_assessment() -> None:
    """Always include INVEST diagnostic line when assessment is missing."""
    story_without_invest: dict[str, object] = {
        "story_title": "Title",
        "statement": "As a user, I want something.",
        "persona": "user",
        "acceptance_criteria": ["Done."],
        "specification_evidence": [],
    }
    lines = _story_item_lines(story_without_invest, indent="  ")
    assert any(
        "INVEST assessment: [MALFORMED / MISSING]" in line for line in lines
    )


def test_story_decide_rejects_acceptance_when_invest_missing_or_malformed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Story decide with accepted decision fails closed on invalid INVEST assessment."""
    app = _Application()
    review = _planning_review("story")
    candidate = cast("dict[str, object]", review["candidate"])
    story_items = cast("list[dict[str, object]]", candidate["story_items"])
    story_items[0]["invest_assessment"] = None
    app.story_review_override = review

    exit_code = main(_decision_argv("story"), application=app)
    assert exit_code == ARGUMENT_ERROR_EXIT_CODE

    captured = capsys.readouterr()
    assert (
        "Story proposal cannot be accepted: required INVEST quality assessment "
        "is missing or malformed." in captured.out
    )
    assert len(app.writes) == 0


def test_story_decide_allows_feedback_and_rejection_for_malformed_invest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feedback and rejection decisions remain available even with malformed INVEST."""
    app = _Application()
    review = _planning_review("story")
    candidate = cast("dict[str, object]", review["candidate"])
    story_items = cast("list[dict[str, object]]", candidate["story_items"])
    story_items[0]["invest_assessment"] = None
    app.story_review_override = review
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\nyes\n"))

    feedback_argv = [
        "story",
        "decide",
        "--project-id",
        "41",
        "--decision",
        "feedback",
        "--rationale",
        "Please provide valid INVEST assessment.",
        "--idempotency-key",
        "story-feedback-41",
        "--actor",
        "operator",
    ]
    assert main(feedback_argv, application=app) == 0
    assert len(app.writes) == 1

    reject_argv = [
        "story",
        "decide",
        "--project-id",
        "41",
        "--decision",
        "rejected",
        "--rationale",
        "Rejected incomplete proposal.",
        "--idempotency-key",
        "story-reject-41",
        "--actor",
        "operator",
    ]
    assert main(reject_argv, application=app) == 0
    decisions = [getattr(w[0], "decision", None) for w in app.writes]
    assert decisions == ["feedback", "rejected"]


def test_story_item_lines_formats_sizing_rank_and_dependencies() -> None:
    """Format effort with derived points, rank/order, and dependency state."""
    item_with_planning: dict[str, object] = {
        "story_title": "Title",
        "statement": "As a user, I want something.",
        "persona": "user",
        "order": 1,
        "rank": "101",
        "estimated_effort": "M",
        "story_points": 3,
        "acceptance_criteria": ["Done."],
        "specification_evidence": [],
        "invest_assessment": {
            "independent": {"result": "pass", "rationale": "r", "evidence": "e"},
            "negotiable": {"result": "pass", "rationale": "r", "evidence": "e"},
            "valuable": {"result": "pass", "rationale": "r", "evidence": "e"},
            "estimable": {"result": "pass", "rationale": "r", "evidence": "e"},
            "small": {"result": "pass", "rationale": "r", "evidence": "e"},
            "testable": {"result": "pass", "rationale": "r", "evidence": "e"},
        },
        "dependency_candidates": [],
    }
    lines = _story_item_lines(item_with_planning, indent="  ")
    assert "  Order: 1 | Rank: 101" in lines
    assert "  Estimated effort: M (derived: 3 story points)" in lines
    assert "  Proposed dependencies: None" in lines

    # Non-empty dependencies
    item_with_deps = dict(item_with_planning)
    item_with_deps["dependency_candidates"] = [
        {
            "prerequisite_ref": "US-001",
            "confidence": "explicit",
            "reason": "Requires US-001",
        }
    ]
    dep_lines = _story_item_lines(item_with_deps, indent="  ")
    assert "  Proposed dependencies:" in dep_lines
    assert "    - Prerequisite: US-001 (explicit) - Requires US-001" in dep_lines
