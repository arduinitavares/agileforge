"""Roadmap Builder Agent."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts import load_prompt
from services.contracts.roadmap import RoadmapBuilderInput, RoadmapBuilderOutput
from utils.model_config import (
    get_model_id,
    get_model_token_limit_args,
    get_openrouter_extra_body,
)
from utils.runtime_config import get_openrouter_api_key, get_roadmap_builder_max_tokens

ROADMAP_INSTRUCTIONS = load_prompt("roadmap.txt")

# Initialize Model
_max_tokens = get_roadmap_builder_max_tokens()
_model_id = get_model_id("roadmap_builder")
model: LiteLlm = LiteLlm(
    model=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
    **get_model_token_limit_args(_model_id, _max_tokens),
)

# Initialize Agent
root_agent: Agent = Agent(
    name="roadmap_builder_tool",
    description=(
        "Constructs a roadmap from exact accepted Backlog items and the accepted "
        "Specification root."
    ),
    model=model,
    input_schema=RoadmapBuilderInput,
    output_schema=RoadmapBuilderOutput,
    output_key="roadmap_result",
    instruction=ROADMAP_INSTRUCTIONS,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
