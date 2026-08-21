"""spec_authority_compiler_agent - agent-first compiler for spec authority."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.prompts.specification import (
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
)
from utils.model_config import get_model_id, get_openrouter_extra_body
from utils.runtime_config import (
    get_openrouter_api_key,
    is_spec_compiler_schema_disabled,
)
from utils.spec_schemas import (
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerInput,
    SpecAuthorityValidationRepairInput,
)


def _compiler_model(model_id: str) -> LiteLlm:
    """Build the LiteLLM wrapper for one compiler model id."""
    return LiteLlm(
        model=model_id,
        api_key=get_openrouter_api_key(),
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
    )


def build_spec_authority_compiler_agent(
    *,
    compiler_model: str | None = None,
    validation_repair: bool = False,
) -> Agent:
    """Build a spec authority compiler agent for one invocation."""
    disable_schema = is_spec_compiler_schema_disabled()
    output_schema = None if disable_schema else SpecAuthorityCompilerEnvelope
    input_schema = (
        SpecAuthorityValidationRepairInput
        if validation_repair
        else SpecAuthorityCompilerInput
    )
    return Agent(
        name=(
            "spec_authority_validation_repair_agent"
            if validation_repair
            else "spec_authority_compiler_agent"
        ),
        description=(
            "Compiler-style agent that corrects Authority output from host findings."
            if validation_repair
            else "Compiler-style agent that extracts spec authority in strict JSON."
        ),
        model=_compiler_model(
            compiler_model or get_model_id("spec_authority_compiler")
        ),
        input_schema=input_schema,
        output_schema=output_schema,
        instruction=SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
        output_key="spec_authority_compilation",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


root_agent = build_spec_authority_compiler_agent()
