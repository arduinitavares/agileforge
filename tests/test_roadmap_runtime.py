"""Tests for Roadmap runtime input projection."""

from __future__ import annotations

from typing import cast

from orchestrator_agent.agent_tools.roadmap_builder.schemes import (
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
    raw_annotation = item.get("as_built_annotation")
    assert isinstance(raw_annotation, dict)
    annotation = cast("dict[str, object]", raw_annotation)
    assert annotation.get("match_tier") == "exact"
    assert "item_id" not in item
    assert "item_fingerprint" not in item
    assert "classification" not in item
    assert "refinement_provenance" not in item
    assert "source_attempt_id" not in item
    assert "source_artifact_fingerprint" not in item
    assert parsed.backlog_items[0].requirement == (
        "Validate Captain-Aware Optimization Contract"
    )
