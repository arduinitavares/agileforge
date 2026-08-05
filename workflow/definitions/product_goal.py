"""Pure Product Goal selectors and isolated graph rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflow.contracts import FactReference, InputField, RecommendationKind
from workflow.graph import AgenticExecutionSpec, NodeSpec, RuleCategory, RuleEvaluation

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from workflow.facts import (
        ProductGoalArtifactFact,
        VisionArtifactFact,
        WorkflowFactSnapshot,
    )


def _reference(kind: str, identifier: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=kind,
        fact_id=str(identifier),
        fingerprint=fingerprint,
    )


def accepted_current_vision(
    snapshot: WorkflowFactSnapshot,
) -> VisionArtifactFact | None:
    """Return the sole accepted leaf Vision or no selection on any conflict."""
    superseded = {
        artifact.supersedes_vision_artifact_id
        for artifact in snapshot.vision_artifacts
        if artifact.supersedes_vision_artifact_id is not None
    }
    leaves = [
        artifact
        for artifact in snapshot.vision_artifacts
        if artifact.vision_artifact_id not in superseded
    ]
    if len(leaves) != 1:
        return None
    vision = leaves[0]
    decisions = [
        decision
        for decision in snapshot.vision_artifact_decisions
        if decision.vision_artifact_id == vision.vision_artifact_id
    ]
    if len(decisions) != 1:
        return None
    decision = decisions[0]
    if (
        decision.artifact_fingerprint != vision.content_fingerprint
        or decision.decision != "accepted"
    ):
        return None
    return vision


def accepted_current_goal(
    snapshot: WorkflowFactSnapshot,
) -> ProductGoalArtifactFact | None:
    """Return the sole unresolved accepted Goal under the accepted Vision."""
    vision = accepted_current_vision(snapshot)
    if vision is None:
        return None
    outcomes = {
        outcome.product_goal_artifact_id for outcome in snapshot.product_goal_outcomes
    }
    accepted: list[ProductGoalArtifactFact] = []
    for goal in snapshot.product_goal_artifacts:
        decisions = [
            decision
            for decision in snapshot.product_goal_artifact_decisions
            if decision.product_goal_artifact_id == goal.product_goal_artifact_id
        ]
        if (
            goal.vision_artifact_id == vision.vision_artifact_id
            and goal.vision_fingerprint == vision.content_fingerprint
            and goal.product_goal_artifact_id not in outcomes
            and len(decisions) == 1
            and decisions[0].decision == "accepted"
            and decisions[0].artifact_fingerprint == goal.content_fingerprint
        ):
            accepted.append(goal)
    return accepted[0] if len(accepted) == 1 else None


def _pending_goal(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
) -> ProductGoalArtifactFact | None:
    pending = []
    for goal in snapshot.product_goal_artifacts:
        decisions = [
            decision
            for decision in snapshot.product_goal_artifact_decisions
            if decision.product_goal_artifact_id == goal.product_goal_artifact_id
        ]
        if (
            goal.vision_artifact_id == vision.vision_artifact_id
            and goal.vision_fingerprint == vision.content_fingerprint
            and not decisions
        ):
            pending.append(goal)
    return pending[0] if len(pending) == 1 else None


def _goal_interview_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    vision = accepted_current_vision(snapshot)
    if vision is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_VISION_NOT_ACCEPTED",
            ),
        )
    if accepted_current_goal(snapshot) is not None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_INTERVIEW_NOT_READY",
            ),
        )
    if _pending_goal(snapshot, vision) is not None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_REVIEW_PENDING",
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "PRODUCT_GOAL_INTERVIEW_REQUIRED",
            fact_references=(
                _reference(
                    "vision",
                    vision.vision_artifact_id,
                    vision.content_fingerprint,
                ),
            ),
        ),
    )


def _goal_review_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    vision = accepted_current_vision(snapshot)
    if vision is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_REVIEW_NOT_READY",
            ),
        )
    goal = _pending_goal(snapshot, vision)
    if goal is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_REVIEW_NOT_PENDING",
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "PRODUCT_GOAL_REVIEW_REQUIRED",
            fact_references=(
                _reference(
                    "vision",
                    vision.vision_artifact_id,
                    vision.content_fingerprint,
                ),
                _reference(
                    "product_goal",
                    goal.product_goal_artifact_id,
                    goal.content_fingerprint,
                ),
            ),
        ),
    )


def _outcome_rule(
    kind: str,
) -> Callable[[WorkflowFactSnapshot, datetime], tuple[RuleEvaluation, ...]]:
    def evaluate(
        snapshot: WorkflowFactSnapshot,
        _at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        goal = accepted_current_goal(snapshot)
        active_sprint = any(sprint.status == "active" for sprint in snapshot.sprints)
        completed_sprint_ids = {
            sprint.sprint_id
            for sprint in snapshot.sprints
            if sprint.status == "completed"
        }
        triaged_sprint_ids = {
            triage.sprint_id for triage in snapshot.post_sprint_triage
        }
        triage_complete = completed_sprint_ids <= triaged_sprint_ids
        if goal is None or active_sprint or not triage_complete:
            return (
                RuleEvaluation(
                    RuleCategory.SATISFIED,
                    "PRODUCT_GOAL_OUTCOME_NOT_READY",
                ),
            )
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                f"PRODUCT_GOAL_{kind.upper()}_AVAILABLE",
                recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                fact_references=(
                    _reference(
                        "product_goal",
                        goal.product_goal_artifact_id,
                        goal.content_fingerprint,
                    ),
                ),
            ),
        )

    return evaluate


PRODUCT_GOAL_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="goal.interview",
        child_graph_id="product_goal",
        request_kind="record_product_goal_interview_turn",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(InputField(name="user_text", value_type="string"),),
        evaluate_rule=_goal_interview_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="PRODUCT_GOAL_INTERVIEW_ACTIVE",
            failure_reason="PRODUCT_GOAL_INTERVIEW_FAILED",
            recovery_reason="PRODUCT_GOAL_INTERVIEW_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="goal.review",
        child_graph_id="product_goal",
        request_kind="decide_product_goal_review",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_goal_review_rule,
    ),
    NodeSpec(
        node_id="goal.fulfill",
        child_graph_id="product_goal",
        request_kind="fulfill_product_goal",
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        required_inputs=(InputField(name="rationale", value_type="string"),),
        evaluate_rule=_outcome_rule("fulfilled"),
    ),
    NodeSpec(
        node_id="goal.abandon",
        child_graph_id="product_goal",
        request_kind="abandon_product_goal",
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        required_inputs=(InputField(name="rationale", value_type="string"),),
        evaluate_rule=_outcome_rule("abandoned"),
    ),
)
