"""Request-local durable dashboard read boundary."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, cast

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

from services.application import AgileForgeApplication, DeliveryReviewSelectionService
from services.read_projections import DurableReadProjectionService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, WorkflowPosition

_SNAPSHOT_TIMEOUT_SECONDS = 1.0
_SNAPSHOT_TIMEOUT_MESSAGE = "Dashboard snapshot capture timed out."


class DashboardSnapshotTimeoutError(TimeoutError):
    """A complete SQLite dashboard snapshot could not be captured in time."""


def _backup_sqlite(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    deadline: float,
) -> None:
    """Copy all pages with a deadline, including busy backup steps."""

    def check_deadline(_status: int, _remaining: int, _total: int) -> None:
        if monotonic() >= deadline:
            raise DashboardSnapshotTimeoutError(_SNAPSHOT_TIMEOUT_MESSAGE)

    source.backup(target, pages=256, progress=check_deadline, sleep=0.01)
    check_deadline(0, 0, 0)


@contextmanager
def _dashboard_read_engine(engine: Engine) -> Iterator[Engine]:
    """Release the live SQLite connection before reading a private copy."""
    if engine.dialect.name != "sqlite":
        yield engine
        return

    deadline = monotonic() + _SNAPSHOT_TIMEOUT_SECONDS
    target = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        with closing(engine.raw_connection()) as source_proxy:
            source = cast("sqlite3.Connection", source_proxy.driver_connection)
            _backup_sqlite(source, target, deadline=deadline)
        target.execute("PRAGMA query_only=ON").close()
        copy_engine = create_engine(
            "sqlite://",
            creator=lambda: target,
            poolclass=StaticPool,
        )
        try:
            yield copy_engine
        finally:
            copy_engine.dispose()
    finally:
        target.close()


@dataclass(frozen=True)
class DashboardReadView:
    """One consistent copied view and its existing projection interface."""

    application: AgileForgeApplication | None
    position: WorkflowPosition | None
    error: JsonObject | None


@contextmanager
def dashboard_read_view(
    application: AgileForgeApplication,
    project_id: int,
) -> Iterator[DashboardReadView]:
    """Load Project facts once and keep all durable reads in one view."""
    engine = application._engine()
    evaluated_at = application.dashboard_evaluation_time()
    with (
        _dashboard_read_engine(engine) as read_engine,
        Session(read_engine) as session,
    ):
        initial_reads = DurableReadProjectionService(
            engine=read_engine,
            session=session,
        )
        snapshot_or_error = initial_reads._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            yield DashboardReadView(
                application=None,
                position=None,
                error=snapshot_or_error,
            )
            return
        position = application.position_from_snapshot(
            snapshot_or_error,
            evaluated_at=evaluated_at,
        )
        reads = DurableReadProjectionService(
            engine=read_engine,
            session=session,
            snapshot=snapshot_or_error,
        )
        view = application.dashboard_read_view(
            position=position,
            read_projection=reads,
            selection=DeliveryReviewSelectionService(
                engine=read_engine,
                session=session,
            ),
        )
        yield DashboardReadView(application=view, position=position, error=None)
