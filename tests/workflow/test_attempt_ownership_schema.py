"""Only reviewed historical schemas gain separate ownership without row rewrites."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, inspect
from sqlmodel import SQLModel

from models import db
from tests.workflow.test_sprint_retry_schema import _file_engine

if TYPE_CHECKING:
    from pathlib import Path

OWNER_TABLE = "workflow_node_attempt_ownership"


def test_previous_current_schema_adds_only_owner_table_atomically(
    tmp_path: Path,
) -> None:
    """Previous current schema adds only owner table atomically."""
    engine = _file_engine(tmp_path / "previous-current.sqlite")
    try:
        SQLModel.metadata.create_all(
            engine,
            tables=[
                table
                for name, table in SQLModel.metadata.tables.items()
                if name != OWNER_TABLE
            ],
        )
        with engine.connect() as connection:
            before = tuple(
                connection.exec_driver_sql(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).all()
            )
        db.ensure_business_db_ready(engine)
        assert OWNER_TABLE in inspect(engine).get_table_names()
        with engine.connect() as connection:
            after = tuple(
                row
                for row in connection.exec_driver_sql(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).all()
                if row[1] != OWNER_TABLE
            )
        # The readiness function may install the separately reviewed audit indexes.
        assert all(row in after for row in before)
        snapshot = db._inspect_business_schema_manifest(engine)
        assert snapshot == db.CURRENT_BUSINESS_SCHEMA_MANIFEST
        db.ensure_business_db_ready(engine)
        assert db._inspect_business_schema_manifest(engine) == snapshot
    finally:
        engine.dispose()


def test_owner_table_ddl_failure_rolls_back_the_whole_upgrade(tmp_path: Path) -> None:
    """Even failure after CREATE executes leaves the previous schema unchanged."""
    engine = _file_engine(tmp_path / "failed-owner.sqlite")
    try:
        SQLModel.metadata.create_all(
            engine,
            tables=[
                table
                for name, table in SQLModel.metadata.tables.items()
                if name != OWNER_TABLE
            ],
        )
        before = db._inspect_business_schema_manifest(engine)

        def fail_after_owner_create(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _many: bool,
        ) -> None:
            if statement.lstrip().startswith("CREATE TABLE " + OWNER_TABLE):
                message = "injected owner DDL failure"
                raise RuntimeError(message)

        event.listen(engine, "after_cursor_execute", fail_after_owner_create)
        with pytest.raises(RuntimeError, match="injected owner DDL failure"):
            db.ensure_business_db_ready(engine)
        event.remove(engine, "after_cursor_execute", fail_after_owner_create)
        assert OWNER_TABLE not in inspect(engine).get_table_names()
        assert db._inspect_business_schema_manifest(engine) == before
    finally:
        engine.dispose()
