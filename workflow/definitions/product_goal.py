"""Pure Product Goal selectors and isolated graph rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import FactReference, InputField, RecommendationKind
from workflow.definitions.vision import select_vision_interview_state
from workflow.graph import AgenticExecutionSpec, NodeSpec, RuleCategory, RuleEvaluation

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from workflow.facts import (
        ProductGoalArtifactDecisionFact,
        ProductGoalArtifactFact,
        ProductGoalInterviewTurnFact,
        VisionArtifactFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class ProductGoalInterviewState:
    """Current Goal review context and active interview chain."""

    vision: VisionArtifactFact | None
    active: ProductGoalArtifactFact | None
    candidate: ProductGoalArtifactFact | None
    decision: ProductGoalArtifactDecisionFact | None
    transcript: tuple[ProductGoalInterviewTurnFact, ...]
    conflict: bool


@dataclass(frozen=True)
class _GoalLineage:
    """Validated Goal artifacts, child links, and exact decisions."""

    goals: tuple[ProductGoalArtifactFact, ...]
    child_ids: frozenset[int]
    decisions: dict[int, ProductGoalArtifactDecisionFact]


@dataclass(frozen=True)
class _GoalCurrentSelection:
    """Selected active Goal or interview identity under one accepted Vision."""

    active: ProductGoalArtifactFact | None
    candidate: ProductGoalArtifactFact | None
    decision: ProductGoalArtifactDecisionFact | None
    goal_number: int | None
    revision_number: int | None
    candidate_source_turn_id: int | None


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
    state = select_product_goal_interview_state(snapshot)
    if state.conflict:
        return None
    return state.active


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
        sprint.sprint_id for sprint in snapshot.sprints if sprint.status == "completed"
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


def _goal_turn_chain(
    turns: tuple[ProductGoalInterviewTurnFact, ...],
    *,
    leaf_id: int | None = None,
) -> tuple[ProductGoalInterviewTurnFact, ...] | None:
    """Return one exact chronological Goal interview chain."""
    if not turns:
        return ()
    by_id = {item.product_goal_interview_turn_id: item for item in turns}
    prior_ids = {item.prior_turn_id for item in turns if item.prior_turn_id is not None}
    leaves = [
        item for item in turns if item.product_goal_interview_turn_id not in prior_ids
    ]
    if (
        len(by_id) != len(turns)
        or len(leaves) != 1
        or (leaf_id is not None and leaves[0].product_goal_interview_turn_id != leaf_id)
    ):
        return None
    leaf = leaves[0]
    reversed_chain: list[ProductGoalInterviewTurnFact] = []
    visited: set[int] = set()
    current: ProductGoalInterviewTurnFact | None = leaf
    missing_parent = False
    while current is not None and current.product_goal_interview_turn_id not in visited:
        identifier = current.product_goal_interview_turn_id
        visited.add(identifier)
        reversed_chain.append(current)
        prior_id = current.prior_turn_id
        missing_parent = prior_id is not None and prior_id not in by_id
        current = None if prior_id is None else by_id.get(prior_id)
        if missing_parent:
            break
    if missing_parent or current is not None or len(visited) != len(turns):
        return None
    return tuple(reversed(reversed_chain))


def _goal_lineage(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
) -> _GoalLineage | None:
    """Validate Goal supersession and decision lineage under one Vision."""
    goals = tuple(
        item
        for item in snapshot.product_goal_artifacts
        if (item.vision_artifact_id, item.vision_fingerprint)
        == (vision.vision_artifact_id, vision.content_fingerprint)
    )
    by_id = {item.product_goal_artifact_id: item for item in goals}
    if len(by_id) != len(goals):
        return None
    children: dict[int, int] = {}
    for item in goals:
        parent_id = item.supersedes_product_goal_artifact_id
        if parent_id is None:
            if item.revision_number != 1:
                return None
            continue
        parent = by_id.get(parent_id)
        if (
            parent is None
            or parent.goal_number != item.goal_number
            or parent.revision_number + 1 != item.revision_number
            or parent_id in children
        ):
            return None
        children[parent_id] = item.product_goal_artifact_id
    decisions_by_goal: dict[int, ProductGoalArtifactDecisionFact] = {}
    for item in snapshot.product_goal_artifact_decisions:
        goal = by_id.get(item.product_goal_artifact_id)
        if goal is None:
            continue
        if (
            item.product_goal_artifact_id in decisions_by_goal
            or item.artifact_fingerprint != goal.content_fingerprint
        ):
            return None
        decisions_by_goal[item.product_goal_artifact_id] = item
    return _GoalLineage(goals, frozenset(children), decisions_by_goal)


def _current_goal_selection(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
    lineage: _GoalLineage,
) -> _GoalCurrentSelection | None:
    """Choose graph-current Goal review context and interview identity."""
    accepted = _unresolved_accepted_goals(snapshot, vision)
    pending = _pending_goals(snapshot, vision)
    if len(accepted) + len(pending) > 1:
        return None
    active = accepted[0] if accepted else None
    if active is not None:
        return (
            None
            if active.product_goal_artifact_id in lineage.child_ids
            else _GoalCurrentSelection(
                active,
                None,
                lineage.decisions.get(active.product_goal_artifact_id),
                None,
                None,
                None,
            )
        )
    if pending:
        candidate = pending[0]
        return _GoalCurrentSelection(
            None,
            candidate,
            None,
            candidate.goal_number,
            candidate.revision_number,
            candidate.source_interview_turn_id,
        )
    reviewed = tuple(
        item
        for item in lineage.goals
        if item.product_goal_artifact_id not in lineage.child_ids
        and (selected := lineage.decisions.get(item.product_goal_artifact_id))
        is not None
        and selected.decision in {"feedback", "rejected"}
    )
    if len(reviewed) > 1:
        return None
    if reviewed:
        candidate = reviewed[0]
        return _GoalCurrentSelection(
            None,
            candidate,
            lineage.decisions[candidate.product_goal_artifact_id],
            candidate.goal_number,
            candidate.revision_number + 1,
            None,
        )
    return _GoalCurrentSelection(
        None,
        None,
        None,
        max((item.goal_number for item in lineage.goals), default=0) + 1,
        1,
        None,
    )


def _goal_state_under_vision(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
) -> ProductGoalInterviewState:
    """Select current Goal state after accepted Vision selection."""
    lineage = _goal_lineage(snapshot, vision)
    current = (
        None if lineage is None else _current_goal_selection(snapshot, vision, lineage)
    )
    if lineage is None or current is None:
        return ProductGoalInterviewState(vision, None, None, None, (), True)
    if current.active is not None:
        return ProductGoalInterviewState(
            vision,
            current.active,
            None,
            current.decision,
            (),
            False,
        )
    if current.goal_number is None or current.revision_number is None:
        return ProductGoalInterviewState(vision, None, None, None, (), True)
    selected_identity = (current.goal_number, current.revision_number)
    artifact_identities = {
        (item.goal_number, item.revision_number) for item in lineage.goals
    }
    unmaterialized_identities = {
        (item.goal_number, item.revision_number)
        for item in snapshot.product_goal_interview_turns
        if (item.vision_artifact_id, item.vision_fingerprint)
        == (vision.vision_artifact_id, vision.content_fingerprint)
        and (item.goal_number, item.revision_number) not in artifact_identities
    }
    if unmaterialized_identities - {selected_identity}:
        return ProductGoalInterviewState(vision, None, None, None, (), True)
    turns = tuple(
        item
        for item in snapshot.product_goal_interview_turns
        if (
            item.vision_artifact_id,
            item.vision_fingerprint,
            item.goal_number,
            item.revision_number,
        )
        == (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            current.goal_number,
            current.revision_number,
        )
    )
    transcript = _goal_turn_chain(
        turns,
        leaf_id=current.candidate_source_turn_id,
    )
    artifact_source_ids = {
        item.source_interview_turn_id for item in snapshot.product_goal_artifacts
    }
    if transcript is None or any(
        item.is_complete
        and item.product_goal_interview_turn_id not in artifact_source_ids
        for item in transcript
    ):
        return ProductGoalInterviewState(vision, None, None, None, (), True)
    return ProductGoalInterviewState(
        vision,
        None,
        current.candidate,
        current.decision,
        transcript,
        False,
    )


def select_product_goal_interview_state(
    snapshot: WorkflowFactSnapshot,
) -> ProductGoalInterviewState:
    """Select Goal review and interview facts using graph lineage only."""
    vision_state = select_vision_interview_state(snapshot)
    if vision_state.conflict:
        return ProductGoalInterviewState(None, None, None, None, (), True)
    vision = accepted_current_vision(snapshot)
    if vision is None:
        return ProductGoalInterviewState(None, None, None, None, (), False)
    return _goal_state_under_vision(snapshot, vision)


def _goal_interview_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = select_product_goal_interview_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    vision = state.vision
    if vision is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_VISION_NOT_ACCEPTED",
            ),
        )
    if state.active is not None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_INTERVIEW_NOT_READY",
            ),
        )
    if state.candidate is not None and state.decision is None:
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
    state = select_product_goal_interview_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    vision = state.vision
    if vision is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_REVIEW_NOT_READY",
            ),
        )
    if state.candidate is None or state.decision is not None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "PRODUCT_GOAL_REVIEW_NOT_PENDING",
            ),
        )
    goal = state.candidate
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


__all__ = [
    "PRODUCT_GOAL_NODES",
    "ProductGoalInterviewState",
    "accepted_current_goal",
    "accepted_current_vision",
    "lifecycle_is_quiescent",
    "select_product_goal_interview_state",
]
