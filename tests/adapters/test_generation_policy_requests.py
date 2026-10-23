"""Every retained role delegates output capacity to the shared provider boundary."""

from __future__ import annotations

import copy
import importlib
import json
from typing import TYPE_CHECKING, cast

import pytest
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from adapters.adk.provider_models import create_openrouter_model
from services.application import _agentic_execution_settings
from services.contracts.story import UserStoryWriterOutput
from tests.adapters.test_product_goal_generation import _CaptureClient
from utils import model_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from google.adk.agents import Agent

MODELS = (
    "openrouter/openai/gpt-5.6-luna",
    "openrouter/openai/gpt-6-sol",
    "openrouter/anthropic/claude-sonnet-4.5",
)
MODULES = (
    "product_goal",
    "vision",
    "specification_author",
    "spec_validator",
    "backlog",
    "roadmap",
    "story",
    "sprint",
)
STAGES = (
    "goal",
    "vision",
    "vision_repair",
    "specification",
    "specification_review",
    "backlog",
    "roadmap",
    "story",
    "story_patch",
    "sprint",
)
LEGACY_SETTINGS = (
    "VISION_INTERVIEWER_MAX_TOKENS",
    "SPECIFICATION_STRUCTURER_MAX_TOKENS",
    "SPEC_VALIDATOR_MAX_TOKENS",
    "BACKLOG_PRIMER_MAX_TOKENS",
    "ROADMAP_BUILDER_MAX_TOKENS",
    "STORY_WRITER_MAX_TOKENS",
    "SPRINT_PLANNER_MAX_TOKENS",
)
ROLES = (
    "product_goal",
    "product_vision",
    "specification_structurer",
    "spec_validator",
    "backlog_primer",
    "roadmap_builder",
    "user_story_writer",
    "sprint_planner",
)


@pytest.fixture(params=MODELS)
def configured_agents(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[str, dict[str, Agent]]]:
    """Compose each real role under explicit model/effort and hostile legacy caps."""
    selected = str(request.param)
    configuration = tmp_path / "models.json"
    configuration.write_text(
        json.dumps(
            {
                "models": dict.fromkeys(ROLES, selected),
                "reasoning": {"effort": "max"},
            }
        )
    )
    original_modules = {
        name: importlib.import_module("adapters.adk.agents." + name) for name in MODULES
    }
    original_namespaces = {
        name: dict(module.__dict__) for name, module in original_modules.items()
    }
    try:
        with monkeypatch.context() as settings:
            settings.setenv("MODEL_CONFIG_PATH", str(configuration))
            for name in LEGACY_SETTINGS:
                settings.setenv(name, "1")
            model_config.clear_config_cache()
            modules = {
                name: importlib.reload(
                    importlib.import_module("adapters.adk.agents." + name)
                )
                for name in MODULES
            }
            yield (
                selected,
                {
                    "goal": modules["product_goal"].root_agent,
                    "vision": modules["vision"].root_agent,
                    "vision_repair": modules["vision"].repair_agent,
                    "specification": modules["specification_author"].root_agent,
                    "specification_review": modules["spec_validator"].root_agent,
                    "backlog": modules["backlog"].root_agent,
                    "roadmap": modules["roadmap"].root_agent,
                    "story": modules["story"].root_agent,
                    "story_patch": modules["story"].create_user_story_patch_agent(),
                    "sprint": modules["sprint"].root_agent,
                },
            )
    finally:
        model_config.clear_config_cache()
        for name, module in original_modules.items():
            module.__dict__.clear()
            module.__dict__.update(original_namespaces[name])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
async def test_real_role_request_has_no_application_cap_and_preserves_configuration(
    configured_agents: tuple[str, dict[str, Agent]], stage: str
) -> None:
    """The SDK receives exact model/effort/schema/privacy, without a role budget."""
    selected, agents = configured_agents
    agent = agents[stage]
    client = _CaptureClient()
    assert isinstance(agent.model, LiteLlm)
    model = agent.model.model_copy(update={"llm_client": client})
    request = LlmRequest(
        model=selected,
        contents=[
            types.Content(role="user", parts=[types.Part(text="complete input")])
        ],
        config=agent.generate_content_config or types.GenerateContentConfig(),
    )
    schema = agent.output_schema or UserStoryWriterOutput
    request.set_output_schema(schema)
    responses = [response async for response in model.generate_content_async(request)]
    assert len(responses) == 1
    assert len(client.requests) == 1
    composed = client.requests[0]
    assert composed["model"] == selected
    assert composed.get("max_tokens") is None
    assert composed.get("max_completion_tokens") is None
    assert composed.get("max_output_tokens") is None
    assert composed.get("stop") is None
    body = composed["extra_body"]
    assert isinstance(body, dict)
    assert body == model_config.get_openrouter_extra_body()
    assert cast("dict[str, object]", body)["reasoning"] == {"effort": "max"}
    response_format = composed["response_format"]
    assert isinstance(response_format, dict)
    assert cast("dict[str, object]", response_format)["type"] == "json_schema"


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", MODELS)
async def test_factory_removes_budgets_before_adk_composition(model_id: str) -> None:
    """The factory strips supported caps without touching aliased business payloads."""
    reasoning: dict[str, object] = {
        "effort": "max",
        "enabled": True,
        "exclude": True,
        "max_tokens": 4096,
        "max_completion_tokens": 1,
        "max_output_tokens": 2,
        "budget_tokens": 3,
    }
    business = {"reasoning": reasoning, "max_tokens": 4096, "timeout": 1}
    body: dict[str, object] = {
        "reasoning": reasoning,
        "provider": {"zdr": True},
        "metadata": business,
        "max_tokens": 1,
        "max_completion_tokens": 2,
        "max_output_tokens": 3,
        "timeout": 1,
        "request_timeout": 1,
    }
    arguments: dict[str, object] = {
        "reasoning": reasoning,
        "extra_body": body,
        "max_tokens": 1,
        "max_completion_tokens": 2,
        "max_output_tokens": 3,
        "timeout": 1,
        "request_timeout": 1,
    }
    before = copy.deepcopy(arguments)
    client = _CaptureClient()
    model = create_openrouter_model(model_id=model_id, **arguments).model_copy(
        update={"llm_client": client}
    )
    request = LlmRequest(
        model=model_id,
        contents=[
            types.Content(
                role="user", parts=[types.Part(text="Unchanged input: max_tokens=4096")]
            )
        ],
        config=types.GenerateContentConfig(),
    )
    responses = [item async for item in model.generate_content_async(request)]
    assert len(responses) == len(client.requests) == 1
    composed = client.requests[0]
    expected = {"effort": "max", "enabled": True, "exclude": True}
    assert composed["reasoning"] == expected
    assert composed["extra_body"] == {
        "reasoning": expected,
        "provider": {"zdr": True},
        "metadata": business,
    }
    for key in (
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "timeout",
        "request_timeout",
    ):
        assert composed.get(key) is None
    assert arguments == before
    assert arguments["reasoning"] is body["reasoning"] is reasoning
    assert business["reasoning"] is reasoning


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning", [None, "unsupported-value", ["effort", "max"]])
async def test_factory_preserves_nonmapping_reasoning_for_existing_validation(
    reasoning: object,
) -> None:
    """Normalization does not invent a general reasoning schema validator."""
    client = _CaptureClient()
    model = create_openrouter_model(
        model_id=MODELS[0], reasoning=reasoning, extra_body={"reasoning": reasoning}
    ).model_copy(update={"llm_client": client})
    request = LlmRequest(
        model=MODELS[0],
        contents=[types.Content(role="user", parts=[types.Part(text="input")])],
        config=types.GenerateContentConfig(),
    )
    responses = [item async for item in model.generate_content_async(request)]
    assert len(responses) == 1
    assert client.requests[0]["reasoning"] == reasoning
    assert client.requests[0]["extra_body"] == {"reasoning": reasoning}


@pytest.mark.parametrize(
    "node_id",
    [
        "goal.interview",
        "vision.bootstrap",
        "vision.interview",
        "specification.structure",
        "backlog.generate",
        "planning.roadmap.generate",
        "planning.story.generate",
        "planning.sprint.plan",
    ],
)
def test_each_attempt_records_policy_and_ignores_injected_legacy_caps(
    node_id: str,
) -> None:
    """All real nodes record one policy and ignore injected legacy limits."""
    settings = _agentic_execution_settings(
        node_id,
        specification_generation_config={"max_output_tokens": 1},
        vision_generation_config={"max_output_tokens": 1},
    )
    policy = settings["generation_policy"]
    assert isinstance(policy, dict)
    assert policy["application_output_cap"] is None
    assert policy["actual_prompt_allowance"] == "unverified"
    assert settings.get("generation_config", {}) == {}
