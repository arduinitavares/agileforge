"""Tests for deterministic Authority input derived from Specification v2."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from services.contracts.authority_input_v2 import (
    AuthorityInputV2,
    build_authority_input_v2,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload


def _payload_data() -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v2",
        "artifact_id": "SPEC.authority-input",
        "title": "Authority input",
        "summary": "Separate normative rules from review context.",
        "problem_statement": "Provenance prose must never become an invariant.",
        "items": [
            {
                "id": "GOAL.authority.reviewable",
                "type": "GOAL",
                "title": "Reviewable Authority",
                "statement": "Keep compiled Authority reviewable.",
            },
            {
                "id": "REQ.authority.typed-input",
                "type": "REQ",
                "title": "Typed compiler input",
                "statement": "Authority compilation MUST consume typed input.",
                "level": "MUST",
                "verification": "system-test",
                "acceptance": ["The compiler receives no raw Markdown."],
                "source_notes": [
                    {
                        "source_id": "SRC.secret",
                        "kind": "external_summary",
                        "text": "SECRET PROVENANCE PROSE MUST BE ABSENT",
                        "external_ref_id": "EXT.secret",
                    }
                ],
            },
            {
                "id": "DATA.authority.source-id",
                "type": "DATA",
                "title": "Source item identity",
                "statement": "Each invariant MUST cite an eligible item ID.",
                "level": "MUST",
                "verification": "inspection",
                "acceptance": ["Every invariant cites an eligible item ID."],
            },
            {
                "id": "QUALITY.authority.explanatory",
                "type": "QUALITY",
                "title": "Explanatory context",
                "statement": "Explanatory prose is informative only.",
                "level": "INFORMATIVE",
                "verification": "manual-review",
                "acceptance": ["The prose is visible for review."],
            },
            {
                "id": "RISK.authority.promotion",
                "type": "RISK",
                "title": "Context promotion",
                "statement": "Context could be promoted into an invariant.",
            },
        ],
        "relations": [
            {
                "from": "REQ.authority.typed-input",
                "type": "depends_on",
                "to": "DATA.authority.source-id",
            },
            {
                "from": "REQ.authority.typed-input",
                "type": "satisfies",
                "to": "GOAL.authority.reviewable",
            },
        ],
        "controlled_terms": [
            {
                "term": "eligible item",
                "definition": "A normative item allowed to source an invariant.",
                "scope": "artifact",
            }
        ],
        "external_references": [
            {
                "id": "EXT.secret",
                "title": "Secret source",
                "summary": "EXTERNAL REFERENCE PROSE MUST BE ABSENT",
            }
        ],
    }


def _payload() -> SpecificationPayload:
    return SpecificationPayload.model_validate(_payload_data())


def test_builder_strips_provenance_prose_from_authority_input() -> None:
    """Source notes and external-reference prose never cross the boundary."""
    authority_input = build_authority_input_v2(_payload())

    serialized = authority_input.model_dump_json()

    assert "SECRET PROVENANCE PROSE MUST BE ABSENT" not in serialized
    assert "EXTERNAL REFERENCE PROSE MUST BE ABSENT" not in serialized
    assert "source_notes" not in serialized
    assert "external_references" not in serialized
    assert authority_input.controlled_terms[0].term == "eligible item"
    assert "eligible item" not in authority_input.eligible_item_ids


def test_builder_classifies_only_supported_noninformative_items_as_normative() -> None:
    """Type and level form a closed invariant-source allowlist."""
    authority_input = build_authority_input_v2(_payload())

    assert authority_input.eligible_item_ids == (
        "DATA.authority.source-id",
        "REQ.authority.typed-input",
    )
    assert tuple(item.id for item in authority_input.normative_items) == (
        "DATA.authority.source-id",
        "REQ.authority.typed-input",
    )
    assert tuple(item.id for item in authority_input.review_context) == (
        "GOAL.authority.reviewable",
        "QUALITY.authority.explanatory",
        "RISK.authority.promotion",
    )


def test_builder_keeps_only_relations_between_eligible_items() -> None:
    """A context endpoint cannot become an invariant source through an edge."""
    authority_input = build_authority_input_v2(_payload())

    assert len(authority_input.normative_relations) == 1
    relation = authority_input.normative_relations[0]
    assert relation.from_ == "REQ.authority.typed-input"
    assert relation.to == "DATA.authority.source-id"


def test_builder_preserves_authored_acceptance_order() -> None:
    """Ordered acceptance evidence remains byte-significant in compiler input."""
    payload_data = _payload_data()
    items = payload_data["items"]
    assert isinstance(items, list)
    requirement = items[1]
    assert isinstance(requirement, dict)
    requirement["acceptance"] = ["Second authored check.", "First lexical check."]

    authority_input = build_authority_input_v2(
        SpecificationPayload.model_validate(payload_data)
    )

    requirement_input = next(
        item
        for item in authority_input.normative_items
        if item.id == "REQ.authority.typed-input"
    )
    assert requirement_input.acceptance == (
        "Second authored check.",
        "First lexical check.",
    )


def test_authority_input_fingerprint_is_stable_under_input_permutations() -> None:
    """Set-like Specification collections have one Authority-input identity."""
    original = _payload_data()
    permuted = deepcopy(original)
    for field in ("items", "relations", "controlled_terms"):
        values = permuted[field]
        assert isinstance(values, list)
        permuted[field] = list(reversed(values))

    first = build_authority_input_v2(SpecificationPayload.model_validate(original))
    second = build_authority_input_v2(SpecificationPayload.model_validate(permuted))

    assert first == second
    assert first.authority_input_fingerprint == second.authority_input_fingerprint


def test_authority_input_dto_is_closed_and_frozen() -> None:
    """Compiler input cannot gain ad-hoc fields or mutate after validation."""
    authority_input = build_authority_input_v2(_payload())

    with pytest.raises(ValidationError):
        authority_input.artifact_id = "SPEC.changed"

    raw = authority_input.model_dump(mode="json")
    raw["unexpected"] = True
    with pytest.raises(ValidationError):
        AuthorityInputV2.model_validate(raw)
