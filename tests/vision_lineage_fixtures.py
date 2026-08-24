"""Provider-free durable Vision lineage fixtures for focused projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from models.product_definition import (
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.workflow import WorkflowNodeAttempt
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    vision_interview_output_fingerprint,
)

if TYPE_CHECKING:
    from sqlmodel import Session


def _required(value: int | None, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise AssertionError(message)
    return value


def _snapshot(
    session: Session,
    *,
    project_id: int,
    attempt_id: int,
    timestamp: datetime,
) -> VisionEvidenceSnapshot:
    evidence_item: JsonObject = {
        "evidence_id": "project:metadata",
        "kind": "project_metadata",
        "relative_path": None,
        "content_fingerprint": canonical_hash(
            {"name": "Vision fixture", "description": None}
        ),
        "trust": "operator_provided",
        "content": {"name": "Vision fixture", "description": None},
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
    row = VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=None,
        workflow_node_attempt_id=attempt_id,
        evidence_json=canonical_json(evidence),
        evidence_fingerprint=str(evidence["evidence_fingerprint"]),
        warnings_json="[]",
        created_at=timestamp,
    )
    session.add(row)
    session.flush()
    return row


def seed_accepted_vision(
    session: Session,
    *,
    project_id: int,
    statement: str,
    version_number: int = 1,
) -> VisionArtifact:
    """Persist one complete immutable Vision and its accepted decision."""
    timestamp = datetime.now(UTC)
    components: JsonObject = {"purpose": f"vision fixture {version_number}"}
    attempt_fingerprint = f"sha256:vision-fixture-{project_id}-{version_number}"
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="vision.bootstrap",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=f"sha256:vision-facts-{project_id}-{version_number}",
        business_fact_fingerprint=(
            f"sha256:vision-business-{project_id}-{version_number}"
        ),
        decision_fingerprint=f"sha256:vision-decision-{project_id}-{version_number}",
        normalized_input_json="{}",
        input_fingerprint=f"sha256:vision-input-{project_id}-{version_number}",
        model_id="fake/vision-fixture",
        execution_settings_json="{}",
        idempotency_key=f"vision-attempt-{project_id}-{version_number}",
        actor="vision-fixture",
        correlation_id=None,
        started_at=timestamp,
        lease_expires_at=timestamp + timedelta(minutes=1),
        attempt_fingerprint=attempt_fingerprint,
    )
    session.add(attempt)
    session.flush()
    snapshot = _snapshot(
        session,
        project_id=project_id,
        attempt_id=_required(attempt.workflow_node_attempt_id, "Vision attempt"),
        timestamp=timestamp,
    )
    turn = VisionInterviewTurn(
        project_id=project_id,
        operation="bootstrap",
        turn_number=version_number,
        revision_intent_id=None,
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "Vision evidence snapshot",
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
            attempt.workflow_node_attempt_id,
            "Vision attempt",
        ),
        attempt_fingerprint=attempt.attempt_fingerprint,
        recorded_at=timestamp + timedelta(seconds=1),
    )
    session.add(turn)
    session.flush()
    artifact = VisionArtifact(
        project_id=project_id,
        version_number=version_number,
        components_json=canonical_json(components),
        statement=statement,
        content_fingerprint=canonical_hash(
            {"components": components, "statement": statement}
        ),
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "Vision evidence snapshot",
        ),
        component_basis_json="[]",
        assumptions_json="[]",
        conflicts_json="[]",
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=_required(
            turn.vision_interview_turn_id,
            "Vision interview turn",
        ),
        created_by="vision-fixture",
        created_at=timestamp + timedelta(seconds=2),
    )
    session.add(artifact)
    session.flush()
    session.add(
        VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=_required(
                artifact.vision_artifact_id,
                "Vision artifact",
            ),
            artifact_fingerprint=artifact.content_fingerprint,
            decision="accepted",
            rationale="Accepted for projection coverage.",
            reviewer="vision-fixture",
            idempotency_key=f"vision-decision-{project_id}-{version_number}",
            decided_at=timestamp + timedelta(seconds=3),
        )
    )
    session.commit()
    return artifact


def seed_accepted_vision_revision(
    session: Session,
    *,
    project_id: int,
    superseded_vision: VisionArtifact,
    statement: str,
) -> VisionArtifact:
    """Persist one accepted revision that supersedes an accepted Vision leaf."""
    prior_id = _required(
        superseded_vision.vision_artifact_id,
        "Superseded Vision artifact",
    )
    version_number = superseded_vision.version_number + 1
    timestamp = datetime.now(UTC)
    components: JsonObject = {"purpose": f"vision fixture {version_number}"}
    attempt_fingerprint = f"sha256:vision-fixture-{project_id}-{version_number}"
    revision = VisionRevisionIntent(
        project_id=project_id,
        source_vision_artifact_id=prior_id,
        source_vision_fingerprint=superseded_vision.content_fingerprint,
        reason="Refine the accepted product direction.",
        initiated_by="vision-fixture",
        initiated_at=timestamp,
    )
    session.add(revision)
    session.flush()
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="vision.bootstrap",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=f"sha256:vision-facts-{project_id}-{version_number}",
        business_fact_fingerprint=(
            f"sha256:vision-business-{project_id}-{version_number}"
        ),
        decision_fingerprint=f"sha256:vision-decision-{project_id}-{version_number}",
        normalized_input_json="{}",
        input_fingerprint=f"sha256:vision-input-{project_id}-{version_number}",
        model_id="fake/vision-fixture",
        execution_settings_json="{}",
        idempotency_key=f"vision-attempt-{project_id}-{version_number}",
        actor="vision-fixture",
        correlation_id=None,
        started_at=timestamp,
        lease_expires_at=timestamp + timedelta(minutes=1),
        attempt_fingerprint=attempt_fingerprint,
    )
    session.add(attempt)
    session.flush()
    snapshot = _snapshot(
        session,
        project_id=project_id,
        attempt_id=_required(attempt.workflow_node_attempt_id, "Vision attempt"),
        timestamp=timestamp,
    )
    turn = VisionInterviewTurn(
        project_id=project_id,
        operation="revision",
        turn_number=1,
        revision_intent_id=_required(
            revision.vision_revision_intent_id,
            "Vision revision intent",
        ),
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "Vision evidence snapshot",
        ),
        prior_turn_id=None,
        user_text="Refine the durable Project Vision.",
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
            attempt.workflow_node_attempt_id,
            "Vision attempt",
        ),
        attempt_fingerprint=attempt.attempt_fingerprint,
        recorded_at=timestamp + timedelta(seconds=1),
    )
    session.add(turn)
    session.flush()
    artifact = VisionArtifact(
        project_id=project_id,
        version_number=version_number,
        components_json=canonical_json(components),
        statement=statement,
        content_fingerprint=canonical_hash(
            {"components": components, "statement": statement}
        ),
        vision_evidence_snapshot_id=_required(
            snapshot.vision_evidence_snapshot_id,
            "Vision evidence snapshot",
        ),
        component_basis_json="[]",
        assumptions_json="[]",
        conflicts_json="[]",
        supersedes_vision_artifact_id=prior_id,
        source_interview_turn_id=_required(
            turn.vision_interview_turn_id,
            "Vision interview turn",
        ),
        created_by="vision-fixture",
        created_at=timestamp + timedelta(seconds=2),
    )
    session.add(artifact)
    session.flush()
    session.add(
        VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=_required(
                artifact.vision_artifact_id,
                "Vision artifact",
            ),
            artifact_fingerprint=artifact.content_fingerprint,
            decision="accepted",
            rationale="Accepted for revision projection coverage.",
            reviewer="vision-fixture",
            idempotency_key=f"vision-decision-{project_id}-{version_number}",
            decided_at=timestamp + timedelta(seconds=3),
        )
    )
    session.commit()
    return artifact
