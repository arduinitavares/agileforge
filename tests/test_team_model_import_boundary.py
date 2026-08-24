"""Focused boundary checks for Team/TeamMember import cleanup."""

from __future__ import annotations

import ast
from pathlib import Path


def _imported_names_from(module_path: Path, import_source: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == import_source:
            imported_names.update(alias.name for alias in node.names)

    return imported_names


def _module_level_imported_names_from(
    module_path: Path, import_source: str
) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == import_source:
            imported_names.update(alias.name for alias in node.names)

    return imported_names


def test_selected_test_and_script_modules_import_team_from_models_core() -> None:
    """Verify selected test and script modules import team from models core."""
    root = Path(__file__).resolve().parents[1]
    selected_modules = [
        Path("tests/unit/test_delete_project.py"),
    ]

    for module_path in selected_modules:
        core_imports = _module_level_imported_names_from(
            root / module_path, "models.core"
        )
        agile_imports = _module_level_imported_names_from(
            root / module_path, "agile_sqlmodel"
        )

        assert "Team" in core_imports, module_path
        assert "Team" not in agile_imports, module_path
        if "TeamMember" in core_imports or "TeamMember" in agile_imports:
            assert "TeamMember" in core_imports, module_path
            assert "TeamMember" not in agile_imports, module_path


def test_conftest_imports_team_models_from_models_core() -> None:
    """Verify conftest imports team models from models core."""
    root = Path(__file__).resolve().parents[1]
    core_imports = _imported_names_from(root / "tests/conftest.py", "models.core")
    agile_imports = _imported_names_from(root / "tests/conftest.py", "agile_sqlmodel")

    assert {"Team", "TeamMember"} <= core_imports
    assert {"Team", "TeamMember"}.isdisjoint(agile_imports)
