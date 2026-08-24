"""Tests for stable Backlog-item Roadmap references."""
# ruff: noqa: D103

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts import roadmap as roadmap_contracts
from services.contracts.backlog import BacklogItem
from services.contracts.roadmap import RoadmapBuilderOutput, RoadmapRelease
from utils.agileforge_spec_profile_v2 import SpecificationPayload

GOLD_SPECIFICATION_PATH = (
    Path(__file__).parents[2]
    / "fixtures"
    / "issue_210"
    / "gold"
    / "canonical-specification.json"
)
GOLD_SPECIFICATION_HASH = (
    "sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
)
GOLD_SPECIFICATION_ITEM_IDS = {
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


def test_roadmap_requires_each_exact_backlog_item_once() -> None:
    roadmap = RoadmapBuilderOutput(
        roadmap_releases=(
            RoadmapRelease(
                release_name="First",
                theme="Foundation",
                focus_area="Technical Foundation",
                backlog_item_ids=("PBI-000002",),
                reasoning="It removes the first dependency.",
            ),
            RoadmapRelease(
                release_name="Second",
                theme="Value",
                focus_area="User Value",
                backlog_item_ids=("PBI-000001",),
                reasoning="It unlocks user value.",
            ),
        ),
        roadmap_summary="Two releases.",
        is_complete=True,
    )

    roadmap_contracts.validate_roadmap_backlog_coverage(
        roadmap, ("PBI-000001", "PBI-000002")
    )


def test_roadmap_rejects_unknown_or_repeated_backlog_ids() -> None:
    roadmap = RoadmapBuilderOutput(
        roadmap_releases=(
            RoadmapRelease(
                release_name="Only",
                theme="Foundation",
                focus_area="Technical Foundation",
                backlog_item_ids=("PBI-000001", "PBI-000001", "PBI-999999"),
                reasoning="A deliberately invalid mapping.",
            ),
        ),
        roadmap_summary="Invalid.",
        is_complete=False,
    )

    with pytest.raises(ValueError, match="duplicate backlog item ID"):
        roadmap_contracts.validate_roadmap_backlog_coverage(roadmap, ("PBI-000001",))


def test_roadmap_release_rejects_impossible_backlog_id_and_blank_content() -> None:
    payload = {
        "release_name": "First",
        "theme": "Foundation",
        "focus_area": "Technical Foundation",
        "backlog_item_ids": ("PBI-000001",),
        "reasoning": "It establishes the foundation.",
    }

    assert RoadmapRelease.model_validate(payload)

    with pytest.raises(ValidationError, match="Backlog item ID"):
        RoadmapRelease.model_validate({**payload, "backlog_item_ids": ("PBI-000000",)})
    with pytest.raises(ValidationError, match="must not be blank"):
        RoadmapRelease.model_validate({**payload, "theme": " \u2003"})


def _gold_roadmap_input_payload() -> dict[str, object]:
    return {
        "accepted_specification_version_id": 11,
        "accepted_specification_hash": GOLD_SPECIFICATION_HASH,
        "accepted_specification_json": GOLD_SPECIFICATION_PATH.read_text(
            encoding="utf-8"
        ),
        "backlog_items": (
            BacklogItem(
                backlog_item_id="PBI-000001",
                priority=1,
                requirement="Implement the accepted calculator operation",
                spec_item_ids=("DATA.001", "REQ.001"),
                value_driver="Strategic",
                justification="It realizes the accepted first release.",
                estimated_effort="M",
            ),
        ),
        "product_vision": "Ship one bounded calculator release.",
        "time_increment": "Milestone-based",
        "prior_roadmap_state": "NO_HISTORY",
        "user_input": "",
    }


def test_roadmap_builder_input_preserves_complete_gold_root_and_parent() -> None:
    root = roadmap_contracts.RoadmapBuilderInput.model_validate(
        _gold_roadmap_input_payload()
    )
    canonical_json = GOLD_SPECIFICATION_PATH.read_text(encoding="utf-8")
    parsed = SpecificationPayload.model_validate_json(root.accepted_specification_json)

    assert root.accepted_specification_json == canonical_json
    assert len(canonical_json.encode("utf-8")) == 36220  # noqa: PLR2004
    assert "sha256:" + hashlib.sha256(canonical_json.encode()).hexdigest() == (
        GOLD_SPECIFICATION_HASH
    )
    assert {item.id for item in parsed.items} == GOLD_SPECIFICATION_ITEM_IDS
    assert "DATA.001" in GOLD_SPECIFICATION_ITEM_IDS
    assert root.backlog_items[0].backlog_item_id == "PBI-000001"
    assert root.backlog_items[0].spec_item_ids == ("DATA.001", "REQ.001")


@pytest.mark.parametrize("retired_field", ["technical_spec"])
def test_roadmap_builder_input_rejects_retired_duplicate_roots(
    retired_field: str,
) -> None:
    payload = {**_gold_roadmap_input_payload(), retired_field: "retired duplicate"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        roadmap_contracts.RoadmapBuilderInput.model_validate(payload)


def test_roadmap_builder_input_rejects_unknown_parent_evidence() -> None:
    payload = _gold_roadmap_input_payload()
    parent = payload["backlog_items"]
    assert isinstance(parent, tuple)
    assert isinstance(parent[0], BacklogItem)
    payload["backlog_items"] = (
        parent[0].model_copy(update={"spec_item_ids": ("REQ.missing",)}),
    )

    with pytest.raises(ValidationError, match="unknown Specification item ID"):
        roadmap_contracts.RoadmapBuilderInput.model_validate(payload)
