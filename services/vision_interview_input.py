"""Host preparation for one isolated Project Vision interview turn."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from sqlmodel import Session, col, select

from models.core import Project
from models.product_definition import (
    ProductGoalArtifactDecision,
    ProductGoalOutcome,
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from services.contracts.vision import VisionComponents, VisionInterviewInput
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    DurableTransitionReplayService,
    NodeAttemptReplayQuery,
    TransitionReplayQuery,
)
from workflow.definitions.vision import _active_goal_exists, _isolated_vision_state
from workflow.facts import (
    ProductGoalArtifactDecisionFact,
    ProductGoalOutcomeFact,
    ProjectFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    VisionInterviewTurnFact,
    VisionRevisionIntentFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, NodeDecision, TransitionResult


type _VisionMode = Literal["initial", "revision"]
type _ReviewDecision = Literal["accepted", "rejected", "feedback"]
type _GoalOutcome = Literal["fulfilled", "abandoned"]
type _ProjectOrigin = Literal["greenfield", "brownfield"]


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
        """Prepare exact project identity and valid prior Vision context only."""
        if decision.node_id != "vision.interview":
            message = "Vision input requires the vision.interview decision."
            raise ValueError(message)
        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                message = "Project does not exist."
                raise ValueError(message)
            snapshot = _VisionInputFactLoader(session).load(project)
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


class _VisionInputFactLoader:
    """Load only durable facts needed to prepare one Vision interview turn."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def load(self, project: Project) -> WorkflowFactSnapshot:
        project_id = _required_id(project.project_id, "Project")
        if project.origin not in {"greenfield", "brownfield"}:
            message = f"Project {project_id} has an invalid origin."
            raise ValueError(message)
        artifacts = self._artifacts(project_id)
        decisions = self._vision_decisions(project_id, artifacts)
        revisions = self._revisions(project_id, artifacts, decisions)
        turns = self._turns(project_id, revisions)
        return WorkflowFactSnapshot(
            project=ProjectFact(
                project_id=project_id,
                name=project.name,
                origin=cast("_ProjectOrigin", project.origin),
                created_at=project.created_at,
            ),
            vision_revision_intents=revisions,
            vision_interview_turns=turns,
            vision_artifacts=artifacts,
            vision_artifact_decisions=decisions,
            product_goal_artifact_decisions=self._goal_decisions(project_id),
            product_goal_outcomes=self._goal_outcomes(project_id),
        )

    def _artifacts(self, project_id: int) -> tuple[VisionArtifactFact, ...]:
        rows = self._session.exec(
            select(VisionArtifact)
            .where(col(VisionArtifact.project_id) == project_id)
            .order_by(col(VisionArtifact.vision_artifact_id))
        ).all()
        facts: list[VisionArtifactFact] = []
        known_ids: set[int] = set()
        superseded_ids: set[int] = set()
        for row in rows:
            identifier = _required_id(row.vision_artifact_id, "Vision artifact")
            components = _canonical_object(row.components_json, "Vision artifact")
            artifact_fingerprint = canonical_hash(
                {"components": components, "statement": row.statement}
            )
            if artifact_fingerprint != row.content_fingerprint:
                message = "Vision artifact fingerprint changed."
                raise ValueError(message)
            parent_id = row.supersedes_vision_artifact_id
            if parent_id is not None and (
                parent_id not in known_ids or parent_id in superseded_ids
            ):
                message = "Vision artifact supersession is invalid."
                raise ValueError(message)
            if parent_id is not None:
                superseded_ids.add(parent_id)
            known_ids.add(identifier)
            facts.append(
                VisionArtifactFact(
                    vision_artifact_id=identifier,
                    version_number=row.version_number,
                    components=components,
                    statement=row.statement,
                    content_fingerprint=row.content_fingerprint,
                    supersedes_vision_artifact_id=parent_id,
                    source_interview_turn_id=row.source_interview_turn_id,
                    created_by=row.created_by,
                    created_at=row.created_at,
                )
            )
        return tuple(facts)

    def _vision_decisions(
        self,
        project_id: int,
        artifacts: tuple[VisionArtifactFact, ...],
    ) -> tuple[VisionArtifactDecisionFact, ...]:
        rows = self._session.exec(
            select(VisionArtifactDecision)
            .where(col(VisionArtifactDecision.project_id) == project_id)
            .order_by(col(VisionArtifactDecision.vision_artifact_decision_id))
        ).all()
        fingerprints = {
            item.vision_artifact_id: item.content_fingerprint for item in artifacts
        }
        seen_artifact_ids: set[int] = set()
        facts: list[VisionArtifactDecisionFact] = []
        for row in rows:
            identifier = _required_id(
                row.vision_artifact_decision_id,
                "Vision artifact decision",
            )
            if (
                row.vision_artifact_id in seen_artifact_ids
                or fingerprints.get(row.vision_artifact_id) != row.artifact_fingerprint
                or row.decision not in {"accepted", "rejected", "feedback"}
            ):
                message = "Vision decision is invalid."
                raise ValueError(message)
            seen_artifact_ids.add(row.vision_artifact_id)
            facts.append(
                VisionArtifactDecisionFact(
                    vision_artifact_decision_id=identifier,
                    vision_artifact_id=row.vision_artifact_id,
                    artifact_fingerprint=row.artifact_fingerprint,
                    decision=cast(
                        "_ReviewDecision",
                        row.decision,
                    ),
                    rationale=row.rationale,
                    reviewer=row.reviewer,
                    idempotency_key=row.idempotency_key,
                    decided_at=row.decided_at,
                )
            )
        return tuple(facts)

    def _revisions(
        self,
        project_id: int,
        artifacts: tuple[VisionArtifactFact, ...],
        decisions: tuple[VisionArtifactDecisionFact, ...],
    ) -> tuple[VisionRevisionIntentFact, ...]:
        rows = self._session.exec(
            select(VisionRevisionIntent)
            .where(col(VisionRevisionIntent.project_id) == project_id)
            .order_by(col(VisionRevisionIntent.vision_revision_intent_id))
        ).all()
        accepted = {
            item.vision_artifact_id: item.artifact_fingerprint
            for item in decisions
            if item.decision == "accepted"
        }
        artifact_ids = {item.vision_artifact_id for item in artifacts}
        facts: list[VisionRevisionIntentFact] = []
        for row in rows:
            identifier = _required_id(
                row.vision_revision_intent_id,
                "Vision revision intent",
            )
            if (
                row.source_vision_artifact_id not in artifact_ids
                or accepted.get(row.source_vision_artifact_id)
                != row.source_vision_fingerprint
            ):
                message = "Vision revision intent source is invalid."
                raise ValueError(message)
            facts.append(
                VisionRevisionIntentFact(
                    vision_revision_intent_id=identifier,
                    source_vision_artifact_id=row.source_vision_artifact_id,
                    source_vision_fingerprint=row.source_vision_fingerprint,
                    reason=row.reason,
                    initiated_by=row.initiated_by,
                    initiated_at=row.initiated_at,
                )
            )
        return tuple(facts)

    def _turns(
        self,
        project_id: int,
        revisions: tuple[VisionRevisionIntentFact, ...],
    ) -> tuple[VisionInterviewTurnFact, ...]:
        rows = self._session.exec(
            select(VisionInterviewTurn)
            .where(col(VisionInterviewTurn.project_id) == project_id)
            .order_by(
                col(VisionInterviewTurn.turn_number),
                col(VisionInterviewTurn.vision_interview_turn_id),
            )
        ).all()
        revision_ids = {item.vision_revision_intent_id for item in revisions}
        facts: dict[int, VisionInterviewTurnFact] = {}
        last_turn_by_chain: dict[tuple[str, int | None], int] = {}
        for row in rows:
            identifier = _required_id(row.vision_interview_turn_id, "Vision turn")
            if row.mode not in {"initial", "revision"}:
                message = "Vision turn mode is invalid."
                raise ValueError(message)
            if (row.mode == "initial") != (row.revision_intent_id is None):
                message = "Vision turn revision linkage is invalid."
                raise ValueError(message)
            if (
                row.revision_intent_id is not None
                and row.revision_intent_id not in revision_ids
            ):
                message = "Vision turn revision intent is invalid."
                raise ValueError(message)
            chain = (row.mode, row.revision_intent_id)
            prior = None if row.prior_turn_id is None else facts.get(row.prior_turn_id)
            if prior is None:
                if row.prior_turn_id is not None or chain in last_turn_by_chain:
                    message = "Vision interview turn chain is invalid."
                    raise ValueError(message)
            elif (
                (prior.mode, prior.revision_intent_id) != chain
                or prior.turn_number + 1 != row.turn_number
                or last_turn_by_chain.get(chain) != row.prior_turn_id
            ):
                message = "Vision interview turn chain is invalid."
                raise ValueError(message)
            components = _canonical_object(row.components_json, "Vision turn")
            questions = _canonical_string_list(
                row.clarifying_questions_json,
                "Vision turn questions",
            )
            if row.output_fingerprint != canonical_hash(
                {
                    "components_json": components,
                    "vision_statement": row.vision_statement,
                    "is_complete": row.is_complete,
                    "clarifying_questions_json": list(questions),
                }
            ):
                message = "Vision turn output fingerprint changed."
                raise ValueError(message)
            facts[identifier] = VisionInterviewTurnFact(
                vision_interview_turn_id=identifier,
                mode=cast("_VisionMode", row.mode),
                turn_number=row.turn_number,
                revision_intent_id=row.revision_intent_id,
                prior_turn_id=row.prior_turn_id,
                user_text=row.user_text,
                components=components,
                vision_statement=row.vision_statement,
                is_complete=row.is_complete,
                clarifying_questions=questions,
                output_fingerprint=row.output_fingerprint,
                workflow_node_attempt_id=row.workflow_node_attempt_id,
                attempt_fingerprint=row.attempt_fingerprint,
                recorded_at=row.recorded_at,
            )
            last_turn_by_chain[chain] = identifier
        return tuple(facts.values())

    def _goal_decisions(
        self,
        project_id: int,
    ) -> tuple[ProductGoalArtifactDecisionFact, ...]:
        rows = self._session.exec(
            select(ProductGoalArtifactDecision)
            .where(col(ProductGoalArtifactDecision.project_id) == project_id)
            .order_by(col(ProductGoalArtifactDecision.product_goal_artifact_decision_id))
        ).all()
        facts: list[ProductGoalArtifactDecisionFact] = []
        for row in rows:
            identifier = _required_id(
                row.product_goal_artifact_decision_id,
                "Product Goal decision",
            )
            if row.decision not in {"accepted", "rejected", "feedback"}:
                message = "Product Goal decision is invalid."
                raise ValueError(message)
            facts.append(
                ProductGoalArtifactDecisionFact(
                    product_goal_artifact_decision_id=identifier,
                    product_goal_artifact_id=row.product_goal_artifact_id,
                    artifact_fingerprint=row.artifact_fingerprint,
                    decision=cast(
                        "_ReviewDecision",
                        row.decision,
                    ),
                    rationale=row.rationale,
                    reviewer=row.reviewer,
                    idempotency_key=row.idempotency_key,
                    decided_at=row.decided_at,
                )
            )
        return tuple(facts)

    def _goal_outcomes(self, project_id: int) -> tuple[ProductGoalOutcomeFact, ...]:
        rows = self._session.exec(
            select(ProductGoalOutcome)
            .where(col(ProductGoalOutcome.project_id) == project_id)
            .order_by(col(ProductGoalOutcome.product_goal_outcome_id))
        ).all()
        facts: list[ProductGoalOutcomeFact] = []
        for row in rows:
            identifier = _required_id(
                row.product_goal_outcome_id,
                "Product Goal outcome",
            )
            if row.outcome not in {"fulfilled", "abandoned"}:
                message = "Product Goal outcome is invalid."
                raise ValueError(message)
            facts.append(
                ProductGoalOutcomeFact(
                    product_goal_outcome_id=identifier,
                    product_goal_artifact_id=row.product_goal_artifact_id,
                    artifact_fingerprint=row.artifact_fingerprint,
                    outcome=cast("_GoalOutcome", row.outcome),
                    rationale=row.rationale,
                    decided_by=row.decided_by,
                    decided_at=row.decided_at,
                )
            )
        return tuple(facts)


def _required_id(value: int | None, label: str) -> int:
    if value is None:
        message = f"Stored {label} has no primary key."
        raise ValueError(message)
    return value


def _canonical_object(raw: str, label: str) -> JsonObject:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        message = f"{label} JSON is invalid."
        raise ValueError(message) from error
    if not isinstance(value, dict) or canonical_json(value) != raw:
        message = f"{label} JSON is not canonical."
        raise ValueError(message)
    return value


def _canonical_string_list(raw: str, label: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        message = f"{label} JSON is invalid."
        raise ValueError(message) from error
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) for item in value)
        or canonical_json(value) != raw
    ):
        message = f"{label} JSON is not canonical."
        raise ValueError(message)
    return tuple(value)


__all__ = ["VisionInterviewInputService"]
