"""Tests for provider Backlog output and host-minted Backlog items."""
# ruff: noqa: D103

import hashlib
from itertools import permutations
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts import backlog as backlog_contracts
from services.contracts.backlog import BacklogAgentItem, BacklogItem
from services.contracts.specification_references import AcceptedSpecificationReference
from utils.agileforge_spec_profile_v2 import (
    RequirementLevel,
    SpecificationItem,
    SpecificationPayload,
    SpecItemType,
    VerificationMethod,
    canonical_spec_hash,
    canonical_spec_json,
)

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


def _reference() -> AcceptedSpecificationReference:
    payload = SpecificationPayload(
        artifact_id="SPEC.backlog-contract",
        title="Backlog contract",
        summary="Host mints IDs",
        problem_statement="Provider output must not own durable identifiers.",
        items=(
            SpecificationItem(
                id="REQ.alpha",
                type=SpecItemType.REQ,
                title="Alpha",
                statement="The system must support alpha.",
                level=RequirementLevel.MUST,
                verification=VerificationMethod.UNIT_TEST,
                acceptance=("Alpha passes.",),
            ),
        ),
    )
    return AcceptedSpecificationReference(
        spec_version_id=3,
        spec_hash=canonical_spec_hash(payload),
        canonical_specification_json=canonical_spec_json(payload),
        payload=payload,
    )


def _agent_item(priority: int, requirement: str) -> BacklogAgentItem:
    return BacklogAgentItem(
        priority=priority,
        requirement=requirement,
        spec_item_ids=("REQ.alpha",),
        value_driver="Strategic",
        justification="It establishes the delivery contract.",
        estimated_effort="S",
    )


def test_backlog_canonicalization_sorts_then_mints_host_ids() -> None:
    items = backlog_contracts.canonicalize_backlog_items(
        _reference(),
        [_agent_item(2, "Second item"), _agent_item(1, "First item")],
    )

    assert [(item.backlog_item_id, item.requirement) for item in items] == [
        ("PBI-000001", "First item"),
        ("PBI-000002", "Second item"),
    ]


def test_backlog_item_ids_are_independent_of_provider_order() -> None:
    for priorities in permutations((1, 2, 3)):
        items = backlog_contracts.canonicalize_backlog_items(
            _reference(),
            [
                _agent_item(priority, f"Requirement {priority}")
                for priority in priorities
            ],
        )

        assert [item.backlog_item_id for item in items] == [
            "PBI-000001",
            "PBI-000002",
            "PBI-000003",
        ]
        assert [item.priority for item in items] == [1, 2, 3]


def test_backlog_rejects_duplicate_normalized_requirement_text() -> None:
    with pytest.raises(ValueError, match="duplicate normalized requirement"):
        backlog_contracts.canonicalize_backlog_items(
            _reference(),
            [_agent_item(1, "Build\u2003Thing!"), _agent_item(2, "build thing!")],
        )


def test_host_backlog_item_rejects_impossible_id_and_noncanonical_evidence() -> None:
    payload = _agent_item(1, "Build the first item").model_dump()
    host_payload = {**payload, "backlog_item_id": "PBI-000001"}

    assert BacklogItem.model_validate(host_payload)
    assert BacklogItem.model_validate({**host_payload, "backlog_item_id": "PBI-999999"})

    with pytest.raises(ValidationError, match="Backlog item ID"):
        BacklogItem.model_validate({**host_payload, "backlog_item_id": "PBI-000000"})
    with pytest.raises(ValidationError, match="Specification item IDs"):
        BacklogItem.model_validate({**host_payload, "spec_item_ids": ()})


def test_backlog_builder_input_preserves_complete_gold_root() -> None:
    canonical_json = GOLD_SPECIFICATION_PATH.read_text(encoding="utf-8")

    root = backlog_contracts.BacklogBuilderInput.model_validate(
        {
            "accepted_specification_version_id": 11,
            "accepted_specification_hash": GOLD_SPECIFICATION_HASH,
            "accepted_specification_json": canonical_json,
            "product_vision_statement": "Ship one bounded calculator release.",
            "product_goal_statement": "Deliver the accepted first release.",
            "prior_backlog_state": "NO_HISTORY",
            "user_input": None,
        }
    )

    assert root.accepted_specification_json == canonical_json
    assert len(canonical_json.encode("utf-8")) == 36220  # noqa: PLR2004
    assert "sha256:" + hashlib.sha256(canonical_json.encode()).hexdigest() == (
        GOLD_SPECIFICATION_HASH
    )
    parsed = SpecificationPayload.model_validate_json(root.accepted_specification_json)
    assert {item.id for item in parsed.items} == GOLD_SPECIFICATION_ITEM_IDS
    assert "DATA.001" in GOLD_SPECIFICATION_ITEM_IDS


@pytest.mark.parametrize("retired_field", ["technical_spec"])
def test_backlog_builder_input_rejects_retired_duplicate_roots(
    retired_field: str,
) -> None:
    canonical_json = GOLD_SPECIFICATION_PATH.read_text(encoding="utf-8")
    payload = {
        "accepted_specification_version_id": 11,
        "accepted_specification_hash": GOLD_SPECIFICATION_HASH,
        "accepted_specification_json": canonical_json,
        "product_vision_statement": "Ship one bounded calculator release.",
        "product_goal_statement": "Deliver the accepted first release.",
        "prior_backlog_state": "NO_HISTORY",
        "user_input": None,
        retired_field: "retired duplicate",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        backlog_contracts.BacklogBuilderInput.model_validate(payload)


def test_backlog_builder_input_rejects_corrupt_gold_identity() -> None:
    canonical_json = GOLD_SPECIFICATION_PATH.read_text(encoding="utf-8")

    with pytest.raises(ValidationError, match="accepted Specification hash"):
        backlog_contracts.BacklogBuilderInput.model_validate(
            {
                "accepted_specification_version_id": 11,
                "accepted_specification_hash": "sha256:" + "0" * 64,
                "accepted_specification_json": canonical_json,
                "product_vision_statement": "Ship one bounded calculator release.",
                "product_goal_statement": "Deliver the accepted first release.",
                "prior_backlog_state": "NO_HISTORY",
                "user_input": None,
            }
        )
