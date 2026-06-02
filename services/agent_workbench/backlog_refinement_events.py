"""Append-only approval events for backlog refinement attempts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import update
from sqlalchemy.engine import Engine

from models.agent_workbench import CliMutationLedger
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    IDEMPOTENCY_KEY_REUSED,
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlmodel import Session

BACKLOG_REFINEMENT_APPROVE_COMMAND = "agileforge backlog approve"


class BacklogRefinementApprovalError(Exception):
    """Raised when backlog refinement approval cannot be recorded."""


class BacklogRefinementApprovalRequest(BaseModel):
    """Host-mediated approval request for a refined backlog artifact."""

    model_config = ConfigDict(extra="forbid")

    project_id: Annotated[int, Field(gt=0)]
    source_attempt_id: str | None = Field(default=None, min_length=1)
    attempt_id: str | None = Field(default=None, min_length=1)
    operation_set_fingerprint: str | None = Field(default=None, min_length=1)
    approved_artifact_fingerprint: Annotated[str, Field(min_length=1)]
    approved_operation_ids: list[str] = Field(default_factory=list)
    approval_source: Annotated[str, Field(min_length=1)] = "cli"
    idempotency_key: Annotated[str, Field(min_length=1)]
    approved_by: Annotated[str, Field(min_length=1)] = "po"

    @field_validator(
        "source_attempt_id",
        "attempt_id",
        "operation_set_fingerprint",
        "approved_artifact_fingerprint",
        "approval_source",
        "idempotency_key",
        "approved_by",
        mode="before",
    )
    @classmethod
    def _strip_string_fields(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("approved_operation_ids")
    @classmethod
    def _canonical_approved_operation_ids(cls, value: list[str]) -> list[str]:
        canonical_ids = [operation_id.strip() for operation_id in value]
        if any(not operation_id for operation_id in canonical_ids):
            message = "approved_operation_ids cannot contain blank ids"
            raise ValueError(message)
        if len(set(canonical_ids)) != len(canonical_ids):
            message = "approved_operation_ids must be unique"
            raise ValueError(message)
        return sorted(canonical_ids)

    @model_validator(mode="after")
    def _has_one_attempt_identifier(self) -> BacklogRefinementApprovalRequest:
        if bool(self.source_attempt_id) == bool(self.attempt_id):
            message = "exactly one of source_attempt_id or attempt_id is required"
            raise ValueError(message)
        return self


def _approval_request_payload(
    request: BacklogRefinementApprovalRequest,
) -> dict[str, object]:
    """Return host boundary fields used for approval recording and replay."""
    return {
        "command": BACKLOG_REFINEMENT_APPROVE_COMMAND,
        "project_id": request.project_id,
        "source_attempt_id": request.source_attempt_id,
        "attempt_id": request.attempt_id,
        "operation_set_fingerprint": request.operation_set_fingerprint,
        "approved_artifact_fingerprint": request.approved_artifact_fingerprint,
        "approved_operation_ids": request.approved_operation_ids,
        "approval_source": request.approval_source,
        "idempotency_key": request.idempotency_key,
        "approved_by": request.approved_by,
    }


def approval_request_fingerprint(
    request: BacklogRefinementApprovalRequest,
) -> str:
    """Return canonical request fingerprint for approval idempotency."""
    return canonical_hash(_approval_request_payload(request))


def _parse_now(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _db_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _json_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _ledger_repository(session: Session) -> MutationLedgerRepository:
    bind = session.get_bind()
    if not isinstance(bind, Engine):
        message = "Approval recording requires a session bound to an Engine."
        raise BacklogRefinementApprovalError(message)
    return MutationLedgerRepository(engine=bind)


def _approval_id(request_fingerprint: str) -> str:
    return "approval:" + request_fingerprint.removeprefix("sha256:")[:16]


def _replayed_approval_response(
    response: dict[str, object],
    *,
    request_fingerprint: str,
) -> dict[str, object]:
    approval_id = response.get("approval_id")
    if (
        not isinstance(approval_id, str)
        or not approval_id
        or response.get("request_fingerprint") != request_fingerprint
    ):
        message = "Existing approval ledger response is invalid."
        raise BacklogRefinementApprovalError(message)
    return {
        "approval_id": approval_id,
        "request_fingerprint": request_fingerprint,
        "idempotent_replay": True,
    }


def _finalize_approval_success(
    session: Session,
    *,
    mutation_event_id: int,
    lease_owner: str,
    payload: dict[str, object],
    now: datetime,
) -> bool:
    db_now = _db_datetime(now)
    mutation_event_id_col = cast("Any", CliMutationLedger.mutation_event_id)
    status_col = cast("Any", CliMutationLedger.status)
    lease_owner_col = cast("Any", CliMutationLedger.lease_owner)
    lease_expires_at_col = cast("Any", CliMutationLedger.lease_expires_at)
    after_payload = cast("dict[str, object]", payload["after"])
    response_payload = cast("dict[str, object]", payload["response"])
    result = session.exec(
        update(CliMutationLedger)
        .where(mutation_event_id_col == mutation_event_id)
        .where(status_col == MutationStatus.PENDING.value)
        .where(lease_owner_col == lease_owner)
        .where(lease_expires_at_col > db_now)
        .values(
            status=MutationStatus.SUCCEEDED.value,
            after_json=_json_dump(after_payload),
            response_json=_json_dump(response_payload),
            recovery_action=RecoveryAction.NONE.value,
            recovery_safe_to_auto_resume=False,
            lease_owner=None,
            lease_acquired_at=None,
            last_heartbeat_at=None,
            lease_expires_at=None,
            updated_at=db_now,
        )
    )
    return result.rowcount == 1


def record_backlog_refinement_approval(
    session: Session,
    *,
    request: BacklogRefinementApprovalRequest,
    now_iso: Callable[[], str],
) -> dict[str, object]:
    """Record or replay a host-mediated refinement approval event."""
    approved_at = now_iso()
    now = _parse_now(approved_at)
    request_fingerprint = approval_request_fingerprint(request)
    approval_id = _approval_id(request_fingerprint)
    ledger = _ledger_repository(session)
    lease_owner = f"backlog-refinement-approval:{request.idempotency_key}"
    loaded = ledger.create_or_load(
        command=BACKLOG_REFINEMENT_APPROVE_COMMAND,
        idempotency_key=request.idempotency_key,
        request_hash=request_fingerprint,
        project_id=request.project_id,
        correlation_id=approval_id,
        changed_by=request.approved_by,
        lease_owner=lease_owner,
        now=now,
    )
    if loaded.response is not None:
        return _replayed_approval_response(
            loaded.response,
            request_fingerprint=request_fingerprint,
        )
    if loaded.replayed:
        message = "Existing approval ledger response is invalid."
        raise BacklogRefinementApprovalError(message)
    if loaded.error_code is not None:
        if loaded.error_code == IDEMPOTENCY_KEY_REUSED:
            message = "Idempotency key reused with different approval inputs."
        else:
            message = (
                f"Approval idempotency ledger blocked request: {loaded.error_code}"
            )
        raise BacklogRefinementApprovalError(message)

    metadata = {
        "action": "backlog_refinement_approved",
        "approval_id": approval_id,
        "request_fingerprint": request_fingerprint,
        "approved_at": approved_at,
        **_approval_request_payload(request),
    }
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_REFINEMENT_APPROVED,
            product_id=request.project_id,
            session_id=str(request.project_id),
            event_metadata=json.dumps(metadata, sort_keys=True),
        )
    )
    response = {
        "approval_id": approval_id,
        "request_fingerprint": request_fingerprint,
        "idempotent_replay": False,
    }
    replay_response = {**response, "idempotent_replay": True}
    event_id = loaded.ledger.mutation_event_id
    if event_id is None:
        session.rollback()
        message = "Mutation ledger row has no primary key."
        raise BacklogRefinementApprovalError(message)
    if not _finalize_approval_success(
        session,
        mutation_event_id=event_id,
        lease_owner=lease_owner,
        payload={
            "after": {
                "approval_id": approval_id,
                "request_fingerprint": request_fingerprint,
            },
            "response": replay_response,
        },
        now=now,
    ):
        session.rollback()
        message = "Approval event recorded but ledger finalization failed."
        raise BacklogRefinementApprovalError(message)
    session.commit()
    return {
        "approval_id": approval_id,
        "request_fingerprint": request_fingerprint,
        "idempotent_replay": False,
    }
