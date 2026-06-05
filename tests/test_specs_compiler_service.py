"""Tests for specs compiler service."""

import json
import time
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecAuthorityAcceptance,
    SpecAuthorityStatus,
    SpecRegistry,
)
from db.migrations import ensure_schema_current
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from tests.typing_helpers import make_tool_context, require_id
from utils import failure_artifacts
from utils.failure_artifacts import AgentInvocationError
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
        compiler_version="1.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _stored_compiled_success_json() -> str:
    return json.dumps(v2_compiled_authority_payload())


def v2_compiled_authority_payload() -> dict[str, object]:
    """Return a stored v2 compiled-authority payload fixture."""
    return {
        "schema_version": "agileforge.compiled_authority.v2",
        "scope_themes": ["Payments"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0123456789abcdef",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "email"},
            }
        ],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "2.0.0",
        "prompt_hash": "a" * 64,
        "ir_schema_version": None,
        "ir_provenance": None,
        "source_units": [],
        "requirement_candidates": [],
        "authority_mappings": [],
        "ir_packet_limits": None,
    }


def legacy_compiled_authority_payload() -> dict[str, object]:
    """Return a legacy stored payload fixture without schema_version."""
    payload = v2_compiled_authority_payload()
    payload.pop("schema_version")
    payload["compiler_version"] = "1.0.0"
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
            "schema_version": "agileforge.compiled_authority.v2",
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
        compiler_version="1.0.0",
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
        compiler_version="1.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


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
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["TodoMVC"],
        domain="todo",
        invariants=[
            Invariant(
                id="INV-0123456789abcdef",
                type=InvariantType.USER_INTERACTION,
                parameters=UserInteractionParams(
                    source_item_id=source_item_id,
                    source_level=source_level,
                    trigger="user action",
                    target=source_item_id,
                    expected_response=f"Honor {source_item_id}.",
                ),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="1.0.0",
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
    sample_product: Product,
) -> None:
    """Invalid structured spec JSON returns a structured compile/update error."""
    from services.specs import compiler_service  # noqa: PLC0415

    result = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
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
    product_id: int,
    content: str | None = None,
) -> SpecRegistry:
    if content is None:
        content = _canonical_agileforge_spec_profile_json()
    spec_row = SpecRegistry(
        product_id=product_id,
        spec_hash=f"{product_id:064d}"[-64:],
        content=content,
        content_ref=None,
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="tester",
        approval_notes="approved",
    )
    session.add(spec_row)
    session.commit()
    session.refresh(spec_row)
    return spec_row


def _create_compiled_authority(
    session: Session,
    *,
    spec_version_id: int,
    artifact_json: str,
) -> CompiledSpecAuthority:
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="1.2.3",
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
    """Verify load compiled artifact returns success result for v2 payloads."""
    from services.specs.compiler_service import (  # noqa: PLC0415
        CompiledArtifactLoadResult,
        load_compiled_artifact,
    )

    authority = SimpleNamespace(
        compiled_artifact_json=json.dumps(v2_compiled_authority_payload())
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
    assert result.observed_schema_version == "agileforge.compiled_authority.v2"
    assert result.validation_error is None
    assert result.artifact.scope_themes == ["Payments"]
    assert result.artifact.invariants[0].id == "INV-0123456789abcdef"
    with pytest.raises(FrozenInstanceError):
        result.status = "missing"  # type: ignore[misc]


def test_compiled_authority_artifact_json_round_trips_through_loader() -> None:
    """Stored-artifact serializer should emit a v2 envelope the loader accepts."""
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

    assert payload["schema_version"] == "agileforge.compiled_authority.v2"
    assert result.status == "success"
    assert result.artifact is not None
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
    """Verify stored artifacts with non-v2 schema_version fail before validation."""
    from services.specs.compiler_service import load_compiled_artifact  # noqa: PLC0415

    payload = v2_compiled_authority_payload()
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


def test_load_compiled_artifact_reports_validation_error_for_invalid_v2_payload(
) -> None:
    """Verify invalid v2 payloads expose schema-invalid result details."""
    from services.specs.compiler_service import load_compiled_artifact  # noqa: PLC0415

    payload = v2_compiled_authority_payload()
    payload["invariants"] = "bad"
    authority = SimpleNamespace(compiled_artifact_json=json.dumps(payload))

    result = load_compiled_artifact(authority)

    assert result.ok is False
    assert result.status == "schema_invalid"
    assert result.unsupported is False
    assert result.artifact is None
    assert result.error_code is None
    assert result.message == "Compiled authority artifact failed schema validation."
    assert result.observed_schema_version == "agileforge.compiled_authority.v2"
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
    assert result.observed_schema_version == "agileforge.compiled_authority.v2"
    assert result.validation_error is None


def test_services_package_exports_ensure_accepted_spec_authority() -> None:
    """Verify services package exports ensure accepted spec authority."""
    from services import specs  # noqa: PLC0415
    from services.specs import compiler_service  # noqa: PLC0415

    assert (
        specs.ensure_accepted_spec_authority
        is compiler_service.ensure_accepted_spec_authority
    )


def test_ensure_accepted_spec_authority_reuses_existing_accepted_version(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ensure accepted spec authority reuses existing accepted version."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    monkeypatch.setattr(spec_tools, "engine", session.get_bind(), raising=False)

    spec_row = _create_spec_version(
        session, product_id=require_id(sample_product.product_id, "product_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )
    acceptance = SpecAuthorityAcceptance(
        product_id=require_id(sample_product.product_id, "product_id"),
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        status="accepted",
        policy="auto",
        decided_by="system",
        decided_at=datetime.now(UTC),
        rationale="Auto-accepted for test",
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        spec_hash=spec_row.spec_hash,
    )
    session.add(acceptance)
    session.commit()

    result = compiler_service.ensure_accepted_spec_authority(
        product_id=require_id(sample_product.product_id, "product_id"),
    )

    assert result == require_id(spec_row.spec_version_id, "spec_version_id")


def test_ensure_accepted_spec_authority_honors_legacy_tool_update_monkeypatch(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
            "product_id": require_id(sample_product.product_id, "product_id"),
        }

    monkeypatch.setattr(
        spec_tools,
        "update_spec_and_compile_authority",
        fake_update,
        raising=False,
    )

    result = compiler_service.ensure_accepted_spec_authority(
        product_id=require_id(sample_product.product_id, "product_id"),
        spec_content="# Spec",
        recompile=True,
    )

    assert result == 777  # noqa: PLR2004
    assert captured["params"] == {
        "product_id": require_id(sample_product.product_id, "product_id"),
        "recompile": True,
        "spec_content": "# Spec",
    }
    assert captured["tool_context"] is None


def test_ensure_spec_authority_accepted_inserts_new_acceptance(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ensure spec authority accepted inserts new acceptance."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session, product_id=require_id(sample_product.product_id, "product_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )

    acceptance = compiler_service.ensure_spec_authority_accepted(
        product_id=require_id(sample_product.product_id, "product_id"),
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        policy="auto",
        decided_by="system",
        rationale="Auto-accepted on compile success",
    )

    assert acceptance.spec_version_id == require_id(
        spec_row.spec_version_id, "spec_version_id"
    )
    assert acceptance.product_id == require_id(sample_product.product_id, "product_id")
    assert acceptance.status == "accepted"
    assert acceptance.policy == "auto"
    assert acceptance.decided_by == "system"
    assert acceptance.compiler_version == authority.compiler_version
    assert acceptance.prompt_hash == authority.prompt_hash
    assert acceptance.spec_hash == spec_row.spec_hash


def test_ensure_spec_authority_accepted_returns_existing_acceptance(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ensure spec authority accepted returns existing acceptance."""
    from agile_sqlmodel import SpecAuthorityAcceptance  # noqa: PLC0415
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session, product_id=require_id(sample_product.product_id, "product_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )
    existing = SpecAuthorityAcceptance(
        product_id=require_id(sample_product.product_id, "product_id"),
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        status="accepted",
        policy="human",
        decided_by="reviewer",
        decided_at=datetime.now(UTC),
        rationale="Manual approval",
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        spec_hash=spec_row.spec_hash,
    )
    session.add(existing)
    session.commit()
    session.refresh(existing)

    acceptance = compiler_service.ensure_spec_authority_accepted(
        product_id=require_id(sample_product.product_id, "product_id"),
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        policy="auto",
        decided_by="system",
        rationale="Should be ignored",
    )

    assert acceptance.id == existing.id
    assert acceptance.policy == "human"
    assert acceptance.decided_by == "reviewer"


def test_ensure_spec_authority_accepted_rejects_failure_artifact(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ensure spec authority accepted rejects failure artifact."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session, product_id=require_id(sample_product.product_id, "product_id")
    )
    _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_compiled_failure_json(),
    )

    with pytest.raises(
        ValueError,
        match="compiled artifact invalid",
    ):
        compiler_service.ensure_spec_authority_accepted(
            product_id=require_id(sample_product.product_id, "product_id"),
            spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
            policy="auto",
            decided_by="system",
        )


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
        invariant.parameters.source_item_id
        for invariant in compiled.root.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
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
        invariant.parameters.source_item_id
        for invariant in compiled.root.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
    }
    assert covered_item_ids == {"REQ.todo-create", "REQ.todo-toggle"}
    assert calls[0] == ["REQ.todo-create", "REQ.todo-toggle", "REQ.todo-color"]


def test_preview_spec_authority_retries_transient_focused_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient focused item failure should not abort structured compilation."""
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
            return _vacant_success_json()

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
        invariant.parameters.source_item_id
        for invariant in compiled.root.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
    }
    assert covered_item_ids == {"REQ.todo-create", "REQ.todo-toggle"}
    assert focused_attempts["REQ.todo-create"] == 2  # noqa: PLR2004


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


def test_resolve_engine_honors_legacy_spec_tools_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve engine honors legacy spec tools engine."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    sentinel_engine = object()
    monkeypatch.setattr(spec_tools, "engine", sentinel_engine, raising=False)
    monkeypatch.setattr(
        spec_tools,
        "get_engine",
        compiler_service.get_engine,
    )

    resolved = compiler_service._resolve_engine()

    assert resolved is sentinel_engine


def test_resolve_engine_prefers_patched_spec_tools_get_engine_over_stale_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve engine prefers patched spec tools get engine over stale engine."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    stale_engine = object()
    preferred_engine = object()
    monkeypatch.setattr(spec_tools, "engine", stale_engine, raising=False)
    monkeypatch.setattr(spec_tools, "get_engine", lambda: preferred_engine)

    resolved = compiler_service._resolve_engine()

    assert resolved is preferred_engine


def test_default_compiler_invocation_rejects_unstructured_spec_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler invocation requires canonical agileforge.spec.v1 JSON."""
    from services.specs import compiler_service  # noqa: PLC0415

    captured: list[SpecAuthorityCompilerInput] = []

    async def fake_invoke(payload: SpecAuthorityCompilerInput) -> str:
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
            product_id=4,
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

    async def fake_invoke(payload: SpecAuthorityCompilerInput) -> str:
        captured.append(payload)
        return _success_payload_json()

    monkeypatch.setattr(
        "services.specs.compiler_service._invoke_spec_authority_compiler_async",
        fake_invoke,
    )

    compiler_service._default_invoke_spec_authority_compiler(
        spec_content=json.dumps(_agileforge_spec_profile_payload()),
        content_ref=None,
        product_id=4,
        spec_version_id=9,
    )

    assert len(captured) == 1
    assert captured[0].spec_source_format == "agileforge.spec.v1"


def test_compile_spec_authority_for_version_persists_authority(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
        session, product_id=require_id(sample_product.product_id, "product_id")
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
    assert sample_product.compiled_authority_json is not None
    assert tool_context.state["compiled_authority_cached"] is not None

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id
            == require_id(spec_row.spec_version_id, "spec_version_id")
        )
    ).first()
    assert authority is not None
    assert authority.compiled_artifact_json == sample_product.compiled_authority_json
    load_result = compiler_service.load_compiled_artifact(authority)
    assert load_result.status == "success"
    assert load_result.artifact is not None


def test_compile_spec_authority_for_version_iteratively_persists_must_coverage(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
        product_id=require_id(sample_product.product_id, "product_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )

    result = compiler_service.compile_spec_authority_for_version(
        {"spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id")},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert sample_product.compiled_authority_json is not None
    load_result = compiler_service.load_compiled_artifact(
        SimpleNamespace(compiled_artifact_json=sample_product.compiled_authority_json)
    )
    assert load_result.status == "success"
    assert load_result.artifact is not None
    covered_item_ids = {
        invariant.parameters.source_item_id
        for invariant in load_result.artifact.invariants
        if isinstance(invariant.parameters, UserInteractionParams)
    }
    assert covered_item_ids == {"REQ.todo-create", "REQ.todo-toggle"}
    assert ["REQ.todo-create"] in calls
    assert ["REQ.todo-toggle"] in calls
    assert ["REQ.todo-color"] not in calls


def test_update_spec_and_compile_authority_suppresses_auto_accept_for_vacant_authority(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vacant authority blocks update+compile before persistence and auto-accept."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(compiler_service, "get_engine", session.get_bind)
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _vacant_success_json(),
    )

    accept_calls: list[object] = []

    def record_accept_call(**kwargs: object) -> object:
        accept_calls.append(kwargs)
        return SimpleNamespace(
            status="accepted",
            policy="auto",
            decided_at=SimpleNamespace(isoformat=lambda: "2026-05-21T00:00:00+00:00"),
            decided_by="system",
        )

    monkeypatch.setattr(
        compiler_service,
        "_ensure_spec_authority_accepted",
        record_accept_call,
    )

    result = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_content": _agileforge_spec_profile_json(),
        },
        tool_context=None,
    )

    assert result["success"] is False
    assert result["error"] == "SPEC_AUTHORITY_VACANT"
    assert result["reason"] == "NO_INVARIANTS_EXTRACTED"
    assert result["blocking_gaps"] == ["No invariants extracted from spec"]
    assert accept_calls == []
    assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_compile_spec_authority_for_version_with_engine_uses_supplied_engine(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify engine-aware compile path never falls back to module get_engine."""
    from services.specs import compiler_service  # noqa: PLC0415

    ensure_schema_current(engine)
    other_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(other_engine)
    ensure_schema_current(other_engine)

    monkeypatch.setattr(compiler_service, "get_engine", lambda: other_engine)
    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )

    with Session(engine) as supplied_session:
        product = Product(name="Supplied Engine Product", vision="vision")
        supplied_session.add(product)
        supplied_session.commit()
        supplied_session.refresh(product)
        spec = _create_spec_version(
            supplied_session,
            product_id=require_id(product.product_id, "product_id"),
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
    sample_product: Product,
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
        product_id=require_id(sample_product.product_id, "product_id"),
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
    assert "product_authority_cache_persisted" in boundaries
    assert "progress:compiled_authority_persisted" in boundaries
    assert "progress:product_authority_cache_persisted" in boundaries


@pytest.mark.parametrize(
    ("blocked_boundary", "expect_authority", "expect_product_cache"),
    [
        ("compiled_authority_persisted", False, False),
        ("product_authority_cache_persisted", True, False),
    ],
)
def test_compile_spec_authority_for_version_with_engine_lease_loss_blocks_write(  # noqa: PLR0913
    engine: Engine,
    session: Session,
    sample_product: Product,
    monkeypatch: pytest.MonkeyPatch,
    blocked_boundary: str,
    expect_authority: bool,
    expect_product_cache: bool,
) -> None:
    """A lost lease should stop the guarded compiler write."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        lambda **_: _raw_compiler_output_json(),
    )
    spec = _create_spec_version(
        session,
        product_id=require_id(sample_product.product_id, "product_id"),
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
    session.refresh(sample_product)
    assert (authority is not None) is expect_authority
    assert (sample_product.compiled_authority_json is not None) is expect_product_cache


@pytest.mark.parametrize(
    ("failed_boundary", "mode"),
    [
        ("compiled_authority_persisted", "false"),
        ("product_authority_cache_persisted", "raise"),
    ],
)
def test_compile_spec_authority_for_version_with_engine_progress_failure_recovers(  # noqa: PLR0913
    engine: Engine,
    session: Session,
    sample_product: Product,
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
        product_id=require_id(sample_product.product_id, "product_id"),
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
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
        session, product_id=require_id(sample_product.product_id, "product_id")
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
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
        session, product_id=require_id(sample_product.product_id, "product_id")
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
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
        session, product_id=require_id(sample_product.product_id, "product_id")
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
    session.refresh(sample_product)
    assert sample_product.compiled_authority_json == existing.compiled_artifact_json


def test_compile_spec_authority_for_version_uses_content_ref_when_content_empty(
    session: Session,
    sample_product: Product,
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
        product_id=require_id(sample_product.product_id, "product_id"),
        content="",
    )
    spec_row.content_ref = str(spec_path)
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
    sample_product: Product,
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
        session, product_id=require_id(sample_product.product_id, "product_id")
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


def test_check_spec_authority_status_returns_not_compiled_when_no_spec_versions(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify check spec authority status returns not compiled when no spec versions."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    result = compiler_service.check_spec_authority_status(
        {"product_id": require_id(sample_product.product_id, "product_id")},
        tool_context=None,
    )

    assert result == {
        "success": True,
        "status": SpecAuthorityStatus.NOT_COMPILED.value,
        "status_details": "No spec versions exist for this product",
        "message": "Status: NOT_COMPILED (no specs)",
    }


def test_check_spec_authority_status_prefers_pending_review_over_stale(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify check spec authority status prefers pending review over stale."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    approved_spec = _create_spec_version(
        session,
        product_id=require_id(sample_product.product_id, "product_id"),
        content="Approved Spec",
    )
    _create_compiled_authority(
        session,
        spec_version_id=require_id(approved_spec.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )

    draft_spec = SpecRegistry(
        product_id=require_id(sample_product.product_id, "product_id"),
        spec_hash="d" * 64,
        content="Draft Spec",
        content_ref=None,
        status="draft",
        approved_at=None,
        approved_by=None,
        approval_notes=None,
    )
    session.add(draft_spec)
    session.commit()
    session.refresh(draft_spec)

    result = compiler_service.check_spec_authority_status(
        {"product_id": require_id(sample_product.product_id, "product_id")},
        tool_context=None,
    )

    draft_spec_version_id = require_id(draft_spec.spec_version_id, "spec_version_id")
    assert result == {
        "success": True,
        "status": SpecAuthorityStatus.PENDING_REVIEW.value,
        "status_details": (
            f"Latest spec version {draft_spec_version_id} is {draft_spec.status}"
        ),
        "latest_spec_version_id": draft_spec_version_id,
        "message": "Status: PENDING_REVIEW (latest spec not approved)",
    }


def test_check_spec_authority_status_does_not_report_legacy_artifact_current(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy stored artifacts without schema_version are not CURRENT."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session,
        product_id=require_id(sample_product.product_id, "product_id"),
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=json.dumps(legacy_compiled_authority_payload()),
    )

    result = compiler_service.check_spec_authority_status(
        {"product_id": require_id(sample_product.product_id, "product_id")},
        tool_context=None,
    )

    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    assert result == {
        "success": True,
        "status": SpecAuthorityStatus.NOT_COMPILED.value,
        "status_details": (
            f"Latest approved spec version {spec_version_id} has an unreadable "
            "compiled artifact (schema_unsupported)."
        ),
        "latest_approved_spec_version_id": spec_version_id,
        "authority_id": require_id(authority.authority_id, "authority_id"),
        "remediation": (
            "Recompile the latest approved spec authority to persist a supported "
            "compiled artifact."
        ),
        "message": "Status: NOT_COMPILED (compiled artifact unreadable)",
    }


def test_get_compiled_authority_by_version_returns_expected_envelope(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify get compiled authority by version returns expected envelope."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session, product_id=require_id(sample_product.product_id, "product_id")
    )
    authority = _create_compiled_authority(
        session,
        spec_version_id=require_id(spec_row.spec_version_id, "spec_version_id"),
        artifact_json=_stored_compiled_success_json(),
    )

    result = compiler_service.get_compiled_authority_by_version(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
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


def test_get_compiled_authority_by_version_falls_back_to_legacy_columns(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify get compiled authority by version falls back to legacy columns."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    spec_row = _create_spec_version(
        session, product_id=require_id(sample_product.product_id, "product_id")
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
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id"),
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["scope_themes"] == ["Legacy Theme"]
    assert result["invariants"] == ["FORBIDDEN_CAPABILITY:upload"]
    assert result["eligible_feature_ids"] == [10, 11]
    assert result["rejected_features"] == ["Feature X"]
    assert result["spec_gaps"] == ["gap one"]


def test_get_compiled_authority_by_version_returns_existing_error_messages(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_version_id": 999999,
        },
        tool_context=None,
    )
    assert not_found == {"success": False, "error": "Spec version 999999 not found"}

    spec_row = _create_spec_version(
        session, product_id=require_id(sample_product.product_id, "product_id")
    )
    other_product = Product(
        name="Other Product",
        description="Other",
        vision="Other vision",
    )
    session.add(other_product)
    session.commit()
    session.refresh(other_product)

    mismatch = compiler_service.get_compiled_authority_by_version(
        {
            "product_id": require_id(other_product.product_id, "product_id"),
            "spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id"),
        },
        tool_context=None,
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    other_product_id = require_id(other_product.product_id, "product_id")
    assert mismatch == {
        "success": False,
        "error": (
            f"Spec version {spec_version_id} does not belong to "
            f"product {other_product_id} (mismatch)"
        ),
    }

    not_compiled = compiler_service.get_compiled_authority_by_version(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_version_id": require_id(spec_row.spec_version_id, "spec_version_id"),
        },
        tool_context=None,
    )
    assert not_compiled == {
        "success": False,
        "error": (
            f"Spec version {spec_version_id} is not compiled. "
            "Use compile_spec_authority to compile it."
        ),
    }


@pytest.fixture
def sample_product(session: Session) -> Product:
    """Return product."""
    product = Product(
        name="Compiler Service Product",
        description="Product for compiler service tests",
        vision="Keep compiler orchestration outside tool modules",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def test_update_spec_and_compile_authority_creates_spec_and_delegates_compile(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify update spec and compile authority creates spec and delegates compile."""
    from services.specs import compiler_service  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )

    compile_calls: dict[str, object] = {}
    acceptance_calls: dict[str, object] = {}

    def fake_compile(
        *, spec_version_id: int, force_recompile: bool, tool_context: object
    ) -> object:
        compile_calls["spec_version_id"] = spec_version_id
        compile_calls["force_recompile"] = force_recompile
        compile_calls["tool_context"] = tool_context

        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="1.2.3",
            prompt_hash="b" * 64,
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

    def fake_accept(
        *,
        product_id: int,
        spec_version_id: int,
        policy: str,
        decided_by: str,
        rationale: str | None = None,
    ) -> object:
        acceptance_calls["product_id"] = product_id
        acceptance_calls["spec_version_id"] = spec_version_id
        acceptance_calls["policy"] = policy
        acceptance_calls["decided_by"] = decided_by
        acceptance_calls["rationale"] = rationale
        return SimpleNamespace(
            status="accepted",
            policy=policy,
            decided_at=SimpleNamespace(isoformat=lambda: "2026-04-05T00:00:00+00:00"),
            decided_by=decided_by,
        )

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        fake_compile,
        raising=False,
    )
    monkeypatch.setattr(
        compiler_service,
        "_ensure_spec_authority_accepted",
        fake_accept,
        raising=False,
    )

    spec_content = _agileforge_spec_profile_json()
    result = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["product_id"] == require_id(sample_product.product_id, "product_id")
    assert result["cache_hit"] is False
    assert result["accepted"] is True
    assert compile_calls["force_recompile"] is False
    assert acceptance_calls["product_id"] == require_id(
        sample_product.product_id, "product_id"
    )

    spec_row = session.get(SpecRegistry, result["spec_version_id"])
    assert spec_row is not None
    assert spec_row.content == _canonical_agileforge_spec_profile_json()
    assert spec_row.status == "approved"
    assert spec_row.approved_by == "implicit"


def test_update_spec_and_compile_authority_honors_tool_compile_override(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
            compiler_version="1.2.3",
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
    monkeypatch.setattr(
        compiler_service,
        "_ensure_spec_authority_accepted",
        lambda **_: SimpleNamespace(
            status="accepted",
            policy="auto",
            decided_at=SimpleNamespace(isoformat=lambda: "2026-04-05T00:00:00+00:00"),
            decided_by="system",
        ),
        raising=False,
    )

    spec_content = _agileforge_spec_profile_json()
    result = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert compile_params["spec_version_id"] == result["spec_version_id"]
    assert compile_params["force_recompile"] is False


def test_update_spec_and_compile_authority_honors_tool_acceptance_override(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify update spec and compile authority honors tool acceptance override."""
    from services.specs import compiler_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    monkeypatch.setattr(
        compiler_service,
        "get_engine",
        session.get_bind,
    )
    monkeypatch.setattr(
        compiler_service,
        "ensure_spec_authority_accepted",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("service acceptance path should be bypassed")
        ),
    )

    def fake_compile(
        *, spec_version_id: int, force_recompile: bool, tool_context: object
    ) -> object:
        del force_recompile, tool_context
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="1.2.3",
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

    acceptance_calls: dict[str, object] = {}

    def fake_tool_ensure(
        *,
        product_id: int,
        spec_version_id: int,
        policy: str,
        decided_by: str,
        rationale: str | None = None,
    ) -> object:
        acceptance_calls["product_id"] = product_id
        acceptance_calls["spec_version_id"] = spec_version_id
        acceptance_calls["policy"] = policy
        acceptance_calls["decided_by"] = decided_by
        acceptance_calls["rationale"] = rationale
        return SimpleNamespace(
            status="accepted",
            policy=policy,
            decided_at=SimpleNamespace(isoformat=lambda: "2026-04-05T00:00:00+00:00"),
            decided_by=decided_by,
        )

    monkeypatch.setattr(
        compiler_service,
        "compile_spec_authority_for_version",
        fake_compile,
    )
    monkeypatch.setattr(
        spec_tools,
        "ensure_spec_authority_accepted",
        fake_tool_ensure,
    )

    spec_content = _agileforge_spec_profile_json()
    result = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert acceptance_calls["product_id"] == require_id(
        sample_product.product_id, "product_id"
    )
    assert acceptance_calls["spec_version_id"] == result["spec_version_id"]
    assert acceptance_calls["policy"] == "auto"


def test_update_spec_and_compile_authority_loads_content_ref(
    session: Session,
    sample_product: Product,
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

    def fake_compile(
        *, spec_version_id: int, force_recompile: bool, tool_context: object
    ) -> object:
        del force_recompile, tool_context
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="1.2.3",
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
    monkeypatch.setattr(
        compiler_service,
        "_ensure_spec_authority_accepted",
        lambda **_: SimpleNamespace(
            status="accepted",
            policy="auto",
            decided_at=SimpleNamespace(isoformat=lambda: "2026-04-05T00:00:00+00:00"),
            decided_by="system",
        ),
        raising=False,
    )

    result = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "content_ref": str(spec_path),
        },
        tool_context=None,
    )

    assert result["success"] is True

    spec_row = session.get(SpecRegistry, result["spec_version_id"])
    assert spec_row is not None
    assert spec_row.content == _canonical_agileforge_spec_profile_json()
    assert spec_row.content_ref == str(spec_path)


def test_update_spec_and_compile_authority_reuses_existing_version_for_same_hash(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
        *, spec_version_id: int, force_recompile: bool, tool_context: object
    ) -> object:
        del tool_context
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
                compiler_version="1.2.3",
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
    monkeypatch.setattr(
        compiler_service,
        "_ensure_spec_authority_accepted",
        lambda **_: SimpleNamespace(
            status="accepted",
            policy="auto",
            decided_at=SimpleNamespace(isoformat=lambda: "2026-04-05T00:00:00+00:00"),
            decided_by="system",
        ),
        raising=False,
    )

    spec_content = _agileforge_spec_profile_json()
    first = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_content": spec_content,
        },
        tool_context=None,
    )
    second = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
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
                    SpecRegistry.product_id
                    == require_id(sample_product.product_id, "product_id")
                )
            ).all()
        )
        == 1
    )


def test_update_spec_and_compile_authority_treats_recompile_none_as_false(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
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
        *, spec_version_id: int, force_recompile: bool, tool_context: object
    ) -> object:
        del tool_context
        compile_calls["force_recompile"] = force_recompile
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="1.2.3",
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
    monkeypatch.setattr(
        compiler_service,
        "_ensure_spec_authority_accepted",
        lambda **_: SimpleNamespace(
            status="accepted",
            policy="auto",
            decided_at=SimpleNamespace(isoformat=lambda: "2026-04-05T00:00:00+00:00"),
            decided_by="system",
        ),
        raising=False,
    )

    spec_content = _agileforge_spec_profile_json()
    result = compiler_service.update_spec_and_compile_authority(
        {
            "product_id": require_id(sample_product.product_id, "product_id"),
            "spec_content": spec_content,
            "recompile": None,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert compile_calls["force_recompile"] is False
    assert result["cache_hit"] is False
