"""Project setup mutation runner for CLI project creation."""

# ruff: noqa: ANN401, ARG001, C901, D107, E501, EM101, EM102, PLR0911, PLR0912, PLR0913, PLR2004, SIM300, TRY003, TC002, TC003

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from models.agent_workbench import CliMutationLedger
from models.core import Product
from models.db import ensure_business_db_ready
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    DEFAULT_LEASE_SECONDS,
    IDEMPOTENCY_KEY_REUSED,
    MUTATION_IN_PROGRESS,
    MUTATION_RECOVERY_REQUIRED,
    MUTATION_RESUME_CONFLICT,
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
    _completed_steps,
    _db_datetime,
    _json_dump,
)
from services.agent_workbench.project_setup_fingerprints import (
    PROJECT_SETUP_RETRY_COMMAND,
    setup_retry_context_fingerprint,
    setup_spec_hash,
)
from services.specs.pending_authority_service import (
    compile_pending_authority_for_project,
    ensure_pending_spec_version_for_project,
)
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)

if TYPE_CHECKING:
    from services.workflow import WorkflowService

PROJECT_CREATE_COMMAND = "agileforge project create"
PROJECT_AUTHORITY_COMPILE_COMMAND = "agileforge authority compile"
PROJECT_ALREADY_EXISTS = "PROJECT_ALREADY_EXISTS"
AUTHORITY_COMPILE_FAILED = "authority_compile_failed"
AUTHORITY_COMPILE_REQUIRED = "authority_compile_required"
AUTHORITY_PENDING_REVIEW = "authority_pending_review"
MUTATION_RECOVERY_INVALID = "MUTATION_RECOVERY_INVALID"
SPEC_COMPILE_FAILED = "SPEC_COMPILE_FAILED"
WORKFLOW_SESSION_FAILED = "WORKFLOW_SESSION_FAILED"
AUTHORITY_COMPILE_FAILURE_FIELDS = (
    "setup_failure_stage",
    "setup_failure_summary",
    "setup_failure_artifact_id",
    "failure_artifact_stage",
    "setup_failure_first_error",
    "raw_output_preview",
    "has_full_artifact",
    "setup_failure_blocking_gaps",
)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_LEDGER_MUTATION_EVENT_ID: Any = CliMutationLedger.mutation_event_id
_LEDGER_COMMAND: Any = CliMutationLedger.command
_LEDGER_IDEMPOTENCY_KEY: Any = CliMutationLedger.idempotency_key
_LEDGER_STATUS: Any = CliMutationLedger.status
_LEDGER_LEASE_OWNER: Any = CliMutationLedger.lease_owner
_LEDGER_LEASE_EXPIRES_AT: Any = CliMutationLedger.lease_expires_at
_SPEC_PRODUCT_ID: Any = SpecRegistry.product_id
_SPEC_VERSION_ID: Any = SpecRegistry.spec_version_id


class ProjectCreateRequest(BaseModel):
    """Validated request for `agileforge project create`."""

    name: str = Field(min_length=1)
    spec_file: str = Field(min_length=1)
    idempotency_key: str | None = None
    dry_run: bool = False
    dry_run_id: str | None = None
    correlation_id: str | None = None
    changed_by: str = "cli-agent"

    @model_validator(mode="after")
    def _validate_mutation_keys(self) -> ProjectCreateRequest:
        _validate_key_mode(
            dry_run=self.dry_run,
            idempotency_key=self.idempotency_key,
            dry_run_id=self.dry_run_id,
        )
        return self


class ProjectSetupRetryRequest(BaseModel):
    """Validated request for `agileforge project setup retry`."""

    project_id: int
    spec_file: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    expected_context_fingerprint: str = Field(min_length=1)
    recovery_mutation_event_id: int | None = None
    idempotency_key: str | None = None
    dry_run: bool = False
    dry_run_id: str | None = None
    correlation_id: str | None = None
    changed_by: str = "cli-agent"

    @model_validator(mode="after")
    def _validate_mutation_keys(self) -> ProjectSetupRetryRequest:
        _validate_key_mode(
            dry_run=self.dry_run,
            idempotency_key=self.idempotency_key,
            dry_run_id=self.dry_run_id,
        )
        return self

    def normalized_request_hash(self) -> str:
        """Return a stable hash including stale-guard inputs."""
        return canonical_hash(
            {
                "command": PROJECT_SETUP_RETRY_COMMAND,
                "project_id": self.project_id,
                "spec_file": self.spec_file,
                "expected_state": self.expected_state,
                "expected_context_fingerprint": self.expected_context_fingerprint,
                "recovery_mutation_event_id": self.recovery_mutation_event_id,
                "changed_by": self.changed_by,
            }
        )


class AuthorityCompileRequest(BaseModel):
    """Validated request for `agileforge authority compile`."""

    project_id: int
    spec_version_id: int
    expected_spec_hash: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    expected_setup_status: str = Field(min_length=1)
    idempotency_key: str | None = None
    dry_run: bool = False
    dry_run_id: str | None = None
    correlation_id: str | None = None
    changed_by: str = "cli-agent"

    @model_validator(mode="after")
    def _validate_mutation_keys(self) -> AuthorityCompileRequest:
        _validate_key_mode(
            dry_run=self.dry_run,
            idempotency_key=self.idempotency_key,
            dry_run_id=self.dry_run_id,
        )
        return self

    def normalized_request_hash(self) -> str:
        """Return a stable hash including stale-guard inputs."""
        return canonical_hash(
            {
                "command": PROJECT_AUTHORITY_COMPILE_COMMAND,
                "project_id": self.project_id,
                "spec_version_id": self.spec_version_id,
                "expected_spec_hash": self.expected_spec_hash,
                "expected_state": self.expected_state,
                "expected_setup_status": self.expected_setup_status,
                "changed_by": self.changed_by,
            }
        )


class ProjectSetupWorkflowPort(Protocol):
    """Workflow session operations used by project setup."""

    def initialize_session(self, session_id: str | None = None) -> str:
        """Create a workflow session."""
        raise NotImplementedError

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge a partial workflow session update."""
        raise NotImplementedError

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return session state or `{}` when absent."""
        raise NotImplementedError

    def ensure_setup_state(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        spec_hash: str,
        spec_version_id: int,
        lease_guard: Callable[[str], bool],
        record_progress: Callable[[str], bool],
    ) -> dict[str, Any]:
        """Reconcile the workflow setup state idempotently."""
        raise NotImplementedError


class SyncProjectSetupWorkflowAdapter:
    """Synchronous adapter over WorkflowService for setup reconciliation."""

    def __init__(self, workflow: WorkflowService) -> None:
        self._workflow = workflow

    def initialize_session(self, session_id: str | None = None) -> str:
        """Create a workflow session outside an active asyncio event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._workflow.initialize_session(session_id=session_id)
            )
        raise RuntimeError(
            "Project setup runner cannot initialize workflow sessions inside "
            "an active event loop"
        )

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge a partial workflow state update."""
        self._workflow.update_session_status(session_id, partial_update)

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return workflow session state or `{}` when absent."""
        return self._workflow.get_session_status(session_id)

    def ensure_setup_state(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        spec_hash: str,
        spec_version_id: int,
        lease_guard: Callable[[str], bool],
        record_progress: Callable[[str], bool],
    ) -> dict[str, Any]:
        """Create or reconcile canonical project setup workflow state."""
        session_id = str(project_id)
        current = self.get_session_status(session_id)

        if current == {}:
            if not lease_guard("workflow_session_created"):
                return {"ok": False, "error_code": MUTATION_IN_PROGRESS}
            self.initialize_session(session_id=session_id)
            current = self.get_session_status(session_id)
        if not record_progress("workflow_session_created"):
            return {"ok": False, "error_code": MUTATION_RECOVERY_REQUIRED}

        required_state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": AUTHORITY_COMPILE_REQUIRED,
            "setup_error": None,
            "setup_spec_file_path": str(resolved_spec_path),
            "setup_spec_hash": str(spec_hash),
            "setup_spec_version_id": int(spec_version_id),
            "setup_next_actions": [
                _authority_compile_action(
                    project_id=project_id,
                    spec_version_id=int(spec_version_id),
                    spec_hash=str(spec_hash),
                    expected_setup_status=AUTHORITY_COMPILE_REQUIRED,
                )
            ],
        }
        merged = {**current, **required_state}
        if current != merged:
            if not lease_guard("workflow_session_status_written"):
                return {"ok": False, "error_code": MUTATION_IN_PROGRESS}
            self.update_session_status(session_id, required_state)
            current = self.get_session_status(session_id)
        if not record_progress("workflow_session_status_written"):
            return {"ok": False, "error_code": MUTATION_RECOVERY_REQUIRED}

        return {
            "ok": True,
            "session_id": session_id,
            "state": self.get_session_status(session_id),
        }


def _default_workflow_port() -> ProjectSetupWorkflowPort:
    """Construct the default workflow adapter only when runtime setup needs it."""
    from services.workflow import WorkflowService  # noqa: PLC0415

    return SyncProjectSetupWorkflowAdapter(WorkflowService())


def compile_spec_authority_for_version_with_engine(**kwargs: Any) -> dict[str, Any]:
    """Compile spec authority through the default service only when invoked."""
    from services.specs.compiler_service import (  # noqa: PLC0415
        compile_spec_authority_for_version_with_engine as compile_with_engine,
    )

    return compile_with_engine(**kwargs)


class ProjectSetupMutationRunner:
    """Run idempotent project creation and setup recovery mutations."""

    def __init__(
        self,
        *,
        engine: Engine,
        workflow: ProjectSetupWorkflowPort | None = None,
    ) -> None:
        self._engine = engine
        ensure_business_db_ready(engine_override=engine)
        self._ledger = MutationLedgerRepository(engine=engine)
        self._workflow = workflow or _default_workflow_port()
        self._lease_seconds = DEFAULT_LEASE_SECONDS
        self.fail_after_step_for_test: str | None = None
        self.fail_retry_before_side_effects_for_test: bool = False
        self.fail_retry_after_side_effects_for_test: bool = False

    def create_project(self, request: ProjectCreateRequest) -> dict[str, Any]:
        """Create project/spec metadata and require authority compilation."""
        return self._run_create(request)

    def retry_setup(self, request: ProjectSetupRetryRequest) -> dict[str, Any]:
        """Retry interrupted setup work with stale guards."""
        return self._run_retry(request)

    def compile_authority(self, request: AuthorityCompileRequest) -> dict[str, Any]:
        """Compile pending authority with project/spec stale guards."""
        return self._run_authority_compile(request)

    def _run_create(self, request: ProjectCreateRequest) -> dict[str, Any]:
        resolved_spec_path = Path(request.spec_file).expanduser().resolve()
        spec_hash_result = _spec_hash_or_error(resolved_spec_path)
        if not isinstance(spec_hash_result, str):
            return spec_hash_result
        if request.dry_run:
            return _success(
                {
                    "preview_available": True,
                    "name": request.name,
                    "resolved_spec_path": str(resolved_spec_path),
                }
            )

        existing_key_row = self._find_ledger(
            command=PROJECT_CREATE_COMMAND,
            idempotency_key=_required(request.idempotency_key),
        )
        if existing_key_row is None and self._product_name_exists(request.name):
            return _error(
                PROJECT_ALREADY_EXISTS,
                details={"name": request.name},
                remediation=["Choose a different project name."],
            )

        loaded = self._ledger.create_or_load(
            command=PROJECT_CREATE_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            request_hash=_create_request_hash(
                request=request,
                resolved_spec_path=resolved_spec_path,
                spec_hash=spec_hash_result,
            ),
            project_id=None,
            correlation_id=_correlation_id(request.correlation_id),
            changed_by=request.changed_by,
            lease_owner=_lease_owner(
                command=PROJECT_CREATE_COMMAND,
                idempotency_key=_required(request.idempotency_key),
                correlation_id=request.correlation_id,
            ),
            now=_now(),
            lease_seconds=self._lease_seconds,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code == MUTATION_RECOVERY_REQUIRED:
            return _recovery_required_response(loaded.ledger, request.spec_file)
        if loaded.error_code is not None:
            return _error_for_ledger(loaded.error_code, loaded.ledger)

        event_id = _event_id(loaded.ledger)
        lease_owner = _required(loaded.ledger.lease_owner)
        return self._run_setup_steps(
            request_name=request.name,
            requested_spec_file=request.spec_file,
            resolved_spec_path=resolved_spec_path,
            mutation_event_id=event_id,
            lease_owner=lease_owner,
            create_product=True,
        )

    def _run_retry(self, request: ProjectSetupRetryRequest) -> dict[str, Any]:
        resolved_spec_path = Path(request.spec_file).expanduser().resolve()
        spec_hash_result = _spec_hash_or_error(resolved_spec_path)
        if not isinstance(spec_hash_result, str):
            return spec_hash_result

        original = self._validate_original_recovery_row(request)
        if isinstance(original, dict):
            return original
        workflow_result = self._workflow_state_or_error(request)
        if not workflow_result["ok"]:
            return workflow_result
        workflow_state = cast("dict[str, Any]", workflow_result["state"])
        current_state = str(workflow_state.get("fsm_state") or "SETUP_REQUIRED")
        if current_state != request.expected_state:
            if request.dry_run:
                return _error(
                    ErrorCode.STALE_STATE.value,
                    details={
                        "expected_state": request.expected_state,
                        "actual_state": current_state,
                    },
                    remediation=["Refresh setup retry stale guards before retrying."],
                )
            return self._guard_rejected_retry(
                request=request,
                code=ErrorCode.STALE_STATE.value,
                details={"expected_state": request.expected_state, "actual_state": current_state},
            )

        current_fingerprint = _retry_context_fingerprint(
            project_id=request.project_id,
            resolved_spec_path=resolved_spec_path,
            workflow_state=workflow_state,
        )
        if current_fingerprint != request.expected_context_fingerprint:
            stale_context_data = _stale_context_retry_data(
                request=request,
                actual_context_fingerprint=current_fingerprint,
            )
            remediation = [
                "Retry setup with data.next_actions[0] after confirming the "
                "actual context fingerprint."
            ]
            if request.dry_run:
                return _error(
                    ErrorCode.STALE_CONTEXT_FINGERPRINT.value,
                    details={
                        "expected_context_fingerprint": request.expected_context_fingerprint,
                        "actual_context_fingerprint": current_fingerprint,
                    },
                    remediation=remediation,
                    data=stale_context_data,
                )
            return self._guard_rejected_retry(
                request=request,
                code=ErrorCode.STALE_CONTEXT_FINGERPRINT.value,
                details={
                    "expected_context_fingerprint": request.expected_context_fingerprint,
                    "actual_context_fingerprint": current_fingerprint,
                },
                remediation=remediation,
                data=stale_context_data,
            )
        if request.dry_run:
            if (
                original is not None
                and _active_recovery_lease_blocks_retry(original, now=_now())
            ):
                return _mutation_resume_conflict(
                    retry_mutation_event_id=None,
                    original_mutation_event_id=request.recovery_mutation_event_id,
                )
            return _success(
                {
                    "preview_available": True,
                    "project_id": request.project_id,
                    "resolved_spec_path": str(resolved_spec_path),
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                    "recovery_status": original.status if original is not None else None,
                    "next_actions": [
                        _retry_action(request, request.recovery_mutation_event_id)
                    ],
                }
            )

        original_event_id = request.recovery_mutation_event_id
        retry_owner = _lease_owner(
            command=PROJECT_SETUP_RETRY_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            correlation_id=request.correlation_id,
        )
        loaded = self._ledger.create_or_load(
            command=PROJECT_SETUP_RETRY_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            request_hash=request.normalized_request_hash(),
            project_id=request.project_id,
            correlation_id=_correlation_id(request.correlation_id),
            changed_by=request.changed_by,
            lease_owner=retry_owner,
            now=_now(),
            recovers_mutation_event_id=original_event_id,
            lease_seconds=self._lease_seconds,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error_for_ledger(loaded.error_code, loaded.ledger)
        retry_event_id = _event_id(loaded.ledger)

        original_recovery_owner = (
            f"project-setup-retry:{retry_event_id}:recovers:{original_event_id}"
        )
        if original_event_id is not None and not self._ledger.acquire_recovery_lease(
            mutation_event_id=original_event_id,
            expected_project_id=request.project_id,
            recovery_lease_owner=original_recovery_owner,
            now=_now(),
            lease_seconds=self._lease_seconds,
        ):
            return _mutation_resume_conflict(
                retry_mutation_event_id=retry_event_id,
                original_mutation_event_id=original_event_id,
            )

        if self.fail_retry_before_side_effects_for_test:
            response_data = _retry_pre_side_effect_failure_data(
                request=request,
                retry_mutation_event_id=retry_event_id,
                original_mutation_event_id=original_event_id,
            )
            self._mark_retry_domain_failed(
                retry_mutation_event_id=retry_event_id,
                retry_lease_owner=retry_owner,
                response_data=response_data,
            )
            if original_event_id is not None:
                self._ledger.release_recovery_lease(
                    mutation_event_id=original_event_id,
                    recovery_lease_owner=original_recovery_owner,
                    now=_now(),
                )
            return _error(
                "MUTATION_FAILED",
                details={"mutation_event_id": retry_event_id},
                remediation=["Retry setup with a new idempotency key."],
                data=response_data,
            )

        setup_result = self._run_setup_steps(
            request_name=None,
            requested_spec_file=request.spec_file,
            resolved_spec_path=resolved_spec_path,
            mutation_event_id=retry_event_id,
            lease_owner=retry_owner,
            create_product=False,
            existing_project_id=request.project_id,
            finalize=False,
            finalize_domain_failures=original_event_id is None,
        )
        if not setup_result["ok"]:
            if original_event_id is not None:
                return self._transfer_retry_recovery(
                    request=request,
                    retry_mutation_event_id=retry_event_id,
                    retry_lease_owner=retry_owner,
                    original_mutation_event_id=original_event_id,
                    original_recovery_lease_owner=original_recovery_owner,
                    last_error={"code": setup_result["errors"][0]["code"]},
                )
            return setup_result

        if self.fail_retry_after_side_effects_for_test:
            return self._transfer_retry_recovery(
                request=request,
                retry_mutation_event_id=retry_event_id,
                retry_lease_owner=retry_owner,
                original_mutation_event_id=_required_int(original_event_id),
                original_recovery_lease_owner=original_recovery_owner,
                last_error={"code": WORKFLOW_SESSION_FAILED},
            )

        retry_response = _success(
            {
                **setup_result["data"],
                "mutation_event_id": retry_event_id,
                "recovery_mutation_event_id": original_event_id,
            }
        )
        if original_event_id is None:
            if not self._ledger.finalize_success(
                mutation_event_id=retry_event_id,
                lease_owner=retry_owner,
                after=retry_response["data"],
                response=retry_response,
                now=_now(),
            ):
                return _mutation_resume_conflict(
                    retry_mutation_event_id=retry_event_id,
                    original_mutation_event_id=original_event_id,
                )
            return retry_response

        linked = self._ledger.finalize_linked_retry_success(
            retry_mutation_event_id=retry_event_id,
            retry_lease_owner=retry_owner,
            original_mutation_event_id=original_event_id,
            original_recovery_lease_owner=original_recovery_owner,
            after=retry_response["data"],
            retry_response=retry_response,
            original_replay_response=retry_response,
            now=_now(),
        )
        if linked.error_code == MUTATION_RESUME_CONFLICT:
            return _mutation_resume_conflict(
                retry_mutation_event_id=retry_event_id,
                original_mutation_event_id=original_event_id,
            )
        return retry_response

    def _run_authority_compile(self, request: AuthorityCompileRequest) -> dict[str, Any]:
        if not self._project_exists(request.project_id):
            return _error(
                ErrorCode.PROJECT_NOT_FOUND.value,
                details={"project_id": request.project_id},
                remediation=["Create the project before compiling authority."],
            )

        workflow_result = self._authority_compile_workflow_state_or_error(request)
        if not workflow_result["ok"]:
            return workflow_result
        workflow_state = cast("dict[str, Any]", workflow_result["state"])
        stale_guard = _authority_compile_stale_guard(
            request=request,
            workflow_state=workflow_state,
        )
        if stale_guard is not None:
            code, details = stale_guard
            if request.dry_run:
                return _error(
                    code,
                    details=details,
                    remediation=["Refresh authority compile stale guards before retrying."],
                )
            return self._guard_rejected_authority_compile(
                request=request,
                code=code,
                details=details,
            )

        resolved_spec_path = Path(str(workflow_state["setup_spec_file_path"])).expanduser().resolve()
        spec_hash = str(workflow_state["setup_spec_hash"])
        spec_version_id = int(str(workflow_state["setup_spec_version_id"]))
        live_spec_hash_result = _spec_hash_or_error(resolved_spec_path)
        if not isinstance(live_spec_hash_result, str):
            return live_spec_hash_result
        if (
            live_spec_hash_result != request.expected_spec_hash
            or live_spec_hash_result != spec_hash
        ):
            details = {
                "expected_spec_hash": request.expected_spec_hash,
                "workflow_spec_hash": spec_hash,
                "actual_spec_hash": live_spec_hash_result,
            }
            if request.dry_run:
                return _error(
                    ErrorCode.STALE_SPEC_HASH.value,
                    details=details,
                    remediation=["Refresh authority compile stale guards before retrying."],
                )
            return self._guard_rejected_authority_compile(
                request=request,
                code=ErrorCode.STALE_SPEC_HASH.value,
                details=details,
            )

        if request.dry_run:
            return _success(
                {
                    "preview_available": True,
                    "project_id": request.project_id,
                    "resolved_spec_path": str(resolved_spec_path),
                    "spec_hash": spec_hash,
                    "spec_version_id": spec_version_id,
                    "setup_status": str(workflow_state["setup_status"]),
                    "fsm_state": str(workflow_state["fsm_state"]),
                    "next_actions": [
                        _authority_compile_action(
                            project_id=request.project_id,
                            spec_version_id=spec_version_id,
                            spec_hash=spec_hash,
                            expected_setup_status=request.expected_setup_status,
                        )
                    ],
                }
            )

        lease_owner = _lease_owner(
            command=PROJECT_AUTHORITY_COMPILE_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            correlation_id=request.correlation_id,
        )
        loaded = self._ledger.create_or_load(
            command=PROJECT_AUTHORITY_COMPILE_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            request_hash=request.normalized_request_hash(),
            project_id=request.project_id,
            correlation_id=_correlation_id(request.correlation_id),
            changed_by=request.changed_by,
            lease_owner=lease_owner,
            now=_now(),
            lease_seconds=self._lease_seconds,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code == MUTATION_RECOVERY_REQUIRED:
            return _authority_compile_recovery_required_response(
                loaded.ledger,
                project_id=request.project_id,
                spec_hash=spec_hash,
                spec_version_id=spec_version_id,
                expected_setup_status=request.expected_setup_status,
            )
        if loaded.error_code is not None:
            return _error_for_ledger(loaded.error_code, loaded.ledger)

        mutation_event_id = _event_id(loaded.ledger)
        try:
            self._write_authority_compile_workflow_state(
                project_id=request.project_id,
                status="authority_compiling",
                resolved_spec_path=resolved_spec_path,
                spec_hash=spec_hash,
                spec_version_id=spec_version_id,
                mutation_event_id=mutation_event_id,
            )
        except Exception as exc:  # noqa: BLE001
            return self._mark_authority_compile_recovery_required(
                request=request,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                spec_hash=spec_hash,
                spec_version_id=spec_version_id,
                workflow_error=str(exc),
            )

        authority_result = self._ensure_pending_authority(
            project_id=request.project_id,
            resolved_spec_path=resolved_spec_path,
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
        )
        if not authority_result.get("ok"):
            return self._mark_authority_compile_validation_failed(
                request=request,
                resolved_spec_path=resolved_spec_path,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                spec_hash=spec_hash,
                spec_version_id=spec_version_id,
                authority_result=authority_result,
            )

        compiled_spec_hash = str(authority_result.get("spec_hash") or spec_hash)
        compiled_spec_version_id = int(
            str(authority_result.get("spec_version_id") or spec_version_id)
        )
        authority_id = _optional_int(authority_result.get("authority_id"))
        try:
            self._write_authority_compile_workflow_state(
                project_id=request.project_id,
                status=AUTHORITY_PENDING_REVIEW,
                resolved_spec_path=resolved_spec_path,
                spec_hash=compiled_spec_hash,
                spec_version_id=compiled_spec_version_id,
                mutation_event_id=mutation_event_id,
            )
        except Exception as exc:  # noqa: BLE001
            return self._mark_authority_compile_recovery_required(
                request=request,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                spec_hash=compiled_spec_hash,
                spec_version_id=compiled_spec_version_id,
                workflow_error=str(exc),
            )

        data = {
            "project_id": request.project_id,
            "resolved_spec_path": str(resolved_spec_path),
            "spec_hash": compiled_spec_hash,
            "spec_version_id": compiled_spec_version_id,
            "pending_authority_id": authority_id,
            "compiled_authority_id": authority_id,
            "setup_status": AUTHORITY_PENDING_REVIEW,
            "fsm_state": "SETUP_REQUIRED",
            "mutation_event_id": mutation_event_id,
            "next_actions": [_authority_review_action(request.project_id)],
        }
        response = _success(data)
        if not self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        ):
            return _error(
                ErrorCode.MUTATION_RESUME_CONFLICT.value,
                details={"mutation_event_id": mutation_event_id},
                remediation=["Re-read mutation state before retrying authority compile."],
            )
        return response

    def _workflow_state_or_error(
        self,
        request: ProjectSetupRetryRequest,
    ) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                "state": self._workflow.get_session_status(str(request.project_id)),
            }
        except Exception as exc:  # noqa: BLE001
            retry_request = request.model_copy(
                update={
                    "expected_context_fingerprint": "<refresh-context-fingerprint>",
                }
            )
            return _error(
                WORKFLOW_SESSION_FAILED,
                details={
                    "project_id": request.project_id,
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                    "workflow_error": str(exc),
                },
                remediation=[
                    "Restore workflow session storage, refresh retry guards, "
                    "then rerun project setup retry."
                ],
                data={
                    "project_id": request.project_id,
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                    "next_actions": [
                        _retry_action(
                            retry_request,
                            request.recovery_mutation_event_id,
                        )
                    ],
                },
            )

    def _authority_compile_workflow_state_or_error(
        self,
        request: AuthorityCompileRequest,
    ) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                "state": self._workflow.get_session_status(str(request.project_id)),
            }
        except Exception as exc:  # noqa: BLE001
            return _error(
                WORKFLOW_SESSION_FAILED,
                details={
                    "project_id": request.project_id,
                    "workflow_error": str(exc),
                },
                remediation=[
                    "Restore workflow session storage, refresh authority compile "
                    "guards, then rerun authority compile."
                ],
                data={
                    "project_id": request.project_id,
                    "next_actions": [
                        _authority_compile_action(
                            project_id=request.project_id,
                            spec_version_id=request.spec_version_id,
                            spec_hash=request.expected_spec_hash,
                            expected_setup_status=request.expected_setup_status,
                        )
                    ],
                },
            )

    def _write_authority_compile_workflow_state(
        self,
        *,
        project_id: int,
        status: str,
        resolved_spec_path: Path,
        spec_hash: str,
        spec_version_id: int,
        mutation_event_id: int,
        failure_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": status,
            "setup_error": None if failure_data is None else failure_data.get("setup_error"),
            "setup_spec_file_path": str(resolved_spec_path),
            "setup_spec_hash": spec_hash,
            "setup_spec_version_id": spec_version_id,
            "setup_compile_mutation_event_id": mutation_event_id,
        }
        if status in {"authority_compiling", AUTHORITY_PENDING_REVIEW}:
            state.update(_cleared_authority_compile_failure_metadata())
        if status == "authority_compiling":
            state["setup_compile_started_at"] = _now().isoformat()
            state["setup_next_actions"] = [
                {
                    "command": "agileforge mutation show",
                    "args": {"mutation_event_id": mutation_event_id},
                    "reason": "Inspect the active authority compile mutation.",
                }
            ]
        if status == AUTHORITY_PENDING_REVIEW:
            state["setup_next_actions"] = [_authority_review_action(project_id)]
        if status == AUTHORITY_COMPILE_FAILED and failure_data is not None:
            state.update(failure_data)
            state["setup_next_actions"] = [
                _authority_compile_action(
                    project_id=project_id,
                    spec_version_id=spec_version_id,
                    spec_hash=spec_hash,
                    expected_setup_status=AUTHORITY_COMPILE_FAILED,
                )
            ]
        self._workflow.update_session_status(str(project_id), state)
        return {"ok": True, "state": self._workflow.get_session_status(str(project_id))}

    def _guard_rejected_authority_compile(
        self,
        *,
        request: AuthorityCompileRequest,
        code: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        loaded = self._ledger.create_or_load(
            command=PROJECT_AUTHORITY_COMPILE_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            request_hash=request.normalized_request_hash(),
            project_id=request.project_id,
            correlation_id=_correlation_id(request.correlation_id),
            changed_by=request.changed_by,
            lease_owner=_lease_owner(
                command=PROJECT_AUTHORITY_COMPILE_COMMAND,
                idempotency_key=_required(request.idempotency_key),
                correlation_id=request.correlation_id,
            ),
            now=_now(),
            lease_seconds=self._lease_seconds,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code == MUTATION_RECOVERY_REQUIRED:
            expected_setup_status = self._current_authority_compile_setup_status(
                project_id=request.project_id,
                fallback=request.expected_setup_status,
            )
            return _authority_compile_recovery_required_response(
                loaded.ledger,
                project_id=request.project_id,
                spec_hash=request.expected_spec_hash,
                spec_version_id=request.spec_version_id,
                expected_setup_status=expected_setup_status,
            )
        if loaded.error_code is not None:
            return _error_for_ledger(loaded.error_code, loaded.ledger)

        response = _error(
            code,
            details=details,
            remediation=["Refresh authority compile stale guards before retrying."],
        )
        self._mark_simple_status(
            mutation_event_id=_event_id(loaded.ledger),
            lease_owner=_required(loaded.ledger.lease_owner),
            status=MutationStatus.GUARD_REJECTED,
            response=response,
        )
        return response

    def _mark_authority_compile_recovery_required(
        self,
        *,
        request: AuthorityCompileRequest,
        mutation_event_id: int,
        lease_owner: str,
        spec_hash: str,
        spec_version_id: int,
        workflow_error: str,
    ) -> dict[str, Any]:
        expected_setup_status = self._current_authority_compile_setup_status(
            project_id=request.project_id,
            fallback=request.expected_setup_status,
        )
        last_error = {
            "code": WORKFLOW_SESSION_FAILED,
            "project_id": request.project_id,
            "spec_version_id": spec_version_id,
            "spec_hash": spec_hash,
            "mutation_event_id": mutation_event_id,
            "workflow_error": workflow_error,
        }
        marked = self._ledger.mark_recovery_required(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            recovery_action=RecoveryAction.RESUME_FROM_STEP,
            safe_to_auto_resume=True,
            last_error=last_error,
            now=_now(),
        )
        row = self._must_get_ledger(mutation_event_id)
        if marked or row.status == MutationStatus.RECOVERY_REQUIRED.value:
            return _authority_compile_recovery_required_response(
                row,
                project_id=request.project_id,
                spec_hash=spec_hash,
                spec_version_id=spec_version_id,
                expected_setup_status=expected_setup_status,
            )
        return _error_for_ledger(MUTATION_IN_PROGRESS, row)

    def _current_authority_compile_setup_status(
        self,
        *,
        project_id: int,
        fallback: str,
    ) -> str:
        try:
            state = self._workflow.get_session_status(str(project_id))
        except Exception:  # noqa: BLE001
            return fallback
        return str(state.get("setup_status") or fallback)

    def _mark_authority_compile_validation_failed(
        self,
        *,
        request: AuthorityCompileRequest,
        resolved_spec_path: Path,
        mutation_event_id: int,
        lease_owner: str,
        spec_hash: str,
        spec_version_id: int,
        authority_result: dict[str, Any],
    ) -> dict[str, Any]:
        error_code = str(authority_result.get("error_code") or SPEC_COMPILE_FAILED)
        failure_summary = str(
            authority_result.get("failure_summary")
            or authority_result.get("error")
            or "Spec authority compile failed."
        )
        artifact_failure_stage = _optional_str(authority_result.get("failure_stage"))
        blocking_gaps = authority_result.get("blocking_gaps")
        setup_failure_first_error = _first_failure_error(
            blocking_gaps,
            failure_summary,
        )
        response_spec_hash = str(authority_result.get("spec_hash") or spec_hash)
        response_spec_version_id = int(
            str(authority_result.get("spec_version_id") or spec_version_id)
        )
        failure_data: dict[str, Any] = {
            "setup_error": error_code,
            "setup_failure_stage": "authority_compile",
            "setup_failure_summary": failure_summary,
            "setup_failure_artifact_id": _optional_str(
                authority_result.get("failure_artifact_id")
            ),
            "failure_artifact_stage": artifact_failure_stage,
            "setup_failure_first_error": setup_failure_first_error,
            "raw_output_preview": _optional_str(authority_result.get("raw_output_preview")),
            "has_full_artifact": _optional_bool(authority_result.get("has_full_artifact")),
        }
        if isinstance(blocking_gaps, list):
            failure_data["setup_failure_blocking_gaps"] = [
                str(item) for item in blocking_gaps
            ]
        try:
            workflow_result = self._write_authority_compile_workflow_state(
                project_id=request.project_id,
                status=AUTHORITY_COMPILE_FAILED,
                resolved_spec_path=resolved_spec_path,
                spec_hash=response_spec_hash,
                spec_version_id=response_spec_version_id,
                mutation_event_id=mutation_event_id,
                failure_data=failure_data,
            )
        except Exception as exc:  # noqa: BLE001
            return self._mark_authority_compile_recovery_required(
                request=request,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                spec_hash=response_spec_hash,
                spec_version_id=response_spec_version_id,
                workflow_error=str(exc),
            )
        next_actions = cast("dict[str, Any]", workflow_result["state"])[
            "setup_next_actions"
        ]
        data = {
            "project_id": request.project_id,
            "resolved_spec_path": str(resolved_spec_path),
            "spec_hash": response_spec_hash,
            "spec_version_id": response_spec_version_id,
            "setup_status": AUTHORITY_COMPILE_FAILED,
            "fsm_state": "SETUP_REQUIRED",
            **failure_data,
            "mutation_event_id": mutation_event_id,
            "next_actions": next_actions,
        }
        response = _error(
            error_code,
            details={
                "project_id": request.project_id,
                "mutation_event_id": mutation_event_id,
                "spec_version_id": response_spec_version_id,
                "failure_artifact_id": failure_data["setup_failure_artifact_id"],
                "first_error": setup_failure_first_error,
            },
            remediation=[
                "Inspect the compiler/setup error, then rerun agileforge authority compile."
            ],
            data=data,
        )
        if not self._mark_simple_status(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            status=MutationStatus.VALIDATION_FAILED,
            response=response,
        ):
            return _error(
                ErrorCode.MUTATION_RESUME_CONFLICT.value,
                details={"mutation_event_id": mutation_event_id},
                remediation=["Re-read mutation state before retrying authority compile."],
            )
        return response

    def _run_setup_steps(
        self,
        *,
        request_name: str | None,
        requested_spec_file: str,
        resolved_spec_path: Path,
        mutation_event_id: int,
        lease_owner: str,
        create_product: bool,
        existing_project_id: int | None = None,
        finalize: bool = True,
        finalize_domain_failures: bool = True,
    ) -> dict[str, Any]:
        completed_steps = self._completed_steps(mutation_event_id)
        project_id = existing_project_id or self._project_id_for_event(mutation_event_id)

        if create_product and "product_created" not in completed_steps:
            if request_name is None:
                raise ValueError("request_name is required to create a product.")
            project_id = self._create_product_and_record_progress(
                name=request_name,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
            )
            if self.fail_after_step_for_test == "product_created":
                self._mark_create_recovery_required(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    project_id=project_id,
                    code=WORKFLOW_SESSION_FAILED,
                    spec_file=requested_spec_file,
                    safe_to_auto_resume=True,
                )
                raise RuntimeError("Injected project setup failure after product_created")
        if project_id is None:
            return _error(
                ErrorCode.MUTATION_RESUME_CONFLICT.value,
                details={"mutation_event_id": mutation_event_id},
                remediation=["Re-read mutation state before retrying recovery."],
            )

        spec_result = self._ensure_pending_spec_version(
            project_id=project_id,
            resolved_spec_path=resolved_spec_path,
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
        )
        if not spec_result.get("ok"):
            error_code = str(spec_result["error_code"])
            if (
                _setup_failure_requires_recovery(error_code)
                or not finalize_domain_failures
            ):
                marked = self._mark_create_recovery_required(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    project_id=project_id,
                    code=error_code,
                    spec_file=requested_spec_file,
                    safe_to_auto_resume=False,
                    spec_version_id=spec_result.get("spec_version_id"),
                )
                if isinstance(marked, dict):
                    return marked
                return _recovery_required_response(marked, requested_spec_file)
            return self._mark_setup_validation_failed(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                project_id=project_id,
                requested_spec_file=requested_spec_file,
                resolved_spec_path=resolved_spec_path,
                error_code=error_code,
                error_summary=str(
                    spec_result.get("error") or "Spec registration failed."
                ),
                spec_hash=spec_result.get("spec_hash"),
                spec_version_id=spec_result.get("spec_version_id"),
                failure_artifact_id=spec_result.get("failure_artifact_id"),
                failure_stage=spec_result.get("failure_stage"),
                failure_summary=spec_result.get("failure_summary"),
                raw_output_preview=spec_result.get("raw_output_preview"),
                has_full_artifact=spec_result.get("has_full_artifact"),
                blocking_gaps=spec_result.get("blocking_gaps"),
            )

        workflow_result = self._ensure_workflow_setup(
            project_id=project_id,
            resolved_spec_path=resolved_spec_path,
            spec_hash=str(spec_result["spec_hash"]),
            spec_version_id=int(spec_result["spec_version_id"]),
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
        )
        if not workflow_result.get("ok"):
            marked = self._mark_create_recovery_required(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                project_id=project_id,
                code=WORKFLOW_SESSION_FAILED,
                spec_file=requested_spec_file,
                safe_to_auto_resume=True,
                spec_version_id=spec_result.get("spec_version_id"),
            )
            if isinstance(marked, dict):
                return marked
            return _recovery_required_response(marked, requested_spec_file)

        data = {
            "project_id": project_id,
            "name": self._project_name(project_id),
            "resolved_spec_path": str(resolved_spec_path),
            "spec_hash": spec_result["spec_hash"],
            "spec_version_id": spec_result["spec_version_id"],
            "setup_status": AUTHORITY_COMPILE_REQUIRED,
            "fsm_state": "SETUP_REQUIRED",
            "mutation_event_id": mutation_event_id,
            "next_actions": [
                _authority_compile_action(
                    project_id=project_id,
                    spec_version_id=int(spec_result["spec_version_id"]),
                    spec_hash=str(spec_result["spec_hash"]),
                    expected_setup_status=AUTHORITY_COMPILE_REQUIRED,
                )
            ],
        }
        response = _success(data)
        if finalize and not self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        ):
            return _error(
                ErrorCode.MUTATION_RESUME_CONFLICT.value,
                details={"mutation_event_id": mutation_event_id},
                remediation=["Re-read mutation state before retrying recovery."],
            )
        return response

    def _create_product_and_record_progress(
        self,
        *,
        name: str,
        mutation_event_id: int,
        lease_owner: str,
    ) -> int:
        now = _now()
        with Session(self._engine) as session:
            if not self._ledger.require_active_owner(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                now=now,
                lease_seconds=self._lease_seconds,
            ):
                raise RuntimeError("Mutation owner is no longer active.")
            product = Product(name=name)
            session.add(product)
            session.flush()
            project_id = _required_int(product.product_id)
            if not MutationLedgerRepository.set_project_id_in_session(
                session,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                project_id=project_id,
                now=now,
            ):
                session.rollback()
                raise RuntimeError("Failed to attach project id to mutation.")
            if not MutationLedgerRepository.mark_step_complete_in_session(
                session,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                step="product_created",
                next_step="product_spec_linked",
                now=now,
            ):
                session.rollback()
                raise RuntimeError("Failed to record product creation progress.")
            session.commit()
            return project_id

    def _ensure_pending_spec_version(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        mutation_event_id: int,
        lease_owner: str,
    ) -> dict[str, Any]:
        completed_steps = self._completed_steps(mutation_event_id)
        if (
            "spec_registry_written" in completed_steps
            and "spec_marked_approved" in completed_steps
        ):
            existing = self._existing_spec_version(project_id)
            if existing is not None:
                return {"ok": True, **existing}

        def lease_guard(boundary: str) -> bool:
            del boundary
            return self._ledger.require_active_owner(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                now=_now(),
                lease_seconds=self._lease_seconds,
            )

        def record_progress(boundary: str) -> bool:
            return self._ledger.mark_step_complete(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                step=boundary,
                next_step=boundary,
                now=_now(),
            )

        with Session(self._engine) as session:
            result = ensure_pending_spec_version_for_project(
                session=session,
                product_id=project_id,
                spec_path=resolved_spec_path,
                approved_by="cli-project-create",
                lease_guard=lease_guard,
                record_progress=record_progress,
            )
        if not result.ok:
            return {
                "ok": False,
                "error_code": result.error_code or "SPEC_FILE_INVALID",
                "error": result.error,
                "spec_hash": result.spec_hash,
                "spec_version_id": result.spec_version_id,
                "reason": result.reason,
            }
        return {
            "ok": True,
            "spec_hash": result.spec_hash,
            "spec_version_id": result.spec_version_id,
        }

    def _ensure_pending_authority(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        mutation_event_id: int,
        lease_owner: str,
    ) -> dict[str, Any]:
        completed_steps = self._completed_steps(mutation_event_id)
        if "pending_authority_compiled" in completed_steps:
            existing = self._existing_authority(project_id)
            if existing is not None:
                return {"ok": True, **existing}

        def lease_guard(boundary: str) -> bool:
            return self._ledger.require_active_owner(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                now=_now(),
                lease_seconds=self._lease_seconds,
            )

        def record_progress(boundary: str) -> bool:
            return self._ledger.mark_step_complete(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                step=boundary,
                next_step=boundary,
                now=_now(),
            )

        def engine_bound_compiler(**kwargs: Any) -> dict[str, Any]:
            return compile_spec_authority_for_version_with_engine(
                engine=self._engine,
                **kwargs,
            )

        with Session(self._engine) as session:
            result = compile_pending_authority_for_project(
                session=session,
                product_id=project_id,
                spec_path=resolved_spec_path,
                approved_by="cli-project-create",
                compile_authority=engine_bound_compiler,
                lease_guard=lease_guard,
                record_progress=record_progress,
            )
        if not result.ok:
            return {
                "ok": False,
                "error_code": result.error_code or SPEC_COMPILE_FAILED,
                "error": result.error,
                "spec_hash": result.spec_hash,
                "spec_version_id": result.spec_version_id,
                "reason": result.reason,
                "failure_artifact_id": result.failure_artifact_id,
                "failure_stage": result.failure_stage,
                "failure_summary": result.failure_summary,
                "raw_output_preview": result.raw_output_preview,
                "has_full_artifact": result.has_full_artifact,
                "blocking_gaps": result.blocking_gaps,
            }
        if not self._ledger.mark_step_complete(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            step="pending_authority_compiled",
            next_step="workflow_session_initialized",
            now=_now(),
        ):
            return {"ok": False, "error_code": MUTATION_RECOVERY_REQUIRED}
        return {
            "ok": True,
            "spec_hash": result.spec_hash,
            "spec_version_id": result.spec_version_id,
            "authority_id": result.authority_id,
        }

    def _ensure_workflow_setup(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        spec_hash: str,
        spec_version_id: int,
        mutation_event_id: int,
        lease_owner: str,
    ) -> dict[str, Any]:
        completed_steps = self._completed_steps(mutation_event_id)
        if "workflow_session_initialized" in completed_steps:
            try:
                state = self._workflow.get_session_status(str(project_id))
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error_code": WORKFLOW_SESSION_FAILED,
                    "error": str(exc),
                }
            if _workflow_has_required_setup_state(
                state,
                resolved_spec_path,
                spec_hash=spec_hash,
                spec_version_id=spec_version_id,
                project_id=project_id,
            ):
                return {"ok": True, "state": state}

        def lease_guard(boundary: str) -> bool:
            del boundary
            return self._ledger.require_active_owner(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                now=_now(),
                lease_seconds=self._lease_seconds,
            )

        def record_progress(boundary: str) -> bool:
            return self._ledger.mark_step_complete(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                step=boundary,
                next_step=boundary,
                now=_now(),
            )

        try:
            result = self._workflow.ensure_setup_state(
                project_id=project_id,
                resolved_spec_path=resolved_spec_path,
                spec_hash=spec_hash,
                spec_version_id=spec_version_id,
                lease_guard=lease_guard,
                record_progress=record_progress,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error_code": WORKFLOW_SESSION_FAILED,
                "error": str(exc),
            }
        if not result.get("ok"):
            return result
        if not self._ledger.mark_step_complete(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            step="workflow_session_initialized",
            next_step="done",
            now=_now(),
        ):
            return {"ok": False, "error_code": MUTATION_RECOVERY_REQUIRED}
        return result

    def _validate_original_recovery_row(
        self,
        request: ProjectSetupRetryRequest,
    ) -> CliMutationLedger | dict[str, Any] | None:
        if request.recovery_mutation_event_id is None:
            with Session(self._engine) as session:
                unresolved = session.exec(
                    select(CliMutationLedger).where(
                        CliMutationLedger.command == PROJECT_CREATE_COMMAND,
                        CliMutationLedger.project_id == request.project_id,
                        CliMutationLedger.status == MutationStatus.RECOVERY_REQUIRED.value,
                    )
                ).first()
            if unresolved is not None:
                return _error(
                    MUTATION_RECOVERY_INVALID,
                    details={"project_id": request.project_id},
                    remediation=[
                        "Pass --recovery-mutation-event-id "
                        f"{unresolved.mutation_event_id}."
                    ],
                )
            return None
        row = self._get_ledger(request.recovery_mutation_event_id)
        if (
            row is None
            or row.command != PROJECT_CREATE_COMMAND
            or row.project_id != request.project_id
        ):
            return _error(
                MUTATION_RECOVERY_INVALID,
                details={
                    "project_id": request.project_id,
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                },
                remediation=["Re-read mutation state before retrying recovery."],
            )
        repaired = self._ledger.repair_setup_recovery_target(
            mutation_event_id=request.recovery_mutation_event_id,
            expected_command=PROJECT_CREATE_COMMAND,
            expected_project_id=request.project_id,
            now=_now(),
        )
        if repaired.error_code == MUTATION_IN_PROGRESS:
            return _error_for_ledger(MUTATION_IN_PROGRESS, repaired.ledger)
        if (
            repaired.error_code is not None
            or repaired.ledger.status != MutationStatus.RECOVERY_REQUIRED.value
        ):
            return _error(
                MUTATION_RECOVERY_INVALID,
                details={
                    "project_id": request.project_id,
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                    "status": repaired.ledger.status,
                },
                remediation=["Re-read mutation state before retrying recovery."],
            )
        return repaired.ledger

    def _guard_rejected_retry(
        self,
        *,
        request: ProjectSetupRetryRequest,
        code: str,
        details: dict[str, Any],
        remediation: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        loaded = self._ledger.create_or_load(
            command=PROJECT_SETUP_RETRY_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            request_hash=request.normalized_request_hash(),
            project_id=request.project_id,
            correlation_id=_correlation_id(request.correlation_id),
            changed_by=request.changed_by,
            lease_owner=_lease_owner(
                command=PROJECT_SETUP_RETRY_COMMAND,
                idempotency_key=_required(request.idempotency_key),
                correlation_id=request.correlation_id,
            ),
            now=_now(),
            recovers_mutation_event_id=request.recovery_mutation_event_id,
            lease_seconds=self._lease_seconds,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error_for_ledger(loaded.error_code, loaded.ledger)
        self._mark_simple_status(
            mutation_event_id=_event_id(loaded.ledger),
            lease_owner=_required(loaded.ledger.lease_owner),
            status=MutationStatus.GUARD_REJECTED,
            response=_error(
                code,
                details=details,
                remediation=remediation or ["Refresh state before retrying setup."],
                data=data,
            ),
        )
        return _error(
            code,
            details=details,
            remediation=remediation or ["Refresh state before retrying setup."],
            data=data,
        )

    def _transfer_retry_recovery(
        self,
        *,
        request: ProjectSetupRetryRequest,
        retry_mutation_event_id: int,
        retry_lease_owner: str,
        original_mutation_event_id: int,
        original_recovery_lease_owner: str,
        last_error: dict[str, Any],
    ) -> dict[str, Any]:
        retry_response = _error(
            MUTATION_RECOVERY_REQUIRED,
            details={
                "mutation_event_id": retry_mutation_event_id,
                "project_id": request.project_id,
            },
            remediation=["Run agileforge project setup retry after fixing the error."],
            data={
                "project_id": request.project_id,
                "mutation_event_id": retry_mutation_event_id,
                "recovery_mutation_event_id": original_mutation_event_id,
                "status": MutationStatus.RECOVERY_REQUIRED.value,
                "next_actions": [_retry_action(request, original_mutation_event_id)],
            },
        )
        original_replay_response = _error(
            MUTATION_RECOVERY_REQUIRED,
            details={
                "mutation_event_id": original_mutation_event_id,
                "recovered_by_mutation_event_id": retry_mutation_event_id,
            },
            remediation=["Resume recovery from the retry mutation event."],
            data={
                "project_id": request.project_id,
                "mutation_event_id": original_mutation_event_id,
                "recovered_by_mutation_event_id": retry_mutation_event_id,
                "retry_status": MutationStatus.RECOVERY_REQUIRED.value,
                "next_actions": [_retry_action(request, retry_mutation_event_id)],
            },
        )
        transferred = self._ledger.transfer_linked_retry_recovery(
            retry_mutation_event_id=retry_mutation_event_id,
            retry_lease_owner=retry_lease_owner,
            original_mutation_event_id=original_mutation_event_id,
            original_recovery_lease_owner=original_recovery_lease_owner,
            recovery_action=RecoveryAction.RESUME_FROM_STEP,
            safe_to_auto_resume=True,
            last_error=last_error,
            retry_response=retry_response,
            original_replay_response=original_replay_response,
            now=_now(),
        )
        if transferred.error_code == MUTATION_RESUME_CONFLICT:
            return _mutation_resume_conflict(
                retry_mutation_event_id=retry_mutation_event_id,
                original_mutation_event_id=original_mutation_event_id,
            )
        return retry_response

    def _mark_create_recovery_required(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        project_id: int,
        code: str,
        spec_file: str,
        safe_to_auto_resume: bool,
        spec_version_id: int | None = None,
    ) -> CliMutationLedger | dict[str, Any]:
        marked = self._ledger.mark_recovery_required(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            recovery_action=RecoveryAction.RESUME_FROM_STEP,
            safe_to_auto_resume=safe_to_auto_resume,
            last_error={
                "code": code,
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "spec_file": spec_file,
            },
            now=_now(),
        )
        if marked:
            return self._must_get_ledger(mutation_event_id)

        repaired = self._ledger.repair_setup_recovery_target(
            mutation_event_id=mutation_event_id,
            expected_command=PROJECT_CREATE_COMMAND,
            expected_project_id=project_id,
            now=_now(),
        )
        if (
            repaired.error_code is None
            and repaired.ledger.status == MutationStatus.RECOVERY_REQUIRED.value
        ):
            return repaired.ledger
        return _error_for_ledger(
            repaired.error_code or MUTATION_IN_PROGRESS,
            repaired.ledger,
        )

    def _mark_setup_validation_failed(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        project_id: int,
        requested_spec_file: str,
        resolved_spec_path: Path,
        error_code: str,
        error_summary: str,
        spec_hash: object,
        spec_version_id: object,
        failure_artifact_id: object = None,
        failure_stage: object = None,
        failure_summary: object = None,
        raw_output_preview: object = None,
        has_full_artifact: object = None,
        blocking_gaps: object = None,
    ) -> dict[str, Any]:
        setup_failure_summary = str(failure_summary or error_summary)
        artifact_failure_stage = _optional_str(failure_stage)
        setup_failure_first_error = _first_failure_error(blocking_gaps, error_summary)
        workflow_result = self._ensure_failed_workflow_setup(
            project_id=project_id,
            resolved_spec_path=resolved_spec_path,
            error_code=error_code,
            artifact_failure_stage=artifact_failure_stage,
            failure_summary=setup_failure_summary,
            failure_artifact_id=_optional_str(failure_artifact_id),
            first_error=setup_failure_first_error,
        )
        if not workflow_result.get("ok"):
            marked = self._mark_create_recovery_required(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                project_id=project_id,
                code=WORKFLOW_SESSION_FAILED,
                spec_file=requested_spec_file,
                safe_to_auto_resume=True,
                spec_version_id=_optional_int(spec_version_id),
            )
            if isinstance(marked, dict):
                return marked
            return _recovery_required_response(marked, requested_spec_file)

        data = {
            "project_id": project_id,
            "name": self._project_name(project_id),
            "resolved_spec_path": str(resolved_spec_path),
            "spec_hash": spec_hash,
            "spec_version_id": spec_version_id,
            "setup_status": "failed",
            "fsm_state": "SETUP_REQUIRED",
            "setup_error": error_code,
            "setup_failure_stage": "authority_compile",
            "setup_failure_summary": setup_failure_summary,
            "setup_failure_artifact_id": _optional_str(failure_artifact_id),
            "failure_artifact_stage": artifact_failure_stage,
            "setup_failure_first_error": setup_failure_first_error,
            "raw_output_preview": _optional_str(raw_output_preview),
            "has_full_artifact": _optional_bool(has_full_artifact),
            "mutation_event_id": mutation_event_id,
            "next_actions": [_failed_setup_retry_action(project_id, requested_spec_file)],
        }
        if isinstance(blocking_gaps, list):
            data["setup_failure_blocking_gaps"] = [str(item) for item in blocking_gaps]
        response = _error(
            error_code,
            details={
                "project_id": project_id,
                "mutation_event_id": mutation_event_id,
                "spec_version_id": spec_version_id,
                "failure_artifact_id": _optional_str(failure_artifact_id),
                "first_error": setup_failure_first_error,
            },
            remediation=[
                "Inspect the compiler/setup error, then run agileforge project setup retry."
            ],
            data=data,
        )
        if not self._mark_simple_status(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            status=MutationStatus.VALIDATION_FAILED,
            response=response,
        ):
            return _error(
                ErrorCode.MUTATION_RESUME_CONFLICT.value,
                details={"mutation_event_id": mutation_event_id},
                remediation=["Re-read mutation state before retrying setup."],
            )
        return response

    def _ensure_failed_workflow_setup(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        error_code: str,
        artifact_failure_stage: str | None,
        failure_summary: str,
        failure_artifact_id: str | None,
        first_error: str | None,
    ) -> dict[str, Any]:
        session_id = str(project_id)
        try:
            current = self._workflow.get_session_status(session_id)
            if current == {}:
                self._workflow.initialize_session(session_id=session_id)
                current = self._workflow.get_session_status(session_id)
            required_state = {
                "fsm_state": "SETUP_REQUIRED",
                "setup_status": "failed",
                "setup_error": error_code,
                "setup_failure_stage": "authority_compile",
                "setup_failure_summary": failure_summary,
                "setup_failure_artifact_id": failure_artifact_id,
                "failure_artifact_stage": artifact_failure_stage,
                "setup_failure_first_error": first_error,
                "setup_spec_file_path": str(resolved_spec_path),
            }
            merged = {**current, **required_state}
            if current != merged:
                self._workflow.update_session_status(session_id, required_state)
            return {
                "ok": True,
                "session_id": session_id,
                "state": self._workflow.get_session_status(session_id),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error_code": WORKFLOW_SESSION_FAILED, "error": str(exc)}

    def _mark_retry_domain_failed(
        self,
        *,
        retry_mutation_event_id: int,
        retry_lease_owner: str,
        response_data: dict[str, Any],
    ) -> None:
        self._mark_simple_status(
            mutation_event_id=retry_mutation_event_id,
            lease_owner=retry_lease_owner,
            status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
            response=_error(
                "MUTATION_FAILED",
                details={"mutation_event_id": retry_mutation_event_id},
                remediation=["Retry setup with a new idempotency key."],
                data=response_data,
            ),
        )

    def _mark_simple_status(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        status: MutationStatus,
        response: dict[str, Any],
    ) -> bool:
        now = _now()
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_LEDGER_MUTATION_EVENT_ID == mutation_event_id)
                .where(_LEDGER_STATUS == MutationStatus.PENDING.value)
                .where(_LEDGER_LEASE_OWNER == lease_owner)
                .where(_LEDGER_LEASE_EXPIRES_AT > db_now)
                .values(
                    status=status.value,
                    response_json=_json_dump(response),
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def _find_ledger(
        self,
        *,
        command: str,
        idempotency_key: str,
    ) -> CliMutationLedger | None:
        with Session(self._engine) as session:
            return session.exec(
                select(CliMutationLedger).where(
                    _LEDGER_COMMAND == command,
                    _LEDGER_IDEMPOTENCY_KEY == idempotency_key,
                )
            ).first()

    def _get_ledger(self, mutation_event_id: int) -> CliMutationLedger | None:
        with Session(self._engine) as session:
            return session.get(CliMutationLedger, mutation_event_id)

    def _must_get_ledger(self, mutation_event_id: int) -> CliMutationLedger:
        row = self._get_ledger(mutation_event_id)
        if row is None:
            raise ValueError(f"Mutation event {mutation_event_id} not found.")
        return row

    def _completed_steps(self, mutation_event_id: int) -> list[str]:
        return _completed_steps(self._must_get_ledger(mutation_event_id))

    def _project_id_for_event(self, mutation_event_id: int) -> int | None:
        return self._must_get_ledger(mutation_event_id).project_id

    def _project_exists(self, project_id: int) -> bool:
        with Session(self._engine) as session:
            return session.get(Product, project_id) is not None

    def _product_name_exists(self, name: str) -> bool:
        with Session(self._engine) as session:
            return session.exec(select(Product).where(Product.name == name)).first() is not None

    def _project_name(self, project_id: int) -> str:
        with Session(self._engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                raise ValueError(f"Project {project_id} not found.")
            return product.name

    def _existing_spec_version(self, project_id: int) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            spec = session.exec(
                select(SpecRegistry)
                .where(_SPEC_PRODUCT_ID == project_id)
                .order_by(_SPEC_VERSION_ID.desc())
            ).first()
            if (
                spec is None
                or spec.spec_version_id is None
                or spec.status != "approved"
            ):
                return None
            return {
                "spec_hash": spec.spec_hash,
                "spec_version_id": spec.spec_version_id,
            }

    def _existing_authority(self, project_id: int) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            spec = session.exec(
                select(SpecRegistry)
                .where(_SPEC_PRODUCT_ID == project_id)
                .order_by(_SPEC_VERSION_ID.desc())
            ).first()
            if spec is None or spec.spec_version_id is None:
                return None
            authority = session.exec(
                select(CompiledSpecAuthority)
                .where(CompiledSpecAuthority.spec_version_id == spec.spec_version_id)
                .order_by(cast("Any", CompiledSpecAuthority.authority_id).desc())
            ).first()
            if authority is None:
                return None
            return {
                "spec_hash": spec.spec_hash,
                "spec_version_id": spec.spec_version_id,
                "authority_id": authority.authority_id,
            }


def _validate_key_mode(
    *,
    dry_run: bool,
    idempotency_key: str | None,
    dry_run_id: str | None,
) -> None:
    if dry_run:
        if idempotency_key is not None:
            raise ValueError("idempotency_key is not allowed with dry_run")
        if dry_run_id is None:
            raise ValueError("dry_run_id is required with dry_run")
        _validate_token(name="dry_run_id", value=dry_run_id)
        return
    if idempotency_key is None:
        raise ValueError("idempotency_key is required for non-dry-run requests")
    _validate_token(name="idempotency_key", value=idempotency_key)
    if dry_run_id is not None:
        _validate_token(name="dry_run_id", value=dry_run_id)


def _validate_token(*, name: str, value: str) -> None:
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be ASCII") from exc
    if not 8 <= len(value) <= 128:
        raise ValueError(f"{name} must be 8-128 characters")
    if _KEY_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must match [A-Za-z0-9._:-]+")


def _create_request_hash(
    *,
    request: ProjectCreateRequest,
    resolved_spec_path: Path,
    spec_hash: str,
) -> str:
    return canonical_hash(
        {
            "command": PROJECT_CREATE_COMMAND,
            "name": request.name,
            "resolved_spec_path": str(resolved_spec_path),
            "spec_hash": spec_hash,
            "changed_by": request.changed_by,
        }
    )


def _retry_context_fingerprint(
    *,
    project_id: int,
    resolved_spec_path: Path,
    workflow_state: dict[str, Any],
) -> str:
    return setup_retry_context_fingerprint(
        project_id=project_id,
        resolved_spec_path=resolved_spec_path,
        workflow_state=workflow_state,
    )


def _spec_hash(path: Path) -> str:
    return setup_spec_hash(path)


def _spec_hash_or_error(path: Path) -> str | dict[str, Any]:
    """Return canonical structured spec hash or a structured spec-file error."""
    if not path.exists():
        return _error(
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            details={"spec_file": str(path)},
            remediation=["Create the spec file or pass the correct path."],
        )
    if not path.is_file():
        return _error(
            ErrorCode.SPEC_FILE_INVALID.value,
            details={"spec_file": str(path), "reason": "not_a_file"},
            remediation=["Pass a readable agileforge.spec.v1 JSON file."],
        )
    try:
        raw_content = path.read_text(encoding="utf-8")
        normalized = normalize_spec_content_for_registry(raw_content)
    except SpecContentNormalizationError as exc:
        remediation = [
            "Generate specs/spec.json as agileforge.spec.v1 JSON.",
            "Retry project create with --spec-file specs/spec.json.",
        ]
        return _error(
            exc.error_code,
            details={"spec_file": str(path), "reason": str(exc)},
            remediation=remediation,
        )
    except UnicodeDecodeError as exc:
        return _error(
            ErrorCode.SPEC_FILE_INVALID.value,
            details={"spec_file": str(path), "reason": str(exc)},
            remediation=["Save the specification file as UTF-8 JSON."],
        )
    except OSError as exc:
        return _error(
            ErrorCode.SPEC_FILE_INVALID.value,
            details={"spec_file": str(path), "reason": str(exc)},
            remediation=["Pass a readable agileforge.spec.v1 JSON file."],
        )
    else:
        return normalized.spec_hash


def _workflow_has_required_setup_state(
    state: dict[str, Any],
    spec_path: Path,
    *,
    spec_hash: str,
    spec_version_id: int,
    project_id: int,
) -> bool:
    expected_action = _authority_compile_action(
        project_id=project_id,
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        expected_setup_status=AUTHORITY_COMPILE_REQUIRED,
    )
    return (
        state.get("fsm_state") == "SETUP_REQUIRED"
        and state.get("setup_status") == AUTHORITY_COMPILE_REQUIRED
        and state.get("setup_error") is None
        and state.get("setup_spec_file_path") == str(spec_path)
        and state.get("setup_spec_hash") == spec_hash
        and state.get("setup_spec_version_id") == spec_version_id
        and state.get("setup_next_actions") == [expected_action]
    )


def _authority_compile_stale_guard(
    *,
    request: AuthorityCompileRequest,
    workflow_state: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    current_state = str(workflow_state.get("fsm_state") or "SETUP_REQUIRED")
    if current_state != request.expected_state:
        return (
            ErrorCode.STALE_STATE.value,
            {"expected_state": request.expected_state, "actual_state": current_state},
        )

    current_setup_status = str(workflow_state.get("setup_status") or "")
    if current_setup_status != request.expected_setup_status:
        return (
            ErrorCode.STALE_SETUP_STATUS.value,
            {
                "expected_setup_status": request.expected_setup_status,
                "actual_setup_status": current_setup_status,
            },
        )

    current_spec_hash = str(workflow_state.get("setup_spec_hash") or "")
    if current_spec_hash != request.expected_spec_hash:
        return (
            ErrorCode.STALE_SPEC_HASH.value,
            {
                "expected_spec_hash": request.expected_spec_hash,
                "actual_spec_hash": current_spec_hash,
            },
        )

    current_spec_version_id = _optional_int(workflow_state.get("setup_spec_version_id"))
    if current_spec_version_id != request.spec_version_id:
        return (
            ErrorCode.STALE_SPEC_VERSION.value,
            {
                "expected_spec_version_id": request.spec_version_id,
                "actual_spec_version_id": current_spec_version_id,
            },
        )

    return None


def _setup_failure_requires_recovery(error_code: str) -> bool:
    """Return true for mutation-fencing failures that need ledger recovery."""
    return error_code in {MUTATION_IN_PROGRESS, MUTATION_RECOVERY_REQUIRED}


def _optional_int(value: object) -> int | None:
    """Return an optional integer when the value is present and numeric."""
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    """Return an optional string when the value is present."""
    if value is None:
        return None
    return str(value)


def _optional_bool(value: object) -> bool | None:
    """Return an optional bool when the value is present."""
    if value is None:
        return None
    return bool(value)


def _first_failure_error(blocking_gaps: object, fallback: str) -> str | None:
    """Return the first actionable compiler validation error."""
    if isinstance(blocking_gaps, list) and blocking_gaps:
        return str(blocking_gaps[0])
    return fallback or None


def _failed_setup_retry_action(project_id: int, spec_file: str) -> dict[str, Any]:
    return {
        "command": PROJECT_SETUP_RETRY_COMMAND,
        "args": {
            "project_id": project_id,
            "spec_file": spec_file,
            "expected_state": "SETUP_REQUIRED",
            "expected_context_fingerprint": "<refresh-context-fingerprint>",
        },
        "reason": "Retry project setup after fixing the compiler/setup failure.",
    }


def _recovery_required_response(
    row: CliMutationLedger,
    spec_file: str,
) -> dict[str, Any]:
    project_id = row.project_id
    data = {
        "project_id": project_id,
        "mutation_event_id": row.mutation_event_id,
        "status": MutationStatus.RECOVERY_REQUIRED.value,
        "next_actions": [
            {
                "command": PROJECT_SETUP_RETRY_COMMAND,
                "args": {
                    "project_id": project_id,
                    "spec_file": spec_file,
                    "expected_state": "SETUP_REQUIRED",
                    "expected_context_fingerprint": "<refresh-context-fingerprint>",
                    "recovery_mutation_event_id": row.mutation_event_id,
                },
                "reason": "Retry project setup after resolving the recorded failure.",
            }
        ],
    }
    return _error(
        MUTATION_RECOVERY_REQUIRED,
        details={"mutation_event_id": row.mutation_event_id, "project_id": project_id},
        remediation=["Run agileforge project setup retry with stale guards."],
        data=data,
    )


def _authority_compile_recovery_required_response(
    row: CliMutationLedger,
    *,
    project_id: int,
    spec_hash: str,
    spec_version_id: int,
    expected_setup_status: str,
) -> dict[str, Any]:
    data = {
        "project_id": project_id,
        "mutation_event_id": row.mutation_event_id,
        "status": MutationStatus.RECOVERY_REQUIRED.value,
        "spec_hash": spec_hash,
        "spec_version_id": spec_version_id,
        "next_actions": [
            _authority_compile_action(
                project_id=project_id,
                spec_version_id=spec_version_id,
                spec_hash=spec_hash,
                expected_setup_status=expected_setup_status,
            )
        ],
    }
    return _error(
        MUTATION_RECOVERY_REQUIRED,
        details={"mutation_event_id": row.mutation_event_id, "project_id": project_id},
        remediation=["Run agileforge authority compile with refreshed stale guards."],
        data=data,
    )


def _retry_pre_side_effect_failure_data(
    *,
    request: ProjectSetupRetryRequest,
    retry_mutation_event_id: int,
    original_mutation_event_id: int | None,
) -> dict[str, Any]:
    return {
        "project_id": request.project_id,
        "mutation_event_id": retry_mutation_event_id,
        "recovery_mutation_event_id": original_mutation_event_id,
        "status": MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS.value,
        "side_effects_started": False,
        "original_status": MutationStatus.RECOVERY_REQUIRED.value,
        "next_actions": [_retry_action(request, original_mutation_event_id)],
    }


def _stale_context_retry_data(
    *,
    request: ProjectSetupRetryRequest,
    actual_context_fingerprint: str,
) -> dict[str, Any]:
    """Return a corrected retry action for stale setup context guards."""
    corrected_request = request.model_copy(
        update={"expected_context_fingerprint": actual_context_fingerprint}
    )
    return {
        "project_id": request.project_id,
        "stale_guard_reason": "context_fingerprint_changed",
        "next_actions": [
            _retry_action(corrected_request, request.recovery_mutation_event_id)
        ],
    }


def _retry_action(
    request: ProjectSetupRetryRequest,
    recovery_mutation_event_id: int | None,
) -> dict[str, Any]:
    return {
        "command": PROJECT_SETUP_RETRY_COMMAND,
        "args": {
            "project_id": request.project_id,
            "spec_file": request.spec_file,
            "expected_state": request.expected_state,
            "expected_context_fingerprint": request.expected_context_fingerprint,
            "recovery_mutation_event_id": recovery_mutation_event_id,
        },
        "reason": "Retry setup with a new idempotency key after fixing the reported error.",
    }


def _authority_compile_action(
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
    expected_setup_status: str,
) -> dict[str, Any]:
    return {
        "command": PROJECT_AUTHORITY_COMPILE_COMMAND,
        "args": {
            "project_id": project_id,
            "spec_version_id": spec_version_id,
            "expected_spec_hash": spec_hash,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": expected_setup_status,
        },
        "reason": "Compile pending authority before authority review.",
    }


def _cleared_authority_compile_failure_metadata() -> dict[str, None]:
    return dict.fromkeys(AUTHORITY_COMPILE_FAILURE_FIELDS, None)


def _authority_review_action(project_id: int) -> dict[str, Any]:
    return {
        "command": "agileforge authority review",
        "args": {"project_id": project_id},
        "reason": "Review pending compiled authority before acceptance.",
    }


def _mutation_resume_conflict(
    *,
    retry_mutation_event_id: int | None,
    original_mutation_event_id: int | None,
) -> dict[str, Any]:
    return _error(
        ErrorCode.MUTATION_RESUME_CONFLICT.value,
        details={
            "retry_mutation_event_id": retry_mutation_event_id,
            "original_mutation_event_id": original_mutation_event_id,
        },
        remediation=["Re-read mutation state before retrying recovery."],
    )


def _active_recovery_lease_blocks_retry(row: CliMutationLedger, *, now: datetime) -> bool:
    """Return whether another worker currently owns the recovery row."""
    return (
        row.status == MutationStatus.RECOVERY_REQUIRED.value
        and row.lease_owner is not None
        and row.lease_expires_at is not None
        and row.lease_expires_at > _db_datetime(now)
    )


def _error_for_ledger(code: str, row: CliMutationLedger) -> dict[str, Any]:
    if code == IDEMPOTENCY_KEY_REUSED:
        return _error(
            code,
            details={"mutation_event_id": row.mutation_event_id},
            remediation=["Use a new idempotency key for changed inputs."],
        )
    data = {
        "mutation_event_id": row.mutation_event_id,
        "command": row.command,
        "project_id": row.project_id,
        "status": row.status,
    }
    return _error(
        code,
        details={"mutation_event_id": row.mutation_event_id},
        remediation=[f"agileforge mutation show --mutation-event-id {row.mutation_event_id}"],
        data=data,
    )


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error(
    code: str,
    *,
    details: dict[str, Any],
    remediation: list[str],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        error = workbench_error(code, details=details, remediation=remediation).to_dict()
    except (KeyError, ValueError):
        error = {
            "code": code,
            "message": "Command failed.",
            "details": details,
            "remediation": remediation,
            "exit_code": 1,
            "retryable": code.startswith("MUTATION_"),
        }
    return {"ok": False, "data": data, "warnings": [], "errors": [error]}


def _lease_owner(
    *,
    command: str,
    idempotency_key: str,
    correlation_id: str | None,
) -> str:
    suffix = correlation_id or idempotency_key
    return f"{command}:{suffix}"


def _correlation_id(value: str | None) -> str:
    return value or str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _required(value: str | None) -> str:
    if value is None:
        raise ValueError("Required value was missing.")
    return value


def _required_int(value: int | None) -> int:
    if value is None:
        raise ValueError("Required integer was missing.")
    return value


def _event_id(row: CliMutationLedger) -> int:
    return _required_int(row.mutation_event_id)
