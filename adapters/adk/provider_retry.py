# adapters/adk/provider_retry.py
"""Structured OpenRouter failure classification and deterministic retry timing."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import random
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx
import litellm
from google.adk.models.lite_llm import LiteLLMClient
from litellm.exceptions import (
    APIError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.llms.openrouter.common_utils import OpenRouterException
from openai import APIError as OpenAIAPIError
from openai import APIStatusError
from pydantic import ValidationError

from services.contracts.provider_retry import (
    PROVIDER_RETRY_MAX_ELAPSED_SECONDS,
    ProviderAttemptAudit,
    ProviderAuditError,
    ProviderFailureSummary,
    ProviderRetryClock,
    ProviderRetryConfig,
    ProviderTransientFailure,
    ProviderTryAuditRecord,
)

if TYPE_CHECKING:
    from litellm import ModelResponse

_TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_OPENROUTER_HOSTS: frozenset[str] = frozenset({"openrouter.ai", "api.openrouter.ai"})
_MAX_RETRY_AFTER_LENGTH: int = 1024
_MAX_RAW_RETRY_AFTER_LENGTH: int = 2 * _MAX_RETRY_AFTER_LENGTH
_MAX_STATUS_CODE_LENGTH: int = 16
_MAX_SAFE_RETRY_AFTER_INTEGER: int = 9_007_199_254_740_991
_TRANSLATED_RESPONSE_ERRORS: tuple[type[BaseException], ...] = (
    APIError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    OpenAIAPIError,
    httpx.HTTPError,
    OpenRouterException,
)
_OWNED_STATUS_ATTRIBUTE: str = "agileforge_response_status"
_MAX_HTTP_STATUS: int = 599
_RETRY_SCHEDULED: object = object()


@dataclass(frozen=True)
class TransientProviderResponse:
    """Confirmed eligible response; raw text is never classifier evidence."""

    status: int
    retry_after_value: str | None


@dataclass(frozen=True)
class RetryAfterGuidance:
    """A provider minimum, or refusal when representing it would send early."""

    delay_seconds: float | None
    refuses_retry: bool


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend(
            linked
            for linked in (
                None if current.__suppress_context__ else current.__context__,
                current.__cause__,
                getattr(current, "error", None),
            )
            if isinstance(linked, BaseException)
        )


def _status_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) <= _MAX_STATUS_CODE_LENGTH:
        normalized = value.strip()
        if normalized.isascii() and normalized.isdecimal():
            return int(normalized)
    return None


def _http_request(error: BaseException) -> httpx.Request | None:
    request = getattr(error, "request", None)
    if isinstance(request, httpx.Request):
        return request
    response = getattr(error, "response", None)
    if isinstance(response, httpx.Response):
        try:
            return response.request
        except RuntimeError:
            return None
    return None


def _is_openrouter_response(error: BaseException) -> bool:
    if isinstance(error, _TRANSLATED_RESPONSE_ERRORS):
        return getattr(error, "llm_provider", None) == "openrouter"
    if isinstance(error, OpenRouterException):
        return True
    if isinstance(error, (httpx.HTTPStatusError, APIStatusError)):
        request = _http_request(error)
        return (
            request is not None
            and request.url.scheme == "https"
            and request.url.host in _OPENROUTER_HOSTS
        )
    return False


def _retry_after_header(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    carriers = (
        getattr(error, "litellm_response_headers", None),
        getattr(error, "headers", None),
        getattr(response, "headers", None),
    )
    for headers in carriers:
        if isinstance(headers, Mapping):
            for key, value in headers.items():
                if (
                    isinstance(key, str)
                    and key.lower() == "retry-after"
                    and isinstance(value, str)
                ):
                    return value
    return None


def classify_openrouter_failure(
    error: BaseException, *, model_id: str
) -> TransientProviderResponse | None:
    """Require a selected OpenRouter model and typed structured response evidence."""
    if not model_id.startswith("openrouter/"):
        return None
    for candidate in _exception_chain(error):
        if isinstance(candidate, (asyncio.CancelledError, ValidationError)):
            return None
        has_owned_status = hasattr(candidate, _OWNED_STATUS_ATTRIBUTE)
        if has_owned_status and getattr(candidate, _OWNED_STATUS_ATTRIBUTE) is None:
            return None
        if not isinstance(candidate, _TRANSPORT_ERRORS):
            continue
        response = getattr(candidate, "response", None)
        status = _status_code(
            getattr(
                candidate,
                _OWNED_STATUS_ATTRIBUTE if has_owned_status else "status_code",
                None,
            )
        )
        if (
            not has_owned_status
            and status is None
            and isinstance(response, httpx.Response)
        ):
            status = _status_code(response.status_code)
        if (
            not _is_openrouter_response(candidate)
            or status not in _TRANSIENT_STATUSES
            or (isinstance(candidate, Timeout) and status != HTTPStatus.GATEWAY_TIMEOUT)
        ):
            return None
        return TransientProviderResponse(
            status=status, retry_after_value=_retry_after_header(candidate)
        )
    return None


def parse_retry_after(value: str | None, *, now: datetime) -> RetryAfterGuidance:
    """Parse a bounded HTTP minimum; preserve finite guidance without clipping."""
    if value is None:
        return RetryAfterGuidance(None, False)
    normalized = value.strip() if len(value) <= _MAX_RAW_RETRY_AFTER_LENGTH else None
    if normalized is None or len(normalized) > _MAX_RETRY_AFTER_LENGTH:
        prefix = (
            value[:_MAX_RETRY_AFTER_LENGTH]
            if normalized is None
            else normalized[:_MAX_RETRY_AFTER_LENGTH]
        ).strip()
        refuses_retry = not prefix or (prefix.isascii() and prefix.isdecimal())
        return RetryAfterGuidance(None, refuses_retry)
    if normalized.isascii() and normalized.isdecimal():
        delay = (
            float(normalized)
            if int(normalized) <= _MAX_SAFE_RETRY_AFTER_INTEGER
            else math.inf
        )
    else:
        try:
            date = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return RetryAfterGuidance(None, False)
        if date.tzinfo is None or date.utcoffset() is None:
            return RetryAfterGuidance(None, False)
        delay = max(0.0, (date - now).total_seconds())
    if not math.isfinite(delay):
        return RetryAfterGuidance(None, True)
    return RetryAfterGuidance(delay, delay > PROVIDER_RETRY_MAX_ELAPSED_SECONDS)


def retry_delay(
    *,
    failed_try: int,
    config: ProviderRetryConfig,
    retry_after_seconds: float | None,
    uniform: Callable[[float, float], float],
) -> float:
    """Apply full jitter to capped backoff, then honor the provider minimum."""
    if isinstance(failed_try, bool) or failed_try < 1:
        message = "failed_try must be a positive integer"
        raise ValueError(message)
    exponent = failed_try - 1
    cap_exponent = math.log2(config.max_delay_seconds) - math.log2(
        config.base_delay_seconds
    )
    ceiling = (
        config.max_delay_seconds
        if exponent >= cap_exponent
        else min(
            config.max_delay_seconds, math.ldexp(config.base_delay_seconds, exponent)
        )
    )
    jittered = uniform(0.0, ceiling)
    return (
        max(jittered, retry_after_seconds)
        if retry_after_seconds is not None
        else jittered
    )


__all__ = [
    "ProviderActionContext",
    "ProviderAttemptStopped",
    "ProviderDeadlineExceeded",
    "RetryAfterGuidance",
    "RetryingOpenRouterClient",
    "TransientProviderResponse",
    "bind_provider_action",
    "classify_openrouter_failure",
    "get_provider_action_context",
    "parse_retry_after",
    "retry_delay",
]


class ProviderDeadlineExceeded(TimeoutError):  # noqa: N818
    """Only a boundary-owned total-duration deadline expired."""


class ProviderAttemptStopped(RuntimeError):  # noqa: N818
    """A missing, unsupported, stale or expired local host boundary."""

    def __init__(self) -> None:
        """Expose fixed local failure copy without provider details."""
        super().__init__("Provider action is unavailable or no longer valid.")


@dataclass(frozen=True, slots=True)
class ProviderActionContext:
    """Immutable host-owned identity and captured policy for model invocations."""

    project_id: int
    action_id: str
    policy: ProviderRetryConfig
    audit: ProviderAttemptAudit
    action_deadline: float
    pre_try_check: Callable[[], None]
    lease_deadline: float | None = None
    workflow_node_attempt_id: int | None = None
    attempt_fingerprint: str | None = None
    node_id: str | None = None
    instance_key: str | None = None
    idempotency_key_digest: str | None = None


_PROVIDER_ACTION: ContextVar[ProviderActionContext | None] = ContextVar(
    "agileforge_provider_action", default=None
)
_UNWIND_RESERVE_SECONDS: float = 2.0
_SEMANTIC_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "response_format",
        "extra_body",
        "reasoning_effort",
        "reasoning",
        "n",
        "logprobs",
        "top_logprobs",
        "parallel_tool_calls",
        "modalities",
        "audio",
        "prediction",
        "user",
    }
)
_TRANSPORT_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "authorization",
        "headers",
        "extra_headers",
        "credentials",
        "api_base",
        "base_url",
        "endpoint",
        "timeout",
        "client",
        "api_version",
    }
)
_GLOBAL_CONTROLS: tuple[str, ...] = (
    "num_retries",
    "num_retries_per_request",
    "retry_policy",
    "model_fallbacks",
    "max_fallbacks",
    "default_fallbacks",
    "fallbacks",
    "context_window_fallbacks",
    "content_policy_fallbacks",
    "api_base",
    "custom_llm_provider",
    "cache",
)
_REQUEST_CONTROLS: tuple[str, ...] = (
    "retry_policy",
    "fallbacks",
    "model_fallbacks",
    "max_fallbacks",
    "default_fallbacks",
    "context_window_fallbacks",
    "content_policy_fallbacks",
    "context_window_fallback_dict",
    "model_list",
    "custom_llm_provider",
    "api_base",
    "base_url",
    "client",
    "model_alias_map",
    "cache_control",
    "on_dispatch",
)


@contextmanager
def bind_provider_action(context: ProviderActionContext) -> Iterator[None]:
    """Bind a captured host action and always restore the previous binding."""
    token = _PROVIDER_ACTION.set(context)
    try:
        yield
    finally:
        _PROVIDER_ACTION.reset(token)


def get_provider_action_context() -> ProviderActionContext | None:
    """Return the immutable host context bound to the current execution."""
    return _PROVIDER_ACTION.get()


class _SystemClock:
    """Use monotonic budgets and explicit UTC audit timestamps."""

    def monotonic(self) -> float:
        """Return the process monotonic clock."""
        return time.monotonic()

    def utc_now(self) -> datetime:
        """Return an aware UTC timestamp."""
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Wait without blocking the event loop."""
        await asyncio.sleep(seconds)


async def _owned_timeout(awaitable: Awaitable[object], seconds: float) -> object:
    """Cancel and await transport cleanup under an actual total-duration cap."""
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            return await awaitable
    except TimeoutError:
        if deadline.expired():
            raise ProviderDeadlineExceeded from None
        raise


def _check_controls(model: str, request: Mapping[str, object]) -> None:
    """Reject implicit SDK settings and immutable identity replacements."""
    if not model.startswith("openrouter/"):
        raise ProviderAttemptStopped()
    extra_body = request.get("extra_body")
    if extra_body is not None and (
        not isinstance(extra_body, Mapping)
        or "model" in extra_body
        or "messages" in extra_body
        or "stream" in extra_body
    ):
        raise ProviderAttemptStopped()
    # These mutable defaults merge after capture/fingerprinting. Explicit
    # request provider routing, reasoning and generation extras remain supported.
    if litellm.OpenrouterConfig.get_config():
        raise ProviderAttemptStopped()
    if request.get("caching") is not None and request.get("caching") is not False:
        raise ProviderAttemptStopped()
    if any(getattr(litellm, knob, None) not in (None, 0) for knob in _GLOBAL_CONTROLS):
        raise ProviderAttemptStopped()
    aliases = getattr(litellm, "model_alias_map", None)
    if aliases:
        raise ProviderAttemptStopped()
    for knob in ("num_retries", "max_retries"):
        value = request.get(knob, 0)
        if type(value) is not int or value != 0:
            raise ProviderAttemptStopped()
    if any(knob in request and request[knob] is not None for knob in _REQUEST_CONTROLS):
        raise ProviderAttemptStopped()


def _semantic_value(value: object) -> object:
    """Semantic value."""
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_value(item)
            for key, item in value.items()
            if str(key).lower() not in _TRANSPORT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    schema = getattr(value, "model_json_schema", None)
    if callable(schema):
        return _semantic_value(schema())
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _semantic_value(dump(mode="json"))
    raise ProviderAttemptStopped()


def _request_fingerprint(request: Mapping[str, object]) -> str:
    """Request fingerprint."""
    semantic = {
        key: _semantic_value(value)
        for key, value in request.items()
        if key in _SEMANTIC_KEYS
    }
    payload = json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _remaining(
    context: ProviderActionContext,
    clock: ProviderRetryClock,
    retry_deadline: float | None,
) -> float:
    """Remaining."""
    deadlines = [context.action_deadline - _UNWIND_RESERVE_SECONDS]
    if context.lease_deadline is not None:
        deadlines.append(context.lease_deadline)
    if retry_deadline is not None:
        deadlines.append(retry_deadline)
    if not all(math.isfinite(deadline) for deadline in deadlines):
        raise ProviderAttemptStopped()
    return min(deadlines) - clock.monotonic()


def _confirmed_status(error: BaseException) -> int | None:
    """Resolve only the effective current transport status."""
    for candidate in _exception_chain(error):
        if hasattr(candidate, _OWNED_STATUS_ATTRIBUTE):
            return _status_code(getattr(candidate, _OWNED_STATUS_ATTRIBUTE))
        if isinstance(candidate, (ValidationError, asyncio.CancelledError)):
            return None
        if isinstance(candidate, _TRANSPORT_ERRORS):
            response = getattr(candidate, "response", None)
            status = _status_code(getattr(candidate, "status_code", None))
            if status is None and isinstance(response, httpx.Response):
                status = response.status_code
            return (
                status
                if status is not None
                and HTTPStatus.CONTINUE <= status <= _MAX_HTTP_STATUS
                else None
            )
    return None


@dataclass
class _ProviderCall:
    """Mutable invocation-local state; never retained on the shared client."""

    context: ProviderActionContext
    clock: ProviderRetryClock
    call_id: str
    model_id: str
    fingerprint: str
    sends: int = 0
    retry_deadline: float | None = None
    last_response: TransientProviderResponse | None = None
    last_guidance: float | None = None

    def started(self, ordinal: int) -> ProviderTryAuditRecord:
        """Create a frozen start from the captured host identity."""
        context = self.context
        return ProviderTryAuditRecord(
            project_id=context.project_id,
            action_id=context.action_id,
            call_id=self.call_id,
            workflow_node_attempt_id=context.workflow_node_attempt_id,
            attempt_fingerprint=context.attempt_fingerprint,
            node_id=context.node_id,
            instance_key=context.instance_key,
            idempotency_key_digest=context.idempotency_key_digest,
            model_id=self.model_id,
            request_fingerprint=self.fingerprint,
            try_ordinal=ordinal,
            max_attempts=context.policy.max_attempts,
            retry_config=context.policy,
            started_at=self.clock.utc_now(),
        )

    def finish(
        self, start: ProviderTryAuditRecord, start_time: float, **facts: object
    ) -> None:
        """Commit truthful finish facts before wait, retry or publication."""
        record = ProviderTryAuditRecord.model_validate(
            start.model_dump()
            | {
                "finished_at": self.clock.utc_now(),
                "duration_seconds": max(0.0, self.clock.monotonic() - start_time),
            }
            | facts
        )
        self.context.audit.append_finished(record)

    def exhausted(self, reason: str) -> ProviderTransientFailure:
        """Exhausted."""
        response = self.last_response
        if response is None:
            raise ProviderAttemptStopped()
        summary = ProviderFailureSummary.model_validate(
            {
                "reason": "rate_limited"
                if response.status == HTTPStatus.TOO_MANY_REQUESTS
                else "unavailable",
                "termination_reason": reason,
                "http_status": response.status,
                "call_id": self.call_id,
                "attempts": self.sends,
                "max_attempts": self.context.policy.max_attempts,
                "retry_after_seconds": self.last_guidance,
                "manual_retry_requires_new_key": self.context.idempotency_key_digest
                is not None,
            }
        )
        recorded = self.context.audit.terminal_failure(
            project_id=self.context.project_id,
            action_id=self.context.action_id,
            call_id=self.call_id,
            expected_summary=summary,
        )
        if recorded != summary:
            raise ProviderAuditError()
        return ProviderTransientFailure(summary)


@dataclass
class _TryDispatch:
    """Invocation-local notification of one real physical transport send."""

    call: _ProviderCall
    ordinal: int
    request: Mapping[str, object]
    dispatched: bool = False

    def __call__(self) -> None:
        """Check current host/SDK controls, then reserve exactly one dispatch."""
        if self.dispatched:
            raise ProviderAttemptStopped()
        context = self.call.context
        context.pre_try_check()
        _check_controls(self.call.model_id, self.request)
        remaining = _remaining(context, self.call.clock, self.call.retry_deadline)
        minimum = context.policy.min_remaining_seconds if self.ordinal > 1 else 0.0
        if remaining <= 0 or remaining < minimum:
            raise ProviderDeadlineExceeded
        self.dispatched = True
        self.call.sends += 1

    def require_dispatched(self) -> None:
        """Withhold a response returned without a physical transport send."""
        if not self.dispatched:
            raise ProviderAttemptStopped()


class RetryingOpenRouterClient(LiteLLMClient):
    """One audited transport attempt per bounded retry, bound to the host action."""

    def __init__(
        self,
        *,
        completion: Callable[..., Awaitable[object]] | None = None,
        clock: ProviderRetryClock | None = None,
        uniform: Callable[[float, float], float] = random.uniform,
        timeout_runner: Callable[
            [Awaitable[object], float], Awaitable[object]
        ] = _owned_timeout,
    ) -> None:
        """Inject deterministic seams without retaining action or event-loop state."""
        if completion is None:
            # Deferred import breaks the transport's structured-evidence dependency.
            from adapters.adk.provider_transport import (  # noqa: PLC0415
                OpenRouterOneSendCompletion,
            )

            completion = OpenRouterOneSendCompletion()
        self._completion: Callable[..., Awaitable[object]] = completion
        self._clock = clock if clock is not None else _SystemClock()
        self._uniform = uniform
        self._timeout_runner = timeout_runner

    def completion(
        self,
        model: object,
        messages: object,
        tools: object,
        stream: bool = False,
        **kwargs: object,
    ) -> ModelResponse:
        """Unsupported synchronous/streaming routes cannot bypass the boundary."""
        del model, messages, tools, stream, kwargs
        raise ProviderAttemptStopped()

    async def acompletion(
        self, model: str, messages: object, tools: object, **kwargs: object
    ) -> ModelResponse:
        """Execute a captured, immutable request under action/count/time bounds."""
        context = _PROVIDER_ACTION.get()
        if context is None or kwargs.get("stream"):
            raise ProviderAttemptStopped()
        request: dict[str, object] = copy.deepcopy(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                **kwargs,
            }
        )
        _check_controls(model, request)
        request.update(num_retries=0, max_retries=0, caching=False)
        call = _ProviderCall(
            context, self._clock, uuid4().hex, model, _request_fingerprint(request)
        )
        existing_timeout = request.get(
            "timeout", getattr(litellm, "request_timeout", 600.0)
        )
        if (
            isinstance(existing_timeout, bool)
            or not isinstance(existing_timeout, (float, int))
            or not math.isfinite(existing_timeout)
            or existing_timeout <= 0
        ):
            raise ProviderAttemptStopped()
        for ordinal in range(1, context.policy.max_attempts + 1):
            start, start_time, remaining = self._reserve_try(call, ordinal, request)
            result = await self._execute_try(
                call, start, start_time, request, (float(existing_timeout), remaining)
            )
            if result is not _RETRY_SCHEDULED:
                return cast("ModelResponse", result)
        raise ProviderAttemptStopped()

    def _reserve_try(
        self, call: _ProviderCall, ordinal: int, request: Mapping[str, object]
    ) -> tuple[ProviderTryAuditRecord, float, float]:
        """Reserve try."""
        context = call.context
        minimum = context.policy.min_remaining_seconds if ordinal > 1 else 0.0
        context.pre_try_check()
        remaining = _remaining(context, self._clock, call.retry_deadline)
        if remaining <= 0 or remaining < minimum:
            self._budget_stop(call)
        _check_controls(call.model_id, request)
        start_time = self._clock.monotonic()
        start = call.started(ordinal)
        context.audit.append_started(start)
        try:
            context.pre_try_check()
            _check_controls(call.model_id, request)
        except ProviderAttemptStopped:
            call.finish(
                start,
                start_time,
                disposition="cancelled",
                retry_classification="cancelled",
            )
            raise
        remaining = _remaining(context, self._clock, call.retry_deadline)
        if remaining <= 0 or remaining < minimum:
            if call.sends:
                call.finish(
                    start,
                    start_time,
                    disposition="not_sent",
                    termination_reason="retry_budget_exhausted",
                )
            self._budget_stop(call)
        return start, start_time, remaining

    @staticmethod
    def _budget_stop(call: _ProviderCall) -> None:
        if call.sends:
            reason = "retry_budget_exhausted"
            raise call.exhausted(reason) from None
        raise ProviderAttemptStopped()

    async def _execute_try(
        self,
        call: _ProviderCall,
        start: ProviderTryAuditRecord,
        start_time: float,
        request: dict[str, object],
        limits: tuple[float, float],
    ) -> object:
        existing_timeout, remaining = limits
        timeout = min(existing_timeout, remaining)
        previous_sends = call.sends
        dispatch = _TryDispatch(call, start.try_ordinal, request)
        owns_dispatch = getattr(self._completion, "owns_dispatch_notifications", False)

        send_request = copy.deepcopy(request) | {"timeout": timeout}
        if owns_dispatch:
            send_request["on_dispatch"] = dispatch
        try:
            if not owns_dispatch:
                # Injected completion seam promises one invocation = one send.
                dispatch()
            result = await self._timeout_runner(
                self._completion(**send_request),
                timeout,
            )
            dispatch.require_dispatched()
        except asyncio.CancelledError:
            call.finish(
                start,
                start_time,
                disposition="cancelled",
                retry_classification="cancelled",
            )
            raise
        except ProviderDeadlineExceeded:
            if (
                start.try_ordinal > 1
                and call.last_response is not None
                and remaining <= existing_timeout
            ):
                reason = "retry_budget_exhausted"
                call.finish(
                    start,
                    start_time,
                    disposition="not_sent"
                    if call.sends == previous_sends
                    else "exhausted",
                    termination_reason=reason,
                )
                raise call.exhausted(reason) from None
            call.finish(
                start,
                start_time,
                disposition="non_retryable",
                retry_classification="non_retryable",
            )
            raise
        except Exception as error:
            eligible = classify_openrouter_failure(error, model_id=call.model_id)
            if eligible is None:
                call.finish(
                    start,
                    start_time,
                    disposition="non_retryable",
                    retry_classification="non_retryable",
                    http_status=_confirmed_status(error),
                )
                raise
            await self._schedule_retry(call, start, start_time, eligible)
            return _RETRY_SCHEDULED
        call.finish(start, start_time, disposition="success")
        call.context.pre_try_check()
        if _remaining(call.context, self._clock, call.retry_deadline) <= 0:
            raise ProviderAttemptStopped()
        return result

    async def _schedule_retry(
        self,
        call: _ProviderCall,
        start: ProviderTryAuditRecord,
        start_time: float,
        eligible: TransientProviderResponse,
    ) -> None:
        context = call.context
        if call.retry_deadline is None:
            call.retry_deadline = (
                self._clock.monotonic() + context.policy.max_elapsed_seconds
            )
        guidance = parse_retry_after(
            eligible.retry_after_value, now=self._clock.utc_now()
        )
        call.last_response, call.last_guidance = eligible, guidance.delay_seconds
        delay = retry_delay(
            failed_try=start.try_ordinal,
            config=context.policy,
            retry_after_seconds=guidance.delay_seconds,
            uniform=self._uniform,
        )
        remaining = _remaining(context, self._clock, call.retry_deadline)
        reason = self._termination_reason(call, start, guidance, delay, remaining)
        facts: dict[str, object] = {
            "http_status": eligible.status,
            "retry_classification": "rate_limited"
            if eligible.status == HTTPStatus.TOO_MANY_REQUESTS
            else "unavailable",
            "retry_after_seconds": guidance.delay_seconds,
        }
        if reason is not None:
            call.finish(
                start,
                start_time,
                **facts,
                disposition="exhausted",
                termination_reason=reason,
            )
            context.pre_try_check()
            raise call.exhausted(reason) from None
        call.finish(
            start,
            start_time,
            **facts,
            disposition="retry_scheduled",
            selected_delay_seconds=delay,
            next_eligible_retry_at=self._clock.utc_now() + timedelta(seconds=delay),
        )
        context.pre_try_check()
        if (
            _remaining(context, self._clock, call.retry_deadline) - delay
            < context.policy.min_remaining_seconds
        ):
            self._budget_stop(call)
        await self._clock.sleep(delay)
        context.pre_try_check()
        if (
            _remaining(context, self._clock, call.retry_deadline)
            < context.policy.min_remaining_seconds
        ):
            self._budget_stop(call)

    @staticmethod
    def _termination_reason(
        call: _ProviderCall,
        start: ProviderTryAuditRecord,
        guidance: RetryAfterGuidance,
        delay: float,
        remaining: float,
    ) -> str | None:
        if start.try_ordinal == call.context.policy.max_attempts:
            return "attempts_exhausted"
        if guidance.refuses_retry or (
            guidance.delay_seconds is not None
            and remaining - delay < call.context.policy.min_remaining_seconds
        ):
            return "retry_after_exceeds_budget"
        if remaining - delay < call.context.policy.min_remaining_seconds:
            return "retry_budget_exhausted"
        return None
