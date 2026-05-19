"""Agent workbench CLI transport."""

from __future__ import annotations

import argparse
import importlib
import io
import json
import sys
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import NoReturn, Protocol, TypedDict, cast
from uuid import uuid4

from pydantic import ValidationError

from services.agent_workbench.authority_decision import (
    AuthorityAcceptRequest,
    AuthorityRejectRequest,
    IncompleteReviewOverride,
)
from services.agent_workbench.envelope import (
    WorkbenchError,
    WorkbenchWarning,
    error_envelope,
    success_envelope,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    export_agileforge_spec_schema,
    render_markdown,
    rendered_markdown_hash,
)
from utils.logging_config import configure_logging

DEFAULT_CONTEXT_PHASE: str = "overview"
INVALID_COMMAND_EXIT_CODE: int = 2
COMMAND_EXCEPTION_EXIT_CODE: int = 1
HELP_DESCRIPTION: str = (
    "AgileForge agent-facing CLI for workflow inspection and guarded mutations."
)
HELP_EPILOG: str = (
    """\
Examples:
  agileforge project list
  agileforge status --project-id 1
  agileforge workflow state --project-id 1
  agileforge authority status --project-id 1
  agileforge authority review --project-id 1
  agileforge authority accept --project-id 1 --review-token <review_token>
  agileforge authority reject --project-id 1 --review-token <review_token> """
    """--reason "..."
  agileforge sprint candidates --project-id 1
  agileforge context pack --project-id 1 --phase sprint-planning
"""
)
type JsonObject = dict[str, object]
type JsonList = list[object]
CommandResult = tuple[str, JsonObject]
CommandHandler = Callable[[argparse.Namespace, "_Application"], CommandResult]
INCOMPLETE_REVIEW_OVERRIDE_PARTS = 3


class _AuthorityRequestKwargs(TypedDict):
    """Typed common kwargs for authority decision request models."""

    project_id: int
    review_token: str | None
    pending_authority_id: int | None
    expected_authority_fingerprint: str | None
    expected_source_spec_hash: str | None
    expected_disk_spec_hash: str | None
    expected_resolved_spec_path: str | None
    expected_state: str | None
    expected_setup_status: str | None
    expected_content_included: bool | None
    expected_omission_assessment: str | None
    expected_coverage_summary_fingerprint: str | None
    idempotency_key: str | None
    changed_by: str | None
    actor_mode: str


AUTHORITY_EXPLICIT_GUARD_FIELDS: tuple[str, ...] = (
    "pending_authority_id",
    "expected_authority_fingerprint",
    "expected_source_spec_hash",
    "expected_disk_spec_hash",
    "expected_resolved_spec_path",
    "expected_state",
    "expected_setup_status",
)
AUTHORITY_COMPLETENESS_GUARD_FIELDS: tuple[str, ...] = (
    "expected_content_included",
    "expected_omission_assessment",
    "expected_coverage_summary_fingerprint",
)
AUTHORITY_ALL_GUARD_FIELDS: tuple[str, ...] = (
    *AUTHORITY_EXPLICIT_GUARD_FIELDS,
    *AUTHORITY_COMPLETENESS_GUARD_FIELDS,
)


class _CliParseError(Exception):
    """Raised when argparse rejects normal command input."""


class _WorkbenchArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that lets main emit JSON for parse errors."""

    def error(self, message: str) -> NoReturn:
        """Raise instead of writing argparse error text to stderr."""
        raise _CliParseError(message)


class _Application(Protocol):
    """Application methods exposed to the CLI transport."""

    def project_list(self) -> JsonObject:
        """Return project list projection."""
        ...

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return project detail projection."""
        ...

    def project_create(  # noqa: PLR0913
        self,
        *,
        name: str,
        spec_file: str,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> JsonObject:
        """Create a project through the guarded mutation facade."""
        ...

    def project_setup_retry(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_file: str,
        expected_state: str,
        expected_context_fingerprint: str,
        recovery_mutation_event_id: int | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> JsonObject:
        """Retry interrupted project setup through the guarded mutation facade."""
        ...

    def workflow_state(self, *, project_id: int) -> JsonObject:
        """Return workflow state projection."""
        ...

    def workflow_next(self, *, project_id: int) -> JsonObject:
        """Return next workflow commands projection."""
        ...

    def authority_status(self, *, project_id: int) -> JsonObject:
        """Return authority status projection."""
        ...

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject:
        """Return authority invariants projection."""
        ...

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> JsonObject:
        """Return a pending authority review packet."""
        ...

    def authority_accept(self, request: AuthorityAcceptRequest) -> JsonObject:
        """Accept pending authority from a guarded request."""
        ...

    def authority_reject(self, request: AuthorityRejectRequest) -> JsonObject:
        """Reject pending authority from a guarded request."""
        ...

    def story_show(self, *, story_id: int) -> JsonObject:
        """Return story detail projection."""
        ...

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        """Return sprint candidate projection."""
        ...

    def context_pack(
        self,
        *,
        project_id: int,
        phase: str = DEFAULT_CONTEXT_PHASE,
    ) -> JsonObject:
        """Return a context pack projection."""
        ...

    def status(self, *, project_id: int) -> JsonObject:
        """Return project status projection."""
        ...

    def doctor(self) -> JsonObject:
        """Return local diagnostics."""
        ...

    def schema_check(self) -> JsonObject:
        """Return schema readiness diagnostics."""
        ...

    def capabilities(self) -> JsonObject:
        """Return installed command capabilities."""
        ...

    def command_schema(self, *, command_name: str) -> JsonObject:
        """Return one command schema."""
        ...

    def mutation_show(self, *, mutation_event_id: int) -> JsonObject:
        """Return one mutation ledger event."""
        ...

    def mutation_list(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
    ) -> JsonObject:
        """Return mutation ledger events."""
        ...

    def mutation_resume(
        self,
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> JsonObject:
        """Acquire a recovery lease for a mutation event."""
        ...


def _print_json(payload: JsonObject) -> None:
    """Write one JSON envelope to stdout."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    sys.stdout.write("\n")


def _coerce_exit_code(value: object, *, default: int = 1) -> int:
    """Return a process exit code from a structured error value."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.removeprefix("-").isdigit():
            return int(stripped)
    return default


def _as_mapping(value: object) -> Mapping[object, object] | None:
    """Return a typed mapping view for JSON-like dictionaries."""
    if not isinstance(value, dict):
        return None
    return cast("Mapping[object, object]", value)


def _exit_code(result: JsonObject) -> int:
    """Return the process exit code for an envelope."""
    if result.get("ok") is True:
        return 0

    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        first_mapping = _as_mapping(first)
        if first_mapping is not None:
            return _coerce_exit_code(first_mapping.get("exit_code"))

    return 1


def _string_list(value: object) -> list[str]:
    """Return a list of strings from a structured envelope field."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _details_dict(value: object) -> dict[str, object]:
    """Return a details mapping from a structured envelope field."""
    mapping = _as_mapping(value)
    if mapping is None:
        return {}
    return {str(key): detail for key, detail in mapping.items()}


def _warning_from_value(value: object) -> WorkbenchWarning:
    """Return a WorkbenchWarning from a raw service warning."""
    if isinstance(value, WorkbenchWarning):
        return value
    mapping = _as_mapping(value)
    if mapping is not None:
        return WorkbenchWarning(
            code=str(mapping.get("code", "COMMAND_WARNING")),
            message=str(mapping.get("message", "Command warning.")),
            details=_details_dict(mapping.get("details")),
            remediation=_string_list(mapping.get("remediation")),
        )
    return WorkbenchWarning(
        code="COMMAND_WARNING",
        message=str(value),
    )


def _warnings_from_result(result: JsonObject) -> list[WorkbenchWarning]:
    """Return structured warnings from a service result."""
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [_warning_from_value(warning) for warning in warnings]


def _error_from_value(value: object) -> WorkbenchError:
    """Return a WorkbenchError from a raw service error."""
    if isinstance(value, WorkbenchError):
        return value
    mapping = _as_mapping(value)
    if mapping is not None:
        return WorkbenchError(
            code=str(mapping.get("code", "COMMAND_FAILED")),
            message=str(mapping.get("message", "Command failed.")),
            details=_details_dict(mapping.get("details")),
            remediation=_string_list(mapping.get("remediation")),
            exit_code=_coerce_exit_code(mapping.get("exit_code")),
            retryable=mapping.get("retryable") is True,
        )
    return WorkbenchError(
        code="COMMAND_FAILED",
        message=str(value),
        exit_code=1,
        retryable=False,
    )


def _errors_from_result(result: JsonObject) -> list[WorkbenchError]:
    """Return structured errors from a service result."""
    errors = result.get("errors")
    if not isinstance(errors, list):
        return []
    return [_error_from_value(error) for error in errors]


def _success_data(result: JsonObject) -> JsonObject | JsonList:
    """Return success data in an envelope-compatible shape."""
    data = result.get("data")
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    if isinstance(data, list):
        return cast("JsonList", data)
    return {}


def _source_fingerprint(result: JsonObject) -> str | None:
    """Return source fingerprint metadata from successful result data."""
    data = result.get("data")
    data_mapping = _as_mapping(data)
    if data_mapping is None:
        return None
    source_fingerprint = data_mapping.get("source_fingerprint")
    if isinstance(source_fingerprint, str):
        return source_fingerprint
    return None


def _wrap(command: str, result: JsonObject) -> JsonObject:
    """Wrap a service result in a stable CLI envelope when needed."""
    if "meta" in result:
        return result

    warnings = _warnings_from_result(result)
    if result.get("ok") is True:
        return success_envelope(
            command=command,
            data=_success_data(result),
            warnings=warnings,
            source_fingerprint=_source_fingerprint(result),
        )

    errors = _errors_from_result(result)
    if errors:
        envelope = error_envelope(
            command=command,
            error=errors[0],
            warnings=warnings,
        )
        data = result.get("data")
        if isinstance(data, dict):
            envelope["data"] = {str(key): value for key, value in data.items()}
        if len(errors) > 1:
            envelope["errors"] = [error.to_dict() for error in errors]
        return envelope

    return error_envelope(
        command=command,
        error=WorkbenchError(
            code="COMMAND_FAILED",
            message="Command failed without structured error details.",
            exit_code=1,
            retryable=False,
        ),
        warnings=warnings,
    )


def _plain_text_output(args: argparse.Namespace, result: JsonObject) -> str | None:
    """Return plain text for commands that explicitly requested text output."""
    if (
        getattr(args, "group", None) != "authority"
        or getattr(args, "action", None) != "review"
        or getattr(args, "format", None) != "text"
        or result.get("ok") is not True
    ):
        return None
    data = _as_mapping(result.get("data"))
    if data is None:
        return None
    text = data.get("text")
    return text if isinstance(text, str) else None


def _parse_error_envelope(message: str, argv: list[str] | None) -> JsonObject:
    """Return a structured envelope for invalid command input."""
    parsed_argv = list(argv) if argv is not None else sys.argv[1:]
    return error_envelope(
        command="agileforge",
        error=WorkbenchError(
            code="INVALID_COMMAND",
            message=message,
            details={"argv": parsed_argv},
            remediation=["Run agileforge --help."],
            exit_code=INVALID_COMMAND_EXIT_CODE,
            retryable=False,
        ),
    )


def _parse_bool_token(value: str) -> bool:
    """Parse an explicit true/false CLI token."""
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    msg = "expected one of: true, false"
    raise argparse.ArgumentTypeError(msg)


def _add_authority_guard_args(command: argparse.ArgumentParser) -> None:
    """Add explicit authority review guard arguments to a parser."""
    command.add_argument("--pending-authority-id", type=int)
    command.add_argument("--expected-authority-fingerprint")
    command.add_argument("--expected-source-spec-hash")
    command.add_argument("--expected-disk-spec-hash")
    command.add_argument("--expected-resolved-spec-path")
    command.add_argument("--expected-state")
    command.add_argument("--expected-setup-status")
    command.add_argument("--expected-content-included", type=_parse_bool_token)
    command.add_argument(
        "--expected-omission-assessment",
        choices=("complete", "incomplete"),
    )
    command.add_argument("--expected-coverage-summary-fingerprint")


def _exception_envelope(exc: Exception) -> JsonObject:
    """Return a structured envelope for unexpected command exceptions."""
    return error_envelope(
        command="agileforge",
        error=WorkbenchError(
            code="COMMAND_EXCEPTION",
            message=str(exc) or "Command failed with an unexpected exception.",
            details={"exception_type": type(exc).__name__},
            remediation=[],
            exit_code=COMMAND_EXCEPTION_EXIT_CODE,
            retryable=False,
        ),
    )


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    """Build the top-level CLI parser."""
    parser = _WorkbenchArgumentParser(
        prog="agileforge",
        description=HELP_DESCRIPTION,
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(
        dest="group",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )

    project = subparsers.add_parser(
        "project",
        help="List and inspect AgileForge projects.",
    )
    project_sub = project.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    project_list = project_sub.add_parser("list", help="List projects.")
    project_list.set_defaults(command_handler=_project_list)
    project_show = project_sub.add_parser("show", help="Show one project.")
    project_show.add_argument("--project-id", type=int, required=True)
    project_show.set_defaults(command_handler=_project_show)
    project_create = project_sub.add_parser("create", help="Create a project.")
    project_create.add_argument("--name", required=True)
    project_create.add_argument("--spec-file", required=True)
    project_create.add_argument("--idempotency-key")
    project_create.add_argument("--dry-run", action="store_true")
    project_create.add_argument("--dry-run-id")
    project_create.add_argument("--correlation-id")
    project_create.add_argument("--changed-by", default="cli-agent")
    project_create.set_defaults(command_handler=_project_create)
    project_setup = project_sub.add_parser("setup", help="Retry project setup.")
    project_setup_sub = project_setup.add_subparsers(
        dest="setup_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    project_setup_retry = project_setup_sub.add_parser("retry", help="Retry setup.")
    project_setup_retry.add_argument("--project-id", type=int, required=True)
    project_setup_retry.add_argument("--spec-file", required=True)
    project_setup_retry.add_argument("--expected-state", required=True)
    project_setup_retry.add_argument("--expected-context-fingerprint", required=True)
    project_setup_retry.add_argument("--recovery-mutation-event-id", type=int)
    project_setup_retry.add_argument("--idempotency-key")
    project_setup_retry.add_argument("--dry-run", action="store_true")
    project_setup_retry.add_argument("--dry-run-id")
    project_setup_retry.add_argument("--correlation-id")
    project_setup_retry.add_argument("--changed-by", default="cli-agent")
    project_setup_retry.set_defaults(command_handler=_project_setup_retry)

    workflow = subparsers.add_parser(
        "workflow",
        help="Inspect workflow state and next installed commands.",
    )
    workflow_sub = workflow.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    workflow_state = workflow_sub.add_parser("state", help="Show workflow state.")
    workflow_state.add_argument("--project-id", type=int, required=True)
    workflow_state.set_defaults(command_handler=_workflow_state)
    workflow_next = workflow_sub.add_parser("next", help="Show next commands.")
    workflow_next.add_argument("--project-id", type=int, required=True)
    workflow_next.set_defaults(command_handler=_workflow_next)

    authority = subparsers.add_parser(
        "authority",
        help="Inspect accepted Spec Authority.",
    )
    authority_sub = authority.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    authority_status = authority_sub.add_parser("status", help="Show authority status.")
    authority_status.add_argument("--project-id", type=int, required=True)
    authority_status.set_defaults(command_handler=_authority_status)
    authority_invariants = authority_sub.add_parser(
        "invariants",
        help="List authority invariants.",
    )
    authority_invariants.add_argument("--project-id", type=int, required=True)
    authority_invariants.add_argument("--spec-version-id", type=int)
    authority_invariants.set_defaults(command_handler=_authority_invariants)
    authority_review = authority_sub.add_parser(
        "review",
        help="Build a pending authority review packet.",
    )
    authority_review.add_argument("--project-id", type=int, required=True)
    authority_review.add_argument(
        "--include-spec",
        choices=("auto", "full", "summary"),
        default="auto",
    )
    authority_review.add_argument("--format", choices=("json", "text"), default="json")
    authority_review.set_defaults(command_handler=_authority_review)
    authority_accept = authority_sub.add_parser(
        "accept",
        help="Accept reviewed pending authority.",
    )
    authority_accept.add_argument("--project-id", type=int, required=True)
    authority_accept.add_argument("--review-token")
    _add_authority_guard_args(authority_accept)
    authority_accept.add_argument("--idempotency-key")
    authority_accept.add_argument("--allow-incomplete-review", action="store_true")
    authority_accept.add_argument("--incomplete-review-rationale")
    authority_accept.add_argument(
        "--incomplete-review-override",
        action="append",
        default=[],
        metavar="CANDIDATE_ID:FINDING_CODE:RATIONALE",
    )
    authority_accept.add_argument("--changed-by")
    authority_accept.set_defaults(command_handler=_authority_accept)
    authority_reject = authority_sub.add_parser(
        "reject",
        help="Reject reviewed pending authority.",
    )
    authority_reject.add_argument("--project-id", type=int, required=True)
    authority_reject.add_argument("--review-token")
    _add_authority_guard_args(authority_reject)
    authority_reject.add_argument("--reason")
    authority_reject.add_argument("--idempotency-key")
    authority_reject.add_argument("--changed-by")
    authority_reject.set_defaults(command_handler=_authority_reject)

    story = subparsers.add_parser("story", help="Inspect user stories.")
    story_sub = story.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    story_show = story_sub.add_parser("show", help="Show one story.")
    story_show.add_argument("--story-id", type=int, required=True)
    story_show.set_defaults(command_handler=_story_show)

    sprint = subparsers.add_parser("sprint", help="Inspect sprint planning inputs.")
    sprint_sub = sprint.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    sprint_candidates = sprint_sub.add_parser(
        "candidates",
        help="List sprint candidate stories.",
    )
    sprint_candidates.add_argument("--project-id", type=int, required=True)
    sprint_candidates.set_defaults(command_handler=_sprint_candidates)

    context = subparsers.add_parser("context", help="Build bounded agent context.")
    context_sub = context.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    context_pack = context_sub.add_parser("pack", help="Build a context pack.")
    context_pack.add_argument("--project-id", type=int, required=True)
    context_pack.add_argument("--phase", default=DEFAULT_CONTEXT_PHASE)
    context_pack.set_defaults(command_handler=_context_pack)

    status = subparsers.add_parser("status", help="Show project orientation status.")
    status.add_argument("--project-id", type=int, required=True)
    status.set_defaults(command_handler=_status)

    doctor = subparsers.add_parser("doctor", help="Run CLI diagnostics.")
    doctor.set_defaults(command_handler=_doctor)

    capabilities = subparsers.add_parser(
        "capabilities",
        help="Show installed command capabilities.",
    )
    capabilities.set_defaults(command_handler=_capabilities)

    schema = subparsers.add_parser("schema", help="Inspect CLI schemas.")
    schema_sub = schema.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    schema_check = schema_sub.add_parser("check", help="Check storage schema.")
    schema_check.set_defaults(command_handler=_schema_check)

    spec = subparsers.add_parser("spec", help="Inspect AgileForge spec artifacts.")
    spec_sub = spec.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    spec_profile = spec_sub.add_parser("profile", help="Inspect spec profile data.")
    spec_profile_sub = spec_profile.add_subparsers(
        dest="profile_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    spec_profile_schema = spec_profile_sub.add_parser(
        "schema",
        help="Export the AgileForge spec profile JSON Schema.",
    )
    spec_profile_schema.set_defaults(command_handler=_spec_profile_schema)
    spec_profile_validate = spec_profile_sub.add_parser(
        "validate",
        help="Validate an AgileForge spec profile JSON file.",
    )
    spec_profile_validate.add_argument("--spec-file", required=True)
    spec_profile_validate.add_argument("--render-md")
    spec_profile_validate.set_defaults(command_handler=_spec_profile_validate)

    command = subparsers.add_parser("command", help="Inspect command contracts.")
    command_sub = command.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    command_schema = command_sub.add_parser("schema", help="Show command schema.")
    command_schema.add_argument("command_name")
    command_schema.set_defaults(command_handler=_command_schema)

    mutation = subparsers.add_parser("mutation", help="Inspect mutation ledger.")
    mutation_sub = mutation.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    mutation_show = mutation_sub.add_parser("show", help="Show one mutation event.")
    mutation_show.add_argument("--mutation-event-id", type=int, required=True)
    mutation_show.set_defaults(command_handler=_mutation_show)
    mutation_list = mutation_sub.add_parser("list", help="List mutation events.")
    mutation_list.add_argument("--project-id", type=int)
    mutation_list.add_argument("--status")
    mutation_list.set_defaults(command_handler=_mutation_list)
    mutation_resume = mutation_sub.add_parser(
        "resume",
        help="Resume a recovery-required mutation event.",
    )
    mutation_resume.add_argument("--mutation-event-id", type=int, required=True)
    mutation_resume.add_argument("--correlation-id")
    mutation_resume.set_defaults(command_handler=_mutation_resume)
    return parser


def _project_list(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route project list to the application facade."""
    return "agileforge project list", application.project_list()


def _project_show(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route project show to the application facade."""
    return "agileforge project show", application.project_show(
        project_id=args.project_id
    )


def _mutation_arg_error(command: str, error: WorkbenchError) -> CommandResult:
    """Return a command result for mutation argument validation failures."""
    return command, {
        "ok": False,
        "data": None,
        "warnings": [],
        "errors": [error.to_dict()],
    }


def _validate_mutation_idempotency_args(
    args: argparse.Namespace,
) -> WorkbenchError | None:
    """Validate dry-run/idempotency flag combinations for mutations."""
    if args.dry_run and args.idempotency_key:
        return WorkbenchError(
            code="INVALID_COMMAND",
            message="--idempotency-key is not allowed with --dry-run.",
            details={"idempotency_key": args.idempotency_key},
            remediation=["Use --dry-run-id for dry-run tracing."],
            exit_code=INVALID_COMMAND_EXIT_CODE,
            retryable=False,
        )
    if not args.dry_run and not args.idempotency_key:
        return WorkbenchError(
            code="INVALID_COMMAND",
            message="--idempotency-key is required for non-dry-run mutations.",
            details={},
            remediation=["Pass --idempotency-key or use --dry-run."],
            exit_code=INVALID_COMMAND_EXIT_CODE,
            retryable=False,
        )
    return None


def _project_create(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route project create to the application facade."""
    command = "agileforge project create"
    validation_error = _validate_mutation_idempotency_args(args)
    if validation_error is not None:
        return _mutation_arg_error(command, validation_error)
    return command, application.project_create(
        name=args.name,
        spec_file=args.spec_file,
        idempotency_key=args.idempotency_key,
        dry_run=args.dry_run,
        dry_run_id=args.dry_run_id,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _project_setup_retry(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route project setup retry to the application facade."""
    command = "agileforge project setup retry"
    validation_error = _validate_mutation_idempotency_args(args)
    if validation_error is not None:
        return _mutation_arg_error(command, validation_error)
    return command, application.project_setup_retry(
        project_id=args.project_id,
        spec_file=args.spec_file,
        expected_state=args.expected_state,
        expected_context_fingerprint=args.expected_context_fingerprint,
        recovery_mutation_event_id=args.recovery_mutation_event_id,
        idempotency_key=args.idempotency_key,
        dry_run=args.dry_run,
        dry_run_id=args.dry_run_id,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _workflow_state(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route workflow state to the application facade."""
    return "agileforge workflow state", application.workflow_state(
        project_id=args.project_id
    )


def _workflow_next(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route workflow next to the application facade."""
    return "agileforge workflow next", application.workflow_next(
        project_id=args.project_id
    )


def _authority_status(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority status to the application facade."""
    return "agileforge authority status", application.authority_status(
        project_id=args.project_id
    )


def _authority_invariants(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority invariants to the application facade."""
    return "agileforge authority invariants", application.authority_invariants(
        project_id=args.project_id,
        spec_version_id=args.spec_version_id,
    )


def _authority_review(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority review to the application facade."""
    return "agileforge authority review", application.authority_review(
        project_id=args.project_id,
        include_spec=args.include_spec,
        output_format=args.format,
    )


def _invalid_command(
    command: str,
    message: str,
    *,
    details: dict[str, object] | None = None,
    remediation: list[str] | None = None,
) -> CommandResult:
    """Return a structured invalid-command result."""
    return _mutation_arg_error(
        command,
        WorkbenchError(
            code=ErrorCode.INVALID_COMMAND.value,
            message=message,
            details=details or {},
            remediation=remediation or ["Run agileforge authority --help."],
            exit_code=INVALID_COMMAND_EXIT_CODE,
            retryable=False,
        ),
    )


def _authority_review_required(command: str) -> CommandResult:
    """Return a missing-review-token result for non-interactive decisions."""
    return _mutation_arg_error(
        command,
        workbench_error(
            ErrorCode.AUTHORITY_REVIEW_REQUIRED,
            message="Run authority review first and pass --review-token.",
            remediation=[
                "Run agileforge authority review --project-id <id>.",
                "Pass --review-token, or run from a TTY for interactive review.",
            ],
        ),
    )


def _has_explicit_authority_args(args: argparse.Namespace) -> bool:
    """Return whether explicit decision mode appears to be requested."""
    return bool(args.idempotency_key) or any(
        getattr(args, field_name) is not None
        for field_name in AUTHORITY_ALL_GUARD_FIELDS
    )


def _missing_authority_guards(
    args: argparse.Namespace,
    *,
    require_completeness: bool,
) -> list[str]:
    """Return required explicit guard fields missing from parsed args."""
    fields = list(AUTHORITY_EXPLICIT_GUARD_FIELDS)
    if require_completeness:
        fields.extend(AUTHORITY_COMPLETENESS_GUARD_FIELDS)
    return [field_name for field_name in fields if getattr(args, field_name) is None]


def _authority_actor_mode(changed_by: str | None, *, token_mode: bool) -> str:
    """Return the actor mode implied by CLI decision input."""
    if not token_mode:
        return "cli-agent"
    if changed_by is None:
        return "cli-human"
    normalized = changed_by.lower()
    if "agent" in normalized or "bot" in normalized or "automation" in normalized:
        return "cli-agent"
    return "cli-human"


def _decision_idempotency_key(args: argparse.Namespace) -> str | None:
    """Return an explicit or generated idempotency key for token mode."""
    if args.idempotency_key:
        return cast("str", args.idempotency_key)
    if args.review_token:
        return f"human-token:{uuid4()}"
    return None


def _authority_request_kwargs(args: argparse.Namespace) -> _AuthorityRequestKwargs:
    """Return request keyword args common to accept/reject decisions."""
    return {
        "project_id": cast("int", args.project_id),
        "review_token": cast("str | None", args.review_token),
        "pending_authority_id": cast("int | None", args.pending_authority_id),
        "expected_authority_fingerprint": cast(
            "str | None",
            args.expected_authority_fingerprint,
        ),
        "expected_source_spec_hash": cast(
            "str | None",
            args.expected_source_spec_hash,
        ),
        "expected_disk_spec_hash": cast(
            "str | None",
            args.expected_disk_spec_hash,
        ),
        "expected_resolved_spec_path": cast(
            "str | None",
            args.expected_resolved_spec_path,
        ),
        "expected_state": cast("str | None", args.expected_state),
        "expected_setup_status": cast("str | None", args.expected_setup_status),
        "expected_content_included": cast(
            "bool | None",
            args.expected_content_included,
        ),
        "expected_omission_assessment": cast(
            "str | None",
            args.expected_omission_assessment,
        ),
        "expected_coverage_summary_fingerprint": cast(
            "str | None",
            args.expected_coverage_summary_fingerprint,
        ),
        "idempotency_key": _decision_idempotency_key(args),
        "changed_by": cast("str | None", args.changed_by),
        "actor_mode": _authority_actor_mode(
            cast("str | None", args.changed_by),
            token_mode=bool(cast("str | None", args.review_token)),
        ),
    }


def _authority_validation_failure(
    command: str,
    exc: ValidationError | ValueError,
) -> CommandResult:
    """Return a structured invalid-command result for request model errors."""
    return _invalid_command(
        command,
        "Invalid authority decision arguments.",
        details={"validation_error": str(exc)},
    )


def _validate_incomplete_override(args: argparse.Namespace) -> CommandResult | None:
    """Validate incomplete review override arguments."""
    overrides = cast("list[str]", args.incomplete_review_override or [])
    if not overrides:
        return None
    try:
        _parse_incomplete_review_overrides(overrides)
    except ValueError as exc:
        return _invalid_command(
            "agileforge authority accept",
            str(exc),
            details={"field": "incomplete_review_override"},
        )
    return None


def _parse_incomplete_review_overrides(
    raw_overrides: list[str],
) -> list[IncompleteReviewOverride]:
    """Parse repeated candidate-scoped incomplete review override flags."""
    parsed: list[IncompleteReviewOverride] = []
    for raw in raw_overrides:
        parts = raw.split(":", 2)
        if len(parts) != INCOMPLETE_REVIEW_OVERRIDE_PARTS or not all(
            part.strip() for part in parts
        ):
            msg = (
                "--incomplete-review-override must be "
                "<candidate_id>:<finding_code>:<rationale>."
            )
            raise ValueError(msg)
        candidate_id, finding_code, rationale = (part.strip() for part in parts)
        parsed.append(
            IncompleteReviewOverride(
                candidate_id=candidate_id,
                finding_code=finding_code,
                rationale=rationale,
            )
        )
    return parsed


def _validate_authority_explicit_args(
    args: argparse.Namespace,
    *,
    command: str,
    require_completeness: bool,
) -> CommandResult | None:
    """Validate explicit authority decision mode arguments."""
    missing = _missing_authority_guards(
        args,
        require_completeness=require_completeness,
    )
    if missing:
        return _invalid_command(
            command,
            "Explicit authority decision mode requires guard fields.",
            details={"missing": missing},
            remediation=["Pass --review-token or every required explicit guard."],
        )
    if not args.idempotency_key:
        return _invalid_command(
            command,
            "Explicit authority decision mode requires --idempotency-key.",
            details={"missing": ["idempotency_key"]},
        )
    return None


def _authority_accept(  # noqa: PLR0911
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority accept to the application facade."""
    command = "agileforge authority accept"
    validation_error = _validate_incomplete_override(args)
    if validation_error is not None:
        return validation_error

    if args.review_token:
        try:
            request = AuthorityAcceptRequest(
                **_authority_request_kwargs(args),
                allow_incomplete_review=args.allow_incomplete_review,
                incomplete_review_rationale=args.incomplete_review_rationale,
                incomplete_review_overrides=_parse_incomplete_review_overrides(
                    cast("list[str]", args.incomplete_review_override or [])
                ),
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_accept(request)

    if _has_explicit_authority_args(args):
        validation_error = _validate_authority_explicit_args(
            args,
            command=command,
            require_completeness=True,
        )
        if validation_error is not None:
            return validation_error
        try:
            request = AuthorityAcceptRequest(
                **_authority_request_kwargs(args),
                allow_incomplete_review=args.allow_incomplete_review,
                incomplete_review_rationale=args.incomplete_review_rationale,
                incomplete_review_overrides=_parse_incomplete_review_overrides(
                    cast("list[str]", args.incomplete_review_override or [])
                ),
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_accept(request)

    if not sys.stdin.isatty():
        return _authority_review_required(command)

    return _interactive_authority_accept(args, application)


def _authority_reject(  # noqa: PLR0911
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority reject to the application facade."""
    command = "agileforge authority reject"
    if args.review_token:
        if not _non_empty(args.reason):
            return _invalid_command(
                command,
                "--reason is required for authority reject.",
                details={"missing": ["reason"]},
            )
        try:
            request = AuthorityRejectRequest(
                **_authority_request_kwargs(args),
                reason=args.reason,
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_reject(request)

    if _has_explicit_authority_args(args):
        if not _non_empty(args.reason):
            return _invalid_command(
                command,
                "--reason is required for authority reject.",
                details={"missing": ["reason"]},
            )
        validation_error = _validate_authority_explicit_args(
            args,
            command=command,
            require_completeness=False,
        )
        if validation_error is not None:
            return validation_error
        try:
            request = AuthorityRejectRequest(
                **_authority_request_kwargs(args),
                reason=args.reason,
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_reject(request)

    if not sys.stdin.isatty():
        return _authority_review_required(command)

    return _interactive_authority_reject(args, application)


def _non_empty(value: object) -> bool:
    """Return whether a CLI string value is non-empty after trimming."""
    return isinstance(value, str) and bool(value.strip())


def _review_data(result: JsonObject) -> Mapping[object, object] | None:
    """Return the data mapping from a review result."""
    return _as_mapping(result.get("data"))


def _guard_tokens_from_review(result: JsonObject) -> Mapping[object, object] | None:
    """Return guard tokens from a review result."""
    data = _review_data(result)
    if data is None:
        return None
    return _as_mapping(data.get("guard_tokens"))


def _print_authority_review_summary(result: JsonObject) -> None:
    """Print a compact review summary for interactive decisions."""
    data = _review_data(result) or {}
    project = _as_mapping(data.get("project")) or {}
    spec = _as_mapping(data.get("spec")) or {}
    pending = _as_mapping(data.get("pending_authority")) or {}
    guards = _as_mapping(data.get("guard_tokens")) or {}
    sys.stderr.write(
        "\n".join(
            [
                "Authority review",
                f"  project_id: {project.get('project_id', '')}",
                f"  authority_id: {pending.get('authority_id', '')}",
                f"  spec_path: {spec.get('resolved_path', '')}",
                (
                    "  omission_assessment: "
                    f"{guards.get('expected_omission_assessment', '')}"
                ),
            ]
        )
        + "\n"
    )


def _args_from_review_token(
    args: argparse.Namespace,
    *,
    review_token: str,
    incomplete_review_rationale: str | None = None,
) -> argparse.Namespace:
    """Return a decision namespace populated for token-mode submission."""
    values = vars(args).copy()
    values["review_token"] = review_token
    values["idempotency_key"] = f"human-token:{uuid4()}"
    values["changed_by"] = args.changed_by
    if incomplete_review_rationale is not None:
        values["allow_incomplete_review"] = True
        values["incomplete_review_rationale"] = incomplete_review_rationale
    return argparse.Namespace(**values)


def _interactive_authority_accept(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Review and confirm authority acceptance in a TTY session."""
    command = "agileforge authority accept"
    review = application.authority_review(
        project_id=args.project_id,
        include_spec="auto",
        output_format="json",
    )
    if review.get("ok") is not True:
        return command, review
    guards = _guard_tokens_from_review(review)
    review_token = guards.get("review_token") if guards is not None else None
    if not isinstance(review_token, str):
        return _authority_review_required(command)
    guards = cast("Mapping[object, object]", guards)
    _print_authority_review_summary(review)
    omission = guards.get("expected_omission_assessment")
    phrase = (
        "ACCEPT AUTHORITY"
        if omission == "complete"
        else "ACCEPT INCOMPLETE AUTHORITY"
    )
    typed = _input_from_stderr(f'Type "{phrase}" to continue: ')
    if typed != phrase:
        return _invalid_command(
            command,
            "Authority acceptance confirmation did not match.",
            details={"required_phrase": phrase},
        )
    if omission != "complete":
        validation_error = _validate_incomplete_override(args)
        if validation_error is not None:
            return validation_error
    token_args = _args_from_review_token(
        args,
        review_token=review_token,
    )
    try:
        request = AuthorityAcceptRequest(
            **_authority_request_kwargs(token_args),
            allow_incomplete_review=token_args.allow_incomplete_review,
            incomplete_review_rationale=token_args.incomplete_review_rationale,
            incomplete_review_overrides=_parse_incomplete_review_overrides(
                cast("list[str]", token_args.incomplete_review_override or [])
            ),
        )
    except (ValidationError, ValueError) as exc:
        return _authority_validation_failure(command, exc)
    return command, application.authority_accept(request)


def _interactive_authority_reject(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Review and confirm authority rejection in a TTY session."""
    command = "agileforge authority reject"
    review = application.authority_review(
        project_id=args.project_id,
        include_spec="auto",
        output_format="json",
    )
    if review.get("ok") is not True:
        return command, review
    guards = _guard_tokens_from_review(review)
    review_token = guards.get("review_token") if guards is not None else None
    if not isinstance(review_token, str):
        return _authority_review_required(command)
    _print_authority_review_summary(review)
    reason = _input_from_stderr("Rejection reason: ").strip()
    if not reason:
        return _invalid_command(
            command,
            "Authority rejection requires a reason.",
            details={"missing": ["reason"]},
        )
    token_args = _args_from_review_token(args, review_token=review_token)
    try:
        request = AuthorityRejectRequest(
            **_authority_request_kwargs(token_args),
            reason=reason,
        )
    except (ValidationError, ValueError) as exc:
        return _authority_validation_failure(command, exc)
    return command, application.authority_reject(request)


def _input_from_stderr(prompt: str) -> str:
    """Prompt interactive CLI users on stderr so stdout remains machine-owned."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input()


def _story_show(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route story show to the application facade."""
    return "agileforge story show", application.story_show(story_id=args.story_id)


def _sprint_candidates(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route sprint candidates to the application facade."""
    return "agileforge sprint candidates", application.sprint_candidates(
        project_id=args.project_id
    )


def _context_pack(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route context pack to the application facade."""
    return "agileforge context pack", application.context_pack(
        project_id=args.project_id,
        phase=args.phase,
    )


def _status(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route root status to the application facade."""
    return "agileforge status", application.status(project_id=args.project_id)


def _doctor(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route doctor diagnostics to the application facade."""
    return "agileforge doctor", application.doctor()


def _schema_check(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route schema check diagnostics to the application facade."""
    return "agileforge schema check", application.schema_check()


def _spec_profile_schema(
    _args: argparse.Namespace,
    _application: _Application,
) -> CommandResult:
    """Return the AgileForge spec profile JSON Schema."""
    return (
        "agileforge spec profile schema",
        {
            "ok": True,
            "data": {"schema": export_agileforge_spec_schema()},
            "warnings": [],
            "errors": [],
        },
    )


def _spec_profile_validate(
    args: argparse.Namespace,
    _application: _Application,
) -> CommandResult:
    """Validate a spec profile JSON file and optionally render Markdown."""
    command = "agileforge spec profile validate"
    spec_path = Path(str(args.spec_file)).expanduser().resolve()
    try:
        raw_spec = spec_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        return _spec_profile_error(
            command,
            ErrorCode.SPEC_FILE_NOT_FOUND,
            str(exc),
            details={
                "exception_type": type(exc).__name__,
                "spec_file": str(spec_path),
            },
            remediation=["Pass an existing AgileForge spec profile JSON file."],
        )
    except (OSError, UnicodeDecodeError) as exc:
        return _spec_profile_error(
            command,
            ErrorCode.SPEC_FILE_INVALID,
            str(exc),
            details={
                "exception_type": type(exc).__name__,
                "spec_file": str(spec_path),
            },
            remediation=["Pass a readable UTF-8 AgileForge spec profile JSON file."],
        )

    try:
        artifact = TechnicalSpecArtifact.model_validate_json(raw_spec)
    except ValidationError as exc:
        return _spec_profile_error(
            command,
            ErrorCode.SPEC_FILE_INVALID,
            str(exc),
            details={
                "exception_type": type(exc).__name__,
                "spec_file": str(spec_path),
            },
            remediation=["Pass a valid AgileForge spec profile JSON file."],
        )

    markdown = render_markdown(artifact)
    render_md = getattr(args, "render_md", None)
    if render_md:
        render_path = Path(str(render_md)).expanduser().resolve()
        try:
            render_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            return _spec_profile_error(
                command,
                ErrorCode.INVALID_COMMAND,
                str(exc),
                details={
                    "exception_type": type(exc).__name__,
                    "render_md": str(render_path),
                },
                remediation=["Choose a writable Markdown output path for --render-md."],
            )

    return (
        command,
        {
            "ok": True,
            "data": {
                "format": "agileforge.spec.v1",
                "spec_sha256": canonical_spec_hash(artifact),
                "rendered_markdown_sha256": rendered_markdown_hash(markdown),
            },
            "warnings": [],
            "errors": [],
        },
    )


def _spec_profile_error(
    command: str,
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, object],
    remediation: list[str],
) -> CommandResult:
    """Return a structured spec profile command failure."""
    return (
        command,
        {
            "ok": False,
            "data": None,
            "warnings": [],
            "errors": [
                workbench_error(
                    code,
                    message=message,
                    details=details,
                    remediation=remediation,
                ).to_dict()
            ],
        },
    )


def _capabilities(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route capabilities to the application facade."""
    return "agileforge capabilities", application.capabilities()


def _command_schema(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route command schema lookup to the application facade."""
    return "agileforge command schema", application.command_schema(
        command_name=args.command_name,
    )


def _mutation_show(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route mutation show to the application facade."""
    return "agileforge mutation show", application.mutation_show(
        mutation_event_id=args.mutation_event_id,
    )


def _mutation_list(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route mutation list to the application facade."""
    return "agileforge mutation list", application.mutation_list(
        project_id=args.project_id,
        status=args.status,
    )


def _mutation_resume(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route mutation resume lease acquisition to the application facade."""
    return "agileforge mutation resume", application.mutation_resume(
        mutation_event_id=args.mutation_event_id,
        correlation_id=args.correlation_id,
    )


def _dispatch(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route parsed arguments to the application facade."""
    handler = getattr(args, "command_handler", None)
    if callable(handler):
        return cast("CommandHandler", handler)(args, application)

    group = args.group
    action = getattr(args, "action", None)
    return "agileforge", {
        "ok": False,
        "warnings": [],
        "errors": [
            {
                "code": "COMMAND_NOT_IMPLEMENTED",
                "message": "Command is not implemented.",
                "details": {"group": group, "action": action},
                "remediation": ["Run agileforge --help."],
                "exit_code": 2,
                "retryable": False,
            }
        ],
    }


def main(argv: list[str] | None = None, *, application: object | None = None) -> int:
    """Run the CLI and return a process exit code."""
    configure_logging(console=False)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _CliParseError as exc:
        envelope = _parse_error_envelope(str(exc), argv)
        _print_json(envelope)
        return INVALID_COMMAND_EXIT_CODE

    try:
        app = (
            cast("_Application", application)
            if application is not None
            else _default_application()
        )
        with redirect_stdout(io.StringIO()):
            command, result = _dispatch(args, app)
        plain_text = _plain_text_output(args, result)
        if plain_text is not None:
            sys.stdout.write(f"{plain_text}\n")
            return 0
        envelope = _wrap(command, result)
    except Exception as exc:  # noqa: BLE001
        envelope = _exception_envelope(exc)
        _print_json(envelope)
        return COMMAND_EXCEPTION_EXIT_CODE

    _print_json(envelope)
    return _exit_code(envelope)


def _default_application() -> _Application:
    """Create the default application facade."""
    application_module = importlib.import_module("services.agent_workbench.application")
    application_factory = cast(
        "Callable[[], _Application]",
        application_module.AgentWorkbenchApplication,
    )
    return application_factory()


if __name__ == "__main__":
    raise SystemExit(main())
