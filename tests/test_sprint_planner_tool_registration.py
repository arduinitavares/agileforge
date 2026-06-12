"""Test that sprint_planner_tool is correctly wired end-to-end.

Validates:
1. FSM definitions register the tool under the correct name
2. _dedupe_tools preserves it
3. AgentTool._get_declaration() produces a valid declaration
4. The tool name in declaration matches the expected name
5. Simulated tools_dict build includes sprint_planner_tool
"""

import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import cast

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: PTH100, PTH120

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool

from orchestrator_agent.fsm.definitions import STATE_REGISTRY
from orchestrator_agent.fsm.states import OrchestratorState


def _build_tools_dict(tools: Iterable[object]) -> dict[str, object]:
    """Simulate what ADK's LlmRequest.append_tools does."""
    tools_dict: dict[str, object] = {}
    for tool in tools:
        if isinstance(tool, BaseTool):
            decl = tool._get_declaration()
            if decl:
                tools_dict[tool.name] = tool
        elif callable(tool):
            ft = FunctionTool(func=tool)
            decl = ft._get_declaration()
            if decl:
                tools_dict[ft.name] = ft
    return tools_dict


def _dedupe_tools(tools: Iterable[object]) -> list[object]:
    """Deduplicate tools by ADK-visible name."""
    seen: set[object] = set()
    deduped: list[object] = []
    for tool in tools:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        name = name or repr(tool)
        if name in seen:
            continue
        seen.add(name)
        deduped.append(tool)
    return deduped


class TestSprintPlannerToolRegistration:
    """Verify that sprint_planner_tool is accessible in all FSM states that list it."""

    STATES_WITH_SPRINT_PLANNER = [  # noqa: RUF012
        OrchestratorState.SETUP_REQUIRED,
        OrchestratorState.STORY_PERSISTENCE,
        OrchestratorState.SPRINT_SETUP,
        OrchestratorState.SPRINT_DRAFT,
        OrchestratorState.SPRINT_PERSISTENCE,
    ]

    def test_sprint_planner_in_routing_mode_tools(self) -> None:
        """Verify sprint planner in routing mode tools."""
        state_def = STATE_REGISTRY[OrchestratorState.SETUP_REQUIRED]
        names = [
            getattr(t, "name", None) or getattr(t, "__name__", None)
            for t in state_def.tools
        ]
        assert "sprint_planner_tool" in names, (
            f"sprint_planner_tool not in SETUP_REQUIRED tools: {names}"
        )

    def test_sprint_planner_survives_dedupe(self) -> None:
        """Verify sprint planner survives dedupe."""
        state_def = STATE_REGISTRY[OrchestratorState.SETUP_REQUIRED]
        deduped = _dedupe_tools(cast("Iterable[object]", state_def.tools))
        names = [
            getattr(t, "name", None) or getattr(t, "__name__", None) for t in deduped
        ]
        assert "sprint_planner_tool" in names, (
            f"sprint_planner_tool lost after _dedupe_tools: {names}"
        )

    def test_sprint_planner_declaration_valid(self) -> None:
        """Verify sprint planner declaration valid."""
        state_def = STATE_REGISTRY[OrchestratorState.SETUP_REQUIRED]
        for tool in state_def.tools:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
            if name == "sprint_planner_tool":
                if isinstance(tool, BaseTool):
                    decl = tool._get_declaration()
                else:
                    assert callable(tool), (
                        f"sprint_planner_tool should be callable or BaseTool, got {type(tool)}"  # noqa: E501
                    )
                    decl = FunctionTool(func=tool)._get_declaration()
                assert decl is not None, "sprint_planner_tool declaration is None"
                assert decl.name == "sprint_planner_tool", (
                    f"Declaration name mismatch: {decl.name}"
                )
                return
        assert False, "sprint_planner_tool not found in SETUP_REQUIRED tools"  # noqa: B011, PT015

    def test_sprint_planner_in_simulated_tools_dict(self) -> None:
        """Key test: Simulates the full ADK tools_dict build pipeline."""
        state_def = STATE_REGISTRY[OrchestratorState.SETUP_REQUIRED]
        deduped = _dedupe_tools(cast("Iterable[object]", state_def.tools))
        tools_dict = _build_tools_dict(deduped)
        assert "sprint_planner_tool" in tools_dict, (
            f"sprint_planner_tool not in tools_dict! Keys: {list(tools_dict.keys())}"
        )

    def test_all_states_with_sprint_planner(self) -> None:
        """Verify sprint_planner_tool is in tools_dict for ALL states that should have it."""  # noqa: E501
        for state in self.STATES_WITH_SPRINT_PLANNER:
            state_def = STATE_REGISTRY.get(state)
            assert state_def is not None, f"State {state} not in registry"
            deduped = _dedupe_tools(cast("Iterable[object]", state_def.tools))
            tools_dict = _build_tools_dict(deduped)
            assert "sprint_planner_tool" in tools_dict, (
                f"sprint_planner_tool not in tools_dict for {state.value}! "
                f"Keys: {list(tools_dict.keys())}"
            )

    def test_no_duplicate_tool_names(self) -> None:
        """Verify no two tools share the same name in SETUP_REQUIRED."""
        state_def = STATE_REGISTRY[OrchestratorState.SETUP_REQUIRED]
        names = []
        for tool in state_def.tools:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
            names.append(name)
        seen = set()
        dupes = [n for n in names if n in seen or seen.add(n)]
        assert not dupes, f"Duplicate tool names: {dupes}"

    def test_sprint_draft_does_not_expose_unguarded_save_tool(self) -> None:
        """Verify Sprint draft cannot bypass guarded command save."""
        state_def = STATE_REGISTRY[OrchestratorState.SPRINT_DRAFT]
        names = [
            getattr(t, "name", None) or getattr(t, "__name__", None)
            for t in state_def.tools
        ]

        assert "save_sprint_plan_tool" not in names
        assert "save_sprint_plan_tool" not in state_def.instruction
        assert "guarded" in state_def.instruction

    def test_root_agent_does_not_register_unguarded_sprint_save_tool(self) -> None:
        """Verify root agent does not expose unguarded Sprint save."""
        agent_source = Path("orchestrator_agent/agent.py").read_text()
        tools_block = agent_source[
            agent_source.index("tools=[") : agent_source.index("    ],")
        ]

        assert "save_sprint_plan_tool" not in tools_block
