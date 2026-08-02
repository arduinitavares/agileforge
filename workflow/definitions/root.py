"""Root product-lifecycle workflow graph hierarchy."""

from datetime import datetime

from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    InputField,
    RecommendationKind,
)
from workflow.definitions.onboarding import (
    GREENFIELD_ONBOARDING_NODES,
    has_historical_accepted_authority,
)
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

ROOT_GRAPH: WorkflowGraph = WorkflowGraph(
    graph_version=GRAPH_VERSION,
    root=ChildGraphSpec(
        child_graph_id="product_lifecycle",
        nodes=(),
        children=(
            ChildGraphSpec(
                child_graph_id="onboarding",
                nodes=(*GREENFIELD_ONBOARDING_NODES, _ABANDON_SHELL_NODE),
            ),
            ChildGraphSpec(child_graph_id="authority", nodes=()),
            ChildGraphSpec(child_graph_id="vision", nodes=()),
            ChildGraphSpec(child_graph_id="backlog", nodes=()),
            ChildGraphSpec(child_graph_id="planning", nodes=()),
            ChildGraphSpec(child_graph_id="execution", nodes=()),
            ChildGraphSpec(child_graph_id="scope_extension", nodes=()),
        ),
    ),
)
