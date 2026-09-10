"""Load immutable retry facts without changing original Sprint history."""
# ruff: noqa: EM101, EM102, TRY003

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, Task, UserStory
from models.sprint_retry import (
    SprintRetryAttempt,
    SprintRetryClosure,
    SprintRetryReview,
    SprintRetryStart,
    SprintRetryStoryClosure,
    SprintRetryStoryState,
    SprintRetryTaskEvidence,
    SprintRetryTaskState,
    SprintRetryTriage,
)
from workflow.contracts import JsonValue
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    StoryClosurePayload,
    canonical_task_evidence_payload,
    story_completion_fingerprint,
    task_evidence_fingerprint,
    triage_payload_fingerprint,
)
from workflow.execution_scope import ExecutionScopeError, resolve_execution_scope
from workflow.facts import (
    PostSprintTriageFact,
    SprintClosureFact,
    SprintRetryFact,
    SprintRetryStartFact,
    SprintReviewFact,
    StoryCompletionFact,
    TaskCompletionFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from collections.abc import Sequence

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_STRING_LIST = TypeAdapter(list[str])
_RETRY_STATUSES: dict[str, Literal["planned", "active", "completed"]] = {
    "Planned": "planned",
    "Active": "active",
    "Completed": "completed",
}


class SprintRetryFactLoadError(RuntimeError):
    """Raised when retry persistence cannot form immutable retry facts."""


class _RetryScopeRow(Protocol):
    """Columns shared by every retry-owned immutable record."""

    project_id: int
    sprint_id: int
    retry_attempt_id: int


def _id(value: int | None, label: str) -> int:
    if value is None:
        raise SprintRetryFactLoadError(
            f"Invalid Sprint retry persistence: {label} has no identity."
        )
    return value


def _invalid(message: str) -> SprintRetryFactLoadError:
    return SprintRetryFactLoadError(f"Invalid Sprint retry persistence: {message}")


def load_sprint_retry_facts(
    session: Session,
    *,
    project_id: int,
    snapshot: WorkflowFactSnapshot,
    query_options: dict[str, object] | None = None,
) -> tuple[SprintRetryFact, ...]:
    """Return all project retry attempts in stable Sprint, ordinal, identity order."""
    options: dict[str, object] = {"autoflush": False}
    if query_options is not None:
        options = query_options
    attempts = session.exec(
        select(SprintRetryAttempt)
        .where(col(SprintRetryAttempt.project_id) == project_id)
        .order_by(
            col(SprintRetryAttempt.sprint_id),
            col(SprintRetryAttempt.ordinal),
            col(SprintRetryAttempt.retry_attempt_id),
        ),
        execution_options=options,
    ).all()
    attempts_by_id = {_id(row.retry_attempt_id, "attempt"): row for row in attempts}
    sprint_ids = {row.sprint_id for row in attempts}
    sprints = (
        {
            _id(row.sprint_id, "source Sprint"): row
            for row in session.exec(
                select(Sprint).where(col(Sprint.sprint_id).in_(sprint_ids)),
                execution_options=options,
            ).all()
        }
        if sprint_ids
        else {}
    )
    for row in attempts:
        if row.status not in _RETRY_STATUSES:
            raise _invalid("attempt status is invalid.")
        sprint = sprints.get(row.sprint_id)
        if sprint is None or sprint.project_id != project_id:
            raise _invalid("attempt targets a cross-Project Sprint.")
        if row.predecessor_retry_attempt_id is not None:
            parent = attempts_by_id.get(row.predecessor_retry_attempt_id)
            if (
                parent is None
                or parent.sprint_id != row.sprint_id
                or parent.ordinal >= row.ordinal
            ):
                raise _invalid(
                    "attempt predecessor does not form an ordered same-Sprint chain."
                )
    return tuple(
        _load_attempt(session, row, project_id, snapshot, options) for row in attempts
    )


def _load_attempt(
    session: Session,
    attempt: SprintRetryAttempt,
    project_id: int,
    snapshot: WorkflowFactSnapshot,
    options: dict[str, object],
) -> SprintRetryFact:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    story_states = session.exec(
        select(SprintRetryStoryState)
        .where(col(SprintRetryStoryState.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryStoryState.story_id)),
        execution_options=options,
    ).all()
    task_states = session.exec(
        select(SprintRetryTaskState)
        .where(col(SprintRetryTaskState.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryTaskState.task_id)),
        execution_options=options,
    ).all()
    _validate_subjects(session, attempt, project_id, story_states, task_states, options)
    task_completions = _task_evidence(session, attempt, options)
    provisional = SprintRetryFact(
        retry_attempt_id=attempt_id,
        project_id=project_id,
        sprint_id=attempt.sprint_id,
        ordinal=attempt.ordinal,
        predecessor_retry_attempt_id=attempt.predecessor_retry_attempt_id,
        contract_fingerprint=attempt.contract_fingerprint,
        created_by=attempt.created_by,
        rationale=attempt.rationale,
        creation_fingerprint=attempt.creation_fingerprint,
        creation_receipt_key=attempt.creation_receipt_key,
        created_at=attempt.created_at,
        status=_RETRY_STATUSES[attempt.status],
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        story_statuses=tuple((row.story_id, row.status) for row in story_states),
        task_statuses=tuple((row.task_id, row.status) for row in task_states),
        start=_start(session, attempt, options),
        task_completions=task_completions,
        story_completions=(),
        sprint_reviews=(),
        sprint_closures=(),
        post_sprint_triage=(),
    )
    scope_snapshot = snapshot.model_copy(
        update={"sprint_retries": (*snapshot.sprint_retries, provisional)}
    )
    _validate_task_fingerprints(scope_snapshot, provisional)
    story_completions = _story_closures(session, attempt, options)
    complete = provisional.model_copy(
        update={
            "story_completions": story_completions,
            "sprint_reviews": _reviews(session, attempt, options),
            "sprint_closures": _closures(session, attempt, options),
            "post_sprint_triage": _triage(session, attempt, options),
        }
    )
    _validate_story_fingerprints(
        scope_snapshot.model_copy(
            update={"sprint_retries": (*snapshot.sprint_retries, complete)}
        ),
        complete,
    )
    return complete


def _validate_subjects(  # noqa: PLR0913
    session: Session,
    attempt: SprintRetryAttempt,
    project_id: int,
    story_states: Sequence[SprintRetryStoryState],
    task_states: Sequence[SprintRetryTaskState],
    options: dict[str, object],
) -> None:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    selected_story_ids = {
        row.story_id
        for row in session.exec(
            select(SprintStory).where(col(SprintStory.sprint_id) == attempt.sprint_id),
            execution_options=options,
        ).all()
    }
    story_ids = {row.story_id for row in story_states}
    stories = (
        {
            _id(row.story_id, "Story"): row
            for row in session.exec(
                select(UserStory).where(col(UserStory.story_id).in_(story_ids)),
                execution_options=options,
            ).all()
        }
        if story_ids
        else {}
    )
    for row in story_states:
        story = stories.get(row.story_id)
        if (
            row.project_id != project_id
            or row.sprint_id != attempt.sprint_id
            or row.retry_attempt_id != attempt_id
            or story is None
            or story.project_id != project_id
            or row.story_id not in selected_story_ids
        ):
            raise _invalid("Story progress is not owned by its retry scope.")
    task_ids = {row.task_id for row in task_states}
    task_rows = (
        session.exec(
            select(Task, UserStory)
            .join(UserStory, col(Task.story_id) == col(UserStory.story_id))
            .where(col(Task.task_id).in_(task_ids)),
            execution_options=options,
        ).all()
        if task_ids
        else []
    )
    tasks = {_id(task.task_id, "Task"): story for task, story in task_rows}
    for row in task_states:
        story = tasks.get(row.task_id)
        if (
            row.project_id != project_id
            or row.sprint_id != attempt.sprint_id
            or row.retry_attempt_id != attempt_id
            or story is None
            or story.project_id != project_id
            or _id(story.story_id, "Task Story") not in selected_story_ids
        ):
            raise _invalid("Task progress is not owned by its retry scope.")


def _check_scope(row: _RetryScopeRow, attempt: SprintRetryAttempt, label: str) -> None:
    if (
        row.project_id != attempt.project_id
        or row.sprint_id != attempt.sprint_id
        or row.retry_attempt_id != attempt.retry_attempt_id
    ):
        raise _invalid(f"{label} is not owned by its retry attempt.")


def _start(
    session: Session, attempt: SprintRetryAttempt, options: dict[str, object]
) -> SprintRetryStartFact | None:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryStart).where(
            col(SprintRetryStart.retry_attempt_id) == attempt_id
        ),
        execution_options=options,
    ).all()
    if len(rows) > 1:
        raise _invalid("attempt has multiple starts.")
    if not rows:
        return None
    row = rows[0]
    _check_scope(row, attempt, "start")
    return SprintRetryStartFact(
        start_id=_id(row.sprint_retry_start_id, "start"),
        retry_attempt_id=attempt_id,
        contract_fingerprint=row.contract_fingerprint,
        decision_fingerprint=row.decision_fingerprint,
        started_by=row.started_by,
        started_at=row.started_at,
    )


def _task_evidence(
    session: Session, attempt: SprintRetryAttempt, options: dict[str, object]
) -> tuple[TaskCompletionFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryTaskEvidence)
        .where(col(SprintRetryTaskEvidence.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryTaskEvidence.sprint_retry_task_evidence_id)),
        execution_options=options,
    ).all()
    result: list[TaskCompletionFact] = []
    for row in rows:
        _check_scope(row, attempt, "Task evidence")
        try:
            evidence = canonical_task_evidence_payload(
                outcome_summary=row.outcome_summary,
                artifact_refs_json=row.artifact_refs_json,
                acceptance_result=row.acceptance_result,
                checklist_result_json=row.checklist_result_json,
            )
        except ExecutionIntegrityError as exc:
            raise _invalid(str(exc)) from exc
        result.append(
            TaskCompletionFact(
                completion_id=_id(row.sprint_retry_task_evidence_id, "Task evidence"),
                task_id=row.task_id,
                sprint_id=attempt.sprint_id,
                outcome_summary=evidence.outcome_summary,
                artifact_refs=evidence.artifact_refs,
                acceptance_result=evidence.acceptance_result,
                checklist_result=evidence.checklist_result,
                evidence_fingerprint=row.evidence_fingerprint,
            )
        )
    return tuple(result)


def _validate_task_fingerprints(
    snapshot: WorkflowFactSnapshot, attempt: SprintRetryFact
) -> None:
    """Reject retry evidence that no longer binds to its scoped contract."""
    if not attempt.task_completions:
        return
    try:
        scope = resolve_execution_scope(
            snapshot,
            sprint_id=attempt.sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
        )
        tasks = {item.task_id: item for item in scope.tasks}
        for fact in attempt.task_completions:
            task = tasks.get(fact.task_id)
            if task is None:
                raise _invalid("Task evidence targets a non-contract Task.")
            expected = task_evidence_fingerprint(
                snapshot,
                task,
                evidence=canonical_task_evidence_payload(
                    outcome_summary=fact.outcome_summary,
                    artifact_refs_json=canonical_json(list(fact.artifact_refs)),
                    acceptance_result=fact.acceptance_result,
                    checklist_result_json=canonical_json(fact.checklist_result),
                ),
                scope=scope,
            )
            if expected != fact.evidence_fingerprint:
                raise _invalid("Task evidence fingerprint changed.")
    except (ExecutionIntegrityError, ExecutionScopeError) as exc:
        raise _invalid("Task evidence execution contract changed.") from exc


def _validate_story_fingerprints(
    snapshot: WorkflowFactSnapshot, attempt: SprintRetryFact
) -> None:
    """Reject retry Story closures that no longer bind to scoped fresh evidence."""
    if not attempt.story_completions:
        return
    try:
        scope = resolve_execution_scope(
            snapshot,
            sprint_id=attempt.sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
        )
        for fact in attempt.story_completions:
            expected = story_completion_fingerprint(
                snapshot,
                sprint_id=attempt.sprint_id,
                story_id=fact.story_id,
                closure=StoryClosurePayload(
                    resolution=fact.resolution,
                    delivered=fact.delivered,
                    evidence=fact.evidence,
                    known_gaps=fact.known_gaps,
                ),
                scope=scope,
            )
            if expected != fact.completion_fingerprint:
                raise _invalid("Story closure fingerprint changed.")
    except (ExecutionIntegrityError, ExecutionScopeError) as exc:
        raise _invalid("Story closure execution contract changed.") from exc


def _story_closures(
    session: Session, attempt: SprintRetryAttempt, options: dict[str, object]
) -> tuple[StoryCompletionFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryStoryClosure)
        .where(col(SprintRetryStoryClosure.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryStoryClosure.sprint_retry_story_closure_id)),
        execution_options=options,
    ).all()
    result: list[StoryCompletionFact] = []
    for row in rows:
        _check_scope(row, attempt, "Story closure")
        result.append(
            StoryCompletionFact(
                completion_id=_id(row.sprint_retry_story_closure_id, "Story closure"),
                story_id=row.story_id,
                sprint_id=attempt.sprint_id,
                completion_fingerprint=row.completion_fingerprint,
                resolution=row.resolution,
                delivered=row.delivered,
                evidence=row.evidence,
                known_gaps=row.known_gaps,
            )
        )
    return tuple(result)


def _reviews(
    session: Session, attempt: SprintRetryAttempt, options: dict[str, object]
) -> tuple[SprintReviewFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryReview).where(
            col(SprintRetryReview.retry_attempt_id) == attempt_id
        ),
        execution_options=options,
    ).all()
    result = []
    for row in rows:
        _check_scope(row, attempt, "review")
        result.append(
            SprintReviewFact(
                review_id=_id(row.sprint_retry_review_id, "review"),
                sprint_id=attempt.sprint_id,
                review_fingerprint=row.review_fingerprint,
            )
        )
    return tuple(result)


def _closures(
    session: Session, attempt: SprintRetryAttempt, options: dict[str, object]
) -> tuple[SprintClosureFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryClosure).where(
            col(SprintRetryClosure.retry_attempt_id) == attempt_id
        ),
        execution_options=options,
    ).all()
    result = []
    for row in rows:
        _check_scope(row, attempt, "closure")
        result.append(
            SprintClosureFact(
                closure_id=_id(row.sprint_retry_closure_id, "closure"),
                sprint_id=attempt.sprint_id,
                review_fingerprint=row.review_fingerprint,
                close_fingerprint=row.close_fingerprint,
            )
        )
    return tuple(result)


def _triage(
    session: Session, attempt: SprintRetryAttempt, options: dict[str, object]
) -> tuple[PostSprintTriageFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryTriage)
        .where(col(SprintRetryTriage.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryTriage.sprint_retry_triage_id)),
        execution_options=options,
    ).all()
    rows_by_id = {_id(row.sprint_retry_triage_id, "triage"): row for row in rows}
    result: list[PostSprintTriageFact] = []
    for row in rows:
        _check_scope(row, attempt, "triage")
        if (
            row.supersedes_sprint_retry_triage_id is not None
            and row.supersedes_sprint_retry_triage_id not in rows_by_id
        ):
            raise _invalid("triage correction parent is missing or crosses attempts.")
        try:
            payload = _JSON_OBJECT.validate_json(row.canonical_payload_json)
        except ValidationError as exc:
            raise _invalid("triage JSON is invalid.") from exc
        if (
            row.impact not in {"none", "backlog", "specification"}
            or canonical_json(payload) != row.canonical_payload_json
            or triage_payload_fingerprint(row.impact, payload)
            != row.payload_fingerprint
        ):
            raise _invalid("triage payload fingerprint changed.")
        result.append(
            PostSprintTriageFact.model_validate(
                {
                    "triage_id": _id(row.sprint_retry_triage_id, "triage"),
                    "sprint_id": attempt.sprint_id,
                    "impact": row.impact,
                    "canonical_payload": payload,
                    "payload_fingerprint": row.payload_fingerprint,
                    "supersedes_triage_id": row.supersedes_sprint_retry_triage_id,
                }
            )
        )
    return tuple(result)
