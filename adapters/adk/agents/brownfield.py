"""Dedicated pre-authority Brownfield specification curator."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from services.contracts.brownfield import (
    BrownfieldCurationInput,
    BrownfieldCurationOutput,
)
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import get_openrouter_api_key

_INSTRUCTIONS = """\
Create a draft AgileForge technical specification from only the supplied trusted
repository inventory and selected evidence. Preserve traceable repository facts,
identify uncertainty explicitly, and do not claim behavior absent from evidence.
Return the strict BrownfieldCurationOutput JSON contract. This is pre-authority
curation, not an as-built compliance assessment.
"""


def build_brownfield_curator_agent() -> Agent:
    """Build the dedicated Brownfield curation leaf."""
    model = LiteLlm(
        model=get_model_id("brownfield_curator"),
        api_key=get_openrouter_api_key(),
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
    )
    return Agent(
        name="brownfield_spec_curator",
        description="Curates an initial draft specification from repository evidence.",
        model=model,
        input_schema=BrownfieldCurationInput,
        output_schema=BrownfieldCurationOutput,
        output_key="brownfield_curation",
        instruction=_INSTRUCTIONS,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


__all__ = ["build_brownfield_curator_agent"]
