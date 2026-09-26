"""Raw Vision response classification without a provider call."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, cast

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adapters.adk.agents.vision import validate_primary_response
from adapters.adk.errors import VisionOutputValidationError
from adapters.adk.vision_output import validate_vision_response
from tests.adapters.test_vision_recipe import _draft

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext

    from workflow.contracts import JsonObject


@pytest.mark.parametrize(
    ("text", "finish_reason", "expected_code"),
    [
        ('{"schema_version":', "MAX_TOKENS", "VISION_OUTPUT_INCOMPLETE"),
        ('{"schema_version":', "STOP", "VISION_OUTPUT_INCOMPLETE"),
        ('{"schema_version":', None, "VISION_OUTPUT_INCOMPLETE"),
        (None, None, "VISION_OUTPUT_INCOMPLETE"),
        ("", "STOP", "VISION_OUTPUT_INCOMPLETE"),
        ('{"schema_version":}', "STOP", "INVALID_VISION_PAYLOAD"),
        ("{}", "STOP", "INVALID_VISION_PAYLOAD"),
    ],
)
def test_invalid_raw_response_has_safe_code_and_bounded_metadata(
    text: str | None, finish_reason: str | None, expected_code: str
) -> None:
    """Differentiate EOF from malformed JSON while preserving only safe metadata."""
    usage: JsonObject = {
        "prompt_token_count": 42,
        "candidates_token_count": 17,
        "thoughts_token_count": True,
        "total_token_count": -1,
        "untrusted_text": "secret value",
    }
    with pytest.raises(VisionOutputValidationError) as captured:
        validate_vision_response(
            text,
            finish_reason=finish_reason,
            usage=usage,
            stage="primary",
        )
    error = captured.value
    assert error.code == expected_code
    assert "secret value" not in str(error)
    assert error.diagnostic == {
        "stage": "primary",
        "code": expected_code,
        "finish_reason": finish_reason,
        "prompt_token_count": 42,
        "candidates_token_count": 17,
        "thoughts_token_count": None,
        "total_token_count": None,
        "response_bytes": None if text is None else len(text.encode("utf-8")),
        "response_sha256": (
            None
            if text is None
            else f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
        ),
    }


def test_complete_json_with_max_tokens_is_still_incomplete() -> None:
    """A provider truncation finish reason outranks valid-looking JSON."""
    with pytest.raises(VisionOutputValidationError) as captured:
        validate_vision_response(
            json.dumps(_draft()),
            finish_reason="MAX_TOKENS",
            usage={},
            stage="repair",
        )
    assert captured.value.code == "VISION_OUTPUT_INCOMPLETE"
    assert captured.value.diagnostic["stage"] == "repair"


def test_valid_vision_output_passes_strict_schema() -> None:
    """A complete draft still reaches the existing semantic validation path."""
    parsed = validate_vision_response(
        json.dumps(_draft()),
        finish_reason="STOP",
        usage={},
        stage="primary",
    )
    assert parsed.model_dump(mode="json") == _draft()


def test_explicit_empty_provider_part_retains_zero_byte_diagnostic() -> None:
    """An empty text part is known output, not missing response metadata."""
    response = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text="")]),
        finish_reason=types.FinishReason.STOP,
    )
    with pytest.raises(VisionOutputValidationError) as captured:
        validate_primary_response(cast("CallbackContext", None), response)
    assert captured.value.diagnostic["response_bytes"] == 0
    assert captured.value.diagnostic["response_sha256"] == (
        "sha256:" + hashlib.sha256(b"").hexdigest()
    )
