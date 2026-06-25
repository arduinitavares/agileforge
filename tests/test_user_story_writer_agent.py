"""TDD tests for User Story Writer agent factory and configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator_agent.agent_tools.user_story_writer_tool.agent import (
    INSTRUCTIONS_PATH,
    create_user_story_patch_agent,
    create_user_story_writer_agent,
    root_agent,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.schemes import (
    UserStoryPatchOutput,
    UserStoryWriterInput,
    UserStoryWriterOutput,
)


def test_agent_has_correct_name() -> None:
    """Verify agent has correct name."""
    assert root_agent.name == "user_story_writer_tool"


def test_agent_has_input_schema() -> None:
    """Verify agent has input schema."""
    assert root_agent.input_schema is UserStoryWriterInput


def test_agent_has_output_schema() -> None:
    """Verify agent has output schema."""
    assert root_agent.output_schema is UserStoryWriterOutput


def test_agent_has_output_key() -> None:
    """Verify agent has output key."""
    assert root_agent.output_key == "story_output"


def test_factory_returns_new_instance() -> None:
    """Verify factory returns new instance."""
    new_agent = create_user_story_writer_agent()
    assert new_agent is not root_agent
    assert new_agent.name == "user_story_writer_tool"


def test_user_story_patch_output_rejects_user_stories_field() -> None:
    """Patch output must not accept full-list story artifacts."""
    payload = {
        "artifact_kind": "story_patch",
        "parent_requirement": "Requirement A",
        "target_refinement_slot": 2,
        "story": {
            "story_title": "Refined target story",
            "statement": (
                "As a user, I want a refined target story, so that the work is clear."
            ),
            "acceptance_criteria": ["Verify that the target story is actionable."],
            "invest_score": "High",
            "estimated_effort": "S",
            "produced_artifacts": [],
        },
        "user_stories": [],
        "is_complete": True,
        "clarifying_questions": [],
    }

    with pytest.raises(ValidationError):
        UserStoryPatchOutput.model_validate(payload)


def test_user_story_patch_agent_uses_patch_output_schema() -> None:
    """Patch agent must bind the patch schema on a fresh ADK Agent."""
    patch_agent = create_user_story_patch_agent()
    full_agent = create_user_story_writer_agent()

    assert patch_agent is not full_agent
    assert patch_agent.name == "user_story_patch_tool"
    assert patch_agent.input_schema is UserStoryWriterInput
    assert patch_agent.output_schema is UserStoryPatchOutput
    assert full_agent.output_schema is UserStoryWriterOutput


def test_instructions_example_does_not_include_placeholder_warning_on_high_story() -> (
    None
):
    """Verify instructions example does not include placeholder warning on high story."""  # noqa: E501
    instructions = Path(INSTRUCTIONS_PATH).read_text(encoding="utf-8")
    assert (
        '"decomposition_warning": "Only include this key if score is Low"'
        not in instructions
    )


def test_instructions_forbid_warning_on_non_low_scores() -> None:
    """Verify instructions forbid warning on non low scores."""
    instructions = Path(INSTRUCTIONS_PATH).read_text(encoding="utf-8")
    assert (
        "Never include `decomposition_warning` on a story scored `High` or `Medium`."
        in instructions
    )
