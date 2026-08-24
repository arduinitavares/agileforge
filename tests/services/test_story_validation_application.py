# tests/services/test_story_validation_application.py
"""Tests for explicit provider-free structural story validation application boundary."""

import concurrent.futures
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, col, select

import api
from models.core import Project, UserStory
from models.workflow import WorkflowTransitionReceipt
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

    with (
        patch("api._application", return_value=app),
        patch.object(story_validation_service, "get_engine", return_value=engine),
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


def test_story_validation_concurrent_identical_requests(
    tmp_path: Path,
) -> None:
    """Concurrent identical requests handle claims safely and replay result."""
    db_file = tmp_path / "story-validation-race.db"
    race_engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(race_engine)
    try:
        story_id = _accepted_story(race_engine)
        with Session(race_engine) as session:
            statement = select(UserStory).where(col(UserStory.story_id) == story_id)
            story = session.exec(statement).first()
            assert story is not None
            project_id = story.project_id

        app = _build_application(race_engine)
        request = StoryValidationRequest(
            project_id=project_id,
            story_id=story_id,
            mode="structural",
            idempotency_key="concurrent-key-1",
            actor="test-operator",
        )
        with (
            patch.object(
                story_validation_service, "get_engine", return_value=race_engine
            ),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor,
        ):
            future_1 = executor.submit(app.validate_story, request)
            future_2 = executor.submit(app.validate_story, request)
            result_1 = future_1.result(timeout=5)
            result_2 = future_2.result(timeout=5)

        assert result_1["ok"] is True
        assert result_2["ok"] is True
        assert result_1 == result_2
    finally:
        race_engine.dispose()


def test_story_validation_forced_failure_allows_clean_retry(
    engine: Engine,
) -> None:
    """If validation fails, transaction rolls back cleanly and retry succeeds."""
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
        idempotency_key="retry-key-1",
        actor="test-operator",
    )

    # 1. Force an exception during in-session validation
    with (
        patch(
            "services.application.validate_story_with_specification_in_session",
            side_effect=RuntimeError("Simulated provider-free transient failure"),
        ),
        pytest.raises(RuntimeError, match="Simulated provider-free transient failure"),
    ):
        app.validate_story(request)

    # 2. Verify no incomplete receipt row was left in the database
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind) == "validate_story",
                col(WorkflowTransitionReceipt.idempotency_key) == "retry-key-1",
            )
        ).one_or_none()
        assert receipt is None

    # 3. Retry the identical request - it succeeds cleanly without IDEMPOTENCY_CONFLICT
    retry_result = app.validate_story(request)
    assert retry_result["ok"] is True
    data = cast("dict[str, Any]", retry_result.get("data"))
    assert data["ready_for_sprint"] is True


def test_story_validation_lock_failure_fails_before_validation(
    engine: Engine,
) -> None:
    """Lock failure on BEGIN IMMEDIATE fails before executing validation."""
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
        idempotency_key="lock-failure-key-1",
        actor="test-operator",
    )

    validation_mock = MagicMock()
    with (
        patch(
            "sqlalchemy.engine.base.Connection.exec_driver_sql",
            side_effect=OperationalError("database is locked", {}, Exception("locked")),
        ),
        patch(
            "services.application.validate_story_with_specification_in_session",
            validation_mock,
        ),
        pytest.raises(OperationalError, match="database is locked"),
    ):
        app.validate_story(request)

    assert validation_mock.call_count == 0
