"""Inspect runtime database schema."""

import argparse
import sqlite3
from pathlib import Path

from utils.cli_output import emit
from utils.runtime_config import resolve_database_target


def resolve_db_path(explicit_db: str | None = None) -> str:
    """Resolve a DB path from CLI input or runtime configuration."""
    return resolve_database_target(
        explicit_db,
        env_name="AGILEFORGE_DB_URL",
    ).sqlite_connect_target


def inspect_schema(db_path: str) -> None:
    """Print basic schema details for the target database."""
    if db_path != ":memory:" and not Path(db_path).exists():
        msg = f"Database file not found: {db_path}"
        raise FileNotFoundError(msg)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = cursor.fetchall()
    emit("Tables in database:")
    for (table_name,) in tables:
        emit(f"  - {table_name}")

    emit()

    for table_name in ["spec_registry"]:
        cursor.execute(f"PRAGMA table_info({table_name})")
        cols = cursor.fetchall()
        emit(f"{table_name} has {len(cols)} columns:")
        for column in cols:
            emit(f"  {column[0]}: {column[1]} ({column[2]}) nullable={column[3]}")
        emit()

    conn.close()


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Inspect the configured runtime database schema."
    )
    parser.add_argument(
        "db",
        nargs="?",
        help="Optional SQLite database path or sqlite:/// URL. Defaults to AGILEFORGE_DB_URL.",  # noqa: E501
    )
    args = parser.parse_args()
    inspect_schema(resolve_db_path(args.db))


if __name__ == "__main__":
    main()
