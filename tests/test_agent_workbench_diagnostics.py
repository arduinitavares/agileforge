"""Tests for agent workbench diagnostics payloads."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, text

from db.migrations import ensure_schema_current
from services.agent_workbench.diagnostics import doctor_payload, schema_check_payload
from services.agent_workbench.version import STORAGE_SCHEMA_VERSION

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine

CLI_MUTATION_LEDGER_CREATE_SQL_PHASE_2A = """
CREATE TABLE IF NOT EXISTS cli_mutation_ledger (
    mutation_event_id INTEGER PRIMARY KEY,
    command VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    request_hash VARCHAR NOT NULL,
    project_id INTEGER,
    correlation_id VARCHAR NOT NULL,
    changed_by VARCHAR NOT NULL DEFAULT 'cli-agent',
    status VARCHAR NOT NULL,
    current_step VARCHAR NOT NULL DEFAULT 'start',
    completed_steps_json TEXT NOT NULL DEFAULT '[]',
    guard_inputs_json TEXT NOT NULL DEFAULT '{}',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT,
    response_json TEXT,
    recovery_action VARCHAR NOT NULL DEFAULT 'none',
    recovery_safe_to_auto_resume BOOLEAN NOT NULL DEFAULT 0,
    lease_owner VARCHAR,
    lease_acquired_at TIMESTAMP,
    last_heartbeat_at TIMESTAMP,
    lease_expires_at TIMESTAMP,
    last_error_json TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_cli_mutation_command_idempotency
        UNIQUE (command, idempotency_key)
)
"""

AGENT_WORKBENCH_SCHEMA_VERSIONS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_workbench_schema_versions (
    component TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def test_schema_check_reports_ready_business_db(engine: Engine) -> None:
    """Report ready when business contract tables are present."""
    ensure_schema_current(engine)

    payload = schema_check_payload(
        business_engine=engine,
        session_db_url="sqlite:///:memory:",
    )

    assert payload["business_db"] == {
        "ok": True,
        "status": "ok",
        "required_version": STORAGE_SCHEMA_VERSION,
        "version_source": "agent_workbench_schema_versions",
        "checks": {
            "schema_versions_table": True,
            "cli_mutation_ledger_table": True,
            "cli_mutation_ledger_columns": True,
        },
        "missing": [],
    }
    assert payload["workflow_session_store"] == {
        "ok": True,
        "status": "ok",
        "required_version": None,
        "version_source": "unavailable",
        "checks": {
            "configured": True,
            "sqlite_url": True,
            "readable_writable_mode": True,
            "sessions_table": True,
        },
    }


def test_schema_check_reports_missing_business_contract_tables() -> None:
    """Report missing contract tables without migrating an empty database."""
    empty_engine = create_engine("sqlite:///:memory:")

    payload = schema_check_payload(
        business_engine=empty_engine,
        session_db_url="sqlite:///:memory:",
    )

    assert payload["business_db"]["ok"] is False
    assert payload["business_db"]["status"] == "blocked"
    assert payload["business_db"]["checks"] == {
        "schema_versions_table": False,
        "cli_mutation_ledger_table": False,
        "cli_mutation_ledger_columns": False,
    }
    assert payload["business_db"]["missing"] == [
        "agent_workbench_schema_versions",
        "cli_mutation_ledger",
    ]


def test_schema_check_reports_missing_business_sqlite_file_without_creating_it(
    tmp_path: Path,
) -> None:
    """Avoid creating an absent business DB during diagnostics."""
    business_db_path = tmp_path / "missing-business.sqlite3"
    business_engine = create_engine(f"sqlite:///{business_db_path.as_posix()}")

    payload = schema_check_payload(
        business_engine=business_engine,
        session_db_url="sqlite:///:memory:",
    )

    assert payload["business_db"]["ok"] is False
    assert payload["business_db"]["status"] == "blocked"
    assert payload["business_db"]["checks"] == {
        "schema_versions_table": False,
        "cli_mutation_ledger_table": False,
        "cli_mutation_ledger_columns": False,
    }
    assert payload["business_db"]["missing"] == [
        "agent_workbench_schema_versions",
        "cli_mutation_ledger",
    ]
    assert not business_db_path.exists()


def test_schema_check_reports_missing_mutation_ledger_columns(
    tmp_path: Path,
) -> None:
    """Report existing pre-Phase-2B ledger tables as blocked."""
    business_db_path = tmp_path / "pre-phase-2b-business.sqlite3"
    business_engine = create_engine(f"sqlite:///{business_db_path.as_posix()}")
    with business_engine.begin() as conn:
        conn.execute(text(AGENT_WORKBENCH_SCHEMA_VERSIONS_CREATE_SQL))
        conn.execute(text(CLI_MUTATION_LEDGER_CREATE_SQL_PHASE_2A))
        conn.execute(
            text(
                """
                INSERT INTO agent_workbench_schema_versions(component, version)
                VALUES ('agent_workbench', '1')
                """
            )
        )

    payload = schema_check_payload(
        business_engine=business_engine,
        session_db_url="sqlite:///:memory:",
    )

    assert payload["business_db"]["ok"] is False
    assert payload["business_db"]["status"] == "blocked"
    assert payload["business_db"]["required_version"] == STORAGE_SCHEMA_VERSION
    assert payload["business_db"]["checks"] == {
        "schema_versions_table": True,
        "cli_mutation_ledger_table": True,
        "cli_mutation_ledger_columns": False,
    }
    assert payload["business_db"]["missing"] == [
        "cli_mutation_ledger.recovers_mutation_event_id",
        "cli_mutation_ledger.superseded_by_mutation_event_id",
    ]


def test_schema_check_reports_malformed_mutation_ledger_columns(
    tmp_path: Path,
) -> None:
    """Report malformed ledgers even when Phase 2B linkage columns exist."""
    business_db_path = tmp_path / "malformed-phase-2b-business.sqlite3"
    business_engine = create_engine(f"sqlite:///{business_db_path.as_posix()}")
    with business_engine.begin() as conn:
        conn.execute(text(AGENT_WORKBENCH_SCHEMA_VERSIONS_CREATE_SQL))
        conn.execute(
            text(
                """
                CREATE TABLE cli_mutation_ledger (
                    mutation_event_id INTEGER PRIMARY KEY,
                    recovers_mutation_event_id INTEGER,
                    superseded_by_mutation_event_id INTEGER
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_workbench_schema_versions(component, version)
                VALUES ('agent_workbench', '2')
                """
            )
        )

    payload = schema_check_payload(
        business_engine=business_engine,
        session_db_url="sqlite:///:memory:",
    )

    assert payload["business_db"]["ok"] is False
    assert payload["business_db"]["checks"] == {
        "schema_versions_table": True,
        "cli_mutation_ledger_table": True,
        "cli_mutation_ledger_columns": False,
    }
    assert "cli_mutation_ledger.command" in payload["business_db"]["missing"]
    assert "cli_mutation_ledger.status" in payload["business_db"]["missing"]
    assert (
        "cli_mutation_ledger.recovers_mutation_event_id"
        not in payload["business_db"]["missing"]
    )


def test_schema_check_blocks_missing_session_db_with_existing_parent(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Reject missing file-backed session DBs without creating them."""
    ensure_schema_current(engine)
    session_db_path = tmp_path / "missing-sessions.sqlite3"

    payload = schema_check_payload(
        business_engine=engine,
        session_db_url=f"sqlite:///{session_db_path.as_posix()}",
    )

    assert payload["workflow_session_store"] == {
        "ok": False,
        "status": "blocked",
        "required_version": None,
        "version_source": "unavailable",
        "checks": {
            "configured": True,
            "sqlite_url": True,
            "readable_writable_mode": False,
            "sessions_table": False,
        },
    }
    assert not session_db_path.exists()


def test_schema_check_blocks_session_db_without_sessions_table(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Require the workflow session store to contain a sessions table."""
    ensure_schema_current(engine)
    session_db_path = tmp_path / "empty-sessions.sqlite3"
    session_db_path.touch()

    payload = schema_check_payload(
        business_engine=engine,
        session_db_url=f"sqlite:///{session_db_path.as_posix()}",
    )

    assert payload["workflow_session_store"] == {
        "ok": False,
        "status": "blocked",
        "required_version": None,
        "version_source": "unavailable",
        "checks": {
            "configured": True,
            "sqlite_url": True,
            "readable_writable_mode": True,
            "sessions_table": False,
        },
    }


def test_schema_check_accepts_session_db_with_sessions_table(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Accept an existing workflow session DB with the required table."""
    ensure_schema_current(engine)
    session_db_path = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(session_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                app_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                id TEXT NOT NULL,
                state TEXT NOT NULL
            )
            """
        )

    payload = schema_check_payload(
        business_engine=engine,
        session_db_url=f"sqlite:///{session_db_path.as_posix()}",
    )

    assert payload["workflow_session_store"]["ok"] is True
    assert payload["workflow_session_store"]["checks"] == {
        "configured": True,
        "sqlite_url": True,
        "readable_writable_mode": True,
        "sessions_table": True,
    }


def test_schema_check_blocks_session_db_with_missing_parent(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Reject file-backed session DB URLs whose parent directory is absent."""
    ensure_schema_current(engine)
    session_db_path = tmp_path / "missing-parent" / "sessions.sqlite3"

    payload = schema_check_payload(
        business_engine=engine,
        session_db_url=f"sqlite:///{session_db_path.as_posix()}",
    )

    assert payload["workflow_session_store"] == {
        "ok": False,
        "status": "blocked",
        "required_version": None,
        "version_source": "unavailable",
        "checks": {
            "configured": True,
            "sqlite_url": True,
            "readable_writable_mode": False,
            "sessions_table": False,
        },
    }
    assert not session_db_path.exists()


def test_doctor_reports_cwd_and_central_repo_root(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report caller cwd separately from the central repository root."""
    ensure_schema_current(engine)
    monkeypatch.chdir(tmp_path)

    payload = doctor_payload(
        business_engine=engine,
        session_db_url="sqlite:///:memory:",
    )

    assert payload["caller_cwd"] == {
        "ok": True,
        "status": "ok",
        "path": str(tmp_path.resolve()),
    }
    central_repo_root = payload["central_repo_root"]
    assert central_repo_root["ok"] is True
    assert central_repo_root["status"] == "ok"
    assert Path(central_repo_root["path"]) == Path(__file__).resolve().parents[1]
    assert central_repo_root["path"] != str(tmp_path.resolve())
    assert payload["business_db"]["status"] == "ok"
    assert payload["workflow_session_store"]["status"] == "ok"
