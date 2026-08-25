"""AgileForge CLI backed exclusively by the durable workflow graph."""

# ruff: noqa: EM101, TRY003, TRY004

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NoReturn, Protocol, cast

from pydantic import TypeAdapter, ValidationError

from cli.workflow_commands import (
    workflow_next,
    workflow_position,
)
from services.agent_workbench.version import agileforge_version
from services.application import (
    BacklogReviewRequest,
    CloseStoryRequest,
    CompleteTaskRequest,
    CreateProjectCommand,
    DeliveryActionRequest,
    ExpectedPlanningReviewBinding,
    PostSprintTriageRequest,
    ProductGoalOutcomeRequest,
    ProductGoalResponseRequest,
    ProductGoalReviewRequest,
    RepositoryAttachRequest,
    RepositoryRefreshRequest,
    RoadmapReviewRequest,
    SpecificationReviewRequest,
    SpecificationSourceRegistrationRequest,
    SpecificationStructuringRequest,
    SprintCloseRequest,
    SprintPlanningRequest,
    SprintPlanReviewRequest,
    SprintReviewRequest,
    SprintStartRequest,
    StoryDependenciesApplyRequest,
    StoryDependencyEdgeRequest,
    StoryReadinessRepair,
    StoryReadinessRepairRequest,
    StoryReviewRequest,
    StoryValidationRequest,
    VisionBootstrapRequest,
    VisionResponseRequest,
    VisionReviewRequest,
    VisionRevisionRequest,
    production_application,
)
from utils.logging_config import configure_logging
from workflow.contracts import (
    JsonObject,
    TransitionResult,
    WorkflowPosition,
)

_JSON_OBJECT = TypeAdapter(JsonObject)


class _ReadProjection(Protocol):
    """Non-routing read methods used by CLI handlers."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

    def repository_status(self, *, project_id: int) -> JsonObject: ...

    def vision_status(self, *, project_id: int) -> JsonObject: ...

    def product_goal_status(self, *, project_id: int) -> JsonObject: ...

    def specification_review(self, *, project_id: int) -> JsonObject: ...

    def artifact_history(
        self,
        *,
        project_id: int,
        node_id: str,
        instance_key: str | None = None,
    ) -> JsonObject: ...

    def story_show(self, *, story_id: int) -> JsonObject: ...

    def story_pending(self, *, project_id: int) -> JsonObject: ...

    def story_dependencies_inspect(self, *, project_id: int) -> JsonObject: ...

    def sprint_candidates(self, *, project_id: int) -> JsonObject: ...

    def sprint_history(self, *, project_id: int) -> JsonObject: ...

    def sprint_metrics(self, *, project_id: int) -> JsonObject: ...

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def context_pack(self, *, project_id: int, phase: str) -> JsonObject: ...

    def status(self, *, project_id: int) -> JsonObject: ...


class _Application(Protocol):
    """Application methods used by CLI handlers."""

    @property
    def reads(self) -> _ReadProjection:
        """Return the injected non-routing projection."""
        ...

    def position(self, *, project_id: int) -> WorkflowPosition:
        """Return one current position."""
        ...

    def create_project(self, request: CreateProjectCommand) -> TransitionResult: ...

    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult: ...

    def respond_to_vision(self, request: VisionResponseRequest) -> TransitionResult: ...

    def review_vision(self, request: VisionReviewRequest) -> TransitionResult: ...

    def begin_vision_revision(
        self,
        request: VisionRevisionRequest,
    ) -> TransitionResult: ...

    def respond_to_product_goal(
        self,
        request: ProductGoalResponseRequest,
    ) -> TransitionResult: ...

    def review_product_goal(
        self,
        request: ProductGoalReviewRequest,
    ) -> TransitionResult: ...

    def resolve_product_goal(
        self,
        request: ProductGoalOutcomeRequest,
    ) -> TransitionResult: ...

    def attach_repository(
        self,
        request: RepositoryAttachRequest,
    ) -> TransitionResult: ...

    def refresh_repository(
        self,
        request: RepositoryRefreshRequest,
    ) -> TransitionResult: ...

    def register_specification_source(
        self,
        request: SpecificationSourceRegistrationRequest,
    ) -> TransitionResult: ...

    def structure_specification(
        self,
        request: SpecificationStructuringRequest,
    ) -> TransitionResult: ...

    def review_specification(
        self,
        request: SpecificationReviewRequest,
    ) -> TransitionResult: ...

    def generate_backlog(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_roadmap(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_story(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_sprint(self, request: SprintPlanningRequest) -> TransitionResult: ...

    def backlog_review(self, project_id: int) -> JsonObject: ...

    def roadmap_review(self, project_id: int) -> JsonObject: ...

    def story_reviews(self, project_id: int) -> JsonObject: ...

    def sprint_plan_review(self, project_id: int) -> JsonObject: ...

    def decide_backlog(
        self,
        request: BacklogReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult: ...

    def decide_roadmap(
        self,
        request: RoadmapReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult: ...

    def decide_story(
        self,
        request: StoryReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult: ...

    def decide_sprint_plan(
        self,
        request: SprintPlanReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult: ...

    def apply_story_dependencies(
        self,
        request: StoryDependenciesApplyRequest,
    ) -> TransitionResult: ...

    def repair_story_readiness(
        self,
        request: StoryReadinessRepairRequest,
    ) -> TransitionResult: ...

    def validate_story(
        self,
        request: StoryValidationRequest,
    ) -> JsonObject: ...

    def start_sprint(self, request: SprintStartRequest) -> TransitionResult: ...

    def complete_task(self, request: CompleteTaskRequest) -> TransitionResult: ...

    def close_story(self, request: CloseStoryRequest) -> TransitionResult: ...

    def review_sprint(self, request: SprintReviewRequest) -> TransitionResult: ...

    def close_sprint(self, request: SprintCloseRequest) -> TransitionResult: ...

    def record_post_sprint_triage(
        self,
        request: PostSprintTriageRequest,
    ) -> TransitionResult: ...


type CommandHandler = Callable[[argparse.Namespace, _Application], int]


class _CliParseError(ValueError):
    """Raised when command arguments are invalid."""


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that leaves JSON error rendering to the transport."""

    def error(self, message: str) -> NoReturn:
        raise _CliParseError(message)


def _read_json_object(path_value: str) -> JsonObject:
    return _JSON_OBJECT.validate_json(Path(path_value).read_text(encoding="utf-8"))


def _write_json(payload: object) -> None:
    sys.stdout.write(f"{json.dumps(payload, sort_keys=True, default=str)}\n")


def _add_mutation_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--correlation-id")


def _parse_story_dependency(value: str) -> StoryDependencyEdgeRequest:
    """Parse DEPENDENT_ID:PREREQUISITE_ID:REASON into one typed edge."""
    parts = value.split(":", maxsplit=2)
    expected_parts = 3
    if len(parts) != expected_parts:
        message = "--dependency must be DEPENDENT_ID:PREREQUISITE_ID:REASON."
        raise argparse.ArgumentTypeError(message)
    try:
        return StoryDependencyEdgeRequest(
            dependent_story_id=int(parts[0]),
            prerequisite_story_id=int(parts[1]),
            reason=parts[2],
        )
    except (ValueError, ValidationError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _parse_story_readiness_repair(value: str) -> StoryReadinessRepair:
    """Parse STORY_ID:POINTS:RANK into one explicit typed repair."""
    parts = value.split(":", maxsplit=2)
    expected_parts = 3
    if len(parts) != expected_parts:
        message = "--repair must be STORY_ID:POINTS:RANK."
        raise argparse.ArgumentTypeError(message)
    try:
        return StoryReadinessRepair(
            story_id=int(parts[0]),
            story_points=int(parts[1]),
            rank=parts[2],
        )
    except (ValueError, ValidationError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _parse_checklist_item(value: str) -> tuple[str, str]:
    """Parse one strict checklist KEY=VALUE pair."""
    key, separator, result = value.partition("=")
    key = key.strip()
    result = result.strip()
    if not separator or not key or not result:
        message = "--checklist-item must be a nonblank KEY=VALUE pair."
        raise argparse.ArgumentTypeError(message)
    return key, result


def _install_artifact_history_reads(
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
) -> None:
    for group, node_id in (
        ("vision", "vision.interview"),
        ("backlog", "backlog.generate"),
        ("roadmap", "planning.roadmap.generate"),
    ):
        history = branches[(group,)].add_parser("history")
        history.add_argument("--project-id", type=int, required=True)
        history.set_defaults(
            command_handler=_artifact_history,
            history_node_id=node_id,
        )


def _install_story_reads(
    story_sub: argparse._SubParsersAction,
    *,
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
    parsers: dict[tuple[str, ...], argparse.ArgumentParser],
) -> None:
    show = story_sub.add_parser("show")
    show.add_argument("--story-id", type=int, required=True)
    show.set_defaults(command_handler=_story_show)
    pending = story_sub.add_parser("pending")
    pending.add_argument("--project-id", type=int, required=True)
    pending.set_defaults(command_handler=_story_pending)
    history = story_sub.add_parser("history")
    history.add_argument("--project-id", type=int, required=True)
    history.add_argument("--instance-key")
    history.set_defaults(
        command_handler=_artifact_history,
        history_node_id="planning.story.generate",
    )
    dependencies = story_sub.add_parser("dependencies")
    dependencies_sub = dependencies.add_subparsers(
        dest="dependency_action",
        required=True,
    )
    parsers[("story", "dependencies")] = dependencies
    branches[("story", "dependencies")] = dependencies_sub
    inspect = dependencies_sub.add_parser("inspect")
    inspect.add_argument("--project-id", type=int, required=True)
    inspect.set_defaults(command_handler=_story_dependencies)
    readiness = story_sub.add_parser("readiness")
    readiness_sub = readiness.add_subparsers(
        dest="readiness_action",
        required=True,
    )
    parsers[("story", "readiness")] = readiness
    branches[("story", "readiness")] = readiness_sub


def _install_sprint_reads(
    sprint_sub: argparse._SubParsersAction,
    *,
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
    parsers: dict[tuple[str, ...], argparse.ArgumentParser],
) -> None:
    for action, handler in (
        ("candidates", _sprint_candidates),
        ("history", _sprint_history),
        ("metrics", _sprint_metrics),
        ("status", _sprint_status),
        ("tasks", _sprint_tasks),
    ):
        read = sprint_sub.add_parser(action)
        read.add_argument("--project-id", type=int, required=True)
        if action in {"status", "tasks"}:
            read.add_argument("--sprint-id", type=int)
        read.set_defaults(command_handler=handler)
    task = sprint_sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_action", required=True)
    parsers[("sprint", "task")] = task
    branches[("sprint", "task")] = task_sub
    for action, handler in (
        ("show", _sprint_task_show),
        ("history", _sprint_task_history),
    ):
        read = task_sub.add_parser(action)
        read.add_argument("--project-id", type=int, required=True)
        read.add_argument("--task-id", type=int, required=True)
        read.add_argument("--sprint-id", type=int)
        read.set_defaults(command_handler=handler)


def _install_read_commands(
    subparsers: argparse._SubParsersAction,
    *,
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
    parsers: dict[tuple[str, ...], argparse.ArgumentParser],
) -> None:
    for group in (
        "vision",
        "goal",
        "repository",
        "specification",
        "backlog",
        "roadmap",
        "story",
        "sprint",
    ):
        group_parser = subparsers.add_parser(group)
        group_sub = group_parser.add_subparsers(
            dest=f"{group}_action",
            required=True,
        )
        parsers[(group,)] = group_parser
        branches[(group,)] = group_sub
    for group, handler in (
        ("vision", _vision_status),
        ("goal", _goal_status),
        ("repository", _repository_status),
        ("specification", _specification_status),
    ):
        status_read = branches[(group,)].add_parser("status")
        status_read.add_argument("--project-id", type=int, required=True)
        status_read.set_defaults(command_handler=handler)
    _install_artifact_history_reads(branches)
    for group, action, handler in (
        ("backlog", "review", _backlog_review),
        ("roadmap", "review", _roadmap_review),
        ("story", "reviews", _story_reviews),
        ("sprint", "plan-review", _sprint_plan_review),
    ):
        review = branches[(group,)].add_parser(action)
        review.add_argument("--project-id", type=int, required=True)
        review.set_defaults(command_handler=handler)
    _install_story_reads(
        branches[("story",)],
        branches=branches,
        parsers=parsers,
    )
    _install_sprint_reads(
        branches[("sprint",)],
        branches=branches,
        parsers=parsers,
    )
    context = subparsers.add_parser("context")
    context_sub = context.add_subparsers(dest="context_action", required=True)
    context_pack = context_sub.add_parser("pack")
    context_pack.add_argument("--project-id", type=int, required=True)
    context_pack.add_argument("--phase", default="sprint-planning")
    context_pack.set_defaults(command_handler=_context_pack)
    status = subparsers.add_parser("status")
    status.add_argument("--project-id", type=int, required=True)
    status.set_defaults(command_handler=_status)


def _semantic_leaf(
    subparsers: argparse._SubParsersAction,
    name: str,
    handler: CommandHandler,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name)
    _add_mutation_metadata(parser)
    parser.set_defaults(command_handler=handler)
    return parser


def _install_planning_action_mutations(
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
) -> None:
    """Install the retained task-specific planning action transports."""
    dependency_apply = _semantic_leaf(
        branches[("story", "dependencies")],
        "apply",
        _story_dependencies_apply,
    )
    dependency_apply.add_argument(
        "--story-id",
        dest="selected_story_ids",
        action="append",
        type=int,
        required=True,
    )
    dependency_apply.add_argument(
        "--dependency",
        dest="reviewed_edges",
        action="append",
        type=_parse_story_dependency,
        default=None,
    )
    readiness_repair = _semantic_leaf(
        branches[("story", "readiness")],
        "repair",
        _story_readiness_repair,
    )
    readiness_repair.add_argument(
        "--repair",
        dest="repairs",
        action="append",
        type=_parse_story_readiness_repair,
        required=True,
    )
    story_validate = _semantic_leaf(
        branches[("story",)],
        "validate",
        _story_validate,
    )
    story_validate.add_argument("--story-id", type=int, required=True)
    story_validate.add_argument(
        "--mode",
        choices=["structural"],
        default="structural",
    )
    _semantic_leaf(branches[("sprint",)], "start", _sprint_start)


def _install_execution_action_mutations(
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
) -> None:
    """Install strict semantic execution and post-Sprint triage commands."""
    complete = _semantic_leaf(
        branches[("sprint", "task")],
        "complete",
        _task_complete,
    )
    complete.add_argument("--instance-key", required=True)
    complete.add_argument("--outcome-summary", required=True)
    complete.add_argument(
        "--artifact-ref",
        dest="artifact_refs",
        action="append",
        required=True,
    )
    complete.add_argument(
        "--acceptance-result",
        choices=("partially_met", "fully_met"),
        required=True,
    )
    complete.add_argument(
        "--checklist-item",
        dest="checklist_items",
        action="append",
        type=_parse_checklist_item,
        required=True,
    )
    story_close = _semantic_leaf(branches[("story",)], "close", _story_close)
    story_close.add_argument("--instance-key", required=True)
    story_close.add_argument("--resolution", required=True)
    story_close.add_argument("--delivered", required=True)
    story_close.add_argument("--evidence", required=True)
    story_close.add_argument("--known-gaps", required=True)
    sprint_review = _semantic_leaf(
        branches[("sprint",)],
        "review",
        _sprint_review,
    )
    sprint_review.add_argument("--instance-key", required=True)
    sprint_close = _semantic_leaf(
        branches[("sprint",)],
        "close",
        _sprint_close,
    )
    sprint_close.add_argument("--instance-key", required=True)
    triage = _semantic_leaf(branches[("sprint",)], "triage", _sprint_triage)
    triage.add_argument("--instance-key", required=True)
    triage.add_argument(
        "--impact",
        choices=("none", "backlog", "specification"),
        required=True,
    )
    triage.add_argument("--file", required=True)


def _install_specification_mutations(
    specification_sub: argparse._SubParsersAction,
) -> None:
    """Install source capture, structuring, and exact human review commands."""
    specification_source = specification_sub.add_parser("source")
    specification_source_sub = specification_source.add_subparsers(
        dest="specification_source_action",
        required=True,
    )
    source_register = _semantic_leaf(
        specification_source_sub,
        "register",
        _specification_source_register,
    )
    source_register.add_argument("--source-path", required=True)
    source_register.add_argument(
        "--preparation-capability",
        choices=("grill-with-docs",),
        required=True,
    )
    source_register.add_argument("--adr-path", dest="adr_paths", action="append")
    _semantic_leaf(specification_sub, "structure", _specification_structure)
    specification_review = _semantic_leaf(
        specification_sub,
        "review",
        _specification_review,
    )
    specification_review.add_argument(
        "--decision", choices=("accepted", "rejected", "feedback"), required=True
    )
    specification_review.add_argument("--rationale", required=True)


def _install_lifecycle_mutations(
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
) -> None:
    _semantic_leaf(branches[("vision",)], "bootstrap", _vision_bootstrap)
    vision_respond = _semantic_leaf(branches[("vision",)], "respond", _vision_respond)
    vision_respond.add_argument("--text", required=True)
    vision_review = _semantic_leaf(branches[("vision",)], "review", _vision_review)
    vision_review.add_argument(
        "--decision", choices=("accepted", "rejected", "feedback"), required=True
    )
    vision_review.add_argument("--rationale", required=True)
    vision_revision = _semantic_leaf(
        branches[("vision",)], "revision", _vision_revision
    )
    vision_revision.add_argument("--reason", required=True)

    goal_respond = _semantic_leaf(branches[("goal",)], "respond", _goal_respond)
    goal_respond.add_argument("--text", required=True)
    goal_review = _semantic_leaf(branches[("goal",)], "review", _goal_review)
    goal_review.add_argument(
        "--decision", choices=("accepted", "rejected", "feedback"), required=True
    )
    goal_review.add_argument("--rationale", required=True)
    for action, outcome in (("complete", "fulfilled"), ("abandon", "abandoned")):
        goal_outcome = _semantic_leaf(branches[("goal",)], action, _goal_outcome)
        goal_outcome.add_argument("--rationale", required=True)
        goal_outcome.set_defaults(goal_outcome=outcome)

    repository_attach = _semantic_leaf(
        branches[("repository",)], "attach", _repository_attach
    )
    repository_attach.add_argument("--path", required=True)
    _semantic_leaf(branches[("repository",)], "refresh", _repository_refresh)

    _install_specification_mutations(branches[("specification",)])

    for group, handler in (
        ("backlog", _backlog_generate),
        ("roadmap", _roadmap_generate),
        ("story", _story_generate),
    ):
        generate = _semantic_leaf(branches[(group,)], "generate", handler)
        generate.add_argument("--instance-key", required=group == "story")

    for group, handler in (
        ("backlog", _backlog_decide),
        ("roadmap", _roadmap_decide),
        ("sprint", _sprint_decide),
    ):
        review = _semantic_leaf(branches[(group,)], "decide", handler)
        review.add_argument(
            "--decision",
            choices=("accepted", "rejected", "feedback"),
            required=True,
        )
        review.add_argument("--rationale", required=True)
    story_review = _semantic_leaf(branches[("story",)], "decide", _story_decide)
    story_review.add_argument(
        "--decision",
        choices=("accepted", "rejected", "feedback"),
        required=True,
    )
    story_review.add_argument("--rationale", required=True)

    _install_planning_action_mutations(branches)
    _install_execution_action_mutations(branches)

    sprint_generate = _semantic_leaf(
        branches[("sprint",)],
        "generate",
        _sprint_generate,
    )
    sprint_generate.add_argument("--input", dest="user_input")
    sprint_generate.add_argument("--selected-story-ids", nargs="+", type=int)
    sprint_generate.add_argument("--max-story-points", type=int)
    sprint_generate.add_argument("--team-name", required=True)


def build_parser() -> argparse.ArgumentParser:
    """Build the graph-backed command tree."""
    parser = _ArgumentParser(prog="agileforge")
    parser.add_argument("--version", action="version", version=agileforge_version())
    subparsers = parser.add_subparsers(
        dest="group",
        required=True,
        parser_class=_ArgumentParser,
    )

    branches: dict[tuple[str, ...], argparse._SubParsersAction] = {(): subparsers}
    parsers: dict[tuple[str, ...], argparse.ArgumentParser] = {}

    project = subparsers.add_parser("project")
    project_sub = project.add_subparsers(dest="project_action", required=True)
    parsers[("project",)] = project
    branches[("project",)] = project_sub
    project_list = project_sub.add_parser("list")
    project_list.set_defaults(command_handler=_project_list)
    project_show = project_sub.add_parser("show")
    project_show.add_argument("--project-id", type=int, required=True)
    project_show.set_defaults(command_handler=_project_show)
    create = project_sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--description")
    create.add_argument("--repository-path")
    create.add_argument("--idempotency-key", required=True)
    create.add_argument("--actor", required=True)
    create.set_defaults(command_handler=_create_project)
    workflow = subparsers.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="workflow_action", required=True)
    next_command = workflow_sub.add_parser("next")
    next_command.add_argument("--project-id", type=int, required=True)
    next_command.set_defaults(command_handler=_workflow_next)
    position = workflow_sub.add_parser("position")
    position.add_argument("--project-id", type=int, required=True)
    position.add_argument("--include-optional", action="store_true")
    position.set_defaults(command_handler=_workflow_position)

    _install_read_commands(
        subparsers,
        branches=branches,
        parsers=parsers,
    )
    _install_lifecycle_mutations(branches)

    return parser


def _emit_read(result: JsonObject) -> int:
    _write_json(result)
    return 0 if result.get("ok") is True else 1


def _project_list(_args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.project_list())


def _project_show(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.project_show(project_id=args.project_id))


def _repository_status(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.repository_status(project_id=args.project_id))


def _vision_status(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.vision_status(project_id=args.project_id))


def _goal_status(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.product_goal_status(project_id=args.project_id))


def _specification_status(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_read(
        application.reads.specification_review(project_id=args.project_id)
    )


def _confirm_specification_review(
    packet: JsonObject,
    *,
    decision: str,
) -> bool:
    """Display the captured packet before obtaining the human confirmation."""
    rendered = json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stderr.write(f"Exact Specification review packet:\n{rendered}\n")
    sys.stderr.write(f"Confirm {decision} for this exact candidate? [y/N] ")
    sys.stderr.flush()
    try:
        answer = sys.stdin.readline()
    except OSError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _backlog_review(args: argparse.Namespace, application: _Application) -> int:
    return _emit_human_planning_review(application.backlog_review(args.project_id))


def _roadmap_review(args: argparse.Namespace, application: _Application) -> int:
    return _emit_human_planning_review(application.roadmap_review(args.project_id))


def _story_reviews(args: argparse.Namespace, application: _Application) -> int:
    return _emit_human_planning_review(application.story_reviews(args.project_id))


def _sprint_plan_review(args: argparse.Namespace, application: _Application) -> int:
    return _emit_human_planning_review(application.sprint_plan_review(args.project_id))


def _artifact_history(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(
        application.reads.artifact_history(
            project_id=args.project_id,
            node_id=args.history_node_id,
            instance_key=getattr(args, "instance_key", None),
        )
    )


def _story_show(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.story_show(story_id=args.story_id))


def _story_pending(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.story_pending(project_id=args.project_id))


def _story_dependencies(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_read(
        application.reads.story_dependencies_inspect(project_id=args.project_id)
    )


def _sprint_candidates(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.sprint_candidates(project_id=args.project_id))


def _sprint_history(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.sprint_history(project_id=args.project_id))


def _sprint_metrics(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.sprint_metrics(project_id=args.project_id))


def _sprint_status(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(
        application.reads.sprint_status(
            project_id=args.project_id,
            sprint_id=args.sprint_id,
        )
    )


def _sprint_tasks(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(
        application.reads.sprint_tasks(
            project_id=args.project_id,
            sprint_id=args.sprint_id,
        )
    )


def _sprint_task_show(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(
        application.reads.sprint_task_show(
            project_id=args.project_id,
            task_id=args.task_id,
            sprint_id=args.sprint_id,
        )
    )


def _sprint_task_history(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_read(
        application.reads.sprint_task_history(
            project_id=args.project_id,
            task_id=args.task_id,
            sprint_id=args.sprint_id,
        )
    )


def _context_pack(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(
        application.reads.context_pack(
            project_id=args.project_id,
            phase=args.phase,
        )
    )


def _status(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.status(project_id=args.project_id))


def _create_project(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.create_project(
            CreateProjectCommand(
                name=args.name,
                description=args.description,
                repository_path=args.repository_path,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
            )
        )
    )


def _vision_bootstrap(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.bootstrap_vision(
            VisionBootstrapRequest(
                project_id=args.project_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _vision_respond(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.respond_to_vision(
            VisionResponseRequest(
                project_id=args.project_id,
                text=args.text,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _vision_review(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    return _emit_result(
        application.review_vision(
            VisionReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _vision_revision(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.begin_vision_revision(
            VisionRevisionRequest(
                project_id=args.project_id,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _goal_respond(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.respond_to_product_goal(
            ProductGoalResponseRequest(
                project_id=args.project_id,
                text=args.text,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _goal_review(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    return _emit_result(
        application.review_product_goal(
            ProductGoalReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _goal_outcome(args: argparse.Namespace, application: _Application) -> int:
    outcome = cast("Literal['fulfilled', 'abandoned']", args.goal_outcome)
    return _emit_result(
        application.resolve_product_goal(
            ProductGoalOutcomeRequest(
                project_id=args.project_id,
                outcome=outcome,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _repository_attach(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.attach_repository(
            RepositoryAttachRequest(
                project_id=args.project_id,
                path=args.path,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _repository_refresh(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.refresh_repository(
            RepositoryRefreshRequest(
                project_id=args.project_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _specification_source_register(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_result(
        application.register_specification_source(
            SpecificationSourceRegistrationRequest(
                project_id=args.project_id,
                source_path=args.source_path,
                preparation_capability=args.preparation_capability,
                adr_paths=tuple(args.adr_paths or ()),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _specification_structure(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_result(
        application.structure_specification(
            SpecificationStructuringRequest(
                project_id=args.project_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _specification_review(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    packet = application.reads.specification_review(project_id=args.project_id)
    data = packet.get("data")
    candidate = data.get("candidate") if isinstance(data, dict) else None
    expected_candidate_fingerprint = (
        candidate.get("candidate_fingerprint") if isinstance(candidate, dict) else None
    )
    if not isinstance(expected_candidate_fingerprint, str) or not (
        expected_candidate_fingerprint.strip()
    ):
        message = (
            "Specification review requires the exact current review packet. "
            "Run specification status and retry."
        )
        raise ValueError(message)
    if not _confirm_specification_review(packet, decision=decision):
        message = "Specification review cancelled before any decision was recorded."
        raise ValueError(message)
    return _emit_result(
        application.review_specification(
            SpecificationReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                expected_candidate_fingerprint=expected_candidate_fingerprint,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _delivery_action_request(args: argparse.Namespace) -> DeliveryActionRequest:
    return DeliveryActionRequest(
        project_id=args.project_id,
        instance_key=args.instance_key,
        idempotency_key=args.idempotency_key,
        actor=args.actor,
        correlation_id=args.correlation_id,
    )


def _backlog_generate(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(application.generate_backlog(_delivery_action_request(args)))


def _roadmap_generate(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(application.generate_roadmap(_delivery_action_request(args)))


def _story_generate(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(application.generate_story(_delivery_action_request(args)))


def _sprint_generate(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.generate_sprint(
            SprintPlanningRequest(
                project_id=args.project_id,
                guidance=args.user_input,
                selected_story_ids=tuple(args.selected_story_ids or ()),
                max_story_points=args.max_story_points,
                team_name=args.team_name,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _review_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        message = f"{label} is unavailable."
        raise ValueError(message)
    return cast("dict[str, object]", value)


def _review_items(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        message = f"{label} is unavailable."
        raise ValueError(message)
    return cast("list[object]", value)


def _review_text(value: object) -> str:
    if value is None:
        return "Not specified"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str | int):
        return str(value)
    raise ValueError("Planning review text is invalid.")


def _list_lines(label: str, value: object, *, indent: str = "") -> list[str]:
    items = _review_items(value, label)
    lines = [f"{indent}{label}:"]
    lines.extend(f"{indent}- {_review_text(item)}" for item in items)
    if not items:
        lines.append(f"{indent}- None")
    return lines


def _specification_lines(value: object, *, indent: str = "") -> list[str]:
    lines = [f"{indent}Specification evidence:"]
    for raw_item in _review_items(value, "Specification evidence"):
        item = _review_object(raw_item, "Specification evidence item")
        lines.extend(
            [
                f"{indent}- Title: {_review_text(item.get('title'))}",
                f"{indent}  Statement: {_review_text(item.get('statement'))}",
                f"{indent}  Level: {_review_text(item.get('level'))}",
                f"{indent}  Acceptance criteria:",
                *(
                    f"{indent}  - {_review_text(criterion)}"
                    for criterion in _review_items(
                        item.get("acceptance_criteria"),
                        "Specification acceptance criteria",
                    )
                ),
                (
                    f"{indent}  Verification: "
                    f"{_review_text(item.get('verification_method'))}"
                ),
            ]
        )
    return lines


def _backlog_item_lines(value: object, *, indent: str = "") -> list[str]:
    item = _review_object(value, "Backlog item")
    lines = [
        f"{indent}Requirement: {_review_text(item.get('requirement'))}",
        f"{indent}Priority: {_review_text(item.get('priority'))}",
        f"{indent}Value driver: {_review_text(item.get('value_driver'))}",
        f"{indent}Justification: {_review_text(item.get('justification'))}",
        f"{indent}Estimated effort: {_review_text(item.get('estimated_effort'))}",
    ]
    if item.get("technical_note") is not None:
        lines.append(
            f"{indent}Implementation note: {_review_text(item['technical_note'])}"
        )
    lines.extend(
        _specification_lines(item.get("specification_evidence"), indent=indent)
    )
    return lines


def _backlog_review_lines(candidate: dict[str, object]) -> list[str]:
    lines = ["Backlog review"]
    for item in _review_items(candidate.get("backlog_items"), "Backlog items"):
        lines.extend(["", *_backlog_item_lines(item)])
    lines.extend(
        [
            "",
            f"Complete: {_review_text(candidate.get('is_complete'))}",
            *_list_lines("Clarifying questions", candidate.get("clarifying_questions")),
        ]
    )
    return lines


def _roadmap_review_lines(candidate: dict[str, object]) -> list[str]:
    lines = [
        "Roadmap review",
        f"Summary: {_review_text(candidate.get('roadmap_summary'))}",
    ]
    for raw_release in _review_items(
        candidate.get("roadmap_releases"), "Roadmap releases"
    ):
        release = _review_object(raw_release, "Roadmap release")
        lines.extend(
            [
                "",
                f"Release: {_review_text(release.get('release_name'))}",
                f"Theme: {_review_text(release.get('theme'))}",
                f"Focus: {_review_text(release.get('focus_area'))}",
                f"Reasoning: {_review_text(release.get('reasoning'))}",
                "Included requirements:",
            ]
        )
        for item in _review_items(
            release.get("backlog_items"), "Roadmap Backlog items"
        ):
            lines.extend(_backlog_item_lines(item, indent="  "))
    lines.extend(
        [
            "",
            f"Complete: {_review_text(candidate.get('is_complete'))}",
            *_list_lines("Clarifying questions", candidate.get("clarifying_questions")),
        ]
    )
    return lines


_INVEST_DIMENSION_NAMES: tuple[str, ...] = (
    "independent",
    "negotiable",
    "valuable",
    "estimable",
    "small",
    "testable",
)


def _is_well_formed_invest_dimension(dim: object) -> bool:
    if not isinstance(dim, dict):
        return False
    dim_dict = cast("dict[str, object]", dim)
    if set(dim_dict.keys()) != {"result", "rationale", "evidence"}:
        return False
    result = dim_dict.get("result")
    rationale = dim_dict.get("rationale")
    evidence = dim_dict.get("evidence")
    if not isinstance(result, str) or result not in {"pass", "concern", "fail"}:
        return False
    if not isinstance(rationale, str) or not rationale.strip():
        return False
    return bool(isinstance(evidence, str) and evidence.strip())


def _is_well_formed_invest_assessment(assessment: object) -> bool:
    if not isinstance(assessment, dict):
        return False
    assessment_dict = cast("dict[str, object]", assessment)
    if set(assessment_dict.keys()) != set(_INVEST_DIMENSION_NAMES):
        return False
    return all(
        _is_well_formed_invest_dimension(assessment_dict.get(dim_name))
        for dim_name in _INVEST_DIMENSION_NAMES
    )


def _is_story_review_acceptable(review: object) -> bool:
    if not isinstance(review, dict):
        return False
    review_dict = cast("dict[str, object]", review)
    candidate = review_dict.get("candidate")
    if not isinstance(candidate, dict):
        return False
    candidate_dict = cast("dict[str, object]", candidate)
    story_items = candidate_dict.get("story_items")
    if not isinstance(story_items, list) or not story_items:
        return False
    for item in story_items:
        if not isinstance(item, dict):
            return False
        item_dict = cast("dict[str, object]", item)
        if not _is_well_formed_invest_assessment(item_dict.get("invest_assessment")):
            return False
    return True


def _invest_assessment_lines(value: object, *, indent: str = "") -> list[str]:
    sub_indent = f"{indent}  "
    if not isinstance(value, dict):
        return [
            f"{indent}INVEST assessment: [MALFORMED / MISSING] - "
            "required quality evidence is incomplete"
        ]
    dict_val = cast("dict[str, object]", value)
    lines = [f"{indent}INVEST assessment:"]
    dimensions = [
        ("Independent", dict_val.get("independent")),
        ("Negotiable", dict_val.get("negotiable")),
        ("Valuable", dict_val.get("valuable")),
        ("Estimable", dict_val.get("estimable")),
        ("Small", dict_val.get("small")),
        ("Testable", dict_val.get("testable")),
    ]
    for name, dim_raw in dimensions:
        if _is_well_formed_invest_dimension(dim_raw):
            dim_dict = cast("dict[str, object]", dim_raw)
            result = cast("str", dim_dict["result"]).upper()
            rationale = cast("str", dim_dict["rationale"]).strip()
            evidence = cast("str", dim_dict["evidence"]).strip()
            lines.append(
                f"{sub_indent}- {name} [{result}]: "
                f"{rationale} (Evidence: {evidence})"
            )
        elif isinstance(dim_raw, dict):
            dim_dict = cast("dict[str, object]", dim_raw)
            result_raw = dim_dict.get("result")
            result = (
                result_raw.upper()
                if isinstance(result_raw, str) and result_raw.strip()
                else "MISSING"
            )
            rationale_raw = dim_dict.get("rationale")
            rat_text = (
                rationale_raw.strip()
                if isinstance(rationale_raw, str) and rationale_raw.strip()
                else "MISSING"
            )
            evidence_raw = dim_dict.get("evidence")
            evi_text = (
                evidence_raw.strip()
                if isinstance(evidence_raw, str) and evidence_raw.strip()
                else "MISSING"
            )
            lines.append(
                f"{sub_indent}- {name} [INVALID]: result={result}, "
                f"rationale={rat_text}, evidence={evi_text}"
            )
        else:
            raw_text = (
                dim_raw.strip()
                if isinstance(dim_raw, str) and dim_raw.strip()
                else "No dimension assessment provided"
            )
            lines.append(f"{sub_indent}- {name} [MISSING]: {raw_text}")
    return lines


def _dependency_candidate_lines(values: object, *, indent: str = "") -> list[str]:
    if not isinstance(values, list):
        return []
    if not values:
        return [f"{indent}Proposed dependencies: None"]
    lines = [f"{indent}Proposed dependencies:"]
    sub_indent = f"{indent}  "
    for item in values:
        if isinstance(item, dict):
            item_dict = cast("dict[str, object]", item)
            pref = _review_text(item_dict.get("prerequisite_ref"))
            reason = _review_text(item_dict.get("reason"))
            conf = _review_text(item_dict.get("confidence"))
            lines.append(f"{sub_indent}- Prerequisite: {pref} ({conf}) - {reason}")
        else:
            lines.append(f"{sub_indent}- {_review_text(item)}")
    return lines


def _story_item_lines(value: object, *, indent: str = "") -> list[str]:
    story = _review_object(value, "Story item")
    lines = [
        (
            f"{indent}Story: "
            f"{_review_text(story.get('story_title') or story.get('title'))}"
        ),
        f"{indent}Statement: {_review_text(story.get('statement'))}",
        f"{indent}Persona: {_review_text(story.get('persona'))}",
    ]
    if story.get("rank") is not None:
        order = story.get("order")
        order_text = f"Order: {_review_text(order)} | " if order is not None else ""
        lines.append(f"{indent}{order_text}Rank: {_review_text(story['rank'])}")
    if story.get("estimated_effort") is not None:
        effort = _review_text(story["estimated_effort"])
        points = story.get("story_points")
        if points is not None:
            lines.append(
                f"{indent}Estimated effort: {effort} "
                f"(derived: {_review_text(points)} story points)"
            )
        else:
            lines.append(f"{indent}Estimated effort: {effort}")
    lines.extend(
        _list_lines(
            "Acceptance criteria", story.get("acceptance_criteria"), indent=indent
        )
    )
    lines.extend(
        _specification_lines(story.get("specification_evidence"), indent=indent)
    )
    lines.extend(
        _invest_assessment_lines(story.get("invest_assessment"), indent=indent)
    )
    if story.get("research_caveats") is not None:
        lines.extend(
            _list_lines("Research caveats", story["research_caveats"], indent=indent)
        )
    if story.get("dependency_candidates") is not None:
        lines.extend(
            _dependency_candidate_lines(
                story["dependency_candidates"], indent=indent
            )
        )
    if story.get("reason_for_selection") is not None:
        lines.append(
            f"{indent}Reason for selection: "
            f"{_review_text(story['reason_for_selection'])}"
        )
    return lines


def _story_review_lines(
    review: dict[str, object], candidate: dict[str, object]
) -> list[str]:
    lines = ["Story review", "", "Source requirement:"]
    lineage = _review_object(review.get("lineage"), "Story review source")
    lines.extend(_backlog_item_lines(lineage.get("backlog_item"), indent="  "))
    for item in _review_items(candidate.get("story_items"), "Story items"):
        lines.extend(["", *_story_item_lines(item)])
    lines.extend(
        [
            "",
            f"Complete: {_review_text(candidate.get('is_complete'))}",
            *_list_lines("Clarifying questions", candidate.get("clarifying_questions")),
        ]
    )
    return lines


def _sprint_review_lines(candidate: dict[str, object]) -> list[str]:
    lines = [
        "Sprint plan review",
        f"Team: {_review_text(candidate.get('team_name'))}",
        f"Sprint goal: {_review_text(candidate.get('sprint_goal'))}",
    ]
    for raw_story in _review_items(
        candidate.get("selected_stories"), "Selected Stories"
    ):
        story = _review_object(raw_story, "Selected Story")
        lines.extend(["", *_story_item_lines(story)])
        lines.append("Tasks:")
        for raw_task in _review_items(story.get("tasks"), "Planned Tasks"):
            task = _review_object(raw_task, "Planned Task")
            lines.extend(
                [
                    f"- Description: {_review_text(task.get('description'))}",
                    f"  Kind: {_review_text(task.get('task_kind'))}",
                    *_list_lines("Checklist", task.get("checklist_items"), indent="  "),
                    *_specification_lines(
                        task.get("specification_evidence"), indent="  "
                    ),
                ]
            )
    return lines


def _render_planning_review(value: object) -> str:
    """Render one closed planning phase in operator language only."""
    review = _review_object(value, "Planning review")
    candidate = _review_object(review.get("candidate"), "Planning candidate")
    phase = review.get("phase")
    if phase == "backlog":
        lines = _backlog_review_lines(candidate)
    elif phase == "roadmap":
        lines = _roadmap_review_lines(candidate)
    elif phase == "story":
        lines = _story_review_lines(review, candidate)
    elif phase == "sprint_plan":
        lines = _sprint_review_lines(candidate)
    else:
        raise ValueError("Planning review phase is unsupported.")
    return "\n".join(lines).strip() + "\n"


def _emit_human_planning_review(result: JsonObject) -> int:
    """Print planning evidence without its machine-only binding or identities."""
    if result.get("ok") is not True:
        return _emit_read(result)
    data = _review_object(result.get("data"), "Planning review data")
    raw_items = data.get("items")
    if raw_items is None:
        output = _render_planning_review(data.get("review"))
    else:
        sections = []
        for ordinal, raw_item in enumerate(
            _review_items(raw_items, "Story reviews"), start=1
        ):
            item = _review_object(raw_item, "Story review")
            sections.append(
                f"Story review {ordinal}\n{_render_planning_review(item.get('review'))}"
            )
        output = "\n".join(sections)
    sys.stdout.write(output)
    return 0


def _review_binding(value: object) -> ExpectedPlanningReviewBinding:
    if not isinstance(value, dict):
        raise ValueError("Planning review has no machine binding.")
    return ExpectedPlanningReviewBinding.model_validate(value)


def _confirm_planning_review(review: object, *, decision: str) -> bool:
    rendered = _render_planning_review(review)
    sys.stderr.write(f"Exact planning review:\n{rendered}\n")
    sys.stderr.write(f"Confirm {decision} for this exact candidate? [y/N] ")
    sys.stderr.flush()
    try:
        answer = sys.stdin.readline()
    except OSError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _unique_review(result: JsonObject) -> tuple[ExpectedPlanningReviewBinding, object]:
    if result.get("ok") is not True or not isinstance(result.get("data"), dict):
        raise ValueError("Exact planning review is unavailable. Reload and retry.")
    data = cast("dict[str, object]", result["data"])
    return _review_binding(data.get("binding")), data.get("review")


def _story_review_choice(
    result: JsonObject,
) -> tuple[ExpectedPlanningReviewBinding, object]:
    if result.get("ok") is not True or not isinstance(result.get("data"), dict):
        raise ValueError("Exact Story reviews are unavailable. Reload and retry.")
    data = cast("dict[str, object]", result["data"])
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("No Story review is currently pending.")
    candidates = cast("list[object]", items)
    for ordinal, item in enumerate(candidates, start=1):
        review = (
            cast("dict[str, object]", item).get("review")
            if isinstance(item, dict)
            else None
        )
        rendered = _render_planning_review(review)
        sys.stderr.write(f"Story review {ordinal}:\n{rendered}\n")
    if len(candidates) == 1:
        selected = candidates[0]
    else:
        if not sys.stdin.isatty():
            raise ValueError(
                "Multiple Story reviews require an interactive numbered choice. "
                "Machine clients must use the API review binding."
            )
        sys.stderr.write(f"Choose Story review [1-{len(items)}]: ")
        sys.stderr.flush()
        selected_text = sys.stdin.readline().strip()
        if not selected_text.isdigit() or not 1 <= int(selected_text) <= len(items):
            raise ValueError("Story review choice is invalid.")
        selected = candidates[int(selected_text) - 1]
    if not isinstance(selected, dict):
        raise ValueError("Selected Story review is invalid.")
    selected_mapping = cast("dict[str, object]", selected)
    return (
        _review_binding(selected_mapping.get("binding")),
        selected_mapping.get("review"),
    )


def _backlog_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    binding, review = _unique_review(application.backlog_review(args.project_id))
    if not _confirm_planning_review(review, decision=decision):
        raise ValueError("Backlog review cancelled before any write.")
    return _emit_result(
        application.decide_backlog(
            BacklogReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            ),
            expected=binding,
        )
    )


def _roadmap_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    binding, review = _unique_review(application.roadmap_review(args.project_id))
    if not _confirm_planning_review(review, decision=decision):
        raise ValueError("Roadmap review cancelled before any write.")
    return _emit_result(
        application.decide_roadmap(
            RoadmapReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            ),
            expected=binding,
        )
    )


def _story_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    binding, review = _story_review_choice(application.story_reviews(args.project_id))
    if decision == "accepted" and not _is_story_review_acceptable(review):
        raise ValueError(
            "Story proposal cannot be accepted: required INVEST quality assessment "
            "is missing or malformed."
        )
    if not _confirm_planning_review(review, decision=decision):
        raise ValueError("Story review cancelled before any write.")
    return _emit_result(
        application.decide_story(
            StoryReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            ),
            expected=binding,
        )
    )


def _sprint_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    binding, review = _unique_review(application.sprint_plan_review(args.project_id))
    if not _confirm_planning_review(review, decision=decision):
        raise ValueError("Sprint-plan review cancelled before any write.")
    return _emit_result(
        application.decide_sprint_plan(
            SprintPlanReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            ),
            expected=binding,
        )
    )


def _story_dependencies_apply(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_result(
        application.apply_story_dependencies(
            StoryDependenciesApplyRequest(
                project_id=args.project_id,
                selected_story_ids=tuple(args.selected_story_ids),
                reviewed_edges=tuple(args.reviewed_edges or ()),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _story_readiness_repair(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_result(
        application.repair_story_readiness(
            StoryReadinessRepairRequest(
                project_id=args.project_id,
                repairs=tuple(args.repairs),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _story_validate(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_read(
        application.validate_story(
            StoryValidationRequest(
                project_id=args.project_id,
                story_id=args.story_id,
                mode=args.mode,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _sprint_start(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.start_sprint(
            SprintStartRequest(
                project_id=args.project_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _task_complete(args: argparse.Namespace, application: _Application) -> int:
    checklist_items = cast("list[tuple[str, str]]", args.checklist_items)
    checklist_result = dict(checklist_items)
    if len(checklist_result) != len(checklist_items):
        message = "--checklist-item keys must be unique."
        raise ValueError(message)
    acceptance_result = cast(
        "Literal['partially_met', 'fully_met']",
        args.acceptance_result,
    )
    return _emit_result(
        application.complete_task(
            CompleteTaskRequest(
                project_id=args.project_id,
                instance_key=args.instance_key,
                outcome_summary=args.outcome_summary,
                artifact_refs=tuple(args.artifact_refs),
                acceptance_result=acceptance_result,
                checklist_result=checklist_result,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _story_close(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.close_story(
            CloseStoryRequest(
                project_id=args.project_id,
                instance_key=args.instance_key,
                resolution=args.resolution,
                delivered=args.delivered,
                evidence=args.evidence,
                known_gaps=args.known_gaps,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _sprint_review(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.review_sprint(
            SprintReviewRequest(
                project_id=args.project_id,
                instance_key=args.instance_key,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _sprint_close(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.close_sprint(
            SprintCloseRequest(
                project_id=args.project_id,
                instance_key=args.instance_key,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _sprint_triage(args: argparse.Namespace, application: _Application) -> int:
    impact = cast("Literal['none', 'backlog', 'specification']", args.impact)
    return _emit_result(
        application.record_post_sprint_triage(
            PostSprintTriageRequest(
                project_id=args.project_id,
                instance_key=args.instance_key,
                impact=impact,
                canonical_payload=_read_json_object(args.file),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _workflow_next(args: argparse.Namespace, application: _Application) -> int:
    _write_json(workflow_next(application=application, project_id=args.project_id))
    return 0


def _workflow_position(args: argparse.Namespace, application: _Application) -> int:
    _write_json(
        workflow_position(
            application=application,
            project_id=args.project_id,
            include_optional=args.include_optional,
        )
    )
    return 0


def _emit_result(result: TransitionResult) -> int:
    _write_json(result.model_dump(mode="json"))
    return 0 if result.ok else 1


def main(argv: list[str] | None = None, *, application: object | None = None) -> int:
    """Run one graph-backed CLI command."""
    configure_logging(console=False)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        selected = cast("_Application", application or production_application())
        handler = cast("CommandHandler", args.command_handler)
        return handler(args, selected)
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _write_json({"ok": False, "error": str(error)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
