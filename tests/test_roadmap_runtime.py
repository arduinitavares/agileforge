"""Tests for Roadmap runtime input projection."""

from __future__ import annotations

import json
from typing import cast

from services.contracts.roadmap import (
    RoadmapBuilderInput,
)
from services.roadmap_runtime import build_roadmap_input_context


def test_build_roadmap_input_context_strips_refinement_metadata() -> None:
    """Roadmap input must not leak refinement-only item fields to the schema."""
    state = {
        "product_vision_assessment": {
            "product_vision_statement": "A safe brownfield Cartola workflow.",
        },
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": {"authority": True},
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Validate Captain-Aware Optimization Contract",
                "authority_ref": "REQ.captain-aware-optimization",
                "capability_hint": "Captain Aware Optimization",
                "as_built_annotation": {
                    "schema_version": "agileforge.brownfield_annotation.v1",
                    "match_tier": "exact",
                    "match_basis": ["authority_ref"],
                },
                "value_driver": "Strategic",
                "justification": "Verify the existing captain multiplier contract.",
                "estimated_effort": "M",
                "technical_note": "Brownfield verification item.",
                "item_id": "item-001",
                "item_fingerprint": "sha256:item",
                "classification": "verification",
                "refinement_provenance": {"operation_id": "op-1"},
                "source_attempt_id": "backlog-attempt-12",
                "source_artifact_fingerprint": "sha256:source",
            }
        ],
    }

    input_context = build_roadmap_input_context(state, user_input="Regenerate")
    parsed = RoadmapBuilderInput.model_validate(input_context)

    backlog_items = input_context["backlog_items"]
    assert isinstance(backlog_items, list)
    item = cast("dict[str, object]", backlog_items[0])
    assert isinstance(item, dict)
    assert "as_built_annotation" not in item
    assert "item_id" not in item
    assert "item_fingerprint" not in item
    assert "classification" not in item
    assert "refinement_provenance" not in item
    assert "source_attempt_id" not in item
    assert "source_artifact_fingerprint" not in item
    assert parsed.backlog_items[0].requirement == (
        "Validate Captain-Aware Optimization Contract"
    )


def test_build_roadmap_input_context_retains_prior_roadmap_without_mode_flags() -> None:
    """Prior Roadmap state remains context without reconciliation control fields."""
    existing_roadmap = [
        {
            "release_name": "Milestone 1",
            "theme": "Foundation",
            "focus_area": "Technical Foundation",
            "items": ["Requirement A"],
            "reasoning": "Existing plan.",
        },
        {
            "release_name": "Milestone 2",
            "theme": "Value",
            "focus_area": "User Value",
            "items": ["Requirement B"],
            "reasoning": "Existing value loop.",
        },
    ]
    state = {
        "product_vision_assessment": {
            "product_vision_statement": "A safe brownfield workflow.",
        },
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": {"authority": True},
        "roadmap_releases": existing_roadmap,
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Requirement A",
                "value_driver": "Strategic",
                "justification": "Existing foundation.",
                "estimated_effort": "M",
            },
            {
                "priority": 2,
                "requirement": "Requirement B",
                "value_driver": "Strategic",
                "justification": "Existing value loop.",
                "estimated_effort": "M",
            },
        ],
    }

    input_context = build_roadmap_input_context(
        state,
        user_input="Refine the prior Roadmap without moving accepted items.",
    )
    parsed = RoadmapBuilderInput.model_validate(input_context)

    assert "generation_mode" not in input_context
    assert "locked_roadmap_shape" not in input_context
    assert "scope_extension" not in input_context
    assert parsed.prior_roadmap_state == json.dumps(
        existing_roadmap,
        ensure_ascii=False,
    )
    assert parsed.user_input == (
        "Refine the prior Roadmap without moving accepted items."
    )
