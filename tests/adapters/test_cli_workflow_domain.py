"""CLI adapter tests for the WorkflowDomain cutover."""

import importlib
from pathlib import Path
from typing import cast

import pytest

from cli import main as cli_main
from cli.workflow_commands import (
    AuthorityDecisionArguments,
    ProjectShellArguments,
    build_decide_authority_request,
    build_open_project_shell_request,
    workflow_next,
    workflow_position,
)
from tests.adapters.test_command_renderer import position_fixture
from workflow.contracts import WorkflowPosition
from workflow.requests import DecideAuthority, OpenProjectShell


def test_version_does_not_compose_production_application(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print installed package version before application composition."""
    version_module = importlib.import_module("services.agent_workbench.version")

    def fail_composition() -> None:
        message = "production application must not be composed"
        raise AssertionError(message)

    monkeypatch.setattr(cli_main, "production_application", fail_composition)

    with pytest.raises(SystemExit) as error:
        cli_main.main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out == f"{version_module.agileforge_version()}\n"


class _FakeApplication:
    def __init__(self, position: WorkflowPosition) -> None:
        self._position = position
        self.position_calls: list[int] = []

    def position(self, *, project_id: int) -> WorkflowPosition:
        self.position_calls.append(project_id)
        return self._position


def test_workflow_next_reads_position_once() -> None:
    """Render workflow-next from exactly one domain position query."""
    application = _FakeApplication(position_fixture())

    payload = workflow_next(application=application, project_id=41)

    assert application.position_calls == [41]
    assert [item["node_id"] for item in payload["commands"]] == [
        "authority.compile",
        "authority.repair",
    ]


def test_workflow_position_can_include_optional_decisions() -> None:
    """Include optional re-entry only when explicitly requested."""
    application = _FakeApplication(position_fixture())

    payload = workflow_position(
        application=application,
        project_id=41,
        include_optional=True,
    )

    assert application.position_calls == [41]
    decisions = cast("list[dict[str, object]]", payload["decisions"])
    assert "scope_extension.start" in {
        cast("str", item["node_id"]) for item in decisions
    }


def test_project_create_builds_open_project_shell() -> None:
    """Require explicit origin when opening a Project Shell."""
    request = build_open_project_shell_request(
        ProjectShellArguments(
            name="Example",
            origin="brownfield",
            idempotency_key="open-41",
            changed_by="cli-user",
            correlation_id="corr-41",
        )
    )

    assert request == OpenProjectShell(
        name="Example",
        origin="brownfield",
        idempotency_key="open-41",
        actor="cli-user",
        correlation_id="corr-41",
    )


def test_mutation_builder_copies_all_position_guards() -> None:
    """Copy every advertised position guard into the exact request."""
    request = build_decide_authority_request(
        AuthorityDecisionArguments(
            project_id=41,
            graph_version="agileforge.workflow.v1",
            expected_fact_fingerprint="facts-41",
            expected_decision_fingerprint="decision-review",
            idempotency_key="accept-41",
            changed_by="cli-user",
            correlation_id="corr-41",
            pending_authority_id=23,
            authority_fingerprint="authority-23",
            review_fingerprint="review-23",
            decision="accepted",
            rationale="Reviewed",
        )
    )

    assert isinstance(request, DecideAuthority)
    assert request.graph_version == "agileforge.workflow.v1"
    assert request.fact_fingerprint == "facts-41"
    assert request.decision_fingerprint == "decision-review"
    assert request.idempotency_key == "accept-41"
    assert request.actor == "cli-user"
    assert request.correlation_id == "corr-41"


def test_cli_adapter_has_no_repository_or_legacy_routing_imports() -> None:
    """Keep CLI adapters free of persistence and old routing dependencies."""
    source = (Path(__file__).parents[2] / "cli" / "workflow_commands.py").read_text()
    assert "repositories" not in source
    assert "services.workflow" not in source
