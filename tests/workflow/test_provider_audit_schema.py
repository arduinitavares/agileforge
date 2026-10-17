# tests/workflow/test_provider_audit_schema.py
"""Exact-baseline atomic installation of provider audit lookup indexes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, inspect
from sqlmodel import SQLModel, create_engine

from models.db import (
    CURRENT_BUSINESS_SCHEMA_MANIFEST,
    _inspect_business_schema_manifest,
    ensure_business_db_ready,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_FIXTURES: Path = Path(__file__).parents[1] / "fixtures"
_PRE_RETRY: Path = _FIXTURES / "issue_260" / "pre_retry_business_schema_da3dbf63.sql"
_PRIOR_CURRENT: Path = (
    _FIXTURES / "issue_230" / "prior_current_retry_additions_8b4ee1f.sql"
)
_EXPECTED_INDEXES: dict[str, str] = {
    "ix_workflow_events_provider_action": (
        "CREATE INDEX ix_workflow_events_provider_action ON workflow_events "
        "(json_extract(CASE WHEN json_valid(event_metadata) = 1 "
        "THEN event_metadata ELSE '{}' END, '$.action_id'), project_id, event_id) "
        "WHERE event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED')"
    ),
    "ix_workflow_events_provider_call": (
        "CREATE INDEX ix_workflow_events_provider_call ON workflow_events "
        "(json_extract(CASE WHEN json_valid(event_metadata) = 1 "
        "THEN event_metadata ELSE '{}' END, '$.call_id'), project_id, event_id) "
        "WHERE event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED')"
    ),
    "ix_workflow_events_provider_invalid": (
        "CREATE INDEX ix_workflow_events_provider_invalid ON workflow_events "
        "(project_id, event_id) WHERE event_type IN "
        "('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED') "
        "AND coalesce(json_valid(event_metadata), 0) != 1"
    ),
}


def _engine(path: Path) -> Engine:
    return create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )


def _baseline(engine: Engine, baseline: str) -> None:
    """Compose independently captured schemas, never the modified metadata."""
    schema = _PRE_RETRY.read_text(encoding="utf-8")
    if baseline == "prior_current":
        schema += "\n" + _PRIOR_CURRENT.read_text(encoding="utf-8")
    with engine.begin() as connection:
        connection.connection.executescript(schema)
        connection.exec_driver_sql(
            "INSERT INTO projects (name) VALUES (?)", ("History",)
        )
        connection.exec_driver_sql(
            "INSERT INTO workflow_events (event_type, project_id, event_metadata) "
            "VALUES (?, ?, ?), (?, ?, ?)",
            (
                "PROVIDER_TRY_STARTED",
                1,
                '{"action_id":"old","call_id":"old"}',
                "PROVIDER_TRY_STARTED",
                None,
                "{damaged foreign metadata",
            ),
        )


def _indexes(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return {
            str(row[0]): str(row[1])
            for row in connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND name IN (?, ?, ?)",
                tuple(_EXPECTED_INDEXES),
            ).all()
        }


def _rows(
    engine: Engine, tables: tuple[str, ...]
) -> dict[str, tuple[tuple[object, ...], ...]]:
    with engine.connect() as connection:
        return {
            table: tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608  # nosec B608
                ).all()
            )
            for table in tables
        }


def _schema(engine: Engine) -> tuple[tuple[object, ...], ...]:
    with engine.connect() as connection:
        return tuple(
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).all()
        )


@pytest.mark.parametrize("baseline", ["pre_retry", "prior_current"])
def test_supported_baselines_install_indexes_without_rewriting_rows(
    tmp_path: Path, baseline: str
) -> None:
    """Reviewed old schemas acquire the complete lookup set in one startup."""
    engine = _engine(tmp_path / "upgrade.sqlite3")
    try:
        _baseline(engine, baseline)
        tables = tuple(sorted(inspect(engine).get_table_names()))
        before = _rows(engine, tables)
        assert _indexes(engine) == {}

        ensure_business_db_ready(engine)

        assert _indexes(engine) == _EXPECTED_INDEXES
        assert _rows(engine, tables) == before
        assert (
            _inspect_business_schema_manifest(engine)
            == CURRENT_BUSINESS_SCHEMA_MANIFEST
        )
    finally:
        engine.dispose()


def test_pre_retry_with_complete_provider_indexes_is_not_a_supported_baseline(
    tmp_path: Path,
) -> None:
    """Only the captured pre-retry state with no owned indexes can be upgraded."""
    engine = _engine(tmp_path / "unsupported-chain.sqlite3")
    try:
        _baseline(engine, "pre_retry")
        with engine.begin() as connection:
            for ddl in _EXPECTED_INDEXES.values():
                connection.exec_driver_sql(ddl)
        before = _indexes(engine)
        tables = tuple(inspect(engine).get_table_names())
        with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
            ensure_business_db_ready(engine)
        assert _indexes(engine) == before
        assert tuple(inspect(engine).get_table_names()) == tables
    finally:
        engine.dispose()


@pytest.mark.parametrize("creation", ["metadata", "startup"])
def test_fresh_schema_and_noop_reopen_have_exact_provider_indexes(
    tmp_path: Path, creation: str
) -> None:
    """Fresh metadata and startup agree; complete current startup emits no DDL."""
    engine = _engine(tmp_path / "fresh.sqlite3")
    try:
        if creation == "metadata":
            SQLModel.metadata.create_all(engine)
        else:
            ensure_business_db_ready(engine)
        assert _indexes(engine) == _EXPECTED_INDEXES
        before = _schema(engine)
        ddl: list[str] = []

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().startswith(("CREATE", "ALTER", "DROP")):
                ddl.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            ensure_business_db_ready(engine)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert ddl == []
        assert _schema(engine) == before
        assert (
            _inspect_business_schema_manifest(engine)
            == CURRENT_BUSINESS_SCHEMA_MANIFEST
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("baseline", ["pre_retry", "prior_current"])
@pytest.mark.parametrize("fail_after", [1, 2])
def test_failed_index_install_rolls_back_entire_startup(
    tmp_path: Path, baseline: str, fail_after: int
) -> None:
    """Failure after either index DDL leaves exact old rows, tables, and indexes."""
    engine = _engine(tmp_path / "atomic.sqlite3")
    try:
        _baseline(engine, baseline)
        tables = tuple(sorted(inspect(engine).get_table_names()))
        before_schema = _schema(engine)
        before_rows = _rows(engine, tables)
        created = 0

        def fail(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal created
            if statement.startswith("CREATE INDEX ix_workflow_events_provider_"):
                created += 1
                if created == fail_after:
                    message = "injected completed provider index DDL failure"
                    raise RuntimeError(message)

        event.listen(engine, "after_cursor_execute", fail)
        try:
            with pytest.raises(RuntimeError, match="completed provider index DDL"):
                ensure_business_db_ready(engine)
        finally:
            event.remove(engine, "after_cursor_execute", fail)
        assert created == fail_after
        assert _indexes(engine) == {}
        assert _schema(engine) == before_schema
        assert _rows(engine, tables) == before_rows
        ensure_business_db_ready(engine)
        assert _indexes(engine) == _EXPECTED_INDEXES
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "damage",
    ["partial", "path_case", "enum_case", "columns", "renamed", "extra", "uppercase"],
)
def test_partial_or_drifted_owned_indexes_fail_before_startup_ddl(
    tmp_path: Path, damage: str
) -> None:
    """Case-sensitive paths and predicates cannot masquerade as reviewed indexes."""
    engine = _engine(tmp_path / "drift.sqlite3")
    try:
        _baseline(engine, "prior_current")
        definitions = dict(_EXPECTED_INDEXES)
        action = "ix_workflow_events_provider_action"
        if damage == "partial":
            definitions = {action: definitions[action]}
        elif damage == "path_case":
            definitions[action] = definitions[action].replace(
                "$.action_id", "$.Action_Id"
            )
        elif damage == "enum_case":
            definitions[action] = definitions[action].replace(
                "PROVIDER_TRY_STARTED", "provider_try_started"
            )
        elif damage == "columns":
            definitions[action] = definitions[action].replace(
                "project_id, event_id", "event_id, project_id"
            )
        elif damage == "renamed":
            definitions[action] = definitions[action].replace(
                action, action + "_renamed"
            )
        elif damage == "uppercase":
            definitions = {
                name: ddl.replace(name, name.upper())
                for name, ddl in definitions.items()
            }
        else:
            definitions["extra"] = (
                "CREATE INDEX ix_workflow_events_provider_extra "
                "ON workflow_events(event_id)"
            )
        with engine.begin() as connection:
            for ddl in definitions.values():
                connection.exec_driver_sql(ddl)
        before = _schema(engine)
        ddl: list[str] = []

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().startswith(("CREATE", "ALTER", "DROP")):
                ddl.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
                ensure_business_db_ready(engine)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert ddl == []
        assert _schema(engine) == before
    finally:
        engine.dispose()


def test_unknown_structure_is_rejected_before_provider_index_install(
    tmp_path: Path,
) -> None:
    """An absent lookup set never authorizes an unknown business schema."""
    engine = _engine(tmp_path / "unknown.sqlite3")
    try:
        _baseline(engine, "prior_current")
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE projects ADD COLUMN guessed TEXT")
        before = _schema(engine)
        with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
            ensure_business_db_ready(engine)
        assert _indexes(engine) == {}
        assert _schema(engine) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("baseline", ["pre_retry", "prior_current"])
def test_competing_startups_install_one_complete_provider_index_set(
    tmp_path: Path, baseline: str
) -> None:
    """Independent engines serialize migration and agree on one complete state."""
    path = tmp_path / "competing.sqlite3"
    engine = _engine(path)
    _baseline(engine, baseline)
    engine.dispose()

    def upgrade(_number: int) -> None:
        worker = _engine(path)
        try:
            ensure_business_db_ready(worker)
        finally:
            worker.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(upgrade, range(2)))
    verified = _engine(path)
    try:
        assert _indexes(verified) == _EXPECTED_INDEXES
        assert (
            _inspect_business_schema_manifest(verified)
            == CURRENT_BUSINESS_SCHEMA_MANIFEST
        )
    finally:
        verified.dispose()
