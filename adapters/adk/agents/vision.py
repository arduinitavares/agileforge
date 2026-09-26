"""
product_vision_agent.py.

This script defines and runs a Google ADK agent that generates a
product vision interview turn. If information is missing, it returns a
draft and one clarifying question or tightly related question set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from adapters.adk.prompts import load_prompt
from adapters.adk.vision_output import validate_vision_response
from services.contracts.vision import (
    VisionDraftOutput,
    VisionModelInput,
    VisionRepairInput,
)
from utils.model_config import (
    get_model_id,
    get_model_token_limit_args,
    get_openrouter_extra_body,
)
from utils.runtime_config import (
    get_openrouter_api_key,
    get_vision_generation_config,
)
from utils.runtime_controls import VISION_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse


def _response_text(response: LlmResponse) -> str | None:
    if response.content is None or not response.content.parts:
        return None
    parts = [
        part.text
        for part in response.content.parts
        if part.text is not None and not part.thought
    ]
    return "".join(parts) if parts else None


def _validate_response(
    callback_context: CallbackContext,
    response: LlmResponse,
    *,
    stage: str,
) -> None:
    del callback_context
    usage = response.usage_metadata
    reason = response.finish_reason
    validate_vision_response(
        _response_text(response),
        finish_reason=None if reason is None else reason.value,
        usage={
            "prompt_token_count": getattr(usage, "prompt_token_count", None),
            "candidates_token_count": getattr(usage, "candidates_token_count", None),
            "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
            "total_token_count": getattr(usage, "total_token_count", None),
        },
        stage=stage,
    )


def validate_primary_response(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> None:
    """Validate a primary provider response before ADK schema parsing."""
    _validate_response(callback_context, llm_response, stage="primary")


def validate_repair_response(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> None:
    """Validate one semantic repair response before ADK schema parsing."""
    _validate_response(callback_context, llm_response, stage="repair")


instructions = load_prompt("vision.txt")
repair_instructions = load_prompt("vision_repair.txt")

# --- Initialize Model with drop_params to prevent logging issues ---
_generation_config = get_vision_generation_config()
_max_tokens = _generation_config["max_output_tokens"]
_model_id = get_model_id("product_vision")
model: LiteLlm = LiteLlm(
    model=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,  # Prevent passing unsupported params that trigger logging
    extra_body=get_openrouter_extra_body(),
    timeout=VISION_TIMEOUT_SECONDS,
    **get_model_token_limit_args(_model_id, _max_tokens),
)


# --- Create Agent ---
root_agent: Agent = Agent(
    name="product_vision_interview",
    description=(
        "An agent that drafts Project Vision from host-provided evidence and "
        "human clarification."
    ),
    model=model,
    input_schema=VisionModelInput,
    output_schema=VisionDraftOutput,
    instruction=instructions,
    generate_content_config=types.GenerateContentConfig.model_validate(
        _generation_config
    ),
    after_model_callback=validate_primary_response,
    mode="single_turn",
    output_key="product_vision_assessment",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

repair_agent: Agent = Agent(
    name="product_vision_repair",
    description="An agent that repairs one semantically invalid Project Vision draft.",
    model=model,
    input_schema=VisionRepairInput,
    output_schema=VisionDraftOutput,
    instruction=repair_instructions,
    generate_content_config=types.GenerateContentConfig.model_validate(
        _generation_config
    ),
    after_model_callback=validate_repair_response,
    mode="single_turn",
    output_key="product_vision_repair",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
