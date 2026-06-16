import json
import logging
import sqlite3
from typing import Any

from utils.runtime_config import DatabaseTarget, get_session_db_target

logger = logging.getLogger(__name__)


class WorkflowSessionRepository:
    """Repository handling volatile session state using sqlite3."""

    def __init__(self, db_target: DatabaseTarget | None = None):
        self.db_target = db_target or get_session_db_target()
        self.db_path = self.db_target.sqlite_connect_target
        self.db_url = self.db_target.sqlite_url
        self.adk_db_url = _adk_async_sqlite_url(self.db_url)

    def has_sessions_table(self) -> bool:
        """Return whether the ADK session schema has been initialized."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions' LIMIT 1"
            )
            return cursor.fetchone() is not None

    def get_session_state(
        self, app_name: str, user_id: str, session_id: str
    ) -> dict[str, Any]:
        """Fetch raw state dict from SQLite (Acts as Volatile RAM)."""
        if not self.has_sessions_table():
            logger.debug("Session store is not initialized yet; returning empty state.")
            return {}

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT state FROM sessions WHERE app_name=? AND user_id=? AND id=?",
                (app_name, user_id, session_id),
            )
            row = cursor.fetchone()

        state: dict[str, Any] = json.loads(row[0]) if row else {}
        logger.debug("Retrieved session state (redacted).")
        return state

    def get_session_states_batch(
        self,
        app_name: str,
        user_id: str,
        session_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Fetch multiple raw state payloads in one or more batched SQLite queries."""
        normalized_ids = list(
            dict.fromkeys(str(session_id) for session_id in session_ids)
        )
        if not normalized_ids:
            return {}

        if not self.has_sessions_table():
            logger.debug(
                "Session store is not initialized yet; returning empty state map."
            )
            return {}

        state_map: dict[str, dict[str, Any]] = {}
        chunk_size = 500

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TEMP TABLE IF NOT EXISTS session_id_filter "
                "(id TEXT PRIMARY KEY)"
            )
            for start in range(0, len(normalized_ids), chunk_size):
                chunk = normalized_ids[start : start + chunk_size]
                cursor.execute("DELETE FROM session_id_filter")
                cursor.executemany(
                    "INSERT INTO session_id_filter (id) VALUES (?)",
                    ((session_id,) for session_id in chunk),
                )
                cursor.execute(
                    """
                    SELECT sessions.id, sessions.state
                    FROM sessions
                    JOIN session_id_filter ON session_id_filter.id = sessions.id
                    WHERE sessions.app_name=? AND sessions.user_id=?
                    """,
                    (app_name, user_id),
                )
                rows = cursor.fetchall()

                for row_session_id, state_json in rows:
                    try:
                        state_map[str(row_session_id)] = json.loads(state_json or "{}")
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping malformed session state for %s",
                            row_session_id,
                        )
                        state_map[str(row_session_id)] = {}

        logger.debug("Retrieved %s session states in batch (redacted).", len(state_map))
        return state_map

    def update_session_state(
        self,
        app_name: str,
        user_id: str,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Updates the Volatile State with a partial update dict."""
        if not self.has_sessions_table():
            raise RuntimeError(
                "Session store is not initialized: sessions table is missing."
            )

        current_state = self.get_session_state(app_name, user_id, session_id)
        current_state.update(partial_update)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE sessions SET state=? WHERE app_name=? AND user_id=? AND id=?",
                (json.dumps(current_state), app_name, user_id, session_id),
            )
            conn.commit()
        logger.info("Session state updated successfully in DB")

    def delete_session(self, app_name: str, user_id: str, session_id: str) -> bool:
        """Deletes the session state from the volatile store."""
        if not self.has_sessions_table():
            return False

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM sessions WHERE app_name=? AND user_id=? AND id=?",
                (app_name, user_id, session_id),
            )
            deleted = cursor.rowcount > 0
            conn.commit()

        if deleted:
            logger.info("Session %s deleted successfully from DB", session_id)
        return deleted


def _adk_async_sqlite_url(db_url: str) -> str:
    """Return an ADK 2-compatible async SQLite database URL."""
    if db_url.startswith("sqlite+aiosqlite://"):
        return db_url
    if db_url.startswith("sqlite://"):
        return db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return db_url
