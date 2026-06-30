"""Agent workbench diagnostics and schema readiness payloads."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from models import db as model_db
from services.agent_workbench.schema_readiness import (
    MUTATION_LEDGER_REQUIRED_COLUMNS,
    MUTATION_LEDGER_TABLE,
    SCOPE_DISCOVERY_REQUIREMENTS,
    check_schema_readiness,
)
from services.agent_workbench.version import STORAGE_SCHEMA_VERSION
from utils.runtime_config import get_session_db_target

BUSINESS_SCHEMA_VERSION_TABLE = "agent_workbench_schema_versions"
BUSINESS_MUTATION_LEDGER_TABLE = MUTATION_LEDGER_TABLE


def _flatten_missing(
    missing: dict[str, list[str]],
    *,
    table_names: set[str],
) -> list[str]:
    """Return table names for absent tables and table.element for missing elements."""
    flattened: list[str] = []
    for table_name, elements in missing.items():
        if table_name not in table_names:
            flattened.append(table_name)
            continue
        flattened.extend(f"{table_name}.{element}" for element in elements)
    return flattened


def schema_check_payload(
    *,
    business_engine: Engine | None = None,
    session_db_url: str | None = None,
) -> dict[str, Any]:
    """Return JSON-friendly schema readiness diagnostics."""
    return {
        "business_db": _business_db_payload(business_engine=business_engine),
        "workflow_session_store": _workflow_session_store_payload(
            session_db_url=session_db_url,
        ),
    }


def doctor_payload(
    *,
    business_engine: Engine | None = None,
    session_db_url: str | None = None,
) -> dict[str, Any]:
    """Return broader local diagnostics for CLI contract health."""
    payload = schema_check_payload(
        business_engine=business_engine,
        session_db_url=session_db_url,
    )
    payload["central_repo_root"] = _path_payload(_central_repo_root())
    payload["caller_cwd"] = _path_payload(Path.cwd())
    return payload


def _business_db_payload(*, business_engine: Engine | None) -> dict[str, Any]:
    """Return readiness for required business DB contract tables."""
    engine = (
        business_engine if business_engine is not None else _default_business_engine()
    )
    missing = [BUSINESS_SCHEMA_VERSION_TABLE, BUSINESS_MUTATION_LEDGER_TABLE]
    checks = {
        "schema_versions_table": False,
        "cli_mutation_ledger_table": False,
        "cli_mutation_ledger_columns": False,
        "scope_discovery_tables": False,
        "scope_discovery_columns": False,
    }
    if _is_missing_sqlite_file(engine):
        return {
            "ok": False,
            "status": "blocked",
            "required_version": STORAGE_SCHEMA_VERSION,
            "version_source": BUSINESS_SCHEMA_VERSION_TABLE,
            "checks": checks,
            "missing": missing,
        }

    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
    except SQLAlchemyError:
        return {
            "ok": False,
            "status": "blocked",
            "required_version": STORAGE_SCHEMA_VERSION,
            "version_source": BUSINESS_SCHEMA_VERSION_TABLE,
            "checks": checks,
            "missing": missing,
        }

    checks = {
        "schema_versions_table": BUSINESS_SCHEMA_VERSION_TABLE in table_names,
        "cli_mutation_ledger_table": BUSINESS_MUTATION_LEDGER_TABLE in table_names,
        "cli_mutation_ledger_columns": False,
        "scope_discovery_tables": False,
        "scope_discovery_columns": False,
    }
    missing_columns: list[str] = []
    if checks["cli_mutation_ledger_table"]:
        existing_columns = {
            column["name"]
            for column in inspector.get_columns(BUSINESS_MUTATION_LEDGER_TABLE)
        }
        missing_columns = [
            column
            for column in MUTATION_LEDGER_REQUIRED_COLUMNS
            if column not in existing_columns
        ]
        checks["cli_mutation_ledger_columns"] = not missing_columns

    discovery_readiness = check_schema_readiness(engine, SCOPE_DISCOVERY_REQUIREMENTS)
    discovery_missing = _flatten_missing(
        discovery_readiness.missing,
        table_names=table_names,
    )
    required_discovery_tables = {
        requirement.table for requirement in SCOPE_DISCOVERY_REQUIREMENTS
    }
    checks["scope_discovery_tables"] = required_discovery_tables.issubset(
        table_names,
    )
    checks["scope_discovery_columns"] = discovery_readiness.ok

    missing = [
        table_name
        for table_name, present in (
            (BUSINESS_SCHEMA_VERSION_TABLE, checks["schema_versions_table"]),
            (BUSINESS_MUTATION_LEDGER_TABLE, checks["cli_mutation_ledger_table"]),
        )
        if not present
    ]
    missing.extend(
        f"{BUSINESS_MUTATION_LEDGER_TABLE}.{column}" for column in missing_columns
    )
    missing.extend(discovery_missing)
    ok = not missing
    return {
        "ok": ok,
        "status": "ok" if ok else "blocked",
        "required_version": STORAGE_SCHEMA_VERSION,
        "version_source": BUSINESS_SCHEMA_VERSION_TABLE,
        "checks": checks,
        "missing": missing,
    }


def _workflow_session_store_payload(*, session_db_url: str | None) -> dict[str, Any]:
    """Return conservative workflow session store readiness."""
    sqlite_url = (
        session_db_url if session_db_url is not None else _default_session_db_url()
    )
    configured = bool(sqlite_url)
    sqlite_ready = _is_sqlite_url(sqlite_url) if configured else False
    writable_mode, sessions_table = (
        _sqlite_session_store_checks(sqlite_url)
        if configured and sqlite_ready
        else (False, False)
    )
    ok = configured and sqlite_ready and writable_mode and sessions_table
    return {
        "ok": ok,
        "status": "ok" if ok else "blocked",
        "required_version": None,
        "version_source": "unavailable",
        "checks": {
            "configured": configured,
            "sqlite_url": sqlite_ready,
            "readable_writable_mode": writable_mode,
            "sessions_table": sessions_table,
        },
    }


def _default_business_engine() -> Engine:
    """Return the configured business engine at call time."""
    return model_db.get_engine()


def _default_session_db_url() -> str:
    """Return the configured workflow session DB URL."""
    return get_session_db_target().sqlite_url


def _is_sqlite_url(value: str) -> bool:
    """Return whether the configured value is a SQLAlchemy SQLite URL."""
    try:
        return make_url(value).drivername.startswith("sqlite")
    except ValueError:
        return False


def _is_missing_sqlite_file(engine: Engine) -> bool:
    """Return whether a SQLite engine targets an absent file."""
    url = engine.url
    if not url.drivername.startswith("sqlite"):
        return False

    database = url.database
    if database in {None, "", ":memory:"}:
        return False

    return not Path(database).exists()


def _sqlite_session_store_checks(value: str) -> tuple[bool, bool]:  # noqa: PLR0911
    """Return read/write and sessions-table readiness without creating files."""
    try:
        url = make_url(value)
    except ValueError:
        return False, False
    mode = url.query.get("mode")
    immutable = url.query.get("immutable")
    if mode == "ro" or immutable == "1":
        return False, False
    if not url.drivername.startswith("sqlite"):
        return False, False

    database = url.database
    if database in {None, "", ":memory:"}:
        return True, True

    path = Path(database)
    if not path.exists():
        return False, False
    if not path.is_file() or not _can_read_write(path):
        return False, False
    return True, _has_sqlite_table(path=path, table_name="sessions")


def _can_read_write(path: Path) -> bool:
    """Return whether an existing database file allows read/write access."""
    try:
        with path.open("r+b"):
            return True
    except OSError:
        return False


def _has_sqlite_table(*, path: Path, table_name: str) -> bool:
    """Return whether an existing SQLite file has a table in read-only mode."""
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
            cursor = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table' AND name=?
                LIMIT 1
                """,
                (table_name,),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error:
        return False


def _central_repo_root() -> Path:
    """Resolve the AgileForge repository root from this package location."""
    return Path(__file__).resolve().parents[2]


def _path_payload(path: Path) -> dict[str, Any]:
    """Return path existence as an ok/blocked diagnostic."""
    resolved = path.resolve()
    ok = resolved.exists()
    return {
        "ok": ok,
        "status": "ok" if ok else "blocked",
        "path": str(resolved),
    }
