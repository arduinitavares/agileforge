"""Host-prepared Product Goal interview input tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from services.contracts.product_goal import ProductGoalInterviewInput
from services.product_goal_interview_input import ProductGoalInterviewInputService
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
)
from workflow.facts import (
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProductGoalInterviewTurnFact,
    ProductGoalOutcomeFact,
    ProjectFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def test_builds_from_the_exact_accepted_vision(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator supplies text only; Vision identity is host-derived."""
    snapshot = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1,
            name="Goal input",
            origin="greenfield",
            created_at=NOW,
        ),
        vision_artifacts=(
            VisionArtifactFact(
                vision_artifact_id=2,
                version_number=1,
                components={},
                statement="Vision statement",
                content_fingerprint="vision",
                supersedes_vision_artifact_id=None,
                source_interview_turn_id=1,
                created_by="operator",
                created_at=NOW,
            ),
        ),
        vision_artifact_decisions=(
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=3,
                vision_artifact_id=2,
                artifact_fingerprint="vision",
                decision="accepted",
                rationale="",
                reviewer="operator",
                idempotency_key="key",
                decided_at=NOW,
            ),
        ),
    )
    monkeypatch.setattr(
        "services.product_goal_interview_input.WorkflowFactRepository.load",
        lambda _self, _project_id: snapshot,
    )
    decision = NodeDecision(
        node_id="goal.interview",
        child_graph_id="product_goal",
        request_kind="record_product_goal_interview_turn",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="PRODUCT_GOAL_INTERVIEW_REQUIRED",
        fact_references=(
            FactReference(fact_type="vision", fact_id="2", fingerprint="vision"),
        ),
        decision_fingerprint="decision",
    )

    payload = ProductGoalInterviewInputService(engine=engine).build(
        1, decision, "Need a faster onboarding flow"
    )

    assert payload["accepted_vision_statement"] == "Vision statement"
    assert payload["user_response"] == "Need a faster onboarding flow"


def test_resolved_goal_does_not_leak_components_into_next_goal(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new goal number starts fresh even when the accepted Vision is unchanged."""
    snapshot = _goal_snapshot(review="accepted", resolved=True)
    monkeypatch.setattr(
        "services.product_goal_interview_input.WorkflowFactRepository.load",
        lambda _self, _project_id: snapshot,
    )

    payload = ProductGoalInterviewInputService(engine=engine).build(
        1, _goal_interview_decision(), "A new objective"
    )

    assert payload["prior_components"] is None


def test_feedback_revision_reuses_only_its_exact_goal_components(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feedback resumes the reviewed Goal chain rather than any Vision turn."""
    snapshot = _goal_snapshot(review="feedback", resolved=False)
    monkeypatch.setattr(
        "services.product_goal_interview_input.WorkflowFactRepository.load",
        lambda _self, _project_id: snapshot,
    )

    payload = ProductGoalInterviewInputService(engine=engine).build(
        1, _goal_interview_decision(), "Refine it"
    )

    prepared = ProductGoalInterviewInput.model_validate(payload)
    assert prepared.prior_components is not None
    assert prepared.prior_components.beneficiary == "Operators"


def _goal_interview_decision() -> NodeDecision:
    return NodeDecision(
        node_id="goal.interview",
        child_graph_id="product_goal",
        request_kind="record_product_goal_interview_turn",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="PRODUCT_GOAL_INTERVIEW_REQUIRED",
        fact_references=(
            FactReference(fact_type="vision", fact_id="2", fingerprint="vision"),
        ),
        decision_fingerprint="decision",
    )


def _goal_snapshot(
    *, review: Literal["accepted", "rejected", "feedback"], resolved: bool
) -> WorkflowFactSnapshot:
    vision = VisionArtifactFact(
        vision_artifact_id=2,
        version_number=1,
        components={},
        statement="Vision statement",
        content_fingerprint="vision",
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator",
        created_at=NOW,
    )
    goal = ProductGoalArtifactFact(
        product_goal_artifact_id=4,
        vision_artifact_id=2,
        vision_fingerprint="vision",
        goal_number=1,
        revision_number=1,
        statement="Goal",
        content_fingerprint="goal",
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=3,
        created_by="operator",
        created_at=NOW,
    )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1, name="Goal input", origin="greenfield", created_at=NOW
        ),
        vision_artifacts=(vision,),
        vision_artifact_decisions=(
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=3,
                vision_artifact_id=2,
                artifact_fingerprint="vision",
                decision="accepted",
                rationale="",
                reviewer="operator",
                idempotency_key="vision",
                decided_at=NOW,
            ),
        ),
        product_goal_interview_turns=(
            ProductGoalInterviewTurnFact(
                product_goal_interview_turn_id=3,
                vision_artifact_id=2,
                vision_fingerprint="vision",
                goal_number=1,
                revision_number=1,
                prior_turn_id=None,
                user_text="Goal",
                components={
                    "valuable_future_state": "Trusted delivery",
                    "beneficiary": "Operators",
                    "value": "Predictability",
                    "success_signals": ["Measured"],
                    "boundaries": ["No features"],
                },
                goal_statement="Goal",
                is_complete=True,
                clarifying_questions=(),
                output_fingerprint="turn",
                workflow_node_attempt_id=1,
                attempt_fingerprint="attempt",
                recorded_at=NOW,
            ),
        ),
        product_goal_artifacts=(goal,),
        product_goal_artifact_decisions=(
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=5,
                product_goal_artifact_id=4,
                artifact_fingerprint="goal",
                decision=review,
                rationale="Revise",
                reviewer="operator",
                idempotency_key="goal",
                decided_at=NOW,
            ),
        ),
        product_goal_outcomes=(
            ()
            if not resolved
            else (
                ProductGoalOutcomeFact(
                    product_goal_outcome_id=6,
                    product_goal_artifact_id=4,
                    artifact_fingerprint="goal",
                    outcome="fulfilled",
                    rationale="Done",
                    decided_by="operator",
                    decided_at=NOW,
                ),
            )
        ),
    )
