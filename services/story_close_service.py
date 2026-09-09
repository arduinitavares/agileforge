"""Story close endpoint orchestration helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, Self, TypedDict, Unpack

from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, Task, UserStory
from models.enums import StoryResolution, TaskStatus
from models.enums import StoryStatus
from models.events import StoryCompletionLog
from models.sprint_retry import (
    SprintRetryStoryClosure,
    SprintRetryStoryState,
)
from models.workflow import StoryClosure, TaskCompletionEvidence
from repositories.workflow import WorkflowFactRepository
from utils.api_schemas import StoryTaskProgressSummary
from workflow.execution_integrity import (
    StoryClosurePayload,
    story_completion_eligibility_fingerprint,
    story_completion_fingerprint,
)
from workflow.execution_scope import (
    ExecutionScope,
    ExecutionScopeError,
    resolve_execution_scope,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

_COMPLETION_FINGERPRINT_STALE = "Story completion fingerprint is stale."
_RESOLUTION_INVALID = "Story resolution is invalid."
_ORIGINAL_CLOSURE_IMMUTABLE = "Story closure is immutable."
_RETRY_CLOSURE_IMMUTABLE = "Retry Story closure is immutable."
_RETRY_STORY_SCOPE_REQUIRED = (
    "Retry Story closure persistence requires validated retry scope."
)


class StoryCloseServiceError(Exception):
    """Domain-level story close error for router translation."""

    def __init__(self, detail: str, *, status_code: int) -> None:
        """Store an API-ready error detail and HTTP status code."""
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code

    @classmethod
    def story_not_found(cls) -> Self:
        """Build the 404 error raised when the story cannot be loaded."""
        return cls(detail="Story not found", status_code=404)

    @classmethod
    def sprint_not_in_project(cls) -> Self:
        """Build the 404 error raised for cross-project or missing sprints."""
        return cls(detail="Sprint not found in this project", status_code=404)

    @classmethod
    def story_not_in_sprint(cls) -> Self:
        """Build the 404 error raised when the story is outside the sprint."""
        return cls(
            detail="Story does not belong to the given sprint",
            status_code=404,
        )

    @classmethod
    def no_executable_tasks(cls) -> Self:
        """Build the 409 error raised when a story has no actionable tasks."""
        return cls(
            detail="Cannot close a story with no executable tasks.",
            status_code=409,
        )

    @classmethod
    def incomplete_tasks(cls) -> Self:
        """Build the 409 error raised when actionable tasks remain incomplete."""
        return cls(
            detail=(
                "Cannot close a story unless all actionable tasks are Done or "
                "Cancelled."
            ),
            status_code=409,
        )

    @classmethod
    def already_closed(cls, status: StoryStatus) -> Self:
        """Build the 409 error raised when attempting to modify a closed story."""
        return cls(
            detail=f"Cannot modify an already {status.value} story.",
            status_code=409,
        )


@dataclass(frozen=True)
class StoryCloseInput:
    """Validated caller inputs for one durable Story closure."""

    project_id: int
    sprint_id: int
    story_id: int
    completion_fingerprint: str
    resolution: str
    delivered: str
    evidence: str
    known_gaps: str
    closed_by: str
    closed_at: datetime
    retry_attempt_id: int | None = None


class _StoryLike(Protocol):
    @property
    def story_id(self) -> int | None: ...

    status: StoryStatus
    resolution: StoryResolution | None
    completion_notes: str | None
    evidence_links: str | None
    completed_at: Any


class _SprintLike(Protocol):
    @property
    def sprint_id(self) -> int | None: ...

    @property
    def project_id(self) -> int | None: ...


class _StoryClosePersistOptions(TypedDict):
    story: _StoryLike
    old_status: StoryStatus
    evidence_json: str | None
    known_gaps: str | None
    follow_up_notes: str | None
    changed_by: str


class _StoryClosePersist(Protocol):
    def __call__(self, **kwargs: Unpack[_StoryClosePersistOptions]) -> None: ...


class _StoryCloseSubjectOptions(TypedDict):
    load_story: Callable[[], _StoryLike | None]
    load_sprint: Callable[[], _SprintLike | None]
    load_sprint_story: Callable[[_StoryLike], object | None]


class _StoryCloseReadinessOptions(_StoryCloseSubjectOptions):
    load_tasks: Callable[[], Sequence[object]]
    task_progress: Callable[[Sequence[object]], tuple[int, int, int, bool]]


class _StoryCloseOptions(_StoryCloseReadinessOptions):
    resolution: StoryResolution
    completion_notes: str
    evidence_links: Sequence[str] | None
    known_gaps: str | None
    follow_up_notes: str | None
    changed_by: str | None
    now: Callable[[], object]
    persist_story_close: _StoryClosePersist


def _load_story_close_subject(
    *,
    project_id: int,
    load_story: Callable[[], _StoryLike | None],
    load_sprint: Callable[[], _SprintLike | None],
    load_sprint_story: Callable[[_StoryLike], object | None],
) -> tuple[_StoryLike, _SprintLike]:
    story = load_story()
    if not story:
        raise StoryCloseServiceError.story_not_found()

    sprint = load_sprint()
    if not sprint or getattr(sprint, "project_id", None) != project_id:
        raise StoryCloseServiceError.sprint_not_in_project()

    sprint_story = load_sprint_story(story)
    if not sprint_story:
        raise StoryCloseServiceError.story_not_in_sprint()

    return story, sprint


def _build_readiness_summary(
    *,
    tasks: Sequence[object],
    task_progress: Callable[[Sequence[object]], tuple[int, int, int, bool]],
) -> StoryTaskProgressSummary:
    total_tasks, done_tasks, cancelled_tasks, all_actionable_done = task_progress(tasks)
    return StoryTaskProgressSummary(
        total_tasks=total_tasks,
        done_tasks=done_tasks,
        cancelled_tasks=cancelled_tasks,
        all_actionable_tasks_done=all_actionable_done,
    )


def get_story_close_readiness(
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
    **options: Unpack[_StoryCloseReadinessOptions],
) -> dict[str, Any]:
    """Return whether a story can be closed and the task progress summary."""
    story, _sprint = _load_story_close_subject(
        project_id=project_id,
        load_story=options["load_story"],
        load_sprint=options["load_sprint"],
        load_sprint_story=options["load_sprint_story"],
    )

    readiness = _build_readiness_summary(
        tasks=options["load_tasks"](),
        task_progress=options["task_progress"],
    )

    close_eligible = readiness.all_actionable_tasks_done
    ineligible_reason = (
        None
        if close_eligible
        else "Not all actionable tasks are completed or cancelled."
    )
    if readiness.total_tasks == 0:
        close_eligible = False
        ineligible_reason = "Story has no executable tasks."

    if story.status in (StoryStatus.ACCEPTED, StoryStatus.DONE):
        close_eligible = False
        ineligible_reason = f"Story is already {story.status.value}."

    return {
        "success": True,
        "story_id": story_id,
        "sprint_id": sprint_id,
        "current_status": story.status.value,
        "resolution": getattr(story, "resolution", None),
        "completion_notes": getattr(story, "completion_notes", None),
        "evidence_links": getattr(story, "evidence_links", None),
        "completed_at": getattr(story, "completed_at", None),
        "readiness": readiness,
        "close_eligible": close_eligible,
        "ineligible_reason": ineligible_reason,
    }


def close_story(
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
    **options: Unpack[_StoryCloseOptions],
) -> dict[str, Any]:
    """Close a story, persist completion details, and return the updated payload."""
    story, _sprint = _load_story_close_subject(
        project_id=project_id,
        load_story=options["load_story"],
        load_sprint=options["load_sprint"],
        load_sprint_story=options["load_sprint_story"],
    )

    readiness = _build_readiness_summary(
        tasks=options["load_tasks"](),
        task_progress=options["task_progress"],
    )

    if readiness.total_tasks == 0:
        raise StoryCloseServiceError.no_executable_tasks()

    if not readiness.all_actionable_tasks_done:
        raise StoryCloseServiceError.incomplete_tasks()

    old_status = story.status
    if old_status in (StoryStatus.ACCEPTED, StoryStatus.DONE):
        raise StoryCloseServiceError.already_closed(old_status)

    evidence_json = (
        json.dumps(list(options["evidence_links"]))
        if options["evidence_links"]
        else None
    )

    story.status = StoryStatus.DONE
    story.resolution = options["resolution"]
    story.completion_notes = options["completion_notes"]
    story.evidence_links = evidence_json
    story.completed_at = options["now"]()

    options["persist_story_close"](
        story=story,
        old_status=old_status,
        evidence_json=evidence_json,
        known_gaps=options["known_gaps"],
        follow_up_notes=options["follow_up_notes"],
        changed_by=options["changed_by"] or "manual-ui",
    )

    return {
        "success": True,
        "story_id": story_id,
        "sprint_id": sprint_id,
        "current_status": story.status.value,
        "resolution": story.resolution,
        "completion_notes": story.completion_notes,
        "evidence_links": story.evidence_links,
        "completed_at": story.completed_at,
        "readiness": readiness,
        "close_eligible": False,
        "ineligible_reason": None,
    }


def _story_close_subject(
    session: Session,
    command: StoryCloseInput,
) -> tuple[Sprint, UserStory]:
    sprint = session.get(Sprint, command.sprint_id)
    story = session.get(UserStory, command.story_id)
    if story is None:
        raise StoryCloseServiceError.story_not_found()
    if sprint is None or sprint.project_id != command.project_id:
        raise StoryCloseServiceError.sprint_not_in_project()
    membership = session.exec(
        select(SprintStory).where(
            col(SprintStory.sprint_id) == command.sprint_id,
            col(SprintStory.story_id) == command.story_id,
        )
    ).one_or_none()
    if story.project_id != command.project_id or membership is None:
        raise StoryCloseServiceError.story_not_in_sprint()
    return sprint, story


def _terminal_story_tasks(
    session: Session,
    command: StoryCloseInput,
) -> tuple[tuple[Task, ...], tuple[int, ...]]:
    tasks = tuple(
        session.exec(
            select(Task)
            .where(col(Task.story_id) == command.story_id)
            .order_by(col(Task.task_id))
        ).all()
    )
    if not tasks:
        raise StoryCloseServiceError.no_executable_tasks()
    if any(
        item.status not in {TaskStatus.DONE, TaskStatus.CANCELLED} for item in tasks
    ):
        raise StoryCloseServiceError.incomplete_tasks()
    done_ids = tuple(
        item.task_id
        for item in tasks
        if item.status is TaskStatus.DONE and item.task_id is not None
    )
    evidence_count = (
        len(
            session.exec(
                select(TaskCompletionEvidence).where(
                    col(TaskCompletionEvidence.sprint_id) == command.sprint_id,
                    col(TaskCompletionEvidence.task_id).in_(done_ids),
                )
            ).all()
        )
        if done_ids
        else 0
    )
    if evidence_count != len(done_ids):
        message = "Done Tasks require immutable completion evidence."
        raise StoryCloseServiceError(message, status_code=409)
    return tasks, done_ids


@dataclass(frozen=True)
class _ValidatedStoryClosure:
    """Semantic close facts resolved before original or retry persistence."""

    story: UserStory
    scope: ExecutionScope
    normalized_resolution: StoryResolution
    completion_fingerprint: str
    retry_state: SprintRetryStoryState | None


def _story_open_message(command: StoryCloseInput) -> str:
    if command.retry_attempt_id is None:
        return "Story close requires an active Sprint."
    return "Story close requires an active Sprint retry."


def _validate_story_closure(
    session: Session,
    command: StoryCloseInput,
) -> _ValidatedStoryClosure:
    """Validate one attempt-scoped Story close before choosing its persistence row."""
    sprint, story = _story_close_subject(session, command)
    retry_state = _validate_story_attempt_open(session, command, sprint, story)

    snapshot = WorkflowFactRepository(session).load(command.project_id)
    try:
        scope = resolve_execution_scope(
            snapshot,
            sprint_id=command.sprint_id,
            retry_attempt_id=command.retry_attempt_id,
        )
    except ExecutionScopeError as error:
        raise StoryCloseServiceError(str(error), status_code=409) from error
    if scope.status.lower() != "active":
        raise StoryCloseServiceError(_story_open_message(command), status_code=409)
    scoped_story = next(
        (item for item in scope.stories if item.story_id == command.story_id),
        None,
    )
    scoped_tasks = tuple(
        item for item in scope.tasks if item.story_id == command.story_id
    )
    if scoped_story is None:
        raise StoryCloseServiceError.story_not_in_sprint()
    if not scoped_tasks:
        raise StoryCloseServiceError.no_executable_tasks()
    if any(item.status not in {"Done", "Cancelled"} for item in scoped_tasks):
        raise StoryCloseServiceError.incomplete_tasks()
    try:
        expected = story_completion_eligibility_fingerprint(
            snapshot,
            sprint_id=command.sprint_id,
            story_id=command.story_id,
            scope=scope,
        )
    except ValueError as error:
        raise StoryCloseServiceError(str(error), status_code=409) from error
    if command.completion_fingerprint != expected:
        raise StoryCloseServiceError(_COMPLETION_FINGERPRINT_STALE, status_code=409)
    try:
        normalized_resolution = StoryResolution(command.resolution)
    except ValueError as error:
        raise StoryCloseServiceError(_RESOLUTION_INVALID, status_code=409) from error
    if command.retry_attempt_id is None:
        _ensure_original_story_closure_absent(session, command)
    completion_fingerprint = story_completion_fingerprint(
        snapshot,
        sprint_id=command.sprint_id,
        story_id=command.story_id,
        closure=StoryClosurePayload(
            resolution=normalized_resolution.value,
            delivered=command.delivered,
            evidence=command.evidence,
            known_gaps=command.known_gaps,
        ),
        scope=scope,
    )
    return _ValidatedStoryClosure(
        story=story,
        scope=scope,
        normalized_resolution=normalized_resolution,
        completion_fingerprint=completion_fingerprint,
        retry_state=retry_state,
    )


def _validate_story_attempt_open(
    session: Session,
    command: StoryCloseInput,
    sprint: Sprint,
    story: UserStory,
) -> SprintRetryStoryState | None:
    """Validate the mutable rows for the selected original or retry Story."""
    if command.retry_attempt_id is not None:
        return _open_retry_story_state(session, command)
    if sprint.status.value != "Active":
        raise StoryCloseServiceError(_story_open_message(command), status_code=409)
    if story.status in {StoryStatus.DONE, StoryStatus.ACCEPTED}:
        raise StoryCloseServiceError.already_closed(story.status)
    _terminal_story_tasks(session, command)
    return None


def _ensure_original_story_closure_absent(
    session: Session,
    command: StoryCloseInput,
) -> None:
    """Reject a second append to the original execution closure stream."""
    existing = session.exec(
        select(StoryClosure).where(
            col(StoryClosure.story_id) == command.story_id,
            col(StoryClosure.sprint_id) == command.sprint_id,
        )
    ).one_or_none()
    if existing is not None:
        raise StoryCloseServiceError(_ORIGINAL_CLOSURE_IMMUTABLE, status_code=409)


def _open_retry_story_state(
    session: Session,
    command: StoryCloseInput,
) -> SprintRetryStoryState:
    """Load the one mutable retry Story state required by this closure."""
    if command.retry_attempt_id is None:
        raise RuntimeError(_RETRY_STORY_SCOPE_REQUIRED)
    retry_state = session.exec(
        select(SprintRetryStoryState).where(
            col(SprintRetryStoryState.retry_attempt_id) == command.retry_attempt_id,
            col(SprintRetryStoryState.story_id) == command.story_id,
        )
    ).one_or_none()
    if retry_state is None or retry_state.status in {"Done", "Accepted"}:
        closed_status = (
            StoryStatus.DONE if retry_state is not None else StoryStatus.ACCEPTED
        )
        raise StoryCloseServiceError.already_closed(closed_status)
    return retry_state


def close_story_in_session(
    session: Session,
    command: StoryCloseInput,
) -> StoryClosure | SprintRetryStoryClosure:
    """Close one Story and append its audit rows in the caller transaction."""
    validated = _validate_story_closure(session, command)
    if command.retry_attempt_id is not None:
        return _persist_retry_story_closure(session, command, validated)
    return _persist_original_story_closure(session, command, validated)


def _persist_original_story_closure(
    session: Session,
    command: StoryCloseInput,
    validated: _ValidatedStoryClosure,
) -> StoryClosure:
    """Append original Story closure after attempt-scoped validation."""
    old_status = validated.story.status
    validated.story.status = StoryStatus.DONE
    validated.story.resolution = validated.normalized_resolution
    validated.story.completion_notes = command.delivered
    validated.story.evidence_links = canonical_json([command.evidence])
    validated.story.completed_at = command.closed_at
    closure = StoryClosure(
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        story_id=command.story_id,
        completion_fingerprint=validated.completion_fingerprint,
        resolution=validated.normalized_resolution.value,
        delivered=command.delivered,
        evidence=command.evidence,
        known_gaps=command.known_gaps,
        closed_by=command.closed_by,
        closed_at=command.closed_at,
    )
    session.add(validated.story)
    session.add(closure)
    session.add(
        StoryCompletionLog(
            story_id=command.story_id,
            old_status=old_status,
            new_status=StoryStatus.DONE,
            resolution=validated.normalized_resolution,
            delivered=command.delivered,
            evidence=command.evidence,
            known_gaps=command.known_gaps,
            changed_by=command.closed_by,
            changed_at=command.closed_at,
        )
    )
    session.flush()
    return closure


def _persist_retry_story_closure(
    session: Session,
    command: StoryCloseInput,
    validated: _ValidatedStoryClosure,
) -> SprintRetryStoryClosure:
    """Append retry Story closure without changing original execution rows."""
    if command.retry_attempt_id is None or validated.retry_state is None:
        raise RuntimeError(_RETRY_STORY_SCOPE_REQUIRED)
    existing = session.exec(
        select(SprintRetryStoryClosure).where(
            col(SprintRetryStoryClosure.retry_attempt_id) == command.retry_attempt_id,
            col(SprintRetryStoryClosure.story_id) == command.story_id,
        )
    ).one_or_none()
    if existing is not None:
        raise StoryCloseServiceError(_RETRY_CLOSURE_IMMUTABLE, status_code=409)
    validated.retry_state.status = StoryStatus.DONE.value
    closure = SprintRetryStoryClosure(
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        retry_attempt_id=command.retry_attempt_id,
        story_id=command.story_id,
        completion_fingerprint=validated.completion_fingerprint,
        resolution=validated.normalized_resolution.value,
        delivered=command.delivered,
        evidence=command.evidence,
        known_gaps=command.known_gaps,
        closed_by=command.closed_by,
        closed_at=command.closed_at,
    )
    session.add(validated.retry_state)
    session.add(closure)
    session.flush()
    return closure
