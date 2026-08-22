# adapters/adk/agents/backlog.py

"""
backlog_primer_agent.py.

Defines a Google ADK agent that builds an initial high-level product backlog.
"""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts import load_prompt
from services.contracts.backlog import BacklogAgentOutput, BacklogBuilderInput
from utils.model_config import (
    get_model_id,
    get_model_token_limit_args,
    get_openrouter_extra_body,
)
from utils.runtime_config import get_backlog_primer_max_tokens, get_openrouter_api_key

BACKLOG_INSTRUCTIONS = load_prompt("backlog.txt")

_max_tokens = get_backlog_primer_max_tokens()
_model_id = get_model_id("backlog_primer")
model: LiteLlm = LiteLlm(
    model=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
    **get_model_token_limit_args(_model_id, _max_tokens),
)

root_agent: Agent = Agent(
    name="backlog_primer_tool",
    description=(
        "An agent that produces an initial high-level product backlog "
        "from the exact accepted Specification, Vision, and Product Goal."
    ),
    model=model,
    input_schema=BacklogBuilderInput,
    output_schema=BacklogAgentOutput,
    output_key="product_backlog",
    instruction=BACKLOG_INSTRUCTIONS,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
