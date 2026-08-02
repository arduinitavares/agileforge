"""Tests for workflow fact and decision fingerprints."""

from datetime import UTC, datetime

from services.agent_workbench.fingerprints import (
    canonical_hash as legacy_canonical_hash,
)
from workflow.facts import ProjectFact, WorkflowFactSnapshot
from workflow.fingerprints import canonical_hash, decision_fingerprint, fact_fingerprint


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


def test_decision_fingerprint_is_order_stable() -> None:
    """Hash semantically equivalent decision payloads identically."""
    assert decision_fingerprint({"b": 2, "a": [1, 2]}) == decision_fingerprint(
        {"a": [1, 2], "b": 2}
    )


def test_legacy_fingerprint_module_reexports_domain_implementation() -> None:
    """Keep existing callers on the exact moved canonical-hash implementation."""
    assert legacy_canonical_hash is canonical_hash
