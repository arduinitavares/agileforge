"""Tests for sprint planner schemas."""

from typing import Any

from pydantic import ValidationError

from orchestrator_agent.agent_tools.sprint_planner_tool.schemes import (
    SprintPlannerCapacityAnalysis,
    SprintPlannerInput,
    SprintPlannerOutput,
    SprintPlannerSelectedStory,
    validate_task_decomposition_quality,
    validate_task_invariant_bindings,
)


def _build_output_payload() -> dict[str, Any]:
    return {
        "sprint_goal": "Ship login onboarding",
        "sprint_number": 1,
        "duration_days": 14,
        "selected_stories": [
            {
                "story_id": 101,
                "story_title": "Enable login",
                "tasks": [
                    {
                        "description": "Create auth table",
                        "task_kind": "implementation",
                        "checklist_items": [
                            "Define the auth table columns",
                            "Add persistence coverage for auth records",
                        ],
                        "artifact_targets": ["auth schema"],
                        "workstream_tags": ["backend", "auth"],
                        "relevant_invariant_ids": ["INV-123"],
                    },
                    {
                        "description": "Add login UI",
                        "task_kind": "implementation",
                        "checklist_items": [
                            "Render the login form locally",
                            "Wire submit handling to the login flow",
                        ],
                        "artifact_targets": ["login form"],
                        "workstream_tags": ["frontend", "auth"],
                        "relevant_invariant_ids": [],
                    },
                ],
                "reason_for_selection": "Core to sprint goal",
            }
        ],
        "deselected_stories": [{"story_id": 102, "reason": "Does not fit capacity"}],
        "capacity_analysis": {
            "velocity_assumption": "Medium",
            "capacity_band": "4-5 stories",
            "selected_count": 1,
            "story_points_used": 3,
            "max_story_points": 10,
            "commitment_note": "Does this scope feel achievable in 2 weeks?",
            "reasoning": "Scope fits the band and aligns to the goal.",
        },
    }


def test_output_schema_round_trip() -> None:
    """Ensure output schema supports JSON round-trip validation."""
    payload = _build_output_payload()
    model = SprintPlannerOutput.model_validate(payload)
    dumped = model.model_dump_json()
    restored = SprintPlannerOutput.model_validate_json(dumped)
    assert restored.sprint_goal == payload["sprint_goal"]
    assert restored.selected_stories[0].tasks[0].checklist_items == [
        "Define the auth table columns",
        "Add persistence coverage for auth records",
    ]


def test_output_schema_rejects_extra_fields() -> None:
    """Ensure extra keys are rejected in output schema."""
    payload = _build_output_payload()
    payload["extra"] = "not allowed"
    try:
        SprintPlannerOutput.model_validate(payload)
    except ValidationError as exc:
        assert "extra" in str(exc)  # noqa: PT017
    else:
        msg = "Expected ValidationError for extra fields"
        raise AssertionError(msg)


def test_input_schema_accepts_optional_fields() -> None:
    """Ensure input schema accepts optional capacity and task flags."""
    input_payload: dict[str, Any] = {
        "available_stories": [
            {
                "story_id": 101,
                "story_title": "Enable login",
                "story_description": "Add login functionality",
                "acceptance_criteria_items": ["Can log in"],
                "persona": "User",
                "source_requirement": "Req 1",
                "priority": 1,
                "story_points": 3,
                "parent_group": 1,
                "group_slot": 1,
                "evaluated_invariant_ids": ["INV-123"],
                "story_compliance_boundary_summaries": ["Must log in"],
            }
        ],
        "team_velocity_assumption": "Low",
        "sprint_duration_days": 10,
        "user_context": "Focus on onboarding",
        "max_story_points": 8,
        "include_task_decomposition": False,
    }
    model = SprintPlannerInput.model_validate(input_payload)
    assert model.max_story_points == 8  # noqa: PLR2004
    assert model.include_task_decomposition is False
    assert model.available_stories[0].parent_group == 1
    assert model.available_stories[0].group_slot == 1


def test_input_schema_requires_duration_days() -> None:
    """Ensure sprint duration is required in input schema."""
    input_payload: dict[str, Any] = {
        "available_stories": [
            {
                "story_id": 101,
                "story_title": "Enable login",
                "story_description": "Add login functionality",
                "acceptance_criteria_items": [],
                "persona": None,
                "source_requirement": None,
                "priority": 1,
                "story_points": 3,
                "evaluated_invariant_ids": [],
                "story_compliance_boundary_summaries": [],
            }
        ],
        "team_velocity_assumption": "Low",
        "user_context": "Focus on onboarding",
        "max_story_points": 8,
        "include_task_decomposition": False,
    }
    try:
        SprintPlannerInput.model_validate(input_payload)
    except ValidationError as exc:
        assert "sprint_duration_days" in str(exc)  # noqa: PT017
    else:
        msg = "Expected ValidationError for missing sprint_duration_days"
        raise AssertionError(msg)


def test_selected_story_requires_reason() -> None:
    """Ensure selected stories include a reason for selection."""
    payload: dict[str, Any] = {
        "story_id": 201,
        "story_title": "Password reset",
        "tasks": [
            {
                "description": "Add reset API",
                "task_kind": "implementation",
                "artifact_targets": ["reset API"],
                "workstream_tags": ["backend"],
                "relevant_invariant_ids": [],
            }
        ],
        "reason_for_selection": "Critical to account access",
    }
    model = SprintPlannerSelectedStory.model_validate(payload)
    assert model.story_id == 201  # noqa: PLR2004


def test_selected_story_rejects_legacy_string_tasks() -> None:
    """Verify selected story rejects legacy string tasks."""
    payload: dict[str, Any] = {
        "story_id": 201,
        "story_title": "Password reset",
        "tasks": ["Add reset API"],
        "reason_for_selection": "Critical to account access",
    }
    try:
        SprintPlannerSelectedStory.model_validate(payload)
    except ValidationError as exc:
        assert "tasks" in str(exc)  # noqa: PT017
    else:
        msg = "Expected ValidationError for legacy string tasks"
        raise AssertionError(msg)


def test_validate_task_invariant_bindings_rejects_out_of_scope_ids() -> None:
    """Verify validate task invariant bindings rejects out of scope ids."""
    model = SprintPlannerOutput.model_validate(_build_output_payload())
    errors = validate_task_invariant_bindings(
        model,
        allowed_invariant_ids_by_story={101: []},
    )
    assert errors == [
        "Story 101 task 'Create auth table' referenced invalid invariant IDs: INV-123"
    ]


def test_validate_task_decomposition_quality_rejects_story_acceptance_criteria_copy() -> (  # noqa: E501
    None
):
    """Verify validate task decomposition quality rejects story acceptance criteria copy."""  # noqa: E501
    model = SprintPlannerOutput.model_validate(_build_output_payload())
    errors = validate_task_decomposition_quality(
        model,
        include_task_decomposition=True,
        acceptance_criteria_items_by_story={101: ["Define the auth table columns"]},
    )
    assert errors == [
        "Story 101 task 'Create auth table': checklist item 'Define the auth table columns' duplicates story acceptance criteria."  # noqa: E501
    ]


def test_validate_task_decomposition_quality_rejects_broad_story_completion_phrase() -> (  # noqa: E501
    None
):
    """Verify validate task decomposition quality rejects broad story completion phrase."""  # noqa: E501
    payload = _build_output_payload()
    payload["selected_stories"][0]["tasks"][0]["checklist_items"] = [
        "Complete the story",
        "Add persistence coverage for auth records",
    ]
    model = SprintPlannerOutput.model_validate(payload)
    errors = validate_task_decomposition_quality(
        model,
        include_task_decomposition=True,
        acceptance_criteria_items_by_story={101: ["Define the auth table columns"]},
    )
    assert errors == [
        "Story 101 task 'Create auth table': checklist item 'Complete the story' is too story-level; use task-local completion criteria instead."  # noqa: E501
    ]


def test_capacity_analysis_requires_commitment_note() -> None:
    """Ensure capacity analysis includes commitment note."""
    payload: dict[str, Any] = {
        "velocity_assumption": "High",
        "capacity_band": "6-7 stories",
        "selected_count": 6,
        "story_points_used": 12,
        "max_story_points": 15,
        "commitment_note": "Does this scope feel achievable in 2 weeks?",
        "reasoning": "Capacity fits the high band.",
    }
    model = SprintPlannerCapacityAnalysis.model_validate(payload)
    assert model.selected_count == 6  # noqa: PLR2004
