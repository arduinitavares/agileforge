"""Pure Vision and Backlog child-graph state matrix tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)
from workflow.definitions.product_definition import product_definition_graph
from workflow.facts import (
    AuthorityFact,
    BacklogReconciliationFact,
    NodeAttemptFact,
    PhaseArtifactFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecVersionFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import fact_fingerprint

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
PROJECT_ID = 10
SPEC_ID = 101
AUTHORITY_ID = 201
AUTHORITY_FINGERPRINT = "sha256:authority-current"
VISION_ID = 301
VISION_FINGERPRINT = "sha256:vision-current"
BACKLOG_ID = 401
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


def _authority_decision(
    *,
    authority_id: int = AUTHORITY_ID,
    fingerprint: str = AUTHORITY_FINGERPRINT,
) -> ReviewDecisionFact:
    return ReviewDecisionFact(
        decision_id=authority_id + 1_000,
        artifact_type="authority",
        artifact_id=authority_id,
        artifact_fingerprint=fingerprint,
        decision="accepted",
        decided_at=EVALUATED_AT,
    )


def _artifact(  # noqa: PLR0913
    artifact_type: str,
    *,
    artifact_id: int,
    fingerprint: str,
    status: str,
    authority_id: int = AUTHORITY_ID,
    authority_fingerprint: str = AUTHORITY_FINGERPRINT,
    supersedes_artifact_id: int | None = None,
) -> PhaseArtifactFact:
    return PhaseArtifactFact.model_validate(
        {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "artifact_fingerprint": fingerprint,
            "authority_id": authority_id,
            "authority_fingerprint": authority_fingerprint,
            "supersedes_artifact_id": supersedes_artifact_id,
            "status": status,
        }
    )


def _artifact_decision(
    artifact_type: str,
    *,
    artifact_id: int,
    fingerprint: str,
    decision: str,
    decision_id: int | None = None,
) -> ReviewDecisionFact:
    return ReviewDecisionFact.model_validate(
        {
            "decision_id": decision_id or artifact_id + 2_000,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "artifact_fingerprint": fingerprint,
            "decision": decision,
            "decided_at": EVALUATED_AT,
        }
    )


def _attempt(node_id: str, *, outcome: str | None = None) -> NodeAttemptFact:
    return NodeAttemptFact.model_validate(
        {
            "attempt_id": 501,
            "node_id": node_id,
            "instance_key": None,
            "graph_version": "agileforge.workflow.v1",
            "input_fingerprint": "sha256:input",
            "fact_fingerprint": "sha256:facts",
            "business_fact_fingerprint": "sha256:business",
            "decision_fingerprint": "sha256:decision",
            "attempt_fingerprint": "sha256:attempt",
            "model_id": "openrouter/openai/gpt-oss-20b:free",
            "lease_expires_at": EVALUATED_AT + timedelta(minutes=5),
            "outcome": outcome,
        }
    )


def _snapshot(  # noqa: PLR0913
    *,
    accepted_authority: bool = True,
    specs: tuple[SpecVersionFact, ...] | None = None,
    authorities: tuple[AuthorityFact, ...] | None = None,
    artifacts: tuple[PhaseArtifactFact, ...] = (),
    decisions: tuple[ReviewDecisionFact, ...] = (),
    attempts: tuple[NodeAttemptFact, ...] = (),
    reconciliations: tuple[BacklogReconciliationFact, ...] = (),
) -> WorkflowFactSnapshot:
    default_specs = (
        SpecVersionFact(
            spec_version_id=SPEC_ID,
            spec_hash="sha256:spec-current",
            status="approved",
            approved_at=EVALUATED_AT,
        ),
    )
    default_authorities = (_authority(),) if accepted_authority else ()
    authority_decisions = (_authority_decision(),) if accepted_authority else ()
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Vision and Backlog",
            origin="greenfield",
            created_at=EVALUATED_AT,
        ),
        spec_versions=default_specs if specs is None else specs,
        authorities=default_authorities if authorities is None else authorities,
        phase_artifacts=artifacts,
        review_decisions=(*authority_decisions, *decisions),
        node_attempts=attempts,
        backlog_reconciliations=reconciliations,
    )


def _position(snapshot: WorkflowFactSnapshot) -> WorkflowPosition:
    return product_definition_graph().evaluate(snapshot, EVALUATED_AT)


def _decision_for(snapshot: WorkflowFactSnapshot, node_id: str) -> NodeDecision:
    return next(
        item for item in _position(snapshot).decisions if item.node_id == node_id
    )


def test_no_accepted_current_authority_blocks_all_generation() -> None:
    """Block both generators until current authority is accepted."""
    position = _position(_snapshot(accepted_authority=False))

    assert "vision.generate" in position.blocked_nodes
    assert "backlog.generate" in position.blocked_nodes
    assert "vision.generate" not in position.available_nodes
    assert "backlog.generate" not in position.available_nodes


def test_accepted_authority_exposes_parallel_product_definition_branches() -> None:
    """Expose both generators directly from durable accepted authority."""
    position = _position(_snapshot())

    assert position.available_nodes == ("vision.generate", "backlog.generate")
    for node_id in position.available_nodes:
        decision = _decision_for(_snapshot(), node_id)
        assert len(decision.fact_references) == 1
        authority = decision.fact_references[0]
        assert authority.fact_type == "authority"
        assert authority.fact_id == str(AUTHORITY_ID)
        assert authority.fingerprint == AUTHORITY_FINGERPRINT


def test_active_generation_attempt_waits_from_durable_lease() -> None:
    """Route active generation attempts from their durable lease."""
    snapshot = _snapshot(attempts=(_attempt("vision.generate"),))
    decision = _decision_for(snapshot, "vision.generate")

    assert decision.category is NodeCategory.WAITING
    assert decision.reason_code == "VISION_GENERATION_ACTIVE"
    assert decision.valid_until == EVALUATED_AT + timedelta(minutes=5)


def test_vision_draft_waits_for_review() -> None:
    """Wait for review after an immutable Vision draft is recorded."""
    vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="pending_review",
    )
    position = _position(_snapshot(artifacts=(vision,)))

    assert position.waiting_nodes == ("vision.review",)
    assert "vision.generate" not in position.available_nodes
    assert "backlog.generate" in position.available_nodes


def test_backlog_draft_waits_for_review_without_blocking_vision() -> None:
    """Let Backlog progress independently while Vision remains unstarted."""
    backlog = _artifact(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        status="pending_review",
    )

    position = _position(_snapshot(artifacts=(backlog,)))

    assert "backlog.review" in position.waiting_nodes
    assert "backlog.generate" not in position.available_nodes
    assert "vision.generate" in position.available_nodes


def test_rejected_vision_routes_to_superseding_generation() -> None:
    """Route rejected Vision output to a superseding version."""
    vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="rejected",
    )
    rejected = _artifact_decision(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        decision="rejected",
    )
    decision = _decision_for(
        _snapshot(artifacts=(vision,), decisions=(rejected,)),
        "vision.generate",
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.recommendation_kind is RecommendationKind.RECOVERY
    assert decision.fact_references[-1].fact_id == str(VISION_ID)


def test_accepted_vision_exposes_parallel_correction_and_backlog_generation() -> None:
    """Expose correction and Backlog work from accepted Vision."""
    vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="accepted",
    )
    accepted = _artifact_decision(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        decision="accepted",
    )
    position = _position(_snapshot(artifacts=(vision,), decisions=(accepted,)))

    assert position.available_nodes == ("vision.generate", "backlog.generate")
    assert (
        _decision_for(
            _snapshot(artifacts=(vision,), decisions=(accepted,)),
            "vision.generate",
        ).recommendation_kind
        is RecommendationKind.OPTIONAL_REENTRY
    )


def test_backlog_draft_and_rejection_route_through_review_then_recovery() -> None:
    """Route Backlog review and rejected-draft recovery."""
    vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="accepted",
    )
    backlog = _artifact(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        status="pending_review",
    )
    vision_decision = _artifact_decision(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        decision="accepted",
    )
    waiting = _position(
        _snapshot(
            artifacts=(vision, backlog),
            decisions=(vision_decision,),
        )
    )

    assert "backlog.review" in waiting.waiting_nodes

    rejected_backlog = backlog.model_copy(update={"status": "rejected"})
    backlog_decision = _artifact_decision(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        decision="feedback",
    )
    recovery = _decision_for(
        _snapshot(
            artifacts=(vision, rejected_backlog),
            decisions=(vision_decision, backlog_decision),
        ),
        "backlog.generate",
    )
    assert recovery.category is NodeCategory.AVAILABLE
    assert recovery.recommendation_kind is RecommendationKind.RECOVERY


def test_superseded_backlog_requires_a_new_version() -> None:
    """Require a new immutable version after Backlog supersession."""
    vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="accepted",
    )
    backlog = _artifact(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        status="superseded",
    )
    vision_decision = _artifact_decision(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        decision="accepted",
    )
    backlog_decision = _artifact_decision(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        decision="accepted",
    )
    decision = _decision_for(
        _snapshot(
            artifacts=(vision, backlog),
            decisions=(vision_decision, backlog_decision),
        ),
        "backlog.generate",
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "BACKLOG_SUPERSEDED"


def test_authority_replacement_stales_prior_artifacts_and_requires_reconciliation() -> (
    None
):
    """Require explicit recovery and reconciliation after authority changes."""
    replacement_spec_id = SPEC_ID + 1
    replacement_authority_id = AUTHORITY_ID + 1
    replacement_fingerprint = "sha256:authority-replacement"
    specs = (
        SpecVersionFact(
            spec_version_id=SPEC_ID,
            spec_hash="sha256:spec-old",
            status="superseded",
            approved_at=EVALUATED_AT,
        ),
        SpecVersionFact(
            spec_version_id=replacement_spec_id,
            spec_hash="sha256:spec-replacement",
            status="approved",
            approved_at=EVALUATED_AT + timedelta(minutes=1),
        ),
    )
    authorities = (
        _authority(status="stale"),
        _authority(
            authority_id=replacement_authority_id,
            spec_version_id=replacement_spec_id,
            fingerprint=replacement_fingerprint,
        ),
    )
    old_vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="accepted",
    )
    old_backlog = _artifact(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        status="accepted",
    )
    decisions = (
        _authority_decision(
            authority_id=replacement_authority_id,
            fingerprint=replacement_fingerprint,
        ),
        _artifact_decision(
            "vision",
            artifact_id=VISION_ID,
            fingerprint=VISION_FINGERPRINT,
            decision="accepted",
        ),
        _artifact_decision(
            "backlog",
            artifact_id=BACKLOG_ID,
            fingerprint=BACKLOG_FINGERPRINT,
            decision="accepted",
        ),
    )
    snapshot = _snapshot(
        specs=specs,
        authorities=authorities,
        artifacts=(old_vision, old_backlog),
        decisions=decisions,
    )
    position = _position(snapshot)

    assert "vision.generate" in position.available_nodes
    assert "backlog.reconcile" in position.available_nodes
    assert "backlog.generate" in position.blocked_nodes
    reconcile = _decision_for(snapshot, "backlog.reconcile")
    assert reconcile.fact_references[0].fact_id == str(replacement_authority_id)
    assert {item.fact_id for item in reconcile.fact_references[1:]} == {
        str(VISION_ID),
        str(BACKLOG_ID),
    }


def test_accepted_current_vision_and_backlog_form_explicit_planning_join() -> None:
    """Unlock the planning boundary only at the explicit product join."""
    vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="accepted",
    )
    backlog = _artifact(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        status="accepted",
    )
    decisions = (
        _artifact_decision(
            "vision",
            artifact_id=VISION_ID,
            fingerprint=VISION_FINGERPRINT,
            decision="accepted",
        ),
        _artifact_decision(
            "backlog",
            artifact_id=BACKLOG_ID,
            fingerprint=BACKLOG_FINGERPRINT,
            decision="accepted",
        ),
    )
    position = _position(_snapshot(artifacts=(vision, backlog), decisions=decisions))

    assert "planning.roadmap.generate" in position.available_nodes
    join = _decision_for(
        _snapshot(artifacts=(vision, backlog), decisions=decisions),
        "planning.roadmap.generate",
    )
    assert {item.fact_type for item in join.fact_references} == {
        "vision",
        "backlog",
    }


def test_contradictory_vision_terminal_decisions_fail_closed() -> None:
    """Expose invalid Vision nodes for contradictory terminal decisions."""
    vision = _artifact(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        status="accepted",
    )
    accepted = _artifact_decision(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        decision="accepted",
        decision_id=601,
    )
    rejected = _artifact_decision(
        "vision",
        artifact_id=VISION_ID,
        fingerprint=VISION_FINGERPRINT,
        decision="rejected",
        decision_id=602,
    )
    position = _position(_snapshot(artifacts=(vision,), decisions=(accepted, rejected)))

    assert {"vision.generate", "vision.review"} <= set(position.invalid_nodes)
    assert all(
        decision.reason_code == "WORKFLOW_FACT_CONFLICT"
        for decision in position.decisions
        if decision.node_id in {"vision.generate", "vision.review"}
    )


def test_contradictory_backlog_terminal_decisions_fail_closed() -> None:
    """Expose invalid Backlog nodes for contradictory terminal decisions."""
    backlog = _artifact(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        status="accepted",
    )
    accepted = _artifact_decision(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        decision="accepted",
        decision_id=701,
    )
    rejected = _artifact_decision(
        "backlog",
        artifact_id=BACKLOG_ID,
        fingerprint=BACKLOG_FINGERPRINT,
        decision="rejected",
        decision_id=702,
    )

    position = _position(
        _snapshot(artifacts=(backlog,), decisions=(accepted, rejected))
    )

    assert {"backlog.generate", "backlog.review"} <= set(position.invalid_nodes)
    assert all(
        decision.reason_code == "WORKFLOW_FACT_CONFLICT"
        for decision in position.decisions
        if decision.node_id in {"backlog.generate", "backlog.review"}
    )


def test_reconciliation_actor_and_audit_binding_are_authoritative_facts() -> None:
    """Fingerprint the actor and exact canonical audit event binding."""
    reconciliation = BacklogReconciliationFact(
        reconciliation_id=801,
        replacement_authority_id=AUTHORITY_ID,
        replacement_authority_fingerprint=AUTHORITY_FINGERPRINT,
        affected_artifact_ids=(VISION_ID, BACKLOG_ID),
        affected_artifacts_fingerprint="sha256:affected-artifacts",
        reconciled_by="operator@example.com",
        audit_event_id=901,
        audit_event_action="backlog_authority_reconciled",
        audit_event_fingerprint="sha256:audit-event",
        reconciled_at=EVALUATED_AT,
    )
    changed_actor = reconciliation.model_copy(
        update={"reconciled_by": "tampered@example.com"}
    )
    changed_audit = reconciliation.model_copy(
        update={"audit_event_fingerprint": "sha256:tampered-audit"}
    )

    baseline = _snapshot(reconciliations=(reconciliation,))
    actor_tampered = _snapshot(reconciliations=(changed_actor,))
    audit_tampered = _snapshot(reconciliations=(changed_audit,))

    assert fact_fingerprint(actor_tampered) != fact_fingerprint(baseline)
    assert fact_fingerprint(audit_tampered) != fact_fingerprint(baseline)
