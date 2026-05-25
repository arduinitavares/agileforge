"""Tests for deterministic context-injection adapters."""

from __future__ import annotations

import inspect
import json

import pytest

from orchestrator_agent.fsm import deterministic_tool_adapters as adapters
from orchestrator_agent.fsm.definitions import STATE_REGISTRY
from orchestrator_agent.fsm.states import OrchestratorState
from tests.typing_helpers import make_tool_context


@pytest.mark.asyncio
async def test_product_vision_adapter_injects_state_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify product vision adapter injects state verbatim."""
    captured = {}

    async def fake_run_async(*, args: object, tool_context: object) -> object:
        captured["args"] = args
        captured["tool_context"] = tool_context
        return {
            "updated_components": {},
            "product_vision_statement": "draft",
            "is_complete": False,
            "clarifying_questions": [],
        }

    monkeypatch.setattr(adapters._PRODUCT_VISION_TOOL, "run_async", fake_run_async)

    state: dict[str, object] = {
        "pending_spec_content": "SPEC_RAW",
        "compiled_authority_cached": '{"invariants":["x"]}',
        "vision_components": {"project_name": "Demo"},
    }
    context = make_tool_context(state)

    result = await adapters.product_vision_tool(
        user_raw_text="refine the vision",
        tool_context=context,
    )

    assert result["is_complete"] is False
    assert captured["tool_context"] is context
    assert captured["args"]["user_raw_text"] == "refine the vision"
    assert captured["args"]["specification_content"] == "SPEC_RAW"
    assert captured["args"]["compiled_authority"] == '{"invariants":["x"]}'
    assert captured["args"]["prior_vision_state"] == json.dumps(
        {"project_name": "Demo"},
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_backlog_adapter_fail_fast_on_missing_required_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify backlog adapter fail fast on missing required context."""

    async def fail_if_called(*, args: object, tool_context: object) -> None:
        del args, tool_context
        pytest.fail("Sub-agent should not run when context is missing")

    monkeypatch.setattr(adapters._BACKLOG_PRIMER_TOOL, "run_async", fail_if_called)

    context = make_tool_context(
        {
            "active_project": {"vision": ""},
            "pending_spec_content": "",
            "compiled_authority_cached": "",
        }
    )

    result = await adapters.backlog_primer_tool(
        user_input="generate backlog",
        tool_context=context,
    )

    assert result["is_complete"] is False
    assert result["error"] == "BACKLOG_CONTEXT_MISSING"
    assert "active_project.vision" in result["missing_context"]
    assert "pending_spec_content" in result["missing_context"]
    assert "compiled_authority_cached" in result["missing_context"]


@pytest.mark.asyncio
async def test_roadmap_adapter_passes_full_payload_without_summarization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify roadmap adapter passes full payload without summarization."""
    captured = {}

    async def fake_run_async(*, args: object, tool_context: object) -> object:
        del tool_context
        captured["args"] = args
        return {
            "roadmap_releases": [],
            "roadmap_summary": "ok",
            "is_complete": False,
            "clarifying_questions": [],
        }

    monkeypatch.setattr(adapters._ROADMAP_BUILDER_TOOL, "run_async", fake_run_async)

    long_spec = "# Spec\n" + ("A" * 9000)
    long_authority = '{"scope_themes":["x"],"invariants":["' + ("B" * 3500) + '"]}'
    backlog_items = [
        {
            "priority": 1,
            "requirement": "Data Usage Eligibility Gate",
            "value_driver": "Strategic",
            "justification": "Mandatory legal gate",
            "estimated_effort": "M",
        }
    ]
    context = make_tool_context(
        {
            "active_project": {"vision": "Vision text"},
            "approved_backlog": {"items": backlog_items},
            "pending_spec_content": long_spec,
            "compiled_authority_cached": long_authority,
        }
    )

    _ = await adapters.roadmap_builder_tool(
        user_input="create roadmap",
        tool_context=context,
    )

    assert captured["args"]["product_vision"] == "Vision text"
    assert captured["args"]["backlog_items"] == backlog_items
    assert captured["args"]["technical_spec"] == long_spec
    assert captured["args"]["compiled_authority"] == long_authority
    assert len(captured["args"]["technical_spec"]) == len(long_spec)
    assert len(captured["args"]["compiled_authority"]) == len(long_authority)


@pytest.mark.asyncio
async def test_story_adapter_derives_requirement_context_from_roadmap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify story adapter derives requirement context from roadmap."""
    captured = {}

    async def fake_run_async(
        *, args: dict[str, object], tool_context: object
    ) -> object:
        del tool_context
        captured["args"] = args
        return {
            "parent_requirement": args["parent_requirement"],
            "user_stories": [
                {
                    "story_title": "Title",
                    "statement": "As a reviewer, I want X, so that Y.",
                    "acceptance_criteria": ["Verify that X."],
                    "invest_score": "High",
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }

    monkeypatch.setattr(adapters._USER_STORY_WRITER_TOOL, "run_async", fake_run_async)

    roadmap = {
        "roadmap_releases": [
            {
                "release_name": "Milestone 1",
                "theme": "Legal Safety",
                "focus_area": "Technical Foundation",
                "items": ["Data Usage Eligibility Gate (mandatory pre-persist)"],
                "reasoning": "Gate must block persistence before attestations.",
            }
        ]
    }
    context = make_tool_context(
        {
            "active_project": {"roadmap": roadmap},
            "pending_spec_content": "SPEC_TEXT",
            "compiled_authority_cached": '{"invariants":[]}',
        }
    )

    await adapters.user_story_writer_tool(
        parent_requirement="Data Usage Eligibility Gate (mandatory pre-persist)",
        requirement_context="MANUAL_CONTEXT_SHOULD_NOT_OVERRIDE_DERIVED",
        tool_context=context,
    )

    derived = captured["args"]["requirement_context"]
    assert "Theme: Legal Safety" in derived
    assert "Focus Area: Technical Foundation" in derived
    assert "Reasoning: Gate must block persistence before attestations." in derived


@pytest.mark.asyncio
async def test_story_adapter_fail_fast_without_derived_or_explicit_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify story adapter fail fast without derived or explicit context."""

    async def fail_if_called(*, args: object, tool_context: object) -> None:
        del args, tool_context
        pytest.fail("Sub-agent should not run when requirement context is unavailable")

    monkeypatch.setattr(adapters._USER_STORY_WRITER_TOOL, "run_async", fail_if_called)

    context = make_tool_context(
        {
            "pending_spec_content": "SPEC_TEXT",
            "compiled_authority_cached": '{"invariants":[]}',
            # No roadmap in active_project and no roadmap_result
        }
    )

    result = await adapters.user_story_writer_tool(
        parent_requirement="Data Usage Eligibility Gate (mandatory pre-persist)",
        requirement_context="",
        tool_context=context,
    )

    assert result["is_complete"] is False
    assert result["error"] == "STORY_CONTEXT_MISSING"
    assert (
        "roadmap release metadata (or explicit requirement_context)"
        in result["missing_context"]
    )


@pytest.mark.asyncio
async def test_sprint_adapter_injects_refined_candidates_from_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sprint adapter injects refined candidates from state."""
    captured = {}

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
                },
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "source_requirement": "REQ-23",
                },
            ],
        }

    async def fake_run_async(*, args: object, tool_context: object) -> object:
        captured["args"] = args
        captured["tool_context"] = tool_context
        return {"sprint_goal": "goal", "selected_stories": [], "capacity_analysis": {}}

    monkeypatch.setattr(
        adapters, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(adapters._SPRINT_PLANNER_TOOL, "run_async", fake_run_async)

    context = make_tool_context({"active_project": {"product_id": 7}})
    _ = await adapters.sprint_planner_tool(
        team_velocity_assumption="high",
        sprint_duration_days=40,
        selected_story_ids=[12],
        include_task_decomposition=False,
        tool_context=context,
    )

    assert captured["tool_context"] is context
    assert captured["args"]["team_velocity_assumption"] == "High"
    assert captured["args"]["sprint_duration_days"] == 31  # noqa: PLR2004
    assert captured["args"]["include_task_decomposition"] is False
    assert captured["args"]["available_stories"] == [
        {
            "story_id": 12,
            "story_title": "Event Delta Persistence",
            "story_description": "",
            "acceptance_criteria_items": [],
            "persona": None,
            "source_requirement": "REQ-23",
            "priority": 2,
            "parent_group": None,
            "group_slot": None,
            "story_points": 3,
            "evaluated_invariant_ids": [],
            "story_compliance_boundary_summaries": [],
            "prerequisite_story_ids": [],
            "blocked_by_story_ids": [],
            "dependency_status": "ready",
        }
    ]


@pytest.mark.asyncio
async def test_sprint_adapter_fail_fast_without_active_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sprint adapter fail fast without active project."""

    async def fail_if_called(*, args: object, tool_context: object) -> None:
        del args, tool_context
        pytest.fail("Sub-agent should not run when active project is missing")

    monkeypatch.setattr(adapters._SPRINT_PLANNER_TOOL, "run_async", fail_if_called)

    context = make_tool_context({})
    result = await adapters.sprint_planner_tool(tool_context=context)

    assert result["is_complete"] is False
    assert result["error"] == "SPRINT_CONTEXT_MISSING"
    assert "active_project.product_id" in result["missing_context"]


@pytest.mark.asyncio
async def test_sprint_adapter_rejects_non_eligible_selected_story_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sprint adapter rejects non eligible selected story ids."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        del product_id
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 22,
                    "story_title": "Refined Story",
                    "priority": 1,
                    "story_points": 3,
                }
            ],
        }

    async def fail_if_called(*, args: object, tool_context: object) -> None:
        del args, tool_context
        pytest.fail("Sub-agent should not run when selection is invalid")

    monkeypatch.setattr(
        adapters, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(adapters._SPRINT_PLANNER_TOOL, "run_async", fail_if_called)

    context = make_tool_context({"active_project": {"product_id": 1}})
    result = await adapters.sprint_planner_tool(
        selected_story_ids=[999],
        tool_context=context,
    )

    assert result["is_complete"] is False
    assert result["error"] == "SPRINT_SELECTION_INVALID"


def test_state_registry_keeps_public_generation_tool_names() -> None:
    """Verify state registry keeps public generation tool names."""
    routing_def = STATE_REGISTRY[OrchestratorState.SETUP_REQUIRED]
    tool_names = [
        getattr(tool, "__name__", None) or getattr(tool, "name", None)
        for tool in routing_def.tools
    ]

    assert "product_vision_tool" in tool_names
    assert "backlog_primer_tool" in tool_names
    assert "roadmap_builder_tool" in tool_names
    assert "user_story_writer_tool" in tool_names
    assert "sprint_planner_tool" in tool_names
    assert len(tool_names) == len(set(tool_names))


def test_minimal_adapter_signatures_remove_legacy_context_fields() -> None:
    """Verify minimal adapter signatures remove legacy context fields."""
    vision_params = inspect.signature(adapters.product_vision_tool).parameters
    backlog_params = inspect.signature(adapters.backlog_primer_tool).parameters
    roadmap_params = inspect.signature(adapters.roadmap_builder_tool).parameters
    sprint_params = inspect.signature(adapters.sprint_planner_tool).parameters
    story_params = inspect.signature(adapters.user_story_writer_tool).parameters

    assert "specification_content" not in vision_params
    assert "compiled_authority" not in vision_params
    assert "prior_vision_state" not in vision_params

    assert "product_vision_statement" not in backlog_params
    assert "technical_spec" not in backlog_params
    assert "compiled_authority" not in backlog_params
    assert "prior_backlog_state" not in backlog_params

    assert "backlog_items" not in roadmap_params
    assert "product_vision" not in roadmap_params
    assert "technical_spec" not in roadmap_params
    assert "compiled_authority" not in roadmap_params
    assert "prior_roadmap_state" not in roadmap_params

    assert "available_stories" not in sprint_params

    assert "technical_spec" not in story_params
    assert "compiled_authority" not in story_params
