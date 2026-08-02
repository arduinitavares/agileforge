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
from workflow.facts import ProjectFact, WorkflowFactSnapshot
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

CLOCK: FixedClock = FixedClock(datetime(2026, 8, 2, 12, tzinfo=UTC))
EVALUATED_AT: datetime = CLOCK.now()


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
    assert first.decisions[0].decision_fingerprint == (
        second.decisions[0].decision_fingerprint
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
