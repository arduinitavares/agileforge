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
async def test_composed_vision_request_delegates_budget_and_lifetime(
    configured_agents: tuple[Agent, Agent], index: int
) -> None:
    """Primary and repair preserve effort while delegating budget and lifetime."""
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

    assert "max_completion_tokens" not in client.kwargs
    assert "timeout" not in client.kwargs
    assert "max_tokens" not in client.kwargs
    assert config.max_output_tokens is None
    extra_body = cast("dict[str, object]", client.kwargs["extra_body"])
    assert extra_body["reasoning"] == {"effort": "max"}


def test_vision_config_has_no_application_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vision's default delegates the output allowance to provider policy."""
    monkeypatch.delenv("VISION_INTERVIEWER_MAX_TOKENS", raising=False)
    assert get_vision_generation_config() == {}


def test_retired_vision_override_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retired environment setting cannot restore the old limit."""
    monkeypatch.setenv("VISION_INTERVIEWER_MAX_TOKENS", "8192")
    assert get_vision_generation_config() == {}
