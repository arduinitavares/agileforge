# tests/workflow/test_sprint_plan_retirement_history.py
"""Retire an unstarted Sprint stream across normal plan revision histories."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

import pytest
from sqlmodel import Session, col, select

from models.core import Sprint, UserStory
from models.enums import SprintStatus
from models.workflow import SprintPlanArtifact, SprintPlanArtifactDecision
from repositories.workflow import WorkflowFactRepository
from services.agent_workbench.sprint_phase import (
    RecordSprintPlanDecisionInput,
    record_sprint_plan_decision_in_session,
)
from services.contracts.sprint import SprintPlannerOutput
from services.planning_lineage import PlanningLineageError
from services.story_sprint_selection import (
    StorySprintSelectionMutationError,
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
from tests.workflow.test_sprint_plan_retirement import _retire_request
from tests.workflow.test_superseded_story_dependencies import (
    _accepted_dependency_sprint,
    _domain,
    _project_business_rows,
    _rejection_receipt,
    _replace,
    _request,
    _stored_sprint_evidence,
)
from workflow.contracts import NodeCategory
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.requests import DecideSprintPlan, RecordSprintPlan, StartSprint

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from tests.workflow.test_superseded_story_dependencies import _DependencySprint


def _revision(
    engine: Engine,
    fixture: _DependencySprint,
    *,
    outcome: Literal["pending", "feedback", "rejected", "accepted"],
    key: str,
) -> int:
    domain = _domain(engine)
    project_id = fixture.scenario.project_id
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    spec = accepted_current_spec(snapshot)
    assert spec is not None
    story = next(item for item in snapshot.stories if item.story_id == fixture.story_id)
    plan = _sprint_plan(
        fixture.story_id,
        story_item_id=story.source_story_item_id,
        spec_item_ids=story.spec_item_ids,
    )
    plan["sprint_goal"] = f"Review the unstarted Sprint for {key}."
    drafted = domain.transition(
        RecordSprintPlan(
            **planning_guards(domain.position(project_id), "planning.sprint.plan"),
            idempotency_key=key,
            team_name="Prerequisite replacement preservation team",
            spec_version_id=spec.spec_version_id,
            spec_hash=spec.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan),
        )
    )
    assert drafted.ok, drafted
    artifact_id = drafted.output["sprint_plan_artifact_id"]
    fingerprint = drafted.output["plan_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    if outcome != "pending":
        reviewed = domain.transition(
            DecideSprintPlan(
                **planning_guards(
                    domain.position(project_id), "planning.sprint.review"
                ),
                idempotency_key=f"{key}-review",
                sprint_plan_artifact_id=artifact_id,
                plan_fingerprint=fingerprint,
                decision=outcome,
                rationale="Explicit review of the proposed Sprint revision.",
            )
        )
        assert reviewed.ok, reviewed
    return artifact_id


def _history(
    engine: Engine, artifact_ids: tuple[int, ...]
) -> dict[str, tuple[str, ...]]:
    with Session(engine) as session:
        plans = session.exec(
            select(SprintPlanArtifact)
            .where(col(SprintPlanArtifact.sprint_plan_artifact_id).in_(artifact_ids))
            .order_by(col(SprintPlanArtifact.sprint_plan_artifact_id))
        ).all()
        decisions = session.exec(
            select(SprintPlanArtifactDecision)
            .where(
                col(SprintPlanArtifactDecision.sprint_plan_artifact_id).in_(
                    artifact_ids
                )
            )
            .order_by(col(SprintPlanArtifactDecision.sprint_plan_artifact_decision_id))
        ).all()
        return {
            "plans": tuple(row.model_dump_json() for row in plans),
            "decisions": tuple(row.model_dump_json() for row in decisions),
        }


def _scope_change(
    engine: Engine,
    fixture: _DependencySprint,
    *,
    intent: Literal["select", "remove", "defer"],
    key: str,
) -> str:
    with Session(engine) as session:
        story = session.get_one(UserStory, fixture.story_id)
        current = story_sprint_selection_fact_in_session(session, story=story)
        result = apply_story_sprint_selection_with_receipt_in_session(
            session,
            StorySprintSelectionRequest(
                project_id=fixture.scenario.project_id,
                story_id=fixture.story_id,
                intent=intent,
                expected_state_fingerprint=current.state_fingerprint,
                actor="operator",
                idempotency_key=key,
            ),
        )
        session.commit()
        return result.selection_state


def _replacement_start(engine: Engine, fixture: _DependencySprint) -> int:
    domain = _domain(engine)
    project_id = fixture.scenario.project_id
    reconciled = domain.transition(
        _request(engine, fixture.scenario, fixture.story_id, (), "history-reconcile")
    )
    assert reconciled.ok, reconciled
    plan_id = _revision(engine, fixture, outcome="accepted", key="history-replacement")
    with Session(engine) as session:
        accepted = session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.sprint_plan_artifact_id == plan_id
            )
        ).one()
        new_sprint_id = accepted.activated_sprint_id
    assert isinstance(new_sprint_id, int)
    assert new_sprint_id != fixture.sprint_id
    for started in (False, True):
        if started:
            result = domain.transition(
                StartSprint(
                    **planning_guards(
                        domain.position(project_id), "planning.sprint.start"
                    ),
                    idempotency_key="history-start-replacement",
                )
            )
            assert result.ok, result
        with pytest.raises(StorySprintSelectionMutationError) as locked:
            _scope_change(engine, fixture, intent="remove", key=f"locked-{started}")
        assert locked.value.code == "SELECTION_LIFECYCLE_LOCKED"
    with Session(engine) as session:
        assert session.get_one(Sprint, fixture.sprint_id).status is SprintStatus.PLANNED
        assert session.get_one(Sprint, new_sprint_id).status is SprintStatus.ACTIVE
    return new_sprint_id


@pytest.mark.parametrize("outcome", ["pending", "feedback", "rejected"])
def test_retirement_recovers_with_an_unaccepted_successor(
    engine: Engine, outcome: Literal["pending", "feedback", "rejected"]
) -> None:
    """A later draft or negative review cannot strand the accepted stale stream."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    successor_id = _revision(engine, fixture, outcome=outcome, key="history-successor")
    ids = (fixture.plan_id, successor_id)
    history = _history(engine, ids)
    evidence = _stored_sprint_evidence(engine, fixture)
    _replace(engine, fixture.scenario.artifact_ids[0])
    domain = _domain(engine)
    project_id = fixture.scenario.project_id
    stale_start = StartSprint(
        **planning_guards(domain.position(project_id), "planning.sprint.start"),
        idempotency_key="history-stale-start",
    )
    assert not domain.transition(stale_start).ok
    assert domain.transition(stale_start).replayed
    receipt = _rejection_receipt(engine, stale_start)
    before = _project_business_rows(engine, project_id)
    decision = planning_decision(domain.position(project_id), "planning.sprint.retire")
    assert decision.category is NodeCategory.AVAILABLE
    request = _retire_request(engine, fixture)
    retired = domain.transition(request)
    assert retired.ok, retired
    replay = _domain(engine).transition(request)
    assert replay.replayed
    assert replay.output == retired.output
    after = _project_business_rows(engine, project_id)
    assert {k: v for k, v in after.items() if k != "sprint_plan_retirements"} == {
        k: v for k, v in before.items() if k != "sprint_plan_retirements"
    }
    with Session(engine) as session:
        descendant = session.get_one(SprintPlanArtifact, successor_id)
        with pytest.raises(
            (ValueError, PlanningLineageError),
            match=r"LINEAGE_AMBIGUOUS|already has an authoritative decision",
        ):
            record_sprint_plan_decision_in_session(
                session,
                inputs=RecordSprintPlanDecisionInput(
                    artifact=descendant,
                    decision="accepted",
                    rationale="A retired descendant must not activate.",
                    reviewer="operator",
                    idempotency_key="history-refuse-descendant",
                    decided_at=EVALUATED_AT,
                ),
            )
        assert not session.new
        assert not session.dirty
        assert not session.deleted
    _replacement_start(engine, fixture)
    assert _history(engine, ids) == history
    assert _rejection_receipt(engine, stale_start) == receipt
    retained = _stored_sprint_evidence(engine, fixture)
    assert {k: v for k, v in retained.items() if k != "sprint_plan_retirements"} == {
        k: v for k, v in evidence.items() if k != "sprint_plan_retirements"
    }


@pytest.mark.parametrize("feedback_between", [False, True])
@pytest.mark.parametrize(
    ("intent", "expected"), [("remove", "unselected"), ("defer", "deferred")]
)
def test_retirement_releases_scope_from_all_accepted_stream_versions(
    engine: Engine,
    feedback_between: bool,
    intent: Literal["remove", "defer"],
    expected: str,
) -> None:
    """Historical accepted versions stop locking scope only for their retired Sprint."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    ids = [fixture.plan_id]
    if feedback_between:
        ids.append(
            _revision(engine, fixture, outcome="feedback", key="history-feedback")
        )
    ids.append(_revision(engine, fixture, outcome="accepted", key="history-accepted"))
    fixture = replace(fixture, plan_id=ids[-1])
    history = _history(engine, tuple(ids))
    evidence = _stored_sprint_evidence(engine, fixture)
    _replace(engine, fixture.scenario.artifact_ids[0])
    retired = _domain(engine).transition(_retire_request(engine, fixture))
    assert retired.ok, retired
    assert (
        _scope_change(engine, fixture, intent=intent, key="history-change-scope")
        == expected
    )
    assert (
        _scope_change(engine, fixture, intent="select", key="history-reselect")
        == "selected"
    )
    _replacement_start(engine, fixture)
    assert _history(engine, tuple(ids)) == history
    retained = _stored_sprint_evidence(engine, fixture)
    assert {k: v for k, v in retained.items() if k != "sprint_plan_retirements"} == {
        k: v for k, v in evidence.items() if k != "sprint_plan_retirements"
    }
