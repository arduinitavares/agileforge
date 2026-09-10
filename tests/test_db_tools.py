# tests/test_db_tools.py
"""Database fixture ownership and provider-free Project hierarchy tools."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

import agile_sqlmodel
from agile_sqlmodel import Project
from models import db
from models.core import Epic, Feature, Theme
from models.product_definition import VisionInterviewTurn
from tests.conftest import fresh_test_engine
from tests.vision_lineage_fixtures import seed_accepted_vision
from tools.db_tools import (
    CreateOrGetProjectInput,
    QueryProjectStructureFailure,
    QueryProjectStructureSuccess,
    create_or_get_project,
    persist_roadmap,
    query_project_structure,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


EXPECTED_FEATURE_COUNT = 2


def _created_roadmap_count(result: object, kind: str) -> int:
    """Read one successful roadmap collection without exporting a test-only type."""
    assert isinstance(result, dict)
    result_mapping = cast("dict[str, object]", result)
    assert result_mapping["success"] is True
    created = result_mapping["created"]
    assert isinstance(created, dict)
    created_mapping = cast("dict[str, object]", created)
    entries = created_mapping[kind]
    assert isinstance(entries, list)
    return len(entries)


def _roadmap() -> list[dict[str, object]]:
    return [
        {
            "quarter": "Q1",
            "theme_title": "Authentication",
            "theme_description": "User identity and access",
            "epics": [
                {
                    "epic_title": "Login System",
                    "epic_summary": "Email and OAuth login",
                    "features": [
                        {"title": "Email Login", "description": "Basic email/password"},
                        {"title": "OAuth 2.0", "description": "Third-party login"},
                    ],
                }
            ],
        }
    ]


def test_create_project_new(engine: Engine) -> None:
    """Create one Project through the retained project tool."""
    del engine

    result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Test Project",
            vision="To revolutionize testing",
            description=None,
        )
    )

    assert result == {
        "success": True,
        "project_id": 1,
        "action": "created",
        "message": "Created project 'Test Project' with ID 1",
    }


def test_create_project_existing(engine: Engine) -> None:
    """Keep the Project tool idempotent by name."""
    first = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Existing Project", vision=None, description=None
        )
    )
    second = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Existing Project", vision=None, description=None
        )
    )

    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert second["project_id"] == first["project_id"]
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1


def test_persist_roadmap_retains_only_theme_epic_feature_hierarchy(
    engine: Engine,
) -> None:
    """Persist the honest hierarchy without a false Feature-to-Story edge."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Roadmap Project", vision=None, description=None
        )
    )["project_id"]

    result = persist_roadmap(project_id, _roadmap())

    assert result["success"] is True
    assert _created_roadmap_count(result, "themes") == 1
    assert _created_roadmap_count(result, "epics") == 1
    assert _created_roadmap_count(result, "features") == EXPECTED_FEATURE_COUNT
    with Session(engine) as session:
        assert len(session.exec(select(Theme)).all()) == 1
        assert len(session.exec(select(Epic)).all()) == 1
        assert len(session.exec(select(Feature)).all()) == EXPECTED_FEATURE_COUNT


def test_query_project_structure_returns_only_current_hierarchy(engine: Engine) -> None:
    """Expose exactly Theme, Epic, and Feature records for a Project."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Query Project",
            vision="Test vision statement",
            description=None,
        )
    )["project_id"]
    with Session(engine) as session:
        seed_accepted_vision(
            session,
            project_id=project_id,
            statement="Test vision statement",
        )
    persist_roadmap(project_id, _roadmap())

    result = query_project_structure(project_id)

    assert result["success"] is True
    success = cast("QueryProjectStructureSuccess", result)
    structure = success["structure"]
    assert structure["project"]["name"] == "Query Project"
    assert structure["project"]["vision"] == "Test vision statement"
    feature = structure["themes"][0]["epics"][0]["features"][0]
    assert feature == {"id": 1, "title": "Email Login"}


def test_query_project_structure_fails_closed_on_ambiguous_vision(
    engine: Engine,
) -> None:
    """Reject two accepted Vision roots instead of selecting one arbitrarily."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Ambiguous Vision Project", vision=None, description=None
        )
    )["project_id"]
    with Session(engine) as session:
        seed_accepted_vision(session, project_id=project_id, statement="First root.")
        seed_accepted_vision(
            session,
            project_id=project_id,
            statement="Second root.",
            version_number=2,
        )

    result = query_project_structure(project_id)

    assert result["success"] is False
    failure = cast("QueryProjectStructureFailure", result)
    assert "Vision lineage is invalid" in failure["error"]


def test_query_project_structure_fails_closed_on_corrupt_vision_source_turn(
    engine: Engine,
) -> None:
    """Expose corrupt durable Vision source evidence through the tool envelope."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Corrupt Vision Source Project", vision=None, description=None
        )
    )["project_id"]
    with Session(engine) as session:
        artifact = seed_accepted_vision(
            session,
            project_id=project_id,
            statement="Trust immutable Vision evidence.",
        )
        turn = session.get(VisionInterviewTurn, artifact.source_interview_turn_id)
        assert turn is not None
        turn.output_fingerprint = "corrupt"
        session.add(turn)
        session.commit()

    result = query_project_structure(project_id)

    assert result["success"] is False
    failure = cast("QueryProjectStructureFailure", result)
    assert "Vision lineage is invalid" in failure["error"]


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


def test_engine_lifecycles_do_not_accumulate_global_connection_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture connections neither duplicate nor accumulate global PRAGMAs."""
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
        monkeypatch.setattr(
            "tests.conftest.create_engine",
            partial(create_engine, creator=trace_connection),
        )
        for _ in range(3):
            with fresh_test_engine("sqlite:///:memory:"):
                pass
            assert statements.count("PRAGMA foreign_keys=ON") == before
            statements.clear()
        with probe.connect():
            pass
        assert statements.count("PRAGMA foreign_keys=ON") == before
    finally:
        probe.dispose()
