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
    COMMAND_PREFIXES,
    workflow_next,
    workflow_position,
)
from services.agent_workbench.version import agileforge_version
from services.application import (
    AuthorityCompileRequest,
    AuthorityRepairRequest,
    AuthorityReviewRequest,
    CreateProjectCommand,
    DeliveryActionRequest,
    DiscoveryArtifactRequest,
    ProductGoalOutcomeRequest,
    ProductGoalResponseRequest,
    ProductGoalReviewRequest,
    RepositoryAttachRequest,
    RepositoryRefreshRequest,
    SpecificationCandidateRequest,
    SpecificationReviewRequest,
    SprintPlanningRequest,
    VisionResponseRequest,
    VisionReviewRequest,
    VisionRevisionRequest,
    production_application,
)
from utils.logging_config import configure_logging
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.requests import TransitionRequest

_JSON_OBJECT = TypeAdapter(JsonObject)
_TRANSITION_REQUEST = TypeAdapter(TransitionRequest)

_SEMANTIC_REQUEST_KINDS = frozenset(
    {
        "abandon_product_goal",
        "begin_vision_revision",
        "compile_authority",
        "decide_authority",
        "decide_product_goal_review",
        "decide_specification",
        "decide_vision_review",
        "fulfill_product_goal",
        "record_discovery_artifact",
        "record_backlog_draft",
        "record_product_goal_interview_turn",
        "record_roadmap_draft",
        "record_specification_candidate",
        "record_sprint_plan",
        "record_story_draft",
        "record_vision_interview_turn",
        "repair_authority",
    }
)


class _ReadProjection(Protocol):
    """Non-routing read methods used by CLI handlers."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

    def repository_status(self, *, project_id: int) -> JsonObject: ...

    def vision_status(self, *, project_id: int) -> JsonObject: ...

    def product_goal_status(self, *, project_id: int) -> JsonObject: ...

    def discovery_status(self, *, project_id: int) -> JsonObject: ...

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

    def transition(self, request: TransitionRequest) -> TransitionResult:
        """Apply one typed transition."""
        ...

    def create_project(self, request: CreateProjectCommand) -> TransitionResult: ...

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

    def record_discovery(
        self,
        request: DiscoveryArtifactRequest,
    ) -> TransitionResult: ...

    def record_specification_candidate(
        self,
        request: SpecificationCandidateRequest,
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

    def repair_authority(self, request: AuthorityRepairRequest) -> TransitionResult: ...

    def generate_backlog(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_roadmap(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_story(self, request: DeliveryActionRequest) -> TransitionResult: ...

    def generate_sprint(self, request: SprintPlanningRequest) -> TransitionResult: ...


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


def _add_transition_leaf(
    parser: argparse.ArgumentParser,
    *,
    request_kind: str,
) -> None:
    _add_mutation_metadata(parser)
    parser.add_argument("--instance-key")
    parser.set_defaults(request_kind=request_kind)
    parser.add_argument("--request-file", required=True)
    parser.set_defaults(command_handler=_run_transition)


def _install_transition_commands(
    subparsers: argparse._SubParsersAction,
    *,
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
    parsers: dict[tuple[str, ...], argparse.ArgumentParser],
) -> None:
    branches[()] = subparsers
    for request_kind, full_prefix in COMMAND_PREFIXES.items():
        if request_kind in _SEMANTIC_REQUEST_KINDS:
            continue
        parts = full_prefix[1:]
        if parts[0] == "project":
            continue
        parent: tuple[str, ...] = ()
        for index, part in enumerate(parts):
            current = (*parent, part)
            parser = parsers.get(current)
            if parser is None:
                parser = branches[parent].add_parser(part)
                parsers[current] = parser
            if index == len(parts) - 1:
                _add_transition_leaf(parser, request_kind=request_kind)
                continue
            if current not in branches:
                branches[current] = parser.add_subparsers(
                    dest=f"command_{index}_{part}",
                    required=True,
                )
            parent = current


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
        "discovery",
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
        ("discovery", _discovery_status),
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


def _install_lifecycle_mutations(
    branches: dict[tuple[str, ...], argparse._SubParsersAction],
) -> None:
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

    discovery_record = _semantic_leaf(
        branches[("discovery",)], "record", _discovery_record
    )
    discovery_record.add_argument("--file", required=True)
    specification_record = _semantic_leaf(
        branches[("specification",)], "record", _specification_record
    )
    specification_record.add_argument("--file", required=True)
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
    _semantic_leaf(branches[("authority",)], "repair", _authority_repair)

    for group, handler in (
        ("backlog", _backlog_generate),
        ("roadmap", _roadmap_generate),
        ("story", _story_generate),
    ):
        generate = _semantic_leaf(branches[(group,)], "generate", handler)
        generate.add_argument("--instance-key")

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

    _install_transition_commands(
        subparsers,
        branches=branches,
        parsers=parsers,
    )
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


def _discovery_status(args: argparse.Namespace, application: _Application) -> int:
    return _emit_read(application.reads.discovery_status(project_id=args.project_id))


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


def _discovery_record(args: argparse.Namespace, application: _Application) -> int:
    return _emit_result(
        application.record_discovery(
            DiscoveryArtifactRequest(
                project_id=args.project_id,
                canonical_content=_read_json_object(args.file),
                idempotency_key=args.idempotency_key,
                actor=args.actor,
                correlation_id=args.correlation_id,
            )
        )
    )


def _specification_record(
    args: argparse.Namespace,
    application: _Application,
) -> int:
    return _emit_result(
        application.record_specification_candidate(
            SpecificationCandidateRequest(
                project_id=args.project_id,
                canonical_content=_read_json_object(args.file),
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


def _current_decision(
    application: _Application,
    args: argparse.Namespace,
) -> tuple[WorkflowPosition, NodeDecision | None]:
    position = application.position(project_id=args.project_id)
    instance_key = getattr(args, "instance_key", None)
    candidates = tuple(
        decision
        for decision in position.decisions
        if decision.request_kind == args.request_kind
        and decision.category in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
        and (instance_key is None or decision.instance_key == instance_key)
    )
    return position, candidates[0] if len(candidates) == 1 else None


def _unavailable_result(
    position: WorkflowPosition,
    request_kind: str,
) -> TransitionResult:
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
            message=f"No unique {request_kind} transition is currently available.",
        ),
    )


def _guarded_payload(
    args: argparse.Namespace,
    payload: JsonObject,
    position: WorkflowPosition,
    decision: NodeDecision,
) -> JsonObject:
    forbidden = {
        "actor",
        "correlation_id",
        "decision_fingerprint",
        "fact_fingerprint",
        "graph_version",
        "idempotency_key",
        "instance_key",
        "kind",
        "project_id",
    }
    if forbidden.intersection(payload):
        message = "Request files must contain semantic fields only."
        raise ValueError(message)
    guarded = dict(payload)
    guarded.update(
        {
            "kind": args.request_kind,
            "project_id": args.project_id,
            "graph_version": position.graph_version,
            "fact_fingerprint": position.fact_fingerprint,
            "decision_fingerprint": decision.decision_fingerprint,
            "instance_key": decision.instance_key,
            "idempotency_key": args.idempotency_key,
            "actor": args.actor,
            "correlation_id": args.correlation_id,
        }
    )
    return _JSON_OBJECT.validate_python(guarded)


def _run_transition(args: argparse.Namespace, application: _Application) -> int:
    position, decision = _current_decision(application, args)
    if decision is None:
        return _emit_result(_unavailable_result(position, args.request_kind))
    payload = _guarded_payload(
        args,
        _read_json_object(args.request_file),
        position,
        decision,
    )
    request = _TRANSITION_REQUEST.validate_python(payload)
    return _emit_result(application.transition(request))


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
