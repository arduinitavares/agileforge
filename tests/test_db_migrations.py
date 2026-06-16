"""Tests for cross-cutting database migrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import create_engine, inspect, text

from db.migrations import CLI_MUTATION_LEDGER_CREATE_SQL, ensure_schema_current
from services.agent_workbench.version import STORAGE_SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from sqlalchemy.engine.reflection import Inspector


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


def _create_min_runtime_schema(engine_url: str) -> Engine:
    engine = create_engine(engine_url)
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
                    title VARCHAR NOT NULL
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


def test_migration_adds_project_setup_recovery_linkage_columns(
    tmp_path: Path,
) -> None:
    """Upgrade pre-Phase-2B mutation ledgers with recovery linkage."""
    engine = _create_min_runtime_schema(
        f"sqlite:///{(tmp_path / 'pre-phase-2b.sqlite3').as_posix()}"
    )
    with engine.begin() as conn:
        conn.execute(text(CLI_MUTATION_LEDGER_CREATE_SQL_PHASE_2A))

    ensure_schema_current(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("cli_mutation_ledger")
    }
    assert "recovers_mutation_event_id" in columns
    assert "superseded_by_mutation_event_id" in columns

    indexes = {
        index["name"] for index in inspect(engine).get_indexes("cli_mutation_ledger")
    }
    assert "ix_cli_mutation_ledger_recovers_mutation_event_id" in indexes
    assert "ix_cli_mutation_ledger_superseded_by_mutation_event_id" in indexes

    with engine.begin() as conn:
        version = conn.execute(
            text(
                """
                SELECT version
                FROM agent_workbench_schema_versions
                WHERE component = 'agent_workbench'
                """
            )
        ).scalar_one()
    assert version == STORAGE_SCHEMA_VERSION


def test_raw_mutation_ledger_create_sql_includes_recovery_linkage_columns(
    tmp_path: Path,
) -> None:
    """Fresh contract-table creation should include Phase 2B columns."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'fresh-phase-2b.sqlite3').as_posix()}"
    )
    with engine.begin() as conn:
        conn.execute(text(CLI_MUTATION_LEDGER_CREATE_SQL))

    columns = {
        column["name"] for column in inspect(engine).get_columns("cli_mutation_ledger")
    }
    assert "recovers_mutation_event_id" in columns
    assert "superseded_by_mutation_event_id" in columns


def test_authority_curation_migration_is_idempotent(tmp_path: Path) -> None:
    """Fresh and repeated migration creates curation storage once."""
    engine = _create_min_runtime_schema(
        f"sqlite:///{(tmp_path / 'authority-curation.sqlite3').as_posix()}"
    )

    ensure_schema_current(engine)
    ensure_schema_current(engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "authority_feedback_attempts" in table_names
    assert "authority_curation_attempts" in table_names

    feedback_columns = {
        column["name"]
        for column in inspector.get_columns("authority_feedback_attempts")
    }
    assert {
        "feedback_row_id",
        "project_id",
        "feedback_attempt_id",
        "source_authority_id",
        "source_authority_fingerprint",
        "feedback_fingerprint",
        "status",
        "has_blocking_feedback",
        "feedback_json",
        "request_hash",
        "idempotency_key",
        "changed_by",
        "created_at",
        "updated_at",
    }.issubset(feedback_columns)

    curation_columns = {
        column["name"]
        for column in inspector.get_columns("authority_curation_attempts")
    }
    assert {
        "curation_row_id",
        "project_id",
        "curation_attempt_id",
        "source_authority_id",
        "source_authority_fingerprint",
        "spec_version_id",
        "feedback_attempt_id",
        "status",
        "max_iterations",
        "iteration_count",
        "compiler_model",
        "candidate_authority_id",
        "candidate_authority_fingerprint",
        "request_json",
        "candidate_lineage_json",
        "diff_summary_json",
        "lineage_json",
        "quality_report_json",
        "failure_artifact_id",
        "request_hash",
        "idempotency_key",
        "changed_by",
        "created_at",
        "updated_at",
    }.issubset(curation_columns)

    feedback_indexes = {
        index["name"]
        for index in inspector.get_indexes("authority_feedback_attempts")
    }
    assert "ix_authority_feedback_project_status" in feedback_indexes
    assert "ix_authority_feedback_source_authority" in feedback_indexes
    assert _has_unique_columns(
        inspector,
        table_name="authority_feedback_attempts",
        columns=("project_id", "idempotency_key"),
    )

    curation_indexes = {
        index["name"]
        for index in inspector.get_indexes("authority_curation_attempts")
    }
    assert "ix_authority_curation_project_status" in curation_indexes
    assert "ix_authority_curation_source_authority" in curation_indexes
    assert _has_unique_columns(
        inspector,
        table_name="authority_curation_attempts",
        columns=("project_id", "idempotency_key"),
    )


def _has_unique_columns(
    inspector: Inspector,
    *,
    table_name: str,
    columns: tuple[str, ...],
) -> bool:
    """Return whether a table has a unique constraint or index on columns."""
    for constraint in inspector.get_unique_constraints(table_name):
        if tuple(constraint.get("column_names") or ()) == columns:
            return True
    for index in inspector.get_indexes(table_name):
        if index.get("unique") and tuple(index.get("column_names") or ()) == columns:
            return True
    return False
