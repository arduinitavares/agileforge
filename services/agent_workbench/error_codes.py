"""Registered agent workbench CLI error codes."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.agent_workbench.envelope import WorkbenchError


@dataclass(frozen=True)
class ErrorMetadata:
    """Stable metadata for a registered CLI error code."""

    code: str
    default_exit_code: int
    retryable: bool
    description: str


class ErrorCode(StrEnum):
    """Registered agent workbench CLI error codes."""

    INVALID_COMMAND = "INVALID_COMMAND"
    COMMAND_EXCEPTION = "COMMAND_EXCEPTION"
    COMMAND_NOT_IMPLEMENTED = "COMMAND_NOT_IMPLEMENTED"
    SCHEMA_NOT_READY = "SCHEMA_NOT_READY"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    PROJECT_ALREADY_EXISTS = "PROJECT_ALREADY_EXISTS"
    STORY_NOT_FOUND = "STORY_NOT_FOUND"
    SPEC_VERSION_NOT_FOUND = "SPEC_VERSION_NOT_FOUND"
    SPEC_FILE_NOT_FOUND = "SPEC_FILE_NOT_FOUND"
    SPEC_FILE_INVALID = "SPEC_FILE_INVALID"
    SPEC_SOURCE_FORMAT_UNSUPPORTED = "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    SPEC_COMPILE_FAILED = "SPEC_COMPILE_FAILED"
    AUTHORITY_NOT_ACCEPTED = "AUTHORITY_NOT_ACCEPTED"
    AUTHORITY_NOT_COMPILED = "AUTHORITY_NOT_COMPILED"
    COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED = "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    AUTHORITY_ACCEPTANCE_MISMATCH = "AUTHORITY_ACCEPTANCE_MISMATCH"
    AUTHORITY_INVARIANTS_INVALID = "AUTHORITY_INVARIANTS_INVALID"
    AUTHORITY_REVIEW_REQUIRED = "AUTHORITY_REVIEW_REQUIRED"
    AUTHORITY_NOT_PENDING = "AUTHORITY_NOT_PENDING"
    AUTHORITY_ALREADY_DECIDED = "AUTHORITY_ALREADY_DECIDED"
    AUTHORITY_SOURCE_CHANGED = "AUTHORITY_SOURCE_CHANGED"
    AUTHORITY_SOURCE_UNAVAILABLE = "AUTHORITY_SOURCE_UNAVAILABLE"
    AUTHORITY_REVIEW_INCOMPLETE = "AUTHORITY_REVIEW_INCOMPLETE"
    AUTHORITY_GUARD_INCOMPLETE = "AUTHORITY_GUARD_INCOMPLETE"
    STALE_STATE = "STALE_STATE"
    STALE_SETUP_STATUS = "STALE_SETUP_STATUS"
    STALE_SPEC_HASH = "STALE_SPEC_HASH"
    STALE_SPEC_VERSION = "STALE_SPEC_VERSION"
    STALE_ARTIFACT_FINGERPRINT = "STALE_ARTIFACT_FINGERPRINT"
    STALE_CONTEXT_FINGERPRINT = "STALE_CONTEXT_FINGERPRINT"
    STALE_AUTHORITY_VERSION = "STALE_AUTHORITY_VERSION"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    ACTIVE_STATE_BLOCKS_DELETE = "ACTIVE_STATE_BLOCKS_DELETE"
    SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"
    MUTATION_FAILED = "MUTATION_FAILED"
    SPRINT_GENERATION_MODEL_RESPONSE_INVALID = (
        "SPRINT_GENERATION_MODEL_RESPONSE_INVALID"
    )
    MUTATION_ROLLBACK = "MUTATION_ROLLBACK"
    MUTATION_IN_PROGRESS = "MUTATION_IN_PROGRESS"
    MUTATION_RECOVERY_REQUIRED = "MUTATION_RECOVERY_REQUIRED"
    MUTATION_RESUME_CONFLICT = "MUTATION_RESUME_CONFLICT"
    MUTATION_RECOVERY_INVALID = "MUTATION_RECOVERY_INVALID"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    MUTATION_NOT_FOUND = "MUTATION_NOT_FOUND"
    WORKFLOW_SESSION_FAILED = "WORKFLOW_SESSION_FAILED"
    TRIAGE_ALREADY_RECORDED = "TRIAGE_ALREADY_RECORDED"
    TRIAGE_FINGERPRINT_MISMATCH = "TRIAGE_FINGERPRINT_MISMATCH"
    TRIAGE_EXPECTED_STATE_MISMATCH = "TRIAGE_EXPECTED_STATE_MISMATCH"
    TRIAGE_IMPACT_FIELDS_INVALID = "TRIAGE_IMPACT_FIELDS_INVALID"
    TRIAGE_REQUIRED_FIELD_MISSING = "TRIAGE_REQUIRED_FIELD_MISSING"
    TRIAGE_FIELD_INVALID = "TRIAGE_FIELD_INVALID"
    BACKLOG_SOURCE_UNAVAILABLE = "BACKLOG_SOURCE_UNAVAILABLE"
    SCOPE_EXTENSION_NOT_AVAILABLE = "SCOPE_EXTENSION_NOT_AVAILABLE"
    SCOPE_EXTENSION_BASE_SPEC_MISMATCH = "SCOPE_EXTENSION_BASE_SPEC_MISMATCH"
    SCOPE_EXTENSION_UNRESOLVED_WORK = "SCOPE_EXTENSION_UNRESOLVED_WORK"
    SCOPE_EXTENSION_NOT_ADDITIVE = "SCOPE_EXTENSION_NOT_ADDITIVE"
    SCOPE_EXTENSION_NO_ADDED_ITEMS = "SCOPE_EXTENSION_NO_ADDED_ITEMS"


_ERROR_REGISTRY: dict[ErrorCode, ErrorMetadata] = {
    ErrorCode.INVALID_COMMAND: ErrorMetadata(
        code=ErrorCode.INVALID_COMMAND.value,
        default_exit_code=2,
        retryable=False,
        description="The command is invalid.",
    ),
    ErrorCode.COMMAND_EXCEPTION: ErrorMetadata(
        code=ErrorCode.COMMAND_EXCEPTION.value,
        default_exit_code=1,
        retryable=False,
        description="The command failed with an unexpected exception.",
    ),
    ErrorCode.COMMAND_NOT_IMPLEMENTED: ErrorMetadata(
        code=ErrorCode.COMMAND_NOT_IMPLEMENTED.value,
        default_exit_code=2,
        retryable=False,
        description="The command is registered but not implemented.",
    ),
    ErrorCode.SCHEMA_NOT_READY: ErrorMetadata(
        code=ErrorCode.SCHEMA_NOT_READY.value,
        default_exit_code=5,
        retryable=True,
        description="Required schema objects are missing.",
    ),
    ErrorCode.PROJECT_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.PROJECT_NOT_FOUND.value,
        default_exit_code=4,
        retryable=False,
        description="The requested project was not found.",
    ),
    ErrorCode.PROJECT_ALREADY_EXISTS: ErrorMetadata(
        code=ErrorCode.PROJECT_ALREADY_EXISTS.value,
        default_exit_code=2,
        retryable=False,
        description="A project with this name already exists.",
    ),
    ErrorCode.STORY_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.STORY_NOT_FOUND.value,
        default_exit_code=4,
        retryable=False,
        description="The requested story was not found.",
    ),
    ErrorCode.SPEC_VERSION_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.SPEC_VERSION_NOT_FOUND.value,
        default_exit_code=4,
        retryable=False,
        description="The requested spec version was not found.",
    ),
    ErrorCode.SPEC_FILE_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.SPEC_FILE_NOT_FOUND.value,
        default_exit_code=2,
        retryable=False,
        description="The requested spec file was not found.",
    ),
    ErrorCode.SPEC_FILE_INVALID: ErrorMetadata(
        code=ErrorCode.SPEC_FILE_INVALID.value,
        default_exit_code=2,
        retryable=False,
        description="The requested spec file is invalid.",
    ),
    ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED: ErrorMetadata(
        code=ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED.value,
        default_exit_code=2,
        retryable=False,
        description="The requested spec source format is not supported.",
    ),
    ErrorCode.SPEC_COMPILE_FAILED: ErrorMetadata(
        code=ErrorCode.SPEC_COMPILE_FAILED.value,
        default_exit_code=1,
        retryable=True,
        description="Spec authority compilation failed.",
    ),
    ErrorCode.AUTHORITY_NOT_ACCEPTED: ErrorMetadata(
        code=ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
        default_exit_code=4,
        retryable=False,
        description="The project has no accepted authority.",
    ),
    ErrorCode.AUTHORITY_NOT_COMPILED: ErrorMetadata(
        code=ErrorCode.AUTHORITY_NOT_COMPILED.value,
        default_exit_code=4,
        retryable=False,
        description="The selected spec version has no compiled authority.",
    ),
    ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED: ErrorMetadata(
        code=ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value,
        default_exit_code=4,
        retryable=False,
        description="Compiled authority artifact schema is unsupported.",
    ),
    ErrorCode.AUTHORITY_ACCEPTANCE_MISMATCH: ErrorMetadata(
        code=ErrorCode.AUTHORITY_ACCEPTANCE_MISMATCH.value,
        default_exit_code=4,
        retryable=False,
        description="Accepted authority does not match compiled authority.",
    ),
    ErrorCode.AUTHORITY_INVARIANTS_INVALID: ErrorMetadata(
        code=ErrorCode.AUTHORITY_INVARIANTS_INVALID.value,
        default_exit_code=4,
        retryable=False,
        description="Authority invariants are invalid.",
    ),
    ErrorCode.AUTHORITY_REVIEW_REQUIRED: ErrorMetadata(
        code=ErrorCode.AUTHORITY_REVIEW_REQUIRED.value,
        default_exit_code=4,
        retryable=False,
        description="Authority review is required before this operation.",
    ),
    ErrorCode.AUTHORITY_NOT_PENDING: ErrorMetadata(
        code=ErrorCode.AUTHORITY_NOT_PENDING.value,
        default_exit_code=4,
        retryable=False,
        description="The requested authority is not pending review.",
    ),
    ErrorCode.AUTHORITY_ALREADY_DECIDED: ErrorMetadata(
        code=ErrorCode.AUTHORITY_ALREADY_DECIDED.value,
        default_exit_code=10,
        retryable=False,
        description="The requested authority already has a terminal decision.",
    ),
    ErrorCode.AUTHORITY_SOURCE_CHANGED: ErrorMetadata(
        code=ErrorCode.AUTHORITY_SOURCE_CHANGED.value,
        default_exit_code=11,
        retryable=True,
        description="Authority source material changed during review.",
    ),
    ErrorCode.AUTHORITY_SOURCE_UNAVAILABLE: ErrorMetadata(
        code=ErrorCode.AUTHORITY_SOURCE_UNAVAILABLE.value,
        default_exit_code=11,
        retryable=True,
        description="Authority source material is unavailable for review.",
    ),
    ErrorCode.AUTHORITY_REVIEW_INCOMPLETE: ErrorMetadata(
        code=ErrorCode.AUTHORITY_REVIEW_INCOMPLETE.value,
        default_exit_code=20,
        retryable=False,
        description="Authority review is incomplete.",
    ),
    ErrorCode.AUTHORITY_GUARD_INCOMPLETE: ErrorMetadata(
        code=ErrorCode.AUTHORITY_GUARD_INCOMPLETE.value,
        default_exit_code=2,
        retryable=False,
        description="Required authority review guard inputs are incomplete.",
    ),
    ErrorCode.STALE_STATE: ErrorMetadata(
        code=ErrorCode.STALE_STATE.value,
        default_exit_code=3,
        retryable=True,
        description="Expected workflow state did not match.",
    ),
    ErrorCode.STALE_SETUP_STATUS: ErrorMetadata(
        code=ErrorCode.STALE_SETUP_STATUS.value,
        default_exit_code=3,
        retryable=True,
        description="Expected setup status did not match.",
    ),
    ErrorCode.STALE_SPEC_HASH: ErrorMetadata(
        code=ErrorCode.STALE_SPEC_HASH.value,
        default_exit_code=3,
        retryable=True,
        description="Expected setup spec hash did not match.",
    ),
    ErrorCode.STALE_SPEC_VERSION: ErrorMetadata(
        code=ErrorCode.STALE_SPEC_VERSION.value,
        default_exit_code=3,
        retryable=True,
        description="Expected setup spec version did not match.",
    ),
    ErrorCode.STALE_ARTIFACT_FINGERPRINT: ErrorMetadata(
        code=ErrorCode.STALE_ARTIFACT_FINGERPRINT.value,
        default_exit_code=3,
        retryable=True,
        description="Reviewed artifact fingerprint changed.",
    ),
    ErrorCode.STALE_CONTEXT_FINGERPRINT: ErrorMetadata(
        code=ErrorCode.STALE_CONTEXT_FINGERPRINT.value,
        default_exit_code=3,
        retryable=True,
        description="Reviewed context fingerprint changed.",
    ),
    ErrorCode.STALE_AUTHORITY_VERSION: ErrorMetadata(
        code=ErrorCode.STALE_AUTHORITY_VERSION.value,
        default_exit_code=3,
        retryable=True,
        description="Accepted authority version changed.",
    ),
    ErrorCode.CONFIRMATION_REQUIRED: ErrorMetadata(
        code=ErrorCode.CONFIRMATION_REQUIRED.value,
        default_exit_code=2,
        retryable=False,
        description="Destructive confirmation is missing.",
    ),
    ErrorCode.ACTIVE_STATE_BLOCKS_DELETE: ErrorMetadata(
        code=ErrorCode.ACTIVE_STATE_BLOCKS_DELETE.value,
        default_exit_code=4,
        retryable=False,
        description="Active workflow state blocks deletion.",
    ),
    ErrorCode.SCHEMA_VERSION_MISMATCH: ErrorMetadata(
        code=ErrorCode.SCHEMA_VERSION_MISMATCH.value,
        default_exit_code=5,
        retryable=True,
        description="Storage schema version is incompatible.",
    ),
    ErrorCode.MUTATION_FAILED: ErrorMetadata(
        code=ErrorCode.MUTATION_FAILED.value,
        default_exit_code=1,
        retryable=False,
        description="The mutation failed.",
    ),
    ErrorCode.SPRINT_GENERATION_MODEL_RESPONSE_INVALID: ErrorMetadata(
        code=ErrorCode.SPRINT_GENERATION_MODEL_RESPONSE_INVALID.value,
        default_exit_code=1,
        retryable=True,
        description="Sprint generation produced an invalid model response.",
    ),
    ErrorCode.MUTATION_ROLLBACK: ErrorMetadata(
        code=ErrorCode.MUTATION_ROLLBACK.value,
        default_exit_code=1,
        retryable=True,
        description="Mutation rolled back or needs recovery.",
    ),
    ErrorCode.MUTATION_IN_PROGRESS: ErrorMetadata(
        code=ErrorCode.MUTATION_IN_PROGRESS.value,
        default_exit_code=1,
        retryable=True,
        description="Mutation lease is still active.",
    ),
    ErrorCode.MUTATION_RECOVERY_REQUIRED: ErrorMetadata(
        code=ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        default_exit_code=1,
        retryable=True,
        description="Mutation requires recovery before replay.",
    ),
    ErrorCode.MUTATION_RESUME_CONFLICT: ErrorMetadata(
        code=ErrorCode.MUTATION_RESUME_CONFLICT.value,
        default_exit_code=1,
        retryable=True,
        description="Another worker acquired recovery.",
    ),
    ErrorCode.MUTATION_RECOVERY_INVALID: ErrorMetadata(
        code=ErrorCode.MUTATION_RECOVERY_INVALID.value,
        default_exit_code=10,
        retryable=False,
        description="The requested mutation recovery link is invalid.",
    ),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: ErrorMetadata(
        code=ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        default_exit_code=2,
        retryable=False,
        description="Idempotency key was reused with a different request.",
    ),
    ErrorCode.MUTATION_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.MUTATION_NOT_FOUND.value,
        default_exit_code=4,
        retryable=False,
        description="The requested mutation was not found.",
    ),
    ErrorCode.WORKFLOW_SESSION_FAILED: ErrorMetadata(
        code=ErrorCode.WORKFLOW_SESSION_FAILED.value,
        default_exit_code=1,
        retryable=True,
        description="Workflow session setup failed.",
    ),
    ErrorCode.TRIAGE_ALREADY_RECORDED: ErrorMetadata(
        code=ErrorCode.TRIAGE_ALREADY_RECORDED.value,
        default_exit_code=2,
        retryable=False,
        description="Post-sprint triage has already been recorded.",
    ),
    ErrorCode.TRIAGE_FINGERPRINT_MISMATCH: ErrorMetadata(
        code=ErrorCode.TRIAGE_FINGERPRINT_MISMATCH.value,
        default_exit_code=2,
        retryable=False,
        description="Post-sprint triage fingerprint did not match.",
    ),
    ErrorCode.TRIAGE_EXPECTED_STATE_MISMATCH: ErrorMetadata(
        code=ErrorCode.TRIAGE_EXPECTED_STATE_MISMATCH.value,
        default_exit_code=2,
        retryable=False,
        description="Workflow state does not allow post-sprint triage.",
    ),
    ErrorCode.TRIAGE_IMPACT_FIELDS_INVALID: ErrorMetadata(
        code=ErrorCode.TRIAGE_IMPACT_FIELDS_INVALID.value,
        default_exit_code=2,
        retryable=False,
        description="Post-sprint triage impact fields are invalid.",
    ),
    ErrorCode.TRIAGE_REQUIRED_FIELD_MISSING: ErrorMetadata(
        code=ErrorCode.TRIAGE_REQUIRED_FIELD_MISSING.value,
        default_exit_code=2,
        retryable=False,
        description="A required post-sprint triage field is missing.",
    ),
    ErrorCode.TRIAGE_FIELD_INVALID: ErrorMetadata(
        code=ErrorCode.TRIAGE_FIELD_INVALID.value,
        default_exit_code=2,
        retryable=False,
        description="A post-sprint triage field is invalid.",
    ),
    ErrorCode.BACKLOG_SOURCE_UNAVAILABLE: ErrorMetadata(
        code=ErrorCode.BACKLOG_SOURCE_UNAVAILABLE.value,
        default_exit_code=2,
        retryable=False,
        description="Backlog source data is unavailable.",
    ),
    ErrorCode.SCOPE_EXTENSION_NOT_AVAILABLE: ErrorMetadata(
        code=ErrorCode.SCOPE_EXTENSION_NOT_AVAILABLE.value,
        default_exit_code=4,
        retryable=False,
        description="Project scope extension is not available.",
    ),
    ErrorCode.SCOPE_EXTENSION_BASE_SPEC_MISMATCH: ErrorMetadata(
        code=ErrorCode.SCOPE_EXTENSION_BASE_SPEC_MISMATCH.value,
        default_exit_code=3,
        retryable=True,
        description="Scope extension base spec guard did not match.",
    ),
    ErrorCode.SCOPE_EXTENSION_UNRESOLVED_WORK: ErrorMetadata(
        code=ErrorCode.SCOPE_EXTENSION_UNRESOLVED_WORK.value,
        default_exit_code=4,
        retryable=False,
        description="Project scope extension is blocked by unresolved work.",
    ),
    ErrorCode.SCOPE_EXTENSION_NOT_ADDITIVE: ErrorMetadata(
        code=ErrorCode.SCOPE_EXTENSION_NOT_ADDITIVE.value,
        default_exit_code=2,
        retryable=False,
        description="Scope extension amendment is not additive.",
    ),
    ErrorCode.SCOPE_EXTENSION_NO_ADDED_ITEMS: ErrorMetadata(
        code=ErrorCode.SCOPE_EXTENSION_NO_ADDED_ITEMS.value,
        default_exit_code=2,
        retryable=False,
        description="Scope extension amendment adds no source items.",
    ),
}

_BROWNFIELD_ERROR_REGISTRY: dict[str, ErrorMetadata] = {
    "BROWNFIELD_SOURCE_FILE_NOT_FOUND": ErrorMetadata(
        code="BROWNFIELD_SOURCE_FILE_NOT_FOUND",
        default_exit_code=2,
        retryable=False,
        description="Brownfield source file was not found.",
    ),
    "BROWNFIELD_REPO_PATH_NOT_FOUND": ErrorMetadata(
        code="BROWNFIELD_REPO_PATH_NOT_FOUND",
        default_exit_code=2,
        retryable=False,
        description="Brownfield repository path was not found.",
    ),
    "BROWNFIELD_SOURCE_NOT_FOUND": ErrorMetadata(
        code="BROWNFIELD_SOURCE_NOT_FOUND",
        default_exit_code=4,
        retryable=False,
        description="Brownfield source attempt was not found.",
    ),
}


def _normalize_code(code: ErrorCode | str) -> ErrorCode:
    """Return a registered ErrorCode from enum or string input."""
    if isinstance(code, ErrorCode):
        return code
    return ErrorCode(code)


def error_metadata(code: ErrorCode | str) -> ErrorMetadata:
    """Return stable metadata for a registered error code."""
    if isinstance(code, str) and code in _BROWNFIELD_ERROR_REGISTRY:
        return _BROWNFIELD_ERROR_REGISTRY[code]
    return _ERROR_REGISTRY[_normalize_code(code)]


def registered_error_codes() -> set[str]:
    """Return the complete registered CLI error code set."""
    return {metadata.code for metadata in _ERROR_REGISTRY.values()}


def workbench_error(
    code: ErrorCode | str,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    remediation: list[str] | None = None,
) -> WorkbenchError:
    """Build a WorkbenchError using registry defaults."""
    metadata = error_metadata(code)
    return WorkbenchError(
        code=metadata.code,
        message=metadata.description if message is None else message,
        details=dict(details or {}),
        remediation=list(remediation or []),
        exit_code=metadata.default_exit_code,
        retryable=metadata.retryable,
    )
