# services/specification_authoring_input.py
"""Prepare one exact host-owned input for direct Specification authoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlmodel import Session, col, select

from models.product_definition import SpecificationCandidate, SpecificationDecision
from models.specs import SpecRegistry
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.specification_authoring import (
    SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID,
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_REPOSITORY_EVIDENCE_SOURCE_ID,
    SPECIFICATION_VISION_SOURCE_ID,
    AcceptedProductGoalContext,
    AcceptedVisionContext,
    BaseSpecificationContext,
    PriorCandidateContext,
    SpecificationAuthoringInput,
    SpecificationSourceContext,
)
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from services.specs.candidate_contract import (
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    load_candidate_contract,
)
from services.vision_evidence import VisionEvidenceCollectionError
from workflow.contracts import WorkflowError, WorkflowErrorCode
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from services.contracts.vision_evidence import VisionEvidenceBundle
    from services.repository_probe import RepositoryProbe
    from utils.agileforge_spec_profile_v2 import SpecificationPayload
    from workflow.contracts import (
        FactReference,
        JsonObject,
        NodeDecision,
        TransitionResult,
    )
    from workflow.facts import (
        ProductGoalArtifactFact,
        VisionArtifactFact,
        WorkflowFactSnapshot,
    )

type AuthoringOperation = Literal["initial", "revision", "amendment"]


@dataclass(frozen=True)
class _LineageIdentity:
    """Compact exact accepted lineage shared by composition helpers."""

    vision_id: int
    vision_fingerprint: str
    goal_id: int
    goal_fingerprint: str


@dataclass(frozen=True)
class SpecificationAuthoringInputService:
    """Derive to-spec input solely from exact current durable facts."""

    engine: Engine
    repository_probe: RepositoryProbe | None = None

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Replay an exact prior authoring attempt before rebuilding input."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def build(self, *, project_id: int, decision: NodeDecision) -> JsonObject:
        """Build initial, revision, or amendment input from graph references."""
        if decision.node_id != "specification.author":
            message = "Specification authoring input requires specification.author."
            raise ValueError(message)
        try:
            with Session(self.engine) as session:
                snapshot = WorkflowFactRepository(session).load(project_id)
                vision = accepted_current_vision(snapshot)
                goal = accepted_current_goal(snapshot)
                if vision is None or goal is None:
                    message = "Specification authoring requires accepted lineage."
                    raise ValueError(message)
                _validate_lineage_references(decision, vision, goal)
                lineage = _LineageIdentity(
                    vision_id=vision.vision_artifact_id,
                    vision_fingerprint=vision.content_fingerprint,
                    goal_id=goal.product_goal_artifact_id,
                    goal_fingerprint=goal.content_fingerprint,
                )
                operation, base, prior = _composition_context(
                    session,
                    project_id=project_id,
                    decision=decision,
                    lineage=lineage,
                )
        except WorkflowFactLoadError as error:
            raise ValueError(str(error)) from error
        current_evidence = self._current_repository_evidence(project_id)
        source_manifest, source_context = _source_context(
            snapshot,
            vision,
            goal,
            current_evidence=current_evidence,
        )
        contract = SpecificationAuthoringInput(
            project_id=project_id,
            project_name=snapshot.project.name,
            operation=operation,
            accepted_vision=AcceptedVisionContext(
                artifact_id=vision.vision_artifact_id,
                fingerprint=vision.content_fingerprint,
                statement=vision.statement,
                components=vision.components,
            ),
            accepted_product_goal=AcceptedProductGoalContext(
                artifact_id=goal.product_goal_artifact_id,
                fingerprint=goal.content_fingerprint,
                statement=goal.statement,
            ),
            source_manifest=source_manifest,
            source_context=source_context,
            base_specification=base,
            prior_candidate=prior,
        )
        return contract.model_dump(mode="json")

    def _current_repository_evidence(
        self,
        project_id: int,
    ) -> VisionEvidenceBundle | None:
        """Collect bounded current source material immediately before to-spec."""
        if self.repository_probe is None:
            return None
        from services.vision_evidence import VisionEvidenceCollector  # noqa: PLC0415

        bundle = VisionEvidenceCollector(
            engine=self.engine,
            repository_probe=self.repository_probe,
        ).collect(project_id)
        if all(item.kind == "project_metadata" for item in bundle.items):
            return None
        return bundle

    def revalidate_sources(
        self,
        project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        """Re-probe the exact active repository bundle before provider use."""
        try:
            contract = SpecificationAuthoringInput.model_validate(persisted_input)
            if contract.project_id != project_id:
                return WorkflowError(
                    code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                    message=(
                        "Specification source input belongs to another project."
                    ),
                )
            expected = next(
                (
                    entry
                    for entry in contract.source_manifest
                    if entry.source_id == SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID
                ),
                None,
            )
            current = self._current_repository_evidence(project_id)
        except (ValueError, VisionEvidenceCollectionError) as error:
            return WorkflowError(
                code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                message=f"Specification source evidence is stale: {error}",
            )
        current_fingerprint = (
            None if current is None else current.evidence_fingerprint
        )
        expected_fingerprint = None if expected is None else expected.fingerprint
        if current_fingerprint != expected_fingerprint:
            return WorkflowError(
                code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                message=(
                    "Specification source evidence changed after the authoring "
                    "attempt started."
                ),
            )
        return None

def _single_reference(
    decision: NodeDecision,
    fact_type: str,
) -> FactReference | None:
    matches = tuple(
        item for item in decision.fact_references if item.fact_type == fact_type
    )
    if len(matches) > 1:
        message = f"Specification authoring has ambiguous {fact_type} references."
        raise ValueError(message)
    return matches[0] if matches else None


def _validate_lineage_references(
    decision: NodeDecision,
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
) -> None:
    vision_reference = _single_reference(decision, "vision")
    goal_reference = _single_reference(decision, "product_goal")
    if (
        vision_reference is None
        or goal_reference is None
        or (vision_reference.fact_id, vision_reference.fingerprint)
        != (str(vision.vision_artifact_id), vision.content_fingerprint)
        or (goal_reference.fact_id, goal_reference.fingerprint)
        != (str(goal.product_goal_artifact_id), goal.content_fingerprint)
    ):
        message = (
            "Specification authoring requires exact accepted Vision and Product Goal."
        )
        raise ValueError(message)


def _source_context(
    snapshot: WorkflowFactSnapshot,
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
    *,
    current_evidence: VisionEvidenceBundle | None = None,
) -> tuple[
    tuple[CandidateSourceManifestEntry, ...],
    tuple[SpecificationSourceContext, ...],
]:
    vision_fingerprint = vision.content_fingerprint
    goal_fingerprint = goal.content_fingerprint
    entries: list[CandidateSourceManifestEntry] = [
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_VISION_SOURCE_ID,
            kind=CandidateSourceKind.VISION,
            fingerprint=vision_fingerprint,
        ),
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
            kind=CandidateSourceKind.PRODUCT_GOAL,
            fingerprint=goal_fingerprint,
        ),
    ]
    contexts: list[SpecificationSourceContext] = [
        SpecificationSourceContext(
            source_id=entries[0].source_id,
            kind=entries[0].kind,
            fingerprint=entries[0].fingerprint,
            content={
                "statement": vision.statement,
                "components": dict(vision.components),
                "component_basis": list(vision.component_basis),
                "assumptions": list(vision.assumptions),
                "conflicts": list(vision.conflicts),
            },
        ),
        SpecificationSourceContext(
            source_id=entries[1].source_id,
            kind=entries[1].kind,
            fingerprint=entries[1].fingerprint,
            content={"statement": goal.statement},
        ),
    ]
    snapshot_id = vision.vision_evidence_snapshot_id
    evidence = next(
        (
            item
            for item in snapshot.vision_evidence_snapshots
            if item.vision_evidence_snapshot_id == snapshot_id
        ),
        None,
    )
    if evidence is not None:
        evidence_kind = (
            CandidateSourceKind.REPOSITORY
            if evidence.repository_binding_id is not None
            else CandidateSourceKind.RESEARCH
        )
        entry = CandidateSourceManifestEntry(
            source_id=SPECIFICATION_REPOSITORY_EVIDENCE_SOURCE_ID,
            kind=evidence_kind,
            fingerprint=evidence.evidence_fingerprint,
            warnings=tuple(
                f"{warning.get('code', 'SOURCE_WARNING')}: "
                f"{warning.get('message', 'Source warning')}"
                for warning in evidence.warnings
            ),
        )
        entries.append(entry)
        contexts.append(
            SpecificationSourceContext(
                source_id=entry.source_id,
                kind=entry.kind,
                fingerprint=entry.fingerprint,
                content=evidence.evidence,
            )
        )
    if current_evidence is not None:
        entry = CandidateSourceManifestEntry(
            source_id=SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID,
            kind=CandidateSourceKind.REPOSITORY,
            fingerprint=current_evidence.evidence_fingerprint,
            warnings=tuple(
                f"{warning.code}: {warning.message}"
                for warning in current_evidence.warnings
            ),
        )
        entries.append(entry)
        contexts.append(
            SpecificationSourceContext(
                source_id=entry.source_id,
                kind=entry.kind,
                fingerprint=entry.fingerprint,
                content=current_evidence.model_dump(mode="json"),
            )
        )
    return tuple(entries), tuple(contexts)


def _composition_context(
    session: Session,
    *,
    project_id: int,
    decision: NodeDecision,
    lineage: _LineageIdentity,
) -> tuple[
    AuthoringOperation,
    BaseSpecificationContext | None,
    PriorCandidateContext | None,
]:
    prior_reference = _single_reference(decision, "specification_candidate")
    base_reference = _single_reference(decision, "specification")
    if prior_reference is not None and base_reference is not None:
        message = (
            "Specification authoring cannot select prior and base references together."
        )
        raise ValueError(message)
    if prior_reference is not None:
        prior_row = _exact_candidate(
            session,
            project_id=project_id,
            reference=prior_reference,
            lineage=lineage,
        )
        prior_payload, prior_envelope = load_candidate_contract(
            prior_row.canonical_envelope_json,
            expected_candidate_fingerprint=prior_row.candidate_fingerprint,
        )
        terminal = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id,
                col(SpecificationDecision.specification_candidate_id)
                == prior_row.specification_candidate_id,
                col(SpecificationDecision.candidate_fingerprint)
                == prior_row.candidate_fingerprint,
            )
        ).one_or_none()
        if terminal is None or terminal.decision not in {"rejected", "feedback"}:
            message = "Specification revision requires exact rejected feedback."
            raise ValueError(message)
        base = _base_context(
            session,
            project_id=project_id,
            spec_version_id=prior_envelope.base_specification_id,
            payload_fingerprint=prior_envelope.base_payload_fingerprint,
            lineage=lineage,
        )
        prior = PriorCandidateContext(
            candidate_fingerprint=prior_row.candidate_fingerprint,
            payload=prior_payload,
            decision=("rejected" if terminal.decision == "rejected" else "feedback"),
            rationale=terminal.rationale,
            base_specification_id=prior_envelope.base_specification_id,
            base_payload_fingerprint=prior_envelope.base_payload_fingerprint,
        )
        return "revision", base, prior
    if base_reference is not None:
        try:
            base_id = int(base_reference.fact_id)
        except ValueError as error:
            message = "Specification amendment base identity is invalid."
            raise ValueError(message) from error
        base = _base_context(
            session,
            project_id=project_id,
            spec_version_id=base_id,
            payload_fingerprint=base_reference.fingerprint,
            lineage=lineage,
        )
        if base is None:
            message = "Specification amendment requires an approved base."
            raise ValueError(message)
        return "amendment", base, None
    return "initial", None, None


def _exact_candidate(
    session: Session,
    *,
    project_id: int,
    reference: FactReference,
    lineage: _LineageIdentity,
) -> SpecificationCandidate:
    try:
        candidate_id = int(reference.fact_id)
    except ValueError as error:
        message = "Specification prior candidate identity is invalid."
        raise ValueError(message) from error
    candidate = session.exec(
        select(SpecificationCandidate).where(
            col(SpecificationCandidate.project_id) == project_id,
            col(SpecificationCandidate.specification_candidate_id) == candidate_id,
            col(SpecificationCandidate.candidate_fingerprint)
            == reference.fingerprint,
        )
    ).one_or_none()
    if candidate is None or (
        candidate.vision_artifact_id,
        candidate.vision_fingerprint,
        candidate.product_goal_artifact_id,
        candidate.product_goal_fingerprint,
    ) != (
        lineage.vision_id,
        lineage.vision_fingerprint,
        lineage.goal_id,
        lineage.goal_fingerprint,
    ):
        message = "Specification prior candidate lineage is stale."
        raise ValueError(message)
    return candidate


def _base_context(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int | None,
    payload_fingerprint: str | None,
    lineage: _LineageIdentity,
) -> BaseSpecificationContext | None:
    if spec_version_id is None and payload_fingerprint is None:
        return None
    if spec_version_id is None or payload_fingerprint is None:
        message = "Specification base identity must be paired."
        raise ValueError(message)
    spec = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.project_id) == project_id,
            col(SpecRegistry.spec_version_id) == spec_version_id,
            col(SpecRegistry.spec_hash) == payload_fingerprint,
            col(SpecRegistry.status) == "approved",
        )
    ).one_or_none()
    if spec is None or (
        spec.source_vision_artifact_id,
        spec.source_vision_fingerprint,
        spec.source_product_goal_artifact_id,
        spec.source_product_goal_fingerprint,
    ) != (
        lineage.vision_id,
        lineage.vision_fingerprint,
        lineage.goal_id,
        lineage.goal_fingerprint,
    ):
        message = "Specification amendment base is stale."
        raise ValueError(message)
    source = session.exec(
        select(SpecificationCandidate).where(
            col(SpecificationCandidate.project_id) == project_id,
            col(SpecificationCandidate.specification_candidate_id)
            == spec.source_specification_candidate_id,
            col(SpecificationCandidate.candidate_fingerprint)
            == spec.source_specification_candidate_fingerprint,
            col(SpecificationCandidate.payload_fingerprint) == spec.spec_hash,
        )
    ).one_or_none()
    if source is None:
        message = "Specification amendment base source is invalid."
        raise ValueError(message)
    payload: SpecificationPayload
    payload, _envelope = load_candidate_contract(
        source.canonical_envelope_json,
        expected_candidate_fingerprint=source.candidate_fingerprint,
    )
    return BaseSpecificationContext(
        spec_version_id=spec_version_id,
        payload_fingerprint=payload_fingerprint,
        payload=payload,
    )


__all__ = ["SpecificationAuthoringInputService"]
