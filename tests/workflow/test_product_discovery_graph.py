"""Provider-free graph tests for discovery and specification rules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from workflow.definitions.product_discovery import (
    _discovery_rule,
    _review_rule,
    _specification_rule,
)
from workflow.facts import (
    DiscoveryArtifactFact,
    PostSprintTriageFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SprintFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _snapshot(
    *,
    candidate_decision: Literal["accepted", "rejected", "feedback"] | None = None,
) -> WorkflowFactSnapshot:
    """Build a complete accepted Vision/Goal chain with one discovery artifact."""
    vision = VisionArtifactFact(
        vision_artifact_id=1,
        version_number=1,
        components={},
        statement="Vision",
        content_fingerprint="vision",
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator",
        created_at=NOW,
    )
    vision_review = VisionArtifactDecisionFact(
        vision_artifact_decision_id=2,
        vision_artifact_id=1,
        artifact_fingerprint="vision",
        decision="accepted",
        rationale="",
        reviewer="operator",
        idempotency_key="v",
        decided_at=NOW,
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
    goal_review = ProductGoalArtifactDecisionFact(
        product_goal_artifact_decision_id=4,
        product_goal_artifact_id=3,
        artifact_fingerprint="goal",
        decision="accepted",
        rationale="",
        reviewer="operator",
        idempotency_key="g",
        decided_at=NOW,
    )
    discovery = DiscoveryArtifactFact(
        discovery_artifact_id=5,
        vision_artifact_id=1,
        vision_fingerprint="vision",
        product_goal_artifact_id=3,
        product_goal_fingerprint="goal",
        content_fingerprint="discovery",
        content_ref=None,
        producer="grill-me-with-docs",
        supersedes_discovery_artifact_id=None,
        recorded_by="operator",
        recorded_at=NOW,
    )
    candidate = SpecificationCandidateFact(
        specification_candidate_id=6,
        vision_artifact_id=1,
        vision_fingerprint="vision",
        product_goal_artifact_id=3,
        product_goal_fingerprint="goal",
        discovery_artifact_id=5,
        discovery_fingerprint="discovery",
        base_spec_version_id=None,
        base_spec_hash=None,
        content_fingerprint="spec",
        content_ref=None,
        supersedes_specification_candidate_id=None,
        recorded_by="operator",
        recorded_at=NOW,
    )
    decisions = ()
    if candidate_decision is not None:
        decisions = (
            SpecificationDecisionFact(
                specification_decision_id=7,
                specification_candidate_id=6,
                artifact_fingerprint="spec",
                decision=candidate_decision,
                rationale="revise" if candidate_decision != "accepted" else "",
                reviewer="operator",
                idempotency_key="s",
                decided_at=NOW,
            ),
        )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1,
            name="Discovery graph",
            created_at=NOW,
        ),
        vision_artifacts=(vision,),
        vision_artifact_decisions=(vision_review,),
        product_goal_artifacts=(goal,),
        product_goal_artifact_decisions=(goal_review,),
        discovery_artifacts=(discovery,),
        specification_candidates=(candidate,),
        specification_decisions=decisions,
    )


def test_discovery_and_specification_are_exactly_chained() -> None:
    """The current discovery supplies the sole specification parent reference."""
    snapshot = _snapshot()

    assert _discovery_rule(snapshot, NOW)[0].reason_code == "DISCOVERY_RECORDED"
    assert (
        _specification_rule(snapshot, NOW)[0].reason_code
        == "SPECIFICATION_REVIEW_PENDING"
    )
    review = _review_rule(snapshot, NOW)[0]
    assert review.reason_code == "SPECIFICATION_REVIEW_REQUIRED"
    assert review.fact_references[0].fingerprint == "spec"


@pytest.mark.parametrize(
    ("candidate_decision", "record_reason", "review_reason"),
    [
        (
            "accepted",
            "SPECIFICATION_ACCEPTED",
            "SPECIFICATION_REVIEW_ACCEPTED",
        ),
        (
            "rejected",
            "SPECIFICATION_REJECTED_REPLACEMENT_REQUIRED",
            "SPECIFICATION_REVIEW_REJECTED",
        ),
        (
            "feedback",
            "SPECIFICATION_FEEDBACK_REPLACEMENT_REQUIRED",
            "SPECIFICATION_REVIEW_FEEDBACK",
        ),
    ],
)
def test_terminal_specification_decisions_remain_distinguishable(
    candidate_decision: Literal["accepted", "rejected", "feedback"],
    record_reason: str,
    review_reason: str,
) -> None:
    """Pending, accepted, rejected, and feedback states drive different rules."""
    snapshot = _snapshot(candidate_decision=candidate_decision)

    assert _specification_rule(snapshot, NOW)[0].reason_code == record_reason
    assert _review_rule(snapshot, NOW)[0].reason_code == review_reason


@pytest.mark.parametrize(
    "update",
    [
        {"vision_fingerprint": "wrong-vision"},
        {"product_goal_fingerprint": "wrong-goal"},
    ],
)
def test_mismatched_discovery_parent_fails_closed(update: dict[str, str]) -> None:
    """A selected Goal chain never treats a mismatched discovery as a new action."""
    snapshot = _snapshot()
    malformed = snapshot.discovery_artifacts[0].model_copy(update=update)
    conflicted = snapshot.model_copy(update={"discovery_artifacts": (malformed,)})

    assert _discovery_rule(conflicted, NOW)[0].reason_code == "WORKFLOW_FACT_CONFLICT"
    assert _discovery_rule(conflicted, NOW)[0].category.value == "invalid"


@pytest.mark.parametrize(
    "update",
    [
        {"vision_fingerprint": "wrong-vision"},
        {"product_goal_fingerprint": "wrong-goal"},
        {"discovery_fingerprint": "wrong-discovery"},
    ],
)
def test_mismatched_candidate_parent_fails_closed(update: dict[str, str]) -> None:
    """Every candidate parent is exact before record/review rules are exposed."""
    snapshot = _snapshot()
    malformed = snapshot.specification_candidates[0].model_copy(update=update)
    conflicted = snapshot.model_copy(update={"specification_candidates": (malformed,)})

    assert (
        _specification_rule(conflicted, NOW)[0].reason_code == "WORKFLOW_FACT_CONFLICT"
    )
    assert _review_rule(conflicted, NOW)[0].reason_code == "WORKFLOW_FACT_CONFLICT"


def test_duplicate_unsuperseded_discovery_leaf_fails_closed() -> None:
    """Ambiguous leaves are invalid facts, not another discovery opportunity."""
    snapshot = _snapshot()
    duplicate = snapshot.discovery_artifacts[0].model_copy(
        update={"discovery_artifact_id": 8, "content_fingerprint": "duplicate"}
    )
    conflicted = snapshot.model_copy(
        update={"discovery_artifacts": (*snapshot.discovery_artifacts, duplicate)}
    )

    assert _discovery_rule(conflicted, NOW)[0].reason_code == "WORKFLOW_FACT_CONFLICT"


def test_triaged_later_sprint_reopens_discovery_once_under_same_goal() -> None:
    """A completed increment exposes one superseding discovery opportunity."""
    current = _snapshot(candidate_decision="accepted")
    old_discovery = current.discovery_artifacts[0].model_copy(
        update={"recorded_at": NOW}
    )
    after_triage = current.model_copy(
        update={
            "discovery_artifacts": (old_discovery,),
            "sprints": (
                SprintFact(
                    sprint_id=8,
                    status="completed",
                    completed_at=NOW.replace(hour=1),
                ),
            ),
            "post_sprint_triage": (
                PostSprintTriageFact(
                    triage_id=9,
                    sprint_id=8,
                    impact="none",
                    canonical_payload={},
                    payload_fingerprint="triage-fingerprint",
                ),
            ),
        }
    )

    decision = _discovery_rule(after_triage, NOW.replace(hour=2))[0]

    assert decision.reason_code == "DISCOVERY_INCREMENT_AVAILABLE"
    assert {
        (reference.fact_type, reference.fact_id, reference.fingerprint)
        for reference in decision.fact_references
    } == {
        ("vision", "1", "vision"),
        ("product_goal", "3", "goal"),
        ("discovery", "5", "discovery"),
    }

    replacement = old_discovery.model_copy(
        update={
            "discovery_artifact_id": 10,
            "content_fingerprint": "next-discovery",
            "supersedes_discovery_artifact_id": 5,
            "recorded_at": NOW.replace(hour=2),
        }
    )
    reopened = after_triage.model_copy(
        update={"discovery_artifacts": (old_discovery, replacement)}
    )

    assert _discovery_rule(reopened, NOW.replace(hour=2))[0].reason_code == (
        "DISCOVERY_RECORDED"
    )
