"""Provider-free transactional guard tests for Product Goal requests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session

from workflow.contracts import NodeCategory, NodeDecision, RecommendationKind
from workflow.handlers.product_goal import execute_decide_product_goal_review
from workflow.requests.product_goal import DecideProductGoalReview

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_feedback_requires_a_non_blank_rationale(engine: Engine) -> None:
    """The transactional handler rejects semantic feedback without a rationale."""
    request = DecideProductGoalReview(
        project_id=1,
        graph_version="test",
        fact_fingerprint="facts",
        decision_fingerprint="decision",
        idempotency_key="review",
        actor="operator",
        product_goal_artifact_id=1,
        product_goal_fingerprint="goal",
        decision="feedback",
        rationale=" ",
    )
    decision = NodeDecision(
        node_id="goal.review",
        child_graph_id="product_goal",
        request_kind=request.kind,
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="PRODUCT_GOAL_REVIEW_REQUIRED",
        decision_fingerprint="decision",
    )
    with Session(engine) as session:
        result = execute_decide_product_goal_review(
            session,
            request,
            decision,
            datetime(2026, 8, 5, tzinfo=UTC),
        )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "WORKFLOW_FACT_CONFLICT"


def test_acceptance_rationale_is_optional_at_the_request_boundary() -> None:
    """Only rejection and feedback require rationale; acceptance may omit it."""
    request = DecideProductGoalReview(
        project_id=1,
        graph_version="test",
        fact_fingerprint="facts",
        decision_fingerprint="decision",
        idempotency_key="accept",
        actor="operator",
        product_goal_artifact_id=1,
        product_goal_fingerprint="goal",
        decision="accepted",
    )

    assert request.rationale == ""
