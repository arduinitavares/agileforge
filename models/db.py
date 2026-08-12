"""Database engine helpers shared by the business model layer."""

from __future__ import annotations

import logging
import os
import sys
from functools import cache
from typing import TYPE_CHECKING

from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from models import (
    authority_curation,
    core,
    events,
    product_definition,
    repository,
    specs,
    workflow,
)
from utils.runtime_config import get_business_db_target, get_database_echo

if TYPE_CHECKING:
    import sqlite3
    from types import ModuleType

logger: logging.Logger = logging.getLogger(name=__name__)

_CURRENT_MODEL_MODULES: tuple[ModuleType, ...] = (
    core,
    specs,
    events,
    product_definition,
    repository,
    workflow,
    authority_curation,
)

_ISSUE_199_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "specification_candidates": frozenset(
        {
            "canonical_envelope_json",
            "payload_fingerprint",
            "candidate_fingerprint",
            "specification_source_id",
            "specification_source_fingerprint",
            "vision_artifact_id",
            "vision_fingerprint",
            "product_goal_artifact_id",
            "product_goal_fingerprint",
            "workflow_node_attempt_id",
            "attempt_fingerprint",
        }
    ),
    "specification_sources": frozenset(
        {
            "source_bundle_json",
            "source_fingerprint",
            "repository_binding_id",
            "repository_head_sha",
            "repository_dirty",
            "repository_status_fingerprint",
            "vision_artifact_id",
            "vision_fingerprint",
            "product_goal_artifact_id",
            "product_goal_fingerprint",
            "supersedes_specification_source_id",
            "supersedes_source_fingerprint",
        }
    ),
    "spec_registry": frozenset(
        {
            "source_specification_candidate_id",
            "source_specification_candidate_fingerprint",
            "source_vision_artifact_id",
            "source_vision_fingerprint",
            "source_product_goal_artifact_id",
            "source_product_goal_fingerprint",
        }
    ),
}
_ISSUE_199_RETIRED_TABLE = "discovery_artifacts"
_REGISTERED_SOURCE_REQUIRED_TABLE = "specification_sources"


class UnsupportedBusinessSchemaError(RuntimeError):
    """Raised when a hard-break database predates the current model contract."""


def _assert_current_business_schema(target_engine: Engine) -> None:
    """Reject pre-issue-199 tables before create_all can mask incompatibility."""
    inspector = inspect(target_engine)
    tables = frozenset(inspector.get_table_names())
    incompatible: list[str] = []
    if _ISSUE_199_RETIRED_TABLE in tables:
        incompatible.append(f"retired table {_ISSUE_199_RETIRED_TABLE}")
    if "projects" in tables and _REGISTERED_SOURCE_REQUIRED_TABLE not in tables:
        incompatible.append(f"missing table {_REGISTERED_SOURCE_REQUIRED_TABLE}")
    for table, required in _ISSUE_199_REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        observed = {column["name"] for column in inspector.get_columns(table)}
        missing = sorted(required - observed)
        if missing:
            incompatible.append(f"{table} missing {', '.join(missing)}")
    if incompatible:
        detail = "; ".join(incompatible)
        message = (
            "UNSUPPORTED_BUSINESS_SCHEMA: the database predates issue #199 "
            f"({detail}). Create a fresh AgileForge profile/database; automatic "
            "migration is intentionally unsupported."
        )
        raise UnsupportedBusinessSchemaError(message)


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
    """Create the current database schema."""
    logger.info("Creating tables.")
    ensure_business_db_ready()
    logger.info("Tables created successfully.")


def ensure_business_db_ready(engine_override: Engine | None = None) -> None:
    """Create all current business tables from SQLModel metadata."""
    target_engine = engine_override or engine
    _assert_current_business_schema(target_engine)
    SQLModel.metadata.create_all(target_engine)
