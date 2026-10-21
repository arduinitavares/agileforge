# workflow/attempt_ownership.py
"""Host-only owner identity and immutable-attempt integrity for renewable leases."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from models.workflow import WorkflowNodeAttemptOwnership
from workflow.fingerprints import canonical_hash, workflow_node_attempt_fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlmodel import Session

    from models.workflow import WorkflowNodeAttempt


def as_utc(value: datetime) -> datetime:
    """Normalize SQLite's stored timezone before comparing owner authority."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AttemptOwnershipLostError(RuntimeError):
    """The host no longer has authority to send, renew or publish this attempt."""

    def __init__(self) -> None:
        """Use safe fixed failure copy for lost local authority."""
        super().__init__("The node attempt owner is no longer authoritative.")


@dataclass(frozen=True)
class AttemptOwnershipGuard:
    """One local host generation; never part of caller or receipt identity."""

    project_id: int
    attempt_id: int
    attempt_fingerprint: str
    owner_generation: str
    lease_seconds: float


_OWNER: ContextVar[AttemptOwnershipGuard | None] = ContextVar(
    "attempt_owner", default=None
)


@contextmanager
def bind_attempt_ownership(guard: AttemptOwnershipGuard) -> Iterator[None]:
    """Carry admitted host authority through local synchronous/async calls."""
    token = _OWNER.set(guard)
    try:
        yield
    finally:
        _OWNER.reset(token)


def current_attempt_ownership() -> AttemptOwnershipGuard | None:
    """Return the current host guard without any storage or configuration reads."""
    return _OWNER.get()


def immutable_attempt_matches(row: WorkflowNodeAttempt) -> bool:
    """Validate the original input, settings and lease without rewriting them."""
    try:
        normalized_input = json.loads(row.normalized_input_json)
        execution_settings = json.loads(row.execution_settings_json)
        identity = {
            "attempt_id": row.workflow_node_attempt_id,
            "project_id": row.project_id,
            "node_id": row.node_id,
            "instance_key": row.instance_key,
            "graph_version": row.graph_version,
            "fact_fingerprint": row.fact_fingerprint,
            "business_fact_fingerprint": row.business_fact_fingerprint,
            "decision_fingerprint": row.decision_fingerprint,
            "normalized_input": normalized_input,
            "input_fingerprint": row.input_fingerprint,
            "model_id": row.model_id,
            "execution_settings": execution_settings,
            "idempotency_key": row.idempotency_key,
            "actor": row.actor,
            "correlation_id": row.correlation_id,
            "started_at": as_utc(row.started_at),
            "lease_expires_at": as_utc(row.lease_expires_at),
        }
        return (
            canonical_hash(normalized_input) == row.input_fingerprint
            and workflow_node_attempt_fingerprint(identity) == row.attempt_fingerprint
        )
    except (ValueError, TypeError):
        return False


def load_attempt_ownership(
    session: Session, row: WorkflowNodeAttempt
) -> WorkflowNodeAttemptOwnership | None:
    """Validate separately governed ownership, with legacy expiry as the fallback."""
    owner = session.get(WorkflowNodeAttemptOwnership, row.workflow_node_attempt_id)
    if owner is not None and (
        owner.project_id != row.project_id
        or owner.attempt_fingerprint != row.attempt_fingerprint
        or not owner.owner_generation
        or owner.renewal_revision < 0
        or as_utc(owner.acquired_at) < as_utc(row.started_at)
        or as_utc(owner.expires_at) <= as_utc(owner.acquired_at)
    ):
        raise AttemptOwnershipLostError
    return owner


def effective_attempt_expiry(session: Session, row: WorkflowNodeAttempt) -> datetime:
    """Project effective authority without modifying the original attempt lease."""
    owner = load_attempt_ownership(session, row)
    return as_utc(row.lease_expires_at if owner is None else owner.expires_at)


def owner_guard_matches(session: Session, row: WorkflowNodeAttempt) -> bool:
    """Require the exact host generation for new writes on an owned attempt."""
    owner = load_attempt_ownership(session, row)
    if owner is None:
        return True
    guard = current_attempt_ownership()
    return guard is not None and (
        guard.project_id == row.project_id
        and guard.attempt_id == row.workflow_node_attempt_id
        and guard.attempt_fingerprint == row.attempt_fingerprint
        and guard.owner_generation == owner.owner_generation
    )
