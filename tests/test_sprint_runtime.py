"""Regression tests for sprint input normalization and runtime wiring."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Never, cast

import pytest

from orchestrator_agent.fsm import deterministic_tool_adapters as adapters
from services import sprint_input, sprint_runtime
from utils import adk_runner

if TYPE_CHECKING:
    from google.adk.tools import ToolContext


class MockToolContext:
    """Minimal ToolContext stub for unit tests."""

    def __init__(self, state: object) -> None:
        """Initialize the test helper."""
        self.state = state


def _valid_sprint_output(*, max_story_points: int | None = 13) -> str:
    return json.dumps(
        {
            "sprint_goal": "Deliver onboarding-ready login flow",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "tasks": [
                        {
                            "description": "Create schema",
                            "task_kind": "design",
                            "checklist_items": [
                                "Define the event schema shape",
                                "Document the persistence boundary",
                            ],
                            "artifact_targets": ["event schema"],
                            "workstream_tags": ["persistence"],
                            "relevant_invariant_ids": ["INV-12"],
                        },
                        {
                            "description": "Write tests",
                            "task_kind": "testing",
                            "checklist_items": [
                                "Cover the persistence behavior in tests",
                            ],
                            "artifact_targets": ["unit tests"],
                            "workstream_tags": ["testing"],
                            "relevant_invariant_ids": [],
                        },
                    ],
                    "reason_for_selection": "Supports the sprint goal.",
                }
            ],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "High",
                "capacity_band": "6-7 stories",
                "selected_count": 1,
                "story_points_used": 3,
                "max_story_points": max_story_points,
                "commitment_note": "Does this scope feel achievable in 2 weeks?",
                "reasoning": "This scope fits the chosen capacity band.",
            },
        }
    )


def _sprint_output_for_story_ids(
    story_ids: list[int],
    *,
    selected_count: int | None = None,
    story_points_used: int | None = None,
    max_story_points: int | None = 10,
    deselected_stories: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "sprint_goal": "Deliver locked sprint scope",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [
                {
                    "story_id": story_id,
                    "story_title": f"Story {story_id}",
                    "tasks": [],
                    "reason_for_selection": "Supports the locked sprint scope.",
                }
                for story_id in story_ids
            ],
            "deselected_stories": deselected_stories or [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "2 stories",
                "selected_count": (
                    len(story_ids) if selected_count is None else selected_count
                ),
                "story_points_used": (
                    2 * len(story_ids)
                    if story_points_used is None
                    else story_points_used
                ),
                "max_story_points": max_story_points,
                "commitment_note": "Does this scope feel achievable in 2 weeks?",
                "reasoning": "The selected work fits the locked sprint scope.",
            },
        }
    )


def test_prepare_sprint_input_context_rejects_invalid_selected_story_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify prepare sprint input context rejects invalid selected story ids."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Attestation Gate UI",
                    "priority": 1,
                    "story_points": 5,
                }
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="High",
        sprint_duration_days=14,
        user_context="Focus on persistence",
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[999],
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_INVALID"
    assert prepared["invalid_selected_ids"] == [999]


def test_prepare_sprint_input_context_auto_selects_locked_priority_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sprint input auto-selects a locked priority prefix."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 3,
            "stories": [
                {
                    "story_id": 66,
                    "story_title": "Budget",
                    "priority": 101,
                    "story_points": 1,
                },
                {
                    "story_id": 85,
                    "story_title": "Live workflow",
                    "priority": 102,
                    "story_points": 3,
                },
                {
                    "story_id": 67,
                    "story_title": "Capture",
                    "priority": 201,
                    "story_points": 3,
                },
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=None,
    )

    assert prepared["success"] is True
    assert prepared["selected_story_ids"] == [66, 85]
    assert prepared["selection_policy"]["mode"] == "auto"
    assert [
        story["story_id"] for story in prepared["input_context"]["available_stories"]
    ] == [66, 85]
    assert prepared["input_context"]["available_stories"][0]["parent_group"] == 1
    assert prepared["input_context"]["available_stories"][0]["group_slot"] == 1


def test_prepare_sprint_input_context_returns_selection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sprint input returns structured selection errors."""
    blocking_story_id = 66

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": blocking_story_id,
                    "story_title": "Budget",
                    "priority": 101,
                    "story_points": 5,
                },
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="Low",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=3,
        include_task_decomposition=True,
        selected_story_ids=None,
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_CAPACITY_BLOCKED"
    assert prepared["selection_details"]["blocking_story_id"] == blocking_story_id


def test_load_sprint_candidates_preserves_readiness_from_fetcher() -> None:
    """Verify candidate normalization keeps readiness diagnostics."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Unsized story",
                    "priority": 999,
                    "story_points": None,
                }
            ],
            "readiness": {
                "status": "blocked",
                "unsized_count": 1,
                "default_priority_count": 1,
                "blocking_codes": [
                    "SPRINT_CANDIDATES_UNSIZED",
                    "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
                ],
                "blocking_story_ids": [11],
            },
        }

    result = sprint_input.load_sprint_candidates(
        7,
        fetch_candidates=fake_fetch_sprint_candidates,
    )

    assert result["success"] is True
    assert result["readiness"] == {
        "status": "blocked",
        "unsized_count": 1,
        "default_priority_count": 1,
        "blocking_codes": [
            "SPRINT_CANDIDATES_UNSIZED",
            "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
        ],
        "blocking_story_ids": [11],
    }


@pytest.mark.asyncio
async def test_runtime_and_adapter_build_matching_sprint_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime and adapter build matching sprint input."""
    runtime_capture = {}
    adapter_capture = {}

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Attestation Gate UI",
                    "priority": 1,
                    "story_points": 5,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": ["INV-12"],
                    "source_requirement": "REQ-44",
                },
            ],
        }

    async def fake_invoke(payload: sprint_runtime.SprintPlannerInput) -> object:
        runtime_capture["payload"] = payload.model_dump()
        return _valid_sprint_output()

    async def fake_run_async(*, args: object, tool_context: object) -> object:
        adapter_capture["args"] = args
        adapter_capture["tool_context"] = tool_context
        return {"sprint_goal": "goal", "selected_stories": [], "capacity_analysis": {}}

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(
        adapters, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)
    monkeypatch.setattr(adapters._SPRINT_PLANNER_TOOL, "run_async", fake_run_async)

    runtime_result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="high",
        sprint_duration_days=40,
        max_story_points=13,
        include_task_decomposition=False,
        selected_story_ids=[12],
        user_input="Focus on persistence",
    )
    context = MockToolContext({"active_project": {"product_id": 7}})
    _ = await adapters.sprint_planner_tool(
        team_velocity_assumption="high",
        sprint_duration_days=40,
        user_context="Focus on persistence",
        max_story_points=13,
        include_task_decomposition=False,
        selected_story_ids=[12],
        tool_context=cast("ToolContext", context),
    )

    assert runtime_result["success"] is True
    assert runtime_result["output_artifact"]["is_complete"] is True
    adapter_args = adapter_capture["args"]
    assert adapter_args["available_stories"][0]["parent_group"] is None
    assert adapter_args["available_stories"][0]["group_slot"] is None
    assert runtime_capture["payload"] == adapter_args
    assert runtime_capture["payload"] == {
        "available_stories": [
            {
                "story_id": 12,
                "story_title": "Event Delta Persistence",
                "story_description": "",
                "acceptance_criteria_items": [],
                "persona": None,
                "source_requirement": "REQ-44",
                "priority": 2,
                "story_points": 3,
                "evaluated_invariant_ids": ["INV-12"],
                "story_compliance_boundary_summaries": [],
                "parent_group": None,
                "group_slot": None,
            }
        ],
        "team_velocity_assumption": "High",
        "sprint_duration_days": 31,
        "user_context": "Focus on persistence",
        "max_story_points": 13,
        "include_task_decomposition": False,
    }


@pytest.mark.asyncio
async def test_runtime_rejects_output_that_changes_locked_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects output that changes locked selected story ids."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 3,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 99,
                    "story_title": "Out of Scope Story",
                    "priority": 3,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 99])

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["validation_errors"] == [
        "selected stories do not match locked Sprint selection: "
        "expected [12, 13], actual [12, 99]"
    ]


@pytest.mark.asyncio
async def test_runtime_rejects_output_that_drops_locked_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects output that drops locked selected story ids."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12])

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["validation_errors"] == [
        "selected stories do not match locked Sprint selection: "
        "expected [12, 13], actual [12]"
    ]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_selected_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects locked sprint selected count mismatches."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13], selected_count=3)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "capacity analysis does not match locked Sprint selection" in result["error"]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_story_points_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects locked sprint story point mismatches."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13], story_points_used=5)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "capacity analysis does not match locked Sprint selection" in result["error"]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_max_story_points_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects locked sprint max story point mismatches."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13], max_story_points=10)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=13,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "capacity analysis does not match locked Sprint selection" in result["error"]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_deselected_stories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects deselected stories for locked sprint selection."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids(
            [12, 13],
            deselected_stories=[
                {
                    "story_id": 99,
                    "reason": "Not included by the model.",
                }
            ],
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert (
        "deselected stories are not allowed for locked Sprint selection"
        in result["error"]
    )


@pytest.mark.asyncio
async def test_runtime_accepts_output_that_matches_locked_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime accepts output with exact locked selection capacity."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13])

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
    )

    assert result["success"] is True
    assert [
        story["story_id"] for story in result["output_artifact"]["selected_stories"]
    ] == [12, 13]


@pytest.mark.asyncio
async def test_runtime_rejects_out_of_scope_task_invariant_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects out of scope task invariant bindings."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _valid_sprint_output(max_story_points=None)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
    )

    assert result["success"] is False
    assert (
        result["error"]
        == "Sprint output validation failed: invalid task invariant bindings"
    )


@pytest.mark.asyncio
async def test_runtime_passes_story_acceptance_criteria_into_decomposition_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime passes story acceptance criteria into decomposition validator."""
    captured = {}

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": ["INV-12"],
                    "acceptance_criteria": "Persist the event\nSurface a success response",  # noqa: E501
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _valid_sprint_output(max_story_points=None)

    def fake_validate_task_decomposition_quality(
        _output: object,
        *,
        include_task_decomposition: object,
        has_acceptance_criteria_by_story: bool,
        acceptance_criteria_items_by_story: object = None,
    ) -> object:
        captured["include_task_decomposition"] = include_task_decomposition
        captured["has_acceptance_criteria_by_story"] = has_acceptance_criteria_by_story
        captured["acceptance_criteria_items_by_story"] = (
            acceptance_criteria_items_by_story
        )
        return []

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)
    monkeypatch.setattr(
        sprint_runtime,
        "validate_task_decomposition_quality",
        fake_validate_task_decomposition_quality,
    )

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
    )

    assert result["success"] is True
    assert captured["include_task_decomposition"] is True
    assert captured["has_acceptance_criteria_by_story"] == {12: True}
    assert captured["acceptance_criteria_items_by_story"] == {
        12: ["Persist the event", "Surface a success response"]
    }


@pytest.mark.asyncio
async def test_runtime_rejects_poor_task_decomposition_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects poor task decomposition quality."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                    "acceptance_criteria": "Persist the event\nSurface a success response",  # noqa: E501
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return json.dumps(
            {
                "sprint_goal": "goal",
                "sprint_number": 1,
                "duration_days": 14,
                "selected_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "tasks": [
                            {
                                "description": "Do the work",
                                "task_kind": "implementation",
                                "checklist_items": ["Persist the event"],
                                "artifact_targets": ["event persistence service"],
                                "workstream_tags": ["backend"],
                                "relevant_invariant_ids": [],
                            }
                        ],
                        "reason_for_selection": "reason",
                    }
                ],
                "deselected_stories": [],
                "capacity_analysis": {
                    "velocity_assumption": "High",
                    "capacity_band": "6-7 stories",
                    "selected_count": 1,
                    "story_points_used": 3,
                    "max_story_points": None,
                    "commitment_note": "Valid note",
                    "reasoning": "Valid reasoning",
                },
            }
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
    )

    assert result["success"] is False
    assert (
        result["error"]
        == "Sprint output validation failed: poor task decomposition quality"
    )


@pytest.mark.asyncio
async def test_runtime_exposes_compact_public_task_kind_retry_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime exposes compact public task kind retry hints."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return json.dumps(
            {
                "sprint_goal": "goal",
                "sprint_number": 1,
                "duration_days": 14,
                "selected_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "tasks": [
                            {
                                "description": "Get approval",
                                "task_kind": "approval",
                                "checklist_items": ["Confirm the change can proceed"],
                                "artifact_targets": ["approval decision"],
                                "workstream_tags": ["governance"],
                                "relevant_invariant_ids": [],
                            }
                        ],
                        "reason_for_selection": "reason",
                    }
                ],
                "deselected_stories": [],
                "capacity_analysis": {
                    "velocity_assumption": "Medium",
                    "capacity_band": "4-5 stories",
                    "selected_count": 1,
                    "story_points_used": 3,
                    "max_story_points": 13,
                    "commitment_note": "Does this scope feel achievable in 2 weeks?",
                    "reasoning": "Fits the chosen capacity.",
                },
            }
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["output_artifact"]["validation_errors"] == [
        "Task 'Get approval' uses unsupported task_kind 'approval'. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]


@pytest.mark.asyncio
async def test_runtime_uses_canonical_public_hint_for_non_string_task_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime uses canonical public hint for non string task kind."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return json.dumps(
            {
                "sprint_goal": "goal",
                "sprint_number": 1,
                "duration_days": 14,
                "selected_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "tasks": [
                            {
                                "description": "Get approval",
                                "task_kind": None,
                                "checklist_items": ["Confirm the change can proceed"],
                                "artifact_targets": ["approval decision"],
                                "workstream_tags": ["governance"],
                                "relevant_invariant_ids": [],
                            }
                        ],
                        "reason_for_selection": "reason",
                    }
                ],
                "deselected_stories": [],
                "capacity_analysis": {
                    "velocity_assumption": "Medium",
                    "capacity_band": "4-5 stories",
                    "selected_count": 1,
                    "story_points_used": 3,
                    "max_story_points": 13,
                    "commitment_note": "Does this scope feel achievable in 2 weeks?",
                    "reasoning": "Fits the chosen capacity.",
                },
            }
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["output_artifact"]["validation_errors"] == [
        "Task 'Get approval' has invalid task_kind. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]
    assert "other" not in result["output_artifact"]["validation_errors"][0]


@pytest.mark.asyncio
async def test_adk_runner_preserves_structured_validation_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify adk runner preserves structured validation details."""
    structured_errors = [
        {
            "type": "literal_error",
            "loc": ("selected_stories", 0, "tasks", 0, "task_kind"),
            "msg": "Input should be 'analysis' or 'design'",
            "input": "approval",
        }
    ]

    class FakeSessionService:
        async def create_session(self, *, app_name: object, user_id: int) -> object:
            del app_name, user_id
            return SimpleNamespace(id="session-1")

    class FakeRunner:
        def __init__(
            self, *, agent: object, app_name: object, session_service: object
        ) -> None:
            self.agent = agent
            self.app_name = app_name
            self.session_service = session_service

        async def run_async(
            self, *, user_id: int, session_id: int, new_message: object
        ) -> object:
            _ = (user_id, session_id, new_message)

            class FakeStructuredValidationError(Exception):
                def errors(self) -> object:
                    return structured_errors

            msg = "ADK validation failed"
            raise RuntimeError(msg) from FakeStructuredValidationError()
            yield None

    class FakePart:
        @staticmethod
        def from_text(*, text: object) -> object:
            return SimpleNamespace(text=text)

    class FakeContent:
        def __init__(self, *, role: object, parts: object) -> None:
            self.role = role
            self.parts = parts

    monkeypatch.setattr(adk_runner, "InMemorySessionService", FakeSessionService)
    monkeypatch.setattr(adk_runner, "Runner", FakeRunner)
    monkeypatch.setattr(
        adk_runner,
        "types",
        SimpleNamespace(Content=FakeContent, Part=FakePart),
    )

    with pytest.raises(adk_runner.AgentInvocationError) as exc_info:
        await adk_runner.invoke_agent_to_text(
            agent=SimpleNamespace(name="sprint"),
            runner_identity=SimpleNamespace(app_name="app", user_id="user"),
            payload_json="{}",
            no_text_error="missing",
        )

    assert exc_info.value.validation_errors == structured_errors


@pytest.mark.asyncio
async def test_runtime_falls_back_to_public_hint_for_adk_task_kind_errors_without_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime falls back to public hint for adk task kind errors without input."""  # noqa: E501

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> Never:
        msg = "ADK validation failed"
        raise adk_runner.AgentInvocationError(
            msg,
            validation_errors=[
                {
                    "type": "missing",
                    "loc": ("selected_stories", 0, "tasks", 0, "task_kind"),
                    "msg": "Field required",
                }
            ],
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="medium",
        sprint_duration_days=14,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "invocation_exception"
    assert result["output_artifact"]["validation_errors"] == [
        "Task has invalid task_kind. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]
