"""Tests for the closed AgileForge specification profile v2."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from utils.agileforge_spec_profile_v2 import (
    SCHEMA_VERSION,
    SpecificationPayload,
    canonical_spec_hash,
    canonical_spec_json,
    render_markdown,
    rendered_markdown_hash,
)

EXPECTED_SCOPED_TERM_COUNT = 2


def _payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": "SPEC.cartola",
        "title": "Cartola Champion Squad Selector",
        "summary": "Recommend a valid champion squad.",
        "problem_statement": "Operators need repeatable squad recommendations.",
        "items": [
            {
                "id": "GOAL.cartola.weekly-decision",
                "type": "GOAL",
                "title": "Weekly decision support",
                "statement": "Help the operator choose a weekly squad.",
                "acceptance": ["A weekly decision is available."],
            },
            {
                "id": "REQ.cartola.budget",
                "type": "REQ",
                "title": "Budget constraint",
                "statement": "The selected squad MUST satisfy budget_used <= budget.",
                "level": "MUST",
                "verification": "system-test",
                "acceptance": [
                    "Given a configured budget, a selected squad stays within it.",
                    "The visible budget total matches the selected players.",
                ],
                "tags": ["Budget", "squad"],
                "source_notes": [
                    {
                        "source_id": "SRC.goal-interview",
                        "kind": "interview",
                        "text": "Operators need a repeatable weekly decision.",
                    },
                    {
                        "source_id": "SRC.external-budget",
                        "kind": "external_summary",
                        "text": "Budget rules are published externally.",
                        "external_ref_id": "EXT.budget-rules",
                    },
                ],
            },
        ],
        "relations": [
            {
                "from": "REQ.cartola.budget",
                "type": "satisfies",
                "to": "GOAL.cartola.weekly-decision",
                "rationale": "Budget validity supports weekly squad selection.",
            }
        ],
        "controlled_terms": [
            {
                "term": "Budget",
                "definition": "The total player cost available to a squad.",
                "scope": "project",
            }
        ],
        "external_references": [
            {
                "id": "EXT.budget-rules",
                "title": "Budget rules",
                "url": "https://example.invalid/budget",
                "summary": "Published squad budget rules.",
            }
        ],
    }


def test_payload_parses_closed_v2_items_without_lifecycle_status() -> None:
    """A v2 payload carries typed content but no review lifecycle state."""
    payload = SpecificationPayload.model_validate(_payload())

    assert payload.schema_version == SCHEMA_VERSION
    assert payload.items[1].type.value == "REQ"
    assert payload.items[1].level is not None

    invalid = _payload()
    items = invalid["items"]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    first["status"] = "accepted"
    with pytest.raises(ValidationError, match="status"):
        SpecificationPayload.model_validate(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "FEATURE"),
        ("level", "REQUIRED"),
        ("verification", "browser-test"),
    ],
)
def test_payload_rejects_closed_item_fields(field: str, value: object) -> None:
    """Item categories, levels, and verification methods remain closed values."""
    invalid = _payload()
    items = invalid["items"]
    assert isinstance(items, list)
    item = items[1]
    assert isinstance(item, dict)
    item[field] = value

    with pytest.raises(ValidationError):
        SpecificationPayload.model_validate(invalid)


def test_payload_rejects_duplicate_or_unresolved_stable_references() -> None:
    """Stable IDs, normalized sets, and relation endpoints must be valid."""
    duplicate_item = _payload()
    items = duplicate_item["items"]
    assert isinstance(items, list)
    second = items[1]
    assert isinstance(second, dict)
    items.append(deepcopy(second))
    with pytest.raises(ValidationError, match="duplicate item ids"):
        SpecificationPayload.model_validate(duplicate_item)

    unknown_endpoint = _payload()
    relations = unknown_endpoint["relations"]
    assert isinstance(relations, list)
    relation = relations[0]
    assert isinstance(relation, dict)
    relation["to"] = "GOAL.missing"
    with pytest.raises(ValidationError, match="unknown relation endpoint"):
        SpecificationPayload.model_validate(unknown_endpoint)

    duplicate_tag = _payload()
    tag_items = duplicate_tag["items"]
    assert isinstance(tag_items, list)
    tagged = tag_items[1]
    assert isinstance(tagged, dict)
    tagged["tags"] = ["budget", "Budget"]
    with pytest.raises(ValidationError, match="duplicate normalized tags"):
        SpecificationPayload.model_validate(duplicate_tag)


def test_payload_rejects_host_and_candidate_metadata() -> None:
    """Host-owned lifecycle and provenance metadata cannot enter semantic bytes."""
    invalid = _payload()
    invalid["candidate_id"] = "candidate-7"

    with pytest.raises(ValidationError, match="candidate_id"):
        SpecificationPayload.model_validate(invalid)


def test_normative_item_requires_level_verification_and_acceptance() -> None:
    """Authority-eligible v1 item types keep their complete semantic contract."""
    invalid = _payload()
    items = invalid["items"]
    assert isinstance(items, list)
    requirement = items[1]
    assert isinstance(requirement, dict)
    requirement.pop("verification")

    with pytest.raises(ValidationError, match="verification"):
        SpecificationPayload.model_validate(invalid)


def test_scalar_text_rejects_blanks_without_silently_trimming_bytes() -> None:
    """Semantic scalar bytes are preserved once validated as nonblank."""
    payload_data = _payload()
    payload_data["summary"] = "  Keep exact spacing.  "
    payload = SpecificationPayload.model_validate(payload_data)
    assert payload.summary == "  Keep exact spacing.  "

    blank = _payload()
    blank["summary"] = "   "
    with pytest.raises(ValidationError, match="blank"):
        SpecificationPayload.model_validate(blank)


def test_relation_keys_and_scoped_term_identities_are_unique() -> None:
    """Relations and terms reject only true stable-key duplicates."""
    duplicate_relation = _payload()
    relations = duplicate_relation["relations"]
    assert isinstance(relations, list)
    relations.append(deepcopy(relations[0]))
    with pytest.raises(ValidationError, match="duplicate relation edge"):
        SpecificationPayload.model_validate(duplicate_relation)

    scoped_terms = _payload()
    terms = scoped_terms["controlled_terms"]
    assert isinstance(terms, list)
    terms.append(
        {
            "term": " budget ",
            "definition": "A domain financial cap.",
            "scope": "domain",
        }
    )
    payload = SpecificationPayload.model_validate(scoped_terms)
    assert len(payload.controlled_terms) == EXPECTED_SCOPED_TERM_COUNT


def test_canonical_json_sorts_each_declared_unordered_collection() -> None:
    """Declared sets hash identically regardless of producer ordering."""
    original = _payload()
    permuted = deepcopy(original)
    items = permuted["items"]
    relations = permuted["relations"]
    terms = permuted["controlled_terms"]
    references = permuted["external_references"]
    assert isinstance(items, list)
    assert isinstance(relations, list)
    assert isinstance(terms, list)
    assert isinstance(references, list)
    items.reverse()
    relations.append(
        {
            "from": "GOAL.cartola.weekly-decision",
            "type": "clarifies",
            "to": "REQ.cartola.budget",
        }
    )
    relations.reverse()
    terms.append(
        {
            "term": "Squad",
            "definition": "The selected set of players.",
            "scope": "project",
        }
    )
    terms.reverse()
    references.append(
        {
            "id": "EXT.squad-rules",
            "title": "Squad rules",
            "summary": "Published squad rules.",
        }
    )
    references.reverse()
    original["relations"] = list(reversed(relations))
    original["controlled_terms"] = list(reversed(terms))
    original["external_references"] = list(reversed(references))
    original_items = original["items"]
    assert isinstance(original_items, list)
    original_tags = original_items[1]
    permuted_tags = items[0]
    assert isinstance(original_tags, dict)
    assert isinstance(permuted_tags, dict)
    original_tags["tags"] = ["squad", "Budget"]
    permuted_tags["tags"] = ["Budget", "squad"]

    first = SpecificationPayload.model_validate(original)
    second = SpecificationPayload.model_validate(permuted)

    assert canonical_spec_json(first) == canonical_spec_json(second)
    assert canonical_spec_hash(first) == canonical_spec_hash(second)


def test_canonical_json_preserves_ordered_criteria_and_source_notes() -> None:
    """Ordered review prose remains semantically order-sensitive."""
    original = _payload()
    reordered_criteria = deepcopy(original)
    reordered_notes = deepcopy(original)
    criteria_items = reordered_criteria["items"]
    notes_items = reordered_notes["items"]
    assert isinstance(criteria_items, list)
    assert isinstance(notes_items, list)
    criteria_item = criteria_items[1]
    notes_item = notes_items[1]
    assert isinstance(criteria_item, dict)
    assert isinstance(notes_item, dict)
    criteria = criteria_item["acceptance"]
    notes = notes_item["source_notes"]
    assert isinstance(criteria, list)
    assert isinstance(notes, list)
    criteria.reverse()
    notes.reverse()

    baseline = canonical_spec_json(SpecificationPayload.model_validate(original))
    assert baseline != canonical_spec_json(
        SpecificationPayload.model_validate(reordered_criteria)
    )
    assert baseline != canonical_spec_json(
        SpecificationPayload.model_validate(reordered_notes)
    )


def test_markdown_review_projection_contains_all_authority_affecting_fields() -> None:
    """The deterministic review projection includes typed sources and references."""
    payload = SpecificationPayload.model_validate(_payload())

    markdown = render_markdown(payload)

    for expected in (
        "Schema: agileforge.spec.v2",
        "SPEC.cartola",
        "REQ.cartola.budget",
        "Type: REQ",
        "Level: MUST",
        "Verification: system-test",
        "Given a configured budget",
        "SRC.goal-interview",
        "Operators need a repeatable weekly decision.",
        "EXT.budget-rules",
        "Published squad budget rules.",
        "REQ.cartola.budget satisfies GOAL.cartola.weekly-decision",
    ):
        assert expected in markdown
    assert markdown == render_markdown(payload)
    assert rendered_markdown_hash(markdown).startswith("sha256:")
