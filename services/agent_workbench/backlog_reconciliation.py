"""Canonical active Backlog reconciliation for agent-facing commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlmodel import Session, select

from models import db as model_db
from models.core import Product, SprintStory, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent

JsonDict = dict[str, Any]


class BacklogReconciliationError(Exception):
    """Raised when active backlog reconciliation cannot be performed safely."""

    def __init__(self, detail: str, *, details: JsonDict | None = None) -> None:
        """Store a user-facing detail string and structured diagnostics."""
        super().__init__(detail)
        self.detail = detail
        self.details = details or {}


def reconcile_active_backlog(
    *,
    project_id: int,
    idempotency_key: str,
) -> JsonDict:
    """Supersede obsolete active backlog_seed rows, preserving one active cohort."""
    normalized_key = idempotency_key.strip()
    if not normalized_key:
        message = "Backlog reconcile requires a non-empty idempotency key."
        raise BacklogReconciliationError(
            message,
            details={"missing": ["idempotency_key"]},
        )

    with Session(model_db.get_engine()) as session:
        if session.get(Product, project_id) is None:
            message = f"Project {project_id} not found."
            raise BacklogReconciliationError(
                message,
                details={"project_id": project_id},
            )

        replay = _idempotent_replay(
            session,
            project_id=project_id,
            idempotency_key=normalized_key,
        )
        if replay is not None:
            return replay

        active_stories = _active_stories(session, project_id)
        blocked = [
            _blocked_story_payload(session, story)
            for story in active_stories
            if _progression_reasons(session, story)
        ]
        if blocked:
            message = "BACKLOG_REPLACEMENT_BLOCKED"
            raise BacklogReconciliationError(
                message,
                details={
                    "project_id": project_id,
                    "blocked_count": len(blocked),
                    "blocked_stories": blocked,
                },
            )

        active_seed_rows = [
            story for story in active_stories if story.story_origin == "backlog_seed"
        ]
        keep_rows, strategy = _select_canonical_seed_cohort(session, active_seed_rows)
        keep_ids = {story.story_id for story in keep_rows if story.story_id is not None}
        obsolete_rows = [
            story
            for story in active_seed_rows
            if story.story_id is not None and story.story_id not in keep_ids
        ]

        replacement_by_slot = _replacement_by_slot(keep_rows)
        now = datetime.now(UTC)
        for story in obsolete_rows:
            story.is_superseded = True
            story.superseded_by_story_id = _replacement_story_id(
                replacement_by_slot,
                story,
            )
            story.updated_at = now
            session.add(story)

        result = {
            "project_id": project_id,
            "status": "reconciled" if obsolete_rows else "already_canonical",
            "strategy": strategy,
            "active_before": len(active_seed_rows),
            "active_after": len(keep_rows),
            "kept_story_ids": [
                story.story_id for story in keep_rows if story.story_id is not None
            ],
            "superseded_story_ids": [
                story.story_id for story in obsolete_rows if story.story_id is not None
            ],
            "superseded_count": len(obsolete_rows),
            "idempotency_key": normalized_key,
        }
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.BACKLOG_SAVED,
                product_id=project_id,
                event_metadata=json.dumps(
                    {
                        "action": "backlog_reconciled",
                        "idempotency_key": normalized_key,
                        "result": result,
                    },
                    sort_keys=True,
                ),
            )
        )
        session.commit()
        return result


def _active_stories(session: Session, project_id: int) -> list[UserStory]:
    rows = session.exec(
        select(UserStory)
        .where(UserStory.product_id == project_id)
        .where(UserStory.is_superseded == False)  # noqa: E712
    ).all()
    return sorted(rows, key=_created_story_key)


def _created_story_key(story: UserStory) -> tuple[datetime, int]:
    return (story.created_at, int(story.story_id or 0))


def _idempotent_replay(
    session: Session,
    *,
    project_id: int,
    idempotency_key: str,
) -> JsonDict | None:
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.product_id == project_id)
        .where(WorkflowEvent.event_type == WorkflowEventType.BACKLOG_SAVED)
    ).all()
    for event in events:
        metadata = _json_object(event.event_metadata)
        if metadata.get("action") != "backlog_reconciled":
            continue
        if metadata.get("idempotency_key") != idempotency_key:
            continue
        result = metadata.get("result")
        if isinstance(result, dict):
            replay = dict(result)
            replay["idempotent_replay"] = True
            return replay
    return None


def _json_object(value: str | None) -> JsonDict:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _progression_reasons(session: Session, story: UserStory) -> list[str]:
    reasons: list[str] = []
    if story.story_origin != "backlog_seed":
        reasons.append("story_origin_not_backlog_seed")
    if story.is_refined:
        reasons.append("is_refined")
    if story.acceptance_criteria:
        reasons.append("acceptance_criteria_present")
    if story.status != StoryStatus.TO_DO:
        reasons.append("status_not_to_do")
    if story.story_id is not None and _has_sprint_link(session, story.story_id):
        reasons.append("sprint_link_present")
    return reasons


def _has_sprint_link(session: Session, story_id: int) -> bool:
    statement = select(SprintStory).where(SprintStory.story_id == story_id)
    return session.exec(statement).first() is not None


def _story_status_value(story: UserStory) -> object:
    return story.status.value if hasattr(story.status, "value") else story.status


def _blocked_story_payload(session: Session, story: UserStory) -> JsonDict:
    return {
        "story_id": story.story_id,
        "title": story.title,
        "status": _story_status_value(story),
        "story_origin": story.story_origin,
        "is_refined": story.is_refined,
        "has_acceptance_criteria": bool(story.acceptance_criteria),
        "reasons": _progression_reasons(session, story),
    }


def _select_canonical_seed_cohort(
    session: Session,
    active_seed_rows: list[UserStory],
) -> tuple[list[UserStory], str]:
    if len(active_seed_rows) <= 1:
        return active_seed_rows, "single_or_empty_active_seed_set"

    latest_saved_count = _latest_saved_count(session, active_seed_rows)
    if latest_saved_count is not None and latest_saved_count < len(active_seed_rows):
        return active_seed_rows[-latest_saved_count:], "latest_backlog_saved_event"

    cohorts = _rank_reset_cohorts(active_seed_rows)
    if len(cohorts) > 1:
        return cohorts[-1], "rank_reset_cohort"

    return active_seed_rows, "already_canonical"


def _latest_saved_count(
    session: Session,
    active_seed_rows: list[UserStory],
) -> int | None:
    project_id = active_seed_rows[0].product_id if active_seed_rows else None
    if project_id is None:
        return None
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.product_id == project_id)
        .where(WorkflowEvent.event_type == WorkflowEventType.BACKLOG_SAVED)
    ).all()
    events = sorted(
        events,
        key=lambda event: (event.timestamp, int(event.event_id or 0)),
        reverse=True,
    )
    for event in events:
        metadata = _json_object(event.event_metadata)
        if metadata.get("action") == "backlog_reconciled":
            continue
        count = metadata.get("processed_count") or metadata.get("created_count")
        try:
            normalized_count = int(cast("Any", count))
        except (TypeError, ValueError):
            continue
        if normalized_count > 0:
            return normalized_count
    return None


def _rank_reset_cohorts(active_seed_rows: list[UserStory]) -> list[list[UserStory]]:
    cohorts: list[list[UserStory]] = []
    current: list[UserStory] = []
    previous_rank: int | None = None
    for story in active_seed_rows:
        rank = _rank_to_int(story.rank)
        if (
            current
            and rank is not None
            and previous_rank is not None
            and rank <= previous_rank
        ):
            cohorts.append(current)
            current = []
        current.append(story)
        if rank is not None:
            previous_rank = rank
    if current:
        cohorts.append(current)
    return cohorts


def _rank_to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _replacement_by_slot(keep_rows: list[UserStory]) -> dict[int, int]:
    replacements: dict[int, int] = {}
    for story in keep_rows:
        slot = story.refinement_slot or _rank_to_int(story.rank)
        if slot is None or story.story_id is None:
            continue
        replacements[slot] = story.story_id
    return replacements


def _replacement_story_id(
    replacement_by_slot: dict[int, int],
    story: UserStory,
) -> int | None:
    slot = story.refinement_slot or _rank_to_int(story.rank)
    if slot is None:
        return None
    return replacement_by_slot.get(slot)


__all__ = [
    "BacklogReconciliationError",
    "reconcile_active_backlog",
]
