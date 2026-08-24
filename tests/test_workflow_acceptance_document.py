"""Contracts for the current workflow operator acceptance checklist."""

from __future__ import annotations

from pathlib import Path

CHECKLIST = Path("docs/testing/workflow-graph-acceptance-checklist.md")


def _text() -> str:
    return CHECKLIST.read_text(encoding="utf-8")


def test_acceptance_checklist_has_current_sections() -> None:
    """Keep the checklist organized around the single durable lifecycle."""
    headings = {
        line.removeprefix("## ")
        for line in _text().splitlines()
        if line.startswith("## ")
    }
    assert headings == {
        "Acceptance State",
        "Targets",
        "Reviewed Runtime Pin",
        "Project Creation",
        "Command Protocol",
        "Lifecycle Evidence",
        "Target Notes",
        "Restart Proof",
        "Trace Reset Proof",
        "Stale Position Proof",
        "Distribution And Quality Evidence",
        "Stop Conditions",
    }


def test_acceptance_checklist_names_each_retained_stage() -> None:
    """Cover every retained human and generated artifact boundary."""
    text = _text()
    for term in (
        "Vision",
        "Product Goal",
        "Specification candidate",
        "Backlog",
        "Roadmap",
        "Stories",
        "Sprint",
        "post-Sprint triage",
    ):
        assert term in text
    assert "Discovery activities" in text
    assert "not an artifact or lifecycle gate" in text


def test_acceptance_checklist_uses_current_cli_entrypoints() -> None:
    """Pin creation and facts-first routing to the checkout launcher."""
    text = _text()
    assert "./agileforge-dev init" in text
    assert "project create" in text
    assert "workflow position" in text
    assert "workflow next" in text
    assert "project " + "initial-spec" not in text


def test_acceptance_checklist_records_required_quality_gates() -> None:
    """Keep deletion, distribution, type, lint, format, and diff evidence."""
    text = _text()
    for command in (
        "tests/test_single_lifecycle_absence.py",
        "scripts/verify_distribution.py",
        "ruff check .",
        "ty check",
        "ruff format --check .",
        "git diff --check",
    ):
        assert command in text
