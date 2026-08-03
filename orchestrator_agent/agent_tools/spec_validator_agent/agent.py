from pathlib import Path

from google.adk.agents import LlmAgent

from services.contracts.specification_validation import SpecValidationResult
from utils.helper import load_instruction

from .tools import model

# --- Agent Definition ---
INSTRUCTIONS_PATH: Path = Path(__file__).parent / "instructions.txt"

spec_validator_agent = LlmAgent(
    name="SpecValidatorAgent",
    model=model,
    instruction=load_instruction(INSTRUCTIONS_PATH),
    description="Validates story compliance with technical specifications using Pydantic-enforced logic checks.",
    output_key="spec_validation_result",
    output_schema=SpecValidationResult,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

root_agent = spec_validator_agent
