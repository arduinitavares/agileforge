"""Regression tests for nullable planned Sprint dates."""

from collections.abc import Iterator
from sqlite3 import Connection as SQLiteConnection

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, create_engine, select

from db.migrations import migrate_sprint_nullable_dates
from models.core import Product, Sprint, Team


def _sqlite_engine() -> Engine:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(
        dbapi_connection: SQLiteConnection,
        _connection_record: object,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _create_legacy_sprint_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE products (
                    product_id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE teams (
                    team_id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE user_stories (
                    story_id INTEGER PRIMARY KEY,
                    title VARCHAR NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE sprints (
                    sprint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal TEXT,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    status VARCHAR NOT NULL,
                    started_at DATETIME,
                    completed_at DATETIME,
                    close_snapshot_json TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    product_id INTEGER NOT NULL REFERENCES products(product_id),
                    team_id INTEGER NOT NULL REFERENCES teams(team_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE sprint_stories (
                    sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id),
                    story_id INTEGER NOT NULL REFERENCES user_stories(story_id),
                    added_at DATETIME NOT NULL,
                    PRIMARY KEY (sprint_id, story_id)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_sprints_status ON sprints (status)"))
        conn.execute(text("CREATE INDEX ix_sprints_product_id ON sprints (product_id)"))
        conn.execute(text("CREATE INDEX ix_sprints_team_id ON sprints (team_id)"))
        conn.execute(
            text(
                """
                INSERT INTO products(product_id, name)
                VALUES (101, 'Widget Platform')
                """
            )
        )
        conn.execute(text("INSERT INTO teams(team_id, name) VALUES (202, 'Core Team')"))
        conn.execute(
            text(
                """
                INSERT INTO user_stories(story_id, title)
                VALUES (303, 'Legacy story')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO sprints(
                    sprint_id,
                    goal,
                    start_date,
                    end_date,
                    status,
                    started_at,
                    completed_at,
                    close_snapshot_json,
                    created_at,
                    updated_at,
                    product_id,
                    team_id
                )
                VALUES (
                    7,
                    'Legacy sprint',
                    '2026-06-01',
                    '2026-06-14',
                    'Planned',
                    NULL,
                    NULL,
                    '{"state":"planned"}',
                    '2026-05-30 10:00:00',
                    '2026-05-30 11:00:00',
                    101,
                    202
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO sprint_stories(sprint_id, story_id, added_at)
                VALUES (7, 303, '2026-05-30 12:00:00')
                """
            )
        )


def _sprint_date_notnull_flags(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info('sprints')")).mappings().all()
    return {
        str(row["name"]): int(row["notnull"])
        for row in rows
        if row["name"] in {"start_date", "end_date"}
    }


def _sprint_index_names(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA index_list('sprints')")).mappings().all()
    return {str(row["name"]) for row in rows}


@pytest.fixture(name="legacy_engine")
def legacy_engine_fixture() -> Iterator[Engine]:
    engine = _sqlite_engine()
    _create_legacy_sprint_schema(engine)
    yield engine
    engine.dispose()


def test_fresh_sprint_persists_without_planned_dates() -> None:
    engine = _sqlite_engine()
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        product = Product(name="Widget Platform")
        team = Team(name="Core Team")
        session.add(product)
        session.add(team)
        session.flush()
        assert product.product_id is not None
        assert team.team_id is not None

        sprint = Sprint(
            goal="Capacity-only planning",
            start_date=None,
            end_date=None,
            product_id=product.product_id,
            team_id=team.team_id,
        )
        session.add(sprint)
        session.commit()
        session.refresh(sprint)

    with Session(engine) as session:
        persisted = session.exec(select(Sprint)).one()
        assert persisted.start_date is None
        assert persisted.end_date is None


def test_old_not_null_sprint_dates_migrate_to_nullable(
    legacy_engine: Engine,
) -> None:
    actions = migrate_sprint_nullable_dates(legacy_engine)

    assert actions == ["migrated sprints table: made start_date and end_date nullable"]
    assert _sprint_date_notnull_flags(legacy_engine) == {
        "start_date": 0,
        "end_date": 0,
    }

    with legacy_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO sprints(
                    goal,
                    start_date,
                    end_date,
                    status,
                    created_at,
                    updated_at,
                    product_id,
                    team_id
                )
                VALUES (
                    'Dateless planned sprint',
                    NULL,
                    NULL,
                    'Planned',
                    '2026-06-12 09:00:00',
                    '2026-06-12 09:00:00',
                    101,
                    202
                )
                """
            )
        )


def test_migration_preserves_sprint_rows_and_product_team_foreign_keys(
    legacy_engine: Engine,
) -> None:
    migrate_sprint_nullable_dates(legacy_engine)

    with legacy_engine.connect() as conn:
        row = (
            conn.execute(text("SELECT * FROM sprints WHERE sprint_id = 7"))
            .mappings()
            .one()
        )
        foreign_keys = (
            conn.execute(text("PRAGMA foreign_key_list('sprints')")).mappings().all()
        )

    assert row["goal"] == "Legacy sprint"
    assert str(row["start_date"]) == "2026-06-01"
    assert str(row["end_date"]) == "2026-06-14"
    assert row["status"] == "Planned"
    assert row["close_snapshot_json"] == '{"state":"planned"}'
    assert row["product_id"] == 101
    assert row["team_id"] == 202
    assert {
        (foreign_key["from"], foreign_key["table"], foreign_key["to"])
        for foreign_key in foreign_keys
    } == {
        ("product_id", "products", "product_id"),
        ("team_id", "teams", "team_id"),
    }

    with pytest.raises(IntegrityError):
        with legacy_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sprints(
                        goal,
                        start_date,
                        end_date,
                        status,
                        created_at,
                        updated_at,
                        product_id,
                        team_id
                    )
                    VALUES (
                        'Invalid parent',
                        NULL,
                        NULL,
                        'Planned',
                        '2026-06-12 09:00:00',
                        '2026-06-12 09:00:00',
                        999,
                        202
                    )
                    """
                )
            )


def test_migration_preserves_sprint_story_links_and_indexes(
    legacy_engine: Engine,
) -> None:
    migrate_sprint_nullable_dates(legacy_engine)

    with legacy_engine.connect() as conn:
        link = (
            conn.execute(text("SELECT * FROM sprint_stories WHERE sprint_id = 7"))
            .mappings()
            .one()
        )
        violations = conn.execute(text("PRAGMA foreign_key_check")).all()

    assert link["story_id"] == 303
    assert str(link["added_at"]) == "2026-05-30 12:00:00"
    assert violations == []
    assert {
        "ix_sprints_status",
        "ix_sprints_product_id",
        "ix_sprints_team_id",
    }.issubset(_sprint_index_names(legacy_engine))


def test_migration_is_idempotent_after_rebuild(legacy_engine: Engine) -> None:
    assert migrate_sprint_nullable_dates(legacy_engine) == [
        "migrated sprints table: made start_date and end_date nullable"
    ]

    assert migrate_sprint_nullable_dates(legacy_engine) == []
