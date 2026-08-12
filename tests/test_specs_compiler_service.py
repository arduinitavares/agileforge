"""Stable service behavior around compiled Authority artifacts and exports."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from services import specs
from services.agent_workbench.error_codes import ErrorCode
from services.specs import compiler_service
from utils.spec_schemas import SpecAuthorityCompilationSuccess


def _success_payload() -> dict[str, Any]:
    return {
        "schema_version": "agileforge.compiled_authority.v3",
        "scope_themes": ["Typed Authority"],
        "domain": "specification",
        "invariants": [],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": ["No eligible normative items."],
        "assumptions": [],
        "source_map": [],
        "compiler_version": compiler_service.SPEC_AUTHORITY_COMPILER_VERSION,
        "prompt_hash": "a" * 64,
    }


def _stored(payload: object) -> SimpleNamespace:
    return SimpleNamespace(compiled_artifact_json=json.dumps(payload))


def test_load_compiled_artifact_returns_typed_success() -> None:
    """Stored v3 success JSON is the authoritative read representation."""
    result = compiler_service.load_compiled_artifact(_stored(_success_payload()))

    assert result.ok is True
    assert result.status == "success"
    assert isinstance(result.artifact, SpecAuthorityCompilationSuccess)
    assert result.artifact.scope_themes == ["Typed Authority"]


@pytest.mark.parametrize(
    ("artifact_json", "expected_status", "observed_schema"),
    [
        (None, "missing", None),
        ("{not-json", "invalid_json", None),
        ("[]", "schema_invalid", None),
        (json.dumps({"schema_version": "agileforge.compiled_authority.v2"}),
         "schema_unsupported", "agileforge.compiled_authority.v2"),
        (json.dumps({"schema_version": "agileforge.compiled_authority.v3"}),
         "schema_invalid", "agileforge.compiled_authority.v3"),
        (
            json.dumps(
                {
                    "schema_version": "agileforge.compiled_authority.v3",
                    "error": "SPEC_COMPILATION_FAILED",
                    "reason": "blocked",
                    "blocking_gaps": ["REQ.blocked: unsupported"],
                }
            ),
            "compiler_failure",
            "agileforge.compiled_authority.v3",
        ),
    ],
)
def test_load_compiled_artifact_fails_closed(
    artifact_json: str | None,
    expected_status: str,
    observed_schema: str | None,
) -> None:
    """Missing, malformed, historical, or failure artifacts never read as success."""
    result = compiler_service.load_compiled_artifact(
        SimpleNamespace(compiled_artifact_json=artifact_json)
    )

    assert result.ok is False
    assert result.status == expected_status
    assert result.observed_schema_version == observed_schema


def test_compiled_authority_read_failure_preserves_recovery_context() -> None:
    """Unsupported stored schemas return one stable graph-owned recovery error."""
    load_result = compiler_service.load_compiled_artifact(
        _stored({"schema_version": "agileforge.compiled_authority.v2"})
    )

    failure = compiler_service.compiled_authority_read_failure(
        load_result,
        project_id=5,
        spec_version_id=7,
        authority_id=11,
    )

    assert failure is not None
    assert failure.error_code == ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED
    assert failure.details == {
        "project_id": 5,
        "spec_version_id": 7,
        "authority_id": 11,
        "load_status": "schema_unsupported",
        "observed_schema_version": "agileforge.compiled_authority.v2",
        "required_schema_version": "agileforge.compiled_authority.v3",
    }
    assert failure.remediation == ("agileforge workflow next --project-id 5",)


def test_compiled_authority_read_failure_is_none_for_success() -> None:
    """A valid stored success does not acquire a synthetic read error."""
    result = compiler_service.load_compiled_artifact(_stored(_success_payload()))

    assert (
        compiler_service.compiled_authority_read_failure(
            result,
            project_id=5,
            spec_version_id=7,
            authority_id=11,
        )
        is None
    )


def test_specs_package_exports_only_active_typed_boundaries() -> None:
    """The lazy service package has no compatibility exports for retired paths."""
    assert set(specs.__all__) == {
        "check_spec_authority_status",
        "compile_spec_authority_for_version",
        "compute_story_input_hash",
        "ensure_accepted_spec_authority",
        "get_compiled_authority_by_version",
        "load_compiled_artifact",
        "validate_story_with_spec_authority",
    }
    assert specs.compile_spec_authority_for_version is (
        compiler_service.compile_spec_authority_for_version
    )


def test_gate_error_orients_to_separate_human_authority_review() -> None:
    """Missing accepted Authority never implies automatic acceptance."""
    error = compiler_service.SpecAuthorityGateError.requires_review(29)

    assert "workflow next --project-id 29" in str(error)
    assert "separate Authority review" in str(error)
