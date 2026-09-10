"""Persisted multi-Story Sprint retry fixtures using normal planning transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from models.core import Task
from repositories.workflow import WorkflowFactRepository
from services.contracts.sprint import SprintPlannerOutput
from tests.workflow.execution_retry_support import _close_execution_sprint
from tests.workflow.planning_fixtures import (
    apply_current_dependencies,
    planning_guards,
    record_and_accept_roadmap,
    record_and_accept_story,
    seed_accepted_backlog,
    select_for_sprint,
)
from workflow.clock import FixedClock
from workflow.definitions.planning import (
    planning_graph,
    story_dependency_source_fingerprint,
)
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.requests import (
    ApplyStoryDependencies,
    CloseSprint,
    CloseStory,
    CompleteTask,
    DecideSprintPlan,
    RecordPostSprintTriage,
    RecordSprintPlan,
    ReviewSprint,
    StartSprint,
)
from workflow.requests.planning import ReviewedDependencyEdge

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class CompletedRetrySource:
    """Exact completed Sprint 2 with two inner Stories and one future candidate."""

    domain: WorkflowDomain
    project_id: int
    source_sprint_id: int
    external_story_id: int
    first_story_id: int
    second_story_id: int
    candidate_story_id: int
    external_task_id: int
    first_task_id: int
    second_task_id: int
    original_first_task_request: CompleteTask
    original_first_task_output: dict[str, object]


@dataclass(frozen=True)
class SprintPlanFixtureInput:
    """Inputs that define one recorded and started fixture Sprint."""

    project_id: int
    story_ids: tuple[int, ...]
    idempotency_suffix: str
    started_at: datetime
    reviewed_edges: tuple[ReviewedDependencyEdge, ...] = ()


def _required_id(value: object, label: str) -> int:
    if type(value) is not int:
        message = f"{label} has no durable identity."
        raise TypeError(message)
    return value


def _required_instance_key(value: str | None, label: str) -> str:
    if value is None:
        message = f"{label} has no execution instance key."
        raise AssertionError(message)
    return value


def _execution_domain(engine: Engine, *, at: datetime) -> WorkflowDomain:
    """Use a clock after the fixture's persisted Sprint start."""
    return WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=at),
    )


def _review_dependencies(
    engine: Engine,
    domain: WorkflowDomain,
    *,
    project_id: int,
    edges: tuple[ReviewedDependencyEdge, ...],
    idempotency_key: str,
) -> None:
    """Persist the exact selected B/C dependency contract through the graph."""
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    completed_sprint_ids = {
        item.sprint_id for item in snapshot.sprints if item.status == "completed"
    }
    selected = tuple(
        item
        for item in snapshot.stories
        if item.structurally_eligible
        and item.sprint_selection_state == "selected"
        and not any(sprint_id in completed_sprint_ids for sprint_id in item.sprint_ids)
    )
    position = domain.position(project_id)
    assert domain.transition(
        ApplyStoryDependencies(
            **planning_guards(position, "planning.story_dependencies"),
            idempotency_key=idempotency_key,
            selected_story_ids=tuple(item.story_id for item in selected),
            reviewed_edges=edges,
            source_fingerprint=story_dependency_source_fingerprint(selected),
        )
    ).ok


def _plan_and_start(
    engine: Engine,
    plan: SprintPlanFixtureInput,
) -> int:
    """Record and start a real multi-Story Sprint plan from selected facts."""
    planning_domain = WorkflowDomain(
        engine=engine,
        graph=planning_graph(),
        clock=FixedClock(now_value=plan.started_at),
    )
    for story_id in plan.story_ids:
        select_for_sprint(engine, story_id)
    if plan.reviewed_edges:
        _review_dependencies(
            engine,
            planning_domain,
            project_id=plan.project_id,
            edges=plan.reviewed_edges,
            idempotency_key=f"retry-fixture-dependencies-{plan.idempotency_suffix}",
        )
    else:
        apply_current_dependencies(
            engine,
            planning_domain,
            plan.project_id,
            idempotency_key=f"retry-fixture-dependencies-{plan.idempotency_suffix}",
        )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(plan.project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    story_facts = tuple(
        next(item for item in snapshot.stories if item.story_id == story_id)
        for story_id in plan.story_ids
    )
    output = SprintPlannerOutput.model_validate(
        {
            "sprint_goal": "Execute the exact approved retry fixture scope.",
            "selected_stories": [
                {
                    "story_id": story.story_id,
                    "story_item_id": story.source_story_item_id,
                    "tasks": [
                        {
                            "description": f"Implement fixture Story {story.story_id}",
                            "relevant_spec_item_ids": list(story.spec_item_ids),
                            "task_kind": "implementation",
                            "artifact_targets": ["workflow retry fixture"],
                            "workstream_tags": ["workflow"],
                            "checklist_items": ["Run focused tests"],
                        }
                    ],
                    "reason_for_selection": "Required retry fixture scope.",
                }
                for story in story_facts
            ],
        }
    )
    position = planning_domain.position(plan.project_id)
    recorded = planning_domain.transition(
        RecordSprintPlan(
            **planning_guards(position, "planning.sprint.plan"),
            idempotency_key=f"retry-fixture-plan-{plan.idempotency_suffix}",
            team_name=f"Retry fixture team {plan.idempotency_suffix}",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=output,
        )
    )
    assert recorded.ok, recorded.error
    plan_id = _required_id(
        recorded.output.get("sprint_plan_artifact_id"),
        "Sprint plan artifact",
    )
    plan_fingerprint = str(recorded.output["plan_fingerprint"])
    position = planning_domain.position(plan.project_id)
    accepted = planning_domain.transition(
        DecideSprintPlan(
            **planning_guards(position, "planning.sprint.review"),
            idempotency_key=f"retry-fixture-accept-{plan.idempotency_suffix}",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Accepted persisted retry fixture plan.",
        )
    )
    assert accepted.ok
    sprint_id = _required_id(accepted.output.get("activated_sprint_id"), "Sprint")
    position = planning_domain.position(plan.project_id)
    started = planning_domain.transition(
        StartSprint(
            **planning_guards(position, "planning.sprint.start"),
            idempotency_key=f"retry-fixture-start-{plan.idempotency_suffix}",
        )
    )
    assert started.ok
    return sprint_id


def _task_id(engine: Engine, story_id: int) -> int:
    with Session(engine) as session:
        task = session.exec(select(Task).where(Task.story_id == story_id)).one()
        return _required_id(task.task_id, "Task")


def _complete_story_without_closing_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    task_id: int,
    story_id: int,
    key: str,
) -> tuple[CompleteTask, dict[str, object]]:
    """Complete one Story while leaving its Sprint open for later scoped work."""
    position = domain.position(project_id)
    task = next(
        item
        for item in position.decisions
        if item.node_id == "execution.task.complete"
        and item.instance_key == f"task:{task_id}"
    )
    task_request = CompleteTask(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=task.decision_fingerprint,
        idempotency_key=f"{key}-task",
        actor="owner@example.com",
        instance_key=_required_instance_key(task.instance_key, "Task"),
        task_id=task_id,
        outcome_summary="Complete persisted fixture work.",
        artifact_refs=("tests/workflow/retry_execution_fixtures.py",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
    )
    completed = domain.transition(task_request)
    assert completed.ok
    position = domain.position(project_id)
    story = next(
        item
        for item in position.decisions
        if item.node_id == "execution.story.close"
        and item.instance_key == f"story:{story_id}"
    )
    assert domain.transition(
        CloseStory(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=story.decision_fingerprint,
            idempotency_key=f"{key}-story",
            actor="owner@example.com",
            instance_key=_required_instance_key(story.instance_key, "Story"),
            story_id=story_id,
            resolution="Completed",
            delivered="Persisted fixture Story delivered.",
            evidence="Focused retry fixture tests pass.",
            known_gaps="None.",
        )
    ).ok
    return task_request, dict(completed.output)


def _triage(
    domain: WorkflowDomain, *, project_id: int, sprint_id: int, key: str
) -> None:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.post_sprint_triage"
        and item.instance_key == f"sprint:{sprint_id}"
    )
    assert domain.transition(
        RecordPostSprintTriage(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=key,
            actor="owner@example.com",
            instance_key=_required_instance_key(decision.instance_key, "Triage"),
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    ).ok


def complete_retry_task(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    task_id: int,
    suffix: str,
) -> CompleteTask:
    """Complete one exact attempt-bound retry Task through the graph."""
    position = domain.position(project_id)
    complete = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.task.complete"
        and decision.instance_key == f"retry:{retry_id}:task:{task_id}"
    )
    request = CompleteTask(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=complete.decision_fingerprint,
        idempotency_key=f"{suffix}-task",
        actor="owner@example.com",
        instance_key=_required_instance_key(complete.instance_key, "Task"),
        task_id=task_id,
        outcome_summary="Re-executed the scoped work.",
        artifact_refs=("workflow/definitions/execution.py",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
    )
    result = domain.transition(request)
    assert result.ok is True
    return request


def close_retry_story(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    story_id: int,
    suffix: str,
) -> CloseStory:
    """Close one exact attempt-bound retry Story through the graph."""
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.story.close"
        and item.instance_key == f"retry:{retry_id}:story:{story_id}"
    )
    request = CloseStory(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-story",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Story"),
        story_id=story_id,
        resolution="Completed",
        delivered="Fresh retry Story delivery.",
        evidence="Retry execution evidence.",
        known_gaps="None.",
    )
    assert domain.transition(request).ok is True
    return request


def retry_review_request(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> ReviewSprint:
    """Build one exact retry review request from the currently actionable binding."""
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.sprint.review"
        and item.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    review_fingerprint = next(
        item.fingerprint
        for item in decision.fact_references
        if item.fact_type == "sprint_review"
    )
    return ReviewSprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-review",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Triage"),
        sprint_id=sprint_id,
        review_fingerprint=review_fingerprint,
    )


def review_retry_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> ReviewSprint:
    """Record review for one exact attempt-bound retry Sprint."""
    request = retry_review_request(
        domain,
        project_id=project_id,
        retry_id=retry_id,
        sprint_id=sprint_id,
        suffix=suffix,
    )
    assert domain.transition(request).ok is True
    return request


def close_retry_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> CloseSprint:
    """Explicitly close one exact attempt-bound retry Sprint."""
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.sprint.close"
        and item.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    review_fingerprint = next(
        item.fingerprint
        for item in decision.fact_references
        if item.fact_type == "sprint_review"
    )
    request = CloseSprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-close",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Triage"),
        sprint_id=sprint_id,
        review_fingerprint=review_fingerprint,
    )
    assert domain.transition(request).ok is True
    return request


def triage_retry_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> RecordPostSprintTriage:
    """Record terminal triage for one exact attempt-bound retry Sprint."""
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.post_sprint_triage"
        and item.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    request = RecordPostSprintTriage(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-triage",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Triage"),
        sprint_id=sprint_id,
        impact="none",
        canonical_payload={"summary": "Fresh retry triage complete."},
    )
    assert domain.transition(request).ok is True
    return request


def seed_completed_retry_source(engine: Engine) -> CompletedRetrySource:
    """Create A completed externally, B/C completed in source, and D unselected."""
    requirements = (
        "Complete external dependency A",
        "Complete scoped Story B",
        "Complete scoped Story C",
        "Preserve next Sprint candidate D",
    )
    project_id = seed_accepted_backlog(engine, requirements=requirements)
    planning_domain = WorkflowDomain(
        engine=engine,
        graph=planning_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, tzinfo=UTC)),
    )
    record_and_accept_roadmap(planning_domain, project_id, requirements=requirements)
    story_ids = tuple(
        record_and_accept_story(
            engine,
            planning_domain,
            project_id,
            requirement=requirement,
            spec_item_id=f"REQ.planning-{index}",
            idempotency_suffix=f"-retry-fixture-{index}",
        )[1]
        for index, requirement in enumerate(requirements, start=1)
    )
    external_story_id, first_story_id, second_story_id, candidate_story_id = story_ids
    first_sprint_id = _plan_and_start(
        engine,
        SprintPlanFixtureInput(
            project_id=project_id,
            story_ids=(external_story_id,),
            idempotency_suffix="external",
            started_at=datetime(2026, 9, 9, 9, tzinfo=UTC),
        ),
    )
    external_task_id = _task_id(engine, external_story_id)
    execution_domain = _execution_domain(
        engine,
        at=datetime(2026, 9, 9, 9, 1, tzinfo=UTC),
    )
    _close_execution_sprint(
        execution_domain,
        project_id=project_id,
        sprint_id=first_sprint_id,
        story_id=external_story_id,
        task_id=external_task_id,
        idempotency_suffix="-retry-fixture-external",
    )
    _triage(
        execution_domain,
        project_id=project_id,
        sprint_id=first_sprint_id,
        key="retry-fixture-triage-external",
    )

    source_sprint_id = _plan_and_start(
        engine,
        SprintPlanFixtureInput(
            project_id=project_id,
            story_ids=(first_story_id, second_story_id),
            idempotency_suffix="source",
            started_at=datetime(2026, 9, 9, 9, 5, tzinfo=UTC),
            reviewed_edges=(
                ReviewedDependencyEdge(
                    dependent_story_id=first_story_id,
                    prerequisite_story_id=external_story_id,
                    reason="Story B requires completed external Story A.",
                ),
                ReviewedDependencyEdge(
                    dependent_story_id=second_story_id,
                    prerequisite_story_id=first_story_id,
                    reason="Story C requires scoped Story B.",
                ),
            ),
        ),
    )
    first_task_id = _task_id(engine, first_story_id)
    second_task_id = _task_id(engine, second_story_id)
    execution_domain = _execution_domain(
        engine,
        at=datetime(2026, 9, 9, 9, 6, tzinfo=UTC),
    )
    original_first_task_request, original_first_task_output = (
        _complete_story_without_closing_sprint(
            execution_domain,
            project_id=project_id,
            task_id=first_task_id,
            story_id=first_story_id,
            key="retry-fixture-source-first",
        )
    )
    _close_execution_sprint(
        execution_domain,
        project_id=project_id,
        sprint_id=source_sprint_id,
        story_id=second_story_id,
        task_id=second_task_id,
        idempotency_suffix="-retry-fixture-source-second",
    )
    _triage(
        execution_domain,
        project_id=project_id,
        sprint_id=source_sprint_id,
        key="retry-fixture-triage-source",
    )
    return CompletedRetrySource(
        domain=WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=datetime(2026, 9, 9, 9, 10, tzinfo=UTC)),
        ),
        project_id=project_id,
        source_sprint_id=source_sprint_id,
        external_story_id=external_story_id,
        first_story_id=first_story_id,
        second_story_id=second_story_id,
        candidate_story_id=candidate_story_id,
        external_task_id=external_task_id,
        first_task_id=first_task_id,
        second_task_id=second_task_id,
        original_first_task_request=original_first_task_request,
        original_first_task_output=original_first_task_output,
    )


def record_pending_successor_plan(
    engine: Engine,
    source: CompletedRetrySource,
) -> tuple[WorkflowDomain, int]:
    """Record one valid later candidate plan without accepting or starting it."""
    select_for_sprint(engine, source.candidate_story_id)
    domain = WorkflowDomain(
        engine=engine,
        graph=planning_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, 9, 20, tzinfo=UTC)),
    )
    apply_current_dependencies(
        engine,
        domain,
        source.project_id,
        idempotency_key="retry-fixture-pending-dependencies",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    story = next(
        item for item in snapshot.stories if item.story_id == source.candidate_story_id
    )
    output = SprintPlannerOutput.model_validate(
        {
            "sprint_goal": "Review the valid next Sprint candidate.",
            "selected_stories": [
                {
                    "story_id": story.story_id,
                    "story_item_id": story.source_story_item_id,
                    "tasks": [
                        {
                            "description": "Plan the next fixture candidate",
                            "relevant_spec_item_ids": list(story.spec_item_ids),
                            "task_kind": "implementation",
                            "artifact_targets": ["workflow retry fixture"],
                            "workstream_tags": ["workflow"],
                            "checklist_items": ["Run focused tests"],
                        }
                    ],
                    "reason_for_selection": "The preserved next candidate is ready.",
                }
            ],
        }
    )
    position = domain.position(source.project_id)
    recorded = domain.transition(
        RecordSprintPlan(
            **planning_guards(position, "planning.sprint.plan"),
            idempotency_key="retry-fixture-pending-plan",
            team_name="Retry fixture pending-plan team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=output,
        )
    )
    assert recorded.ok, recorded.error
    return domain, _required_id(
        recorded.output.get("sprint_plan_artifact_id"),
        "Pending Sprint plan artifact",
    )


__all__ = [
    "CompletedRetrySource",
    "close_retry_sprint",
    "close_retry_story",
    "complete_retry_task",
    "record_pending_successor_plan",
    "retry_review_request",
    "review_retry_sprint",
    "seed_completed_retry_source",
    "triage_retry_sprint",
]
