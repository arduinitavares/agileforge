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


class _Application(Protocol):
    """Application methods used by CLI handlers."""

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


def _install_transition_commands(subparsers: argparse._SubParsersAction) -> None:
    branches: dict[tuple[str, ...], argparse._SubParsersAction] = {(): subparsers}
    parsers: dict[tuple[str, ...], argparse.ArgumentParser] = {}
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


def build_parser() -> argparse.ArgumentParser:
    """Build the graph-backed command tree."""
    parser = _ArgumentParser(prog="agileforge")
    subparsers = parser.add_subparsers(
        dest="group",
        required=True,
        parser_class=_ArgumentParser,
    )

    project = subparsers.add_parser("project")
    project_sub = project.add_subparsers(dest="project_action", required=True)
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

    _install_transition_commands(subparsers)
    return parser


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
