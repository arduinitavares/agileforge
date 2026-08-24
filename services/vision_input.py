"""Host preparation for context-grounded Vision generation input."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlmodel import Session

from repositories.workflow import (
    VisionInputFactRepository,
    WorkflowFactLoadError,
    select_vision_input,
)
from services.contracts.vision import (
    VisionAgentInput,
    VisionAssumption,
    VisionBootstrapInput,
    VisionClarificationInput,
    VisionClarifyingQuestion,
    VisionComponentBasis,
    VisionComponents,
    VisionConflict,
    VisionHostMetadata,
    VisionPreflight,
    VisionRevisionInput,
)
from services.contracts.vision_evidence import VisionEvidenceBundle
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    DurableTransitionReplayService,
    NodeAttemptReplayQuery,
    TransitionReplayQuery,
)
from services.vision_evidence import VisionEvidenceCollection, VisionEvidenceCollector
from workflow.contracts import RecommendationKind

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbe
    from workflow.contracts import JsonObject, NodeDecision, TransitionResult


@dataclass(frozen=True)
class VisionInputService:
    """Build trusted Vision model input from durable facts and fresh evidence."""

    engine: Engine
    repository_probe: RepositoryProbe

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Recover a persisted attempt before deriving current Vision state."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        """Recover a completed Vision lifecycle command before state evaluation."""
        return DurableTransitionReplayService(engine=self.engine).replay(query)

    def build_bootstrap(self, project_id: int, decision: NodeDecision) -> JsonObject:
        """Collect evidence and prepare bootstrap or revision generation input."""
        if decision.node_id != "vision.bootstrap":
            message = "Vision bootstrap input requires the vision.bootstrap decision."
            raise ValueError(message)
        try:
            with Session(self.engine) as session:
                facts = VisionInputFactRepository(session)
                context = facts.load_context(project_id)
                selection = select_vision_input(context)
                replacement_recovery = (
                    selection.prior_turn is not None
                    and decision.recommendation_kind is RecommendationKind.RECOVERY
                )
                if selection.prior_turn is not None and not replacement_recovery:
                    message = "Vision bootstrap is not current for this lineage."
                    raise ValueError(message)
                if replacement_recovery and selection.evidence_snapshot is None:
                    message = "Vision recovery requires an active stale snapshot."
                    raise ValueError(message)
                if (
                    selection.generation_operation == "revision"
                    and facts.has_active_product_goal(context)
                ):
                    message = (
                        "Vision revision is blocked while a Product Goal is active."
                    )
                    raise ValueError(message)
        except WorkflowFactLoadError as error:
            raise ValueError(str(error)) from error
        collection = self._collect(project_id)
        evidence = collection.bundle
        if selection.generation_operation == "revision":
            accepted = selection.accepted_vision
            revision = next(
                item
                for item in context.revision_intents
                if item.vision_revision_intent_id == selection.revision_intent_id
            )
            if accepted is None:
                message = "Vision revision input is missing the accepted Vision."
                raise ValueError(message)
            request = VisionRevisionInput(
                schema_version="agileforge.vision-input.v1",
                operation="revision",
                project_name=context.project.name,
                project_description=context.project_description,
                evidence=evidence,
                accepted_components=VisionComponents.model_validate(
                    accepted.components
                ),
                accepted_statement=accepted.statement,
                accepted_vision_fingerprint=accepted.content_fingerprint,
                revision_reason=revision.reason,
                active_product_goal_status="none",
                prior_review_feedback=None,
            )
        else:
            request = VisionBootstrapInput(
                schema_version="agileforge.vision-input.v1",
                operation="bootstrap",
                project_name=context.project.name,
                project_description=context.project_description,
                evidence=evidence,
            )
        supersedes_snapshot_id = (
            None
            if selection.evidence_snapshot is None
            else selection.evidence_snapshot.vision_evidence_snapshot_id
        )
        return VisionAgentInput(
            request=request,
            host=VisionHostMetadata(
                repository_binding_id=collection.repository_binding_id,
                supersedes_vision_evidence_snapshot_id=(
                    supersedes_snapshot_id if replacement_recovery else None
                ),
            ),
        ).model_dump(mode="json")

    def build_clarification(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject:
        """Prepare clarification from the current draft and stored snapshot."""
        if decision.node_id != "vision.interview":
            message = (
                "Vision clarification input requires the vision.interview "
                "decision."
            )
            raise ValueError(message)
        try:
            with Session(self.engine) as session:
                context = VisionInputFactRepository(session).load_context(project_id)
                selection = select_vision_input(context)
        except WorkflowFactLoadError as error:
            raise ValueError(str(error)) from error
        prior = selection.prior_turn
        snapshot = selection.evidence_snapshot
        if prior is None or snapshot is None:
            message = "Vision clarification requires an existing draft snapshot."
            raise ValueError(message)
        evidence = VisionEvidenceBundle.model_validate(snapshot.evidence)
        current_questions = prior.clarifying_questions
        question_ids = tuple(
            str(question["question_id"])
            for question in current_questions
            if isinstance(question.get("question_id"), str)
        )
        request = VisionClarificationInput(
            schema_version="agileforge.vision-input.v1",
            operation="clarification",
            project_name=context.project.name,
            project_description=context.project_description,
            vision_evidence_snapshot_id=snapshot.vision_evidence_snapshot_id,
            evidence=evidence,
            current_components=VisionComponents.model_validate(prior.components),
            current_statement=prior.vision_statement,
            current_component_basis=tuple(
                VisionComponentBasis.model_validate(item)
                for item in prior.component_basis
            ),
            current_assumptions=tuple(
                VisionAssumption.model_validate(item) for item in prior.assumptions
            ),
            current_conflicts=tuple(
                VisionConflict.model_validate(item) for item in prior.conflicts
            ),
            current_questions=tuple(
                VisionClarifyingQuestion.model_validate(item)
                for item in current_questions
            ),
            human_response=user_text,
            addressed_question_ids=question_ids,
        )
        observed = self._collect(project_id).bundle
        return VisionAgentInput(
            request=request,
            preflight=VisionPreflight(
                expected_evidence_fingerprint=evidence.evidence_fingerprint,
                observed_evidence=observed,
            ),
        ).model_dump(mode="json")

    def _collect(self, project_id: int) -> VisionEvidenceCollection:
        """Collect and validate current bounded evidence."""
        try:
            return VisionEvidenceCollector(
                engine=self.engine,
                repository_probe=self.repository_probe,
            ).collect_with_provenance(project_id)
        except (ValidationError, json.JSONDecodeError) as error:
            raise ValueError(str(error)) from error


__all__ = ["VisionInputService"]
