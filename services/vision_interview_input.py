"""Host preparation for one isolated Project Vision interview turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session

from repositories.workflow import (
    VisionInputFactRepository,
    WorkflowFactLoadError,
    select_vision_interview_input,
)
from services.contracts.vision import VisionComponents, VisionInterviewInput
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    DurableTransitionReplayService,
    NodeAttemptReplayQuery,
    TransitionReplayQuery,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, NodeDecision, TransitionResult


@dataclass(frozen=True)
class VisionInterviewInputService:
    """Build only human-intent context from durable Product Vision facts."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Recover a persisted attempt before deriving current interview state."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        """Recover a completed Vision lifecycle command before state evaluation."""
        return DurableTransitionReplayService(engine=self.engine).replay(query)

    def build(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject:
        """Prepare exact Project identity and valid prior Vision context only."""
        if decision.node_id != "vision.interview":
            message = "Vision input requires the vision.interview decision."
            raise ValueError(message)
        try:
            with Session(self.engine) as session:
                facts = VisionInputFactRepository(session)
                context = facts.load_context(project_id)
                selection = select_vision_interview_input(context)
                if (
                    selection.mode == "revision"
                    and facts.has_active_product_goal(context)
                ):
                    message = (
                        "Vision revision is blocked while a Product Goal is active."
                    )
                    raise ValueError(message)
        except WorkflowFactLoadError as error:
            raise ValueError(str(error)) from error
        payload = VisionInterviewInput(
            project_name=context.project.name,
            project_description=context.project_description,
            mode=selection.mode,
            user_response=user_text,
            prior_components=(
                None
                if selection.prior_turn is None
                else VisionComponents.model_validate(selection.prior_turn.components)
            ),
            accepted_vision_statement=selection.accepted_vision_statement,
        )
        return payload.model_dump(mode="json")


__all__ = ["VisionInterviewInputService"]
