"""Product Goal interview contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.product_goal import (
    ProductGoalComponents,
    ProductGoalInterviewOutput,
)


def _complete_components() -> ProductGoalComponents:
    return ProductGoalComponents(
        valuable_future_state="Operators make decisions from durable facts.",
        beneficiary="Delivery operators",
        value="They can trust the lifecycle state.",
        success_signals=("Every decision has durable evidence.",),
        boundaries=("No implementation tasks.",),
    )


def test_complete_goal_requires_every_component_and_no_questions() -> None:
    """Complete Goal output cannot retain a clarification request."""
    output = ProductGoalInterviewOutput(
        updated_components=_complete_components(),
        product_goal_statement="Operators trust lifecycle evidence.",
        is_complete=True,
        clarifying_questions=[],
    )
    assert output.updated_components.is_fully_defined()


def test_incomplete_goal_requires_focused_question() -> None:
    """Incomplete semantic components require at least one clarifier."""
    with pytest.raises(ValidationError, match="clarifying"):
        ProductGoalInterviewOutput(
            updated_components=ProductGoalComponents(beneficiary="Operators"),
            product_goal_statement="A draft goal.",
            is_complete=False,
            clarifying_questions=[],
        )


def test_components_strip_and_reject_blank_values() -> None:
    """Every supplied semantic component must contain meaningful text."""
    with pytest.raises(ValidationError):
        ProductGoalComponents(success_signals=(" ",))
    assert _complete_components().beneficiary == "Delivery operators"
