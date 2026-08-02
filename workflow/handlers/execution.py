"""Caller-transaction handlers for durable execution workflow facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    CompleteTask
    | CloseStory
    | ReviewSprint
    | CloseSprint
    | RecordPostSprintTriage
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


def _active_sprint_id(session: Session, project_id: int) -> int | None:
    snapshot = WorkflowFactRepository(session).load(project_id)
    active = tuple(
        item.sprint_id for item in snapshot.sprints if item.status == "active"
    )
    return active[0] if len(active) == 1 else None


def _execute_complete_task(
    session: Session,
    request: CompleteTask,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    sprint_id = _active_sprint_id(session, request.project_id)
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
            ),
        )
    except TaskExecutionServiceError as error:
        return _conflict(error.detail)
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
    sprint_id = _active_sprint_id(session, request.project_id)
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
            ),
        )
    except StoryCloseServiceError as error:
        return _conflict(error.detail)
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
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
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
    expected = _reference(decision, "sprint_review", request.sprint_id)
    if expected is None or request.review_fingerprint != expected:
        return _conflict("CloseSprint does not target the persisted review fact.")
    try:
        row = close_sprint_in_session(
            session,
            SprintCloseInput(
                project_id=request.project_id,
                sprint_id=request.sprint_id,
                review_fingerprint=request.review_fingerprint,
                closed_by=request.actor,
                closed_at=evaluated_at,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    if row.sprint_closure_id is None:
        return _conflict("Sprint closure has no durable identity.")
    return _success(
        decision,
        {
            "sprint_id": request.sprint_id,
            "sprint_closure_id": row.sprint_closure_id,
            "review_fingerprint": row.review_fingerprint,
        },
    )


def _execute_triage(
    session: Session,
    request: RecordPostSprintTriage,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
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
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
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
