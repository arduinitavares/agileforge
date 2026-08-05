"""Transactional handlers for the isolated Project Vision lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, func, select

from models.product_definition import (
    ProductGoalArtifactDecision,
    ProductGoalOutcome,
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.requests.vision import (
        BeginVisionRevision,
        DecideVisionReview,
        RecordVisionInterviewTurn,
    )


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT, message=message
        ),
    )


def _success(decision: NodeDecision, output: dict[str, object]) -> TransitionResult:
    return TransitionResult(ok=True, applied_node_id=decision.node_id, output=output)


def _has_reference(
    decision: NodeDecision, *, fact_type: str, fact_id: int, fingerprint: str
) -> bool:
    return any(
        reference.fact_type == fact_type
        and reference.fact_id == str(fact_id)
        and reference.fingerprint == fingerprint
        for reference in decision.fact_references
    )


def _active_goal_exists(session: Session, project_id: int) -> bool:
    """Return whether an accepted Product Goal has no durable outcome."""
    accepted_ids = {
        row.product_goal_artifact_id
        for row in session.exec(
            select(ProductGoalArtifactDecision).where(
                col(ProductGoalArtifactDecision.project_id) == project_id,
                col(ProductGoalArtifactDecision.decision) == "accepted",
            )
        ).all()
    }
    outcome_ids = {
        row.product_goal_artifact_id
        for row in session.exec(
            select(ProductGoalOutcome).where(
                col(ProductGoalOutcome.project_id) == project_id
            )
        ).all()
    }
    return bool(accepted_ids - outcome_ids)


def _open_revision_intent(
    session: Session, project_id: int
) -> VisionRevisionIntent | None:
    """Return the one revision intent that has not yet produced a Vision artifact."""
    intents = session.exec(
        select(VisionRevisionIntent)
        .where(col(VisionRevisionIntent.project_id) == project_id)
        .order_by(col(VisionRevisionIntent.vision_revision_intent_id))
    ).all()
    completed_turn_ids = {
        artifact.source_interview_turn_id
        for artifact in session.exec(
            select(VisionArtifact).where(col(VisionArtifact.project_id) == project_id)
        ).all()
    }
    open_intents: list[VisionRevisionIntent] = []
    for intent in intents:
        turns = session.exec(
            select(VisionInterviewTurn).where(
                col(VisionInterviewTurn.project_id) == project_id,
                col(VisionInterviewTurn.revision_intent_id)
                == intent.vision_revision_intent_id,
            )
        ).all()
        if not any(
            turn.vision_interview_turn_id in completed_turn_ids for turn in turns
        ):
            open_intents.append(intent)
    if len(open_intents) != 1:
        return None
    return open_intents[0]


def execute_record_vision_interview_turn(
    session: Session,
    request: RecordVisionInterviewTurn,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist one Vision turn and atomically materialize a complete Vision."""
    if request.mode == "revision":
        revision = _open_revision_intent(session, request.project_id)
        if revision is None:
            return _conflict("Vision revision does not have one open revision intent.")
        revision_intent_id = revision.vision_revision_intent_id
    else:
        revision = None
        if _open_revision_intent(session, request.project_id) is not None:
            return _conflict("Initial Vision turn is invalid while a revision is open.")
        revision_intent_id = None
    prior = session.exec(
        select(VisionInterviewTurn)
        .where(
            col(VisionInterviewTurn.project_id) == request.project_id,
            col(VisionInterviewTurn.mode) == request.mode,
            col(VisionInterviewTurn.revision_intent_id) == revision_intent_id,
        )
        .order_by(col(VisionInterviewTurn.turn_number).desc())
    ).first()
    turn_number = 1 if prior is None else prior.turn_number + 1
    components_json = canonical_json(request.updated_components)
    questions_json = canonical_json(list(request.clarifying_questions))
    output_fingerprint = canonical_hash(
        {
            "components_json": request.updated_components,
            "vision_statement": request.project_vision_statement.strip(),
            "is_complete": request.is_complete,
            "clarifying_questions_json": list(request.clarifying_questions),
        }
    )
    turn = VisionInterviewTurn(
        project_id=request.project_id,
        mode=request.mode,
        turn_number=turn_number,
        revision_intent_id=revision_intent_id,
        prior_turn_id=None if prior is None else prior.vision_interview_turn_id,
        user_text=request.user_text.strip(),
        components_json=components_json,
        vision_statement=request.project_vision_statement.strip(),
        is_complete=request.is_complete,
        clarifying_questions_json=questions_json,
        output_fingerprint=output_fingerprint,
        workflow_node_attempt_id=request.attempt_id,
        attempt_fingerprint=request.attempt_fingerprint,
        recorded_at=evaluated_at,
    )
    session.add(turn)
    session.flush()
    if turn.vision_interview_turn_id is None:
        return _conflict("Vision interview turn did not receive a durable identity.")
    output: dict[str, object] = {
        "vision_interview_turn_id": turn.vision_interview_turn_id
    }
    if request.is_complete:
        parent_id = None if revision is None else revision.source_vision_artifact_id
        if parent_id is None:
            parent_id = session.exec(
                select(func.max(VisionArtifact.vision_artifact_id)).where(
                    col(VisionArtifact.project_id) == request.project_id
                )
            ).one()
        version_number = (
            session.exec(
                select(func.max(VisionArtifact.version_number)).where(
                    col(VisionArtifact.project_id) == request.project_id
                )
            ).one()
            or 0
        ) + 1
        fingerprint = canonical_hash(
            {
                "components": request.updated_components,
                "statement": request.project_vision_statement.strip(),
            }
        )
        artifact = VisionArtifact(
            project_id=request.project_id,
            version_number=version_number,
            components_json=components_json,
            statement=request.project_vision_statement.strip(),
            content_fingerprint=fingerprint,
            supersedes_vision_artifact_id=parent_id,
            source_interview_turn_id=turn.vision_interview_turn_id,
            created_by=request.actor,
            created_at=evaluated_at,
        )
        session.add(artifact)
        session.flush()
        if artifact.vision_artifact_id is None:
            return _conflict("Vision artifact did not receive a durable identity.")
        output.update(
            vision_artifact_id=artifact.vision_artifact_id,
            vision_fingerprint=artifact.content_fingerprint,
        )
    return _success(decision, output)


def execute_decide_vision_review(
    session: Session,
    request: DecideVisionReview,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append exactly one decision for the graph-selected pending Vision."""
    artifact = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == request.project_id,
            col(VisionArtifact.vision_artifact_id) == request.vision_artifact_id,
        )
    ).one_or_none()
    if (
        artifact is None
        or artifact.content_fingerprint != request.vision_fingerprint
        or not _has_reference(
            decision,
            fact_type="vision",
            fact_id=request.vision_artifact_id,
            fingerprint=request.vision_fingerprint,
        )
    ):
        return _conflict("Vision review does not target the waiting artifact.")
    if (
        session.exec(
            select(VisionArtifactDecision).where(
                col(VisionArtifactDecision.project_id) == request.project_id,
                col(VisionArtifactDecision.vision_artifact_id)
                == request.vision_artifact_id,
            )
        ).one_or_none()
        is not None
    ):
        return _conflict("Vision artifact already has a terminal review decision.")
    if request.decision == "accepted" and _active_goal_exists(
        session, request.project_id
    ):
        return _conflict(
            "Vision revision acceptance is blocked while a Product Goal is active."
        )
    row = VisionArtifactDecision(
        project_id=request.project_id,
        vision_artifact_id=request.vision_artifact_id,
        artifact_fingerprint=request.vision_fingerprint,
        decision=request.decision,
        rationale=request.rationale.strip(),
        reviewer=request.actor,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(row)
    session.flush()
    if row.vision_artifact_decision_id is None:
        return _conflict("Vision decision did not receive a durable identity.")
    return _success(
        decision,
        {"vision_artifact_decision_id": row.vision_artifact_decision_id},
    )


def execute_begin_vision_revision(
    session: Session,
    request: BeginVisionRevision,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Open a Vision replacement only after every accepted Goal is resolved."""
    artifact = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == request.project_id,
            col(VisionArtifact.vision_artifact_id) == request.source_vision_artifact_id,
        )
    ).one_or_none()
    accepted = session.exec(
        select(VisionArtifactDecision).where(
            col(VisionArtifactDecision.project_id) == request.project_id,
            col(VisionArtifactDecision.vision_artifact_id)
            == request.source_vision_artifact_id,
            col(VisionArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if (
        artifact is None
        or accepted is None
        or artifact.content_fingerprint != request.source_vision_fingerprint
        or _active_goal_exists(session, request.project_id)
        or not _has_reference(
            decision,
            fact_type="vision",
            fact_id=request.source_vision_artifact_id,
            fingerprint=request.source_vision_fingerprint,
        )
    ):
        return _conflict("Vision revision does not target an eligible accepted Vision.")
    if _open_revision_intent(session, request.project_id) is not None:
        return _conflict("Vision revision is already open.")
    row = VisionRevisionIntent(
        project_id=request.project_id,
        source_vision_artifact_id=request.source_vision_artifact_id,
        source_vision_fingerprint=request.source_vision_fingerprint,
        reason=request.reason.strip(),
        initiated_by=request.actor,
        initiated_at=evaluated_at,
    )
    session.add(row)
    session.flush()
    if row.vision_revision_intent_id is None:
        return _conflict("Vision revision intent did not receive a durable identity.")
    return _success(
        decision,
        {"vision_revision_intent_id": row.vision_revision_intent_id},
    )


__all__ = [
    "execute_begin_vision_revision",
    "execute_decide_vision_review",
    "execute_record_vision_interview_turn",
]
