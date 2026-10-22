"""Raw final-output safety across real downstream agents and helper extraction."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import TypeAdapter
from sqlmodel import Session, select

from adapters.adk.agents.backlog import root_agent as backlog_agent
from adapters.adk.agents.roadmap import root_agent as roadmap_agent
from adapters.adk.agents.spec_validator import root_agent as review_agent
from adapters.adk.agents.sprint import root_agent as sprint_agent
from adapters.adk.agents.story import create_user_story_patch_agent
from adapters.adk.agents.story import root_agent as story_agent
from adapters.adk.errors import GenerationOutputValidationError
from adapters.adk.generation_output import validate_generation_response
from adapters.adk.runner import AdkRunGuards
from models.workflow import StoryArtifact, StoryArtifactDecision
from services import roadmap_runtime, story_runtime
from services.specs import story_validation_service
from tests.adapters.test_adk_graph_recipes import _sprint_output
from tests.adapters.test_adk_workflow_runner import (
    _node_outcomes,
    _story_decision,
    _story_provider_output,
    _story_runner_system,
)
from tests.adapters.test_vision_recipe import RawVisionLlm
from tests.test_roadmap_runtime import _state as roadmap_state
from tests.test_story_runtime import _state as story_state
from tests.test_story_validation_service import _accepted_story
from utils import failure_artifacts
from utils.adk_runner import extract_final_response_text, invoke_agent_to_text
from utils.failure_artifacts import AgentInvocationError
from workflow.contracts import WorkflowErrorCode

if TYPE_CHECKING:
    from pathlib import Path

    from google.adk.agents import Agent
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject


COMPLETION_TOKENS = 10
REASONING_TOKENS = 4

AGENTS: dict[str, Agent] = {
    "backlog": backlog_agent,
    "roadmap": roadmap_agent,
    "sprint": sprint_agent,
    "story": story_agent,
    "story_patch": create_user_story_patch_agent(),
    "review": review_agent,
}


def _complete(stage: str) -> JsonObject:
    if stage == "backlog":
        return {"backlog_items": [], "is_complete": True, "clarifying_questions": []}
    if stage == "roadmap":
        return {
            "roadmap_releases": [],
            "roadmap_summary": "Complete independent output evidence.",
            "is_complete": True,
            "clarifying_questions": [],
        }
    if stage == "sprint":
        return _sprint_output()
    if stage == "review":
        return {
            "schema_version": "agileforge.story-specification-review.v1",
            "compliant": True,
            "complete": True,
            "findings": [],
        }
    return _story_provider_output()


async def _invoke(stage: str, text: str, reason: types.FinishReason) -> str:
    raw = RawVisionLlm(
        model="raw-generation",
        response=LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            finish_reason=reason,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=42,
                candidates_token_count=COMPLETION_TOKENS,
                thoughts_token_count=REASONING_TOKENS,
                total_token_count=52,
            ),
        ),
    )
    # The text helper uses Runner's message input; graph input conversion is
    # exercised separately. Keep the production callbacks/schema/model config.
    agent = AGENTS[stage].model_copy(update={"model": raw, "input_schema": None})
    return await invoke_agent_to_text(
        agent=agent,
        runner_identity=SimpleNamespace(app_name="generation_safety", user_id="host"),
        payload_json="{}",
        no_text_error="No complete generation output.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", tuple(AGENTS))
@pytest.mark.parametrize("case", ["valid-json-length", "empty", "eof", "malformed"])
async def test_downstream_helpers_reject_incomplete_or_invalid_final_output(
    stage: str, case: str
) -> None:
    """Length-limited output cannot become generation or review evidence."""
    text = {
        "valid-json-length": json.dumps(_complete(stage)),
        "empty": "",
        "eof": '{"private-output-marker":',
        "malformed": "private-output-marker: invalid JSON",
    }[case]
    reason = (
        types.FinishReason.MAX_TOKENS
        if case == "valid-json-length"
        else types.FinishReason.STOP
    )

    with pytest.raises(AgentInvocationError) as captured:
        await _invoke(stage, text, reason)

    error = captured.value
    diagnostic = getattr(error, "output_diagnostic", None)
    assert isinstance(diagnostic, dict)
    assert diagnostic["code"] == (
        "INVALID_GENERATION_OUTPUT"
        if case == "malformed"
        else "GENERATION_OUTPUT_INCOMPLETE"
    )
    assert diagnostic["finish_reason"] == reason.value
    assert diagnostic["candidates_token_count"] == COMPLETION_TOKENS
    assert diagnostic["thoughts_token_count"] == REASONING_TOKENS
    assert error.partial_output is None
    assert error.validation_errors is None
    assert "private-output-marker" not in str(error)
    assert "private-output-marker" not in json.dumps(diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", tuple(AGENTS))
async def test_complete_downstream_output_preserves_existing_schema(stage: str) -> None:
    """A raw guard must leave complete structured model output intact."""
    payload = _complete(stage)
    schema = AGENTS[stage].output_schema
    if schema is not None:
        TypeAdapter(schema).validate_python(payload)

    result = await _invoke(stage, json.dumps(payload), types.FinishReason.STOP)

    assert json.loads(result) == payload


def test_intermediate_tool_response_is_not_empty_final_output() -> None:
    """Tools can run between model responses without bypassing final validation."""
    response = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="read"))],
        ),
        finish_reason=types.FinishReason.STOP,
    )
    assert validate_generation_response(None, response) is None


def test_helper_extraction_rejects_a_length_limited_final_event() -> None:
    """A helper with an externally supplied agent still validates final metadata."""
    response = LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text='{"complete":true}')]
        ),
        finish_reason=types.FinishReason.MAX_TOKENS,
    )
    with pytest.raises(GenerationOutputValidationError):
        extract_final_response_text([response])


def test_length_limited_story_cannot_persist_candidate_or_lose_replay_code(
    engine: Engine,
) -> None:
    """The real graph owner stores one precise failure and no Story candidate."""
    raw = RawVisionLlm(
        model="raw-generation",
        response=LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text=json.dumps(_complete("story")))]
            ),
            finish_reason=types.FinishReason.MAX_TOKENS,
        ),
    )
    system = _story_runner_system(
        engine, leaf=story_agent.model_copy(update={"model": raw})
    )
    decision = _story_decision(system.domain, system.project_id)
    guards = AdkRunGuards(
        position=system.domain.position(system.project_id),
        idempotency_key="length-limited-story",
        actor="operator@example.com",
        correlation_id="generation-safety",
    )
    first = system.runner.run(decision, system.payload, guards=guards)
    replay = system.runner.run(decision, system.payload, guards=guards)
    assert first.ok is False
    assert first.error is not None
    assert first.error.code is WorkflowErrorCode.GENERATION_OUTPUT_INCOMPLETE
    assert replay == first.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert session.exec(select(StoryArtifact)).all() == []
        assert session.exec(select(StoryArtifactDecision)).all() == []
        outcomes = _node_outcomes(session, "planning.story.generate")
        assert len(outcomes) == 1
        assert outcomes[0].failure_code == "GENERATION_OUTPUT_INCOMPLETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["story", "roadmap"])
async def test_helper_owner_preserves_safe_actionable_output_failure(
    stage: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Owners expose metadata without semantic retries or partial-output artifacts."""
    calls = []

    async def invoke(_payload: object) -> str:
        calls.append(stage)
        return await _invoke(
            stage, json.dumps(_complete(stage)), types.FinishReason.MAX_TOKENS
        )

    monkeypatch.setattr(failure_artifacts, "FAILURES_DIR", tmp_path / "failures")
    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    owner = story_runtime if stage == "story" else roadmap_runtime
    monkeypatch.setattr(owner, f"_invoke_{stage}_agent", invoke)
    state = story_state() if stage == "story" else roadmap_state()
    run = (
        story_runtime.run_story_agent_from_state
        if stage == "story"
        else roadmap_runtime.run_roadmap_agent_from_state
    )
    result = await run(state, project_id=1, user_input=None)
    assert result["success"] is False
    assert "finish_reason=MAX_TOKENS" in result["error"]
    assert calls == [stage]
    assert result["raw_output_preview"] is None
    files = list((tmp_path / "failures").glob("*/*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text())
    assert stored["raw_output"] is None
    assert (
        stored["extra"]["generation_output_diagnostic"]["code"]
        == "GENERATION_OUTPUT_INCOMPLETE"
    )


def test_length_limited_specification_review_is_invalid_and_actionable(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete-looking review JSON must not become successful semantic evidence."""
    story_id = _accepted_story(engine)
    monkeypatch.setattr(story_validation_service, "get_engine", lambda: engine)

    def review(_payload: object) -> str:
        return asyncio.run(
            _invoke(
                "review", json.dumps(_complete("review")), types.FinishReason.MAX_TOKENS
            )
        )

    result = story_validation_service.validate_story_with_specification(
        {"story_id": story_id, "mode": "hybrid"}, semantic_review=review
    )
    assert result["semantic_review_state"] == "invalid"
    assert result["semantic_error"] == "GENERATION_OUTPUT_INCOMPLETE"
    diagnostic = result["generation_output_diagnostic"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["finish_reason"] == "MAX_TOKENS"


def test_helper_cannot_hide_length_exhaustion_behind_a_tool_call() -> None:
    """A tool-shaped response is still incomplete when the provider exhausts output."""
    response = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="read"))],
        ),
        finish_reason=types.FinishReason.MAX_TOKENS,
    )
    with pytest.raises(GenerationOutputValidationError):
        extract_final_response_text([response])
