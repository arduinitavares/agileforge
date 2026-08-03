"""Tests for TCC metrics extraction helpers."""

import json
import sqlite3

from scripts.extract_tcc_metrics import extract_state_dwell_summary


def test_extract_state_dwell_summary_groups_by_from_state() -> None:
    """Verify extract state dwell summary groups by from state."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE workflow_events (
            event_id INTEGER PRIMARY KEY,
            event_type TEXT,
            duration_seconds REAL,
            event_metadata TEXT
        )
        """
    )
    cur.executemany(
        """
        INSERT INTO workflow_events(event_type, duration_seconds, event_metadata)
        VALUES (?, ?, ?)
        """,
        [
            (
                "FSM_STATE_DWELL",
                10.0,
                json.dumps({"from_state": "SETUP_REQUIRED"}),
            ),
            (
                "FSM_STATE_DWELL",
                20.0,
                json.dumps({"from_state": "SETUP_REQUIRED"}),
            ),
            (
                "FSM_STATE_DWELL",
                5.0,
                json.dumps({"from_state": "SPRINT_SETUP"}),
            ),
            ("BACKLOG_SAVED", 1.0, json.dumps({})),
            ("FSM_STATE_DWELL", None, json.dumps({"from_state": "SETUP_REQUIRED"})),
        ],
    )
    conn.commit()

    summary = extract_state_dwell_summary(cur)

    assert summary["SETUP_REQUIRED"]["count"] == 2  # noqa: PLR2004
    assert summary["SETUP_REQUIRED"]["avg_duration_sec"] == 15.0  # noqa: PLR2004
    assert summary["SETUP_REQUIRED"]["total_duration_sec"] == 30.0  # noqa: PLR2004
    assert summary["SPRINT_SETUP"]["count"] == 1
    assert summary["SPRINT_SETUP"]["total_duration_sec"] == 5.0  # noqa: PLR2004

    conn.close()
