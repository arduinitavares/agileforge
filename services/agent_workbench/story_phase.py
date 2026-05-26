"""Agent workbench Story phase command runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

import anyio
from sqlmodel import Session, select

from models.core import Sprint, SprintStory, UserStory, UserStoryDependency
from models.db import get_engine
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key
from orchestrator_agent.agent_tools.user_story_writer_tool.tools import (
    save_stories_tool,
)
from orchestrator_agent.fsm.states import OrchestratorState
from repositories.product import ProductRepository
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.interview_runtime import (
    append_attempt,
    append_feedback_entry,
    mark_feedback_absorbed,
    promote_reusable_draft,
    reset_subject_working_set,
    set_request_projection,
)
from services.phases.sprint_service import reset_sprint_planner_working_set
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
from services.story_dependencies import (
    dependency_inspect_payload,
    load_story_dependency_graph,
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

_DEPENDENCY_REVIEW_STATES = {"STORY_PERSISTENCE", "SPRINT_SETUP", "SPRINT_DRAFT"}
_DEPENDENCY_ATTEMPTS_KEY = "story_dependency_attempts"
_DEPENDENCY_PROPOSE_IDEMPOTENCY_KEY = "story_dependency_propose_idempotency_keys"
_DEPENDENCY_APPLY_IDEMPOTENCY_KEY = "story_dependency_apply_idempotency_keys"
_MAX_DEPENDENCY_ATTEMPTS = 20


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


class StoryPhaseRunner:
    """Run Story phase commands through the same service boundary as the API."""

    def __init__(
        self,
        *,
        product_repo: ProductRepository | _ProductRepositoryLike | None = None,
        workflow_service: WorkflowService | _WorkflowServiceLike | None = None,
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

    def dependency_inspect(self, *, project_id: int) -> dict[str, Any]:
        """Inspect active/proposed Story dependency edges."""
        return anyio.run(self._dependency_inspect, project_id)

    def dependency_propose(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a reviewed dependency proposal attempt."""
        return anyio.run(
            self._dependency_propose,
            project_id,
            expected_state,
            idempotency_key,
        )

    def dependency_apply(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Apply an exact reviewed dependency proposal attempt."""
        return anyio.run(
            self._dependency_apply,
            project_id,
            attempt_id,
            expected_artifact_fingerprint,
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

        initial_fsm_state: str | None = None

        async def load_state() -> dict[str, Any]:
            nonlocal initial_fsm_state
            state = await self._load_story_state(str(project_id), project_id, product)
            initial_fsm_state = (
                str(state["fsm_state"]) if state.get("fsm_state") is not None else None
            )
            return state

        try:
            data = await save_story_draft(
                project_id=project_id,
                parent_requirement=parent_requirement,
                attempt_id=attempt_id,
                expected_artifact_fingerprint=expected_artifact_fingerprint,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                load_state=load_state,
                save_state=lambda state: self._save_story_mutation_state(
                    str(project_id),
                    state,
                    reason="story_saved",
                    initial_fsm_state=initial_fsm_state,
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

        initial_fsm_state: str | None = None

        async def load_state() -> dict[str, Any]:
            nonlocal initial_fsm_state
            state = await self._load_story_state(str(project_id), project_id, product)
            initial_fsm_state = (
                str(state["fsm_state"]) if state.get("fsm_state") is not None else None
            )
            return state

        try:
            data = await reopen_story_requirement(
                parent_requirement=parent_requirement,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
                load_state=load_state,
                save_state=lambda state: self._save_story_mutation_state(
                    str(project_id),
                    state,
                    reason="story_reopened",
                    initial_fsm_state=initial_fsm_state,
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

    async def _dependency_inspect(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                return _data_envelope(
                    dependency_inspect_payload(session, project_id=project_id)
                )
        except RuntimeError as exc:
            return _workflow_error(exc)

    async def _dependency_propose(
        self,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        state = await self._ensure_session(str(project_id))
        replay = _dependency_replay(
            state,
            registry_key=_DEPENDENCY_PROPOSE_IDEMPOTENCY_KEY,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return _data_envelope(replay)
        guard_error = _dependency_guard_error(
            state=state,
            expected_state=expected_state,
        )
        if guard_error is not None:
            return guard_error

        try:
            with Session(get_engine()) as session:
                artifact = _build_dependency_proposal_artifact(
                    session,
                    project_id=project_id,
                )
                session.add(
                    WorkflowEvent(
                        event_type=WorkflowEventType.STORY_DEPENDENCIES_PROPOSED,
                        product_id=project_id,
                        session_id=str(project_id),
                        event_metadata=json.dumps(
                            _dependency_propose_event_metadata(
                                artifact=artifact,
                                idempotency_key=idempotency_key,
                                project_id=project_id,
                            )
                        ),
                    )
                )
                session.commit()
        except RuntimeError as exc:
            return _workflow_error(exc)

        _record_dependency_attempt(state, artifact)
        _record_dependency_replay(
            state,
            registry_key=_DEPENDENCY_PROPOSE_IDEMPOTENCY_KEY,
            idempotency_key=idempotency_key,
            payload=artifact,
        )
        self._save_session_state(str(project_id), state)
        return _data_envelope(artifact)

    async def _dependency_apply(  # noqa: PLR0911
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

        state = await self._ensure_session(str(project_id))
        replay = _dependency_replay(
            state,
            registry_key=_DEPENDENCY_APPLY_IDEMPOTENCY_KEY,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return _data_envelope(replay)
        guard_error = _dependency_guard_error(
            state=state,
            expected_state=expected_state,
        )
        if guard_error is not None:
            return guard_error

        attempt = _find_dependency_attempt(
            state,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
        )
        if attempt is None:
            return _error_envelope(
                ErrorCode.INVALID_COMMAND,
                "Dependency apply requires an exact known attempt and fingerprint.",
                details={
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                },
            )

        try:
            with Session(get_engine()) as session:
                before_graph = load_story_dependency_graph(
                    session,
                    project_id=project_id,
                )
                payload = _apply_dependency_attempt(
                    session,
                    project_id=project_id,
                    attempt=attempt,
                    attempt_id=attempt_id,
                    artifact_fingerprint=expected_artifact_fingerprint,
                    idempotency_key=idempotency_key,
                    before_cycle_count=len(before_graph.cycle_paths),
                )
                if payload.get("success") is False:
                    session.rollback()
                    return _error_envelope(
                        ErrorCode.MUTATION_FAILED,
                        str(payload["error"]),
                        details=payload,
                    )
                session.add(
                    WorkflowEvent(
                        event_type=WorkflowEventType.STORY_DEPENDENCIES_APPLIED,
                        product_id=project_id,
                        session_id=str(project_id),
                        event_metadata=json.dumps(
                            _dependency_apply_event_metadata(
                                payload=payload,
                                idempotency_key=idempotency_key,
                                project_id=project_id,
                            )
                        ),
                    )
                )
                session.commit()
        except RuntimeError as exc:
            return _workflow_error(exc)

        _record_dependency_replay(
            state,
            registry_key=_DEPENDENCY_APPLY_IDEMPOTENCY_KEY,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        _invalidate_unsaved_sprint_working_set(
            state,
            reason="story_dependencies_applied",
            now_iso=_now_iso(),
        )
        self._save_session_state(str(project_id), state)
        return _data_envelope(payload)

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

    def _save_story_mutation_state(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        reason: str,
        initial_fsm_state: str | None,
    ) -> None:
        current_fsm_state = state.get("fsm_state")
        current_fsm_state_entered_at = state.get("fsm_state_entered_at")
        if current_fsm_state not in {
            OrchestratorState.SPRINT_SETUP.value,
            OrchestratorState.SPRINT_DRAFT.value,
        }:
            state["fsm_state"] = initial_fsm_state
        _invalidate_unsaved_sprint_working_set(
            state,
            reason=reason,
            now_iso=_now_iso(),
        )
        if current_fsm_state not in {
            OrchestratorState.SPRINT_SETUP.value,
            OrchestratorState.SPRINT_DRAFT.value,
        }:
            state["fsm_state"] = current_fsm_state
            if current_fsm_state_entered_at is None:
                state.pop("fsm_state_entered_at", None)
            else:
                state["fsm_state_entered_at"] = current_fsm_state_entered_at
        self._save_session_state(session_id, state)


def _invalidate_unsaved_sprint_working_set(
    state: dict[str, Any],
    *,
    reason: str,
    now_iso: str,
) -> None:
    """Clear unsaved Sprint planner state after upstream Story data changes."""
    if state.get("sprint_planner_owner_sprint_id") is not None:
        return
    if state.get("fsm_state") not in {
        OrchestratorState.SPRINT_SETUP.value,
        OrchestratorState.SPRINT_DRAFT.value,
    }:
        return

    reset_sprint_planner_working_set(state)
    state["fsm_state"] = OrchestratorState.SPRINT_SETUP.value
    state["fsm_state_entered_at"] = now_iso
    state["sprint_invalidated_reason"] = reason
    state["sprint_invalidated_at"] = now_iso


def _dependency_guard_error(
    *,
    state: dict[str, Any],
    expected_state: str,
) -> dict[str, Any] | None:
    current_state = state.get("fsm_state")
    if expected_state not in _DEPENDENCY_REVIEW_STATES:
        return _error_envelope(
            ErrorCode.INVALID_COMMAND,
            "Story dependency review is only allowed in Story/Sprint review states.",
            details={
                "expected_state": expected_state,
                "allowed_states": sorted(_DEPENDENCY_REVIEW_STATES),
            },
        )
    if current_state != expected_state:
        return _error_envelope(
            ErrorCode.INVALID_COMMAND,
            "Expected workflow state does not match current state.",
            details={"expected_state": expected_state, "current_state": current_state},
        )
    return None


def _dependency_replay(
    state: dict[str, Any],
    *,
    registry_key: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    registry = state.get(registry_key)
    if not isinstance(registry, dict):
        return None
    payload = registry.get(idempotency_key)
    return dict(payload) if isinstance(payload, dict) else None


def _record_dependency_replay(
    state: dict[str, Any],
    *,
    registry_key: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> None:
    registry = state.get(registry_key)
    if not isinstance(registry, dict):
        registry = {}
    registry[idempotency_key] = dict(payload)
    state[registry_key] = registry


def _record_dependency_attempt(
    state: dict[str, Any],
    artifact: dict[str, Any],
) -> None:
    attempts = state.get(_DEPENDENCY_ATTEMPTS_KEY)
    if not isinstance(attempts, list):
        attempts = []
    attempts.append(dict(artifact))
    state[_DEPENDENCY_ATTEMPTS_KEY] = attempts[-_MAX_DEPENDENCY_ATTEMPTS:]


def _find_dependency_attempt(
    state: dict[str, Any],
    *,
    attempt_id: str,
    expected_artifact_fingerprint: str,
) -> dict[str, Any] | None:
    attempts = state.get(_DEPENDENCY_ATTEMPTS_KEY)
    if not isinstance(attempts, list):
        return None
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        if (
            attempt.get("attempt_id") == attempt_id
            and attempt.get("artifact_fingerprint") == expected_artifact_fingerprint
        ):
            return dict(attempt)
    return None


def _build_dependency_proposal_artifact(
    session: Session,
    *,
    project_id: int,
) -> dict[str, Any]:
    attempt_id = f"story-dependencies-{uuid4()}"
    inspect_payload = dependency_inspect_payload(session, project_id=project_id)
    active_edges = [
        {**edge, "selected": True} for edge in inspect_payload["active_edges"]
    ]
    proposed_edges = [
        {**edge, "selected": True} for edge in inspect_payload["proposed_edges"]
    ]
    edge_keys = {
        (edge["dependent_story_id"], edge["prerequisite_story_id"])
        for edge in [*active_edges, *proposed_edges]
    }
    deterministic_edges = _deterministic_dependency_edges(
        session,
        project_id=project_id,
        existing_edge_keys=edge_keys,
    )
    edges = [*active_edges, *proposed_edges, *deterministic_edges]
    artifact: dict[str, Any] = {
        "attempt_id": attempt_id,
        "is_complete": True,
        "active_edge_count": len(active_edges),
        "proposed_edge_count": len(proposed_edges) + len(deterministic_edges),
        "cycle_count": inspect_payload["cycle_count"],
        "cycle_paths": inspect_payload["cycle_paths"],
        "edges": edges,
    }
    artifact["artifact_fingerprint"] = canonical_hash(
        {"phase": "story_dependencies", "artifact": artifact}
    )
    return artifact


def _deterministic_dependency_edges(
    session: Session,
    *,
    project_id: int,
    existing_edge_keys: set[tuple[int, int]],
) -> list[dict[str, Any]]:
    stories = session.exec(
        select(UserStory)
        .where(UserStory.product_id == project_id)
        .where(UserStory.is_refined == True)  # noqa: E712
        .where(UserStory.is_superseded == False)  # noqa: E712
        .order_by(
            cast("Any", UserStory.source_requirement),
            cast("Any", UserStory.refinement_slot),
        )
    ).all()
    by_requirement: dict[str, list[UserStory]] = {}
    for story in stories:
        if (
            story.story_id is None
            or not story.source_requirement
            or story.refinement_slot is None
        ):
            continue
        by_requirement.setdefault(story.source_requirement, []).append(story)

    edges: list[dict[str, Any]] = []
    for requirement_stories in by_requirement.values():
        ordered = sorted(
            requirement_stories,
            key=lambda story: (story.refinement_slot or 0, story.story_id or 0),
        )
        for prerequisite, dependent in pairwise(ordered):
            if prerequisite.story_id is None or dependent.story_id is None:
                continue
            key = (dependent.story_id, prerequisite.story_id)
            if key in existing_edge_keys:
                continue
            existing_edge_keys.add(key)
            edges.append(
                {
                    "dependency_id": None,
                    "dependent_story_id": dependent.story_id,
                    "dependent_story_title": dependent.title,
                    "prerequisite_story_id": prerequisite.story_id,
                    "prerequisite_story_title": prerequisite.title,
                    "status": "proposed",
                    "source": "dependency_repair",
                    "confidence": "inferred",
                    "reason": (
                        "Deterministic refinement order: later slot depends on "
                        "the immediately previous slot in the same requirement."
                    ),
                    "selected": True,
                }
            )
    return edges


def _apply_dependency_attempt(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    attempt: dict[str, Any],
    attempt_id: str,
    artifact_fingerprint: str,
    idempotency_key: str,
    before_cycle_count: int,
) -> dict[str, Any]:
    edges = attempt.get("edges")
    if not isinstance(edges, list):
        return {
            "success": False,
            "error": "Dependency attempt has malformed edges.",
            "attempt_id": attempt_id,
        }

    artifact_edge_keys: set[tuple[int, int]] = set()
    activated_edges: list[dict[str, Any]] = []
    rejected_edges: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        pair = _edge_pair(edge)
        if pair is None:
            continue
        artifact_edge_keys.add(pair)
        if edge.get("status") == "rejected" or edge.get("selected") is False:
            rejected_edges.append(
                _set_dependency_edge_status(
                    session,
                    project_id=project_id,
                    pair=pair,
                    status="rejected",
                    reason=str(edge.get("reason") or "Rejected by dependency review."),
                )
            )
            continue
        if edge.get("selected") is True:
            activated_edges.append(
                _set_dependency_edge_status(
                    session,
                    project_id=project_id,
                    pair=pair,
                    status="active",
                    reason=str(edge.get("reason") or "Accepted by dependency review."),
                )
            )

    for proposed_edge in session.exec(
        select(UserStoryDependency)
        .where(UserStoryDependency.product_id == project_id)
        .where(UserStoryDependency.status == "proposed")
    ).all():
        pair = (proposed_edge.dependent_story_id, proposed_edge.prerequisite_story_id)
        if pair in artifact_edge_keys:
            continue
        proposed_edge.status = "rejected"
        proposed_edge.source = "manual_review"
        proposed_edge.confidence = "reviewed"
        proposed_edge.updated_at = datetime.now(UTC)
        session.add(proposed_edge)
        rejected_edges.append(_dependency_edge_summary(proposed_edge))

    session.flush()
    after_graph = load_story_dependency_graph(session, project_id=project_id)
    after_cycle_count = len(after_graph.cycle_paths)
    if after_cycle_count and after_cycle_count >= before_cycle_count:
        return {
            "success": False,
            "error": "Dependency apply would leave an active cycle unresolved.",
            "attempt_id": attempt_id,
            "cycle_count_before_apply": before_cycle_count,
            "cycle_count_after_apply": after_cycle_count,
            "cycle_paths": after_graph.cycle_paths,
        }

    return {
        "success": True,
        "project_id": project_id,
        "attempt_id": attempt_id,
        "artifact_fingerprint": artifact_fingerprint,
        "idempotency_key": idempotency_key,
        "activated_edge_count": len(activated_edges),
        "activated_edges": activated_edges,
        "rejected_edge_count": len(rejected_edges),
        "rejected_edges": rejected_edges,
        "active_edge_count": sum(
            len(items) for items in after_graph.active_edges.values()
        ),
        "cycle_count_after_apply": after_cycle_count,
    }


def _edge_pair(edge: dict[str, Any]) -> tuple[int, int] | None:
    try:
        dependent_story_id = int(edge["dependent_story_id"])
        prerequisite_story_id = int(edge["prerequisite_story_id"])
    except (KeyError, TypeError, ValueError):
        return None
    return dependent_story_id, prerequisite_story_id


def _set_dependency_edge_status(
    session: Session,
    *,
    project_id: int,
    pair: tuple[int, int],
    status: str,
    reason: str,
) -> dict[str, Any]:
    dependent_story_id, prerequisite_story_id = pair
    edge = session.exec(
        select(UserStoryDependency)
        .where(UserStoryDependency.product_id == project_id)
        .where(UserStoryDependency.dependent_story_id == dependent_story_id)
        .where(UserStoryDependency.prerequisite_story_id == prerequisite_story_id)
    ).first()
    if edge is None:
        edge = UserStoryDependency(
            product_id=project_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=prerequisite_story_id,
        )
    edge.status = status
    edge.source = "manual_review"
    edge.confidence = "reviewed"
    edge.reason = reason
    edge.updated_at = datetime.now(UTC)
    session.add(edge)
    session.flush()
    return _dependency_edge_summary(edge)


def _dependency_edge_summary(edge: UserStoryDependency) -> dict[str, Any]:
    return {
        "dependent_story_id": edge.dependent_story_id,
        "prerequisite_story_id": edge.prerequisite_story_id,
        "reason": edge.reason,
        "source": edge.source,
        "confidence": edge.confidence,
    }


def _dependency_propose_event_metadata(
    *,
    artifact: dict[str, Any],
    idempotency_key: str,
    project_id: int,
) -> dict[str, Any]:
    edge_ids = [
        edge["dependency_id"]
        for edge in artifact.get("edges", [])
        if isinstance(edge, dict) and edge.get("dependency_id") is not None
    ]
    return {
        "action": "story_dependencies_proposed",
        "idempotency_key": idempotency_key,
        "attempt_id": artifact["attempt_id"],
        "artifact_fingerprint": artifact["artifact_fingerprint"],
        "project_id": project_id,
        "proposed_edge_count": artifact["proposed_edge_count"],
        "active_edge_count": artifact["active_edge_count"],
        "cycle_count": artifact["cycle_count"],
        "edge_ids": edge_ids,
    }


def _dependency_apply_event_metadata(
    *,
    payload: dict[str, Any],
    idempotency_key: str,
    project_id: int,
) -> dict[str, Any]:
    return {
        "action": "story_dependencies_applied",
        "idempotency_key": idempotency_key,
        "attempt_id": payload["attempt_id"],
        "artifact_fingerprint": payload["artifact_fingerprint"],
        "project_id": project_id,
        "activated_edges": payload["activated_edges"],
        "rejected_edges": payload["rejected_edges"],
        "active_edge_count": payload["active_edge_count"],
        "rejected_edge_count": payload["rejected_edge_count"],
        "cycle_count_after_apply": payload["cycle_count_after_apply"],
    }


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
                    cast("Any", UserStory.is_superseded).is_(False),
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
