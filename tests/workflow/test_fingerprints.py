"""Tests for workflow fact and decision fingerprints."""

from datetime import UTC, datetime, timedelta

from services.agent_workbench.fingerprints import (
    canonical_hash as legacy_canonical_hash,
)
from workflow.contracts import TransitionResult
from workflow.facts import (
    NodeAttemptFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalInterviewTurnFact,
    ProjectFact,
    VisionInterviewTurnFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import (
    business_fact_fingerprint,
    canonical_hash,
    canonical_json,
    decision_fingerprint,
    fact_fingerprint,
)


def test_fact_fingerprint_is_stable_for_equivalent_snapshots() -> None:
    """Hash equivalent immutable snapshots to the same graph fact fingerprint."""
    created = datetime(2026, 8, 2, 12, tzinfo=UTC)
    first = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=3,
            name="MyFinance",
            created_at=created,
        )
    )
    second = first.model_copy(deep=True)
    assert fact_fingerprint(first) == fact_fingerprint(second)
    assert fact_fingerprint(first).startswith("sha256:")


def test_attempt_changes_full_fingerprint_but_not_business_fingerprint() -> None:
    """Execution trace facts must not invalidate their own business guard."""
    created = datetime(2026, 8, 2, 12, tzinfo=UTC)
    snapshot = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=3,
            name="MyFinance",
            created_at=created,
        )
    )
    with_attempt = snapshot.model_copy(
        update={
            "node_attempts": (
                NodeAttemptFact(
                    attempt_id=7,
                    node_id="backlog.generate",
                    instance_key=None,
                    graph_version="agileforge.workflow.v1",
                    input_fingerprint="sha256:input",
                    fact_fingerprint=fact_fingerprint(snapshot),
                    business_fact_fingerprint=business_fact_fingerprint(snapshot),
                    decision_fingerprint="sha256:decision",
                    attempt_fingerprint="sha256:attempt",
                    model_id="fake/model",
                    lease_expires_at=created + timedelta(minutes=5),
                    outcome=None,
                ),
            )
        }
    )

    assert fact_fingerprint(with_attempt) != fact_fingerprint(snapshot)
    assert business_fact_fingerprint(with_attempt) == business_fact_fingerprint(
        snapshot
    )


def test_business_fact_changes_both_fingerprints() -> None:
    """Every durable business fact remains part of both authority hashes."""
    created = datetime(2026, 8, 2, 12, tzinfo=UTC)
    snapshot = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=3,
            name="MyFinance",
            created_at=created,
        )
    )
    changed = snapshot.model_copy(
        update={"project": snapshot.project.model_copy(update={"name": "caRtola"})}
    )

    assert fact_fingerprint(changed) != fact_fingerprint(snapshot)
    assert business_fact_fingerprint(changed) != business_fact_fingerprint(snapshot)


def test_incomplete_vision_turn_changes_business_fact_fingerprint() -> None:
    """Treat durable incomplete interview turns as product business evidence."""
    created = datetime(2026, 8, 2, 12, tzinfo=UTC)
    snapshot = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=3,
            name="MyFinance",
            created_at=created,
        ),
        vision_interview_turns=(
            VisionInterviewTurnFact(
                vision_interview_turn_id=7,
                mode="initial",
                turn_number=1,
                revision_intent_id=None,
                prior_turn_id=None,
                user_text="Track the household ledger.",
                components={"scope": "household"},
                vision_statement="A household ledger.",
                is_complete=False,
                clarifying_questions=("Which institutions?",),
                output_fingerprint="sha256:turn",
                workflow_node_attempt_id=11,
                attempt_fingerprint="sha256:attempt",
                recorded_at=created,
            ),
        ),
    )
    completed = snapshot.model_copy(
        update={
            "vision_interview_turns": (
                snapshot.vision_interview_turns[0].model_copy(
                    update={"is_complete": True}
                ),
            )
        }
    )

    assert business_fact_fingerprint(snapshot) != business_fact_fingerprint(completed)


def test_incomplete_product_goal_turn_changes_business_fact_fingerprint() -> None:
    """Treat incomplete Product Goal turns as durable business evidence."""
    created = datetime(2026, 8, 2, 12, tzinfo=UTC)
    snapshot = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=3,
            name="MyFinance",
            created_at=created,
        ),
        product_goal_interview_turns=(
            ProductGoalInterviewTurnFact(
                product_goal_interview_turn_id=8,
                vision_artifact_id=7,
                vision_fingerprint="sha256:vision",
                goal_number=1,
                revision_number=1,
                prior_turn_id=None,
                user_text="Preserve exact lineage.",
                components={"scope": "lineage"},
                goal_statement="Preserve durable lineage.",
                is_complete=False,
                clarifying_questions=("Which parent records?",),
                output_fingerprint="sha256:goal-turn",
                workflow_node_attempt_id=12,
                attempt_fingerprint="sha256:attempt",
                recorded_at=created,
            ),
        ),
    )
    completed = snapshot.model_copy(
        update={
            "product_goal_interview_turns": (
                snapshot.product_goal_interview_turns[0].model_copy(
                    update={"is_complete": True}
                ),
            )
        }
    )

    assert business_fact_fingerprint(snapshot) != business_fact_fingerprint(completed)


def test_goal_decision_changes_business_fingerprint_before_discovery() -> None:
    """Goal review state is authoritative even before discovery is recorded."""
    created = datetime(2026, 8, 2, 12, tzinfo=UTC)
    pending = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=3,
            name="MyFinance",
            created_at=created,
        )
    )
    states = {
        decision: pending.model_copy(
            update={
                "product_goal_artifact_decisions": (
                    ProductGoalArtifactDecisionFact(
                        product_goal_artifact_decision_id=7,
                        product_goal_artifact_id=6,
                        artifact_fingerprint="sha256:goal",
                        decision=decision,
                        rationale=f"{decision} review state",
                        reviewer="operator",
                        idempotency_key=f"goal-{decision}",
                        decided_at=created,
                    ),
                )
            }
        )
        for decision in ("accepted", "rejected", "feedback")
    }

    fingerprints = {
        business_fact_fingerprint(snapshot) for snapshot in (pending, *states.values())
    }

    assert not pending.discovery_artifacts
    assert business_fact_fingerprint(states["accepted"]) != business_fact_fingerprint(
        pending
    )
    assert len(fingerprints) == len(states) + 1


def test_decision_fingerprint_is_order_stable() -> None:
    """Hash semantically equivalent decision payloads identically."""
    assert decision_fingerprint({"b": 2, "a": [1, 2]}) == decision_fingerprint(
        {"a": [1, 2], "b": 2}
    )


def test_legacy_fingerprint_module_reexports_domain_implementation() -> None:
    """Keep existing callers on the exact moved canonical-hash implementation."""
    assert legacy_canonical_hash is canonical_hash


def test_frozen_transition_output_preserves_canonical_fingerprints() -> None:
    """Keep immutable output byte-compatible with ordinary canonical JSON."""
    output = {
        "attempt_id": 7,
        "metadata": {"ready": True, "labels": ["authority", None]},
    }
    result = TransitionResult(ok=True, output=output)
    dumped_output = result.model_dump(mode="json")["output"]

    assert canonical_json(result.output) == canonical_json(output)
    assert canonical_hash(result.output) == canonical_hash(output)
    assert canonical_hash(dumped_output) == canonical_hash(output)
