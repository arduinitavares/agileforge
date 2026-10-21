"""Roadmap Builder Agent."""

from google.adk.agents import Agent

from adapters.adk.generation_output import validate_generation_response
from adapters.adk.prompts import load_prompt
from adapters.adk.provider_models import LiteLlm, create_openrouter_model
from services.contracts.roadmap import RoadmapBuilderInput, RoadmapBuilderOutput
from utils.model_config import (
    get_model_id,
    get_openrouter_extra_body,
)
from utils.runtime_config import get_openrouter_api_key

ROADMAP_INSTRUCTIONS = load_prompt("roadmap.txt")

# Initialize Model
_model_id = get_model_id("roadmap_builder")
model: LiteLlm = create_openrouter_model(
    model_id=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
)

# Initialize Agent
root_agent: Agent = Agent(
    name="roadmap_builder_tool",
    description=(
        "Constructs a roadmap from exact accepted Backlog items and the accepted "
        "Specification root."
    ),
    model=model,
    after_model_callback=validate_generation_response,
    input_schema=RoadmapBuilderInput,
    output_schema=RoadmapBuilderOutput,
    output_key="roadmap_result",
    instruction=ROADMAP_INSTRUCTIONS,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
