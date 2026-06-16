# ruff: noqa: E501
"""Regression tests for fresh session-store bootstrap behavior."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING

import pytest  # noqa: TC002
from fastapi.testclient import TestClient

import api as api_module
from repositories.session import WorkflowSessionRepository
from services.workflow import WorkflowService
from utils.runtime_config import resolve_database_target

if TYPE_CHECKING:
    from pathlib import Path


def _session_repo(db_path: Path) -> WorkflowSessionRepository:
    target = resolve_database_target(
        str(db_path),
        env_name="AGILEFORGE_SESSION_DB_URL",
    )
    return WorkflowSessionRepository(db_target=target)


def _create_sessions_table(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                app_name VARCHAR(128) NOT NULL,
                user_id VARCHAR(128) NOT NULL,
                id VARCHAR(128) NOT NULL,
                state TEXT NOT NULL,
                create_time DATETIME NOT NULL,
                update_time DATETIME NOT NULL,
                PRIMARY KEY (app_name, user_id, id)
            )
            """
        )


def _insert_session_row(
    db_path: Path,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    state_payload: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (app_name, user_id, id, state, create_time, update_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                app_name,
                user_id,
                session_id,
                state_payload,
                "2026-03-13 00:00:00",
                "2026-03-13 00:00:00",
            ),
        )


def _workflow_service(db_path: Path) -> WorkflowService:
    service = WorkflowService()
    service.session_repo = _session_repo(db_path)
    return service


def test_get_session_state_returns_empty_when_sessions_table_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify get session state returns empty when sessions table missing."""
    repo = _session_repo(tmp_path / "fresh_sessions.db")

    with caplog.at_level(logging.ERROR, logger="repositories.session"):
        state = repo.get_session_state("app", "user", "session")

    assert state == {}
    assert not caplog.records


def test_get_session_state_returns_existing_row(tmp_path: Path) -> None:
    """Verify get session state returns existing row."""
    db_path = tmp_path / "existing_sessions.db"
    repo = _session_repo(db_path)
    _create_sessions_table(db_path)
    _insert_session_row(
        db_path,
        app_name="app",
        user_id="user",
        session_id="session",
        state_payload=json.dumps({"fsm_state": "VISION_INTERVIEW"}),
    )

    state = repo.get_session_state("app", "user", "session")

    assert state == {"fsm_state": "VISION_INTERVIEW"}


def test_workflow_session_events_accept_adk2_fields(tmp_path: Path) -> None:
    """Session serialization must tolerate ADK 2.0 workflow event fields."""
    db_path = tmp_path / "adk2_event_sessions.db"
    repo = _session_repo(db_path)
    _create_sessions_table(db_path)
    _insert_session_row(
        db_path,
        app_name="app",
        user_id="user",
        session_id="session",
        state_payload=json.dumps({}),
    )
    event_payload = {
        "author": "AuthorityCurationWorkflow",
        "content": {"parts": [{"text": "gate passed"}]},
        "node_info": {"node_id": "GateDecision", "iteration": 1},
        "output": {"status": "pass", "review_ready": True},
    }

    repo.update_session_state("app", "user", "session", {"events": [event_payload]})

    stored_event = repo.get_session_state("app", "user", "session")["events"][0]
    assert stored_event["node_info"]["node_id"] == "GateDecision"
    assert stored_event["output"]["status"] == "pass"


def test_get_session_states_batch_returns_matching_rows(tmp_path: Path) -> None:
    """Verify get session states batch returns matching rows."""
    db_path = tmp_path / "batch_sessions.db"
    repo = _session_repo(db_path)
    _create_sessions_table(db_path)
    _insert_session_row(
        db_path,
        app_name="app",
        user_id="user",
        session_id="session-1",
        state_payload=json.dumps({"fsm_state": "VISION_INTERVIEW"}),
    )
    _insert_session_row(
        db_path,
        app_name="app",
        user_id="user",
        session_id="session-2",
        state_payload=json.dumps({"fsm_state": "SETUP_REQUIRED"}),
    )

    state_map = repo.get_session_states_batch(
        "app",
        "user",
        ["session-1", "missing", "session-2"],
    )

    assert state_map == {
        "session-1": {"fsm_state": "VISION_INTERVIEW"},
        "session-2": {"fsm_state": "SETUP_REQUIRED"},
    }


def test_migrate_legacy_setup_state_returns_zero_when_sessions_table_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify migrate legacy setup state returns zero when sessions table missing."""
    service = _workflow_service(tmp_path / "missing_sessions.db")

    with caplog.at_level(logging.ERROR, logger="services.workflow"):
        migrated = service.migrate_legacy_setup_state()

    assert migrated == 0
    assert not caplog.records


def test_migrate_legacy_setup_state_returns_zero_when_sessions_table_exists_without_rows(
    tmp_path: Path,
) -> None:
    """Verify migrate legacy setup state returns zero when sessions table exists without rows."""
    db_path = tmp_path / "no_rows_sessions.db"
    _create_sessions_table(db_path)
    service = _workflow_service(db_path)

    assert service.migrate_legacy_setup_state() == 0


def test_migrate_legacy_setup_state_updates_routing_mode_rows(tmp_path: Path) -> None:
    """Verify migrate legacy setup state updates routing mode rows."""
    db_path = tmp_path / "routing_mode_sessions.db"
    _create_sessions_table(db_path)
    service = _workflow_service(db_path)

    _insert_session_row(
        db_path,
        app_name=service.app_name,
        user_id=service.user_id,
        session_id="legacy",
        state_payload=json.dumps({"fsm_state": "ROUTING_MODE", "other": "value"}),
    )
    _insert_session_row(
        db_path,
        app_name=service.app_name,
        user_id=service.user_id,
        session_id="already-new",
        state_payload=json.dumps({"fsm_state": "VISION_INTERVIEW"}),
    )

    migrated = service.migrate_legacy_setup_state()

    assert migrated == 1
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT state FROM sessions WHERE app_name=? AND user_id=? AND id=?",
            (service.app_name, service.user_id, "legacy"),
        )
        row = cursor.fetchone()
    assert row is not None
    assert json.loads(row[0])["fsm_state"] == "SETUP_REQUIRED"


def test_migrate_legacy_setup_state_skips_malformed_json_rows(tmp_path: Path) -> None:
    """Verify migrate legacy setup state skips malformed json rows."""
    db_path = tmp_path / "malformed_sessions.db"
    _create_sessions_table(db_path)
    service = _workflow_service(db_path)

    _insert_session_row(
        db_path,
        app_name=service.app_name,
        user_id=service.user_id,
        session_id="broken",
        state_payload="{not json",
    )
    _insert_session_row(
        db_path,
        app_name=service.app_name,
        user_id=service.user_id,
        session_id="legacy",
        state_payload=json.dumps({"fsm_state": "ROUTING_MODE"}),
    )

    migrated = service.migrate_legacy_setup_state()

    assert migrated == 1
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT state FROM sessions WHERE app_name=? AND user_id=? AND id=?",
            (service.app_name, service.user_id, "legacy"),
        )
        migrated_row = cursor.fetchone()
        cursor.execute(
            "SELECT state FROM sessions WHERE app_name=? AND user_id=? AND id=?",
            (service.app_name, service.user_id, "broken"),
        )
        broken_row = cursor.fetchone()

    assert migrated_row is not None
    assert json.loads(migrated_row[0])["fsm_state"] == "SETUP_REQUIRED"
    assert broken_row is not None
    assert broken_row[0] == "{not json"


def test_api_startup_skips_missing_sessions_table_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify api startup skips missing sessions table error."""
    workflow_service = _workflow_service(tmp_path / "startup_sessions.db")
    monkeypatch.setattr(api_module, "workflow_service", workflow_service)

    with caplog.at_level(logging.ERROR), TestClient(api_module.app):
        pass

    messages = [record.getMessage() for record in caplog.records]
    assert "Failed migrating legacy setup states" not in " ".join(messages)
    assert "no such table: sessions" not in " ".join(messages)
