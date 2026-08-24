"""HTTP adapter coverage for transactional Project deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from sqlmodel import Session

import api
from models.core import Project

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_HTTP_OK = 200


def test_delete_project_removes_the_project_through_the_api(engine: Engine) -> None:
    """Expose transactional Project deletion through the HTTP adapter."""
    with Session(engine) as session:
        project = Project(name="Delete through API")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id

    response = TestClient(api.app).delete(f"/api/projects/{project_id}")

    assert response.status_code == _HTTP_OK
    assert response.json()["status"] == "success"
    with Session(engine) as session:
        assert session.get(Project, project_id) is None
