"""Single-project root graph contract tests."""

from workflow.definitions.root import ROOT_GRAPH


def test_root_graph_has_exact_v2_lifecycle_order_without_retired_nodes() -> None:
    """Expose one product lifecycle without setup or reconciliation wrappers."""
    assert ROOT_GRAPH.graph_version == "agileforge.workflow.v2"
    assert tuple(child.child_graph_id for child in ROOT_GRAPH.root.children) == (
        "vision",
        "product_goal",
        "product_discovery",
        "authority",
        "backlog",
        "planning",
        "execution",
    )
    assert {
        "onboarding.greenfield",
        "onboarding.brownfield.curation",
        "onboarding.abandon_shell",
        "scope_extension.start",
        "scope_extension.reconcile",
    }.isdisjoint(node.node_id for node in ROOT_GRAPH.root.iter_nodes())
