"""Provider-free Product Goal recipe contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.product_goal import ProductGoalInterviewOutput


def test_product_goal_output_requires_complete_components_for_a_candidate() -> None:
    """A model cannot claim a complete Goal while retaining clarification gaps."""
    with pytest.raises(ValidationError):
        ProductGoalInterviewOutput.model_validate(
            {
                "updated_components": {
                    "target_users": "Operators",
                    "problem": "Slow setup",
                    "desired_outcome": "Faster setup",
                    "measurable_signal": "Minutes to first value",
                    "constraints": "No provider call",
                },
                "product_goal_statement": "Reduce setup time",
                "is_complete": True,
                "clarifying_questions": ["What baseline applies?"],
            }
        )
