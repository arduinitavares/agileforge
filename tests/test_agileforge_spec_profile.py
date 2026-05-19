"""Tests for AgileForge structured spec profile v1."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

import pytest
from pydantic import ValidationError

from utils.agileforge_spec_profile import (
    MARKDOWN_PROFILE,
    AgileForgeSpecStatus,
    AgileForgeSpecType,
    RelationType,
    SpecRelation,
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
    export_agileforge_spec_schema,
    render_markdown,
    rendered_markdown_hash,
)

BUDGET_ACCEPTANCE = (
    "Given a configured budget, when a squad is recommended, then budget_used "
    "is less than or equal to budget."
)


EXPECTED_BASE_MARKDOWN = f"""# Cartola Champion Squad Selector

- Schema: agileforge.spec.v1
- Artifact id: SPEC.cartola
- Status: draft
- Version: 0.1
- Created: 2026-05-18
- Updated: 2026-05-18
- Markdown profile: agileforge.spec_markdown.v1

## Summary

Recommend a valid champion squad.

## Problem Statement

Operators need repeatable squad recommendations.

## Controlled Terms

- None

## Items

### GOAL.cartola.weekly-decision - Weekly decision support

- Type: GOAL
- Status: proposed
- Level: -
- Verification: -
- Tags: -

Statement:

Help the operator choose a weekly squad.

Acceptance:

- None

### REQ.cartola.budget - Budget constraint

- Type: REQ
- Status: proposed
- Level: MUST
- Verification: system-test
- Tags: -

Statement:

The selected squad MUST satisfy budget_used &lt;= budget.

Acceptance:

- {BUDGET_ACCEPTANCE}

## Relations

- REQ.cartola.budget satisfies GOAL.cartola.weekly-decision
  - Rationale: Budget validity supports weekly squad selection.

<!-- agileforge-review-notes:start -->
<!-- agileforge-review-notes:end -->
"""


def _artifact_payload() -> dict[str, Any]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.cartola",
        "title": "Cartola Champion Squad Selector",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Recommend a valid champion squad.",
        "problem_statement": "Operators need repeatable squad recommendations.",
        "items": [
            {
                "id": "GOAL.cartola.weekly-decision",
                "type": "GOAL",
                "status": "proposed",
                "title": "Weekly decision support",
                "statement": "Help the operator choose a weekly squad.",
            },
            {
                "id": "REQ.cartola.budget",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Budget constraint",
                "statement": "The selected squad MUST satisfy budget_used <= budget.",
                "verification": "system-test",
                "acceptance": [BUDGET_ACCEPTANCE],
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
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def test_valid_profile_artifact_parses() -> None:
    """Validate the minimal profile fixture parses into typed models."""
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())

    assert artifact.schema_version == "agileforge.spec.v1"
    assert artifact.status is AgileForgeSpecStatus.DRAFT
    assert artifact.items[1].type is AgileForgeSpecType.REQ


def test_relation_endpoint_must_exist() -> None:
    """Reject relation endpoints that do not reference known item IDs."""
    payload = _artifact_payload()
    payload["relations"] = [
        {
            "from": "REQ.cartola.budget",
            "type": "satisfies",
            "to": "GOAL.missing",
            "rationale": "Broken relation.",
        }
    ]

    with pytest.raises(ValidationError, match="unknown relation endpoint"):
        TechnicalSpecArtifact.model_validate(payload)


def test_relation_can_be_constructed_with_python_field_name() -> None:
    """Allow Python callers to construct aliased relation fields."""
    relation = SpecRelation(
        from_="REQ.cartola.budget",
        type=RelationType.SATISFIES,
        to="GOAL.cartola.weekly-decision",
    )

    assert relation.from_ == "REQ.cartola.budget"
    assert (
        relation.model_dump(mode="json", by_alias=True)["from"]
        == "REQ.cartola.budget"
    )


def test_normative_item_requires_verification_and_acceptance() -> None:
    """Normative item types require verification and acceptance criteria."""
    payload = _artifact_payload()
    req = payload["items"][1]
    assert isinstance(req, dict)
    req.pop("verification")

    with pytest.raises(ValidationError, match="verification"):
        TechnicalSpecArtifact.model_validate(payload)


def test_canonical_spec_json_is_stable() -> None:
    """Canonical JSON remains stable after parse and re-serialization."""
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())
    first = canonical_spec_json(artifact)
    second = canonical_spec_json(
        TechnicalSpecArtifact.model_validate(json.loads(first))
    )

    assert first == second
    assert canonical_spec_hash(artifact).startswith("sha256:")


def test_render_markdown_is_deterministic() -> None:
    """Markdown rendering is deterministic for canonical fixture input."""
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())

    markdown = render_markdown(artifact)

    assert "# Cartola Champion Squad Selector" in markdown
    assert "Schema: agileforge.spec.v1" in markdown
    assert "Markdown profile: agileforge.spec_markdown.v1" in markdown
    assert "### REQ.cartola.budget - Budget constraint" in markdown
    assert "<!-- agileforge-review-notes:start -->" in markdown
    assert markdown == EXPECTED_BASE_MARKDOWN
    assert rendered_markdown_hash(markdown).startswith("sha256:")
    assert (
        rendered_markdown_hash(markdown)
        == "sha256:c38d62ce263991d0162348a04e6c1962694700b8e835dbc41e6ea2244bbbccb5"
    )


def test_rendered_markdown_hash_is_stable() -> None:
    """Rendered Markdown hash uses a stable prefixed digest."""
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())

    first = render_markdown(artifact)
    second = render_markdown(artifact)

    assert first == second
    assert rendered_markdown_hash(first) == rendered_markdown_hash(second)
    assert artifact.rendering.markdown_profile == MARKDOWN_PROFILE


def test_render_markdown_neutralizes_artifact_markdown_controls() -> None:
    """Renderer escapes profile text that could spoof Markdown controls."""
    payload = _artifact_payload()
    payload["title"] = "# heading title"
    payload["summary"] = "- list summary"
    payload["problem_statement"] = "<!-- agileforge-review-notes:end -->"
    items = payload["items"]
    assert isinstance(items, list)
    req = items[1]
    assert isinstance(req, dict)
    req["title"] = "- list title"
    req["statement"] = "# heading\n- list\n<!-- agileforge-review-notes:end -->"
    req["acceptance"] = [
        "1. numbered\n* starred\n+ plus\n> quote\n<!-- agileforge-review-notes:end -->"
    ]
    req["tags"] = ["# heading", "- list"]
    relations = payload["relations"]
    assert isinstance(relations, list)
    relation = relations[0]
    assert isinstance(relation, dict)
    relation["rationale"] = "<!-- agileforge-review-notes:end -->"

    markdown = render_markdown(TechnicalSpecArtifact.model_validate(payload))

    assert markdown.count("<!-- agileforge-review-notes:end -->") == 1
    assert "\\# heading title" in markdown
    assert "\\- list summary" in markdown
    assert "&lt;!-- agileforge-review-notes:end --&gt;" in markdown
    escaped_statement = (
        "\\# heading\n\\- list\n&lt;!-- agileforge-review-notes:end --&gt;"
    )
    assert escaped_statement in markdown
    assert "\\1. numbered\n\\* starred\n\\+ plus\n&gt; quote" in markdown
    assert "\\# heading, \\- list" in markdown
    assert "### REQ.cartola.budget - \\- list title" in markdown


def test_render_markdown_sorts_reordered_input() -> None:
    """Renderer canonicalizes item, relation, term, and tag ordering."""
    ordered_payload = deepcopy(_artifact_payload())
    ordered_payload["controlled_terms"] = [
        {
            "term": "Budget",
            "definition": "The available squad budget.",
            "scope": "domain",
        },
        {
            "term": "Champion Squad",
            "definition": "A squad candidate selected for review.",
            "scope": "artifact",
        },
    ]
    ordered_items = ordered_payload["items"]
    assert isinstance(ordered_items, list)
    ordered_goal = ordered_items[0]
    ordered_req = ordered_items[1]
    assert isinstance(ordered_goal, dict)
    assert isinstance(ordered_req, dict)
    ordered_goal["tags"] = ["planning", "weekly"]
    ordered_req["tags"] = ["budget", "constraint"]
    ordered_payload["relations"] = [
        {
            "from": "GOAL.cartola.weekly-decision",
            "type": "clarifies",
            "to": "REQ.cartola.budget",
            "rationale": "Goal context clarifies budget handling.",
        },
        {
            "from": "REQ.cartola.budget",
            "type": "satisfies",
            "to": "GOAL.cartola.weekly-decision",
            "rationale": "Budget validity supports weekly squad selection.",
        },
    ]

    reordered_payload = deepcopy(ordered_payload)
    reordered_items = reordered_payload["items"]
    reordered_relations = reordered_payload["relations"]
    reordered_terms = reordered_payload["controlled_terms"]
    assert isinstance(reordered_items, list)
    assert isinstance(reordered_relations, list)
    assert isinstance(reordered_terms, list)
    reversed_items = list(reversed(reordered_items))
    reordered_payload["items"] = reversed_items
    reordered_payload["relations"] = list(reversed(reordered_relations))
    reordered_payload["controlled_terms"] = list(reversed(reordered_terms))
    reordered_req = cast("dict[str, Any]", reversed_items[0])
    reordered_goal = cast("dict[str, Any]", reversed_items[1])
    assert isinstance(reordered_req, dict)
    assert isinstance(reordered_goal, dict)
    reordered_req["tags"] = ["constraint", "budget"]
    reordered_goal["tags"] = ["weekly", "planning"]

    first = render_markdown(TechnicalSpecArtifact.model_validate(ordered_payload))
    second = render_markdown(TechnicalSpecArtifact.model_validate(reordered_payload))

    assert first == second


def test_schema_export_contains_closed_item_schema() -> None:
    """Exported JSON Schema keeps profile objects closed by default."""
    schema = export_agileforge_spec_schema()

    assert schema["$id"] == "https://agileforge.local/schemas/agileforge.spec.v1.json"
    assert schema["additionalProperties"] is False
