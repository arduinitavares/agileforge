"""Production Specification author agent configuration boundaries."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from workflow.contracts import JsonObject

if TYPE_CHECKING:
    import pytest


def test_agent_prompt_hash_binds_the_actual_packaged_instructions() -> None:
    """Fail collection when the loaded to-spec prompt drifts from provenance."""
    prompt_module = importlib.import_module(
        "adapters.adk.prompts.specification_author"
    )
    contract = importlib.import_module(
        "services.contracts.specification_authoring"
    )
    prompts = importlib.import_module("adapters.adk.prompts")

    instructions = prompts.load_prompt("specification_author.txt")

    assert instructions == prompt_module.SPECIFICATION_AUTHOR_INSTRUCTIONS
    assert prompt_module.SPECIFICATION_AUTHOR_PROMPT_HASH == (
        contract.SPECIFICATION_AUTHOR_PROMPT_HASH
    )
    assert contract.compute_specification_author_prompt_hash(instructions) == (
        contract.SPECIFICATION_AUTHOR_PROMPT_HASH
    )
    assert contract.SPECIFICATION_AUTHOR_PROMPT_HASH == (
        "sha256:ab4ec877a7fa25a38100820269c5aad25a476fb55d29cd51296123bd01dfe678"
    )


def test_agent_requests_structured_json_without_owning_semantic_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave schema-versus-payload classification to the recipe wrapper."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    module = importlib.import_module("adapters.adk.agents.specification_author")
    prompt_module = importlib.import_module(
        "adapters.adk.prompts.specification_author"
    )

    output_schema = module.root_agent.output_schema

    assert output_schema is module.SpecificationAuthoringModelOutput
    assert module.root_agent.instruction == (
        prompt_module.SPECIFICATION_AUTHOR_INSTRUCTIONS
    )
    assert issubclass(output_schema, BaseModel)
    assert output_schema.model_config["extra"] == "allow"
    adapter = TypeAdapter(output_schema)
    assert adapter.validate_python(
        {"payload": {"schema_version": "agileforge.spec.v1"}}
    ).model_dump() == {"payload": {"schema_version": "agileforge.spec.v1"}}
    assert adapter.validate_python(
        {"payload": {"schema_version": "agileforge.spec.v2"}}
    ).model_dump() == {"payload": {"schema_version": "agileforge.spec.v2"}}


def test_model_output_contract_is_a_json_object() -> None:
    """Reject scalar/list output before it reaches semantic classification."""
    output_schema = importlib.import_module(
        "adapters.adk.agents.specification_author"
    ).SpecificationAuthoringModelOutput

    assert TypeAdapter(JsonObject).validate_python(
        output_schema.model_validate({"payload": {}}).model_dump()
    ) == {"payload": {}}
