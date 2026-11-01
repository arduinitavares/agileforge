# tests/workflow/test_sprint_plan_retirement.py
"""Provider-free recovery of accepted, stale, unstarted Sprint Plans."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from models.core import Project, Sprint, UserStory
from models.db import ensure_business_db_ready
from models.enums import SprintStatus
from models.workflow import SprintPlanArtifactDecision, SprintPlanRetirement
from repositories.project import ProjectRepository
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.sprint_phase import (
    SprintStartInput,
    start_sprint_in_session,
)
from services.contracts.sprint import SprintPlannerOutput
from services.sprint_plan_retirement import retirement_fingerprint
from services.story_sprint_selection import (
    StorySprintSelectionRequest,
    apply_story_sprint_selection_with_receipt_in_session,
    story_sprint_selection_fact_in_session,
)
from tests.workflow.planning_fixtures import (
    EVALUATED_AT,
    planning_decision,
    planning_guards,
)
from tests.workflow.test_planning_transitions import _sprint_plan
from tests.workflow.test_superseded_story_dependencies import (
    _accepted_dependency_sprint,
    _domain,
    _project_business_rows,
    _replace,
    _request,
    _stored_sprint_evidence,
)
from workflow import requests
from workflow.contracts import NodeCategory
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.requests import DecideSprintPlan, RecordSprintPlan, StartSprint

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from tests.workflow.test_superseded_story_dependencies import _DependencySprint


def test_stale_plan_retirement_releases_only_its_planning_lock(engine: Engine) -> None:  # noqa: PLR0915
    """Retire explicitly, replay once, and preserve the accepted projection."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    project_id = fixture.scenario.project_id
    _replace(engine, fixture.scenario.artifact_ids[0])
    domain = _domain(engine)
    stale_start = StartSprint(
        **planning_guards(domain.position(project_id), "planning.sprint.start"),
        idempotency_key="refuse-original-stale-plan",
    )
    refused = domain.transition(stale_start)
    assert not refused.ok
    assert domain.transition(stale_start).replayed
    before = _project_business_rows(engine, project_id)
    evidence = _stored_sprint_evidence(engine, fixture)
    decision = planning_decision(domain.position(project_id), "planning.sprint.retire")
    assert (decision.category, decision.reason_code) == (
        NodeCategory.AVAILABLE,
        "STALE_SPRINT_PLAN_RETIREMENT_AVAILABLE",
    )
    with Session(engine) as session:
        accepted = session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.sprint_plan_artifact_id == fixture.plan_id
            )
        ).one()
        assert accepted.sprint_plan_artifact_decision_id is not None
        request = requests.RetireSprintPlan(
            **planning_guards(domain.position(project_id), "planning.sprint.retire"),
            sprint_plan_artifact_id=fixture.plan_id,
            sprint_plan_artifact_decision_id=accepted.sprint_plan_artifact_decision_id,
            sprint_id=fixture.sprint_id,
            plan_fingerprint=accepted.plan_fingerprint,
            rationale="Retire the obsolete plan so dependencies can be reviewed.",
            idempotency_key="retire-stale-plan",
        )
    result = domain.transition(request)
    assert result.ok, result
    assert result.output["sprint_id"] == fixture.sprint_id
    replay = _domain(engine).transition(request)
    assert replay.ok
    assert replay.replayed
    assert replay.output == result.output
    after = _project_business_rows(engine, project_id)
    assert {
        name: rows for name, rows in after.items() if name != "sprint_plan_retirements"
    } == {
        name: rows for name, rows in before.items() if name != "sprint_plan_retirements"
    }
    retained = _stored_sprint_evidence(engine, fixture)
    assert {
        name: rows
        for name, rows in retained.items()
        if name != "sprint_plan_retirements"
    } == {
        name: rows
        for name, rows in evidence.items()
        if name != "sprint_plan_retirements"
    }
    dependency_decision = planning_decision(
        _domain(engine).position(project_id), "planning.story_dependencies"
    )
    assert (
        dependency_decision.reason_code != "SPRINT_DEPENDENCY_REVIEW_LIFECYCLE_LOCKED"
    )
    with Session(engine) as session:
        with pytest.raises(
            ValueError, match="cannot resolve one current accepted plan"
        ):
            start_sprint_in_session(
                session,
                SprintStartInput(
                    project_id=project_id,
                    expected_sprint_id=fixture.sprint_id,
                    expected_task_content_fingerprint="old-tasks",
                    decision_fingerprint="old-decision",
                    started_by="operator",
                    started_at=EVALUATED_AT,
                ),
            )
        assert not session.new
        assert not session.dirty
        assert not session.deleted

    # A person explicitly drops the obsolete edge; no replacement is inferred.
    reconciled = domain.transition(
        _request(
            engine,
            fixture.scenario,
            fixture.story_id,
            (),
            "reconcile-retired-dependencies",
        )
    )
    assert reconciled.ok, reconciled
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    spec = accepted_current_spec(snapshot)
    assert spec is not None
    story = next(item for item in snapshot.stories if item.story_id == fixture.story_id)
    recorded = domain.transition(
        RecordSprintPlan(
            **planning_guards(domain.position(project_id), "planning.sprint.plan"),
            team_name="Replacement Sprint team",
            idempotency_key="replacement-sprint-plan",
            spec_version_id=spec.spec_version_id,
            spec_hash=spec.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(
                _sprint_plan(
                    fixture.story_id,
                    story_item_id=story.source_story_item_id,
                    spec_item_ids=story.spec_item_ids,
                )
            ),
        )
    )
    assert recorded.ok, recorded
    plan_id = recorded.output["sprint_plan_artifact_id"]
    fingerprint = recorded.output["plan_fingerprint"]
    assert isinstance(plan_id, int)
    assert isinstance(fingerprint, str)
    assert plan_id != fixture.plan_id
    accepted = domain.transition(
        DecideSprintPlan(
            **planning_guards(domain.position(project_id), "planning.sprint.review"),
            idempotency_key="accept-replacement-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept the explicitly reconciled replacement plan.",
        )
    )
    assert accepted.ok, accepted
    new_sprint_id = accepted.output["activated_sprint_id"]
    assert new_sprint_id != fixture.sprint_id
    start = domain.transition(
        StartSprint(
            **planning_guards(domain.position(project_id), "planning.sprint.start"),
            idempotency_key="start-replacement-sprint",
        )
    )
    assert start.ok, start
    assert _domain(engine).transition(stale_start).replayed
    assert (
        not _domain(engine)
        .transition(
            stale_start.model_copy(
                update={"idempotency_key": "old-plan-never-restarts"}
            )
        )
        .ok
    )
    with Session(engine) as session:
        assert session.get_one(Sprint, fixture.sprint_id).status is SprintStatus.PLANNED
        assert session.get_one(Sprint, new_sprint_id).status is SprintStatus.ACTIVE
        assert len(session.exec(select(SprintPlanRetirement)).all()) == 1
    final = _stored_sprint_evidence(engine, fixture)
    assert {
        name: rows for name, rows in final.items() if name != "sprint_plan_retirements"
    } == {
        name: rows
        for name, rows in evidence.items()
        if name != "sprint_plan_retirements"
    }


def _retire_request(
    engine: Engine, fixture: _DependencySprint
) -> requests.RetireSprintPlan:
    """Bind an explicit operator request to the current retirement decision."""
    with Session(engine) as session:
        accepted = session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.sprint_plan_artifact_id == fixture.plan_id
            )
        ).one()
        assert accepted.sprint_plan_artifact_decision_id is not None
        return requests.RetireSprintPlan(
            **planning_guards(
                _domain(engine).position(fixture.scenario.project_id),
                "planning.sprint.retire",
            ),
            sprint_plan_artifact_id=fixture.plan_id,
            sprint_plan_artifact_decision_id=accepted.sprint_plan_artifact_decision_id,
            sprint_id=fixture.sprint_id,
            plan_fingerprint=accepted.plan_fingerprint,
            rationale="Retire this obsolete unstarted plan.",
            idempotency_key="retire-guarded-plan",
        )


@pytest.mark.parametrize(
    "change",
    [
        {"sprint_id": 99999},
        {"sprint_plan_artifact_id": 99999},
        {"sprint_plan_artifact_decision_id": 99999},
        {"plan_fingerprint": "wrong-plan"},
        {"fact_fingerprint": "stale"},
        {"decision_fingerprint": "stale"},
    ],
)
def test_retirement_rejects_invalid_guards_without_domain_writes(
    engine: Engine, change: dict[str, object]
) -> None:
    """Mismatched identity and stale freshness never release a planning lock."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    request = _retire_request(engine, fixture).model_copy(update=change)
    before = _project_business_rows(engine, fixture.scenario.project_id)
    refused = _domain(engine).transition(request)
    assert not refused.ok
    assert _project_business_rows(engine, fixture.scenario.project_id) == before


@pytest.mark.parametrize("completed", [False, True])
def test_retirement_rejects_started_sprint_without_writes(
    engine: Engine, completed: bool
) -> None:
    """A started Sprint retains its original dependency-review authority."""
    fixture = _accepted_dependency_sprint(engine, started=True)
    if completed:
        with Session(engine) as session:
            sprint = session.get_one(Sprint, fixture.sprint_id)
            sprint.status = SprintStatus.COMPLETED
            sprint.completed_at = EVALUATED_AT
            session.add(sprint)
            session.commit()
    _replace(engine, fixture.scenario.artifact_ids[0])
    before = _project_business_rows(engine, fixture.scenario.project_id)
    result = _domain(engine).transition(_retire_request(engine, fixture))
    assert not result.ok
    assert _project_business_rows(engine, fixture.scenario.project_id) == before
    decision = planning_decision(
        _domain(engine).position(fixture.scenario.project_id),
        "planning.story_dependencies",
    )
    assert decision.reason_code == "SPRINT_DEPENDENCY_REVIEW_LIFECYCLE_LOCKED"


def test_schema_upgrade_preserves_accepted_plan_rows(engine: Engine) -> None:
    """Install retirement evidence storage without rewriting accepted history."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    before = _stored_sprint_evidence(engine, fixture)
    SQLModel.metadata.tables["sprint_plan_retirements"].drop(engine)
    ensure_business_db_ready(engine)
    assert _stored_sprint_evidence(engine, fixture) == before


def test_production_upgrade_preserves_accepted_sprint_history(tmp_path: Path) -> None:
    """The registered production migration preserves history and enables recovery."""
    from cli.production_schema_upgrade import (  # noqa: PLC0415
        CURRENT_SCHEMA_ID,
        upgrade_registered_database,
    )

    database = tmp_path / "production.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    try:
        ensure_business_db_ready(engine)
        fixture = _accepted_dependency_sprint(engine, started=False)
        before = _stored_sprint_evidence(engine, fixture)
        SQLModel.metadata.tables["sprint_plan_retirements"].drop(engine)
        engine.dispose()

        installed = upgrade_registered_database(database)

        assert installed.release_id == CURRENT_SCHEMA_ID
        assert _stored_sprint_evidence(engine, fixture) == before
        _replace(engine, fixture.scenario.artifact_ids[0])
        assert _domain(engine).transition(_retire_request(engine, fixture)).ok
        with Session(engine) as session:
            assert len(session.exec(select(SprintPlanRetirement)).all()) == 1
    finally:
        engine.dispose()


def test_retirement_releases_story_scope_selection(engine: Engine) -> None:
    """A person can remove an old selected Story after explicit retirement."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    assert _domain(engine).transition(_retire_request(engine, fixture)).ok
    with Session(engine) as session:
        story = session.get_one(UserStory, fixture.story_id)
        current = story_sprint_selection_fact_in_session(session, story=story)
        result = apply_story_sprint_selection_with_receipt_in_session(
            session,
            StorySprintSelectionRequest(
                project_id=fixture.scenario.project_id,
                story_id=fixture.story_id,
                intent="remove",
                expected_state_fingerprint=current.state_fingerprint,
                actor="operator",
                idempotency_key="deselect-retired-scope",
            ),
        )
        session.commit()
        assert result.selection_state == "unselected"


@pytest.mark.parametrize("competitor", ["start", "retire"])
def test_concurrent_retirement_has_one_winner(tmp_path: Path, competitor: str) -> None:
    """The writer transaction serializes stale start and retirement competitors."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    domain = _domain(engine)
    request = _retire_request(engine, fixture)
    competing = (
        StartSprint(
            **planning_guards(
                domain.position(fixture.scenario.project_id), "planning.sprint.start"
            ),
            idempotency_key="race-start",
        )
        if competitor == "start"
        else request.model_copy(update={"idempotency_key": "race-retire"})
    )
    barrier = Barrier(2)

    def run(command: requests.TransitionRequest) -> bool:
        barrier.wait(timeout=10)
        return _domain(engine).transition(command).ok

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run, (request, competing)))
    assert sum(results) == 1
    with Session(engine) as session:
        assert len(session.exec(select(SprintPlanRetirement)).all()) == 1
        assert session.get_one(Sprint, fixture.sprint_id).status is SprintStatus.PLANNED
    engine.dispose()


def test_corrupt_retirement_evidence_never_unlocks_planning(engine: Engine) -> None:
    """A changed retirement record fails closed on the next fact reload."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    assert _domain(engine).transition(_retire_request(engine, fixture)).ok
    with Session(engine) as session:
        retirement = session.exec(select(SprintPlanRetirement)).one()
        retirement.rationale = "Rewritten evidence"
        session.add(retirement)
        session.commit()
    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match="retirement evidence conflicts"),
    ):
        WorkflowFactRepository(session).load(fixture.scenario.project_id)


def test_retirement_digest_must_match_its_completed_receipt(engine: Engine) -> None:
    """A rewritten retirement cannot substitute for the original outcome."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    assert _domain(engine).transition(_retire_request(engine, fixture)).ok
    with Session(engine) as session:
        retirement = session.exec(select(SprintPlanRetirement)).one()
        retirement.candidate_set_fingerprint = "rewritten-current-candidates"
        retirement.retirement_fingerprint = retirement_fingerprint(retirement)
        session.add(retirement)
        session.commit()
    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match="retirement"),
    ):
        WorkflowFactRepository(session).load(fixture.scenario.project_id)


def test_retirement_cannot_target_another_existing_project(engine: Engine) -> None:
    """A valid Project cannot retire another Project's accepted plan."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    with Session(engine) as session:
        other = Project(name="Other Project")
        session.add(other)
        session.commit()
        other_id = other.project_id
    before = _project_business_rows(engine, fixture.scenario.project_id)
    request = _retire_request(engine, fixture).model_copy(
        update={"project_id": other_id}
    )
    assert not _domain(engine).transition(request).ok
    assert _project_business_rows(engine, fixture.scenario.project_id) == before


def test_project_deletion_includes_its_retirement_records(engine: Engine) -> None:
    """The existing explicit Project deletion leaves no retirement orphan."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    with Session(engine) as session:
        story = session.get_one(UserStory, fixture.story_id)
        story.story_points = 99
        session.add(story)
        session.commit()
    assert _domain(engine).transition(_retire_request(engine, fixture)).ok
    with Session(engine) as session:
        assert ProjectRepository(session).delete_project(fixture.scenario.project_id)
        assert session.exec(select(SprintPlanRetirement)).all() == []
