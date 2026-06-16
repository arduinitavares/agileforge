from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from db.migrations import ensure_schema_current
from services.agent_workbench.schema_readiness import (
    check_authority_curation_readiness,
)


def test_authority_curation_tables_are_created(engine: Engine) -> None:
    """Authority curation attempts must have dedicated tables."""
    ensure_schema_current(engine)

    table_names = set(inspect(engine).get_table_names())

    assert "authority_feedback_attempts" in table_names
    assert "authority_curation_attempts" in table_names


def test_authority_curation_schema_readiness_passes_after_migration(
    engine: Engine,
) -> None:
    """Readiness check must accept migrated curation storage."""
    ensure_schema_current(engine)

    result = check_authority_curation_readiness(engine)

    assert result.ok is True
    assert result.missing == {}


def test_authority_curation_create_all_defaults_match_migration(
    engine: Engine,
) -> None:
    """Metadata-created curation tables must retain migration-level defaults."""
    SQLModel.metadata.create_all(engine)
    ensure_schema_current(engine)

    feedback_defaults = _column_defaults(engine, "authority_feedback_attempts")
    assert feedback_defaults["status"] == "'recorded'"
    assert feedback_defaults["has_blocking_feedback"] == "0"
    assert feedback_defaults["changed_by"] == "'cli-agent'"

    curation_defaults = _column_defaults(engine, "authority_curation_attempts")
    assert curation_defaults["status"] == "'running'"
    assert curation_defaults["max_iterations"] == "2"
    assert curation_defaults["iteration_count"] == "0"
    assert curation_defaults["request_json"] == "'{}'"
    assert curation_defaults["candidate_lineage_json"] == "'{}'"
    assert curation_defaults["diff_summary_json"] == "'{}'"
    assert curation_defaults["lineage_json"] == "'{}'"
    assert curation_defaults["quality_report_json"] == "'{}'"
    assert curation_defaults["changed_by"] == "'cli-agent'"


def _column_defaults(engine: Engine, table_name: str) -> dict[str, str | None]:
    """Return SQLite column defaults keyed by column name."""
    return {
        column["name"]: column["default"]
        for column in inspect(engine).get_columns(table_name)
    }
