"""Pure planning child-graph matrix and repeated-node properties."""

# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from workflow.contracts import NodeCategory, NodeDecision, WorkflowPosition
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
PROJECT_ID = 11
BACKLOG_ID = 101
BACKLOG_FINGERPRINT = "sha256:backlog"
ROADMAP_ID = 201
ROADMAP_FINGERPRINT = "sha256:roadmap"
EXPECTED_PARALLEL_STORY_COUNT = 2


def _phase_artifact(
    artifact_type: str,
    *,
    artifact_id: int,
    fingerprint: str,
    status: str,
) -> PhaseArtifactFact:
    return PhaseArtifactFact.model_validate(
        {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "artifact_fingerprint": fingerprint,
            "status": status,
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
            "source_fingerprint": BACKLOG_FINGERPRINT,
            "requirement_id": None,
            "story_ids": [],
            "candidate_set_fingerprint": None,
            "supersedes_artifact_id": None,
            "status": status,
        }
    )


def _story(  # noqa: PLR0913
    story_id: int,
    requirement_id: str,
    *,
    points: int | None = 3,
    rank: str | None = "1.1",
    accepted: bool = True,
    candidate: bool = True,
) -> StoryFact:
    return StoryFact(
        story_id=story_id,
        requirement_id=requirement_id,
        content_fingerprint=f"sha256:story-{story_id}",
        content_accepted=accepted,
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
            "source_fingerprint": ROADMAP_FINGERPRINT,
            "requirement_id": requirement_id,
            "story_ids": [story_id],
            "candidate_set_fingerprint": None,
            "supersedes_artifact_id": None,
            "status": status,
        }
    )


def _snapshot(  # noqa: PLR0913
    *,
    backlog_status: str | None = "accepted",
    requirements: tuple[BacklogRequirementFact, ...] = (),
    planning_artifacts: tuple[PlanningArtifactFact, ...] = (),
    stories: tuple[StoryFact, ...] = (),
    dependencies: tuple[StoryDependencyFact, ...] = (),
    decisions: tuple[ReviewDecisionFact, ...] = (),
) -> WorkflowFactSnapshot:
    backlog = (
        (
            _phase_artifact(
                "backlog",
                artifact_id=BACKLOG_ID,
                fingerprint=BACKLOG_FINGERPRINT,
                status=backlog_status,
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
            ),
        )
        if backlog_status == "accepted"
        else ()
    )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Planning graph",
            origin="greenfield",
            created_at=EVALUATED_AT,
        ),
        phase_artifacts=backlog,
        backlog_requirements=requirements,
        planning_artifacts=planning_artifacts,
        stories=stories,
        story_dependencies=dependencies,
        review_decisions=(*backlog_decision, *decisions),
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
        (None, NodeCategory.BLOCKED, "ACCEPTED_BACKLOG_REQUIRED"),
        ("pending_review", NodeCategory.BLOCKED, "ACCEPTED_BACKLOG_REQUIRED"),
        ("rejected", NodeCategory.BLOCKED, "ACCEPTED_BACKLOG_REQUIRED"),
        ("accepted", NodeCategory.AVAILABLE, "ROADMAP_GENERATION_REQUIRED"),
    ],
)
def test_roadmap_requires_accepted_current_backlog(
    backlog_status: str | None,
    category: NodeCategory,
    reason: str,
) -> None:
    decision = _node(
        _snapshot(backlog_status=backlog_status), "planning.roadmap.generate"
    )
    assert decision.category is category
    assert decision.reason_code == reason


def test_roadmap_draft_waits_for_exact_review() -> None:
    snapshot = _snapshot(planning_artifacts=(_roadmap("pending_review"),))
    decision = _node(snapshot, "planning.roadmap.review")
    assert decision.category is NodeCategory.WAITING
    assert decision.reason_code == "ROADMAP_REVIEW_REQUIRED"


def test_story_nodes_are_offered_once_per_uncovered_accepted_requirement() -> None:
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


def test_dependency_cycle_fails_join_closed_as_invalid() -> None:
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
        ),
        StoryDependencyFact(
            dependency_id=2,
            dependent_story_id=2,
            prerequisite_story_id=1,
            status="active",
            source="manual_review",
            confidence="reviewed",
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
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a")),
        stories=(_story(1, "req-a", candidate=False),),
    )
    sprint = _node(snapshot, "planning.sprint.plan")
    assert sprint.category is NodeCategory.BLOCKED
    assert sprint.reason_code == "SPRINT_CANDIDATES_MISSING"


def test_reviewed_current_plan_is_ready_to_start() -> None:
    story = _story(1, "req-a")
    current_fingerprint = candidate_set_fingerprint((story,), ())
    sprint_plan = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": 501,
            "artifact_fingerprint": "sha256:plan",
            "source_fingerprint": ROADMAP_FINGERPRINT,
            "requirement_id": None,
            "story_ids": [1],
            "candidate_set_fingerprint": current_fingerprint,
            "supersedes_artifact_id": None,
            "status": "accepted",
        }
    )
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a"), sprint_plan),
        stories=(story,),
        decisions=(_decision("sprint", artifact_id=501, fingerprint="sha256:plan"),),
    )
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.AVAILABLE
    assert start.reason_code == "SPRINT_READY_TO_START"


def test_story_change_makes_reviewed_sprint_plan_stale() -> None:
    original = _story(1, "req-a")
    sprint_plan = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "sprint_plan",
            "artifact_id": 501,
            "artifact_fingerprint": "sha256:plan",
            "source_fingerprint": ROADMAP_FINGERPRINT,
            "requirement_id": None,
            "story_ids": [1],
            "candidate_set_fingerprint": candidate_set_fingerprint((original,), ()),
            "supersedes_artifact_id": None,
            "status": "accepted",
        }
    )
    changed = original.model_copy(update={"story_points": 5})
    snapshot = _snapshot(
        requirements=_requirements("req-a"),
        planning_artifacts=(_roadmap(), _story_artifact(1, "req-a"), sprint_plan),
        stories=(changed,),
        decisions=(_decision("sprint", artifact_id=501, fingerprint="sha256:plan"),),
    )
    start = _node(snapshot, "planning.sprint.start")
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "SPRINT_PLAN_STALE"
