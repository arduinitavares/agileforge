"""Closed to-spec model input and output contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_VISION_SOURCE_ID,
    AcceptedProductGoalContext,
    AcceptedVisionContext,
    SpecificationAuthoringInput,
    SpecificationAuthoringOutput,
    SpecificationSourceContext,
    specification_authoring_fact_fingerprint,
    specification_authoring_input_fingerprint,
)
from services.specs.candidate_contract import (
    CandidateSourceKind,
    CandidateSourceManifestEntry,
)

FINGERPRINT = "sha256:" + ("a" * 64)


def _input() -> SpecificationAuthoringInput:
    manifest = (
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_VISION_SOURCE_ID,
            kind=CandidateSourceKind.VISION,
            fingerprint=FINGERPRINT,
        ),
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
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
        SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
        SPECIFICATION_VISION_SOURCE_ID,
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
                                "source_id": SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
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


def test_authoring_fingerprints_exclude_host_database_identity() -> None:
    """Equivalent semantic authoring input has one portable producer identity."""
    baseline = _input()
    recreated_data = baseline.model_dump(mode="json")
    recreated_data["project_id"] = 109
    accepted_vision = recreated_data["accepted_vision"]
    accepted_goal = recreated_data["accepted_product_goal"]
    assert isinstance(accepted_vision, dict)
    assert isinstance(accepted_goal, dict)
    accepted_vision["artifact_id"] = 101
    accepted_goal["artifact_id"] = 102
    recreated = SpecificationAuthoringInput.model_validate(recreated_data)

    assert specification_authoring_input_fingerprint(baseline) == (
        specification_authoring_input_fingerprint(recreated)
    )
    assert specification_authoring_fact_fingerprint(baseline) == (
        specification_authoring_fact_fingerprint(recreated)
    )

    changed_data = recreated.model_dump(mode="json")
    changed_goal = changed_data["accepted_product_goal"]
    assert isinstance(changed_goal, dict)
    changed_fingerprint = "sha256:" + ("b" * 64)
    changed_goal["fingerprint"] = changed_fingerprint
    for collection_name in ("source_manifest", "source_context"):
        collection = changed_data[collection_name]
        assert isinstance(collection, list)
        for entry in collection:
            assert isinstance(entry, dict)
            if entry["source_id"] == SPECIFICATION_PRODUCT_GOAL_SOURCE_ID:
                entry["fingerprint"] = changed_fingerprint
    changed = SpecificationAuthoringInput.model_validate(changed_data)
    assert specification_authoring_input_fingerprint(baseline) != (
        specification_authoring_input_fingerprint(changed)
    )
    assert specification_authoring_fact_fingerprint(baseline) != (
        specification_authoring_fact_fingerprint(changed)
    )


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
