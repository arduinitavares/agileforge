"""Backlog delivery-lineage graph tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest
from pydantic import ValidationError

from services.contracts.backlog import BacklogAgentOutput
from services.contracts.roadmap import RoadmapBuilderOutput
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)
from workflow.definitions.root import project_graph
from workflow.facts import (
    NodeAttemptFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SpecVersionFact,
    SprintFact,
    SprintStartFact,
    StoryDependencyFact,
    StoryDependencyReviewFact,
    StoryFact,
    TaskFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import business_fact_fingerprint
from workflow.graph import (
    ChildGraphSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
PROJECT_ID = 10
SPEC_ID = 101
SPEC_HASH = "sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
CANDIDATE_FINGERPRINT = (
    "sha256:f8714ebde7f56a1de259fa8df4283be6521881a814e036df5c61d33a1a1110ee"
)
GOAL_ID = 301
GOAL_FINGERPRINT = "sha256:goal-current"
PRODUCT_VISION_ID = 401
PRODUCT_VISION_FINGERPRINT = "sha256:product-vision-current"
BACKLOG_ID = 501
BACKLOG_FINGERPRINT = "sha256:backlog-current"


def _artifact(
    *,
    status: str,
    spec_version_id: int = SPEC_ID,
    spec_hash: str = SPEC_HASH,
    goal_id: int = GOAL_ID,
    goal_fingerprint: str = GOAL_FINGERPRINT,
) -> PhaseArtifactFact:
    return PhaseArtifactFact.model_validate(
        {
            "artifact_type": "backlog",
            "artifact_id": BACKLOG_ID,
            "artifact_fingerprint": BACKLOG_FINGERPRINT,
            "version_number": 1,
            "spec_version_id": spec_version_id,
            "spec_hash": spec_hash,
            "product_goal_artifact_id": goal_id,
            "product_goal_fingerprint": goal_fingerprint,
            "status": status,
        }
    )


def _snapshot(
    *,
    authorities: tuple[object, ...] = (),
    backlog: PhaseArtifactFact | None = None,
    active_goal: bool = True,
    vision_decision: Literal["accepted", "feedback", "rejected"] = "accepted",
    attempts: tuple[NodeAttemptFact, ...] = (),
) -> WorkflowFactSnapshot:
    assert authorities == ()
    vision = VisionArtifactFact(
        vision_artifact_id=PRODUCT_VISION_ID,
        version_number=1,
        components={},
        statement="Reliable delivery decisions.",
        content_fingerprint=PRODUCT_VISION_FINGERPRINT,
        vision_evidence_snapshot_id=1,
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    goal = ProductGoalArtifactFact(
        product_goal_artifact_id=GOAL_ID,
        vision_artifact_id=PRODUCT_VISION_ID,
        vision_fingerprint=PRODUCT_VISION_FINGERPRINT,
        goal_number=1,
        revision_number=1,
        statement="Deliver one durable workflow lineage.",
        content_fingerprint=GOAL_FINGERPRINT,
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    review_decisions = (
        (
            ReviewDecisionFact(
                decision_id=BACKLOG_ID,
                artifact_type="backlog",
                artifact_id=BACKLOG_ID,
                artifact_fingerprint=BACKLOG_FINGERPRINT,
                decision=cast(
                    "Literal['accepted', 'rejected', 'feedback']", backlog.status
                ),
                decided_at=EVALUATED_AT,
            ),
        )
        if backlog is not None
        and backlog.status in {"accepted", "rejected", "feedback"}
        else ()
    )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Backlog lineage",
            created_at=EVALUATED_AT,
        ),
        specification_candidates=(
            SpecificationCandidateFact(
                specification_candidate_id=2,
                candidate_kind="initial",
                specification_source_id=1,
                specification_source_fingerprint="sha256:source",
                vision_artifact_id=PRODUCT_VISION_ID,
                vision_fingerprint=PRODUCT_VISION_FINGERPRINT,
                product_goal_artifact_id=GOAL_ID,
                product_goal_fingerprint=GOAL_FINGERPRINT,
                base_spec_version_id=None,
                base_spec_hash=None,
                canonical_envelope={},
                payload_fingerprint=SPEC_HASH,
                source_manifest_fingerprint="sha256:manifest",
                producer_input_fingerprint="sha256:producer",
                rendered_view_fingerprint="sha256:rendered",
                candidate_fingerprint=CANDIDATE_FINGERPRINT,
                workflow_node_attempt_id=1,
                attempt_fingerprint="sha256:attempt",
                supersedes_specification_candidate_id=None,
                supersedes_candidate_fingerprint=None,
                recorded_by="operator",
                recorded_at=EVALUATED_AT,
            ),
        ),
        specification_decisions=(
            SpecificationDecisionFact(
                specification_decision_id=3,
                specification_candidate_id=2,
                candidate_fingerprint=CANDIDATE_FINGERPRINT,
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator",
                idempotency_key="specification",
                decided_at=EVALUATED_AT,
            ),
        ),
        spec_versions=(
            SpecVersionFact(
                spec_version_id=SPEC_ID,
                spec_hash=SPEC_HASH,
                status="approved",
                source_specification_decision_id=3,
                accepted_at=EVALUATED_AT,
                accepted_by="operator",
                acceptance_notes="Accepted.",
                source_specification_candidate_id=2,
                source_specification_candidate_fingerprint=CANDIDATE_FINGERPRINT,
                source_vision_artifact_id=PRODUCT_VISION_ID,
                source_vision_fingerprint=PRODUCT_VISION_FINGERPRINT,
                source_product_goal_artifact_id=GOAL_ID,
                source_product_goal_fingerprint=GOAL_FINGERPRINT,
            ),
        ),
        phase_artifacts=() if backlog is None else (backlog,),
        review_decisions=review_decisions,
        vision_artifacts=(vision,),
        vision_artifact_decisions=(
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=PRODUCT_VISION_ID,
                vision_artifact_id=PRODUCT_VISION_ID,
                artifact_fingerprint=PRODUCT_VISION_FINGERPRINT,
                decision=vision_decision,
                rationale=f"Vision {vision_decision}.",
                reviewer="operator@example.com",
                idempotency_key=f"vision-{vision_decision}",
                decided_at=EVALUATED_AT,
            ),
        ),
        product_goal_artifacts=(goal,) if active_goal else (),
        product_goal_artifact_decisions=(
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=GOAL_ID,
                product_goal_artifact_id=GOAL_ID,
                artifact_fingerprint=GOAL_FINGERPRINT,
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="goal-accepted",
                decided_at=EVALUATED_AT,
            ),
        )
        if active_goal
        else (),
        node_attempts=attempts,
    )


def _position(snapshot: WorkflowFactSnapshot) -> WorkflowPosition:
    return project_graph().evaluate(snapshot, EVALUATED_AT)


def _decision(snapshot: WorkflowFactSnapshot, node_id: str) -> NodeDecision:
    return next(
        item for item in _position(snapshot).decisions if item.node_id == node_id
    )


def test_backlog_requires_an_active_accepted_goal() -> None:
    """A Specification alone cannot expose Backlog generation."""
    position = _position(_snapshot(active_goal=False))

    assert "backlog.generate" in position.blocked_nodes
    assert _decision(_snapshot(active_goal=False), "backlog.generate").reason_code == (
        "ACCEPTED_PRODUCT_GOAL_REQUIRED"
    )


@pytest.mark.parametrize(
    ("vision_decision", "goal_unlocked"),
    [("accepted", True), ("feedback", False), ("rejected", False)],
)
def test_only_human_accepted_vision_unlocks_product_goal(
    vision_decision: Literal["accepted", "feedback", "rejected"],
    goal_unlocked: bool,
) -> None:
    """Product Goal starts only after the operator accepts the exact Vision."""
    position = _position(_snapshot(active_goal=False, vision_decision=vision_decision))

    assert ("goal.interview" in position.available_nodes) is goal_unlocked


def test_active_product_goal_blocks_accepted_vision_revision() -> None:
    """A current Goal prevents an accepted Vision from reopening revision work."""
    position = _position(_snapshot())

    assert "vision.revision.start" not in position.available_nodes
    assert "vision.revision.start" not in {item.node_id for item in position.decisions}


def test_backlog_generation_references_exact_goal_and_specification() -> None:
    """Generation has both immutable current parents in its graph decision."""
    decision = _decision(_snapshot(), "backlog.generate")

    assert decision.category is NodeCategory.AVAILABLE
    fact_references = {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in decision.fact_references
    }
    assert fact_references == {
        ("specification", str(SPEC_ID), SPEC_HASH),
        ("product_goal", str(GOAL_ID), GOAL_FINGERPRINT),
    }


def test_backlog_attempt_waits_on_the_durable_generation_lease() -> None:
    """A retry cannot replace an active Goal/Specification-bound attempt."""
    base_snapshot = _snapshot()
    attempt = NodeAttemptFact(
        attempt_id=1,
        node_id="backlog.generate",
        instance_key=None,
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=business_fact_fingerprint(base_snapshot),
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="test",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=None,
    )

    decision = _decision(_snapshot(attempts=(attempt,)), "backlog.generate")

    assert decision.category is NodeCategory.WAITING
    assert decision.reason_code == "BACKLOG_GENERATION_ACTIVE"



def test_historical_goal_backlog_is_not_current_delivery_state() -> None:
    """Leave old Backlogs immutable while excluding them from planning selection."""
    position = _position(
        _snapshot(
            backlog=_artifact(
                status="accepted",
                goal_id=GOAL_ID + 1,
                goal_fingerprint="sha256:goal-historical",
            )
        )
    )

    assert "backlog.generate" in position.available_nodes
    assert "planning.roadmap.generate" in position.blocked_nodes


def test_specification_replacement_requires_fresh_backlog() -> None:
    """A Backlog pinned to another Specification is historical only."""
    position = _position(
        _snapshot(
            backlog=_artifact(
                status="accepted",
                spec_version_id=SPEC_ID + 1,
                spec_hash="sha256:historical-specification",
            ),
        )
    )

    assert "backlog.generate" in position.available_nodes
    assert "backlog.reconcile" not in {item.node_id for item in position.decisions}


def test_accepted_current_backlog_unlocks_roadmap_with_goal_lineage() -> None:
    """Planning inherits the exact active Goal through the selected Backlog."""
    decision = _decision(
        _snapshot(backlog=_artifact(status="accepted")),
        "planning.roadmap.generate",
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert {item.fact_type for item in decision.fact_references} == {
        "backlog",
        "product_goal",
        "specification",
    }


def test_agent_inputs_reject_unknown_context() -> None:
    """Provider contracts remain closed to undeclared delivery context."""
    with pytest.raises(ValidationError):
        BacklogAgentOutput.model_validate(
            {
                "backlog_items": [],
                "is_complete": True,
                "unknown_control": "invalid",
            }
        )
    with pytest.raises(ValidationError):
        RoadmapBuilderOutput.model_validate(
            {
                "roadmap_releases": [],
                "roadmap_summary": "Current delivery sequence",
                "is_complete": True,
                "unknown_control": {"invalid": True},
            }
        )


@pytest.mark.parametrize(
    (
        "outcome",
        "expired",
        "expected_reason",
        "expected_category",
        "expected_recommendation",
    ),
    [
        (None, False, "BACKLOG_CORRECTION_ACTIVE", NodeCategory.WAITING, None),
        (
            "failure",
            False,
            "BACKLOG_CORRECTION_FAILED",
            NodeCategory.AVAILABLE,
            RecommendationKind.RECOVERY,
        ),
        (
            "obsolete",
            False,
            "BACKLOG_CORRECTION_RECOVERY_REQUIRED",
            NodeCategory.AVAILABLE,
            RecommendationKind.RECOVERY,
        ),
        (
            None,
            True,
            "BACKLOG_CORRECTION_RECOVERY_REQUIRED",
            NodeCategory.AVAILABLE,
            RecommendationKind.RECOVERY,
        ),
    ],
)
def test_backlog_correction_attempt_overlays_correction_specific_reasons(
    outcome: Literal["success", "failure", "obsolete"] | None,
    expired: bool,
    expected_reason: str,
    expected_category: NodeCategory,
    expected_recommendation: RecommendationKind | None,
) -> None:
    """Correction attempts overlay correction-specific active and recovery reasons."""
    base_snapshot = _snapshot(backlog=_artifact(status="accepted"))
    current_facts = business_fact_fingerprint(base_snapshot)
    lease_expires_at = (
        EVALUATED_AT - timedelta(minutes=1)
        if expired
        else EVALUATED_AT + timedelta(minutes=5)
    )
    attempt = NodeAttemptFact(
        attempt_id=19,
        node_id="backlog.generate",
        instance_key=None,
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=current_facts,
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt-19",
        model_id="test",
        lease_expires_at=lease_expires_at,
        outcome=outcome,
    )
    snapshot = _snapshot(
        backlog=_artifact(status="accepted"),
        attempts=(attempt,),
    )
    decision = _decision(snapshot, "backlog.generate")

    assert decision.category is expected_category
    assert decision.reason_code == expected_reason
    if expected_recommendation is not None:
        assert decision.recommendation_kind is expected_recommendation
        attempt_refs = [
            ref for ref in decision.fact_references if ref.fact_type == "node_attempt"
        ]
        assert len(attempt_refs) == 1
        assert attempt_refs[0].fact_id == "19"
        assert attempt_refs[0].fingerprint == "sha256:attempt-19"


def test_story_optional_reentry_reasons_stay_unchanged_without_overrides() -> None:
    """Nodes without optional-reentry overrides fall back to generic reasons."""
    story_node = next(
        node
        for node in project_graph().root.iter_nodes()
        if node.node_id == "planning.story.generate"
    )
    assert story_node.agentic_execution is not None
    assert story_node.agentic_execution.optional_reentry_active_reason is None
    assert story_node.agentic_execution.optional_reentry_failure_reason is None
    assert story_node.agentic_execution.optional_reentry_recovery_reason is None

    rule_eval = RuleEvaluation(
        category=RuleCategory.AVAILABLE,
        reason_code="STORY_CORRECTION_AVAILABLE",
        instance_key="backlog_item:PBI-000001",
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
    )
    graph = WorkflowGraph(
        graph_version="agileforge.workflow.v2",
        root=ChildGraphSpec(
            child_graph_id="root",
            nodes=(),
            children=(
                ChildGraphSpec(
                    child_graph_id="planning",
                    nodes=(
                        replace(story_node, evaluate_rule=lambda _s, _t: (rule_eval,)),
                    ),
                ),
            ),
        ),
    )
    base_snapshot = _snapshot()
    current_facts = business_fact_fingerprint(base_snapshot)
    active_attempt = NodeAttemptFact(
        attempt_id=21,
        node_id="planning.story.generate",
        instance_key="backlog_item:PBI-000001",
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=current_facts,
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt-21",
        model_id="test",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=None,
    )
    position = graph.evaluate(
        base_snapshot.model_copy(update={"node_attempts": (active_attempt,)}),
        EVALUATED_AT,
    )
    assert position.decisions[0].reason_code == "STORY_GENERATION_ACTIVE"


def test_backlog_correction_remains_available_with_terminal_roadmap() -> None:
    """A terminal Roadmap, feedback, and failed attempt allow correction."""
    base = _snapshot(backlog=_artifact(status="accepted"))
    current_facts = business_fact_fingerprint(base)
    terminal_roadmap = PlanningArtifactFact.model_validate(
        {
            "artifact_type": "roadmap",
            "artifact_id": 901,
            "artifact_fingerprint": "sha256:roadmap-901",
            "source_fingerprint": BACKLOG_FINGERPRINT,
            "backlog_artifact_id": BACKLOG_ID,
            "backlog_artifact_fingerprint": BACKLOG_FINGERPRINT,
            "roadmap_artifact_id": 901,
            "roadmap_artifact_fingerprint": "sha256:roadmap-901",
            "status": "feedback",
        }
    )
    failed_attempt = NodeAttemptFact(
        attempt_id=18,
        node_id="planning.roadmap.generate",
        instance_key=None,
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=current_facts,
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt-18",
        model_id="test",
        lease_expires_at=EVALUATED_AT - timedelta(minutes=5),
        outcome="failure",
    )
    snapshot = base.model_copy(
        update={
            "planning_artifacts": (terminal_roadmap,),
            "node_attempts": (failed_attempt,),
        }
    )
    decision = _decision(snapshot, "backlog.generate")
    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "BACKLOG_CORRECTION_AVAILABLE"
    assert decision.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY


@pytest.mark.parametrize(
    "patch_fn",
    [
        # 1. pending-review Roadmap
        lambda _s: {
            "planning_artifacts": (
                PlanningArtifactFact.model_validate(
                    {
                        "artifact_type": "roadmap",
                        "artifact_id": 902,
                        "artifact_fingerprint": "sha256:rm-902",
                        "source_fingerprint": BACKLOG_FINGERPRINT,
                        "backlog_artifact_id": BACKLOG_ID,
                        "backlog_artifact_fingerprint": BACKLOG_FINGERPRINT,
                        "roadmap_artifact_id": 902,
                        "roadmap_artifact_fingerprint": "sha256:rm-902",
                        "status": "pending_review",
                    }
                ),
            )
        },
        # 2a. Story planning artifact
        lambda _s: {
            "planning_artifacts": (
                PlanningArtifactFact.model_validate(
                    {
                        "artifact_type": "story",
                        "artifact_id": 903,
                        "artifact_fingerprint": "sha256:st-903",
                        "source_fingerprint": "sha256:rm",
                        "backlog_artifact_id": BACKLOG_ID,
                        "backlog_artifact_fingerprint": BACKLOG_FINGERPRINT,
                        "status": "pending_review",
                    }
                ),
            )
        },
        # 2b. Story fact
        lambda _s: {
            "stories": (
                StoryFact(
                    story_id=1,
                    is_superseded=False,
                    source_story_artifact_id=903,
                    source_story_artifact_fingerprint="sha256:st-903",
                    source_story_item_id="US-000001",
                    source_story_item_fingerprint="sha256:item-000001",
                    accepted_spec_version_id=SPEC_ID,
                    accepted_spec_hash=SPEC_HASH,
                    spec_item_ids=("SPEC-001",),
                    content_fingerprint="sha256:st-903",
                    content_accepted=True,
                    story_artifact_id=903,
                    backlog_artifact_id=BACKLOG_ID,
                    backlog_artifact_fingerprint=BACKLOG_FINGERPRINT,
                    roadmap_artifact_id=901,
                    roadmap_artifact_fingerprint="sha256:rm-901",
                    status="to_do",
                    story_points=3,
                    rank="1",
                    structurally_eligible=True,
                    structural_eligibility_status="eligible",
                    sprint_selection_state="unselected",
                    sprint_selection_state_fingerprint="sha256:sel",
                    sprint_candidate=False,
                    readiness_blockers=(),
                ),
            )
        },
        # 3a. Dependency row
        lambda _s: {
            "story_dependencies": (
                StoryDependencyFact(
                    dependency_id=1,
                    dependent_story_id=2,
                    prerequisite_story_id=1,
                    status="proposed",
                    source="story_writer",
                    confidence="explicit",
                ),
            )
        },
        # 3b. Dependency review row
        lambda _s: {
            "story_dependency_reviews": (
                StoryDependencyReviewFact(
                    review_id=1,
                    selected_story_ids=(1, 2),
                    reviewed_edges=(),
                    source_fingerprint="sha256:" + "0" * 64,
                    dependency_fingerprint="sha256:dep",
                ),
            )
        },
        # 4. Sprint-plan artifact (accepted, feedback, or rejected)
        lambda _s: {
            "planning_artifacts": (
                PlanningArtifactFact.model_validate(
                    {
                        "artifact_type": "sprint_plan",
                        "artifact_id": 904,
                        "artifact_fingerprint": "sha256:sp-904",
                        "source_fingerprint": "sha256:cand",
                        "status": "feedback",
                    }
                ),
            )
        },
        # 5a. Sprint fact
        lambda _s: {
            "sprints": (
                SprintFact(sprint_id=1, status="planned", completed_at=None),
            )
        },
        # 5b. Sprint start fact
        lambda _s: {
            "sprint_starts": (
                SprintStartFact(
                    start_id=1,
                    sprint_id=1,
                    spec_version_id=SPEC_ID,
                    spec_hash=SPEC_HASH,
                    sprint_plan_artifact_id=904,
                    sprint_plan_artifact_decision_id=1,
                    story_dependency_review_id=1,
                    plan_fingerprint="sha256:plan",
                    candidate_set_fingerprint="sha256:cand",
                    selected_story_ids=(1,),
                    task_content_fingerprint="sha256:tasks",
                    dependency_source_fingerprint="sha256:dep",
                    dependency_fingerprint="sha256:deps",
                    dependency_rows_snapshot=(),
                    dependency_rows_fingerprint="sha256:rows",
                    decision_fingerprint="sha256:dec",
                    audit_event_id=1,
                    audit_event_fingerprint="sha256:audit",
                    started_by="operator@example.com",
                    started_at=EVALUATED_AT,
                ),
            )
        },
        # 5c. Task fact
        lambda _s: {
            "tasks": (
                TaskFact(
                    task_id=1,
                    sprint_id=1,
                    story_id=1,
                    description="Task",
                    metadata_json="{}",
                    status="To Do",
                    dependencies_satisfied=True,
                ),
            )
        },
        # 6. Same-current-facts Story or Sprint-plan attempt
        lambda s: {
            "node_attempts": (
                NodeAttemptFact(
                    attempt_id=22,
                    node_id="planning.story.generate",
                    instance_key=None,
                    graph_version="agileforge.workflow.v2",
                    input_fingerprint="sha256:input",
                    fact_fingerprint="sha256:facts",
                    business_fact_fingerprint=business_fact_fingerprint(s),
                    decision_fingerprint="sha256:decision",
                    attempt_fingerprint="sha256:attempt-22",
                    model_id="test",
                    lease_expires_at=EVALUATED_AT - timedelta(minutes=5),
                    outcome="failure",
                ),
            )
        },
    ],
)
def test_backlog_correction_stage_closed_by_downstream_planning_facts(
    patch_fn: Callable[[WorkflowFactSnapshot], dict[str, object]],
) -> None:
    """Downstream planning facts close the Backlog correction stage."""
    base = _snapshot(backlog=_artifact(status="accepted"))
    snapshot = base.model_copy(update=patch_fn(base))
    decision = _decision(snapshot, "backlog.generate")
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "BACKLOG_CORRECTION_STAGE_CLOSED"
    assert len(decision.blockers) == 1
    assert decision.blockers[0].code == "BACKLOG_CORRECTION_STAGE_CLOSED"
    expected_closed_msg = (
        "Guided Backlog correction is available only before Story "
        "or Sprint planning begins."
    )
    assert decision.blockers[0].message == expected_closed_msg


def test_backlog_correction_downstream_active_by_running_roadmap_attempt() -> None:
    """An in-flight Roadmap attempt blocks correction with DOWNSTREAM_ACTIVE."""
    base = _snapshot(backlog=_artifact(status="accepted"))
    current_facts = business_fact_fingerprint(base)
    active_attempt = NodeAttemptFact(
        attempt_id=25,
        node_id="planning.roadmap.generate",
        instance_key=None,
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=current_facts,
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt-25",
        model_id="test",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=None,
    )
    snapshot = base.model_copy(update={"node_attempts": (active_attempt,)})
    decision = _decision(snapshot, "backlog.generate")
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "BACKLOG_CORRECTION_DOWNSTREAM_ACTIVE"
    assert len(decision.blockers) == 1
    assert decision.blockers[0].code == "BACKLOG_CORRECTION_DOWNSTREAM_ACTIVE"
    assert (
        decision.blockers[0].message
        == "Wait for the current downstream operation to finish."
    )


def test_backlog_correction_stale_downstream_attempts_do_not_close_boundary() -> None:
    """Stale downstream attempts with different business facts do not close boundary."""
    base = _snapshot(backlog=_artifact(status="accepted"))
    stale_attempt = NodeAttemptFact(
        attempt_id=26,
        node_id="planning.story.generate",
        instance_key=None,
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint="sha256:old-stale-facts",
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt-26",
        model_id="test",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=None,
    )
    snapshot = base.model_copy(update={"node_attempts": (stale_attempt,)})
    decision = _decision(snapshot, "backlog.generate")
    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "BACKLOG_CORRECTION_AVAILABLE"


@pytest.mark.parametrize(
    "node_id",
    ["planning.story.generate", "planning.sprint.plan"],
)
@pytest.mark.parametrize(
    "outcome",
    ["active", "success", "failure", "obsolete", "expired"],
)
def test_backlog_correction_stage_closed_by_downstream_attempt_matrix(
    node_id: str,
    outcome: str,
) -> None:
    """Downstream Story and Sprint-plan attempts close Backlog correction."""
    base = _snapshot(backlog=_artifact(status="accepted"))
    current_facts = business_fact_fingerprint(base)

    if outcome == "active":
        attempt_outcome = None
        lease_expires_at = EVALUATED_AT + timedelta(minutes=5)
    elif outcome == "expired":
        attempt_outcome = None
        lease_expires_at = EVALUATED_AT - timedelta(minutes=5)
    else:
        attempt_outcome = cast('Literal["success", "failure", "obsolete"]', outcome)
        lease_expires_at = EVALUATED_AT - timedelta(minutes=5)

    attempt = NodeAttemptFact(
        attempt_id=30,
        node_id=node_id,
        instance_key=None,
        graph_version="agileforge.workflow.v2",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=current_facts,
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt-30",
        model_id="test",
        lease_expires_at=lease_expires_at,
        outcome=attempt_outcome,
    )
    snapshot = base.model_copy(update={"node_attempts": (attempt,)})
    decision = _decision(snapshot, "backlog.generate")
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "BACKLOG_CORRECTION_STAGE_CLOSED"
    assert len(decision.blockers) == 1
    assert decision.blockers[0].code == "BACKLOG_CORRECTION_STAGE_CLOSED"
    assert (
        decision.blockers[0].message
        == "Guided Backlog correction is available only before Story "
        "or Sprint planning begins."
    )
