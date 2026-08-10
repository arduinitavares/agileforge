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
    accepted = _unresolved_accepted_goals(snapshot, vision)
    return accepted[0] if len(accepted) == 1 else None


def _unresolved_accepted_goals(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
) -> tuple[ProductGoalArtifactFact, ...]:
    """Return every unresolved accepted Goal under one accepted Vision."""
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
    return tuple(accepted)


def lifecycle_is_quiescent(snapshot: WorkflowFactSnapshot) -> bool:
    """Return whether delivery has no active work or unresolved review."""
    if any(sprint.status == "active" for sprint in snapshot.sprints):
        return False
    completed_sprint_ids = {
        sprint.sprint_id
        for sprint in snapshot.sprints
        if sprint.status == "completed"
    }
    triaged_sprint_ids = {triage.sprint_id for triage in snapshot.post_sprint_triage}
    if not completed_sprint_ids <= triaged_sprint_ids:
        return False
    if any(
        artifact.status == "pending_review"
        for artifact in (*snapshot.phase_artifacts, *snapshot.planning_artifacts)
    ):
        return False
    if any(authority.status == "pending_review" for authority in snapshot.authorities):
        return False
    reviewed_candidate_ids = {
        decision.specification_candidate_id
        for decision in snapshot.specification_decisions
    }
    return all(
        candidate.specification_candidate_id in reviewed_candidate_ids
        for candidate in snapshot.specification_candidates
    )


def _pending_goal(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
) -> ProductGoalArtifactFact | None:
    """Return the sole pending Goal under the accepted Vision."""
    pending = _pending_goals(snapshot, vision)
    return pending[0] if len(pending) == 1 else None


def _pending_goals(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
) -> tuple[ProductGoalArtifactFact, ...]:
    """Return every pending Goal under one accepted Vision."""
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
    return tuple(pending)


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
    accepted_goals = _unresolved_accepted_goals(snapshot, vision)
    pending_goals = _pending_goals(snapshot, vision)
    if len(accepted_goals) > 1 or len(pending_goals) > 1:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if accepted_goals:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_INTERVIEW_NOT_READY",
            ),
        )
    if pending_goals:
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
    accepted_goals = _unresolved_accepted_goals(snapshot, vision)
    pending_goals = _pending_goals(snapshot, vision)
    if len(accepted_goals) > 1 or len(pending_goals) > 1:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if len(pending_goals) != 1:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_REVIEW_NOT_PENDING",
            ),
        )
    goal = pending_goals[0]
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
        if goal is None or not lifecycle_is_quiescent(snapshot):
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
