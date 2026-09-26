"""Provider-free capture of ADK, LiteLLM, and SDK OpenRouter requests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adapters.adk.agents.specification_author import (
    reject_incomplete_specification_output,
)
from adapters.adk.errors import SpecificationAgenticExecutionError
from utils import model_config
from utils.model_config import get_model_token_limit_args, get_openrouter_extra_body

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext

TOKEN_LIMIT = 4096


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id",
    ["openrouter/openai/gpt-5.6-luna", "openrouter/openai/gpt-6-sol"],
)
async def test_adk_litellm_sends_reasoning_and_budget_to_openrouter(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch loss of model, effort, privacy, or limit before SDK HTTP send."""
    monkeypatch.delenv("MODEL_CONFIG_PATH", raising=False)
    monkeypatch.setenv("RELAX_ZDR_FOR_TESTS", "false")
    model_config.clear_config_cache()
    requests: list[dict[str, object]] = []

    async def capture_send(
        _client: httpx.AsyncClient, request: httpx.Request, **_kwargs: object
    ) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": model_id.removeprefix("openrouter/"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", capture_send)
    model = LiteLlm(
        model=model_id,
        api_key="dummy-key",
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
        **get_model_token_limit_args(model_id, TOKEN_LIMIT),
    )
    request = LlmRequest(
        model=model.model,
        contents=[types.Content(role="user", parts=[types.Part(text="input")])],
    )
    try:
        responses = [item async for item in model.generate_content_async(request)]
    finally:
        model_config.clear_config_cache()

    assert len(responses) == 1
    assert len(requests) == 1
    body = requests[0]
    assert body["model"] == model_id.removeprefix("openrouter/")
    assert body["reasoning"] == {"effort": "max"}
    assert body["max_completion_tokens"] == TOKEN_LIMIT
    assert "max_tokens" not in body
    assert body["provider"] == {
        "data_collection": "deny",
        "zdr": True,
        "sort": "price",
        "allow_fallbacks": True,
        "require_parameters": True,
    }


@pytest.mark.parametrize("finish_reason", [types.FinishReason.MAX_TOKENS, None])
def test_reasoning_exhaustion_or_no_visible_content_is_actionable(
    finish_reason: types.FinishReason | None,
) -> None:
    """An empty structured result must fail with a concrete budget remedy."""
    with pytest.raises(SpecificationAgenticExecutionError) as error:
        reject_incomplete_specification_output(
            cast("CallbackContext", object()), LlmResponse(finish_reason=finish_reason)
        )
    assert error.value.code == "SPECIFICATION_OUTPUT_INCOMPLETE"
    assert "SPECIFICATION_STRUCTURER_MAX_TOKENS" in error.value.message
