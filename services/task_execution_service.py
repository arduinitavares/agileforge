# services/task_execution_service.py

"""Task execution endpoint orchestration helpers."""

from __future__ import annotations

import json
import typing
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, Self, TypedDict, Unpack

from pydantic import ValidationError
from sqlmodel import Session, col, select

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from workflow.contracts import JsonObject

from models.core import Sprint, SprintStory, Task, UserStory
from models.enums import TaskAcceptanceResult, TaskStatus
from models.events import TaskExecutionLog
from models.workflow import TaskCompletionEvidence
from utils.api_schemas import TaskExecutionLogEntry
from utils.task_metadata import TaskMetadata
from workflow.execution_integrity import task_evidence_fingerprint
from workflow.facts import TaskFact
from workflow.fingerprints import canonical_json


class TaskExecutionServiceError(Exception):
    """Domain-level task execution error for router translation."""

    def __init__(self, detail: str, *, status_code: int) -> None:
        """Store an API-ready error detail and HTTP status code."""
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code

    @classmethod
    def task_not_found(cls) -> Self:
        """Build the 404 error raised when the task cannot be loaded."""
        return cls(detail="Task not found", status_code=404)

    @classmethod
    def sprint_not_in_project(cls) -> Self:
        """Build the 404 error raised for cross-project or missing sprints."""
        return cls(detail="Sprint not found in this project", status_code=404)

    @classmethod
    def task_not_in_sprint(cls) -> Self:
        """Build the 404 error raised when the task is outside the sprint."""
        return cls(detail="Task does not belong to the given sprint", status_code=404)

    @classmethod
    def task_not_executable(cls) -> Self:
        """Build the 409 error raised for tasks without executable checklist items."""
        return cls(detail="Task has no executable checklist items.", status_code=409)


@dataclass(frozen=True)
class TaskCompletionInput:
    """Validated caller inputs for one durable Task completion."""

    project_id: int
    sprint_id: int
    task_id: int
    outcome_summary: str
    artifact_refs: tuple[str, ...]
    acceptance_result: typing.Literal["partially_met", "fully_met"]
    checklist_result: JsonObject
    completed_by: str
    completed_at: datetime


class _TaskLike(Protocol):
    @property
    def task_id(self) -> int | None: ...

    @property
    def story_id(self) -> int: ...

    status: TaskStatus

    @property
    def metadata_json(self) -> str | None: ...


class _SprintLike(Protocol):
    @property
    def sprint_id(self) -> int | None: ...

    @property
    def product_id(self) -> int | None: ...


class _TaskExecutionLogLike(Protocol):
    @property
    def log_id(self) -> int | None: ...

    @property
    def task_id(self) -> int: ...

    @property
    def sprint_id(self) -> int: ...

    @property
    def old_status(self) -> TaskStatus | None: ...

    @property
    def new_status(self) -> TaskStatus: ...

    @property
    def outcome_summary(self) -> str | None: ...

    @property
    def artifact_refs_json(self) -> object | None: ...

    @property
    def acceptance_result(self) -> TaskAcceptanceResult: ...

    @property
    def notes(self) -> str | None: ...

    @property
    def changed_by(self) -> str: ...

    @property
    def changed_at(self) -> datetime: ...


class _TaskMetadataLike(Protocol):
    @property
    def checklist_items(self) -> Sequence[object]: ...


class _PersistExecutionLogOptions(TypedDict):
    task: _TaskLike
    old_status: TaskStatus
    new_status: TaskStatus
    outcome_summary: str | None
    artifact_refs_json: str | None
    notes: str | None
    acceptance_result: TaskAcceptanceResult
    changed_by: str


class _PersistExecutionLog(Protocol):
    def __call__(self, **kwargs: Unpack[_PersistExecutionLogOptions]) -> None: ...


class _TaskExecutionSubjectOptions(TypedDict):
    load_task: Callable[[], _TaskLike | None]
    load_sprint: Callable[[], _SprintLike | None]
    load_sprint_story: Callable[[_TaskLike], object | None]


class _TaskExecutionHistoryOptions(_TaskExecutionSubjectOptions):
    load_logs: Callable[[], Sequence[_TaskExecutionLogLike]]


class _TaskExecutionRecordOptions(_TaskExecutionHistoryOptions):
    new_status: TaskStatus | None
    outcome_summary: str | None
    artifact_refs: Sequence[str] | None
    notes: str | None
    acceptance_result: TaskAcceptanceResult | None
    changed_by: str | None
    parse_task_metadata: Callable[[str | None], _TaskMetadataLike]
    persist_execution_log: _PersistExecutionLog


def _load_task_execution_subject(
    *,
    project_id: int,
    load_task: Callable[[], _TaskLike | None],
    load_sprint: Callable[[], _SprintLike | None],
    load_sprint_story: Callable[[_TaskLike], object | None],
) -> tuple[_TaskLike, _SprintLike]:
    task: _TaskLike | None = load_task()
    if not task:
        raise TaskExecutionServiceError.task_not_found()

    sprint = load_sprint()
    if not sprint or getattr(sprint, "product_id", None) != project_id:
        raise TaskExecutionServiceError.sprint_not_in_project()

    sprint_story: object = load_sprint_story(task)
    if not sprint_story:
        raise TaskExecutionServiceError.task_not_in_sprint()

    return task, sprint


def _deserialize_artifact_refs(raw_value: object) -> list[str]:
    if not raw_value:
        return []
    with suppress(Exception):
        value: Any = json.loads(str(raw_value))
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
    return []


def _normalize_artifact_refs(raw_refs: Sequence[str] | None) -> str | None:
    if not raw_refs:
        return None

    refs: list[str] = []
    seen: set[str] = set()
    for ref in raw_refs:
        normalized: str = str(ref).strip()
        if normalized and normalized not in seen:
            refs.append(normalized)
            seen.add(normalized)
    return json.dumps(refs) if refs else None


def get_task_execution_history(
    *,
    project_id: int,
    sprint_id: int,
    task_id: int,
    **options: Unpack[_TaskExecutionHistoryOptions],
) -> dict[str, Any]:
    """Return the current task status together with its persisted execution history."""
    task, _sprint = _load_task_execution_subject(
        project_id=project_id,
        load_task=options["load_task"],
        load_sprint=options["load_sprint"],
        load_sprint_story=options["load_sprint_story"],
    )

    history: list[TaskExecutionLogEntry] = []
    for log in options["load_logs"]():
        log_id = log.log_id
        if log_id is None:
            continue
        history.append(
            TaskExecutionLogEntry(
                log_id=log_id,
                task_id=log.task_id,
                sprint_id=log.sprint_id,
                old_status=log.old_status,
                new_status=log.new_status,
                outcome_summary=log.outcome_summary,
                artifact_refs=_deserialize_artifact_refs(
                    getattr(log, "artifact_refs_json", None)
                ),
                acceptance_result=log.acceptance_result,
                notes=log.notes,
                changed_by=log.changed_by,
                changed_at=log.changed_at,
            )
        )

    return {
        "success": True,
        "task_id": task_id,
        "sprint_id": sprint_id,
        "current_status": task.status,
        "latest_entry": history[0] if history else None,
        "history": history,
    }


def record_task_execution(
    *,
    project_id: int,
    sprint_id: int,
    task_id: int,
    **options: Unpack[_TaskExecutionRecordOptions],
) -> dict[str, Any]:
    """Persist a task execution update and return the refreshed execution history."""
    task, _sprint = _load_task_execution_subject(
        project_id=project_id,
        load_task=options["load_task"],
        load_sprint=options["load_sprint"],
        load_sprint_story=options["load_sprint_story"],
    )

    task_metadata = options["parse_task_metadata"](getattr(task, "metadata_json", None))
    if not getattr(task_metadata, "checklist_items", []):
        raise TaskExecutionServiceError.task_not_executable()

    old_status: TaskStatus = task.status
    if options["new_status"] is not None:
        task.status = options["new_status"]

    options["persist_execution_log"](
        task=task,
        old_status=old_status,
        new_status=task.status,
        outcome_summary=options["outcome_summary"],
        artifact_refs_json=_normalize_artifact_refs(options["artifact_refs"]),
        notes=options["notes"],
        acceptance_result=options["acceptance_result"]
        or TaskAcceptanceResult.NOT_CHECKED,
        changed_by=options["changed_by"] or "manual-ui",
    )

    return get_task_execution_history(
        project_id=project_id,
        sprint_id=sprint_id,
        task_id=task_id,
        load_task=options["load_task"],
        load_sprint=options["load_sprint"],
        load_sprint_story=options["load_sprint_story"],
        load_logs=options["load_logs"],
    )


def _completion_metadata(
    task: Task,
    checklist_result: JsonObject,
) -> TaskMetadata:
    if task.metadata_json is None:
        message = "Task metadata is invalid."
        raise TaskExecutionServiceError(message, status_code=409)
    try:
        metadata = TaskMetadata.model_validate_json(task.metadata_json)
    except (ValidationError, ValueError, TypeError) as exc:
        message = "Task metadata is invalid."
        raise TaskExecutionServiceError(message, status_code=409) from exc
    if not metadata.checklist_items:
        raise TaskExecutionServiceError.task_not_executable()
    if set(checklist_result) != set(metadata.checklist_items):
        message = "Checklist result must cover every executable checklist item."
        raise TaskExecutionServiceError(message, status_code=409)
    return metadata


def complete_task_in_session(
    session: Session,
    command: TaskCompletionInput,
) -> TaskCompletionEvidence:
    """Complete one exact Sprint Task inside the caller's transaction."""
    sprint = session.get(Sprint, command.sprint_id)
    task = session.get(Task, command.task_id)
    if sprint is None or sprint.product_id != command.project_id:
        raise TaskExecutionServiceError.sprint_not_in_project()
    if task is None:
        raise TaskExecutionServiceError.task_not_found()
    story = session.get(UserStory, task.story_id)
    membership = session.exec(
        select(SprintStory).where(
            col(SprintStory.sprint_id) == command.sprint_id,
            col(SprintStory.story_id) == task.story_id,
        )
    ).one_or_none()
    if story is None or story.product_id != command.project_id or membership is None:
        raise TaskExecutionServiceError.task_not_in_sprint()
    if sprint.status.value != "Active" or task.status in {
        TaskStatus.DONE,
        TaskStatus.CANCELLED,
    }:
        message = "Task is not open in the active Sprint."
        raise TaskExecutionServiceError(message, status_code=409)
    metadata = _completion_metadata(task, command.checklist_result)
    normalized_outcome = command.outcome_summary.strip()
    if not normalized_outcome:
        message = "Task completion requires an outcome summary."
        raise TaskExecutionServiceError(message, status_code=409)
    normalized_refs = tuple(
        sorted({item.strip() for item in command.artifact_refs if item.strip()})
    )
    if metadata.artifact_targets and not normalized_refs:
        message = "Task completion requires artifact references."
        raise TaskExecutionServiceError(message, status_code=409)
    existing = session.exec(
        select(TaskCompletionEvidence).where(
            col(TaskCompletionEvidence.task_id) == command.task_id,
            col(TaskCompletionEvidence.sprint_id) == command.sprint_id,
        )
    ).one_or_none()
    if existing is not None:
        message = "Task completion evidence is immutable."
        raise TaskExecutionServiceError(message, status_code=409)
    task_fact = TaskFact(
        task_id=command.task_id,
        sprint_id=command.sprint_id,
        story_id=task.story_id,
        description=task.description,
        metadata_json=task.metadata_json or "",
        status=TaskStatus.DONE.value,
        dependencies_satisfied=True,
    )
    evidence_fingerprint = task_evidence_fingerprint(
        task_fact,
        outcome_summary=normalized_outcome,
        artifact_refs=normalized_refs,
        acceptance_result=command.acceptance_result,
        checklist_result=command.checklist_result,
    )
    old_status = task.status
    task.status = TaskStatus.DONE
    task.updated_at = command.completed_at
    evidence = TaskCompletionEvidence(
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        task_id=command.task_id,
        outcome_summary=normalized_outcome,
        artifact_refs_json=canonical_json(list(normalized_refs)),
        acceptance_result=command.acceptance_result,
        checklist_result_json=canonical_json(command.checklist_result),
        evidence_fingerprint=evidence_fingerprint,
        completed_by=command.completed_by,
        completed_at=command.completed_at,
    )
    session.add(task)
    session.add(evidence)
    session.add(
        TaskExecutionLog(
            task_id=command.task_id,
            sprint_id=command.sprint_id,
            old_status=old_status,
            new_status=TaskStatus.DONE,
            outcome_summary=normalized_outcome,
            artifact_refs_json=canonical_json(list(normalized_refs)),
            acceptance_result=TaskAcceptanceResult(command.acceptance_result),
            notes=canonical_json(command.checklist_result),
            changed_by=command.completed_by,
            changed_at=command.completed_at,
        )
    )
    session.flush()
    return evidence
