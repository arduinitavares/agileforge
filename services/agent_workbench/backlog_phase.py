"""Agent workbench Backlog phase command runner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session, col, func, select

from models.core import Project, SprintStory, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority
from models.workflow import BacklogArtifact, BacklogArtifactDecision
from services.contracts.backlog import (
    BacklogItem,
    OutputSchema,
)
from services.story_linkage import normalize_requirement_key
from workflow.contracts import JsonObject
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from datetime import datetime

_JSON_OBJECT = TypeAdapter(JsonObject)


def record_backlog_draft_in_session(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
    product_goal_artifact_id: int,
    product_goal_fingerprint: str,
    canonical_content: JsonObject,
    content_fingerprint: str,
    supersedes_backlog_artifact_id: int | None,
    artifact_id: int,
    actor: str,
    recorded_at: datetime,
) -> BacklogArtifact:
    """Validate and append one immutable Backlog artifact in caller transaction."""
    if session.get(Project, project_id) is None:
        message = f"Project {project_id} not found."
        raise ValueError(message)
    validated = OutputSchema.model_validate(canonical_content)
    normalized = _JSON_OBJECT.validate_python(validated.model_dump(mode="json"))
    if not validated.is_complete or not validated.backlog_items:
        message = "Backlog output is incomplete and cannot enter review."
        raise ValueError(message)
    if normalized != canonical_content:
        message = "Backlog content must be the exact host-validated canonical output."
        raise ValueError(message)
    if canonical_hash(canonical_content) != content_fingerprint:
        message = "Backlog content fingerprint does not match canonical content."
        raise ValueError(message)

    parent: BacklogArtifact | None = None
    if supersedes_backlog_artifact_id is not None:
        parent = session.exec(
            select(BacklogArtifact).where(
                col(BacklogArtifact.project_id) == project_id,
                col(BacklogArtifact.backlog_artifact_id)
                == supersedes_backlog_artifact_id,
            )
        ).one_or_none()
        if parent is None:
            message = "Backlog supersession parent does not belong to this Project."
            raise ValueError(message)
        if (
            parent.authority_id != authority_id
            or parent.authority_fingerprint != authority_fingerprint
            or parent.product_goal_artifact_id != product_goal_artifact_id
            or parent.product_goal_fingerprint != product_goal_fingerprint
        ):
            message = "Backlog supersession parent has different delivery lineage."
            raise ValueError(message)

    version_number = (
        session.exec(
            select(func.count())
            .select_from(BacklogArtifact)
            .where(col(BacklogArtifact.project_id) == project_id)
        ).one()
        + 1
    )
    row = BacklogArtifact(
        backlog_artifact_id=artifact_id,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        product_goal_artifact_id=product_goal_artifact_id,
        product_goal_fingerprint=product_goal_fingerprint,
        version_number=version_number,
        canonical_content_json=canonical_json(canonical_content),
        content_fingerprint=content_fingerprint,
        supersedes_backlog_artifact_id=(
            None if parent is None else parent.backlog_artifact_id
        ),
        created_by=actor,
        created_at=recorded_at,
    )
    session.add(row)
    session.flush()
    return row


def record_backlog_decision_in_session(  # noqa: PLR0913
    session: Session,
    *,
    artifact: BacklogArtifact,
    decision: str,
    rationale: str,
    reviewer: str,
    idempotency_key: str,
    decided_at: datetime,
) -> BacklogArtifactDecision:
    """Append one exact decision and install accepted Backlog stories atomically."""
    existing = session.exec(
        select(BacklogArtifactDecision).where(
            col(BacklogArtifactDecision.project_id) == artifact.project_id,
            col(BacklogArtifactDecision.backlog_artifact_id)
            == artifact.backlog_artifact_id,
        )
    ).one_or_none()
    if existing is not None:
        message = "Backlog artifact already has a terminal review decision."
        raise ValueError(message)
    if decision == "accepted":
        persist_accepted_backlog_in_session(
            session,
            artifact=artifact,
            idempotency_key=idempotency_key,
            accepted_at=decided_at,
        )
    row = BacklogArtifactDecision(
        project_id=artifact.project_id,
        backlog_artifact_id=artifact.backlog_artifact_id,
        artifact_fingerprint=artifact.content_fingerprint,
        decision=decision,
        rationale=rationale,
        reviewer=reviewer,
        idempotency_key=idempotency_key,
        decided_at=decided_at,
    )
    session.add(row)
    session.flush()
    return row


def persist_accepted_backlog_in_session(
    session: Session,
    *,
    artifact: BacklogArtifact,
    idempotency_key: str,
    accepted_at: datetime,
) -> tuple[int, ...]:
    """Validate and install accepted Backlog content without owning transaction."""
    content = _JSON_OBJECT.validate_json(artifact.canonical_content_json)
    validated = OutputSchema.model_validate(content)
    authority = session.get(CompiledSpecAuthority, artifact.authority_id)
    if authority is None:
        message = "Accepted Backlog authority does not exist."
        raise ValueError(message)
    active_stories = session.exec(
        select(UserStory)
        .where(col(UserStory.project_id) == artifact.project_id)
        .where(col(UserStory.is_superseded).is_(False))
    ).all()
    blocked = [
        story for story in active_stories if _blocks_backlog_replacement(session, story)
    ]
    if blocked:
        blocked_ids = tuple(
            story.story_id for story in blocked if story.story_id is not None
        )
        message = f"BACKLOG_REPLACEMENT_BLOCKED: {blocked_ids}"
        raise ValueError(message)
    for story in active_stories:
        if story.story_origin == "backlog_seed":
            story.is_superseded = True
            story.updated_at = accepted_at
            session.add(story)

    created_ids: list[int] = []
    for item in validated.backlog_items:
        story = _story_from_validated_backlog_item(
            artifact.project_id,
            item,
            accepted_spec_version_id=authority.spec_version_id,
        )
        session.add(story)
        session.flush()
        if story.story_id is not None:
            created_ids.append(story.story_id)
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_SAVED,
            project_id=artifact.project_id,
            timestamp=accepted_at,
            event_metadata=canonical_json(
                {
                    "action": "backlog_artifact_accepted",
                    "idempotency_key": idempotency_key,
                    "backlog_artifact_id": artifact.backlog_artifact_id,
                    "artifact_fingerprint": artifact.content_fingerprint,
                    "approved_artifact_fingerprint": artifact.content_fingerprint,
                    "authority_id": artifact.authority_id,
                    "authority_fingerprint": artifact.authority_fingerprint,
                    "created_story_ids": created_ids,
                    "created_count": len(created_ids),
                }
            ),
        )
    )
    session.flush()
    return tuple(created_ids)


def _story_from_validated_backlog_item(
    project_id: int,
    item: BacklogItem,
    *,
    accepted_spec_version_id: int,
) -> UserStory:
    effort_points = {"S": 1, "M": 3, "L": 5, "XL": 8}
    return UserStory(
        title=item.requirement,
        project_id=project_id,
        status=StoryStatus.TO_DO,
        rank=str(item.priority),
        story_points=effort_points[item.estimated_effort],
        story_description=item.justification,
        acceptance_criteria=None,
        source_requirement=normalize_requirement_key(item.requirement),
        refinement_slot=item.priority,
        story_origin="backlog_seed",
        is_refined=False,
        is_superseded=False,
        accepted_spec_version_id=accepted_spec_version_id,
    )


def _blocks_backlog_replacement(session: Session, story: UserStory) -> bool:
    """Return whether a Story has progressed beyond replaceable seed state."""
    if story.story_origin != "backlog_seed":
        return True
    if story.is_refined or story.acceptance_criteria:
        return True
    if story.status != StoryStatus.TO_DO:
        return True
    if story.story_id is None:
        return False
    return (
        session.exec(
            select(SprintStory).where(SprintStory.story_id == story.story_id)
        ).first()
        is not None
    )
