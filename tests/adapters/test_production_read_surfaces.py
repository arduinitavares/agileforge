"""Production transport contracts for retained non-routing reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api as api_module
from cli.main import main
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.read_projections import DurableReadProjectionService
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
)

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


class _ProjectionApplication:
    """Expose the real durable projection through production transports."""

    def __init__(self, reads: DurableReadProjectionService) -> None:
        self._reads = reads

    @property
    def reads(self) -> DurableReadProjectionService:
        return self._reads


_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "command",
        "expected_setup_status",
        "expected_state",
        "fsm_state",
        "guard_tokens",
        "next_actions",
        "recommendation",
        "review_token",
        "setup_status",
    }
)
_FORBIDDEN_AUTHORITY_VALUES = frozenset(
    {
        "SETUP_REQUIRED",
        "authority_pending_review",
    }
)
_FORBIDDEN_AUTHORITY_COMMANDS = (
    "agileforge authority accept",
    "agileforge authority reject",
)


def _assert_facts_only_authority_payload(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in _FORBIDDEN_AUTHORITY_KEYS
            _assert_facts_only_authority_payload(child)
        return
    if isinstance(value, list):
        for child in value:
            _assert_facts_only_authority_payload(child)
        return
    if isinstance(value, str):
        assert value not in _FORBIDDEN_AUTHORITY_VALUES
        assert all(command not in value for command in _FORBIDDEN_AUTHORITY_COMMANDS)


def _seed_project(session: Session, *, name: str = "Read projection") -> int:
    project = Product(name=name, origin="greenfield")
    session.add(project)
    session.commit()
    session.refresh(project)
    assert project.product_id is not None
    return project.product_id


def _compiled_authority_json(*, theme: str, gap: str) -> str:
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=[theme],
        domain="workflow",
        invariants=[
            Invariant(
                id="INV-0123456789abcdef",
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="project_id"),
            )
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[gap],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-0123456789abcdef",
                excerpt="Every durable read identifies its Project.",
                location="REQ.project-read",
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    return artifact.model_dump_json()


def _seed_authority_review_project(session: Session) -> tuple[int, int, int]:
    project_id = _seed_project(session, name="Authority review")
    accepted_spec = SpecRegistry(
        product_id=project_id,
        spec_hash="sha256:accepted",
        content="# Accepted\nAccepted authority source.",
        status="superseded",
        approved_at=datetime(2026, 8, 1, tzinfo=UTC),
        approved_by="reviewer",
    )
    pending_spec = SpecRegistry(
        product_id=project_id,
        spec_hash="sha256:pending",
        content="# Pending\nPending authority source.",
        status="approved",
        approved_at=datetime(2026, 8, 2, tzinfo=UTC),
        approved_by="reviewer",
    )
    session.add(accepted_spec)
    session.add(pending_spec)
    session.commit()
    session.refresh(accepted_spec)
    session.refresh(pending_spec)
    assert accepted_spec.spec_version_id is not None
    assert pending_spec.spec_version_id is not None

    accepted_authority = CompiledSpecAuthority(
        spec_version_id=accepted_spec.spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
        compiled_at=datetime(2026, 8, 1, 1, tzinfo=UTC),
        compiled_artifact_json=_compiled_authority_json(
            theme="Accepted scope",
            gap="Accepted historical gap",
        ),
        scope_themes='["Accepted scope"]',
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps='["Accepted historical gap"]',
    )
    pending_authority = CompiledSpecAuthority(
        spec_version_id=pending_spec.spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
        compiled_at=datetime(2026, 8, 2, 1, tzinfo=UTC),
        compiled_artifact_json=_compiled_authority_json(
            theme="Pending scope",
            gap="Pending review gap",
        ),
        scope_themes='["Pending scope"]',
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps='["Pending review gap"]',
    )
    session.add(accepted_authority)
    session.add(pending_authority)
    session.commit()
    session.refresh(accepted_authority)
    session.refresh(pending_authority)
    assert accepted_authority.authority_id is not None
    assert pending_authority.authority_id is not None
    fingerprint = pending_authority_fingerprint(accepted_authority)
    assert fingerprint is not None
    session.add(
        SpecAuthorityAcceptance(
            product_id=project_id,
            spec_version_id=accepted_spec.spec_version_id,
            status="accepted",
            policy="test",
            decided_by="reviewer",
            decided_at=datetime(2026, 8, 1, 2, tzinfo=UTC),
            rationale="Accepted durable authority.",
            compiler_version=accepted_authority.compiler_version,
            prompt_hash=accepted_authority.prompt_hash,
            spec_hash=accepted_spec.spec_hash,
            pending_authority_id=accepted_authority.authority_id,
            authority_fingerprint=fingerprint,
            review_token=f"legacy-token-{fingerprint}",
        )
    )
    session.commit()
    return (
        project_id,
        accepted_authority.authority_id,
        pending_authority.authority_id,
    )


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


def test_authority_review_projection_is_facts_only_and_keeps_review_data(
    engine: Engine,
    session: Session,
) -> None:
    """Keep durable review evidence while excluding the legacy routing packet."""
    project_id, accepted_authority_id, pending_authority_id = (
        _seed_authority_review_project(session)
    )
    projection = DurableReadProjectionService(engine=engine)

    result = projection.authority_review(
        project_id=project_id,
        include_spec="summary",
    )

    assert result["ok"] is True
    _assert_facts_only_authority_payload(result)
    data = result["data"]
    assert isinstance(data, dict)
    assert data["schema_version"] == "agileforge.authority_review_projection.v1"
    accepted = data["accepted_authority"]
    pending = data["pending_authority"]
    findings = data["findings"]
    assert isinstance(accepted, dict)
    assert isinstance(pending, dict)
    assert isinstance(findings, list)
    assert accepted["authority_id"] == accepted_authority_id
    assert accepted["status"] == "accepted"
    assert pending["authority_id"] == pending_authority_id
    assert pending["status"] == "pending_review"
    assert pending["invariants"] == [
        {
            "id": "INV-0123456789abcdef",
            "type": "REQUIRED_FIELD",
            "source_item_id": None,
            "source_level": None,
            "parameters": {"field_name": "project_id"},
        }
    ]
    assert pending["artifact"] is not None
    assert {item["message"] for item in findings if isinstance(item, dict)} == {
        "Pending review gap"
    }


def test_live_authority_review_api_returns_only_durable_review_facts(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the live authority endpoint returns the facts-only projection."""
    project_id, accepted_authority_id, pending_authority_id = (
        _seed_authority_review_project(session)
    )
    application = _ProjectionApplication(DurableReadProjectionService(engine=engine))
    monkeypatch.setattr(api_module, "_application", lambda: application)

    response = TestClient(api_module.app).get(
        f"/api/projects/{project_id}/authority/review?include_spec=summary"
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    _assert_facts_only_authority_payload(payload)
    data = payload["data"]
    assert data["accepted_authority"]["authority_id"] == accepted_authority_id
    assert data["pending_authority"]["authority_id"] == pending_authority_id
    assert data["findings"][0]["message"] == "Pending review gap"


def test_production_read_composition_does_not_import_legacy_review_service() -> None:
    """Keep the legacy review service unreachable from production reads."""
    source = Path("services/read_projections.py").read_text(encoding="utf-8")

    assert "AuthorityReviewService" not in source


def test_every_project_scoped_read_uses_project_not_found(
    engine: Engine,
) -> None:
    """Use one missing-Project contract across every retained scoped read."""
    projection = DurableReadProjectionService(engine=engine)
    missing_project_id = 404_404

    results = [
        projection.project_show(project_id=missing_project_id),
        projection.authority_status(project_id=missing_project_id),
        projection.authority_invariants(project_id=missing_project_id),
        projection.authority_review(project_id=missing_project_id),
        projection.artifact_history(
            project_id=missing_project_id,
            node_id="vision.generate",
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
        node_id="vision.generate",
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
