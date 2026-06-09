"""Story close endpoint orchestration helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, Self, TypedDict, Unpack

from models.enums import StoryResolution
from models.enums import StoryStatus
from utils.api_schemas import StoryTaskProgressSummary

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


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
    def product_id(self) -> int | None: ...


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
    if not sprint or getattr(sprint, "product_id", None) != project_id:
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
