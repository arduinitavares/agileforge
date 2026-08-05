"""
product_vision_agent.py.

This script defines and runs a Google ADK agent that generates a
product vision interview turn. If information is missing, it returns a
draft and one clarifying question or tightly related question set.
"""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts import load_prompt
from services.contracts.vision import (
    InputSchema,
    OutputSchema,
    VisionInterviewInput,
    VisionInterviewOutput,
)
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import (
    get_openrouter_api_key,
    get_vision_interviewer_max_tokens,
)

instructions = load_prompt("vision.txt")
legacy_instructions = load_prompt("vision_legacy.txt")

# --- Initialize Model with drop_params to prevent logging issues ---
_max_tokens = get_vision_interviewer_max_tokens()
model: LiteLlm = LiteLlm(
    model=get_model_id("product_vision"),
    api_key=get_openrouter_api_key(),
    drop_params=True,  # Prevent passing unsupported params that trigger logging
    extra_body=get_openrouter_extra_body(),
    max_tokens=_max_tokens,
)


# --- Create Agent ---
root_agent: Agent = Agent(
    name="product_vision_interview",
    description=(
        "An agent that records human product-intent answers into one "
        "Project Vision interview turn."
    ),
    model=model,
    input_schema=VisionInterviewInput,
    output_schema=VisionInterviewOutput,
    instruction=instructions,
    output_key="product_vision_assessment",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)


legacy_root_agent: Agent = Agent(
    name="product_vision_tool",
    description="An agent that creates a legacy root-graph product vision.",
    model=model,
    input_schema=InputSchema,
    output_schema=OutputSchema,
    instruction=legacy_instructions,
    output_key="product_vision_assessment",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
