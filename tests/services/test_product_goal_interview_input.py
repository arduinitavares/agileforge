"""Host-prepared Product Goal interview input tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from services.product_goal_interview_input import ProductGoalInterviewInputService
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
)
from workflow.facts import (
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
