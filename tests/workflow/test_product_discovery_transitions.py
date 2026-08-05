"""Provider-free transactional guard tests for specification decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session

from workflow.contracts import NodeCategory, NodeDecision, RecommendationKind
from workflow.handlers.product_discovery import execute_decide_specification
from workflow.requests.product_discovery import DecideSpecification

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_specification_feedback_requires_exact_pending_candidate(
    engine: Engine,
) -> None:
    """A stale candidate identity cannot insert a durable specification review."""
    request = DecideSpecification(
        project_id=1,
        graph_version="test",
        fact_fingerprint="facts",
        decision_fingerprint="decision",
        idempotency_key="review",
        actor="operator",
        specification_candidate_id=1,
        specification_fingerprint="candidate",
        decision="feedback",
        rationale="Needs a measurable constraint.",
    )
    decision = NodeDecision(
        node_id="specification.review",
        child_graph_id="product_discovery",
        request_kind=request.kind,
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPECIFICATION_REVIEW_REQUIRED",
        decision_fingerprint="decision",
    )
    with Session(engine) as session:
        result = execute_decide_specification(
            session,
            request,
            decision,
            datetime(2026, 8, 5, tzinfo=UTC),
        )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "WORKFLOW_FACT_CONFLICT"


def test_specification_acceptance_may_omit_rationale() -> None:
    """The append-only acceptance record allows an empty optional rationale."""
    request = DecideSpecification(
        project_id=1,
        graph_version="test",
        fact_fingerprint="facts",
        decision_fingerprint="decision",
        idempotency_key="accept",
        actor="operator",
        specification_candidate_id=1,
        specification_fingerprint="candidate",
        decision="accepted",
    )

    assert request.rationale == ""
