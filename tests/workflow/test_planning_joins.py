"""Planning dependency, readiness, and sprint freshness join tests."""

# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime

from workflow.contracts import NodeCategory
from workflow.definitions.planning import candidate_set_fingerprint, planning_graph
from workflow.facts import (
    BacklogRequirementFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    ProjectFact,
    ReviewDecisionFact,
    StoryDependencyFact,
    StoryFact,
    WorkflowFactSnapshot,
)

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


def _story(
    story_id: int,
    *,
    accepted: bool = True,
    points: int | None = 3,
    rank: str | None = "1.1",
    candidate: bool = True,
) -> StoryFact:
    return StoryFact(
        story_id=story_id,
        requirement_id=f"req-{story_id}",
        content_fingerprint=f"sha256:story-{story_id}",
        content_accepted=accepted,
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
        }
    )


def _snapshot(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...] = (),
    *,
    sprint_plan: PlanningArtifactFact | None = None,
) -> WorkflowFactSnapshot:
    requirements = tuple(
        BacklogRequirementFact(
            requirement_id=f"req-{story.story_id}",
            backlog_artifact_id=10,
            backlog_artifact_fingerprint="sha256:backlog",
            requirement=f"Requirement {story.story_id}",
            rank=story.story_id,
        )
        for story in stories
    )
    roadmap = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "roadmap",
            "artifact_id": 20,
            "artifact_fingerprint": "sha256:roadmap",
            "source_fingerprint": "sha256:backlog",
            "story_ids": [],
            "status": "accepted",
        }
    )
    story_artifacts = tuple(
        PlanningArtifactFact.model_validate(
            {
                "artifact_type": "story",
                "artifact_id": 100 + story.story_id,
                "artifact_fingerprint": story.content_fingerprint,
                "source_fingerprint": "sha256:roadmap",
                "requirement_id": story.requirement_id,
                "story_ids": [story.story_id],
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
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1,
            name="Planning joins",
            origin="greenfield",
            created_at=EVALUATED_AT,
        ),
        phase_artifacts=(
            PhaseArtifactFact(
                artifact_type="backlog",
                artifact_id=10,
                artifact_fingerprint="sha256:backlog",
                status="accepted",
            ),
        ),
        backlog_requirements=requirements,
        planning_artifacts=planning_artifacts,
        review_decisions=decisions,
        stories=stories,
        story_dependencies=dependencies,
    )


def _decision(snapshot: WorkflowFactSnapshot, node_id: str):  # noqa: ANN202
    return next(
        item
        for item in planning_graph().evaluate(snapshot, EVALUATED_AT).decisions
        if item.node_id == node_id
    )


def _accepted_plan(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...] = (),
) -> PlanningArtifactFact:
    return PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": 500,
            "artifact_fingerprint": "sha256:plan",
            "source_fingerprint": "sha256:roadmap",
            "story_ids": [story.story_id for story in stories],
            "candidate_set_fingerprint": candidate_set_fingerprint(
                stories,
                dependencies,
            ),
            "status": "accepted",
        }
    )


def test_join_requires_accepted_content_for_every_candidate_story() -> None:
    snapshot = _snapshot((_story(1), _story(2, accepted=False)))
    decision = _decision(snapshot, "planning.sprint.plan")
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "STORY_CONTENT_NOT_ACCEPTED"


def test_join_rejects_semantic_dependency_endpoint_outside_story_set() -> None:
    snapshot = _snapshot((_story(1),), (_dependency(1, 1, 99),))
    decision = _decision(snapshot, "planning.sprint.plan")
    assert decision.category is NodeCategory.INVALID
    assert decision.reason_code == "STORY_DEPENDENCY_INVALID"


def test_join_blocks_unreviewed_proposed_dependencies() -> None:
    snapshot = _snapshot(
        (_story(1), _story(2)),
        (_dependency(1, 2, 1, status="proposed"),),
    )
    decision = _decision(snapshot, "planning.sprint.plan")
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "STORY_DEPENDENCIES_UNREVIEWED"


def test_candidate_fingerprint_is_order_independent() -> None:
    stories = (_story(1), _story(2))
    dependencies = (_dependency(1, 2, 1),)
    assert candidate_set_fingerprint(
        stories, dependencies
    ) == candidate_set_fingerprint(
        tuple(reversed(stories)),
        tuple(reversed(dependencies)),
    )


def test_dependency_change_makes_accepted_plan_stale() -> None:
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
    stories = (_story(1),)
    plan = _accepted_plan(stories)
    changed = (stories[0].model_copy(update={"rank": "1.2"}),)
    start = _decision(
        _snapshot(changed, sprint_plan=plan),
        "planning.sprint.start",
    )
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_STALE"


def test_sprint_plan_review_waits_on_exact_immutable_plan() -> None:
    stories = (_story(1),)
    plan = _accepted_plan(stories).model_copy(update={"status": "pending_review"})
    review = _decision(
        _snapshot(stories, sprint_plan=plan),
        "planning.sprint.review",
    )
    assert review.category is NodeCategory.WAITING
    assert review.fact_references[0].fingerprint == "sha256:plan"


def test_sprint_plan_selected_ids_must_be_current_candidates() -> None:
    stories = (_story(1), _story(2))
    plan = _accepted_plan(stories).model_copy(update={"story_ids": (1, 99)})
    start = _decision(
        _snapshot(stories, sprint_plan=plan),
        "planning.sprint.start",
    )
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_SELECTED_STORY_INVALID"
