"""Exact-baseline, atomic schema upgrade tests for retry persistence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, inspect
from sqlmodel import SQLModel, create_engine

from models.db import (
    CURRENT_BUSINESS_SCHEMA_MANIFEST,
    PRE_RETRY_BUSINESS_SCHEMA_MANIFEST,
    _inspect_business_schema_manifest,
    ensure_business_db_ready,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


RETRY_TABLES = frozenset(
    {
        "sprint_retry_attempts",
        "sprint_retry_story_states",
        "sprint_retry_task_states",
        "sprint_retry_starts",
        "sprint_retry_task_evidence",
        "sprint_retry_story_closures",
        "sprint_retry_reviews",
        "sprint_retry_closures",
        "sprint_retry_triage",
    }
)
_SECOND_RETRY_DDL = 2
_INJECTED_DDL_FAILURE = "injected second retry DDL failure"


def _file_engine(path: Path) -> Engine:
    return create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )


def _pre_retry_schema(engine: Engine) -> None:
    """Create the reviewed pre-retry metadata set without retry tables."""
    tables = [
        SQLModel.metadata.tables[name]
        for name in PRE_RETRY_BUSINESS_SCHEMA_MANIFEST.table_names
    ]
    SQLModel.metadata.create_all(engine, tables=tables)


def _snapshot_original_rows(engine: Engine) -> tuple[tuple[object, ...], ...]:
    with engine.connect() as connection:
        return tuple(
            connection.exec_driver_sql(
                "SELECT project_id, name, description FROM projects ORDER BY project_id"
            ).all()
        )


def test_exact_pre_retry_schema_upgrades_without_rewriting_existing_rows(
    tmp_path: Path,
) -> None:
    """A migration that changes old rows or misses a retry table is unsafe."""
    engine = _file_engine(tmp_path / "baseline-upgrade.sqlite")
    _pre_retry_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO projects (name, description, created_at, updated_at) "
            "VALUES ('Synthetic retry migration', 'kept verbatim', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    before = _snapshot_original_rows(engine)

    ensure_business_db_ready(engine)

    assert _snapshot_original_rows(engine) == before
    assert _inspect_business_schema_manifest(engine) == CURRENT_BUSINESS_SCHEMA_MANIFEST


def test_partial_retry_schema_fails_closed(tmp_path: Path) -> None:
    """A missing retry table must never be repaired by inference."""
    engine = _file_engine(tmp_path / "partial-retry.sqlite")
    _pre_retry_schema(engine)
    SQLModel.metadata.create_all(
        engine,
        tables=[SQLModel.metadata.tables["sprint_retry_attempts"]],
    )

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        ensure_business_db_ready(engine)


def test_unknown_extra_column_fails_closed(tmp_path: Path) -> None:
    """Adding an unreviewed column must not be mistaken for the baseline."""
    engine = _file_engine(tmp_path / "unknown-column.sqlite")
    _pre_retry_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE projects ADD COLUMN retry_guess TEXT")

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        ensure_business_db_ready(engine)


def test_failure_after_first_retry_ddl_rolls_back_every_retry_table(
    tmp_path: Path,
) -> None:
    """A failed additive upgrade cannot leave a partially durable schema."""
    engine = _file_engine(tmp_path / "rollback.sqlite")
    _pre_retry_schema(engine)
    created_retry_tables = 0

    @event.listens_for(engine, "before_cursor_execute")
    def fail_after_first_retry_table(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal created_retry_tables
        if "CREATE TABLE sprint_retry" not in statement:
            return
        created_retry_tables += 1
        if created_retry_tables == _SECOND_RETRY_DDL:
            raise RuntimeError(_INJECTED_DDL_FAILURE)

    with pytest.raises(RuntimeError, match=_INJECTED_DDL_FAILURE):
        ensure_business_db_ready(engine)

    assert not (set(inspect(engine).get_table_names()) & RETRY_TABLES)


def test_two_connections_upgrade_one_baseline_to_one_complete_schema(
    tmp_path: Path,
) -> None:
    """Competing upgrades must serialize to a single complete current schema."""
    path = tmp_path / "concurrent-upgrade.sqlite"
    baseline = _file_engine(path)
    _pre_retry_schema(baseline)
    baseline.dispose()

    def upgrade() -> None:
        engine = _file_engine(path)
        try:
            ensure_business_db_ready(engine)
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _value: upgrade(), range(2)))

    verified = _file_engine(path)
    try:
        assert (
            _inspect_business_schema_manifest(verified)
            == CURRENT_BUSINESS_SCHEMA_MANIFEST
        )
    finally:
        verified.dispose()
