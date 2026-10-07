"""HTTP adapter coverage for transactional Project deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import api
from models.core import Project
from models.enums import WorkflowEventType
from models.events import WorkflowEvent

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


def test_delete_project_removes_provider_audit_and_preserves_other_project(
    engine: Engine,
) -> None:
    """Provider events follow their owning Project's existing deletion boundary."""
    with Session(engine) as session:
        deleted = Project(name="Delete provider audit")
        retained = Project(name="Retain provider audit")
        session.add_all([deleted, retained])
        session.commit()
        assert deleted.project_id is not None
        assert retained.project_id is not None
        deleted_id = deleted.project_id
        retained_id = retained.project_id
        for project_id in (deleted_id, retained_id):
            session.add_all(
                [
                    WorkflowEvent(project_id=project_id, event_type=event_type)
                    for event_type in (
                        WorkflowEventType.PROVIDER_TRY_STARTED,
                        WorkflowEventType.PROVIDER_TRY_FINISHED,
                    )
                ]
            )
        session.commit()

    response = TestClient(api.app).delete(f"/api/projects/{deleted_id}")

    assert response.status_code == _HTTP_OK
    with Session(engine) as session:
        assert session.get(Project, deleted_id) is None
        assert session.get(Project, retained_id) is not None
        events = session.exec(select(WorkflowEvent)).all()
        assert [(event.project_id, event.event_type) for event in events] == [
            (retained_id, WorkflowEventType.PROVIDER_TRY_STARTED),
            (retained_id, WorkflowEventType.PROVIDER_TRY_FINISHED),
        ]
