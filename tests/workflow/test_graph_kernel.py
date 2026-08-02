"""Table-driven tests for the pure hierarchical workflow graph kernel."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from workflow.clock import FixedClock
from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    FactReference,
    InputField,
    NodeCategory,
    RecommendationKind,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.facts import (
    NodeAttemptFact,
    ProjectFact,
    StoryFact,
    WorkflowFactSnapshot,
)
from workflow.graph import (
    ChildGraphSpec,
    NodeRule,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

if TYPE_CHECKING:
    from collections.abc import Callable

CLOCK: FixedClock = FixedClock(datetime(2026, 8, 2, 12, tzinfo=UTC))
EVALUATED_AT: datetime = CLOCK.now()


def _snapshot(
    *,
    stories: tuple[StoryFact, ...] = (),
    node_attempts: tuple[NodeAttemptFact, ...] = (),
) -> WorkflowFactSnapshot:
    """Build a minimal immutable snapshot for graph evaluation."""
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=7,
            name="Kernel Test",
            origin="brownfield",
            created_at=EVALUATED_AT - timedelta(days=1),
        ),
        stories=stories,
        node_attempts=node_attempts,
    )


def _constant_rule(*evaluations: RuleEvaluation) -> NodeRule:
    """Return a pure rule with fixed evaluations."""

    def evaluate(
        _snapshot: WorkflowFactSnapshot,
        _evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        return evaluations

    return evaluate


def _node(
    node_id: str,
    evaluation: RuleEvaluation,
    *,
    recommendation_kind: RecommendationKind = RecommendationKind.REQUIRED,
    child_graph_id: str = "test",
) -> NodeSpec:
    """Build one node with a constant singleton rule."""
    return NodeSpec(
        node_id=node_id,
        child_graph_id=child_graph_id,
        request_kind=f"{node_id}.request",
        recommendation_kind=recommendation_kind,
        required_inputs=(InputField(name="payload", value_type="object"),),
        evaluate_rule=_constant_rule(evaluation),
    )


def _graph(*nodes: NodeSpec, graph_version: str = GRAPH_VERSION) -> WorkflowGraph:
    """Build a one-child graph while preserving node order."""
    return WorkflowGraph(
        graph_version=graph_version,
        root=ChildGraphSpec(
            child_graph_id="root",
            nodes=(),
            children=(ChildGraphSpec(child_graph_id="test", nodes=nodes),),
        ),
    )


@pytest.mark.parametrize(
    ("rule_category", "public_category", "position_field"),
    [
        (RuleCategory.AVAILABLE, NodeCategory.AVAILABLE, "available_nodes"),
        (RuleCategory.WAITING, NodeCategory.WAITING, "waiting_nodes"),
        (RuleCategory.BLOCKED, NodeCategory.BLOCKED, "blocked_nodes"),
        (RuleCategory.INVALID, NodeCategory.INVALID, "invalid_nodes"),
    ],
)
def test_rule_categories_project_to_public_decisions(
    rule_category: RuleCategory,
    public_category: NodeCategory,
    position_field: str,
) -> None:
    """Map every externally visible rule category to the public contract."""
    reference = FactReference(
        fact_type="story",
        fact_id="11",
        fingerprint="sha256:story-11",
    )
    blocker = Blocker(
        code="PREREQUISITE",
        message="A required prerequisite is missing.",
        fact_references=(reference,),
    )
    graph = _graph(
        _node(
            "test.node",
            RuleEvaluation(
                category=rule_category,
                reason_code="TABLE_CASE",
                fact_references=(reference,),
                blockers=(blocker,),
            ),
        )
    )

    position = graph.evaluate(_snapshot(), EVALUATED_AT)

    assert len(position.decisions) == 1
    decision = position.decisions[0]
    assert decision.category is public_category
    assert decision.fact_references == (reference,)
    assert decision.blockers == (blocker,)
    assert getattr(position, position_field) == ("test.node",)


def test_satisfied_category_does_not_leak_to_public_decisions() -> None:
    """Elide internal completion markers from public node categories."""
    graph = _graph(
        _node(
            "test.complete",
            RuleEvaluation(
                category=RuleCategory.SATISFIED,
                reason_code="ALREADY_COMPLETE",
            ),
        )
    )

    position = graph.evaluate(_snapshot(), EVALUATED_AT)

    assert position.decisions == ()
    assert position.terminal is True
    assert {item.value for item in NodeCategory} == {
        "available",
        "waiting",
        "blocked",
        "invalid",
    }


def test_optional_reentry_does_not_make_terminal_project_unfinished() -> None:
    """Keep optional scope extension visible without reopening required work."""
    graph = _graph(
        _node(
            "scope_extension.start",
            RuleEvaluation(
                category=RuleCategory.AVAILABLE,
                reason_code="OPTIONAL_EXTENSION",
            ),
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        )
    )

    position = graph.evaluate(_snapshot(), EVALUATED_AT)

    assert position.terminal is True
    assert position.available_nodes == ("scope_extension.start",)
    assert position.decisions[0].recommendation_kind is (
        RecommendationKind.OPTIONAL_REENTRY
    )


def test_join_waits_for_every_required_branch() -> None:
    """Block an all-of join until every required branch is complete."""
    first = StoryFact(
        story_id=1,
        status="completed",
        sprint_candidate=False,
        readiness_blockers=(),
    )
    second = StoryFact(
        story_id=2,
        status="planned",
        sprint_candidate=False,
        readiness_blockers=(),
    )

    def all_branches_complete(
        snapshot: WorkflowFactSnapshot,
        _evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        complete = all(story.status == "completed" for story in snapshot.stories)
        return (
            RuleEvaluation(
                category=(RuleCategory.AVAILABLE if complete else RuleCategory.BLOCKED),
                reason_code="ALL_BRANCHES_COMPLETE" if complete else "JOIN_INCOMPLETE",
            ),
        )

    graph = _graph(
        replace(
            _node(
                "test.join",
                RuleEvaluation(
                    category=RuleCategory.BLOCKED,
                    reason_code="UNUSED",
                ),
            ),
            evaluate_rule=all_branches_complete,
        )
    )

    position = graph.evaluate(_snapshot(stories=(first, second)), EVALUATED_AT)

    assert "test.join" in position.blocked_nodes
    assert position.terminal is False


def test_parallel_branches_can_be_available_together() -> None:
    """Expose independent required branches in one position."""
    graph = _graph(
        _node(
            "test.branch_a",
            RuleEvaluation(category=RuleCategory.AVAILABLE, reason_code="READY"),
        ),
        _node(
            "test.branch_b",
            RuleEvaluation(category=RuleCategory.AVAILABLE, reason_code="READY"),
        ),
    )

    position = graph.evaluate(_snapshot(), EVALUATED_AT)

    assert position.available_nodes == ("test.branch_a", "test.branch_b")
    assert position.terminal is False


def test_graph_version_mismatch_rule_is_invalid() -> None:
    """Preserve an invalid decision emitted for a stale attempt graph version."""
    attempt = NodeAttemptFact(
        attempt_id=19,
        node_id="test.execute",
        instance_key=None,
        graph_version="agileforge.workflow.v0",
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint="sha256:business",
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="fixed-model",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=None,
    )

    def reject_mismatched_attempt(
        snapshot: WorkflowFactSnapshot,
        _evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        mismatch = snapshot.node_attempts[0].graph_version != GRAPH_VERSION
        return (
            RuleEvaluation(
                category=RuleCategory.INVALID if mismatch else RuleCategory.WAITING,
                reason_code="GRAPH_VERSION_MISMATCH" if mismatch else "LEASE_ACTIVE",
            ),
        )

    graph = _graph(
        replace(
            _node(
                "test.execute",
                RuleEvaluation(
                    category=RuleCategory.WAITING,
                    reason_code="UNUSED",
                ),
            ),
            evaluate_rule=reject_mismatched_attempt,
        )
    )

    position = graph.evaluate(
        _snapshot(node_attempts=(attempt,)),
        EVALUATED_AT,
    )

    assert position.invalid_nodes == ("test.execute",)
    assert position.decisions[0].reason_code == "GRAPH_VERSION_MISMATCH"


def test_lease_decision_changes_exactly_at_expiry() -> None:
    """Use explicit evaluation time and a stable lease boundary."""
    lease_expires_at = EVALUATED_AT + timedelta(minutes=10)

    def lease_rule(
        _snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        if evaluated_at < lease_expires_at:
            return (
                RuleEvaluation(
                    category=RuleCategory.WAITING,
                    reason_code="LEASE_ACTIVE",
                    valid_until=lease_expires_at,
                ),
            )
        return (
            RuleEvaluation(
                category=RuleCategory.AVAILABLE,
                reason_code="LEASE_EXPIRED",
            ),
        )

    graph = _graph(
        replace(
            _node(
                "test.retry",
                RuleEvaluation(
                    category=RuleCategory.WAITING,
                    reason_code="UNUSED",
                ),
                recommendation_kind=RecommendationKind.RECOVERY,
            ),
            evaluate_rule=lease_rule,
        )
    )

    before = graph.evaluate(_snapshot(), lease_expires_at - timedelta(microseconds=1))
    at_boundary = graph.evaluate(_snapshot(), lease_expires_at)

    assert before.waiting_nodes == ("test.retry",)
    assert before.decisions[0].valid_until == lease_expires_at
    assert at_boundary.available_nodes == ("test.retry",)
    assert before.decisions[0].decision_fingerprint != (
        at_boundary.decisions[0].decision_fingerprint
    )


def test_root_definition_has_named_children_in_lifecycle_order() -> None:
    """Expose the approved hierarchy with only Task 6 abandonment executable."""
    assert tuple(child.child_graph_id for child in ROOT_GRAPH.root.children) == (
        "onboarding",
        "authority",
        "vision",
        "backlog",
        "planning",
        "execution",
        "scope_extension",
    )
    onboarding, *later_children = ROOT_GRAPH.root.children
    assert tuple(node.node_id for node in onboarding.nodes) == (
        "onboarding.abandon_shell",
    )
    assert all(not child.nodes for child in later_children)


@pytest.mark.parametrize(
    "graph_factory",
    [
        lambda: WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(
                child_graph_id="root",
                nodes=(
                    _node(
                        "duplicate.node",
                        RuleEvaluation(
                            category=RuleCategory.AVAILABLE,
                            reason_code="FIRST",
                        ),
                        child_graph_id="root",
                    ),
                ),
                children=(
                    ChildGraphSpec(
                        child_graph_id="child",
                        nodes=(
                            _node(
                                "duplicate.node",
                                RuleEvaluation(
                                    category=RuleCategory.WAITING,
                                    reason_code="SECOND",
                                ),
                                child_graph_id="child",
                            ),
                        ),
                    ),
                ),
            ),
        ),
        lambda: WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(
                child_graph_id="root",
                nodes=(),
                children=(
                    ChildGraphSpec(child_graph_id="duplicate.child", nodes=()),
                    ChildGraphSpec(child_graph_id="duplicate.child", nodes=()),
                ),
            ),
        ),
    ],
    ids=("node_id", "child_graph_id"),
)
def test_graph_construction_rejects_duplicate_identifiers(
    graph_factory: Callable[[], WorkflowGraph],
) -> None:
    """Reject ambiguous node and child graph identities at construction."""
    with pytest.raises(ValueError, match="Duplicate"):
        graph_factory()
