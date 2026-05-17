# services/agent_workbench/authority_decision.py
"""Guarded authority accept/reject mutation service."""

# ruff: noqa: C901, PLR0911, PLR0912, PLR0913

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4
from weakref import WeakKeyDictionary

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models import db as model_db
from models.agent_workbench import CliMutationLedger
from models.specs import SpecAuthorityAcceptance
from services.agent_workbench.authority_review import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot,
)
from services.agent_workbench.envelope import error_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    DEFAULT_LEASE_SECONDS,
    MUTATION_IN_PROGRESS,
    MUTATION_RECOVERY_REQUIRED,
    MUTATION_RESUME_CONFLICT,
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
    _db_datetime,
    _json_dump,
    _json_load,
)
from services.agent_workbench.schema_readiness import (
    check_authority_decision_readiness,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Engine

JsonDict = dict[str, Any]

AUTHORITY_DECISION_COMMAND = "agileforge authority decision"
AUTHORITY_ACCEPT_COMMAND = "agileforge authority accept"
AUTHORITY_REJECT_COMMAND = "agileforge authority reject"
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_KEY_MIN_LENGTH = 8
_KEY_MAX_LENGTH = 128
_ENGINE_LOCKS: WeakKeyDictionary[Engine, RLock] = WeakKeyDictionary()
_ENGINE_LOCKS_GUARD = RLock()
_ACCEPTANCE_STATUS: Any = SpecAuthorityAcceptance.status
_LEDGER_MUTATION_EVENT_ID: Any = CliMutationLedger.mutation_event_id
_LEDGER_STATUS: Any = CliMutationLedger.status
_LEDGER_LEASE_OWNER: Any = CliMutationLedger.lease_owner


class AuthorityDecisionWorkflowPort(Protocol):
    """Workflow operations used by authority decisions."""

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current workflow session state."""
        raise NotImplementedError

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge a workflow session state update."""
        raise NotImplementedError


class SyncAuthorityDecisionWorkflowAdapter:
    """Synchronous adapter over the default workflow service."""

    def __init__(self) -> None:
        """Initialize the adapter lazily to avoid importing workflow at module load."""
        from services.workflow import WorkflowService  # noqa: PLC0415

        self._workflow = WorkflowService()

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current workflow session state."""
        return self._workflow.get_session_status(session_id)

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge a workflow session state update."""
        self._workflow.update_session_status(session_id, partial_update)


class AuthorityDecisionBase(BaseModel):
    """Common guarded authority decision request fields."""

    project_id: int
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
    idempotency_key: str | None = None
    changed_by: str | None = None
    actor_mode: str = "cli-agent"
    policy: str = "agent_requested"
    correlation_id: str | None = None

    @model_validator(mode="after")
    def _validate_idempotency_mode(self) -> AuthorityDecisionBase:
        if self.idempotency_key is None:
            if self.review_token is not None and self.actor_mode == "human":
                self.idempotency_key = f"human-token:{uuid4()}"
                _validate_token(name="idempotency_key", value=self.idempotency_key)
                return self
            msg = "idempotency_key is required for non-dry-run agent mutations"
            raise ValueError(msg)
        _validate_token(name="idempotency_key", value=self.idempotency_key)
        return self


class IncompleteReviewOverride(BaseModel):
    """Candidate-scoped incomplete review override request."""

    candidate_id: str = Field(min_length=1)
    finding_code: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class AuthorityAcceptRequest(AuthorityDecisionBase):
    """Guarded authority acceptance request."""

    allow_incomplete_review: bool = False
    incomplete_review_rationale: str | None = None
    incomplete_review_overrides: list[IncompleteReviewOverride] = Field(
        default_factory=list
    )


class AuthorityRejectRequest(AuthorityDecisionBase):
    """Guarded authority rejection request."""

    reason: str = Field(min_length=1)


@dataclass(frozen=True)
class ReviewedAuthoritySnapshot:
    """Immutable normalized guard snapshot used by decision mutations."""

    project_id: int
    pending_authority_id: int
    authority_fingerprint: str
    source_spec_hash: str
    disk_spec_hash: str
    resolved_spec_path: str
    compiler_version: str
    prompt_hash: str
    fsm_state: str
    setup_status: str
    content_included: bool
    omission_assessment: str
    coverage_summary_fingerprint: str
    review_token: str | None
    spec_version_id: int
    review_findings: list[JsonDict]
    ir_packet_limits: JsonDict


class AuthorityDecisionRunner:
    """Run guarded authority accept/reject decisions."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        workflow: AuthorityDecisionWorkflowPort | None = None,
    ) -> None:
        """Initialize the runner with storage and workflow ports."""
        self._engine: Engine = engine or model_db.get_engine()
        self._ledger = MutationLedgerRepository(engine=self._engine)
        self._workflow = workflow or SyncAuthorityDecisionWorkflowAdapter()
        self._lease_seconds = DEFAULT_LEASE_SECONDS
        self.fail_decision_progress_for_test: bool = False
        self.inject_terminal_conflict_after_precheck_for_test: bool = False

    def accept(self, request: AuthorityAcceptRequest) -> JsonDict:
        """Accept the currently reviewed pending authority."""
        with _decision_lock(self._engine):
            return self._run(decision="accept", request=request)

    def reject(self, request: AuthorityRejectRequest) -> JsonDict:
        """Reject the currently reviewed pending authority."""
        with _decision_lock(self._engine):
            return self._run(decision="reject", request=request)

    def _run(
        self,
        *,
        decision: Literal["accept", "reject"],
        request: AuthorityAcceptRequest | AuthorityRejectRequest,
    ) -> JsonDict:
        command = _command_for_decision(decision)
        schema_error = self._schema_error(command)
        if schema_error is not None:
            return schema_error

        request_hash = normalized_decision_request_hash(
            decision=decision,
            request=request,
        )
        idempotency_key = _required(request.idempotency_key)
        with _decision_lock(self._engine):
            loaded = self._ledger.create_or_load(
                command=AUTHORITY_DECISION_COMMAND,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                project_id=request.project_id,
                correlation_id=request.correlation_id or str(uuid4()),
                changed_by=request.changed_by or request.actor_mode,
                lease_owner=_lease_owner(
                    idempotency_key=idempotency_key,
                    correlation_id=request.correlation_id,
                ),
                now=_now(),
                lease_seconds=self._lease_seconds,
            )
            if loaded.response is not None:
                return loaded.response
            if loaded.error_code == MUTATION_RECOVERY_REQUIRED:
                return self._recover_from_decision_row(
                    decision=decision,
                    request=request,
                    mutation_event_id=_event_id(loaded.ledger),
                    command=command,
                )
            if loaded.error_code is not None:
                return _ledger_error(
                    command=command,
                    code=loaded.error_code,
                    mutation_event_id=_event_id(loaded.ledger),
                )

            if loaded.replayed or loaded.ledger.status != MutationStatus.PENDING.value:
                return _replay_terminal_non_success(
                    command=command,
                    row=loaded.ledger,
                )

            mutation_event_id = _event_id(loaded.ledger)
            lease_owner = _required(loaded.ledger.lease_owner)
            return self._execute_owned_decision(
                decision=decision,
                request=request,
                command=command,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
            )

    def _execute_owned_decision(
        self,
        *,
        decision: Literal["accept", "reject"],
        request: AuthorityAcceptRequest | AuthorityRejectRequest,
        command: str,
        mutation_event_id: int,
        lease_owner: str,
    ) -> JsonDict:
        replay_conflict = self._terminal_conflict_before_snapshot(
            request=request,
            command=command,
        )
        if replay_conflict is not None:
            if not self._finalize_validation_failed(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                response=replay_conflict,
            ):
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return replay_conflict

        snapshot_result = self._normalize_snapshot(request=request, command=command)
        if not isinstance(snapshot_result, ReviewedAuthoritySnapshot):
            snapshot_error = cast("JsonDict", snapshot_result)
            if not self._finalize_validation_failed(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                response=snapshot_error,
            ):
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return snapshot_error
        snapshot = snapshot_result

        guard_error = self._validate_guards(
            decision=decision,
            request=request,
            snapshot=snapshot,
            command=command,
        )
        if guard_error is not None:
            if not self._finalize_validation_failed(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                response=guard_error,
            ):
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return guard_error

        workflow_error = self._validate_current_workflow(
            snapshot=snapshot,
            command=command,
        )
        if workflow_error is not None:
            if not self._finalize_validation_failed(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                response=workflow_error,
            ):
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return workflow_error

        with _decision_lock(self._engine):
            terminal_error = self._terminal_conflict_for_snapshot(
                snapshot=snapshot,
                command=command,
            )
            if terminal_error is not None:
                if not self._finalize_validation_failed(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    response=terminal_error,
                ):
                    return _ledger_error(
                        command=command,
                        code=MUTATION_RESUME_CONFLICT,
                        mutation_event_id=mutation_event_id,
                    )
                return terminal_error

            if decision == "accept":
                incomplete_error = _accept_incomplete_error(
                    request=_cast_accept(request),
                    snapshot=snapshot,
                    command=command,
                )
                if incomplete_error is not None:
                    if not self._finalize_validation_failed(
                        mutation_event_id=mutation_event_id,
                        lease_owner=lease_owner,
                        response=incomplete_error,
                    ):
                        return _ledger_error(
                            command=command,
                            code=MUTATION_RESUME_CONFLICT,
                            mutation_event_id=mutation_event_id,
                        )
                    return incomplete_error

            recorded = self._record_terminal_decision(
                decision=decision,
                request=request,
                snapshot=snapshot,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                command=command,
            )
        if (
            isinstance(recorded, dict)
            and _first_error_code(recorded)
            == ErrorCode.AUTHORITY_ALREADY_DECIDED.value
            and not self._finalize_validation_failed(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                response=recorded,
            )
        ):
            return _ledger_error(
                command=command,
                code=MUTATION_RESUME_CONFLICT,
                mutation_event_id=mutation_event_id,
            )
        if isinstance(recorded, dict):
            return recorded

        try:
            self._write_workflow_state(
                decision=decision,
                snapshot=snapshot,
                request=request,
            )
        except Exception as exc:  # noqa: BLE001
            marked = self._mark_recovery_required_after_decision(
                command=command,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                error=exc,
            )
            if not marked:
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return _recovery_required_response(
                command=command,
                mutation_event_id=mutation_event_id,
                completed_step="decision_recorded",
                next_step="workflow_state_written",
            )

        response = _success(_response_data(row=recorded))
        if not self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=response["data"],
            response=response,
            now=_now(),
        ):
            return _ledger_error(
                command=command,
                code=MUTATION_RESUME_CONFLICT,
                mutation_event_id=mutation_event_id,
            )
        return response

    def _schema_error(self, command: str) -> JsonDict | None:
        readiness = check_authority_decision_readiness(self._engine)
        if readiness.ok:
            return None
        return error_envelope(
            command=command,
            error=workbench_error(
                ErrorCode.SCHEMA_NOT_READY,
                message=(
                    "Database schema is missing required authority decision "
                    "tables, columns, indexes, or storage version metadata."
                ),
                details={"missing": readiness.missing},
                remediation=["Run the database migration before accepting authority."],
            ),
        )

    def _normalize_snapshot(
        self,
        *,
        request: AuthorityDecisionBase,
        command: str,
    ) -> ReviewedAuthoritySnapshot | JsonDict:
        result = build_authority_review_snapshot(
            project_id=request.project_id,
            engine=self._engine,
        )
        if not isinstance(result, AuthorityReviewSnapshot):
            return _retarget_error(cast("JsonDict", result), command=command)
        raw = _snapshot_from_review(result)
        if (
            request.review_token is not None
            and request.review_token != result.review_token
        ):
            return _error(
                command=command,
                code=ErrorCode.STALE_ARTIFACT_FINGERPRINT,
                details={
                    "field": "review_token",
                    "expected": request.review_token,
                    "actual": result.review_token,
                },
                remediation=["Run authority review again and retry the decision."],
            )
        return raw

    def _validate_guards(
        self,
        *,
        decision: Literal["accept", "reject"],
        request: AuthorityAcceptRequest | AuthorityRejectRequest,
        snapshot: ReviewedAuthoritySnapshot,
        command: str,
    ) -> JsonDict | None:
        if request.review_token is None:
            missing = _missing_explicit_guards(
                request=request,
                require_completeness=decision == "accept",
            )
            if missing:
                return _error(
                    command=command,
                    code=ErrorCode.AUTHORITY_GUARD_INCOMPLETE,
                    details={"missing": missing},
                    remediation=[
                        "Provide a review token or every required explicit guard."
                    ],
                )

        comparisons: tuple[tuple[str, object | None, object], ...] = (
            (
                "pending_authority_id",
                request.pending_authority_id,
                snapshot.pending_authority_id,
            ),
            (
                "expected_authority_fingerprint",
                request.expected_authority_fingerprint,
                snapshot.authority_fingerprint,
            ),
            (
                "expected_source_spec_hash",
                request.expected_source_spec_hash,
                snapshot.source_spec_hash,
            ),
            (
                "expected_disk_spec_hash",
                request.expected_disk_spec_hash,
                snapshot.disk_spec_hash,
            ),
            (
                "expected_resolved_spec_path",
                request.expected_resolved_spec_path,
                snapshot.resolved_spec_path,
            ),
            ("expected_state", request.expected_state, snapshot.fsm_state),
            (
                "expected_setup_status",
                request.expected_setup_status,
                snapshot.setup_status,
            ),
            (
                "expected_content_included",
                request.expected_content_included,
                snapshot.content_included,
            ),
            (
                "expected_omission_assessment",
                request.expected_omission_assessment,
                snapshot.omission_assessment,
            ),
            (
                "expected_coverage_summary_fingerprint",
                request.expected_coverage_summary_fingerprint,
                snapshot.coverage_summary_fingerprint,
            ),
        )
        for field_name, expected, actual in comparisons:
            if expected is not None and expected != actual:
                return _stale_guard_error(
                    command=command,
                    field_name=field_name,
                    expected=expected,
                    actual=actual,
                )
        return None

    def _validate_current_workflow(
        self,
        *,
        snapshot: ReviewedAuthoritySnapshot,
        command: str,
    ) -> JsonDict | None:
        state = self._workflow.get_session_status(str(snapshot.project_id))
        if not state:
            return None
        actual_fsm_state = state.get("fsm_state")
        actual_setup_status = state.get("setup_status")
        if (
            actual_fsm_state in {None, snapshot.fsm_state}
            and actual_setup_status in {None, snapshot.setup_status}
        ):
            return None
        return _error(
            command=command,
            code=ErrorCode.STALE_STATE,
            details={
                "expected_state": snapshot.fsm_state,
                "actual_state": actual_fsm_state,
                "expected_setup_status": snapshot.setup_status,
                "actual_setup_status": actual_setup_status,
            },
            remediation=["Run authority review again from the current workflow state."],
        )

    def _terminal_conflict_before_snapshot(
        self,
        *,
        request: AuthorityDecisionBase,
        command: str,
    ) -> JsonDict | None:
        with Session(self._engine) as session:
            statement = select(SpecAuthorityAcceptance).where(
                SpecAuthorityAcceptance.product_id == request.project_id,
                _ACCEPTANCE_STATUS.in_(["accepted", "rejected"]),
            )
            if request.pending_authority_id is not None:
                statement = statement.where(
                    SpecAuthorityAcceptance.pending_authority_id
                    == request.pending_authority_id
                )
            elif request.review_token is not None:
                statement = statement.where(
                    SpecAuthorityAcceptance.review_token == request.review_token
                )
            else:
                return None
            row = session.exec(statement).first()
        if row is None:
            return None
        return _authority_already_decided_error(command=command, row=row)

    def _terminal_conflict_for_snapshot(
        self,
        *,
        snapshot: ReviewedAuthoritySnapshot,
        command: str,
    ) -> JsonDict | None:
        key = terminal_decision_key(
            project_id=snapshot.project_id,
            spec_version_id=snapshot.spec_version_id,
            pending_authority_id=snapshot.pending_authority_id,
        )
        with Session(self._engine) as session:
            row = session.exec(
                select(SpecAuthorityAcceptance).where(
                    SpecAuthorityAcceptance.terminal_decision_key == key
                )
            ).first()
        if row is None:
            return None
        return _authority_already_decided_error(command=command, row=row)

    def _record_terminal_decision(
        self,
        *,
        decision: Literal["accept", "reject"],
        request: AuthorityAcceptRequest | AuthorityRejectRequest,
        snapshot: ReviewedAuthoritySnapshot,
        mutation_event_id: int,
        lease_owner: str,
        command: str,
    ) -> SpecAuthorityAcceptance | JsonDict:
        now = _now()
        terminal_key = terminal_decision_key(
            project_id=snapshot.project_id,
            spec_version_id=snapshot.spec_version_id,
            pending_authority_id=snapshot.pending_authority_id,
        )
        with Session(self._engine) as session:
            if not self._ledger.require_active_owner(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                now=now,
                lease_seconds=self._lease_seconds,
            ):
                return _ledger_error(
                    command=command,
                    code=MUTATION_IN_PROGRESS,
                    mutation_event_id=mutation_event_id,
                )
            if self.inject_terminal_conflict_after_precheck_for_test:
                self._insert_terminal_conflict_for_test(snapshot=snapshot)
            row = SpecAuthorityAcceptance(
                product_id=snapshot.project_id,
                spec_version_id=snapshot.spec_version_id,
                status="accepted" if decision == "accept" else "rejected",
                policy=request.policy,
                decided_by=request.changed_by or request.actor_mode,
                decided_at=now,
                rationale=_rationale(decision=decision, request=request),
                compiler_version=snapshot.compiler_version,
                prompt_hash=snapshot.prompt_hash,
                spec_hash=_stored_spec_hash(snapshot.source_spec_hash),
                pending_authority_id=snapshot.pending_authority_id,
                authority_fingerprint=snapshot.authority_fingerprint,
                review_token=snapshot.review_token,
                review_fingerprint=snapshot.coverage_summary_fingerprint,
                disk_spec_hash=snapshot.disk_spec_hash,
                resolved_spec_path=snapshot.resolved_spec_path,
                actor_mode=request.actor_mode,
                review_completeness=snapshot.omission_assessment,
                incomplete_review_override=(
                    isinstance(request, AuthorityAcceptRequest)
                    and (
                        request.allow_incomplete_review
                        or bool(request.incomplete_review_overrides)
                    )
                ),
                incomplete_review_rationale=(
                    request.incomplete_review_rationale
                    if isinstance(request, AuthorityAcceptRequest)
                    else None
                ),
                incomplete_review_overrides_json=(
                    _persisted_incomplete_review_overrides_json(
                        request,
                        snapshot=snapshot,
                        recorded_at=now,
                    )
                    if isinstance(request, AuthorityAcceptRequest)
                    else None
                ),
                terminal_decision_key=terminal_key,
                provenance_source=(
                    "review_token" if request.review_token is not None else "explicit"
                ),
            )
            session.add(row)
            try:
                session.flush()
                if self.fail_decision_progress_for_test:
                    session.rollback()
                    return _ledger_error(
                        command=command,
                        code=MUTATION_RESUME_CONFLICT,
                        mutation_event_id=mutation_event_id,
                    )
                if not MutationLedgerRepository.mark_step_complete_in_session(
                    session,
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    step="decision_recorded",
                    next_step="workflow_state_written",
                    now=now,
                ):
                    session.rollback()
                    return _ledger_error(
                        command=command,
                        code=MUTATION_RESUME_CONFLICT,
                        mutation_event_id=mutation_event_id,
                    )
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.exec(
                    select(SpecAuthorityAcceptance).where(
                        SpecAuthorityAcceptance.terminal_decision_key == terminal_key
                    )
                ).first()
                if existing is not None:
                    return _authority_already_decided_error(
                        command=command,
                        row=existing,
                    )
                raise
            session.refresh(row)
            return row

    def _insert_terminal_conflict_for_test(
        self,
        *,
        snapshot: ReviewedAuthoritySnapshot,
    ) -> None:
        key = terminal_decision_key(
            project_id=snapshot.project_id,
            spec_version_id=snapshot.spec_version_id,
            pending_authority_id=snapshot.pending_authority_id,
        )
        with Session(self._engine) as session:
            if session.exec(
                select(SpecAuthorityAcceptance).where(
                    SpecAuthorityAcceptance.terminal_decision_key == key
                )
            ).first():
                return
            row = SpecAuthorityAcceptance(
                product_id=snapshot.project_id,
                spec_version_id=snapshot.spec_version_id,
                status="rejected",
                policy="test",
                decided_by="external-conflict",
                decided_at=_now(),
                rationale="Injected terminal conflict.",
                compiler_version=snapshot.compiler_version,
                prompt_hash=snapshot.prompt_hash,
                spec_hash=_stored_spec_hash(snapshot.source_spec_hash),
                pending_authority_id=snapshot.pending_authority_id,
                authority_fingerprint=snapshot.authority_fingerprint,
                review_token="injected-conflict",  # noqa: S106  # nosec B106
                review_fingerprint=snapshot.coverage_summary_fingerprint,
                disk_spec_hash=snapshot.disk_spec_hash,
                resolved_spec_path=snapshot.resolved_spec_path,
                actor_mode="test",
                review_completeness=snapshot.omission_assessment,
                terminal_decision_key=key,
            )
            session.add(row)
            session.commit()

    def _write_workflow_state(
        self,
        *,
        decision: Literal["accept", "reject"],
        snapshot: ReviewedAuthoritySnapshot,
        request: AuthorityAcceptRequest | AuthorityRejectRequest,
    ) -> None:
        if decision == "accept":
            self._workflow.update_session_status(
                str(snapshot.project_id),
                {
                    "setup_status": "passed",
                    "fsm_state": "VISION_INTERVIEW",
                    "setup_error": None,
                    "setup_error_code": None,
                },
            )
            return
        self._workflow.update_session_status(
            str(snapshot.project_id),
            {
                "setup_status": "authority_rejected",
                "fsm_state": "SETUP_REQUIRED",
                "setup_error": _cast_reject(request).reason,
                "setup_error_code": "AUTHORITY_REJECTED",
            },
        )

    def _mark_recovery_required_after_decision(
        self,
        *,
        command: str,
        mutation_event_id: int,
        lease_owner: str,
        error: Exception,
        code: str | ErrorCode = ErrorCode.WORKFLOW_SESSION_FAILED,
    ) -> bool:
        del command
        return self._ledger.mark_recovery_required(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            recovery_action=RecoveryAction.RESUME_FROM_STEP,
            safe_to_auto_resume=True,
            last_error={
                "code": str(code.value if isinstance(code, ErrorCode) else code),
                "message": str(error),
                "completed_step": "decision_recorded",
                "next_step": "workflow_state_written",
            },
            now=_now(),
        )

    def _recover_from_decision_row(
        self,
        *,
        decision: Literal["accept", "reject"],
        request: AuthorityAcceptRequest | AuthorityRejectRequest,
        mutation_event_id: int,
        command: str,
    ) -> JsonDict:
        recovery_owner = _recovery_lease_owner(
            idempotency_key=_required(request.idempotency_key),
            correlation_id=request.correlation_id,
        )
        acquired = self._ledger.acquire_resume_lease(
            mutation_event_id=mutation_event_id,
            lease_owner=recovery_owner,
            now=_now(),
            lease_seconds=self._lease_seconds,
        )
        if acquired.error_code is not None:
            return _ledger_error(
                command=command,
                code=acquired.error_code,
                mutation_event_id=mutation_event_id,
            )
        row = self._find_recoverable_decision(request=request)
        if row is None:
            marked = self._mark_recovery_required_after_decision(
                command=command,
                mutation_event_id=mutation_event_id,
                lease_owner=recovery_owner,
                error=RuntimeError("Recoverable authority decision row was missing."),
                code=MUTATION_RECOVERY_REQUIRED,
            )
            if not marked:
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return _recovery_required_response(
                command=command,
                mutation_event_id=mutation_event_id,
                completed_step="decision_recorded",
                next_step="workflow_state_written",
            )
        try:
            self._write_workflow_state_from_row(row)
        except Exception as exc:  # noqa: BLE001
            marked = self._mark_recovery_required_after_decision(
                command=command,
                mutation_event_id=mutation_event_id,
                lease_owner=recovery_owner,
                error=exc,
            )
            if not marked:
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return _recovery_required_response(
                command=command,
                mutation_event_id=mutation_event_id,
                completed_step="decision_recorded",
                next_step="workflow_state_written",
            )
        if _decision_from_status(row.status) != decision:
            response = _authority_already_decided_error(command=command, row=row)
            marked = self._mark_recovery_required_after_decision(
                command=command,
                mutation_event_id=mutation_event_id,
                lease_owner=recovery_owner,
                error=RuntimeError("Recoverable authority decision status mismatch."),
                code=ErrorCode.AUTHORITY_ALREADY_DECIDED,
            )
            if not marked:
                return _ledger_error(
                    command=command,
                    code=MUTATION_RESUME_CONFLICT,
                    mutation_event_id=mutation_event_id,
                )
            return response
        response = _success(_response_data(row=row))
        if not self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=recovery_owner,
            after=response["data"],
            response=response,
            now=_now(),
        ):
            return _ledger_error(
                command=command,
                code=MUTATION_RESUME_CONFLICT,
                mutation_event_id=mutation_event_id,
            )
        return response

    def _find_recoverable_decision(
        self,
        *,
        request: AuthorityDecisionBase,
    ) -> SpecAuthorityAcceptance | None:
        with Session(self._engine) as session:
            statement = select(SpecAuthorityAcceptance).where(
                SpecAuthorityAcceptance.product_id == request.project_id,
                _ACCEPTANCE_STATUS.in_(["accepted", "rejected"]),
            )
            if request.pending_authority_id is not None:
                statement = statement.where(
                    SpecAuthorityAcceptance.pending_authority_id
                    == request.pending_authority_id
                )
            elif request.review_token is not None:
                statement = statement.where(
                    SpecAuthorityAcceptance.review_token == request.review_token
                )
            else:
                return None
            return session.exec(statement).first()

    def _write_workflow_state_from_row(self, row: SpecAuthorityAcceptance) -> None:
        if row.status == "accepted":
            self._workflow.update_session_status(
                str(row.product_id),
                {
                    "setup_status": "passed",
                    "fsm_state": "VISION_INTERVIEW",
                    "setup_error": None,
                    "setup_error_code": None,
                },
            )
            return
        self._workflow.update_session_status(
            str(row.product_id),
            {
                "setup_status": "authority_rejected",
                "fsm_state": "SETUP_REQUIRED",
                "setup_error": row.rationale,
                "setup_error_code": "AUTHORITY_REJECTED",
            },
        )

    def _finalize_validation_failed(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        response: JsonDict,
    ) -> bool:
        db_now = _db_datetime(_now())
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_LEDGER_MUTATION_EVENT_ID == mutation_event_id)  # noqa: SIM300
                .where(_LEDGER_STATUS == MutationStatus.PENDING.value)  # noqa: SIM300
                .where(_LEDGER_LEASE_OWNER == lease_owner)  # noqa: SIM300
                .values(
                    status=MutationStatus.VALIDATION_FAILED.value,
                    current_step="validation_failed",
                    response_json=_json_dump(response),
                    last_error_json=_json_dump(_first_error(response)),
                    recovery_action=RecoveryAction.NONE.value,
                    recovery_safe_to_auto_resume=False,
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1


def terminal_decision_key(
    *,
    project_id: int,
    spec_version_id: int,
    pending_authority_id: int,
) -> str:
    """Return the canonical terminal decision key."""
    return f"{project_id}:{spec_version_id}:{pending_authority_id}"


def _stored_spec_hash(source_spec_hash: str) -> str:
    """Return the legacy decision spec_hash value compatible with projections."""
    return source_spec_hash.removeprefix("sha256:")


def _decision_lock(engine: Engine) -> RLock:
    with _ENGINE_LOCKS_GUARD:
        lock = _ENGINE_LOCKS.get(engine)
        if lock is None:
            lock = RLock()
            _ENGINE_LOCKS[engine] = lock
        return lock


def normalized_decision_request_hash(
    *,
    decision: Literal["accept", "reject"],
    request: AuthorityAcceptRequest | AuthorityRejectRequest,
) -> str:
    """Return a canonical idempotency request hash."""
    return canonical_hash(
        {
            "command": AUTHORITY_DECISION_COMMAND,
            "decision": decision,
            "project_id": request.project_id,
            "pending_authority_id": request.pending_authority_id,
            "review_token": request.review_token,
            "explicit_guard_tuple": {
                "authority_fingerprint": request.expected_authority_fingerprint,
                "source_spec_hash": request.expected_source_spec_hash,
                "disk_spec_hash": request.expected_disk_spec_hash,
                "resolved_spec_path": request.expected_resolved_spec_path,
                "state": request.expected_state,
                "setup_status": request.expected_setup_status,
                "content_included": request.expected_content_included,
                "omission_assessment": request.expected_omission_assessment,
                "coverage_summary_fingerprint": (
                    request.expected_coverage_summary_fingerprint
                ),
            },
            "policy": request.policy,
            "actor_mode": request.actor_mode,
            "decided_by": request.changed_by or request.actor_mode,
            "allow_incomplete_review": (
                request.allow_incomplete_review
                if isinstance(request, AuthorityAcceptRequest)
                else None
            ),
            "incomplete_review_rationale": (
                request.incomplete_review_rationale
                if isinstance(request, AuthorityAcceptRequest)
                else None
            ),
            "incomplete_review_overrides": (
                [
                    override.model_dump(mode="json")
                    for override in request.incomplete_review_overrides
                ]
                if isinstance(request, AuthorityAcceptRequest)
                else None
            ),
            "reason": (
                request.reason
                if isinstance(request, AuthorityRejectRequest)
                else None
            ),
        }
    )


def _snapshot_from_review(
    snapshot: AuthorityReviewSnapshot,
) -> ReviewedAuthoritySnapshot:
    return ReviewedAuthoritySnapshot(
        project_id=snapshot.project_id,
        pending_authority_id=_required_int(snapshot.pending_authority_id),
        authority_fingerprint=_required(snapshot.authority_fingerprint),
        source_spec_hash=snapshot.source_spec_hash,
        disk_spec_hash=snapshot.disk_spec_hash,
        resolved_spec_path=snapshot.resolved_spec_path,
        compiler_version=snapshot.compiler_version,
        prompt_hash=snapshot.prompt_hash,
        fsm_state=snapshot.fsm_state,
        setup_status=snapshot.setup_status,
        content_included=snapshot.content_included,
        omission_assessment=snapshot.omission_assessment,
        coverage_summary_fingerprint=snapshot.coverage_summary_fingerprint,
        review_token=snapshot.review_token,
        spec_version_id=_required_int(snapshot.spec_version_id),
        review_findings=list(snapshot.review_findings),
        ir_packet_limits=dict(snapshot.ir_packet_limits),
    )


def _missing_explicit_guards(
    *,
    request: AuthorityDecisionBase,
    require_completeness: bool,
) -> list[str]:
    fields = [
        "pending_authority_id",
        "expected_authority_fingerprint",
        "expected_source_spec_hash",
        "expected_disk_spec_hash",
        "expected_resolved_spec_path",
        "expected_state",
        "expected_setup_status",
    ]
    if require_completeness:
        fields.extend(
            [
                "expected_content_included",
                "expected_omission_assessment",
                "expected_coverage_summary_fingerprint",
            ]
        )
    return [field for field in fields if getattr(request, field) is None]


def _stale_guard_error(
    *,
    command: str,
    field_name: str,
    expected: object,
    actual: object,
) -> JsonDict:
    code = ErrorCode.STALE_ARTIFACT_FINGERPRINT
    if field_name in {"expected_state", "expected_setup_status"}:
        code = ErrorCode.STALE_STATE
    elif field_name in {"pending_authority_id"}:
        code = ErrorCode.STALE_AUTHORITY_VERSION
    elif field_name in {
        "expected_source_spec_hash",
        "expected_disk_spec_hash",
        "expected_resolved_spec_path",
    }:
        code = ErrorCode.AUTHORITY_SOURCE_CHANGED
    elif field_name in {
        "expected_content_included",
        "expected_omission_assessment",
        "expected_coverage_summary_fingerprint",
    }:
        code = ErrorCode.STALE_CONTEXT_FINGERPRINT
    return _error(
        command=command,
        code=code,
        details={"field": field_name, "expected": expected, "actual": actual},
        remediation=["Run authority review again and retry the decision."],
    )


def _accept_incomplete_error(
    *,
    request: AuthorityAcceptRequest,
    snapshot: ReviewedAuthoritySnapshot,
    command: str,
) -> JsonDict | None:
    blocking_findings = _blocking_review_findings(snapshot.review_findings)
    if request.allow_incomplete_review and not request.incomplete_review_overrides:
        return _invalid_override_error(
            command=command,
            message=(
                "Broad incomplete-review override is not accepted without "
                "candidate-specific overrides."
            ),
            details={"missing": ["incomplete_review_overrides"]},
        )
    if request.incomplete_review_rationale and not request.incomplete_review_overrides:
        return _invalid_override_error(
            command=command,
            message=(
                "Incomplete review rationale must be attached to "
                "candidate-specific overrides."
            ),
            details={"missing": ["incomplete_review_overrides"]},
        )
    if not blocking_findings and not request.incomplete_review_overrides:
        return None
    if not blocking_findings and request.incomplete_review_overrides:
        return _invalid_override_error(
            command=command,
            message="Incomplete review overrides did not match blocking findings.",
            details={"unmatched_overrides": _override_keys(request)},
        )
    if request.incomplete_review_overrides:
        validation_error = _validate_incomplete_review_overrides(
            request=request,
            blocking_findings=blocking_findings,
            command=command,
        )
        if validation_error is None:
            return None
        return validation_error
    return _error(
        command=command,
        code=ErrorCode.AUTHORITY_REVIEW_INCOMPLETE,
        details={
            "omission_assessment": snapshot.omission_assessment,
            "allow_incomplete_review": request.allow_incomplete_review,
            "blocking_findings": blocking_findings,
        },
        remediation=[
            "Review blocking findings and provide candidate-specific overrides."
        ],
    )


def _blocking_review_findings(findings: list[JsonDict]) -> list[JsonDict]:
    return [
        finding
        for finding in findings
        if finding.get("severity") == "blocking"
        and finding.get("code") != "AUTHORITY_COVERAGE_INCOMPLETE"
    ]


def _validate_incomplete_review_overrides(
    *,
    request: AuthorityAcceptRequest,
    blocking_findings: list[JsonDict],
    command: str,
) -> JsonDict | None:
    non_overrideable = [
        finding
        for finding in blocking_findings
        if finding.get("override_allowed") is False
    ]
    requested_keys = set(_override_keys(request))
    if non_overrideable:
        return _invalid_override_error(
            command=command,
            message="Non-overrideable authority review findings cannot be accepted.",
            details={"non_overrideable_findings": non_overrideable},
        )
    if any(
        (
            str(finding.get("code")),
            candidate_id,
        )
        in requested_keys
        for finding in non_overrideable
        for candidate_id in _finding_candidate_ids(finding)
    ) or any(
        str(override.finding_code) == "AUTHORITY_REVIEW_PACKET_TRUNCATED"
        for override in request.incomplete_review_overrides
    ):
        return _invalid_override_error(
            command=command,
            message=(
                "Review packet truncation and non-overrideable findings cannot "
                "be overridden."
            ),
            details={"non_overrideable_findings": non_overrideable},
        )

    valid_keys = {
        (str(finding.get("code")), candidate_id)
        for finding in blocking_findings
        if finding.get("override_allowed") is not False
        for candidate_id in _finding_candidate_ids(finding)
    }
    missing_keys = sorted(valid_keys - requested_keys)
    extra_keys = sorted(requested_keys - valid_keys)
    if extra_keys:
        return _invalid_override_error(
            command=command,
            message=(
                "Incomplete review override does not match a current blocking "
                "finding."
            ),
            details={"unmatched_overrides": extra_keys},
        )
    if missing_keys:
        return _error(
            command=command,
            code=ErrorCode.AUTHORITY_REVIEW_INCOMPLETE,
            details={
                "missing_overrides": missing_keys,
                "blocking_findings": blocking_findings,
            },
            remediation=[
                "Provide an override for each affected candidate and finding code."
            ],
        )
    return None


def _finding_candidate_ids(finding: Mapping[str, Any]) -> list[str]:
    candidate_ids = finding.get("candidate_ids")
    if not isinstance(candidate_ids, list):
        return []
    return [str(candidate_id) for candidate_id in candidate_ids if candidate_id]


def _override_keys(request: AuthorityAcceptRequest) -> list[tuple[str, str]]:
    return [
        (override.finding_code, override.candidate_id)
        for override in request.incomplete_review_overrides
    ]


def _invalid_override_error(
    *,
    command: str,
    message: str,
    details: JsonDict,
) -> JsonDict:
    return error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.INVALID_COMMAND,
            message=message,
            details=details,
            remediation=[
                "Pass repeated candidate-specific incomplete-review overrides."
            ],
        ),
    )


def _response_data(
    *,
    row: SpecAuthorityAcceptance,
) -> JsonDict:
    if row.status == "accepted":
        return {
            "project_id": row.product_id,
            "authority_id": row.pending_authority_id,
            "accepted_decision_id": row.id,
            "accepted_spec_version_id": row.spec_version_id,
            "authority_fingerprint": row.authority_fingerprint,
            "setup_status": "passed",
            "fsm_state": "VISION_INTERVIEW",
            "next_actions": [
                {
                    "command": (
                        f"agileforge vision generate --project-id {row.product_id}"
                    ),
                    "reason": "Authority is accepted and Vision is unlocked.",
                }
            ],
        }
    return {
        "project_id": row.product_id,
        "pending_authority_id": row.pending_authority_id,
        "rejected_decision_id": row.id,
        "setup_status": "authority_rejected",
        "fsm_state": "SETUP_REQUIRED",
        "reason": row.rationale,
        "next_actions": [],
        "blocked_future_commands": [
            {
                "command": (
                    "agileforge project spec update "
                    f"--project-id {row.product_id} "
                    f"--spec-file {row.resolved_spec_path}"
                ),
                "installed": False,
                "reason": (
                    "Spec update/recompile is required after rejection, but this "
                    "command is not installed yet."
                ),
            }
        ],
        "manual_remediation": [
            "No installed CLI command can recompile a rejected authority yet.",
            (
                "Revise the spec or compiler, then run the future project spec "
                "update command when installed."
            ),
        ],
    }


def _decision_from_status(status: str) -> Literal["accept", "reject"] | None:
    if status == "accepted":
        return "accept"
    if status == "rejected":
        return "reject"
    return None


def _retarget_error(payload: JsonDict, *, command: str) -> JsonDict:
    if payload.get("ok") is not False:
        return payload
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return payload
    first = errors[0]
    if not isinstance(first, dict):
        return payload
    original_code = str(first.get("code") or ErrorCode.COMMAND_EXCEPTION.value)
    code = (
        ErrorCode.AUTHORITY_SOURCE_UNAVAILABLE
        if original_code in {ErrorCode.SPEC_FILE_NOT_FOUND.value}
        else original_code
    )
    return error_envelope(
        command=command,
        error=workbench_error(
            code,
            message=str(first.get("message") or "Authority decision failed."),
            details=dict(first.get("details") or {}),
            remediation=list(first.get("remediation") or []),
        ),
    )


def _first_error(response: JsonDict) -> JsonDict:
    errors = response.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return dict(errors[0])
    return {}


def _first_error_code(response: JsonDict) -> str | None:
    error = _first_error(response)
    code = error.get("code")
    return str(code) if code is not None else None


def _replay_terminal_non_success(
    *,
    command: str,
    row: CliMutationLedger,
) -> JsonDict:
    loaded = _json_load(row.response_json)
    if isinstance(loaded, dict):
        return loaded
    last_error = _json_load(row.last_error_json)
    code = ErrorCode.MUTATION_FAILED.value
    details: JsonDict = {
        "mutation_event_id": row.mutation_event_id,
        "status": row.status,
    }
    if isinstance(last_error, dict):
        maybe_code = last_error.get("code")
        if maybe_code is not None:
            code = str(maybe_code)
        details["last_error"] = last_error
    return _error(
        command=command,
        code=code,
        details=details,
        remediation=["Use a new idempotency key for a new request."],
    )


def _authority_already_decided_error(
    *,
    command: str,
    row: SpecAuthorityAcceptance,
) -> JsonDict:
    return _error(
        command=command,
        code=ErrorCode.AUTHORITY_ALREADY_DECIDED,
        details={
            "project_id": row.product_id,
            "spec_version_id": row.spec_version_id,
            "pending_authority_id": row.pending_authority_id,
            "terminal_decision_key": row.terminal_decision_key,
            "status": row.status,
        },
        remediation=["Use the existing terminal decision or compile a new authority."],
    )


def _ledger_error(
    *,
    command: str,
    code: str | ErrorCode,
    mutation_event_id: int,
) -> JsonDict:
    return _error(
        command=command,
        code=code,
        details={"mutation_event_id": mutation_event_id},
        remediation=["Inspect the mutation ledger before retrying."],
    )


def _recovery_required_response(
    *,
    command: str,
    mutation_event_id: int,
    completed_step: str,
    next_step: str,
) -> JsonDict:
    return _error(
        command=command,
        code=ErrorCode.MUTATION_RECOVERY_REQUIRED,
        details={
            "mutation_event_id": mutation_event_id,
            "completed_steps": [completed_step],
            "next_step": next_step,
        },
        remediation=["Retry the same idempotency key to repair workflow state."],
    )


def _error(
    *,
    command: str,
    code: str | ErrorCode,
    details: JsonDict,
    remediation: list[str],
) -> JsonDict:
    return error_envelope(
        command=command,
        error=workbench_error(code, details=details, remediation=remediation),
    )


def _success(data: JsonDict) -> JsonDict:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _command_for_decision(decision: Literal["accept", "reject"]) -> str:
    return (
        AUTHORITY_ACCEPT_COMMAND
        if decision == "accept"
        else AUTHORITY_REJECT_COMMAND
    )


def _rationale(
    *,
    decision: Literal["accept", "reject"],
    request: AuthorityAcceptRequest | AuthorityRejectRequest,
) -> str | None:
    if decision == "reject":
        return _cast_reject(request).reason
    return _cast_accept(request).incomplete_review_rationale


def _persisted_incomplete_review_overrides_json(
    request: AuthorityAcceptRequest | AuthorityRejectRequest,
    *,
    snapshot: ReviewedAuthoritySnapshot,
    recorded_at: datetime,
) -> str | None:
    if not isinstance(request, AuthorityAcceptRequest):
        return None
    if not request.incomplete_review_overrides:
        return None
    actor = request.actor_mode
    payload = [
        {
            "candidate_id": override.candidate_id,
            "finding_code": override.finding_code,
            "rationale": override.rationale,
            "actor": actor,
            "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
            "review_token": snapshot.review_token,
        }
        for override in request.incomplete_review_overrides
    ]
    return _json_dump(payload)


def _lease_owner(*, idempotency_key: str, correlation_id: str | None) -> str:
    suffix = f":{correlation_id}" if correlation_id else ""
    return f"authority-decision:{idempotency_key}{suffix}"


def _recovery_lease_owner(*, idempotency_key: str, correlation_id: str | None) -> str:
    suffix = f":{correlation_id}" if correlation_id else ""
    return f"authority-decision-recovery:{idempotency_key}{suffix}"


def _now() -> datetime:
    return datetime.now(UTC)


def _event_id(row: CliMutationLedger) -> int:
    return _required_int(row.mutation_event_id)


def _required(value: str | None) -> str:
    if value is None:
        msg = "Required value was missing."
        raise ValueError(msg)
    return value


def _required_int(value: int | None) -> int:
    if value is None:
        msg = "Required integer value was missing."
        raise ValueError(msg)
    return value


def _validate_token(*, name: str, value: str) -> None:
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        msg = f"{name} must be ASCII"
        raise ValueError(msg) from exc
    if not _KEY_MIN_LENGTH <= len(value) <= _KEY_MAX_LENGTH:
        msg = f"{name} must be 8-128 characters"
        raise ValueError(msg)
    if _KEY_PATTERN.fullmatch(value) is None:
        msg = f"{name} must match [A-Za-z0-9._:-]+"
        raise ValueError(msg)


def _cast_accept(request: AuthorityDecisionBase) -> AuthorityAcceptRequest:
    if not isinstance(request, AuthorityAcceptRequest):
        msg = "AuthorityAcceptRequest expected."
        raise TypeError(msg)
    return request


def _cast_reject(request: AuthorityDecisionBase) -> AuthorityRejectRequest:
    if not isinstance(request, AuthorityRejectRequest):
        msg = "AuthorityRejectRequest expected."
        raise TypeError(msg)
    return request
