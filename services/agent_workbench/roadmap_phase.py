"""Immutable Roadmap artifact persistence in a caller-owned transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from sqlmodel import Session, col, select

from models.workflow import (
    BacklogArtifact,
    RoadmapArtifact,
    RoadmapArtifactDecision,
)
from services.agent_workbench.backlog_phase import (
    _backlog_lineage_nodes,
    _require_current_root,
)
from services.planning_artifact_content import (
    load_stored_backlog_planning_content,
    load_stored_roadmap_planning_content,
    validate_roadmap_planning_content,
)
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageError,
    next_artifact_version,
    select_current_accepted_artifact,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from services.planning_lineage import Decision
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


def _required_id(value: int | None, *, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise ValueError(message)
    return value


def _roadmap_lineage_nodes(
    session: Session,
    *,
    project_id: int,
) -> tuple[ArtifactLineageNode, ...]:
    artifacts = session.exec(
        select(RoadmapArtifact).where(col(RoadmapArtifact.project_id) == project_id)
    ).all()
    decisions = session.exec(
        select(RoadmapArtifactDecision).where(
            col(RoadmapArtifactDecision.project_id) == project_id
        )
    ).all()
    artifacts_by_id = {
        _required_id(row.roadmap_artifact_id, label="Roadmap artifact"): row
        for row in artifacts
    }
    decisions_by_artifact: dict[int, Decision] = {}
    for decision in decisions:
        artifact = artifacts_by_id.get(decision.roadmap_artifact_id)
        if (
            artifact is None
            or artifact.content_fingerprint != decision.artifact_fingerprint
            or decision.roadmap_artifact_id in decisions_by_artifact
            or decision.decision not in {"accepted", "feedback", "rejected"}
        ):
            message = "Stored Roadmap decision lineage is invalid."
            raise ValueError(message)
        decisions_by_artifact[decision.roadmap_artifact_id] = cast(
            "Decision", decision.decision
        )
    return tuple(
        ArtifactLineageNode(
            artifact_id=artifact_id,
            chain_key=(
                row.project_id,
                row.backlog_artifact_id,
                row.backlog_artifact_fingerprint,
            ),
            version_number=row.version_number,
            supersedes_artifact_id=row.supersedes_roadmap_artifact_id,
            decision=decisions_by_artifact.get(artifact_id),
        )
        for artifact_id, row in artifacts_by_id.items()
    )


def _current_accepted_backlog(
    session: Session,
    *,
    project_id: int,
    backlog_artifact_id: int,
    backlog_artifact_fingerprint: str,
) -> tuple[BacklogArtifact, tuple[str, ...]]:
    parent = session.exec(
        select(BacklogArtifact).where(
            col(BacklogArtifact.project_id) == project_id,
            col(BacklogArtifact.backlog_artifact_id) == backlog_artifact_id,
            col(BacklogArtifact.content_fingerprint) == backlog_artifact_fingerprint,
        )
    ).one_or_none()
    if parent is None:
        message = "Roadmap source Backlog does not match one exact artifact."
        raise ValueError(message)
    specification = _require_current_root(
        session,
        project_id=project_id,
        spec_version_id=parent.spec_version_id,
        spec_hash=parent.spec_hash,
        product_goal_artifact_id=parent.product_goal_artifact_id,
        product_goal_fingerprint=parent.product_goal_fingerprint,
    )
    _canonical_content, validated = load_stored_backlog_planning_content(
        parent.canonical_content_json,
        expected_fingerprint=parent.content_fingerprint,
        specification=specification,
    )
    key = (
        project_id,
        parent.product_goal_artifact_id,
        parent.product_goal_fingerprint,
        parent.spec_version_id,
        parent.spec_hash,
    )
    try:
        current = select_current_accepted_artifact(
            _backlog_lineage_nodes(session, project_id=project_id),
            chain_key=key,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    if current.artifact_id != backlog_artifact_id:
        message = "Roadmap requires the sole current accepted Backlog parent."
        raise ValueError(message)
    return parent, tuple(item.backlog_item_id for item in validated.backlog_items)


def record_roadmap_draft_in_session(
    session: Session,
    *,
    inputs: RecordRoadmapDraftInput,
) -> RoadmapArtifact:
    """Validate and add one immutable Roadmap artifact without committing."""
    _parent, parent_item_ids = _current_accepted_backlog(
        session,
        project_id=inputs.project_id,
        backlog_artifact_id=inputs.backlog_artifact_id,
        backlog_artifact_fingerprint=inputs.backlog_artifact_fingerprint,
    )
    validate_roadmap_planning_content(
        canonical_content=inputs.canonical_content,
        content_fingerprint=inputs.content_fingerprint,
        parent_backlog_item_ids=parent_item_ids,
    )
    chain_key = (
        inputs.project_id,
        inputs.backlog_artifact_id,
        inputs.backlog_artifact_fingerprint,
    )
    try:
        version_number = next_artifact_version(
            _roadmap_lineage_nodes(session, project_id=inputs.project_id),
            chain_key=chain_key,
            supersedes_id=inputs.supersedes_roadmap_artifact_id,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    row = RoadmapArtifact(
        project_id=inputs.project_id,
        backlog_artifact_id=inputs.backlog_artifact_id,
        backlog_artifact_fingerprint=inputs.backlog_artifact_fingerprint,
        version_number=version_number,
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
    """Append one terminal Roadmap decision and mutate no other durable record."""
    if inputs.decision not in {"accepted", "rejected", "feedback"}:
        message = "Roadmap decision is invalid."
        raise ValueError(message)
    artifact = inputs.artifact
    artifact_id = _required_id(artifact.roadmap_artifact_id, label="Roadmap artifact")
    stored = session.exec(
        select(RoadmapArtifact).where(
            col(RoadmapArtifact.project_id) == artifact.project_id,
            col(RoadmapArtifact.roadmap_artifact_id) == artifact_id,
            col(RoadmapArtifact.content_fingerprint) == artifact.content_fingerprint,
        )
    ).one_or_none()
    if stored is None:
        message = "Roadmap decision does not match one exact artifact."
        raise ValueError(message)
    existing = session.exec(
        select(RoadmapArtifactDecision).where(
            col(RoadmapArtifactDecision.project_id) == artifact.project_id,
            col(RoadmapArtifactDecision.roadmap_artifact_id) == artifact_id,
        )
    ).one_or_none()
    if existing is not None:
        message = "Roadmap artifact already has a terminal decision."
        raise ValueError(message)
    _parent, parent_item_ids = _current_accepted_backlog(
        session,
        project_id=stored.project_id,
        backlog_artifact_id=stored.backlog_artifact_id,
        backlog_artifact_fingerprint=stored.backlog_artifact_fingerprint,
    )
    _canonical_content, _content = load_stored_roadmap_planning_content(
        stored.canonical_content_json,
        expected_fingerprint=stored.content_fingerprint,
        parent_backlog_item_ids=parent_item_ids,
    )
    key = (
        stored.project_id,
        stored.backlog_artifact_id,
        stored.backlog_artifact_fingerprint,
    )
    try:
        next_artifact_version(
            _roadmap_lineage_nodes(session, project_id=stored.project_id),
            chain_key=key,
            supersedes_id=artifact_id,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    row = RoadmapArtifactDecision(
        project_id=stored.project_id,
        roadmap_artifact_id=artifact_id,
        artifact_fingerprint=stored.content_fingerprint,
        decision=inputs.decision,
        rationale=inputs.rationale,
        reviewer=inputs.reviewer,
        idempotency_key=inputs.idempotency_key,
        decided_at=inputs.decided_at,
    )
    session.add(row)
    session.flush()
    return row


__all__ = [
    "RecordRoadmapDecisionInput",
    "RecordRoadmapDraftInput",
    "record_roadmap_decision_in_session",
    "record_roadmap_draft_in_session",
]
