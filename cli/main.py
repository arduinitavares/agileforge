"""AgileForge CLI backed exclusively by the durable workflow graph."""

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
    AuthorityCompileRequest,
    AuthorityFeedbackRequest,
    AuthorityRepairRequest,
    AuthorityReviewRequest,
    BacklogReviewRequest,
    CloseStoryRequest,
    CompleteTaskRequest,
    CreateProjectCommand,
    DeliveryActionRequest,
    PostSprintTriageRequest,
    ProductGoalOutcomeRequest,
    ProductGoalResponseRequest,
    ProductGoalReviewRequest,
    RepositoryAttachRequest,
    RepositoryRefreshRequest,
    RoadmapReviewRequest,
    SpecificationAuthoringRequest,
    SpecificationReviewRequest,
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

    def authority_status(self, *, project_id: int) -> JsonObject: ...

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject: ...

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
    ) -> JsonObject: ...

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

    def author_specification(
        self,
        request: SpecificationAuthoringRequest,
    ) -> TransitionResult: ...

    def review_specification(
        self,
        request: SpecificationReviewRequest,
    ) -> TransitionResult: ...

    def compile_authority(
        self,
        request: AuthorityCompileRequest,
    ) -> TransitionResult: ...

    def decide_authority(self, request: AuthorityReviewRequest) -> TransitionResult: ...

    def record_authority_feedback(
        self,
        request: AuthorityFeedbackRequest,
    ) -> TransitionResult: ...

    def repair_authority(self, request: AuthorityRepairRequest) -> TransitionResult: ...

    def generate_backlog(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_roadmap(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_story(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_sprint(self, request: SprintPlanningRequest) -> TransitionResult: ...

    def decide_backlog(self, request: BacklogReviewRequest) -> TransitionResult: ...

    def decide_roadmap(self, request: RoadmapReviewRequest) -> TransitionResult: ...

    def decide_story(self, request: StoryReviewRequest) -> TransitionResult: ...

    def decide_sprint_plan(
        self,
        request: SprintPlanReviewRequest,
    ) -> TransitionResult: ...

    def apply_story_dependencies(
        self,
        request: StoryDependenciesApplyRequest,
    ) -> TransitionResult: ...

    def repair_story_readiness(
        self,
        request: StoryReadinessRepairRequest,
    ) -> TransitionResult: ...

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


def _install_authority_reads(
    authority_sub: argparse._SubParsersAction,
) -> None:
    status = authority_sub.add_parser("status")
    status.add_argument("--project-id", type=int, required=True)
    status.set_defaults(command_handler=_authority_status)
    invariants = authority_sub.add_parser("invariants")
    invariants.add_argument("--project-id", type=int, required=True)
    invariants.add_argument("--spec-version-id", type=int)
    invariants.set_defaults(command_handler=_authority_invariants)
    review = authority_sub.add_parser("review")
    review.add_argument("--project-id", type=int, required=True)
    review.add_argument(
        "--include-spec",
        choices=("auto", "full", "summary"),
        default="auto",
    )
    review.set_defaults(command_handler=_authority_review)


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
        "authority",
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
    _install_authority_reads(branches[("authority",)])
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

    _semantic_leaf(
        branches[("specification",)], "author", _specification_author
    )
    specification_review = _semantic_leaf(
        branches[("specification",)], "review", _specification_review
    )
    specification_review.add_argument(
        "--decision", choices=("accepted", "rejected", "feedback"), required=True
    )
    specification_review.add_argument("--rationale", required=True)

    _semantic_leaf(branches[("authority",)], "compile", _authority_compile)
    authority_decide = _semantic_leaf(
        branches[("authority",)], "decide", _authority_decide
    )
    authority_decide.add_argument(
        "--decision", choices=("accepted", "rejected"), required=True
    )
    authority_decide.add_argument("--rationale", required=True)
    authority_feedback = _semantic_leaf(
        branches[("authority",)], "feedback", _authority_feedback
    )
    authority_feedback.add_argument("--feedback", required=True)
    _semantic_leaf(branches[("authority",)], "repair", _authority_repair)

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
    story_review.add_argument("--instance-key", required=True)
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
    sprint_generate.add_argument(
        "--no-task-decomposition",
        action="store_false",
        dest="include_task_decomposition",
        default=True,
    )
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


def _authority_status(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.authority_status(project_id=args.project_id))


def _authority_invariants(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_read(
        application.reads.authority_invariants(
            project_id=args.project_id,
            spec_version_id=args.spec_version_id,
        )
    )


def _authority_review(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(
        application.reads.authority_review(
            project_id=args.project_id,
            include_spec=args.include_spec,
        )
    )


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


def _specification_author(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_result(
        application.author_specification(
            SpecificationAuthoringRequest(
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
    return _emit_result(
        application.review_specification(
            SpecificationReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _authority_compile(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.compile_authority(
            AuthorityCompileRequest(
                project_id=args.project_id,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _authority_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected']", args.decision)
    return _emit_result(
        application.decide_authority(
            AuthorityReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _authority_feedback(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.record_authority_feedback(
            AuthorityFeedbackRequest(
                project_id=args.project_id,
                feedback=args.feedback,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _authority_repair(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.repair_authority(
            AuthorityRepairRequest(
                project_id=args.project_id,
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
                include_task_decomposition=args.include_task_decomposition,
                team_name=args.team_name,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _backlog_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    return _emit_result(
        application.decide_backlog(
            BacklogReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _roadmap_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    return _emit_result(
        application.decide_roadmap(
            RoadmapReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _story_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    return _emit_result(
        application.decide_story(
            StoryReviewRequest(
                project_id=args.project_id,
                instance_key=args.instance_key,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _sprint_decide(args: argparse.Namespace, application: _Application) -> int:
    decision = cast("Literal['accepted', 'rejected', 'feedback']", args.decision)
    return _emit_result(
        application.decide_sprint_plan(
            SprintPlanReviewRequest(
                project_id=args.project_id,
                decision=decision,
                rationale=args.rationale,
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
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
