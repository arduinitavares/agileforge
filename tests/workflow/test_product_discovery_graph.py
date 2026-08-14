"""Provider-free graph tests for registered-source Specification structuring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from workflow.contracts import RecommendationKind
from workflow.definitions.product_discovery import (
    SPECIFICATION_NODES,
    _review_rule,
    _source_registration_rule,
    _specification_rule,
    accepted_current_spec,
    current_specification_candidate,
    current_specification_source,
)
from workflow.facts import (
    NodeAttemptFact,
    PostSprintTriageFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SpecificationSourceFact,
    SpecVersionFact,
    SprintFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _snapshot(
    *,
    source_registered: bool = False,
    candidate_decision: Literal["accepted", "rejected", "feedback"] | None = None,
    approved_spec: bool = False,
) -> WorkflowFactSnapshot:
    """Build an accepted Vision/Goal with optional candidate and approved spec."""
    vision = VisionArtifactFact(
        vision_artifact_id=1,
        version_number=1,
        components={},
        statement="Vision",
        content_fingerprint="vision",
        vision_evidence_snapshot_id=1,
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator",
        created_at=NOW,
    )
    goal = ProductGoalArtifactFact(
        product_goal_artifact_id=3,
        vision_artifact_id=1,
        vision_fingerprint="vision",
        goal_number=1,
        revision_number=1,
        statement="Goal",
        content_fingerprint="goal",
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=2,
        created_by="operator",
        created_at=NOW,
    )
    sources: tuple[SpecificationSourceFact, ...] = ()
    if source_registered or candidate_decision is not None:
        sources = (
            SpecificationSourceFact(
                specification_source_id=5,
                source_fingerprint="source",
                bundle={},
                repository_binding_id=20,
                repository_head_sha="abc123",
                repository_dirty=False,
                repository_status_fingerprint="status",
                vision_artifact_id=1,
                vision_fingerprint="vision",
                product_goal_artifact_id=3,
                product_goal_fingerprint="goal",
                supersedes_specification_source_id=None,
                supersedes_source_fingerprint=None,
                registered_by="operator",
                registered_at=NOW,
            ),
        )
    candidates: tuple[SpecificationCandidateFact, ...] = ()
    decisions: tuple[SpecificationDecisionFact, ...] = ()
    specs: tuple[SpecVersionFact, ...] = ()
    if candidate_decision is not None:
        candidate = SpecificationCandidateFact(
            specification_candidate_id=6,
            candidate_kind="initial",
            specification_source_id=5,
            specification_source_fingerprint="source",
            vision_artifact_id=1,
            vision_fingerprint="vision",
            product_goal_artifact_id=3,
            product_goal_fingerprint="goal",
            base_spec_version_id=None,
            base_spec_hash=None,
            canonical_envelope={},
            payload_fingerprint="payload",
            source_manifest_fingerprint="sources",
            producer_input_fingerprint="input",
            rendered_view_fingerprint="view",
            candidate_fingerprint="candidate",
            workflow_node_attempt_id=9,
            attempt_fingerprint="attempt",
            supersedes_specification_candidate_id=None,
            supersedes_candidate_fingerprint=None,
            recorded_by="worker",
            recorded_at=NOW,
        )
        candidates = (candidate,)
        decisions = (
            SpecificationDecisionFact(
                specification_decision_id=7,
                specification_candidate_id=6,
                candidate_fingerprint="candidate",
                decision=candidate_decision,
                rationale="revise" if candidate_decision != "accepted" else "",
                reviewer="operator",
                idempotency_key="s",
                decided_at=NOW,
            ),
        )
        if approved_spec:
            specs = (
                SpecVersionFact(
                    spec_version_id=8,
                    spec_hash="payload",
                    status="approved",
                    approved_at=NOW,
                    source_specification_candidate_id=6,
                    source_specification_candidate_fingerprint="candidate",
                    source_vision_artifact_id=1,
                    source_vision_fingerprint="vision",
                    source_product_goal_artifact_id=3,
                    source_product_goal_fingerprint="goal",
                ),
            )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1,
            name="Specification graph",
            created_at=NOW,
            active_repository_binding_id=20,
        ),
        vision_artifacts=(vision,),
        vision_artifact_decisions=(
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=2,
                vision_artifact_id=1,
                artifact_fingerprint="vision",
                decision="accepted",
                rationale="",
                reviewer="operator",
                idempotency_key="v",
                decided_at=NOW,
            ),
        ),
        product_goal_artifacts=(goal,),
        product_goal_artifact_decisions=(
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=4,
                product_goal_artifact_id=3,
                artifact_fingerprint="goal",
                decision="accepted",
                rationale="",
                reviewer="operator",
                idempotency_key="g",
                decided_at=NOW,
            ),
        ),
        specification_sources=sources,
        specification_candidates=candidates,
        specification_decisions=decisions,
        spec_versions=specs,
    )


def _with_replacement_product_lineage(
    snapshot: WorkflowFactSnapshot,
    *,
    repository_binding_id: int = 20,
) -> WorkflowFactSnapshot:
    """Replace Vision, Goal, source, and optionally the active repository binding."""
    original_vision = snapshot.vision_artifacts[0]
    replacement_vision = original_vision.model_copy(
        update={
            "vision_artifact_id": 11,
            "version_number": 2,
            "statement": "Replacement Vision",
            "content_fingerprint": "replacement-vision",
            "supersedes_vision_artifact_id": original_vision.vision_artifact_id,
        }
    )
    replacement_vision_decision = snapshot.vision_artifact_decisions[0].model_copy(
        update={
            "vision_artifact_decision_id": 12,
            "vision_artifact_id": 11,
            "artifact_fingerprint": "replacement-vision",
            "idempotency_key": "replacement-vision",
        }
    )
    original_goal = snapshot.product_goal_artifacts[0]
    replacement_goal = original_goal.model_copy(
        update={
            "product_goal_artifact_id": 13,
            "vision_artifact_id": 11,
            "vision_fingerprint": "replacement-vision",
            "statement": "Replacement Goal",
            "content_fingerprint": "replacement-goal",
            "supersedes_product_goal_artifact_id": None,
        }
    )
    replacement_goal_decision = snapshot.product_goal_artifact_decisions[0].model_copy(
        update={
            "product_goal_artifact_decision_id": 14,
            "product_goal_artifact_id": 13,
            "artifact_fingerprint": "replacement-goal",
            "idempotency_key": "replacement-goal",
        }
    )
    original_source = snapshot.specification_sources[0]
    replacement_source = original_source.model_copy(
        update={
            "specification_source_id": 15,
            "source_fingerprint": "replacement-source",
            "repository_binding_id": repository_binding_id,
            "vision_artifact_id": 11,
            "vision_fingerprint": "replacement-vision",
            "product_goal_artifact_id": 13,
            "product_goal_fingerprint": "replacement-goal",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
        }
    )
    return snapshot.model_copy(
        update={
            "project": snapshot.project.model_copy(
                update={"active_repository_binding_id": repository_binding_id}
            ),
            "vision_artifacts": (original_vision, replacement_vision),
            "vision_artifact_decisions": (
                *snapshot.vision_artifact_decisions,
                replacement_vision_decision,
            ),
            "product_goal_artifacts": (original_goal, replacement_goal),
            "product_goal_artifact_decisions": (
                *snapshot.product_goal_artifact_decisions,
                replacement_goal_decision,
            ),
            "specification_sources": (original_source, replacement_source),
        }
    )


def test_no_registered_to_spec_source_makes_structuring_unavailable() -> None:
    """Accepted Vision/Goal exposes source registration, never direct structuring."""
    snapshot = _snapshot()
    source = _source_registration_rule(snapshot, NOW)[0]
    structure = _specification_rule(snapshot, NOW)[0]
    nodes = {node.node_id: node for node in SPECIFICATION_NODES}

    assert source.reason_code == "SPECIFICATION_SOURCE_REQUIRED"
    assert source.category.value == "available"
    assert structure.reason_code == "SPECIFICATION_SOURCE_NOT_REGISTERED"
    assert structure.category.value == "satisfied"
    references = {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in source.fact_references
    }
    assert references == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
    }
    assert set(nodes) == {
        "specification.source.register",
        "specification.structure",
        "specification.review",
    }
    assert nodes["specification.source.register"].request_kind == (
        "register_specification_source"
    )
    assert nodes["specification.source.register"].agentic_execution is None
    assert {
        field.name for field in nodes["specification.source.register"].required_inputs
    } == {"source_path", "preparation_capability", "adr_path"}
    assert nodes["specification.structure"].request_kind == "structure_specification"
    assert nodes["specification.structure"].agentic_execution is not None
    assert "canonical_content" not in {
        field.name for field in nodes["specification.structure"].required_inputs
    }


def test_valid_registered_source_enables_structuring_with_exact_reference() -> None:
    """The structurer is gated by one current immutable source registration."""
    snapshot = _snapshot(source_registered=True)
    structure = _specification_rule(snapshot, NOW)[0]

    assert current_specification_source(snapshot) is not None
    assert structure.reason_code == "SPECIFICATION_INITIAL_REQUIRED"
    assert structure.category.value == "available"
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in structure.fact_references
    } == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
        ("specification_source", "5", "source"),
    }


def test_pending_candidate_waits_for_review_by_exact_candidate_fingerprint() -> None:
    """The review gate references the candidate identity, not payload content."""
    snapshot = _snapshot(source_registered=True, candidate_decision=None)
    candidate = _snapshot(candidate_decision="accepted").specification_candidates[0]
    snapshot = snapshot.model_copy(update={"specification_candidates": (candidate,)})

    assert _specification_rule(snapshot, NOW)[0].reason_code == (
        "SPECIFICATION_REVIEW_PENDING"
    )
    review = _review_rule(snapshot, NOW)[0]
    assert review.reason_code == "SPECIFICATION_REVIEW_REQUIRED"
    assert review.fact_references[0].fingerprint == "candidate"


def test_rejected_candidate_requires_a_replacement_registered_source() -> None:
    """Rejection reopens external source preparation before another structuring call."""
    snapshot = _snapshot(candidate_decision="rejected")
    source = _source_registration_rule(snapshot, NOW)[0]
    structure = _specification_rule(snapshot, NOW)[0]

    assert source.reason_code == "SPECIFICATION_REJECTED_SOURCE_REVISION_REQUIRED"
    assert source.category.value == "available"
    assert structure.reason_code == "SPECIFICATION_SOURCE_REVISION_REQUIRED"
    assert structure.category.value == "satisfied"
    assert {(item.fact_type, item.fingerprint) for item in source.fact_references} == {
        ("vision", "vision"),
        ("product_goal", "goal"),
        ("specification_source", "source"),
        ("specification_candidate", "candidate"),
    }


def test_feedback_exposes_same_source_retry_and_optional_source_revision() -> None:
    """Feedback offers an exact retry or a genuinely revised external source."""
    snapshot = _snapshot(candidate_decision="feedback")

    source = _source_registration_rule(snapshot, NOW)[0]
    structure = _specification_rule(snapshot, NOW)[0]

    assert source.reason_code == "SPECIFICATION_FEEDBACK_SOURCE_REVISION_AVAILABLE"
    assert source.category.value == "available"
    assert source.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert structure.reason_code == "SPECIFICATION_FEEDBACK_RETRY_AVAILABLE"
    assert structure.category.value == "available"
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in structure.fact_references
    } == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
        ("specification_source", "5", "source"),
        ("specification_candidate", "6", "candidate"),
    }


def test_active_feedback_retry_closes_revised_source_choice() -> None:
    """One live structuring lease prevents choosing source replacement concurrently."""
    snapshot = _snapshot(candidate_decision="feedback").model_copy(
        update={
            "node_attempts": (
                NodeAttemptFact(
                    attempt_id=9,
                    node_id="specification.structure",
                    instance_key=None,
                    graph_version="agileforge.workflow.v2",
                    input_fingerprint="input",
                    fact_fingerprint="facts",
                    business_fact_fingerprint="business",
                    decision_fingerprint="decision",
                    attempt_fingerprint="attempt",
                    model_id="fake/model",
                    lease_expires_at=NOW + timedelta(minutes=5),
                    outcome=None,
                ),
            )
        }
    )

    source = _source_registration_rule(snapshot, NOW)[0]

    assert source.category.value == "waiting"
    assert source.reason_code == "SPECIFICATION_STRUCTURER_ACTIVE"
    assert source.valid_until == NOW + timedelta(minutes=5)


def test_replacement_source_enables_revision_with_prior_feedback_reference() -> None:
    """A new external source carries the exact rejected candidate into structuring."""
    snapshot = _snapshot(candidate_decision="feedback")
    original_source = snapshot.specification_sources[0]
    replacement = original_source.model_copy(
        update={
            "specification_source_id": 15,
            "source_fingerprint": "replacement-source",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
            "registered_at": NOW.replace(hour=1),
        }
    )
    snapshot = snapshot.model_copy(
        update={"specification_sources": (original_source, replacement)}
    )

    structure = _specification_rule(snapshot, NOW.replace(hour=2))[0]

    assert structure.reason_code == "SPECIFICATION_REVISION_REQUIRED"
    assert structure.category.value == "available"
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in structure.fact_references
    } == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
        ("specification_source", "15", "replacement-source"),
        ("specification_candidate", "6", "candidate"),
    }


def test_optional_replacements_preserve_exact_ancestor_feedback_reference() -> None:
    """Source re-entry without a candidate does not discard prior feedback."""
    snapshot = _snapshot(candidate_decision="feedback")
    original = snapshot.specification_sources[0]
    second = original.model_copy(
        update={
            "specification_source_id": 15,
            "source_fingerprint": "second-source",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
            "registered_at": NOW.replace(hour=1),
        }
    )
    with_second = snapshot.model_copy(
        update={"specification_sources": (original, second)}
    )
    reentry = _source_registration_rule(with_second, NOW.replace(hour=2))[0]
    third = second.model_copy(
        update={
            "specification_source_id": 16,
            "source_fingerprint": "third-source",
            "supersedes_specification_source_id": 15,
            "supersedes_source_fingerprint": "second-source",
            "registered_at": NOW.replace(hour=3),
        }
    )
    with_third = snapshot.model_copy(
        update={"specification_sources": (original, second, third)}
    )

    structure = _specification_rule(with_third, NOW.replace(hour=4))[0]

    assert reentry.reason_code == "SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE"
    assert reentry.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert structure.reason_code == "SPECIFICATION_REVISION_REQUIRED"
    assert structure.category.value == "available"
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in structure.fact_references
    } == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
        ("specification_source", "16", "third-source"),
        ("specification_candidate", "6", "candidate"),
    }


def test_ambiguous_ancestor_feedback_fails_structuring_closed() -> None:
    """Two terminal candidates on one ancestor never select feedback by order."""
    snapshot = _snapshot(candidate_decision="feedback")
    original = snapshot.specification_sources[0]
    replacement = original.model_copy(
        update={
            "specification_source_id": 15,
            "source_fingerprint": "replacement-source",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
        }
    )
    first_candidate = snapshot.specification_candidates[0]
    second_candidate = first_candidate.model_copy(
        update={
            "specification_candidate_id": 16,
            "candidate_fingerprint": "second-candidate",
        }
    )
    second_decision = snapshot.specification_decisions[0].model_copy(
        update={
            "specification_decision_id": 17,
            "specification_candidate_id": 16,
            "candidate_fingerprint": "second-candidate",
        }
    )
    conflicted = snapshot.model_copy(
        update={
            "specification_sources": (original, replacement),
            "specification_candidates": (first_candidate, second_candidate),
            "specification_decisions": (
                snapshot.specification_decisions[0],
                second_decision,
            ),
        }
    )

    structure = _specification_rule(conflicted, NOW)[0]

    assert structure.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert structure.category.value == "invalid"


def test_replacement_source_uses_latest_same_source_feedback_retry() -> None:
    """A linear retry chain selects its exact terminal leaf on source replacement."""
    snapshot = _snapshot(candidate_decision="feedback")
    original = snapshot.specification_sources[0]
    replacement = original.model_copy(
        update={
            "specification_source_id": 18,
            "source_fingerprint": "replacement-source",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
        }
    )
    first_candidate = snapshot.specification_candidates[0]
    second_candidate = first_candidate.model_copy(
        update={
            "specification_candidate_id": 16,
            "candidate_fingerprint": "second-candidate",
            "supersedes_specification_candidate_id": (
                first_candidate.specification_candidate_id
            ),
            "supersedes_candidate_fingerprint": (first_candidate.candidate_fingerprint),
        }
    )
    second_decision = snapshot.specification_decisions[0].model_copy(
        update={
            "specification_decision_id": 17,
            "specification_candidate_id": 16,
            "candidate_fingerprint": "second-candidate",
        }
    )
    retried = snapshot.model_copy(
        update={
            "specification_sources": (original, replacement),
            "specification_candidates": (first_candidate, second_candidate),
            "specification_decisions": (
                snapshot.specification_decisions[0],
                second_decision,
            ),
        }
    )

    structure = _specification_rule(retried, NOW)[0]

    assert structure.reason_code == "SPECIFICATION_REVISION_REQUIRED"
    assert structure.category.value == "available"
    assert (
        "specification_candidate",
        "16",
        "second-candidate",
    ) in {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in structure.fact_references
    }


def test_candidate_tail_into_cycle_fails_ancestor_selection_closed() -> None:
    """One apparent leaf cannot legitimize a cyclic same-source predecessor chain."""
    snapshot = _snapshot(candidate_decision="feedback")
    original = snapshot.specification_sources[0]
    replacement = original.model_copy(
        update={
            "specification_source_id": 19,
            "source_fingerprint": "replacement-source",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
        }
    )
    template = snapshot.specification_candidates[0]
    candidate_a = template.model_copy(
        update={
            "specification_candidate_id": 16,
            "candidate_fingerprint": "candidate-a",
            "supersedes_specification_candidate_id": 17,
            "supersedes_candidate_fingerprint": "candidate-b",
        }
    )
    candidate_b = template.model_copy(
        update={
            "specification_candidate_id": 17,
            "candidate_fingerprint": "candidate-b",
            "supersedes_specification_candidate_id": 16,
            "supersedes_candidate_fingerprint": "candidate-a",
        }
    )
    candidate_c = template.model_copy(
        update={
            "specification_candidate_id": 18,
            "candidate_fingerprint": "candidate-c",
            "supersedes_specification_candidate_id": 16,
            "supersedes_candidate_fingerprint": "candidate-a",
        }
    )
    decision_template = snapshot.specification_decisions[0]
    decisions = tuple(
        decision_template.model_copy(
            update={
                "specification_decision_id": 20 + offset,
                "specification_candidate_id": candidate.specification_candidate_id,
                "candidate_fingerprint": candidate.candidate_fingerprint,
            }
        )
        for offset, candidate in enumerate((candidate_a, candidate_b, candidate_c))
    )
    malformed = snapshot.model_copy(
        update={
            "specification_sources": (original, replacement),
            "specification_candidates": (candidate_a, candidate_b, candidate_c),
            "specification_decisions": decisions,
        }
    )

    structure = _specification_rule(malformed, NOW)[0]

    assert structure.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert structure.category.value == "invalid"


def test_cycle_in_source_ancestor_chain_fails_structuring_closed() -> None:
    """A malformed ancestor cycle cannot produce an initial or revision attempt."""
    snapshot = _snapshot(candidate_decision="feedback")
    original = snapshot.specification_sources[0]
    cycle_a = original.model_copy(
        update={
            "supersedes_specification_source_id": 16,
            "supersedes_source_fingerprint": "cycle-b",
        }
    )
    cycle_b = original.model_copy(
        update={
            "specification_source_id": 16,
            "source_fingerprint": "cycle-b",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
        }
    )
    current = original.model_copy(
        update={
            "specification_source_id": 17,
            "source_fingerprint": "current-source",
            "supersedes_specification_source_id": 5,
            "supersedes_source_fingerprint": "source",
        }
    )
    conflicted = snapshot.model_copy(
        update={
            "specification_sources": (cycle_a, cycle_b, current),
        }
    )

    structure = _specification_rule(conflicted, NOW)[0]

    assert structure.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert structure.category.value == "invalid"


def test_cross_lineage_replacement_source_starts_initial_structuring() -> None:
    """Rejected work from an old Vision/Goal cannot become revision context."""
    snapshot = _with_replacement_product_lineage(
        _snapshot(candidate_decision="rejected")
    )

    structure = _specification_rule(snapshot, NOW)[0]

    assert structure.reason_code == "SPECIFICATION_INITIAL_REQUIRED"
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in structure.fact_references
    } == {
        ("vision", "11", "replacement-vision"),
        ("product_goal", "13", "replacement-goal"),
        ("specification_source", "15", "replacement-source"),
    }


def test_pending_candidate_remains_review_target_after_binding_drift() -> None:
    """Repository drift blocks acceptance downstream, not exact human feedback."""
    snapshot = _snapshot(source_registered=True)
    candidate = _snapshot(candidate_decision="accepted").specification_candidates[0]
    drifted = snapshot.model_copy(
        update={
            "project": snapshot.project.model_copy(
                update={"active_repository_binding_id": 21}
            ),
            "specification_candidates": (candidate,),
        }
    )

    review = _review_rule(drifted, NOW)[0]
    source = _source_registration_rule(drifted, NOW)[0]

    assert current_specification_candidate(drifted) == candidate
    assert review.reason_code == "SPECIFICATION_REVIEW_REQUIRED"
    assert review.category.value == "waiting"
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in review.fact_references
    } == {("specification_candidate", "6", "candidate")}
    assert source.reason_code == "SPECIFICATION_REVIEW_PENDING"
    assert source.category.value == "satisfied"


def test_pending_candidate_remains_review_target_after_product_lineage_drift() -> None:
    """A sole pending candidate stays reviewable after Vision and Goal replacement."""
    snapshot = _snapshot(source_registered=True)
    candidate = _snapshot(candidate_decision="accepted").specification_candidates[0]
    drifted = _with_replacement_product_lineage(
        snapshot.model_copy(update={"specification_candidates": (candidate,)})
    )

    review = _review_rule(drifted, NOW)[0]

    assert current_specification_candidate(drifted) == candidate
    assert review.reason_code == "SPECIFICATION_REVIEW_REQUIRED"
    assert review.category.value == "waiting"
    assert review.fact_references[0].fingerprint == "candidate"


def test_registered_source_without_candidate_allows_optional_replacement() -> None:
    """Pre-candidate source drift can be replaced without pretending it is required."""
    snapshot = _snapshot(source_registered=True)

    source = _source_registration_rule(snapshot, NOW)[0]

    assert source.reason_code == "SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE"
    assert source.category.value == "available"
    assert source.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in source.fact_references
    } == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
        ("specification_source", "5", "source"),
    }


def test_later_triaged_sprint_reopens_source_registration_before_amendment() -> None:
    """A quiet later increment requires a fresh external source before structuring."""
    snapshot = _snapshot(candidate_decision="accepted", approved_spec=True).model_copy(
        update={
            "sprints": (
                SprintFact(
                    sprint_id=10,
                    status="completed",
                    completed_at=NOW.replace(hour=1),
                ),
            ),
            "post_sprint_triage": (
                PostSprintTriageFact(
                    triage_id=11,
                    sprint_id=10,
                    impact="specification",
                    canonical_payload={},
                    payload_fingerprint="triage",
                ),
            ),
        }
    )
    source = _source_registration_rule(snapshot, NOW.replace(hour=2))[0]
    structure = _specification_rule(snapshot, NOW.replace(hour=2))[0]

    assert accepted_current_spec(snapshot) is not None
    assert source.reason_code == "SPECIFICATION_SOURCE_AMENDMENT_AVAILABLE"
    assert source.category.value == "available"
    assert structure.reason_code == "SPECIFICATION_SOURCE_AMENDMENT_REQUIRED"
    assert structure.category.value == "satisfied"
    references = {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in source.fact_references
    }
    assert references == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
        ("specification_source", "5", "source"),
        ("specification", "8", "payload"),
    }


def test_newer_amendment_outweighs_a_candidate_with_superseded_registry_row() -> None:
    """An amendment does not need a candidate-supersession pointer to be current."""
    baseline = _snapshot(candidate_decision="accepted", approved_spec=True)
    original = baseline.specification_candidates[0]
    amendment = original.model_copy(
        update={
            "specification_candidate_id": 12,
            "candidate_kind": "amendment",
            "base_spec_version_id": 8,
            "base_spec_hash": "payload",
            "candidate_fingerprint": "amendment",
            "recorded_at": NOW.replace(hour=1),
        }
    )
    superseded_base = baseline.spec_versions[0].model_copy(
        update={"status": "superseded", "approved_at": NOW.replace(hour=1)}
    )
    amended_spec = superseded_base.model_copy(
        update={
            "spec_version_id": 13,
            "status": "approved",
            "approved_at": NOW.replace(hour=2),
            "source_specification_candidate_id": 12,
            "source_specification_candidate_fingerprint": "amendment",
            "supersedes_spec_version_id": 8,
        }
    )
    amendment_decision = baseline.specification_decisions[0].model_copy(
        update={
            "specification_decision_id": 14,
            "specification_candidate_id": 12,
            "candidate_fingerprint": "amendment",
        }
    )
    snapshot = baseline.model_copy(
        update={
            "specification_candidates": (original, amendment),
            "specification_decisions": (
                baseline.specification_decisions[0],
                amendment_decision,
            ),
            "spec_versions": (superseded_base, amended_spec),
        }
    )

    assert current_specification_candidate(snapshot) == amendment
    assert accepted_current_spec(snapshot) == amended_spec


def test_conflicting_direct_candidate_lineage_fails_closed() -> None:
    """A candidate with a stale Goal fingerprint cannot produce graph work."""
    snapshot = _snapshot(candidate_decision="rejected")
    malformed = snapshot.specification_candidates[0].model_copy(
        update={"product_goal_fingerprint": "wrong-goal"}
    )
    conflicted = snapshot.model_copy(update={"specification_candidates": (malformed,)})

    assert current_specification_candidate(conflicted) is None
    assert _specification_rule(conflicted, NOW)[0].reason_code == (
        "WORKFLOW_FACT_CONFLICT"
    )
