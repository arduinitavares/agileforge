# tests/services/test_story_sprint_selection.py
"""Real-database tests for append-only human Sprint-selection state."""

from __future__ import annotations

import concurrent.futures
import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, col, create_engine, select

import api
from models.core import Sprint, Team, UserStory
from models.enums import SprintStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import (
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services import story_sprint_selection as selection_service
from services.application import (
    AgileForgeApplication,
    StoryEligibilityReconcileRequest,
)
from services.read_projections import DurableReadProjectionService
from tests.test_create_user_story import (
    _decide_story,
    _record_story,
    _seed_story_parent,
)
from tests.test_story_validation_service import _accepted_story
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject

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


def _result_data(result: JsonObject) -> JsonObject:
    data = result["data"]
    assert isinstance(data, dict)
    return data


def _result_string(result: JsonObject, key: str) -> str:
    value = _result_data(result)[key]
    assert isinstance(value, str)
    return value


def _error_code(result: JsonObject) -> str:
    errors = result["errors"]
    assert isinstance(errors, list)
    error = errors[0]
    assert isinstance(error, dict)
    code = error["code"]
    assert isinstance(code, str)
    return code


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
    intent: selection_service.StorySprintSelectionIntent,
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
        intents: tuple[selection_service.StorySprintSelectionIntent, ...] = (
            "select",
            "select",
            "remove",
            "defer",
            "select",
        )
        for index, intent in enumerate(intents, start=1):
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
    assert _result_data(first)["selection_state"] == "selected"
    assert conflict["ok"] is False
    assert _error_code(conflict) == "IDEMPOTENCY_CONFLICT"
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


@pytest.mark.parametrize("field", ["actor", "correlation_id", "rationale"])
def test_selection_api_rejects_blank_audit_metadata_before_application(
    field: str,
) -> None:
    """Selection-only blank audit text must stop at HTTP validation."""
    application = MagicMock(spec=AgileForgeApplication)
    payload: dict[str, int | str] = {
        "story_id": 17,
        "intent": "select",
        "expected_state_fingerprint": f"sha256:{'0' * 64}",
        "rationale": "Select exact Story.",
        "idempotency_key": "api-invalid-audit",
        "actor": "api-operator",
        "correlation_id": "api-correlation",
    }
    payload[field] = "   "

    with patch("api._application", return_value=application):
        response = TestClient(api.app, raise_server_exceptions=False).post(
            "/api/projects/1/story/sprint-selection",
            json=payload,
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    application.apply_story_sprint_selection.assert_not_called()


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
    selected_fingerprint = _result_string(selected, "state_fingerprint")
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
    assert restored.dependency_safe is False
    assert restored.sprint_candidate is False


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
    assert _error_code(rejected) == (
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
            expected_state_fingerprint=_result_string(
                deferred,
                "state_fingerprint",
            ),
            key="remove-stale",
        )
    )
    assert removed["ok"] is True
    assert _result_data(removed)["selection_state"] == "unselected"
    with Session(engine) as session:
        metadata = [
            selection_service.StorySprintSelectionEventMetadata.model_validate_json(
                event.event_metadata,
                strict=True,
            )
            for event in session.exec(
                select(WorkflowEvent)
                .where(
                    col(WorkflowEvent.event_type)
                    == WorkflowEventType.STORY_SELECTION_CHANGED
                )
                .order_by(col(WorkflowEvent.event_id))
            ).all()
            if event.event_metadata is not None
        ]
        WorkflowFactRepository(session).load(project_id)
    assert [item.action for item in metadata] == ["defer", "remove"]
    assert all(
        item.observed_eligibility_evidence_fingerprint is None
        for item in metadata
    )


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
            expected_state_fingerprint=_result_string(
                selected,
                "state_fingerprint",
            ),
            key="remove-after-plan",
        )
    )
    assert locked["ok"] is False
    assert _error_code(locked) == "SELECTION_LIFECYCLE_LOCKED"


def test_selection_event_audit_binds_exact_story_and_operator_metadata(
    engine: Engine,
) -> None:
    """The event is a complete canonical audit fact, not a mutable state cache."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        initial = selection_service.story_sprint_selection_fact_in_session(
            session,
            story=story,
        )
        project_id = story.project_id
        expected_identity = (
            story.source_story_artifact_id,
            story.source_story_artifact_fingerprint,
            story.source_story_item_id,
            story.source_story_item_fingerprint,
            story.accepted_spec_version_id,
            story.accepted_spec_hash,
        )
    result = _build_application(engine).apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=initial.state_fingerprint,
            key="audit-selection",
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
    metadata = selection_service.StorySprintSelectionEventMetadata.model_validate_json(
        event.event_metadata,
        strict=True,
    )
    assert metadata.schema_version == "agileforge.story-sprint-selection.v1"
    assert metadata.project_id == project_id
    assert metadata.story_id == story_id
    assert (
        metadata.source_story_artifact_id,
        metadata.source_story_artifact_fingerprint,
        metadata.source_story_item_id,
        metadata.source_story_item_fingerprint,
        metadata.accepted_spec_version_id,
        metadata.accepted_spec_hash,
    ) == expected_identity
    assert metadata.actor == "operator@example.com"
    assert metadata.action == "select"
    assert metadata.previous_state == "unselected"
    assert metadata.new_state == "selected"
    assert metadata.rationale == "Operator chose select."
    assert metadata.observed_eligibility_evidence_fingerprint is not None
    assert _result_data(result)["selection_event_id"] == event.event_id


def test_same_state_is_receipted_and_stale_expected_state_fails_closed(
    engine: Engine,
) -> None:
    """No-op intent replays durably while stale optimistic guards never append."""
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
            key="select-once",
        )
    )
    no_op = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=_result_string(
                selected,
                "state_fingerprint",
            ),
            key="select-no-op",
        )
    )
    stale = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="defer",
            expected_state_fingerprint=initial.state_fingerprint,
            key="stale-expected",
        )
    )
    assert no_op == selected
    assert stale["ok"] is False
    assert _error_code(stale) == "STALE_SELECTION_STATE"
    with Session(engine) as session:
        events = session.exec(
            select(WorkflowEvent).where(
                col(WorkflowEvent.event_type)
                == WorkflowEventType.STORY_SELECTION_CHANGED
            )
        ).all()
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind)
                == "apply_story_sprint_selection"
            )
        ).all()
    assert len(events) == 1
    assert len(receipts) == 3  # noqa: PLR2004


def test_concurrent_identical_selection_requests_share_one_event(
    tmp_path: Path,
) -> None:
    """SQLite writer serialization returns one receipt result and one audit event."""
    database = tmp_path / "selection-race.sqlite3"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
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
            key="concurrent-selection",
        )
        app = _build_application(engine)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(app.apply_story_sprint_selection, request)
            second = executor.submit(app.apply_story_sprint_selection, request)
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)
        assert first_result == second_result
        with Session(engine) as session:
            assert len(
                session.exec(
                    select(WorkflowEvent).where(
                        col(WorkflowEvent.event_type)
                        == WorkflowEventType.STORY_SELECTION_CHANGED
                    )
                ).all()
            ) == 1
            assert len(
                session.exec(
                    select(WorkflowTransitionReceipt).where(
                        col(WorkflowTransitionReceipt.request_kind)
                        == "apply_story_sprint_selection"
                    )
                ).all()
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("malformed_json", "metadata is malformed"),
        ("noncanonical_json", "metadata is not canonical"),
        ("timestamp_mismatch", "exact Story lineage is invalid"),
        ("wrong_identity", "exact Story lineage is invalid"),
        ("wrong_previous_fingerprint", "transition chain is invalid"),
        ("invalid_transition", "transition chain is invalid"),
        ("select_without_evidence", "metadata is malformed"),
        ("blank_actor", "metadata is malformed"),
        ("blank_rationale", "metadata is malformed"),
    ],
)
def test_every_corrupt_selection_history_shape_fails_closed(
    engine: Engine,
    corruption: str,
    message: str,
) -> None:
    """Canonical parsing rejects bytes, lineage, and transition-chain corruption."""
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
            key=f"corrupt-{corruption}",
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
        if corruption == "malformed_json":
            event.event_metadata = "{"
        elif corruption == "noncanonical_json":
            event.event_metadata = json.dumps(metadata, indent=2)
        elif corruption == "timestamp_mismatch":
            metadata["event_timestamp"] = "2030-01-01T00:00:00Z"
            event.event_metadata = canonical_json(metadata)
        elif corruption == "wrong_identity":
            metadata["source_story_item_fingerprint"] = f"sha256:{'0' * 64}"
            event.event_metadata = canonical_json(metadata)
        elif corruption == "wrong_previous_fingerprint":
            metadata["previous_state_fingerprint"] = f"sha256:{'0' * 64}"
            event.event_metadata = canonical_json(metadata)
        elif corruption == "invalid_transition":
            metadata["new_state"] = "unselected"
            event.event_metadata = canonical_json(metadata)
        elif corruption == "select_without_evidence":
            metadata["observed_eligibility_evidence_fingerprint"] = None
            event.event_metadata = canonical_json(metadata)
        elif corruption == "blank_actor":
            metadata["actor"] = "   "
            event.event_metadata = canonical_json(metadata)
        elif corruption == "blank_rationale":
            metadata["rationale"] = "   "
            event.event_metadata = canonical_json(metadata)
        else:
            raise AssertionError(corruption)
        session.add(event)
        session.commit()
    with Session(engine) as session, pytest.raises(
        WorkflowFactLoadError,
        match=message,
    ):
        WorkflowFactRepository(session).load(project_id)


def test_selection_request_rejects_blank_actor() -> None:
    """Whitespace-only actor text must never reach the append-only audit log."""
    with pytest.raises(ValueError, match="nonblank"):
        selection_service.StorySprintSelectionRequest(
            project_id=1,
            story_id=1,
            intent="select",
            expected_state_fingerprint=f"sha256:{'0' * 64}",
            idempotency_key="blank-actor",
            actor="   ",
        )


def test_repository_replays_selection_history_once_per_project(
    engine: Engine,
) -> None:
    """One snapshot must replay the canonical project history once, not per Story."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            item_count=2,
        )
        result = _decide_story(session, artifact, decision="accepted", offset=2)
        session.commit()
    assert len(result.activated_story_ids) == 2  # noqa: PLR2004

    with patch.object(
        selection_service,
        "_selection_facts",
        wraps=selection_service._selection_facts,
    ) as replay, Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    assert len(snapshot.stories) == 2  # noqa: PLR2004
    assert replay.call_count == 1


def test_cross_project_story_binding_in_selection_event_fails_closed(
    engine: Engine,
) -> None:
    """An event row cannot claim exact Story identity owned by another Project."""
    story_id = _accepted_story(engine)
    foreign_project_id, foreign_roadmap_id = _seed_story_parent(
        engine,
        requirements=("Foreign selection work",),
    )
    with Session(engine) as session:
        foreign_artifact = _record_story(
            session,
            project_id=foreign_project_id,
            roadmap_id=foreign_roadmap_id,
            title="Foreign Story",
        )
        foreign_result = _decide_story(
            session,
            foreign_artifact,
            decision="accepted",
            offset=2,
        )
        session.commit()
        foreign_story_id = foreign_result.activated_story_ids[0]
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        foreign = session.get_one(UserStory, foreign_story_id)
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
            key="cross-project-corruption",
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
        metadata.update(
            {
                "story_id": foreign_story_id,
                "source_story_artifact_id": foreign.source_story_artifact_id,
                "source_story_artifact_fingerprint": (
                    foreign.source_story_artifact_fingerprint
                ),
                "source_story_item_id": foreign.source_story_item_id,
                "source_story_item_fingerprint": (
                    foreign.source_story_item_fingerprint
                ),
                "accepted_spec_version_id": foreign.accepted_spec_version_id,
                "accepted_spec_hash": foreign.accepted_spec_hash,
            }
        )
        event.event_metadata = canonical_json(metadata)
        session.add(event)
        session.commit()
    with Session(engine) as session, pytest.raises(
        WorkflowFactLoadError,
        match="exact Story lineage is invalid",
    ):
        WorkflowFactRepository(session).load(project_id)


def test_selection_response_uses_the_canonical_post_event_story_fact(
    engine: Engine,
) -> None:
    """A selection write must not locally infer dependency safety or candidacy."""
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
            key="canonical-selection-response",
        )
    )
    selected_data = _result_data(selected)
    no_op = app.apply_story_sprint_selection(
        _selection_request(
            project_id=project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=_result_string(
                selected,
                "state_fingerprint",
            ),
            key="canonical-selection-no-op",
        )
    )

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    fact = next(item for item in snapshot.stories if item.story_id == story_id)

    for response in (selected_data, _result_data(no_op)):
        assert response["structurally_eligible"] == fact.structurally_eligible
        assert (
            response["structural_eligibility_status"]
            == fact.structural_eligibility_status
        )
        assert response["selected_scope_fingerprint"] == fact.selected_scope_fingerprint
        assert response["dependency_safe"] is False
        assert response["sprint_candidate"] is False
        assert response["dependency_safe"] == fact.dependency_safe
        assert response["sprint_candidate"] == fact.sprint_candidate


def test_direct_story_api_and_cli_show_exact_unselected_story_fact(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An eligible but unselected Story is never represented as a candidate."""
    from cli.main import main  # noqa: PLC0415

    story_id = _accepted_story(engine)
    app = _build_application(engine)
    direct = _result_data(app.reads.story_show(story_id=story_id))
    assert direct["structurally_eligible"] is True
    assert direct["structural_eligibility_status"] == "eligible"
    assert direct["structural_failures"] == []
    assert direct["sprint_selection_state"] == "unselected"
    assert direct["sprint_selection_event_id"] is None
    assert direct["sprint_selection_event_fingerprint"] is None
    assert direct["dependency_safe"] is False
    assert direct["sprint_candidate"] is False
    assert "ready_for_sprint" not in direct
    assert "validation_status" not in direct

    with patch("api._application", return_value=app):
        api_response = TestClient(api.app).get(f"/api/stories/{story_id}")
    assert api_response.status_code == HTTPStatus.OK
    assert api_response.json()["data"] == direct

    assert main(["story", "show", "--story-id", str(story_id)], application=app) == 0
    assert json.loads(capsys.readouterr().out)["data"] == direct


@pytest.mark.parametrize(
    "corruption",
    ["missing", "malformed", "mismatched", "coherent_tail_rewrite"],
)
def test_selection_event_requires_an_exact_completed_receipt_anchor(
    engine: Engine,
    corruption: str,
) -> None:
    """Selection history fails closed without its exact creating receipt anchor."""
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
            key=f"receipt-anchor-select-{corruption}",
        )
    )

    if corruption == "coherent_tail_rewrite":
        removed = app.apply_story_sprint_selection(
            _selection_request(
                project_id=project_id,
                story_id=story_id,
                intent="remove",
                expected_state_fingerprint=_result_string(
                    selected,
                    "state_fingerprint",
                ),
                key="receipt-anchor-remove",
            )
        )
        app.apply_story_sprint_selection(
            _selection_request(
                project_id=project_id,
                story_id=story_id,
                intent="defer",
                expected_state_fingerprint=_result_string(
                    removed,
                    "state_fingerprint",
                ),
                key="receipt-anchor-defer",
            )
        )

    with Session(engine) as session:
        events = session.exec(
            select(WorkflowEvent)
            .where(
                col(WorkflowEvent.event_type)
                == WorkflowEventType.STORY_SELECTION_CHANGED
            )
            .order_by(col(WorkflowEvent.event_id))
        ).all()
        event = events[-1]
        assert event.event_metadata is not None
        metadata = json.loads(event.event_metadata)
        receipt_id = metadata.get("workflow_transition_receipt_id")
        assert isinstance(receipt_id, int)
        receipt = session.get(WorkflowTransitionReceipt, receipt_id)
        assert receipt is not None

        if corruption == "missing":
            session.delete(receipt)
        elif corruption == "malformed":
            receipt.request_json = "{"
            session.add(receipt)
        elif corruption == "mismatched":
            request = json.loads(receipt.request_json)
            request["actor"] = "tampered@example.com"
            receipt.request_json = canonical_json(request)
            session.add(receipt)
        else:
            assert len(events) == 3  # noqa: PLR2004
            first_metadata = json.loads(events[0].event_metadata or "{}")
            metadata.update(
                {
                    "action": "select",
                    "new_state": "selected",
                    "observed_eligibility_evidence_fingerprint": first_metadata[
                        "observed_eligibility_evidence_fingerprint"
                    ],
                }
            )
            event.event_metadata = canonical_json(metadata)
            session.add(event)
        session.commit()

    with Session(engine), pytest.raises(
        WorkflowFactLoadError,
        match="selection receipt",
    ):
        WorkflowFactRepository(session).load(project_id)
