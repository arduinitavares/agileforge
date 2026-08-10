"""Whole-repository hard-break absence regressions from the Task 17 review."""

from __future__ import annotations

import importlib.util
import re
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
    "F" + "SMController",
    "STATE" + "_REGISTRY",
    "fsm" + "_state",
    "AGILEFORGE" + "_SESSION_DB_URL",
    "Green" + "fieldDiscovery" + "Context",
    "context" + "_key",
)
_OBSOLETE_LITERALS = (
    "WORKFLOW_" + "RUNNER_IDENTITY",
    "agile_" + "orchestrator",
    "storage_" + "schema_version",
    "next_" + "actions",
    "AuthorityReview" + "Service",
    "Cli" + "MutationLedger",
    "MutationLedger" + "Repository",
    "cli_" + "mutation" + "_ledger",
    "mutation_" + "ledger",
    "mutation " + "ledger",
)
_OBSOLETE_MODULES = (
    "models.agent_" + "workbench",
    "services.agent_workbench.authority_" + "review",
    "services.agent_workbench.authority_" + "regenerate",
    "services.agent_workbench.fake_" + "mutation",
    "services.agent_workbench.mutation_" + "ledger",
    "services.agent_workbench.backlog_refinement_" + "events",
)
_OBSOLETE_PATHS = (
    "adapters/adk/agents/as_" + "built.py",
    "adapters/adk/prompts/as_" + "built.txt",
    "scripts/benchmark_" + "product_structure.py",
    "tests/test_link_spec_to_" + "product.py",
    "tests/test_agent_workbench_mutation_" + "ledger.py",
)
_DELETED_COMMAND_LITERALS = (
    "agileforge workflow " + "state",
    "agileforge project " + "setup",
    "agileforge authority " + "accept",
    "agileforge authority " + "reject",
    "agileforge authority " + "curate",
    "agileforge authority " + "regenerate",
    "agileforge backlog " + "reset-active",
    "agileforge sprint " + "save",
    "agileforge story " + "save",
    "--expected-" + "state",
    "--expected-context-" + "fingerprint",
)
_DELETED_ROUTING_PROSE = (
    "F" + "SM",
    "workbench F" + "SM",
    "F" + "SM/session state",
    "session as " + "authority",
    "session-as-" + "authority",
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
    literals = (
        *_ROUTING_LITERALS,
        *_OBSOLETE_LITERALS,
        *_DELETED_COMMAND_LITERALS,
        *_DELETED_ROUTING_PROSE,
    )
    patterns = {
        literal: re.compile(rf"(?<![A-Za-z0-9_]){re.escape(literal)}(?![A-Za-z0-9_])")
        for literal in literals
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
