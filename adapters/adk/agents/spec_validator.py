"""ADK leaf agent for spec-backed story validation."""

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from services.contracts.specification_validation import SpecValidationResult
from utils.helper import load_instruction
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import get_openrouter_api_key, get_spec_validator_max_tokens

_INSTRUCTIONS_PATH = Path(__file__).parents[1] / "prompts" / "spec_validator.txt"


def _spec_validator_model() -> LiteLlm:
    """Build the configured model wrapper for story validation."""
    return LiteLlm(
        model=get_model_id("spec_validator"),
        api_key=get_openrouter_api_key(),
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
        max_tokens=get_spec_validator_max_tokens(),
    )


def build_spec_validator_agent() -> LlmAgent:
    """Build the retained spec-validator leaf agent."""
    return LlmAgent(
        name="SpecValidatorAgent",
        model=_spec_validator_model(),
        instruction=load_instruction(_INSTRUCTIONS_PATH),
        description=(
            "Validates story compliance with technical specifications using "
            "structured logic checks."
        ),
        output_key="spec_validation_result",
        output_schema=SpecValidationResult,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


root_agent = build_spec_validator_agent()
