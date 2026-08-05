"""Transactional Product Goal lifecycle handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
)
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import (
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.requests.product_goal import (
        AbandonProductGoal,
        DecideProductGoalReview,
        FulfillProductGoal,
        RecordProductGoalInterviewTurn,
    )


def _fail(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT, message=message
        ),
    )


def _reference(
    decision: NodeDecision, kind: str, identifier: int, fingerprint: str
) -> bool:
    return any(
        item.fact_type == kind
        and item.fact_id == str(identifier)
        and item.fingerprint == fingerprint
        for item in decision.fact_references
    )


def execute_record_product_goal_interview_turn(
    session: Session,
    request: RecordProductGoalInterviewTurn,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist one trusted turn and create a candidate only when complete."""
    vision_refs = [
        item for item in decision.fact_references if item.fact_type == "vision"
    ]
    if len(vision_refs) != 1:
        return _fail("Product Goal interview requires exact accepted Vision.")
    goals = session.exec(
        select(ProductGoalArtifact)
        .where(col(ProductGoalArtifact.project_id) == request.project_id)
        .order_by(col(ProductGoalArtifact.goal_number).desc())
    ).all()
    active_ids = {
        row.product_goal_artifact_id
        for row in session.exec(
            select(ProductGoalArtifactDecision).where(
                col(ProductGoalArtifactDecision.project_id) == request.project_id,
                col(ProductGoalArtifactDecision.decision) == "accepted",
            )
        ).all()
    }
    resolved_ids = {
        row.product_goal_artifact_id
        for row in session.exec(
            select(ProductGoalOutcome).where(
                col(ProductGoalOutcome.project_id) == request.project_id
            )
        ).all()
    }
    if active_ids - resolved_ids:
        return _fail("A Product Goal is already active.")
    feedback = {
        row.product_goal_artifact_id
        for row in session.exec(
            select(ProductGoalArtifactDecision).where(
                col(ProductGoalArtifactDecision.project_id) == request.project_id,
                col(ProductGoalArtifactDecision.decision).in_(["feedback", "rejected"]),
            )
        ).all()
    }
    prior_goal = next(
        (item for item in goals if item.product_goal_artifact_id in feedback), None
    )
    goal_number = (
        prior_goal.goal_number
        if prior_goal is not None
        else (goals[0].goal_number + 1 if goals else 1)
    )
    turns = session.exec(
        select(ProductGoalInterviewTurn)
        .where(
            col(ProductGoalInterviewTurn.project_id) == request.project_id,
            col(ProductGoalInterviewTurn.goal_number) == goal_number,
        )
        .order_by(col(ProductGoalInterviewTurn.revision_number).desc())
    ).all()
    revision_number = (
        prior_goal.revision_number + 1
        if prior_goal is not None
        else (turns[0].revision_number if turns else 1)
    )
    turn = ProductGoalInterviewTurn(
        project_id=request.project_id,
        vision_artifact_id=int(vision_refs[0].fact_id),
        vision_fingerprint=vision_refs[0].fingerprint,
        goal_number=goal_number,
        revision_number=revision_number,
        prior_turn_id=(
            None
            if prior_goal is not None or not turns
            else turns[0].product_goal_interview_turn_id
        ),
        user_text=request.user_text.strip(),
        components_json=canonical_json(request.updated_components),
        goal_statement=request.product_goal_statement.strip(),
        is_complete=request.is_complete,
        clarifying_questions_json=canonical_json(list(request.clarifying_questions)),
        output_fingerprint=product_goal_interview_output_fingerprint(
            request.updated_components,
            request.product_goal_statement.strip(),
            request.is_complete,
            request.clarifying_questions,
        ),
        workflow_node_attempt_id=request.attempt_id,
        attempt_fingerprint=request.attempt_fingerprint,
        recorded_at=evaluated_at,
    )
    session.add(turn)
    session.flush()
    if turn.product_goal_interview_turn_id is None:
        return _fail("Product Goal turn did not receive an identity.")
    output: dict[str, object] = {
        "product_goal_interview_turn_id": turn.product_goal_interview_turn_id
    }
    if request.is_complete:
        goal = ProductGoalArtifact(
            project_id=request.project_id,
            vision_artifact_id=turn.vision_artifact_id,
            vision_fingerprint=turn.vision_fingerprint,
            goal_number=goal_number,
            revision_number=revision_number,
            statement=turn.goal_statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                request.updated_components,
                turn.goal_statement,
            ),
            supersedes_product_goal_artifact_id=(
                None if prior_goal is None else prior_goal.product_goal_artifact_id
            ),
            source_interview_turn_id=turn.product_goal_interview_turn_id,
            created_by=request.actor,
            created_at=evaluated_at,
        )
        session.add(goal)
        session.flush()
        output.update(
            product_goal_artifact_id=goal.product_goal_artifact_id,
            product_goal_fingerprint=goal.content_fingerprint,
        )
    return TransitionResult(ok=True, applied_node_id=decision.node_id, output=output)


def execute_decide_product_goal_review(
    session: Session,
    request: DecideProductGoalReview,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist a decision only for the graph-selected pending Goal candidate."""
    goal = session.get(ProductGoalArtifact, request.product_goal_artifact_id)
    if goal is None:
        return _fail("Product Goal review does not target the pending candidate.")
    goal_id = goal.product_goal_artifact_id
    if goal_id is None:
        return _fail("Product Goal review does not target the pending candidate.")
    if (
        goal.project_id != request.project_id
        or goal.content_fingerprint != request.product_goal_fingerprint
        or not _reference(
            decision,
            "product_goal",
            goal_id,
            goal.content_fingerprint,
        )
        or not _reference(
            decision, "vision", goal.vision_artifact_id, goal.vision_fingerprint
        )
        or (
            request.decision in {"rejected", "feedback"}
            and not request.rationale.strip()
        )
    ):
        return _fail("Product Goal review does not target the pending candidate.")
    if (
        session.exec(
            select(ProductGoalArtifactDecision).where(
                col(ProductGoalArtifactDecision.project_id) == request.project_id,
                col(ProductGoalArtifactDecision.product_goal_artifact_id)
                == request.product_goal_artifact_id,
            )
        ).one_or_none()
        is not None
    ):
        return _fail("Product Goal already has a terminal review decision.")
    row = ProductGoalArtifactDecision(
        project_id=request.project_id,
        product_goal_artifact_id=request.product_goal_artifact_id,
        artifact_fingerprint=request.product_goal_fingerprint,
        decision=request.decision,
        rationale=request.rationale.strip(),
        reviewer=request.actor,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(row)
    session.flush()
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={
            "product_goal_artifact_decision_id": row.product_goal_artifact_decision_id
        },
    )


def _outcome(
    session: Session,
    request: FulfillProductGoal | AbandonProductGoal,
    decision: NodeDecision,
    evaluated_at: datetime,
    outcome: str,
) -> TransitionResult:
    goal = session.get(ProductGoalArtifact, request.product_goal_artifact_id)
    if goal is None:
        return _fail("Product Goal outcome does not target the active Goal.")
    goal_id = goal.product_goal_artifact_id
    if goal_id is None:
        return _fail("Product Goal outcome does not target the active Goal.")
    if (
        goal.content_fingerprint != request.product_goal_fingerprint
        or not request.rationale.strip()
        or not _reference(
            decision,
            "product_goal",
            goal_id,
            goal.content_fingerprint,
        )
    ):
        return _fail("Product Goal outcome does not target the active Goal.")
    accepted = session.exec(
        select(ProductGoalArtifactDecision).where(
            col(ProductGoalArtifactDecision.project_id) == request.project_id,
            col(ProductGoalArtifactDecision.product_goal_artifact_id) == goal_id,
            col(ProductGoalArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    existing = session.exec(
        select(ProductGoalOutcome).where(
            col(ProductGoalOutcome.project_id) == request.project_id,
            col(ProductGoalOutcome.product_goal_artifact_id) == goal_id,
        )
    ).one_or_none()
    if accepted is None or existing is not None:
        return _fail("Product Goal outcome does not target the active Goal.")
    row = ProductGoalOutcome(
        project_id=request.project_id,
        product_goal_artifact_id=request.product_goal_artifact_id,
        artifact_fingerprint=request.product_goal_fingerprint,
        outcome=outcome,
        rationale=request.rationale.strip(),
        decided_by=request.actor,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(row)
    session.flush()
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={"product_goal_outcome_id": row.product_goal_outcome_id},
    )


def execute_fulfill_product_goal(
    session: Session,
    request: FulfillProductGoal,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist a fulfillment outcome for the exact active Product Goal."""
    return _outcome(session, request, decision, evaluated_at, "fulfilled")


def execute_abandon_product_goal(
    session: Session,
    request: AbandonProductGoal,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist an abandonment outcome for the exact active Product Goal."""
    return _outcome(session, request, decision, evaluated_at, "abandoned")
