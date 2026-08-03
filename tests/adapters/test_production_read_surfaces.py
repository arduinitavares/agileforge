"""Production transport contracts for retained non-routing reads."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api as api_module
from cli.main import main

if TYPE_CHECKING:
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

    def authority_status(self, *, project_id: int) -> JsonObject:
        self.calls.append(("authority_status", {"project_id": project_id}))
        return _read_result("authority-status")

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject:
        self.calls.append(
            (
                "authority_invariants",
                {"project_id": project_id, "spec_version_id": spec_version_id},
            )
        )
        return _read_result("authority-invariants")

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
    ) -> JsonObject:
        self.calls.append(
            (
                "authority_review",
                {"project_id": project_id, "include_spec": include_spec},
            )
        )
        return _read_result("authority-review")

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
        ("GET", "/api/projects/{project_id}/authority/status"),
        ("GET", "/api/projects/{project_id}/authority/invariants"),
        ("GET", "/api/projects/{project_id}/authority/review"),
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


def test_production_api_read_handlers_use_injected_non_routing_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise project, authority, history, pending, status, and packet reads."""
    application = _FakeReadApplication()
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    responses = [
        client.get("/api/projects/41"),
        client.get("/api/projects/41/authority/review?include_spec=summary"),
        client.get("/api/projects/41/vision/history"),
        client.get("/api/projects/41/story/pending"),
        client.get("/api/projects/41/sprints/7"),
        client.get("/api/projects/41/sprints/7/tasks/13/packet?flavor=compact"),
    ]

    assert all(response.status_code == HTTPStatus.OK for response in responses)
    assert [response.json()["data"]["marker"] for response in responses] == [
        "project-show",
        "authority-review",
        "artifact-history",
        "story-pending",
        "sprint-status",
        "task-packet",
    ]
    assert application.calls == [
        ("project_show", {"project_id": 41}),
        (
            "authority_review",
            {"project_id": 41, "include_spec": "summary"},
        ),
        (
            "artifact_history",
            {"project_id": 41, "node_id": "vision.generate", "instance_key": None},
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
        (["authority", "status", "--project-id", "41"], "authority_status"),
        (["authority", "review", "--project-id", "41"], "authority_review"),
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
