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

_AUTHORITY_REGENERATE_IDEMPOTENCY_POLICY: dict[str, str] = {
    "non_dry_run": "required",
    "dry_run": "not_required_no_ledger",
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
        input_optional=("include_spec", "format", "open"),
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
        requires_idempotency_key=False,
        input_required=("project_id",),
        input_optional=(
            "idempotency_key",
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
    CommandMetadata(
        name="agileforge authority regenerate",
        mutates=True,
        phase="phase_2c",
        requires_idempotency_key=True,
        idempotency_policy=_AUTHORITY_REGENERATE_IDEMPOTENCY_POLICY,
        input_required=("project_id", "spec_version_id"),
        input_optional=(
            "idempotency_key",
            "changed_by",
            "dry_run",
        ),
        errors=(
            ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value,
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.SPEC_VERSION_NOT_FOUND.value,
            ErrorCode.AUTHORITY_REVIEW_REQUIRED.value,
            ErrorCode.SPEC_COMPILE_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
            ErrorCode.MUTATION_RESUME_CONFLICT.value,
        ),
    ),
)


_PHASE_2D_COMMANDS: tuple[CommandMetadata, ...] = (
    CommandMetadata(
        name="agileforge vision generate",
        mutates=True,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("input",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge vision history",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge vision save",
        mutates=True,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog generate",
        mutates=True,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("input",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog preview",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("input",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog refine-preview",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=(
            "source_attempt_id",
            "operations_file",
            "source_artifact",
            "input",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog refine-record",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "source_attempt_id",
            "operations_file",
            "expected_source_fingerprint",
            "expected_state",
            "idempotency_key",
        ),
        input_optional=("approval_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog approve",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "approved_artifact_fingerprint",
            "idempotency_key",
        ),
        input_optional=(
            "source_attempt_id",
            "attempt_id",
            "operation_set_fingerprint",
            "approved_operation_id",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog refine-import",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "source_artifact",
            "edited_file",
            "expected_source_fingerprint",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog history",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog save",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog reset-active",
        mutates=True,
        phase="phase_2d",
        destructive=False,
        requires_idempotency_key=True,
        accepts_expected_state=True,
        accepts_expected_artifact_fingerprint=True,
        input_required=(
            "project_id",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "reset_reason",
            "archive_all_active_stories",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge backlog reconcile",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "idempotency_key"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge evidence collect",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "idempotency_key"),
        input_optional=("repo_path", "from_file"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.AUTHORITY_NOT_COMPILED.value,
            ErrorCode.AUTHORITY_ACCEPTANCE_MISMATCH.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge as-built assess",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "repo_path", "idempotency_key"),
        input_optional=("spec_file", "spec_mode", "user_input"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.AUTHORITY_NOT_COMPILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge roadmap generate",
        mutates=True,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("input",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge roadmap history",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge roadmap save",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story pending",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story generate",
        mutates=True,
        phase="phase_2d",
        input_required=("project_id", "parent_requirement"),
        input_optional=("input", "force_feedback"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story retry",
        mutates=True,
        phase="phase_2d",
        input_required=("project_id", "parent_requirement"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story history",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id", "parent_requirement"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story save",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "parent_requirement",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story complete",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "expected_state", "idempotency_key"),
        input_optional=("scope", "scope_id", "parent_requirement"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story reopen",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "parent_requirement",
            "expected_state",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story repair-readiness",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "expected_state", "idempotency_key"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story dependencies inspect",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story dependencies propose",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "expected_state", "idempotency_key"),
        input_optional=("manual_edge",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge story dependencies apply",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint generate",
        mutates=True,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=(
            "input",
            "selected_story_ids",
            "max_story_points",
            "include_task_decomposition",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint history",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint metrics",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint save",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "team_name",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint start",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "expected_state", "idempotency_key"),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint status",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint tasks",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint task next",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint task show",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id", "task_id"),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint task history",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id", "task_id"),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint task update",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "task_id",
            "status",
            "expected_status",
            "expected_task_fingerprint",
            "idempotency_key",
        ),
        input_optional=(
            "sprint_id",
            "outcome_summary",
            "artifact_ref",
            "checklist_result",
            "validation_summary",
            "notes",
            "changed_by",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint story readiness",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id", "story_id"),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint story close",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "story_id",
            "expected_status",
            "expected_story_fingerprint",
            "idempotency_key",
            "resolution",
            "completion_notes",
        ),
        input_optional=(
            "sprint_id",
            "evidence_link",
            "changed_by",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint close-readiness",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint close",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "expected_state",
            "expected_status",
            "expected_sprint_fingerprint",
            "idempotency_key",
            "completion_notes",
        ),
        input_optional=(
            "sprint_id",
            "follow_up_notes",
            "changed_by",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint review",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("sprint_id",),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge sprint triage",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "expected_state",
            "impact",
            "learning_summary",
            "decision_reason",
            "idempotency_key",
        ),
        input_optional=(
            "sprint_id",
            "affected_requirement",
            "affected_task_id",
            "affected_story_id",
            "affected_backlog_item_id",
            "affected_roadmap_item_id",
            "affected_layer",
            "replace_existing",
            "expected_triage_fingerprint",
            "changed_by",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.WORKFLOW_SESSION_FAILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
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
        *_PHASE_2D_COMMANDS,
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
