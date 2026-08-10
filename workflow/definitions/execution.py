"""Pure Sprint execution, review, close, and triage graph rules."""

from __future__ import annotations

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
from workflow.fingerprints import canonical_hash
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

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


def _after_abandonment(
    rule: Callable[
        [WorkflowFactSnapshot, datetime],
        tuple[RuleEvaluation, ...],
    ],
) -> Callable[
    [WorkflowFactSnapshot, datetime],
    tuple[RuleEvaluation, ...],
]:
    def guarded(
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        if snapshot.project_abandonments:
            return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
        return rule(snapshot, evaluated_at)

    return guarded


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
    try:
        execution_contract(snapshot, active[0].sprint_id)
    except ExecutionIntegrityError:
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    if require_completed_history:
        history_problem = _historical_execution_problem(snapshot)
        if history_problem is not None:
            return history_problem
    return active[0]


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
    active = _active_sprint(snapshot)
    if isinstance(active, RuleEvaluation):
        return (active,)
    if active is None:
        if any(item.status == "completed" for item in snapshot.sprints):
            return (
                RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_EXECUTION_COMPLETE"),
            )
        return (
            _blocked(
                "ACTIVE_SPRINT_REQUIRED",
                "Task completion requires an active Sprint.",
            ),
        )
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
    active = _active_sprint(snapshot)
    if isinstance(active, RuleEvaluation):
        return (active,)
    if active is None:
        return _story_without_active_sprint(snapshot)
    return _active_story_rule(snapshot, active)


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


def _sprint_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
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
    if not reviews:
        return (
            RuleEvaluation(
                RuleCategory.WAITING,
                "SPRINT_REVIEW_REQUIRED",
                instance_key=f"sprint:{active.sprint_id}",
                fact_references=(
                    FactReference(
                        fact_type="sprint_review",
                        fact_id=str(active.sprint_id),
                        fingerprint=expected,
                    ),
                ),
            ),
        )
    if len(reviews) != 1 or reviews[0].review_fingerprint != expected:
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_REVIEW_FACT_CONFLICT"),)
    return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_REVIEW_RECORDED"),)


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


def _sprint_close_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
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
        if not reviews:
            result = (
                _blocked(
                    "SPRINT_REVIEW_REQUIRED",
                    "Sprint close requires persisted review.",
                ),
            )
        elif len(reviews) != 1 or reviews[0].review_fingerprint != expected:
            result = (
                RuleEvaluation(RuleCategory.INVALID, "SPRINT_REVIEW_FACT_CONFLICT"),
            )
        elif any(
            item.sprint_id == active.sprint_id for item in snapshot.sprint_closures
        ):
            result = (
                RuleEvaluation(RuleCategory.INVALID, "SPRINT_CLOSE_STATUS_CONFLICT"),
            )
        else:
            close_fingerprint = sprint_close_fingerprint(
                snapshot,
                active.sprint_id,
                expected,
            )
            result = (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "SPRINT_READY_TO_CLOSE",
                    instance_key=f"sprint:{active.sprint_id}",
                    fact_references=(
                        FactReference(
                            fact_type="sprint",
                            fact_id=str(active.sprint_id),
                            fingerprint=canonical_hash(active.model_dump(mode="json")),
                        ),
                        FactReference(
                            fact_type="sprint_review",
                            fact_id=str(active.sprint_id),
                            fingerprint=expected,
                        ),
                        FactReference(
                            fact_type="sprint_close",
                            fact_id=str(active.sprint_id),
                            fingerprint=close_fingerprint,
                        ),
                    ),
                ),
            )
    return result


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
    closure_reference = FactReference(
        fact_type="sprint_closure",
        fact_id=str(completed.sprint_id),
        fingerprint=closure.close_fingerprint,
    )
    if current is None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "POST_SPRINT_TRIAGE_REQUIRED",
                instance_key=f"sprint:{completed.sprint_id}",
                fact_references=(closure_reference,),
            ),
        )
    if not correction:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "POST_SPRINT_TRIAGE_RECORDED",
                instance_key=f"sprint:{completed.sprint_id}",
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "POST_SPRINT_TRIAGE_CORRECTION_AVAILABLE",
            instance_key=f"sprint:{completed.sprint_id}",
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


def _triage_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    active = _active_sprint(snapshot, require_completed_history=False)
    if isinstance(active, RuleEvaluation):
        result = (active,)
    else:
        completed = tuple(
            item for item in snapshot.sprints if item.status == "completed"
        )
        result = _triage_for_completed_history(snapshot, active, completed)
    return result


def _triage_for_completed_history(
    snapshot: WorkflowFactSnapshot,
    active: SprintFact | None,
    completed: tuple[SprintFact, ...],
) -> tuple[RuleEvaluation, ...]:
    completed_with_time = tuple(
        (item.completed_at, item) for item in completed if item.completed_at is not None
    )
    if len(completed_with_time) != len(completed):
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_COMPLETION_TIME_MISSING"),)
    if not completed:
        if active is not None:
            return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_STILL_ACTIVE"),)
        return (
            _blocked(
                "COMPLETED_SPRINT_REQUIRED",
                "Triage requires a completed Sprint.",
            ),
        )
    ordered = tuple(
        item
        for _completed_at, item in sorted(
            completed_with_time,
            key=lambda entry: (entry[0], entry[1].sprint_id),
        )
    )
    for sprint in ordered:
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
    if active is not None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_STILL_ACTIVE"),)
    return _completed_sprint_triage_rule(
        snapshot,
        ordered[-1],
        correction=True,
    )


EXECUTION_NODES: tuple[NodeSpec, ...] = (
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
        evaluate_rule=_after_abandonment(_task_rule),
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
        evaluate_rule=_after_abandonment(_story_rule),
    ),
    NodeSpec(
        node_id="execution.sprint.review",
        child_graph_id="execution",
        request_kind="review_sprint",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_after_abandonment(_sprint_review_rule),
    ),
    NodeSpec(
        node_id="execution.sprint.close",
        child_graph_id="execution",
        request_kind="close_sprint",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_after_abandonment(_sprint_close_rule),
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
        evaluate_rule=_after_abandonment(_triage_rule),
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
