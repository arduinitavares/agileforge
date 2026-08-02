"""Root product-lifecycle workflow graph hierarchy."""

from workflow.contracts import GRAPH_VERSION
from workflow.graph import ChildGraphSpec, WorkflowGraph

ROOT_GRAPH: WorkflowGraph = WorkflowGraph(
    graph_version=GRAPH_VERSION,
    root=ChildGraphSpec(
        child_graph_id="product_lifecycle",
        nodes=(),
        children=(
            ChildGraphSpec(child_graph_id="onboarding", nodes=()),
            ChildGraphSpec(child_graph_id="authority", nodes=()),
            ChildGraphSpec(child_graph_id="vision", nodes=()),
            ChildGraphSpec(child_graph_id="backlog", nodes=()),
            ChildGraphSpec(child_graph_id="planning", nodes=()),
            ChildGraphSpec(child_graph_id="execution", nodes=()),
            ChildGraphSpec(child_graph_id="scope_extension", nodes=()),
        ),
    ),
)
