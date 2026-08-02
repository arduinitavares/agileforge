"""Pure brownfield onboarding graph-state tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from workflow.contracts import NodeCategory
from workflow.definitions.onboarding import brownfield_graph
from workflow.facts import (
    DiscoveryRunFact,
    InitialScopeRegistrationFact,
    ProjectFact,
    RepositoryBaselineFact,
    RepositoryInventoryFact,
    ReviewDecisionFact,
    SpecDraftFact,
    WorkflowFactSnapshot,
)

EVALUATED_AT = datetime(2026, 8, 2, 16, tzinfo=UTC)
PROJECT_ID = 9
RUN_ID = 12
BASELINE_ID = 31
INVENTORY_ID = 41
SPEC_DRAFT_ID = 51
SPEC_FINGERPRINT = "sha256:brownfield-spec"


def _snapshot(
    *,
    baseline: bool = False,
    inventory: bool = False,
    draft: bool = False,
    decision: Literal["accepted", "rejected", "feedback"] | None = None,
    registered: bool = False,
) -> WorkflowFactSnapshot:
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Brownfield Matrix",
            origin="brownfield",
            created_at=EVALUATED_AT,
        ),
        discovery_runs=(
            DiscoveryRunFact(
                discovery_run_id=RUN_ID,
                project_id=PROJECT_ID,
                purpose="initial",
                ordinal=1,
                created_at=EVALUATED_AT,
                closed_at=None,
            ),
        ),
        repository_baselines=(
            RepositoryBaselineFact(
                repository_baseline_id=BASELINE_ID,
                repository_path="/evidence/repository",
                git_commit="a" * 40,
                dirty=False,
                content_fingerprint="sha256:baseline",
            ),
        )
        if baseline
        else (),
        repository_inventories=(
            RepositoryInventoryFact(
                repository_inventory_id=INVENTORY_ID,
                repository_baseline_id=BASELINE_ID,
                content_fingerprint="sha256:inventory",
                file_count=2,
                total_bytes=20,
                selected_for_model=("README.md",),
            ),
        )
        if inventory
        else (),
        spec_drafts=(
            SpecDraftFact(
                spec_draft_id=SPEC_DRAFT_ID,
                discovery_run_id=RUN_ID,
                kind="initial",
                content_fingerprint=SPEC_FINGERPRINT,
                base_spec_version_id=None,
                base_spec_hash=None,
                supersedes_id=None,
            ),
        )
        if draft
        else (),
        review_decisions=(
            ReviewDecisionFact(
                decision_id=61,
                artifact_type="spec_draft",
                artifact_id=SPEC_DRAFT_ID,
                artifact_fingerprint=SPEC_FINGERPRINT,
                decision=decision,
                decided_at=EVALUATED_AT,
            ),
        )
        if decision is not None
        else (),
        initial_registrations=(
            InitialScopeRegistrationFact(
                registration_id=71,
                discovery_run_id=RUN_ID,
                spec_draft_id=SPEC_DRAFT_ID,
                spec_version_id=81,
                spec_hash="sha256:registered",
            ),
        )
        if registered
        else (),
    )


def _available(snapshot: WorkflowFactSnapshot) -> tuple[str, ...]:
    position = brownfield_graph().evaluate(snapshot, EVALUATED_AT)
    return tuple(
        item.node_id
        for item in position.decisions
        if item.category is NodeCategory.AVAILABLE
    )


def test_brownfield_graph_routes_exact_four_nodes_then_shared_registration() -> None:
    """Converge reviewed brownfield evidence on shared initial registration."""
    assert _available(_snapshot()) == ("onboarding.brownfield.baseline",)
    assert _available(_snapshot(baseline=True)) == ("onboarding.brownfield.inventory",)
    assert _available(_snapshot(baseline=True, inventory=True)) == (
        "onboarding.brownfield.curation",
    )
    assert _available(_snapshot(baseline=True, inventory=True, draft=True)) == (
        "onboarding.brownfield.initial_spec_review",
    )
    assert _available(
        _snapshot(
            baseline=True,
            inventory=True,
            draft=True,
            decision="accepted",
        )
    ) == ("onboarding.initial_scope_registration",)
    assert (
        _available(
            _snapshot(
                baseline=True,
                inventory=True,
                draft=True,
                decision="accepted",
                registered=True,
            )
        )
        == ()
    )


def test_repository_evidence_is_not_accepted_project_authority() -> None:
    """Inventory advances curation only; it cannot register or accept scope."""
    snapshot = _snapshot(baseline=True, inventory=True)
    position = brownfield_graph().evaluate(snapshot, EVALUATED_AT)

    assert snapshot.project.project_id == PROJECT_ID
    assert snapshot.authorities == ()
    assert "onboarding.brownfield.curation" in _available(snapshot)
    registration = next(
        item
        for item in position.decisions
        if item.node_id == "onboarding.initial_scope_registration"
    )
    assert registration.category is NodeCategory.WAITING


def test_brownfield_rejected_draft_returns_to_curation() -> None:
    """Require a reviewed replacement without duplicating registration logic."""
    assert _available(
        _snapshot(
            baseline=True,
            inventory=True,
            draft=True,
            decision="rejected",
        )
    ) == ("onboarding.brownfield.curation",)


def test_brownfield_downstream_facts_without_baseline_are_invalid() -> None:
    """Expose relationship corruption instead of routing around missing evidence."""
    position = brownfield_graph().evaluate(_snapshot(inventory=True), EVALUATED_AT)

    assert any(item.category is NodeCategory.INVALID for item in position.decisions)
    assert all(
        item.category is not NodeCategory.AVAILABLE for item in position.decisions
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(),
        _snapshot(baseline=True),
        _snapshot(baseline=True, inventory=True),
        _snapshot(baseline=True, inventory=True, draft=True),
    ],
)
def test_normal_brownfield_prerequisites_are_not_fact_conflicts(
    snapshot: WorkflowFactSnapshot,
) -> None:
    """Use waiting states for incomplete valid flow, reserving invalid for tamper."""
    position = brownfield_graph().evaluate(snapshot, EVALUATED_AT)

    assert all(item.category is not NodeCategory.INVALID for item in position.decisions)
