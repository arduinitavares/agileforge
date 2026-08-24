"""Caller-transaction handlers for Vision and Backlog workflow facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, func, select

from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalOutcome,
)
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    VisionArtifact,
    WorkflowTransitionReceipt,
)
from services.specs.accepted_specification import (
    AcceptedSpecificationIntegrityError,
    load_current_accepted_specification,
)
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.requests.product_definition import DecideBacklog

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.requests.product_definition import RecordBacklogDraft


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


def _accepted_specification(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
) -> bool:
    try:
        specification = load_current_accepted_specification(
            session,
            project_id=project_id,
        )
    except AcceptedSpecificationIntegrityError:
        return False
    return (
        specification is not None
        and specification.spec_version_id == spec_version_id
        and specification.spec_hash == spec_hash
    )


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


def _accepted_goal(
    session: Session,
    *,
    project_id: int,
    product_goal_artifact_id: int,
    product_goal_fingerprint: str,
) -> ProductGoalArtifact | None:
    """Return one unresolved Goal accepted with its exact fingerprint."""
    goal = session.exec(
        select(ProductGoalArtifact).where(
            col(ProductGoalArtifact.project_id) == project_id,
            col(ProductGoalArtifact.product_goal_artifact_id)
            == product_goal_artifact_id,
            col(ProductGoalArtifact.content_fingerprint) == product_goal_fingerprint,
        )
    ).one_or_none()
    if goal is None:
        return None
    decision = session.exec(
        select(ProductGoalArtifactDecision).where(
            col(ProductGoalArtifactDecision.project_id) == project_id,
            col(ProductGoalArtifactDecision.product_goal_artifact_id)
            == product_goal_artifact_id,
            col(ProductGoalArtifactDecision.artifact_fingerprint)
            == product_goal_fingerprint,
            col(ProductGoalArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    outcome = session.exec(
        select(ProductGoalOutcome).where(
            col(ProductGoalOutcome.project_id) == project_id,
            col(ProductGoalOutcome.product_goal_artifact_id)
            == product_goal_artifact_id,
        )
    ).one_or_none()
    return goal if decision is not None and outcome is None else None


def _next_artifact_id(session: Session) -> int:
    """Allocate one identity space shared by Vision and Backlog artifacts."""
    latest_vision = session.exec(
        select(func.max(VisionArtifact.vision_artifact_id))
    ).one()
    latest_backlog = session.exec(
        select(func.max(BacklogArtifact.backlog_artifact_id))
    ).one()
    return max(latest_vision or 0, latest_backlog or 0) + 1


def _backlog_review_guards_match(first: DecideBacklog, second: DecideBacklog) -> bool:
    return (
        first.project_id == second.project_id
        and first.graph_version == second.graph_version
        and first.fact_fingerprint == second.fact_fingerprint
        and first.decision_fingerprint == second.decision_fingerprint
        and first.instance_key == second.instance_key
        and first.attempt_id == second.attempt_id
        and first.attempt_fingerprint == second.attempt_fingerprint
        and first.backlog_artifact_id == second.backlog_artifact_id
        and first.artifact_fingerprint == second.artifact_fingerprint
    )


def validate_decide_backlog_review(
    session: Session,
    request: DecideBacklog,
) -> TransitionResult | None:
    """Fail closed when exact Backlog review guards already reached a decision."""
    existing = session.exec(
        select(BacklogArtifactDecision).where(
            col(BacklogArtifactDecision.project_id) == request.project_id,
            col(BacklogArtifactDecision.backlog_artifact_id)
            == request.backlog_artifact_id,
        )
    ).one_or_none()
    if existing is None:
        return None
    receipts = session.exec(
        select(WorkflowTransitionReceipt).where(
            col(WorkflowTransitionReceipt.request_kind) == request.kind
        )
    ).all()
    if any(
        _backlog_review_guards_match(
            DecideBacklog.model_validate_json(receipt.request_json),
            request,
        )
        for receipt in receipts
    ):
        return _conflict("Backlog artifact already has a terminal review decision.")
    return None


def execute_record_backlog_draft(
    session: Session,
    request: RecordBacklogDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Validate exact direct-Specification Backlog guards before persistence."""
    specification_matches = _accepted_specification(
        session,
        project_id=request.project_id,
        spec_version_id=request.spec_version_id,
        spec_hash=request.spec_hash,
    )
    goal = _accepted_goal(
        session,
        project_id=request.project_id,
        product_goal_artifact_id=request.product_goal_artifact_id,
        product_goal_fingerprint=request.product_goal_fingerprint,
    )
    expected_parent = _expected_parent(decision, "backlog")
    if (
        not specification_matches
        or goal is None
        or not _matches_reference(
            decision,
            fact_type="specification",
            fact_id=request.spec_version_id,
            fingerprint=request.spec_hash,
        )
        or not _matches_reference(
            decision,
            fact_type="product_goal",
            fact_id=request.product_goal_artifact_id,
            fingerprint=request.product_goal_fingerprint,
        )
        or request.supersedes_backlog_artifact_id != expected_parent
    ):
        return _conflict("RecordBacklogDraft does not target exact graph facts.")
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_draft_in_session,
    )

    try:
        row = record_backlog_draft_in_session(
            session,
            project_id=request.project_id,
            spec_version_id=request.spec_version_id,
            spec_hash=request.spec_hash,
            product_goal_artifact_id=request.product_goal_artifact_id,
            product_goal_fingerprint=request.product_goal_fingerprint,
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
            "spec_version_id": row.spec_version_id,
            "product_goal_artifact_id": row.product_goal_artifact_id,
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
        from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
            record_backlog_decision_in_session,
        )

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


__all__ = [
    "execute_decide_backlog",
    "execute_record_backlog_draft",
    "validate_decide_backlog_review",
]
