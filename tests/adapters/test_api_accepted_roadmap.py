"""Accepted Roadmap HTTP reads need no review routing or mutation capability."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from sqlmodel import Session

import api as api_module
from models.core import Project, UserStory
from models.workflow import RoadmapArtifact, StoryArtifact
from services.read_projections import DurableReadProjectionService
from tests.services.test_durable_product_definition_projections import (
    _accepted_story_for_show,
)

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class _AcceptedRoadmapReadApplication:
    """Expose only durable reads; review and workflow mutation methods are absent."""

    reads: DurableReadProjectionService


def test_accepted_roadmap_http_requires_no_pending_review_or_action(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serve accepted content and return a distinct error for corrupt stored bytes."""
    story_id = _accepted_story_for_show(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        source = session.get(StoryArtifact, story.source_story_artifact_id)
        assert source is not None
        project_id, roadmap_id = story.project_id, source.roadmap_artifact_id
    application = _AcceptedRoadmapReadApplication(
        reads=DurableReadProjectionService(engine=engine)
    )
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)
    response = client.get(f"/api/projects/{project_id}/roadmap")
    assert response.status_code == HTTPStatus.OK
    data = response.json()["data"]
    assert data["state"] == "accepted"
    assert data["roadmap"]["roadmap_artifact_id"] == roadmap_id
    assert data["acceptance"]["state"] == "accepted"
    assert "binding" not in data

    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        assert roadmap is not None
        roadmap.canonical_content_json = '{"invalid":true}'
        session.add(roadmap)
        session.commit()
    invalid = client.get(f"/api/projects/{project_id}/roadmap")
    assert invalid.status_code == HTTPStatus.CONFLICT
    errors = invalid.json()["detail"]["errors"]
    assert errors[0]["code"] == "PLANNING_ARTIFACT_CONTENT_INVALID"


def test_accepted_roadmap_http_distinguishes_no_acceptance_from_missing_project(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is successful Roadmap data; an unknown Project remains a 404."""
    with Session(engine) as session:
        project = Project(name="Roadmap without accepted content")
        session.add(project)
        session.commit()
        session.refresh(project)
        project_id = project.project_id
    assert project_id is not None
    application = _AcceptedRoadmapReadApplication(
        reads=DurableReadProjectionService(engine=engine)
    )
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)
    response = client.get(f"/api/projects/{project_id}/roadmap")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"] == {"state": "absent", "project_id": project_id}
    missing = client.get(f"/api/projects/{project_id + 1}/roadmap")
    assert missing.status_code == HTTPStatus.NOT_FOUND
