"""Pure execution child-graph ordering, completion, and triage rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict, Unpack

import pytest

from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.contracts import JsonObject, NodeCategory, NodeDecision
from workflow.definitions.execution import (
    execution_graph,
    sprint_close_fingerprint,
    sprint_review_fingerprint,
    story_completion_fingerprint,
    task_evidence_fingerprint,
    triage_payload_fingerprint,
)
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    story_dependency_source_fingerprint,
)
from workflow.execution_integrity import StoryClosurePayload, TaskEvidencePayload
from workflow.facts import (
    PlanningArtifactFact,
    PostSprintTriageFact,
    ProjectFact,
    ReviewDecisionFact,
    SprintClosureFact,
    SprintFact,
    SprintReviewFact,
    SprintStartFact,
    StoryCompletionFact,
    StoryDependencyFact,
    StoryDependencyReviewEdgeFact,
    StoryDependencyReviewFact,
    StoryFact,
    TaskCompletionFact,
    TaskFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import canonical_hash

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
PROJECT_ID = 12
SPRINT_ID = 21
SPEC_VERSION_ID = 41
SPEC_HASH = "sha256:" + "a" * 64


def _sprint_plan_id(sprint_id: int) -> int:
    return sprint_id * 100 + 1


def _sprint_plan_fingerprint(sprint_id: int) -> str:
    return canonical_hash({"sprint_plan_artifact_id": _sprint_plan_id(sprint_id)})


def _story(
    story_id: int,
    *,
    status: str = "To Do",
    sprint_ids: tuple[int, ...] = (SPRINT_ID,),
) -> StoryFact:
    return StoryFact(
        story_id=story_id,
        source_story_artifact_id=100 + story_id,
        source_story_artifact_fingerprint=f"sha256:story-artifact-{story_id}",
        source_story_item_id=f"US-{story_id:06d}",
        source_story_item_fingerprint=f"sha256:story-item-{story_id}",
        accepted_spec_version_id=SPEC_VERSION_ID,
        accepted_spec_hash=SPEC_HASH,
        spec_item_ids=(f"SPEC-{story_id:03d}",),
        content_fingerprint=canonical_hash({"story_id": story_id}),
        content_accepted=True,
        story_artifact_id=100 + story_id,
        status=status,
        sprint_ids=sprint_ids,
        structurally_eligible=True,
        structural_eligibility_status="eligible",
        sprint_selection_state="selected",
        sprint_selection_state_fingerprint=f"sha256:selection-{story_id}",
        selected_scope_fingerprint="sha256:" + "b" * 64,
        dependency_safe=True,
        sprint_candidate=True,
        readiness_blockers=(),
    )


def _task(
    task_id: int,
    story_id: int,
    *,
    sprint_id: int = SPRINT_ID,
    status: str = "To Do",
    artifact_targets: tuple[str, ...] = (),
) -> TaskFact:
    return TaskFact(
        task_id=task_id,
        sprint_id=sprint_id,
        story_id=story_id,
        description=f"Task {task_id}",
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                spec_version_id=SPEC_VERSION_ID,
                spec_hash=SPEC_HASH,
                sprint_plan_stream_id=f"SPS-{sprint_id:032x}",
                sprint_plan_artifact_id=_sprint_plan_id(sprint_id),
                sprint_plan_fingerprint=_sprint_plan_fingerprint(sprint_id),
                relevant_spec_item_ids=(f"SPEC-{story_id:03d}",),
                task_kind="implementation",
                artifact_targets=artifact_targets,
                workstream_tags=(),
                checklist_items=("Tests pass",),
            )
        ),
        status=status,
        dependencies_satisfied=True,
    )


def _task_completion(
    snapshot: WorkflowFactSnapshot,
    task: TaskFact,
) -> TaskCompletionFact:
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
            snapshot,
            task,
            evidence=TaskEvidencePayload(
                outcome_summary="Implemented and verified.",
                artifact_refs=(),
                acceptance_result="fully_met",
                checklist_result=checklist,
            ),
        ),
    )


def _story_completion(
    snapshot: WorkflowFactSnapshot,
    story: StoryFact,
    *,
    sprint_id: int = SPRINT_ID,
) -> StoryCompletionFact:
    resolution = "Completed"
    delivered = "Delivered the accepted scope."
    evidence = "Tests and artifacts are attached."
    known_gaps = "None."
    return StoryCompletionFact(
        completion_id=2_000 + story.story_id,
        story_id=story.story_id,
        sprint_id=sprint_id,
        completion_fingerprint=story_completion_fingerprint(
            snapshot,
            sprint_id=sprint_id,
            story_id=story.story_id,
            closure=StoryClosurePayload(
                resolution=resolution,
                delivered=delivered,
                evidence=evidence,
                known_gaps=known_gaps,
            ),
        ),
        resolution=resolution,
        delivered=delivered,
        evidence=evidence,
        known_gaps=known_gaps,
    )


class _SnapshotOverrides(TypedDict, total=False):
    sprints: tuple[SprintFact, ...] | None
    stories: tuple[StoryFact, ...]
    tasks: tuple[TaskFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    planning_artifacts: tuple[PlanningArtifactFact, ...] | None
    review_decisions: tuple[ReviewDecisionFact, ...] | None
    dependency_reviews: tuple[StoryDependencyReviewFact, ...] | None
    sprint_starts: tuple[SprintStartFact, ...] | None
    task_completions: tuple[TaskCompletionFact, ...]
    story_completions: tuple[StoryCompletionFact, ...]
    sprint_reviews: tuple[SprintReviewFact, ...]
    sprint_closures: tuple[SprintClosureFact, ...]
    triage: tuple[PostSprintTriageFact, ...]


def _task_plan_fingerprint(
    tasks: tuple[TaskFact, ...],
    *,
    sprint_id: int,
    story_ids: tuple[int, ...],
) -> str:
    selected = set(story_ids)
    ordinals: dict[int, int] = {}
    payload: list[JsonObject] = []
    for task in sorted(tasks, key=lambda item: (item.story_id, item.task_id)):
        if task.sprint_id != sprint_id or task.story_id not in selected:
            continue
        ordinal = ordinals.get(task.story_id, 0) + 1
        ordinals[task.story_id] = ordinal
        payload.append(
            {
                "story_id": task.story_id,
                "task_ordinal": ordinal,
                "description": task.description,
                "metadata_json": task.metadata_json,
                "status": "To Do",
            }
        )
    return canonical_hash(payload)


def _execution_lineage(
    stories: tuple[StoryFact, ...],
    tasks: tuple[TaskFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
    sprint_id: int,
) -> tuple[
    tuple[PlanningArtifactFact, ...],
    tuple[ReviewDecisionFact, ...],
    tuple[StoryDependencyReviewFact, ...],
    tuple[SprintStartFact, ...],
]:
    selected_story_ids = tuple(
        sorted(item.story_id for item in stories if sprint_id in item.sprint_ids)
    )
    candidates = tuple(sorted(stories, key=lambda item: item.story_id))
    candidate_fingerprint = candidate_set_fingerprint(candidates, dependencies)
    task_fingerprint = _task_plan_fingerprint(
        tasks,
        sprint_id=sprint_id,
        story_ids=selected_story_ids,
    )
    plan_fingerprint = _sprint_plan_fingerprint(sprint_id)
    reviewed_edges = tuple(
        StoryDependencyReviewEdgeFact(
            dependent_story_id=item.dependent_story_id,
            prerequisite_story_id=item.prerequisite_story_id,
            reason=item.reason or "Reviewed dependency",
        )
        for item in sorted(
            (edge for edge in dependencies if edge.status == "active"),
            key=lambda edge: (
                edge.dependent_story_id,
                edge.prerequisite_story_id,
                edge.dependency_id,
            ),
        )
    )
    dependency_fingerprint = canonical_hash(
        [item.model_dump(mode="json") for item in reviewed_edges]
    )
    dependency_rows_fingerprint = canonical_hash(
        [
            item.model_dump(mode="json")
            for item in sorted(
                dependencies,
                key=lambda edge: (
                    edge.dependent_story_id,
                    edge.prerequisite_story_id,
                    edge.dependency_id,
                ),
            )
        ]
    )
    dependency_source = story_dependency_source_fingerprint(candidates)
    plan_id = _sprint_plan_id(sprint_id)
    decision_id = sprint_id * 100 + 2
    dependency_review_id = sprint_id * 100 + 3
    start_id = sprint_id * 100 + 4
    audit_event_id = sprint_id * 100 + 5
    planning = (
        PlanningArtifactFact(
            artifact_type="sprint_plan",
            artifact_id=plan_id,
            artifact_fingerprint=plan_fingerprint,
            source_fingerprint=candidate_fingerprint,
            spec_version_id=SPEC_VERSION_ID,
            spec_hash=SPEC_HASH,
            sprint_plan_stream_id=(f"SPS-{sprint_id:032x}"),
            selected_story_ids=selected_story_ids,
            activated_sprint_id=sprint_id,
            candidate_set_fingerprint=candidate_fingerprint,
            task_content_fingerprint=task_fingerprint,
            status="accepted",
        ),
    )
    decisions = (
        ReviewDecisionFact(
            decision_id=decision_id,
            artifact_type="sprint",
            artifact_id=plan_id,
            artifact_fingerprint=plan_fingerprint,
            decision="accepted",
            decided_at=EVALUATED_AT,
        ),
    )
    dependency_reviews = (
        StoryDependencyReviewFact(
            review_id=dependency_review_id,
            selected_story_ids=tuple(item.story_id for item in candidates),
            reviewed_edges=reviewed_edges,
            source_fingerprint=dependency_source,
            dependency_fingerprint=dependency_fingerprint,
        ),
    )
    starts = (
        SprintStartFact(
            start_id=start_id,
            sprint_id=sprint_id,
            spec_version_id=SPEC_VERSION_ID,
            spec_hash=SPEC_HASH,
            sprint_plan_artifact_id=plan_id,
            sprint_plan_artifact_decision_id=decision_id,
            story_dependency_review_id=dependency_review_id,
            plan_fingerprint=plan_fingerprint,
            candidate_set_fingerprint=candidate_fingerprint,
            selected_story_ids=selected_story_ids,
            task_content_fingerprint=task_fingerprint,
            dependency_source_fingerprint=dependency_source,
            dependency_fingerprint=dependency_fingerprint,
            dependency_rows_fingerprint=dependency_rows_fingerprint,
            decision_fingerprint="sha256:start-decision",
            audit_event_id=audit_event_id,
            audit_event_fingerprint="sha256:start-audit",
            started_by="operator@example.com",
            started_at=EVALUATED_AT,
        ),
    )
    return planning, decisions, dependency_reviews, starts


def _snapshot(**overrides: Unpack[_SnapshotOverrides]) -> WorkflowFactSnapshot:
    sprints = overrides.get("sprints")
    sprint_facts = (
        (SprintFact(sprint_id=SPRINT_ID, status="active", completed_at=None),)
        if sprints is None
        else sprints
    )
    stories = overrides.get("stories", ())
    tasks = overrides.get("tasks", ())
    dependencies = overrides.get("dependencies", ())
    lineage = (
        _execution_lineage(stories, tasks, dependencies, sprint_facts[0].sprint_id)
        if len(sprint_facts) == 1 and stories
        else ((), (), (), ())
    )
    review_decisions = overrides.get("review_decisions")
    planning_artifacts = overrides.get("planning_artifacts")
    sprint_starts = overrides.get("sprint_starts")
    dependency_reviews = overrides.get("dependency_reviews")
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Execution graph",
            created_at=EVALUATED_AT,
        ),
        review_decisions=(lineage[1] if review_decisions is None else review_decisions),
        planning_artifacts=(
            lineage[0] if planning_artifacts is None else planning_artifacts
        ),
        sprints=sprint_facts,
        sprint_starts=(lineage[3] if sprint_starts is None else sprint_starts),
        stories=stories,
        story_dependencies=dependencies,
        story_dependency_reviews=(
            lineage[2] if dependency_reviews is None else dependency_reviews
        ),
        tasks=tasks,
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


def _reviewed_sprint_snapshot(
    *,
    sprint_id: int = SPRINT_ID,
    story_id: int = 7,
    task_id: int = 42,
) -> WorkflowFactSnapshot:
    task = _task(task_id, story_id, sprint_id=sprint_id, status="Done")
    story = _story(story_id, status="Done", sprint_ids=(sprint_id,))
    base = _snapshot(
        sprints=(SprintFact(sprint_id=sprint_id, status="active", completed_at=None),),
        stories=(story,),
        tasks=(task,),
    )
    completion = _task_completion(base, task)
    completed_tasks = base.model_copy(update={"task_completions": (completion,)})
    story_close = _story_completion(
        completed_tasks,
        story,
        sprint_id=sprint_id,
    )
    base = completed_tasks.model_copy(update={"story_completions": (story_close,)})
    review_fingerprint = sprint_review_fingerprint(base, sprint_id)
    return base.model_copy(
        update={
            "sprint_reviews": (
                SprintReviewFact(
                    review_id=3_000 + sprint_id,
                    sprint_id=sprint_id,
                    review_fingerprint=review_fingerprint,
                ),
            )
        }
    )


def _reviewed_snapshot() -> WorkflowFactSnapshot:
    return _reviewed_sprint_snapshot()


def _completed_sprint_snapshot(
    *,
    sprint_id: int,
    story_id: int,
    task_id: int,
    completed_at: datetime,
    impact: Literal["none", "backlog", "specification"] | None,
) -> WorkflowFactSnapshot:
    reviewed = _reviewed_sprint_snapshot(
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    review = reviewed.sprint_reviews[0]
    closed = reviewed.model_copy(
        update={
            "sprints": (
                SprintFact(
                    sprint_id=sprint_id,
                    status="completed",
                    completed_at=completed_at,
                ),
            ),
            "sprint_closures": (
                SprintClosureFact(
                    closure_id=4_000 + sprint_id,
                    sprint_id=sprint_id,
                    review_fingerprint=review.review_fingerprint,
                    close_fingerprint=sprint_close_fingerprint(
                        reviewed,
                        sprint_id,
                        review.review_fingerprint,
                    ),
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
                    triage_id=5_000 + sprint_id,
                    sprint_id=sprint_id,
                    impact=impact,
                    canonical_payload=payload,
                    payload_fingerprint=triage_payload_fingerprint(impact, payload),
                    supersedes_triage_id=None,
                ),
            )
        }
    )


def _completed_snapshot(
    impact: Literal["none", "backlog", "specification"] | None = None,
) -> WorkflowFactSnapshot:
    return _completed_sprint_snapshot(
        sprint_id=SPRINT_ID,
        story_id=7,
        task_id=42,
        completed_at=EVALUATED_AT,
        impact=impact,
    )


def _combine_execution_history(
    *snapshots: WorkflowFactSnapshot,
) -> WorkflowFactSnapshot:
    base = snapshots[-1]
    fields = (
        "review_decisions",
        "planning_artifacts",
        "sprints",
        "sprint_starts",
        "stories",
        "story_dependencies",
        "story_dependency_reviews",
        "tasks",
        "task_completions",
        "story_completions",
        "sprint_reviews",
        "sprint_closures",
        "post_sprint_triage",
    )
    return base.model_copy(
        update={
            field: tuple(
                item for snapshot in snapshots for item in getattr(snapshot, field)
            )
            for field in fields
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


def test_unlineaged_active_sprint_is_invalid_and_exposes_no_task() -> None:
    """Reject an active Sprint that was not started from accepted plan facts."""
    story = _story(7)
    task = _task(42, story.story_id, status="In Progress")

    position = execution_graph().evaluate(
        _snapshot(stories=(story,), tasks=(task,), sprint_starts=()),
        EVALUATED_AT,
    )
    item = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.task.complete"
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert not any(
        decision.node_id == "execution.task.complete"
        and decision.category is NodeCategory.AVAILABLE
        for decision in position.decisions
    )


def test_mismatched_active_sprint_lineage_is_invalid() -> None:
    """Reject a start whose selected Story set no longer matches its plan."""
    story = _story(7)
    task = _task(42, story.story_id, status="In Progress")
    snapshot = _snapshot(stories=(story,), tasks=(task,))
    start = snapshot.sprint_starts[0].model_copy(
        update={"selected_story_ids": (story.story_id, 999)}
    )

    position = execution_graph().evaluate(
        snapshot.model_copy(update={"sprint_starts": (start,)}),
        EVALUATED_AT,
    )
    item = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.task.complete"
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_dependencies_satisfied_fact_cannot_be_fabricated() -> None:
    """Never expose a Task whose normalized dependency flag is false."""
    story = _story(7)
    task = _task(42, story.story_id, status="In Progress").model_copy(
        update={"dependencies_satisfied": False}
    )

    item = _decision(
        _snapshot(stories=(story,), tasks=(task,)),
        "execution.task.complete",
        f"task:{task.task_id}",
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "TASK_DEPENDENCY_FACT_CONFLICT"


def test_next_task_is_derived_from_durable_dependencies() -> None:
    """Select a Task only after its durable Story prerequisites finish."""
    prerequisite = _story(1, status="Done")
    dependent = _story(2)
    prerequisite_task = _task(41, 1, status="Done")
    task = _task(42, 2)
    base = _snapshot(
        stories=(dependent, prerequisite),
        tasks=(prerequisite_task, task),
        dependencies=(
            StoryDependencyFact(
                dependency_id=9,
                dependent_story_id=2,
                prerequisite_story_id=1,
                status="active",
                source="manual_review",
                confidence="reviewed",
                reason="Prerequisite must complete first.",
            ),
        ),
    )
    snapshot = base.model_copy(
        update={"task_completions": (_task_completion(base, prerequisite_task),)}
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
    prerequisite_task = _task(41, 1, status="Done")
    base = _snapshot(
        stories=(_story(1), _story(2)),
        tasks=(
            prerequisite_task,
            _task(42, 2).model_copy(update={"dependencies_satisfied": False}),
        ),
        dependencies=(
            StoryDependencyFact(
                dependency_id=9,
                dependent_story_id=2,
                prerequisite_story_id=1,
                status="active",
                source="manual_review",
                confidence="reviewed",
                reason="Prerequisite must complete first.",
            ),
        ),
    )
    snapshot = base.model_copy(
        update={"task_completions": (_task_completion(base, prerequisite_task),)}
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
            reason="Prerequisite must complete first.",
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
                    reason="Prerequisite must complete first.",
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
    base = _snapshot(stories=(_story(7),), tasks=(task,))
    completion = _task_completion(base, task)
    item = _decision(
        base.model_copy(update={"task_completions": (completion,)}),
        "execution.story.close",
        "story:7",
    )
    assert item.category is NodeCategory.AVAILABLE
    assert any(ref.fact_type == "story_completion" for ref in item.fact_references)


@pytest.mark.parametrize(
    "tamper",
    [
        "story_content",
        "task_content",
        "dependency_identity",
        "edge",
        "completion_evidence",
    ],
)
def test_completion_integrity_binds_complete_execution_contract(
    tamper: str,
) -> None:
    """Invalidate completion eligibility after execution-contract tampering."""
    prerequisite = _story(1, status="Done")
    story = _story(2)
    prerequisite_task = _task(41, prerequisite.story_id, status="Done")
    task = _task(42, story.story_id, status="Done")
    dependency = StoryDependencyFact(
        dependency_id=9,
        dependent_story_id=story.story_id,
        prerequisite_story_id=prerequisite.story_id,
        status="active",
        source="manual_review",
        confidence="reviewed",
        reason="Prerequisite must complete first.",
    )
    base = _snapshot(
        stories=(prerequisite, story),
        tasks=(prerequisite_task, task),
        dependencies=(dependency,),
    )
    prerequisite_completion = _task_completion(base, prerequisite_task)
    completion = _task_completion(base, task)
    completed = base.model_copy(
        update={"task_completions": (prerequisite_completion, completion)}
    )
    if tamper == "story_content":
        tampered = completed.model_copy(
            update={
                "stories": (
                    prerequisite,
                    story.model_copy(update={"content_fingerprint": "sha256:tampered"}),
                )
            }
        )
    elif tamper == "task_content":
        tampered = completed.model_copy(
            update={
                "tasks": (
                    prerequisite_task,
                    task.model_copy(update={"description": "Tampered Task content"}),
                )
            }
        )
    elif tamper == "dependency_identity":
        tampered = completed.model_copy(
            update={
                "story_dependencies": (
                    dependency.model_copy(update={"dependency_id": 999}),
                )
            }
        )
    elif tamper == "edge":
        tampered = completed.model_copy(
            update={
                "story_dependencies": (
                    dependency.model_copy(update={"reason": "Changed edge semantics."}),
                )
            }
        )
    else:
        tampered = completed.model_copy(
            update={
                "task_completions": (
                    prerequisite_completion,
                    completion.model_copy(
                        update={"outcome_summary": "Tampered completion evidence."}
                    ),
                )
            }
        )

    item = next(
        decision
        for decision in execution_graph().evaluate(tampered, EVALUATED_AT).decisions
        if decision.node_id == "execution.story.close"
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == (
        "TASK_COMPLETION_EVIDENCE_STALE"
        if tamper in {"story_content", "completion_evidence"}
        else "WORKFLOW_FACT_CONFLICT"
    )


def test_sprint_review_and_close_are_separate_factual_transitions() -> None:
    """Keep review and closure as distinct persisted transitions."""
    reviewed = _reviewed_snapshot()
    before_review = reviewed.model_copy(update={"sprint_reviews": ()})
    review = _decision(
        before_review,
        "execution.sprint.review",
        f"sprint:{SPRINT_ID}",
    )
    blocked_close = _decision(before_review, "execution.sprint.close")
    close = _decision(
        reviewed,
        "execution.sprint.close",
        f"sprint:{SPRINT_ID}",
    )
    assert review.category is NodeCategory.WAITING
    assert blocked_close.category is NodeCategory.BLOCKED
    assert close.category is NodeCategory.AVAILABLE
    assert (
        next(
            ref.fingerprint
            for ref in close.fact_references
            if ref.fact_type == "sprint_review"
        )
        == reviewed.sprint_reviews[0].review_fingerprint
    )


def test_sprint_review_waits_after_every_story_is_terminal() -> None:
    """Offer review only after every attached Story is terminal."""
    reviewed = _reviewed_snapshot().model_copy(update={"sprint_reviews": ()})
    item = _decision(
        reviewed,
        "execution.sprint.review",
        f"sprint:{SPRINT_ID}",
    )
    assert item.category is NodeCategory.WAITING
    assert item.reason_code == "SPRINT_REVIEW_REQUIRED"


@pytest.mark.parametrize("tamper", ["task_evidence", "story_evidence"])
def test_sprint_review_rejects_stale_completion_chain(tamper: str) -> None:
    """Validate Task and Story completion hashes before offering Sprint review."""
    reviewed = _reviewed_snapshot().model_copy(update={"sprint_reviews": ()})
    if tamper == "task_evidence":
        completion = reviewed.task_completions[0].model_copy(
            update={"outcome_summary": "Tampered completion evidence."}
        )
        tampered = reviewed.model_copy(update={"task_completions": (completion,)})
    else:
        closure = reviewed.story_completions[0].model_copy(
            update={"delivered": "Tampered Story delivery."}
        )
        tampered = reviewed.model_copy(update={"story_completions": (closure,)})

    item = _decision(tampered, "execution.sprint.review")

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_post_sprint_triage_is_required_for_exact_completed_sprint() -> None:
    """Require triage for the exact normalized completed Sprint."""
    item = _decision(
        _completed_snapshot(),
        "execution.post_sprint_triage",
        f"sprint:{SPRINT_ID}",
    )
    assert item.category is NodeCategory.AVAILABLE
    assert item.fact_references[0].fact_id == str(SPRINT_ID)


def test_post_close_content_tamper_blocks_triage() -> None:
    """Recompute terminal integrity before exposing post-Sprint triage."""
    closed = _completed_snapshot()
    story = closed.stories[0].model_copy(
        update={"content_fingerprint": "sha256:tampered-story"}
    )
    position = execution_graph().evaluate(
        closed.model_copy(update={"stories": (story,)}),
        EVALUATED_AT,
    )
    item = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.post_sprint_triage"
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "WORKFLOW_FACT_CONFLICT"


@pytest.mark.parametrize("tamper", ["review", "close"])
def test_post_close_stale_review_or_close_fingerprint_blocks_triage(
    tamper: str,
) -> None:
    """Reject stored terminal hashes that no longer match current facts."""
    closed = _completed_snapshot()
    review = closed.sprint_reviews[0]
    closure = closed.sprint_closures[0]
    if tamper == "review":
        stale_review = review.model_copy(update={"review_fingerprint": "sha256:stale"})
        stale_closure = closure.model_copy(
            update={"review_fingerprint": "sha256:stale"}
        )
        tampered = closed.model_copy(
            update={
                "sprint_reviews": (stale_review,),
                "sprint_closures": (stale_closure,),
            }
        )
    else:
        tampered = closed.model_copy(
            update={
                "sprint_closures": (
                    closure.model_copy(update={"close_fingerprint": "sha256:stale"}),
                )
            }
        )
    item = next(
        decision
        for decision in execution_graph().evaluate(tampered, EVALUATED_AT).decisions
        if decision.node_id == "execution.post_sprint_triage"
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_older_completed_sprint_missing_triage_is_recovered_first() -> None:
    """Do not hide an older missing triage behind a newer completed Sprint."""
    older = _completed_sprint_snapshot(
        sprint_id=20,
        story_id=6,
        task_id=41,
        completed_at=EVALUATED_AT - timedelta(days=14),
        impact=None,
    )
    newer = _completed_sprint_snapshot(
        sprint_id=21,
        story_id=7,
        task_id=42,
        completed_at=EVALUATED_AT,
        impact="none",
    )
    position = execution_graph().evaluate(
        _combine_execution_history(older, newer),
        EVALUATED_AT,
    )
    item = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == "sprint:20"
    )

    assert item.category is NodeCategory.AVAILABLE
    assert item.reason_code == "POST_SPRINT_TRIAGE_REQUIRED"


def test_older_missing_triage_blocks_later_active_execution() -> None:
    """Expose only older triage recovery before later Sprint execution."""
    older = _completed_sprint_snapshot(
        sprint_id=20,
        story_id=6,
        task_id=41,
        completed_at=EVALUATED_AT - timedelta(days=14),
        impact=None,
    )
    story = _story(7, sprint_ids=(21,))
    task = _task(42, story.story_id, sprint_id=21, status="In Progress")
    active = _snapshot(
        sprints=(SprintFact(sprint_id=21, status="active", completed_at=None),),
        stories=(story,),
        tasks=(task,),
    )
    position = execution_graph().evaluate(
        _combine_execution_history(older, active),
        EVALUATED_AT,
    )
    task_decision = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.task.complete"
    )
    triage = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == "sprint:20"
    )

    assert task_decision.category is NodeCategory.BLOCKED
    assert task_decision.reason_code == "POST_SPRINT_TRIAGE_REQUIRED"
    assert triage.category is NodeCategory.AVAILABLE
    assert not any(
        decision.node_id == "execution.task.complete"
        and decision.category is NodeCategory.AVAILABLE
        for decision in position.decisions
    )


def test_older_completed_sprint_conflicting_triage_is_invalid() -> None:
    """Surface a conflicting older triage chain before newer history."""
    older = _completed_sprint_snapshot(
        sprint_id=20,
        story_id=6,
        task_id=41,
        completed_at=EVALUATED_AT - timedelta(days=14),
        impact="none",
    )
    existing = older.post_sprint_triage[0]
    payload: JsonObject = {"summary": "Conflicting root."}
    conflict = PostSprintTriageFact(
        triage_id=99_020,
        sprint_id=20,
        impact="backlog",
        canonical_payload=payload,
        payload_fingerprint=triage_payload_fingerprint("backlog", payload),
        supersedes_triage_id=None,
    )
    older = older.model_copy(update={"post_sprint_triage": (existing, conflict)})
    newer = _completed_sprint_snapshot(
        sprint_id=21,
        story_id=7,
        task_id=42,
        completed_at=EVALUATED_AT,
        impact="none",
    )
    position = execution_graph().evaluate(
        _combine_execution_history(older, newer),
        EVALUATED_AT,
    )
    item = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == "sprint:20"
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "POST_SPRINT_TRIAGE_FACT_CONFLICT"


def test_older_completed_sprint_stale_triage_is_invalid() -> None:
    """Do not hide an older stale triage fingerprint behind newer history."""
    older = _completed_sprint_snapshot(
        sprint_id=20,
        story_id=6,
        task_id=41,
        completed_at=EVALUATED_AT - timedelta(days=14),
        impact="none",
    )
    stale = older.post_sprint_triage[0].model_copy(
        update={"payload_fingerprint": "sha256:stale"}
    )
    newer = _completed_sprint_snapshot(
        sprint_id=21,
        story_id=7,
        task_id=42,
        completed_at=EVALUATED_AT,
        impact="none",
    )
    position = execution_graph().evaluate(
        _combine_execution_history(
            older.model_copy(update={"post_sprint_triage": (stale,)}),
            newer,
        ),
        EVALUATED_AT,
    )
    item = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == "sprint:20"
    )

    assert item.category is NodeCategory.INVALID
    assert item.reason_code == "POST_SPRINT_TRIAGE_FINGERPRINT_STALE"


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
