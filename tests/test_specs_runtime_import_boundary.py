"""Focused boundary checks for specs-service runtime import cleanup."""

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
    module_path: Path, import_names: set[str]
) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    referenced: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in import_names
        ):
            referenced.add(node.attr)

    return referenced


def test_specs_story_validation_service_import_boundary() -> None:
    """Verify specs story validation service import boundary."""
    module_path = ROOT / "services/specs/story_validation_service.py"

    core_imports = _imported_names_from(module_path, "models.core")
    core_bound_imports = _bound_import_names_from(module_path, "models.core")
    db_imports = _imported_names_from(module_path, "models.db")
    db_bound_imports = _bound_import_names_from(module_path, "models.db")
    specs_imports = _imported_names_from(module_path, "models.specs")
    specs_bound_imports = _bound_import_names_from(module_path, "models.specs")
    agile_imports = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases = _module_import_aliases(module_path, "agile_sqlmodel")
    core_aliases = _module_import_aliases(module_path, "models.core")
    db_aliases = _module_import_aliases(module_path, "models.db")
    specs_aliases = _module_import_aliases(module_path, "models.specs")
    agile_attr_refs = _attribute_references_from_import(
        module_path,
        {"agile_sqlmodel", *agile_aliases},
    )

    assert core_imports == {"UserStory"}
    assert core_bound_imports == {"UserStory"}
    assert db_imports == {"get_engine"}
    assert db_bound_imports == {"get_engine"}
    assert specs_imports == set()
    assert specs_bound_imports == set()
    assert agile_imports == set()
    assert not core_aliases
    assert not db_aliases
    assert not specs_aliases
    assert agile_aliases == set()
    assert agile_attr_refs == set()
