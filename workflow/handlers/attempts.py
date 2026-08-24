"""Transaction-bound durable node-attempt persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from workflow.contracts import NodeDecision, TransitionResult
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    workflow_node_attempt_fingerprint,
)

if TYPE_CHECKING:
    from workflow.requests import FailNodeAttempt, StartNodeAttempt


@dataclass(frozen=True)
class AttemptStartState:
    """Derived authority state required to persist one attempt."""

    business_fingerprint: str
    expired_attempt_id: int | None


def as_utc(value: datetime) -> datetime:
    """Normalize persisted SQLite datetimes for lease comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def load_attempt(
    session: Session,
    *,
    project_id: int,
    attempt_id: int,
) -> WorkflowNodeAttempt | None:
    """Load one attempt only through its same-Project identity."""
    return session.exec(
        select(WorkflowNodeAttempt).where(
            col(WorkflowNodeAttempt.project_id) == project_id,
            col(WorkflowNodeAttempt.workflow_node_attempt_id) == attempt_id,
        )
    ).one_or_none()


def load_attempt_outcome(
    session: Session,
    *,
    project_id: int,
    attempt_id: int,
) -> WorkflowNodeAttemptOutcome | None:
    """Load the single terminal outcome for an attempt."""
    return session.exec(
        select(WorkflowNodeAttemptOutcome).where(
            col(WorkflowNodeAttemptOutcome.project_id) == project_id,
            col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id) == attempt_id,
        )
    ).one_or_none()


def execute_start_node_attempt(
    session: Session,
    request: StartNodeAttempt,
    decision: NodeDecision,
    evaluated_at: datetime,
    state: AttemptStartState,
) -> TransitionResult:
    """Obsolete an expired lease and create its durable replacement."""
    if state.expired_attempt_id is not None:
        record_obsolete_outcome(
            session,
            project_id=request.project_id,
            attempt_id=state.expired_attempt_id,
            evaluated_at=evaluated_at,
        )
    normalized_input_json = canonical_json(request.normalized_input)
    execution_settings_json = canonical_json(request.execution_settings)
    lease_expires_at = evaluated_at + timedelta(seconds=request.lease_seconds)
    row = WorkflowNodeAttempt(
        project_id=request.project_id,
        node_id=request.target_node_id,
        instance_key=request.target_instance_key,
        graph_version=request.graph_version,
        fact_fingerprint=request.fact_fingerprint,
        business_fact_fingerprint=state.business_fingerprint,
        decision_fingerprint=request.decision_fingerprint,
        normalized_input_json=normalized_input_json,
        input_fingerprint=canonical_hash(request.normalized_input),
        model_id=request.model_id,
        execution_settings_json=execution_settings_json,
        idempotency_key=request.idempotency_key,
        actor=request.actor,
        correlation_id=request.correlation_id,
        started_at=evaluated_at,
        lease_expires_at=lease_expires_at,
        attempt_fingerprint="pending",
    )
    session.add(row)
    session.flush()
    if row.workflow_node_attempt_id is None:
        msg = "Workflow node attempt primary key was not assigned."
        raise RuntimeError(msg)
    row.attempt_fingerprint = workflow_node_attempt_fingerprint(
        {
            "attempt_id": row.workflow_node_attempt_id,
            "project_id": row.project_id,
            "node_id": row.node_id,
            "instance_key": row.instance_key,
            "graph_version": row.graph_version,
            "fact_fingerprint": row.fact_fingerprint,
            "business_fact_fingerprint": row.business_fact_fingerprint,
            "decision_fingerprint": row.decision_fingerprint,
            "normalized_input": request.normalized_input,
            "input_fingerprint": row.input_fingerprint,
            "model_id": row.model_id,
            "execution_settings": request.execution_settings,
            "idempotency_key": row.idempotency_key,
            "actor": row.actor,
            "correlation_id": row.correlation_id,
            "started_at": evaluated_at,
            "lease_expires_at": lease_expires_at,
        }
    )
    session.add(row)
    session.flush()
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={
            "attempt_id": row.workflow_node_attempt_id,
            "attempt_fingerprint": row.attempt_fingerprint,
            "lease_expires_at": lease_expires_at.isoformat(),
        },
    )


def record_success_outcome(
    session: Session,
    *,
    project_id: int,
    attempt_id: int,
    output: object,
    evaluated_at: datetime,
) -> None:
    """Record the canonical downstream handler output as attempt success."""
    output_json = canonical_json(output)
    session.add(
        WorkflowNodeAttemptOutcome(
            project_id=project_id,
            workflow_node_attempt_id=attempt_id,
            status="success",
            output_fingerprint=canonical_hash(output),
            output_json=output_json,
            recorded_at=evaluated_at,
        )
    )
    session.flush()


def record_failure_outcome(
    session: Session,
    request: FailNodeAttempt,
    evaluated_at: datetime,
) -> None:
    """Record one external execution failure without a business artifact."""
    session.add(
        WorkflowNodeAttemptOutcome(
            project_id=request.project_id,
            workflow_node_attempt_id=request.attempt_id,
            status="failure",
            failure_code=request.failure_code,
            failure_message=request.failure_message,
            recorded_at=evaluated_at,
        )
    )
    session.flush()


def record_obsolete_outcome(
    session: Session,
    *,
    project_id: int,
    attempt_id: int,
    evaluated_at: datetime,
) -> None:
    """Terminate a stale attempt without granting it business authority."""
    if load_attempt_outcome(
        session,
        project_id=project_id,
        attempt_id=attempt_id,
    ) is not None:
        return
    session.add(
        WorkflowNodeAttemptOutcome(
            project_id=project_id,
            workflow_node_attempt_id=attempt_id,
            status="obsolete",
            recorded_at=evaluated_at,
        )
    )
    session.flush()


__all__ = [
    "AttemptStartState",
    "as_utc",
    "execute_start_node_attempt",
    "load_attempt",
    "load_attempt_outcome",
    "record_failure_outcome",
    "record_obsolete_outcome",
    "record_success_outcome",
]
