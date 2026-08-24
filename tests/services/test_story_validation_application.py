# tests/services/test_story_validation_application.py
"""Tests for explicit provider-free structural story validation application boundary."""

from datetime import UTC, datetime
from http import HTTPStatus
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, col, select

import api
from models.core import Project, UserStory
from services.application import (
    AgileForgeApplication,
    StoryValidationRequest,
)
from services.read_projections import DurableReadProjectionService
from services.specs import story_validation_service
from tests.test_story_validation_service import _accepted_story
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _build_application(engine: Engine) -> AgileForgeApplication:
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=NOW),
    )
    return AgileForgeApplication(
        workflow_domain=domain,
        read_projection=DurableReadProjectionService(engine=engine),
    )


def test_story_validation_application_facade_validates_story_structurally(
    engine: Engine,
) -> None:
    """Validate accepted Story structurally provider-free and set ready_for_sprint."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        statement = select(UserStory).where(col(UserStory.story_id) == story_id)
        story_before = session.exec(statement).first()
        assert story_before is not None
        assert story_before.validation_evidence is None
        project_id = story_before.project_id

    app = _build_application(engine)
    request = StoryValidationRequest(
        project_id=project_id,
        story_id=story_id,
        mode="structural",
        idempotency_key="test-validate-key",
        actor="test-operator",
    )
    with patch.object(story_validation_service, "get_engine", return_value=engine):
        result = app.validate_story(request)

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert data["success"] is True
    assert data["ready_for_sprint"] is True
    assert data["story_id"] == story_id
    assert data["mode"] == "structural"
    assert data["structural_failures"] == []

    # Verify persisted UserStory row now contains validation evidence
    with Session(engine) as session:
        statement = select(UserStory).where(col(UserStory.story_id) == story_id)
        story_after = session.exec(statement).first()
        assert story_after is not None
        assert story_after.validation_evidence is not None
        assert '"ready_for_sprint":true' in story_after.validation_evidence


def test_story_validation_application_facade_rejects_mismatched_project(
    engine: Engine,
) -> None:
    """Story validation fails closed when the story belongs to a different project."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        statement = select(UserStory).where(col(UserStory.story_id) == story_id)
        story = session.exec(statement).first()
        assert story is not None
        other_project = Project(name="other-project")
        session.add(other_project)
        session.commit()
        session.refresh(other_project)
        other_project_id = cast("int", other_project.project_id)

    app = _build_application(engine)
    request = StoryValidationRequest(
        project_id=other_project_id,
        story_id=story_id,
        mode="structural",
        idempotency_key="test-validate-key-2",
        actor="test-operator",
    )
    with patch.object(story_validation_service, "get_engine", return_value=engine):
        result = app.validate_story(request)

    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    assert len(errors) > 0
    first_error = errors[0]
    assert isinstance(first_error, dict)
    assert first_error["code"] == "STORY_NOT_FOUND"


def test_story_validation_api_endpoint(
    engine: Engine,
) -> None:
    """POST /api/projects/{id}/story/validate validates story successfully."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        statement = select(UserStory).where(col(UserStory.story_id) == story_id)
        story = session.exec(statement).first()
        assert story is not None
        project_id = story.project_id

    app = _build_application(engine)
    client = TestClient(api.app)

    with patch("api._application", return_value=app), patch.object(
        story_validation_service, "get_engine", return_value=engine
    ):
        response = client.post(
            f"/api/projects/{project_id}/story/validate",
            json={
                "story_id": story_id,
                "mode": "structural",
                "idempotency_key": "api-validate-1",
                "actor": "api-operator",
            },
        )

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["success"] is True
    assert body["data"]["ready_for_sprint"] is True
    assert body["data"]["story_id"] == story_id


def test_story_validation_idempotent_replay(
    engine: Engine,
) -> None:
    """Same idempotency key replays exact result without re-executing."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        statement = select(UserStory).where(col(UserStory.story_id) == story_id)
        story = session.exec(statement).first()
        assert story is not None
        project_id = story.project_id

    app = _build_application(engine)
    request = StoryValidationRequest(
        project_id=project_id,
        story_id=story_id,
        mode="structural",
        idempotency_key="same-key-replay-1",
        actor="test-operator",
    )
    with patch.object(story_validation_service, "get_engine", return_value=engine):
        result_1 = app.validate_story(request)
        result_2 = app.validate_story(request)

    assert result_1["ok"] is True
    assert result_2["ok"] is True
    assert result_1 == result_2


def test_story_validation_idempotency_conflict_on_different_payload(
    engine: Engine,
) -> None:
    """Same idempotency key with different request fails with conflict error."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        statement = select(UserStory).where(col(UserStory.story_id) == story_id)
        story = session.exec(statement).first()
        assert story is not None
        project_id = story.project_id

    app = _build_application(engine)
    request_1 = StoryValidationRequest(
        project_id=project_id,
        story_id=story_id,
        mode="structural",
        idempotency_key="conflict-key-1",
        actor="test-operator-1",
    )
    request_2 = StoryValidationRequest(
        project_id=project_id,
        story_id=story_id,
        mode="structural",
        idempotency_key="conflict-key-1",
        actor="test-operator-2",  # different actor
    )
    with patch.object(story_validation_service, "get_engine", return_value=engine):
        result_1 = app.validate_story(request_1)
        result_2 = app.validate_story(request_2)

    assert result_1["ok"] is True
    assert result_2["ok"] is False
    errors = result_2["errors"]
    assert isinstance(errors, list)
    assert len(errors) > 0
    first_error = errors[0]
    assert isinstance(first_error, dict)
    assert first_error["code"] == "IDEMPOTENCY_CONFLICT"
