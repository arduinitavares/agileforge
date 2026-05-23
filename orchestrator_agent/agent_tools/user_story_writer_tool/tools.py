"""Tools for the User Story Writer agent."""

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from google.adk.tools import ToolContext
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from models.core import Product, UserStory
from models.db import get_engine
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.story_linkage import (
    normalize_requirement_key,
    title_changed_significantly,
)

from .schemes import UserStoryItem

logger = logging.getLogger(__name__)


class SaveStoriesInput(BaseModel):
    """Input schema for save_stories_tool."""

    idempotency_key: Annotated[
        str,
        Field(description="Stable key used to safely replay the same persistence call."),
    ]
    product_id: Annotated[
        int,
        Field(description="The product ID to attach stories to."),
    ]
    parent_requirement: Annotated[
        str,
        Field(description="The roadmap requirement these stories decompose."),
    ]
    stories: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "List of approved story dicts from user_story_writer_tool output. "
                "Each must have: story_title, statement, acceptance_criteria, invest_score."
            ),
        ),
    ]


def _extract_persona(statement: str) -> str | None:
    """Extract the persona/role from 'As a [role], I want ...' format.

    Args:
        statement: Full story statement string.

    Returns:
        Extracted role string, or None if format does not match.
    """
    match = re.match(r"[Aa]s\s+(?:a|an)\s+(.+?),\s+I\s+want", statement)
    if match:
        return match.group(1).strip()
    return None


def _format_acceptance_criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" if not c.startswith("- ") else c for c in criteria)


def _metadata_json(metadata: str | None) -> dict[str, Any]:
    if not metadata:
        return {}
    try:
        parsed = json.loads(metadata)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _acquire_story_save_write_lock(session: Session) -> None:
    """Acquire a SQLite writer lock before idempotency-sensitive reads."""
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _find_story_save_event(
    session: Session,
    *,
    product_id: int,
    idempotency_key: str,
) -> WorkflowEvent | None:
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.event_type == WorkflowEventType.STORIES_SAVED)
        .where(WorkflowEvent.product_id == product_id)
        .order_by(WorkflowEvent.timestamp.desc(), WorkflowEvent.event_id.desc())
    ).all()
    for event in events:
        metadata = _metadata_json(event.event_metadata)
        if metadata.get("idempotency_key") == idempotency_key:
            return event
    return None


def _story_request_payload_hash(validated: list[UserStoryItem]) -> str:
    payload = [item.model_dump(mode="json") for item in validated]
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical_payload.encode()).hexdigest()}"


def _story_save_request_identity(
    *,
    normalized_req: str,
    validated: list[UserStoryItem],
) -> dict[str, str]:
    return {
        "normalized_requirement": normalized_req,
        "story_payload_hash": _story_request_payload_hash(validated),
    }


def _idempotency_key_reused_response(
    *,
    input_data: SaveStoriesInput,
) -> dict[str, Any]:
    return {
        "success": False,
        "product_id": input_data.product_id,
        "parent_requirement": input_data.parent_requirement,
        "idempotency_key": input_data.idempotency_key,
        "idempotency_replayed": False,
        "error_code": "IDEMPOTENCY_KEY_REUSED",
        "error": "Idempotency key was already used for a different story save request.",
    }


def _story_save_event_matches_request(
    event: WorkflowEvent,
    *,
    request_identity: dict[str, str],
) -> bool:
    metadata = _metadata_json(event.event_metadata)
    return (
        metadata.get("normalized_requirement")
        == request_identity["normalized_requirement"]
        and metadata.get("story_payload_hash") == request_identity["story_payload_hash"]
    )


def _story_replacement_blockers(stories: list[UserStory]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for story in stories:
        reasons: list[str] = []
        if len(story.sprints or []) > 0:
            reasons.append("linked_sprint")
        status_value = getattr(story.status, "value", story.status)
        if story.status != StoryStatus.TO_DO and status_value != StoryStatus.TO_DO.value:
            reasons.append("status_progressed")
        if reasons:
            blockers.append(
                {
                    "story_id": story.story_id,
                    "refinement_slot": story.refinement_slot,
                    "title": story.title,
                    "status": status_value,
                    "reasons": reasons,
                }
            )
    return blockers


def _validate_story_items(
    stories: list[dict[str, Any]],
) -> tuple[list[UserStoryItem], list[str]]:
    validated: list[UserStoryItem] = []
    validation_errors: list[str] = []

    for idx, story_dict in enumerate(stories):
        try:
            item = UserStoryItem.model_validate(story_dict)
            validated.append(item)
        except ValidationError as e:
            errors = []
            for err in e.errors():
                loc = err.get("loc", ())
                prefix = str(loc[0]) if loc else "(model)"
                errors.append(f"{prefix}: {err['msg']}")
            validation_errors.append(f"Story {idx + 1}: {'; '.join(errors)}")

    return validated, validation_errors


def _active_stories_for_requirement(
    session: Session,
    *,
    product_id: int,
    normalized_req: str,
) -> list[UserStory]:
    return session.exec(
        select(UserStory)
        .where(UserStory.product_id == product_id)
        .where(UserStory.source_requirement == normalized_req)
        .where(UserStory.is_superseded == False)  # noqa: E712
        .order_by(UserStory.refinement_slot, UserStory.story_id)
    ).all()


def _replay_response(
    *,
    input_data: SaveStoriesInput,
    event: WorkflowEvent,
) -> dict[str, Any]:
    metadata = _metadata_json(event.event_metadata)
    return {
        "success": True,
        "product_id": input_data.product_id,
        "parent_requirement": input_data.parent_requirement,
        "idempotency_key": input_data.idempotency_key,
        "idempotency_replayed": True,
        "saved_count": metadata.get("saved_count", 0),
        "updated_count": metadata.get("updated_count", 0),
        "created_count": metadata.get("created_count", 0),
        "superseded_count": metadata.get("superseded_count", 0),
        "updated_story_ids": metadata.get("updated_story_ids", []),
        "created_story_ids": metadata.get("created_story_ids", []),
        "superseded_story_ids": metadata.get("superseded_story_ids", []),
        "story_ids": metadata.get("story_ids", []),
        "message": f"Replayed previous save for '{input_data.parent_requirement}'.",
    }


def _unsafe_replacement_response(blockers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": "STORY_REPLACEMENT_UNSAFE",
        "error": (
            "Existing active stories for this requirement have progressed "
            "downstream and cannot be replaced."
        ),
        "blockers": blockers,
    }


def _upsert_refined_story(
    session: Session,
    *,
    linkage: tuple[int, str],
    slot: int,
    item: UserStoryItem,
    existing: UserStory | None,
) -> tuple[int, str]:
    """Upsert a refined story by deterministic linkage key."""
    product_id, normalized_req = linkage
    persona = _extract_persona(item.statement)
    ac_text = _format_acceptance_criteria(item.acceptance_criteria)

    if existing:
        if title_changed_significantly(existing.title, item.story_title):
            logger.warning(
                "refinement.slot_title_drift product_id=%s requirement=%s slot=%s old=%r new=%r",
                product_id,
                normalized_req,
                slot,
                existing.title,
                item.story_title,
            )

        if (not (existing.acceptance_criteria or "").strip()) and (
            existing.original_acceptance_criteria is None
        ):
            existing.original_acceptance_criteria = existing.acceptance_criteria

        existing.title = item.story_title
        existing.story_description = item.statement
        existing.acceptance_criteria = ac_text
        existing.persona = persona
        existing.story_origin = "refined"
        existing.is_refined = True
        existing.is_superseded = False
        existing.ac_updated_at = datetime.now(UTC)
        existing.ac_update_reason = "user_story_refinement"
        session.add(existing)
        session.flush()
        existing_story_id = existing.story_id
        if existing_story_id is None:
            raise RuntimeError("Existing story ID was not generated.")
        return existing_story_id, "updated"

    story = UserStory(
        product_id=product_id,
        title=item.story_title,
        story_description=item.statement,
        acceptance_criteria=ac_text,
        persona=persona,
        source_requirement=normalized_req,
        refinement_slot=slot,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        ac_update_reason="user_story_refinement",
    )
    session.add(story)
    session.flush()
    story_id = story.story_id
    if story_id is None:
        raise RuntimeError("Story ID was not generated.")
    return story_id, "created"


def _story_save_metadata(
    *,
    input_data: SaveStoriesInput,
    request_identity: dict[str, str],
    updated_ids: list[int],
    created_ids: list[int],
    superseded_ids: list[int],
) -> dict[str, Any]:
    return {
        "parent_requirement": input_data.parent_requirement,
        "idempotency_key": input_data.idempotency_key,
        **request_identity,
        "saved_count": len(updated_ids) + len(created_ids),
        "updated_count": len(updated_ids),
        "created_count": len(created_ids),
        "superseded_count": len(superseded_ids),
        "updated_story_ids": updated_ids,
        "created_story_ids": created_ids,
        "story_ids": sorted(updated_ids + created_ids),
        "superseded_story_ids": superseded_ids,
    }


def _success_response(
    *,
    input_data: SaveStoriesInput,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "success": True,
        "product_id": input_data.product_id,
        "parent_requirement": input_data.parent_requirement,
        "idempotency_key": input_data.idempotency_key,
        "idempotency_replayed": False,
        "saved_count": metadata["saved_count"],
        "updated_count": metadata["updated_count"],
        "created_count": metadata["created_count"],
        "superseded_count": metadata["superseded_count"],
        "updated_story_ids": metadata["updated_story_ids"],
        "created_story_ids": metadata["created_story_ids"],
        "superseded_story_ids": metadata["superseded_story_ids"],
        "story_ids": metadata["story_ids"],
        "message": (
            f"{metadata['saved_count']} user stories saved for "
            f"'{input_data.parent_requirement}'."
        ),
    }


def _persist_validated_stories(
    session: Session,
    *,
    input_data: SaveStoriesInput,
    normalized_req: str,
    validated: list[UserStoryItem],
    existing_active: list[UserStory],
) -> dict[str, Any]:
    created_ids: list[int] = []
    updated_ids: list[int] = []
    superseded_ids: list[int] = []
    existing_by_slot = {
        story.refinement_slot: story
        for story in existing_active
        if story.refinement_slot is not None
    }

    for idx, item in enumerate(validated, start=1):
        story_id, action = _upsert_refined_story(
            session,
            linkage=(input_data.product_id, normalized_req),
            slot=idx,
            item=item,
            existing=existing_by_slot.get(idx),
        )
        target = created_ids if action == "created" else updated_ids
        target.append(story_id)

    active_slots = set(range(1, len(validated) + 1))
    for story in existing_active:
        if story.refinement_slot in active_slots:
            continue
        story.is_superseded = True
        session.add(story)
        if story.story_id is None:
            raise RuntimeError("Existing story ID was not generated.")
        superseded_ids.append(story.story_id)

    if len(validated) < len(existing_active):
        logger.warning(
            "refinement.slot_underflow product_id=%s requirement=%s refined_count=%s seeded_count=%s",
            input_data.product_id,
            input_data.parent_requirement,
            len(validated),
            len(existing_active),
        )

    return _story_save_metadata(
        input_data=input_data,
        request_identity=_story_save_request_identity(
            normalized_req=normalized_req,
            validated=validated,
        ),
        updated_ids=updated_ids,
        created_ids=created_ids,
        superseded_ids=superseded_ids,
    )


def _story_refinement_duration(
    *,
    tool_context: ToolContext | None,
    start_ts: float,
) -> float:
    duration_seconds = None
    if tool_context and tool_context.state:
        duration_seconds = tool_context.state.get("story_refinement_duration")
    if duration_seconds is None:
        duration_seconds = round(time.perf_counter() - start_ts, 3)
    return float(duration_seconds)


def _store_story_context(
    *,
    tool_context: ToolContext | None,
    input_data: SaveStoriesInput,
    metadata: dict[str, Any],
) -> None:
    if not tool_context:
        return
    saved_key = f"stories_{input_data.parent_requirement}"
    tool_context.state[saved_key] = {
        "product_id": input_data.product_id,
        "parent_requirement": input_data.parent_requirement,
        "story_ids": metadata["story_ids"],
        "count": metadata["saved_count"],
    }


def save_stories_tool(
    input_data: SaveStoriesInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """
    Persist approved user stories to the database.

    Validates each story against UserStoryItem schema, creates UserStory
    rows linked to the given product_id, and extracts persona from statement.

    Args:
        input_data: Stories payload with product_id and story list.
        tool_context: Optional ADK context for session state storage.

    Returns:
        Dict with success status, saved count, and created story IDs.
    """
    engine = get_engine()
    start_ts = time.perf_counter()

    with Session(engine) as session:
        _acquire_story_save_write_lock(session)

        # Verify product exists
        product = session.exec(
            select(Product).where(Product.product_id == input_data.product_id)
        ).first()

        if not product:
            return {
                "success": False,
                "error": f"Product with ID {input_data.product_id} not found.",
            }

        # Validate each story against schema
        validated, validation_errors = _validate_story_items(input_data.stories)

        if validation_errors:
            return {
                "success": False,
                "error": f"Validation errors: {'; '.join(validation_errors)}",
                "valid_count": len(validated),
                "invalid_count": len(validation_errors),
            }

        # Persist to database via deterministic linkage upsert
        normalized_req = normalize_requirement_key(input_data.parent_requirement)
        request_identity = _story_save_request_identity(
            normalized_req=normalized_req,
            validated=validated,
        )
        previous_event = _find_story_save_event(
            session,
            product_id=input_data.product_id,
            idempotency_key=input_data.idempotency_key,
        )
        if previous_event is not None:
            if not _story_save_event_matches_request(
                previous_event,
                request_identity=request_identity,
            ):
                return _idempotency_key_reused_response(input_data=input_data)
            return _replay_response(input_data=input_data, event=previous_event)

        existing_active = _active_stories_for_requirement(
            session,
            product_id=input_data.product_id,
            normalized_req=normalized_req,
        )
        blockers = _story_replacement_blockers(existing_active)
        if blockers:
            return _unsafe_replacement_response(blockers)

        metadata = _persist_validated_stories(
            session,
            input_data=input_data,
            normalized_req=normalized_req,
            validated=validated,
            existing_active=existing_active,
        )
        duration_seconds = _story_refinement_duration(
            tool_context=tool_context,
            start_ts=start_ts,
        )
        session_id = getattr(tool_context, "session_id", None) if tool_context else None
        event_metadata = json.dumps(metadata)
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.STORIES_SAVED,
                product_id=input_data.product_id,
                session_id=session_id,
                duration_seconds=float(duration_seconds),
                event_metadata=event_metadata,
            )
        )
        session.commit()

        # Optionally store in session state for downstream use
        _store_story_context(
            tool_context=tool_context,
            input_data=input_data,
            metadata=metadata,
        )

        print(
            f"\n\033[92m[Stories Saved]\033[0m "
            f"{metadata['saved_count']} stories for '{input_data.parent_requirement}' "
            f"(updated={metadata['updated_count']}, created={metadata['created_count']})"
        )

        return _success_response(input_data=input_data, metadata=metadata)


__all__ = ["SaveStoriesInput", "save_stories_tool"]
