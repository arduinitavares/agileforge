"""User Story Writer Agent."""

from pathlib import Path

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from utils.helper import load_instruction
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import get_openrouter_api_key, get_story_writer_max_tokens

from .schemes import UserStoryPatchOutput, UserStoryWriterInput, UserStoryWriterOutput

# Load instruction text
INSTRUCTIONS_PATH: Path = Path(__file__).parent / "instructions.txt"
USER_STORY_WRITER_INSTRUCTIONS = load_instruction(INSTRUCTIONS_PATH)
PATCH_INSTRUCTIONS_PATH: Path = Path(__file__).parent / "patch_instructions.txt"
USER_STORY_PATCH_INSTRUCTIONS = load_instruction(PATCH_INSTRUCTIONS_PATH)


def _create_story_writer_model() -> LiteLlm:
    """Create the configured Story Writer model."""
    _max_tokens = get_story_writer_max_tokens()
    return LiteLlm(
        model=get_model_id("user_story_writer"),
        api_key=get_openrouter_api_key(),
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
        max_tokens=_max_tokens,
    )


def create_user_story_writer_agent() -> Agent:
    """Factory: create a fresh User Story Writer agent instance."""
    model: LiteLlm = _create_story_writer_model()
    return Agent(
        name="user_story_writer_tool",
        description=(
            "Decomposes a single high-level roadmap requirement into "
            "INVEST-compliant, Scrum user stories (Stage 2 → Chapter 5)."
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
    """Factory: create a fresh targeted User Story patch agent instance."""
    model: LiteLlm = _create_story_writer_model()
    return Agent(
        name="user_story_patch_tool",
        description="Refines exactly one existing Scrum user story.",
        model=model,
        input_schema=UserStoryWriterInput,
        output_schema=UserStoryPatchOutput,
        output_key="story_output",
        instruction=USER_STORY_PATCH_INSTRUCTIONS,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


# Module-level singleton (used by AgentTool wrapping)
root_agent: Agent = create_user_story_writer_agent()
