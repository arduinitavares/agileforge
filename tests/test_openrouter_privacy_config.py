"""Tests for OpenRouter privacy routing configuration."""

import pytest

from utils.model_config import (
    OPENROUTER_PRIVACY_ERROR_MESSAGE,
    OPENROUTER_PROVIDER,
    ZDR_MAX_BACKOFF_SECONDS,
    ZDR_MAX_RETRIES,
    get_model_token_limit_args,
    get_openrouter_extra_body,
    is_zdr_routing_error,
)


def test_gpt_5_openrouter_models_use_max_completion_tokens() -> None:
    """Strict GPT-5 routing should retain Luna's supported token parameter."""
    assert get_model_token_limit_args("openrouter/openai/gpt-5.6-luna", 4096) == {
        "max_completion_tokens": 4096
    }


def test_non_gpt_5_openrouter_models_keep_max_tokens() -> None:
    """The inexpensive test model should keep its supported token parameter."""
    assert get_model_token_limit_args("openrouter/openai/gpt-oss-20b:free", 4096) == {
        "max_tokens": 4096
    }


def test_openrouter_extra_body_includes_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra body should include a provider object with strict privacy controls."""
    monkeypatch.setenv("RELAX_ZDR_FOR_TESTS", "false")
    extra_body = get_openrouter_extra_body()
    assert extra_body["provider"] == OPENROUTER_PROVIDER
    assert extra_body["provider"] is not OPENROUTER_PROVIDER


def test_openrouter_privacy_error_message_is_stable() -> None:
    """Privacy error message should be explicit and stable for routing failures."""
    assert (
        OPENROUTER_PRIVACY_ERROR_MESSAGE
        == "No ZDR/data_collection=deny provider available for this model"
    )


def test_is_zdr_routing_error_detects_zdr_errors() -> None:
    """is_zdr_routing_error should detect ZDR/privacy routing failures."""
    # Should detect
    assert is_zdr_routing_error(Exception("No ZDR provider available"))
    assert is_zdr_routing_error(Exception("data_collection=deny not supported"))
    assert is_zdr_routing_error(Exception("No providers available for this model"))
    assert is_zdr_routing_error(
        Exception("provider unavailable, no matching providers")
    )

    # Should NOT detect (unrelated errors)
    assert not is_zdr_routing_error(Exception("Connection timeout"))
    assert not is_zdr_routing_error(Exception("Invalid API key"))
    assert not is_zdr_routing_error(ValueError("Bad input"))


def test_zdr_retry_constants_are_reasonable() -> None:
    """ZDR retry constants should have sensible defaults."""
    assert ZDR_MAX_RETRIES >= 3  # At least 3 retries  # noqa: PLR2004
    assert ZDR_MAX_RETRIES <= 10  # Not too many  # noqa: PLR2004
    assert ZDR_MAX_BACKOFF_SECONDS >= 5.0  # At least 5s max backoff  # noqa: PLR2004
    assert ZDR_MAX_BACKOFF_SECONDS <= 30.0  # Not too long  # noqa: PLR2004
