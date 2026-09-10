"""Shared disposable transport setup and durable-state assertions for Sprint retries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlmodel import SQLModel

import models.core as core_models  # noqa: F401
import models.events as event_models  # noqa: F401
import models.sprint_retry as retry_models  # noqa: F401
import models.workflow as workflow_models  # noqa: F401
from services.application import AgileForgeApplication, ExecutionActionSelectionService
from services.read_projections import DurableReadProjectionService
from tests.workflow.retry_execution_fixtures import (
    CompletedRetrySource,
    seed_completed_retry_source,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.sql.schema import Table


type DurableRow = tuple[object, ...]
type DurableRowKey = tuple[object, ...]
type DurableTableRows = dict[DurableRowKey, DurableRow]


@dataclass(frozen=True)
class DurableRows:
    """Every registered SQLModel table captured by primary key and raw values."""

    tables: dict[str, DurableTableRows]


@dataclass(frozen=True)
class RetryTransportFixture:
    """One completed disposable Sprint with its real domain and read application."""

    source: CompletedRetrySource
    application: AgileForgeApplication


def retry_transport_fixture(engine: Engine) -> RetryTransportFixture:
    """Seed the reviewed shared retry fixture without importing concrete tests."""
    source = seed_completed_retry_source(engine)
    return RetryTransportFixture(
        source=source,
        application=AgileForgeApplication(
            workflow_domain=source.domain,
            read_projection=DurableReadProjectionService(engine=engine),
            execution_action_selection=ExecutionActionSelectionService(engine=engine),
        ),
    )


def durable_rows(engine: Engine) -> DurableRows:
    """Capture raw stored rows without SQLAlchemy's model result processors."""
    with engine.connect() as connection:
        return DurableRows(
            tables={
                table.name: _raw_table_rows(connection, table)
                for table in SQLModel.metadata.sorted_tables
            }
        )


def _raw_table_rows(connection: Connection, table: Table) -> DurableTableRows:
    """Key raw SQLite rows by the registered table's real primary-key columns."""
    columns = tuple(table.columns)
    primary_key_names = tuple(column.name for column in table.primary_key.columns)
    primary_key_indexes = tuple(
        index
        for index, column in enumerate(columns)
        if column.name in primary_key_names
    )
    # The SQLModel metadata supplies this registered table identifier.
    rows = connection.execute(
        text(f"SELECT * FROM {table.name} ORDER BY rowid")  # noqa: S608  # nosec B608
    )
    return {
        tuple(row[index] for index in primary_key_indexes): tuple(row) for row in rows
    }


def preserves_existing_rows(before: DurableRows, after: DurableRows) -> bool:
    """Return whether every pre-existing durable row retained its exact values."""
    return before.tables.keys() == after.tables.keys() and all(
        all(
            after.tables[table_name].get(primary_key) == row
            for primary_key, row in rows.items()
        )
        for table_name, rows in before.tables.items()
    )


def retry_row_count(rows: DurableRows, table_name: str) -> int:
    """Return one retry table's durable row count for concise behavior assertions."""
    return len(rows.tables[table_name])


def event_row_count(rows: DurableRows) -> int:
    """Return the exact audit-event count in a durable snapshot."""
    return len(rows.tables["workflow_events"])


def receipt_row_count(rows: DurableRows) -> int:
    """Return the exact transition-receipt count in a durable snapshot."""
    return len(rows.tables["workflow_transition_receipts"])
