"""Immutable Backlog artifact persistence in a caller-owned transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlmodel import Session, col, select

from models.core import Project
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalOutcome,
)
from models.specs import SpecRegistry
from models.workflow import BacklogArtifact, BacklogArtifactDecision
from services.planning_artifact_content import (
    load_stored_backlog_planning_content,
    validate_backlog_planning_content,
)
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageError,
    next_artifact_version,
)
from services.specs.accepted_specification import (
    AcceptedSpecification,
    require_current_accepted_specification,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from services.planning_lineage import Decision
    from workflow.contracts import JsonObject


def _required_id(value: int | None, *, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise ValueError(message)
    return value


def _require_current_root(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
    product_goal_artifact_id: int,
    product_goal_fingerprint: str,
) -> AcceptedSpecification:
    """Prove the exact current Specification and its still-active source Goal."""
    specification = require_current_accepted_specification(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
    )
    registry = session.get(SpecRegistry, spec_version_id)
    if registry is None or (
        registry.project_id,
        registry.spec_hash,
        registry.source_product_goal_artifact_id,
        registry.source_product_goal_fingerprint,
    ) != (
        project_id,
        spec_hash,
        product_goal_artifact_id,
        product_goal_fingerprint,
    ):
        message = "Backlog does not target the accepted Specification's Product Goal."
        raise ValueError(message)
    goal = session.exec(
        select(ProductGoalArtifact).where(
            col(ProductGoalArtifact.project_id) == project_id,
            col(ProductGoalArtifact.product_goal_artifact_id)
            == product_goal_artifact_id,
            col(ProductGoalArtifact.content_fingerprint) == product_goal_fingerprint,
        )
    ).one_or_none()
    goal_decision = session.exec(
        select(ProductGoalArtifactDecision).where(
            col(ProductGoalArtifactDecision.project_id) == project_id,
            col(ProductGoalArtifactDecision.product_goal_artifact_id)
            == product_goal_artifact_id,
            col(ProductGoalArtifactDecision.artifact_fingerprint)
            == product_goal_fingerprint,
            col(ProductGoalArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    outcome = session.exec(
        select(ProductGoalOutcome).where(
            col(ProductGoalOutcome.project_id) == project_id,
            col(ProductGoalOutcome.product_goal_artifact_id)
            == product_goal_artifact_id,
        )
    ).one_or_none()
    if goal is None or goal_decision is None or outcome is not None:
        message = "Backlog requires the exact active Product Goal."
        raise ValueError(message)
    return specification


def _backlog_lineage_nodes(
    session: Session,
    *,
    project_id: int,
) -> tuple[ArtifactLineageNode, ...]:
    artifacts = session.exec(
        select(BacklogArtifact).where(col(BacklogArtifact.project_id) == project_id)
    ).all()
    decisions = session.exec(
        select(BacklogArtifactDecision).where(
            col(BacklogArtifactDecision.project_id) == project_id
        )
    ).all()
    decisions_by_artifact: dict[int, Decision] = {}
    artifacts_by_id = {
        _required_id(row.backlog_artifact_id, label="Backlog artifact"): row
        for row in artifacts
    }
    for decision in decisions:
        artifact = artifacts_by_id.get(decision.backlog_artifact_id)
        if (
            artifact is None
            or artifact.content_fingerprint != decision.artifact_fingerprint
            or decision.backlog_artifact_id in decisions_by_artifact
            or decision.decision not in {"accepted", "feedback", "rejected"}
        ):
            message = "Stored Backlog decision lineage is invalid."
            raise ValueError(message)
        decisions_by_artifact[decision.backlog_artifact_id] = cast(
            "Decision", decision.decision
        )
    return tuple(
        ArtifactLineageNode(
            artifact_id=artifact_id,
            chain_key=(
                row.project_id,
                row.product_goal_artifact_id,
                row.product_goal_fingerprint,
                row.spec_version_id,
                row.spec_hash,
            ),
            version_number=row.version_number,
            supersedes_artifact_id=row.supersedes_backlog_artifact_id,
            decision=decisions_by_artifact.get(artifact_id),
        )
        for artifact_id, row in artifacts_by_id.items()
    )


def record_backlog_draft_in_session(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
    product_goal_artifact_id: int,
    product_goal_fingerprint: str,
    canonical_content: JsonObject,
    content_fingerprint: str,
    supersedes_backlog_artifact_id: int | None,
    artifact_id: int,
    actor: str,
    recorded_at: datetime,
) -> BacklogArtifact:
    """Validate and append one immutable Backlog artifact without committing."""
    if session.get(Project, project_id) is None:
        message = f"Project {project_id} not found."
        raise ValueError(message)
    specification = _require_current_root(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        product_goal_artifact_id=product_goal_artifact_id,
        product_goal_fingerprint=product_goal_fingerprint,
    )
    validate_backlog_planning_content(
        canonical_content=canonical_content,
        content_fingerprint=content_fingerprint,
        specification=specification,
    )
    chain_key = (
        project_id,
        product_goal_artifact_id,
        product_goal_fingerprint,
        spec_version_id,
        spec_hash,
    )
    try:
        version_number = next_artifact_version(
            _backlog_lineage_nodes(session, project_id=project_id),
            chain_key=chain_key,
            supersedes_id=supersedes_backlog_artifact_id,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    row = BacklogArtifact(
        backlog_artifact_id=artifact_id,
        project_id=project_id,
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        product_goal_artifact_id=product_goal_artifact_id,
        product_goal_fingerprint=product_goal_fingerprint,
        version_number=version_number,
        canonical_content_json=canonical_json(canonical_content),
        content_fingerprint=content_fingerprint,
        supersedes_backlog_artifact_id=supersedes_backlog_artifact_id,
        created_by=actor,
        created_at=recorded_at,
    )
    session.add(row)
    session.flush()
    return row


def record_backlog_decision_in_session(  # noqa: PLR0913
    session: Session,
    *,
    artifact: BacklogArtifact,
    decision: str,
    rationale: str,
    reviewer: str,
    idempotency_key: str,
    decided_at: datetime,
) -> BacklogArtifactDecision:
    """Append one terminal decision and mutate no artifact or operational row."""
    if decision not in {"accepted", "rejected", "feedback"}:
        message = "Backlog decision is invalid."
        raise ValueError(message)
    artifact_id = _required_id(artifact.backlog_artifact_id, label="Backlog artifact")
    stored = session.exec(
        select(BacklogArtifact).where(
            col(BacklogArtifact.project_id) == artifact.project_id,
            col(BacklogArtifact.backlog_artifact_id) == artifact_id,
            col(BacklogArtifact.content_fingerprint) == artifact.content_fingerprint,
        )
    ).one_or_none()
    if stored is None:
        message = "Backlog decision does not match one exact artifact."
        raise ValueError(message)
    _canonical_content, _content = load_stored_backlog_planning_content(
        stored.canonical_content_json,
        expected_fingerprint=stored.content_fingerprint,
        specification=_require_current_root(
            session,
            project_id=stored.project_id,
            spec_version_id=stored.spec_version_id,
            spec_hash=stored.spec_hash,
            product_goal_artifact_id=stored.product_goal_artifact_id,
            product_goal_fingerprint=stored.product_goal_fingerprint,
        ),
    )
    existing = session.exec(
        select(BacklogArtifactDecision).where(
            col(BacklogArtifactDecision.project_id) == artifact.project_id,
            col(BacklogArtifactDecision.backlog_artifact_id) == artifact_id,
        )
    ).one_or_none()
    if existing is not None:
        message = "Backlog artifact already has a terminal review decision."
        raise ValueError(message)
    nodes = _backlog_lineage_nodes(session, project_id=stored.project_id)
    chain_key = (
        stored.project_id,
        stored.product_goal_artifact_id,
        stored.product_goal_fingerprint,
        stored.spec_version_id,
        stored.spec_hash,
    )
    try:
        next_artifact_version(
            nodes,
            chain_key=chain_key,
            supersedes_id=artifact_id,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    row = BacklogArtifactDecision(
        project_id=stored.project_id,
        backlog_artifact_id=artifact_id,
        artifact_fingerprint=stored.content_fingerprint,
        decision=decision,
        rationale=rationale,
        reviewer=reviewer,
        idempotency_key=idempotency_key,
        decided_at=decided_at,
    )
    session.add(row)
    session.flush()
    return row


__all__ = ["record_backlog_decision_in_session", "record_backlog_draft_in_session"]
