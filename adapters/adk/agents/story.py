"""User Story Writer Agent."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts import load_prompt
from services.contracts.story import (
    UserStoryWriterInput,
    UserStoryWriterOutput,
)
from utils.model_config import (
    get_model_id,
    get_model_token_limit_args,
    get_openrouter_extra_body,
)
from utils.runtime_config import get_openrouter_api_key, get_story_writer_max_tokens

USER_STORY_WRITER_INSTRUCTIONS = load_prompt("story.txt")
USER_STORY_PATCH_INSTRUCTIONS = load_prompt("story_patch.txt")


def _create_story_writer_model() -> LiteLlm:
    """Create the configured Story Writer model."""
    _max_tokens = get_story_writer_max_tokens()
    model_id = get_model_id("user_story_writer")
    return LiteLlm(
        model=model_id,
        api_key=get_openrouter_api_key(),
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
        **get_model_token_limit_args(model_id, _max_tokens),
    )


def create_user_story_writer_agent() -> Agent:
    """Create a fresh User Story Writer agent instance."""
    model: LiteLlm = _create_story_writer_model()
    return Agent(
        name="user_story_writer_tool",
        description=(
            "Decomposes one exact Backlog item into evidence-bound Scrum user "
            "stories under the accepted Specification."
        ),
        model=model,
        input_schema=UserStoryWriterInput,
        output_schema=UserStoryWriterOutput,
        output_key="story_output",
        instruction=USER_STORY_WRITER_INSTRUCTIONS,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def create_user_story_patch_agent() -> Agent:
    """Create a fresh targeted User Story patch agent instance."""
    model: LiteLlm = _create_story_writer_model()
    return Agent(
        name="user_story_patch_tool",
        description=(
            "Corrects exactly one host-selected Scrum Story under its immutable "
            "Backlog and accepted-Specification boundaries."
        ),
        model=model,
        input_schema=UserStoryWriterInput,
        output_schema=UserStoryWriterOutput,
        output_key="story_output",
        instruction=USER_STORY_PATCH_INSTRUCTIONS,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


# Module-level singleton (used by AgentTool wrapping)
root_agent: Agent = create_user_story_writer_agent()
