"""ValidationEvidence v2 contract and canonical persistence tests."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from services.contracts.specification_validation import StorySpecificationFinding
from utils.spec_schemas import StructuralValidationFailure, ValidationEvidence
from workflow.fingerprints import canonical_json


def _evidence(**changes: object) -> ValidationEvidence:
    values: dict[str, object] = {
        "schema_version": "agileforge.story-validation-evidence.v2",
        "project_id": 3,
        "story_id": 5,
        "source_story_artifact_id": 7,
        "source_story_artifact_fingerprint": "sha256:" + "1" * 64,
        "source_story_item_id": "US-0001",
        "source_story_item_fingerprint": "sha256:" + "2" * 64,
        "source_backlog_artifact_id": 11,
        "source_backlog_artifact_fingerprint": "sha256:" + "3" * 64,
        "source_backlog_item_id": "PBI-000001",
        "spec_version_id": 13,
        "spec_hash": "sha256:" + "4" * 64,
        "validated_at": datetime(2026, 8, 21, 12, tzinfo=UTC),
        "story_validation_input_fingerprint": "sha256:" + "5" * 64,
        "validator_version": "2.0.0",
        "mode": "structural",
        "ready_for_sprint": True,
        "structural_failures": (),
        "structural_warnings": (),
        "semantic_review_state": "not_requested",
        "semantic_findings": (),
        "referenced_spec_item_ids": ("DATA.001",),
    }
    values.update(changes)
    return ValidationEvidence.model_validate(values)


def test_validation_evidence_v2_is_closed_frozen_and_canonical() -> None:
    evidence = _evidence()
    encoded = canonical_json(evidence.model_dump(mode="json"))
    assert ValidationEvidence.model_validate_json(encoded, strict=True) == evidence
    assert "invariant" not in encoded
    assert "passed" not in evidence.model_fields_set

    with pytest.raises(ValidationError):
        _evidence(extra_v1_field=[])
    with pytest.raises(ValidationError):
        evidence.story_id = 9  # type: ignore[misc]


def test_validation_evidence_enforces_structural_and_semantic_consistency() -> None:
    failure = StructuralValidationFailure(
        code="STORY_STATEMENT_INVALID",
        message="Story statement does not use the required shape.",
    )
    with pytest.raises(ValidationError):
        _evidence(structural_failures=(failure,), ready_for_sprint=True)
    with pytest.raises(ValidationError):
        _evidence(
            mode="structural",
            semantic_review_state="valid",
            semantic_findings=(),
        )
    with pytest.raises(ValidationError):
        _evidence(
            mode="hybrid",
            semantic_review_state="invalid",
            semantic_findings=(
                StorySpecificationFinding(
                    code="SPEC_ITEM_OMISSION",
                    spec_item_id="DATA.001",
                    message="Missing coverage.",
                ),
            ),
            ready_for_sprint=False,
        )


def test_validation_evidence_requires_ordered_codes_and_derived_references() -> None:
    failures = (
        StructuralValidationFailure(
            code="ACCEPTANCE_CRITERIA_INVALID",
            message="Criteria invalid.",
        ),
        StructuralValidationFailure(
            code="STORY_STATEMENT_INVALID",
            message="Statement invalid.",
        ),
    )
    with pytest.raises(ValidationError):
        _evidence(structural_failures=failures, ready_for_sprint=False)
    with pytest.raises(ValidationError):
        _evidence(referenced_spec_item_ids=("DATA.001", "DATA.001"))
