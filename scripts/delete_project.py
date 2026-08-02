"""Script to delete a project and all its related data from the database.

Usage: python -m scripts.delete_project <product_id> [db_path].
"""

import argparse
import sqlite3
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlmodel import Session, create_engine

from models.core import Product
from repositories.product import ProductRepository
from utils.cli_output import emit
from utils.runtime_config import resolve_database_target


def _set_sqlite_pragma(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    """Preserve foreign-key enforcement on the script-owned engine."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def resolve_db_path(explicit_path: str | None = None) -> str:
    """Resolve a database path from CLI input or required runtime config."""
    return resolve_database_target(
        explicit_path,
        env_name="AGILEFORGE_DB_URL",
    ).sqlite_connect_target


def delete_project(product_id: int, db_path: str) -> None:
    """Delete one pre-authority Project through the guarded repository path."""
    emit(f"Connecting to database at: {db_path}")
    if db_path != ":memory:" and not Path(db_path).exists():
        msg = f"Database file not found: {db_path}"
        raise FileNotFoundError(msg)
    engine = create_engine(
        URL.create(drivername="sqlite", database=db_path),
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    try:
        with Session(engine) as session:
            product = session.get(Product, product_id)
            if product is None:
                emit(f"Product ID {product_id} not found.")
                return

            emit(
                f"Found product: {product.name} (ID: {product_id}). "
                "preparing to delete..."
            )
            deleted = ProductRepository(session).delete_project(product_id)
        if not deleted:
            emit(f"Product ID {product_id} not found.")
            return
        emit("Deletion complete.")
        emit(f"SUCCESS: Product {product_id} successfully deleted.")
    finally:
        engine.dispose()


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Delete a project and all related records from the configured business database.",  # noqa: E501
    )
    parser.add_argument("product_id", type=int, help="Product ID to delete.")
    parser.add_argument(
        "db",
        nargs="?",
        help="Optional SQLite database path or sqlite:/// URL. Defaults to AGILEFORGE_DB_URL.",  # noqa: E501
    )
    args = parser.parse_args()
    delete_project(args.product_id, resolve_db_path(args.db))


if __name__ == "__main__":
    main()
