"""Tools for backlog_primer agent."""

import json
import time
from typing import Annotated, Any

from google.adk.tools import ToolContext
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from models.core import SprintStory, UserStory
from models.db import get_engine
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key

from .schemes import BacklogItem


class SaveBacklogInput(BaseModel):
    """Input schema for save_backlog_tool."""

    product_id: Annotated[int, Field(description="The product ID.")]
    idempotency_key: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional save idempotency key supplied by guarded callers.",
        ),
    ]
    backlog_items: Annotated[
        list[dict[str, Any]],
        Field(
            default_factory=list,
            description=(
                "List of approved backlog items from backlog_primer_tool output. "
                "Each must have: priority, requirement, value_driver, justification, estimated_effort."
            ),
        ),
    ]


def _resolve_backlog_items_from_state(
    tool_context: ToolContext,
) -> list[dict[str, Any]]:
    """Fallback loader for approved backlog items stored in volatile state."""
    state = tool_context.state or {}

    approved_backlog = state.get("approved_backlog")
    if isinstance(approved_backlog, dict):
        items = approved_backlog.get("items")
        if isinstance(items, list):
            return items

    product_backlog = state.get("product_backlog")
    if isinstance(product_backlog, dict):
        items = product_backlog.get("backlog_items")
        if isinstance(items, list):
            return items

    return []


async def save_backlog_tool(
    save_input: SaveBacklogInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """
    Save approved backlog items to the DATABASE as UserStory records.

    This tool persists the approved backlog items directly to the UserStory table
    so they are available for Sprint Planning and Roadmap creation.

    Use this tool when:
    - User approves the backlog in BACKLOG_REVIEW state
    """
    if not tool_context:
        return {
            "success": False,
            "error": "ToolContext required for session state storage.",
        }

    normalized_input = save_input
    if not normalized_input.backlog_items:
        normalized_input = SaveBacklogInput(
            product_id=normalized_input.product_id,
            backlog_items=_resolve_backlog_items_from_state(tool_context),
        )

    # Validate backlog items using BacklogItem schema
    validated_items: list[BacklogItem] = []
    validation_errors: list[str] = []

    for idx, item in enumerate(normalized_input.backlog_items):
        try:
            validated = BacklogItem.model_validate(item)
            validated_items.append(validated)
        except ValidationError as e:
            errors = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
            validation_errors.append(f"Item {idx + 1}: {'; '.join(errors)}")

    if validation_errors:
        return {
            "success": False,
            "error": f"Validation errors: {'; '.join(validation_errors)}",
            "valid_count": len(validated_items),
            "invalid_count": len(validation_errors),
        }

    # 1. Update Session State (Legacy/Fallback support)
    tool_context.state["approved_backlog"] = {
        "product_id": normalized_input.product_id,
        "items": [item.model_dump() for item in validated_items],
        "item_count": len(validated_items),
    }

    # 2. Persist to Database (New Logic)
    start_ts = time.perf_counter()
    engine = get_engine()
    created_count = 0
    superseded_count = 0

    with Session(engine) as session:
        replay = _idempotent_backlog_save_replay(
            session,
            product_id=normalized_input.product_id,
            idempotency_key=normalized_input.idempotency_key,
        )
        if replay is not None:
            return replay

        active_stories = session.exec(
            select(UserStory)
            .where(UserStory.product_id == normalized_input.product_id)
            .where(UserStory.is_superseded == False)  # noqa: E712
        ).all()
        blocked = [
            story
            for story in active_stories
            if _blocks_backlog_replacement(session, story)
        ]
        if blocked:
            return {
                "success": False,
                "error": "BACKLOG_REPLACEMENT_BLOCKED",
                "blocked_count": len(blocked),
                "blocked_story_ids": [
                    story.story_id for story in blocked if story.story_id is not None
                ],
                "message": (
                    "Existing backlog stories have progressed downstream; "
                    "refine or reconcile them before replacing the backlog."
                ),
            }

        for story in active_stories:
            if story.story_origin == "backlog_seed":
                story.is_superseded = True
                session.add(story)
                superseded_count += 1

        for item in validated_items:
            normalized_requirement = normalize_requirement_key(item.requirement)
            slot = item.priority

            # Create new UserStory
            # Mapping:
            # requirement -> title
            # priority -> rank (converted to string to ensure sortability? or just numeric string)
            # estimated_effort -> story_points (approximate mapping)

            # Map 'S', 'M', 'L', 'XL' (and legacy Low/Medium/High) to points
            points = None
            effort_str = str(item.estimated_effort).strip().upper()

            # T-Shirt Sizing from schema
            if effort_str == "S":
                points = 1
            elif effort_str == "M":
                points = 3
            elif effort_str == "L":
                points = 5
            elif effort_str == "XL":
                points = 8
            # Legacy/Fallback
            elif effort_str == "LOW":
                points = 1
            elif effort_str == "MEDIUM":
                points = 3
            elif effort_str == "HIGH":
                points = 5
            elif effort_str.isdigit():
                points = int(effort_str)

            new_story = UserStory(
                title=item.requirement,
                product_id=normalized_input.product_id,
                status=StoryStatus.TO_DO,
                rank=str(item.priority),  # Storing priority as rank
                story_points=points,
                story_description=item.justification,  # Using justification as initial description context
                acceptance_criteria=None,  # To be filled by UserStory Writer later
                source_requirement=normalized_requirement,
                refinement_slot=slot,
                story_origin="backlog_seed",
                is_refined=False,
                is_superseded=False,
            )
            session.add(new_story)
            created_count += 1

        duration_seconds = tool_context.state.get("backlog_generation_duration")
        if duration_seconds is None:
            duration_seconds = round(time.perf_counter() - start_ts, 3)
        raw_session_id = getattr(tool_context, "session_id", None)
        session_id = raw_session_id if isinstance(raw_session_id, str) else None
        event_metadata = json.dumps(
            {
                "action": "backlog_saved",
                "idempotency_key": normalized_input.idempotency_key,
                "processed_count": len(validated_items),
                "created_count": created_count,
                "superseded_count": superseded_count,
            }
        )
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.BACKLOG_SAVED,
                product_id=normalized_input.product_id,
                session_id=session_id,
                duration_seconds=float(duration_seconds),
                event_metadata=event_metadata,
            )
        )
        session.commit()

    print(
        "\n\033[92m[Backlog Saved]\033[0m "
        f"{created_count} new items persisted to DB "
        f"(superseded: {superseded_count}; total processed: {len(validated_items)})"
    )

    return {
        "success": True,
        "product_id": normalized_input.product_id,
        "saved_count": created_count,
        "superseded_count": superseded_count,
        "total_items": len(validated_items),
        "message": f"Backlog saved. {created_count} new stories created in database.",
        "next_phase": "roadmap",  # Or "sprint_planning" if user prefers
    }


def _idempotent_backlog_save_replay(
    session: Session,
    *,
    product_id: int,
    idempotency_key: str | None,
) -> dict[str, Any] | None:
    """Return a replay response when this save key already succeeded."""
    if not idempotency_key:
        return None
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.product_id == product_id)
        .where(WorkflowEvent.event_type == WorkflowEventType.BACKLOG_SAVED)
    ).all()
    for event in events:
        metadata = _json_object(event.event_metadata)
        if metadata.get("action") == "backlog_reconciled":
            continue
        if metadata.get("idempotency_key") != idempotency_key:
            continue
        return {
            "success": True,
            "product_id": product_id,
            "saved_count": 0,
            "superseded_count": 0,
            "total_items": int(metadata.get("processed_count") or 0),
            "idempotent_replay": True,
            "message": "Backlog save idempotency key already persisted.",
            "next_phase": "roadmap",
        }
    return None


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _blocks_backlog_replacement(session: Session, story: UserStory) -> bool:
    """Return whether a story has progressed beyond replaceable seed state."""
    if story.story_origin != "backlog_seed":
        return True
    if story.is_refined or story.acceptance_criteria:
        return True
    if story.status != StoryStatus.TO_DO:
        return True
    if story.story_id is None:
        return False
    sprint_link = session.exec(
        select(SprintStory).where(SprintStory.story_id == story.story_id)
    ).first()
    return sprint_link is not None


__all__ = ["SaveBacklogInput", "save_backlog_tool"]
