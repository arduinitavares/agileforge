"""Tests for workflow fact and decision fingerprints."""

from datetime import UTC, datetime, timedelta

from services.agent_workbench.fingerprints import (
    canonical_hash as legacy_canonical_hash,
)
from workflow.contracts import TransitionResult
from workflow.facts import NodeAttemptFact, ProjectFact, WorkflowFactSnapshot
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
            origin="brownfield",
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
            origin="brownfield",
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
            origin="brownfield",
            created_at=created,
        )
    )
    changed = snapshot.model_copy(
        update={"project": snapshot.project.model_copy(update={"name": "caRtola"})}
    )

    assert fact_fingerprint(changed) != fact_fingerprint(snapshot)
    assert business_fact_fingerprint(changed) != business_fact_fingerprint(snapshot)


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
