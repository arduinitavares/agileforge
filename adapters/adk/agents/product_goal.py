"""Product Goal interview ADK agent."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts import load_prompt
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

_model_id = get_model_id("product_goal")
model: LiteLlm = LiteLlm(
    model=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
    **get_model_token_limit_args(_model_id, get_vision_interviewer_max_tokens()),
)

root_agent: Agent = Agent(
    name="product_goal_interview",
    description="Record one Product Goal interview turn.",
    model=model,
    input_schema=ProductGoalInterviewInput,
    output_schema=ProductGoalInterviewOutput,
    instruction=load_prompt("product_goal.txt"),
    output_key="product_goal_assessment",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
