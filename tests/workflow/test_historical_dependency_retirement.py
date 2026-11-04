# tests/workflow/test_historical_dependency_retirement.py
"""Guarded retirement of historical edges preserves original authority."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect as inspect_orm
from sqlmodel import Session, SQLModel, col, create_engine, select

from models.core import Project, Sprint, SprintStory, UserStory, UserStoryDependency
from models.enums import SprintStatus, StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import (
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintPlanRetirement,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services import sprint_plan_retirement as plan_retirement
from services import story_dependency_retirement as retirement
from services.story_dependency_lifecycle import (
    load_story_dependency_invalidations_in_session,
)
from tests.workflow.execution_retry_support import (
    _close_execution_sprint,
    _triage_execution_sprint,
)
from tests.workflow.execution_retry_support import _domain as execution_domain
from tests.workflow.planning_fixtures import (
    planning_decision,
    planning_guards,
    record_and_accept_roadmap,
    record_and_accept_story,
    seed_accepted_backlog,
)
from tests.workflow.test_sprint_plan_retirement import (
    _retire_request as _retire_plan_request,
)
from tests.workflow.test_sprint_plan_retirement_history import _retired_replacement
from tests.workflow.test_superseded_story_dependencies import (
    _Scenario,
    _accepted_dependency_sprint,
    _complete_fixture_story_sprint,
    _completed_history,
    _domain,
    _edge,
    _events,
    _plan_or_start_preservation_retry,
    _project_business_rows,
    _replace,
    _request as _apply_request,
    _review,
    _seed,
    _snapshot,
)
from workflow import requests
from workflow.contracts import NodeCategory, WorkflowErrorCode
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.planning_integrity import historical_dependency_edges

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

def _historical_cycle(engine: Engine) -> tuple[_Scenario, int]:
    scenario = _seed(engine)
    with Session(engine) as session:
        for story_id in scenario.story_ids[:2]:
            story = session.get_one(UserStory, story_id)
            story.status = StoryStatus.DONE
            session.add(story)
        session.commit()
    dependency_id = _edge(engine, scenario, *scenario.story_ids[:2])
    _edge(engine, scenario, *reversed(scenario.story_ids[:2]))
    _review(engine, scenario, scenario.story_ids[2])
    return scenario, dependency_id


def _stored_edges(engine: Engine, project_id: int) -> tuple[dict, ...]:
    with Session(engine) as session:
        return tuple(
            row.model_dump()
            for row in session.exec(
                select(UserStoryDependency)
                .where(UserStoryDependency.project_id == project_id)
                .order_by(col(UserStoryDependency.dependency_id))
            )
        )


def _retire_request(
    engine: Engine, scenario: _Scenario, dependency_id: int
) -> requests.RetireHistoricalStoryDependency:
    edge = next(
        row
        for row in _snapshot(engine, scenario).story_dependencies
        if row.dependency_id == dependency_id
    )
    return requests.RetireHistoricalStoryDependency(
        **planning_guards(
            _domain(engine).position(scenario.project_id),
            "planning.story_dependencies.retire_historical",
            instance_key=f"story_dependency:{dependency_id}",
        ),
        dependency_id=dependency_id,
        dependency_fingerprint=canonical_hash(edge.model_dump(mode="json")),
        rationale="Remove obsolete historical cycle authority for current planning.",
        idempotency_key="retire-historical-cycle-edge",
    )


def test_historical_cycle_retirement_preserves_edges_and_review(engine: Engine) -> None:
    """Repair only current authority and retain one immutable audited outcome."""
    scenario, dependency_id = _historical_cycle(engine)
    domain = _domain(engine)
    position = domain.position(scenario.project_id)
    planning = planning_decision(position, "planning.sprint.plan")
    assert (planning.category, planning.reason_code) == (
        NodeCategory.INVALID,
        "STORY_DEPENDENCY_CYCLE",
    )
    before = _snapshot(engine, scenario)
    stored = _stored_edges(engine, scenario.project_id)
    edge = next(
        row for row in before.story_dependencies if row.dependency_id == dependency_id
    )
    request = requests.RetireHistoricalStoryDependency(
        **planning_guards(
            position,
            "planning.story_dependencies.retire_historical",
            instance_key=f"story_dependency:{dependency_id}",
        ),
        dependency_id=dependency_id,
        dependency_fingerprint=canonical_hash(edge.model_dump(mode="json")),
        rationale="Remove obsolete historical cycle authority for current planning.",
        idempotency_key="retire-historical-cycle-edge",
    )
    result = domain.transition(request)
    assert result.ok, result
    assert result.output["dependency_id"] == dependency_id
    retirement_event_id = result.output["retirement_event_id"]
    assert isinstance(retirement_event_id, int)
    assert retirement_event_id > 0
    reloaded = _snapshot(engine, scenario)
    assert _stored_edges(engine, scenario.project_id) == stored
    assert reloaded.story_dependency_reviews == before.story_dependency_reviews
    assert (
        next(
            row
            for row in reloaded.story_dependencies
            if row.dependency_id == dependency_id
        ).status
        == "retired"
    )
    assert len(reloaded.story_dependency_retirements) == 1
    planning = planning_decision(
        _domain(engine).position(scenario.project_id), "planning.sprint.plan"
    )
    assert planning.category is NodeCategory.AVAILABLE
    replay = _domain(engine).transition(request)
    assert replay.ok
    assert replay.replayed
    assert replay.output == result.output
    assert len(_snapshot(engine, scenario).story_dependency_retirements) == 1


@pytest.mark.parametrize("field", ["actor", "rationale", "idempotency_key"])
@pytest.mark.parametrize("value", ["", "  ", None])
def test_retirement_requires_accountable_intent(
    engine: Engine, field: str, value: str | None
) -> None:
    """Missing accountable intent is rejected before a transition starts."""
    scenario, dependency_id = _historical_cycle(engine)
    raw = _retire_request(engine, scenario, dependency_id).model_dump()
    before = _project_business_rows(engine, scenario.project_id)
    raw[field] = value
    with pytest.raises(ValidationError):
        requests.RetireHistoricalStoryDependency.model_validate(raw)
    assert _project_business_rows(engine, scenario.project_id) == before


@pytest.mark.parametrize("change", ["fingerprint", "foreign_edge", "stale_position"])
def test_retirement_identity_refusals_are_atomic(engine: Engine, change: str) -> None:
    """Foreign identities and stale freshness guards cannot append retirement."""
    scenario, dependency_id = _historical_cycle(engine)
    requirements = ("Foreign prerequisite", "Foreign dependent")
    foreign_project_id = seed_accepted_backlog(engine, requirements=requirements)
    foreign_domain = _domain(engine)
    record_and_accept_roadmap(
        foreign_domain,
        foreign_project_id,
        requirements=requirements,
        idempotency_suffix="-foreign",
    )
    foreign_stories = tuple(
        record_and_accept_story(
            engine,
            foreign_domain,
            foreign_project_id,
            requirement=requirement,
            spec_item_id=f"REQ.planning-{index + 1}",
            backlog_item_id=f"PBI-{index + 1:06d}",
            idempotency_suffix=f"-foreign-{index}",
        )[1]
        for index, requirement in enumerate(requirements)
    )
    with Session(engine) as session:
        foreign_edge = UserStoryDependency(
            project_id=foreign_project_id,
            dependent_story_id=foreign_stories[0],
            prerequisite_story_id=foreign_stories[1],
            status="active",
            source="manual_review",
            confidence="reviewed",
            reason="Foreign Project dependency.",
        )
        session.add(foreign_edge)
        session.commit()
        foreign_id = foreign_edge.dependency_id
        assert foreign_id is not None
    request = _retire_request(engine, scenario, dependency_id)
    updates = {
        "fingerprint": {"dependency_fingerprint": "sha256:" + "0" * 64},
        "foreign_edge": {"dependency_id": foreign_id},
        "stale_position": {"fact_fingerprint": "sha256:" + "0" * 64},
    }
    before = _project_business_rows(engine, scenario.project_id)
    foreign_before = _project_business_rows(engine, foreign_project_id)
    result = _domain(engine).transition(request.model_copy(update=updates[change]))
    assert not result.ok
    assert result.error is not None
    assert result.error.code is (
        WorkflowErrorCode.STALE_POSITION
        if change == "stale_position"
        else WorkflowErrorCode.STORY_DEPENDENCY_IDENTITY_CHANGED
    )
    assert _project_business_rows(engine, scenario.project_id) == before
    assert _project_business_rows(engine, foreign_project_id) == foreign_before


def test_current_dependency_has_no_historical_bypass(engine: Engine) -> None:
    """An accepted current endpoint cannot use historical-edge retirement."""
    scenario, dependency_id = _historical_cycle(engine)
    current_id = _edge(engine, scenario, scenario.story_ids[2], scenario.story_ids[0])
    request = _retire_request(engine, scenario, dependency_id)
    current = next(
        item
        for item in _snapshot(engine, scenario).story_dependencies
        if item.dependency_id == current_id
    )
    request = request.model_copy(
        update={
            "dependency_id": current_id,
            "dependency_fingerprint": canonical_hash(current.model_dump(mode="json")),
            "instance_key": f"story_dependency:{current_id}",
        }
    )
    before = _project_business_rows(engine, scenario.project_id)
    result = _domain(engine).transition(request)
    assert not result.ok
    with Session(engine) as session:
        with pytest.raises(retirement.StoryDependencyRetirementError) as refused:
            retirement.retire_historical_story_dependency_in_session(
                session, request, _domain(engine).evaluation_time()
            )
        assert (
            refused.value.code is WorkflowErrorCode.HISTORICAL_DEPENDENCY_NOT_ELIGIBLE
        )
        assert not session.new
        assert not session.dirty
    assert _project_business_rows(engine, scenario.project_id) == before


@pytest.mark.parametrize(
    "state", ["planned", "active", "retry-planned", "retry-active"]
)
def test_live_sprint_lifecycle_refuses_retirement(engine: Engine, state: str) -> None:
    """Done endpoint status cannot override planned, active or retry ownership."""
    fixture = _accepted_dependency_sprint(engine, started=state != "planned")
    scenario = fixture.scenario
    if state.startswith("retry-"):
        execution = execution_domain(engine)
        _close_execution_sprint(
            execution,
            project_id=scenario.project_id,
            sprint_id=fixture.sprint_id,
            story_id=fixture.story_id,
            task_id=fixture.task_id,
        )
        _triage_execution_sprint(
            execution, project_id=scenario.project_id, sprint_id=fixture.sprint_id
        )
        _plan_or_start_preservation_retry(
            engine, fixture, started=state == "retry-active"
        )
    else:
        with Session(engine) as session:
            row = session.get_one(UserStory, fixture.story_id)
            row.status = StoryStatus.DONE
            session.add(row)
            session.commit()
    snapshot = _snapshot(engine, scenario)
    edge = snapshot.story_dependencies[0]
    position = _domain(engine).position(scenario.project_id)
    assert not any(
        item.node_id == "planning.story_dependencies.retire_historical"
        and item.category is NodeCategory.AVAILABLE
        for item in position.decisions
    )
    request = requests.RetireHistoricalStoryDependency(
        project_id=scenario.project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint="unavailable",
        instance_key=f"story_dependency:{edge.dependency_id}",
        dependency_id=edge.dependency_id,
        dependency_fingerprint=canonical_hash(edge.model_dump(mode="json")),
        actor="operator",
        rationale="This must not bypass a live Sprint lifecycle.",
        idempotency_key="refuse-live-retirement",
    )
    before = _project_business_rows(engine, scenario.project_id)
    result = _domain(engine).transition(request)
    assert not result.ok
    with Session(engine) as session:
        with pytest.raises(retirement.StoryDependencyRetirementError) as refused:
            retirement.retire_historical_story_dependency_in_session(
                session, request, _domain(engine).evaluation_time()
            )
        assert (
            refused.value.code is WorkflowErrorCode.HISTORICAL_DEPENDENCY_NOT_ELIGIBLE
        )
        assert not session.new
        assert not session.dirty
    assert _project_business_rows(engine, scenario.project_id) == before


@pytest.mark.parametrize("field", ["actor", "rationale", "dependency_fingerprint"])
def test_retirement_key_collision_cannot_change_audit(
    engine: Engine, field: str
) -> None:
    """One idempotency key cannot represent changed intent or edge identity."""
    scenario, dependency_id = _historical_cycle(engine)
    request = _retire_request(engine, scenario, dependency_id)
    assert _domain(engine).transition(request).ok
    before = _project_business_rows(engine, scenario.project_id)
    value = (
        "sha256:" + "0" * 64
        if field == "dependency_fingerprint"
        else "Different intent"
    )
    result = _domain(engine).transition(request.model_copy(update={field: value}))
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert _project_business_rows(engine, scenario.project_id) == before


@pytest.mark.parametrize("membership_status", [StoryStatus.DONE, StoryStatus.TO_DO])
def test_retirement_preserves_completed_sprint_pinned_edge(
    engine: Engine, membership_status: StoryStatus
) -> None:
    """An effective retired edge retains its original accepted execution snapshot."""
    scenario = _seed(engine)
    a, b, current = scenario.story_ids
    with Session(engine) as session:
        story = session.get_one(UserStory, b)
        story.status = StoryStatus.DONE
        session.add(story)
        session.commit()
    dependency_id = _edge(engine, scenario, a, b)
    _complete_fixture_story_sprint(engine, scenario, a)
    with Session(engine) as session:
        story = session.get_one(UserStory, a)
        story.status = membership_status
        session.add(story)
        session.commit()
    _edge(engine, scenario, b, a)
    _review(engine, scenario, current)
    before = _snapshot(engine, scenario)
    stored = _project_business_rows(engine, scenario.project_id)
    assert (
        _domain(engine).transition(_retire_request(engine, scenario, dependency_id)).ok
    )
    after = _snapshot(engine, scenario)
    assert _completed_history(after) == _completed_history(before)
    assert after.tasks == before.tasks
    assert after.story_dependency_reviews == before.story_dependency_reviews
    assert (
        after.sprint_starts[0].dependency_rows_snapshot
        == before.sprint_starts[0].dependency_rows_snapshot
    )
    assert any(
        edge.dependency_id == dependency_id and edge.status == "active"
        for edge in after.sprint_starts[0].dependency_rows_snapshot
    )
    retained = _project_business_rows(engine, scenario.project_id)
    old_events = stored["workflow_events"]
    assert retained["workflow_events"][: len(old_events)] == old_events
    assert len(retained["workflow_events"]) == len(old_events) + 1
    assert {
        name: rows for name, rows in retained.items() if name != "workflow_events"
    } == {name: rows for name, rows in stored.items() if name != "workflow_events"}


def test_retirement_preserves_saved_invalidation_path(engine: Engine) -> None:
    """A saved active causal path remains verifiable after effective retirement."""
    scenario = _seed(engine)
    a, b, _current = scenario.story_ids
    dependency_id = _edge(engine, scenario, b, a)
    _replace(engine, scenario.artifact_ids[0])
    assert len(_events(engine)) == 1
    with Session(engine) as session:
        for story_id in (a, b):
            row = session.get_one(UserStory, story_id)
            row.status = StoryStatus.DONE
            session.add(row)
        session.commit()
        before = load_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id
        )
    assert (
        _domain(engine).transition(_retire_request(engine, scenario, dependency_id)).ok
    )
    with Session(engine) as session:
        assert (
            load_story_dependency_invalidations_in_session(
                session, project_id=scenario.project_id
            )
            == before
        )
    assert len(_events(engine)) == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "rationale",
        "dependency_fingerprint",
        "receipt",
        "duplicate",
        "incomplete_receipt",
        "failed_receipt",
        "edge",
    ],
)
def test_retirement_corruption_fails_closed(engine: Engine, tamper: str) -> None:
    """Malformed or conflicting audit never silently becomes accepted authority."""
    scenario, dependency_id = _historical_cycle(engine)
    request = _retire_request(engine, scenario, dependency_id)
    assert _domain(engine).transition(request).ok
    with Session(engine) as session:
        event = session.exec(
            select(WorkflowEvent).where(
                WorkflowEvent.event_type == WorkflowEventType.STORY_DEPENDENCY_RETIRED
            )
        ).one()
        assert event.event_metadata is not None
        payload = json.loads(event.event_metadata)
        if tamper in {"rationale", "dependency_fingerprint", "receipt"}:
            field = "receipt_id" if tamper == "receipt" else tamper
            payload[field] = (
                999999 if tamper == "receipt" else "Changed immutable evidence"
            )
            event.event_metadata = canonical_json(payload)
            session.add(event)
        elif tamper == "duplicate":
            session.add(WorkflowEvent(**event.model_dump(exclude={"event_id"})))
        elif tamper in {"incomplete_receipt", "failed_receipt"}:
            receipt = session.get_one(WorkflowTransitionReceipt, payload["receipt_id"])
            if tamper == "incomplete_receipt":
                receipt.result_json = None
            else:
                assert receipt.result_json is not None
                result = json.loads(receipt.result_json)
                result["ok"] = False
                receipt.result_json = canonical_json(result)
            session.add(receipt)
        else:
            edge = session.get_one(UserStoryDependency, dependency_id)
            edge.reason = "Changed original edge"
            session.add(edge)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        _snapshot(engine, scenario)


def test_retirement_writer_failure_rolls_back_event(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after flush leaves only the deterministic refusal receipt."""
    scenario, dependency_id = _historical_cycle(engine)
    request = _retire_request(engine, scenario, dependency_id)
    before = _project_business_rows(engine, scenario.project_id)
    load = retirement.load_story_dependency_retirements

    def reject_appended(session: Session, project_id: int) -> tuple:
        facts = load(session, project_id)
        if facts:
            message = "Injected verification refusal after retirement flush."
            raise ValueError(message)
        return facts

    monkeypatch.setattr(
        retirement, "load_story_dependency_retirements", reject_appended
    )
    result = _domain(engine).transition(request)
    assert not result.ok
    assert _project_business_rows(engine, scenario.project_id) == before
    assert _domain(engine).transition(request).replayed


def test_retirement_reload_rechecks_held_orm_identity(engine: Engine) -> None:
    """A caller's retained ORM row cannot hide a later changed original edge."""
    scenario, dependency_id = _historical_cycle(engine)
    assert (
        _domain(engine).transition(_retire_request(engine, scenario, dependency_id)).ok
    )
    with Session(engine) as reader:
        held = reader.get_one(UserStoryDependency, dependency_id)
        repository = WorkflowFactRepository(reader)
        assert (
            len(repository.load(scenario.project_id).story_dependency_retirements) == 1
        )
        with Session(engine) as writer:
            edge = writer.get_one(UserStoryDependency, dependency_id)
            edge.reason = "Changed in another committed operation"
            writer.add(edge)
            writer.commit()
        assert held.reason != "Changed in another committed operation"
        with pytest.raises(WorkflowFactLoadError):
            repository.load(scenario.project_id)


def test_retirement_reload_preserves_unflushed_caller_state(engine: Engine) -> None:
    """A canonical read validates committed evidence without flushing caller edits."""
    scenario, dependency_id = _historical_cycle(engine)
    assert (
        _domain(engine).transition(_retire_request(engine, scenario, dependency_id)).ok
    )
    with Session(engine) as reader:
        held = reader.get_one(UserStoryDependency, dependency_id)
        held.reason = "Unflushed caller edit"
        reader.add(held)
        assert (
            len(
                WorkflowFactRepository(reader)
                .load(scenario.project_id)
                .story_dependency_retirements
            )
            == 1
        )
        assert held.reason == "Unflushed caller edit"
        assert held in reader.dirty
        reader.rollback()


def test_concurrent_historical_retirement_has_one_winner(tmp_path: Path) -> None:
    """Competing guarded writers cannot retire the same edge twice."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'retirement-race.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    scenario, dependency_id = _historical_cycle(engine)
    first = _retire_request(engine, scenario, dependency_id)
    second = first.model_copy(update={"idempotency_key": "other-retirement-key"})
    barrier = Barrier(2)

    def run(request: requests.RetireHistoricalStoryDependency) -> bool:
        barrier.wait(timeout=10)
        return _domain(engine).transition(request).ok

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run, (first, second)))
    assert sum(results) == 1
    assert len(_snapshot(engine, scenario).story_dependency_retirements) == 1
    engine.dispose()


def _selected_terminal_retirement(
    engine: Engine, status: StoryStatus
) -> tuple[_Scenario, int]:
    scenario = _seed(engine)
    with Session(engine) as session:
        for story_id in scenario.story_ids[:2]:
            row = session.get_one(UserStory, story_id)
            row.status = status
            session.add(row)
        session.commit()
    dependency_id = _edge(engine, scenario, *scenario.story_ids[:2])
    _review(engine, scenario, scenario.story_ids[0])
    retired = _domain(engine).transition(_retire_request(engine, scenario, dependency_id))
    assert retired.ok, retired
    return scenario, dependency_id


def _retirement_audits(engine: Engine, project_id: int) -> tuple[str, ...]:
    with Session(engine) as session:
        return tuple(
            row.model_dump_json()
            for row in session.exec(
                select(WorkflowEvent)
                .where(
                    WorkflowEvent.project_id == project_id,
                    WorkflowEvent.event_type == WorkflowEventType.STORY_DEPENDENCY_RETIRED,
                )
                .order_by(col(WorkflowEvent.event_id))
            )
        )


@pytest.mark.parametrize("status", [StoryStatus.DONE, StoryStatus.ACCEPTED])
def test_retired_dependency_survives_selected_terminal_apply(
    engine: Engine, status: StoryStatus
) -> None:
    """Ordinary Apply cannot rewrite the original row of an audited retirement."""
    scenario, _ = _selected_terminal_retirement(engine, status)
    edges = _stored_edges(engine, scenario.project_id)
    audits = _retirement_audits(engine, scenario.project_id)
    reviews = _snapshot(engine, scenario).story_dependency_reviews
    applied = _domain(engine).transition(
        _apply_request(engine, scenario, scenario.story_ids[0], (), "apply-after-retirement")
    )
    assert applied.ok, applied
    assert _stored_edges(engine, scenario.project_id) == edges
    assert _retirement_audits(engine, scenario.project_id) == audits
    after = _snapshot(engine, scenario)
    assert all(review in after.story_dependency_reviews for review in reviews)
    assert len(after.story_dependency_retirements) == 1
    assert _domain(engine).position(scenario.project_id).project_id == scenario.project_id


@pytest.mark.parametrize("status", [StoryStatus.DONE, StoryStatus.ACCEPTED])
def test_retired_dependency_reactivation_is_atomic_conflict(
    engine: Engine, status: StoryStatus
) -> None:
    """A retired pair is refused explicitly before review or history mutation."""
    scenario, _ = _selected_terminal_retirement(engine, status)
    request = _apply_request(
        engine, scenario, scenario.story_ids[0], (scenario.story_ids[1],), "reactivate-retired-pair"
    )
    before = _project_business_rows(engine, scenario.project_id)
    audits = _retirement_audits(engine, scenario.project_id)
    refused = _domain(engine).transition(request)
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert "retired" in refused.error.message.lower()
    assert "reactivat" in refused.error.message.lower()
    assert _project_business_rows(engine, scenario.project_id) == before
    assert _retirement_audits(engine, scenario.project_id) == audits
    replay = _domain(engine).transition(request)
    assert replay.replayed
    assert replay.error == refused.error
    assert _project_business_rows(engine, scenario.project_id) == before


def _replacement_history_edge(
    engine: Engine, replacement_state: str = "completed"
) -> tuple[_Scenario, int, int]:
    project_id, retired_sprint_id, _ = _retired_replacement(
        engine, replacement_state=replacement_state
    )
    with Session(engine) as session:
        dependent_id = session.exec(
            select(SprintStory.story_id).where(SprintStory.sprint_id == retired_sprint_id)
        ).one()
        other_id = session.exec(
            select(UserStory.story_id).where(
                UserStory.project_id == project_id,
                UserStory.story_id != dependent_id,
                UserStory.status == StoryStatus.DONE,
            )
        ).one()
    scenario = _Scenario(project_id, (), (other_id, dependent_id))
    dependency_id = _edge(engine, scenario, other_id, dependent_id)
    return scenario, dependency_id, retired_sprint_id


def _proof_row(
    session: Session, retirement_id: int, kind: str
) -> tuple[SQLModel, str, object]:
    row = session.get_one(SprintPlanRetirement, retirement_id)
    if kind == "retirement":
        return row, "retirement_fingerprint", "corrupt-persisted-retirement"
    if kind == "receipt":
        return session.get_one(WorkflowTransitionReceipt, row.receipt_id), "request_kind", "wrong-kind"
    if kind == "plan":
        return session.get_one(SprintPlanArtifact, row.sprint_plan_artifact_id), "plan_fingerprint", "corrupt-plan"
    if kind == "decision":
        return session.get_one(SprintPlanArtifactDecision, row.sprint_plan_artifact_decision_id), "decision", "rejected"
    assert kind == "sprint"
    return session.get_one(Sprint, row.sprint_id), "status", SprintStatus.ACTIVE


def _proof_identity(engine: Engine, project_id: int) -> int:
    with Session(engine) as session:
        retirement_id = session.exec(
            select(SprintPlanRetirement.retirement_id).where(
                SprintPlanRetirement.project_id == project_id
            )
        ).one()
    assert isinstance(retirement_id, int)
    return retirement_id


def _assert_later_corruption_visible(
    engine: Engine, project_id: int, retirement_id: int, kind: str
) -> None:
    with Session(engine) as reader:
        held, field, invalid = _proof_row(reader, retirement_id, kind)
        retained = getattr(held, field)
        assert plan_retirement.load_sprint_plan_retirements(reader, project_id)
        with Session(engine) as writer:
            changed, _, _ = _proof_row(writer, retirement_id, kind)
            setattr(changed, field, invalid)
            writer.add(changed)
            writer.commit()
        identity = inspect_orm(held).identity
        assert identity is not None
        primary_key = next(iter(type(held).__table__.primary_key.columns))
        assert reader.exec(
            select(getattr(type(held), field)).where(primary_key == identity[0])
        ).one() == invalid
        if kind in {"retirement", "receipt"}:
            with pytest.raises(WorkflowFactLoadError):
                WorkflowFactRepository(reader).load(project_id)
        else:
            with pytest.raises(ValueError):
                plan_retirement.load_sprint_plan_retirements(reader, project_id)
        assert getattr(held, field) == retained
        assert not inspect_orm(held).modified
        assert held not in reader.dirty


def test_retired_plan_completed_replacement_allows_dependency_retirement(engine: Engine) -> None:
    """Retired planned membership supplies no current lock after replacement completion."""
    scenario, dependency_id, retired_sprint_id = _replacement_history_edge(engine)
    before = _project_business_rows(engine, scenario.project_id)
    snapshot = _snapshot(engine, scenario)
    assert dependency_id in {edge.dependency_id for edge in historical_dependency_edges(snapshot)}
    request = _retire_request(engine, scenario, dependency_id)
    result = _domain(engine).transition(request)
    assert result.ok, result
    assert _domain(engine).transition(request).replayed
    after = _project_business_rows(engine, scenario.project_id)
    assert {k: v for k, v in after.items() if k != WorkflowEvent.__tablename__} == {
        k: v for k, v in before.items() if k != WorkflowEvent.__tablename__
    }
    assert len(_snapshot(engine, scenario).story_dependency_retirements) == 1
    with Session(engine) as session:
        assert session.get_one(Sprint, retired_sprint_id).status is SprintStatus.PLANNED
    _assert_later_corruption_visible(
        engine, scenario.project_id, _proof_identity(engine, scenario.project_id), "retirement"
    )


@pytest.mark.parametrize("replacement_state", ["planned", "active"])
def test_retired_plan_does_not_bypass_live_replacement_guard(
    engine: Engine, replacement_state: str
) -> None:
    """Only retired membership loses authority; genuine live ownership still refuses."""
    scenario, dependency_id, _ = _replacement_history_edge(engine, replacement_state)
    with Session(engine) as session:
        endpoint = session.get_one(UserStory, scenario.story_ids[1])
        endpoint.status = StoryStatus.DONE
        session.add(endpoint)
        session.commit()
    before = _project_business_rows(engine, scenario.project_id)
    result = _domain(engine).transition(_retire_request(engine, scenario, dependency_id))
    assert not result.ok
    assert _project_business_rows(engine, scenario.project_id) == before


@pytest.mark.parametrize("kind", ["retirement", "receipt"])
def test_dependency_retirement_rejects_invalid_retired_plan_proof(engine: Engine, kind: str) -> None:
    """Invalid stored retirement authority fails before first historical repair."""
    project_id, _, _ = _retired_replacement(engine, replacement_state="planned")
    retirement_id = _proof_identity(engine, project_id)
    with Session(engine) as session:
        held, field, invalid = _proof_row(session, retirement_id, kind)
        setattr(held, field, invalid)
        session.add(held)
        session.commit()
    before = _project_business_rows(engine, project_id)
    with Session(engine) as session, pytest.raises(WorkflowFactLoadError):
        WorkflowFactRepository(session).load(project_id)
    assert _project_business_rows(engine, project_id) == before


@pytest.mark.parametrize("kind", ["retirement", "receipt", "plan", "decision", "sprint"])
def test_retired_plan_retained_proof_sees_later_committed_corruption(engine: Engine, kind: str) -> None:
    """A retained ORM identity cannot conceal transaction-visible invalid proof."""
    project_id, _, _ = _retired_replacement(engine, replacement_state="planned")
    assert _retirement_audits(engine, project_id) == ()
    _assert_later_corruption_visible(engine, project_id, _proof_identity(engine, project_id), kind)


@pytest.mark.parametrize("kind", ["retirement", "receipt"])
@pytest.mark.parametrize("persisted_valid", [False, True])
def test_retired_plan_unflushed_caller_proof_does_not_supply_authority(
    engine: Engine, kind: str, persisted_valid: bool
) -> None:
    """Proof reads neither trust nor flush pending caller edits and business rows."""
    project_id, _, _ = _retired_replacement(engine, replacement_state="planned")
    retirement_id = _proof_identity(engine, project_id)
    with Session(engine) as writer:
        row, field, invalid = _proof_row(writer, retirement_id, kind)
        valid = getattr(row, field)
        if not persisted_valid:
            setattr(row, field, invalid)
            writer.add(row)
            writer.commit()
    with Session(engine) as reader:
        held, field, invalid = _proof_row(reader, retirement_id, kind)
        setattr(held, field, invalid if persisted_valid else valid)
        reader.add(held)
        pending = Project(name="Pending caller business row")
        reader.add(pending)
        transaction = reader.get_transaction()
        if persisted_valid:
            try:
                assert plan_retirement.load_sprint_plan_retirements(reader, project_id)
            except ValueError as error:
                pytest.fail(f"Valid persisted proof was replaced by unflushed caller state: {error}")
        else:
            with pytest.raises(ValueError):
                plan_retirement.load_sprint_plan_retirements(reader, project_id)
        assert getattr(held, field) == (invalid if persisted_valid else valid)
        assert held in reader.dirty
        assert inspect_orm(held).modified
        assert reader.get_transaction() is transaction
        assert reader.get(type(held), inspect_orm(held).identity) is held
        assert pending in reader.new
        assert pending.project_id is None
        with reader.no_autoflush:
            assert reader.exec(select(Project).where(Project.name == pending.name)).first() is None
        reader.rollback()


def test_retired_plan_proof_rebuilds_after_flush_rollback_and_new_session(engine: Engine) -> None:
    """Proof follows the owning transaction without ending or repairing it."""
    project_id, _, _ = _retired_replacement(engine, replacement_state="planned")
    retirement_id = _proof_identity(engine, project_id)
    with Session(engine) as reader:
        held = reader.get_one(SprintPlanRetirement, retirement_id)
        valid = held.retirement_fingerprint
        held.retirement_fingerprint = "flushed-invalid-proof"
        reader.add(held)
        reader.flush()
        transaction = reader.get_transaction()
        with pytest.raises(ValueError):
            plan_retirement.load_sprint_plan_retirements(reader, project_id)
        assert reader.get_transaction() is transaction
        assert held.retirement_fingerprint == "flushed-invalid-proof"
        assert not inspect_orm(held).modified
        reader.rollback()
        assert plan_retirement.load_sprint_plan_retirements(reader, project_id)
        assert held.retirement_fingerprint == valid
        assert reader.get_one(SprintPlanRetirement, retirement_id) is held
    with Session(engine) as fresh:
        rows = plan_retirement.load_sprint_plan_retirements(fresh, project_id)
        assert next(iter(rows.values())).retirement_fingerprint == valid


def test_retired_plan_fresh_reader_preserves_active_writer_receipt(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the actual active retirement writer may validate an incomplete receipt."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    request = _retire_plan_request(engine, fixture)
    load = plan_retirement.load_sprint_plan_retirements
    observed: list[int] = []

    def inspect_writer(session: Session, project_id: int) -> dict[int, SprintPlanRetirement]:
        rows = load(session, project_id)
        for row in rows.values():
            receipt = session.get_one(WorkflowTransitionReceipt, row.receipt_id)
            marker = session.info.get("agileforge.active_transition_receipt")
            if receipt.result_json is None:
                assert isinstance(marker, dict)
                assert marker["receipt_id"] == row.receipt_id
                observed.append(row.receipt_id)
        return rows

    monkeypatch.setattr(plan_retirement, "load_sprint_plan_retirements", inspect_writer)
    result = _domain(engine).transition(request)
    assert result.ok, result
    assert observed
    replay = _domain(engine).transition(request)
    assert replay.ok and replay.replayed
    with Session(engine) as writer:
        receipt = writer.get_one(WorkflowTransitionReceipt, observed[0])
        receipt.result_json = None
        writer.add(receipt)
        writer.commit()
    with Session(engine) as fresh, pytest.raises(ValueError):
        load(fresh, fixture.scenario.project_id)
