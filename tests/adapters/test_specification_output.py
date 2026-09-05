# tests/adapters/test_specification_output.py
"""Tests for pure Specification output validation and diagnostic extraction."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, cast

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adapters.adk.agents.specification_author import validate_specification_output
from adapters.adk.errors import (
    SpecificationAgenticExecutionError,
    SpecificationOutputValidationError,
)
from adapters.adk.specification_output import (
    build_specification_output_diagnostic,
    validate_specification_response,
)

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext

    from workflow.contracts import JsonObject

_TWO_MISSING_COUNT: int = 2
_EIGHT_MISSING_COUNT: int = 8
_ONE_HUNDRED_ITEMS: int = 100
_ONE_HUNDRED_FIVE_ITEMS: int = 105


def _valid_payload() -> JsonObject:
    return {
        "payload": {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.valid-spec",
            "title": "Valid Specification",
            "summary": "Summary of valid specification.",
            "problem_statement": "Problem statement.",
            "items": [
                {
                    "id": "REQ.first-item",
                    "type": "REQ",
                    "title": "First item",
                    "statement": "The system MUST do something.",
                    "level": "MUST",
                    "verification": "inspection",
                    "acceptance": ["It works."],
                }
            ],
            "relations": [],
        }
    }


def test_valid_specification_response_returns_model() -> None:
    """A structurally valid response returns SpecificationStructuringOutput."""
    raw = json.dumps(_valid_payload())
    result = validate_specification_response(raw, finish_reason="STOP", usage={})
    assert result.payload.artifact_id == "SPEC.valid-spec"
    assert len(result.payload.items) == 1
    assert result.payload.items[0].id == "REQ.first-item"


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        (
            '{"payload":{"schema_version":"agileforge.spec.v2"}}',
            "INVALID_SPECIFICATION_PAYLOAD",
        ),
        (
            '{"payload":{"schema_version":"agileforge.spec.v1"}}',
            "UNSUPPORTED_SPECIFICATION_SCHEMA",
        ),
        (
            '{"payload":',
            "SPECIFICATION_OUTPUT_INCOMPLETE",
        ),
        (
            '{"payload": invalid}',
            "INVALID_SPECIFICATION_PAYLOAD",
        ),
        (
            '[1, 2, 3]',
            "INVALID_SPECIFICATION_PAYLOAD",
        ),
        (
            "",
            "SPECIFICATION_OUTPUT_INCOMPLETE",
        ),
    ],
)
def test_complete_and_incomplete_outputs_keep_distinct_codes(
    raw: str, expected_code: str
) -> None:
    """Ensure malformed, unsupported, and truncated outputs preserve codes."""
    response = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=raw)]),
        finish_reason=types.FinishReason.STOP,
    )
    dummy_context = cast("CallbackContext", object())
    with pytest.raises(SpecificationAgenticExecutionError) as raised:
        validate_specification_output(dummy_context, response)
    assert raised.value.code == expected_code


def test_valid_json_at_max_tokens_is_incomplete() -> None:
    """Valid JSON truncated by max tokens is classified as incomplete."""
    raw = json.dumps(_valid_payload())
    response = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=raw)]),
        finish_reason=types.FinishReason.MAX_TOKENS,
    )
    dummy_context = cast("CallbackContext", object())
    with pytest.raises(SpecificationAgenticExecutionError) as raised:
        validate_specification_output(dummy_context, response)
    assert raised.value.code == "SPECIFICATION_OUTPUT_INCOMPLETE"


def test_invalid_output_diagnostics_do_not_echo_response_prose() -> None:
    """Diagnostic objects must never leak private response strings or prose."""
    sentinel = "PRIVATE_RESPONSE_SENTINEL_245"
    raw = json.dumps(
        {
            "payload": {
                "schema_version": "agileforge.spec.v2",
                "items": [{"statement": sentinel}],
            }
        }
    )
    with pytest.raises(SpecificationOutputValidationError) as raised:
        validate_specification_response(raw, finish_reason="STOP", usage={})
    error = raised.value
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert sentinel not in json.dumps(error.diagnostic)
    assert error.diagnostic["response_bytes"] == len(raw.encode("utf-8"))
    assert error.diagnostic["response_sha256"] == (
        f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
    )


def test_unknown_relation_endpoint_formatting_up_to_five() -> None:
    """Missing relation endpoints up to five are listed in the error message."""
    payload = _valid_payload()
    p = payload["payload"]
    assert isinstance(p, dict)
    p["relations"] = [
        {"from": "REQ.first-item", "type": "tracks", "to": "RISK.missing-one"},
        {"from": "REQ.first-item", "type": "tracks", "to": "RISK.missing-two"},
    ]
    raw = json.dumps(payload)
    with pytest.raises(SpecificationOutputValidationError) as raised:
        validate_specification_response(raw, finish_reason="STOP", usage={})
    error = raised.value
    assert "RISK.missing-one" in str(error)
    assert "RISK.missing-two" in str(error)
    assert error.diagnostic["missing_item_count"] == _TWO_MISSING_COUNT
    missing_ids = cast("list[str]", error.diagnostic["missing_item_ids"])
    assert sorted(missing_ids) == [
        "RISK.missing-one",
        "RISK.missing-two",
    ]


def test_unknown_relation_endpoint_formatting_exceeding_five() -> None:
    """Missing relation endpoints exceeding five are summarized with (+N more)."""
    payload = _valid_payload()
    p = payload["payload"]
    assert isinstance(p, dict)
    p["relations"] = [
        {"from": "REQ.first-item", "type": "tracks", "to": f"RISK.missing-{i}"}
        for i in range(8)
    ]
    raw = json.dumps(payload)
    with pytest.raises(SpecificationOutputValidationError) as raised:
        validate_specification_response(raw, finish_reason="STOP", usage={})
    error = raised.value
    assert "(+3 more)" in str(error)
    assert error.diagnostic["missing_item_count"] == _EIGHT_MISSING_COUNT


def test_diagnostic_caps_and_truncation_at_100() -> None:
    """Diagnostic item IDs are capped at 100 with ids_truncated=True."""
    payload = _valid_payload()
    p = payload["payload"]
    assert isinstance(p, dict)
    p["items"] = [
        {
            "id": f"REQ.item-{i:03d}",
            "type": "REQ",
            "title": f"Item {i}",
            "statement": "Statement.",
            "level": "MUST",
            "verification": "inspection",
            "acceptance": ["Acceptance."],
        }
        for i in range(105)
    ]
    raw = json.dumps(payload)
    diagnostic = build_specification_output_diagnostic(
        raw, finish_reason="STOP", usage={}, code="TEST_CODE"
    )
    assert diagnostic["item_count"] == _ONE_HUNDRED_FIVE_ITEMS
    item_ids = cast("list[str]", diagnostic["item_ids"])
    assert len(item_ids) == _ONE_HUNDRED_ITEMS
    assert diagnostic["ids_truncated"] is True


@pytest.mark.parametrize("finish_reason", ["SAFETY", "OTHER"])
def test_empty_provider_response_with_non_truncation_is_invalid_payload(
    finish_reason: str,
) -> None:
    """Empty provider rejections (SAFETY, OTHER) must be classified as invalid."""
    with pytest.raises(SpecificationOutputValidationError) as raised:
        validate_specification_response("", finish_reason=finish_reason, usage={})
    assert raised.value.code == "INVALID_SPECIFICATION_PAYLOAD"
    assert (
        "Specification structurer returned an invalid v2 payload." in str(raised.value)
    )


def test_unknown_relation_endpoint_formatting_exceeding_one_hundred() -> None:
    """Missing endpoints exceeding 100 calculate remaining from total count."""
    payload = _valid_payload()
    p = payload["payload"]
    assert isinstance(p, dict)
    p["relations"] = [
        {"from": "REQ.first-item", "type": "tracks", "to": f"RISK.missing-{i:03d}"}
        for i in range(105)
    ]
    raw = json.dumps(payload)
    with pytest.raises(SpecificationOutputValidationError) as raised:
        validate_specification_response(raw, finish_reason="STOP", usage={})
    error = raised.value
    assert "(+100 more)" in str(error)
    assert error.diagnostic["missing_item_count"] == _ONE_HUNDRED_FIVE_ITEMS
    assert (
        len(cast("list[str]", error.diagnostic["missing_item_ids"]))
        == _ONE_HUNDRED_ITEMS
    )
