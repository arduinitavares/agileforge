"""Resolve immutable original or retry execution facts without graph selection."""
# ruff: noqa: C901, EM101, PLR2004, TRY003

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from services.planning_lineage import (
    PlanningLineageError,
    select_current_accepted_artifact,
)
from workflow.execution_integrity import (
    ExecutionContract,
    ExecutionIntegrityError,
    execution_contract,
    sprint_close_fingerprint,
    sprint_review_fingerprint,
    triage_payload_fingerprint,
)
from workflow.fingerprints import canonical_hash
from workflow.sprint_lineage import (
    current_sprint_stream_artifacts,
    plan_has_matching_sprint_start,
    sprint_stream_nodes,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        PostSprintTriageFact,
        SprintClosureFact,
        SprintFact,
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
    try:
        contract = execution_contract(snapshot, sprint_id)
    except ExecutionIntegrityError as exc:
        raise ExecutionScopeError("Execution contract is invalid.") from exc
    if retry_attempt_id is None:
        sprint = _source_sprint(snapshot, sprint_id)
        if sprint.status == "planned":
            raise ExecutionScopeError("Original planned Sprint has not started.")
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
    _validate_retry(snapshot, retry, contract)
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


def current_execution_scope(snapshot: WorkflowFactSnapshot) -> ExecutionScope | None:
    """Select the sole live attempt or proven terminal attempt for the project."""
    retries = _validated_retries(snapshot)
    live_originals = tuple(
        item for item in snapshot.sprints if item.status in {"planned", "active"}
    )
    live_retries = tuple(
        item for item in retries if item.status in {"planned", "active"}
    )
    live_count = len(live_originals) + len(live_retries)
    if live_count > 1:
        raise ExecutionScopeError("multiple live original or retry attempts exist.")
    _require_finalized_predecessors(snapshot, retries)
    if live_retries:
        retry = live_retries[0]
        scope = resolve_execution_scope(
            snapshot,
            sprint_id=retry.sprint_id,
            retry_attempt_id=retry.retry_attempt_id,
        )
        if scope.status == "active":
            _require_current_active_attempt(snapshot, scope)
        return scope
    if live_originals:
        sprint = live_originals[0]
        if sprint.status == "planned":
            return None
        scope = resolve_execution_scope(snapshot, sprint_id=sprint.sprint_id)
        _require_current_active_attempt(snapshot, scope)
        return scope

    sprint_id = _current_terminal_sprint_id(snapshot)
    if sprint_id is None:
        return None
    terminal_retries = tuple(
        item
        for item in retries
        if item.sprint_id == sprint_id and item.status == "completed"
    )
    if not terminal_retries:
        scope = resolve_execution_scope(snapshot, sprint_id=sprint_id)
        _require_current_closed_attempt(snapshot, scope)
        return scope
    retry = max(terminal_retries, key=lambda item: item.ordinal)
    scope = resolve_execution_scope(
        snapshot,
        sprint_id=sprint_id,
        retry_attempt_id=retry.retry_attempt_id,
    )
    _require_current_closed_attempt(snapshot, scope)
    return scope


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


def _source_sprint(snapshot: WorkflowFactSnapshot, sprint_id: int) -> SprintFact:
    matches = tuple(item for item in snapshot.sprints if item.sprint_id == sprint_id)
    if len(matches) != 1:
        raise ExecutionScopeError("Execution Sprint is missing or ambiguous.")
    return matches[0]


def _validated_retries(snapshot: WorkflowFactSnapshot) -> tuple[SprintRetryFact, ...]:
    """Validate every persisted retry chain before selecting any current attempt."""
    retries = snapshot.sprint_retries
    if len({item.retry_attempt_id for item in retries}) != len(retries):
        raise ExecutionScopeError("Retry attempt identity is ambiguous.")
    if len({(item.sprint_id, item.ordinal) for item in retries}) != len(retries):
        raise ExecutionScopeError("Retry attempt ordinal is ambiguous.")
    by_id = {item.retry_attempt_id: item for item in retries}
    for retry in retries:
        if retry.project_id != snapshot.project.project_id:
            raise ExecutionScopeError("Retry attempt is not owned by this Project.")
        try:
            contract = execution_contract(snapshot, retry.sprint_id)
        except ExecutionIntegrityError as exc:
            raise ExecutionScopeError(
                "Retry source execution contract is invalid."
            ) from exc
        _validate_retry(snapshot, retry, contract)
        _validate_retry_link(retry, by_id)
    return retries


def _validate_retry_link(
    retry: SprintRetryFact,
    by_id: dict[int, SprintRetryFact],
) -> None:
    if retry.ordinal < 2:
        raise ExecutionScopeError("Retry attempt ordinal is invalid.")
    predecessor_id = retry.predecessor_retry_attempt_id
    if retry.ordinal == 2:
        if predecessor_id is not None:
            raise ExecutionScopeError(
                "Retry lineage must begin from the original attempt."
            )
        return
    predecessor = None if predecessor_id is None else by_id.get(predecessor_id)
    if (
        predecessor is None
        or predecessor.sprint_id != retry.sprint_id
        or predecessor.project_id != retry.project_id
        or predecessor.ordinal != retry.ordinal - 1
        or predecessor.status != "completed"
    ):
        raise ExecutionScopeError(
            "Retry lineage does not link the prior completed ordinal."
        )


def _require_finalized_predecessors(
    snapshot: WorkflowFactSnapshot,
    retries: tuple[SprintRetryFact, ...],
) -> None:
    """Require a full terminal lifecycle before a successor can be current."""
    for retry in retries:
        if retry.ordinal == 2:
            predecessor_scope = resolve_execution_scope(
                snapshot,
                sprint_id=retry.sprint_id,
            )
        else:
            predecessor_id = retry.predecessor_retry_attempt_id
            if predecessor_id is None:
                raise ExecutionScopeError("Retry lineage has no predecessor.")
            predecessor_scope = resolve_execution_scope(
                snapshot,
                sprint_id=retry.sprint_id,
                retry_attempt_id=predecessor_id,
            )
        _require_finalized_attempt(snapshot, predecessor_scope)


def _validate_retry(
    snapshot: WorkflowFactSnapshot,
    retry: SprintRetryFact,
    contract: ExecutionContract,
) -> None:
    if retry.project_id != snapshot.project.project_id:
        raise ExecutionScopeError("Retry attempt is not owned by this Project.")
    if retry.contract_fingerprint != contract.fingerprint:
        raise ExecutionScopeError("Retry execution contract fingerprint changed.")
    _reject_duplicate_progress(retry)
    _validate_retry_lifecycle(retry)
    task_ids = {item.task_id for item in contract.tasks}
    story_ids = {item.story_id for item in contract.stories}
    if {item[0] for item in retry.task_statuses} != task_ids or {
        item[0] for item in retry.story_statuses
    } != story_ids:
        raise ExecutionScopeError("Retry progress does not exactly match its contract.")
    _validate_retry_evidence(retry, task_ids, story_ids)


def _validate_retry_lifecycle(retry: SprintRetryFact) -> None:
    if retry.status not in {"planned", "active", "completed"}:
        raise ExecutionScopeError("Retry attempt lifecycle status is invalid.")
    if retry.status == "planned":
        if (
            retry.start is not None
            or retry.started_at is not None
            or retry.completed_at is not None
            or retry.task_completions
            or retry.story_completions
            or retry.sprint_reviews
            or retry.sprint_closures
            or retry.post_sprint_triage
        ):
            raise ExecutionScopeError("Planned retry has execution evidence.")
        return
    start = retry.start
    if (
        start is None
        or start.retry_attempt_id != retry.retry_attempt_id
        or start.contract_fingerprint != retry.contract_fingerprint
        or retry.started_at != start.started_at
    ):
        raise ExecutionScopeError("Retry start does not match its attempt lifecycle.")
    if retry.status == "active":
        if (
            retry.completed_at is not None
            or len(retry.sprint_reviews) > 1
            or retry.sprint_closures
            or retry.post_sprint_triage
        ):
            raise ExecutionScopeError("Active retry has terminal lifecycle evidence.")
        return
    started_at = retry.started_at
    if (
        retry.completed_at is None
        or started_at is None
        or retry.completed_at < started_at
    ):
        raise ExecutionScopeError("Completed retry timestamps are invalid.")


def _validate_retry_evidence(
    retry: SprintRetryFact,
    task_ids: set[int],
    story_ids: set[int],
) -> None:
    if any(item.sprint_id != retry.sprint_id for item in retry.task_completions):
        raise ExecutionScopeError("Retry Task evidence has the wrong source Sprint.")
    if any(item.task_id not in task_ids for item in retry.task_completions):
        raise ExecutionScopeError("Retry Task evidence is outside the contract.")
    if len({item.task_id for item in retry.task_completions}) != len(
        retry.task_completions
    ):
        raise ExecutionScopeError("Retry Task evidence has duplicate subjects.")
    if any(item.sprint_id != retry.sprint_id for item in retry.story_completions):
        raise ExecutionScopeError("Retry Story closure has the wrong source Sprint.")
    if any(item.story_id not in story_ids for item in retry.story_completions):
        raise ExecutionScopeError("Retry Story closure is outside the contract.")
    if len({item.story_id for item in retry.story_completions}) != len(
        retry.story_completions
    ):
        raise ExecutionScopeError("Retry Story closure has duplicate subjects.")
    if any(item.sprint_id != retry.sprint_id for item in retry.sprint_reviews):
        raise ExecutionScopeError("Retry review has the wrong source Sprint.")
    if any(item.sprint_id != retry.sprint_id for item in retry.sprint_closures):
        raise ExecutionScopeError("Retry closure has the wrong source Sprint.")
    if any(item.sprint_id != retry.sprint_id for item in retry.post_sprint_triage):
        raise ExecutionScopeError("Retry triage has the wrong source Sprint.")


def _current_terminal_sprint_id(snapshot: WorkflowFactSnapshot) -> int | None:
    approved_specs = tuple(
        item for item in snapshot.spec_versions if item.status == "approved"
    )
    if not approved_specs:
        return None
    if len(approved_specs) != 1:
        raise ExecutionScopeError("Current Specification lineage is ambiguous.")
    spec = approved_specs[0]
    artifacts = tuple(
        item
        for item in snapshot.planning_artifacts
        if item.artifact_type == "sprint_plan"
        and item.spec_version_id == spec.spec_version_id
        and item.spec_hash == spec.spec_hash
    )
    if not artifacts:
        return None
    try:
        stream = current_sprint_stream_artifacts(
            snapshot,
            artifacts,
            spec_identity=(spec.spec_version_id, spec.spec_hash),
        )
        nodes = sprint_stream_nodes(stream)
        accepted_id = select_current_accepted_artifact(
            nodes,
            chain_key=nodes[0].chain_key,
        ).artifact_id
    except PlanningLineageError as exc:
        raise ExecutionScopeError("Current Sprint lineage is ambiguous.") from exc
    plan = next(item for item in stream if item.artifact_id == accepted_id)
    if not plan_has_matching_sprint_start(snapshot, plan):
        return None
    return plan.activated_sprint_id


def _require_current_active_attempt(
    snapshot: WorkflowFactSnapshot,
    scope: ExecutionScope,
) -> None:
    """Validate the optional single review allowed before an explicit close."""
    if scope.status != "active":
        raise ExecutionScopeError("Execution attempt is not active.")
    if scope.completed_at is not None:
        raise ExecutionScopeError("Active execution has a completed timestamp.")
    if scope.sprint_closures or scope.post_sprint_triage:
        raise ExecutionScopeError("Active execution has terminal lifecycle evidence.")
    if len(scope.sprint_reviews) > 1:
        raise ExecutionScopeError("Active execution has duplicate review facts.")
    if not scope.sprint_reviews:
        return
    review = scope.sprint_reviews[0]
    expected_review = sprint_review_fingerprint(
        snapshot,
        scope.sprint_id,
        scope=scope,
    )
    if review.review_fingerprint != expected_review:
        raise ExecutionScopeError("Active execution review fingerprint changed.")


def _require_current_closed_attempt(
    snapshot: WorkflowFactSnapshot,
    scope: ExecutionScope,
) -> None:
    """Validate a closed current attempt, allowing triage to be the next action."""
    if (
        scope.status != "completed"
        or scope.started_at is None
        or scope.completed_at is None
        or scope.completed_at < scope.started_at
    ):
        raise ExecutionScopeError("Execution attempt is not terminal.")
    if len(scope.sprint_reviews) != 1 or len(scope.sprint_closures) != 1:
        raise ExecutionScopeError(
            "Terminal execution has incomplete review or closure facts."
        )
    review = scope.sprint_reviews[0]
    closure = scope.sprint_closures[0]
    expected_review = sprint_review_fingerprint(
        snapshot,
        scope.sprint_id,
        scope=scope,
    )
    expected_close = sprint_close_fingerprint(
        snapshot,
        scope.sprint_id,
        expected_review,
        scope=scope,
    )
    if (
        review.review_fingerprint != expected_review
        or closure.review_fingerprint != expected_review
        or closure.close_fingerprint != expected_close
    ):
        raise ExecutionScopeError(
            "Terminal execution review or closure fingerprint changed."
        )
    if scope.post_sprint_triage:
        _require_resolved_triage(scope.post_sprint_triage)


def _require_finalized_attempt(
    snapshot: WorkflowFactSnapshot,
    scope: ExecutionScope,
) -> None:
    """Require a closed predecessor's complete triage chain before succession."""
    _require_current_closed_attempt(snapshot, scope)
    _require_resolved_triage(scope.post_sprint_triage)


def _require_resolved_triage(rows: tuple[PostSprintTriageFact, ...]) -> None:
    if not rows:
        raise ExecutionScopeError("Terminal execution requires resolved triage.")
    by_id = {item.triage_id: item for item in rows}
    if len(by_id) != len(rows):
        raise ExecutionScopeError("Terminal execution triage is ambiguous.")
    children: dict[int, int] = {}
    roots: list[int] = []
    for item in rows:
        if triage_payload_fingerprint(item.impact, item.canonical_payload) != (
            item.payload_fingerprint
        ):
            raise ExecutionScopeError("Terminal execution triage fingerprint changed.")
        parent = item.supersedes_triage_id
        if parent is None:
            roots.append(item.triage_id)
        elif parent not in by_id or parent in children:
            raise ExecutionScopeError("Terminal execution triage is ambiguous.")
        else:
            children[parent] = item.triage_id
    if len(roots) != 1:
        raise ExecutionScopeError("Terminal execution triage is ambiguous.")
    current = roots[0]
    visited: set[int] = set()
    while current in children:
        if current in visited:
            raise ExecutionScopeError("Terminal execution triage has a cycle.")
        visited.add(current)
        current = children[current]
    if len(visited | {current}) != len(rows):
        raise ExecutionScopeError("Terminal execution triage is ambiguous.")


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
