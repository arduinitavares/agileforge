"""Resolve immutable original or retry execution facts without graph selection."""
# ruff: noqa: EM101, TRY003

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from workflow.execution_integrity import ExecutionContract, execution_contract
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        PostSprintTriageFact,
        SprintClosureFact,
        SprintRetryFact,
        SprintReviewFact,
        StoryCompletionFact,
        StoryDependencyFact,
        StoryFact,
        TaskCompletionFact,
        TaskFact,
        WorkflowFactSnapshot,
    )


class ExecutionScopeError(ValueError):
    """One attempt cannot be resolved to its immutable execution contract."""


@dataclass(frozen=True)
class ExecutionScope:
    """The effective facts for one original or persisted retry Sprint attempt."""

    sprint_id: int
    retry_attempt_id: int | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    contract: ExecutionContract
    stories: tuple[StoryFact, ...]
    project_stories: tuple[StoryFact, ...]
    tasks: tuple[TaskFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    task_completions: tuple[TaskCompletionFact, ...]
    story_completions: tuple[StoryCompletionFact, ...]
    sprint_reviews: tuple[SprintReviewFact, ...]
    sprint_closures: tuple[SprintClosureFact, ...]
    post_sprint_triage: tuple[PostSprintTriageFact, ...]


def resolve_execution_scope(
    snapshot: WorkflowFactSnapshot,
    *,
    sprint_id: int,
    retry_attempt_id: int | None = None,
) -> ExecutionScope:
    """Resolve exactly one original or retry attempt from durable snapshot facts."""
    contract = execution_contract(snapshot, sprint_id)
    if retry_attempt_id is None:
        sprint = next(
            (item for item in snapshot.sprints if item.sprint_id == sprint_id), None
        )
        if sprint is None:
            raise ExecutionScopeError("Execution Sprint is missing.")
        start = contract.start
        return ExecutionScope(
            sprint_id=sprint_id,
            retry_attempt_id=None,
            status=sprint.status,
            started_at=start.started_at,
            completed_at=sprint.completed_at,
            contract=contract,
            stories=contract.stories,
            project_stories=snapshot.stories,
            tasks=contract.tasks,
            dependencies=contract.dependencies,
            task_completions=tuple(
                item
                for item in snapshot.task_completions
                if item.sprint_id == sprint_id
            ),
            story_completions=tuple(
                item
                for item in snapshot.story_completions
                if item.sprint_id == sprint_id
            ),
            sprint_reviews=tuple(
                item for item in snapshot.sprint_reviews if item.sprint_id == sprint_id
            ),
            sprint_closures=tuple(
                item for item in snapshot.sprint_closures if item.sprint_id == sprint_id
            ),
            post_sprint_triage=tuple(
                item
                for item in snapshot.post_sprint_triage
                if item.sprint_id == sprint_id
            ),
        )
    retry = _retry(snapshot, sprint_id, retry_attempt_id)
    if retry.contract_fingerprint != contract.fingerprint:
        raise ExecutionScopeError("Retry execution contract fingerprint changed.")
    _reject_duplicate_progress(retry)
    task_statuses = dict(retry.task_statuses)
    story_statuses = dict(retry.story_statuses)
    task_ids = {item.task_id for item in contract.tasks}
    story_ids = {item.story_id for item in contract.stories}
    if set(task_statuses) != task_ids or set(story_statuses) != story_ids:
        raise ExecutionScopeError("Retry progress does not exactly match its contract.")
    stories = tuple(
        item.model_copy(update={"status": story_statuses[item.story_id]})
        for item in contract.stories
    )
    project_stories = tuple(
        item.model_copy(update={"status": story_statuses[item.story_id]})
        if item.story_id in story_statuses
        else item
        for item in snapshot.stories
    )
    status_by_story = {item.story_id: item.status for item in project_stories}
    tasks = tuple(
        item.model_copy(
            update={
                "status": task_statuses[item.task_id],
                "dependencies_satisfied": _dependencies_complete(
                    item.story_id, contract.dependencies, status_by_story
                ),
            }
        )
        for item in contract.tasks
    )
    effective_contract = replace(
        contract,
        stories=stories,
        tasks=tasks,
        fingerprint=canonical_hash(
            {
                "source_execution_contract_fingerprint": contract.fingerprint,
                "retry_attempt_id": retry.retry_attempt_id,
                "sprint_id": retry.sprint_id,
            }
        ),
    )
    return ExecutionScope(
        sprint_id=sprint_id,
        retry_attempt_id=retry_attempt_id,
        status=retry.status,
        started_at=retry.started_at,
        completed_at=retry.completed_at,
        contract=effective_contract,
        stories=stories,
        project_stories=project_stories,
        tasks=tasks,
        dependencies=contract.dependencies,
        task_completions=retry.task_completions,
        story_completions=retry.story_completions,
        sprint_reviews=retry.sprint_reviews,
        sprint_closures=retry.sprint_closures,
        post_sprint_triage=retry.post_sprint_triage,
    )


def _retry(
    snapshot: WorkflowFactSnapshot, sprint_id: int, retry_attempt_id: int
) -> SprintRetryFact:
    matches = tuple(
        item
        for item in snapshot.sprint_retries
        if item.sprint_id == sprint_id and item.retry_attempt_id == retry_attempt_id
    )
    if len(matches) != 1:
        raise ExecutionScopeError("Retry attempt is missing or ambiguous.")
    return matches[0]


def _reject_duplicate_progress(retry: SprintRetryFact) -> None:
    if len({item[0] for item in retry.task_statuses}) != len(retry.task_statuses):
        raise ExecutionScopeError("Retry Task progress has duplicate subjects.")
    if len({item[0] for item in retry.story_statuses}) != len(retry.story_statuses):
        raise ExecutionScopeError("Retry Story progress has duplicate subjects.")


def _dependencies_complete(
    story_id: int,
    dependencies: tuple[StoryDependencyFact, ...],
    status_by_story: dict[int, str],
) -> bool:
    """Require completed scoped prerequisites while retaining completed externals."""
    return all(
        status_by_story.get(edge.prerequisite_story_id) in {"Done", "Accepted"}
        for edge in dependencies
        if edge.status == "active" and edge.dependent_story_id == story_id
    )
