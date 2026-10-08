# tests/workflow/test_task_completion_revision_schema.py
"""Exact, atomic revision-evidence upgrades preserve immutable legacy history."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, inspect
from sqlmodel import Session, select

from models.db import (
    CURRENT_BUSINESS_SCHEMA_MANIFEST,
    PRE_RETRY_BUSINESS_SCHEMA_MANIFEST,
    PRE_REVISION_BUSINESS_SCHEMA_MANIFEST,
    _inspect_business_schema_manifest,
    ensure_business_db_ready,
)
from models.sprint_retry import SprintRetryTaskEvidence
from models.workflow import TaskCompletionEvidence
from repositories.workflow import WorkflowFactRepository
from tests.workflow.test_sprint_retry_schema import (
    _copy_original_history,
    _file_engine,
    _frozen_completed_history,
    _original_table_columns,
    _pre_retry_schema,
    _snapshot_original_rows,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


_EVIDENCE_TABLES: tuple[str, ...] = (
    "task_completion_evidence",
    "sprint_retry_task_evidence",
)
_INJECTED_COLUMN_FAILURE: str = "injected second revision column failure"


def _assert_nullable_text_evidence(engine: Engine) -> None:
    """Both completion records must store optional payloads without backfill."""
    inspector = inspect(engine)
    for table in _EVIDENCE_TABLES:
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        assert "repository_evidence_json" in columns
        evidence = columns["repository_evidence_json"]
        assert str(evidence["type"]) == "TEXT"
        assert evidence["nullable"] is True
        assert evidence["default"] is None
        with engine.connect() as connection:
            values = (
                connection.exec_driver_sql(
                    f"SELECT repository_evidence_json FROM {table}"  # noqa: S608  # nosec B608
                )
                .scalars()
                .all()
            )
            assert all(value is None for value in values)


def _schema_sql(engine: Engine) -> tuple[tuple[object, ...], ...]:
    """Retain complete DDL/index bytes to detect a partial durable upgrade."""
    with engine.connect() as connection:
        return tuple(
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).all()
        )


def test_pre_revision_upgrade_preserves_existing_completion_history(
    tmp_path: Path,
) -> None:
    """Adding columns must preserve every old payload, hash, log, and receipt."""
    engine = _frozen_completed_history(tmp_path / "pre-revision.sqlite")
    old_columns = _original_table_columns(engine)
    before = _snapshot_original_rows(engine, old_columns)
    assert (
        _inspect_business_schema_manifest(engine)
        == PRE_REVISION_BUSINESS_SCHEMA_MANIFEST
    )
    assert before["task_completion_evidence"]
    assert before["sprint_retry_task_evidence"]
    assert before["workflow_transition_receipts"]

    ensure_business_db_ready(engine)

    assert _snapshot_original_rows(engine, old_columns) == before
    assert _inspect_business_schema_manifest(engine) == CURRENT_BUSINESS_SCHEMA_MANIFEST
    _assert_nullable_text_evidence(engine)
    with Session(engine) as session:
        original = session.exec(select(TaskCompletionEvidence)).one()
        retry = session.exec(select(SprintRetryTaskEvidence)).one()
        assert original.repository_evidence_json is None
        assert retry.repository_evidence_json is None
        snapshot = WorkflowFactRepository(session).load(original.project_id)
    assert snapshot.task_completions
    assert snapshot.story_completions
    assert snapshot.sprint_closures
    assert snapshot.post_sprint_triage
    retry_fact = snapshot.sprint_retries[0]
    assert retry_fact.task_completions
    assert retry_fact.story_completions
    assert retry_fact.sprint_closures
    assert retry_fact.post_sprint_triage
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_pre_retry_upgrade_reaches_revision_schema(tmp_path: Path) -> None:
    """The older supported route must reach the same complete fresh schema."""
    engine = _file_engine(tmp_path / "pre-retry.sqlite")
    _pre_retry_schema(engine)
    assert (
        _inspect_business_schema_manifest(engine) == PRE_RETRY_BUSINESS_SCHEMA_MANIFEST
    )
    source = _frozen_completed_history(tmp_path / "source.sqlite")
    try:
        _copy_original_history(source, engine)
    finally:
        source.dispose()
    old_columns = _original_table_columns(engine)
    before = _snapshot_original_rows(engine, old_columns)

    ensure_business_db_ready(engine)

    assert _snapshot_original_rows(engine, old_columns) == before
    _assert_nullable_text_evidence(engine)
    fresh = _file_engine(tmp_path / "fresh.sqlite")
    try:
        ensure_business_db_ready(fresh)
        assert (
            _inspect_business_schema_manifest(engine)
            == _inspect_business_schema_manifest(fresh)
            == CURRENT_BUSINESS_SCHEMA_MANIFEST
        )
    finally:
        fresh.dispose()


def test_failed_second_column_upgrade_is_atomic(tmp_path: Path) -> None:
    """Failure after one ALTER must roll back its column and preserve rows."""
    engine = _frozen_completed_history(tmp_path / "rollback.sqlite")
    old_columns = _original_table_columns(engine)
    before_rows = _snapshot_original_rows(engine, old_columns)
    before_sql = _schema_sql(engine)
    first_column_added = False

    @event.listens_for(engine, "after_cursor_execute")
    def observe_first_column(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal first_column_added
        if statement.startswith("ALTER TABLE task_completion_evidence ADD COLUMN"):
            first_column_added = True

    @event.listens_for(engine, "before_cursor_execute")
    def fail_second_column(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.startswith("ALTER TABLE sprint_retry_task_evidence ADD COLUMN"):
            raise RuntimeError(_INJECTED_COLUMN_FAILURE)

    with pytest.raises(RuntimeError, match=_INJECTED_COLUMN_FAILURE):
        ensure_business_db_ready(engine)

    assert first_column_added is True
    assert _schema_sql(engine) == before_sql
    assert _snapshot_original_rows(engine, old_columns) == before_rows


def test_pre_retry_column_failure_rolls_back_retry_tables(tmp_path: Path) -> None:
    """Retry tables and original evidence ALTER must share one atomic upgrade."""
    engine = _file_engine(tmp_path / "pre-retry-rollback.sqlite")
    _pre_retry_schema(engine)
    before_sql = _schema_sql(engine)
    original_column_added = False

    @event.listens_for(engine, "after_cursor_execute")
    def fail_after_original_column(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal original_column_added
        if statement.startswith("ALTER TABLE task_completion_evidence ADD COLUMN"):
            original_column_added = True
            raise RuntimeError(_INJECTED_COLUMN_FAILURE)

    with pytest.raises(RuntimeError, match=_INJECTED_COLUMN_FAILURE):
        ensure_business_db_ready(engine)

    assert original_column_added is True
    assert _schema_sql(engine) == before_sql
    assert (
        _inspect_business_schema_manifest(engine) == PRE_RETRY_BUSINESS_SCHEMA_MANIFEST
    )


@pytest.mark.parametrize(
    ("baseline", "mutation"),
    [
        ("pre_revision", "ALTER TABLE projects ADD COLUMN revision_guess TEXT"),
        (
            "pre_revision",
            "ALTER TABLE task_completion_evidence "
            "ADD COLUMN repository_evidence_json TEXT",
        ),
        (
            "pre_revision",
            "ALTER TABLE sprint_retry_task_evidence "
            "ADD COLUMN repository_evidence_json TEXT",
        ),
        ("pre_revision", "DROP TABLE sprint_retry_triage"),
        (
            "pre_retry",
            "ALTER TABLE task_completion_evidence "
            "ADD COLUMN repository_evidence_json TEXT",
        ),
    ],
)
def test_unknown_or_partial_schema_fails_closed(
    tmp_path: Path,
    baseline: str,
    mutation: str,
) -> None:
    """Neither unknown columns nor mixed upgrade layouts authorize repair."""
    if baseline == "pre_revision":
        engine = _frozen_completed_history(tmp_path / "unsupported.sqlite")
    else:
        engine = _file_engine(tmp_path / "unsupported.sqlite")
        _pre_retry_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(mutation)
    before_sql = _schema_sql(engine)
    old_columns = _original_table_columns(engine)
    before_rows = _snapshot_original_rows(engine, old_columns)

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        ensure_business_db_ready(engine)

    assert _schema_sql(engine) == before_sql
    assert _snapshot_original_rows(engine, old_columns) == before_rows


def test_concurrent_startup_upgrades_once(tmp_path: Path) -> None:
    """Competing startups must add each column once and retain legacy rows."""
    database_path = tmp_path / "concurrent.sqlite"
    baseline = _frozen_completed_history(database_path)
    old_columns = _original_table_columns(baseline)
    before = _snapshot_original_rows(baseline, old_columns)
    baseline.dispose()
    ready = Barrier(2)
    additions: list[str] = []
    additions_lock = Lock()

    def upgrade() -> None:
        engine = _file_engine(database_path)

        @event.listens_for(engine, "after_cursor_execute")
        def record_column_addition(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.startswith("ALTER TABLE"):
                with additions_lock:
                    additions.append(statement)

        try:
            ready.wait(timeout=10)
            ensure_business_db_ready(engine)
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(upgrade) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    assert sorted(additions) == sorted(
        f"ALTER TABLE {table} ADD COLUMN repository_evidence_json TEXT"
        for table in _EVIDENCE_TABLES
    )
    verified = _file_engine(database_path)
    try:
        assert (
            _inspect_business_schema_manifest(verified)
            == CURRENT_BUSINESS_SCHEMA_MANIFEST
        )
        assert _snapshot_original_rows(verified, old_columns) == before
        _assert_nullable_text_evidence(verified)
    finally:
        verified.dispose()


@pytest.mark.parametrize("table", _EVIDENCE_TABLES)
@pytest.mark.parametrize(
    "definition",
    [
        "INTEGER",
        "TEXT DEFAULT '{}'",
        "TEXT DEFAULT 'NULL'",
        "TEXT NOT NULL DEFAULT '{}'",
        "TEXT GENERATED ALWAYS AS (NULL) VIRTUAL",
    ],
)
def test_invalid_evidence_column_contract_fails_closed(
    tmp_path: Path,
    table: str,
    definition: str,
) -> None:
    """Wrong types, defaults, or unwritable generated columns must be rejected."""
    engine = _frozen_completed_history(tmp_path / "invalid-column.sqlite")
    with engine.begin() as connection:
        for evidence_table in _EVIDENCE_TABLES:
            column_definition = definition if evidence_table == table else "TEXT"
            connection.exec_driver_sql(
                f"ALTER TABLE {evidence_table} ADD COLUMN "  # nosec B608
                f"repository_evidence_json {column_definition}"
            )
    before_sql = _schema_sql(engine)
    old_columns = _original_table_columns(engine)
    before_rows = _snapshot_original_rows(engine, old_columns)

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        ensure_business_db_ready(engine)

    assert _schema_sql(engine) == before_sql
    assert _snapshot_original_rows(engine, old_columns) == before_rows


def test_fresh_schema_and_current_startup_are_complete(tmp_path: Path) -> None:
    """Fresh creation and subsequent startup must keep the same exact schema."""
    engine = _file_engine(tmp_path / "fresh.sqlite")
    ensure_business_db_ready(engine)
    _assert_nullable_text_evidence(engine)
    before_sql = _schema_sql(engine)

    ensure_business_db_ready(engine)

    assert _schema_sql(engine) == before_sql
    assert _inspect_business_schema_manifest(engine) == CURRENT_BUSINESS_SCHEMA_MANIFEST
