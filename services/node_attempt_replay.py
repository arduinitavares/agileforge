"""Read-only replay of durable node-attempt command receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.workflow import WorkflowTransitionReceipt
from workflow.contracts import (
    FrozenModel,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_hash
from workflow.requests import StartNodeAttempt

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
                target_instance_key=query.instance_key,
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


__all__ = ["DurableNodeAttemptReplayService", "NodeAttemptReplayQuery"]
