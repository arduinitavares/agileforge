"""Schema tests for Roadmap Builder agent contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.roadmap import RoadmapBuilderInput


def test_roadmap_input_rejects_retired_annotations_and_keeps_semantic_fields() -> None:
    """Roadmap input accepts only current Backlog and lifecycle context."""
    payload = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Validate Captain-Aware Optimizer Contract",
                "authority_ref": "REQ.captain-aware-optimization",
                "capability_hint": "Captain-Aware Squad Optimizer",
                "value_driver": "Strategic",
                "justification": "The accepted Goal prioritizes this behavior.",
                "estimated_effort": "M",
                "technical_note": "Validate existing captain multiplier behavior.",
            }
        ],
        "product_vision": "For operators who need safe live recommendations.",
        "technical_spec": "Spec content",
        "compiled_authority": '{"invariants":[]}',
    }
    retired_payload = {
        **payload,
        "backlog_items": [
            {
                **payload["backlog_items"][0],
                "as_built_annotation": {"match_tier": "exact"},
            }
        ],
    }

    with pytest.raises(ValidationError):
        RoadmapBuilderInput.model_validate(retired_payload)
    parsed = RoadmapBuilderInput.model_validate(payload)

    item = parsed.backlog_items[0]
    assert item.authority_ref == "REQ.captain-aware-optimization"
    assert item.capability_hint == "Captain-Aware Squad Optimizer"
    assert set(item.model_fields_set) == {
        "priority",
        "requirement",
        "authority_ref",
        "capability_hint",
        "value_driver",
        "justification",
        "estimated_effort",
        "technical_note",
    }
