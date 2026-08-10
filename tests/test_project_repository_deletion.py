"""Fresh-schema Project deletion tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from models.core import Project
from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from repositories.project import ProjectRepository
from services.contracts.vision_evidence import VisionEvidenceBundle, VisionEvidenceItem
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_REPOSITORY_PATH = "repository"
_COMMIT_FAILURE = "injected commit failure"

_PRODUCT_MODELS = (
    VisionRevisionIntent,
    VisionInterviewTurn,
    VisionArtifactDecision,
    VisionArtifact,
    VisionEvidenceSnapshot,
    ProductGoalOutcome,
    ProductGoalArtifactDecision,
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    SpecificationDecision,
    SpecificationCandidate,
    DiscoveryArtifact,
    SpecRegistry,
    RepositoryBinding,
    WorkflowNodeAttemptOutcome,
    WorkflowNodeAttempt,
)


def _repository_binding(project_id: int) -> RepositoryBinding:
    return RepositoryBinding(
        project_id=project_id,
        worktree_path=_REPOSITORY_PATH,
        common_git_dir=f"{_REPOSITORY_PATH}/.git",
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_fingerprint="status-1",
        remotes_json="[]",
        warnings_json="[]",
        probe_version="agileforge.repository-probe.v1",
        inspected_at=datetime(2026, 8, 9, 12, tzinfo=UTC),
        recorded_by="operator@example.com",
    )


def _vision_evidence_snapshot(
    project_id: int,
    attempt: WorkflowNodeAttempt,
    repository_binding_id: int,
) -> VisionEvidenceSnapshot:
    """Create one valid immutable snapshot bound to the deletion fixture."""
    content: JsonObject = {"project_name": "Populated lineage"}
    item = VisionEvidenceItem(
        evidence_id="project-metadata",
        kind="project_metadata",
        relative_path=None,
        content_fingerprint=canonical_hash(content),
        trust="operator_provided",
        content=content,
        truncated=False,
    )
    payload = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [item.model_dump(mode="json")],
        "warnings": [],
    }
    evidence = VisionEvidenceBundle(
        schema_version="agileforge.vision-evidence.v1",
        items=(item,),
        warnings=(),
        evidence_fingerprint=canonical_hash(payload),
    )
    assert attempt.workflow_node_attempt_id is not None
    return VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=repository_binding_id,
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        evidence_json=canonical_json(evidence.model_dump(mode="json")),
        evidence_fingerprint=evidence.evidence_fingerprint,
        warnings_json="[]",
        created_at=datetime(2026, 8, 9, 13, 3, 15, tzinfo=UTC),
    )


def _seed_populated_product_lineage(session: Session) -> int:
    """Persist every current product-lineage family, including a Vision revision."""
    project = Project(name="Populated lineage")
    session.add(project)
    session.flush()
    assert project.project_id is not None
    project_id = project.project_id
    binding = _repository_binding(project_id)
    session.add(binding)
    session.flush()
    assert binding.repository_binding_id is not None
    project.active_repository_binding_id = binding.repository_binding_id
    session.add(project)
    session.commit()

    lineage = seed_accepted_specification(
        session,
        project_id=project_id,
        content='{"title":"Accepted specification"}',
        recorded_at=datetime(2026, 8, 9, 13, tzinfo=UTC),
    )
    session.add(
        ProductGoalOutcome(
            project_id=project_id,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            artifact_fingerprint=lineage.product_goal_fingerprint,
            outcome="fulfilled",
            rationale="Completed before deletion.",
            decided_by="operator@example.com",
            idempotency_key="goal-outcome-delete",
            decided_at=datetime(2026, 8, 9, 13, 1, tzinfo=UTC),
        )
    )
    intent = VisionRevisionIntent(
        project_id=project_id,
        source_vision_artifact_id=lineage.vision_artifact_id,
        source_vision_fingerprint=lineage.vision_fingerprint,
        reason="Exercise revision deletion order.",
        initiated_by="operator@example.com",
        initiated_at=datetime(2026, 8, 9, 13, 2, tzinfo=UTC),
    )
    session.add(intent)
    session.flush()
    assert intent.vision_revision_intent_id is not None
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="vision.interview",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=canonical_hash({"facts": "revision"}),
        business_fact_fingerprint=canonical_hash({"business": "revision"}),
        decision_fingerprint=canonical_hash({"decision": "revision"}),
        normalized_input_json="{}",
        input_fingerprint=canonical_hash({"input": "revision"}),
        model_id="fake/revision",
        execution_settings_json="{}",
        idempotency_key="vision-revision-attempt-delete",
        actor="operator@example.com",
        correlation_id=None,
        started_at=datetime(2026, 8, 9, 13, 3, tzinfo=UTC),
        lease_expires_at=datetime(2026, 8, 9, 13, 4, tzinfo=UTC),
        attempt_fingerprint=canonical_hash({"attempt": "revision"}),
    )
    session.add(attempt)
    session.flush()
    assert attempt.workflow_node_attempt_id is not None
    snapshot = _vision_evidence_snapshot(
        project_id,
        attempt,
        binding.repository_binding_id,
    )
    session.add(snapshot)
    session.flush()
    assert snapshot.vision_evidence_snapshot_id is not None
    turn = VisionInterviewTurn(
        project_id=project_id,
        operation="revision",
        turn_number=1,
        revision_intent_id=intent.vision_revision_intent_id,
        vision_evidence_snapshot_id=snapshot.vision_evidence_snapshot_id,
        prior_turn_id=None,
        user_text="Revise the Vision.",
        components_json='{"purpose":"revised"}',
        vision_statement="Deliver the revised Vision.",
        is_complete=True,
        clarifying_questions_json="[]",
        component_basis_json="[]",
        assumptions_json="[]",
        conflicts_json="[]",
        output_fingerprint=canonical_hash({"output": "revision"}),
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        attempt_fingerprint=attempt.attempt_fingerprint,
        recorded_at=datetime(2026, 8, 9, 13, 3, 30, tzinfo=UTC),
    )
    session.add(turn)
    session.flush()
    assert turn.vision_interview_turn_id is not None
    session.add(
        VisionArtifact(
            project_id=project_id,
            version_number=2,
            components_json='{"purpose":"revised"}',
            statement="Deliver the revised Vision.",
            content_fingerprint=canonical_hash({"vision": "revised"}),
            vision_evidence_snapshot_id=snapshot.vision_evidence_snapshot_id,
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=lineage.vision_artifact_id,
            source_interview_turn_id=turn.vision_interview_turn_id,
            created_by="operator@example.com",
            created_at=datetime(2026, 8, 9, 13, 4, tzinfo=UTC),
        )
    )
    session.commit()
    return project_id


def _record_counts(session: Session) -> dict[type[object], int]:
    return {model: len(session.exec(select(model)).all()) for model in _PRODUCT_MODELS}


def test_delete_project_removes_active_repository_binding(engine: Engine) -> None:
    """Remove the Project pointer and immutable repository observations together."""
    with Session(engine) as session:
        project = Project(name="Repository deletion")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        binding = _repository_binding(project_id)
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        project.active_repository_binding_id = binding_id
        session.add(project)
        session.commit()

        assert ProjectRepository(session).delete_project(project_id) is True
        assert session.get(Project, project_id) is None
        assert session.get(RepositoryBinding, binding_id) is None


def test_delete_project_rolls_back_repository_rows_when_commit_fails(
    engine: Engine,
) -> None:
    """Leave both Project and active binding intact when the write cannot commit."""
    with Session(engine) as session:
        project = Project(name="Repository rollback")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        binding = _repository_binding(project_id)
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        project.active_repository_binding_id = binding_id
        session.add(project)
        session.commit()

        def fail_commit(_session: Session) -> None:
            raise RuntimeError(_COMMIT_FAILURE)

        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match=_COMMIT_FAILURE):
                ProjectRepository(session).delete_project(project_id)
        finally:
            event.remove(session, "before_commit", fail_commit)

        assert session.get(Project, project_id) is not None
        assert session.get(RepositoryBinding, binding_id) is not None


def test_delete_project_removes_complete_product_lineage(engine: Engine) -> None:
    """Delete revision Vision, Goal, discovery, specification, and repository rows."""
    with Session(engine) as session:
        project_id = _seed_populated_product_lineage(session)

        assert all(count > 0 for count in _record_counts(session).values())
        assert ProjectRepository(session).delete_project(project_id) is True

        assert session.get(Project, project_id) is None
        assert all(count == 0 for count in _record_counts(session).values())


def test_populated_product_lineage_deletion_rolls_back_on_failure(
    engine: Engine,
) -> None:
    """Restore every populated lineage row when the deletion transaction fails."""
    with Session(engine) as session:
        project_id = _seed_populated_product_lineage(session)
        before = _record_counts(session)

        def fail_commit(_session: Session) -> None:
            raise RuntimeError(_COMMIT_FAILURE)

        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match=_COMMIT_FAILURE):
                ProjectRepository(session).delete_project(project_id)
        finally:
            event.remove(session, "before_commit", fail_commit)

        assert session.get(Project, project_id) is not None
        assert _record_counts(session) == before
        assert ProjectRepository(session).delete_project(project_id) is True
