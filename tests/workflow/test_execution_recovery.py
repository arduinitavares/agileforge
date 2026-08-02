"""Execution transition rollback, interrupted retry, and replay tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import pytest
from sqlmodel import Session, col, select

import workflow.handlers.execution as execution_handlers
from models.core import Product, Sprint, SprintStory, Task, Team, UserStory
from models.enums import SprintStatus, StoryStatus, TaskStatus
from models.events import TaskExecutionLog
from models.workflow import TaskCompletionEvidence, WorkflowTransitionReceipt
from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.clock import FixedClock
from workflow.definitions.execution import execution_graph
from workflow.domain import WorkflowDomain
from workflow.requests import CompleteTask

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from services.task_execution_service import TaskCompletionInput
    from workflow.contracts import NodeDecision

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


class _InterruptedAfterFlushError(RuntimeError):
    """Controlled interruption after all execution rows have flushed."""


def _seed(engine: Engine) -> tuple[int, int]:
    with Session(engine) as session:
        project = Product(name="Task 12 recovery", origin="greenfield")
        team = Team(name="Task 12 recovery team")
        session.add(project)
        session.add(team)
        session.flush()
        assert project.product_id is not None
        assert team.team_id is not None
        sprint = Sprint(
            product_id=project.product_id,
            team_id=team.team_id,
            status=SprintStatus.ACTIVE,
            started_at=EVALUATED_AT,
        )
        story = UserStory(
            product_id=project.product_id,
            title="Recover transition",
            status=StoryStatus.TO_DO,
            is_refined=True,
            story_points=1,
            rank="1.1",
        )
        session.add(sprint)
        session.add(story)
        session.flush()
        assert sprint.sprint_id is not None
        assert story.story_id is not None
        session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))
        task = Task(
            story_id=story.story_id,
            description="Flush then recover",
            metadata_json=serialize_task_metadata(
                TaskMetadata(
                    task_kind="testing",
                    checklist_items=["Recovery test passes"],
                )
            ),
            status=TaskStatus.IN_PROGRESS,
        )
        session.add(task)
        session.commit()
        assert task.task_id is not None
        return project.product_id, task.task_id


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=execution_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _request(domain: WorkflowDomain, project_id: int, task_id: int) -> CompleteTask:
    position = domain.position(project_id)
    decision: NodeDecision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.task.complete"
        and item.instance_key == f"task:{task_id}"
    )
    assert decision.instance_key is not None
    return CompleteTask(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        instance_key=decision.instance_key,
        idempotency_key="interrupted-completion",
        actor="operator@example.com",
        task_id=task_id,
        outcome_summary="Recovered completion.",
        artifact_refs=(),
        acceptance_result="fully_met",
        checklist_result={"Recovery test passes": "passed"},
    )


def test_handler_failure_after_flush_rolls_back_business_audit_and_receipt(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback business, audit, and receipt rows after a flushed failure."""
    project_id, task_id = _seed(engine)
    domain = _domain(engine)
    request = _request(domain, project_id, task_id)
    original = execution_handlers.complete_task_in_session

    def fail_after_flush(
        session: Session,
        command: TaskCompletionInput,
    ) -> NoReturn:
        original(session, command)
        raise _InterruptedAfterFlushError

    monkeypatch.setattr(
        execution_handlers,
        "complete_task_in_session",
        fail_after_flush,
    )
    with pytest.raises(_InterruptedAfterFlushError):
        domain.transition(request)
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.IN_PROGRESS
        assert session.exec(select(TaskCompletionEvidence)).all() == []
        assert session.exec(select(TaskExecutionLog)).all() == []
        assert session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == "interrupted-completion"
            )
        ).all() == []


def test_interrupted_retry_succeeds_once_and_replays_once(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let one interrupted request retry once and then replay once."""
    project_id, task_id = _seed(engine)
    domain = _domain(engine)
    request = _request(domain, project_id, task_id)
    original = execution_handlers.complete_task_in_session

    def fail_once(
        _session: Session,
        _command: TaskCompletionInput,
    ) -> NoReturn:
        raise _InterruptedAfterFlushError

    monkeypatch.setattr(execution_handlers, "complete_task_in_session", fail_once)
    with pytest.raises(_InterruptedAfterFlushError):
        domain.transition(request)
    monkeypatch.setattr(execution_handlers, "complete_task_in_session", original)
    first = domain.transition(request)
    replay = domain.transition(request)
    assert first.ok is True
    assert replay.ok is True
    assert replay.replayed is True
    assert replay.output == first.output
    with Session(engine) as session:
        assert len(session.exec(select(TaskCompletionEvidence)).all()) == 1
        assert len(session.exec(select(TaskExecutionLog)).all()) == 1
        assert len(session.exec(select(WorkflowTransitionReceipt)).all()) == 1
