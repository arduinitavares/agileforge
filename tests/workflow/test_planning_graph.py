"""Pure planning child-graph matrix and repeated-node properties."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NotRequired, TypedDict, Unpack

import pytest

from workflow.contracts import NodeCategory, NodeDecision, WorkflowPosition
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    planning_graph,
    story_dependency_source_fingerprint,
)
from workflow.facts import (
    AuthorityFact,
    BacklogRequirementFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecVersionFact,
    StoryDependencyFact,
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
PROJECT_ID = 11
BACKLOG_ID = 101
BACKLOG_FINGERPRINT = "sha256:backlog"
ROADMAP_ID = 201
ROADMAP_FINGERPRINT = "sha256:roadmap"
AUTHORITY_ID = 51
AUTHORITY_FINGERPRINT = "sha256:authority"
SPEC_VERSION_ID = 41
EXPECTED_PARALLEL_STORY_COUNT = 2


class _PhaseArtifactOptions(TypedDict):
    artifact_id: int
    fingerprint: str
    status: str
    authority_id: NotRequired[int]
    authority_fingerprint: NotRequired[str]


class _StoryOptions(TypedDict, total=False):
    points: int | None
    rank: str | None
    accepted: bool
    candidate: bool


class _SnapshotOptions(TypedDict, total=False):
    backlog_status: str | None
    requirements: tuple[BacklogRequirementFact, ...]
    planning_artifacts: tuple[PlanningArtifactFact, ...]
    stories: tuple[StoryFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    dependency_reviews: tuple[StoryDependencyReviewFact, ...] | None
    tasks: tuple[TaskFact, ...]
    decisions: tuple[ReviewDecisionFact, ...]
    authority_id: int
    authority_fingerprint: str
    backlog_authority_id: int | None
    backlog_authority_fingerprint: str | None


def _phase_artifact(
    artifact_type: str,
    **options: Unpack[_PhaseArtifactOptions],
) -> PhaseArtifactFact:
    authority_id = options.get("authority_id", AUTHORITY_ID)
    authority_fingerprint = options.get(
        "authority_fingerprint",
        AUTHORITY_FINGERPRINT,
    )
    return PhaseArtifactFact.model_validate(
        {
            "artifact_type": artifact_type,
            "artifact_id": options["artifact_id"],
            "artifact_fingerprint": options["fingerprint"],
            "authority_id": authority_id,
            "authority_fingerprint": authority_fingerprint,
            "product_goal_artifact_id": 1,
            "product_goal_fingerprint": "sha256:goal",
            "status": options["status"],
        }
    )


def _decision(
    artifact_type: str,
    *,
    artifact_id: int,
    fingerprint: str,
    decision: str = "accepted",
) -> ReviewDecisionFact:
    return ReviewDecisionFact.model_validate(
        {
            "decision_id": artifact_id + 10_000,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "artifact_fingerprint": fingerprint,
            "decision": decision,
            "decided_at": EVALUATED_AT,
        }
    )


def _requirements(*ids: str) -> tuple[BacklogRequirementFact, ...]:
    return tuple(
        BacklogRequirementFact(
            requirement_id=requirement_id,
            backlog_artifact_id=BACKLOG_ID,
            backlog_artifact_fingerprint=BACKLOG_FINGERPRINT,
            requirement=f"Requirement {requirement_id}",
            rank=index,
        )
        for index, requirement_id in enumerate(ids, start=1)
    )


def _roadmap(status: str = "accepted") -> PlanningArtifactFact:
    return PlanningArtifactFact.model_validate(
        {
            "artifact_type": "roadmap",
            "artifact_id": ROADMAP_ID,
            "artifact_fingerprint": ROADMAP_FINGERPRINT,
            "source_artifact_id": BACKLOG_ID,
            "source_fingerprint": BACKLOG_FINGERPRINT,
            "authority_id": AUTHORITY_ID,
            "authority_fingerprint": AUTHORITY_FINGERPRINT,
            "backlog_artifact_id": BACKLOG_ID,
            "backlog_artifact_fingerprint": BACKLOG_FINGERPRINT,
            "roadmap_artifact_id": ROADMAP_ID,
            "roadmap_artifact_fingerprint": ROADMAP_FINGERPRINT,
            "requirement_id": None,
            "story_ids": [],
            "candidate_set_fingerprint": None,
            "supersedes_artifact_id": None,
            "status": status,
        }
    )


def _story(
    story_id: int,
    requirement_id: str,
    **options: Unpack[_StoryOptions],
) -> StoryFact:
    points = options.get("points", 3)
    rank = options.get("rank", "1.1")
    accepted = options.get("accepted", True)
    candidate = options.get("candidate", True)
    return StoryFact(
        story_id=story_id,
        requirement_id=requirement_id,
        content_fingerprint=f"sha256:story-{story_id}",
        content_accepted=accepted,
        story_artifact_id=300 + story_id if accepted else None,
        authority_id=AUTHORITY_ID if accepted else None,
        authority_fingerprint=AUTHORITY_FINGERPRINT if accepted else None,
        backlog_artifact_id=BACKLOG_ID if accepted else None,
        backlog_artifact_fingerprint=BACKLOG_FINGERPRINT if accepted else None,
        roadmap_artifact_id=ROADMAP_ID if accepted else None,
        roadmap_artifact_fingerprint=ROADMAP_FINGERPRINT if accepted else None,
        status="to_do",
        story_points=points,
        rank=rank,
        sprint_candidate=candidate,
        readiness_blockers=(),
    )


def _story_artifact(
    story_id: int,
    requirement_id: str,
    *,
    status: str = "accepted",
) -> PlanningArtifactFact:
    return PlanningArtifactFact.model_validate(
        {
            "artifact_type": "story",
            "artifact_id": 300 + story_id,
            "artifact_fingerprint": f"sha256:story-{story_id}",
            "source_artifact_id": ROADMAP_ID,
            "source_fingerprint": ROADMAP_FINGERPRINT,
            "authority_id": AUTHORITY_ID,
            "authority_fingerprint": AUTHORITY_FINGERPRINT,
            "backlog_artifact_id": BACKLOG_ID,
            "backlog_artifact_fingerprint": BACKLOG_FINGERPRINT,
            "roadmap_artifact_id": ROADMAP_ID,
            "roadmap_artifact_fingerprint": ROADMAP_FINGERPRINT,
            "requirement_id": requirement_id,
            "story_ids": [story_id],
            "candidate_set_fingerprint": None,
            "supersedes_artifact_id": None,
            "status": status,
        }
    )


def _task(story_id: int, *, description: str = "Implement planning") -> TaskFact:
    return TaskFact(
        task_id=700 + story_id,
        sprint_id=601,
        story_id=story_id,
        description=description,
        metadata_json=(
            '{"artifact_targets":[],"checklist_items":[],'
            '"relevant_invariant_ids":[],"task_kind":"implementation",'
            '"version":"task_metadata.v1","workstream_tags":[]}'
        ),
        status="To Do",
        dependencies_satisfied=True,
    )


def _snapshot(
    **options: Unpack[_SnapshotOptions],
) -> WorkflowFactSnapshot:
    backlog_status = options.get("backlog_status", "accepted")
    requirements = options.get("requirements", ())
    planning_artifacts = options.get("planning_artifacts", ())
    stories = options.get("stories", ())
    dependencies = options.get("dependencies", ())
    dependency_reviews = options.get("dependency_reviews")
    tasks = options.get("tasks", ())
    decisions = options.get("decisions", ())
    authority_id = options.get("authority_id", AUTHORITY_ID)
    authority_fingerprint = options.get(
        "authority_fingerprint",
        AUTHORITY_FINGERPRINT,
    )
    backlog_authority_id = options.get("backlog_authority_id")
    backlog_authority_fingerprint = options.get("backlog_authority_fingerprint")
    backlog = (
        (
            _phase_artifact(
                "backlog",
                artifact_id=BACKLOG_ID,
                fingerprint=BACKLOG_FINGERPRINT,
                status=backlog_status,
                authority_id=(
                    authority_id
                    if backlog_authority_id is None
                    else backlog_authority_id
                ),
                authority_fingerprint=(
                    authority_fingerprint
                    if backlog_authority_fingerprint is None
                    else backlog_authority_fingerprint
                ),
            ),
        )
        if backlog_status is not None
        else ()
    )
    backlog_decision = (
        (
            _decision(
                "backlog",
                artifact_id=BACKLOG_ID,
                fingerprint=BACKLOG_FINGERPRINT,
                decision=backlog_status,
            ),
        )
        if backlog_status in {"accepted", "rejected", "feedback"}
        else ()
    )
    reviewed_edges = active_dependency_review_edges(dependencies)
    current_dependency_reviews = (
        (
            StoryDependencyReviewFact(
                review_id=901,
                selected_story_ids=tuple(
                    item.story_id for item in stories if item.sprint_candidate
                ),
                reviewed_edges=reviewed_edges,
                source_fingerprint=story_dependency_source_fingerprint(stories),
                dependency_fingerprint=dependency_review_fingerprint(reviewed_edges),
            ),
        )
        if dependency_reviews is None and stories
        else (dependency_reviews or ())
    )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Planning graph",
            origin="greenfield",
            created_at=EVALUATED_AT,
        ),
        spec_versions=(
            SpecVersionFact(
                spec_version_id=SPEC_VERSION_ID,
                spec_hash="sha256:spec",
                status="approved",
                approved_at=EVALUATED_AT,
                source_specification_candidate_id=1,
                source_vision_artifact_id=1,
                source_vision_fingerprint="sha256:vision",
                source_product_goal_artifact_id=1,
                source_product_goal_fingerprint="sha256:goal",
                source_discovery_artifact_id=1,
                source_discovery_fingerprint="sha256:discovery",
            ),
        ),
        authorities=(
            AuthorityFact(
                authority_id=authority_id,
                spec_version_id=SPEC_VERSION_ID,
                authority_fingerprint=authority_fingerprint,
                status="accepted",
                decided_at=EVALUATED_AT,
            ),
            *(
                (
                    AuthorityFact(
                        authority_id=backlog_authority_id,
                        spec_version_id=SPEC_VERSION_ID - 1,
                        authority_fingerprint=(
                            backlog_authority_fingerprint or "sha256:old-authority"
                        ),
                        status="stale",
                        decided_at=EVALUATED_AT,
                    ),
                )
                if backlog_authority_id is not None
                and backlog_authority_id != authority_id
                else ()
            ),
        ),
        vision_artifacts=(
            VisionArtifactFact(
                vision_artifact_id=1,
                version_number=1,
                components={},
                statement="Planning Vision",
                content_fingerprint="sha256:vision",
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
        phase_artifacts=backlog,
        backlog_requirements=requirements,
        planning_artifacts=planning_artifacts,
        stories=stories,
        story_dependencies=dependencies,
        story_dependency_reviews=current_dependency_reviews,
        tasks=tasks,
        review_decisions=(
            _decision(
                "authority",
                artifact_id=authority_id,
                fingerprint=authority_fingerprint,
            ),
            *backlog_decision,
            *decisions,
        ),
    )


def _position(snapshot: WorkflowFactSnapshot) -> WorkflowPosition:
    return planning_graph().evaluate(snapshot, EVALUATED_AT)


def _node(
    snapshot: WorkflowFactSnapshot, node_id: str, instance_key: str | None = None
) -> NodeDecision:
    return next(
        item
        for item in _position(snapshot).decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )


@pytest.mark.parametrize(
    ("backlog_status", "category", "reason"),
    [
        (None, NodeCategory.BLOCKED, "ACCEPTED_CURRENT_BACKLOG_REQUIRED"),
        ("pending_review", NodeCategory.BLOCKED, "ACCEPTED_CURRENT_BACKLOG_REQUIRED"),
        ("rejected", NodeCategory.BLOCKED, "ACCEPTED_CURRENT_BACKLOG_REQUIRED"),
        ("accepted", NodeCategory.AVAILABLE, "ROADMAP_GENERATION_REQUIRED"),
    ],
)
def test_roadmap_requires_accepted_current_backlog(
    backlog_status: str | None,
    category: NodeCategory,
    reason: str,
) -> None:
    """Require accepted current Backlog facts before Roadmap generation."""
    decision = _node(
        _snapshot(backlog_status=backlog_status), "planning.roadmap.generate"
    )
    assert decision.category is category
    assert decision.reason_code == reason


def test_backlog_accepted_under_stale_authority_cannot_expose_planning() -> None:
    """Require the Backlog to bind the exact accepted current authority."""
    decision = _node(
        _snapshot(
            authority_id=AUTHORITY_ID + 1,
            authority_fingerprint="sha256:current-authority",
            backlog_authority_id=AUTHORITY_ID,
            backlog_authority_fingerprint=AUTHORITY_FINGERPRINT,
        ),
        "planning.roadmap.generate",
    )
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "ACCEPTED_CURRENT_BACKLOG_REQUIRED"


def test_planning_artifacts_bind_exact_source_artifact_identity() -> None:
    """Keep source identity as well as source content in every planning fact."""
    assert "source_artifact_id" in PlanningArtifactFact.model_fields


def test_roadmap_draft_waits_for_exact_review() -> None:
    """Expose review for the exact pending Roadmap draft."""
    snapshot = _snapshot(planning_artifacts=(_roadmap("pending_review"),))
    decision = _node(snapshot, "planning.roadmap.review")
    assert decision.category is NodeCategory.WAITING
    assert decision.reason_code == "ROADMAP_REVIEW_REQUIRED"


def test_story_nodes_are_offered_once_per_uncovered_accepted_requirement() -> None:
    """Offer one Story node for each uncovered accepted requirement."""
    requirements = _requirements("req-a", "req-b", "req-c")
    snapshot = _snapshot(
        requirements=requirements,
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-b")),
        stories=(_story(1, "req-b"),),
        decisions=(
            _decision(
                "roadmap",
                artifact_id=ROADMAP_ID,
                fingerprint=ROADMAP_FINGERPRINT,
            ),
        ),
    )
    decisions = [
        item
        for item in _position(snapshot).decisions
        if item.node_id == "planning.story.generate"
    ]
    assert [item.instance_key for item in decisions] == [
        "requirement:req-a",
        "requirement:req-c",
    ]
    assert all(item.category is NodeCategory.AVAILABLE for item in decisions)


def test_story_nodes_can_be_available_in_parallel() -> None:
    """Allow independent requirement-scoped Story nodes concurrently."""
    snapshot = _snapshot(
        requirements=_requirements("req-a", "req-b"),
        planning_artifacts=(_roadmap(),),
        decisions=(
            _decision(
                "roadmap",
                artifact_id=ROADMAP_ID,
                fingerprint=ROADMAP_FINGERPRINT,
            ),
        ),
    )
    position = _position(snapshot)
    assert (
        position.available_nodes.count("planning.story.generate")
        == EXPECTED_PARALLEL_STORY_COUNT
    )


def test_reversing_repository_rows_preserves_story_order_and_fingerprints() -> None:
    """Preserve repeated Story ordering and fingerprints under row reversal."""
    requirements = _requirements("req-c", "req-a", "req-b")
    artifacts = (_roadmap(),)
    decisions = (
        _decision(
            "roadmap",
            artifact_id=ROADMAP_ID,
            fingerprint=ROADMAP_FINGERPRINT,
        ),
    )
    forward = _position(
        _snapshot(
            requirements=requirements,
            planning_artifacts=artifacts,
            decisions=decisions,
        )
    )
    reverse = _position(
        _snapshot(
            requirements=tuple(reversed(requirements)),
            planning_artifacts=artifacts,
            decisions=decisions,
        )
    )
    forward_story = [
        (item.instance_key, item.decision_fingerprint)
        for item in forward.decisions
        if item.node_id == "planning.story.generate"
    ]
    reverse_story = [
        (item.instance_key, item.decision_fingerprint)
        for item in reverse.decisions
        if item.node_id == "planning.story.generate"
    ]
    assert forward_story == reverse_story


def test_pending_story_artifact_waits_for_requirement_scoped_review() -> None:
    """Scope pending Story review to its exact requirement instance."""
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a", status="pending_review"),
        ),
        decisions=(
            _decision(
                "roadmap",
                artifact_id=ROADMAP_ID,
                fingerprint=ROADMAP_FINGERPRINT,
            ),
        ),
    )
    review = _node(snapshot, "planning.story.review", "requirement:req-a")
    assert review.category is NodeCategory.WAITING
    assert review.reason_code == "STORY_REVIEW_REQUIRED"


@pytest.mark.parametrize(
    ("case", "node_id", "instance_key", "reason"),
    [
        (
            "roadmap_backlog",
            "planning.roadmap.review",
            None,
            "ROADMAP_REVIEW_SOURCE_STALE",
        ),
        (
            "story_roadmap",
            "planning.story.review",
            "requirement:req-a",
            "STORY_REVIEW_SOURCE_STALE",
        ),
        (
            "sprint_candidates",
            "planning.sprint.review",
            None,
            "SPRINT_PLAN_REVIEW_SOURCE_STALE",
        ),
        (
            "sprint_tasks",
            "planning.sprint.review",
            None,
            "SPRINT_PLAN_REVIEW_TASK_CONTENT_STALE",
        ),
    ],
)
def test_planning_review_freshness_matrix_fails_closed(
    case: str,
    node_id: str,
    instance_key: str | None,
    reason: str,
) -> None:
    """Reject every pending planning artifact whose source facts changed."""
    story = _story(1, "req-a")
    if case == "roadmap_backlog":
        artifacts = (
            _roadmap("pending_review").model_copy(
                update={"source_fingerprint": "sha256:obsolete-backlog"}
            ),
        )
        snapshot = _snapshot(planning_artifacts=artifacts)
    elif case == "story_roadmap":
        artifacts = (
            _roadmap(),
            _story_artifact(1, "req-a", status="pending_review").model_copy(
                update={"source_fingerprint": "sha256:obsolete-roadmap"}
            ),
        )
        snapshot = _snapshot(
            requirements=_requirements("req-a"),
            planning_artifacts=artifacts,
            stories=(story,),
            decisions=(
                _decision(
                    "roadmap",
                    artifact_id=ROADMAP_ID,
                    fingerprint=ROADMAP_FINGERPRINT,
                ),
            ),
        )
    else:
        original_fingerprint = candidate_set_fingerprint((story,), ())
        task = _task(1)
        changed_story = (
            story.model_copy(update={"story_points": 5})
            if case == "sprint_candidates"
            else story
        )
        current_task = (
            task.model_copy(update={"description": "Unreviewed task content"})
            if case == "sprint_tasks"
            else task
        )
        sprint_plan = PlanningArtifactFact.model_validate(
            {
                "artifact_type": "sprint_plan",
                "artifact_id": 501,
                "artifact_fingerprint": "sha256:plan",
                "source_fingerprint": original_fingerprint,
                "story_ids": [1],
                "sprint_id": 601,
                "candidate_set_fingerprint": original_fingerprint,
                "task_content_fingerprint": current_task_content_fingerprint(
                    (task,),
                    sprint_id=601,
                    story_ids=(1,),
                ),
                "status": "pending_review",
            }
        )
        snapshot = _snapshot(
            requirements=_requirements("req-a"),
            planning_artifacts=(
                _roadmap(),
                _story_artifact(1, "req-a"),
                sprint_plan,
            ),
            stories=(changed_story,),
            tasks=(current_task,),
            decisions=(
                _decision(
                    "roadmap",
                    artifact_id=ROADMAP_ID,
                    fingerprint=ROADMAP_FINGERPRINT,
                ),
                _decision(
                    "story",
                    artifact_id=301,
                    fingerprint="sha256:story-1",
                ),
            ),
        )

    review = _node(snapshot, node_id, instance_key)
    assert review.category is NodeCategory.INVALID
    assert review.reason_code == reason


def test_dependency_cycle_fails_join_closed_as_invalid() -> None:
    """Reject a semantic dependency cycle at the Sprint planning join."""
    stories = (_story(1, "req-a"), _story(2, "req-b"))
    artifacts = (
        _roadmap(),
        _story_artifact(1, "req-a"),
        _story_artifact(2, "req-b"),
    )
    dependencies = (
        StoryDependencyFact(
            dependency_id=1,
            dependent_story_id=1,
            prerequisite_story_id=2,
            status="active",
            source="manual_review",
            confidence="reviewed",
            reason="First requires second.",
        ),
        StoryDependencyFact(
            dependency_id=2,
            dependent_story_id=2,
            prerequisite_story_id=1,
            status="active",
            source="manual_review",
            confidence="reviewed",
            reason="Second requires first.",
        ),
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a", "req-b"),
        planning_artifacts=artifacts,
        stories=stories,
        dependencies=dependencies,
    )
    sprint = _node(snapshot, "planning.sprint.plan")
    assert sprint.category is NodeCategory.INVALID
    assert sprint.reason_code == "STORY_DEPENDENCY_CYCLE"


@pytest.mark.parametrize(
    ("points", "rank", "reason"),
    [
        (None, "1.1", "STORY_POINTS_MISSING"),
        (3, None, "STORY_RANK_MISSING"),
    ],
)
def test_missing_points_or_rank_blocks_sprint_join(
    points: int | None,
    rank: str | None,
    reason: str,
) -> None:
    """Block Sprint planning when candidate readiness metadata is incomplete."""
    story = _story(1, "req-a", points=points, rank=rank)
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a")),
        stories=(story,),
    )
    readiness = _node(snapshot, "planning.story_readiness")
    sprint = _node(snapshot, "planning.sprint.plan")
    assert readiness.category is NodeCategory.AVAILABLE
    assert any(blocker.code == reason for blocker in sprint.blockers)
    assert sprint.category is NodeCategory.BLOCKED


def test_no_candidates_blocks_sprint_planning_explicitly() -> None:
    """Block Sprint planning explicitly when no candidate Story exists."""
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a")),
        stories=(_story(1, "req-a", candidate=False),),
    )
    sprint = _node(snapshot, "planning.sprint.plan")
    assert sprint.category is NodeCategory.BLOCKED
    assert sprint.reason_code == "SPRINT_CANDIDATES_MISSING"


def test_reviewed_current_plan_is_ready_to_start() -> None:
    """Expose Sprint start for a fresh reviewed plan with exact tasks."""
    story = _story(1, "req-a")
    task = _task(1)
    current_fingerprint = candidate_set_fingerprint((story,), ())
    sprint_plan = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": 501,
            "artifact_fingerprint": "sha256:plan",
            "source_fingerprint": ROADMAP_FINGERPRINT,
            "requirement_id": None,
            "story_ids": [1],
            "sprint_id": 601,
            "candidate_set_fingerprint": current_fingerprint,
            "task_content_fingerprint": current_task_content_fingerprint(
                (task,),
                sprint_id=601,
                story_ids=(1,),
            ),
            "supersedes_artifact_id": None,
            "status": "accepted",
        }
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a"), sprint_plan),
        stories=(story,),
        tasks=(task,),
        decisions=(_decision("sprint", artifact_id=501, fingerprint="sha256:plan"),),
    )
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.AVAILABLE
    assert start.reason_code == "SPRINT_READY_TO_START"


def test_story_change_makes_reviewed_sprint_plan_stale() -> None:
    """Invalidate a reviewed Sprint plan after candidate Story changes."""
    original = _story(1, "req-a")
    task = _task(1)
    sprint_plan = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": 501,
            "artifact_fingerprint": "sha256:plan",
            "source_fingerprint": ROADMAP_FINGERPRINT,
            "requirement_id": None,
            "story_ids": [1],
            "sprint_id": 601,
            "candidate_set_fingerprint": candidate_set_fingerprint((original,), ()),
            "task_content_fingerprint": current_task_content_fingerprint(
                (task,),
                sprint_id=601,
                story_ids=(1,),
            ),
            "supersedes_artifact_id": None,
            "status": "accepted",
        }
    )
    changed = original.model_copy(update={"story_points": 5})
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a"), sprint_plan),
        stories=(changed,),
        tasks=(task,),
        decisions=(_decision("sprint", artifact_id=501, fingerprint="sha256:plan"),),
    )
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_STALE"
