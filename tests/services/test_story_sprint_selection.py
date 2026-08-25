# tests/services/test_story_sprint_selection.py
"""Real-database tests for append-only human Sprint-selection state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import UserStory
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from repositories.workflow import WorkflowFactRepository
from services import story_sprint_selection as selection_service
from tests.test_story_validation_service import _accepted_story

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_EXPECTED_SELECTION_EVENT_COUNT = 4


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


def _selection_request(
    *,
    project_id: int,
    story_id: int,
    intent: str,
    expected_state_fingerprint: str,
    key: str,
) -> selection_service.StorySprintSelectionRequest:
    return selection_service.StorySprintSelectionRequest(
        project_id=project_id,
        story_id=story_id,
        intent=intent,
        expected_state_fingerprint=expected_state_fingerprint,
        rationale=f"Operator chose {intent}.",
        idempotency_key=key,
        actor="operator@example.com",
        correlation_id="corr-selection",
    )


def test_complete_selection_transition_table_is_append_only(engine: Engine) -> None:
    """Every real state change appends once; same-state intent appends nothing."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        current = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )

        states: list[str] = []
        for index, intent in enumerate(
            ("select", "select", "remove", "defer", "select"),
            start=1,
        ):
            current = selection_service.apply_story_sprint_selection_in_session(
                session,
                _selection_request(
                    project_id=story.project_id,
                    story_id=story_id,
                    intent=intent,
                    expected_state_fingerprint=current.state_fingerprint,
                    key=f"transition-{index}",
                ),
            )
            states.append(current.selection_state)
        session.commit()

    assert states == ["selected", "selected", "unselected", "deferred", "selected"]
    with Session(engine) as session:
        events = session.exec(
            select(WorkflowEvent)
            .where(
                col(WorkflowEvent.event_type)
                == WorkflowEventType.STORY_SELECTION_CHANGED
            )
            .order_by(col(WorkflowEvent.event_id))
        ).all()
        story = session.get_one(UserStory, story_id)
        reloaded = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )

    assert len(events) == _EXPECTED_SELECTION_EVENT_COUNT
    assert reloaded.selection_state == "selected"
    assert reloaded.event_id == events[-1].event_id
    assert reloaded.event_fingerprint is not None
