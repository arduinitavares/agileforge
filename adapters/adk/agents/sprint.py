"""Sprint Planner agent definition."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts import load_prompt
from services.contracts.sprint import SprintPlannerInput, SprintPlannerOutput
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import get_openrouter_api_key

SPRINT_PLANNER_INSTRUCTIONS = load_prompt("sprint.txt")

model: LiteLlm = LiteLlm(
    model=get_model_id("sprint_planner"),
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
)

root_agent: Agent = Agent(
    name="sprint_planner_tool",
    description=(
        "An agent that converts a prioritized product backlog into a committed "
        "sprint backlog with sprint goal, capacity reasoning, and tasks."
    ),
    model=model,
    input_schema=SprintPlannerInput,
    output_schema=SprintPlannerOutput,
    output_key="sprint_plan",
    instruction=SPRINT_PLANNER_INSTRUCTIONS,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
