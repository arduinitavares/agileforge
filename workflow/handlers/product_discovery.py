"""Transactional discovery and specification handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.product_definition import (
    DiscoveryArtifact,
    SpecificationCandidate,
    SpecificationDecision,
)
from models.specs import SpecRegistry
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    canonical_stored_json_hash,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.requests.product_discovery import (
        DecideSpecification,
        RecordDiscoveryArtifact,
        RecordSpecificationCandidate,
    )


def _failure(message: str) -> TransitionResult:
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


def execute_record_discovery_artifact(
    session: Session,
    request: RecordDiscoveryArtifact,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Record immutable discovery under the graph-selected Vision and Goal."""
    visions = [item for item in decision.fact_references if item.fact_type == "vision"]
    goals = [
        item for item in decision.fact_references if item.fact_type == "product_goal"
    ]
    if len(visions) != 1 or len(goals) != 1:
        return _failure("Discovery requires exact accepted Vision and Product Goal.")
    content = canonical_json(request.canonical_content)
    artifact = DiscoveryArtifact(
        project_id=request.project_id,
        vision_artifact_id=int(visions[0].fact_id),
        vision_fingerprint=visions[0].fingerprint,
        product_goal_artifact_id=int(goals[0].fact_id),
        product_goal_fingerprint=goals[0].fingerprint,
        canonical_content_json=content,
        content_fingerprint=canonical_hash(request.canonical_content),
        content_ref=request.content_ref,
        producer="grill-me-with-docs",
        supersedes_discovery_artifact_id=None,
        recorded_by=request.actor,
        recorded_at=evaluated_at,
    )
    session.add(artifact)
    session.flush()
    if artifact.discovery_artifact_id is None:
        return _failure("Discovery artifact did not receive an identity.")
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={
            "discovery_artifact_id": artifact.discovery_artifact_id,
            "discovery_fingerprint": artifact.content_fingerprint,
        },
    )


def execute_record_specification_candidate(
    session: Session,
    request: RecordSpecificationCandidate,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Record one candidate with graph-derived discovery and base-spec lineage."""
    references = [
        item for item in decision.fact_references if item.fact_type == "discovery"
    ]
    if len(references) != 1:
        return _failure("Specification requires one exact discovery artifact.")
    discovery = session.get(DiscoveryArtifact, int(references[0].fact_id))
    if discovery is None or discovery.content_fingerprint != references[0].fingerprint:
        return _failure("Specification discovery reference is stale.")
    current_specs = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.project_id) == request.project_id,
            col(SpecRegistry.status) == "approved",
        )
    ).all()
    if len(current_specs) > 1:
        return _failure("Approved specification lineage is ambiguous.")
    base = current_specs[0] if current_specs else None
    supersedes = request.supersedes_specification_candidate_id
    if supersedes is not None:
        previous = session.get(SpecificationCandidate, supersedes)
        decision_row = (
            None
            if previous is None
            else session.exec(
                select(SpecificationDecision).where(
                    col(SpecificationDecision.project_id) == request.project_id,
                    col(SpecificationDecision.specification_candidate_id) == supersedes,
                )
            ).one_or_none()
        )
        if (
            previous is None
            or decision_row is None
            or decision_row.decision not in {"rejected", "feedback"}
            or previous.discovery_artifact_id != discovery.discovery_artifact_id
        ):
            return _failure(
                "Specification replacement must supersede exact rejected feedback."
            )
    candidate = SpecificationCandidate(
        project_id=request.project_id,
        vision_artifact_id=discovery.vision_artifact_id,
        vision_fingerprint=discovery.vision_fingerprint,
        product_goal_artifact_id=discovery.product_goal_artifact_id,
        product_goal_fingerprint=discovery.product_goal_fingerprint,
        discovery_artifact_id=discovery.discovery_artifact_id,
        discovery_fingerprint=discovery.content_fingerprint,
        base_spec_version_id=None if base is None else base.spec_version_id,
        base_spec_hash=None if base is None else base.spec_hash,
        canonical_content_json=canonical_json(request.canonical_content),
        content_fingerprint=canonical_hash(request.canonical_content),
        content_ref=request.content_ref,
        supersedes_specification_candidate_id=supersedes,
        recorded_by=request.actor,
        recorded_at=evaluated_at,
    )
    session.add(candidate)
    session.flush()
    if candidate.specification_candidate_id is None:
        return _failure("Specification candidate did not receive an identity.")
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={
            "specification_candidate_id": candidate.specification_candidate_id,
            "specification_fingerprint": candidate.content_fingerprint,
        },
    )


def execute_decide_specification(
    session: Session,
    request: DecideSpecification,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append one exact review and atomically register only an accepted candidate."""
    candidate = session.get(SpecificationCandidate, request.specification_candidate_id)
    if (
        candidate is None
        or candidate.project_id != request.project_id
        or candidate.content_fingerprint != request.specification_fingerprint
        or not _reference(
            decision,
            "specification_candidate",
            request.specification_candidate_id,
            request.specification_fingerprint,
        )
        or (
            request.decision in {"rejected", "feedback"}
            and not request.rationale.strip()
        )
    ):
        return _failure("Specification review does not target the pending candidate.")
    if (
        session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == request.project_id,
                col(SpecificationDecision.specification_candidate_id)
                == request.specification_candidate_id,
            )
        ).one_or_none()
        is not None
    ):
        return _failure("Specification candidate already has a terminal decision.")
    review = SpecificationDecision(
        project_id=request.project_id,
        specification_candidate_id=request.specification_candidate_id,
        artifact_fingerprint=request.specification_fingerprint,
        decision=request.decision,
        rationale=request.rationale.strip(),
        reviewer=request.actor,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(review)
    if request.decision == "accepted":
        for row in session.exec(
            select(SpecRegistry).where(
                col(SpecRegistry.project_id) == request.project_id,
                col(SpecRegistry.status) == "approved",
            )
        ).all():
            row.status = "superseded"
        content_hash = canonical_stored_json_hash(candidate.canonical_content_json)
        session.add(
            SpecRegistry(
                project_id=request.project_id,
                spec_hash=content_hash,
                content=candidate.canonical_content_json,
                content_ref=candidate.content_ref,
                status="approved",
                approved_at=evaluated_at,
                approved_by=request.actor,
                approval_notes=request.rationale.strip() or None,
                source_specification_candidate_id=candidate.specification_candidate_id,
                source_vision_artifact_id=candidate.vision_artifact_id,
                source_vision_fingerprint=candidate.vision_fingerprint,
                source_product_goal_artifact_id=candidate.product_goal_artifact_id,
                source_product_goal_fingerprint=candidate.product_goal_fingerprint,
                source_discovery_artifact_id=candidate.discovery_artifact_id,
                source_discovery_fingerprint=candidate.discovery_fingerprint,
                supersedes_spec_version_id=candidate.base_spec_version_id,
            )
        )
    session.flush()
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={"specification_decision_id": review.specification_decision_id},
    )
