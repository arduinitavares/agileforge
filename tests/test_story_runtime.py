"""Tests for story runtime."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from services import story_runtime

if TYPE_CHECKING:
    from orchestrator_agent.agent_tools.user_story_writer_tool.schemes import (
        UserStoryWriterInput,
    )

EXPECTED_SCHEMA_REPAIR_CALLS: int = 2
ADK_SCHEMA_ERROR_MESSAGE: str = "1 validation error for UserStoryWriterOutput"


def _base_state() -> dict[str, Any]:
    return {
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
    }


def _valid_story(title: str) -> dict[str, Any]:
    return {
        "story_title": title,
        "statement": (
            "As a Cartola operator, I want a validated live recommendation, "
            "so that I can review it before market lock."
        ),
        "acceptance_criteria": [
            "Verify that the recommendation artifact records the selected squad."
        ],
        "invest_score": "High",
        "estimated_effort": "M",
        "produced_artifacts": ["recommendation_artifact"],
    }


def _low_story(title: str) -> dict[str, Any]:
    story = _valid_story(title)
    story["invest_score"] = "Low"
    story["decomposition_warning"] = (
        "Story still combines too many uncertain decomposition choices."
    )
    return story


def _valid_story_output(
    parent_requirement: str,
    *,
    is_complete: bool = True,
) -> str:
    return json.dumps(
        {
            "parent_requirement": parent_requirement,
            "user_stories": [
                {
                    "story_title": "Projection-backed story",
                    "statement": "As a developer, I want projection-aware drafts, so that retries stay deterministic.",  # noqa: E501
                    "acceptance_criteria": [
                        "Verify that reusable drafts come from projections."
                    ],
                    "invest_score": "High",
                    "estimated_effort": "S",
                    "produced_artifacts": [],
                }
            ],
            "is_complete": is_complete,
            "clarifying_questions": [],
        }
    )


@pytest.mark.asyncio
async def test_run_story_agent_from_state_uses_latest_reusable_projection_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run story agent from state uses latest reusable projection draft."""
    captured: dict[str, Any] = {}

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        captured["payload"] = payload
        return _valid_story_output(payload.parent_requirement)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    state = _base_state()
    state["story_attempts"] = {
        "Requirement A": [
            {
                "output_artifact": {
                    "parent_requirement": "Requirement A",
                    "user_stories": [
                        {
                            "story_title": "Wrong raw draft",
                            "statement": "As a team, I want the wrong draft, so that this test catches raw attempt lookups.",  # noqa: E501
                            "acceptance_criteria": [
                                "Verify that raw attempt lookup is not used."
                            ],
                            "invest_score": "High",
                            "estimated_effort": "S",
                            "produced_artifacts": [],
                        }
                    ],
                    "is_complete": True,
                    "clarifying_questions": [],
                }
            }
        ]
    }
    state["interview_runtime"] = {
        "story": {
            "Requirement A": {
                "attempt_history": [
                    {
                        "attempt_id": "attempt-1",
                        "classification": "reusable_content_result",
                        "is_reusable": True,
                        "retryable": False,
                        "draft_kind": "complete_draft",
                        "output_artifact": {
                            "parent_requirement": "Requirement A",
                            "user_stories": [
                                {
                                    "story_title": "Projection draft",
                                    "statement": "As a developer, I want the projection draft, so that the runtime reuses the right attempt.",  # noqa: E501
                                    "acceptance_criteria": [
                                        "Verify that the projection draft is injected."
                                    ],
                                    "invest_score": "High",
                                    "estimated_effort": "S",
                                    "produced_artifacts": [],
                                }
                            ],
                            "is_complete": True,
                            "clarifying_questions": [],
                        },
                    }
                ],
                "draft_projection": {
                    "latest_reusable_attempt_id": "attempt-1",
                    "kind": "complete_draft",
                    "is_complete": True,
                },
                "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                "request_projection": {},
            }
        }
    }

    result = await story_runtime.run_story_agent_from_state(
        state,
        project_id=1,
        parent_requirement="Requirement A",
        user_input=None,
    )

    assert result["success"] is True
    assert "--- PREVIOUS DRAFT TO REFINE ---" in captured["payload"].requirement_context
    assert "Projection draft" in captured["payload"].requirement_context
    assert "Wrong raw draft" not in captured["payload"].requirement_context


@pytest.mark.asyncio
async def test_run_story_agent_from_state_includes_only_unabsorbed_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run story agent from state includes only unabsorbed feedback."""
    captured: dict[str, Any] = {}

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        captured["payload"] = payload
        return _valid_story_output(payload.parent_requirement)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    state = _base_state()
    state["interview_runtime"] = {
        "story": {
            "Requirement A": {
                "attempt_history": [],
                "draft_projection": {},
                "feedback_projection": {
                    "items": [
                        {
                            "feedback_id": "feedback-1",
                            "text": "Please narrow the scope.",
                            "status": "unabsorbed",
                            "absorbed_by_attempt_id": None,
                        },
                        {
                            "feedback_id": "feedback-2",
                            "text": "This older feedback was already handled.",
                            "status": "absorbed",
                            "absorbed_by_attempt_id": "attempt-1",
                        },
                    ],
                    "next_feedback_sequence": 2,
                },
                "request_projection": {},
            }
        }
    }

    result = await story_runtime.run_story_agent_from_state(
        state,
        project_id=1,
        parent_requirement="Requirement A",
        user_input=None,
    )

    assert result["success"] is True
    assert "--- USER REFINEMENT FEEDBACK ---" in captured["payload"].requirement_context
    assert "Please narrow the scope." in captured["payload"].requirement_context
    assert (
        "This older feedback was already handled."
        not in captured["payload"].requirement_context
    )


@pytest.mark.asyncio
async def test_run_story_agent_from_state_includes_current_call_user_input_before_projection_persistence(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run story agent from state includes current call user input before projection persistence."""  # noqa: E501
    captured: dict[str, Any] = {}

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        captured["payload"] = payload
        return _valid_story_output(payload.parent_requirement)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _base_state(),
        project_id=1,
        parent_requirement="Requirement A",
        user_input="Please keep this to one milestone.",
    )

    assert result["success"] is True
    assert "--- USER REFINEMENT FEEDBACK ---" in captured["payload"].requirement_context
    assert (
        "Please keep this to one milestone." in captured["payload"].requirement_context
    )


@pytest.mark.asyncio
async def test_run_story_agent_from_state_does_not_crash_on_unserializable_reusable_artifact(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run story agent from state does not crash on unserializable reusable artifact."""  # noqa: E501
    captured: dict[str, Any] = {}

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        captured["payload"] = payload
        return _valid_story_output(payload.parent_requirement)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    state = _base_state()
    state["interview_runtime"] = {
        "story": {
            "Requirement A": {
                "attempt_history": [
                    {
                        "attempt_id": "attempt-1",
                        "classification": "reusable_content_result",
                        "is_reusable": True,
                        "retryable": False,
                        "draft_kind": "complete_draft",
                        "output_artifact": {
                            "parent_requirement": "Requirement A",
                            "user_stories": [],
                            "is_complete": True,
                            "clarifying_questions": [],
                            "debug_handle": object(),
                        },
                    }
                ],
                "draft_projection": {
                    "latest_reusable_attempt_id": "attempt-1",
                    "kind": "complete_draft",
                    "is_complete": True,
                },
                "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                "request_projection": {},
            }
        }
    }

    result = await story_runtime.run_story_agent_from_state(
        state,
        project_id=1,
        parent_requirement="Requirement A",
        user_input=None,
    )

    assert result["success"] is True
    assert result["classification"] == "reusable_content_result"
    assert (
        "--- PREVIOUS DRAFT TO REFINE ---"
        not in captured["payload"].requirement_context
    )


@pytest.mark.asyncio
async def test_story_runtime_forces_incomplete_when_clarifying_questions_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify clarifying questions force an incomplete story draft."""

    async def fake_invoke_story_agent(payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": payload.parent_requirement,
                "user_stories": [
                    {
                        "story_title": "Live lineup decision",
                        "statement": "As a Cartola manager, I want a recommended lineup so that I can act before market lock.",  # noqa: E501
                        "acceptance_criteria": [
                            "Given eligible players exist, when the recommendation is generated, then a lineup is returned with player names and positions."  # noqa: E501
                        ],
                        "invest_score": "High",
                        "estimated_effort": "M",
                        "produced_artifacts": ["lineup_recommendation"],
                    }
                ],
                "is_complete": True,
                "clarifying_questions": [
                    "Which live-lock cutoff should the story use?"
                ],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await story_runtime.run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Live weekly recommendation MVP"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Live weekly recommendation MVP",
        user_input=None,
    )

    assert result["success"] is True
    assert result["is_complete"] is False
    assert result["draft_kind"] == "incomplete_draft"
    assert result["output_artifact"]["is_complete"] is False


@pytest.mark.asyncio
async def test_story_runtime_rejects_incomplete_without_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify incomplete story drafts need clarifying questions."""

    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Budget-bound live workflow",
                "user_stories": [_valid_story("Budget story")],
                "is_complete": False,
                "clarifying_questions": [],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await story_runtime.run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Budget-bound live workflow"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Budget-bound live workflow",
        user_input=None,
    )

    assert result["success"] is False
    assert result["classification"] == "nonreusable_schema_failure"
    assert result["failure_stage"] == "output_validation"
    assert "clarifying question" in result["failure_summary"].lower()


@pytest.mark.asyncio
async def test_story_runtime_rejects_complete_with_generic_clarifying_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generic clarifying questions fail before complete draft demotion."""

    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Budget-bound live workflow",
                "user_stories": [_valid_story("Budget story")],
                "is_complete": True,
                "clarifying_questions": ["Please clarify the requirements."],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await story_runtime.run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Budget-bound live workflow"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Budget-bound live workflow",
        user_input=None,
    )

    assert result["success"] is False
    assert result["classification"] == "nonreusable_schema_failure"
    assert result["failure_stage"] == "output_validation"


@pytest.mark.parametrize(
    "clarifying_question",
    [
        "   ",
        "Clarify?",
        "Please clarify the requirements.",
        "Can you please clarify the requirements?",
        "Can you provide more details?",
        "Can you tell me what should happen for this workflow?",
        "Can you explain what is expected for this requirement?",
        "Can you give me more details about this story?",
        "Can you clarify requirements for this item?",
    ],
)
@pytest.mark.asyncio
async def test_story_runtime_rejects_generic_clarifying_questions(
    clarifying_question: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generic clarifying questions are not accepted."""

    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Budget-bound live workflow",
                "user_stories": [_valid_story("Budget story")],
                "is_complete": False,
                "clarifying_questions": [clarifying_question],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await story_runtime.run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Budget-bound live workflow"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Budget-bound live workflow",
        user_input=None,
    )

    assert result["success"] is False
    assert result["classification"] == "nonreusable_schema_failure"
    assert result["failure_stage"] == "output_validation"
    assert "actionable" in result["failure_summary"].lower()


@pytest.mark.asyncio
async def test_story_runtime_accepts_concrete_clarifying_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify concrete clarifying questions keep incomplete drafts reusable."""

    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Budget-bound live workflow",
                "user_stories": [_valid_story("Budget story")],
                "is_complete": False,
                "clarifying_questions": [
                    "Which budget source should the live command require when no account balance is available?"  # noqa: E501
                ],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await story_runtime.run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Budget-bound live workflow"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Budget-bound live workflow",
        user_input=None,
    )

    assert result["success"] is True
    assert result["draft_kind"] == "incomplete_draft"
    assert result["is_reusable"] is True


@pytest.mark.asyncio
async def test_story_runtime_blocks_complete_all_low_quality_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete drafts with only Low stories are not reusable/saveable."""

    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Technology and Model Research Spike",
                "user_stories": [
                    _low_story("Research candidate models"),
                    _low_story("Research validation windows"),
                ],
                "is_complete": True,
                "clarifying_questions": [],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await story_runtime.run_story_agent_from_state(
        {
            "roadmap_releases": [
                {"items": ["Technology and Model Research Spike"]}
            ],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Technology and Model Research Spike",
        user_input=None,
    )

    assert result["success"] is True
    assert result["classification"] == "quality_gate_failed"
    assert result["draft_kind"] == "quality_blocked_draft"
    assert result["is_reusable"] is False
    assert result["is_complete"] is False
    assert result["quality"]["saveable"] is False
    assert result["quality"]["invest_score_counts"] == {
        "High": 0,
        "Medium": 0,
        "Low": 2,
    }
    assert result["quality"]["blocking_findings"][0]["code"] == (
        "ALL_STORIES_LOW_INVEST"
    )
    assert result["output_artifact"]["is_complete"] is False


@pytest.mark.asyncio
async def test_story_runtime_blocks_silent_completion_when_refinement_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request for more than one bounded attempt cannot silently complete."""
    requested_count = 15

    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Technology and Model Research Spike",
                "user_stories": [
                    _valid_story(f"Research slice {index}") for index in range(1, 9)
                ],
                "is_complete": True,
                "clarifying_questions": [],
                "coverage_status": "complete",
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )
    request_payload: story_runtime.StoryInputContext = {
        "parent_requirement": "Technology and Model Research Spike",
        "requirement_context": (
            "Requirement: Technology and Model Research Spike\n\n"
            "--- USER REFINEMENT FEEDBACK ---\n"
            f"Please split this into ~{requested_count} smaller stories."
        ),
        "technical_spec": "{}",
        "compiled_authority": "{}",
        "global_roadmap_context": "",
        "already_generated_milestone_stories": "",
        "artifact_registry": {},
    }

    result = await story_runtime.run_story_agent_request(
        request_payload,
        project_id=2,
        parent_requirement="Technology and Model Research Spike",
    )

    assert result["success"] is True
    assert result["classification"] == "quality_gate_failed"
    assert result["is_reusable"] is False
    assert result["quality"]["requested_story_count"] == requested_count
    assert result["quality"]["blocking_findings"][0]["code"] == (
        "REQUESTED_STORY_COUNT_EXCEEDS_CAP"
    )
    assert result["output_artifact"]["quality"]["coverage_status"] == "complete"


@pytest.mark.asyncio
async def test_story_runtime_surfaces_capacity_limited_remaining_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity-limited incomplete drafts are quality-gated, not schema failures."""
    remaining_scope = [
        "action-set research across relevant SP/setpoint variables",
        "reward shaping and delay-horizon validation",
    ]

    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Technology and Model Research Spike",
                "user_stories": [
                    _valid_story("Define research rubric"),
                    _valid_story("Compare model families"),
                ],
                "is_complete": False,
                "coverage_status": "partial_capacity_limited",
                "remaining_scope": remaining_scope,
                "clarifying_questions": [],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await story_runtime.run_story_agent_from_state(
        {
            "roadmap_releases": [
                {"items": ["Technology and Model Research Spike"]}
            ],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Technology and Model Research Spike",
        user_input=(
            "Split this across research rubric, model families, action-set "
            "research, reward shaping, delay horizon, and validation strategy."
        ),
    )

    assert result["success"] is True
    assert result["classification"] == "quality_gate_failed"
    assert result["draft_kind"] == "quality_blocked_draft"
    assert result["is_reusable"] is False
    assert result["is_complete"] is False
    assert result["quality"]["coverage_status"] == "partial_capacity_limited"
    assert result["quality"]["remaining_scope"] == remaining_scope
    assert result["quality"]["blocking_findings"][0]["code"] == (
        "PARTIAL_CAPACITY_LIMITED"
    )
    assert result["output_artifact"]["remaining_scope"] == remaining_scope


@pytest.mark.asyncio
async def test_story_runtime_invalid_json_is_nonreusable_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify story runtime invalid json is nonreusable schema failure."""

    async def fake_invoke(_payload: object) -> str:
        return '{"broken": '

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _base_state(),
        project_id=1,
        parent_requirement="Requirement A",
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "invalid_json"
    assert result["classification"] == "nonreusable_schema_failure"
    assert result["is_reusable"] is False
    assert result["draft_kind"] is None


@pytest.mark.asyncio
async def test_story_runtime_retries_schema_invalid_output_with_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify schema-invalid Story output receives one feedback repair attempt."""
    captured_contexts: list[str] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        captured_contexts.append(payload.requirement_context)
        if len(captured_contexts) == 1:
            return json.dumps(
                {
                    "parent_requirement": payload.parent_requirement,
                    "user_stories": [_valid_story("Budget proof story")],
                    "clarifying_questions": [],
                }
            )
        return _valid_story_output(payload.parent_requirement)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _base_state(),
        project_id=1,
        parent_requirement="Requirement A",
        user_input=None,
    )

    assert result["success"] is True
    assert result["classification"] == "reusable_content_result"
    assert len(captured_contexts) == EXPECTED_SCHEMA_REPAIR_CALLS
    assert "SYSTEM_FEEDBACK" in captured_contexts[1]
    assert "is_complete" in captured_contexts[1]
    assert "UserStoryWriterOutput" in captured_contexts[1]


@pytest.mark.asyncio
async def test_story_runtime_retries_adk_schema_validation_error_with_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ADK output-schema validation errors receive feedback retry."""
    captured_contexts: list[str] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        captured_contexts.append(payload.requirement_context)
        if len(captured_contexts) == 1:
            raise story_runtime.AgentInvocationError(
                ADK_SCHEMA_ERROR_MESSAGE,
                validation_errors=[
                    {
                        "loc": ("is_complete",),
                        "msg": "Field required",
                        "type": "missing",
                    }
                ],
            )
        return _valid_story_output(payload.parent_requirement)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _base_state(),
        project_id=1,
        parent_requirement="Requirement A",
        user_input=None,
    )

    assert result["success"] is True
    assert len(captured_contexts) == EXPECTED_SCHEMA_REPAIR_CALLS
    assert "SYSTEM_FEEDBACK" in captured_contexts[1]
    assert "is_complete" in captured_contexts[1]
    assert "UserStoryWriterOutput" in captured_contexts[1]


@pytest.mark.asyncio
async def test_story_runtime_exhausted_adk_schema_validation_stays_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify exhausted ADK output-schema failures are classified as schema failures."""

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        raise story_runtime.AgentInvocationError(
            ADK_SCHEMA_ERROR_MESSAGE,
            validation_errors=[
                {
                    "loc": ("is_complete",),
                    "msg": "Field required",
                    "type": "missing",
                }
            ],
        )

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _base_state(),
        project_id=1,
        parent_requirement="Requirement A",
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["classification"] == "nonreusable_schema_failure"
    assert result["is_reusable"] is False


@pytest.mark.asyncio
async def test_story_runtime_replay_uses_frozen_request_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify story runtime replay uses frozen request payload."""
    captured: dict[str, Any] = {}

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        captured["payload"] = payload.model_dump()
        return _valid_story_output(payload.parent_requirement)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    request_payload: story_runtime.StoryInputContext = {
        "parent_requirement": "Requirement A",
        "requirement_context": "Frozen request payload",
        "technical_spec": "SPEC",
        "compiled_authority": '{"ok": true}',
        "global_roadmap_context": "",
        "already_generated_milestone_stories": "",
        "artifact_registry": {},
    }

    result = await story_runtime.run_story_agent_request(
        request_payload,
        project_id=1,
        parent_requirement="Requirement A",
    )

    assert captured["payload"] == request_payload
    assert result["classification"] == "reusable_content_result"
    assert result["draft_kind"] == "complete_draft"
    assert result["request_payload"] == request_payload
