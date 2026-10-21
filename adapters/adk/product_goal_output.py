# adapters/adk/product_goal_output.py
"""Classify Product Goal output before ADK schema conversion loses metadata."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from google.genai import types
from pydantic import ValidationError
from pydantic_core import from_json

from adapters.adk.errors import ProductGoalOutputValidationError
from services.contracts.product_goal import ProductGoalInterviewOutput
from utils.generation_policy import get_generation_policy
from workflow.contracts import WorkflowErrorCode

if TYPE_CHECKING:
    from workflow.contracts import JsonObject


def _safe_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _safe_generation_settings(settings: JsonObject) -> JsonObject:
    """Copy only known nonsecret generation fields, never arbitrary arguments."""
    config = settings.get("generation_config")
    limits: JsonObject = {}
    if isinstance(config, dict):
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            count = _safe_count(config.get(key))
            if count is not None:
                limits[key] = count
    safe: JsonObject = {"generation_config": limits}
    if settings.get("generation_policy") == get_generation_policy():
        safe["generation_policy"] = get_generation_policy()
    return safe


def validate_product_goal_output(
    response_text: str | None,
    *,
    finish_reason: str | None,
    usage: JsonObject,
    generation_settings: JsonObject,
) -> ProductGoalInterviewOutput:
    """Reject empty/truncated output even when it contains schema-valid JSON."""
    incomplete = finish_reason == types.FinishReason.MAX_TOKENS.value
    if response_text is None or not response_text.strip():
        incomplete = True
    elif not incomplete:
        try:
            from_json(response_text)
        except ValueError as error:
            incomplete = str(error).startswith("EOF while parsing")
    if not incomplete and response_text is not None:
        try:
            return ProductGoalInterviewOutput.model_validate(json.loads(response_text))
        except (ValueError, ValidationError, TypeError):
            pass
    code = (
        WorkflowErrorCode.PRODUCT_GOAL_OUTPUT_INCOMPLETE
        if incomplete
        else WorkflowErrorCode.INVALID_PRODUCT_GOAL_PAYLOAD
    )
    encoded = None if response_text is None else response_text.encode("utf-8")
    safe_reason = (
        finish_reason
        if finish_reason in {reason.value for reason in types.FinishReason}
        else None
    )
    diagnostic: JsonObject = {
        "code": code.value,
        "finish_reason": safe_reason,
        "prompt_token_count": _safe_count(usage.get("prompt_token_count")),
        "candidates_token_count": _safe_count(usage.get("candidates_token_count")),
        "thoughts_token_count": _safe_count(usage.get("thoughts_token_count")),
        "total_token_count": _safe_count(usage.get("total_token_count")),
        "response_bytes": None if encoded is None else len(encoded),
        "response_sha256": (
            None if encoded is None else "sha256:" + hashlib.sha256(encoded).hexdigest()
        ),
        "generation_settings": _safe_generation_settings(generation_settings),
    }
    detail = "incomplete output" if incomplete else "an invalid payload"
    settings_json = json.dumps(diagnostic["generation_settings"], sort_keys=True)
    message = (
        f"Product Goal generation returned {detail}. "
        f"finish_reason={safe_reason or 'unknown'}; "
        f"completion_tokens={diagnostic['candidates_token_count']}; "
        f"reasoning_tokens={diagnostic['thoughts_token_count']}; "
        f"generation_settings={settings_json}. "
        "Check the effective generation policy and provider response, then use "
        "the current Goal recovery command with a new idempotency key. "
        "The same key replays this failure."
    )
    raise ProductGoalOutputValidationError(
        code=code.value, message=message, diagnostic=diagnostic
    ) from None
