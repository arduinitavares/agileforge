"""Dashboard bundle keeps standalone read contracts on one durable snapshot."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http import HTTPStatus
from threading import Barrier
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import api as api_module
from models.core import Project
from repositories.workflow import WorkflowFactRepository
from services.application import AgileForgeApplication, DeliveryReviewSelectionService
from services.read_projections import DurableReadProjectionService
from services.vision_evidence_reader import RepositoryEvidenceCapability
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.graph import WorkflowGraph

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import WorkflowPosition
    from workflow.facts import WorkflowFactSnapshot


_ROUTES = {
    "project": "",
    "position": "/position",
    "vision": "/vision/status",
    "goal": "/goals/status",
    "specification": "/specifications/review",
    "repository": "/repository",
    "backlogReview": "/backlog/review",
    "roadmapReview": "/roadmap/review",
    "acceptedRoadmap": "/roadmap",
    "storyReviews": "/story/reviews",
    "sprintPlanReview": "/sprint/plan/review",
    "storyPending": "/story/pending",
    "storyDependencies": "/story/dependencies",
    "sprintCandidates": "/sprint/candidates",
    "sprintStatusResponse": "/sprint/status",
    "sprintHistory": "/sprint/history",
}


def _application(engine: Engine) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(datetime(2026, 9, 23, tzinfo=UTC)),
        ),
        read_projection=DurableReadProjectionService(engine=engine),
        delivery_review_selection=DeliveryReviewSelectionService(engine=engine),
    )


def test_dashboard_bundle_matches_standalone_reads_and_loads_facts_once(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every slot preserves its endpoint body while facts load once."""
    with Session(engine) as session:
        project = Project(name="Dashboard bundle")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id
    application = _application(engine)
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)
    path = f"/api/projects/{project_id}"
    expected = {slot: client.get(f"{path}{suffix}") for slot, suffix in _ROUTES.items()}

    loads = 0
    evaluations = 0
    original_load = WorkflowFactRepository.load
    original_evaluate = WorkflowGraph.evaluate

    def counted_load(self: WorkflowFactRepository, current_id: int) -> object:
        nonlocal loads
        loads += 1
        return original_load(self, current_id)

    def counted_evaluate(
        self: WorkflowGraph,
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> WorkflowPosition:
        nonlocal evaluations
        evaluations += 1
        return original_evaluate(self, snapshot, evaluated_at)

    monkeypatch.setattr(WorkflowFactRepository, "load", counted_load)
    monkeypatch.setattr(WorkflowGraph, "evaluate", counted_evaluate)
    response = client.get(f"{path}/dashboard")

    assert response.status_code == HTTPStatus.OK
    slots = response.json()["data"]
    assert tuple(slots) == tuple(_ROUTES)
    assert loads == 1
    assert evaluations == 1
    for slot, standalone in expected.items():
        assert slots[slot] == {
            "status": standalone.status_code,
            "body": standalone.json(),
        }
    expected_next_count = loads + 1
    application.position(project_id=project_id)
    assert loads == expected_next_count
    assert evaluations == expected_next_count


def test_dashboard_bundle_refreshes_between_requests(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later request sees committed changes after the first closes."""
    with Session(engine) as session:
        project = Project(name="Before")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id
    monkeypatch.setattr(api_module, "_application", lambda: _application(engine))
    client = TestClient(api_module.app)
    path = f"/api/projects/{project_id}/dashboard"

    before = client.get(path).json()["data"]["project"]["body"]["data"]["name"]
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.name = "After"
        session.add(project)
        session.commit()
    after = client.get(path).json()["data"]["project"]["body"]["data"]["name"]

    assert before == "Before"
    assert after == "After"


def test_dashboard_bundle_matches_active_sprint_reads(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active Sprint keeps its review and execution read contracts."""
    project_id, _sprint_id, _story_id, _task_id = seed_started_execution(engine)
    monkeypatch.setattr(api_module, "_application", lambda: _application(engine))
    client = TestClient(api_module.app)
    path = f"/api/projects/{project_id}"

    bundled = client.get(f"{path}/dashboard")

    assert bundled.status_code == HTTPStatus.OK
    for slot, suffix in _ROUTES.items():
        standalone = client.get(f"{path}{suffix}")
        assert bundled.json()["data"][slot] == {
            "status": standalone.status_code,
            "body": standalone.json(),
        }
    sprints = client.get(f"{path}/sprints")
    assert bundled.json()["data"]["sprintHistory"] == {
        "status": sprints.status_code,
        "body": sprints.json(),
    }


@pytest.mark.parametrize(
    ("slot", "method"),
    [
        ("acceptedRoadmap", "accepted_roadmap"),
        ("sprintStatusResponse", "sprint_status"),
    ],
)
def test_optional_projection_exception_remains_slot_local(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    slot: str,
    method: str,
) -> None:
    """Unexpected optional read errors leave other dashboard sections usable."""
    with Session(engine) as session:
        project = Project(name="Optional section")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id
    application = _application(engine)
    monkeypatch.setattr(api_module, "_application", lambda: application)

    def fail_optional(*_args: object, **_kwargs: object) -> None:
        msg = "private projection exception"
        raise RuntimeError(msg)

    monkeypatch.setattr(DurableReadProjectionService, method, fail_optional)

    response = TestClient(api_module.app).get(f"/api/projects/{project_id}/dashboard")

    assert response.status_code == HTTPStatus.OK
    slots = response.json()["data"]
    assert slots[slot] == {
        "status": HTTPStatus.INTERNAL_SERVER_ERROR,
        "body": {"detail": "Internal Server Error"},
    }
    assert slots["project"]["body"]["data"]["project_id"] == project_id
    assert slots["position"]["status"] == HTTPStatus.OK
    assert "private projection exception" not in response.text
    assert "Dashboard slot read failed" in caplog.text


def test_dashboard_bundle_holds_sqlite_read_snapshot_until_all_slots_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent WAL writer cannot mix old and new rows in one bundle."""
    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard.db'}")
    try:
        with engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            assert journal_mode.scalar() == "wal"
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(name="Before")
            session.add(project)
            session.commit()
            session.refresh(project)
            assert project.project_id is not None
            project_id = project.project_id
        application = _application(engine)
        original_position = application.position_from_snapshot
        wrote = False

        def write_after_snapshot(snapshot: WorkflowFactSnapshot) -> WorkflowPosition:
            nonlocal wrote
            if not wrote:
                with Session(engine) as session:
                    current = session.get(Project, project_id)
                    assert current is not None
                    current.name = "After"
                    session.add(current)
                    session.commit()
                wrote = True
            return original_position(snapshot)

        monkeypatch.setattr(application, "position_from_snapshot", write_after_snapshot)
        monkeypatch.setattr(api_module, "_application", lambda: application)
        client = TestClient(api_module.app)
        path = f"/api/projects/{project_id}/dashboard"

        first = client.get(path).json()["data"]
        second = client.get(path).json()["data"]

        assert wrote
        assert first["project"]["body"]["data"]["name"] == "Before"
        assert second["project"]["body"]["data"]["name"] == "After"
    finally:
        engine.dispose()


def test_simultaneous_dashboard_requests_keep_project_snapshots_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping requests keep their own position and read service."""
    engine = create_engine(f"sqlite:///{tmp_path / 'simultaneous.db'}")
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            projects = [Project(name="First"), Project(name="Second")]
            session.add_all(projects)
            session.commit()
            project_ids = []
            for project in projects:
                session.refresh(project)
                assert project.project_id is not None
                project_ids.append(project.project_id)
        application = _application(engine)
        original_position = application.position_from_snapshot
        overlap = Barrier(len(project_ids))

        def evaluate_during_overlap(
            snapshot: WorkflowFactSnapshot,
        ) -> WorkflowPosition:
            overlap.wait(timeout=5)
            return original_position(snapshot)

        monkeypatch.setattr(
            application, "position_from_snapshot", evaluate_during_overlap
        )
        monkeypatch.setattr(api_module, "_application", lambda: application)

        def read_project(project_id: int) -> dict[str, Any]:
            client = TestClient(api_module.app)
            response = client.get(f"/api/projects/{project_id}/dashboard")
            assert response.status_code == HTTPStatus.OK
            return response.json()["data"]

        with ThreadPoolExecutor(max_workers=len(project_ids)) as executor:
            bundled = list(executor.map(read_project, project_ids))

        for project_id, slots in zip(project_ids, bundled, strict=True):
            assert slots["project"]["body"]["data"]["project_id"] == project_id
            assert slots["position"]["body"]["data"]["project_id"] == project_id
    finally:
        engine.dispose()


def test_dashboard_capability_checks_run_after_read_session_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live capability probe can acquire the only pooled DB connection."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capability.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(name="Capability")
            session.add(project)
            session.commit()
            session.refresh(project)
            assert project.project_id is not None
            project_id = project.project_id
        application = _application(engine)
        checked: list[int] = []

        def capability(*, project_id: int) -> RepositoryEvidenceCapability:
            with Session(engine) as session:
                assert session.get(Project, project_id) is not None
            checked.append(project_id)
            return RepositoryEvidenceCapability(available=True)

        monkeypatch.setattr(application, "vision_bootstrap_capability", capability)
        monkeypatch.setattr(api_module, "_application", lambda: application)
        response = TestClient(api_module.app).get(
            f"/api/projects/{project_id}/dashboard"
        )

        assert response.status_code == HTTPStatus.OK
        assert checked == [project_id]
        actions = response.json()["data"]["position"]["body"]["actions"]
        assert any(
            action["request_kind"] == "generate_vision_bootstrap" for action in actions
        )
    finally:
        engine.dispose()
