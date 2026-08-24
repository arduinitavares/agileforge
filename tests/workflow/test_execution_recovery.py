"""Execution transition rollback, interrupted retry, and replay tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import pytest
from sqlmodel import Session, SQLModel, col, create_engine, select

import workflow.handlers.execution as execution_handlers
from models.core import Task
from models.enums import TaskStatus
from models.events import TaskExecutionLog
from models.workflow import TaskCompletionEvidence, WorkflowTransitionReceipt
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.clock import FixedClock
from workflow.definitions.execution import execution_graph
from workflow.domain import WorkflowDomain
from workflow.requests import CompleteTask

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.task_execution_service import TaskCompletionInput
    from workflow.contracts import NodeDecision

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


class _InterruptedAfterFlushError(RuntimeError):
    """Controlled interruption after all execution rows have flushed."""


def _seed(engine: Engine) -> tuple[int, int]:
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    return project_id, task_id


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=execution_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _file_engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


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
        artifact_refs=("planning workflow handler",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
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
        assert (
            session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.idempotency_key)
                    == "interrupted-completion"
                )
            ).all()
            == []
        )


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
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == "interrupted-completion"
            )
        ).all()
        assert len(receipts) == 1


def test_process_restart_recovers_identical_position_retry_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover exact position and one retry after all dependencies restart."""
    first_engine = _file_engine(tmp_path / "execution-restart.db")
    project_id, task_id = _seed(first_engine)
    first_domain = _domain(first_engine)
    before = first_domain.position(project_id)
    request = _request(first_domain, project_id, task_id)
    original = execution_handlers.complete_task_in_session

    def interrupt(
        _session: Session,
        _command: TaskCompletionInput,
    ) -> NoReturn:
        raise _InterruptedAfterFlushError

    monkeypatch.setattr(execution_handlers, "complete_task_in_session", interrupt)
    with pytest.raises(_InterruptedAfterFlushError):
        first_domain.transition(request)
    first_engine.dispose()

    restarted_engine = _file_engine(tmp_path / "execution-restart.db")
    restarted_domain = _domain(restarted_engine)
    recovered = restarted_domain.position(project_id)
    assert recovered.fact_fingerprint == before.fact_fingerprint
    assert recovered.decisions == before.decisions

    monkeypatch.setattr(execution_handlers, "complete_task_in_session", original)
    applied = restarted_domain.transition(request)
    replay = restarted_domain.transition(request)
    assert applied.ok is True
    assert replay.ok is True
    assert replay.replayed is True
    assert replay.output == applied.output
    with Session(restarted_engine) as session:
        assert len(session.exec(select(TaskCompletionEvidence)).all()) == 1
        assert len(session.exec(select(TaskExecutionLog)).all()) == 1
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == "interrupted-completion"
            )
        ).all()
        assert len(receipts) == 1
    restarted_engine.dispose()
