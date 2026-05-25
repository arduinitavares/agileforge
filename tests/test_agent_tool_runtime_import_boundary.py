"""Focused boundary checks for ADK tool runtime import cleanup."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]


def _imported_names_from(module_path: Path, import_source: str) -> set[str]:
    source_text: str = module_path.read_text(encoding="utf-8")
    tree: ast.Module = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == import_source:
            imported_names.update(alias.name for alias in node.names)

    return imported_names


def _module_import_aliases(module_path: Path, module_name: str) -> set[str]:
    source_text: str = module_path.read_text(encoding="utf-8")
    tree: ast.Module = ast.parse(source_text, filename=str(module_path))
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
    source_text: str = module_path.read_text(encoding="utf-8")
    tree: ast.Module = ast.parse(source_text, filename=str(module_path))
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


def test_product_vision_tool_imports_runtime_surfaces_from_models_packages() -> None:
    """Check that ProductVisionTool imports only from models packages at runtime."""
    module_path: Path = (
        ROOT / "orchestrator_agent/agent_tools/product_vision_tool/tools.py"
    )

    core_imports: set[str] = _imported_names_from(module_path, "models.core")
    db_imports: set[str] = _imported_names_from(module_path, "models.db")
    event_imports: set[str] = _imported_names_from(module_path, "models.events")
    enum_imports: set[str] = _imported_names_from(module_path, "models.enums")
    agile_imports: set[str] = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases: set[str] = _module_import_aliases(module_path, "agile_sqlmodel")
    agile_attr_refs: set[str] = _attribute_references_from_import(
        module_path,
        {"agile_sqlmodel", *agile_aliases},
        {"Product", "get_engine", "WorkflowEvent", "WorkflowEventType"},
    )

    assert core_imports == {"Product"}
    assert "get_engine" in db_imports
    assert "WorkflowEvent" in event_imports
    assert "WorkflowEventType" in enum_imports
    assert "*" not in agile_imports
    assert "Product" not in agile_imports
    assert "get_engine" not in agile_imports
    assert "WorkflowEvent" not in agile_imports
    assert "WorkflowEventType" not in agile_imports
    assert not agile_attr_refs


def test_roadmap_builder_tool_imports_runtime_surfaces_from_models_packages() -> None:
    """Check that RoadmapBuilderTool imports only from models packages at runtime."""
    module_path = ROOT / "orchestrator_agent/agent_tools/roadmap_builder/tools.py"

    core_imports: set[str] = _imported_names_from(module_path, "models.core")
    db_imports: set[str] = _imported_names_from(module_path, "models.db")
    event_imports: set[str] = _imported_names_from(module_path, "models.events")
    enum_imports: set[str] = _imported_names_from(module_path, "models.enums")
    agile_imports: set[str] = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases: set[str] = _module_import_aliases(module_path, "agile_sqlmodel")
    agile_attr_refs: set[str] = _attribute_references_from_import(
        module_path,
        {"agile_sqlmodel", *agile_aliases},
        {"Product", "get_engine", "WorkflowEvent", "WorkflowEventType"},
    )

    assert core_imports == {"Product"}
    assert "get_engine" in db_imports
    assert "WorkflowEvent" in event_imports
    assert "WorkflowEventType" in enum_imports
    assert "*" not in agile_imports
    assert "Product" not in agile_imports
    assert "get_engine" not in agile_imports
    assert "WorkflowEvent" not in agile_imports
    assert "WorkflowEventType" not in agile_imports
    assert not agile_attr_refs


def test_user_story_writer_tool_imports_runtime_surfaces_from_models_packages() -> None:
    """Check that UserStoryWriterTool imports only from models packages at runtime."""
    module_path: Path = (
        ROOT / "orchestrator_agent/agent_tools/user_story_writer_tool/tools.py"
    )

    core_imports: set[str] = _imported_names_from(module_path, "models.core")
    db_imports: set[str] = _imported_names_from(module_path, "models.db")
    event_imports: set[str] = _imported_names_from(module_path, "models.events")
    enum_imports: set[str] = _imported_names_from(module_path, "models.enums")
    agile_imports: set[str] = _imported_names_from(module_path, "agile_sqlmodel")
    agile_aliases: set[str] = _module_import_aliases(module_path, "agile_sqlmodel")
    agile_attr_refs: set[str] = _attribute_references_from_import(
        module_path,
        {"agile_sqlmodel", *agile_aliases},
        {"Product", "UserStory", "get_engine", "WorkflowEvent", "WorkflowEventType"},
    )

    assert core_imports == {"Product", "UserStory", "UserStoryDependency"}
    assert "get_engine" in db_imports
    assert "WorkflowEvent" in event_imports
    assert "WorkflowEventType" in enum_imports
    assert agile_imports == set()
    assert "*" not in agile_imports
    assert not agile_attr_refs
