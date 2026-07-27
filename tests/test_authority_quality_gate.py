"""Tests for project-agnostic compiled authority quality gate."""

from __future__ import annotations

from pathlib import Path

from services.specs.authority_quality import apply_authority_quality_gate
from utils.spec_authority_assumptions import (
    AcceptedNormativeCountAssumptionClaim,
    AuthorityAssumption,
    FreeTextAssumption,
    ItemStatusAssumptionClaim,
    StructuredSpecClaimProvenance,
)
from utils.spec_schemas import (
    AuthorityQualityInvalidatedItem,
    AuthorityQualityReport,
    AuthorityQualitySummary,
    DataContractParams,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthoritySourceLevel,
    StateTransitionParams,
)

EXPECTED_SOURCE_EVIDENCE_COUNT: int = 2
EXPECTED_NEAR_DUPLICATE_INVARIANT_COUNT: int = 2
EXPECTED_OVER_SPLIT_INVARIANT_COUNT: int = 5


def _success(
    *,
    invariants: list[Invariant],
    assumptions: list[AuthorityAssumption] | None = None,
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
    source_level: SpecAuthoritySourceLevel = "MUST",
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
                FreeTextAssumption(
                    kind="free_text",
                    text="Python runtime should be confirmed before implementation.",
                ),
                FreeTextAssumption(
                    kind="free_text",
                    text="python runtime should be confirmed before implementation.",
                ),
                FreeTextAssumption(
                    kind="free_text",
                    text=(
                        "Python runtime should be confirmed before "
                        "implementation step."
                    ),
                ),
            ],
        )
    )

    assert gated.assumptions == [
        FreeTextAssumption(
            kind="free_text",
            text="Python runtime should be confirmed before implementation.",
        ),
        FreeTextAssumption(
            kind="free_text",
            text="Python runtime should be confirmed before implementation step.",
        ),
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
                FreeTextAssumption(kind="free_text", text="API is stable."),
                FreeTextAssumption(kind="free_text", text="API stable"),
            ],
        )
    )

    assert gated.assumptions == [
        FreeTextAssumption(kind="free_text", text="API is stable."),
        FreeTextAssumption(kind="free_text", text="API stable"),
    ]
    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.merged_assumption_count == 0
    assert any(
        group.group_type == "noisy_assumptions"
        for group in gated.authority_quality.review_groups
    )


def test_quality_gate_keeps_different_structured_assumption_values() -> None:
    """Canonical identity does not collapse structured claims with different values."""
    provenance = StructuredSpecClaimProvenance(
        source="structured_spec",
        artifact_id="SPEC.test",
        source_item_ids=["REQ.alpha"],
    )
    gated = apply_authority_quality_gate(
        _success(
            invariants=[],
            assumptions=[
                AcceptedNormativeCountAssumptionClaim(
                    kind="accepted_normative_count",
                    count=1,
                    provenance=provenance,
                ),
                AcceptedNormativeCountAssumptionClaim(
                    kind="accepted_normative_count",
                    count=2,
                    provenance=provenance,
                ),
            ],
        )
    )

    assert [assumption.count for assumption in gated.assumptions] == [1, 2]
    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.merged_assumption_count == 0


def test_quality_gate_noisy_grouping_ignores_structured_assumptions() -> None:
    """Only free text participates in review-only noisy grouping."""
    provenance = StructuredSpecClaimProvenance(
        source="structured_spec",
        artifact_id="SPEC.test",
        source_item_ids=["REQ.alpha"],
    )
    gated = apply_authority_quality_gate(
        _success(
            invariants=[],
            assumptions=[
                ItemStatusAssumptionClaim(
                    kind="item_status",
                    item_id="REQ.alpha",
                    status="accepted",
                    provenance=provenance,
                ),
                AcceptedNormativeCountAssumptionClaim(
                    kind="accepted_normative_count",
                    count=1,
                    provenance=provenance,
                ),
            ],
        )
    )

    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.noisy_assumption_group_count == 0


def test_quality_gate_preserves_and_renumbers_scope_invalidations() -> None:
    """Quality rebuilds keep scope-extension invalidation history stable."""
    success = _success(invariants=[])
    success.authority_quality = AuthorityQualityReport(
        summary=AuthorityQualitySummary(
            original_invariant_count=0,
            final_invariant_count=0,
            merged_invariant_count=0,
            merged_assumption_count=0,
            review_group_count=0,
            near_duplicate_group_count=0,
            over_split_group_count=0,
            noisy_assumption_group_count=0,
        ),
        invalidated_items=[
            AuthorityQualityInvalidatedItem(
                invalidation_id="temporary",
                removed_id="ASM-1",
                assumption_kind="accepted_normative_count",
                reason="aggregate_claim_invalidated_by_scope_extension",
            )
        ],
    )

    gated = apply_authority_quality_gate(success)

    assert gated.authority_quality is not None
    assert [
        item.invalidation_id for item in gated.authority_quality.invalidated_items
    ] == ["AQ-INVALIDATE-001"]


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
