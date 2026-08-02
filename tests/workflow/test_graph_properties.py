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
    BacklogReconciliationFact,
    ChallengeArtifactFact,
    DiscoveryRunAbandonmentFact,
    DiscoveryRunFact,
    InitialScopeRegistrationFact,
    NodeAttemptFact,
    PhaseArtifactFact,
    PostSprintTriageFact,
    PrdVersionFact,
    ProjectAbandonmentFact,
    ProjectFact,
    RepositoryBaselineFact,
    RepositoryInventoryFact,
    ReviewDecisionFact,
    SpecDraftFact,
    SpecVersionFact,
    SprintFact,
    StoryFact,
    TaskFact,
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
            origin="brownfield",
            created_at=EVALUATED_AT - timedelta(hours=2),
        ),
    ),
    (
        "project_abandonments",
        (
            ProjectAbandonmentFact(
                project_abandonment_id=1,
                project_id=23,
                reason="Superseded",
                abandoned_by="reviewer",
                abandoned_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "discovery_runs",
        (
            DiscoveryRunFact(
                discovery_run_id=2,
                project_id=23,
                purpose="initial",
                ordinal=1,
                created_at=EVALUATED_AT - timedelta(minutes=30),
                closed_at=None,
            ),
        ),
    ),
    (
        "discovery_run_abandonments",
        (
            DiscoveryRunAbandonmentFact(
                discovery_run_abandonment_id=3,
                project_id=23,
                discovery_run_id=2,
                reason="Restarted",
                abandoned_by="reviewer",
                abandoned_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "challenge_artifacts",
        (
            ChallengeArtifactFact(
                challenge_artifact_id=4,
                discovery_run_id=2,
                content_fingerprint="sha256:challenge",
                supersedes_id=None,
            ),
        ),
    ),
    (
        "prd_versions",
        (
            PrdVersionFact(
                prd_version_id=5,
                discovery_run_id=2,
                content_fingerprint="sha256:prd",
                supersedes_id=None,
            ),
        ),
    ),
    (
        "review_decisions",
        (
            ReviewDecisionFact(
                decision_id=6,
                artifact_type="prd",
                artifact_id=5,
                artifact_fingerprint="sha256:prd",
                decision="accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
    ),
    (
        "spec_drafts",
        (
            SpecDraftFact(
                spec_draft_id=7,
                discovery_run_id=2,
                kind="initial",
                content_fingerprint="sha256:spec-draft",
                base_spec_version_id=None,
                base_spec_hash=None,
                supersedes_id=None,
            ),
        ),
    ),
    (
        "initial_registrations",
        (
            InitialScopeRegistrationFact(
                registration_id=8,
                discovery_run_id=2,
                spec_draft_id=7,
                spec_version_id=9,
                spec_hash="sha256:spec-version",
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
            ),
        ),
    ),
    (
        "repository_baselines",
        (
            RepositoryBaselineFact(
                repository_baseline_id=10,
                repository_path="/evidence/repository",
                git_commit="a" * 40,
                dirty=False,
                content_fingerprint="sha256:baseline",
            ),
        ),
    ),
    (
        "repository_inventories",
        (
            RepositoryInventoryFact(
                repository_inventory_id=11,
                repository_baseline_id=10,
                content_fingerprint="sha256:inventory",
                file_count=2,
                total_bytes=20,
                selected_for_model=("README.md",),
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
        "backlog_reconciliations",
        (
            BacklogReconciliationFact(
                reconciliation_id=12,
                replacement_authority_id=10,
                replacement_authority_fingerprint="sha256:authority",
                affected_artifact_ids=(11,),
                affected_artifacts_fingerprint="sha256:reconciliation",
                reconciled_by="reviewer",
                audit_event_id=16,
                audit_event_action="backlog_authority_reconciled",
                audit_event_fingerprint="sha256:reconciliation-audit",
                reconciled_at=EVALUATED_AT,
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
        "tasks",
        (
            TaskFact(
                task_id=14,
                sprint_id=12,
                story_id=13,
                status="ready",
                dependencies_satisfied=True,
            ),
        ),
    ),
    (
        "post_sprint_triage",
        (
            PostSprintTriageFact(
                sprint_id=12,
                impact="none",
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
            origin="greenfield",
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
            _graph(baseline_node, graph_version="agileforge.workflow.v2"),
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
