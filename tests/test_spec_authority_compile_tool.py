"""Tests for Spec Authority compile tool used by the orchestrator."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from agile_sqlmodel import CompiledSpecAuthority, Product
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.compiler_contract import (  # noqa: E501
    compute_invariant_id_from_payload,
    compute_prompt_hash,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from services.specs.compiler_service import (
    CheckSpecAuthorityStatusInput as ServiceCheckSpecAuthorityStatusInput,
)
from services.specs.compiler_service import (
    CompileSpecAuthorityForVersionInput as ServiceCompileSpecAuthorityForVersionInput,
)
from services.specs.compiler_service import (
    CompileSpecAuthorityInput as ServiceCompileSpecAuthorityInput,
)
from services.specs.compiler_service import (
    GetCompiledAuthorityInput as ServiceGetCompiledAuthorityInput,
)
from services.specs.compiler_service import (
    PreviewSpecAuthorityInput as ServicePreviewSpecAuthorityInput,
)
from services.specs.compiler_service import (
    UpdateSpecAndCompileAuthorityInput as ServiceUpdateSpecAndCompileAuthorityInput,
)
from tools import spec_tools
from tools.spec_tools import (
    CheckSpecAuthorityStatusInput,
    CompileSpecAuthorityForVersionInput,
    CompileSpecAuthorityInput,
    GetCompiledAuthorityInput,
    PreviewSpecAuthorityInput,
    UpdateSpecAndCompileAuthorityInput,
    approve_spec_version,
    compile_spec_authority_for_version,
    register_spec_version,
)
from utils import failure_artifacts
from utils.failure_artifacts import AgentInvocationError
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

_INSTRUCTION_REQUIRED_TERMS = (
    "agileforge.compiled_authority.v2",
    "source_map.location",
    "source_map.excerpt",
    "semantic fields only",
    "Do not put source_item_id or source_level inside parameters.",
)
_INSTRUCTION_FORBIDDEN_TERMS = (
    'USER_INTERACTION => parameters: {"source_item_id"',
    'STATE_TRANSITION => parameters: {"source_item_id"',
    'DATA_CONTRACT => parameters: {"source_item_id"',
    'ROUTE_CONTRACT => parameters: {"source_item_id"',
    'VISIBILITY_RULE => parameters: {"source_item_id"',
)


def _assert_v2_instruction_contract(instructions: str) -> None:
    """Assert the durable v2 instruction contract without prose-coupling."""
    for required_term in _INSTRUCTION_REQUIRED_TERMS:
        assert required_term in instructions

    assert '"schema_version": "agileforge.compiled_authority.v2"' in instructions
    assert 'USER_INTERACTION => invariant: {"type": "USER_INTERACTION"' in instructions

    for forbidden_term in _INSTRUCTION_FORBIDDEN_TERMS:
        assert forbidden_term not in instructions


@pytest.fixture
def sample_product(session: Session, engine: Engine) -> Product:
    """Create a product without spec."""
    spec_tools.engine = engine

    product = Product(
        name="Compile Tool Product",
        description="Product for compile tool tests",
        vision="Keep spec authority deterministic",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@pytest.fixture
def sample_spec_content() -> str:
    """Sample spec content for testing."""
    return """
# Technical Specification v1

## Scope
- Feature A: User authentication
- Feature B: Data export

## Invariants
- All API calls MUST require auth token.
- Export formats SHALL be CSV or JSON only.
"""


@pytest.fixture
def compiler_stub(monkeypatch: pytest.MonkeyPatch) -> object:
    """Stub compiler agent to avoid real LLM calls."""
    raw_json = _build_raw_compiler_output(
        excerpt="The payload must include user_id.",
        field_name="user_id",
    )
    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        lambda **_: raw_json,
    )
    return raw_json


def test_compile_tool_blocks_unapproved_spec(
    session: Session, sample_product: Product, sample_spec_content: str
) -> None:
    """Compilation should fail for unapproved spec versions."""
    del session
    reg_result = register_spec_version(
        {"product_id": sample_product.product_id, "content": sample_spec_content},
        tool_context=None,
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": reg_result["spec_version_id"]},
        tool_context=None,
    )

    assert result["success"] is False
    assert "not approved" in result["error"].lower()


def test_tool_compile_input_models_alias_service_models() -> None:
    """Tool-facing compiler input models should be compatibility aliases."""
    assert PreviewSpecAuthorityInput is ServicePreviewSpecAuthorityInput
    assert CompileSpecAuthorityInput is ServiceCompileSpecAuthorityInput
    assert (
        CompileSpecAuthorityForVersionInput
        is ServiceCompileSpecAuthorityForVersionInput
    )
    assert (
        UpdateSpecAndCompileAuthorityInput is ServiceUpdateSpecAndCompileAuthorityInput
    )
    assert CheckSpecAuthorityStatusInput is ServiceCheckSpecAuthorityStatusInput
    assert GetCompiledAuthorityInput is ServiceGetCompiledAuthorityInput


def test_tool_runtime_helpers_delegate_to_compiler_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy tool helper names should forward directly to compiler_service."""
    captured: dict[str, object] = {}

    def fake_run_async_task(coro: object) -> str:
        captured["run_async_task"] = coro
        return "ran"

    def fake_extract_compiler_response_text(events: object) -> str:
        captured["extract_compiler_response_text"] = events
        return "text"

    async def fake_invoke_async(input_payload: object) -> str:
        captured["invoke_async"] = input_payload
        return "async-raw-json"

    def fake_invoke(
        spec_content: str,
        content_ref: str | None,
        product_id: int | None,
        spec_version_id: int | None,
    ) -> str:
        captured["invoke"] = {
            "spec_content": spec_content,
            "content_ref": content_ref,
            "product_id": product_id,
            "spec_version_id": spec_version_id,
        }
        return "raw-json"

    monkeypatch.setattr(
        spec_tools,
        "_service_run_async_task",
        fake_run_async_task,
        raising=False,
    )
    monkeypatch.setattr(
        spec_tools,
        "_service_extract_compiler_response_text",
        fake_extract_compiler_response_text,
        raising=False,
    )
    monkeypatch.setattr(
        spec_tools,
        "_service_invoke_spec_authority_compiler_async",
        fake_invoke_async,
        raising=False,
    )
    monkeypatch.setattr(
        spec_tools,
        "_service_invoke_spec_authority_compiler",
        fake_invoke,
        raising=False,
    )

    async def _coro_token() -> str:
        return "token"

    coro_token = _coro_token()
    payload_token = object()
    events_token = [object()]

    try:
        assert spec_tools._run_async_task(coro_token) == "ran"
        assert captured["run_async_task"] is coro_token
    finally:
        coro_token.close()

    assert spec_tools._extract_compiler_response_text(events_token) == "text"
    assert captured["extract_compiler_response_text"] == events_token

    assert (
        asyncio.run(spec_tools._invoke_spec_authority_compiler_async(payload_token))
        == "async-raw-json"
    )
    assert captured["invoke_async"] is payload_token

    assert (
        spec_tools._invoke_spec_authority_compiler(
            spec_content="spec text",
            content_ref="spec.md",
            product_id=7,
            spec_version_id=11,
        )
        == "raw-json"
    )
    assert captured["invoke"] == {
        "spec_content": "spec text",
        "content_ref": "spec.md",
        "product_id": 7,
        "spec_version_id": 11,
    }


def test_tool_compiler_failure_and_extractor_helpers_delegate_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool compatibility helpers should forward failure shaping and extraction."""
    captured: dict[str, object] = {}
    failure_result = {"success": False, "error": "boom"}
    extractor_result = object()

    def fake_failure(**kwargs: object) -> object:
        captured["failure"] = kwargs
        return failure_result

    def fake_extract(
        *,
        spec_content: str,
        content_ref: str | None,
        product_id: int,
        spec_version_id: int,
    ) -> object:
        captured["extract"] = {
            "spec_content": spec_content,
            "content_ref": content_ref,
            "product_id": product_id,
            "spec_version_id": spec_version_id,
        }
        return extractor_result

    monkeypatch.setattr(
        spec_tools,
        "_service_compiler_failure_result",
        fake_failure,
        raising=False,
    )
    monkeypatch.setattr(
        spec_tools,
        "_service_extract_spec_authority_llm",
        fake_extract,
        raising=False,
    )

    assert (
        spec_tools._compiler_failure_result(
            product_id=3,
            spec_version_id=5,
            content_ref="spec.md",
            failure_stage="compile",
            error="boom",
            reason="bad data",
            raw_output="{}",
            blocking_gaps=["gap"],
            exception=None,
        )
        == failure_result
    )
    assert captured["failure"] == {
        "product_id": 3,
        "spec_version_id": 5,
        "content_ref": "spec.md",
        "failure_stage": "compile",
        "error": "boom",
        "reason": "bad data",
        "raw_output": "{}",
        "blocking_gaps": ["gap"],
        "exception": None,
    }

    assert (
        spec_tools._extract_spec_authority_llm(
            spec_content="spec text",
            content_ref="spec.md",
            product_id=13,
            spec_version_id=21,
        )
        is extractor_result
    )
    assert captured["extract"] == {
        "spec_content": "spec text",
        "content_ref": "spec.md",
        "product_id": 13,
        "spec_version_id": 21,
    }


def test_compiler_instructions_require_v2_provenance_contract() -> None:
    """Compiler instructions must require the v2 provenance layout."""
    _assert_v2_instruction_contract(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)


def test_compile_tool_compiles_and_returns_summary(
    session: Session,
    sample_product: Product,
    sample_spec_content: str,
    compiler_stub: object,
) -> None:
    """Compilation should create authority and return summary payload."""
    del compiler_stub
    reg_result = register_spec_version(
        {"product_id": sample_product.product_id, "content": sample_spec_content},
        tool_context=None,
    )
    spec_version_id = reg_result["spec_version_id"]

    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"},
        tool_context=None,
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )

    assert result["success"] is True
    assert result["cached"] is False
    assert result["spec_version_id"] == spec_version_id
    assert result["compiler_version"] == SPEC_AUTHORITY_COMPILER_VERSION
    assert len(result["prompt_hash"]) == 64  # noqa: PLR2004
    assert result["scope_themes_count"] >= 1
    assert result["invariants_count"] >= 1

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()

    assert authority is not None

    session.refresh(sample_product)
    assert sample_product.compiled_authority_json is not None


def test_compile_tool_returns_cached_when_already_compiled(
    session: Session,
    sample_product: Product,
    sample_spec_content: str,
    compiler_stub: object,
) -> None:
    """Compilation tool should be idempotent for existing authority."""
    del compiler_stub
    reg_result = register_spec_version(
        {"product_id": sample_product.product_id, "content": sample_spec_content},
        tool_context=None,
    )
    spec_version_id = reg_result["spec_version_id"]

    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"},
        tool_context=None,
    )

    first = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )
    assert first["success"] is True

    result = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )

    assert result["success"] is True
    assert result["cached"] is True
    assert result["authority_id"] == first["authority_id"]

    session.refresh(sample_product)
    assert sample_product.compiled_authority_json is not None


def test_compile_tool_uses_content_ref_when_content_empty(
    session: Session,
    sample_product: Product,
    tmp_path: Path,
    compiler_stub: object,
) -> None:
    """Compilation should load spec content from content_ref when needed."""
    del compiler_stub
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        """
# Spec v1

## API
- Endpoint: /v1/data

## Invariants
- Requests MUST include a token.
""",
        encoding="utf-8",
    )

    reg_result = register_spec_version(
        {
            "product_id": sample_product.product_id,
            "content": "",
            "content_ref": str(spec_path),
        },
        tool_context=None,
    )
    spec_version_id = reg_result["spec_version_id"]

    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"},
        tool_context=None,
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )

    assert result["success"] is True
    assert result["content_source"] == "content_ref"

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()

    assert authority is not None
    scope_themes = json.loads(authority.scope_themes)
    assert len(scope_themes) >= 1


def _build_raw_compiler_output(excerpt: str, field_name: str) -> str:
    """Build a raw compiler JSON output (pre-normalization)."""
    invariant = Invariant(
        id="INV-0000000000000000",
        type=InvariantType.REQUIRED_FIELD,
        parameters=RequiredFieldParams(field_name=field_name),
    )
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Scope"],
        invariants=[invariant],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt=excerpt,
                location=None,
            )
        ],
        compiler_version="0.0.0",
        prompt_hash="0" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def test_compile_persists_compiled_artifact_and_normalized_ids(
    session: Session,
    sample_product: Product,
    sample_spec_content: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compilation should persist normalized artifact and deterministic IDs."""
    reg_result = register_spec_version(
        {"product_id": sample_product.product_id, "content": sample_spec_content},
        tool_context=None,
    )
    spec_version_id = reg_result["spec_version_id"]

    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"},
        tool_context=None,
    )

    raw_json = _build_raw_compiler_output(
        excerpt="The payload must include user_id.",
        field_name="user_id",
    )
    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        lambda **_: raw_json,
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )
    assert result["success"] is True

    session.expire_all()
    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()

    assert authority is not None
    assert authority.compiled_artifact_json

    parsed = SpecAuthorityCompilerOutput.model_validate_json(
        authority.compiled_artifact_json
    )
    assert isinstance(parsed.root, SpecAuthorityCompilationSuccess)

    expected_prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
    assert parsed.root.prompt_hash == expected_prompt_hash
    assert authority.prompt_hash == expected_prompt_hash

    for inv in parsed.root.invariants:
        entry = next(
            (e for e in parsed.root.source_map if e.invariant_id == inv.id), None
        )
        assert entry is not None
        expected_id = compute_invariant_id_from_payload(inv.type, inv.parameters)
        assert inv.id == expected_id
        assert entry.invariant_id == expected_id


def test_compile_cache_hit_does_not_change_compiled_artifact(
    session: Session,
    sample_product: Product,
    sample_spec_content: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache hit should not change artifact unless force_recompile=True."""
    reg_result = register_spec_version(
        {"product_id": sample_product.product_id, "content": sample_spec_content},
        tool_context=None,
    )
    spec_version_id = reg_result["spec_version_id"]

    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"},
        tool_context=None,
    )

    raw_json_1 = _build_raw_compiler_output(
        excerpt="The payload must include user_id.",
        field_name="user_id",
    )
    raw_json_2 = _build_raw_compiler_output(
        excerpt="The payload must include account_id.",
        field_name="account_id",
    )

    call_count = {"count": 0}

    def _fake_invoke(**_: object) -> str:
        call_count["count"] += 1
        return raw_json_1 if call_count["count"] == 1 else raw_json_2

    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        _fake_invoke,
    )

    first = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )
    assert first["success"] is True

    session.expire_all()
    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    assert authority is not None
    artifact_first = authority.compiled_artifact_json

    second = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )
    assert second["success"] is True
    assert second["cached"] is True
    assert call_count["count"] == 1

    session.expire_all()
    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    assert authority is not None
    assert authority.compiled_artifact_json == artifact_first

    third = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id, "force_recompile": True},
        tool_context=None,
    )
    assert third["success"] is True
    assert call_count["count"] == 2  # noqa: PLR2004

    session.expire_all()
    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    assert authority is not None
    assert authority.compiled_artifact_json != artifact_first


def test_compile_persists_invocation_failure_artifact(
    session: Session,
    sample_product: Product,
    sample_spec_content: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify compile persists invocation failure artifact."""
    del session
    reg_result = register_spec_version(
        {"product_id": sample_product.product_id, "content": sample_spec_content},
        tool_context=None,
    )
    spec_version_id = reg_result["spec_version_id"]
    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"},
        tool_context=None,
    )

    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        failure_artifacts, "FAILURES_DIR", tmp_path / "logs" / "failures"
    )
    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        lambda **_: (_ for _ in ()).throw(
            AgentInvocationError("provider timeout", partial_output='{"partial": true}')
        ),
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )

    assert result["success"] is False
    assert result["error"] == "SPEC_COMPILER_INVOCATION_FAILED"
    assert result["failure_stage"] == "invocation_exception"
    artifact = failure_artifacts.read_failure_artifact(result["failure_artifact_id"])
    assert artifact is not None
    assert artifact["project_id"] == sample_product.product_id
    assert artifact["raw_output"] == '{"partial": true}'


def test_compile_persists_normalizer_failure_artifact(
    session: Session,
    sample_product: Product,
    sample_spec_content: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify compile persists normalizer failure artifact."""
    del session
    reg_result = register_spec_version(
        {"product_id": sample_product.product_id, "content": sample_spec_content},
        tool_context=None,
    )
    spec_version_id = reg_result["spec_version_id"]
    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"},
        tool_context=None,
    )

    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        failure_artifacts, "FAILURES_DIR", tmp_path / "logs" / "failures"
    )
    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        lambda **_: "{}",
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": spec_version_id},
        tool_context=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    artifact = failure_artifacts.read_failure_artifact(result["failure_artifact_id"])
    assert artifact is not None
    assert artifact["project_id"] == sample_product.product_id
    assert artifact["raw_output"] == "{}"
    assert artifact["validation_errors"]
