"""Read-only replay of durable node-attempt command receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from models.workflow import (
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from workflow.contracts import (
    FrozenModel,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_hash
from workflow.requests import StartNodeAttempt, TransitionRequest

_TRANSITION_REQUEST = TypeAdapter(TransitionRequest)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class NodeAttemptReplayQuery(FrozenModel):
    """Identity of a host-prepared attempt, excluding its persisted input."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    node_id: str
    instance_key: str | None = None
    idempotency_key: str
    actor: str
    correlation_id: str | None = None


@dataclass(frozen=True)
class DurableNodeAttemptReplayService:
    """Recover persisted attempt results without rebuilding prepared input."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Return one matching in-flight or terminal receipt when it exists."""
        with Session(self.engine) as session:
            receipt = session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.request_kind) == "start_node_attempt",
                    col(WorkflowTransitionReceipt.idempotency_key)
                    == query.idempotency_key,
                )
            ).one_or_none()
            if receipt is None:
                return None
            stored = StartNodeAttempt.model_validate_json(receipt.request_json)
            expected = StartNodeAttempt(
                project_id=query.project_id,
                graph_version=query.graph_version,
                fact_fingerprint=query.fact_fingerprint,
                decision_fingerprint=query.decision_fingerprint,
                idempotency_key=query.idempotency_key,
                actor=query.actor,
                correlation_id=query.correlation_id,
                target_node_id=query.node_id,
                target_instance_key=stored.target_instance_key,
                normalized_input=stored.normalized_input,
                model_id=stored.model_id,
                execution_settings=stored.execution_settings,
                lease_seconds=stored.lease_seconds,
            )
            if canonical_hash(expected.model_dump(mode="json")) != (
                receipt.request_fingerprint
            ):
                return _fact_conflict(
                    "The idempotency key was already used for different input."
                )
            if receipt.result_json is None or receipt.completed_at is None:
                return _fact_conflict("The idempotency receipt is incomplete.")
            return _replay_attempt_result(session, receipt, stored)


@dataclass(frozen=True)
class DurableTransitionReplayService:
    """Recover one completed host request before any current-state read."""

    engine: Engine

    def replay(
        self,
        *,
        request_kind: str,
        project_id: int,
        idempotency_key: str,
        actor: str,
        correlation_id: str | None,
    ) -> TransitionResult | None:
        """Return a compatible terminal receipt or no result for a new request."""
        with Session(self.engine) as session:
            receipt = session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.request_kind) == request_kind,
                    col(WorkflowTransitionReceipt.idempotency_key) == idempotency_key,
                )
            ).one_or_none()
            if receipt is None:
                return None
            stored = _TRANSITION_REQUEST.validate_json(receipt.request_json)
            if (
                stored.project_id != project_id
                or stored.actor != actor
                or stored.correlation_id != correlation_id
            ):
                return _fact_conflict(
                    "The idempotency key was already used for different input."
                )
            if receipt.result_json is None or receipt.completed_at is None:
                return _fact_conflict("The idempotency receipt is incomplete.")
            return _replayed_result(receipt)


def _replay_attempt_result(
    session: Session,
    receipt: WorkflowTransitionReceipt,
    stored: StartNodeAttempt,
) -> TransitionResult:
    """Return the terminal outcome for a completed attempt or its start receipt."""
    attempt = session.exec(
        select(WorkflowNodeAttempt).where(
            col(WorkflowNodeAttempt.project_id) == stored.project_id,
            col(WorkflowNodeAttempt.idempotency_key) == stored.idempotency_key,
        )
    ).one_or_none()
    if attempt is None:
        return _replayed_result(receipt)
    outcome = session.exec(
        select(WorkflowNodeAttemptOutcome).where(
            col(WorkflowNodeAttemptOutcome.project_id) == attempt.project_id,
            col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id)
            == attempt.workflow_node_attempt_id,
        )
    ).one_or_none()
    if outcome is None:
        return _replayed_result(receipt)
    completion_receipt = _terminal_receipt(session, attempt)
    if completion_receipt is None:
        return _fact_conflict("The terminal node attempt has no completion receipt.")
    return _replayed_result(completion_receipt)


def _terminal_receipt(
    session: Session,
    attempt: WorkflowNodeAttempt,
) -> WorkflowTransitionReceipt | None:
    """Return the persisted terminal transition matching one durable attempt."""
    attempt_id = attempt.workflow_node_attempt_id
    if attempt_id is None:
        return None
    receipts = session.exec(
        select(WorkflowTransitionReceipt).where(
            col(WorkflowTransitionReceipt.completed_at).is_not(None)
        )
    ).all()
    for receipt in receipts:
        request = _TRANSITION_REQUEST.validate_json(receipt.request_json)
        if (
            request.project_id == attempt.project_id
            and getattr(request, "attempt_id", None) == attempt_id
            and getattr(request, "attempt_fingerprint", None)
            == attempt.attempt_fingerprint
        ):
            return receipt
    return None


def _replayed_result(receipt: WorkflowTransitionReceipt) -> TransitionResult:
    """Decode one completed durable receipt as an idempotent replay."""
    if receipt.result_json is None:
        message = "The idempotency receipt is incomplete."
        raise RuntimeError(message)
    persisted = TransitionResult.model_validate_json(receipt.result_json)
    return persisted.model_copy(update={"replayed": True})


def _fact_conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


__all__ = [
    "DurableNodeAttemptReplayService",
    "DurableTransitionReplayService",
    "NodeAttemptReplayQuery",
]
