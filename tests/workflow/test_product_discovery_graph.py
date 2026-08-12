"""Provider-free graph tests for direct specification authoring."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from workflow.definitions.product_discovery import (
    SPECIFICATION_NODES,
    _review_rule,
    _specification_rule,
    accepted_current_spec,
    current_specification_candidate,
)
from workflow.facts import (
    PostSprintTriageFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SpecVersionFact,
    SprintFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _snapshot(
    *,
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
    candidates: tuple[SpecificationCandidateFact, ...] = ()
    decisions: tuple[SpecificationDecisionFact, ...] = ()
    specs: tuple[SpecVersionFact, ...] = ()
    if candidate_decision is not None:
        candidate = SpecificationCandidateFact(
            specification_candidate_id=6,
            candidate_kind="initial",
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
        project=ProjectFact(project_id=1, name="Specification graph", created_at=NOW),
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
        specification_candidates=candidates,
        specification_decisions=decisions,
        spec_versions=specs,
    )


def test_accepted_goal_routes_directly_to_agentic_specification_author() -> None:
    """A direct Vision/Goal chain exposes initial authoring without Discovery."""
    snapshot = _snapshot()
    author = _specification_rule(snapshot, NOW)[0]
    nodes = {node.node_id: node for node in SPECIFICATION_NODES}

    assert author.reason_code == "SPECIFICATION_INITIAL_REQUIRED"
    references = {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in author.fact_references
    }
    assert references == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
    }
    assert set(nodes) == {"specification.author", "specification.review"}
    assert nodes["specification.author"].request_kind == "author_specification"
    assert nodes["specification.author"].agentic_execution is not None
    assert "canonical_content" not in {
        field.name for field in nodes["specification.author"].required_inputs
    }


def test_pending_candidate_waits_for_review_by_exact_candidate_fingerprint() -> None:
    """The review gate references the candidate identity, not payload content."""
    snapshot = _snapshot(candidate_decision=None)
    candidate = _snapshot(candidate_decision="accepted").specification_candidates[0]
    snapshot = snapshot.model_copy(update={"specification_candidates": (candidate,)})

    assert _specification_rule(snapshot, NOW)[0].reason_code == (
        "SPECIFICATION_REVIEW_PENDING"
    )
    review = _review_rule(snapshot, NOW)[0]
    assert review.reason_code == "SPECIFICATION_REVIEW_REQUIRED"
    assert review.fact_references[0].fingerprint == "candidate"


def test_rejected_candidate_reopens_author_with_prior_candidate_reference() -> None:
    """Revision authoring keeps the rejected candidate as exact reviewer context."""
    snapshot = _snapshot(candidate_decision="rejected")
    author = _specification_rule(snapshot, NOW)[0]

    assert author.reason_code == "SPECIFICATION_REJECTED_REVISION_REQUIRED"
    assert {(item.fact_type, item.fingerprint) for item in author.fact_references} == {
        ("vision", "vision"),
        ("product_goal", "goal"),
        ("specification_candidate", "candidate"),
    }


def test_later_triaged_sprint_reopens_author_as_amendment_from_exact_base() -> None:
    """A quiet later increment permits one amendment from the approved base."""
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
    author = _specification_rule(snapshot, NOW.replace(hour=2))[0]

    assert accepted_current_spec(snapshot) is not None
    assert author.reason_code == "SPECIFICATION_AMENDMENT_REQUIRED"
    references = {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in author.fact_references
    }
    assert references == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
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
