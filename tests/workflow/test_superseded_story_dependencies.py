# tests/workflow/test_superseded_story_dependencies.py
"""Provider-free lifecycle regressions for replaced Story prerequisites."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, col, create_engine, select
from sqlmodel.sql.expression import Select

import services.story_dependencies as writer
from cli.workflow_commands import workflow_next
from models.core import Sprint, Task, UserStory, UserStoryDependency
from models.db import (
    CURRENT_BUSINESS_SCHEMA_MANIFEST,
    _assert_current_business_schema,
    _sqlmodel_business_schema_manifest,
    ensure_business_db_ready,
)
from models.enums import SprintStatus, StoryStatus, TaskStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import (
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintStart,
    StoryArtifact,
    StoryArtifactDecision,
    StoryDependencyReview,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services import story_dependency_lifecycle
from services.agent_workbench import sprint_phase, story_phase
from services.agent_workbench.sprint_phase import (
    SprintStartInput,
    start_sprint_in_session,
)
from services.agent_workbench.story_phase import (
    RecordStoryDecisionInput,
    RecordStoryDraftInput,
    record_story_decision_in_session,
    record_story_draft_in_session,
)
from services.application import (
    AgileForgeApplication,
    PlanningActionSelectionService,
    StoryDependenciesApplyRequest,
    StoryDependencyEdgeRequest,
)
from services.contracts.sprint import SprintPlannerOutput
from services.read_projections import DurableReadProjectionService
from services.specs.story_validation_service import require_story_ready_for_sprint
from services.sprint_retry import build_sprint_retry_preview
from services.story_dependencies import (
    ApplyStoryDependenciesInput,
    SelectedScopeStory,
    StoryDependencyGraphError,
    apply_story_dependencies_in_session,
    selected_scope_fingerprint,
)
from services.story_dependency_lifecycle import (
    StoryDependencyInvalidationAnchor,
    StoryDependencyLifecycleIntegrityError,
    adopt_story_dependency_invalidations_in_session,
    infer_unrecorded_story_dependency_invalidations_in_session,
    invalidate_story_dependents_for_replacement_in_session,
    load_story_dependency_invalidations_in_session,
    selected_scope_dependency_lifecycle_fingerprint,
    validate_reviewed_dependency_lifecycle_in_session,
)
from services.story_sprint_selection import (
    StorySprintSelectionIntent,
    StorySprintSelectionRequest,
    apply_story_sprint_selection_with_receipt_in_session,
    story_sprint_selection_fact_in_session,
)
from tests.test_story_dependencies import _story_set
from tests.workflow.execution_fixtures import (
    _accept_and_start_sprint,
    unbind_synthetic_execution_repository,
)
from tests.workflow.execution_retry_support import (
    _close_execution_sprint,
    _complete_task,
    _triage_execution_sprint,
)
from tests.workflow.execution_retry_support import _domain as execution_domain
from tests.workflow.execution_retry_support import _guards as execution_guards
from tests.workflow.planning_fixtures import (
    EVALUATED_AT,
    apply_current_dependencies,
    planning_decision,
    planning_guards,
    record_and_accept_roadmap,
    record_and_accept_story,
    seed_accepted_backlog,
    select_for_sprint,
)
from tests.workflow.test_planning_transitions import (
    _domain,
    _record_sprint_plan_draft,
    _sprint_plan,
)
from workflow.clock import FixedClock
from workflow.contracts import NodeCategory, WorkflowErrorCode
from workflow.definitions.execution import execution_graph
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    story_dependency_source_fingerprint,
)
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.execution_integrity import (
    TaskEvidencePayload,
    execution_contract,
    task_evidence_fingerprint,
)
from workflow.facts import StoryDependencyReviewEdgeFact
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.graph import RuleCategory
from workflow.planning_integrity import (
    current_dependency_closure,
    selected_dependency_active_closure,
    superseded_dependency_edges,
)
from workflow.requests import (
    ApplyStoryDependencies,
    CloseSprint,
    CloseStory,
    DecideSprintPlan,
    RecordSprintPlan,
    RetrySprint,
    ReviewSprint,
    StartSprint,
    StartSprintRetry,
)
from workflow.requests.planning import ReviewedDependencyEdge
from workflow.sprint_retry_eligibility import (
    evaluate_sprint_retry_eligibility,
    source_sprint_contract_is_current,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.facts import StoryFact, WorkflowFactSnapshot


@dataclass(frozen=True)
class _Scenario:
    project_id: int
    artifact_ids: tuple[int, ...]
    story_ids: tuple[int, ...]


def _seed(engine: Engine, count: int = 3) -> _Scenario:
    requirements = tuple(f"Lifecycle requirement {index}" for index in range(count))
    project_id = seed_accepted_backlog(engine, requirements=requirements)
    domain = _domain(engine)
    record_and_accept_roadmap(domain, project_id, requirements=requirements)
    pairs = tuple(
        record_and_accept_story(
            engine,
            domain,
            project_id,
            requirement=requirement,
            spec_item_id=f"REQ.planning-{index + 1}",
            backlog_item_id=f"PBI-{index + 1:06d}",
            idempotency_suffix=f"-lifecycle-{index}",
        )
        for index, requirement in enumerate(requirements)
    )
    return _Scenario(project_id, tuple(x[0] for x in pairs), tuple(x[1] for x in pairs))


def _edge(
    engine: Engine,
    scenario: _Scenario,
    dependent: int,
    prerequisite: int,
    *,
    status: str = "active",
) -> int:
    with Session(engine) as session:
        row = UserStoryDependency(
            project_id=scenario.project_id,
            dependent_story_id=dependent,
            prerequisite_story_id=prerequisite,
            status=status,
            source="manual_review",
            confidence="reviewed",
            reason="Exact fixture prerequisite.",
        )
        session.add(row)
        session.commit()
        assert row.dependency_id is not None
        return row.dependency_id


def _snapshot(engine: Engine, scenario: _Scenario) -> WorkflowFactSnapshot:
    with Session(engine) as session:
        return WorkflowFactRepository(session).load(scenario.project_id)


def _story(snapshot: WorkflowFactSnapshot, story_id: int) -> StoryFact:
    return next(story for story in snapshot.stories if story.story_id == story_id)


def _review(engine: Engine, scenario: _Scenario, selected: int) -> None:
    select_for_sprint(engine, selected)
    apply_current_dependencies(
        engine, _domain(engine), scenario.project_id, idempotency_key="lifecycle-review"
    )


def _draft(
    session: Session,
    source_id: int,
    suffix: str = "replacement",
    *,
    item_count: int | None = None,
) -> StoryArtifact:
    source = session.get_one(StoryArtifact, source_id)
    content = json.loads(source.canonical_content_json)
    for envelope in content["story_items"]:
        envelope["item"]["story_title"] += f" ({suffix})"
        envelope["item_fingerprint"] = canonical_hash(envelope["item"])
    if item_count is not None:
        content["story_items"] = [
            deepcopy(content["story_items"][0]) for _ in range(item_count)
        ]
        for ordinal, envelope in enumerate(content["story_items"], start=1):
            envelope["item"]["story_item_id"] = f"US-{ordinal:04d}"
            envelope["item"]["story_title"] += f" item {ordinal}"
            envelope["item_fingerprint"] = canonical_hash(envelope["item"])
    return record_story_draft_in_session(
        session,
        inputs=RecordStoryDraftInput(
            project_id=source.project_id,
            source_backlog_artifact_id=source.source_backlog_artifact_id,
            source_backlog_artifact_fingerprint=source.source_backlog_artifact_fingerprint,
            backlog_item_id=source.backlog_item_id,
            roadmap_artifact_id=source.roadmap_artifact_id,
            roadmap_artifact_fingerprint=source.roadmap_artifact_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_story_artifact_id=source_id,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT + timedelta(minutes=1),
        ),
    )


def _decide(
    session: Session, artifact: StoryArtifact, decision: str = "accepted"
) -> tuple[int, ...]:
    return record_story_decision_in_session(
        session,
        inputs=RecordStoryDecisionInput(
            artifact=artifact,
            decision=decision,
            rationale="Reviewed exact correction.",
            reviewer="operator@example.com",
            idempotency_key=f"lifecycle-{decision}-{artifact.story_artifact_id}",
            decided_at=EVALUATED_AT + timedelta(minutes=2),
        ),
    ).activated_story_ids


def _replace(engine: Engine, artifact_id: int) -> tuple[int, tuple[int, ...]]:
    with Session(engine) as session:
        replacement = _draft(session, artifact_id)
        replacement_ids = _decide(session, replacement)
        session.commit()
        assert replacement.story_artifact_id is not None
        return replacement.story_artifact_id, replacement_ids


def _events(engine: Engine) -> tuple[WorkflowEvent, ...]:
    with Session(engine) as session:
        return tuple(
            event
            for event in session.exec(select(WorkflowEvent)).all()
            if event.event_type.value == "story_dependency_stale"
        )


def _inspect(engine: Engine, scenario: _Scenario) -> dict:
    result = DurableReadProjectionService(engine=engine).story_dependencies_inspect(
        project_id=scenario.project_id
    )
    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    return data


def _request(  # noqa: PLR0913
    engine: Engine,
    scenario: _Scenario,
    selected: int,
    prerequisites: tuple[int, ...],
    key: str,
    *,
    source: str | None = None,
) -> ApplyStoryDependencies:
    snapshot = _snapshot(engine, scenario)
    return ApplyStoryDependencies(
        **planning_guards(
            _domain(engine).position(scenario.project_id), "planning.story_dependencies"
        ),
        idempotency_key=key,
        selected_story_ids=(selected,),
        reviewed_edges=tuple(
            ReviewedDependencyEdge(
                dependent_story_id=selected,
                prerequisite_story_id=prerequisite,
                reason="Human confirms this exact prerequisite.",
            )
            for prerequisite in prerequisites
        ),
        source_fingerprint=source
        or story_dependency_source_fingerprint(snapshot.stories),
    )


def _business_state(engine: Engine) -> tuple:
    with Session(engine) as session:
        return (
            *(
                tuple(
                    row.model_dump(mode="json")
                    for row in session.exec(select(model)).all()
                )
                for model in (UserStoryDependency, StoryDependencyReview)
            ),
            tuple(event.model_dump(mode="json") for event in _events(engine)),
        )


def _legacy_replace(
    engine: Engine, artifact_id: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, tuple[int, ...]]:
    """Construct pre-marker acceptance by suppressing only the new event writer."""
    with monkeypatch.context() as context:
        context.setattr(
            story_phase,
            "invalidate_story_dependents_for_replacement_in_session",
            lambda *_args, **_kwargs: None,
        )
        return _replace(engine, artifact_id)


def test_reconciled_obsolete_edges_remain_audit_only(engine: Engine) -> None:
    """Human exclusion removes diagnostics while retaining old edge/review facts."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    edge_id = _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    retained_reviews = _business_state(engine)[1]
    _replace(engine, scenario.artifact_ids[0])
    obsolete = _inspect(engine, scenario)
    assert {issue["dependency_id"] for issue in obsolete["issues"]} == {edge_id}
    request = _request(engine, scenario, b, (), "inspection-exclude-obsolete")
    assert _domain(engine).transition(request).ok

    before_inspect = _business_state(engine)
    reconciled = _inspect(engine, scenario)
    assert reconciled["issues"] == []
    edge = next(row for row in reconciled["edges"] if row["dependency_id"] == edge_id)
    assert (
        edge["dependent_story_id"],
        edge["prerequisite_story_id"],
        edge["status"],
    ) == (
        b,
        a,
        "rejected",
    )
    snapshot = _snapshot(engine, scenario)
    assert (
        snapshot.story_dependency_reviews[-1].source_fingerprint
        == (reconciled["selected_scope_fingerprint"])
    )
    assert {story.selected_scope_fingerprint for story in snapshot.stories} == {
        reconciled["selected_scope_fingerprint"]
    }
    reviews_by_id = {
        row["story_dependency_review_id"]: row for row in _business_state(engine)[1]
    }
    for old in retained_reviews:
        assert reviews_by_id[old["story_dependency_review_id"]] == old
    assert _business_state(engine) == before_inspect


def test_asa_superseded_dependents_remain_history_after_apply(engine: Engine) -> None:
    """Accepted corrections and exact current review leave superseded rows as audit."""
    scenario = _seed(engine)
    a, b, live = scenario.story_ids
    historical_edge_id = _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    original_scope_fingerprint = _story(
        _snapshot(engine, scenario), b
    ).selected_scope_fingerprint
    assert original_scope_fingerprint is not None
    with Session(engine) as session:
        old_review = session.exec(
            select(StoryDependencyReview).where(
                StoryDependencyReview.project_id == scenario.project_id,
                StoryDependencyReview.source_fingerprint == original_scope_fingerprint,
            )
        ).one()
        old_review_id = old_review.story_dependency_review_id
        old_review_bytes = old_review.model_dump(mode="json")
        old_edge = session.get_one(UserStoryDependency, historical_edge_id)
        old_edge_bytes = old_edge.model_dump(mode="json")
    _, (current_a,) = _replace(engine, scenario.artifact_ids[0])
    _, (current_b,) = _replace(engine, scenario.artifact_ids[1])
    live_edge_id = _edge(engine, scenario, live, b)
    with Session(engine) as session:
        completed = session.get_one(UserStory, current_a)
        completed.status = StoryStatus.DONE
        session.add(completed)
        session.commit()
    select_for_sprint(engine, current_b)
    select_for_sprint(engine, live)
    before = _inspect(engine, scenario)
    assert {issue["dependency_id"] for issue in before["issues"]} == {live_edge_id}
    assert {row["story_id"] for row in before["stories"]}.isdisjoint({a, b})
    assert set(before["selected_story_ids"]) == {current_b, live}
    assert all(row["dependent_story_id"] != current_b for row in before["edges"])
    request = ApplyStoryDependencies(
        **planning_guards(
            _domain(engine).position(scenario.project_id), "planning.story_dependencies"
        ),
        idempotency_key="asa-current-exact-review",
        selected_story_ids=tuple(sorted((current_b, live))),
        reviewed_edges=(
            ReviewedDependencyEdge(
                dependent_story_id=current_b,
                prerequisite_story_id=current_a,
                reason="Human confirms only the exact current prerequisite.",
            ),
        ),
        source_fingerprint=before["selected_scope_fingerprint"],
    )
    assert _domain(engine).transition(request).ok

    after = _inspect(engine, scenario)
    assert after["issues"] == []
    retained_edges = {row["dependency_id"]: row for row in after["edges"]}
    assert retained_edges[historical_edge_id]["status"] == "active"
    assert retained_edges[historical_edge_id]["source"] == "manual_review"
    assert retained_edges[live_edge_id]["status"] == "rejected"
    assert any(
        (row["dependent_story_id"], row["prerequisite_story_id"], row["status"])
        == (current_b, current_a, "active")
        for row in after["edges"]
    )
    with Session(engine) as session:
        assert (
            session.get_one(StoryDependencyReview, old_review_id).model_dump(
                mode="json"
            )
            == old_review_bytes
        )
        assert (
            session.get_one(UserStoryDependency, historical_edge_id).model_dump(
                mode="json"
            )
            == old_edge_bytes
        )
    current_review = _snapshot(engine, scenario).story_dependency_reviews[-1]
    assert current_review.source_fingerprint == after["selected_scope_fingerprint"]
    assert current_review.selected_story_ids == tuple(sorted((current_b, live)))


@pytest.mark.parametrize("terminal_status", [StoryStatus.DONE, StoryStatus.ACCEPTED])
def test_story_dependencies_inspect_keeps_terminal_external_history_audit_only(
    engine: Engine, terminal_status: StoryStatus
) -> None:
    """Terminal X is neither an inspection root nor a bridge to obsolete A."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    history_edge_id = _edge(engine, scenario, x, a)
    incoming_edge_id = _edge(engine, scenario, b, x)
    with Session(engine) as session:
        terminal = session.get_one(UserStory, x)
        terminal.status = terminal_status
        session.add(terminal)
        session.commit()
    _review(engine, scenario, b)
    token_before = _inspect(engine, scenario)["selected_scope_fingerprint"]
    _replace(engine, scenario.artifact_ids[0])
    before_inspect = _business_state(engine)

    inspected = _inspect(engine, scenario)

    assert inspected["issues"] == []
    edges_by_id = {row["dependency_id"]: row for row in inspected["edges"]}
    assert edges_by_id[history_edge_id]["prerequisite_story_id"] == a
    assert edges_by_id[incoming_edge_id]["prerequisite_story_id"] == x
    assert inspected["selected_scope_fingerprint"] == token_before
    assert _story(_snapshot(engine, scenario), b).dependency_safe
    assert _business_state(engine) == before_inspect


def test_story_dependencies_inspect_stops_unfinished_completed_member_history(
    engine: Engine,
) -> None:
    """Membership stops X's history while its incoming edge remains incomplete."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    with Session(engine) as session:
        prerequisite = session.get_one(UserStory, a)
        prerequisite.status = StoryStatus.DONE
        session.add(prerequisite)
        session.commit()
    history_edge_id = _edge(engine, scenario, x, a)
    _complete_fixture_story_sprint(engine, scenario, x)
    incoming_edge_id = _edge(engine, scenario, b, x)
    select_for_sprint(engine, b)
    with Session(engine) as session:
        incomplete = session.get_one(UserStory, x)
        incomplete.status = StoryStatus.TO_DO
        session.add(incomplete)
        session.commit()
    _replace(engine, scenario.artifact_ids[0])
    before_inspect = _business_state(engine)

    inspected = _inspect(engine, scenario)

    assert inspected["issues"] == []
    edges_by_id = {row["dependency_id"]: row for row in inspected["edges"]}
    assert edges_by_id[history_edge_id]["prerequisite_story_id"] == a
    assert edges_by_id[incoming_edge_id]["prerequisite_story_id"] == x
    assert _story(_snapshot(engine, scenario), b).readiness_blockers == (
        f"PREREQUISITE_STORY_{x}_INCOMPLETE",
    )
    assert _business_state(engine) == before_inspect


@pytest.mark.parametrize("structurally_validated", [False, True])
@pytest.mark.parametrize("edge_status", ["active", "proposed"])
def test_story_dependencies_inspect_reports_unselected_live_obsolete_authority(
    engine: Engine, structurally_validated: bool, edge_status: str
) -> None:
    """Unselected and awaiting-validation live dependents still need human review."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    edge_id = _edge(engine, scenario, b, a, status=edge_status)
    if not structurally_validated:
        with Session(engine) as session:
            dependent = session.get_one(UserStory, b)
            dependent.validation_evidence = None
            session.add(dependent)
            session.commit()
    assert _story(_snapshot(engine, scenario), b).validation_status == (
        "validated" if structurally_validated else "unvalidated"
    )
    before_token = _inspect(engine, scenario)["selected_scope_fingerprint"]
    _replace(engine, scenario.artifact_ids[0])
    before_inspect = _business_state(engine)

    inspected = _inspect(engine, scenario)

    assert inspected["selected_story_ids"] == []
    assert inspected["selected_scope_fingerprint"] == before_token
    assert [
        (
            issue["dependency_id"],
            issue["dependent_story_id"],
            issue["prerequisite_story_id"],
            issue["edge_status"],
        )
        for issue in inspected["issues"]
    ] == [(edge_id, b, a, edge_status)]
    assert _business_state(engine) == before_inspect


@pytest.mark.parametrize("choice", ["exclude", "replacement"])
def test_dependency_reconciliation_uses_current_inspected_scope(  # noqa: PLR0915
    engine: Engine, choice: str
) -> None:
    """Public Apply rejects stale inspection, then applies/replays explicit intent."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    edge_id = _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    observed = _inspect(engine, scenario)
    retained_reviews = _business_state(engine)[1]
    _, (replacement,) = _replace(engine, scenario.artifact_ids[0])
    with Session(engine) as session:
        current = session.get_one(UserStory, replacement)
        current.status = StoryStatus.DONE
        session.add(current)
        session.commit()
    application = AgileForgeApplication(
        workflow_domain=_domain(engine),
        read_projection=DurableReadProjectionService(engine=engine),
        planning_action_selection=PlanningActionSelectionService(engine=engine),
    )
    reviewed_edges = (
        ()
        if choice == "exclude"
        else (
            StoryDependencyEdgeRequest(
                dependent_story_id=b,
                prerequisite_story_id=replacement,
                reason="Explicit human confirmation of the exact replacement.",
            ),
        )
    )
    stale_request = StoryDependenciesApplyRequest(
        project_id=scenario.project_id,
        idempotency_key=f"application-stale-{choice}",
        actor="operator@example.com",
        selected_story_ids=tuple(observed["selected_story_ids"]),
        selected_scope_fingerprint=observed["selected_scope_fingerprint"],
        reviewed_edges=reviewed_edges,
    )
    before_stale = _business_state(engine)
    with Session(engine) as session:
        receipts_before = tuple(
            row.model_dump(mode="json")
            for row in session.exec(select(WorkflowTransitionReceipt)).all()
        )
    stale = application.apply_story_dependencies(stale_request)
    assert not stale.ok
    assert stale.error is not None
    assert stale.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert not stale.replayed
    assert _business_state(engine) == before_stale
    with Session(engine) as session:
        assert (
            tuple(
                row.model_dump(mode="json")
                for row in session.exec(select(WorkflowTransitionReceipt)).all()
            )
            == receipts_before
        )
    current = _inspect(engine, scenario)
    assert (
        current["selected_scope_fingerprint"] != observed["selected_scope_fingerprint"]
    )
    assert {issue["dependency_id"] for issue in current["issues"]} == {edge_id}
    request = stale_request.model_copy(
        update={
            "idempotency_key": f"application-current-{choice}",
            "selected_scope_fingerprint": current["selected_scope_fingerprint"],
        }
    )
    applied = application.apply_story_dependencies(request)
    assert applied.ok
    assert not applied.replayed
    reconciled = _inspect(engine, scenario)
    assert reconciled["issues"] == []
    assert (
        reconciled["selected_scope_fingerprint"]
        == current["selected_scope_fingerprint"]
    )
    retained_edge = next(
        row for row in reconciled["edges"] if row["dependency_id"] == edge_id
    )
    assert retained_edge["status"] == "rejected"
    assert {
        (row["dependent_story_id"], row["prerequisite_story_id"])
        for row in reconciled["edges"]
        if row["status"] == "active"
    } == (set() if choice == "exclude" else {(b, replacement)})
    snapshot = _snapshot(engine, scenario)
    assert _story(snapshot, b).dependency_safe
    assert (
        snapshot.story_dependency_reviews[-1].source_fingerprint
        == (reconciled["selected_scope_fingerprint"])
    )
    reviews_by_id = {
        row["story_dependency_review_id"]: row for row in _business_state(engine)[1]
    }
    for old in retained_reviews:
        assert reviews_by_id[old["story_dependency_review_id"]] == old
    before_replay = _business_state(engine)
    with Session(engine) as session:
        receipt_before = (
            session.exec(
                select(WorkflowTransitionReceipt).where(
                    WorkflowTransitionReceipt.idempotency_key
                    == request.idempotency_key,
                    WorkflowTransitionReceipt.request_kind
                    == "apply_story_dependencies",
                )
            )
            .one()
            .model_dump(mode="json")
        )
    replay = application.apply_story_dependencies(request)
    assert replay.ok, replay.error
    assert replay.replayed
    assert replay == applied.model_copy(update={"replayed": True})
    assert _business_state(engine) == before_replay
    with Session(engine) as session:
        receipt_after = (
            session.exec(
                select(WorkflowTransitionReceipt).where(
                    WorkflowTransitionReceipt.idempotency_key
                    == request.idempotency_key,
                    WorkflowTransitionReceipt.request_kind
                    == "apply_story_dependencies",
                )
            )
            .one()
            .model_dump(mode="json")
        )
    assert receipt_after == receipt_before


def _complete_fixture_story_sprint(
    engine: Engine, scenario: _Scenario, story_id: int
) -> int:
    """Retain a real accepted/start/close/triage history for boundary fixtures."""
    domain = _domain(engine)
    binding = _record_sprint_plan_draft(
        engine,
        domain,
        scenario.project_id,
        story_id,
        team_name="Completed membership boundary team",
        idempotency_key="completed-membership-plan",
    )
    sprint_id = _accept_and_start_sprint(
        domain,
        project_id=scenario.project_id,
        plan_binding=binding,
        idempotency_suffix="-completed-membership",
        engine=engine,
    )
    with Session(engine) as session:
        task_id = (
            session.exec(select(Task).where(Task.story_id == story_id)).one().task_id
        )
        assert task_id is not None
    execution = execution_domain(engine)
    _close_execution_sprint(
        execution,
        project_id=scenario.project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    _triage_execution_sprint(
        execution, project_id=scenario.project_id, sprint_id=sprint_id
    )
    return sprint_id


def _completed_history(snapshot: WorkflowFactSnapshot) -> tuple:
    return (
        snapshot.sprints,
        snapshot.sprint_starts,
        snapshot.sprint_reviews,
        snapshot.sprint_closures,
        snapshot.story_completions,
        snapshot.task_completions,
        snapshot.post_sprint_triage,
        tuple(
            artifact
            for artifact in snapshot.planning_artifacts
            if artifact.artifact_type == "sprint_plan"
        ),
    )


@pytest.mark.parametrize("already_reviewed", [False, True])
def test_nonterminal_completed_member_history_preserves_current_review_boundary(  # noqa: PLR0915
    engine: Engine,
    already_reviewed: bool,
) -> None:
    """Completed membership stops outgoing history without satisfying unfinished X."""
    scenario = _seed(engine, 3)
    a, b, x = scenario.story_ids
    with Session(engine) as session:
        prerequisite = session.get_one(UserStory, a)
        prerequisite.status = StoryStatus.DONE
        session.add(prerequisite)
        session.commit()
    _edge(engine, scenario, x, a)
    sprint_id = _complete_fixture_story_sprint(engine, scenario, x)
    b_edge_id = _edge(engine, scenario, b, x)
    select_for_sprint(engine, b)
    if already_reviewed:
        apply_current_dependencies(
            engine,
            _domain(engine),
            scenario.project_id,
            idempotency_key="review-before-status-drift",
        )
    before = _snapshot(engine, scenario)
    source = _story(before, b).selected_scope_fingerprint
    history = _completed_history(before)
    assert sprint_id in _story(before, x).sprint_ids
    assert (
        next(
            sprint for sprint in before.sprints if sprint.sprint_id == sprint_id
        ).status
        == "completed"
    )
    with Session(engine) as session:
        # Explicit loadable legacy/current-status drift after a real completed
        # Sprint. This is not a public partial-close/reopen lifecycle claim.
        incomplete = session.get_one(UserStory, x)
        incomplete.status = StoryStatus.TO_DO
        session.add(incomplete)
        session.commit()
    _replace(engine, scenario.artifact_ids[0])
    with Session(engine) as session:
        lifecycle_error = None
        try:
            validate_reviewed_dependency_lifecycle_in_session(
                session,
                project_id=scenario.project_id,
                selected_story_ids=(b,),
                reviewed_edges=(
                    StoryDependencyReviewEdgeFact(
                        dependent_story_id=b,
                        prerequisite_story_id=x,
                        reason="Retain the exact completed-member prerequisite.",
                    ),
                ),
            )
        except ValueError as exc:
            lifecycle_error = exc
        assert lifecycle_error is None, str(lifecycle_error)
    after = _snapshot(engine, scenario)
    assert _story(after, b).selected_scope_fingerprint == source
    assert _events(engine) == ()
    assert _completed_history(after) == history
    assert _story(after, x).sprint_ids == _story(before, x).sprint_ids
    assert after.story_dependency_reviews == before.story_dependency_reviews
    decision = planning_decision(
        _domain(engine).position(scenario.project_id), "planning.story_dependencies"
    )
    assert decision.category is (
        NodeCategory.BLOCKED if already_reviewed else NodeCategory.AVAILABLE
    )
    assert decision.reason_code == (
        "STORY_DEPENDENCY_EXTERNAL_INCOMPLETE"
        if already_reviewed
        else "STORY_DEPENDENCY_REVIEW_REQUIRED"
    )
    assert _story(after, b).readiness_blockers == (
        f"PREREQUISITE_STORY_{x}_INCOMPLETE",
    )
    assert _story(after, x).readiness_blockers == ()
    completed_ids = frozenset({x})
    assert (
        superseded_dependency_edges(
            stories=after.stories,
            dependencies=after.story_dependencies,
            root_story_ids=(b, x),
            completed_story_ids=completed_ids,
        )
        == ()
    )
    assert (
        current_dependency_closure(
            stories=after.stories,
            dependencies=after.story_dependencies,
            root_story_ids=(x,),
            completed_story_ids=completed_ids,
        )
        == ()
    )
    assert [
        edge.dependency_id
        for edge in current_dependency_closure(
            stories=after.stories,
            dependencies=after.story_dependencies,
            root_story_ids=(b,),
            completed_story_ids=completed_ids,
        )
    ] == [b_edge_id]
    with Session(engine) as session:
        assert (
            infer_unrecorded_story_dependency_invalidations_in_session(
                session,
                project_id=scenario.project_id,
                root_story_ids=(b, x),
            )
            == ()
        )
        validate_reviewed_dependency_lifecycle_in_session(
            session,
            project_id=scenario.project_id,
            selected_story_ids=(b,),
            reviewed_edges=(
                StoryDependencyReviewEdgeFact(
                    dependent_story_id=b,
                    prerequisite_story_id=x,
                    reason="Retain the exact completed-member prerequisite.",
                ),
            ),
        )
    if not already_reviewed:
        assert (
            _domain(engine)
            .transition(
                _request(
                    engine, scenario, b, (x,), "retain-incomplete-completed-member"
                )
            )
            .ok
        )
    retained = _snapshot(engine, scenario)
    assert (
        len(
            [
                review
                for review in retained.story_dependency_reviews
                if review.source_fingerprint == source
            ]
        )
        == 1
    )
    assert _story(retained, b).selected_scope_fingerprint == source
    assert not _story(retained, b).dependency_safe
    assert (
        planning_decision(
            _domain(engine).position(scenario.project_id), "planning.story_dependencies"
        ).reason_code
        == "STORY_DEPENDENCY_EXTERNAL_INCOMPLETE"
    )
    with Session(engine) as session:
        completed = session.get_one(UserStory, x)
        completed.status = StoryStatus.DONE
        session.add(completed)
        session.commit()
    safe = _snapshot(engine, scenario)
    assert _story(safe, b).dependency_safe
    assert _story(safe, b).sprint_candidate
    assert _completed_history(safe) == history
    # Pure authority classification covers superseded completed membership;
    # replacing the completed execution Story itself would invalidate its
    # retained execution-content contract and is outside this fixture.
    obsolete_stories = tuple(
        story.model_copy(update={"is_superseded": True})
        if story.story_id == x
        else story
        for story in safe.stories
    )
    assert [
        (edge.dependent_story_id, edge.prerequisite_story_id)
        for edge in superseded_dependency_edges(
            stories=obsolete_stories,
            dependencies=safe.story_dependencies,
            root_story_ids=(b, x),
            completed_story_ids=completed_ids,
        )
    ] == [(b, x)]
    assert _completed_history(_snapshot(engine, scenario)) == history


@pytest.mark.parametrize("old_status", [StoryStatus.TO_DO, StoryStatus.DONE])
@pytest.mark.parametrize("choice", ["exclude", "replacement"])
def test_review_reopens_after_accepted_external_prerequisite_replacement(  # noqa: PLR0915
    engine: Engine, old_status: StoryStatus, choice: str
) -> None:
    """Old completion must not satisfy obsolete authority or transfer approval."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    with Session(engine) as session:
        old = session.get_one(UserStory, a)
        old.status = old_status
        session.add(old)
        session.commit()
    edge_id = _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    old_reviews = _business_state(engine)[1]
    replacement_artifact_id, (c,) = _replace(engine, scenario.artifact_ids[0])
    data = _inspect(engine, scenario)
    assert [
        (edge["dependency_id"], edge["prerequisite_story_id"], edge["status"])
        for edge in data["edges"]
    ] == [(edge_id, a, "active")]
    assert all(edge["prerequisite_story_id"] != c for edge in data["edges"])
    assert data["selected_story_ids"] == [b]
    before = _snapshot(engine, scenario)
    assert not _story(before, b).dependency_safe
    assert not _story(before, b).sprint_candidate
    position = _domain(engine).position(scenario.project_id)
    review = planning_decision(position, "planning.story_dependencies")
    assert (review.category, review.reason_code) == (
        NodeCategory.AVAILABLE,
        "STORY_DEPENDENCY_REVIEW_REQUIRED",
    )
    assert "STORY_DEPENDENCY_SUPERSEDED_ENDPOINT" in {x.code for x in review.blockers}
    planning = planning_decision(position, "planning.sprint.plan")
    assert planning.category is NodeCategory.BLOCKED
    assert "STORY_DEPENDENCY_SUPERSEDED_ENDPOINT" in {x.code for x in planning.blockers}
    request = _request(
        engine,
        scenario,
        b,
        () if choice == "exclude" else (c,),
        f"renew-{old_status.value}-{choice}",
    )
    result = _domain(engine).transition(request)
    assert result.ok is True
    after = _snapshot(engine, scenario)
    assert (
        _story(after, b).selected_scope_fingerprint
        == _story(before, b).selected_scope_fingerprint
    )
    assert _business_state(engine)[1][0] == old_reviews[0]
    assert (
        next(x for x in after.story_dependencies if x.dependency_id == edge_id).status
        == "rejected"
    )
    if choice == "replacement":
        assert not _story(after, b).dependency_safe
        with Session(engine) as session:
            replacement = session.get_one(UserStory, c)
            replacement.status = StoryStatus.DONE
            session.add(replacement)
            session.commit()
    ready = _story(_snapshot(engine, scenario), b)
    assert ready.dependency_safe
    assert ready.sprint_candidate
    if choice == "replacement":
        # Replacing explicitly confirmed C creates one new epoch. Replacing its
        # successor again must not move B's old-C identity to the latest leaf.
        next_artifact_id, (c2,) = _replace(engine, replacement_artifact_id)
        obsolete = _snapshot(engine, scenario)
        new_source = _story(obsolete, b).selected_scope_fingerprint
        assert new_source != ready.selected_scope_fingerprint
        assert not _story(obsolete, b).dependency_safe
        assert (
            planning_decision(
                _domain(engine).position(scenario.project_id),
                "planning.story_dependencies",
            ).category
            is NodeCategory.AVAILABLE
        )
        _replace(engine, next_artifact_id)
        repeated = _snapshot(engine, scenario)
        assert _story(repeated, b).selected_scope_fingerprint == new_source
        assert all(
            edge.prerequisite_story_id != c2 for edge in repeated.story_dependencies
        )
        assert (
            _domain(engine)
            .transition(
                _request(engine, scenario, b, (), "renew-after-repeated-c-replacement")
            )
            .ok
        )
        assert _story(_snapshot(engine, scenario), b).dependency_safe


@pytest.mark.parametrize("edge_status", ["active", "proposed"])
def test_superseded_transitive_endpoint_cannot_satisfy_review(
    engine: Engine, edge_status: str
) -> None:
    """Renewing B->X cannot silently reconcile the external X->A authority."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    _edge(engine, scenario, b, x)
    _review(engine, scenario, b)
    _edge(engine, scenario, x, a, status=edge_status)
    _replace(engine, scenario.artifact_ids[0])
    position = _domain(engine).position(scenario.project_id)
    decision = planning_decision(position, "planning.story_dependencies")
    assert decision.category is NodeCategory.AVAILABLE
    assert "STORY_DEPENDENCY_SUPERSEDED_ENDPOINT" in {x.code for x in decision.blockers}
    before = _business_state(engine)
    result = _domain(engine).transition(
        _request(engine, scenario, b, (x,), "unsafe-transitive")
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert "STORY_DEPENDENCY_SUPERSEDED_ENDPOINT" in result.error.message
    assert _business_state(engine) == before
    excluded = _domain(engine).transition(
        _request(engine, scenario, b, (), "exclude-external")
    )
    assert excluded.ok
    assert _story(_snapshot(engine, scenario), b).dependency_safe


def test_superseded_proposed_endpoint_requires_confirmation(engine: Engine) -> None:
    """An obsolete proposal offers human exclusion but cannot be approved."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a, status="proposed")
    select_for_sprint(engine, b)
    _replace(engine, scenario.artifact_ids[0])
    decision = planning_decision(
        _domain(engine).position(scenario.project_id), "planning.story_dependencies"
    )
    assert decision.category is NodeCategory.AVAILABLE
    assert "STORY_DEPENDENCY_SUPERSEDED_ENDPOINT" in {x.code for x in decision.blockers}
    before = _business_state(engine)
    rejected = _domain(engine).transition(
        _request(engine, scenario, b, (a,), "approve-obsolete")
    )
    assert not rejected.ok
    assert rejected.error is not None
    assert rejected.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert "STORY_DEPENDENCY_STORY_SET_INVALID" in rejected.error.message
    assert _business_state(engine) == before
    assert (
        _domain(engine)
        .transition(_request(engine, scenario, b, (), "exclude-obsolete-proposal"))
        .ok
    )
    assert _story(_snapshot(engine, scenario), b).dependency_safe


def test_old_source_submission_conflicts_without_writes(engine: Engine) -> None:
    """Exact historical replay remains replay; a fresh old-source key conflicts."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    select_for_sprint(engine, b)
    request = _request(engine, scenario, b, (a,), "original-reviewed-source")
    domain = _domain(engine)
    original = domain.transition(request)
    assert original.ok
    _replace(engine, scenario.artifact_ids[0])
    before = _business_state(engine)
    replay = domain.transition(request)
    assert replay.ok
    assert replay.replayed
    stale_request = _request(
        engine,
        scenario,
        b,
        (),
        "fresh-key-old-source",
        source=request.source_fingerprint,
    )
    stale = domain.transition(stale_request)
    assert not stale.ok
    assert stale.error is not None
    assert stale.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert _business_state(engine) == before
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.idempotency_key
                == stale_request.idempotency_key,
                WorkflowTransitionReceipt.request_kind == "apply_story_dependencies",
            )
        ).one()
        assert receipt.completed_at is not None
        assert receipt.result_json is not None
        recorded_result = json.loads(receipt.result_json)
        assert recorded_result["ok"] is False
        assert (
            recorded_result["error"]["code"]
            == WorkflowErrorCode.WORKFLOW_FACT_CONFLICT.value
        )
        receipt_before = receipt.model_dump(mode="json")
    replay_rejection = domain.transition(stale_request)
    assert not replay_rejection.ok
    assert replay_rejection.replayed
    assert replay_rejection.error == stale.error
    assert _business_state(engine) == before
    with Session(engine) as session:
        receipt_after = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.idempotency_key
                == stale_request.idempotency_key,
                WorkflowTransitionReceipt.request_kind == "apply_story_dependencies",
            )
        ).one()
        assert receipt_after.model_dump(mode="json") == receipt_before


@pytest.mark.parametrize("identifiable", [True, False])
def test_legacy_implicit_invalidation_is_adopted_without_source_change(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, identifiable: bool
) -> None:
    """Reconciliation must persist its implicit epoch before removing the path."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    if identifiable:
        _legacy_replace(engine, scenario.artifact_ids[0], monkeypatch)
    else:
        with Session(engine) as session:
            old = session.get_one(UserStory, a)
            old.is_superseded = True
            session.add(old)
            session.commit()
    before = _snapshot(engine, scenario)
    source = _story(before, b).selected_scope_fingerprint
    assert not _story(before, b).dependency_safe
    data = _inspect(engine, scenario)
    assert data["selected_scope_fingerprint"] == source
    assert _events(engine) == ()
    with Session(engine) as session:
        implicit = infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(b,)
        )
    assert (implicit[0].replacement_story_artifact_id is not None) is identifiable
    available = planning_decision(
        _domain(engine).position(scenario.project_id), "planning.story_dependencies"
    )
    assert available.category is NodeCategory.AVAILABLE
    assert available.reason_code == "STORY_DEPENDENCY_REVIEW_REQUIRED"
    request = _request(engine, scenario, b, (), "adopt-and-exclude")
    assert _domain(engine).transition(request).ok
    after = _snapshot(engine, scenario)
    assert _story(after, b).selected_scope_fingerprint == source
    assert _story(after, b).dependency_safe
    assert _story(after, b).sprint_candidate
    with Session(engine) as session:
        assert (
            load_story_dependency_invalidations_in_session(
                session, project_id=scenario.project_id
            )
            == implicit
        )
    events = _events(engine)
    assert len(events) == 1
    assert events[0].event_metadata is not None
    assert json.loads(events[0].event_metadata)["mode"] == "review_adoption"
    assert _domain(engine).transition(request).replayed
    assert _events(engine)[0].event_metadata == events[0].event_metadata
    if not identifiable:
        # Enrich successor diagnostics after the path has already been repaired.
        _replace(engine, scenario.artifact_ids[0])
        enriched = _snapshot(engine, scenario)
        assert _story(enriched, b).selected_scope_fingerprint == source
        assert _story(enriched, b).dependency_safe
        assert _events(engine)[0].event_metadata == events[0].event_metadata
        assert enriched.story_dependency_reviews[-1].source_fingerprint == source


def test_new_edge_to_rejected_history_does_not_stale_renewal(engine: Engine) -> None:
    """Rejected obsolete history never joins new approved authority or its epoch."""
    scenario = _seed(engine, 4)
    a, b, x, d = scenario.story_ids
    _edge(engine, scenario, b, a)
    _edge(engine, scenario, x, d, status="rejected")
    _review(engine, scenario, b)
    _replace(engine, scenario.artifact_ids[0])
    _replace(engine, scenario.artifact_ids[3])
    source = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    assert (
        _domain(engine)
        .transition(_request(engine, scenario, b, (x,), "approve-rejected-history"))
        .ok
    )
    assert _story(_snapshot(engine, scenario), b).selected_scope_fingerprint == source
    _replace(engine, scenario.artifact_ids[2])
    revised = _snapshot(engine, scenario)
    assert _story(revised, b).selected_scope_fingerprint != source
    assert (
        planning_decision(
            _domain(engine).position(scenario.project_id), "planning.story_dependencies"
        ).category
        is NodeCategory.AVAILABLE
    )


def test_newly_requested_unsafe_endpoint_closure_fails_without_writes(
    engine: Engine,
) -> None:
    """Human approval of B->X cannot approve obsolete authority behind X."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    _edge(engine, scenario, x, a, status="proposed")
    _replace(engine, scenario.artifact_ids[0])
    select_for_sprint(engine, b)
    before = _business_state(engine)
    result = _domain(engine).transition(
        _request(engine, scenario, b, (x,), "new-unsafe-edge")
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert "STORY_DEPENDENCY_SUPERSEDED_ENDPOINT" in result.error.message
    assert _business_state(engine) == before


def test_proposed_dependency_review_can_repair_an_existing_active_cycle(
    engine: Engine,
) -> None:
    """A proposal opens human repair while cyclic approval still fails closed."""
    scenario = _seed(engine)
    y, b, x = scenario.story_ids
    with Session(engine) as session:
        completed = session.get_one(UserStory, y)
        completed.status = StoryStatus.DONE
        session.add(completed)
        session.commit()
    selected_cycle_edge = _edge(engine, scenario, b, x)
    external_cycle_edge = _edge(engine, scenario, x, b)
    proposal = _edge(engine, scenario, b, y, status="proposed")
    select_for_sprint(engine, b)
    decision = planning_decision(
        _domain(engine).position(scenario.project_id), "planning.story_dependencies"
    )
    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "STORY_DEPENDENCY_REVIEW_REQUIRED"
    assert {blocker.code for blocker in decision.blockers} == {
        "STORY_DEPENDENCIES_UNREVIEWED"
    }
    before = _business_state(engine)
    unsafe = _domain(engine).transition(
        _request(engine, scenario, b, (y, x), "keep-current-cycle")
    )
    assert not unsafe.ok
    assert unsafe.error is not None
    assert unsafe.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert "STORY_DEPENDENCY_CYCLE" in unsafe.error.message
    assert _business_state(engine) == before
    repaired = _domain(engine).transition(
        _request(engine, scenario, b, (y,), "repair-current-cycle")
    )
    assert repaired.ok
    snapshot = _snapshot(engine, scenario)
    assert _story(snapshot, b).dependency_safe
    assert {
        edge.dependency_id: edge.status for edge in snapshot.story_dependencies
    } == {
        selected_cycle_edge: "rejected",
        external_cycle_edge: "active",
        proposal: "active",
    }


@pytest.mark.parametrize(
    "stage",
    [
        "load_story_dependency_invalidations_in_session",
        "infer_unrecorded_story_dependency_invalidations_in_session",
        "adopt_story_dependency_invalidations_in_session",
    ],
)
def test_unrelated_lifecycle_value_error_is_not_mislabeled_as_obsolete_authority(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Only the prospective validator's semantic ValueError is an obsolete issue."""
    scenario = _seed(engine)
    _, b, _ = scenario.story_ids
    select_for_sprint(engine, b)
    source = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    assert source is not None
    before = _business_state(engine)
    original = getattr(writer, stage)
    message = "Unrelated lifecycle failure"
    expected_error = ValueError(message)

    def fail_after_real_validation(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise expected_error

    monkeypatch.setattr(writer, stage, fail_after_real_validation)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="Unrelated lifecycle failure") as caught:
            apply_story_dependencies_in_session(
                session,
                inputs=ApplyStoryDependenciesInput(
                    project_id=scenario.project_id,
                    selected_story_ids=(b,),
                    reviewed_edges=(),
                    source_fingerprint=source,
                    reviewer="operator@example.com",
                    reviewed_at=EVALUATED_AT,
                ),
            )
        assert caught.value is expected_error
        session.rollback()
    assert _business_state(engine) == before


def test_prospective_malformed_closure_is_integrity_error(engine: Engine) -> None:
    """A reached corrupt row is distinct from semantic obsolete authority."""
    scenario = _seed(engine)
    _, b, x = scenario.story_ids
    # Deliberate stored-row corruption in this disposable database; retain all
    # real Project/Story identities and restore normal CHECK enforcement.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        try:
            _edge(engine, scenario, x, x)
        finally:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    with Session(engine) as session:
        error = None
        try:
            validate_reviewed_dependency_lifecycle_in_session(
                session,
                project_id=scenario.project_id,
                selected_story_ids=(b,),
                reviewed_edges=(
                    StoryDependencyReviewEdgeFact(
                        dependent_story_id=b,
                        prerequisite_story_id=x,
                        reason="Explicit live prerequisite.",
                    ),
                ),
            )
        except (ValueError, StoryDependencyLifecycleIntegrityError) as exc:
            error = exc
        assert isinstance(error, StoryDependencyLifecycleIntegrityError)
        assert "malformed endpoints" in str(error)


@pytest.mark.parametrize("problem", ["cycle", "foreign"])
def test_prospective_endpoint_integrity_still_fails_closed(
    engine: Engine, problem: str
) -> None:
    """Prospective checks must retain cycles and corruption as structured failures."""
    scenario = _seed(engine)
    y, b, x = scenario.story_ids
    if problem == "cycle":
        _edge(engine, scenario, x, y)
        _edge(engine, scenario, y, x)
    else:
        # A real accepted foreign Story keeps every FK valid while violating
        # the dependency row's Project ownership contract.
        with Session(engine) as session:
            _, foreign_story_id = _story_set(session, titles=("Foreign prerequisite",))
        _edge(engine, scenario, x, foreign_story_id)
    before = _business_state(engine)
    with Session(engine) as session:
        with pytest.raises(StoryDependencyGraphError) as caught:
            apply_story_dependencies_in_session(
                session,
                inputs=ApplyStoryDependenciesInput(
                    project_id=scenario.project_id,
                    selected_story_ids=(b,),
                    reviewed_edges=(
                        StoryDependencyReviewEdgeFact(
                            dependent_story_id=b,
                            prerequisite_story_id=x,
                            reason="Human requests exact external authority.",
                        ),
                    ),
                    source_fingerprint="sha256:" + "e" * 64,
                    reviewer="operator@example.com",
                    reviewed_at=EVALUATED_AT,
                ),
            )
        assert {issue.code for issue in caught.value.issues} == {
            "STORY_DEPENDENCY_CYCLE"
            if problem == "cycle"
            else "STORY_DEPENDENCY_INVALID"
        }
        session.rollback()
    assert _business_state(engine) == before


@pytest.mark.parametrize(
    "terminal_status",
    [StoryStatus.DONE, StoryStatus.ACCEPTED, None],
    ids=["done", "accepted", "completed-membership"],
)
def test_review_rejects_new_cycle_through_completed_external_history(
    engine: Engine, terminal_status: StoryStatus | None
) -> None:
    """Approval cannot close X->B->X across a readiness terminal boundary."""
    scenario = _seed(engine)
    _, b, x = scenario.story_ids
    if terminal_status is None:
        _complete_fixture_story_sprint(engine, scenario, x)
    with Session(engine) as session:
        external = session.get_one(UserStory, x)
        external.status = terminal_status or StoryStatus.TO_DO
        session.add(external)
        session.commit()
    _edge(engine, scenario, x, b)
    before = _project_business_rows(engine, scenario.project_id)
    with Session(engine) as session:
        with pytest.raises(StoryDependencyGraphError) as caught:
            apply_story_dependencies_in_session(
                session,
                inputs=ApplyStoryDependenciesInput(
                    project_id=scenario.project_id,
                    selected_story_ids=(b,),
                    reviewed_edges=(
                        StoryDependencyReviewEdgeFact(
                            dependent_story_id=b,
                            prerequisite_story_id=x,
                            reason="Explicit review of completed external authority.",
                        ),
                    ),
                    source_fingerprint="sha256:" + "e" * 64,
                    reviewer="operator@example.com",
                    reviewed_at=EVALUATED_AT,
                ),
            )
        assert {issue.code for issue in caught.value.issues} == {
            "STORY_DEPENDENCY_CYCLE"
        }
        session.rollback()
    assert _project_business_rows(engine, scenario.project_id) == before


@pytest.mark.parametrize("closes_cycle", [True, False])
def test_review_checks_each_submitted_pair_past_an_existing_cycle(
    engine: Engine, closes_cycle: bool
) -> None:
    """A visited X<->Y branch cannot hide B->Y->X->B or condemn B->Y alone."""
    scenario = _seed(engine)
    x, y, b = scenario.story_ids
    with Session(engine) as session:
        terminal = session.get_one(UserStory, x)
        terminal.status = StoryStatus.DONE
        session.add(terminal)
        session.commit()
    history_ids = (_edge(engine, scenario, x, y), _edge(engine, scenario, y, x))
    if closes_cycle:
        _edge(engine, scenario, x, b)
    before = _project_business_rows(engine, scenario.project_id)
    inputs = ApplyStoryDependenciesInput(
        project_id=scenario.project_id,
        selected_story_ids=(b,),
        reviewed_edges=(
            StoryDependencyReviewEdgeFact(
                dependent_story_id=b,
                prerequisite_story_id=y,
                reason="Exact external prerequisite with retained history.",
            ),
        ),
        source_fingerprint="sha256:" + "e" * 64,
        reviewer="operator@example.com",
        reviewed_at=EVALUATED_AT,
    )
    with Session(engine) as session:
        if closes_cycle:
            with pytest.raises(StoryDependencyGraphError) as caught:
                apply_story_dependencies_in_session(session, inputs=inputs)
            assert {issue.code for issue in caught.value.issues} == {
                "STORY_DEPENDENCY_CYCLE"
            }
            session.rollback()
        else:
            apply_story_dependencies_in_session(session, inputs=inputs)
            session.commit()
    if closes_cycle:
        assert _project_business_rows(engine, scenario.project_id) == before
    else:
        with Session(engine) as session:
            assert all(
                session.get_one(UserStoryDependency, edge_id).status == "active"
                for edge_id in history_ids
            )
            assert {
                (edge.dependent_story_id, edge.prerequisite_story_id)
                for edge in session.exec(select(UserStoryDependency)).all()
                if edge.status == "active"
            } == {(x, y), (y, x), (b, y)}


@pytest.mark.parametrize("status", [StoryStatus.DONE, StoryStatus.ACCEPTED])
@pytest.mark.parametrize("cyclic", [False, True])
def test_terminal_boundary_hides_incomplete_and_cyclic_history_without_supersession(
    engine: Engine, status: StoryStatus, cyclic: bool
) -> None:
    """Readiness stops at X while full active cycles still prevent planning."""
    scenario = _seed(engine)
    y, b, x = scenario.story_ids
    with Session(engine) as session:
        terminal = session.get_one(UserStory, x)
        terminal.status = status
        session.add(terminal)
        session.commit()
    edge_id = _edge(engine, scenario, b, x)
    _review(engine, scenario, b)
    _edge(engine, scenario, x, y)
    if cyclic:
        _edge(engine, scenario, y, x)
    snapshot = _snapshot(engine, scenario)
    assert _story(snapshot, b).dependency_safe
    assert _story(snapshot, b).sprint_candidate
    assert (
        next(
            edge
            for edge in snapshot.story_dependencies
            if edge.dependency_id == edge_id
        ).status
        == "active"
    )
    position = _domain(engine).position(scenario.project_id)
    assert all(
        item.reason_code != "STORY_DEPENDENCY_EXTERNAL_INCOMPLETE"
        for item in position.decisions
    )
    assert _inspect(engine, scenario)["issues"] == []
    assert planning_decision(position, "planning.sprint.plan").reason_code == (
        "STORY_DEPENDENCY_CYCLE" if cyclic else "SPRINT_PLANNING_REQUIRED"
    )
    with Session(engine) as session:
        incomplete = session.get_one(UserStory, x)
        incomplete.status = StoryStatus.TO_DO
        session.add(incomplete)
        session.commit()
    unsafe = _snapshot(engine, scenario)
    assert not _story(unsafe, b).dependency_safe
    decision = planning_decision(
        _domain(engine).position(scenario.project_id), "planning.story_dependencies"
    )
    assert decision.reason_code == (
        "STORY_DEPENDENCY_CYCLE" if cyclic else "STORY_DEPENDENCY_EXTERNAL_INCOMPLETE"
    )


def _selection(
    engine: Engine, story_id: int, intent: StorySprintSelectionIntent, key: str
) -> None:
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        current = story_sprint_selection_fact_in_session(session, story=story)
        result = apply_story_sprint_selection_with_receipt_in_session(
            session,
            StorySprintSelectionRequest(
                project_id=story.project_id,
                story_id=story_id,
                intent=intent,
                expected_state_fingerprint=current.state_fingerprint,
                idempotency_key=key,
                actor="operator@example.com",
            ),
        )
        assert result.selection_state == (
            "selected" if intent == "select" else "unselected"
        )
        session.commit()


def _selected_base(engine: Engine, scenario: _Scenario, story_id: int) -> str:
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        selection = story_sprint_selection_fact_in_session(session, story=story)
        evidence = require_story_ready_for_sprint(session, story=story)
        assert selection.event_id is not None
        assert selection.event_fingerprint is not None
        return selected_scope_fingerprint(
            project_id=scenario.project_id,
            stories=(
                SelectedScopeStory(
                    story_id=story_id,
                    source_story_artifact_id=story.source_story_artifact_id,
                    source_story_artifact_fingerprint=story.source_story_artifact_fingerprint,
                    source_story_item_id=story.source_story_item_id,
                    source_story_item_fingerprint=story.source_story_item_fingerprint,
                    accepted_spec_version_id=story.accepted_spec_version_id,
                    accepted_spec_hash=story.accepted_spec_hash,
                    validation_evidence_fingerprint=canonical_hash(
                        evidence.model_dump(mode="json")
                    ),
                    selection_state_fingerprint=selection.state_fingerprint,
                    selection_event_id=selection.event_id,
                    selection_event_fingerprint=selection.event_fingerprint,
                ),
            ),
        )


def test_legacy_unselected_ancestor_anchor_survives_external_scope_reconciliation(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repairing selected X must preserve unselected B's already implicit epoch."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    b_edge_id = _edge(engine, scenario, b, x)
    _edge(engine, scenario, x, a)
    _review(engine, scenario, b)
    _legacy_replace(engine, scenario.artifact_ids[0], monkeypatch)
    with Session(engine) as session:
        b_implicit = infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(b,)
        )
    fixed_b_base = _selected_base(engine, scenario, b)
    expected_revision = selected_scope_dependency_lifecycle_fingerprint(
        selected_scope_fingerprint=fixed_b_base,
        selected_story_ids=(b,),
        invalidation_anchors=b_implicit,
    )
    _selection(engine, b, "remove", "unselect-b")
    select_for_sprint(engine, x)
    before = _snapshot(engine, scenario)
    projected = _story(before, b).selected_scope_fingerprint
    assert projected is not None
    b_edge_before = next(
        edge for edge in before.story_dependencies if edge.dependency_id == b_edge_id
    )
    b_reviews_before = tuple(
        review
        for review in before.story_dependency_reviews
        if b in review.selected_story_ids
    )
    assert (
        _domain(engine)
        .transition(_request(engine, scenario, x, (), "repair-external-x"))
        .ok
    )
    repaired = _snapshot(engine, scenario)
    assert _story(repaired, b).selected_scope_fingerprint == projected
    assert (
        next(
            edge
            for edge in repaired.story_dependencies
            if edge.dependency_id == b_edge_id
        )
        == b_edge_before
    )
    assert (
        tuple(
            review
            for review in repaired.story_dependency_reviews
            if b in review.selected_story_ids
        )
        == b_reviews_before
    )
    with Session(engine) as session:
        adopted = load_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id
        )
    b_persisted = tuple(anchor for anchor in adopted if anchor.dependent_story_id == b)
    assert b_persisted == b_implicit
    assert (
        selected_scope_dependency_lifecycle_fingerprint(
            selected_scope_fingerprint=fixed_b_base,
            selected_story_ids=(b,),
            invalidation_anchors=b_persisted,
        )
        == expected_revision
    )
    assert expected_revision != fixed_b_base
    _selection(engine, x, "remove", "unselect-x")
    _selection(engine, b, "select", "reselect-b")
    reselected = _snapshot(engine, scenario)
    assert _story(
        reselected, b
    ).selected_scope_fingerprint == selected_scope_dependency_lifecycle_fingerprint(
        selected_scope_fingerprint=_selected_base(engine, scenario, b),
        selected_story_ids=(b,),
        invalidation_anchors=b_persisted,
    )
    assert (
        _domain(engine)
        .transition(_request(engine, scenario, b, (x,), "renew-b-after-x-repair"))
        .ok
    )
    renewed = _snapshot(engine, scenario)
    assert (
        renewed.story_dependency_reviews[-1].source_fingerprint
        == _story(renewed, b).selected_scope_fingerprint
    )
    assert (
        planning_decision(
            _domain(engine).position(scenario.project_id), "planning.story_dependencies"
        ).reason_code
        == "STORY_DEPENDENCY_EXTERNAL_INCOMPLETE"
    )


def test_dependency_adoption_and_apply_roll_back_together(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late review failure rolls back its already-adopted epoch and graph writes."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    _legacy_replace(engine, scenario.artifact_ids[0], monkeypatch)
    before = _business_state(engine)
    source = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    assert source is not None
    with Session(engine) as session:

        def fail_after_adoption(_edges: tuple) -> str:
            events = session.exec(select(WorkflowEvent)).all()

            def is_adoption_event(event: WorkflowEvent) -> bool:
                assert event.event_metadata is not None
                return json.loads(event.event_metadata).get("mode") == "review_adoption"

            assert any(
                is_adoption_event(event)
                for event in events
                if event.event_type is WorkflowEventType.STORY_DEPENDENCY_STALE
            )
            message = "Injected late review failure"
            raise RuntimeError(message)

        monkeypatch.setattr(
            writer, "dependency_review_fingerprint", fail_after_adoption
        )
        with pytest.raises(RuntimeError, match="Injected late review failure"):
            apply_story_dependencies_in_session(
                session,
                inputs=ApplyStoryDependenciesInput(
                    project_id=scenario.project_id,
                    selected_story_ids=(b,),
                    reviewed_edges=(),
                    source_fingerprint=source,
                    reviewer="operator@example.com",
                    reviewed_at=EVALUATED_AT,
                ),
            )
        session.rollback()
    assert _business_state(engine) == before


def test_prerequisite_replacement_revises_selected_scope(engine: Engine) -> None:
    """Accepting A' makes B's unchanged selected lineage a new review epoch."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    edge_id = _edge(engine, scenario, b, x)
    _edge(engine, scenario, x, a)
    _review(engine, scenario, b)
    before = _snapshot(engine, scenario)
    with Session(engine) as session:
        old_review = session.exec(select(StoryDependencyReview)).one()
        saved_review = old_review.model_dump()
    replacement_id, replacement_stories = _replace(engine, scenario.artifact_ids[0])
    after = _snapshot(engine, scenario)
    old_b, new_b = _story(before, b), _story(after, b)
    assert new_b.selected_scope_fingerprint != old_b.selected_scope_fingerprint
    assert (
        new_b.sprint_selection_state_fingerprint
        == old_b.sprint_selection_state_fingerprint
    )
    assert (
        new_b.source_story_artifact_fingerprint
        == old_b.source_story_artifact_fingerprint
    )
    assert new_b.source_story_item_fingerprint == old_b.source_story_item_fingerprint
    events = _events(engine)
    assert len(events) == 1
    metadata = json.loads(events[0].event_metadata or "{}")
    assert {
        (anchor["dependent_story_id"], anchor["superseded_story_id"])
        for anchor in metadata["anchors"]
    } == {(b, a), (x, a)}
    assert all(
        anchor["replacement_story_artifact_id"] == replacement_id
        for anchor in metadata["anchors"]
    )
    assert all(
        anchor["superseded_story_artifact_id"] == scenario.artifact_ids[0]
        for anchor in metadata["anchors"]
    )
    assert all(
        _story(after, story_id).sprint_selection_state == "unselected"
        for story_id in replacement_stories
    )
    with Session(engine) as session:
        assert (
            session.exec(select(StoryDependencyReview)).one().model_dump()
            == saved_review
        )
        edge = session.get_one(UserStoryDependency, edge_id)
        assert (edge.dependent_story_id, edge.prerequisite_story_id, edge.status) == (
            b,
            x,
            "active",
        )
        invalidate_story_dependents_for_replacement_in_session(
            session,
            artifact=session.get_one(StoryArtifact, replacement_id),
            superseded_stories=(session.get_one(UserStory, a),),
            accepted_at=EVALUATED_AT + timedelta(minutes=2),
        )
        session.commit()
    assert len(_events(engine)) == 1
    assert (
        _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
        == new_b.selected_scope_fingerprint
    )


@pytest.mark.parametrize(
    "mode", ["unrelated", "rejected_edge", "draft", "feedback", "rejected"]
)
def test_unaffected_scope_keeps_v1_fingerprint(engine: Engine, mode: str) -> None:
    """Preserve the v1 source when replacement has no actionable selected impact."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    if mode != "unrelated":
        _edge(
            engine,
            scenario,
            b,
            a,
            status="rejected" if mode == "rejected_edge" else "active",
        )
    _review(engine, scenario, b)
    before = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    with Session(engine) as session:
        artifact = _draft(session, scenario.artifact_ids[0])
        if mode != "draft":
            _decide(
                session,
                artifact,
                mode if mode in {"feedback", "rejected"} else "accepted",
            )
        session.commit()
    assert _story(_snapshot(engine, scenario), b).selected_scope_fingerprint == before
    assert _events(engine) == ()


def test_dependent_replacement_invalidation_targets_each_live_ancestor(
    engine: Engine,
) -> None:
    """Target each live ancestor without transferring superseded dependent history."""
    scenario = _seed(engine, 4)
    a, b, d, e = scenario.story_ids
    audit_edge = _edge(engine, scenario, b, a)
    _edge(engine, scenario, d, b)
    _edge(engine, scenario, e, b, status="proposed")
    replacement_id, replacement_stories = _replace(engine, scenario.artifact_ids[1])
    events = _events(engine)
    assert len(events) == 1
    anchors = json.loads(events[0].event_metadata or "{}")["anchors"]
    assert {
        (anchor["dependent_story_id"], anchor["superseded_story_id"])
        for anchor in anchors
    } == {(d, b), (e, b)}
    assert all(
        anchor["replacement_story_artifact_id"] == replacement_id for anchor in anchors
    )
    with Session(engine) as session:
        assert (
            session.get_one(UserStoryDependency, audit_edge).prerequisite_story_id == a
        )
        assert not any(
            edge.dependent_story_id in replacement_stories
            for edge in session.exec(select(UserStoryDependency)).all()
        )
    after = _snapshot(engine, scenario)
    assert all(
        _story(after, story_id).sprint_selection_state == "unselected"
        for story_id in replacement_stories
    )


def test_multi_story_replacement_invalidation_is_one_canonical_event(
    engine: Engine,
) -> None:
    """Capture all old identities from one accepted Story-set replacement once."""
    scenario = _seed(engine)
    b = scenario.story_ids[1]
    with Session(engine) as session:
        first_replacement = _draft(
            session, scenario.artifact_ids[0], "split", item_count=2
        )
        a1, a2 = _decide(session, first_replacement)
        first_replacement_id = first_replacement.story_artifact_id
        assert first_replacement_id is not None
        session.commit()
    _edge(engine, scenario, b, a2)
    _edge(engine, scenario, b, a1, status="proposed")
    replacement_id, new_ids = _replace(engine, first_replacement_id)
    events = _events(engine)
    assert len(events) == 1
    anchors = json.loads(events[0].event_metadata or "{}")["anchors"]
    assert [
        (
            anchor["dependent_story_id"],
            anchor["superseded_story_id"],
            anchor["superseded_story_artifact_id"],
        )
        for anchor in anchors
    ] == [(b, a1, first_replacement_id), (b, a2, first_replacement_id)]
    assert all(
        anchor["replacement_story_artifact_id"] == replacement_id for anchor in anchors
    )
    with Session(engine) as session:
        assert not any(
            edge.prerequisite_story_id in new_ids
            for edge in session.exec(select(UserStoryDependency)).all()
        )


def test_acceptance_and_invalidation_roll_back_together(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roll back invalidation together with every failed acceptance side effect."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    before = _snapshot(engine, scenario)
    with Session(engine) as session:
        draft = _draft(session, scenario.artifact_ids[0])
        draft_id = draft.story_artifact_id
        session.commit()
    observed_events: list[int] = []

    def fail_validation(session: Session, *_args: object, **_kwargs: object) -> None:
        observed_events.append(
            sum(
                event.event_type.value == "story_dependency_stale"
                for event in session.exec(select(WorkflowEvent)).all()
            )
        )
        message = "Injected structural evidence failure"
        raise RuntimeError(message)

    monkeypatch.setattr(
        story_phase, "validate_story_with_specification_in_session", fail_validation
    )
    with Session(engine) as session:
        with pytest.raises(story_phase.StoryAcceptanceValidationError):
            _decide(session, session.get_one(StoryArtifact, draft_id))
        session.rollback()
    assert observed_events == [1]
    after = _snapshot(engine, scenario)
    assert after.stories == before.stories
    assert after.story_dependencies == before.story_dependencies
    assert after.story_dependency_reviews == before.story_dependency_reviews
    assert _events(engine) == ()
    with Session(engine) as session:
        assert not any(
            row.story_artifact_id == draft_id
            for row in session.exec(select(StoryArtifactDecision)).all()
        )


@pytest.mark.parametrize("status", [StoryStatus.DONE, StoryStatus.ACCEPTED])
def test_completed_external_endpoint_history_does_not_block_review_or_readiness(
    engine: Engine, status: StoryStatus
) -> None:
    """Preserve completed external authority and its full pinned historical closure."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    with Session(engine) as session:
        completed = session.get_one(UserStory, a)
        completed.status = StoryStatus.DONE
        session.add(completed)
        session.commit()
    _edge(engine, scenario, x, a)
    domain = _domain(engine)
    binding = _record_sprint_plan_draft(
        engine,
        domain,
        scenario.project_id,
        x,
        team_name="Lifecycle boundary team",
        idempotency_key="boundary-plan",
    )
    sprint_id = _accept_and_start_sprint(
        domain,
        project_id=scenario.project_id,
        plan_binding=binding,
        idempotency_suffix="-boundary",
        engine=engine,
    )
    with Session(engine) as session:
        task_id = session.exec(select(Task).where(Task.story_id == x)).one().task_id
        assert task_id is not None
    execution = execution_domain(engine)
    _close_execution_sprint(
        execution,
        project_id=scenario.project_id,
        sprint_id=sprint_id,
        story_id=x,
        task_id=task_id,
    )
    _triage_execution_sprint(
        execution, project_id=scenario.project_id, sprint_id=sprint_id
    )
    with Session(engine) as session:
        completed = session.get_one(UserStory, x)
        completed.status = status
        session.add(completed)
        session.commit()
    _edge(engine, scenario, b, x)
    _review(engine, scenario, b)
    before = _snapshot(engine, scenario)
    with Session(engine) as session:
        # Completed X's prerequisite history may later become incomplete.
        old = session.get_one(UserStory, a)
        old.status = StoryStatus.TO_DO
        session.add(old)
        session.commit()
    _replace(engine, scenario.artifact_ids[0])
    after = _snapshot(engine, scenario)
    assert (
        _story(after, b).selected_scope_fingerprint
        == _story(before, b).selected_scope_fingerprint
    )
    assert after.story_dependency_reviews == before.story_dependency_reviews
    assert _events(engine) == ()
    assert _story(after, b).dependency_safe
    assert _story(after, b).sprint_candidate
    assert (
        superseded_dependency_edges(
            stories=after.stories,
            dependencies=after.story_dependencies,
            root_story_ids=(b,),
        )
        == ()
    )
    assert {
        (edge.dependent_story_id, edge.prerequisite_story_id)
        for edge in selected_dependency_active_closure(after.story_dependencies, (b,))
    } == {(b, x), (x, a)}
    assert [
        (edge.dependent_story_id, edge.prerequisite_story_id)
        for edge in current_dependency_closure(
            stories=after.stories,
            dependencies=after.story_dependencies,
            root_story_ids=(b,),
        )
    ] == [(b, x)]
    _edge(engine, scenario, b, a)
    direct = _snapshot(engine, scenario)
    assert [
        (edge.dependent_story_id, edge.prerequisite_story_id)
        for edge in superseded_dependency_edges(
            stories=direct.stories,
            dependencies=direct.story_dependencies,
            root_story_ids=(b,),
        )
    ] == [(b, a)]
    assert (
        _domain(engine)
        .transition(_request(engine, scenario, b, (x,), "retain-completed-external"))
        .ok
    )
    renewed = _snapshot(engine, scenario)
    assert _story(renewed, b).dependency_safe
    assert any(
        edge.dependent_story_id == b
        and edge.prerequisite_story_id == x
        and edge.status == "active"
        for edge in renewed.story_dependencies
    )


@pytest.mark.parametrize(
    "corruption", ["project", "source", "successor", "path", "noncanonical"]
)
def test_invalid_invalidation_anchor_fails_fact_loading(
    engine: Engine, corruption: str
) -> None:
    """Reject malformed or contradictory persisted causal evidence at fact loading."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    _replace(engine, scenario.artifact_ids[0])
    events = _events(engine)
    assert len(events) == 1
    with Session(engine) as session:
        event = session.get_one(WorkflowEvent, events[0].event_id)
        metadata = json.loads(event.event_metadata or "{}")
        if corruption == "project":
            metadata["anchors"][0]["dependent_story_id"] = 99999
        elif corruption == "source":
            metadata["anchors"][0]["superseded_story_artifact_fingerprint"] = (
                "sha256:" + "f" * 64
            )
        elif corruption == "successor":
            metadata["anchors"][0]["replacement_story_artifact_fingerprint"] = (
                "sha256:" + "f" * 64
            )
        elif corruption == "path":
            metadata["causal_paths"][0]["dependency_rows"][0][
                "prerequisite_story_id"
            ] = b

        event.event_metadata = (
            json.dumps(metadata)
            if corruption == "noncanonical"
            else canonical_json(metadata)
        )
        session.add(event)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        _snapshot(engine, scenario)


@pytest.mark.parametrize("status", [StoryStatus.DONE, StoryStatus.ACCEPTED])
def test_terminal_unattached_story_is_not_actionable_or_anchored(
    engine: Engine, status: StoryStatus
) -> None:
    """Stop actionable traversal at terminal Stories regardless of Sprint membership."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    _edge(engine, scenario, b, x)
    _edge(engine, scenario, x, a)
    with Session(engine) as session:
        terminal = session.get_one(UserStory, x)
        terminal.status = status
        session.add(terminal)
        session.commit()
    _review(engine, scenario, b)
    before = _snapshot(engine, scenario)
    _replace(engine, scenario.artifact_ids[0])
    after = _snapshot(engine, scenario)
    assert (
        _story(after, b).selected_scope_fingerprint
        == _story(before, b).selected_scope_fingerprint
    )
    assert after.story_dependency_reviews == before.story_dependency_reviews
    assert _events(engine) == ()
    assert (
        superseded_dependency_edges(
            stories=after.stories,
            dependencies=after.story_dependencies,
            root_story_ids=(b, x),
        )
        == ()
    )
    closure = current_dependency_closure(
        stories=after.stories,
        dependencies=after.story_dependencies,
        root_story_ids=(b, x),
        include_proposed=True,
    )
    assert [
        (edge.dependent_story_id, edge.prerequisite_story_id) for edge in closure
    ] == [(b, x)]


def test_legacy_successor_through_intervening_drafts_is_identified(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover exact first accepted ancestry across feedback and rejected drafts."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    before = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    with Session(engine) as session:
        feedback = _draft(session, scenario.artifact_ids[0], "feedback")
        _decide(session, feedback, "feedback")
        assert feedback.story_artifact_id is not None
        rejected = _draft(session, feedback.story_artifact_id, "rejected")
        _decide(session, rejected, "rejected")
        assert rejected.story_artifact_id is not None
        successor = _draft(session, rejected.story_artifact_id, "accepted")
        with monkeypatch.context() as context:
            context.setattr(
                story_phase,
                "invalidate_story_dependents_for_replacement_in_session",
                lambda *_args, **_kwargs: None,
            )
            _decide(session, successor)
        successor_id = successor.story_artifact_id
        session.commit()
    after = _snapshot(engine, scenario)
    assert _story(after, b).selected_scope_fingerprint != before

    with Session(engine) as session:
        anchors = infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(b,)
        )
    assert len(anchors) == 1
    assert anchors[0].replacement_story_artifact_id == successor_id
    assert _events(engine) == ()
    assert not _story(after, b).dependency_safe
    assert (
        _inspect(engine, scenario)["selected_scope_fingerprint"]
        == _story(after, b).selected_scope_fingerprint
    )
    assert (
        planning_decision(
            _domain(engine).position(scenario.project_id), "planning.story_dependencies"
        ).category
        is NodeCategory.AVAILABLE
    )
    assert (
        _domain(engine)
        .transition(_request(engine, scenario, b, (), "legacy-draft-chain-renewal"))
        .ok
    )
    renewed = _snapshot(engine, scenario)
    assert (
        _story(renewed, b).selected_scope_fingerprint
        == _story(after, b).selected_scope_fingerprint
    )
    assert _story(renewed, b).dependency_safe


def test_legacy_unidentifiable_successor_requires_review_without_fact_load_failure(
    engine: Engine,
) -> None:
    """Retain a source-only legacy revision when no successor can be proven."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    before = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    with Session(engine) as session:
        # An otherwise loadable legacy source carries supersession without a
        # discoverable descendant. This is compatibility evidence, not the
        # main acceptance reproduction.
        old = session.get_one(UserStory, a)
        old.is_superseded = True
        session.add(old)
        session.commit()
    after = _snapshot(engine, scenario)
    assert _story(after, b).selected_scope_fingerprint != before

    with Session(engine) as session:
        anchors = infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(b,)
        )
    assert len(anchors) == 1
    assert (anchors[0].dependent_story_id, anchors[0].superseded_story_id) == (b, a)
    assert anchors[0].replacement_story_artifact_id is None
    assert _events(engine) == ()


def test_replacement_invalidation_revision_is_order_independent_and_cycle_bounded(
    engine: Engine,
) -> None:
    """Canonicalize identity unions and retain deterministic simple paths in cycles."""
    scenario = _seed(engine, 4)
    a, b, x, y = scenario.story_ids
    for dependent, prerequisite in ((b, x), (b, y), (x, a), (y, a), (x, y), (y, x)):
        _edge(engine, scenario, dependent, prerequisite)
    select_for_sprint(engine, b)
    before = _snapshot(engine, scenario)
    base = _story(before, b).selected_scope_fingerprint
    assert base is not None
    anchor = StoryDependencyInvalidationAnchor(
        dependent_story_id=b,
        superseded_story_id=a,
        superseded_story_artifact_id=scenario.artifact_ids[0],
        superseded_story_artifact_fingerprint=_story(
            before, a
        ).source_story_artifact_fingerprint,
    )
    irrelevant = anchor.model_copy(update={"dependent_story_id": x})
    token = selected_scope_dependency_lifecycle_fingerprint(
        selected_scope_fingerprint=base,
        selected_story_ids=(b,),
        invalidation_anchors=(anchor, irrelevant),
    )
    assert token != base
    assert (
        selected_scope_dependency_lifecycle_fingerprint(
            selected_scope_fingerprint=base,
            selected_story_ids=(),
            invalidation_anchors=(anchor,),
        )
        == base
    )
    assert (
        selected_scope_dependency_lifecycle_fingerprint(
            selected_scope_fingerprint=base,
            selected_story_ids=(b,),
            invalidation_anchors=(irrelevant,),
        )
        == base
    )
    assert token == canonical_hash(
        {
            "schema_version": "agileforge.story-selected-scope.v2",
            "selected_story_scope_fingerprint": base,
            "invalidation_anchors": [
                {
                    "dependent_story_id": b,
                    "superseded_story_id": a,
                    "superseded_story_artifact_id": scenario.artifact_ids[0],
                    "superseded_story_artifact_fingerprint": (
                        anchor.superseded_story_artifact_fingerprint
                    ),
                }
            ],
        }
    )
    assert (
        selected_scope_dependency_lifecycle_fingerprint(
            selected_scope_fingerprint=base,
            selected_story_ids=(b,),
            invalidation_anchors=(irrelevant, anchor, anchor),
        )
        == token
    )
    _replace(engine, scenario.artifact_ids[0])
    events = _events(engine)
    assert len(events) == 1
    metadata = json.loads(events[0].event_metadata or "{}")
    assert {
        (item["dependent_story_id"], item["superseded_story_id"])
        for item in metadata["anchors"]
    } == {(b, a), (x, a), (y, a)}
    for path in metadata["causal_paths"]:
        row_ids = [edge["dependent_story_id"] for edge in path["dependency_rows"]]
        assert len(row_ids) == len(set(row_ids))
    after = _snapshot(engine, scenario)
    assert current_dependency_closure(
        stories=after.stories,
        dependencies=after.story_dependencies,
        root_story_ids=(b,),
    ) == current_dependency_closure(
        stories=tuple(reversed(after.stories)),
        dependencies=tuple(reversed(after.story_dependencies)),
        root_story_ids=(b,),
    )


def test_source_only_anchor_successor_diagnostics_do_not_revise_scope(
    engine: Engine,
) -> None:
    """Enrich diagnostics without changing identity, token, or event history."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    _review(engine, scenario, b)
    with Session(engine) as session:
        source = session.get_one(UserStory, a)
        source.is_superseded = True
        session.add(source)
        session.commit()
        implicit = infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(b,)
        )
        assert len(implicit) == 1
        assert implicit[0].replacement_story_artifact_id is None
        adopt_story_dependency_invalidations_in_session(
            session,
            project_id=scenario.project_id,
            anchors=implicit,
            reviewed_at=EVALUATED_AT,
        )
        session.commit()
        persisted = load_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id
        )
        assert persisted == implicit
        adopt_story_dependency_invalidations_in_session(
            session,
            project_id=scenario.project_id,
            anchors=implicit,
            reviewed_at=EVALUATED_AT,
        )
        session.commit()
    before = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    events = _events(engine)
    assert len(events) == 1
    metadata_before = events[0].event_metadata
    replacement_id, _ = _replace(engine, scenario.artifact_ids[0])
    after = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    assert after == before
    assert len(_events(engine)) == 1
    assert _events(engine)[0].event_metadata == metadata_before
    with Session(engine) as session:
        enriched = infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(b,)
        )
    assert len(enriched) == 1
    assert enriched[0].replacement_story_artifact_id == replacement_id
    diagnostics = {
        "replacement_story_artifact_id",
        "replacement_story_artifact_fingerprint",
    }
    assert enriched[0].model_dump(exclude=diagnostics) == implicit[0].model_dump(
        exclude=diagnostics
    )


def test_no_superseded_endpoint_skips_inference_lineage_work(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip inference-specific artifact proof for unaffected current closures."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    _edge(engine, scenario, b, a)
    _edge(engine, scenario, b, x, status="proposed")
    _review(engine, scenario, b)
    before = _snapshot(engine, scenario)

    def forbidden_lineage(*_args: object, **_kwargs: object) -> None:
        pytest.fail(
            "Normal current closure must skip inference-specific lineage queries"  # ty: ignore[invalid-argument-type]
        )

    monkeypatch.setattr(
        story_dependency_lifecycle, "_build_inference_lineage", forbidden_lineage
    )
    assert _snapshot(engine, scenario) == before
    with Session(engine) as session:
        assert (
            story_dependency_lifecycle.infer_unrecorded_story_dependency_invalidations_in_session(
                session, project_id=scenario.project_id, root_story_ids=(b,)
            )
            == ()
        )


def test_direct_superseded_terminal_prerequisite_still_revises_scope(
    engine: Engine,
) -> None:
    """Classify a direct obsolete endpoint before its former completion status."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    with Session(engine) as session:
        old = session.get_one(UserStory, a)
        old.status = StoryStatus.DONE
        session.add(old)
        session.commit()
    _review(engine, scenario, b)
    before = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    _replace(engine, scenario.artifact_ids[0])
    after = _snapshot(engine, scenario)
    assert [
        (edge.dependent_story_id, edge.prerequisite_story_id)
        for edge in superseded_dependency_edges(
            stories=after.stories,
            dependencies=after.story_dependencies,
            root_story_ids=(b,),
        )
    ] == [(b, a)]
    assert _story(after, b).selected_scope_fingerprint != before


def test_invalidation_anchor_requires_paired_successor_diagnostics() -> None:
    """Reject partial accepted-successor proof rather than treating it as absent."""
    with pytest.raises(ValidationError):
        StoryDependencyInvalidationAnchor(
            dependent_story_id=2,
            superseded_story_id=1,
            superseded_story_artifact_id=10,
            superseded_story_artifact_fingerprint="sha256:" + "a" * 64,
            replacement_story_artifact_id=20,
        )


def test_prospective_revision_rejects_obsolete_authority_behind_new_edge(
    engine: Engine,
) -> None:
    """Reject a newly approved edge reaching actionable obsolete proposed authority."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    _edge(engine, scenario, x, a, status="proposed")
    _replace(engine, scenario.artifact_ids[0])
    with Session(engine) as session, pytest.raises(ValueError, match="superseded"):
        validate_reviewed_dependency_lifecycle_in_session(
            session,
            project_id=scenario.project_id,
            selected_story_ids=(b,),
            reviewed_edges=(
                StoryDependencyReviewEdgeFact(
                    dependent_story_id=b,
                    prerequisite_story_id=x,
                    reason="Explicit new authority",
                ),
            ),
        )


def test_persisted_invalidation_revision_survives_reconciliation_and_completion(
    engine: Engine,
) -> None:
    """Validate saved paths independently of later edge state and completion."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    _edge(engine, scenario, b, x)
    _edge(engine, scenario, x, a)
    _review(engine, scenario, b)
    _replace(engine, scenario.artifact_ids[0])
    events = _events(engine)
    assert len(events) == 1
    before = _story(_snapshot(engine, scenario), b).selected_scope_fingerprint
    with Session(engine) as session:
        anchors = load_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id
        )
        for edge in session.exec(select(UserStoryDependency)).all():
            edge.status = "rejected"
            edge.reason = "Later reconciliation retained historical endpoint identity."
            session.add(edge)
        terminal = session.get_one(UserStory, x)
        terminal.status = StoryStatus.DONE
        session.add(terminal)
        session.commit()
        assert (
            load_story_dependency_invalidations_in_session(
                session, project_id=scenario.project_id
            )
            == anchors
        )
    assert _story(_snapshot(engine, scenario), b).selected_scope_fingerprint == before
    assert _events(engine)[0].event_metadata == events[0].event_metadata


def test_review_adoption_invalidation_preserves_each_unselected_live_ancestor(
    engine: Engine,
) -> None:
    """Adopt every live reverse ancestor before external reconciliation erases paths."""
    scenario = _seed(engine)
    a, b, x = scenario.story_ids
    _edge(engine, scenario, b, x)
    _edge(engine, scenario, x, a)
    with Session(engine) as session:
        source = session.get_one(UserStory, a)
        source.is_superseded = True
        session.add(source)
        session.commit()
        anchors = infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(x,)
        )
        assert len(anchors) == 1
        adopt_story_dependency_invalidations_in_session(
            session,
            project_id=scenario.project_id,
            anchors=anchors,
            reviewed_at=EVALUATED_AT,
        )
        session.commit()
        persisted = load_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id
        )
    assert {
        (anchor.dependent_story_id, anchor.superseded_story_id) for anchor in persisted
    } == {(b, a), (x, a)}


def test_current_schema_invalidation_enum_round_trip_preserves_manifest(
    engine: Engine,
) -> None:
    """Round trip the new event on existing and fresh schemas without migration."""
    assert _sqlmodel_business_schema_manifest() == CURRENT_BUSINESS_SCHEMA_MANIFEST
    _assert_current_business_schema(engine)
    with Session(engine) as session:
        session.add(WorkflowEvent(event_type=WorkflowEventType.STORY_DEPENDENCY_STALE))
        session.commit()
        assert (
            session.exec(select(WorkflowEvent)).one().event_type
            is WorkflowEventType.STORY_DEPENDENCY_STALE
        )
    fresh = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        ensure_business_db_ready(fresh)
        with Session(fresh) as session:
            session.add(
                WorkflowEvent(event_type=WorkflowEventType.STORY_DEPENDENCY_STALE)
            )
            session.commit()
            assert (
                session.exec(select(WorkflowEvent)).one().event_type
                is WorkflowEventType.STORY_DEPENDENCY_STALE
            )
        _assert_current_business_schema(fresh)
    finally:
        fresh.dispose()


def test_malformed_legacy_invalidation_identity_is_integrity_error(
    engine: Engine,
) -> None:
    """Classify malformed old Story identity as corruption."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    with Session(engine) as session:
        source = session.get_one(UserStory, a)
        source.story_id = -a
        source.is_superseded = True
        session.add(source)
        session.commit()
    _edge(engine, scenario, b, -a)
    with (
        Session(engine) as session,
        pytest.raises(StoryDependencyLifecycleIntegrityError),
    ):
        infer_unrecorded_story_dependency_invalidations_in_session(
            session,
            project_id=scenario.project_id,
            root_story_ids=(b,),
        )


def test_malformed_legacy_invalidation_artifact_identity_is_integrity_error(
    engine: Engine,
) -> None:
    """Classify corrupted loaded artifact identity as lifecycle corruption."""
    scenario = _seed(engine)
    a, b, _ = scenario.story_ids
    _edge(engine, scenario, b, a)
    source_id = scenario.artifact_ids[0]
    with Session(engine) as session, session.no_autoflush:
        # Keep the loaded proof internally consistent without flushing corrupt
        # identities through database foreign keys. The read-side helper must
        # still reject malformed exact identity as lifecycle corruption.
        artifact = session.get_one(StoryArtifact, source_id)
        artifact.story_artifact_id = -source_id
        session.add(artifact)
        decision = session.exec(
            select(StoryArtifactDecision).where(
                StoryArtifactDecision.story_artifact_id == source_id
            )
        ).one()
        decision.story_artifact_id = -source_id
        session.add(decision)
        source = session.get_one(UserStory, a)
        source.source_story_artifact_id = -source_id
        source.is_superseded = True
        session.add(source)
        with pytest.raises(
            (ValidationError, StoryDependencyLifecycleIntegrityError)
        ) as raised:
            infer_unrecorded_story_dependency_invalidations_in_session(
                session,
                project_id=scenario.project_id,
                root_story_ids=(b,),
            )
        assert isinstance(raised.value, StoryDependencyLifecycleIntegrityError)


@dataclass(frozen=True)
class _DependencySprint:
    scenario: _Scenario
    sprint_id: int
    story_id: int
    task_id: int
    plan_id: int
    review_source: str


def _accepted_dependency_sprint(
    engine: Engine, *, started: bool, story_count: int = 2
) -> _DependencySprint:
    """Seed completed external A, then review and accept B's exact plan publicly."""
    scenario = _seed(engine, count=story_count)
    prerequisite, dependent = scenario.story_ids[:2]
    with Session(engine) as session:
        external = session.get_one(UserStory, prerequisite)
        external.status = StoryStatus.DONE
        session.add(external)
        session.commit()
    _edge(engine, scenario, dependent, prerequisite)
    domain = _domain(engine)
    plan_id, _, _, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        scenario.project_id,
        dependent,
        team_name="Prerequisite replacement preservation team",
        idempotency_key="preservation-dependent-plan",
        assert_empty_execution=False,
    )
    accepted = domain.transition(
        DecideSprintPlan(
            **planning_guards(
                domain.position(scenario.project_id), "planning.sprint.review"
            ),
            idempotency_key="preservation-dependent-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Accept the reviewed completed external prerequisite.",
        )
    )
    assert accepted.ok
    sprint_id = accepted.output["activated_sprint_id"]
    assert isinstance(sprint_id, int)
    if started:
        result = domain.transition(
            StartSprint(
                **planning_guards(
                    domain.position(scenario.project_id), "planning.sprint.start"
                ),
                idempotency_key="preservation-dependent-start",
            )
        )
        assert result.ok
        assert result.output["sprint_id"] == sprint_id
        unbind_synthetic_execution_repository(engine, scenario.project_id)
    with Session(engine) as session:
        task = session.exec(select(Task).where(Task.story_id == dependent)).one()
        assert task.task_id is not None
        review_source = _story(
            _snapshot(engine, scenario), dependent
        ).selected_scope_fingerprint
        assert review_source is not None
        return _DependencySprint(
            scenario, sprint_id, dependent, task.task_id, plan_id, review_source
        )


def _project_business_rows(
    engine: Engine, project_id: int
) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Compare stored project-owned values, including JSON, excluding receipts."""
    story_ids = select(UserStory.story_id).where(UserStory.project_id == project_id)
    sprint_ids = select(Sprint.sprint_id).where(Sprint.project_id == project_id)
    task_ids = select(Task.task_id).where(col(Task.story_id).in_(story_ids))
    team_ids = select(Sprint.team_id).where(Sprint.project_id == project_id)
    ownership = {
        "project_id": (project_id,),
        "sprint_id": sprint_ids,
        "story_id": story_ids,
        "task_id": task_ids,
        "team_id": team_ids,
    }
    with Session(engine) as session:
        rows: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in SQLModel.metadata.sorted_tables:
            if table.name == WorkflowTransitionReceipt.__tablename__:
                continue
            for column, identities in ownership.items():
                if column in table.c:
                    statement = Select(table).where(table.c[column].in_(identities))
                    statement = statement.order_by(*table.primary_key.columns)
                    rows[table.name] = tuple(
                        tuple(row) for row in session.exec(statement).all()
                    )
                    break
        return rows


def _stored_sprint_evidence(
    engine: Engine, fixture: _DependencySprint
) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Retain exact rows for this Sprint and its pinned dependency review."""
    project_rows = _project_business_rows(engine, fixture.scenario.project_id)
    retained: dict[str, tuple[tuple[object, ...], ...]] = {}
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in project_rows:
            continue
        columns = tuple(table.c.keys())
        selector: tuple[str, object] | None = None
        if "sprint_id" in columns:
            selector = ("sprint_id", fixture.sprint_id)
        elif table.name in {
            SprintPlanArtifact.__tablename__,
            SprintPlanArtifactDecision.__tablename__,
        }:
            selector = ("sprint_plan_artifact_id", fixture.plan_id)
        elif table.name == StoryDependencyReview.__tablename__:
            selector = ("source_fingerprint", fixture.review_source)
        elif table.name == Task.__tablename__:
            selector = ("task_id", fixture.task_id)
        if selector is not None:
            column, expected = selector
            ordinal = columns.index(column)
            retained[table.name] = tuple(
                row for row in project_rows[table.name] if row[ordinal] == expected
            )
    return retained


def _rejection_receipt(
    engine: Engine,
    request: ApplyStoryDependencies | RecordSprintPlan | DecideSprintPlan | StartSprint,
) -> dict[str, object]:
    with Session(engine) as session:
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.request_kind == request.kind,
                WorkflowTransitionReceipt.idempotency_key == request.idempotency_key,
            )
        ).all()
        assert len(receipts) == 1
        assert json.loads(receipts[0].request_json)["project_id"] == request.project_id
        return receipts[0].model_dump(mode="json")


@pytest.mark.parametrize("stage", ["draft", "review", "start"])
def test_full_project_cycle_prevents_public_sprint_planning_without_writes(  # noqa: PLR0915
    engine: Engine, stage: str
) -> None:
    """Refuse an execution-blocking history cycle even with unchanged plan facts."""
    scenario = _seed(engine, count=4)
    prerequisite, b, x, y = scenario.story_ids
    with Session(engine) as session:
        for story_id in (prerequisite, x):
            external = session.get_one(UserStory, story_id)
            external.status = StoryStatus.DONE
            session.add(external)
        session.commit()
    _edge(engine, scenario, b, prerequisite)
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )
    if stage == "draft":
        _review(engine, scenario, b)
        plan_id = None
        plan_fingerprint = None
        sprint_id = None
    else:
        plan_id, _, _, plan_fingerprint = _record_sprint_plan_draft(
            engine,
            domain,
            scenario.project_id,
            b,
            team_name="Full cycle barrier team",
            idempotency_key="full-cycle-barrier-plan",
        )
        sprint_id = None
        if stage == "start":
            accepted = domain.transition(
                DecideSprintPlan(
                    **planning_guards(
                        domain.position(scenario.project_id), "planning.sprint.review"
                    ),
                    idempotency_key="full-cycle-barrier-accept",
                    sprint_plan_artifact_id=plan_id,
                    plan_fingerprint=plan_fingerprint,
                    decision="accepted",
                    rationale="Accept current acyclic Project dependencies.",
                )
            )
            assert accepted.ok
            sprint_id = accepted.output["activated_sprint_id"]
            assert isinstance(sprint_id, int)
    before = _snapshot(engine, scenario)
    selected_before = _story(before, b)
    candidate_before = candidate_set_fingerprint(
        tuple(story for story in before.stories if story.sprint_candidate),
        before.story_dependencies,
    )
    _edge(engine, scenario, x, y)
    _edge(engine, scenario, y, x)
    current = _snapshot(engine, scenario)
    assert _story(current, b) == selected_before
    assert _story(current, b).dependency_safe
    assert current.story_dependency_reviews == before.story_dependency_reviews
    assert _inspect(engine, scenario)["issues"] == []
    assert (
        candidate_set_fingerprint(
            tuple(story for story in current.stories if story.sprint_candidate),
            current.story_dependencies,
        )
        == candidate_before
    )
    node_id = {
        "draft": "planning.sprint.plan",
        "review": "planning.sprint.review",
        "start": "planning.sprint.start",
    }[stage]
    position = domain.position(scenario.project_id)
    decision = planning_decision(position, node_id)
    assert decision.category is NodeCategory.INVALID
    assert decision.reason_code == "STORY_DEPENDENCY_CYCLE"
    assert any(
        blocker.code == "STORY_DEPENDENCY_CYCLE" and blocker.message.strip()
        for blocker in decision.blockers
    )
    request: RecordSprintPlan | DecideSprintPlan | StartSprint
    if stage == "draft":
        assert selected_before.source_story_item_id is not None
        assert selected_before.accepted_spec_version_id is not None
        assert selected_before.accepted_spec_hash is not None
        request = RecordSprintPlan(
            **planning_guards(position, node_id),
            idempotency_key="reject-full-cycle-draft",
            team_name="Full cycle barrier team",
            spec_version_id=selected_before.accepted_spec_version_id,
            spec_hash=selected_before.accepted_spec_hash,
            planner_output=SprintPlannerOutput.model_validate(
                _sprint_plan(
                    b,
                    story_item_id=selected_before.source_story_item_id,
                    spec_item_ids=selected_before.spec_item_ids,
                )
            ),
        )
    elif stage == "review":
        assert plan_id is not None
        assert plan_fingerprint is not None
        request = DecideSprintPlan(
            **planning_guards(position, node_id),
            idempotency_key="reject-full-cycle-review",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Attempt acceptance with unrelated cyclic authority.",
        )
    else:
        request = StartSprint(
            **planning_guards(position, node_id),
            idempotency_key="reject-full-cycle-start",
        )
    business_before = _project_business_rows(engine, scenario.project_id)
    refused = domain.transition(request)
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert _project_business_rows(engine, scenario.project_id) == business_before
    receipt = _rejection_receipt(engine, request)
    replay = domain.transition(request)
    assert not replay.ok
    assert replay.replayed
    assert replay.error == refused.error
    assert _rejection_receipt(engine, request) == receipt
    assert _project_business_rows(engine, scenario.project_id) == business_before
    if sprint_id is not None:
        with Session(engine) as session:
            assert session.get_one(Sprint, sprint_id).status is SprintStatus.PLANNED
            assert (
                session.exec(
                    select(SprintStart).where(SprintStart.sprint_id == sprint_id)
                ).all()
                == []
            )
        locked = domain.transition(
            _request(engine, scenario, b, (prerequisite,), "full-cycle-lock-retained")
        )
        assert not locked.ok
        assert locked.error is not None
        assert locked.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE
        assert {blocker.code for blocker in locked.error.blockers} == {
            "SPRINT_DEPENDENCY_REVIEW_LIFECYCLE_LOCKED"
        }
        assert _project_business_rows(engine, scenario.project_id) == business_before


@pytest.mark.parametrize("started", [False, True], ids=["accepted-planned", "started"])
def test_accepted_and_started_sprint_evidence_survives_prerequisite_replacement(
    engine: Engine, started: bool
) -> None:
    """Keep accepted evidence and pinned start authority after A's replacement."""
    fixture = _accepted_dependency_sprint(engine, started=started)
    before = _snapshot(engine, fixture.scenario)
    evidence_before = _stored_sprint_evidence(engine, fixture)
    contract_before = (
        execution_contract(before, fixture.sprint_id).fingerprint if started else None
    )

    _replace(engine, fixture.scenario.artifact_ids[0])

    after = _snapshot(engine, fixture.scenario)
    assert _stored_sprint_evidence(engine, fixture) == evidence_before
    assert (
        _story(after, fixture.story_id).selected_scope_fingerprint
        != fixture.review_source
    )
    assert after.story_dependency_reviews == before.story_dependency_reviews
    starts = tuple(
        item for item in after.sprint_starts if item.sprint_id == fixture.sprint_id
    )
    if started:
        old_starts = tuple(
            item for item in before.sprint_starts if item.sprint_id == fixture.sprint_id
        )
        assert starts == old_starts
        assert (
            execution_contract(after, fixture.sprint_id).fingerprint == contract_before
        )
        assert starts[0].dependency_source_fingerprint == fixture.review_source
        assert (
            starts[0].dependency_rows_snapshot == old_starts[0].dependency_rows_snapshot
        )
    else:
        assert starts == ()
        with Session(engine) as session:
            assert (
                session.get_one(Sprint, fixture.sprint_id).status
                is SprintStatus.PLANNED
            )


def test_stale_accepted_planned_sprint_start_rejects_without_writes(  # noqa: PLR0915
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse stale acceptance with one receipt and no domain evidence writes."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    scenario = fixture.scenario
    with Session(engine) as session:
        accepted_candidate = session.get_one(
            SprintPlanArtifact, fixture.plan_id
        ).candidate_set_fingerprint
    evidence_before = _stored_sprint_evidence(engine, fixture)
    _replace(engine, scenario.artifact_ids[0])
    snapshot = _snapshot(engine, scenario)
    current_source = _story(snapshot, fixture.story_id).selected_scope_fingerprint
    assert current_source != fixture.review_source
    assert (
        candidate_set_fingerprint(
            tuple(item for item in snapshot.stories if item.sprint_candidate),
            snapshot.story_dependencies,
        )
        != accepted_candidate
    )
    domain = _domain(engine)
    payload = workflow_next(
        application=AgileForgeApplication(workflow_domain=domain),
        project_id=scenario.project_id,
    )
    assert {
        item["node_id"]: item["reason_code"] for item in payload["invalid_node_reasons"]
    }["planning.sprint.plan"] == "SPRINT_PLAN_STALE"
    request_guards = planning_guards(
        domain.position(scenario.project_id), "planning.sprint.start"
    )
    request_guards["actor"] = "replacement-start-operator"
    request = StartSprint(
        **request_guards, idempotency_key="reject-stale-preservation-start"
    )
    business_before = _project_business_rows(engine, scenario.project_id)
    refused = domain.transition(request)
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert not refused.replayed
    assert _project_business_rows(engine, scenario.project_id) == business_before
    assert _stored_sprint_evidence(engine, fixture) == evidence_before
    receipt = _rejection_receipt(engine, request)
    replay = domain.transition(request)
    assert not replay.ok
    assert replay.replayed
    assert replay.error == refused.error
    assert replay.output == refused.output
    assert _rejection_receipt(engine, request) == receipt
    assert _project_business_rows(engine, scenario.project_id) == business_before
    with Session(engine) as session:
        assert session.get_one(Sprint, fixture.sprint_id).status is SprintStatus.PLANNED
        assert (
            session.exec(
                select(SprintStart).where(SprintStart.sprint_id == fixture.sprint_id)
            ).all()
            == []
        )
        assert (
            session.exec(
                select(StoryDependencyReview).where(
                    StoryDependencyReview.project_id == scenario.project_id,
                    StoryDependencyReview.source_fingerprint == current_source,
                )
            ).all()
            == []
        )
        assert (
            session.exec(
                select(StoryDependencyReview).where(
                    StoryDependencyReview.project_id == scenario.project_id,
                    StoryDependencyReview.reviewed_by == request.actor,
                )
            ).all()
            == []
        )
        plan_fact = next(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
            and item.artifact_id == fixture.plan_id
        )
        assert plan_fact.task_content_fingerprint is not None
        review_lookup_calls: list[object] = []

        def unexpected_review_lookup(*args: object, **kwargs: object) -> None:
            review_lookup_calls.append((args, kwargs))
            pytest.fail(
                "A stale candidate set must not resolve or create a review."  # ty: ignore[invalid-argument-type]
            )

        monkeypatch.setattr(
            sprint_phase, "_selected_dependency_review_id", unexpected_review_lookup
        )
        with pytest.raises(ValueError) as rejected:  # noqa: PT011
            start_sprint_in_session(
                session,
                SprintStartInput(
                    project_id=scenario.project_id,
                    expected_sprint_id=fixture.sprint_id,
                    expected_task_content_fingerprint=plan_fact.task_content_fingerprint,
                    decision_fingerprint=request.decision_fingerprint,
                    started_by=request.actor,
                    started_at=EVALUATED_AT,
                ),
            )
        assert rejected.value.args == (
            "Accepted Sprint plan candidate set changed before start.",
        )
        assert review_lookup_calls == []
        assert not session.new
        assert not session.dirty
        assert not session.deleted
        assert _project_business_rows(engine, scenario.project_id) == business_before
    locked = planning_decision(
        domain.position(scenario.project_id), "planning.story_dependencies"
    )
    assert (locked.category, locked.reason_code) == (
        NodeCategory.BLOCKED,
        "SPRINT_DEPENDENCY_REVIEW_LIFECYCLE_LOCKED",
    )


def _execution_checkpoint(
    engine: Engine, fixture: _DependencySprint
) -> tuple[tuple[str, NodeCategory, str, tuple[str, ...]], ...]:
    targets = {
        "execution.task.complete": f"task:{fixture.task_id}",
        "execution.story.close": f"story:{fixture.story_id}",
        "execution.sprint.review": f"sprint:{fixture.sprint_id}",
        "execution.sprint.close": f"sprint:{fixture.sprint_id}",
        "execution.post_sprint_triage": f"sprint:{fixture.sprint_id}",
    }
    position = execution_domain(engine).position(fixture.scenario.project_id)
    return tuple(
        (
            item.node_id,
            item.category,
            item.reason_code,
            tuple(blocker.code for blocker in item.blockers),
        )
        for item in position.decisions
        if targets.get(item.node_id) == item.instance_key
    )


@pytest.mark.parametrize(
    ("status", "satisfied"),
    [
        (StoryStatus.DONE, True),
        (StoryStatus.ACCEPTED, True),
        (StoryStatus.TO_DO, False),
        (StoryStatus.IN_PROGRESS, False),
    ],
)
def test_started_task_dependency_fact_tracks_direct_external_completion(
    engine: Engine, status: StoryStatus, satisfied: bool
) -> None:
    """Keep nonterminal external prerequisites blocked without fact conflicts."""
    fixture = _accepted_dependency_sprint(engine, started=True)
    with Session(engine) as session:
        prerequisite = session.get_one(UserStory, fixture.scenario.story_ids[0])
        prerequisite.status = status
        session.add(prerequisite)
        session.commit()
    snapshot = _snapshot(engine, fixture.scenario)
    task = next(item for item in snapshot.tasks if item.task_id == fixture.task_id)
    assert task.dependencies_satisfied is satisfied
    decision = next(
        item
        for item in execution_domain(engine)
        .position(fixture.scenario.project_id)
        .decisions
        if item.node_id == "execution.task.complete"
        and item.instance_key == f"task:{fixture.task_id}"
    )
    assert (decision.category, decision.reason_code) == (
        (NodeCategory.AVAILABLE, "NEXT_TASK_READY")
        if satisfied
        else (NodeCategory.BLOCKED, "TASK_DEPENDENCY_BLOCKED")
    )


@pytest.mark.parametrize("status", [StoryStatus.TO_DO, StoryStatus.IN_PROGRESS])
def test_done_task_completion_survives_current_prerequisite_regression(
    engine: Engine, status: StoryStatus
) -> None:
    """Preserve accepted completion without treating its prerequisite as ready."""
    fixture = _accepted_dependency_sprint(engine, started=True)
    domain = execution_domain(engine)
    assert domain.transition(
        _complete_task(domain, fixture.scenario.project_id, fixture.task_id)
    ).ok
    completed = _snapshot(engine, fixture.scenario)
    original_task = next(
        item for item in completed.tasks if item.task_id == fixture.task_id
    )
    original_completion = next(
        item for item in completed.task_completions if item.task_id == fixture.task_id
    )
    original_contract = execution_contract(completed, fixture.sprint_id)
    original_evidence = _stored_sprint_evidence(engine, fixture)
    original_close = planning_decision(
        domain.position(fixture.scenario.project_id),
        "execution.story.close",
        f"story:{fixture.story_id}",
    )
    assert (original_close.category, original_close.reason_code) == (
        NodeCategory.AVAILABLE,
        "STORY_READY_TO_CLOSE",
    )

    with Session(engine) as session:
        prerequisite = session.get_one(UserStory, fixture.scenario.story_ids[0])
        prerequisite.status = status
        session.add(prerequisite)
        session.commit()

    snapshot = _snapshot(engine, fixture.scenario)
    task = next(item for item in snapshot.tasks if item.task_id == fixture.task_id)
    completion = next(
        item for item in snapshot.task_completions if item.task_id == fixture.task_id
    )
    assert task.status == "Done"
    assert task.dependencies_satisfied is True
    story = _story(snapshot, fixture.story_id)
    assert not story.dependency_safe
    assert (
        f"PREREQUISITE_STORY_{fixture.scenario.story_ids[0]}_INCOMPLETE"
        in story.readiness_blockers
    )
    assert task == original_task
    assert completion == original_completion
    assert _stored_sprint_evidence(engine, fixture) == original_evidence
    contract = execution_contract(snapshot, fixture.sprint_id)
    assert contract.fingerprint == original_contract.fingerprint
    assert contract.start == original_contract.start
    assert (
        task_evidence_fingerprint(
            snapshot,
            task,
            evidence=TaskEvidencePayload(
                outcome_summary=completion.outcome_summary,
                artifact_refs=completion.artifact_refs,
                acceptance_result=completion.acceptance_result,
                checklist_result=completion.checklist_result,
                repository_evidence=completion.repository_evidence,
            ),
        )
        == original_completion.evidence_fingerprint
    )
    position = domain.position(fixture.scenario.project_id)
    assert "TASK_DEPENDENCY_FACT_CONFLICT" not in {
        item.reason_code for item in position.decisions
    }
    task_node = next(
        node
        for node in execution_graph().root.iter_nodes()
        if node.node_id == "execution.task.complete"
    )
    (evaluation,) = task_node.evaluate_rule(snapshot, EVALUATED_AT)
    assert (evaluation.category, evaluation.reason_code, evaluation.instance_key) == (
        RuleCategory.SATISFIED,
        "ALL_TASKS_TERMINAL",
        None,
    )
    assert not any(
        item.node_id == "execution.task.complete"
        and item.category is NodeCategory.AVAILABLE
        for item in position.decisions
    )
    close = planning_decision(
        position, "execution.story.close", f"story:{fixture.story_id}"
    )
    assert (close.category, close.reason_code, close.fact_references) == (
        original_close.category,
        original_close.reason_code,
        original_close.fact_references,
    )


@pytest.mark.parametrize(
    ("status", "satisfied"),
    [(StoryStatus.DONE, True), (StoryStatus.TO_DO, False)],
)
def test_cancelled_task_retains_current_prerequisite_completion_checks(
    engine: Engine, status: StoryStatus, satisfied: bool
) -> None:
    """Cancellation does not grant the immutable Done completion fact."""
    fixture = _accepted_dependency_sprint(engine, started=True)
    with Session(engine) as session:
        prerequisite = session.get_one(UserStory, fixture.scenario.story_ids[0])
        prerequisite.status = status
        task = session.get_one(Task, fixture.task_id)
        task.status = TaskStatus.CANCELLED
        session.add_all((prerequisite, task))
        session.commit()
    snapshot = _snapshot(engine, fixture.scenario)
    task_fact = next(item for item in snapshot.tasks if item.task_id == fixture.task_id)
    assert task_fact.dependencies_satisfied is satisfied
    assert "TASK_DEPENDENCY_FACT_CONFLICT" not in {
        item.reason_code
        for item in execution_domain(engine)
        .position(fixture.scenario.project_id)
        .decisions
    }


def test_done_task_still_requires_durable_completion_evidence(engine: Engine) -> None:
    """A synthetic Done status without accepted completion remains invalid."""
    fixture = _accepted_dependency_sprint(engine, started=True)
    with Session(engine) as session:
        task = session.get_one(Task, fixture.task_id)
        task.status = TaskStatus.DONE
        session.add(task)
        session.commit()
    snapshot = _snapshot(engine, fixture.scenario)
    assert not any(
        item.task_id == fixture.task_id for item in snapshot.task_completions
    )
    decision = planning_decision(
        execution_domain(engine).position(fixture.scenario.project_id),
        "execution.task.complete",
        f"task:{fixture.task_id}",
    )
    assert (decision.category, decision.reason_code) == (
        NodeCategory.INVALID,
        "TASK_COMPLETION_EVIDENCE_MISSING",
    )


def _complete_and_close_at_checkpoints(
    engine: Engine, fixture: _DependencySprint
) -> tuple[tuple[tuple[str, NodeCategory, str, tuple[str, ...]], ...], ...]:
    """Use fresh public guards and expose each successive execution checkpoint."""
    domain = execution_domain(engine)
    project_id = fixture.scenario.project_id
    checkpoints = [_execution_checkpoint(engine, fixture)]
    assert domain.transition(_complete_task(domain, project_id, fixture.task_id)).ok
    checkpoints.append(_execution_checkpoint(engine, fixture))
    assert domain.transition(
        CloseStory(
            **execution_guards(
                domain, project_id, "execution.story.close", f"story:{fixture.story_id}"
            ),
            instance_key=f"story:{fixture.story_id}",
            idempotency_key="preservation-close-story",
            story_id=fixture.story_id,
            resolution="Completed",
            delivered="Reviewed work delivered.",
            evidence="Focused verification passed.",
            known_gaps="None.",
        )
    ).ok
    checkpoints.append(_execution_checkpoint(engine, fixture))
    review = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "execution.sprint.review"
        and item.instance_key == f"sprint:{fixture.sprint_id}"
    )
    review_fingerprint = next(
        item.fingerprint
        for item in review.fact_references
        if item.fact_type == "sprint_review"
    )
    assert domain.transition(
        ReviewSprint(
            **execution_guards(
                domain,
                project_id,
                "execution.sprint.review",
                f"sprint:{fixture.sprint_id}",
            ),
            instance_key=f"sprint:{fixture.sprint_id}",
            idempotency_key="preservation-review-sprint",
            sprint_id=fixture.sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok
    checkpoints.append(_execution_checkpoint(engine, fixture))
    assert domain.transition(
        CloseSprint(
            **execution_guards(
                domain,
                project_id,
                "execution.sprint.close",
                f"sprint:{fixture.sprint_id}",
            ),
            instance_key=f"sprint:{fixture.sprint_id}",
            idempotency_key="preservation-close-sprint",
            sprint_id=fixture.sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok
    checkpoints.append(_execution_checkpoint(engine, fixture))
    return tuple(checkpoints)


def test_active_sprint_prerequisite_replacement_preserves_execution_close_and_retry_eligibility(  # noqa: E501
    engine: Engine,
) -> None:
    """Match an unchanged control through completion, close, triage, and retry."""
    fixture = _accepted_dependency_sprint(engine, started=True)
    before = _snapshot(engine, fixture.scenario)
    original_contract = execution_contract(before, fixture.sprint_id)
    assert source_sprint_contract_is_current(before, sprint_id=fixture.sprint_id)
    original_evidence = _stored_sprint_evidence(engine, fixture)
    checkpoint = _execution_checkpoint(engine, fixture)
    control_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    try:
        ensure_business_db_ready(control_engine)
        control = _accepted_dependency_sprint(control_engine, started=True)
        control_before = _snapshot(control_engine, control.scenario)
        control_contract = execution_contract(control_before, control.sprint_id)
        assert source_sprint_contract_is_current(
            control_before, sprint_id=control.sprint_id
        )
        assert _execution_checkpoint(control_engine, control) == checkpoint

        _replace(engine, fixture.scenario.artifact_ids[0])

        replaced = _snapshot(engine, fixture.scenario)
        assert (
            _story(replaced, fixture.story_id).selected_scope_fingerprint
            != fixture.review_source
        )
        assert not _story(replaced, fixture.story_id).dependency_safe
        assert not _story(replaced, fixture.story_id).sprint_candidate
        assert (
            "STORY_DEPENDENCY_SUPERSEDED_ENDPOINT"
            in _story(replaced, fixture.story_id).readiness_blockers
        )
        assert _stored_sprint_evidence(engine, fixture) == original_evidence
        assert (
            execution_contract(replaced, fixture.sprint_id).fingerprint
            == original_contract.fingerprint
        )
        assert source_sprint_contract_is_current(replaced, sprint_id=fixture.sprint_id)
        assert _execution_checkpoint(engine, fixture) == checkpoint
        assert _complete_and_close_at_checkpoints(
            engine, fixture
        ) == _complete_and_close_at_checkpoints(control_engine, control)
        for target_engine, target, expected_contract in (
            (engine, fixture, original_contract),
            (control_engine, control, control_contract),
        ):
            closed = _snapshot(target_engine, target.scenario)
            assert source_sprint_contract_is_current(closed, sprint_id=target.sprint_id)
            assert (
                execution_contract(closed, target.sprint_id).fingerprint
                == expected_contract.fingerprint
            )
            assert (
                next(
                    item
                    for item in closed.sprint_starts
                    if item.sprint_id == target.sprint_id
                )
                == expected_contract.start
            )
            _triage_execution_sprint(
                execution_domain(target_engine),
                project_id=target.scenario.project_id,
                sprint_id=target.sprint_id,
            )
        replaced_closed = _snapshot(engine, fixture.scenario)
        control_closed = _snapshot(control_engine, control.scenario)
        eligibility = evaluate_sprint_retry_eligibility(
            replaced_closed, sprint_id=fixture.sprint_id
        )
        control_eligibility = evaluate_sprint_retry_eligibility(
            control_closed, sprint_id=control.sprint_id
        )
        assert eligibility.blockers == control_eligibility.blockers == ()
        assert (
            eligibility.predecessor_retry_attempt_id
            == control_eligibility.predecessor_retry_attempt_id
        )
        assert eligibility.next_ordinal == control_eligibility.next_ordinal
        assert eligibility.story_ids == (fixture.story_id,)
        assert control_eligibility.story_ids == (control.story_id,)
        assert eligibility.task_ids == (fixture.task_id,)
        assert control_eligibility.task_ids == (control.task_id,)
        assert eligibility.contract_fingerprint == original_contract.fingerprint
        assert control_eligibility.contract_fingerprint == control_contract.fingerprint
    finally:
        control_engine.dispose()


def test_triaged_history_survives_later_dependency_reconciliation(
    engine: Engine,
) -> None:
    """Reconcile fresh selected work while retaining exact triaged Sprint bytes."""
    fixture = _accepted_dependency_sprint(engine, started=True, story_count=3)
    scenario = fixture.scenario
    _close_execution_sprint(
        execution_domain(engine),
        project_id=scenario.project_id,
        sprint_id=fixture.sprint_id,
        story_id=fixture.story_id,
        task_id=fixture.task_id,
        idempotency_suffix="-preservation-triaged",
    )
    _triage_execution_sprint(
        execution_domain(engine),
        project_id=scenario.project_id,
        sprint_id=fixture.sprint_id,
    )
    historical = _stored_sprint_evidence(engine, fixture)
    old_facts = _completed_history(_snapshot(engine, scenario))
    fresh = scenario.story_ids[2]
    edge_id = _edge(engine, scenario, fresh, scenario.story_ids[0])
    _review(engine, scenario, fresh)
    reviewed = _snapshot(engine, scenario)
    old_source = _story(reviewed, fresh).selected_scope_fingerprint

    _replace(engine, scenario.artifact_ids[0])

    inspection = _inspect(engine, scenario)
    assert {item["dependency_id"] for item in inspection["issues"]} == {edge_id}
    assert inspection["selected_scope_fingerprint"] != old_source
    assert not _story(_snapshot(engine, scenario), fresh).sprint_candidate
    request = _request(
        engine, scenario, fresh, (), "preservation-explicit-reconciliation"
    )
    assert _domain(engine).transition(request).ok
    ready = _snapshot(engine, scenario)
    assert _story(ready, fresh).sprint_candidate
    assert _story(ready, fresh).dependency_safe
    with Session(engine) as session:
        require_story_ready_for_sprint(session, story=session.get_one(UserStory, fresh))
        assert session.get_one(UserStoryDependency, edge_id).status == "rejected"
    assert _inspect(engine, scenario)["issues"] == []
    assert _stored_sprint_evidence(engine, fixture) == historical
    assert _completed_history(ready) == old_facts


def _plan_or_start_preservation_retry(
    engine: Engine, fixture: _DependencySprint, *, started: bool
) -> None:
    domain = WorkflowDomain(
        engine=engine, graph=project_graph(), clock=FixedClock(now_value=EVALUATED_AT)
    )
    project_id = fixture.scenario.project_id
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=fixture.sprint_id,
        )
    assert preview.blockers == ()
    position = domain.position(project_id)
    result = domain.transition(
        RetrySprint(
            **planning_guards(
                position, "execution.sprint.retry", f"sprint:{fixture.sprint_id}"
            ),
            idempotency_key="preservation-plan-retry",
            sprint_id=fixture.sprint_id,
            confirm=True,
            rationale="Retry the exact preserved source contract.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
    )
    assert result.ok
    retry_id = result.output["retry_attempt_id"]
    assert isinstance(retry_id, int)
    if started:
        start = next(
            item
            for item in domain.position(project_id).decisions
            if item.node_id == "execution.sprint.retry.start"
        )
        assert domain.transition(
            StartSprintRetry(
                **planning_guards(
                    domain.position(project_id), start.node_id, start.instance_key
                ),
                idempotency_key="preservation-start-retry",
                sprint_id=fixture.sprint_id,
                retry_attempt_id=retry_id,
            )
        ).ok


@pytest.mark.parametrize(
    "state",
    [
        "accepted-planned",
        "active",
        "retry-planned",
        "retry-active",
        "untriaged-completed",
    ],
)
def test_dependency_review_lock_after_replacement_preserves_business_rows(
    engine: Engine, state: str
) -> None:
    """Keep each unresolved Sprint/retry lifecycle locked after replacement."""
    fixture = _accepted_dependency_sprint(engine, started=state != "accepted-planned")
    scenario = fixture.scenario
    if state in {"retry-planned", "retry-active", "untriaged-completed"}:
        _close_execution_sprint(
            execution_domain(engine),
            project_id=scenario.project_id,
            sprint_id=fixture.sprint_id,
            story_id=fixture.story_id,
            task_id=fixture.task_id,
            idempotency_suffix="-preservation-lock",
        )
    if state in {"retry-planned", "retry-active"}:
        _triage_execution_sprint(
            execution_domain(engine),
            project_id=scenario.project_id,
            sprint_id=fixture.sprint_id,
        )
        _plan_or_start_preservation_retry(
            engine, fixture, started=state == "retry-active"
        )
    domain = _domain(engine)
    lock_before = planning_decision(
        domain.position(scenario.project_id), "planning.story_dependencies"
    )
    assert lock_before.category is NodeCategory.BLOCKED
    _replace(engine, scenario.artifact_ids[0])
    locked = planning_decision(
        domain.position(scenario.project_id), "planning.story_dependencies"
    )
    assert (locked.category, locked.reason_code) == (
        lock_before.category,
        lock_before.reason_code,
    )
    request = _request(
        engine, scenario, fixture.story_id, (), f"preservation-locked-apply-{state}"
    )
    before = _project_business_rows(engine, scenario.project_id)
    refused = domain.transition(request)
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE
    assert _project_business_rows(engine, scenario.project_id) == before
    receipt = _rejection_receipt(engine, request)
    replay = domain.transition(request)
    assert not replay.ok
    assert replay.replayed
    assert replay.error == refused.error
    assert _rejection_receipt(engine, request) == receipt
    assert _project_business_rows(engine, scenario.project_id) == before
