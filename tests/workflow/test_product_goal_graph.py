"""Provider-free graph tests for Product Goal lifecycle rules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from workflow.contracts import NodeCategory
from workflow.definitions.product_goal import (
    _goal_interview_rule,
    _goal_review_rule,
    _outcome_rule,
    accepted_current_goal,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.facts import (
    PhaseArtifactFact,
    PostSprintTriageFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    SprintFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)
from workflow.graph import RuleCategory

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
        vision_evidence_snapshot_id=1,
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


def test_multiple_unresolved_accepted_goals_invalidate_goal_selection() -> None:
    """Contradictory accepted Goals never advertise another interview."""
    snapshot = _snapshot(goal_decision="accepted")
    goal = snapshot.product_goal_artifacts[0]
    decision = snapshot.product_goal_artifact_decisions[0]
    conflicting_goal = goal.model_copy(
        update={
            "product_goal_artifact_id": 22,
            "goal_number": 2,
            "content_fingerprint": "goal-fingerprint-2",
        }
    )
    conflicting_decision = decision.model_copy(
        update={
            "product_goal_artifact_decision_id": 23,
            "product_goal_artifact_id": 22,
            "artifact_fingerprint": "goal-fingerprint-2",
            "idempotency_key": "goal-review-2",
        }
    )
    conflicted = snapshot.model_copy(
        update={
            "product_goal_artifacts": (goal, conflicting_goal),
            "product_goal_artifact_decisions": (decision, conflicting_decision),
        }
    )

    interview = _goal_interview_rule(conflicted, NOW)[0]
    review = _goal_review_rule(conflicted, NOW)[0]

    assert accepted_current_goal(conflicted) is None
    assert interview.category is RuleCategory.INVALID
    assert interview.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert review.category is RuleCategory.INVALID
    assert review.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_multiple_pending_goals_invalidate_goal_selection() -> None:
    """Ambiguous review candidates never advertise another interview."""
    snapshot = _snapshot()
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
    conflicting_goal = goal.model_copy(
        update={
            "product_goal_artifact_id": 22,
            "goal_number": 2,
            "content_fingerprint": "goal-fingerprint-2",
        }
    )
    conflicted = snapshot.model_copy(
        update={"product_goal_artifacts": (goal, conflicting_goal)}
    )

    interview = _goal_interview_rule(conflicted, NOW)[0]
    review = _goal_review_rule(conflicted, NOW)[0]

    assert interview.category is RuleCategory.INVALID
    assert interview.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert review.category is RuleCategory.INVALID
    assert review.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_mixed_goal_lineage_advertises_no_goal_or_downstream_action() -> None:
    """Conflicting single-active Goal lineage blocks every Goal-dependent action."""
    snapshot = _snapshot(goal_decision="accepted")
    accepted_goal = snapshot.product_goal_artifacts[0]
    pending_goal = accepted_goal.model_copy(
        update={
            "product_goal_artifact_id": 22,
            "goal_number": 2,
            "content_fingerprint": "goal-fingerprint-2",
        }
    )
    conflicted = snapshot.model_copy(
        update={"product_goal_artifacts": (accepted_goal, pending_goal)}
    )

    position = ROOT_GRAPH.evaluate(conflicted, NOW)
    decisions = {item.node_id: item for item in position.decisions}
    advertised = set(position.available_nodes)

    assert decisions["goal.interview"].category is NodeCategory.INVALID
    assert decisions["goal.interview"].reason_code == "WORKFLOW_FACT_CONFLICT"
    assert decisions["goal.review"].category is NodeCategory.INVALID
    assert decisions["goal.review"].reason_code == "WORKFLOW_FACT_CONFLICT"
    assert accepted_current_goal(conflicted) is None
    assert {node_id.split(".", maxsplit=1)[0] for node_id in advertised}.isdisjoint(
        {
            "goal",
            "discovery",
            "specification",
            "authority",
            "backlog",
            "roadmap",
            "story",
            "sprint",
            "task",
        }
    )


def test_feedback_reopens_goal_interview_without_an_active_goal() -> None:
    """Feedback is terminal for its candidate but permits its exact replacement."""
    snapshot = _snapshot(goal_decision="feedback")

    assert accepted_current_goal(snapshot) is None
    assert (
        _goal_interview_rule(snapshot, NOW)[0].reason_code
        == "PRODUCT_GOAL_INTERVIEW_REQUIRED"
    )


def test_goal_outcome_requires_triage_for_every_completed_sprint() -> None:
    """One triaged closure cannot make another completed Sprint quiescent."""
    snapshot = _snapshot(goal_decision="accepted").model_copy(
        update={
            "sprints": (
                SprintFact(sprint_id=1, status="completed", completed_at=NOW),
                SprintFact(sprint_id=2, status="completed", completed_at=NOW),
            ),
            "post_sprint_triage": (
                PostSprintTriageFact(
                    triage_id=1,
                    sprint_id=1,
                    impact="none",
                    canonical_payload={},
                    payload_fingerprint="triage-1",
                ),
            ),
        }
    )

    assert _outcome_rule("fulfilled")(snapshot, NOW)[0].reason_code == (
        "PRODUCT_GOAL_OUTCOME_NOT_READY"
    )
    complete = snapshot.model_copy(
        update={
            "post_sprint_triage": (
                *snapshot.post_sprint_triage,
                PostSprintTriageFact(
                    triage_id=2,
                    sprint_id=2,
                    impact="none",
                    canonical_payload={},
                    payload_fingerprint="triage-2",
                ),
            )
        }
    )
    assert _outcome_rule("fulfilled")(complete, NOW)[0].reason_code == (
        "PRODUCT_GOAL_FULFILLED_AVAILABLE"
    )


def test_goal_outcome_is_blocked_by_active_sprint_but_not_no_sprints() -> None:
    """Quiescence permits a Goal without Sprint history and blocks active work."""
    quiescent = _snapshot(goal_decision="accepted")
    assert _outcome_rule("abandoned")(quiescent, NOW)[0].reason_code == (
        "PRODUCT_GOAL_ABANDONED_AVAILABLE"
    )
    active = quiescent.model_copy(
        update={
            "sprints": (SprintFact(sprint_id=1, status="active", completed_at=None),)
        }
    )
    assert _outcome_rule("abandoned")(active, NOW)[0].reason_code == (
        "PRODUCT_GOAL_OUTCOME_NOT_READY"
    )


def test_goal_outcome_requires_every_artifact_review_to_be_quiescent() -> None:
    """An unresolved delivery review blocks fulfillment and abandonment."""
    pending_backlog = PhaseArtifactFact(
        artifact_type="backlog",
        artifact_id=30,
        artifact_fingerprint="backlog-fingerprint",
        spec_version_id=40,
        spec_hash="specification-fingerprint",
        product_goal_artifact_id=20,
        product_goal_fingerprint="goal-fingerprint",
        status="pending_review",
    )
    snapshot = _snapshot(goal_decision="accepted").model_copy(
        update={"phase_artifacts": (pending_backlog,)}
    )

    assert _outcome_rule("fulfilled")(snapshot, NOW)[0].reason_code == (
        "PRODUCT_GOAL_OUTCOME_NOT_READY"
    )
    assert _outcome_rule("abandoned")(snapshot, NOW)[0].reason_code == (
        "PRODUCT_GOAL_OUTCOME_NOT_READY"
    )
