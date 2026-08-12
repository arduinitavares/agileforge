"""Closed to-spec model input and output contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.specification_authoring import (
    AcceptedProductGoalContext,
    AcceptedVisionContext,
    SpecificationAuthoringInput,
    SpecificationAuthoringOutput,
    SpecificationSourceContext,
)
from services.specs.candidate_contract import (
    CandidateSourceKind,
    CandidateSourceManifestEntry,
)

FINGERPRINT = "sha256:" + ("a" * 64)


def _input() -> SpecificationAuthoringInput:
    manifest = (
        CandidateSourceManifestEntry(
            source_id="SRC.vision.1",
            kind=CandidateSourceKind.VISION,
            fingerprint=FINGERPRINT,
        ),
        CandidateSourceManifestEntry(
            source_id="SRC.product-goal.2",
            kind=CandidateSourceKind.PRODUCT_GOAL,
            fingerprint=FINGERPRINT,
        ),
    )
    return SpecificationAuthoringInput(
        project_id=9,
        project_name="Contract project",
        operation="initial",
        accepted_vision=AcceptedVisionContext(
            artifact_id=1,
            fingerprint=FINGERPRINT,
            statement="Operators can trust one exact lifecycle.",
            components={"target_user": "operator"},
        ),
        accepted_product_goal=AcceptedProductGoalContext(
            artifact_id=2,
            fingerprint=FINGERPRINT,
            statement="Complete one accepted product increment.",
        ),
        source_manifest=manifest,
        source_context=tuple(
            SpecificationSourceContext(
                source_id=item.source_id,
                kind=item.kind,
                fingerprint=item.fingerprint,
                content={"statement": item.source_id},
            )
            for item in manifest
        ),
    )


def test_authoring_input_is_closed_and_binds_manifest_context() -> None:
    """Every source exposed to the model is host-owned and fingerprinted."""
    contract = _input()

    assert contract.schema_version == "agileforge.spec-authoring-input.v2"
    assert contract.operation == "initial"
    assert contract.base_specification is None
    assert contract.prior_candidate is None
    assert tuple(item.source_id for item in contract.source_manifest) == (
        "SRC.product-goal.2",
        "SRC.vision.1",
    )

    raw = contract.model_dump(mode="json")
    raw["raw_markdown"] = "# caller supplied"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpecificationAuthoringInput.model_validate(raw)


def test_authoring_output_contains_only_semantics_and_amendment_declarations() -> None:
    """The model cannot choose lineage, hashes, review state, or attempt metadata."""
    output = SpecificationAuthoringOutput.model_validate(
        {
            "payload": {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": "SPEC.direct-boundary",
                "title": "Direct boundary",
                "summary": "One typed Specification authoring boundary.",
                "problem_statement": "Raw workflow JSON is ambiguous.",
                "items": [
                    {
                        "id": "REQ.direct-boundary",
                        "type": "REQ",
                        "title": "Typed authoring",
                        "statement": "The producer MUST return a typed payload.",
                        "level": "MUST",
                        "verification": "system-test",
                        "acceptance": ["The payload validates as v2."],
                        "source_notes": [
                            {
                                "source_id": "SRC.product-goal.2",
                                "kind": "interview",
                                "text": "Accepted Product Goal context.",
                            }
                        ],
                    }
                ],
            },
            "removal_justifications": {},
            "stable_id_replacements": [],
        }
    )

    assert output.payload.artifact_id == "SPEC.direct-boundary"
    raw = output.model_dump(mode="json")
    raw["candidate_fingerprint"] = FINGERPRINT
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpecificationAuthoringOutput.model_validate(raw)


def test_initial_input_rejects_base_or_prior_candidate() -> None:
    """Initial composition cannot hide amendment or revision context."""
    raw = _input().model_dump(mode="json")
    raw["base_specification"] = {
        "spec_version_id": 3,
        "payload_fingerprint": FINGERPRINT,
        "payload": {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.base",
            "title": "Base",
            "summary": "Base summary.",
            "problem_statement": "Base problem.",
            "items": [],
        },
    }

    with pytest.raises(ValidationError, match="initial authoring cannot include"):
        SpecificationAuthoringInput.model_validate(raw)

