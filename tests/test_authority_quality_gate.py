"""Tests for project-agnostic compiled authority quality gate."""

from __future__ import annotations

from pathlib import Path

from services.specs.authority_quality import apply_authority_quality_gate
from utils.spec_schemas import (
    DataContractParams,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    StateTransitionParams,
)

EXPECTED_SOURCE_EVIDENCE_COUNT: int = 2
EXPECTED_NEAR_DUPLICATE_INVARIANT_COUNT: int = 2
EXPECTED_OVER_SPLIT_INVARIANT_COUNT: int = 5


def _success(
    *,
    invariants: list[Invariant],
    assumptions: list[str] | None = None,
    source_map: list[SourceMapEntry] | None = None,
) -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Project"],
        domain=None,
        invariants=invariants,
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=assumptions or [],
        source_map=source_map or [],
        compiler_version="2.0.0",
        prompt_hash="a" * 64,
    )


def _required(
    item_id: str,
    *,
    source_item_id: str = "REQ.alpha",
    source_level: str = "MUST",
    field_name: str = "email",
) -> Invariant:
    return Invariant(
        id=item_id,
        type=InvariantType.REQUIRED_FIELD,
        source_item_id=source_item_id,
        source_level=source_level,
        parameters=RequiredFieldParams(field_name=field_name),
    )


def test_quality_gate_merges_exact_duplicate_invariants_and_preserves_sources() -> None:
    """Exact same invariant semantics and provenance merge safely."""
    first = _required("INV-1111111111111111")
    duplicate = _required("INV-2222222222222222")
    success = _success(
        invariants=[first, duplicate],
        source_map=[
            SourceMapEntry(
                invariant_id=first.id,
                excerpt="Alpha requires email.",
                location="REQ.alpha.statement",
            ),
            SourceMapEntry(
                invariant_id=duplicate.id,
                excerpt="Email is required.",
                location="REQ.alpha.acceptance[0]",
            ),
        ],
    )

    gated = apply_authority_quality_gate(success)

    assert [invariant.id for invariant in gated.invariants] == [first.id]
    assert [entry.invariant_id for entry in gated.source_map] == [first.id, first.id]
    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.merged_invariant_count == 1
    assert gated.authority_quality.merged_items[0].removed_ids == [duplicate.id]
    assert (
        gated.authority_quality.merged_items[0].source_evidence_count
        == EXPECTED_SOURCE_EVIDENCE_COUNT
    )


def test_quality_gate_groups_same_shape_different_source_without_merging() -> None:
    """Same-shaped rules from different source items remain reviewable."""
    alpha = _required("INV-1111111111111111", source_item_id="REQ.alpha")
    beta = _required("INV-2222222222222222", source_item_id="REQ.beta")
    gated = apply_authority_quality_gate(_success(invariants=[alpha, beta]))

    assert [invariant.id for invariant in gated.invariants] == [alpha.id, beta.id]
    assert gated.authority_quality is not None
    groups = gated.authority_quality.review_groups
    assert any(group.group_type == "related_source_variants" for group in groups)


def test_quality_gate_groups_near_duplicate_invariants_without_merging() -> None:
    """High-overlap invariant text becomes a review group, not a merge."""
    first = Invariant(
        id="INV-1111111111111111",
        type=InvariantType.DATA_CONTRACT,
        source_item_id="REQ.alpha",
        source_level="MUST",
        parameters=DataContractParams(
            subject="profile",
            fields=["email", "name"],
            rule="profile record stores email and display name",
        ),
    )
    second = Invariant(
        id="INV-2222222222222222",
        type=InvariantType.DATA_CONTRACT,
        source_item_id="REQ.alpha",
        source_level="MUST",
        parameters=DataContractParams(
            subject="profile",
            fields=["email", "display_name"],
            rule="profile record persists email and display name",
        ),
    )

    gated = apply_authority_quality_gate(_success(invariants=[first, second]))

    assert len(gated.invariants) == EXPECTED_NEAR_DUPLICATE_INVARIANT_COUNT
    assert gated.authority_quality is not None
    assert any(
        group.group_type == "near_duplicate_invariants"
        for group in gated.authority_quality.review_groups
    )


def test_quality_gate_groups_over_split_source_item() -> None:
    """Many invariants from one source item produce an over-split group."""
    invariants = [
        Invariant(
            id=f"INV-{index:016x}",
            type=InvariantType.STATE_TRANSITION,
            source_item_id="REQ.alpha",
            source_level="MUST",
            parameters=StateTransitionParams(
                state=f"step_{index}",
                trigger="input accepted",
                outcome=f"records step {index}",
            ),
        )
        for index in range(1, 6)
    ]

    gated = apply_authority_quality_gate(_success(invariants=invariants))

    assert len(gated.invariants) == EXPECTED_OVER_SPLIT_INVARIANT_COUNT
    assert gated.authority_quality is not None
    assert any(
        group.group_type == "over_split_invariants"
        for group in gated.authority_quality.review_groups
    )


def test_quality_gate_merges_exact_duplicate_assumptions_and_groups_noisy() -> None:
    """Assumption cleanup merges exact duplicates and groups high-overlap noise."""
    gated = apply_authority_quality_gate(
        _success(
            invariants=[],
            assumptions=[
                "Python runtime should be confirmed before implementation.",
                "python runtime should be confirmed before implementation",
                "Python runtime should be confirmed before implementation step.",
            ],
        )
    )

    assert gated.assumptions == [
        "Python runtime should be confirmed before implementation.",
        "Python runtime should be confirmed before implementation step.",
    ]
    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.merged_assumption_count == 1
    assert any(
        group.group_type == "noisy_assumptions"
        for group in gated.authority_quality.review_groups
    )


def test_quality_gate_keeps_non_identical_noisy_assumptions_unmerged() -> None:
    """High-overlap but non-identical assumptions are review-only."""
    gated = apply_authority_quality_gate(
        _success(
            invariants=[],
            assumptions=[
                "API is stable.",
                "API stable",
            ],
        )
    )

    assert gated.assumptions == [
        "API is stable.",
        "API stable",
    ]
    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.merged_assumption_count == 0
    assert any(
        group.group_type == "noisy_assumptions"
        for group in gated.authority_quality.review_groups
    )


def test_authority_quality_gate_has_no_project_specific_terms() -> None:
    """Gate implementation must stay project-agnostic."""
    implementation = Path("services/specs/authority_quality.py").read_text()
    forbidden_terms = [
        "ASA",
        "Deep Process",
        "REQ.project-scaffold",
        "DDPG",
        "pyrometer",
        "TemperatureTargets",
        "stainless",
        "annealing",
        "pickling",
    ]
    offenders = [term for term in forbidden_terms if term in implementation]
    assert offenders == []
