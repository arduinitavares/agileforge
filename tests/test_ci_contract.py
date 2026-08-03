"""Structural contract tests for the repository GitHub Actions workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
BOOLEAN_TAG = "tag:yaml.org,2002:bool"


class WorkflowLoader(yaml.SafeLoader):
    """Load workflow YAML without treating the literal key ``on`` as a boolean."""


WorkflowLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != BOOLEAN_TAG
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
WorkflowLoader.add_implicit_resolver(
    BOOLEAN_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast("dict[str, object]", value)


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast("list[object]", value)


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    """Load the workflow with GitHub's literal trigger key preserved."""
    if not WORKFLOW_PATH.is_file():
        pytest.skip("workflow is absent until the Task 6 implementation")
    # WorkflowLoader only changes SafeLoader's boolean resolver for GitHub's `on` key.
    loaded = yaml.load(  # nosec B506
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        Loader=WorkflowLoader,  # noqa: S506
    )
    return _mapping(loaded)


def _job(workflow: dict[str, object], name: str) -> dict[str, object]:
    return _mapping(_mapping(workflow["jobs"])[name])


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    return [_mapping(step) for step in _sequence(job["steps"])]


def _runs(job: dict[str, object]) -> str:
    return "\n".join(
        run
        for step in _steps(job)
        if isinstance((run := step.get("run")), str)
    )


def test_ci_workflow_exists() -> None:
    """Require the Task 6 workflow at the repository-owned path."""
    assert WORKFLOW_PATH.is_file(), f"missing workflow: {WORKFLOW_PATH}"


def test_workflow_triggers_default_branch_and_manual_runs(
    workflow: dict[str, object],
) -> None:
    """Run for pull requests, default-branch pushes, and manual dispatches."""
    triggers = _mapping(workflow["on"])

    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers
    assert _mapping(triggers["push"])["branches"] == ["master"]


def test_workflow_has_read_only_permissions_and_cancellation(
    workflow: dict[str, object],
) -> None:
    """Grant read-only contents access and cancel superseded ref runs."""
    assert workflow["permissions"] == {"contents": "read"}
    concurrency = _mapping(workflow["concurrency"])
    group = concurrency["group"]

    assert isinstance(group, str)
    assert "${{ github.workflow }}" in group
    assert "${{ github.ref }}" in group
    assert concurrency["cancel-in-progress"] is True


def test_workflow_has_required_runtimes(workflow: dict[str, object]) -> None:
    """Cover Ubuntu Python 3.12/3.13, Node, and macOS Python 3.13."""
    expected = {
        "python-312": ("ubuntu-latest", "3.12"),
        "python-313": ("ubuntu-latest", "3.13"),
        "macos-smoke": ("macos-latest", "3.13"),
    }
    for name, (runner, python_version) in expected.items():
        job = _job(workflow, name)
        assert job["runs-on"] == runner
        setup_uv = next(
            step
            for step in _steps(job)
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
        )
        assert _mapping(setup_uv["with"])["python-version"] == python_version

    assert _job(workflow, "frontend")["runs-on"] == "ubuntu-latest"


def test_actions_and_uv_are_exactly_pinned(workflow: dict[str, object]) -> None:
    """Require immutable action revisions and the brief's uv version."""
    expected_actions = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "astral-sh/setup-uv": "11f9893b081a58869d3b5fccaea48c9e9e46f990",
        "actions/setup-node": "249970729cb0ef3589644e2896645e5dc5ba9c38",
    }
    observed_actions: dict[str, set[str]] = {}
    for raw_job in _mapping(workflow["jobs"]).values():
        for step in _steps(_mapping(raw_job)):
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            action, separator, revision = uses.partition("@")
            assert separator == "@"
            assert FULL_SHA.fullmatch(revision), uses
            observed_actions.setdefault(action, set()).add(revision)
            if action == "astral-sh/setup-uv":
                assert _mapping(step["with"])["version"] == "0.10.12"

    assert observed_actions == {
        action: {revision} for action, revision in expected_actions.items()
    }


def test_jobs_invoke_locked_repository_surfaces(workflow: dict[str, object]) -> None:
    """Use uv lock checks and repository-owned quality/runtime entry points."""
    python_312 = _runs(_job(workflow, "python-312"))
    python_313 = _runs(_job(workflow, "python-313"))
    frontend = _runs(_job(workflow, "frontend"))

    assert "uv lock --check" in python_312
    assert "uv run --locked pyrepo-check --all" in python_312
    assert "uv lock --check" in python_313
    assert "./agileforge-dev check" in python_313
    assert "scripts/verify_distribution.py" not in python_313
    assert (
        "node --test tests/test_workflow_position_display.mjs "
        "tests/test_create_project_modal_required_fields.mjs"
    ) in " ".join(frontend.split())


def test_macos_smoke_exercises_json_runtime_and_cleans_processes(
    workflow: dict[str, object],
) -> None:
    """Pin acceptance state, parse JSON, and prove attached UI cleanup."""
    smoke = _runs(_job(workflow, "macos-smoke"))

    assert 'test "$SHA" = "$GITHUB_SHA"' in smoke
    assert "./agileforge-dev init" in smoke
    assert "--mode acceptance" in smoke
    assert '--expect-sha "$GITHUB_SHA"' in smoke
    assert "./agileforge-dev info" in smoke
    assert "./agileforge-dev cli" in smoke
    assert "-- project list" in smoke
    assert "./agileforge-dev ui" in smoke
    assert "--ephemeral" in smoke
    assert "--json" in smoke
    assert "--reload" not in smoke
    assert "uv run --locked python" in smoke
    assert "json.loads" in smoke
    assert "urllib.request.urlopen" in smoke
    assert 'kill -TERM "$launcher_pid"' in smoke
    assert 'kill -0 "$launcher_pid"' in smoke
    assert 'kill -0 "$child_pid"' in smoke
    assert "./agileforge-dev reset" in smoke
    assert "ephemeral_profile" in smoke
    assert "trap cleanup EXIT" in smoke
    assert re.search(r"(?m)^\s*python(?:3)?\b", smoke) is None


def test_workflow_has_no_provider_secrets_or_live_markers() -> None:
    """Keep CI offline from provider credentials and live integrations."""
    source = WORKFLOW_PATH.read_text(encoding="utf-8").lower()

    assert "secrets." not in source
    assert "open_router" not in source
    assert "openrouter" not in source
    assert "--live" not in source
    assert "-m integration" not in source
    assert "integration test" not in source
