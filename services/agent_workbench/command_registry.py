"""Installed command metadata for the agent workbench."""

from dataclasses import dataclass, field

from services.agent_workbench.error_codes import ErrorCode
from services.agent_workbench.version import COMMAND_VERSION


@dataclass(frozen=True)
class CommandMetadata:
    """Metadata for a discoverable workbench command contract."""

    name: str
    mutates: bool
    phase: str
    command_version: str = COMMAND_VERSION
    installed: bool = True
    stable: bool = True
    destructive: bool = False
    accepts_expected_state: bool = False
    accepts_expected_artifact_fingerprint: bool = False
    accepts_expected_context_fingerprint: bool = False
    accepts_expected_authority_version: bool = False
    guard_policy: tuple[str, ...] = ()
    requires_idempotency_key: bool = False
    idempotency_policy: dict[str, str] = field(
        default_factory=lambda: {
            "non_dry_run": "not_applicable",
            "dry_run": "not_applicable",
            "dry_run_trace_field": "none",
        }
    )
    input_required: tuple[str, ...] = ()
    input_optional: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


_PHASE_1_COMMANDS: tuple[CommandMetadata, ...] = (
    CommandMetadata(
        name="agileforge status",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
    ),
    CommandMetadata(name="agileforge project list", mutates=False, phase="phase_1"),
    CommandMetadata(
        name="agileforge project show",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
    ),
    CommandMetadata(
        name="agileforge workflow state",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
    ),
    CommandMetadata(
        name="agileforge workflow next",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
    ),
    CommandMetadata(
        name="agileforge authority status",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
    ),
    CommandMetadata(
        name="agileforge authority invariants",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
        input_optional=("spec_version_id",),
    ),
    CommandMetadata(
        name="agileforge story show",
        mutates=False,
        phase="phase_1",
        input_required=("story_id",),
    ),
    CommandMetadata(
        name="agileforge sprint candidates",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
    ),
    CommandMetadata(
        name="agileforge context pack",
        mutates=False,
        phase="phase_1",
        input_required=("project_id",),
        input_optional=("phase",),
    ),
)

_PHASE_2A_COMMANDS: tuple[CommandMetadata, ...] = (
    CommandMetadata(name="agileforge doctor", mutates=False, phase="phase_2a"),
    CommandMetadata(name="agileforge schema check", mutates=False, phase="phase_2a"),
    CommandMetadata(name="agileforge capabilities", mutates=False, phase="phase_2a"),
    CommandMetadata(
        name="agileforge command schema",
        mutates=False,
        phase="phase_2a",
        input_required=("command_name",),
    ),
    CommandMetadata(
        name="agileforge mutation show",
        mutates=False,
        phase="phase_2a",
        input_required=("mutation_event_id",),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.MUTATION_NOT_FOUND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge mutation list",
        mutates=False,
        phase="phase_2a",
        input_optional=("project_id", "status"),
        errors=(ErrorCode.SCHEMA_NOT_READY.value,),
    ),
    CommandMetadata(
        name="agileforge mutation resume",
        mutates=True,
        phase="phase_2a",
        input_required=("mutation_event_id",),
        input_optional=("correlation_id",),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.MUTATION_NOT_FOUND.value,
            ErrorCode.MUTATION_RESUME_CONFLICT.value,
        ),
    ),
)

_DRY_RUN_IDEMPOTENCY_POLICY: dict[str, str] = {
    "non_dry_run": "required",
    "dry_run": "forbidden",
    "dry_run_trace_field": "dry_run_id",
}

_AUTHORITY_DECISION_IDEMPOTENCY_POLICY: dict[str, str] = {
    "non_dry_run": "required",
    "dry_run": "not_supported",
    "dry_run_trace_field": "none",
}

_PHASE_2B_COMMANDS: tuple[CommandMetadata, ...] = (
    CommandMetadata(
        name="agileforge project create",
        mutates=True,
        phase="phase_2b",
        requires_idempotency_key=True,
        idempotency_policy=_DRY_RUN_IDEMPOTENCY_POLICY,
        input_required=("name", "spec_file"),
        input_optional=(
            "idempotency_key",
            "dry_run",
            "dry_run_id",
            "correlation_id",
            "changed_by",
        ),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.PROJECT_ALREADY_EXISTS.value,
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_INVALID.value,
            ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED.value,
            ErrorCode.SPEC_COMPILE_FAILED.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge project setup retry",
        mutates=True,
        phase="phase_2b",
        requires_idempotency_key=True,
        accepts_expected_state=True,
        accepts_expected_context_fingerprint=True,
        idempotency_policy=_DRY_RUN_IDEMPOTENCY_POLICY,
        input_required=(
            "project_id",
            "spec_file",
            "expected_state",
            "expected_context_fingerprint",
        ),
        input_optional=(
            "recovery_mutation_event_id",
            "idempotency_key",
            "dry_run",
            "dry_run_id",
            "correlation_id",
            "changed_by",
        ),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_INVALID.value,
            ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED.value,
            ErrorCode.SPEC_COMPILE_FAILED.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.STALE_STATE.value,
            ErrorCode.STALE_CONTEXT_FINGERPRINT.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
            ErrorCode.MUTATION_RECOVERY_INVALID.value,
            ErrorCode.MUTATION_RESUME_CONFLICT.value,
        ),
    ),
)

_AUTHORITY_DECISION_GUARDS: tuple[str, ...] = (
    "review_token",
    "pending_authority_id",
    "expected_authority_fingerprint",
    "expected_source_spec_hash",
    "expected_disk_spec_hash",
    "expected_resolved_spec_path",
    "expected_state",
    "expected_setup_status",
    "expected_content_included",
    "expected_omission_assessment",
    "expected_coverage_summary_fingerprint",
)

_PHASE_2C_COMMANDS: tuple[CommandMetadata, ...] = (
    CommandMetadata(
        name="agileforge authority review",
        mutates=False,
        phase="phase_2c",
        input_required=("project_id",),
        input_optional=("include_spec", "format"),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_PENDING.value,
            ErrorCode.AUTHORITY_SOURCE_CHANGED.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge authority accept",
        mutates=True,
        phase="phase_2c",
        guard_policy=_AUTHORITY_DECISION_GUARDS,
        accepts_expected_state=True,
        requires_idempotency_key=True,
        idempotency_policy=_AUTHORITY_DECISION_IDEMPOTENCY_POLICY,
        input_required=("project_id", "idempotency_key"),
        input_optional=(
            *_AUTHORITY_DECISION_GUARDS,
            "allow_incomplete_review",
            "incomplete_review_rationale",
            "incomplete_review_overrides",
            "changed_by",
            "actor_mode",
            "policy",
            "correlation_id",
        ),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_PENDING.value,
            ErrorCode.AUTHORITY_REVIEW_INCOMPLETE.value,
            ErrorCode.AUTHORITY_ALREADY_DECIDED.value,
            ErrorCode.AUTHORITY_SOURCE_CHANGED.value,
            ErrorCode.AUTHORITY_SOURCE_UNAVAILABLE.value,
            ErrorCode.AUTHORITY_GUARD_INCOMPLETE.value,
            ErrorCode.STALE_STATE.value,
            ErrorCode.STALE_AUTHORITY_VERSION.value,
            ErrorCode.STALE_ARTIFACT_FINGERPRINT.value,
            ErrorCode.STALE_CONTEXT_FINGERPRINT.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
            ErrorCode.MUTATION_RESUME_CONFLICT.value,
        ),
    ),
    CommandMetadata(
        name="agileforge authority reject",
        mutates=True,
        phase="phase_2c",
        guard_policy=_AUTHORITY_DECISION_GUARDS,
        accepts_expected_state=True,
        requires_idempotency_key=True,
        idempotency_policy=_AUTHORITY_DECISION_IDEMPOTENCY_POLICY,
        input_required=("project_id", "reason", "idempotency_key"),
        input_optional=(
            *_AUTHORITY_DECISION_GUARDS,
            "changed_by",
            "actor_mode",
            "policy",
            "correlation_id",
        ),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_PENDING.value,
            ErrorCode.AUTHORITY_REVIEW_INCOMPLETE.value,
            ErrorCode.AUTHORITY_ALREADY_DECIDED.value,
            ErrorCode.AUTHORITY_SOURCE_CHANGED.value,
            ErrorCode.AUTHORITY_SOURCE_UNAVAILABLE.value,
            ErrorCode.AUTHORITY_GUARD_INCOMPLETE.value,
            ErrorCode.STALE_STATE.value,
            ErrorCode.STALE_AUTHORITY_VERSION.value,
            ErrorCode.STALE_ARTIFACT_FINGERPRINT.value,
            ErrorCode.STALE_CONTEXT_FINGERPRINT.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
            ErrorCode.MUTATION_RESUME_CONFLICT.value,
        ),
    ),
)


_PHASE_2E_COMMANDS: tuple[CommandMetadata, ...] = (
    CommandMetadata(
        name="agileforge spec profile schema",
        mutates=False,
        phase="phase_2e",
    ),
    CommandMetadata(
        name="agileforge spec profile validate",
        mutates=False,
        phase="phase_2e",
        input_required=("spec_file",),
        input_optional=("render_md",),
        errors=(
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_INVALID.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
)


def command_contracts() -> tuple[CommandMetadata, ...]:
    """Return discoverable command contracts for the current workbench phase."""
    return (
        *_PHASE_1_COMMANDS,
        *_PHASE_2A_COMMANDS,
        *_PHASE_2B_COMMANDS,
        *_PHASE_2C_COMMANDS,
        *_PHASE_2E_COMMANDS,
    )


def installed_commands() -> tuple[CommandMetadata, ...]:
    """Return CLI-installed command metadata for the current workbench phase."""
    return tuple(command for command in command_contracts() if command.installed)


def installed_command_names() -> set[str]:
    """Return names for installed commands."""
    return {command.name for command in installed_commands()}


def command_is_available(name: str) -> bool:
    """Return whether a command is installed."""
    return name in installed_command_names()
