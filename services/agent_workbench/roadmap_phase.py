"""Agent workbench Roadmap phase command runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import Project
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import RoadmapArtifact, RoadmapArtifactDecision
from services.contracts.roadmap import RoadmapBuilderOutput
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.contracts import JsonObject


@dataclass(frozen=True)
class RecordRoadmapDraftInput:
    """Exact immutable values used to record one Roadmap draft."""

    project_id: int
    backlog_artifact_id: int
    backlog_artifact_fingerprint: str
    canonical_content: JsonObject
    content_fingerprint: str
    supersedes_roadmap_artifact_id: int | None
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class RecordRoadmapDecisionInput:
    """Exact append-only values used to decide one Roadmap draft."""

    artifact: RoadmapArtifact
    decision: str
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: datetime


def record_roadmap_draft_in_session(
    session: Session,
    *,
    inputs: RecordRoadmapDraftInput,
) -> RoadmapArtifact:
    """Validate and add one immutable Roadmap artifact to the caller transaction."""
    RoadmapBuilderOutput.model_validate(inputs.canonical_content)
    if canonical_hash(inputs.canonical_content) != inputs.content_fingerprint:
        message = "Roadmap content fingerprint does not match canonical content."
        raise ValueError(message)
    existing = session.exec(
        select(RoadmapArtifact)
        .where(RoadmapArtifact.project_id == inputs.project_id)
        .order_by(col(RoadmapArtifact.version_number))
    ).all()
    expected_parent = existing[-1].roadmap_artifact_id if existing else None
    if inputs.supersedes_roadmap_artifact_id != expected_parent:
        message = "Roadmap supersession does not match the current artifact."
        raise ValueError(message)
    row = RoadmapArtifact(
        project_id=inputs.project_id,
        backlog_artifact_id=inputs.backlog_artifact_id,
        backlog_artifact_fingerprint=inputs.backlog_artifact_fingerprint,
        version_number=len(existing) + 1,
        canonical_content_json=canonical_json(inputs.canonical_content),
        content_fingerprint=inputs.content_fingerprint,
        supersedes_roadmap_artifact_id=inputs.supersedes_roadmap_artifact_id,
        created_by=inputs.actor,
        created_at=inputs.recorded_at,
    )
    session.add(row)
    session.flush()
    return row

def record_roadmap_decision_in_session(
    session: Session,
    *,
    inputs: RecordRoadmapDecisionInput,
) -> RoadmapArtifactDecision:
    """Append one exact Roadmap decision and update the accepted projection."""
    artifact = inputs.artifact
    if inputs.decision not in {"accepted", "rejected", "feedback"}:
        message = "Roadmap decision is invalid."
        raise ValueError(message)
    artifact_id = artifact.roadmap_artifact_id
    if artifact_id is None:
        message = "Roadmap artifact has no durable identity."
        raise ValueError(message)
    existing = session.exec(
        select(RoadmapArtifactDecision).where(
            RoadmapArtifactDecision.project_id == artifact.project_id,
            RoadmapArtifactDecision.roadmap_artifact_id == artifact_id,
        )
    ).first()
    if existing is not None:
        message = "Roadmap artifact already has a terminal decision."
        raise ValueError(message)
    row = RoadmapArtifactDecision(
        project_id=artifact.project_id,
        roadmap_artifact_id=artifact_id,
        artifact_fingerprint=artifact.content_fingerprint,
        decision=inputs.decision,
        rationale=inputs.rationale,
        reviewer=inputs.reviewer,
        idempotency_key=inputs.idempotency_key,
        decided_at=inputs.decided_at,
    )
    session.add(row)
    if inputs.decision == "accepted":
        project = session.get(Project, artifact.project_id)
        if project is None:
            message = "Roadmap Project does not exist."
            raise ValueError(message)
        project.roadmap = artifact.canonical_content_json
        project.updated_at = inputs.decided_at
        session.add(project)
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.ROADMAP_SAVED,
                timestamp=inputs.decided_at,
                project_id=artifact.project_id,
                duration_seconds=0.0,
                event_metadata=canonical_json(
                    {
                        "action": "roadmap_accepted",
                        "content_fingerprint": artifact.content_fingerprint,
                        "roadmap_artifact_id": artifact.roadmap_artifact_id,
                    }
                ),
            )
        )
    session.flush()
    return row
