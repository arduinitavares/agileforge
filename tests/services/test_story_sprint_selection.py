# tests/services/test_story_sprint_selection.py
"""Real-database tests for append-only human Sprint-selection state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session

from repositories.workflow import WorkflowFactRepository
from tests.test_story_validation_service import _accepted_story

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_eligible_story_defaults_to_unselected_and_not_a_candidate(
    engine: Engine,
) -> None:
    """Eligibility alone must never infer the operator's Sprint intent."""
    story_id = _accepted_story(engine)

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id=1)

    story = next(item for item in snapshot.stories if item.story_id == story_id)
    assert story.structurally_eligible is True
    assert story.structural_eligibility_status == "eligible"
    assert story.sprint_selection_state == "unselected"
    assert story.sprint_selection_state_fingerprint.startswith("sha256:")
    assert story.sprint_selection_event_id is None
    assert story.sprint_selection_event_fingerprint is None
    assert story.sprint_candidate is False
