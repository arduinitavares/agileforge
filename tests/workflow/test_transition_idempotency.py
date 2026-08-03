"""Transactional workflow idempotency and SQLite lock tests."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import workflow.domain as workflow_domain_module
from models.core import Project
from models.db import set_sqlite_pragma
from models.workflow import DiscoveryRun, WorkflowTransitionReceipt
from workflow import OpenProjectShell, WorkflowDomain
from workflow.clock import FixedClock
from workflow.contracts import WorkflowErrorCode
from workflow.definitions.root import ROOT_GRAPH

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy.engine import Connection, Engine

    from workflow.contracts import TransitionResult
    from workflow.graph import WorkflowGraph


EVALUATED_AT = datetime(2026, 8, 2, 13, tzinfo=UTC)
_MAX_BUSY_WAIT_SECONDS = 2.0


def make_domain(engine: Engine) -> WorkflowDomain:
    """Build a deterministic workflow domain."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def request(*, name: str = "Idempotent", actor: str = "operator") -> OpenProjectShell:
    """Build one canonical idempotent request."""
    return OpenProjectShell(
        name=name,
        origin="greenfield",
        idempotency_key="same-key",
        actor=actor,
        correlation_id="correlation-1",
    )


def test_canonical_replay_returns_persisted_result_without_handler(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay a completed receipt and bypass the non-idempotent handler."""
    domain = make_domain(engine)
    first = domain.transition(request())
    assert first.ok is True

    def forbidden_handler(*_args: object, **_kwargs: object) -> object:
        message = "replay invoked handler"
        raise AssertionError(message)

    monkeypatch.setattr(
        "workflow.domain.execute_open_project_shell",
        forbidden_handler,
    )

    replay = domain.transition(request())

    assert replay == first.model_copy(update={"replayed": True})
    with Session(engine) as session:
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        assert len(receipts) == 1
        assert receipts[0].completed_at == EVALUATED_AT.replace(tzinfo=None)
        assert len(session.exec(select(Project)).all()) == 1
        assert len(session.exec(select(DiscoveryRun)).all()) == 1


def test_same_key_with_changed_canonical_request_conflicts_without_mutation(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject reuse when any canonical request input changes."""
    domain = make_domain(engine)
    first = domain.transition(request())
    assert first.ok is True
    handler_calls = 0

    def forbidden_handler(*_args: object, **_kwargs: object) -> object:
        nonlocal handler_calls
        handler_calls += 1
        message = "changed canonical request invoked handler"
        raise AssertionError(message)

    monkeypatch.setattr(
        "workflow.domain.execute_open_project_shell",
        forbidden_handler,
    )

    conflict = domain.transition(request(actor="different-operator"))

    assert conflict.ok is False
    assert conflict.replayed is False
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert handler_calls == 0
    with Session(engine) as session:
        assert len(session.exec(select(WorkflowTransitionReceipt)).all()) == 1
        assert len(session.exec(select(Project)).all()) == 1
        assert len(session.exec(select(DiscoveryRun)).all()) == 1


def test_begin_immediate_is_first_statement_for_new_and_replayed_transition(
    engine: Engine,
) -> None:
    """Acquire the SQLite write lock before receipt lookup or fact reads."""
    domain = make_domain(engine)
    statements: list[str] = []

    def capture_statement(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        first = domain.transition(request())
        assert first.ok is True
        assert statements[0] == "BEGIN IMMEDIATE"

        statements.clear()
        replay = domain.transition(request())
        assert replay.replayed is True
        assert statements[0] == "BEGIN IMMEDIATE"
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)


def test_busy_timeout_exhaustion_maps_to_workflow_fact_conflict(
    tmp_path: Path,
) -> None:
    """Return a bounded conflict when another writer holds the database lock."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow-lock.db'}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    try:
        domain = make_domain(engine)
        with engine.connect() as locking_connection:
            locking_connection.exec_driver_sql("BEGIN IMMEDIATE")
            started = monotonic()
            result = domain.transition(request(name="Locked"))
            elapsed = monotonic() - started
            locking_connection.rollback()

        assert result.ok is False
        assert result.error is not None
        assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
        assert elapsed < _MAX_BUSY_WAIT_SECONDS
        with Session(engine) as session:
            assert session.exec(select(Project)).all() == []
            assert session.exec(select(WorkflowTransitionReceipt)).all() == []
    finally:
        engine.dispose()


def test_commit_time_lock_exhaustion_maps_to_workflow_fact_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map a lock raised by commit after one handler invocation."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow-commit-lock.db'}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    domain = make_domain(engine)
    real_handler = workflow_domain_module.execute_open_project_shell
    handler_calls = 0

    def counted_handler(
        session: Session,
        open_request: OpenProjectShell,
        graph: WorkflowGraph,
        evaluated_at: datetime,
    ) -> TransitionResult:
        nonlocal handler_calls
        handler_calls += 1
        return real_handler(session, open_request, graph, evaluated_at)

    monkeypatch.setattr(
        workflow_domain_module,
        "execute_open_project_shell",
        counted_handler,
    )
    try:
        with engine.connect() as reader:
            reader.exec_driver_sql("BEGIN")
            reader.exec_driver_sql("SELECT project_id FROM projects").all()
            started = monotonic()
            result = domain.transition(request(name="Commit Locked"))
            elapsed = monotonic() - started
            reader.rollback()

        assert result.ok is False
        assert result.error is not None
        assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
        assert handler_calls == 1
        assert elapsed < _MAX_BUSY_WAIT_SECONDS
        with Session(engine) as session:
            assert session.exec(select(Project)).all() == []
            assert session.exec(select(DiscoveryRun)).all() == []
            assert session.exec(select(WorkflowTransitionReceipt)).all() == []
    finally:
        engine.dispose()
