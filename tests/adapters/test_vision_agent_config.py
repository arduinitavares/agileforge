"""Vision's composed ADK request settings at the LiteLLM boundary."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from litellm import ModelResponse

import adapters.adk.agents.vision as vision_module
from utils import model_config
from utils.runtime_config import (
    get_vision_generation_config,
    get_vision_interviewer_max_tokens,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from google.adk.agents import Agent


TOKEN_BUDGET = 128_000
REQUEST_TIMEOUT = 600
GOAL_DEFAULT = 4096


@pytest.fixture
def configured_agents(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Agent, Agent]]:
    """Compose Vision under the packaged Luna/max profile for one test."""
    profile = Path(__file__).parents[2] / "config" / "models.yaml"
    with monkeypatch.context() as scoped:
        scoped.setenv("MODEL_CONFIG_PATH", str(profile))
        scoped.delenv("VISION_INTERVIEWER_MAX_TOKENS", raising=False)
        model_config.clear_config_cache()
        module = importlib.reload(vision_module)
        yield module.root_agent, module.repair_agent
    model_config.clear_config_cache()
    importlib.reload(vision_module)


class _CaptureClient(LiteLLMClient):
    """Capture a completion request without contacting a provider."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def acompletion(
        self, model: object, messages: object, tools: object, **kwargs: object
    ) -> ModelResponse:
        self.kwargs = {"model": model, "messages": messages, "tools": tools, **kwargs}
        return ModelResponse(
            model="fake/vision",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
async def test_composed_vision_request_uses_full_budget_and_timeout(
    configured_agents: tuple[Agent, Agent], index: int
) -> None:
    """Primary and repair send the same output budget through installed ADK."""
    agent = configured_agents[index]
    client = _CaptureClient()
    assert isinstance(agent.model, LiteLlm)
    model = agent.model.model_copy(update={"llm_client": client})
    config = agent.generate_content_config
    assert config is not None
    request = LlmRequest(
        model=model.model,
        contents=[types.Content(role="user", parts=[types.Part(text="input")])],
        config=config,
    )
    _responses = [item async for item in model.generate_content_async(request)]

    assert client.kwargs["max_completion_tokens"] == TOKEN_BUDGET
    assert client.kwargs["timeout"] == REQUEST_TIMEOUT
    assert "max_tokens" not in client.kwargs
    assert config.max_output_tokens == TOKEN_BUDGET
    extra_body = cast("dict[str, object]", client.kwargs["extra_body"])
    assert extra_body["reasoning"] == {"effort": "max"}


def test_product_goal_retains_its_previous_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vision's new default does not raise Product Goal's shared getter value."""
    monkeypatch.delenv("VISION_INTERVIEWER_MAX_TOKENS", raising=False)
    assert get_vision_generation_config() == {"max_output_tokens": TOKEN_BUDGET}
    assert get_vision_interviewer_max_tokens() == GOAL_DEFAULT


def test_explicit_vision_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured limit changes only the Vision generation accessor."""
    monkeypatch.setenv("VISION_INTERVIEWER_MAX_TOKENS", "8192")
    assert get_vision_generation_config() == {"max_output_tokens": 8192}
