"""Pure authority child-graph state matrix tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from workflow.contracts import NodeCategory, NodeDecision, RecommendationKind
from workflow.definitions.authority import authority_graph
from workflow.facts import (
    AuthorityFact,
    AuthorityFeedbackFact,
    NodeAttemptFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecVersionFact,
    WorkflowFactSnapshot,
)

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
PROJECT_ID = 9
SPEC_ID = 101
SPEC_HASH = "sha256:current-spec"
AUTHORITY_ID = 201
AUTHORITY_FINGERPRINT = "sha256:current-authority"
COMPILE_INSTANCE_KEY = f"spec:{SPEC_ID}:{SPEC_HASH}"


def _snapshot(
    *,
    specs: tuple[SpecVersionFact, ...] | None = None,
    authorities: tuple[AuthorityFact, ...] = (),
    decisions: tuple[ReviewDecisionFact, ...] = (),
    feedback: tuple[AuthorityFeedbackFact, ...] = (),
    attempts: tuple[NodeAttemptFact, ...] = (),
) -> WorkflowFactSnapshot:
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Authority graph",
            created_at=EVALUATED_AT,
        ),
        spec_versions=(
            SpecVersionFact(
                spec_version_id=SPEC_ID,
                spec_hash=SPEC_HASH,
                status="approved",
                approved_at=EVALUATED_AT,
                source_specification_candidate_id=1,
                source_vision_artifact_id=1,
                source_vision_fingerprint="sha256:vision",
                source_product_goal_artifact_id=1,
                source_product_goal_fingerprint="sha256:goal",
                source_discovery_artifact_id=1,
                source_discovery_fingerprint="sha256:discovery",
            ),
        )
        if specs is None
        else specs,
        authorities=authorities,
        review_decisions=decisions,
        authority_feedback=feedback,
        node_attempts=attempts,
    )


def _authority(*, status: str = "pending_review") -> AuthorityFact:
    return AuthorityFact.model_validate(
        {
            "authority_id": AUTHORITY_ID,
            "spec_version_id": SPEC_ID,
            "authority_fingerprint": AUTHORITY_FINGERPRINT,
            "status": status,
            "decided_at": None,
        }
    )


def _decision(decision: str, *, decision_id: int = 301) -> ReviewDecisionFact:
    return ReviewDecisionFact.model_validate(
        {
            "decision_id": decision_id,
            "artifact_type": "authority",
            "artifact_id": AUTHORITY_ID,
            "artifact_fingerprint": AUTHORITY_FINGERPRINT,
            "decision": decision,
            "decided_at": EVALUATED_AT,
        }
    )


def _attempt(*, outcome: str | None, lease_delta: timedelta) -> NodeAttemptFact:
    return NodeAttemptFact.model_validate(
        {
            "attempt_id": 401,
            "node_id": "authority.compile",
            "instance_key": COMPILE_INSTANCE_KEY,
            "graph_version": "agileforge.workflow.v2",
            "input_fingerprint": "sha256:input",
            "fact_fingerprint": "sha256:facts",
            "business_fact_fingerprint": "sha256:business",
            "decision_fingerprint": "sha256:decision",
            "attempt_fingerprint": "sha256:attempt",
            "model_id": "openrouter/openai/gpt-5.6-luna",
            "lease_expires_at": EVALUATED_AT + lease_delta,
            "outcome": outcome,
        }
    )


def _decision_for(snapshot: WorkflowFactSnapshot, node_id: str) -> NodeDecision:
    position = authority_graph().evaluate(snapshot, EVALUATED_AT)
    return next(item for item in position.decisions if item.node_id == node_id)


def test_registered_current_spec_exposes_compile() -> None:
    """A registered current spec exposes compile with exact identity facts."""
    position = authority_graph().evaluate(_snapshot(), EVALUATED_AT)

    assert position.available_nodes == ("authority.compile",)
    compile_decision = _decision_for(_snapshot(), "authority.compile")
    assert compile_decision.instance_key == COMPILE_INSTANCE_KEY
    assert compile_decision.fact_references[0].fact_id == str(SPEC_ID)
    assert compile_decision.fact_references[0].fingerprint == SPEC_HASH


def test_active_compile_attempt_waits_until_its_explicit_lease_expires() -> None:
    """An active attempt waits until its explicit lease boundary."""
    active = _decision_for(
        _snapshot(attempts=(_attempt(outcome=None, lease_delta=timedelta(minutes=5)),)),
        "authority.compile",
    )
    expired = _decision_for(
        _snapshot(attempts=(_attempt(outcome=None, lease_delta=timedelta(0)),)),
        "authority.compile",
    )

    assert active.category is NodeCategory.WAITING
    assert active.reason_code == "AUTHORITY_COMPILE_ACTIVE"
    assert active.valid_until == EVALUATED_AT + timedelta(minutes=5)
    assert expired.category is NodeCategory.AVAILABLE
    assert expired.recommendation_kind is RecommendationKind.RECOVERY


def test_compile_failure_exposes_recovery_compile() -> None:
    """A durable compile failure changes compile to a recovery action."""
    decision = _decision_for(
        _snapshot(attempts=(_attempt(outcome="failure", lease_delta=timedelta(0)),)),
        "authority.compile",
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.recommendation_kind is RecommendationKind.RECOVERY
    assert decision.reason_code == "AUTHORITY_COMPILE_FAILED"


def test_pending_authority_derives_human_review_waiting_from_facts() -> None:
    """A pending authority creates a factual human-review wait."""
    position = authority_graph().evaluate(
        _snapshot(authorities=(_authority(),)),
        EVALUATED_AT,
    )

    assert position.waiting_nodes == ("authority.review",)
    assert "authority.compile" not in position.available_nodes


def test_accepted_authority_does_not_expose_a_vision_boundary() -> None:
    """Accepted authority completes the isolated Authority graph."""
    position = authority_graph().evaluate(
        _snapshot(
            authorities=(_authority(status="accepted"),),
            decisions=(_decision("accepted"),),
        ),
        EVALUATED_AT,
    )

    assert "authority.compile" not in position.available_nodes
    assert "authority.review" not in position.waiting_nodes
    assert position.available_nodes == ()


def test_rejected_authority_routes_to_typed_feedback() -> None:
    """A rejected authority requires immutable feedback next."""
    position = authority_graph().evaluate(
        _snapshot(
            authorities=(_authority(status="rejected"),),
            decisions=(_decision("rejected"),),
        ),
        EVALUATED_AT,
    )

    assert position.available_nodes == ("authority.feedback",)


def test_recorded_feedback_routes_to_typed_repair() -> None:
    """Recorded feedback exposes repair for the exact rejected authority."""
    position = authority_graph().evaluate(
        _snapshot(
            authorities=(_authority(status="rejected"),),
            decisions=(_decision("rejected"),),
            feedback=(
                AuthorityFeedbackFact(
                    feedback_id=501,
                    source_authority_id=AUTHORITY_ID,
                    source_authority_fingerprint=AUTHORITY_FINGERPRINT,
                    feedback_fingerprint="sha256:feedback",
                    recorded_at=EVALUATED_AT,
                ),
            ),
        ),
        EVALUATED_AT,
    )

    assert position.available_nodes == ("authority.repair",)


def test_repair_candidate_returns_to_pending_review() -> None:
    """A replacement authority returns the shared graph to review waiting."""
    repaired = AuthorityFact(
        authority_id=AUTHORITY_ID + 1,
        spec_version_id=SPEC_ID,
        authority_fingerprint="sha256:repaired-authority",
        status="pending_review",
        decided_at=None,
    )
    position = authority_graph().evaluate(
        _snapshot(
            authorities=(_authority(status="rejected"), repaired),
            decisions=(_decision("rejected"),),
            feedback=(
                AuthorityFeedbackFact(
                    feedback_id=501,
                    source_authority_id=AUTHORITY_ID,
                    source_authority_fingerprint=AUTHORITY_FINGERPRINT,
                    feedback_fingerprint="sha256:feedback",
                    recorded_at=EVALUATED_AT,
                ),
            ),
        ),
        EVALUATED_AT,
    )

    assert position.waiting_nodes == ("authority.review",)


def test_new_current_spec_makes_historical_acceptance_stale_and_recompilable() -> None:
    """A new current spec invalidates the old executable authority gate."""
    old_spec = SpecVersionFact(
        spec_version_id=SPEC_ID,
        spec_hash=SPEC_HASH,
        status="superseded",
        approved_at=EVALUATED_AT,
        source_specification_candidate_id=1,
        source_vision_artifact_id=1,
        source_vision_fingerprint="sha256:vision",
        source_product_goal_artifact_id=1,
        source_product_goal_fingerprint="sha256:goal",
        source_discovery_artifact_id=1,
        source_discovery_fingerprint="sha256:discovery",
    )
    new_spec = SpecVersionFact(
        spec_version_id=SPEC_ID + 1,
        spec_hash="sha256:new-spec",
        status="approved",
        approved_at=EVALUATED_AT + timedelta(minutes=1),
        source_specification_candidate_id=2,
        source_vision_artifact_id=1,
        source_vision_fingerprint="sha256:vision",
        source_product_goal_artifact_id=1,
        source_product_goal_fingerprint="sha256:goal",
        source_discovery_artifact_id=1,
        source_discovery_fingerprint="sha256:discovery",
    )
    position = authority_graph().evaluate(
        _snapshot(
            specs=(old_spec, new_spec),
            authorities=(_authority(status="stale"),),
            decisions=(_decision("accepted"),),
        ),
        EVALUATED_AT,
    )

    assert position.available_nodes == ("authority.compile",)


def test_conflicting_terminal_decisions_fail_closed() -> None:
    """Conflicting terminal authority decisions invalidate every route."""
    position = authority_graph().evaluate(
        _snapshot(
            authorities=(_authority(status="accepted"),),
            decisions=(
                _decision("accepted", decision_id=301),
                _decision("rejected", decision_id=302),
            ),
        ),
        EVALUATED_AT,
    )

    assert position.invalid_nodes == (
        "authority.compile",
        "authority.review",
        "authority.feedback",
        "authority.repair",
    )
    assert all(
        decision.reason_code == "WORKFLOW_FACT_CONFLICT"
        for decision in position.decisions
    )
