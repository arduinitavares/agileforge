"""TDD tests for User Story Writer agent factory and configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adapters.adk.agents.story import (
    USER_STORY_PATCH_INSTRUCTIONS,
    USER_STORY_WRITER_INSTRUCTIONS,
    create_user_story_patch_agent,
    create_user_story_writer_agent,
    preserve_story_output_schema,
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


def test_agent_preserves_raw_output_for_recipe_validation() -> None:
    """Keep required-null field presence intact until strict recipe validation."""
    assert root_agent.output_schema is None
    assert root_agent.before_model_callback is preserve_story_output_schema


def test_agent_does_not_save_lossy_structured_output_to_state() -> None:
    """Do not let ADK dump required-null Story output with exclude_none."""
    assert root_agent.output_key is None


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
            "invest_assessment": {
                "independent": {
                    "result": "pass",
                    "rationale": "Actionable story.",
                    "evidence": "Contained.",
                },
                "negotiable": {
                    "result": "pass",
                    "rationale": "Actionable story.",
                    "evidence": "Contained.",
                },
                "valuable": {
                    "result": "pass",
                    "rationale": "Actionable story.",
                    "evidence": "Contained.",
                },
                "estimable": {
                    "result": "pass",
                    "rationale": "Actionable story.",
                    "evidence": "Contained.",
                },
                "small": {
                    "result": "pass",
                    "rationale": "Actionable story.",
                    "evidence": "Contained.",
                },
                "testable": {
                    "result": "pass",
                    "rationale": "Actionable story.",
                    "evidence": "Contained.",
                },
            },
            "estimated_effort": "S",
            "produced_artifacts": [],
        },
        "user_stories": [],
        "is_complete": True,
        "clarifying_questions": [],
    }

    with pytest.raises(ValidationError):
        UserStoryWriterOutput.model_validate(payload)


def test_user_story_patch_agent_preserves_the_same_strict_recipe_boundary() -> None:
    """Correction requests the current schema but returns raw JSON to the recipe."""
    patch_agent = create_user_story_patch_agent()
    full_agent = create_user_story_writer_agent()

    assert patch_agent is not full_agent
    assert patch_agent.name == "user_story_patch_tool"
    assert patch_agent.input_schema is UserStoryWriterInput
    assert patch_agent.output_schema is None
    assert full_agent.output_schema is None
    assert patch_agent.before_model_callback is preserve_story_output_schema
    assert full_agent.before_model_callback is preserve_story_output_schema


def test_instructions_require_explainable_invest_assessment_with_rubric() -> None:
    """Verify prompt instructions define INVEST rubric and advisory semantics."""
    for instructions in (
        USER_STORY_WRITER_INSTRUCTIONS,
        USER_STORY_PATCH_INSTRUCTIONS,
    ):
        assert "INVEST Assessment Rubric & Operational Semantics" in instructions
        assert (
            "INVEST assessments are advisory model recommendations "
            "provided as evidence for human review"
        ) in instructions
        assert "result: \"pass\", \"concern\", or \"fail\"" in instructions
        assert "rationale: concise non-blank explanation" in instructions
        assert "evidence: concrete non-blank citation" in instructions
        for dim in (
            "independent",
            "negotiable",
            "valuable",
            "estimable",
            "small",
            "testable",
        ):
            assert dim in instructions
        assert "vertical slice" in instructions
        assert "bounded scenario scope" in instructions
        assert "XS, S, or M" not in instructions
        assert "XS/S/M" not in instructions
        assert "iteration capacity" not in instructions
        assert "invest_score: High, Medium, or Low" not in instructions
        assert "decomposition_warning" not in instructions


def test_instructions_define_planning_proposal_semantics() -> None:
    """Verify prompt instructions define sizing mapping and planning semantics."""
    for instructions in (
        USER_STORY_WRITER_INSTRUCTIONS,
        USER_STORY_PATCH_INSTRUCTIONS,
    ):
        assert (
            "Planning Proposal Semantics (Sizing, Ordering, Dependencies):"
            in instructions
        )
        assert (
            "Planning metadata values are advisory model recommendations"
            in instructions
        )
        assert (
            "XS: 1 point, S: 2 points, M: 3 points, L: 5 points, XL: 8 points"
            in instructions
        )
        assert "Developer sizing responsibility" in instructions
        assert "dependency_candidates" in instructions
        assert "Human acceptance binds the exact visible package" in instructions
