"""Graph contracts for context-grounded Project Vision bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime

from workflow.contracts import NodeCategory
from workflow.definitions.root import project_graph
from workflow.facts import ProjectFact, WorkflowFactSnapshot

NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)


def test_new_project_requires_zero_input_vision_bootstrap() -> None:
    """A new Project starts with explicit generation, not a human-first form."""
    position = project_graph().evaluate(
        WorkflowFactSnapshot(
            project=ProjectFact(
                project_id=1,
                name="String Calculator Lab",
                created_at=NOW,
            )
        ),
        NOW,
    )

    decision = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.required_inputs == ()
    assert "vision.interview" not in position.available_nodes
