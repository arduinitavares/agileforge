"""Agent workbench Vision phase command runner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session, col, func, select

from models.core import Project
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.product_definition import (
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
)
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
    canonical_content: JsonObject,
    content_fingerprint: str,
    supersedes_vision_artifact_id: int | None,
    user_text: str,
    attempt_id: int | None,
    attempt_fingerprint: str | None,
    actor: str,
    recorded_at: datetime,
) -> VisionArtifact:
    """Materialize one retained legacy result as narrowed Vision facts."""
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
    if not user_text.strip():
        message = "Legacy Vision input must include the trusted user text."
        raise ValueError(message)
    if attempt_id is None or attempt_fingerprint is None:
        message = "Legacy Vision completion requires its durable node attempt."
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
    prior_turn = session.exec(
        select(VisionInterviewTurn)
        .where(
            col(VisionInterviewTurn.project_id) == project_id,
            col(VisionInterviewTurn.mode) == "initial",
            col(VisionInterviewTurn.revision_intent_id).is_(None),
        )
        .order_by(col(VisionInterviewTurn.turn_number).desc())
    ).first()
    components = validated.updated_components.model_dump(mode="json")
    questions = list(validated.clarifying_questions)
    turn = VisionInterviewTurn(
        project_id=project_id,
        mode="initial",
        turn_number=1 if prior_turn is None else prior_turn.turn_number + 1,
        revision_intent_id=None,
        prior_turn_id=(
            None if prior_turn is None else prior_turn.vision_interview_turn_id
        ),
        user_text=user_text.strip(),
        components_json=canonical_json(components),
        vision_statement=validated.product_vision_statement.strip(),
        is_complete=True,
        clarifying_questions_json=canonical_json(questions),
        output_fingerprint=canonical_hash(
            {
                "components_json": components,
                "vision_statement": validated.product_vision_statement.strip(),
                "is_complete": True,
                "clarifying_questions_json": questions,
            }
        ),
        workflow_node_attempt_id=attempt_id,
        attempt_fingerprint=attempt_fingerprint,
        recorded_at=recorded_at,
    )
    session.add(turn)
    session.flush()
    if turn.vision_interview_turn_id is None:
        message = "Legacy Vision turn did not receive a durable identity."
        raise ValueError(message)
    artifact_fingerprint = canonical_hash(
        {
            "components": components,
            "statement": validated.product_vision_statement.strip(),
        }
    )
    row = VisionArtifact(
        project_id=project_id,
        version_number=version_number,
        components_json=canonical_json(components),
        statement=validated.product_vision_statement.strip(),
        content_fingerprint=artifact_fingerprint,
        supersedes_vision_artifact_id=(
            None if parent is None else parent.vision_artifact_id
        ),
        source_interview_turn_id=turn.vision_interview_turn_id,
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
                    }
                ),
            )
        )
    session.flush()
    return row
