"""Deterministic context-injection adapters for orchestrated generation tools.

These adapters preserve the original public tool names while removing model
control over context-heavy arguments (technical spec, compiled authority, and
prior states). The Orchestrator LLM passes only user intent fields.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Optional, cast

from google.adk.tools import AgentTool, ToolContext

from orchestrator_agent.agent_tools.backlog_primer.agent import (
    root_agent as backlog_agent,
)
from orchestrator_agent.agent_tools.product_vision_tool.agent import (
    root_agent as vision_agent,
)
from orchestrator_agent.agent_tools.roadmap_builder.agent import (
    root_agent as roadmap_agent,
)
from orchestrator_agent.agent_tools.sprint_planner_tool.agent import (
    root_agent as sprint_planner_agent,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.agent import (
    root_agent as story_writer_agent,
)
from services.sprint_input import prepare_sprint_input_context
from tools.orchestrator_tools import fetch_sprint_candidates

_PRODUCT_VISION_TOOL = AgentTool(agent=vision_agent)
_BACKLOG_PRIMER_TOOL = AgentTool(agent=backlog_agent)
_ROADMAP_BUILDER_TOOL = AgentTool(agent=roadmap_agent)
_SPRINT_PLANNER_TOOL = AgentTool(agent=sprint_planner_agent)
_USER_STORY_WRITER_TOOL = AgentTool(agent=story_writer_agent)


def _state(tool_context: ToolContext | None) -> dict[str, Any]:
    """Return state dict from ToolContext or empty dict when unavailable."""
    if not tool_context or tool_context.state is None:
        return {}
    return cast("dict[str, Any]", tool_context.state)


def _as_text(value: Any) -> str:
    """Normalize arbitrary values into text for tool arguments."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _json_or_no_history(value: Any) -> str:
    """Serialize stateful prior payloads to JSON or fallback to NO_HISTORY."""
    if value is None:
        return "NO_HISTORY"
    if isinstance(value, str):
        text = value.strip()
        return text if text else "NO_HISTORY"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "NO_HISTORY"


def _missing_context_error(
    *,
    code: str,
    missing_context: Sequence[str],
    guidance: str,
) -> dict[str, Any]:
    """Build standardized fail-fast payload for orchestration states."""
    missing = list(missing_context)
    return {
        "is_complete": False,
        "error": code,
        "missing_context": missing,
        "clarifying_questions": [
            (f"Missing context: {', '.join(missing)}. {guidance}")
        ],
        "message": (
            "Deterministic adapter blocked tool execution due to missing "
            "volatile context."
        ),
    }


def _parse_roadmap_payload(value: Any) -> dict[str, Any] | None:
    """Parse roadmap payload from state into a dict when possible."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return cast("dict[str, Any]", parsed)
    return None


def _extract_roadmap_dict(state: dict[str, Any]) -> dict[str, Any] | None:
    """Load roadmap from active_project first, then roadmap_result."""
    active_project = state.get("active_project")
    if isinstance(active_project, dict):
        roadmap = _parse_roadmap_payload(active_project.get("roadmap"))
        if roadmap:
            return roadmap
    roadmap_result = _parse_roadmap_payload(state.get("roadmap_result"))
    if roadmap_result:
        return roadmap_result
    return None


def _derive_requirement_context(
    *,
    state: dict[str, Any],
    parent_requirement: str,
) -> str | None:
    """Derive requirement context from roadmap release metadata."""
    roadmap = _extract_roadmap_dict(state)
    if not roadmap:
        return None

    releases = roadmap.get("roadmap_releases")
    if not isinstance(releases, list):
        return None

    normalized_parent = parent_requirement.strip()
    for release in releases:
        if not isinstance(release, dict):
            continue
        items = release.get("items")
        if not isinstance(items, list):
            continue

        if normalized_parent not in [str(item).strip() for item in items]:
            continue

        theme = _as_text(release.get("theme")).strip()
        focus_area = _as_text(release.get("focus_area")).strip()
        reasoning = _as_text(release.get("reasoning")).strip()
        parts: list[str] = []
        if theme:
            parts.append(f"Theme: {theme}")
        if focus_area:
            parts.append(f"Focus Area: {focus_area}")
        if reasoning:
            parts.append(f"Reasoning: {reasoning}")
        return " | ".join(parts).strip() if parts else None

    return None


def _normalize_positive_int(value: Any) -> int | None:
    """Normalize optional positive integer fields."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_priority(value: Any, fallback: int) -> int:
    """Ensure priority is always an integer >= 1."""
    parsed = _normalize_positive_int(value)
    return parsed if parsed is not None else max(1, fallback)


def _normalize_selected_story_ids(value: Any) -> list[int]:
    """Normalize selected story IDs while preserving order and uniqueness."""
    if not isinstance(value, list):
        return []
    seen = set()
    normalized: list[int] = []
    for item in value:
        parsed = _normalize_positive_int(item)
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        normalized.append(parsed)
    return normalized


def _selection_error(invalid_ids: Sequence[int]) -> dict[str, Any]:
    """Build deterministic error payload for invalid sprint selections."""
    invalid = list(invalid_ids)
    invalid_text = ", ".join(str(item) for item in invalid)
    return {
        "is_complete": False,
        "error": "SPRINT_SELECTION_INVALID",
        "missing_context": [f"selected_story_ids_not_eligible: {invalid_text}"],
        "clarifying_questions": [
            (
                "Some selected_story_ids are not refined TO_DO candidates: "
                f"{invalid_text}. Please provide eligible story IDs."
            )
        ],
        "message": (
            "Deterministic adapter blocked sprint planning because selection "
            "contains non-eligible stories."
        ),
        "selection_details": {"invalid_selected_ids": invalid},
    }


def _sprint_selection_error(
    *,
    prepared: dict[str, Any],
    selected_story_ids: object,
) -> dict[str, Any]:
    """Build deterministic error payloads for structured sprint selection errors."""
    error_code = str(prepared.get("error_code") or "SPRINT_SELECTION_INVALID")
    if error_code == "SPRINT_SELECTION_INVALID":
        return _selection_error(prepared.get("invalid_selected_ids") or [])

    details = prepared.get("selection_details")
    selection_details = dict(details) if isinstance(details, dict) else {}
    normalized_selected_ids = _normalize_selected_story_ids(selected_story_ids)
    if normalized_selected_ids:
        selection_details.setdefault("selected_story_ids", normalized_selected_ids)

    guidance = (
        "Include prerequisite story IDs in selected_story_ids or change "
        "selected_story_ids to remove blocked dependent stories."
        if error_code == "SPRINT_SELECTION_DEPENDENCY_MISSING"
        else "Change selected_story_ids to a valid sprint selection and retry."
    )
    return {
        "is_complete": False,
        "error": error_code,
        "missing_context": [error_code.lower()],
        "clarifying_questions": [
            (
                f"Manual sprint selection failed with {error_code}. "
                f"{guidance}"
            )
        ],
        "guidance": guidance,
        "message": (
            "Deterministic adapter blocked sprint planning because manual "
            "selected_story_ids failed selection validation."
        ),
        "selection_details": selection_details,
    }


async def product_vision_tool(
    user_raw_text: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Deterministic adapter for product_vision_tool."""
    if tool_context is None:
        return _missing_context_error(
            code="PRODUCT_VISION_CONTEXT_MISSING",
            missing_context=["tool_context.state"],
            guidance="Retry from orchestrator runtime so session state is available.",
        )

    state = _state(tool_context)
    args = {
        "user_raw_text": user_raw_text,
        "specification_content": _as_text(state.get("pending_spec_content")),
        "compiled_authority": _as_text(state.get("compiled_authority_cached")),
        "prior_vision_state": _json_or_no_history(state.get("vision_components")),
    }
    result = await _PRODUCT_VISION_TOOL.run_async(args=args, tool_context=tool_context)
    return cast("dict[str, Any]", result)


async def backlog_primer_tool(
    user_input: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Deterministic adapter for backlog_primer_tool."""
    if tool_context is None:
        return _missing_context_error(
            code="BACKLOG_CONTEXT_MISSING",
            missing_context=["tool_context.state"],
            guidance="Select a project and ensure specification/authority are loaded.",
        )

    state = _state(tool_context)
    active_project = state.get("active_project")
    vision = ""
    if isinstance(active_project, dict):
        vision = _as_text(active_project.get("vision")).strip()

    technical_spec = _as_text(state.get("pending_spec_content")).strip()
    compiled_authority = _as_text(state.get("compiled_authority_cached")).strip()

    missing: list[str] = []
    if not vision:
        missing.append("active_project.vision")
    if not technical_spec:
        missing.append("pending_spec_content")
    if not compiled_authority:
        missing.append("compiled_authority_cached")

    if missing:
        return _missing_context_error(
            code="BACKLOG_CONTEXT_MISSING",
            missing_context=missing,
            guidance=(
                "Run project selection/spec loading and authority compilation "
                "before backlog generation."
            ),
        )

    args = {
        "product_vision_statement": vision,
        "technical_spec": technical_spec,
        "compiled_authority": compiled_authority,
        "prior_backlog_state": _json_or_no_history(state.get("product_backlog")),
        "user_input": user_input,
    }
    result = await _BACKLOG_PRIMER_TOOL.run_async(args=args, tool_context=tool_context)
    return cast("dict[str, Any]", result)


async def roadmap_builder_tool(
    user_input: str,
    time_increment: str = "Milestone-based",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Deterministic adapter for roadmap_builder_tool."""
    if tool_context is None:
        return _missing_context_error(
            code="ROADMAP_CONTEXT_MISSING",
            missing_context=["tool_context.state"],
            guidance="Select a project and persist backlog before roadmap generation.",
        )

    state = _state(tool_context)
    active_project = state.get("active_project")
    vision = ""
    if isinstance(active_project, dict):
        vision = _as_text(active_project.get("vision")).strip()

    approved_backlog = state.get("approved_backlog")
    backlog_items: list[dict[str, Any]] = []
    if isinstance(approved_backlog, dict) and isinstance(
        approved_backlog.get("items"), list
    ):
        backlog_items = cast("list[dict[str, Any]]", approved_backlog["items"])

    technical_spec = _as_text(state.get("pending_spec_content")).strip()
    compiled_authority = _as_text(state.get("compiled_authority_cached")).strip()

    missing: list[str] = []
    if not vision:
        missing.append("active_project.vision")
    if not backlog_items:
        missing.append("approved_backlog.items")
    if not technical_spec:
        missing.append("pending_spec_content")
    if not compiled_authority:
        missing.append("compiled_authority_cached")

    if missing:
        return _missing_context_error(
            code="ROADMAP_CONTEXT_MISSING",
            missing_context=missing,
            guidance=(
                "Save backlog and ensure spec/authority are available before "
                "roadmap generation."
            ),
        )

    args = {
        "backlog_items": backlog_items,
        "product_vision": vision,
        "technical_spec": technical_spec,
        "compiled_authority": compiled_authority,
        "time_increment": time_increment,
        "prior_roadmap_state": _json_or_no_history(state.get("roadmap_result")),
        "user_input": user_input,
    }
    result = await _ROADMAP_BUILDER_TOOL.run_async(args=args, tool_context=tool_context)
    return cast("dict[str, Any]", result)


async def sprint_planner_tool(
    capacity_points: int,
    capacity_source: str,
    capacity_basis: str,
    user_context: str = "",
    max_story_points: Optional[int] = None,
    include_task_decomposition: bool = True,
    selected_story_ids: Optional[list[int]] = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Deterministic adapter for sprint_planner_tool."""
    if tool_context is None:
        return _missing_context_error(
            code="SPRINT_CONTEXT_MISSING",
            missing_context=["tool_context.state"],
            guidance="Select a project before sprint planning.",
        )

    state = _state(tool_context)
    active_project = state.get("active_project")
    product_id = None
    if isinstance(active_project, dict):
        product_id = _normalize_positive_int(active_project.get("product_id"))

    if product_id is None:
        return _missing_context_error(
            code="SPRINT_CONTEXT_MISSING",
            missing_context=["active_project.product_id"],
            guidance="Select an active project, then retry sprint planning.",
        )

    prepared = prepare_sprint_input_context(
        product_id=product_id,
        capacity_points=capacity_points,
        capacity_source=capacity_source,
        capacity_basis=capacity_basis,
        user_context=user_context,
        max_story_points=max_story_points,
        include_task_decomposition=include_task_decomposition,
        selected_story_ids=selected_story_ids,
        fetch_candidates=fetch_sprint_candidates,
    )

    if not prepared.get("success"):
        error_code = prepared.get("error_code")
        if isinstance(error_code, str) and error_code.startswith("SPRINT_SELECTION_"):
            return _sprint_selection_error(
                prepared=prepared,
                selected_story_ids=selected_story_ids,
            )
        return _missing_context_error(
            code=str(error_code or "SPRINT_CONTEXT_MISSING"),
            missing_context=[
                "sprint candidates"
                if error_code == "SPRINT_CANDIDATE_FETCH_FAILED"
                else "refined TO_DO stories"
            ],
            guidance=(
                "Retry fetching the backlog after selecting the active project."
                if error_code == "SPRINT_CANDIDATE_FETCH_FAILED"
                else "Only refined stories are sprint-eligible. Refine stories first."
            ),
        )

    result = await _SPRINT_PLANNER_TOOL.run_async(
        args=cast("dict[str, Any]", prepared["input_context"]),
        tool_context=tool_context,
    )
    return cast("dict[str, Any]", result)


async def user_story_writer_tool(
    parent_requirement: str,
    requirement_context: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Deterministic adapter for user_story_writer_tool."""
    if tool_context is None:
        return _missing_context_error(
            code="STORY_CONTEXT_MISSING",
            missing_context=["tool_context.state"],
            guidance="Select a project and ensure roadmap/spec/authority are present.",
        )

    state = _state(tool_context)
    technical_spec = _as_text(state.get("pending_spec_content")).strip()
    compiled_authority = _as_text(state.get("compiled_authority_cached")).strip()

    derived_context = _derive_requirement_context(
        state=state, parent_requirement=parent_requirement
    )
    resolved_requirement_context = derived_context or requirement_context.strip()

    missing: list[str] = []
    if not technical_spec:
        missing.append("pending_spec_content")
    if not compiled_authority:
        missing.append("compiled_authority_cached")
    if not resolved_requirement_context:
        missing.append("roadmap release metadata (or explicit requirement_context)")

    if missing:
        return _missing_context_error(
            code="STORY_CONTEXT_MISSING",
            missing_context=missing,
            guidance=(
                "Save roadmap first so release metadata can be used to decompose "
                "requirements into stories."
            ),
        )

    args = {
        "parent_requirement": parent_requirement,
        "requirement_context": resolved_requirement_context,
        "technical_spec": technical_spec,
        "compiled_authority": compiled_authority,
    }
    result = await _USER_STORY_WRITER_TOOL.run_async(
        args=args, tool_context=tool_context
    )
    return cast("dict[str, Any]", result)


__all__ = [
    "backlog_primer_tool",
    "product_vision_tool",
    "roadmap_builder_tool",
    "sprint_planner_tool",
    "user_story_writer_tool",
]
