"""Tests for implicit approval spec update tool."""

import json
import time
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from agile_sqlmodel import CompiledSpecAuthority, Product, SpecRegistry
from tools import spec_tools
from tools.spec_tools import update_spec_and_compile_authority
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)


@pytest.fixture
def sample_product(session: Session, engine: Engine) -> Product:
    """Create a product without spec."""
    spec_tools.engine = engine

    product = Product(
        name="Implicit Spec Product",
        description="Product for implicit spec updates",
        vision="Keep updates explicit",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _build_raw_compiler_output(
    excerpt: str,
    field_name: str,
    *,
    location: str | None = None,
) -> str:
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
                location=location,
            )
        ],
        compiler_version="0.0.0",
        prompt_hash="0" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _structured_spec_content(name: str) -> str:
    """Return structured spec JSON accepted by the authority compiler path."""
    return json.dumps(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": f"SPEC.update-{name.lower().replace(' ', '-')}",
            "title": f"{name} Spec",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-05-20",
            "updated_at": "2026-05-20",
            "summary": f"Exercise update flow for {name}.",
            "problem_statement": "Update flow needs structured specs.",
            "items": [
                {
                    "id": "REQ.update-behavior",
                    "type": "REQ",
                    "status": "accepted",
                    "title": "Update behavior",
                    "statement": f"The system must compile {name}.",
                    "level": "MUST",
                    "verification": "inspection",
                    "acceptance": [f"The system compiles {name}."],
                }
            ],
        },
        sort_keys=True,
    )


@pytest.fixture
def compiler_stub(monkeypatch: pytest.MonkeyPatch) -> object:
    """Return compiler stub."""

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        payload = json.loads(spec_content)
        item = payload["items"][0]
        return _build_raw_compiler_output(
            excerpt=item["statement"],
            field_name="user_id",
            location=f"{item['id']}.statement",
        )

    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )
    return fake_compiler


def test_creates_new_version_on_content_change(
    session: Session, sample_product: Product, compiler_stub: object
) -> None:
    """Tool should create approved spec and compiled authority."""
    del compiler_stub
    result = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "spec_content": _structured_spec_content("Spec A"),
        },
        tool_context=None,
    )

    assert result["success"] is True
    spec_version_id = result["spec_version_id"]

    spec_row = session.get(SpecRegistry, spec_version_id)
    assert spec_row is not None
    assert spec_row.status == "approved"
    assert spec_row.approved_at is not None
    assert spec_row.approved_by == "implicit"

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    assert authority is not None


def test_noop_on_unchanged_content(
    session: Session, sample_product: Product, compiler_stub: object
) -> None:
    """Second call with unchanged content should reuse version and authority."""
    del compiler_stub
    first = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "spec_content": _structured_spec_content("Spec A"),
        },
        tool_context=None,
    )

    second = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "spec_content": _structured_spec_content("Spec A"),
        },
        tool_context=None,
    )

    assert second["spec_version_id"] == first["spec_version_id"]
    assert second["cache_hit"] is True

    versions = session.exec(
        select(SpecRegistry).where(SpecRegistry.product_id == sample_product.product_id)
    ).all()
    assert len(versions) == 1


def test_content_ref_path(
    session: Session, sample_product: Product, tmp_path: Path, compiler_stub: object
) -> None:
    """Tool should load content from content_ref path."""
    del compiler_stub
    spec_content = _structured_spec_content("Spec from file")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec_content, encoding="utf-8")

    result = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "content_ref": str(spec_path),
        },
        tool_context=None,
    )

    assert result["success"] is True
    spec_row = session.get(SpecRegistry, result["spec_version_id"])
    assert spec_row is not None
    assert json.loads(spec_row.content)["title"] == "Spec from file Spec"
    assert spec_row.content_ref == str(spec_path)


def test_recompile_behavior(
    session: Session, sample_product: Product, compiler_stub: object
) -> None:
    """Recompile should update compiled_at when requested."""
    del compiler_stub
    first = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "spec_content": _structured_spec_content("Spec A"),
        },
        tool_context=None,
    )

    authority_before = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == first["spec_version_id"]
        )
    ).first()
    assert authority_before is not None
    compiled_at_before = authority_before.compiled_at

    time.sleep(0.01)

    second = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "spec_content": _structured_spec_content("Spec A"),
            "recompile": True,
        },
        tool_context=None,
    )

    assert second["cache_hit"] is False

    session.expire_all()

    authority_after = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == first["spec_version_id"]
        )
    ).first()
    assert authority_after is not None
    assert authority_after.compiled_at != compiled_at_before


def test_input_validation() -> None:
    """Providing both or neither content inputs should raise ValueError."""
    with pytest.raises(ValueError):  # noqa: PT011
        update_spec_and_compile_authority(
            {"product_id": 1, "spec_content": "A", "content_ref": "x"},
            tool_context=None,
        )

    with pytest.raises(ValueError):  # noqa: PT011
        update_spec_and_compile_authority(
            {"product_id": 1},
            tool_context=None,
        )


def test_compiler_hashing_failure_is_rejected(
    session: Session, sample_product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compiler hashing-related failures should be rejected at the boundary."""
    del session
    failure_payload = {
        "error": "SPEC_COMPILATION_FAILED",
        "reason": "Unable to deterministically compute SHA-256 prompt_hash",
        "blocking_gaps": ["Cannot compute SHA-256"],
    }
    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        lambda **_: json.dumps(failure_payload),
    )

    result = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "spec_content": _structured_spec_content("Spec A"),
        },
        tool_context=None,
    )

    assert result["success"] is False
    assert result["error"] == "SPEC_COMPILATION_FAILED"
