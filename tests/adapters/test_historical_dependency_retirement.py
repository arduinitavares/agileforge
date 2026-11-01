# tests/adapters/test_historical_dependency_retirement.py
"""Supported transports carry the exact historical-edge retirement action."""

from __future__ import annotations

import json
from contextlib import nullcontext
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

import api
import cli.main as cli_main
import services.application as application_module
from cli.workflow_commands import workflow_next
from tests.adapters.test_sprint_plan_retirement import _application
from tests.workflow.test_historical_dependency_retirement import (
    _historical_cycle,
    _retire_request,
)
from tests.workflow.test_superseded_story_dependencies import (
    _project_business_rows,
    _snapshot,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@pytest.mark.parametrize("transport", ["application", "api", "cli"])
def test_historical_retirement_transport_replay(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: str,
) -> None:
    """The exact advertised repair works and replays once across supported adapters."""
    scenario, dependency_id = _historical_cycle(engine)
    raw = _retire_request(engine, scenario, dependency_id)
    payload = {
        "dependency_id": raw.dependency_id,
        "dependency_fingerprint": raw.dependency_fingerprint,
        "expected_decision_fingerprint": raw.decision_fingerprint,
        "rationale": raw.rationale,
        "actor": raw.actor,
        "idempotency_key": raw.idempotency_key,
    }
    application = _application(engine)
    if transport == "application":
        request_type = application_module.HistoricalStoryDependencyRetirementRequest
        request = request_type.model_validate(
            {"project_id": scenario.project_id, **payload}
        )
        assert application.retire_historical_story_dependency(request).ok
        assert application.retire_historical_story_dependency(request).replayed
    elif transport == "api":
        monkeypatch.setattr(api, "_application", lambda: application)
        client = TestClient(api.app)
        url = (
            f"/api/projects/{scenario.project_id}/story/dependencies/retire-historical"
        )
        response = client.post(url, json=payload)
        assert response.status_code == HTTPStatus.OK, response.text
        assert response.json()["data"]["ok"]
        assert client.post(url, json=payload).json()["data"]["replayed"]
    else:
        monkeypatch.setattr(cli_main, "runtime_access", lambda **_kwargs: nullcontext())
        args = [
            "story",
            "dependencies",
            "retire-historical",
            "--project-id",
            str(scenario.project_id),
        ]
        for key, value in payload.items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        assert cli_main.main(args, application=application) == 0
        capsys.readouterr()
        assert cli_main.main(args, application=application) == 0
        assert json.loads(capsys.readouterr().out)["replayed"]
    assert len(_snapshot(engine, scenario).story_dependency_retirements) == 1


def test_next_and_inspection_expose_exact_edge_identity(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API and CLI actions name one edge without an unresolved identity placeholder."""
    scenario, dependency_id = _historical_cycle(engine)
    application = _application(engine)
    monkeypatch.setattr(api, "_application", lambda: application)
    position = (
        TestClient(api.app).get(f"/api/projects/{scenario.project_id}/position").json()
    )
    action = next(
        item
        for item in position["actions"]
        if item["request_kind"] == "retire_historical_story_dependency"
    )
    assert action["endpoint"] == "story/dependencies/retire-historical"
    command = next(
        item["command"]
        for item in workflow_next(
            application=application, project_id=scenario.project_id
        )["commands"]
        if item["instance_key"] == f"story_dependency:{dependency_id}"
    )
    assert "story dependencies retire-historical" in command
    assert "<dependency-id>" not in command
    assert (
        _retire_request(engine, scenario, dependency_id).dependency_fingerprint
        in command
    )
    inspected = application.reads.story_dependencies_inspect(
        project_id=scenario.project_id
    )
    assert inspected["ok"]
    data = inspected["data"]
    assert isinstance(data, dict)
    assert "retirements" in data


@pytest.mark.parametrize("field", ["actor", "rationale", "idempotency_key"])
def test_api_retirement_requires_explicit_intent(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """HTTP callers cannot obtain an implicit actor, rationale or replay key."""
    scenario, dependency_id = _historical_cycle(engine)
    raw = _retire_request(engine, scenario, dependency_id)
    payload = {
        "dependency_id": dependency_id,
        "dependency_fingerprint": raw.dependency_fingerprint,
        "expected_decision_fingerprint": raw.decision_fingerprint,
        "actor": raw.actor,
        "rationale": raw.rationale,
        "idempotency_key": raw.idempotency_key,
    }
    payload.pop(field)
    monkeypatch.setattr(api, "_application", lambda: _application(engine))
    before = _project_business_rows(engine, scenario.project_id)
    response = TestClient(api.app).post(
        f"/api/projects/{scenario.project_id}/story/dependencies/retire-historical",
        json=payload,
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert _project_business_rows(engine, scenario.project_id) == before
