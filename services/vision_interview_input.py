"""Host preparation for one isolated Project Vision interview turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session

from models.core import Project
from repositories.workflow import WorkflowFactRepository
from services.contracts.vision import VisionComponents, VisionInterviewInput
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from workflow.definitions.vision import _active_goal_exists, _isolated_vision_state

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

    def build(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject:
        """Prepare exact project identity and valid prior Vision context only."""
        if decision.node_id != "vision.interview":
            message = "Vision input requires the vision.interview decision."
            raise ValueError(message)
        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                message = "Project does not exist."
                raise ValueError(message)
            snapshot = WorkflowFactRepository(session).load(project_id)
        state = _isolated_vision_state(snapshot)
        if state.conflict:
            message = "Vision facts are ambiguous."
            raise ValueError(message)
        if state.open_revision is not None:
            mode = "revision"
            accepted_statement = state.open_revision.source_vision_fingerprint
            source = next(
                item
                for item in snapshot.vision_artifacts
                if item.vision_artifact_id
                == state.open_revision.source_vision_artifact_id
            )
            accepted_statement = source.statement
            intent_id = state.open_revision.vision_revision_intent_id
        else:
            mode = "initial"
            accepted_statement = None
            intent_id = None
            if (
                state.artifact is not None
                and state.decision is not None
                and state.decision.decision == "accepted"
            ):
                message = "Accepted Vision requires an explicit revision intent."
                raise ValueError(message)
        if mode == "revision" and _active_goal_exists(snapshot):
            message = "Vision revision is blocked while a Product Goal is active."
            raise ValueError(message)
        turns = tuple(
            item
            for item in snapshot.vision_interview_turns
            if item.mode == mode and item.revision_intent_id == intent_id
        )
        leaves = {
            item.vision_interview_turn_id
            for item in turns
            if item.vision_interview_turn_id
            not in {
                turn.prior_turn_id for turn in turns if turn.prior_turn_id is not None
            }
        }
        if len(leaves) > 1:
            message = "Vision interview turn chain is ambiguous."
            raise ValueError(message)
        prior = next(
            (item for item in turns if item.vision_interview_turn_id in leaves), None
        )
        payload = VisionInterviewInput(
            project_name=project.name,
            project_description=project.description,
            mode=mode,
            user_response=user_text,
            prior_components=(
                None
                if prior is None
                else VisionComponents.model_validate(prior.components)
            ),
            accepted_vision_statement=accepted_statement,
        )
        return payload.model_dump(mode="json")


__all__ = ["VisionInterviewInputService"]
