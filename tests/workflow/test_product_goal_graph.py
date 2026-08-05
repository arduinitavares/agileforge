"""Provider-free graph tests for Product Goal lifecycle rules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from workflow.definitions.product_goal import (
    _goal_interview_rule,
    _goal_review_rule,
    accepted_current_goal,
)
from workflow.facts import (
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _snapshot(
    *,
    goal_decision: Literal["accepted", "rejected", "feedback"] | None = None,
) -> WorkflowFactSnapshot:
    """Build the smallest accepted-Vision chain for a Goal graph assertion."""
    vision = VisionArtifactFact(
        vision_artifact_id=10,
        version_number=1,
        components={},
        statement="Make delivery predictable.",
        content_fingerprint="vision-fingerprint",
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator",
        created_at=NOW,
    )
    decisions = (
        VisionArtifactDecisionFact(
            vision_artifact_decision_id=11,
            vision_artifact_id=10,
            artifact_fingerprint="vision-fingerprint",
            decision="accepted",
            rationale="ready",
            reviewer="operator",
            idempotency_key="vision-review",
            decided_at=NOW,
        ),
    )
    goal = ProductGoalArtifactFact(
        product_goal_artifact_id=20,
        vision_artifact_id=10,
        vision_fingerprint="vision-fingerprint",
        goal_number=1,
        revision_number=1,
        statement="Ship an approved specification.",
        content_fingerprint="goal-fingerprint",
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=2,
        created_by="operator",
        created_at=NOW,
    )
    goal_reviews = ()
    goals = ()
    if goal_decision is not None:
        goals = (goal,)
        goal_reviews = (
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=21,
                product_goal_artifact_id=20,
                artifact_fingerprint="goal-fingerprint",
                decision=goal_decision,
                rationale="needs revision" if goal_decision != "accepted" else "",
                reviewer="operator",
                idempotency_key="goal-review",
                decided_at=NOW,
            ),
        )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1,
            name="Goal graph",
            origin="greenfield",
            created_at=NOW,
        ),
        vision_artifacts=(vision,),
        vision_artifact_decisions=decisions,
        product_goal_artifacts=goals,
        product_goal_artifact_decisions=goal_reviews,
    )


def test_accepted_vision_exposes_goal_interview() -> None:
    """The isolated Goal graph starts only after exact Vision acceptance."""
    rule = _goal_interview_rule(_snapshot(), NOW)

    assert rule[0].reason_code == "PRODUCT_GOAL_INTERVIEW_REQUIRED"
    assert rule[0].fact_references[0].fact_id == "10"


def test_pending_goal_exposes_only_review() -> None:
    """A candidate without a terminal decision blocks another interview."""
    snapshot = _snapshot(goal_decision=None)
    goal = ProductGoalArtifactFact(
        product_goal_artifact_id=20,
        vision_artifact_id=10,
        vision_fingerprint="vision-fingerprint",
        goal_number=1,
        revision_number=1,
        statement="Ship an approved specification.",
        content_fingerprint="goal-fingerprint",
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=2,
        created_by="operator",
        created_at=NOW,
    )
    snapshot = snapshot.model_copy(update={"product_goal_artifacts": (goal,)})

    assert (
        _goal_interview_rule(snapshot, NOW)[0].reason_code
        == "PRODUCT_GOAL_REVIEW_PENDING"
    )
    review = _goal_review_rule(snapshot, NOW)[0]
    assert review.reason_code == "PRODUCT_GOAL_REVIEW_REQUIRED"
    assert review.fact_references[1].fact_id == "20"


def test_feedback_reopens_goal_interview_without_an_active_goal() -> None:
    """Feedback is terminal for its candidate but permits its exact replacement."""
    snapshot = _snapshot(goal_decision="feedback")

    assert accepted_current_goal(snapshot) is None
    assert (
        _goal_interview_rule(snapshot, NOW)[0].reason_code
        == "PRODUCT_GOAL_INTERVIEW_REQUIRED"
    )
