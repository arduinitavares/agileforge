"""Shared provider-free persisted v2 lifecycle fixtures."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlmodel import Session, col, select

from models.core import Project
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
)
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_STRUCTURER_PROMPT_VERSION,
    SPECIFICATION_VISION_SOURCE_ID,
    AcceptedProductGoalContext,
    AcceptedVisionContext,
    BaseSpecificationContext,
    RegisteredRepositoryEvidence,
    RegisteredSpecificationSource,
    SpecificationStructuringContextCapture,
    SpecificationStructuringDocument,
    SpecificationStructuringInput,
    specification_structuring_fact_fingerprint,
    specification_structuring_input_fingerprint,
)
from services.contracts.specification_source import (
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
)
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    build_candidate_envelope,
    canonical_candidate_json,
    load_candidate_contract,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
    vision_interview_output_fingerprint,
    workflow_node_attempt_fingerprint,
)


@dataclass(frozen=True)
class PersistedSpecificationLineage:
    """Durable identities for one accepted v2 specification chain."""

    vision_artifact_id: int
    vision_fingerprint: str
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    specification_candidate_id: int
    specification_fingerprint: str
    candidate_fingerprint: str
    spec: SpecRegistry


def _required(value: int | None, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise AssertionError(message)
    return value


def _source_document(content: bytes) -> SpecificationSourceDocument:
    """Build one exact primary source document for a lifecycle fixture."""
    return SpecificationSourceDocument(
        source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
        relative_path="SPECIFICATION.md",
        content_base64=base64.b64encode(content).decode("ascii"),
        byte_length=len(content),
        content_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
    )


def _structuring_document(
    document: SpecificationSourceDocument,
) -> SpecificationStructuringDocument:
    """Project one exact registered document as provider-readable UTF-8 text."""
    return SpecificationStructuringDocument(
        source_id=document.source_id,
        relative_path=document.relative_path,
        text=base64.b64decode(document.content_base64, validate=True).decode("utf-8"),
        byte_length=document.byte_length,
        content_fingerprint=document.content_fingerprint,
    )


def _repository_binding_for_source(
    session: Session,
    *,
    project: Project,
    base_time: datetime,
) -> RepositoryBinding:
    """Return or create the exact active binding used by a source fixture."""
    if project.active_repository_binding_id is not None:
        binding = session.get(
            RepositoryBinding,
            project.active_repository_binding_id,
        )
        if binding is None:
            message = "Specification fixture repository binding is missing."
            raise AssertionError(message)
        return binding
    status_fingerprint = canonical_hash({"fixture_repository": project.project_id})
    binding = RepositoryBinding(
        project_id=_required(project.project_id, "Project"),
        worktree_path="repository",
        common_git_dir="repository/.git",
        head_sha="f" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_fingerprint=status_fingerprint,
        status_entries_json="[]",
        remotes_json="[]",
        warnings_json="[]",
        probe_version="agileforge.repository-probe.v1",
        inspected_at=base_time + timedelta(seconds=7, microseconds=250_000),
        recorded_by="fixture",
    )
    session.add(binding)
    session.flush()
    project.active_repository_binding_id = _required(
        binding.repository_binding_id,
        "repository binding",
    )
    session.add(project)
    session.flush()
    return binding


def _attempt(
    session: Session,
    *,
    project_id: int,
    node_id: str,
    ordinal: int,
    started_at: datetime,
) -> WorkflowNodeAttempt:
    attempt_fingerprint = canonical_hash(
        {"node_id": node_id, "project_id": project_id, "ordinal": ordinal}
    )
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id=node_id,
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=f"sha256:fixture-facts-{project_id}-{ordinal}",
        business_fact_fingerprint=canonical_hash(
            {
                "kind": "fixture-business-facts",
                "project_id": project_id,
                "ordinal": ordinal,
            }
        ),
        decision_fingerprint=f"sha256:fixture-decision-{project_id}-{ordinal}",
        normalized_input_json="{}",
        input_fingerprint=canonical_hash(
            {
                "kind": "fixture-model-input",
                "project_id": project_id,
                "ordinal": ordinal,
            }
        ),
        model_id="fake/product-definition",
        execution_settings_json="{}",
        idempotency_key=f"fixture-{node_id}-{project_id}-{ordinal}",
        actor="fixture",
        correlation_id=None,
        started_at=started_at,
        lease_expires_at=started_at + timedelta(minutes=1),
        attempt_fingerprint=attempt_fingerprint,
    )
    session.add(attempt)
    session.flush()
    _required(attempt.workflow_node_attempt_id, f"{node_id} attempt")
    return attempt


def _complete_attempt(
    session: Session,
    *,
    project_id: int,
    attempt: WorkflowNodeAttempt,
    recorded_at: datetime,
) -> None:
    attempt_id = _required(attempt.workflow_node_attempt_id, "workflow attempt")
    session.add(
        WorkflowNodeAttemptOutcome(
            project_id=project_id,
            workflow_node_attempt_id=attempt_id,
            status="success",
            output_fingerprint=f"sha256:fixture-output-{attempt_id}",
            output_json="{}",
            recorded_at=recorded_at,
        )
    )


def _seed_accepted_vision_and_goal(
    session: Session,
    *,
    project_id: int,
    recorded_at: datetime,
) -> tuple[VisionArtifact, ProductGoalArtifact]:
    vision = session.exec(
        select(VisionArtifact)
        .where(col(VisionArtifact.project_id) == project_id)
        .order_by(col(VisionArtifact.vision_artifact_id).desc())
    ).first()
    goal = session.exec(
        select(ProductGoalArtifact)
        .where(col(ProductGoalArtifact.project_id) == project_id)
        .order_by(col(ProductGoalArtifact.product_goal_artifact_id).desc())
    ).first()
    if vision is not None and goal is not None:
        return vision, goal
    if vision is not None or goal is not None:
        message = "Partial product-definition fixture lineage exists."
        raise AssertionError(message)

    vision_components: JsonObject = {"purpose": "exercise durable lifecycle"}
    vision_statement = "Deliver one verified product increment."
    vision_attempt = _attempt(
        session,
        project_id=project_id,
        node_id="vision.bootstrap",
        ordinal=1,
        started_at=recorded_at,
    )
    evidence_item: JsonObject = {
        "evidence_id": "project:metadata",
        "kind": "project_metadata",
        "relative_path": None,
        "content_fingerprint": canonical_hash({"name": "Fixture", "description": None}),
        "trust": "operator_provided",
        "content": {"name": "Fixture", "description": None},
        "truncated": False,
    }
    evidence: JsonObject = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [evidence_item],
        "warnings": [],
        "evidence_fingerprint": canonical_hash(
            {
                "schema_version": "agileforge.vision-evidence.v1",
                "items": [evidence_item],
                "warnings": [],
            }
        ),
    }
    snapshot = VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=None,
        workflow_node_attempt_id=_required(
            vision_attempt.workflow_node_attempt_id,
            "Vision attempt",
        ),
        evidence_json=canonical_json(evidence),
        evidence_fingerprint=str(evidence["evidence_fingerprint"]),
        warnings_json="[]",
        created_at=recorded_at,
    )
    session.add(snapshot)
    session.flush()
    basis_json = canonical_json([])
    vision_turn = VisionInterviewTurn(
        project_id=project_id,
        operation="bootstrap",
        turn_number=1,
        revision_intent_id=None,
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "Vision evidence snapshot",
        ),
        prior_turn_id=None,
        user_text=None,
        components_json=canonical_json(vision_components),
        vision_statement=vision_statement,
        is_complete=True,
        clarifying_questions_json="[]",
        component_basis_json=basis_json,
        assumptions_json="[]",
        conflicts_json="[]",
        output_fingerprint=vision_interview_output_fingerprint(
            vision_components,
            vision_statement,
            True,
            (),
            {"component_basis": (), "assumptions": (), "conflicts": ()},
        ),
        workflow_node_attempt_id=_required(
            vision_attempt.workflow_node_attempt_id,
            "Vision attempt",
        ),
        attempt_fingerprint=vision_attempt.attempt_fingerprint,
        recorded_at=recorded_at + timedelta(seconds=1),
    )
    session.add(vision_turn)
    session.flush()
    vision = VisionArtifact(
        project_id=project_id,
        version_number=1,
        components_json=canonical_json(vision_components),
        statement=vision_statement,
        content_fingerprint=canonical_hash(
            {"components": vision_components, "statement": vision_statement}
        ),
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "Vision evidence snapshot",
        ),
        component_basis_json=basis_json,
        assumptions_json="[]",
        conflicts_json="[]",
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=_required(
            vision_turn.vision_interview_turn_id,
            "Vision turn",
        ),
        created_by="fixture",
        created_at=recorded_at + timedelta(seconds=2),
    )
    session.add(vision)
    session.flush()
    vision_id = _required(vision.vision_artifact_id, "Vision")
    session.add(
        VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=vision_id,
            artifact_fingerprint=vision.content_fingerprint,
            decision="accepted",
            rationale="Accepted for fixture delivery.",
            reviewer="fixture",
            idempotency_key=f"fixture-vision-accepted-{project_id}",
            decided_at=recorded_at + timedelta(seconds=3),
        )
    )
    _complete_attempt(
        session,
        project_id=project_id,
        attempt=vision_attempt,
        recorded_at=recorded_at + timedelta(seconds=3),
    )

    goal_components: JsonObject = {
        "valuable_future_state": "One increment is accepted",
        "beneficiary": "Operators",
        "value": "Predictable delivery",
        "success_signals": ["The lifecycle reaches triage"],
        "boundaries": ["No provider calls"],
    }
    goal_statement = "Complete one accepted increment through triage."
    goal_attempt = _attempt(
        session,
        project_id=project_id,
        node_id="goal.interview",
        ordinal=2,
        started_at=recorded_at + timedelta(seconds=4),
    )
    goal_turn = ProductGoalInterviewTurn(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=1,
        revision_number=1,
        prior_turn_id=None,
        user_text="Define the Product Goal.",
        components_json=canonical_json(goal_components),
        goal_statement=goal_statement,
        is_complete=True,
        clarifying_questions_json="[]",
        output_fingerprint=product_goal_interview_output_fingerprint(
            goal_components,
            goal_statement,
            True,
            (),
        ),
        workflow_node_attempt_id=_required(
            goal_attempt.workflow_node_attempt_id,
            "Goal attempt",
        ),
        attempt_fingerprint=goal_attempt.attempt_fingerprint,
        recorded_at=recorded_at + timedelta(seconds=5),
    )
    session.add(goal_turn)
    session.flush()
    goal = ProductGoalArtifact(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=1,
        revision_number=1,
        statement=goal_statement,
        content_fingerprint=product_goal_artifact_fingerprint(
            goal_components,
            goal_statement,
        ),
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=_required(
            goal_turn.product_goal_interview_turn_id,
            "Goal turn",
        ),
        created_by="fixture",
        created_at=recorded_at + timedelta(seconds=6),
    )
    session.add(goal)
    session.flush()
    session.add(
        ProductGoalArtifactDecision(
            project_id=project_id,
            product_goal_artifact_id=_required(
                goal.product_goal_artifact_id,
                "Product Goal",
            ),
            artifact_fingerprint=goal.content_fingerprint,
            decision="accepted",
            rationale="Accepted for fixture delivery.",
            reviewer="fixture",
            idempotency_key=f"fixture-goal-accepted-{project_id}",
            decided_at=recorded_at + timedelta(seconds=7),
        )
    )
    _complete_attempt(
        session,
        project_id=project_id,
        attempt=goal_attempt,
        recorded_at=recorded_at + timedelta(seconds=7),
    )
    return vision, goal


def seed_accepted_specification(  # noqa: PLR0915
    session: Session,
    *,
    project_id: int,
    content: str,
    content_ref: str | None = None,
    recorded_at: datetime | None = None,
) -> PersistedSpecificationLineage:
    """Persist a complete accepted v2 specification under the active Goal."""
    raw_parsed: object = json.loads(content)
    if not isinstance(raw_parsed, dict):
        message = "Specification fixture content must be a JSON object."
        raise TypeError(message)
    parsed = cast("dict[str, object]", raw_parsed)
    existing_specs = session.exec(
        select(SpecRegistry)
        .where(col(SpecRegistry.project_id) == project_id)
        .order_by(col(SpecRegistry.spec_version_id))
    ).all()
    ordinal = len(existing_specs) + 1
    base_time = recorded_at or datetime.now(UTC) + timedelta(minutes=ordinal * 10)
    vision, goal = _seed_accepted_vision_and_goal(
        session,
        project_id=project_id,
        recorded_at=base_time,
    )
    vision_id = _required(vision.vision_artifact_id, "Vision")
    goal_id = _required(goal.product_goal_artifact_id, "Product Goal")
    current_spec = next(
        (row for row in existing_specs if row.status == "approved"),
        None,
    )
    prior_candidate = (
        None
        if current_spec is None
        else session.get(
            SpecificationCandidate,
            current_spec.source_specification_candidate_id,
        )
    )
    base_payload: SpecificationPayload | None = None
    if prior_candidate is not None:
        base_payload, _base_envelope = load_candidate_contract(
            prior_candidate.canonical_envelope_json,
            expected_candidate_fingerprint=prior_candidate.candidate_fingerprint,
        )
    if parsed.get("schema_version") == "agileforge.spec.v2":
        payload = SpecificationPayload.model_validate(parsed)
    else:
        authored_content = canonical_json(parsed)
        payload = SpecificationPayload.model_validate(
            {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": f"SPEC.fixture-{project_id}",
                "title": "Fixture specification",
                "summary": authored_content,
                "problem_statement": "Exercise the accepted specification lifecycle.",
                "items": [
                    {
                        "id": "GOAL.fixture.accepted-specification",
                        "type": "GOAL",
                        "title": "Accepted specification",
                        "statement": authored_content,
                        "acceptance": ["The specification is accepted exactly once."],
                    }
                ],
                "relations": [],
                "controlled_terms": [],
                "external_references": [],
            }
        )
    candidate_attempt = _attempt(
        session,
        project_id=project_id,
        node_id="specification.structure",
        ordinal=ordinal,
        started_at=base_time + timedelta(seconds=8),
    )
    candidate_attempt_id = _required(
        candidate_attempt.workflow_node_attempt_id,
        "specification structuring attempt",
    )
    project = session.get(Project, project_id)
    if project is None:
        message = "Specification fixture Project is missing."
        raise AssertionError(message)
    repository_binding = _repository_binding_for_source(
        session,
        project=project,
        base_time=base_time,
    )
    source_bundle = SpecificationSourceBundle(
        source=_source_document(content.encode("utf-8")),
        context=SpecificationContextCapture(state="absent"),
        repository_revision=SpecificationRepositoryRevision(
            head_sha=repository_binding.head_sha,
            dirty=repository_binding.dirty,
            status_fingerprint=repository_binding.status_fingerprint,
        ),
        accepted_vision_fingerprint=vision.content_fingerprint,
        accepted_product_goal_fingerprint=goal.content_fingerprint,
    )
    prior_source = (
        None
        if prior_candidate is None
        else session.get(
            SpecificationSource,
            prior_candidate.specification_source_id,
        )
    )
    source = SpecificationSource(
        project_id=project_id,
        source_bundle_json=canonical_json(source_bundle.model_dump(mode="json")),
        source_fingerprint=source_bundle_fingerprint(source_bundle),
        repository_binding_id=_required(
            repository_binding.repository_binding_id,
            "repository binding",
        ),
        repository_head_sha=repository_binding.head_sha,
        repository_dirty=repository_binding.dirty,
        repository_status_fingerprint=repository_binding.status_fingerprint,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        supersedes_specification_source_id=(
            None if prior_source is None else prior_source.specification_source_id
        ),
        supersedes_source_fingerprint=(
            None if prior_source is None else prior_source.source_fingerprint
        ),
        registered_by="fixture",
        registered_at=base_time + timedelta(seconds=7, microseconds=500_000),
    )
    session.add(source)
    session.flush()
    source_id = _required(source.specification_source_id, "Specification source")
    source_manifest = (
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
            source_id=source_bundle.source.source_id,
            kind=CandidateSourceKind.EXTERNAL,
            fingerprint=source_bundle.source.content_fingerprint,
        ),
    )
    structuring_input = SpecificationStructuringInput(
        project_id=project_id,
        project_name=project.name,
        operation="initial" if current_spec is None else "amendment",
        accepted_vision=AcceptedVisionContext(
            artifact_id=vision_id,
            fingerprint=vision.content_fingerprint,
            statement=vision.statement,
            components=cast("JsonObject", json.loads(vision.components_json)),
            component_basis=tuple(
                cast("list[JsonObject]", json.loads(vision.component_basis_json))
            ),
            assumptions=tuple(
                cast("list[JsonObject]", json.loads(vision.assumptions_json))
            ),
            conflicts=tuple(
                cast("list[JsonObject]", json.loads(vision.conflicts_json))
            ),
        ),
        accepted_product_goal=AcceptedProductGoalContext(
            artifact_id=goal_id,
            fingerprint=goal.content_fingerprint,
            statement=goal.statement,
        ),
        registered_source=RegisteredSpecificationSource(
            specification_source_id=source_id,
            source_fingerprint=source.source_fingerprint,
            producer_capability=source_bundle.producer_capability,
            preparation_capability=source_bundle.preparation_capability,
            source=_structuring_document(source_bundle.source),
            context=SpecificationStructuringContextCapture(state="absent"),
            adrs=(),
            repository_revision=source_bundle.repository_revision,
            repository_evidence=RegisteredRepositoryEvidence(
                repository_binding_id=_required(
                    repository_binding.repository_binding_id,
                    "repository binding",
                ),
                binding_fingerprint=repository_binding_fingerprint(repository_binding),
                head_sha=repository_binding.head_sha,
                branch_name=repository_binding.branch_name,
                detached_head=repository_binding.detached_head,
                dirty=repository_binding.dirty,
                status_fingerprint=repository_binding.status_fingerprint,
                status_entries=tuple(
                    cast(
                        "list[JsonObject]",
                        json.loads(repository_binding.status_entries_json),
                    )
                ),
                remotes=tuple(
                    cast("list[str]", json.loads(repository_binding.remotes_json))
                ),
                warnings=tuple(
                    cast(
                        "list[JsonObject]",
                        json.loads(repository_binding.warnings_json),
                    )
                ),
                probe_version=repository_binding.probe_version,
            ),
            accepted_vision_fingerprint=vision.content_fingerprint,
            accepted_product_goal_fingerprint=goal.content_fingerprint,
        ),
        source_manifest=source_manifest,
        base_specification=(
            None
            if current_spec is None or base_payload is None
            else BaseSpecificationContext(
                spec_version_id=_required(
                    current_spec.spec_version_id,
                    "base Specification",
                ),
                payload_fingerprint=current_spec.spec_hash,
                payload=base_payload,
            )
        ),
    )
    normalized_input = structuring_input.model_dump(mode="json")
    candidate_attempt.normalized_input_json = canonical_json(normalized_input)
    candidate_attempt.input_fingerprint = canonical_hash(normalized_input)
    candidate_attempt.attempt_fingerprint = workflow_node_attempt_fingerprint(
        {
            "attempt_id": candidate_attempt_id,
            "project_id": project_id,
            "node_id": candidate_attempt.node_id,
            "instance_key": candidate_attempt.instance_key,
            "graph_version": candidate_attempt.graph_version,
            "fact_fingerprint": candidate_attempt.fact_fingerprint,
            "business_fact_fingerprint": (candidate_attempt.business_fact_fingerprint),
            "decision_fingerprint": candidate_attempt.decision_fingerprint,
            "normalized_input": normalized_input,
            "input_fingerprint": candidate_attempt.input_fingerprint,
            "model_id": candidate_attempt.model_id,
            "execution_settings": {},
            "idempotency_key": candidate_attempt.idempotency_key,
            "actor": candidate_attempt.actor,
            "correlation_id": candidate_attempt.correlation_id,
            "started_at": candidate_attempt.started_at,
            "lease_expires_at": candidate_attempt.lease_expires_at,
        }
    )
    session.add(candidate_attempt)
    session.flush()
    envelope = build_candidate_envelope(
        payload=payload,
        metadata=CandidateBuildInput(
            candidate_kind=(
                CandidateKind.INITIAL
                if current_spec is None
                else CandidateKind.AMENDMENT
            ),
            accepted_vision_id=vision_id,
            accepted_vision_fingerprint=vision.content_fingerprint,
            accepted_product_goal_id=goal_id,
            accepted_product_goal_fingerprint=goal.content_fingerprint,
            registered_source_fingerprint=source.source_fingerprint,
            source_producer_capability=source_bundle.producer_capability,
            source_preparation_capability=source_bundle.preparation_capability,
            source_manifest=source_manifest,
            accepted_fact_fingerprint=specification_structuring_fact_fingerprint(
                structuring_input
            ),
            producer_input_fingerprint=specification_structuring_input_fingerprint(
                structuring_input
            ),
            producer_capability="specification-structurer",
            producer_version="fixture-v2",
            model_id="fake/product-definition",
            model_configuration_fingerprint=canonical_hash(
                {"model_id": "fake/product-definition"}
            ),
            prompt_version=SPECIFICATION_STRUCTURER_PROMPT_VERSION,
            prompt_fingerprint=canonical_hash(
                {"prompt": "fixture-specification-structurer-v1"}
            ),
            workflow_node_attempt_id=candidate_attempt_id,
            attempt_fingerprint=candidate_attempt.attempt_fingerprint,
            correlation_id=f"fixture-specification-{project_id}-{ordinal}",
            produced_at=base_time + timedelta(seconds=8),
            base_payload=base_payload,
            base_specification_id=(
                None if current_spec is None else current_spec.spec_version_id
            ),
            base_payload_fingerprint=(
                None if current_spec is None else current_spec.spec_hash
            ),
        ),
    )
    _ = content_ref
    candidate = SpecificationCandidate(
        project_id=project_id,
        candidate_kind=envelope.candidate_kind.value,
        specification_source_id=source_id,
        specification_source_fingerprint=source.source_fingerprint,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        base_spec_version_id=(
            None if current_spec is None else current_spec.spec_version_id
        ),
        base_spec_hash=None if current_spec is None else current_spec.spec_hash,
        canonical_envelope_json=canonical_candidate_json(payload, envelope),
        payload_fingerprint=envelope.payload_fingerprint,
        source_manifest_fingerprint=envelope.source_manifest_fingerprint,
        producer_input_fingerprint=envelope.producer_input_fingerprint,
        rendered_view_fingerprint=envelope.review_view_fingerprint,
        candidate_fingerprint=envelope.candidate_fingerprint,
        workflow_node_attempt_id=candidate_attempt_id,
        attempt_fingerprint=candidate_attempt.attempt_fingerprint,
        supersedes_specification_candidate_id=None,
        supersedes_candidate_fingerprint=None,
        recorded_by="fixture",
        recorded_at=base_time + timedelta(seconds=9),
    )
    session.add(candidate)
    session.flush()
    candidate_id = _required(candidate.specification_candidate_id, "specification")
    _complete_attempt(
        session,
        project_id=project_id,
        attempt=candidate_attempt,
        recorded_at=base_time + timedelta(seconds=9),
    )
    session.add(
        SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=candidate_id,
            candidate_fingerprint=envelope.candidate_fingerprint,
            decision="accepted",
            rationale="Accepted for fixture delivery.",
            reviewer="fixture",
            idempotency_key=f"fixture-specification-accepted-{project_id}-{ordinal}",
            decided_at=base_time + timedelta(seconds=10),
        )
    )
    if current_spec is not None:
        current_spec.status = "superseded"
        session.add(current_spec)
    spec = SpecRegistry(
        project_id=project_id,
        spec_hash=envelope.payload_fingerprint,
        status="approved",
        approved_at=base_time + timedelta(seconds=10),
        approved_by="fixture",
        approval_notes="Accepted for fixture delivery.",
        source_specification_candidate_id=candidate_id,
        source_specification_candidate_fingerprint=envelope.candidate_fingerprint,
        source_vision_artifact_id=vision_id,
        source_vision_fingerprint=vision.content_fingerprint,
        source_product_goal_artifact_id=goal_id,
        source_product_goal_fingerprint=goal.content_fingerprint,
        supersedes_spec_version_id=(
            None if current_spec is None else current_spec.spec_version_id
        ),
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return PersistedSpecificationLineage(
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        specification_candidate_id=candidate_id,
        specification_fingerprint=envelope.payload_fingerprint,
        candidate_fingerprint=envelope.candidate_fingerprint,
        spec=spec,
    )


__all__ = ["PersistedSpecificationLineage", "seed_accepted_specification"]
