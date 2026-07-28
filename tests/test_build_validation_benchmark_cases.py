"""Tests for build validation benchmark cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest  # noqa: TC002

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecAuthorityAcceptance,
    SpecRegistry,
    UserStory,
)
from scripts import build_validation_benchmark_cases as builder
from services.specs.authority_selection import pending_authority_fingerprint
from tests.authority_assumption_fixtures import current_v3_compiled_authority_json
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from sqlmodel import Session


def _story(title: str, description: str, acceptance_criteria: str) -> UserStory:
    return UserStory(
        product_id=1,
        title=title,
        story_description=description,
        acceptance_criteria=acceptance_criteria,
    )


def test_compute_content_hash_changes_with_story_text() -> None:
    """Verify compute content hash changes with story text."""
    s1 = _story("A", "B", "C")
    s2 = _story("A", "B changed", "C")
    h1 = builder._compute_content_hash(s1)  # pylint: disable=protected-access
    h2 = builder._compute_content_hash(s2)  # pylint: disable=protected-access
    assert h1 != h2


def test_apply_no_evidence_labels_clears_labels() -> None:
    """Verify apply no evidence labels clears labels."""
    expected_pass, reasons = builder._apply_no_evidence_labels(  # pylint: disable=protected-access
        True,
        ["RULE_X"],
        "validation_evidence",
        no_evidence_labels=True,
    )
    assert expected_pass is None
    assert reasons == []


def test_apply_no_evidence_labels_keeps_non_evidence() -> None:
    """Verify apply no evidence labels keeps non evidence."""
    expected_pass, reasons = builder._apply_no_evidence_labels(  # pylint: disable=protected-access
        False,
        ["RULE_X"],
        "human_review",
        no_evidence_labels=True,
    )
    assert expected_pass is False
    assert reasons == ["RULE_X"]


def test_warn_when_all_cases_are_validation_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify warn when all cases are validation evidence."""
    rows = [
        {"label_source": "validation_evidence"},
        {"label_source": "validation_evidence"},
    ]
    builder._maybe_warn_evidence_only(rows)  # pylint: disable=protected-access
    captured = capsys.readouterr()
    assert "WARNING: All labels derive from validation_evidence" in captured.err


def test_strict_spec_resolution_requires_exact_accepted_valid_authority(
    session: Session,
) -> None:
    """Strict cases reject missing or malformed acceptance; permissive is explicit."""
    product = Product(name="Benchmark Builder Product")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="builder-spec",
        content="# Builder",
        status="approved",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    story = UserStory(
        product_id=product_id,
        accepted_spec_version_id=spec_version_id,
        title="Builder story",
    )
    session.add(story)
    malformed = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
        compiled_artifact_json="not-json",
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(malformed)
    session.flush()
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="builder-test",
            compiler_version=malformed.compiler_version,
            prompt_hash=malformed.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=malformed.authority_id,
            authority_fingerprint=pending_authority_fingerprint(malformed),
        )
    )
    session.add(
        CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="b" * 64,
            compiled_artifact_json=current_v3_compiled_authority_json(
                prompt_hash="b" * 64
            ),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
    )
    session.commit()

    assert builder._resolve_spec_version_id(
        session,
        story,
        require_compiled=False,
    ) == (spec_version_id, "accepted_spec_version_id")
    assert builder._resolve_spec_version_id(
        session,
        story,
        require_compiled=True,
    ) == (None, "accepted_spec_invalid")

    malformed.compiled_artifact_json = current_v3_compiled_authority_json(
        prompt_hash=malformed.prompt_hash
    )
    session.add(malformed)
    session.commit()

    assert builder._resolve_spec_version_id(
        session,
        story,
        require_compiled=True,
    ) == (None, "accepted_spec_invalid")

    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="builder-reaccept-test",
            compiler_version=malformed.compiler_version,
            prompt_hash=malformed.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=malformed.authority_id,
            authority_fingerprint=pending_authority_fingerprint(malformed),
        )
    )
    session.commit()

    assert builder._resolve_spec_version_id(
        session,
        story,
        require_compiled=True,
    ) == (spec_version_id, "accepted_spec_version_id")
