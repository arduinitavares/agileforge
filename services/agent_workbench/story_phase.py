"""Agent workbench Story phase command runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.specs import SpecRegistry
from models.workflow import StoryArtifact, StoryArtifactDecision
from services.agent_workbench.fingerprints import canonical_hash
from services.contracts.story import (
    UserStoryItem,
    UserStoryWriterOutput,
)
from services.story_linkage import normalize_requirement_key
from services.story_rank import parse_story_rank
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.contracts import JsonObject


@dataclass(frozen=True)
class RecordStoryDraftInput:
    """Exact immutable values used to record one Story-set draft."""

    project_id: int
    requirement_id: str
    requirement_text: str
    requirement_rank: int
    roadmap_artifact_id: int
    roadmap_artifact_fingerprint: str
    canonical_content: JsonObject
    content_fingerprint: str
    supersedes_story_artifact_id: int | None
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class RecordStoryDecisionInput:
    """Exact append-only values used to decide one Story-set draft."""

    artifact: StoryArtifact
    decision: str
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: datetime


@dataclass(frozen=True)
class _StoryWriteContext:
    """Shared immutable inputs for deterministic Story row writes."""

    inputs: RecordStoryDraftInput
    normalized_requirement: str
    accepted_spec_version_id: int | None


def _validated_story_output(inputs: RecordStoryDraftInput) -> UserStoryWriterOutput:
    output = UserStoryWriterOutput.model_validate(inputs.canonical_content)
    if output.parent_requirement != inputs.requirement_text:
        message = "Story content does not target the exact Backlog requirement."
        raise ValueError(message)
    if canonical_hash(inputs.canonical_content) != inputs.content_fingerprint:
        message = "Story content fingerprint does not match canonical content."
        raise ValueError(message)
    normalized_requirement = normalize_requirement_key(inputs.requirement_text)
    if inputs.requirement_id != normalized_requirement:
        message = "Story requirement identity is not canonical."
        raise ValueError(message)
    return output


def _story_artifact_history(
    session: Session,
    inputs: RecordStoryDraftInput,
) -> tuple[StoryArtifact, ...]:
    artifacts = tuple(
        session.exec(
            select(StoryArtifact)
            .where(
                StoryArtifact.project_id == inputs.project_id,
                StoryArtifact.requirement_id == inputs.requirement_id,
            )
            .order_by(col(StoryArtifact.version_number))
        ).all()
    )
    expected_parent = artifacts[-1].story_artifact_id if artifacts else None
    if inputs.supersedes_story_artifact_id != expected_parent:
        message = "Story supersession does not match the current artifact."
        raise ValueError(message)
    return artifacts


def _active_requirement_stories(
    session: Session,
    inputs: RecordStoryDraftInput,
    normalized_requirement: str,
) -> tuple[UserStory, ...]:
    return tuple(
        session.exec(
            select(UserStory)
            .where(
                UserStory.project_id == inputs.project_id,
                UserStory.source_requirement == normalized_requirement,
                col(UserStory.is_superseded).is_(False),
            )
            .order_by(col(UserStory.refinement_slot))
        ).all()
    )


def _accepted_spec_version_id(session: Session, project_id: int) -> int | None:
    spec = session.exec(
        select(SpecRegistry)
        .where(
            SpecRegistry.project_id == project_id,
            SpecRegistry.status == "approved",
        )
        .order_by(col(SpecRegistry.spec_version_id).desc())
    ).first()
    return None if spec is None else spec.spec_version_id


def _story_persona(statement: str) -> str | None:
    if ", I want " not in statement:
        return None
    prefix = statement.split(", I want ", maxsplit=1)[0]
    return prefix.removeprefix("As a ").removeprefix("As an ").strip()


def _write_story_item(
    session: Session,
    context: _StoryWriteContext,
    slot: int,
    item: UserStoryItem,
    story: UserStory | None,
) -> int:
    rank = str((context.inputs.requirement_rank * 100) + slot)
    parse_story_rank(rank)
    acceptance_criteria = "\n".join(
        criterion if criterion.startswith("- ") else f"- {criterion}"
        for criterion in item.acceptance_criteria
    )
    if story is not None:
        linked = session.exec(
            select(SprintStory).where(SprintStory.story_id == story.story_id)
        ).first()
        changed = (
            story.title != item.story_title
            or story.story_description != item.statement
            or story.acceptance_criteria != acceptance_criteria
        )
        if linked is not None and changed:
            message = "Story correction is unsafe after Sprint work exists."
            raise ValueError(message)
    else:
        story = UserStory(
            project_id=context.inputs.project_id,
            title=item.story_title,
            source_requirement=context.normalized_requirement,
            refinement_slot=slot,
            story_origin="refined",
            created_at=context.inputs.recorded_at,
        )
    story.title = item.story_title
    story.story_description = item.statement
    story.acceptance_criteria = acceptance_criteria
    story.persona = _story_persona(item.statement.strip())
    story.status = StoryStatus.TO_DO
    story.story_points = {"XS": 1, "S": 2, "M": 3, "L": 5, "XL": 8}[
        item.estimated_effort
    ]
    story.rank = rank
    story.source_requirement = context.normalized_requirement
    story.refinement_slot = slot
    story.story_origin = "refined"
    story.is_refined = True
    story.is_superseded = False
    story.accepted_spec_version_id = context.accepted_spec_version_id
    story.ac_updated_at = context.inputs.recorded_at
    story.ac_update_reason = "user_story_refinement"
    story.updated_at = context.inputs.recorded_at
    session.add(story)
    session.flush()
    if story.story_id is None:
        message = "Story row did not receive a durable identity."
        raise ValueError(message)
    return story.story_id


def _supersede_removed_story_rows(
    session: Session,
    existing: tuple[UserStory, ...],
    retained_slots: set[int],
    recorded_at: datetime,
) -> None:
    for story in existing:
        if story.refinement_slot in retained_slots:
            continue
        linked = session.exec(
            select(SprintStory).where(SprintStory.story_id == story.story_id)
        ).first()
        if linked is not None:
            message = "Story replacement is unsafe after Sprint work exists."
            raise ValueError(message)
        story.is_superseded = True
        story.updated_at = recorded_at
        session.add(story)


def _add_story_artifact(
    session: Session,
    inputs: RecordStoryDraftInput,
    artifacts: tuple[StoryArtifact, ...],
    story_ids: list[int],
) -> StoryArtifact:
    row = StoryArtifact(
        project_id=inputs.project_id,
        requirement_id=inputs.requirement_id,
        roadmap_artifact_id=inputs.roadmap_artifact_id,
        roadmap_artifact_fingerprint=inputs.roadmap_artifact_fingerprint,
        version_number=len(artifacts) + 1,
        canonical_content_json=canonical_json(inputs.canonical_content),
        content_fingerprint=inputs.content_fingerprint,
        story_ids_json=canonical_json(sorted(story_ids)),
        supersedes_story_artifact_id=inputs.supersedes_story_artifact_id,
        created_by=inputs.actor,
        created_at=inputs.recorded_at,
    )
    session.add(row)
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.STORIES_SAVED,
            timestamp=inputs.recorded_at,
            project_id=inputs.project_id,
            duration_seconds=0.0,
            event_metadata=canonical_json(
                {
                    "action": "story_artifact_recorded",
                    "content_fingerprint": inputs.content_fingerprint,
                    "requirement_id": inputs.requirement_id,
                    "story_ids": sorted(story_ids),
                }
            ),
        )
    )
    session.flush()
    return row


def record_story_draft_in_session(
    session: Session,
    *,
    inputs: RecordStoryDraftInput,
) -> StoryArtifact:
    """Persist deterministic Story linkage and one immutable Story-set artifact."""
    output = _validated_story_output(inputs)
    artifacts = _story_artifact_history(session, inputs)
    normalized_requirement = normalize_requirement_key(inputs.requirement_text)
    existing = _active_requirement_stories(session, inputs, normalized_requirement)
    existing_by_slot = {
        item.refinement_slot: item
        for item in existing
        if item.refinement_slot is not None
    }
    context = _StoryWriteContext(
        inputs=inputs,
        normalized_requirement=normalized_requirement,
        accepted_spec_version_id=_accepted_spec_version_id(
            session,
            inputs.project_id,
        ),
    )
    story_ids = [
        _write_story_item(session, context, slot, item, existing_by_slot.get(slot))
        for slot, item in enumerate(output.user_stories, start=1)
    ]
    retained_slots = set(range(1, len(output.user_stories) + 1))
    _supersede_removed_story_rows(
        session,
        existing,
        retained_slots,
        inputs.recorded_at,
    )
    return _add_story_artifact(session, inputs, artifacts, story_ids)


def record_story_decision_in_session(
    session: Session,
    *,
    inputs: RecordStoryDecisionInput,
) -> StoryArtifactDecision:
    """Append one terminal decision for exact immutable Story content."""
    artifact = inputs.artifact
    if inputs.decision not in {"accepted", "rejected", "feedback"}:
        message = "Story decision is invalid."
        raise ValueError(message)
    artifact_id = artifact.story_artifact_id
    if artifact_id is None:
        message = "Story artifact has no durable identity."
        raise ValueError(message)
    existing = session.exec(
        select(StoryArtifactDecision).where(
            StoryArtifactDecision.project_id == artifact.project_id,
            StoryArtifactDecision.story_artifact_id == artifact_id,
        )
    ).first()
    if existing is not None:
        message = "Story artifact already has a terminal decision."
        raise ValueError(message)
    row = StoryArtifactDecision(
        project_id=artifact.project_id,
        story_artifact_id=artifact_id,
        artifact_fingerprint=artifact.content_fingerprint,
        decision=inputs.decision,
        rationale=inputs.rationale,
        reviewer=inputs.reviewer,
        idempotency_key=inputs.idempotency_key,
        decided_at=inputs.decided_at,
    )
    session.add(row)
    session.flush()
    return row


def repair_story_readiness_in_session(
    session: Session,
    *,
    project_id: int,
    repairs: tuple[tuple[int, int, str], ...],
    repaired_at: datetime,
) -> tuple[int, ...]:
    """Repair exact Story points and rank under the caller-owned transaction."""
    for _story_id, _story_points, rank in repairs:
        parse_story_rank(rank)
    _assert_repair_readiness_safe_in_session(session, project_id=project_id)
    story_ids = tuple(item[0] for item in repairs)
    rows = session.exec(
        select(UserStory).where(col(UserStory.story_id).in_(story_ids))
    ).all()
    by_id = {item.story_id: item for item in rows if item.story_id is not None}
    if set(by_id) != set(story_ids):
        message = "Story readiness repair does not target exact Project stories."
        raise ValueError(message)
    for story_id, story_points, rank in repairs:
        story = by_id[story_id]
        if (
            story.project_id != project_id
            or story.is_superseded
            or not story.is_refined
        ):
            message = "Story readiness repair targets an inactive Story."
            raise ValueError(message)
        story.story_points = story_points
        story.rank = rank
        story.updated_at = repaired_at
        session.add(story)
    session.flush()
    return tuple(sorted(story_ids))


def _assert_repair_readiness_safe_in_session(
    session: Session,
    *,
    project_id: int,
) -> None:
    """Block Story readiness repair if refined rows already feed any Sprint."""
    active_story_ids = [
        story_id
        for story_id in session.exec(
            select(UserStory.story_id).where(
                UserStory.project_id == project_id,
                UserStory.is_refined == True,  # noqa: E712
                UserStory.is_superseded == False,  # noqa: E712
            )
        ).all()
        if story_id is not None
    ]
    if not active_story_ids:
        return

    sprint_link = session.exec(
        select(SprintStory.story_id)
        .join(Sprint, col(Sprint.sprint_id) == col(SprintStory.sprint_id))
        .where(
            Sprint.project_id == project_id,
            col(SprintStory.story_id).in_(active_story_ids),
        )
    ).first()
    if sprint_link is not None:
        message = "Story readiness repair is unsafe after Sprint work exists."
        raise ValueError(message)
