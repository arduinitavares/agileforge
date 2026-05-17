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
    ErrorCode.SPEC_COMPILE_FAILED: (1, True),
    ErrorCode.AUTHORITY_NOT_ACCEPTED: (4, False),
    ErrorCode.AUTHORITY_NOT_COMPILED: (4, False),
    ErrorCode.AUTHORITY_ACCEPTANCE_MISMATCH: (4, False),
    ErrorCode.AUTHORITY_INVARIANTS_INVALID: (4, False),
    ErrorCode.AUTHORITY_REVIEW_REQUIRED: (4, False),
    ErrorCode.AUTHORITY_NOT_PENDING: (4, False),
    ErrorCode.AUTHORITY_ALREADY_DECIDED: (10, False),
    ErrorCode.AUTHORITY_SOURCE_CHANGED: (11, True),
    ErrorCode.AUTHORITY_REVIEW_INCOMPLETE: (20, False),
    ErrorCode.AUTHORITY_GUARD_INCOMPLETE: (2, False),
    ErrorCode.SCHEMA_VERSION_MISMATCH: (5, True),
    ErrorCode.STALE_STATE: (3, True),
    ErrorCode.STALE_ARTIFACT_FINGERPRINT: (3, True),
    ErrorCode.STALE_CONTEXT_FINGERPRINT: (3, True),
    ErrorCode.STALE_AUTHORITY_VERSION: (3, True),
    ErrorCode.CONFIRMATION_REQUIRED: (2, False),
    ErrorCode.ACTIVE_STATE_BLOCKS_DELETE: (4, False),
    ErrorCode.MUTATION_FAILED: (1, False),
    ErrorCode.MUTATION_ROLLBACK: (1, True),
    ErrorCode.MUTATION_IN_PROGRESS: (1, True),
    ErrorCode.MUTATION_RECOVERY_REQUIRED: (1, True),
    ErrorCode.MUTATION_RESUME_CONFLICT: (1, True),
    ErrorCode.MUTATION_RECOVERY_INVALID: (10, False),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: (2, False),
    ErrorCode.MUTATION_NOT_FOUND: (4, False),
    ErrorCode.WORKFLOW_SESSION_FAILED: (1, True),
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
        "AUTHORITY_REVIEW_INCOMPLETE",
        "AUTHORITY_GUARD_INCOMPLETE",
    ]:
        assert code in codes, code


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
