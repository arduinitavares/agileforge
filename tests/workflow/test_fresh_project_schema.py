"""Fresh-schema regressions for the canonical Project aggregate."""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from models.db import ensure_business_db_ready

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


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
