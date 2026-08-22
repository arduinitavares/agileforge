"""Boundary checks for the repository shim-cleanup slice."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imported_names_from(module_path: Path, import_source: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == import_source:
            imported_names.update(alias.name for alias in node.names)

    return imported_names


def _module_import_aliases(module_path: Path, module_name: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    aliases.add(alias.asname or alias.name)

    return aliases


def test_project_repository_import_boundary() -> None:
    """Verify the canonical Project repository import boundary."""
    module_path = ROOT / "repositories/project.py"

    core_imports = _imported_names_from(module_path, "models.core")
    db_imports = _imported_names_from(module_path, "models.db")
    event_imports = _imported_names_from(module_path, "models.events")
    spec_imports = _imported_names_from(module_path, "models.specs")
    agile_imports = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases = _module_import_aliases(module_path, "agile_sqlmodel")

    assert {
        "Project",
        "ProjectPersona",
        "Sprint",
        "SprintStory",
        "Task",
        "UserStory",
        "Epic",
        "Feature",
        "Theme",
    } <= core_imports
    assert db_imports == {"get_engine"}
    assert {"StoryCompletionLog", "WorkflowEvent"} <= event_imports
    assert spec_imports == {"SpecRegistry"}
    assert not agile_imports
    assert not agile_aliases
