"""Production transport contracts for retained non-routing reads."""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api as api_module
from cli.main import main
from models.core import Project
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.read_projections import DurableReadProjectionService

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from workflow.contracts import JsonObject


def _read_result(marker: str) -> JsonObject:
    return {
        "ok": True,
        "data": {"marker": marker},
        "warnings": [],
        "errors": [],
    }


class _FakeReadApplication:
    """Record read-only calls made by production transports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []

    @property
    def reads(self) -> _FakeReadApplication:
        """Expose this fake as the injected read projection."""
        return self

    def project_list(self) -> JsonObject:
        self.calls.append(("project_list", {}))
        return _read_result("project-list")

    def project_show(self, *, project_id: int) -> JsonObject:
        self.calls.append(("project_show", {"project_id": project_id}))
        return _read_result("project-show")

    def artifact_history(
        self,
        *,
        project_id: int,
        node_id: str,
        instance_key: str | None = None,
    ) -> JsonObject:
        self.calls.append(
            (
                "artifact_history",
                {
                    "project_id": project_id,
                    "node_id": node_id,
                    "instance_key": instance_key,
                },
            )
        )
        return _read_result("artifact-history")

    def story_show(self, *, story_id: int) -> JsonObject:
        self.calls.append(("story_show", {"story_id": story_id}))
        return _read_result("story-show")

    def story_pending(self, *, project_id: int) -> JsonObject:
        self.calls.append(("story_pending", {"project_id": project_id}))
        return _read_result("story-pending")

    def story_dependencies_inspect(self, *, project_id: int) -> JsonObject:
        self.calls.append(("story_dependencies_inspect", {"project_id": project_id}))
        return _read_result("story-dependencies")

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        self.calls.append(("sprint_candidates", {"project_id": project_id}))
        return _read_result("sprint-candidates")

    def sprint_history(self, *, project_id: int) -> JsonObject:
        self.calls.append(("sprint_history", {"project_id": project_id}))
        return _read_result("sprint-history")

    def sprint_metrics(self, *, project_id: int) -> JsonObject:
        self.calls.append(("sprint_metrics", {"project_id": project_id}))
        return _read_result("sprint-metrics")

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        self.calls.append(
            ("sprint_status", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return _read_result("sprint-status")

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        self.calls.append(
            ("sprint_tasks", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return _read_result("sprint-tasks")

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        self.calls.append(
            (
                "sprint_task_show",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "sprint_id": sprint_id,
                },
            )
        )
        return _read_result("sprint-task-show")

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        self.calls.append(
            (
                "sprint_task_history",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "sprint_id": sprint_id,
                },
            )
        )
        return _read_result("sprint-task-history")

    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        self.calls.append(
            ("sprint_review", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return _read_result("sprint-review")

    def task_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        task_id: int,
        flavor: str | None = None,
    ) -> JsonObject:
        self.calls.append(
            (
                "task_packet",
                {
                    "project_id": project_id,
                    "sprint_id": sprint_id,
                    "task_id": task_id,
                    "flavor": flavor,
                },
            )
        )
        return _read_result("task-packet")

    def story_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        story_id: int,
        flavor: str | None = None,
    ) -> JsonObject:
        self.calls.append(
            (
                "story_packet",
                {
                    "project_id": project_id,
                    "sprint_id": sprint_id,
                    "story_id": story_id,
                    "flavor": flavor,
                },
            )
        )
        return _read_result("story-packet")

    def context_pack(self, *, project_id: int, phase: str) -> JsonObject:
        self.calls.append(("context_pack", {"project_id": project_id, "phase": phase}))
        return _read_result("context-pack")

    def status(self, *, project_id: int) -> JsonObject:
        self.calls.append(("status", {"project_id": project_id}))
        return _read_result("status")


class _ProjectionApplication:
    """Expose the real durable projection through production transports."""

    def __init__(self, reads: DurableReadProjectionService) -> None:
        self._reads = reads

    @property
    def reads(self) -> DurableReadProjectionService:
        return self._reads


def _seed_project(session: Session, *, name: str = "Read projection") -> int:
    project = Project(name=name)
    session.add(project)
    session.commit()
    session.refresh(project)
    assert project.project_id is not None
    return project.project_id


def test_production_api_registers_retained_read_routes() -> None:
    """Keep supported inspection routes in the live API module."""
    routes = {
        (method, route.path)
        for route in api_module.app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    }
    expected = {
        ("GET", "/api/projects/{project_id}"),
        ("GET", "/api/projects/{project_id}/vision/history"),
        ("GET", "/api/projects/{project_id}/backlog/history"),
        ("GET", "/api/projects/{project_id}/roadmap/history"),
        ("GET", "/api/projects/{project_id}/story/pending"),
        ("GET", "/api/projects/{project_id}/story/history"),
        ("GET", "/api/projects/{project_id}/story/dependencies"),
        ("GET", "/api/projects/{project_id}/sprint/history"),
        ("GET", "/api/projects/{project_id}/sprint/metrics"),
        ("GET", "/api/projects/{project_id}/sprints"),
        ("GET", "/api/projects/{project_id}/sprints/{sprint_id}"),
        ("GET", "/api/projects/{project_id}/sprints/{sprint_id}/tasks"),
        ("GET", "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}"),
        (
            "GET",
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
        ),
        (
            "GET",
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet",
        ),
        (
            "GET",
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet",
        ),
    }
    assert expected <= routes


def test_production_api_registers_semantic_lifecycle_routes() -> None:
    """Expose the complete task-specific lifecycle transport surface."""
    routes = {
        (method, route.path)
        for route in api_module.app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    }
    expected = {
        ("POST", "/api/projects"),
        ("GET", "/api/projects/{project_id}"),
        ("DELETE", "/api/projects/{project_id}"),
        ("GET", "/api/projects/{project_id}/position"),
        ("POST", "/api/projects/{project_id}/vision/respond"),
        ("GET", "/api/projects/{project_id}/vision/status"),
        ("POST", "/api/projects/{project_id}/vision/review"),
        ("POST", "/api/projects/{project_id}/vision/revision"),
        ("POST", "/api/projects/{project_id}/goals/respond"),
        ("GET", "/api/projects/{project_id}/goals/status"),
        ("POST", "/api/projects/{project_id}/goals/review"),
        ("POST", "/api/projects/{project_id}/goals/complete"),
        ("POST", "/api/projects/{project_id}/goals/abandon"),
        ("POST", "/api/projects/{project_id}/specifications/source"),
        ("POST", "/api/projects/{project_id}/specifications/structure"),
        ("GET", "/api/projects/{project_id}/specifications/review"),
        ("POST", "/api/projects/{project_id}/specifications/review"),
        ("POST", "/api/projects/{project_id}/repository"),
        ("GET", "/api/projects/{project_id}/repository"),
        ("POST", "/api/projects/{project_id}/repository/refresh"),
    }

    assert expected <= routes
    assert ("POST", "/api/projects/{project_id}/specifications/author") not in routes


def test_production_api_read_handlers_use_injected_non_routing_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise project, history, pending, status, and packet reads."""
    application = _FakeReadApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    responses = [
        client.get("/api/projects/41"),
        client.get("/api/projects/41/vision/history"),
        client.get("/api/projects/41/story/pending"),
        client.get("/api/projects/41/sprints/7"),
        client.get("/api/projects/41/sprints/7/tasks/13/packet?flavor=compact"),
    ]

    assert all(response.status_code == HTTPStatus.OK for response in responses)
    assert [response.json()["data"]["marker"] for response in responses] == [
        "project-show",
        "artifact-history",
        "story-pending",
        "sprint-status",
        "task-packet",
    ]
    assert application.calls == [
        ("project_show", {"project_id": 41}),
        (
            "artifact_history",
            {"project_id": 41, "node_id": "vision.interview", "instance_key": None},
        ),
        ("story_pending", {"project_id": 41}),
        ("sprint_status", {"project_id": 41, "sprint_id": 7}),
        (
            "task_packet",
            {"project_id": 41, "sprint_id": 7, "task_id": 13, "flavor": "compact"},
        ),
    ]


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["project", "list"], "project_list"),
        (["project", "show", "--project-id", "41"], "project_show"),
        (["vision", "history", "--project-id", "41"], "artifact_history"),
        (["backlog", "history", "--project-id", "41"], "artifact_history"),
        (["roadmap", "history", "--project-id", "41"], "artifact_history"),
        (["story", "pending", "--project-id", "41"], "story_pending"),
        (["sprint", "history", "--project-id", "41"], "sprint_history"),
        (["sprint", "status", "--project-id", "41"], "sprint_status"),
        (["sprint", "tasks", "--project-id", "41"], "sprint_tasks"),
        (
            [
                "sprint",
                "task",
                "history",
                "--project-id",
                "41",
                "--task-id",
                "13",
            ],
            "sprint_task_history",
        ),
        (["context", "pack", "--project-id", "41"], "context_pack"),
        (["status", "--project-id", "41"], "status"),
    ],
)
def test_production_cli_retains_read_commands(
    argv: list[str],
    expected_call: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise retained reads through production cli/main.py."""
    application = _FakeReadApplication()

    exit_code = main(argv, application=application)

    assert exit_code == 0
    assert application.calls[0][0] == expected_call
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_production_read_composition_does_not_import_legacy_review_service() -> None:
    """Keep the legacy review service unreachable from production reads."""
    source = Path("services/read_projections.py").read_text(encoding="utf-8")

    assert "AuthorityReview" + "Service" not in source


def test_every_project_scoped_read_uses_project_not_found(
    engine: Engine,
) -> None:
    """Use one missing-Project contract across every retained scoped read."""
    projection = DurableReadProjectionService(engine=engine)
    missing_project_id = 404_404

    results = [
        projection.project_show(project_id=missing_project_id),
        projection.artifact_history(
            project_id=missing_project_id,
            node_id="vision.interview",
        ),
        projection.story_pending(project_id=missing_project_id),
        projection.story_dependencies_inspect(project_id=missing_project_id),
        projection.sprint_candidates(project_id=missing_project_id),
        projection.sprint_history(project_id=missing_project_id),
        projection.sprint_metrics(project_id=missing_project_id),
        projection.sprint_status(project_id=missing_project_id),
        projection.sprint_tasks(project_id=missing_project_id),
        projection.sprint_task_show(project_id=missing_project_id, task_id=11),
        projection.sprint_task_history(project_id=missing_project_id, task_id=11),
        projection.sprint_review(project_id=missing_project_id),
        projection.task_packet(
            project_id=missing_project_id,
            sprint_id=7,
            task_id=11,
        ),
        projection.story_packet(
            project_id=missing_project_id,
            sprint_id=7,
            story_id=13,
        ),
        projection.context_pack(project_id=missing_project_id, phase="planning"),
        projection.status(project_id=missing_project_id),
    ]

    for result in results:
        assert result["ok"] is False
        errors = result["errors"]
        assert isinstance(errors, list)
        first_error = errors[0]
        assert isinstance(first_error, dict)
        assert first_error["code"] == "PROJECT_NOT_FOUND"


def test_existing_project_keeps_empty_artifact_history_success(
    engine: Engine,
    session: Session,
) -> None:
    """Distinguish an existing Project with no attempts from a missing Project."""
    project_id = _seed_project(session, name="No history")
    projection = DurableReadProjectionService(engine=engine)

    result = projection.artifact_history(
        project_id=project_id,
        node_id="vision.interview",
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert data["items"] == []
    assert data["count"] == 0


def test_existing_project_fact_conflict_remains_distinct_from_not_found(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep durable fact conflicts separate from Project lookup failures."""
    project_id = _seed_project(session, name="Broken facts")
    projection = DurableReadProjectionService(engine=engine)

    def fail_load(
        _repository: WorkflowFactRepository,
        _project_id: int,
    ) -> object:
        message = "Stored facts conflict."
        raise WorkflowFactLoadError(message)

    monkeypatch.setattr(WorkflowFactRepository, "load", fail_load)

    result = projection.story_pending(project_id=project_id)

    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    first_error = errors[0]
    assert isinstance(first_error, dict)
    assert first_error["code"] == "PROJECT_FACTS_UNAVAILABLE"


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/404404/vision/history",
        "/api/projects/404404/story/pending",
        "/api/projects/404404/sprints/7",
        "/api/projects/404404/sprints/7/tasks/11",
    ],
)
def test_live_read_api_maps_missing_project_to_404(
    path: str,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map representative real projection misses to HTTP 404."""
    application = _ProjectionApplication(DurableReadProjectionService(engine=engine))
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).get(path)

    assert response.status_code == HTTPStatus.NOT_FOUND
    detail = response.json()["detail"]
    assert detail["errors"][0]["code"] == "PROJECT_NOT_FOUND"


def test_live_read_api_keeps_existing_empty_history_at_200(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep empty attempt history successful through the live API."""
    project_id = _seed_project(session, name="API empty history")
    application = _ProjectionApplication(DurableReadProjectionService(engine=engine))
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).get(
        f"/api/projects/{project_id}/vision/history"
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"]["items"] == []


def test_production_cli_returns_failure_for_missing_project_read(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Return exit one and PROJECT_NOT_FOUND for a missing scoped CLI read."""
    application = _ProjectionApplication(DurableReadProjectionService(engine=engine))

    exit_code = main(
        ["vision", "history", "--project-id", "404404"],
        application=application,
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["code"] == "PROJECT_NOT_FOUND"


# Retained Task 11 planning-review read coverage.
def _review(phase: str) -> dict[str, object]:
    evidence = [
        {
            "title": "Exact evidence",
            "statement": "The exact planning source is reviewed.",
            "level": "MUST",
            "acceptance_criteria": ["The planning source remains exact."],
            "verification_method": "acceptance-test",
        }
    ]
    backlog_item = {
        "requirement": "Review exact planning content",
        "priority": 1,
        "value_driver": "Strategic",
        "justification": "The operator needs exact evidence.",
        "estimated_effort": "M",
        "technical_note": None,
        "specification_evidence": evidence,
    }
    if phase == "backlog":
        candidate = {
            "backlog_items": [backlog_item],
            "is_complete": True,
            "clarifying_questions": [],
        }
        lineage = {}
    elif phase == "roadmap":
        candidate = {
            "roadmap_summary": "Deliver exact planning.",
            "roadmap_releases": [
                {
                    "release_name": "Exact release",
                    "theme": "Trust",
                    "focus_area": "User Value",
                    "reasoning": "Review evidence first.",
                    "backlog_items": [backlog_item],
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
        lineage = {}
    elif phase == "story":
        candidate = {
            "story_items": [
                {
                    "story_title": "Review exact Story",
                    "statement": "As an operator, I want exact evidence.",
                    "persona": "operator",
                    "acceptance_criteria": ["Evidence is visible."],
                    "specification_evidence": evidence,
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
        lineage = {"backlog_item": backlog_item}
    else:
        story = {
            "title": "Review exact Story",
            "statement": "As an operator, I want exact evidence.",
            "persona": "operator",
            "acceptance_criteria": ["Evidence is visible."],
            "specification_evidence": evidence,
            "reason_for_selection": "Highest value.",
            "tasks": [
                {
                    "description": "Implement exact review",
                    "task_kind": "implementation",
                    "checklist_items": ["Verify evidence"],
                    "specification_evidence": evidence,
                }
            ],
        }
        candidate = {
            "team_name": "Review team",
            "sprint_owner": {
                "kind": "legacy_named_team",
                "key": (
                    "agileforge:sprint-owner:legacy-named-team:v1:sha256:"
                    "4433d3cbd694dfae6e3f5747577b68f57c2df760563d7524a4e9635d721c64ff"
                ),
                "label": "Review team",
            },
            "sprint_goal": "Deliver exact review.",
            "selected_stories": [story],
        }
        lineage = {}
    return {"phase": phase, "lineage": lineage, "candidate": candidate}


def _result(marker: str) -> dict[str, object]:
    phase = {
        "backlog-review": "backlog",
        "roadmap-review": "roadmap",
        "story-reviews": "story",
        "sprint-plan-review": "sprint_plan",
    }[marker]
    selected = {
        "binding": {"decision_fingerprint": "hidden", "instance_key": None},
        "review": _review(phase),
    }
    data = (
        {"marker": marker, "items": [selected]}
        if phase == "story"
        else {"marker": marker, **selected}
    )
    return {"ok": True, "data": data, "warnings": [], "errors": []}


class _Application:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def backlog_review(self, project_id: int) -> dict[str, object]:
        self.calls.append(("backlog_review", project_id))
        return _result("backlog-review")

    def roadmap_review(self, project_id: int) -> dict[str, object]:
        self.calls.append(("roadmap_review", project_id))
        return _result("roadmap-review")

    def story_reviews(self, project_id: int) -> dict[str, object]:
        self.calls.append(("story_reviews", project_id))
        return _result("story-reviews")

    def sprint_plan_review(self, project_id: int) -> dict[str, object]:
        self.calls.append(("sprint_plan_review", project_id))
        return _result("sprint-plan-review")


def _routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in api_module.app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    }


def test_production_api_registers_exact_planning_review_routes() -> None:
    """Register the four retained planning-review read routes."""
    assert {
        ("GET", "/api/projects/{project_id}/backlog/review"),
        ("GET", "/api/projects/{project_id}/roadmap/review"),
        ("GET", "/api/projects/{project_id}/story/reviews"),
        ("GET", "/api/projects/{project_id}/sprint/plan/review"),
    } <= _routes()


def test_production_api_has_no_retired_operator_routes() -> None:
    """Keep removed operator routes absent from the production API."""
    paths = {path.casefold() for _, path in _routes()}
    assert all("auth" + "ority" not in path for path in paths)
    assert all("invar" + "iant" not in path for path in paths)


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        ("/api/projects/41/backlog/review", "backlog-review"),
        ("/api/projects/41/roadmap/review", "roadmap-review"),
        ("/api/projects/41/story/reviews", "story-reviews"),
        ("/api/projects/41/sprint/plan/review", "sprint-plan-review"),
    ],
)
def test_review_gets_use_injected_application(
    path: str,
    marker: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serve each planning-review read through the injected application."""
    application = _Application()
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).get(path)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"]["marker"] == marker


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["backlog", "review", "--project-id", "41"], "backlog_review"),
        (["roadmap", "review", "--project-id", "41"], "roadmap_review"),
        (["story", "reviews", "--project-id", "41"], "story_reviews"),
        (["sprint", "plan-review", "--project-id", "41"], "sprint_plan_review"),
    ],
)
def test_production_cli_exposes_exact_review_reads(
    argv: list[str], expected_call: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dispatch each retained CLI review read to the matching application call."""
    application = _Application()

    assert main(argv, application=application) == 0

    assert application.calls == [(expected_call, 41)]
    output = capsys.readouterr().out
    assert "Exact" in output
    assert "{" not in output


def test_live_task11_read_paths_have_no_retired_names() -> None:
    """Keep retired operator terminology out of active Task 11 read paths."""
    for path in (
        "api.py",
        "cli/main.py",
        "cli/workflow_commands.py",
        "services/application.py",
        "services/read_projections.py",
    ):
        source = Path(path).read_text(encoding="utf-8").casefold()
        assert "auth" + "ority" not in source
        assert "invar" + "iant" not in source
