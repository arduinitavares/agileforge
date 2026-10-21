"""Product Goal generation regressions using real ADK and isolated domain state."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from litellm import ModelResponse
from sqlmodel import Session, select

from adapters.adk.agents.product_goal import root_agent
from adapters.adk.runner import AdkRunGuards
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    VisionArtifact,
    VisionArtifactDecision,
)
from tests.adapters.test_adk_workflow_runner import (
    TrackingSessionService,
    _goal_decision,
    _goal_runner_input,
    _goal_runner_system,
)
from tests.adapters.test_vision_recipe import RawVisionLlm
from utils.runtime_config import ADK_EXECUTION_TRACE_IDENTITY
from workflow.contracts import NodeCategory

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

PROMPT_TOKENS = 1_411
EXHAUSTED_COMPLETION_TOKENS = 4_096


def _complete_goal() -> dict[str, object]:
    return {
        "updated_components": {
            "valuable_future_state": "A predictable delivery flow",
            "beneficiary": "Operators",
            "value": "Complete evidence " * 5_000,
            "success_signals": ["Every decision is auditable"],
            "boundaries": ["No provider call in tests"],
        },
        "product_goal_statement": "Operators trust lifecycle evidence.",
        "is_complete": True,
        "clarifying_questions": [],
    }


@pytest.mark.parametrize(
    ("text", "finish_reason"),
    [
        (None, types.FinishReason.MAX_TOKENS),
        ("", types.FinishReason.STOP),
        (json.dumps(_complete_goal()), types.FinishReason.MAX_TOKENS),
    ],
    ids=["empty-max-tokens", "empty-stop", "complete-json-max-tokens"],
)
def test_incomplete_goal_has_durable_diagnostics_without_business_changes(
    engine: Engine, text: str | None, finish_reason: types.FinishReason
) -> None:
    """Empty or length-limited output must not become a generic schema failure."""
    raw = RawVisionLlm(
        model="raw-product-goal",
        response=LlmResponse(
            content=types.Content(
                role="model", parts=[] if text is None else [types.Part(text=text)]
            ),
            finish_reason=finish_reason,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=PROMPT_TOKENS,
                candidates_token_count=EXHAUSTED_COMPLETION_TOKENS,
                thoughts_token_count=EXHAUSTED_COMPLETION_TOKENS,
                total_token_count=5_507,
            ),
        ),
    )
    agent = root_agent.model_copy(update={"model": raw})
    runner, domain, project_id = _goal_runner_system(engine, agent)
    with Session(engine) as session:
        visions = [item.model_dump() for item in session.exec(select(VisionArtifact))]
        decisions = [
            item.model_dump() for item in session.exec(select(VisionArtifactDecision))
        ]
        goals_before = len(session.exec(select(ProductGoalArtifact)).all())
        turns_before = len(session.exec(select(ProductGoalInterviewTurn)).all())
    position = domain.position(project_id)
    decision = _goal_decision(domain, project_id)
    guards = AdkRunGuards(
        position=position, idempotency_key="incomplete-goal", actor="operator"
    )

    result = runner.run(decision, _goal_runner_input(), guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "PRODUCT_GOAL_OUTPUT_INCOMPLETE"
    assert "dict_type" not in result.error.message
    with Session(engine) as session:
        assert len(session.exec(select(ProductGoalArtifact)).all()) == goals_before
        assert len(session.exec(select(ProductGoalInterviewTurn)).all()) == turns_before
        assert [
            item.model_dump() for item in session.exec(select(VisionArtifact))
        ] == visions
        assert [
            item.model_dump() for item in session.exec(select(VisionArtifactDecision))
        ] == decisions
    trace = runner._session_service
    assert isinstance(trace, TrackingSessionService)
    recorded = asyncio.run(
        trace.get_session(
            app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
            user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
            session_id=trace.created_session_ids[0],
        )
    )
    assert recorded is not None
    diagnostic = recorded.state["product_goal_output_diagnostic"]
    assert diagnostic["finish_reason"] == finish_reason.value
    assert diagnostic["prompt_token_count"] == PROMPT_TOKENS
    assert diagnostic["candidates_token_count"] == EXHAUSTED_COMPLETION_TOKENS
    assert diagnostic["thoughts_token_count"] == EXHAUSTED_COMPLETION_TOKENS
    replay = runner.run(decision, _goal_runner_input(), guards=guards)
    assert replay == result.model_copy(update={"replayed": True})
    assert raw.calls == 1


def test_reasoning_heavy_complete_goal_reaches_the_existing_review_gate(
    engine: Engine,
) -> None:
    """A complete large result retains schema, lineage and human acceptance."""
    raw = RawVisionLlm(
        model="raw-product-goal",
        response=LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text=json.dumps(_complete_goal()))]
            ),
            finish_reason=types.FinishReason.STOP,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1_411,
                candidates_token_count=20_000,
                thoughts_token_count=8_000,
                total_token_count=21_411,
            ),
        ),
    )
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": raw})
    )

    result = runner.run(_goal_decision(domain, project_id), _goal_runner_input())

    assert result.ok is True
    assert raw.calls == 1
    assert any(
        item.node_id == "goal.review" and item.category is NodeCategory.WAITING
        for item in domain.position(project_id).decisions
    )


class _CaptureClient(LiteLLMClient):
    """Replace only the external completion to inspect the composed request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def acompletion(
        self, model: object, messages: object, tools: object, **kwargs: object
    ) -> ModelResponse:
        self.requests.append(
            {"model": model, "messages": messages, "tools": tools, **kwargs}
        )
        return ModelResponse(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ]
        )


@pytest.mark.asyncio
async def test_product_goal_does_not_supply_a_fixed_application_output_cap() -> None:
    """The interviewer must delegate output allowance to the shared provider policy."""
    assert isinstance(root_agent.model, LiteLlm)
    client = _CaptureClient()
    model = root_agent.model.model_copy(update={"llm_client": client})
    request = LlmRequest(
        model=model.model,
        contents=[types.Content(role="user", parts=[types.Part(text="input")])],
        config=root_agent.generate_content_config or types.GenerateContentConfig(),
    )

    responses = [item async for item in model.generate_content_async(request)]

    assert len(responses) == 1
    assert len(client.requests) == 1
    assert client.requests[0].get("max_tokens") is None
    assert client.requests[0].get("max_completion_tokens") is None
