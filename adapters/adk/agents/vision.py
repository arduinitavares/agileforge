"""
product_vision_agent.py.

This script defines and runs a Google ADK agent that generates a
product vision statement. If information is missing, it returns a
draft and clarifying questions.
"""

from pathlib import Path

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from services.contracts.vision import InputSchema, OutputSchema
from utils.helper import load_instruction
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import (
    get_openrouter_api_key,
    get_vision_interviewer_max_tokens,
)

# --- Load Instruction ---
INSTRUCTIONS_PATH: Path = Path(__file__).parents[1] / "prompts" / "vision.txt"
instructions = load_instruction(INSTRUCTIONS_PATH)

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
    name="product_vision_tool",
    description=(
        "An agent that creates a product vision from unstructured "
        "requirements. Asks questions if info is missing."
    ),
    model=model,
    input_schema=InputSchema,
    output_schema=OutputSchema,
    instruction=instructions,
    output_key="product_vision_assessment",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
