"""Agent workbench Backlog phase command runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

import anyio
from sqlmodel import Session

from models.db import get_engine
from orchestrator_agent.agent_tools.backlog_primer.tools import save_backlog_tool
from repositories.product import ProductRepository
from services.agent_workbench.backlog_reconciliation import (
    BacklogReconciliationError,
    reconcile_active_backlog,
)
from services.agent_workbench.backlog_refinement_events import (
    BacklogRefinementApprovalError,
    BacklogRefinementApprovalRequest,
    record_backlog_refinement_approval,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.backlog_runtime import run_backlog_agent_from_state
from services.phases.backlog_service import (
    BacklogPhaseError,
    generate_backlog_draft,
    get_backlog_history,
    import_backlog_refinement,
    preview_backlog_draft,
    preview_backlog_refinement,
    record_backlog_refinement,
    save_backlog_draft,
)
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project

if TYPE_CHECKING:
    from google.adk.tools import ToolContext
    from sqlalchemy.engine import Engine

    from models.core import Product
else:
    ToolContext = Any

_REFINE_RECORD_IDEMPOTENCY_REUSED_MESSAGE = (
    "Backlog refinement idempotency key reused with different request"
)
_APPROVAL_IDEMPOTENCY_REUSED_MESSAGE = (
    "Idempotency key reused with different approval inputs."
)


class _ProductRepositoryLike(Protocol):
    def get_by_id(self, product_id: int) -> object: ...


class _WorkflowServiceLike(Protocol):
    def get_session_status(self, session_id: str) -> dict[str, Any]: ...
    async def initialize_session(self, *, session_id: str) -> object: ...
    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None: ...


class BacklogPhaseRunner:
    """Run Backlog phase commands through the same service boundary as the API."""

    def __init__(
        self,
        *,
        product_repo: ProductRepository | _ProductRepositoryLike | None = None,
        workflow_service: WorkflowService | _WorkflowServiceLike | None = None,
        engine: Engine | None = None,
    ) -> None:
        """Initialize repositories for CLI Backlog commands."""
        self._product_repo = product_repo or ProductRepository()
        self._workflow_service = workflow_service or WorkflowService()
        self._engine = engine

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Backlog draft."""
        return anyio.run(self._generate, project_id, user_input)

    def preview(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate a non-persisted Backlog preview."""
        return anyio.run(self._preview, project_id, user_input)

    def refine_preview(
        self,
        *,
        project_id: int,
        source_attempt_id: str | None = None,
        operations_file: str | None = None,
        source_artifact: str | None = None,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Preview canonical Backlog refinement operations."""
        return anyio.run(
            self._refine_preview,
            project_id,
            source_attempt_id,
            operations_file,
            source_artifact,
            user_input,
        )

    def refine_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        source_attempt_id: str,
        operations_file: str,
        expected_source_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Record canonical Backlog refinement operations."""
        return anyio.run(
            self._refine_record,
            project_id,
            source_attempt_id,
            operations_file,
            expected_source_fingerprint,
            expected_state,
            idempotency_key,
            approval_id,
        )

    def approve(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        approved_artifact_fingerprint: str,
        idempotency_key: str,
        source_attempt_id: str | None = None,
        attempt_id: str | None = None,
        operation_set_fingerprint: str | None = None,
        approved_operation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record host-mediated Backlog refinement approval."""
        return anyio.run(
            self._approve,
            project_id,
            source_attempt_id,
            attempt_id,
            operation_set_fingerprint,
            approved_artifact_fingerprint,
            approved_operation_ids,
            idempotency_key,
        )

    def refine_import(
        self,
        *,
        project_id: int,
        source_artifact: str,
        edited_file: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Import deterministic Backlog refinements from edited artifacts."""
        return anyio.run(
            self._refine_import,
            project_id,
            source_artifact,
            edited_file,
            expected_source_fingerprint,
            idempotency_key,
        )

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog draft attempt history."""
        return anyio.run(self._history, project_id)

    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Backlog draft."""
        return anyio.run(
            self._save,
            project_id,
            attempt_id,
            expected_artifact_fingerprint,
            expected_state,
            idempotency_key,
        )

    def reconcile(
        self,
        *,
        project_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Repair legacy duplicate active Backlog seed rows."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product
        try:
            data = reconcile_active_backlog(
                project_id=project_id,
                idempotency_key=idempotency_key,
            )
        except BacklogReconciliationError as exc:
            return _error_envelope(
                ErrorCode.MUTATION_FAILED,
                exc.detail,
                details=exc.details,
                remediation=[
                    "Inspect active backlog rows before retrying reconciliation.",
                    (
                        "If any row has progressed downstream, do not replace the "
                        "backlog; resolve downstream state first."
                    ),
                ],
            )
        return _data_envelope(data)

    async def _generate(
        self,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await generate_backlog_draft(
                project_id=project_id,
                load_state=lambda: self._load_backlog_state(
                    str(project_id), project_id
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                run_backlog_agent=run_backlog_agent_from_state,
                user_input=user_input,
            )
        except BacklogPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if data.get("backlog_run_success") is False:
            return _backlog_runtime_error(project_id=project_id, data=data)
        return _data_envelope(data)

    async def _preview(
        self,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await preview_backlog_draft(
                project_id=project_id,
                load_state=lambda: self._load_backlog_state(
                    str(project_id), project_id
                ),
                run_backlog_agent=run_backlog_agent_from_state,
                user_input=user_input,
            )
        except BacklogPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if data.get("backlog_run_success") is False:
            return _backlog_runtime_error(project_id=project_id, data=data)
        return _data_envelope(data)

    async def _refine_preview(
        self,
        project_id: int,
        source_attempt_id: str | None,
        operations_file: str | None,
        source_artifact: str | None,
        user_input: str | None,
    ) -> dict[str, Any]:
        unsupported_error = _unsupported_refine_preview_arg_error(
            source_artifact=source_artifact,
            user_input=user_input,
        )
        if unsupported_error is not None:
            return unsupported_error
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            operations_payload = _load_operations_payload(
                operations_file=operations_file,
                source_attempt_id=source_attempt_id,
            )
            data = await preview_backlog_refinement(
                project_id=project_id,
                load_state=lambda: self._load_backlog_state(
                    str(project_id), project_id
                ),
                operations_payload=operations_payload,
                now_iso=_now_iso,
            )
        except BacklogPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _refine_record(  # noqa: PLR0913
        self,
        project_id: int,
        source_attempt_id: str,
        operations_file: str,
        expected_source_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
        approval_id: str | None,
    ) -> dict[str, Any]:
        if isinstance(approval_id, str) and approval_id.strip():
            return _error_envelope(
                ErrorCode.INVALID_COMMAND,
                (
                    "backlog refine-record --approval-id is not supported until "
                    "approval binding is implemented."
                ),
            )
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            operations_payload = _load_operations_payload(
                operations_file=operations_file,
                source_attempt_id=source_attempt_id,
            )
            data = await record_backlog_refinement(
                project_id=project_id,
                load_state=lambda: self._load_backlog_state(
                    str(project_id), project_id
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                operations_payload=operations_payload,
                expected_source_fingerprint=expected_source_fingerprint,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                now_iso=_now_iso,
            )
        except BacklogPhaseError as exc:
            if exc.detail == _REFINE_RECORD_IDEMPOTENCY_REUSED_MESSAGE:
                return _error_envelope(ErrorCode.IDEMPOTENCY_KEY_REUSED, exc.detail)
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _approve(  # noqa: PLR0913
        self,
        project_id: int,
        source_attempt_id: str | None,
        attempt_id: str | None,
        operation_set_fingerprint: str | None,
        approved_artifact_fingerprint: str,
        approved_operation_ids: list[str] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            request = BacklogRefinementApprovalRequest.model_validate(
                {
                    "project_id": project_id,
                    "source_attempt_id": source_attempt_id,
                    "attempt_id": attempt_id,
                    "operation_set_fingerprint": operation_set_fingerprint,
                    "approved_artifact_fingerprint": approved_artifact_fingerprint,
                    "approved_operation_ids": approved_operation_ids or [],
                    "approval_source": "cli",
                    "idempotency_key": idempotency_key,
                    "approved_by": "po",
                }
            )
            engine = self._engine or get_engine()
            with Session(engine) as session:
                data = record_backlog_refinement_approval(
                    session,
                    request=request,
                    now_iso=_now_iso,
                )
        except ValueError as exc:
            return _error_envelope(ErrorCode.INVALID_COMMAND, str(exc))
        except BacklogRefinementApprovalError as exc:
            if str(exc) == _APPROVAL_IDEMPOTENCY_REUSED_MESSAGE:
                return _error_envelope(ErrorCode.IDEMPOTENCY_KEY_REUSED, str(exc))
            return _error_envelope(ErrorCode.MUTATION_FAILED, str(exc))
        return _data_envelope(cast("dict[str, Any]", data))

    async def _refine_import(
        self,
        project_id: int,
        source_artifact: str,
        edited_file: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product
        try:
            source_payload = _load_json_object_file(
                path=source_artifact,
                label="source_artifact",
            )
            edited_payload = _load_json_object_file(
                path=edited_file,
                label="edited_file",
            )
            data = await import_backlog_refinement(
                project_id=project_id,
                load_state=lambda: self._load_backlog_state(
                    str(project_id), project_id
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                source_artifact=source_payload,
                edited_artifact=edited_payload,
                expected_source_fingerprint=expected_source_fingerprint,
                idempotency_key=idempotency_key,
                now_iso=_now_iso,
            )
        except (TypeError, ValueError) as exc:
            return _error_envelope(ErrorCode.INVALID_COMMAND, str(exc))
        except BacklogPhaseError as exc:
            return _refine_import_phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _history(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await get_backlog_history(
                load_state=lambda: self._ensure_session(str(project_id))
            )
        except BacklogPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _save(
        self,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await save_backlog_draft(
                project_id=project_id,
                project_name=product.name,
                attempt_id=attempt_id,
                expected_artifact_fingerprint=expected_artifact_fingerprint,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                hydrate_context=lambda: self._hydrate_context(
                    str(project_id), project_id
                ),
                build_tool_context=_build_tool_context,
                save_backlog_tool=save_backlog_tool,
            )
        except BacklogPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    def _load_project(self, project_id: int) -> Product | dict[str, Any]:
        product = self._product_repo.get_by_id(project_id)
        if product is not None:
            return cast("Product", product)
        return _error_envelope(
            ErrorCode.PROJECT_NOT_FOUND,
            f"Project {project_id} not found.",
            details={"project_id": project_id},
            remediation=["Run agileforge project list."],
        )

    async def _ensure_session(self, session_id: str) -> dict[str, Any]:
        state = self._workflow_service.get_session_status(session_id) or {}
        if not state.get("fsm_state"):
            await self._workflow_service.initialize_session(session_id=session_id)
            state = self._workflow_service.get_session_status(session_id) or {}
        return state

    async def _load_backlog_state(
        self,
        session_id: str,
        project_id: int,
    ) -> dict[str, Any]:
        """Load workflow state with active project, spec, and authority hydrated."""
        context = await self._hydrate_context(session_id, project_id)
        return dict(context.state)

    async def _hydrate_context(
        self,
        session_id: str,
        project_id: int,
    ) -> SimpleNamespace:
        state = await self._ensure_session(session_id)
        context = SimpleNamespace(state=dict(state), session_id=session_id)
        result = select_project(project_id, _build_tool_context(context))
        if not result.get("success"):
            raise BacklogPhaseError(
                str(result.get("error", "Project hydration failed"))
            )
        _hydrate_vision_assessment_from_active_project(context.state)
        _assert_required_context(context.state)
        return context

    def _save_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        self._workflow_service.update_session_status(session_id, state)


def _now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_tool_context(context: object) -> ToolContext:
    """Return a lightweight ToolContext-compatible state holder."""
    return cast("ToolContext", context)


def _load_operations_payload(
    *,
    operations_file: str | None,
    source_attempt_id: str | None,
) -> dict[str, Any]:
    """Load and normalize a Backlog refinement operations JSON file."""
    if operations_file is None or not operations_file.strip():
        message = "Backlog refinement operations_file is required"
        raise BacklogPhaseError(message)
    try:
        payload = json.loads(Path(operations_file).read_text(encoding="utf-8"))
    except OSError as exc:
        message = f"Backlog refinement operations_file could not be read: {exc}"
        raise BacklogPhaseError(message) from exc
    except json.JSONDecodeError as exc:
        message = f"Backlog refinement operations_file is invalid JSON: {exc}"
        raise BacklogPhaseError(message) from exc
    if not isinstance(payload, dict):
        message = "Backlog refinement operations_file must contain a JSON object"
        raise BacklogPhaseError(message)
    if source_attempt_id is None:
        return payload

    existing_attempt_id = payload.get("source_attempt_id")
    if existing_attempt_id in (None, ""):
        return {**payload, "source_attempt_id": source_attempt_id}
    if existing_attempt_id != source_attempt_id:
        message = "Backlog refinement source_attempt_id conflicts with operations_file"
        raise BacklogPhaseError(message)
    return payload


def _load_json_object_file(*, path: str, label: str) -> dict[str, Any]:
    """Load a JSON object from a command file argument."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        message = f"Backlog refinement {label} could not be read: {exc}"
        raise ValueError(message) from exc
    except json.JSONDecodeError as exc:
        message = f"Backlog refinement {label} is invalid JSON: {exc}"
        raise ValueError(message) from exc
    if not isinstance(payload, dict):
        message = f"Backlog refinement {label} must contain a JSON object"
        raise TypeError(message)
    return payload


def _refine_import_phase_error(exc: BacklogPhaseError) -> dict[str, Any]:
    if exc.detail == _REFINE_RECORD_IDEMPOTENCY_REUSED_MESSAGE:
        return _error_envelope(ErrorCode.IDEMPOTENCY_KEY_REUSED, exc.detail)
    if "ambiguous" in exc.detail:
        return _error_envelope(ErrorCode.MUTATION_FAILED, exc.detail)
    return _phase_error(exc)


def _unsupported_refine_preview_arg_error(
    *,
    source_artifact: str | None,
    user_input: str | None,
) -> dict[str, Any] | None:
    """Return an error for Task 7/NLP inputs not supported by typed operations."""
    if isinstance(source_artifact, str) and source_artifact.strip():
        return _error_envelope(
            ErrorCode.INVALID_COMMAND,
            (
                "backlog refine-preview --source-artifact is not supported until "
                "deterministic import is implemented."
            ),
        )
    if isinstance(user_input, str) and user_input.strip():
        return _error_envelope(
            ErrorCode.INVALID_COMMAND,
            (
                "backlog refine-preview --input is not supported for typed "
                "refinement operations."
            ),
        )
    return None


def _hydrate_vision_assessment_from_active_project(state: dict[str, Any]) -> None:
    """Backfill the saved Vision text into runtime context when needed."""
    if isinstance(state.get("product_vision_assessment"), dict):
        return
    active_project = state.get("active_project")
    if not isinstance(active_project, dict):
        return
    vision = active_project.get("vision")
    if isinstance(vision, str) and vision.strip():
        state["product_vision_assessment"] = {
            "product_vision_statement": vision,
            "is_complete": True,
        }


def _assert_required_context(state: dict[str, Any]) -> None:
    """Block Backlog runtime if hydrated context is missing semantic inputs."""
    missing: list[str] = []
    if not state.get("pending_spec_content"):
        missing.append("pending_spec_content")
    if not state.get("compiled_authority_cached"):
        missing.append("compiled_authority_cached")
    assessment = state.get("product_vision_assessment")
    vision = (
        assessment.get("product_vision_statement")
        if isinstance(assessment, dict)
        else None
    )
    if not isinstance(vision, str) or not vision.strip():
        missing.append("product_vision_assessment.product_vision_statement")
    if missing:
        raise BacklogPhaseError(
            "Setup required: Backlog context hydration missing " + ", ".join(missing)
        )


def _data_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Return application facade success envelope."""
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error_envelope(
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    remediation: list[str] | None = None,
) -> dict[str, Any]:
    """Return application facade failure envelope."""
    return {
        "ok": False,
        "data": None,
        "warnings": [],
        "errors": [
            workbench_error(
                code,
                message=message,
                details=details or {},
                remediation=remediation or [],
            ).to_dict()
        ],
    }


def _phase_error(exc: BacklogPhaseError) -> dict[str, Any]:
    """Map Backlog phase errors onto registered CLI errors."""
    message = exc.detail
    code = (
        ErrorCode.AUTHORITY_NOT_ACCEPTED
        if message.startswith("Setup required")
        else ErrorCode.INVALID_COMMAND
    )
    return _error_envelope(code, message)


def _workflow_error(exc: RuntimeError) -> dict[str, Any]:
    """Map workflow persistence errors onto registered CLI errors."""
    return _error_envelope(ErrorCode.WORKFLOW_SESSION_FAILED, str(exc))


def _backlog_runtime_error(*, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Map a recorded Backlog runtime failure onto a hard CLI failure."""
    message = str(
        data.get("failure_summary") or data.get("error") or "Backlog generation failed."
    )
    details = {
        "project_id": project_id,
        "backlog_run_success": False,
        "failure_stage": data.get("failure_stage"),
        "failure_artifact_id": data.get("failure_artifact_id"),
        "attempt_count": data.get("attempt_count"),
        "fsm_state": data.get("fsm_state"),
    }
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        message,
        details={key: value for key, value in details.items() if value is not None},
        remediation=[
            "Inspect agileforge backlog history --project-id <project_id>.",
            "Fix the Backlog runtime/provider configuration or refine the input.",
        ],
    )
