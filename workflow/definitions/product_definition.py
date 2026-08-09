"""Compatibility access to the complete Project lifecycle graph."""

from workflow.definitions.root import project_graph
from workflow.graph import WorkflowGraph


def product_definition_graph() -> WorkflowGraph:
    """Return the authoritative version-2 Project lifecycle graph."""
    return project_graph()


__all__ = ["product_definition_graph"]
