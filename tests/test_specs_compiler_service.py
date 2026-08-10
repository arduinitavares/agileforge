"""Tests for specs compiler service."""

import json
import time
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Project,
    SpecAuthorityAcceptance,
    SpecAuthorityStatus,
    SpecRegistry,
)
from services.agent_workbench.authority_projection import (
    pending_authority_fingerprint,
)
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from tests.authority_assumption_fixtures import (
    free_text_assumption,
    historical_v2_compiled_authority,
)
from tests.typing_helpers import make_tool_context, require_id
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils import failure_artifacts
from utils.agileforge_spec_profile import TechnicalSpecArtifact
from utils.failure_artifacts import AgentInvocationError
from utils.spec_authority_assumptions import ItemStatusAssumptionClaim
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerInput,
    SpecAuthorityCompilerOutput,
    SpecAuthoritySourceLevel,
    UserInteractionParams,
)

_SCHEMA_RETRY_ATTEMPTS = 1
_TOTAL_BLOCKED_MUST_ITEMS = 2
_EXPECTED_FOCUSED_RETRY_CALLS = 2
_EXPECTED_CROSS_SUCCESS_SOURCE_EVIDENCE_COUNT = 2
_EXPECTED_REPAIR_CALLS = 2
_EXPECTED_COVERAGE_REPAIR_CALLS = 5
_EXPECTED_COVERAGE_REPAIR_FAIL_FAST_CALLS = 4


def _scope_merge_spec(*item_ids: str) -> TechnicalSpecArtifact:
    """Return a full accepted spec for scope-aware merge assertions."""
    payload = _agileforge_spec_profile_payload()
    payload["items"] = [
        {
            "id": item_id,
            "type": "REQ",
            "status": "accepted",
            "level": "MUST",
            "title": item_id,
            "statement": f"The system MUST implement {item_id}.",
            "verification": "system-test",
            "acceptance": [f"{item_id} is implemented."],
        }
        for item_id in item_ids
    ]
    return TechnicalSpecArtifact.model_validate(payload)


def _scope_merge_success(
    assumption: dict[str, object],
) -> SpecAuthorityCompilationSuccess:
    """Return a minimal compiler success with one typed assumption."""
    return SpecAuthorityCompilationSuccess.model_validate(
        {
            "scope_themes": ["Scope merge"],
            "domain": None,
            "invariants": [],
            "eligible_feature_rules": [],
            "rejected_features": [],
            "gaps": [],
            "assumptions": [assumption],
            "source_map": [],
            "compiler_version": "3.0.0",
            "prompt_hash": "a" * 64,
        }
    )


def _true_count_claim(*, count: int, source_item_ids: list[str]) -> dict[str, object]:
    """Return a count claim grounded to the given canonical item IDs."""
    return {
        "kind": "accepted_normative_count",
        "count": count,
        "provenance": {
            "source": "structured_spec",
            "artifact_id": "SPEC.test",
            "source_item_ids": source_item_ids,
        },
    }


def _true_set_claim(*, item_ids: list[str]) -> dict[str, object]:
    """Return a set claim grounded to the given canonical item IDs."""
    return {
        "kind": "accepted_normative_set",
        "item_ids": item_ids,
        "provenance": {
            "source": "structured_spec",
            "artifact_id": "SPEC.test",
            "source_item_ids": item_ids,
        },
    }


def _item_status_claim(item_id: str) -> dict[str, object]:
    """Return an accepted item-status claim grounded to one source item."""
    return {
        "kind": "item_status",
        "item_id": item_id,
        "status": "accepted",
        "provenance": {
            "source": "structured_spec",
            "artifact_id": "SPEC.test",
            "source_item_ids": [item_id],
        },
    }


def _compiled_success_json() -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        domain=None,
        invariants=[
            Invariant(
                id="INV-0123456789abcdef",
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="email"),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _stored_compiled_success_json() -> str:
    return json.dumps(v3_compiled_authority_payload())


def v3_compiled_authority_payload() -> dict[str, Any]:
    """Return a stored v3 compiled-authority payload fixture."""
    return {
        "schema_version": "agileforge.compiled_authority.v3",
        "scope_themes": ["Payments"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0123456789abcdef",
                "type": "REQUIRED_FIELD",
                "source_item_id": "REQ.payments.email",
                "source_level": "MUST",
                "parameters": {"field_name": "email"},
            }
        ],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [
            free_text_assumption("Audit evidence is retained."),
            {
                "kind": "item_status",
                "item_id": "REQ.alpha",
                "status": "accepted",
                "provenance": {
                    "source": "structured_spec",
                    "artifact_id": "SPEC.loader",
                    "source_item_ids": ["REQ.alpha"],
                },
            },
            {
                "kind": "accepted_normative_count",
                "count": 2,
                "provenance": {
                    "source": "structured_spec",
                    "artifact_id": "SPEC.loader",
                    "source_item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
                },
            },
            {
                "kind": "accepted_normative_set",
                "item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
                "provenance": {
                    "source": "structured_spec",
                    "artifact_id": "SPEC.loader",
                    "source_item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
                },
            },
        ],
        "source_map": [],
        "compiler_version": "3.0.0",
        "prompt_hash": "a" * 64,
        "ir_schema_version": None,
        "ir_provenance": None,
        "source_units": [],
        "requirement_candidates": [],
        "authority_mappings": [],
        "ir_packet_limits": None,
    }


def legacy_compiled_authority_payload() -> dict[str, Any]:
    """Return a legacy stored payload fixture without schema_version."""
    payload = v3_compiled_authority_payload()
    payload.pop("schema_version")
    invariant = payload["invariants"][0]
    assert isinstance(invariant, dict)
    parameters = invariant["parameters"]
    assert isinstance(parameters, dict)
    parameters["source_item_id"] = invariant.pop("source_item_id")
    parameters["source_level"] = invariant.pop("source_level")
    payload["compiler_version"] = "3.0.0"
    return payload


def _compiled_failure_json() -> str:
    failure = SpecAuthorityCompilationFailure(
        error="COMPILATION_FAILED",
        reason="Missing scope",
        blocking_gaps=["scope"],
    )
    return SpecAuthorityCompilerOutput(root=failure).model_dump_json()


def _stored_compiler_failure_json() -> str:
    return json.dumps(
        {
            "schema_version": "agileforge.compiled_authority.v3",
            "error": "COMPILATION_FAILED",
            "reason": "Missing scope",
            "blocking_gaps": ["scope"],
        }
    )


def _vacant_success_json() -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["notes-only"],
        domain=None,
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _raw_compiler_output_json() -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        domain=None,
        invariants=[
            Invariant(
                id="INV-0123456789abcdef",
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="email"),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-0123456789abcdef",
                excerpt="The payload must include email.",
                location=None,
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _duplicate_required_field_compiler_output_json() -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Quality"],
        domain=None,
        invariants=[
            Invariant(
                id="INV-1111111111111111",
                type=InvariantType.REQUIRED_FIELD,
                source_item_id="REQ.test.audit",
                source_level="MUST",
                parameters=RequiredFieldParams(field_name="email"),
            ),
            Invariant(
                id="INV-2222222222222222",
                type=InvariantType.REQUIRED_FIELD,
                source_item_id="REQ.test.audit",
                source_level="MUST",
                parameters=RequiredFieldParams(field_name="email"),
            ),
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _structured_retry_success_payload() -> dict[str, Any]:
    """Return a compile-success payload valid against structured source checks."""
    return {
        "schema_version": "agileforge.compiled_authority.v3",
        "scope_themes": ["Audit"],
        "domain": "operations",
        "invariants": [
            {
                "id": "INV-0123456789abcdef",
                "type": "REQUIRED_FIELD",
                "source_item_id": "REQ.test.audit",
                "source_level": "MUST",
                "parameters": {"field_name": "audit evidence"},
            }
        ],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0123456789abcdef",
                "excerpt": "The system MUST record audit evidence.",
                "location": "REQ.test.audit",
            }
        ],
        "compiler_version": "3.0.0",
        "prompt_hash": "a" * 64,
    }


def _structured_retry_invalid_payload() -> dict[str, Any]:
    """Return schema-invalid structured retry output for retry tests."""
    payload = _structured_retry_success_payload()
    payload["invariants"] = [
        {
            **cast("dict[str, Any]", payload["invariants"][0]),
            "parameters": {"unexpected": "value"},
        }
    ]
    return payload


def _agileforge_spec_profile_payload() -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.test",
        "title": "Test Spec",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Test summary.",
        "problem_statement": "Test problem.",
        "items": [
            {
                "id": "REQ.test.audit",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Audit evidence",
                "statement": "The system MUST record audit evidence.",
                "verification": "system-test",
                "acceptance": ["Audit evidence is stored for each operation."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def _agileforge_spec_profile_json() -> str:
    return json.dumps(_agileforge_spec_profile_payload())


def _accepted_multi_item_spec_profile_payload() -> dict[str, object]:
    payload = _agileforge_spec_profile_payload()
    payload["items"] = [
        {
            "id": "REQ.todo-create",
            "type": "REQ",
            "status": "accepted",
            "level": "MUST",
            "title": "Create todos",
            "statement": "The app MUST create a todo when Enter is pressed.",
            "verification": "system-test",
            "acceptance": ["Pressing Enter creates a new todo."],
        },
        {
            "id": "REQ.todo-toggle",
            "type": "REQ",
            "status": "accepted",
            "level": "MUST_NOT",
            "title": "Toggle without deleting",
            "statement": "The app MUST_NOT delete a todo when it is toggled.",
            "verification": "system-test",
            "acceptance": ["Toggling changes completion state without deletion."],
        },
        {
            "id": "REQ.todo-color",
            "type": "REQ",
            "status": "accepted",
            "level": "SHOULD",
            "title": "Highlight todos",
            "statement": "The app SHOULD highlight the active todo.",
            "verification": "inspection",
            "acceptance": ["The active todo is visually distinct."],
        },
    ]
    return payload


def _accepted_multi_item_spec_profile_json() -> str:
    return normalize_spec_content_for_registry(
        json.dumps(_accepted_multi_item_spec_profile_payload())
    ).content


def _canonical_agileforge_spec_profile_json() -> str:
    return normalize_spec_content_for_registry(_agileforge_spec_profile_json()).content


def _behavioral_payload_json(
    source_item_id: str, source_level: SpecAuthoritySourceLevel
) -> str:
    if source_item_id == "REQ.todo-create":
        trigger = "Enter is pressed"
        target = "todo"
        expected_response = "create a todo"
        excerpt = "The app MUST create a todo when Enter is pressed."
    elif source_item_id == "REQ.todo-toggle":
        trigger = "todo is toggled"
        target = "todo"
        expected_response = "do not delete a todo"
        excerpt = "The app MUST_NOT delete a todo when it is toggled."
    else:
        trigger = "user action"
        target = source_item_id
        expected_response = f"Honor {source_item_id}."
        excerpt = f"{source_item_id}."

    success = SpecAuthorityCompilationSuccess(
        scope_themes=["TodoMVC"],
        domain="todo",
        invariants=[
            Invariant(
                id="INV-0123456789abcdef",
                type=InvariantType.USER_INTERACTION,
                source_item_id=source_item_id,
                source_level=source_level,
                parameters=UserInteractionParams(
                    trigger=trigger,
                    target=target,
                    expected_response=expected_response,
                ),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-0123456789abcdef",
                excerpt=excerpt,
                location=source_item_id,
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _focused_repair_spec_profile_payload() -> dict[str, object]:
    payload = _agileforge_spec_profile_payload()
    payload["items"] = [
        {
            "id": "REQ.payments.email",
            "type": "REQ",
            "status": "accepted",
            "level": "MUST",
            "title": "Collect customer email",
            "statement": "The system must collect customer email.",
            "verification": "system-test",
            "acceptance": ["The system must collect customer email."],
        }
    ]
    return payload


def _source_metadata_failure_json(
    *,
    source_item_id: str,
    invariant_id: str,
    source_excerpt: str | None = None,
) -> str:
    issue: dict[str, object] = {
        "subcode": "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED",
        "message": (
            f"{invariant_id} source_item_id {source_item_id} "
            "lacks supporting real source_map evidence."
        ),
        "invariant_id": invariant_id,
        "source_item_id": source_item_id,
        "expected_source_level": "MUST",
        "repairable": True,
    }
    if source_excerpt is not None:
        issue["source_excerpt"] = source_excerpt
    failure = SpecAuthorityCompilationFailure(
        error="SPEC_COMPILATION_FAILED",
        reason="SOURCE_METADATA_MISMATCH",
        blocking_gaps=[
            f"{invariant_id} source_item_id {source_item_id} "
            "lacks supporting real source_map evidence."
        ],
        source_metadata_issues=[issue],
    )
    return SpecAuthorityCompilerOutput(root=failure).model_dump_json()


def _compiled_success_json_for_source_item(source_item_id: str) -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        domain=None,
        invariants=[
            Invariant(
                id="INV-1111111111111111",
                type=InvariantType.REQUIRED_FIELD,
                source_item_id=source_item_id,
                source_level="MUST",
                parameters=RequiredFieldParams(field_name="email"),
            )
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-1111111111111111",
                excerpt="The system must collect customer email.",
                location=f"{source_item_id}.acceptance[0]",
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def test_normalize_structured_spec_content_canonicalizes_json() -> None:
    """Structured spec profile content is stored in canonical JSON form."""
    raw_json = json.dumps(_agileforge_spec_profile_payload(), indent=2)

    normalized = normalize_spec_content_for_registry(raw_json)

    assert normalized.format == "agileforge.spec.v1"
    assert normalized.spec_hash.startswith("sha256:")
    assert "\n" not in normalized.content
    assert json.loads(normalized.content)["schema_version"] == "agileforge.spec.v1"


def test_normalize_markdown_spec_content_rejects_authority_input() -> None:
    """Authority compilation requires canonical agileforge.spec.v1 JSON."""
    raw_markdown = "# Spec\n\nThe system must record audit evidence.\n"

    with pytest.raises(SpecContentNormalizationError) as exc_info:
        normalize_spec_content_for_registry(raw_markdown)

    assert exc_info.value.error_code == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert "Expected agileforge.spec.v1 JSON" in str(exc_info.value)


def test_normalize_arbitrary_json_rejects_authority_input() -> None:
    """JSON without the AgileForge profile marker is not compiler input."""
    raw_json = json.dumps({"title": "Loose JSON spec"})

    with pytest.raises(SpecContentNormalizationError) as exc_info:
        normalize_spec_content_for_registry(raw_json)

    assert exc_info.value.error_code == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert "schema_version" in str(exc_info.value)


def test_update_spec_and_compile_authority_returns_error_for_invalid_structured_spec(
    sample_project: Project,
) -> None:
    """Invalid structured spec JSON returns a structured compile/update error."""
    from services.specs import compiler_service  # noqa: PLC0415

    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": json.dumps(
                {
                    "schema_version": "agileforge.spec.v1",
                    "artifact_id": "SPEC.invalid",
                }
            ),
        },
        tool_context=None,
    )

    assert result["success"] is False
    assert result["error_code"] == "SPEC_FILE_INVALID"
    assert "Invalid agileforge.spec.v1 content" in result["error"]


def _success_payload_json() -> str:
    return _raw_compiler_output_json()


def _raw_compiler_failure_json() -> str:
    failure = SpecAuthorityCompilationFailure(
        error="COMPILATION_FAILED",
        reason="Missing scope",
        blocking_gaps=["scope"],
    )
    return SpecAuthorityCompilerOutput(root=failure).model_dump_json()


def _create_spec_version(
    session: Session,
    *,
    project_id: int,
    content: str | None = None,
    content_ref: str | None = None,
) -> SpecRegistry:
    if content is None:
        content = _canonical_agileforge_spec_profile_json()
    content = normalize_spec_content_for_registry(content).content
    lineage = seed_accepted_specification(
        session,
        project_id=project_id,
        content=content,
        content_ref=content_ref,
    )
    return lineage.spec


def _create_compiled_authority(
    session: Session,
    *,
    spec_version_id: int,
    artifact_json: str,
) -> CompiledSpecAuthority:
    payload = json.loads(artifact_json)
    assert isinstance(payload, dict)
    compiler_version = payload.get("compiler_version", "3.0.0")
    assert isinstance(compiler_version, str)
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=compiler_version,
        prompt_hash="e" * 64,
        compiled_at=datetime.now(UTC),
        compiled_artifact_json=artifact_json,
        scope_themes='["Payments"]',
        invariants='["REQUIRED_FIELD:email"]',
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    return authority


def test_load_compiled_artifact_returns_success_payload() -> None:
    """Verify the loader round-trips every typed v3 assumption variant."""
    from services.specs.compiler_service import (  # noqa: PLC0415
        CompiledArtifactLoadResult,
        load_compiled_artifact,
    )

    authority = SimpleNamespace(
        compiled_artifact_json=json.dumps(v3_compiled_authority_payload())
    )

    result = load_compiled_artifact(authority)

    assert type(result) is CompiledArtifactLoadResult
    assert is_dataclass(result) is True
    assert [field.name for field in fields(result)] == [
        "status",
        "artifact",
        "error_code",
        "message",
        "observed_schema_version",
        "validation_error",
    ]
    assert result.ok is True
    assert result.status == "success"
    assert result.unsupported is False
    assert result.artifact is not None
    assert result.error_code is None
    assert result.message is None
    assert result.observed_schema_version == "agileforge.compiled_authority.v3"
    assert result.validation_error is None
    assert result.artifact.scope_themes == ["Payments"]
    assert result.artifact.invariants[0].id == "INV-0123456789abcdef"
    assert result.artifact.schema_version == "agileforge.compiled_authority.v3"
    assert result.artifact.invariants[0].source_item_id == "REQ.payments.email"
    assert result.artifact.invariants[0].source_level == "MUST"
    assert [assumption.kind for assumption in result.artifact.assumptions] == [
        "free_text",
        "item_status",
        "accepted_normative_count",
        "accepted_normative_set",
    ]
    with pytest.raises(FrozenInstanceError):
        result.status = "missing"  # type: ignore[misc]


def test_compiled_authority_artifact_json_round_trips_through_loader() -> None:
    """Stored-artifact serializer emits a v3 envelope the loader accepts."""
    from services.specs.compiler_service import (  # noqa: PLC0415
        _compiled_authority_artifact_json,
        load_compiled_artifact,
    )

    success = SpecAuthorityCompilationSuccess.model_validate_json(
        _raw_compiler_output_json()
    )

    artifact_json = _compiled_authority_artifact_json(success)
    payload = json.loads(artifact_json)
    result = load_compiled_artifact(
        SimpleNamespace(compiled_artifact_json=artifact_json)
    )

    assert payload["schema_version"] == "agileforge.compiled_authority.v3"
    assert result.status == "success"
    assert result.artifact is not None
    assert result.artifact.schema_version == "agileforge.compiled_authority.v3"
    assert result.artifact.scope_themes == success.scope_themes


def test_load_compiled_artifact_raw_sniffs_missing_schema_version() -> None:
    """Verify stored artifacts without schema_version fail closed as unsupported."""
    from services.specs.compiler_service import load_compiled_artifact  # noqa: PLC0415

    authority = SimpleNamespace(
        compiled_artifact_json=json.dumps(legacy_compiled_authority_payload())
    )

    result = load_compiled_artifact(authority)

    assert result.ok is False
    assert result.status == "schema_unsupported"
    assert result.unsupported is True
    assert result.artifact is None
    assert result.error_code == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result.message == "Compiled authority artifact schema is unsupported."
    assert result.observed_schema_version is None
    assert result.validation_error is None


def test_load_compiled_artifact_raw_sniffs_wrong_schema_version() -> None:
    """Verify stored artifacts with a non-v3 schema fail before validation."""
    from services.specs.compiler_service import load_compiled_artifact  # noqa: PLC0415

    payload = v3_compiled_authority_payload()
    payload["schema_version"] = "agileforge.compiled_authority.v1"
    authority = SimpleNamespace(compiled_artifact_json=json.dumps(payload))

    result = load_compiled_artifact(authority)

    assert result.ok is False
    assert result.status == "schema_unsupported"
    assert result.unsupported is True
    assert result.artifact is None
    assert result.error_code == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result.message == "Compiled authority artifact schema is unsupported."
    assert result.observed_schema_version == "agileforge.compiled_authority.v1"
    assert result.validation_error is None


def test_load_compiled_artifact_rejects_historical_v2_payload() -> None:
    """Historical v2 rows remain immutable and unsupported at the v3 boundary."""
    from services.specs.compiler_service import load_compiled_artifact  # noqa: PLC0415

    payload = historical_v2_compiled_authority(prompt_hash="a" * 64)
    authority = SimpleNamespace(compiled_artifact_json=json.dumps(payload))

    result = load_compiled_artifact(authority)

    assert result.ok is False
    assert result.status == "schema_unsupported"
    assert result.error_code == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result.observed_schema_version == "agileforge.compiled_authority.v2"


def test_load_compiled_artifact_reports_validation_error_for_invalid_v3_payload() -> (
    None
):
    """Verify invalid v3 payloads expose schema-invalid result details."""
    from services.specs.compiler_service import load_compiled_artifact  # noqa: PLC0415

    payload = v3_compiled_authority_payload()
    payload["invariants"] = "bad"
    authority = SimpleNamespace(compiled_artifact_json=json.dumps(payload))

    result = load_compiled_artifact(authority)

    assert result.ok is False
    assert result.status == "schema_invalid"
    assert result.unsupported is False
    assert result.artifact is None
    assert result.error_code is None
    assert result.message == "Compiled authority artifact failed schema validation."
    assert result.observed_schema_version == "agileforge.compiled_authority.v3"
    assert result.validation_error is not None


def test_load_compiled_artifact_returns_compiler_failure_result() -> None:
    """Verify compiler failure payloads are distinguished after schema sniffing."""
    from services.specs.compiler_service import load_compiled_artifact  # noqa: PLC0415

    authority = SimpleNamespace(compiled_artifact_json=_stored_compiler_failure_json())

    result = load_compiled_artifact(authority)

    assert result.ok is False
    assert result.status == "compiler_failure"
    assert result.unsupported is False
    assert result.artifact is None
    assert result.error_code is None
    assert result.message == "Compiled authority artifact is a compiler failure."
    assert result.observed_schema_version == "agileforge.compiled_authority.v3"
    assert result.validation_error is None


@pytest.mark.parametrize(
    ("artifact_json", "expected_status", "expected_code", "observed_schema"),
    [
        (None, "missing", "COMPILED_AUTHORITY_INVALID", None),
        ("not-json", "invalid_json", "COMPILED_AUTHORITY_INVALID", None),
        (
            json.dumps(
                {
                    **v3_compiled_authority_payload(),
                    "invariants": "not-a-list",
                }
            ),
            "schema_invalid",
            "COMPILED_AUTHORITY_INVALID",
            "agileforge.compiled_authority.v3",
        ),
        (
            json.dumps(historical_v2_compiled_authority(prompt_hash="a" * 64)),
            "schema_unsupported",
            "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED",
            "agileforge.compiled_authority.v2",
        ),
        (
            _stored_compiler_failure_json(),
            "compiler_failure",
            "COMPILED_AUTHORITY_INVALID",
            "agileforge.compiled_authority.v3",
        ),
    ],
)
def test_compiled_authority_read_failure_describes_every_non_success_status(
    artifact_json: str | None,
    expected_status: str,
    expected_code: str,
    observed_schema: str | None,
) -> None:
    """Every stored-artifact load failure has one stable public descriptor."""
    from services.specs.compiler_service import (  # noqa: PLC0415
        compiled_authority_read_failure,
        load_compiled_artifact,
    )

    load_result = load_compiled_artifact(
        SimpleNamespace(compiled_artifact_json=artifact_json)
    )
    failure = compiled_authority_read_failure(
        load_result,
        project_id=17,
        spec_version_id=23,
        authority_id=41,
    )

    assert failure is not None
    assert failure.error_code == expected_code
    assert failure.details == {
        "project_id": 17,
        "spec_version_id": 23,
        "authority_id": 41,
        "load_status": expected_status,
        "observed_schema_version": observed_schema,
        "required_schema_version": "agileforge.compiled_authority.v3",
    }
    assert failure.remediation == ("agileforge workflow next --project-id 17",)
    assert "validation" not in failure.details
    assert "validation_error" not in failure.details
    with pytest.raises(FrozenInstanceError):
        failure.__setattr__("error_code", "changed")


def test_compiled_authority_read_failure_is_none_only_for_success() -> None:
    """A parsed v3 success is the only loader state without a failure."""
    from services.specs.compiler_service import (  # noqa: PLC0415
        compiled_authority_read_failure,
        load_compiled_artifact,
    )

    load_result = load_compiled_artifact(
        SimpleNamespace(compiled_artifact_json=_stored_compiled_success_json())
    )

    assert (
        compiled_authority_read_failure(
            load_result,
            project_id=17,
            spec_version_id=23,
            authority_id=41,
        )
        is None
    )


def test_compiled_authority_schema_unsupported_helpers_use_graph_recovery() -> None:
    """Unsupported artifacts should orient operators through WorkflowDomain."""
    from services.specs.compiler_service import (  # noqa: PLC0415
        COMPILED_AUTHORITY_SCHEMA_VERSION,
        compiled_authority_schema_unsupported_details,
        compiled_authority_schema_unsupported_remediation,
    )

    details = compiled_authority_schema_unsupported_details(
        project_id=7,
        spec_version_id=11,
        observed_schema_version=None,
    )
    remediation = compiled_authority_schema_unsupported_remediation(
        project_id=7,
        spec_version_id=11,
    )

    assert details == {
        "project_id": 7,
        "spec_version_id": 11,
        "observed_schema_version": None,
        "required_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
    }
    assert remediation == ["agileforge workflow next --project-id 7"]


def test_services_package_exports_ensure_accepted_spec_authority() -> None:
    """Verify services package exports ensure accepted spec authority."""
    from services import specs  # noqa: PLC0415
    from services.specs import compiler_service  # noqa: PLC0415

    assert (
        specs.ensure_accepted_spec_authority
        is compiler_service.ensure_accepted_spec_authority
    )


def test_ensure_accepted_spec_authority_reuses_existing_accepted_version(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ensure accepted spec authority reuses existing accepted version."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    monkeypatch.setattr(spec_tools, "engine", session.get_bind(), raising=False)

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )
    acceptance = SpecAuthorityAcceptance(
        project_id=require_id(sample_project.project_id, "project_id"),
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        status="accepted",
        policy="manual",
        decided_by="reviewer",
        decided_at=datetime.now(UTC),
        rationale="Explicitly accepted for test",
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        spec_hash=spec_row.spec_hash,
        pending_authority_id=authority.authority_id,
        authority_fingerprint=pending_authority_fingerprint(authority),
    )
    session.add(acceptance)
    session.commit()

    result = compiler_service.ensure_accepted_spec_authority(
        project_id=require_id(sample_project.project_id, "project_id"),
    )

    assert result == require_id(spec_row.spec_version_id, "spec_version_id")


def test_accepted_authority_reuse_breaks_decision_time_ties_by_id(
    session: Session,
    sample_project: Project,
) -> None:
    """Equal timestamps select the newest inserted accepted decision."""
    from services.specs import compiler_service  # noqa: PLC0415

    project_id = require_id(sample_project.project_id, "project_id")
    decided_at = datetime(2026, 7, 27, tzinfo=UTC)
    first_spec = _create_spec_version(session, project_id=project_id)
    first_authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(first_spec.spec_version_id, "spec_version_id"),
        artifact_json=json.dumps(v3_compiled_authority_payload()),
    )
    session.add(
        SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=require_id(first_spec.spec_version_id, "spec_version_id"),
            status="accepted",
            policy="manual",
            decided_by="reviewer",
            decided_at=decided_at,
            compiler_version=first_authority.compiler_version,
            prompt_hash=first_authority.prompt_hash,
            spec_hash=first_spec.spec_hash,
            pending_authority_id=first_authority.authority_id,
            authority_fingerprint=pending_authority_fingerprint(first_authority),
        )
    )
    session.commit()
    second_spec = _create_spec_version(session, project_id=project_id)
    second_authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(second_spec.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )
    session.add(
        SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=require_id(second_spec.spec_version_id, "spec_version_id"),
            status="accepted",
            policy="manual",
            decided_by="reviewer",
            decided_at=decided_at,
            compiler_version=second_authority.compiler_version,
            prompt_hash=second_authority.prompt_hash,
            spec_hash=second_spec.spec_hash,
            pending_authority_id=second_authority.authority_id,
            authority_fingerprint=pending_authority_fingerprint(second_authority),
        )
    )
    session.commit()

    lookup = compiler_service._lookup_reusable_accepted_authority(
        session,
        project_id=project_id,
    )

    assert lookup.reusable_spec_version_id == second_spec.spec_version_id


def test_ensure_accepted_spec_authority_honors_legacy_tool_update_monkeypatch(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ensure accepted spec authority honors legacy tool update monkeypatch."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    monkeypatch.setattr(spec_tools, "engine", session.get_bind(), raising=False)

    captured: dict[str, object] = {}

    def fake_update(params: object, tool_context: object = None) -> object:
        captured["params"] = params
        captured["tool_context"] = tool_context
        return {
            "success": True,
            "accepted": True,
            "spec_version_id": 777,
            "project_id": require_id(sample_project.project_id, "project_id"),
        }

    monkeypatch.setattr(
        spec_tools,
        "update_spec_and_compile_authority",
        fake_update,
        raising=False,
    )

    result = compiler_service.ensure_accepted_spec_authority(
        project_id=require_id(sample_project.project_id, "project_id"),
        spec_content="# Spec",
        recompile=True,
    )

    assert result == 777  # noqa: PLR2004
    assert captured["params"] == {
        "project_id": require_id(sample_project.project_id, "project_id"),
        "recompile": True,
        "spec_content": "# Spec",
    }
    assert captured["tool_context"] is None


def test_automatic_acceptance_compatibility_api_is_not_public() -> None:
    """Only guarded review/decision services may persist acceptance."""
    from services import specs  # noqa: PLC0415
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    removed_name = "ensure_spec_authority_accepted"
    assert removed_name not in specs.__all__
    assert not hasattr(specs, removed_name)
    assert not hasattr(compiler_service, removed_name)
    assert not hasattr(spec_tools, removed_name)


def test_preview_spec_authority_returns_success_and_updates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify preview spec authority returns success and updates cache."""
    from services.specs import compiler_service  # noqa: PLC0415

    tool_context = make_tool_context()
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )

    result = compiler_service.preview_spec_authority(
        {"content": _canonical_agileforge_spec_profile_json()},
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["compiled_authority"] is not None
    assert (
        tool_context.state["compiled_authority_cached"] == result["compiled_authority"]
    )


def test_preview_spec_authority_iteratively_covers_accepted_must_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured preview compiles each accepted MUST/MUST_NOT item in focus."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[list[str]] = []

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        item_ids = [item["id"] for item in items]
        calls.append(item_ids)
        first_item = items[0]
        assert isinstance(first_item, dict)
        source_item_id = first_item["id"]
        source_level = first_item["level"]
        assert isinstance(source_item_id, str)
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=source_item_id,
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    compiled = SpecAuthorityCompilerOutput.model_validate_json(
        result["compiled_authority"]
    )
    assert isinstance(compiled.root, SpecAuthorityCompilationSuccess)
    covered_item_ids = {
        invariant.source_item_id
        for invariant in compiled.root.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
        and invariant.source_item_id is not None
    }
    assert covered_item_ids == {"REQ.todo-create", "REQ.todo-toggle"}
    assert ["REQ.todo-create"] in calls
    assert ["REQ.todo-toggle"] in calls
    assert ["REQ.todo-color"] not in calls


def test_preview_spec_authority_rejects_unaccounted_iterative_must_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured item pass cannot succeed without source-item coverage."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _compiled_success_json(),
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["details"]["error"] == "STRUCTURED_COVERAGE_INCOMPLETE"
    assert result["details"]["reason"] == "MISSING_ACCEPTED_MUST_AUTHORITY"
    assert result["details"]["blocking_gaps"] == [
        "REQ.todo-create",
        "REQ.todo-toggle",
    ]


def test_preview_spec_authority_coverage_repair_succeeds_with_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing MUST/MUST_NOT coverage gets one explicit focused repair pass."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[dict[str, object]] = []

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        domain_hint = kwargs.get("domain_hint")
        payload = json.loads(spec_content)
        item_ids = [item["id"] for item in payload["items"]]
        calls.append({"item_ids": item_ids, "domain_hint": domain_hint})
        if domain_hint and "failed structured coverage validation" in str(domain_hint):
            item_id = item_ids[0]
            source_level = payload["items"][0]["level"]
            assert f"missing source_item_id: {item_id}" in str(domain_hint)
            assert f"The previous attempt failed to cover {item_id}." in str(
                domain_hint
            )
            assert "single repair attempt" in str(domain_hint)
            return _behavioral_payload_json(
                source_item_id=cast("str", item_id),
                source_level=cast("SpecAuthoritySourceLevel", source_level),
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert len(calls) == _EXPECTED_COVERAGE_REPAIR_CALLS
    repair_hints = [
        str(call["domain_hint"]) for call in calls if call["domain_hint"] is not None
    ]
    assert any(
        "missing source_item_id: REQ.todo-create" in hint for hint in repair_hints
    )
    assert any(
        "missing source_item_id: REQ.todo-toggle" in hint for hint in repair_hints
    )


def test_preview_spec_authority_coverage_repair_fails_closed_on_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage repair does not enter a second metadata repair loop."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[str | None] = []

    def fake_compiler(**kwargs: object) -> str:
        domain_hint = cast("str | None", kwargs.get("domain_hint"))
        calls.append(domain_hint)
        if domain_hint and "failed structured coverage validation" in domain_hint:
            return _source_metadata_failure_json(
                source_item_id="REQ.todo-create",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["details"]["error"] == "STRUCTURED_ITEM_COMPILATION_FAILED"
    assert result["details"]["reason"] == "FOCUSED_ITEM_AUTHORITY_FAILED"
    assert len(calls) == _EXPECTED_COVERAGE_REPAIR_FAIL_FAST_CALLS
    assert (
        sum(
            1
            for hint in calls
            if hint and "failed structured coverage validation" in hint
        )
        == 1
    )


def test_preview_spec_authority_rejects_vacant_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normalized zero-invariant success is not usable compiled authority."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _vacant_success_json(),
    )

    result = compiler_service.preview_spec_authority(
        {"content": _canonical_agileforge_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["details"]["error"] == "SPEC_AUTHORITY_VACANT"
    assert result["details"]["reason"] == "NO_INVARIANTS_EXTRACTED"
    assert result["details"]["blocking_gaps"] == ["No invariants extracted from spec"]


def test_preview_spec_authority_recovers_when_structured_full_pass_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focused item passes can succeed even when the full orienting pass fails."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[list[str]] = []

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        item_ids = [item["id"] for item in items]
        calls.append(item_ids)
        if len(items) > 1:
            return _raw_compiler_failure_json()
        first_item = items[0]
        assert isinstance(first_item, dict)
        source_item_id = first_item["id"]
        source_level = first_item["level"]
        assert isinstance(source_item_id, str)
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=source_item_id,
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    compiled = SpecAuthorityCompilerOutput.model_validate_json(
        result["compiled_authority"]
    )
    assert isinstance(compiled.root, SpecAuthorityCompilationSuccess)
    covered_item_ids = {
        invariant.source_item_id
        for invariant in compiled.root.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
        and invariant.source_item_id is not None
    }
    assert covered_item_ids == {"REQ.todo-create", "REQ.todo-toggle"}
    assert calls[0] == ["REQ.todo-create", "REQ.todo-toggle", "REQ.todo-color"]


def test_preview_spec_authority_repairs_merged_structured_source_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merged focused successes are re-normalized against structured spec text."""
    from services.specs import compiler_service  # noqa: PLC0415

    def focused_success(item_id: str) -> SpecAuthorityCompilationSuccess:
        trigger = "Enter is pressed" if item_id == "REQ.todo-create" else "todo"
        target = "todo"
        expected_response = (
            "create a todo"
            if item_id == "REQ.todo-create"
            else "change completion state without deletion"
        )
        source_level: SpecAuthoritySourceLevel = (
            "MUST" if item_id == "REQ.todo-create" else "MUST_NOT"
        )
        return SpecAuthorityCompilationSuccess(
            scope_themes=["TodoMVC"],
            domain="todo",
            invariants=[
                Invariant(
                    id=(
                        "INV-1111111111111111"
                        if item_id == "REQ.todo-create"
                        else "INV-2222222222222222"
                    ),
                    type=InvariantType.USER_INTERACTION,
                    source_item_id=item_id,
                    source_level=source_level,
                    parameters=UserInteractionParams(
                        trigger=trigger,
                        target=target,
                        expected_response=expected_response,
                    ),
                )
            ],
            eligible_feature_rules=[],
            gaps=[],
            assumptions=[],
            source_map=[],
            compiler_version="3.0.0",
            prompt_hash="a" * 64,
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_and_normalize_spec_authority",
        lambda **_: compiler_service._NormalizedCompilerInvocation(
            raw_json=_raw_compiler_failure_json(),
            output=SpecAuthorityCompilerOutput.model_validate_json(
                _raw_compiler_failure_json()
            ),
        ),
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_focused_structured_item_authority",
        lambda _artifact, *, item_id, **_kwargs: focused_success(cast("str", item_id)),
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    compiled = SpecAuthorityCompilerOutput.model_validate_json(
        result["compiled_authority"]
    )
    assert isinstance(compiled.root, SpecAuthorityCompilationSuccess)
    source_locations = {entry.location for entry in compiled.root.source_map}
    assert "REQ.todo-create.statement" in source_locations
    assert "REQ.todo-toggle.acceptance[0]" in source_locations
    assert "REQ.todo-create" not in source_locations
    assert "REQ.todo-toggle" not in source_locations


def test_preview_spec_authority_retries_transient_focused_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient focused-item schema failure should not abort compilation."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[list[str]] = []
    focused_attempts: dict[str, int] = {}

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        item_ids = [item["id"] for item in items]
        calls.append(item_ids)
        if len(items) > 1:
            return _raw_compiler_failure_json()

        item_id = item_ids[0]
        focused_attempts[item_id] = focused_attempts.get(item_id, 0) + 1
        if item_id == "REQ.todo-create" and focused_attempts[item_id] == 1:
            return "{"

        first_item = items[0]
        assert isinstance(first_item, dict)
        source_level = first_item["level"]
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=item_id,
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    compiled = SpecAuthorityCompilerOutput.model_validate_json(
        result["compiled_authority"]
    )
    assert isinstance(compiled.root, SpecAuthorityCompilationSuccess)
    covered_item_ids = {
        invariant.source_item_id
        for invariant in compiled.root.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
        and invariant.source_item_id is not None
    }
    assert covered_item_ids == {"REQ.todo-create", "REQ.todo-toggle"}
    assert focused_attempts["REQ.todo-create"] == _EXPECTED_FOCUSED_RETRY_CALLS


def test_preview_spec_authority_reports_persistent_focused_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent focused item failure should identify the failed item."""
    from services.specs import compiler_service  # noqa: PLC0415

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        if len(items) > 1:
            return _raw_compiler_failure_json()
        first_item = items[0]
        assert isinstance(first_item, dict)
        item_id = first_item["id"]
        if item_id == "REQ.todo-create":
            return _vacant_success_json()
        source_level = first_item["level"]
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=cast("str", item_id),
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["details"]["error"] == "STRUCTURED_ITEM_COMPILATION_FAILED"
    assert result["details"]["reason"] == "FOCUSED_ITEM_AUTHORITY_FAILED"
    assert result["details"]["blocking_gaps"] == [
        "BLOCKED_REVIEW: 1/2 accepted MUST/MUST_NOT items did not compile into "
        "authority; downstream planning is blocked until the source spec item is "
        "fixed or explicitly marked non-accepted/proposed.",
        "REQ.todo-create: SPEC_AUTHORITY_VACANT - "
        "NO_INVARIANTS_EXTRACTED: No invariants extracted from spec",
    ]


def test_preview_spec_authority_schema_retry_adds_feedback_for_focused_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focused item schema retry should add bounded schema feedback."""
    from services.specs import compiler_service  # noqa: PLC0415

    focused_domain_hints: list[str | None] = []

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        domain_hint = kwargs.get("domain_hint")
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        if len(items) > 1:
            return _raw_compiler_failure_json()

        first_item = items[0]
        assert isinstance(first_item, dict)
        item_id = first_item["id"]
        if item_id == "REQ.todo-create":
            focused_domain_hints.append(cast("str | None", domain_hint))
            if len(focused_domain_hints) == _SCHEMA_RETRY_ATTEMPTS:
                return "{"

        source_level = first_item["level"]
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=cast("str", item_id),
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert focused_domain_hints == [
        None,
        compiler_service._SCHEMA_RETRY_FEEDBACK,
    ]


def test_preview_spec_authority_does_not_retry_semantic_focused_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focused item semantic/source failures must not get a schema retry."""
    from services.specs import compiler_service  # noqa: PLC0415

    focused_attempts: dict[str, int] = {}

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        if len(items) > 1:
            return _raw_compiler_failure_json()

        first_item = items[0]
        assert isinstance(first_item, dict)
        item_id = cast("str", first_item["id"])
        focused_attempts[item_id] = focused_attempts.get(item_id, 0) + 1
        if item_id == "REQ.todo-create":
            invalid_payload = {
                "schema_version": "agileforge.compiled_authority.v3",
                "scope_themes": ["Audit"],
                "domain": "todo",
                "invariants": [
                    {
                        "id": "INV-0123456789abcdef",
                        "type": "DATA_CONTRACT",
                        "source_item_id": item_id,
                        "source_level": "MUST_NOT",
                        "parameters": {
                            "subject": "todo",
                            "fields": ["id"],
                            "rule": "create a todo",
                        },
                    }
                ],
                "eligible_feature_rules": [],
                "rejected_features": [],
                "gaps": [],
                "assumptions": [],
                "source_map": [
                    {
                        "invariant_id": "INV-0123456789abcdef",
                        "excerpt": "The app MUST create a todo when Enter is pressed.",
                        "location": item_id,
                    }
                ],
                "compiler_version": "3.0.0",
                "prompt_hash": "a" * 64,
            }
            return json.dumps(invalid_payload)

        source_level = first_item["level"]
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=item_id,
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert focused_attempts["REQ.todo-create"] == _SCHEMA_RETRY_ATTEMPTS


def test_preview_spec_authority_preserves_focused_schema_retry_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focused failure details should retain both schema-retry attempts."""
    from services.specs import compiler_service  # noqa: PLC0415

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        if len(items) > 1:
            return _raw_compiler_failure_json()

        first_item = items[0]
        assert isinstance(first_item, dict)
        item_id = cast("str", first_item["id"])
        if item_id == "REQ.todo-create":
            if kwargs.get("domain_hint") is None:
                return "{"
            return json.dumps(_structured_retry_invalid_payload())

        source_level = first_item["level"]
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=item_id,
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    blocking_gaps = result["details"]["blocking_gaps"]
    assert any("attempt_1" in gap and "INVALID_JSON" in gap for gap in blocking_gaps)
    assert any(
        "attempt_2" in gap and "JSON_VALIDATION_FAILED" in gap for gap in blocking_gaps
    )


def test_preview_spec_authority_returns_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify preview spec authority returns failure envelope."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_failure_json(),
    )

    result = compiler_service.preview_spec_authority(
        {"content": _canonical_agileforge_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["error"] == "Compilation failed"
    assert result["details"]["error"] == "COMPILATION_FAILED"
    assert result["details"]["reason"] == "Missing scope"


def test_preview_spec_authority_returns_invalid_input_envelope() -> None:
    """Verify preview spec authority returns invalid input envelope."""
    from services.specs import compiler_service  # noqa: PLC0415

    result = compiler_service.preview_spec_authority({}, tool_context=None)

    assert result["success"] is False
    assert result["error"].startswith("Invalid input: ")


def test_preview_spec_authority_returns_unexpected_exception_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify preview spec authority returns unexpected exception error."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: (_ for _ in ()).throw(RuntimeError("preview boom")),
    )

    with caplog.at_level("ERROR"):
        result = compiler_service.preview_spec_authority(
            {"content": "# Spec"},
            tool_context=make_tool_context(),
        )

    assert result == {"success": False, "error": "preview boom"}
    assert any(
        record.levelname == "ERROR"
        and "preview_spec_authority failed" in record.getMessage()
        for record in caplog.records
    )


def test_preview_spec_authority_honors_tool_compiler_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify preview spec authority honors tool compiler monkeypatch."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    tool_context = make_tool_context()
    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )

    result = compiler_service.preview_spec_authority(
        {"content": _canonical_agileforge_spec_profile_json()},
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert (
        tool_context.state["compiled_authority_cached"] == result["compiled_authority"]
    )


def test_default_compiler_invocation_rejects_unstructured_spec_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler invocation requires canonical agileforge.spec.v1 JSON."""
    from services.specs import compiler_service  # noqa: PLC0415

    captured: list[SpecAuthorityCompilerInput] = []

    async def fake_invoke(
        payload: SpecAuthorityCompilerInput,
        *,
        compiler_model: str | None = None,
    ) -> str:
        del compiler_model
        captured.append(payload)
        return _success_payload_json()

    monkeypatch.setattr(
        "services.specs.compiler_service._invoke_spec_authority_compiler_async",
        fake_invoke,
    )

    with pytest.raises(SpecContentNormalizationError) as exc_info:
        compiler_service._default_invoke_spec_authority_compiler(
            spec_content="# Spec\n\nThe system must record audit evidence.",
            content_ref=None,
            project_id=4,
            spec_version_id=9,
        )

    assert exc_info.value.error_code == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert captured == []


def test_default_compiler_invocation_marks_structured_spec_source_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler input should identify canonical structured AgileForge spec JSON."""
    from services.specs import compiler_service  # noqa: PLC0415

    captured: list[SpecAuthorityCompilerInput] = []

    async def fake_invoke(
        payload: SpecAuthorityCompilerInput,
        *,
        compiler_model: str | None = None,
    ) -> str:
        del compiler_model
        captured.append(payload)
        return _success_payload_json()

    monkeypatch.setattr(
        "services.specs.compiler_service._invoke_spec_authority_compiler_async",
        fake_invoke,
    )

    compiler_service._default_invoke_spec_authority_compiler(
        spec_content=json.dumps(_agileforge_spec_profile_payload()),
        content_ref=None,
        project_id=4,
        spec_version_id=9,
    )

    assert len(captured) == 1
    assert captured[0].spec_source_format == "agileforge.spec.v1"


def test_default_compiler_invocation_passes_compiler_model_to_async_invoker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler model override should reach the async agent invocation seam."""
    from services.specs import compiler_service  # noqa: PLC0415

    captured: list[str | None] = []

    async def fake_invoke(
        payload: SpecAuthorityCompilerInput,
        *,
        compiler_model: str | None = None,
    ) -> str:
        del payload
        captured.append(compiler_model)
        return _success_payload_json()

    monkeypatch.setattr(
        "services.specs.compiler_service._invoke_spec_authority_compiler_async",
        fake_invoke,
    )

    compiler_service._default_invoke_spec_authority_compiler(
        spec_content=json.dumps(_agileforge_spec_profile_payload()),
        content_ref=None,
        project_id=4,
        spec_version_id=9,
        compiler_model="openrouter/openai/gpt-5.2",
    )

    assert captured == ["openrouter/openai/gpt-5.2"]


def test_compiler_agent_override_rechecks_schema_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override agent construction should observe the current schema-disable flag."""
    from adapters.adk.agents import specification as agent  # noqa: PLC0415

    monkeypatch.setattr(agent, "is_spec_compiler_schema_disabled", lambda: True)

    built = agent.build_spec_authority_compiler_agent(
        compiler_model="openrouter/openai/gpt-5.2"
    )

    assert getattr(built, "output_schema", None) is None


def test_compile_spec_authority_for_version_persists_authority(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify compile spec authority for version persists authority."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    tool_context = make_tool_context()

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["cached"] is False
    assert result["recompiled"] is False
    assert result["spec_version_id"] == require_id(
        spec_row.spec_version_id, "spec_version_id"
    )
    assert result["content_source"] == "content"
    assert result["compiler_version"] is not None
    assert not hasattr(sample_project, "compiled_authority_json")
    assert tool_context.state["compiled_authority_cached"] is not None

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id
            == require_id(spec_row.spec_version_id, "spec_version_id")
        )
    ).first()
    assert authority is not None
    load_result = compiler_service.load_compiled_artifact(authority)
    assert load_result.status == "success"
    assert load_result.artifact is not None


def test_compile_spec_authority_for_version_persists_quality_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Compilation applies authority quality gate before persistence."""
    from services.specs import compiler_service  # noqa: PLC0415

    engine = create_engine(
        f"sqlite:///{tmp_path / 'business.sqlite3'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="Quality Gate Project")
        session.add(project)
        session.commit()
        session.refresh(project)
        lineage = seed_accepted_specification(
            session,
            project_id=require_id(project.project_id, "project_id"),
            content=_agileforge_spec_profile_json(),
            content_ref="specs/spec.json",
        )
        spec = lineage.spec
        spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    def fake_compile(**_: object) -> object:
        success = SpecAuthorityCompilationSuccess(
            scope_themes=["Quality"],
            domain=None,
            invariants=[
                Invariant(
                    id="INV-1111111111111111",
                    type=InvariantType.REQUIRED_FIELD,
                    source_item_id="REQ.test.audit",
                    source_level="MUST",
                    parameters=RequiredFieldParams(field_name="email"),
                ),
                Invariant(
                    id="INV-2222222222222222",
                    type=InvariantType.REQUIRED_FIELD,
                    source_item_id="REQ.test.audit",
                    source_level="MUST",
                    parameters=RequiredFieldParams(field_name="email"),
                ),
            ],
            eligible_feature_rules=[],
            rejected_features=[],
            gaps=[],
            assumptions=[],
            source_map=[],
            compiler_version="3.0.0",
            prompt_hash="a" * 64,
        )
        output = SpecAuthorityCompilerOutput(root=success)
        return compiler_service._NormalizedCompilerInvocation(
            raw_json=output.model_dump_json(),
            output=output,
        )

    monkeypatch.setattr(
        compiler_service,
        "_compile_spec_authority_output",
        fake_compile,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        spec_version_id=spec_version_id,
        force_recompile=False,
        engine=engine,
    )

    assert result["success"] is True
    with Session(engine) as session:
        authority = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == spec_version_id
            )
        ).one()
        assert authority.compiled_artifact_json is not None
        artifact = json.loads(authority.compiled_artifact_json)
    assert artifact["authority_quality"]["summary"]["merged_invariant_count"] == 1
    assert len(artifact["invariants"]) == 1


def test_compile_spec_authority_for_version_reports_normalized_duplicate_merges(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalizer duplicate cleanup is carried into persisted quality report."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _duplicate_required_field_compiler_output_json(),
    )
    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id
            == require_id(spec_row.spec_version_id, "spec_version_id")
        )
    ).one()
    assert authority.compiled_artifact_json is not None
    artifact = json.loads(authority.compiled_artifact_json)
    assert len(artifact["invariants"]) == 1
    assert artifact["authority_quality"]["summary"]["merged_invariant_count"] == 1
    assert len(artifact["authority_quality"]["merged_items"]) == 1


def test_merge_compilation_successes_preserves_later_quality_reports() -> None:
    """Focused pass quality metadata survives multi-success merge."""
    from services.specs import compiler_service  # noqa: PLC0415

    first = SpecAuthorityCompilationSuccess(
        scope_themes=["Quality"],
        domain=None,
        invariants=[],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    second_output = compiler_service.normalize_compiler_output(
        _duplicate_required_field_compiler_output_json()
    )
    assert isinstance(second_output.root, SpecAuthorityCompilationSuccess)
    assert second_output.root.authority_quality is not None
    assert second_output.root.authority_quality.summary.merged_invariant_count == 1

    merged = compiler_service._merge_compilation_successes(
        [
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FULL_SPEC,
                success=first,
            ),
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FOCUSED_ITEM,
                success=second_output.root,
            ),
        ],
        final_spec=None,
    )

    assert merged.authority_quality is not None
    assert merged.authority_quality.summary.merged_invariant_count == 1
    assert len(merged.authority_quality.merged_items) == 1


def test_merge_compilation_successes_remaps_later_assumption_review_ids() -> None:
    """Later quality groups identify assumptions in the final merged order."""
    from services.specs import compiler_service  # noqa: PLC0415

    leading = _scope_merge_success(
        cast(
            "dict[str, object]",
            free_text_assumption("Deployment region remains undecided."),
        )
    )
    later = SpecAuthorityCompilationSuccess.model_validate(
        {
            **leading.model_dump(mode="json"),
            "assumptions": [
                free_text_assumption(
                    "Python runtime should be confirmed before implementation."
                ),
                free_text_assumption(
                    "Python runtime should be confirmed before implementation step."
                ),
            ],
        }
    )
    later = compiler_service.apply_authority_quality_gate(later)
    assert later.authority_quality is not None
    assert later.authority_quality.review_groups[0].member_ids == [
        "ASM-1",
        "ASM-2",
    ]

    merged = compiler_service._merge_compilation_successes(
        [
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FULL_SPEC,
                success=leading,
            ),
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FOCUSED_ITEM,
                success=later,
            ),
        ],
        final_spec=None,
    )

    assert merged.authority_quality is not None
    noisy_groups = [
        group
        for group in merged.authority_quality.review_groups
        if group.group_type == "noisy_assumptions"
    ]
    assert [group.member_ids for group in noisy_groups] == [["ASM-2", "ASM-3"]]


@pytest.mark.parametrize(
    "scope",
    ["FOCUSED_ITEM", "REPAIR_ITEM"],
)
@pytest.mark.parametrize(
    "claim",
    [
        _true_count_claim(count=1, source_item_ids=["REQ.alpha"]),
        _true_set_claim(item_ids=["REQ.alpha"]),
    ],
)
def test_partial_scope_rejects_aggregate_assumption_claims(
    scope: str,
    claim: dict[str, object],
) -> None:
    """Partial inputs cannot make aggregate claims about a full spec."""
    from services.specs import compiler_service  # noqa: PLC0415

    with pytest.raises(ValueError, match="ASSUMPTION_CLAIM_SCOPE_INVALID"):
        compiler_service._merge_compilation_successes(
            [
                compiler_service.ScopedCompilationSuccess(
                    scope=compiler_service.CompilationScope(scope.lower()),
                    success=_scope_merge_success(claim),
                )
            ],
            final_spec=_scope_merge_spec("REQ.alpha"),
        )


@pytest.mark.parametrize(
    ("claim", "assumption_kind"),
    [
        (
            _true_count_claim(
                count=2,
                source_item_ids=["REQ.alpha", "REQ.beta"],
            ),
            "accepted_normative_count",
        ),
        (
            _true_set_claim(item_ids=["REQ.alpha", "REQ.beta"]),
            "accepted_normative_set",
        ),
    ],
)
def test_single_full_spec_aggregate_claim_is_retained_when_grounded(
    claim: dict[str, object],
    assumption_kind: str,
) -> None:
    """Full-spec aggregate claims survive a one-success semantic merge."""
    from services.specs import compiler_service  # noqa: PLC0415

    merged = compiler_service._merge_compilation_successes(
        [
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FULL_SPEC,
                success=_scope_merge_success(claim),
            ),
        ],
        final_spec=_scope_merge_spec("REQ.alpha", "REQ.beta"),
    )

    assert [assumption.kind for assumption in merged.assumptions] == [assumption_kind]


def test_iterative_compilation_converts_scope_error_to_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The iterative caller returns the exact partial-scope semantic failure."""
    from services.specs import compiler_service  # noqa: PLC0415

    artifact = _scope_merge_spec("REQ.alpha")
    full_success = _scope_merge_success(_item_status_claim("REQ.alpha"))
    focused_success = _scope_merge_success(
        _true_count_claim(count=1, source_item_ids=["REQ.alpha"])
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_and_normalize_spec_authority",
        lambda **_kwargs: SimpleNamespace(
            raw_json=SpecAuthorityCompilerOutput(root=full_success).model_dump_json(),
            output=SpecAuthorityCompilerOutput(root=full_success),
        ),
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_focused_structured_item_authority",
        lambda *_args, **_kwargs: focused_success,
    )

    result = compiler_service._compile_spec_authority_output(
        spec_content=artifact.model_dump_json(by_alias=True),
        content_ref=None,
        project_id=None,
        spec_version_id=None,
    )

    assert isinstance(result.output.root, SpecAuthorityCompilationFailure)
    assert result.output.root.error == "COMPILATION_FAILED"
    assert result.output.root.reason == "ASSUMPTION_CLAIM_SCOPE_INVALID"


def test_partial_item_status_claims_re_ground_against_final_full_spec() -> None:
    """Retained partial claims validate against the complete parsed artifact."""
    from services.specs import compiler_service  # noqa: PLC0415

    merged = compiler_service._merge_compilation_successes(
        [
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FOCUSED_ITEM,
                success=_scope_merge_success(_item_status_claim("REQ.beta")),
            )
        ],
        final_spec=_scope_merge_spec("REQ.alpha", "REQ.beta"),
    )

    assert len(merged.assumptions) == 1
    assumption = merged.assumptions[0]
    assert isinstance(assumption, ItemStatusAssumptionClaim)
    assert assumption.item_id == "REQ.beta"


def test_merge_compilation_successes_reports_cross_success_duplicate_merges() -> None:
    """Cross-success invariant dedupe is visible in quality metadata."""
    from services.specs import compiler_service  # noqa: PLC0415

    first = SpecAuthorityCompilationSuccess(
        scope_themes=["Quality"],
        domain=None,
        invariants=[
            Invariant(
                id="INV-1111111111111111",
                type=InvariantType.REQUIRED_FIELD,
                source_item_id="REQ.test.audit",
                source_level="MUST",
                parameters=RequiredFieldParams(field_name="email"),
            )
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-1111111111111111",
                excerpt="Email is required.",
                location="REQ.test.audit.acceptance[0]",
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    second = first.model_copy(
        deep=True,
        update={
            "source_map": [
                SourceMapEntry(
                    invariant_id="INV-1111111111111111",
                    excerpt="Audit output includes email.",
                    location="REQ.test.audit.acceptance[1]",
                )
            ]
        },
    )

    merged = compiler_service._merge_compilation_successes(
        [
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FULL_SPEC,
                success=first,
            ),
            compiler_service.ScopedCompilationSuccess(
                scope=compiler_service.CompilationScope.FOCUSED_ITEM,
                success=second,
            ),
        ],
        final_spec=None,
    )

    assert len(merged.invariants) == 1
    assert len(merged.source_map) == _EXPECTED_CROSS_SUCCESS_SOURCE_EVIDENCE_COUNT
    assert merged.authority_quality is not None
    assert merged.authority_quality.summary.merged_invariant_count == 1
    assert len(merged.authority_quality.merged_items) == 1
    assert merged.authority_quality.merged_items[0].kept_id == "INV-1111111111111111"
    assert merged.authority_quality.merged_items[0].removed_ids == [
        "INV-1111111111111111"
    ]
    assert (
        merged.authority_quality.merged_items[0].source_evidence_count
        == _EXPECTED_CROSS_SUCCESS_SOURCE_EVIDENCE_COUNT
    )


def test_compile_spec_authority_repairs_one_behavioral_source_item(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repairable source metadata failure should retry only the failing item."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=json.dumps(_focused_repair_spec_profile_payload()),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    calls: list[dict[str, str | None]] = []

    def fake_invoke(  # noqa: PLR0913
        *,
        spec_content: str,
        content_ref: str | None,
        project_id: int | None,
        spec_version_id: int | None,
        domain_hint: str | None = None,
        compiler_model: str | None = None,
    ) -> str:
        del content_ref, project_id, spec_version_id
        calls.append(
            {
                "spec_content": spec_content,
                "domain_hint": domain_hint,
                "compiler_model": compiler_model,
            }
        )
        if domain_hint is None:
            return _source_metadata_failure_json(
                source_item_id="REQ.payments.email",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _compiled_success_json_for_source_item("REQ.payments.email")

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
        compiler_model="openrouter/openai/gpt-5.2",
    )

    assert result["success"] is True
    assert len(calls) == _EXPECTED_REPAIR_CALLS
    focused_spec_content = calls[1]["spec_content"]
    assert focused_spec_content is not None
    assert "REQ.payments.email" in focused_spec_content
    assert "source_item_id: REQ.payments.email" in str(calls[1]["domain_hint"])
    assert calls[1]["compiler_model"] == "openrouter/openai/gpt-5.2"


def test_compile_spec_authority_does_not_repair_mixed_source_metadata_issues(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed source metadata failures must fail closed without focused repair."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=json.dumps(_focused_repair_spec_profile_payload()),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    calls = 0

    def fake_invoke(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if kwargs.get("domain_hint") is not None:
            return _compiled_success_json_for_source_item("REQ.payments.email")
        failure = SpecAuthorityCompilationFailure(
            error="SPEC_COMPILATION_FAILED",
            reason="SOURCE_METADATA_MISMATCH",
            blocking_gaps=[
                "INV-badbadbadbadbad1 source_item_id REQ.payments.email "
                "lacks supporting real source_map evidence.",
                "INV-hard FORBIDDEN_CAPABILITY over-promotes "
                "REQ.payments.email source level MUST.",
            ],
            source_metadata_issues=[
                {
                    "subcode": "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED",
                    "message": (
                        "INV-badbadbadbadbad1 source_item_id "
                        "REQ.payments.email lacks supporting real "
                        "source_map evidence."
                    ),
                    "invariant_id": "INV-badbadbadbadbad1",
                    "source_item_id": "REQ.payments.email",
                    "expected_source_level": "MUST",
                    "repairable": True,
                },
                {
                    "subcode": "LEGACY_MODALITY_PROMOTION",
                    "message": (
                        "INV-hard FORBIDDEN_CAPABILITY over-promotes "
                        "REQ.payments.email source level MUST."
                    ),
                    "invariant_id": "INV-hard",
                    "source_item_id": "REQ.payments.email",
                    "expected_source_level": "MUST",
                    "repairable": False,
                },
            ],
        )
        return SpecAuthorityCompilerOutput(root=failure).model_dump_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    assert calls == 1
    assert result["details"]["repair_attempted"] is False
    with Session(session.get_bind()) as verify_session:
        rows = verify_session.exec(select(CompiledSpecAuthority)).all()
    assert rows == []


def test_compile_spec_authority_repaired_item_cannot_skip_required_coverage(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair success must still cover every accepted MUST/MUST_NOT item."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    calls: list[str | None] = []

    def fake_invoke(**kwargs: object) -> str:
        domain_hint = kwargs.get("domain_hint")
        calls.append(cast("str | None", domain_hint))
        if domain_hint is None:
            return _source_metadata_failure_json(
                source_item_id="REQ.todo-create",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _behavioral_payload_json("REQ.todo-create", "MUST")

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    assert len(calls) == _EXPECTED_REPAIR_CALLS
    assert calls[0] is None
    assert "source_item_id: REQ.todo-create" in str(calls[1])
    assert result["error"] == "STRUCTURED_COVERAGE_INCOMPLETE"
    assert result["reason"] == "MISSING_ACCEPTED_MUST_AUTHORITY"
    assert result["blocking_gaps"] == ["REQ.todo-toggle"]
    assert result["details"]["repair_attempted"] is True
    assert result["details"]["repair_item_ids"] == ["REQ.todo-create"]
    assert result["details"]["repair_result"] == "coverage_incomplete"
    with Session(session.get_bind()) as verify_session:
        rows = verify_session.exec(select(CompiledSpecAuthority)).all()
    assert rows == []


def test_compile_spec_authority_coverage_repair_does_not_chain_metadata_repair(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage repair failure is terminal and cannot start metadata repair."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    calls: list[str | None] = []

    def fake_invoke(**kwargs: object) -> str:
        domain_hint = cast("str | None", kwargs.get("domain_hint"))
        calls.append(domain_hint)
        if domain_hint and "failed structured coverage validation" in domain_hint:
            return _source_metadata_failure_json(
                source_item_id="REQ.todo-create",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    assert result["error"] == "STRUCTURED_ITEM_COMPILATION_FAILED"
    assert result["reason"] == "FOCUSED_ITEM_AUTHORITY_FAILED"
    assert len(calls) == _EXPECTED_COVERAGE_REPAIR_FAIL_FAST_CALLS
    assert (
        sum(
            1
            for hint in calls
            if hint and "failed structured coverage validation" in hint
        )
        == 1
    )
    assert not any(
        hint and "failed source metadata validation" in hint for hint in calls
    )
    assert result["details"]["coverage_repair_attempted"] is True
    assert result["details"]["coverage_repair_item_ids"] == [
        "REQ.todo-create",
        "REQ.todo-toggle",
    ]
    assert result["details"]["coverage_repair_result"] == "failed"
    with Session(session.get_bind()) as verify_session:
        rows = verify_session.exec(select(CompiledSpecAuthority)).all()
    assert rows == []


def test_compile_spec_authority_repairs_missing_coverage_and_persists(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage repair can produce persisted authority when feedback succeeds."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")

    def fake_invoke(**kwargs: object) -> str:
        spec_content = cast("str", kwargs["spec_content"])
        domain_hint = cast("str | None", kwargs.get("domain_hint"))
        payload = json.loads(spec_content)
        item = payload["items"][0]
        item_id = cast("str", item["id"])
        if domain_hint and "failed structured coverage validation" in domain_hint:
            return _behavioral_payload_json(
                source_item_id=item_id,
                source_level=cast("SpecAuthoritySourceLevel", item["level"]),
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is True
    with Session(session.get_bind()) as verify_session:
        rows = verify_session.exec(select(CompiledSpecAuthority)).all()
    assert len(rows) == 1


def test_compile_spec_authority_does_not_repair_over_promotion(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-repairable source metadata failures should not trigger focused retry."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=json.dumps(_focused_repair_spec_profile_payload()),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    calls = 0

    def fake_invoke(**kwargs: object) -> str:
        nonlocal calls
        del kwargs
        calls += 1
        failure = SpecAuthorityCompilationFailure(
            error="SPEC_COMPILATION_FAILED",
            reason="SOURCE_METADATA_MISMATCH",
            blocking_gaps=[
                "INV-hard FORBIDDEN_CAPABILITY over-promotes "
                "DECISION.choice source level None."
            ],
            source_metadata_issues=[
                {
                    "subcode": "LEGACY_MODALITY_PROMOTION",
                    "message": (
                        "INV-hard FORBIDDEN_CAPABILITY over-promotes "
                        "DECISION.choice source level None."
                    ),
                    "invariant_id": "INV-hard",
                    "source_item_id": "DECISION.choice",
                    "repairable": False,
                }
            ],
        )
        return SpecAuthorityCompilerOutput(root=failure).model_dump_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    assert calls == 1
    assert result["details"]["repair_attempted"] is False


def test_compile_spec_authority_failed_repair_leaves_no_compiled_authority_rows(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed source metadata repair must not persist partial authority."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=json.dumps(_focused_repair_spec_profile_payload()),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")

    def fake_invoke(**kwargs: object) -> str:
        domain_hint = kwargs.get("domain_hint")
        if domain_hint is None:
            return _source_metadata_failure_json(
                source_item_id="REQ.payments.email",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _source_metadata_failure_json(
            source_item_id="REQ.payments.email",
            invariant_id="INV-stillbadstillbd",
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    assert result["details"]["repair_attempted"] is True
    assert result["details"]["repair_item_ids"] == ["REQ.payments.email"]
    assert result["details"]["repair_result"] == "failed"
    with Session(session.get_bind()) as verify_session:
        rows = verify_session.exec(select(CompiledSpecAuthority)).all()
    assert rows == []


def test_source_metadata_failure_details_include_repair_guidance(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrepaired source metadata failures should include actionable guidance."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=json.dumps(_focused_repair_spec_profile_payload()),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    long_excerpt = "unsupported evidence " * 40

    def fake_invoke(**kwargs: object) -> str:
        del kwargs
        return _source_metadata_failure_json(
            source_item_id="REQ.payments.email",
            invariant_id="INV-badbadbadbadbad1",
            source_excerpt=long_excerpt,
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    details = result["details"]
    assert (
        details["source_metadata_subcode"] == "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED"
    )
    assert details["source_item_id"] == "REQ.payments.email"
    assert details["invalid_invariant_id"] == "INV-badbadbadbadbad1"
    assert details["source_level"] == "MUST"
    assert details["source_excerpt"] == long_excerpt[:500]
    assert details["repair_attempted"] is True
    assert details["repair_item_ids"] == ["REQ.payments.email"]
    assert details["repair_result"] == "failed"
    assert details["suggested_commands"] == [
        f"agileforge workflow next --project-id {sample_project.project_id}"
    ]


def test_compile_spec_authority_for_version_iteratively_persists_must_coverage(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted structured compilation merges focused MUST/MUST_NOT item outputs."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[list[str]] = []

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        items = payload["items"]
        assert isinstance(items, list)
        item_ids = [item["id"] for item in items]
        calls.append(item_ids)
        first_item = items[0]
        assert isinstance(first_item, dict)
        source_item_id = first_item["id"]
        source_level = first_item["level"]
        assert isinstance(source_item_id, str)
        assert source_level in {"MUST", "MUST_NOT"}
        return _behavioral_payload_json(
            source_item_id=source_item_id,
            source_level=cast("SpecAuthoritySourceLevel", source_level),
        )

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )
    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id
            == require_id(spec_row.spec_version_id, "spec_version_id")
        )
    ).one()
    load_result = compiler_service.load_compiled_artifact(authority)
    assert load_result.status == "success"
    assert load_result.artifact is not None
    covered_item_ids = {
        invariant.source_item_id
        for invariant in load_result.artifact.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
        and invariant.source_item_id is not None
    }
    assert covered_item_ids == {"REQ.todo-create", "REQ.todo-toggle"}
    assert ["REQ.todo-create"] in calls
    assert ["REQ.todo-toggle"] in calls
    assert ["REQ.todo-color"] not in calls


def test_update_spec_and_compile_authority_suppresses_auto_accept_for_vacant_authority(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vacant authority blocks update+compile before persistence."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _vacant_success_json(),
    )
    _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )

    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": _agileforge_spec_profile_json(),
        },
        tool_context=None,
    )

    assert result["success"] is False
    assert result["error"] == "SPEC_AUTHORITY_VACANT"
    assert result["reason"] == "NO_INVARIANTS_EXTRACTED"
    assert result["blocking_gaps"] == ["No invariants extracted from spec"]
    assert session.exec(select(CompiledSpecAuthority)).all() == []
    assert session.exec(select(SpecAuthorityAcceptance)).all() == []


def test_compile_spec_authority_for_version_with_engine_uses_supplied_engine(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify engine-aware compile path never falls back to module get_engine."""
    from services.specs import compiler_service  # noqa: PLC0415

    other_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(other_engine)

    monkeypatch.setattr(compiler_service, "get_engine", lambda: other_engine)
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )

    with Session(engine) as supplied_session:
        project = Project(name="Supplied Engine Project", vision="vision")
        supplied_session.add(project)
        supplied_session.commit()
        supplied_session.refresh(project)
        spec = _create_spec_version(
            supplied_session,
            project_id=require_id(project.project_id, "project_id"),
        )
        spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=spec_version_id,
        force_recompile=False,
    )

    assert result["success"] is True
    with Session(other_engine) as other_session:
        other_rows = other_session.exec(select(CompiledSpecAuthority)).all()
    assert other_rows == []


def test_compiler_invocation_guard_heartbeats_until_blocking_call_finishes() -> None:
    """Blocking compiler invocations should heartbeat until the worker finishes."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[str] = []
    result_value = object()

    def invoke() -> object:
        time.sleep(0.03)
        return result_value

    def lease_guard(boundary: str) -> bool:
        calls.append(boundary)
        return True

    result = compiler_service._run_compiler_invocation_with_guards(
        invoke=invoke,
        lease_guard=lease_guard,
        heartbeat_interval_seconds=0.005,
        timeout_seconds=1.0,
        timeout_result=lambda: {"success": False, "error": "timeout"},
    )

    assert result is result_value
    assert calls[0] == "authority_compile_invocation_started"
    assert "authority_compile_invocation_heartbeat" in calls
    assert calls[-1] == "authority_compile_invocation_finished"


def test_compiler_invocation_guard_returns_timeout_without_finish_guard() -> None:
    """Timed-out compiler invocations should not run the finish lease guard."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[str] = []

    def invoke() -> object:
        time.sleep(0.05)
        return object()

    result = compiler_service._run_compiler_invocation_with_guards(
        invoke=invoke,
        lease_guard=lambda boundary: calls.append(boundary) or True,
        heartbeat_interval_seconds=0.005,
        timeout_seconds=0.01,
        timeout_result=lambda: {
            "success": False,
            "error": "SPEC_COMPILER_INVOCATION_TIMEOUT",
            "failure_stage": "invocation_timeout",
        },
    )

    assert result == {
        "success": False,
        "error": "SPEC_COMPILER_INVOCATION_TIMEOUT",
        "failure_stage": "invocation_timeout",
    }
    assert "authority_compile_invocation_started" in calls
    assert "authority_compile_invocation_finished" not in calls


def test_compiler_invocation_guard_returns_lease_loss_when_heartbeat_fails() -> None:
    """Heartbeat lease loss should use the mutation lease-loss envelope."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[str] = []

    def invoke() -> object:
        time.sleep(0.05)
        return object()

    def lease_guard(boundary: str) -> bool:
        calls.append(boundary)
        return boundary != "authority_compile_invocation_heartbeat"

    result = compiler_service._run_compiler_invocation_with_guards(
        invoke=invoke,
        lease_guard=lease_guard,
        heartbeat_interval_seconds=0.005,
        timeout_seconds=1.0,
        timeout_result=lambda: {"success": False, "error": "timeout"},
    )

    assert result == {
        "success": False,
        "error": "MUTATION_LEASE_LOST",
        "error_code": "MUTATION_IN_PROGRESS",
        "boundary": "authority_compile_invocation_heartbeat",
    }


def test_compile_spec_authority_for_version_with_engine_runs_lease_guard_before_persist(
    engine: Engine,
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify engine-aware compile path guards both durable writes."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )
    spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )
    boundaries: list[str] = []

    def lease_guard(boundary: str) -> bool:
        boundaries.append(boundary)
        return True

    def record_progress(boundary: str) -> bool:
        boundaries.append(f"progress:{boundary}")
        return True

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        force_recompile=False,
        lease_guard=lease_guard,
        record_progress=record_progress,
    )

    assert result["success"] is True
    assert "compiled_authority_persisted" in boundaries
    assert "progress:compiled_authority_persisted" in boundaries


@pytest.mark.parametrize(
    ("blocked_boundary", "expect_authority"),
    [
        ("compiled_authority_persisted", False),
    ],
)
def test_compile_spec_authority_for_version_with_engine_lease_loss_blocks_write(  # noqa: PLR0913
    engine: Engine,
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
    blocked_boundary: str,
    expect_authority: bool,
) -> None:
    """A lost lease should roll back every guarded compiler write atomically."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )
    spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=spec_version_id,
        force_recompile=False,
        lease_guard=lambda boundary: boundary != blocked_boundary,
        record_progress=lambda _boundary: True,
    )

    assert result["success"] is False
    assert result["error_code"] == "MUTATION_IN_PROGRESS"

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    assert (authority is not None) is expect_authority


@pytest.mark.parametrize(
    ("failed_boundary", "mode"),
    [
        ("compiled_authority_persisted", "false"),
    ],
)
def test_compile_spec_authority_for_version_with_engine_progress_failure_recovers(  # noqa: PLR0913
    engine: Engine,
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
    failed_boundary: str,
    mode: str,
) -> None:
    """Progress recorder failure should stop with recovery-required metadata."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )
    spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )

    def record_progress(boundary: str) -> bool:
        if boundary != failed_boundary:
            return True
        if mode == "raise":
            message = "progress failed"
            raise RuntimeError(message)
        return False

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        force_recompile=False,
        lease_guard=lambda _boundary: True,
        record_progress=record_progress,
    )

    assert result["success"] is False
    assert result["error_code"] == "MUTATION_RECOVERY_REQUIRED"
    assert result["boundary"] == failed_boundary


def test_compile_spec_authority_persists_authority_with_legacy_envelope(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify compile spec authority persists authority with legacy envelope."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    tool_context = make_tool_context()

    result = compiler_service.compile_spec_authority(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert set(result.keys()) == {
        "success",
        "authority_id",
        "spec_version_id",
        "compiler_version",
        "prompt_hash",
        "scope_themes_count",
        "invariants_count",
        "message",
    }
    assert result["spec_version_id"] == require_id(
        spec_row.spec_version_id, "spec_version_id"
    )
    assert len(result["prompt_hash"]) == 8  # noqa: PLR2004
    assert "compiled_authority_cached" not in tool_context.state

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id
            == require_id(spec_row.spec_version_id, "spec_version_id")
        )
    ).first()
    assert authority is not None
    load_result = compiler_service.load_compiled_artifact(authority)
    assert load_result.status == "success"
    assert load_result.artifact is not None


def test_compile_spec_authority_returns_error_when_already_compiled(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify compile spec authority returns error when already compiled."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("compiler should not run for already-compiled specs")
        ),
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )

    result = compiler_service.compile_spec_authority(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    authority_id = require_id(authority.authority_id, "authority_id")
    assert result["success"] is False
    assert result["error"] == (
        f"Spec version {spec_version_id} is already compiled "
        f"(authority_id: {authority_id})"
    )


def test_compile_spec_authority_for_version_returns_cached_authority(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify compile spec authority for version returns cached authority."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: (_ for _ in ()).throw(AssertionError("compiler should not run")),
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    existing = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )
    tool_context = make_tool_context()

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["cached"] is True
    assert "recompiled" not in result
    assert result["authority_id"] == require_id(existing.authority_id, "authority_id")
    assert result["content_source"] == "content"
    assert (
        tool_context.state["compiled_authority_cached"]
        == existing.compiled_artifact_json
    )
    session.refresh(sample_project)
    assert not hasattr(sample_project, "compiled_authority_json")


def test_force_recompile_inserts_without_mutating_existing_history(
    engine: Engine,
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced recompilation appends a candidate and preserves accepted v2 history."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )
    spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    existing = _create_compiled_authority(
        session,
        spec_version_id=spec_version_id,
        artifact_json=json.dumps(
            historical_v2_compiled_authority(prompt_hash="a" * 64)
        ),
    )
    existing_id = require_id(existing.authority_id, "authority_id")
    acceptance = SpecAuthorityAcceptance(
        project_id=require_id(sample_project.project_id, "project_id"),
        spec_version_id=spec_version_id,
        status="accepted",
        policy="test",
        decided_by="test",
        rationale="Historical v2 acceptance.",
        compiler_version=existing.compiler_version,
        prompt_hash=existing.prompt_hash,
        spec_hash=spec.spec_hash,
        pending_authority_id=existing_id,
        authority_fingerprint="immutable-history",
        terminal_decision_key=(
            f"{require_id(sample_project.project_id, 'project_id')}:"
            f"{spec_version_id}:{existing_id}"
        ),
        provenance_source="legacy_backfill",
    )
    session.add(acceptance)
    session.commit()
    session.refresh(existing)
    session.refresh(acceptance)
    acceptance_id = require_id(acceptance.id, "acceptance_id")
    before_authority = existing.model_dump()
    before_acceptance = acceptance.model_dump()

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    session.expire_all()
    rows = session.exec(
        select(CompiledSpecAuthority)
        .where(CompiledSpecAuthority.spec_version_id == spec_version_id)
        .order_by(cast("Any", CompiledSpecAuthority.authority_id).asc())
    ).all()
    preserved_acceptance = session.get(SpecAuthorityAcceptance, acceptance_id)
    session.refresh(sample_project)

    assert result["success"] is True
    assert result["recompiled"] is True
    assert len(rows) == 2  # noqa: PLR2004
    assert rows[0].authority_id == existing_id
    assert rows[0].model_dump() == before_authority
    assert preserved_acceptance is not None
    assert preserved_acceptance.model_dump() == before_acceptance
    assert result["authority_id"] == rows[1].authority_id
    assert not hasattr(sample_project, "compiled_authority_json")


def test_force_recompile_inserts_when_existing_row_has_no_terminal_decision(
    engine: Engine,
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced recompilation appends even when the existing candidate is pending."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )
    spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    existing = _create_compiled_authority(
        session,
        spec_version_id=spec_version_id,
        artifact_json=json.dumps(v3_compiled_authority_payload()),
    )
    existing_id = require_id(existing.authority_id, "authority_id")

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    rows = session.exec(
        select(CompiledSpecAuthority)
        .where(CompiledSpecAuthority.spec_version_id == spec_version_id)
        .order_by(cast("Any", CompiledSpecAuthority.authority_id).asc())
    ).all()
    assert len(rows) == 2  # noqa: PLR2004
    assert rows[0].authority_id == existing_id
    assert result["authority_id"] == rows[1].authority_id


def test_compile_spec_authority_for_version_rejects_unsupported_cached_authority(
    engine: Engine,
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported cached authority artifacts fail closed without cache updates."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: (_ for _ in ()).throw(AssertionError("compiler should not run")),
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=json.dumps(
            historical_v2_compiled_authority(prompt_hash="a" * 64)
        ),
    )
    tool_context = make_tool_context()

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        force_recompile=False,
        tool_context=tool_context,
    )

    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    project_id = require_id(sample_project.project_id, "project_id")
    assert result["success"] is False
    assert result["error_code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result["details"] == {
        "project_id": project_id,
        "spec_version_id": spec_version_id,
        "authority_id": require_id(authority.authority_id, "authority_id"),
        "load_status": "schema_unsupported",
        "observed_schema_version": "agileforge.compiled_authority.v2",
        "required_schema_version": "agileforge.compiled_authority.v3",
    }
    assert result["remediation"] == [
        f"agileforge workflow next --project-id {project_id}"
    ]
    assert "compiled_authority_cached" not in tool_context.state
    session.refresh(sample_project)
    assert not hasattr(sample_project, "compiled_authority_json")


@pytest.mark.parametrize(
    ("artifact_json", "load_status"),
    [
        ("not-json", "invalid_json"),
        (None, "missing"),
    ],
)
def test_compile_spec_authority_for_version_rejects_invalid_cached_authority(  # noqa: PLR0913
    engine: Engine,
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
    artifact_json: str | None,
    load_status: str,
) -> None:
    """Malformed cached rows fail closed without compiler or cache mutation."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: (_ for _ in ()).throw(AssertionError("compiler should not run")),
    )
    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )
    authority = CompiledSpecAuthority(
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        compiler_version="3.0.0",
        prompt_hash="f" * 64,
        compiled_at=datetime.now(UTC),
        compiled_artifact_json=artifact_json,
        scope_themes='["false-success"]',
        invariants='["false-success"]',
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    tool_context = make_tool_context()

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=engine,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        force_recompile=False,
        tool_context=tool_context,
    )

    assert result["success"] is False
    assert result["cached"] is False
    assert result["error_code"] == "COMPILED_AUTHORITY_INVALID"
    assert result["details"]["load_status"] == load_status
    assert result["details"]["authority_id"] == require_id(
        authority.authority_id, "authority_id"
    )
    assert "compiled_authority_cached" not in tool_context.state
    session.refresh(sample_project)
    assert not hasattr(sample_project, "compiled_authority_json")


def test_compile_spec_authority_for_version_uses_content_ref_when_content_empty(
    session: Session,
    sample_project: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify compile spec authority for version uses content ref when content empty."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_agileforge_spec_profile_json(), encoding="utf-8")
    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content_ref=str(spec_path),
    )
    spec_row.content = ""
    session.add(spec_row)
    session.commit()
    session.refresh(spec_row)

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert result["content_source"] == "content_ref"


def test_compile_spec_authority_for_version_persists_invocation_failure_artifact(
    session: Session,
    sample_project: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify compile spec authority for version persists invocation failure artifact."""  # noqa: E501
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        failure_artifacts,
        "FAILURES_DIR",
        tmp_path / "logs" / "failures",
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: (_ for _ in ()).throw(
            AgentInvocationError(
                "provider timeout",
                partial_output='{"partial": true}',
                event_count=2,
            )
        ),
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["error"] == "SPEC_COMPILER_INVOCATION_FAILED"
    assert result["failure_artifact_id"] is not None
    artifact = failure_artifacts.read_failure_artifact(result["failure_artifact_id"])
    assert artifact is not None
    assert artifact["phase"] == "spec_authority"
    assert artifact["raw_output"] == '{"partial": true}'


def test_invalid_json_gets_one_schema_retry(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid JSON should trigger exactly one schema-feedback retry."""
    from services.specs import compiler_service  # noqa: PLC0415

    payloads: list[dict[str, object]] = []

    async def fake_invoke_agent_to_text(*args: object, **kwargs: object) -> str:
        del args
        payload_json = kwargs.get("payload_json")
        assert isinstance(payload_json, str)
        payload = json.loads(payload_json)
        assert isinstance(payload, dict)
        payloads.append(payload)
        if len(payloads) == 1:
            return "{"
        return json.dumps(_structured_retry_success_payload())

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "invoke_agent_to_text",
        fake_invoke_agent_to_text,
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert len(payloads) == _EXPECTED_FOCUSED_RETRY_CALLS
    assert payloads[0]["domain_hint"] is None
    retry_hint = payloads[1]["domain_hint"]
    assert isinstance(retry_hint, str)
    assert 'schema_version must be "agileforge.compiled_authority.v3".' in retry_hint
    assert "Do not put source_item_id or source_level inside parameters." in retry_hint
    assert result["schema_retry_attempted"] is True
    assert result["schema_retry_reason"] == "INVALID_JSON"
    assert result["schema_retry_attempts"] == 1


def test_json_validation_failed_gets_one_schema_retry(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema-shaped output drift should get one bounded retry."""
    from services.specs import compiler_service  # noqa: PLC0415

    attempts: list[dict[str, object]] = []

    async def fake_invoke_agent_to_text(*args: object, **kwargs: object) -> str:
        del args
        payload_json = kwargs.get("payload_json")
        assert isinstance(payload_json, str)
        payload = json.loads(payload_json)
        assert isinstance(payload, dict)
        attempts.append(payload)
        if len(attempts) == 1:
            invalid_payload = _structured_retry_success_payload()
            invalid_payload["invariants"][0]["parameters"] = {"unexpected": "value"}  # type: ignore[index]
            return json.dumps(invalid_payload)
        return json.dumps(_structured_retry_success_payload())

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "invoke_agent_to_text",
        fake_invoke_agent_to_text,
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert len(attempts) == _EXPECTED_FOCUSED_RETRY_CALLS
    assert result["schema_retry_attempted"] is True
    assert result["schema_retry_reason"] == "JSON_VALIDATION_FAILED"
    assert result["schema_retry_attempts"] == 1


def test_claim_like_assumption_gets_one_schema_retry(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedicated typed-claim contract failure gets one retry only."""
    from services.specs import compiler_service  # noqa: PLC0415

    attempts: list[dict[str, object]] = []

    async def fake_invoke_agent_to_text(*args: object, **kwargs: object) -> str:
        del args
        payload_json = kwargs.get("payload_json")
        assert isinstance(payload_json, str)
        attempts.append(json.loads(payload_json))
        payload = _structured_retry_success_payload()
        if len(attempts) == 1:
            payload["assumptions"] = ["Only accepted items are in scope."]
        return json.dumps(payload)

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "invoke_agent_to_text",
        fake_invoke_agent_to_text,
    )
    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert len(attempts) == _EXPECTED_FOCUSED_RETRY_CALLS
    assert result["schema_retry_reason"] == "ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM"
    assert result["schema_retry_attempts"] == 1


def test_schema_retry_stops_after_one_retry(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema retry should stop after one additional attempt."""
    from services.specs import compiler_service  # noqa: PLC0415

    attempts: list[dict[str, object]] = []

    async def fake_invoke_agent_to_text(*args: object, **kwargs: object) -> str:
        del args
        payload_json = kwargs.get("payload_json")
        assert isinstance(payload_json, str)
        payload = json.loads(payload_json)
        assert isinstance(payload, dict)
        attempts.append(payload)
        return json.dumps(_structured_retry_invalid_payload())

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "invoke_agent_to_text",
        fake_invoke_agent_to_text,
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert len(attempts) == _EXPECTED_FOCUSED_RETRY_CALLS
    assert result["schema_retry_attempted"] is True
    assert result["schema_retry_reason"] == "JSON_VALIDATION_FAILED"
    assert result["schema_retry_attempts"] == _SCHEMA_RETRY_ATTEMPTS
    assert result["schema_retry_failure_details"] == [
        {
            "attempt": 1,
            "reason": "JSON_VALIDATION_FAILED",
            "raw_output": json.dumps(_structured_retry_invalid_payload()),
        },
        {
            "attempt": 2,
            "reason": "JSON_VALIDATION_FAILED",
            "raw_output": json.dumps(_structured_retry_invalid_payload()),
        },
    ]


def test_false_structured_claim_does_not_retry_or_persist(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A false structured claim fails closed without retry or persistence."""
    from services.specs import compiler_service  # noqa: PLC0415

    attempts: list[dict[str, object]] = []

    async def fake_invoke_agent_to_text(*args: object, **kwargs: object) -> str:
        del args
        payload_json = kwargs.get("payload_json")
        assert isinstance(payload_json, str)
        payload = json.loads(payload_json)
        assert isinstance(payload, dict)
        attempts.append(payload)
        invalid_payload = {
            "schema_version": "agileforge.compiled_authority.v3",
            "scope_themes": ["Audit"],
            "domain": "operations",
            "invariants": [
                {
                    "id": "INV-0123456789abcdef",
                    "type": "DATA_CONTRACT",
                    "source_item_id": "REQ.test.audit",
                    "source_level": "MUST_NOT",
                    "parameters": {
                        "subject": "audit evidence",
                        "fields": ["operation"],
                        "rule": "record audit evidence for each operation",
                    },
                }
            ],
            "eligible_feature_rules": [],
            "rejected_features": [],
            "gaps": [],
            "assumptions": [
                {
                    "kind": "accepted_normative_count",
                    "count": 1,
                    "provenance": {
                        "source": "structured_spec",
                        "artifact_id": "SPEC.test",
                        "source_item_ids": [],
                    },
                }
            ],
            "source_map": [
                {
                    "invariant_id": "INV-0123456789abcdef",
                    "excerpt": "The system MUST record audit evidence.",
                    "location": "REQ.test.audit",
                }
            ],
            "compiler_version": "3.0.0",
            "prompt_hash": "a" * 64,
        }
        return json.dumps(invalid_payload)

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "invoke_agent_to_text",
        fake_invoke_agent_to_text,
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["reason"] == "ASSUMPTION_CLAIM_MISMATCH"
    assert len(attempts) == 1
    assert result["schema_retry_attempted"] is False
    assert result["schema_retry_reason"] is None
    assert result["schema_retry_attempts"] == 0
    assert session.exec(select(CompiledSpecAuthority)).all() == []


@pytest.mark.parametrize(
    "source_text",
    [
        "",
        '{"schema_version":"agileforge.spec.v1","artifact_id":"SPEC.test"}',
    ],
    ids=["missing", "malformed"],
)
def test_unavailable_structured_claim_source_does_not_retry_or_persist(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
    source_text: str,
) -> None:
    """Missing or malformed source reaches claim grounding without persistence."""
    from services.specs import compiler_service  # noqa: PLC0415

    attempts: list[str] = []
    payload = _structured_retry_success_payload()
    payload["assumptions"] = [
        {
            "kind": "accepted_normative_count",
            "count": 1,
            "provenance": {
                "source": "structured_spec",
                "artifact_id": "SPEC.test",
                "source_item_ids": [],
            },
        }
    ]

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs.get("spec_content")
        assert spec_content == source_text
        attempts.append(cast("str", spec_content))
        return json.dumps(payload)

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "_load_spec_content_for_compile",
        lambda _spec_version: (source_text, "test_override"),
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )
    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["reason"] == "ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE"
    assert attempts == [source_text]
    assert result["schema_retry_attempted"] is False
    assert result["schema_retry_reason"] is None
    assert result["schema_retry_attempts"] == 0
    assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_check_spec_authority_status_returns_not_compiled_when_no_spec_versions(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify check spec authority status returns not compiled when no spec versions."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    result = compiler_service.check_spec_authority_status(
        {"project_id": require_id(sample_project.project_id, "project_id")},
        tool_context=None,
    )

    assert result == {
        "success": True,
        "status": SpecAuthorityStatus.NOT_COMPILED.value,
        "status_details": "No spec versions exist for this project",
        "message": "Status: NOT_COMPILED (no specs)",
    }


def test_check_spec_authority_status_reports_stale_for_newer_approved_lineage(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer accepted spec makes an older compiled authority stale."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    approved_spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=_canonical_agileforge_spec_profile_json(),
    )
    _create_compiled_authority(
        session,
        spec_version_id=require_id(approved_spec.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )

    replacement_spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )

    result = compiler_service.check_spec_authority_status(
        {"project_id": require_id(sample_project.project_id, "project_id")},
        tool_context=None,
    )

    approved_spec_version_id = require_id(
        approved_spec.spec_version_id,
        "spec_version_id",
    )
    replacement_spec_version_id = require_id(
        replacement_spec.spec_version_id,
        "spec_version_id",
    )
    assert result == {
        "success": True,
        "status": SpecAuthorityStatus.STALE.value,
        "status_details": "Compiled authority is stale (newer approved spec exists)",
        "compiled_spec_version_id": approved_spec_version_id,
        "latest_approved_spec_version_id": replacement_spec_version_id,
        "message": "Status: STALE (compiled for older spec)",
    }


def test_check_spec_authority_status_does_not_report_historical_v2_artifact_current(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical v2 stored artifacts are not CURRENT."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=json.dumps(
            historical_v2_compiled_authority(prompt_hash="a" * 64)
        ),
    )

    result = compiler_service.check_spec_authority_status(
        {"project_id": require_id(sample_project.project_id, "project_id")},
        tool_context=None,
    )

    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    authority_id = require_id(authority.authority_id, "authority_id")
    project_id = require_id(sample_project.project_id, "project_id")
    assert result["success"] is False
    assert result["error_code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result["details"] == {
        "project_id": project_id,
        "spec_version_id": spec_version_id,
        "authority_id": authority_id,
        "load_status": "schema_unsupported",
        "observed_schema_version": "agileforge.compiled_authority.v2",
        "required_schema_version": "agileforge.compiled_authority.v3",
    }
    assert result["remediation"] == [
        f"agileforge workflow next --project-id {project_id}"
    ]


def test_check_spec_authority_status_rejects_invalid_artifact(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed latest rows return the central invalid-artifact failure."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    spec_row = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
    )
    authority = CompiledSpecAuthority(
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        compiler_version="3.0.0",
        prompt_hash="f" * 64,
        compiled_at=datetime.now(UTC),
        compiled_artifact_json="not-json",
        scope_themes='["false-success"]',
        invariants='["false-success"]',
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)

    result = compiler_service.check_spec_authority_status(
        {"project_id": require_id(sample_project.project_id, "project_id")}
    )

    assert result["success"] is False
    assert result["error_code"] == "COMPILED_AUTHORITY_INVALID"
    assert result["details"]["load_status"] == "invalid_json"
    assert result["details"]["authority_id"] == require_id(
        authority.authority_id, "authority_id"
    )


def test_get_compiled_authority_by_version_returns_expected_envelope(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify get compiled authority by version returns expected envelope."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )

    result = compiler_service.get_compiled_authority_by_version(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id"),
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["spec_version_id"] == require_id(
        spec_row.spec_version_id, "spec_version_id"
    )
    assert result["authority_id"] == require_id(authority.authority_id, "authority_id")
    assert result["compiler_version"] == authority.compiler_version
    assert result["compiled_at"] == authority.compiled_at.isoformat()
    assert result["scope_themes"] == ["Payments"]
    assert result["invariants"] == ["REQUIRED_FIELD:email"]
    assert result["eligible_feature_ids"] == []
    assert result["rejected_features"] == []
    assert result["spec_gaps"] == []
    assert result["compiled_artifact_json"] == authority.compiled_artifact_json
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    assert result["message"] == (
        f"Retrieved compiled authority for spec version {spec_version_id}"
    )


def test_get_compiled_authority_by_version_rejects_invalid_artifact(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed stored JSON must not fall back to denormalized columns."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    authority = CompiledSpecAuthority(
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        compiler_version="9.9.9",
        prompt_hash="f" * 64,
        compiled_at=datetime.now(UTC),
        compiled_artifact_json="not-json",
        scope_themes=json.dumps(["Legacy Theme"]),
        invariants=json.dumps(["FORBIDDEN_CAPABILITY:upload"]),
        eligible_feature_ids=json.dumps([10, 11]),
        rejected_features=json.dumps(["Feature X"]),
        spec_gaps=json.dumps(["gap one"]),
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)

    result = compiler_service.get_compiled_authority_by_version(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id"),
        },
        tool_context=None,
    )

    assert result["success"] is False
    assert result["error_code"] == "COMPILED_AUTHORITY_INVALID"
    assert result["details"]["load_status"] == "invalid_json"
    assert result["details"]["authority_id"] == require_id(
        authority.authority_id, "authority_id"
    )
    assert "scope_themes" not in result
    assert "invariants" not in result


def test_get_compiled_authority_by_version_returns_existing_error_messages(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify get compiled authority by version returns existing error messages."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    not_found = compiler_service.get_compiled_authority_by_version(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_version_id": 999999,
        },
        tool_context=None,
    )
    assert not_found == {"success": False, "error": "Spec version 999999 not found"}

    spec_row = _create_spec_version(
        session, project_id=require_id(sample_project.project_id, "project_id")
    )
    other_project = Project(
        name="Other Project",
        description="Other",
        vision="Other vision",
    )
    session.add(other_project)
    session.commit()
    session.refresh(other_project)

    mismatch = compiler_service.get_compiled_authority_by_version(
        {
            "project_id": require_id(other_project.project_id, "project_id"),
            "spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id"),
        },
        tool_context=None,
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    other_project_id = require_id(other_project.project_id, "project_id")
    assert mismatch == {
        "success": False,
        "error": (
            f"Spec version {spec_version_id} does not belong to "
            f"project {other_project_id} (mismatch)"
        ),
    }

    not_compiled = compiler_service.get_compiled_authority_by_version(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id"),
        },
        tool_context=None,
    )
    assert not_compiled == {
        "success": False,
        "error_code": "AUTHORITY_NOT_COMPILED",
        "error": (
            f"Spec version {spec_version_id} is not compiled. "
            "Use compile_spec_authority to compile it."
        ),
    }


@pytest.fixture
def sample_project(session: Session) -> Project:
    """Return project."""
    project = Project(
        name="Compiler Service Project",
        description="Project for compiler service tests",
        vision="Keep compiler orchestration outside tool modules",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def test_update_spec_and_compile_authority_requires_accepted_specification(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy entry point cannot create or implicitly approve a specification."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("compile must not run without accepted specification")
        ),
    )
    spec_content = _agileforge_spec_profile_json()
    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )

    assert result == {
        "success": False,
        "error_code": "SPECIFICATION_NOT_ACCEPTED",
        "error": (
            "Specification must be accepted through specification.review "
            "before Authority compilation."
        ),
    }
    assert session.exec(select(SpecRegistry)).all() == []
    assert session.exec(select(SpecAuthorityAcceptance)).all() == []


def test_update_spec_and_compile_authority_honors_tool_compile_override(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify update spec and compile authority honors tool compile override."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("service compile path should be bypassed")
        ),
    )

    compile_params: dict[str, object] = {}

    def fake_tool_compile(
        params: dict[str, object], tool_context: object = None
    ) -> dict[str, object]:
        del tool_context
        compile_params.update(params)
        spec_version_id = params["spec_version_id"]
        assert isinstance(spec_version_id, int)
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="f" * 64,
            compiled_at=datetime.now(UTC),
            compiled_artifact_json=_stored_compiled_success_json(),
            scope_themes='["Payments"]',
            invariants='["REQUIRED_FIELD:email"]',
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        return {
            "success": True,
            "cached": False,
            "authority_id": require_id(authority.authority_id, "authority_id"),
        }

    monkeypatch.setattr(
        spec_tools,
        "compile_spec_authority_for_version",
        fake_tool_compile,
    )
    spec_content = _agileforge_spec_profile_json()
    accepted_spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=spec_content,
    )
    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert compile_params["spec_version_id"] == require_id(
        accepted_spec.spec_version_id,
        "spec_version_id",
    )
    assert compile_params["force_recompile"] is False


def test_update_spec_and_compile_authority_never_persists_acceptance(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Update+compile returns a pending candidate without an acceptance row."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    def fake_compile(
        *,
        spec_version_id: int,
        force_recompile: bool,
        tool_context: object,
        compiler_model: str | None = None,
    ) -> object:
        del force_recompile, tool_context, compiler_model
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="a" * 64,
            compiled_at=datetime.now(UTC),
            compiled_artifact_json=_stored_compiled_success_json(),
            scope_themes='["Payments"]',
            invariants='["REQUIRED_FIELD:email"]',
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        return {
            "success": True,
            "cached": False,
            "authority_id": require_id(authority.authority_id, "authority_id"),
        }

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        fake_compile,
    )

    spec_content = _agileforge_spec_profile_json()
    _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=spec_content,
    )
    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["accepted"] is False
    assert result["authority_status"] == "pending_acceptance"
    assert session.exec(select(SpecAuthorityAcceptance)).all() == []


def test_update_spec_and_compile_authority_rejects_malformed_postcompile_row(
    session: Session,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-compile metrics require the exact persisted row to parse as v3."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)

    def fake_compile(
        *,
        spec_version_id: int,
        **_: object,
    ) -> dict[str, object]:
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="a" * 64,
            compiled_at=datetime.now(UTC),
            compiled_artifact_json="not-json",
            scope_themes='["false-success"]',
            invariants='["false-success"]',
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        return {
            "success": True,
            "cached": False,
            "authority_id": require_id(authority.authority_id, "authority_id"),
        }

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        fake_compile,
    )

    _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=_agileforge_spec_profile_json(),
    )
    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": _agileforge_spec_profile_json(),
        }
    )

    assert result["success"] is False
    assert result["error_code"] == "COMPILED_AUTHORITY_INVALID"
    assert result["details"]["load_status"] == "invalid_json"
    assert "num_scope_themes" not in result


def test_update_spec_and_compile_authority_loads_content_ref(
    session: Session,
    sample_project: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify update spec and compile authority loads content ref."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_path = tmp_path / "service_spec.json"
    spec_content = _agileforge_spec_profile_json()
    spec_path.write_text(spec_content, encoding="utf-8")
    accepted_spec = _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=spec_content,
        content_ref=str(spec_path),
    )

    def fake_compile(
        *,
        spec_version_id: int,
        force_recompile: bool,
        tool_context: object,
        compiler_model: str | None = None,
    ) -> object:
        del force_recompile, tool_context, compiler_model
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="c" * 64,
            compiled_at=datetime.now(UTC),
            compiled_artifact_json=_stored_compiled_success_json(),
            scope_themes='["Payments"]',
            invariants='["REQUIRED_FIELD:email"]',
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        return {
            "success": True,
            "cached": False,
            "authority_id": require_id(authority.authority_id, "authority_id"),
        }

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        fake_compile,
        raising=False,
    )
    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "content_ref": str(spec_path),
        },
        tool_context=None,
    )

    assert result["success"] is True

    spec_row = session.get(
        SpecRegistry,
        require_id(accepted_spec.spec_version_id, "spec_version_id"),
    )
    assert spec_row is not None
    assert spec_row.content == _canonical_agileforge_spec_profile_json()
    assert spec_row.content_ref == str(spec_path)


def test_update_spec_and_compile_authority_reuses_existing_version_for_same_hash(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify update spec and compile authority reuses existing version for same hash."""  # noqa: E501
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    compile_calls: list[dict[str, object]] = []
    authority_counter = {"value": 0}

    def fake_compile(
        *,
        spec_version_id: int,
        force_recompile: bool,
        tool_context: object,
        compiler_model: str | None = None,
    ) -> object:
        del tool_context, compiler_model
        compile_calls.append(
            {
                "spec_version_id": spec_version_id,
                "force_recompile": force_recompile,
            }
        )
        existing = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == spec_version_id
            )
        ).first()
        if existing is None:
            authority_counter["value"] += 1
            authority = CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version="3.0.0",
                prompt_hash=f"{authority_counter['value']:064d}"[-64:],
                compiled_at=datetime.now(UTC),
                compiled_artifact_json=_stored_compiled_success_json(),
                scope_themes='["Payments"]',
                invariants='["REQUIRED_FIELD:email"]',
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            )
            session.add(authority)
            session.commit()
            session.refresh(authority)
            authority_id = require_id(authority.authority_id, "authority_id")
        else:
            authority_id = require_id(existing.authority_id, "authority_id")
        return {
            "success": True,
            "cached": True,
            "authority_id": authority_id,
        }

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        fake_compile,
        raising=False,
    )
    spec_content = _agileforge_spec_profile_json()
    _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=spec_content,
    )
    first = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )
    second = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )

    assert first["success"] is True
    assert second["success"] is True
    assert first["spec_version_id"] == second["spec_version_id"]
    assert second["cache_hit"] is True
    assert len(compile_calls) == 2  # noqa: PLR2004
    assert (
        len(
            session.exec(
                select(SpecRegistry).where(
                    SpecRegistry.project_id
                    == require_id(sample_project.project_id, "project_id")
                )
            ).all()
        )
        == 1
    )


def test_update_spec_and_compile_authority_treats_recompile_none_as_false(
    session: Session, sample_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify update spec and compile authority treats recompile none as false."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    compile_calls: dict[str, object] = {}

    def fake_compile(
        *,
        spec_version_id: int,
        force_recompile: bool,
        tool_context: object,
        compiler_model: str | None = None,
    ) -> object:
        del tool_context, compiler_model
        compile_calls["force_recompile"] = force_recompile
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="d" * 64,
            compiled_at=datetime.now(UTC),
            compiled_artifact_json=_stored_compiled_success_json(),
            scope_themes='["Payments"]',
            invariants='["REQUIRED_FIELD:email"]',
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        return {
            "success": True,
            "cached": False,
            "authority_id": require_id(authority.authority_id, "authority_id"),
        }

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        fake_compile,
        raising=False,
    )
    spec_content = _agileforge_spec_profile_json()
    _create_spec_version(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        content=spec_content,
    )
    result = compiler_service.update_spec_and_compile_authority(
        {
            "project_id": require_id(sample_project.project_id, "project_id"),
            "spec_content": spec_content,
            "recompile": None,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert compile_calls["force_recompile"] is False
    assert result["cache_hit"] is False
