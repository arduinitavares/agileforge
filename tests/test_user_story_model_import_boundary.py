"""Focused UserStory boundary checks for the upcoming model extraction."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

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
            "models.core": {"Feature", "UserStory"},
            "agile_sqlmodel": set(),
        },
        "tools.story_query_tools": {
            "models.core": {"Epic", "Feature", "Project", "Theme", "UserStory"},
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
