"""Tests for the typed compiled-authority assumption contract."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from utils.agileforge_spec_profile import TechnicalSpecArtifact
from utils.spec_authority_assumptions import (
    AUTHORITY_ASSUMPTION_ADAPTER,
    AcceptedNormativeCountAssumptionClaim,
    AcceptedNormativeSetAssumptionClaim,
    FreeTextAssumption,
    GroundingFailure,
    ItemStatusAssumptionClaim,
    StructuredSpecClaimProvenance,
    canonical_assumption_key,
    ground_assumption,
    is_structured_assumption,
    render_assumption_text,
)

EXPECTED_ACCEPTED_NORMATIVE_COUNT = 2


@pytest.fixture
def structured_spec() -> TechnicalSpecArtifact:
    """Return a spec with two accepted normative items."""
    return TechnicalSpecArtifact.model_validate(
        {
            "artifact_id": "SPEC.authority-review",
            "title": "Authority review",
            "status": "accepted",
            "version": "1.0",
            "created_at": "2026-07-27",
            "updated_at": "2026-07-27",
            "summary": "Exercise grounded authority assumptions.",
            "problem_statement": "Claims must be verifiable.",
            "items": [
                {
                    "id": "REQ.alpha",
                    "type": "REQ",
                    "status": "accepted",
                    "level": "MUST",
                    "title": "Alpha",
                    "statement": "Provide alpha.",
                    "verification": "unit-test",
                    "acceptance": ["Alpha is available."],
                },
                {
                    "id": "CONSTRAINT.beta",
                    "type": "CONSTRAINT",
                    "status": "accepted",
                    "level": "MUST_NOT",
                    "title": "Beta",
                    "statement": "Do not violate beta.",
                    "verification": "inspection",
                    "acceptance": ["Beta is respected."],
                },
                {
                    "id": "GOAL.gamma",
                    "type": "GOAL",
                    "status": "draft",
                    "title": "Gamma",
                    "statement": "Describe gamma.",
                },
            ],
        }
    )


@pytest.mark.parametrize(
"payload",
[
    {"kind": "free_text", "text": "External provider is available."},
    {
        "kind": "item_status",
        "item_id": "REQ.alpha",
        "status": "accepted",
        "provenance": {
            "source": "structured_spec",
            "artifact_id": "SPEC.authority-review",
            "source_item_ids": ["REQ.alpha"],
        },
    },
    {
        "kind": "accepted_normative_count",
        "count": 2,
        "provenance": {
            "source": "structured_spec",
            "artifact_id": "SPEC.authority-review",
            "source_item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
        },
    },
    {
        "kind": "accepted_normative_set",
        "item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
        "provenance": {
            "source": "structured_spec",
            "artifact_id": "SPEC.authority-review",
            "source_item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
        },
    },
],
)
def test_assumption_adapter_parses_each_variant(payload: dict[str, Any]) -> None:
    """Each documented kind parses through the discriminated adapter."""
    assumption = AUTHORITY_ASSUMPTION_ADAPTER.validate_python(payload)

    assert assumption.kind == payload["kind"]


@pytest.mark.parametrize(
"payload",
[
    {"text": "Missing discriminator."},
    {"kind": "unknown", "text": "Unknown discriminator."},
    {"kind": "item_status", "item_id": "REQ.alpha", "status": "accepted"},
    {"kind": "free_text", "text": "valid", "unexpected": True},
    "plain string assumptions are not part of v3",
],
)
def test_assumption_adapter_rejects_invalid_contract_shapes(payload: object) -> None:
    """The strict union rejects unknown, incomplete, and legacy values."""
    with pytest.raises(ValidationError):
        AUTHORITY_ASSUMPTION_ADAPTER.validate_python(payload)


@pytest.mark.parametrize(
"text",
[
    "Only REQ.alpha was accepted.",
    "REQ.alpha was the only accepted item.",
    "One item was accepted: REQ.alpha.",
    "Accepted items: REQ.alpha.",
    "Only CONSTRAINT.beta was accepted.",
    "ＲＥＱ.alpha is discussed; DRAFT assumptions remain open.",  # noqa: RUF001
],
)
def test_free_text_rejects_reserved_claim_cues(text: str) -> None:
    """Finite lexical claim cues require a typed claim form."""
    with pytest.raises(ValidationError) as exc_info:
        FreeTextAssumption(kind="free_text", text=text)

    assert any(
        error["type"] == "assumption_claim_requires_typed_form"
        for error in exc_info.value.errors()
    )


@pytest.mark.parametrize(
"text",
[
    "REQ.alpha depends on an external identity provider.",
    "Draft audit evidence is stored with each decision.",
]
)
def test_free_text_keeps_non_claim_assumptions(text: str) -> None:
    """Text outside the finite cue predicate remains available."""
    assert FreeTextAssumption(kind="free_text", text=text).text == text


@pytest.mark.parametrize("text", ["", "   "])
def test_free_text_rejects_empty_text(text: str) -> None:
    """Free text must remain meaningful after trimming."""
    with pytest.raises(ValidationError):
        FreeTextAssumption(kind="free_text", text=text)


def test_provenance_rejects_duplicates_before_sorting() -> None:
    """Duplicate evidence is invalid rather than repaired by normalization."""
    with pytest.raises(ValidationError, match="source_item_ids must be unique"):
        StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id="SPEC.authority-review",
            source_item_ids=["REQ.alpha", "REQ.alpha"],
        )


def test_claim_set_rejects_duplicates_before_sorting() -> None:
    """Duplicate claimed items are invalid rather than silently de-duplicated."""
    with pytest.raises(ValidationError, match="item_ids must be unique"):
        AcceptedNormativeSetAssumptionClaim(
            kind="accepted_normative_set",
            item_ids=["REQ.alpha", "REQ.alpha"],
            provenance=StructuredSpecClaimProvenance(
                source="structured_spec",
                artifact_id="SPEC.authority-review",
                source_item_ids=["REQ.alpha"],
            ),
        )


def test_unique_claim_ids_are_sorted_canonically() -> None:
    """Unique list identities are stored in lexical order."""
    claim = AcceptedNormativeSetAssumptionClaim(
        kind="accepted_normative_set",
        item_ids=["REQ.alpha", "CONSTRAINT.beta"],
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id="SPEC.authority-review",
            source_item_ids=["REQ.alpha", "CONSTRAINT.beta"],
        ),
    )

    assert claim.item_ids == ["CONSTRAINT.beta", "REQ.alpha"]
    assert claim.provenance.source_item_ids == ["CONSTRAINT.beta", "REQ.alpha"]


def test_discriminator_does_not_depend_on_union_trial_order() -> None:
    """A kind selects its model independently from union declaration order."""
    assumption = AUTHORITY_ASSUMPTION_ADAPTER.validate_python(
        {"kind": "free_text", "text": "An external provider is available."}
    )

    assert isinstance(assumption, FreeTextAssumption)


def test_canonical_free_text_identity_normalizes_unicode_and_case() -> None:
    """Equivalent free text has one stable identity key."""
    first = FreeTextAssumption(kind="free_text", text="  Café Provider  ")
    second = FreeTextAssumption(kind="free_text", text="cafe\u0301 provider")

    assert canonical_assumption_key(first) == canonical_assumption_key(second)


@pytest.mark.parametrize(
"assumption",
[
    FreeTextAssumption(kind="free_text", text="Provider is available."),
    ItemStatusAssumptionClaim(
        kind="item_status",
        item_id="REQ.alpha",
        status="accepted",
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id="SPEC.authority-review",
            source_item_ids=["REQ.alpha"],
        ),
    ),
    AcceptedNormativeCountAssumptionClaim(
        kind="accepted_normative_count",
        count=2,
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id="SPEC.authority-review",
            source_item_ids=["REQ.alpha", "CONSTRAINT.beta"],
        ),
    ),
    AcceptedNormativeSetAssumptionClaim(
        kind="accepted_normative_set",
        item_ids=["REQ.alpha", "CONSTRAINT.beta"],
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id="SPEC.authority-review",
            source_item_ids=["REQ.alpha", "CONSTRAINT.beta"],
        ),
    ),
],
)
def test_canonical_keys_are_separator_stable_json(assumption: object) -> None:
    """Every variant uses one compact sorted JSON identity encoding."""
    assert canonical_assumption_key(assumption).replace(":", ":") == json.dumps(
        json.loads(canonical_assumption_key(assumption)),
        sort_keys=True,
        separators=(",", ":"),
    )


def test_canonical_keys_distinguish_kinds_and_values() -> None:
    """Distinct semantic claims cannot share an identity key."""
    free_text = FreeTextAssumption(kind="free_text", text="Provider is available.")
    other_text = FreeTextAssumption(kind="free_text", text="Provider is unavailable.")
    count = AcceptedNormativeCountAssumptionClaim(
        kind="accepted_normative_count",
        count=2,
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id="SPEC.authority-review",
            source_item_ids=["CONSTRAINT.beta", "REQ.alpha"],
        ),
    )

    assert canonical_assumption_key(free_text) != canonical_assumption_key(other_text)
    assert canonical_assumption_key(free_text) != canonical_assumption_key(count)


@pytest.mark.parametrize(
    ("assumption", "expected"),
[
    (
        FreeTextAssumption(kind="free_text", text="Provider is available."),
        "Provider is available.",
    ),
    (
        ItemStatusAssumptionClaim(
            kind="item_status",
            item_id="REQ.alpha",
            status="accepted",
            provenance=StructuredSpecClaimProvenance(
                source="structured_spec",
                artifact_id="SPEC.authority-review",
                source_item_ids=["REQ.alpha"],
            ),
        ),
        "REQ.alpha status is accepted",
    ),
    (
        AcceptedNormativeCountAssumptionClaim(
            kind="accepted_normative_count",
            count=2,
            provenance=StructuredSpecClaimProvenance(
                source="structured_spec",
                artifact_id="SPEC.authority-review",
                source_item_ids=["CONSTRAINT.beta", "REQ.alpha"],
            ),
        ),
        "2 accepted normative items",
    ),
    (
        AcceptedNormativeSetAssumptionClaim(
            kind="accepted_normative_set",
            item_ids=["CONSTRAINT.beta", "REQ.alpha"],
            provenance=StructuredSpecClaimProvenance(
                source="structured_spec",
                artifact_id="SPEC.authority-review",
                source_item_ids=["CONSTRAINT.beta", "REQ.alpha"],
            ),
        ),
        "accepted normative items: CONSTRAINT.beta, REQ.alpha",
    ),
],
)
def test_render_assumption_text_is_readable(assumption: object, expected: str) -> None:
    """Every variant has stable review-friendly text."""
    assert render_assumption_text(assumption) == expected


def test_grounding_accepts_true_item_status(
    structured_spec: TechnicalSpecArtifact,
) -> None:
    """A true status claim with exact evidence grounds successfully."""
    claim = ItemStatusAssumptionClaim(
        kind="item_status",
        item_id="GOAL.gamma",
        status="draft",
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id=structured_spec.artifact_id,
            source_item_ids=["GOAL.gamma"],
        ),
    )

    assert ground_assumption(claim, structured_spec) is claim


def test_grounding_rejects_false_item_status(
    structured_spec: TechnicalSpecArtifact,
) -> None:
    """A wrong status receives a claim-mismatch failure."""
    claim = ItemStatusAssumptionClaim(
        kind="item_status",
        item_id="GOAL.gamma",
        status="accepted",
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id=structured_spec.artifact_id,
            source_item_ids=["GOAL.gamma"],
        ),
    )

    result = ground_assumption(claim, structured_spec)

    assert isinstance(result, GroundingFailure)
    assert result.reason == "ASSUMPTION_CLAIM_MISMATCH"
    assert result.claimed_value == "accepted"
    assert result.actual_value == "draft"


@pytest.mark.parametrize(
    ("count", "grounds"),
    [(EXPECTED_ACCEPTED_NORMATIVE_COUNT, True), (1, False)],
)
def test_grounding_checks_accepted_normative_count(
    structured_spec: TechnicalSpecArtifact, count: int, grounds: bool
) -> None:
    """Count claims cover the complete accepted normative item set."""
    claim = AcceptedNormativeCountAssumptionClaim(
        kind="accepted_normative_count",
        count=count,
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id=structured_spec.artifact_id,
            source_item_ids=["CONSTRAINT.beta", "REQ.alpha"],
        ),
    )

    result = ground_assumption(claim, structured_spec)

    assert (result is claim) is grounds
    if not grounds:
        assert isinstance(result, GroundingFailure)
        assert result.reason == "ASSUMPTION_CLAIM_MISMATCH"
        assert result.claimed_value == count
        assert result.actual_value == EXPECTED_ACCEPTED_NORMATIVE_COUNT


@pytest.mark.parametrize(
    ("item_ids", "grounds"),
[
    (["CONSTRAINT.beta", "REQ.alpha"], True),
    (["REQ.alpha"], False),
],
)
def test_grounding_checks_accepted_normative_set(
    structured_spec: TechnicalSpecArtifact, item_ids: list[str], grounds: bool
) -> None:
    """Set claims must state the complete accepted normative item set."""
    claim = AcceptedNormativeSetAssumptionClaim(
        kind="accepted_normative_set",
        item_ids=item_ids,
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id=structured_spec.artifact_id,
            source_item_ids=["CONSTRAINT.beta", "REQ.alpha"],
        ),
    )

    result = ground_assumption(claim, structured_spec)

    assert (result is claim) is grounds
    if not grounds:
        assert isinstance(result, GroundingFailure)
        assert result.reason == "ASSUMPTION_CLAIM_MISMATCH"
        assert result.claimed_value == ["REQ.alpha"]
        assert result.actual_value == ["CONSTRAINT.beta", "REQ.alpha"]


@pytest.mark.parametrize(
    ("artifact_id", "source_item_ids", "reason"),
[
    ("SPEC.other", ["REQ.alpha"], "ASSUMPTION_CLAIM_SOURCE_MISMATCH"),
    ("SPEC.authority-review", ["CONSTRAINT.beta"], "ASSUMPTION_CLAIM_SOURCE_MISMATCH"),
],
)
def test_grounding_rejects_wrong_or_incomplete_provenance(
    structured_spec: TechnicalSpecArtifact,
    artifact_id: str,
    source_item_ids: list[str],
    reason: str,
) -> None:
    """Claims never gain missing or invented evidence during grounding."""
    claim = ItemStatusAssumptionClaim(
        kind="item_status",
        item_id="REQ.alpha",
        status="accepted",
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id=artifact_id,
            source_item_ids=source_item_ids,
        ),
    )

    result = ground_assumption(claim, structured_spec)

    assert isinstance(result, GroundingFailure)
    assert result.reason == reason


def test_grounding_rejects_missing_item(structured_spec: TechnicalSpecArtifact) -> None:
    """A claim referencing an absent structured item cannot ground."""
    claim = ItemStatusAssumptionClaim(
        kind="item_status",
        item_id="REQ.missing",
        status="accepted",
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id=structured_spec.artifact_id,
            source_item_ids=["REQ.missing"],
        ),
    )

    result = ground_assumption(claim, structured_spec)

    assert isinstance(result, GroundingFailure)
    assert result.reason == "ASSUMPTION_CLAIM_SOURCE_MISMATCH"
    assert result.actual_value is None


def test_free_text_needs_no_structured_grounding(
    structured_spec: TechnicalSpecArtifact,
) -> None:
    """Ordinary assumptions pass through the grounding service unchanged."""
    assumption = FreeTextAssumption(kind="free_text", text="Provider is available.")

    assert ground_assumption(assumption, structured_spec) is assumption
    assert not is_structured_assumption(assumption)


def test_structured_claim_is_identified() -> None:
    """Structured claim detection separates grounding-required variants."""
    claim = AcceptedNormativeCountAssumptionClaim(
        kind="accepted_normative_count",
        count=0,
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id="SPEC.authority-review",
            source_item_ids=[],
        ),
    )

    assert is_structured_assumption(claim)
