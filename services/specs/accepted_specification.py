"""Decision-grounded loading for immutable accepted Specification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError
from sqlmodel import col, select

from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
)
from models.specs import SpecRegistry
from services.contracts.specification_source import (
    SpecificationSourceBundle,
    source_bundle_fingerprint,
)
from services.specs.candidate_contract import (
    SpecificationCandidateEnvelope,
    load_candidate_contract,
)
from utils.agileforge_spec_profile_v2 import (
    SpecificationPayload,
    canonical_spec_hash,
    canonical_spec_json,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

AcceptedSpecificationStatus = Literal["approved", "superseded"]
_CANONICAL_BYTES_INVALID = "SPECIFICATION_CANONICAL_BYTES_INVALID"
_CURRENT_AMBIGUOUS = "CURRENT_SPECIFICATION_AMBIGUOUS"
_IDENTITY_MISMATCH = "SPECIFICATION_IDENTITY_MISMATCH"
_LINEAGE_INVALID = "SPECIFICATION_LINEAGE_INVALID"
_NOT_ACCEPTED = "SPECIFICATION_NOT_ACCEPTED"
_NOT_FOUND = "SPECIFICATION_NOT_FOUND"
_STALE = "STALE_SPECIFICATION"


class AcceptedSpecificationIntegrityError(RuntimeError):
    """Report one stable accepted-Specification integrity classification."""

    def __init__(self, code: str, message: str) -> None:
        """Retain a stable machine code beside the diagnostic message."""
        super().__init__(message)
        self.code: str = code


@dataclass(frozen=True)
class AcceptedSpecification:
    """One exact human-accepted canonical Specification and its proof."""

    project_id: int
    spec_version_id: int
    spec_hash: str
    status: AcceptedSpecificationStatus
    specification_decision_id: int
    accepted_at: datetime
    accepted_by: str
    acceptance_notes: str
    source_specification_candidate_id: int
    source_specification_candidate_fingerprint: str
    canonical_specification_json: str
    payload: SpecificationPayload


def _fail(code: str, message: str) -> AcceptedSpecificationIntegrityError:
    return AcceptedSpecificationIntegrityError(code, message)


def _required_id(value: int | None, *, label: str) -> int:
    if value is None:
        message = f"{label} has no identity."
        raise _fail(_IDENTITY_MISMATCH, message)
    return value


def _accepted_decision(
    session: Session,
    *,
    registry: SpecRegistry,
) -> SpecificationDecision:
    decision = session.exec(
        select(SpecificationDecision).where(
            col(SpecificationDecision.project_id) == registry.project_id,
            col(SpecificationDecision.specification_decision_id)
            == registry.source_specification_decision_id,
            col(SpecificationDecision.specification_candidate_id)
            == registry.source_specification_candidate_id,
            col(SpecificationDecision.candidate_fingerprint)
            == registry.source_specification_candidate_fingerprint,
        )
    ).one_or_none()
    if decision is None or decision.decision != "accepted":
        raise _fail(
            _NOT_ACCEPTED,
            "Specification registry is not bound to one exact accepted decision.",
        )
    return decision


def _source_candidate(
    session: Session,
    *,
    registry: SpecRegistry,
    decision: SpecificationDecision,
) -> SpecificationCandidate:
    candidate = session.exec(
        select(SpecificationCandidate).where(
            col(SpecificationCandidate.project_id) == registry.project_id,
            col(SpecificationCandidate.specification_candidate_id)
            == registry.source_specification_candidate_id,
        )
    ).one_or_none()
    if (
        candidate is None
        or candidate.candidate_fingerprint
        != registry.source_specification_candidate_fingerprint
        or candidate.specification_candidate_id != decision.specification_candidate_id
        or candidate.candidate_fingerprint != decision.candidate_fingerprint
        or candidate.payload_fingerprint != registry.spec_hash
    ):
        raise _fail(
            _IDENTITY_MISMATCH,
            "Accepted Specification candidate identity or payload hash changed.",
        )
    return candidate


def _load_canonical_payload(
    candidate: SpecificationCandidate,
) -> tuple[SpecificationPayload, SpecificationCandidateEnvelope]:
    try:
        payload, envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
    except (TypeError, ValidationError) as exc:
        raise _fail(
            _CANONICAL_BYTES_INVALID,
            "Accepted Specification candidate bytes are invalid.",
        ) from exc
    except ValueError as exc:
        identity_messages = (
            "payload fingerprint does not match",
            "review view fingerprint does not match",
            "candidate fingerprint does not match",
        )
        code = (
            _IDENTITY_MISMATCH
            if any(message in str(exc) for message in identity_messages)
            else _CANONICAL_BYTES_INVALID
        )
        message = "Accepted Specification candidate contract is corrupt."
        raise _fail(code, message) from exc
    return payload, envelope


def _validate_candidate_identity(
    *,
    registry: SpecRegistry,
    candidate: SpecificationCandidate,
    payload: SpecificationPayload,
    envelope: SpecificationCandidateEnvelope,
) -> None:
    envelope_values = (
        envelope.candidate_kind.value,
        envelope.accepted_vision_id,
        envelope.accepted_vision_fingerprint,
        envelope.accepted_product_goal_id,
        envelope.accepted_product_goal_fingerprint,
        envelope.base_specification_id,
        envelope.base_payload_fingerprint,
        envelope.registered_source_fingerprint,
        envelope.source_manifest_fingerprint,
        envelope.producer_input_fingerprint,
        envelope.review_view_fingerprint,
        envelope.workflow_node_attempt_id,
        envelope.attempt_fingerprint,
        envelope.payload_fingerprint,
        envelope.candidate_fingerprint,
    )
    candidate_values = (
        candidate.candidate_kind,
        candidate.vision_artifact_id,
        candidate.vision_fingerprint,
        candidate.product_goal_artifact_id,
        candidate.product_goal_fingerprint,
        candidate.base_spec_version_id,
        candidate.base_spec_hash,
        candidate.specification_source_fingerprint,
        candidate.source_manifest_fingerprint,
        candidate.producer_input_fingerprint,
        candidate.rendered_view_fingerprint,
        candidate.workflow_node_attempt_id,
        candidate.attempt_fingerprint,
        candidate.payload_fingerprint,
        candidate.candidate_fingerprint,
    )
    if (
        envelope_values != candidate_values
        or canonical_spec_hash(payload) != registry.spec_hash
    ):
        raise _fail(
            _IDENTITY_MISMATCH,
            "Accepted Specification duplicated candidate identities do not match.",
        )


def _validate_source_lineage(
    session: Session,
    *,
    registry: SpecRegistry,
    candidate: SpecificationCandidate,
) -> None:
    source = session.exec(
        select(SpecificationSource).where(
            col(SpecificationSource.project_id) == registry.project_id,
            col(SpecificationSource.specification_source_id)
            == candidate.specification_source_id,
            col(SpecificationSource.source_fingerprint)
            == candidate.specification_source_fingerprint,
        )
    ).one_or_none()
    lineage = (
        candidate.vision_artifact_id,
        candidate.vision_fingerprint,
        candidate.product_goal_artifact_id,
        candidate.product_goal_fingerprint,
    )
    registry_lineage = (
        registry.source_vision_artifact_id,
        registry.source_vision_fingerprint,
        registry.source_product_goal_artifact_id,
        registry.source_product_goal_fingerprint,
    )
    source_lineage = (
        None
        if source is None
        else (
            source.vision_artifact_id,
            source.vision_fingerprint,
            source.product_goal_artifact_id,
            source.product_goal_fingerprint,
        )
    )
    if source is not None:
        try:
            bundle = SpecificationSourceBundle.model_validate_json(
                source.source_bundle_json
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise _fail(
                _LINEAGE_INVALID,
                "Accepted Specification source bundle is invalid.",
            ) from exc
        bundle_lineage = (
            bundle.repository_revision.head_sha,
            bundle.repository_revision.dirty,
            bundle.repository_revision.status_fingerprint,
            bundle.accepted_vision_fingerprint,
            bundle.accepted_product_goal_fingerprint,
        )
        source_bundle_lineage = (
            source.repository_head_sha,
            source.repository_dirty,
            source.repository_status_fingerprint,
            source.vision_fingerprint,
            source.product_goal_fingerprint,
        )
        if (
            canonical_json(bundle.model_dump(mode="json")) != source.source_bundle_json
            or source_bundle_fingerprint(bundle) != source.source_fingerprint
            or bundle_lineage != source_bundle_lineage
        ):
            raise _fail(
                _LINEAGE_INVALID,
                "Accepted Specification source bundle lineage is invalid.",
            )
    vision = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == registry.project_id,
            col(VisionArtifact.vision_artifact_id) == candidate.vision_artifact_id,
            col(VisionArtifact.content_fingerprint) == candidate.vision_fingerprint,
        )
    ).one_or_none()
    vision_decision = session.exec(
        select(VisionArtifactDecision).where(
            col(VisionArtifactDecision.project_id) == registry.project_id,
            col(VisionArtifactDecision.vision_artifact_id)
            == candidate.vision_artifact_id,
            col(VisionArtifactDecision.artifact_fingerprint)
            == candidate.vision_fingerprint,
            col(VisionArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    goal = session.exec(
        select(ProductGoalArtifact).where(
            col(ProductGoalArtifact.project_id) == registry.project_id,
            col(ProductGoalArtifact.product_goal_artifact_id)
            == candidate.product_goal_artifact_id,
            col(ProductGoalArtifact.content_fingerprint)
            == candidate.product_goal_fingerprint,
        )
    ).one_or_none()
    goal_decision = session.exec(
        select(ProductGoalArtifactDecision).where(
            col(ProductGoalArtifactDecision.project_id) == registry.project_id,
            col(ProductGoalArtifactDecision.product_goal_artifact_id)
            == candidate.product_goal_artifact_id,
            col(ProductGoalArtifactDecision.artifact_fingerprint)
            == candidate.product_goal_fingerprint,
            col(ProductGoalArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if (
        source is None
        or lineage != registry_lineage
        or lineage != source_lineage
        or vision is None
        or vision_decision is None
        or goal is None
        or goal_decision is None
        or (goal.vision_artifact_id, goal.vision_fingerprint)
        != (candidate.vision_artifact_id, candidate.vision_fingerprint)
    ):
        raise _fail(
            _LINEAGE_INVALID,
            (
                "Accepted Specification source, Vision, or Product Goal lineage "
                "is invalid."
            ),
        )


def load_accepted_specification(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
) -> AcceptedSpecification:
    """Load one exact accepted historical or current contract, or raise."""
    registry = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.project_id) == project_id,
            col(SpecRegistry.spec_version_id) == spec_version_id,
            col(SpecRegistry.spec_hash) == spec_hash,
        )
    ).one_or_none()
    if registry is None:
        raise _fail(
            _NOT_FOUND,
            "Exact accepted Specification identity was not found in this Project.",
        )
    if registry.status not in {"approved", "superseded"}:
        raise _fail(
            _NOT_ACCEPTED,
            "Specification registry status is not accepted.",
        )
    decision = _accepted_decision(session, registry=registry)
    candidate = _source_candidate(session, registry=registry, decision=decision)
    payload, envelope = _load_canonical_payload(candidate)
    _validate_candidate_identity(
        registry=registry,
        candidate=candidate,
        payload=payload,
        envelope=envelope,
    )
    _validate_source_lineage(session, registry=registry, candidate=candidate)
    status: AcceptedSpecificationStatus = (
        "approved" if registry.status == "approved" else "superseded"
    )
    return AcceptedSpecification(
        project_id=project_id,
        spec_version_id=_required_id(registry.spec_version_id, label="registry row"),
        spec_hash=registry.spec_hash,
        status=status,
        specification_decision_id=_required_id(
            decision.specification_decision_id,
            label="accepted decision",
        ),
        accepted_at=decision.decided_at,
        accepted_by=decision.reviewer,
        acceptance_notes=decision.rationale,
        source_specification_candidate_id=_required_id(
            candidate.specification_candidate_id,
            label="source candidate",
        ),
        source_specification_candidate_fingerprint=candidate.candidate_fingerprint,
        canonical_specification_json=canonical_spec_json(payload),
        payload=payload,
    )


def load_current_accepted_specification(
    session: Session,
    *,
    project_id: int,
) -> AcceptedSpecification | None:
    """Return the sole current accepted contract; None means not yet accepted."""
    rows = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.project_id) == project_id,
            col(SpecRegistry.status) == "approved",
        )
    ).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise _fail(
            _CURRENT_AMBIGUOUS,
            "Project has multiple current accepted Specification rows.",
        )
    row = rows[0]
    return load_accepted_specification(
        session,
        project_id=project_id,
        spec_version_id=_required_id(row.spec_version_id, label="current registry row"),
        spec_hash=row.spec_hash,
    )


def require_current_accepted_specification(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
) -> AcceptedSpecification:
    """Resolve an exact contract and reject valid history for new planning."""
    exact = load_accepted_specification(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
    )
    if exact.status != "approved":
        raise _fail(
            _STALE,
            "New planning requires the current accepted Specification.",
        )
    current = load_current_accepted_specification(session, project_id=project_id)
    if current is None or (
        current.spec_version_id,
        current.spec_hash,
    ) != (exact.spec_version_id, exact.spec_hash):
        raise _fail(
            _STALE,
            "New planning requires the current accepted Specification.",
        )
    return exact


__all__ = [
    "AcceptedSpecification",
    "AcceptedSpecificationIntegrityError",
    "load_accepted_specification",
    "load_current_accepted_specification",
    "require_current_accepted_specification",
]
