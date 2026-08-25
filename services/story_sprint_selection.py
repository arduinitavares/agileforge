# services/story_sprint_selection.py
"""Append-only human Sprint-selection state for exact accepted Stories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from services.specs.story_validation_service import (
    StoryValidationReadinessError,
    require_current_story_validation_evidence,
    require_story_ready_for_sprint,
)
from workflow.contracts import FrozenModel
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from sqlmodel import Session

    from models.core import UserStory

type SprintSelectionState = Literal["unselected", "selected", "deferred"]
type StructuralEligibilityStatus = Literal["eligible", "ineligible", "stale"]


class StorySprintSelectionFact(FrozenModel):
    """Latest human selection fact bound to one exact Story projection."""

    selection_state: SprintSelectionState
    state_fingerprint: str
    event_id: int | None = None
    event_fingerprint: str | None = None


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
    """Return the deterministic default before any explicit human event."""
    del session
    payload = {
        "schema_version": "agileforge.story-sprint-selection-state.v1",
        "project_id": story.project_id,
        "story_id": story.story_id,
        "source_story_artifact_id": story.source_story_artifact_id,
        "source_story_artifact_fingerprint": (
            story.source_story_artifact_fingerprint
        ),
        "source_story_item_id": story.source_story_item_id,
        "source_story_item_fingerprint": story.source_story_item_fingerprint,
        "accepted_spec_version_id": story.accepted_spec_version_id,
        "accepted_spec_hash": story.accepted_spec_hash,
        "selection_state": "unselected",
        "latest_event_id": None,
        "latest_event_fingerprint": None,
    }
    return StorySprintSelectionFact(
        selection_state="unselected",
        state_fingerprint=canonical_hash(payload),
    )


__all__ = [
    "SprintSelectionState",
    "StorySprintSelectionFact",
    "story_sprint_selection_fact_in_session",
    "story_structural_eligibility",
]
