"""Focused UserStory boundary checks for the upcoming model extraction."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from sqlalchemy.schema import ForeignKeyConstraint, UniqueConstraint
from sqlmodel import SQLModel

ROOT = Path(__file__).resolve().parents[1]


def _import_sources_for_name(module_path: Path, name: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    sources: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if alias.name == name:
                    sources.add(node.module)

    return sources


def _imported_names_from_source(module_path: Path, source: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == source:
            imported_names.update(alias.name for alias in node.names)

    return imported_names


def test_task3_user_story_consumers_import_user_story_from_models_core_only() -> None:
    """Verify task3 user story consumers import user story from models core only."""
    from models import core  # noqa: PLC0415

    # Intentionally scoped to the Task 3 runtime consumers from
    # 2026-04-06-user-story-model-extraction.md. Later Phase 6 cleanup slices
    # also moved some adjacent Project/Sprint imports onto models.core, so this
    # contract reflects the current runtime boundary rather than the earlier
    # intermediate shim state.
    module_specs = {
        "services.specs.story_validation_service": {
            "models.core": {"UserStory"},
            "agile_sqlmodel": set(),
        },
    }

    for module_name, expected_sources in module_specs.items():
        module_path = ROOT / (module_name.replace(".", "/") + ".py")
        import_sources = _import_sources_for_name(module_path, "UserStory")

        assert import_sources == {"models.core"}, module_name
        assert (
            _imported_names_from_source(module_path, "models.core")
            == expected_sources["models.core"]
        ), module_name
        assert (
            _imported_names_from_source(module_path, "agile_sqlmodel")
            == expected_sources["agile_sqlmodel"]
        ), module_name

        module = importlib.import_module(module_name)
        assert module.UserStory is core.UserStory, module_name
        assert module.UserStory.__module__ == "models.core", module_name


def test_task3_user_story_is_exact_immutable_story_item_projection() -> None:
    """Require replay-safe Story-item and Specification identities only."""
    from models import core  # noqa: PLC0415

    table = SQLModel.metadata.tables["user_stories"]
    required = {
        "source_story_artifact_id",
        "source_story_artifact_fingerprint",
        "source_story_item_id",
        "source_story_item_fingerprint",
        "accepted_spec_version_id",
        "accepted_spec_hash",
        "spec_item_ids_json",
        "acceptance_criteria_json",
    }
    retired = {
        "acceptance_criteria",
        "source_requirement",
        "refinement_slot",
        "story_origin",
        "is_refined",
        "archived_reason",
        "archived_at",
        "archived_by",
        "archive_reset_attempt_id",
        "archive_previous_status",
        "original_acceptance_criteria",
        "ac_updated_at",
        "ac_update_reason",
        "superseded_by_story_id",
    }
    forbidden_backlog_identity = {
        "source_backlog_artifact_id",
        "source_backlog_artifact_fingerprint",
        "source_backlog_item_id",
        "backlog_item_id",
    }

    column_names = set(table.columns.keys())
    assert required <= column_names
    assert retired.isdisjoint(column_names)
    assert forbidden_backlog_identity.isdisjoint(column_names)
    assert all(not table.c[name].nullable for name in required)
    assert core.UserStory.model_fields["story_description"].is_required()
    assert core.UserStory.model_fields["persona"].is_required()

    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "project_id",
        "source_story_artifact_id",
        "source_story_item_id",
    ) in unique_columns

    foreign_keys = {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("project_id", "accepted_spec_version_id", "accepted_spec_hash"),
        (
            "spec_registry.project_id",
            "spec_registry.spec_version_id",
            "spec_registry.spec_hash",
        ),
    ) in foreign_keys
    assert (
        (
            "project_id",
            "source_story_artifact_id",
            "source_story_artifact_fingerprint",
        ),
        (
            "story_artifacts.project_id",
            "story_artifacts.story_artifact_id",
            "story_artifacts.content_fingerprint",
        ),
    ) in foreign_keys
