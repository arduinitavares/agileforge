# adapters/adk/agents/specification_author.py
"""Single-turn to-spec agent for canonical Specification v2 authoring."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from pydantic import BaseModel, ConfigDict

from adapters.adk.prompts.specification_author import (
    SPECIFICATION_AUTHOR_INSTRUCTIONS,
)
from services.contracts.specification_authoring import (
    SpecificationAuthoringInput,
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

_model_id: str = get_model_id("specification_author")


class SpecificationAuthoringModelOutput(BaseModel):
    """Permissive structured object classified by the owning recipe wrapper."""

    model_config = ConfigDict(extra="allow")


model: LiteLlm = LiteLlm(
    model=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
    **get_model_token_limit_args(_model_id, get_vision_interviewer_max_tokens()),
)

root_agent: Agent = Agent(
    name="specification_author",
    description="Create one canonical typed Specification from host-owned sources.",
    model=model,
    input_schema=SpecificationAuthoringInput,
    output_schema=SpecificationAuthoringModelOutput,
    instruction=SPECIFICATION_AUTHOR_INSTRUCTIONS,
    mode="single_turn",
    output_key="specification_candidate",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

__all__ = ["SpecificationAuthoringModelOutput", "root_agent"]
