"""Provider-free transactional guard tests for specification decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import Project
from models.product_definition import DiscoveryArtifact
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
)
from workflow.handlers.product_discovery import (
    execute_decide_specification,
    execute_record_discovery_artifact,
)
from workflow.requests.product_discovery import (
    DecideSpecification,
    RecordDiscoveryArtifact,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EXPECTED_DISCOVERY_COUNT = 2


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


def test_reopened_discovery_persists_exact_same_goal_supersession(
    engine: Engine,
) -> None:
    """The reopened increment appends one discovery under unchanged Goal lineage."""
    recorded_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
    with Session(engine) as session:
        project = Project(name="Discovery increment")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content='{"increment":1}',
            recorded_at=recorded_at,
        )
        request = RecordDiscoveryArtifact(
            project_id=project.project_id,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            idempotency_key="discovery-increment-2",
            actor="operator@example.com",
            canonical_content={"increment": 2},
            content_ref=None,
        )
        decision = NodeDecision(
            node_id="discovery.record",
            child_graph_id="product_discovery",
            request_kind=request.kind,
            category=NodeCategory.AVAILABLE,
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
            reason_code="DISCOVERY_INCREMENT_AVAILABLE",
            fact_references=(
                FactReference(
                    fact_type="vision",
                    fact_id=str(lineage.vision_artifact_id),
                    fingerprint=lineage.vision_fingerprint,
                ),
                FactReference(
                    fact_type="product_goal",
                    fact_id=str(lineage.product_goal_artifact_id),
                    fingerprint=lineage.product_goal_fingerprint,
                ),
                FactReference(
                    fact_type="discovery",
                    fact_id=str(lineage.discovery_artifact_id),
                    fingerprint=lineage.discovery_fingerprint,
                ),
            ),
            decision_fingerprint="sha256:decision",
        )

        result = execute_record_discovery_artifact(
            session,
            request,
            decision,
            recorded_at + timedelta(minutes=5),
        )
        session.commit()
        rows = session.exec(
            select(DiscoveryArtifact).order_by(
                col(DiscoveryArtifact.discovery_artifact_id)
            )
        ).all()
        assert result.ok
        assert len(rows) == EXPECTED_DISCOVERY_COUNT
        replacement = rows[1]
        assert (
            replacement.supersedes_discovery_artifact_id
            == lineage.discovery_artifact_id
        )
        assert replacement.vision_artifact_id == lineage.vision_artifact_id
        assert replacement.vision_fingerprint == lineage.vision_fingerprint
        assert (
            replacement.product_goal_artifact_id
            == lineage.product_goal_artifact_id
        )
        assert (
            replacement.product_goal_fingerprint
            == lineage.product_goal_fingerprint
        )
