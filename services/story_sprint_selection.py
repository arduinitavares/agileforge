# services/story_sprint_selection.py
"""Append-only human Sprint-selection state for exact accepted Stories."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import (
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, UserStory
from models.enums import SprintStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import (
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintStart,
)
from services.specs.story_validation_service import (
    StoryValidationReadinessError,
    require_current_story_validation_evidence,
    require_story_ready_for_sprint,
)
from workflow.contracts import FrozenModel
from workflow.fingerprints import canonical_hash, canonical_json

type SprintSelectionState = Literal["unselected", "selected", "deferred"]
type StructuralEligibilityStatus = Literal["eligible", "ineligible", "stale"]
type StorySprintSelectionIntent = Literal["select", "remove", "defer"]
type Sha256Fingerprint = Annotated[
    str,
    Field(pattern=r"^sha256:[0-9a-f]{64}$"),
]

_INT_LIST = TypeAdapter(list[int])
_LIFECYCLE_LOCKED = "SELECTION_LIFECYCLE_LOCKED"
_STORY_NOT_FOUND = "STORY_NOT_FOUND"
_STALE_SELECTION_STATE = "STALE_SELECTION_STATE"
_ELIGIBILITY_REQUIRED = "STORY_STRUCTURAL_ELIGIBILITY_REQUIRED"
_INTENT_STATE: dict[StorySprintSelectionIntent, SprintSelectionState] = {
    "select": "selected",
    "remove": "unselected",
    "defer": "deferred",
}


class StorySprintSelectionIntegrityError(RuntimeError):
    """Raised when stored selection history is not canonical and exact."""


class StorySprintSelectionMutationError(RuntimeError):
    """Structured expected rejection for one selection mutation."""

    def __init__(self, code: str, message: str) -> None:
        """Retain a stable error code for application adapters."""
        super().__init__(message)
        self.code = code


class StorySprintSelectionRequest(FrozenModel):
    """One exact human intent guarded by the observed selection state."""

    project_id: Annotated[int, Field(gt=0)]
    story_id: Annotated[int, Field(gt=0)]
    intent: StorySprintSelectionIntent
    expected_state_fingerprint: Sha256Fingerprint
    rationale: str | None = None
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None

    @field_validator("rationale", "correlation_id")
    @classmethod
    def reject_blank_optional_text(cls, value: str | None) -> str | None:
        """Keep absent text distinct from invalid whitespace-only input."""
        if value is not None and not value.strip():
            message = "Optional selection metadata must be nonblank."
            raise ValueError(message)
        return value


class StorySprintSelectionEventMetadata(FrozenModel):
    """Strict canonical audit payload persisted inside ``WorkflowEvent``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["agileforge.story-sprint-selection.v1"]
    project_id: int
    story_id: int
    source_story_artifact_id: int
    source_story_artifact_fingerprint: Sha256Fingerprint
    source_story_item_id: str
    source_story_item_fingerprint: Sha256Fingerprint
    accepted_spec_version_id: int
    accepted_spec_hash: Sha256Fingerprint
    actor: str
    action: StorySprintSelectionIntent
    new_state: SprintSelectionState
    previous_state: SprintSelectionState
    previous_state_fingerprint: Sha256Fingerprint
    observed_eligibility_evidence_fingerprint: Sha256Fingerprint | None
    rationale: str | None
    event_timestamp: datetime


class StorySprintSelectionFact(FrozenModel):
    """Latest human selection fact bound to one exact Story projection."""

    selection_state: SprintSelectionState
    state_fingerprint: str
    event_id: int | None = None
    event_fingerprint: str | None = None


def _story_id(story: UserStory) -> int:
    if story.story_id is None:
        message = "Sprint-selection Story identity is missing."
        raise StorySprintSelectionIntegrityError(message)
    return story.story_id


def _state_fingerprint(
    story: UserStory,
    *,
    selection_state: SprintSelectionState,
    event_id: int | None,
    event_fingerprint: str | None,
) -> str:
    return canonical_hash(
        {
            "schema_version": "agileforge.story-sprint-selection-state.v1",
            "project_id": story.project_id,
            "story_id": _story_id(story),
            "source_story_artifact_id": story.source_story_artifact_id,
            "source_story_artifact_fingerprint": (
                story.source_story_artifact_fingerprint
            ),
            "source_story_item_id": story.source_story_item_id,
            "source_story_item_fingerprint": story.source_story_item_fingerprint,
            "accepted_spec_version_id": story.accepted_spec_version_id,
            "accepted_spec_hash": story.accepted_spec_hash,
            "selection_state": selection_state,
            "latest_event_id": event_id,
            "latest_event_fingerprint": event_fingerprint,
        }
    )


def _default_fact(story: UserStory) -> StorySprintSelectionFact:
    return StorySprintSelectionFact(
        selection_state="unselected",
        state_fingerprint=_state_fingerprint(
            story,
            selection_state="unselected",
            event_id=None,
            event_fingerprint=None,
        ),
    )


def _utc(value: datetime) -> datetime:
    source = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return source.astimezone(UTC)


def _parse_event(
    session: Session,
    event: WorkflowEvent,
) -> tuple[UserStory, StorySprintSelectionEventMetadata, str]:
    if (
        event.event_id is None
        or event.project_id is None
        or event.event_metadata is None
    ):
        message = "Story selection event has incomplete audit identity."
        raise StorySprintSelectionIntegrityError(message)
    try:
        metadata = StorySprintSelectionEventMetadata.model_validate_json(
            event.event_metadata,
            strict=True,
        )
    except ValidationError as error:
        message = "Story selection event metadata is malformed."
        raise StorySprintSelectionIntegrityError(message) from error
    if canonical_json(metadata.model_dump(mode="json")) != event.event_metadata:
        message = "Story selection event metadata is not canonical."
        raise StorySprintSelectionIntegrityError(message)
    story = session.get(UserStory, metadata.story_id)
    exact_identity = (
        event.event_type is WorkflowEventType.STORY_SELECTION_CHANGED
        and event.project_id == metadata.project_id
        and event.sprint_id is None
        and event.duration_seconds is None
        and event.turn_count is None
        and _utc(event.timestamp) == _utc(metadata.event_timestamp)
        and story is not None
        and story.project_id == metadata.project_id
        and story.source_story_artifact_id == metadata.source_story_artifact_id
        and story.source_story_artifact_fingerprint
        == metadata.source_story_artifact_fingerprint
        and story.source_story_item_id == metadata.source_story_item_id
        and story.source_story_item_fingerprint
        == metadata.source_story_item_fingerprint
        and story.accepted_spec_version_id == metadata.accepted_spec_version_id
        and story.accepted_spec_hash == metadata.accepted_spec_hash
    )
    if not exact_identity:
        message = "Story selection event exact Story lineage is invalid."
        raise StorySprintSelectionIntegrityError(message)
    return story, metadata, canonical_hash(metadata.model_dump(mode="json"))


def _selection_facts(
    session: Session,
    *,
    project_id: int,
) -> dict[int, StorySprintSelectionFact]:
    stories = session.exec(
        select(UserStory)
        .where(col(UserStory.project_id) == project_id)
        .order_by(col(UserStory.story_id))
    ).all()
    facts = {_story_id(story): _default_fact(story) for story in stories}
    rows = session.exec(
        select(WorkflowEvent)
        .where(
            col(WorkflowEvent.project_id) == project_id,
            col(WorkflowEvent.event_type) == WorkflowEventType.STORY_SELECTION_CHANGED,
        )
        .order_by(col(WorkflowEvent.event_id))
    ).all()
    for event in rows:
        story, metadata, event_fingerprint = _parse_event(session, event)
        story_id = _story_id(story)
        previous = facts[story_id]
        expected_new_state = _INTENT_STATE[metadata.action]
        if (
            metadata.previous_state != previous.selection_state
            or metadata.previous_state_fingerprint != previous.state_fingerprint
            or metadata.new_state != expected_new_state
            or metadata.new_state == metadata.previous_state
        ):
            message = "Story selection event transition chain is invalid."
            raise StorySprintSelectionIntegrityError(message)
        event_id = cast("int", event.event_id)
        facts[story_id] = StorySprintSelectionFact(
            selection_state=metadata.new_state,
            state_fingerprint=_state_fingerprint(
                story,
                selection_state=metadata.new_state,
                event_id=event_id,
                event_fingerprint=event_fingerprint,
            ),
            event_id=event_id,
            event_fingerprint=event_fingerprint,
        )
    return facts


def story_structural_eligibility(
    session: Session,
    *,
    story: UserStory,
) -> tuple[bool, StructuralEligibilityStatus]:
    """Derive current eligibility independently from durable human intent."""
    try:
        require_story_ready_for_sprint(session, story=story)
    except StoryValidationReadinessError:
        try:
            evidence = require_current_story_validation_evidence(session, story=story)
        except StoryValidationReadinessError:
            return False, "stale"
        return False, "ineligible" if not evidence.structurally_eligible else "stale"
    return True, "eligible"


def story_sprint_selection_fact_in_session(
    session: Session,
    *,
    story: UserStory,
) -> StorySprintSelectionFact:
    """Strictly derive the latest state from canonical append-only history."""
    return _selection_facts(session, project_id=story.project_id)[_story_id(story)]


def _current_evidence_fingerprint(
    session: Session,
    *,
    story: UserStory,
) -> str | None:
    try:
        evidence = require_current_story_validation_evidence(session, story=story)
    except StoryValidationReadinessError:
        return None
    return canonical_hash(evidence.model_dump(mode="json"))


def _require_mutable_lifecycle(session: Session, *, story: UserStory) -> None:
    story_id = _story_id(story)
    decisions = session.exec(
        select(SprintPlanArtifactDecision).where(
            col(SprintPlanArtifactDecision.project_id) == story.project_id,
            col(SprintPlanArtifactDecision.decision) == "accepted",
        )
    ).all()
    for decision in decisions:
        plan = session.get(SprintPlanArtifact, decision.sprint_plan_artifact_id)
        if plan is None or plan.project_id != story.project_id:
            message = "Accepted Sprint-plan lifecycle lineage is invalid."
            raise StorySprintSelectionIntegrityError(message)
        try:
            selected_ids = _INT_LIST.validate_json(plan.selected_story_ids_json)
        except ValidationError as error:
            message = "Accepted Sprint-plan Story IDs are invalid."
            raise StorySprintSelectionIntegrityError(message) from error
        if canonical_json(
            selected_ids
        ) != plan.selected_story_ids_json or selected_ids != sorted(set(selected_ids)):
            message = "Accepted Sprint-plan Story IDs are not canonical."
            raise StorySprintSelectionIntegrityError(message)
        if story_id in selected_ids:
            message = "Story selection is locked by an accepted Sprint plan."
            raise StorySprintSelectionMutationError(_LIFECYCLE_LOCKED, message)
    memberships = session.exec(
        select(SprintStory).where(col(SprintStory.story_id) == story_id)
    ).all()
    for membership in memberships:
        sprint = session.get(Sprint, membership.sprint_id)
        started = session.exec(
            select(SprintStart).where(
                col(SprintStart.sprint_id) == membership.sprint_id
            )
        ).first()
        if sprint is not None and (
            sprint.status is SprintStatus.ACTIVE or started is not None
        ):
            message = "Story selection is locked by a started Sprint."
            raise StorySprintSelectionMutationError(_LIFECYCLE_LOCKED, message)


def apply_story_sprint_selection_in_session(
    session: Session,
    request: StorySprintSelectionRequest,
) -> StorySprintSelectionFact:
    """Validate and append one human selection transition in caller transaction."""
    story = session.get(UserStory, request.story_id)
    if story is None or story.project_id != request.project_id or story.is_superseded:
        message = (
            f"Story {request.story_id} was not found as an active Story in project"
            f" {request.project_id}."
        )
        raise StorySprintSelectionMutationError(_STORY_NOT_FOUND, message)
    current = story_sprint_selection_fact_in_session(session, story=story)
    if request.expected_state_fingerprint != current.state_fingerprint:
        message = "Story Sprint-selection state changed since it was observed."
        raise StorySprintSelectionMutationError(_STALE_SELECTION_STATE, message)
    _require_mutable_lifecycle(session, story=story)
    observed_evidence_fingerprint = _current_evidence_fingerprint(
        session,
        story=story,
    )
    if request.intent == "select":
        try:
            evidence = require_story_ready_for_sprint(session, story=story)
        except StoryValidationReadinessError as error:
            message = "Selecting a Story requires current eligible v3 evidence."
            raise StorySprintSelectionMutationError(
                _ELIGIBILITY_REQUIRED,
                message,
            ) from error
        observed_evidence_fingerprint = canonical_hash(evidence.model_dump(mode="json"))
    new_state = _INTENT_STATE[request.intent]
    if new_state == current.selection_state:
        return current
    event_timestamp = datetime.now(tz=UTC)
    metadata = StorySprintSelectionEventMetadata(
        schema_version="agileforge.story-sprint-selection.v1",
        project_id=story.project_id,
        story_id=_story_id(story),
        source_story_artifact_id=story.source_story_artifact_id,
        source_story_artifact_fingerprint=story.source_story_artifact_fingerprint,
        source_story_item_id=story.source_story_item_id,
        source_story_item_fingerprint=story.source_story_item_fingerprint,
        accepted_spec_version_id=story.accepted_spec_version_id,
        accepted_spec_hash=story.accepted_spec_hash,
        actor=request.actor,
        action=request.intent,
        new_state=new_state,
        previous_state=current.selection_state,
        previous_state_fingerprint=current.state_fingerprint,
        observed_eligibility_evidence_fingerprint=observed_evidence_fingerprint,
        rationale=request.rationale,
        event_timestamp=event_timestamp,
    )
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.STORY_SELECTION_CHANGED,
            timestamp=event_timestamp,
            project_id=story.project_id,
            event_metadata=canonical_json(metadata.model_dump(mode="json")),
        )
    )
    session.flush()
    return story_sprint_selection_fact_in_session(session, story=story)


__all__ = [
    "SprintSelectionState",
    "StorySprintSelectionEventMetadata",
    "StorySprintSelectionFact",
    "StorySprintSelectionIntegrityError",
    "StorySprintSelectionIntent",
    "StorySprintSelectionMutationError",
    "StorySprintSelectionRequest",
    "apply_story_sprint_selection_in_session",
    "story_sprint_selection_fact_in_session",
    "story_structural_eligibility",
]
