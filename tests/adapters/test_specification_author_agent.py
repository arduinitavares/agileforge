"""Production Specification author agent configuration boundaries."""

from __future__ import annotations

import importlib
import json
import os
import subprocess  # nosec B404  # test-only clean-process import boundary
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from google.adk.models.lite_llm import (
    LiteLlm,
    LiteLLMClient,
    _to_litellm_response_format,
)
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from litellm import ModelResponse
from pydantic import TypeAdapter, ValidationError

from services.contracts.specification_authoring import SpecificationStructuringOutput
from workflow.contracts import JsonObject

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext

DEDICATED_TOKEN_BUDGET: int = 24_576
PRODUCTION_TOKEN_BUDGET: int = 32_768
NORMATIVE_FIDELITY_REQUIREMENTS: tuple[str, ...] = (
    "Preserve the restrictive force of every normative source contract.",
    (
        "Never transform equality into prefix, subset, example-only, optional, "
        "or advisory semantics."
    ),
    (
        "exact values and diagnostic messages, ordering, duplicate preservation, "
        "separators, standard output versus standard error, exit statuses, and "
        "trailing-newline behavior"
    ),
)


class _CapturingLiteLlmClient(LiteLLMClient):
    """Capture the exact completion kwargs without making a provider call."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def acompletion(
        self,
        model: object,
        messages: object,
        tools: object,
        **kwargs: object,
    ) -> ModelResponse:
        self.kwargs = {
            "model": model,
            "messages": messages,
            "tools": tools,
            **kwargs,
        }
        return ModelResponse(
            model="fake/specification-structurer",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ],
        )


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


def test_structurer_uses_explicit_dedicated_adk_generation_config() -> None:
    """Expose the effective budget on the ADK request instead of LiteLLM internals."""
    completed = subprocess.run(  # nosec B603
        (
            sys.executable,
            "-c",
            (
                "import json; "
                "from adapters.adk.agents.specification_author import root_agent; "
                "print(json.dumps({"
                "'max_output_tokens': "
                "root_agent.generate_content_config.max_output_tokens,"
                "'model_args': root_agent.model._additional_args"
                "}))"
            ),
        ),
        cwd=Path(__file__).parents[2],
        env={
            **os.environ,
            "OPENROUTER_API_KEY": "test-key",
            "SPECIFICATION_STRUCTURER_MAX_TOKENS": "24576",
            "VISION_INTERVIEWER_MAX_TOKENS": "1024",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    config = json.loads(completed.stdout)
    assert config["max_output_tokens"] == DEDICATED_TOKEN_BUDGET
    assert "max_tokens" not in config["model_args"]
    assert "max_completion_tokens" not in config["model_args"]


@pytest.mark.asyncio
async def test_adk_generation_config_reaches_litellm_completion_contract() -> None:
    """Map ADK max output tokens to the supported LiteLLM provider argument."""
    client = _CapturingLiteLlmClient()
    model = LiteLlm(
        model="openrouter/openai/gpt-5.6-luna",
        llm_client=client,
        drop_params=True,
    )
    request = LlmRequest(
        model=model.model,
        contents=[types.Content(role="user", parts=[types.Part(text="input")])],
        config=types.GenerateContentConfig(max_output_tokens=PRODUCTION_TOKEN_BUDGET),
    )

    _responses = [response async for response in model.generate_content_async(request)]

    assert client.kwargs["max_completion_tokens"] == PRODUCTION_TOKEN_BUDGET
    assert "max_tokens" not in client.kwargs


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
    assert contract.SPECIFICATION_STRUCTURER_VERSION == "1.0.1"
    assert contract.SPECIFICATION_STRUCTURER_PROMPT_VERSION == (
        "agileforge.specification-structurer.prompt.v2"
    )
    assert contract.SPECIFICATION_STRUCTURER_PROMPT_HASH == (
        "sha256:88cad14ee56fde7c351b98063f375b5bd7747d4eb7f2c89191cd29b560f1d669"
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


def test_production_structurer_receives_normative_fidelity_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give the production model explicit protection against semantic weakening."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    module = importlib.import_module("adapters.adk.agents.specification_author")

    instructions = module.root_agent.instruction

    assert isinstance(instructions, str)
    for requirement in NORMATIVE_FIDELITY_REQUIREMENTS:
        assert requirement in instructions


def test_agent_response_schema_uses_closed_removal_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep strict provider output free of unsupported free-form objects."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    module = importlib.import_module("adapters.adk.agents.specification_author")
    model = cast("LiteLlm", module.root_agent.model)

    response_format = _to_litellm_response_format(
        module.root_agent.output_schema,
        model.model,
    )
    assert response_format is not None
    schema = response_format["json_schema"]["schema"]
    removal_schema = schema["properties"]["removal_justifications"]

    assert removal_schema["type"] == "array"
    assert "additionalProperties" not in removal_schema


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


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        '{"payload": tru',
        '{"payload": 1e',
        '{"payload":"\\u12',
        '{"payload": [1, 2,',
    ],
)
def test_incomplete_json_classifier_covers_non_string_eof_positions(
    text: str | None,
) -> None:
    """Recognize EOF truncation even when provider finish metadata is absent."""
    module = importlib.import_module("adapters.adk.agents.specification_author")

    assert module._contains_incomplete_json(text) is True


@pytest.mark.parametrize(
    "text",
    ['{"payload":,}', '{"payload": 1.}', '{"payload": true garbage}'],
)
def test_incomplete_json_classifier_leaves_complete_malformed_json_to_schema(
    text: str,
) -> None:
    """Keep non-EOF validation failures in the existing closed-schema path."""
    module = importlib.import_module("adapters.adk.agents.specification_author")

    assert module._contains_incomplete_json(text) is False


@pytest.mark.parametrize(
    "finish_reason",
    [types.FinishReason.SAFETY, types.FinishReason.OTHER],
)
def test_empty_non_capacity_responses_keep_provider_failure_semantics(
    finish_reason: types.FinishReason,
) -> None:
    """Do not prescribe token-budget recovery for explicit provider rejections."""
    module = importlib.import_module("adapters.adk.agents.specification_author")

    module.reject_incomplete_specification_output(
        cast("CallbackContext", object()),
        LlmResponse(finish_reason=finish_reason),
    )
