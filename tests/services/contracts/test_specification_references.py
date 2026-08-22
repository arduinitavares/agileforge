"""Tests for exact accepted-Specification item-reference validation."""
# ruff: noqa: D103

import json

import pytest
from pydantic import ValidationError

from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    SpecificationReferenceError,
    canonical_spec_item_ids,
    derived_referenced_spec_item_ids,
    has_qualifying_normative_evidence,
)
from services.contracts.sprint import SprintPlannerInput
from services.contracts.story import UserStoryWriterInput
from utils.agileforge_spec_profile_v2 import (
    RequirementLevel,
    SpecificationItem,
    SpecificationPayload,
    SpecItemType,
    VerificationMethod,
    canonical_spec_hash,
    canonical_spec_json,
)


def _payload() -> SpecificationPayload:
    return SpecificationPayload(
        artifact_id="SPEC.reference-contract",
        title="Reference contract",
        summary="Stable references",
        problem_statement="Planning needs exact Specification evidence.",
        items=(
            SpecificationItem(
                id="REQ.alpha",
                type=SpecItemType.REQ,
                title="Alpha",
                statement="The system must support alpha.",
                level=RequirementLevel.MUST,
                verification=VerificationMethod.UNIT_TEST,
                acceptance=("Alpha passes.",),
            ),
            SpecificationItem(
                id="EXAMPLE.sample",
                type=SpecItemType.EXAMPLE,
                title="Sample",
                statement="A helpful example.",
            ),
        ),
    )


def _reference() -> AcceptedSpecificationReference:
    payload = _payload()
    return AcceptedSpecificationReference(
        spec_version_id=7,
        spec_hash=canonical_spec_hash(payload),
        canonical_specification_json=canonical_spec_json(payload),
        payload=payload,
    )


def _evidence_item(
    *,
    item_type: SpecItemType,
    level: RequirementLevel,
) -> SpecificationItem:
    """Build one valid profile item for the qualifying-evidence matrix."""
    normative = item_type in {
        SpecItemType.REQ,
        SpecItemType.QUALITY,
        SpecItemType.CONSTRAINT,
        SpecItemType.INTERFACE,
        SpecItemType.DATA,
    }
    return SpecificationItem(
        id=f"{item_type.value}.evidence",
        type=item_type,
        title=f"{item_type.value} evidence",
        statement=f"{item_type.value} evidence is present.",
        level=level,
        verification=VerificationMethod.INSPECTION if normative else None,
        acceptance=("Evidence is present.",) if normative else (),
    )


def test_reference_boundary_rejects_mismatched_canonical_identity() -> None:
    payload = _payload()

    with pytest.raises(ValidationError, match="canonical Specification"):
        AcceptedSpecificationReference(
            spec_version_id=7,
            spec_hash=canonical_spec_hash(payload),
            canonical_specification_json="{}",
            payload=payload,
        )


def test_reference_sets_are_canonicalized_and_must_include_normative_evidence() -> None:
    assert canonical_spec_item_ids(_reference(), ["REQ.alpha", "EXAMPLE.sample"]) == (
        "EXAMPLE.sample",
        "REQ.alpha",
    )

    with pytest.raises(SpecificationReferenceError, match="qualifying normative"):
        canonical_spec_item_ids(_reference(), ["EXAMPLE.sample"])


def test_reference_sets_reject_duplicates_unknown_and_out_of_parent_scope() -> None:
    with pytest.raises(SpecificationReferenceError) as exc_info:
        canonical_spec_item_ids(
            _reference(),
            ["REQ.alpha", "REQ.alpha", "REQ.missing"],
            parent_spec_item_ids=("REQ.alpha",),
        )

    assert exc_info.value.errors == (
        "duplicate Specification item ID: REQ.alpha",
        "unknown Specification item ID: REQ.missing",
    )


def test_referenced_specification_ids_are_a_host_derived_sorted_union() -> None:
    assert derived_referenced_spec_item_ids(
        ("REQ.alpha",), ("EXAMPLE.sample", "REQ.alpha")
    ) == ("EXAMPLE.sample", "REQ.alpha")


def test_story_and_sprint_roots_reject_invalid_or_noncanonical_specification_json() -> (
    None
):
    reference = _reference()
    root_payload = {
        "accepted_specification_version_id": reference.spec_version_id,
        "accepted_specification_hash": reference.spec_hash,
        "accepted_specification_json": reference.canonical_specification_json,
    }
    story_payload = {
        **root_payload,
        "parent_backlog_item_id": "PBI-000001",
        "parent_backlog_spec_item_ids": ("REQ.alpha",),
    }
    sprint_payload = {
        **root_payload,
        "available_stories": (),
        "capacity_points": 1,
        "capacity_source": "user_override",
        "capacity_basis": "One point is available.",
    }

    assert UserStoryWriterInput.model_validate(story_payload)
    assert SprintPlannerInput.model_validate(sprint_payload)

    for invalid_json in (
        "{",
        json.dumps(reference.payload.model_dump(mode="json"), ensure_ascii=False),
    ):
        with pytest.raises(ValidationError, match="accepted Specification"):
            UserStoryWriterInput.model_validate(
                {**story_payload, "accepted_specification_json": invalid_json}
            )
        with pytest.raises(ValidationError, match="accepted Specification"):
            SprintPlannerInput.model_validate(
                {**sprint_payload, "accepted_specification_json": invalid_json}
            )

    wrong_hash = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="accepted Specification hash"):
        UserStoryWriterInput.model_validate(
            {**story_payload, "accepted_specification_hash": wrong_hash}
        )
    with pytest.raises(ValidationError, match="accepted Specification hash"):
        SprintPlannerInput.model_validate(
            {**sprint_payload, "accepted_specification_hash": wrong_hash}
        )

    with pytest.raises(ValidationError):
        UserStoryWriterInput.model_validate(
            {**story_payload, "accepted_specification_version_id": 0}
        )
    with pytest.raises(ValidationError):
        SprintPlannerInput.model_validate(
            {**sprint_payload, "accepted_specification_version_id": 0}
        )


def test_parent_evidence_must_already_be_canonical_and_qualifying() -> None:
    with pytest.raises(ValueError, match="unique and sorted"):
        canonical_spec_item_ids(
            _reference(),
            ("REQ.alpha",),
            parent_spec_item_ids=("REQ.alpha", "EXAMPLE.sample"),
        )

    with pytest.raises(SpecificationReferenceError, match="qualifying normative"):
        canonical_spec_item_ids(
            _reference(),
            ("REQ.alpha",),
            parent_spec_item_ids=("EXAMPLE.sample",),
        )

    with pytest.raises(SpecificationReferenceError, match="unknown"):
        canonical_spec_item_ids(
            _reference(),
            ("REQ.alpha",),
            parent_spec_item_ids=("REQ.missing",),
        )


@pytest.mark.parametrize(
    ("item_type", "level", "expected"),
    [
        (SpecItemType.REQ, RequirementLevel.MUST, True),
        (SpecItemType.QUALITY, RequirementLevel.MUST_NOT, True),
        (SpecItemType.CONSTRAINT, RequirementLevel.SHOULD, True),
        (SpecItemType.INTERFACE, RequirementLevel.MAY, True),
        (SpecItemType.DATA, RequirementLevel.MUST, True),
        (SpecItemType.REQ, RequirementLevel.INFORMATIVE, False),
        (SpecItemType.QUALITY, RequirementLevel.INFORMATIVE, False),
        (SpecItemType.CONSTRAINT, RequirementLevel.INFORMATIVE, False),
        (SpecItemType.INTERFACE, RequirementLevel.INFORMATIVE, False),
        (SpecItemType.DATA, RequirementLevel.INFORMATIVE, False),
        (SpecItemType.GOAL, RequirementLevel.MUST, False),
        (SpecItemType.NON_GOAL, RequirementLevel.MUST_NOT, False),
        (SpecItemType.DECISION, RequirementLevel.SHOULD, False),
        (SpecItemType.RISK, RequirementLevel.MAY, False),
        (SpecItemType.EXAMPLE, RequirementLevel.INFORMATIVE, False),
    ],
)
def test_qualifying_normative_evidence_uses_the_profile_type_and_level_contract(
    item_type: SpecItemType,
    level: RequirementLevel,
    expected: bool,
) -> None:
    assert (
        has_qualifying_normative_evidence(
            (_evidence_item(item_type=item_type, level=level),)
        )
        is expected
    )


def test_qualifying_normative_evidence_accepts_one_qualifying_item_among_mixed() -> (
    None
):
    assert has_qualifying_normative_evidence(
        (
            _evidence_item(
                item_type=SpecItemType.EXAMPLE,
                level=RequirementLevel.INFORMATIVE,
            ),
            _evidence_item(
                item_type=SpecItemType.REQ,
                level=RequirementLevel.INFORMATIVE,
            ),
            _evidence_item(item_type=SpecItemType.DATA, level=RequirementLevel.MUST),
        )
    )


def test_canonical_spec_item_ids_reuses_qualifying_normative_evidence() -> None:
    payload = _payload().model_copy(
        update={
            "items": (
                _evidence_item(
                    item_type=SpecItemType.REQ,
                    level=RequirementLevel.INFORMATIVE,
                ),
            )
        }
    )
    reference = AcceptedSpecificationReference(
        spec_version_id=7,
        spec_hash=canonical_spec_hash(payload),
        canonical_specification_json=canonical_spec_json(payload),
        payload=payload,
    )

    with pytest.raises(SpecificationReferenceError, match="qualifying normative"):
        canonical_spec_item_ids(reference, ("REQ.evidence",))
