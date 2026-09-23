"""Request-local durable dashboard read boundary."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session

from services.application import AgileForgeApplication, DeliveryReviewSelectionService
from services.read_projections import DurableReadProjectionService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from workflow.contracts import JsonObject, WorkflowPosition


@dataclass(frozen=True)
class DashboardReadView:
    """One consistent read transaction and its existing projection interface."""

    application: AgileForgeApplication | None
    position: WorkflowPosition | None
    error: JsonObject | None


@contextmanager
def dashboard_read_view(
    application: AgileForgeApplication,
    project_id: int,
) -> Iterator[DashboardReadView]:
    """Load Project facts once and keep all durable reads in one transaction."""
    engine = application._engine()
    with Session(engine) as session:
        if session.get_bind().dialect.name == "sqlite":
            # sqlite3 legacy SELECT handling needs an explicit read transaction.
            session.connection().exec_driver_sql("BEGIN")
        initial_reads = DurableReadProjectionService(engine=engine, session=session)
        snapshot_or_error = initial_reads._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            yield DashboardReadView(
                application=None,
                position=None,
                error=snapshot_or_error,
            )
            return
        position = application.position_from_snapshot(snapshot_or_error)
        reads = DurableReadProjectionService(
            engine=engine,
            session=session,
            snapshot=snapshot_or_error,
        )
        view = application.dashboard_read_view(
            position=position,
            read_projection=reads,
            selection=DeliveryReviewSelectionService(engine=engine, session=session),
        )
        yield DashboardReadView(application=view, position=position, error=None)
