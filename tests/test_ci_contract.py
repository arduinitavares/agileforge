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
PYREPO_CHECK_REVISION = "40119c00d4efc469655dec16b1a976e1b3298d7d"
PYREPO_CHECK_SOURCE = (
    f"git+https://github.com/arduinitavares/pyrepo-check.git@{PYREPO_CHECK_REVISION}"
)
CANONICAL_PYTHON = "3.13.15"
CI_UV_VERSION = "0.12.8"


class WorkflowLoader(yaml.SafeLoader):
    """Load workflow YAML without treating the literal key ``on`` as a boolean."""


WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != BOOLEAN_TAG]
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
        pytest.skip(
            "workflow is absent until the Task 6 implementation"  # ty: ignore[too-many-positional-arguments]
        )
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
        run for step in _steps(job) if isinstance((run := step.get("run")), str)
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
    """Pin every Python CI job to the canonical repository runtime."""
    expected = {
        "python-313": ("ubuntu-latest", CANONICAL_PYTHON),
        "macos-smoke": ("macos-latest", CANONICAL_PYTHON),
        "windows-vision-evidence": ("windows-latest", CANONICAL_PYTHON),
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


def test_python_jobs_install_canonical_python_before_use(
    workflow: dict[str, object],
) -> None:
    """Provision the exact Python before controller or repository execution."""
    first_consumers = {
        "python-313": "Install pyrepo-check controller",
        "macos-smoke": "Exercise launcher lifecycle",
        "windows-vision-evidence": "Run secure Windows evidence suites",
    }
    install_command = f"uv python install {CANONICAL_PYTHON}"

    for job_name, consumer_name in first_consumers.items():
        steps = _steps(_job(workflow, job_name))
        setup_index = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
        )
        install_indices = [
            index
            for index, step in enumerate(steps)
            if step.get("run") == install_command
        ]
        consumer_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == consumer_name
        )

        assert len(install_indices) == 1
        assert setup_index < install_indices[0] < consumer_index


def test_python_jobs_install_playwright_chromium_before_gate(
    workflow: dict[str, object],
) -> None:
    """Provision Chromium before running browser-bearing Python gates."""
    commands = [
        run
        for step in _steps(_job(workflow, "python-313"))
        if isinstance((run := step.get("run")), str)
    ]
    install_command = (
        "uv run --locked python -m playwright install --with-deps chromium"
    )

    assert install_command in commands
    assert commands.index(install_command) < commands.index("./agileforge-dev check")


def test_python_jobs_install_immutable_global_controller_before_gate(
    workflow: dict[str, object],
) -> None:
    """Keep the controller outside the locked Repository Environment."""
    steps = _steps(_job(workflow, "python-313"))
    install_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install pyrepo-check controller"
    )
    install = steps[install_index]
    install_command = install.get("run")

    assert isinstance(install_command, str)
    normalized_install = " ".join(install_command.split())
    assert (
        f'uv tool install --python 3.13.15 "{PYREPO_CHECK_SOURCE}"'
        in normalized_install
    )
    assert 'echo "$(uv tool dir --bin)" >> "$GITHUB_PATH"' in install_command

    gate_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name")
        in {"Run repository quality gate", "Run canonical full gate"}
    )
    assert install_index < gate_index


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
                assert _mapping(step["with"])["version"] == CI_UV_VERSION

    assert observed_actions == {
        action: {revision} for action, revision in expected_actions.items()
    }


def test_jobs_invoke_locked_repository_surfaces(workflow: dict[str, object]) -> None:
    """Use uv lock checks and repository-owned quality/runtime entry points."""
    python_313 = _runs(_job(workflow, "python-313"))
    frontend = _runs(_job(workflow, "frontend"))

    assert "uv lock --check" in python_313
    assert "./agileforge-dev check" in python_313
    assert "uv run --locked pyrepo-check" not in python_313
    assert "pyrepo-check --python" not in python_313
    assert "scripts/verify_distribution.py" not in python_313
    assert (
        "node --test tests/test_workflow_position_display.mjs "
        "tests/test_create_project_modal_required_fields.mjs "
        "tests/test_vision_interview_ui.mjs"
    ) in " ".join(frontend.split())


def test_macos_smoke_delegates_to_exact_repository_command(
    workflow: dict[str, object],
) -> None:
    """Keep lifecycle policy in one tested repository command."""
    commands = [
        " ".join(run.split())
        for step in _steps(_job(workflow, "macos-smoke"))
        if isinstance((run := step.get("run")), str)
        and "scripts/ci_launcher_smoke.py" in run
    ]
    expected = (
        "uv run --locked python scripts/ci_launcher_smoke.py "
        "--profile ci-macos-${{ github.run_id }}-${{ github.run_attempt }} "
        "--expect-sha ${{ github.sha }}"
    )

    assert commands == [expected]
    smoke = commands[0]
    for policy_token in (
        "./agileforge-dev",
        "kill",
        "trap",
        "sleep",
        "json",
        "urllib",
        "process_id",
        "ephemeral_profile",
        "reset",
        "--reload",
    ):
        assert policy_token not in smoke


def test_windows_job_runs_only_provider_free_evidence_contracts(
    workflow: dict[str, object],
) -> None:
    """Exercise real Windows handle semantics without profiles or providers."""
    commands = [
        " ".join(run.split())
        for step in _steps(_job(workflow, "windows-vision-evidence"))
        if isinstance((run := step.get("run")), str) and "pytest" in run
    ]
    expected = (
        "uv run --locked pytest "
        "tests/windows/test_vision_evidence_windows.py "
        "tests/services/test_vision_evidence_reader.py "
        "tests/adapters/test_vision_bootstrap_api.py "
        "tests/adapters/test_cli_workflow_domain.py -q"
    )

    assert commands == [expected]
    source = _runs(_job(workflow, "windows-vision-evidence")).lower()
    for forbidden in (
        "secrets.",
        "openrouter",
        "open_router",
        "agileforge-dev init",
        "--profile",
        "--live",
    ):
        assert forbidden not in source


def test_workflow_has_no_provider_secrets_or_live_markers() -> None:
    """Keep CI offline from provider credentials and live integrations."""
    source = WORKFLOW_PATH.read_text(encoding="utf-8").lower()

    assert "secrets." not in source
    assert "open_router" not in source
    assert "openrouter" not in source
    assert "--live" not in source
    assert "-m integration" not in source
    assert "integration test" not in source
