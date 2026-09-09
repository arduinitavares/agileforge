"""Pure Sprint execution, review, close, and triage graph rules."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic import ValidationError

from utils.task_metadata import TaskMetadata
from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    FactReference,
    InputField,
    RecommendationKind,
)
from workflow.execution_identity import (
    execution_instance_key,
    parse_execution_instance_key,
)
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    StoryClosurePayload,
    TaskEvidencePayload,
    execution_contract,
    sprint_close_fingerprint,
    sprint_review_fingerprint,
    story_completion_eligibility_fingerprint,
    story_completion_fingerprint,
    task_evidence_fingerprint,
    triage_payload_fingerprint,
)
from workflow.execution_scope import ExecutionScopeError, current_execution_scope
from workflow.fingerprints import canonical_hash
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)
from workflow.sprint_retry_eligibility import (
    evaluate_sprint_retry_eligibility,
    retry_start_authority_is_current,
    sprint_retry_eligibility_payload,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.execution_identity import ExecutionKind
    from workflow.execution_scope import ExecutionScope
    from workflow.facts import (
        PostSprintTriageFact,
        SprintClosureFact,
        SprintFact,
        SprintReviewFact,
        StoryCompletionFact,
        StoryFact,
        TaskCompletionFact,
        TaskFact,
        WorkflowFactSnapshot,
    )

_TERMINAL_STORY_STATUSES = frozenset({"Done", "Accepted"})
_TERMINAL_TASK_STATUSES = frozenset({"Done", "Cancelled"})
_SPRINT_INTEGRITY_REASONS = frozenset(
    {"SPRINT_STORY_COMPLETION_CONFLICT", "WORKFLOW_FACT_CONFLICT"}
)


def _blocked(
    reason: str,
    message: str,
    *,
    instance_key: str | None = None,
) -> RuleEvaluation:
    return RuleEvaluation(
        RuleCategory.BLOCKED,
        reason,
        instance_key=instance_key,
        blockers=(Blocker(code=reason, message=message),),
    )


def _active_sprint(
    snapshot: WorkflowFactSnapshot,
    *,
    require_completed_history: bool = True,
) -> SprintFact | RuleEvaluation | None:
    active = tuple(item for item in snapshot.sprints if item.status == "active")
    if len(active) > 1:
        return RuleEvaluation(RuleCategory.INVALID, "MULTIPLE_ACTIVE_SPRINTS")
    if not active:
        return None
    starts = tuple(
        item for item in snapshot.sprint_starts if item.sprint_id == active[0].sprint_id
    )
    if len(starts) != 1 or any(
        not _active_sprint_lineage_is_proven(
            snapshot,
            active[0].sprint_id,
            story_id=story_id,
        )
        for story_id in starts[0].selected_story_ids
    ):
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    try:
        execution_contract(snapshot, active[0].sprint_id)
    except ExecutionIntegrityError:
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    if require_completed_history:
        history_problem = _historical_execution_problem(snapshot)
        if history_problem is not None:
            return history_problem
    return active[0]


def _active_sprint_lineage_is_proven(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
    *,
    story_id: int,
) -> bool:
    """Prove one old-lineage Story belongs to its exact active SprintStart."""
    sprint = next(
        (
            item
            for item in snapshot.sprints
            if item.sprint_id == sprint_id and item.status == "active"
        ),
        None,
    )
    starts = tuple(
        item for item in snapshot.sprint_starts if item.sprint_id == sprint_id
    )
    if sprint is None or len(starts) != 1:
        return False
    start = starts[0]
    plans = tuple(
        item
        for item in snapshot.planning_artifacts
        if item.artifact_type == "sprint_plan"
        and item.artifact_id == start.sprint_plan_artifact_id
    )
    stories = tuple(item for item in snapshot.stories if item.story_id == story_id)
    if len(plans) != 1 or len(stories) != 1:
        return False
    plan = plans[0]
    story = stories[0]
    return (
        plan.status in {"accepted", "superseded"}
        and plan.activated_sprint_id == sprint_id
        and plan.artifact_fingerprint == start.plan_fingerprint
        and plan.spec_version_id == start.spec_version_id
        and plan.spec_hash == start.spec_hash
        and story_id in start.selected_story_ids
        and story_id in plan.selected_story_ids
        and sprint_id in story.sprint_ids
        and story.accepted_spec_version_id == start.spec_version_id
        and story.accepted_spec_hash == start.spec_hash
        and any(
            task.sprint_id == sprint_id and task.story_id == story_id
            for task in snapshot.tasks
        )
    )


def _completed_sprint(
    snapshot: WorkflowFactSnapshot,
) -> SprintFact | RuleEvaluation | None:
    completed = tuple(item for item in snapshot.sprints if item.status == "completed")
    if not completed:
        return None
    if any(item.completed_at is None for item in completed):
        return RuleEvaluation(RuleCategory.INVALID, "SPRINT_COMPLETION_TIME_MISSING")
    return max(
        completed,
        key=lambda item: (item.completed_at, item.sprint_id),
    )


def _story_by_id(snapshot: WorkflowFactSnapshot) -> dict[int, StoryFact] | None:
    result = {item.story_id: item for item in snapshot.stories}
    return result if len(result) == len(snapshot.stories) else None


def _active_dependencies(
    snapshot: WorkflowFactSnapshot,
) -> tuple[dict[int, set[int]] | None, str | None]:
    stories = _story_by_id(snapshot)
    if stories is None:
        return None, "DUPLICATE_STORY_FACT"
    edges: dict[int, set[int]] = {}
    for item in snapshot.story_dependencies:
        if item.status != "active":
            continue
        if (
            item.dependent_story_id not in stories
            or item.prerequisite_story_id not in stories
        ):
            return None, "TASK_DEPENDENCY_PREREQUISITE_MISSING"
        edges.setdefault(item.dependent_story_id, set()).add(item.prerequisite_story_id)

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(story_id: int) -> bool:
        if story_id in visiting:
            return True
        if story_id in visited:
            return False
        visiting.add(story_id)
        if any(visit(item) for item in sorted(edges.get(story_id, set()))):
            return True
        visiting.remove(story_id)
        visited.add(story_id)
        return False

    if any(visit(story_id) for story_id in sorted(stories)):
        return None, "TASK_DEPENDENCY_CYCLE"
    return edges, None


def _task_reference(task: TaskFact) -> FactReference:
    return FactReference(
        fact_type="task",
        fact_id=str(task.task_id),
        fingerprint=canonical_hash(task.model_dump(mode="json")),
    )


def _completion_by_task(
    snapshot: WorkflowFactSnapshot,
) -> tuple[dict[tuple[int, int], TaskCompletionFact] | None, str | None]:
    tasks = {(item.sprint_id, item.task_id): item for item in snapshot.tasks}
    if len(tasks) != len(snapshot.tasks):
        return None, "DUPLICATE_TASK_FACT"
    completions: dict[tuple[int, int], TaskCompletionFact] = {}
    for item in snapshot.task_completions:
        key = (item.sprint_id, item.task_id)
        task = tasks.get(key)
        problem = _task_completion_problem(
            snapshot,
            item,
            task,
            duplicate=key in completions,
        )
        if problem is not None:
            return None, problem
        completions[key] = item
    return completions, None


def _task_completion_problem(
    snapshot: WorkflowFactSnapshot,
    completion: TaskCompletionFact,
    task: TaskFact | None,
    *,
    duplicate: bool,
) -> str | None:
    if task is None:
        return "TASK_COMPLETION_ORPHANED"
    if duplicate:
        return "TASK_COMPLETION_CONFLICT"
    if task.status != "Done":
        return "TASK_COMPLETION_STATUS_CONFLICT"
    try:
        expected = task_evidence_fingerprint(
            snapshot,
            task,
            evidence=TaskEvidencePayload(
                outcome_summary=completion.outcome_summary,
                artifact_refs=completion.artifact_refs,
                acceptance_result=completion.acceptance_result,
                checklist_result=completion.checklist_result,
            ),
        )
    except ExecutionIntegrityError:
        return "WORKFLOW_FACT_CONFLICT"
    return (
        None
        if expected == completion.evidence_fingerprint
        else "TASK_COMPLETION_EVIDENCE_STALE"
    )


def _task_metadata(task: TaskFact) -> TaskMetadata | None:
    try:
        return TaskMetadata.model_validate_json(task.metadata_json)
    except (ValidationError, ValueError, TypeError):
        return None


def _task_integrity(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
) -> tuple[dict[tuple[int, int], TaskCompletionFact] | None, RuleEvaluation | None]:
    completions, error = _completion_by_task(snapshot)
    if error is not None or completions is None:
        return None, RuleEvaluation(RuleCategory.INVALID, error or "TASK_FACT_CONFLICT")
    for task in sorted(snapshot.tasks, key=lambda item: (item.sprint_id, item.task_id)):
        if task.sprint_id != sprint_id:
            continue
        metadata = _task_metadata(task)
        if metadata is None:
            return None, RuleEvaluation(
                RuleCategory.INVALID,
                "TASK_METADATA_INVALID",
                instance_key=f"task:{task.task_id}",
            )
        completion = completions.get((task.sprint_id, task.task_id))
        if task.status == "Done" and completion is None:
            return None, RuleEvaluation(
                RuleCategory.INVALID,
                "TASK_COMPLETION_EVIDENCE_MISSING",
                instance_key=f"task:{task.task_id}",
            )
        if task.status != "Done" and completion is not None:
            return None, RuleEvaluation(
                RuleCategory.INVALID,
                "TASK_COMPLETION_STATUS_CONFLICT",
                instance_key=f"task:{task.task_id}",
            )
    return completions, None


def _dependency_blockers(
    task: TaskFact,
    stories: dict[int, StoryFact],
    edges: dict[int, set[int]],
) -> tuple[int, ...]:
    return tuple(
        story_id
        for story_id in sorted(edges.get(task.story_id, set()))
        if stories[story_id].status not in _TERMINAL_STORY_STATUSES
    )


def _dependency_fact_problem(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
    stories: dict[int, StoryFact],
    edges: dict[int, set[int]],
) -> RuleEvaluation | None:
    for task in sorted(snapshot.tasks, key=lambda item: (item.sprint_id, item.task_id)):
        if task.sprint_id != sprint_id:
            continue
        expected = not _dependency_blockers(task, stories, edges)
        if task.dependencies_satisfied != expected:
            return RuleEvaluation(
                RuleCategory.INVALID,
                "TASK_DEPENDENCY_FACT_CONFLICT",
                instance_key=f"task:{task.task_id}",
            )
    return None


def _task_candidate_rule(
    nonterminal: tuple[TaskFact, ...],
    stories: dict[int, StoryFact],
    edges: dict[int, set[int]],
) -> tuple[RuleEvaluation, ...]:
    in_progress = tuple(item for item in nonterminal if item.status == "In Progress")
    for task in sorted(in_progress, key=lambda item: item.task_id):
        blockers = _dependency_blockers(task, stories, edges)
        if blockers:
            return (
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "IN_PROGRESS_TASK_DEPENDENCY_BLOCKED",
                    instance_key=f"task:{task.task_id}",
                ),
            )
    eligible_pool = in_progress or tuple(
        item for item in nonterminal if item.status == "To Do"
    )
    eligible = tuple(
        item for item in eligible_pool if not _dependency_blockers(item, stories, edges)
    )
    if eligible:
        task = min(eligible, key=lambda item: item.task_id)
        metadata = _task_metadata(task)
        if metadata is None:
            return (
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "TASK_METADATA_INVALID",
                    instance_key=f"task:{task.task_id}",
                ),
            )
        if not metadata.checklist_items:
            return (
                _blocked(
                    "TASK_CHECKLIST_REQUIRED",
                    "Task completion requires executable checklist items.",
                    instance_key=f"task:{task.task_id}",
                ),
            )
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "IN_PROGRESS_TASK_REQUIRED" if in_progress else "NEXT_TASK_READY",
                instance_key=f"task:{task.task_id}",
                fact_references=(_task_reference(task),),
            ),
        )
    task = min(nonterminal, key=lambda item: item.task_id)
    blockers = _dependency_blockers(task, stories, edges)
    return (
        RuleEvaluation(
            RuleCategory.BLOCKED,
            "TASK_DEPENDENCY_BLOCKED",
            instance_key=f"task:{task.task_id}",
            blockers=(
                Blocker(
                    code="TASK_DEPENDENCY_BLOCKED",
                    message="Task prerequisites are not terminal.",
                    fact_references=tuple(
                        FactReference(
                            fact_type="story",
                            fact_id=str(item),
                            fingerprint=canonical_hash(
                                stories[item].model_dump(mode="json")
                            ),
                        )
                        for item in blockers
                    ),
                ),
            ),
        ),
    )


def _task_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    retry_result = _active_retry_task_rule(snapshot)
    if retry_result is not None:
        return retry_result
    active = _active_sprint(snapshot)
    if isinstance(active, RuleEvaluation):
        return (active,)
    if active is None:
        return _no_active_task_rule(snapshot)
    stories = _story_by_id(snapshot)
    edges, dependency_error = _active_dependencies(snapshot)
    if stories is None or edges is None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                dependency_error or "WORKFLOW_FACT_CONFLICT",
            ),
        )
    dependency_fact_problem = _dependency_fact_problem(
        snapshot,
        active.sprint_id,
        stories,
        edges,
    )
    if dependency_fact_problem is not None:
        return (dependency_fact_problem,)
    _completions, task_error = _task_integrity(snapshot, active.sprint_id)
    if task_error is not None:
        result = (task_error,)
    else:
        nonterminal = tuple(
            item
            for item in snapshot.tasks
            if item.sprint_id == active.sprint_id
            and item.status not in _TERMINAL_TASK_STATUSES
        )
        result = (
            _task_candidate_rule(nonterminal, stories, edges)
            if nonterminal
            else (RuleEvaluation(RuleCategory.SATISFIED, "ALL_TASKS_TERMINAL"),)
        )
    return result


def _no_active_task_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...]:
    """Return terminal or blocked task status when no Sprint is active."""
    if any(item.status == "completed" for item in snapshot.sprints):
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_EXECUTION_COMPLETE"),)
    return (
        _blocked(
            "ACTIVE_SPRINT_REQUIRED",
            "Task completion requires an active Sprint.",
        ),
    )


def _retry_historical_integrity_result(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...] | None:
    """Keep earlier completed Sprint integrity authoritative during retry delivery."""
    history_problem = _historical_execution_problem(snapshot)
    if history_problem is None:
        return None
    return (history_problem,)


def _active_retry_task_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...] | None:
    """Evaluate only the live retry's local progress without changing source facts."""
    retry_scope = _retry_scope_for_status(snapshot, status="active")
    if not isinstance(retry_scope, _CurrentRetryScope):
        return None if retry_scope is None else retry_scope.evaluations
    scope = retry_scope.scope
    stories = {item.story_id: item for item in scope.project_stories}
    if len(stories) != len(scope.project_stories):
        return (RuleEvaluation(RuleCategory.INVALID, "DUPLICATE_STORY_FACT"),)
    edges: dict[int, set[int]] = {}
    for dependency in scope.dependencies:
        if dependency.status != "active":
            continue
        if (
            dependency.dependent_story_id not in stories
            or dependency.prerequisite_story_id not in stories
        ):
            return (
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "TASK_DEPENDENCY_PREREQUISITE_MISSING",
                ),
            )
        edges.setdefault(dependency.dependent_story_id, set()).add(
            dependency.prerequisite_story_id
        )
    nonterminal = tuple(
        item for item in scope.tasks if item.status not in _TERMINAL_TASK_STATUSES
    )
    if not nonterminal:
        return (RuleEvaluation(RuleCategory.SATISFIED, "ALL_TASKS_TERMINAL"),)
    result = _task_candidate_rule(nonterminal, stories, edges)
    return tuple(
        _retry_bound_task_evaluation(
            item,
            retry_attempt_id=retry_scope.retry_attempt_id,
        )
        for item in result
    )


@dataclass(frozen=True)
class _CurrentRetryScope:
    """An execution scope whose retry identity has been validated as present."""

    scope: ExecutionScope
    retry_attempt_id: int


@dataclass(frozen=True)
class _InvalidRetryScope:
    """A retry scope could not be loaded without an integrity conflict."""

    evaluations: tuple[RuleEvaluation, ...]


def _retry_scope_for_status(
    snapshot: WorkflowFactSnapshot,
    *,
    status: str,
) -> _CurrentRetryScope | _InvalidRetryScope | None:
    """Load the current retry scope only when its durable status matches."""
    if not any(item.status == status for item in snapshot.sprint_retries):
        return None
    try:
        scope = current_execution_scope(snapshot)
    except ExecutionScopeError:
        return _InvalidRetryScope(
            (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
        )
    if scope is None or scope.retry_attempt_id is None or scope.status != status:
        return _InvalidRetryScope(
            (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
        )
    history_result = _retry_historical_integrity_result(snapshot)
    if history_result is not None:
        return _InvalidRetryScope(history_result)
    return _CurrentRetryScope(scope=scope, retry_attempt_id=scope.retry_attempt_id)


def _retry_bound_task_evaluation(
    item: RuleEvaluation,
    *,
    retry_attempt_id: int,
) -> RuleEvaluation:
    """Bind a generated original Task evaluation through the shared parser."""
    if item.instance_key is None:
        return item
    try:
        identity = parse_execution_instance_key(item.instance_key)
    except (TypeError, ValueError):
        return item
    if identity.kind != "task" or identity.retry_attempt_id is not None:
        return item
    return replace(
        item,
        instance_key=execution_instance_key(
            "task",
            identity.entity_id,
            retry_attempt_id,
        ),
    )


def _story_evaluation(
    story: StoryFact,
    tasks: tuple[TaskFact, ...],
    closures: list[StoryCompletionFact],
    expected: str,
) -> RuleEvaluation | None:
    instance_key = f"story:{story.story_id}"
    if story.status in _TERMINAL_STORY_STATUSES:
        if len(closures) != 1:
            evaluation = RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_COMPLETION_FACT_CONFLICT",
                instance_key=instance_key,
            )
        elif closures[0].completion_fingerprint != expected:
            evaluation = RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_COMPLETION_FINGERPRINT_STALE",
                instance_key=instance_key,
            )
        else:
            evaluation = None
    elif closures:
        evaluation = RuleEvaluation(
            RuleCategory.INVALID,
            "STORY_COMPLETION_STATUS_CONFLICT",
            instance_key=instance_key,
        )
    elif not tasks:
        evaluation = _blocked(
            "STORY_TASKS_REQUIRED",
            "Story close requires attached executable Tasks.",
            instance_key=instance_key,
        )
    elif not all(item.status in _TERMINAL_TASK_STATUSES for item in tasks):
        evaluation = _blocked(
            "STORY_TASKS_NOT_TERMINAL",
            "Every attached Task must be Done or Cancelled.",
            instance_key=instance_key,
        )
    else:
        evaluation = RuleEvaluation(
            RuleCategory.AVAILABLE,
            "STORY_READY_TO_CLOSE",
            instance_key=instance_key,
            fact_references=(
                FactReference(
                    fact_type="story_completion",
                    fact_id=str(story.story_id),
                    fingerprint=expected,
                ),
            ),
        )
    return evaluation


def _story_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    retry_result = _active_retry_story_rule(snapshot)
    if retry_result is not None:
        return retry_result
    active = _active_sprint(snapshot)
    if isinstance(active, RuleEvaluation):
        return (active,)
    if active is None:
        return _story_without_active_sprint(snapshot)
    return _active_story_rule(snapshot, active)


def _active_retry_story_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...] | None:
    """Evaluate retry-local Story closure without altering the source Story facts."""
    retry_scope = _retry_scope_for_status(snapshot, status="active")
    if not isinstance(retry_scope, _CurrentRetryScope):
        return None if retry_scope is None else retry_scope.evaluations
    scope = retry_scope.scope
    closures_by_story: dict[int, list[StoryCompletionFact]] = {}
    for closure in scope.story_completions:
        closures_by_story.setdefault(closure.story_id, []).append(closure)
    evaluations: list[RuleEvaluation] = []
    for story in scope.stories:
        tasks = tuple(item for item in scope.tasks if item.story_id == story.story_id)
        closures = closures_by_story.get(story.story_id, [])
        try:
            expected = _scoped_story_completion(
                snapshot,
                scope=scope,
                story_id=story.story_id,
                closures=closures,
            )
        except ExecutionIntegrityError:
            return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
        evaluation = _story_evaluation(story, tasks, closures, expected)
        if evaluation is not None:
            evaluations.append(
                replace(
                    evaluation,
                    instance_key=execution_instance_key(
                        "story",
                        story.story_id,
                        scope.retry_attempt_id,
                    ),
                )
            )
    return tuple(evaluations) or (
        RuleEvaluation(RuleCategory.SATISFIED, "ALL_STORIES_TERMINAL"),
    )


def _scoped_story_completion(
    snapshot: WorkflowFactSnapshot,
    *,
    scope: ExecutionScope,
    story_id: int,
    closures: list[StoryCompletionFact],
) -> str:
    """Calculate retry Story close eligibility from retry-owned Task evidence."""
    if len(closures) != 1:
        return story_completion_eligibility_fingerprint(
            snapshot,
            sprint_id=scope.sprint_id,
            story_id=story_id,
            scope=scope,
        )
    closure = closures[0]
    return story_completion_fingerprint(
        snapshot,
        sprint_id=scope.sprint_id,
        story_id=story_id,
        closure=StoryClosurePayload(
            resolution=closure.resolution,
            delivered=closure.delivered,
            evidence=closure.evidence,
            known_gaps=closure.known_gaps,
        ),
        scope=scope,
    )


def _story_without_active_sprint(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...]:
    if any(item.status == "completed" for item in snapshot.sprints):
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_EXECUTION_COMPLETE"),)
    return (
        _blocked(
            "ACTIVE_SPRINT_REQUIRED",
            "Story close requires an active Sprint.",
        ),
    )


def _expected_story_completion(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
    story_id: int,
    closures: list[StoryCompletionFact],
) -> str:
    if len(closures) != 1:
        return story_completion_eligibility_fingerprint(
            snapshot,
            sprint_id=sprint_id,
            story_id=story_id,
        )
    closure = closures[0]
    return story_completion_fingerprint(
        snapshot,
        sprint_id=sprint_id,
        story_id=story_id,
        closure=StoryClosurePayload(
            resolution=closure.resolution,
            delivered=closure.delivered,
            evidence=closure.evidence,
            known_gaps=closure.known_gaps,
        ),
    )


def _active_story_rule(
    snapshot: WorkflowFactSnapshot,
    active: SprintFact,
) -> tuple[RuleEvaluation, ...]:
    stories = _story_by_id(snapshot)
    edges, dependency_error = _active_dependencies(snapshot)
    if stories is None or edges is None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                dependency_error or "WORKFLOW_FACT_CONFLICT",
            ),
        )
    dependency_fact_problem = _dependency_fact_problem(
        snapshot,
        active.sprint_id,
        stories,
        edges,
    )
    if dependency_fact_problem is not None:
        return (dependency_fact_problem,)
    completions, task_error = _task_integrity(snapshot, active.sprint_id)
    if task_error is not None or completions is None:
        return (
            task_error or RuleEvaluation(RuleCategory.INVALID, "TASK_FACT_CONFLICT"),
        )
    attached = tuple(
        sorted(
            (item for item in snapshot.stories if active.sprint_id in item.sprint_ids),
            key=lambda item: item.story_id,
        )
    )
    closure_by_story: dict[int, list[StoryCompletionFact]] = {}
    for item in snapshot.story_completions:
        if item.sprint_id == active.sprint_id:
            closure_by_story.setdefault(item.story_id, []).append(item)
    evaluations: list[RuleEvaluation] = []
    for story in attached:
        tasks = tuple(
            item
            for item in snapshot.tasks
            if item.sprint_id == active.sprint_id and item.story_id == story.story_id
        )
        closures = closure_by_story.get(story.story_id, [])
        try:
            expected = _expected_story_completion(
                snapshot,
                active.sprint_id,
                story.story_id,
                closures,
            )
        except ExecutionIntegrityError:
            return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
        evaluation = _story_evaluation(story, tasks, closures, expected)
        if evaluation is not None:
            evaluations.append(evaluation)
    return tuple(evaluations) or (
        RuleEvaluation(RuleCategory.SATISFIED, "ALL_STORIES_TERMINAL"),
    )


def _sprint_ready(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
) -> tuple[str | None, str | None]:
    attached = tuple(item for item in snapshot.stories if sprint_id in item.sprint_ids)
    if not attached:
        return None, "SPRINT_STORIES_REQUIRED"
    if any(item.status not in _TERMINAL_STORY_STATUSES for item in attached):
        return None, "SPRINT_STORIES_NOT_TERMINAL"
    completion_problem = _terminal_completion_problem(
        snapshot,
        sprint_id,
        attached,
    )
    if completion_problem is not None:
        return None, completion_problem
    try:
        return sprint_review_fingerprint(snapshot, sprint_id), None
    except ExecutionIntegrityError:
        return None, "WORKFLOW_FACT_CONFLICT"


def _terminal_completion_problem(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
    attached: tuple[StoryFact, ...],
) -> str | None:
    _completions, task_error = _task_integrity(snapshot, sprint_id)
    if task_error is not None:
        return "WORKFLOW_FACT_CONFLICT"
    closure_by_story: dict[int, list[StoryCompletionFact]] = {}
    for item in snapshot.story_completions:
        if item.sprint_id == sprint_id:
            closure_by_story.setdefault(item.story_id, []).append(item)
    attached_ids = {item.story_id for item in attached}
    if set(closure_by_story) != attached_ids or any(
        len(closure_by_story[item.story_id]) != 1 for item in attached
    ):
        return "SPRINT_STORY_COMPLETION_CONFLICT"
    for story in attached:
        tasks = tuple(
            item
            for item in snapshot.tasks
            if item.sprint_id == sprint_id and item.story_id == story.story_id
        )
        if not tasks or any(
            item.status not in _TERMINAL_TASK_STATUSES for item in tasks
        ):
            return "WORKFLOW_FACT_CONFLICT"
        closures = closure_by_story[story.story_id]
        try:
            expected = _expected_story_completion(
                snapshot,
                sprint_id,
                story.story_id,
                closures,
            )
        except ExecutionIntegrityError:
            return "WORKFLOW_FACT_CONFLICT"
        if closures[0].completion_fingerprint != expected:
            return "WORKFLOW_FACT_CONFLICT"
    return None


def _terminal_sprint_facts(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
) -> tuple[SprintReviewFact | None, SprintClosureFact | None, str | None]:
    expected_review, error = _sprint_ready(snapshot, sprint_id)
    if error is not None or expected_review is None:
        return None, None, error or "SPRINT_TERMINAL_FACT_CONFLICT"
    reviews = tuple(
        item for item in snapshot.sprint_reviews if item.sprint_id == sprint_id
    )
    closures = tuple(
        item for item in snapshot.sprint_closures if item.sprint_id == sprint_id
    )
    if len(reviews) != 1 or len(closures) != 1:
        return None, None, "SPRINT_TERMINAL_FACT_CONFLICT"
    review = reviews[0]
    closure = closures[0]
    expected_close = sprint_close_fingerprint(
        snapshot,
        sprint_id,
        expected_review,
    )
    if (
        review.review_fingerprint != expected_review
        or closure.review_fingerprint != expected_review
        or closure.close_fingerprint != expected_close
    ):
        return None, None, "SPRINT_TERMINAL_FACT_CONFLICT"
    return review, closure, None


def _sprint_readiness_evaluation(reason: str, message: str) -> RuleEvaluation:
    if reason in _SPRINT_INTEGRITY_REASONS:
        return RuleEvaluation(RuleCategory.INVALID, reason)
    return _blocked(reason, message)


def _scope_instance_key(
    kind: ExecutionKind,
    sprint_id: int,
    retry_attempt_id: int | None,
) -> str:
    """Render one explicit original or retry action identity."""
    if retry_attempt_id is None:
        return f"{kind}:{sprint_id}"
    return execution_instance_key(kind, sprint_id, retry_attempt_id)


def _sprint_review_evaluation(
    *,
    sprint_id: int,
    retry_attempt_id: int | None,
    reviews: tuple[SprintReviewFact, ...],
    expected: str,
) -> tuple[RuleEvaluation, ...]:
    """Build the shared review decision after scope-specific readiness checks."""
    if not reviews:
        return (
            RuleEvaluation(
                RuleCategory.WAITING,
                "SPRINT_REVIEW_REQUIRED",
                instance_key=_scope_instance_key("sprint", sprint_id, retry_attempt_id),
                fact_references=(
                    FactReference(
                        fact_type="sprint_review",
                        fact_id=str(sprint_id),
                        fingerprint=expected,
                    ),
                ),
            ),
        )
    if len(reviews) != 1 or reviews[0].review_fingerprint != expected:
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_REVIEW_FACT_CONFLICT"),)
    return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_REVIEW_RECORDED"),)


def _sprint_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    retry_result = _active_retry_review_rule(snapshot)
    if retry_result is not None:
        return retry_result
    active = _active_sprint(snapshot)
    if isinstance(active, RuleEvaluation):
        return (active,)
    if active is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_REVIEW_NOT_PENDING"),)
    expected, error = _sprint_ready(snapshot, active.sprint_id)
    if error is not None or expected is None:
        return (
            _sprint_readiness_evaluation(
                error or "SPRINT_NOT_REVIEWABLE",
                "Sprint Stories are not ready for review.",
            ),
        )
    reviews = tuple(
        item for item in snapshot.sprint_reviews if item.sprint_id == active.sprint_id
    )
    return _sprint_review_evaluation(
        sprint_id=active.sprint_id,
        retry_attempt_id=None,
        reviews=reviews,
        expected=expected,
    )


def _active_retry_review_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...] | None:
    """Offer retry review only after its own terminal evidence is complete."""
    retry_scope = _retry_scope_for_status(snapshot, status="active")
    if not isinstance(retry_scope, _CurrentRetryScope):
        return None if retry_scope is None else retry_scope.evaluations
    scope = retry_scope.scope
    expected, error = _scoped_sprint_ready(snapshot, scope)
    if error is not None or expected is None:
        return (
            _sprint_readiness_evaluation(
                error or "SPRINT_NOT_REVIEWABLE",
                "Sprint Stories are not ready for review.",
            ),
        )
    return _sprint_review_evaluation(
        sprint_id=scope.sprint_id,
        retry_attempt_id=scope.retry_attempt_id,
        reviews=scope.sprint_reviews,
        expected=expected,
    )


def _scoped_sprint_ready(
    snapshot: WorkflowFactSnapshot,
    scope: ExecutionScope,
) -> tuple[str | None, str | None]:
    """Validate terminal evidence using only one effective original or retry scope."""
    if not scope.stories:
        return None, "SPRINT_STORIES_REQUIRED"
    if any(item.status not in _TERMINAL_STORY_STATUSES for item in scope.stories):
        return None, "SPRINT_STORIES_NOT_TERMINAL"
    closures_by_story: dict[int, list[StoryCompletionFact]] = {}
    for closure in scope.story_completions:
        closures_by_story.setdefault(closure.story_id, []).append(closure)
    if set(closures_by_story) != {item.story_id for item in scope.stories}:
        return None, "SPRINT_STORY_COMPLETION_CONFLICT"
    for story in scope.stories:
        error = _scoped_story_ready(
            snapshot,
            scope=scope,
            story=story,
            closures=closures_by_story[story.story_id],
        )
        if error is not None:
            return None, error
    return sprint_review_fingerprint(
        snapshot,
        scope.sprint_id,
        scope=scope,
    ), None


def _scoped_story_ready(
    snapshot: WorkflowFactSnapshot,
    *,
    scope: ExecutionScope,
    story: StoryFact,
    closures: list[StoryCompletionFact],
) -> str | None:
    """Validate one Story's terminal Tasks and immutable completion evidence."""
    tasks = tuple(item for item in scope.tasks if item.story_id == story.story_id)
    if (
        not tasks
        or any(item.status not in _TERMINAL_TASK_STATUSES for item in tasks)
        or len(closures) != 1
    ):
        return "WORKFLOW_FACT_CONFLICT"
    try:
        expected = _scoped_story_completion(
            snapshot,
            scope=scope,
            story_id=story.story_id,
            closures=closures,
        )
    except ExecutionIntegrityError:
        return "WORKFLOW_FACT_CONFLICT"
    if closures[0].completion_fingerprint != expected:
        return "WORKFLOW_FACT_CONFLICT"
    return None


def _completed_sprint_close_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...]:
    completed = _completed_sprint(snapshot)
    if isinstance(completed, RuleEvaluation):
        return (completed,)
    if completed is None:
        return (
            _blocked(
                "ACTIVE_SPRINT_REQUIRED",
                "Sprint close requires an active Sprint.",
            ),
        )
    _review, _closure, error = _terminal_sprint_facts(
        snapshot,
        completed.sprint_id,
    )
    if error is not None:
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_TERMINAL_FACT_CONFLICT"),)
    return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_CLOSED"),)


@dataclass(frozen=True)
class _SprintCloseEvaluationInput:
    """Verified facts used to build one original or retry close decision."""

    source: SprintFact
    retry_attempt_id: int | None
    reviews: tuple[SprintReviewFact, ...]
    closures: tuple[SprintClosureFact, ...]
    expected_review: str
    close_fingerprint: str
    closure_conflict: str


def _sprint_close_evaluation(
    close: _SprintCloseEvaluationInput,
) -> tuple[RuleEvaluation, ...]:
    """Build close availability after original or retry scope facts are checked."""
    if not close.reviews:
        return (
            _blocked(
                "SPRINT_REVIEW_REQUIRED",
                "Sprint close requires persisted review.",
            ),
        )
    if (
        len(close.reviews) != 1
        or close.reviews[0].review_fingerprint != close.expected_review
    ):
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_REVIEW_FACT_CONFLICT"),)
    if close.closures:
        return (RuleEvaluation(RuleCategory.INVALID, close.closure_conflict),)
    sprint_id = close.source.sprint_id
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_READY_TO_CLOSE",
            instance_key=_scope_instance_key(
                "sprint", sprint_id, close.retry_attempt_id
            ),
            fact_references=(
                FactReference(
                    fact_type="sprint",
                    fact_id=str(sprint_id),
                    fingerprint=canonical_hash(close.source.model_dump(mode="json")),
                ),
                FactReference(
                    fact_type="sprint_review",
                    fact_id=str(sprint_id),
                    fingerprint=close.expected_review,
                ),
                FactReference(
                    fact_type="sprint_close",
                    fact_id=str(sprint_id),
                    fingerprint=close.close_fingerprint,
                ),
            ),
        ),
    )


def _sprint_close_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    retry_result = _active_retry_close_rule(snapshot)
    if retry_result is not None:
        return retry_result
    active = _active_sprint(snapshot)
    if isinstance(active, RuleEvaluation):
        return (active,)
    if active is None:
        return _completed_sprint_close_rule(snapshot)
    expected, error = _sprint_ready(snapshot, active.sprint_id)
    if error is not None or expected is None:
        result = (
            _sprint_readiness_evaluation(
                error or "SPRINT_NOT_CLOSABLE",
                "Sprint Stories are not terminal.",
            ),
        )
    else:
        reviews = tuple(
            item
            for item in snapshot.sprint_reviews
            if item.sprint_id == active.sprint_id
        )
        closures = tuple(
            item
            for item in snapshot.sprint_closures
            if item.sprint_id == active.sprint_id
        )
        close_fingerprint = sprint_close_fingerprint(
            snapshot,
            active.sprint_id,
            expected,
        )
        result = _sprint_close_evaluation(
            _SprintCloseEvaluationInput(
                source=active,
                retry_attempt_id=None,
                reviews=reviews,
                closures=closures,
                expected_review=expected,
                close_fingerprint=close_fingerprint,
                closure_conflict="SPRINT_CLOSE_STATUS_CONFLICT",
            )
        )
    return result


def _active_retry_close_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...] | None:
    """Offer explicit close only for the reviewed active retry."""
    retry_scope = _retry_scope_for_status(snapshot, status="active")
    if not isinstance(retry_scope, _CurrentRetryScope):
        return None if retry_scope is None else retry_scope.evaluations
    scope = retry_scope.scope
    expected, error = _scoped_sprint_ready(snapshot, scope)
    if error is not None or expected is None:
        return (
            _sprint_readiness_evaluation(
                error or "SPRINT_NOT_CLOSABLE",
                "Sprint Stories are not terminal.",
            ),
        )
    close_fingerprint = sprint_close_fingerprint(
        snapshot,
        scope.sprint_id,
        expected,
        scope=scope,
    )
    source = next(
        item for item in snapshot.sprints if item.sprint_id == scope.sprint_id
    )
    return _sprint_close_evaluation(
        _SprintCloseEvaluationInput(
            source=source,
            retry_attempt_id=scope.retry_attempt_id,
            reviews=scope.sprint_reviews,
            closures=scope.sprint_closures,
            expected_review=expected,
            close_fingerprint=close_fingerprint,
            closure_conflict="SPRINT_REVIEW_FACT_CONFLICT",
        )
    )


def _triage_relationships(
    rows: tuple[PostSprintTriageFact, ...],
    by_id: dict[int, PostSprintTriageFact],
) -> tuple[dict[int, list[int]], list[int], str | None]:
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for item in rows:
        if (
            triage_payload_fingerprint(item.impact, item.canonical_payload)
            != item.payload_fingerprint
        ):
            return children, roots, "POST_SPRINT_TRIAGE_FINGERPRINT_STALE"
        parent = item.supersedes_triage_id
        if parent is None:
            roots.append(item.triage_id)
        elif parent not in by_id:
            return children, roots, "POST_SPRINT_TRIAGE_PARENT_MISSING"
        else:
            children.setdefault(parent, []).append(item.triage_id)
    if len(roots) != 1 or any(len(items) != 1 for items in children.values()):
        return children, roots, "POST_SPRINT_TRIAGE_FACT_CONFLICT"
    return children, roots, None


def _triage_tip(
    rows: tuple[PostSprintTriageFact, ...],
    children: dict[int, list[int]],
    root_id: int,
) -> tuple[int | None, str | None]:
    current_id = root_id
    seen: set[int] = set()
    while current_id in children:
        if current_id in seen:
            return None, "POST_SPRINT_TRIAGE_CYCLE"
        seen.add(current_id)
        current_id = children[current_id][0]
    if len(seen | {current_id}) != len(rows):
        return None, "POST_SPRINT_TRIAGE_FACT_CONFLICT"
    return current_id, None


def _current_triage(
    rows: tuple[PostSprintTriageFact, ...],
) -> tuple[PostSprintTriageFact | None, str | None]:
    if not rows:
        return None, None
    by_id = {item.triage_id: item for item in rows}
    if len(by_id) != len(rows):
        return None, "POST_SPRINT_TRIAGE_FACT_CONFLICT"
    children, roots, error = _triage_relationships(rows, by_id)
    if error is not None:
        return None, error
    current_id, error = _triage_tip(rows, children, roots[0])
    if error is not None or current_id is None:
        return None, error
    return by_id[current_id], None


def _historical_execution_problem(
    snapshot: WorkflowFactSnapshot,
) -> RuleEvaluation | None:
    completed = tuple(item for item in snapshot.sprints if item.status == "completed")
    if any(item.completed_at is None for item in completed):
        return RuleEvaluation(RuleCategory.INVALID, "SPRINT_COMPLETION_TIME_MISSING")
    for sprint in sorted(
        completed,
        key=lambda item: (item.completed_at, item.sprint_id),
    ):
        _review, _closure, terminal_error = _terminal_sprint_facts(
            snapshot,
            sprint.sprint_id,
        )
        if terminal_error is not None:
            return RuleEvaluation(
                RuleCategory.INVALID,
                "WORKFLOW_FACT_CONFLICT",
                instance_key=f"sprint:{sprint.sprint_id}",
            )
        rows = tuple(
            item
            for item in snapshot.post_sprint_triage
            if item.sprint_id == sprint.sprint_id
        )
        current, triage_error = _current_triage(rows)
        if triage_error is not None:
            return RuleEvaluation(
                RuleCategory.INVALID,
                triage_error,
                instance_key=f"sprint:{sprint.sprint_id}",
            )
        if current is None:
            return _blocked(
                "POST_SPRINT_TRIAGE_REQUIRED",
                "Earlier completed Sprint triage must be recorded first.",
                instance_key=f"sprint:{sprint.sprint_id}",
            )
    return None


def _triage_evaluation(
    *,
    sprint_id: int,
    retry_attempt_id: int | None,
    close_fingerprint: str,
    current: PostSprintTriageFact | None,
    correction: bool,
) -> tuple[RuleEvaluation, ...]:
    """Build one triage decision after the original or retry facts are proven."""
    instance_key = _scope_instance_key("sprint", sprint_id, retry_attempt_id)
    closure_reference = FactReference(
        fact_type="sprint_closure",
        fact_id=str(sprint_id),
        fingerprint=close_fingerprint,
    )
    if current is None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "POST_SPRINT_TRIAGE_REQUIRED",
                instance_key=instance_key,
                fact_references=(closure_reference,),
            ),
        )
    if not correction:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "POST_SPRINT_TRIAGE_RECORDED",
                instance_key=instance_key,
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "POST_SPRINT_TRIAGE_CORRECTION_AVAILABLE",
            instance_key=instance_key,
            fact_references=(
                closure_reference,
                FactReference(
                    fact_type="post_sprint_triage",
                    fact_id=str(current.triage_id),
                    fingerprint=current.payload_fingerprint,
                ),
            ),
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        ),
    )


def _completed_sprint_triage_rule(
    snapshot: WorkflowFactSnapshot,
    completed: SprintFact,
    *,
    correction: bool,
) -> tuple[RuleEvaluation, ...]:
    _review, closure, terminal_error = _terminal_sprint_facts(
        snapshot,
        completed.sprint_id,
    )
    if terminal_error is not None or closure is None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                "WORKFLOW_FACT_CONFLICT",
                instance_key=f"sprint:{completed.sprint_id}",
            ),
        )
    rows = tuple(
        item
        for item in snapshot.post_sprint_triage
        if item.sprint_id == completed.sprint_id
    )
    current, error = _current_triage(rows)
    if error is not None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                error,
                instance_key=f"sprint:{completed.sprint_id}",
            ),
        )
    return _triage_evaluation(
        sprint_id=completed.sprint_id,
        retry_attempt_id=None,
        close_fingerprint=closure.close_fingerprint,
        current=current,
        correction=correction,
    )


def _in_progress_retry_triage_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...] | None:
    """Hold original triage correction while a validated retry owns delivery."""
    if not any(
        item.status in {"planned", "active"} for item in snapshot.sprint_retries
    ):
        return None
    try:
        scope = current_execution_scope(snapshot)
    except ExecutionScopeError:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if (
        scope is None
        or scope.retry_attempt_id is None
        or scope.status not in {"planned", "active"}
    ):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    completed = tuple(item for item in snapshot.sprints if item.status == "completed")
    historical_recovery = _historical_triage_recovery(snapshot, completed)
    if historical_recovery is not None:
        return historical_recovery
    return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_RETRY_IN_PROGRESS"),)


def _triage_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    retry_result = _completed_retry_triage_rule(snapshot)
    if retry_result is not None:
        return retry_result
    in_progress_retry = _in_progress_retry_triage_rule(snapshot)
    if in_progress_retry is not None:
        return in_progress_retry
    active = _active_sprint(snapshot, require_completed_history=False)
    if isinstance(active, RuleEvaluation):
        result = (active,)
    else:
        completed = tuple(
            item for item in snapshot.sprints if item.status == "completed"
        )
        result = _triage_for_completed_history(snapshot, active, completed)
    return result


def _completed_retry_triage_rule(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RuleEvaluation, ...] | None:
    """Offer triage only against the current retry's own persisted closure."""
    if not any(item.status == "completed" for item in snapshot.sprint_retries):
        return None
    try:
        scope = current_execution_scope(snapshot)
    except ExecutionScopeError:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if scope is None or scope.retry_attempt_id is None or scope.status != "completed":
        return None
    return _completed_retry_triage_scope_rule(snapshot, scope)


def _completed_retry_triage_scope_rule(
    snapshot: WorkflowFactSnapshot,
    scope: ExecutionScope,
) -> tuple[RuleEvaluation, ...]:
    """Validate completed retry facts before exposing its scoped triage action."""
    completed = tuple(item for item in snapshot.sprints if item.status == "completed")
    historical_recovery = _historical_triage_recovery(snapshot, completed)
    if historical_recovery is not None:
        return historical_recovery
    expected_review, error = _scoped_sprint_ready(snapshot, scope)
    if error is not None or expected_review is None:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if len(scope.sprint_closures) != 1:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    closure = scope.sprint_closures[0]
    expected_close = sprint_close_fingerprint(
        snapshot,
        scope.sprint_id,
        expected_review,
        scope=scope,
    )
    if (
        closure.review_fingerprint != expected_review
        or closure.close_fingerprint != expected_close
    ):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    current, triage_error = _current_triage(scope.post_sprint_triage)
    if triage_error is not None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                triage_error,
                instance_key=_scope_instance_key(
                    "sprint", scope.sprint_id, scope.retry_attempt_id
                ),
            ),
        )
    return _triage_evaluation(
        sprint_id=scope.sprint_id,
        retry_attempt_id=scope.retry_attempt_id,
        close_fingerprint=expected_close,
        current=current,
        correction=True,
    )


def _historical_triage_recovery(
    snapshot: WorkflowFactSnapshot,
    completed: tuple[SprintFact, ...],
) -> tuple[RuleEvaluation, ...] | None:
    """Return only invalid or required triage history; omit optional correction."""
    completed_with_time = tuple(
        (item.completed_at, item) for item in completed if item.completed_at is not None
    )
    if len(completed_with_time) != len(completed):
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_COMPLETION_TIME_MISSING"),)
    for _completed_at, sprint in sorted(
        completed_with_time,
        key=lambda entry: (entry[0], entry[1].sprint_id),
    ):
        evaluation = _completed_sprint_triage_rule(
            snapshot,
            sprint,
            correction=False,
        )[0]
        if (
            evaluation.category is RuleCategory.INVALID
            or evaluation.reason_code == "POST_SPRINT_TRIAGE_REQUIRED"
        ):
            return (evaluation,)
    return None


def _triage_for_completed_history(
    snapshot: WorkflowFactSnapshot,
    active: SprintFact | None,
    completed: tuple[SprintFact, ...],
) -> tuple[RuleEvaluation, ...]:
    historical_recovery = _historical_triage_recovery(snapshot, completed)
    if historical_recovery is not None:
        return historical_recovery
    if not completed:
        if active is not None:
            return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_STILL_ACTIVE"),)
        return (
            _blocked(
                "COMPLETED_SPRINT_REQUIRED",
                "Triage requires a completed Sprint.",
            ),
        )
    if active is not None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_STILL_ACTIVE"),)
    latest = max(
        completed,
        key=lambda item: (item.completed_at, item.sprint_id),
    )
    return _completed_sprint_triage_rule(
        snapshot,
        latest,
        correction=True,
    )


def _retry_references(
    snapshot: WorkflowFactSnapshot, sprint_id: int
) -> tuple[FactReference, ...]:
    """Bind retry decisions to all retry-only guard facts."""
    return (
        FactReference(
            fact_type="sprint_retry_target",
            fact_id=str(sprint_id),
            fingerprint=canonical_hash(
                sprint_retry_eligibility_payload(
                    evaluate_sprint_retry_eligibility(snapshot, sprint_id=sprint_id)
                )
            ),
        ),
        *(
            FactReference(
                fact_type="sprint_plan_generation_guard",
                fact_id=str(item.attempt_id),
                fingerprint=canonical_hash(item.model_dump(mode="json")),
            )
            for item in sorted(
                snapshot.sprint_plan_generation_guards,
                key=lambda item: canonical_hash(item.model_dump(mode="json")),
            )
        ),
        *(
            FactReference(
                fact_type="provider_generation_guard",
                fact_id=str(item.attempt_id),
                fingerprint=canonical_hash(item.model_dump(mode="json")),
            )
            for item in sorted(
                snapshot.provider_generation_guards,
                key=lambda item: canonical_hash(item.model_dump(mode="json")),
            )
        ),
        *(
            FactReference(
                fact_type="incomplete_transition",
                fact_id=str(item.receipt_id),
                fingerprint=canonical_hash(item.model_dump(mode="json")),
            )
            for item in sorted(
                snapshot.incomplete_transitions,
                key=lambda item: canonical_hash(item.model_dump(mode="json")),
            )
        ),
    )


def _retry_rule(snapshot: WorkflowFactSnapshot) -> tuple[RuleEvaluation, ...]:
    """Offer retry only as an explicit optional action for its exact target."""
    if any(item.status in {"planned", "active"} for item in snapshot.sprint_retries):
        return (RuleEvaluation(RuleCategory.SATISFIED, "RETRY_ALREADY_LIVE"),)
    candidates = tuple(item for item in snapshot.sprints if item.status == "completed")
    values: list[RuleEvaluation] = []
    for sprint in candidates:
        eligibility = evaluate_sprint_retry_eligibility(
            snapshot, sprint_id=sprint.sprint_id
        )
        references = _retry_references(snapshot, sprint.sprint_id)
        if eligibility.blockers:
            values.append(
                RuleEvaluation(
                    RuleCategory.BLOCKED,
                    eligibility.blockers[0].code,
                    instance_key=f"sprint:{sprint.sprint_id}",
                    fact_references=references,
                )
            )
        else:
            values.append(
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "SPRINT_RETRY_AVAILABLE",
                    instance_key=f"sprint:{sprint.sprint_id}",
                    fact_references=references,
                    recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                )
            )
    return tuple(values) or (
        RuleEvaluation(RuleCategory.SATISFIED, "NO_COMPLETED_SPRINT_RETRY"),
    )


def _retry_start_rule(snapshot: WorkflowFactSnapshot) -> tuple[RuleEvaluation, ...]:
    """Require a human to explicitly start the one planned retry."""
    planned = tuple(
        item for item in snapshot.sprint_retries if item.status == "planned"
    )
    if len(planned) != 1:
        return (RuleEvaluation(RuleCategory.SATISFIED, "RETRY_START_NOT_PENDING"),)
    retry = planned[0]
    if not retry_start_authority_is_current(
        snapshot,
        sprint_id=retry.sprint_id,
        retry_attempt_id=retry.retry_attempt_id,
        retry_contract_fingerprint=retry.contract_fingerprint,
    ):
        return (
            RuleEvaluation(
                RuleCategory.BLOCKED,
                "SPRINT_RETRY_START_AUTHORITY_STALE",
                instance_key=execution_instance_key(
                    "sprint", retry.sprint_id, retry.retry_attempt_id
                ),
                fact_references=_retry_references(snapshot, retry.sprint_id),
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_RETRY_READY_TO_START",
            instance_key=execution_instance_key(
                "sprint", retry.sprint_id, retry.retry_attempt_id
            ),
            fact_references=_retry_references(snapshot, retry.sprint_id),
        ),
    )


EXECUTION_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="execution.sprint.retry",
        child_graph_id="execution",
        request_kind="retry_sprint",
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        required_inputs=(
            InputField(name="confirm", value_type="boolean"),
            InputField(name="rationale", value_type="string"),
            InputField(name="expected_state_fingerprint", value_type="string"),
        ),
        evaluate_rule=lambda snapshot, _at: _retry_rule(snapshot),
    ),
    NodeSpec(
        node_id="execution.sprint.retry.start",
        child_graph_id="execution",
        request_kind="start_sprint_retry",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=lambda snapshot, _at: _retry_start_rule(snapshot),
    ),
    NodeSpec(
        node_id="execution.task.complete",
        child_graph_id="execution",
        request_kind="complete_task",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="outcome_summary", value_type="string"),
            InputField(name="artifact_refs", value_type="array"),
            InputField(name="acceptance_result", value_type="string"),
            InputField(name="checklist_result", value_type="object"),
        ),
        evaluate_rule=_task_rule,
    ),
    NodeSpec(
        node_id="execution.story.close",
        child_graph_id="execution",
        request_kind="close_story",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="resolution", value_type="string"),
            InputField(name="delivered", value_type="string"),
            InputField(name="evidence", value_type="string"),
            InputField(name="known_gaps", value_type="string"),
        ),
        evaluate_rule=_story_rule,
    ),
    NodeSpec(
        node_id="execution.sprint.review",
        child_graph_id="execution",
        request_kind="review_sprint",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_sprint_review_rule,
    ),
    NodeSpec(
        node_id="execution.sprint.close",
        child_graph_id="execution",
        request_kind="close_sprint",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_sprint_close_rule,
    ),
    NodeSpec(
        node_id="execution.post_sprint_triage",
        child_graph_id="execution",
        request_kind="record_post_sprint_triage",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="impact", value_type="string"),
            InputField(name="canonical_payload", value_type="object"),
        ),
        evaluate_rule=_triage_rule,
    ),
)


def execution_graph() -> WorkflowGraph:
    """Return the standalone execution graph used by focused tests."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(child_graph_id="execution", nodes=EXECUTION_NODES),
    )


__all__ = [
    "EXECUTION_NODES",
    "execution_graph",
    "sprint_close_fingerprint",
    "sprint_review_fingerprint",
    "story_completion_fingerprint",
    "task_evidence_fingerprint",
    "triage_payload_fingerprint",
]
