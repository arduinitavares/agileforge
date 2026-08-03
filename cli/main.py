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
    AGENTIC_REQUEST_KINDS,
    COMMAND_PREFIXES,
    ProjectShellArguments,
    build_open_project_shell_request,
    workflow_next,
    workflow_position,
)
from services.application import AgenticActionRequest, production_application
from utils.logging_config import configure_logging
from utils.model_config import get_model_id
from workflow.contracts import JsonObject, TransitionResult, WorkflowPosition
from workflow.requests import TransitionRequest

_JSON_OBJECT = TypeAdapter(JsonObject)
_TRANSITION_REQUEST = TypeAdapter(TransitionRequest)

_AGENTIC_REQUEST_NODES: dict[str, str] = {
    "record_brownfield_spec_draft": "onboarding.brownfield.curation",
    "compile_authority": "authority.compile",
    "repair_authority": "authority.repair",
    "record_vision_draft": "vision.generate",
    "record_backlog_draft": "backlog.generate",
    "record_roadmap_draft": "planning.roadmap.generate",
    "record_story_draft": "planning.story.generate",
    "record_sprint_plan": "planning.sprint.plan",
}
_AGENTIC_MODEL_KEYS: dict[str, str] = {
    "record_brownfield_spec_draft": "brownfield_curator",
    "compile_authority": "spec_authority_compiler",
    "repair_authority": "spec_authority_compiler",
    "record_vision_draft": "product_vision",
    "record_backlog_draft": "backlog_primer",
    "record_roadmap_draft": "roadmap_builder",
    "record_story_draft": "user_story_writer",
    "record_sprint_plan": "sprint_planner",
}


class _ReadProjection(Protocol):
    """Non-routing read methods used by CLI handlers."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

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

    def run_agentic_action(
        self,
        request: AgenticActionRequest,
    ) -> TransitionResult:
        """Run one exact graph-selected ADK action."""
        ...


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


def _add_position_guards(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--graph-version", required=True)
    parser.add_argument("--expected-fact-fingerprint", required=True)
    parser.add_argument("--expected-decision-fingerprint", required=True)
    parser.add_argument("--instance-key")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--changed-by", required=True)
    parser.add_argument("--correlation-id")


def _add_transition_leaf(
    parser: argparse.ArgumentParser,
    *,
    request_kind: str,
) -> None:
    _add_position_guards(parser)
    parser.set_defaults(request_kind=request_kind)
    if request_kind in AGENTIC_REQUEST_KINDS:
        parser.add_argument("--input-file", required=True)
        parser.add_argument("--model-id")
        parser.set_defaults(command_handler=_run_agentic)
        return
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
        ("vision", "vision.generate"),
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
    for group in ("authority", "vision", "backlog", "roadmap", "story", "sprint"):
        group_parser = subparsers.add_parser(group)
        group_sub = group_parser.add_subparsers(
            dest=f"{group}_action",
            required=True,
        )
        parsers[(group,)] = group_parser
        branches[(group,)] = group_sub
    _install_authority_reads(branches[("authority",)])
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


def build_parser() -> argparse.ArgumentParser:
    """Build the graph-backed command tree."""
    parser = _ArgumentParser(prog="agileforge")
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
    create.add_argument("--origin", choices=("greenfield", "brownfield"), required=True)
    create.add_argument("--idempotency-key", required=True)
    create.add_argument("--changed-by", required=True)
    create.add_argument("--correlation-id")
    create.set_defaults(command_handler=_open_project_shell)
    abandon = project_sub.add_parser("abandon")
    _add_transition_leaf(abandon, request_kind="abandon_project_shell")

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


def _open_project_shell(args: argparse.Namespace, application: _Application) -> int:
    origin = cast("Literal['greenfield', 'brownfield']", args.origin)
    if origin not in {"greenfield", "brownfield"}:
        raise ValueError(origin)
    request = build_open_project_shell_request(
        ProjectShellArguments(
            name=args.name,
            origin=origin,
            idempotency_key=args.idempotency_key,
            changed_by=args.changed_by,
            correlation_id=args.correlation_id,
        )
    )
    return _emit_result(application.transition(request))


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


def _guarded_payload(args: argparse.Namespace, payload: JsonObject) -> JsonObject:
    guarded = dict(payload)
    guarded.update(
        {
            "kind": args.request_kind,
            "project_id": args.project_id,
            "graph_version": args.graph_version,
            "fact_fingerprint": args.expected_fact_fingerprint,
            "decision_fingerprint": args.expected_decision_fingerprint,
            "instance_key": args.instance_key,
            "idempotency_key": args.idempotency_key,
            "actor": args.changed_by,
            "correlation_id": args.correlation_id,
        }
    )
    return _JSON_OBJECT.validate_python(guarded)


def _run_transition(args: argparse.Namespace, application: _Application) -> int:
    payload = _guarded_payload(args, _read_json_object(args.request_file))
    request = _TRANSITION_REQUEST.validate_python(payload)
    return _emit_result(application.transition(request))


def _run_agentic(args: argparse.Namespace, application: _Application) -> int:
    request_kind = cast("str", args.request_kind)
    model_id = args.model_id or get_model_id(_AGENTIC_MODEL_KEYS[request_kind])
    result = application.run_agentic_action(
        AgenticActionRequest(
            project_id=args.project_id,
            graph_version=args.graph_version,
            fact_fingerprint=args.expected_fact_fingerprint,
            decision_fingerprint=args.expected_decision_fingerprint,
            node_id=_AGENTIC_REQUEST_NODES[request_kind],
            instance_key=args.instance_key,
            input_payload=_read_json_object(args.input_file),
            model_id=model_id,
            idempotency_key=args.idempotency_key,
            actor=args.changed_by,
            correlation_id=args.correlation_id,
        )
    )
    return _emit_result(result)


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
