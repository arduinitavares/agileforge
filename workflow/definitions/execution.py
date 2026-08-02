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
    sprint_review_fingerprint,
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
        SprintFact,
        StoryCompletionFact,
        StoryFact,
        TaskCompletionFact,
        TaskFact,
        WorkflowFactSnapshot,
    )

_TERMINAL_STORY_STATUSES = frozenset({"Done", "Accepted"})
_TERMINAL_TASK_STATUSES = frozenset({"Done", "Cancelled"})


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
) -> SprintFact | RuleEvaluation | None:
    active = tuple(item for item in snapshot.sprints if item.status == "active")
    if len(active) > 1:
        return RuleEvaluation(RuleCategory.INVALID, "MULTIPLE_ACTIVE_SPRINTS")
    return active[0] if active else None


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
        edges.setdefault(item.dependent_story_id, set()).add(
            item.prerequisite_story_id
        )

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
        if task is None:
            return None, "TASK_COMPLETION_ORPHANED"
        if key in completions:
            return None, "TASK_COMPLETION_CONFLICT"
        if task.status != "Done":
            return None, "TASK_COMPLETION_STATUS_CONFLICT"
        expected = task_evidence_fingerprint(
            task,
            outcome_summary=item.outcome_summary,
            artifact_refs=item.artifact_refs,
            acceptance_result=item.acceptance_result,
            checklist_result=item.checklist_result,
        )
        if expected != item.evidence_fingerprint:
            return None, "TASK_COMPLETION_EVIDENCE_STALE"
        completions[key] = item
    return completions, None


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
        item
        for item in eligible_pool
        if not _dependency_blockers(item, stories, edges)
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
        if any(item.status == "completed" for item in snapshot.sprints):
            return (
                RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_EXECUTION_COMPLETE"),
            )
        return (
            _blocked(
                "ACTIVE_SPRINT_REQUIRED",
                "Story close requires an active Sprint.",
            ),
        )
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
        expected = story_completion_fingerprint(
            story,
            tasks,
            tuple(completions.values()),
        )
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
    closure_by_story: dict[int, list[object]] = {}
    for item in snapshot.story_completions:
        if item.sprint_id == sprint_id:
            closure_by_story.setdefault(item.story_id, []).append(item)
    if any(len(closure_by_story.get(item.story_id, [])) != 1 for item in attached):
        return None, "SPRINT_STORY_COMPLETION_CONFLICT"
    return sprint_review_fingerprint(snapshot, sprint_id), None


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
            _blocked(
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
    reviews = tuple(
        item
        for item in snapshot.sprint_reviews
        if item.sprint_id == completed.sprint_id
    )
    closures = tuple(
        item
        for item in snapshot.sprint_closures
        if item.sprint_id == completed.sprint_id
    )
    if (
        len(reviews) != 1
        or len(closures) != 1
        or closures[0].review_fingerprint != reviews[0].review_fingerprint
    ):
        return (
            RuleEvaluation(RuleCategory.INVALID, "SPRINT_TERMINAL_FACT_CONFLICT"),
        )
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
            _blocked(
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
            result = (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "SPRINT_READY_TO_CLOSE",
                    fact_references=(
                        FactReference(
                            fact_type="sprint",
                            fact_id=str(active.sprint_id),
                            fingerprint=canonical_hash(
                                active.model_dump(mode="json")
                            ),
                        ),
                        FactReference(
                            fact_type="sprint_review",
                            fact_id=str(active.sprint_id),
                            fingerprint=expected,
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


def _completed_sprint_triage_rule(
    snapshot: WorkflowFactSnapshot,
    completed: SprintFact,
) -> tuple[RuleEvaluation, ...]:
    reviews = tuple(
        item
        for item in snapshot.sprint_reviews
        if item.sprint_id == completed.sprint_id
    )
    closures = tuple(
        item
        for item in snapshot.sprint_closures
        if item.sprint_id == completed.sprint_id
    )
    if (
        len(reviews) != 1
        or len(closures) != 1
        or closures[0].review_fingerprint != reviews[0].review_fingerprint
    ):
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_TERMINAL_FACT_CONFLICT"),)
    rows = tuple(
        item
        for item in snapshot.post_sprint_triage
        if item.sprint_id == completed.sprint_id
    )
    current, error = _current_triage(rows)
    if error is not None:
        return (RuleEvaluation(RuleCategory.INVALID, error),)
    closure_reference = FactReference(
        fact_type="sprint_closure",
        fact_id=str(completed.sprint_id),
        fingerprint=closures[0].review_fingerprint,
    )
    if current is None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "POST_SPRINT_TRIAGE_REQUIRED",
                fact_references=(closure_reference,),
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "POST_SPRINT_TRIAGE_CORRECTION_AVAILABLE",
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
    active = _active_sprint(snapshot)
    if isinstance(active, RuleEvaluation):
        return (active,)
    if active is not None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_STILL_ACTIVE"),)
    completed = _completed_sprint(snapshot)
    if isinstance(completed, RuleEvaluation):
        return (completed,)
    if completed is None:
        return (
            _blocked(
                "COMPLETED_SPRINT_REQUIRED",
                "Triage requires a completed Sprint.",
            ),
        )
    return _completed_sprint_triage_rule(snapshot, completed)


EXECUTION_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="execution.task.complete",
        child_graph_id="execution",
        request_kind="complete_task",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="task_id", value_type="integer"),
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
            InputField(name="story_id", value_type="integer"),
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
        required_inputs=(
            InputField(name="sprint_id", value_type="integer"),
            InputField(name="review_fingerprint", value_type="string"),
        ),
        evaluate_rule=_after_abandonment(_sprint_review_rule),
    ),
    NodeSpec(
        node_id="execution.sprint.close",
        child_graph_id="execution",
        request_kind="close_sprint",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="sprint_id", value_type="integer"),
            InputField(name="review_fingerprint", value_type="string"),
        ),
        evaluate_rule=_after_abandonment(_sprint_close_rule),
    ),
    NodeSpec(
        node_id="execution.post_sprint_triage",
        child_graph_id="execution",
        request_kind="record_post_sprint_triage",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="sprint_id", value_type="integer"),
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
    "sprint_review_fingerprint",
    "story_completion_fingerprint",
    "task_evidence_fingerprint",
    "triage_payload_fingerprint",
]
