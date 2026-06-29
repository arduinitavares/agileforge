"""API tests for deterministic setup-first dashboard endpoints."""

import asyncio
import concurrent.futures
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
from fastapi.testclient import TestClient

import api as api_module
from services.agent_workbench.authority_decision import (
    AuthorityAcceptRequest,
    AuthorityRejectRequest,
)
from services.agent_workbench.authority_projection import _AuthoritySelection
from services.agent_workbench.authority_review import AuthorityReviewSnapshot
from services.specs.compiler_service import CompiledArtifactLoadResult

if TYPE_CHECKING:
    from sqlmodel import Session

    from models.specs import CompiledSpecAuthority, SpecRegistry
    from utils.spec_schemas import ValidationEvidence
    from utils.task_metadata import TaskMetadata

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409
HTTP_TEMP_REDIRECT = 307
HTTP_UNPROCESSABLE = 422
HTTP_SERVER_ERROR = 500
REVIEW_FIELD = "review_token"
AUTHORITY_REVIEW_FIXTURE = "agileforge.authority_review.v1:sha256:test"
LEGACY_COMPILED_AUTHORITY_JSON = '{"invariants":[]}'


class _StateContext(Protocol):
    """Minimal tool context shape used by dashboard route shims."""

    state: dict[str, object]


def test_api_uses_public_spec_lifecycle_wrapper() -> None:
    """Confirm the API uses the public spec lifecycle wrapper."""
    assert (
        api_module.link_spec_to_product.__module__ == "services.specs.lifecycle_service"
    )


@dataclass
class DummyProduct:
    """Simple in-memory product used by dashboard API tests."""

    product_id: int
    name: str
    description: str | None = None
    vision: str | None = None
    spec_file_path: str | None = None
    compiled_authority_json: str | None = None


class DummyProductRepository:
    """Tiny repository double for dashboard route tests."""

    def __init__(self) -> None:
        """Initialize the in-memory product list."""
        self.products = []

    def get_all(self) -> list[DummyProduct]:
        """Return all known products."""
        return list(self.products)

    def get_by_id(self, product_id: int) -> DummyProduct | None:
        """Return the product matching the provided ID."""
        for product in self.products:
            if product.product_id == product_id:
                return product
        return None

    def create(
        self,
        name: str,
        description: str | None = None,
    ) -> DummyProduct:
        """Create and store a new in-memory product."""
        product = DummyProduct(
            product_id=len(self.products) + 1,
            name=name,
            description=description,
        )
        self.products.append(product)
        return product


class DummyWorkflowService:
    """Workflow-state double used by dashboard route tests."""

    def __init__(self) -> None:
        """Initialize the in-memory workflow state store."""
        self.states: dict[str, dict[str, object]] = {}
        self.single_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    async def initialize_session(self, session_id: str | None = None) -> str:
        """Create a session with the setup-required FSM state."""
        sid = str(session_id or "generated")
        self.states[sid] = {"fsm_state": "SETUP_REQUIRED"}
        return sid

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return a shallow copy of the stored session state."""
        self.single_calls.append(str(session_id))
        return dict(self.states.get(str(session_id), {}))

    def get_session_statuses(
        self,
        session_ids: list[str],
    ) -> dict[str, dict[str, object]]:
        """Return state snapshots for the provided session IDs."""
        normalized = [str(session_id) for session_id in session_ids]
        self.batch_calls.append(normalized)
        return {
            session_id: dict(self.states.get(session_id, {}))
            for session_id in normalized
        }

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Merge a partial state update into the stored session state."""
        sid = str(session_id)
        current = dict(self.states.get(sid, {}))
        current.update(partial_update)
        self.states[sid] = current

    def migrate_legacy_setup_state(self) -> int:
        """Normalize legacy routing-mode sessions to setup-required."""
        migrated = 0
        for sid, payload in self.states.items():
            if payload.get("fsm_state") == "ROUTING_MODE":
                self.states[sid]["fsm_state"] = "SETUP_REQUIRED"
                migrated += 1
        return migrated


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, DummyProductRepository, DummyWorkflowService]:
    repo = DummyProductRepository()
    workflow = DummyWorkflowService()

    monkeypatch.setattr(api_module, "product_repo", repo)
    monkeypatch.setattr(api_module, "workflow_service", workflow)

    def fake_select_project(
        product_id: int,
        context: _StateContext,
    ) -> dict[str, object]:
        product = repo.get_by_id(product_id)
        if not product:
            return {"success": False, "error": "missing"}
        context.state["active_project"] = {
            "product_id": product_id,
            "name": product.name,
            "vision": product.vision,
            "spec_file_path": product.spec_file_path,
        }
        return {"success": True}

    def fake_link_spec_to_product(
        params: dict[str, object],
        tool_context: _StateContext | None = None,
    ) -> dict[str, object]:
        product_id = params["product_id"]
        assert isinstance(product_id, int | str)
        product = repo.get_by_id(int(product_id))
        assert product is not None
        spec_path = params["spec_path"]
        assert isinstance(spec_path, str)

        if "invalid" in spec_path.lower():
            if tool_context:
                tool_context.state["setup_error"] = "invalid spec path"
            return {
                "success": True,
                "compile_success": False,
                "compile_error": "invalid spec path",
                "failure_artifact_id": "setup-artifact-1",
                "failure_stage": "output_validation",
                "failure_summary": "SPEC_COMPILATION_FAILED: invalid spec path",
                "raw_output_preview": '{"invalid": true}',
                "has_full_artifact": True,
            }

        product.spec_file_path = spec_path
        product.compiled_authority_json = '{"ok": true}'

        if tool_context:
            tool_context.state["pending_spec_path"] = spec_path
            tool_context.state["pending_spec_content"] = "SPEC"
            tool_context.state["compiled_authority_cached"] = '{"ok": true}'

        return {
            "success": True,
            "compile_success": True,
            "spec_path": spec_path,
        }

    monkeypatch.setattr(api_module, "select_project", fake_select_project)
    monkeypatch.setattr(api_module, "link_spec_to_product", fake_link_spec_to_product)

    async def fake_run_vision_agent_from_state(
        state: dict[str, object],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, object]:
        del project_id
        return {
            "success": True,
            "input_context": {
                "user_raw_text": user_input or "",
                "prior_vision_state": "NO_HISTORY",
                "specification_content": state.get("pending_spec_content", "SPEC"),
                "compiled_authority": state.get(
                    "compiled_authority_cached", '{"ok": true}'
                ),
            },
            "output_artifact": {
                "updated_components": {
                    "project_name": "Vision Project",
                    "target_user": None,
                    "problem": None,
                    "product_category": None,
                    "key_benefit": None,
                    "competitors": None,
                    "differentiator": None,
                },
                "product_vision_statement": "Draft",
                "is_complete": False,
                "clarifying_questions": ["Need details"],
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    monkeypatch.setattr(
        api_module, "run_vision_agent_from_state", fake_run_vision_agent_from_state
    )

    app_double = FakeAuthorityApplication(workflow=workflow, repo=repo)
    monkeypatch.setattr(
        api_module,
        "AgentWorkbenchApplication",
        lambda: app_double,
        raising=False,
    )

    return TestClient(api_module.app), repo, workflow


class FakeAuthorityApplication:
    """Application facade double for dashboard authority route tests."""

    def __init__(
        self,
        workflow: DummyWorkflowService | None = None,
        repo: DummyProductRepository | None = None,
    ) -> None:
        """Initialize captured request state."""
        self.workflow = workflow
        self.repo = repo
        self.accept_requests: list[AuthorityAcceptRequest] = []
        self.reject_requests: list[AuthorityRejectRequest] = []
        self.create_calls: list[dict[str, Any]] = []
        self.retry_calls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.results: dict[str, dict[str, object]] = {}

    def project_create(
        self,
        *,
        name: str,
        spec_file: str,
        setup_mode: str = "greenfield",
        idempotency_key: str,
        changed_by: str,
    ) -> dict[str, object]:
        """Mock project creation."""
        self.create_calls.append(
            {
                "name": name,
                "spec_file": spec_file,
                "setup_mode": setup_mode,
                "idempotency_key": idempotency_key,
                "changed_by": changed_by,
            }
        )
        if "project_create" in self.results:
            return self.results["project_create"]
        if not self.repo:
            return {"ok": False, "error": "Repo not initialized"}
        product = self.repo.create(name)
        if product.product_id is None:
            msg = "Repository failed to persist product ID"
            raise ValueError(msg)
        product.spec_file_path = spec_file

        if "nonexistent" in spec_file.lower():
            return {
                "ok": False,
                "data": None,
                "errors": [
                    {
                        "code": "SPEC_FILE_NOT_FOUND",
                        "message": f"Specification file not found at path {spec_file}",
                        "remediation": ["Please check if the file exists."],
                    }
                ],
                "warnings": [],
            }

        if "invalid" in spec_file.lower():
            data = {
                "project_id": product.product_id,
                "name": product.name,
                "setup_status": "failed",
                "setup_error": "invalid spec path",
                "fsm_state": "SETUP_REQUIRED",
                "setup_failure_artifact_id": "setup-artifact-1",
                "setup_failure_stage": "output_validation",
                "setup_failure_summary": ("SPEC_COMPILATION_FAILED: invalid spec path"),
                "raw_output_preview": '{"invalid": true}',
                "has_full_artifact": True,
            }
            if self.workflow:
                self.workflow.states[str(product.product_id)] = {
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_status": "failed",
                    "setup_error": "invalid spec path",
                    "setup_failure_artifact_id": "setup-artifact-1",
                    "setup_failure_stage": "output_validation",
                    "setup_failure_summary": (
                        "SPEC_COMPILATION_FAILED: invalid spec path"
                    ),
                    "setup_raw_output_preview": '{"invalid": true}',
                    "setup_has_full_artifact": True,
                    "setup_spec_file_path": spec_file,
                }
            return {
                "ok": False,
                "error": "SPEC_COMPILATION_FAILED: invalid spec path",
                "data": data,
            }

        state = {
            "fsm_state": "VISION_INTERVIEW",
            "setup_status": "passed",
            "pending_spec_path": spec_file,
            "pending_spec_content": "SPEC",
            "compiled_authority_cached": '{"ok": true}',
        }
        product.compiled_authority_json = '{"ok": true}'

        with concurrent.futures.ThreadPoolExecutor() as executor:
            vision_res = executor.submit(
                lambda: asyncio.run(
                    api_module.run_vision_agent_from_state(
                        state,
                        project_id=product.product_id,
                        user_input=None,
                    )
                )
            ).result()

        vision_auto = {
            "attempted": True,
            "success": bool(vision_res.get("success")),
            "is_complete": vision_res.get("is_complete"),
            "failure_artifact_id": vision_res.get("failure_artifact_id"),
            "failure_stage": vision_res.get("failure_stage"),
            "failure_summary": vision_res.get("failure_summary"),
            "raw_output_preview": vision_res.get("raw_output_preview"),
            "has_full_artifact": bool(vision_res.get("has_full_artifact")),
        }

        if self.workflow:
            workflow_state: dict[str, object] = {
                "fsm_state": "VISION_INTERVIEW",
                "setup_status": "passed",
                "pending_spec_path": spec_file,
                "pending_spec_content": "SPEC",
                "compiled_authority_cached": '{"ok": true}',
            }
            if not vision_res.get("success"):
                attempt = {
                    "trigger": "auto_setup_transition",
                    "is_complete": False,
                    "failure_artifact_id": vision_res.get("failure_artifact_id"),
                    "failure_stage": vision_res.get("failure_stage"),
                    "failure_summary": vision_res.get("failure_summary"),
                }
                workflow_state["vision_attempts"] = [attempt]
            self.workflow.states[str(product.product_id)] = workflow_state

        return {
            "ok": True,
            "data": {
                "project_id": product.product_id,
                "name": product.name,
                "setup_status": "passed",
                "fsm_state": "VISION_INTERVIEW",
                "vision_auto_run": vision_auto,
            },
        }

    def project_setup_retry(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_file: str,
        expected_state: str,
        expected_context_fingerprint: str,
        recovery_mutation_event_id: str | None,
        idempotency_key: str,
        changed_by: str,
    ) -> dict[str, object]:
        """Mock setup retry service."""
        self.retry_calls.append(
            {
                "project_id": project_id,
                "spec_file": spec_file,
                "expected_state": expected_state,
                "expected_context_fingerprint": expected_context_fingerprint,
                "recovery_mutation_event_id": recovery_mutation_event_id,
                "idempotency_key": idempotency_key,
                "changed_by": changed_by,
            }
        )
        if not self.repo:
            return {"ok": False, "error": "Repo not initialized"}
        product = self.repo.get_by_id(project_id)
        if not product:
            return {"ok": False, "error": "Project not found"}
        product.spec_file_path = spec_file

        if "nonexistent" in spec_file.lower():
            return {
                "ok": False,
                "data": None,
                "errors": [
                    {
                        "code": "SPEC_FILE_NOT_FOUND",
                        "message": f"Specification file not found at path {spec_file}",
                        "remediation": ["Please check if the file exists."],
                    }
                ],
                "warnings": [],
            }

        if "invalid" in spec_file.lower():
            data = {
                "project_id": product.product_id,
                "name": product.name,
                "setup_status": "failed",
                "setup_error": "invalid spec path",
                "fsm_state": "SETUP_REQUIRED",
                "setup_failure_artifact_id": "setup-artifact-1",
                "setup_failure_stage": "output_validation",
                "setup_failure_summary": ("SPEC_COMPILATION_FAILED: invalid spec path"),
                "raw_output_preview": '{"invalid": true}',
                "has_full_artifact": True,
            }
            if self.workflow:
                self.workflow.states[str(product.product_id)] = {
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_status": "failed",
                    "setup_error": "invalid spec path",
                    "setup_failure_artifact_id": "setup-artifact-1",
                    "setup_failure_stage": "output_validation",
                    "setup_failure_summary": (
                        "SPEC_COMPILATION_FAILED: invalid spec path"
                    ),
                    "setup_raw_output_preview": '{"invalid": true}',
                    "setup_has_full_artifact": True,
                    "setup_spec_file_path": spec_file,
                }
            return {
                "ok": False,
                "error": "SPEC_COMPILATION_FAILED: invalid spec path",
                "data": data,
            }

        product.compiled_authority_json = '{"ok": true}'
        if self.workflow:
            self.workflow.states[str(product.product_id)] = {
                "fsm_state": "VISION_INTERVIEW",
                "setup_status": "passed",
                "pending_spec_path": spec_file,
                "pending_spec_content": "SPEC",
                "compiled_authority_cached": '{"ok": true}',
            }
        return {
            "ok": True,
            "data": {
                "project_id": product.product_id,
                "name": product.name,
                "setup_status": "passed",
                "fsm_state": "VISION_INTERVIEW",
                "vision_auto_run": {
                    "attempted": True,
                    "success": True,
                    "is_complete": False,
                },
            },
        }

    def authority_compile(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_version_id: int,
        expected_spec_hash: str,
        expected_state: str,
        expected_setup_status: str,
        compiler_model: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, object]:
        """Mock guarded authority compilation."""
        del dry_run, dry_run_id, correlation_id
        self.calls.append(
            (
                "authority_compile",
                {
                    "project_id": project_id,
                    "spec_version_id": spec_version_id,
                    "expected_spec_hash": expected_spec_hash,
                    "expected_state": expected_state,
                    "expected_setup_status": expected_setup_status,
                    "compiler_model": compiler_model,
                    "idempotency_key": idempotency_key,
                    "changed_by": changed_by,
                },
            )
        )
        return self.results.get(
            "authority_compile",
            {
                "ok": True,
                "data": {"project_id": project_id},
                "warnings": [],
                "errors": [],
            },
        )

    def authority_feedback_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        pending_authority_id: int,
        expected_authority_fingerprint: str,
        feedback_file: str,
        idempotency_key: str,
        changed_by: str = "dashboard-ui",
        correlation_id: str | None = None,
    ) -> dict[str, object]:
        """Mock authority feedback recording."""
        self.calls.append(
            (
                "authority_feedback_record",
                {
                    "project_id": project_id,
                    "pending_authority_id": pending_authority_id,
                    "expected_authority_fingerprint": expected_authority_fingerprint,
                    "feedback_file": feedback_file,
                    "idempotency_key": idempotency_key,
                    "changed_by": changed_by,
                    "correlation_id": correlation_id,
                },
            )
        )
        return self.results.get(
            "authority_feedback_record",
            {
                "ok": True,
                "data": {
                    "project_id": project_id,
                    "feedback_attempt_id": "feedback-1",
                },
                "warnings": [],
                "errors": [],
            },
        )

    def authority_curate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        idempotency_key: str,
        spec_version_id: int | None = None,
        source_authority_id: int | None = None,
        expected_source_authority_fingerprint: str | None = None,
        feedback_attempt_id: str | None = None,
        recovery_mutation_event_id: int | None = None,
        expected_candidate_authority_id: int | None = None,
        expected_candidate_authority_fingerprint: str | None = None,
        max_iterations: int = 2,
        compiler_model: str | None = None,
        changed_by: str = "dashboard-ui",
        correlation_id: str | None = None,
    ) -> dict[str, object]:
        """Mock authority curation."""
        self.calls.append(
            (
                "authority_curate",
                {
                    "project_id": project_id,
                    "spec_version_id": spec_version_id,
                    "source_authority_id": source_authority_id,
                    "expected_source_authority_fingerprint": (
                        expected_source_authority_fingerprint
                    ),
                    "feedback_attempt_id": feedback_attempt_id,
                    "recovery_mutation_event_id": recovery_mutation_event_id,
                    "expected_candidate_authority_id": (
                        expected_candidate_authority_id
                    ),
                    "expected_candidate_authority_fingerprint": (
                        expected_candidate_authority_fingerprint
                    ),
                    "idempotency_key": idempotency_key,
                    "max_iterations": max_iterations,
                    "compiler_model": compiler_model,
                    "changed_by": changed_by,
                    "correlation_id": correlation_id,
                },
            )
        )
        return self.results.get(
            "authority_curate",
            {
                "ok": True,
                "data": {
                    "project_id": project_id,
                    "status": "authority_pending_review",
                },
                "warnings": [],
                "errors": [],
            },
        )

    def scope_extension_validate(
        self,
        *,
        project_id: int,
        spec_file: str,
        base_spec_version_id: int | None = None,
    ) -> dict[str, object]:
        """Capture a scope-extension validation request."""
        self.calls.append(
            (
                "scope_extension_validate",
                {
                    "project_id": project_id,
                    "spec_file": spec_file,
                    "base_spec_version_id": base_spec_version_id,
                },
            )
        )
        return self.results.get(
            "scope_extension_validate",
            {
                "ok": True,
                "data": {
                    "project_id": project_id,
                    "status": "project_scope_extension_valid",
                },
                "warnings": [],
                "errors": [],
            },
        )

    def scope_extension_start(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_file: str,
        base_spec_version_id: int,
        expected_state: str,
        idempotency_key: str,
        changed_by: str = "dashboard-agent",
    ) -> dict[str, object]:
        """Capture a guarded scope-extension start request."""
        self.calls.append(
            (
                "scope_extension_start",
                {
                    "project_id": project_id,
                    "spec_file": spec_file,
                    "base_spec_version_id": base_spec_version_id,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                    "changed_by": changed_by,
                },
            )
        )
        return self.results.get(
            "scope_extension_start",
            {
                "ok": True,
                "data": {
                    "project_id": project_id,
                    "status": "project_scope_extension_started",
                    "next_actions": [
                        {
                            "command": "agileforge authority compile",
                            "args": {"project_id": project_id},
                            "reason": "Compile authority for amended scope.",
                        }
                    ],
                },
                "warnings": [],
                "errors": [],
            },
        )

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, object]:
        """Return a review packet containing a dashboard review token."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "include_spec": include_spec,
                "output_format": output_format,
                "summary": {"omission_assessment": "complete"},
                "guard_tokens": {REVIEW_FIELD: AUTHORITY_REVIEW_FIXTURE},
            },
            "warnings": [],
            "errors": [],
        }

    def authority_accept(self, request: AuthorityAcceptRequest) -> dict[str, object]:
        """Capture an authority accept request."""
        self.accept_requests.append(request)
        return {
            "ok": True,
            "data": {
                "project_id": request.project_id,
                "setup_status": "passed",
                "fsm_state": "VISION_INTERVIEW",
            },
            "warnings": [],
            "errors": [],
        }

    def authority_reject(self, request: AuthorityRejectRequest) -> dict[str, object]:
        """Capture an authority reject request and keep setup locked."""
        self.accept_requests = []  # Clear to avoid cross-contamination if any
        self.reject_requests.append(request)
        if self.workflow is not None:
            self.workflow.update_session_status(
                str(request.project_id),
                {
                    "setup_status": "authority_rejected",
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_error": request.reason,
                },
            )
        return {
            "ok": True,
            "data": {
                "project_id": request.project_id,
                "setup_status": "authority_rejected",
                "fsm_state": "SETUP_REQUIRED",
                "reason": request.reason,
            },
            "warnings": [],
            "errors": [],
        }


def _install_fake_authority_application(
    monkeypatch: pytest.MonkeyPatch,
    app: FakeAuthorityApplication,
) -> None:
    """Patch API authority routes to use a fake application facade."""
    monkeypatch.setattr(
        api_module,
        "AgentWorkbenchApplication",
        lambda: app,
        raising=False,
    )


def test_get_dashboard_config_returns_setup_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the setup-first workflow state in dashboard config."""
    client, _, _ = _build_client(monkeypatch)

    response = client.get("/api/dashboard/config")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    steps = payload["data"]["workflow_steps"]

    assert steps[0]["id"] == "setup"
    assert steps[0]["states"] == ["SETUP_REQUIRED"]


def test_root_redirects_to_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect the root endpoint to the dashboard."""
    client, _, _ = _build_client(monkeypatch)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == HTTP_TEMP_REDIRECT
    assert response.headers["location"] == "/dashboard"


def test_create_project_requires_spec_file_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject project creation without a specification file path."""
    client, _, _ = _build_client(monkeypatch)

    response = client.post("/api/projects", json={"name": "Alpha"})
    assert response.status_code == HTTP_UNPROCESSABLE


def test_dashboard_project_create_remains_greenfield_only() -> None:
    """Keep dashboard project creation on the greenfield setup contract."""
    schema = api_module.CreateProjectRequest.model_json_schema()

    assert "name" in schema["properties"]
    assert "spec_file_path" in schema["properties"]
    assert "setup_mode" not in schema["properties"]
    assert "source_file" not in schema["properties"]
    assert "repo_path" not in schema["properties"]


def test_create_project_success_advances_to_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advance to vision interviewing after successful project setup."""
    client, _, workflow = _build_client(monkeypatch)

    response = client.post(
        "/api/projects",
        json={"name": "Project Alpha", "spec_file_path": __file__},
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["setup_status"] == "passed"
    assert payload["data"]["fsm_state"] == "VISION_INTERVIEW"
    assert payload["data"]["vision_auto_run"]["attempted"] is True
    assert payload["data"]["vision_auto_run"]["success"] is True
    assert payload["data"]["vision_auto_run"]["is_complete"] is False

    assert workflow.states["1"]["fsm_state"] == "VISION_INTERVIEW"


def test_create_project_returns_compile_required_without_pending_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard create should return immediately before authority compilation."""
    client, _repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    project_id = 10
    spec_version_id = 3
    spec_hash = "a" * 64
    fake_app.results["project_create"] = {
        "ok": True,
        "data": {
            "project_id": project_id,
            "name": "API Project",
            "setup_status": "authority_compile_required",
            "fsm_state": "SETUP_REQUIRED",
            "spec_hash": spec_hash,
            "spec_version_id": spec_version_id,
            "next_actions": [
                {
                    "command": "agileforge authority compile",
                    "args": {
                        "project_id": project_id,
                        "spec_version_id": spec_version_id,
                        "expected_spec_hash": spec_hash,
                        "expected_state": "SETUP_REQUIRED",
                        "expected_setup_status": "authority_compile_required",
                    },
                    "reason": "Compile pending authority before authority review.",
                }
            ],
        },
        "warnings": [],
        "errors": [],
    }
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        "/api/projects",
        json={"name": "API Project", "spec_file_path": "specs/spec.json"},
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["id"] == project_id
    assert payload["data"]["setup_status"] == "authority_compile_required"
    assert payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert payload["data"]["spec_hash"] == spec_hash
    assert payload["data"]["spec_version_id"] == spec_version_id
    assert payload["data"]["next_actions"][0]["command"] == (
        "agileforge authority compile"
    )
    assert fake_app.create_calls[0]["setup_mode"] == "greenfield"


def test_get_projects_uses_batch_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use batch workflow status lookup when listing projects."""
    client, repo, workflow = _build_client(monkeypatch)

    repo.products = [
        DummyProduct(
            product_id=1,
            name="Alpha",
            spec_file_path=__file__,
            compiled_authority_json='{"ok": true}',
        ),
        DummyProduct(product_id=2, name="Beta", description="Second project"),
    ]
    workflow.states = {
        "1": {"fsm_state": "VISION_INTERVIEW", "setup_status": "passed"},
        "2": {"fsm_state": "SETUP_REQUIRED", "setup_status": "failed"},
    }

    response = client.get("/api/projects")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert [item["id"] for item in payload["data"]] == [1, 2]
    assert payload["data"][0]["fsm_state"] == "VISION_INTERVIEW"
    assert payload["data"][1]["summary"] == "Second project"
    assert workflow.batch_calls == [["1", "2"]]
    assert workflow.single_calls == []


def test_scope_discovery_projects_sprint_runtime_primary_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard runtime projection exposes exhausted-scope discovery action."""
    project_id = 10

    def workflow_next(*, project_id: int) -> dict[str, object]:
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "status": "scope_discovery_challenge_artifact_missing",
                "next_actions": [
                    {
                        "command": (
                            "agileforge discovery challenge record "
                            "--project-id 10 "
                            "--artifact-file <challenge_artifact_file> "
                            "--idempotency-key <idempotency_key>"
                        ),
                        "status": "scope_discovery_challenge_artifact_missing",
                        "reason": (
                            "The current execution scope is exhausted; record a "
                            "grill-with-docs Challenge Artifact before drafting a "
                            "PRD."
                        ),
                    }
                ],
                "blocked_commands": [],
            },
            "warnings": [],
            "errors": [],
        }

    monkeypatch.setattr(
        api_module,
        "_workbench_application",
        lambda: SimpleNamespace(workflow_next=workflow_next),
    )

    payload = {
        "sprint_runtime": api_module._scope_extension_runtime_projection(project_id)
    }
    runtime = payload["sprint_runtime"]
    assert isinstance(runtime, dict)

    assert runtime["status"] == "scope_discovery_challenge_artifact_missing"
    assert runtime["primary_action"]["label"] == "Record Challenge Artifact"
    assert runtime["primary_action"]["command"].startswith(
        "agileforge discovery challenge record"
    )
    assert runtime["next_actions"] == [runtime["primary_action"]]
    assert runtime["scope_extension_actions"] == [
        runtime["scope_extension_primary_action"]
    ]


def test_create_project_returns_500_when_repository_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return 500 when the repository returns a product without an ID."""
    client, repo, _workflow = _build_client(monkeypatch)

    @dataclass
    class BrokenDummyProduct:
        """Broken product double that simulates a missing primary key."""

        product_id: int | None
        name: str
        description: str | None = None
        vision: str | None = None
        spec_file_path: str | None = None
        compiled_authority_json: str | None = None

    def create_without_id(
        name: str,
        description: str | None = None,
    ) -> BrokenDummyProduct:
        product = BrokenDummyProduct(
            product_id=None,
            name=name,
            description=description,
        )
        repo.products.append(cast("DummyProduct", product))
        return product

    monkeypatch.setattr(repo, "create", create_without_id)

    response = client.post(
        "/api/projects",
        json={"name": "Broken Project", "spec_file_path": __file__},
    )

    assert response.status_code == HTTP_SERVER_ERROR
    assert response.json()["detail"] == "Failed to create project"


def test_create_project_setup_fail_and_retry_same_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry setup on the same project after a setup-phase failure."""
    client, repo, _workflow = _build_client(monkeypatch)

    create_response = client.post(
        "/api/projects",
        json={"name": "Project Retry", "spec_file_path": "invalid/path.md"},
    )
    assert create_response.status_code == HTTP_OK

    create_payload = create_response.json()
    assert create_payload["data"]["setup_status"] == "failed"
    assert create_payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert create_payload["data"]["vision_auto_run"]["attempted"] is False

    product = repo.get_by_id(create_payload["data"]["id"])
    assert product is not None

    retry_response = client.post(
        f"/api/projects/{product.product_id}/setup/retry",
        json={
            "spec_file_path": (
                "benchmarks/authority-quality/todomvc/agileforge/gold-spec/spec.json"
            )
        },
    )
    assert retry_response.status_code == HTTP_OK

    retry_payload = retry_response.json()
    assert retry_payload["data"]["setup_status"] == "passed"
    assert retry_payload["data"]["fsm_state"] == "VISION_INTERVIEW"


def test_get_project_state_preserves_specific_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve setup failure metadata in the project-state endpoint."""
    client, _repo, _workflow = _build_client(monkeypatch)

    create_response = client.post(
        "/api/projects",
        json={"name": "Project Retry", "spec_file_path": "invalid/path.md"},
    )
    assert create_response.status_code == HTTP_OK

    project_id = create_response.json()["data"]["id"]

    state_response = client.get(f"/api/projects/{project_id}/state")
    assert state_response.status_code == HTTP_OK

    payload = state_response.json()
    assert payload["data"]["setup_status"] == "failed"
    assert payload["data"]["setup_error"] == "invalid spec path"
    assert payload["data"]["setup_failure_artifact_id"] == "setup-artifact-1"
    assert payload["data"]["setup_failure_stage"] == "output_validation"
    assert payload["data"]["setup_has_full_artifact"] is True


def test_project_state_preserves_authority_pending_review_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep pending authority review as setup-required, not failed setup."""
    client, repo, workflow = _build_client(monkeypatch)

    product = repo.create("Pending Authority")
    product.spec_file_path = __file__
    workflow.states[str(product.product_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "authority_pending_review",
    }

    response = client.get(f"/api/projects/{product.product_id}/state")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert payload["data"]["setup_status"] == "authority_pending_review"
    assert payload["data"]["setup_error"] is None
    assert payload["data"]["setup_failure_summary"] is None


def test_dashboard_authority_review_endpoint_returns_review_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the pending authority review token through the dashboard API."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.get(f"/api/projects/{product.product_id}/authority/review")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["guard_tokens"]["review_token"].startswith(
        "agileforge.authority_review.v1:sha256:"
    )


def test_dashboard_accept_requires_review_token_or_full_guard_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject dashboard accept requests without a token or full guards."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/accept",
        json={},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert fake_app.accept_requests == []
    assert (
        response.json()["detail"]["errors"][0]["code"] == "AUTHORITY_GUARD_INCOMPLETE"
    )


def test_dashboard_accept_rejects_fingerprint_only_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject authority-fingerprint-only dashboard decision guards."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/accept",
        json={"expected_authority_fingerprint": "sha256:test"},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert fake_app.accept_requests == []
    assert (
        response.json()["detail"]["errors"][0]["code"] == "AUTHORITY_GUARD_INCOMPLETE"
    )


def test_dashboard_accept_passes_candidate_scoped_incomplete_review_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard accept forwards candidate-specific override payloads."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/accept",
        json={
            REVIEW_FIELD: AUTHORITY_REVIEW_FIXTURE,
            "incomplete_review_overrides": [
                {
                    "candidate_id": "REQ-1",
                    "finding_code": "AUTHORITY_CANDIDATE_UNCOVERED",
                    "rationale": "Reviewed uncovered candidate.",
                }
            ],
        },
    )

    assert response.status_code == HTTP_OK
    assert len(fake_app.accept_requests) == 1
    assert fake_app.accept_requests[0].incomplete_review_overrides[0].candidate_id == (
        "REQ-1"
    )


def test_dashboard_accept_passes_broad_incomplete_review_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard accept forwards broad incomplete-review fields."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/accept",
        json={
            REVIEW_FIELD: AUTHORITY_REVIEW_FIXTURE,
            "allow_incomplete_review": True,
            "incomplete_review_rationale": "Reviewed manually.",
        },
    )

    assert response.status_code == HTTP_OK
    assert len(fake_app.accept_requests) == 1
    assert fake_app.accept_requests[0].allow_incomplete_review is True
    assert fake_app.accept_requests[0].incomplete_review_rationale == (
        "Reviewed manually."
    )


def test_dashboard_reject_records_reason_and_keeps_vision_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send rejection reason through the app facade and keep setup locked."""
    client, repo, workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication(workflow=workflow)
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")
    reason = "The generated invariant omits the audit trail requirement."

    response = client.post(
        f"/api/projects/{product.product_id}/authority/reject",
        json={
            REVIEW_FIELD: AUTHORITY_REVIEW_FIXTURE,
            "idempotency_key": "dashboard-reject-001",
            "reason": reason,
        },
    )

    assert response.status_code == HTTP_OK
    assert response.json()["data"]["reason"] == reason
    assert len(fake_app.reject_requests) == 1
    request = fake_app.reject_requests[0]
    assert request.reason == reason
    assert request.idempotency_key == "dashboard-reject-001"
    assert request.policy == "dashboard_manual"
    assert request.actor_mode == "dashboard-human"
    assert workflow.states[str(product.product_id)]["fsm_state"] == "SETUP_REQUIRED"
    assert (
        workflow.states[str(product.product_id)]["setup_status"] == "authority_rejected"
    )
    assert workflow.states[str(product.product_id)]["setup_error"] == reason


def test_dashboard_reject_requires_explicit_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject dashboard reject requests without caller idempotency."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/reject",
        json={
            REVIEW_FIELD: AUTHORITY_REVIEW_FIXTURE,
            "reason": "Spec needs revision.",
        },
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert fake_app.reject_requests == []
    assert (
        response.json()["detail"]["errors"][0]["code"] == "AUTHORITY_GUARD_INCOMPLETE"
    )
    assert response.json()["detail"]["errors"][0]["details"]["missing"] == [
        "idempotency_key"
    ]


def test_authority_compile_api_routes_guarded_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority compile API should mirror the CLI guarded mutation."""
    client, repo, _workflow = _build_client(monkeypatch)
    repo.products.append(DummyProduct(product_id=10, name="API Project"))
    fake_app = FakeAuthorityApplication()
    fake_app.results["authority_compile"] = {
        "ok": True,
        "data": {
            "project_id": 10,
            "spec_version_id": 3,
            "spec_hash": "a" * 64,
            "pending_authority_id": 99,
            "compiled_authority_id": 99,
            "setup_status": "authority_pending_review",
            "fsm_state": "SETUP_REQUIRED",
            "mutation_event_id": 123,
            "next_actions": [
                {
                    "command": "agileforge authority review",
                    "args": {"project_id": 10},
                    "reason": ("Review pending compiled authority before acceptance."),
                }
            ],
        },
        "warnings": [],
        "errors": [],
    }
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        "/api/projects/10/authority/compile",
        json={
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "compiler_model": "openrouter/openai/gpt-5.2",
            "idempotency_key": "authority-compile-api-001",
        },
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["setup_status"] == "authority_pending_review"
    assert fake_app.calls[-1] == (
        "authority_compile",
        {
            "project_id": 10,
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "compiler_model": "openrouter/openai/gpt-5.2",
            "idempotency_key": "authority-compile-api-001",
            "changed_by": "dashboard-ui",
        },
    )


def test_authority_feedback_record_api_routes_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority feedback API should mirror the CLI facade contract."""
    client, repo, _workflow = _build_client(monkeypatch)
    repo.products.append(DummyProduct(product_id=10, name="API Project"))
    fake_app = FakeAuthorityApplication()
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        "/api/projects/10/authority/feedback",
        json={
            "pending_authority_id": 99,
            "expected_authority_fingerprint": "sha256:abc",
            "feedback_file": "authority-feedback.json",
            "idempotency_key": "feedback-api-001",
            "changed_by": "dashboard-user",
            "correlation_id": "corr-feedback",
        },
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["feedback_attempt_id"] == "feedback-1"
    assert fake_app.calls[-1] == (
        "authority_feedback_record",
        {
            "project_id": 10,
            "pending_authority_id": 99,
            "expected_authority_fingerprint": "sha256:abc",
            "feedback_file": "authority-feedback.json",
            "idempotency_key": "feedback-api-001",
            "changed_by": "dashboard-user",
            "correlation_id": "corr-feedback",
        },
    )


def test_authority_curate_api_routes_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority curation API should mirror the CLI facade contract."""
    client, repo, _workflow = _build_client(monkeypatch)
    repo.products.append(DummyProduct(product_id=10, name="API Project"))
    fake_app = FakeAuthorityApplication()
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        "/api/projects/10/authority/curate",
        json={
            "spec_version_id": 3,
            "source_authority_id": 99,
            "expected_source_authority_fingerprint": "sha256:abc",
            "feedback_attempt_id": "feedback-1",
            "max_iterations": 2,
            "compiler_model": "openrouter/openai/gpt-5.2",
            "idempotency_key": "curate-api-001",
            "changed_by": "dashboard-user",
            "correlation_id": "corr-curate",
        },
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "authority_pending_review"
    assert fake_app.calls[-1] == (
        "authority_curate",
        {
            "project_id": 10,
            "spec_version_id": 3,
            "source_authority_id": 99,
            "expected_source_authority_fingerprint": "sha256:abc",
            "feedback_attempt_id": "feedback-1",
            "recovery_mutation_event_id": None,
            "expected_candidate_authority_id": None,
            "expected_candidate_authority_fingerprint": None,
            "idempotency_key": "curate-api-001",
            "max_iterations": 2,
            "compiler_model": "openrouter/openai/gpt-5.2",
            "changed_by": "dashboard-user",
            "correlation_id": "corr-curate",
        },
    )


def test_authority_curate_api_routes_recovery_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority curation API routes explicit recovery requests."""
    client, repo, _workflow = _build_client(monkeypatch)
    repo.products.append(DummyProduct(product_id=10, name="API Project"))
    fake_app = FakeAuthorityApplication()
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        "/api/projects/10/authority/curate",
        json={
            "recovery_mutation_event_id": 647,
            "expected_candidate_authority_id": 7,
            "expected_candidate_authority_fingerprint": "sha256:" + ("a" * 64),
            "idempotency_key": "recover-api-001",
            "changed_by": "dashboard-user",
            "correlation_id": "corr-recover",
        },
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert fake_app.calls[-1] == (
        "authority_curate",
        {
            "project_id": 10,
            "spec_version_id": None,
            "source_authority_id": None,
            "expected_source_authority_fingerprint": None,
            "feedback_attempt_id": None,
            "recovery_mutation_event_id": 647,
            "expected_candidate_authority_id": 7,
            "expected_candidate_authority_fingerprint": "sha256:" + ("a" * 64),
            "idempotency_key": "recover-api-001",
            "max_iterations": 2,
            "compiler_model": None,
            "changed_by": "dashboard-user",
            "correlation_id": "corr-recover",
        },
    )


def test_scope_extension_validate_api_routes_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope-extension validate should mirror the CLI facade contract."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id = 10
    repo.products.append(DummyProduct(product_id=project_id, name="API Project"))
    fake_app = FakeAuthorityApplication()
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        f"/api/projects/{project_id}/scope-extension/validate",
        json={"spec_file": "specs/amended.json", "base_spec_version_id": 3},
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "project_scope_extension_valid"
    assert fake_app.calls[-1] == (
        "scope_extension_validate",
        {
            "project_id": project_id,
            "spec_file": "specs/amended.json",
            "base_spec_version_id": 3,
        },
    )


def test_scope_extension_validate_api_allows_optional_base_spec_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation can infer the base spec version when omitted."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id = 10
    repo.products.append(DummyProduct(product_id=project_id, name="API Project"))
    fake_app = FakeAuthorityApplication()
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        f"/api/projects/{project_id}/scope-extension/validate",
        json={"spec_file": "specs/amended.json"},
    )

    assert response.status_code == HTTP_OK
    assert fake_app.calls[-1][1]["base_spec_version_id"] is None


def test_scope_extension_start_api_routes_request_with_next_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope-extension start should forward guarded mutation fields."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id = 10
    repo.products.append(DummyProduct(product_id=project_id, name="API Project"))
    fake_app = FakeAuthorityApplication()
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        f"/api/projects/{project_id}/scope-extension/start",
        json={
            "spec_file": "specs/amended.json",
            "base_spec_version_id": 3,
            "expected_state": "SPRINT_COMPLETE",
            "idempotency_key": "scope-extension-api-001",
            "changed_by": "dashboard-human",
        },
    )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "project_scope_extension_started"
    assert payload["data"]["next_actions"][0]["command"] == (
        "agileforge authority compile"
    )
    assert fake_app.calls[-1] == (
        "scope_extension_start",
        {
            "project_id": project_id,
            "spec_file": "specs/amended.json",
            "base_spec_version_id": 3,
            "expected_state": "SPRINT_COMPLETE",
            "idempotency_key": "scope-extension-api-001",
            "changed_by": "dashboard-human",
        },
    )


def test_scope_extension_start_api_rejects_blank_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only idempotency keys should fail at the request boundary."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id = 10
    repo.products.append(DummyProduct(product_id=project_id, name="API Project"))
    fake_app = FakeAuthorityApplication()
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        f"/api/projects/{project_id}/scope-extension/start",
        json={
            "spec_file": "specs/amended.json",
            "base_spec_version_id": 3,
            "expected_state": "SPRINT_COMPLETE",
            "idempotency_key": "   ",
        },
    )

    assert response.status_code == HTTP_UNPROCESSABLE
    assert fake_app.calls == []


def test_scope_extension_api_forbids_legacy_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed or unrelated scope-extension fields should fail validation."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id = 10
    repo.products.append(DummyProduct(product_id=project_id, name="API Project"))

    validate_response = client.post(
        f"/api/projects/{project_id}/scope-extension/validate",
        json={
            "spec_file": "specs/amended.json",
            "sprint_duration_days": 14,
        },
    )
    start_response = client.post(
        f"/api/projects/{project_id}/scope-extension/start",
        json={
            "spec_file": "specs/amended.json",
            "base_spec_version_id": 3,
            "expected_state": "SPRINT_COMPLETE",
            "idempotency_key": "scope-extension-api-001",
            "legacy_field": "legacy",
        },
    )

    assert validate_response.status_code == HTTP_UNPROCESSABLE
    assert start_response.status_code == HTTP_UNPROCESSABLE


def test_scope_extension_start_api_failure_uses_dashboard_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope-extension start failures should surface as structured errors."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id = 10
    repo.products.append(DummyProduct(product_id=project_id, name="API Project"))
    fake_app = FakeAuthorityApplication()
    result: dict[str, object] = {
        "ok": False,
        "data": {
            "project_id": project_id,
            "expected_state": "SPRINT_COMPLETE",
            "actual_state": "STORY_REVIEW",
        },
        "errors": [
            {
                "code": "SCOPE_EXTENSION_EXPECTED_STATE_STALE",
                "message": "Scope extension expected state is stale.",
            }
        ],
        "warnings": [
            {
                "code": "SCOPE_EXTENSION_REFRESH_REQUIRED",
                "message": "Refresh workflow state and retry.",
            }
        ],
    }
    fake_app.results["scope_extension_start"] = result
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        f"/api/projects/{project_id}/scope-extension/start",
        json={
            "spec_file": "specs/amended.json",
            "base_spec_version_id": 3,
            "expected_state": "SPRINT_COMPLETE",
            "idempotency_key": "scope-extension-api-001",
        },
    )

    assert response.status_code == HTTP_BAD_REQUEST
    detail = response.json()["detail"]
    assert detail["status"] == "error"
    assert detail["data"] == result["data"]
    assert detail["errors"] == result["errors"]
    assert detail["warnings"] == result["warnings"]


def test_authority_compile_api_failure_uses_dashboard_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority compile failures should use the shared dashboard error envelope."""
    client, repo, _workflow = _build_client(monkeypatch)
    project_id = 10
    repo.products.append(DummyProduct(product_id=project_id, name="API Project"))
    fake_app = FakeAuthorityApplication()
    result: dict[str, object] = {
        "ok": False,
        "data": {
            "project_id": project_id,
            "setup_status": "authority_compile_required",
        },
        "errors": [
            {
                "code": "AUTHORITY_COMPILE_GUARD_STALE",
                "message": "Authority compile guard is stale.",
            }
        ],
        "warnings": [
            {
                "code": "AUTHORITY_COMPILE_RETRY_READY",
                "message": "Refresh compile metadata and retry.",
            }
        ],
    }
    fake_app.results["authority_compile"] = result
    monkeypatch.setattr(api_module, "_workbench_application", lambda: fake_app)

    response = client.post(
        f"/api/projects/{project_id}/authority/compile",
        json={
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "authority-compile-api-001",
        },
    )

    assert response.status_code == HTTP_BAD_REQUEST
    detail = response.json()["detail"]
    assert detail["status"] == "error"
    assert detail["data"] == result["data"]
    assert detail["errors"] == result["errors"]
    assert detail["warnings"] == result["warnings"]


def test_authority_compile_api_forbids_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed or unrelated fields should fail validation instead of being ignored."""
    client, _repo, _workflow = _build_client(monkeypatch)

    response = client.post(
        "/api/projects/10/authority/compile",
        json={
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "authority-compile-api-001",
            "spec_file_path": "specs/spec.json",
        },
    )

    assert response.status_code == HTTP_UNPROCESSABLE


def test_authority_compile_api_rejects_misspelled_compiler_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler model must use the exact public field name."""
    client, repo, _workflow = _build_client(monkeypatch)
    repo.products.append(DummyProduct(product_id=10, name="API Project"))

    response = client.post(
        "/api/projects/10/authority/compile",
        json={
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "authority-compile-api-001",
            "compilerModel": "openrouter/openai/gpt-5.2",
        },
    )

    assert response.status_code == HTTP_UNPROCESSABLE


def test_dashboard_reject_empty_reason_returns_request_boundary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an empty authority rejection reason before app service dispatch."""
    client, repo, _workflow = _build_client(monkeypatch)
    fake_app = FakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)
    product = repo.create("Pending Authority")

    response = client.post(
        f"/api/projects/{product.product_id}/authority/reject",
        json={
            REVIEW_FIELD: AUTHORITY_REVIEW_FIXTURE,
            "reason": "",
        },
    )

    assert response.status_code == HTTP_UNPROCESSABLE
    assert fake_app.reject_requests == []
    errors = response.json()["detail"]
    assert errors[0]["loc"] == ["body", "reason"]
    assert errors[0]["type"] == "string_too_short"


def test_dashboard_pending_review_copy_is_not_project_setup_required() -> None:
    """Keep the dashboard pending review panel copy authority-specific."""
    html = Path("frontend/project.html").read_text()
    marker = 'id="authority-review-card"'

    assert marker in html
    review_card = html[html.index(marker) : html.index(marker) + 1200]
    assert "Pending Authority Review" in review_card
    assert "Project Setup Required" not in review_card


def test_dashboard_sprint_planning_ui_uses_point_capacity_contract() -> None:
    """Keep Sprint planning UI aligned with point capacity, not calendar heuristics."""
    html = Path("frontend/project.html").read_text()
    js = Path("frontend/project.js").read_text()

    assert 'id="sprint-velocity"' not in html
    assert 'id="sprint-duration"' not in html
    assert "Velocity Assumption" not in html
    assert "Sprint Duration" not in html
    assert "sprint-velocity" not in js
    assert "sprint-duration" not in js
    assert "SPRINT_VELOCITY_LIMITS" not in js
    assert "team_velocity_assumption" not in js
    assert "sprint_duration_days" not in js
    assert "Max Story Points" in html
    assert "project metrics recommendation" in html


def test_dashboard_sprint_save_uses_guarded_contract_without_start_date() -> None:
    """Keep Sprint save UI aligned with guarded API save contract."""
    html = Path("frontend/project.html").read_text()
    js = Path("frontend/project.js").read_text()

    assert 'id="sprint-start-date"' not in html
    assert "Sprint Start Date" not in html
    assert "sprint-start-date" not in js
    assert "sprint_start_date" not in js
    assert "attempt_id" in js
    assert "expected_artifact_fingerprint" in js
    assert "expected_state: 'SPRINT_DRAFT'" in js
    assert "idempotency_key" in js


def test_dashboard_sprint_save_reuses_idempotency_key_for_current_draft() -> None:
    """Keep retry clicks idempotent for the same reviewed Sprint draft."""
    js = Path("frontend/project.js").read_text()

    assert "let latestSprintSaveIdempotencyKey = null;" in js
    assert "function sprintSaveIdempotencyKey()" in js
    assert "idempotency_key: sprintSaveIdempotencyKey()" in js
    assert "latestSprintSaveIdempotencyKey = null;" in js
    save_payload_index = js.index("idempotency_key: sprintSaveIdempotencyKey()")
    assert "Date.now()" not in js[save_payload_index : save_payload_index + 120]


def test_state_forces_setup_required_when_product_missing_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force setup-required when a stored product has no persisted spec."""
    client, repo, workflow = _build_client(monkeypatch)

    product = repo.create("Legacy")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "VISION_REVIEW",
        "setup_status": "passed",
    }

    response = client.get(f"/api/projects/{product.product_id}/state")
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert payload["data"]["setup_status"] == "failed"


def test_create_project_auto_vision_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record vision auto-run failure metadata during project creation."""
    client, _, workflow = _build_client(monkeypatch)

    async def failing_auto_vision(
        state: dict[str, object],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, object]:
        del project_id
        return {
            "success": False,
            "input_context": {
                "user_raw_text": user_input or "",
                "prior_vision_state": "NO_HISTORY",
                "specification_content": state.get("pending_spec_content", "SPEC"),
                "compiled_authority": state.get(
                    "compiled_authority_cached", '{"ok": true}'
                ),
            },
            "output_artifact": {
                "error": "VISION_GENERATION_FAILED",
                "message": "provider error",
                "is_complete": False,
                "clarifying_questions": [],
            },
            "is_complete": None,
            "error": "provider error",
            "failure_artifact_id": "vision-auto-failure",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider error",
            "raw_output_preview": '{"partial": true}',
            "has_full_artifact": True,
        }

    monkeypatch.setattr(api_module, "run_vision_agent_from_state", failing_auto_vision)

    response = client.post(
        "/api/projects",
        json={"name": "Project Auto Fail", "spec_file_path": __file__},
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["data"]["setup_status"] == "passed"
    assert payload["data"]["fsm_state"] == "VISION_INTERVIEW"
    assert payload["data"]["vision_auto_run"]["attempted"] is True
    assert payload["data"]["vision_auto_run"]["success"] is False
    assert payload["data"]["vision_auto_run"]["is_complete"] is None
    assert (
        payload["data"]["vision_auto_run"]["failure_artifact_id"]
        == "vision-auto-failure"
    )

    history = workflow.states["1"]["vision_attempts"]
    assert isinstance(history, list)
    assert len(history) == 1
    first_attempt = history[0]
    assert isinstance(first_attempt, dict)
    first_attempt = cast("dict[str, object]", first_attempt)
    assert first_attempt["trigger"] == "auto_setup_transition"
    assert first_attempt["is_complete"] is False
    assert first_attempt["failure_artifact_id"] == "vision-auto-failure"


def test_create_project_setup_failure_exposes_failure_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose setup failure metadata on the project-creation response."""
    client, _, workflow = _build_client(monkeypatch)

    response = client.post(
        "/api/projects",
        json={"name": "Project Retry", "spec_file_path": "invalid/path.md"},
    )
    assert response.status_code == HTTP_OK

    payload = response.json()
    assert payload["data"]["setup_status"] == "failed"
    assert payload["data"]["failure_artifact_id"] == "setup-artifact-1"
    assert payload["data"]["failure_stage"] == "output_validation"
    assert payload["data"]["has_full_artifact"] is True
    assert workflow.states["1"]["setup_failure_artifact_id"] == "setup-artifact-1"


def test_get_project_authority_review_post_accept_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback to post-accept rendering when AUTHORITY_NOT_PENDING is returned."""
    client, repo, _workflow = _build_client(monkeypatch)
    product = repo.create("Accepted Product")
    product.spec_file_path = "specs/cartola/spec.json"
    product.compiled_authority_json = "{}"

    class FallbackFakeAuthorityApplication(FakeAuthorityApplication):
        def authority_review(
            self,
            *,
            project_id: int,
            include_spec: str = "auto",
            output_format: str = "json",
        ) -> dict[str, object]:
            _ = (project_id, include_spec, output_format)
            return {
                "ok": False,
                "errors": [{"code": "AUTHORITY_NOT_PENDING"}],
            }

    fake_app = FallbackFakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)

    selection = _AuthoritySelection(
        specs=[],
        latest_spec=None,
        accepted=None,
        rejected=None,
        accepted_spec=cast("Any", object()),
        authority=cast("Any", object()),
        pending_authority=None,
    )

    monkeypatch.setattr(
        api_module, "_load_authority_selection", lambda *_args, **_kwargs: selection
    )

    snapshot = AuthorityReviewSnapshot(
        schema="agileforge.authority_review.v1",
        project_id=product.product_id,
        project_name=product.name,
        fsm_state="VISION_INTERVIEW",
        setup_status="passed",
        spec_version_id=1,
        content_ref="ref",
        resolved_spec_path="path",
        source_spec_hash="hash",
        disk_status="ok",
        disk_spec_hash="hash",
        size_bytes=100,
        review_source_limit_bytes=1000,
        source_outline=[],
        source_units=[],
        coverage_summary={"omission_assessment": "complete"},
        coverage_summary_fingerprint="fp",
        coverage_diagnostics=[],
        excerpt="excerpt",
        content_included=True,
        content_truncated=False,
        source_content="spec source content",
        source_content_sha256="sha",
        structured_spec_snapshot=None,
        pending_authority_id=1,
        pending_spec_version_id=1,
        authority_fingerprint="fingerprint",
        compiler_version="1.0",
        prompt_hash="hash",
        compiled_at="date",
        artifact={
            "invariants": [],
            "gaps": [],
            "assumptions": [],
            "rejected_features": [],
            "eligible_feature_rules": [],
            "domain": "test",
            "scope_themes": [],
        },
        ir_provenance="provenance",
        review_findings=[],
        ir_packet_limits={},
        authority_mappings=[],
        ir_coverage_summary={},
        omission_assessment="complete",
    )

    monkeypatch.setattr(
        api_module,
        "build_authority_review_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )

    response = client.get(f"/api/projects/{product.product_id}/authority/review")
    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["post_accept"] is True
    assert payload["data"]["project"]["setup_status"] == "complete"
    assert payload["data"]["spec"]["source_content"] == "spec source content"
    assert payload["data"]["spec"]["content_included"] is True
    assert payload["data"]["spec"]["content_truncated"] is False
    assert payload["data"]["pending_authority"]["artifact"]["domain"] == "test"
    assert payload["data"]["pending_authority"]["review_findings"] == []
    assert payload["data"]["pending_authority"]["authority_fingerprint"] == (
        "fingerprint"
    )


def test_get_project_authority_review_rejects_legacy_post_accept_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard authority readers must fail closed for legacy artifacts."""
    client, repo, _workflow = _build_client(monkeypatch)
    product = repo.create("Accepted Legacy Product")
    product.spec_file_path = "specs/cartola/spec.json"
    product.compiled_authority_json = "{}"

    class FallbackFakeAuthorityApplication(FakeAuthorityApplication):
        def authority_review(
            self,
            *,
            project_id: int,
            include_spec: str = "auto",
            output_format: str = "json",
        ) -> dict[str, object]:
            _ = (project_id, include_spec, output_format)
            return {
                "ok": False,
                "errors": [{"code": "AUTHORITY_NOT_PENDING"}],
            }

    fake_app = FallbackFakeAuthorityApplication()
    _install_fake_authority_application(monkeypatch, fake_app)

    selection = _AuthoritySelection(
        specs=[],
        latest_spec=None,
        accepted=None,
        rejected=None,
        accepted_spec=cast("SpecRegistry", SimpleNamespace(spec_version_id=9)),
        authority=cast(
            "CompiledSpecAuthority",
            SimpleNamespace(compiled_artifact_json="{}"),
        ),
        pending_authority=None,
    )

    monkeypatch.setattr(
        api_module, "_load_authority_selection", lambda *_args, **_kwargs: selection
    )
    monkeypatch.setattr(
        api_module,
        "build_authority_review_snapshot",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        api_module,
        "_render_review_packet",
        lambda _snapshot: {
            "project": {},
            "spec": {},
            "pending_authority": {},
        },
    )

    response = client.get(f"/api/projects/{product.product_id}/authority/review")

    assert response.status_code == HTTP_CONFLICT
    payload = response.json()["detail"]
    error = payload["errors"][0]
    assert error["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert error["message"] == "Compiled authority artifact schema is unsupported."
    assert error["details"] == {
        "project_id": product.product_id,
        "spec_version_id": 9,
        "observed_schema_version": None,
        "required_schema_version": "agileforge.compiled_authority.v2",
    }
    assert error["remediation"] == [
        (
            "Run agileforge authority regenerate "
            f"--project-id {product.product_id} "
            "--spec-version-id 9 "
            "--idempotency-key <new-key>."
        )
    ]


@pytest.mark.parametrize(
    ("endpoint", "service_attr", "body"),
    [
        (
            "/api/projects/{project_id}/vision/generate",
            "generate_vision_draft_service",
            {"user_input": "draft vision"},
        ),
        (
            "/api/projects/{project_id}/backlog/generate",
            "generate_backlog_draft_service",
            {"user_input": "draft backlog"},
        ),
        (
            "/api/projects/{project_id}/roadmap/generate",
            "generate_roadmap_draft_service",
            {"user_input": "draft roadmap"},
        ),
        (
            "/api/projects/{project_id}/story/generate"
            "?parent_requirement=Seed%20backlog%20item",
            "generate_story_draft_service",
            {"user_input": "draft story"},
        ),
        (
            "/api/projects/{project_id}/sprint/generate",
            "generate_sprint_plan_service",
            {"user_input": "draft sprint"},
        ),
    ],
)
def test_phase_generate_blocks_legacy_cached_authority(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    service_attr: str,
    body: dict[str, object],
) -> None:
    """Phase generation must not start from a legacy cached authority artifact."""
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Legacy Phase Project")
    product.spec_file_path = __file__
    product.compiled_authority_json = LEGACY_COMPILED_AUTHORITY_JSON
    workflow.states[str(product.product_id)] = {
        "fsm_state": "BACKLOG_READY",
        "latest_spec_version_id": 9,
        "compiled_authority_cached": LEGACY_COMPILED_AUTHORITY_JSON,
    }
    service_calls: list[str] = []

    async def unexpected_service_call(*_args: object, **_kwargs: object) -> object:
        service_calls.append(service_attr)
        msg = f"{service_attr} should not run with unsupported authority"
        raise AssertionError(msg)

    monkeypatch.setattr(api_module, service_attr, unexpected_service_call)

    response = client.post(
        endpoint.format(project_id=product.product_id),
        json=body,
    )

    assert response.status_code == HTTP_CONFLICT
    assert service_calls == []
    error = response.json()["detail"]["errors"][0]
    assert error["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert error["message"] == "Compiled authority artifact schema is unsupported."
    assert error["details"] == {
        "project_id": product.product_id,
        "spec_version_id": 9,
        "observed_schema_version": None,
        "required_schema_version": "agileforge.compiled_authority.v2",
    }
    assert error["remediation"] == [
        (
            "Run agileforge authority regenerate "
            f"--project-id {product.product_id} "
            "--spec-version-id 9 "
            "--idempotency-key <new-key>."
        )
    ]


def test_retry_setup_nonexistent_or_invalid_spec_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test setup retry with a missing spec file returning structured failure."""
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Retry Missing Spec Product")

    response = client.post(
        f"/api/projects/{product.product_id}/setup/retry",
        json={"spec_file_path": "nonexistent_spec.json"},
    )
    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["setup_status"] == "failed"

    # Assert structured workbench validation errors are preserved
    assert "[SPEC_FILE_NOT_FOUND]" in payload["data"]["setup_error"]
    assert "Specification file not found" in payload["data"]["setup_error"]
    assert "Please check if the file exists" in payload["data"]["setup_error"]
    assert "[SPEC_FILE_NOT_FOUND]" in payload["data"]["failure_summary"]

    assert payload["errors"] == [
        {
            "code": "SPEC_FILE_NOT_FOUND",
            "message": "Specification file not found at path nonexistent_spec.json",
            "remediation": ["Please check if the file exists."],
        }
    ]
    assert payload["data"]["errors"] == payload["errors"]
    assert payload["warnings"] == []
    assert payload["data"]["warnings"] == []

    # Assert failed setup state is persisted in session state
    session_id = str(product.product_id)
    assert session_id in workflow.states
    saved_state = workflow.states[session_id]
    assert saved_state["setup_status"] == "failed"
    assert saved_state["fsm_state"] == "SETUP_REQUIRED"
    assert isinstance(saved_state["setup_error"], str)
    assert "[SPEC_FILE_NOT_FOUND]" in saved_state["setup_error"]
    assert saved_state["errors"] == payload["errors"]


def test_ui_retry_calls_facade_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate UI setup retry telemetry: idempotency key shape & changed_by."""
    client, repo, _workflow = _build_client(monkeypatch)
    product = repo.create("Telemetry Retry Product")

    app_double = api_module.AgentWorkbenchApplication()
    assert isinstance(app_double, FakeAuthorityApplication)
    assert len(app_double.retry_calls) == 0

    response = client.post(
        f"/api/projects/{product.product_id}/setup/retry",
        json={"spec_file_path": "specs/cartola/spec.json"},
    )
    assert response.status_code == HTTP_OK
    assert len(app_double.retry_calls) == 1

    call_params = app_double.retry_calls[0]
    assert call_params["changed_by"] == "dashboard-ui"
    assert call_params["idempotency_key"] is not None
    assert call_params["idempotency_key"].startswith("ui-retry-")


def test_ui_create_calls_facade_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate UI project creation telemetry: idempotency key & changed_by."""
    client, _repo, _workflow = _build_client(monkeypatch)

    app_double = api_module.AgentWorkbenchApplication()
    assert isinstance(app_double, FakeAuthorityApplication)
    assert len(app_double.create_calls) == 0

    response = client.post(
        "/api/projects",
        json={
            "name": "Telemetry Create Project",
            "spec_file_path": "specs/cartola/spec.json",
        },
    )
    assert response.status_code == HTTP_OK
    assert len(app_double.create_calls) == 1

    call_params = app_double.create_calls[0]
    assert call_params["changed_by"] == "dashboard-ui"
    assert call_params["idempotency_key"] is not None
    assert call_params["idempotency_key"].startswith("ui-create-")


def test_build_story_compliance_boundaries_ignores_non_success_loader_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story compliance boundaries should treat unreadable artifacts as absent."""
    monkeypatch.setattr(
        api_module,
        "load_compiled_artifact",
        lambda _authority: CompiledArtifactLoadResult(
            status="schema_invalid",
            message="invalid",
        ),
    )

    result = api_module._build_story_compliance_boundaries(
        authority=cast("CompiledSpecAuthority", object()),
        evidence=cast(
            "ValidationEvidence",
            SimpleNamespace(finding_invariant_ids=["INV-1"]),
        ),
    )

    assert result == []


def test_build_task_hard_constraints_ignores_non_success_loader_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task hard constraints should treat unreadable artifacts as absent."""
    monkeypatch.setattr(
        api_module,
        "load_compiled_artifact",
        lambda _authority: CompiledArtifactLoadResult(
            status="schema_invalid",
            message="invalid",
        ),
    )

    result = api_module._build_task_hard_constraints(
        authority=cast("CompiledSpecAuthority", object()),
        task_metadata=cast(
            "TaskMetadata",
            SimpleNamespace(relevant_invariant_ids=["INV-1"]),
        ),
    )

    assert result == []


def test_load_packet_story_context_marks_unreadable_authority_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packet context should only report authority available when loader succeeds."""

    class _ExecResult:
        def __init__(self, value: object) -> None:
            self._value = value

        def first(self) -> object:
            return self._value

    class _Session:
        def __init__(self, values: list[object]) -> None:
            self._values = list(values)

        def exec(self, _query: object) -> _ExecResult:
            return _ExecResult(self._values.pop(0))

    product = SimpleNamespace(
        product_id=1,
        updated_at=None,
        name="Product",
        vision=None,
    )
    story = SimpleNamespace(
        story_id=7,
        product_id=1,
        product=product,
        tasks=[],
        validation_evidence=None,
        accepted_spec_version_id=11,
        updated_at=None,
        ac_updated_at=None,
        acceptance_criteria=None,
        title="Story",
        persona=None,
        story_description=None,
        status=SimpleNamespace(value="draft"),
        story_points=None,
        rank=None,
        source_requirement=None,
    )
    sprint = SimpleNamespace(
        product_id=1,
        sprint_id=2,
        team=None,
        updated_at=None,
        status=SimpleNamespace(value="planned"),
        started_at=None,
        start_date=None,
        end_date=None,
        team_id=None,
        goal=None,
    )
    sprint_story = SimpleNamespace(added_at=None)

    monkeypatch.setattr(api_module, "_load_validation_evidence", lambda _raw: None)
    monkeypatch.setattr(api_module, "compute_story_input_hash", lambda _story: "hash")
    monkeypatch.setattr(api_module, "_load_pinned_authority", lambda *_args: object())
    monkeypatch.setattr(
        api_module,
        "load_compiled_artifact",
        lambda _authority: CompiledArtifactLoadResult(
            status="missing",
            message="missing",
        ),
    )

    context = api_module._load_packet_story_context(
        cast("Session", _Session([story, sprint, sprint_story])),
        project_id=1,
        sprint_id=2,
        story_id=7,
    )

    assert context is not None
    assert context.spec_binding_status == "pinned"
    assert context.authority_status == "missing"
