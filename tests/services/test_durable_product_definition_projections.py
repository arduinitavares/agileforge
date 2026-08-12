"""Durable product-definition read projections."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter
from sqlmodel import Session

from models.core import Project
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt
from repositories.workflow import WorkflowFactRepository
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
    SPECIFICATION_SOURCE_CONTEXT_ID,
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
    specification_source_adr_id,
)
from services.read_projections import DurableReadProjectionService
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    build_candidate_envelope,
    canonical_candidate_json,
    render_candidate_review_markdown,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.contracts import (
    GRAPH_VERSION,
    JsonObject,
    JsonValue,
    NodeCategory,
    WorkflowPosition,
)
from workflow.definitions.product_discovery import select_product_definition_state
from workflow.definitions.product_goal import _goal_interview_rule, _goal_review_rule
from workflow.definitions.root import ROOT_GRAPH
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
    vision_interview_output_fingerprint,
    workflow_node_attempt_fingerprint,
)
from workflow.graph import RuleCategory

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Engine


NOW = datetime(2026, 8, 5, 14, tzinfo=UTC)
_JSON_OBJECT = TypeAdapter(JsonObject)


def _vision_output_fingerprint(
    components: Mapping[str, object],
    statement: str,
    is_complete: bool,
    questions: Sequence[Mapping[str, object]],
) -> str:
    """Build a valid complete Vision turn fingerprint for direct fixtures."""
    return vision_interview_output_fingerprint(
        components,
        statement,
        is_complete,
        questions,
        {"component_basis": (), "assumptions": (), "conflicts": ()},
    )


def test_public_product_definition_selection_retains_projection_state(
    engine: Engine,
) -> None:
    """Read projections share one stable selection interface with graph rules."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    selection = select_product_definition_state(snapshot)

    assert selection.specification_candidate is not None
    assert selection.accepted_spec is None
    assert not selection.has_conflict


def _add_vision_evidence_snapshot(
    session: Session,
    project_id: int,
    attempt_id: int,
    *,
    key: str,
) -> int:
    evidence_payload = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [
            {
                "evidence_id": "project:metadata",
                "kind": "project_metadata",
                "relative_path": None,
                "content_fingerprint": canonical_hash(
                    {"project_id": project_id, "key": key}
                ),
                "trust": "operator_provided",
                "content": {"project_id": project_id, "key": key},
                "truncated": False,
            }
        ],
        "warnings": [],
    }
    snapshot = VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=None,
        workflow_node_attempt_id=attempt_id,
        evidence_json=canonical_json(
            {
                **evidence_payload,
                "evidence_fingerprint": canonical_hash(evidence_payload),
            }
        ),
        evidence_fingerprint=canonical_hash(evidence_payload),
        warnings_json="[]",
        created_at=NOW,
    )
    session.add(snapshot)
    session.flush()
    assert snapshot.vision_evidence_snapshot_id is not None
    return snapshot.vision_evidence_snapshot_id


def _registered_document(
    *,
    source_id: str,
    relative_path: str,
    content: bytes,
) -> SpecificationSourceDocument:
    """Build one byte-exact registered document for projection tests."""
    return SpecificationSourceDocument(
        source_id=source_id,
        relative_path=relative_path,
        content_base64=base64.b64encode(content).decode("ascii"),
        byte_length=len(content),
        content_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
    )


def _structuring_document(
    document: SpecificationSourceDocument,
) -> SpecificationStructuringDocument:
    """Expose exact registered UTF-8 bytes to the structuring contract."""
    return SpecificationStructuringDocument(
        source_id=document.source_id,
        relative_path=document.relative_path,
        text=base64.b64decode(document.content_base64, validate=True).decode("utf-8"),
        byte_length=document.byte_length,
        content_fingerprint=document.content_fingerprint,
    )


def _seed_lineage(  # noqa: PLR0915
    engine: Engine,
    *,
    goal_number: int = 1,
) -> dict[str, object]:
    """Seed one valid durable Vision, Goal, and typed candidate chain."""
    with Session(engine) as session:
        project = Project(
            name="Projection contract",
            vision="mutable cache must not be read",
            spec_file_path="/must/not/be/read.json",
        )
        session.add(project)
        session.flush()
        assert project.project_id is not None
        attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="vision.bootstrap",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint=canonical_hash({"facts": goal_number}),
            business_fact_fingerprint=canonical_hash({"business": goal_number}),
            decision_fingerprint=canonical_hash({"decision": goal_number}),
            normalized_input_json="{}",
            input_fingerprint=canonical_hash({"input": goal_number}),
            model_id="fake/product-definition",
            execution_settings_json="{}",
            idempotency_key=f"attempt-{goal_number}",
            actor="operator",
            correlation_id=None,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=canonical_hash({"attempt": goal_number}),
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        snapshot_id = _add_vision_evidence_snapshot(
            session,
            project.project_id,
            attempt.workflow_node_attempt_id,
            key=f"lineage-{goal_number}",
        )

        vision_components = {"purpose": "durable reads"}
        vision_turn = VisionInterviewTurn(
            project_id=project.project_id,
            operation="bootstrap",
            turn_number=1,
            revision_intent_id=None,
            vision_evidence_snapshot_id=snapshot_id,
            prior_turn_id=None,
            user_text=None,
            components_json=canonical_json(vision_components),
            vision_statement="A durable Vision.",
            is_complete=True,
            clarifying_questions_json="[]",
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                vision_components,
                "A durable Vision.",
                True,
                [],
            ),
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW,
        )
        session.add(vision_turn)
        session.flush()
        assert vision_turn.vision_interview_turn_id is not None
        vision = VisionArtifact(
            project_id=project.project_id,
            version_number=1,
            components_json=canonical_json(vision_components),
            statement="A durable Vision.",
            content_fingerprint=canonical_hash(
                {"components": vision_components, "statement": "A durable Vision."}
            ),
            vision_evidence_snapshot_id=snapshot_id,
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=None,
            source_interview_turn_id=vision_turn.vision_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=1),
        )
        session.add(vision)
        session.flush()
        assert vision.vision_artifact_id is not None
        session.add(
            VisionArtifactDecision(
                project_id=project.project_id,
                vision_artifact_id=vision.vision_artifact_id,
                artifact_fingerprint=vision.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key=f"vision-{goal_number}",
                decided_at=NOW + timedelta(seconds=2),
            )
        )

        goal_components = {
            "valuable_future_state": "Reliable decisions",
            "beneficiary": "Operators",
            "value": "Confidence",
            "success_signals": ["Measured outcomes"],
            "boundaries": ["No implementation"],
        }
        goal_statement = f"Goal {goal_number}: reliable decisions."
        goal_turn = ProductGoalInterviewTurn(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            goal_number=goal_number,
            revision_number=1,
            prior_turn_id=None,
            user_text="Define goal",
            components_json=canonical_json(goal_components),
            goal_statement=goal_statement,
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=product_goal_interview_output_fingerprint(
                goal_components, goal_statement, True, ()
            ),
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=3),
        )
        session.add(goal_turn)
        session.flush()
        assert goal_turn.product_goal_interview_turn_id is not None
        goal = ProductGoalArtifact(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            goal_number=goal_number,
            revision_number=1,
            statement=goal_statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                goal_components, goal_statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=goal_turn.product_goal_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(goal)
        session.flush()
        assert goal.product_goal_artifact_id is not None
        session.add(
            ProductGoalArtifactDecision(
                project_id=project.project_id,
                product_goal_artifact_id=goal.product_goal_artifact_id,
                artifact_fingerprint=goal.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key=f"goal-{goal_number}",
                decided_at=NOW + timedelta(seconds=5),
            )
        )

        repository_status_fingerprint = canonical_hash(
            {"projection_repository": goal_number}
        )
        repository_binding = RepositoryBinding(
            project_id=project.project_id,
            worktree_path="/projection/repository",
            common_git_dir="/projection/repository/.git",
            head_sha="a" * 40,
            branch_name="dev/projection",
            detached_head=False,
            dirty=False,
            status_fingerprint=repository_status_fingerprint,
            status_entries_json="[]",
            remotes_json="[]",
            warnings_json="[]",
            probe_version="agileforge.repository-probe.v1",
            inspected_at=NOW + timedelta(seconds=5),
            supersedes_repository_binding_id=None,
            recorded_by="operator",
        )
        session.add(repository_binding)
        session.flush()
        assert repository_binding.repository_binding_id is not None
        project.active_repository_binding_id = repository_binding.repository_binding_id
        session.add(project)
        source_document = _registered_document(
            source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
            relative_path="SPECIFICATION.md",
            content=b"REGISTERED_TO_SPEC_SOURCE_MUST_NOT_LEAK\n",
        )
        context_document = _registered_document(
            source_id=SPECIFICATION_SOURCE_CONTEXT_ID,
            relative_path="CONTEXT.md",
            content=b"REGISTERED_CONTEXT_MUST_NOT_LEAK\n",
        )
        adr_document = _registered_document(
            source_id=specification_source_adr_id("docs/adr/0001-projection.md"),
            relative_path="docs/adr/0001-projection.md",
            content=b"REGISTERED_ADR_MUST_NOT_LEAK\n",
        )
        source_bundle = SpecificationSourceBundle(
            source=source_document,
            context=SpecificationContextCapture(
                state="present",
                document=context_document,
            ),
            adrs=(adr_document,),
            repository_revision=SpecificationRepositoryRevision(
                head_sha=repository_binding.head_sha,
                branch_name=repository_binding.branch_name,
                detached_head=repository_binding.detached_head,
                dirty=repository_binding.dirty,
                status_fingerprint=repository_binding.status_fingerprint,
            ),
            accepted_vision_fingerprint=vision.content_fingerprint,
            accepted_product_goal_fingerprint=goal.content_fingerprint,
        )
        source = SpecificationSource(
            project_id=project.project_id,
            source_bundle_json=canonical_json(source_bundle.model_dump(mode="json")),
            source_fingerprint=source_bundle_fingerprint(source_bundle),
            repository_binding_id=repository_binding.repository_binding_id,
            repository_head_sha=repository_binding.head_sha,
            repository_dirty=repository_binding.dirty,
            repository_status_fingerprint=repository_binding.status_fingerprint,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            product_goal_artifact_id=goal.product_goal_artifact_id,
            product_goal_fingerprint=goal.content_fingerprint,
            supersedes_specification_source_id=None,
            supersedes_source_fingerprint=None,
            registered_by="operator",
            registered_at=NOW + timedelta(seconds=6),
        )
        session.add(source)
        session.flush()
        assert source.specification_source_id is not None

        specification_attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="specification.structure",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint=canonical_hash({"specification": "facts"}),
            business_fact_fingerprint=canonical_hash({"specification": "business"}),
            decision_fingerprint=canonical_hash({"specification": "decision"}),
            normalized_input_json="{}",
            input_fingerprint=canonical_hash({"specification": "input"}),
            model_id="fake/product-definition",
            execution_settings_json="{}",
            idempotency_key=f"specification-attempt-{goal_number}",
            actor="operator",
            correlation_id=f"specification-{goal_number}",
            started_at=NOW + timedelta(seconds=6),
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=canonical_hash(
                {"specification": "attempt", "goal_number": goal_number}
            ),
        )
        session.add(specification_attempt)
        session.flush()
        assert specification_attempt.workflow_node_attempt_id is not None
        specification_payload = SpecificationPayload.model_validate(
            {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": f"SPEC.projection-{goal_number}",
                "title": "Durable specification",
                "summary": "Project durable typed specification review data.",
                "problem_statement": "Operators need exact durable review packets.",
                "items": [
                    {
                        "id": f"GOAL.projection-{goal_number}",
                        "type": "GOAL",
                        "title": "Durable review",
                        "statement": "Expose the exact persisted candidate.",
                        "acceptance": ["The review packet is deterministic."],
                        "source_notes": [
                            {
                                "source_id": SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
                                "kind": "interview",
                                "text": "Accepted Product Goal source.",
                                "external_ref_id": "EXT.issue-199",
                            }
                        ],
                    },
                    {
                        "id": f"REQ.projection-{goal_number}",
                        "type": "REQ",
                        "title": "Exact candidate",
                        "statement": "The packet MUST expose exact v2 bytes.",
                        "level": "MUST",
                        "verification": "system-test",
                        "acceptance": ["Payload and envelope fingerprints match."],
                    },
                ],
                "relations": [
                    {
                        "from": f"REQ.projection-{goal_number}",
                        "type": "satisfies",
                        "to": f"GOAL.projection-{goal_number}",
                    }
                ],
                "controlled_terms": [],
                "external_references": [
                    {
                        "id": "EXT.issue-199",
                        "title": "Issue 199",
                        "url": "https://github.com/arduinitavares/agileforge/issues/199",
                        "summary": "Approved hard-break contract.",
                    }
                ],
            }
        )
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
                source_id=source_document.source_id,
                kind=CandidateSourceKind.EXTERNAL,
                fingerprint=source_document.content_fingerprint,
            ),
            CandidateSourceManifestEntry(
                source_id=context_document.source_id,
                kind=CandidateSourceKind.REPOSITORY,
                fingerprint=context_document.content_fingerprint,
            ),
            CandidateSourceManifestEntry(
                source_id=adr_document.source_id,
                kind=CandidateSourceKind.REPOSITORY,
                fingerprint=adr_document.content_fingerprint,
            ),
        )
        structuring_input = SpecificationStructuringInput(
            project_id=project.project_id,
            project_name=project.name,
            operation="initial",
            accepted_vision=AcceptedVisionContext(
                artifact_id=vision.vision_artifact_id,
                fingerprint=vision.content_fingerprint,
                statement=vision.statement,
                components=_JSON_OBJECT.validate_python(vision_components),
            ),
            accepted_product_goal=AcceptedProductGoalContext(
                artifact_id=goal.product_goal_artifact_id,
                fingerprint=goal.content_fingerprint,
                statement=goal.statement,
            ),
            registered_source=RegisteredSpecificationSource(
                specification_source_id=source.specification_source_id,
                source_fingerprint=source.source_fingerprint,
                producer_capability=source_bundle.producer_capability,
                preparation_capability=source_bundle.preparation_capability,
                source=_structuring_document(source_document),
                context=SpecificationStructuringContextCapture(
                    state="present",
                    document=_structuring_document(context_document),
                ),
                adrs=(_structuring_document(adr_document),),
                repository_revision=source_bundle.repository_revision,
                repository_evidence=RegisteredRepositoryEvidence(
                    repository_binding_id=repository_binding.repository_binding_id,
                    binding_fingerprint=repository_binding_fingerprint(
                        repository_binding
                    ),
                    head_sha=repository_binding.head_sha,
                    branch_name=repository_binding.branch_name,
                    detached_head=repository_binding.detached_head,
                    dirty=repository_binding.dirty,
                    status_fingerprint=repository_binding.status_fingerprint,
                    status_entries=(),
                    remotes=(),
                    warnings=(),
                    probe_version=repository_binding.probe_version,
                ),
                accepted_vision_fingerprint=vision.content_fingerprint,
                accepted_product_goal_fingerprint=goal.content_fingerprint,
            ),
            source_manifest=source_manifest,
        )
        normalized_input = structuring_input.model_dump(mode="json")
        specification_attempt.normalized_input_json = canonical_json(normalized_input)
        specification_attempt.input_fingerprint = canonical_hash(normalized_input)
        specification_attempt.attempt_fingerprint = workflow_node_attempt_fingerprint(
            {
                "attempt_id": specification_attempt.workflow_node_attempt_id,
                "project_id": specification_attempt.project_id,
                "node_id": specification_attempt.node_id,
                "instance_key": specification_attempt.instance_key,
                "graph_version": specification_attempt.graph_version,
                "fact_fingerprint": specification_attempt.fact_fingerprint,
                "business_fact_fingerprint": (
                    specification_attempt.business_fact_fingerprint
                ),
                "decision_fingerprint": specification_attempt.decision_fingerprint,
                "normalized_input": normalized_input,
                "input_fingerprint": specification_attempt.input_fingerprint,
                "model_id": specification_attempt.model_id,
                "execution_settings": {},
                "idempotency_key": specification_attempt.idempotency_key,
                "actor": specification_attempt.actor,
                "correlation_id": specification_attempt.correlation_id,
                "started_at": specification_attempt.started_at,
                "lease_expires_at": specification_attempt.lease_expires_at,
            }
        )
        session.add(specification_attempt)
        session.flush()
        envelope = build_candidate_envelope(
            payload=specification_payload,
            metadata=CandidateBuildInput(
                candidate_kind=CandidateKind.INITIAL,
                accepted_vision_id=vision.vision_artifact_id,
                accepted_vision_fingerprint=vision.content_fingerprint,
                accepted_product_goal_id=goal.product_goal_artifact_id,
                accepted_product_goal_fingerprint=goal.content_fingerprint,
                registered_source_fingerprint=source.source_fingerprint,
                source_producer_capability=source_bundle.producer_capability,
                source_preparation_capability=(source_bundle.preparation_capability),
                source_manifest=source_manifest,
                accepted_fact_fingerprint=specification_structuring_fact_fingerprint(
                    structuring_input
                ),
                producer_input_fingerprint=(
                    specification_structuring_input_fingerprint(structuring_input)
                ),
                producer_capability="specification-structurer",
                producer_version="1.0.0",
                model_id=specification_attempt.model_id,
                model_configuration_fingerprint=canonical_hash(
                    {"model": specification_attempt.model_id}
                ),
                prompt_version=SPECIFICATION_STRUCTURER_PROMPT_VERSION,
                prompt_fingerprint=canonical_hash({"prompt": "specification-v2"}),
                workflow_node_attempt_id=(
                    specification_attempt.workflow_node_attempt_id
                ),
                attempt_fingerprint=specification_attempt.attempt_fingerprint,
                correlation_id=f"specification-{goal_number}",
                produced_at=NOW + timedelta(seconds=7),
            ),
        )
        candidate = SpecificationCandidate(
            project_id=project.project_id,
            candidate_kind="initial",
            specification_source_id=source.specification_source_id,
            specification_source_fingerprint=source.source_fingerprint,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            product_goal_artifact_id=goal.product_goal_artifact_id,
            product_goal_fingerprint=goal.content_fingerprint,
            base_spec_version_id=None,
            base_spec_hash=None,
            canonical_envelope_json=canonical_candidate_json(
                specification_payload, envelope
            ),
            payload_fingerprint=envelope.payload_fingerprint,
            source_manifest_fingerprint=envelope.source_manifest_fingerprint,
            producer_input_fingerprint=envelope.producer_input_fingerprint,
            rendered_view_fingerprint=envelope.review_view_fingerprint,
            candidate_fingerprint=envelope.candidate_fingerprint,
            workflow_node_attempt_id=specification_attempt.workflow_node_attempt_id,
            attempt_fingerprint=specification_attempt.attempt_fingerprint,
            supersedes_specification_candidate_id=None,
            supersedes_candidate_fingerprint=None,
            recorded_by="operator",
            recorded_at=NOW + timedelta(seconds=7),
        )
        session.add(candidate)
        session.flush()
        assert candidate.specification_candidate_id is not None
        result = {
            "project_id": project.project_id,
            "vision_id": vision.vision_artifact_id,
            "vision_fingerprint": vision.content_fingerprint,
            "goal_id": goal.product_goal_artifact_id,
            "goal_fingerprint": goal.content_fingerprint,
            "goal_statement": goal.statement,
            "source_id": source.specification_source_id,
            "source_fingerprint": source.source_fingerprint,
            "source_document_fingerprint": source_document.content_fingerprint,
            "context_document_fingerprint": context_document.content_fingerprint,
            "adr_document_fingerprint": adr_document.content_fingerprint,
            "repository_binding_id": repository_binding.repository_binding_id,
            "repository_status_fingerprint": (repository_binding.status_fingerprint),
            "candidate_id": candidate.specification_candidate_id,
            "candidate_fingerprint": candidate.candidate_fingerprint,
            "payload_fingerprint": candidate.payload_fingerprint,
            "source_manifest_fingerprint": candidate.source_manifest_fingerprint,
            "producer_input_fingerprint": candidate.producer_input_fingerprint,
            "rendered_view_fingerprint": candidate.rendered_view_fingerprint,
            "specification_payload": specification_payload.model_dump(mode="json"),
            "rendered_markdown": render_candidate_review_markdown(
                specification_payload, envelope
            ),
            "source_manifest": [
                item.model_dump(mode="json") for item in envelope.source_manifest
            ],
            "accepted_fact_fingerprint": envelope.accepted_fact_fingerprint,
            "attempt_id": attempt.workflow_node_attempt_id,
            "attempt_fingerprint": attempt.attempt_fingerprint,
            "specification_attempt_id": (
                specification_attempt.workflow_node_attempt_id
            ),
            "specification_attempt_fingerprint": (
                specification_attempt.attempt_fingerprint
            ),
        }
        session.commit()
        return result


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return _JSON_OBJECT.validate_python(value)


def _data(result: JsonObject) -> JsonObject:
    assert result["ok"] is True
    return _json_object(result["data"])


def _error_code(result: JsonObject) -> str:
    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    assert errors
    code = _json_object(errors[0])["code"]
    assert isinstance(code, str)
    return code


def _stored_iso(value: datetime) -> str:
    """Match SQLite's durable naive-datetime representation."""
    return value.replace(tzinfo=None).isoformat()


def _assert_complete_candidate_projection(
    candidate: JsonObject,
    seeded: dict[str, object],
    *,
    decision_state: str,
) -> None:
    """Assert one packet exposes the full immutable v2 review contract."""
    assert set(candidate) == {
        "specification_candidate_id",
        "envelope_version",
        "candidate_kind",
        "canonical_payload",
        "rendered_markdown",
        "vision_artifact_id",
        "vision_fingerprint",
        "product_goal_artifact_id",
        "product_goal_fingerprint",
        "base_spec_version_id",
        "base_spec_hash",
        "specification_source_id",
        "registered_source_fingerprint",
        "source_producer_capability",
        "source_preparation_capability",
        "source_manifest",
        "source_manifest_fingerprint",
        "accepted_fact_fingerprint",
        "producer_input_fingerprint",
        "producer_capability",
        "producer_version",
        "model_id",
        "model_configuration_fingerprint",
        "prompt_fingerprint",
        "prompt_version",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "correlation_id",
        "produced_at",
        "payload_fingerprint",
        "profile_version",
        "renderer_version",
        "rendered_view_fingerprint",
        "amendment_diff",
        "candidate_fingerprint",
        "supersedes_specification_candidate_id",
        "supersedes_candidate_fingerprint",
        "decision_state",
    }
    assert candidate["canonical_payload"] == seeded["specification_payload"]
    assert candidate["rendered_markdown"] == seeded["rendered_markdown"]
    assert candidate["vision_artifact_id"] == seeded["vision_id"]
    assert candidate["vision_fingerprint"] == seeded["vision_fingerprint"]
    assert candidate["product_goal_artifact_id"] == seeded["goal_id"]
    assert candidate["product_goal_fingerprint"] == seeded["goal_fingerprint"]
    assert candidate["specification_source_id"] == seeded["source_id"]
    assert candidate["registered_source_fingerprint"] == seeded["source_fingerprint"]
    assert candidate["source_producer_capability"] == "to-spec"
    assert candidate["source_preparation_capability"] == "grill-with-docs"
    assert candidate["producer_capability"] == "specification-structurer"
    assert candidate["prompt_version"] == SPECIFICATION_STRUCTURER_PROMPT_VERSION
    assert candidate["source_manifest"] == seeded["source_manifest"]
    assert (
        candidate["source_manifest_fingerprint"]
        == seeded["source_manifest_fingerprint"]
    )
    assert candidate["accepted_fact_fingerprint"] == seeded["accepted_fact_fingerprint"]
    assert (
        candidate["producer_input_fingerprint"] == seeded["producer_input_fingerprint"]
    )
    assert candidate["workflow_node_attempt_id"] == seeded["specification_attempt_id"]
    assert (
        candidate["attempt_fingerprint"] == seeded["specification_attempt_fingerprint"]
    )
    assert candidate["payload_fingerprint"] == seeded["payload_fingerprint"]
    assert candidate["rendered_view_fingerprint"] == seeded["rendered_view_fingerprint"]
    assert candidate["candidate_fingerprint"] == seeded["candidate_fingerprint"]
    assert candidate["base_spec_version_id"] is None
    assert candidate["base_spec_hash"] is None
    assert candidate["amendment_diff"] is None
    assert candidate["supersedes_specification_candidate_id"] is None
    assert candidate["supersedes_candidate_fingerprint"] is None
    assert candidate["decision_state"] == decision_state
    assert "content_ref" not in candidate
    assert "raw_input" not in candidate


def _seeded_int(seeded: dict[str, object], key: str) -> int:
    value = seeded[key]
    assert isinstance(value, int)
    return value


def _seed_interview_project(engine: Engine) -> dict[str, object]:
    """Seed one Project and durable attempt used by direct fact fixtures."""
    with Session(engine) as session:
        project = Project(
            name="Interview read contract",
            description="Durable human review state",
        )
        session.add(project)
        session.flush()
        assert project.project_id is not None
        attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="vision.interview",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:interview-facts",
            business_fact_fingerprint="sha256:interview-business",
            decision_fingerprint="sha256:interview-decision",
            normalized_input_json="{}",
            input_fingerprint="sha256:interview-input",
            model_id="fake/interview-read-contract",
            execution_settings_json="{}",
            idempotency_key="interview-read-attempt",
            actor="operator",
            correlation_id=None,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint="sha256:interview-attempt",
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        snapshot_id = _add_vision_evidence_snapshot(
            session,
            project.project_id,
            attempt.workflow_node_attempt_id,
            key="interview-read",
        )
        result = {
            "project_id": project.project_id,
            "attempt_id": attempt.workflow_node_attempt_id,
            "attempt_fingerprint": attempt.attempt_fingerprint,
            "vision_evidence_snapshot_id": snapshot_id,
        }
        session.commit()
        return result


def _vision_components(*, complete: bool) -> JsonObject:
    return {
        "project_name": "AgileForge",
        "target_user": "Product teams",
        "problem": "Workflow state is hard to review",
        "product_category": "Product delivery system",
        "key_benefit": "Durable review context",
        "competitors": "Mutable chat history" if complete else None,
        "differentiator": "Immutable workflow facts" if complete else None,
    }


def _goal_components(*, complete: bool) -> JsonObject:
    return {
        "valuable_future_state": "Every review uses durable facts",
        "beneficiary": "Product operators",
        "value": "Reliable decisions",
        "success_signals": ["Exact candidates are reviewable"],
        "boundaries": ["No mutable cache reads"] if complete else [],
    }


def _vision_question_payload(questions: tuple[str, ...]) -> list[JsonObject]:
    return [
        {
            "question_id": f"q{index + 1}",
            "text": question,
            "affected_components": ["competitors"],
            "conflict_ids": [],
        }
        for index, question in enumerate(questions)
    ]


def _vision_display_material(
    components: JsonObject,
    statement: str,
    questions: tuple[str, ...] = (),
) -> JsonObject:
    """Return the display-safe Vision shape expected from direct fixtures."""
    return {
        "statement": statement,
        "components": [
            {"name": name, "value": value, "source_kinds": []}
            for name, value in components.items()
        ],
        "assumptions": [],
        "conflicts": [],
        "questions": [
            {
                "question_id": f"q{index + 1}",
                "text": question,
                "affected_components": ["competitors"],
            }
            for index, question in enumerate(questions)
        ],
    }


@dataclass(frozen=True)
class _VisionTurnSeed:
    """Input values for one direct Vision turn fixture."""

    components: JsonObject
    statement: str
    is_complete: bool
    questions: tuple[str, ...]
    turn_number: int
    prior_turn_id: int | None
    recorded_at: datetime


@dataclass(frozen=True)
class _GoalTurnSeed:
    """Input values for one direct Product Goal turn fixture."""

    components: JsonObject
    statement: str
    is_complete: bool
    questions: tuple[str, ...]
    goal_number: int
    revision_number: int
    prior_turn_id: int | None
    recorded_at: datetime


def _add_vision_turn(
    engine: Engine,
    seeded: dict[str, object],
    turn_seed: _VisionTurnSeed,
) -> int:
    project_id = seeded["project_id"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    snapshot_id = seeded["vision_evidence_snapshot_id"]
    assert isinstance(project_id, int)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    assert isinstance(snapshot_id, int)
    questions = _vision_question_payload(turn_seed.questions)
    operation = "bootstrap" if turn_seed.prior_turn_id is None else "clarification"
    with Session(engine) as session:
        turn = VisionInterviewTurn(
            project_id=project_id,
            operation=operation,
            turn_number=turn_seed.turn_number,
            revision_intent_id=None,
            vision_evidence_snapshot_id=snapshot_id,
            prior_turn_id=turn_seed.prior_turn_id,
            user_text=(
                None
                if operation == "bootstrap"
                else f"Vision answer {turn_seed.turn_number}"
            ),
            components_json=canonical_json(turn_seed.components),
            vision_statement=turn_seed.statement,
            is_complete=turn_seed.is_complete,
            clarifying_questions_json=canonical_json(questions),
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                turn_seed.components,
                turn_seed.statement,
                turn_seed.is_complete,
                questions,
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=turn_seed.recorded_at,
        )
        session.add(turn)
        session.commit()
        session.refresh(turn)
        assert turn.vision_interview_turn_id is not None
        return turn.vision_interview_turn_id


def _seed_vision_candidate(
    engine: Engine,
    *,
    decision: str | None = None,
) -> dict[str, object]:
    seeded = _seed_interview_project(engine)
    components = _vision_components(complete=True)
    statement = "Product teams review exact durable workflow state."
    turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            turn_number=1,
            prior_turn_id=None,
            recorded_at=NOW,
        ),
    )
    project_id = seeded["project_id"]
    snapshot_id = seeded["vision_evidence_snapshot_id"]
    assert isinstance(project_id, int)
    assert isinstance(snapshot_id, int)
    with Session(engine) as session:
        artifact = VisionArtifact(
            project_id=project_id,
            version_number=1,
            components_json=canonical_json(components),
            statement=statement,
            content_fingerprint=canonical_hash(
                {"components": components, "statement": statement}
            ),
            vision_evidence_snapshot_id=snapshot_id,
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=1),
        )
        session.add(artifact)
        session.flush()
        assert artifact.vision_artifact_id is not None
        if decision is not None:
            session.add(
                VisionArtifactDecision(
                    project_id=project_id,
                    vision_artifact_id=artifact.vision_artifact_id,
                    artifact_fingerprint=artifact.content_fingerprint,
                    decision=decision,
                    rationale=f"Vision {decision} rationale.",
                    reviewer="vision-reviewer",
                    idempotency_key=f"vision-{decision}",
                    decided_at=NOW + timedelta(seconds=2),
                )
            )
        seeded.update(
            vision_id=artifact.vision_artifact_id,
            vision_fingerprint=artifact.content_fingerprint,
            vision_statement=artifact.statement,
            vision_components=components,
            vision_turn_id=turn_id,
        )
        session.commit()
    return seeded


def _add_goal_turn(
    engine: Engine,
    seeded: dict[str, object],
    turn_seed: _GoalTurnSeed,
) -> int:
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    with Session(engine) as session:
        turn = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=turn_seed.goal_number,
            revision_number=turn_seed.revision_number,
            prior_turn_id=turn_seed.prior_turn_id,
            user_text=(
                f"Goal answer {turn_seed.goal_number}.{turn_seed.revision_number}"
            ),
            components_json=canonical_json(turn_seed.components),
            goal_statement=turn_seed.statement,
            is_complete=turn_seed.is_complete,
            clarifying_questions_json=canonical_json(list(turn_seed.questions)),
            output_fingerprint=product_goal_interview_output_fingerprint(
                turn_seed.components,
                turn_seed.statement,
                turn_seed.is_complete,
                turn_seed.questions,
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=turn_seed.recorded_at,
        )
        session.add(turn)
        session.commit()
        session.refresh(turn)
        assert turn.product_goal_interview_turn_id is not None
        return turn.product_goal_interview_turn_id


def _seed_goal_candidate(
    engine: Engine,
    *,
    decision: str | None = None,
) -> dict[str, object]:
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _goal_components(complete=True)
    statement = "Make every product-definition review durable."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            goal_number=1,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    with Session(engine) as session:
        artifact = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=1,
            revision_number=1,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(artifact)
        session.flush()
        assert artifact.product_goal_artifact_id is not None
        if decision is not None:
            session.add(
                ProductGoalArtifactDecision(
                    project_id=project_id,
                    product_goal_artifact_id=artifact.product_goal_artifact_id,
                    artifact_fingerprint=artifact.content_fingerprint,
                    decision=decision,
                    rationale=f"Goal {decision} rationale.",
                    reviewer="goal-reviewer",
                    idempotency_key=f"goal-{decision}",
                    decided_at=NOW + timedelta(seconds=5),
                )
            )
        seeded.update(
            goal_id=artifact.product_goal_artifact_id,
            goal_fingerprint=artifact.content_fingerprint,
            goal_statement=artifact.statement,
            goal_components=components,
            goal_turn_id=turn_id,
        )
        session.commit()
    return seeded


def _seed_superseded_vision_with_stale_open_intent(
    engine: Engine,
) -> dict[str, object]:
    """Persist an open intent on Vision A after accepted Vision B supersedes it."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    project_id = _seeded_int(seeded, "project_id")
    vision_id = _seeded_int(seeded, "vision_id")
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = _seeded_int(seeded, "attempt_id")
    snapshot_id = _seeded_int(seeded, "vision_evidence_snapshot_id")
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_fingerprint, str)
    with Session(engine) as session:
        stale_intent = VisionRevisionIntent(
            project_id=project_id,
            source_vision_artifact_id=vision_id,
            source_vision_fingerprint=vision_fingerprint,
            reason="Keep an obsolete revision interview open.",
            initiated_by="operator",
            initiated_at=NOW + timedelta(seconds=3),
        )
        replacement_intent = VisionRevisionIntent(
            project_id=project_id,
            source_vision_artifact_id=vision_id,
            source_vision_fingerprint=vision_fingerprint,
            reason="Create the selected replacement Vision.",
            initiated_by="operator",
            initiated_at=NOW + timedelta(seconds=4),
        )
        session.add(stale_intent)
        session.add(replacement_intent)
        session.flush()
        assert stale_intent.vision_revision_intent_id is not None
        assert replacement_intent.vision_revision_intent_id is not None

        source_snapshot = session.get(VisionEvidenceSnapshot, snapshot_id)
        assert source_snapshot is not None
        stale_snapshot = VisionEvidenceSnapshot(
            project_id=project_id,
            repository_binding_id=source_snapshot.repository_binding_id,
            workflow_node_attempt_id=attempt_id,
            evidence_json=source_snapshot.evidence_json,
            evidence_fingerprint=source_snapshot.evidence_fingerprint,
            warnings_json=source_snapshot.warnings_json,
            created_at=NOW + timedelta(seconds=4),
        )
        replacement_snapshot = VisionEvidenceSnapshot(
            project_id=project_id,
            repository_binding_id=source_snapshot.repository_binding_id,
            workflow_node_attempt_id=attempt_id,
            evidence_json=source_snapshot.evidence_json,
            evidence_fingerprint=source_snapshot.evidence_fingerprint,
            warnings_json=source_snapshot.warnings_json,
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(stale_snapshot)
        session.add(replacement_snapshot)
        session.flush()
        assert stale_snapshot.vision_evidence_snapshot_id is not None
        assert replacement_snapshot.vision_evidence_snapshot_id is not None

        stale_components = _vision_components(complete=False)
        stale_statement = "An obsolete Vision revision interview."
        stale_questions = [
            {
                "question_id": "stale-q1",
                "prompt": "What should the obsolete revision emphasize?",
            }
        ]
        stale_turn = VisionInterviewTurn(
            project_id=project_id,
            operation="revision",
            turn_number=1,
            revision_intent_id=stale_intent.vision_revision_intent_id,
            vision_evidence_snapshot_id=(stale_snapshot.vision_evidence_snapshot_id),
            prior_turn_id=None,
            user_text="Continue revising Vision A.",
            components_json=canonical_json(stale_components),
            vision_statement=stale_statement,
            is_complete=False,
            clarifying_questions_json=canonical_json(stale_questions),
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                stale_components,
                stale_statement,
                False,
                stale_questions,
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=5),
        )
        replacement_components = _vision_components(complete=True)
        replacement_statement = "Product teams trust the selected durable Vision."
        replacement_turn = VisionInterviewTurn(
            project_id=project_id,
            operation="revision",
            turn_number=1,
            revision_intent_id=replacement_intent.vision_revision_intent_id,
            vision_evidence_snapshot_id=(
                replacement_snapshot.vision_evidence_snapshot_id
            ),
            prior_turn_id=None,
            user_text="Complete the selected replacement Vision.",
            components_json=canonical_json(replacement_components),
            vision_statement=replacement_statement,
            is_complete=True,
            clarifying_questions_json="[]",
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                replacement_components,
                replacement_statement,
                True,
                [],
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=6),
        )
        session.add(stale_turn)
        session.add(replacement_turn)
        session.flush()
        assert stale_turn.vision_interview_turn_id is not None
        assert replacement_turn.vision_interview_turn_id is not None

        replacement = VisionArtifact(
            project_id=project_id,
            version_number=2,
            components_json=canonical_json(replacement_components),
            statement=replacement_statement,
            content_fingerprint=canonical_hash(
                {
                    "components": replacement_components,
                    "statement": replacement_statement,
                }
            ),
            vision_evidence_snapshot_id=(
                replacement_snapshot.vision_evidence_snapshot_id
            ),
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=vision_id,
            source_interview_turn_id=replacement_turn.vision_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=7),
        )
        session.add(replacement)
        session.flush()
        assert replacement.vision_artifact_id is not None
        replacement_decision = VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=replacement.vision_artifact_id,
            artifact_fingerprint=replacement.content_fingerprint,
            decision="accepted",
            rationale="This is the current accepted Vision.",
            reviewer="vision-reviewer",
            idempotency_key="vision-replacement-accepted",
            decided_at=NOW + timedelta(seconds=8),
        )
        session.add(replacement_decision)
        session.flush()
        assert replacement_decision.vision_artifact_decision_id is not None
        session.commit()
        seeded.update(
            stale_vision_intent_id=stale_intent.vision_revision_intent_id,
            replacement_vision_intent_id=(replacement_intent.vision_revision_intent_id),
            stale_vision_turn_id=stale_turn.vision_interview_turn_id,
            replacement_vision_turn_id=replacement_turn.vision_interview_turn_id,
            current_vision_id=replacement.vision_artifact_id,
            current_vision_decision_id=(
                replacement_decision.vision_artifact_decision_id
            ),
        )
    return seeded


def _remove_superseded_vision_fixture(
    engine: Engine,
    seeded: dict[str, object],
) -> None:
    """Delete the cyclic intent/artifact fixture before SQLite drops tables."""
    with Session(engine) as session:
        for model, key in (
            (VisionArtifactDecision, "current_vision_decision_id"),
            (VisionArtifact, "current_vision_id"),
            (VisionInterviewTurn, "stale_vision_turn_id"),
            (VisionInterviewTurn, "replacement_vision_turn_id"),
            (VisionRevisionIntent, "stale_vision_intent_id"),
            (VisionRevisionIntent, "replacement_vision_intent_id"),
        ):
            row = session.get(model, _seeded_int(seeded, key))
            assert row is not None
            session.delete(row)
            session.flush()
        session.commit()


def _add_detached_goal_revision(
    engine: Engine,
    seeded: dict[str, object],
) -> int:
    """Persist Goal 1 revision 2 without its required revision 1 parent."""
    components = _goal_components(complete=True)
    statement = "Make detached revisions impossible to review."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            goal_number=1,
            revision_number=2,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=9),
        ),
    )
    project_id = _seeded_int(seeded, "project_id")
    vision_id = _seeded_int(seeded, "vision_id")
    vision_fingerprint = seeded["vision_fingerprint"]
    assert isinstance(vision_fingerprint, str)
    with Session(engine) as session:
        detached = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=1,
            revision_number=2,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=10),
        )
        session.add(detached)
        session.commit()
        session.refresh(detached)
        assert detached.product_goal_artifact_id is not None
        return detached.product_goal_artifact_id


def _root_position(engine: Engine, project_id: int) -> WorkflowPosition:
    """Evaluate the root graph from the same durable snapshot as projections."""
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    return ROOT_GRAPH.evaluate(snapshot, NOW)


def test_new_project_has_empty_durable_interview_reads(engine: Engine) -> None:
    """A Project with no interview facts exposes an empty, stable contract."""
    with Session(engine) as session:
        project = Project(name="Empty interview state")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id

    reads = DurableReadProjectionService(engine=engine)

    assert _data(reads.vision_status(project_id=project_id)) == {
        "bootstrap_available": True,
        "current": None,
        "draft": None,
        "transcript": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_NOT_ACCEPTED",
    }
    assert _data(reads.product_goal_status(project_id=project_id)) == {
        "accepted_vision": None,
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": None,
        "stale_reason": "GOAL_NOT_ACTIVE",
    }


def test_incomplete_vision_turn_exposes_exact_transcript_and_questions(
    engine: Engine,
) -> None:
    """The read contract preserves one incomplete Vision turn verbatim."""
    seeded = _seed_interview_project(engine)
    components = _vision_components(complete=False)
    questions = ("What alternatives do product teams use today?",)
    statement = "Product teams need durable workflow review."
    _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=False,
            questions=questions,
            turn_number=1,
            prior_turn_id=None,
            recorded_at=NOW,
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data["bootstrap_available"] is False
    assert data["transcript"] == []
    assert data["draft"] == _vision_display_material(
        components,
        statement,
        questions,
    )
    assert data["candidate"] is None
    assert data["review"] is None


def test_pending_vision_exposes_exact_candidate_and_pending_review(
    engine: Engine,
) -> None:
    """A complete Vision turn projects the immutable candidate it created."""
    seeded = _seed_vision_candidate(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )
    vision_components = _JSON_OBJECT.validate_python(seeded["vision_components"])

    assert data["candidate"] == {
        **_vision_display_material(
            vision_components,
            str(seeded["vision_statement"]),
        ),
        "review_fingerprint": seeded["vision_fingerprint"],
    }
    assert data["review"] == {"state": "pending", "rationale": None}
    assert data["draft"] is None
    assert data["transcript"] == []


def test_vision_feedback_keeps_reviewed_candidate_separate_from_revision_chain(
    engine: Engine,
) -> None:
    """Feedback context remains exact while only new turns are current."""
    seeded = _seed_vision_candidate(engine, decision="feedback")
    components = _vision_components(complete=False)
    questions = ("Which differentiator should the revision emphasize?",)
    revision_statement = "Product teams need a sharper durable workflow Vision."
    _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=revision_statement,
            is_complete=False,
            questions=questions,
            turn_number=2,
            prior_turn_id=_seeded_int(seeded, "vision_turn_id"),
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    candidate = _json_object(data["candidate"])
    assert candidate["review_fingerprint"] == seeded["vision_fingerprint"]
    assert data["review"] == {
        "state": "feedback",
        "rationale": "Vision feedback rationale.",
    }
    assert data["transcript"] == [{"user_text": "Vision answer 2"}]
    assert data["draft"] == _vision_display_material(
        components,
        revision_statement,
        questions,
    )


@pytest.mark.parametrize(
    "review_decision",
    ["feedback", "rejected"],
)
def test_nonaccepted_vision_review_reopens_ordinary_language_clarification(
    engine: Engine,
    review_decision: str,
) -> None:
    """Feedback and rejection return to the human response node, never bootstrap."""
    seeded = _seed_vision_candidate(engine, decision=review_decision)
    project_id = _seeded_int(seeded, "project_id")

    position = _root_position(engine, project_id)
    clarification = next(
        item for item in position.decisions if item.node_id == "vision.interview"
    )

    assert clarification.category is NodeCategory.AVAILABLE
    assert clarification.request_kind == "record_vision_interview_turn"
    assert [item.name for item in clarification.required_inputs] == ["user_text"]
    assert "goal.interview" not in position.available_nodes


def test_accepted_vision_has_current_artifact_and_no_pending_candidate(
    engine: Engine,
) -> None:
    """Acceptance promotes current Vision while preserving terminal review."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data["current"] == {
        "statement": seeded["vision_statement"],
    }
    assert data["candidate"] is None
    assert data["transcript"] == []
    assert data["draft"] is None
    assert data["review"] == {
        "state": "accepted",
        "rationale": "Vision accepted rationale.",
    }
    assert data["stale_reason"] is None


def test_superseded_vision_open_intent_fails_closed_without_transcript(
    engine: Engine,
) -> None:
    """An intent on Vision A cannot remain current after accepted Vision B."""
    seeded = _seed_superseded_vision_with_stale_open_intent(engine)
    project_id = _seeded_int(seeded, "project_id")
    try:
        data = _data(
            DurableReadProjectionService(engine=engine).vision_status(
                project_id=project_id
            )
        )

        assert data == {
            "bootstrap_available": False,
            "current": None,
            "draft": None,
            "transcript": [],
            "candidate": None,
            "review": None,
            "stale_reason": "VISION_FACT_CONFLICT",
        }
    finally:
        _remove_superseded_vision_fixture(engine, seeded)


def test_superseded_vision_open_intent_invalidates_graph_recommendations(
    engine: Engine,
) -> None:
    """The root graph never recommends an interview for a superseded source."""
    seeded = _seed_superseded_vision_with_stale_open_intent(engine)
    project_id = _seeded_int(seeded, "project_id")
    try:
        position = _root_position(engine, project_id)
        decisions = {item.node_id: item for item in position.decisions}

        assert decisions["vision.interview"].category is NodeCategory.INVALID
        assert decisions["vision.interview"].reason_code == "WORKFLOW_FACT_CONFLICT"
        assert decisions["vision.revision.start"].category is NodeCategory.INVALID
        assert "vision.interview" not in position.available_nodes
    finally:
        _remove_superseded_vision_fixture(engine, seeded)


def test_incomplete_goal_exposes_accepted_vision_transcript_and_questions(
    engine: Engine,
) -> None:
    """Goal interview reads include immutable accepted Vision context."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _goal_components(complete=False)
    questions = ("What is outside this Product Goal?",)
    statement = "Make product-definition reviews durable."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=False,
            questions=questions,
            goal_number=1,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["accepted_vision"] == {
        "vision_artifact_id": seeded["vision_id"],
        "fingerprint": seeded["vision_fingerprint"],
        "statement": seeded["vision_statement"],
    }
    assert data["transcript"] == [
        {
            "product_goal_interview_turn_id": turn_id,
            "vision_artifact_id": seeded["vision_id"],
            "vision_fingerprint": seeded["vision_fingerprint"],
            "goal_number": 1,
            "revision_number": 1,
            "prior_turn_id": None,
            "user_text": "Goal answer 1.1",
            "statement": statement,
            "components": components,
            "is_complete": False,
            "clarifying_questions": list(questions),
            "output_fingerprint": product_goal_interview_output_fingerprint(
                components, statement, False, questions
            ),
            "recorded_at": _stored_iso(NOW + timedelta(seconds=3)),
        }
    ]
    assert data["latest_questions"] == list(questions)
    assert data["candidate"] is None
    assert data["review"] is None


def test_pending_goal_exposes_exact_candidate_and_pending_review(
    engine: Engine,
) -> None:
    """A complete Goal turn projects its exact immutable candidate."""
    seeded = _seed_goal_candidate(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["candidate"] == {
        "product_goal_artifact_id": seeded["goal_id"],
        "vision_artifact_id": seeded["vision_id"],
        "vision_fingerprint": seeded["vision_fingerprint"],
        "goal_number": 1,
        "revision_number": 1,
        "fingerprint": seeded["goal_fingerprint"],
        "statement": seeded["goal_statement"],
        "components": seeded["goal_components"],
        "supersedes_product_goal_artifact_id": None,
        "source_interview_turn_id": seeded["goal_turn_id"],
        "created_by": "operator",
        "created_at": _stored_iso(NOW + timedelta(seconds=4)),
    }
    assert data["review"] == {"state": "pending"}
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert len(transcript) == 1
    assert data["latest_questions"] == []


def test_goal_feedback_keeps_candidate_separate_from_revision_chain(
    engine: Engine,
) -> None:
    """A rejected Goal remains review context while revision turns advance."""
    seeded = _seed_goal_candidate(engine, decision="feedback")
    components = _goal_components(complete=False)
    questions = ("Which boundary should the revision add?",)
    revision_turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement="Make durable reviews narrower and measurable.",
            is_complete=False,
            questions=questions,
            goal_number=1,
            revision_number=2,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=6),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    candidate = _json_object(data["candidate"])
    assert candidate["product_goal_artifact_id"] == seeded["goal_id"]
    assert data["review"] == {
        "state": "feedback",
        "product_goal_artifact_decision_id": 1,
        "decision": "feedback",
        "rationale": "Goal feedback rationale.",
        "reviewer": "goal-reviewer",
        "decided_at": _stored_iso(NOW + timedelta(seconds=5)),
    }
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert [
        _json_object(item)["product_goal_interview_turn_id"] for item in transcript
    ] == [revision_turn_id]
    assert data["latest_questions"] == list(questions)


def test_resolved_goal_followed_by_new_interview_excludes_old_candidate(
    engine: Engine,
) -> None:
    """The next Goal transcript does not revive the resolved Goal candidate."""
    seeded = _seed_goal_candidate(engine, decision="accepted")
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    with Session(engine) as session:
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=goal_id,
                artifact_fingerprint=goal_fingerprint,
                outcome="fulfilled",
                rationale="The first durable review Goal was fulfilled.",
                decided_by="goal-owner",
                idempotency_key="goal-one-fulfilled",
                decided_at=NOW + timedelta(seconds=6),
            )
        )
        session.commit()
    components = _goal_components(complete=False)
    questions = ("What should the next measurable outcome be?",)
    new_turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement="Define the next durable product outcome.",
            is_complete=False,
            questions=questions,
            goal_number=2,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=7),
        ),
    )

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["active"] is None
    assert data["candidate"] is None
    assert data["review"] is None
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert [
        _json_object(item)["product_goal_interview_turn_id"] for item in transcript
    ] == [new_turn_id]
    outcome = _json_object(data["outcome"])
    assert outcome["product_goal_artifact_id"] == goal_id
    assert data["stale_reason"] == "GOAL_RESOLVED"


@pytest.mark.parametrize("prior_state", ["feedback", "resolved"])
def test_detached_goal_revision_fails_closed_in_projection(
    engine: Engine,
    prior_state: str,
) -> None:
    """Revision 2 without its exact revision 1 parent is never current."""
    seeded = _seed_goal_candidate(
        engine,
        decision="feedback" if prior_state == "feedback" else "accepted",
    )
    if prior_state == "resolved":
        _resolve_goal(engine, seeded)
    _add_detached_goal_revision(engine, seeded)
    project_id = _seeded_int(seeded, "project_id")

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data == {
        "accepted_vision": {
            "vision_artifact_id": seeded["vision_id"],
            "fingerprint": seeded["vision_fingerprint"],
            "statement": seeded["vision_statement"],
        },
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": None,
        "stale_reason": "PRODUCT_GOAL_FACT_CONFLICT",
    }


@pytest.mark.parametrize("prior_state", ["feedback", "resolved"])
def test_detached_goal_revision_invalidates_graph_review(
    engine: Engine,
    prior_state: str,
) -> None:
    """Detached Goal revisions cannot become graph-current review candidates."""
    seeded = _seed_goal_candidate(
        engine,
        decision="feedback" if prior_state == "feedback" else "accepted",
    )
    if prior_state == "resolved":
        _resolve_goal(engine, seeded)
    detached_id = _add_detached_goal_revision(engine, seeded)
    project_id = _seeded_int(seeded, "project_id")
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    interview = _goal_interview_rule(snapshot, NOW)[0]
    review = _goal_review_rule(snapshot, NOW)[0]

    assert interview.category is RuleCategory.INVALID
    assert interview.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert review.category is RuleCategory.INVALID
    assert all(
        reference.fact_id != str(detached_id) for reference in review.fact_references
    )


def test_ambiguous_vision_leaf_fails_closed_with_typed_stale_reason(
    engine: Engine,
) -> None:
    """Two immutable Vision leaves never degrade to latest-row selection."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _vision_components(complete=True)
    statement = "A conflicting durable Vision leaf."
    turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            turn_number=2,
            prior_turn_id=_seeded_int(seeded, "vision_turn_id"),
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    snapshot_id = seeded["vision_evidence_snapshot_id"]
    assert isinstance(project_id, int)
    assert isinstance(snapshot_id, int)
    with Session(engine) as session:
        session.add(
            VisionArtifact(
                project_id=project_id,
                version_number=2,
                components_json=canonical_json(components),
                statement=statement,
                content_fingerprint=canonical_hash(
                    {"components": components, "statement": statement}
                ),
                vision_evidence_snapshot_id=snapshot_id,
                component_basis_json="[]",
                assumptions_json="[]",
                conflicts_json="[]",
                supersedes_vision_artifact_id=None,
                source_interview_turn_id=turn_id,
                created_by="operator",
                created_at=NOW + timedelta(seconds=4),
            )
        )
        session.commit()

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data == {
        "bootstrap_available": False,
        "current": None,
        "draft": None,
        "transcript": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_FACT_CONFLICT",
    }


def _resolve_goal(engine: Engine, seeded: dict[str, object]) -> None:
    """Record the exact terminal outcome for the fixture's accepted first Goal."""
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    with Session(engine) as session:
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=goal_id,
                artifact_fingerprint=goal_fingerprint,
                outcome="fulfilled",
                rationale="Observable success signals were reached.",
                decided_by="operator",
                idempotency_key="goal-fulfilled",
                decided_at=NOW + timedelta(seconds=8),
            )
        )
        session.commit()


def _accept_next_goal(engine: Engine, seeded: dict[str, object]) -> None:
    """Add the next accepted Goal under the unchanged accepted Vision."""
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    components = {
        "valuable_future_state": "Transparent decisions",
        "beneficiary": "Operators",
        "value": "Trust",
        "success_signals": ["Auditable outcomes"],
        "boundaries": ["No implementation"],
    }
    statement = "Goal 2: transparent decisions."
    with Session(engine) as session:
        turn = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=2,
            revision_number=1,
            prior_turn_id=None,
            user_text="Define the next goal",
            components_json=canonical_json(components),
            goal_statement=statement,
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=product_goal_interview_output_fingerprint(
                components, statement, True, ()
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=9),
        )
        session.add(turn)
        session.flush()
        assert turn.product_goal_interview_turn_id is not None
        goal = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=2,
            revision_number=1,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn.product_goal_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=10),
        )
        session.add(goal)
        session.flush()
        assert goal.product_goal_artifact_id is not None
        session.add(
            ProductGoalArtifactDecision(
                project_id=project_id,
                product_goal_artifact_id=goal.product_goal_artifact_id,
                artifact_fingerprint=goal.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key="goal-2-accepted",
                decided_at=NOW + timedelta(seconds=11),
            )
        )
        session.commit()


def test_durable_projections_expose_current_human_content_and_pending_review(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Pending status and review expose one complete exact v2 candidate packet."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    goal_statement = seeded["goal_statement"]
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    assert isinstance(goal_statement, str)

    reads = DurableReadProjectionService(engine=engine)
    vision_data = _data(reads.vision_status(project_id=project_id))
    goal_data = _data(reads.product_goal_status(project_id=project_id))
    specification_data = _data(reads.specification_status(project_id=project_id))
    review_data = _data(reads.specification_review(project_id=project_id))

    assert vision_data["current"] == {
        "statement": "A durable Vision.",
    }
    active_goal = _json_object(goal_data["active"])
    assert active_goal["product_goal_artifact_id"] == goal_id
    assert active_goal["statement"] == goal_statement
    candidate_data = _json_object(specification_data["candidate"])
    _assert_complete_candidate_projection(
        candidate_data,
        seeded,
        decision_state="pending",
    )
    assert specification_data["schema_version"] == (
        "agileforge.specification_review.v2"
    )
    source_data = _json_object(specification_data["source"])
    assert source_data["specification_source_id"] == seeded["source_id"]
    assert source_data["source_fingerprint"] == seeded["source_fingerprint"]
    assert source_data["producer_capability"] == "to-spec"
    assert source_data["preparation_capability"] == "grill-with-docs"
    source_document = _json_object(source_data["source"])
    assert source_document == {
        "source_id": SPECIFICATION_SOURCE_PRIMARY_ID,
        "relative_path": "SPECIFICATION.md",
        "byte_length": len(b"REGISTERED_TO_SPEC_SOURCE_MUST_NOT_LEAK\n"),
        "content_fingerprint": seeded["source_document_fingerprint"],
    }
    context = _json_object(source_data["context"])
    assert context["state"] == "present"
    context_document = _json_object(context["document"])
    assert (
        context_document["content_fingerprint"]
        == seeded["context_document_fingerprint"]
    )
    adrs = source_data["adrs"]
    assert isinstance(adrs, list)
    assert (
        _json_object(adrs[0])["content_fingerprint"]
        == seeded["adr_document_fingerprint"]
    )
    repository = _json_object(source_data["repository"])
    assert repository["repository_binding_id"] == seeded["repository_binding_id"]
    assert repository["status_fingerprint"] == seeded["repository_status_fingerprint"]
    serialized_source = json.dumps(source_data)
    assert "content_base64" not in serialized_source
    assert "REGISTERED_TO_SPEC_SOURCE_MUST_NOT_LEAK" not in serialized_source
    assert "REGISTERED_CONTEXT_MUST_NOT_LEAK" not in serialized_source
    assert "REGISTERED_ADR_MUST_NOT_LEAK" not in serialized_source
    assert specification_data["current"] is None
    assert review_data["review"] == {"state": "pending"}
    assert review_data["candidate"] == candidate_data
    assert review_data["source"] == source_data
    assert review_data["schema_version"] == "agileforge.specification_review.v2"


def test_registered_source_status_precedes_structured_candidate(
    engine: Engine,
) -> None:
    """A registered source is visible as digest metadata before structuring."""
    seeded = _seed_lineage(engine)
    project_id = _seeded_int(seeded, "project_id")
    candidate_id = _seeded_int(seeded, "candidate_id")
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, candidate_id)
        assert candidate is not None
        session.delete(candidate)
        session.commit()

    status = _data(
        DurableReadProjectionService(engine=engine).specification_status(
            project_id=project_id
        )
    )

    source = _json_object(status["source"])
    assert source["source_fingerprint"] == seeded["source_fingerprint"]
    assert status["candidate"] is None
    assert status["review"] is None
    assert status["stale_reason"] == "SPECIFICATION_NOT_STRUCTURED"
    assert "content_base64" not in json.dumps(source)


def test_successor_source_retains_current_accepted_spec_before_amendment(
    engine: Engine,
) -> None:
    """Registering amendment bytes does not hide the still-current accepted base."""
    seeded = _seed_lineage(engine)
    project_id = _seeded_int(seeded, "project_id")
    candidate_id = _seeded_int(seeded, "candidate_id")
    source_id = _seeded_int(seeded, "source_id")
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, candidate_id)
        source = session.get(SpecificationSource, source_id)
        assert candidate is not None
        assert source is not None
        payload_fingerprint = candidate.payload_fingerprint
        session.add(
            SpecificationDecision(
                project_id=project_id,
                specification_candidate_id=candidate_id,
                candidate_fingerprint=candidate.candidate_fingerprint,
                decision="accepted",
                rationale="Approved base.",
                reviewer="operator",
                idempotency_key="accepted-base-before-successor-source",
                decided_at=NOW + timedelta(seconds=8),
            )
        )
        registry = SpecRegistry(
            project_id=project_id,
            spec_hash=candidate.payload_fingerprint,
            status="approved",
            approved_at=NOW + timedelta(seconds=8),
            approved_by="operator",
            source_specification_candidate_id=candidate_id,
            source_specification_candidate_fingerprint=(
                candidate.candidate_fingerprint
            ),
            source_vision_artifact_id=candidate.vision_artifact_id,
            source_vision_fingerprint=candidate.vision_fingerprint,
            source_product_goal_artifact_id=candidate.product_goal_artifact_id,
            source_product_goal_fingerprint=candidate.product_goal_fingerprint,
        )
        session.add(registry)
        successor = SpecificationSource(
            project_id=project_id,
            source_bundle_json=source.source_bundle_json,
            source_fingerprint=source.source_fingerprint,
            repository_binding_id=source.repository_binding_id,
            repository_head_sha=source.repository_head_sha,
            repository_dirty=source.repository_dirty,
            repository_status_fingerprint=source.repository_status_fingerprint,
            vision_artifact_id=source.vision_artifact_id,
            vision_fingerprint=source.vision_fingerprint,
            product_goal_artifact_id=source.product_goal_artifact_id,
            product_goal_fingerprint=source.product_goal_fingerprint,
            supersedes_specification_source_id=source_id,
            supersedes_source_fingerprint=source.source_fingerprint,
            registered_by="operator",
            registered_at=NOW + timedelta(seconds=9),
        )
        session.add(successor)
        session.commit()
        session.refresh(registry)
        session.refresh(successor)
        registry_id = registry.spec_version_id
        successor_id = successor.specification_source_id

    status = _data(
        DurableReadProjectionService(engine=engine).specification_status(
            project_id=project_id
        )
    )

    source_data = _json_object(status["source"])
    current = _json_object(status["current"])
    assert source_data["specification_source_id"] == successor_id
    assert current["spec_version_id"] == registry_id
    assert current["spec_hash"] == payload_fingerprint
    accepted_candidate = _json_object(current["candidate"])
    assert accepted_candidate["specification_candidate_id"] == candidate_id
    assert status["candidate"] is None
    assert status["review"] is None
    assert status["stale_reason"] == "SPECIFICATION_NOT_STRUCTURED"


def test_pending_amendment_review_projects_the_exact_amendment_candidate(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """An approved base cannot replace its pending amendment review packet."""
    seeded = _seed_lineage(engine)
    project_id = _seeded_int(seeded, "project_id")
    initial_candidate_id = _seeded_int(seeded, "candidate_id")
    source_id = _seeded_int(seeded, "source_id")
    base_payload = SpecificationPayload.model_validate(seeded["specification_payload"])
    amendment_payload = base_payload.model_copy(
        update={"summary": "Project the exact pending amendment for review."}
    )

    with Session(engine) as session:
        initial_candidate = session.get(
            SpecificationCandidate,
            initial_candidate_id,
        )
        original_source = session.get(SpecificationSource, source_id)
        assert initial_candidate is not None
        assert original_source is not None
        session.add(
            SpecificationDecision(
                project_id=project_id,
                specification_candidate_id=initial_candidate_id,
                candidate_fingerprint=initial_candidate.candidate_fingerprint,
                decision="accepted",
                rationale="Approved base.",
                reviewer="operator",
                idempotency_key="approve-base-before-amendment",
                decided_at=NOW + timedelta(seconds=8),
            )
        )
        base_registry = SpecRegistry(
            project_id=project_id,
            spec_hash=initial_candidate.payload_fingerprint,
            status="approved",
            approved_at=NOW + timedelta(seconds=8),
            approved_by="operator",
            source_specification_candidate_id=initial_candidate_id,
            source_specification_candidate_fingerprint=(
                initial_candidate.candidate_fingerprint
            ),
            source_vision_artifact_id=initial_candidate.vision_artifact_id,
            source_vision_fingerprint=initial_candidate.vision_fingerprint,
            source_product_goal_artifact_id=(
                initial_candidate.product_goal_artifact_id
            ),
            source_product_goal_fingerprint=(
                initial_candidate.product_goal_fingerprint
            ),
        )
        session.add(base_registry)
        session.flush()
        assert base_registry.spec_version_id is not None
        base_spec_version_id = base_registry.spec_version_id
        base_spec_hash = base_registry.spec_hash

        original_bundle = SpecificationSourceBundle.model_validate_json(
            original_source.source_bundle_json
        )
        amendment_document = _registered_document(
            source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
            relative_path="SPECIFICATION.md",
            content=b"EXACT_PENDING_AMENDMENT_SOURCE\n",
        )
        amendment_bundle = SpecificationSourceBundle(
            source=amendment_document,
            context=original_bundle.context,
            adrs=original_bundle.adrs,
            repository_revision=original_bundle.repository_revision,
            accepted_vision_fingerprint=(original_bundle.accepted_vision_fingerprint),
            accepted_product_goal_fingerprint=(
                original_bundle.accepted_product_goal_fingerprint
            ),
        )
        amendment_source = SpecificationSource(
            project_id=project_id,
            source_bundle_json=canonical_json(amendment_bundle.model_dump(mode="json")),
            source_fingerprint=source_bundle_fingerprint(amendment_bundle),
            repository_binding_id=original_source.repository_binding_id,
            repository_head_sha=original_source.repository_head_sha,
            repository_dirty=original_source.repository_dirty,
            repository_status_fingerprint=(
                original_source.repository_status_fingerprint
            ),
            vision_artifact_id=original_source.vision_artifact_id,
            vision_fingerprint=original_source.vision_fingerprint,
            product_goal_artifact_id=original_source.product_goal_artifact_id,
            product_goal_fingerprint=original_source.product_goal_fingerprint,
            supersedes_specification_source_id=source_id,
            supersedes_source_fingerprint=original_source.source_fingerprint,
            registered_by="operator",
            registered_at=NOW + timedelta(seconds=9),
        )
        session.add(amendment_source)
        session.flush()
        assert amendment_source.specification_source_id is not None

        initial_attempt = session.get(
            WorkflowNodeAttempt,
            initial_candidate.workflow_node_attempt_id,
        )
        assert initial_attempt is not None
        initial_structuring_input = SpecificationStructuringInput.model_validate_json(
            initial_attempt.normalized_input_json
        )
        stored_manifest = seeded["source_manifest"]
        assert isinstance(stored_manifest, list)
        original_manifest = tuple(
            CandidateSourceManifestEntry.model_validate(item)
            for item in stored_manifest
        )
        amendment_manifest = tuple(
            CandidateSourceManifestEntry(
                source_id=item.source_id,
                kind=item.kind,
                fingerprint=(
                    amendment_document.content_fingerprint
                    if item.source_id == SPECIFICATION_SOURCE_PRIMARY_ID
                    else item.fingerprint
                ),
                warnings=item.warnings,
            )
            for item in original_manifest
        )
        amendment_structuring_input = SpecificationStructuringInput(
            project_id=project_id,
            project_name=initial_structuring_input.project_name,
            operation="amendment",
            accepted_vision=initial_structuring_input.accepted_vision,
            accepted_product_goal=(initial_structuring_input.accepted_product_goal),
            registered_source=RegisteredSpecificationSource(
                specification_source_id=(amendment_source.specification_source_id),
                source_fingerprint=amendment_source.source_fingerprint,
                producer_capability=amendment_bundle.producer_capability,
                preparation_capability=amendment_bundle.preparation_capability,
                source=_structuring_document(amendment_document),
                context=initial_structuring_input.registered_source.context,
                adrs=initial_structuring_input.registered_source.adrs,
                repository_revision=amendment_bundle.repository_revision,
                repository_evidence=(
                    initial_structuring_input.registered_source.repository_evidence
                ),
                accepted_vision_fingerprint=(
                    amendment_bundle.accepted_vision_fingerprint
                ),
                accepted_product_goal_fingerprint=(
                    amendment_bundle.accepted_product_goal_fingerprint
                ),
            ),
            source_manifest=amendment_manifest,
            base_specification=BaseSpecificationContext(
                spec_version_id=base_registry.spec_version_id,
                payload_fingerprint=base_registry.spec_hash,
                payload=base_payload,
            ),
        )
        normalized_input = amendment_structuring_input.model_dump(mode="json")
        amendment_attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id="specification.structure",
            instance_key=str(base_registry.spec_version_id),
            graph_version=GRAPH_VERSION,
            fact_fingerprint=canonical_hash({"amendment": "facts"}),
            business_fact_fingerprint=canonical_hash({"amendment": "business"}),
            decision_fingerprint=canonical_hash({"amendment": "decision"}),
            normalized_input_json=canonical_json(normalized_input),
            input_fingerprint=canonical_hash(normalized_input),
            model_id="fake/product-definition",
            execution_settings_json="{}",
            idempotency_key="pending-amendment-attempt",
            actor="operator",
            correlation_id="pending-amendment",
            started_at=NOW + timedelta(seconds=10),
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=canonical_hash({"amendment": "pending"}),
        )
        session.add(amendment_attempt)
        session.flush()
        assert amendment_attempt.workflow_node_attempt_id is not None
        amendment_attempt.attempt_fingerprint = workflow_node_attempt_fingerprint(
            {
                "attempt_id": amendment_attempt.workflow_node_attempt_id,
                "project_id": amendment_attempt.project_id,
                "node_id": amendment_attempt.node_id,
                "instance_key": amendment_attempt.instance_key,
                "graph_version": amendment_attempt.graph_version,
                "fact_fingerprint": amendment_attempt.fact_fingerprint,
                "business_fact_fingerprint": (
                    amendment_attempt.business_fact_fingerprint
                ),
                "decision_fingerprint": amendment_attempt.decision_fingerprint,
                "normalized_input": normalized_input,
                "input_fingerprint": amendment_attempt.input_fingerprint,
                "model_id": amendment_attempt.model_id,
                "execution_settings": {},
                "idempotency_key": amendment_attempt.idempotency_key,
                "actor": amendment_attempt.actor,
                "correlation_id": amendment_attempt.correlation_id,
                "started_at": amendment_attempt.started_at,
                "lease_expires_at": amendment_attempt.lease_expires_at,
            }
        )
        session.add(amendment_attempt)
        session.flush()
        amendment_envelope = build_candidate_envelope(
            payload=amendment_payload,
            metadata=CandidateBuildInput(
                candidate_kind=CandidateKind.AMENDMENT,
                accepted_vision_id=initial_candidate.vision_artifact_id,
                accepted_vision_fingerprint=initial_candidate.vision_fingerprint,
                accepted_product_goal_id=(initial_candidate.product_goal_artifact_id),
                accepted_product_goal_fingerprint=(
                    initial_candidate.product_goal_fingerprint
                ),
                registered_source_fingerprint=(amendment_source.source_fingerprint),
                source_producer_capability="to-spec",
                source_preparation_capability="grill-with-docs",
                source_manifest=amendment_manifest,
                accepted_fact_fingerprint=(
                    specification_structuring_fact_fingerprint(
                        amendment_structuring_input
                    )
                ),
                producer_input_fingerprint=(
                    specification_structuring_input_fingerprint(
                        amendment_structuring_input
                    )
                ),
                producer_capability="specification-structurer",
                producer_version="1.0.0",
                model_id=amendment_attempt.model_id,
                model_configuration_fingerprint=canonical_hash(
                    {"model": amendment_attempt.model_id}
                ),
                prompt_version=SPECIFICATION_STRUCTURER_PROMPT_VERSION,
                prompt_fingerprint=canonical_hash({"prompt": "specification-v2"}),
                workflow_node_attempt_id=(amendment_attempt.workflow_node_attempt_id),
                attempt_fingerprint=amendment_attempt.attempt_fingerprint,
                correlation_id="pending-amendment",
                produced_at=NOW + timedelta(seconds=11),
                base_payload=base_payload,
                base_specification_id=base_registry.spec_version_id,
                base_payload_fingerprint=base_registry.spec_hash,
            ),
        )
        amendment_candidate = SpecificationCandidate(
            project_id=project_id,
            candidate_kind="amendment",
            specification_source_id=amendment_source.specification_source_id,
            specification_source_fingerprint=amendment_source.source_fingerprint,
            vision_artifact_id=initial_candidate.vision_artifact_id,
            vision_fingerprint=initial_candidate.vision_fingerprint,
            product_goal_artifact_id=initial_candidate.product_goal_artifact_id,
            product_goal_fingerprint=initial_candidate.product_goal_fingerprint,
            base_spec_version_id=base_registry.spec_version_id,
            base_spec_hash=base_registry.spec_hash,
            canonical_envelope_json=canonical_candidate_json(
                amendment_payload,
                amendment_envelope,
            ),
            payload_fingerprint=amendment_envelope.payload_fingerprint,
            source_manifest_fingerprint=(
                amendment_envelope.source_manifest_fingerprint
            ),
            producer_input_fingerprint=(amendment_envelope.producer_input_fingerprint),
            rendered_view_fingerprint=(amendment_envelope.review_view_fingerprint),
            candidate_fingerprint=amendment_envelope.candidate_fingerprint,
            workflow_node_attempt_id=amendment_attempt.workflow_node_attempt_id,
            attempt_fingerprint=amendment_attempt.attempt_fingerprint,
            supersedes_specification_candidate_id=initial_candidate_id,
            supersedes_candidate_fingerprint=(initial_candidate.candidate_fingerprint),
            recorded_by="operator",
            recorded_at=NOW + timedelta(seconds=11),
        )
        session.add(amendment_candidate)
        session.flush()
        assert amendment_candidate.specification_candidate_id is not None
        amendment_candidate_id = amendment_candidate.specification_candidate_id
        session.commit()

    review = _data(
        DurableReadProjectionService(engine=engine).specification_review(
            project_id=project_id
        )
    )
    status = _data(
        DurableReadProjectionService(engine=engine).specification_status(
            project_id=project_id
        )
    )

    candidate = _json_object(review["candidate"])
    assert status["candidate"] == review["candidate"]
    assert status["review"] == {"state": "pending"}
    assert status["current"] is None
    assert review["review"] == {"state": "pending"}
    assert candidate["specification_candidate_id"] == amendment_candidate_id
    assert candidate["candidate_kind"] == "amendment"
    assert candidate["canonical_payload"] == amendment_payload.model_dump(mode="json")
    assert candidate["rendered_markdown"] == render_candidate_review_markdown(
        amendment_payload,
        amendment_envelope,
    )
    assert candidate["base_spec_version_id"] == base_spec_version_id
    assert candidate["base_spec_hash"] == base_spec_hash
    assert candidate["decision_state"] == "pending"
    amendment_diff = _json_object(candidate["amendment_diff"])
    assert amendment_diff["changed_fields"] == ["summary"]


@pytest.mark.parametrize(
    ("decision", "expect_registry"),
    [("feedback", False), ("rejected", False), ("accepted", True)],
)
def test_specification_projections_expose_terminal_review_and_registry_content(
    engine: Engine,
    decision: str,
    expect_registry: bool,
) -> None:
    """Terminal reads resolve exact candidate bytes through the registry row."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    candidate_id = seeded["candidate_id"]
    candidate_fingerprint = seeded["candidate_fingerprint"]
    payload_fingerprint = seeded["payload_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(candidate_id, int)
    assert isinstance(candidate_fingerprint, str)
    assert isinstance(payload_fingerprint, str)
    with Session(engine) as session:
        session.add(
            SpecificationDecision(
                project_id=project_id,
                specification_candidate_id=candidate_id,
                candidate_fingerprint=candidate_fingerprint,
                decision=decision,
                rationale="Ready.",
                reviewer="operator",
                idempotency_key="spec-accepted",
                decided_at=NOW + timedelta(seconds=8),
            )
        )
        registry: SpecRegistry | None = None
        if expect_registry:
            registry = SpecRegistry(
                project_id=project_id,
                spec_hash=payload_fingerprint,
                status="approved",
                source_specification_candidate_id=candidate_id,
                source_specification_candidate_fingerprint=candidate_fingerprint,
                source_vision_artifact_id=seeded["vision_id"],
                source_vision_fingerprint=seeded["vision_fingerprint"],
                source_product_goal_artifact_id=seeded["goal_id"],
                source_product_goal_fingerprint=seeded["goal_fingerprint"],
            )
            session.add(registry)
        session.commit()
        if registry is not None:
            session.refresh(registry)

    reads = DurableReadProjectionService(engine=engine)
    status = _data(reads.specification_status(project_id=project_id))
    review = _data(reads.specification_review(project_id=project_id))
    authority_review = _data(
        reads.authority_review(project_id=project_id, include_spec="summary")
    )

    current = status["current"]
    if registry is None:
        assert current is None
        assert status["stale_reason"] == "SPECIFICATION_NOT_APPROVED"
    else:
        current = _json_object(current)
        assert current["spec_version_id"] == registry.spec_version_id
        assert current["spec_hash"] == payload_fingerprint
        assert current["source_specification_candidate_id"] == candidate_id
        assert (
            current["source_specification_candidate_fingerprint"]
            == candidate_fingerprint
        )
    candidate_projection = _json_object(review["candidate"])
    _assert_complete_candidate_projection(
        candidate_projection,
        seeded,
        decision_state=decision,
    )
    assert review["review"] == {
        "state": decision,
        "specification_decision_id": 1,
        "decision": decision,
        "rationale": "Ready.",
        "reviewer": "operator",
    }
    specifications = authority_review["specifications"]
    assert isinstance(specifications, list)
    if registry is None:
        assert specifications == []
    else:
        assert len(specifications) == 1
        authority_spec = _json_object(specifications[0])
        assert authority_spec["spec_hash"] == payload_fingerprint
        _assert_complete_candidate_projection(
            _json_object(authority_spec["candidate"]),
            seeded,
            decision_state="accepted",
        )
        assert "content" not in authority_spec
        assert "content_ref" not in authority_spec


def test_obsolete_product_discovery_selection_service_is_removed() -> None:
    """The deleted Discovery gate has no standalone selection service."""
    assert importlib.util.find_spec("services.product_discovery_selection") is None


def test_resolved_goal_and_next_goal_leave_old_product_definition_non_current(
    engine: Engine,
) -> None:
    """A later Goal keeps the old pending candidate as an exact review target."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    _resolve_goal(engine, seeded)

    reads = DurableReadProjectionService(engine=engine)
    resolved = _data(reads.product_goal_status(project_id=project_id))
    assert resolved == {
        "accepted_vision": {
            "vision_artifact_id": seeded["vision_id"],
            "fingerprint": seeded["vision_fingerprint"],
            "statement": "A durable Vision.",
        },
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": {
            "product_goal_artifact_id": goal_id,
            "fingerprint": goal_fingerprint,
            "statement": "Goal 1: reliable decisions.",
            "goal_number": 1,
            "revision_number": 1,
            "outcome": "fulfilled",
            "rationale": "Observable success signals were reached.",
            "decided_by": "operator",
        },
        "stale_reason": "GOAL_RESOLVED",
    }

    _accept_next_goal(engine, seeded)

    vision = _data(reads.vision_status(project_id=project_id))
    active = _data(reads.product_goal_status(project_id=project_id))
    specification = _data(reads.specification_status(project_id=project_id))
    review = _data(reads.specification_review(project_id=project_id))
    assert vision["current"] is not None
    active_goal = _json_object(active["active"])
    assert active_goal["statement"] == "Goal 2: transparent decisions."
    assert active_goal["product_goal_artifact_id"] != goal_id
    assert specification["schema_version"] == "agileforge.specification_review.v2"
    assert specification["source"] is None
    assert specification["current"] is None
    assert specification["review"] == {"state": "pending"}
    assert specification["stale_reason"] == "SPECIFICATION_NOT_APPROVED"
    candidate = _json_object(specification["candidate"])
    _assert_complete_candidate_projection(
        candidate,
        seeded,
        decision_state="pending",
    )
    assert review == {
        "schema_version": "agileforge.specification_review.v2",
        "source": None,
        "candidate": candidate,
        "review": {"state": "pending"},
        "stale_reason": None,
    }


def test_malformed_durable_projection_data_returns_typed_error(engine: Engine) -> None:
    """Loader validation failures are reported as typed reads instead of crashes."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    candidate_id = seeded["candidate_id"]
    assert isinstance(project_id, int)
    assert isinstance(candidate_id, int)
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, candidate_id)
        assert candidate is not None
        candidate.canonical_envelope_json = "not-json"
        session.add(candidate)
        session.commit()

    result = DurableReadProjectionService(engine=engine).specification_status(
        project_id=project_id
    )

    assert _error_code(result) == "PROJECT_FACTS_UNAVAILABLE"
