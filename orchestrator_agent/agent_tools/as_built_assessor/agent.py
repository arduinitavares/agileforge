"""As-Built Assessment Agent."""

from pathlib import Path

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from utils.helper import load_instruction
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import (
    get_as_built_assessor_max_tokens,
    get_openrouter_api_key,
)

from .schemes import AsBuiltAssessment, AsBuiltAssessorInput

INSTRUCTIONS_PATH: Path = Path(__file__).parent / "instructions.txt"
AS_BUILT_INSTRUCTIONS = load_instruction(INSTRUCTIONS_PATH)

_max_tokens = get_as_built_assessor_max_tokens()
model: LiteLlm = LiteLlm(
    model=get_model_id("as_built_assessor"),
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
    max_tokens=_max_tokens,
)

root_agent: Agent = Agent(
    name="as_built_assessor_tool",
    description=(
        "Assesses current repository behavior against accepted AgileForge authority "
        "using a bounded evidence pack."
    ),
    model=model,
    input_schema=AsBuiltAssessorInput,
    output_schema=AsBuiltAssessment,
    output_key="as_built_assessment",
    instruction=AS_BUILT_INSTRUCTIONS,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
