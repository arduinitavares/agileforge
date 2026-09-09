"""Load immutable retry facts without changing original Sprint history."""
# ruff: noqa: EM101, EM102, TRY003

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, cast

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
from workflow.execution_integrity import triage_payload_fingerprint
from workflow.facts import (
    PostSprintTriageFact,
    SprintClosureFact,
    SprintRetryFact,
    SprintRetryStartFact,
    SprintReviewFact,
    StoryCompletionFact,
    TaskCompletionFact,
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
    session: Session, *, project_id: int
) -> tuple[SprintRetryFact, ...]:
    """Return all project retry attempts in stable Sprint, ordinal, identity order."""
    attempts = session.exec(
        select(SprintRetryAttempt)
        .where(col(SprintRetryAttempt.project_id) == project_id)
        .order_by(
            col(SprintRetryAttempt.sprint_id),
            col(SprintRetryAttempt.ordinal),
            col(SprintRetryAttempt.retry_attempt_id),
        )
    ).all()
    attempts_by_id = {_id(row.retry_attempt_id, "attempt"): row for row in attempts}
    sprint_ids = {row.sprint_id for row in attempts}
    sprints = (
        {
            _id(row.sprint_id, "source Sprint"): row
            for row in session.exec(
                select(Sprint).where(col(Sprint.sprint_id).in_(sprint_ids))
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
    return tuple(_load_attempt(session, row, project_id) for row in attempts)


def _load_attempt(
    session: Session, attempt: SprintRetryAttempt, project_id: int
) -> SprintRetryFact:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    story_states = session.exec(
        select(SprintRetryStoryState)
        .where(col(SprintRetryStoryState.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryStoryState.story_id))
    ).all()
    task_states = session.exec(
        select(SprintRetryTaskState)
        .where(col(SprintRetryTaskState.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryTaskState.task_id))
    ).all()
    _validate_subjects(session, attempt, project_id, story_states, task_states)
    return SprintRetryFact(
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
        start=_start(session, attempt),
        task_completions=_task_evidence(session, attempt),
        story_completions=_story_closures(session, attempt),
        sprint_reviews=_reviews(session, attempt),
        sprint_closures=_closures(session, attempt),
        post_sprint_triage=_triage(session, attempt),
    )


def _validate_subjects(
    session: Session,
    attempt: SprintRetryAttempt,
    project_id: int,
    story_states: Sequence[SprintRetryStoryState],
    task_states: Sequence[SprintRetryTaskState],
) -> None:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    selected_story_ids = {
        row.story_id
        for row in session.exec(
            select(SprintStory).where(col(SprintStory.sprint_id) == attempt.sprint_id)
        ).all()
    }
    story_ids = {row.story_id for row in story_states}
    stories = (
        {
            _id(row.story_id, "Story"): row
            for row in session.exec(
                select(UserStory).where(col(UserStory.story_id).in_(story_ids))
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
            .where(col(Task.task_id).in_(task_ids))
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
    session: Session, attempt: SprintRetryAttempt
) -> SprintRetryStartFact | None:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryStart).where(
            col(SprintRetryStart.retry_attempt_id) == attempt_id
        )
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
    session: Session, attempt: SprintRetryAttempt
) -> tuple[TaskCompletionFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryTaskEvidence)
        .where(col(SprintRetryTaskEvidence.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryTaskEvidence.sprint_retry_task_evidence_id))
    ).all()
    result: list[TaskCompletionFact] = []
    for row in rows:
        _check_scope(row, attempt, "Task evidence")
        try:
            artifact_refs = tuple(_STRING_LIST.validate_json(row.artifact_refs_json))
            checklist_result = _JSON_OBJECT.validate_json(row.checklist_result_json)
        except ValidationError as exc:
            raise _invalid("Task evidence JSON is invalid.") from exc
        if (
            artifact_refs != tuple(sorted(set(artifact_refs)))
            or canonical_json(list(artifact_refs)) != row.artifact_refs_json
            or canonical_json(checklist_result) != row.checklist_result_json
            or row.acceptance_result not in {"partially_met", "fully_met"}
        ):
            raise _invalid("Task evidence is not canonical.")
        result.append(
            TaskCompletionFact(
                completion_id=_id(row.sprint_retry_task_evidence_id, "Task evidence"),
                task_id=row.task_id,
                sprint_id=attempt.sprint_id,
                outcome_summary=row.outcome_summary,
                artifact_refs=artifact_refs,
                acceptance_result=cast(
                    "Literal['partially_met', 'fully_met']", row.acceptance_result
                ),
                checklist_result=checklist_result,
                evidence_fingerprint=row.evidence_fingerprint,
            )
        )
    return tuple(result)


def _story_closures(
    session: Session, attempt: SprintRetryAttempt
) -> tuple[StoryCompletionFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryStoryClosure)
        .where(col(SprintRetryStoryClosure.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryStoryClosure.sprint_retry_story_closure_id))
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
    session: Session, attempt: SprintRetryAttempt
) -> tuple[SprintReviewFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryReview).where(
            col(SprintRetryReview.retry_attempt_id) == attempt_id
        )
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
    session: Session, attempt: SprintRetryAttempt
) -> tuple[SprintClosureFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryClosure).where(
            col(SprintRetryClosure.retry_attempt_id) == attempt_id
        )
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
    session: Session, attempt: SprintRetryAttempt
) -> tuple[PostSprintTriageFact, ...]:
    attempt_id = _id(attempt.retry_attempt_id, "attempt")
    rows = session.exec(
        select(SprintRetryTriage)
        .where(col(SprintRetryTriage.retry_attempt_id) == attempt_id)
        .order_by(col(SprintRetryTriage.sprint_retry_triage_id))
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
