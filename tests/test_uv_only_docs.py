"""Contracts for uv-only setup and checkout-local runtime guidance."""

from __future__ import annotations

import re
from pathlib import Path

README_PATH = Path("README.md")
ENV_EXAMPLE_PATH = Path(".env.example")
AGENTS_PATH = Path("AGENTS.md")
CLI_MANUAL_PATH = Path("docs/agent-cli-manual.md")
ACCEPTANCE_CHECKLIST_PATH = Path("docs/testing/workflow-graph-acceptance-checklist.md")
DESIGN_PATH = Path(
    "docs/superpowers/specs/2026-08-03-uv-only-developer-runtime-and-ci-design.md"
)
PLAN_PATH = Path(
    "docs/superpowers/plans/2026-08-03-uv-only-developer-runtime-and-ci.md"
)
CURRENT_OPERATING_DOCS = (
    README_PATH,
    ENV_EXAMPLE_PATH,
    AGENTS_PATH,
    CLI_MANUAL_PATH,
    ACCEPTANCE_CHECKLIST_PATH,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_current_operating_docs_gate_has_exact_bounded_scope() -> None:
    """Cover current guidance while excluding historical design and plan docs."""
    assert set(CURRENT_OPERATING_DOCS) == {
        README_PATH,
        ENV_EXAMPLE_PATH,
        AGENTS_PATH,
        CLI_MANUAL_PATH,
        ACCEPTANCE_CHECKLIST_PATH,
    }


def test_current_operating_docs_use_only_uv_for_python_setup() -> None:
    """Reject alternate Python installers without adding stale scan literals."""
    text = "\n".join(_read(path) for path in CURRENT_OPERATING_DOCS).lower()
    installer_names = ("p" + "ip", "po" + "etry")

    for installer in installer_names:
        assert f"{installer} install" not in text
    assert "python -m " + installer_names[0] not in text
    assert "uv sync --frozen" in _read(README_PATH)


def test_current_operating_docs_exclude_removed_runtime_guidance() -> None:
    """Reject removed session configuration and the old absolute user shim."""
    removed_session_db_variable = "AGILEFORGE_" + "SESSION_DB_URL"
    old_user_shim = "/Users/aaat/" + ".local/bin/" + "agileforge"

    for path in CURRENT_OPERATING_DOCS:
        text = _read(path)
        assert removed_session_db_variable not in text
        assert old_user_shim not in text


def test_docs_separate_stable_and_checkout_local_commands() -> None:
    """Keep the release command distinct from branch/worktree execution."""
    required_examples = (
        "Stable release: `agileforge workflow next --project-id 1`",
        (
            "Current checkout: `./agileforge-dev cli --profile local -- "
            "workflow next --project-id 1`"
        ),
        ("Current checkout UI: `./agileforge-dev ui --profile local --port auto`"),
        "Provenance: `./agileforge-dev info --profile local --json`",
    )

    for path in (README_PATH, CLI_MANUAL_PATH):
        text = _read(path)
        for example in required_examples:
            assert example in text
        assert "./agileforge-dev init --profile local --json" in text


def test_environment_example_names_separate_runtime_inputs() -> None:
    """Expose business, trace, and model configuration without legacy roots."""
    text = _read(ENV_EXAMPLE_PATH)

    for variable in (
        "AGILEFORGE_DB_URL",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        "MODEL_CONFIG_PATH",
    ):
        assert re.search(rf"^{variable}=", text, flags=re.MULTILINE)
    assert "AGILEFORGE_CONFIG_ROOT" not in text
    assert "/Users/" not in text


def test_agents_registers_development_branch_runtime_rule() -> None:
    """Make branch runtime ownership durable for fresh agent sessions."""
    text = " ".join(_read(AGENTS_PATH).split())
    expected = " ".join(
        (
            "## Development Branch Runtime",
            "Use only uv. For a development branch or linked worktree, invoke",
            "that checkout's `./agileforge-dev`; never use a bare or user-level",
            "`agileforge` shim. Run `info --json` before mutations. Each worktree",
            "owns separate profiles, business and ADK trace databases, and UI",
            "ports. Older branches must merge or rebase the launcher change",
            "before using it.",
        )
    )

    assert expected in text


def test_info_is_the_complete_redacted_operator_preflight() -> None:
    """Document one machine-readable command for models, credentials, and child env."""
    for path in (README_PATH, CLI_MANUAL_PATH, ACCEPTANCE_CHECKLIST_PATH):
        text = _read(path)
        for field in (
            "configured_models",
            "provider_credentials",
            "child_runtime_environment",
        ):
            assert field in text
        assert "credential values" in text
    assert "--secrets-file" in _read(CLI_MANUAL_PATH)
    assert "--secrets-file" in _read(ACCEPTANCE_CHECKLIST_PATH)


def test_bootstrap_policy_is_limited_to_checkout_and_uv_isolation() -> None:
    """Keep shell ownership narrow while naming every hostile uv selector."""
    for path in (DESIGN_PATH, PLAN_PATH):
        normalized = " ".join(_read(path).split())
        assert "checkout and uv isolation policy" in normalized
        assert "no application, database, or routing policy" in normalized
        for variable in (
            "UV_PROJECT",
            "UV_PROJECT_ENVIRONMENT",
            "UV_NO_SYNC",
            "UV_WORKING_DIR",
            "UV_WORKING_DIRECTORY",
        ):
            assert variable in normalized
