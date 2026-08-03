"""Whole-repository hard-break absence regressions from the Task 17 review."""

from __future__ import annotations

import importlib.util
import re
import tokenize
from io import BytesIO
from pathlib import Path

from git import Repo

_ROOT = Path(__file__).resolve().parents[1]
_HISTORICAL_PREFIXES = (
    "docs/superpowers/plans/",
    "docs/superpowers/specs/",
    "artifacts/",
    ".superpowers/",
)
_TEXT_SUFFIXES = {
    ".css",
    ".env",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_ROUTING_LITERALS = (
    "orchestrator" + "_agent",
    "FSM" + "Controller",
    "STATE" + "_REGISTRY",
    "fsm" + "_state",
    "AGILEFORGE" + "_SESSION_DB_URL",
    "GreenfieldDiscovery" + "Context",
    "context" + "_key",
)
_AGGREGATE_LITERALS = (
    "Pro" + "duct",
    "product" + "_id",
    "products." + "product" + "_id",
    "repositories." + "product",
    "_SPEC_" + "PRODUCT_ID",
    "SPEC_" + "PRODUCT_MATCH",
    "PRODUCT" + "_NOT_FOUND",
    "product" + "_spec_linked",
    "product" + "_name",
    "query_" + "product_structure",
    "benchmark_" + "product_structure",
    "link_spec_to_" + "product",
    "product_" + "context",
    "product_" + "authority_cache_persisted",
    "product_" + "not_found",
    "product_" + "description",
    "sample_" + "product",
    "product" + " ID",
    "product" + " identifier",
)
_OBSOLETE_LITERALS = (
    "WORKFLOW_" + "RUNNER_IDENTITY",
    "agile_" + "orchestrator",
    "storage_" + "schema_version",
    "next_" + "actions",
    "AuthorityReview" + "Service",
)
_OBSOLETE_MODULES = (
    "services.agent_workbench.authority_" + "review",
    "services.agent_workbench.authority_" + "regenerate",
    "services.agent_workbench.fake_" + "mutation",
)
_OBSOLETE_PATHS = (
    "adapters/adk/agents/as_" + "built.py",
    "adapters/adk/prompts/as_" + "built.txt",
    "scripts/benchmark_" + "product_structure.py",
    "tests/test_link_spec_to_" + "product.py",
)
_ARTIFACT_IDENTIFIER_PARTS = (
    "product" + "_backlog",
    "product" + "_category",
    "product" + "_definition",
    "product" + "_new_work",
    "product" + "_owner",
    "product" + "_vision",
)


def _tracked_current_paths() -> tuple[Path, ...]:
    repository = Repo(_ROOT)
    tracked = {path for path, _stage in repository.index.entries}
    tracked.update(repository.untracked_files)
    return tuple(
        _ROOT / relative
        for relative in sorted(tracked)
        if relative
        and not relative.startswith(_HISTORICAL_PREFIXES)
        and (_ROOT / relative).is_file()
    )


def _read_tracked_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def test_obsolete_review_runtime_modules_and_paths_are_absent() -> None:
    """Delete command-authoring review helpers and obsolete model leaves."""
    for module_name in _OBSOLETE_MODULES:
        assert importlib.util.find_spec(module_name) is None
    for relative_path in _OBSOLETE_PATHS:
        assert not (_ROOT / relative_path).exists()


def test_all_tracked_current_surfaces_enforce_the_hard_break() -> None:
    """Scan path names and current text across the entire tracked repository."""
    violations: list[str] = []
    literals = (*_ROUTING_LITERALS, *_AGGREGATE_LITERALS, *_OBSOLETE_LITERALS)
    patterns = {
        literal: re.compile(rf"\b{re.escape(literal)}\b") for literal in literals
    }

    for path in _tracked_current_paths():
        relative = path.relative_to(_ROOT).as_posix()
        violations.extend(
            f"{relative}:path:{literal}"
            for literal in literals
            if patterns[literal].search(relative)
        )
        if path.suffix.lower() not in _TEXT_SUFFIXES and path.name != ".env.example":
            continue
        content = _read_tracked_text(path)
        violations.extend(
            f"{relative}:content:{literal}"
            for literal in literals
            if patterns[literal].search(content)
        )

    assert violations == [], "\n".join(violations)


def test_python_identifiers_use_project_for_aggregate_identity() -> None:
    """Permit product artifact terms, but reject product-shaped aggregate names."""
    violations: list[str] = []
    for path in _tracked_current_paths():
        if path.suffix != ".py":
            continue
        relative = path.relative_to(_ROOT).as_posix()
        for token in tokenize.tokenize(BytesIO(path.read_bytes()).readline):
            if token.type != tokenize.NAME:
                continue
            normalized = re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])",
                "_",
                token.string,
            ).lower()
            segments = normalized.split("_")
            if "product" not in segments:
                continue
            if any(part in normalized for part in _ARTIFACT_IDENTIFIER_PARTS):
                continue
            if {"capability", "invariant"}.intersection(segments):
                continue
            violations.append(f"{relative}:{token.start[0]}:{token.string}")

    assert violations == [], "\n".join(violations)
