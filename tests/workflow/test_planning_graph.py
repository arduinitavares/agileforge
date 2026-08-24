"""Pure planning child-graph matrix and repeated-node properties."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict, Unpack

import pytest

from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)
from workflow.definitions.planning import (
    _sprint_start_rule,
    candidate_set_fingerprint,
    planning_graph,
    story_dependency_source_fingerprint,
)
from workflow.facts import (
    BacklogItemFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    PostSprintTriageFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SpecVersionFact,
    SprintFact,
    SprintStartFact,
    StoryDependencyFact,
    StoryDependencyReviewFact,
    StoryFact,
    TaskFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)
from workflow.graph import RuleCategory
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
SPEC_VERSION_ID = 41
SPEC_HASH = "sha256:spec"
EXPECTED_PARALLEL_STORY_COUNT = 2


class _PhaseArtifactOptions(TypedDict):
    artifact_id: int
    fingerprint: str
    status: str


class _StoryOptions(TypedDict, total=False):
    points: int | None
    rank: str | None
    accepted: bool
    candidate: bool


class _SnapshotOptions(TypedDict, total=False):
    backlog_status: str | None
    requirements: tuple[BacklogItemFact, ...]
    planning_artifacts: tuple[PlanningArtifactFact, ...]
    stories: tuple[StoryFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    dependency_reviews: tuple[StoryDependencyReviewFact, ...] | None
    tasks: tuple[TaskFact, ...]
    decisions: tuple[ReviewDecisionFact, ...]
    sprints: tuple[SprintFact, ...]
    sprint_starts: tuple[SprintStartFact, ...]
    post_sprint_triage: tuple[PostSprintTriageFact, ...]
    spec_version_id: int
    spec_hash: str
    backlog_spec_version_id: int | None
    backlog_spec_hash: str | None


def _phase_artifact(
    artifact_type: str,
    **options: Unpack[_PhaseArtifactOptions],
) -> PhaseArtifactFact:
    return PhaseArtifactFact.model_validate(
        {
            "artifact_type": artifact_type,
            "artifact_id": options["artifact_id"],
            "artifact_fingerprint": options["fingerprint"],
            "spec_version_id": SPEC_VERSION_ID,
            "spec_hash": SPEC_HASH,
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


def _requirements(*ids: str) -> tuple[BacklogItemFact, ...]:
    return tuple(
        BacklogItemFact(
            backlog_item_id=requirement_id,
            backlog_artifact_id=BACKLOG_ID,
            backlog_artifact_fingerprint=BACKLOG_FINGERPRINT,
            item_fingerprint=f"sha256:item-{requirement_id}",
            spec_item_ids=(f"SPEC-{index:03d}",),
            priority=index,
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
            "backlog_artifact_id": BACKLOG_ID,
            "backlog_artifact_fingerprint": BACKLOG_FINGERPRINT,
            "roadmap_artifact_id": ROADMAP_ID,
            "roadmap_artifact_fingerprint": ROADMAP_FINGERPRINT,
            "candidate_set_fingerprint": None,
            "supersedes_artifact_id": None,
            "status": status,
        }
    )


def _story(
    story_id: int,
    _backlog_item_id: str,
    **options: Unpack[_StoryOptions],
) -> StoryFact:
    points = options.get("points", 3)
    rank = options.get("rank", "1")
    accepted = options.get("accepted", True)
    candidate = options.get("candidate", True)
    return StoryFact(
        story_id=story_id,
        source_story_artifact_id=300 + story_id,
        source_story_artifact_fingerprint=f"sha256:story-{story_id}",
        source_story_item_id=f"US-{story_id:06d}",
        source_story_item_fingerprint=f"sha256:story-item-{story_id}",
        accepted_spec_version_id=SPEC_VERSION_ID,
        accepted_spec_hash=SPEC_HASH,
        spec_item_ids=(f"SPEC-{story_id:03d}",),
        content_fingerprint=f"sha256:story-{story_id}",
        content_accepted=accepted,
        story_artifact_id=300 + story_id if accepted else None,
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
            "backlog_artifact_id": BACKLOG_ID,
            "backlog_artifact_fingerprint": BACKLOG_FINGERPRINT,
            "roadmap_artifact_id": ROADMAP_ID,
            "roadmap_artifact_fingerprint": ROADMAP_FINGERPRINT,
            "backlog_item_id": requirement_id,
            "story_item_ids": [f"US-{story_id:06d}"],
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
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                spec_version_id=SPEC_VERSION_ID,
                spec_hash="sha256:" + "a" * 64,
                sprint_plan_stream_id="SPS-" + "b" * 32,
                sprint_plan_artifact_id=601,
                sprint_plan_fingerprint="sha256:" + "c" * 64,
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


def _sprint_plan_artifact(  # noqa: PLR0913
    *,
    artifact_id: int,
    stream_id: str,
    status: str,
    selected_story_ids: tuple[int, ...] = (1,),
    activated_sprint_id: int | None = None,
    candidate_fingerprint: str = "sha256:historical-candidates",
) -> PlanningArtifactFact:
    return PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": artifact_id,
            "artifact_fingerprint": f"sha256:plan-{artifact_id}",
            "source_fingerprint": candidate_fingerprint,
            "spec_version_id": SPEC_VERSION_ID,
            "spec_hash": SPEC_HASH,
            "sprint_plan_stream_id": stream_id,
            "selected_story_ids": selected_story_ids,
            "activated_sprint_id": activated_sprint_id,
            "candidate_set_fingerprint": candidate_fingerprint,
            "task_content_fingerprint": (
                None
                if activated_sprint_id is None
                else f"sha256:tasks-{activated_sprint_id}"
            ),
            "status": status,
        }
    )


def _sprint_start_fact(
    plan: PlanningArtifactFact,
    *,
    plan_fingerprint: str | None = None,
    start_id: int = 1,
    started_at: datetime | None = None,
) -> SprintStartFact:
    sprint_id = plan.activated_sprint_id
    task_fingerprint = plan.task_content_fingerprint
    candidate_fingerprint = plan.candidate_set_fingerprint
    assert sprint_id is not None
    assert task_fingerprint is not None
    assert candidate_fingerprint is not None
    assert plan.spec_version_id is not None
    assert plan.spec_hash is not None
    return SprintStartFact(
        start_id=start_id,
        sprint_id=sprint_id,
        spec_version_id=plan.spec_version_id,
        spec_hash=plan.spec_hash,
        sprint_plan_artifact_id=plan.artifact_id,
        sprint_plan_artifact_decision_id=plan.artifact_id + 1,
        story_dependency_review_id=plan.artifact_id + 2,
        plan_fingerprint=(
            plan.artifact_fingerprint if plan_fingerprint is None else plan_fingerprint
        ),
        candidate_set_fingerprint=candidate_fingerprint,
        selected_story_ids=plan.selected_story_ids,
        task_content_fingerprint=task_fingerprint,
        dependency_source_fingerprint="sha256:dependency-source",
        dependency_fingerprint="sha256:dependencies",
        dependency_rows_fingerprint="sha256:dependency-rows",
        decision_fingerprint="sha256:decision",
        audit_event_id=plan.artifact_id + 3,
        audit_event_fingerprint="sha256:audit",
        started_by="operator@example.com",
        started_at=(
            EVALUATED_AT - timedelta(hours=1) if started_at is None else started_at
        ),
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
    sprints = options.get("sprints", ())
    sprint_starts = options.get("sprint_starts", ())
    post_sprint_triage = options.get("post_sprint_triage", ())
    spec_version_id = options.get("spec_version_id", SPEC_VERSION_ID)
    spec_hash = options.get("spec_hash", SPEC_HASH)
    backlog_spec_version_id = options.get("backlog_spec_version_id")
    backlog_spec_hash = options.get("backlog_spec_hash")
    backlog = (
        (
            _phase_artifact(
                "backlog",
                artifact_id=BACKLOG_ID,
                fingerprint=BACKLOG_FINGERPRINT,
                status=backlog_status,
            ).model_copy(
                update={
                    "spec_version_id": (
                        spec_version_id
                        if backlog_spec_version_id is None
                        else backlog_spec_version_id
                    ),
                    "spec_hash": (
                        spec_hash if backlog_spec_hash is None else backlog_spec_hash
                    ),
                }
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
            created_at=EVALUATED_AT,
        ),
        spec_versions=(
            SpecVersionFact(
                spec_version_id=spec_version_id,
                spec_hash=spec_hash,
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
                payload_fingerprint=spec_hash,
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
        specification_decisions=(
            SpecificationDecisionFact(
                specification_decision_id=1,
                specification_candidate_id=1,
                candidate_fingerprint="sha256:candidate-1",
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="specification-accepted",
                decided_at=EVALUATED_AT,
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
        phase_artifacts=backlog,
        backlog_items=requirements,
        planning_artifacts=planning_artifacts,
        stories=stories,
        story_dependencies=dependencies,
        story_dependency_reviews=current_dependency_reviews,
        tasks=tasks,
        review_decisions=(
            *backlog_decision,
            *decisions,
        ),
        sprints=sprints,
        sprint_starts=sprint_starts,
        post_sprint_triage=post_sprint_triage,
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


def test_backlog_accepted_under_stale_specification_cannot_expose_planning() -> None:
    """Require the Backlog to bind the exact accepted current Specification."""
    decision = _node(
        _snapshot(
            spec_version_id=SPEC_VERSION_ID + 1,
            spec_hash="sha256:current-spec",
            backlog_spec_version_id=SPEC_VERSION_ID,
            backlog_spec_hash=SPEC_HASH,
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


def test_story_nodes_offer_generation_and_accepted_leaf_correction() -> None:
    """Offer new PBI generation plus optional re-entry for an accepted leaf."""
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
        "backlog_item:req-a",
        "backlog_item:req-b",
        "backlog_item:req-c",
    ]
    assert all(item.category is NodeCategory.AVAILABLE for item in decisions)
    correction = next(
        item for item in decisions if item.instance_key == "backlog_item:req-b"
    )
    assert correction.reason_code == "STORY_CORRECTION_AVAILABLE"
    assert correction.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert any(
        reference.fact_type == "backlog_item" and reference.fact_id == "req-b"
        for reference in correction.fact_references
    )


def test_accepted_story_under_replaced_same_backlog_roadmap_offers_successor() -> None:
    """A current same-PBI Roadmap replacement opens normal Story generation."""
    replacement_roadmap_id = ROADMAP_ID + 1
    replacement_roadmap_fingerprint = "sha256:replacement-roadmap"
    prior_roadmap = _roadmap()
    replacement_roadmap = prior_roadmap.model_copy(
        update={
            "artifact_id": replacement_roadmap_id,
            "artifact_fingerprint": replacement_roadmap_fingerprint,
            "roadmap_artifact_id": replacement_roadmap_id,
            "roadmap_artifact_fingerprint": replacement_roadmap_fingerprint,
            "version_number": 2,
            "supersedes_artifact_id": ROADMAP_ID,
        }
    )
    accepted_story = _story_artifact(1, "req-a")
    decision = _node(
        _snapshot(
            requirements=_requirements("req-a"),
            planning_artifacts=(
                prior_roadmap,
                replacement_roadmap,
                accepted_story,
            ),
            stories=(_story(1, "req-a"),),
            decisions=(
                _decision(
                    "roadmap",
                    artifact_id=replacement_roadmap_id,
                    fingerprint=replacement_roadmap_fingerprint,
                ),
            ),
        ),
        "planning.story.generate",
        "backlog_item:req-a",
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "STORY_GENERATION_REQUIRED"
    assert decision.recommendation_kind is RecommendationKind.REQUIRED
    assert any(
        reference.fact_type == "roadmap"
        and reference.fact_id == str(replacement_roadmap_id)
        and reference.fingerprint == replacement_roadmap_fingerprint
        for reference in decision.fact_references
    )
    assert any(
        reference.fact_type == "story"
        and reference.fact_id == str(accepted_story.artifact_id)
        and reference.fingerprint == accepted_story.artifact_fingerprint
        for reference in decision.fact_references
    )


def test_accepted_story_with_drifted_current_roadmap_binding_stays_invalid() -> None:
    """Do not mistake same-Roadmap fingerprint corruption for a replacement."""
    accepted_story = _story_artifact(1, "req-a").model_copy(
        update={
            "source_fingerprint": "sha256:drifted-roadmap",
            "roadmap_artifact_fingerprint": "sha256:drifted-roadmap",
        }
    )
    decision = _node(
        _snapshot(
            requirements=_requirements("req-a"),
            planning_artifacts=(_roadmap(), accepted_story),
            stories=(_story(1, "req-a"),),
            decisions=(
                _decision(
                    "roadmap",
                    artifact_id=ROADMAP_ID,
                    fingerprint=ROADMAP_FINGERPRINT,
                ),
            ),
        ),
        "planning.story.generate",
        "backlog_item:req-a",
    )

    assert decision.category is NodeCategory.INVALID
    assert decision.reason_code == "STORY_ARTIFACT_STALE"


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
    review = _node(snapshot, "planning.story.review", "backlog_item:req-a")
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
            "backlog_item:req-a",
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
                "spec_version_id": SPEC_VERSION_ID,
                "spec_hash": SPEC_HASH,
                "sprint_plan_stream_id": "SPS-0123456789abcdef0123456789abcdef",
                "selected_story_ids": [1],
                "activated_sprint_id": 601,
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
            "spec_version_id": SPEC_VERSION_ID,
            "spec_hash": SPEC_HASH,
            "sprint_plan_stream_id": "SPS-0123456789abcdef0123456789abcdef",
            "selected_story_ids": [1],
            "activated_sprint_id": 601,
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
        sprints=(SprintFact(sprint_id=601, status="planned", completed_at=None),),
    )
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.AVAILABLE
    assert start.reason_code == "SPRINT_READY_TO_START"


def test_sequential_terminal_sprint_streams_expose_next_planning() -> None:
    """Use lifecycle order, not artifact ID, after two completed streams."""
    story = _story(1, "req-a")
    older = _sprint_plan_artifact(
        artifact_id=900,
        stream_id="SPS-ffffffffffffffffffffffffffffffff",
        status="accepted",
        selected_story_ids=(90,),
        activated_sprint_id=601,
    )
    newer = _sprint_plan_artifact(
        artifact_id=100,
        stream_id="SPS-00000000000000000000000000000000",
        status="accepted",
        selected_story_ids=(91,),
        activated_sprint_id=602,
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            older,
            newer,
        ),
        stories=(story,),
        sprints=(
            SprintFact(
                sprint_id=601,
                status="completed",
                completed_at=EVALUATED_AT - timedelta(days=2),
            ),
            SprintFact(
                sprint_id=602,
                status="completed",
                completed_at=EVALUATED_AT - timedelta(days=1),
            ),
        ),
        sprint_starts=(
            _sprint_start_fact(
                older,
                start_id=1,
                started_at=EVALUATED_AT - timedelta(days=3),
            ),
            _sprint_start_fact(
                newer,
                start_id=2,
                started_at=EVALUATED_AT - timedelta(days=2),
            ),
        ),
        post_sprint_triage=(
            PostSprintTriageFact(
                triage_id=1,
                sprint_id=601,
                impact="none",
                canonical_payload={},
                payload_fingerprint="sha256:triage-601",
            ),
            PostSprintTriageFact(
                triage_id=2,
                sprint_id=602,
                impact="none",
                canonical_payload={},
                payload_fingerprint="sha256:triage-602",
            ),
        ),
    )

    planning = _node(snapshot, "planning.sprint.plan")

    assert planning.category is NodeCategory.AVAILABLE
    assert planning.reason_code == "NEXT_SPRINT_PLANNING_REQUIRED"
    assert planning.fact_references[0].fact_id == "100"


def test_started_sprint_stream_exposes_next_cycle_planning() -> None:
    """A matching SprintStart closes correction and permits a new stream."""
    story = _story(1, "req-a")
    started_plan = _sprint_plan_artifact(
        artifact_id=900,
        stream_id="SPS-ffffffffffffffffffffffffffffffff",
        status="accepted",
        selected_story_ids=(90,),
        activated_sprint_id=601,
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            started_plan,
        ),
        stories=(story,),
        sprints=(SprintFact(sprint_id=601, status="active", completed_at=None),),
        sprint_starts=(_sprint_start_fact(started_plan),),
    )

    planning = _node(snapshot, "planning.sprint.plan")
    position = _position(snapshot)
    start_rule = _sprint_start_rule(snapshot, EVALUATED_AT)[0]

    assert planning.category is NodeCategory.AVAILABLE
    assert planning.reason_code == "NEXT_SPRINT_PLANNING_REQUIRED"
    assert planning.fact_references[0].fact_id == "900"
    assert "planning.sprint.start" not in position.available_nodes
    assert all(item.node_id != "planning.sprint.start" for item in position.decisions)
    assert start_rule.category is RuleCategory.SATISFIED
    assert start_rule.reason_code == "SPRINT_ALREADY_STARTED"


def test_different_active_sprint_blocks_start_with_stable_reason() -> None:
    """Distinguish another active Sprint from an exact already-started plan."""
    story = _story(1, "req-a")
    task = _task(1)
    plan = _sprint_plan_artifact(
        artifact_id=501,
        stream_id="SPS-11111111111111111111111111111111",
        status="accepted",
        activated_sprint_id=601,
        candidate_fingerprint=candidate_set_fingerprint((story,), ()),
    ).model_copy(
        update={
            "task_content_fingerprint": current_task_content_fingerprint(
                (task,),
                sprint_id=601,
                story_ids=(1,),
            )
        }
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a"), plan),
        stories=(story,),
        tasks=(task,),
        decisions=(
            _decision("sprint", artifact_id=501, fingerprint="sha256:plan-501"),
        ),
        sprints=(
            SprintFact(sprint_id=601, status="planned", completed_at=None),
            SprintFact(sprint_id=602, status="active", completed_at=None),
        ),
    )

    start_rule = _sprint_start_rule(snapshot, EVALUATED_AT)[0]

    assert start_rule.category is RuleCategory.BLOCKED
    assert start_rule.reason_code == "ACTIVE_SPRINT_EXISTS"
    assert start_rule.blockers[0].message == (
        "Another Sprint is already active for this Project. Close it before "
        "starting this Sprint."
    )


@pytest.mark.parametrize("with_unrelated_active_sprint", [False, True])
def test_superseded_specification_plan_remains_discoverable_for_stale_start(
    with_unrelated_active_sprint: bool,
) -> None:
    """A planned accepted old-Spec plan fails stale before active-Sprint checks."""
    stale_plan = _sprint_plan_artifact(
        artifact_id=701,
        stream_id="SPS-22222222222222222222222222222222",
        status="accepted",
        activated_sprint_id=801,
    ).model_copy(
        update={
            "spec_version_id": SPEC_VERSION_ID,
            "spec_hash": SPEC_HASH,
        }
    )
    sprints = (SprintFact(sprint_id=801, status="planned", completed_at=None),)
    if with_unrelated_active_sprint:
        sprints = (
            *sprints,
            SprintFact(sprint_id=802, status="active", completed_at=None),
        )
    snapshot = _snapshot(
        spec_version_id=SPEC_VERSION_ID + 1,
        spec_hash="sha256:replacement-spec",
        planning_artifacts=(stale_plan,),
        sprints=sprints,
    )

    start_rule = _sprint_start_rule(snapshot, EVALUATED_AT)[0]

    assert start_rule.category is RuleCategory.INVALID
    assert start_rule.reason_code == "STALE_SPECIFICATION"


@pytest.mark.parametrize(
    ("sprint_status", "include_start", "completed_at"),
    [
        ("active", False, None),
        ("completed", False, EVALUATED_AT - timedelta(hours=1)),
        ("planned", True, None),
    ],
)
def test_superseded_specification_lifecycle_corruption_precedes_stale_start(
    sprint_status: Literal["planned", "active", "completed"],
    include_start: bool,
    completed_at: datetime | None,
) -> None:
    """Keep an old-Spec target visible until its Sprint lifecycle is validated."""
    stale_plan = _sprint_plan_artifact(
        artifact_id=701,
        stream_id="SPS-22222222222222222222222222222222",
        status="accepted",
        activated_sprint_id=801,
    )
    snapshot = _snapshot(
        spec_version_id=SPEC_VERSION_ID + 1,
        spec_hash="sha256:replacement-spec",
        planning_artifacts=(stale_plan,),
        sprints=(
            SprintFact(
                sprint_id=801,
                status=sprint_status,
                completed_at=completed_at,
            ),
        ),
        sprint_starts=((_sprint_start_fact(stale_plan),) if include_start else ()),
    )

    start_rule = _sprint_start_rule(snapshot, EVALUATED_AT)[0]

    assert start_rule.category is RuleCategory.INVALID
    assert start_rule.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_superseded_specification_active_started_plan_remains_satisfied() -> None:
    """Preserve exact active old-lineage start evidence after Spec replacement."""
    started_plan = _sprint_plan_artifact(
        artifact_id=701,
        stream_id="SPS-22222222222222222222222222222222",
        status="accepted",
        activated_sprint_id=801,
    )
    snapshot = _snapshot(
        spec_version_id=SPEC_VERSION_ID + 1,
        spec_hash="sha256:replacement-spec",
        planning_artifacts=(started_plan,),
        sprints=(SprintFact(sprint_id=801, status="active", completed_at=None),),
        sprint_starts=(_sprint_start_fact(started_plan),),
    )

    start_rule = _sprint_start_rule(snapshot, EVALUATED_AT)[0]

    assert start_rule.category is RuleCategory.SATISFIED
    assert start_rule.reason_code == "SPRINT_ALREADY_STARTED"


@pytest.mark.parametrize(
    ("include_current_target", "older_role", "category", "reason"),
    [
        (True, "started", RuleCategory.BLOCKED, "ACTIVE_SPRINT_EXISTS"),
        (True, "corrupt", RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),
        (True, "unstarted", RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),
        (False, "started", RuleCategory.SATISFIED, "SPRINT_ALREADY_STARTED"),
        (False, "corrupt", RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),
        (False, "unstarted", RuleCategory.INVALID, "STALE_SPECIFICATION"),
    ],
)
def test_sprint_start_distinguishes_current_target_from_older_lifecycle_role(
    include_current_target: bool,
    older_role: Literal["started", "corrupt", "unstarted"],
    category: RuleCategory,
    reason: str,
) -> None:
    """Choose the current target while validating older lifecycle evidence."""
    story = _story(1, "req-a")
    task = _task(1)
    candidate_fingerprint = candidate_set_fingerprint((story,), ())
    current_plan = _sprint_plan_artifact(
        artifact_id=501,
        stream_id="SPS-11111111111111111111111111111111",
        status="accepted",
        activated_sprint_id=601,
        candidate_fingerprint=candidate_fingerprint,
    ).model_copy(
        update={
            "task_content_fingerprint": current_task_content_fingerprint(
                (task,),
                sprint_id=601,
                story_ids=(1,),
            )
        }
    )
    older_plan = _sprint_plan_artifact(
        artifact_id=401,
        stream_id="SPS-00000000000000000000000000000000",
        status="accepted",
        activated_sprint_id=801,
    ).model_copy(
        update={
            "spec_version_id": SPEC_VERSION_ID - 1,
            "spec_hash": "sha256:older-spec",
        }
    )
    older_status = "planned" if older_role == "unstarted" else "active"
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            *((current_plan,) if include_current_target else ()),
            older_plan,
        ),
        stories=(story,),
        tasks=(task,),
        sprints=(
            *(
                (SprintFact(sprint_id=601, status="planned", completed_at=None),)
                if include_current_target
                else ()
            ),
            SprintFact(sprint_id=801, status=older_status, completed_at=None),
        ),
        sprint_starts=(
            (_sprint_start_fact(older_plan),) if older_role == "started" else ()
        ),
    )

    start_rule = _sprint_start_rule(snapshot, EVALUATED_AT)[0]

    assert start_rule.category is category
    assert start_rule.reason_code == reason
    if reason == "ACTIVE_SPRINT_EXISTS":
        assert start_rule.blockers[0].message == (
            "Another Sprint is already active for this Project. Close it before "
            "starting this Sprint."
        )


def _completed_sprint_snapshot(
    *,
    include_start: bool,
) -> WorkflowFactSnapshot:
    story = _story(1, "req-a")
    task = _task(1)
    candidate_fingerprint = candidate_set_fingerprint((story,), ())
    started_plan = _sprint_plan_artifact(
        artifact_id=900,
        stream_id="SPS-ffffffffffffffffffffffffffffffff",
        status="accepted",
        activated_sprint_id=601,
        candidate_fingerprint=candidate_fingerprint,
    ).model_copy(
        update={
            "task_content_fingerprint": current_task_content_fingerprint(
                (task,),
                sprint_id=601,
                story_ids=(1,),
            )
        }
    )
    return _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            started_plan,
        ),
        stories=(story,),
        tasks=(task,),
        sprints=(
            SprintFact(
                sprint_id=601,
                status="completed",
                completed_at=EVALUATED_AT - timedelta(hours=1),
            ),
        ),
        sprint_starts=((_sprint_start_fact(started_plan),) if include_start else ()),
        post_sprint_triage=(
            PostSprintTriageFact(
                triage_id=1,
                sprint_id=601,
                impact="none",
                canonical_payload={},
                payload_fingerprint="sha256:triage-601",
            ),
        ),
    )


def test_completed_started_stream_cannot_start_twice() -> None:
    """A completed exact started stream exposes planning but consumes start."""
    snapshot = _completed_sprint_snapshot(include_start=True)
    planning = _node(snapshot, "planning.sprint.plan")
    position = _position(snapshot)
    start_rule = _sprint_start_rule(snapshot, EVALUATED_AT)[0]

    assert planning.category is NodeCategory.AVAILABLE
    assert planning.reason_code == "NEXT_SPRINT_PLANNING_REQUIRED"
    assert "planning.sprint.start" not in position.available_nodes
    assert all(item.node_id != "planning.sprint.start" for item in position.decisions)
    assert start_rule.category is RuleCategory.SATISFIED
    assert start_rule.reason_code == "SPRINT_ALREADY_STARTED"


@pytest.mark.parametrize("sprint_state", ["missing", "active", "completed"])
def test_nonplanned_sprint_without_exact_start_fails_closed(
    sprint_state: Literal["missing", "active", "completed"],
) -> None:
    """A missing, active, or completed Sprint without exact start is corrupt."""
    base = _completed_sprint_snapshot(include_start=False)
    snapshot = base.model_copy(
        update={
            "sprints": (
                ()
                if sprint_state == "missing"
                else (
                    SprintFact(
                        sprint_id=601,
                        status=sprint_state,
                        completed_at=(
                            EVALUATED_AT - timedelta(hours=1)
                            if sprint_state == "completed"
                            else None
                        ),
                    ),
                )
            ),
            "post_sprint_triage": (
                base.post_sprint_triage if sprint_state == "completed" else ()
            ),
        }
    )

    planning = _node(snapshot, "planning.sprint.plan")
    assert planning.category is NodeCategory.INVALID
    assert planning.reason_code == "WORKFLOW_FACT_CONFLICT"
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_corrupt_active_lifecycle_precedes_missing_candidates_across_nodes() -> None:
    """Expose one lifecycle conflict consistently before candidate joining."""
    base = _completed_sprint_snapshot(include_start=False)
    snapshot = base.model_copy(
        update={
            "stories": (),
            "tasks": (),
            "sprints": (SprintFact(sprint_id=601, status="active", completed_at=None),),
            "post_sprint_triage": (),
        }
    )

    for node_id in (
        "planning.sprint.plan",
        "planning.sprint.review",
        "planning.sprint.start",
    ):
        decision = _node(snapshot, node_id)
        assert decision.category is NodeCategory.INVALID
        assert decision.reason_code == "WORKFLOW_FACT_CONFLICT"


@pytest.mark.parametrize("sprint_state", ["missing", "planned"])
def test_exact_start_without_active_or_completed_sprint_fails_closed(
    sprint_state: Literal["missing", "planned"],
) -> None:
    """An exact start requires its one atomically active-or-completed Sprint."""
    snapshot = _completed_sprint_snapshot(include_start=True).model_copy(
        update={
            "sprints": (
                ()
                if sprint_state == "missing"
                else (
                    SprintFact(
                        sprint_id=601,
                        status="planned",
                        completed_at=None,
                    ),
                )
            ),
            "post_sprint_triage": (),
        }
    )

    planning = _node(snapshot, "planning.sprint.plan")
    assert planning.category is NodeCategory.INVALID
    assert planning.reason_code == "WORKFLOW_FACT_CONFLICT"
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "WORKFLOW_FACT_CONFLICT"


@pytest.mark.parametrize(
    ("sprint_status", "include_start", "completed_at"),
    [
        ("planned", False, EVALUATED_AT - timedelta(hours=1)),
        ("active", True, EVALUATED_AT),
        ("completed", True, None),
        ("completed", True, EVALUATED_AT - timedelta(hours=2)),
    ],
)
def test_sprint_lifecycle_timestamp_mismatch_fails_closed(
    sprint_status: Literal["planned", "active", "completed"],
    include_start: bool,
    completed_at: datetime | None,
) -> None:
    """Reject timestamps inconsistent with the exact Sprint lifecycle state."""
    base = _completed_sprint_snapshot(include_start=include_start)
    snapshot = base.model_copy(
        update={
            "sprints": (
                SprintFact(
                    sprint_id=601,
                    status=sprint_status,
                    completed_at=completed_at,
                ),
            ),
            "post_sprint_triage": (
                base.post_sprint_triage if sprint_status == "completed" else ()
            ),
        }
    )

    planning = _node(snapshot, "planning.sprint.plan")
    assert planning.category is NodeCategory.INVALID
    assert planning.reason_code == "WORKFLOW_FACT_CONFLICT"
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "WORKFLOW_FACT_CONFLICT"


@pytest.mark.parametrize(
    ("started_at", "completed_at", "category", "reason"),
    [
        (
            datetime(2026, 8, 2, 10, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 8, 2, 11, tzinfo=UTC),
            NodeCategory.AVAILABLE,
            "NEXT_SPRINT_PLANNING_REQUIRED",
        ),
        (
            datetime(2026, 8, 2, 10, tzinfo=UTC),
            datetime(2026, 8, 2, 11, tzinfo=UTC).replace(tzinfo=None),
            NodeCategory.AVAILABLE,
            "NEXT_SPRINT_PLANNING_REQUIRED",
        ),
        (
            datetime(2026, 8, 2, 11, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 8, 2, 10, tzinfo=UTC),
            NodeCategory.INVALID,
            "WORKFLOW_FACT_CONFLICT",
        ),
    ],
)
def test_sprint_lifecycle_normalizes_mixed_timezone_facts(
    started_at: datetime,
    completed_at: datetime,
    category: NodeCategory,
    reason: str,
) -> None:
    """Normalize persisted UTC facts before deterministic chronology checks."""
    base = _completed_sprint_snapshot(include_start=True)
    raw_start = base.sprint_starts[0]
    start = SprintStartFact.model_validate(
        {**raw_start.model_dump(mode="python"), "started_at": started_at}
    )
    sprint = SprintFact(
        sprint_id=601,
        status="completed",
        completed_at=completed_at,
    )
    assert start.started_at.tzinfo is UTC
    assert sprint.completed_at is not None
    assert sprint.completed_at.tzinfo is UTC
    snapshot = base.model_copy(update={"sprints": (sprint,), "sprint_starts": (start,)})

    planning = _node(snapshot, "planning.sprint.plan")

    assert planning.category is category
    assert planning.reason_code == reason


@pytest.mark.parametrize("leaf_status", ["feedback", "rejected"])
def test_started_accepted_plan_freezes_unaccepted_physical_leaf(
    leaf_status: str,
) -> None:
    """Start A freezes its stream even when physical successor B is unaccepted."""
    story = _story(1, "req-a")
    candidate_fingerprint = candidate_set_fingerprint((story,), ())
    accepted = _sprint_plan_artifact(
        artifact_id=501,
        stream_id="SPS-0123456789abcdef0123456789abcdef",
        status="accepted",
        activated_sprint_id=601,
        candidate_fingerprint=candidate_fingerprint,
    )
    correction = accepted.model_copy(
        update={
            "artifact_id": 502,
            "artifact_fingerprint": "sha256:plan-502",
            "version_number": 2,
            "supersedes_artifact_id": 501,
            "activated_sprint_id": None,
            "task_content_fingerprint": None,
            "status": leaf_status,
        }
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            accepted,
            correction,
        ),
        stories=(story,),
        sprints=(SprintFact(sprint_id=601, status="active", completed_at=None),),
        sprint_starts=(_sprint_start_fact(accepted),),
    )

    planning = _node(snapshot, "planning.sprint.plan")

    assert planning.category is NodeCategory.AVAILABLE
    assert planning.reason_code == "NEXT_SPRINT_PLANNING_REQUIRED"
    assert planning.fact_references[0].fact_id == "501"


@pytest.mark.parametrize("leaf_status", ["feedback", "rejected"])
def test_mismatched_start_does_not_freeze_unaccepted_physical_leaf(
    leaf_status: str,
) -> None:
    """A corrupt start that targets the stream fails closed."""
    story = _story(1, "req-a")
    candidate_fingerprint = candidate_set_fingerprint((story,), ())
    accepted = _sprint_plan_artifact(
        artifact_id=501,
        stream_id="SPS-0123456789abcdef0123456789abcdef",
        status="accepted",
        activated_sprint_id=601,
        candidate_fingerprint=candidate_fingerprint,
    )
    correction = accepted.model_copy(
        update={
            "artifact_id": 502,
            "artifact_fingerprint": "sha256:plan-502",
            "version_number": 2,
            "supersedes_artifact_id": 501,
            "activated_sprint_id": None,
            "task_content_fingerprint": None,
            "status": leaf_status,
        }
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            accepted,
            correction,
        ),
        stories=(story,),
        sprints=(SprintFact(sprint_id=601, status="active", completed_at=None),),
        sprint_starts=(
            _sprint_start_fact(accepted, plan_fingerprint="sha256:corrupt"),
        ),
    )

    planning = _node(snapshot, "planning.sprint.plan")

    assert planning.category is NodeCategory.INVALID
    assert planning.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_extra_targeted_start_in_one_stream_fails_closed() -> None:
    """Reject an exact accepted-plan start plus any extra targeted start row."""
    story = _story(1, "req-a")
    candidate_fingerprint = candidate_set_fingerprint((story,), ())
    accepted = _sprint_plan_artifact(
        artifact_id=501,
        stream_id="SPS-0123456789abcdef0123456789abcdef",
        status="accepted",
        activated_sprint_id=601,
        candidate_fingerprint=candidate_fingerprint,
    )
    correction = accepted.model_copy(
        update={
            "artifact_id": 502,
            "artifact_fingerprint": "sha256:plan-502",
            "version_number": 2,
            "supersedes_artifact_id": 501,
            "activated_sprint_id": None,
            "task_content_fingerprint": None,
            "status": "feedback",
        }
    )
    exact_start = _sprint_start_fact(accepted)
    extra_start = exact_start.model_copy(
        update={
            "start_id": 2,
            "sprint_id": 602,
            "sprint_plan_artifact_id": correction.artifact_id,
            "plan_fingerprint": correction.artifact_fingerprint,
        }
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            accepted,
            correction,
        ),
        stories=(story,),
        sprints=(SprintFact(sprint_id=601, status="active", completed_at=None),),
        sprint_starts=(exact_start, extra_start),
    )

    planning = _node(snapshot, "planning.sprint.plan")

    assert planning.category is NodeCategory.INVALID
    assert planning.reason_code == "WORKFLOW_FACT_CONFLICT"


@pytest.mark.parametrize("open_status", ["feedback", "rejected"])
def test_open_sprint_stream_wins_over_closed_history(open_status: str) -> None:
    """Select the sole open correction stream ahead of completed history."""
    story = _story(1, "req-a")
    current_fingerprint = candidate_set_fingerprint((story,), ())
    closed = _sprint_plan_artifact(
        artifact_id=900,
        stream_id="SPS-ffffffffffffffffffffffffffffffff",
        status="accepted",
        selected_story_ids=(90,),
        activated_sprint_id=601,
    )
    open_plan = _sprint_plan_artifact(
        artifact_id=100,
        stream_id="SPS-00000000000000000000000000000000",
        status=open_status,
        candidate_fingerprint=current_fingerprint,
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            closed,
            open_plan,
        ),
        stories=(story,),
        sprints=(
            SprintFact(
                sprint_id=601,
                status="completed",
                completed_at=EVALUATED_AT - timedelta(days=1),
            ),
        ),
        sprint_starts=(
            _sprint_start_fact(
                closed,
                started_at=EVALUATED_AT - timedelta(days=2),
            ),
        ),
    )

    planning = _node(snapshot, "planning.sprint.plan")

    assert planning.category is NodeCategory.AVAILABLE
    assert planning.reason_code == "SPRINT_PLAN_REVISION_REQUIRED"
    assert planning.fact_references[0].fact_id == "100"


def test_two_open_sprint_streams_fail_closed() -> None:
    """Never guess between parallel unstarted streams for one Specification."""
    story = _story(1, "req-a")
    current_fingerprint = candidate_set_fingerprint((story,), ())
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            _sprint_plan_artifact(
                artifact_id=900,
                stream_id="SPS-ffffffffffffffffffffffffffffffff",
                status="feedback",
                candidate_fingerprint=current_fingerprint,
            ),
            _sprint_plan_artifact(
                artifact_id=100,
                stream_id="SPS-00000000000000000000000000000000",
                status="rejected",
                candidate_fingerprint=current_fingerprint,
            ),
        ),
        stories=(story,),
    )

    planning = _node(snapshot, "planning.sprint.plan")

    assert planning.category is NodeCategory.INVALID
    assert planning.reason_code == "WORKFLOW_FACT_CONFLICT"


@pytest.mark.parametrize("leaf_status", ["feedback", "rejected"])
def test_accepted_sprint_plan_starts_behind_unaccepted_leaf(
    leaf_status: str,
) -> None:
    """Keep accepted plan A startable while correction B is not accepted."""
    story = _story(1, "req-a")
    task = _task(1)
    candidate_fingerprint = candidate_set_fingerprint((story,), ())
    accepted = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": 501,
            "artifact_fingerprint": "sha256:plan-a",
            "version_number": 1,
            "source_fingerprint": candidate_fingerprint,
            "spec_version_id": SPEC_VERSION_ID,
            "spec_hash": SPEC_HASH,
            "sprint_plan_stream_id": "SPS-0123456789abcdef0123456789abcdef",
            "selected_story_ids": [1],
            "activated_sprint_id": 601,
            "candidate_set_fingerprint": candidate_fingerprint,
            "task_content_fingerprint": current_task_content_fingerprint(
                (task,), sprint_id=601, story_ids=(1,)
            ),
            "status": "accepted",
        }
    )
    correction = accepted.model_copy(
        update={
            "artifact_id": 502,
            "artifact_fingerprint": "sha256:plan-b",
            "version_number": 2,
            "supersedes_artifact_id": 501,
            "activated_sprint_id": None,
            "status": leaf_status,
        }
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(
            _roadmap(),
            _story_artifact(1, "req-a"),
            accepted,
            correction,
        ),
        stories=(story,),
        tasks=(task,),
        sprints=(SprintFact(sprint_id=601, status="planned", completed_at=None),),
    )

    start = _node(snapshot, "planning.sprint.start")

    assert start.category is NodeCategory.AVAILABLE
    assert start.reason_code == "SPRINT_READY_TO_START"
    assert start.fact_references[0].fact_id == "501"


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
            "spec_version_id": SPEC_VERSION_ID,
            "spec_hash": SPEC_HASH,
            "sprint_plan_stream_id": "SPS-0123456789abcdef0123456789abcdef",
            "selected_story_ids": [1],
            "activated_sprint_id": 601,
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
        sprints=(SprintFact(sprint_id=601, status="planned", completed_at=None),),
    )
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_STALE"
