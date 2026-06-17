"""Durable mutation ledger for CLI idempotency and recovery."""

# ruff: noqa: E501, EM101, PLR0913, SIM300, TRY003, TRY301

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.agent_workbench import CliMutationLedger
from services.agent_workbench.error_codes import ErrorCode, workbench_error

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_MUTATION_EVENT_ID: Any = CliMutationLedger.mutation_event_id
_COMMAND: Any = CliMutationLedger.command
_IDEMPOTENCY_KEY: Any = CliMutationLedger.idempotency_key
_PROJECT_ID: Any = CliMutationLedger.project_id
_STATUS: Any = CliMutationLedger.status
_LEASE_OWNER: Any = CliMutationLedger.lease_owner
_LEASE_EXPIRES_AT: Any = CliMutationLedger.lease_expires_at

IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
MUTATION_IN_PROGRESS = "MUTATION_IN_PROGRESS"
MUTATION_RECOVERY_REQUIRED = "MUTATION_RECOVERY_REQUIRED"
MUTATION_RESUME_CONFLICT = "MUTATION_RESUME_CONFLICT"
MUTATION_NOT_FOUND = "MUTATION_NOT_FOUND"
DEFAULT_STALE_PENDING_TIMEOUT_SECONDS = 300
DEFAULT_LEASE_SECONDS = DEFAULT_STALE_PENDING_TIMEOUT_SECONDS
DEFAULT_CLI_RESUME_LEASE_OWNER = "agileforge-cli:mutation-resume"


class MutationStatus(StrEnum):
    """Stable mutation ledger states."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    VALIDATION_FAILED = "validation_failed"
    GUARD_REJECTED = "guard_rejected"
    DOMAIN_FAILED_NO_SIDE_EFFECTS = "domain_failed_no_side_effects"
    RECOVERY_REQUIRED = "recovery_required"
    SUPERSEDED = "superseded"


REPLAYABLE_RESPONSE_STATUSES = frozenset(
    {
        MutationStatus.SUCCEEDED.value,
        MutationStatus.VALIDATION_FAILED.value,
        MutationStatus.GUARD_REJECTED.value,
        MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS.value,
        MutationStatus.SUPERSEDED.value,
    }
)


class RecoveryAction(StrEnum):
    """Finite recovery actions for incomplete mutations."""

    NONE = "none"
    RESUME_FROM_STEP = "resume_from_step"
    RECONCILE_THEN_RESUME = "reconcile_then_resume"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"


@dataclass(frozen=True)
class LedgerLoadResult:
    """Result from creating or loading a mutation ledger row."""

    ledger: CliMutationLedger
    replayed: bool = False
    response: dict[str, Any] | None = None
    error_code: str | None = None


def _json_dump(value: object) -> str:
    """Dump deterministic JSON for persisted ledger blobs."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _json_load(value: str | None) -> dict[str, Any] | list[Any] | None:
    """Load a persisted JSON object or array."""
    if not value:
        return None
    loaded = json.loads(value)
    if isinstance(loaded, (dict, list)):
        return loaded
    return None


def _json_blob(value: str | None) -> object:
    """Return a JSON-friendly value from a persisted JSON blob."""
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _completed_steps(row: CliMutationLedger) -> list[str]:
    loaded = _json_load(row.completed_steps_json)
    if isinstance(loaded, list):
        return [str(item) for item in loaded]
    return []


def _stale_pending_error(row: CliMutationLedger, now: datetime) -> dict[str, Any]:
    return {
        "code": "STALE_PENDING",
        "message": "Pending mutation lease expired.",
        "details": {"current_step": row.current_step},
        "retryable": True,
        "recorded_at": _utc_isoformat(now),
    }


def _utc_isoformat(value: datetime) -> str:
    """Return an ISO timestamp for the UTC instant represented by value."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _db_datetime(value: datetime) -> datetime:
    """Normalize datetimes to UTC-naive values for SQLite persistence.

    Naive inputs are treated as already UTC. Aware inputs are converted to the
    equivalent UTC instant before dropping tzinfo.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(tzinfo=None)


class MutationLedgerRepository:
    """Repository for mutation ledger compare-and-set transitions."""

    def __init__(self, *, engine: Engine) -> None:
        """Initialize the repository with a SQLAlchemy engine."""
        self._engine: Engine = engine
        self.fail_after_retry_update_for_test: bool = False

    def show_event(
        self,
        *,
        mutation_event_id: int,
        include_trace_metadata: bool = True,
    ) -> dict[str, Any]:
        """Return one mutation ledger event in a service envelope."""
        with Session(self._engine) as session:
            row = session.get(CliMutationLedger, mutation_event_id)
            if row is None:
                return _error_result(
                    code=ErrorCode.MUTATION_NOT_FOUND,
                    details={"mutation_event_id": mutation_event_id},
                    remediation=["agileforge mutation list"],
                )
            return _success_result(
                _row_payload(
                    row,
                    include_trace_metadata=include_trace_metadata,
                )
            )

    def list_events(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Return mutation ledger events filtered by project and status."""
        statement = select(CliMutationLedger)
        if project_id is not None:
            statement = statement.where(_PROJECT_ID == project_id)
        if status is not None:
            statement = statement.where(_STATUS == status)
        statement = statement.order_by(_MUTATION_EVENT_ID)

        with Session(self._engine) as session:
            rows = session.exec(statement).all()
            return _success_result({"items": [_row_payload(row) for row in rows]})

    def resume_event(
        self,
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Acquire a recovery lease without performing domain recovery."""
        shown = self.show_event(
            mutation_event_id=mutation_event_id,
            include_trace_metadata=False,
        )
        if shown.get("ok") is not True:
            return shown

        now = datetime.now(UTC)
        self._repair_expired_pending_resume(
            mutation_event_id=mutation_event_id,
            now=now,
        )
        with Session(self._engine) as session:
            row = session.get(CliMutationLedger, mutation_event_id)
            if row is None:
                return _error_result(
                    code=MUTATION_NOT_FOUND,
                    details={"mutation_event_id": mutation_event_id},
                    remediation=["Re-read mutation state before retrying recovery."],
                )
            if (
                row.command == "agileforge project create"
                and row.status == MutationStatus.RECOVERY_REQUIRED.value
            ):
                data = _row_payload(row)
                data["recovery"] = {
                    "acquired": False,
                    "domain_resume_required": False,
                    "handoff_command": "agileforge project setup retry",
                    "reason": "Project creation recovery is resumed by setup retry.",
                }
                return _success_result(data)
        result = self.acquire_resume_lease(
            mutation_event_id=mutation_event_id,
            lease_owner=_resume_lease_owner(correlation_id),
            now=now,
            lease_seconds=DEFAULT_LEASE_SECONDS,
        )
        data = _row_payload(result.ledger)
        if result.error_code is not None:
            return _error_result(
                code=result.error_code,
                details={"mutation_event_id": mutation_event_id},
                remediation=[
                    "agileforge mutation show "
                    f"--mutation-event-id {mutation_event_id}"
                ],
                data=data,
            )

        data["recovery"] = {
            "acquired": True,
            "domain_resume_required": True,
        }
        return _success_result(data)

    def _repair_expired_pending_resume(
        self,
        *,
        mutation_event_id: int,
        now: datetime,
    ) -> LedgerLoadResult | None:
        """Fence an expired pending resume lease back into recovery-required."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            row = session.get(CliMutationLedger, mutation_event_id)
            if (
                row is None
                or row.status != MutationStatus.PENDING.value
                or row.lease_expires_at is None
                or row.lease_expires_at > db_now
            ):
                return None
            return self._handle_pending_existing(session=session, row=row, now=now)

    def create_or_load(
        self,
        *,
        command: str,
        idempotency_key: str,
        request_hash: str,
        project_id: int | None,
        correlation_id: str,
        changed_by: str,
        lease_owner: str,
        now: datetime,
        recovers_mutation_event_id: int | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> LedgerLoadResult:
        """Create a pending row or return the existing deterministic outcome."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            existing = session.exec(
                select(CliMutationLedger).where(
                    _COMMAND == command,
                    _IDEMPOTENCY_KEY == idempotency_key,
                )
            ).first()
            if existing is not None:
                return self._existing_result(
                    session=session,
                    row=existing,
                    request_hash=request_hash,
                    now=now,
                )

            row = CliMutationLedger(
                command=command,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                project_id=project_id,
                correlation_id=correlation_id,
                changed_by=changed_by,
                recovers_mutation_event_id=recovers_mutation_event_id,
                status=MutationStatus.PENDING.value,
                lease_owner=lease_owner,
                lease_acquired_at=db_now,
                last_heartbeat_at=db_now,
                lease_expires_at=db_now + timedelta(seconds=lease_seconds),
                created_at=db_now,
                updated_at=db_now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raced_row = session.exec(
                    select(CliMutationLedger).where(
                        _COMMAND == command,
                        _IDEMPOTENCY_KEY == idempotency_key,
                    )
                ).first()
                if raced_row is None:
                    raise
                return self._existing_result(
                    session=session,
                    row=raced_row,
                    request_hash=request_hash,
                    now=now,
                )
            session.refresh(row)
            return LedgerLoadResult(ledger=row)

    def _existing_result(
        self,
        *,
        session: Session,
        row: CliMutationLedger,
        request_hash: str,
        now: datetime,
    ) -> LedgerLoadResult:
        if row.request_hash != request_hash:
            return LedgerLoadResult(ledger=row, error_code=IDEMPOTENCY_KEY_REUSED)
        if row.status in REPLAYABLE_RESPONSE_STATUSES:
            response = _json_load(row.response_json)
            return LedgerLoadResult(
                ledger=row,
                replayed=True,
                response=response if isinstance(response, dict) else None,
            )
        if row.status == MutationStatus.PENDING.value:
            return self._handle_pending_existing(session=session, row=row, now=now)
        if row.status == MutationStatus.RECOVERY_REQUIRED.value:
            return LedgerLoadResult(ledger=row, error_code=MUTATION_RECOVERY_REQUIRED)
        return LedgerLoadResult(ledger=row, replayed=True)

    def _handle_pending_existing(
        self,
        *,
        session: Session,
        row: CliMutationLedger,
        now: datetime,
    ) -> LedgerLoadResult:
        db_now = _db_datetime(now)
        if row.lease_expires_at and row.lease_expires_at > db_now:
            return LedgerLoadResult(ledger=row, error_code=MUTATION_IN_PROGRESS)

        statement = (
            update(CliMutationLedger)
            .where(_MUTATION_EVENT_ID == row.mutation_event_id)
            .where(_STATUS == MutationStatus.PENDING.value)
        )
        if row.lease_owner is None:
            statement = statement.where(_LEASE_OWNER.is_(None))
        else:
            statement = statement.where(_LEASE_OWNER == row.lease_owner)
        if row.lease_expires_at is None:
            statement = statement.where(_LEASE_EXPIRES_AT.is_(None))
        else:
            statement = statement.where(_LEASE_EXPIRES_AT <= db_now)

        result = session.exec(
            statement.values(
                status=MutationStatus.RECOVERY_REQUIRED.value,
                recovery_action=RecoveryAction.RECONCILE_THEN_RESUME.value,
                recovery_safe_to_auto_resume=False,
                lease_owner=None,
                lease_acquired_at=None,
                last_heartbeat_at=None,
                lease_expires_at=None,
                last_error_json=_json_dump(_stale_pending_error(row=row, now=now)),
                updated_at=db_now,
            )
        )
        session.commit()
        if result.rowcount != 1:
            return LedgerLoadResult(ledger=row, error_code=MUTATION_IN_PROGRESS)

        refreshed = session.get(CliMutationLedger, row.mutation_event_id)
        if refreshed is None:
            message = f"Mutation event {row.mutation_event_id} not found."
            raise ValueError(message)
        return LedgerLoadResult(
            ledger=refreshed,
            error_code=MUTATION_RECOVERY_REQUIRED,
        )

    def repair_setup_recovery_target(
        self,
        *,
        mutation_event_id: int,
        expected_command: str,
        expected_project_id: int,
        now: datetime,
    ) -> LedgerLoadResult:
        """Repair a project setup recovery target before setup retry validation."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            row = session.get(CliMutationLedger, mutation_event_id)
            if row is None:
                message = f"Mutation event {mutation_event_id} not found."
                raise ValueError(message)
            if row.command != expected_command or row.project_id != expected_project_id:
                return LedgerLoadResult(ledger=row, error_code=MUTATION_RESUME_CONFLICT)
            if row.status == MutationStatus.RECOVERY_REQUIRED.value:
                return LedgerLoadResult(ledger=row)
            if row.status != MutationStatus.PENDING.value:
                return LedgerLoadResult(ledger=row, error_code=MUTATION_RESUME_CONFLICT)
            if row.lease_expires_at is None or row.lease_expires_at > db_now:
                return LedgerLoadResult(ledger=row, error_code=MUTATION_IN_PROGRESS)

            snapshot_lease_owner = row.lease_owner
            snapshot_lease_expires_at = row.lease_expires_at
            statement = (
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_COMMAND == expected_command)
                .where(_PROJECT_ID == expected_project_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .where(_LEASE_EXPIRES_AT == snapshot_lease_expires_at)
                .where(_LEASE_EXPIRES_AT <= db_now)
            )
            if snapshot_lease_owner is None:
                statement = statement.where(_LEASE_OWNER.is_(None))
            else:
                statement = statement.where(_LEASE_OWNER == snapshot_lease_owner)

            result = session.exec(
                statement.values(
                    status=MutationStatus.RECOVERY_REQUIRED.value,
                    recovery_action=RecoveryAction.RECONCILE_THEN_RESUME.value,
                    recovery_safe_to_auto_resume=False,
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    last_error_json=_json_dump(_stale_pending_error(row=row, now=now)),
                    updated_at=db_now,
                )
            )
            session.commit()
            session.expire_all()
            repaired = session.get(CliMutationLedger, mutation_event_id)
            if repaired is None:
                message = f"Mutation event {mutation_event_id} not found."
                raise ValueError(message)
            if result.rowcount != 1:
                error_code = MUTATION_RESUME_CONFLICT
                if (
                    repaired.status == MutationStatus.PENDING.value
                    and (
                        repaired.lease_expires_at is None
                        or repaired.lease_expires_at > db_now
                    )
                ):
                    error_code = MUTATION_IN_PROGRESS
                return LedgerLoadResult(
                    ledger=repaired,
                    error_code=error_code,
                )
            return LedgerLoadResult(ledger=repaired)

    def transition_status(
        self,
        *,
        mutation_event_id: int,
        expected_status: MutationStatus,
        expected_lease_owner: str | None,
        new_status: MutationStatus,
        new_lease_owner: str | None,
        now: datetime,
        lease_seconds: int | None = None,
    ) -> LedgerLoadResult:
        """Transition status using compare-and-set and affected-row checking."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            statement = (
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == expected_status.value)
            )
            if expected_lease_owner is None:
                statement = statement.where(_LEASE_OWNER.is_(None))
            else:
                statement = statement.where(_LEASE_OWNER == expected_lease_owner)

            values: dict[str, Any] = {
                "status": new_status.value,
                "lease_owner": new_lease_owner,
                "updated_at": db_now,
            }
            if new_lease_owner is None:
                values.update(
                    {
                        "lease_acquired_at": None,
                        "last_heartbeat_at": None,
                        "lease_expires_at": None,
                    }
                )
            elif lease_seconds is not None:
                values.update(
                    {
                        "lease_acquired_at": db_now,
                        "last_heartbeat_at": db_now,
                        "lease_expires_at": db_now + timedelta(seconds=lease_seconds),
                    }
                )

            result = session.exec(statement.values(**values))
            session.commit()
            row = session.get(CliMutationLedger, mutation_event_id)
            if row is None:
                message = f"Mutation event {mutation_event_id} not found."
                raise ValueError(message)
            if result.rowcount != 1:
                return LedgerLoadResult(
                    ledger=row,
                    error_code=MUTATION_RESUME_CONFLICT,
                )
            return LedgerLoadResult(ledger=row)

    def heartbeat(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        now: datetime,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Refresh a lease only when the caller still owns it."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .where(_LEASE_OWNER == lease_owner)
                .where(_LEASE_EXPIRES_AT > db_now)
                .values(
                    last_heartbeat_at=db_now,
                    lease_expires_at=db_now + timedelta(seconds=lease_seconds),
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def set_project_id(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        project_id: int,
        now: datetime,
    ) -> bool:
        """Attach the created project id to an active mutation row."""
        with Session(self._engine) as session:
            result = self.set_project_id_in_session(
                session,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                project_id=project_id,
                now=now,
            )
            session.commit()
            return result

    @staticmethod
    def set_project_id_in_session(
        session: Session,
        *,
        mutation_event_id: int,
        lease_owner: str,
        project_id: int,
        now: datetime,
    ) -> bool:
        """Attach the created project id using the caller's transaction."""
        db_now = _db_datetime(now)
        result = session.exec(
            update(CliMutationLedger)
            .where(_MUTATION_EVENT_ID == mutation_event_id)
            .where(_STATUS == MutationStatus.PENDING.value)
            .where(_LEASE_OWNER == lease_owner)
            .where(_LEASE_EXPIRES_AT > db_now)
            .values(project_id=project_id, updated_at=db_now)
        )
        return result.rowcount == 1

    @staticmethod
    def mark_step_complete_in_session(
        session: Session,
        *,
        mutation_event_id: int,
        lease_owner: str,
        step: str,
        next_step: str,
        now: datetime,
    ) -> bool:
        """Record a completed step using the caller's transaction."""
        db_now = _db_datetime(now)
        row = session.get(CliMutationLedger, mutation_event_id)
        if (
            row is None
            or row.status != MutationStatus.PENDING.value
            or row.lease_owner != lease_owner
            or row.lease_expires_at is None
            or row.lease_expires_at <= db_now
        ):
            return False
        steps = _completed_steps(row)
        if step not in steps:
            steps.append(step)
        result = session.exec(
            update(CliMutationLedger)
            .where(_MUTATION_EVENT_ID == mutation_event_id)
            .where(_STATUS == MutationStatus.PENDING.value)
            .where(_LEASE_OWNER == lease_owner)
            .where(_LEASE_EXPIRES_AT > db_now)
            .values(
                completed_steps_json=_json_dump(steps),
                current_step=next_step,
                updated_at=db_now,
            )
        )
        return result.rowcount == 1

    def acquire_recovery_lease(
        self,
        *,
        mutation_event_id: int,
        expected_project_id: int,
        recovery_lease_owner: str,
        now: datetime,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Acquire ownership of an original recovery_required mutation row."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.RECOVERY_REQUIRED.value)
                .where(_PROJECT_ID == expected_project_id)
                .where(
                    (_LEASE_OWNER.is_(None))
                    | (_LEASE_EXPIRES_AT.is_(None))
                    | (_LEASE_EXPIRES_AT <= db_now)
                )
                .values(
                    lease_owner=recovery_lease_owner,
                    lease_acquired_at=db_now,
                    last_heartbeat_at=db_now,
                    lease_expires_at=db_now + timedelta(seconds=lease_seconds),
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def release_recovery_lease(
        self,
        *,
        mutation_event_id: int,
        recovery_lease_owner: str,
        now: datetime,
    ) -> bool:
        """Clear a retry-owned recovery lease without changing recovery_required status."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.RECOVERY_REQUIRED.value)
                .where(_LEASE_OWNER == recovery_lease_owner)
                .values(
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def mark_recovery_required(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        recovery_action: RecoveryAction,
        safe_to_auto_resume: bool,
        last_error: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Move an owned pending mutation to recovery_required."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .where(_LEASE_OWNER == lease_owner)
                .where(_LEASE_EXPIRES_AT > db_now)
                .values(
                    status=MutationStatus.RECOVERY_REQUIRED.value,
                    recovery_action=recovery_action.value,
                    recovery_safe_to_auto_resume=safe_to_auto_resume,
                    last_error_json=_json_dump(last_error),
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def supersede_recovered_event(
        self,
        *,
        mutation_event_id: int,
        superseded_by_mutation_event_id: int,
        lease_owner: str,
        response: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Single-row legacy helper; linked retry paths must not call this."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.RECOVERY_REQUIRED.value)
                .where(_LEASE_OWNER == lease_owner)
                .values(
                    status=MutationStatus.SUPERSEDED.value,
                    superseded_by_mutation_event_id=superseded_by_mutation_event_id,
                    response_json=_json_dump(response),
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def finalize_linked_retry_success(
        self,
        *,
        retry_mutation_event_id: int,
        retry_lease_owner: str,
        original_mutation_event_id: int,
        original_recovery_lease_owner: str,
        after: dict[str, Any],
        retry_response: dict[str, Any],
        original_replay_response: dict[str, Any],
        now: datetime,
    ) -> LedgerLoadResult:
        """Atomically mark retry succeeded and original recovery row superseded."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            try:
                retry_result = session.exec(
                    update(CliMutationLedger)
                    .where(_MUTATION_EVENT_ID == retry_mutation_event_id)
                    .where(_STATUS == MutationStatus.PENDING.value)
                    .where(_LEASE_OWNER == retry_lease_owner)
                    .where(_LEASE_EXPIRES_AT > db_now)
                    .values(
                        status=MutationStatus.SUCCEEDED.value,
                        after_json=_json_dump(after),
                        response_json=_json_dump(retry_response),
                        recovery_action=RecoveryAction.NONE.value,
                        recovery_safe_to_auto_resume=False,
                        lease_owner=None,
                        lease_acquired_at=None,
                        last_heartbeat_at=None,
                        lease_expires_at=None,
                        updated_at=db_now,
                    )
                )
                if retry_result.rowcount != 1:
                    session.rollback()
                    return self._linked_conflict(
                        session=session,
                        retry_mutation_event_id=retry_mutation_event_id,
                    )
                if self.fail_after_retry_update_for_test:
                    raise RuntimeError("Injected linked retry transition failure.")
                original_result = session.exec(
                    update(CliMutationLedger)
                    .where(_MUTATION_EVENT_ID == original_mutation_event_id)
                    .where(_STATUS == MutationStatus.RECOVERY_REQUIRED.value)
                    .where(_LEASE_OWNER == original_recovery_lease_owner)
                    .values(
                        status=MutationStatus.SUPERSEDED.value,
                        superseded_by_mutation_event_id=retry_mutation_event_id,
                        response_json=_json_dump(original_replay_response),
                        lease_owner=None,
                        lease_acquired_at=None,
                        last_heartbeat_at=None,
                        lease_expires_at=None,
                        updated_at=db_now,
                    )
                )
                if original_result.rowcount != 1:
                    session.rollback()
                    return self._linked_conflict(
                        session=session,
                        retry_mutation_event_id=retry_mutation_event_id,
                    )
                session.commit()
            except RuntimeError:
                session.rollback()
                return self._linked_conflict(
                    session=session,
                    retry_mutation_event_id=retry_mutation_event_id,
                )
            row = self._refreshed_row(
                session=session,
                mutation_event_id=retry_mutation_event_id,
            )
            return LedgerLoadResult(ledger=row, response=retry_response)

    def transfer_linked_retry_recovery(
        self,
        *,
        retry_mutation_event_id: int,
        retry_lease_owner: str,
        original_mutation_event_id: int,
        original_recovery_lease_owner: str,
        recovery_action: RecoveryAction,
        safe_to_auto_resume: bool,
        last_error: dict[str, Any],
        retry_response: dict[str, Any],
        original_replay_response: dict[str, Any],
        now: datetime,
    ) -> LedgerLoadResult:
        """Atomically transfer recovery ownership from original row to retry row."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            try:
                retry_result = session.exec(
                    update(CliMutationLedger)
                    .where(_MUTATION_EVENT_ID == retry_mutation_event_id)
                    .where(_STATUS == MutationStatus.PENDING.value)
                    .where(_LEASE_OWNER == retry_lease_owner)
                    .where(_LEASE_EXPIRES_AT > db_now)
                    .values(
                        status=MutationStatus.RECOVERY_REQUIRED.value,
                        recovery_action=recovery_action.value,
                        recovery_safe_to_auto_resume=safe_to_auto_resume,
                        last_error_json=_json_dump(last_error),
                        response_json=_json_dump(retry_response),
                        lease_owner=None,
                        lease_acquired_at=None,
                        last_heartbeat_at=None,
                        lease_expires_at=None,
                        updated_at=db_now,
                    )
                )
                if retry_result.rowcount != 1:
                    session.rollback()
                    return self._linked_conflict(
                        session=session,
                        retry_mutation_event_id=retry_mutation_event_id,
                    )
                if self.fail_after_retry_update_for_test:
                    raise RuntimeError("Injected linked retry transition failure.")
                original_result = session.exec(
                    update(CliMutationLedger)
                    .where(_MUTATION_EVENT_ID == original_mutation_event_id)
                    .where(_STATUS == MutationStatus.RECOVERY_REQUIRED.value)
                    .where(_LEASE_OWNER == original_recovery_lease_owner)
                    .values(
                        status=MutationStatus.SUPERSEDED.value,
                        superseded_by_mutation_event_id=retry_mutation_event_id,
                        response_json=_json_dump(original_replay_response),
                        lease_owner=None,
                        lease_acquired_at=None,
                        last_heartbeat_at=None,
                        lease_expires_at=None,
                        updated_at=db_now,
                    )
                )
                if original_result.rowcount != 1:
                    session.rollback()
                    return self._linked_conflict(
                        session=session,
                        retry_mutation_event_id=retry_mutation_event_id,
                    )
                session.commit()
            except RuntimeError:
                session.rollback()
                return self._linked_conflict(
                    session=session,
                    retry_mutation_event_id=retry_mutation_event_id,
                )
            row = self._refreshed_row(
                session=session,
                mutation_event_id=retry_mutation_event_id,
            )
            return LedgerLoadResult(ledger=row, response=retry_response)

    @staticmethod
    def _refreshed_row(
        *,
        session: Session,
        mutation_event_id: int,
    ) -> CliMutationLedger:
        row = session.get(CliMutationLedger, mutation_event_id)
        if row is None:
            message = f"Mutation event {mutation_event_id} not found."
            raise ValueError(message)
        return row

    def _linked_conflict(
        self,
        *,
        session: Session,
        retry_mutation_event_id: int,
    ) -> LedgerLoadResult:
        row = self._refreshed_row(
            session=session,
            mutation_event_id=retry_mutation_event_id,
        )
        return LedgerLoadResult(ledger=row, error_code=MUTATION_RESUME_CONFLICT)

    def require_active_owner(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        now: datetime,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Verify ownership immediately before a declared side-effect write."""
        return self.heartbeat(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            now=now,
            lease_seconds=lease_seconds,
        )

    def mark_step_complete(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        step: str,
        next_step: str,
        now: datetime,
    ) -> bool:
        """Record a completed step with owner fencing."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            row = session.get(CliMutationLedger, mutation_event_id)
            if (
                row is None
                or row.status != MutationStatus.PENDING.value
                or row.lease_owner != lease_owner
                or row.lease_expires_at is None
                or row.lease_expires_at <= db_now
            ):
                return False

            steps = _completed_steps(row)
            if step not in steps:
                steps.append(step)
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .where(_LEASE_OWNER == lease_owner)
                .where(_LEASE_EXPIRES_AT > db_now)
                .values(
                    completed_steps_json=_json_dump(steps),
                    current_step=next_step,
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def finalize_success(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        after: dict[str, Any],
        response: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Store final response when the caller still owns a pending lease."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .where(_LEASE_OWNER == lease_owner)
                .where(_LEASE_EXPIRES_AT > db_now)
                .values(
                    status=MutationStatus.SUCCEEDED.value,
                    after_json=_json_dump(after),
                    response_json=_json_dump(response),
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

    def _force_recovery_required_for_test(
        self,
        *,
        mutation_event_id: int,
        recovery_action: RecoveryAction,
        safe_to_auto_resume: bool,
        last_error: dict[str, Any],
        now: datetime,
    ) -> None:
        """Force a pending row into recovery-required state for tests."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .values(
                    status=MutationStatus.RECOVERY_REQUIRED.value,
                    recovery_action=recovery_action.value,
                    recovery_safe_to_auto_resume=safe_to_auto_resume,
                    last_error_json=_json_dump(last_error),
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=db_now,
                )
            )
            session.commit()
            if result.rowcount != 1:
                row = session.get(CliMutationLedger, mutation_event_id)
                if row is None:
                    message = f"Mutation event {mutation_event_id} not found."
                    raise ValueError(message)
                message = f"Mutation event {mutation_event_id} was not pending."
                raise RuntimeError(message)

    def acquire_resume_lease(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        now: datetime,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> LedgerLoadResult:
        """Acquire a recovery lease by compare-and-set."""
        return self.transition_status(
            mutation_event_id=mutation_event_id,
            expected_status=MutationStatus.RECOVERY_REQUIRED,
            expected_lease_owner=None,
            new_status=MutationStatus.PENDING,
            new_lease_owner=lease_owner,
            now=now,
            lease_seconds=lease_seconds,
        )


def _success_result(data: dict[str, Any]) -> dict[str, Any]:
    """Return a standard successful service envelope."""
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error_result(
    *,
    code: ErrorCode | str,
    details: dict[str, Any],
    remediation: list[str],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a standard failed service envelope using registered errors."""
    return {
        "ok": False,
        "data": data,
        "warnings": [],
        "errors": [
            workbench_error(
                code,
                details=details,
                remediation=remediation,
            ).to_dict()
        ],
    }


def _resume_lease_owner(correlation_id: str | None) -> str:
    """Return the deterministic CLI lease owner for recovery acquisition."""
    if correlation_id:
        return f"{DEFAULT_CLI_RESUME_LEASE_OWNER}:{correlation_id}"
    return DEFAULT_CLI_RESUME_LEASE_OWNER


def _timestamp_payload(value: datetime | None) -> str | None:
    """Return a JSON timestamp for a stored DB datetime."""
    if value is None:
        return None
    return _utc_isoformat(value)


def _authority_curation_trace_payload(*, mutation_event_id: int) -> dict[str, Any]:
    """Return fail-open trace metadata for an authority curation mutation."""
    payload: dict[str, Any] = {
        "trace_artifact_id": None,
        "trace_artifact_present": None,
        "last_trace_step": None,
        "last_trace_status": None,
    }
    try:
        from utils.authority_curation_trace import (  # noqa: PLC0415
            summarize_trace,
            trace_artifact_path,
        )

        payload.update(summarize_trace(mutation_event_id=mutation_event_id))
        payload["trace_artifact_present"] = trace_artifact_path(
            mutation_event_id
        ).exists()
    except (OSError, UnicodeError):
        return payload
    return payload


def _row_payload(
    row: CliMutationLedger,
    *,
    include_trace_metadata: bool = False,
) -> dict[str, Any]:
    """Return a JSON-friendly mutation ledger row payload."""
    payload: dict[str, Any] = {
        "mutation_event_id": row.mutation_event_id,
        "command": row.command,
        "idempotency_key": row.idempotency_key,
        "request_hash": row.request_hash,
        "project_id": row.project_id,
        "correlation_id": row.correlation_id,
        "changed_by": row.changed_by,
        "status": row.status,
        "current_step": row.current_step,
        "completed_steps": _completed_steps(row),
        "guard_inputs": _json_blob(row.guard_inputs_json),
        "before": _json_blob(row.before_json),
        "after": _json_blob(row.after_json),
        "response": _json_blob(row.response_json),
        "recovery_action": row.recovery_action,
        "recovery_safe_to_auto_resume": row.recovery_safe_to_auto_resume,
        "lease_owner": row.lease_owner,
        "lease_acquired_at": _timestamp_payload(row.lease_acquired_at),
        "last_heartbeat_at": _timestamp_payload(row.last_heartbeat_at),
        "lease_expires_at": _timestamp_payload(row.lease_expires_at),
        "created_at": _timestamp_payload(row.created_at),
        "updated_at": _timestamp_payload(row.updated_at),
        "last_error": _json_blob(row.last_error_json),
    }
    if row.recovers_mutation_event_id is not None:
        payload["recovers_mutation_event_id"] = row.recovers_mutation_event_id
    if row.superseded_by_mutation_event_id is not None:
        payload["superseded_by_mutation_event_id"] = (
            row.superseded_by_mutation_event_id
        )
    if (
        include_trace_metadata
        and row.command == "agileforge authority curate"
        and row.mutation_event_id is not None
    ):
        payload.update(
            _authority_curation_trace_payload(
                mutation_event_id=row.mutation_event_id
            )
        )
    return payload
