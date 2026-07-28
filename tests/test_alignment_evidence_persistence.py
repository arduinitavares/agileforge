# tests/test_alignment_evidence_persistence.py
"""Tests for alignment findings persisted in ValidationEvidence."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecAuthorityAcceptance,
    SpecRegistry,
    UserStory,
)
from models.core import Epic, Feature, Theme
from services.specs.authority_selection import pending_authority_fingerprint
from tools import spec_tools
from tools.spec_tools import (
    approve_spec_version,
    register_spec_version,
    validate_story_with_spec_authority,
)
from utils.spec_schemas import (
    ForbiddenCapabilityParams,
    Invariant,
    InvariantType,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)


@pytest.fixture
def product_with_spec(session: Session, engine: Engine) -> tuple[Product, int]:
    """Create product with a pre-compiled spec authority."""
    spec_tools.engine = engine

    product = Product(name="Alignment Product", vision="Test")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = _require_id(product.product_id, "product.product_id")

    spec_content = "# Spec\n\n## Invariants\n- Stories MUST NOT include web features."
    spec_hash = hashlib.sha256(spec_content.encode()).hexdigest()

    # Create spec registry entry
    spec_version = SpecRegistry(
        product_id=product_id,
        content=spec_content,
        spec_hash=spec_hash,
        status="approved",
        approved_by="tester",
    )
    session.add(spec_version)
    session.commit()
    session.refresh(spec_version)
    spec_version_id = _require_id(
        spec_version.spec_version_id,
        "spec_version.spec_version_id",
    )

    # Create pre-compiled authority with explicit FORBIDDEN_CAPABILITY
    invariants = [
        Invariant(
            id="INV-0000000000000001",
            type=InvariantType.FORBIDDEN_CAPABILITY,
            parameters=ForbiddenCapabilityParams(capability="web"),
        ),
    ]
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["core"],
        domain=None,
        invariants=invariants,
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-0000000000000001",
                excerpt="Stories MUST NOT include web features.",
                location=None,
            ),
        ],
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
    )
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
        scope_themes='["core"]',
        invariants='["FORBIDDEN_CAPABILITY:web"]',
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=success
        ).model_dump_json(),
        compiled_at=datetime.now(UTC),
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="tester",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=spec_version.spec_hash,
            pending_authority_id=authority.authority_id,
            authority_fingerprint=pending_authority_fingerprint(authority),
        )
    )
    session.commit()

    return product, spec_version_id


def _require_id(value: int | None, label: str) -> int:
    """Return a non-null integer ID or raise a clear assertion."""
    assert value is not None, f"{label} unexpectedly None"
    return value


def _load_validation_evidence(story: UserStory) -> dict[str, Any]:
    """Load persisted validation evidence with a non-null assertion."""
    evidence_json = story.validation_evidence
    assert evidence_json is not None, "validation_evidence unexpectedly None"
    return json.loads(evidence_json)


def _create_story(session: Session, product_id: int, title: str) -> UserStory:
    theme = Theme(product_id=product_id, title="Theme", description="")
    session.add(theme)
    session.commit()
    session.refresh(theme)

    epic = Epic(
        theme_id=_require_id(theme.theme_id, "theme.theme_id"),
        title="Epic",
        summary="",
    )
    session.add(epic)
    session.commit()
    session.refresh(epic)

    feature = Feature(
        epic_id=_require_id(epic.epic_id, "epic.epic_id"),
        title="Feature",
        description="",
    )
    session.add(feature)
    session.commit()
    session.refresh(feature)

    story = UserStory(
        product_id=product_id,
        feature_id=_require_id(feature.feature_id, "feature.feature_id"),
        title=title,
        story_description="As a user, I want a feature.",
        acceptance_criteria="- AC",
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    return story


def test_alignment_failure_persisted(
    engine: Engine,
    session: Session,
    product_with_spec: tuple[Product, int],
) -> None:
    """Alignment rejection persists alignment_failures in evidence."""
    spec_tools.engine: Engine = engine
    product, spec_version_id = product_with_spec

    story: UserStory = _create_story(
        session,
        _require_id(product.product_id, "product.product_id"),
        title="Web dashboard",
    )
    result: dict[str, Any] = validate_story_with_spec_authority(
        {"story_id": story.story_id, "spec_version_id": spec_version_id},
        tool_context=None,
    )

    assert result["success"] is True
    assert result["passed"] is False

    session.refresh(story)
    evidence: dict[str, Any] = _load_validation_evidence(story)
    assert evidence["alignment_failures"]
    assert any(
        f["code"] == "FORBIDDEN_CAPABILITY" for f in evidence["alignment_failures"]
    )


def test_alignment_warning_persisted(engine: Engine, session: Session) -> None:
    """Alignment warning persists alignment_warnings in evidence."""
    spec_tools.engine = engine

    product = Product(name="Warn Product", vision="Test")
    session.add(product)
    session.commit()
    session.refresh(product)

    # Create an approved spec + precompiled authority with zero invariants.
    reg: dict[str, Any] = register_spec_version(
        {
            "product_id": product.product_id,
            "content": "# Spec\n\n## Notes\n- No requirements here",
        },
        tool_context=None,
    )
    spec_version_id = reg["spec_version_id"]
    approve_spec_version(
        {"spec_version_id": spec_version_id, "approved_by": "tester"}, tool_context=None
    )

    zero_invariant_artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["notes-only"],
        domain=None,
        invariants=[],
        eligible_feature_rules=[],
        gaps=["No invariants extracted from spec"],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
    )
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
        scope_themes='["notes-only"]',
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps='["No invariants extracted from spec"]',
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=zero_invariant_artifact
        ).model_dump_json(),
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    accepted_spec = session.get(SpecRegistry, spec_version_id)
    assert accepted_spec is not None
    session.add(
        SpecAuthorityAcceptance(
            product_id=_require_id(product.product_id, "product.product_id"),
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="tester",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=accepted_spec.spec_hash,
            pending_authority_id=authority.authority_id,
            authority_fingerprint=pending_authority_fingerprint(authority),
        )
    )
    session.commit()

    story: UserStory = _create_story(
        session,
        _require_id(product.product_id, "product.product_id"),
        title="Normal story",
    )
    result: dict[str, Any] = validate_story_with_spec_authority(
        {"story_id": story.story_id, "spec_version_id": spec_version_id},
        tool_context=None,
    )

    assert result["success"] is True

    session.refresh(story)
    evidence: dict[str, Any] = _load_validation_evidence(story)
    assert evidence["alignment_warnings"]
    assert any(w["code"] == "NO_INVARIANTS" for w in evidence["alignment_warnings"])


def test_alignment_evidence_includes_spec_and_hash(
    engine: Engine,
    session: Session,
    product_with_spec: tuple[Product, int],
) -> None:
    """Evidence includes spec_version_id and input_hash without vision access."""
    spec_tools.engine = engine
    product, spec_version_id = product_with_spec

    story: UserStory = _create_story(
        session,
        _require_id(product.product_id, "product.product_id"),
        title="Web dashboard",
    )
    validate_story_with_spec_authority(
        {"story_id": story.story_id, "spec_version_id": spec_version_id},
        tool_context=None,
    )

    session.refresh(story)
    evidence: dict[str, Any] = _load_validation_evidence(story)
    assert evidence["spec_version_id"] == spec_version_id
    assert evidence["input_hash"]
