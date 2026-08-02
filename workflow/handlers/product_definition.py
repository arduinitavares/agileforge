"""Caller-transaction handlers for Vision and Backlog workflow facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, func, select

from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import BacklogArtifact, VisionArtifact
from services.agent_workbench.backlog_phase import (
    record_backlog_decision_in_session,
    record_backlog_draft_in_session,
)
from services.agent_workbench.backlog_reconciliation import (
    BacklogReconciliationError,
    reconcile_stale_backlog_in_session,
)
from services.agent_workbench.vision_phase import (
    record_vision_decision_in_session,
    record_vision_draft_in_session,
)
from services.specs.authority_selection import pending_authority_fingerprint
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.requests.product_definition import (
        DecideBacklog,
        DecideVision,
        ReconcileBacklog,
        RecordBacklogDraft,
        RecordVisionDraft,
    )


def _success(decision: NodeDecision, output: dict[str, object]) -> TransitionResult:
    return TransitionResult(ok=True, applied_node_id=decision.node_id, output=output)


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


def _accepted_authority(
    session: Session,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
) -> CompiledSpecAuthority | None:
    authority = session.exec(
        select(CompiledSpecAuthority)
        .join(
            SpecRegistry,
            col(CompiledSpecAuthority.spec_version_id)
            == col(SpecRegistry.spec_version_id),
        )
        .where(
            col(SpecRegistry.product_id) == project_id,
            col(SpecRegistry.status) == "approved",
            col(CompiledSpecAuthority.authority_id) == authority_id,
        )
    ).one_or_none()
    if (
        authority is None
        or pending_authority_fingerprint(authority) != authority_fingerprint
    ):
        return None
    acceptance = session.exec(
        select(SpecAuthorityAcceptance).where(
            col(SpecAuthorityAcceptance.product_id) == project_id,
            col(SpecAuthorityAcceptance.pending_authority_id) == authority_id,
            col(SpecAuthorityAcceptance.authority_fingerprint) == authority_fingerprint,
            col(SpecAuthorityAcceptance.status) == "accepted",
        )
    ).one_or_none()
    return authority if acceptance is not None else None


def _matches_reference(
    decision: NodeDecision,
    *,
    fact_type: str,
    fact_id: int,
    fingerprint: str,
) -> bool:
    return any(
        item.fact_type == fact_type
        and item.fact_id == str(fact_id)
        and item.fingerprint == fingerprint
        for item in decision.fact_references
    )


def _expected_parent(decision: NodeDecision, artifact_type: str) -> int | None:
    references = tuple(
        item for item in decision.fact_references if item.fact_type == artifact_type
    )
    if not references:
        return None
    try:
        return max(int(item.fact_id) for item in references)
    except ValueError:
        return None


def _next_artifact_id(session: Session) -> int:
    """Allocate one identity space shared by Vision and Backlog artifacts."""
    latest_vision = session.exec(
        select(func.max(VisionArtifact.vision_artifact_id))
    ).one()
    latest_backlog = session.exec(
        select(func.max(BacklogArtifact.backlog_artifact_id))
    ).one()
    return max(latest_vision or 0, latest_backlog or 0) + 1


def execute_record_vision_draft(
    session: Session,
    request: RecordVisionDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Record host-validated Vision content for exact graph authority facts."""
    authority = _accepted_authority(
        session,
        project_id=request.project_id,
        authority_id=request.authority_id,
        authority_fingerprint=request.authority_fingerprint,
    )
    expected_parent = _expected_parent(decision, "vision")
    if (
        authority is None
        or not _matches_reference(
            decision,
            fact_type="authority",
            fact_id=request.authority_id,
            fingerprint=request.authority_fingerprint,
        )
        or request.supersedes_vision_artifact_id != expected_parent
    ):
        return _conflict("RecordVisionDraft does not target exact graph facts.")
    try:
        row = record_vision_draft_in_session(
            session,
            project_id=request.project_id,
            authority_id=request.authority_id,
            authority_fingerprint=request.authority_fingerprint,
            canonical_content=request.canonical_content,
            content_fingerprint=request.content_fingerprint,
            supersedes_vision_artifact_id=request.supersedes_vision_artifact_id,
            artifact_id=_next_artifact_id(session),
            actor=request.actor,
            recorded_at=evaluated_at,
        )
    except ValueError as error:
        return _conflict(str(error))
    if row.vision_artifact_id is None:
        return _conflict("Vision artifact did not receive a durable identity.")
    return _success(
        decision,
        {
            "vision_artifact_id": row.vision_artifact_id,
            "content_fingerprint": row.content_fingerprint,
            "authority_id": row.authority_id,
        },
    )


def execute_decide_vision(
    session: Session,
    request: DecideVision,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append one decision for the exact waiting Vision artifact."""
    artifact = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == request.project_id,
            col(VisionArtifact.vision_artifact_id) == request.vision_artifact_id,
        )
    ).one_or_none()
    if (
        artifact is None
        or artifact.content_fingerprint != request.artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="vision",
            fact_id=request.vision_artifact_id,
            fingerprint=request.artifact_fingerprint,
        )
    ):
        return _conflict("DecideVision does not target the waiting artifact.")
    try:
        row = record_vision_decision_in_session(
            session,
            artifact=artifact,
            decision=request.decision,
            rationale=request.rationale,
            reviewer=request.actor,
            idempotency_key=request.idempotency_key,
            decided_at=evaluated_at,
        )
    except ValueError as error:
        return _conflict(str(error))
    if row.vision_artifact_decision_id is None:
        return _conflict("Vision decision did not receive a durable identity.")
    return _success(
        decision,
        {
            "vision_artifact_decision_id": row.vision_artifact_decision_id,
            "vision_artifact_id": request.vision_artifact_id,
            "decision": request.decision,
        },
    )


def execute_record_backlog_draft(
    session: Session,
    request: RecordBacklogDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Record host-validated Backlog content for exact graph authority facts."""
    authority = _accepted_authority(
        session,
        project_id=request.project_id,
        authority_id=request.authority_id,
        authority_fingerprint=request.authority_fingerprint,
    )
    expected_parent = _expected_parent(decision, "backlog")
    if (
        authority is None
        or not _matches_reference(
            decision,
            fact_type="authority",
            fact_id=request.authority_id,
            fingerprint=request.authority_fingerprint,
        )
        or request.supersedes_backlog_artifact_id != expected_parent
    ):
        return _conflict("RecordBacklogDraft does not target exact graph facts.")
    try:
        row = record_backlog_draft_in_session(
            session,
            project_id=request.project_id,
            authority_id=request.authority_id,
            authority_fingerprint=request.authority_fingerprint,
            canonical_content=request.canonical_content,
            content_fingerprint=request.content_fingerprint,
            supersedes_backlog_artifact_id=request.supersedes_backlog_artifact_id,
            artifact_id=_next_artifact_id(session),
            actor=request.actor,
            recorded_at=evaluated_at,
        )
    except ValueError as error:
        return _conflict(str(error))
    if row.backlog_artifact_id is None:
        return _conflict("Backlog artifact did not receive a durable identity.")
    return _success(
        decision,
        {
            "backlog_artifact_id": row.backlog_artifact_id,
            "content_fingerprint": row.content_fingerprint,
            "authority_id": row.authority_id,
        },
    )


def execute_decide_backlog(
    session: Session,
    request: DecideBacklog,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append one exact Backlog decision and preserve replacement guards."""
    artifact = session.exec(
        select(BacklogArtifact).where(
            col(BacklogArtifact.project_id) == request.project_id,
            col(BacklogArtifact.backlog_artifact_id) == request.backlog_artifact_id,
        )
    ).one_or_none()
    if (
        artifact is None
        or artifact.content_fingerprint != request.artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="backlog",
            fact_id=request.backlog_artifact_id,
            fingerprint=request.artifact_fingerprint,
        )
    ):
        return _conflict("DecideBacklog does not target the waiting artifact.")
    try:
        row = record_backlog_decision_in_session(
            session,
            artifact=artifact,
            decision=request.decision,
            rationale=request.rationale,
            reviewer=request.actor,
            idempotency_key=request.idempotency_key,
            decided_at=evaluated_at,
        )
    except ValueError as error:
        return _conflict(str(error))
    if row.backlog_artifact_decision_id is None:
        return _conflict("Backlog decision did not receive a durable identity.")
    return _success(
        decision,
        {
            "backlog_artifact_decision_id": row.backlog_artifact_decision_id,
            "backlog_artifact_id": request.backlog_artifact_id,
            "decision": request.decision,
        },
    )


def execute_reconcile_backlog(
    session: Session,
    request: ReconcileBacklog,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Reconcile exactly the stale artifact set selected by the graph."""
    authority = _accepted_authority(
        session,
        project_id=request.project_id,
        authority_id=request.replacement_authority_id,
        authority_fingerprint=request.replacement_authority_fingerprint,
    )
    referenced_ids = tuple(
        sorted(
            int(item.fact_id)
            for item in decision.fact_references
            if item.fact_type in {"vision", "backlog"}
        )
    )
    if (
        authority is None
        or request.affected_artifact_ids != referenced_ids
        or not _matches_reference(
            decision,
            fact_type="authority",
            fact_id=request.replacement_authority_id,
            fingerprint=request.replacement_authority_fingerprint,
        )
    ):
        return _conflict("ReconcileBacklog does not target exact stale facts.")
    try:
        row = reconcile_stale_backlog_in_session(
            session,
            project_id=request.project_id,
            replacement_authority_id=request.replacement_authority_id,
            replacement_authority_fingerprint=(
                request.replacement_authority_fingerprint
            ),
            affected_artifact_ids=request.affected_artifact_ids,
            reconciled_by=request.actor,
            reconciled_at=evaluated_at,
        )
    except BacklogReconciliationError as error:
        return _conflict(error.detail)
    if row.backlog_authority_reconciliation_id is None:
        return _conflict("Backlog reconciliation did not receive a durable identity.")
    return _success(
        decision,
        {
            "backlog_authority_reconciliation_id": (
                row.backlog_authority_reconciliation_id
            ),
            "replacement_authority_id": request.replacement_authority_id,
            "affected_artifact_ids": request.affected_artifact_ids,
        },
    )


__all__ = [
    "execute_decide_backlog",
    "execute_decide_vision",
    "execute_reconcile_backlog",
    "execute_record_backlog_draft",
    "execute_record_vision_draft",
]
