"""Pure greenfield onboarding graph-state matrix tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import pytest

from workflow.contracts import NodeCategory
from workflow.definitions.onboarding import (
    GREENFIELD_ONBOARDING_NODES,
    greenfield_graph,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.facts import (
    ChallengeArtifactFact,
    DiscoveryRunFact,
    InitialScopeRegistrationFact,
    PrdVersionFact,
    ProjectAbandonmentFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecDraftFact,
    WorkflowFactSnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Collection

EVALUATED_AT = datetime(2026, 8, 2, 14, tzinfo=UTC)
PROJECT_ID = 7
INITIAL_RUN_ID = 11
CHALLENGE_ID = 101
CHALLENGE_FINGERPRINT = "sha256:challenge"
PRD_ID = 201
PRD_FINGERPRINT = "sha256:prd-v1"
SPEC_DRAFT_ID = 301
SPEC_DRAFT_FINGERPRINT = "sha256:spec-v1"


@dataclass(frozen=True)
class _MatrixCase:
    """Expected routing projection for one greenfield fact stage."""

    snapshot: WorkflowFactSnapshot
    available: tuple[str, ...]
    invalid: tuple[str, ...] = ()
    terminal: bool = False


def _project(
    *,
    origin: Literal["greenfield", "brownfield"] = "greenfield",
) -> ProjectFact:
    return ProjectFact(
        project_id=PROJECT_ID,
        name="Greenfield Matrix",
        origin=origin,
        created_at=EVALUATED_AT,
    )


def _initial_run(*, run_id: int = INITIAL_RUN_ID) -> DiscoveryRunFact:
    return DiscoveryRunFact(
        discovery_run_id=run_id,
        project_id=PROJECT_ID,
        purpose="initial",
        ordinal=1,
        created_at=EVALUATED_AT,
        closed_at=None,
    )


def _challenge(*, run_id: int = INITIAL_RUN_ID) -> ChallengeArtifactFact:
    return ChallengeArtifactFact(
        challenge_artifact_id=CHALLENGE_ID,
        discovery_run_id=run_id,
        content_fingerprint=CHALLENGE_FINGERPRINT,
        supersedes_id=None,
    )


def _prd(
    *,
    prd_id: int = PRD_ID,
    fingerprint: str = PRD_FINGERPRINT,
    supersedes_id: int | None = None,
) -> PrdVersionFact:
    return PrdVersionFact(
        prd_version_id=prd_id,
        discovery_run_id=INITIAL_RUN_ID,
        content_fingerprint=fingerprint,
        supersedes_id=supersedes_id,
    )


def _spec_draft(
    *,
    spec_draft_id: int = SPEC_DRAFT_ID,
    fingerprint: str = SPEC_DRAFT_FINGERPRINT,
    supersedes_id: int | None = None,
) -> SpecDraftFact:
    return SpecDraftFact(
        spec_draft_id=spec_draft_id,
        discovery_run_id=INITIAL_RUN_ID,
        kind="initial",
        content_fingerprint=fingerprint,
        base_spec_version_id=None,
        base_spec_hash=None,
        supersedes_id=supersedes_id,
    )


def _decision(
    *,
    decision_id: int,
    artifact_type: Literal["prd", "spec_draft", "authority"],
    artifact_id: int,
    fingerprint: str,
    decision: Literal["accepted", "rejected", "feedback"],
) -> ReviewDecisionFact:
    return ReviewDecisionFact(
        decision_id=decision_id,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        artifact_fingerprint=fingerprint,
        decision=decision,
        decided_at=EVALUATED_AT,
    )


def _snapshot(
    *,
    artifacts: Collection[str] = (),
    decisions: tuple[ReviewDecisionFact, ...] = (),
    states: Collection[str] = (),
) -> WorkflowFactSnapshot:
    return WorkflowFactSnapshot(
        project=_project(),
        project_abandonments=(
            (
                ProjectAbandonmentFact(
                    project_abandonment_id=1,
                    project_id=PROJECT_ID,
                    reason="Stopped",
                    abandoned_by="operator",
                    abandoned_at=EVALUATED_AT,
                ),
            )
            if "abandoned" in states
            else ()
        ),
        discovery_runs=(_initial_run(),),
        challenge_artifacts=(_challenge(),) if "challenge" in artifacts else (),
        prd_versions=(_prd(),) if "prd" in artifacts else (),
        review_decisions=decisions,
        spec_drafts=(_spec_draft(),) if "spec" in artifacts else (),
        initial_registrations=(
            (
                InitialScopeRegistrationFact(
                    registration_id=401,
                    discovery_run_id=INITIAL_RUN_ID,
                    spec_draft_id=SPEC_DRAFT_ID,
                    spec_version_id=501,
                    spec_hash="sha256:registered-spec",
                ),
            )
            if "registered" in states
            else ()
        ),
    )


PRD_ACCEPTED = _decision(
    decision_id=1,
    artifact_type="prd",
    artifact_id=PRD_ID,
    fingerprint=PRD_FINGERPRINT,
    decision="accepted",
)
PRD_REJECTED = _decision(
    decision_id=2,
    artifact_type="prd",
    artifact_id=PRD_ID,
    fingerprint=PRD_FINGERPRINT,
    decision="rejected",
)
SPEC_ACCEPTED = _decision(
    decision_id=3,
    artifact_type="spec_draft",
    artifact_id=SPEC_DRAFT_ID,
    fingerprint=SPEC_DRAFT_FINGERPRINT,
    decision="accepted",
)
SPEC_REJECTED = _decision(
    decision_id=4,
    artifact_type="spec_draft",
    artifact_id=SPEC_DRAFT_ID,
    fingerprint=SPEC_DRAFT_FINGERPRINT,
    decision="feedback",
)
AUTHORITY_ACCEPTED = _decision(
    decision_id=5,
    artifact_type="authority",
    artifact_id=501,
    fingerprint="sha256:accepted-authority",
    decision="accepted",
)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(),
                available=("onboarding.greenfield.challenge",),
            ),
            id="no-challenge",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(artifacts={"challenge"}),
                available=("onboarding.greenfield.prd",),
            ),
            id="challenge-recorded",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(artifacts={"challenge", "prd"}),
                available=("onboarding.greenfield.prd_review",),
            ),
            id="prd-draft",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(artifacts={"challenge", "prd"}),
                available=("onboarding.greenfield.prd_review",),
            ),
            id="pending-prd-review",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd"},
                    decisions=(PRD_REJECTED,),
                ),
                available=("onboarding.greenfield.prd",),
            ),
            id="rejected-prd",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd"},
                    decisions=(PRD_ACCEPTED,),
                ),
                available=("onboarding.greenfield.initial_spec",),
            ),
            id="accepted-prd",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd", "spec"},
                    decisions=(PRD_ACCEPTED,),
                ),
                available=("onboarding.greenfield.initial_spec_review",),
            ),
            id="initial-spec-draft",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd", "spec"},
                    decisions=(PRD_ACCEPTED,),
                ),
                available=("onboarding.greenfield.initial_spec_review",),
            ),
            id="pending-initial-spec-review",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd", "spec"},
                    decisions=(PRD_ACCEPTED, SPEC_REJECTED),
                ),
                available=("onboarding.greenfield.initial_spec",),
            ),
            id="rejected-initial-spec",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd", "spec"},
                    decisions=(PRD_ACCEPTED, SPEC_ACCEPTED),
                ),
                available=("onboarding.initial_scope_registration",),
            ),
            id="accepted-initial-spec",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd", "spec"},
                    decisions=(PRD_ACCEPTED, SPEC_ACCEPTED),
                    states={"registered"},
                ),
                available=(),
                terminal=True,
            ),
            id="registered-scope",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(
                    artifacts={"challenge", "prd"},
                    decisions=(PRD_ACCEPTED, PRD_REJECTED),
                ),
                available=(),
                invalid=("onboarding.greenfield.prd_review",),
            ),
            id="contradictory-terminal-decisions",
        ),
        pytest.param(
            _MatrixCase(
                snapshot=_snapshot(states={"abandoned"}),
                available=(),
                terminal=True,
            ),
            id="abandoned-shell",
        ),
    ],
)
def test_greenfield_graph_state_matrix(case: _MatrixCase) -> None:
    """Route every approved greenfield stage from immutable facts."""
    position = greenfield_graph().evaluate(case.snapshot, EVALUATED_AT)

    assert position.available_nodes == case.available
    assert position.invalid_nodes == case.invalid
    assert position.terminal is case.terminal


def test_greenfield_rules_use_only_the_projects_initial_discovery_run() -> None:
    """Ignore extension-run artifacts while selecting initial onboarding facts."""
    extension = DiscoveryRunFact(
        discovery_run_id=99,
        project_id=PROJECT_ID,
        purpose="extension",
        ordinal=2,
        created_at=EVALUATED_AT,
        closed_at=None,
    )
    snapshot = WorkflowFactSnapshot(
        project=_project(),
        discovery_runs=(_initial_run(), extension),
        challenge_artifacts=(_challenge(run_id=extension.discovery_run_id),),
    )

    position = greenfield_graph().evaluate(snapshot, EVALUATED_AT)

    assert position.available_nodes == ("onboarding.greenfield.challenge",)


def test_review_decisions_bind_to_the_exact_persisted_fingerprint() -> None:
    """Fail closed when a decision fingerprint does not match its artifact."""
    mismatched = _decision(
        decision_id=8,
        artifact_type="prd",
        artifact_id=PRD_ID,
        fingerprint="sha256:not-the-prd",
        decision="accepted",
    )

    position = greenfield_graph().evaluate(
        _snapshot(
            artifacts={"challenge", "prd"},
            decisions=(mismatched,),
        ),
        EVALUATED_AT,
    )

    assert position.available_nodes == ()
    assert position.invalid_nodes == ("onboarding.greenfield.prd_review",)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "onboarding.greenfield.prd_review"
    )
    assert decision.category is NodeCategory.INVALID
    assert decision.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_raw_initial_spec_cannot_bypass_challenge_and_prd_review() -> None:
    """Treat downstream artifacts without accepted prerequisites as conflicts."""
    snapshot = WorkflowFactSnapshot(
        project=_project(),
        discovery_runs=(_initial_run(),),
        spec_drafts=(_spec_draft(),),
        review_decisions=(SPEC_ACCEPTED,),
    )

    position = greenfield_graph().evaluate(snapshot, EVALUATED_AT)

    assert position.available_nodes == ()
    assert position.invalid_nodes
    assert all(not node.startswith("backlog.") for node in position.available_nodes)


def test_abandoned_shell_remains_terminal_with_historical_artifact_facts() -> None:
    """Keep prior immutable provenance while abandonment ends normal routing."""
    position = greenfield_graph().evaluate(
        _snapshot(artifacts={"challenge"}, states={"abandoned"}),
        EVALUATED_AT,
    )

    assert position.available_nodes == ()
    assert position.invalid_nodes == ()
    assert position.terminal is True


def test_abandonment_with_historical_accepted_authority_is_fact_conflict() -> None:
    """Fail closed when terminal shell and downstream activation facts coexist."""
    snapshot = _snapshot(
        decisions=(AUTHORITY_ACCEPTED,),
        states={"abandoned"},
    )
    greenfield_node_ids = tuple(node.node_id for node in GREENFIELD_ONBOARDING_NODES)

    onboarding_position = greenfield_graph().evaluate(snapshot, EVALUATED_AT)
    root_position = ROOT_GRAPH.evaluate(snapshot, EVALUATED_AT)

    assert onboarding_position.available_nodes == ()
    assert onboarding_position.invalid_nodes == greenfield_node_ids
    assert onboarding_position.terminal is False
    assert root_position.available_nodes == ()
    assert root_position.invalid_nodes == (
        *greenfield_node_ids,
        "onboarding.abandon_shell",
    )
    assert root_position.terminal is False
    assert all(
        decision.category is NodeCategory.INVALID
        and decision.reason_code == "WORKFLOW_FACT_CONFLICT"
        for decision in root_position.decisions
        if decision.node_id in root_position.invalid_nodes
    )


def test_node_decisions_expose_exact_request_kinds_and_artifact_references() -> None:
    """Keep the closed request contract and selected fingerprint visible."""
    graph = greenfield_graph()
    shell_position = graph.evaluate(_snapshot(), EVALUATED_AT)
    shell_decision = shell_position.decisions[0]
    assert shell_decision.request_kind == "record_challenge_artifact"

    review_position = graph.evaluate(
        _snapshot(artifacts={"challenge", "prd"}),
        EVALUATED_AT,
    )
    review_decision = next(
        item
        for item in review_position.decisions
        if item.node_id == "onboarding.greenfield.prd_review"
    )
    assert review_decision.request_kind == "decide_prd"
    assert tuple(
        (reference.fact_id, reference.fingerprint)
        for reference in review_decision.fact_references
    ) == ((str(PRD_ID), PRD_FINGERPRINT),)
