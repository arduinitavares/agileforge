"""Tools for the User Story Writer agent."""

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from google.adk.tools import ToolContext
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlmodel import Session, select

from models.core import Product, UserStory, UserStoryDependency
from models.db import get_engine
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.story_linkage import (
    normalize_requirement_key,
    title_changed_significantly,
)

from .schemes import StoryDependencyCandidate, UserStoryItem

logger = logging.getLogger(__name__)


class SaveStoriesInput(BaseModel):
    """Input schema for save_stories_tool."""

    idempotency_key: Annotated[
        str,
        Field(
            description="Stable key used to safely replay the same persistence call."
        ),
    ]
    product_id: Annotated[
        int,
        Field(description="The product ID to attach stories to."),
    ]
    parent_requirement: Annotated[
        str,
        Field(description="The roadmap requirement these stories decompose."),
    ]
    parent_rank: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "1-based Roadmap parent order used to derive deterministic child story rank."
            ),
        ),
    ] = None
    story_origin: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional persistence origin override for extension scope.",
        ),
    ] = None
    accepted_spec_version_id: Annotated[
        int | None,
        Field(
            default=None,
            description="Accepted amended spec version that produced these stories.",
        ),
    ] = None
    stories: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "List of approved story dicts from user_story_writer_tool output. "
                "Each must have: story_title, statement, acceptance_criteria, invest_score."
            ),
        ),
    ]


class SaveStoryPatchInput(BaseModel):
    """Input schema for saving one targeted story refinement patch."""

    idempotency_key: Annotated[
        str,
        Field(
            description="Stable key used to safely replay the same persistence call."
        ),
    ]
    product_id: Annotated[
        int,
        Field(description="The product ID that owns the target story."),
    ]
    parent_requirement: Annotated[
        str,
        Field(description="The roadmap requirement that owns the target story."),
    ]
    parent_rank: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "1-based Roadmap parent order used to derive deterministic child story rank."
            ),
        ),
    ] = None
    target_story_id: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Existing story ID to update. Mutually exclusive with "
                "target_refinement_slot."
            ),
        ),
    ] = None
    target_refinement_slot: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Existing refinement slot to update. Mutually exclusive with "
                "target_story_id."
            ),
        ),
    ] = None
    story_origin: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional persistence origin override for extension scope.",
        ),
    ] = None
    accepted_spec_version_id: Annotated[
        int | None,
        Field(
            default=None,
            description="Accepted amended spec version that produced the story.",
        ),
    ] = None
    story: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Single approved story dict from user_story_writer_tool output. "
                "Must have: story_title, statement, acceptance_criteria, invest_score."
            ),
        ),
    ]

    @model_validator(mode="after")
    def _validate_exactly_one_target(self) -> "SaveStoryPatchInput":
        has_story_id = self.target_story_id is not None
        has_slot = self.target_refinement_slot is not None
        if has_story_id == has_slot:
            raise ValueError(
                "Exactly one of target_story_id or target_refinement_slot is required."
            )
        return self


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


_EFFORT_TO_STORY_POINTS: dict[str, int] = {
    "XS": 1,
    "S": 2,
    "M": 3,
    "L": 5,
    "XL": 8,
}
_RANK_CHILD_SCALE = 100
_SLOT_REF_PATTERN = re.compile(r"^(?P<requirement>.+)#(?:slot[-_ ]*)?(?P<slot>\d+)$")


def _story_points_from_effort(estimated_effort: str) -> int:
    return _EFFORT_TO_STORY_POINTS[estimated_effort]


def _rank_to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parent_rank_from_existing(existing_active: list[UserStory]) -> int | None:
    ranks = [
        parsed
        for parsed in (_rank_to_int(story.rank) for story in existing_active)
        if parsed is not None
    ]
    derived_ranks = [rank for rank in ranks if rank >= (_RANK_CHILD_SCALE + 1)]
    if not derived_ranks:
        return None
    return max(1, min(derived_ranks) // _RANK_CHILD_SCALE)


def _refined_story_rank(
    *,
    parent_rank: int | None,
    existing_active: list[UserStory],
    slot: int,
) -> str:
    base = parent_rank or _parent_rank_from_existing(existing_active)
    if base is None:
        return str(slot)
    return str((base * _RANK_CHILD_SCALE) + slot)


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
        .order_by(
            cast("Any", WorkflowEvent.timestamp).desc(),
            cast("Any", WorkflowEvent.event_id).desc(),
        )
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
    parent_rank: int | None,
    validated: list[UserStoryItem],
    story_origin: str | None = None,
    accepted_spec_version_id: int | None = None,
) -> dict[str, str]:
    return {
        "normalized_requirement": normalized_req,
        "parent_rank": "" if parent_rank is None else str(parent_rank),
        "story_payload_hash": _story_request_payload_hash(validated),
        "story_origin": story_origin or "",
        "accepted_spec_version_id": (
            "" if accepted_spec_version_id is None else str(accepted_spec_version_id)
        ),
    }


def _story_patch_request_identity(  # noqa: PLR0913
    *,
    normalized_req: str,
    parent_rank: int | None,
    validated: UserStoryItem,
    target_story_id: int,
    target_refinement_slot: int,
    story_origin: str | None = None,
    accepted_spec_version_id: int | None = None,
) -> dict[str, str]:
    identity = _story_save_request_identity(
        normalized_req=normalized_req,
        parent_rank=parent_rank,
        validated=[validated],
        story_origin=story_origin,
        accepted_spec_version_id=accepted_spec_version_id,
    )
    identity.update(
        {
            "operation": "story_patch",
            "target_story_id": str(target_story_id),
            "target_refinement_slot": str(target_refinement_slot),
        }
    )
    return identity


def _idempotency_key_reused_response(
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
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
    if metadata.get("operation") == "story_patch":
        return False
    return (
        metadata.get("normalized_requirement")
        == request_identity["normalized_requirement"]
        and str(metadata.get("parent_rank", "")) == request_identity["parent_rank"]
        and metadata.get("story_payload_hash") == request_identity["story_payload_hash"]
        and str(metadata.get("story_origin", "")) == request_identity["story_origin"]
        and str(metadata.get("accepted_spec_version_id", ""))
        == request_identity["accepted_spec_version_id"]
    )


def _story_patch_event_matches_request(
    event: WorkflowEvent,
    *,
    request_identity: dict[str, str],
) -> bool:
    metadata = _metadata_json(event.event_metadata)
    for key, value in request_identity.items():
        if str(metadata.get(key, "")) != value:
            return False
    return True


def _story_replacement_protection_reasons(story: UserStory) -> list[str]:
    reasons: list[str] = []
    if len(story.sprints or []) > 0:
        reasons.append("linked_sprint")
    status_value = getattr(story.status, "value", story.status)
    if story.status != StoryStatus.TO_DO and status_value != StoryStatus.TO_DO.value:
        reasons.append("status_progressed")
    return reasons


def _story_item_modified(story: UserStory, item: UserStoryItem) -> bool:
    ac_text = _format_acceptance_criteria(item.acceptance_criteria)
    points = _story_points_from_effort(item.estimated_effort)
    return (
        story.title != item.story_title
        or story.story_description != item.statement
        or story.acceptance_criteria != ac_text
        or story.story_points != points
    )


def _story_replacement_blockers(
    stories: list[UserStory],
    validated: list[UserStoryItem],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    validated_by_slot = dict(enumerate(validated, start=1))
    for story in stories:
        reasons: list[str] = []
        protection_reasons = _story_replacement_protection_reasons(story)
        status_value = getattr(story.status, "value", story.status)
        if protection_reasons:
            slot = story.refinement_slot
            if slot is None or slot not in validated_by_slot:
                reasons.extend(protection_reasons)
            else:
                item = validated_by_slot[slot]
                if _story_item_modified(story, item):
                    reasons.extend(protection_reasons)
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


def _story_patch_target_mismatch_response(
    input_data: SaveStoryPatchInput,
    *,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "success": False,
        "product_id": input_data.product_id,
        "parent_requirement": input_data.parent_requirement,
        "idempotency_key": input_data.idempotency_key,
        "idempotency_replayed": False,
        "error_code": "STORY_PATCH_TARGET_MISMATCH",
        "error": message,
        "details": details,
    }


def _active_stories_for_requirement(
    session: Session,
    *,
    product_id: int,
    normalized_req: str,
    story_origin: str | None = None,
    accepted_spec_version_id: int | None = None,
) -> list[UserStory]:
    statement = (
        select(UserStory)
        .where(UserStory.product_id == product_id)
        .where(UserStory.source_requirement == normalized_req)
        .where(UserStory.is_superseded == False)  # noqa: E712
    )
    if story_origin is not None:
        statement = statement.where(UserStory.story_origin == story_origin)
    if accepted_spec_version_id is not None:
        statement = statement.where(
            UserStory.accepted_spec_version_id == accepted_spec_version_id
        )
    return list(
        session.exec(
            statement.order_by(
                cast("Any", UserStory.refinement_slot),
                cast("Any", UserStory.story_id),
            )
        ).all()
    )


def _active_stories_for_product(
    session: Session,
    *,
    product_id: int,
) -> list[UserStory]:
    return list(
        session.exec(
            select(UserStory)
            .where(UserStory.product_id == product_id)
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(
                cast("Any", UserStory.source_requirement),
                cast("Any", UserStory.refinement_slot),
            )
        ).all()
    )


def _replay_response(
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
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
        "dependency_proposed_count": metadata.get("dependency_proposed_count", 0),
        "dependency_warnings": metadata.get("dependency_warnings", []),
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


def _upsert_refined_story(  # noqa: PLR0913
    session: Session,
    *,
    linkage: tuple[int, str],
    slot: int,
    item: UserStoryItem,
    existing: UserStory | None,
    rank: str,
    story_origin: str,
    accepted_spec_version_id: int | None,
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
        existing.story_origin = story_origin
        existing.is_refined = True
        existing.is_superseded = False
        existing.story_points = _story_points_from_effort(item.estimated_effort)
        existing.rank = rank
        if accepted_spec_version_id is not None:
            existing.accepted_spec_version_id = accepted_spec_version_id
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
        story_origin=story_origin,
        is_refined=True,
        is_superseded=False,
        story_points=_story_points_from_effort(item.estimated_effort),
        rank=rank,
        ac_update_reason="user_story_refinement",
        accepted_spec_version_id=accepted_spec_version_id,
    )
    session.add(story)
    session.flush()
    story_id = story.story_id
    if story_id is None:
        raise RuntimeError("Story ID was not generated.")
    return story_id, "created"


def _story_save_metadata(  # noqa: PLR0913
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
    request_identity: dict[str, str],
    updated_ids: list[int],
    created_ids: list[int],
    superseded_ids: list[int],
    story_ids_by_slot: list[dict[str, int]],
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
        "story_ids_by_slot": story_ids_by_slot,
        "superseded_story_ids": superseded_ids,
    }


def _success_response(
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
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
        "dependency_proposed_count": metadata.get("dependency_proposed_count", 0),
        "dependency_warnings": metadata.get("dependency_warnings", []),
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
    story_ids_by_slot: list[dict[str, int]] = []
    existing_by_slot = {
        story.refinement_slot: story
        for story in existing_active
        if story.refinement_slot is not None
    }
    story_origin = input_data.story_origin or "refined"

    for idx, item in enumerate(validated, start=1):
        rank = _refined_story_rank(
            parent_rank=input_data.parent_rank,
            existing_active=existing_active,
            slot=idx,
        )
        existing = existing_by_slot.get(idx)
        if (
            existing is not None
            and _story_replacement_protection_reasons(existing)
            and not _story_item_modified(existing, item)
        ):
            continue
        story_id, action = _upsert_refined_story(
            session,
            linkage=(input_data.product_id, normalized_req),
            slot=idx,
            item=item,
            existing=existing,
            rank=rank,
            story_origin=story_origin,
            accepted_spec_version_id=input_data.accepted_spec_version_id,
        )
        target = created_ids if action == "created" else updated_ids
        target.append(story_id)
        story_ids_by_slot.append({"slot": idx, "story_id": story_id})

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
            parent_rank=input_data.parent_rank,
            validated=validated,
            story_origin=input_data.story_origin,
            accepted_spec_version_id=input_data.accepted_spec_version_id,
        ),
        updated_ids=updated_ids,
        created_ids=created_ids,
        superseded_ids=superseded_ids,
        story_ids_by_slot=story_ids_by_slot,
    )


def _resolve_story_patch_target(
    *,
    input_data: SaveStoryPatchInput,
    existing_active: list[UserStory],
) -> tuple[UserStory | None, dict[str, Any] | None]:
    if input_data.target_story_id is not None:
        matches = [
            story
            for story in existing_active
            if story.story_id == input_data.target_story_id
        ]
    else:
        matches = [
            story
            for story in existing_active
            if story.refinement_slot == input_data.target_refinement_slot
        ]

    if len(matches) != 1:
        return None, _story_patch_target_mismatch_response(
            input_data,
            message="Story patch target does not belong to the requested requirement.",
            details={
                "target_story_id": input_data.target_story_id,
                "target_refinement_slot": input_data.target_refinement_slot,
            },
        )

    target = matches[0]
    if target.story_id is None or target.refinement_slot is None:
        return None, _story_patch_target_mismatch_response(
            input_data,
            message="Story patch target is missing stable story linkage.",
            details={
                "target_story_id": target.story_id,
                "target_refinement_slot": target.refinement_slot,
            },
        )
    return target, None


def _persist_target_story_patch(  # noqa: PLR0913
    session: Session,
    *,
    input_data: SaveStoryPatchInput,
    normalized_req: str,
    item: UserStoryItem,
    target: UserStory,
    existing_active: list[UserStory],
    request_identity: dict[str, str],
) -> dict[str, Any]:
    target_slot = int(target.refinement_slot or 0)
    story_origin = input_data.story_origin or "refined"
    story_id, action = _upsert_refined_story(
        session,
        linkage=(input_data.product_id, normalized_req),
        slot=target_slot,
        item=item,
        existing=target,
        rank=_refined_story_rank(
            parent_rank=input_data.parent_rank,
            existing_active=existing_active,
            slot=target_slot,
        ),
        story_origin=story_origin,
        accepted_spec_version_id=input_data.accepted_spec_version_id,
    )
    updated_ids = [story_id] if action == "updated" else []
    created_ids = [story_id] if action == "created" else []
    metadata = _story_save_metadata(
        input_data=input_data,
        request_identity=request_identity,
        updated_ids=updated_ids,
        created_ids=created_ids,
        superseded_ids=[],
        story_ids_by_slot=[{"slot": target_slot, "story_id": story_id}],
    )
    metadata["operation"] = "story_patch"
    metadata["target_story_id"] = story_id
    metadata["target_refinement_slot"] = target_slot
    return metadata


def _dependency_candidate_failure_response(
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
    code: str,
    story_id: int,
    candidate: StoryDependencyCandidate,
    message: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "product_id": input_data.product_id,
        "parent_requirement": input_data.parent_requirement,
        "idempotency_key": input_data.idempotency_key,
        "error_code": code,
        "error": message,
        "story_id": story_id,
        "prerequisite_ref": candidate.prerequisite_ref,
        "confidence": candidate.confidence,
    }


def _dependency_candidate_warning(
    *,
    code: str,
    story_id: int,
    candidate: StoryDependencyCandidate,
    message: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "story_id": story_id,
        "prerequisite_ref": candidate.prerequisite_ref,
        "confidence": candidate.confidence,
    }


def _dependency_candidate_finding(
    *,
    code: str,
    severity: str,
    message: str,
    story_context: dict[str, Any],
    candidate: StoryDependencyCandidate,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "story_id": story_context["story_id"],
        "prerequisite_ref": candidate.prerequisite_ref,
        "confidence": candidate.confidence,
        "affected_story_indexes": [story_context["story_index"]],
        "affected_story_titles": [story_context["story_title"]],
    }


def _dependency_candidate_failure_response_from_finding(
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
    finding: dict[str, Any],
) -> dict[str, Any]:
    return {
        "success": False,
        "product_id": input_data.product_id,
        "parent_requirement": input_data.parent_requirement,
        "idempotency_key": input_data.idempotency_key,
        "error_code": finding["code"],
        "error": finding["message"],
        "story_id": finding["story_id"],
        "prerequisite_ref": finding["prerequisite_ref"],
        "confidence": finding["confidence"],
    }


def _resolve_dependency_candidate(
    *,
    candidate: StoryDependencyCandidate,
    active_stories: list[UserStory],
    normalized_req: str,
    dependent_slot: int | None,
) -> list[UserStory]:
    ref = candidate.prerequisite_ref.strip()
    if ref.isdigit():
        story_id = int(ref)
        return [story for story in active_stories if story.story_id == story_id]

    normalized_ref = normalize_requirement_key(ref)
    title_matches = [
        story
        for story in active_stories
        if normalize_requirement_key(story.title) == normalized_ref
    ]
    if title_matches:
        return title_matches

    slot_match = _SLOT_REF_PATTERN.match(ref)
    if slot_match:
        requirement_key = normalize_requirement_key(slot_match.group("requirement"))
        slot = int(slot_match.group("slot"))
        slot_matches = [
            story
            for story in active_stories
            if story.source_requirement == requirement_key
            and story.refinement_slot == slot
        ]
        if slot_matches:
            return slot_matches

    if normalized_ref == normalized_req and dependent_slot is not None:
        return [
            story
            for story in active_stories
            if story.source_requirement == normalized_req
            and story.refinement_slot is not None
            and story.refinement_slot < dependent_slot
        ]

    return []


def _post_save_dependency_reference_stories(  # noqa: PLR0913
    session: Session,
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
    normalized_req: str,
    validated: list[UserStoryItem],
    story_ids_by_slot: list[dict[str, int]] | None,
    slot_items: list[tuple[int, UserStoryItem]] | None = None,
) -> tuple[list[UserStory], dict[int, int]]:
    active_stories = _active_stories_for_product(
        session,
        product_id=input_data.product_id,
    )
    current_active = [
        story for story in active_stories if story.source_requirement == normalized_req
    ]
    existing_by_slot = {
        story.refinement_slot: story
        for story in current_active
        if story.refinement_slot is not None
    }
    supplied_ids = {
        int(item["slot"]): int(item["story_id"])
        for item in story_ids_by_slot or []
        if item.get("slot") is not None and item.get("story_id") is not None
    }
    reference_stories = [
        story for story in active_stories if story.source_requirement != normalized_req
    ]
    dependent_story_ids: dict[int, int] = {}
    items = slot_items if slot_items is not None else list(enumerate(validated, start=1))
    for slot, item in items:
        existing = existing_by_slot.get(slot)
        story_id = supplied_ids.get(slot)
        if story_id is None and existing is not None:
            story_id = existing.story_id
        if story_id is None:
            story_id = -slot
        dependent_story_ids[slot] = int(story_id)
        synthetic = UserStory(
            product_id=input_data.product_id,
            title=item.story_title,
            story_description=item.statement,
            acceptance_criteria=_format_acceptance_criteria(item.acceptance_criteria),
            status=StoryStatus.TO_DO,
            story_points=_story_points_from_effort(item.estimated_effort),
            rank=_refined_story_rank(
                parent_rank=input_data.parent_rank,
                existing_active=current_active,
                slot=slot,
            ),
            source_requirement=normalized_req,
            refinement_slot=slot,
            story_origin=input_data.story_origin or "refined",
            is_refined=True,
            is_superseded=False,
            accepted_spec_version_id=input_data.accepted_spec_version_id,
        )
        synthetic.story_id = int(story_id)
        reference_stories.append(synthetic)
    return reference_stories, dependent_story_ids


def evaluate_dependency_candidates(  # noqa: PLR0912, PLR0913
    input_data: SaveStoriesInput | SaveStoryPatchInput,
    *,
    session: Session | None = None,
    normalized_req: str | None = None,
    validated: list[UserStoryItem] | None = None,
    story_ids_by_slot: list[dict[str, int]] | None = None,
    slot_items: list[tuple[int, UserStoryItem]] | None = None,
) -> dict[str, Any]:
    """Return read-only dependency candidate findings before persistence."""
    owns_session = session is None
    if validated is None:
        raw_stories = (
            input_data.stories
            if isinstance(input_data, SaveStoriesInput)
            else [input_data.story]
        )
        validated, validation_errors = _validate_story_items(raw_stories)
        if validation_errors:
            return {
                "success": False,
                "error": f"Validation errors: {'; '.join(validation_errors)}",
                "blocking_findings": [],
                "warning_findings": [],
            }
    if (
        isinstance(input_data, SaveStoryPatchInput)
        and slot_items is None
        and input_data.target_refinement_slot is not None
        and validated
    ):
        slot_items = [(input_data.target_refinement_slot, validated[0])]
    if normalized_req is None:
        normalized_req = normalize_requirement_key(input_data.parent_requirement)

    active_session = session or Session(get_engine())
    try:
        active_stories, dependent_story_ids = _post_save_dependency_reference_stories(
            active_session,
            input_data=input_data,
            normalized_req=normalized_req,
            validated=validated,
            story_ids_by_slot=story_ids_by_slot,
            slot_items=slot_items,
        )
        blocking_findings: list[dict[str, Any]] = []
        warning_findings: list[dict[str, Any]] = []
        items = slot_items if slot_items is not None else list(enumerate(validated, start=1))
        for slot, item in items:
            story_id = dependent_story_ids[slot]
            for candidate in item.dependency_candidates:
                try:
                    matches = _resolve_dependency_candidate(
                        candidate=candidate,
                        active_stories=active_stories,
                        normalized_req=normalized_req,
                        dependent_slot=slot,
                    )
                except (AttributeError, ValueError) as exc:
                    code = "STORY_DEPENDENCY_CANDIDATE_RESOLUTION_FAILED"
                    message = f"Could not resolve dependency candidate: {exc}"
                else:
                    code = ""
                    message = ""
                    if not matches:
                        code = "STORY_DEPENDENCY_CANDIDATE_UNRESOLVED"
                        message = (
                            "Dependency candidate did not resolve to an active story."
                        )
                    elif len(matches) > 1:
                        code = "STORY_DEPENDENCY_CANDIDATE_AMBIGUOUS"
                        message = (
                            "Dependency candidate resolved to multiple active stories."
                        )
                    elif matches[0].story_id == story_id:
                        code = "STORY_DEPENDENCY_CANDIDATE_SELF_EDGE"
                        message = "Dependency candidate resolves to the same story."
                if not code:
                    continue
                severity = (
                    "blocking" if candidate.confidence == "explicit" else "warning"
                )
                finding = _dependency_candidate_finding(
                    code=code,
                    severity=severity,
                    message=message,
                    story_context={
                        "story_id": story_id,
                        "story_index": slot,
                        "story_title": item.story_title,
                    },
                    candidate=candidate,
                )
                target = (
                    blocking_findings
                    if candidate.confidence == "explicit"
                    else warning_findings
                )
                target.append(finding)
        return {
            "success": True,
            "blocking_findings": blocking_findings,
            "warning_findings": warning_findings,
        }
    finally:
        if owns_session:
            active_session.close()


def _purge_stale_story_writer_proposals(
    session: Session,
    *,
    product_id: int,
    story_ids: list[int],
) -> int:
    if not story_ids:
        return 0
    stale_edges = session.exec(
        select(UserStoryDependency)
        .where(UserStoryDependency.product_id == product_id)
        .where(cast("Any", UserStoryDependency.dependent_story_id).in_(story_ids))
        .where(UserStoryDependency.status == "proposed")
        .where(UserStoryDependency.source == "story_writer")
    ).all()
    for edge in stale_edges:
        session.delete(edge)
    session.flush()
    return len(stale_edges)


def _dependency_edges_by_pair(
    session: Session,
    *,
    product_id: int,
) -> dict[tuple[int, int], UserStoryDependency]:
    rows = session.exec(
        select(UserStoryDependency).where(UserStoryDependency.product_id == product_id)
    ).all()
    return {
        (edge.dependent_story_id, edge.prerequisite_story_id): edge for edge in rows
    }


def _persist_dependency_candidates(  # noqa: PLR0912, PLR0913, PLR0915
    session: Session,
    *,
    input_data: SaveStoriesInput | SaveStoryPatchInput,
    normalized_req: str,
    validated: list[UserStoryItem],
    metadata: dict[str, Any],
    slot_items: list[tuple[int, UserStoryItem]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    slot_to_story_id = {
        item["slot"]: item["story_id"] for item in metadata.get("story_ids_by_slot", [])
    }
    evaluation = evaluate_dependency_candidates(
        input_data,
        session=session,
        normalized_req=normalized_req,
        validated=validated,
        story_ids_by_slot=metadata.get("story_ids_by_slot", []),
        slot_items=slot_items,
    )
    blocking_findings = evaluation.get("blocking_findings")
    if isinstance(blocking_findings, list) and blocking_findings:
        return (
            _dependency_candidate_failure_response_from_finding(
                input_data=input_data,
                finding=blocking_findings[0],
            ),
            {},
        )
    current_story_ids = list(slot_to_story_id.values())
    purged_count = _purge_stale_story_writer_proposals(
        session,
        product_id=input_data.product_id,
        story_ids=current_story_ids,
    )
    active_stories = _active_stories_for_product(
        session,
        product_id=input_data.product_id,
    )
    existing_edges = _dependency_edges_by_pair(
        session,
        product_id=input_data.product_id,
    )
    warnings: list[dict[str, Any]] = []
    proposed_count = 0

    items = slot_items if slot_items is not None else list(enumerate(validated, start=1))
    for slot, item in items:
        story_id = slot_to_story_id.get(slot)
        if story_id is None:
            continue
        for candidate in item.dependency_candidates:
            try:
                matches = _resolve_dependency_candidate(
                    candidate=candidate,
                    active_stories=active_stories,
                    normalized_req=normalized_req,
                    dependent_slot=slot,
                )
            except (AttributeError, ValueError) as exc:
                message = f"Could not resolve dependency candidate: {exc}"
                if candidate.confidence == "explicit":
                    return (
                        _dependency_candidate_failure_response(
                            input_data=input_data,
                            code="STORY_DEPENDENCY_CANDIDATE_RESOLUTION_FAILED",
                            story_id=story_id,
                            candidate=candidate,
                            message=message,
                        ),
                        {},
                    )
                warnings.append(
                    _dependency_candidate_warning(
                        code="STORY_DEPENDENCY_CANDIDATE_RESOLUTION_FAILED",
                        story_id=story_id,
                        candidate=candidate,
                        message=message,
                    )
                )
                continue

            if not matches:
                message = "Dependency candidate did not resolve to an active story."
                if candidate.confidence == "explicit":
                    return (
                        _dependency_candidate_failure_response(
                            input_data=input_data,
                            code="STORY_DEPENDENCY_CANDIDATE_UNRESOLVED",
                            story_id=story_id,
                            candidate=candidate,
                            message=message,
                        ),
                        {},
                    )
                warnings.append(
                    _dependency_candidate_warning(
                        code="STORY_DEPENDENCY_CANDIDATE_UNRESOLVED",
                        story_id=story_id,
                        candidate=candidate,
                        message=message,
                    )
                )
                continue

            if len(matches) > 1:
                message = "Dependency candidate resolved to multiple active stories."
                if candidate.confidence == "explicit":
                    return (
                        _dependency_candidate_failure_response(
                            input_data=input_data,
                            code="STORY_DEPENDENCY_CANDIDATE_AMBIGUOUS",
                            story_id=story_id,
                            candidate=candidate,
                            message=message,
                        ),
                        {},
                    )
                warnings.append(
                    _dependency_candidate_warning(
                        code="STORY_DEPENDENCY_CANDIDATE_AMBIGUOUS",
                        story_id=story_id,
                        candidate=candidate,
                        message=message,
                    )
                )
                continue

            prerequisite_story_id = matches[0].story_id
            if prerequisite_story_id is None:
                continue
            if prerequisite_story_id == story_id:
                message = "Dependency candidate resolves to the same story."
                if candidate.confidence == "explicit":
                    return (
                        _dependency_candidate_failure_response(
                            input_data=input_data,
                            code="STORY_DEPENDENCY_CANDIDATE_SELF_EDGE",
                            story_id=story_id,
                            candidate=candidate,
                            message=message,
                        ),
                        {},
                    )
                warnings.append(
                    _dependency_candidate_warning(
                        code="STORY_DEPENDENCY_CANDIDATE_SELF_EDGE",
                        story_id=story_id,
                        candidate=candidate,
                        message=message,
                    )
                )
                continue

            pair = (story_id, prerequisite_story_id)
            existing_edge = existing_edges.get(pair)
            if existing_edge is not None and existing_edge.status == "active":
                warnings.append(
                    _dependency_candidate_warning(
                        code="STORY_DEPENDENCY_CANDIDATE_ALREADY_ACTIVE",
                        story_id=story_id,
                        candidate=candidate,
                        message="Dependency candidate is already active.",
                    )
                )
                continue
            if existing_edge is not None:
                existing_edge.status = "proposed"
                existing_edge.source = "story_writer"
                existing_edge.confidence = candidate.confidence
                existing_edge.reason = candidate.reason
                existing_edge.updated_at = datetime.now(UTC)
                session.add(existing_edge)
                proposed_count += 1
                continue

            edge = UserStoryDependency(
                product_id=input_data.product_id,
                dependent_story_id=story_id,
                prerequisite_story_id=prerequisite_story_id,
                status="proposed",
                source="story_writer",
                confidence=candidate.confidence,
                reason=candidate.reason,
            )
            session.add(edge)
            existing_edges[pair] = edge
            proposed_count += 1

    session.flush()
    return (
        None,
        {
            "dependency_proposed_count": proposed_count,
            "dependency_purged_stale_count": purged_count,
            "dependency_warnings": warnings,
        },
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
    input_data: SaveStoriesInput | SaveStoryPatchInput,
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


def save_stories_tool(  # noqa: PLR0911
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
            parent_rank=input_data.parent_rank,
            validated=validated,
            story_origin=input_data.story_origin,
            accepted_spec_version_id=input_data.accepted_spec_version_id,
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
            story_origin=input_data.story_origin
            if input_data.story_origin == "scope_extension"
            else None,
            accepted_spec_version_id=input_data.accepted_spec_version_id
            if input_data.story_origin == "scope_extension"
            else None,
        )
        blockers = _story_replacement_blockers(existing_active, validated)
        if blockers:
            return _unsafe_replacement_response(blockers)

        metadata = _persist_validated_stories(
            session,
            input_data=input_data,
            normalized_req=normalized_req,
            validated=validated,
            existing_active=existing_active,
        )
        dependency_failure, dependency_metadata = _persist_dependency_candidates(
            session,
            input_data=input_data,
            normalized_req=normalized_req,
            validated=validated,
            metadata=metadata,
        )
        if dependency_failure is not None:
            return dependency_failure
        metadata.update(dependency_metadata)
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


def save_story_patch_tool(  # noqa: PLR0911
    input_data: SaveStoryPatchInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Persist one targeted user story refinement to the database."""
    engine = get_engine()
    start_ts = time.perf_counter()

    with Session(engine) as session:
        _acquire_story_save_write_lock(session)

        product = session.exec(
            select(Product).where(Product.product_id == input_data.product_id)
        ).first()
        if not product:
            return {
                "success": False,
                "error": f"Product with ID {input_data.product_id} not found.",
            }

        validated, validation_errors = _validate_story_items([input_data.story])
        if validation_errors:
            return {
                "success": False,
                "error": f"Validation errors: {'; '.join(validation_errors)}",
                "valid_count": len(validated),
                "invalid_count": len(validation_errors),
            }
        item = validated[0]

        normalized_req = normalize_requirement_key(input_data.parent_requirement)
        existing_active = _active_stories_for_requirement(
            session,
            product_id=input_data.product_id,
            normalized_req=normalized_req,
            story_origin=input_data.story_origin
            if input_data.story_origin == "scope_extension"
            else None,
            accepted_spec_version_id=input_data.accepted_spec_version_id
            if input_data.story_origin == "scope_extension"
            else None,
        )
        target, target_error = _resolve_story_patch_target(
            input_data=input_data,
            existing_active=existing_active,
        )
        if target_error is not None:
            return target_error
        if target is None or target.story_id is None or target.refinement_slot is None:
            raise RuntimeError("Story patch target resolution failed.")

        protection_reasons = _story_replacement_protection_reasons(target)
        if protection_reasons:
            return _unsafe_replacement_response(
                [
                    {
                        "story_id": target.story_id,
                        "refinement_slot": target.refinement_slot,
                        "title": target.title,
                        "status": getattr(target.status, "value", target.status),
                        "reasons": protection_reasons,
                    }
                ]
            )

        request_identity = _story_patch_request_identity(
            normalized_req=normalized_req,
            parent_rank=input_data.parent_rank,
            validated=item,
            target_story_id=target.story_id,
            target_refinement_slot=target.refinement_slot,
            story_origin=input_data.story_origin,
            accepted_spec_version_id=input_data.accepted_spec_version_id,
        )
        previous_event = _find_story_save_event(
            session,
            product_id=input_data.product_id,
            idempotency_key=input_data.idempotency_key,
        )
        if previous_event is not None:
            if not _story_patch_event_matches_request(
                previous_event,
                request_identity=request_identity,
            ):
                return _idempotency_key_reused_response(input_data=input_data)
            return _replay_response(input_data=input_data, event=previous_event)

        metadata = _persist_target_story_patch(
            session,
            input_data=input_data,
            normalized_req=normalized_req,
            item=item,
            target=target,
            existing_active=existing_active,
            request_identity=request_identity,
        )
        slot_items = [(int(target.refinement_slot), item)]
        dependency_failure, dependency_metadata = _persist_dependency_candidates(
            session,
            input_data=input_data,
            normalized_req=normalized_req,
            validated=validated,
            metadata=metadata,
            slot_items=slot_items,
        )
        if dependency_failure is not None:
            return dependency_failure
        metadata.update(dependency_metadata)

        duration_seconds = _story_refinement_duration(
            tool_context=tool_context,
            start_ts=start_ts,
        )
        session_id = getattr(tool_context, "session_id", None) if tool_context else None
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.STORIES_SAVED,
                product_id=input_data.product_id,
                session_id=session_id,
                duration_seconds=float(duration_seconds),
                event_metadata=json.dumps(metadata),
            )
        )
        session.commit()

        _store_story_context(
            tool_context=tool_context,
            input_data=input_data,
            metadata=metadata,
        )
        print(
            f"\n\033[92m[Story Patch Saved]\033[0m "
            f"{metadata['saved_count']} stories for '{input_data.parent_requirement}' "
            f"(updated={metadata['updated_count']}, created={metadata['created_count']})"
        )

        return _success_response(input_data=input_data, metadata=metadata)


__all__ = [
    "SaveStoriesInput",
    "SaveStoryPatchInput",
    "save_stories_tool",
    "save_story_patch_tool",
]
