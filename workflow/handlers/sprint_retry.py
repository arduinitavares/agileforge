"""Transactional dispatch for guarded Sprint retry operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from repositories.workflow import WorkflowFactRepository
from services.sprint_retry import retry_sprint_in_session, start_sprint_retry_in_session
from workflow.contracts import TransitionResult, WorkflowError, WorkflowErrorCode
from workflow.requests.sprint_retry import RetrySprint, StartSprintRetry

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

    from workflow.contracts import NodeDecision


def execute_sprint_retry_request(
    session: Session,
    request: RetrySprint | StartSprintRetry,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Reload authority after receipt claim and apply the selected retry action."""
    snapshot = WorkflowFactRepository(session).load(request.project_id)
    try:
        result = (
            retry_sprint_in_session(
                session, request=request, snapshot=snapshot, now=evaluated_at
            )
            if isinstance(request, RetrySprint)
            else start_sprint_retry_in_session(
                session, request=request, snapshot=snapshot, now=evaluated_at
            )
        )
    except ValueError as exc:
        return TransitionResult(
            ok=False,
            error=WorkflowError(
                code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT, message=str(exc)
            ),
        )
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={"retry_attempt_id": result.retry_attempt_id, "status": result.status},
    )
