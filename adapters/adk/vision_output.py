# adapters/adk/vision_output.py
"""Validate raw Vision responses before ADK can discard provider metadata."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from google.genai import types
from pydantic import ValidationError
from pydantic_core import from_json

from adapters.adk.errors import VisionOutputValidationError
from services.contracts.vision import VisionDraftOutput
from workflow.contracts import WorkflowErrorCode

if TYPE_CHECKING:
    from workflow.contracts import JsonObject


def _safe_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _diagnostic(
    response_text: str | None,
    *,
    stage: str,
    code: str,
    finish_reason: str | None,
    usage: JsonObject,
) -> JsonObject:
    encoded = None if response_text is None else response_text.encode("utf-8")
    allowed_reasons = {reason.value for reason in types.FinishReason}
    return {
        "stage": stage,
        "code": code,
        "finish_reason": finish_reason if finish_reason in allowed_reasons else None,
        "prompt_token_count": _safe_count(usage.get("prompt_token_count")),
        "candidates_token_count": _safe_count(usage.get("candidates_token_count")),
        "thoughts_token_count": _safe_count(usage.get("thoughts_token_count")),
        "total_token_count": _safe_count(usage.get("total_token_count")),
        "response_bytes": None if encoded is None else len(encoded),
        "response_sha256": (
            None if encoded is None else f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        ),
    }


def validate_vision_response(
    response_text: str | None,
    *,
    finish_reason: str | None,
    usage: JsonObject,
    stage: str,
) -> VisionDraftOutput:
    """Classify a provider response and enforce the strict Vision schema."""
    if stage not in ("primary", "repair"):
        message = "Vision output stage must be primary or repair."
        raise ValueError(message)
    incomplete = finish_reason == types.FinishReason.MAX_TOKENS.value
    if response_text is None or not response_text.strip():
        incomplete = True
    elif not incomplete:
        try:
            from_json(response_text)
        except ValueError as error:
            incomplete = str(error).startswith("EOF while parsing")
    code = (
        WorkflowErrorCode.VISION_OUTPUT_INCOMPLETE
        if incomplete
        else WorkflowErrorCode.INVALID_VISION_PAYLOAD
    )
    if not incomplete and response_text is not None:
        try:
            parsed = json.loads(response_text)
            return VisionDraftOutput.model_validate(parsed)
        except (ValueError, ValidationError, TypeError):
            pass
    message = (
        "Vision generation returned incomplete output. Retry this Vision step."
        if incomplete
        else "Vision generation returned an invalid payload. Retry this Vision step."
    )
    raise VisionOutputValidationError(
        code=code.value,
        message=message,
        diagnostic=_diagnostic(
            response_text,
            stage=stage,
            code=code.value,
            finish_reason=finish_reason,
            usage=usage,
        ),
    ) from None
