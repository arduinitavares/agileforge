# tests/adapters/test_provider_retry.py
"""Provider-free retry classification, guidance, and policy contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from google.adk.workflow._errors import DynamicNodeFailError
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.llms.openrouter.common_utils import OpenRouterException
from pydantic import BaseModel, ValidationError

from adapters.adk import provider_retry as policy
from services.contracts import provider_retry as contracts

_MODEL_ID: str = "openrouter/synthetic/test-model"
_NOW: datetime = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _api_error(status: object, *, provider: str = "openrouter") -> APIError:
    error = APIError(
        status_code=500,
        message=(
            "OpenrouterException - The server had an error processing your request."
        ),
        llm_provider=provider,
        model="synthetic/test-model",
    )
    # The pinned SDK forwards HTTP-200 error.code, including strings and None.
    setattr(error, "status_code", status)  # noqa: B010
    return error


@pytest.mark.parametrize(
    "status", [429, 500, 502, 503, 504, "429", "500", "502", "503", "504"]
)
def test_classifies_only_structured_openrouter_transient_status(status: object) -> None:
    """Catch missing eligible statuses or failure to normalize numeric strings."""
    result = policy.classify_openrouter_failure(_api_error(status), model_id=_MODEL_ID)

    assert result is not None
    assert result.status == int(str(status))
    assert result.retry_after_value is None


@pytest.mark.parametrize(
    "status",
    [400, 401, 403, 402, 404, 408, 409, 422, 501, None, "bad", True, False, 500.0],
)
def test_excludes_nontransient_or_unstructured_api_error(status: object) -> None:
    """Catch retries based on an APIError class or a misleading error message."""
    assert (
        policy.classify_openrouter_failure(_api_error(status), model_id=_MODEL_ID)
        is None
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "", None])
def test_rejects_foreign_or_unknown_translated_provider(provider: str | None) -> None:
    """The selected OpenRouter model alone cannot prove the error's provider."""
    error = _api_error(500)
    setattr(error, "llm_provider", provider)  # noqa: B010
    assert policy.classify_openrouter_failure(error, model_id=_MODEL_ID) is None


def test_rejects_non_openrouter_selected_model() -> None:
    """Catch classification of a transport unrelated to the selected model."""
    assert (
        policy.classify_openrouter_failure(_api_error(500), model_id="openai/test")
        is None
    )


@pytest.mark.parametrize("status", [408, None, 429, 500, 502, 503])
def test_timeout_without_confirmed_504_is_not_transient(status: int | None) -> None:
    """An ordinary timeout must retain its existing infrastructure path."""
    error = Timeout(message="timed out", model="test", llm_provider="openrouter")
    setattr(error, "status_code", status)  # noqa: B010
    assert policy.classify_openrouter_failure(error, model_id=_MODEL_ID) is None


def test_timeout_504_preserves_litellm_retry_after_header() -> None:
    """Catch loss of the pinned 504 Timeout's separately attached headers."""
    error = Timeout(
        message="timed out",
        model="test",
        llm_provider="openrouter",
        exception_status_code=504,
    )
    setattr(error, "litellm_response_headers", {"rEtRy-AfTeR": "17"})  # noqa: B010
    result = policy.classify_openrouter_failure(error, model_id=_MODEL_ID)

    assert result is not None
    assert (result.status, result.retry_after_value) == (504, "17")


@pytest.mark.parametrize("error_type", [RateLimitError, ServiceUnavailableError])
def test_translated_response_error_keeps_case_insensitive_guidance(
    error_type: type[RateLimitError] | type[ServiceUnavailableError],
) -> None:
    """Catch loss of guidance carried by the original HTTP response."""
    response = httpx.Response(
        429 if error_type is RateLimitError else 503,
        headers={"RETRY-AFTER": "4"},
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
    )
    error = error_type(
        message="unavailable",
        llm_provider="openrouter",
        model="test",
        response=response,
    )
    if error_type is ServiceUnavailableError:
        # Its pinned constructor discards response; the mapper attaches headers.
        setattr(error, "litellm_response_headers", dict(response.headers))  # noqa: B010
    result = policy.classify_openrouter_failure(error, model_id=_MODEL_ID)

    assert result is not None
    assert (result.status, result.retry_after_value) == (response.status_code, "4")


@pytest.mark.parametrize(
    "host",
    [
        "openrouter.ai",
        "api.openrouter.ai",
        "api.openai.com",
        "openrouter.ai.attacker.invalid",
    ],
)
def test_http_status_error_requires_openrouter_origin(host: str) -> None:
    """Catch missing typed HTTP support or weak hostname substring matching."""
    request = httpx.Request("POST", f"https://{host}/api/v1/chat/completions")
    response = httpx.Response(502, headers={"Retry-After": "5"}, request=request)
    error = httpx.HTTPStatusError("bad gateway", request=request, response=response)
    result = policy.classify_openrouter_failure(error, model_id=_MODEL_ID)

    if host in {"openrouter.ai", "api.openrouter.ai"}:
        assert result is not None
        assert (result.status, result.retry_after_value) == (502, "5")
    else:
        assert result is None


def test_openrouter_translation_exception_is_structured_provider_evidence() -> None:
    """The pinned translation class carries provider identity independently of text."""
    error = OpenRouterException(status_code=500, message="unavailable")
    result = policy.classify_openrouter_failure(error, model_id=_MODEL_ID)
    assert result is not None
    assert (result.status, result.retry_after_value) == (500, None)


def test_connection_error_is_not_a_confirmed_provider_response() -> None:
    """LiteLLM assigns connection errors 500; that must not trigger retries."""
    error = APIConnectionError(
        message="connection failed", llm_provider="openrouter", model="test"
    )
    assert policy.classify_openrouter_failure(error, model_id=_MODEL_ID) is None


@pytest.mark.parametrize(
    "message", ["OpenRouter 429 Retry-After: 8", "APIError OpenrouterException 500"]
)
def test_generic_exception_text_cannot_enable_retry(message: str) -> None:
    """Catch parsing of provider identity, status, or guidance from raw text."""
    assert (
        policy.classify_openrouter_failure(RuntimeError(message), model_id=_MODEL_ID)
        is None
    )


@pytest.mark.parametrize("wrapper_kind", ["cause", "context", "adk"])
def test_traverses_real_wrappers_without_parsing_wrapper_text(
    wrapper_kind: str,
) -> None:
    """Catch loss of a transport failure under supported exception wrappers."""
    original = _api_error("502")
    setattr(original, "litellm_response_headers", {"Retry-After": "6"})  # noqa: B010
    if wrapper_kind == "adk":
        wrapped = DynamicNodeFailError(
            message="node failed", error=original, error_node_path="test.node"
        )
    else:
        wrapped = RuntimeError("node failed")
        setattr(wrapped, f"__{wrapper_kind}__", original)

    result = policy.classify_openrouter_failure(wrapped, model_id=_MODEL_ID)
    assert result is not None
    assert (result.status, result.retry_after_value) == (502, "6")


def test_cyclic_exception_chain_terminates_without_fabricating_status() -> None:
    """Catch unbounded cause traversal and text-based fallback on cycles."""
    first = RuntimeError("500")
    second = RuntimeError("429")
    first.__cause__ = second
    second.__context__ = first
    assert policy.classify_openrouter_failure(first, model_id=_MODEL_ID) is None


@pytest.mark.parametrize(
    "status", [400, 401, 403, 402, 404, 408, 409, 422, 501, None, "bad"]
)
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_current_api_error_exclusion_is_decisive_over_previous_transient(
    status: object,
    link: str,
) -> None:
    """A prior caught outage cannot turn the current response into a retry."""
    current = _api_error(status)
    setattr(current, link, _api_error(500))
    assert policy.classify_openrouter_failure(current, model_id=_MODEL_ID) is None


@pytest.mark.parametrize("status", [408, None])
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_current_timeout_exclusion_is_decisive_over_previous_transient(
    status: int | None,
    link: str,
) -> None:
    """A previous 500 cannot reclassify a current ordinary or unknown timeout."""
    current = Timeout(message="timed out", model="test", llm_provider="openrouter")
    setattr(current, "status_code", status)  # noqa: B010
    setattr(current, link, _api_error(500))
    assert policy.classify_openrouter_failure(current, model_id=_MODEL_ID) is None


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_current_connection_error_exclusion_is_decisive_over_previous_transient(
    link: str,
) -> None:
    """A current connection failure must not reuse a previous provider status."""
    current = APIConnectionError(
        message="connection failed", llm_provider="openrouter", model="test"
    )
    setattr(current, link, _api_error(500))
    assert policy.classify_openrouter_failure(current, model_id=_MODEL_ID) is None


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_current_confirmed_transient_is_not_vetoed_by_previous_timeout(
    link: str,
) -> None:
    """An older 408 cannot suppress the current confirmed OpenRouter 500."""
    current = _api_error(500)
    previous = Timeout(message="old timeout", model="test", llm_provider="openrouter")
    setattr(current, link, previous)
    result = policy.classify_openrouter_failure(current, model_id=_MODEL_ID)
    assert result is not None
    assert (result.status, result.retry_after_value) == (500, None)


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_current_confirmed_transient_does_not_inherit_previous_guidance(
    link: str,
) -> None:
    """Guidance belongs to the current provider response, not a caught old 429."""
    previous = _api_error(429)
    setattr(previous, "litellm_response_headers", {"Retry-After": "17"})  # noqa: B010
    current = _api_error(500)
    setattr(current, link, previous)
    result = policy.classify_openrouter_failure(current, model_id=_MODEL_ID)
    assert result is not None
    assert (result.status, result.retry_after_value) == (500, None)


def test_suppressed_historical_context_cannot_supply_provider_evidence() -> None:
    """An explicit raise-from-None boundary cannot resurrect a caught outage."""
    current = RuntimeError("current unrelated failure")
    current.__context__ = _api_error(500)
    current.__suppress_context__ = True
    assert policy.classify_openrouter_failure(current, model_id=_MODEL_ID) is None


@pytest.mark.parametrize("provider", ["anthropic", "openai", "", None])
def test_current_foreign_or_unknown_provider_is_decisive_over_previous_transient(
    provider: str | None,
) -> None:
    """A current foreign typed failure cannot inherit earlier OpenRouter identity."""
    current = _api_error(500)
    setattr(current, "llm_provider", provider)  # noqa: B010
    current.__context__ = _api_error(500)
    assert policy.classify_openrouter_failure(current, model_id=_MODEL_ID) is None


def test_pydantic_validation_and_cancellation_are_never_transient() -> None:
    """A downstream validation or cancelled action cannot restart transport."""

    class RequiredOutput(BaseModel):
        value: int

    with pytest.raises(ValidationError) as caught:
        RequiredOutput.model_validate({"value": "invalid"})
    caught.value.__cause__ = _api_error(500)
    cancelled = asyncio.CancelledError()
    cancelled.__cause__ = _api_error(500)
    assert policy.classify_openrouter_failure(caught.value, model_id=_MODEL_ID) is None
    assert policy.classify_openrouter_failure(cancelled, model_id=_MODEL_ID) is None


@pytest.mark.parametrize(
    ("value", "delay", "refuses"),
    [
        (None, None, False),
        ("0", 0.0, False),
        (" 17 ", 17.0, False),
        ("Wed, 07 Oct 2026 12:00:20 GMT", 20.0, False),
        ("Wed, 07 Oct 2026 11:59:59 GMT", 0.0, False),
        ("-1", None, False),
        ("1.5", None, False),
        ("NaN", None, False),
        ("inf", None, False),
        ("nonsense", None, False),
        ("Wed, 07 Oct 2026 12:00:20", None, False),
        ("999999999999", 999999999999.0, True),
        ("9" * 400, None, True),
        ("9" * 10000, None, True),
        ("x" * 10000, None, False),
    ],
    ids=[
        "absent",
        "zero",
        "delta",
        "future-date",
        "past-date",
        "negative",
        "fractional",
        "nan",
        "infinity",
        "malformed",
        "naive-date",
        "huge-finite",
        "unrepresentable-integer",
        "oversized-integer",
        "oversized-malformed",
    ],
)
def test_retry_after_parsing_preserves_minimum_or_refuses_unsafe_guidance(
    value: str | None, delay: float | None, refuses: bool
) -> None:
    """Catch early retries, malformed guidance adoption, clipping, or huge parsing."""
    guidance = policy.parse_retry_after(value, now=_NOW)
    assert (guidance.delay_seconds, guidance.refuses_retry) == (delay, refuses)


@pytest.mark.parametrize(
    ("value", "delay"),
    [
        ("9007199254740991", 9007199254740991.0),
        ("9007199254740992", None),
        ("9007199254740993", None),
    ],
)
def test_retry_after_integer_guidance_never_projects_rounded_json_numbers(
    value: str, delay: float | None
) -> None:
    """Refuse values above the exact JSON integer bound without rounding them."""
    guidance = policy.parse_retry_after(value, now=_NOW)
    assert (guidance.delay_seconds, guidance.refuses_retry) == (delay, True)


@pytest.mark.parametrize(
    ("value", "delay", "refuses"),
    [
        (" " * 1024 + "121", 121.0, True),
        (" " * 512 + "121" + " " * 512, 121.0, True),
        (" " * 1024 + "60", 60.0, False),
        (" " * 10000 + "121", None, True),
        (" " * 1024 + "9007199254740992", None, True),
        ("x" * 10000, None, False),
    ],
    ids=[
        "leading-padding",
        "both-padding",
        "usable-padded-minimum",
        "ambiguous-oversized-padding",
        "unsafe-integer-with-padding",
        "clearly-malformed-oversized",
    ],
)
def test_retry_after_padding_never_causes_early_retry_fallback(
    value: str, delay: float | None, refuses: bool
) -> None:
    """Normalize bounded padding and refuse undecidable oversized guidance."""
    guidance = policy.parse_retry_after(value, now=_NOW)
    assert (guidance.delay_seconds, guidance.refuses_retry) == (delay, refuses)


@pytest.mark.parametrize(
    ("value", "delay", "refuses"),
    [
        ("121" + " " * 2045, 121.0, True),
        ("121" + " " * 2046, None, True),
        (" " * 512 + "121" + " " * 10000, None, True),
        ("121\t" + " " * 10000, None, True),
        ("9007199254740992" + " " * 10000, None, True),
        ("121x" + " " * 10000, None, False),
        ("12 1" + " " * 10000, None, False),
    ],
    ids=[
        "trailing-padding-at-raw-cutoff",
        "trailing-padding-over-raw-cutoff",
        "mixed-oversized-padding",
        "tab-and-space-trailing-padding",
        "unsafe-integer-trailing-padding",
        "malformed-suffix",
        "malformed-interior-space",
    ],
)
def test_oversized_numeric_prefix_with_trailing_padding_refuses_retry(
    value: str, delay: float | None, refuses: bool
) -> None:
    """The raw-length cutoff cannot turn a padded minimum into fallback."""
    guidance = policy.parse_retry_after(value, now=_NOW)
    assert (guidance.delay_seconds, guidance.refuses_retry) == (delay, refuses)


@pytest.mark.parametrize(
    ("failed_try", "ceiling"), [(1, 1.0), (2, 2.0), (4, 8.0), (10, 8.0), (10000, 8.0)]
)
@pytest.mark.parametrize("sample", [0.0, 0.5, 1.0])
def test_backoff_uses_full_jitter_and_bounded_exponential_ceiling(
    failed_try: int, ceiling: float, sample: float
) -> None:
    """Catch equal jitter, a wrong exponent, lost cap, or overflowing exponent."""
    config = contracts.ProviderRetryConfig()

    def uniform(low: float, high: float) -> float:
        assert (low, high) == (0.0, ceiling)
        return sample * high

    delay = policy.retry_delay(
        failed_try=failed_try, config=config, retry_after_seconds=None, uniform=uniform
    )
    assert delay == sample * ceiling


@pytest.mark.parametrize(
    ("guidance", "expected"), [(0.25, 0.5), (4.0, 4.0), (100.0, 100.0)]
)
def test_retry_after_is_a_minimum_and_is_not_clipped_to_backoff_cap(
    guidance: float, expected: float
) -> None:
    """Catch choosing the shorter guidance or clipping it to the jitter cap."""
    assert (
        policy.retry_delay(
            failed_try=1,
            config=contracts.ProviderRetryConfig(),
            retry_after_seconds=guidance,
            uniform=lambda low, high: (low + high) / 2,
        )
        == expected
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_attempts": True},
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"max_attempts": 1.5},
        {"base_delay_seconds": True},
        {"base_delay_seconds": 0.0},
        {"base_delay_seconds": 61.0},
        {"base_delay_seconds": float("nan")},
        {"max_delay_seconds": float("inf")},
        {"max_delay_seconds": 0.5},
        {"max_delay_seconds": 61.0},
        {"max_elapsed_seconds": 0.0},
        {"max_elapsed_seconds": 121.0},
        {"min_remaining_seconds": 0.0},
        {"min_remaining_seconds": 61.0},
        {"min_remaining_seconds": False},
    ],
)
def test_retry_configuration_rejects_unsafe_values(
    overrides: dict[str, object],
) -> None:
    """Catch coercion of bools, nonfinite values, or invalid related budgets."""
    with pytest.raises(ValidationError):
        contracts.ProviderRetryConfig.model_validate(overrides)


def test_retry_configuration_is_frozen() -> None:
    """An action's captured retry policy cannot be mutated mid-call."""
    config = contracts.ProviderRetryConfig()
    with pytest.raises((ValidationError, AttributeError)):
        config.max_attempts = 8


def test_failure_summary_is_closed_consistent_and_safe() -> None:
    """Catch leaked raw provider fields or contradictory terminal summaries."""
    payload = {
        "reason": "rate_limited",
        "termination_reason": "attempts_exhausted",
        "http_status": 429,
        "call_id": "test-call-1",
        "attempts": 3,
        "max_attempts": 3,
        "retry_after_seconds": None,
        "manual_retry_requires_new_key": True,
    }
    summary = contracts.ProviderFailureSummary.model_validate(payload)
    assert summary.model_dump() == {
        "schema_version": "agileforge.provider-failure.v1",
        "provider": "openrouter",
        "category": "external_temporary",
        "retryable": True,
        **payload,
    }
    with pytest.raises(ValidationError):
        contracts.ProviderFailureSummary.model_validate(
            {**payload, "raw_message": "secret"}
        )
    with pytest.raises(ValidationError):
        contracts.ProviderFailureSummary.model_validate({**payload, "http_status": 500})
    with pytest.raises(ValidationError):
        contracts.ProviderFailureSummary.model_validate({**payload, "attempts": 4})
    with pytest.raises(ValidationError):
        contracts.ProviderFailureSummary.model_validate(
            {**payload, "retry_after_seconds": float("inf")}
        )


@pytest.mark.parametrize(
    ("status", "reason", "keyed", "expected"),
    [
        (
            429,
            "rate_limited",
            True,
            "OpenRouter is temporarily rate-limited. Automatic retries stopped. "
            "Retry this action with a new idempotency key.",
        ),
        (
            503,
            "unavailable",
            True,
            "OpenRouter is temporarily unavailable. Automatic retries stopped. "
            "Retry this action with a new idempotency key.",
        ),
        (
            429,
            "rate_limited",
            False,
            "OpenRouter is temporarily rate-limited. Automatic retries stopped. "
            "Retry this action later.",
        ),
        (
            500,
            "unavailable",
            False,
            "OpenRouter is temporarily unavailable. Automatic retries stopped. "
            "Retry this action later.",
        ),
    ],
)
def test_terminal_failure_message_uses_only_sanitized_summary(
    status: int, reason: str, keyed: bool, expected: str
) -> None:
    """Catch unsafe raw text or a wrong retry instruction at the shared seam."""
    summary = contracts.ProviderFailureSummary.model_validate(
        {
            "http_status": status,
            "reason": reason,
            "termination_reason": "attempts_exhausted",
            "call_id": "test-call",
            "attempts": 3,
            "max_attempts": 3,
            "retry_after_seconds": None,
            "manual_retry_requires_new_key": keyed,
        }
    )
    assert contracts.provider_failure_message(summary) == expected
    failure = contracts.ProviderTransientFailure(summary)
    assert failure.summary is summary
    assert str(failure) == expected
