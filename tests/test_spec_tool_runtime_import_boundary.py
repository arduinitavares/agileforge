"""Focused boundary checks for spec tool runtime import cleanup."""

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


def test_spec_tools_import_boundary() -> None:
    """Verify spec tools import boundary."""
    module_path = ROOT / "tools/spec_tools.py"

    core_imports = _imported_names_from(module_path, "models.core")
    core_bound_imports = _bound_import_names_from(module_path, "models.core")
    db_imports = _imported_names_from(module_path, "models.db")
    db_bound_imports = _bound_import_names_from(module_path, "models.db")
    spec_imports = _imported_names_from(module_path, "models.specs")
    spec_bound_imports = _bound_import_names_from(module_path, "models.specs")
    agile_imports = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases = _module_import_aliases(module_path, "agile_sqlmodel")

    assert {"Feature", "UserStory"} <= core_imports
    assert {"Feature", "UserStory"} <= core_bound_imports
    assert db_imports == {"engine", "get_engine"}
    assert db_bound_imports == {"engine", "get_engine"}
    assert spec_imports == {"CompiledSpecAuthority"}
    assert spec_bound_imports == {"CompiledSpecAuthority"}
    assert not agile_imports
    assert not agile_aliases
