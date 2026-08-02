"""Caller-transaction handlers for authority workflow facts."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlmodel import Session, col, select

from models.authority_curation import AuthorityFeedbackAttempt
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.agent_workbench.authority_decision import (
    AuthorityDecisionConflictError,
    record_authority_decision_in_session,
)
from services.agent_workbench.authority_review import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.specs.authority_selection import pending_authority_fingerprint
from services.specs.compiler_service import (
    compile_spec_authority_for_version_in_session,
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

    from workflow.requests.authority import (
        CompileAuthority,
        DecideAuthority,
        RecordAuthorityFeedback,
        RepairAuthority,
    )


def _success(decision: NodeDecision, output: dict[str, object]) -> TransitionResult:
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output=output,
    )


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


def _external_failure(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED,
            message=message,
        ),
    )


def _authority(
    session: Session,
    *,
    project_id: int,
    authority_id: int,
) -> CompiledSpecAuthority | None:
    return session.exec(
        select(CompiledSpecAuthority)
        .join(
            SpecRegistry,
            col(CompiledSpecAuthority.spec_version_id)
            == col(SpecRegistry.spec_version_id),
        )
        .where(
            col(SpecRegistry.product_id) == project_id,
            col(CompiledSpecAuthority.authority_id) == authority_id,
        )
    ).one_or_none()


def _terminal_decision(
    session: Session,
    *,
    project_id: int,
    authority_id: int,
) -> SpecAuthorityAcceptance | None:
    rows = session.exec(
        select(SpecAuthorityAcceptance)
        .where(
            col(SpecAuthorityAcceptance.product_id) == project_id,
            col(SpecAuthorityAcceptance.pending_authority_id) == authority_id,
        )
        .order_by(
            col(SpecAuthorityAcceptance.decided_at),
            col(SpecAuthorityAcceptance.id),
        )
    ).all()
    if len(rows) > 1:
        return None
    return rows[0] if rows else None


def _matches_reference(
    decision: NodeDecision,
    *,
    fact_type: str,
    fact_id: int,
    fingerprint: str,
) -> bool:
    return len(decision.fact_references) == 1 and all(
        reference.fact_type == fact_type
        and reference.fact_id == str(fact_id)
        and reference.fingerprint == fingerprint
        for reference in decision.fact_references
    )


def _review_for_decision(
    session: Session,
    request: DecideAuthority,
) -> AuthorityReviewSnapshot | TransitionResult:
    """Rebuild and bind every persisted input for one authority decision."""
    authority = _authority(
        session,
        project_id=request.project_id,
        authority_id=request.pending_authority_id,
    )
    actual_fingerprint = (
        pending_authority_fingerprint(authority) if authority is not None else None
    )
    if authority is None or actual_fingerprint != request.authority_fingerprint:
        return _conflict("DecideAuthority does not target the pending authority.")
    review = build_authority_review_snapshot_in_session(
        session,
        project_id=request.project_id,
    )
    if not isinstance(review, AuthorityReviewSnapshot):
        return _conflict("The pending authority review packet is unavailable.")
    if (
        review.pending_authority_id != request.pending_authority_id
        or review.authority_fingerprint != request.authority_fingerprint
        or review.review_fingerprint != request.review_fingerprint
    ):
        return _conflict("The authority review facts changed.")
    return review


def validate_decide_authority_review(
    session: Session,
    request: DecideAuthority,
) -> TransitionResult | None:
    """Return a pre-receipt conflict when the reviewed packet changed."""
    review = _review_for_decision(session, request)
    return review if isinstance(review, TransitionResult) else None


def execute_compile_authority(
    session: Session,
    request: CompileAuthority,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Compile the exact registered spec selected by the graph."""
    spec = session.get(SpecRegistry, request.spec_version_id)
    if (
        spec is None
        or spec.product_id != request.project_id
        or spec.status != "approved"
        or spec.spec_hash != request.expected_spec_hash
        or not _matches_reference(
            decision,
            fact_type="spec_version",
            fact_id=request.spec_version_id,
            fingerprint=request.expected_spec_hash,
        )
    ):
        return _conflict("CompileAuthority does not target the graph-selected spec.")
    result = cast(
        "dict[str, object]",
        compile_spec_authority_for_version_in_session(
            session,
            spec_version_id=request.spec_version_id,
            compiled_at=evaluated_at,
            compiler_model=request.compiler_model,
        ),
    )
    if result.get("success") is not True:
        return _external_failure(
            str(result.get("error") or "Authority compile failed.")
        )
    authority_id = result.get("authority_id")
    if not isinstance(authority_id, int):
        return _conflict("Authority compilation did not return a durable identity.")
    return _success(
        decision,
        {
            "authority_id": authority_id,
            "spec_version_id": request.spec_version_id,
            "compiler_model": request.compiler_model,
        },
    )


def execute_decide_authority(
    session: Session,
    request: DecideAuthority,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append a terminal decision bound to the exact review packet."""
    review = _review_for_decision(session, request)
    if isinstance(review, TransitionResult):
        return review
    if not _matches_reference(
        decision,
        fact_type="authority",
        fact_id=request.pending_authority_id,
        fingerprint=request.authority_fingerprint,
    ):
        return _conflict("DecideAuthority does not target the pending authority.")
    try:
        row = record_authority_decision_in_session(
            session,
            snapshot=review,
            decision=request.decision,
            rationale=request.rationale,
            actor=request.actor,
            policy="manual",
            review_fingerprint=request.review_fingerprint,
            decided_at=evaluated_at,
        )
    except AuthorityDecisionConflictError as error:
        return _conflict(str(error))
    if row.id is None:
        return _conflict("Authority decision did not receive a durable identity.")
    return _success(
        decision,
        {
            "authority_decision_id": row.id,
            "authority_id": request.pending_authority_id,
            "decision": request.decision,
        },
    )


def execute_record_authority_feedback(
    session: Session,
    request: RecordAuthorityFeedback,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append canonical feedback for one exact rejected authority."""
    authority = _authority(
        session,
        project_id=request.project_id,
        authority_id=request.pending_authority_id,
    )
    actual_fingerprint = (
        pending_authority_fingerprint(authority) if authority is not None else None
    )
    terminal = _terminal_decision(
        session,
        project_id=request.project_id,
        authority_id=request.pending_authority_id,
    )
    if (
        authority is None
        or actual_fingerprint != request.authority_fingerprint
        or terminal is None
        or terminal.status != "rejected"
        or not _matches_reference(
            decision,
            fact_type="authority",
            fact_id=request.pending_authority_id,
            fingerprint=request.authority_fingerprint,
        )
    ):
        return _conflict("Feedback requires the exact rejected authority.")
    feedback_fingerprint = canonical_hash(request.feedback)
    row = AuthorityFeedbackAttempt(
        project_id=request.project_id,
        feedback_attempt_id=f"workflow-{request.idempotency_key}",
        source_authority_id=request.pending_authority_id,
        source_authority_fingerprint=request.authority_fingerprint,
        feedback_fingerprint=feedback_fingerprint,
        status="recorded",
        has_blocking_feedback=True,
        feedback_json=canonical_json(request.feedback),
        request_hash=canonical_hash(request.model_dump(mode="json")),
        idempotency_key=request.idempotency_key,
        changed_by=request.actor,
        created_at=evaluated_at,
        updated_at=evaluated_at,
    )
    session.add(row)
    session.flush()
    if row.feedback_row_id is None:
        return _conflict("Authority feedback did not receive a durable identity.")
    return _success(
        decision,
        {
            "authority_feedback_id": row.feedback_row_id,
            "feedback_fingerprint": feedback_fingerprint,
        },
    )


def execute_repair_authority(
    session: Session,
    request: RepairAuthority,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Compile a replacement candidate from exact rejected authority facts."""
    authority = _authority(
        session,
        project_id=request.project_id,
        authority_id=request.source_authority_id,
    )
    actual_fingerprint = (
        pending_authority_fingerprint(authority) if authority is not None else None
    )
    terminal = _terminal_decision(
        session,
        project_id=request.project_id,
        authority_id=request.source_authority_id,
    )
    feedback = session.exec(
        select(AuthorityFeedbackAttempt).where(
            col(AuthorityFeedbackAttempt.project_id) == request.project_id,
            col(AuthorityFeedbackAttempt.source_authority_id)
            == request.source_authority_id,
            col(AuthorityFeedbackAttempt.source_authority_fingerprint)
            == request.source_authority_fingerprint,
        )
    ).first()
    if (
        authority is None
        or actual_fingerprint != request.source_authority_fingerprint
        or terminal is None
        or terminal.status != "rejected"
        or feedback is None
        or not _matches_reference(
            decision,
            fact_type="authority",
            fact_id=request.source_authority_id,
            fingerprint=request.source_authority_fingerprint,
        )
    ):
        return _conflict("Repair requires exact rejected authority feedback facts.")
    result = cast(
        "dict[str, object]",
        compile_spec_authority_for_version_in_session(
            session,
            spec_version_id=authority.spec_version_id,
            compiled_at=evaluated_at,
            force_recompile=True,
            compiler_model="openrouter/openai/gpt-5.6-luna",
        ),
    )
    if result.get("success") is not True:
        return _external_failure(str(result.get("error") or "Authority repair failed."))
    authority_id = result.get("authority_id")
    if not isinstance(authority_id, int):
        return _conflict("Authority repair did not return a durable identity.")
    return _success(
        decision,
        {
            "authority_id": authority_id,
            "source_authority_id": request.source_authority_id,
        },
    )


__all__ = [
    "execute_compile_authority",
    "execute_decide_authority",
    "execute_record_authority_feedback",
    "execute_repair_authority",
]
