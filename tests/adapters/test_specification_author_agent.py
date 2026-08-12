"""Production Specification author agent configuration boundaries."""

from __future__ import annotations

import importlib
import os
import subprocess  # nosec B404  # test-only clean-process import boundary
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from services.contracts.specification_authoring import SpecificationStructuringOutput
from workflow.contracts import JsonObject


def test_structurer_agent_imports_in_a_clean_process() -> None:
    """Keep the ADK entrypoint free of package-initialization cycles."""
    completed = subprocess.run(  # nosec B603
        (
            sys.executable,
            "-c",
            "import adapters.adk.agents.specification_author",
        ),
        cwd=Path(__file__).parents[2],
        env={**os.environ, "OPENROUTER_API_KEY": "test-key"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_structurer_prompt_hash_binds_the_actual_packaged_instructions() -> None:
    """Fail collection when the loaded to-spec prompt drifts from provenance."""
    prompt_module = importlib.import_module("adapters.adk.prompts.specification_author")
    contract = importlib.import_module("services.contracts.specification_authoring")
    prompts = importlib.import_module("adapters.adk.prompts")

    instructions = prompts.load_prompt("specification_author.txt")

    assert hasattr(prompt_module, "SPECIFICATION_STRUCTURER_INSTRUCTIONS")
    assert hasattr(contract, "SPECIFICATION_STRUCTURER_PROMPT_HASH")
    assert instructions == prompt_module.SPECIFICATION_STRUCTURER_INSTRUCTIONS
    assert prompt_module.SPECIFICATION_STRUCTURER_PROMPT_HASH == (
        contract.SPECIFICATION_STRUCTURER_PROMPT_HASH
    )
    assert contract.compute_specification_structurer_prompt_hash(instructions) == (
        contract.SPECIFICATION_STRUCTURER_PROMPT_HASH
    )
    assert contract.SPECIFICATION_STRUCTURER_VERSION == "1.0.0"
    assert contract.SPECIFICATION_STRUCTURER_PROMPT_VERSION == (
        "agileforge.specification-structurer.prompt.v1"
    )
    assert contract.SPECIFICATION_STRUCTURER_PROMPT_HASH == (
        "sha256:fec7c251132af921dd721e5e3cdea758eef95ce0437bfd85d2f24dad00c70e21"
    )


def test_agent_advertises_the_exact_closed_structuring_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave schema-versus-payload classification to the recipe wrapper."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    module = importlib.import_module("adapters.adk.agents.specification_author")
    prompt_module = importlib.import_module("adapters.adk.prompts.specification_author")

    output_schema = module.root_agent.output_schema

    contract = importlib.import_module("services.contracts.specification_authoring")

    assert module.root_agent.name == "specification_structurer"
    assert output_schema is contract.SpecificationStructuringOutput
    assert module.root_agent.instruction == (
        prompt_module.SPECIFICATION_STRUCTURER_INSTRUCTIONS
    )
    assert output_schema.model_config["extra"] == "forbid"
    assert output_schema.model_config["frozen"] is True
    adapter = TypeAdapter(output_schema)
    with pytest.raises(ValidationError):
        adapter.validate_python({"payload": {"schema_version": "agileforge.spec.v1"}})


def test_model_output_contract_is_a_json_object() -> None:
    """Retain JSON-object compatibility after closing the provider schema."""
    output = SpecificationStructuringOutput.model_validate(
        {
            "payload": {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": "SPEC.closed-provider",
                "title": "Closed provider",
                "summary": "Expose the exact typed structuring result.",
                "problem_statement": "Permissive provider schemas hide drift.",
                "items": [],
            }
        }
    )

    assert (
        TypeAdapter(JsonObject).validate_python(output.model_dump(mode="json"))[
            "payload"
        ]["schema_version"]
        == "agileforge.spec.v2"
    )
