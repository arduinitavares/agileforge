"""Tests for the direct-Specification Story runtime."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

from services import story_runtime
from services.contracts.story import UserStoryWriterInput
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
EXPECTED_REPAIR_CALL_COUNT = 2
TARGET_STORY_ID = 42


def _state() -> dict[str, Any]:
    return {
        "accepted_specification_version_id": 11,
        "accepted_specification_hash": GOLD_HASH,
        "accepted_specification_json": GOLD_PATH.read_text(encoding="utf-8"),
        "parent_backlog_item_id": "PBI-000001",
        "parent_backlog_spec_item_ids": ["DATA.001", "REQ.001"],
        "roadmap_context": json.dumps(
            {
                "release_name": "Release 1",
                "backlog_item_ids": ["PBI-000001"],
            },
            sort_keys=True,
        ),
    }


def _story_item(title: str = "Implement the accepted operation") -> dict[str, Any]:
    return {
        "story_title": title,
        "statement": (
            "As a calculator user, I want the accepted operation, so that I can "
            "obtain the specified result."
        ),
        "acceptance_criteria": ["Verify the result against DATA.001."],
        "spec_item_ids": ["DATA.001", "REQ.001"],
        "invest_score": "High",
        "estimated_effort": "S",
        "produced_artifacts": [],
        "research_caveats": [],
        "decomposition_warning": None,
        "dependency_candidates": [],
    }


def _valid_output(*, story_count: int = 1) -> str:
    return json.dumps(
        {
            "user_stories": [
                _story_item(f"Accepted operation {ordinal}")
                for ordinal in range(1, story_count + 1)
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )


def _fake_failure(**kwargs: object) -> dict[str, object]:
    details = cast("story_runtime._FailureDetails", kwargs["details"])
    return {
        "success": False,
        "failure_stage": kwargs["failure_stage"],
        "error": details.message,
    }


def test_story_input_context_preserves_complete_root_and_parent_evidence() -> None:
    """Preserve complete gold bytes and the exact Backlog evidence boundary."""
    context = story_runtime.build_story_input_context(
        _state(), current_user_input="Use only the accepted parent."
    )
    parsed = UserStoryWriterInput.model_validate(context)
    canonical_json = GOLD_PATH.read_text(encoding="utf-8")
    payload = SpecificationPayload.model_validate_json(
        parsed.accepted_specification_json
    )

    assert parsed.accepted_specification_json == canonical_json
    assert "sha256:" + hashlib.sha256(canonical_json.encode()).hexdigest() == GOLD_HASH
    assert {item.id for item in payload.items} == GOLD_ITEM_IDS
    assert "DATA.001" in GOLD_ITEM_IDS
    assert parsed.parent_backlog_item_id == "PBI-000001"
    assert parsed.parent_backlog_spec_item_ids == ("DATA.001", "REQ.001")
    dumped = parsed.model_dump(mode="json")
    assert "technical_spec" not in dumped
    assert "invariants" not in dumped


@pytest.mark.asyncio
async def test_story_runtime_uses_one_provider_call_and_no_automatic_semantic_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run one writer call without triggering an implicit semantic review."""
    observed: list[UserStoryWriterInput] = []
    semantic_calls: list[str] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        observed.append(payload)
        return _valid_output()

    semantic_module = types.ModuleType("services.story_validation")

    def forbidden_semantic_call(*_args: object, **_kwargs: object) -> None:
        semantic_calls.append("semantic")
        message = "semantic review must be explicit"
        raise AssertionError(message)

    semantic_module.__dict__["validate_story_semantics"] = forbidden_semantic_call
    monkeypatch.setitem(sys.modules, "services.story_validation", semantic_module)
    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["success"] is True
    assert len(observed) == 1
    assert observed[0].accepted_specification_json == GOLD_PATH.read_text(
        encoding="utf-8"
    )
    assert semantic_calls == []


@pytest.mark.asyncio
async def test_story_schema_repair_reuses_exact_root_without_semantic_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse the exact root during bounded schema repair and avoid semantic work."""
    observed: list[UserStoryWriterInput] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        observed.append(payload)
        return "not-json" if len(observed) == 1 else _valid_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input="Keep the reviewed boundary."
    )

    assert result["success"] is True
    assert len(observed) == EXPECTED_REPAIR_CALL_COUNT
    assert {item.accepted_specification_json for item in observed} == {
        GOLD_PATH.read_text(encoding="utf-8")
    }
    assert {item.accepted_specification_hash for item in observed} == {GOLD_HASH}
    assert "SYSTEM_FEEDBACK" in (observed[1].user_input or "")


@pytest.mark.asyncio
async def test_targeted_correction_uses_writer_output_and_keeps_story_id_host_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use one writer item while retaining the selected Story ID in the host."""
    observed: list[UserStoryWriterInput] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        observed.append(payload)
        return _valid_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_patch_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _state(),
        project_id=1,
        user_input="Replace only the host-selected Story item.",
        target_story_id=TARGET_STORY_ID,
    )

    assert result["success"] is True
    assert result["draft_kind"] == "story_correction"
    assert result["target_story_id"] == TARGET_STORY_ID
    assert len(result["output_artifact"]["user_stories"]) == 1
    provider_payload = observed[0].model_dump(mode="json")
    assert "story_id" not in provider_payload
    assert "target_refinement_slot" not in provider_payload
    assert "parent_requirement" not in provider_payload


@pytest.mark.asyncio
async def test_targeted_correction_rejects_multiple_replacement_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a targeted correction that returns more than one provider item."""
    calls = 0

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        nonlocal calls
        calls += 1
        return _valid_output(story_count=2)

    monkeypatch.setattr(story_runtime, "_invoke_story_patch_agent", fake_invoke)
    monkeypatch.setattr(story_runtime, "_failure", _fake_failure)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None, target_story_id=TARGET_STORY_ID
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "exactly one" in str(result["error"])
    assert calls == story_runtime.MAX_STORY_SCHEMA_REPAIR_ATTEMPTS


@pytest.mark.asyncio
async def test_story_input_failure_stops_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop an invalid parent evidence reference before provider execution."""
    state = _state()
    state["parent_backlog_spec_item_ids"] = ["REQ.missing"]
    calls: list[str] = []

    async def forbidden_invoke(_payload: UserStoryWriterInput) -> str:
        calls.append("provider")
        return _valid_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", forbidden_invoke)
    monkeypatch.setattr(story_runtime, "_failure", _fake_failure)

    result = await story_runtime.run_story_agent_from_state(
        state, project_id=1, user_input=None
    )

    assert result["success"] is False
    assert result["failure_stage"] == "input_validation"
    assert calls == []
