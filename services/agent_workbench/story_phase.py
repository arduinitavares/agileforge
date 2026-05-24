"""Agent workbench Story phase command runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import anyio
from sqlmodel import Session, select

from models.core import Sprint, SprintStory, UserStory
from models.db import get_engine
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key
from orchestrator_agent.agent_tools.user_story_writer_tool.tools import (
    save_stories_tool,
)
from repositories.product import ProductRepository
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.interview_runtime import (
    append_attempt,
    append_feedback_entry,
    mark_feedback_absorbed,
    promote_reusable_draft,
    reset_subject_working_set,
    set_request_projection,
)
from services.phases.story_service import (
    StoryPhaseError,
    complete_story_phase,
    generate_story_draft,
    get_story_history,
    get_story_pending,
    reopen_story_requirement,
    repair_story_readiness,
    retry_story_draft,
    save_story_draft,
)
from services.story_runtime import (
    run_story_agent_from_state,
    run_story_agent_request,
)
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project

if TYPE_CHECKING:
    from google.adk.tools import ToolContext

    from models.core import Product
else:
    ToolContext = Any


class StoryPhaseRunner:
    """Run Story phase commands through the same service boundary as the API."""

    def __init__(
        self,
        *,
        product_repo: ProductRepository | None = None,
        workflow_service: WorkflowService | None = None,
    ) -> None:
        """Initialize repositories for CLI Story commands."""
        self._product_repo = product_repo or ProductRepository()
        self._workflow_service = workflow_service or WorkflowService()

    def pending(self, *, project_id: int) -> dict[str, Any]:
        """Return roadmap requirements grouped by Story completion status."""
        return anyio.run(self._pending, project_id)

    def generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        return anyio.run(self._generate, project_id, parent_requirement, user_input)

    def retry(self, *, project_id: int, parent_requirement: str) -> dict[str, Any]:
        """Retry the latest retryable Story request."""
        return anyio.run(self._retry, project_id, parent_requirement)

    def history(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        """Return Story draft attempt history for a roadmap requirement."""
        return anyio.run(self._history, project_id, parent_requirement)

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Story draft."""
        return anyio.run(
            self._save,
            project_id,
            parent_requirement,
            attempt_id,
            expected_artifact_fingerprint,
            expected_state,
            idempotency_key,
        )

    def complete(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Complete the Story phase after all roadmap requirements are covered."""
        return anyio.run(self._complete, project_id, expected_state, idempotency_key)

    def reopen(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reopen one saved Story requirement before Sprint work exists."""
        return anyio.run(
            self._reopen,
            project_id,
            parent_requirement,
            expected_state,
            idempotency_key,
        )

    def repair_readiness(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Backfill Story planning metadata before Sprint work starts."""
        return anyio.run(
            self._repair_readiness,
            project_id,
            expected_state,
            idempotency_key,
        )

    async def _pending(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await get_story_pending(
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                )
            )
        except StoryPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _generate(
        self,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await generate_story_draft(
                project_id=project_id,
                parent_requirement=parent_requirement,
                user_input=user_input,
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                run_story_agent_from_state=run_story_agent_from_state,
                append_feedback_entry=append_feedback_entry,
                set_request_projection=set_request_projection,
                append_attempt=append_attempt,
                promote_reusable_draft=promote_reusable_draft,
                mark_feedback_absorbed=mark_feedback_absorbed,
                failure_meta=_failure_meta,
            )
        except StoryPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if _story_runtime_failed(data):
            return _story_runtime_error(
                project_id=project_id,
                parent_requirement=parent_requirement,
                data=data,
                state=self._workflow_service.get_session_status(str(project_id)) or {},
            )
        return _data_envelope(data)

    async def _retry(
        self,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await retry_story_draft(
                project_id=project_id,
                parent_requirement=parent_requirement,
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                run_story_agent_request=run_story_agent_request,
                append_attempt=append_attempt,
                promote_reusable_draft=promote_reusable_draft,
                mark_feedback_absorbed=mark_feedback_absorbed,
                failure_meta=_failure_meta,
            )
        except StoryPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if _story_runtime_failed(data):
            return _story_runtime_error(
                project_id=project_id,
                parent_requirement=parent_requirement,
                data=data,
                state=self._workflow_service.get_session_status(str(project_id)) or {},
            )
        return _data_envelope(data)

    async def _history(
        self,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await get_story_history(
                parent_requirement=parent_requirement,
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                ),
            )
        except StoryPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _save(  # noqa: PLR0913
        self,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await save_story_draft(
                project_id=project_id,
                parent_requirement=parent_requirement,
                attempt_id=attempt_id,
                expected_artifact_fingerprint=expected_artifact_fingerprint,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                hydrate_context=lambda session_id, hydrated_project_id: (
                    self._hydrate_context(session_id, hydrated_project_id, product)
                ),
                build_tool_context=_build_tool_context,
                save_stories_tool=save_stories_tool,
            )
        except StoryPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _complete(
        self,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await complete_story_phase(
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
            )
        except StoryPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _reopen(
        self,
        project_id: int,
        parent_requirement: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await reopen_story_requirement(
                parent_requirement=parent_requirement,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                now_iso=_now_iso,
                assert_reopen_safe=lambda normalized_requirement: _assert_reopen_safe(
                    project_id=project_id,
                    normalized_requirement=normalized_requirement,
                ),
                reset_subject_working_set=reset_subject_working_set,
            )
        except StoryPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _repair_readiness(
        self,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await repair_story_readiness(
                project_id=project_id,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                load_state=lambda: self._load_story_state(
                    str(project_id), project_id, product
                ),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                repair_rows=_repair_story_readiness_rows,
                assert_repair_safe=lambda repair_project_id: (
                    _assert_repair_readiness_safe(project_id=repair_project_id)
                ),
            )
        except StoryPhaseError as exc:
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

    async def _load_story_state(
        self,
        session_id: str,
        project_id: int,
        product: Product,
    ) -> dict[str, Any]:
        """Load workflow state with active project, spec, authority, and roadmap."""
        context = await self._hydrate_context(session_id, project_id, product)
        return dict(context.state)

    async def _hydrate_context(
        self,
        session_id: str,
        project_id: int,
        product: Product,
    ) -> SimpleNamespace:
        state = await self._ensure_session(session_id)
        context = SimpleNamespace(state=dict(state), session_id=session_id)
        result = select_project(project_id, _build_tool_context(context))
        if not result.get("success"):
            raise StoryPhaseError(str(result.get("error", "Project hydration failed")))
        _hydrate_roadmap_from_product(context.state, product)
        _assert_required_context(context.state)
        return context

    def _save_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        self._workflow_service.update_session_status(session_id, state)


def _now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _assert_reopen_safe(*, project_id: int, normalized_requirement: str) -> None:
    """Block Story reopen if active story rows already feed a Sprint."""
    normalized_key = normalize_requirement_key(normalized_requirement)
    with Session(get_engine()) as session:
        story_ids = [
            story_id
            for story_id in session.exec(
                select(UserStory.story_id).where(
                    UserStory.product_id == project_id,
                    UserStory.source_requirement == normalized_key,
                    UserStory.is_superseded.is_(False),
                )
            ).all()
            if story_id is not None
        ]
        if not story_ids:
            return

        sprint_link = session.exec(
            select(SprintStory.story_id).where(
                cast("Any", SprintStory.story_id).in_(story_ids)
            )
        ).first()
        if sprint_link is not None:
            message = (
                "Story correction is unsafe: active story already has Sprint links."
            )
            raise StoryPhaseError(
                message,
                status_code=409,
            )


def _repair_story_readiness_rows(request: dict[str, Any]) -> dict[str, Any]:
    """Backfill only Story planning metadata for active refined rows."""
    project_id = int(request["project_id"])
    items = list(request.get("items") or [])
    repaired_ids: list[int] = []
    with Session(get_engine()) as session:
        _assert_repair_readiness_safe_in_session(session, project_id=project_id)
        for item in items:
            if not isinstance(item, dict):
                continue
            story = session.exec(
                select(UserStory).where(
                    UserStory.product_id == project_id,
                    UserStory.source_requirement
                    == normalize_requirement_key(item["parent_requirement"]),
                    UserStory.refinement_slot == int(item["slot"]),
                    UserStory.is_refined == True,  # noqa: E712
                    UserStory.is_superseded == False,  # noqa: E712
                )
            ).first()
            if story is None:
                message = (
                    "Story readiness repair could not find active refined story "
                    f"for {item.get('parent_requirement')!r} slot {item.get('slot')!r}."
                )
                raise StoryPhaseError(message, status_code=409)
            story.story_points = int(item["story_points"])
            story.rank = str(item["rank"])
            session.add(story)
            if story.story_id is not None:
                repaired_ids.append(story.story_id)
        session.commit()
    return {"repaired_count": len(repaired_ids), "story_ids": repaired_ids}


def _assert_repair_readiness_safe(*, project_id: int) -> None:
    """Block Story readiness repair if refined rows already feed any Sprint."""
    with Session(get_engine()) as session:
        _assert_repair_readiness_safe_in_session(session, project_id=project_id)


def _assert_repair_readiness_safe_in_session(
    session: Session,
    *,
    project_id: int,
) -> None:
    """Block Story readiness repair if refined rows already feed any Sprint."""
    active_story_ids = [
        story_id
        for story_id in session.exec(
            select(UserStory.story_id).where(
                UserStory.product_id == project_id,
                UserStory.is_refined == True,  # noqa: E712
                UserStory.is_superseded == False,  # noqa: E712
            )
        ).all()
        if story_id is not None
    ]
    if not active_story_ids:
        return

    sprint_link = session.exec(
        select(SprintStory.story_id)
        .join(Sprint, cast("Any", Sprint.sprint_id) == SprintStory.sprint_id)
        .where(
            Sprint.product_id == project_id,
            cast("Any", SprintStory.story_id).in_(active_story_ids),
        )
    ).first()
    if sprint_link is not None:
        message = "Story readiness repair is unsafe after Sprint work exists."
        raise StoryPhaseError(
            message,
            status_code=409,
        )


def _build_tool_context(context: object) -> ToolContext:
    """Return a lightweight ToolContext-compatible state holder."""
    return cast("ToolContext", context)


def _hydrate_roadmap_from_product(state: dict[str, Any], product: Product) -> None:
    """Backfill saved roadmap releases when workflow state lacks them."""
    roadmap_releases = state.get("roadmap_releases")
    if isinstance(roadmap_releases, list) and roadmap_releases:
        return

    roadmap = getattr(product, "roadmap", None)
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
        state["roadmap_releases"] = [
            release for release in parsed if isinstance(release, dict)
        ]


def _assert_required_context(state: dict[str, Any]) -> None:
    """Block Story runtime if hydrated context is missing semantic inputs."""
    missing: list[str] = []
    if not state.get("pending_spec_content"):
        missing.append("pending_spec_content")
    if not state.get("compiled_authority_cached"):
        missing.append("compiled_authority_cached")
    roadmap_releases = state.get("roadmap_releases")
    if not isinstance(roadmap_releases, list) or not roadmap_releases:
        missing.append("roadmap_releases")
    if missing:
        raise StoryPhaseError(
            "Setup required: Story context hydration missing " + ", ".join(missing)
        )


def _failure_meta(
    story_result: dict[str, Any],
    *,
    fallback_summary: object = None,
) -> dict[str, Any]:
    """Copy normalized runtime failure metadata onto Story attempts."""
    return {
        "failure_artifact_id": story_result.get("failure_artifact_id"),
        "failure_stage": story_result.get("failure_stage"),
        "failure_summary": story_result.get("failure_summary") or fallback_summary,
        "raw_output_preview": story_result.get("raw_output_preview"),
        "has_full_artifact": bool(story_result.get("has_full_artifact", False)),
    }


def _story_runtime_failed(data: dict[str, Any]) -> bool:
    """Return whether a Story service response recorded runtime failure."""
    payload = data.get("data")
    output_artifact = (
        payload.get("output_artifact") if isinstance(payload, dict) else {}
    )
    if not isinstance(output_artifact, dict):
        return False
    return bool(
        output_artifact.get("failure_artifact_id")
        or output_artifact.get("error") == "STORY_GENERATION_FAILED"
    )


def _attempt_count(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> int | None:
    story_attempts = state.get("story_attempts")
    if not isinstance(story_attempts, dict):
        return None
    attempts = story_attempts.get(parent_requirement)
    if not isinstance(attempts, list):
        return None
    return len(attempts)


def _latest_attempt(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any]:
    story_attempts = state.get("story_attempts")
    if not isinstance(story_attempts, dict):
        return {}
    attempts = story_attempts.get(parent_requirement)
    if not isinstance(attempts, list) or not attempts:
        return {}
    latest = attempts[-1]
    return latest if isinstance(latest, dict) else {}


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


def _phase_error(exc: StoryPhaseError) -> dict[str, Any]:
    """Map Story phase errors onto registered CLI errors."""
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


def _story_runtime_error(
    *,
    project_id: int,
    parent_requirement: str,
    data: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Map a recorded Story runtime failure onto a hard CLI failure."""
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    output_artifact = (
        payload.get("output_artifact") if isinstance(payload, dict) else {}
    )
    if not isinstance(output_artifact, dict):
        output_artifact = {}
    parent = str(data.get("parent_requirement") or parent_requirement)
    latest_attempt = _latest_attempt(state, parent_requirement=parent)
    message = str(
        output_artifact.get("failure_summary")
        or latest_attempt.get("failure_summary")
        or output_artifact.get("message")
        or output_artifact.get("error")
        or "Story generation failed."
    )
    details = {
        "project_id": project_id,
        "parent_requirement": parent,
        "story_run_success": False,
        "failure_stage": output_artifact.get("failure_stage")
        or latest_attempt.get("failure_stage"),
        "failure_artifact_id": output_artifact.get("failure_artifact_id")
        or latest_attempt.get("failure_artifact_id"),
        "attempt_count": _attempt_count(state, parent_requirement=parent),
        "fsm_state": data.get("fsm_state") or state.get("fsm_state"),
    }
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        message,
        details={key: value for key, value in details.items() if value is not None},
        remediation=[
            "Inspect agileforge story history --project-id <project_id> --parent-requirement <requirement>.",  # noqa: E501
            "Fix the Story runtime/provider configuration or refine the input.",
        ],
    )
