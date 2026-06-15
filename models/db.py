"""Database engine helpers shared by the business model layer."""

from __future__ import annotations

import logging
import os
import sys
from functools import cache
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from db.migrations import ensure_schema_current
from models import agent_workbench as _agent_workbench_models  # noqa: F401
from models import brownfield as _brownfield_models  # noqa: F401
from utils.runtime_config import get_business_db_target, get_database_echo

if TYPE_CHECKING:
    import sqlite3

logger: logging.Logger = logging.getLogger(name=__name__)


def _is_pytest_running() -> bool:
    """Detect if code is running under pytest."""
    return "pytest" in sys.modules or "py.test" in sys.modules


def get_database_url() -> str:
    """Return the configured business database URL."""
    return get_business_db_target().sqlite_url


class _PytestEngineGuardError(RuntimeError):
    """Raised when production DB access is attempted during pytest."""

    def __init__(self) -> None:
        super().__init__(
            "get_engine() called during pytest without ALLOW_PROD_DB_IN_TEST=1. "
            "Tests should use the 'engine' fixture and monkey-patch the module. "
            "Example: monkeypatch.setattr(save_mod, 'engine', test_engine)"
        )


@cache
def _create_production_engine() -> Engine:
    """Create the production database engine."""
    return create_engine(
        get_database_url(),
        echo=get_database_echo(),
        connect_args={"check_same_thread": False},
    )


def get_engine() -> Engine:
    """Return the database engine with test safety guard."""
    if _is_pytest_running() and not os.environ.get("ALLOW_PROD_DB_IN_TEST"):
        raise _PytestEngineGuardError()

    return _create_production_engine()


DB_URL = get_database_url()
engine = create_engine(
    DB_URL,
    echo=get_database_echo(),
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    """Enforce foreign key constraints on SQLite."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_and_tables() -> None:
    """Create the database and all tables, then run migrations."""
    logger.info("Creating tables.")
    ensure_business_db_ready()
    logger.info("Tables created successfully.")


def ensure_business_db_ready(engine_override: Engine | None = None) -> None:
    """Create core business tables and apply idempotent migrations."""
    target_engine = engine_override or engine
    SQLModel.metadata.create_all(target_engine)
    ensure_schema_current(target_engine)
