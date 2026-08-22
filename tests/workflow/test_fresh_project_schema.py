"""Fresh-schema regressions for the canonical Project aggregate."""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from models.db import _assert_current_business_schema, ensure_business_db_ready

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


ISSUE_210_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "issue_210"
ATTEMPT_30_BASELINE_TABLE_COUNT = 53
_REPLAY_COLUMNS = (
    "project_id",
    "source_story_artifact_id",
    "source_story_item_id",
)


def _complete_current_schema() -> Engine:
    """Create the complete current metadata schema in an isolated database."""
    current = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    ensure_business_db_ready(current)
    return current


def _captured_issue_210_baseline_schema() -> Engine:
    """Load the complete attempt-30 business DDL captured provider-free."""
    baseline = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    schema_sql = (ISSUE_210_FIXTURE_ROOT / "baseline-business-schema.sql").read_text(
        encoding="utf-8"
    )
    raw_connection = baseline.raw_connection()
    try:
        raw_connection.executescript(schema_sql)
    finally:
        raw_connection.close()
    return baseline


def _inject_retired_table(current: Engine, retired_table: str) -> None:
    """Contaminate a complete schema only when fresh metadata no longer has it."""
    if retired_table in inspect(current).get_table_names():
        return
    with current.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TABLE {retired_table} (id INTEGER PRIMARY KEY)"
        )


def _inject_retired_column(
    current: Engine,
    *,
    table_name: str,
    column_name: str,
) -> None:
    """Contaminate a complete schema only when fresh metadata no longer has it."""
    columns = {column["name"] for column in inspect(current).get_columns(table_name)}
    if column_name in columns:
        return
    with current.begin() as connection:
        connection.exec_driver_sql(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} TEXT"
        )


def _remove_replay_unique_or_add_fresh_columns(current: Engine) -> None:
    """Derive one missing-replay-unique mixed schema from current table metadata."""
    inspector = inspect(current)
    columns = {column["name"] for column in inspector.get_columns("user_stories")}
    if not set(_REPLAY_COLUMNS) <= columns:
        missing_columns = {
            "source_story_artifact_id": "INTEGER",
            "source_story_item_id": "TEXT",
            "source_story_artifact_fingerprint": "TEXT",
            "source_story_item_fingerprint": "TEXT",
            "accepted_spec_version_id": "INTEGER",
            "accepted_spec_hash": "TEXT",
            "spec_item_ids_json": "TEXT",
            "acceptance_criteria_json": "TEXT",
        }
        with current.begin() as connection:
            for name, sql_type in missing_columns.items():
                if name not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE user_stories ADD COLUMN {name} {sql_type}"
                    )
        return

    with current.connect() as connection:
        create_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'user_stories'"
        ).scalar_one()
    replay_unique = re.compile(
        r",\s*(?:CONSTRAINT\s+[^\s]+\s+)?UNIQUE\s*\(\s*"
        + r"\s*,\s*".join(_REPLAY_COLUMNS)
        + r"\s*\)",
        flags=re.IGNORECASE,
    )
    recreated_sql, replacements = replay_unique.subn("", create_sql)
    if replacements == 0:
        return
    assert replacements == 1
    with current.begin() as connection:
        connection.exec_driver_sql("DROP TABLE user_stories")
        connection.exec_driver_sql(recreated_sql)


def _remove_named_table_constraint(
    current: Engine,
    *,
    table_name: str,
    constraint_name: str,
) -> None:
    """Recreate one fresh table without one reviewed named constraint."""
    raw_connection = current.raw_connection()
    try:
        create_sql = raw_connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()[0]
        index_sql = tuple(
            row[0]
            for row in raw_connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
                (table_name,),
            )
        )
        marker = f"CONSTRAINT {constraint_name} "
        recreated_lines = [
            line for line in create_sql.splitlines() if marker not in line
        ]
        assert len(recreated_lines) == len(create_sql.splitlines()) - 1

        raw_connection.execute("PRAGMA foreign_keys=OFF")
        raw_connection.execute(f"DROP TABLE {table_name}")
        raw_connection.execute("\n".join(recreated_lines))
        for statement in index_sql:
            raw_connection.execute(statement)
        raw_connection.commit()
        raw_connection.execute("PRAGMA foreign_keys=ON")
    finally:
        raw_connection.close()


def test_fresh_schema_uses_project_names(engine: Engine) -> None:
    """Create only the canonical Project schema without deleted state columns."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert "projects" in table_names
    assert "products" not in table_names
    assert "sessions" not in table_names

    forbidden_columns = {
        "product" + "_id",
        "context" + "_key",
        "fsm" + "_state",
        "setup" + "_status",
        "session" + "_id",
    }
    legacy_columns = {
        f"{table_name}.{column['name']}"
        for table_name in table_names
        for column in inspector.get_columns(table_name)
        if column["name"] in forbidden_columns
    }
    assert legacy_columns == set()

    legacy_foreign_keys = {
        f"{table_name}.{foreign_key['constrained_columns'][0]}"
        for table_name in table_names
        for foreign_key in inspector.get_foreign_keys(table_name)
        if foreign_key["referred_table"] == "products"
    }
    assert legacy_foreign_keys == set()


def test_project_models_and_repository_are_canonical() -> None:
    """Expose only canonical Project models and repository names."""
    core = importlib.import_module("models.core")
    project_repository = importlib.import_module("repositories.project")

    assert core.Project.__tablename__ == "projects"
    assert core.ProjectTeam.__tablename__ == "project_teams"
    assert core.ProjectPersona.__tablename__ == "project_personas"
    assert project_repository.ProjectRepository.__name__ == "ProjectRepository"


def test_old_aggregate_modules_and_symbols_are_absent() -> None:
    """Reject the deleted aggregate module and class identity."""
    core = importlib.import_module("models.core")
    old_class_name = "Prod" + "uct"
    old_repository_module = "repositories." + old_class_name.lower()

    assert not hasattr(core, old_class_name)
    assert importlib.util.find_spec(old_repository_module) is None


def test_pre_issue_199_schema_is_rejected_before_runtime_access() -> None:
    """Fail clearly instead of allowing create_all over the retired schema."""
    legacy = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with legacy.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE discovery_artifacts "
            "(discovery_artifact_id INTEGER PRIMARY KEY, project_id INTEGER)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE specification_candidates "
            "(specification_candidate_id INTEGER PRIMARY KEY, "
            "discovery_artifact_id INTEGER NOT NULL, content TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE spec_registry "
            "(spec_version_id INTEGER PRIMARY KEY, content_ref TEXT NOT NULL)"
        )

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        ensure_business_db_ready(legacy)

    assert "projects" not in inspect(legacy).get_table_names()


def test_pre_source_registration_schema_is_rejected_before_create_all() -> None:
    """Do not let create_all silently upgrade an existing issue-199 database."""
    legacy = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with legacy.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE projects (project_id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )

    with pytest.raises(
        RuntimeError,
        match=r"UNSUPPORTED_BUSINESS_SCHEMA.*missing tables .*specification_sources",
    ):
        ensure_business_db_ready(legacy)

    assert "specification_sources" not in inspect(legacy).get_table_names()


def test_issue_210_exact_captured_baseline_schema_is_rejected() -> None:
    """The complete immutable attempt-30 schema must fail the fresh-only gate."""
    baseline = _captured_issue_210_baseline_schema()

    assert len(inspect(baseline).get_table_names()) == ATTEMPT_30_BASELINE_TABLE_COUNT
    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        _assert_current_business_schema(baseline)


@pytest.mark.parametrize(
    "retired_table",
    [
        "compiled_spec_authority",
        "spec_authority_acceptance",
        "authority_feedback_attempts",
        "authority_curation_attempts",
    ],
)
def test_issue_210_retired_authority_tables_are_rejected(
    retired_table: str,
) -> None:
    """A complete schema plus one retired Authority table is unsupported."""
    mixed = _complete_current_schema()
    _inject_retired_table(mixed, retired_table)

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        _assert_current_business_schema(mixed)


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("backlog_artifacts", "authority_id"),
        ("backlog_artifacts", "authority_fingerprint"),
        ("spec_registry", "approved_at"),
        ("spec_registry", "approved_by"),
        ("spec_registry", "approval_notes"),
        ("story_artifacts", "requirement_id"),
        ("story_artifacts", "story_ids_json"),
        ("sprint_plan_artifacts", "sprint_id"),
        ("user_stories", "acceptance_criteria"),
        ("user_stories", "source_requirement"),
        ("user_stories", "refinement_slot"),
        ("user_stories", "story_origin"),
        ("user_stories", "is_refined"),
        ("user_stories", "archived_reason"),
        ("user_stories", "archived_at"),
        ("user_stories", "archived_by"),
        ("user_stories", "archive_reset_attempt_id"),
        ("user_stories", "archive_previous_status"),
        ("user_stories", "original_acceptance_criteria"),
        ("user_stories", "ac_updated_at"),
        ("user_stories", "ac_update_reason"),
        ("user_stories", "superseded_by_story_id"),
    ],
)
def test_issue_210_retired_columns_are_rejected(
    table_name: str,
    column_name: str,
) -> None:
    """A complete schema plus one retired Authority column is unsupported."""
    mixed = _complete_current_schema()
    _inject_retired_column(
        mixed,
        table_name=table_name,
        column_name=column_name,
    )

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        _assert_current_business_schema(mixed)


def test_issue_210_fresh_metadata_matches_independent_structural_manifest() -> None:
    """The reviewed manifest, not create_all output, defines the fresh schema."""
    from models.db import (  # noqa: PLC0415
        CURRENT_BUSINESS_SCHEMA_MANIFEST,
        _inspect_business_schema_manifest,
        _sqlmodel_business_schema_manifest,
    )

    fresh = _complete_current_schema()

    assert set(CURRENT_BUSINESS_SCHEMA_MANIFEST.structures) == set(
        CURRENT_BUSINESS_SCHEMA_MANIFEST.table_names
    )
    assert CURRENT_BUSINESS_SCHEMA_MANIFEST.structures["spec_registry"].checks == {
        "status in ('approved', 'superseded')"
    }
    assert _sqlmodel_business_schema_manifest() == CURRENT_BUSINESS_SCHEMA_MANIFEST
    assert _inspect_business_schema_manifest(fresh) == CURRENT_BUSINESS_SCHEMA_MANIFEST
    _assert_current_business_schema(fresh)


def test_issue_210_missing_required_partial_unique_is_rejected() -> None:
    """Current-Specification cardinality is structural, not an index-name hint."""
    mixed = _complete_current_schema()
    with mixed.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_spec_registry_current_approved")

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        _assert_current_business_schema(mixed)


@pytest.mark.parametrize(
    ("table_name", "constraint_name"),
    [
        (
            "specification_decisions",
            "uq_specification_decision_registry_parent",
        ),
        ("spec_registry", "uq_spec_registry_project_id_hash"),
        ("spec_registry", "uq_spec_registry_candidate"),
        ("backlog_artifacts", "uq_backlog_artifact_review_parent"),
        ("backlog_artifacts", "uq_backlog_artifact_version"),
        ("backlog_artifacts", "uq_backlog_artifact_fingerprint"),
        ("roadmap_artifacts", "uq_roadmap_review_parent"),
        ("roadmap_artifacts", "uq_roadmap_version"),
        ("roadmap_artifacts", "uq_roadmap_fingerprint"),
        ("story_artifacts", "uq_story_artifact_review_parent"),
        ("story_artifacts", "uq_story_artifact_version"),
        ("story_artifacts", "uq_story_artifact_fingerprint"),
        ("user_stories", "uq_user_story_artifact_item"),
        ("sprint_plan_artifacts", "uq_sprint_plan_review_parent"),
        ("sprint_plan_artifacts", "uq_sprint_plan_version"),
        ("sprint_plan_artifacts", "uq_sprint_plan_fingerprint"),
        ("spec_registry", "fk_spec_registry_accepted_decision"),
        ("backlog_artifacts", "fk_backlog_artifact_specification"),
        ("roadmap_artifacts", "fk_roadmap_backlog"),
        ("story_artifacts", "fk_story_artifact_backlog"),
        ("story_artifacts", "fk_story_artifact_roadmap"),
        ("user_stories", "fk_user_story_specification"),
        ("user_stories", "fk_user_story_artifact"),
        ("sprint_plan_artifacts", "fk_sprint_plan_specification"),
        ("spec_registry", "ck_spec_registry_status"),
        (
            "sprint_plan_artifact_decisions",
            "ck_sprint_plan_decision_activation",
        ),
    ],
)
def test_issue_210_each_required_named_signature_mutation_is_rejected(
    table_name: str,
    constraint_name: str,
) -> None:
    """Every critical unique, foreign key, and check is fail-closed."""
    mixed = _complete_current_schema()
    _remove_named_table_constraint(
        mixed,
        table_name=table_name,
        constraint_name=constraint_name,
    )

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        _assert_current_business_schema(mixed)


@pytest.mark.parametrize(
    ("table_name", "columns"),
    [
        (
            "backlog_artifacts",
            "project_id, version_number",
        ),
        (
            "backlog_artifacts",
            "project_id, content_fingerprint",
        ),
        (
            "roadmap_artifacts",
            "project_id, version_number",
        ),
        (
            "roadmap_artifacts",
            "project_id, content_fingerprint",
        ),
        (
            "sprint_plan_artifacts",
            "project_id, version_number",
        ),
        (
            "sprint_plan_artifacts",
            "project_id, plan_fingerprint",
        ),
    ],
)
def test_issue_210_fresh_columns_reject_old_project_global_uniques(
    table_name: str,
    columns: str,
) -> None:
    """New lineage columns cannot coexist with one old Project-global unique."""
    mixed = _complete_current_schema()
    with mixed.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE UNIQUE INDEX old_global_unique ON {table_name} ({columns})"
        )

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        _assert_current_business_schema(mixed)


def test_issue_210_fresh_user_story_schema_requires_replay_unique() -> None:
    """A mixed schema without exact Story-item replay protection is unsupported."""
    mixed = _complete_current_schema()
    _remove_replay_unique_or_add_fresh_columns(mixed)

    with pytest.raises(RuntimeError, match="UNSUPPORTED_BUSINESS_SCHEMA"):
        _assert_current_business_schema(mixed)
