# tests/services/test_story_validation_application.py
"""Tests for explicit provider-free Story structural-eligibility reconciliation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

import api
from models.core import UserStory
from models.workflow import WorkflowTransitionReceipt
from services.application import AgileForgeApplication, StoryEligibilityReconcileRequest
from services.read_projections import DurableReadProjectionService
from tests.test_story_validation_service import _accepted_story, _validate
from utils.spec_schemas import ValidationEvidence
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from sqlalchemy import Engine

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _build_application(engine: Engine) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=NOW),
        ),
        read_projection=DurableReadProjectionService(engine=engine),
    )


def _request(
    project_id: int,
    *,
    story_ids: tuple[int, ...] | None = None,
    key: str = "reconcile-key-1",
    actor: str = "test-operator",
    correlation_id: str | None = None,
) -> StoryEligibilityReconcileRequest:
    return StoryEligibilityReconcileRequest(
        project_id=project_id,
        story_ids=story_ids,
        idempotency_key=key,
        actor=actor,
        correlation_id=correlation_id,
    )


def _story(engine: Engine, story_id: int) -> UserStory:
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        session.expunge(story)
        return story


def _clear_evidence(engine: Engine, story_id: int) -> None:
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.validation_evidence = None
        session.add(story)
        session.commit()


def _data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    assert isinstance(data, dict)
    return data


def test_reconcile_all_active_stories_replaces_missing_evidence(engine: Engine) -> None:
    """Missing evidence is persisted for every active accepted Story."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    _clear_evidence(engine, story_id)

    result = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id)
    )

    assert result["ok"] is True
    data = cast("dict[str, Any]", result["data"])
    assert data["story_ids"] == [story_id]
    assert data["reconciled_story_ids"] == [story_id]
    assert data["unchanged_story_ids"] == []
    item = data["stories"][0]
    assert item["story_id"] == story_id
    assert item["structurally_eligible"] is True
    assert item["structural_failures"] == []
    assert item["validated_at"]
    assert item["evidence_fingerprint"].startswith("sha256:")
    assert "Sprint selection." in data["does_not_prove"]
    assert "Dependency safety." in data["does_not_prove"]


def test_reconcile_explicit_subset_is_canonical_and_rejects_duplicate_ids(
    engine: Engine,
) -> None:
    """Explicit subsets use sorted unique Story IDs."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)

    result = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id, story_ids=(story_id,), key="subset-key")
    )

    assert result["ok"] is True
    assert _data(result)["story_ids"] == [story_id]
    with pytest.raises(ValueError, match="duplicate Story IDs"):
        _request(story.project_id, story_ids=(story_id, story_id))


def test_reconcile_replaces_legacy_v2_and_preserves_current_failed_v3_bytes(
    engine: Engine,
) -> None:
    """Legacy evidence is replaced while current failed evidence is retained."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    _validate(engine, story_id)
    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        assert row.validation_evidence is not None
        legacy = json.loads(row.validation_evidence)
        legacy["schema_version"] = "agileforge.story-validation-evidence.v2"
        row.validation_evidence = json.dumps(
            legacy,
            sort_keys=True,
            separators=(",", ":"),
        )
        row.story_description = "invalid statement"
        session.add(row)
        session.commit()

    first = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id, key="legacy-key")
    )
    assert first["ok"] is True
    first_data = _data(first)
    assert first_data["reconciled_story_ids"] == [story_id]
    assert first_data["stories"][0]["structurally_eligible"] is False
    raw_after_replacement = _story(engine, story_id).validation_evidence
    assert raw_after_replacement is not None
    original_validated_at = ValidationEvidence.model_validate_json(
        raw_after_replacement, strict=True
    ).validated_at

    second = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id, key="current-failed-key")
    )
    assert second["ok"] is True
    assert _data(second)["unchanged_story_ids"] == [story_id]
    assert _story(engine, story_id).validation_evidence == raw_after_replacement
    assert ValidationEvidence.model_validate_json(
        raw_after_replacement, strict=True
    ).validated_at == original_validated_at


def test_reconcile_replays_exact_result_and_rejects_changed_metadata(
    engine: Engine,
) -> None:
    """A matching key replays while changed metadata conflicts."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    app = _build_application(engine)
    request = _request(story.project_id, key="replay-key", correlation_id="trace-1")

    first = app.reconcile_story_eligibility(request)
    replay = app.reconcile_story_eligibility(request)
    conflict = app.reconcile_story_eligibility(
        _request(story.project_id, key="replay-key", correlation_id="trace-2")
    )

    assert replay == first
    assert conflict["ok"] is False
    errors = cast("list[dict[str, Any]]", conflict["errors"])
    assert errors[0]["code"] == "IDEMPOTENCY_CONFLICT"


def test_reconcile_rolls_back_evidence_and_receipt_on_unexpected_failure(
    engine: Engine,
) -> None:
    """Unexpected evaluator failures leave no evidence or receipt behind."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    _clear_evidence(engine, story_id)
    request = _request(story.project_id, key="rollback-key")

    with (
        patch(
            "services.application.validate_story_with_specification_in_session",
            side_effect=RuntimeError("simulated evaluator failure"),
        ),
        pytest.raises(RuntimeError, match="simulated evaluator failure"),
    ):
        _build_application(engine).reconcile_story_eligibility(request)

    assert _story(engine, story_id).validation_evidence is None
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind)
                == "reconcile_story_structural_eligibility",
                col(WorkflowTransitionReceipt.idempotency_key) == "rollback-key",
            )
        ).one_or_none()
    assert receipt is None


def test_reconcile_api_and_cli_expose_only_the_renamed_operator_surface(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the reconciliation API route and CLI command are exposed."""
    from cli.main import main  # noqa: PLC0415

    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    app = _build_application(engine)
    client = TestClient(api.app)

    with patch("api._application", return_value=app):
        response = client.post(
            f"/api/projects/{story.project_id}/story/structural-eligibility/reconcile",
            json={"idempotency_key": "api-key", "actor": "api-operator"},
        )
        removed = client.post(
            f"/api/projects/{story.project_id}/story/validate",
            json={"idempotency_key": "legacy-key", "actor": "api-operator"},
        )
        duplicate_ids = client.post(
            f"/api/projects/{story.project_id}/story/structural-eligibility/reconcile",
            json={
                "story_ids": [story_id, story_id],
                "idempotency_key": "duplicate-key",
                "actor": "api-operator",
            },
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"]["story_ids"] == [story_id]
    assert removed.status_code == HTTPStatus.NOT_FOUND
    assert duplicate_ids.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    assert main(
        [
            "story", "eligibility", "reconcile", "--project-id",
            str(story.project_id), "--story-id", str(story_id),
            "--idempotency-key", "cli-key", "--actor", "cli-operator",
        ],
        application=app,
    ) == 0
    assert json.loads(capsys.readouterr().out)["data"]["story_ids"] == [story_id]
    assert main(["story", "validate", "--project-id", str(story.project_id)]) == 2  # noqa: PLR2004
