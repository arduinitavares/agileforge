"""Normalized persisted Sprint execution fixtures shared by focused tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, select

from models.core import Task
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
    plan_id, sprint_id, candidate_fingerprint, plan = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Task 12 normalized execution team",
        idempotency_key="task-12-record-sprint-plan",
    )
    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_planning_guards(position, "planning.sprint.review"),
            idempotency_key="task-12-accept-sprint-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=canonical_hash(plan),
            decision="accepted",
            rationale="Accepted for execution integrity tests.",
        )
    )
    assert accepted.ok is True
    position = domain.position(project_id)
    started = domain.transition(
        StartSprint(
            **_planning_guards(position, "planning.sprint.start"),
            idempotency_key="task-12-start-sprint",
            sprint_plan_artifact_id=plan_id,
            sprint_id=sprint_id,
            plan_fingerprint=canonical_hash(plan),
            candidate_set_fingerprint=candidate_fingerprint,
        )
    )
    assert started.ok is True
    with Session(engine) as session:
        task = session.exec(select(Task).where(Task.story_id == story_id)).one()
        task.status = task_status
        session.add(task)
        session.commit()
        assert task.task_id is not None
        return project_id, sprint_id, story_id, task.task_id


__all__ = ["seed_started_execution"]
