"""Caller-transaction handlers for durable execution workflow facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from models.sprint_retry import (
    SprintRetryClosure,
    SprintRetryReview,
    SprintRetryStoryClosure,
    SprintRetryTaskEvidence,
    SprintRetryTriage,
)
from models.workflow import (
    PostSprintTriage,
    SprintClosure,
    SprintReview,
    StoryClosure,
    TaskCompletionEvidence,
)
from repositories.workflow import WorkflowFactRepository
from services.agent_workbench.post_sprint_triage import (
    PostSprintTriageInput,
    record_post_sprint_triage_in_session,
)
from services.agent_workbench.sprint_phase import (
    SprintCloseInput,
    SprintReviewInput,
    close_sprint_in_session,
    review_sprint_in_session,
)
from services.story_close_service import (
    StoryCloseInput,
    StoryCloseServiceError,
    close_story_in_session,
)
from services.task_execution_service import (
    TaskCompletionInput,
    TaskExecutionServiceError,
    complete_task_in_session,
)
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.execution_identity import parse_execution_instance_key
from workflow.execution_scope import ExecutionScopeError, resolve_execution_scope
from workflow.requests.execution import (
    CloseSprint,
    CloseStory,
    CompleteTask,
    RecordPostSprintTriage,
    ReviewSprint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

type ExecutionRequest = (
    CompleteTask | CloseStory | ReviewSprint | CloseSprint | RecordPostSprintTriage
)


def _success(decision: NodeDecision, output: dict[str, object]) -> TransitionResult:
    return TransitionResult(ok=True, applied_node_id=decision.node_id, output=output)


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


def _reference(
    decision: NodeDecision,
    fact_type: str,
    fact_id: int,
) -> str | None:
    matches = tuple(
        item.fingerprint
        for item in decision.fact_references
        if item.fact_type == fact_type and item.fact_id == str(fact_id)
    )
    return matches[0] if len(matches) == 1 else None


def _execution_sprint_id(session: Session, project_id: int) -> int | None:
    snapshot = WorkflowFactRepository(session).load(project_id)
    active = tuple(
        item.sprint_id for item in snapshot.sprints if item.status == "active"
    )
    return active[0] if len(active) == 1 else None


def _retry_sprint_id(
    session: Session,
    *,
    project_id: int,
    retry_attempt_id: int,
) -> int | None:
    """Resolve the exact Sprint owned by one retry attempt."""
    snapshot = WorkflowFactRepository(session).load(project_id)
    retries = tuple(
        item
        for item in snapshot.sprint_retries
        if item.retry_attempt_id == retry_attempt_id
    )
    if len(retries) != 1:
        return None
    try:
        scope = resolve_execution_scope(
            snapshot,
            sprint_id=retries[0].sprint_id,
            retry_attempt_id=retry_attempt_id,
        )
    except ExecutionScopeError:
        return None
    return scope.sprint_id


def _execute_complete_task(
    session: Session,
    request: CompleteTask,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    identity = parse_execution_instance_key(request.instance_key)
    retry_attempt_id = identity.retry_attempt_id
    sprint_id = _execution_sprint_id(session, request.project_id)
    if retry_attempt_id is not None:
        sprint_id = _retry_sprint_id(
            session,
            project_id=request.project_id,
            retry_attempt_id=retry_attempt_id,
        )
        if sprint_id is None:
            return _conflict("CompleteTask does not target the selected Task fact.")
    if sprint_id is None or _reference(decision, "task", request.task_id) is None:
        return _conflict("CompleteTask does not target the selected Task fact.")
    try:
        row = complete_task_in_session(
            session,
            TaskCompletionInput(
                project_id=request.project_id,
                sprint_id=sprint_id,
                task_id=request.task_id,
                outcome_summary=request.outcome_summary,
                artifact_refs=request.artifact_refs,
                acceptance_result=request.acceptance_result,
                checklist_result=request.checklist_result,
                completed_by=request.actor,
                completed_at=evaluated_at,
                retry_attempt_id=retry_attempt_id,
            ),
        )
    except TaskExecutionServiceError as error:
        return _conflict(error.detail)
    return _task_completion_result(
        decision,
        row,
        request=request,
        sprint_id=sprint_id,
        retry_attempt_id=retry_attempt_id,
    )


def _task_completion_result(
    decision: NodeDecision,
    row: TaskCompletionEvidence | SprintRetryTaskEvidence,
    *,
    request: CompleteTask,
    sprint_id: int,
    retry_attempt_id: int | None,
) -> TransitionResult:
    """Build a verified result from one original or retry Task evidence row."""
    if retry_attempt_id is not None:
        return _retry_task_completion_result(
            decision,
            row,
            request=request,
            sprint_id=sprint_id,
            retry_attempt_id=retry_attempt_id,
        )
    return _original_task_completion_result(
        decision,
        row,
        request=request,
        sprint_id=sprint_id,
    )


def _retry_task_completion_result(
    decision: NodeDecision,
    row: TaskCompletionEvidence | SprintRetryTaskEvidence,
    *,
    request: CompleteTask,
    sprint_id: int,
    retry_attempt_id: int,
) -> TransitionResult:
    """Build the result only if retry Task evidence has a durable identity."""
    if not isinstance(row, SprintRetryTaskEvidence):
        return _conflict("Retry task completion did not persist retry evidence.")
    if row.sprint_retry_task_evidence_id is None:
        return _conflict("Retry task completion evidence has no durable identity.")
    return _success(
        decision,
        {
            "task_id": request.task_id,
            "sprint_id": sprint_id,
            "retry_attempt_id": retry_attempt_id,
            "sprint_retry_task_evidence_id": row.sprint_retry_task_evidence_id,
            "evidence_fingerprint": row.evidence_fingerprint,
        },
    )


def _original_task_completion_result(
    decision: NodeDecision,
    row: TaskCompletionEvidence | SprintRetryTaskEvidence,
    *,
    request: CompleteTask,
    sprint_id: int,
) -> TransitionResult:
    """Build the result only if original Task evidence has a durable identity."""
    if not isinstance(row, TaskCompletionEvidence):
        return _conflict("Task completion did not persist original evidence.")
    if row.task_completion_evidence_id is None:
        return _conflict("Task completion evidence has no durable identity.")
    return _success(
        decision,
        {
            "task_id": request.task_id,
            "sprint_id": sprint_id,
            "task_completion_evidence_id": row.task_completion_evidence_id,
            "evidence_fingerprint": row.evidence_fingerprint,
        },
    )


def _execute_close_story(
    session: Session,
    request: CloseStory,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    identity = parse_execution_instance_key(request.instance_key)
    retry_attempt_id = identity.retry_attempt_id
    sprint_id = _execution_sprint_id(session, request.project_id)
    if retry_attempt_id is not None:
        sprint_id = _retry_sprint_id(
            session,
            project_id=request.project_id,
            retry_attempt_id=retry_attempt_id,
        )
        if sprint_id is None:
            return _conflict("CloseStory does not target the selected Story facts.")
    completion_fingerprint = _reference(
        decision,
        "story_completion",
        request.story_id,
    )
    if sprint_id is None or completion_fingerprint is None:
        return _conflict("CloseStory does not target the selected Story facts.")
    try:
        row = close_story_in_session(
            session,
            StoryCloseInput(
                project_id=request.project_id,
                sprint_id=sprint_id,
                story_id=request.story_id,
                completion_fingerprint=completion_fingerprint,
                resolution=request.resolution,
                delivered=request.delivered,
                evidence=request.evidence,
                known_gaps=request.known_gaps,
                closed_by=request.actor,
                closed_at=evaluated_at,
                retry_attempt_id=retry_attempt_id,
            ),
        )
    except StoryCloseServiceError as error:
        return _conflict(error.detail)
    return _story_close_result(
        decision,
        row,
        request=request,
        sprint_id=sprint_id,
        retry_attempt_id=retry_attempt_id,
    )


def _story_close_result(
    decision: NodeDecision,
    row: StoryClosure | SprintRetryStoryClosure,
    *,
    request: CloseStory,
    sprint_id: int,
    retry_attempt_id: int | None,
) -> TransitionResult:
    """Build a verified result from one original or retry Story closure row."""
    if retry_attempt_id is not None:
        return _retry_story_close_result(
            decision,
            row,
            request=request,
            sprint_id=sprint_id,
            retry_attempt_id=retry_attempt_id,
        )
    return _original_story_close_result(
        decision,
        row,
        request=request,
        sprint_id=sprint_id,
    )


def _retry_story_close_result(
    decision: NodeDecision,
    row: StoryClosure | SprintRetryStoryClosure,
    *,
    request: CloseStory,
    sprint_id: int,
    retry_attempt_id: int,
) -> TransitionResult:
    """Build the result only if retry Story closure has a durable identity."""
    if not isinstance(row, SprintRetryStoryClosure):
        return _conflict("Retry Story closure did not persist retry facts.")
    if row.sprint_retry_story_closure_id is None:
        return _conflict("Retry Story closure has no durable identity.")
    return _success(
        decision,
        {
            "story_id": request.story_id,
            "sprint_id": sprint_id,
            "retry_attempt_id": retry_attempt_id,
            "sprint_retry_story_closure_id": row.sprint_retry_story_closure_id,
            "completion_fingerprint": row.completion_fingerprint,
        },
    )


def _original_story_close_result(
    decision: NodeDecision,
    row: StoryClosure | SprintRetryStoryClosure,
    *,
    request: CloseStory,
    sprint_id: int,
) -> TransitionResult:
    """Build the result only if original Story closure has a durable identity."""
    if not isinstance(row, StoryClosure):
        return _conflict("Story closure did not persist original facts.")
    if row.story_closure_id is None:
        return _conflict("Story closure has no durable identity.")
    return _success(
        decision,
        {
            "story_id": request.story_id,
            "sprint_id": sprint_id,
            "story_closure_id": row.story_closure_id,
            "completion_fingerprint": row.completion_fingerprint,
        },
    )


def _execute_review_sprint(
    session: Session,
    request: ReviewSprint,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    if request.instance_key is None:
        return _conflict("ReviewSprint does not target the exact terminal facts.")
    identity = parse_execution_instance_key(request.instance_key)
    retry_attempt_id = identity.retry_attempt_id
    expected = _reference(decision, "sprint_review", request.sprint_id)
    if expected is None or request.review_fingerprint != expected:
        return _conflict("ReviewSprint does not target the exact terminal facts.")
    try:
        row = review_sprint_in_session(
            session,
            SprintReviewInput(
                project_id=request.project_id,
                sprint_id=request.sprint_id,
                review_fingerprint=request.review_fingerprint,
                reviewed_by=request.actor,
                reviewed_at=evaluated_at,
                retry_attempt_id=retry_attempt_id,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    return _sprint_review_result(
        decision,
        row,
        request=request,
        retry_attempt_id=retry_attempt_id,
    )


def _sprint_review_result(
    decision: NodeDecision,
    row: SprintReview | SprintRetryReview,
    *,
    request: ReviewSprint,
    retry_attempt_id: int | None,
) -> TransitionResult:
    """Build a verified result from one original or retry Sprint review row."""
    if retry_attempt_id is not None:
        if not isinstance(row, SprintRetryReview):
            return _conflict("Retry Sprint review did not persist retry facts.")
        if row.sprint_retry_review_id is None:
            return _conflict("Retry Sprint review has no durable identity.")
        return _success(
            decision,
            {
                "sprint_id": request.sprint_id,
                "retry_attempt_id": retry_attempt_id,
                "sprint_retry_review_id": row.sprint_retry_review_id,
                "review_fingerprint": row.review_fingerprint,
            },
        )
    if not isinstance(row, SprintReview):
        return _conflict("Sprint review did not persist original facts.")
    if row.sprint_review_id is None:
        return _conflict("Sprint review has no durable identity.")
    return _success(
        decision,
        {
            "sprint_id": request.sprint_id,
            "sprint_review_id": row.sprint_review_id,
            "review_fingerprint": row.review_fingerprint,
        },
    )


def _execute_close_sprint(
    session: Session,
    request: CloseSprint,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    if request.instance_key is None:
        return _conflict("CloseSprint does not target the persisted review fact.")
    identity = parse_execution_instance_key(request.instance_key)
    retry_attempt_id = identity.retry_attempt_id
    expected = _reference(decision, "sprint_review", request.sprint_id)
    close_fingerprint = _reference(decision, "sprint_close", request.sprint_id)
    if (
        expected is None
        or close_fingerprint is None
        or request.review_fingerprint != expected
    ):
        return _conflict("CloseSprint does not target the persisted review fact.")
    try:
        row = close_sprint_in_session(
            session,
            SprintCloseInput(
                project_id=request.project_id,
                sprint_id=request.sprint_id,
                review_fingerprint=request.review_fingerprint,
                close_fingerprint=close_fingerprint,
                closed_by=request.actor,
                closed_at=evaluated_at,
                retry_attempt_id=retry_attempt_id,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    return _sprint_close_result(
        decision,
        row,
        request=request,
        retry_attempt_id=retry_attempt_id,
    )


def _sprint_close_result(
    decision: NodeDecision,
    row: SprintClosure | SprintRetryClosure,
    *,
    request: CloseSprint,
    retry_attempt_id: int | None,
) -> TransitionResult:
    """Build a verified result from one original or retry Sprint closure row."""
    if retry_attempt_id is not None:
        if not isinstance(row, SprintRetryClosure):
            return _conflict("Retry Sprint closure did not persist retry facts.")
        if row.sprint_retry_closure_id is None:
            return _conflict("Retry Sprint closure has no durable identity.")
        return _success(
            decision,
            {
                "sprint_id": request.sprint_id,
                "retry_attempt_id": retry_attempt_id,
                "sprint_retry_closure_id": row.sprint_retry_closure_id,
                "review_fingerprint": row.review_fingerprint,
                "close_fingerprint": row.close_fingerprint,
            },
        )
    if not isinstance(row, SprintClosure):
        return _conflict("Sprint closure did not persist original facts.")
    if row.sprint_closure_id is None:
        return _conflict("Sprint closure has no durable identity.")
    return _success(
        decision,
        {
            "sprint_id": request.sprint_id,
            "sprint_closure_id": row.sprint_closure_id,
            "review_fingerprint": row.review_fingerprint,
            "close_fingerprint": row.close_fingerprint,
        },
    )


def _execute_triage(
    session: Session,
    request: RecordPostSprintTriage,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    if request.instance_key is None:
        return _conflict("Triage does not target the exact completed Sprint.")
    identity = parse_execution_instance_key(request.instance_key)
    retry_attempt_id = identity.retry_attempt_id
    if _reference(decision, "sprint_closure", request.sprint_id) is None:
        return _conflict("Triage does not target the exact completed Sprint.")
    try:
        row = record_post_sprint_triage_in_session(
            session,
            PostSprintTriageInput(
                project_id=request.project_id,
                sprint_id=request.sprint_id,
                impact=request.impact,
                canonical_payload=request.canonical_payload,
                recorded_by=request.actor,
                recorded_at=evaluated_at,
                retry_attempt_id=retry_attempt_id,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    return _triage_result(
        decision,
        row,
        request=request,
        retry_attempt_id=retry_attempt_id,
    )


def _triage_result(
    decision: NodeDecision,
    row: PostSprintTriage | SprintRetryTriage,
    *,
    request: RecordPostSprintTriage,
    retry_attempt_id: int | None,
) -> TransitionResult:
    """Build a verified result from one original or retry triage row."""
    if retry_attempt_id is not None:
        if not isinstance(row, SprintRetryTriage):
            return _conflict("Retry post-sprint triage did not persist retry facts.")
        if row.sprint_retry_triage_id is None:
            return _conflict("Retry post-sprint triage has no durable identity.")
        return _success(
            decision,
            {
                "sprint_id": request.sprint_id,
                "retry_attempt_id": retry_attempt_id,
                "sprint_retry_triage_id": row.sprint_retry_triage_id,
                "impact": row.impact,
                "payload_fingerprint": row.payload_fingerprint,
            },
        )
    if not isinstance(row, PostSprintTriage):
        return _conflict("Post-sprint triage did not persist original facts.")
    if row.triage_id is None:
        return _conflict("Post-sprint triage has no durable identity.")
    return _success(
        decision,
        {
            "sprint_id": request.sprint_id,
            "triage_id": row.triage_id,
            "impact": row.impact,
            "payload_fingerprint": row.payload_fingerprint,
        },
    )


def execute_execution_request(
    session: Session,
    request: ExecutionRequest,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Dispatch the closed five-request execution family."""
    if isinstance(request, CompleteTask):
        return _execute_complete_task(session, request, decision, evaluated_at)
    if isinstance(request, CloseStory):
        return _execute_close_story(session, request, decision, evaluated_at)
    if isinstance(request, ReviewSprint):
        return _execute_review_sprint(session, request, decision, evaluated_at)
    if isinstance(request, CloseSprint):
        return _execute_close_sprint(session, request, decision, evaluated_at)
    return _execute_triage(session, request, decision, evaluated_at)


__all__ = ["ExecutionRequest", "execute_execution_request"]
