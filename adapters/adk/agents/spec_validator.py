"""ADK leaf agent for spec-backed story validation."""

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts import load_prompt
from services.contracts.specification_validation import StorySpecificationReviewOutput
from utils.model_config import (
    get_model_id,
    get_model_token_limit_args,
    get_openrouter_extra_body,
)
from utils.runtime_config import get_openrouter_api_key, get_spec_validator_max_tokens


def _spec_validator_model() -> LiteLlm:
    """Build the configured model wrapper for story validation."""
    model_id = get_model_id("spec_validator")
    return LiteLlm(
        model=model_id,
        api_key=get_openrouter_api_key(),
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
        **get_model_token_limit_args(model_id, get_spec_validator_max_tokens()),
    )


def build_spec_validator_agent() -> LlmAgent:
    """Build the retained spec-validator leaf agent."""
    return LlmAgent(
        name="SpecValidatorAgent",
        model=_spec_validator_model(),
        instruction=load_prompt("spec_validator.txt"),
        description="Reviews one accepted Story against its exact Specification.",
        output_key="spec_validation_result",
        output_schema=StorySpecificationReviewOutput,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


root_agent = build_spec_validator_agent()
