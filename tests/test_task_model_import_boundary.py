"""Focused Task boundary checks for the upcoming model extraction."""

from __future__ import annotations

import ast
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


def _module_import_access_paths(module_path: Path, module_name: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    access_paths: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    access_paths.add(alias.asname or alias.name)

    return access_paths


def _dotted_attribute_references(module_path: Path) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    references: set[str] = set()

    def _as_dotted_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = _as_dotted_name(node.value)
            if parent is None:
                return None
            return f"{parent}.{node.attr}"
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted_name = _as_dotted_name(node)
            if dotted_name is not None:
                references.add(dotted_name)

    return references


def test_task_runtime_consumers_import_task_from_models_core_only() -> None:
    # Intentionally limited to the first Task runtime consumers from
    # 2026-04-06-task-sprint-model-extraction.md. Later slices keep Sprint on
    # the shim and are handled in separate tests.
    """Verify task runtime consumers import task from models core only."""
    module_names = [
        "tools.db_tools",
        "orchestrator_agent.agent_tools.sprint_planner_tool.tools",
    ]

    assert not _imported_names_from_source(ROOT / "api.py", "models.core")

    for module_name in module_names:
        module_path = ROOT / (module_name.replace(".", "/") + ".py")
        task_import_sources = _import_sources_for_name(module_path, "Task")
        core_imports = _imported_names_from_source(module_path, "models.core")
        agile_imports = _imported_names_from_source(module_path, "agile_sqlmodel")
        dotted_refs = _dotted_attribute_references(module_path)
        core_task_attr_refs = {
            f"{access_path}.Task"
            for access_path in _module_import_access_paths(module_path, "models.core")
        }
        agile_task_attr_refs = {
            f"{access_path}.Task"
            for access_path in _module_import_access_paths(
                module_path, "agile_sqlmodel"
            )
        }

        assert task_import_sources == {"models.core"}, module_name
        assert "Task" in core_imports or bool(core_task_attr_refs & dotted_refs), (
            module_name
        )
        assert "Task" not in agile_imports, module_name
        assert not (agile_task_attr_refs & dotted_refs), module_name
