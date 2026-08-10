"""Backlog delivery-lineage graph tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from services.contracts.backlog import InputSchema as BacklogInput
from services.contracts.roadmap import RoadmapBuilderInput
from workflow.contracts import NodeCategory, NodeDecision, WorkflowPosition
from workflow.definitions.root import project_graph
from workflow.facts import (
    AuthorityFact,
    NodeAttemptFact,
    PhaseArtifactFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecVersionFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
PROJECT_ID = 10
SPEC_ID = 101
AUTHORITY_ID = 201
AUTHORITY_FINGERPRINT = "sha256:authority-current"
GOAL_ID = 301
GOAL_FINGERPRINT = "sha256:goal-current"
PRODUCT_VISION_ID = 401
PRODUCT_VISION_FINGERPRINT = "sha256:product-vision-current"
BACKLOG_ID = 501
BACKLOG_FINGERPRINT = "sha256:backlog-current"


def _authority(
    *,
    authority_id: int = AUTHORITY_ID,
    spec_version_id: int = SPEC_ID,
    fingerprint: str = AUTHORITY_FINGERPRINT,
    status: str = "accepted",
) -> AuthorityFact:
    return AuthorityFact.model_validate(
        {
            "authority_id": authority_id,
            "spec_version_id": spec_version_id,
            "authority_fingerprint": fingerprint,
            "status": status,
            "decided_at": EVALUATED_AT,
        }
    )


def _artifact(
    *,
    status: str,
    authority_id: int = AUTHORITY_ID,
    authority_fingerprint: str = AUTHORITY_FINGERPRINT,
    goal_id: int = GOAL_ID,
    goal_fingerprint: str = GOAL_FINGERPRINT,
) -> PhaseArtifactFact:
    return PhaseArtifactFact.model_validate(
        {
            "artifact_type": "backlog",
            "artifact_id": BACKLOG_ID,
            "artifact_fingerprint": BACKLOG_FINGERPRINT,
            "authority_id": authority_id,
            "authority_fingerprint": authority_fingerprint,
            "product_goal_artifact_id": goal_id,
            "product_goal_fingerprint": goal_fingerprint,
            "status": status,
        }
    )


def _snapshot(
    *,
    authorities: tuple[AuthorityFact, ...] | None = None,
    backlog: PhaseArtifactFact | None = None,
    active_goal: bool = True,
    attempts: tuple[NodeAttemptFact, ...] = (),
) -> WorkflowFactSnapshot:
    vision = VisionArtifactFact(
        vision_artifact_id=PRODUCT_VISION_ID,
        version_number=1,
        components={},
        statement="Reliable delivery decisions.",
        content_fingerprint=PRODUCT_VISION_FINGERPRINT,
        vision_evidence_snapshot_id=1,
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    goal = ProductGoalArtifactFact(
        product_goal_artifact_id=GOAL_ID,
        vision_artifact_id=PRODUCT_VISION_ID,
        vision_fingerprint=PRODUCT_VISION_FINGERPRINT,
        goal_number=1,
        revision_number=1,
        statement="Deliver one durable workflow lineage.",
        content_fingerprint=GOAL_FINGERPRINT,
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    current_authorities = authorities if authorities is not None else (_authority(),)
    authority_decisions = tuple(
        ReviewDecisionFact(
            decision_id=item.authority_id,
            artifact_type="authority",
            artifact_id=item.authority_id,
            artifact_fingerprint=item.authority_fingerprint,
            decision="accepted",
            decided_at=EVALUATED_AT,
        )
        for item in current_authorities
        if item.status == "accepted"
    )
    review_decisions = authority_decisions + (
        (
            ReviewDecisionFact(
                decision_id=BACKLOG_ID,
                artifact_type="backlog",
                artifact_id=BACKLOG_ID,
                artifact_fingerprint=BACKLOG_FINGERPRINT,
                decision=cast(
                    "Literal['accepted', 'rejected', 'feedback']", backlog.status
                ),
                decided_at=EVALUATED_AT,
            ),
        )
        if backlog is not None
        and backlog.status in {"accepted", "rejected", "feedback"}
        else ()
    )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Backlog lineage",
            created_at=EVALUATED_AT,
        ),
        spec_versions=(
            SpecVersionFact(
                spec_version_id=SPEC_ID,
                spec_hash="sha256:spec-current",
                status="approved",
                approved_at=EVALUATED_AT,
                source_specification_candidate_id=1,
                source_vision_artifact_id=PRODUCT_VISION_ID,
                source_vision_fingerprint=PRODUCT_VISION_FINGERPRINT,
                source_product_goal_artifact_id=GOAL_ID,
                source_product_goal_fingerprint=GOAL_FINGERPRINT,
                source_discovery_artifact_id=1,
                source_discovery_fingerprint="sha256:discovery",
            ),
        ),
        authorities=current_authorities,
        phase_artifacts=() if backlog is None else (backlog,),
        review_decisions=review_decisions,
        vision_artifacts=(vision,),
        vision_artifact_decisions=(
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=PRODUCT_VISION_ID,
                vision_artifact_id=PRODUCT_VISION_ID,
                artifact_fingerprint=PRODUCT_VISION_FINGERPRINT,
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="vision-accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
        product_goal_artifacts=(goal,) if active_goal else (),
        product_goal_artifact_decisions=(
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=GOAL_ID,
                product_goal_artifact_id=GOAL_ID,
                artifact_fingerprint=GOAL_FINGERPRINT,
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="goal-accepted",
                decided_at=EVALUATED_AT,
            ),
        )
        if active_goal
        else (),
        node_attempts=attempts,
    )


def _position(snapshot: WorkflowFactSnapshot) -> WorkflowPosition:
    return project_graph().evaluate(snapshot, EVALUATED_AT)


def _decision(snapshot: WorkflowFactSnapshot, node_id: str) -> NodeDecision:
    return next(
        item for item in _position(snapshot).decisions if item.node_id == node_id
    )


def test_backlog_requires_an_active_accepted_goal() -> None:
    """Authority alone cannot expose Backlog generation."""
    position = _position(_snapshot(active_goal=False))

    assert "backlog.generate" in position.blocked_nodes
    assert _decision(_snapshot(active_goal=False), "backlog.generate").reason_code == (
        "ACCEPTED_PRODUCT_GOAL_REQUIRED"
    )


def test_backlog_generation_references_exact_goal_and_authority() -> None:
    """Generation has both immutable current parents in its graph decision."""
    decision = _decision(_snapshot(), "backlog.generate")

    assert decision.category is NodeCategory.AVAILABLE
    fact_references = {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in decision.fact_references
    }
    assert fact_references == {
        ("authority", str(AUTHORITY_ID), AUTHORITY_FINGERPRINT),
        ("product_goal", str(GOAL_ID), GOAL_FINGERPRINT),
    }


def test_backlog_attempt_waits_on_the_durable_generation_lease() -> None:
    """A retry cannot replace an active Goal/Authority-bound generation attempt."""
    attempt = NodeAttemptFact(
        attempt_id=1,
        node_id="backlog.generate",
        instance_key=None,
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint="sha256:business",
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="test",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=None,
    )

    decision = _decision(_snapshot(attempts=(attempt,)), "backlog.generate")

    assert decision.category is NodeCategory.WAITING
    assert decision.reason_code == "BACKLOG_GENERATION_ACTIVE"


def test_historical_goal_backlog_is_not_current_delivery_state() -> None:
    """Leave old Backlogs immutable while excluding them from planning selection."""
    position = _position(
        _snapshot(
            backlog=_artifact(
                status="accepted",
                goal_id=GOAL_ID + 1,
                goal_fingerprint="sha256:goal-historical",
            )
        )
    )

    assert "backlog.generate" in position.available_nodes
    assert "planning.roadmap.generate" in position.blocked_nodes


def test_authority_replacement_requires_fresh_backlog() -> None:
    """An old-Authority Backlog never produces a reconciliation route."""
    replacement_authority = _authority(
        authority_id=AUTHORITY_ID + 1,
        spec_version_id=SPEC_ID,
        fingerprint="sha256:authority-replacement",
    )
    position = _position(
        _snapshot(
            authorities=(replacement_authority,),
            backlog=_artifact(status="accepted"),
        )
    )

    assert "backlog.generate" in position.available_nodes
    assert "backlog.reconcile" not in {item.node_id for item in position.decisions}


def test_accepted_current_backlog_unlocks_roadmap_with_goal_lineage() -> None:
    """Planning inherits the exact active Goal through the selected Backlog."""
    decision = _decision(
        _snapshot(backlog=_artifact(status="accepted")),
        "planning.roadmap.generate",
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert {item.fact_type for item in decision.fact_references} == {
        "authority",
        "backlog",
        "product_goal",
    }


def test_agent_inputs_reject_unknown_context() -> None:
    """Agents receive only their declared product-delivery context."""
    with pytest.raises(ValidationError):
        BacklogInput.model_validate(
            {
                "product_vision_statement": "Vision",
                "product_goal_statement": "Goal",
                "technical_spec": "Spec",
                "compiled_authority": "Authority",
                "prior_backlog_state": "NO_HISTORY",
                "user_input": None,
                "unknown_control": "invalid",
            }
        )
    with pytest.raises(ValidationError):
        RoadmapBuilderInput.model_validate(
            {
                "backlog_items": [],
                "product_vision": "Vision",
                "technical_spec": "Spec",
                "compiled_authority": "Authority",
                "unknown_control": {"invalid": True},
            }
        )
