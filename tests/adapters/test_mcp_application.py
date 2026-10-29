"""Exercise the fixed MCP catalogue against real disposable workflow state."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from threading import Event
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

import api
from adapters.git.repository_probe import GitPythonRepositoryProbe
from cli.main import main as cli_main
from cli.workflow_commands import render_workflow_next
from services.application import (
    AgileForgeApplication,
    CompleteTaskRequest,
    CreateProjectCommand,
    DeliveryActionInputService,
    DeliveryReviewSelectionService,
    ExecutionActionSelectionService,
)
from services.project_lifecycle import ProjectLifecycleService
from services.read_projections import DurableReadProjectionService
from tests.adapters.sprint_retry_fixtures import durable_rows
from tests.adapters.test_adk_workflow_runner import (
    ProviderGraphClock,
    _provider_graph_system,
)
from tests.services.test_specification_source_application import (
    _registered_source_fixture,
)
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

# Deliberate synthetic credential for authentication and disclosure checks.
TOKEN = "mcp-application-secret-canary-304"  # noqa: S105  # nosec B105
READ_TOOLS = {
    "service_identity",
    "project_list",
    "project_show",
    "repository_status",
    "vision_status",
    "product_goal_status",
    "specification_status",
    "specification_review",
    "backlog_review",
    "roadmap_review",
    "story_reviews",
    "sprint_plan_review",
    "accepted_roadmap",
    "artifact_history",
    "story_pending",
    "story_show",
    "sprint_status",
    "sprint_history",
    "sprint_tasks",
    "sprint_task_show",
    "sprint_task_history",
    "workflow_position",
    "workflow_next",
}
COMMAND_TOOLS = {
    "create_project",
    "attach_repository",
    "bootstrap_vision",
    "respond_to_vision",
    "respond_to_product_goal",
    "register_specification_source",
    "structure_specification",
    "generate_backlog",
    "generate_roadmap",
    "generate_story",
    "generate_sprint",
    "complete_task",
    "close_story",
}


def _request(client: TestClient, method: str, params: dict[str, object]) -> dict:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert "result" in body, body
    return body["result"]


def _call(client: TestClient, tool_name: str, **arguments: object) -> dict:
    return _request(client, "tools/call", {"name": tool_name, "arguments": arguments})


@pytest.fixture
def workflow_system(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Engine, AgileForgeApplication, int, int]]:
    """Bind every transport call to the same real application and database."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=ProviderGraphClock(),
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        read_projection=DurableReadProjectionService(engine=engine),
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )
    application.set_project_lifecycle(
        ProjectLifecycleService(
            engine=engine,
            workflow_domain=domain,
            repository_probe=GitPythonRepositoryProbe(),
        )
    )
    monkeypatch.setattr(api, "_application", lambda: application)
    monkeypatch.setenv("AGILEFORGE_MCP_BEARER_TOKEN", TOKEN)
    try:
        yield engine, application, project_id, task_id
    finally:
        engine.dispose()


def test_catalogue_is_explicit_and_preserves_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery must expose only typed approved operations without composition."""
    monkeypatch.setenv("AGILEFORGE_MCP_BEARER_TOKEN", TOKEN)

    def forbidden_application() -> AgileForgeApplication:
        message = "Discovery must not compose or run the application"
        raise AssertionError(message)

    monkeypatch.setattr(api, "_application", forbidden_application)
    with TestClient(api.app, base_url="http://localhost") as client:
        result = _request(client, "tools/list", {})
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert tools.keys() == READ_TOOLS | COMMAND_TOOLS
    assert TOKEN not in json.dumps(result)
    for name, tool in tools.items():
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert tool["outputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is (name in READ_TOOLS)
        if name in COMMAND_TOOLS:
            assert {"actor", "idempotency_key"} <= set(schema["required"])
    assert {"project_id", "expected_decision_fingerprint"} <= set(
        tools["structure_specification"]["inputSchema"]["required"],
    )
    assert {
        "project_id",
        "expected_decision_fingerprint",
        "expected_source_fingerprint",
    } <= set(tools["register_specification_source"]["inputSchema"]["required"])
    assert {"project_id", "path"} <= set(
        tools["attach_repository"]["inputSchema"]["required"],
    )
    assert {"project_id", "instance_key", "artifact_refs", "checklist_result"} <= set(
        tools["complete_task"]["inputSchema"]["required"],
    )


def test_discovery_and_reads_preserve_business_rows(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
) -> None:
    """Reading current state must not create receipts, attempts, or other facts."""
    engine, application, project_id, task_id = workflow_system
    before = durable_rows(engine)
    with TestClient(api.app, base_url="http://localhost") as client:
        _request(client, "tools/list", {})
        listed = _call(client, "project_list")
        shown = _call(client, "project_show", project_id=project_id)
        tasks = _call(
            client, "sprint_task_show", project_id=project_id, task_id=task_id
        )
        position = _call(client, "workflow_position", project_id=project_id)
    assert listed["structuredContent"] == application.reads.project_list()
    assert shown["structuredContent"] == application.reads.project_show(
        project_id=project_id,
    )
    assert tasks["structuredContent"] == application.reads.sprint_task_show(
        project_id=project_id,
        task_id=task_id,
    )
    assert position["structuredContent"] == application.position(
        project_id=project_id,
    ).model_dump(mode="json")
    assert all(item["isError"] is False for item in (listed, shown, tasks, position))
    assert durable_rows(engine) == before


def test_project_setup_replays_identically_through_cli_http_and_mcp(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All adapters must preserve one durable Project creation and replay result."""
    engine, application, _project_id, _task_id = workflow_system
    arguments = {
        "name": "MCP synthetic Project",
        "description": "Provider-free setup",
        "actor": "mcp-test",
        "idempotency_key": "setup-shared-304",
    }
    with TestClient(api.app, base_url="http://localhost") as client:
        first = _call(client, "create_project", **arguments)
        after = durable_rows(engine)
        http_replay = client.post("/api/projects", json=arguments)
        mcp_replay = _call(client, "create_project", **arguments)
    assert first["isError"] is False, first
    expected = application.create_project(CreateProjectCommand(**arguments)).model_dump(
        mode="json"
    )
    assert expected["replayed"] is True
    assert expected == {**first["structuredContent"], "replayed": True}
    assert http_replay.status_code == HTTPStatus.OK
    assert http_replay.json()["data"] == expected
    assert mcp_replay["structuredContent"] == expected
    assert (
        cli_main(
            [
                "project",
                "create",
                "--name",
                arguments["name"],
                "--description",
                arguments["description"],
                "--actor",
                arguments["actor"],
                "--idempotency-key",
                arguments["idempotency_key"],
            ],
            application=application,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == expected
    assert durable_rows(engine) == after


def test_work_submission_replay_and_conflicting_reuse_preserve_domain_results(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Task completion must keep exact evidence, reasons, and rejection atomicity."""
    engine, application, project_id, task_id = workflow_system
    arguments = {
        "project_id": project_id,
        "instance_key": f"task:{task_id}",
        "outcome_summary": "Focused tests pass",
        "artifact_refs": ["test-output.txt"],
        "acceptance_result": "fully_met",
        "checklist_result": {"Run focused tests": "met"},
        "actor": "mcp-test",
        "idempotency_key": "complete-shared-304",
    }
    with TestClient(api.app, base_url="http://localhost") as client:
        first = _call(client, "complete_task", **arguments)
        assert first["isError"] is False, first
        after = durable_rows(engine)
        replay = _call(client, "complete_task", **arguments)
        http_replay = client.post(
            f"/api/projects/{project_id}/sprint/task/complete",
            json={
                key: value for key, value in arguments.items() if key != "project_id"
            },
        )
        conflicting = {
            **arguments,
            "outcome_summary": "Changed input under the same key",
        }
        rejected = _call(client, "complete_task", **conflicting)
    expected = application.complete_task(
        CompleteTaskRequest.model_validate(arguments)
    ).model_dump(mode="json")
    assert expected == {**first["structuredContent"], "replayed": True}
    assert replay["structuredContent"] == expected
    assert http_replay.status_code == HTTPStatus.OK
    assert http_replay.json()["data"] == expected
    assert (
        cli_main(
            [
                "sprint",
                "task",
                "complete",
                "--project-id",
                str(project_id),
                "--instance-key",
                f"task:{task_id}",
                "--outcome-summary",
                "Focused tests pass",
                "--artifact-ref",
                "test-output.txt",
                "--acceptance-result",
                "fully_met",
                "--checklist-item",
                "Run focused tests=met",
                "--actor",
                "mcp-test",
                "--idempotency-key",
                "complete-shared-304",
            ],
            application=application,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == expected
    assert rejected["isError"] is True
    assert rejected["structuredContent"] == application.complete_task(
        CompleteTaskRequest.model_validate(conflicting),
    ).model_dump(mode="json")
    assert durable_rows(engine) == after


def test_unknown_tools_cannot_invoke_application_or_mutate_facts(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
) -> None:
    """Human decisions and arbitrary dispatch must remain unavailable."""
    engine, _application, project_id, _task_id = workflow_system
    before = durable_rows(engine)
    with TestClient(api.app, base_url="http://localhost") as client:
        for name in (
            "transition",
            "review_backlog",
            "execute_shell",
            "reset",
            "configure_models",
        ):
            result = _call(client, name, project_id=project_id)
            assert result["isError"] is True
            assert result["structuredContent"]["error"]["code"] == "UNKNOWN_TOOL"
    assert durable_rows(engine) == before


def test_fake_provider_generation_replays_and_waits_for_human_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MCP generation must produce only a pending draft with retained lineage."""
    engine = create_engine(f"sqlite:///{tmp_path / 'generation.db'}")
    SQLModel.metadata.create_all(engine)
    runner, domain, lineage, sends = _provider_graph_system(
        engine,
        statuses=[],
        clock=ProviderGraphClock(),
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        recipe_registry=runner._registry,
        read_projection=DurableReadProjectionService(engine=engine),
        delivery_action_input=DeliveryActionInputService(engine=engine),
        delivery_review_selection=DeliveryReviewSelectionService(engine=engine),
    )
    monkeypatch.setattr(api, "_application", lambda: application)
    monkeypatch.setenv("AGILEFORGE_MCP_BEARER_TOKEN", TOKEN)
    arguments = {
        "project_id": lineage.project_id,
        "actor": "mcp-test",
        "idempotency_key": "draft-304",
        "correlation_id": "provider-free-304",
    }
    monkeypatch.setenv(
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL", f"sqlite:///{tmp_path / 'trace.db'}"
    )
    before = durable_rows(engine)
    try:
        with TestClient(api.app, base_url="http://localhost") as client:
            _request(client, "tools/list", {})
            _call(client, "project_show", project_id=lineage.project_id)
            _call(client, "workflow_position", project_id=lineage.project_id)
            assert sends == []
            assert durable_rows(engine) == before
            first = _call(client, "generate_backlog", **arguments)
            assert first["isError"] is False, application.reads.artifact_history(
                project_id=lineage.project_id,
                node_id="backlog.generate",
            )
            after = durable_rows(engine)
            replay = _call(client, "generate_backlog", **arguments)
            review = _call(client, "backlog_review", project_id=lineage.project_id)
            next_work = _call(client, "workflow_next", project_id=lineage.project_id)
            http_replay = client.post(
                f"/api/projects/{lineage.project_id}/backlog/generate",
                json={
                    key: value
                    for key, value in arguments.items()
                    if key != "project_id"
                },
            )
        assert len(sends) == 1
        assert replay["structuredContent"] == {
            **first["structuredContent"],
            "replayed": True,
        }
        assert http_replay.status_code == HTTPStatus.OK
        assert http_replay.json()["data"] == replay["structuredContent"]
        assert review["structuredContent"] == application.backlog_review(
            lineage.project_id
        )
        position = application.position(project_id=lineage.project_id)
        assert "backlog.review" in position.waiting_nodes
        assert review["structuredContent"]["ok"] is True
        assert next_work["structuredContent"] == render_workflow_next(
            position, application=application
        )
        assert "decide_backlog" in json.dumps(next_work["structuredContent"])
        assert durable_rows(engine) == after
    finally:
        engine.dispose()


def test_known_runtime_secrets_are_redacted_without_changing_business_facts(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stored text must not turn a read into a runtime credential disclosure."""
    engine, application, _project_id, _task_id = workflow_system
    # Deliberate synthetic provider credential for disclosure checks.
    provider_secret = (
        "provider-response-secret-canary-304"  # noqa: S105  # nosec B105
    )
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", provider_secret)
    created = application.create_project(
        CreateProjectCommand(
            name=TOKEN,
            description=provider_secret,
            actor="test",
            idempotency_key="secret-read-304",
        )
    )
    assert created.ok
    before = durable_rows(engine)
    with TestClient(api.app, base_url="http://localhost") as client:
        result = _call(client, "project_list")
    wire = json.dumps(result)
    assert TOKEN not in wire + caplog.text
    assert provider_secret not in wire + caplog.text
    assert "[REDACTED]" in wire
    assert durable_rows(engine) == before


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("project_show", {"project_id": 0}),
        ("project_show", {"project_id": True}),
        ("project_list", {"shell": TOKEN}),
        (
            "generate_backlog",
            {"project_id": True, "actor": "test", "idempotency_key": "typed-project"},
        ),
        ("create_project", {"name": TOKEN, "idempotency_key": "missing-actor"}),
        (
            "structure_specification",
            {
                "project_id": 1,
                "actor": "test",
                "idempotency_key": "missing-freshness",
            },
        ),
    ],
)
def test_invalid_inputs_are_sanitized_and_atomic(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
    name: str,
    arguments: dict[str, object],
) -> None:
    """Typed validation must reject input before any application write."""
    engine, _application, _project_id, _task_id = workflow_system
    before = durable_rows(engine)
    with TestClient(api.app, base_url="http://localhost") as client:
        result = _call(client, name, **arguments)
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "INVALID_ARGUMENTS"
    assert TOKEN not in json.dumps(result)
    assert durable_rows(engine) == before


def test_stale_source_registration_matches_http_and_preserves_state(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale observed decision must retain the domain reason and atomicity."""
    engine, _application, _project_id, _task_id = workflow_system
    source = _registered_source_fixture(engine, tmp_path)
    monkeypatch.setattr(api, "_application", lambda: source.application)
    arguments = {
        "project_id": source.project_id,
        "source_path": "SPECIFICATION.md",
        "preparation_capability": "grill-with-docs",
        "expected_decision_fingerprint": "stale-observed-decision",
        "expected_source_fingerprint": "sha256:" + "0" * 64,
        "actor": "mcp-test",
        "idempotency_key": "stale-source-304",
    }
    before = durable_rows(engine)
    with TestClient(api.app, base_url="http://localhost") as client:
        result = _call(client, "register_specification_source", **arguments)
        response = client.post(
            f"/api/projects/{source.project_id}/specifications/source",
            headers={
                "X-AgileForge-Expected-Decision": arguments[
                    "expected_decision_fingerprint"
                ],
                "X-AgileForge-Expected-Source": arguments[
                    "expected_source_fingerprint"
                ],
            },
            json={
                key: value
                for key, value in arguments.items()
                if key
                not in {
                    "project_id",
                    "expected_decision_fingerprint",
                    "expected_source_fingerprint",
                }
            },
        )
    assert result["isError"] is True
    assert response.status_code == HTTPStatus.CONFLICT
    http_result = response.json()["detail"]
    http_result.pop("actions", None)
    assert result["structuredContent"] == http_result
    assert result["structuredContent"]["error"]["code"] == "STALE_POSITION"
    assert durable_rows(engine) == before


def test_long_generation_can_be_observed_without_competing_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent reads and duplicate delivery must preserve one attempt owner."""
    engine = create_engine(f"sqlite:///{tmp_path / 'long-generation.db'}")
    SQLModel.metadata.create_all(engine)
    started, release = Event(), Event()

    def before_send() -> None:
        started.set()
        assert release.wait(timeout=30), "Test did not release the fake provider"

    runner, domain, lineage, sends = _provider_graph_system(
        engine,
        statuses=[],
        clock=ProviderGraphClock(),
        before_send=before_send,
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        recipe_registry=runner._registry,
        read_projection=DurableReadProjectionService(engine=engine),
        delivery_action_input=DeliveryActionInputService(engine=engine),
    )
    monkeypatch.setattr(api, "_application", lambda: application)
    monkeypatch.setenv("AGILEFORGE_MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv(
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        f"sqlite:///{tmp_path / 'long-trace.db'}",
    )
    arguments = {
        "project_id": lineage.project_id,
        "actor": "mcp-test",
        "idempotency_key": "long-draft-304",
    }
    try:
        with (
            TestClient(api.app, base_url="http://localhost") as client,
            ThreadPoolExecutor() as pool,
        ):
            future = pool.submit(_call, client, "generate_backlog", **arguments)
            try:
                assert started.wait(timeout=15), "Fake provider did not start"
                history = _call(
                    client,
                    "artifact_history",
                    project_id=lineage.project_id,
                    node_id="backlog.generate",
                )
                items = history["structuredContent"]["data"]["items"]
                assert len(items) == 1
                assert items[0]["status"] == "in_progress"
                before_duplicate = durable_rows(engine)
                _call(client, "generate_backlog", **arguments)
                assert durable_rows(engine) == before_duplicate
            finally:
                release.set()
            first = future.result(timeout=20)
            replay = _call(client, "generate_backlog", **arguments)
        assert first["isError"] is False, first
        assert len(sends) == 1
        assert replay["structuredContent"] == {
            **first["structuredContent"],
            "replayed": True,
        }
        history_after = application.reads.artifact_history(
            project_id=lineage.project_id, node_id="backlog.generate"
        )
        history_data = history_after["data"]
        assert isinstance(history_data, dict)
        history_items = history_data["items"]
        assert isinstance(history_items, list)
        assert len(history_items) == 1
    finally:
        release.set()
        engine.dispose()


def test_reconnect_keeps_the_same_service_project_and_business_facts(
    workflow_system: tuple[Engine, AgileForgeApplication, int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh transport lifetimes must retain the selected application authority."""
    engine, application, project_id, _task_id = workflow_system
    trace_path = tmp_path / "reconnect-trace.db"
    monkeypatch.setenv("AGILEFORGE_DB_URL", f"sqlite:///{engine.url.database}")
    monkeypatch.setenv(
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL", f"sqlite:///{trace_path}"
    )
    before = durable_rows(engine)
    identities = []
    for _connection in range(2):
        with TestClient(api.app, base_url="http://localhost") as client:
            initialized = _request(
                client,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "reconnect-test", "version": "1"},
                },
            )
            assert initialized["serverInfo"]["name"] == "AgileForge"
            identities.append(_call(client, "service_identity")["structuredContent"])
            _request(client, "tools/list", {})
            project = _call(client, "project_show", project_id=project_id)
            missing = _call(client, "project_show", project_id=999999)
        assert project["structuredContent"] == application.reads.project_show(
            project_id=project_id
        )
        assert missing["isError"] is True
        assert missing["structuredContent"] == application.reads.project_show(
            project_id=999999
        )
        assert durable_rows(engine) == before
    assert identities[0] == identities[1]
    assert identities[0]["business_database"] == engine.url.database
    assert not trace_path.exists()
