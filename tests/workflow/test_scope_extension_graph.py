"""Pure scope-extension graph matrix tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from workflow.contracts import (
    FactReference,
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
)
from workflow.definitions.scope_extension import scope_extension_graph
from workflow.facts import (
    AuthorityFact,
    ChallengeArtifactFact,
    DiscoveryRunAbandonmentFact,
    DiscoveryRunFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    PostSprintTriageFact,
    PrdVersionFact,
    ProjectFact,
    ReviewDecisionFact,
    ScopeExtensionReconciliationFact,
    ScopeExtensionRegistrationFact,
    SpecDraftFact,
    SpecVersionFact,
    SprintFact,
    SprintStartFact,
    StoryFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import canonical_hash

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
PROJECT_ID = 13
BASE_SPEC_ID = 101
BASE_SPEC_HASH = "sha256:base-spec"
BASE_AUTHORITY_ID = 201
EXTENSION_RUN_ID = 301
AMENDMENT_DRAFT_ID = 401
REPLACEMENT_SPEC_ID = 501
REPLACEMENT_SPEC_HASH = "sha256:replacement-spec"
REPLACEMENT_AUTHORITY_ID = 601


def _review(
    *,
    decision_id: int,
    artifact_type: str,
    artifact_id: int,
    fingerprint: str,
    decision: str = "accepted",
) -> ReviewDecisionFact:
    return ReviewDecisionFact.model_validate(
        {
            "decision_id": decision_id,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "artifact_fingerprint": fingerprint,
            "decision": decision,
            "decided_at": EVALUATED_AT,
        }
    )


def completed_project_snapshot() -> WorkflowFactSnapshot:
    """Return a terminal Project with accepted authority and completed triage."""
    triage_payload: JsonObject = {"summary": "Current scope is complete."}
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Task 13 graph",
            origin="greenfield",
            created_at=EVALUATED_AT,
        ),
        discovery_runs=(
            DiscoveryRunFact(
                discovery_run_id=1,
                project_id=PROJECT_ID,
                purpose="initial",
                ordinal=1,
                created_at=EVALUATED_AT,
                closed_at=EVALUATED_AT,
            ),
        ),
        review_decisions=(
            _review(
                decision_id=1,
                artifact_type="authority",
                artifact_id=BASE_AUTHORITY_ID,
                fingerprint="sha256:base-authority",
            ),
        ),
        spec_versions=(
            SpecVersionFact(
                spec_version_id=BASE_SPEC_ID,
                spec_hash=BASE_SPEC_HASH,
                status="approved",
                approved_at=EVALUATED_AT,
            ),
        ),
        authorities=(
            AuthorityFact(
                authority_id=BASE_AUTHORITY_ID,
                spec_version_id=BASE_SPEC_ID,
                authority_fingerprint="sha256:base-authority",
                status="accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
        sprints=(
            SprintFact(
                sprint_id=11,
                status="completed",
                completed_at=EVALUATED_AT,
            ),
        ),
        stories=(
            StoryFact(
                story_id=21,
                status="Done",
                sprint_candidate=False,
                readiness_blockers=(),
            ),
        ),
        post_sprint_triage=(
            PostSprintTriageFact(
                triage_id=31,
                sprint_id=11,
                impact="none",
                canonical_payload=triage_payload,
                payload_fingerprint=canonical_hash(
                    {"impact": "none", "canonical_payload": triage_payload}
                ),
                supersedes_triage_id=None,
            ),
        ),
    )


def _extension_run(*, closed: bool = False) -> DiscoveryRunFact:
    return DiscoveryRunFact(
        discovery_run_id=EXTENSION_RUN_ID,
        project_id=PROJECT_ID,
        purpose="extension",
        ordinal=2,
        base_spec_version_id=BASE_SPEC_ID,
        base_spec_hash=BASE_SPEC_HASH,
        created_at=EVALUATED_AT,
        closed_at=EVALUATED_AT if closed else None,
    )


def _with_extension(
    snapshot: WorkflowFactSnapshot,
    **updates: object,
) -> WorkflowFactSnapshot:
    values: dict[str, object] = {
        "discovery_runs": (*snapshot.discovery_runs, _extension_run()),
    }
    values.update(updates)
    return snapshot.model_copy(update=values)


def _decision(
    snapshot: WorkflowFactSnapshot,
    node_id: str,
    instance_key: str | None = None,
) -> NodeDecision | None:
    position = scope_extension_graph().evaluate(snapshot, EVALUATED_AT)
    return next(
        (
            item
            for item in position.decisions
            if item.node_id == node_id and item.instance_key == instance_key
        ),
        None,
    )


def test_terminal_project_exposes_optional_new_extension_only() -> None:
    """Keep a completed Project terminal while exposing optional re-entry."""
    snapshot = completed_project_snapshot()
    position = scope_extension_graph().evaluate(snapshot, EVALUATED_AT)
    start = _decision(snapshot, "scope_extension.start")

    assert position.terminal is True
    assert start is not None
    assert start.category is NodeCategory.AVAILABLE
    assert start.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert {item.node_id for item in position.decisions} == {"scope_extension.start"}


@pytest.mark.parametrize(
    ("update", "reason_code"),
    [
        (
            {
                "sprints": (
                    SprintFact(sprint_id=12, status="active", completed_at=None),
                )
            },
            "ACTIVE_SPRINT_EXISTS",
        ),
        (
            {
                "stories": (
                    StoryFact(
                        story_id=22,
                        status="Ready",
                        sprint_candidate=True,
                        readiness_blockers=(),
                    ),
                )
            },
            "SPRINT_CANDIDATES_EXIST",
        ),
        ({"post_sprint_triage": ()}, "POST_SPRINT_TRIAGE_REQUIRED"),
    ],
)
def test_start_blocks_nonterminal_scope(
    update: dict[str, object],
    reason_code: str,
) -> None:
    """Block extension start until execution and triage are complete."""
    snapshot = completed_project_snapshot().model_copy(update=update)
    start = _decision(snapshot, "scope_extension.start")

    assert start is not None
    assert start.category is NodeCategory.BLOCKED
    assert start.reason_code == reason_code


def test_one_unresolved_extension_hides_start_and_exposes_challenge() -> None:
    """Expose challenge recording instead of a second extension start."""
    snapshot = _with_extension(completed_project_snapshot())

    assert _decision(snapshot, "scope_extension.start") is None
    challenge = _decision(
        snapshot,
        "scope_extension.challenge",
        f"run:{EXTENSION_RUN_ID}",
    )
    assert challenge is not None
    assert challenge.category is NodeCategory.AVAILABLE


def test_multiple_unresolved_extensions_are_invalid() -> None:
    """Reject snapshots that violate unresolved-extension cardinality."""
    snapshot = _with_extension(completed_project_snapshot())
    duplicate = DiscoveryRunFact(
        discovery_run_id=EXTENSION_RUN_ID + 1,
        project_id=PROJECT_ID,
        purpose="extension",
        ordinal=3,
        base_spec_version_id=BASE_SPEC_ID,
        base_spec_hash=BASE_SPEC_HASH,
        created_at=EVALUATED_AT,
        closed_at=None,
    )
    snapshot = snapshot.model_copy(
        update={"discovery_runs": (*snapshot.discovery_runs, duplicate)}
    )

    start = _decision(snapshot, "scope_extension.start")
    assert start is not None
    assert start.category is NodeCategory.INVALID
    assert start.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_extension_artifact_review_and_rejection_matrix() -> None:
    """Derive challenge, PRD, amendment, review, and revision decisions."""
    base = _with_extension(completed_project_snapshot())
    challenge = ChallengeArtifactFact(
        challenge_artifact_id=701,
        discovery_run_id=EXTENSION_RUN_ID,
        content_fingerprint="sha256:challenge",
        supersedes_id=None,
    )
    with_challenge = base.model_copy(update={"challenge_artifacts": (challenge,)})
    prd = _decision(with_challenge, "scope_extension.prd", f"run:{EXTENSION_RUN_ID}")
    assert prd is not None
    assert prd.category is NodeCategory.AVAILABLE

    prd_fact = PrdVersionFact(
        prd_version_id=702,
        discovery_run_id=EXTENSION_RUN_ID,
        content_fingerprint="sha256:prd",
        supersedes_id=None,
    )
    with_prd = with_challenge.model_copy(update={"prd_versions": (prd_fact,)})
    prd_review = _decision(
        with_prd,
        "scope_extension.prd_review",
        f"prd:{prd_fact.prd_version_id}",
    )
    assert prd_review is not None
    assert prd_review.category is NodeCategory.WAITING

    rejected_prd = with_prd.model_copy(
        update={
            "review_decisions": (
                *with_prd.review_decisions,
                _review(
                    decision_id=703,
                    artifact_type="prd",
                    artifact_id=prd_fact.prd_version_id,
                    fingerprint=prd_fact.content_fingerprint,
                    decision="rejected",
                ),
            )
        }
    )
    prd_retry = _decision(
        rejected_prd,
        "scope_extension.prd",
        f"run:{EXTENSION_RUN_ID}",
    )
    assert prd_retry is not None
    assert prd_retry.category is NodeCategory.AVAILABLE
    assert prd_retry.recommendation_kind is RecommendationKind.RECOVERY

    accepted_prd = with_prd.model_copy(
        update={
            "review_decisions": (
                *with_prd.review_decisions,
                _review(
                    decision_id=704,
                    artifact_type="prd",
                    artifact_id=prd_fact.prd_version_id,
                    fingerprint=prd_fact.content_fingerprint,
                ),
            )
        }
    )
    spec = _decision(
        accepted_prd,
        "scope_extension.spec",
        f"run:{EXTENSION_RUN_ID}",
    )
    assert spec is not None
    assert spec.category is NodeCategory.AVAILABLE

    draft = SpecDraftFact(
        spec_draft_id=AMENDMENT_DRAFT_ID,
        discovery_run_id=EXTENSION_RUN_ID,
        kind="amendment",
        content_fingerprint=REPLACEMENT_SPEC_HASH,
        base_spec_version_id=BASE_SPEC_ID,
        base_spec_hash=BASE_SPEC_HASH,
        supersedes_id=None,
    )
    with_draft = accepted_prd.model_copy(update={"spec_drafts": (draft,)})
    spec_review = _decision(
        with_draft,
        "scope_extension.spec_review",
        f"spec:{AMENDMENT_DRAFT_ID}",
    )
    assert spec_review is not None
    assert spec_review.category is NodeCategory.WAITING

    rejected_draft = with_draft.model_copy(
        update={
            "review_decisions": (
                *with_draft.review_decisions,
                _review(
                    decision_id=705,
                    artifact_type="spec_draft",
                    artifact_id=AMENDMENT_DRAFT_ID,
                    fingerprint=REPLACEMENT_SPEC_HASH,
                    decision="rejected",
                ),
            )
        }
    )
    spec_retry = _decision(
        rejected_draft,
        "scope_extension.spec",
        f"run:{EXTENSION_RUN_ID}",
    )
    assert spec_retry is not None
    assert spec_retry.category is NodeCategory.AVAILABLE
    assert spec_retry.recommendation_kind is RecommendationKind.RECOVERY


def _with_accepted_replacement_scope(
    snapshot: WorkflowFactSnapshot,
) -> WorkflowFactSnapshot:
    """Add exact accepted downstream facts for the replacement authority."""
    authority_fingerprint = "sha256:replacement-authority"
    return snapshot.model_copy(
        update={
            "phase_artifacts": (
                PhaseArtifactFact(
                    artifact_type="vision",
                    artifact_id=901,
                    artifact_fingerprint="sha256:replacement-vision",
                    authority_id=REPLACEMENT_AUTHORITY_ID,
                    authority_fingerprint=authority_fingerprint,
                    status="accepted",
                ),
                PhaseArtifactFact(
                    artifact_type="backlog",
                    artifact_id=902,
                    artifact_fingerprint="sha256:replacement-backlog",
                    authority_id=REPLACEMENT_AUTHORITY_ID,
                    authority_fingerprint=authority_fingerprint,
                    status="accepted",
                ),
            ),
            "planning_artifacts": (
                PlanningArtifactFact(
                    artifact_type="roadmap",
                    artifact_id=903,
                    artifact_fingerprint="sha256:replacement-roadmap",
                    source_artifact_id=902,
                    source_fingerprint="sha256:replacement-backlog",
                    authority_id=REPLACEMENT_AUTHORITY_ID,
                    authority_fingerprint=authority_fingerprint,
                    backlog_artifact_id=902,
                    backlog_artifact_fingerprint="sha256:replacement-backlog",
                    status="accepted",
                ),
                PlanningArtifactFact(
                    artifact_type="story",
                    artifact_id=904,
                    artifact_fingerprint="sha256:replacement-story",
                    source_artifact_id=903,
                    source_fingerprint="sha256:replacement-roadmap",
                    authority_id=REPLACEMENT_AUTHORITY_ID,
                    authority_fingerprint=authority_fingerprint,
                    roadmap_artifact_id=903,
                    roadmap_artifact_fingerprint="sha256:replacement-roadmap",
                    requirement_id="REQ-1",
                    story_ids=(21,),
                    status="accepted",
                ),
                PlanningArtifactFact(
                    artifact_type="sprint_plan",
                    artifact_id=905,
                    artifact_fingerprint="sha256:replacement-sprint-plan",
                    source_artifact_id=904,
                    source_fingerprint="sha256:replacement-story",
                    story_ids=(21,),
                    sprint_id=11,
                    candidate_set_fingerprint="sha256:replacement-candidates",
                    task_content_fingerprint="sha256:replacement-tasks",
                    status="accepted",
                ),
            ),
            "sprint_starts": (
                SprintStartFact(
                    start_id=906,
                    sprint_id=11,
                    sprint_plan_artifact_id=905,
                    sprint_plan_artifact_decision_id=907,
                    story_dependency_review_id=908,
                    plan_fingerprint="sha256:replacement-sprint-plan",
                    candidate_set_fingerprint="sha256:replacement-candidates",
                    selected_story_ids=(21,),
                    task_content_fingerprint="sha256:replacement-tasks",
                    dependency_source_fingerprint="sha256:dependency-source",
                    dependency_fingerprint="sha256:dependencies",
                    dependency_rows_fingerprint="sha256:dependency-rows",
                    decision_fingerprint="sha256:sprint-plan-decision",
                    audit_event_id=909,
                    audit_event_fingerprint="sha256:sprint-start-audit",
                    started_by="operator@example.com",
                    started_at=EVALUATED_AT,
                ),
            ),
        }
    )


def _accepted_amendment_snapshot() -> WorkflowFactSnapshot:
    snapshot = _with_extension(completed_project_snapshot())
    challenge = ChallengeArtifactFact(
        challenge_artifact_id=701,
        discovery_run_id=EXTENSION_RUN_ID,
        content_fingerprint="sha256:challenge",
        supersedes_id=None,
    )
    prd = PrdVersionFact(
        prd_version_id=702,
        discovery_run_id=EXTENSION_RUN_ID,
        content_fingerprint="sha256:prd",
        supersedes_id=None,
    )
    draft = SpecDraftFact(
        spec_draft_id=AMENDMENT_DRAFT_ID,
        discovery_run_id=EXTENSION_RUN_ID,
        kind="amendment",
        content_fingerprint=REPLACEMENT_SPEC_HASH,
        base_spec_version_id=BASE_SPEC_ID,
        base_spec_hash=BASE_SPEC_HASH,
        supersedes_id=None,
    )
    return snapshot.model_copy(
        update={
            "challenge_artifacts": (challenge,),
            "prd_versions": (prd,),
            "spec_drafts": (draft,),
            "review_decisions": (
                *snapshot.review_decisions,
                _review(
                    decision_id=704,
                    artifact_type="prd",
                    artifact_id=prd.prd_version_id,
                    fingerprint=prd.content_fingerprint,
                ),
                _review(
                    decision_id=706,
                    artifact_type="spec_draft",
                    artifact_id=draft.spec_draft_id,
                    fingerprint=draft.content_fingerprint,
                ),
            ),
        }
    )


def test_accepted_amendment_registers_then_reuses_shared_authority_graph() -> None:
    """Register an accepted amendment before shared authority compilation."""
    accepted = _accepted_amendment_snapshot()
    registration = _decision(
        accepted,
        "scope_extension.registration",
        f"run:{EXTENSION_RUN_ID}",
    )
    assert registration is not None
    assert registration.category is NodeCategory.AVAILABLE

    registered = accepted.model_copy(
        update={
            "spec_versions": (
                accepted.spec_versions[0].model_copy(update={"status": "superseded"}),
                SpecVersionFact(
                    spec_version_id=REPLACEMENT_SPEC_ID,
                    spec_hash=REPLACEMENT_SPEC_HASH,
                    status="approved",
                    approved_at=EVALUATED_AT,
                ),
            ),
            "extension_registrations": (
                ScopeExtensionRegistrationFact(
                    registration_id=801,
                    discovery_run_id=EXTENSION_RUN_ID,
                    spec_draft_id=AMENDMENT_DRAFT_ID,
                    spec_version_id=REPLACEMENT_SPEC_ID,
                    spec_hash=REPLACEMENT_SPEC_HASH,
                ),
            ),
        }
    )
    shared_compile = _decision(
        registered,
        "authority.compile",
        f"spec:{REPLACEMENT_SPEC_ID}:{REPLACEMENT_SPEC_HASH}",
    )
    extension_authority = _decision(
        registered,
        "scope_extension.authority",
        f"run:{EXTENSION_RUN_ID}",
    )
    assert shared_compile is not None
    assert shared_compile.category is NodeCategory.AVAILABLE
    assert extension_authority is not None
    assert extension_authority.category is NodeCategory.WAITING


def test_accepted_replacement_authority_exposes_reconciliation() -> None:
    """Require downstream reconciliation after replacement authority acceptance."""
    accepted = _accepted_amendment_snapshot()
    replacement_authority = AuthorityFact(
        authority_id=REPLACEMENT_AUTHORITY_ID,
        spec_version_id=REPLACEMENT_SPEC_ID,
        authority_fingerprint="sha256:replacement-authority",
        status="accepted",
        decided_at=EVALUATED_AT,
    )
    snapshot = _with_accepted_replacement_scope(
        accepted.model_copy(
            update={
                "spec_versions": (
                    accepted.spec_versions[0].model_copy(
                        update={"status": "superseded"}
                    ),
                    SpecVersionFact(
                        spec_version_id=REPLACEMENT_SPEC_ID,
                        spec_hash=REPLACEMENT_SPEC_HASH,
                        status="approved",
                        approved_at=EVALUATED_AT,
                    ),
                ),
                "extension_registrations": (
                    ScopeExtensionRegistrationFact(
                        registration_id=801,
                        discovery_run_id=EXTENSION_RUN_ID,
                        spec_draft_id=AMENDMENT_DRAFT_ID,
                        spec_version_id=REPLACEMENT_SPEC_ID,
                        spec_hash=REPLACEMENT_SPEC_HASH,
                    ),
                ),
                "authorities": (*accepted.authorities, replacement_authority),
                "review_decisions": (
                    *accepted.review_decisions,
                    _review(
                        decision_id=802,
                        artifact_type="authority",
                        artifact_id=REPLACEMENT_AUTHORITY_ID,
                        fingerprint=replacement_authority.authority_fingerprint,
                    ),
                ),
            }
        )
    )

    assert (
        _decision(
            snapshot,
            "scope_extension.authority",
            f"run:{EXTENSION_RUN_ID}",
        )
        is None
    )
    reconciliation = _decision(
        snapshot,
        "scope_extension.reconciliation",
        f"run:{EXTENSION_RUN_ID}",
    )
    assert reconciliation is not None
    assert reconciliation.category is NodeCategory.AVAILABLE


def test_completed_and_abandoned_runs_allow_only_fresh_optional_reentry() -> None:
    """Offer a fresh optional decision after completion or abandonment."""
    accepted = _accepted_amendment_snapshot()
    replacement_authority = AuthorityFact(
        authority_id=REPLACEMENT_AUTHORITY_ID,
        spec_version_id=REPLACEMENT_SPEC_ID,
        authority_fingerprint="sha256:replacement-authority",
        status="accepted",
        decided_at=EVALUATED_AT,
    )
    completed = accepted.model_copy(
        update={
            "discovery_runs": (
                accepted.discovery_runs[0],
                _extension_run(closed=True),
            ),
            "spec_versions": (
                accepted.spec_versions[0].model_copy(update={"status": "superseded"}),
                SpecVersionFact(
                    spec_version_id=REPLACEMENT_SPEC_ID,
                    spec_hash=REPLACEMENT_SPEC_HASH,
                    status="approved",
                    approved_at=EVALUATED_AT,
                ),
            ),
            "extension_registrations": (
                ScopeExtensionRegistrationFact(
                    registration_id=801,
                    discovery_run_id=EXTENSION_RUN_ID,
                    spec_draft_id=AMENDMENT_DRAFT_ID,
                    spec_version_id=REPLACEMENT_SPEC_ID,
                    spec_hash=REPLACEMENT_SPEC_HASH,
                ),
            ),
            "authorities": (*accepted.authorities, replacement_authority),
            "review_decisions": (
                *accepted.review_decisions,
                _review(
                    decision_id=802,
                    artifact_type="authority",
                    artifact_id=REPLACEMENT_AUTHORITY_ID,
                    fingerprint=replacement_authority.authority_fingerprint,
                ),
            ),
            "scope_extension_reconciliations": (
                ScopeExtensionReconciliationFact(
                    reconciliation_id=901,
                    discovery_run_id=EXTENSION_RUN_ID,
                    replacement_authority_id=REPLACEMENT_AUTHORITY_ID,
                    replacement_authority_fingerprint=(
                        replacement_authority.authority_fingerprint
                    ),
                    artifact_references=(
                        FactReference(
                            fact_type="backlog",
                            fact_id="1",
                            fingerprint="sha256:backlog",
                        ),
                    ),
                    reconciled_at=EVALUATED_AT,
                ),
            ),
        }
    )
    start = _decision(completed, "scope_extension.start")
    assert start is not None
    assert start.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert (
        _decision(
            completed,
            "scope_extension.registration",
            f"run:{EXTENSION_RUN_ID}",
        )
        is None
    )

    abandoned = completed_project_snapshot().model_copy(
        update={
            "discovery_runs": (
                *completed_project_snapshot().discovery_runs,
                _extension_run(closed=True),
            ),
            "discovery_run_abandonments": (
                DiscoveryRunAbandonmentFact(
                    discovery_run_abandonment_id=902,
                    project_id=PROJECT_ID,
                    discovery_run_id=EXTENSION_RUN_ID,
                    reason="Scope withdrawn.",
                    abandoned_by="operator@example.com",
                    abandoned_at=EVALUATED_AT,
                ),
            ),
        }
    )
    abandoned_start = _decision(abandoned, "scope_extension.start")
    assert abandoned_start is not None
    assert abandoned_start.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
