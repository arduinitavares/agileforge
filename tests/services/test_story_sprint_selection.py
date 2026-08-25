# tests/services/test_story_sprint_selection.py
"""Real-database tests for append-only human Sprint-selection state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

import api
from models.core import Sprint, Team, UserStory
from models.enums import SprintStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import SprintPlanArtifact, SprintPlanArtifactDecision
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services import story_sprint_selection as selection_service
from services.application import (
    AgileForgeApplication,
    StoryEligibilityReconcileRequest,
)
from services.read_projections import DurableReadProjectionService
from tests.test_story_validation_service import _accepted_story
from tests.test_create_user_story import _decide_story, _record_story, _seed_story_parent
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_EXPECTED_SELECTION_EVENT_COUNT = 4
_NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _build_application(engine: Engine) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=_NOW),
        ),
        read_projection=DurableReadProjectionService(engine=engine),
    )


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


def test_application_replays_identical_request_and_rejects_conflicts(
    engine: Engine,
) -> None:
    """One key has one full request fingerprint and one durable result."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )
        project_id = story.project_id
    request = _selection_request(
        project_id=project_id,
        story_id=story_id,
        intent="select",
        expected_state_fingerprint=initial.state_fingerprint,
        key="application-replay",
    )
    app = _build_application(engine)

    first = app.apply_story_sprint_selection(request)
    replay = app.apply_story_sprint_selection(request)
    conflict = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="defer",
            expected_state_fingerprint=initial.state_fingerprint,
            key="application-replay",
        )
    )

    assert first == replay
    assert first["ok"] is True
    assert first["data"]["selection_state"] == "selected"
    assert conflict["ok"] is False
    assert conflict["errors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    with Session(engine) as session:
        assert len(
            session.exec(
                select(WorkflowEvent).where(
                    col(WorkflowEvent.event_type)
                    == WorkflowEventType.STORY_SELECTION_CHANGED
                )
            ).all()
        ) == 1


def test_api_and_cli_expose_intent_based_selection_mutations(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """HTTP uses one intent route while CLI exposes explicit state-change verbs."""
    from cli.main import main  # noqa: PLC0415

    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )
        project_id = story.project_id
    app = _build_application(engine)
    client = TestClient(api.app)
    with patch("api._application", return_value=app):
        selected = client.post(
            f"/api/projects/{project_id}/story/sprint-selection",
            json={
                "story_id": story_id,
                "intent": "select",
                "expected_state_fingerprint": initial.state_fingerprint,
                "rationale": "Select exact Story.",
                "idempotency_key": "api-select",
                "actor": "api-operator",
                "correlation_id": "api-correlation",
            },
        )
        invalid = client.post(
            f"/api/projects/{project_id}/story/sprint-selection",
            json={
                "story_id": story_id,
                "intent": "maybe",
                "expected_state_fingerprint": initial.state_fingerprint,
                "idempotency_key": "api-invalid",
                "actor": "api-operator",
            },
        )
    assert selected.status_code == HTTPStatus.OK
    selected_data = selected.json()["data"]
    assert selected_data["selection_state"] == "selected"
    assert invalid.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    exit_code = main(
        [
            "story",
            "sprint-selection",
            "defer",
            "--project-id",
            str(project_id),
            "--story-id",
            str(story_id),
            "--expected-state-fingerprint",
            selected_data["state_fingerprint"],
            "--rationale",
            "Defer exact Story.",
            "--idempotency-key",
            "cli-defer",
            "--actor",
            "cli-operator",
        ],
        application=app,
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["data"]["selection_state"] == "deferred"


def test_selected_intent_survives_staleness_and_reactivates_after_reconciliation(
    engine: Engine,
) -> None:
    """Evidence repair changes eligibility, never the preserved human intent."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )
        project_id = story.project_id
    app = _build_application(engine)
    selected = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=initial.state_fingerprint,
            key="preserve-select",
        )
    )
    selected_fingerprint = selected["data"]["state_fingerprint"]
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        story.validation_evidence = None
        session.add(story)
        session.commit()
    with Session(engine) as session:
        stale = WorkflowFactRepository(session).load(project_id).stories[0]
    assert stale.sprint_selection_state == "selected"
    assert stale.sprint_selection_state_fingerprint == selected_fingerprint
    assert stale.structural_eligibility_status == "stale"
    assert stale.structurally_eligible is False
    assert stale.sprint_candidate is False

    reconciled = app.reconcile_story_eligibility(
        StoryEligibilityReconcileRequest(
            project_id=project_id,
            story_ids=(story_id,),
            idempotency_key="restore-evidence",
            actor="operator@example.com",
            correlation_id="reconcile-selection",
        )
    )
    assert reconciled["ok"] is True
    with Session(engine) as session:
        restored = WorkflowFactRepository(session).load(project_id).stories[0]
    assert restored.sprint_selection_state == "selected"
    assert restored.sprint_selection_state_fingerprint == selected_fingerprint
    assert restored.structurally_eligible is True
    assert restored.sprint_candidate is True


def test_malformed_selection_history_uses_repository_integrity_error(
    engine: Engine,
) -> None:
    """Unexpected metadata keys fail closed through the repository error path."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )
        project_id = story.project_id
    _build_application(engine).apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=initial.state_fingerprint,
            key="corrupt-history",
        )
    )
    with Session(engine) as session:
        event = session.exec(
            select(WorkflowEvent).where(
                col(WorkflowEvent.event_type)
                == WorkflowEventType.STORY_SELECTION_CHANGED
            )
        ).one()
        assert event.event_metadata is not None
        metadata = json.loads(event.event_metadata)
        metadata["unexpected"] = "not canonical schema"
        event.event_metadata = canonical_json(metadata)
        session.add(event)
        session.commit()

    with Session(engine) as session, pytest.raises(
        WorkflowFactLoadError,
        match="Story selection event metadata is malformed",
    ):
        WorkflowFactRepository(session).load(project_id)


def test_select_requires_current_evidence_but_defer_and_remove_do_not(
    engine: Engine,
) -> None:
    """Only creating selected intent is gated by current structural evidence."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )
        project_id = story.project_id
        story.validation_evidence = None
        session.add(story)
        session.commit()
    app = _build_application(engine)
    rejected = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=initial.state_fingerprint,
            key="select-stale",
        )
    )
    assert rejected["ok"] is False
    assert rejected["errors"][0]["code"] == (
        "STORY_STRUCTURAL_ELIGIBILITY_REQUIRED"
    )
    deferred = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="defer",
            expected_state_fingerprint=initial.state_fingerprint,
            key="defer-stale",
        )
    )
    assert deferred["ok"] is True
    removed = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="remove",
            expected_state_fingerprint=deferred["data"]["state_fingerprint"],
            key="remove-stale",
        )
    )
    assert removed["ok"] is True
    assert removed["data"]["selection_state"] == "unselected"


def test_superseding_story_receives_no_selection_state_from_replaced_story(
    engine: Engine,
) -> None:
    """A replacement Story ID starts unselected while old audit remains exact."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        first_artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Original selected work",
        )
        first_result = _decide_story(
            session,
            first_artifact,
            decision="accepted",
            offset=2,
        )
        session.commit()
        first_story_id = first_result.activated_story_ids[0]
        first_artifact_id = int(first_artifact.story_artifact_id or 0)
    with Session(engine) as session:
        first_story = session.get_one(UserStory, first_story_id)
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=first_story,
        )
    selected = _build_application(engine).apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=first_story_id,
            intent="select",
            expected_state_fingerprint=initial.state_fingerprint,
            key="select-original",
        )
    )
    assert selected["ok"] is True
    with Session(engine) as session:
        replacement_artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Replacement work",
            supersedes_id=first_artifact_id,
            recorded_offset=3,
        )
        replacement = _decide_story(
            session,
            replacement_artifact,
            decision="accepted",
            offset=4,
        )
        session.commit()
        replacement_story_id = replacement.activated_story_ids[0]

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    old = next(item for item in snapshot.stories if item.story_id == first_story_id)
    new = next(
        item for item in snapshot.stories if item.story_id == replacement_story_id
    )
    assert old.sprint_selection_state == "selected"
    assert old.sprint_candidate is False
    assert new.sprint_selection_state == "unselected"
    assert new.sprint_selection_event_id is None
    assert new.sprint_selection_state_fingerprint != (
        old.sprint_selection_state_fingerprint
    )


def test_accepted_sprint_plan_locks_further_selection_changes(engine: Engine) -> None:
    """An exact Story cannot change intent after accepted Sprint-plan binding."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        project_id = story.project_id
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )
    app = _build_application(engine)
    selected = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=initial.state_fingerprint,
            key="select-before-plan",
        )
    )
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        team = Team(name=f"Selection lock team {project_id}")
        session.add(team)
        session.flush()
        assert team.team_id is not None
        sprint = Sprint(
            project_id=project_id,
            team_id=team.team_id,
            goal="Lock selected Story.",
            status=SprintStatus.PLANNED,
        )
        session.add(sprint)
        session.flush()
        assert sprint.sprint_id is not None
        plan_fingerprint = canonical_hash({"plan": story_id})
        plan = SprintPlanArtifact(
            project_id=project_id,
            spec_version_id=story.accepted_spec_version_id,
            spec_hash=story.accepted_spec_hash,
            sprint_plan_stream_id="SPS-1234567890abcdef1234567890abcdef",
            version_number=1,
            selected_story_ids_json=canonical_json([story_id]),
            canonical_task_plan_json=canonical_json({"tasks": []}),
            plan_fingerprint=plan_fingerprint,
            candidate_set_fingerprint=canonical_hash({"stories": [story_id]}),
            created_by="operator@example.com",
            created_at=_NOW,
        )
        session.add(plan)
        session.flush()
        assert plan.sprint_plan_artifact_id is not None
        session.add(
            SprintPlanArtifactDecision(
                project_id=project_id,
                sprint_plan_artifact_id=plan.sprint_plan_artifact_id,
                plan_fingerprint=plan_fingerprint,
                decision="accepted",
                activated_sprint_id=sprint.sprint_id,
                rationale="Accepted exact selected scope.",
                reviewer="operator@example.com",
                idempotency_key="accept-selection-lock-plan",
                decided_at=_NOW,
            )
        )
        session.commit()

    locked = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="remove",
            expected_state_fingerprint=selected["data"]["state_fingerprint"],
            key="remove-after-plan",
        )
    )
    assert locked["ok"] is False
    assert locked["errors"][0]["code"] == "SELECTION_LIFECYCLE_LOCKED"
