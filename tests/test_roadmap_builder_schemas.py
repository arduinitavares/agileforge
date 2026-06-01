"""Schema tests for Roadmap Builder agent contracts."""

from __future__ import annotations

from orchestrator_agent.agent_tools.roadmap_builder.schemes import RoadmapBuilderInput
from utils.brownfield_annotations import BrownfieldAnnotation


def test_roadmap_input_accepts_host_derived_brownfield_annotations() -> None:
    """Roadmap Builder accepts host-derived brownfield annotations."""
    annotation = BrownfieldAnnotation(
        schema_version="agileforge.brownfield_annotation.v1",
        match_tier="exact",
        match_basis=["authority_ref"],
    )

    parsed = RoadmapBuilderInput.model_validate(
        {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Validate Captain-Aware Optimizer Contract",
                    "authority_ref": "REQ.captain-aware-optimization",
                    "capability_hint": "Captain-Aware Squad Optimizer",
                    "as_built_annotation": annotation,
                    "value_driver": "Strategic",
                    "justification": "As-Built evidence indicates existing behavior.",
                    "estimated_effort": "M",
                    "technical_note": "Validate existing captain multiplier behavior.",
                }
            ],
            "product_vision": "For operators who need safe live recommendations.",
            "technical_spec": "Spec content",
            "compiled_authority": '{"invariants":[]}',
        }
    )

    item = parsed.backlog_items[0]
    assert item.authority_ref == "REQ.captain-aware-optimization"
    assert item.capability_hint == "Captain-Aware Squad Optimizer"
    assert item.as_built_annotation == annotation

    assert "capability_name" not in item.model_fields_set
    assert "as_built_status" not in item.model_fields_set
    assert "recommended_backlog_treatment" not in item.model_fields_set
