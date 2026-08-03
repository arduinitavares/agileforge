"""Caller-transaction handlers for authority workflow facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.authority_curation import AuthorityFeedbackAttempt
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.authority_review_projection import (
    AuthorityReviewSnapshot,
    authority_review_fingerprint,
    build_authority_review_snapshot_in_session,
)
from services.specs.authority_selection import pending_authority_fingerprint
from services.specs.compiler_service import (
    AuthorityPersistenceError,
    persist_compiled_authority_for_version_in_session,
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


class AuthorityDecisionConflictError(RuntimeError):
    """Raised when durable facts already contain a terminal authority decision."""


def _terminal_decision_key(
    *,
    project_id: int,
    spec_version_id: int,
    pending_authority_id: int,
) -> str:
    return f"{project_id}:{spec_version_id}:{pending_authority_id}"


def _record_authority_decision(
    session: Session,
    *,
    snapshot: AuthorityReviewSnapshot,
    request: DecideAuthority,
    decided_at: datetime,
) -> SpecAuthorityAcceptance:
    """Append one exact terminal decision in the caller-owned transaction."""
    authority_id = snapshot.pending_authority_id
    authority_fingerprint = snapshot.authority_fingerprint
    spec_version_id = snapshot.spec_version_id
    if authority_id is None or authority_fingerprint is None or spec_version_id is None:
        message = "The authority review snapshot is missing durable identity."
        raise ValueError(message)
    if request.review_fingerprint != authority_review_fingerprint(snapshot):
        message = "The authority review fingerprint changed."
        raise AuthorityDecisionConflictError(message)
    key = _terminal_decision_key(
        project_id=snapshot.project_id,
        spec_version_id=spec_version_id,
        pending_authority_id=authority_id,
    )
    existing = session.exec(
        select(SpecAuthorityAcceptance).where(
            SpecAuthorityAcceptance.terminal_decision_key == key
        )
    ).first()
    if existing is not None:
        message = "The exact authority already has a terminal decision."
        raise AuthorityDecisionConflictError(message)
    row = SpecAuthorityAcceptance(
        project_id=snapshot.project_id,
        spec_version_id=spec_version_id,
        status=request.decision,
        policy="manual",
        decided_by=request.actor,
        decided_at=decided_at,
        rationale=request.rationale,
        compiler_version=snapshot.compiler_version,
        prompt_hash=snapshot.prompt_hash,
        spec_hash=snapshot.source_spec_hash,
        pending_authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        review_token=snapshot.review_token,
        review_fingerprint=request.review_fingerprint,
        disk_spec_hash=snapshot.disk_spec_hash,
        resolved_spec_path=snapshot.resolved_spec_path,
        actor_mode="workflow_domain",
        review_completeness=snapshot.omission_assessment,
        terminal_decision_key=key,
        provenance_source="workflow_domain",
    )
    session.add(row)
    session.flush()
    return row


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
            col(SpecRegistry.project_id) == project_id,
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
            col(SpecAuthorityAcceptance.project_id) == project_id,
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
    """Persist precomputed authority for the exact graph-selected spec."""
    spec = session.get(SpecRegistry, request.spec_version_id)
    if (
        spec is None
        or spec.project_id != request.project_id
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
    try:
        authority_id = persist_compiled_authority_for_version_in_session(
            session,
            spec_version_id=request.spec_version_id,
            compiled_authority=request.compiled_authority,
            compiled_at=evaluated_at,
        )
    except AuthorityPersistenceError as error:
        return _conflict(str(error))
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
        row = _record_authority_decision(
            session,
            snapshot=review,
            request=request,
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
    """Persist a precomputed replacement from exact rejected authority facts."""
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
    try:
        authority_id = persist_compiled_authority_for_version_in_session(
            session,
            spec_version_id=authority.spec_version_id,
            compiled_authority=request.compiled_authority,
            compiled_at=evaluated_at,
            force_recompile=True,
        )
    except AuthorityPersistenceError as error:
        return _conflict(str(error))
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
