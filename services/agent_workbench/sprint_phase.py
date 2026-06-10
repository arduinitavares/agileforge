"""Agent workbench Sprint phase command runner."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast
from uuid import uuid4

import anyio
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from models.core import Sprint, SprintStory, Task, UserStory
from models.db import get_engine
from models.enums import (
    SprintStatus,
    StoryResolution,
    StoryStatus,
    TaskAcceptanceResult,
    TaskStatus,
    WorkflowEventType,
)
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from orchestrator_agent.agent_tools.sprint_planner_tool.tools import (
    save_sprint_plan_tool,
)
from repositories.product import ProductRepository
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    LedgerLoadResult,
    MutationLedgerRepository,
)
from services.agent_workbench.post_sprint_triage import (
    TRIAGE_FIELD_INVALID,
    PostSprintTriageValidationError,
    build_triage_payload,
    current_triage_for_latest_sprint,
    post_sprint_triage_required,
)
from services.phases import workflow_state
from services.phases.sprint_service import (
    SprintPhaseError,
    generate_sprint_plan,
    get_sprint_history,
    save_sprint_plan,
    start_sprint_execution,
)
from services.phases.sprint_service import (
    close_sprint as close_sprint_service,
)
from services.phases.sprint_service import (
    get_sprint_close_readiness as get_sprint_close_readiness_service,
)
from services.sprint_runtime import run_sprint_agent_from_state
from services.story_close_service import (
    StoryCloseServiceError,
)
from services.story_close_service import (
    close_story as close_story_service,
)
from services.story_close_service import (
    get_story_close_readiness as get_story_close_readiness_service,
)
from services.story_dependencies import load_story_dependency_graph
from services.task_execution_service import (
    TaskExecutionServiceError,
    get_task_execution_history,
    record_task_execution,
)
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project
from utils.api_schemas import (
    SprintCloseReadiness,
    SprintCloseStorySummary,
    SprintCloseWriteRequest,
    StoryCloseWriteRequest,
    TaskExecutionWriteRequest,
)
from utils.task_metadata import parse_task_metadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    from google.adk.tools import ToolContext
    from sqlmodel.sql._expression_select_cls import SelectOfScalar

    from models.core import Product
else:
    ToolContext = Any

_DEPENDENCY_ORDER_FALLBACK_INDEX = 1_000_000
_DEPENDENCY_RISK_MIN_MATCHED_TERMS = 2
_DEPENDENCY_RISK_MIN_TERM_LENGTH = 5
_SPRINT_TASK_UPDATE_COMMAND = "agileforge sprint task update"
_SPRINT_TASK_UPDATE_LEASE_OWNER = "agileforge-cli:sprint-task-update"
_SPRINT_STORY_CLOSE_COMMAND = "agileforge sprint story close"
_SPRINT_STORY_CLOSE_LEASE_OWNER = "agileforge-cli:sprint-story-close"
_SPRINT_CLOSE_COMMAND = "agileforge sprint close"
_SPRINT_CLOSE_LEASE_OWNER = "agileforge-cli:sprint-close"
_SPRINT_REVIEW_COMMAND = "agileforge sprint review"
_SPRINT_TRIAGE_COMMAND = "agileforge sprint triage"
_SPRINT_TRIAGE_LEASE_OWNER = "agileforge-cli:sprint-triage"
_DEPENDENCY_RISK_INTEGRATION_MARKERS = (
    "execute",
    "artifact set",
    "end-to-end",
    "orchestrat",
    "workflow",
)
_DEPENDENCY_RISK_CUE_MARKERS = (
    "before",
    "exist",
    "guard",
    "reject",
    "require",
    "using",
    "valid",
    "without",
)
_DEPENDENCY_RISK_STOPWORDS = frozenset(
    {
        "cartola",
        "command",
        "containing",
        "data",
        "recommendation",
        "recommendations",
        "story",
        "validate",
        "validation",
    }
)


@dataclass(frozen=True)
class _StoryDependencyMetadataContext:
    """Inputs needed to build per-story execution dependency metadata."""

    active_edges: dict[int, set[int]]
    downstream_edges: dict[int, set[int]]
    story_statuses: dict[int, StoryStatus]
    execution_index_by_story_id: dict[int, int]
    dependency_order_source: str


class _SprintTaskUpdateError(SprintPhaseError):
    """Sprint task update guard failure with structured details."""

    def __init__(
        self,
        detail: str,
        *,
        details: dict[str, Any],
        status_code: int = 409,
    ) -> None:
        super().__init__(detail, status_code=status_code)
        self.details = details


class _SprintStoryCloseError(SprintPhaseError):
    """Sprint story close guard failure with structured details."""

    def __init__(
        self,
        detail: str,
        *,
        details: dict[str, Any],
        status_code: int = 409,
    ) -> None:
        super().__init__(detail, status_code=status_code)
        self.details = details


class _SprintCloseError(SprintPhaseError):
    """Sprint close guard failure with structured details."""

    def __init__(
        self,
        detail: str,
        *,
        details: dict[str, Any],
        status_code: int = 409,
    ) -> None:
        super().__init__(detail, status_code=status_code)
        self.details = details


class _SprintTriageError(SprintPhaseError):
    """Sprint triage guard failure with a registered error code."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        details: dict[str, Any],
        remediation: list[str],
        status_code: int = 409,
    ) -> None:
        super().__init__(detail, status_code=status_code)
        self.code = code
        self.details = details
        self.remediation = remediation


class _ResolveSprintId(Protocol):
    """Callable shape for resolving the active/planned execution Sprint."""

    def __call__(
        self,
        project_id: int,
        *,
        sprint_id: int | None,
        session: Session,
    ) -> int:
        """Resolve a Sprint id for execution reads."""
        ...


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

    def start(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start a saved Sprint and move the workflow into execution."""
        return anyio.run(
            self._start,
            project_id,
            sprint_id,
            expected_state,
            idempotency_key,
        )

    def status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return active/planned Sprint execution status."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                sprint = _get_saved_sprint(session, project_id, resolved_sprint_id)
                if sprint is None:
                    _raise_sprint_not_found()
                data = {
                    "project_id": project_id,
                    "sprint_id": resolved_sprint_id,
                    "sprint": _serialize_sprint_detail(
                        sprint,
                        runtime_summary=_build_sprint_runtime_summary(
                            session=session,
                            project_id=project_id,
                        ),
                    ),
                    "task_summary": _task_summary(sprint),
                }
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data)

    def tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return task rows for the active/planned Sprint."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                sprint = _get_saved_sprint(session, project_id, resolved_sprint_id)
                if sprint is None:
                    _raise_sprint_not_found()
                task_view = _build_sprint_task_view(
                    session=session,
                    project_id=project_id,
                    sprint=sprint,
                )
                data = {
                    "project_id": project_id,
                    "sprint_id": resolved_sprint_id,
                    "task_count": len(task_view["tasks"]),
                    "tasks": task_view["tasks"],
                    "dependency_summary": task_view["dependency_summary"],
                }
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data, warnings=task_view["warnings"])

    def task_next(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return the next agent-executable Sprint task ticket."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                sprint, task_view = _execution_sprint_and_task_view(
                    session=session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    resolve_sprint_id=self._resolve_execution_sprint_id,
                )
                ticket, reason = _next_task_ticket(
                    session=session,
                    project_id=project_id,
                    sprint=sprint,
                    task_view=task_view,
                )
                data = {
                    "project_id": project_id,
                    "sprint_id": sprint.sprint_id,
                    "task_ticket": ticket,
                    "reason": reason,
                    "dependency_summary": task_view["dependency_summary"],
                }
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data, warnings=task_view["warnings"])

    def task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one Sprint task ticket."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                sprint, task_view = _execution_sprint_and_task_view(
                    session=session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    resolve_sprint_id=self._resolve_execution_sprint_id,
                )
                row = _task_row_from_view(task_view, task_id=task_id)
                if row is None:
                    _raise_task_not_found()
                ticket = _build_task_ticket(
                    session=session,
                    project_id=project_id,
                    sprint=sprint,
                    task_row=row,
                    dependency_summary=task_view["dependency_summary"],
                )
                data = {
                    "project_id": project_id,
                    "sprint_id": sprint.sprint_id,
                    "task_ticket": ticket,
                }
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data, warnings=task_view["warnings"])

    def task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one Sprint task execution history."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                history = _task_execution_history(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                    task_id=task_id,
                )
                data = {
                    "project_id": project_id,
                    "sprint_id": resolved_sprint_id,
                    "execution": history,
                }
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data)

    def task_update(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        task_id: int,
        status: str,
        expected_status: str,
        expected_task_fingerprint: str,
        idempotency_key: str,
        sprint_id: int | None = None,
        outcome_summary: str | None = None,
        artifact_refs: list[str] | None = None,
        checklist_result: str | None = None,
        validation_summary: str | None = None,
        notes: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Log Sprint task execution progress through a guarded mutation."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        engine = get_engine()
        ledger = MutationLedgerRepository(engine=engine)
        new_status = _normalize_update_status(status)
        expected = _normalize_update_status(expected_status)
        acceptance_result = _normalize_acceptance_result(checklist_result)
        request_payload = {
            "project_id": project_id,
            "sprint_id": sprint_id,
            "task_id": task_id,
            "status": new_status.value,
            "expected_status": expected.value,
            "expected_task_fingerprint": expected_task_fingerprint,
            "outcome_summary": outcome_summary,
            "artifact_refs": artifact_refs or [],
            "checklist_result": acceptance_result.value if acceptance_result else None,
            "validation_summary": validation_summary,
            "notes": notes,
            "changed_by": changed_by,
        }
        request_hash = canonical_hash(request_payload)
        now = datetime.now(UTC)
        ledger_result = ledger.create_or_load(
            command=_SPRINT_TASK_UPDATE_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=f"sprint-task-update:{uuid4()}",
            changed_by=changed_by,
            lease_owner=_SPRINT_TASK_UPDATE_LEASE_OWNER,
            now=now,
        )
        if ledger_result.error_code is not None:
            return _ledger_error(ledger_result)
        if ledger_result.replayed:
            replay = ledger_result.response or {}
            if isinstance(replay.get("data"), dict):
                replay["data"].setdefault("idempotency", {})["replayed"] = True
            return replay

        try:
            write_request = _task_execution_write_request(
                new_status=new_status,
                outcome_summary=outcome_summary,
                artifact_refs=artifact_refs,
                acceptance_result=acceptance_result,
                validation_summary=validation_summary,
                notes=notes,
                changed_by=changed_by,
            )
            with Session(engine) as session:
                sprint, task_view = _execution_sprint_and_task_view(
                    session=session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    resolve_sprint_id=self._resolve_execution_sprint_id,
                )
                row = _task_row_from_view(task_view, task_id=task_id)
                if row is None:
                    _raise_task_not_found()
                current_fingerprint = _task_row_fingerprint(
                    sprint=sprint,
                    task_row=row,
                )
                _assert_task_update_guards(
                    task_row=row,
                    expected_status=expected,
                    expected_task_fingerprint=expected_task_fingerprint,
                    current_task_fingerprint=current_fingerprint,
                    new_status=new_status,
                    artifact_refs=artifact_refs,
                )
                execution = _record_task_execution(
                    session=session,
                    project_id=project_id,
                    sprint_id=cast("int", sprint.sprint_id),
                    task_id=task_id,
                    write_request=write_request,
                )
                refreshed_sprint, refreshed_view = _execution_sprint_and_task_view(
                    session=session,
                    project_id=project_id,
                    sprint_id=cast("int", sprint.sprint_id),
                    resolve_sprint_id=self._resolve_execution_sprint_id,
                )
                refreshed_row = _task_row_from_view(refreshed_view, task_id=task_id)
                if refreshed_row is None:
                    _raise_task_not_found()
                ticket = _build_task_ticket(
                    session=session,
                    project_id=project_id,
                    sprint=refreshed_sprint,
                    task_row=refreshed_row,
                    dependency_summary=refreshed_view["dependency_summary"],
                )
                next_ticket, next_reason = _next_task_ticket(
                    session=session,
                    project_id=project_id,
                    sprint=refreshed_sprint,
                    task_view=refreshed_view,
                )
                data = {
                    "project_id": project_id,
                    "sprint_id": refreshed_sprint.sprint_id,
                    "task_ticket": ticket,
                    "execution": execution,
                    "next_recommended_task": next_ticket,
                    "next_reason": next_reason,
                    "idempotency": {"replayed": False},
                }
                response = _data_envelope(data, warnings=refreshed_view["warnings"])
        except (SprintPhaseError, ValueError) as exc:
            response = _task_update_error(exc)

        raw_response_data = response.get("data")
        response_data = (
            cast("dict[str, Any]", raw_response_data)
            if isinstance(raw_response_data, dict)
            else {}
        )
        ledger.finalize_success(
            mutation_event_id=cast("int", ledger_result.ledger.mutation_event_id),
            lease_owner=_SPRINT_TASK_UPDATE_LEASE_OWNER,
            after=response_data,
            response=response,
            now=datetime.now(UTC),
        )
        return response

    def story_readiness(
        self,
        *,
        project_id: int,
        story_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return close readiness for one Sprint story."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                data = _story_close_readiness_payload(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                    story_id=story_id,
                )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data)

    def story_close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        story_id: int,
        expected_status: str,
        expected_story_fingerprint: str,
        idempotency_key: str,
        resolution: str,
        completion_notes: str,
        evidence_links: list[str] | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Close one Sprint story through a guarded mutation."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        engine = get_engine()
        ledger = MutationLedgerRepository(engine=engine)
        expected = _normalize_story_status(expected_status)
        write_request = StoryCloseWriteRequest(
            resolution=_normalize_story_resolution(resolution),
            completion_notes=completion_notes,
            evidence_links=evidence_links,
            changed_by=changed_by,
        )
        request_payload = {
            "project_id": project_id,
            "sprint_id": sprint_id,
            "story_id": story_id,
            "expected_status": expected.value,
            "expected_story_fingerprint": expected_story_fingerprint,
            "resolution": write_request.resolution.value,
            "completion_notes": write_request.completion_notes,
            "evidence_links": write_request.evidence_links or [],
            "changed_by": changed_by,
        }
        request_hash = canonical_hash(request_payload)
        now = datetime.now(UTC)
        ledger_result = ledger.create_or_load(
            command=_SPRINT_STORY_CLOSE_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=f"sprint-story-close:{uuid4()}",
            changed_by=changed_by,
            lease_owner=_SPRINT_STORY_CLOSE_LEASE_OWNER,
            now=now,
        )
        if ledger_result.error_code is not None:
            return _ledger_error(ledger_result)
        if ledger_result.replayed:
            replay = ledger_result.response or {}
            if isinstance(replay.get("data"), dict):
                replay["data"].setdefault("idempotency", {})["replayed"] = True
            return replay

        try:
            with Session(engine) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                current_payload = _story_close_readiness_payload(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                    story_id=story_id,
                )
                _assert_story_close_guards(
                    current_status=str(current_payload["current_status"]),
                    expected_status=expected,
                    current_story_fingerprint=str(current_payload["story_fingerprint"]),
                    expected_story_fingerprint=expected_story_fingerprint,
                    story_id=story_id,
                )
                closed = _close_story(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                    story_id=story_id,
                    request=write_request,
                )
                closed["story_fingerprint"] = _story_fingerprint_for_ids(
                    session=session,
                    sprint_id=resolved_sprint_id,
                    story_id=story_id,
                )
                closed["idempotency"] = {"replayed": False}
                response = _data_envelope(closed)
        except (SprintPhaseError, ValueError) as exc:
            response = _story_close_error(exc)

        raw_response_data = response.get("data")
        response_data = (
            cast("dict[str, Any]", raw_response_data)
            if isinstance(raw_response_data, dict)
            else {}
        )
        ledger.finalize_success(
            mutation_event_id=cast("int", ledger_result.ledger.mutation_event_id),
            lease_owner=_SPRINT_STORY_CLOSE_LEASE_OWNER,
            after=response_data,
            response=response,
            now=datetime.now(UTC),
        )
        return response

    def close_readiness(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return close readiness for the active Sprint."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                data = _sprint_close_readiness_payload(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data)

    def close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        expected_status: str,
        expected_sprint_fingerprint: str,
        idempotency_key: str,
        completion_notes: str,
        follow_up_notes: str | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Close an active Sprint through a guarded mutation."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        engine = get_engine()
        ledger = MutationLedgerRepository(engine=engine)
        write_request = SprintCloseWriteRequest(
            completion_notes=completion_notes,
            follow_up_notes=follow_up_notes,
            changed_by=changed_by,
        )
        request_payload = {
            "project_id": project_id,
            "sprint_id": sprint_id,
            "expected_state": expected_state,
            "expected_status": expected_status,
            "expected_sprint_fingerprint": expected_sprint_fingerprint,
            "completion_notes": write_request.completion_notes,
            "follow_up_notes": write_request.follow_up_notes,
            "changed_by": changed_by,
        }
        request_hash = canonical_hash(request_payload)
        now = datetime.now(UTC)
        ledger_result = ledger.create_or_load(
            command=_SPRINT_CLOSE_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=f"sprint-close:{uuid4()}",
            changed_by=changed_by,
            lease_owner=_SPRINT_CLOSE_LEASE_OWNER,
            now=now,
        )
        if ledger_result.error_code is not None:
            return _ledger_error(ledger_result)
        if ledger_result.replayed:
            replay = ledger_result.response or {}
            if isinstance(replay.get("data"), dict):
                replay["data"].setdefault("idempotency", {})["replayed"] = True
                if replay["data"].get("fsm_state") == "SPRINT_COMPLETE":
                    resolved_replay_sprint_id = _int_or_none(
                        replay["data"].get("sprint_id")
                    )
                    if resolved_replay_sprint_id is not None:
                        self._sync_completed_sprint_state(
                            project_id=project_id,
                            sprint_id=resolved_replay_sprint_id,
                        )
            return replay

        try:
            state = anyio.run(self._ensure_session, str(project_id))
            with Session(engine) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                current_payload = _sprint_close_readiness_payload(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                )
                _assert_sprint_close_guards(
                    current_state=str(state.get("fsm_state") or ""),
                    expected_state=expected_state,
                    current_status=str(current_payload["current_status"]),
                    expected_status=expected_status,
                    current_sprint_fingerprint=str(
                        current_payload["sprint_fingerprint"]
                    ),
                    expected_sprint_fingerprint=expected_sprint_fingerprint,
                    sprint_id=resolved_sprint_id,
                )
                closed = _close_sprint(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                    request=write_request,
                )
                state["fsm_state"] = "SPRINT_COMPLETE"
                state["fsm_state_entered_at"] = _now_iso()
                state["active_sprint_id"] = None
                state["latest_completed_sprint_id"] = resolved_sprint_id
                state["sprint_completed_at"] = state["fsm_state_entered_at"]
                self._save_session_state(str(project_id), state)
                closed["fsm_state"] = "SPRINT_COMPLETE"
                closed["active_sprint_id"] = None
                closed["sprint_fingerprint"] = _sprint_fingerprint_for_ids(
                    session=session,
                    sprint_id=resolved_sprint_id,
                )
                closed["idempotency"] = {"replayed": False}
                response = _data_envelope(closed)
        except (SprintPhaseError, ValueError) as exc:
            response = _sprint_close_error(exc)

        raw_response_data = response.get("data")
        response_data = (
            cast("dict[str, Any]", raw_response_data)
            if isinstance(raw_response_data, dict)
            else {}
        )
        ledger.finalize_success(
            mutation_event_id=cast("int", ledger_result.ledger.mutation_event_id),
            lease_owner=_SPRINT_CLOSE_LEASE_OWNER,
            after=response_data,
            response=response,
            now=datetime.now(UTC),
        )
        return response

    def review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return read-only post-sprint review context."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        state = self._workflow_service.get_session_status(str(project_id)) or {}
        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = _resolve_review_sprint_id(
                    session=session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    state=state,
                )
                sprint = _completed_sprint(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id,
                )
                data = _post_sprint_review_payload(
                    state=state,
                    project_id=project_id,
                    sprint=sprint,
                    sprint_id=resolved_sprint_id,
                )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data)

    def triage(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        *,
        project_id: int,
        expected_state: str,
        impact: str,
        learning_summary: str,
        decision_reason: str,
        idempotency_key: str,
        affected_requirements: list[str] | None = None,
        affected_task_ids: list[int] | None = None,
        affected_story_ids: list[int] | None = None,
        affected_backlog_item_ids: list[str] | None = None,
        affected_roadmap_item_ids: list[str] | None = None,
        affected_layers: list[str] | None = None,
        sprint_id: int | None = None,
        replace_existing: bool = False,
        expected_triage_fingerprint: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record or correct post-sprint triage metadata."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            request_hash = _post_sprint_triage_ledger_request_hash(
                project_id=project_id,
                sprint_id=sprint_id,
                expected_state=expected_state,
                impact=impact,
                affected_requirements=affected_requirements,
                affected_task_ids=affected_task_ids,
                affected_story_ids=affected_story_ids,
                affected_backlog_item_ids=affected_backlog_item_ids,
                affected_roadmap_item_ids=affected_roadmap_item_ids,
                affected_layers=affected_layers,
                learning_summary=learning_summary,
                decision_reason=decision_reason,
                idempotency_key=idempotency_key,
                replace_existing=replace_existing,
                expected_triage_fingerprint=expected_triage_fingerprint,
                changed_by=changed_by,
            )
        except PostSprintTriageValidationError as exc:
            return _post_sprint_triage_validation_error(exc)

        engine = get_engine()
        ledger = MutationLedgerRepository(engine=engine)
        ledger_result = ledger.create_or_load(
            command=_SPRINT_TRIAGE_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=f"sprint-triage:{uuid4()}",
            changed_by=changed_by,
            lease_owner=_SPRINT_TRIAGE_LEASE_OWNER,
            now=datetime.now(UTC),
        )
        if ledger_result.error_code is not None:
            return _ledger_error(ledger_result)
        if ledger_result.replayed:
            replay = ledger_result.response or {}
            if isinstance(replay.get("data"), dict):
                replay["data"].setdefault("idempotency", {})["replayed"] = True
            return replay

        try:
            state = anyio.run(self._ensure_session, str(project_id))
            resolved_sprint_id = (
                sprint_id
                if sprint_id is not None
                else _int_or_none(state.get("latest_completed_sprint_id"))
            )
            _assert_post_sprint_triage_state(
                state=state,
                expected_state=expected_state,
                sprint_id=resolved_sprint_id,
            )
            resolved_sprint_id_int = cast("int", resolved_sprint_id)
            recorded_at = _now_iso()
            triage_payload = build_triage_payload(
                project_id=project_id,
                sprint_id=resolved_sprint_id_int,
                impact=impact,
                affected_requirements=affected_requirements,
                affected_task_ids=affected_task_ids,
                affected_story_ids=affected_story_ids,
                affected_backlog_item_ids=affected_backlog_item_ids,
                affected_roadmap_item_ids=affected_roadmap_item_ids,
                affected_layers=affected_layers,
                learning_summary=learning_summary,
                decision_reason=decision_reason,
                idempotency_key=idempotency_key,
                replace_existing=replace_existing,
                recorded_at=recorded_at,
                recorded_by=changed_by,
            )
            normalized_replace_existing = bool(triage_payload["replace_existing"])
            with Session(engine) as session:
                sprint = _completed_sprint(
                    session=session,
                    project_id=project_id,
                    sprint_id=resolved_sprint_id_int,
                )
                current_triage = current_triage_for_latest_sprint(state)
                history = _post_sprint_triage_history(state)
                event_metadata: dict[str, Any] = {
                    "project_id": project_id,
                    "sprint_id": resolved_sprint_id_int,
                    "replace_existing": normalized_replace_existing,
                    "triage_fingerprint": triage_payload["triage_fingerprint"],
                }

                if current_triage is None:
                    if normalized_replace_existing:
                        _raise_triage_fingerprint_mismatch(
                            expected_triage_fingerprint=expected_triage_fingerprint,
                            current_triage_fingerprint=None,
                            sprint_id=resolved_sprint_id_int,
                        )
                    state["post_sprint_triage"] = triage_payload
                    history.append(
                        _triage_history_entry(
                            triage_payload,
                            history_action="recorded",
                            recorded_at=recorded_at,
                            recorded_by=changed_by,
                        )
                    )
                    event_metadata["history_action"] = "recorded"
                else:
                    if not normalized_replace_existing:
                        _raise_triage_already_recorded(
                            sprint_id=resolved_sprint_id_int,
                            current_triage=current_triage,
                        )
                    current_fingerprint = str(
                        current_triage.get("triage_fingerprint") or ""
                    )
                    if expected_triage_fingerprint != current_fingerprint:
                        _raise_triage_fingerprint_mismatch(
                            expected_triage_fingerprint=expected_triage_fingerprint,
                            current_triage_fingerprint=current_fingerprint,
                            sprint_id=resolved_sprint_id_int,
                        )
                    history.append(
                        _triage_history_entry(
                            current_triage,
                            history_action="superseded",
                            recorded_at=recorded_at,
                            recorded_by=changed_by,
                        )
                    )
                    state["post_sprint_triage"] = triage_payload
                    history.append(
                        _triage_history_entry(
                            triage_payload,
                            history_action="corrected",
                            recorded_at=recorded_at,
                            recorded_by=changed_by,
                        )
                    )
                    event_metadata["history_action"] = "corrected"
                    event_metadata["superseded_triage_fingerprint"] = (
                        current_fingerprint
                    )

                state["post_sprint_triage_history"] = history
                self._save_session_state(str(project_id), state)
                session.add(
                    WorkflowEvent(
                        event_type=WorkflowEventType.POST_SPRINT_TRIAGE_RECORDED,
                        product_id=project_id,
                        sprint_id=resolved_sprint_id_int,
                        session_id=str(project_id),
                        event_metadata=json.dumps(event_metadata),
                    )
                )
                session.commit()
                data = _post_sprint_review_payload(
                    state=state,
                    project_id=project_id,
                    sprint=sprint,
                    sprint_id=resolved_sprint_id_int,
                )
                data["idempotency"] = {"replayed": False}
                response = _data_envelope(data)
        except PostSprintTriageValidationError as exc:
            response = _post_sprint_triage_validation_error(exc)
        except _SprintTriageError as exc:
            response = _post_sprint_triage_error(exc)
        except SprintPhaseError as exc:
            response = _phase_error(exc)

        raw_response_data = response.get("data")
        response_data = (
            cast("dict[str, Any]", raw_response_data)
            if isinstance(raw_response_data, dict)
            else {}
        )
        ledger.finalize_success(
            mutation_event_id=cast("int", ledger_result.ledger.mutation_event_id),
            lease_owner=_SPRINT_TRIAGE_LEASE_OWNER,
            after=response_data,
            response=response,
            now=datetime.now(UTC),
        )
        return response

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
            state = await self._ensure_session(str(project_id))
            allow_completed_sprint_generation = (
                _allow_completed_sprint_generation(state)
            )

            async def load_state() -> dict[str, Any]:
                return state

            data = await generate_sprint_plan(
                project_id=project_id,
                load_state=load_state,
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
                allow_completed_sprint_generation=allow_completed_sprint_generation,
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

    async def _start(
        self,
        project_id: int,
        sprint_id: int | None,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        state = await self._ensure_session(str(project_id))
        with Session(get_engine()) as session:

            def _persist_started_sprint(resolved_sprint_id: int) -> Sprint | None:
                sprint = _get_saved_sprint(session, project_id, resolved_sprint_id)
                if sprint is None:
                    return None
                if sprint.started_at is None:
                    sprint.started_at = datetime.now(UTC)
                    sprint.status = SprintStatus.ACTIVE
                    session.add(sprint)
                    session.add(
                        WorkflowEvent(
                            event_type=WorkflowEventType.SPRINT_STARTED,
                            product_id=project_id,
                            sprint_id=resolved_sprint_id,
                            session_id=str(project_id),
                            event_metadata=json.dumps(
                                {
                                    "team_id": sprint.team_id,
                                    "planned_start_date": str(sprint.start_date),
                                    "planned_end_date": str(sprint.end_date),
                                }
                            ),
                        )
                    )
                    session.commit()
                    session.refresh(sprint)
                return _get_saved_sprint(session, project_id, resolved_sprint_id)

            try:
                data = start_sprint_execution(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    expected_state=expected_state,
                    idempotency_key=idempotency_key,
                    load_state=lambda: state,
                    save_state=lambda state: self._save_session_state(
                        str(project_id), state
                    ),
                    now_iso=_now_iso,
                    resolve_sprint_id=lambda: self._current_planned_sprint_id(
                        project_id
                    ),
                    load_sprint=lambda resolved_id: _get_saved_sprint(
                        session, project_id, resolved_id
                    ),
                    load_other_active=lambda resolved_id: session.exec(
                        select(Sprint).where(
                            Sprint.product_id == project_id,
                            Sprint.status == SprintStatus.ACTIVE,
                            Sprint.sprint_id != resolved_id,
                        )
                    ).first(),
                    persist_started_sprint=_persist_started_sprint,
                    build_runtime_summary=lambda: _build_sprint_runtime_summary(
                        session=session,
                        project_id=project_id,
                    ),
                    serialize_sprint=lambda sprint, runtime_summary: (
                        _serialize_sprint_detail(
                            sprint,
                            runtime_summary=runtime_summary,
                        )
                    ),
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

    def _sync_completed_sprint_state(self, *, project_id: int, sprint_id: int) -> None:
        state = self._workflow_service.get_session_status(str(project_id)) or {}
        completed_at = self._completed_sprint_timestamp(
            project_id=project_id,
            sprint_id=sprint_id,
        )
        state["fsm_state"] = "SPRINT_COMPLETE"
        if completed_at is not None:
            state["fsm_state_entered_at"] = completed_at
        state["active_sprint_id"] = None
        state["latest_completed_sprint_id"] = sprint_id
        state["sprint_completed_at"] = completed_at
        self._save_session_state(str(project_id), state)

    def _completed_sprint_timestamp(
        self,
        *,
        project_id: int,
        sprint_id: int,
    ) -> str | None:
        with Session(get_engine()) as session:
            sprint = _get_saved_sprint(session, project_id, sprint_id)
            if sprint is None:
                return None
            return _serialize_utc_temporal(
                sprint.completed_at or sprint.updated_at or sprint.created_at
            )

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

    def _current_active_sprint_id(self, project_id: int) -> int | None:
        with Session(get_engine()) as session:
            sprint = session.exec(
                select(Sprint)
                .where(
                    Sprint.product_id == project_id,
                    Sprint.status == SprintStatus.ACTIVE,
                )
                .order_by(
                    cast("Any", Sprint.started_at).desc(),
                    cast("Any", Sprint.sprint_id).desc(),
                )
            ).first()
            return sprint.sprint_id if sprint else None

    def _resolve_execution_sprint_id(
        self,
        project_id: int,
        *,
        sprint_id: int | None,
        session: Session,
    ) -> int:
        if sprint_id is not None:
            return sprint_id
        sprint = session.exec(
            select(Sprint)
            .where(
                Sprint.product_id == project_id,
                Sprint.status == SprintStatus.ACTIVE,
            )
            .order_by(
                cast("Any", Sprint.started_at).desc(),
                cast("Any", Sprint.sprint_id).desc(),
            )
        ).first()
        if sprint is None:
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
        if sprint is None or sprint.sprint_id is None:
            completed_sprint = session.exec(
                select(Sprint)
                .where(
                    Sprint.product_id == project_id,
                    Sprint.status == SprintStatus.COMPLETED,
                )
                .order_by(
                    cast("Any", Sprint.completed_at).desc(),
                    cast("Any", Sprint.updated_at).desc(),
                    cast("Any", Sprint.sprint_id).desc(),
                )
            ).first()
            completed_sprint_id = (
                completed_sprint.sprint_id if completed_sprint is not None else None
            )
            _raise_no_execution_sprint_found(
                project_id=project_id,
                completed_sprint_id=completed_sprint_id,
            )
        return sprint.sprint_id


def _allow_completed_sprint_generation(state: dict[str, Any]) -> bool:
    """Return whether SPRINT_COMPLETE may bridge into Sprint generation."""
    if state.get("fsm_state") != "SPRINT_COMPLETE":
        return False
    try:
        workflow_state.assert_downstream_backlog_not_stale(state)
    except workflow_state.DownstreamBacklogStaleError as exc:
        raise SprintPhaseError(str(exc)) from exc

    current_triage = current_triage_for_latest_sprint(state)
    if current_triage is None or current_triage.get("impact") != "none":
        impact = (
            current_triage.get("impact")
            if isinstance(current_triage, dict)
            else None
        )
        message = (
            "Sprint generation from SPRINT_COMPLETE requires current "
            "post-sprint triage impact none."
        )
        raise SprintPhaseError(
            message,
            details={
                "fsm_state": state.get("fsm_state"),
                "latest_completed_sprint_id": state.get("latest_completed_sprint_id"),
                "current_post_sprint_triage_impact": impact,
            },
            remediation=[
                (
                    "Run agileforge sprint triage --project-id <project_id> "
                    "--expected-state SPRINT_COMPLETE --impact none."
                )
            ],
        )
    return True


def _raise_sprint_not_found() -> NoReturn:
    message = "Sprint not found"
    raise SprintPhaseError(message, status_code=404)


def _raise_no_execution_sprint_found(
    *,
    project_id: int,
    completed_sprint_id: int | None,
) -> NoReturn:
    message = "No active or planned Sprint found."
    details: dict[str, Any] = {"project_id": project_id}
    if completed_sprint_id is not None:
        details["latest_completed_sprint_id"] = completed_sprint_id
        remediation = [
            (
                f"Run agileforge sprint status --project-id {project_id} "
                f"--sprint-id {completed_sprint_id} to inspect the latest "
                "completed Sprint."
            )
        ]
    else:
        remediation = [
            (
                f"Run agileforge sprint status --project-id {project_id} "
                "--sprint-id <completed_sprint_id> to inspect completed Sprint "
                "history."
            )
        ]
    raise SprintPhaseError(
        message,
        status_code=404,
        details=details,
        remediation=remediation,
    )


def _raise_task_not_found() -> NoReturn:
    message = "Task not found in this Sprint."
    raise SprintPhaseError(message, status_code=404)


def _raise_story_not_found() -> NoReturn:
    message = "Story not found"
    raise SprintPhaseError(message, status_code=404)


def _saved_sprint_query() -> SelectOfScalar[Sprint]:
    """Return saved Sprint query with execution relationships loaded."""
    return select(Sprint).options(
        selectinload(cast("Any", Sprint.team)),
        selectinload(cast("Any", Sprint.stories)).selectinload(
            cast("Any", UserStory.tasks)
        ),
    )


def _get_saved_sprint(
    session: Session,
    project_id: int,
    sprint_id: int,
) -> Sprint | None:
    """Return a saved Sprint constrained to one project."""
    return session.exec(
        _saved_sprint_query().where(
            Sprint.product_id == project_id,
            Sprint.sprint_id == sprint_id,
        )
    ).first()


def _resolve_review_sprint_id(
    *,
    session: Session,
    project_id: int,
    sprint_id: int | None,
    state: dict[str, Any],
) -> int:
    """Resolve a completed Sprint id for post-sprint review."""
    if sprint_id is not None:
        return sprint_id
    latest_completed_sprint_id = _int_or_none(state.get("latest_completed_sprint_id"))
    if latest_completed_sprint_id is not None:
        return latest_completed_sprint_id
    sprint = session.exec(
        select(Sprint)
        .where(
            Sprint.product_id == project_id,
            Sprint.status == SprintStatus.COMPLETED,
        )
        .order_by(
            cast("Any", Sprint.completed_at).desc(),
            cast("Any", Sprint.updated_at).desc(),
            cast("Any", Sprint.sprint_id).desc(),
        )
    ).first()
    if sprint is None or sprint.sprint_id is None:
        message = "No completed Sprint found."
        raise SprintPhaseError(
            message,
            details={"project_id": project_id},
            remediation=["Complete a Sprint before running sprint review."],
        )
    return sprint.sprint_id


def _completed_sprint(
    *,
    session: Session,
    project_id: int,
    sprint_id: int,
) -> Sprint:
    """Return a completed Sprint constrained to one project."""
    sprint = _get_saved_sprint(session, project_id, sprint_id)
    if sprint is None:
        _raise_sprint_not_found()
    if sprint.status != SprintStatus.COMPLETED:
        message = "Sprint is not completed."
        raise SprintPhaseError(
            message,
            details={
                "project_id": project_id,
                "sprint_id": sprint_id,
                "current_status": _enum_value(sprint.status),
                "required_status": SprintStatus.COMPLETED.value,
            },
            remediation=["Complete the Sprint before running post-sprint review."],
        )
    return sprint


def _post_sprint_review_payload(
    *,
    state: dict[str, Any],
    project_id: int,
    sprint: Sprint,
    sprint_id: int,
) -> dict[str, Any]:
    """Build stable post-sprint review and triage response data."""
    latest_completed_sprint_id = _int_or_none(state.get("latest_completed_sprint_id"))
    is_latest_completed_sprint = latest_completed_sprint_id == sprint_id
    current_triage = (
        current_triage_for_latest_sprint(state) if is_latest_completed_sprint else None
    )
    triage_required = (
        post_sprint_triage_required(state) if is_latest_completed_sprint else False
    )
    return {
        "project_id": project_id,
        "fsm_state": state.get("fsm_state"),
        "latest_completed_sprint_id": state.get("latest_completed_sprint_id"),
        "planned_sprint_id": state.get("planned_sprint_id"),
        "sprint_id": sprint_id,
        "post_sprint_triage_required": triage_required,
        "post_sprint_triage": current_triage,
        "post_sprint_triage_history": _post_sprint_triage_history(
            state,
            sprint_id=sprint_id,
        ),
        "sprint": {
            "id": sprint.sprint_id,
            "goal": sprint.goal,
            "status": _enum_value(sprint.status),
            "started_at": _serialize_temporal(sprint.started_at),
            "completed_at": _serialize_temporal(sprint.completed_at),
            "start_date": _serialize_temporal(sprint.start_date),
            "end_date": _serialize_temporal(sprint.end_date),
            "team_id": sprint.team_id,
            "team_name": sprint.team.name if sprint.team else None,
            "story_count": len(_sorted_sprint_stories(sprint)),
        },
    }


def _post_sprint_triage_ledger_request_hash(  # noqa: PLR0913
    *,
    project_id: int,
    sprint_id: int | None,
    expected_state: str,
    impact: str,
    affected_requirements: list[str] | None,
    affected_task_ids: list[int] | None,
    affected_story_ids: list[int] | None,
    affected_backlog_item_ids: list[str] | None,
    affected_roadmap_item_ids: list[str] | None,
    affected_layers: list[str] | None,
    learning_summary: str,
    decision_reason: str,
    idempotency_key: str,
    replace_existing: bool | str,
    expected_triage_fingerprint: str | None,
    changed_by: str,
) -> str:
    """Return an idempotency hash from normalized caller inputs."""
    return canonical_hash(
        {
            "project_id": _int_or_none(project_id),
            "sprint_id": _int_or_none(sprint_id) if sprint_id is not None else None,
            "expected_state": _triage_ledger_text(expected_state),
            "impact": _triage_ledger_text(impact).lower(),
            "affected_requirements": _triage_ledger_text_list(
                affected_requirements
            ),
            "affected_task_ids": _triage_ledger_positive_int_list(
                affected_task_ids
            ),
            "affected_story_ids": _triage_ledger_positive_int_list(
                affected_story_ids
            ),
            "affected_backlog_item_ids": _triage_ledger_text_list(
                affected_backlog_item_ids
            ),
            "affected_roadmap_item_ids": _triage_ledger_text_list(
                affected_roadmap_item_ids
            ),
            "affected_layers": _triage_ledger_layers(affected_layers),
            "learning_summary": _triage_ledger_text(learning_summary),
            "decision_reason": _triage_ledger_text(decision_reason),
            "idempotency_key": _triage_ledger_text(idempotency_key),
            "replace_existing": _triage_ledger_replace_existing(replace_existing),
            "expected_triage_fingerprint": (
                _triage_ledger_text(expected_triage_fingerprint)
                if expected_triage_fingerprint is not None
                else None
            ),
            "changed_by": _triage_ledger_text(changed_by),
        }
    )


def _triage_ledger_text(value: object) -> str:
    """Normalize text for Sprint triage idempotency hashing."""
    if value is None:
        return ""
    return str(value).strip()


def _triage_ledger_text_list(values: object) -> list[str] | dict[str, str]:
    """Normalize text-list caller inputs for Sprint triage idempotency hashing."""
    if values is None:
        return []
    if not isinstance(values, list):
        return {"invalid_container_type": type(values).__name__}
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _triage_ledger_text(value)
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _triage_ledger_positive_int_list(values: object) -> list[int] | dict[str, str]:
    """Normalize int-list caller inputs for Sprint triage idempotency hashing."""
    if values is None:
        return []
    if not isinstance(values, list):
        return {"invalid_container_type": type(values).__name__}
    normalized: list[int] = []
    seen: set[int] = set()
    for value in values:
        item_id = _triage_ledger_positive_int_or_none(value)
        if item_id is None or item_id in seen:
            continue
        normalized.append(item_id)
        seen.add(item_id)
    return normalized


def _triage_ledger_layers(values: object) -> list[str] | dict[str, str]:
    """Normalize layer-list caller inputs for Sprint triage idempotency hashing."""
    normalized = _triage_ledger_text_list(values)
    if isinstance(normalized, dict):
        return normalized
    return sorted({layer.lower() for layer in normalized if layer})


def _triage_ledger_positive_int_or_none(value: object) -> int | None:
    """Return a positive integer for int-shaped triage list values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isinstance(value, (str, bytes, bytearray)) and value != normalized:
        return None
    if normalized <= 0:
        return None
    return normalized


def _triage_ledger_replace_existing(value: bool | str) -> bool:
    """Parse replace_existing for Sprint triage idempotency hashing."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise PostSprintTriageValidationError(
        code=TRIAGE_FIELD_INVALID,
        message="replace_existing must be a boolean.",
        details={"field": "replace_existing"},
        remediation=["Provide replace_existing as true or false."],
    )


def _assert_post_sprint_triage_state(
    *,
    state: dict[str, Any],
    expected_state: str,
    sprint_id: int | None,
) -> None:
    """Raise when the current workflow state cannot accept Sprint triage."""
    current_state = str(state.get("fsm_state") or "")
    latest_completed_sprint_id = _int_or_none(state.get("latest_completed_sprint_id"))
    if (
        current_state == "SPRINT_COMPLETE"
        and expected_state == "SPRINT_COMPLETE"
        and sprint_id is not None
        and latest_completed_sprint_id == sprint_id
    ):
        return
    raise _SprintTriageError(
        ErrorCode.TRIAGE_EXPECTED_STATE_MISMATCH,
        "Post-sprint triage requires the latest completed Sprint.",
        details={
            "current_state": current_state,
            "expected_state": expected_state,
            "required_state": "SPRINT_COMPLETE",
            "sprint_id": sprint_id,
            "latest_completed_sprint_id": latest_completed_sprint_id,
        },
        remediation=[
            "Run agileforge sprint review --project-id <project_id>.",
            "Retry triage only while the project is in SPRINT_COMPLETE.",
        ],
    )


def _post_sprint_triage_history(
    state: dict[str, Any],
    *,
    sprint_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return stored triage history entries without mutating state."""
    history = state.get("post_sprint_triage_history")
    if not isinstance(history, list):
        return []
    entries = [dict(entry) for entry in history if isinstance(entry, dict)]
    if sprint_id is None:
        return entries
    return [
        entry
        for entry in entries
        if _int_or_none(entry.get("sprint_id")) == sprint_id
    ]


def _triage_history_entry(
    triage_payload: dict[str, Any],
    *,
    history_action: str,
    recorded_at: str,
    recorded_by: str,
) -> dict[str, Any]:
    """Return a JSON-ready triage history entry."""
    entry = cast("dict[str, Any]", _json_ready(dict(triage_payload)))
    entry["history_action"] = history_action
    entry["history_recorded_at"] = recorded_at
    entry["history_recorded_by"] = recorded_by
    return entry


def _raise_triage_already_recorded(
    *,
    sprint_id: int,
    current_triage: dict[str, Any],
) -> NoReturn:
    """Reject an unguarded triage write over existing current metadata."""
    raise _SprintTriageError(
        ErrorCode.TRIAGE_ALREADY_RECORDED,
        "Post-sprint triage has already been recorded.",
        details={
            "sprint_id": sprint_id,
            "current_triage_fingerprint": current_triage.get("triage_fingerprint"),
        },
        remediation=[
            "Pass replace_existing=true with expected_triage_fingerprint to correct it."
        ],
    )


def _raise_triage_fingerprint_mismatch(
    *,
    expected_triage_fingerprint: str | None,
    current_triage_fingerprint: str | None,
    sprint_id: int,
) -> NoReturn:
    """Reject a guarded correction whose current triage fingerprint changed."""
    raise _SprintTriageError(
        ErrorCode.TRIAGE_FINGERPRINT_MISMATCH,
        "Post-sprint triage fingerprint did not match.",
        details={
            "sprint_id": sprint_id,
            "expected_triage_fingerprint": expected_triage_fingerprint,
            "current_triage_fingerprint": current_triage_fingerprint,
        },
        remediation=[
            "Run agileforge sprint review --project-id <project_id> and retry.",
        ],
    )


def _execution_sprint_and_task_view(
    *,
    session: Session,
    project_id: int,
    sprint_id: int | None,
    resolve_sprint_id: _ResolveSprintId,
) -> tuple[Sprint, dict[str, Any]]:
    """Load the resolved Sprint and its dependency-aware task view."""
    resolved_sprint_id = resolve_sprint_id(
        project_id,
        sprint_id=sprint_id,
        session=session,
    )
    sprint = _get_saved_sprint(session, project_id, resolved_sprint_id)
    if sprint is None:
        _raise_sprint_not_found()
    task_view = _build_sprint_task_view(
        session=session,
        project_id=project_id,
        sprint=sprint,
    )
    return sprint, task_view


def _task_row_from_view(
    task_view: dict[str, Any],
    *,
    task_id: int,
) -> dict[str, Any] | None:
    """Return one serialized task row by id."""
    tasks = task_view.get("tasks")
    if not isinstance(tasks, list):
        return None
    for row in tasks:
        if isinstance(row, dict) and row.get("task_id") == task_id:
            return row
    return None


def _story_for_task_row(sprint: Sprint, task_row: dict[str, Any]) -> UserStory:
    """Return the Sprint story for one serialized task row."""
    story_id = task_row.get("story_id")
    for story in sprint.stories:
        if story.story_id == story_id:
            return story
    _raise_task_not_found()


def _task_for_row(story: UserStory, task_row: dict[str, Any]) -> Task:
    """Return the ORM task for one serialized task row."""
    task_id = task_row.get("task_id")
    for task in story.tasks:
        if task.task_id == task_id:
            return task
    _raise_task_not_found()


def _enum_value(value: object) -> str | None:
    """Return enum value text for JSON payloads."""
    enum_value = getattr(value, "value", value)
    return str(enum_value) if enum_value is not None else None


def _serialize_temporal(value: object) -> str | None:
    """Serialize date/datetime-like values for CLI JSON."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _serialize_utc_temporal(value: object) -> str | None:
    """Serialize timestamp-like values as UTC ISO-8601 strings."""
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _serialize_temporal(value)


def _sorted_sprint_stories(sprint: Sprint) -> list[UserStory]:
    """Return Sprint stories in stable planning order."""
    return sorted(
        sprint.stories,
        key=lambda story: (
            story.rank or "",
            story.story_id or 0,
        ),
    )


def _serialize_sprint_story(story: UserStory) -> dict[str, Any]:
    """Return compact story detail for Sprint execution commands."""
    return {
        "story_id": story.story_id,
        "title": story.title,
        "rank": story.rank,
        "status": _enum_value(story.status),
        "story_points": story.story_points,
        "task_count": len(story.tasks),
    }


def _serialize_sprint_detail(
    sprint: Sprint,
    *,
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return Sprint detail payload for CLI execution commands."""
    stories = _sorted_sprint_stories(sprint)
    return {
        "id": sprint.sprint_id,
        "goal": sprint.goal,
        "status": _enum_value(sprint.status),
        "started_at": _serialize_temporal(sprint.started_at),
        "completed_at": _serialize_temporal(sprint.completed_at),
        "start_date": _serialize_temporal(sprint.start_date),
        "end_date": _serialize_temporal(sprint.end_date),
        "team_id": sprint.team_id,
        "team_name": sprint.team.name if sprint.team else None,
        "story_count": len(stories),
        "selected_stories": [_serialize_sprint_story(story) for story in stories],
        "runtime_summary": runtime_summary,
    }


def _build_sprint_runtime_summary(
    *,
    session: Session,
    project_id: int,
) -> dict[str, Any]:
    """Return active/planned Sprint summary for guard messaging."""
    active = session.exec(
        select(Sprint).where(
            Sprint.product_id == project_id,
            Sprint.status == SprintStatus.ACTIVE,
        )
    ).first()
    planned = session.exec(
        select(Sprint).where(
            Sprint.product_id == project_id,
            Sprint.status == SprintStatus.PLANNED,
        )
    ).first()
    return {
        "active_sprint_id": active.sprint_id if active else None,
        "planned_sprint_id": planned.sprint_id if planned else None,
    }


def _build_sprint_task_view(
    *,
    session: Session,
    project_id: int,
    sprint: Sprint,
) -> dict[str, Any]:
    """Return dependency-aware task rows and read-side diagnostics."""
    graph = load_story_dependency_graph(session, project_id=project_id)
    active_edge_count = sum(
        len(prerequisites) for prerequisites in graph.active_edges.values()
    )
    warnings: list[dict[str, Any]] = []
    ordering = "topological"

    if graph.cycle_paths:
        ordering = "rank_fallback"
        ordered_stories = _sorted_sprint_stories(sprint)
        warnings.append(
            {
                "code": "SPRINT_TASK_DEPENDENCY_CYCLE_FALLBACK",
                "message": (
                    "Active story dependencies contain a cycle; Sprint tasks are "
                    "returned using rank fallback order."
                ),
                "details": {"cycle_paths": graph.cycle_paths},
            }
        )
    else:
        ordered_stories = _topologically_sorted_sprint_stories(
            sprint,
            active_edges=graph.active_edges,
        )

    story_statuses = _story_statuses(
        session,
        sprint=sprint,
        active_edges=graph.active_edges,
    )
    current_story_ids = {
        story.story_id for story in sprint.stories if story.story_id is not None
    }
    execution_index_by_story_id = {
        story.story_id: index
        for index, story in enumerate(ordered_stories, start=1)
        if story.story_id is not None
    }
    downstream_edges = _downstream_edges(
        active_edges=graph.active_edges,
        current_story_ids=current_story_ids,
    )
    dependency_order_source = (
        "rank_fallback" if ordering == "rank_fallback" else "active_story_dependencies"
    )
    story_metadata = _story_dependency_metadata(
        ordered_stories,
        context=_StoryDependencyMetadataContext(
            active_edges=graph.active_edges,
            downstream_edges=downstream_edges,
            story_statuses=story_statuses,
            execution_index_by_story_id=execution_index_by_story_id,
            dependency_order_source=dependency_order_source,
        ),
    )
    risk_metadata = _story_dependency_risk_metadata(
        ordered_stories,
        active_edges=graph.active_edges,
        story_statuses=story_statuses,
    )
    for story_id, metadata in risk_metadata.items():
        story_metadata.setdefault(story_id, {}).update(metadata)
    dependency_review_required_story_count = sum(
        1
        for metadata in risk_metadata.values()
        if metadata["dependency_review_required"]
    )
    if dependency_review_required_story_count:
        warnings.append(_dependency_review_required_warning(risk_metadata))
    tasks = _serialize_sprint_tasks(
        ordered_stories,
        story_dependency_metadata=story_metadata,
    )
    return {
        "tasks": tasks,
        "warnings": warnings,
        "dependency_summary": {
            "active_edge_count": active_edge_count,
            "cycle_count": len(graph.cycle_paths),
            "blocked_story_count": sum(
                1 for metadata in story_metadata.values() if metadata["is_blocked"]
            ),
            "dependency_review_required_story_count": (
                dependency_review_required_story_count
            ),
            "ordering": ordering,
        },
    }


def _next_task_ticket(
    *,
    session: Session,
    project_id: int,
    sprint: Sprint,
    task_view: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Return current in-progress work or the first unblocked To Do task."""
    tasks = [task for task in task_view["tasks"] if isinstance(task, dict)]
    in_progress = [
        task for task in tasks if task.get("status") == TaskStatus.IN_PROGRESS.value
    ]
    candidates = in_progress or [
        task
        for task in tasks
        if task.get("status") == TaskStatus.TO_DO.value
        and task.get("is_blocked") is not True
        and task.get("dependency_review_required") is not True
    ]
    if not candidates:
        return None, "no_available_task"
    row = sorted(
        candidates,
        key=lambda task: (
            int(task.get("task_execution_order") or _DEPENDENCY_ORDER_FALLBACK_INDEX),
            int(task.get("task_id") or 0),
        ),
    )[0]
    return (
        _build_task_ticket(
            session=session,
            project_id=project_id,
            sprint=sprint,
            task_row=row,
            dependency_summary=task_view["dependency_summary"],
        ),
        "in_progress_task" if in_progress else "next_unblocked_todo",
    )


def _build_task_ticket(
    *,
    session: Session,
    project_id: int,
    sprint: Sprint,
    task_row: dict[str, Any],
    dependency_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return the agent-native task ticket payload."""
    story = _story_for_task_row(sprint, task_row)
    task = _task_for_row(story, task_row)
    sprint_id = cast("int", sprint.sprint_id)
    task_id = cast("int", task.task_id)
    history = _task_execution_history(
        session=session,
        project_id=project_id,
        sprint_id=sprint_id,
        task_id=task_id,
    )
    fingerprint = _task_row_fingerprint(sprint=sprint, task_row=task_row)
    return {
        "ticket_type": "sprint_task",
        "project_id": project_id,
        "sprint_id": sprint_id,
        "task": {
            "task_id": task_id,
            "description": task.description,
            "status": _enum_value(task.status),
            "task_kind": task_row.get("task_kind"),
            "task_execution_order": task_row.get("task_execution_order"),
            "task_fingerprint": fingerprint,
        },
        "story": {
            "story_id": story.story_id,
            "title": story.title,
            "status": _enum_value(story.status),
            "story_description": story.story_description,
            "acceptance_criteria": story.acceptance_criteria,
            "story_execution_order": task_row.get("story_execution_order"),
            "story_points": story.story_points,
            "rank": story.rank,
        },
        "execution": {
            "is_blocked": task_row.get("is_blocked") is True,
            "blocked_by_story_ids": list(task_row.get("blocked_by_story_ids") or []),
            "direct_blocked_by_story_ids": list(
                task_row.get("direct_blocked_by_story_ids") or []
            ),
            "unblocks_story_ids": list(task_row.get("unblocks_story_ids") or []),
            "dependency_summary": dependency_summary,
            "dependency_order_source": task_row.get("dependency_order_source"),
            "dependency_review_required": (
                task_row.get("dependency_review_required") is True
            ),
            "missing_dependency_story_ids": list(
                task_row.get("missing_dependency_story_ids") or []
            ),
            "dependency_review_candidates": list(
                task_row.get("dependency_review_candidates") or []
            ),
        },
        "work_contract": {
            "checklist_items": list(task_row.get("checklist_items") or []),
            "artifact_targets": list(task_row.get("artifact_targets") or []),
            "relevant_invariant_ids": list(
                task_row.get("relevant_invariant_ids") or []
            ),
            "workstream_tags": list(task_row.get("workstream_tags") or []),
            "done_requires": {
                "outcome_summary": True,
                "checklist_result": True,
                "artifact_refs": "required_if_artifact_targets_present",
                "validation_summary": True,
            },
        },
        "history": {
            "latest_entry": history.get("latest_entry"),
            "log_count": len(cast("list[Any]", history.get("history") or [])),
        },
        "guards": {
            "expected_status": _enum_value(task.status),
            "expected_task_fingerprint": fingerprint,
        },
        "next_actions": {
            "update": (
                f"agileforge sprint task update --project-id {project_id} "
                f'--task-id {task_id} --expected-status "{_enum_value(task.status)}" '
                f"--expected-task-fingerprint {fingerprint} "
                "--idempotency-key <idempotency_key> --status <status>"
            ),
            "history": (
                f"agileforge sprint task history --project-id {project_id} "
                f"--task-id {task_id}"
            ),
        },
    }


def _task_row_fingerprint(*, sprint: Sprint, task_row: dict[str, Any]) -> str:
    """Return a stable guard fingerprint for one task ticket."""
    return canonical_hash(
        {
            "sprint_id": sprint.sprint_id,
            "sprint_status": _enum_value(sprint.status),
            "task": task_row,
        }
    )


def _serialize_sprint_tasks(
    stories: list[UserStory],
    *,
    story_dependency_metadata: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return Sprint task rows grouped by dependency-aware story order."""
    dependency_metadata = story_dependency_metadata or {}
    rows: list[dict[str, Any]] = []
    task_execution_order = 1
    for story in stories:
        tasks = sorted(story.tasks, key=lambda task: task.task_id or 0)
        for task in tasks:
            metadata = parse_task_metadata(
                task.metadata_json,
                task_id=task.task_id,
            )
            row = {
                "task_id": task.task_id,
                "story_id": story.story_id,
                "story_title": story.title,
                "description": task.description,
                "status": _enum_value(task.status),
                "task_kind": metadata.task_kind,
                "artifact_targets": list(metadata.artifact_targets),
                "workstream_tags": list(metadata.workstream_tags),
                "relevant_invariant_ids": list(metadata.relevant_invariant_ids),
                "checklist_items": list(metadata.checklist_items),
                "task_execution_order": task_execution_order,
            }
            if story.story_id is not None:
                row.update(dependency_metadata.get(story.story_id, {}))
            rows.append(row)
            task_execution_order += 1
    return rows


def _task_summary(sprint: Sprint) -> dict[str, Any]:
    """Return status counts for Sprint tasks."""
    counts: dict[str, int] = {}
    tasks = _serialize_sprint_tasks(_sorted_sprint_stories(sprint))
    for task in tasks:
        status = str(task.get("status") or "Unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"task_count": len(tasks), "status_counts": counts}


def _normalize_update_status(value: str) -> TaskStatus:
    """Parse a CLI task status value."""
    normalized = value.strip().replace("_", " ").replace("-", " ").lower()
    for status in TaskStatus:
        if status.value.lower() == normalized:
            return status
    message = f"Unsupported task status: {value}"
    raise ValueError(message)


def _normalize_acceptance_result(value: str | None) -> TaskAcceptanceResult | None:
    """Parse a CLI task acceptance result value."""
    if value is None:
        return None
    normalized = value.strip().lower()
    for result in TaskAcceptanceResult:
        if result.value == normalized:
            return result
    message = f"Unsupported checklist result: {value}"
    raise ValueError(message)


def _task_execution_notes(
    *,
    notes: str | None,
    validation_summary: str | None,
) -> str | None:
    """Combine optional human notes and validation evidence into one log field."""
    parts = []
    if notes and notes.strip():
        parts.append(notes.strip())
    if validation_summary and validation_summary.strip():
        parts.append(f"Validation: {validation_summary.strip()}")
    return "\n".join(parts) if parts else None


def _task_execution_write_request(  # noqa: PLR0913
    *,
    new_status: TaskStatus,
    outcome_summary: str | None,
    artifact_refs: list[str] | None,
    acceptance_result: TaskAcceptanceResult | None,
    validation_summary: str | None,
    notes: str | None,
    changed_by: str,
) -> TaskExecutionWriteRequest:
    """Build and validate a task execution write request."""
    if new_status == TaskStatus.DONE and not (
        validation_summary and validation_summary.strip()
    ):
        message = "validation_summary is required when marking a task Done."
        raise ValueError(message)
    return TaskExecutionWriteRequest(
        new_status=new_status,
        outcome_summary=outcome_summary,
        artifact_refs=artifact_refs,
        acceptance_result=acceptance_result,
        notes=_task_execution_notes(
            notes=notes,
            validation_summary=validation_summary,
        ),
        changed_by=changed_by,
    )


def _assert_task_update_guards(  # noqa: PLR0913
    *,
    task_row: dict[str, Any],
    expected_status: TaskStatus,
    expected_task_fingerprint: str,
    current_task_fingerprint: str,
    new_status: TaskStatus,
    artifact_refs: list[str] | None,
) -> None:
    """Fail closed when an agent update is stale or unsafe."""
    current_status = str(task_row.get("status") or "")
    if current_status != expected_status.value:
        message = (
            f"Task {task_row.get('task_id')} has status {current_status!r}, "
            f"expected {expected_status.value!r}."
        )
        raise SprintPhaseError(message, status_code=409)
    if expected_task_fingerprint != current_task_fingerprint:
        message = "Task ticket fingerprint is stale."
        raise SprintPhaseError(message, status_code=409)
    if current_status in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}:
        message = f"Task {task_row.get('task_id')} is terminal."
        raise SprintPhaseError(message, status_code=409)
    if (
        new_status == TaskStatus.DONE
        and task_row.get("artifact_targets")
        and not artifact_refs
    ):
        message = "SPRINT_TASK_ARTIFACT_REFS_REQUIRED: artifact refs are required."
        raise _SprintTaskUpdateError(
            message,
            status_code=409,
            details={
                "reason_code": "SPRINT_TASK_ARTIFACT_REFS_REQUIRED",
                "task_id": task_row.get("task_id"),
                "artifact_targets": list(task_row.get("artifact_targets") or []),
            },
        )
    if (
        new_status in {TaskStatus.IN_PROGRESS, TaskStatus.DONE}
        and task_row.get("dependency_review_required") is True
    ):
        missing_ids = list(task_row.get("missing_dependency_story_ids") or [])
        message = (
            "SPRINT_TASK_DEPENDENCY_REVIEW_REQUIRED: task needs dependency review."
        )
        raise _SprintTaskUpdateError(
            message,
            status_code=409,
            details={
                "reason_code": "SPRINT_TASK_DEPENDENCY_REVIEW_REQUIRED",
                "task_id": task_row.get("task_id"),
                "story_id": task_row.get("story_id"),
                "missing_dependency_story_ids": missing_ids,
                "dependency_review_candidates": list(
                    task_row.get("dependency_review_candidates") or []
                ),
            },
        )
    if (
        new_status in {TaskStatus.IN_PROGRESS, TaskStatus.DONE}
        and task_row.get("is_blocked") is True
    ):
        blocked_by = list(task_row.get("blocked_by_story_ids") or [])
        message = "SPRINT_TASK_BLOCKED: task cannot start or finish yet."
        raise _SprintTaskUpdateError(
            message,
            status_code=409,
            details={
                "reason_code": "SPRINT_TASK_BLOCKED",
                "task_id": task_row.get("task_id"),
                "blocked_by_story_ids": blocked_by,
            },
        )


def _record_task_execution(
    *,
    session: Session,
    project_id: int,
    sprint_id: int,
    task_id: int,
    write_request: TaskExecutionWriteRequest,
) -> dict[str, Any]:
    """Persist task execution using the domain service and serialize response."""

    def load_task() -> Task | None:
        return session.get(Task, task_id)

    def load_sprint() -> Sprint | None:
        return session.get(Sprint, sprint_id)

    def load_sprint_story(task: object) -> SprintStory | None:
        task_obj = cast("Task", task)
        return session.exec(
            select(SprintStory).where(
                SprintStory.sprint_id == sprint_id,
                SprintStory.story_id == task_obj.story_id,
            )
        ).first()

    def load_logs() -> list[TaskExecutionLog]:
        return list(
            session.exec(
                select(TaskExecutionLog)
                .where(TaskExecutionLog.task_id == task_id)
                .where(TaskExecutionLog.sprint_id == sprint_id)
                .order_by(
                    cast("Any", TaskExecutionLog.changed_at).desc(),
                    cast("Any", TaskExecutionLog.log_id).desc(),
                )
            ).all()
        )

    def persist_execution_log(**kwargs: object) -> None:
        task = cast("Task", kwargs["task"])
        session.add(task)
        session.add(
            TaskExecutionLog(
                task_id=cast("int", task.task_id),
                sprint_id=sprint_id,
                old_status=kwargs["old_status"],
                new_status=kwargs["new_status"],
                outcome_summary=kwargs["outcome_summary"],
                artifact_refs_json=kwargs["artifact_refs_json"],
                notes=kwargs["notes"],
                acceptance_result=kwargs["acceptance_result"],
                changed_by=kwargs["changed_by"],
            )
        )
        session.commit()

    try:
        payload = record_task_execution(
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
            load_task=load_task,
            load_sprint=load_sprint,
            load_sprint_story=load_sprint_story,
            load_logs=load_logs,
            new_status=write_request.new_status,
            outcome_summary=write_request.outcome_summary,
            artifact_refs=write_request.artifact_refs,
            notes=write_request.notes,
            acceptance_result=write_request.acceptance_result,
            changed_by=write_request.changed_by,
            parse_task_metadata=parse_task_metadata,
            persist_execution_log=persist_execution_log,
        )
    except TaskExecutionServiceError as exc:
        raise SprintPhaseError(exc.detail, status_code=exc.status_code) from exc
    return cast("dict[str, Any]", _json_ready(payload))


def _task_execution_history(
    *,
    session: Session,
    project_id: int,
    sprint_id: int,
    task_id: int,
) -> dict[str, Any]:
    """Load and serialize task execution history."""

    def load_task() -> Task | None:
        return session.get(Task, task_id)

    def load_sprint() -> Sprint | None:
        return session.get(Sprint, sprint_id)

    def load_sprint_story(task: object) -> SprintStory | None:
        task_obj = cast("Task", task)
        return session.exec(
            select(SprintStory).where(
                SprintStory.sprint_id == sprint_id,
                SprintStory.story_id == task_obj.story_id,
            )
        ).first()

    def load_logs() -> list[TaskExecutionLog]:
        return list(
            session.exec(
                select(TaskExecutionLog)
                .where(TaskExecutionLog.task_id == task_id)
                .where(TaskExecutionLog.sprint_id == sprint_id)
                .order_by(
                    cast("Any", TaskExecutionLog.changed_at).desc(),
                    cast("Any", TaskExecutionLog.log_id).desc(),
                )
            ).all()
        )

    try:
        payload = get_task_execution_history(
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
            load_task=load_task,
            load_sprint=load_sprint,
            load_sprint_story=load_sprint_story,
            load_logs=load_logs,
        )
    except TaskExecutionServiceError as exc:
        raise SprintPhaseError(exc.detail, status_code=exc.status_code) from exc
    return cast("dict[str, Any]", _json_ready(payload))


def _story_close_readiness_payload(
    *,
    session: Session,
    project_id: int,
    sprint_id: int,
    story_id: int,
) -> dict[str, Any]:
    """Return story close readiness plus the guard fingerprint."""
    try:
        payload = get_story_close_readiness_service(
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
            load_story=lambda: session.get(UserStory, story_id),
            load_sprint=lambda: session.get(Sprint, sprint_id),
            load_sprint_story=lambda story: _load_sprint_story_for_story(
                session=session,
                sprint_id=sprint_id,
                story=story,
            ),
            load_tasks=lambda: _load_story_tasks(session=session, story_id=story_id),
            task_progress=_story_task_progress,
        )
    except StoryCloseServiceError as exc:
        raise SprintPhaseError(exc.detail, status_code=exc.status_code) from exc
    data = cast("dict[str, Any]", _json_ready(payload))
    data["story_fingerprint"] = _story_fingerprint_for_ids(
        session=session,
        sprint_id=sprint_id,
        story_id=story_id,
    )
    data["guards"] = {
        "expected_status": data["current_status"],
        "expected_story_fingerprint": data["story_fingerprint"],
    }
    return data


def _sprint_close_readiness_payload(
    *,
    session: Session,
    project_id: int,
    sprint_id: int,
) -> dict[str, Any]:
    """Return Sprint close readiness plus guard values."""
    payload = get_sprint_close_readiness_service(
        sprint_id=sprint_id,
        load_sprint=lambda: _get_saved_sprint(session, project_id, sprint_id),
        build_readiness=lambda sprint: _build_sprint_close_readiness(
            list(sprint.stories)
        ),
        history_fidelity=_history_fidelity,
        load_close_snapshot=_load_sprint_close_snapshot,
    )
    data = cast("dict[str, Any]", _json_ready(payload))
    sprint_fingerprint = _sprint_fingerprint_for_ids(
        session=session,
        sprint_id=sprint_id,
    )
    data["sprint_fingerprint"] = sprint_fingerprint
    data["guards"] = {
        "expected_state": "SPRINT_VIEW",
        "expected_status": data["current_status"],
        "expected_sprint_fingerprint": sprint_fingerprint,
    }
    return data


def _build_sprint_close_readiness(stories: list[UserStory]) -> SprintCloseReadiness:
    """Return Sprint close readiness from persisted story/task state."""
    summaries: list[SprintCloseStorySummary] = []
    completed_story_count = 0
    unfinished_story_ids: list[int] = []

    for story in stories:
        total_tasks, done_tasks, cancelled_tasks, all_actionable_done = (
            _story_task_progress(story.tasks)
        )
        story_id = int(story.story_id) if story.story_id is not None else 0
        story_done = story.status in (StoryStatus.DONE, StoryStatus.ACCEPTED)
        tasks_done = total_tasks == 0 or all_actionable_done
        completion_state = "completed" if story_done and tasks_done else "unfinished"
        if completion_state == "completed":
            completed_story_count += 1
        elif story.story_id is not None:
            unfinished_story_ids.append(story_id)
        summaries.append(
            SprintCloseStorySummary(
                story_id=story_id,
                story_title=story.title,
                story_status=story.status.value,
                total_tasks=total_tasks,
                done_tasks=done_tasks,
                cancelled_tasks=cancelled_tasks,
                completion_state=completion_state,
            )
        )

    return SprintCloseReadiness(
        completed_story_count=completed_story_count,
        open_story_count=len(summaries) - completed_story_count,
        unfinished_story_ids=unfinished_story_ids,
        stories=summaries,
    )


def _history_fidelity(sprint: Sprint) -> str:
    """Return whether Sprint close history comes from snapshot or live rows."""
    return "snapshotted" if bool(sprint.close_snapshot_json) else "derived"


def _load_sprint_close_snapshot(sprint: Sprint) -> dict[str, Any] | None:
    """Return parsed Sprint close snapshot when one exists."""
    if not sprint.close_snapshot_json:
        return None
    try:
        return cast("dict[str, Any]", json.loads(sprint.close_snapshot_json))
    except (TypeError, ValueError):
        return None


def _close_sprint(
    *,
    session: Session,
    project_id: int,
    sprint_id: int,
    request: SprintCloseWriteRequest,
) -> dict[str, Any]:
    """Persist Sprint close using the domain service and serialize response."""

    def persist_closed_sprint(snapshot: dict[str, Any]) -> Sprint | None:
        sprint = _get_saved_sprint(session, project_id, sprint_id)
        if sprint is None:
            return None
        sprint.status = SprintStatus.COMPLETED
        sprint.completed_at = datetime.now(UTC)
        sprint.close_snapshot_json = json.dumps(snapshot)
        session.add(sprint)
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.SPRINT_COMPLETED,
                product_id=project_id,
                sprint_id=sprint_id,
                session_id=str(project_id),
                event_metadata=json.dumps(snapshot),
            )
        )
        session.commit()
        session.refresh(sprint)
        return sprint

    payload = close_sprint_service(
        sprint_id=sprint_id,
        completion_notes=request.completion_notes,
        follow_up_notes=request.follow_up_notes,
        load_sprint=lambda: _get_saved_sprint(session, project_id, sprint_id),
        build_readiness=lambda sprint: _build_sprint_close_readiness(
            list(sprint.stories)
        ),
        now_iso=_now_iso,
        persist_closed_sprint=persist_closed_sprint,
    )
    return cast("dict[str, Any]", _json_ready(payload))


def _close_story(
    *,
    session: Session,
    project_id: int,
    sprint_id: int,
    story_id: int,
    request: StoryCloseWriteRequest,
) -> dict[str, Any]:
    """Persist story close using the domain service and serialize response."""

    def persist_story_close(**kwargs: object) -> None:
        story = cast("UserStory", kwargs["story"])
        session.add(story)
        session.add(
            StoryCompletionLog(
                story_id=story_id,
                old_status=cast("StoryStatus", kwargs["old_status"]),
                new_status=StoryStatus.DONE,
                resolution=story.resolution,
                delivered=story.completion_notes,
                evidence=cast("str | None", kwargs["evidence_json"]),
                known_gaps=cast("str | None", kwargs["known_gaps"]),
                follow_ups_created=cast("str | None", kwargs["follow_up_notes"]),
                changed_by=cast("str", kwargs["changed_by"]),
                changed_at=datetime.now(UTC),
            )
        )
        session.commit()

    try:
        payload = close_story_service(
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
            resolution=request.resolution,
            completion_notes=request.completion_notes,
            evidence_links=request.evidence_links,
            known_gaps=request.known_gaps,
            follow_up_notes=request.follow_up_notes,
            changed_by=request.changed_by,
            now=lambda: datetime.now(UTC),
            load_story=lambda: session.get(UserStory, story_id),
            load_sprint=lambda: session.get(Sprint, sprint_id),
            load_sprint_story=lambda story: _load_sprint_story_for_story(
                session=session,
                sprint_id=sprint_id,
                story=story,
            ),
            load_tasks=lambda: _load_story_tasks(session=session, story_id=story_id),
            task_progress=_story_task_progress,
            persist_story_close=persist_story_close,
        )
    except StoryCloseServiceError as exc:
        raise SprintPhaseError(exc.detail, status_code=exc.status_code) from exc
    return cast("dict[str, Any]", _json_ready(payload))


def _load_sprint_story_for_story(
    *,
    session: Session,
    sprint_id: int,
    story: object,
) -> SprintStory | None:
    """Return the SprintStory join row for a story-like object."""
    story_obj = cast("UserStory", story)
    return session.exec(
        select(SprintStory).where(
            SprintStory.sprint_id == sprint_id,
            SprintStory.story_id == story_obj.story_id,
        )
    ).first()


def _load_story_tasks(*, session: Session, story_id: int) -> list[Task]:
    """Return tasks for one story in deterministic order."""
    return list(
        session.exec(
            select(Task)
            .where(Task.story_id == story_id)
            .order_by(cast("Any", Task.task_id).asc())
        ).all()
    )


def _story_task_progress(tasks: Sequence[object]) -> tuple[int, int, int, bool]:
    """Return actionable task progress using the API's checklist policy."""
    task_models = cast("Sequence[Task]", tasks)
    actionable_tasks = [
        task
        for task in task_models
        if bool(parse_task_metadata(task.metadata_json).checklist_items)
    ]
    total_tasks = len(actionable_tasks)
    done_tasks = sum(1 for task in actionable_tasks if task.status == TaskStatus.DONE)
    cancelled_tasks = sum(
        1 for task in actionable_tasks if task.status == TaskStatus.CANCELLED
    )
    all_actionable_done = (
        total_tasks > 0 and (done_tasks + cancelled_tasks) == total_tasks
    )
    return total_tasks, done_tasks, cancelled_tasks, all_actionable_done


def _story_fingerprint_for_ids(
    *,
    session: Session,
    sprint_id: int,
    story_id: int,
) -> str:
    """Return the guard fingerprint for one story in one Sprint."""
    story = session.get(UserStory, story_id)
    if story is None:
        _raise_story_not_found()
    tasks = _load_story_tasks(session=session, story_id=story_id)
    return _story_fingerprint(story=story, sprint_id=sprint_id, tasks=tasks)


def _story_fingerprint(
    *,
    story: UserStory,
    sprint_id: int,
    tasks: list[Task],
) -> str:
    """Return a deterministic close guard fingerprint for a story."""
    return canonical_hash(
        {
            "story_id": story.story_id,
            "sprint_id": sprint_id,
            "status": _enum_value(story.status),
            "tasks": [
                {
                    "task_id": task.task_id,
                    "status": _enum_value(task.status),
                }
                for task in sorted(tasks, key=lambda item: item.task_id or 0)
            ],
        }
    )


def _sprint_fingerprint_for_ids(
    *,
    session: Session,
    sprint_id: int,
) -> str:
    """Return the guard fingerprint for one Sprint close operation."""
    sprint = session.exec(
        _saved_sprint_query().where(Sprint.sprint_id == sprint_id)
    ).first()
    if sprint is None:
        _raise_sprint_not_found()
    return _sprint_fingerprint(sprint=sprint)


def _sprint_fingerprint(*, sprint: Sprint) -> str:
    """Return a deterministic Sprint close guard fingerprint."""
    stories = _sorted_sprint_stories(sprint)
    return canonical_hash(
        {
            "sprint_id": sprint.sprint_id,
            "status": _enum_value(sprint.status),
            "stories": [
                {
                    "story_id": story.story_id,
                    "status": _enum_value(story.status),
                    "tasks": [
                        {
                            "task_id": task.task_id,
                            "status": _enum_value(task.status),
                        }
                        for task in sorted(
                            story.tasks,
                            key=lambda item: item.task_id or 0,
                        )
                    ],
                }
                for story in stories
            ],
        }
    )


def _normalize_story_status(value: str) -> StoryStatus:
    """Parse a CLI story status value."""
    normalized = value.strip().replace("_", " ").replace("-", " ").lower()
    for status in StoryStatus:
        if status.value.lower() == normalized:
            return status
    message = f"Unsupported story status: {value}"
    raise ValueError(message)


def _normalize_story_resolution(value: str) -> StoryResolution:
    """Parse a CLI story close resolution value."""
    normalized = value.strip().lower()
    for resolution in StoryResolution:
        if resolution.value.lower() == normalized:
            return resolution
    message = f"Unsupported story resolution: {value}"
    raise ValueError(message)


def _assert_story_close_guards(
    *,
    current_status: str,
    expected_status: StoryStatus,
    current_story_fingerprint: str,
    expected_story_fingerprint: str,
    story_id: int,
) -> None:
    """Fail closed when a story close request is stale."""
    if current_status != expected_status.value:
        message = (
            f"Story {story_id} has status {current_status!r}, "
            f"expected {expected_status.value!r}."
        )
        raise _SprintStoryCloseError(
            message,
            details={
                "reason_code": "SPRINT_STORY_STATUS_STALE",
                "story_id": story_id,
                "current_status": current_status,
                "expected_status": expected_status.value,
            },
        )
    if current_story_fingerprint != expected_story_fingerprint:
        message = "SPRINT_STORY_FINGERPRINT_STALE: story fingerprint is stale."
        raise _SprintStoryCloseError(
            message,
            details={
                "reason_code": "SPRINT_STORY_FINGERPRINT_STALE",
                "story_id": story_id,
                "current_story_fingerprint": current_story_fingerprint,
                "expected_story_fingerprint": expected_story_fingerprint,
            },
        )


def _assert_sprint_close_guards(  # noqa: PLR0913
    *,
    current_state: str,
    expected_state: str,
    current_status: str,
    expected_status: str,
    current_sprint_fingerprint: str,
    expected_sprint_fingerprint: str,
    sprint_id: int,
) -> None:
    """Fail closed when a Sprint close request is stale."""
    if expected_state != "SPRINT_VIEW":
        message = "Sprint close expected_state must be SPRINT_VIEW"
        raise _SprintCloseError(
            message,
            details={
                "reason_code": "SPRINT_CLOSE_EXPECTED_STATE_INVALID",
                "expected_state": expected_state,
            },
        )
    if current_state != expected_state:
        message = (
            f"Sprint workflow state is {current_state!r}, expected {expected_state!r}."
        )
        raise _SprintCloseError(
            message,
            details={
                "reason_code": "SPRINT_CLOSE_STATE_STALE",
                "current_state": current_state,
                "expected_state": expected_state,
            },
        )
    if current_status != expected_status:
        message = (
            f"Sprint {sprint_id} has status {current_status!r}, "
            f"expected {expected_status!r}."
        )
        raise _SprintCloseError(
            message,
            details={
                "reason_code": "SPRINT_STATUS_STALE",
                "sprint_id": sprint_id,
                "current_status": current_status,
                "expected_status": expected_status,
            },
        )
    if current_sprint_fingerprint != expected_sprint_fingerprint:
        message = "SPRINT_FINGERPRINT_STALE: sprint fingerprint is stale."
        raise _SprintCloseError(
            message,
            details={
                "reason_code": "SPRINT_FINGERPRINT_STALE",
                "sprint_id": sprint_id,
                "current_sprint_fingerprint": current_sprint_fingerprint,
                "expected_sprint_fingerprint": expected_sprint_fingerprint,
            },
        )


def _topologically_sorted_sprint_stories(
    sprint: Sprint,
    *,
    active_edges: dict[int, set[int]],
) -> list[UserStory]:
    """Return Sprint stories ordered with in-sprint prerequisites first."""
    fallback_order = _sorted_sprint_stories(sprint)
    stories_by_id = {
        story.story_id: story for story in fallback_order if story.story_id is not None
    }
    story_ids = set(stories_by_id)
    fallback_index = {
        story_id: index for index, story_id in enumerate(stories_by_id, start=1)
    }
    indegree = dict.fromkeys(story_ids, 0)
    dependents_by_prerequisite: dict[int, set[int]] = {}
    for dependent_id in sorted(story_ids):
        for prerequisite_id in sorted(active_edges.get(dependent_id, set())):
            if prerequisite_id not in story_ids:
                continue
            indegree[dependent_id] += 1
            dependents_by_prerequisite.setdefault(prerequisite_id, set()).add(
                dependent_id
            )

    ready = sorted(
        [story_id for story_id, count in indegree.items() if count == 0],
        key=lambda story_id: fallback_index[story_id],
    )
    ordered_ids: list[int] = []
    while ready:
        story_id = ready.pop(0)
        ordered_ids.append(story_id)
        for dependent_id in sorted(
            dependents_by_prerequisite.get(story_id, set()),
            key=lambda item: fallback_index[item],
        ):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)
                ready.sort(key=lambda item: fallback_index[item])

    if len(ordered_ids) != len(story_ids):
        return fallback_order
    return [stories_by_id[story_id] for story_id in ordered_ids]


def _story_statuses(
    session: Session,
    *,
    sprint: Sprint,
    active_edges: dict[int, set[int]],
) -> dict[int, StoryStatus]:
    """Return statuses for current and dependency graph stories."""
    story_ids = {
        story.story_id for story in sprint.stories if story.story_id is not None
    }
    for dependent_id, prerequisite_ids in active_edges.items():
        story_ids.add(dependent_id)
        story_ids.update(prerequisite_ids)
    if not story_ids:
        return {}

    rows = session.exec(
        select(UserStory).where(cast("Any", UserStory.story_id).in_(story_ids))
    ).all()
    return {
        story.story_id: story.status for story in rows if story.story_id is not None
    }


def _downstream_edges(
    *,
    active_edges: dict[int, set[int]],
    current_story_ids: set[int],
) -> dict[int, set[int]]:
    """Return current-sprint dependents keyed by prerequisite story id."""
    downstream: dict[int, set[int]] = {}
    for dependent_id, prerequisite_ids in active_edges.items():
        if dependent_id not in current_story_ids:
            continue
        for prerequisite_id in prerequisite_ids:
            downstream.setdefault(prerequisite_id, set()).add(dependent_id)
    return downstream


def _story_dependency_metadata(
    stories: list[UserStory],
    *,
    context: _StoryDependencyMetadataContext,
) -> dict[int, dict[str, Any]]:
    """Return dependency metadata safe to attach to every task row."""
    metadata_by_story_id: dict[int, dict[str, Any]] = {}
    for story in stories:
        story_id = story.story_id
        if story_id is None:
            continue
        direct_blocked_by = sorted(context.active_edges.get(story_id, set()))
        closure = _dependency_closure(story_id, context.active_edges)
        blocked_by = sorted(
            prerequisite_id
            for prerequisite_id in closure
            if context.story_statuses.get(prerequisite_id) != StoryStatus.DONE
        )
        unblocks = sorted(
            context.downstream_edges.get(story_id, set()),
            key=lambda dependent_id: (
                context.execution_index_by_story_id.get(
                    dependent_id,
                    _DEPENDENCY_ORDER_FALLBACK_INDEX,
                ),
                dependent_id,
            ),
        )
        metadata_by_story_id[story_id] = {
            "story_execution_order": context.execution_index_by_story_id.get(story_id),
            "direct_blocked_by_story_ids": direct_blocked_by,
            "blocked_by_story_ids": blocked_by,
            "unblocks_story_ids": unblocks,
            "is_blocked": bool(blocked_by),
            "dependency_order_source": context.dependency_order_source,
        }
    return metadata_by_story_id


def _story_dependency_risk_metadata(
    stories: list[UserStory],
    *,
    active_edges: dict[int, set[int]],
    story_statuses: dict[int, StoryStatus],
) -> dict[int, dict[str, Any]]:
    """Return high-confidence missing dependency review hints."""
    metadata_by_story_id: dict[int, dict[str, Any]] = {}
    for dependent in stories:
        dependent_id = dependent.story_id
        if dependent_id is None or story_statuses.get(dependent_id) == StoryStatus.DONE:
            continue
        dependent_title = _normalize_dependency_text(str(dependent.title or ""))
        dependent_text = _dependency_risk_story_text(dependent)
        if not _looks_like_dependency_consumer(
            title=dependent_title,
            text=dependent_text,
        ):
            continue
        covered_prerequisite_ids = _dependency_closure(dependent_id, active_edges)
        candidates: list[dict[str, Any]] = []
        for prerequisite in stories:
            prerequisite_id = prerequisite.story_id
            if (
                prerequisite_id is None
                or prerequisite_id == dependent_id
                or prerequisite_id in covered_prerequisite_ids
                or story_statuses.get(prerequisite_id) == StoryStatus.DONE
                or not _candidate_can_be_missing_prerequisite(dependent, prerequisite)
            ):
                continue
            matched_terms = _matched_dependency_terms(
                dependent_text=dependent_text,
                prerequisite=prerequisite,
            )
            if len(matched_terms) < _DEPENDENCY_RISK_MIN_MATCHED_TERMS:
                continue
            candidates.append(
                {
                    "story_id": prerequisite_id,
                    "title": prerequisite.title,
                    "matched_terms": matched_terms,
                }
            )
        if not candidates:
            continue
        candidates.sort(key=lambda item: (str(item["title"]), int(item["story_id"])))
        metadata_by_story_id[dependent_id] = {
            "dependency_review_required": True,
            "dependency_review_reason_code": "MISSING_SEMANTIC_DEPENDENCY_EDGE",
            "missing_dependency_story_ids": [
                int(candidate["story_id"]) for candidate in candidates
            ],
            "dependency_review_candidates": candidates,
        }
    return metadata_by_story_id


def _dependency_review_required_warning(
    risk_metadata: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Return a structured warning for missing semantic dependency edges."""
    story_ids = sorted(risk_metadata)
    pairs: list[dict[str, Any]] = []
    for dependent_story_id in story_ids:
        metadata = risk_metadata[dependent_story_id]
        pairs.extend(
            [
                {
                    "dependent_story_id": dependent_story_id,
                    "prerequisite_story_id": candidate["story_id"],
                    "matched_terms": candidate["matched_terms"],
                }
                for candidate in metadata.get("dependency_review_candidates", [])
            ]
        )
    return {
        "code": "SPRINT_TASK_DEPENDENCY_REVIEW_REQUIRED",
        "message": (
            "Some Sprint stories reference unfinished peer stories without active "
            "dependency edges."
        ),
        "details": {
            "story_ids": story_ids,
            "missing_dependency_pairs": pairs,
        },
    }


def _dependency_risk_story_text(story: UserStory) -> str:
    """Return normalized text used for dependency-risk matching."""
    return _normalize_dependency_text(
        " ".join(
            str(value or "")
            for value in (
                story.title,
                story.story_description,
                story.acceptance_criteria,
            )
        )
    )


def _normalize_dependency_text(value: str) -> str:
    """Normalize story text for conservative dependency-risk matching."""
    lowered = value.lower().replace("\u2011", "-").replace("\u2010", "-")
    return re.sub(r"\s+", " ", lowered).strip()


def _looks_like_dependency_consumer(*, title: str, text: str) -> bool:
    """Return true when a story appears to consume prerequisite work."""
    return any(
        marker in title for marker in _DEPENDENCY_RISK_INTEGRATION_MARKERS
    ) and any(marker in text for marker in _DEPENDENCY_RISK_CUE_MARKERS)


def _candidate_can_be_missing_prerequisite(
    dependent: UserStory,
    prerequisite: UserStory,
) -> bool:
    """Return true when rank order can support a missing semantic prerequisite."""
    dependent_rank = _story_rank_number(dependent)
    prerequisite_rank = _story_rank_number(prerequisite)
    if dependent_rank is None or prerequisite_rank is None:
        return True
    return prerequisite_rank > dependent_rank


def _story_rank_number(story: UserStory) -> int | None:
    """Return the numeric story rank when available."""
    match = re.search(r"\d+", str(story.rank or ""))
    if match is None:
        return None
    return int(match.group(0))


def _matched_dependency_terms(
    *,
    dependent_text: str,
    prerequisite: UserStory,
) -> list[str]:
    """Return prerequisite title terms present in dependent story text."""
    terms = _dependency_title_terms(str(prerequisite.title or ""))
    return sorted(term for term in terms if term in dependent_text)


def _dependency_title_terms(title: str) -> set[str]:
    """Return significant title terms for dependency-risk matching."""
    normalized = _normalize_dependency_text(title)
    raw_terms = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", normalized)
    return {
        term
        for term in raw_terms
        if (
            len(term) >= _DEPENDENCY_RISK_MIN_TERM_LENGTH
            and term not in _DEPENDENCY_RISK_STOPWORDS
        )
    }


def _dependency_closure(
    story_id: int,
    active_edges: dict[int, set[int]],
) -> set[int]:
    """Return all transitive prerequisites for one story, cycle-safe."""
    closure: set[int] = set()
    visiting: set[int] = set()

    def visit(current_id: int) -> None:
        visiting.add(current_id)
        for prerequisite_id in sorted(active_edges.get(current_id, set())):
            if prerequisite_id not in closure:
                closure.add(prerequisite_id)
            if prerequisite_id in visiting:
                continue
            visit(prerequisite_id)
        visiting.remove(current_id)

    visit(story_id)
    closure.discard(story_id)
    return closure


def _now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _int_or_none(value: object) -> int | None:
    """Return an integer for int-shaped values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal():
            return int(stripped)
    return None


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


def _data_envelope(
    data: dict[str, Any],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return application facade success envelope."""
    return {
        "ok": True,
        "data": _flatten_phase_payload(data),
        "warnings": warnings or [],
        "errors": [],
    }


def _json_ready(value: object) -> object:  # noqa: PLR0911
    """Return a JSON-serializable projection for nested service payloads."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return enum_value
    if isinstance(value, datetime):
        return _serialize_temporal(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    return value


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


def _ledger_error(result: LedgerLoadResult) -> dict[str, Any]:
    """Return a structured error for mutation ledger guard failures."""
    raw_code = str(result.error_code or ErrorCode.MUTATION_FAILED.value)
    try:
        code = ErrorCode(raw_code)
    except ValueError:
        code = ErrorCode.MUTATION_FAILED
    return _error_envelope(
        code,
        raw_code,
        details={
            "mutation_event_id": result.ledger.mutation_event_id,
            "status": result.ledger.status,
        },
        remediation=[
            (
                "Retry with the same command body, or use a new idempotency key "
                "for new work."
            )
        ],
    )


def _task_update_error(exc: Exception) -> dict[str, Any]:
    """Map task update validation failures onto mutation failure envelopes."""
    details = getattr(exc, "details", None)
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        str(exc),
        details=details if isinstance(details, dict) else {},
        remediation=[
            (
                "Run agileforge sprint task show --project-id <project_id> "
                "--task-id <task_id>."
            ),
            "Refresh the task ticket and retry with current guard values.",
        ],
    )


def _story_close_error(exc: Exception) -> dict[str, Any]:
    """Map story close validation failures onto mutation failure envelopes."""
    details = getattr(exc, "details", None)
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        str(exc),
        details=details if isinstance(details, dict) else {},
        remediation=[
            (
                "Run agileforge sprint story readiness --project-id <project_id> "
                "--story-id <story_id>."
            ),
            "Refresh the story close guard values and retry.",
        ],
    )


def _sprint_close_error(exc: Exception) -> dict[str, Any]:
    """Map Sprint close validation failures onto mutation failure envelopes."""
    details = getattr(exc, "details", None)
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        str(exc),
        details=details if isinstance(details, dict) else {},
        remediation=[
            "Run agileforge sprint close-readiness --project-id <project_id>.",
            "Refresh the Sprint close guard values and retry.",
        ],
    )


def _post_sprint_triage_validation_error(
    exc: PostSprintTriageValidationError,
) -> dict[str, Any]:
    """Map post-sprint triage validation failures onto registered errors."""
    details = dict(exc.details)
    try:
        code = ErrorCode(exc.code)
    except ValueError:
        code = ErrorCode.TRIAGE_IMPACT_FIELDS_INVALID
        details.setdefault("validation_code", exc.code)
    return _error_envelope(
        code,
        exc.message,
        details=details,
        remediation=exc.remediation,
    )


def _post_sprint_triage_error(exc: _SprintTriageError) -> dict[str, Any]:
    """Map Sprint triage guard failures onto registered CLI errors."""
    return _error_envelope(
        exc.code,
        exc.detail,
        details=exc.details,
        remediation=exc.remediation,
    )


def _phase_error(exc: SprintPhaseError) -> dict[str, Any]:
    """Map Sprint phase errors onto registered CLI errors."""
    message = exc.detail
    code = (
        ErrorCode.AUTHORITY_NOT_ACCEPTED
        if message.startswith("Setup required")
        else ErrorCode.INVALID_COMMAND
    )
    return _error_envelope(
        code,
        message,
        details=exc.details,
        remediation=exc.remediation,
    )


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
