"""Focused boundary checks for utility tool runtime import cleanup."""

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


def _bound_import_names_from(module_path: Path, import_source: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == import_source:
            imported_names.update(alias.asname or alias.name for alias in node.names)

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


def _attribute_references_from_import(
    module_path: Path, import_names: set[str], attrs: set[str]
) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    referenced: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in import_names
            and node.attr in attrs
        ):
            referenced.add(node.attr)

    return referenced


def _attribute_references_to_module(
    module_path: Path, module_name: str, attrs: set[str]
) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    referenced: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == module_name
            and node.attr in attrs
        ):
            referenced.add(node.attr)

    return referenced


def test_db_tools_import_boundary() -> None:
    """Verify db tools import boundary."""
    module_path = ROOT / "tools/db_tools.py"

    core_imports = _imported_names_from(module_path, "models.core")
    db_imports = _imported_names_from(module_path, "models.db")
    agile_imports = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases = _module_import_aliases(module_path, "agile_sqlmodel")
    agile_task_attr_refs = _attribute_references_to_module(
        module_path, "agile_sqlmodel", {"Task"}
    )

    assert core_imports == {
        "Epic",
        "Feature",
        "Project",
        "ProjectPersona",
        "Theme",
    }
    assert db_imports == {"get_engine"}
    assert agile_imports == set()
    assert agile_aliases == set()
    assert not agile_task_attr_refs


def test_export_snapshot_import_boundary() -> None:
    """Verify export snapshot import boundary."""
    module_path = ROOT / "tools/export_snapshot.py"

    core_imports = _imported_names_from(module_path, "models.core")
    db_imports = _imported_names_from(module_path, "models.db")
    db_bound_imports = _bound_import_names_from(module_path, "models.db")
    enum_imports = _imported_names_from(module_path, "models.enums")
    specs_imports = _imported_names_from(module_path, "models.specs")
    agile_imports = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases = _module_import_aliases(module_path, "agile_sqlmodel")
    agile_attr_refs = _attribute_references_from_import(
        module_path, {"agile_sqlmodel", *agile_aliases}, {"Sprint"}
    )

    assert core_imports == {
        "Epic",
        "Feature",
        "Project",
        "Sprint",
        "SprintStory",
        "Theme",
        "UserStory",
    }
    assert db_imports == {"engine"}
    assert db_bound_imports == {"default_engine"}
    assert enum_imports == {"StoryStatus"}
    assert specs_imports == set()
    assert agile_imports == set()
    assert agile_aliases == set()
    assert not agile_attr_refs
