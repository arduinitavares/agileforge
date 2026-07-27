"""Tests for implicit approval spec update tool."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.specs.profile_content import normalize_spec_content_for_registry
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
    """Recompile should append a newer authority row when requested."""
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

    authorities = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == first["spec_version_id"]
        )
    ).all()
    authority_after = session.get(CompiledSpecAuthority, second["authority_id"])
    assert authority_after is not None
    assert len(authorities) == 2  # noqa: PLR2004
    assert authority_before.compiled_at == compiled_at_before
    assert authority_after.compiled_at != compiled_at_before


def test_recompile_returns_exact_candidate_without_transferring_acceptance(
    session: Session,
    sample_product: Product,
    compiler_stub: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced compile returns its exact pending row without accepting it."""
    del compiler_stub
    normalized = normalize_spec_content_for_registry(
        _structured_spec_content("Spec A")
    )
    spec = SpecRegistry(
        product_id=sample_product.product_id,
        spec_hash=normalized.spec_hash,
        content=normalized.content,
        status="approved",
        approved_at=datetime(2026, 7, 27, tzinfo=UTC),
        approved_by="reviewer",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    first = spec_tools.compile_spec_authority_for_version(
        {"spec_version_id": spec.spec_version_id},
        tool_context=None,
    )
    assert first["success"] is True
    first_authority_id = first["authority_id"]
    first_authority = session.get(CompiledSpecAuthority, first_authority_id)
    assert first_authority is not None
    session.add(
        SpecAuthorityAcceptance(
            product_id=sample_product.product_id,
            spec_version_id=spec.spec_version_id,
            status="accepted",
            policy="manual",
            decided_by="reviewer",
            decided_at=datetime(2026, 7, 27, tzinfo=UTC),
            compiler_version=first_authority.compiler_version,
            prompt_hash=first_authority.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=first_authority_id,
            terminal_decision_key=(
                f"{sample_product.product_id}:{spec.spec_version_id}:"
                f"{first_authority_id}"
            ),
        )
    )
    session.commit()

    original_compile = spec_tools.compile_spec_authority_for_version
    compiled_candidate_id: int | None = None

    def compile_then_append_newer_row(
        params: object,
        tool_context: object = None,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal compiled_candidate_id
        result = original_compile(params, tool_context=tool_context, **kwargs)
        candidate_id = result["authority_id"]
        assert isinstance(candidate_id, int)
        compiled_candidate_id = candidate_id
        candidate = session.get(CompiledSpecAuthority, candidate_id)
        assert candidate is not None
        session.add(
            CompiledSpecAuthority(
                spec_version_id=candidate.spec_version_id,
                compiler_version="unrelated-newer",
                prompt_hash="f" * 64,
                compiled_at=datetime.now(UTC),
                compiled_artifact_json=candidate.compiled_artifact_json,
                scope_themes=candidate.scope_themes,
                invariants=candidate.invariants,
                eligible_feature_ids=candidate.eligible_feature_ids,
                rejected_features=candidate.rejected_features,
                spec_gaps=candidate.spec_gaps,
            )
        )
        session.commit()
        return result

    monkeypatch.setattr(
        spec_tools,
        "compile_spec_authority_for_version",
        compile_then_append_newer_row,
    )

    result = update_spec_and_compile_authority(
        {
            "product_id": sample_product.product_id,
            "spec_content": _structured_spec_content("Spec A"),
            "recompile": True,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["authority_id"] == compiled_candidate_id
    assert result["accepted"] is False
    assert result["authority_status"] == "pending_acceptance"
    acceptances = session.exec(
        select(SpecAuthorityAcceptance).where(
            SpecAuthorityAcceptance.product_id == sample_product.product_id
        )
    ).all()
    assert len(acceptances) == 1
    assert acceptances[0].pending_authority_id == first_authority_id


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
