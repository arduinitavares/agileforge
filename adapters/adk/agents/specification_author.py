# adapters/adk/agents/specification_author.py
"""Single-turn agent for canonical Specification v2 structuring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.genai import types
from pydantic_core import from_json

from adapters.adk.errors import (
    SpecificationAgenticExecutionError,
    SpecificationOutputValidationError,
)
from adapters.adk.prompts.specification_author import (
    SPECIFICATION_STRUCTURER_INSTRUCTIONS,
)
from adapters.adk.specification_output import (
    build_specification_output_diagnostic,
    validate_specification_response,
)
from services.contracts.specification_authoring import (
    SpecificationStructuringInput,
    SpecificationStructuringOutput,
)
from utils.model_config import (
    get_model_id,
    get_openrouter_extra_body,
)
from utils.runtime_config import (
    get_openrouter_api_key,
    get_specification_structurer_generation_config,
)

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse

    from services.contracts.specification_authoring import JsonObject

_INCOMPLETE_OUTPUT_MESSAGE: str = (
    "Specification structurer returned incomplete output. Increase "
    "SPECIFICATION_STRUCTURER_MAX_TOKENS or select a provider that can return "
    "the complete structured payload, then retry Structure Specification."
)
_INCOMPLETE_OUTPUT_CODE: str = "SPECIFICATION_OUTPUT_INCOMPLETE"

_model_id: str = get_model_id("specification_structurer")


model: LiteLlm = LiteLlm(
    model=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
)


def _response_text(llm_response: LlmResponse) -> str | None:
    """Join non-thought model text without changing provider bytes."""
    if llm_response.content is None or not llm_response.content.parts:
        return None
    text = "".join(
        part.text
        for part in llm_response.content.parts
        if part.text is not None and not part.thought
    )
    return text or None


def _contains_incomplete_json(text: str | None) -> bool:
    """Recognize syntactic truncation without replacing closed-schema validation."""
    if text is None or not text.strip():
        return True
    try:
        from_json(text)
    except ValueError as error:
        return str(error).startswith("EOF while parsing")
    return False


def reject_incomplete_specification_output(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> None:
    """Fail before ADK schema parsing can erase provider truncation metadata."""
    del callback_context
    finish_reason = llm_response.finish_reason
    if finish_reason == types.FinishReason.MAX_TOKENS or (
        finish_reason in (None, types.FinishReason.STOP)
        and _contains_incomplete_json(_response_text(llm_response))
    ):
        raise SpecificationAgenticExecutionError(
            code=_INCOMPLETE_OUTPUT_CODE,
            message=_INCOMPLETE_OUTPUT_MESSAGE,
        )


def validate_specification_output(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> None:
    """Validate structurer output before ADK output schema processing."""
    text = _response_text(llm_response)
    reason = llm_response.finish_reason
    finish_reason = None if reason is None else reason.value
    usage: JsonObject = {
        "prompt_token_count": getattr(
            llm_response.usage_metadata, "prompt_token_count", None
        ),
        "candidates_token_count": getattr(
            llm_response.usage_metadata, "candidates_token_count", None
        ),
    }
    try:
        reject_incomplete_specification_output(callback_context, llm_response)
    except SpecificationAgenticExecutionError as error:
        raise SpecificationOutputValidationError(
            code=error.code,
            message=error.message,
            diagnostic=build_specification_output_diagnostic(
                text,
                finish_reason=finish_reason,
                usage=usage,
                code=error.code,
            ),
        ) from None
    validate_specification_response(
        text,
        finish_reason=finish_reason,
        usage=usage,
    )


root_agent: Agent = Agent(
    name="specification_structurer",
    description="Structure host-owned sources into one canonical Specification.",
    model=model,
    input_schema=SpecificationStructuringInput,
    output_schema=SpecificationStructuringOutput,
    instruction=SPECIFICATION_STRUCTURER_INSTRUCTIONS,
    generate_content_config=types.GenerateContentConfig.model_validate(
        get_specification_structurer_generation_config()
    ),
    after_model_callback=validate_specification_output,
    mode="single_turn",
    output_key="specification_candidate",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

__all__ = [
    "reject_incomplete_specification_output",
    "root_agent",
    "validate_specification_output",
]
