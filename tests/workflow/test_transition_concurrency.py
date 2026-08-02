"""Independent-session workflow transition concurrency tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Lock
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import workflow.domain as workflow_domain_module
from models.core import Product
from models.db import set_sqlite_pragma
from models.workflow import DiscoveryRun, WorkflowTransitionReceipt
from workflow import OpenProjectShell, WorkflowDomain
from workflow.clock import FixedClock
from workflow.definitions.root import ROOT_GRAPH

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import TransitionResult
    from workflow.graph import WorkflowGraph


EVALUATED_AT = datetime(2026, 8, 2, 14, tzinfo=UTC)


@pytest.fixture
def sqlite_file_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provide independent SQLite connections backed by one database file."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow-concurrency.db'}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_two_concurrent_identical_requests_apply_once_and_replay_once(
    sqlite_file_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serialize receipt claim and handler facts across independent sessions."""
    domain = WorkflowDomain(
        engine=sqlite_file_engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )
    request = OpenProjectShell(
        name="Concurrent Project",
        origin="greenfield",
        idempotency_key="concurrent-key",
        actor="operator",
    )
    start = Barrier(2)
    invocation_lock = Lock()
    invocation_count = 0
    real_handler = workflow_domain_module.execute_open_project_shell

    def counted_handler(
        session: Session,
        request: OpenProjectShell,
        graph: WorkflowGraph,
        evaluated_at: datetime,
    ) -> TransitionResult:
        nonlocal invocation_count
        with invocation_lock:
            invocation_count += 1
        return real_handler(session, request, graph, evaluated_at)

    monkeypatch.setattr(
        workflow_domain_module,
        "execute_open_project_shell",
        counted_handler,
    )

    def execute() -> TransitionResult:
        start.wait()
        return domain.transition(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: execute(), range(2)))

    assert invocation_count == 1
    assert sorted(result.replayed for result in results) == [False, True]
    assert all(result.ok for result in results)
    with Session(sqlite_file_engine) as session:
        assert len(session.exec(select(Product)).all()) == 1
        assert len(session.exec(select(DiscoveryRun)).all()) == 1
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        assert len(receipts) == 1
        assert receipts[0].completed_at == EVALUATED_AT.replace(tzinfo=None)
