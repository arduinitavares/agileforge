# repositories/provider_attempts.py
"""Independently committed, serialized audit for physical provider tries."""

from __future__ import annotations

from hashlib import sha256
from http import HTTPStatus
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import case, func, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, col, select

from models.core import Project
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import WorkflowNodeAttempt
from services.contracts.provider_retry import (
    ProviderAuditError,
    ProviderFailureSummary,
    ProviderTryAuditRecord,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.sql.elements import ColumnElement

_EVENT_TYPES: tuple[WorkflowEventType, ...] = (
    WorkflowEventType.PROVIDER_TRY_STARTED,
    WorkflowEventType.PROVIDER_TRY_FINISHED,
)
_CALL_IDENTITY_FIELDS: frozenset[str] = frozenset(
    {
        "project_id",
        "action_id",
        "call_id",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "node_id",
        "instance_key",
        "idempotency_key_digest",
        "model_id",
        "request_fingerprint",
        "max_attempts",
        "retry_config",
    }
)
_MAX_CALL_EVENT_ROWS: int = 20
_ACTION_IDENTITY_FIELDS: frozenset[str] = _CALL_IDENTITY_FIELDS - {
    "call_id",
    "model_id",
    "request_fingerprint",
    "max_attempts",
    "retry_config",
}
_WRITE_BUSY_TIMEOUT_MS: int = 1000
_TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _call_identity(record: ProviderTryAuditRecord) -> dict[str, object]:
    return record.model_dump(include=set(_CALL_IDENTITY_FIELDS))


def _start_identity(record: ProviderTryAuditRecord) -> dict[str, object]:
    return record.model_dump(
        include=set(_CALL_IDENTITY_FIELDS) | {"try_ordinal", "started_at"}
    )


class ProviderAttemptAuditRepository:
    """Store start/finish pairs in existing events without changing business facts."""

    def __init__(self, engine: Engine) -> None:
        """Use only the explicitly supplied business engine and owned sessions."""
        self._engine: Engine = engine

    def append_started(self, record: ProviderTryAuditRecord) -> None:
        """Commit the host's safe start record before any physical send."""
        self._append(record, finished=False)

    def append_finished(self, record: ProviderTryAuditRecord) -> None:
        """Commit truthful finish facts independently of the recipe transaction."""
        self._append(record, finished=True)

    def _append(self, record: ProviderTryAuditRecord, *, finished: bool) -> None:
        try:
            record = ProviderTryAuditRecord.model_validate(record.model_dump())
            if (record.finished_at is not None) != finished:
                raise ProviderAuditError
            if self._engine.dialect.name != "sqlite":
                raise ProviderAuditError
            with self._engine.connect() as connection:
                original_timeout = connection.exec_driver_sql(
                    "PRAGMA busy_timeout"
                ).scalar_one()
                try:
                    connection.exec_driver_sql(
                        f"PRAGMA busy_timeout={_WRITE_BUSY_TIMEOUT_MS}"
                    )
                    # Reserve SQLite's one writer before reading the identity.
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    with Session(connection) as session:
                        self._append_in_transaction(session, record, finished=finished)
                        session.flush()
                        connection.commit()
                finally:
                    connection.rollback()
                    connection.exec_driver_sql(
                        f"PRAGMA busy_timeout={int(original_timeout)}"
                    )
        except (SQLAlchemyError, ValidationError, ValueError, TypeError):
            raise ProviderAuditError from None

    def _append_in_transaction(
        self, session: Session, record: ProviderTryAuditRecord, *, finished: bool
    ) -> None:
        _validate_host(session, record)
        _validate_action(session, record)
        records = _call_records(session, record.project_id, record.call_id)
        pairs = _paired_records(records)
        for existing in records:
            if _call_identity(existing) != _call_identity(record):
                raise ProviderAuditError
            if (
                existing.try_ordinal == record.try_ordinal
                and (existing.finished_at is not None) == finished
            ):
                if existing != record:
                    raise ProviderAuditError
                return
        if finished:
            if (
                not pairs
                or pairs[-1][1] is not None
                or _start_identity(pairs[-1][0]) != _start_identity(record)
            ):
                raise ProviderAuditError
        elif record.try_ordinal != len(pairs) + 1 or (
            pairs
            and (pairs[-1][1] is None or pairs[-1][1].disposition != "retry_scheduled")
        ):
            raise ProviderAuditError
        session.add(
            WorkflowEvent(
                event_type=(
                    WorkflowEventType.PROVIDER_TRY_FINISHED
                    if finished
                    else WorkflowEventType.PROVIDER_TRY_STARTED
                ),
                project_id=record.project_id,
                timestamp=record.finished_at if finished else record.started_at,
                duration_seconds=record.duration_seconds,
                event_metadata=record.model_dump_json(),
            )
        )

    def terminal_failure(
        self,
        *,
        project_id: int,
        action_id: str,
        call_id: str,
        expected_summary: ProviderFailureSummary | None = None,
    ) -> ProviderFailureSummary | None:
        """Verify every paired try and retain the last confirmed eligible status."""
        try:
            if expected_summary is not None:
                expected_summary = ProviderFailureSummary.model_validate(
                    expected_summary.model_dump()
                )
            with Session(self._engine) as session:
                _reject_foreign_identity(
                    session, project_id=project_id, action_id=action_id, call_id=call_id
                )
                records = _call_records(session, project_id, call_id)
                if not records:
                    if expected_summary is not None:
                        raise ProviderAuditError
                    return None
                first = records[0]
                if first.project_id != project_id or first.action_id != action_id:
                    raise ProviderAuditError
                _validate_host(session, first)
                pairs = _paired_records(records)
                derived = _terminal_summary(pairs, expected_summary=expected_summary)
                if expected_summary is not None and derived != expected_summary:
                    raise ProviderAuditError
                return derived
        except (SQLAlchemyError, ValidationError, ValueError, TypeError):
            raise ProviderAuditError from None


def _validate_host(session: Session, record: ProviderTryAuditRecord) -> None:
    if session.get(Project, record.project_id) is None:
        raise ProviderAuditError
    if record.workflow_node_attempt_id is None:
        return
    attempt = session.get(WorkflowNodeAttempt, record.workflow_node_attempt_id)
    if attempt is None or (
        attempt.project_id != record.project_id
        or attempt.attempt_fingerprint != record.attempt_fingerprint
        or attempt.node_id != record.node_id
        or attempt.instance_key != record.instance_key
        or sha256(attempt.idempotency_key.encode()).hexdigest()
        != record.idempotency_key_digest
    ):
        raise ProviderAuditError


def _validate_action(session: Session, record: ProviderTryAuditRecord) -> None:
    _reject_foreign_identity(
        session,
        project_id=record.project_id,
        action_id=record.action_id,
        call_id=record.call_id,
    )
    existing = session.exec(
        select(WorkflowEvent)
        .where(
            col(WorkflowEvent.event_type).in_(_EVENT_TYPES),
            col(WorkflowEvent.project_id) == record.project_id,
            or_(
                _invalid_metadata(),
                _safe_metadata_value("action_id") == record.action_id,
            ),
        )
        .order_by(col(WorkflowEvent.event_id))
        .limit(1)
    ).first()
    if existing is None:
        return
    if existing.event_metadata is None:
        raise ProviderAuditError
    previous = ProviderTryAuditRecord.model_validate_json(existing.event_metadata)
    if existing.project_id != previous.project_id or previous.model_dump(
        include=set(_ACTION_IDENTITY_FIELDS)
    ) != record.model_dump(include=set(_ACTION_IDENTITY_FIELDS)):
        raise ProviderAuditError


def _safe_metadata_value(field: str) -> ColumnElement[str | None]:
    metadata = col(WorkflowEvent.event_metadata)
    # CASE is lazy in SQLite; no optimizer-dependent WHERE short circuit is needed.
    safe_metadata = case((func.json_valid(metadata) == 1, metadata), else_="{}")
    return func.json_extract(safe_metadata, f"$.{field}")


def _invalid_metadata() -> ColumnElement[bool]:
    return func.coalesce(func.json_valid(col(WorkflowEvent.event_metadata)), 0) != 1


def _reject_foreign_identity(
    session: Session, *, project_id: int, action_id: str, call_id: str
) -> None:
    collision = session.exec(
        select(col(WorkflowEvent.event_id))
        .where(
            col(WorkflowEvent.event_type).in_(_EVENT_TYPES),
            or_(
                col(WorkflowEvent.project_id) != project_id,
                col(WorkflowEvent.project_id).is_(None),
            ),
            or_(
                _safe_metadata_value("action_id") == action_id,
                _safe_metadata_value("call_id") == call_id,
            ),
        )
        .limit(1)
    ).first()
    if collision is not None:
        raise ProviderAuditError


def _call_records(
    session: Session, project_id: int, call_id: str
) -> list[ProviderTryAuditRecord]:
    events = session.exec(
        select(WorkflowEvent)
        .where(
            col(WorkflowEvent.event_type).in_(_EVENT_TYPES),
            col(WorkflowEvent.project_id) == project_id,
            or_(_invalid_metadata(), _safe_metadata_value("call_id") == call_id),
        )
        .order_by(col(WorkflowEvent.event_id))
        .limit(_MAX_CALL_EVENT_ROWS + 1)
    ).all()
    if len(events) > _MAX_CALL_EVENT_ROWS:
        raise ProviderAuditError
    records: list[ProviderTryAuditRecord] = []
    for event in events:
        if event.event_metadata is None:
            raise ProviderAuditError
        record = ProviderTryAuditRecord.model_validate_json(event.event_metadata)
        if (
            event.project_id != record.project_id
            or record.call_id != call_id
            or (event.event_type == WorkflowEventType.PROVIDER_TRY_FINISHED)
            != (record.finished_at is not None)
        ):
            raise ProviderAuditError
        records.append(record)
    return records


def _paired_records(
    records: list[ProviderTryAuditRecord],
) -> list[tuple[ProviderTryAuditRecord, ProviderTryAuditRecord | None]]:
    pairs: list[tuple[ProviderTryAuditRecord, ProviderTryAuditRecord | None]] = []
    for record in records:
        if _call_identity(record) != _call_identity(records[0]):
            raise ProviderAuditError
        if record.finished_at is None:
            if record.try_ordinal != len(pairs) + 1 or (
                pairs
                and (
                    pairs[-1][1] is None
                    or pairs[-1][1].disposition != "retry_scheduled"
                )
            ):
                raise ProviderAuditError
            pairs.append((record, None))
        else:
            if (
                not pairs
                or pairs[-1][1] is not None
                or _start_identity(pairs[-1][0]) != _start_identity(record)
            ):
                raise ProviderAuditError
            pairs[-1] = (pairs[-1][0], record)
    return pairs


def _terminal_summary(
    pairs: list[tuple[ProviderTryAuditRecord, ProviderTryAuditRecord | None]],
    *,
    expected_summary: ProviderFailureSummary | None = None,
) -> ProviderFailureSummary | None:
    if not pairs:
        return None
    first = pairs[0][0]
    terminal = pairs[-1][1]
    if terminal is None:
        return None
    sent_pairs = pairs
    if terminal.disposition == "not_sent":
        if len(pairs) <= 1:
            raise ProviderAuditError
        sent_pairs = pairs[:-1]
        termination_reason = terminal.termination_reason
    elif terminal.disposition == "exhausted":
        termination_reason = terminal.termination_reason
    elif (
        terminal.disposition == "retry_scheduled"
        and expected_summary is not None
        and expected_summary.termination_reason == "retry_budget_exhausted"
    ):
        termination_reason = expected_summary.termination_reason
    else:
        return None
    if termination_reason == "attempts_exhausted" and (
        len(sent_pairs) != first.max_attempts or terminal.http_status is None
    ):
        raise ProviderAuditError
    confirmed = [
        finish
        for _, finish in sent_pairs
        if finish is not None and finish.http_status in _TRANSIENT_STATUSES
    ]
    if not confirmed:
        raise ProviderAuditError
    status = confirmed[-1].http_status
    if status is None or termination_reason is None:
        raise ProviderAuditError
    return ProviderFailureSummary(
        reason="rate_limited"
        if status == HTTPStatus.TOO_MANY_REQUESTS
        else "unavailable",
        termination_reason=termination_reason,
        http_status=status,
        call_id=first.call_id,
        attempts=len(sent_pairs),
        max_attempts=first.max_attempts,
        retry_after_seconds=confirmed[-1].retry_after_seconds,
        manual_retry_requires_new_key=first.idempotency_key_digest is not None,
    )
