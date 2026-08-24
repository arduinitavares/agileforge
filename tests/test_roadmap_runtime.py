"""Tests for direct-Specification Roadmap runtime projection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from services import roadmap_runtime
from services.contracts.backlog import BacklogItem
from services.contracts.roadmap import RoadmapBuilderInput
from utils.agileforge_spec_profile_v2 import SpecificationPayload

GOLD_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "issue_210"
    / "gold"
    / "canonical-specification.json"
)
GOLD_HASH = "sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
GOLD_ITEM_IDS = {
    "ASSUMPTION.001",
    "CONSTRAINT.001",
    "CONSTRAINT.002",
    "DATA.001",
    "DATA.002",
    "DECISION.001",
    "DECISION.002",
    "DECISION.003",
    "EXAMPLE.001",
    "GOAL.001",
    "GOAL.002",
    "INTERFACE.001",
    "INTERFACE.002",
    "NON_GOAL.001",
    "NON_GOAL.002",
    "NON_GOAL.003",
    "NON_GOAL.004",
    "OPEN_QUESTION.001",
    "QUALITY.001",
    "REQ.001",
    "REQ.002",
    "REQ.003",
    "REQ.004",
    "REQ.005",
    "REQ.006",
    "REQ.007",
    "REQ.008",
    "REQ.009",
    "REQ.010",
    "REQ.011",
    "REQ.012",
    "REQ.013",
    "REQ.014",
    "REQ.015",
    "RISK.001",
    "RISK.002",
    "RISK.003",
}


def _state() -> dict[str, Any]:
    return {
        "accepted_specification_version_id": 11,
        "accepted_specification_hash": GOLD_HASH,
        "accepted_specification_json": GOLD_PATH.read_text(encoding="utf-8"),
        "backlog_items": [
            BacklogItem(
                backlog_item_id="PBI-000001",
                priority=1,
                requirement="Implement the accepted calculator operation",
                spec_item_ids=("DATA.001", "REQ.001"),
                value_driver="Strategic",
                justification="It realizes the accepted first release.",
                estimated_effort="M",
            ).model_dump(mode="json")
        ],
        "product_vision": "Ship one bounded calculator release.",
        "prior_roadmap_state": json.dumps(
            {"roadmap_releases": ["Prior reviewed state"]},
            sort_keys=True,
        ),
    }


def _valid_output() -> str:
    return json.dumps(
        {
            "roadmap_releases": [
                {
                    "release_name": "Release 1",
                    "theme": "Accepted calculator scope",
                    "focus_area": "User Value",
                    "backlog_item_ids": ["PBI-000001"],
                    "reasoning": "Deliver the accepted parent first.",
                }
            ],
            "roadmap_summary": "One exact reviewed release.",
            "is_complete": True,
            "clarifying_questions": [],
        }
    )


def test_build_roadmap_input_context_preserves_complete_root_and_exact_parent() -> None:
    """Preserve the complete gold root and exact immutable Backlog parent."""
    context = roadmap_runtime.build_roadmap_input_context(
        _state(), user_input="Keep the accepted parent boundary."
    )
    parsed = RoadmapBuilderInput.model_validate(context)
    canonical_json = GOLD_PATH.read_text(encoding="utf-8")
    payload = SpecificationPayload.model_validate_json(
        parsed.accepted_specification_json
    )

    assert parsed.accepted_specification_json == canonical_json
    assert "sha256:" + hashlib.sha256(canonical_json.encode()).hexdigest() == GOLD_HASH
    assert {item.id for item in payload.items} == GOLD_ITEM_IDS
    assert "DATA.001" in GOLD_ITEM_IDS
    assert parsed.backlog_items[0].backlog_item_id == "PBI-000001"
    assert parsed.backlog_items[0].spec_item_ids == ("DATA.001", "REQ.001")
    dumped = parsed.model_dump(mode="json")
    assert "technical_spec" not in dumped
    assert "compiled_authority" not in dumped
    assert "compiled_authority_cached" not in dumped


@pytest.mark.asyncio
async def test_roadmap_runtime_invokes_same_contract_and_validates_parent_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use one production contract and require complete parent coverage."""
    observed: list[RoadmapBuilderInput] = []

    async def fake_invoke(payload: RoadmapBuilderInput) -> str:
        observed.append(payload)
        return _valid_output()

    monkeypatch.setattr(roadmap_runtime, "_invoke_roadmap_agent", fake_invoke)

    result = await roadmap_runtime.run_roadmap_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["success"] is True
    assert observed == [RoadmapBuilderInput.model_validate(_state())]
    assert result["output_artifact"]["roadmap_releases"][0]["backlog_item_ids"] == [
        "PBI-000001"
    ]


@pytest.mark.asyncio
async def test_roadmap_runtime_rejects_unknown_output_parent_via_failure_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route an unknown provider-owned parent through normal output failure."""

    async def fake_invoke(_payload: RoadmapBuilderInput) -> str:
        return _valid_output().replace("PBI-000001", "PBI-000002")

    def fake_failure(**kwargs: object) -> dict[str, object]:
        details = cast("roadmap_runtime._FailureDetails", kwargs["details"])
        return {
            "success": False,
            "failure_stage": kwargs["failure_stage"],
            "error": details.message,
        }

    monkeypatch.setattr(roadmap_runtime, "_invoke_roadmap_agent", fake_invoke)
    monkeypatch.setattr(roadmap_runtime, "_failure", fake_failure)

    result = await roadmap_runtime.run_roadmap_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "unknown backlog item ID" in str(result["error"])
