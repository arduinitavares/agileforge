"""Isolated Vision/Backlog graph used by Task 10 transition tests."""

from workflow.contracts import GRAPH_VERSION
from workflow.definitions.backlog import BACKLOG_NODES, PLANNING_BOUNDARY_NODE
from workflow.definitions.vision import VISION_NODES
from workflow.graph import ChildGraphSpec, WorkflowGraph


def product_definition_graph() -> WorkflowGraph:
    """Return Vision, Backlog, and their explicit planning boundary join."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="product_lifecycle",
            nodes=(),
            children=(
                ChildGraphSpec(child_graph_id="vision", nodes=VISION_NODES),
                ChildGraphSpec(child_graph_id="backlog", nodes=BACKLOG_NODES),
                ChildGraphSpec(
                    child_graph_id="planning",
                    nodes=(PLANNING_BOUNDARY_NODE,),
                ),
            ),
        ),
    )


__all__ = ["product_definition_graph"]
