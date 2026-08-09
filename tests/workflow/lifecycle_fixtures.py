"""Shared provider-free persisted v2 lifecycle fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    SpecificationCandidate,
    SpecificationDecision,
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
)
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    canonical_stored_json_hash,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
)


@dataclass(frozen=True)
class PersistedSpecificationLineage:
    """Durable identities for one accepted v2 specification chain."""

    vision_artifact_id: int
    vision_fingerprint: str
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    discovery_artifact_id: int
    discovery_fingerprint: str
    specification_candidate_id: int
    specification_fingerprint: str
    spec: SpecRegistry


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
    started_at: datetime,
) -> WorkflowNodeAttempt:
    attempt_fingerprint = f"sha256:fixture-{node_id}-{project_id}-{ordinal}"
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id=node_id,
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=f"sha256:fixture-facts-{project_id}-{ordinal}",
        business_fact_fingerprint=f"sha256:fixture-business-{project_id}-{ordinal}",
        decision_fingerprint=f"sha256:fixture-decision-{project_id}-{ordinal}",
        normalized_input_json="{}",
        input_fingerprint=f"sha256:fixture-input-{project_id}-{ordinal}",
        model_id="fake/product-definition",
        execution_settings_json="{}",
        idempotency_key=f"fixture-{node_id}-{project_id}-{ordinal}",
        actor="fixture",
        correlation_id=None,
        started_at=started_at,
        lease_expires_at=started_at + timedelta(minutes=1),
        attempt_fingerprint=attempt_fingerprint,
    )
    session.add(attempt)
    session.flush()
    _required(attempt.workflow_node_attempt_id, f"{node_id} attempt")
    return attempt


def _complete_attempt(
    session: Session,
    *,
    project_id: int,
    attempt: WorkflowNodeAttempt,
    recorded_at: datetime,
) -> None:
    attempt_id = _required(attempt.workflow_node_attempt_id, "workflow attempt")
    session.add(
        WorkflowNodeAttemptOutcome(
            project_id=project_id,
            workflow_node_attempt_id=attempt_id,
            status="success",
            output_fingerprint=f"sha256:fixture-output-{attempt_id}",
            output_json="{}",
            recorded_at=recorded_at,
        )
    )


def _seed_accepted_vision_and_goal(
    session: Session,
    *,
    project_id: int,
    recorded_at: datetime,
) -> tuple[VisionArtifact, ProductGoalArtifact]:
    vision = session.exec(
        select(VisionArtifact)
        .where(col(VisionArtifact.project_id) == project_id)
        .order_by(col(VisionArtifact.vision_artifact_id).desc())
    ).first()
    goal = session.exec(
        select(ProductGoalArtifact)
        .where(col(ProductGoalArtifact.project_id) == project_id)
        .order_by(col(ProductGoalArtifact.product_goal_artifact_id).desc())
    ).first()
    if vision is not None and goal is not None:
        return vision, goal
    if vision is not None or goal is not None:
        message = "Partial product-definition fixture lineage exists."
        raise AssertionError(message)

    vision_components: JsonObject = {"purpose": "exercise durable lifecycle"}
    vision_statement = "Deliver one verified product increment."
    vision_attempt = _attempt(
        session,
        project_id=project_id,
        node_id="vision.interview",
        ordinal=1,
        started_at=recorded_at,
    )
    vision_turn = VisionInterviewTurn(
        project_id=project_id,
        mode="initial",
        turn_number=1,
        revision_intent_id=None,
        prior_turn_id=None,
        user_text="Define the product Vision.",
        components_json=canonical_json(vision_components),
        vision_statement=vision_statement,
        is_complete=True,
        clarifying_questions_json="[]",
        output_fingerprint=canonical_hash(
            {
                "components_json": vision_components,
                "vision_statement": vision_statement,
                "is_complete": True,
                "clarifying_questions_json": [],
            }
        ),
        workflow_node_attempt_id=_required(
            vision_attempt.workflow_node_attempt_id,
            "Vision attempt",
        ),
        attempt_fingerprint=vision_attempt.attempt_fingerprint,
        recorded_at=recorded_at + timedelta(seconds=1),
    )
    session.add(vision_turn)
    session.flush()
    vision = VisionArtifact(
        project_id=project_id,
        version_number=1,
        components_json=canonical_json(vision_components),
        statement=vision_statement,
        content_fingerprint=canonical_hash(
            {"components": vision_components, "statement": vision_statement}
        ),
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=_required(
            vision_turn.vision_interview_turn_id,
            "Vision turn",
        ),
        created_by="fixture",
        created_at=recorded_at + timedelta(seconds=2),
    )
    session.add(vision)
    session.flush()
    vision_id = _required(vision.vision_artifact_id, "Vision")
    session.add(
        VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=vision_id,
            artifact_fingerprint=vision.content_fingerprint,
            decision="accepted",
            rationale="Accepted for fixture delivery.",
            reviewer="fixture",
            idempotency_key=f"fixture-vision-accepted-{project_id}",
            decided_at=recorded_at + timedelta(seconds=3),
        )
    )
    _complete_attempt(
        session,
        project_id=project_id,
        attempt=vision_attempt,
        recorded_at=recorded_at + timedelta(seconds=3),
    )

    goal_components: JsonObject = {
        "valuable_future_state": "One increment is accepted",
        "beneficiary": "Operators",
        "value": "Predictable delivery",
        "success_signals": ["The lifecycle reaches triage"],
        "boundaries": ["No provider calls"],
    }
    goal_statement = "Complete one accepted increment through triage."
    goal_attempt = _attempt(
        session,
        project_id=project_id,
        node_id="goal.interview",
        ordinal=2,
        started_at=recorded_at + timedelta(seconds=4),
    )
    goal_turn = ProductGoalInterviewTurn(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=1,
        revision_number=1,
        prior_turn_id=None,
        user_text="Define the Product Goal.",
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
            "Goal attempt",
        ),
        attempt_fingerprint=goal_attempt.attempt_fingerprint,
        recorded_at=recorded_at + timedelta(seconds=5),
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
            "Goal turn",
        ),
        created_by="fixture",
        created_at=recorded_at + timedelta(seconds=6),
    )
    session.add(goal)
    session.flush()
    session.add(
        ProductGoalArtifactDecision(
            project_id=project_id,
            product_goal_artifact_id=_required(
                goal.product_goal_artifact_id,
                "Product Goal",
            ),
            artifact_fingerprint=goal.content_fingerprint,
            decision="accepted",
            rationale="Accepted for fixture delivery.",
            reviewer="fixture",
            idempotency_key=f"fixture-goal-accepted-{project_id}",
            decided_at=recorded_at + timedelta(seconds=7),
        )
    )
    _complete_attempt(
        session,
        project_id=project_id,
        attempt=goal_attempt,
        recorded_at=recorded_at + timedelta(seconds=7),
    )
    return vision, goal


def seed_accepted_specification(
    session: Session,
    *,
    project_id: int,
    content: str,
    content_ref: str | None = None,
    recorded_at: datetime | None = None,
) -> PersistedSpecificationLineage:
    """Persist a complete accepted v2 specification under the active Goal."""
    parsed: object = json.loads(content)
    if not isinstance(parsed, dict):
        message = "Specification fixture content must be a JSON object."
        raise TypeError(message)
    canonical_content = canonical_json(parsed)
    specification_fingerprint = canonical_stored_json_hash(canonical_content)
    existing_specs = session.exec(
        select(SpecRegistry)
        .where(col(SpecRegistry.project_id) == project_id)
        .order_by(col(SpecRegistry.spec_version_id))
    ).all()
    ordinal = len(existing_specs) + 1
    base_time = recorded_at or datetime.now(UTC) + timedelta(minutes=ordinal * 10)
    vision, goal = _seed_accepted_vision_and_goal(
        session,
        project_id=project_id,
        recorded_at=base_time,
    )
    vision_id = _required(vision.vision_artifact_id, "Vision")
    goal_id = _required(goal.product_goal_artifact_id, "Product Goal")

    previous_discovery = session.exec(
        select(DiscoveryArtifact)
        .where(col(DiscoveryArtifact.project_id) == project_id)
        .order_by(col(DiscoveryArtifact.discovery_artifact_id).desc())
    ).first()
    discovery_content: JsonObject = {
        "increment": ordinal,
        "specification_fingerprint": specification_fingerprint,
    }
    discovery = DiscoveryArtifact(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        canonical_content_json=canonical_json(discovery_content),
        content_fingerprint=canonical_hash(discovery_content),
        content_ref=None,
        producer="fixture",
        supersedes_discovery_artifact_id=(
            None
            if previous_discovery is None
            else previous_discovery.discovery_artifact_id
        ),
        recorded_by="fixture",
        recorded_at=base_time + timedelta(seconds=8),
    )
    session.add(discovery)
    session.flush()
    discovery_id = _required(discovery.discovery_artifact_id, "discovery")

    current_spec = next(
        (row for row in existing_specs if row.status == "approved"),
        None,
    )
    candidate = SpecificationCandidate(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        discovery_artifact_id=discovery_id,
        discovery_fingerprint=discovery.content_fingerprint,
        base_spec_version_id=(
            None if current_spec is None else current_spec.spec_version_id
        ),
        base_spec_hash=None if current_spec is None else current_spec.spec_hash,
        canonical_content_json=canonical_content,
        content_fingerprint=specification_fingerprint,
        content_ref=content_ref,
        supersedes_specification_candidate_id=(
            None
            if current_spec is None
            else current_spec.source_specification_candidate_id
        ),
        recorded_by="fixture",
        recorded_at=base_time + timedelta(seconds=9),
    )
    session.add(candidate)
    session.flush()
    candidate_id = _required(candidate.specification_candidate_id, "specification")
    session.add(
        SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=candidate_id,
            artifact_fingerprint=specification_fingerprint,
            decision="accepted",
            rationale="Accepted for fixture delivery.",
            reviewer="fixture",
            idempotency_key=f"fixture-specification-accepted-{project_id}-{ordinal}",
            decided_at=base_time + timedelta(seconds=10),
        )
    )
    if current_spec is not None:
        current_spec.status = "superseded"
        session.add(current_spec)
    spec = SpecRegistry(
        project_id=project_id,
        spec_hash=specification_fingerprint,
        content=canonical_content,
        content_ref=content_ref,
        status="approved",
        approved_at=base_time + timedelta(seconds=10),
        approved_by="fixture",
        approval_notes="Accepted for fixture delivery.",
        source_specification_candidate_id=candidate_id,
        source_vision_artifact_id=vision_id,
        source_vision_fingerprint=vision.content_fingerprint,
        source_product_goal_artifact_id=goal_id,
        source_product_goal_fingerprint=goal.content_fingerprint,
        source_discovery_artifact_id=discovery_id,
        source_discovery_fingerprint=discovery.content_fingerprint,
        supersedes_spec_version_id=(
            None if current_spec is None else current_spec.spec_version_id
        ),
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return PersistedSpecificationLineage(
        vision_artifact_id=vision_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        discovery_artifact_id=discovery_id,
        discovery_fingerprint=discovery.content_fingerprint,
        specification_candidate_id=candidate_id,
        specification_fingerprint=specification_fingerprint,
        spec=spec,
    )


__all__ = ["PersistedSpecificationLineage", "seed_accepted_specification"]
