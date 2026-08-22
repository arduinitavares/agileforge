"""TDD tests for User Story Writer agent factory and configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adapters.adk.agents.story import (
    USER_STORY_WRITER_INSTRUCTIONS,
    create_user_story_patch_agent,
    create_user_story_writer_agent,
    root_agent,
)
from services.contracts.story import (
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


def test_story_writer_output_rejects_retired_patch_envelope() -> None:
    """Correction output uses the current one-item writer contract."""
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
        UserStoryWriterOutput.model_validate(payload)


def test_user_story_patch_agent_uses_current_writer_output_schema() -> None:
    """Correction agent binds the current writer schema without an alias."""
    patch_agent = create_user_story_patch_agent()
    full_agent = create_user_story_writer_agent()

    assert patch_agent is not full_agent
    assert patch_agent.name == "user_story_patch_tool"
    assert patch_agent.input_schema is UserStoryWriterInput
    assert patch_agent.output_schema is UserStoryWriterOutput
    assert full_agent.output_schema is UserStoryWriterOutput


def test_high_story_example_omits_placeholder_warning() -> None:
    """Verify the high-story example omits placeholder warnings."""
    instructions = USER_STORY_WRITER_INSTRUCTIONS
    assert (
        '"decomposition_warning": "Only include this key if score is Low"'
        not in instructions
    )


def test_instructions_bound_decomposition_warning_to_quality_failure() -> None:
    """Verify the warning is absent unless decomposition quality fails."""
    instructions = USER_STORY_WRITER_INSTRUCTIONS
    assert (
        "decomposition_warning, null unless the Story fails decomposition quality"
        in instructions
    )
