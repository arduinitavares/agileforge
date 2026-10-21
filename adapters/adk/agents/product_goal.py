"""Product Goal interview ADK agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import Agent

from adapters.adk.product_goal_output import validate_product_goal_output
from adapters.adk.prompts import load_prompt
from adapters.adk.provider_models import LiteLlm, create_openrouter_model
from services.contracts.product_goal import (
    ProductGoalInterviewInput,
    ProductGoalInterviewOutput,
)
from utils.model_config import (
    get_model_id,
    get_model_token_limit_args,
    get_openrouter_extra_body,
)
from utils.runtime_config import (
    get_openrouter_api_key,
    get_vision_interviewer_max_tokens,
)

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse

    from workflow.contracts import JsonObject

_model_id = get_model_id("product_goal")
_token_limit_args = get_model_token_limit_args(
    _model_id, get_vision_interviewer_max_tokens()
)


def get_generation_settings() -> JsonObject:
    """Expose the current constructed model's nonsecret output configuration."""
    return {"generation_config": dict(_token_limit_args)}


def validate_product_goal_response(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> None:
    """Classify the raw final response before ADK parses its output schema."""
    del callback_context
    content = llm_response.content
    texts = (
        []
        if content is None or not content.parts
        else [p.text for p in content.parts if p.text is not None and not p.thought]
    )
    usage = llm_response.usage_metadata
    reason = llm_response.finish_reason
    validate_product_goal_output(
        "".join(texts) if texts else None,
        finish_reason=None if reason is None else reason.value,
        usage={
            "prompt_token_count": getattr(usage, "prompt_token_count", None),
            "candidates_token_count": getattr(usage, "candidates_token_count", None),
            "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
            "total_token_count": getattr(usage, "total_token_count", None),
        },
        generation_settings=get_generation_settings(),
    )


model: LiteLlm = create_openrouter_model(
    model_id=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
    **_token_limit_args,
)

root_agent: Agent = Agent(
    name="product_goal_interview",
    description="Record one Product Goal interview turn.",
    model=model,
    input_schema=ProductGoalInterviewInput,
    output_schema=ProductGoalInterviewOutput,
    instruction=load_prompt("product_goal.txt"),
    after_model_callback=validate_product_goal_response,
    output_key="product_goal_assessment",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
