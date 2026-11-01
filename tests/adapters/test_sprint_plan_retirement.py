# tests/adapters/test_sprint_plan_retirement.py
"""Transport agreement for explicit stale Sprint Plan retirement."""

from __future__ import annotations

import json
from contextlib import nullcontext
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import api
import cli.main as cli_main
import services.application as application_module
from cli.workflow_commands import workflow_next
from models.workflow import SprintPlanRetirement
from services.application import AgileForgeApplication, PlanningActionSelectionService
from services.read_projections import DurableReadProjectionService
from tests.workflow.test_sprint_plan_retirement import _retire_request
from tests.workflow.test_superseded_story_dependencies import (
    _accepted_dependency_sprint,
    _domain,
    _project_business_rows,
    _replace,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def _application(engine: Engine) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=_domain(engine),
        planning_action_selection=PlanningActionSelectionService(engine=engine),
        read_projection=DurableReadProjectionService(engine=engine),
    )


@pytest.mark.parametrize("transport", ["application", "api", "cli"])
def test_retirement_transport_and_exact_replay(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: str,
) -> None:
    """All transports perform one guarded retirement and replay its outcome."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    raw = _retire_request(engine, fixture)
    payload = {
        "sprint_plan_artifact_id": raw.sprint_plan_artifact_id,
        "sprint_plan_artifact_decision_id": raw.sprint_plan_artifact_decision_id,
        "sprint_id": raw.sprint_id,
        "plan_fingerprint": raw.plan_fingerprint,
        "expected_decision_fingerprint": raw.decision_fingerprint,
        "rationale": raw.rationale,
        "actor": raw.actor,
        "idempotency_key": raw.idempotency_key,
    }
    application = _application(engine)
    if transport == "application":
        request = application_module.SprintPlanRetirementRequest.model_validate(
            {"project_id": raw.project_id, **payload}
        )
        assert application.retire_sprint_plan(request).ok
        assert application.retire_sprint_plan(request).replayed
    elif transport == "api":
        monkeypatch.setattr(api, "_application", lambda: application)
        client = TestClient(api.app)
        url = f"/api/projects/{raw.project_id}/sprint/retire"
        response = client.post(url, json=payload)
        assert response.status_code == HTTPStatus.OK, response.text
        assert response.json()["data"]["ok"]
        assert client.post(url, json=payload).json()["data"]["replayed"]
    else:
        monkeypatch.setattr(cli_main, "runtime_access", lambda **_kwargs: nullcontext())
        args = ["sprint", "plan", "retire", "--project-id", str(raw.project_id)]
        for key, value in payload.items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        assert cli_main.main(args, application=application) == 0
        capsys.readouterr()
        assert cli_main.main(args, application=application) == 0
        assert json.loads(capsys.readouterr().out)["replayed"]
    with Session(engine) as session:
        assert len(session.exec(select(SprintPlanRetirement)).all()) == 1


def test_position_and_next_advertise_explicit_recovery(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical stale-plan recovery has an actionable API route and CLI command."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    application = _application(engine)
    monkeypatch.setattr(api, "_application", lambda: application)
    position = (
        TestClient(api.app)
        .get(f"/api/projects/{fixture.scenario.project_id}/position")
        .json()
    )
    action = next(
        item
        for item in position["actions"]
        if item["request_kind"] == "retire_sprint_plan"
    )
    assert action["endpoint"] == "sprint/retire"
    payload = workflow_next(
        application=application, project_id=fixture.scenario.project_id
    )
    assert "sprint plan retire" in json.dumps(payload)
    assert "STALE_SPRINT_PLAN_RETIREMENT_AVAILABLE" in json.dumps(payload)
    command = next(
        item["command"]
        for item in payload["commands"]
        if item["request_kind"] == "retire_sprint_plan"
    )
    assert _retire_request(engine, fixture).decision_fingerprint in command
    assert "<sprint-plan-artifact-id>" not in command


def test_retired_sprint_reads_preserve_explicit_historical_identity(
    engine: Engine,
) -> None:
    """A retired planned Sprint remains readable with its retirement evidence."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    assert _domain(engine).transition(_retire_request(engine, fixture)).ok
    projections = DurableReadProjectionService(engine=engine)
    status = projections.sprint_status(
        project_id=fixture.scenario.project_id, sprint_id=fixture.sprint_id
    )
    assert status["ok"], status
    data = status["data"]
    assert isinstance(data, dict)
    assert data["effective_status"] == "retired"
    retirement = data["retirement"]
    assert isinstance(retirement, dict)
    assert retirement["sprint_plan_artifact_id"] == fixture.plan_id
    history = projections.sprint_history(project_id=fixture.scenario.project_id)
    history_data = history["data"]
    assert isinstance(history_data, dict)
    retirements = history_data["retirements"]
    assert isinstance(retirements, list)
    first = retirements[0]
    assert isinstance(first, dict)
    assert first["sprint_id"] == fixture.sprint_id


@pytest.mark.parametrize(
    "change",
    [
        {"actor": " "},
        {"rationale": " "},
        {"idempotency_key": " "},
        {"expected_decision_fingerprint": "stale"},
        {"sprint_id": 99999},
    ],
)
def test_invalid_retirement_api_input_leaves_domain_rows_unchanged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    """Missing intent and stale or mismatched identity cannot release locks."""
    fixture = _accepted_dependency_sprint(engine, started=False)
    _replace(engine, fixture.scenario.artifact_ids[0])
    raw = _retire_request(engine, fixture)
    application = _application(engine)
    monkeypatch.setattr(api, "_application", lambda: application)
    before = _project_business_rows(engine, fixture.scenario.project_id)
    payload = raw.model_dump(
        exclude={
            "project_id",
            "kind",
            "graph_version",
            "fact_fingerprint",
            "decision_fingerprint",
            "instance_key",
            "attempt_id",
            "attempt_fingerprint",
        }
    )
    payload["expected_decision_fingerprint"] = raw.decision_fingerprint
    payload.update(change)
    response = TestClient(api.app).post(
        f"/api/projects/{raw.project_id}/sprint/retire", json=payload
    )
    assert response.status_code in {
        HTTPStatus.CONFLICT,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    }
    assert _project_business_rows(engine, fixture.scenario.project_id) == before
