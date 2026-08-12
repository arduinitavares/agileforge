"""Transactional Specification source, structuring, and review handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import Project
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
)
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_STRUCTURER_PROMPT_HASH,
    SPECIFICATION_STRUCTURER_PROMPT_VERSION,
    SPECIFICATION_STRUCTURER_VERSION,
    SPECIFICATION_VISION_SOURCE_ID,
    SpecificationStructuringInput,
    specification_structuring_fact_fingerprint,
    specification_structuring_input_fingerprint,
)
from services.contracts.specification_source import source_bundle_fingerprint
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    SpecificationCandidateEnvelope,
    build_candidate_envelope,
    canonical_candidate_json,
    load_candidate_contract,
)
from workflow.contracts import (
    FactReference,
    JsonObject,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.definitions.product_discovery import current_specification_source
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from utils.agileforge_spec_profile_v2 import SpecificationPayload
    from workflow.requests.product_discovery import (
        CompleteSpecificationStructuring,
        DecideSpecification,
        RegisterSpecificationSource,
    )

_JSON_OBJECT = TypeAdapter(JsonObject)
_PRODUCER_CAPABILITY = "specification-structurer"


class _GuardError(Exception):
    """Internal closed failure used to keep handlers fail-closed and linear."""

    def __init__(self, code: WorkflowErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _CandidateLineage:
    """Host-derived lineage selected from an exact graph decision."""

    vision: VisionArtifact
    goal: ProductGoalArtifact
    candidate_kind: CandidateKind
    base: SpecRegistry | None
    base_payload: SpecificationPayload | None
    supersedes: SpecificationCandidate | None
    source: SpecificationSource


@dataclass(frozen=True)
class _SourceRegistrationState:
    """Validated durable source leaf and canonical prepared bytes."""

    current: SpecificationSource | None
    bundle_json: str


def _failure(code: WorkflowErrorCode, message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(code=code, message=message),
    )


def _current_source_row(
    session: Session,
    *,
    project_id: int,
) -> SpecificationSource | None:
    """Resolve the sole durable source leaf or fail on a branched history."""
    rows = tuple(
        session.exec(
            select(SpecificationSource)
            .where(col(SpecificationSource.project_id) == project_id)
            .order_by(col(SpecificationSource.specification_source_id))
        ).all()
    )
    superseded = {
        row.supersedes_specification_source_id
        for row in rows
        if row.supersedes_specification_source_id is not None
    }
    leaves = tuple(row for row in rows if row.specification_source_id not in superseded)
    if len(leaves) > 1:
        raise _GuardError(
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            "Specification source history has multiple current rows.",
        )
    return leaves[0] if leaves else None


def _validate_registration_references(
    session: Session,
    *,
    request: RegisterSpecificationSource,
    decision: NodeDecision,
) -> tuple[VisionArtifact, ProductGoalArtifact]:
    """Bind every graph reference to current durable product-definition facts."""
    allowed = {
        "vision",
        "product_goal",
        "specification_source",
        "specification_candidate",
        "specification",
    }
    if any(item.fact_type not in allowed for item in decision.fact_references):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "Specification source registration contains an unknown graph source.",
        )
    vision_ref = cast("FactReference", _reference(decision, "vision", required=True))
    goal_ref = cast(
        "FactReference", _reference(decision, "product_goal", required=True)
    )
    vision = _accepted_vision(
        session,
        project_id=request.project_id,
        reference=vision_ref,
    )
    goal = _accepted_goal(
        session,
        project_id=request.project_id,
        reference=goal_ref,
        vision=vision,
    )
    try:
        snapshot = WorkflowFactRepository(session).load(request.project_id)
    except WorkflowFactLoadError as error:
        raise _GuardError(
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            "Current Specification source facts are invalid.",
        ) from error
    current_vision = accepted_current_vision(snapshot)
    current_goal = accepted_current_goal(snapshot)
    if (
        current_vision is None
        or current_goal is None
        or (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )
        != (
            current_vision.vision_artifact_id,
            current_vision.content_fingerprint,
            current_goal.product_goal_artifact_id,
            current_goal.content_fingerprint,
        )
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "Specification source registration does not use the current "
            "Vision and Goal.",
        )

    current_source = current_specification_source(snapshot)
    source_ref = _reference(decision, "specification_source", required=False)
    expected_source = (
        None
        if current_source is None
        else (
            str(current_source.specification_source_id),
            current_source.source_fingerprint,
        )
    )
    actual_source = (
        None if source_ref is None else (source_ref.fact_id, source_ref.fingerprint)
    )
    if actual_source != expected_source:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The referenced Specification source is no longer current.",
        )

    candidate_ref = _reference(
        decision,
        "specification_candidate",
        required=False,
    )
    if candidate_ref is not None:
        candidate = session.get(SpecificationCandidate, int(candidate_ref.fact_id))
        if (
            candidate is None
            or candidate.project_id != request.project_id
            or candidate.candidate_fingerprint != candidate_ref.fingerprint
            or current_source is None
            or (
                candidate.specification_source_id,
                candidate.specification_source_fingerprint,
            )
            != (
                current_source.specification_source_id,
                current_source.source_fingerprint,
            )
        ):
            raise _GuardError(
                WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                "The referenced candidate does not belong to the current source.",
            )
    specification_ref = _reference(
        decision,
        "specification",
        required=False,
    )
    if specification_ref is not None:
        _spec_by_reference(
            session,
            project_id=request.project_id,
            reference=specification_ref,
        )
    if candidate_ref is not None and specification_ref is not None:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "Source registration cannot revise and amend simultaneously.",
        )
    return vision, goal


def _registration_state(
    session: Session,
    *,
    request: RegisterSpecificationSource,
    decision: NodeDecision,
) -> _SourceRegistrationState:
    """Revalidate prepared source semantics against current transaction facts."""
    vision, goal = _validate_registration_references(
        session,
        request=request,
        decision=decision,
    )
    project = session.get(Project, request.project_id)
    binding = session.get(RepositoryBinding, request.repository_binding_id)
    revision = request.bundle.repository_revision
    if (
        project is None
        or project.active_repository_binding_id != request.repository_binding_id
        or binding is None
        or binding.project_id != request.project_id
        or repository_binding_fingerprint(binding)
        != request.repository_binding_fingerprint
        or (revision.head_sha, revision.dirty, revision.status_fingerprint)
        != (binding.head_sha, binding.dirty, binding.status_fingerprint)
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The active repository binding changed before source registration.",
        )
    if (
        request.accepted_vision_artifact_id,
        request.bundle.accepted_vision_fingerprint,
        request.accepted_product_goal_artifact_id,
        request.bundle.accepted_product_goal_fingerprint,
    ) != (
        vision.vision_artifact_id,
        vision.content_fingerprint,
        goal.product_goal_artifact_id,
        goal.content_fingerprint,
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The prepared source bundle has stale product-definition lineage.",
        )
    bundle_json = canonical_json(request.bundle.model_dump(mode="json"))
    if source_bundle_fingerprint(request.bundle) != request.source_fingerprint:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The prepared source fingerprint does not match its exact bundle.",
        )
    current = _current_source_row(session, project_id=request.project_id)
    if (
        current is not None
        and current.source_fingerprint == request.source_fingerprint
        and current.repository_binding_id == request.repository_binding_id
        and current.source_bundle_json != bundle_json
    ):
        raise _GuardError(
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            "The current source fingerprint resolves to different bytes.",
        )
    return _SourceRegistrationState(current=current, bundle_json=bundle_json)


def execute_register_specification_source(
    session: Session,
    request: RegisterSpecificationSource,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Revalidate and persist one immutable external source inside the transition."""
    try:
        state = _registration_state(
            session,
            request=request,
            decision=decision,
        )
    except _GuardError as error:
        return _failure(error.code, error.message)
    current = state.current
    bundle_json = state.bundle_json
    revision = request.bundle.repository_revision
    if (
        current is not None
        and current.source_fingerprint == request.source_fingerprint
        and current.repository_binding_id == request.repository_binding_id
        and current.vision_artifact_id == request.accepted_vision_artifact_id
        and current.vision_fingerprint == request.bundle.accepted_vision_fingerprint
        and current.product_goal_artifact_id
        == request.accepted_product_goal_artifact_id
        and current.product_goal_fingerprint
        == request.bundle.accepted_product_goal_fingerprint
    ):
        return TransitionResult(
            ok=True,
            applied_node_id=decision.node_id,
            output={
                "specification_source_id": current.specification_source_id,
                "source_fingerprint": current.source_fingerprint,
                "repository_binding_id": current.repository_binding_id,
                "created": False,
            },
        )

    source = SpecificationSource(
        project_id=request.project_id,
        source_bundle_json=bundle_json,
        source_fingerprint=request.source_fingerprint,
        repository_binding_id=request.repository_binding_id,
        repository_head_sha=revision.head_sha,
        repository_dirty=revision.dirty,
        repository_status_fingerprint=revision.status_fingerprint,
        vision_artifact_id=request.accepted_vision_artifact_id,
        vision_fingerprint=request.bundle.accepted_vision_fingerprint,
        product_goal_artifact_id=request.accepted_product_goal_artifact_id,
        product_goal_fingerprint=request.bundle.accepted_product_goal_fingerprint,
        supersedes_specification_source_id=(
            None if current is None else current.specification_source_id
        ),
        supersedes_source_fingerprint=(
            None if current is None else current.source_fingerprint
        ),
        registered_by=request.actor,
        registered_at=evaluated_at,
    )
    session.add(source)
    session.flush()
    if source.specification_source_id is None:
        return _failure(
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            "Specification source did not receive a durable identity.",
        )
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={
            "specification_source_id": source.specification_source_id,
            "source_fingerprint": source.source_fingerprint,
            "repository_binding_id": source.repository_binding_id,
            "created": True,
        },
    )


def _prompt_fingerprint() -> str:
    """Load the prompt identity owned by the structuring adapter contract."""
    return SPECIFICATION_STRUCTURER_PROMPT_HASH


def _reference(
    decision: NodeDecision,
    kind: str,
    *,
    required: bool,
) -> FactReference | None:
    matches = [item for item in decision.fact_references if item.fact_type == kind]
    if len(matches) > 1 or (required and not matches):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            f"Specification structuring requires one exact {kind} reference.",
        )
    return None if not matches else matches[0]


def _accepted_vision(
    session: Session,
    *,
    project_id: int,
    reference: FactReference,
) -> VisionArtifact:
    vision = session.get(VisionArtifact, int(reference.fact_id))
    decision = session.exec(
        select(VisionArtifactDecision).where(
            col(VisionArtifactDecision.project_id) == project_id,
            col(VisionArtifactDecision.vision_artifact_id) == int(reference.fact_id),
            col(VisionArtifactDecision.artifact_fingerprint) == reference.fingerprint,
            col(VisionArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if (
        vision is None
        or vision.project_id != project_id
        or vision.content_fingerprint != reference.fingerprint
        or decision is None
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The accepted Vision source is stale.",
        )
    return vision


def _accepted_goal(
    session: Session,
    *,
    project_id: int,
    reference: FactReference,
    vision: VisionArtifact,
) -> ProductGoalArtifact:
    goal = session.get(ProductGoalArtifact, int(reference.fact_id))
    goal_decision = session.exec(
        select(ProductGoalArtifactDecision).where(
            col(ProductGoalArtifactDecision.project_id) == project_id,
            col(ProductGoalArtifactDecision.product_goal_artifact_id)
            == int(reference.fact_id),
            col(ProductGoalArtifactDecision.artifact_fingerprint)
            == reference.fingerprint,
            col(ProductGoalArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if (
        goal is None
        or goal.project_id != project_id
        or goal.content_fingerprint != reference.fingerprint
        or goal.vision_artifact_id != vision.vision_artifact_id
        or goal.vision_fingerprint != vision.content_fingerprint
        or goal_decision is None
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The accepted Product Goal source is stale.",
        )
    return goal


def _load_candidate(
    candidate: SpecificationCandidate,
) -> tuple[SpecificationPayload, SpecificationCandidateEnvelope]:
    try:
        payload, envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise _GuardError(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "The immutable Specification candidate bytes are invalid.",
        ) from exc
    expected = (
        envelope.candidate_kind.value,
        envelope.accepted_vision_id,
        envelope.accepted_vision_fingerprint,
        envelope.accepted_product_goal_id,
        envelope.accepted_product_goal_fingerprint,
        envelope.base_specification_id,
        envelope.base_payload_fingerprint,
        envelope.payload_fingerprint,
        envelope.source_manifest_fingerprint,
        envelope.producer_input_fingerprint,
        envelope.review_view_fingerprint,
        envelope.candidate_fingerprint,
        envelope.workflow_node_attempt_id,
        envelope.attempt_fingerprint,
    )
    actual = (
        candidate.candidate_kind,
        candidate.vision_artifact_id,
        candidate.vision_fingerprint,
        candidate.product_goal_artifact_id,
        candidate.product_goal_fingerprint,
        candidate.base_spec_version_id,
        candidate.base_spec_hash,
        candidate.payload_fingerprint,
        candidate.source_manifest_fingerprint,
        candidate.producer_input_fingerprint,
        candidate.rendered_view_fingerprint,
        candidate.candidate_fingerprint,
        candidate.workflow_node_attempt_id,
        candidate.attempt_fingerprint,
    )
    if actual != expected:
        raise _GuardError(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "The Specification candidate columns do not match its immutable bytes.",
        )
    return payload, envelope


def _candidate_by_reference(
    session: Session,
    *,
    project_id: int,
    reference: FactReference,
) -> SpecificationCandidate:
    candidate = session.get(SpecificationCandidate, int(reference.fact_id))
    if (
        candidate is None
        or candidate.project_id != project_id
        or candidate.candidate_fingerprint != reference.fingerprint
    ):
        raise _GuardError(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "The referenced Specification candidate is stale.",
        )
    return candidate


def _spec_by_reference(
    session: Session,
    *,
    project_id: int,
    reference: FactReference,
) -> SpecRegistry:
    spec = session.get(SpecRegistry, int(reference.fact_id))
    if (
        spec is None
        or spec.project_id != project_id
        or spec.status != "approved"
        or spec.spec_hash != reference.fingerprint
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "The accepted Specification amendment base is stale.",
        )
    return spec


def _base_payload(
    session: Session,
    *,
    project_id: int,
    base: SpecRegistry,
) -> SpecificationPayload:
    source = session.get(
        SpecificationCandidate,
        base.source_specification_candidate_id,
    )
    if (
        source is None
        or source.project_id != project_id
        or source.candidate_fingerprint
        != base.source_specification_candidate_fingerprint
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "The amendment base candidate is stale.",
        )
    payload, _envelope = _load_candidate(source)
    accepted = session.exec(
        select(SpecificationDecision).where(
            col(SpecificationDecision.project_id) == project_id,
            col(SpecificationDecision.specification_candidate_id)
            == source.specification_candidate_id,
            col(SpecificationDecision.candidate_fingerprint)
            == source.candidate_fingerprint,
            col(SpecificationDecision.decision) == "accepted",
        )
    ).one_or_none()
    if accepted is None or source.payload_fingerprint != base.spec_hash:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "The amendment base is not an exact accepted payload.",
        )
    return payload


def _validate_base_lineage(
    base: SpecRegistry,
    vision: VisionArtifact,
    goal: ProductGoalArtifact,
) -> None:
    if (
        base.source_vision_artifact_id,
        base.source_vision_fingerprint,
        base.source_product_goal_artifact_id,
        base.source_product_goal_fingerprint,
    ) != (
        vision.vision_artifact_id,
        vision.content_fingerprint,
        goal.product_goal_artifact_id,
        goal.content_fingerprint,
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "The amendment base belongs to different product-definition facts.",
        )


def _lineage_for_completion(  # noqa: C901
    session: Session,
    request: CompleteSpecificationStructuring,
    decision: NodeDecision,
) -> _CandidateLineage:
    allowed = {
        "vision",
        "product_goal",
        "specification_source",
        "specification",
        "specification_candidate",
    }
    if any(item.fact_type not in allowed for item in decision.fact_references):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "Specification structuring contains an unknown graph source.",
        )
    vision_ref = cast("FactReference", _reference(decision, "vision", required=True))
    goal_ref = cast(
        "FactReference", _reference(decision, "product_goal", required=True)
    )
    base_ref = _reference(decision, "specification", required=False)
    prior_ref = _reference(decision, "specification_candidate", required=False)
    source_ref = cast(
        "FactReference",
        _reference(decision, "specification_source", required=True),
    )
    if base_ref is not None and prior_ref is not None:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "Specification structuring cannot revise and amend simultaneously.",
        )
    vision = _accepted_vision(
        session,
        project_id=request.project_id,
        reference=vision_ref,
    )
    goal = _accepted_goal(
        session,
        project_id=request.project_id,
        reference=goal_ref,
        vision=vision,
    )
    source = session.get(SpecificationSource, int(source_ref.fact_id))
    current_source = _current_source_row(session, project_id=request.project_id)
    if (
        source is None
        or source.project_id != request.project_id
        or source.source_fingerprint != source_ref.fingerprint
        or current_source is None
        or source.specification_source_id != current_source.specification_source_id
        or source.source_fingerprint != current_source.source_fingerprint
        or (source.vision_artifact_id, source.vision_fingerprint)
        != (vision.vision_artifact_id, vision.content_fingerprint)
        or (source.product_goal_artifact_id, source.product_goal_fingerprint)
        != (goal.product_goal_artifact_id, goal.content_fingerprint)
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The registered Specification source is no longer current.",
        )
    if prior_ref is not None:
        prior = _candidate_by_reference(
            session,
            project_id=request.project_id,
            reference=prior_ref,
        )
        _prior_payload, prior_envelope = _load_candidate(prior)
        terminal = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == request.project_id,
                col(SpecificationDecision.specification_candidate_id)
                == prior.specification_candidate_id,
                col(SpecificationDecision.candidate_fingerprint)
                == prior.candidate_fingerprint,
            )
        ).one_or_none()
        successor = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == request.project_id,
                col(SpecificationCandidate.supersedes_specification_candidate_id)
                == prior.specification_candidate_id,
            )
        ).one_or_none()
        if (
            terminal is None
            or terminal.decision not in {"rejected", "feedback"}
            or successor is not None
            or (
                prior.vision_artifact_id,
                prior.vision_fingerprint,
                prior.product_goal_artifact_id,
                prior.product_goal_fingerprint,
            )
            != (
                vision.vision_artifact_id,
                vision.content_fingerprint,
                goal.product_goal_artifact_id,
                goal.content_fingerprint,
            )
        ):
            raise _GuardError(
                WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
                "Specification revision does not supersede exact rejected feedback.",
            )
        if prior_envelope.candidate_kind is CandidateKind.INITIAL:
            return _CandidateLineage(
                vision=vision,
                goal=goal,
                candidate_kind=CandidateKind.INITIAL,
                base=None,
                base_payload=None,
                supersedes=prior,
                source=source,
            )
        if prior.base_spec_version_id is None or prior.base_spec_hash is None:
            raise _GuardError(
                WorkflowErrorCode.STALE_SPECIFICATION_BASE,
                "The rejected amendment has no exact base.",
            )
        base = session.get(SpecRegistry, prior.base_spec_version_id)
        if (
            base is None
            or base.project_id != request.project_id
            or base.status != "approved"
            or base.spec_hash != prior.base_spec_hash
        ):
            raise _GuardError(
                WorkflowErrorCode.STALE_SPECIFICATION_BASE,
                "The rejected amendment base is no longer current.",
            )
        _validate_base_lineage(base, vision, goal)
        return _CandidateLineage(
            vision=vision,
            goal=goal,
            candidate_kind=CandidateKind.AMENDMENT,
            base=base,
            base_payload=_base_payload(
                session,
                project_id=request.project_id,
                base=base,
            ),
            supersedes=prior,
            source=source,
        )
    if base_ref is not None:
        base = _spec_by_reference(
            session,
            project_id=request.project_id,
            reference=base_ref,
        )
        _validate_base_lineage(base, vision, goal)
        return _CandidateLineage(
            vision=vision,
            goal=goal,
            candidate_kind=CandidateKind.AMENDMENT,
            base=base,
            base_payload=_base_payload(
                session,
                project_id=request.project_id,
                base=base,
            ),
            supersedes=None,
            source=source,
        )

    candidates = session.exec(
        select(SpecificationCandidate).where(
            col(SpecificationCandidate.project_id) == request.project_id,
            col(SpecificationCandidate.vision_artifact_id) == vision.vision_artifact_id,
            col(SpecificationCandidate.vision_fingerprint)
            == vision.content_fingerprint,
            col(SpecificationCandidate.product_goal_artifact_id)
            == goal.product_goal_artifact_id,
            col(SpecificationCandidate.product_goal_fingerprint)
            == goal.content_fingerprint,
            col(SpecificationCandidate.specification_source_id)
            == source.specification_source_id,
            col(SpecificationCandidate.specification_source_fingerprint)
            == source.source_fingerprint,
        )
    ).all()
    superseded = {
        item.supersedes_specification_candidate_id
        for item in candidates
        if item.supersedes_specification_candidate_id is not None
    }
    if any(item.specification_candidate_id not in superseded for item in candidates):
        raise _GuardError(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "Initial structuring already has a current candidate.",
        )
    return _CandidateLineage(
        vision=vision,
        goal=goal,
        candidate_kind=CandidateKind.INITIAL,
        base=None,
        base_payload=None,
        supersedes=None,
        source=source,
    )


def _attempt(
    session: Session,
    request: CompleteSpecificationStructuring,
) -> WorkflowNodeAttempt:
    attempt = session.get(WorkflowNodeAttempt, request.attempt_id)
    if (
        attempt is None
        or attempt.project_id != request.project_id
        or attempt.node_id != "specification.structure"
        or attempt.attempt_fingerprint != request.attempt_fingerprint
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The durable Specification structuring attempt is stale.",
        )
    return attempt


def _attempt_inputs(
    attempt: WorkflowNodeAttempt,
) -> tuple[
    SpecificationStructuringInput,
    tuple[CandidateSourceManifestEntry, ...],
    JsonObject,
]:
    try:
        normalized_input = _JSON_OBJECT.validate_json(attempt.normalized_input_json)
        execution_settings = _JSON_OBJECT.validate_json(attempt.execution_settings_json)
        contract = SpecificationStructuringInput.model_validate(normalized_input)
    except (ValidationError, TypeError, ValueError) as exc:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The persisted Specification structuring input is invalid.",
        ) from exc
    if (
        canonical_json(normalized_input) != attempt.normalized_input_json
        or canonical_hash(normalized_input) != attempt.input_fingerprint
        or canonical_json(execution_settings) != attempt.execution_settings_json
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The persisted Specification structuring input fingerprint changed.",
        )
    return contract, contract.source_manifest, execution_settings


def _validate_attempt_contract(
    contract: SpecificationStructuringInput,
    lineage: _CandidateLineage,
) -> None:
    operation = (
        "revision"
        if lineage.supersedes is not None
        else (
            "amendment"
            if lineage.candidate_kind is CandidateKind.AMENDMENT
            else "initial"
        )
    )
    base_identity = (
        None
        if contract.base_specification is None
        else (
            contract.base_specification.spec_version_id,
            contract.base_specification.payload_fingerprint,
        )
    )
    expected_base_identity = (
        None
        if lineage.base is None
        else (lineage.base.spec_version_id, lineage.base.spec_hash)
    )
    prior_fingerprint = (
        None
        if contract.prior_candidate is None
        else contract.prior_candidate.candidate_fingerprint
    )
    expected_prior_fingerprint = (
        None if lineage.supersedes is None else lineage.supersedes.candidate_fingerprint
    )
    if (
        contract.project_id,
        contract.operation,
        contract.accepted_vision.artifact_id,
        contract.accepted_vision.fingerprint,
        contract.accepted_product_goal.artifact_id,
        contract.accepted_product_goal.fingerprint,
        contract.registered_source.specification_source_id,
        contract.registered_source.source_fingerprint,
        base_identity,
        prior_fingerprint,
    ) != (
        lineage.vision.project_id,
        operation,
        lineage.vision.vision_artifact_id,
        lineage.vision.content_fingerprint,
        lineage.goal.product_goal_artifact_id,
        lineage.goal.content_fingerprint,
        lineage.source.specification_source_id,
        lineage.source.source_fingerprint,
        expected_base_identity,
        expected_prior_fingerprint,
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The persisted structuring context does not match the graph decision.",
        )


def _validate_manifest(
    *,
    lineage: _CandidateLineage,
    manifest: tuple[CandidateSourceManifestEntry, ...],
    payload: SpecificationPayload,
) -> None:
    by_id = {entry.source_id: entry for entry in manifest}
    if len(by_id) != len(manifest):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The host source manifest contains duplicate identities.",
        )
    required = {
        SPECIFICATION_VISION_SOURCE_ID: (
            CandidateSourceKind.VISION,
            lineage.vision.content_fingerprint,
        ),
        SPECIFICATION_PRODUCT_GOAL_SOURCE_ID: (
            CandidateSourceKind.PRODUCT_GOAL,
            lineage.goal.content_fingerprint,
        ),
    }
    for source_id, (kind, fingerprint) in required.items():
        entry = by_id.get(source_id)
        if entry is None or (entry.kind, entry.fingerprint) != (kind, fingerprint):
            raise _GuardError(
                WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                f"The host source manifest does not match {source_id}.",
            )
    referenced_sources = {
        note.source_id for item in payload.items for note in item.source_notes
    }
    missing = sorted(referenced_sources - set(by_id))
    if missing:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "Specification source notes reference sources outside the host manifest: "
            + ", ".join(missing),
        )


def _model_configuration_fingerprint(
    attempt: WorkflowNodeAttempt,
    execution_settings: JsonObject,
) -> str:
    return canonical_hash(
        {
            "model_id": attempt.model_id,
            "execution_settings": execution_settings,
        }
    )


def execute_complete_specification_structuring(
    session: Session,
    request: CompleteSpecificationStructuring,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Bind provider semantics to the exact host attempt and graph lineage."""
    try:
        lineage = _lineage_for_completion(session, request, decision)
        attempt = _attempt(session, request)
        structuring_input, manifest, execution_settings = _attempt_inputs(attempt)
        _validate_attempt_contract(structuring_input, lineage)
        _validate_manifest(
            lineage=lineage,
            manifest=manifest,
            payload=request.payload,
        )
        envelope = build_candidate_envelope(
            payload=request.payload,
            metadata=CandidateBuildInput(
                candidate_kind=lineage.candidate_kind,
                accepted_vision_id=cast("int", lineage.vision.vision_artifact_id),
                accepted_vision_fingerprint=lineage.vision.content_fingerprint,
                accepted_product_goal_id=cast(
                    "int", lineage.goal.product_goal_artifact_id
                ),
                accepted_product_goal_fingerprint=lineage.goal.content_fingerprint,
                registered_source_fingerprint=lineage.source.source_fingerprint,
                source_producer_capability=(
                    structuring_input.registered_source.producer_capability
                ),
                source_preparation_capability=(
                    structuring_input.registered_source.preparation_capability
                ),
                source_manifest=manifest,
                accepted_fact_fingerprint=(
                    specification_structuring_fact_fingerprint(structuring_input)
                ),
                producer_input_fingerprint=(
                    specification_structuring_input_fingerprint(structuring_input)
                ),
                producer_capability=_PRODUCER_CAPABILITY,
                producer_version=SPECIFICATION_STRUCTURER_VERSION,
                model_id=attempt.model_id,
                model_configuration_fingerprint=(
                    _model_configuration_fingerprint(attempt, execution_settings)
                ),
                prompt_version=SPECIFICATION_STRUCTURER_PROMPT_VERSION,
                prompt_fingerprint=_prompt_fingerprint(),
                workflow_node_attempt_id=request.attempt_id,
                attempt_fingerprint=request.attempt_fingerprint,
                correlation_id=(
                    attempt.correlation_id or f"workflow-attempt:{request.attempt_id}"
                ),
                produced_at=evaluated_at,
                base_payload=lineage.base_payload,
                base_specification_id=(
                    None if lineage.base is None else lineage.base.spec_version_id
                ),
                base_payload_fingerprint=(
                    None if lineage.base is None else lineage.base.spec_hash
                ),
                removal_justifications=request.removal_justifications,
                stable_id_replacements=request.stable_id_replacements,
            ),
        )
        serialized = canonical_candidate_json(request.payload, envelope)
    except _GuardError as exc:
        return _failure(exc.code, exc.message)
    except (TypeError, ValueError, ValidationError):
        return _failure(
            WorkflowErrorCode.SPECIFICATION_AMENDMENT_MISMATCH,
            "The Specification amendment manifest does not match its exact base.",
        )

    candidate = SpecificationCandidate(
        project_id=request.project_id,
        candidate_kind=envelope.candidate_kind.value,
        specification_source_id=cast("int", lineage.source.specification_source_id),
        specification_source_fingerprint=lineage.source.source_fingerprint,
        vision_artifact_id=lineage.vision.vision_artifact_id,
        vision_fingerprint=lineage.vision.content_fingerprint,
        product_goal_artifact_id=lineage.goal.product_goal_artifact_id,
        product_goal_fingerprint=lineage.goal.content_fingerprint,
        base_spec_version_id=envelope.base_specification_id,
        base_spec_hash=envelope.base_payload_fingerprint,
        canonical_envelope_json=serialized,
        payload_fingerprint=envelope.payload_fingerprint,
        source_manifest_fingerprint=envelope.source_manifest_fingerprint,
        producer_input_fingerprint=envelope.producer_input_fingerprint,
        rendered_view_fingerprint=envelope.review_view_fingerprint,
        candidate_fingerprint=envelope.candidate_fingerprint,
        workflow_node_attempt_id=request.attempt_id,
        attempt_fingerprint=request.attempt_fingerprint,
        supersedes_specification_candidate_id=(
            None
            if lineage.supersedes is None
            else lineage.supersedes.specification_candidate_id
        ),
        supersedes_candidate_fingerprint=(
            None
            if lineage.supersedes is None
            else lineage.supersedes.candidate_fingerprint
        ),
        recorded_by=attempt.actor,
        recorded_at=evaluated_at,
    )
    session.add(candidate)
    session.flush()
    if candidate.specification_candidate_id is None:
        return _failure(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "Specification candidate did not receive a durable identity.",
        )
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={
            "specification_candidate_id": candidate.specification_candidate_id,
            "candidate_fingerprint": candidate.candidate_fingerprint,
            "payload_fingerprint": candidate.payload_fingerprint,
        },
    )


def _validate_candidate_attempt(  # noqa: PLR0913
    session: Session,
    *,
    candidate: SpecificationCandidate,
    envelope: SpecificationCandidateEnvelope,
    payload: SpecificationPayload,
    lineage: _CandidateLineage,
    repository_source_fingerprint: str | None,
    require_current_repository_source: bool,
) -> None:
    attempt = session.get(WorkflowNodeAttempt, candidate.workflow_node_attempt_id)
    outcome = session.exec(
        select(WorkflowNodeAttemptOutcome).where(
            col(WorkflowNodeAttemptOutcome.project_id) == candidate.project_id,
            col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id)
            == candidate.workflow_node_attempt_id,
        )
    ).one_or_none()
    if (
        attempt is None
        or attempt.project_id != candidate.project_id
        or attempt.node_id != "specification.structure"
        or attempt.attempt_fingerprint != candidate.attempt_fingerprint
        or outcome is None
        or outcome.status != "success"
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The candidate structuring attempt is not an exact successful attempt.",
        )
    structuring_input, manifest, settings = _attempt_inputs(attempt)
    expected_repository_fingerprint = lineage.source.source_fingerprint
    if (
        require_current_repository_source
        and repository_source_fingerprint != expected_repository_fingerprint
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The current registered source does not match the candidate source.",
        )
    _validate_attempt_contract(structuring_input, lineage)
    _validate_manifest(
        lineage=lineage,
        manifest=manifest,
        payload=payload,
    )
    if (
        envelope.source_manifest
        != tuple(sorted(manifest, key=lambda item: item.source_id))
        or envelope.accepted_fact_fingerprint
        != specification_structuring_fact_fingerprint(structuring_input)
        or envelope.producer_input_fingerprint
        != specification_structuring_input_fingerprint(structuring_input)
        or envelope.registered_source_fingerprint != lineage.source.source_fingerprint
        or envelope.source_producer_capability
        != structuring_input.registered_source.producer_capability
        or envelope.source_preparation_capability
        != structuring_input.registered_source.preparation_capability
        or envelope.model_id != attempt.model_id
        or envelope.model_configuration_fingerprint
        != _model_configuration_fingerprint(attempt, settings)
        or envelope.prompt_fingerprint != _prompt_fingerprint()
        or envelope.prompt_version != SPECIFICATION_STRUCTURER_PROMPT_VERSION
        or envelope.producer_capability != _PRODUCER_CAPABILITY
        or envelope.producer_version != SPECIFICATION_STRUCTURER_VERSION
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The candidate producer metadata does not match its durable attempt.",
        )


def _lineage_for_review(
    session: Session,
    candidate: SpecificationCandidate,
) -> _CandidateLineage:
    vision = session.get(VisionArtifact, candidate.vision_artifact_id)
    goal = session.get(ProductGoalArtifact, candidate.product_goal_artifact_id)
    if vision is None or goal is None:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The candidate product-definition sources are missing.",
        )
    vision_ref = FactReference(
        fact_type="vision",
        fact_id=str(vision.vision_artifact_id),
        fingerprint=candidate.vision_fingerprint,
    )
    goal_ref = FactReference(
        fact_type="product_goal",
        fact_id=str(goal.product_goal_artifact_id),
        fingerprint=candidate.product_goal_fingerprint,
    )
    accepted_vision = _accepted_vision(
        session,
        project_id=candidate.project_id,
        reference=vision_ref,
    )
    accepted_goal = _accepted_goal(
        session,
        project_id=candidate.project_id,
        reference=goal_ref,
        vision=accepted_vision,
    )
    source = session.get(SpecificationSource, candidate.specification_source_id)
    if (
        source is None
        or source.project_id != candidate.project_id
        or source.source_fingerprint != candidate.specification_source_fingerprint
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            "The candidate registered source is unavailable.",
        )
    supersedes: SpecificationCandidate | None = None
    if candidate.supersedes_specification_candidate_id is not None:
        supersedes = _candidate_by_reference(
            session,
            project_id=candidate.project_id,
            reference=FactReference(
                fact_type="specification_candidate",
                fact_id=str(candidate.supersedes_specification_candidate_id),
                fingerprint=cast("str", candidate.supersedes_candidate_fingerprint),
            ),
        )
        _load_candidate(supersedes)
        terminal = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == candidate.project_id,
                col(SpecificationDecision.specification_candidate_id)
                == supersedes.specification_candidate_id,
                col(SpecificationDecision.candidate_fingerprint)
                == supersedes.candidate_fingerprint,
            )
        ).one_or_none()
        if (
            terminal is None
            or terminal.decision not in {"rejected", "feedback"}
            or (
                supersedes.candidate_kind,
                supersedes.vision_artifact_id,
                supersedes.vision_fingerprint,
                supersedes.product_goal_artifact_id,
                supersedes.product_goal_fingerprint,
                supersedes.base_spec_version_id,
                supersedes.base_spec_hash,
            )
            != (
                candidate.candidate_kind,
                candidate.vision_artifact_id,
                candidate.vision_fingerprint,
                candidate.product_goal_artifact_id,
                candidate.product_goal_fingerprint,
                candidate.base_spec_version_id,
                candidate.base_spec_hash,
            )
        ):
            raise _GuardError(
                WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
                "The Specification revision does not match exact rejected feedback.",
            )
    if candidate.candidate_kind == CandidateKind.INITIAL.value:
        return _CandidateLineage(
            vision=accepted_vision,
            goal=accepted_goal,
            candidate_kind=CandidateKind.INITIAL,
            base=None,
            base_payload=None,
            supersedes=supersedes,
            source=source,
        )
    if candidate.base_spec_version_id is None or candidate.base_spec_hash is None:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "The amendment candidate has no exact base.",
        )
    base = session.get(SpecRegistry, candidate.base_spec_version_id)
    if (
        base is None
        or base.project_id != candidate.project_id
        or base.status != "approved"
        or base.spec_hash != candidate.base_spec_hash
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "The amendment base changed before review.",
        )
    _validate_base_lineage(base, accepted_vision, accepted_goal)
    return _CandidateLineage(
        vision=accepted_vision,
        goal=accepted_goal,
        candidate_kind=CandidateKind.AMENDMENT,
        base=base,
        base_payload=_base_payload(
            session,
            project_id=candidate.project_id,
            base=base,
        ),
        supersedes=supersedes,
        source=source,
    )


def _validated_review_target(
    session: Session,
    request: DecideSpecification,
    decision: NodeDecision,
) -> tuple[SpecificationCandidate, SpecRegistry | None]:
    """Validate immutable candidate, attempt, rationale, and accepted base."""
    candidate_ref = cast(
        "FactReference",
        _reference(
            decision,
            "specification_candidate",
            required=True,
        ),
    )
    candidate = _candidate_by_reference(
        session,
        project_id=request.project_id,
        reference=candidate_ref,
    )
    if (
        candidate.specification_candidate_id != request.specification_candidate_id
        or candidate.candidate_fingerprint != request.candidate_fingerprint
    ):
        raise _GuardError(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "Specification review does not target the pending candidate.",
        )
    payload, envelope = _load_candidate(candidate)
    lineage = _lineage_for_review(session, candidate)
    _validate_candidate_attempt(
        session,
        candidate=candidate,
        envelope=envelope,
        payload=payload,
        lineage=lineage,
        repository_source_fingerprint=request.repository_source_fingerprint,
        require_current_repository_source=request.decision == "accepted",
    )
    if request.decision in {"rejected", "feedback"} and not request.rationale.strip():
        raise _GuardError(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "Rejected Specification feedback requires a rationale.",
        )
    existing = session.exec(
        select(SpecificationDecision).where(
            col(SpecificationDecision.project_id) == request.project_id,
            col(SpecificationDecision.specification_candidate_id)
            == request.specification_candidate_id,
        )
    ).one_or_none()
    if existing is not None:
        raise _GuardError(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "Specification candidate already has a terminal decision.",
        )
    approved_specs = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.project_id) == request.project_id,
            col(SpecRegistry.status) == "approved",
        )
    ).all()
    if len(approved_specs) > 1:
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "Approved Specification lineage is ambiguous.",
        )
    prior_approved = None if not approved_specs else approved_specs[0]
    if lineage.candidate_kind is CandidateKind.AMENDMENT and (
        prior_approved is None
        or prior_approved.spec_version_id != candidate.base_spec_version_id
        or prior_approved.spec_hash != candidate.base_spec_hash
    ):
        raise _GuardError(
            WorkflowErrorCode.STALE_SPECIFICATION_BASE,
            "The accepted amendment base changed before review.",
        )
    return candidate, prior_approved


def execute_decide_specification(
    session: Session,
    request: DecideSpecification,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append one exact review and register accepted bytes without rewriting them."""
    try:
        candidate, prior_approved = _validated_review_target(
            session,
            request,
            decision,
        )
    except _GuardError as exc:
        return _failure(exc.code, exc.message)

    review = SpecificationDecision(
        project_id=request.project_id,
        specification_candidate_id=request.specification_candidate_id,
        candidate_fingerprint=request.candidate_fingerprint,
        decision=request.decision,
        rationale=request.rationale.strip(),
        reviewer=request.actor,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(review)
    if request.decision == "accepted":
        if prior_approved is not None:
            prior_approved.status = "superseded"
            session.add(prior_approved)
        session.add(
            SpecRegistry(
                project_id=request.project_id,
                spec_hash=candidate.payload_fingerprint,
                status="approved",
                approved_at=evaluated_at,
                approved_by=request.actor,
                approval_notes=request.rationale.strip() or None,
                source_specification_candidate_id=request.specification_candidate_id,
                source_specification_candidate_fingerprint=(
                    candidate.candidate_fingerprint
                ),
                source_vision_artifact_id=candidate.vision_artifact_id,
                source_vision_fingerprint=candidate.vision_fingerprint,
                source_product_goal_artifact_id=(candidate.product_goal_artifact_id),
                source_product_goal_fingerprint=(candidate.product_goal_fingerprint),
                supersedes_spec_version_id=(
                    None if prior_approved is None else prior_approved.spec_version_id
                ),
            )
        )
    session.flush()
    if review.specification_decision_id is None:
        return _failure(
            WorkflowErrorCode.SPECIFICATION_CANDIDATE_CONFLICT,
            "Specification review did not receive a durable identity.",
        )
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={"specification_decision_id": review.specification_decision_id},
    )


__all__ = [
    "execute_complete_specification_structuring",
    "execute_decide_specification",
    "execute_register_specification_source",
]
