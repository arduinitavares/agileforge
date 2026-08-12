"""Root product-lifecycle workflow graph hierarchy."""

from workflow.contracts import GRAPH_VERSION
from workflow.definitions.authority import AUTHORITY_NODES
from workflow.definitions.backlog import BACKLOG_NODES
from workflow.definitions.execution import EXECUTION_NODES
from workflow.definitions.planning import PLANNING_NODES
from workflow.definitions.product_discovery import SPECIFICATION_NODES
from workflow.definitions.product_goal import PRODUCT_GOAL_NODES
from workflow.definitions.vision import VISION_INTERVIEW_NODES
from workflow.graph import ChildGraphSpec, WorkflowGraph

ROOT_GRAPH: WorkflowGraph = WorkflowGraph(
    graph_version=GRAPH_VERSION,
    root=ChildGraphSpec(
        child_graph_id="product_lifecycle",
        nodes=(),
        children=(
            ChildGraphSpec(child_graph_id="vision", nodes=VISION_INTERVIEW_NODES),
            ChildGraphSpec(child_graph_id="product_goal", nodes=PRODUCT_GOAL_NODES),
            ChildGraphSpec(
                child_graph_id="specification",
                nodes=SPECIFICATION_NODES,
            ),
            ChildGraphSpec(child_graph_id="authority", nodes=AUTHORITY_NODES),
            ChildGraphSpec(child_graph_id="backlog", nodes=BACKLOG_NODES),
            ChildGraphSpec(child_graph_id="planning", nodes=PLANNING_NODES),
            ChildGraphSpec(child_graph_id="execution", nodes=EXECUTION_NODES),
        ),
    ),
)


def project_graph() -> WorkflowGraph:
    """Return the complete immutable Project workflow graph."""
    return ROOT_GRAPH
