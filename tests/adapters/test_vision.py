"""Vision ADK adapter tests without provider execution."""

from __future__ import annotations

from adapters.adk.recipes import (
    AttemptCompletionContext,
    RecipeOutput,
    _vision_interview_output_adapter,
)


def test_vision_adapter_uses_trusted_attempt_input_for_human_turn() -> None:
    """Model output cannot replace the captured user response or interview mode."""
    completion = _vision_interview_output_adapter(
        RecipeOutput(
            payload={
                "updated_components": {
                    "project_name": "Vision",
                    "target_user": "Operators",
                    "problem": "State drift",
                    "product_category": "Tool",
                    "key_benefit": "Trust",
                    "competitors": "Spreadsheets",
                    "differentiator": "Facts",
                },
                "project_vision_statement": "A trusted workflow tool.",
                "is_complete": True,
                "clarifying_questions": [],
            }
        ),
        AttemptCompletionContext(
            project_id=1,
            graph_version="agileforge.workflow.v1",
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            instance_key=None,
            attempt_id=2,
            attempt_fingerprint="sha256:attempt",
            idempotency_key="vision:complete",
            actor="operator@example.com",
            correlation_id=None,
            normalized_input={
                "mode": "revision",
                "user_response": "Correct the target user.",
            },
        ),
    )

    assert completion.mode == "revision"
    assert completion.user_text == "Correct the target user."
