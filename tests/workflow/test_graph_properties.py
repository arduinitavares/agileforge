"""Deterministic property cases for the pure workflow graph kernel."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from workflow.clock import FixedClock
from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    FactReference,
    InputField,
    RecommendationKind,
)
from workflow.facts import (
    AuthorityFact,
    AuthorityFeedbackFact,
    BacklogRequirementFact,
    NodeAttemptFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    PostSprintTriageFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProductGoalInterviewTurnFact,
    ProductGoalOutcomeFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SpecificationSourceFact,
    SpecVersionFact,
    SprintClosureFact,
    SprintFact,
    SprintReviewFact,
    SprintStartFact,
    StoryCompletionFact,
    StoryDependencyFact,
    StoryDependencyReviewFact,
    StoryFact,
    TaskCompletionFact,
    TaskFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    VisionEvidenceSnapshotFact,
    VisionInterviewTurnFact,
    VisionRevisionIntentFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import fact_fingerprint
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

CLOCK: FixedClock = FixedClock(datetime(2026, 8, 2, 12, tzinfo=UTC))
EVALUATED_AT: datetime = CLOCK.now()

AUTHORITATIVE_SNAPSHOT_VARIANTS: tuple[tuple[str, object], ...] = (
    (
        "project",
        ProjectFact(
            project_id=24,
            name="Changed Project",
            created_at=EVALUATED_AT - timedelta(hours=2),
        ),
    ),
    (
        "review_decisions",
        (
            ReviewDecisionFact(
                decision_id=6,
                artifact_type="vision",
                artifact_id=5,
                artifact_fingerprint="sha256:prd",
                decision="accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "vision_revision_intents",
        (
            VisionRevisionIntentFact(
                vision_revision_intent_id=82,
                source_vision_artifact_id=83,
                source_vision_fingerprint="sha256:vision",
                reason="Clarify the current direction.",
                initiated_by="operator",
                initiated_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "vision_evidence_snapshots",
        (
            VisionEvidenceSnapshotFact(
                vision_evidence_snapshot_id=83,
                repository_binding_id=84,
                supersedes_vision_evidence_snapshot_id=None,
                workflow_node_attempt_id=85,
                evidence={"project": {"name": "Changed Project"}},
                evidence_fingerprint="sha256:evidence",
                warnings=(),
                created_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "vision_interview_turns",
        (
            VisionInterviewTurnFact(
                vision_interview_turn_id=84,
                operation="revision",
                turn_number=1,
                revision_intent_id=82,
                vision_evidence_snapshot_id=83,
                prior_turn_id=None,
                user_text="Clarify the Vision.",
                components={"constraint": "durable"},
                vision_statement="A durable workflow.",
                is_complete=True,
                clarifying_questions=(),
                output_fingerprint="sha256:vision-turn",
                workflow_node_attempt_id=85,
                attempt_fingerprint="sha256:attempt",
                recorded_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "vision_artifacts",
        (
            VisionArtifactFact(
                vision_artifact_id=83,
                version_number=1,
                components={"constraint": "durable"},
                statement="A durable workflow.",
                content_fingerprint="sha256:vision",
                vision_evidence_snapshot_id=83,
                supersedes_vision_artifact_id=None,
                source_interview_turn_id=84,
                created_by="operator",
                created_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "vision_artifact_decisions",
        (
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=85,
                vision_artifact_id=83,
                artifact_fingerprint="sha256:vision",
                decision="accepted",
                rationale="",
                reviewer="reviewer",
                idempotency_key="vision-review-85",
                decided_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "product_goal_interview_turns",
        (
            ProductGoalInterviewTurnFact(
                product_goal_interview_turn_id=86,
                vision_artifact_id=83,
                vision_fingerprint="sha256:vision",
                goal_number=1,
                revision_number=1,
                prior_turn_id=None,
                user_text="Define the Product Goal.",
                components={"priority": "high"},
                goal_statement="Preserve durable evidence.",
                is_complete=True,
                clarifying_questions=(),
                output_fingerprint="sha256:goal-turn",
                workflow_node_attempt_id=87,
                attempt_fingerprint="sha256:goal-attempt",
                recorded_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "product_goal_artifacts",
        (
            ProductGoalArtifactFact(
                product_goal_artifact_id=88,
                vision_artifact_id=83,
                vision_fingerprint="sha256:vision",
                goal_number=1,
                revision_number=1,
                statement="Preserve durable evidence.",
                content_fingerprint="sha256:goal",
                supersedes_product_goal_artifact_id=None,
                source_interview_turn_id=86,
                created_by="operator",
                created_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "product_goal_artifact_decisions",
        (
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=89,
                product_goal_artifact_id=88,
                artifact_fingerprint="sha256:goal",
                decision="accepted",
                rationale="Goal is ready for discovery.",
                reviewer="reviewer",
                idempotency_key="goal-review-89",
                decided_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "product_goal_outcomes",
        (
            ProductGoalOutcomeFact(
                product_goal_outcome_id=89,
                product_goal_artifact_id=88,
                artifact_fingerprint="sha256:goal",
                outcome="fulfilled",
                rationale="Evidence is durable.",
                decided_by="operator",
                decided_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "specification_sources",
        (
            SpecificationSourceFact(
                specification_source_id=90,
                source_fingerprint="sha256:source",
                bundle={"schema_version": "agileforge.specification-source.v1"},
                repository_binding_id=7,
                repository_head_sha="a" * 40,
                repository_dirty=False,
                repository_status_fingerprint="sha256:status",
                vision_artifact_id=83,
                vision_fingerprint="sha256:vision",
                product_goal_artifact_id=88,
                product_goal_fingerprint="sha256:goal",
                supersedes_specification_source_id=None,
                supersedes_source_fingerprint=None,
                registered_by="operator",
                registered_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "specification_candidates",
        (
            SpecificationCandidateFact(
                specification_candidate_id=91,
                candidate_kind="initial",
                specification_source_id=90,
                specification_source_fingerprint="sha256:source",
                vision_artifact_id=83,
                vision_fingerprint="sha256:vision",
                product_goal_artifact_id=88,
                product_goal_fingerprint="sha256:goal",
                base_spec_version_id=None,
                base_spec_hash=None,
                canonical_envelope={"schema_version": "candidate-envelope"},
                payload_fingerprint="sha256:payload",
                source_manifest_fingerprint="sha256:sources",
                producer_input_fingerprint="sha256:input",
                rendered_view_fingerprint="sha256:view",
                candidate_fingerprint="sha256:candidate",
                workflow_node_attempt_id=93,
                attempt_fingerprint="sha256:attempt",
                supersedes_specification_candidate_id=None,
                supersedes_candidate_fingerprint=None,
                recorded_by="operator",
                recorded_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "spec_versions",
        (
            SpecVersionFact(
                spec_version_id=9,
                spec_hash="sha256:spec-version",
                status="approved",
                approved_at=EVALUATED_AT,
                source_specification_candidate_id=91,
                source_specification_candidate_fingerprint="sha256:candidate",
                source_vision_artifact_id=83,
                source_vision_fingerprint="sha256:vision",
                source_product_goal_artifact_id=88,
                source_product_goal_fingerprint="sha256:goal",
                supersedes_spec_version_id=None,
            ),
        ),
    ),
    (
        "specification_decisions",
        (
            SpecificationDecisionFact(
                specification_decision_id=92,
                specification_candidate_id=91,
                candidate_fingerprint="sha256:candidate",
                decision="accepted",
                rationale="",
                reviewer="reviewer",
                idempotency_key="specification-review-92",
                decided_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "authorities",
        (
            AuthorityFact(
                authority_id=10,
                spec_version_id=9,
                authority_fingerprint="sha256:authority",
                status="accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "authority_feedback",
        (
            AuthorityFeedbackFact(
                feedback_id=11,
                source_authority_id=10,
                source_authority_fingerprint="sha256:authority",
                feedback_fingerprint="sha256:feedback",
                recorded_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "phase_artifacts",
        (
            PhaseArtifactFact(
                artifact_type="vision",
                artifact_id="vision:11",
                artifact_fingerprint="sha256:vision",
                status="accepted",
            ),
        ),
    ),
    (
        "backlog_requirements",
        (
            BacklogRequirementFact(
                requirement_id="requirement-a",
                backlog_artifact_id=12,
                backlog_artifact_fingerprint="sha256:backlog",
                requirement="Persist planning facts",
                rank=1,
            ),
        ),
    ),
    (
        "planning_artifacts",
        (
            PlanningArtifactFact(
                artifact_type="roadmap",
                artifact_id=13,
                artifact_fingerprint="sha256:roadmap",
                source_fingerprint="sha256:backlog",
                status="accepted",
            ),
        ),
    ),
    (
        "sprints",
        (
            SprintFact(
                sprint_id=12,
                status="active",
                completed_at=None,
            ),
        ),
    ),
    (
        "sprint_starts",
        (
            SprintStartFact(
                start_id=13,
                sprint_id=12,
                sprint_plan_artifact_id=14,
                sprint_plan_artifact_decision_id=15,
                story_dependency_review_id=16,
                plan_fingerprint="sha256:sprint-plan",
                candidate_set_fingerprint="sha256:candidates",
                selected_story_ids=(13,),
                task_content_fingerprint="sha256:tasks",
                dependency_source_fingerprint="sha256:story-source",
                dependency_fingerprint="sha256:dependencies",
                dependency_rows_fingerprint="sha256:dependency-rows",
                decision_fingerprint="sha256:start-decision",
                audit_event_id=17,
                audit_event_fingerprint="sha256:start-audit",
                started_by="reviewer",
                started_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "stories",
        (
            StoryFact(
                story_id=13,
                status="ready",
                sprint_candidate=True,
                readiness_blockers=(),
            ),
        ),
    ),
    (
        "story_dependencies",
        (
            StoryDependencyFact(
                dependency_id=14,
                dependent_story_id=13,
                prerequisite_story_id=12,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
        ),
    ),
    (
        "story_dependency_reviews",
        (
            StoryDependencyReviewFact(
                review_id=15,
                selected_story_ids=(12, 13),
                reviewed_edges=(),
                source_fingerprint="sha256:story-source",
                dependency_fingerprint="sha256:dependencies",
            ),
        ),
    ),
    (
        "tasks",
        (
            TaskFact(
                task_id=14,
                sprint_id=12,
                story_id=13,
                description="Implement Story 13",
                metadata_json=(
                    '{"artifact_targets":[],"checklist_items":[],'
                    '"relevant_invariant_ids":[],"task_kind":"other",'
                    '"version":"task_metadata.v1","workstream_tags":[]}'
                ),
                status="ready",
                dependencies_satisfied=True,
            ),
        ),
    ),
    (
        "task_completions",
        (
            TaskCompletionFact(
                completion_id=17,
                task_id=14,
                sprint_id=12,
                outcome_summary="Implemented and verified.",
                artifact_refs=("workflow/domain.py",),
                acceptance_result="fully_met",
                checklist_result={"Tests pass": "passed"},
                evidence_fingerprint="sha256:task-completion",
            ),
        ),
    ),
    (
        "story_completions",
        (
            StoryCompletionFact(
                completion_id=18,
                story_id=13,
                sprint_id=12,
                completion_fingerprint="sha256:story-completion",
                resolution="Completed",
                delivered="Delivered.",
                evidence="Verified.",
                known_gaps="None.",
            ),
        ),
    ),
    (
        "sprint_reviews",
        (
            SprintReviewFact(
                review_id=19,
                sprint_id=12,
                review_fingerprint="sha256:sprint-review",
            ),
        ),
    ),
    (
        "sprint_closures",
        (
            SprintClosureFact(
                closure_id=20,
                sprint_id=12,
                review_fingerprint="sha256:sprint-review",
                close_fingerprint="sha256:sprint-close",
            ),
        ),
    ),
    (
        "post_sprint_triage",
        (
            PostSprintTriageFact(
                triage_id=16,
                sprint_id=12,
                impact="none",
                canonical_payload={"summary": "No impact."},
                payload_fingerprint="sha256:triage",
            ),
        ),
    ),
    (
        "node_attempts",
        (
            NodeAttemptFact(
                attempt_id=15,
                node_id="properties.stable",
                instance_key=None,
                graph_version=GRAPH_VERSION,
                input_fingerprint="sha256:input",
                fact_fingerprint="sha256:attempt-facts",
                business_fact_fingerprint="sha256:business-facts",
                decision_fingerprint="sha256:prior-decision",
                attempt_fingerprint="sha256:attempt",
                model_id="fixed-model",
                lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
                outcome=None,
            ),
        ),
    ),
)


def _snapshot(*, name: str = "Properties") -> WorkflowFactSnapshot:
    """Build a snapshot with one controllable fingerprint field."""
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=23,
            name=name,
            created_at=EVALUATED_AT - timedelta(hours=1),
        )
    )


def _node(
    node_id: str,
    evaluations: tuple[RuleEvaluation, ...],
    *,
    request_kind: str = "properties.request",
    recommendation_kind: RecommendationKind = RecommendationKind.REQUIRED,
    required_inputs: tuple[InputField, ...] = (),
) -> NodeSpec:
    """Build a node whose rule returns a fixed tuple."""

    def rule(
        _snapshot: WorkflowFactSnapshot,
        _evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        return evaluations

    return NodeSpec(
        node_id=node_id,
        child_graph_id="properties",
        request_kind=request_kind,
        recommendation_kind=recommendation_kind,
        required_inputs=required_inputs,
        evaluate_rule=rule,
    )


def _graph(
    *nodes: NodeSpec,
    graph_version: str = GRAPH_VERSION,
) -> WorkflowGraph:
    """Build a deterministic nested graph for property cases."""
    return WorkflowGraph(
        graph_version=graph_version,
        root=ChildGraphSpec(
            child_graph_id="root",
            nodes=(),
            children=(ChildGraphSpec(child_graph_id="properties", nodes=nodes),),
        ),
    )


def test_repeated_instances_are_sorted_by_stable_instance_key() -> None:
    """Ignore rule emission order within one repeated node."""
    graph = _graph(
        _node(
            "properties.story",
            (
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="READY",
                    instance_key="story:20",
                ),
                RuleEvaluation(
                    category=RuleCategory.WAITING,
                    reason_code="DEPENDENCY",
                    instance_key="story:3",
                ),
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="READY",
                    instance_key="story:10",
                ),
            ),
        )
    )

    position = graph.evaluate(_snapshot(), EVALUATED_AT)

    assert tuple(item.instance_key for item in position.decisions) == (
        "story:10",
        "story:20",
        "story:3",
    )
    assert position.available_nodes == (
        "properties.story",
        "properties.story",
    )
    assert position.waiting_nodes == ("properties.story",)


def test_instance_order_is_stable_for_none_empty_and_string_keys() -> None:
    """Use a total order when accepted instance-key values would otherwise collide."""
    evaluations = (
        RuleEvaluation(
            category=RuleCategory.AVAILABLE,
            reason_code="NO_KEY",
            instance_key=None,
        ),
        RuleEvaluation(
            category=RuleCategory.AVAILABLE,
            reason_code="EMPTY_KEY",
            instance_key="",
        ),
        RuleEvaluation(
            category=RuleCategory.AVAILABLE,
            reason_code="STRING_KEY",
            instance_key="story:1",
        ),
    )
    forward = _graph(_node("properties.story", evaluations)).evaluate(
        _snapshot(),
        EVALUATED_AT,
    )
    reverse = _graph(_node("properties.story", tuple(reversed(evaluations)))).evaluate(
        _snapshot(),
        EVALUATED_AT,
    )

    expected = (
        (None, "NO_KEY"),
        ("", "EMPTY_KEY"),
        ("story:1", "STRING_KEY"),
    )
    assert (
        tuple(
            (decision.instance_key, decision.reason_code)
            for decision in forward.decisions
        )
        == expected
    )
    assert (
        tuple(
            (decision.instance_key, decision.reason_code)
            for decision in reverse.decisions
        )
        == expected
    )


@pytest.mark.parametrize("duplicate_key", [None, "story:7"])
def test_duplicate_instance_keys_are_rejected(duplicate_key: str | None) -> None:
    """Reject two decisions that claim the same node instance identity."""
    graph = _graph(
        _node(
            "properties.story",
            (
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="FIRST",
                    instance_key=duplicate_key,
                ),
                RuleEvaluation(
                    category=RuleCategory.WAITING,
                    reason_code="SECOND",
                    instance_key=duplicate_key,
                ),
            ),
        )
    )

    with pytest.raises(ValueError, match="Duplicate instance key"):
        graph.evaluate(_snapshot(), EVALUATED_AT)


def test_node_order_precedes_instance_order() -> None:
    """Preserve hierarchy and node order before sorting repeated instances."""
    graph = _graph(
        _node(
            "properties.first",
            (
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="READY",
                    instance_key="z",
                ),
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="READY",
                    instance_key="a",
                ),
            ),
        ),
        _node(
            "properties.second",
            (
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="READY",
                    instance_key="0",
                ),
            ),
        ),
    )

    position = graph.evaluate(_snapshot(), EVALUATED_AT)

    assert tuple((item.node_id, item.instance_key) for item in position.decisions) == (
        ("properties.first", "a"),
        ("properties.first", "z"),
        ("properties.second", "0"),
    )


def test_time_insensitive_decision_fingerprint_ignores_evaluation_time() -> None:
    """Keep ordinary decision identities stable across repeated reads."""
    graph = _graph(
        _node(
            "properties.stable",
            (
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="READY",
                ),
            ),
        )
    )
    snapshot = _snapshot()

    first = graph.evaluate(snapshot, EVALUATED_AT)
    second = graph.evaluate(snapshot, EVALUATED_AT + timedelta(hours=1))

    assert first.evaluated_at != second.evaluated_at
    assert first.fact_fingerprint == second.fact_fingerprint
    assert first.decisions[0].decision_fingerprint == (
        second.decisions[0].decision_fingerprint
    )


def test_snapshot_variants_cover_every_authoritative_field() -> None:
    """Keep the sensitivity matrix aligned with the complete snapshot contract."""
    assert {name for name, _value in AUTHORITATIVE_SNAPSHOT_VARIANTS} == set(
        WorkflowFactSnapshot.model_fields
    )


@pytest.mark.parametrize(
    ("field_name", "authoritative_value"),
    AUTHORITATIVE_SNAPSHOT_VARIANTS,
    ids=tuple(name for name, _value in AUTHORITATIVE_SNAPSHOT_VARIANTS),
)
def test_authoritative_snapshot_fields_change_fact_and_decision_fingerprints(
    field_name: str,
    authoritative_value: object,
) -> None:
    """Fingerprint every authoritative top-level snapshot semantic input."""
    baseline = _snapshot()
    variant = baseline.model_copy(update={field_name: authoritative_value})
    graph = _graph(
        _node(
            "properties.fingerprint",
            (
                RuleEvaluation(
                    category=RuleCategory.AVAILABLE,
                    reason_code="READY",
                ),
            ),
        )
    )

    baseline_position = graph.evaluate(baseline, EVALUATED_AT)
    variant_position = graph.evaluate(variant, EVALUATED_AT)

    assert fact_fingerprint(variant) != fact_fingerprint(baseline), field_name
    assert variant_position.fact_fingerprint != baseline_position.fact_fingerprint
    assert variant_position.decisions[0].decision_fingerprint != (
        baseline_position.decisions[0].decision_fingerprint
    )


def test_decision_fingerprint_covers_complete_decision_payload() -> None:
    """Change the hash when any guarded decision component changes."""
    reference = FactReference(
        fact_type="story",
        fact_id="9",
        fingerprint="sha256:story-9",
    )
    blocker = Blocker(
        code="DEPENDENCY",
        message="Dependency is incomplete.",
        fact_references=(reference,),
    )
    baseline_evaluation = RuleEvaluation(
        category=RuleCategory.WAITING,
        reason_code="BASELINE",
        instance_key="story:9",
        fact_references=(reference,),
        blockers=(blocker,),
        valid_until=EVALUATED_AT + timedelta(minutes=5),
    )
    baseline_node = _node(
        "properties.fingerprint",
        (baseline_evaluation,),
        request_kind="properties.baseline",
        recommendation_kind=RecommendationKind.RECOVERY,
        required_inputs=(InputField(name="force", value_type="boolean"),),
    )
    baseline = (
        _graph(baseline_node)
        .evaluate(
            _snapshot(),
            EVALUATED_AT,
        )
        .decisions[0]
        .decision_fingerprint
    )

    def with_evaluation(evaluation: RuleEvaluation) -> NodeSpec:
        return _node(
            baseline_node.node_id,
            (evaluation,),
            request_kind=baseline_node.request_kind,
            recommendation_kind=baseline_node.recommendation_kind,
            required_inputs=baseline_node.required_inputs,
        )

    variants = (
        (
            _graph(
                baseline_node,
                graph_version="agileforge.workflow.fingerprint-variant",
            ),
            _snapshot(),
        ),
        (_graph(baseline_node), _snapshot(name="Changed Facts")),
        (_graph(replace(baseline_node, node_id="properties.other")), _snapshot()),
        (
            _graph(
                with_evaluation(replace(baseline_evaluation, instance_key="story:10"))
            ),
            _snapshot(),
        ),
        (
            _graph(replace(baseline_node, request_kind="properties.other")),
            _snapshot(),
        ),
        (
            _graph(
                with_evaluation(
                    replace(baseline_evaluation, category=RuleCategory.BLOCKED)
                )
            ),
            _snapshot(),
        ),
        (
            _graph(
                replace(
                    baseline_node,
                    recommendation_kind=RecommendationKind.REQUIRED,
                )
            ),
            _snapshot(),
        ),
        (
            _graph(with_evaluation(replace(baseline_evaluation, reason_code="OTHER"))),
            _snapshot(),
        ),
        (
            _graph(
                replace(
                    baseline_node,
                    required_inputs=(InputField(name="reason", value_type="string"),),
                )
            ),
            _snapshot(),
        ),
        (
            _graph(with_evaluation(replace(baseline_evaluation, fact_references=()))),
            _snapshot(),
        ),
        (
            _graph(with_evaluation(replace(baseline_evaluation, blockers=()))),
            _snapshot(),
        ),
        (
            _graph(
                with_evaluation(
                    replace(
                        baseline_evaluation,
                        valid_until=EVALUATED_AT + timedelta(minutes=6),
                    )
                )
            ),
            _snapshot(),
        ),
    )

    variant_hashes = tuple(
        graph.evaluate(snapshot, EVALUATED_AT).decisions[0].decision_fingerprint
        for graph, snapshot in variants
    )

    assert all(item != baseline for item in variant_hashes)
