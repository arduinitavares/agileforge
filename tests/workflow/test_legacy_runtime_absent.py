"""Hard-break regressions for deleted workflow routing surfaces."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PYTHON_ROOTS = (
    "models",
    "repositories",
    "workflow",
    "adapters",
    "services",
    "cli",
    "routers",
    "tools",
    "utils",
)
_CURRENT_SURFACES = (
    *_PYTHON_ROOTS,
    "tests",
    "config",
    "api.py",
    "agile_sqlmodel.py",
    "pyproject.toml",
    "README.md",
    "docs/agent-cli-manual.md",
)
_DELETED_MODULES = (
    "orchestrator" + "_agent",
    "services.workflow",
    "repositories.session",
    "services.agent_workbench.session_reader",
    "services.agent_workbench.application",
    "services.orchestrator_context_service",
    "services.orchestrator_query_service",
    "services.phases.workflow_state",
    "tools.orchestrator_tools",
    "db.migrations",
)
_FORBIDDEN_LITERALS = (
    "orchestrator" + "_agent",
    "FSM" + "Controller",
    "STATE" + "_REGISTRY",
    "fsm" + "_state",
    "AGILEFORGE" + "_SESSION_DB_URL",
    "GreenfieldDiscovery" + "Context",
    "context" + "_key",
)


def _current_files() -> tuple[Path, ...]:
    files: list[Path] = []
    for surface in _CURRENT_SURFACES:
        path = _ROOT / surface
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and "__pycache__" not in candidate.parts
                and candidate.suffix in {".md", ".py", ".toml", ".yaml", ".yml"}
            )
    return tuple(sorted(set(files)))


@pytest.mark.parametrize("module_name", _DELETED_MODULES)
def test_legacy_runtime_modules_are_absent(module_name: str) -> None:
    """Make every deleted routing module unimportable."""
    assert importlib.util.find_spec(module_name) is None


def test_live_python_imports_do_not_reference_deleted_modules() -> None:
    """Keep live Python imports independent of deleted routing modules."""
    violations: list[str] = []
    for root_name in _PYTHON_ROOTS:
        for path in (_ROOT / root_name).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported_names: tuple[str, ...] = ()
                line_number: int | None = None
                if isinstance(node, ast.Import):
                    imported_names = tuple(alias.name for alias in node.names)
                    line_number = node.lineno
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    imported_names = (node.module,)
                    line_number = node.lineno
                if line_number is None:
                    continue
                violations.extend(
                    f"{path.relative_to(_ROOT)}:{line_number}: {imported_name}"
                    for imported_name in imported_names
                    if any(
                        imported_name == deleted
                        or imported_name.startswith(f"{deleted}.")
                        for deleted in _DELETED_MODULES
                    )
                )

    assert violations == []


def test_current_surfaces_contain_no_legacy_routing_literals() -> None:
    """Keep executable, test, config, and current operator surfaces clean."""
    violations: list[str] = []
    for path in _current_files():
        text = path.read_text(encoding="utf-8")
        violations.extend(
            f"{path.relative_to(_ROOT)}: {literal}"
            for literal in _FORBIDDEN_LITERALS
            if literal in text
        )

    assert violations == []
