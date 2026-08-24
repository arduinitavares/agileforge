"""Import boundaries for relocated service contracts and ADK leaf agents."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[2]
CONTRACT_ROOT: Path = ROOT / "services" / "contracts"
AGENT_ROOT: Path = ROOT / "adapters" / "adk" / "agents"

CONTRACT_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "adapters",
    "api",
    "cli",
    "google.adk",
    "litellm",
    "orchestrator" + "_agent",
    "repositories",
    "routers",
)
LEAF_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "models",
    "repositories",
    "sqlmodel",
    "workflow",
)


def imported_modules_under(root: Path) -> set[str]:
    """Return absolute imports declared by Python modules below ``root``."""
    imported: set[str] = set()
    for module_path in sorted(root.rglob("*.py")):
        tree = ast.parse(
            module_path.read_text(encoding="utf-8"),
            filename=str(module_path),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
    return imported


def _matching_prefixes(
    imported: set[str], forbidden_prefixes: tuple[str, ...]
) -> set[str]:
    return {
        name
        for name in imported
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in forbidden_prefixes
        )
    }


def test_service_contracts_have_only_service_owned_dependencies() -> None:
    """Keep deterministic contracts independent from adapters and repositories."""
    assert CONTRACT_ROOT.is_dir()

    imported = imported_modules_under(CONTRACT_ROOT)

    assert not _matching_prefixes(imported, CONTRACT_FORBIDDEN_PREFIXES)


def test_leaf_agents_do_not_own_persistence_or_routing() -> None:
    """Keep retained ADK leaves independent from SQLModel and graph routing."""
    assert AGENT_ROOT.is_dir()

    imported = imported_modules_under(AGENT_ROOT)

    assert not _matching_prefixes(imported, LEAF_FORBIDDEN_PREFIXES)


def test_spec_validator_leaf_uses_direct_specification_review_contract() -> None:
    """Bind the retained paid leaf to the closed direct-Spec output only."""
    source = (AGENT_ROOT / "spec_validator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "services.contracts.specification_validation"
        for alias in node.names
    }
    assert imported_names == {"StorySpecificationReviewOutput"}
    assert "SpecValidationResult" not in source


def test_retained_modules_import_without_deleted_root_composition() -> None:
    """Import current owners without loading the deleted root composition."""
    for module_name in (
        "services.contracts",
        "services.contracts.backlog",
        "services.contracts.roadmap",
        "services.contracts.specification_validation",
        "services.contracts.sprint",
        "services.contracts.story",
        "services.contracts.vision",
        "adapters.adk.agents",
        "adapters.adk.agents.backlog",
        "adapters.adk.agents.roadmap",
        "adapters.adk.agents.spec_validator",
        "adapters.adk.agents.sprint",
        "adapters.adk.agents.story",
        "adapters.adk.agents.vision",
    ):
        importlib.import_module(module_name)

    deleted_root_module = ".".join(("orchestrator" + "_agent", "agent"))
    assert deleted_root_module not in sys.modules
