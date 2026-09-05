"""Table-driven tests for the pure hierarchical workflow graph kernel."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

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
from workflow.fingerprints import business_fact_fingerprint, canonical_hash
from workflow.graph import (
    AgenticExecutionSpec,
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


def _agentic_graph(  # noqa: PLR0913
    reason_code: str = "READY",
    *,
    node_id: str = "test.execute",
    recommendation_kind: RecommendationKind = RecommendationKind.REQUIRED,
    active_reason: str = "TEST_ACTIVE",
    failure_reason: str = "TEST_FAILED",
    recovery_reason: str = "TEST_RECOVERY_REQUIRED",
    evaluate_rule: NodeRule | None = None,
) -> WorkflowGraph:
    """Build a one-node agentic graph for kernel testing."""
    evaluation = RuleEvaluation(
        category=RuleCategory.AVAILABLE,
        reason_code=reason_code,
        recommendation_kind=recommendation_kind,
    )
    rule = evaluate_rule or _constant_rule(evaluation)
    return _graph(
        NodeSpec(
            node_id=node_id,
            child_graph_id="test",
            request_kind=f"{node_id}.request",
            recommendation_kind=recommendation_kind,
            required_inputs=(InputField(name="payload", value_type="object"),),
            evaluate_rule=rule,
            agentic_execution=AgenticExecutionSpec(
                active_reason=active_reason,
                failure_reason=failure_reason,
                recovery_reason=recovery_reason,
            ),
        )
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
    """Keep optional work visible without reopening required work."""
    graph = _graph(
        _node(
            "test.optional_reentry",
            RuleEvaluation(
                category=RuleCategory.AVAILABLE,
                reason_code="OPTIONAL_EXTENSION",
            ),
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        )
    )

    position = graph.evaluate(_snapshot(), EVALUATED_AT)

    assert position.terminal is True
    assert position.available_nodes == ("test.optional_reentry",)
    assert position.decisions[0].recommendation_kind is (
        RecommendationKind.OPTIONAL_REENTRY
    )


def test_join_waits_for_every_required_branch() -> None:
    """Block an all-of join until every required branch is complete."""
    first = StoryFact(
        story_id=1,
        is_superseded=False,
        source_story_artifact_id=101,
        source_story_artifact_fingerprint="sha256:story-artifact-1",
        source_story_item_id="US-000001",
        source_story_item_fingerprint="sha256:story-item-1",
        accepted_spec_version_id=1,
        accepted_spec_hash="sha256:" + "a" * 64,
        spec_item_ids=("REQ.001",),
        status="completed",
        structurally_eligible=True,
        structural_eligibility_status="eligible",
        sprint_selection_state="unselected",
        sprint_selection_state_fingerprint="sha256:selection-1",
        sprint_candidate=False,
        readiness_blockers=(),
    )
    second = StoryFact(
        story_id=2,
        is_superseded=False,
        source_story_artifact_id=102,
        source_story_artifact_fingerprint="sha256:story-artifact-2",
        source_story_item_id="US-000002",
        source_story_item_fingerprint="sha256:story-item-2",
        accepted_spec_version_id=1,
        accepted_spec_hash="sha256:" + "a" * 64,
        spec_item_ids=("REQ.002",),
        status="planned",
        structurally_eligible=True,
        structural_eligibility_status="eligible",
        sprint_selection_state="unselected",
        sprint_selection_state_fingerprint="sha256:selection-2",
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
    """Expose the approved hierarchy and ordered lifecycle paths."""
    assert tuple(child.child_graph_id for child in ROOT_GRAPH.root.children) == (
        "vision",
        "product_goal",
        "specification",
        "backlog",
        "planning",
        "execution",
    )
    (
        vision,
        product_goal,
        specification,
        backlog,
        planning,
        execution,
    ) = ROOT_GRAPH.root.children
    assert tuple(node.node_id for node in vision.nodes) == (
        "vision.bootstrap",
        "vision.interview",
        "vision.review",
        "vision.revision.start",
    )
    assert tuple(node.node_id for node in product_goal.nodes) == (
        "goal.interview",
        "goal.review",
        "goal.fulfill",
        "goal.abandon",
    )
    assert tuple(node.node_id for node in specification.nodes) == (
        "specification.source.register",
        "specification.structure",
        "specification.review",
    )
    assert tuple(node.node_id for node in backlog.nodes) == (
        "backlog.generate",
        "backlog.review",
    )
    assert tuple(node.node_id for node in planning.nodes) == (
        "planning.roadmap.generate",
        "planning.roadmap.review",
        "planning.story.generate",
        "planning.story.review",
        "planning.story_dependencies",
        "planning.story_readiness",
        "planning.sprint.plan",
        "planning.sprint.review",
        "planning.sprint.start",
    )
    assert tuple(node.node_id for node in execution.nodes) == (
        "execution.task.complete",
        "execution.story.close",
        "execution.sprint.review",
        "execution.sprint.close",
        "execution.post_sprint_triage",
    )


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


@pytest.mark.parametrize("outcome", [None, "failure", "obsolete", "success"])
def test_agentic_overlay_ignores_attempt_from_prior_business_facts(
    outcome: Literal["success", "failure", "obsolete"] | None,
) -> None:
    """Ignore attempts from prior business facts for all attempt outcomes."""
    snapshot = _snapshot()
    stale_attempt = NodeAttemptFact(
        attempt_id=19,
        node_id="test.execute",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=canonical_hash({"prior": True}),
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="fixed-model",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=outcome,
    )
    graph = _agentic_graph(reason_code="READY")

    decision = graph.evaluate(
        snapshot.model_copy(update={"node_attempts": (stale_attempt,)}),
        EVALUATED_AT,
    ).decisions[0]

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "READY"
    assert decision.recommendation_kind is RecommendationKind.REQUIRED


def test_agentic_overlay_preserves_active_attempt_on_current_business_facts() -> None:
    """Keep waiting category and active reason for current-facts active attempts."""
    snapshot = _snapshot()
    attempt = NodeAttemptFact(
        attempt_id=19,
        node_id="test.execute",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=business_fact_fingerprint(snapshot),
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="fixed-model",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=None,
    )
    graph = _agentic_graph(reason_code="READY")

    decision = graph.evaluate(
        snapshot.model_copy(update={"node_attempts": (attempt,)}),
        EVALUATED_AT,
    ).decisions[0]

    assert decision.category is NodeCategory.WAITING
    assert decision.reason_code == "TEST_ACTIVE"


def test_agentic_overlay_preserves_failed_attempt_on_current_business_facts() -> None:
    """Overlay failure reason and reference for current-facts failed attempts."""
    snapshot = _snapshot()
    attempt = NodeAttemptFact(
        attempt_id=19,
        node_id="test.execute",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=business_fact_fingerprint(snapshot),
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="fixed-model",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome="failure",
    )
    graph = _agentic_graph(reason_code="READY")

    decision = graph.evaluate(
        snapshot.model_copy(update={"node_attempts": (attempt,)}),
        EVALUATED_AT,
    ).decisions[0]

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "TEST_FAILED"
    assert decision.recommendation_kind is RecommendationKind.RECOVERY
    assert decision.fact_references == (
        FactReference(
            fact_type="node_attempt",
            fact_id="19",
            fingerprint=attempt.attempt_fingerprint,
        ),
    )


def test_decision_refs_ignore_stale_attempt_for_intrinsic_recovery() -> None:
    """Recovery decisions do not attach attempts from older business facts."""
    intrinsic_ref = FactReference(
        fact_type="intrinsic",
        fact_id="1",
        fingerprint="sha256:intrinsic",
    )
    evaluation = RuleEvaluation(
        category=RuleCategory.AVAILABLE,
        reason_code="INTRINSIC_RECOVERY",
        recommendation_kind=RecommendationKind.RECOVERY,
        fact_references=(intrinsic_ref,),
    )
    graph = _agentic_graph(
        reason_code="INTRINSIC_RECOVERY",
        recommendation_kind=RecommendationKind.RECOVERY,
        evaluate_rule=_constant_rule(evaluation),
    )
    snapshot = _snapshot()
    stale_failed_attempt = NodeAttemptFact(
        attempt_id=20,
        node_id="test.execute",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=canonical_hash({"prior": True}),
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="fixed-model",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome="failure",
    )

    decision = graph.evaluate(
        snapshot.model_copy(update={"node_attempts": (stale_failed_attempt,)}),
        EVALUATED_AT,
    ).decisions[0]

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "INTRINSIC_RECOVERY"
    assert decision.recommendation_kind is RecommendationKind.RECOVERY
    assert decision.fact_references == (intrinsic_ref,)
