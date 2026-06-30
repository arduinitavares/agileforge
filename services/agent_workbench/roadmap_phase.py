"""Agent workbench Roadmap phase command runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

import anyio
from sqlmodel import Session, select

from models.core import UserStory
from models.db import get_engine
from orchestrator_agent.agent_tools.roadmap_builder.tools import save_roadmap_tool
from repositories.product import ProductRepository
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.execution_guard import AcceptedAuthorityExecutionGuard
from services.phases.roadmap_service import (
    RoadmapPhaseError,
    generate_roadmap_draft,
    get_roadmap_history,
    save_roadmap_draft,
)
from services.roadmap_runtime import run_roadmap_agent_from_state
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project

if TYPE_CHECKING:
    from google.adk.tools import ToolContext

    from models.core import Product
else:
    ToolContext = Any


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


class RoadmapPhaseRunner:
    """Run Roadmap phase commands through the same service boundary as the API."""

    def __init__(
        self,
        *,
        product_repo: ProductRepository | _ProductRepositoryLike | None = None,
        workflow_service: WorkflowService | _WorkflowServiceLike | None = None,
    ) -> None:
        """Initialize repositories for CLI Roadmap commands."""
        self._product_repo = product_repo or ProductRepository()
        self._workflow_service = workflow_service or WorkflowService()

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Roadmap draft."""
        return anyio.run(self._generate, project_id, user_input)

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Roadmap draft attempt history."""
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
        """Persist the current complete Roadmap draft."""
        return anyio.run(
            self._save,
            project_id,
            attempt_id,
            expected_artifact_fingerprint,
            expected_state,
            idempotency_key,
        )

    async def _generate(
        self,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product
        authority_error = self._accepted_authority_error(project_id)
        if authority_error is not None:
            return authority_error

        try:
            data = await generate_roadmap_draft(
                project_id=project_id,
                load_state=lambda: self._load_roadmap_state(
                    str(project_id), project_id
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                run_roadmap_agent=run_roadmap_agent_from_state,
                user_input=user_input,
            )
        except RoadmapPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if data.get("roadmap_run_success") is False:
            return _roadmap_runtime_error(project_id=project_id, data=data)
        return _data_envelope(data)

    async def _history(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await get_roadmap_history(
                load_state=lambda: self._ensure_session(str(project_id))
            )
        except RoadmapPhaseError as exc:
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
            data = await save_roadmap_draft(
                project_id=project_id,
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
                save_roadmap_tool=save_roadmap_tool,
            )
        except RoadmapPhaseError as exc:
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

    async def _load_roadmap_state(
        self,
        session_id: str,
        project_id: int,
    ) -> dict[str, Any]:
        """Load workflow state with active project, spec, authority, and backlog."""
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
            raise RoadmapPhaseError(
                str(result.get("error", "Project hydration failed"))
            )
        _hydrate_vision_assessment_from_active_project(context.state)
        _hydrate_saved_roadmap_from_active_project(context.state)
        _hydrate_active_backlog_from_db(context.state, project_id=project_id)
        _assert_required_context(context.state)
        return context

    def _save_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        self._workflow_service.update_session_status(session_id, state)

    def _accepted_authority_error(self, project_id: int) -> dict[str, Any] | None:
        """Return an execution guard error for real repository-backed commands."""
        if not isinstance(self._product_repo, ProductRepository):
            return None
        return AcceptedAuthorityExecutionGuard(
            engine=get_engine()
        ).reject_unless_current(project_id=project_id)


def _now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_tool_context(context: object) -> ToolContext:
    """Return a lightweight ToolContext-compatible state holder."""
    return cast("ToolContext", context)


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


def _hydrate_saved_roadmap_from_active_project(state: dict[str, Any]) -> None:
    """Use the persisted Product roadmap as the normal reconciliation base."""
    active_project = state.get("active_project")
    if not isinstance(active_project, dict):
        return
    roadmap = active_project.get("roadmap")
    if not roadmap:
        return

    parsed: Any = roadmap
    if isinstance(roadmap, str):
        try:
            parsed = json.loads(roadmap)
        except json.JSONDecodeError:
            return

    if isinstance(parsed, dict):
        parsed = parsed.get("roadmap_releases")

    if isinstance(parsed, list) and parsed:
        releases = [release for release in parsed if isinstance(release, dict)]
        if releases:
            state["roadmap_releases"] = releases


def _hydrate_active_backlog_from_db(
    state: dict[str, Any],
    *,
    project_id: int,
) -> None:
    """Backfill canonical active backlog items from persisted seed stories."""
    if _scope_extension_backlog_saved(state):
        _hydrate_scope_extension_backlog_from_db(state, project_id=project_id)
        return

    backlog_items = state.get("backlog_items")
    force_db_reload = bool(state.get("active_backlog_reset_attempt_id"))
    if not force_db_reload and isinstance(backlog_items, list) and backlog_items:
        return

    with Session(get_engine()) as session:
        stories = session.exec(
            select(UserStory)
            .where(UserStory.product_id == project_id)
            .where(UserStory.story_origin == "backlog_seed")
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(cast("Any", UserStory.rank), cast("Any", UserStory.story_id))
        ).all()

    if not stories:
        return

    state["backlog_items"] = [
        {
            "priority": _priority_from_story(story, fallback=index),
            "requirement": story.title,
            "value_driver": "Strategic",
            "justification": story.story_description or story.title,
            "estimated_effort": _effort_from_points(story.story_points),
        }
        for index, story in enumerate(stories, start=1)
    ]


def _scope_extension_backlog_saved(state: dict[str, Any]) -> bool:
    context = state.get("scope_extension_context")
    return (
        isinstance(context, dict)
        and bool(context.get("backlog_extension_saved_at"))
        and not bool(context.get("roadmap_extension_saved_at"))
    )


def _scope_extension_amended_spec_version_id(state: dict[str, Any]) -> int | None:
    context = state.get("scope_extension_context")
    if not isinstance(context, dict):
        return None
    value = context.get("amended_spec_version_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _hydrate_scope_extension_backlog_from_db(
    state: dict[str, Any],
    *,
    project_id: int,
) -> None:
    amended_spec_version_id = _scope_extension_amended_spec_version_id(state)
    with Session(get_engine()) as session:
        statement = (
            select(UserStory)
            .where(UserStory.product_id == project_id)
            .where(UserStory.story_origin == "scope_extension")
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(cast("Any", UserStory.rank), cast("Any", UserStory.story_id))
        )
        if amended_spec_version_id is not None:
            statement = statement.where(
                UserStory.accepted_spec_version_id == amended_spec_version_id
            )
        stories = session.exec(statement).all()

    if not stories:
        return

    state["backlog_items"] = [
        {
            "priority": _priority_from_story(story, fallback=index),
            "requirement": story.title,
            "value_driver": "Strategic",
            "justification": story.story_description or story.title,
            "estimated_effort": _effort_from_points(story.story_points),
            "story_origin": "scope_extension",
            "accepted_spec_version_id": story.accepted_spec_version_id,
        }
        for index, story in enumerate(stories, start=1)
    ]


def _priority_from_story(story: UserStory, *, fallback: int) -> int:
    if story.rank and str(story.rank).strip().isdigit():
        return int(str(story.rank).strip())
    if story.refinement_slot is not None:
        return story.refinement_slot
    return fallback


def _effort_from_points(points: int | None) -> str:
    if points is None or points <= 1:
        return "S"
    if points <= 3:  # noqa: PLR2004
        return "M"
    if points <= 5:  # noqa: PLR2004
        return "L"
    return "XL"


def _assert_required_context(state: dict[str, Any]) -> None:
    """Block Roadmap runtime if hydrated context is missing semantic inputs."""
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
    backlog_items = state.get("backlog_items")
    if not isinstance(backlog_items, list) or not backlog_items:
        missing.append("backlog_items")
    if missing:
        raise RoadmapPhaseError(
            "Setup required: Roadmap context hydration missing " + ", ".join(missing)
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


def _phase_error(exc: RoadmapPhaseError) -> dict[str, Any]:
    """Map Roadmap phase errors onto registered CLI errors."""
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


def _roadmap_runtime_error(*, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Map a recorded Roadmap runtime failure onto a hard CLI failure."""
    message = str(
        data.get("failure_summary") or data.get("error") or "Roadmap generation failed."
    )
    details = {
        "project_id": project_id,
        "roadmap_run_success": False,
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
            "Inspect agileforge roadmap history --project-id <project_id>.",
            "Fix the Roadmap runtime/provider configuration or refine the input.",
        ],
    )
