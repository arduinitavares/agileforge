"""Provider capacities, wire requests and explicit accounting limits."""

from __future__ import annotations

import copy
import importlib
import json
from dataclasses import replace
from http import HTTPStatus
from typing import cast

import httpx
import pytest
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from litellm.exceptions import UnsupportedParamsError
from sqlalchemy.engine import Engine  # noqa: TC002
from sqlmodel import Session, select

from adapters.adk import provider_retry
from adapters.adk.agents.product_goal import root_agent
from adapters.adk.errors import GenerationProviderError
from adapters.adk.provider_models import create_openrouter_model
from adapters.adk.provider_transport import OpenRouterOneSendCompletion
from adapters.adk.runner import AdkRunGuards
from models.product_definition import ProductGoalInterviewTurn
from models.workflow import WorkflowNodeAttempt
from services.application import _agentic_execution_settings
from services.contracts.generation_policy import GenerationAllowance  # noqa: TC001
from tests.adapters.test_adk_workflow_runner import (
    _goal_decision,
    _goal_runner_input,
    _goal_runner_system,
)
from tests.adapters.test_product_goal_generation import _complete_goal
from tests.adapters.test_provider_boundary_routes import Audit, Clock, context
from tests.adapters.test_provider_transport_contract import SUCCESS, Wire
from workflow.contracts import NodeCategory, WorkflowErrorCode

PUBLISHED_LARGE_CAPACITY = 128_000
PUBLISHED_SMALL_CAPACITY = 64_000
EXPECTED_METADATA_GETS = 2

_ALIASED_REASONING: dict[str, object] = {"effort": "max", "budget_tokens": 4096}
_REASONING_CASES = [
    pytest.param(
        {
            "effort": "max",
            "max_tokens": 4096,
            "max_completion_tokens": 1,
            "max_output_tokens": 2,
            "budget_tokens": 3,
        },
        None,
        {"effort": "max"},
        id="root-budgets",
    ),
    pytest.param(
        None,
        {
            "effort": "max",
            "max_tokens": 4096,
            "max_completion_tokens": 1,
            "max_output_tokens": 2,
            "budget_tokens": 3,
        },
        {"effort": "max"},
        id="nested-budgets",
    ),
    pytest.param(
        {"max_tokens": 4096}, {"effort": "max"}, {"effort": "max"}, id="cap-only-root"
    ),
    pytest.param(
        {"effort": "max"}, {"max_tokens": 4096}, {"effort": "max"}, id="cap-only-nested"
    ),
    pytest.param({}, {"effort": "max"}, {"effort": "max"}, id="empty-root"),
    pytest.param({"effort": "max"}, {}, {"effort": "max"}, id="empty-nested"),
    pytest.param(
        _ALIASED_REASONING,
        _ALIASED_REASONING,
        {"effort": "max"},
        id="aliased-locations",
    ),
    pytest.param(
        {"effort": "max", "enabled": True, "exclude": True},
        {"effort": "max", "enabled": True, "exclude": True},
        {"effort": "max", "enabled": True, "exclude": True},
        id="identical-controls",
    ),
    pytest.param(
        {"enabled": True}, {"effort": "max"}, {"effort": "max"}, id="disjoint-controls"
    ),
    pytest.param(
        {"effort": "max"},
        {"exclude": True},
        {"exclude": True},
        id="reverse-disjoint-controls",
    ),
    pytest.param(
        {"effort": "medium"},
        {"effort": "max"},
        {"effort": "max"},
        id="conflicting-effort",
    ),
    pytest.param(
        {"effort": "max"},
        {"effort": "medium"},
        {"effort": "medium"},
        id="reverse-conflicting-effort",
    ),
    pytest.param({"max_tokens": 4096}, None, None, id="only-root-budget"),
    pytest.param(None, {"budget_tokens": 4096}, None, id="only-nested-budget"),
]


def _descriptor(
    model: str, ceiling: int | None = 128_000
) -> dict:  # test-only heterogeneous public API fixture
    return {
        "data": {
            "id": model.removeprefix("openrouter/"),
            "endpoints": [
                {
                    "provider_name": "Public provider A",
                    "tag": "provider-a",
                    "context_length": 1_050_000,
                    "max_completion_tokens": ceiling,
                    "max_prompt_tokens": 922_000,
                    "supported_parameters": ["max_tokens", "max_completion_tokens"],
                },
                {
                    "provider_name": "Public provider B",
                    "tag": "provider-b",
                    "context_length": 200_000,
                    "max_completion_tokens": 64_000,
                    "max_prompt_tokens": None,
                    "supported_parameters": ["max_tokens"],
                },
            ],
        }
    }


def _metadata_transport(
    payload: object, gets: list[httpx.Request]
) -> httpx.MockTransport:
    def get(request: httpx.Request) -> httpx.Response:
        gets.append(request)
        assert request.method == "GET"
        assert request.url.host == "openrouter.ai"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=payload, request=request)

    return httpx.MockTransport(get)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "openrouter/openai/gpt-5.6-luna",
        "openrouter/openai/gpt-6.1-sol",
        "openrouter/anthropic/claude-sonnet-4.5",
    ],
)
async def test_unsupported_thinking_with_effort_still_fails_before_http_send(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing legacy reasoning caps does not hide the pinned SDK rejection."""
    transport = _metadata_transport(_descriptor(model), [])

    async def resolve(selected: str) -> GenerationAllowance:
        resolver = importlib.import_module("adapters.adk.generation_allowance")
        return await resolver.resolve_generation_allowance(
            selected, transport=transport
        )

    monkeypatch.setattr(provider_retry, "resolve_generation_allowance", resolve)
    sends: list[httpx.Request] = []
    clock = Clock()
    audit = Audit(clock)
    client = provider_retry.RetryingOpenRouterClient(
        completion=OpenRouterOneSendCompletion(
            transport_factory=lambda: Wire([(200, SUCCESS, {})], sends, [])
        ),
        clock=clock,
    )
    with (
        provider_retry.bind_provider_action(
            context(clock, audit, action_deadline=None)
        ),
        pytest.raises(UnsupportedParamsError),
    ):
        await client.acompletion(
            model=model,
            messages=[{"role": "user", "content": "complete input"}],
            tools=[],
            api_key="synthetic-key",
            thinking={"type": "enabled", "budget_tokens": 4096},
            extra_body={"reasoning": {"effort": "medium"}},
        )
    assert sends == []
    assert audit.finishes[-1].disposition == "non_retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "openrouter/openai/gpt-5.6-luna",
        "openrouter/openai/gpt-6.1-sol",
        "openrouter/anthropic/claude-sonnet-4.5",
    ],
)
@pytest.mark.parametrize("route", ["factory", "late-kwargs"])
@pytest.mark.parametrize(
    ("root_reasoning", "nested_reasoning", "expected"), _REASONING_CASES
)
async def test_real_wire_discards_reasoning_caps_in_both_locations(  # noqa: PLR0913
    model: str,
    route: str,
    root_reasoning: dict[str, object] | None,
    nested_reasoning: dict[str, object] | None,
    expected: dict[str, object] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budgets and empty masks cannot change effective reasoning or caller data."""
    metadata_gets: list[httpx.Request] = []
    transport = _metadata_transport(_descriptor(model), metadata_gets)

    async def resolve(selected: str) -> GenerationAllowance:
        resolver = importlib.import_module("adapters.adk.generation_allowance")
        return await resolver.resolve_generation_allowance(
            selected, transport=transport
        )

    monkeypatch.setattr(provider_retry, "resolve_generation_allowance", resolve)
    sends: list[httpx.Request] = []
    clock = Clock()
    audit = Audit(clock)
    client = provider_retry.RetryingOpenRouterClient(
        completion=OpenRouterOneSendCompletion(
            transport_factory=lambda: Wire([(200, SUCCESS, {})], sends, [])
        ),
        clock=clock,
    )
    privacy = {
        "data_collection": "deny",
        "zdr": True,
        "allow_fallbacks": True,
        "require_parameters": True,
        "sort": "price",
    }
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "Complete",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"max_tokens": {"type": "integer"}},
                "required": ["max_tokens"],
                "additionalProperties": False,
            },
        },
    }
    body: dict[str, object] = {
        "provider": privacy,
        "max_tokens": 1,
        "max_completion_tokens": 2,
        "max_output_tokens": 3,
        "timeout": 1,
        "request_timeout": 1,
    }
    arguments: dict[str, object] = {
        "extra_body": body,
        "api_key": "synthetic-key",
        "response_format": schema,
        "max_tokens": 1,
        "max_completion_tokens": 2,
        "max_output_tokens": 3,
        "timeout": 1,
        "request_timeout": 1,
    }
    if root_reasoning is not None:
        arguments["reasoning"] = root_reasoning
    if nested_reasoning is not None:
        body["reasoning"] = nested_reasoning
    before = copy.deepcopy(arguments)
    messages = [{"role": "user", "content": "Exact input: max_tokens=4096"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "retain",
                "parameters": {
                    "type": "object",
                    "properties": {"budget_tokens": {"type": "integer"}},
                },
            },
        }
    ]
    with provider_retry.bind_provider_action(
        context(clock, audit, action_deadline=None)
    ):
        if route == "factory":
            llm = create_openrouter_model(model_id=model, **arguments).model_copy(
                update={"llm_client": client}
            )
            request = LlmRequest(
                model=model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text="Exact input: max_tokens=4096")],
                    )
                ],
                config=types.GenerateContentConfig(),
            )
            responses = [item async for item in llm.generate_content_async(request)]
            assert len(responses) == 1
        else:
            await client.acompletion(
                model=model, messages=messages, tools=tools, **arguments
            )
    assert arguments == before
    assert arguments.get("reasoning") is root_reasoning
    assert body.get("reasoning") is nested_reasoning
    assert len(sends) == len(metadata_gets) == 1
    sent = json.loads(sends[0].content)
    assert sent.get("reasoning") == expected
    if expected is None:
        assert "reasoning" not in sent
    parameter = "max_completion_tokens" if "/openai/gpt-" in model else "max_tokens"
    assert sent[parameter] == PUBLISHED_LARGE_CAPACITY
    other = (
        "max_tokens"
        if parameter == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert other not in sent
    assert "max_output_tokens" not in sent
    assert "timeout" not in sent
    assert "request_timeout" not in sent
    assert sent["model"] == model.removeprefix("openrouter/")
    assert sent["messages"] == messages
    assert sent["response_format"] == schema
    assert sent["provider"] == privacy
    if route == "late-kwargs":
        assert sent["tools"] == tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "openrouter/openai/gpt-5.6-luna",
        "openrouter/openai/gpt-6-sol",
        "openrouter/anthropic/claude-sonnet-4.5",
    ],
)
async def test_documented_capacity_replaces_legacy_caps_on_actual_wire(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owned SDK sends a published ceiling, exact input, effort and privacy."""
    metadata_gets: list[httpx.Request] = []
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    descriptor = _descriptor(model)
    transport = _metadata_transport(descriptor, metadata_gets)

    async def resolve(selected: str) -> GenerationAllowance:
        resolver = importlib.import_module("adapters.adk.generation_allowance")
        return await resolver.resolve_generation_allowance(
            selected, transport=transport
        )

    monkeypatch.setattr(
        provider_retry, "resolve_generation_allowance", resolve, raising=False
    )
    clock = Clock()
    audit = Audit(clock)
    completion = OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire([(200, SUCCESS, {})], sends, closed)
    )
    client = provider_retry.RetryingOpenRouterClient(completion=completion, clock=clock)
    messages = [
        {"role": "user", "content": "PRIVATE_INPUT preserved without truncation"}
    ]
    privacy = {
        "data_collection": "deny",
        "zdr": True,
        "allow_fallbacks": True,
        "require_parameters": True,
        "sort": "price",
    }
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "Complete",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }
    with provider_retry.bind_provider_action(
        context(clock, audit, action_deadline=None)
    ):
        await client.acompletion(
            model=model,
            messages=messages,
            tools=[],
            api_key="synthetic-key",
            max_tokens=1,
            max_completion_tokens=1,
            response_format=schema,
            extra_body={
                "provider": privacy,
                "reasoning": {"effort": "max", "max_tokens": 1},
            },
        )
    assert len(sends) == 1
    sent = json.loads(sends[0].content)
    parameter = "max_completion_tokens" if "/openai/gpt-" in model else "max_tokens"
    assert sent[parameter] == PUBLISHED_LARGE_CAPACITY
    assert sent["model"] == model.removeprefix("openrouter/")
    assert sent["messages"] == messages
    assert sent["response_format"] == schema
    assert sent["provider"] == privacy
    assert sent["reasoning"] == {"effort": "max"}
    assert len(metadata_gets) == 1
    evidence = audit.starts[0].generation_allowance
    assert evidence is not None
    assert evidence.completion_tokens == PUBLISHED_LARGE_CAPACITY
    assert evidence.actual_prompt_allowance == "unverified"
    assert evidence.route_eligibility == "provider_enforced_unverified"
    assert "PRIVATE_INPUT" not in evidence.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, 0, -1, True, "128000"])
async def test_unknown_or_invalid_capacities_do_not_become_guessed_limits(
    bad: object,
) -> None:
    """Unknown metadata cannot authorize an arbitrary cap or a default-only claim."""
    resolver = importlib.import_module("adapters.adk.generation_allowance")
    payload = _descriptor("openrouter/openai/gpt-6-sol")
    data = payload["data"]
    data["endpoints"] = [data["endpoints"][0]]
    data["endpoints"][0]["max_completion_tokens"] = bad
    with pytest.raises(GenerationProviderError) as captured:
        await resolver.resolve_generation_allowance(
            "openrouter/openai/gpt-6-sol", transport=_metadata_transport(payload, [])
        )
    assert captured.value.code == "PROVIDER_CAPACITY_UNAVAILABLE"
    assert "128000" not in str(captured.value)


@pytest.mark.asyncio
async def test_mixed_capacities_record_unknown_routes() -> None:
    """Unknown route eligibility is retained rather than hidden by a minimum."""
    resolver = importlib.import_module("adapters.adk.generation_allowance")
    payload = _descriptor("openrouter/openai/gpt-6-sol", None)
    resolved = await resolver.resolve_generation_allowance(
        "openrouter/openai/gpt-6-sol", transport=_metadata_transport(payload, [])
    )
    assert resolved.completion_tokens == PUBLISHED_SMALL_CAPACITY
    assert resolved.unknown_capacity_endpoints == 1
    assert resolved.actual_prompt_allowance == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("context_length_exceeded", "PROVIDER_CONTEXT_LIMIT_EXCEEDED"),
        ("context_window_exceeded", "PROVIDER_CONTEXT_LIMIT_EXCEEDED"),
        ("unsupported_parameter", "PROVIDER_REQUEST_REJECTED"),
    ],
)
async def test_owned_rejection_is_actionable_without_echoing_provider_text(
    provider_code: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify actual owned request behavior and retained safe evidence."""
    resolver = importlib.import_module("adapters.adk.generation_allowance")
    model = "openrouter/openai/gpt-6-sol"

    async def resolve(selected: str) -> GenerationAllowance:
        return await resolver.resolve_generation_allowance(
            selected, transport=_metadata_transport(_descriptor(model), [])
        )

    monkeypatch.setattr(provider_retry, "resolve_generation_allowance", resolve)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    payload = {
        "error": {
            "code": 400,
            "message": "PRIVATE_INPUT echoed by provider",
            "metadata": {"error_type": provider_code},
        }
    }
    clock = Clock()
    audit = Audit(clock)
    client = provider_retry.RetryingOpenRouterClient(
        completion=OpenRouterOneSendCompletion(
            transport_factory=lambda: Wire([(400, payload, {})], sends, closed)
        ),
        clock=clock,
    )
    with (
        provider_retry.bind_provider_action(
            context(clock, audit, action_deadline=None)
        ),
        pytest.raises(GenerationProviderError) as captured,
    ):
        await client.acompletion(
            model=model,
            tools=[],
            api_key="synthetic-key",
            messages=[{"role": "user", "content": "PRIVATE_INPUT"}],
            extra_body={"reasoning": {"effort": "max"}},
        )
    assert getattr(captured.value, "code", None) == expected
    assert "PRIVATE_INPUT" not in str(captured.value)
    assert "unverified" in str(captured.value)
    assert "new idempotency key" in str(captured.value)
    assert len(sends) == 1
    assert len(audit.starts) == 1
    assert audit.finishes[0].http_status == HTTPStatus.BAD_REQUEST


@pytest.mark.asyncio
async def test_transient_retry_reuses_resolved_request_but_new_input_resolves_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify actual owned request behavior and retained safe evidence."""
    resolver = importlib.import_module("adapters.adk.generation_allowance")
    model = "openrouter/openai/gpt-6-sol"
    gets: list[httpx.Request] = []

    async def resolve(selected: str) -> GenerationAllowance:
        ceiling = 128_000 if not gets else 64_000
        payload = _descriptor(model, ceiling)
        return await resolver.resolve_generation_allowance(
            selected, transport=_metadata_transport(payload, gets)
        )

    monkeypatch.setattr(provider_retry, "resolve_generation_allowance", resolve)
    sends: list[httpx.Request] = []
    responses = [
        (503, {"error": {"code": 503, "message": "unavailable"}}, {}),
        (200, SUCCESS, {}),
        (200, SUCCESS, {}),
    ]
    completion = OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire([responses.pop(0)], sends, [])
    )
    clock = Clock()
    audit = Audit(clock)
    client = provider_retry.RetryingOpenRouterClient(
        completion=completion, clock=clock, uniform=lambda _a, _b: 0
    )
    with provider_retry.bind_provider_action(
        context(clock, audit, action_deadline=None)
    ):
        for content in ("original", "original plus full repair feedback"):
            await client.acompletion(
                model=model,
                tools=[],
                api_key="synthetic-key",
                messages=[{"role": "user", "content": content}],
                reasoning={"effort": "max", "max_tokens": 4096, "max_output_tokens": 1},
                extra_body={"reasoning": {"budget_tokens": 4096}},
            )
    assert len(gets) == EXPECTED_METADATA_GETS
    bodies = [json.loads(send.content) for send in sends]
    assert bodies[0] == bodies[1]
    assert bodies[0]["reasoning"] == bodies[2]["reasoning"] == {"effort": "max"}
    assert bodies[0]["max_completion_tokens"] == PUBLISHED_LARGE_CAPACITY
    assert bodies[2]["max_completion_tokens"] == PUBLISHED_SMALL_CAPACITY
    assert bodies[2]["messages"][0]["content"] == "original plus full repair feedback"
    assert audit.starts[0].generation_allowance == audit.starts[1].generation_allowance
    assert audit.starts[2].generation_allowance != audit.starts[0].generation_allowance
    assert audit.starts[0].request_fingerprint == audit.starts[1].request_fingerprint
    assert audit.starts[2].request_fingerprint != audit.starts[0].request_fingerprint


@pytest.mark.parametrize("rejected", [False, True])
def test_real_goal_wire_preserves_human_review_and_safe_failure_replay(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    rejected: bool,
) -> None:
    """Real transport retains complete output or a durable safe failure."""
    model_id = "openrouter/openai/gpt-6-sol"
    gets: list[httpx.Request] = []
    resolver = importlib.import_module("adapters.adk.generation_allowance")

    async def resolve(selected: str) -> GenerationAllowance:
        return await resolver.resolve_generation_allowance(
            selected, transport=_metadata_transport(_descriptor(model_id), gets)
        )

    monkeypatch.setattr(provider_retry, "resolve_generation_allowance", resolve)
    response = cast("dict", copy.deepcopy(SUCCESS))
    response["choices"][0]["message"]["content"] = json.dumps(_complete_goal())
    response["usage"] = {
        "prompt_tokens": 10,
        "completion_tokens": 20_000,
        "total_tokens": 20_010,
        "completion_tokens_details": {"reasoning_tokens": 15_000},
    }
    status = 200
    if rejected:
        status = 400
        response = {
            "error": {
                "code": 400,
                "message": "PRIVATE_INPUT provider echo",
                "metadata": {"error_type": "context_length_exceeded"},
            }
        }
    sends: list[httpx.Request] = []
    client = provider_retry.RetryingOpenRouterClient(
        completion=OpenRouterOneSendCompletion(
            transport_factory=lambda: Wire([(status, response, {})], sends, [])
        )
    )
    assert isinstance(root_agent.model, LiteLlm)
    model = root_agent.model.model_copy(
        update={"model": model_id, "llm_client": client}
    )
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": model})
    )
    runner._config = replace(
        runner._config,
        model_id=model_id,
        execution_settings={
            **_agentic_execution_settings("goal.interview"),
            "generation_config": {"max_tokens": 1},
        },
    )
    decision = _goal_decision(domain, project_id)
    guards = AdkRunGuards(
        position=domain.position(project_id),
        idempotency_key="goal-wire",
        actor="operator",
    )
    result = runner.run(decision, _goal_runner_input(), guards=guards)
    assert result.ok is not rejected
    assert len(sends) == len(gets) == 1
    assert (
        json.loads(sends[0].content)["max_completion_tokens"]
        == PUBLISHED_LARGE_CAPACITY
    )
    if rejected:
        assert result.error is not None
        assert result.error.code is WorkflowErrorCode.PROVIDER_CONTEXT_LIMIT_EXCEEDED
        assert "PRIVATE_INPUT" not in result.error.message
        replay = runner.run(decision, _goal_runner_input(), guards=guards)
        assert replay == result.model_copy(update={"replayed": True})
        assert len(sends) == len(gets) == 1
    with Session(engine) as session:
        attempt = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.idempotency_key == "goal-wire"
            )
        ).one()
        assert json.loads(attempt.execution_settings_json)["generation_config"] == {}
        turns = session.exec(
            select(ProductGoalInterviewTurn).where(
                ProductGoalInterviewTurn.attempt_fingerprint
                == attempt.attempt_fingerprint
            )
        ).all()
        assert len(turns) == (0 if rejected else 1)
        if not rejected:
            assert any(
                item.node_id == "goal.review" and item.category is NodeCategory.WAITING
                for item in domain.position(project_id).decisions
            )


@pytest.mark.asyncio
async def test_foreign_model_metadata_cannot_authorize_selected_request() -> None:
    """A successful lookup must identify the exact selected public model."""
    resolver = importlib.import_module("adapters.adk.generation_allowance")
    payload = _descriptor("openrouter/another/model")
    with pytest.raises(GenerationProviderError) as captured:
        await resolver.resolve_generation_allowance(
            "openrouter/openai/gpt-6-sol", transport=_metadata_transport(payload, [])
        )
    assert captured.value.code == "PROVIDER_CAPACITY_UNAVAILABLE"
