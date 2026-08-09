"""Provider-free Product Goal recipe contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adapters.adk.agents.product_goal import root_agent
from adapters.adk.recipes import (
    AGENTIC_NODE_IDS,
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    _product_goal_interview_output_adapter,
    build_agentic_recipe_registry,
)
from services.contracts.product_goal import ProductGoalInterviewOutput
from workflow.requests import RecordProductGoalInterviewTurn

ATTEMPT_ID = 3


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


def test_product_goal_recipe_adapter_uses_the_persisted_human_response() -> None:
    """Model output cannot replace the host-captured interview response."""
    request = _product_goal_interview_output_adapter(
        RecipeOutput(
            payload={
                "updated_components": {
                    "valuable_future_state": "A predictable delivery flow",
                    "beneficiary": "Operators",
                    "value": "Trustworthy evidence",
                    "success_signals": ["Every decision is auditable"],
                    "boundaries": ["No provider call in tests"],
                },
                "product_goal_statement": "Operators trust lifecycle evidence.",
                "is_complete": True,
                "clarifying_questions": [],
            }
        ),
        AttemptCompletionContext(
            project_id=7,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            instance_key=None,
            attempt_id=ATTEMPT_ID,
            attempt_fingerprint="sha256:attempt",
            idempotency_key="goal:completion",
            actor="operator@example.com",
            correlation_id=None,
            normalized_input={"user_response": "We need durable evidence."},
        ),
    )

    assert isinstance(request, RecordProductGoalInterviewTurn)
    assert request.user_text == "We need durable evidence."
    assert request.attempt_id == ATTEMPT_ID


def test_product_goal_recipe_is_registered_without_provider_execution() -> None:
    """The v2 catalog exposes the Goal recipe and output adapter."""
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=root_agent,
            authority_repair=root_agent,
            vision_interview=root_agent,
            product_goal=root_agent,
            backlog_generation=root_agent,
            roadmap_generation=root_agent,
            story_generation=root_agent,
            sprint_planning=root_agent,
        ),
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
    )

    assert registry.node_ids == AGENTIC_NODE_IDS
    assert registry.require("goal.interview").output_adapter is (
        _product_goal_interview_output_adapter
    )
