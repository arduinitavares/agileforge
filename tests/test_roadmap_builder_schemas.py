"""Schema tests for Roadmap Builder agent contracts."""

from __future__ import annotations

from orchestrator_agent.agent_tools.roadmap_builder.schemes import RoadmapBuilderInput


def test_roadmap_input_accepts_enriched_backlog_items() -> None:
    """Roadmap Builder must accept Backlog Primer brownfield metadata."""
    parsed = RoadmapBuilderInput.model_validate(
        {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Validate Captain-Aware Optimizer Contract",
                    "capability_name": "Captain-Aware Squad Optimizer",
                    "authority_ref": "REQ.captain-aware-optimization",
                    "as_built_status": "observed_with_missing_evidence",
                    "recommended_backlog_treatment": "create_verification_item",
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
    assert item.capability_name == "Captain-Aware Squad Optimizer"
    assert item.authority_ref == "REQ.captain-aware-optimization"
    assert item.as_built_status == "observed_with_missing_evidence"
    assert item.recommended_backlog_treatment == "create_verification_item"
