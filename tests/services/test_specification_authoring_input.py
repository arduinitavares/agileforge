"""Host preparation for exact registered Specification structuring."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from git import Repo
from sqlmodel import Session

from adapters.git.repository_probe import GitPythonRepositoryProbe
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
from models.repository import RepositoryBinding
from models.workflow import WorkflowNodeAttempt
from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_STRUCTURER_PROMPT_VERSION,
    SPECIFICATION_VISION_SOURCE_ID,
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
from services.repository_probe import RepositoryProbeError, RepositoryProbeErrorCode
from services.specification_authoring_input import SpecificationStructuringInputService
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    build_candidate_envelope,
    canonical_candidate_json,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.contracts import (
    GRAPH_VERSION,
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowErrorCode,
)
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
    vision_interview_output_fingerprint,
    workflow_node_attempt_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbeResult

ADR_PATH = "docs/adr/0001-exact-source.md"
REMOTE_CREDENTIAL_SENTINEL = "STRUCTURING_INPUT_CREDENTIAL_SENTINEL"


def _required(value: int | None, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise AssertionError(message)
    return value


def _attempt(
    session: Session,
    *,
    project_id: int,
    node_id: str,
    ordinal: int,
    at: datetime,
) -> WorkflowNodeAttempt:
    normalized_input: dict[str, object] = {}
    input_fingerprint = canonical_hash(normalized_input)
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id=node_id,
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=canonical_hash({"facts": ordinal}),
        business_fact_fingerprint=canonical_hash({"business": ordinal}),
        decision_fingerprint=canonical_hash({"decision": ordinal}),
        normalized_input_json=canonical_json(normalized_input),
        input_fingerprint=input_fingerprint,
        model_id="fake/model",
        execution_settings_json="{}",
        idempotency_key=f"fixture-{node_id}-{ordinal}",
        actor="fixture",
        correlation_id=None,
        started_at=at,
        lease_expires_at=at + timedelta(minutes=1),
        attempt_fingerprint=canonical_hash({"pending": ordinal}),
    )
    session.add(attempt)
    session.flush()
    attempt_id = _required(attempt.workflow_node_attempt_id, "attempt")
    attempt.attempt_fingerprint = workflow_node_attempt_fingerprint(
        {
            "attempt_id": attempt_id,
            "project_id": project_id,
            "node_id": node_id,
            "instance_key": None,
            "graph_version": GRAPH_VERSION,
            "fact_fingerprint": attempt.fact_fingerprint,
            "business_fact_fingerprint": attempt.business_fact_fingerprint,
            "decision_fingerprint": attempt.decision_fingerprint,
            "normalized_input": normalized_input,
            "input_fingerprint": input_fingerprint,
            "model_id": attempt.model_id,
            "execution_settings": {},
            "idempotency_key": attempt.idempotency_key,
            "actor": attempt.actor,
            "correlation_id": None,
            "started_at": at,
            "lease_expires_at": at + timedelta(minutes=1),
        }
    )
    session.add(attempt)
    session.flush()
    return attempt


def _seed_accepted_lineage(
    session: Session,
    *,
    project_id: int,
    at: datetime,
) -> tuple[VisionArtifact, ProductGoalArtifact]:
    vision_attempt = _attempt(
        session,
        project_id=project_id,
        node_id="vision.bootstrap",
        ordinal=1,
        at=at,
    )
    evidence = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [
            {
                "evidence_id": "project:metadata",
                "kind": "project_metadata",
                "relative_path": None,
                "content_fingerprint": canonical_hash({"name": "fixture"}),
                "trust": "operator_provided",
                "content": {"name": "fixture"},
                "truncated": False,
            }
        ],
        "warnings": [],
    }
    evidence["evidence_fingerprint"] = canonical_hash(evidence)
    snapshot = VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=None,
        workflow_node_attempt_id=_required(
            vision_attempt.workflow_node_attempt_id,
            "vision attempt",
        ),
        evidence_json=canonical_json(evidence),
        evidence_fingerprint=str(evidence["evidence_fingerprint"]),
        warnings_json="[]",
        created_at=at,
    )
    session.add(snapshot)
    session.flush()
    components = {"purpose": "structure exact registered source"}
    statement = "Structure one exact registered Specification source."
    vision_turn = VisionInterviewTurn(
        project_id=project_id,
        operation="bootstrap",
        turn_number=1,
        revision_intent_id=None,
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "evidence",
        ),
        prior_turn_id=None,
        user_text=None,
        components_json=canonical_json(components),
        vision_statement=statement,
        is_complete=True,
        clarifying_questions_json="[]",
        component_basis_json="[]",
        assumptions_json="[]",
        conflicts_json="[]",
        output_fingerprint=vision_interview_output_fingerprint(
            components,
            statement,
            True,
            (),
            {"component_basis": (), "assumptions": (), "conflicts": ()},
        ),
        workflow_node_attempt_id=_required(
            vision_attempt.workflow_node_attempt_id,
            "vision attempt",
        ),
        attempt_fingerprint=vision_attempt.attempt_fingerprint,
        recorded_at=at + timedelta(seconds=1),
    )
    session.add(vision_turn)
    session.flush()
    vision = VisionArtifact(
        project_id=project_id,
        version_number=1,
        components_json=canonical_json(components),
        statement=statement,
        content_fingerprint=canonical_hash(
            {"components": components, "statement": statement}
        ),
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "evidence",
        ),
        component_basis_json="[]",
        assumptions_json="[]",
        conflicts_json="[]",
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=_required(
            vision_turn.vision_interview_turn_id,
            "vision turn",
        ),
        created_by="fixture",
        created_at=at + timedelta(seconds=2),
    )
    session.add(vision)
    session.flush()
    vision_id = _required(vision.vision_artifact_id, "vision")
    session.add(
        VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=vision_id,
            artifact_fingerprint=vision.content_fingerprint,
            decision="accepted",
            rationale="Accepted.",
            reviewer="fixture",
            idempotency_key=f"vision-{project_id}",
            decided_at=at + timedelta(seconds=3),
        )
    )
    goal_attempt = _attempt(
        session,
        project_id=project_id,
        node_id="goal.interview",
        ordinal=2,
        at=at + timedelta(seconds=4),
    )
    goal_components = {
        "valuable_future_state": "Exact source is structured",
        "beneficiary": "Operators",
        "value": "Auditable provenance",
        "success_signals": ["The candidate references exact bytes"],
        "boundaries": ["No opportunistic repository evidence"],
    }
    goal_statement = "Create one source-grounded typed Specification."
    goal_turn = ProductGoalInterviewTurn(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=1,
        revision_number=1,
        prior_turn_id=None,
        user_text="Define the exact source goal.",
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
            "goal attempt",
        ),
        attempt_fingerprint=goal_attempt.attempt_fingerprint,
        recorded_at=at + timedelta(seconds=5),
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
            "goal turn",
        ),
        created_by="fixture",
        created_at=at + timedelta(seconds=6),
    )
    session.add(goal)
    session.flush()
    session.add(
        ProductGoalArtifactDecision(
            project_id=project_id,
            product_goal_artifact_id=_required(
                goal.product_goal_artifact_id,
                "goal",
            ),
            artifact_fingerprint=goal.content_fingerprint,
            decision="accepted",
            rationale="Accepted.",
            reviewer="fixture",
            idempotency_key=f"goal-{project_id}",
            decided_at=at + timedelta(seconds=7),
        )
    )
    session.flush()
    return vision, goal


def _document(
    source_id: str,
    relative_path: str,
    text: str,
) -> SpecificationSourceDocument:
    raw = text.encode("utf-8")
    return SpecificationSourceDocument(
        source_id=source_id,
        relative_path=relative_path,
        content_base64=base64.b64encode(raw).decode("ascii"),
        byte_length=len(raw),
        content_fingerprint="sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def _seed_repository_and_source(  # noqa: PLR0913
    session: Session,
    *,
    project: Project,
    repository: Path,
    probe: GitPythonRepositoryProbe,
    vision: VisionArtifact,
    goal: ProductGoalArtifact,
) -> SpecificationSource:
    observed = probe.inspect(repository)
    binding = RepositoryBinding(
        project_id=_required(project.project_id, "project"),
        worktree_path=observed.worktree_path,
        common_git_dir=observed.common_git_dir,
        head_sha=observed.head_sha,
        branch_name=observed.branch_name,
        detached_head=observed.detached_head,
        dirty=observed.dirty,
        status_fingerprint=observed.status_fingerprint,
        status_entries_json=canonical_json(
            [item.model_dump(mode="json") for item in observed.status_entries]
        ),
        remotes_json=canonical_json(list(observed.remotes)),
        warnings_json=canonical_json(
            [item.model_dump(mode="json") for item in observed.warnings]
        ),
        probe_version=observed.probe_version,
        inspected_at=observed.inspected_at,
        recorded_by="fixture",
    )
    session.add(binding)
    session.flush()
    binding_id = _required(binding.repository_binding_id, "binding")
    project.active_repository_binding_id = binding_id
    session.add(project)
    primary_text = (repository / "SPECIFICATION.md").read_bytes().decode("utf-8")
    context_text = (repository / "CONTEXT.md").read_bytes().decode("utf-8")
    adr_text = (repository / ADR_PATH).read_bytes().decode("utf-8")
    bundle = SpecificationSourceBundle(
        source=_document(
            SPECIFICATION_SOURCE_PRIMARY_ID,
            "SPECIFICATION.md",
            primary_text,
        ),
        context=SpecificationContextCapture(
            state="present",
            document=_document(
                SPECIFICATION_SOURCE_CONTEXT_ID,
                "CONTEXT.md",
                context_text,
            ),
        ),
        adrs=(
            _document(
                specification_source_adr_id(ADR_PATH),
                ADR_PATH,
                adr_text,
            ),
        ),
        repository_revision=SpecificationRepositoryRevision(
            head_sha=observed.head_sha,
            branch_name=observed.branch_name,
            detached_head=observed.detached_head,
            dirty=observed.dirty,
            status_entries=observed.status_entries,
            status_fingerprint=observed.status_fingerprint,
            remotes=observed.remotes,
            probe_version=observed.probe_version,
            warnings=observed.warnings,
        ),
        accepted_vision_fingerprint=vision.content_fingerprint,
        accepted_product_goal_fingerprint=goal.content_fingerprint,
    )
    source = SpecificationSource(
        project_id=_required(project.project_id, "project"),
        source_bundle_json=canonical_json(bundle.model_dump(mode="json")),
        source_fingerprint=source_bundle_fingerprint(bundle),
        repository_binding_id=binding_id,
        repository_head_sha=observed.head_sha,
        repository_dirty=observed.dirty,
        repository_status_fingerprint=observed.status_fingerprint,
        vision_artifact_id=_required(vision.vision_artifact_id, "vision"),
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=_required(goal.product_goal_artifact_id, "goal"),
        product_goal_fingerprint=goal.content_fingerprint,
        supersedes_specification_source_id=None,
        supersedes_source_fingerprint=None,
        registered_by="fixture",
    )
    session.add(source)
    session.flush()
    return source


def _decision(
    vision: VisionArtifact,
    goal: ProductGoalArtifact,
    source: SpecificationSource,
) -> NodeDecision:
    return NodeDecision(
        node_id="specification.structure",
        child_graph_id="specification",
        request_kind="structure_specification",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPECIFICATION_INITIAL_REQUIRED",
        fact_references=(
            FactReference(
                fact_type="vision",
                fact_id=str(_required(vision.vision_artifact_id, "vision")),
                fingerprint=vision.content_fingerprint,
            ),
            FactReference(
                fact_type="product_goal",
                fact_id=str(_required(goal.product_goal_artifact_id, "goal")),
                fingerprint=goal.content_fingerprint,
            ),
            FactReference(
                fact_type="specification_source",
                fact_id=str(
                    _required(source.specification_source_id, "specification source")
                ),
                fingerprint=source.source_fingerprint,
            ),
        ),
        decision_fingerprint=canonical_hash({"decision": "structure"}),
    )


@pytest.fixture
def registered_source(
    engine: Engine,
    tmp_path: Path,
) -> tuple[SpecificationStructuringInputService, NodeDecision, Path]:
    """Persist one exact registered source with current accepted lineage."""
    repository = tmp_path / "registered-source"
    repository.mkdir()
    (repository / "docs/adr").mkdir(parents=True)
    (repository / "SPECIFICATION.md").write_text(
        "\ufeff# Exact source\r\n\r\nKeep exact trailing bytes.\r\n",
        encoding="utf-8",
        newline="",
    )
    (repository / "CONTEXT.md").write_text(
        "Context only from registration.\n",
        encoding="utf-8",
    )
    (repository / ADR_PATH).write_text(
        "# Exact source decision\n",
        encoding="utf-8",
    )
    with Repo.init(repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Structuring Test")
            config.set_value("user", "email", "structuring@example.com")
        repo.index.add(["SPECIFICATION.md", "CONTEXT.md", ADR_PATH])
        repo.index.commit("register source")
        repo.create_remote(
            "origin",
            "https://operator:"
            f"{REMOTE_CREDENTIAL_SENTINEL}@example.invalid/team/repository.git",
        )
    probe = GitPythonRepositoryProbe()
    with Session(engine) as session:
        project = Project(name="Specification structuring input")
        session.add(project)
        session.flush()
        project_id = _required(project.project_id, "project")
        vision, goal = _seed_accepted_lineage(
            session,
            project_id=project_id,
            at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )
        source = _seed_repository_and_source(
            session,
            project=project,
            repository=repository,
            probe=probe,
            vision=vision,
            goal=goal,
        )
        decision = _decision(vision, goal, source)
        session.commit()
    return (
        SpecificationStructuringInputService(
            engine=engine,
            repository_probe=probe,
        ),
        decision,
        repository,
    )


def test_builds_input_from_exact_registered_source_only(
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
) -> None:
    """Build decodes exact registered prose and adds no fresh evidence bundle."""
    service, decision, _repository = registered_source

    raw = service.build(project_id=1, decision=decision)
    result = SpecificationStructuringInput.model_validate(raw)

    assert result.operation == "initial"
    assert result.registered_source.source.text == (
        "\ufeff# Exact source\r\n\r\nKeep exact trailing bytes.\r\n"
    )
    assert result.registered_source.context.document is not None
    assert result.registered_source.context.document.text == (
        "Context only from registration.\n"
    )
    assert result.registered_source.adrs[0].text == "# Exact source decision\n"
    assert {item.source_id for item in result.source_manifest} == {
        SPECIFICATION_VISION_SOURCE_ID,
        SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
        SPECIFICATION_SOURCE_PRIMARY_ID,
        SPECIFICATION_SOURCE_CONTEXT_ID,
        specification_source_adr_id(ADR_PATH),
    }
    assert "vision-evidence" not in json.dumps(raw)
    assert "repository-context.active" not in json.dumps(raw)


def test_build_revision_preserves_feedback_across_source_reentry_chain(
    engine: Engine,
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
) -> None:
    """S1 feedback remains exact input after candidate-less S2 and optional S3."""
    service, initial_decision, _repository = registered_source
    initial_input = SpecificationStructuringInput.model_validate(
        service.build(project_id=1, decision=initial_decision)
    )
    with Session(engine) as session:
        vision = session.get(VisionArtifact, 1)
        goal = session.get(ProductGoalArtifact, 1)
        first = session.get(SpecificationSource, 1)
        assert vision is not None
        assert goal is not None
        assert first is not None
        source_bundle = SpecificationSourceBundle.model_validate_json(
            first.source_bundle_json
        )
        base_at = first.registered_at.replace(tzinfo=UTC) + timedelta(seconds=1)
        attempt = _attempt(
            session,
            project_id=1,
            node_id="specification.structure",
            ordinal=3,
            at=base_at,
        )
        normalized_input = initial_input.model_dump(mode="json")
        attempt.normalized_input_json = canonical_json(normalized_input)
        attempt.input_fingerprint = canonical_hash(normalized_input)
        attempt.attempt_fingerprint = workflow_node_attempt_fingerprint(
            {
                "attempt_id": _required(
                    attempt.workflow_node_attempt_id,
                    "attempt",
                ),
                "project_id": 1,
                "node_id": attempt.node_id,
                "instance_key": attempt.instance_key,
                "graph_version": attempt.graph_version,
                "fact_fingerprint": attempt.fact_fingerprint,
                "business_fact_fingerprint": attempt.business_fact_fingerprint,
                "decision_fingerprint": attempt.decision_fingerprint,
                "normalized_input": normalized_input,
                "input_fingerprint": attempt.input_fingerprint,
                "model_id": attempt.model_id,
                "execution_settings": {},
                "idempotency_key": attempt.idempotency_key,
                "actor": attempt.actor,
                "correlation_id": attempt.correlation_id,
                "started_at": attempt.started_at,
                "lease_expires_at": attempt.lease_expires_at,
            }
        )
        session.add(attempt)
        session.flush()
        payload = SpecificationPayload.model_validate(
            {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": "SPEC.ancestor-feedback",
                "title": "Ancestor feedback",
                "summary": "Preserve exact feedback across source re-entry.",
                "problem_statement": (
                    "Optional replacement must retain revision context."
                ),
                "items": [
                    {
                        "id": "REQ.ancestor-feedback",
                        "type": "REQ",
                        "title": "Preserve feedback",
                        "statement": (
                            "Structure using the exact rejected ancestor candidate."
                        ),
                        "level": "MUST",
                        "verification": "system-test",
                        "acceptance": ["Prior feedback is supplied without rewriting."],
                    }
                ],
                "relations": [],
                "controlled_terms": [],
                "external_references": [],
            }
        )
        attempt_id = _required(attempt.workflow_node_attempt_id, "attempt")
        envelope = build_candidate_envelope(
            payload=payload,
            metadata=CandidateBuildInput(
                candidate_kind=CandidateKind.INITIAL,
                accepted_vision_id=_required(vision.vision_artifact_id, "vision"),
                accepted_vision_fingerprint=vision.content_fingerprint,
                accepted_product_goal_id=_required(
                    goal.product_goal_artifact_id,
                    "goal",
                ),
                accepted_product_goal_fingerprint=goal.content_fingerprint,
                registered_source_fingerprint=first.source_fingerprint,
                source_producer_capability=source_bundle.producer_capability,
                source_preparation_capability=(source_bundle.preparation_capability),
                source_manifest=initial_input.source_manifest,
                accepted_fact_fingerprint=(
                    specification_structuring_fact_fingerprint(initial_input)
                ),
                producer_input_fingerprint=(
                    specification_structuring_input_fingerprint(initial_input)
                ),
                producer_capability="specification-structurer",
                producer_version="fixture-v2",
                model_id=attempt.model_id,
                model_configuration_fingerprint=canonical_hash(
                    {"model": attempt.model_id}
                ),
                prompt_version=SPECIFICATION_STRUCTURER_PROMPT_VERSION,
                prompt_fingerprint=canonical_hash({"prompt": "fixture"}),
                workflow_node_attempt_id=attempt_id,
                attempt_fingerprint=attempt.attempt_fingerprint,
                correlation_id="ancestor-feedback",
                produced_at=base_at,
            ),
        )
        candidate = SpecificationCandidate(
            project_id=1,
            candidate_kind="initial",
            specification_source_id=_required(
                first.specification_source_id,
                "source",
            ),
            specification_source_fingerprint=first.source_fingerprint,
            vision_artifact_id=_required(vision.vision_artifact_id, "vision"),
            vision_fingerprint=vision.content_fingerprint,
            product_goal_artifact_id=_required(
                goal.product_goal_artifact_id,
                "goal",
            ),
            product_goal_fingerprint=goal.content_fingerprint,
            canonical_envelope_json=canonical_candidate_json(payload, envelope),
            payload_fingerprint=envelope.payload_fingerprint,
            source_manifest_fingerprint=envelope.source_manifest_fingerprint,
            producer_input_fingerprint=envelope.producer_input_fingerprint,
            rendered_view_fingerprint=envelope.review_view_fingerprint,
            candidate_fingerprint=envelope.candidate_fingerprint,
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_by="fixture",
            recorded_at=base_at + timedelta(seconds=1),
        )
        session.add(candidate)
        session.flush()
        candidate_id = _required(
            candidate.specification_candidate_id,
            "candidate",
        )
        session.add(
            SpecificationDecision(
                project_id=1,
                specification_candidate_id=candidate_id,
                candidate_fingerprint=candidate.candidate_fingerprint,
                decision="feedback",
                rationale="Clarify exact ancestor terminology.",
                reviewer="fixture",
                idempotency_key="ancestor-feedback",
                decided_at=base_at + timedelta(seconds=2),
            )
        )

        def replacement(
            parent: SpecificationSource,
            *,
            registered_at: datetime,
        ) -> SpecificationSource:
            return SpecificationSource(
                project_id=parent.project_id,
                source_bundle_json=parent.source_bundle_json,
                source_fingerprint=parent.source_fingerprint,
                repository_binding_id=parent.repository_binding_id,
                repository_head_sha=parent.repository_head_sha,
                repository_dirty=parent.repository_dirty,
                repository_status_fingerprint=parent.repository_status_fingerprint,
                vision_artifact_id=parent.vision_artifact_id,
                vision_fingerprint=parent.vision_fingerprint,
                product_goal_artifact_id=parent.product_goal_artifact_id,
                product_goal_fingerprint=parent.product_goal_fingerprint,
                supersedes_specification_source_id=_required(
                    parent.specification_source_id,
                    "parent source",
                ),
                supersedes_source_fingerprint=parent.source_fingerprint,
                registered_by="fixture",
                registered_at=registered_at,
            )

        second = replacement(
            first,
            registered_at=base_at + timedelta(seconds=3),
        )
        session.add(second)
        session.flush()
        third = replacement(
            second,
            registered_at=base_at + timedelta(seconds=4),
        )
        session.add(third)
        session.flush()
        decision = _decision(vision, goal, third).model_copy(
            update={
                "reason_code": "SPECIFICATION_REVISION_REQUIRED",
                "fact_references": (
                    *_decision(vision, goal, third).fact_references,
                    FactReference(
                        fact_type="specification_candidate",
                        fact_id=str(candidate_id),
                        fingerprint=candidate.candidate_fingerprint,
                    ),
                ),
            }
        )
        prior_fingerprint = candidate.candidate_fingerprint
        session.commit()

    result = SpecificationStructuringInput.model_validate(
        service.build(project_id=1, decision=decision)
    )

    assert result.operation == "revision"
    assert result.prior_candidate is not None
    assert result.prior_candidate.candidate_fingerprint == prior_fingerprint
    assert result.prior_candidate.decision == "feedback"
    assert result.prior_candidate.rationale == "Clarify exact ancestor terminology."


def test_build_excludes_remote_credentials_from_provider_input(
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
) -> None:
    """Provider input retains remote identity without configured credentials."""
    service, decision, _repository = registered_source

    raw = service.build(project_id=1, decision=decision)
    result = SpecificationStructuringInput.model_validate(raw)

    expected = ("https://example.invalid/team/repository.git",)
    assert result.registered_source.repository_revision.remotes == expected
    assert result.registered_source.repository_evidence.remotes == expected
    assert REMOTE_CREDENTIAL_SENTINEL not in json.dumps(raw, sort_keys=True)


def test_build_rejects_decision_without_exact_source_reference(
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
) -> None:
    """Transport input cannot choose or omit the durable registration identity."""
    service, decision, _repository = registered_source
    stale = decision.model_copy(
        update={
            "fact_references": tuple(
                item
                for item in decision.fact_references
                if item.fact_type != "specification_source"
            )
        }
    )

    with pytest.raises(ValueError, match="exact registered Specification source"):
        service.build(project_id=1, decision=stale)


def test_build_requires_structuring_node(
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
) -> None:
    """The removed specification.author decision cannot enter this service."""
    service, decision, _repository = registered_source
    retired = decision.model_copy(update={"node_id": "specification.author"})

    with pytest.raises(ValueError, match=r"specification\.structure"):
        service.build(project_id=1, decision=retired)


@pytest.mark.parametrize(
    "drift_kind",
    ["source", "context", "adr", "vision", "goal", "repository"],
)
def test_revalidate_detects_every_registered_input_drift(
    engine: Engine,
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
    drift_kind: str,
) -> None:
    """Source, Context, ADR, Vision, Goal, and repository drift all return STALE."""
    service, decision, repository = registered_source
    raw = service.build(project_id=1, decision=decision)
    if drift_kind == "source":
        (repository / "SPECIFICATION.md").write_text(
            "Changed source.\n",
            encoding="utf-8",
        )
    elif drift_kind == "context":
        (repository / "CONTEXT.md").write_text(
            "Changed Context.\n",
            encoding="utf-8",
        )
    elif drift_kind == "adr":
        (repository / ADR_PATH).write_text("Changed ADR.\n", encoding="utf-8")
    elif drift_kind == "repository":
        (repository / "unregistered-change.txt").write_text(
            "Repository revision changed.\n",
            encoding="utf-8",
        )
    else:
        with Session(engine) as session:
            if drift_kind == "vision":
                artifact = session.get(VisionArtifact, 1)
            else:
                artifact = session.get(ProductGoalArtifact, 1)
            assert artifact is not None
            artifact.statement = "Persisted lineage was tampered."
            session.add(artifact)
            session.commit()

    stale = service.revalidate_sources(1, raw)

    assert stale is not None
    assert stale.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT


def test_revalidate_translates_repository_probe_failure_to_stale(
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
) -> None:
    """A failed live probe never escapes as an infrastructure exception."""
    service, decision, _repository = registered_source
    raw = service.build(project_id=1, decision=decision)

    class _UnavailableProbe:
        def inspect(self, path: Path | str) -> RepositoryProbeResult:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
                str(path),
            )

    stale = SpecificationStructuringInputService(
        engine=service.engine,
        repository_probe=_UnavailableProbe(),
    ).revalidate_sources(1, raw)

    assert stale is not None
    assert stale.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT


def test_revalidate_detects_persisted_source_tampering(
    engine: Engine,
    registered_source: tuple[SpecificationStructuringInputService, NodeDecision, Path],
) -> None:
    """Canonical durable source JSON remains part of every live check."""
    service, decision, _repository = registered_source
    raw = service.build(project_id=1, decision=decision)
    with Session(engine) as session:
        source = session.get(SpecificationSource, 1)
        assert source is not None
        source.source_bundle_json = "{}"
        session.add(source)
        session.commit()

    stale = service.revalidate_sources(1, raw)

    assert stale is not None
    assert stale.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
