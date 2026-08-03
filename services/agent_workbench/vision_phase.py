"""Agent workbench Vision phase command runner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session, col, func, select

from models.core import Project
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import VisionArtifact, VisionArtifactDecision
from services.contracts.vision import OutputSchema
from workflow.contracts import JsonObject
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from datetime import datetime

_JSON_OBJECT = TypeAdapter(JsonObject)


def record_vision_draft_in_session(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
    canonical_content: JsonObject,
    content_fingerprint: str,
    supersedes_vision_artifact_id: int | None,
    artifact_id: int,
    actor: str,
    recorded_at: datetime,
) -> VisionArtifact:
    """Validate and append one immutable Vision artifact in caller transaction."""
    if session.get(Project, project_id) is None:
        message = f"Project {project_id} not found."
        raise ValueError(message)
    validated = OutputSchema.model_validate(canonical_content)
    normalized = _JSON_OBJECT.validate_python(validated.model_dump(mode="json"))
    if not validated.is_complete or not validated.updated_components.is_fully_defined():
        message = "Vision output is incomplete and cannot enter review."
        raise ValueError(message)
    if normalized != canonical_content:
        message = "Vision content must be the exact host-validated canonical output."
        raise ValueError(message)
    if canonical_hash(canonical_content) != content_fingerprint:
        message = "Vision content fingerprint does not match canonical content."
        raise ValueError(message)

    parent: VisionArtifact | None = None
    if supersedes_vision_artifact_id is not None:
        parent = session.exec(
            select(VisionArtifact).where(
                col(VisionArtifact.project_id) == project_id,
                col(VisionArtifact.vision_artifact_id) == supersedes_vision_artifact_id,
            )
        ).one_or_none()
        if parent is None:
            message = "Vision supersession parent does not belong to this Project."
            raise ValueError(message)

    version_number = (
        session.exec(
            select(func.count())
            .select_from(VisionArtifact)
            .where(col(VisionArtifact.project_id) == project_id)
        ).one()
        + 1
    )
    row = VisionArtifact(
        vision_artifact_id=artifact_id,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        version_number=version_number,
        canonical_content_json=canonical_json(canonical_content),
        content_fingerprint=content_fingerprint,
        supersedes_vision_artifact_id=(
            None if parent is None else parent.vision_artifact_id
        ),
        created_by=actor,
        created_at=recorded_at,
    )
    session.add(row)
    session.flush()
    return row

def record_vision_decision_in_session(  # noqa: PLR0913
    session: Session,
    *,
    artifact: VisionArtifact,
    decision: str,
    rationale: str,
    reviewer: str,
    idempotency_key: str,
    decided_at: datetime,
) -> VisionArtifactDecision:
    """Append one exact Vision decision and refresh only the legacy projection."""
    existing = session.exec(
        select(VisionArtifactDecision).where(
            col(VisionArtifactDecision.project_id) == artifact.project_id,
            col(VisionArtifactDecision.vision_artifact_id)
            == artifact.vision_artifact_id,
        )
    ).one_or_none()
    if existing is not None:
        message = "Vision artifact already has a terminal review decision."
        raise ValueError(message)
    row = VisionArtifactDecision(
        project_id=artifact.project_id,
        vision_artifact_id=artifact.vision_artifact_id,
        artifact_fingerprint=artifact.content_fingerprint,
        decision=decision,
        rationale=rationale,
        reviewer=reviewer,
        idempotency_key=idempotency_key,
        decided_at=decided_at,
    )
    session.add(row)
    if decision == "accepted":
        content = _JSON_OBJECT.validate_json(artifact.canonical_content_json)
        validated = OutputSchema.model_validate(content)
        project = session.get(Project, artifact.project_id)
        if project is None:
            message = f"Project {artifact.project_id} not found."
            raise ValueError(message)
        project.vision = validated.product_vision_statement
        session.add(project)
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.VISION_SAVED,
                project_id=artifact.project_id,
                timestamp=decided_at,
                event_metadata=canonical_json(
                    {
                        "action": "vision_artifact_accepted",
                        "vision_artifact_id": artifact.vision_artifact_id,
                        "artifact_fingerprint": artifact.content_fingerprint,
                        "authority_id": artifact.authority_id,
                        "authority_fingerprint": artifact.authority_fingerprint,
                    }
                ),
            )
        )
    session.flush()
    return row
