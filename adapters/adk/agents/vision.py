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
    VisionAgentInput,
    VisionDraftOutput,
    VisionRepairInput,
)
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import (
    get_openrouter_api_key,
    get_vision_interviewer_max_tokens,
)

instructions = load_prompt("vision.txt")
repair_instructions = load_prompt("vision_repair.txt")

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
        "An agent that drafts Project Vision from host-provided evidence and "
        "human clarification."
    ),
    model=model,
    input_schema=VisionAgentInput,
    output_schema=VisionDraftOutput,
    instruction=instructions,
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
    output_key="product_vision_repair",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
