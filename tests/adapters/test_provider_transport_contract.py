# ruff: noqa: PLR2004
# Expected protocol statuses, send counts and synthetic settings are literal assertions.
"""Real pinned OpenRouter translation over owned, socket-free HTTP transport."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import ModuleType

    from adapters.adk.provider_transport import OneSendAsyncHTTPHandler

import httpx
import litellm
import pytest
from litellm.exceptions import Timeout
from openai import APIError as OpenAIAPIError
from pydantic import ValidationError

from services.contracts.provider_retry import ProviderTransientFailure
from tests.adapters.test_provider_boundary_routes import Audit, Clock, boundary, context


def transport_module() -> ModuleType:
    """Transport module."""
    try:
        return importlib.import_module("adapters.adk.provider_transport")
    except ModuleNotFoundError:
        raise AssertionError from None


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sockets."""

    def deny(*args: object, **kwargs: object) -> object:
        """Deny."""
        del kwargs
        del args
        msg = "socket attempted in provider-free wire test"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


class Wire(httpx.MockTransport):
    """Provider-free wire fixture."""

    def __init__(
        self, script: Sequence[object], sends: list[httpx.Request], closed: list[bool]
    ) -> None:
        """Capture this instance's injected deterministic seams."""
        self.script = cast("list[object]", script)
        self.sends = sends
        self.closed = closed
        super().__init__(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Handle."""
        self.sends.append(request)
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, tuple)
        status, body, headers = cast("tuple[int, object, dict[str, str]]", outcome)
        return httpx.Response(status, json=body, headers=headers, request=request)

    async def aclose(self) -> None:
        """Aclose."""
        self.closed.append(True)
        await super().aclose()


SUCCESS = {
    "id": "safe",
    "object": "chat.completion",
    "created": 1,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "safe"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (504, {"error": {"message": "upstream timeout"}}, 504),
        (500, {}, 500),
        (500, {"error": {"code": "invalid"}}, 500),
        (
            500,
            {"error": {"message": "The server had an error processing your request."}},
            500,
        ),
        (200, {"error": {"code": 500, "message": "upstream unavailable"}}, 500),
        (200, {"error": {"code": "502", "message": "upstream unavailable"}}, 502),
        (429, {"error": {"message": "rate limited"}}, 429),
        (503, {"error": {"message": "upstream unavailable"}}, 503),
    ],
)
async def test_pinned_openrouter_one_http_send_per_audited_try(
    status: int,
    body: dict[str, object],
    expected: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify pinned openrouter one http send per audited try."""
    module = boundary()
    factory = (
        importlib.import_module("adapters.adk.provider_models")
        if hasattr(module, "RetryingOpenRouterClient")
        else None
    )
    assert factory is not None
    factory.create_openrouter_model(model_id="openrouter/test-model")
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    script = [(status, body, {"Retry-After": "2"}), (200, SUCCESS, {})]
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire(script, sends, closed)
    )
    client = module.RetryingOpenRouterClient(
        completion=completion, clock=clock, uniform=lambda _low, _high: 0.0
    )
    with module.bind_provider_action(context(clock, audit)):
        response = await client.acompletion(
            model="openrouter/test-model",
            messages=[{"role": "user", "content": "safe"}],
            tools=[],
            api_key="synthetic-key",
            temperature=0.2,
            max_tokens=128,
            extra_body={"provider": {"allow_fallbacks": True}},
        )
    assert response.choices[0].message.content == "safe"
    assert len(sends) == len(audit.starts) == len(audit.finishes) == 2
    assert len(closed) == 2
    assert audit.finishes[0].http_status == expected
    assert audit.finishes[0].retry_after_seconds == 2.0
    assert clock.waits == [2.0]
    bodies = [json.loads(request.content) for request in sends]
    assert bodies[0] == bodies[1]
    assert bodies[0]["model"] == "test-model"
    assert bodies[0]["temperature"] == 0.2
    assert bodies[0]["max_tokens"] == 128
    assert bodies[0]["provider"]["allow_fallbacks"] is True
    assert "Feedback" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_excluded_wire_status_sends_exactly_once(status: int) -> None:
    """Verify excluded wire status sends exactly once."""
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    script = [(status, {"error": {"message": "permanent"}}, {"Retry-After": "2"})]
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire(script, sends, closed)
    )
    module = boundary()
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(
            (OpenAIAPIError, httpx.HTTPError, ValidationError, ValueError)
        ) as caught,
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert not isinstance(caught.value, ProviderTransientFailure)
    assert len(sends) == len(audit.starts) == len(audit.finishes) == len(closed) == 1
    assert audit.finishes[0].disposition == "non_retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparation", "dispatched"])
async def test_owned_cap_counts_only_http_dispatches_and_closes_resources(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep unsent preparation distinct from a slow dispatched HTTP retry."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    entered = asyncio.Event()
    cleaned: list[bool] = []
    script = [(500, {"error": {"message": "unavailable"}}, {})]
    original_sdk = litellm.acompletion

    async def prepare(**kwargs: object) -> object:
        """Delay only retry preparation before it reaches the owned handler."""
        if phase == "preparation" and sends:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        return await original_sdk(**kwargs)

    monkeypatch.setattr(litellm, "acompletion", prepare)

    class SlowWire(Wire):
        """Retain first real SDK translation, then hold an actual retry send."""

        async def handle(self, request: httpx.Request) -> httpx.Response:
            """Block only the second physical dispatch."""
            if not sends:
                return await super().handle(request)
            sends.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
            raise AssertionError

    async def deadline(awaitable: Awaitable[object], seconds: float) -> object:
        """Expire the actual owned cap after preparation or send has begun."""
        if not sends:
            return await awaitable
        task = asyncio.ensure_future(awaitable)
        await entered.wait()
        clock.value += seconds
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise module.ProviderDeadlineExceeded

    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: SlowWire(script, sends, closed),
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(ProviderTransientFailure) as caught,
    ):
        await module.RetryingOpenRouterClient(
            completion=completion,
            clock=clock,
            uniform=lambda _low, _high: 0.0,
            timeout_runner=deadline,
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert (
        caught.value.summary.attempts
        == len(sends)
        == (1 if phase == "preparation" else 2)
    )
    assert caught.value.summary.http_status == 500
    assert len(closed) == 2
    assert cleaned == [True]
    assert audit.finishes[-1].disposition == (
        "not_sent" if phase == "preparation" else "exhausted"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connect", "protocol", "redirect", "timeout"])
async def test_owned_handler_does_not_resend_connection_errors_or_redirects(
    kind: str,
) -> None:
    """Verify owned handler does not resend connection errors or redirects."""
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    outcome: object = {
        "connect": httpx.ConnectError("synthetic"),
        "protocol": httpx.RemoteProtocolError("synthetic"),
        "timeout": httpx.ReadTimeout("synthetic"),
        "redirect": (307, {}, {"Location": "https://openrouter.ai/redirected"}),
    }[kind]
    script = [outcome]
    module = boundary()
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire(script, sends, closed)
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(
            (OpenAIAPIError, httpx.HTTPError, ValidationError, ValueError)
        ) as caught,
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert not isinstance(caught.value, ProviderTransientFailure)
    if kind == "timeout":
        assert isinstance(caught.value, Timeout)
        assert caught.value.status_code == 408
    assert len(sends) == len(audit.starts) == len(audit.finishes) == len(closed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("knob", "value"),
    [
        ("num_retries", 1),
        ("num_retries_per_request", 1),
        ("retry_policy", {"RateLimitErrorRetries": 1}),
        ("model_fallbacks", ["other"]),
        ("max_fallbacks", 1),
        ("default_fallbacks", ["other"]),
        ("fallbacks", ["other"]),
        ("context_window_fallbacks", {"other": "other"}),
        ("content_policy_fallbacks", {"other": "other"}),
        ("model_alias_map", {"test-model": "other"}),
        ("api_base", "https://other.invalid"),
        ("custom_llm_provider", "openai"),
        ("cache", object()),
    ],
)
async def test_global_multiplication_or_model_replacement_fails_closed_without_reset(
    knob: str, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify global multiplication or model replacement fails closed without reset."""
    monkeypatch.setattr(litellm, knob, value, raising=False)
    clock = Clock()
    audit = Audit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends
        sends += 1
        return object()

    module = boundary()
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(module.ProviderAttemptStopped),
    ):
        await module.RetryingOpenRouterClient(completion=send, clock=clock).acompletion(
            "openrouter/test-model", [], []
        )
    assert sends == 0
    assert not audit.starts
    assert getattr(litellm, knob) == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "controls",
    [
        {"num_retries": 1},
        {"max_retries": 2},
        {"retry_policy": {"RateLimitErrorRetries": 1}},
        {"fallbacks": ["other"]},
        {"context_window_fallback_dict": {"a": "b"}},
        {"model_list": []},
        {"custom_llm_provider": "openai"},
        {"api_base": "https://other.invalid"},
        {"client": object()},
        {"caching": True},
        {"cache_control": {"ttl": 60}},
    ],
)
async def test_request_override_controls_fail_closed(
    controls: dict[str, object],
) -> None:
    """Verify request override controls fail closed."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        msg = "override sent"
        raise AssertionError(msg)

    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(module.ProviderAttemptStopped),
    ):
        await module.RetryingOpenRouterClient(completion=send, clock=clock).acompletion(
            "openrouter/test-model", [], [], **controls
        )
    assert not audit.starts


def test_transport_resources_are_owned_per_try_across_event_loops() -> None:
    """Verify transport resources are owned per try across event loops."""
    module = boundary()
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    script = [(200, SUCCESS, {}), (200, SUCCESS, {})]
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire(script, sends, closed)
    )
    client = module.RetryingOpenRouterClient(completion=completion)

    async def action() -> None:
        """Run one separately bound action."""
        clock = Clock()
        audit = Audit(clock)
        with module.bind_provider_action(context(clock, audit, action_deadline=10**20)):
            await client.acompletion(
                "openrouter/test-model",
                [{"role": "user", "content": "safe"}],
                [],
                api_key="synthetic-key",
            )

    asyncio_module = importlib.import_module("asyncio")
    asyncio_module.run(action())
    asyncio_module.run(action())
    assert len(sends) == len(closed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", ["no_dispatch", "double_dispatch"])
async def test_owned_try_rejects_missing_or_second_dispatch(
    bypass: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Withhold cache-like success and stop any SDK resend before HTTPX.send."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    script = [(200, SUCCESS, {})] * 2
    original = litellm.acompletion

    async def sdk(**kwargs: object) -> object:
        """Exercise responses before dispatch and duplicate dispatch behavior."""
        if bypass == "no_dispatch":
            return litellm.ModelResponse(**SUCCESS)
        response = await original(**kwargs)
        handler = cast("OneSendAsyncHTTPHandler", kwargs["client"])
        await handler.post("https://openrouter.ai/api/v1/chat/completions", json={})
        return response

    monkeypatch.setattr(litellm, "acompletion", sdk)
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire(script, sends, closed)
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(module.ProviderAttemptStopped),
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert len(sends) == (0 if bypass == "no_dispatch" else 1)
    assert len(closed) == 1
    assert audit.finishes[0].disposition == "non_retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": "invalid"},
        {"error": {"message": "missing code"}},
        {"error": {"code": True}},
        {"error": {"code": "invalid"}},
        {"error": {"code": None}},
        {"error": {"code": 401}},
    ],
)
async def test_http_200_conversion_failures_are_not_confirmed_provider_500(
    body: dict[str, object],
) -> None:
    """Verify http 200 conversion failures are not confirmed provider 500."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    # Extra responses reveal accidental retries without an unrelated IndexError.
    script = [(200, body, {})] * 3
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire(script, sends, closed)
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(
            (OpenAIAPIError, httpx.HTTPError, ValidationError, ValueError)
        ) as caught,
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock, uniform=lambda _low, _high: 0.0
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert not isinstance(caught.value, ProviderTransientFailure)
    assert len(sends) == len(closed) == 1
    assert audit.finishes[0].disposition == "non_retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize("preceding_response", [False, True])
async def test_local_request_preparation_failure_has_no_confirmed_provider_status(
    preceding_response: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse fabricated SDK500 and any prior try's response after local failure."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    script = (
        [(500, {"error": {"message": "unavailable"}}, {})] if preceding_response else []
    ) + [(200, SUCCESS, {})]
    original = cast("Callable[..., httpx.Request]", httpx.AsyncClient.build_request)
    preparations = 0
    failure_preparation = 2 if preceding_response else 1

    def prepare(
        self: httpx.AsyncClient, *args: object, **kwargs: object
    ) -> httpx.Request:
        """Make only the intended preparation fail; a retry would otherwise succeed."""
        nonlocal preparations
        preparations += 1
        if preparations == failure_preparation:
            message = "local request preparation failed"
            raise RuntimeError(message)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "build_request", prepare)
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire(script, sends, closed)
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(OpenAIAPIError) as caught,
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock, uniform=lambda _low, _high: 0.0
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert not isinstance(caught.value, ProviderTransientFailure)
    assert preparations == failure_preparation
    assert len(sends) == int(preceding_response)
    assert (
        len(closed) == len(audit.starts) == len(audit.finishes) == failure_preparation
    )
    assert clock.waits == ([0.0] if preceding_response else [])
    assert audit.finishes[-1].http_status is None
    assert audit.finishes[-1].disposition == "non_retryable"
    assert audit.finishes[-1].retry_classification == "non_retryable"
    assert (
        module.classify_openrouter_failure(
            caught.value, model_id="openrouter/test-model"
        )
        is None
    )
    assert module._confirmed_status(caught.value) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reserved_body",
    [
        {"model": "other-model"},
        {"messages": [{"role": "user", "content": "changed"}]},
        {"stream": True},
    ],
)
async def test_reserved_body_identity_cannot_replace_audited_request(
    reserved_body: dict[str, object],
) -> None:
    """Reject body identity/stream replacement before any physical dispatch."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire([(200, SUCCESS, {})], sends, closed)
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(module.ProviderAttemptStopped),
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
            extra_body=reserved_body,
        )
    assert not sends
    assert not audit.starts
    assert not clock.waits
    assert not closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation_phase", ["before_action", "after_response", "pre_dispatch"]
)
async def test_mutable_openrouter_class_config_cannot_change_wire_semantics(
    mutation_phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse implicit SDK settings initially, between tries and before dispatch."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    replacement = {
        "model": "other-model",
        "messages": [{"role": "user", "content": "changed"}],
    }

    def mutate() -> None:
        """Set the SDK class knob without any production reset."""
        monkeypatch.setattr(
            litellm.OpenrouterConfig, "extra_body", replacement, raising=False
        )

    class MutatingWire(Wire):
        """Change settings only after the first actual eligible response."""

        async def handle(self, request: httpx.Request) -> httpx.Response:
            """Return real translated responses while mutating the class setting."""
            response = await super().handle(request)
            if mutation_phase == "after_response" and len(sends) == 1:
                mutate()
            return response

    if mutation_phase == "before_action":
        mutate()
    elif mutation_phase == "pre_dispatch":
        original = cast("Callable[..., httpx.Request]", httpx.AsyncClient.build_request)

        def prepare(
            self: httpx.AsyncClient, *args: object, **kwargs: object
        ) -> httpx.Request:
            """Mutate after SDK transformation but before owned dispatch validation."""
            request = original(self, *args, **kwargs)
            mutate()
            return request

        monkeypatch.setattr(httpx.AsyncClient, "build_request", prepare)
    script = [(500, {"error": {"message": "unavailable"}}, {}), (200, SUCCESS, {})]
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: MutatingWire(script, sends, closed)
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(module.ProviderAttemptStopped),
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock, uniform=lambda _low, _high: 0.0
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
            extra_body={
                "provider": {"allow_fallbacks": True},
                "reasoning": {"effort": "high"},
            },
        )
    assert vars(litellm.OpenrouterConfig)["extra_body"] is replacement
    assert len(sends) == (1 if mutation_phase == "after_response" else 0)
    assert len(closed) == (0 if mutation_phase == "before_action" else 1)
    assert len(audit.starts) == len(audit.finishes) == len(closed)
    assert clock.waits == ([0.0] if mutation_phase == "after_response" else [])
    if mutation_phase == "after_response":
        body = json.loads(sends[0].content)
        assert body["model"] == "test-model"
        assert body["messages"] == [{"role": "user", "content": "safe"}]
        assert body["provider"] == {"allow_fallbacks": True}
        assert body["reasoning"] == {"effort": "high"}
        assert audit.finishes[0].http_status == 500
        assert audit.finishes[0].disposition == "retry_scheduled"
    elif mutation_phase == "pre_dispatch":
        assert audit.finishes[0].http_status is None
        assert audit.finishes[0].disposition == "non_retryable"


@pytest.mark.asyncio
async def test_environment_base_override_cannot_send_to_another_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate the real resolved URL before any dispatch accounting or send."""
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://other.invalid/v1")
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    completion = transport_module().OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire([(200, SUCCESS, {})], sends, closed)
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(module.ProviderAttemptStopped) as caught,
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert not sends
    assert not clock.waits
    assert len(closed) == len(audit.starts) == len(audit.finishes) == 1
    assert audit.finishes[0].http_status is None
    assert audit.finishes[0].disposition == "non_retryable"
    assert module._confirmed_status(caught.value) is None


@pytest.mark.asyncio
async def test_owned_completion_setup_failure_has_authoritative_null_response() -> None:
    """No owned completion exception may claim a response before resources exist."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    setups = 0

    def setup() -> httpx.AsyncBaseTransport:
        """Expose a typed SDK500 from local setup without any actual HTTP response."""
        nonlocal setups
        setups += 1
        error = litellm.APIError(
            status_code=500,
            message="local setup failed",
            llm_provider="openrouter",
            model="test-model",
        )
        # An earlier owned try's marker cannot establish this try's response.
        setattr(error, module._OWNED_STATUS_ATTRIBUTE, 500)
        raise error

    completion = transport_module().OpenRouterOneSendCompletion(transport_factory=setup)
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(OpenAIAPIError) as caught,
    ):
        await module.RetryingOpenRouterClient(
            completion=completion, clock=clock, uniform=lambda _low, _high: 0.0
        ).acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="synthetic-key",
        )
    assert setups == len(audit.starts) == len(audit.finishes) == 1
    assert not clock.waits
    assert audit.finishes[0].http_status is None
    assert audit.finishes[0].disposition == "non_retryable"
    assert module._confirmed_status(caught.value) is None
    assert (
        module.classify_openrouter_failure(
            caught.value, model_id="openrouter/test-model"
        )
        is None
    )
