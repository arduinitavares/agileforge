"""Tests for agent workbench error code registry."""

import pytest

from services.agent_workbench.error_codes import (
    ErrorCode,
    error_metadata,
    registered_error_codes,
    workbench_error,
)

EXPECTED_ERROR_METADATA = {
    ErrorCode.INVALID_COMMAND: (2, False),
    ErrorCode.COMMAND_EXCEPTION: (1, False),
    ErrorCode.COMMAND_NOT_IMPLEMENTED: (2, False),
    ErrorCode.SCHEMA_NOT_READY: (5, True),
    ErrorCode.PROJECT_NOT_FOUND: (4, False),
    ErrorCode.PROJECT_ALREADY_EXISTS: (2, False),
    ErrorCode.STORY_NOT_FOUND: (4, False),
    ErrorCode.SPEC_VERSION_NOT_FOUND: (4, False),
    ErrorCode.SPEC_FILE_NOT_FOUND: (2, False),
    ErrorCode.SPEC_FILE_INVALID: (2, False),
    ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED: (2, False),
    ErrorCode.SPEC_COMPILE_FAILED: (1, True),
    ErrorCode.AUTHORITY_NOT_ACCEPTED: (4, False),
    ErrorCode.AUTHORITY_NOT_COMPILED: (4, False),
    ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED: (4, False),
    ErrorCode.AUTHORITY_ACCEPTANCE_MISMATCH: (4, False),
    ErrorCode.AUTHORITY_INVARIANTS_INVALID: (4, False),
    ErrorCode.AUTHORITY_REVIEW_REQUIRED: (4, False),
    ErrorCode.AUTHORITY_NOT_PENDING: (4, False),
    ErrorCode.AUTHORITY_ALREADY_DECIDED: (10, False),
    ErrorCode.AUTHORITY_SOURCE_CHANGED: (11, True),
    ErrorCode.AUTHORITY_SOURCE_UNAVAILABLE: (11, True),
    ErrorCode.AUTHORITY_REVIEW_INCOMPLETE: (20, False),
    ErrorCode.AUTHORITY_GUARD_INCOMPLETE: (2, False),
    ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND: (4, False),
    ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID: (2, False),
    ErrorCode.AUTHORITY_CURATED_DIFF_UNBOUNDED: (1, False),
    ErrorCode.AUTHORITY_CURATION_MAX_ITERATIONS: (1, True),
    ErrorCode.AUTHORITY_REPAIR_INTENT_INVALID: (1, False),
    ErrorCode.AUTHORITY_REPAIR_TARGET_NOT_FOUND: (1, False),
    ErrorCode.SCHEMA_VERSION_MISMATCH: (5, True),
    ErrorCode.STALE_STATE: (3, True),
    ErrorCode.STALE_SETUP_STATUS: (3, True),
    ErrorCode.STALE_SPEC_HASH: (3, True),
    ErrorCode.STALE_SPEC_VERSION: (3, True),
    ErrorCode.STALE_ARTIFACT_FINGERPRINT: (3, True),
    ErrorCode.STALE_CONTEXT_FINGERPRINT: (3, True),
    ErrorCode.STALE_AUTHORITY_VERSION: (3, True),
    ErrorCode.CONFIRMATION_REQUIRED: (2, False),
    ErrorCode.ACTIVE_STATE_BLOCKS_DELETE: (4, False),
    ErrorCode.MUTATION_FAILED: (1, False),
    ErrorCode.SPRINT_GENERATION_MODEL_RESPONSE_INVALID: (1, True),
    ErrorCode.MUTATION_ROLLBACK: (1, True),
    ErrorCode.MUTATION_IN_PROGRESS: (1, True),
    ErrorCode.MUTATION_RECOVERY_REQUIRED: (1, True),
    ErrorCode.MUTATION_RESUME_CONFLICT: (1, True),
    ErrorCode.MUTATION_RECOVERY_INVALID: (10, False),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: (2, False),
    ErrorCode.MUTATION_NOT_FOUND: (4, False),
    ErrorCode.WORKFLOW_SESSION_FAILED: (1, True),
    ErrorCode.TRIAGE_ALREADY_RECORDED: (2, False),
    ErrorCode.TRIAGE_FINGERPRINT_MISMATCH: (2, False),
    ErrorCode.TRIAGE_EXPECTED_STATE_MISMATCH: (2, False),
    ErrorCode.TRIAGE_IMPACT_FIELDS_INVALID: (2, False),
    ErrorCode.TRIAGE_REQUIRED_FIELD_MISSING: (2, False),
    ErrorCode.TRIAGE_FIELD_INVALID: (2, False),
    ErrorCode.BACKLOG_SOURCE_UNAVAILABLE: (2, False),
    ErrorCode.SCOPE_EXTENSION_NOT_AVAILABLE: (4, False),
    ErrorCode.SCOPE_EXTENSION_BASE_SPEC_MISMATCH: (3, True),
    ErrorCode.SCOPE_EXTENSION_UNRESOLVED_WORK: (4, False),
    ErrorCode.SCOPE_EXTENSION_NOT_ADDITIVE: (2, False),
    ErrorCode.SCOPE_EXTENSION_NO_ADDED_ITEMS: (2, False),
    ErrorCode.CHALLENGE_ARTIFACT_FILE_NOT_FOUND: (2, False),
    ErrorCode.CHALLENGE_ARTIFACT_INVALID: (2, False),
    ErrorCode.CHALLENGE_PRODUCER_INVALID: (2, False),
    ErrorCode.PRD_FILE_NOT_FOUND: (2, False),
    ErrorCode.PRD_DRAFT_INVALID: (2, False),
    ErrorCode.PRD_PRODUCER_INVALID: (2, False),
    ErrorCode.PRD_SOURCE_CHALLENGE_NOT_FOUND: (4, False),
    ErrorCode.PRD_SOURCE_CHALLENGE_NOT_READY: (4, False),
    ErrorCode.PRD_NOT_FOUND: (4, False),
    ErrorCode.PRD_REVIEW_STATE_INVALID: (4, False),
    ErrorCode.PRD_ACCEPTED_IMMUTABLE: (4, False),
    ErrorCode.PRD_SUPERSEDES_NOT_FOUND: (4, False),
    ErrorCode.PRD_SUPERSEDES_NOT_ACCEPTED: (4, False),
    ErrorCode.SPEC_AMENDMENT_SOURCE_PRD_NOT_ACCEPTED: (4, False),
    ErrorCode.SPEC_AMENDMENT_NOT_FOUND: (4, False),
    ErrorCode.SPEC_AMENDMENT_REVIEW_STATE_INVALID: (4, False),
    ErrorCode.SPEC_AMENDMENT_NOT_ACCEPTED: (4, False),
    ErrorCode.BROWNFIELD_SOURCE_FILE_NOT_FOUND: (2, False),
    ErrorCode.BROWNFIELD_REPO_PATH_NOT_FOUND: (2, False),
    ErrorCode.BROWNFIELD_SOURCE_NOT_FOUND: (4, False),
    ErrorCode.BROWNFIELD_SCAN_NOT_FOUND: (4, False),
    ErrorCode.BROWNFIELD_DRAFT_NOT_FOUND: (4, False),
    ErrorCode.BROWNFIELD_DRAFT_STALE: (3, True),
    ErrorCode.BROWNFIELD_DRAFT_INCOMPLETE: (4, False),
    ErrorCode.BROWNFIELD_SOURCE_SUPERSEDED: (3, True),
    ErrorCode.BROWNFIELD_APPROVAL_CHAIN_MISMATCH: (3, True),
    ErrorCode.BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED: (10, False),
    ErrorCode.BROWNFIELD_APPROVAL_STALE_GUARD: (3, True),
}


def test_registry_covers_representative_phase_2a_error_codes() -> None:
    """Expose stable metadata for the CLI hardening error taxonomy."""
    codes = registered_error_codes()

    assert isinstance(codes, set)
    assert {
        "INVALID_COMMAND",
        "PROJECT_NOT_FOUND",
        "STALE_ARTIFACT_FINGERPRINT",
        "CONFIRMATION_REQUIRED",
        "MUTATION_RECOVERY_REQUIRED",
        "IDEMPOTENCY_KEY_REUSED",
        "BROWNFIELD_DRAFT_NOT_FOUND",
        "BROWNFIELD_APPROVAL_CHAIN_MISMATCH",
        "BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED",
    }.issubset(codes)
    assert "STALE_FINGERPRINT" not in codes
    assert codes == {code.value for code in ErrorCode}
    assert all(isinstance(code, str) for code in codes)


def test_registry_covers_authority_review_decision_error_codes() -> None:
    """Expose stable authority review decision error taxonomy."""
    codes = registered_error_codes()

    for code in [
        "AUTHORITY_REVIEW_REQUIRED",
        "AUTHORITY_NOT_PENDING",
        "AUTHORITY_ALREADY_DECIDED",
        "AUTHORITY_SOURCE_CHANGED",
        "AUTHORITY_SOURCE_UNAVAILABLE",
        "AUTHORITY_REVIEW_INCOMPLETE",
        "AUTHORITY_GUARD_INCOMPLETE",
    ]:
        assert code in codes, code


def test_registry_covers_authority_curation_error_codes() -> None:
    """Expose stable authority curation error taxonomy."""
    codes = registered_error_codes()

    for code in [
        "AUTHORITY_FEEDBACK_TARGET_NOT_FOUND",
        "AUTHORITY_FEEDBACK_SCHEMA_INVALID",
        "AUTHORITY_CURATED_DIFF_UNBOUNDED",
        "AUTHORITY_CURATION_MAX_ITERATIONS",
        "AUTHORITY_REPAIR_INTENT_INVALID",
        "AUTHORITY_REPAIR_TARGET_NOT_FOUND",
    ]:
        assert code in codes, code

    assert (
        error_metadata(ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND).description
        == "Authority feedback references a target that does not exist."
    )


def test_authority_repair_v2_error_codes_are_registered() -> None:
    """Expose stable metadata for repair-menu contract failures."""
    invalid = error_metadata(ErrorCode.AUTHORITY_REPAIR_INTENT_INVALID)
    missing = error_metadata(ErrorCode.AUTHORITY_REPAIR_TARGET_NOT_FOUND)

    assert invalid.code == "AUTHORITY_REPAIR_INTENT_INVALID"
    assert invalid.default_exit_code == 1
    assert invalid.retryable is False
    assert missing.code == "AUTHORITY_REPAIR_TARGET_NOT_FOUND"
    assert missing.default_exit_code == 1
    assert missing.retryable is False


def test_compiled_authority_schema_unsupported_error_is_registered() -> None:
    """Expose stable metadata for unsupported compiled-authority artifacts."""
    expected_exit_code = 4
    metadata = error_metadata(ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED)

    assert metadata.code == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert metadata.default_exit_code == expected_exit_code
    assert metadata.retryable is False
    assert metadata.description == "Compiled authority artifact schema is unsupported."


def test_authority_compile_stale_guard_error_codes_are_registered() -> None:
    """Expose stable metadata for authority compile stale guards."""
    expected_exit_code = 3

    assert ErrorCode.STALE_SETUP_STATUS.value == "STALE_SETUP_STATUS"
    assert ErrorCode.STALE_SPEC_HASH.value == "STALE_SPEC_HASH"
    assert ErrorCode.STALE_SPEC_VERSION.value == "STALE_SPEC_VERSION"
    for code in (
        ErrorCode.STALE_SETUP_STATUS,
        ErrorCode.STALE_SPEC_HASH,
        ErrorCode.STALE_SPEC_VERSION,
    ):
        metadata = error_metadata(code)
        assert metadata.default_exit_code == expected_exit_code
        assert metadata.retryable is True


def test_scope_extension_guard_error_codes_are_registered() -> None:
    """Expose stable metadata for scope-extension availability guards."""
    codes = registered_error_codes()

    assert "SCOPE_EXTENSION_NOT_AVAILABLE" in codes
    assert "SCOPE_EXTENSION_UNRESOLVED_WORK" in codes
    assert error_metadata(ErrorCode.SCOPE_EXTENSION_NOT_AVAILABLE).description == (
        "Project scope extension is not available."
    )
    assert error_metadata(ErrorCode.SCOPE_EXTENSION_UNRESOLVED_WORK).description == (
        "Project scope extension is blocked by unresolved work."
    )


@pytest.mark.parametrize(
    ("code", "exit_code", "retryable"),
    [
        (code, exit_code, retryable)
        for code, (exit_code, retryable) in EXPECTED_ERROR_METADATA.items()
    ],
)
def test_error_metadata_has_stable_exit_codes(
    code: ErrorCode,
    exit_code: int,
    retryable: bool,
) -> None:
    """Keep error metadata stable for CLI callers."""
    metadata = error_metadata(code)

    assert metadata.code == code.value
    assert metadata.default_exit_code == exit_code
    assert metadata.retryable is retryable
    assert metadata.description


def test_error_metadata_table_covers_every_error_code() -> None:
    """Ensure every defined error code has explicit mapping coverage."""
    assert set(EXPECTED_ERROR_METADATA) == set(ErrorCode)


def test_workbench_error_uses_metadata_defaults() -> None:
    """Build WorkbenchError instances from registry metadata."""
    error = workbench_error(ErrorCode.PROJECT_NOT_FOUND)
    metadata = error_metadata(ErrorCode.PROJECT_NOT_FOUND)

    assert error.code == "PROJECT_NOT_FOUND"
    assert error.message == metadata.description
    assert error.details == {}
    assert error.remediation == []
    assert error.exit_code == metadata.default_exit_code
    assert error.retryable is False


def test_workbench_error_accepts_string_codes_and_overrides() -> None:
    """Allow command code paths to override caller-facing error payloads."""
    error = workbench_error(
        "STALE_ARTIFACT_FINGERPRINT",
        message="State changed while the command was running.",
        details={"expected": "abc", "actual": "def"},
        remediation=["Refresh and retry the command."],
    )

    assert error.code == "STALE_ARTIFACT_FINGERPRINT"
    assert error.message == "State changed while the command was running."
    assert error.details == {"expected": "abc", "actual": "def"}
    assert error.remediation == ["Refresh and retry the command."]
    assert (
        error.exit_code
        == error_metadata(ErrorCode.STALE_ARTIFACT_FINGERPRINT).default_exit_code
    )
    assert error.retryable is True
