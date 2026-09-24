"""Dashboard bundle keeps standalone read contracts on one durable snapshot."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from threading import Barrier, Event, Timer
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import api as api_module
from models.core import Project
from repositories.workflow import WorkflowFactRepository
from services import dashboard_reads
from services.application import (
    AgileForgeApplication,
    DeliveryReviewSelectionService,
)
from services.dashboard_reads import dashboard_read_view
from services.read_projections import DurableReadProjectionService
from services.vision_evidence_reader import RepositoryEvidenceCapability
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, RecommendationKind
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.application import WorkflowDomainPort
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


def test_dashboard_evaluates_lease_at_request_entry_before_fact_load(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease crossing expiry during fact loading stays active for that request."""
    entered_at = datetime(2026, 9, 23, 12, tzinfo=UTC)
    lease_expires_at = entered_at + timedelta(seconds=1)
    with Session(engine) as session:
        project = Project(name="Lease boundary", created_at=entered_at)
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id

    class MutableClock:
        def __init__(self, now_value: datetime) -> None:
            self.now_value = now_value

        def now(self) -> datetime:
            return self.now_value

    def lease_rule(
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        expiry = snapshot.project.created_at.replace(tzinfo=UTC) + timedelta(seconds=1)
        return (
            RuleEvaluation(
                category=(
                    RuleCategory.WAITING
                    if evaluated_at < expiry
                    else RuleCategory.AVAILABLE
                ),
                reason_code=(
                    "LEASE_ACTIVE" if evaluated_at < expiry else "LEASE_EXPIRED"
                ),
                valid_until=expiry if evaluated_at < expiry else None,
            ),
        )

    graph = WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="test",
            nodes=(
                NodeSpec(
                    node_id="test.lease",
                    child_graph_id="test",
                    request_kind="test_lease",
                    recommendation_kind=RecommendationKind.RECOVERY,
                    required_inputs=(),
                    evaluate_rule=lease_rule,
                ),
            ),
        ),
    )
    clock = MutableClock(entered_at)
    application = AgileForgeApplication(
        workflow_domain=WorkflowDomain(engine=engine, graph=graph, clock=clock),
        read_projection=DurableReadProjectionService(engine=engine),
        delivery_review_selection=DeliveryReviewSelectionService(engine=engine),
    )
    original_load = WorkflowFactRepository.load
    loads = 0

    def advance_after_load(
        repository: WorkflowFactRepository,
        current_id: int,
    ) -> WorkflowFactSnapshot:
        nonlocal loads
        snapshot = original_load(repository, current_id)
        loads += 1
        if loads == 1:
            clock.now_value = lease_expires_at
        return snapshot

    monkeypatch.setattr(WorkflowFactRepository, "load", advance_after_load)
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)
    path = f"/api/projects/{project_id}/dashboard"

    first_response = client.get(path)
    second_response = client.get(path)

    assert first_response.status_code == HTTPStatus.OK
    assert second_response.status_code == HTTPStatus.OK
    assert loads == len((first_response, second_response))
    first = first_response.json()["data"]["position"]["body"]["data"]
    second = second_response.json()["data"]["position"]["body"]["data"]
    assert datetime.fromisoformat(first["evaluated_at"]) == entered_at
    assert first["waiting_nodes"] == ["test.lease"]
    assert first["decisions"][0]["reason_code"] == "LEASE_ACTIVE"
    assert datetime.fromisoformat(first["decisions"][0]["valid_until"]) == (
        lease_expires_at
    )
    assert datetime.fromisoformat(second["evaluated_at"]) == lease_expires_at
    assert second["available_nodes"] == ["test.lease"]
    assert second["decisions"][0]["reason_code"] == "LEASE_EXPIRED"


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


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_dashboard_bundle_holds_sqlite_read_snapshot_until_all_slots_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_mode: str,
) -> None:
    """A concurrent writer commits while each bundle keeps one SQLite view."""
    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard.db'}")
    try:
        with engine.connect() as connection:
            selected_mode = connection.exec_driver_sql(
                f"PRAGMA journal_mode={journal_mode}"
            )
            assert selected_mode.scalar() == journal_mode.lower()
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

        def write_after_snapshot(
            snapshot: WorkflowFactSnapshot,
            *,
            evaluated_at: datetime,
        ) -> WorkflowPosition:
            nonlocal wrote
            if not wrote:
                with Session(engine) as session:
                    current = session.get(Project, project_id)
                    assert current is not None
                    current.name = "After"
                    session.add(current)
                    session.commit()
                wrote = True
            return original_position(snapshot, evaluated_at=evaluated_at)

        monkeypatch.setattr(application, "position_from_snapshot", write_after_snapshot)
        monkeypatch.setattr(api_module, "_application", lambda: application)
        client = TestClient(api_module.app)
        path = f"/api/projects/{project_id}/dashboard"

        first_response = client.get(path)
        second_response = client.get(path)
        assert first_response.status_code == HTTPStatus.OK
        assert second_response.status_code == HTTPStatus.OK
        first = first_response.json()["data"]
        second = second_response.json()["data"]

        assert wrote
        assert tuple(first) == tuple(_ROUTES)
        assert tuple(second) == tuple(_ROUTES)
        assert all(first[slot]["status"] == second[slot]["status"] for slot in _ROUTES)
        assert all(
            first[slot]["status"] != HTTPStatus.INTERNAL_SERVER_ERROR
            for slot in _ROUTES
        )
        assert first["project"]["body"]["data"]["name"] == "Before"
        assert second["project"]["body"]["data"]["name"] == "After"
        with engine.connect() as connection:
            actual_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert actual_mode == journal_mode.lower()
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
            *,
            evaluated_at: datetime,
        ) -> WorkflowPosition:
            overlap.wait(timeout=5)
            return original_position(snapshot, evaluated_at=evaluated_at)

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


@pytest.mark.parametrize("raise_in_view", [False, True])
def test_dashboard_copy_is_readonly_and_closes_after_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_in_view: bool,
) -> None:
    """Live pool access remains available while the private copy is in use."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'copy-cleanup.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(name="Read-only copy")
            session.add(project)
            session.commit()
            session.refresh(project)
            assert project.project_id is not None
            project_id = project.project_id
        copies: list[sqlite3.Connection] = []
        original_backup = dashboard_reads._backup_sqlite

        def track_backup(
            source: sqlite3.Connection,
            target: sqlite3.Connection,
            *,
            deadline: float,
        ) -> None:
            copies.append(target)
            original_backup(source, target, deadline=deadline)

        monkeypatch.setattr(dashboard_reads, "_backup_sqlite", track_backup)
        application = _application(engine)

        def read_view() -> None:
            with dashboard_read_view(application, project_id) as view:
                assert view.application is not None
                assert len(copies) == 1
                target = copies[0]
                assert target.execute("PRAGMA query_only").fetchone() == (1,)
                with pytest.raises(sqlite3.OperationalError):
                    target.execute("UPDATE projects SET name='Forbidden'")
                with Session(engine) as live_session:
                    assert live_session.get(Project, project_id) is not None
                    result = view.application.reads.project_show(project_id=project_id)
                    assert result["ok"] is True
                if raise_in_view:
                    msg = "Projection stopped"
                    raise RuntimeError(msg)

        if raise_in_view:
            with pytest.raises(RuntimeError, match="Projection stopped"):
                read_view()
        else:
            read_view()

        assert len(copies) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            copies[0].execute("SELECT 1")
        with Session(engine) as session:
            assert session.get(Project, project_id) is not None
    finally:
        engine.dispose()


def test_dashboard_snapshot_timeout_is_whole_response_and_releases_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exclusive rollback-journal lock yields 503 without a partial bundle."""
    database_path = tmp_path / "locked-copy.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(name="Lock timeout")
            session.add(project)
            session.commit()
            session.refresh(project)
            assert project.project_id is not None
            project_id = project.project_id
        application = _application(engine)
        monkeypatch.setattr(api_module, "_application", lambda: application)
        monkeypatch.setattr(dashboard_reads, "_SNAPSHOT_TIMEOUT_SECONDS", 0.01)
        client = TestClient(api_module.app)
        path = f"/api/projects/{project_id}/dashboard"
        blocker = sqlite3.connect(database_path, check_same_thread=False)
        watchdog_expired = Event()

        def release_stuck_backup() -> None:
            watchdog_expired.set()
            blocker.rollback()

        watchdog = Timer(10, release_stuck_backup)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            watchdog.start()
            response = client.get(path)
            assert not watchdog_expired.is_set()
            assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
            assert response.json() == {
                "detail": "Dashboard snapshot is temporarily unavailable."
            }
            assert response.headers["retry-after"] == "1"
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        finally:
            watchdog.cancel()
            if watchdog.ident is not None:
                watchdog.join()
            blocker.rollback()
            blocker.close()
        monkeypatch.setattr(dashboard_reads, "_SNAPSHOT_TIMEOUT_SECONDS", 1.0)
        recovered = client.get(path)
        assert recovered.status_code == HTTPStatus.OK
        assert tuple(recovered.json()["data"]) == tuple(_ROUTES)
    finally:
        engine.dispose()


def test_dashboard_accepts_structural_timed_domain_adapter(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed snapshot adapter need not inherit the concrete domain class."""
    with Session(engine) as session:
        project = Project(name="Structural domain")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(datetime(2026, 9, 23, tzinfo=UTC)),
    )
    adapter = SimpleNamespace(
        position=domain.position,
        transition=domain.transition,
        load_persisted_attempt_input=domain.load_persisted_attempt_input,
        evaluation_time=domain.evaluation_time,
        position_from_snapshot=domain.position_from_snapshot,
        _engine=engine,
    )
    application = AgileForgeApplication(
        workflow_domain=cast("WorkflowDomainPort", adapter),
        read_projection=DurableReadProjectionService(engine=engine),
        delivery_review_selection=DeliveryReviewSelectionService(engine=engine),
    )
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).get(f"/api/projects/{project_id}/dashboard")

    assert response.status_code == HTTPStatus.OK
    position = response.json()["data"]["position"]["body"]["data"]
    assert position["project_id"] == project_id
