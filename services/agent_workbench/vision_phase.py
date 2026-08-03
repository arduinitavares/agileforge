"""Agent workbench Vision phase command runner."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

import anyio
from pydantic import TypeAdapter
from sqlmodel import Session, col, func, select

from models.core import Product
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import VisionArtifact, VisionArtifactDecision
from orchestrator_agent.agent_tools.product_vision_tool.tools import save_vision_tool
from repositories.product import ProductRepository
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.contracts.vision import OutputSchema
from services.phases.vision_service import (
    VisionPhaseError,
    generate_vision_draft,
    get_vision_history,
    save_vision_draft,
)
from services.vision_runtime import run_vision_agent_from_state
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project
from workflow.contracts import JsonObject
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from google.adk.tools import ToolContext
else:
    ToolContext = Any

_JSON_OBJECT = TypeAdapter(JsonObject)


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


class VisionPhaseRunner:
    """Run Vision phase commands through the same service boundary as the API."""

    def __init__(
        self,
        *,
        product_repo: ProductRepository | _ProductRepositoryLike | None = None,
        workflow_service: WorkflowService | _WorkflowServiceLike | None = None,
    ) -> None:
        """Initialize repositories for CLI Vision commands."""
        self._product_repo = product_repo or ProductRepository()
        self._workflow_service = workflow_service or WorkflowService()

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Vision draft."""
        return anyio.run(self._generate, project_id, user_input)

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Vision draft attempt history."""
        return anyio.run(self._history, project_id)

    def save(self, *, project_id: int) -> dict[str, Any]:
        """Persist the current complete Vision draft."""
        return anyio.run(self._save, project_id)

    async def _generate(
        self,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await generate_vision_draft(
                project_id=project_id,
                setup_blocker=_setup_blocker(product),
                load_state=lambda: self._load_vision_state(str(project_id), project_id),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                run_vision_agent=run_vision_agent_from_state,
                user_input=user_input,
            )
        except _ProjectHydrationError as exc:
            return _project_hydration_error(exc.error)
        except VisionPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if data.get("vision_run_success") is False:
            return _vision_runtime_error(project_id=project_id, data=data)
        return _data_envelope(data)

    async def _history(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await get_vision_history(
                load_state=lambda: self._ensure_session(str(project_id))
            )
        except VisionPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _save(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await save_vision_draft(
                project_id=project_id,
                project_name=product.name,
                setup_blocker=_setup_blocker(product),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                hydrate_context=lambda: self._hydrate_context(
                    str(project_id), project_id
                ),
                build_tool_context=_build_tool_context,
                save_vision_tool=save_vision_tool,
            )
        except _ProjectHydrationError as exc:
            return _project_hydration_error(exc.error)
        except VisionPhaseError as exc:
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

    async def _load_vision_state(
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
            raise _ProjectHydrationError(
                result.get("error", "Project hydration failed")
            )
        return context

    def _save_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        self._workflow_service.update_session_status(session_id, state)


def record_vision_draft_in_session(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
    canonical_content: JsonObject,
    content_fingerprint: str,
    supersedes_vision_artifact_id: int | None,
    artifact_id: int,
    actor: str,
    recorded_at: datetime,
) -> VisionArtifact:
    """Validate and append one immutable Vision artifact in caller transaction."""
    if session.get(Product, project_id) is None:
        message = f"Project {project_id} not found."
        raise ValueError(message)
    validated = OutputSchema.model_validate(canonical_content)
    normalized = _JSON_OBJECT.validate_python(validated.model_dump(mode="json"))
    if not validated.is_complete or not validated.updated_components.is_fully_defined():
        message = "Vision output is incomplete and cannot enter review."
        raise ValueError(message)
    if normalized != canonical_content:
        message = "Vision content must be the exact host-validated canonical output."
        raise ValueError(message)
    if canonical_hash(canonical_content) != content_fingerprint:
        message = "Vision content fingerprint does not match canonical content."
        raise ValueError(message)

    parent: VisionArtifact | None = None
    if supersedes_vision_artifact_id is not None:
        parent = session.exec(
            select(VisionArtifact).where(
                col(VisionArtifact.project_id) == project_id,
                col(VisionArtifact.vision_artifact_id) == supersedes_vision_artifact_id,
            )
        ).one_or_none()
        if parent is None:
            message = "Vision supersession parent does not belong to this Project."
            raise ValueError(message)

    version_number = (
        session.exec(
            select(func.count())
            .select_from(VisionArtifact)
            .where(col(VisionArtifact.project_id) == project_id)
        ).one()
        + 1
    )
    row = VisionArtifact(
        vision_artifact_id=artifact_id,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        version_number=version_number,
        canonical_content_json=canonical_json(canonical_content),
        content_fingerprint=content_fingerprint,
        supersedes_vision_artifact_id=(
            None if parent is None else parent.vision_artifact_id
        ),
        created_by=actor,
        created_at=recorded_at,
    )
    session.add(row)
    session.flush()
    return row


def record_vision_decision_in_session(  # noqa: PLR0913
    session: Session,
    *,
    artifact: VisionArtifact,
    decision: str,
    rationale: str,
    reviewer: str,
    idempotency_key: str,
    decided_at: datetime,
) -> VisionArtifactDecision:
    """Append one exact Vision decision and refresh only the legacy projection."""
    existing = session.exec(
        select(VisionArtifactDecision).where(
            col(VisionArtifactDecision.project_id) == artifact.project_id,
            col(VisionArtifactDecision.vision_artifact_id)
            == artifact.vision_artifact_id,
        )
    ).one_or_none()
    if existing is not None:
        message = "Vision artifact already has a terminal review decision."
        raise ValueError(message)
    row = VisionArtifactDecision(
        project_id=artifact.project_id,
        vision_artifact_id=artifact.vision_artifact_id,
        artifact_fingerprint=artifact.content_fingerprint,
        decision=decision,
        rationale=rationale,
        reviewer=reviewer,
        idempotency_key=idempotency_key,
        decided_at=decided_at,
    )
    session.add(row)
    if decision == "accepted":
        content = _JSON_OBJECT.validate_json(artifact.canonical_content_json)
        validated = OutputSchema.model_validate(content)
        project = session.get(Product, artifact.project_id)
        if project is None:
            message = f"Project {artifact.project_id} not found."
            raise ValueError(message)
        project.vision = validated.product_vision_statement
        session.add(project)
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.VISION_SAVED,
                product_id=artifact.project_id,
                timestamp=decided_at,
                event_metadata=canonical_json(
                    {
                        "action": "vision_artifact_accepted",
                        "vision_artifact_id": artifact.vision_artifact_id,
                        "artifact_fingerprint": artifact.content_fingerprint,
                        "authority_id": artifact.authority_id,
                        "authority_fingerprint": artifact.authority_fingerprint,
                    }
                ),
            )
        )
    session.flush()
    return row


def _now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_tool_context(context: object) -> ToolContext:
    """Return a lightweight ToolContext-compatible state holder."""
    return cast("ToolContext", context)


def _setup_blocker(product: Product) -> str | None:
    """Return why Vision must remain blocked, or None when setup passed."""
    if not getattr(product, "spec_file_path", None):
        return "Specification file path is required."
    if not getattr(product, "compiled_authority_json", None):
        return "Specification authority is missing. Run setup retry."
    return None


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


class _ProjectHydrationError(RuntimeError):
    """Raised when active project hydration fails before Vision execution."""

    def __init__(self, error: object) -> None:
        super().__init__(str(error))
        self.error = error


def _project_hydration_error(error: object) -> dict[str, Any]:
    """Map select_project failures onto the workbench error envelope."""
    if not isinstance(error, dict):
        return _error_envelope(ErrorCode.INVALID_COMMAND, str(error))

    error_dict = cast("dict[str, Any]", error)
    try:
        code = ErrorCode(str(error_dict.get("code", ErrorCode.INVALID_COMMAND.value)))
    except ValueError:
        code = ErrorCode.INVALID_COMMAND
    message = str(error_dict.get("message") or error_dict)
    details = error_dict.get("details")
    remediation = error_dict.get("remediation")
    return _error_envelope(
        code,
        message,
        details=details if isinstance(details, dict) else {},
        remediation=remediation if _is_string_list(remediation) else [],
    )


def _is_string_list(value: object) -> bool:
    """Return whether a value can be passed as remediation text."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _phase_error(exc: VisionPhaseError) -> dict[str, Any]:
    """Map Vision phase errors onto registered CLI errors."""
    message = exc.detail
    code = (
        ErrorCode.AUTHORITY_NOT_ACCEPTED
        if message.startswith("Setup required:")
        else ErrorCode.INVALID_COMMAND
    )
    return _error_envelope(code, message)


def _workflow_error(exc: RuntimeError) -> dict[str, Any]:
    """Map workflow persistence errors onto registered CLI errors."""
    return _error_envelope(ErrorCode.WORKFLOW_SESSION_FAILED, str(exc))


def _vision_runtime_error(*, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Map a recorded Vision runtime failure onto a hard CLI failure."""
    message = str(
        data.get("failure_summary") or data.get("error") or "Vision generation failed."
    )
    details = {
        "project_id": project_id,
        "vision_run_success": False,
        "failure_stage": data.get("failure_stage"),
        "failure_artifact_id": data.get("failure_artifact_id"),
        "attempt_count": data.get("attempt_count"),
        "fsm_state": data.get("fsm_state"),
        "model_info": data.get("model_info"),
    }
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        message,
        details={key: value for key, value in details.items() if value is not None},
        remediation=[
            "Inspect agileforge vision history --project-id <project_id>.",
            "Fix the Vision runtime/provider configuration or refine the input.",
        ],
    )
