"""Agent workbench Sprint phase command runner."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import anyio
from sqlmodel import Session, select

from models.core import Sprint
from models.db import get_engine
from models.enums import SprintStatus
from orchestrator_agent.agent_tools.sprint_planner_tool.tools import (
    save_sprint_plan_tool,
)
from repositories.product import ProductRepository
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.phases import workflow_state
from services.phases.sprint_service import (
    SprintPhaseError,
    generate_sprint_plan,
    get_sprint_history,
    save_sprint_plan,
)
from services.sprint_runtime import run_sprint_agent_from_state
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project

if TYPE_CHECKING:
    from google.adk.tools import ToolContext

    from models.core import Product
else:
    ToolContext = Any


class SprintPhaseRunner:
    """Run Sprint phase commands through the same service boundary as the API."""

    def __init__(
        self,
        *,
        product_repo: ProductRepository | None = None,
        workflow_service: WorkflowService | None = None,
    ) -> None:
        """Initialize repositories for CLI Sprint commands."""
        self._product_repo = product_repo or ProductRepository()
        self._workflow_service = workflow_service or WorkflowService()

    def generate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        user_input: str | None = None,
        selected_story_ids: list[int] | None = None,
        team_velocity_assumption: str = "Medium",
        sprint_duration_days: int = 14,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> dict[str, Any]:
        """Generate or refine a Sprint draft."""
        return anyio.run(
            self._generate,
            project_id,
            user_input,
            selected_story_ids,
            team_velocity_assumption,
            sprint_duration_days,
            max_story_points,
            include_task_decomposition,
        )

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Sprint draft attempt history."""
        return anyio.run(self._history, project_id)

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        sprint_start_date: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Sprint draft."""
        return anyio.run(
            self._save,
            project_id,
            team_name,
            sprint_start_date,
            attempt_id,
            expected_artifact_fingerprint,
            expected_state,
            idempotency_key,
        )

    async def _generate(  # noqa: PLR0913
        self,
        project_id: int,
        user_input: str | None,
        selected_story_ids: list[int] | None,
        team_velocity_assumption: str,
        sprint_duration_days: int,
        max_story_points: int | None,
        include_task_decomposition: bool,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await generate_sprint_plan(
                project_id=project_id,
                load_state=lambda: self._ensure_session(str(project_id)),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                current_planned_sprint_id=self._current_planned_sprint_id(project_id),
                now_iso=_now_iso,
                run_sprint_agent=run_sprint_agent_from_state,
                failure_meta_builder=lambda source, fallback_summary=None: (
                    workflow_state.failure_meta(
                        source,
                        fallback_summary=fallback_summary,
                    )
                ),
                team_velocity_assumption=team_velocity_assumption,
                sprint_duration_days=sprint_duration_days,
                max_story_points=max_story_points,
                include_task_decomposition=include_task_decomposition,
                selected_story_ids=selected_story_ids,
                user_input=user_input,
            )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if data.get("sprint_run_success") is False:
            return _sprint_runtime_error(project_id=project_id, data=data)
        return _data_envelope(data)

    async def _history(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await get_sprint_history(
                load_state=lambda: self._ensure_session(str(project_id)),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                current_planned_sprint_id=self._current_planned_sprint_id(project_id),
            )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _save(  # noqa: PLR0913
        self,
        project_id: int,
        team_name: str,
        sprint_start_date: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await save_sprint_plan(
                project_id=project_id,
                load_state=lambda: self._ensure_session(str(project_id)),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                current_planned_sprint_id=self._current_planned_sprint_id(project_id),
                now_iso=_now_iso,
                hydrate_context=self._hydrate_context,
                build_tool_context=_build_tool_context,
                save_plan_tool=save_sprint_plan_tool,
                team_name=team_name,
                sprint_start_date=sprint_start_date,
                attempt_id=attempt_id,
                expected_artifact_fingerprint=expected_artifact_fingerprint,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
            )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    def _load_project(self, project_id: int) -> Product | dict[str, Any]:
        product = self._product_repo.get_by_id(project_id)
        if product is not None:
            return product
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

    async def _hydrate_context(
        self,
        session_id: str,
        project_id: int,
    ) -> SimpleNamespace:
        state = await self._ensure_session(session_id)
        context = SimpleNamespace(state=dict(state), session_id=session_id)
        result = select_project(project_id, _build_tool_context(context))
        if not result.get("success"):
            raise SprintPhaseError(str(result.get("error", "Project hydration failed")))
        return context

    def _save_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        self._workflow_service.update_session_status(session_id, state)

    def _current_planned_sprint_id(self, project_id: int) -> int | None:
        with Session(get_engine()) as session:
            sprint = session.exec(
                select(Sprint)
                .where(
                    Sprint.product_id == project_id,
                    Sprint.status == SprintStatus.PLANNED,
                )
                .order_by(
                    cast("Any", Sprint.updated_at).desc(),
                    cast("Any", Sprint.sprint_id).desc(),
                )
            ).first()
            return sprint.sprint_id if sprint else None


def _now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_tool_context(context: object) -> ToolContext:
    """Return a lightweight ToolContext-compatible state holder."""
    return cast("ToolContext", context)


def _flatten_phase_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten phase service payloads for CLI consumers."""
    payload: dict[str, Any] = {
        str(key): value for key, value in data.items() if key != "data"
    }
    inner = data.get("data")
    if isinstance(inner, dict):
        payload.update({str(key): value for key, value in inner.items()})
    return payload


def _data_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Return application facade success envelope."""
    return {
        "ok": True,
        "data": _flatten_phase_payload(data),
        "warnings": [],
        "errors": [],
    }


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


def _phase_error(exc: SprintPhaseError) -> dict[str, Any]:
    """Map Sprint phase errors onto registered CLI errors."""
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


def _sprint_runtime_error(*, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Map a recorded Sprint runtime failure onto a hard CLI failure."""
    message = str(
        data.get("failure_summary") or data.get("error") or "Sprint generation failed."
    )
    details = {
        "project_id": project_id,
        "sprint_run_success": False,
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
            "Inspect agileforge sprint history --project-id <project_id>.",
            "Fix the Sprint runtime/provider configuration or refine the input.",
        ],
    )
