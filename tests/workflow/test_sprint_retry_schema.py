"""Exact-baseline, atomic schema upgrade tests for retry persistence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, inspect
from sqlmodel import Session, SQLModel, create_engine

from models.db import (
    CURRENT_BUSINESS_SCHEMA_MANIFEST,
    _inspect_business_schema_manifest,
    ensure_business_db_ready,
)
from repositories.workflow import WorkflowFactRepository
from tests.workflow.test_execution_transitions import (
    _complete_execution_sprint,
    _triage_execution_sprint,
)

if TYPE_CHECKING:
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
_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "issue_260"
    / "pre_retry_business_schema_da3dbf63.sql"
)
_SECOND_RETRY_DDL = 2
_INJECTED_DDL_FAILURE = "injected second retry DDL failure"


def _file_engine(path: Path) -> Engine:
    return create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )


def _pre_retry_schema(engine: Engine) -> None:
    """Create only the independently frozen pre-retry SQLite schema."""
    with engine.begin() as connection:
        connection.connection.executescript(_FIXTURE.read_text(encoding="utf-8"))


def _original_table_names(engine: Engine) -> tuple[str, ...]:
    """Read the pre-retry table set from the frozen schema, never metadata."""
    return tuple(sorted(inspect(engine).get_table_names()))


def _snapshot_original_rows(
    engine: Engine,
    tables: tuple[str, ...],
) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Capture every persisted column of every pre-retry table."""
    with engine.connect() as connection:
        return {
            table: tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608
                ).all()
            )
            for table in tables
        }


def _copy_original_history(source: Engine, target: Engine) -> None:
    """Copy a complete real execution history into the frozen old database."""
    tables = _original_table_names(target)
    with source.connect() as source_connection, target.begin() as target_connection:
        target_connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
        for table in tables:
            rows = source_connection.exec_driver_sql(
                f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608
            ).all()
            if not rows:
                continue
            columns = tuple(row["name"] for row in inspect(source).get_columns(table))
            quoted_columns = ", ".join(columns)
            placeholders = ", ".join("?" for _column in columns)
            statement = (
                f"INSERT INTO {table} ({quoted_columns}) VALUES ({placeholders})"  # noqa: S608
            )
            for row in rows:
                target_connection.exec_driver_sql(statement, tuple(row))


def _real_completed_history(path: Path) -> Engine:
    """Create valid original history through the normal domain transitions."""
    source = _file_engine(path)
    SQLModel.metadata.create_all(source)
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(source)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with source.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    return source


def test_exact_pre_retry_schema_upgrades_without_rewriting_existing_rows(
    tmp_path: Path,
) -> None:
    """A migration that changes old rows or misses a retry table is unsafe."""
    engine = _file_engine(tmp_path / "baseline-upgrade.sqlite")
    _pre_retry_schema(engine)
    source = _real_completed_history(tmp_path / "source-history.sqlite")
    try:
        _copy_original_history(source, engine)
    finally:
        source.dispose()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    original_tables = _original_table_names(engine)
    before = _snapshot_original_rows(engine, original_tables)

    ensure_business_db_ready(engine)

    assert _snapshot_original_rows(engine, original_tables) == before
    assert _inspect_business_schema_manifest(engine) == CURRENT_BUSINESS_SCHEMA_MANIFEST
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(1)
    assert snapshot.task_completions
    assert snapshot.story_completions
    assert snapshot.post_sprint_triage


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
