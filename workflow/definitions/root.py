"""Root product-lifecycle workflow graph hierarchy."""

from dataclasses import replace
from datetime import datetime

from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    InputField,
    RecommendationKind,
)
from workflow.definitions.authority import AUTHORITY_NODES
from workflow.definitions.backlog import BACKLOG_NODES
from workflow.definitions.execution import EXECUTION_NODES
from workflow.definitions.onboarding import (
    BROWNFIELD_ONBOARDING_NODES,
    GREENFIELD_ONBOARDING_NODES,
    has_historical_accepted_authority,
)
from workflow.definitions.planning import PLANNING_NODES
from workflow.definitions.scope_extension import (
    SCOPE_EXTENSION_NODES,
    scope_execution_is_complete,
    scope_reconciled_snapshot,
    scope_reconciliation_retires_node,
)
from workflow.definitions.vision import VISION_NODES
from workflow.facts import WorkflowFactSnapshot
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)


def _abandon_shell_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Offer typed shell abandonment only before accepted authority."""
    accepted_authority_exists = has_historical_accepted_authority(snapshot)
    if snapshot.project_abandonments and accepted_authority_exists:
        return (
            RuleEvaluation(
                category=RuleCategory.INVALID,
                reason_code="WORKFLOW_FACT_CONFLICT",
            ),
        )
    if snapshot.project_abandonments:
        return (
            RuleEvaluation(
                category=RuleCategory.SATISFIED,
                reason_code="PROJECT_ALREADY_ABANDONED",
            ),
        )
    if accepted_authority_exists:
        return (
            RuleEvaluation(
                category=RuleCategory.BLOCKED,
                reason_code="ACCEPTED_AUTHORITY_EXISTS",
                blockers=(
                    Blocker(
                        code="ACCEPTED_AUTHORITY_EXISTS",
                        message=(
                            "A Project with accepted authority cannot be abandoned."
                        ),
                    ),
                ),
            ),
        )
    return (
        RuleEvaluation(
            category=RuleCategory.AVAILABLE,
            reason_code="PROJECT_SHELL_CAN_BE_ABANDONED",
        ),
    )


_ABANDON_SHELL_NODE = NodeSpec(
    node_id="onboarding.abandon_shell",
    child_graph_id="onboarding",
    request_kind="abandon_project_shell",
    recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
    required_inputs=(InputField(name="reason", value_type="string"),),
    evaluate_rule=_abandon_shell_rule,
)


def _scope_aware_lifecycle_node(node: NodeSpec) -> NodeSpec:
    """Retire historical downstream routing only after exact reconciliation."""

    def completed_plan_is_current(snapshot: WorkflowFactSnapshot) -> bool:
        plans = tuple(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
        )
        parent_ids = {
            item.supersedes_artifact_id
            for item in plans
            if item.supersedes_artifact_id is not None
        }
        current = tuple(
            item
            for item in plans
            if item.artifact_id not in parent_ids and item.status != "superseded"
        )
        if len(current) != 1 or current[0].status != "accepted":
            return False
        plan = current[0]
        starts = tuple(
            item
            for item in snapshot.sprint_starts
            if item.sprint_plan_artifact_id == plan.artifact_id
            and item.plan_fingerprint == plan.artifact_fingerprint
        )
        completed_sprint_ids = {
            item.sprint_id for item in snapshot.sprints if item.status == "completed"
        }
        return len(starts) == 1 and starts[0].sprint_id in completed_sprint_ids

    def evaluate_rule(
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        reconciled = scope_reconciliation_retires_node(snapshot, node.node_id)
        completed_historical_plan = (
            node.node_id
            in {
                "planning.sprint.plan",
                "planning.sprint.start",
            }
            and scope_execution_is_complete(snapshot)
            and completed_plan_is_current(snapshot)
        )
        if reconciled or completed_historical_plan:
            return (
                RuleEvaluation(
                    category=RuleCategory.SATISFIED,
                    reason_code=(
                        "SCOPE_EXTENSION_RECONCILED"
                        if reconciled
                        else "CURRENT_SCOPE_EXECUTION_COMPLETE"
                    ),
                ),
            )
        return node.evaluate_rule(
            scope_reconciled_snapshot(snapshot),
            evaluated_at,
        )

    return replace(node, evaluate_rule=evaluate_rule)


_ROOT_VISION_NODES: tuple[NodeSpec, ...] = tuple(
    _scope_aware_lifecycle_node(node) for node in VISION_NODES
)
_ROOT_BACKLOG_NODES: tuple[NodeSpec, ...] = tuple(
    _scope_aware_lifecycle_node(node) for node in BACKLOG_NODES
)
_ROOT_PLANNING_NODES: tuple[NodeSpec, ...] = tuple(
    _scope_aware_lifecycle_node(node) for node in PLANNING_NODES
)
_ROOT_ONBOARDING_NODES: tuple[NodeSpec, ...] = (
    *GREENFIELD_ONBOARDING_NODES[:-1],
    *BROWNFIELD_ONBOARDING_NODES,
    GREENFIELD_ONBOARDING_NODES[-1],
    _ABANDON_SHELL_NODE,
)
_ROOT_NON_SCOPE_NODES: tuple[NodeSpec, ...] = (
    *_ROOT_ONBOARDING_NODES,
    *AUTHORITY_NODES,
    *_ROOT_VISION_NODES,
    *_ROOT_BACKLOG_NODES,
    *_ROOT_PLANNING_NODES,
    *EXECUTION_NODES,
)


def _project_is_otherwise_terminal(
    snapshot: WorkflowFactSnapshot,
    evaluated_at: datetime,
) -> bool:
    """Return whether every non-scope required or recovery rule is satisfied."""
    for node in _ROOT_NON_SCOPE_NODES:
        for evaluation in node.evaluate_rule(snapshot, evaluated_at):
            recommendation = evaluation.recommendation_kind or node.recommendation_kind
            if evaluation.category is not RuleCategory.SATISFIED and recommendation in {
                RecommendationKind.REQUIRED,
                RecommendationKind.RECOVERY,
            }:
                return False
    return True


def _terminal_only_scope_start_node(node: NodeSpec) -> NodeSpec:
    """Expose optional scope re-entry only at the root terminal boundary."""

    def evaluate_rule(
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        evaluations = node.evaluate_rule(snapshot, evaluated_at)
        if not any(
            evaluation.category is RuleCategory.AVAILABLE for evaluation in evaluations
        ):
            return evaluations
        if _project_is_otherwise_terminal(snapshot, evaluated_at):
            return evaluations
        return (
            RuleEvaluation(
                category=RuleCategory.SATISFIED,
                reason_code="PROJECT_NOT_TERMINAL",
            ),
        )

    return replace(node, evaluate_rule=evaluate_rule)


_ROOT_SCOPE_EXTENSION_NODES: tuple[NodeSpec, ...] = tuple(
    _terminal_only_scope_start_node(node)
    if node.node_id == "scope_extension.start"
    else node
    for node in SCOPE_EXTENSION_NODES
)

ROOT_GRAPH: WorkflowGraph = WorkflowGraph(
    graph_version=GRAPH_VERSION,
    root=ChildGraphSpec(
        child_graph_id="product_lifecycle",
        nodes=(),
        children=(
            ChildGraphSpec(
                child_graph_id="onboarding",
                nodes=_ROOT_ONBOARDING_NODES,
            ),
            ChildGraphSpec(child_graph_id="authority", nodes=AUTHORITY_NODES),
            ChildGraphSpec(child_graph_id="vision", nodes=_ROOT_VISION_NODES),
            ChildGraphSpec(child_graph_id="backlog", nodes=_ROOT_BACKLOG_NODES),
            ChildGraphSpec(
                child_graph_id="planning",
                nodes=_ROOT_PLANNING_NODES,
            ),
            ChildGraphSpec(child_graph_id="execution", nodes=EXECUTION_NODES),
            ChildGraphSpec(
                child_graph_id="scope_extension",
                nodes=_ROOT_SCOPE_EXTENSION_NODES,
            ),
        ),
    ),
)


def project_graph() -> WorkflowGraph:
    """Return the complete immutable Project workflow graph."""
    return ROOT_GRAPH
