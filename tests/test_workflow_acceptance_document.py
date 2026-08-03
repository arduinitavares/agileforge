"""Contract tests for the Operator-run workflow graph acceptance package."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

from cli.main import build_parser

CHECKLIST_PATH = Path("docs/testing/workflow-graph-acceptance-checklist.md")
README_PATH = Path("README.md")
SELECTED_REPOSITORIES = {
    "/Users/aaat/projects/caRtola",
    "/Users/aaat/projects/asa-deep-process-control-experiments",
    "/Users/aaat/myfinance",
}
MYFINANCE_BOUNDARY = (
    '"Statement Streams and Coverage" is the real feature supplied by the Operator '
    "to test AgileForge. AgileForge must guide the work through accepted authority, "
    "backlog, roadmap/story, sprint planning, task execution, review, sprint close, "
    "and post-sprint triage. Operator runs every command and owns all MyFinance "
    "changes."
)
EVIDENCE_TEMPLATE = """repository_name: ""
repository_path: ""
repository_commit: ""
repository_dirty: false
agileforge_commit: ""
project_id: 0
graph_versions: []
commands: []
fact_fingerprints: []
decision_fingerprints: []
authority_ids: []
authority_hashes: []
model_ids: []
verification_commands: []
verification_results: []
final_position: {}
observed_failures: []"""
LEGACY_COMMAND_STRINGS = (
    "agileforge " + "workflow state",
    "agileforge " + "project setup",
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
STEP_EVIDENCE_FIELDS = (
    "step_id:",
    "started_at:",
    "completed_at:",
    "command:",
    "result:",
    "graph_version_before:",
    "fact_fingerprint_before:",
    "decision_fingerprint:",
    "instance_key:",
    "graph_version_after:",
    "fact_fingerprint_after:",
    "failure:",
)


def _checklist_text() -> str:
    return CHECKLIST_PATH.read_text(encoding="utf-8")


def test_acceptance_checklist_has_only_the_selected_repositories() -> None:
    """Limit acceptance to the three Operator-selected repository roots."""
    text = _checklist_text()
    absolute_user_paths = set(
        re.findall(r"/Users/aaat/(?:projects/)?[A-Za-z0-9._-]+", text)
    )

    assert absolute_user_paths == SELECTED_REPOSITORIES


def test_myfinance_boundary_and_safety_are_explicit() -> None:
    """Preserve the exact feature statement and synthetic-only ownership rules."""
    text = _checklist_text()

    assert MYFINANCE_BOUNDARY in text
    assert "synthetic evidence only" in text
    assert "isolated MyFinance test environment" in text
    assert "Do not prescribe MyFinance code changes" in text
    assert "Operator owns all external changes" in text


def test_evidence_templates_are_copy_ready() -> None:
    """Keep the required base keys and guarded per-step evidence parseable."""
    text = _checklist_text()
    yaml_blocks = re.findall(r"```yaml\n(.*?)\n```", text, flags=re.DOTALL)

    assert EVIDENCE_TEMPLATE in text
    for field in STEP_EVIDENCE_FIELDS:
        assert field in text
    base_evidence = yaml.safe_load(yaml_blocks[1])
    step_evidence = yaml.safe_load(yaml_blocks[2])
    assert set(base_evidence) == {
        "repository_name",
        "repository_path",
        "repository_commit",
        "repository_dirty",
        "agileforge_commit",
        "project_id",
        "graph_versions",
        "commands",
        "fact_fingerprints",
        "decision_fingerprints",
        "authority_ids",
        "authority_hashes",
        "model_ids",
        "verification_commands",
        "verification_results",
        "final_position",
        "observed_failures",
    }
    assert set(step_evidence["guards"]) == {
        "graph_version_before",
        "fact_fingerprint_before",
        "decision_fingerprint",
        "instance_key",
        "graph_version_after",
        "fact_fingerprint_after",
    }
    assert set(step_evidence["failure"]) == {
        "expected",
        "code",
        "message",
        "mutation_applied",
    }


def test_stop_boundary_keeps_acceptance_not_run() -> None:
    """Prevent checklist preparation from being reported as acceptance."""
    text = _checklist_text()

    assert "acceptance_status: not_run" in text
    assert "checklist preparation is not acceptance execution" in text
    assert "Task 19" in text
    assert not re.search(
        r"(?:caRtola|ASA|MyFinance)\s*(?:acceptance\s*)?(?:status\s*)?[:=-]\s*PASS\b",
        text,
        flags=re.IGNORECASE,
    )


def test_checklist_uses_current_workflow_contract_only() -> None:
    """Require graph guards and exclude deleted command contracts."""
    text = _checklist_text()

    assert "WorkflowDomain.position(project_id)" in text
    assert "agileforge workflow next --project-id <id>" in text
    assert "--expected-fact-fingerprint" in text
    assert "--expected-decision-fingerprint" in text
    assert "facts-only reads" in text
    for legacy_command in LEGACY_COMMAND_STRINGS:
        assert legacy_command not in text


def test_literal_agileforge_examples_parse_with_the_live_parser() -> None:
    """Parse every literal AgileForge example through the production parser."""
    text = _checklist_text()
    literal_commands = re.findall(r"^agileforge .+$", text, flags=re.MULTILINE)

    assert literal_commands
    for command in literal_commands:
        argv = shlex.split(command.replace("<id>", "41"))[1:]
        build_parser().parse_args(argv)


def test_readme_links_the_operator_checklist() -> None:
    """Expose the acceptance package from the repository entry point."""
    readme = README_PATH.read_text(encoding="utf-8")

    assert "docs/testing/workflow-graph-acceptance-checklist.md" in readme
