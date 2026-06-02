"""Tests for active backlog reset archive-column migrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import create_engine, inspect, text

from db.migrations import ensure_schema_current
from models.core import UserStory

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


ARCHIVE_COLUMNS = {
    "archived_reason",
    "archived_at",
    "archived_by",
    "archive_reset_attempt_id",
    "archive_previous_status",
}


def _create_legacy_runtime_schema() -> Engine:
    """Create a small pre-reset runtime schema for additive migration tests."""
    engine = create_engine("sqlite://")
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
                CREATE TABLE user_stories (
                    story_id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL,
                    title VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE sprints (
                    sprint_id INTEGER PRIMARY KEY,
                    goal TEXT,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    product_id INTEGER NOT NULL,
                    team_id INTEGER NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE tasks (
                    task_id INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    status VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME,
                    story_id INTEGER
                )
                """
            )
        )
    return engine


def _column_names(engine: Engine) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns("user_stories")}


def test_active_backlog_reset_migration_adds_nullable_archive_columns() -> None:
    """Archive metadata columns are additive and nullable."""
    engine = _create_legacy_runtime_schema()

    ensure_schema_current(engine)
    ensure_schema_current(engine)

    columns = _column_names(engine)
    assert ARCHIVE_COLUMNS.issubset(columns)
    column_map = {
        col["name"]: col for col in inspect(engine).get_columns("user_stories")
    }
    for column_name in ARCHIVE_COLUMNS:
        assert column_map[column_name]["nullable"] is True


def test_active_backlog_reset_migration_backfills_no_existing_rows() -> None:
    """Existing rows stay unarchived after additive migration."""
    engine = _create_legacy_runtime_schema()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO products (name) VALUES ('Cartola')"))
        conn.execute(
            text(
                """
                INSERT INTO user_stories
                    (title, status, product_id, created_at, updated_at)
                VALUES
                    ('Old story', 'To Do', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )

    ensure_schema_current(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT archived_reason, archived_at, archived_by,
                       archive_reset_attempt_id, archive_previous_status
                FROM user_stories
                WHERE title = 'Old story'
                """
            )
        ).mappings().one()
    assert dict(row) == {
        "archived_reason": None,
        "archived_at": None,
        "archived_by": None,
        "archive_reset_attempt_id": None,
        "archive_previous_status": None,
    }


def test_user_story_model_exposes_archive_columns() -> None:
    """SQLModel metadata includes reset archive fields."""
    model_columns = set(UserStory.model_fields)
    assert ARCHIVE_COLUMNS.issubset(model_columns)
