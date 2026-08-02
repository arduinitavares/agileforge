"""Pure execution child-graph ordering, completion, and triage rules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypedDict, Unpack

import pytest

from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.contracts import JsonObject, NodeCategory, NodeDecision
from workflow.definitions.execution import (
    execution_graph,
    sprint_review_fingerprint,
    story_completion_fingerprint,
    task_evidence_fingerprint,
    triage_payload_fingerprint,
)
from workflow.facts import (
    PostSprintTriageFact,
    ProjectFact,
    SprintClosureFact,
    SprintFact,
    SprintReviewFact,
    StoryCompletionFact,
    StoryDependencyFact,
    StoryFact,
    TaskCompletionFact,
    TaskFact,
    WorkflowFactSnapshot,
)

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
PROJECT_ID = 12
SPRINT_ID = 21


def _story(
    story_id: int,
    *,
    status: str = "To Do",
    sprint_ids: tuple[int, ...] = (SPRINT_ID,),
) -> StoryFact:
    return StoryFact(
        story_id=story_id,
        status=status,
        sprint_ids=sprint_ids,
        sprint_candidate=False,
        readiness_blockers=(),
    )


def _task(
    task_id: int,
    story_id: int,
    *,
    status: str = "To Do",
    artifact_targets: tuple[str, ...] = (),
) -> TaskFact:
    return TaskFact(
        task_id=task_id,
        sprint_id=SPRINT_ID,
        story_id=story_id,
        description=f"Task {task_id}",
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="implementation",
                artifact_targets=list(artifact_targets),
                checklist_items=["Tests pass"],
            )
        ),
        status=status,
        dependencies_satisfied=True,
    )


def _task_completion(task: TaskFact) -> TaskCompletionFact:
    checklist: JsonObject = {"Tests pass": "passed"}
    return TaskCompletionFact(
        completion_id=1_000 + task.task_id,
        task_id=task.task_id,
        sprint_id=task.sprint_id,
        outcome_summary="Implemented and verified.",
        artifact_refs=(),
        acceptance_result="fully_met",
        checklist_result=checklist,
        evidence_fingerprint=task_evidence_fingerprint(
            task,
            outcome_summary="Implemented and verified.",
            artifact_refs=(),
            acceptance_result="fully_met",
            checklist_result=checklist,
        ),
    )


def _story_completion(
    story: StoryFact,
    tasks: tuple[TaskFact, ...],
    completions: tuple[TaskCompletionFact, ...],
) -> StoryCompletionFact:
    return StoryCompletionFact(
        completion_id=2_000 + story.story_id,
        story_id=story.story_id,
        sprint_id=SPRINT_ID,
        completion_fingerprint=story_completion_fingerprint(
            story,
            tasks,
            completions,
        ),
        resolution="Completed",
        delivered="Delivered the accepted scope.",
        evidence="Tests and artifacts are attached.",
        known_gaps="None.",
    )


class _SnapshotOverrides(TypedDict, total=False):
    sprints: tuple[SprintFact, ...] | None
    stories: tuple[StoryFact, ...]
    tasks: tuple[TaskFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    task_completions: tuple[TaskCompletionFact, ...]
    story_completions: tuple[StoryCompletionFact, ...]
    sprint_reviews: tuple[SprintReviewFact, ...]
    sprint_closures: tuple[SprintClosureFact, ...]
    triage: tuple[PostSprintTriageFact, ...]


def _snapshot(**overrides: Unpack[_SnapshotOverrides]) -> WorkflowFactSnapshot:
    sprints = overrides.get("sprints")
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Execution graph",
            origin="greenfield",
            created_at=EVALUATED_AT,
        ),
        sprints=(
            (SprintFact(sprint_id=SPRINT_ID, status="active", completed_at=None),)
            if sprints is None
            else sprints
        ),
        stories=overrides.get("stories", ()),
        story_dependencies=overrides.get("dependencies", ()),
        tasks=overrides.get("tasks", ()),
        task_completions=overrides.get("task_completions", ()),
        story_completions=overrides.get("story_completions", ()),
        sprint_reviews=overrides.get("sprint_reviews", ()),
        sprint_closures=overrides.get("sprint_closures", ()),
        post_sprint_triage=overrides.get("triage", ()),
    )


def _decision(
    snapshot: WorkflowFactSnapshot,
    node_id: str,
    instance_key: str | None = None,
) -> NodeDecision:
    position = execution_graph().evaluate(snapshot, EVALUATED_AT)
    return next(
        item
        for item in position.decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )


def _reviewed_snapshot() -> WorkflowFactSnapshot:
    task = _task(42, 7, status="Done")
    completion = _task_completion(task)
    story = _story(7, status="Done")
    story_close = _story_completion(story, (task,), (completion,))
    base = _snapshot(
        stories=(story,),
        tasks=(task,),
        task_completions=(completion,),
        story_completions=(story_close,),
    )
    review_fingerprint = sprint_review_fingerprint(base, SPRINT_ID)
    return base.model_copy(
        update={
            "sprint_reviews": (
                SprintReviewFact(
                    review_id=3_001,
                    sprint_id=SPRINT_ID,
                    review_fingerprint=review_fingerprint,
                ),
            )
        }
    )


def _completed_snapshot(
    impact: Literal["none", "backlog", "specification"] | None = None,
) -> WorkflowFactSnapshot:
    reviewed = _reviewed_snapshot()
    review = reviewed.sprint_reviews[0]
    closed = reviewed.model_copy(
        update={
            "sprints": (
                SprintFact(
                    sprint_id=SPRINT_ID,
                    status="completed",
                    completed_at=EVALUATED_AT,
                ),
            ),
            "sprint_closures": (
                SprintClosureFact(
                    closure_id=4_001,
                    sprint_id=SPRINT_ID,
                    review_fingerprint=review.review_fingerprint,
                ),
            ),
        }
    )
    if impact is None:
        return closed
    payload: JsonObject = {"summary": f"Impact is {impact}."}
    return closed.model_copy(
        update={
            "post_sprint_triage": (
                PostSprintTriageFact(
                    triage_id=5_001,
                    sprint_id=SPRINT_ID,
                    impact=impact,
                    canonical_payload=payload,
                    payload_fingerprint=triage_payload_fingerprint(impact, payload),
                    supersedes_triage_id=None,
                ),
            )
        }
    )


def test_execution_graph_has_exact_fixed_nodes() -> None:
    """Expose exactly the five approved fixed execution node IDs."""
    assert [node.node_id for node in execution_graph().root.nodes] == [
        "execution.task.complete",
        "execution.story.close",
        "execution.sprint.review",
        "execution.sprint.close",
        "execution.post_sprint_triage",
    ]


def test_no_active_or_completed_sprint_blocks_execution() -> None:
    """Require a normalized active Sprint before Task execution."""
    item = _decision(_snapshot(sprints=()), "execution.task.complete")
    assert item.category is NodeCategory.BLOCKED
    assert item.reason_code == "ACTIVE_SPRINT_REQUIRED"


def test_next_task_is_derived_from_durable_dependencies() -> None:
    """Select a Task only after its durable Story prerequisites finish."""
    prerequisite = _story(1, status="Done")
    dependent = _story(2)
    task = _task(42, 2)
    snapshot = _snapshot(
        stories=(dependent, prerequisite),
        tasks=(task,),
        dependencies=(
            StoryDependencyFact(
                dependency_id=9,
                dependent_story_id=2,
                prerequisite_story_id=1,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
        ),
    )
    item = _decision(snapshot, "execution.task.complete", "task:42")
    assert item.category is NodeCategory.AVAILABLE
    assert item.fact_references[0].fact_id == "42"


def test_eligible_in_progress_task_precedes_new_todo() -> None:
    """Keep eligible in-progress work ahead of a new todo Task."""
    story = _story(7)
    snapshot = _snapshot(
        stories=(story,),
        tasks=(_task(41, 7), _task(42, 7, status="In Progress")),
    )
    position = execution_graph().evaluate(snapshot, EVALUATED_AT)
    available = [
        item
        for item in position.decisions
        if item.node_id == "execution.task.complete"
        and item.category is NodeCategory.AVAILABLE
    ]
    assert [item.instance_key for item in available] == ["task:42"]


def test_blocked_dependency_never_guesses_a_task() -> None:
    """Block a Task whose durable prerequisite Story is still open."""
    snapshot = _snapshot(
        stories=(_story(1), _story(2)),
        tasks=(_task(42, 2),),
        dependencies=(
            StoryDependencyFact(
                dependency_id=9,
                dependent_story_id=2,
                prerequisite_story_id=1,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
        ),
    )
    item = _decision(snapshot, "execution.task.complete", "task:42")
    assert item.category is NodeCategory.BLOCKED
    assert item.reason_code == "TASK_DEPENDENCY_BLOCKED"


@pytest.mark.parametrize("corruption", ["missing", "cycle"])
def test_missing_or_cyclic_dependencies_are_invalid(corruption: str) -> None:
    """Reject missing and cyclic dependency facts as invalid."""
    stories = (_story(1), _story(2))
    dependencies = (
        StoryDependencyFact(
            dependency_id=9,
            dependent_story_id=2,
            prerequisite_story_id=99 if corruption == "missing" else 1,
            status="active",
            source="manual_review",
            confidence="reviewed",
        ),
        *(
            (
                StoryDependencyFact(
                    dependency_id=10,
                    dependent_story_id=1,
                    prerequisite_story_id=2,
                    status="active",
                    source="manual_review",
                    confidence="reviewed",
                ),
            )
            if corruption == "cycle"
            else ()
        ),
    )
    item = _decision(
        _snapshot(stories=stories, tasks=(_task(42, 2),), dependencies=dependencies),
        "execution.task.complete",
    )
    assert item.category is NodeCategory.INVALID


def test_done_task_without_immutable_evidence_is_invalid() -> None:
    """Reject a Done Task that lacks normalized completion evidence."""
    item = _decision(
        _snapshot(stories=(_story(7),), tasks=(_task(42, 7, status="Done"),)),
        "execution.task.complete",
        "task:42",
    )
    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "TASK_COMPLETION_EVIDENCE_MISSING"


def test_story_close_requires_all_tasks_terminal_and_exact_fingerprint() -> None:
    """Bind Story close to terminal Tasks and their exact evidence."""
    task = _task(42, 7, status="Done")
    completion = _task_completion(task)
    item = _decision(
        _snapshot(
            stories=(_story(7),),
            tasks=(task,),
            task_completions=(completion,),
        ),
        "execution.story.close",
        "story:7",
    )
    assert item.category is NodeCategory.AVAILABLE
    assert any(ref.fact_type == "story_completion" for ref in item.fact_references)


def test_sprint_review_and_close_are_separate_factual_transitions() -> None:
    """Keep review and closure as distinct persisted transitions."""
    reviewed = _reviewed_snapshot()
    before_review = reviewed.model_copy(update={"sprint_reviews": ()})
    review = _decision(before_review, "execution.sprint.review")
    blocked_close = _decision(before_review, "execution.sprint.close")
    close = _decision(reviewed, "execution.sprint.close")
    assert review.category is NodeCategory.WAITING
    assert blocked_close.category is NodeCategory.BLOCKED
    assert close.category is NodeCategory.AVAILABLE
    assert (
        close.fact_references[-1].fingerprint
        == reviewed.sprint_reviews[0].review_fingerprint
    )


def test_sprint_review_waits_after_every_story_is_terminal() -> None:
    """Offer review only after every attached Story is terminal."""
    reviewed = _reviewed_snapshot().model_copy(update={"sprint_reviews": ()})
    item = _decision(reviewed, "execution.sprint.review")
    assert item.category is NodeCategory.WAITING
    assert item.reason_code == "SPRINT_REVIEW_REQUIRED"


def test_post_sprint_triage_is_required_for_exact_completed_sprint() -> None:
    """Require triage for the exact normalized completed Sprint."""
    item = _decision(_completed_snapshot(), "execution.post_sprint_triage")
    assert item.category is NodeCategory.AVAILABLE
    assert item.fact_references[0].fact_id == str(SPRINT_ID)


@pytest.mark.parametrize("impact", ["none", "backlog", "specification"])
def test_recorded_triage_exposes_only_optional_correction_boundary(
    impact: Literal["none", "backlog", "specification"],
) -> None:
    """Expose only correction after triage, without Task 13 expansion."""
    position = execution_graph().evaluate(_completed_snapshot(impact), EVALUATED_AT)
    item = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.post_sprint_triage"
    )
    assert item.category is NodeCategory.AVAILABLE
    assert item.reason_code == "POST_SPRINT_TRIAGE_CORRECTION_AVAILABLE"
    assert position.terminal is True


def test_row_reversal_preserves_order_and_fingerprints() -> None:
    """Keep graph ordering and fingerprints stable under row reversal."""
    stories = (_story(1), _story(2))
    tasks = (_task(10, 1), _task(20, 2))
    snapshot = _snapshot(stories=stories, tasks=tasks)
    reversed_snapshot = snapshot.model_copy(
        update={"stories": tuple(reversed(stories)), "tasks": tuple(reversed(tasks))}
    )
    first = execution_graph().evaluate(snapshot, EVALUATED_AT)
    second = execution_graph().evaluate(reversed_snapshot, EVALUATED_AT)
    assert first.fact_fingerprint == second.fact_fingerprint
    assert first.decisions == second.decisions
