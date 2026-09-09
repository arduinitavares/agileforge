# ruff: noqa: PLR2004
"""API transport coverage for the explicit Sprint retry lifecycle."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import api as api_module
import cli.main as cli_main
from models.workflow import WorkflowTransitionReceipt
from tests.adapters.sprint_retry_fixtures import (
    durable_rows,
    event_row_count,
    preserves_existing_rows,
    receipt_row_count,
    retry_row_count,
    retry_transport_fixture,
)

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine


def test_api_retry_preview_matches_cli_scope_and_does_not_write(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require exact API preview parity before a retry can write state."""
    fixture = retry_transport_fixture(engine)
    source = fixture.source
    monkeypatch.setattr(api_module, "_application", lambda: fixture.application)
    before = durable_rows(engine)
    client = TestClient(api_module.app)

    response = client.get(
        f"/api/projects/{source.project_id}/sprint/{source.source_sprint_id}/retry-preview"
    )

    assert response.status_code == 200
    api_preview = response.json()
    assert api_preview["status"] == "success"
    assert api_preview["data"]["sprint_id"] == source.source_sprint_id
    assert api_preview["data"]["story_ids"] == [
        source.first_story_id,
        source.second_story_id,
    ]
    assert api_preview["data"]["task_ids"] == [
        source.first_task_id,
        source.second_task_id,
    ]
    assert api_preview["data"]["blockers"] == []
    assert durable_rows(engine) == before

    exit_code = cli_main.main(
        [
            "sprint",
            "retry-preview",
            "--project-id",
            str(source.project_id),
            "--sprint-id",
            str(source.source_sprint_id),
        ],
        application=fixture.application,
    )

    assert exit_code == 0
    cli_preview = json.loads(capsys.readouterr().out)
    assert cli_preview["data"] == api_preview["data"]
    assert durable_rows(engine) == before


def test_api_retry_rejects_invalid_confirmation_and_stale_or_wrong_targets(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject coercion and target substitution without durable writes."""
    fixture = retry_transport_fixture(engine)
    source = fixture.source
    monkeypatch.setattr(api_module, "_application", lambda: fixture.application)
    client = TestClient(api_module.app)
    before = durable_rows(engine)
    preview = client.get(
        f"/api/projects/{source.project_id}/sprint/{source.source_sprint_id}/retry-preview"
    ).json()["data"]
    payload = {
        "sprint_id": source.source_sprint_id,
        "confirm": True,
        "expected_state_fingerprint": preview["expected_state_fingerprint"],
        "rationale": "Fresh evidence is required for this retry.",
        "actor": "owner@example.com",
        "idempotency_key": "api-retry-transport",
    }
    path = f"/api/projects/{source.project_id}/sprint/retry"

    missing_confirmation = {
        key: value for key, value in payload.items() if key != "confirm"
    }
    for invalid in (
        missing_confirmation,
        {**payload, "confirm": False},
        {**payload, "confirm": 1},
        {**payload, "confirm": "true"},
    ):
        response = client.post(path, json=invalid)
        assert response.status_code == 422
        assert durable_rows(engine) == before

    for field in ("actor", "rationale"):
        response = client.post(path, json={**payload, field: "  "})
        assert response.status_code == 422
        assert durable_rows(engine) == before

    wrong_sprint_path = (
        f"/api/projects/{source.project_id}/sprint/"
        f"{source.source_sprint_id + 999}/retry-preview"
    )
    wrong_project_path = (
        f"/api/projects/{source.project_id + 999}/sprint/"
        f"{source.source_sprint_id}/retry-preview"
    )
    wrong_sprint = client.get(wrong_sprint_path)
    wrong_project = client.get(wrong_project_path)
    assert wrong_sprint.status_code == 404
    assert wrong_project.status_code == 404
    assert durable_rows(engine) == before

    stale = client.post(
        path,
        json={
            **payload,
            "expected_state_fingerprint": "sha256:" + ("0" * 64),
            "idempotency_key": "api-retry-stale",
        },
    )
    assert stale.status_code == 409
    after_stale = durable_rows(engine)
    assert preserves_existing_rows(before, after_stale)
    assert retry_row_count(after_stale, "sprint_retry_attempts") == 0
    assert retry_row_count(after_stale, "sprint_retry_story_states") == 0
    assert retry_row_count(after_stale, "sprint_retry_task_states") == 0
    assert event_row_count(after_stale) == event_row_count(before)
    assert {
        table_name: rows
        for table_name, rows in after_stale.tables.items()
        if table_name != "workflow_transition_receipts"
    } == {
        table_name: rows
        for table_name, rows in before.tables.items()
        if table_name != "workflow_transition_receipts"
    }
    assert receipt_row_count(after_stale) == receipt_row_count(before) + 1
    with Session(engine) as session:
        stale_receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.request_kind == "retry_sprint",
                WorkflowTransitionReceipt.idempotency_key == "api-retry-stale",
            )
        ).one()
    assert stale_receipt.completed_at is not None
    assert stale_receipt.result_json is not None
    assert json.loads(stale_receipt.result_json)["ok"] is False

    applied = client.post(path, json=payload)
    assert applied.status_code == 200
    assert applied.json()["data"]["output"]["status"] == "Planned"
    after_apply = durable_rows(engine)
    assert preserves_existing_rows(after_stale, after_apply)
    assert retry_row_count(after_apply, "sprint_retry_attempts") == 1
    assert retry_row_count(after_apply, "sprint_retry_story_states") == 2
    assert retry_row_count(after_apply, "sprint_retry_task_states") == 2
    assert event_row_count(after_apply) == event_row_count(before) + 1

    stale_after_apply = client.post(
        path,
        json={**payload, "idempotency_key": "api-retry-stale-after-apply"},
    )
    assert stale_after_apply.status_code == 409
    assert durable_rows(engine) == after_apply


def test_api_start_binds_one_exact_planned_retry(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing start route must dispatch the exact retry action binding."""
    fixture = retry_transport_fixture(engine)
    source = fixture.source
    monkeypatch.setattr(api_module, "_application", lambda: fixture.application)
    client = TestClient(api_module.app)
    preview = client.get(
        f"/api/projects/{source.project_id}/sprint/{source.source_sprint_id}/retry-preview"
    ).json()["data"]
    created = client.post(
        f"/api/projects/{source.project_id}/sprint/retry",
        json={
            "sprint_id": source.source_sprint_id,
            "confirm": True,
            "expected_state_fingerprint": preview["expected_state_fingerprint"],
            "rationale": "Fresh evidence is required for this retry.",
            "actor": "owner@example.com",
            "idempotency_key": "api-retry-start",
        },
    )
    assert created.status_code == 200
    retry_attempt_id = created.json()["data"]["output"]["retry_attempt_id"]
    decision = next(
        item
        for item in fixture.application.position(project_id=source.project_id).decisions
        if item.node_id == "execution.sprint.retry.start"
        and item.instance_key
        == f"retry:{retry_attempt_id}:sprint:{source.source_sprint_id}"
    )

    before_start = durable_rows(engine)
    blank_actor = TestClient(api_module.app, raise_server_exceptions=False).post(
        f"/api/projects/{source.project_id}/sprint/start",
        headers={"X-AgileForge-Expected-Decision": decision.decision_fingerprint},
        json={
            "instance_key": decision.instance_key,
            "idempotency_key": "api-retry-start-blank-actor",
            "actor": "  ",
        },
    )
    assert blank_actor.status_code == 422
    assert durable_rows(engine) == before_start
    for rejected_instance_key, idempotency_key in (
        (f"retry:{retry_attempt_id + 99}:sprint:{source.source_sprint_id}", "unknown"),
        (f"retry:{retry_attempt_id}:sprint:{source.source_sprint_id + 99}", "wrong"),
        ("retry:0:sprint:0", "malformed"),
    ):
        rejected = client.post(
            f"/api/projects/{source.project_id}/sprint/start",
            headers={"X-AgileForge-Expected-Decision": decision.decision_fingerprint},
            json={
                "instance_key": rejected_instance_key,
                "idempotency_key": f"api-retry-start-{idempotency_key}",
                "actor": "owner@example.com",
            },
        )
        assert rejected.status_code == 409
        assert durable_rows(engine) == before_start

    start_payload = {
        "instance_key": decision.instance_key,
        "idempotency_key": "api-retry-start-binding",
        "actor": "owner@example.com",
    }
    started = client.post(
        f"/api/projects/{source.project_id}/sprint/start",
        headers={"X-AgileForge-Expected-Decision": decision.decision_fingerprint},
        json=start_payload,
    )

    assert started.status_code == 200
    assert started.json()["data"]["output"] == {
        "retry_attempt_id": retry_attempt_id,
        "status": "Active",
    }
    after_start = durable_rows(engine)

    replayed = client.post(
        f"/api/projects/{source.project_id}/sprint/start",
        headers={"X-AgileForge-Expected-Decision": decision.decision_fingerprint},
        json=start_payload,
    )
    assert replayed.status_code == 200, replayed.json()
    assert replayed.json()["data"]["replayed"] is True
    assert replayed.json()["data"]["output"] == started.json()["data"]["output"]
    assert durable_rows(engine) == after_start

    stale = client.post(
        f"/api/projects/{source.project_id}/sprint/start",
        headers={"X-AgileForge-Expected-Decision": decision.decision_fingerprint},
        json={**start_payload, "idempotency_key": "api-retry-start-stale"},
    )
    assert stale.status_code == 409
    assert durable_rows(engine) == after_start
