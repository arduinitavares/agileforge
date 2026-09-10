"""Isolation and resource ownership of the lazy per-test database."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, create_engine

import agile_sqlmodel
from models import db
from tests.conftest import fresh_test_engine

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_pure_test_does_not_construct_an_engine(request: pytest.FixtureRequest) -> None:
    """The always-installed database guard does not allocate unused databases."""
    assert "engine" not in request.node.funcargs


def test_implicit_engine_access_uses_the_same_function_fixture(
    request: pytest.FixtureRequest,
) -> None:
    """Implicit callers stay isolated even without an explicit engine argument."""
    assert "engine" not in request.node.funcargs
    implicit = agile_sqlmodel.get_engine()
    assert implicit is db.get_engine()
    assert implicit is request.getfixturevalue("engine")
    with implicit.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_fresh_engines_preserve_constraints_and_discard_all_rows() -> None:
    """Commit real rows, enforce an FK, then close the actual SQLite connection."""
    with (
        fresh_test_engine("sqlite:///:memory:") as first,
        first.begin() as connection,
    ):
        connection.exec_driver_sql(
            "CREATE TABLE fixture_parent (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE fixture_child "
            "(parent_id INTEGER REFERENCES fixture_parent(id))"
        )
        connection.exec_driver_sql("INSERT INTO fixture_parent VALUES (1)")
        connection.exec_driver_sql("INSERT INTO fixture_child VALUES (1)")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("INSERT INTO fixture_child VALUES (2)")
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        raw.execute("SELECT 1")
    with fresh_test_engine("sqlite:///:memory:") as second:
        assert second is not first
        with second.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM sqlite_master WHERE name LIKE 'fixture_%'"
                ).scalar_one()
                == 0
            )
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM projects").scalar_one()
                == 0
            )


def test_concurrent_implicit_callers_construct_one_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simultaneous first requests share one function-scoped database."""
    callers = Barrier(4)
    constructed: list[Engine] = []
    create_schema = SQLModel.metadata.create_all

    def count_construction(engine: Engine) -> None:
        constructed.append(engine)
        create_schema(engine)

    def get_concurrently() -> Engine:
        callers.wait(timeout=10)
        return agile_sqlmodel.get_engine()

    monkeypatch.setattr(SQLModel.metadata, "create_all", count_construction)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(get_concurrently) for _ in range(4)]
        engines = [future.result(timeout=10) for future in futures]
    assert len(constructed) == 1
    assert all(engine is constructed[0] for engine in engines)


def test_failed_schema_setup_closes_the_owned_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema construction error propagates and still disposes the pool."""
    connections: list[sqlite3.Connection] = []

    def fail_setup(engine: Engine) -> None:
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            assert isinstance(raw, sqlite3.Connection)
            connections.append(raw)
        message = "schema setup failed"
        raise RuntimeError(message)

    monkeypatch.setattr(SQLModel.metadata, "create_all", fail_setup)
    with (
        pytest.raises(RuntimeError, match="schema setup failed"),
        fresh_test_engine("sqlite:///:memory:"),
    ):
        pass
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_engine_lifecycles_do_not_accumulate_global_connection_listeners() -> None:
    """Fresh unrelated connections receive the same PRAGMAs after fixture use."""
    statements: list[str] = []

    def trace_connection() -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.set_trace_callback(statements.append)
        return connection

    probe = create_engine("sqlite://", creator=trace_connection)
    try:
        with probe.connect():
            pass
        before = statements.count("PRAGMA foreign_keys=ON")
        assert before >= 1
        probe.dispose()
        statements.clear()
        for _ in range(3):
            with fresh_test_engine("sqlite:///:memory:"):
                pass
        with probe.connect():
            pass
        assert statements.count("PRAGMA foreign_keys=ON") == before
    finally:
        probe.dispose()
