"""FastAPI application for AgenticFlow orchestration and workflow management.

Provides REST endpoints for project setup, vision generation,
backlog management,
roadmap planning, user story creation, and sprint execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    cast,
    runtime_checkable,
)
from uuid import uuid4

import anyio
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import desc
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlmodel import Session, select
from sqlmodel.sql._expression_select_cls import SelectOfScalar

from models.agent_workbench import CliMutationLedger
from models.core import Product, Sprint, SprintStory, Task, UserStory
from models.db import ensure_business_db_ready, get_engine
from models.enums import (
    SprintStatus,
    StoryStatus,
    TaskAcceptanceResult,
    TaskStatus,
    WorkflowEventType,
)
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from models.specs import CompiledSpecAuthority
from orchestrator_agent.agent_tools.backlog_primer.tools import (
    save_backlog_tool,
)
from orchestrator_agent.agent_tools.product_vision_tool.tools import (
    save_vision_tool,
)
from orchestrator_agent.agent_tools.roadmap_builder.tools import (
    save_roadmap_tool,
)
from orchestrator_agent.agent_tools.sprint_planner_tool.tools import (
    save_sprint_plan_tool,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.tools import (
    save_stories_tool,
)
from orchestrator_agent.fsm.states import OrchestratorState
from repositories.product import ProductRepository
from repositories.story import StoryRepository
from routers.sprint import register_sprint_routes
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.authority_decision import (
    AuthorityAcceptRequest,
    AuthorityRejectRequest,
)
from services.agent_workbench.authority_projection import _load_authority_selection
from services.agent_workbench.authority_review import (
    _render_review_packet,
    build_authority_review_snapshot,
)
from services.agent_workbench.mutation_ledger import MutationStatus
from services.agent_workbench.post_sprint_triage import (
    current_triage_for_latest_sprint,
    post_sprint_triage_required,
)
from services.agent_workbench.project_setup_fingerprints import (
    setup_retry_context_fingerprint,
)
from services.backlog_runtime import run_backlog_agent_from_state
from services.interview_runtime import (
    append_attempt,
    append_feedback_entry,
    mark_feedback_absorbed,
    promote_reusable_draft,
    reset_subject_working_set,
    set_request_projection,
)
from services.packet_renderer import render_packet
from services.packets.packet_service import (
    PacketServiceError,
)
from services.packets.packet_service import (
    get_story_packet as get_story_packet_service,
)
from services.packets.packet_service import (
    get_task_packet as get_task_packet_service,
)
from services.phases.backlog_service import (
    BacklogPhaseError,
)
from services.phases.backlog_service import (
    generate_backlog_draft as generate_backlog_draft_service,
)
from services.phases.backlog_service import (
    get_backlog_history as get_backlog_history_service,
)
from services.phases.backlog_service import (
    save_backlog_draft as save_backlog_draft_service,
)
from services.phases.roadmap_service import (
    RoadmapPhaseError,
)
from services.phases.roadmap_service import (
    generate_roadmap_draft as generate_roadmap_draft_service,
)
from services.phases.roadmap_service import (
    get_roadmap_history as get_roadmap_history_service,
)
from services.phases.roadmap_service import (
    save_roadmap_draft as save_roadmap_draft_service,
)
from services.phases.sprint_metrics import build_sprint_metrics
from services.phases.sprint_service import (
    SprintPhaseError,
    append_sprint_execution_history,
)
from services.phases.sprint_service import (
    close_sprint as close_sprint_service,
)
from services.phases.sprint_service import (
    generate_sprint_plan as generate_sprint_plan_service,
)
from services.phases.sprint_service import (
    get_saved_sprint_detail as get_saved_sprint_detail_service,
)
from services.phases.sprint_service import (
    get_sprint_close_readiness as get_sprint_close_readiness_service,
)
from services.phases.sprint_service import (
    get_sprint_history as get_sprint_history_service,
)
from services.phases.sprint_service import (
    list_saved_sprints as list_saved_sprints_service,
)
from services.phases.sprint_service import (
    reset_sprint_planner as reset_sprint_planner_service,
)
from services.phases.sprint_service import (
    save_sprint_plan as save_sprint_plan_service,
)
from services.phases.sprint_service import (
    start_saved_sprint as start_saved_sprint_service,
)
from services.phases.story_service import (
    StoryPhaseError,
)
from services.phases.story_service import (
    complete_story_phase as complete_story_phase_service,
)
from services.phases.story_service import (
    delete_story_requirement as delete_story_requirement_service,
)
from services.phases.story_service import (
    generate_story_draft as generate_story_draft_service,
)
from services.phases.story_service import (
    get_story_history as get_story_history_service,
)
from services.phases.story_service import (
    get_story_pending as get_story_pending_service,
)
from services.phases.story_service import (
    merge_story_resolution as merge_story_resolution_service,
)
from services.phases.story_service import (
    retry_story_draft as retry_story_draft_service,
)
from services.phases.story_service import (
    save_story_draft as save_story_draft_service,
)
from services.phases.vision_service import (
    VisionPhaseError,
)
from services.phases.vision_service import (
    generate_vision_draft as generate_vision_draft_service,
)
from services.phases.vision_service import (
    get_vision_history as get_vision_history_service,
)
from services.phases.vision_service import (
    save_vision_draft as save_vision_draft_service,
)
from services.roadmap_runtime import run_roadmap_agent_from_state
from services.setup_service import (
    run_project_setup as run_project_setup_service,
)
from services.specs.compiler_service import (
    COMPILED_AUTHORITY_INVALID_ERROR_CODE,
    compiled_authority_schema_unsupported_details,
    compiled_authority_schema_unsupported_remediation,
    compiled_authority_unreadable_details,
    load_compiled_artifact,
)
from services.specs.lifecycle_service import link_spec_to_product
from services.specs.story_validation_service import (
    compute_story_input_hash,
)
from services.sprint_input import load_sprint_candidates
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
from services.story_runtime import (
    run_story_agent_from_state,
    run_story_agent_request,
)
from services.task_execution_service import (
    TaskExecutionServiceError,
)
from services.task_execution_service import (
    get_task_execution_history as get_task_execution_history_service,
)
from services.task_execution_service import (
    record_task_execution as record_task_execution_service,
)
from services.vision_runtime import run_vision_agent_from_state
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project
from utils.api_schemas import (
    SprintCloseReadiness,
    SprintCloseReadResponse,
    SprintCloseStorySummary,
    SprintCloseWriteRequest,
    StoryCloseReadResponse,
    StoryCloseWriteRequest,
    TaskExecutionReadResponse,
    TaskExecutionWriteRequest,
)
from utils.failure_artifacts import read_failure_artifact
from utils.logging_config import configure_logging
from utils.runtime_config import get_api_host, get_api_port, get_api_reload
from utils.spec_schemas import ValidationEvidence
from utils.task_metadata import (
    TaskMetadata,
    hash_task_metadata,
    parse_task_metadata,
)

if TYPE_CHECKING:
    from google.adk.tools import ToolContext
else:
    ToolContext = Any

configure_logging()
logger = logging.getLogger(__name__)

product_repo = ProductRepository()
workflow_service = WorkflowService()


@runtime_checkable
class _SupportsIsoFormat(Protocol):
    def isoformat(self) -> str: ...


def _queryable_attr(attr: object) -> QueryableAttribute[object]:
    return cast("QueryableAttribute[object]", attr)


class ProductIDError(ValueError):
    """Exception raised when a product ID is missing."""

    def __init__(self, message: str = "Product creation did not return an id") -> None:
        """Initialize the exception with a default or custom error message."""
        super().__init__(message)


def _require_product_id(product: Product) -> int:
    if product.product_id is None:
        raise ProductIDError()
    return product.product_id


def _raise_delete_project_failed() -> None:
    raise HTTPException(
        status_code=500,
        detail="Failed to delete project due to database error.",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize runtime services and perform startup migrations."""
    configure_logging()
    ensure_business_db_ready()
    migrated = workflow_service.migrate_legacy_setup_state()
    if migrated:
        logger.info(
            "Migrated %s legacy sessions from ROUTING_MODE to SETUP_REQUIRED",
            migrated,
        )
    yield


app = FastAPI(title="AgenticFlow API", lifespan=lifespan)

app.mount("/dashboard", StaticFiles(directory="frontend", html=True), name="frontend")


class CreateProjectRequest(BaseModel):
    """Request body for creating a new project."""

    name: str = Field(min_length=1)
    spec_file_path: str = Field(min_length=1)


class RetrySetupRequest(BaseModel):
    """Request body for retrying project setup after a failure."""

    spec_file_path: str = Field(min_length=1)


class AuthorityCompileApiRequest(BaseModel):
    """Request body for guarded authority compilation."""

    model_config = ConfigDict(extra="forbid")

    spec_version_id: int
    expected_spec_hash: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    expected_setup_status: str = Field(min_length=1)
    compiler_model: str | None = Field(default=None, min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class AuthorityFeedbackRecordApiRequest(BaseModel):
    """Request body for structured authority feedback recording."""

    model_config = ConfigDict(extra="forbid")

    pending_authority_id: int
    expected_authority_fingerprint: str = Field(min_length=1)
    feedback_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = Field(default="dashboard-ui", min_length=1)
    correlation_id: str | None = Field(default=None, min_length=1)


class AuthorityCurateApiRequest(BaseModel):
    """Request body for bounded authority curation."""

    model_config = ConfigDict(extra="forbid")

    spec_version_id: int | None = None
    source_authority_id: int | None = None
    expected_source_authority_fingerprint: str | None = Field(
        default=None,
        min_length=1,
    )
    feedback_attempt_id: str | None = Field(default=None, min_length=1)
    recovery_mutation_event_id: int | None = None
    expected_candidate_authority_id: int | None = None
    expected_candidate_authority_fingerprint: str | None = Field(
        default=None,
        min_length=1,
    )
    max_iterations: int = Field(default=2, ge=1, le=2)
    compiler_model: str | None = Field(default=None, min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = Field(default="dashboard-ui", min_length=1)
    correlation_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_curation_mode(self) -> AuthorityCurateApiRequest:
        recovery_fields = (
            self.recovery_mutation_event_id,
            self.expected_candidate_authority_id,
            self.expected_candidate_authority_fingerprint,
        )
        normal_fields = (
            self.spec_version_id,
            self.source_authority_id,
            self.expected_source_authority_fingerprint,
            self.feedback_attempt_id,
            self.compiler_model,
        )
        recovery_present = any(item is not None for item in recovery_fields)
        if recovery_present:
            if any(item is not None for item in normal_fields):
                message = (
                    "authority curate recovery cannot include normal curation inputs"
                )
                raise ValueError(message)
            if any(item is None for item in recovery_fields):
                message = (
                    "authority curate recovery requires mutation event, "
                    "candidate id, and candidate fingerprint"
                )
                raise ValueError(message)
            return self
        if any(item is None for item in normal_fields[:4]):
            message = (
                "normal authority curate requires spec version, source authority, "
                "source fingerprint, and feedback attempt"
            )
            raise ValueError(message)
        return self


class ScopeExtensionValidateApiRequest(BaseModel):
    """Request body for validating an amended project-scope spec."""

    model_config = ConfigDict(extra="forbid")

    spec_file: str = Field(min_length=1)
    base_spec_version_id: int | None = None


class ScopeExtensionStartApiRequest(BaseModel):
    """Request body for guarded project-scope extension start."""

    model_config = ConfigDict(extra="forbid")

    spec_file: str = Field(min_length=1)
    base_spec_version_id: int
    expected_state: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = Field(default="dashboard-agent", min_length=1)

    @field_validator("spec_file", "expected_state", "idempotency_key", "changed_by")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            message = "must not be blank"
            raise ValueError(message)
        return normalized


class IncompleteReviewOverrideApiRequest(BaseModel):
    """Candidate-scoped incomplete review override payload."""

    candidate_id: str = Field(min_length=1)
    finding_code: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class AuthorityDecisionApiRequest(BaseModel):
    """Dashboard authority accept request with review-token or explicit guards."""

    review_token: str | None = None
    pending_authority_id: int | None = None
    expected_authority_fingerprint: str | None = None
    expected_source_spec_hash: str | None = None
    expected_disk_spec_hash: str | None = None
    expected_resolved_spec_path: str | None = None
    expected_state: str | None = None
    expected_setup_status: str | None = None
    expected_content_included: bool | None = None
    expected_omission_assessment: str | None = None
    expected_coverage_summary_fingerprint: str | None = None
    allow_incomplete_review: bool = False
    incomplete_review_rationale: str | None = None
    incomplete_review_overrides: list[IncompleteReviewOverrideApiRequest] = Field(
        default_factory=list
    )


class AuthorityRejectApiRequest(AuthorityDecisionApiRequest):
    """Dashboard authority rejection request."""

    reason: str = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class VisionGenerateRequest(BaseModel):
    """Request body for generating product vision."""

    user_input: str | None = None


class BacklogGenerateRequest(BaseModel):
    """Request body for generating product backlog."""

    user_input: str | None = None


class BacklogSaveRequest(BaseModel):
    """Request body for guarded Backlog persistence."""

    attempt_id: str
    expected_artifact_fingerprint: str
    expected_state: str
    idempotency_key: str


class RoadmapGenerateRequest(BaseModel):
    """Request body for generating product roadmap."""

    user_input: str | None = None


class RoadmapSaveRequest(BaseModel):
    """Request body for guarded Roadmap persistence."""

    attempt_id: str
    expected_artifact_fingerprint: str
    expected_state: str
    idempotency_key: str


class StoryGenerateRequest(BaseModel):
    """Request body for generating user stories."""

    user_input: str | None = None
    force_feedback: bool = False


class StorySaveRequest(BaseModel):
    """Request body for guarded Story persistence."""

    attempt_id: str
    expected_artifact_fingerprint: str
    expected_state: str
    idempotency_key: str


class StoryCompleteRequest(BaseModel):
    """Request body for guarded Story completion."""

    expected_state: str
    idempotency_key: str
    scope: str | None = None
    scope_id: str | None = None
    parent_requirements: list[str] | None = None


class SprintGenerateRequest(BaseModel):
    """Request body for generating sprint plans."""

    model_config = {"extra": "forbid"}

    user_input: str | None = None
    max_story_points: int | None = Field(default=None, gt=0)
    include_task_decomposition: bool = True
    selected_story_ids: list[int] | None = None


class SprintSaveRequest(BaseModel):
    """Request body for saving sprint details after execution."""

    model_config = {"extra": "forbid"}

    team_name: str = Field(min_length=1)
    attempt_id: str
    expected_artifact_fingerprint: str
    expected_state: str
    idempotency_key: str

    @field_validator(
        "team_name",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    )
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            message = "must not be blank"
            raise ValueError(message)
        return normalized


WORKFLOW_STEPS: list[dict[str, Any]] = [
    {
        "id": "setup",
        "label": "Project Setup",
        "states": [OrchestratorState.SETUP_REQUIRED.value],
    },
    {
        "id": "vision",
        "label": "Vision",
        "states": [
            OrchestratorState.VISION_INTERVIEW.value,
            OrchestratorState.VISION_REVIEW.value,
            OrchestratorState.VISION_PERSISTENCE.value,
        ],
    },
    {
        "id": "backlog",
        "label": "Backlog",
        "states": [
            OrchestratorState.BACKLOG_INTERVIEW.value,
            OrchestratorState.BACKLOG_REVIEW.value,
            OrchestratorState.BACKLOG_PERSISTENCE.value,
        ],
    },
    {
        "id": "roadmap",
        "label": "Roadmap",
        "states": [
            OrchestratorState.ROADMAP_INTERVIEW.value,
            OrchestratorState.ROADMAP_REVIEW.value,
            OrchestratorState.ROADMAP_PERSISTENCE.value,
        ],
    },
    {
        "id": "story",
        "label": "Stories",
        "states": [
            OrchestratorState.STORY_INTERVIEW.value,
            OrchestratorState.STORY_REVIEW.value,
            OrchestratorState.STORY_PERSISTENCE.value,
        ],
    },
    {
        "id": "sprint",
        "label": "Sprint",
        "states": [
            OrchestratorState.SPRINT_SETUP.value,
            OrchestratorState.SPRINT_DRAFT.value,
            OrchestratorState.SPRINT_PERSISTENCE.value,
            OrchestratorState.SPRINT_VIEW.value,
            OrchestratorState.SPRINT_LIST.value,
            OrchestratorState.SPRINT_UPDATE_STORY.value,
            OrchestratorState.SPRINT_MODIFY.value,
            OrchestratorState.SPRINT_COMPLETE.value,
        ],
    },
]
VALID_FSM_STATES = {state.value for state in OrchestratorState}
FAILURE_META_FIELDS = (
    "failure_artifact_id",
    "failure_stage",
    "failure_summary",
    "raw_output_preview",
    "has_full_artifact",
)
AUTHORITY_EXPLICIT_GUARD_FIELDS: tuple[str, ...] = (
    "pending_authority_id",
    "expected_authority_fingerprint",
    "expected_source_spec_hash",
    "expected_disk_spec_hash",
    "expected_resolved_spec_path",
    "expected_state",
    "expected_setup_status",
    "expected_content_included",
    "expected_omission_assessment",
    "expected_coverage_summary_fingerprint",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _event_date_or_none(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _normalize_fsm_state(value: str | None) -> str:
    """Normalize state to canonical key, fallback to SETUP_REQUIRED."""
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in VALID_FSM_STATES:
            return normalized
    return OrchestratorState.SETUP_REQUIRED.value


def _normalize_shell_fsm_state(value: str | None) -> str:
    """Normalize shell-visible FSM state."""
    return _normalize_fsm_state(value)


def _coerce_int(value: object) -> int | None:
    """Return an integer when the value is an int-shaped scalar."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal():
            return int(stripped)
    return None


def _reconcile_completed_sprint_state(
    *,
    project_id: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Project stale active-sprint state as complete when persisted truth wins."""
    fsm_state = state.get("fsm_state")
    if fsm_state not in {
        OrchestratorState.SPRINT_PERSISTENCE.value,
        OrchestratorState.SPRINT_VIEW.value,
    }:
        return state

    active_sprint_id = _coerce_int(state.get("active_sprint_id"))
    if active_sprint_id is None:
        return state

    with Session(get_engine()) as session:
        sprint = session.get(Sprint, active_sprint_id)

    if (
        sprint is None
        or sprint.product_id != project_id
        or sprint.status != SprintStatus.COMPLETED
    ):
        return state

    reconciled = dict(state)
    completed_at = _serialize_utc_temporal(
        sprint.completed_at or sprint.updated_at or sprint.created_at
    )
    reconciled["fsm_state"] = OrchestratorState.SPRINT_COMPLETE.value
    reconciled["active_sprint_id"] = None
    reconciled["latest_completed_sprint_id"] = active_sprint_id
    reconciled["sprint_completed_at"] = completed_at
    reconciled["sprint_state_reconciled_reason"] = "active_sprint_completed"
    return reconciled


def _failure_meta(
    source: dict[str, Any] | None,
    *,
    fallback_summary: str | None = None,
) -> dict[str, Any]:
    payload = source or {}
    return {
        "failure_artifact_id": payload.get("failure_artifact_id"),
        "failure_stage": payload.get("failure_stage"),
        "failure_summary": payload.get("failure_summary") or fallback_summary,
        "raw_output_preview": payload.get("raw_output_preview"),
        "has_full_artifact": bool(payload.get("has_full_artifact", False)),
    }


def _workbench_application() -> AgentWorkbenchApplication:
    """Construct the application facade used by dashboard parity routes."""
    return AgentWorkbenchApplication()


def _dashboard_authority_error(
    *,
    code: str,
    message: str,
    missing: list[str] | None = None,
    status_code: int = 400,
) -> None:
    """Raise a dashboard-shaped authority decision validation error."""
    detail: dict[str, Any] = {
        "status": "error",
        "errors": [
            {
                "code": code,
                "message": message,
                "details": {"missing": missing or []},
            }
        ],
    }
    raise HTTPException(status_code=status_code, detail=detail)


def _dashboard_authority_response(result: dict[str, Any]) -> dict[str, Any]:
    """Convert an application envelope into the dashboard API envelope."""
    if result.get("ok"):
        return {
            "status": "success",
            "data": result.get("data", {}),
            "warnings": result.get("warnings", []),
        }

    raise HTTPException(
        status_code=400,
        detail={
            "status": "error",
            "data": result.get("data", {}),
            "errors": result.get("errors", []),
            "warnings": result.get("warnings", []),
        },
    )


def _raise_compiled_authority_schema_unsupported(
    *,
    project_id: int,
    spec_version_id: int | None,
    observed_schema_version: str | None,
) -> None:
    """Raise the dashboard conflict for unsupported compiled-authority artifacts."""
    raise HTTPException(
        status_code=409,
        detail={
            "status": "error",
            "errors": [
                {
                    "code": "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED",
                    "message": ("Compiled authority artifact schema is unsupported."),
                    "details": compiled_authority_schema_unsupported_details(
                        project_id=project_id,
                        spec_version_id=spec_version_id,
                        observed_schema_version=observed_schema_version,
                    ),
                    "remediation": (
                        compiled_authority_schema_unsupported_remediation(
                            project_id=project_id,
                            spec_version_id=spec_version_id,
                        )
                    ),
                }
            ],
        },
    )


def _phase_authority_spec_version_id(state: dict[str, Any]) -> int | None:
    """Return the active spec version id from workflow state when available."""
    raw_spec_version_id = state.get("latest_spec_version_id")
    if isinstance(raw_spec_version_id, bool):
        return None
    if isinstance(raw_spec_version_id, int):
        return raw_spec_version_id
    if isinstance(raw_spec_version_id, str) and raw_spec_version_id.isdecimal():
        return int(raw_spec_version_id)
    return None


def _raise_compiled_authority_unreadable(
    *,
    project_id: int,
    spec_version_id: int | None,
    load_result: object,
) -> None:
    """Raise the dashboard conflict for unreadable compiled-authority artifacts."""
    message = (
        getattr(load_result, "message", None)
        or "Compiled authority artifact is unreadable."
    )
    raise HTTPException(
        status_code=409,
        detail={
            "status": "error",
            "errors": [
                {
                    "code": COMPILED_AUTHORITY_INVALID_ERROR_CODE,
                    "message": message,
                    "details": compiled_authority_unreadable_details(
                        project_id=project_id,
                        spec_version_id=spec_version_id,
                        load_result=load_result,
                    ),
                    "remediation": compiled_authority_schema_unsupported_remediation(
                        project_id=project_id,
                        spec_version_id=spec_version_id,
                    ),
                }
            ],
        },
    )


def _raise_if_authority_json_unsupported(
    *,
    project_id: int,
    spec_version_id: int | None,
    authority_json: object,
) -> None:
    """Fail closed when a phase start source has unusable authority JSON."""
    if not isinstance(authority_json, str) or not authority_json:
        return
    load_result = load_compiled_artifact(
        SimpleNamespace(compiled_artifact_json=authority_json)
    )
    if load_result.unsupported:
        _raise_compiled_authority_schema_unsupported(
            project_id=project_id,
            spec_version_id=spec_version_id,
            observed_schema_version=load_result.observed_schema_version,
        )
    if not load_result.ok:
        _raise_compiled_authority_unreadable(
            project_id=project_id,
            spec_version_id=spec_version_id,
            load_result=load_result,
        )


async def _guard_phase_generation_authority(
    *,
    project_id: int,
    product: object,
    session_id: str,
) -> None:
    """Block phase generation when active authority is an unsupported artifact."""
    state = await _ensure_session(session_id)
    spec_version_id = _phase_authority_spec_version_id(state)
    _raise_if_authority_json_unsupported(
        project_id=project_id,
        spec_version_id=spec_version_id,
        authority_json=state.get("compiled_authority_cached"),
    )
    _raise_if_authority_json_unsupported(
        project_id=project_id,
        spec_version_id=spec_version_id,
        authority_json=getattr(product, "compiled_authority_json", None),
    )


def _extract_workbench_error(result: dict[str, Any]) -> str:
    """Extract a user-friendly error message from a failed result envelope."""
    errors = result.get("errors") or []
    if errors:
        err = errors[0]
        code = err.get("code") or ""
        msg = err.get("message") or ""
        rem = err.get("remediation") or ""
        if isinstance(rem, list):
            rem = " ".join(rem)
        parts = []
        if code:
            parts.append(f"[{code}]")
        if msg:
            parts.append(msg)
        if rem:
            parts.append(f"Remediation: {rem}")
        if parts:
            return " ".join(parts)
    return str(result.get("error") or "")


def _complete_authority_guard_set(req: AuthorityDecisionApiRequest) -> bool:
    """Return true when dashboard provided every explicit freshness guard."""
    return all(
        getattr(req, field_name) is not None
        for field_name in AUTHORITY_EXPLICIT_GUARD_FIELDS
    )


def _validate_dashboard_authority_guards(req: AuthorityDecisionApiRequest) -> None:
    """Reject missing or fingerprint-only dashboard authority guards."""
    if req.review_token:
        return

    if _complete_authority_guard_set(req):
        return

    missing = [
        field_name
        for field_name in AUTHORITY_EXPLICIT_GUARD_FIELDS
        if getattr(req, field_name) is None
    ]

    if (
        req.expected_authority_fingerprint is not None
        and len(missing) == len(AUTHORITY_EXPLICIT_GUARD_FIELDS) - 1
    ):
        _dashboard_authority_error(
            code="AUTHORITY_GUARD_INCOMPLETE",
            message="Authority fingerprint alone is not a complete dashboard guard.",
            missing=missing,
        )

    _dashboard_authority_error(
        code="AUTHORITY_GUARD_INCOMPLETE",
        message=(
            "Dashboard authority decisions require a review token or the complete "
            "explicit guard set."
        ),
        missing=missing,
    )


def _validate_dashboard_incomplete_review_override(
    req: AuthorityDecisionApiRequest,
) -> None:
    """Leave incomplete-review policy enforcement to the authority service."""


def _validate_dashboard_reject_idempotency(
    req: AuthorityRejectApiRequest,
) -> None:
    """Require caller-provided idempotency for dashboard rejection."""
    if isinstance(req.idempotency_key, str) and req.idempotency_key.strip():
        return
    _dashboard_authority_error(
        code="AUTHORITY_GUARD_INCOMPLETE",
        message="Dashboard authority reject requires an explicit idempotency key.",
        missing=["idempotency_key"],
    )


def _dashboard_changed_by() -> str:
    """Return the authenticated dashboard user when available."""
    return "dashboard-human"


def _authority_request_kwargs(
    *,
    project_id: int,
    req: AuthorityDecisionApiRequest,
) -> dict[str, Any]:
    """Shape dashboard authority decisions for the application service."""
    payload = req.model_dump()
    payload.update(
        {
            "project_id": project_id,
            "policy": "dashboard_manual",
            "actor_mode": "dashboard-human",
            "changed_by": _dashboard_changed_by(),
            "idempotency_key": payload.get("idempotency_key")
            or f"dashboard-authority:{uuid4()}",
        }
    )
    return payload


def _setup_blocker(product: object) -> str | None:
    if not product:
        return "Project not found."

    spec_file_path = (getattr(product, "spec_file_path", None) or "").strip()
    if not spec_file_path:
        return "Specification file path is required."

    if not Path(spec_file_path).exists():
        return "Specification file path does not exist on disk."

    compiled_authority = getattr(product, "compiled_authority_json", None)
    if not compiled_authority:
        return "Specification authority is missing. Run setup retry."

    return None


async def _ensure_session(session_id: str) -> dict[str, Any]:
    state = workflow_service.get_session_status(session_id) or {}
    if not state.get("fsm_state"):
        await workflow_service.initialize_session(session_id=session_id)
        state = workflow_service.get_session_status(session_id) or {}
    return state


def _build_tool_context(
    context: object,
) -> ToolContext:
    # API flows use a lightweight state container outside the ADK runtime.
    return cast("ToolContext", context)


async def _hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
    state = await _ensure_session(session_id)
    context = SimpleNamespace(state=dict(state), session_id=session_id)
    select_project(project_id, _build_tool_context(context))
    return context


def _save_session_state(session_id: str, state: dict[str, Any]) -> None:
    workflow_service.update_session_status(session_id, state)


def _serialize_sprint_task(task: Task) -> dict[str, Any]:
    meta = parse_task_metadata(task.metadata_json)
    return {
        "id": task.task_id,
        "description": task.description,
        "status": task.status.value if hasattr(task.status, "value") else task.status,
        "task_kind": meta.task_kind,
        "artifact_targets": meta.artifact_targets,
        "workstream_tags": meta.workstream_tags,
        "checklist_items": meta.checklist_items,
        "is_executable": bool(meta.checklist_items),
    }


def _build_story_task_plan(story: UserStory) -> list[dict[str, Any]]:
    return sorted(
        [_serialize_sprint_task(task) for task in story.tasks],
        key=lambda item: (item["description"].lower(), item["id"]),
    )


def _story_task_progress(tasks: Sequence[Task]) -> tuple[int, int, int, bool]:
    actionable_tasks = [
        task
        for task in tasks
        if bool(parse_task_metadata(task.metadata_json).checklist_items)
    ]
    total_tasks = len(actionable_tasks)
    done_tasks = sum(1 for task in actionable_tasks if task.status == TaskStatus.DONE)
    cancelled_tasks = sum(
        1 for task in actionable_tasks if task.status == TaskStatus.CANCELLED
    )
    all_actionable_tasks_done = (
        total_tasks > 0 and (done_tasks + cancelled_tasks) == total_tasks
    )
    return total_tasks, done_tasks, cancelled_tasks, all_actionable_tasks_done


def _build_sprint_close_readiness(
    stories: list[UserStory],
) -> SprintCloseReadiness:
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


def _serialize_sprint_story(story: UserStory) -> dict[str, Any]:
    tasks = sorted(
        [_serialize_sprint_task(task) for task in story.tasks],
        key=lambda t: t["description"].lower(),
    )
    return {
        "story_id": story.story_id,
        "story_title": story.title,
        "status": story.status.value
        if hasattr(story.status, "value")
        else story.status,
        "story_points": story.story_points,
        "persona": story.persona,
        "tasks": tasks,
    }


def _history_fidelity(sprint: Sprint) -> Literal["snapshotted", "derived"]:
    return "snapshotted" if bool(sprint.close_snapshot_json) else "derived"


def _load_sprint_close_snapshot(sprint: Sprint) -> dict[str, Any] | None:
    if not sprint.close_snapshot_json:
        return None
    try:
        return json.loads(sprint.close_snapshot_json)
    except (TypeError, ValueError):
        logger.warning(
            "Failed to parse sprint close snapshot for sprint %s",
            sprint.sprint_id,
        )
        return None


def _sprint_generation_candidate_blocker(
    candidate_summary: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the create-next blocker when no refined Sprint candidates exist."""
    if candidate_summary.get("success") is False:
        return None

    raw_count = candidate_summary.get("count")
    if isinstance(raw_count, int):
        candidate_count = raw_count
    elif isinstance(raw_count, str):
        try:
            candidate_count = int(raw_count)
        except ValueError:
            return None
    else:
        return None

    if candidate_count != 0:
        return None

    message = (
        "Sprint generation is blocked because no refined Story candidates are "
        "available."
    )
    return {
        "command": "agileforge sprint generate",
        "reason": "NO_REFINED_SPRINT_CANDIDATES",
        "message": message,
        "candidate_count": candidate_count,
        "excluded_counts": candidate_summary.get("excluded_counts", {}),
    }


def _load_sprint_generation_blocker(
    *,
    can_create_next_sprint: bool,
    latest_completed_sprint_id: int | None,
    current_triage: dict[str, Any] | None,
    triage_impact: object,
    load_candidate_summary: Callable[[], dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Load candidate availability only for post-sprint create-next decisions."""
    if (
        not can_create_next_sprint
        or latest_completed_sprint_id is None
        or current_triage is None
        or triage_impact != "none"
        or load_candidate_summary is None
    ):
        return None

    try:
        candidate_summary = load_candidate_summary()
    except Exception:
        logger.exception("Failed to load sprint candidates for runtime summary blocker")
        return None
    return _sprint_generation_candidate_blocker(candidate_summary)


def _scope_extension_runtime_projection(project_id: int) -> dict[str, Any] | None:
    """Return scope-extension workflow projection fields when available."""
    try:
        result = _workbench_application().workflow_next(project_id=project_id)
    except Exception:
        logger.exception("Failed to load workflow-next scope-extension projection")
        return None

    if not result.get("ok"):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status not in {
        "project_scope_extension_available",
        "project_scope_extension_blocked",
    }:
        return None

    default_primary_action = {
        "label": "Extend Project Scope",
        "command": (
            f"agileforge scope extension validate --project-id {project_id} "
            "--spec-file <amended_spec_file>"
        ),
        "status": status,
        "reason": (
            "The current execution scope is exhausted; validate an amended spec "
            "before generating new work."
        ),
    }

    def _normalize_action(action: object) -> dict[str, Any]:
        """Return the dashboard-normalized scope-extension action shape."""
        if not isinstance(action, dict):
            return dict(default_primary_action)
        action_data = cast("dict[str, Any]", action)
        return {
            **default_primary_action,
            **action_data,
            "label": action_data.get("label") or default_primary_action["label"],
            "command": action_data.get("command")
            or default_primary_action["command"],
        }

    raw_actions = data.get("next_actions", [])
    actions = (
        [_normalize_action(action) for action in raw_actions]
        if isinstance(raw_actions, list)
        else []
    )
    if not actions:
        actions = [dict(default_primary_action)]
    primary_action = actions[0]
    blocked_reasons = data.get("blocked_reasons")
    if not isinstance(blocked_reasons, list):
        blocked_reasons = data.get("blocked_commands", [])
    if not isinstance(blocked_reasons, list):
        blocked_reasons = []
    return {
        "status": status,
        "next_actions": actions,
        "blocked_reasons": blocked_reasons,
        "primary_action": primary_action,
        "workflow_next_status": status,
        "scope_extension_status": status,
        "scope_extension_available": status == "project_scope_extension_available",
        "scope_extension_actions": actions,
        "scope_extension_primary_action": primary_action,
        "scope_extension_blocked_reasons": blocked_reasons,
    }


def _apply_sprint_generation_runtime_blocker(
    summary: dict[str, Any],
    *,
    project_id: int | None,
    completed: Sequence[Sprint],
    sprint_generation_blocker: dict[str, Any] | None,
) -> None:
    """Add create-next blocker and scope-extension projection fields."""
    if sprint_generation_blocker is None:
        return

    if project_id is None and not completed:
        return
    command_project_id = (
        project_id if project_id is not None else completed[0].product_id
    )
    scope_extension_projection = _scope_extension_runtime_projection(
        command_project_id
    )
    summary.update(
        {
            "workflow_next_status": "post_sprint_sprint_candidates_unavailable",
            "create_next_sprint_blocked_reason": sprint_generation_blocker["reason"],
            "create_next_sprint_valid_commands": [
                f"agileforge story pending --project-id {command_project_id}",
                f"agileforge sprint candidates --project-id {command_project_id}",
            ],
            "create_next_sprint_blocked_command": sprint_generation_blocker,
        }
    )
    if scope_extension_projection is not None:
        summary.update(scope_extension_projection)


def _build_sprint_runtime_summary(
    sprints: Sequence[Sprint],
    *,
    project_id: int | None = None,
    workflow_state: dict[str, Any] | None = None,
    load_candidate_summary: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active = next(
        (sprint for sprint in sprints if sprint.status == SprintStatus.ACTIVE),
        None,
    )
    planned = next(
        (sprint for sprint in sprints if sprint.status == SprintStatus.PLANNED),
        None,
    )
    completed = sorted(
        [sprint for sprint in sprints if sprint.status == SprintStatus.COMPLETED],
        key=lambda sprint: (
            sprint.completed_at or sprint.updated_at or sprint.created_at
        ),
        reverse=True,
    )
    state = workflow_state or {}
    draft_assessment = state.get("sprint_plan_assessment")
    if not isinstance(draft_assessment, dict):
        draft_assessment = {}
    has_reviewable_draft = state.get(
        "fsm_state"
    ) == OrchestratorState.SPRINT_DRAFT.value and bool(
        draft_assessment.get("is_complete")
    )
    latest_completed_sprint_id = completed[0].sprint_id if completed else None
    triage_state = dict(state)
    triage_state["latest_completed_sprint_id"] = latest_completed_sprint_id
    current_triage = current_triage_for_latest_sprint(triage_state)
    triage_required = post_sprint_triage_required(triage_state)
    triage_impact = (
        current_triage.get("impact") if isinstance(current_triage, dict) else None
    )
    triage_blocks_next_sprint = current_triage is not None and triage_impact != "none"
    can_create_next_sprint = (
        planned is None
        and not has_reviewable_draft
        and not triage_required
        and not triage_blocks_next_sprint
    )
    sprint_generation_blocker = _load_sprint_generation_blocker(
        can_create_next_sprint=can_create_next_sprint,
        latest_completed_sprint_id=latest_completed_sprint_id,
        current_triage=current_triage,
        triage_impact=triage_impact,
        load_candidate_summary=load_candidate_summary,
    )
    if sprint_generation_blocker is not None:
        can_create_next_sprint = False

    disabled_reason = None
    if triage_required:
        disabled_reason = (
            "Post-sprint triage is required before creating the next sprint."
        )
    elif triage_blocks_next_sprint:
        impact_label = str(triage_impact or "follow-up")
        disabled_reason = (
            f"Post-sprint triage recorded {impact_label} impact. "
            "Reconcile it before creating the next sprint."
        )
    elif has_reviewable_draft:
        disabled_reason = (
            "A sprint draft is waiting for review. Save or refine it before "
            "creating another sprint."
        )
    elif planned is not None:
        disabled_reason = (
            "A planned sprint already exists. Modify it instead of creating another."
        )
    elif sprint_generation_blocker is not None:
        disabled_reason = sprint_generation_blocker["message"]

    summary = {
        "active_sprint_id": active.sprint_id if active else None,
        "planned_sprint_id": planned.sprint_id if planned else None,
        "latest_completed_sprint_id": latest_completed_sprint_id,
        "can_create_next_sprint": can_create_next_sprint,
        "create_next_sprint_disabled_reason": disabled_reason,
        "post_sprint_triage_required": triage_required,
        "post_sprint_triage": current_triage,
    }
    if has_reviewable_draft:
        summary.update(
            {
                "has_reviewable_sprint_draft": True,
                "sprint_draft_attempt_id": draft_assessment.get("attempt_id"),
                "sprint_draft_artifact_fingerprint": draft_assessment.get(
                    "artifact_fingerprint"
                ),
            }
        )
    _apply_sprint_generation_runtime_blocker(
        summary,
        project_id=project_id,
        completed=completed,
        sprint_generation_blocker=sprint_generation_blocker,
    )
    return summary


def _allowed_actions_for_sprint(
    sprint: Sprint,
    *,
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    is_planned = sprint.status == SprintStatus.PLANNED
    is_active = sprint.status == SprintStatus.ACTIVE
    can_start = bool(is_planned and runtime_summary.get("active_sprint_id") is None)
    can_close = bool(is_active)
    can_modify_planned = bool(is_planned)
    return {
        "can_start": can_start,
        "start_disabled_reason": (
            None
            if can_start
            else ("Only planned sprints without another active sprint can be started.")
        ),
        "can_close": can_close,
        "close_disabled_reason": (
            None if can_close else "Only active sprints can be closed."
        ),
        "can_modify_planned": can_modify_planned,
        "modify_disabled_reason": (
            None
            if can_modify_planned
            else "Only planned sprints can be edited in place."
        ),
    }


def _serialize_sprint_list_item(
    sprint: Sprint,
    *,
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    stories = sorted(
        sprint.stories,
        key=lambda story: (
            story.rank or "",
            story.story_id or 0,
        ),
    )
    return {
        "id": sprint.sprint_id,
        "goal": sprint.goal,
        "status": sprint.status.value,
        "created_at": _serialize_temporal(sprint.created_at),
        "updated_at": _serialize_temporal(sprint.updated_at),
        "started_at": _serialize_temporal(sprint.started_at),
        "completed_at": _serialize_temporal(sprint.completed_at),
        "start_date": _serialize_temporal(sprint.start_date),
        "end_date": _serialize_temporal(sprint.end_date),
        "team_id": sprint.team_id,
        "team_name": sprint.team.name if sprint.team else None,
        "story_count": len(stories),
        "history_fidelity": _history_fidelity(sprint),
        "allowed_actions": _allowed_actions_for_sprint(
            sprint,
            runtime_summary=runtime_summary,
        ),
    }


def _sprint_elapsed_seconds(sprint: Sprint) -> int | None:
    if sprint.started_at is None or sprint.completed_at is None:
        return None
    started_at = (
        sprint.started_at
        if sprint.started_at.tzinfo
        else sprint.started_at.replace(tzinfo=UTC)
    )
    completed_at = (
        sprint.completed_at
        if sprint.completed_at.tzinfo
        else sprint.completed_at.replace(tzinfo=UTC)
    )
    return int((completed_at - started_at).total_seconds())


def _serialize_sprint_execution_history_item(sprint: Sprint) -> dict[str, Any]:
    stories = list(sprint.stories)
    tasks = [task for story in stories for task in story.tasks]
    completed_stories = [
        story
        for story in stories
        if story.status in (StoryStatus.DONE, StoryStatus.ACCEPTED)
    ]
    return {
        "sprint_id": sprint.sprint_id,
        "goal": sprint.goal,
        "status": _enum_value(sprint.status),
        "started_at": _serialize_temporal(sprint.started_at),
        "completed_at": _serialize_temporal(sprint.completed_at),
        "start_date": _serialize_temporal(sprint.start_date),
        "end_date": _serialize_temporal(sprint.end_date),
        "story_count": len(stories),
        "completed_story_count": len(completed_stories),
        "task_count": len(tasks),
        "completed_task_count": sum(
            1 for task in tasks if task.status == TaskStatus.DONE
        ),
        "story_points_total": sum(story.story_points or 0 for story in stories),
        "story_points_done": sum(
            story.story_points or 0 for story in completed_stories
        ),
        "elapsed_seconds": _sprint_elapsed_seconds(sprint),
        "history_fidelity": _history_fidelity(sprint),
    }


def _serialize_sprint_metrics_row(
    sprint: Sprint,
    events: Sequence[WorkflowEvent],
) -> dict[str, Any]:
    stories = sorted(
        sprint.stories,
        key=lambda story: (
            story.rank or "",
            story.story_id or 0,
        ),
    )
    tasks = [task for story in stories for task in story.tasks]
    completed_stories = [
        story
        for story in stories
        if story.status in (StoryStatus.DONE, StoryStatus.ACCEPTED)
    ]
    duration_values = [
        event.duration_seconds for event in events if event.duration_seconds is not None
    ]
    turn_count_values = [
        event.turn_count for event in events if event.turn_count is not None
    ]
    turn_count = sum(turn_count_values) if turn_count_values else None

    return {
        "sprint_id": sprint.sprint_id,
        "goal": sprint.goal,
        "status": _enum_value(sprint.status),
        "started_at": _serialize_temporal(sprint.started_at),
        "completed_at": _serialize_temporal(sprint.completed_at),
        "start_date": _serialize_temporal(sprint.start_date),
        "end_date": _serialize_temporal(sprint.end_date),
        "story_count": len(stories),
        "completed_story_count": len(completed_stories),
        "task_count": len(tasks),
        "completed_task_count": sum(
            1 for task in tasks if task.status == TaskStatus.DONE
        ),
        "story_points_planned": sum(story.story_points or 0 for story in stories),
        "story_points_completed": sum(
            story.story_points or 0 for story in completed_stories
        ),
        "unestimated_completed_story_count": sum(
            1 for story in completed_stories if story.story_points is None
        ),
        "elapsed_seconds": _sprint_elapsed_seconds(sprint),
        "workflow_event_count": len(events),
        "workflow_event_duration_seconds": (
            sum(duration_values) if duration_values else None
        ),
        "turn_count": turn_count,
        "history_fidelity": _history_fidelity(sprint),
    }


def _serialize_sprint_detail(
    sprint: Sprint,
    *,
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    stories = sorted(
        sprint.stories,
        key=lambda story: (
            story.rank or "",
            story.story_id or 0,
        ),
    )
    payload = _serialize_sprint_list_item(sprint, runtime_summary=runtime_summary)
    payload["selected_stories"] = [_serialize_sprint_story(story) for story in stories]
    payload["close_snapshot"] = _load_sprint_close_snapshot(sprint)
    return payload


def _saved_sprint_query() -> SelectOfScalar[Sprint]:
    return select(Sprint).options(
        selectinload(_queryable_attr(Sprint.team)),
        selectinload(_queryable_attr(Sprint.stories)).selectinload(
            _queryable_attr(UserStory.tasks)
        ),
    )


def _get_saved_sprint(
    session: Session, project_id: int, sprint_id: int
) -> Sprint | None:
    return session.exec(
        _saved_sprint_query().where(
            Sprint.product_id == project_id,
            Sprint.sprint_id == sprint_id,
        )
    ).first()


def _serialize_temporal(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, _SupportsIsoFormat):
        return value.isoformat()
    return str(value)


def _enum_value(value: object) -> str | None:
    enum_value = getattr(value, "value", value)
    return str(enum_value) if enum_value is not None else None


def _serialize_utc_temporal(value: object) -> str | None:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _serialize_temporal(value)


def _hash_payload(payload: object) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _truncate_text(text: str, max_length: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3].rstrip()}..."


def _build_task_label(description: str) -> str:
    normalized = _truncate_text(description or "Task", 80)
    return normalized or "Task"


def _extract_vision_excerpt(vision: str | None) -> str | None:
    if not vision or not vision.strip():
        return None
    for paragraph in re.split(r"\n\s*\n", vision.strip()):
        normalized = " ".join(paragraph.split())
        if normalized:
            return _truncate_text(normalized, 500)
    return None


def _normalize_acceptance_criteria(text: str | None) -> list[str]:
    if not text or not text.strip():
        return []

    items: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        normalized = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", stripped).strip()
        if normalized:
            items.append(normalized)

    if items:
        return items

    collapsed = " ".join(text.split())
    return [collapsed] if collapsed else []


def _load_validation_evidence(
    raw_value: str | None,
) -> ValidationEvidence | None:
    if not raw_value:
        return None
    try:
        return ValidationEvidence.model_validate_json(raw_value)
    except Exception as exc:  # pragma: no cover - legacy malformed evidence
        logger.warning("Failed to parse validation evidence: %s", exc)
        return None


def _load_pinned_authority(
    session: Session,
    accepted_spec_version_id: int | None,
) -> CompiledSpecAuthority | None:
    if accepted_spec_version_id is None:
        return None
    return session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == accepted_spec_version_id
        )
    ).first()


def _build_packet_findings(
    evidence: ValidationEvidence | None,
) -> list[dict[str, str | None]]:
    if not evidence:
        return []

    findings: list[dict[str, str | None]] = [
        {
            "severity": "failure",
            "source": "validation_failure",
            "code": failure.rule,
            "message": failure.message,
            "invariant_id": None,
            "rule": failure.rule,
            "capability": None,
        }
        for failure in evidence.failures
    ]
    findings.extend(
        {
            "severity": "warning",
            "source": "validation_warning",
            "code": warning,
            "message": warning,
            "invariant_id": None,
            "rule": None,
            "capability": None,
        }
        for warning in evidence.warnings
    )
    findings.extend(
        {
            "severity": finding.severity,
            "source": "alignment_warning",
            "code": finding.code,
            "message": finding.message,
            "invariant_id": finding.invariant,
            "rule": None,
            "capability": finding.capability,
        }
        for finding in evidence.alignment_warnings
    )
    findings.extend(
        {
            "severity": finding.severity,
            "source": "alignment_failure",
            "code": finding.code,
            "message": finding.message,
            "invariant_id": finding.invariant,
            "rule": None,
            "capability": finding.capability,
        }
        for finding in evidence.alignment_failures
    )
    return findings


def _build_story_compliance_boundaries(
    authority: CompiledSpecAuthority | None,
    evidence: ValidationEvidence | None,
    *,
    project_id: int = 0,
) -> list[dict[str, Any]]:
    if not authority or not evidence:
        return []

    load_result = load_compiled_artifact(authority)
    if load_result.unsupported:
        _raise_compiled_authority_schema_unsupported(
            project_id=project_id,
            spec_version_id=authority.spec_version_id,
            observed_schema_version=load_result.observed_schema_version,
        )
    artifact = load_result.artifact if load_result.ok else None
    if artifact is None:
        return []

    referenced_ids = set()
    if hasattr(evidence, "finding_invariant_ids") and evidence.finding_invariant_ids:
        referenced_ids.update(evidence.finding_invariant_ids)

    if not referenced_ids:
        return []

    source_map: dict[str, Any] = {}
    for entry in artifact.source_map:
        source_map.setdefault(entry.invariant_id, entry)

    relevant: list[dict[str, Any]] = []
    for invariant in artifact.invariants:
        if invariant.id not in referenced_ids:
            continue

        source_entry = source_map.get(invariant.id)
        parameters = invariant.parameters.model_dump(mode="json")
        relevant.append(
            {
                "invariant_id": invariant.id,
                "type": invariant.type.value,
                "parameters": parameters,
                "source_excerpt": source_entry.excerpt if source_entry else None,
                "source_location": source_entry.location if source_entry else None,
            }
        )
    return relevant


def _build_task_hard_constraints(
    authority: CompiledSpecAuthority | None,
    *,
    project_id: int = 0,
    task_metadata: TaskMetadata,
) -> list[dict[str, Any]]:
    if not authority or not task_metadata.relevant_invariant_ids:
        return []

    load_result = load_compiled_artifact(authority)
    if load_result.unsupported:
        _raise_compiled_authority_schema_unsupported(
            project_id=project_id,
            spec_version_id=authority.spec_version_id,
            observed_schema_version=load_result.observed_schema_version,
        )
    artifact = load_result.artifact if load_result.ok else None
    if artifact is None:
        return []

    source_map: dict[str, Any] = {}
    for entry in artifact.source_map:
        source_map.setdefault(entry.invariant_id, entry)

    invariant_map = {invariant.id: invariant for invariant in artifact.invariants}
    constraints: list[dict[str, Any]] = []
    for invariant_id in task_metadata.relevant_invariant_ids:
        invariant = invariant_map.get(invariant_id)
        if invariant is None:
            logger.warning(
                "Ignoring unknown invariant id '%s' while building "
                "task packet hard constraints.",
                invariant_id,
            )
            continue
        source_entry = source_map.get(invariant.id)
        constraints.append(
            {
                "invariant_id": invariant.id,
                "type": invariant.type.value,
                "parameters": invariant.parameters.model_dump(mode="json"),
                "source_excerpt": source_entry.excerpt if source_entry else None,
                "source_location": source_entry.location if source_entry else None,
            }
        )
    return constraints


def _load_packet_story_context(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int | None = None,
    task_id: int | None = None,
) -> SimpleNamespace | None:
    task = None
    if task_id is not None:
        task = session.exec(
            select(Task)
            .options(
                selectinload(_queryable_attr(Task.assignee)),
                selectinload(_queryable_attr(Task.story)).selectinload(
                    _queryable_attr(UserStory.product)
                ),
                selectinload(_queryable_attr(Task.story)).selectinload(
                    _queryable_attr(UserStory.tasks)
                ),
            )
            .where(Task.task_id == task_id)
        ).first()
        if not task or not task.story or task.story.product_id != project_id:
            return None
        story = task.story
    else:
        story = session.exec(
            select(UserStory)
            .options(
                selectinload(_queryable_attr(UserStory.product)),
                selectinload(_queryable_attr(UserStory.tasks)),
            )
            .where(UserStory.story_id == story_id)
        ).first()
        if not story or story.product_id != project_id:
            return None

    sprint = session.exec(
        select(Sprint)
        .options(selectinload(_queryable_attr(Sprint.team)))
        .where(
            Sprint.product_id == project_id,
            Sprint.sprint_id == sprint_id,
        )
    ).first()
    if not sprint:
        return None

    sprint_story = session.exec(
        select(SprintStory).where(
            SprintStory.sprint_id == sprint_id,
            SprintStory.story_id == story.story_id,
        )
    ).first()
    if not sprint_story:
        return None

    product = story.product
    if not product or product.product_id != project_id:
        product = session.get(Product, project_id)
        if not product:
            return None

    evidence = _load_validation_evidence(story.validation_evidence)
    current_story_input_hash = compute_story_input_hash(story)
    validation_input_hash = evidence.input_hash if evidence else None
    input_hash_matches = (
        current_story_input_hash == validation_input_hash
        if validation_input_hash is not None
        else None
    )
    validation_freshness = (
        "missing" if evidence is None else "current" if input_hash_matches else "stale"
    )

    authority = _load_pinned_authority(session, story.accepted_spec_version_id)
    load_result = load_compiled_artifact(authority) if authority else None
    if load_result is not None and load_result.unsupported:
        _raise_compiled_authority_schema_unsupported(
            project_id=project_id,
            spec_version_id=story.accepted_spec_version_id,
            observed_schema_version=load_result.observed_schema_version,
        )
    compiled_artifact = (
        load_result.artifact if load_result is not None and load_result.ok else None
    )
    spec_binding_status = (
        "pinned" if story.accepted_spec_version_id is not None else "unpinned"
    )
    authority_status = "available" if compiled_artifact is not None else "missing"

    task_metadata = None
    if task is not None:
        task_metadata = parse_task_metadata(
            task.metadata_json,
            logger=logger,
            task_id=task.task_id,
        )

    return SimpleNamespace(
        task=task,
        task_metadata=task_metadata,
        story=story,
        sprint=sprint,
        sprint_story=sprint_story,
        product=product,
        evidence=evidence,
        current_story_input_hash=current_story_input_hash,
        validation_input_hash=validation_input_hash,
        input_hash_matches=input_hash_matches,
        validation_freshness=validation_freshness,
        authority=authority,
        spec_binding_status=spec_binding_status,
        authority_status=authority_status,
    )


def _build_story_packet(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
) -> dict[str, Any] | None:
    context = _load_packet_story_context(
        session,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
    )
    if not context:
        return None

    story = context.story
    sprint = context.sprint
    sprint_story = context.sprint_story
    product = context.product
    evidence = context.evidence
    task_plan_tasks = _build_story_task_plan(story)

    source_snapshot = {
        "product_id": project_id,
        "sprint_id": sprint_id,
        "story_id": story.story_id,
        "product_updated_at": _serialize_temporal(product.updated_at),
        "sprint_updated_at": _serialize_temporal(sprint.updated_at),
        "sprint_story_added_at": _serialize_temporal(sprint_story.added_at),
        "story_updated_at": _serialize_temporal(story.updated_at),
        "story_ac_updated_at": _serialize_temporal(story.ac_updated_at),
        "accepted_spec_version_id": story.accepted_spec_version_id,
        "validation_validated_at": _serialize_temporal(
            evidence.validated_at if evidence else None
        ),
        "validation_input_hash": context.validation_input_hash,
        "compiled_authority_compiled_at": _serialize_temporal(
            context.authority.compiled_at if context.authority else None
        ),
        "task_plan_hash": _hash_payload(task_plan_tasks),
    }

    packet_id_hash = hashlib.sha256(
        f"story_packet.v1:{sprint_id}:{story_id}".encode()
    ).hexdigest()[:16]

    return {
        "schema_version": "story_packet.v1",
        "metadata": {
            "packet_id": f"sp_{packet_id_hash}",
            "generated_at": _serialize_temporal(datetime.now(UTC)),
            "generator_version": "v1",
            "source_fingerprint": _hash_payload(source_snapshot),
        },
        "source_snapshot": source_snapshot,
        "story": {
            "story_id": story.story_id,
            "title": story.title,
            "persona": story.persona,
            "story_description": story.story_description,
            "status": story.status.value,
            "story_points": story.story_points,
            "rank": story.rank,
            "source_requirement": story.source_requirement,
        },
        "task_plan": {"tasks": task_plan_tasks},
        "context": {
            "sprint": {
                "sprint_id": sprint.sprint_id,
                "goal": sprint.goal,
                "status": sprint.status.value,
                "started_at": _serialize_temporal(sprint.started_at),
                "start_date": _serialize_temporal(sprint.start_date),
                "end_date": _serialize_temporal(sprint.end_date),
                "team_id": sprint.team_id,
                "team_name": sprint.team.name if sprint.team else None,
            },
            "product": {
                "product_id": product.product_id,
                "name": product.name,
                "vision_excerpt": _extract_vision_excerpt(product.vision),
            },
        },
        "constraints": {
            "story_acceptance_criteria_text": story.acceptance_criteria,
            "story_acceptance_criteria_items": _normalize_acceptance_criteria(
                story.acceptance_criteria
            ),
            "spec_binding": {
                "mode": "pinned_story_authority",
                "binding_status": context.spec_binding_status,
                "spec_version_id": story.accepted_spec_version_id,
                "authority_artifact_status": context.authority_status,
            },
            "validation": {
                "present": evidence is not None,
                "passed": evidence.passed if evidence else None,
                "freshness_status": context.validation_freshness,
                "validated_at": _serialize_temporal(
                    evidence.validated_at if evidence else None
                ),
                "validator_version": evidence.validator_version if evidence else None,
                "current_story_input_hash": context.current_story_input_hash,
                "validation_input_hash": context.validation_input_hash,
                "input_hash_matches": context.input_hash_matches,
                "rules_checked": list(evidence.rules_checked) if evidence else [],
            },
            "story_compliance_boundaries": _build_story_compliance_boundaries(
                context.authority,
                evidence,
                project_id=project_id,
            ),
            "findings": _build_packet_findings(evidence),
        },
    }


def _build_task_packet(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    task_id: int,
) -> dict[str, Any] | None:
    context = _load_packet_story_context(
        session,
        project_id=project_id,
        sprint_id=sprint_id,
        task_id=task_id,
    )
    if not context or context.task is None or context.task_metadata is None:
        return None

    task = context.task
    task_metadata = context.task_metadata
    story = context.story
    sprint = context.sprint
    sprint_story = context.sprint_story
    product = context.product
    evidence = context.evidence

    source_snapshot = {
        "product_id": project_id,
        "sprint_id": sprint_id,
        "story_id": story.story_id,
        "task_id": task_id,
        "product_updated_at": _serialize_temporal(product.updated_at),
        "sprint_updated_at": _serialize_temporal(sprint.updated_at),
        "sprint_story_added_at": _serialize_temporal(sprint_story.added_at),
        "story_updated_at": _serialize_temporal(story.updated_at),
        "story_ac_updated_at": _serialize_temporal(story.ac_updated_at),
        "task_updated_at": _serialize_temporal(task.updated_at),
        "task_metadata_hash": hash_task_metadata(task_metadata),
        "accepted_spec_version_id": story.accepted_spec_version_id,
        "validation_validated_at": _serialize_temporal(
            evidence.validated_at if evidence else None
        ),
        "validation_input_hash": context.validation_input_hash,
        "compiled_authority_compiled_at": _serialize_temporal(
            context.authority.compiled_at if context.authority else None
        ),
    }

    packet_id_hash = hashlib.sha256(
        f"task_packet.v2:{sprint_id}:{task_id}".encode()
    ).hexdigest()[:16]

    return {
        "schema_version": "task_packet.v2",
        "metadata": {
            "packet_id": f"tp_{packet_id_hash}",
            "generated_at": _serialize_temporal(datetime.now(UTC)),
            "generator_version": "v2",
            "source_fingerprint": _hash_payload(source_snapshot),
        },
        "source_snapshot": source_snapshot,
        "task": {
            "task_id": task.task_id,
            "label": _build_task_label(task.description),
            "description": task.description,
            "status": task.status.value,
            "assignee_member_id": task.assigned_to_member_id,
            "assignee_name": task.assignee.name if task.assignee else None,
            "task_kind": task_metadata.task_kind,
            "artifact_targets": list(task_metadata.artifact_targets),
            "workstream_tags": list(task_metadata.workstream_tags),
            "checklist_items": list(task_metadata.checklist_items),
            "is_executable": bool(task_metadata.checklist_items),
        },
        "context": {
            "story": {
                "story_id": story.story_id,
                "title": story.title,
                "persona": story.persona,
                "story_description": story.story_description,
                "status": story.status.value,
                "story_points": story.story_points,
                "rank": story.rank,
                "source_requirement": story.source_requirement,
            },
            "sprint": {
                "sprint_id": sprint.sprint_id,
                "goal": sprint.goal,
                "status": sprint.status.value,
                "started_at": _serialize_temporal(sprint.started_at),
                "start_date": _serialize_temporal(sprint.start_date),
                "end_date": _serialize_temporal(sprint.end_date),
                "team_id": sprint.team_id,
                "team_name": sprint.team.name if sprint.team else None,
            },
            "product": {
                "product_id": product.product_id,
                "name": product.name,
                "vision_excerpt": _extract_vision_excerpt(product.vision),
            },
        },
        "constraints": {
            "spec_binding": {
                "mode": "pinned_story_authority",
                "binding_status": context.spec_binding_status,
                "spec_version_id": story.accepted_spec_version_id,
                "authority_artifact_status": context.authority_status,
            },
            "validation": {
                "present": evidence is not None,
                "passed": evidence.passed if evidence else None,
                "freshness_status": context.validation_freshness,
                "validated_at": _serialize_temporal(
                    evidence.validated_at if evidence else None
                ),
                "validator_version": evidence.validator_version if evidence else None,
                "current_story_input_hash": context.current_story_input_hash,
                "validation_input_hash": context.validation_input_hash,
                "input_hash_matches": context.input_hash_matches,
                "rules_checked": list(evidence.rules_checked) if evidence else [],
            },
            "task_hard_constraints": _build_task_hard_constraints(
                context.authority,
                project_id=project_id,
                task_metadata=task_metadata,
            ),
            "story_compliance_boundaries": _build_story_compliance_boundaries(
                context.authority,
                evidence,
                project_id=project_id,
            ),
            "findings": _build_packet_findings(evidence),
        },
    }


async def _run_setup(
    session_id: str, project_id: int, spec_file_path: str
) -> dict[str, Any]:
    def _refresh_project_context(current_project_id: int, context: object) -> object:
        return select_project(current_project_id, cast("ToolContext", context))

    return await run_project_setup_service(
        session_id=session_id,
        project_id=project_id,
        spec_file_path=spec_file_path,
        hydrate_context=_hydrate_context,
        build_tool_context=_build_tool_context,
        link_spec_to_product=link_spec_to_product,
        refresh_project_context=_refresh_project_context,
        load_project=product_repo.get_by_id,
        setup_blocker=_setup_blocker,
        run_vision_agent=run_vision_agent_from_state,
        now_iso=_now_iso,
        save_session_state=_save_session_state,
    )


def _effective_project_state(
    project: object, raw_state: dict[str, Any]
) -> dict[str, Any]:
    state = dict(raw_state)
    setup_status = state.get("setup_status")
    if setup_status == "authority_pending_review":
        state["fsm_state"] = OrchestratorState.SETUP_REQUIRED.value
        state["setup_status"] = "authority_pending_review"
        state["setup_error"] = None
        state["setup_error_code"] = None
        state["setup_failure_summary"] = None
    elif setup_status == "authority_rejected":
        state["fsm_state"] = OrchestratorState.SETUP_REQUIRED.value
        state["setup_status"] = "authority_rejected"
        state["setup_error"] = state.get("setup_error") or (
            "Authority review was rejected. Update the specification or "
            "recompile authority before continuing."
        )
        state["setup_error_code"] = state.get("setup_error_code") or (
            "AUTHORITY_REJECTED"
        )
        state["setup_failure_summary"] = state["setup_error"]
    else:
        state = _effective_setup_or_ready_state(project, state)

    project_id = _coerce_int(getattr(project, "product_id", None))
    if project_id is not None:
        state = _reconcile_completed_sprint_state(project_id=project_id, state=state)

    spec_path = getattr(project, "spec_file_path", None)
    state.setdefault("setup_failure_artifact_id", None)
    state.setdefault("setup_failure_stage", None)
    state.setdefault("setup_failure_summary", state.get("setup_error"))
    state.setdefault("setup_raw_output_preview", None)
    state.setdefault("setup_has_full_artifact", False)
    if spec_path:
        state["setup_spec_file_path"] = spec_path

    return state


def _effective_setup_or_ready_state(
    project: object,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Apply existing setup failure/passed classification."""
    blocker = _setup_blocker(project)

    if blocker:
        state["fsm_state"] = OrchestratorState.SETUP_REQUIRED.value
        state["setup_status"] = "failed"
        existing_error = state.get("setup_error")
        state["setup_error"] = existing_error or blocker
    else:
        state["fsm_state"] = _normalize_shell_fsm_state(state.get("fsm_state"))
        state.setdefault("setup_status", "passed")
        state.setdefault("setup_error", None)

    return state


@app.get("/")
def root() -> RedirectResponse:
    """Redirect the application root to the dashboard UI."""
    return RedirectResponse(url="/dashboard")


@app.get("/api/dashboard/config")
def get_dashboard_config() -> dict[str, object]:
    """Return static dashboard workflow configuration for the frontend."""
    return {
        "status": "success",
        "data": {
            "workflow_steps": WORKFLOW_STEPS,
        },
    }


@app.get("/api/projects")
def get_projects() -> dict[str, object]:
    """Return a list of all projects."""
    try:
        products = product_repo.get_all()
        raw_states = workflow_service.get_session_statuses(
            [str(product.product_id) for product in products]
        )
        payload = []

        for product in products:
            session_id = str(product.product_id)
            raw_state = raw_states.get(session_id, {})
            effective_state = _effective_project_state(product, raw_state)

            payload.append(
                {
                    "id": product.product_id,
                    "name": product.name,
                    "summary": product.description or "No description provided",
                    "fsm_state": effective_state.get(
                        "fsm_state", OrchestratorState.SETUP_REQUIRED.value
                    ),
                    "setup_status": effective_state.get("setup_status", "failed"),
                    "setup_error": effective_state.get("setup_error"),
                    "setup_failure_artifact_id": effective_state.get(
                        "setup_failure_artifact_id"
                    ),
                    "setup_failure_stage": effective_state.get("setup_failure_stage"),
                    "setup_failure_summary": effective_state.get(
                        "setup_failure_summary"
                    ),
                    "setup_raw_output_preview": effective_state.get(
                        "setup_raw_output_preview"
                    ),
                    "setup_has_full_artifact": effective_state.get(
                        "setup_has_full_artifact", False
                    ),
                }
            )

    except Exception as exc:
        logger.exception("Error fetching projects")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    else:
        return {"status": "success", "data": payload}


@app.post("/api/projects")
async def create_project(
    req: CreateProjectRequest,
) -> dict[str, object]:
    """Create a new project and initialize its workflow session."""
    # Note: This automatically generated key is a human convenience layer
    # to prevent duplicate setups from rapid double-clicks on the UI form
    # (which is also protected by disabling the submit button). It does not
    # represent mathematically true browser-to-server network retry idempotency.
    idempotency_key = f"ui-create-{uuid4()}"
    try:
        result = _workbench_application().project_create(
            name=req.name,
            spec_file=req.spec_file_path,
            setup_mode="greenfield",
            idempotency_key=idempotency_key,
            changed_by="dashboard-ui",
        )
    except Exception as exc:
        logger.exception("Error creating project")
        raise HTTPException(status_code=500, detail="Failed to create project") from exc

    if not result.get("ok"):
        data = result.get("data") or {}
        project_id = data.get("project_id")
        errors = result.get("errors") or []
        warnings = result.get("warnings") or []
        err_msg = _extract_workbench_error(result) or "Setup failed"
        if project_id is not None:
            failed_state = {
                "fsm_state": "SETUP_REQUIRED",
                "setup_status": "failed",
                "setup_error": data.get("setup_error") or err_msg,
                "setup_failure_artifact_id": data.get("setup_failure_artifact_id"),
                "setup_failure_stage": (
                    data.get("setup_failure_stage")
                    or data.get("failure_artifact_stage")
                ),
                "setup_failure_summary": data.get("setup_failure_summary") or err_msg,
                "setup_raw_output_preview": data.get("raw_output_preview"),
                "setup_has_full_artifact": bool(data.get("has_full_artifact", False)),
                "setup_spec_file_path": req.spec_file_path,
                "errors": errors,
                "warnings": warnings,
            }
            _save_session_state(str(project_id), failed_state)

            return {
                "status": "success",
                "data": {
                    "id": project_id,
                    "name": data.get("name") or req.name,
                    "setup_status": "failed",
                    "setup_error": data.get("setup_error") or err_msg,
                    "fsm_state": data.get("fsm_state") or "SETUP_REQUIRED",
                    "vision_auto_run": {"attempted": False},
                    "failure_artifact_id": data.get("setup_failure_artifact_id"),
                    "failure_stage": (
                        data.get("setup_failure_stage")
                        or data.get("failure_artifact_stage")
                    ),
                    "failure_summary": data.get("setup_failure_summary") or err_msg,
                    "raw_output_preview": data.get("raw_output_preview"),
                    "has_full_artifact": bool(data.get("has_full_artifact", False)),
                    "errors": errors,
                    "warnings": warnings,
                },
                "errors": errors,
                "warnings": warnings,
            }
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": err_msg,
                "errors": errors,
                "warnings": warnings,
            },
        )
    data = result.get("data") or {}
    project_id = data.get("project_id")
    setup_status = data.get("setup_status") or "authority_compile_required"
    return {
        "status": "success",
        "data": {
            "id": project_id,
            "name": data.get("name") or req.name,
            "setup_status": setup_status,
            "setup_error": None,
            "fsm_state": data.get("fsm_state") or "SETUP_REQUIRED",
            "vision_auto_run": data.get("vision_auto_run"),
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
            "spec_hash": data.get("spec_hash"),
            "spec_version_id": data.get("spec_version_id"),
            "next_actions": data.get("next_actions") or [],
        },
    }


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: int) -> dict[str, object]:
    """Delete a project and all its associated data."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        # Delete volatile session state
        workflow_service.delete_session(str(project_id))
        # Cascade delete products and all artifacts
        success = product_repo.delete_project(project_id)
        if not success:
            _raise_delete_project_failed()
    except Exception as exc:
        logger.exception("Error deleting project %d", project_id)
        raise HTTPException(status_code=500, detail="Failed to delete project") from exc
    else:
        return {
            "status": "success",
            "data": {"message": f"Project {project_id} deleted."},
        }


@app.post("/api/projects/{project_id}/setup/retry")
async def retry_project_setup(
    project_id: int, req: RetrySetupRequest
) -> dict[str, object]:
    """Retry project setup after a failure using unified workbench flow."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    state = await _ensure_session(session_id)

    recovery_event_id = None
    with Session(get_engine()) as db_session:
        unresolved = db_session.exec(
            select(CliMutationLedger).where(
                CliMutationLedger.command == "agileforge project create",
                CliMutationLedger.project_id == project_id,
                CliMutationLedger.status == MutationStatus.RECOVERY_REQUIRED.value,
            )
        ).first()
        if unresolved is not None:
            recovery_event_id = unresolved.mutation_event_id
        else:
            unresolved_retry = db_session.exec(
                select(CliMutationLedger).where(
                    CliMutationLedger.command == "agileforge project setup retry",
                    CliMutationLedger.project_id == project_id,
                    CliMutationLedger.status == MutationStatus.RECOVERY_REQUIRED.value,
                )
            ).first()
            if unresolved_retry is not None:
                recovery_event_id = unresolved_retry.mutation_event_id

    expected_state = str(state.get("fsm_state") or "SETUP_REQUIRED")

    try:

        def _compute() -> str:
            resolved = Path(req.spec_file_path).expanduser().resolve()
            return setup_retry_context_fingerprint(
                project_id=project_id,
                resolved_spec_path=resolved,
                workflow_state=state,
            )

        expected_context_fingerprint = await cast("Any", anyio.to_thread).run_sync(
            _compute
        )
    except Exception:
        # If the spec file path is invalid/missing/unreadable, the fingerprint
        # cannot be computed. Fall back to a non-empty sentinel string. This avoids
        # Pydantic validation errors (since the field requires min_length=1) while
        # allowing the workbench service to safely validate the spec file path first
        # and return a structured error response.
        expected_context_fingerprint = "unavailable-before-spec-validation"

    # Note: This automatically generated key is a human convenience layer
    # to prevent duplicate setups from rapid double-clicks on the UI form
    # (which is also protected by disabling the submit button). It does not
    # represent mathematically true browser-to-server network retry idempotency.
    idempotency_key = f"ui-retry-{uuid4()}"

    result = _workbench_application().project_setup_retry(
        project_id=project_id,
        spec_file=req.spec_file_path,
        expected_state=expected_state,
        expected_context_fingerprint=expected_context_fingerprint,
        recovery_mutation_event_id=recovery_event_id,
        idempotency_key=idempotency_key,
        changed_by="dashboard-ui",
    )

    if not result.get("ok"):
        data = result.get("data") or {}
        errors = result.get("errors") or []
        warnings = result.get("warnings") or []
        err_msg = _extract_workbench_error(result) or "Setup retry failed"
        failed_state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "failed",
            "setup_error": data.get("setup_error") or err_msg,
            "setup_failure_artifact_id": data.get("setup_failure_artifact_id"),
            "setup_failure_stage": (
                data.get("setup_failure_stage") or data.get("failure_artifact_stage")
            ),
            "setup_failure_summary": data.get("setup_failure_summary") or err_msg,
            "setup_raw_output_preview": data.get("raw_output_preview"),
            "setup_has_full_artifact": bool(data.get("has_full_artifact", False)),
            "setup_spec_file_path": req.spec_file_path,
            "errors": errors,
            "warnings": warnings,
        }
        _save_session_state(str(project_id), failed_state)

        return {
            "status": "success",
            "data": {
                "id": project_id,
                "name": product.name,
                "setup_status": "failed",
                "setup_error": data.get("setup_error") or err_msg,
                "fsm_state": data.get("fsm_state") or "SETUP_REQUIRED",
                "vision_auto_run": {"attempted": False},
                "failure_artifact_id": data.get("setup_failure_artifact_id"),
                "failure_stage": (
                    data.get("setup_failure_stage")
                    or data.get("failure_artifact_stage")
                ),
                "failure_summary": data.get("setup_failure_summary") or err_msg,
                "raw_output_preview": data.get("raw_output_preview"),
                "has_full_artifact": bool(data.get("has_full_artifact", False)),
                "errors": errors,
                "warnings": warnings,
            },
            "errors": errors,
            "warnings": warnings,
        }

    data = result.get("data") or {}
    setup_status = data.get("setup_status") or "authority_pending_review"
    return {
        "status": "success",
        "data": {
            "id": project_id,
            "name": product.name,
            "setup_status": setup_status,
            "setup_error": None,
            "fsm_state": data.get("fsm_state") or "SETUP_REQUIRED",
            "vision_auto_run": data.get("vision_auto_run"),
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        },
    }


@app.get("/api/projects/{project_id}/state")
async def get_project_state(project_id: int) -> dict[str, object]:
    """Get the current state of a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    state = await _ensure_session(session_id)
    effective_state = _effective_project_state(product, state)

    _save_session_state(session_id, effective_state)

    return {"status": "success", "data": effective_state}


@app.get("/api/projects/{project_id}/authority/review")
async def get_project_authority_review(
    project_id: int,
    include_spec: str = "auto",
) -> dict[str, Any]:
    """Return the pending authority review packet for the dashboard."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    result = _workbench_application().authority_review(
        project_id=project_id,
        include_spec=include_spec,
        output_format="json",
    )

    if not result.get("ok"):
        errors = result.get("errors", [])
        is_not_pending = any(
            err.get("code") == "AUTHORITY_NOT_PENDING" for err in errors
        )
        if (
            is_not_pending
            and product.compiled_authority_json
            and product.spec_file_path
        ):
            with Session(get_engine()) as session:
                selection = _load_authority_selection(session, project_id=project_id)
                accepted_spec = selection.accepted_spec
                authority = selection.authority

                if accepted_spec is not None and authority is not None:
                    load_result = load_compiled_artifact(authority)
                    if load_result.unsupported:
                        _raise_compiled_authority_schema_unsupported(
                            project_id=project_id,
                            spec_version_id=getattr(
                                accepted_spec,
                                "spec_version_id",
                                None,
                            ),
                            observed_schema_version=(
                                load_result.observed_schema_version
                            ),
                        )
                    snapshot = build_authority_review_snapshot(
                        project_id=project_id,
                        product=product,
                        spec=accepted_spec,
                        authority=authority,
                        include_spec=include_spec,
                    )
                    if isinstance(snapshot, dict):
                        pass
                    else:
                        rendered = _render_review_packet(snapshot)
                        rendered["post_accept"] = True
                        rendered["project"]["setup_status"] = "complete"
                        return {
                            "status": "success",
                            "data": rendered,
                            "warnings": [],
                        }

    return _dashboard_authority_response(result)


@app.post("/api/projects/{project_id}/authority/compile")
async def compile_project_authority(
    project_id: int,
    req: AuthorityCompileApiRequest,
) -> dict[str, object]:
    """Compile pending authority for a created project/spec shell."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    result = _workbench_application().authority_compile(
        project_id=project_id,
        spec_version_id=req.spec_version_id,
        expected_spec_hash=req.expected_spec_hash,
        expected_state=req.expected_state,
        expected_setup_status=req.expected_setup_status,
        compiler_model=req.compiler_model,
        idempotency_key=req.idempotency_key,
        changed_by="dashboard-ui",
    )

    return _dashboard_authority_response(result)


@app.post("/api/projects/{project_id}/authority/feedback")
async def record_project_authority_feedback(
    project_id: int,
    req: AuthorityFeedbackRecordApiRequest,
) -> dict[str, Any]:
    """Record structured authority feedback through the application service."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    result = _workbench_application().authority_feedback_record(
        project_id=project_id,
        pending_authority_id=req.pending_authority_id,
        expected_authority_fingerprint=req.expected_authority_fingerprint,
        feedback_file=req.feedback_file,
        idempotency_key=req.idempotency_key,
        changed_by=req.changed_by,
        correlation_id=req.correlation_id,
    )
    return _dashboard_authority_response(result)


@app.post("/api/projects/{project_id}/authority/curate")
async def curate_project_authority(
    project_id: int,
    req: AuthorityCurateApiRequest,
) -> dict[str, Any]:
    """Run bounded authority curation through the application service."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    result = _workbench_application().authority_curate(
        project_id=project_id,
        spec_version_id=req.spec_version_id,
        source_authority_id=req.source_authority_id,
        expected_source_authority_fingerprint=(
            req.expected_source_authority_fingerprint
        ),
        feedback_attempt_id=req.feedback_attempt_id,
        recovery_mutation_event_id=req.recovery_mutation_event_id,
        expected_candidate_authority_id=req.expected_candidate_authority_id,
        expected_candidate_authority_fingerprint=(
            req.expected_candidate_authority_fingerprint
        ),
        max_iterations=req.max_iterations,
        compiler_model=req.compiler_model,
        idempotency_key=req.idempotency_key,
        changed_by=req.changed_by,
        correlation_id=req.correlation_id,
    )
    return _dashboard_authority_response(result)


@app.post("/api/projects/{project_id}/scope-extension/validate")
async def validate_project_scope_extension(
    project_id: int,
    req: ScopeExtensionValidateApiRequest,
) -> dict[str, Any]:
    """Validate an amended project-scope spec through the application facade."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    result = _workbench_application().scope_extension_validate(
        project_id=project_id,
        spec_file=req.spec_file,
        base_spec_version_id=req.base_spec_version_id,
    )
    return _dashboard_authority_response(result)


@app.post("/api/projects/{project_id}/scope-extension/start")
async def start_project_scope_extension(
    project_id: int,
    req: ScopeExtensionStartApiRequest,
) -> dict[str, Any]:
    """Start guarded project-scope extension through the application facade."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    result = _workbench_application().scope_extension_start(
        project_id=project_id,
        spec_file=req.spec_file,
        base_spec_version_id=req.base_spec_version_id,
        expected_state=req.expected_state,
        idempotency_key=req.idempotency_key,
        changed_by=req.changed_by,
    )
    return _dashboard_authority_response(result)


@app.post("/api/projects/{project_id}/authority/accept")
async def accept_project_authority(
    project_id: int,
    req: AuthorityDecisionApiRequest,
) -> dict[str, Any]:
    """Accept pending authority through the application service."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    _validate_dashboard_authority_guards(req)
    _validate_dashboard_incomplete_review_override(req)
    request = AuthorityAcceptRequest(
        **_authority_request_kwargs(project_id=project_id, req=req)
    )
    result = _workbench_application().authority_accept(request)
    return _dashboard_authority_response(result)


@app.post("/api/projects/{project_id}/authority/reject")
async def reject_project_authority(
    project_id: int,
    req: AuthorityRejectApiRequest,
) -> dict[str, Any]:
    """Reject pending authority through the application service."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    _validate_dashboard_authority_guards(req)
    _validate_dashboard_reject_idempotency(req)
    request = AuthorityRejectRequest(
        **_authority_request_kwargs(project_id=project_id, req=req)
    )
    result = _workbench_application().authority_reject(request)
    return _dashboard_authority_response(result)


@app.get("/api/projects/{project_id}/debug/failures/{artifact_id}")
async def get_project_failure_artifact(
    project_id: int, artifact_id: str
) -> dict[str, object]:
    """Get a failure artifact for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    artifact = read_failure_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Failure artifact not found")

    if artifact.get("project_id") != project_id:
        raise HTTPException(
            status_code=404, detail="Failure artifact not found for project"
        )

    return {"status": "success", "data": artifact}


@app.post("/api/projects/{project_id}/vision/generate")
async def generate_project_vision(
    project_id: int, req: VisionGenerateRequest
) -> dict[str, object]:
    """Generate product vision for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    blocker = _setup_blocker(product)
    session_id = str(project_id)
    await _guard_phase_generation_authority(
        project_id=project_id,
        product=product,
        session_id=session_id,
    )
    try:
        data = await generate_vision_draft_service(
            project_id=project_id,
            setup_blocker=blocker,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda state: _save_session_state(session_id, state),
            now_iso=_now_iso,
            run_vision_agent=run_vision_agent_from_state,
            user_input=req.user_input,
        )
    except VisionPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.get("/api/projects/{project_id}/vision/history")
async def get_project_vision_history(project_id: int) -> dict[str, Any]:
    """Get the history of vision generation attempts for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await get_vision_history_service(
            load_state=lambda: _ensure_session(session_id)
        )
    except VisionPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.post("/api/projects/{project_id}/vision/save")
async def save_project_vision(
    project_id: int,
) -> dict[str, Any]:
    """Save the current product vision for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    blocker = _setup_blocker(product)
    session_id = str(project_id)
    try:
        data = await save_vision_draft_service(
            project_id=project_id,
            project_name=product.name,
            setup_blocker=blocker,
            save_state=lambda state: _save_session_state(session_id, state),
            now_iso=_now_iso,
            hydrate_context=lambda: _hydrate_context(session_id, project_id),
            build_tool_context=_build_tool_context,
            save_vision_tool=save_vision_tool,
        )
    except VisionPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.post("/api/projects/{project_id}/backlog/generate")
async def generate_project_backlog(
    project_id: int, req: BacklogGenerateRequest
) -> dict[str, Any]:
    """Generate or refine the product backlog for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    await _guard_phase_generation_authority(
        project_id=project_id,
        product=product,
        session_id=session_id,
    )
    try:
        data = await generate_backlog_draft_service(
            project_id=project_id,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda state: _save_session_state(session_id, state),
            now_iso=_now_iso,
            run_backlog_agent=run_backlog_agent_from_state,
            user_input=req.user_input,
        )
    except BacklogPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.get("/api/projects/{project_id}/backlog/history")
async def get_project_backlog_history(project_id: int) -> dict[str, Any]:
    """Get the history of backlog generation attempts for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await get_backlog_history_service(
            load_state=lambda: _ensure_session(session_id)
        )
    except BacklogPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.post("/api/projects/{project_id}/backlog/save")
async def save_project_backlog(
    project_id: int,
    req: BacklogSaveRequest,
) -> dict[str, Any]:
    """Save the current product backlog for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await save_backlog_draft_service(
            project_id=project_id,
            project_name=product.name,
            attempt_id=req.attempt_id,
            expected_artifact_fingerprint=req.expected_artifact_fingerprint,
            expected_state=req.expected_state,
            idempotency_key=req.idempotency_key,
            save_state=lambda state: _save_session_state(session_id, state),
            now_iso=_now_iso,
            hydrate_context=lambda: _hydrate_context(session_id, project_id),
            build_tool_context=_build_tool_context,
            save_backlog_tool=save_backlog_tool,
        )
    except BacklogPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.post("/api/projects/{project_id}/roadmap/generate")
async def generate_project_roadmap(
    project_id: int, req: RoadmapGenerateRequest
) -> dict[str, Any]:
    """Generate or refine the product roadmap for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    await _guard_phase_generation_authority(
        project_id=project_id,
        product=product,
        session_id=session_id,
    )
    try:
        data = await generate_roadmap_draft_service(
            project_id=project_id,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda state: _save_session_state(session_id, state),
            now_iso=_now_iso,
            run_roadmap_agent=run_roadmap_agent_from_state,
            user_input=req.user_input,
        )
    except RoadmapPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.get("/api/projects/{project_id}/roadmap/history")
async def get_project_roadmap_history(project_id: int) -> dict[str, Any]:
    """Get the history of roadmap generation attempts for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await get_roadmap_history_service(
            load_state=lambda: _ensure_session(session_id)
        )
    except RoadmapPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


@app.post("/api/projects/{project_id}/roadmap/save")
async def save_project_roadmap(
    project_id: int,
    req: RoadmapSaveRequest,
) -> dict[str, Any]:
    """Save the current product roadmap for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await save_roadmap_draft_service(
            project_id=project_id,
            attempt_id=req.attempt_id,
            expected_artifact_fingerprint=req.expected_artifact_fingerprint,
            expected_state=req.expected_state,
            idempotency_key=req.idempotency_key,
            save_state=lambda state: _save_session_state(session_id, state),
            now_iso=_now_iso,
            hydrate_context=lambda: _hydrate_context(session_id, project_id),
            build_tool_context=_build_tool_context,
            save_roadmap_tool=save_roadmap_tool,
        )
    except RoadmapPhaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "data": data,
    }


# ===========================================================================
# STORY ENDPOINTS
# ===========================================================================


@app.get("/api/projects/{project_id}/story/pending")
async def get_project_story_pending(project_id: int) -> dict[str, Any]:
    """Get the list of requirements with pending story generation."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    data = await get_story_pending_service(
        load_state=lambda: _ensure_session(session_id),
    )

    return {
        "status": "success",
        "data": data,
    }


@app.post("/api/projects/{project_id}/story/generate")
async def generate_project_story(
    project_id: int, parent_requirement: str, req: StoryGenerateRequest
) -> dict[str, Any]:
    """Generate or refine a user story for a parent requirement."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    await _guard_phase_generation_authority(
        project_id=project_id,
        product=product,
        session_id=session_id,
    )
    try:
        result = await generate_story_draft_service(
            project_id=project_id,
            parent_requirement=parent_requirement,
            user_input=req.user_input,
            force_feedback=req.force_feedback,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda updated: _save_session_state(session_id, updated),
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
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        **result,
    }


@app.post("/api/projects/{project_id}/story/retry")
async def retry_project_story(
    project_id: int, parent_requirement: str
) -> dict[str, Any]:
    """Retry user story generation for a parent requirement."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        result = await retry_story_draft_service(
            project_id=project_id,
            parent_requirement=parent_requirement,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda updated: _save_session_state(session_id, updated),
            now_iso=_now_iso,
            run_story_agent_request=run_story_agent_request,
            append_attempt=append_attempt,
            promote_reusable_draft=promote_reusable_draft,
            mark_feedback_absorbed=mark_feedback_absorbed,
            failure_meta=_failure_meta,
        )
    except StoryPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        **result,
    }


@app.get("/api/projects/{project_id}/story/history")
async def get_project_story_history(
    project_id: int, parent_requirement: str
) -> dict[str, Any]:
    """Get the history of story generation attempts for a requirement."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await get_story_history_service(
            parent_requirement=parent_requirement,
            load_state=lambda: _ensure_session(session_id),
        )
    except StoryPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        **data,
    }


@app.post("/api/projects/{project_id}/story/save")
async def save_project_story(
    project_id: int, parent_requirement: str, req: StorySaveRequest
) -> dict[str, Any]:
    """Save the current user story draft for a specific requirement."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await save_story_draft_service(
            project_id=project_id,
            parent_requirement=parent_requirement,
            attempt_id=req.attempt_id,
            expected_artifact_fingerprint=req.expected_artifact_fingerprint,
            expected_state=req.expected_state,
            idempotency_key=req.idempotency_key,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda updated: _save_session_state(session_id, updated),
            hydrate_context=_hydrate_context,
            build_tool_context=_build_tool_context,
            save_stories_tool=save_stories_tool,
        )
    except StoryPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        **data,
    }


@app.post("/api/projects/{project_id}/story/merge")
async def merge_project_story(
    project_id: int, parent_requirement: str
) -> dict[str, Any]:
    """Merge a user story into another based on agent recommendations."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await merge_story_resolution_service(
            parent_requirement=parent_requirement,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda updated: _save_session_state(session_id, updated),
            now_iso=_now_iso,
        )
    except StoryPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        **data,
    }


@app.delete("/api/projects/{project_id}/story")
async def delete_project_story(
    project_id: int, parent_requirement: str
) -> dict[str, Any]:
    """Delete all generated stories for a specific requirement."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        with Session(get_engine()) as session:
            story_repo = StoryRepository(session)
            data = await delete_story_requirement_service(
                parent_requirement=parent_requirement,
                load_state=lambda: _ensure_session(session_id),
                save_state=lambda updated: _save_session_state(session_id, updated),
                now_iso=_now_iso,
                delete_requirement_stories=lambda normalized_requirement: (
                    story_repo.delete_by_requirement(
                        product_id=project_id,
                        normalized_requirement=normalized_requirement,
                    )
                ),
                reset_subject_working_set=reset_subject_working_set,
            )
    except StoryPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        **data,
    }


@app.post("/api/projects/{project_id}/story/complete_phase")
async def complete_story_phase(
    project_id: int, req: StoryCompleteRequest
) -> dict[str, Any]:
    """Complete the story phase and transition to sprint setup."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await complete_story_phase_service(
            expected_state=req.expected_state,
            idempotency_key=req.idempotency_key,
            scope=req.scope,
            scope_id=req.scope_id,
            parent_requirements=req.parent_requirements,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda updated: _save_session_state(session_id, updated),
            now_iso=_now_iso,
        )
    except StoryPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {"status": "success", "data": data}


# ===========================================================================
# SPRINT ENDPOINTS
# ===========================================================================
def _load_current_planned_sprint_id(project_id: int) -> int | None:
    with Session(get_engine()) as session:
        return session.exec(
            select(Sprint.sprint_id)
            .where(
                Sprint.product_id == project_id,
                Sprint.status == SprintStatus.PLANNED,
            )
            .order_by(
                desc(_queryable_attr(Sprint.updated_at)),
                desc(_queryable_attr(Sprint.created_at)),
                desc(_queryable_attr(Sprint.sprint_id)),
            )
        ).first()


async def get_project_sprint_candidates(project_id: int) -> dict[str, Any]:
    """Get the list of stories eligible for the next sprint."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    state = await _ensure_session(str(project_id))
    result = load_sprint_candidates(
        project_id,
        story_completion_scope=state.get("story_completion_scope"),
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=500,
            detail=result.get("message") or "Failed to load sprint candidates",
        )

    return {
        "status": "success",
        "data": {
            "items": result.get("stories", []),
            "count": result.get("count", 0),
            "excluded_counts": result.get("excluded_counts", {}),
            "readiness": result.get("readiness"),
            "warnings": result.get("warnings", []),
            "governance_spec_update_story_ids": result.get(
                "governance_spec_update_story_ids",
                [],
            ),
            "story_completion_scope": result.get("story_completion_scope"),
            "message": result.get("message"),
        },
    }


async def generate_project_sprint(
    project_id: int, req: SprintGenerateRequest
) -> dict[str, Any]:
    """Generate or refine a sprint plan for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    await _guard_phase_generation_authority(
        project_id=project_id,
        product=product,
        session_id=session_id,
    )
    capacity_points = req.max_story_points
    capacity_source = "user_override" if req.max_story_points is not None else None
    capacity_basis = (
        f"{req.max_story_points} points, manually specified by user."
        if req.max_story_points is not None
        else ""
    )
    if capacity_points is None:
        metrics_payload = await get_project_sprint_metrics(project_id)
        metrics_data = metrics_payload.get("data") or {}
        recommendation = metrics_data.get("recommendation") or {}
        capacity_points = recommendation.get("recommended_next_sprint_points")
        capacity_basis = recommendation.get("basis") or ""
        if capacity_points is not None:
            capacity_source = "project_metrics"
    if capacity_points is None or capacity_source is None:
        raise HTTPException(
            status_code=422,
            detail="max_story_points is required for Sprint capacity planning",
        )
    if not isinstance(capacity_points, int) or capacity_points <= 0:
        raise HTTPException(
            status_code=422,
            detail="Sprint capacity in story points must be positive",
        )
    try:
        data = await generate_sprint_plan_service(
            project_id=project_id,
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda state: _save_session_state(session_id, state),
            current_planned_sprint_id=_load_current_planned_sprint_id(project_id),
            now_iso=_now_iso,
            run_sprint_agent=run_sprint_agent_from_state,
            failure_meta_builder=lambda source, fallback_summary=None: _failure_meta(
                cast("dict[str, Any] | None", source),
                fallback_summary=fallback_summary,
            ),
            capacity_points=capacity_points,
            capacity_source=capacity_source,
            capacity_basis=capacity_basis,
            max_story_points=req.max_story_points,
            include_task_decomposition=req.include_task_decomposition,
            selected_story_ids=req.selected_story_ids,
            user_input=req.user_input,
        )
    except SprintPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        "data": data,
    }


async def get_project_sprint_history(project_id: int) -> dict[str, Any]:
    """Get Sprint planner attempts and execution history for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    data = await get_sprint_history_service(
        load_state=lambda: _ensure_session(session_id),
        save_state=lambda state: _save_session_state(session_id, state),
        current_planned_sprint_id=_load_current_planned_sprint_id(project_id),
    )
    with Session(get_engine()) as session:
        execution_sprints = session.exec(
            _saved_sprint_query()
            .where(Sprint.product_id == project_id)
            .order_by(desc(_queryable_attr(Sprint.created_at)))
        ).all()
        data = append_sprint_execution_history(
            data,
            execution_items=[
                _serialize_sprint_execution_history_item(sprint)
                for sprint in execution_sprints
            ],
        )

    return {
        "status": "success",
        "data": data,
    }


async def get_project_sprint_metrics(project_id: int) -> dict[str, Any]:
    """Get read-only Sprint metrics for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    with Session(get_engine()) as session:
        completed_sprints = session.exec(
            _saved_sprint_query().where(
                Sprint.product_id == project_id,
                Sprint.status == SprintStatus.COMPLETED,
            )
        ).all()
        sprint_ids = [
            sprint.sprint_id for sprint in completed_sprints if sprint.sprint_id
        ]
        events_by_sprint_id: dict[int, list[WorkflowEvent]] = {
            sprint_id: [] for sprint_id in sprint_ids
        }
        if sprint_ids:
            events = session.exec(
                select(WorkflowEvent).where(
                    cast("Any", WorkflowEvent.sprint_id).in_(sprint_ids)
                )
            ).all()
            for event in events:
                if event.sprint_id is not None:
                    events_by_sprint_id.setdefault(event.sprint_id, []).append(event)

        data = build_sprint_metrics(
            project_id=project_id,
            completed_sprints=[
                _serialize_sprint_metrics_row(
                    sprint,
                    events_by_sprint_id.get(cast("int", sprint.sprint_id), []),
                )
                for sprint in completed_sprints
            ],
        )

    return {
        "status": "success",
        "data": data,
    }


async def reset_project_sprint_planner(project_id: int) -> dict[str, Any]:
    """Reset the sprint planner working set for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(project_id)
    try:
        data = await reset_sprint_planner_service(
            load_state=lambda: _ensure_session(session_id),
            save_state=lambda state: _save_session_state(session_id, state),
            current_planned_sprint_id=_load_current_planned_sprint_id(project_id),
        )
    except SprintPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        "data": data,
    }


async def list_project_sprints(project_id: int) -> dict[str, Any]:
    """List all saved sprints for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    with Session(get_engine()) as session:
        workflow_state = workflow_service.get_session_status(str(project_id))
        payload = list_saved_sprints_service(
            load_sprints=lambda: session.exec(
                _saved_sprint_query()
                .where(Sprint.product_id == project_id)
                .order_by(desc(_queryable_attr(Sprint.created_at)))
            ).all(),
            build_runtime_summary=lambda sprints: _build_sprint_runtime_summary(
                sprints,
                project_id=project_id,
                workflow_state=workflow_state,
                load_candidate_summary=lambda: load_sprint_candidates(
                    project_id,
                    story_completion_scope=workflow_state.get("story_completion_scope"),
                ),
            ),
            serialize_sprint_list_item=lambda sprint, runtime_summary: (
                _serialize_sprint_list_item(
                    sprint,
                    runtime_summary=runtime_summary,
                )
            ),
        )

    return {
        "status": "success",
        "data": payload,
    }


async def get_project_sprint(project_id: int, sprint_id: int) -> dict[str, Any]:
    """Get detailed information for a specific sprint."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    with Session(get_engine()) as session:
        workflow_state = workflow_service.get_session_status(str(project_id))
        try:
            data = get_saved_sprint_detail_service(
                load_sprint=lambda: _get_saved_sprint(session, project_id, sprint_id),
                load_sprints=lambda: session.exec(
                    _saved_sprint_query()
                    .where(Sprint.product_id == project_id)
                    .order_by(desc(_queryable_attr(Sprint.created_at)))
                ).all(),
                build_runtime_summary=lambda sprints: _build_sprint_runtime_summary(
                    sprints,
                    project_id=project_id,
                    workflow_state=workflow_state,
                    load_candidate_summary=lambda: load_sprint_candidates(
                        project_id,
                        story_completion_scope=workflow_state.get(
                            "story_completion_scope"
                        ),
                    ),
                ),
                serialize_sprint_detail=lambda sprint, runtime_summary: (
                    _serialize_sprint_detail(
                        sprint,
                        runtime_summary=runtime_summary,
                    )
                ),
            )
        except SprintPhaseError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return {
            "status": "success",
            "data": data,
        }


def get_sprint_close(project_id: int, sprint_id: int) -> SprintCloseReadResponse:
    """Get readiness information for closing an active sprint."""
    with Session(get_engine()) as session:
        try:
            data = get_sprint_close_readiness_service(
                sprint_id=sprint_id,
                load_sprint=lambda: _get_saved_sprint(session, project_id, sprint_id),
                build_readiness=lambda sprint: _build_sprint_close_readiness(
                    list(sprint.stories)
                ),
                history_fidelity=_history_fidelity,
                load_close_snapshot=_load_sprint_close_snapshot,
            )
        except SprintPhaseError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return SprintCloseReadResponse(**data)


def post_sprint_close(
    project_id: int, sprint_id: int, req: SprintCloseWriteRequest
) -> SprintCloseReadResponse:
    """Close an active sprint and record the final snapshot."""
    with Session(get_engine()) as session:

        def _persist_closed_sprint(snapshot: dict[str, Any]) -> Sprint | None:
            sprint = _get_saved_sprint(session, project_id, sprint_id)
            if not sprint:
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

        try:
            data = close_sprint_service(
                sprint_id=sprint_id,
                completion_notes=req.completion_notes,
                follow_up_notes=req.follow_up_notes,
                load_sprint=lambda: _get_saved_sprint(session, project_id, sprint_id),
                build_readiness=lambda sprint: _build_sprint_close_readiness(
                    list(sprint.stories)
                ),
                now_iso=_now_iso,
                persist_closed_sprint=_persist_closed_sprint,
            )
        except SprintPhaseError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        if data.get("current_status") == SprintStatus.COMPLETED.value:
            _sync_completed_sprint_workflow_state(
                project_id=project_id,
                sprint_id=sprint_id,
            )

        return SprintCloseReadResponse(**data)


async def get_project_task_packet(
    project_id: int, sprint_id: int, task_id: int, flavor: str | None = None
) -> dict[str, Any]:
    """Get the execution context packet for a specific sprint task."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    with Session(get_engine()) as session:
        try:
            data = get_task_packet_service(
                load_packet=lambda: _build_task_packet(
                    session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    task_id=task_id,
                ),
                flavor=flavor,
                render_packet=render_packet,
            )
        except PacketServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return {
            "status": "success",
            "data": data,
        }


async def get_project_story_packet(
    project_id: int,
    sprint_id: int,
    story_id: int,
    flavor: str | None = None,
) -> dict[str, Any]:
    """Get the context packet for a specific user story in a sprint."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    with Session(get_engine()) as session:
        try:
            data = get_story_packet_service(
                load_packet=lambda: _build_story_packet(
                    session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    story_id=story_id,
                ),
                flavor=flavor,
                render_packet=render_packet,
            )
        except PacketServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return {
            "status": "success",
            "data": data,
        }


def get_task_execution(
    project_id: int, sprint_id: int, task_id: int
) -> TaskExecutionReadResponse:
    """Get the execution history for a specific sprint task."""
    with Session(get_engine()) as session:
        try:
            data = get_task_execution_history_service(
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
                load_task=lambda: session.get(Task, task_id),
                load_sprint=lambda: session.get(Sprint, sprint_id),
                load_sprint_story=lambda task: session.exec(
                    select(SprintStory).where(
                        SprintStory.sprint_id == sprint_id,
                        SprintStory.story_id == task.story_id,
                    )
                ).first(),
                load_logs=lambda: session.exec(
                    select(TaskExecutionLog)
                    .where(
                        TaskExecutionLog.task_id == task_id,
                        TaskExecutionLog.sprint_id == sprint_id,
                    )
                    .order_by(desc(_queryable_attr(TaskExecutionLog.changed_at)))
                ).all(),
            )
        except TaskExecutionServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return TaskExecutionReadResponse(**data)


def post_task_execution(
    project_id: int,
    sprint_id: int,
    task_id: int,
    req: TaskExecutionWriteRequest,
) -> TaskExecutionReadResponse:
    """Record a progress log entry for an active sprint task."""
    with Session(get_engine()) as session:

        def _persist_execution_log(  # noqa: PLR0913
            *,
            task: Task,
            old_status: TaskStatus,
            new_status: TaskStatus,
            outcome_summary: str | None,
            artifact_refs_json: str | None,
            notes: str | None,
            acceptance_result: TaskAcceptanceResult,
            changed_by: str,
        ) -> None:
            session.add(task)
            session.add(
                TaskExecutionLog(
                    task_id=task_id,
                    sprint_id=sprint_id,
                    old_status=old_status,
                    new_status=new_status,
                    outcome_summary=outcome_summary,
                    artifact_refs_json=artifact_refs_json,
                    notes=notes,
                    acceptance_result=acceptance_result,
                    changed_by=changed_by,
                )
            )
            session.commit()

        try:
            data = record_task_execution_service(
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
                new_status=req.new_status,
                outcome_summary=req.outcome_summary,
                artifact_refs=req.artifact_refs,
                notes=req.notes,
                acceptance_result=req.acceptance_result,
                changed_by=req.changed_by,
                load_task=lambda: session.get(Task, task_id),
                load_sprint=lambda: session.get(Sprint, sprint_id),
                load_sprint_story=lambda task: session.exec(
                    select(SprintStory).where(
                        SprintStory.sprint_id == sprint_id,
                        SprintStory.story_id == task.story_id,
                    )
                ).first(),
                load_logs=lambda: session.exec(
                    select(TaskExecutionLog)
                    .where(
                        TaskExecutionLog.task_id == task_id,
                        TaskExecutionLog.sprint_id == sprint_id,
                    )
                    .order_by(desc(_queryable_attr(TaskExecutionLog.changed_at)))
                ).all(),
                parse_task_metadata=parse_task_metadata,
                persist_execution_log=lambda **kwargs: _persist_execution_log(
                    task=cast("Task", kwargs["task"]),
                    old_status=kwargs["old_status"],
                    new_status=kwargs["new_status"],
                    outcome_summary=kwargs["outcome_summary"],
                    artifact_refs_json=kwargs["artifact_refs_json"],
                    notes=kwargs["notes"],
                    acceptance_result=kwargs["acceptance_result"],
                    changed_by=kwargs["changed_by"],
                ),
            )
        except TaskExecutionServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return TaskExecutionReadResponse(**data)


def get_story_close(
    project_id: int, sprint_id: int, story_id: int
) -> StoryCloseReadResponse:
    """Get readiness information for closing a user story in a sprint."""
    with Session(get_engine()) as session:

        def _task_progress(tasks: Sequence[object]) -> tuple[int, int, int, bool]:
            return _story_task_progress(cast("Sequence[Task]", tasks))

        try:
            data = get_story_close_readiness_service(
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
                load_story=lambda: session.get(UserStory, story_id),
                load_sprint=lambda: session.get(Sprint, sprint_id),
                load_sprint_story=lambda current_story: session.exec(
                    select(SprintStory).where(
                        SprintStory.sprint_id == sprint_id,
                        SprintStory.story_id == current_story.story_id,
                    )
                ).first(),
                load_tasks=lambda: session.exec(
                    select(Task).where(Task.story_id == story_id)
                ).all(),
                task_progress=_task_progress,
            )
        except StoryCloseServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return StoryCloseReadResponse(**data)


def post_story_close(
    project_id: int, sprint_id: int, story_id: int, req: StoryCloseWriteRequest
) -> StoryCloseReadResponse:
    """Close a user story and record the final resolution notes."""
    with Session(get_engine()) as session:

        def _task_progress(tasks: Sequence[object]) -> tuple[int, int, int, bool]:
            return _story_task_progress(cast("Sequence[Task]", tasks))

        def _persist_story_close(  # noqa: PLR0913
            *,
            story: object,
            old_status: StoryStatus,
            evidence_json: str | None,
            known_gaps: str | None,
            follow_up_notes: str | None,
            changed_by: str,
        ) -> None:
            story_model = cast("UserStory", story)
            session.add(story_model)
            session.add(
                StoryCompletionLog(
                    story_id=story_id,
                    old_status=old_status,
                    new_status=StoryStatus.DONE,
                    resolution=story_model.resolution,
                    delivered=story_model.completion_notes,
                    evidence=evidence_json,
                    known_gaps=known_gaps,
                    follow_ups_created=follow_up_notes,
                    changed_by=changed_by,
                    changed_at=datetime.now(UTC),
                )
            )
            session.commit()

        try:
            data = close_story_service(
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
                resolution=req.resolution,
                completion_notes=req.completion_notes,
                evidence_links=req.evidence_links,
                known_gaps=req.known_gaps,
                follow_up_notes=req.follow_up_notes,
                changed_by=req.changed_by,
                now=lambda: datetime.now(UTC),
                load_story=lambda: session.get(UserStory, story_id),
                load_sprint=lambda: session.get(Sprint, sprint_id),
                load_sprint_story=lambda current_story: session.exec(
                    select(SprintStory).where(
                        SprintStory.sprint_id == sprint_id,
                        SprintStory.story_id == current_story.story_id,
                    )
                ).first(),
                load_tasks=lambda: session.exec(
                    select(Task).where(Task.story_id == story_id)
                ).all(),
                task_progress=_task_progress,
                persist_story_close=lambda **kwargs: _persist_story_close(
                    story=kwargs["story"],
                    old_status=kwargs["old_status"],
                    evidence_json=kwargs["evidence_json"],
                    known_gaps=kwargs["known_gaps"],
                    follow_up_notes=kwargs["follow_up_notes"],
                    changed_by=kwargs["changed_by"],
                ),
            )
        except StoryCloseServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        return StoryCloseReadResponse(**data)


async def save_project_sprint(
    project_id: int, req: SprintSaveRequest
) -> dict[str, Any]:
    """Save the current sprint planning draft for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        data = await save_sprint_plan_service(
            project_id=project_id,
            load_state=lambda: _ensure_session(str(project_id)),
            save_state=lambda state: _save_session_state(str(project_id), state),
            current_planned_sprint_id=_load_current_planned_sprint_id(project_id),
            now_iso=_now_iso,
            hydrate_context=_hydrate_context,
            build_tool_context=_build_tool_context,
            save_plan_tool=save_sprint_plan_tool,
            team_name=req.team_name,
            attempt_id=req.attempt_id,
            expected_artifact_fingerprint=req.expected_artifact_fingerprint,
            expected_state=req.expected_state,
            idempotency_key=req.idempotency_key,
        )
    except SprintPhaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    return {
        "status": "success",
        "data": data,
    }


async def start_project_sprint(project_id: int, sprint_id: int) -> dict[str, Any]:
    """Start an active sprint for a project."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    with Session(get_engine()) as session:

        def _persist_started_sprint() -> Sprint | None:
            sprint = _get_saved_sprint(session, project_id, sprint_id)
            if not sprint:
                return None

            if sprint.started_at is None:
                sprint.started_at = datetime.now(UTC)
                sprint.status = SprintStatus.ACTIVE
                session.add(sprint)
                session.add(
                    WorkflowEvent(
                        event_type=WorkflowEventType.SPRINT_STARTED,
                        product_id=project_id,
                        sprint_id=sprint_id,
                        session_id=str(project_id),
                        event_metadata=json.dumps(
                            {
                                "team_id": sprint.team_id,
                                "planned_start_date": _event_date_or_none(
                                    sprint.start_date
                                ),
                                "planned_end_date": _event_date_or_none(
                                    sprint.end_date
                                ),
                            }
                        ),
                    )
                )
                session.commit()
                session.refresh(sprint)

            return _get_saved_sprint(session, project_id, sprint_id)

        try:
            data = start_saved_sprint_service(
                project_id=project_id,
                sprint_id=sprint_id,
                load_sprint=lambda: _get_saved_sprint(session, project_id, sprint_id),
                load_other_active=lambda: session.exec(
                    select(Sprint).where(
                        Sprint.product_id == project_id,
                        Sprint.status == SprintStatus.ACTIVE,
                        Sprint.sprint_id != sprint_id,
                    )
                ).first(),
                persist_started_sprint=_persist_started_sprint,
                build_runtime_summary=lambda: _build_sprint_runtime_summary(
                    session.exec(
                        _saved_sprint_query()
                        .where(Sprint.product_id == project_id)
                        .order_by(desc(_queryable_attr(Sprint.created_at)))
                    ).all()
                ),
                serialize_sprint=lambda sprint, runtime_summary: (
                    _serialize_sprint_detail(
                        sprint,
                        runtime_summary=runtime_summary,
                    )
                ),
            )
        except SprintPhaseError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        _sync_started_sprint_workflow_state(project_id=project_id, sprint_id=sprint_id)
        return {
            "status": "success",
            "data": data,
        }


def _sync_started_sprint_workflow_state(*, project_id: int, sprint_id: int) -> None:
    """Move the workbench session into Sprint execution after API start."""
    session_id = str(project_id)
    state = workflow_service.get_session_status(session_id) or {}
    now = _now_iso()
    state["fsm_state"] = OrchestratorState.SPRINT_VIEW.value
    state["fsm_state_entered_at"] = now
    state["active_sprint_id"] = sprint_id
    state["sprint_started_at"] = now
    workflow_service.update_session_status(session_id, state)


def _sync_completed_sprint_workflow_state(*, project_id: int, sprint_id: int) -> None:
    """Move the workbench session to Sprint complete after API close."""
    session_id = str(project_id)
    state = workflow_service.get_session_status(session_id) or {}
    now = _now_iso()
    state["fsm_state"] = OrchestratorState.SPRINT_COMPLETE.value
    state["fsm_state_entered_at"] = now
    state["active_sprint_id"] = None
    state["latest_completed_sprint_id"] = sprint_id
    state["sprint_completed_at"] = now
    workflow_service.update_session_status(session_id, state)


register_sprint_routes(
    app,
    get_project_sprint_candidates=get_project_sprint_candidates,
    generate_project_sprint=generate_project_sprint,
    get_project_sprint_history=get_project_sprint_history,
    get_project_sprint_metrics=get_project_sprint_metrics,
    reset_project_sprint_planner=reset_project_sprint_planner,
    list_project_sprints=list_project_sprints,
    get_project_sprint=get_project_sprint,
    save_project_sprint=save_project_sprint,
    start_project_sprint=start_project_sprint,
    get_sprint_close=get_sprint_close,
    post_sprint_close=post_sprint_close,
    get_project_task_packet=get_project_task_packet,
    get_project_story_packet=get_project_story_packet,
    get_task_execution=get_task_execution,
    post_task_execution=post_task_execution,
    get_story_close=get_story_close,
    post_story_close=post_story_close,
)


if __name__ == "__main__":
    host = get_api_host()
    port = get_api_port()
    reload_enabled = get_api_reload()
    logger.info("Starting AgenticFlow Dashboard on http://%s:%s", host, port)
    uvicorn.run("api:app", host=host, port=port, reload=reload_enabled)
