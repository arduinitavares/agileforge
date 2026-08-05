"""Contract tests for the isolated Vision interview agent."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.vision import (
    VisionComponents,
    VisionInterviewInput,
    VisionInterviewOutput,
)


def _components(**overrides: str | None) -> VisionComponents:
    """Build one complete Vision component set with focused overrides."""
    values: dict[str, str | None] = {
        "project_name": "AgileForge",
        "target_user": "Product operators",
        "problem": "Workflow state is difficult to trust",
        "product_category": "Local workflow tool",
        "key_benefit": "Durable workflow decisions",
        "competitors": "Spreadsheets",
        "differentiator": "Evidence-backed state",
    }
    values.update(overrides)
    return VisionComponents(**values)


def test_interview_input_is_focused_and_strips_required_strings() -> None:
    """Only project identity, interview state, and the human response reach ADK."""
    parsed = VisionInterviewInput(
        project_name="  AgileForge  ",
        project_description="  Durable workflow graph.  ",
        mode="initial",
        user_response="  Build a reliable workflow tool.  ",
        prior_components=None,
        accepted_vision_statement=None,
    )

    assert parsed.model_dump() == {
        "project_name": "AgileForge",
        "project_description": "Durable workflow graph.",
        "mode": "initial",
        "user_response": "Build a reliable workflow tool.",
        "prior_components": None,
        "accepted_vision_statement": None,
    }
    with pytest.raises(ValidationError):
        VisionInterviewInput(
            project_name=" ",
            project_description=None,
            mode="initial",
            user_response="Answer",
            prior_components=None,
            accepted_vision_statement=None,
        )


def test_incomplete_output_requires_nonempty_questions() -> None:
    """An incomplete Vision response must direct the next human turn."""
    partial = _components(problem=None)

    output = VisionInterviewOutput(
        updated_components=partial,
        project_vision_statement="  A durable workflow tool.  ",
        is_complete=False,
        clarifying_questions=["  What problem should it solve?  "],
    )

    assert output.project_vision_statement == "A durable workflow tool."
    assert output.clarifying_questions == ["What problem should it solve?"]
    with pytest.raises(ValidationError):
        VisionInterviewOutput(
            updated_components=partial,
            project_vision_statement="A durable workflow tool.",
            is_complete=False,
            clarifying_questions=[],
        )


def test_output_completion_matches_components() -> None:
    """The model cannot claim a completed Vision before all components exist."""
    complete = _components()

    parsed = VisionInterviewOutput(
        updated_components=complete,
        project_vision_statement="A durable workflow tool.",
        is_complete=True,
        clarifying_questions=[],
    )

    assert parsed.is_complete is True
    with pytest.raises(ValidationError):
        VisionInterviewOutput(
            updated_components=_components(competitors=None),
            project_vision_statement="A durable workflow tool.",
            is_complete=True,
            clarifying_questions=["Which alternatives exist?"],
        )
