# adapters/adk/generation_output.py
"""Reject incomplete structured generations before SDK schema conversion."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from google.genai import types
from pydantic_core import from_json

from adapters.adk.errors import GenerationOutputValidationError
from workflow.contracts import WorkflowErrorCode

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse

    from workflow.contracts import JsonObject


def _safe_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def validate_generation_output(response: object, *, require_json: bool = True) -> None:
    """Check raw final metadata without persisting output or provider error text."""
    if getattr(response, "partial", False):
        return
    content = getattr(response, "content", None)
    parts = getattr(content, "parts", None) or []
    reason = getattr(response, "finish_reason", None)
    reason = getattr(reason, "value", reason)
    incomplete = reason == types.FinishReason.MAX_TOKENS.value
    if not incomplete and any(getattr(p, "function_call", None) for p in parts):
        return
    text = "".join(
        p.text
        for p in parts
        if getattr(p, "text", None) and not getattr(p, "thought", False)
    )
    incomplete = incomplete or not text.strip()
    invalid = False
    if not incomplete and require_json:
        try:
            from_json(text)
        except ValueError as error:
            incomplete = str(error).startswith("EOF while parsing")
            invalid = not incomplete
    if not incomplete and not invalid:
        return
    code = (
        WorkflowErrorCode.GENERATION_OUTPUT_INCOMPLETE
        if incomplete
        else WorkflowErrorCode.INVALID_GENERATION_OUTPUT
    )
    safe_reason = reason if reason in {r.value for r in types.FinishReason} else None
    usage = getattr(response, "usage_metadata", None)
    encoded = text.encode("utf-8")
    diagnostic: JsonObject = {
        "code": code.value,
        "finish_reason": safe_reason,
        "response_bytes": len(encoded),
        "response_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }
    for key in (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
    ):
        diagnostic[key] = _safe_count(getattr(usage, key, None))
    detail = "incomplete output" if incomplete else "invalid structured output"
    message = (
        f"Generation returned {detail}. finish_reason={safe_reason or 'unknown'}; "
        f"completion_tokens={diagnostic['candidates_token_count']}; "
        f"reasoning_tokens={diagnostic['thoughts_token_count']}. "
        "Check the effective generation policy and provider response, then use "
        "the current workflow recovery command with a new idempotency key. "
        "The same key replays this failure."
    )
    raise GenerationOutputValidationError(
        code=code.value, message=message, diagnostic=diagnostic
    ) from None


def validate_generation_response(
    callback_context: CallbackContext | None, llm_response: LlmResponse
) -> None:
    """ADK callback shared by downstream generation, repair and review agents."""
    del callback_context
    validate_generation_output(llm_response)
