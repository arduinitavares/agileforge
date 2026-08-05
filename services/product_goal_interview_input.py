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
            snapshot = WorkflowFactRepository(session).load(project_id)
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
        turns = [
            item
            for item in snapshot.product_goal_interview_turns
            if item.vision_artifact_id == vision.vision_artifact_id
            and item.vision_fingerprint == vision.content_fingerprint
        ]
        prior = None
        if turns:
            latest = max(turns, key=lambda item: item.product_goal_interview_turn_id)
            prior = ProductGoalComponents.model_validate(latest.components)
        return ProductGoalInterviewInput(
            project_name=snapshot.project.name,
            accepted_vision_statement=vision.statement,
            user_response=user_text,
            prior_components=prior,
        ).model_dump(mode="json")
