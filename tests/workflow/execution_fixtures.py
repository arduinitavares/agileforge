"""Normalized persisted Sprint execution fixtures shared by focused tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, select

from models.core import Task, UserStoryDependency
from models.enums import TaskStatus
from tests.workflow.test_planning_transitions import (
    _domain as _planning_domain,
)
from tests.workflow.test_planning_transitions import (
    _guards as _planning_guards,
)
from tests.workflow.test_planning_transitions import (
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _record_sprint_plan_draft,
    _seed_accepted_backlog,
)
from workflow.fingerprints import canonical_hash
from workflow.requests import DecideSprintPlan, StartSprint

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject
    from workflow.domain import WorkflowDomain

type _SprintPlanBinding = tuple[int, int, str, JsonObject]


def _required_identity(value: int | None, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise AssertionError(message)
    return value


def _accept_and_start_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    plan_binding: _SprintPlanBinding,
    idempotency_suffix: str,
) -> None:
    plan_id, sprint_id, candidate_fingerprint, plan = plan_binding
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_planning_guards(position, "planning.sprint.review"),
            idempotency_key=f"task-12-accept-sprint-plan{idempotency_suffix}",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=canonical_hash(plan),
            decision="accepted",
            rationale="Accepted for execution integrity tests.",
        )
    )
    if not accepted.ok:
        message = "Sprint plan acceptance fixture failed."
        raise AssertionError(message)
    position = domain.position(project_id)
    started = domain.transition(
        StartSprint(
            **_planning_guards(position, "planning.sprint.start"),
            idempotency_key=f"task-12-start-sprint{idempotency_suffix}",
            sprint_plan_artifact_id=plan_id,
            sprint_id=sprint_id,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=candidate_fingerprint,
        )
    )
    if not started.ok:
        message = "Sprint start fixture failed."
        raise AssertionError(message)


def seed_started_execution(
    engine: Engine,
    *,
    task_status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> tuple[int, int, int, int]:
    """Persist one Sprint through the exact accepted planning/start lineage."""
    project_id = _seed_accepted_backlog(engine)
    domain = _planning_domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(domain, project_id)
    plan_binding = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Task 12 normalized execution team",
        idempotency_key="task-12-record-sprint-plan",
    )
    _accept_and_start_sprint(
        domain,
        project_id=project_id,
        plan_binding=plan_binding,
        idempotency_suffix="",
    )
    _plan_id, sprint_id, _candidate_fingerprint, _plan = plan_binding
    with Session(engine) as session:
        task = session.exec(select(Task).where(Task.story_id == story_id)).one()
        task.status = task_status
        session.add(task)
        session.commit()
        task_id = _required_identity(task.task_id, "Task")
        return project_id, sprint_id, story_id, task_id


def seed_started_execution_with_unselected_story(
    engine: Engine,
    *,
    task_status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> tuple[int, int, int, int, int, int]:
    """Start one selected Story while preserving one reviewed future candidate."""
    selected_requirement = "Plan immutable work"
    future_requirement = "Plan future work"
    requirements = (selected_requirement, future_requirement)
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    domain = _planning_domain(engine)
    _record_and_accept_roadmap(domain, project_id, requirements=requirements)
    _story_artifact_id, selected_story_id = _record_and_accept_story(
        domain,
        project_id,
        requirement=selected_requirement,
    )
    _future_artifact_id, future_story_id = _record_and_accept_story(
        domain,
        project_id,
        requirement=future_requirement,
        idempotency_suffix="-future",
    )
    with Session(engine) as session:
        dependency = UserStoryDependency(
            project_id=project_id,
            dependent_story_id=selected_story_id,
            prerequisite_story_id=future_story_id,
            status="rejected",
            source="manual_review",
            confidence="reviewed",
            reason="Rejected future-only ordering constraint.",
        )
        session.add(dependency)
        session.commit()
        dependency_id = _required_identity(
            dependency.dependency_id,
            "Story dependency",
        )
    plan_binding = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        selected_story_id,
        team_name="Task 12 selected-scope execution team",
        idempotency_key="task-12-record-selected-scope-sprint-plan",
    )
    _accept_and_start_sprint(
        domain,
        project_id=project_id,
        plan_binding=plan_binding,
        idempotency_suffix="-selected-scope",
    )
    _plan_id, sprint_id, _candidate_fingerprint, _plan = plan_binding
    with Session(engine) as session:
        task = session.exec(
            select(Task).where(Task.story_id == selected_story_id)
        ).one()
        task.status = task_status
        session.add(task)
        session.commit()
        task_id = _required_identity(task.task_id, "Task")
        return (
            project_id,
            sprint_id,
            selected_story_id,
            future_story_id,
            task_id,
            dependency_id,
        )


__all__ = ["seed_started_execution", "seed_started_execution_with_unselected_story"]
