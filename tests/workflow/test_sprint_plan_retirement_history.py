# tests/workflow/test_sprint_plan_retirement_history.py
"""Retire an unstarted Sprint stream across normal plan revision histories."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest
from sqlmodel import Session, col, select

from models.core import Sprint, Task, UserStory
from models.enums import SprintStatus
from models.events import TaskExecutionLog
from models.workflow import (
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    StoryArtifact,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.sprint_phase import (
    RecordSprintPlanDecisionInput,
    record_sprint_plan_decision_in_session,
)
from services.contracts.sprint import SprintPlannerOutput
from services.packets.canonical import build_story_packet, build_task_packet
from services.planning_lineage import PlanningLineageError
from services.read_projections import DurableReadProjectionService
from services.story_sprint_selection import (
    StorySprintSelectionMutationError,
    StorySprintSelectionRequest,
    apply_story_sprint_selection_with_receipt_in_session,
    story_sprint_selection_fact_in_session,
)
from tests.workflow.execution_fixtures import unbind_synthetic_execution_repository
from tests.workflow.execution_retry_support import (
    _close_execution_sprint,
    _triage_execution_sprint,
)
from tests.workflow.execution_retry_support import (
    _domain as execution_domain,
)
from tests.workflow.planning_fixtures import (
    EVALUATED_AT,
    apply_current_dependencies,
    planning_decision,
    planning_guards,
    select_for_sprint,
)
from tests.workflow.retry_execution_fixtures import (
    CompletedRetrySource,
    record_pending_successor_plan,
    seed_completed_retry_source,
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
from utils.task_metadata import parse_task_metadata
from workflow.contracts import NodeCategory
from workflow.definitions.planning import _sprint_plan_rule
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.execution_integrity import execution_contract
from workflow.execution_scope import current_execution_scope
from workflow.facts import SprintRetryFact, SprintRetryStartFact, WorkflowFactSnapshot
from workflow.fingerprints import canonical_json
from workflow.requests import (
    DecideSprintPlan,
    RecordSprintPlan,
    RetireSprintPlan,
    StartSprint,
)

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


def _retired_replacement(
    engine: Engine,
    *,
    replacement_state: Literal["planned", "active", "completed"],
    successor_outcome: Literal["none", "pending", "feedback", "rejected"] = "none",
    story_count: int = 2,
) -> tuple[int, int, int]:
    """Retain a retired stream while a distinct replacement advances normally."""
    fixture = _accepted_dependency_sprint(
        engine, started=False, story_count=story_count
    )
    project_id = fixture.scenario.project_id
    artifact_ids = [fixture.plan_id]
    if successor_outcome != "none":
        artifact_ids.append(
            _revision(engine, fixture, outcome=successor_outcome, key="scope-successor")
        )
    history = _history(engine, tuple(artifact_ids))
    _replace(engine, fixture.scenario.artifact_ids[0])
    domain = _domain(engine)
    retired = domain.transition(_retire_request(engine, fixture))
    assert retired.ok, retired
    retired_evidence = _stored_sprint_evidence(engine, fixture)
    reconciled = domain.transition(
        _request(engine, fixture.scenario, fixture.story_id, (), "scope-reconcile")
    )
    assert reconciled.ok, reconciled
    plan_id = _revision(engine, fixture, outcome="accepted", key="scope-replacement")
    with Session(engine) as session:
        accepted = session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.sprint_plan_artifact_id == plan_id
            )
        ).one()
        replacement_id = accepted.activated_sprint_id
    assert isinstance(replacement_id, int)
    assert replacement_id != fixture.sprint_id
    if replacement_state != "planned":
        started = domain.transition(
            StartSprint(
                **planning_guards(domain.position(project_id), "planning.sprint.start"),
                idempotency_key="scope-start-replacement",
            )
        )
        assert started.ok, started
        unbind_synthetic_execution_repository(engine, project_id)
    if replacement_state == "completed":
        with Session(engine) as session:
            snapshot = WorkflowFactRepository(session).load(project_id)
        contract = execution_contract(snapshot, replacement_id)
        assert len(contract.tasks) == 1
        execution = execution_domain(engine)
        _close_execution_sprint(
            execution,
            project_id=project_id,
            sprint_id=replacement_id,
            story_id=fixture.story_id,
            task_id=contract.tasks[0].task_id,
            idempotency_suffix="-scope-replacement",
        )
        _triage_execution_sprint(
            execution, project_id=project_id, sprint_id=replacement_id
        )
    assert _history(engine, tuple(artifact_ids)) == history
    assert _stored_sprint_evidence(engine, fixture) == retired_evidence
    with Session(engine) as session:
        assert session.get_one(Sprint, fixture.sprint_id).status is SprintStatus.PLANNED
    return project_id, fixture.sprint_id, replacement_id


@pytest.mark.parametrize("successor_outcome", ["pending", "feedback", "rejected"])
def test_retired_pending_successor_allows_real_next_sprint_plan(
    engine: Engine,
    successor_outcome: Literal["pending", "feedback", "rejected"],
) -> None:
    """A retired review cannot block a new stream after replacement triage."""
    project_id, retired_id, replacement_id = _retired_replacement(
        engine,
        replacement_state="completed",
        successor_outcome=successor_outcome,
        story_count=3,
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        retired_plan = next(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
            and item.status == "retired"
            and item.activated_sprint_id == retired_id
        )
        successor = next(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
            and item.sprint_plan_stream_id == retired_plan.sprint_plan_stream_id
            and item.supersedes_artifact_id == retired_plan.artifact_id
        )
        next_story = next(
            item
            for item in snapshot.stories
            if not item.is_superseded and item.spec_item_ids == ("REQ.planning-3",)
        )
        story_artifact = session.get_one(
            StoryArtifact, next_story.source_story_artifact_id
        )
        assert story_artifact.backlog_item_id == "PBI-000003"
    expected_status = (
        "pending_review" if successor_outcome == "pending" else successor_outcome
    )
    assert successor.status == expected_status
    assert replacement_id in {item.sprint_id for item in snapshot.post_sprint_triage}
    before = _project_business_rows(engine, project_id)
    receipts = _retained_project_receipts(engine, project_id)
    domain = _domain(engine)
    select_for_sprint(engine, next_story.story_id)
    apply_current_dependencies(
        engine, domain, project_id, idempotency_key="next-cycle-explicit-dependencies"
    )
    position = domain.position(project_id)
    with Session(engine) as session:
        observed = WorkflowFactRepository(session).load(project_id)
    assert _sprint_plan_rule(observed, EVALUATED_AT)[0].reason_code == (
        "NEXT_SPRINT_PLANNING_REQUIRED"
    )
    decision = planning_decision(position, "planning.sprint.plan")
    assert decision.category is NodeCategory.AVAILABLE, decision
    assert decision.reason_code == "NEXT_SPRINT_PLANNING_REQUIRED"
    spec = accepted_current_spec(observed)
    assert spec is not None
    recorded = domain.transition(
        RecordSprintPlan(
            **planning_guards(position, "planning.sprint.plan"),
            team_name="Next reviewed Sprint team",
            idempotency_key="next-cycle-plan-after-retired-review",
            spec_version_id=spec.spec_version_id,
            spec_hash=spec.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(
                _sprint_plan(
                    next_story.story_id,
                    story_item_id=next_story.source_story_item_id,
                    spec_item_ids=next_story.spec_item_ids,
                )
            ),
        )
    )
    assert recorded.ok, recorded
    plan_id = recorded.output["sprint_plan_artifact_id"]
    assert isinstance(plan_id, int)
    with Session(engine) as session:
        plan = session.get_one(SprintPlanArtifact, plan_id)
        assert plan.sprint_plan_stream_id not in {
            item.sprint_plan_stream_id
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
        }
        assert plan.selected_story_ids_json == canonical_json([next_story.story_id])
        after_snapshot = WorkflowFactRepository(session).load(project_id)
    after = _project_business_rows(engine, project_id)
    for table, rows in before.items():
        assert all(row in after[table] for row in rows), table
    after_receipts = _retained_project_receipts(engine, project_id)
    assert {key: after_receipts[key] for key in receipts} == receipts
    assert successor in after_snapshot.planning_artifacts
    assert retired_plan in after_snapshot.planning_artifacts


def _correction_replacement(engine: Engine) -> tuple[_DependencySprint, int, int]:
    """Keep the real original fixture available for repeated replacement review."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    domain = _domain(engine)
    retired = domain.transition(_retire_request(engine, fixture))
    assert retired.ok, retired
    reconciled = domain.transition(
        _request(engine, fixture.scenario, fixture.story_id, (), "correction-reconcile")
    )
    assert reconciled.ok, reconciled
    plan_id = _revision(engine, fixture, outcome="accepted", key="correction-initial")
    with Session(engine) as session:
        decision = session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.sprint_plan_artifact_id == plan_id
            )
        ).one()
        sprint_id = decision.activated_sprint_id
    assert isinstance(sprint_id, int)
    assert sprint_id != fixture.sprint_id
    return fixture, plan_id, sprint_id


def _retained_project_receipts(
    engine: Engine, project_id: int
) -> dict[tuple[str, str], str]:
    with Session(engine) as session:
        return {
            (row.request_kind, row.idempotency_key): row.model_dump_json()
            for row in session.exec(select(WorkflowTransitionReceipt)).all()
            if json.loads(row.request_json).get("project_id") == project_id
        }


def test_replacement_correction_preserves_retired_projection(engine: Engine) -> None:
    """Two accepted corrections preserve full retired rows even if IDs are reused."""
    fixture, initial_plan_id, sprint_id = _correction_replacement(engine)
    project_id = fixture.scenario.project_id
    protected = _stored_sprint_evidence(engine, fixture)
    protected_plans = _history(engine, (fixture.plan_id, initial_plan_id))
    receipts = _retained_project_receipts(engine, project_id)
    reads = DurableReadProjectionService(engine=engine)
    for outcome, key in (
        ("feedback", "correction-feedback"),
        ("accepted", "correction-first"),
        ("accepted", "correction-second"),
    ):
        plan_id = _revision(engine, fixture, outcome=outcome, key=key)
        assert _stored_sprint_evidence(engine, fixture) == protected
        assert _history(engine, (fixture.plan_id, initial_plan_id)) == protected_plans
        current_receipts = _retained_project_receipts(engine, project_id)
        assert {
            identity: current_receipts[identity] for identity in receipts
        } == receipts
        if outcome == "feedback":
            continue
        historical = reads.sprint_status(
            project_id=project_id, sprint_id=fixture.sprint_id
        )
        current = reads.sprint_status(project_id=project_id, sprint_id=sprint_id)
        assert historical["ok"] is True, historical
        assert current["ok"] is True, current
        historical_data, current_data = historical["data"], current["data"]
        assert isinstance(historical_data, dict)
        assert isinstance(current_data, dict)
        assert historical_data["effective_status"] == "retired"
        assert current_data["effective_status"] == "planned"
        with Session(engine) as session:
            tasks = session.exec(
                select(Task).where(col(Task.story_id) == fixture.story_id)
            ).all()
            current_tasks = [
                task
                for task in tasks
                if parse_task_metadata(task.metadata_json).sprint_plan_artifact_id
                == plan_id
            ]
            assert len(current_tasks) == 1
            task_id = current_tasks[0].task_id
            assert task_id is not None
            assert task_id != fixture.task_id
            for packet in (
                build_story_packet(
                    session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    story_id=fixture.story_id,
                ),
                build_task_packet(
                    session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    task_id=task_id,
                ),
            ):
                lineage = packet["lineage"]
                assert isinstance(lineage, dict)
                plan = lineage["sprint_plan"]
                assert isinstance(plan, dict)
                assert plan["sprint_plan_artifact_id"] == plan_id


@pytest.mark.parametrize(
    "mutation",
    [
        "invalid-prior-ownership",
        "malformed",
        "missing",
        "duplicate",
        "execution",
        "started",
    ],
)
def test_replacement_correction_ownership_failure_rolls_back(  # noqa: PLR0915
    engine: Engine, mutation: str
) -> None:
    """Invalid current ownership or execution cannot cause a partial replacement."""
    fixture, initial_plan_id, sprint_id = _correction_replacement(engine)
    project_id = fixture.scenario.project_id
    domain = _domain(engine)
    if mutation == "started":
        with Session(engine) as session:
            snapshot = WorkflowFactRepository(session).load(project_id)
        spec = accepted_current_spec(snapshot)
        assert spec is not None
        story = next(
            item for item in snapshot.stories if item.story_id == fixture.story_id
        )
        request = RecordSprintPlan(
            **planning_guards(domain.position(project_id), "planning.sprint.plan"),
            idempotency_key="correction-after-start",
            team_name="Prerequisite replacement preservation team",
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
        started = domain.transition(
            StartSprint(
                **planning_guards(domain.position(project_id), "planning.sprint.start"),
                idempotency_key="correction-guard-real-start",
            )
        )
        assert started.ok, started
    else:
        pending_id = _revision(
            engine, fixture, outcome="pending", key="correction-invalid"
        )
        with Session(engine) as session:
            pending = session.get_one(SprintPlanArtifact, pending_id)
            fingerprint = pending.plan_fingerprint
        request = DecideSprintPlan(
            **planning_guards(domain.position(project_id), "planning.sprint.review"),
            idempotency_key="correction-invalid-review",
            sprint_plan_artifact_id=pending_id,
            plan_fingerprint=fingerprint,
            decision="accepted",
            rationale="Review this exact proposed correction.",
        )
        with Session(engine) as session:
            task = next(
                row
                for row in session.exec(
                    select(Task).where(col(Task.story_id) == fixture.story_id)
                ).all()
                if parse_task_metadata(row.metadata_json).sprint_plan_artifact_id
                == initial_plan_id
            )
            if mutation == "missing":
                session.delete(task)
            elif mutation == "duplicate":
                session.add(
                    Task(
                        story_id=task.story_id,
                        description=task.description,
                        metadata_json=task.metadata_json,
                    )
                )
            elif mutation == "execution":
                assert task.task_id is not None
                session.add(
                    TaskExecutionLog(
                        task_id=task.task_id,
                        sprint_id=sprint_id,
                        new_status=task.status,
                        notes="Execution evidence protects this projection.",
                    )
                )
            else:
                metadata = json.loads(task.metadata_json)
                if mutation == "invalid-prior-ownership":
                    metadata["sprint_plan_artifact_id"] = fixture.plan_id
                    task.metadata_json = canonical_json(metadata)
                else:
                    task.metadata_json = "{}"
                session.add(task)
            session.commit()
    before = _project_business_rows(engine, project_id)
    protected = _stored_sprint_evidence(engine, fixture)
    if mutation in {"invalid-prior-ownership", "malformed"}:
        receipts = _retained_project_receipts(engine, project_id)
        for _ in range(2):
            with pytest.raises(WorkflowFactLoadError, match="metadata"):
                domain.transition(request)
            assert _project_business_rows(engine, project_id) == before
            assert _stored_sprint_evidence(engine, fixture) == protected
            assert _retained_project_receipts(engine, project_id) == receipts
        return
    result = domain.transition(request)
    assert not result.ok, result
    assert _project_business_rows(engine, project_id) == before
    assert _stored_sprint_evidence(engine, fixture) == protected
    receipt = _rejection_receipt(engine, request)
    replay = domain.transition(request)
    assert not replay.ok, replay
    assert replay.replayed, replay
    assert _rejection_receipt(engine, request) == receipt
    assert _project_business_rows(engine, project_id) == before


def _retired_after_completed_sprints(
    engine: Engine,
) -> tuple[CompletedRetrySource, int]:
    """Accept, stale and retire a third plan after two genuinely closed Sprints."""
    source = seed_completed_retry_source(engine)
    domain, plan_id = record_pending_successor_plan(engine, source)
    with Session(engine) as session:
        fingerprint = session.get_one(SprintPlanArtifact, plan_id).plan_fingerprint
    accepted = domain.transition(
        DecideSprintPlan(
            **planning_guards(
                domain.position(source.project_id), "planning.sprint.review"
            ),
            idempotency_key="scope-third-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept the exact later Sprint before its scope changes.",
        )
    )
    assert accepted.ok, accepted
    retired_id = accepted.output["activated_sprint_id"]
    assert isinstance(retired_id, int)
    with Session(engine) as session:
        story = session.get_one(UserStory, source.candidate_story_id)
        assert story.story_points is not None
        story.story_points += 1
        session.add(story)
        session.commit()
        decision = session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.sprint_plan_artifact_id == plan_id
            )
        ).one()
        decision_id = decision.sprint_plan_artifact_decision_id
    assert decision_id is not None
    retired = domain.transition(
        RetireSprintPlan(
            **planning_guards(
                domain.position(source.project_id), "planning.sprint.retire"
            ),
            idempotency_key="scope-third-retire",
            sprint_plan_artifact_id=plan_id,
            sprint_plan_artifact_decision_id=decision_id,
            sprint_id=retired_id,
            plan_fingerprint=fingerprint,
            rationale="Retire the stale unstarted scope and preserve earlier history.",
        )
    )
    assert retired.ok, retired
    return source, retired_id


def _live_retry_snapshot_before_retired_history(
    engine: Engine, *, active: bool
) -> tuple[WorkflowFactSnapshot, int, int]:
    """Exercise selection with valid retry facts, without public retry admission."""
    source, retired_id = _retired_after_completed_sprints(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.project_id)
    contract = execution_contract(snapshot, source.source_sprint_id)
    created_at = datetime(2026, 9, 9, 9, 30, tzinfo=UTC)
    retry = SprintRetryFact(
        retry_attempt_id=17,
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        ordinal=2,
        predecessor_retry_attempt_id=None,
        contract_fingerprint=contract.fingerprint,
        created_by="owner@example.com",
        rationale="Verify earlier retry priority over retired history.",
        creation_fingerprint="sha256:mixed-history-retry",
        creation_receipt_key="scope-mixed-history",
        created_at=created_at,
        status="planned",
        story_statuses=tuple((item.story_id, "To Do") for item in contract.stories),
        task_statuses=tuple((item.task_id, "To Do") for item in contract.tasks),
    )
    if active:
        started_at = created_at + timedelta(minutes=1)
        retry = retry.model_copy(
            update={
                "status": "active",
                "started_at": started_at,
                "start": SprintRetryStartFact(
                    start_id=717,
                    retry_attempt_id=retry.retry_attempt_id,
                    contract_fingerprint=contract.fingerprint,
                    decision_fingerprint="sha256:mixed-history-start",
                    started_by="owner@example.com",
                    started_at=started_at,
                ),
            }
        )
    return (
        snapshot.model_copy(update={"sprint_retries": (retry,)}),
        retired_id,
        retry.retry_attempt_id,
    )


def test_completed_replacement_preserves_retired_task_and_evidence(
    engine: Engine,
) -> None:
    """Real closure must leave the retired Task unfinished and its evidence intact."""
    project_id, retired_id, replacement_id = _retired_replacement(
        engine, replacement_state="completed"
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    retired_tasks = tuple(
        item for item in snapshot.tasks if item.sprint_id == retired_id
    )
    assert len(retired_tasks) == 1
    assert retired_tasks[0].status == "To Do"
    assert not any(item.sprint_id == retired_id for item in snapshot.task_completions)
    assert not any(item.sprint_id == retired_id for item in snapshot.story_completions)
    assert not any(item.sprint_id == retired_id for item in snapshot.sprint_starts)
    assert any(item.sprint_id == replacement_id for item in snapshot.task_completions)
    assert any(item.sprint_id == replacement_id for item in snapshot.story_completions)
    assert any(item.sprint_id == replacement_id for item in snapshot.sprint_reviews)
    assert any(item.sprint_id == replacement_id for item in snapshot.sprint_closures)
    assert any(item.sprint_id == replacement_id for item in snapshot.post_sprint_triage)


@pytest.mark.parametrize("replacement_state", ["planned", "active", "completed"])
def test_retired_history_does_not_block_current_execution_scope(
    engine: Engine, replacement_state: Literal["planned", "active", "completed"]
) -> None:
    """Retired originals cannot compete with a replacement's execution authority."""
    project_id, retired_id, replacement_id = _retired_replacement(
        engine, replacement_state=replacement_state
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    scope = current_execution_scope(snapshot)

    if replacement_state == "planned":
        assert scope is None
    else:
        assert scope is not None
        assert scope.sprint_id == replacement_id
        assert scope.status == replacement_state
    assert (
        next(item for item in snapshot.sprints if item.sprint_id == retired_id).status
        == "planned"
    )
    assert any(
        item.status == "retired" and item.activated_sprint_id == retired_id
        for item in snapshot.planning_artifacts
    )


@pytest.mark.parametrize("outcome", ["pending", "feedback", "rejected"])
def test_retired_stream_successor_does_not_select_terminal_scope(
    engine: Engine, outcome: Literal["pending", "feedback", "rejected"]
) -> None:
    """Unaccepted historical revisions cannot hide a completed replacement stream."""
    project_id, _retired_id, replacement_id = _retired_replacement(
        engine, replacement_state="completed", successor_outcome=outcome
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    scope = current_execution_scope(snapshot)

    assert scope is not None
    assert scope.sprint_id == replacement_id
    expected_status = "pending_review" if outcome == "pending" else outcome
    assert any(item.status == expected_status for item in snapshot.planning_artifacts)


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
