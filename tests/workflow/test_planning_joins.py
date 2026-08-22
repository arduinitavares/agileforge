"""Planning dependency, readiness, and sprint freshness join tests."""

from __future__ import annotations

from datetime import UTC, datetime

from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.contracts import NodeCategory, NodeDecision
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    planning_graph,
    story_dependency_source_fingerprint,
)
from workflow.facts import (
    BacklogItemFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecificationCandidateFact,
    SpecVersionFact,
    SprintFact,
    StoryDependencyFact,
    StoryDependencyReviewEdgeFact,
    StoryDependencyReviewFact,
    StoryFact,
    TaskFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)
from workflow.planning_integrity import (
    active_dependency_review_edges,
    current_task_content_fingerprint,
    dependency_review_fingerprint,
)

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
SPEC_VERSION_ID = 4
SPEC_HASH = "sha256:" + "a" * 64
PLAN_FINGERPRINT = "sha256:" + "b" * 64


def _story(
    story_id: int,
    *,
    accepted: bool = True,
    points: int | None = 3,
    rank: str | None = "1",
    candidate: bool = True,
) -> StoryFact:
    return StoryFact(
        story_id=story_id,
        source_story_artifact_id=100 + story_id,
        source_story_artifact_fingerprint=f"sha256:story-{story_id}",
        source_story_item_id=f"US-{story_id:06d}",
        source_story_item_fingerprint=f"sha256:story-item-{story_id}",
        accepted_spec_version_id=SPEC_VERSION_ID,
        accepted_spec_hash=SPEC_HASH,
        spec_item_ids=(f"SPEC-{story_id:03d}",),
        content_fingerprint=f"sha256:story-{story_id}",
        content_accepted=accepted,
        story_artifact_id=100 + story_id if accepted else None,
        backlog_artifact_id=10 if accepted else None,
        backlog_artifact_fingerprint="sha256:backlog" if accepted else None,
        roadmap_artifact_id=20 if accepted else None,
        roadmap_artifact_fingerprint="sha256:roadmap" if accepted else None,
        status="to_do",
        story_points=points,
        rank=rank,
        sprint_candidate=candidate,
        readiness_blockers=(),
    )


def _dependency(
    dependency_id: int,
    dependent: int,
    prerequisite: int,
    *,
    status: str = "active",
) -> StoryDependencyFact:
    return StoryDependencyFact.model_validate(
        {
            "dependency_id": dependency_id,
            "dependent_story_id": dependent,
            "prerequisite_story_id": prerequisite,
            "status": status,
            "source": "manual_review",
            "confidence": "reviewed",
            "reason": f"Story {dependent} requires Story {prerequisite}.",
        }
    )


def _snapshot(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...] = (),
    *,
    sprint_plan: PlanningArtifactFact | None = None,
    dependency_reviews: tuple[StoryDependencyReviewFact, ...] | None = None,
    tasks: tuple[TaskFact, ...] | None = None,
) -> WorkflowFactSnapshot:
    requirements = tuple(
        BacklogItemFact(
            backlog_item_id=f"req-{story.story_id}",
            backlog_artifact_id=10,
            backlog_artifact_fingerprint="sha256:backlog",
            item_fingerprint=f"sha256:item-{story.story_id}",
            spec_item_ids=(f"SPEC-{story.story_id:03d}",),
            priority=story.story_id,
        )
        for story in stories
    )
    roadmap = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "roadmap",
            "artifact_id": 20,
            "artifact_fingerprint": "sha256:roadmap",
            "source_artifact_id": 10,
            "source_fingerprint": "sha256:backlog",
            "backlog_artifact_id": 10,
            "backlog_artifact_fingerprint": "sha256:backlog",
            "roadmap_artifact_id": 20,
            "roadmap_artifact_fingerprint": "sha256:roadmap",
            "status": "accepted",
        }
    )
    story_artifacts = tuple(
        PlanningArtifactFact.model_validate(
            {
                "artifact_type": "story",
                "artifact_id": 100 + story.story_id,
                "artifact_fingerprint": story.content_fingerprint,
                "source_artifact_id": 20,
                "source_fingerprint": "sha256:roadmap",
                "backlog_artifact_id": 10,
                "backlog_artifact_fingerprint": "sha256:backlog",
                "roadmap_artifact_id": 20,
                "roadmap_artifact_fingerprint": "sha256:roadmap",
                "backlog_item_id": f"req-{story.story_id}",
                "story_item_ids": [story.source_story_item_id],
                "status": "accepted" if story.content_accepted else "pending_review",
            }
        )
        for story in stories
    )
    planning_artifacts = (
        roadmap,
        *story_artifacts,
        *((sprint_plan,) if sprint_plan is not None else ()),
    )
    decisions = (
        ReviewDecisionFact(
            decision_id=10,
            artifact_type="backlog",
            artifact_id=10,
            artifact_fingerprint="sha256:backlog",
            decision="accepted",
            decided_at=EVALUATED_AT,
        ),
        ReviewDecisionFact(
            decision_id=21,
            artifact_type="roadmap",
            artifact_id=20,
            artifact_fingerprint="sha256:roadmap",
            decision="accepted",
            decided_at=EVALUATED_AT,
        ),
        *(
            ReviewDecisionFact(
                decision_id=1_000 + story.story_id,
                artifact_type="story",
                artifact_id=100 + story.story_id,
                artifact_fingerprint=story.content_fingerprint or "",
                decision="accepted",
                decided_at=EVALUATED_AT,
            )
            for story in stories
            if story.content_accepted
        ),
        *(
            (
                ReviewDecisionFact(
                    decision_id=2_000,
                    artifact_type="sprint",
                    artifact_id=int(sprint_plan.artifact_id),
                    artifact_fingerprint=sprint_plan.artifact_fingerprint,
                    decision="accepted",
                    decided_at=EVALUATED_AT,
                ),
            )
            if sprint_plan is not None and sprint_plan.status == "accepted"
            else ()
        ),
    )
    reviewed_edges = active_dependency_review_edges(dependencies)
    current_reviews = (
        (
            StoryDependencyReviewFact(
                review_id=3_000,
                selected_story_ids=tuple(
                    item.story_id for item in stories if item.sprint_candidate
                ),
                reviewed_edges=reviewed_edges,
                source_fingerprint=story_dependency_source_fingerprint(stories),
                dependency_fingerprint=dependency_review_fingerprint(reviewed_edges),
            ),
        )
        if dependency_reviews is None
        else dependency_reviews
    )
    current_tasks = tasks
    if current_tasks is None and sprint_plan is not None:
        current_tasks = tuple(_task(story.story_id) for story in stories)
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1,
            name="Planning joins",
            created_at=EVALUATED_AT,
        ),
        spec_versions=(
            SpecVersionFact(
                spec_version_id=SPEC_VERSION_ID,
                spec_hash=SPEC_HASH,
                status="approved",
                source_specification_decision_id=1,
                accepted_at=EVALUATED_AT,
                accepted_by="operator@example.com",
                acceptance_notes="Accepted.",
                source_specification_candidate_id=1,
                source_specification_candidate_fingerprint="sha256:candidate-1",
                source_vision_artifact_id=1,
                source_vision_fingerprint="sha256:vision",
                source_product_goal_artifact_id=1,
                source_product_goal_fingerprint="sha256:goal",
            ),
        ),
        specification_candidates=(
            SpecificationCandidateFact(
                specification_candidate_id=1,
                candidate_kind="initial",
                specification_source_id=1,
                specification_source_fingerprint="sha256:source",
                vision_artifact_id=1,
                vision_fingerprint="sha256:vision",
                product_goal_artifact_id=1,
                product_goal_fingerprint="sha256:goal",
                base_spec_version_id=None,
                base_spec_hash=None,
                canonical_envelope={},
                payload_fingerprint=SPEC_HASH,
                source_manifest_fingerprint="sha256:manifest",
                producer_input_fingerprint="sha256:producer-input",
                rendered_view_fingerprint="sha256:rendered",
                candidate_fingerprint="sha256:candidate-1",
                workflow_node_attempt_id=1,
                attempt_fingerprint="sha256:attempt",
                supersedes_specification_candidate_id=None,
                supersedes_candidate_fingerprint=None,
                recorded_by="operator@example.com",
                recorded_at=EVALUATED_AT,
            ),
        ),
        phase_artifacts=(
            PhaseArtifactFact(
                artifact_type="backlog",
                artifact_id=10,
                artifact_fingerprint="sha256:backlog",
                spec_version_id=SPEC_VERSION_ID,
                spec_hash=SPEC_HASH,
                product_goal_artifact_id=1,
                product_goal_fingerprint="sha256:goal",
                status="accepted",
            ),
        ),
        vision_artifacts=(
            VisionArtifactFact(
                vision_artifact_id=1,
                version_number=1,
                components={},
                statement="Planning Vision",
                content_fingerprint="sha256:vision",
                vision_evidence_snapshot_id=1,
                supersedes_vision_artifact_id=None,
                source_interview_turn_id=1,
                created_by="operator@example.com",
                created_at=EVALUATED_AT,
            ),
        ),
        vision_artifact_decisions=(
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=1,
                vision_artifact_id=1,
                artifact_fingerprint="sha256:vision",
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="vision-accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
        product_goal_artifacts=(
            ProductGoalArtifactFact(
                product_goal_artifact_id=1,
                vision_artifact_id=1,
                vision_fingerprint="sha256:vision",
                goal_number=1,
                revision_number=1,
                statement="Plan current delivery work.",
                content_fingerprint="sha256:goal",
                supersedes_product_goal_artifact_id=None,
                source_interview_turn_id=1,
                created_by="operator@example.com",
                created_at=EVALUATED_AT,
            ),
        ),
        product_goal_artifact_decisions=(
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=1,
                product_goal_artifact_id=1,
                artifact_fingerprint="sha256:goal",
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="goal-accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
        backlog_items=requirements,
        planning_artifacts=planning_artifacts,
        review_decisions=decisions,
        stories=stories,
        story_dependencies=dependencies,
        story_dependency_reviews=current_reviews,
        tasks=current_tasks or (),
        sprints=(
            (
                SprintFact(
                    sprint_id=sprint_plan.activated_sprint_id,
                    status="planned",
                    completed_at=None,
                ),
            )
            if sprint_plan is not None
            and sprint_plan.status == "accepted"
            and sprint_plan.activated_sprint_id is not None
            else ()
        ),
    )


def _decision(snapshot: WorkflowFactSnapshot, node_id: str) -> NodeDecision:
    return next(
        item
        for item in planning_graph().evaluate(snapshot, EVALUATED_AT).decisions
        if item.node_id == node_id
    )


def _accepted_plan(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...] = (),
) -> PlanningArtifactFact:
    tasks = tuple(_task(story.story_id) for story in stories)
    return PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": 500,
            "artifact_fingerprint": PLAN_FINGERPRINT,
            "source_fingerprint": "sha256:roadmap",
            "spec_version_id": SPEC_VERSION_ID,
            "spec_hash": SPEC_HASH,
            "sprint_plan_stream_id": "SPS-0123456789abcdef0123456789abcdef",
            "selected_story_ids": [story.story_id for story in stories],
            "activated_sprint_id": 600,
            "candidate_set_fingerprint": candidate_set_fingerprint(
                stories,
                dependencies,
            ),
            "task_content_fingerprint": current_task_content_fingerprint(
                tasks,
                sprint_id=600,
                story_ids=tuple(story.story_id for story in stories),
            ),
            "status": "accepted",
        }
    )


def _task(story_id: int) -> TaskFact:
    return TaskFact(
        task_id=700 + story_id,
        sprint_id=600,
        story_id=story_id,
        description=f"Implement Story {story_id}",
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                spec_version_id=SPEC_VERSION_ID,
                spec_hash=SPEC_HASH,
                sprint_plan_stream_id="SPS-0123456789abcdef0123456789abcdef",
                sprint_plan_artifact_id=500,
                sprint_plan_fingerprint=PLAN_FINGERPRINT,
                relevant_spec_item_ids=("SPEC-001",),
                task_kind="implementation",
                artifact_targets=(),
                workstream_tags=(),
                checklist_items=("Implementation is complete.",),
            )
        ),
        status="To Do",
        dependencies_satisfied=True,
    )


def test_join_requires_accepted_content_for_every_candidate_story() -> None:
    """Require accepted content for every Story entering the Sprint join."""
    snapshot = _snapshot((_story(1), _story(2, accepted=False)))
    decision = _decision(snapshot, "planning.sprint.plan")
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "STORY_CONTENT_NOT_ACCEPTED"


def test_join_rejects_semantic_dependency_endpoint_outside_story_set() -> None:
    """Reject dependency semantics whose endpoint leaves the Story set."""
    snapshot = _snapshot((_story(1),), (_dependency(1, 1, 99),))
    decision = _decision(snapshot, "planning.sprint.plan")
    assert decision.category is NodeCategory.INVALID
    assert decision.reason_code == "STORY_DEPENDENCY_INVALID"


def test_join_blocks_unreviewed_proposed_dependencies() -> None:
    """Block the Sprint join while proposed dependencies remain unreviewed."""
    snapshot = _snapshot(
        (_story(1), _story(2)),
        (_dependency(1, 2, 1, status="proposed"),),
    )
    decision = _decision(snapshot, "planning.sprint.plan")
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "STORY_DEPENDENCIES_UNREVIEWED"


def test_dependency_review_fingerprint_must_match_typed_current_edges() -> None:
    """Treat a review with a forged dependency fingerprint as invalid."""
    stories = (_story(1), _story(2))
    dependencies = (_dependency(1, 2, 1),)
    review = StoryDependencyReviewFact(
        review_id=7,
        selected_story_ids=(1, 2),
        reviewed_edges=(
            StoryDependencyReviewEdgeFact(
                dependent_story_id=2,
                prerequisite_story_id=1,
                reason="Story 2 requires Story 1.",
            ),
        ),
        source_fingerprint=story_dependency_source_fingerprint(stories),
        dependency_fingerprint="sha256:tampered",
    )
    decision = _decision(
        _snapshot(
            stories,
            dependencies,
            dependency_reviews=(review,),
        ),
        "planning.story_dependencies",
    )
    assert decision.category is NodeCategory.INVALID
    assert decision.reason_code == "STORY_DEPENDENCY_REVIEW_STALE"


def test_candidate_fingerprint_is_order_independent() -> None:
    """Keep candidate fingerprints independent of repository row order."""
    stories = (_story(1), _story(2))
    dependencies = (_dependency(1, 2, 1),)
    assert candidate_set_fingerprint(
        stories, dependencies
    ) == candidate_set_fingerprint(
        tuple(reversed(stories)),
        tuple(reversed(dependencies)),
    )


def test_reversed_dependency_and_task_rows_preserve_semantic_fingerprints() -> None:
    """Sort repository-derived dependency and task semantics before hashing."""
    dependencies = (_dependency(1, 2, 1), _dependency(2, 3, 2))
    forward_edges = active_dependency_review_edges(dependencies)
    reverse_edges = active_dependency_review_edges(tuple(reversed(dependencies)))
    tasks = (_task(1), _task(2))
    assert forward_edges == reverse_edges
    assert dependency_review_fingerprint(
        forward_edges
    ) == dependency_review_fingerprint(reverse_edges)
    assert current_task_content_fingerprint(
        tasks,
        sprint_id=600,
        story_ids=(1, 2),
    ) == current_task_content_fingerprint(
        tuple(reversed(tasks)),
        sprint_id=600,
        story_ids=(1, 2),
    )


def test_dependency_change_makes_accepted_plan_stale() -> None:
    """Invalidate an accepted Sprint plan after dependency changes."""
    stories = (_story(1), _story(2))
    plan = _accepted_plan(stories)
    changed_dependencies = (_dependency(1, 2, 1),)
    start = _decision(
        _snapshot(stories, changed_dependencies, sprint_plan=plan),
        "planning.sprint.start",
    )
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_STALE"


def test_readiness_change_makes_accepted_plan_stale() -> None:
    """Invalidate an accepted Sprint plan after readiness changes."""
    stories = (_story(1),)
    plan = _accepted_plan(stories)
    changed = (stories[0].model_copy(update={"rank": "2"}),)
    start = _decision(
        _snapshot(changed, sprint_plan=plan),
        "planning.sprint.start",
    )
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_STALE"


def test_sprint_plan_review_waits_on_exact_immutable_plan() -> None:
    """Wait for review of the exact immutable Sprint-plan artifact."""
    stories = (_story(1),)
    plan = _accepted_plan(stories).model_copy(update={"status": "pending_review"})
    review = _decision(
        _snapshot(stories, sprint_plan=plan),
        "planning.sprint.review",
    )
    assert review.category is NodeCategory.WAITING
    assert review.fact_references[0].fingerprint == PLAN_FINGERPRINT


def test_sprint_plan_selected_ids_must_be_current_candidates() -> None:
    """Reject Sprint plans selecting IDs outside current candidates."""
    stories = (_story(1), _story(2))
    plan = _accepted_plan(stories).model_copy(update={"selected_story_ids": (1, 99)})
    start = _decision(
        _snapshot(stories, sprint_plan=plan),
        "planning.sprint.start",
    )
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_SELECTED_STORY_INVALID"
