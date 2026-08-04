"""Structural contract tests for the Operator-run acceptance package."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter

from cli.dev_main import build_parser as build_dev_parser
from cli.main import build_parser
from workflow.contracts import JsonObject, JsonValue

CHECKLIST_PATH = Path("docs/testing/workflow-graph-acceptance-checklist.md")
README_PATH = Path("README.md")
WORKTREE_PATH = (
    "/Users/aaat/projects/agileforge/.worktrees/domain-workflow-graph-hard-break"
)
SELECTED_REPOSITORIES = {
    "/Users/aaat/projects/caRtola",
    "/Users/aaat/projects/asa-deep-process-control-experiments",
    "/Users/aaat/myfinance",
}
ALL_ABSOLUTE_PATHS = {*SELECTED_REPOSITORIES, WORKTREE_PATH}
MYFINANCE_BOUNDARY = (
    '"Statement Streams and Coverage" is the real feature supplied by the Operator '
    "to test AgileForge. AgileForge must guide the work through accepted authority, "
    "backlog, roadmap/story, sprint planning, task execution, review, sprint close, "
    "and post-sprint triage. Operator runs every command and owns all MyFinance "
    "changes."
)
EXPECTED_SECTIONS = (
    "Acceptance State",
    "Scope And Ownership",
    "Reviewed Runtime Pin",
    "Fresh Database Preflight",
    "Command And Placeholder Protocol",
    "caRtola Acceptance",
    "ASA Acceptance",
    "MyFinance Real-Feature Acceptance",
    "Stale-Guard Rejection Probe",
    "Pinned CLI Process Restart",
    "ADK Execution-Trace Reset",
    "Evidence Template",
    "Stop Boundary",
)
COMMON_LIFECYCLE = (
    "Project Shell",
    "repository baseline",
    "complete Git-aware inventory",
    "initial specification curation",
    "project initial-spec",
    "human initial-spec decision",
    "initial scope registration",
    "authority compile",
    "facts-only authority review",
    "human authority decision",
    "position capture",
    "pinned CLI process restart",
    "position recapture",
)
MYFINANCE_LIFECYCLE = (
    "Project Shell",
    "complete Git-aware inventory",
    "project initial-spec",
    "human initial-spec decision",
    "initial scope registration",
    "accepted authority",
    "backlog",
    "roadmap",
    "story",
    "sprint planning",
    "task execution",
    "review",
    "sprint close",
    "post-sprint triage",
)
REQUIRED_EVIDENCE_KEYS = {
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
STEP_STATUS_VALUES = {"not_run", "passed", "failed", "blocked"}
ACCEPTANCE_PROFILES = {
    "caRtola": "acceptance-cartola",
    "ASA": "acceptance-asa",
    "MyFinance": "acceptance-myfinance",
}
LAUNCHER_CLI_PREFIX = (
    "./agileforge-dev cli --profile \"$ACCEPTANCE_PROFILE\" --json -- "
)
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
_JSON_OBJECT = TypeAdapter(JsonObject)


@dataclass(frozen=True)
class ParsedChecklist:
    """Checklist text split into exact second-level sections."""

    title: str
    sections: dict[str, str]

    def section(self, heading: str) -> str:
        """Return one required section body."""
        assert heading in self.sections, f"missing section: {heading}"
        return self.sections[heading]


def _parse_checklist(text: str) -> ParsedChecklist:
    lines = text.splitlines()
    assert lines
    assert lines[0] == "# Workflow Graph Operator Acceptance Checklist"
    headings = [
        (index, line.removeprefix("## "))
        for index, line in enumerate(lines)
        if line.startswith("## ")
    ]
    assert tuple(heading for _, heading in headings) == EXPECTED_SECTIONS
    sections: dict[str, str] = {}
    for item_index, (line_index, heading) in enumerate(headings):
        end = (
            headings[item_index + 1][0]
            if item_index + 1 < len(headings)
            else len(lines)
        )
        sections[heading] = "\n".join(lines[line_index + 1 : end]).strip()
    return ParsedChecklist(title=lines[0], sections=sections)


def _require_ordered(text: str, tokens: tuple[str, ...], *, scope: str) -> None:
    text = " ".join(text.split())
    cursor = -1
    for token in tokens:
        position = text.find(" ".join(token.split()), cursor + 1)
        assert position >= 0, f"{scope} missing ordered token: {token}"
        cursor = position


def _yaml_blocks(text: str) -> list[JsonObject]:
    return [
        _JSON_OBJECT.validate_python(yaml.safe_load(block))
        for block in re.findall(r"```yaml\n(.*?)\n```", text, flags=re.DOTALL)
    ]


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _validate_position_snapshot(snapshot: JsonValue) -> None:
    assert isinstance(snapshot, dict)
    assert set(snapshot) == {
        "captured_at",
        "argv",
        "exit_code",
        "result",
        "graph_version",
        "fact_fingerprint",
        "decision_fingerprint",
        "instance_key",
    }


def _validate_step(step: JsonValue) -> None:
    assert isinstance(step, dict)
    assert set(step) == {
        "step_id",
        "repository",
        "phase",
        "status",
        "timestamps",
        "node_id",
        "request_kind",
        "recommendation_kind",
        "command_template",
        "placeholder_substitutions",
        "runtime_info",
        "executed",
        "positions",
        "guards",
        "authority",
        "model_id",
        "verification",
        "attached_artifacts",
        "failure",
    }
    status = step["status"]
    assert isinstance(status, str)
    assert status in STEP_STATUS_VALUES
    assert set(_object(step["repository"])) == {"name", "path", "commit", "dirty"}
    assert set(_object(step["timestamps"])) == {"started_at", "completed_at"}
    assert set(_object(step["runtime_info"])) == {
        "captured_at",
        "argv",
        "exit_code",
        "result",
    }
    assert set(_object(step["executed"])) == {
        "argv",
        "forwarded_argv",
        "exit_code",
        "production_result",
    }
    positions = _object(step["positions"])
    assert set(positions) == {"before", "after"}
    _validate_position_snapshot(positions["before"])
    _validate_position_snapshot(positions["after"])
    assert set(_object(step["guards"])) == {
        "graph_version",
        "fact_fingerprint",
        "decision_fingerprint",
        "instance_key",
    }
    assert set(_object(step["authority"])) == {"authority_id", "authority_hash"}
    assert set(_object(step["verification"])) == {"command", "result"}
    assert set(_object(step["failure"])) == {
        "kind",
        "code",
        "message",
        "details",
        "expected",
        "mutation_applied",
    }


class ChecklistValidator:
    """Validate the complete section-scoped Operator contract."""

    def __init__(self, text: str) -> None:
        """Parse one candidate checklist for subsequent validation."""
        self.text = text
        self.parsed = _parse_checklist(text)

    def validate(self) -> None:
        """Run structural, lifecycle, safety, and evidence checks."""
        self._validate_paths_and_state()
        self._validate_runtime_and_preflight()
        self._validate_repository_flows()
        self._validate_negative_and_restart_proofs()
        self._validate_evidence()
        self._validate_stop_boundary()

    def _validate_paths_and_state(self) -> None:
        paths = set(
            re.findall(r"/Users/aaat/(?:projects/)?[A-Za-z0-9._/-]+", self.text)
        )
        assert paths == ALL_ABSOLUTE_PATHS
        state = self.parsed.section("Acceptance State")
        assert "acceptance_status: not_run" in state
        assert "No repository acceptance has run" in state
        assert MYFINANCE_BOUNDARY in self.parsed.section(
            "MyFinance Real-Feature Acceptance"
        )

    def _validate_runtime_and_preflight(self) -> None:
        runtime = self.parsed.section("Reviewed Runtime Pin")
        normalized_runtime = " ".join(runtime.split())
        for required in (
            f'AGILEFORGE_WORKTREE="{WORKTREE_PATH}"',
            'git -C "$AGILEFORGE_WORKTREE" rev-parse HEAD',
            "AGILEFORGE_SHA",
            "before every CLI invocation",
            "before each restart boundary",
            (
                './agileforge-dev init --profile "$ACCEPTANCE_PROFILE" '
                '--mode acceptance --expect-sha "$AGILEFORGE_SHA" --json'
            ),
            './agileforge-dev info --profile "$ACCEPTANCE_PROFILE" --json',
        ):
            assert required in normalized_runtime
        for repository, profile in ACCEPTANCE_PROFILES.items():
            assert f"{repository}: `{profile}`" in runtime
        assert runtime.count("--mode acceptance") == 1
        preflight = self.parsed.section("Fresh Database Preflight")
        normalized_preflight = " ".join(preflight.split())
        for required in (
            "one new exact-SHA acceptance profile per repository",
            "prior durable database remains untouched",
            "no migration",
            "AGILEFORGE_DB_URL",
            "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
            "MODEL_CONFIG_PATH",
            "ACCEPTANCE_ACTOR",
            "business and trace paths differ",
            "Profile initialization creates the current schema",
            "Do not export database URLs manually",
        ):
            assert required in normalized_preflight
        assert not re.search(
            (
                r"^export (?:AGILEFORGE_DB_URL|"
                r"AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL|MODEL_CONFIG_PATH)="
            ),
            preflight,
            flags=re.MULTILINE,
        )

    def _validate_repository_flows(self) -> None:
        for heading in ("caRtola Acceptance", "ASA Acceptance"):
            section = self.parsed.section(heading)
            normalized_section = " ".join(section.split())
            _require_ordered(section, COMMON_LIFECYCLE, scope=heading)
            for required in (
                "git_available: true",
                "truncated: false",
                "complete inventory file count and paths",
                "accepted authority ID and hash",
                "same fact fingerprint",
                f"{LAUNCHER_CLI_PREFIX}project initial-spec --project-id",
            ):
                assert required in normalized_section
        myfinance = self.parsed.section("MyFinance Real-Feature Acceptance")
        normalized_myfinance = " ".join(myfinance.split())
        _require_ordered(myfinance, MYFINANCE_LIFECYCLE, scope="MyFinance")
        for required in (
            "synthetic evidence only",
            "isolated MyFinance test environment",
            "approved context",
            "stale-guard rejection",
            "process restart",
            "ADK execution-trace reset",
            "Do not prescribe MyFinance code changes",
            "Operator owns all external changes",
            f"{LAUNCHER_CLI_PREFIX}project initial-spec --project-id",
        ):
            assert required in normalized_myfinance

    def _validate_negative_and_restart_proofs(self) -> None:
        stale = self.parsed.section("Stale-Guard Rejection Probe")
        _require_ordered(
            stale,
            (
                "original guarded command template",
                "first successful mutation",
                "new idempotency key",
                "old graph, fact, decision, and instance guards",
                "require stale rejection",
                "no second mutation",
            ),
            scope="stale probe",
        )
        restart = self.parsed.section("Pinned CLI Process Restart")
        normalized_restart = " ".join(restart.split())
        for required in (
            "one-shot CLI",
            "separate fresh process/shell invocation",
            "same recorded worktree SHA",
            "same AGILEFORGE_DB_URL",
            "same MODEL_CONFIG_PATH",
            "same ACCEPTANCE_ACTOR",
            "same AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
            "same acceptance profile",
            "both argv vectors, process exit results, and timestamps",
        ):
            assert required in normalized_restart
        trace = self.parsed.section("ADK Execution-Trace Reset")
        normalized_trace = " ".join(trace.split())
        for required in (
            "separately configured disposable trace file",
            "inside the acceptance profile root",
            "must differ from the durable business database",
            "no active acceptance CLI process",
            'rm -- "$TRACE_DB_PATH"',
            "delete only that trace file",
            "Never run or invent a session deletion command",
            "new pinned CLI process",
        ):
            assert required in normalized_trace

    def _validate_evidence(self) -> None:
        evidence = self.parsed.section("Evidence Template")
        normalized_evidence = " ".join(evidence.split())
        blocks = _yaml_blocks(evidence)
        assert len(blocks) == 1
        payload = blocks[0]
        assert set(payload) > REQUIRED_EVIDENCE_KEYS
        assert payload["acceptance_status"] == "not_run"
        assert "steps" in payload
        steps = payload["steps"]
        assert isinstance(steps, list)
        assert len(steps) == 1
        _validate_step(steps[0])
        for status in STEP_STATUS_VALUES:
            assert f"`{status}`" in normalized_evidence
        assert "authoritative source" in normalized_evidence
        assert "top-level arrays are summaries" in normalized_evidence
        assert "Overall repository status" in normalized_evidence
        assert "every required step is passed" in normalized_evidence
        assert "incomplete evidence remains not_run" in normalized_evidence
        assert "Task 19" in normalized_evidence
        assert "`info --json` before every product CLI step" in normalized_evidence
        assert "exact forwarded argv" in normalized_evidence
        assert "production JSON result" in normalized_evidence

    def _validate_stop_boundary(self) -> None:
        stop = self.parsed.section("Stop Boundary")
        normalized_stop = " ".join(stop.split()).lower()
        assert "checklist preparation is not acceptance execution" in normalized_stop
        assert "acceptance_status: not_run" in normalized_stop
        assert "do not start task 19" in normalized_stop
        assert not re.search(
            r"(?:caRtola|ASA|MyFinance)\s*(?:status\s*)?[:=-]\s*PASS\b",
            self.text,
            flags=re.IGNORECASE,
        )
        for legacy_command in LEGACY_COMMAND_STRINGS:
            assert legacy_command not in self.text


def _checklist_text() -> str:
    return CHECKLIST_PATH.read_text(encoding="utf-8")


def test_acceptance_checklist_has_complete_structural_contract() -> None:
    """Require every review, lifecycle, safety, and evidence section."""
    ChecklistValidator(_checklist_text()).validate()


def test_keyword_only_document_cannot_satisfy_contract() -> None:
    """Reject a synthetic bundle of keywords without scoped procedures."""
    keyword_only = f"""# Workflow Graph Operator Acceptance Checklist
{MYFINANCE_BOUNDARY}
{WORKTREE_PATH}
{chr(10).join(sorted(SELECTED_REPOSITORIES))}
acceptance_status: not_run
Project Shell repository baseline complete Git-aware inventory initial specification
curation project initial-spec human decision authority compile backlog roadmap story
sprint planning task execution review sprint close post-sprint triage
AGILEFORGE_DB_URL MODEL_CONFIG_PATH instance_key Task 19
```yaml
{yaml.safe_dump({key: [] for key in REQUIRED_EVIDENCE_KEYS})}```
"""

    with pytest.raises(AssertionError):
        ChecklistValidator(keyword_only).validate()


def test_literal_pinned_cli_examples_parse_with_live_parser() -> None:
    """Parse checkout-local launcher examples and their forwarded CLI argv."""
    commands = re.findall(
        r"^\./agileforge-dev .+$",
        _checklist_text(),
        flags=re.MULTILINE,
    )
    assert commands
    assert any("project initial-spec" in command for command in commands)
    replacements = {
        "$ACCEPTANCE_PROFILE": "acceptance-cartola",
        "$AGILEFORGE_SHA": "a" * 40,
        "$PROJECT_ID": "41",
        "$PROJECT_NAME": "Acceptance Project",
        "$PROJECT_OPEN_KEY": "open-41",
        "$ACCEPTANCE_ACTOR": "operator",
    }
    for command in commands:
        tokens = shlex.split(command)
        launcher_argv = [replacements.get(token, token) for token in tokens[1:]]
        build_dev_parser().parse_args(launcher_argv)
        if "--" in launcher_argv:
            forwarded = launcher_argv[launcher_argv.index("--") + 1 :]
            build_parser().parse_args(forwarded)
    assert not re.search(r"^agileforge ", _checklist_text(), flags=re.MULTILINE)
    assert "uv run --frozen agileforge" not in _checklist_text()


def test_readme_links_the_operator_checklist() -> None:
    """Expose the acceptance package from the repository entry point."""
    readme = README_PATH.read_text(encoding="utf-8")
    assert "docs/testing/workflow-graph-acceptance-checklist.md" in readme
