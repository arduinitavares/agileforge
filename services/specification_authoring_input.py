# services/specification_authoring_input.py
"""Prepare one exact host-owned input for Specification structuring."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError
from sqlmodel import Session, col, select

from models.product_definition import (
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
)
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import SpecRegistry
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_VISION_SOURCE_ID,
    AcceptedProductGoalContext,
    AcceptedVisionContext,
    BaseSpecificationContext,
    PriorCandidateContext,
    RegisteredRepositoryEvidence,
    RegisteredSpecificationSource,
    SpecificationStructuringContextCapture,
    SpecificationStructuringDocument,
    SpecificationStructuringInput,
)
from services.contracts.specification_source import (
    SpecificationSourceBundle,
    source_bundle_fingerprint,
)
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from services.specification_source_registration import (
    SpecificationSourceRegistrationError,
    SpecificationSourceRegistrationRequest,
    SpecificationSourceRegistrationService,
)
from services.specs.candidate_contract import (
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    load_candidate_contract,
)
from workflow.contracts import WorkflowError, WorkflowErrorCode
from workflow.definitions.product_discovery import current_specification_source
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from services.contracts.specification_source import SpecificationSourceDocument
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
        SpecificationSourceFact,
        VisionArtifactFact,
    )

type StructuringOperation = Literal["initial", "revision", "amendment"]


@dataclass(frozen=True)
class _LineageIdentity:
    """Compact exact accepted lineage shared by composition helpers."""

    vision_id: int
    vision_fingerprint: str
    goal_id: int
    goal_fingerprint: str


@dataclass(frozen=True)
class _SourceIdentity:
    """Exact registered source identity required by revisions and candidates."""

    source_id: int
    source_fingerprint: str
    superseded_source_id: int | None
    superseded_source_fingerprint: str | None


@dataclass(frozen=True)
class SpecificationStructuringInputService:
    """Derive structurer input solely from exact current durable facts."""

    engine: Engine
    repository_probe: RepositoryProbe

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Replay an exact prior structuring attempt before rebuilding input."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def build(self, *, project_id: int, decision: NodeDecision) -> JsonObject:
        """Build initial, revision, or amendment input from graph references."""
        if decision.node_id != "specification.structure":
            message = (
                "Specification structuring input requires specification.structure."
            )
            raise ValueError(message)
        try:
            with Session(self.engine) as session:
                snapshot = WorkflowFactRepository(session).load(project_id)
                vision = accepted_current_vision(snapshot)
                goal = accepted_current_goal(snapshot)
                source_fact = current_specification_source(snapshot)
                if vision is None or goal is None or source_fact is None:
                    message = (
                        "Specification structuring requires accepted lineage and "
                        "one current registered source."
                    )
                    raise ValueError(message)
                _validate_lineage_references(decision, vision, goal)
                _validate_source_reference(decision, source_fact)
                source_row, bundle, binding = _load_registered_source(
                    session,
                    project_id=project_id,
                    source_fact=source_fact,
                    vision=vision,
                    goal=goal,
                    active_repository_binding_id=(
                        snapshot.project.active_repository_binding_id
                    ),
                )
                lineage = _LineageIdentity(
                    vision_id=vision.vision_artifact_id,
                    vision_fingerprint=vision.content_fingerprint,
                    goal_id=goal.product_goal_artifact_id,
                    goal_fingerprint=goal.content_fingerprint,
                )
                source_identity = _SourceIdentity(
                    source_id=source_fact.specification_source_id,
                    source_fingerprint=source_fact.source_fingerprint,
                    superseded_source_id=(
                        source_fact.supersedes_specification_source_id
                    ),
                    superseded_source_fingerprint=(
                        source_fact.supersedes_source_fingerprint
                    ),
                )
                operation, base, prior = _composition_context(
                    session,
                    project_id=project_id,
                    decision=decision,
                    lineage=lineage,
                    source=source_identity,
                )
                registered_source = _registered_source_context(
                    source_row,
                    bundle=bundle,
                    binding=binding,
                )
                contract = SpecificationStructuringInput(
                    project_id=project_id,
                    project_name=snapshot.project.name,
                    operation=operation,
                    accepted_vision=AcceptedVisionContext(
                        artifact_id=vision.vision_artifact_id,
                        fingerprint=vision.content_fingerprint,
                        statement=vision.statement,
                        components=vision.components,
                        component_basis=vision.component_basis,
                        assumptions=vision.assumptions,
                        conflicts=vision.conflicts,
                    ),
                    accepted_product_goal=AcceptedProductGoalContext(
                        artifact_id=goal.product_goal_artifact_id,
                        fingerprint=goal.content_fingerprint,
                        statement=goal.statement,
                    ),
                    registered_source=registered_source,
                    source_manifest=_source_manifest(
                        vision,
                        goal,
                        registered_source,
                    ),
                    base_specification=base,
                    prior_candidate=prior,
                )
        except WorkflowFactLoadError as error:
            raise ValueError(str(error)) from error
        stale = self.revalidate_sources(project_id, contract.model_dump(mode="json"))
        if stale is not None:
            raise ValueError(stale.message)
        return contract.model_dump(mode="json")

    def revalidate_sources(
        self,
        project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        """Reload facts and recapture every selected registered source path."""
        try:
            contract = SpecificationStructuringInput.model_validate(persisted_input)
            if contract.project_id != project_id:
                return _stale(
                    "Specification structuring input belongs to another project."
                )
            registered = contract.registered_source
            with Session(self.engine) as session:
                snapshot = WorkflowFactRepository(session).load(project_id)
                vision = accepted_current_vision(snapshot)
                goal = accepted_current_goal(snapshot)
                source_fact = current_specification_source(snapshot)
                if vision is None or goal is None or source_fact is None:
                    return _stale(
                        "Specification structuring lineage is no longer current."
                    )
                source_row, bundle, binding = _load_registered_source(
                    session,
                    project_id=project_id,
                    source_fact=source_fact,
                    vision=vision,
                    goal=goal,
                    active_repository_binding_id=(
                        snapshot.project.active_repository_binding_id
                    ),
                )
                current_contract = _registered_source_context(
                    source_row,
                    bundle=bundle,
                    binding=binding,
                )
                if (
                    current_contract != registered
                    or contract.accepted_vision
                    != AcceptedVisionContext(
                        artifact_id=vision.vision_artifact_id,
                        fingerprint=vision.content_fingerprint,
                        statement=vision.statement,
                        components=vision.components,
                        component_basis=vision.component_basis,
                        assumptions=vision.assumptions,
                        conflicts=vision.conflicts,
                    )
                    or contract.accepted_product_goal
                    != AcceptedProductGoalContext(
                        artifact_id=goal.product_goal_artifact_id,
                        fingerprint=goal.content_fingerprint,
                        statement=goal.statement,
                    )
                ):
                    return _stale("Specification registered source or lineage changed.")
            prepared = SpecificationSourceRegistrationService(
                engine=self.engine,
                repository_probe=self.repository_probe,
            ).prepare(
                SpecificationSourceRegistrationRequest(
                    project_id=project_id,
                    source_path=registered.source.relative_path,
                    preparation_capability=registered.preparation_capability,
                    adr_paths=tuple(item.relative_path for item in registered.adrs),
                    idempotency_key="specification-structuring-revalidation",
                    actor="specification-structuring-revalidation",
                    correlation_id=None,
                )
            )
        except (
            SpecificationSourceRegistrationError,
            ValidationError,
            ValueError,
            WorkflowFactLoadError,
        ) as error:
            return _stale(f"Specification registered source is stale: {error}")
        if (
            prepared.source_fingerprint != registered.source_fingerprint
            or prepared.bundle.model_dump(mode="json")
            != _registered_bundle(registered).model_dump(mode="json")
            or prepared.repository_binding_id
            != registered.repository_evidence.repository_binding_id
        ):
            return _stale(
                "Specification source, Context, ADRs, or repository revision changed."
            )
        return None


def _stale(message: str) -> WorkflowError:
    return WorkflowError(
        code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
        message=message,
    )


def _single_reference(
    decision: NodeDecision,
    fact_type: str,
) -> FactReference | None:
    matches = tuple(
        item for item in decision.fact_references if item.fact_type == fact_type
    )
    if len(matches) > 1:
        message = f"Specification structuring has ambiguous {fact_type} references."
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
            "Specification structuring requires exact accepted Vision and Product Goal."
        )
        raise ValueError(message)


def _validate_source_reference(
    decision: NodeDecision,
    source: SpecificationSourceFact,
) -> None:
    reference = _single_reference(decision, "specification_source")
    if reference is None or (reference.fact_id, reference.fingerprint) != (
        str(source.specification_source_id),
        source.source_fingerprint,
    ):
        message = (
            "Specification structuring requires exact registered Specification source."
        )
        raise ValueError(message)


def _load_registered_source(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    source_fact: SpecificationSourceFact,
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
    active_repository_binding_id: int | None,
) -> tuple[SpecificationSource, SpecificationSourceBundle, RepositoryBinding]:
    row = session.get(SpecificationSource, source_fact.specification_source_id)
    if row is None or row.project_id != project_id:
        message = "Registered Specification source is unavailable."
        raise ValueError(message)
    try:
        bundle = SpecificationSourceBundle.model_validate_json(row.source_bundle_json)
    except ValidationError as error:
        message = "Registered Specification source bundle is invalid."
        raise ValueError(message) from error
    binding = session.get(RepositoryBinding, row.repository_binding_id)
    expected = (
        source_fact.source_fingerprint,
        active_repository_binding_id,
        vision.vision_artifact_id,
        vision.content_fingerprint,
        goal.product_goal_artifact_id,
        goal.content_fingerprint,
        bundle.repository_revision.head_sha,
        bundle.repository_revision.dirty,
        bundle.repository_revision.status_fingerprint,
    )
    actual = (
        source_bundle_fingerprint(bundle),
        row.repository_binding_id,
        row.vision_artifact_id,
        row.vision_fingerprint,
        row.product_goal_artifact_id,
        row.product_goal_fingerprint,
        row.repository_head_sha,
        row.repository_dirty,
        row.repository_status_fingerprint,
    )
    if (
        expected != actual
        or row.source_fingerprint != source_fact.source_fingerprint
        or binding is None
        or binding.project_id != project_id
    ):
        message = "Registered Specification source lineage or repository changed."
        raise ValueError(message)
    return row, bundle, binding


def _decode_document(
    document: SpecificationSourceDocument,
) -> SpecificationStructuringDocument:
    try:
        text = base64.b64decode(
            document.content_base64,
            validate=True,
        ).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as error:
        message = "Registered Specification source text is invalid."
        raise ValueError(message) from error
    return SpecificationStructuringDocument(
        source_id=document.source_id,
        relative_path=document.relative_path,
        text=text,
        byte_length=document.byte_length,
        content_fingerprint=document.content_fingerprint,
    )


def _registered_source_context(
    row: SpecificationSource,
    *,
    bundle: SpecificationSourceBundle,
    binding: RepositoryBinding,
) -> RegisteredSpecificationSource:
    identifier = row.specification_source_id
    if identifier is None:
        message = "Registered Specification source has no durable identity."
        raise ValueError(message)
    context_document = (
        None
        if bundle.context.document is None
        else _decode_document(bundle.context.document)
    )
    return RegisteredSpecificationSource(
        specification_source_id=identifier,
        source_fingerprint=row.source_fingerprint,
        producer_capability=bundle.producer_capability,
        preparation_capability=bundle.preparation_capability,
        source=_decode_document(bundle.source),
        context=SpecificationStructuringContextCapture(
            state=bundle.context.state,
            document=context_document,
        ),
        adrs=tuple(_decode_document(item) for item in bundle.adrs),
        repository_revision=bundle.repository_revision,
        repository_evidence=RegisteredRepositoryEvidence(
            repository_binding_id=row.repository_binding_id,
            binding_fingerprint=repository_binding_fingerprint(binding),
            head_sha=binding.head_sha,
            branch_name=binding.branch_name,
            detached_head=binding.detached_head,
            dirty=binding.dirty,
            status_fingerprint=binding.status_fingerprint,
            status_entries=tuple(_json_object_list(binding.status_entries_json)),
            remotes=tuple(_string_list(binding.remotes_json)),
            warnings=tuple(_json_object_list(binding.warnings_json)),
            probe_version=binding.probe_version,
        ),
        accepted_vision_fingerprint=bundle.accepted_vision_fingerprint,
        accepted_product_goal_fingerprint=(bundle.accepted_product_goal_fingerprint),
    )


def _registered_bundle(
    registered: RegisteredSpecificationSource,
) -> SpecificationSourceBundle:
    import base64 as _base64  # noqa: PLC0415

    from services.contracts.specification_source import (  # noqa: PLC0415
        SpecificationContextCapture,
        SpecificationSourceDocument,
    )

    def document(
        value: SpecificationStructuringDocument,
    ) -> SpecificationSourceDocument:
        return SpecificationSourceDocument(
            source_id=value.source_id,
            relative_path=value.relative_path,
            content_base64=_base64.b64encode(value.text.encode("utf-8")).decode(
                "ascii"
            ),
            byte_length=value.byte_length,
            content_fingerprint=value.content_fingerprint,
        )

    return SpecificationSourceBundle(
        producer_capability=registered.producer_capability,
        preparation_capability=registered.preparation_capability,
        source=document(registered.source),
        context=(
            SpecificationContextCapture(state="absent")
            if registered.context.document is None
            else SpecificationContextCapture(
                state="present",
                document=document(registered.context.document),
            )
        ),
        adrs=tuple(document(item) for item in registered.adrs),
        repository_revision=registered.repository_revision,
        accepted_vision_fingerprint=registered.accepted_vision_fingerprint,
        accepted_product_goal_fingerprint=(
            registered.accepted_product_goal_fingerprint
        ),
    )


def _json_object_list(raw: str) -> list[JsonObject]:
    import json  # noqa: PLC0415

    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        message = "Registered repository evidence must be a JSON object list."
        raise ValueError(message)
    return value


def _string_list(raw: str) -> list[str]:
    import json  # noqa: PLC0415

    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        message = "Registered repository remotes must be a string list."
        raise ValueError(message)
    return value


def _source_manifest(
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
    registered: RegisteredSpecificationSource,
) -> tuple[CandidateSourceManifestEntry, ...]:
    entries = [
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_VISION_SOURCE_ID,
            kind=CandidateSourceKind.VISION,
            fingerprint=vision.content_fingerprint,
        ),
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
            kind=CandidateSourceKind.PRODUCT_GOAL,
            fingerprint=goal.content_fingerprint,
        ),
        CandidateSourceManifestEntry(
            source_id=registered.source.source_id,
            kind=CandidateSourceKind.EXTERNAL,
            fingerprint=registered.source.content_fingerprint,
        ),
    ]
    if registered.context.document is not None:
        context = registered.context.document
        entries.append(
            CandidateSourceManifestEntry(
                source_id=context.source_id,
                kind=CandidateSourceKind.REPOSITORY,
                fingerprint=context.content_fingerprint,
            )
        )
    entries.extend(
        CandidateSourceManifestEntry(
            source_id=adr.source_id,
            kind=CandidateSourceKind.REPOSITORY,
            fingerprint=adr.content_fingerprint,
        )
        for adr in registered.adrs
    )
    return tuple(entries)


def _composition_context(
    session: Session,
    *,
    project_id: int,
    decision: NodeDecision,
    lineage: _LineageIdentity,
    source: _SourceIdentity,
) -> tuple[
    StructuringOperation,
    BaseSpecificationContext | None,
    PriorCandidateContext | None,
]:
    prior_reference = _single_reference(decision, "specification_candidate")
    base_reference = _single_reference(decision, "specification")
    if prior_reference is not None and base_reference is not None:
        message = (
            "Specification structuring cannot select prior and base references "
            "together."
        )
        raise ValueError(message)
    if prior_reference is not None:
        prior_row = _exact_candidate(
            session,
            project_id=project_id,
            reference=prior_reference,
            lineage=lineage,
            source=source,
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
    source: _SourceIdentity,
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
            col(SpecificationCandidate.candidate_fingerprint) == reference.fingerprint,
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
        message = "Specification prior candidate lineage or source is stale."
        raise ValueError(message)
    if not _source_descends_from_candidate(
        session,
        project_id=project_id,
        source=source,
        candidate=candidate,
        lineage=lineage,
    ):
        message = "Specification prior candidate lineage or source is stale."
        raise ValueError(message)
    return candidate


def _source_descends_from_candidate(
    session: Session,
    *,
    project_id: int,
    source: _SourceIdentity,
    candidate: SpecificationCandidate,
    lineage: _LineageIdentity,
) -> bool:
    """Validate one exact, acyclic source ancestry back to the candidate source."""
    current_id = source.superseded_source_id
    current_fingerprint = source.superseded_source_fingerprint
    visited = {source.source_id}
    found = False
    while current_id is not None or current_fingerprint is not None:
        if current_id is None or current_fingerprint is None or current_id in visited:
            return False
        visited.add(current_id)
        row = session.exec(
            select(SpecificationSource).where(
                col(SpecificationSource.project_id) == project_id,
                col(SpecificationSource.specification_source_id) == current_id,
                col(SpecificationSource.source_fingerprint) == current_fingerprint,
            )
        ).one_or_none()
        if row is None:
            return False
        matches_candidate = (
            candidate.specification_source_id,
            candidate.specification_source_fingerprint,
        ) == (current_id, current_fingerprint)
        if not found and (
            row.vision_artifact_id,
            row.vision_fingerprint,
            row.product_goal_artifact_id,
            row.product_goal_fingerprint,
        ) != (
            lineage.vision_id,
            lineage.vision_fingerprint,
            lineage.goal_id,
            lineage.goal_fingerprint,
        ):
            return False
        found = found or matches_candidate
        current_id = row.supersedes_specification_source_id
        current_fingerprint = row.supersedes_source_fingerprint
    return found


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
    candidate = session.exec(
        select(SpecificationCandidate).where(
            col(SpecificationCandidate.project_id) == project_id,
            col(SpecificationCandidate.specification_candidate_id)
            == spec.source_specification_candidate_id,
            col(SpecificationCandidate.candidate_fingerprint)
            == spec.source_specification_candidate_fingerprint,
            col(SpecificationCandidate.payload_fingerprint) == spec.spec_hash,
        )
    ).one_or_none()
    if candidate is None:
        message = "Specification amendment base source is invalid."
        raise ValueError(message)
    payload: SpecificationPayload
    payload, _envelope = load_candidate_contract(
        candidate.canonical_envelope_json,
        expected_candidate_fingerprint=candidate.candidate_fingerprint,
    )
    return BaseSpecificationContext(
        spec_version_id=spec_version_id,
        payload_fingerprint=payload_fingerprint,
        payload=payload,
    )


__all__ = ["SpecificationStructuringInputService"]
