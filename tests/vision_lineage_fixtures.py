"""Provider-free durable Vision lineage fixtures for focused projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from models.product_definition import (
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
)
from models.workflow import WorkflowNodeAttempt
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlmodel import Session


def _required(value: int | None, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise AssertionError(message)
    return value


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
        node_id="vision.interview",
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
    turn = VisionInterviewTurn(
        project_id=project_id,
        mode="initial",
        turn_number=version_number,
        revision_intent_id=None,
        prior_turn_id=None,
        user_text="Define the durable Project Vision.",
        components_json=canonical_json(components),
        vision_statement=statement,
        is_complete=True,
        clarifying_questions_json="[]",
        output_fingerprint=canonical_hash(
            {
                "components_json": components,
                "vision_statement": statement,
                "is_complete": True,
                "clarifying_questions_json": [],
            }
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
