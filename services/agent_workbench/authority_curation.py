"""Authority feedback and curation mutation service."""

# ruff: noqa: SIM300

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.agent_workbench import CliMutationLedger
from models.authority_curation import AuthorityCurationAttempt, AuthorityFeedbackAttempt
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.envelope import (
    WorkbenchError,
    error_envelope,
    success_envelope,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    IDEMPOTENCY_KEY_REUSED,
    MUTATION_IN_PROGRESS,
    MUTATION_RECOVERY_REQUIRED,
    MUTATION_RESUME_CONFLICT,
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
)
from services.specs.authority_curation_diff import (
    AuthorityDiffValidationError,
    build_authority_diff,
)
from utils.adk_runner import (
    AgentInvocationError,
    extract_final_response_text,
    extract_partial_response_text,
    get_agent_model_info,
    parse_json_payload,
)
from utils.authority_curation_trace import (
    append_trace_event,
    summarize_trace,
    trace_artifact_id,
)
from utils.failure_artifacts import write_failure_artifact

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator, Mapping

    from google.adk.models.base_llm import BaseLlm
    from google.genai import types
    from sqlalchemy.engine import Engine

AUTHORITY_FEEDBACK_RECORD_COMMAND = "agileforge authority feedback record"
AUTHORITY_CURATE_COMMAND = "agileforge authority curate"
AUTHORITY_CURATION_LEASE_SECONDS = 600
AUTHORITY_CURATION_COMPILER_VERSION = "authority-curation.v1"
AUTHORITY_CURATION_PROMPT_HASH = canonical_hash(
    {
        "command": AUTHORITY_CURATE_COMMAND,
        "schema_version": "agileforge.authority_curation.v1",
    }
)
AUTHORITY_CURATION_STATE_INPUT = "authority_curation_input"
AUTHORITY_CURATION_STATE_SEMANTIC_FINDINGS = "authority_curation_semantic_findings"
AUTHORITY_CURATION_STATE_QUALITY_FINDINGS = "authority_curation_quality_findings"
AUTHORITY_CURATION_STATE_REPAIR_PLAN = "authority_curation_repair_plan"
AUTHORITY_CURATION_STATE_REPAIR_OUTPUT = "authority_curation_repair_output"
AUTHORITY_CURATION_STATE_GATE = "authority_curation_gate_decision"
AUTHORITY_CURATION_FAILURE_PHASE = "authority_curation"

_LEDGER_MUTATION_EVENT_ID: Any = CliMutationLedger.mutation_event_id
_LEDGER_STATUS: Any = CliMutationLedger.status
_LEDGER_LEASE_OWNER: Any = CliMutationLedger.lease_owner
_LEDGER_LEASE_EXPIRES_AT: Any = CliMutationLedger.lease_expires_at

FeedbackTargetKind = Literal[
    "invariant",
    "gap",
    "assumption",
    "quality_group",
    "source_item",
    "authority_candidate",
]
FeedbackIssueType = Literal[
    "overstrong_invariant",
    "understrong_invariant",
    "materially_wrong_invariant",
    "duplicate_invariant",
    "near_duplicate_invariant",
    "over_split_group",
    "brittle_wording",
    "missing_invariant",
    "invalid_gap",
    "invalid_assumption",
    "source_map_error",
    "coverage_gap",
]
FeedbackSeverity = Literal["blocking", "non_blocking"]
TargetIndex = dict[str, set[str]]


@dataclass(frozen=True)
class _AuthorityGuardResult:
    """Loaded authority plus any guard error envelope."""

    authority: CompiledSpecAuthority | None
    authority_fingerprint: str | None
    error: dict[str, Any] | None


class _StrictModel(BaseModel):
    """Base model for strict authority curation payloads."""

    model_config = ConfigDict(extra="forbid")


class AuthorityFeedbackItem(_StrictModel):
    """One structured feedback item targeted at authority content."""

    feedback_id: str = Field(min_length=1)
    target_kind: FeedbackTargetKind
    target_id: str | None = Field(default=None, min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    issue_type: FeedbackIssueType
    severity: FeedbackSeverity
    instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_concrete_target(self) -> AuthorityFeedbackItem:
        if self.target_id is None and self.source_item_id is None:
            msg = "target_id or source_item_id is required"
            raise ValueError(msg)
        return self


class AuthorityFeedbackFile(_StrictModel):
    """Canonical feedback file schema."""

    schema_version: Literal["agileforge.authority_feedback.v1"]
    authority_id: int
    feedback_items: list[AuthorityFeedbackItem] = Field(min_length=1)


class AuthorityFeedbackRecordRequest(_StrictModel):
    """CLI request for feedback recording."""

    project_id: int
    pending_authority_id: int
    expected_authority_fingerprint: str = Field(min_length=1)
    feedback_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"
    correlation_id: str | None = None


class AuthorityCurationRequest(_StrictModel):
    """Guarded request for authority curation."""

    project_id: int
    spec_version_id: int
    source_authority_id: int
    expected_source_authority_fingerprint: str = Field(min_length=1)
    feedback_attempt_id: str = Field(min_length=1)
    max_iterations: int = Field(default=2, ge=1, le=2)
    compiler_model: str | None = Field(default=None, min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"
    correlation_id: str | None = None


class AuthorityCurationWorkflowPort(Protocol):
    """Workflow state operations needed by curation."""

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current workflow state."""
        raise NotImplementedError

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge workflow state update."""
        raise NotImplementedError


class SyncAuthorityCurationWorkflowAdapter:
    """Synchronous adapter over the default workflow service."""

    def __init__(self) -> None:
        """Initialize the adapter lazily to avoid workflow import at module load."""
        from services.workflow import WorkflowService  # noqa: PLC0415

        self._workflow = WorkflowService()

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current workflow state."""
        return self._workflow.get_session_status(session_id)

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge workflow state update."""
        self._workflow.update_session_status(session_id, partial_update)


@dataclass(frozen=True)
class _ActiveMutation:
    """Owned mutation ledger lease for curation."""

    ledger: MutationLedgerRepository
    lease_owner: str
    mutation_event_id: int


@dataclass(frozen=True)
class _ValidatedCurationCandidate:
    """Host-validated authority curation candidate metadata."""

    candidate_authority_json: dict[str, Any]
    diff: dict[str, Any]
    quality_report: object
    candidate_lineage: object


@dataclass(frozen=True)
class _PublishedCurationCandidate:
    """New pending authority published from a validated curation candidate."""

    authority_id: int
    authority_fingerprint: str


@dataclass(frozen=True)
class _LoadedCurationInputs:
    """Source artifact and feedback inputs needed for curation validation."""

    source_authority_json: dict[str, Any]
    feedback_json: str


@dataclass(frozen=True)
class _CurationWorkflowFailure:
    """Host-visible failure metadata from the ADK curation workflow."""

    error_code: ErrorCode
    failure_stage: str
    failure_summary: str
    raw_output: str | None = None
    model_info: dict[str, Any] | None = None
    validation_errors: object | None = None
    extra: dict[str, object] | None = None


class AuthorityCurationRunner:
    """Run authority feedback and curation commands."""

    def __init__(
        self,
        *,
        engine: Engine,
        workflow: AuthorityCurationWorkflowPort | None = None,
    ) -> None:
        """Initialize the curation runner."""
        self._engine = engine
        self._workflow = workflow or SyncAuthorityCurationWorkflowAdapter()

    def curate(self, request: AuthorityCurationRequest) -> dict[str, Any]:
        """Run bounded authority curation behind the authority_curating mutex."""
        active_mutation = self._start_curation_mutation(request)
        if not isinstance(active_mutation, _ActiveMutation):
            return active_mutation
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="mutation_lease_acquired",
            status="completed",
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )

        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="guard_validation_started",
            status="started",
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )
        guard_error = self._validate_curation_guards(request)
        if guard_error is not None:
            guard_error = _decorate_response_trace_artifact(
                guard_error,
                mutation_event_id=active_mutation.mutation_event_id,
            )
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="guard_validation_failed",
                status="failed",
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
                error=_trace_error_from_response(
                    guard_error,
                    current_step="guard_validation_started",
                ),
            )
            self._finalize_mutation_status_with_trace(
                request=request,
                active_mutation=active_mutation,
                response=guard_error,
                status=MutationStatus.GUARD_REJECTED,
            )
            return guard_error
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="guard_validation_completed",
            status="completed",
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )

        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="curation_attempt_create_started",
            status="started",
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )
        attempt = self._create_running_curation_attempt(
            request=request,
            request_hash=_curation_request_hash(request),
            mutation_event_id=active_mutation.mutation_event_id,
        )
        if not isinstance(attempt, AuthorityCurationAttempt):
            attempt = _decorate_response_trace_artifact(
                attempt,
                mutation_event_id=active_mutation.mutation_event_id,
            )
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="curation_attempt_create_failed",
                status="failed",
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
                error=_trace_error_from_response(
                    attempt,
                    current_step="curation_attempt_create_started",
                ),
            )
            self._finalize_mutation_status_with_trace(
                request=request,
                active_mutation=active_mutation,
                response=attempt,
                status=MutationStatus.GUARD_REJECTED,
            )
            return attempt
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="curation_attempt_create_completed",
            status="completed",
            curation_attempt_id=attempt.curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )

        try:
            with _trace_step_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="workflow_curating_status_started",
                completed_step="workflow_curating_status_completed",
                failed_step="workflow_curating_status_failed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
            ):
                self._mark_workflow_curating(
                    request=request,
                    mutation_event_id=active_mutation.mutation_event_id,
                )
        except Exception as exc:  # noqa: BLE001
            response = error_envelope(
                command=AUTHORITY_CURATE_COMMAND,
                error=workbench_error(
                    ErrorCode.MUTATION_FAILED,
                    message="Authority curation workflow state update failed.",
                    details={
                        "project_id": request.project_id,
                        "curation_attempt_id": attempt.curation_attempt_id,
                        "exception_type": type(exc).__name__,
                        "trace_artifact_id": trace_artifact_id(
                            active_mutation.mutation_event_id
                        ),
                    },
                ),
                correlation_id=request.correlation_id,
            )
            self._finalize_failed_curation(
                request=request,
                active_mutation=active_mutation,
                attempt=attempt,
                response=response,
                status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
            )
            return response

        return self._run_curation_after_status_update(
            request=request,
            active_mutation=active_mutation,
            attempt=attempt,
        )

    def feedback_record(
        self,
        request: AuthorityFeedbackRecordRequest,
    ) -> dict[str, Any]:
        """Record structured feedback for a pending authority."""
        feedback = _load_feedback_file(request.feedback_file)
        if not isinstance(feedback, AuthorityFeedbackFile):
            return feedback
        if feedback.authority_id != request.pending_authority_id:
            return _feedback_schema_invalid(
                message="Feedback authority_id does not match request.",
                details={
                    "feedback_authority_id": feedback.authority_id,
                    "pending_authority_id": request.pending_authority_id,
                },
            )

        with Session(self._engine) as session:
            return _record_feedback_in_session(
                session=session,
                request=request,
                feedback=feedback,
            )

    def _start_curation_mutation(
        self,
        request: AuthorityCurationRequest,
    ) -> _ActiveMutation | dict[str, Any]:
        """Acquire the curation mutation lease or return deterministic replay."""
        now = datetime.now(UTC)
        lease_owner = (
            f"agileforge-cli:authority-curate:{request.idempotency_key}:{uuid4()}"
        )
        ledger = MutationLedgerRepository(engine=self._engine)
        loaded = ledger.create_or_load(
            command=AUTHORITY_CURATE_COMMAND,
            idempotency_key=request.idempotency_key,
            request_hash=_curation_request_hash(request),
            project_id=request.project_id,
            correlation_id=request.correlation_id or str(uuid4()),
            changed_by=request.changed_by,
            lease_owner=lease_owner,
            now=now,
            lease_seconds=AUTHORITY_CURATION_LEASE_SECONDS,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code == MUTATION_RECOVERY_REQUIRED:
            stored_response = _stored_ledger_response(
                loaded.ledger.response_json
            )
            if stored_response is not None:
                return stored_response
            reconciled = self._reconcile_no_side_effect_curation_recovery(
                request=request,
                mutation_event_id=_required_mutation_event_id(
                    loaded.ledger.mutation_event_id
                ),
            )
            if reconciled is not None:
                return reconciled
        if loaded.error_code is not None:
            return _curation_ledger_error_response(
                error_code=loaded.error_code,
                mutation_event_id=loaded.ledger.mutation_event_id,
                correlation_id=request.correlation_id,
            )
        return _ActiveMutation(
            ledger=ledger,
            lease_owner=lease_owner,
            mutation_event_id=_required_mutation_event_id(
                loaded.ledger.mutation_event_id
            ),
        )

    def _reconcile_no_side_effect_curation_recovery(
        self,
        *,
        request: AuthorityCurationRequest,
        mutation_event_id: int,
    ) -> dict[str, Any] | None:
        """Recover expired curation if trace/DB prove no candidate was published."""
        summary = summarize_trace(mutation_event_id=mutation_event_id)
        if bool(summary.get("candidate_published")):
            return None

        attempt: AuthorityCurationAttempt | None
        with Session(self._engine) as session:
            attempt = session.exec(
                select(AuthorityCurationAttempt).where(
                    AuthorityCurationAttempt.mutation_event_id
                    == mutation_event_id
                )
            ).first()
            if (
                attempt is not None
                and attempt.candidate_authority_id is not None
            ):
                return None

        response = error_envelope(
            command=AUTHORITY_CURATE_COMMAND,
            error=workbench_error(
                ErrorCode.MUTATION_FAILED,
                message=(
                    "Authority curation mutation expired before candidate "
                    "publication."
                ),
                details={
                    "project_id": request.project_id,
                    "mutation_event_id": mutation_event_id,
                    "trace_artifact_id": summary.get("trace_artifact_id"),
                    "last_trace_step": summary.get("last_trace_step"),
                    "last_trace_status": summary.get("last_trace_status"),
                },
                remediation=[
                    "Retry authority curation with a fresh idempotency key.",
                    "Inspect the trace with agileforge authority curation trace.",
                ],
            ),
            correlation_id=request.correlation_id,
        )
        if attempt is not None:
            self._update_failed_curation_attempt(
                attempt.curation_attempt_id,
                failure_artifact_id=cast(
                    "str | None",
                    summary.get("failure_artifact_id"),
                ),
            )
        finalized = MutationLedgerRepository(
            engine=self._engine
        ).finalize_recovery_as_no_side_effect_failure(
            mutation_event_id=mutation_event_id,
            response=response,
            now=datetime.now(UTC),
        )
        if finalized:
            self._restore_recovered_no_side_effect_workflow(
                request=request,
                mutation_event_id=mutation_event_id,
                failure_artifact_id=_response_failure_artifact_id(response),
                error_code=_response_error_code(response),
            )
            return response
        return None

    def _restore_recovered_no_side_effect_workflow(
        self,
        *,
        request: AuthorityCurationRequest,
        mutation_event_id: int,
        failure_artifact_id: str | None,
        error_code: str | None,
    ) -> None:
        """Restore only the workflow mutex owned by the recovered mutation."""
        try:
            state = self._workflow.get_session_status(str(request.project_id))
        except Exception:  # noqa: BLE001
            return
        if state.get("fsm_state") != "SETUP_REQUIRED":
            return
        if state.get("setup_status") != "authority_curating":
            return
        state_mutation_event_id = state.get("setup_curation_mutation_event_id")
        if str(state_mutation_event_id) != str(mutation_event_id):
            return
        self._restore_authority_rejected_workflow(
            request=request,
            failure_artifact_id=failure_artifact_id,
            error_code=error_code,
        )

    def _validate_curation_guards(  # noqa: PLR0911
        self,
        request: AuthorityCurationRequest,
    ) -> dict[str, Any] | None:
        """Validate ownership, feedback, fingerprint, and workflow mutex guards."""
        with Session(self._engine) as session:
            spec = session.get(SpecRegistry, request.spec_version_id)
            if spec is None or spec.product_id != request.project_id:
                return error_envelope(
                    command=AUTHORITY_CURATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.SPEC_VERSION_NOT_FOUND,
                        message="Spec version was not found for project.",
                        details={
                            "project_id": request.project_id,
                            "spec_version_id": request.spec_version_id,
                        },
                    ),
                    correlation_id=request.correlation_id,
                )

            authority = session.get(
                CompiledSpecAuthority,
                request.source_authority_id,
            )
            if (
                authority is None
                or authority.spec_version_id != request.spec_version_id
            ):
                return error_envelope(
                    command=AUTHORITY_CURATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_NOT_PENDING,
                        message="Source authority was not found for spec version.",
                        details={
                            "source_authority_id": request.source_authority_id,
                            "spec_version_id": request.spec_version_id,
                        },
                    ),
                    correlation_id=request.correlation_id,
                )

            actual_fingerprint = pending_authority_fingerprint(authority)
            if actual_fingerprint != request.expected_source_authority_fingerprint:
                return error_envelope(
                    command=AUTHORITY_CURATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.STALE_AUTHORITY_VERSION,
                        message="Source authority fingerprint changed.",
                        details={
                            "expected": request.expected_source_authority_fingerprint,
                            "actual": actual_fingerprint,
                        },
                    ),
                    correlation_id=request.correlation_id,
                )

            feedback = session.exec(
                select(AuthorityFeedbackAttempt)
                .where(AuthorityFeedbackAttempt.project_id == request.project_id)
                .where(
                    AuthorityFeedbackAttempt.source_authority_id
                    == request.source_authority_id
                )
                .where(
                    AuthorityFeedbackAttempt.feedback_attempt_id
                    == request.feedback_attempt_id
                )
            ).first()
            if feedback is None:
                return error_envelope(
                    command=AUTHORITY_CURATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_GUARD_INCOMPLETE,
                        message="Feedback attempt was not found for source authority.",
                        details={
                            "project_id": request.project_id,
                            "source_authority_id": request.source_authority_id,
                            "feedback_attempt_id": request.feedback_attempt_id,
                        },
                    ),
                    correlation_id=request.correlation_id,
                )

            rejected = session.exec(
                select(SpecAuthorityAcceptance)
                .where(SpecAuthorityAcceptance.product_id == request.project_id)
                .where(
                    SpecAuthorityAcceptance.spec_version_id
                    == request.spec_version_id
                )
                .where(
                    SpecAuthorityAcceptance.pending_authority_id
                    == request.source_authority_id
                )
                .where(SpecAuthorityAcceptance.status == "rejected")
            ).first()
            if rejected is None:
                return error_envelope(
                    command=AUTHORITY_CURATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_REVIEW_REQUIRED,
                        message="Source authority must be rejected before curation.",
                        details={
                            "project_id": request.project_id,
                            "spec_version_id": request.spec_version_id,
                            "source_authority_id": request.source_authority_id,
                        },
                    ),
                    correlation_id=request.correlation_id,
                )

        workflow_error = self._validate_curation_workflow_guard(request)
        if workflow_error is not None:
            return workflow_error
        return None

    def _validate_curation_workflow_guard(
        self,
        request: AuthorityCurationRequest,
    ) -> dict[str, Any] | None:
        """Validate setup state and curation mutex before long-running work."""
        state = self._workflow.get_session_status(str(request.project_id))
        setup_status = state.get("setup_status")
        if setup_status == "authority_curating":
            return _stale_setup_status_error(
                request=request,
                message="Authority curation is already running.",
                actual_fsm_state=state.get("fsm_state"),
                actual_setup_status=setup_status,
                setup_curation_mutation_event_id=state.get(
                    "setup_curation_mutation_event_id"
                ),
            )
        if state.get("fsm_state") != "SETUP_REQUIRED" or setup_status != (
            "authority_rejected"
        ):
            return _stale_setup_status_error(
                request=request,
                message="Authority curation requires rejected setup authority.",
                actual_fsm_state=state.get("fsm_state"),
                actual_setup_status=setup_status,
                setup_curation_mutation_event_id=state.get(
                    "setup_curation_mutation_event_id"
                ),
            )
        return None

    def _create_running_curation_attempt(
        self,
        *,
        request: AuthorityCurationRequest,
        request_hash: str,
        mutation_event_id: int,
    ) -> AuthorityCurationAttempt | dict[str, Any]:
        """Persist the in-progress curation attempt before workflow work."""
        now = datetime.now(UTC)
        row = AuthorityCurationAttempt(
            project_id=request.project_id,
            curation_attempt_id=f"curation-{uuid4()}",
            source_authority_id=request.source_authority_id,
            source_authority_fingerprint=(
                request.expected_source_authority_fingerprint
            ),
            spec_version_id=request.spec_version_id,
            feedback_attempt_id=request.feedback_attempt_id,
            status="running",
            max_iterations=request.max_iterations,
            compiler_model=request.compiler_model,
            request_json=json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            request_hash=request_hash,
            idempotency_key=request.idempotency_key,
            changed_by=request.changed_by,
            mutation_event_id=mutation_event_id,
            created_at=now,
            updated_at=now,
        )
        with Session(self._engine) as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return _running_curation_conflict_response(
                    session=session,
                    request=request,
                )
            session.refresh(row)
            return row

    def _mark_workflow_curating(
        self,
        *,
        request: AuthorityCurationRequest,
        mutation_event_id: int,
    ) -> None:
        """Mark workflow state as curating after durable mutex acquisition."""
        self._workflow.update_session_status(
            str(request.project_id),
            {
                "fsm_state": "SETUP_REQUIRED",
                "setup_status": "authority_curating",
                "setup_curation_mutation_event_id": mutation_event_id,
                "setup_next_actions": [
                    {
                        "command": "agileforge mutation show",
                        "args": {"mutation_event_id": mutation_event_id},
                        "reason": "Inspect the active authority curation mutation.",
                    }
                ],
            },
        )

    def _run_curation_after_status_update(  # noqa: PLR0911
        self,
        *,
        request: AuthorityCurationRequest,
        active_mutation: _ActiveMutation,
        attempt: AuthorityCurationAttempt,
    ) -> dict[str, Any]:
        """Run the ADK workflow and finalize minimal attempt state."""
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="input_load_started",
            status="started",
            curation_attempt_id=attempt.curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )
        loaded_inputs = self._load_source_authority_and_feedback(
            request=request,
            attempt=attempt,
            mutation_event_id=active_mutation.mutation_event_id,
        )
        if not isinstance(loaded_inputs, _LoadedCurationInputs):
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="input_load_failed",
                status="failed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
                error=_trace_error_from_response(
                    loaded_inputs,
                    current_step="input_load_started",
                ),
            )
            self._finalize_failed_curation(
                request=request,
                active_mutation=active_mutation,
                attempt=attempt,
                response=loaded_inputs,
                status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
            )
            return loaded_inputs
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="input_load_completed",
            status="completed",
            curation_attempt_id=attempt.curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )

        try:
            with _trace_step_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="adk_invocation_started",
                completed_step="adk_invocation_completed",
                failed_step="adk_invocation_failed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
            ):
                workflow_result = run_authority_curation_workflow(
                    request=request,
                    curation_attempt_id=attempt.curation_attempt_id,
                    source_authority_json=loaded_inputs.source_authority_json,
                    feedback_json=loaded_inputs.feedback_json,
                )
        except Exception as exc:  # noqa: BLE001
            response = error_envelope(
                command=AUTHORITY_CURATE_COMMAND,
                error=workbench_error(
                    ErrorCode.MUTATION_FAILED,
                    message="Authority curation workflow failed.",
                    details={
                        "project_id": request.project_id,
                        "curation_attempt_id": attempt.curation_attempt_id,
                        "exception_type": type(exc).__name__,
                        "trace_artifact_id": trace_artifact_id(
                            active_mutation.mutation_event_id
                        ),
                    },
                ),
                correlation_id=request.correlation_id,
            )
            self._finalize_failed_curation(
                request=request,
                active_mutation=active_mutation,
                attempt=attempt,
                response=response,
                status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
            )
            return response

        if workflow_result.get("ok") is True:
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="diff_validation_started",
                status="started",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
            )
            validated = self._validate_successful_curation_candidate(
                request=request,
                attempt=attempt,
                workflow_result=workflow_result,
                mutation_event_id=active_mutation.mutation_event_id,
            )
            if not isinstance(validated, _ValidatedCurationCandidate):
                _append_trace_event_safely(
                    mutation_event_id=active_mutation.mutation_event_id,
                    project_id=request.project_id,
                    step="diff_validation_failed",
                    status="failed",
                    curation_attempt_id=attempt.curation_attempt_id,
                    correlation_id=request.correlation_id,
                    attributes=_curation_failure_trace_attributes(
                        request=request,
                        response=validated,
                    ),
                    error=_trace_error_from_response(
                        validated,
                        current_step="diff_validation_started",
                    ),
                )
                self._finalize_failed_curation(
                    request=request,
                    active_mutation=active_mutation,
                    attempt=attempt,
                    response=validated,
                    status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
                )
                return validated
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="diff_validation_completed",
                status="completed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
            )

            published = self._publish_validated_curation_candidate(
                request=request,
                attempt=attempt,
                validated=validated,
                mutation_event_id=active_mutation.mutation_event_id,
            )
            if not isinstance(published, _PublishedCurationCandidate):
                if _response_error_code(published) == (
                    ErrorCode.MUTATION_RECOVERY_REQUIRED.value
                ):
                    self._finalize_recovery_required_curation(
                        request=request,
                        active_mutation=active_mutation,
                        response=published,
                        curation_attempt_id=attempt.curation_attempt_id,
                    )
                else:
                    self._finalize_failed_curation(
                        request=request,
                        active_mutation=active_mutation,
                        attempt=attempt,
                        response=published,
                        status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
                    )
                return published

            response = success_envelope(
                command=AUTHORITY_CURATE_COMMAND,
                data={
                    "status": "authority_pending_review",
                    "project_id": request.project_id,
                    "curation_attempt_id": attempt.curation_attempt_id,
                    "mutation_event_id": active_mutation.mutation_event_id,
                    "trace_artifact_id": trace_artifact_id(
                        active_mutation.mutation_event_id
                    ),
                    "pending_authority_id": published.authority_id,
                    "pending_authority_fingerprint": (
                        published.authority_fingerprint
                    ),
                    "diff_summary": validated.diff["summary"],
                    "lineage": validated.diff["lineage_json"],
                },
                correlation_id=request.correlation_id,
            )
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="mutation_finalize_started",
                status="started",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(
                    request,
                    candidate_authority_id=published.authority_id,
                    candidate_authority_fingerprint=published.authority_fingerprint,
                ),
            )
            finalized = active_mutation.ledger.finalize_success(
                mutation_event_id=active_mutation.mutation_event_id,
                lease_owner=active_mutation.lease_owner,
                after={
                    "curation_attempt_id": attempt.curation_attempt_id,
                    "candidate_authority_id": published.authority_id,
                    "candidate_authority_fingerprint": (
                        published.authority_fingerprint
                    ),
                    "diff_summary": validated.diff["summary"],
                },
                response=response,
                now=datetime.now(UTC),
            )
            if not finalized:
                _append_trace_event_safely(
                    mutation_event_id=active_mutation.mutation_event_id,
                    project_id=request.project_id,
                    step="mutation_finalize_failed",
                    status="failed",
                    curation_attempt_id=attempt.curation_attempt_id,
                    correlation_id=request.correlation_id,
                    attributes=_curation_trace_attributes(
                        request,
                        candidate_authority_id=published.authority_id,
                        candidate_authority_fingerprint=(
                            published.authority_fingerprint
                        ),
                        failure_stage="ledger_finalize_failed_after_publish",
                    ),
                    error={
                        "code": ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
                        "message": "Authority curation mutation finalization failed.",
                        "retryable": False,
                        "details": {
                            "current_step": "mutation_finalize_started",
                            "failure_stage": "ledger_finalize_failed_after_publish",
                            "candidate_authority_id": published.authority_id,
                            "candidate_authority_fingerprint": (
                                published.authority_fingerprint
                            ),
                        },
                    },
                )
                recovery_response = _published_curation_recovery_response(
                    request=request,
                    attempt=attempt,
                    published=published,
                    failure_stage="ledger_finalize_failed_after_publish",
                    metadata={
                        "mutation_event_id": active_mutation.mutation_event_id,
                    },
                )
                self._finalize_recovery_required_curation(
                    request=request,
                    active_mutation=active_mutation,
                    response=recovery_response,
                    curation_attempt_id=attempt.curation_attempt_id,
                )
                return recovery_response
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="mutation_finalize_completed",
                status="completed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(
                    request,
                    candidate_authority_id=published.authority_id,
                    candidate_authority_fingerprint=published.authority_fingerprint,
                    event_count=_trace_event_count_safely(
                        mutation_event_id=active_mutation.mutation_event_id,
                    ),
                ),
            )
            return response

        if workflow_result.get("status") == "failed":
            response = _failed_curation_workflow_response(
                request=request,
                attempt=attempt,
                workflow_result=workflow_result,
                mutation_event_id=active_mutation.mutation_event_id,
            )
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="adk_invocation_failed",
                status="failed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
                error=_trace_error_from_response(
                    response,
                    current_step="adk_invocation_started",
                ),
            )
            self._finalize_failed_curation(
                request=request,
                active_mutation=active_mutation,
                attempt=attempt,
                response=response,
                status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
            )
            return response

        response = error_envelope(
            command=AUTHORITY_CURATE_COMMAND,
            error=workbench_error(
                ErrorCode.COMMAND_NOT_IMPLEMENTED,
                message="Authority curation workflow is not implemented.",
                details={
                    "curation_attempt_id": attempt.curation_attempt_id,
                    "trace_artifact_id": trace_artifact_id(
                        active_mutation.mutation_event_id
                    ),
                },
            ),
            correlation_id=request.correlation_id,
        )
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="adk_invocation_failed",
            status="failed",
            curation_attempt_id=attempt.curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
            error=_trace_error_from_response(
                response,
                current_step="adk_invocation_started",
            ),
        )
        self._finalize_failed_curation(
            request=request,
            active_mutation=active_mutation,
            attempt=attempt,
            response=response,
            status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
        )
        return response

    def _validate_successful_curation_candidate(
        self,
        *,
        request: AuthorityCurationRequest,
        attempt: AuthorityCurationAttempt,
        workflow_result: dict[str, Any],
        mutation_event_id: int,
    ) -> _ValidatedCurationCandidate | dict[str, Any]:
        """Validate workflow candidate JSON before marking curation succeeded."""
        candidate_authority_json = _json_object_from_value(
            workflow_result.get("candidate_authority_json")
        )
        if not _is_authority_json(candidate_authority_json):
            return _invalid_curation_candidate_response(
                request=request,
                attempt=attempt,
                reason="missing_or_invalid_candidate_authority_json",
                mutation_event_id=mutation_event_id,
            )
        candidate_authority_json = cast("dict[str, Any]", candidate_authority_json)

        loaded = self._load_source_authority_and_feedback(
            request=request,
            attempt=attempt,
            mutation_event_id=mutation_event_id,
        )
        if not isinstance(loaded, _LoadedCurationInputs):
            return loaded

        targeted_source_item_ids = _targeted_source_item_ids(
            feedback_json=loaded.feedback_json,
            source_authority_json=loaded.source_authority_json,
        )
        try:
            diff = build_authority_diff(
                source_authority_json=loaded.source_authority_json,
                candidate_authority_json=candidate_authority_json,
                targeted_source_item_ids=targeted_source_item_ids,
            )
        except AuthorityDiffValidationError as exc:
            return error_envelope(
                command=AUTHORITY_CURATE_COMMAND,
                error=workbench_error(
                    ErrorCode.AUTHORITY_CURATED_DIFF_UNBOUNDED,
                    message="Authority curation produced an unsafe authority diff.",
                    details={
                        "curation_attempt_id": attempt.curation_attempt_id,
                        "validation_error_count": len(exc.validation_errors),
                        "validation_errors": exc.validation_errors,
                        "trace_artifact_id": trace_artifact_id(mutation_event_id),
                    },
                ),
                correlation_id=request.correlation_id,
            )
        if diff["summary"]["untargeted_change_count"] > 0:
            return error_envelope(
                command=AUTHORITY_CURATE_COMMAND,
                error=workbench_error(
                    ErrorCode.AUTHORITY_CURATED_DIFF_UNBOUNDED,
                    message="Authority curation changed untargeted authority items.",
                    details={
                        "curation_attempt_id": attempt.curation_attempt_id,
                        "untargeted_change_count": diff["summary"][
                            "untargeted_change_count"
                        ],
                        "untargeted_changes": diff["untargeted_changes"],
                        "trace_artifact_id": trace_artifact_id(mutation_event_id),
                    },
                ),
                correlation_id=request.correlation_id,
            )

        return _ValidatedCurationCandidate(
            candidate_authority_json=candidate_authority_json,
            diff=diff,
            quality_report=_json_like_or_empty(workflow_result.get("quality_report")),
            candidate_lineage=_json_like_or_empty(
                workflow_result.get("candidate_lineage_json"),
                default=diff["lineage_json"],
            ),
        )

    def _publish_validated_curation_candidate(
        self,
        *,
        request: AuthorityCurationRequest,
        attempt: AuthorityCurationAttempt,
        validated: _ValidatedCurationCandidate,
        mutation_event_id: int,
    ) -> _PublishedCurationCandidate | dict[str, Any]:
        """Publish a validated candidate as a new pending authority row."""
        candidate_authority_json = validated.candidate_authority_json
        _append_trace_event_safely(
            mutation_event_id=mutation_event_id,
            project_id=request.project_id,
            step="candidate_publication_started",
            status="started",
            curation_attempt_id=attempt.curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )
        try:
            authority = _authority_from_candidate_json(
                spec_version_id=request.spec_version_id,
                candidate_authority_json=candidate_authority_json,
            )
            with Session(self._engine) as session:
                session.add(authority)
                session.commit()
                session.refresh(authority)
                published = _published_curation_candidate(authority)
        except Exception as exc:  # noqa: BLE001
            response = error_envelope(
                command=AUTHORITY_CURATE_COMMAND,
                error=workbench_error(
                    ErrorCode.MUTATION_FAILED,
                    message="Authority curation candidate publication failed.",
                    details={
                        "project_id": request.project_id,
                        "curation_attempt_id": attempt.curation_attempt_id,
                        "exception_type": type(exc).__name__,
                        "trace_artifact_id": trace_artifact_id(mutation_event_id),
                    },
                ),
                correlation_id=request.correlation_id,
            )
            _append_trace_event_safely(
                mutation_event_id=mutation_event_id,
                project_id=request.project_id,
                step="candidate_publication_failed",
                status="failed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(request),
                error=_trace_error_from_response(
                    response,
                    current_step="candidate_publication_started",
                ),
            )
            return response
        _append_trace_event_safely(
            mutation_event_id=mutation_event_id,
            project_id=request.project_id,
            step="candidate_publication_completed",
            status="completed",
            curation_attempt_id=attempt.curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(
                request,
                candidate_authority_id=published.authority_id,
                candidate_authority_fingerprint=published.authority_fingerprint,
            ),
        )
        try:
            with _trace_step_safely(
                mutation_event_id=mutation_event_id,
                project_id=request.project_id,
                step="workflow_pending_review_started",
                completed_step="workflow_pending_review_completed",
                failed_step="workflow_pending_review_failed",
                curation_attempt_id=attempt.curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=_curation_trace_attributes(
                    request,
                    candidate_authority_id=published.authority_id,
                    candidate_authority_fingerprint=(
                        published.authority_fingerprint
                    ),
                ),
            ):
                self._update_succeeded_curation_attempt(
                    attempt.curation_attempt_id,
                    published=published,
                    validated=validated,
                )
                self._mark_workflow_pending_review(
                    request=request,
                    pending_authority_id=published.authority_id,
                    pending_authority_fingerprint=published.authority_fingerprint,
                )
        except Exception as exc:  # noqa: BLE001
            return _published_curation_recovery_response(
                request=request,
                attempt=attempt,
                published=published,
                failure_stage="workflow_update_failed_after_publish",
                metadata={
                    "exception_type": type(exc).__name__,
                    "mutation_event_id": mutation_event_id,
                },
            )
        return published

    def _mark_workflow_pending_review(
        self,
        *,
        request: AuthorityCurationRequest,
        pending_authority_id: int,
        pending_authority_fingerprint: str,
    ) -> None:
        """Move setup workflow to review the newly published curation candidate."""
        self._workflow.update_session_status(
            str(request.project_id),
            {
                "fsm_state": "SETUP_REQUIRED",
                "setup_status": "authority_pending_review",
                "setup_curation_mutation_event_id": None,
                "pending_authority_id": pending_authority_id,
                "pending_authority_fingerprint": pending_authority_fingerprint,
                "setup_next_actions": [
                    {
                        "command": "agileforge authority review",
                        "args": {"project_id": request.project_id},
                        "reason": "Review the curated authority candidate.",
                    }
                ],
            },
        )

    def _load_source_authority_and_feedback(
        self,
        *,
        request: AuthorityCurationRequest,
        attempt: AuthorityCurationAttempt,
        mutation_event_id: int | None = None,
    ) -> _LoadedCurationInputs | dict[str, Any]:
        """Load source authority artifact and feedback row for host validation."""
        with Session(self._engine) as session:
            authority = session.get(
                CompiledSpecAuthority,
                request.source_authority_id,
            )
            feedback = session.exec(
                select(AuthorityFeedbackAttempt)
                .where(AuthorityFeedbackAttempt.project_id == request.project_id)
                .where(
                    AuthorityFeedbackAttempt.source_authority_id
                    == request.source_authority_id
                )
                .where(
                    AuthorityFeedbackAttempt.feedback_attempt_id
                    == request.feedback_attempt_id
                )
            ).first()
            source_authority_json = _json_object_from_value(
                authority.compiled_artifact_json if authority is not None else None
            )
            if not _is_authority_json(source_authority_json) or feedback is None:
                return _invalid_curation_candidate_response(
                    request=request,
                    attempt=attempt,
                    reason="missing_or_invalid_source_authority_inputs",
                    mutation_event_id=mutation_event_id,
                )
            source_authority_json = cast("dict[str, Any]", source_authority_json)
            return _LoadedCurationInputs(
                source_authority_json=source_authority_json,
                feedback_json=feedback.feedback_json,
            )

    def _finalize_failed_curation(
        self,
        *,
        request: AuthorityCurationRequest,
        active_mutation: _ActiveMutation,
        attempt: AuthorityCurationAttempt,
        response: dict[str, Any],
        status: MutationStatus,
    ) -> None:
        """Finalize failed curation and restore rejected workflow state."""
        response = _decorate_response_trace_artifact(
            response,
            mutation_event_id=active_mutation.mutation_event_id,
        )
        failure_artifact_id = _response_failure_artifact_id(response)
        error_code = _response_error_code(response)
        self._update_failed_curation_attempt(
            attempt.curation_attempt_id,
            failure_artifact_id=failure_artifact_id,
        )
        self._finalize_mutation_status_with_trace(
            request=request,
            active_mutation=active_mutation,
            response=response,
            status=status,
            curation_attempt_id=attempt.curation_attempt_id,
        )
        self._restore_authority_rejected_workflow(
            request=request,
            failure_artifact_id=failure_artifact_id,
            error_code=error_code,
        )

    def _finalize_mutation_status_with_trace(
        self,
        *,
        request: AuthorityCurationRequest,
        active_mutation: _ActiveMutation,
        response: dict[str, Any],
        status: MutationStatus,
        curation_attempt_id: str | None = None,
    ) -> bool:
        """Finalize a failed mutation and trace the ledger terminal write."""
        attributes = _curation_failure_trace_attributes(
            request=request,
            response=response,
        )
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="mutation_finalize_started",
            status="started",
            curation_attempt_id=curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=attributes,
        )
        finalized = _finalize_mutation_status(
            engine=self._engine,
            mutation_event_id=active_mutation.mutation_event_id,
            lease_owner=active_mutation.lease_owner,
            status=status,
            response=response,
        )
        if finalized:
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="mutation_finalize_completed",
                status="completed",
                curation_attempt_id=curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=attributes,
            )
        else:
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="mutation_finalize_failed",
                status="failed",
                curation_attempt_id=curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=attributes,
                error={
                    "code": ErrorCode.MUTATION_FAILED.value,
                    "message": "Authority curation mutation finalization failed.",
                    "retryable": False,
                    "details": {"current_step": "mutation_finalize_started"},
                },
            )
        return finalized

    def _finalize_recovery_required_curation(
        self,
        *,
        request: AuthorityCurationRequest,
        active_mutation: _ActiveMutation,
        response: dict[str, Any],
        curation_attempt_id: str | None = None,
    ) -> None:
        """Persist recovery-required mutation state after publish side effects."""
        attributes = _curation_failure_trace_attributes(
            request=request,
            response=response,
        )
        _append_trace_event_safely(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="mutation_finalize_started",
            status="started",
            curation_attempt_id=curation_attempt_id,
            correlation_id=request.correlation_id,
            attributes=attributes,
        )
        finalized = _finalize_mutation_recovery_required(
            engine=self._engine,
            mutation_event_id=active_mutation.mutation_event_id,
            lease_owner=active_mutation.lease_owner,
            response=response,
        )
        if finalized:
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="mutation_finalize_completed",
                status="completed",
                curation_attempt_id=curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=attributes,
            )
        else:
            _append_trace_event_safely(
                mutation_event_id=active_mutation.mutation_event_id,
                project_id=request.project_id,
                step="mutation_finalize_failed",
                status="failed",
                curation_attempt_id=curation_attempt_id,
                correlation_id=request.correlation_id,
                attributes=attributes,
                error={
                    "code": ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
                    "message": "Authority curation recovery finalization failed.",
                    "retryable": False,
                    "details": {"current_step": "mutation_finalize_started"},
                },
            )

    def _restore_authority_rejected_workflow(
        self,
        *,
        request: AuthorityCurationRequest,
        failure_artifact_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Restore setup workflow after curation fails before publication."""
        try:
            self._workflow.update_session_status(
                str(request.project_id),
                {
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_status": "authority_rejected",
                    "setup_curation_mutation_event_id": None,
                    "setup_curation_failure_artifact_id": failure_artifact_id,
                    "setup_curation_error_code": error_code
                    or ErrorCode.MUTATION_FAILED.value,
                    "setup_error": {
                        "code": error_code or ErrorCode.MUTATION_FAILED.value,
                        "message": "Authority curation failed before publication.",
                    },
                    "setup_next_actions": [
                        {
                            "command": AUTHORITY_CURATE_COMMAND,
                            "args": {
                                "project_id": request.project_id,
                                "spec_version_id": request.spec_version_id,
                                "source_authority_id": request.source_authority_id,
                                "expected_source_authority_fingerprint": (
                                    request.expected_source_authority_fingerprint
                                ),
                                "feedback_attempt_id": request.feedback_attempt_id,
                                "idempotency_key": "<idempotency_key>",
                            },
                            "reason": (
                                "Retry authority curation with a fresh "
                                "idempotency key."
                            ),
                        }
                    ],
                },
            )
        except Exception:  # noqa: BLE001
            return

    def _update_failed_curation_attempt(
        self,
        curation_attempt_id: str,
        *,
        failure_artifact_id: str | None = None,
    ) -> None:
        """Mark a curation attempt failed by stable attempt id."""
        with Session(self._engine) as session:
            row = session.exec(
                select(AuthorityCurationAttempt).where(
                    AuthorityCurationAttempt.curation_attempt_id
                    == curation_attempt_id
                )
            ).first()
            if row is None:
                return
            row.status = "failed"
            row.failure_artifact_id = failure_artifact_id
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()

    def _update_succeeded_curation_attempt(
        self,
        curation_attempt_id: str,
        *,
        published: _PublishedCurationCandidate,
        validated: _ValidatedCurationCandidate,
    ) -> None:
        """Persist successful curation audit metadata."""
        with Session(self._engine) as session:
            row = session.exec(
                select(AuthorityCurationAttempt).where(
                    AuthorityCurationAttempt.curation_attempt_id
                    == curation_attempt_id
                )
            ).first()
            if row is None:
                return
            row.status = "succeeded"
            row.candidate_authority_id = published.authority_id
            row.candidate_authority_fingerprint = published.authority_fingerprint
            row.diff_summary_json = _canonical_json(validated.diff["summary"])
            row.lineage_json = _canonical_json(validated.diff["lineage_json"])
            row.quality_report_json = _canonical_json(validated.quality_report)
            row.candidate_lineage_json = _canonical_json(
                validated.candidate_lineage
            )
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()


def _record_feedback_in_session(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
    feedback: AuthorityFeedbackFile,
) -> dict[str, Any]:
    """Record validated feedback inside one database session."""
    payload = feedback.model_dump(mode="json")
    feedback_fingerprint = canonical_hash(payload)
    request_hash = _request_hash(
        request=request,
        feedback_fingerprint=feedback_fingerprint,
    )
    replay = _idempotency_replay(
        session=session,
        request=request,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    guard = _authority_guard(session=session, request=request)
    if guard.error is not None:
        return guard.error
    authority = cast("CompiledSpecAuthority", guard.authority)

    target_error = _feedback_target_error(feedback=feedback, authority=authority)
    if target_error is not None:
        return error_envelope(
            command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
            error=target_error,
            correlation_id=request.correlation_id,
        )

    row = _build_feedback_attempt(
        request=request,
        actual_fingerprint=guard.authority_fingerprint or "",
        feedback=feedback,
        feedback_fingerprint=feedback_fingerprint,
        request_hash=request_hash,
    )
    commit_conflict = _commit_feedback_attempt(
        session=session,
        request=request,
        request_hash=request_hash,
        row=row,
    )
    if commit_conflict is not None:
        return commit_conflict

    return success_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        data=_feedback_attempt_response(row),
        correlation_id=request.correlation_id,
    )


def _curation_request_hash(request: AuthorityCurationRequest) -> str:
    """Return deterministic request hash for curation idempotency."""
    return canonical_hash(
        {
            "command": AUTHORITY_CURATE_COMMAND,
            "project_id": request.project_id,
            "spec_version_id": request.spec_version_id,
            "source_authority_id": request.source_authority_id,
            "expected_source_authority_fingerprint": (
                request.expected_source_authority_fingerprint
            ),
            "feedback_attempt_id": request.feedback_attempt_id,
            "max_iterations": request.max_iterations,
            "compiler_model": request.compiler_model,
        }
    )


def _curation_trace_attributes(
    request: AuthorityCurationRequest,
    *,
    candidate_authority_id: int | None = None,
    candidate_authority_fingerprint: str | None = None,
    event_count: object = None,
    failure_stage: str | None = None,
) -> dict[str, object]:
    """Return allowlisted trace attributes for authority curation."""
    attrs: dict[str, object] = {
        "spec_version_id": request.spec_version_id,
        "source_authority_id": request.source_authority_id,
        "source_authority_fingerprint": (
            request.expected_source_authority_fingerprint
        ),
        "feedback_attempt_id": request.feedback_attempt_id,
        "requested_model_id": _authority_curation_model_id(request),
        "compiler_version": AUTHORITY_CURATION_COMPILER_VERSION,
        "prompt_hash": AUTHORITY_CURATION_PROMPT_HASH,
    }
    if candidate_authority_id is not None:
        attrs["candidate_authority_id"] = candidate_authority_id
    if candidate_authority_fingerprint is not None:
        attrs["candidate_authority_fingerprint"] = candidate_authority_fingerprint
    if event_count is not None:
        attrs["event_count"] = event_count
    if failure_stage is not None:
        attrs["failure_stage"] = failure_stage
    return attrs


def _curation_failure_trace_attributes(
    *,
    request: AuthorityCurationRequest,
    response: dict[str, Any],
) -> dict[str, object]:
    """Return trace attributes with bounded failure counters from a response."""
    attrs = _curation_trace_attributes(request)
    details = _response_error_details(response)
    for key in ("validation_error_count", "untargeted_change_count"):
        if key in details:
            attrs[key] = details[key]
    return attrs


def _decorate_response_trace_artifact(
    response: dict[str, Any],
    *,
    mutation_event_id: int,
) -> dict[str, Any]:
    """Add trace artifact metadata to the first response error details."""
    errors = response.get("errors")
    if not isinstance(errors, list) or not errors:
        return response
    first_error = errors[0]
    if not isinstance(first_error, dict):
        return response
    details = first_error.get("details")
    decorated_details = dict(details) if isinstance(details, dict) else {}
    decorated_details["trace_artifact_id"] = trace_artifact_id(mutation_event_id)
    first_error["details"] = decorated_details
    return response


def _append_trace_event_safely(  # noqa: PLR0913
    *,
    mutation_event_id: int,
    project_id: int,
    step: str,
    status: str,
    curation_attempt_id: str | None = None,
    correlation_id: str | None = None,
    attributes: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
) -> None:
    """Append a trace event without masking curation behavior."""
    with suppress(Exception):
        append_trace_event(
            mutation_event_id=mutation_event_id,
            project_id=project_id,
            step=step,
            status=status,
            curation_attempt_id=curation_attempt_id,
            correlation_id=correlation_id,
            attributes=attributes,
            error=error,
        )


@contextmanager
def _trace_step_safely(  # noqa: PLR0913
    *,
    mutation_event_id: int,
    project_id: int,
    step: str,
    completed_step: str,
    failed_step: str,
    curation_attempt_id: str | None = None,
    correlation_id: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """Trace a step without allowing trace failures to change behavior."""
    _append_trace_event_safely(
        mutation_event_id=mutation_event_id,
        project_id=project_id,
        step=step,
        status="started",
        curation_attempt_id=curation_attempt_id,
        correlation_id=correlation_id,
        attributes=attributes,
    )
    try:
        yield
    except Exception as exc:
        _append_trace_event_safely(
            mutation_event_id=mutation_event_id,
            project_id=project_id,
            step=failed_step,
            status="failed",
            curation_attempt_id=curation_attempt_id,
            correlation_id=correlation_id,
            attributes=attributes,
            error={
                "code": type(exc).__name__,
                "message": "Authority curation step failed.",
                "retryable": False,
                "details": {"current_step": step},
            },
        )
        raise
    else:
        _append_trace_event_safely(
            mutation_event_id=mutation_event_id,
            project_id=project_id,
            step=completed_step,
            status="completed",
            curation_attempt_id=curation_attempt_id,
            correlation_id=correlation_id,
            attributes=attributes,
        )


def _trace_event_count_safely(*, mutation_event_id: int) -> object:
    """Return trace event_count when summary is readable."""
    with suppress(Exception):
        return summarize_trace(mutation_event_id=mutation_event_id).get("event_count")
    return None


def _trace_error_from_response(
    response: dict[str, Any],
    *,
    current_step: str,
) -> dict[str, object]:
    """Return a bounded trace error from a workbench response envelope."""
    errors = response.get("errors")
    first_error = errors[0] if isinstance(errors, list) and errors else {}
    if not isinstance(first_error, dict):
        first_error = {}
    code = _string_or_none(first_error.get("code")) or ErrorCode.MUTATION_FAILED.value
    message = _string_or_none(first_error.get("message")) or (
        "Authority curation step failed."
    )
    retryable = first_error.get("retryable")
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": retryable if isinstance(retryable, bool) else False,
    }
    failure_artifact_id = _string_or_none(first_error.get("failure_artifact_id"))
    if failure_artifact_id is not None:
        error["failure_artifact_id"] = failure_artifact_id
    details = _response_error_details(response)
    details["current_step"] = current_step
    error["details"] = details
    return error


def _response_error_details(response: dict[str, Any]) -> dict[str, object]:
    """Extract response error details without exposing non-object values."""
    errors = response.get("errors")
    first_error = errors[0] if isinstance(errors, list) and errors else {}
    if not isinstance(first_error, dict):
        return {}
    details = first_error.get("details")
    if not isinstance(details, dict):
        return {}
    return {str(key): value for key, value in details.items()}


def _json_object_from_value(value: object) -> dict[str, Any] | None:
    """Return a JSON object from an existing dict or encoded string."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        if isinstance(dumped, dict):
            return {str(key): item for key, item in dumped.items()}
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if not isinstance(value, str) or not value:
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return {str(key): item for key, item in loaded.items()}
    return None


def _is_authority_json(value: object) -> bool:
    """Return whether a value has the minimal authority JSON shape."""
    if not isinstance(value, dict):
        return False
    parsed = {str(key): item for key, item in value.items()}
    invariants = parsed.get("invariants")
    return isinstance(invariants, list) and all(
        isinstance(item, dict) for item in invariants
    )


def _authority_from_candidate_json(
    *,
    spec_version_id: int,
    candidate_authority_json: dict[str, Any],
) -> CompiledSpecAuthority:
    """Build a pending compiled authority row from candidate JSON."""
    compiler_version = _candidate_text_field(
        candidate_authority_json,
        "compiler_version",
        default=AUTHORITY_CURATION_COMPILER_VERSION,
    )
    prompt_hash = _candidate_text_field(
        candidate_authority_json,
        "prompt_hash",
        default=AUTHORITY_CURATION_PROMPT_HASH,
    )
    return CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=compiler_version,
        prompt_hash=prompt_hash,
        compiled_artifact_json=_canonical_json(candidate_authority_json),
        scope_themes=_canonical_json(
            _candidate_json_field(candidate_authority_json, "scope_themes")
        ),
        invariants=_canonical_json(
            _candidate_json_field(candidate_authority_json, "invariants")
        ),
        eligible_feature_ids=_canonical_json(
            _candidate_json_field(
                candidate_authority_json,
                "eligible_feature_ids",
                "eligible_feature_rules",
            )
        ),
        rejected_features=_canonical_json(
            _candidate_json_field(candidate_authority_json, "rejected_features")
        ),
        spec_gaps=_canonical_json(
            _candidate_json_field(candidate_authority_json, "spec_gaps", "gaps")
        ),
    )


def _published_curation_candidate(
    authority: CompiledSpecAuthority,
) -> _PublishedCurationCandidate:
    """Return required identity metadata for a published authority."""
    authority_id = authority.authority_id
    authority_fingerprint = pending_authority_fingerprint(authority)
    if authority_id is None or authority_fingerprint is None:
        message = "Published authority is missing identity metadata."
        raise RuntimeError(message)
    return _PublishedCurationCandidate(
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )


def _candidate_text_field(
    candidate_authority_json: dict[str, Any],
    field_name: str,
    *,
    default: str,
) -> str:
    """Return a stable text field from candidate JSON with deterministic fallback."""
    value = candidate_authority_json.get(field_name)
    if isinstance(value, str) and value:
        return value
    return default


def _candidate_json_field(
    candidate_authority_json: dict[str, Any],
    field_name: str,
    fallback_field_name: str | None = None,
) -> object:
    """Return candidate JSON field with optional equivalent fallback."""
    value = candidate_authority_json.get(field_name)
    if value is None and fallback_field_name is not None:
        value = candidate_authority_json.get(fallback_field_name)
    if value is None:
        return []
    return value


def _invalid_curation_candidate_response(
    *,
    request: AuthorityCurationRequest,
    attempt: AuthorityCurationAttempt,
    reason: str,
    mutation_event_id: int | None = None,
) -> dict[str, Any]:
    """Return a structured fail-closed curation validation error."""
    details: dict[str, Any] = {
        "project_id": request.project_id,
        "curation_attempt_id": attempt.curation_attempt_id,
        "reason": reason,
    }
    if mutation_event_id is not None:
        details["trace_artifact_id"] = trace_artifact_id(mutation_event_id)
    return error_envelope(
        command=AUTHORITY_CURATE_COMMAND,
        error=workbench_error(
            ErrorCode.MUTATION_FAILED,
            message="Authority curation did not produce a valid candidate.",
            details=details,
        ),
        correlation_id=request.correlation_id,
    )


def _targeted_source_item_ids(
    *,
    feedback_json: str,
    source_authority_json: dict[str, Any],
) -> set[str]:
    """Derive feedback-targeted source item ids for host diff validation."""
    feedback = _json_object_from_value(feedback_json)
    if feedback is None:
        return set()
    invariant_source_items = _source_item_ids_by_invariant_id(source_authority_json)
    targeted: set[str] = set()
    feedback_items = feedback.get("feedback_items")
    if not isinstance(feedback_items, list):
        return targeted
    for item in feedback_items:
        if not isinstance(item, dict):
            continue
        source_item_id = item.get("source_item_id")
        if isinstance(source_item_id, str):
            targeted.add(source_item_id)
        target_id = item.get("target_id")
        if not isinstance(target_id, str):
            continue
        if item.get("target_kind") == "source_item":
            targeted.add(target_id)
        mapped_source_item_id = invariant_source_items.get(target_id)
        if item.get("target_kind") == "invariant" and mapped_source_item_id is not None:
            targeted.add(mapped_source_item_id)
    return targeted


def _source_item_ids_by_invariant_id(
    source_authority_json: dict[str, Any],
) -> dict[str, str]:
    """Map source invariant ids to source item ids where available."""
    invariants = source_authority_json.get("invariants")
    if not isinstance(invariants, list):
        return {}
    result: dict[str, str] = {}
    for item in invariants:
        if not isinstance(item, dict):
            continue
        invariant_id = item.get("id")
        source_item_id = item.get("source_item_id")
        if isinstance(invariant_id, str) and isinstance(source_item_id, str):
            result[invariant_id] = source_item_id
    return result


def _json_like_or_empty(value: object, *, default: object | None = None) -> object:
    """Return JSON-like workflow metadata without failing on malformed values."""
    if isinstance(value, dict | list):
        return value
    return default if default is not None else {}


def _string_or_none(value: object) -> str | None:
    """Return a simple string value without coercing nested workflow content."""
    if isinstance(value, str) and value:
        return value
    return None


def _error_code_from_workflow(value: object) -> ErrorCode:
    """Map workflow error code strings onto registered workbench errors."""
    if not isinstance(value, str):
        return ErrorCode.MUTATION_FAILED
    try:
        return ErrorCode(value)
    except ValueError:
        return ErrorCode.MUTATION_FAILED


def _failed_curation_workflow_response(
    *,
    request: AuthorityCurationRequest,
    attempt: AuthorityCurationAttempt,
    workflow_result: dict[str, Any],
    mutation_event_id: int,
) -> dict[str, Any]:
    """Return a bounded envelope for a failed workflow result."""
    code = _error_code_from_workflow(workflow_result.get("error_code"))
    failure_artifact_id = _string_or_none(workflow_result.get("failure_artifact_id"))
    details: dict[str, Any] = {
        "project_id": request.project_id,
        "curation_attempt_id": attempt.curation_attempt_id,
        "trace_artifact_id": trace_artifact_id(mutation_event_id),
    }
    if failure_artifact_id is not None:
        details["failure_artifact_id"] = failure_artifact_id
    return error_envelope(
        command=AUTHORITY_CURATE_COMMAND,
        error=workbench_error(
            code,
            message="Authority curation workflow failed.",
            details=details,
            remediation=[
                "Inspect the failure artifact when failure_artifact_id is present.",
                "Retry authority curation after addressing the failure.",
            ],
        ),
        correlation_id=request.correlation_id,
    )


def _published_curation_recovery_response(
    *,
    request: AuthorityCurationRequest,
    attempt: AuthorityCurationAttempt,
    published: _PublishedCurationCandidate,
    failure_stage: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return recovery-required response after candidate publication."""
    metadata_details = metadata or {}
    details: dict[str, Any] = {
        "project_id": request.project_id,
        "curation_attempt_id": attempt.curation_attempt_id,
        "candidate_authority_id": published.authority_id,
        "candidate_authority_fingerprint": published.authority_fingerprint,
        "failure_stage": failure_stage,
    }
    metadata_mutation_event_id = metadata_details.get("mutation_event_id")
    if isinstance(metadata_mutation_event_id, int):
        details["trace_artifact_id"] = trace_artifact_id(metadata_mutation_event_id)
    details.update(metadata_details)
    return error_envelope(
        command=AUTHORITY_CURATE_COMMAND,
        error=workbench_error(
            ErrorCode.MUTATION_RECOVERY_REQUIRED,
            message=(
                "Authority curation published a candidate but workflow recovery "
                "is required."
            ),
            details=details,
            remediation=[
                "Inspect the mutation ledger before retrying curation.",
                (
                    "Use the candidate_authority_id and fingerprint to recover "
                    "the pending authority review state."
                ),
            ],
        ),
        correlation_id=request.correlation_id,
    )


def _response_error_code(response: dict[str, Any]) -> str | None:
    """Extract the first response error code without coercing nested content."""
    errors = response.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first_error = errors[0]
    if not isinstance(first_error, dict):
        return None
    return _string_or_none(first_error.get("code"))


def _response_failure_artifact_id(response: dict[str, Any]) -> str | None:
    """Extract a bounded failure artifact id from a response envelope."""
    errors = response.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first_error = errors[0]
    if not isinstance(first_error, dict):
        return None
    details = first_error.get("details")
    if not isinstance(details, dict):
        return None
    return _string_or_none(details.get("failure_artifact_id"))


def _response_error_details(response: dict[str, Any]) -> dict[str, Any]:
    """Extract first error details from an envelope as a plain dictionary."""
    errors = response.get("errors")
    if not isinstance(errors, list) or not errors:
        return {}
    first_error = errors[0]
    if not isinstance(first_error, dict):
        return {}
    details = first_error.get("details")
    if isinstance(details, dict):
        return dict(details)
    return {}


def _canonical_json(value: object) -> str:
    """Serialize JSON audit fields deterministically."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _curation_ledger_error_response(
    *,
    error_code: str,
    mutation_event_id: int | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    """Map ledger startup errors into curation command envelopes."""
    mapped_code = {
        IDEMPOTENCY_KEY_REUSED: ErrorCode.IDEMPOTENCY_KEY_REUSED,
        MUTATION_IN_PROGRESS: ErrorCode.MUTATION_IN_PROGRESS,
        MUTATION_RECOVERY_REQUIRED: ErrorCode.MUTATION_RECOVERY_REQUIRED,
        MUTATION_RESUME_CONFLICT: ErrorCode.MUTATION_RESUME_CONFLICT,
    }.get(error_code, ErrorCode.MUTATION_FAILED)
    return error_envelope(
        command=AUTHORITY_CURATE_COMMAND,
        error=workbench_error(
            mapped_code,
            message="Authority curation mutation cannot start.",
            details={"mutation_event_id": mutation_event_id},
            remediation=["Inspect the mutation ledger before retrying curation."],
        ),
        correlation_id=correlation_id,
    )


def _running_curation_conflict_response(
    *,
    session: Session,
    request: AuthorityCurationRequest,
) -> dict[str, Any]:
    """Return structured active-curation conflict after mutex insert failure."""
    existing = session.exec(
        select(AuthorityCurationAttempt)
        .where(AuthorityCurationAttempt.project_id == request.project_id)
        .where(
            AuthorityCurationAttempt.source_authority_id
            == request.source_authority_id
        )
        .where(AuthorityCurationAttempt.status == "running")
        .order_by(
            cast("Any", AuthorityCurationAttempt.created_at).desc(),
            cast("Any", AuthorityCurationAttempt.curation_row_id).desc(),
        )
    ).first()
    details: dict[str, Any] = {
        "project_id": request.project_id,
        "source_authority_id": request.source_authority_id,
    }
    if existing is not None:
        details.update(
            {
                "curation_attempt_id": existing.curation_attempt_id,
                "feedback_attempt_id": existing.feedback_attempt_id,
            }
        )
    return error_envelope(
        command=AUTHORITY_CURATE_COMMAND,
        error=workbench_error(
            ErrorCode.MUTATION_IN_PROGRESS,
            message="Authority curation is already running.",
            details=details,
            remediation=["Inspect authority status or the mutation ledger."],
        ),
        correlation_id=request.correlation_id,
    )


def _stale_setup_status_error(
    *,
    request: AuthorityCurationRequest,
    message: str,
    actual_fsm_state: object,
    actual_setup_status: object,
    setup_curation_mutation_event_id: object,
) -> dict[str, Any]:
    """Return stale setup status error for curation guards."""
    return error_envelope(
        command=AUTHORITY_CURATE_COMMAND,
        error=workbench_error(
            ErrorCode.STALE_SETUP_STATUS,
            message=message,
            details={
                "project_id": request.project_id,
                "expected_fsm_state": "SETUP_REQUIRED",
                "expected_setup_status": "authority_rejected",
                "actual_fsm_state": actual_fsm_state,
                "actual_setup_status": actual_setup_status,
                "setup_curation_mutation_event_id": (
                    setup_curation_mutation_event_id
                ),
            },
        ),
        correlation_id=request.correlation_id,
    )


def _finalize_mutation_status(
    *,
    engine: Engine,
    mutation_event_id: int | None,
    lease_owner: str,
    status: MutationStatus,
    response: dict[str, Any],
) -> bool:
    """Persist a terminal non-success ledger response while lease is active."""
    if mutation_event_id is None:
        return False
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as session:
        result = session.exec(
            update(CliMutationLedger)
            .where(_LEDGER_MUTATION_EVENT_ID == mutation_event_id)
            .where(_LEDGER_STATUS == MutationStatus.PENDING.value)
            .where(_LEDGER_LEASE_OWNER == lease_owner)
            .where(_LEDGER_LEASE_EXPIRES_AT > now)
            .values(
                status=status.value,
                response_json=json.dumps(
                    response,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                lease_owner=None,
                lease_acquired_at=None,
                last_heartbeat_at=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def _finalize_mutation_recovery_required(
    *,
    engine: Engine,
    mutation_event_id: int | None,
    lease_owner: str,
    response: dict[str, Any],
) -> bool:
    """Persist a recovery-required response while lease is active."""
    if mutation_event_id is None:
        return False
    now = datetime.now(UTC).replace(tzinfo=None)
    details = _response_error_details(response)
    with Session(engine) as session:
        result = session.exec(
            update(CliMutationLedger)
            .where(_LEDGER_MUTATION_EVENT_ID == mutation_event_id)
            .where(_LEDGER_STATUS == MutationStatus.PENDING.value)
            .where(_LEDGER_LEASE_OWNER == lease_owner)
            .where(_LEDGER_LEASE_EXPIRES_AT > now)
            .values(
                status=MutationStatus.RECOVERY_REQUIRED.value,
                after_json=_canonical_json(
                    {
                        "candidate_authority_id": details.get(
                            "candidate_authority_id"
                        ),
                        "candidate_authority_fingerprint": details.get(
                            "candidate_authority_fingerprint"
                        ),
                        "curation_attempt_id": details.get(
                            "curation_attempt_id"
                        ),
                    }
                ),
                response_json=_canonical_json(response),
                recovery_action=RecoveryAction.RECONCILE_THEN_RESUME.value,
                recovery_safe_to_auto_resume=False,
                last_error_json=_canonical_json(
                    {
                        "code": _response_error_code(response),
                        "details": details,
                    }
                ),
                lease_owner=None,
                lease_acquired_at=None,
                last_heartbeat_at=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def _stored_ledger_response(value: str | None) -> dict[str, Any] | None:
    """Return stored ledger response JSON for deterministic recovery replay."""
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _required_mutation_event_id(value: int | None) -> int:
    """Return a non-null mutation event id after ledger creation."""
    if value is None:
        message = "Mutation ledger row is missing mutation_event_id."
        raise RuntimeError(message)
    return value


def _run_async_task[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine from sync command code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return cast("T", future.result())


def _curation_model(model_id: str) -> BaseLlm:
    """Build the LiteLLM model wrapper used by authority curation."""
    lite_llm_module = importlib.import_module("google.adk.models.lite_llm")
    model_config_module = importlib.import_module("utils.model_config")
    runtime_config_module = importlib.import_module("utils.runtime_config")
    lite_llm = lite_llm_module.LiteLlm
    return cast(
        "BaseLlm",
        lite_llm(
            model=model_id,
            api_key=runtime_config_module.get_openrouter_api_key(),
            drop_params=True,
            extra_body=model_config_module.get_openrouter_extra_body(),
        ),
    )


def _authority_curation_model_id(request: AuthorityCurationRequest) -> str:
    """Return the requested curation model or the compiler default."""
    if request.compiler_model:
        return request.compiler_model
    model_config_module = importlib.import_module("utils.model_config")

    return str(model_config_module.get_model_id("spec_authority_compiler"))


async def _invoke_authority_curation_workflow_async(
    *,
    payload: dict[str, object],
    model_id: str,
) -> dict[str, Any]:
    """Invoke the ADK authority curation workflow and return final state."""
    curation_module = importlib.import_module(
        "orchestrator_agent.agent_tools.authority_curation"
    )
    runners_module = importlib.import_module("google.adk.runners")
    sessions_module = importlib.import_module("google.adk.sessions")
    genai_types_module = importlib.import_module("google.genai.types")
    validate_workflow_input = curation_module.validate_workflow_input
    build_authority_curation_workflow = (
        curation_module.build_authority_curation_workflow
    )
    validated_payload = validate_workflow_input(payload).model_dump(mode="json")
    workflow = build_authority_curation_workflow(model=_curation_model(model_id))
    app_name = "authority_curation"
    user_id = f"authority-curation-project-{validated_payload['project_id']}"
    session_service = sessions_module.InMemorySessionService()
    try:
        session = await session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            state={AUTHORITY_CURATION_STATE_INPUT: validated_payload},
        )
        runner = runners_module.Runner(
            node=workflow,
            app_name=app_name,
            session_service=session_service,
        )
        message: types.Content = genai_types_module.Content(
            role="user",
            parts=[
                genai_types_module.Part.from_text(
                    text=json.dumps(
                        validated_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            ],
        )
        events: list[Any] = []
        try:
            events.extend(
                [
                    event
                    async for event in runner.run_async(
                        user_id=user_id,
                        session_id=session.id,
                        new_message=message,
                    )
                ]
            )
        except Exception as exc:
            partial_output = extract_partial_response_text(events) or None
            raise AgentInvocationError(
                str(exc),
                partial_output=partial_output,
                event_count=len(events),
                validation_errors=_validation_errors_from_exception(exc),
            ) from exc

        updated_session = await session_service.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session.id,
        )
        state = dict(getattr(updated_session, "state", {}) or {})
        return {
            "final_text": extract_final_response_text(events),
            "state": state,
            "event_count": len(events),
            "model_info": get_agent_model_info(workflow),
        }
    finally:
        close = getattr(session_service, "close", None)
        if callable(close):
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result


def _invoke_authority_curation_workflow(
    *,
    payload: dict[str, object],
    model_id: str,
) -> dict[str, Any]:
    """Invoke the ADK workflow from synchronous mutation code."""
    return _run_async_task(
        _invoke_authority_curation_workflow_async(
            payload=payload,
            model_id=model_id,
        )
    )


def run_authority_curation_workflow(
    *,
    request: AuthorityCurationRequest,
    curation_attempt_id: str,
    source_authority_json: dict[str, Any],
    feedback_json: str,
) -> dict[str, Any]:
    """Run the ADK authority curation workflow and normalize its output."""
    feedback_payload = _json_object_from_value(feedback_json)
    if feedback_payload is None:
        return _curation_workflow_failure_result(
            request=request,
            curation_attempt_id=curation_attempt_id,
            failure=_CurationWorkflowFailure(
                error_code=ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID,
                failure_stage="feedback_payload_parse",
                failure_summary="Stored authority feedback JSON is invalid.",
                raw_output=feedback_json,
            ),
        )

    payload: dict[str, object] = {
        "project_id": request.project_id,
        "spec_version_id": request.spec_version_id,
        "source_authority_id": request.source_authority_id,
        "source_authority_fingerprint": (
            request.expected_source_authority_fingerprint
        ),
        "source_authority_json": source_authority_json,
        "feedback_json": feedback_payload,
        "max_iterations": request.max_iterations,
    }
    model_id = _authority_curation_model_id(request)
    try:
        invocation = _invoke_authority_curation_workflow(
            payload=payload,
            model_id=model_id,
        )
    except AgentInvocationError as exc:
        return _curation_workflow_failure_result(
            request=request,
            curation_attempt_id=curation_attempt_id,
            failure=_CurationWorkflowFailure(
                error_code=ErrorCode.SPEC_COMPILE_FAILED,
                failure_stage="adk_invocation_failed",
                failure_summary="Authority curation ADK workflow failed.",
                raw_output=exc.partial_output,
                model_info={"model_id": model_id},
                validation_errors=exc.validation_errors,
            ),
        )

    state = _json_object_from_value(invocation.get("state")) or {}
    repair_output = _json_object_from_value(
        state.get(AUTHORITY_CURATION_STATE_REPAIR_OUTPUT)
    )
    gate = _json_object_from_value(state.get(AUTHORITY_CURATION_STATE_GATE))
    if gate is None:
        gate = parse_json_payload(str(invocation.get("final_text") or ""))
    if gate is None:
        return _curation_workflow_failure_result(
            request=request,
            curation_attempt_id=curation_attempt_id,
            failure=_CurationWorkflowFailure(
                error_code=ErrorCode.SPEC_COMPILE_FAILED,
                failure_stage="adk_gate_missing",
                failure_summary=(
                    "Authority curation workflow returned no gate decision."
                ),
                raw_output=str(invocation.get("final_text") or ""),
                model_info=_model_info_with_requested_model(invocation, model_id),
            ),
        )

    status = gate.get("status")
    if status == "pass" and isinstance(repair_output, dict):
        candidate = _json_object_from_value(
            repair_output.get("candidate_authority_json")
        )
        if candidate is not None:
            return {
                "ok": True,
                "curation_attempt_id": curation_attempt_id,
                "project_id": request.project_id,
                "candidate_authority_json": candidate,
                "quality_report": _curation_quality_report(
                    invocation=invocation,
                    state=state,
                    gate=gate,
                    repair_output=repair_output,
                ),
                "candidate_lineage_json": {
                    "source_authority_id": request.source_authority_id,
                    "curation_attempt_id": curation_attempt_id,
                    "resolved_feedback_ids": repair_output.get(
                        "resolved_feedback_ids",
                        [],
                    ),
                    "unresolved_feedback_ids": repair_output.get(
                        "unresolved_feedback_ids",
                        [],
                    ),
                },
            }

    return _curation_workflow_failure_result(
        request=request,
        curation_attempt_id=curation_attempt_id,
        failure=_CurationWorkflowFailure(
            error_code=_curation_failure_code(gate=gate),
            failure_stage="adk_gate_failed",
            failure_summary=_curation_failure_summary(gate=gate),
            raw_output=str(invocation.get("final_text") or ""),
            model_info=_model_info_with_requested_model(invocation, model_id),
            extra={
                "gate": gate,
                "repair_output": repair_output,
                "event_count": invocation.get("event_count"),
            },
        ),
    )


def _validation_errors_from_exception(
    exc: BaseException,
) -> list[dict[str, Any]] | None:
    """Return Pydantic-style validation errors from an exception chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        errors = getattr(current, "errors", None)
        if callable(errors):
            try:
                raw_errors = errors()
            except TypeError:
                raw_errors = None
            if isinstance(raw_errors, list):
                return cast("list[dict[str, Any]]", raw_errors)
        current = current.__cause__ or current.__context__
    return None


def _model_info_with_requested_model(
    invocation: dict[str, Any],
    model_id: str,
) -> dict[str, Any]:
    """Return invocation model info with the requested model id preserved."""
    raw = invocation.get("model_info")
    model_info = dict(raw) if isinstance(raw, dict) else {}
    model_info["requested_model_id"] = model_id
    return model_info


def _curation_quality_report(
    *,
    invocation: dict[str, Any],
    state: dict[str, Any],
    gate: dict[str, Any],
    repair_output: dict[str, Any],
) -> dict[str, Any]:
    """Return compact audit metadata from ADK curation state."""
    return {
        "status": "passed",
        "event_count": invocation.get("event_count"),
        "semantic_findings": state.get(AUTHORITY_CURATION_STATE_SEMANTIC_FINDINGS),
        "quality_findings": state.get(AUTHORITY_CURATION_STATE_QUALITY_FINDINGS),
        "repair_plan": state.get(AUTHORITY_CURATION_STATE_REPAIR_PLAN),
        "repair_output": repair_output,
        "gate": gate,
    }


def _curation_failure_code(*, gate: dict[str, Any]) -> ErrorCode:
    """Return a registered error code for a failed gate result."""
    if gate.get("status") == "retry":
        return ErrorCode.AUTHORITY_CURATION_MAX_ITERATIONS
    return ErrorCode.SPEC_COMPILE_FAILED


def _curation_failure_summary(*, gate: dict[str, Any]) -> str:
    """Return a bounded failure summary for curation gate failures."""
    reason = gate.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    if gate.get("status") == "retry":
        return "Authority curation reached the maximum iteration count."
    return "Authority curation did not produce an acceptable candidate."


def _curation_workflow_failure_result(
    *,
    request: AuthorityCurationRequest,
    curation_attempt_id: str,
    failure: _CurationWorkflowFailure,
) -> dict[str, Any]:
    """Persist an authority curation failure artifact and return workflow failure."""
    artifact = write_failure_artifact(
        phase=AUTHORITY_CURATION_FAILURE_PHASE,
        project_id=request.project_id,
        failure_stage=failure.failure_stage,
        failure_summary=failure.failure_summary,
        raw_output=failure.raw_output,
        context={
            "curation_attempt_id": curation_attempt_id,
            "spec_version_id": request.spec_version_id,
            "source_authority_id": request.source_authority_id,
            "feedback_attempt_id": request.feedback_attempt_id,
        },
        model_info=failure.model_info,
        validation_errors=failure.validation_errors,
        extra=failure.extra,
    )
    metadata = artifact["metadata"]
    return {
        "status": "failed",
        "error_code": failure.error_code.value,
        "failure_artifact_id": metadata["failure_artifact_id"],
        "failure_summary": metadata["failure_summary"],
    }


def _commit_feedback_attempt(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
    request_hash: str,
    row: AuthorityFeedbackAttempt,
) -> dict[str, Any] | None:
    """Commit a feedback row, replaying durable idempotency conflicts."""
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        replay = _idempotency_replay(
            session=session,
            request=request,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        return error_envelope(
            command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
            error=workbench_error(
                ErrorCode.MUTATION_FAILED,
                message="Authority feedback record conflicted during commit.",
                details={"idempotency_key": request.idempotency_key},
            ),
            correlation_id=request.correlation_id,
        )
    return None


def _authority_guard(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
) -> _AuthorityGuardResult:
    """Load authority and validate ownership plus expected fingerprint."""
    authority = session.get(CompiledSpecAuthority, request.pending_authority_id)
    if authority is None:
        return _AuthorityGuardResult(
            authority=None,
            authority_fingerprint=None,
            error=_authority_not_pending_error(
                request=request,
                message="Pending authority was not found.",
                details={"authority_id": request.pending_authority_id},
            ),
        )

    spec = session.get(SpecRegistry, authority.spec_version_id)
    authority_project_id = spec.product_id if spec is not None else None
    if authority_project_id != request.project_id:
        return _AuthorityGuardResult(
            authority=None,
            authority_fingerprint=None,
            error=_authority_not_pending_error(
                request=request,
                message="Pending authority does not belong to project.",
                details={
                    "project_id": request.project_id,
                    "authority_id": request.pending_authority_id,
                    "authority_project_id": authority_project_id,
                },
            ),
        )

    actual_fingerprint = pending_authority_fingerprint(authority)
    if actual_fingerprint != request.expected_authority_fingerprint:
        return _AuthorityGuardResult(
            authority=None,
            authority_fingerprint=None,
            error=error_envelope(
                command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
                error=workbench_error(
                    ErrorCode.STALE_AUTHORITY_VERSION,
                    message="Authority fingerprint changed.",
                    details={
                        "expected": request.expected_authority_fingerprint,
                        "actual": actual_fingerprint,
                    },
                ),
                correlation_id=request.correlation_id,
            ),
        )
    return _AuthorityGuardResult(
        authority=authority,
        authority_fingerprint=actual_fingerprint,
        error=None,
    )


def _authority_not_pending_error(
    *,
    request: AuthorityFeedbackRecordRequest,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Return an authority-not-pending error envelope."""
    return error_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_NOT_PENDING,
            message=message,
            details=details,
        ),
        correlation_id=request.correlation_id,
    )


def _feedback_target_error(
    *,
    feedback: AuthorityFeedbackFile,
    authority: CompiledSpecAuthority,
) -> WorkbenchError | None:
    """Return the first target validation error for feedback."""
    targets = _authority_targets_by_kind(authority)
    for item in feedback.feedback_items:
        target_error = _validate_feedback_target(item=item, targets=targets)
        if target_error is not None:
            return target_error
    return None


def _build_feedback_attempt(
    *,
    request: AuthorityFeedbackRecordRequest,
    actual_fingerprint: str,
    feedback: AuthorityFeedbackFile,
    feedback_fingerprint: str,
    request_hash: str,
) -> AuthorityFeedbackAttempt:
    """Build a feedback attempt row."""
    payload = feedback.model_dump(mode="json")
    now = datetime.now(UTC)
    return AuthorityFeedbackAttempt(
        project_id=request.project_id,
        feedback_attempt_id=f"feedback-{uuid4()}",
        source_authority_id=request.pending_authority_id,
        source_authority_fingerprint=actual_fingerprint,
        feedback_fingerprint=feedback_fingerprint,
        has_blocking_feedback=any(
            item.severity == "blocking" for item in feedback.feedback_items
        ),
        feedback_json=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        request_hash=request_hash,
        idempotency_key=request.idempotency_key,
        changed_by=request.changed_by,
        created_at=now,
        updated_at=now,
    )


def _load_feedback_file(path: str) -> AuthorityFeedbackFile | dict[str, Any]:
    """Load and validate a feedback file from disk."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return AuthorityFeedbackFile.model_validate(payload)
    except ValidationError as exc:
        return _feedback_schema_invalid(
            message="Authority feedback payload is invalid.",
            details={"validation_errors": _validation_error_details(exc)},
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _feedback_schema_invalid(
            message="Authority feedback payload is invalid.",
            details={"error": str(exc)},
        )


def _feedback_schema_invalid(
    *,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Return a structured invalid feedback payload error."""
    return error_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID,
            message=message,
            details=details,
        ),
    )


def _validation_error_details(exc: ValidationError) -> list[dict[str, Any]]:
    """Return Pydantic validation errors without raw input values."""
    return cast(
        "list[dict[str, Any]]",
        exc.errors(
            include_input=False,
            include_context=False,
            include_url=False,
        ),
    )


def _idempotency_replay(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
    request_hash: str,
) -> dict[str, Any] | None:
    """Return replay/conflict envelope for an existing idempotency key."""
    existing = session.exec(
        select(AuthorityFeedbackAttempt)
        .where(AuthorityFeedbackAttempt.project_id == request.project_id)
        .where(AuthorityFeedbackAttempt.idempotency_key == request.idempotency_key)
    ).first()
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        return error_envelope(
            command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
            error=workbench_error(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                message="Idempotency key was reused with a different request.",
                details={"idempotency_key": request.idempotency_key},
            ),
            correlation_id=request.correlation_id,
        )
    return success_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        data=_feedback_attempt_response(existing),
        correlation_id=request.correlation_id,
    )


def _feedback_attempt_response(row: AuthorityFeedbackAttempt) -> dict[str, Any]:
    """Return the feedback record success payload for a stored row."""
    return {
        "status": "authority_feedback_recorded",
        "project_id": row.project_id,
        "feedback_attempt_id": row.feedback_attempt_id,
        "source_authority_id": row.source_authority_id,
        "source_authority_fingerprint": row.source_authority_fingerprint,
        "feedback_fingerprint": row.feedback_fingerprint,
        "has_blocking_feedback": row.has_blocking_feedback,
    }


def _authority_targets_by_kind(authority: CompiledSpecAuthority) -> TargetIndex:
    """Return known target ids grouped by feedback target kind."""
    compiled = _json_from_column(authority.compiled_artifact_json)
    invariant_json = _json_from_column(authority.invariants)
    gap_json = _json_from_column(authority.spec_gaps)
    targets: TargetIndex = {
        "invariant": set(),
        "gap": set(),
        "assumption": set(),
        "quality_group": set(),
        "source_item": set(),
        "authority_candidate": {f"authority:{authority.authority_id}"},
    }
    targets["invariant"].update(
        _collect_ids_from_paths(
            [invariant_json, _dict_value(compiled, "invariants")],
            keys=("id", "invariant_id"),
        )
    )
    targets["gap"].update(
        _collect_ids_from_paths(
            [gap_json, _dict_value(compiled, "gaps")],
            keys=("id", "gap_id"),
        )
    )
    targets["assumption"].update(
        _collect_ids_from_paths(
            [_dict_value(compiled, "assumptions")],
            keys=("id", "assumption_id"),
        )
    )
    targets["quality_group"].update(
        _collect_ids_from_paths(
            [
                _dict_value(compiled, "quality_groups"),
                _dict_value(compiled, "review_groups"),
            ],
            keys=("id", "group_id"),
        )
    )
    targets["source_item"].update(_collect_source_item_ids(invariant_json))
    targets["source_item"].update(_collect_source_item_ids(compiled))
    return targets


def _json_from_column(raw_value: object) -> object:
    """Parse JSON stored in an authority text column."""
    if not raw_value:
        return None
    try:
        return json.loads(str(raw_value))
    except json.JSONDecodeError:
        return None


def _dict_value(value: object, key: str) -> object:
    """Return a dictionary value when the parsed JSON is an object."""
    if isinstance(value, dict):
        parsed = {str(item_key): item for item_key, item in value.items()}
        return parsed.get(key)
    return None


def _collect_ids_from_paths(
    values: list[object],
    *,
    keys: tuple[str, ...],
) -> set[str]:
    """Collect target ids from selected JSON branches."""
    found: set[str] = set()
    for value in values:
        found.update(_collect_ids(value, keys=keys))
    return found


def _validate_feedback_target(
    *,
    item: AuthorityFeedbackItem,
    targets: TargetIndex,
) -> WorkbenchError | None:
    """Validate target_id and source_item_id against kind-scoped target ids."""
    if item.target_id is not None and item.target_id not in targets[item.target_kind]:
        return workbench_error(
            ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND,
            message="Feedback target does not exist.",
            details={
                "target_kind": item.target_kind,
                "target_id": item.target_id,
            },
        )
    if (
        item.source_item_id is not None
        and item.source_item_id not in targets["source_item"]
    ):
        return workbench_error(
            ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND,
            message="Feedback source item does not exist.",
            details={"source_item_id": item.source_item_id},
        )
    return None


def _collect_ids(value: object, *, keys: tuple[str, ...]) -> set[str]:
    """Collect id-like strings from nested JSON."""
    found: set[str] = set()
    if isinstance(value, dict):
        parsed = {str(item_key): item for item_key, item in value.items()}
        for key in keys:
            item_id = parsed.get(key)
            if isinstance(item_id, str) and item_id:
                found.add(item_id)
        for child in parsed.values():
            found.update(_collect_ids(child, keys=keys))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_ids(child, keys=keys))
    return found


def _collect_source_item_ids(value: object) -> set[str]:
    """Collect source item ids from nested authority JSON."""
    found: set[str] = set()
    if isinstance(value, dict):
        parsed = {str(item_key): item for item_key, item in value.items()}
        found.update(_collect_direct_source_ids(parsed))
        source_map = parsed.get("source_map")
        if isinstance(source_map, list):
            for source_entry in source_map:
                found.update(_collect_source_map_ids(source_entry))
        found.update(_collect_child_source_ids(parsed))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_source_item_ids(child))
    return found


def _collect_direct_source_ids(value: dict[str, object]) -> set[str]:
    """Collect source item ids from direct source-related fields."""
    found: set[str] = set()
    for key in ("source_item_id", "spec_item_id", "item_id", "source_id"):
        item_id = value.get(key)
        if isinstance(item_id, str) and _looks_like_source_item_id(item_id):
            found.add(item_id)
    for key in ("location", "locations", "source_ref"):
        found.update(_collect_source_location_ids(value.get(key)))
    return found


def _collect_child_source_ids(value: dict[str, object]) -> set[str]:
    """Collect source item ids from child nodes except source_map."""
    found: set[str] = set()
    for child_key, child in value.items():
        if child_key != "source_map":
            found.update(_collect_source_item_ids(child))
    return found


def _collect_source_map_ids(value: object) -> set[str]:
    """Collect source item ids from source_map entries."""
    found: set[str] = set()
    if isinstance(value, dict):
        parsed = {str(item_key): item for item_key, item in value.items()}
        for key in ("source_item_id", "spec_item_id", "item_id", "source_id", "id"):
            item_id = parsed.get(key)
            if isinstance(item_id, str) and _looks_like_source_item_id(item_id):
                found.add(item_id)
        for key in ("location", "locations", "source_ref"):
            found.update(_collect_source_location_ids(parsed.get(key)))
    return found


def _collect_source_location_ids(value: object) -> set[str]:
    """Collect source item ids from location values."""
    if isinstance(value, str) and _looks_like_source_item_id(value):
        return {value}
    if isinstance(value, list):
        return {
            item
            for item in value
            if isinstance(item, str) and _looks_like_source_item_id(item)
        }
    return set()


def _looks_like_source_item_id(value: str) -> bool:
    """Return whether a string looks like an AgileForge spec item id."""
    prefixes = (
        "SRC",
        "SPEC.",
        "REQ",
        "DECISION",
        "NON_GOAL",
        "RISK",
        "OPEN_QUESTION",
    )
    return value.startswith(prefixes)


def _request_hash(
    *,
    request: AuthorityFeedbackRecordRequest,
    feedback_fingerprint: str,
) -> str:
    """Return the stable request hash for feedback recording."""
    return canonical_hash(
        {
            "command": AUTHORITY_FEEDBACK_RECORD_COMMAND,
            "project_id": request.project_id,
            "pending_authority_id": request.pending_authority_id,
            "expected_authority_fingerprint": request.expected_authority_fingerprint,
            "feedback_fingerprint": feedback_fingerprint,
            "changed_by": request.changed_by,
        }
    )
