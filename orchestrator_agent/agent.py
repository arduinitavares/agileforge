# agent.py
"""Orchestrator agent definition."""

from pathlib import Path

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.agent_tool import AgentTool

from orchestrator_agent.agent_tools.backlog_primer.agent import (
    root_agent as backlog_agent,
)
from orchestrator_agent.agent_tools.backlog_primer.tools import (
    save_backlog_tool,
)
from orchestrator_agent.agent_tools.product_vision_tool.agent import (
    root_agent as vision_agent,
)
from orchestrator_agent.agent_tools.product_vision_tool.tools import (
    save_vision_tool,
)
from orchestrator_agent.agent_tools.roadmap_builder.agent import (
    root_agent as roadmap_agent,
)
from orchestrator_agent.agent_tools.roadmap_builder.tools import (
    save_roadmap_tool,
)
from orchestrator_agent.agent_tools.sprint_planner_tool.agent import (
    root_agent as sprint_planner_agent,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.agent import (
    root_agent as story_writer_agent,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.tools import (
    save_stories_tool,
)
from orchestrator_agent.agent_tools.utils.resilience import SelfHealingAgent
from tools.db_tools import (
    get_story_details,
)
from tools.orchestrator_tools import (
    count_projects,
    get_project_by_name,
    get_project_details,
    list_projects,
    load_specification_from_file,
    select_project,
)
from tools.spec_tools import (
    compile_spec_authority_for_version,
    read_project_specification,
    update_spec_and_compile_authority,
)

# Story query tools (extracted from legacy product_user_story_tool)
from tools.story_query_tools import query_features_for_stories
from utils.helper import load_instruction
from utils.model_config import (
    get_model_id,
    get_openrouter_extra_body,
)
from utils.runtime_config import get_openrouter_api_key

# --- Load environment and instruction ---
INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.txt")
instruction_text = load_instruction(INSTRUCTIONS_PATH)


# --- Initialize model ---
model = LiteLlm(
    model=get_model_id("orchestrator"),
    api_key=get_openrouter_api_key(),
    drop_params=True,  # Prevent passing unsupported params that trigger logging
    extra_body=get_openrouter_extra_body(),
)

# --- Define the agent ---
# Wrap the root agent with SelfHealingAgent for robust retry handling (ZDR, 429, 5xx)
# We use higher retry limits for the orchestrator since it's the main entry point
orchestrator_agent = Agent(
    name="orchestrator_agent",  # must match folder name, no spaces
    description="Routing agent for the Agile platform.",
    model=model,
    tools=[
        # Project management tools
        count_projects,
        list_projects,
        get_project_details,
        get_project_by_name,
        select_project,
        load_specification_from_file,
        # Specification tools
        read_project_specification,
        compile_spec_authority_for_version,
        update_spec_and_compile_authority,
        # Story query tools
        get_story_details,
        query_features_for_stories,
        # Vision tools
        save_vision_tool,
        # Backlog tools
        save_backlog_tool,
        # Roadmap tools
        save_roadmap_tool,
        # Story tools
        save_stories_tool,
        # Agent tools
        AgentTool(agent=vision_agent),
        AgentTool(agent=backlog_agent),
        AgentTool(agent=roadmap_agent),
        AgentTool(agent=sprint_planner_agent),
        AgentTool(agent=story_writer_agent),
    ],
    instruction=instruction_text,
    output_key="orchestrator_response",
)

root_agent = SelfHealingAgent(
    agent=orchestrator_agent,
    max_retries=3,  # standard for validation
    max_zdr_retries=10,  # higher for orchestrator
    max_rate_limit_retries=10,  # withstand temporary bursts
    max_provider_error_retries=5,  # tolerate some instability
    name="orchestrator_agent",  # Preserve the original name
)
