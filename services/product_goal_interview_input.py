"""Host preparation for Product Goal interview attempts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session

from repositories.workflow import WorkflowFactRepository
from services.contracts.product_goal import (
    ProductGoalComponents,
    ProductGoalInterviewInput,
)
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    DurableTransitionReplayService,
    NodeAttemptReplayQuery,
    TransitionReplayQuery,
)
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, NodeDecision, TransitionResult
    from workflow.facts import (
        ProductGoalArtifactFact,
        VisionArtifactFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class ProductGoalInterviewInputService:
    """Build Goal agent input exclusively from durable Product Goal facts."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Replay before deriving current input."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def replay_transition(
        self, query: TransitionReplayQuery
    ) -> TransitionResult | None:
        """Replay host decisions before position reads."""
        return DurableTransitionReplayService(engine=self.engine).replay(query)

    def build(
        self, project_id: int, decision: NodeDecision, user_text: str
    ) -> JsonObject:
        """Bind the exact accepted Vision and latest valid Goal interview state."""
        if decision.node_id != "goal.interview":
            message = "Product Goal input requires goal.interview."
            raise ValueError(message)
        with Session(self.engine) as session:
            snapshot = WorkflowFactRepository(
                session
            ).load_product_goal_interview_snapshot(project_id)
        vision = accepted_current_vision(snapshot)
        if vision is None or accepted_current_goal(snapshot) is not None:
            message = "Product Goal interview facts are not eligible."
            raise ValueError(message)
        references = [
            item for item in decision.fact_references if item.fact_type == "vision"
        ]
        if len(references) != 1 or (
            references[0].fact_id,
            references[0].fingerprint,
        ) != (str(vision.vision_artifact_id), vision.content_fingerprint):
            message = "Product Goal interview Vision reference is stale."
            raise ValueError(message)
        feedback_or_rejected = _revision_candidate(snapshot, vision)
        if feedback_or_rejected is None:
            return ProductGoalInterviewInput(
                project_name=snapshot.project.name,
                accepted_vision_statement=vision.statement,
                user_response=user_text,
                prior_components=None,
            ).model_dump(mode="json")
        turns = [
            item
            for item in snapshot.product_goal_interview_turns
            if item.vision_artifact_id == vision.vision_artifact_id
            and item.vision_fingerprint == vision.content_fingerprint
            and item.goal_number == feedback_or_rejected.goal_number
        ]
        if not turns:
            message = "Product Goal revision has no interview chain."
            raise ValueError(message)
        latest = max(turns, key=lambda item: item.product_goal_interview_turn_id)
        prior = ProductGoalComponents.model_validate(latest.components)
        return ProductGoalInterviewInput(
            project_name=snapshot.project.name,
            accepted_vision_statement=vision.statement,
            user_response=user_text,
            prior_components=prior,
        ).model_dump(mode="json")


def _revision_candidate(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
) -> ProductGoalArtifactFact | None:
    """Return the sole leaf awaiting a feedback/rejection revision.

    Resolved Goals intentionally never contribute prior components to the next
    goal number. A terminal feedback/rejection only contributes within its
    exact unsuperseded Goal chain.
    """
    superseded = {
        goal.supersedes_product_goal_artifact_id
        for goal in snapshot.product_goal_artifacts
        if goal.supersedes_product_goal_artifact_id is not None
    }
    outcomes = {
        outcome.product_goal_artifact_id for outcome in snapshot.product_goal_outcomes
    }
    candidates = []
    for goal in snapshot.product_goal_artifacts:
        decisions = [
            decision
            for decision in snapshot.product_goal_artifact_decisions
            if decision.product_goal_artifact_id == goal.product_goal_artifact_id
        ]
        if (
            goal.product_goal_artifact_id not in superseded
            and goal.product_goal_artifact_id not in outcomes
            and (goal.vision_artifact_id, goal.vision_fingerprint)
            == (vision.vision_artifact_id, vision.content_fingerprint)
            and len(decisions) == 1
            and decisions[0].artifact_fingerprint == goal.content_fingerprint
            and decisions[0].decision in {"feedback", "rejected"}
        ):
            candidates.append(goal)
    if len(candidates) > 1:
        message = "Product Goal revision chain is ambiguous."
        raise ValueError(message)
    return candidates[0] if candidates else None
