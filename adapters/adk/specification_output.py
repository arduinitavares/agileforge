# adapters/adk/specification_output.py
"""Pure response classification and bounded diagnostic extraction."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from adapters.adk.errors import SpecificationOutputValidationError
from services.contracts.specification_authoring import (
    SpecificationStructuringOutput,
)
from utils.agileforge_spec_profile_v2 import _ITEM_ID_RE, SCHEMA_VERSION
from workflow.contracts import WorkflowErrorCode

if TYPE_CHECKING:
    from workflow.contracts import JsonObject

logger: logging.Logger = logging.getLogger(name=__name__)

_DIAGNOSTIC_SCHEMA_VERSION: str = "agileforge.specification-output-diagnostic.v1"
_MAX_DIAGNOSTIC_IDS: int = 100
_MAX_MESSAGE_ENDPOINT_IDS: int = 5


def _is_valid_item_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ITEM_ID_RE.match(value))


def _extract_known_item_ids(
    raw_items: object,
) -> tuple[int | None, list[str], bool, set[str]]:
    if not isinstance(raw_items, list):
        return None, [], False, set()

    known_ids: set[str] = set()
    for item in raw_items:
        if isinstance(item, dict):
            item_dict = cast("dict[str, object]", item)
            item_id = item_dict.get("id")
            if _is_valid_item_id(item_id):
                known_ids.add(cast("str", item_id))
    sorted_known = sorted(known_ids)
    ids_truncated = len(sorted_known) > _MAX_DIAGNOSTIC_IDS
    item_ids = sorted_known[:_MAX_DIAGNOSTIC_IDS]
    return len(raw_items), item_ids, ids_truncated, known_ids


def _extract_missing_relation_ids(
    raw_relations: object,
    known_ids: set[str],
) -> tuple[int | None, int | None, list[str], bool]:
    if not isinstance(raw_relations, list):
        return None, None, [], False

    missing_set: set[str] = set()
    for rel in raw_relations:
        if isinstance(rel, dict):
            rel_dict = cast("dict[str, object]", rel)
            for key in ("from", "to", "from_"):
                endpoint = rel_dict.get(key)
                if _is_valid_item_id(endpoint) and endpoint not in known_ids:
                    missing_set.add(cast("str", endpoint))
    sorted_missing = sorted(missing_set)
    ids_truncated = len(sorted_missing) > _MAX_DIAGNOSTIC_IDS
    missing_item_ids = sorted_missing[:_MAX_DIAGNOSTIC_IDS]
    return len(raw_relations), len(missing_set), missing_item_ids, ids_truncated


def _extract_item_and_relation_ids(
    parsed: object,
) -> tuple[
    int | None,
    int | None,
    int | None,
    list[str],
    list[str],
    bool,
]:
    if not isinstance(parsed, dict):
        return None, None, None, [], [], False

    dict_val = cast("dict[str, object]", parsed)
    payload = dict_val.get("payload")
    if not isinstance(payload, dict):
        return None, None, None, [], [], False

    payload_dict = cast("dict[str, object]", payload)
    raw_items = payload_dict.get("items")
    raw_relations = payload_dict.get("relations")
    if not isinstance(raw_items, list):
        rel_count = len(raw_relations) if isinstance(raw_relations, list) else None
        return None, rel_count, None, [], [], False

    item_count, item_ids, items_truncated, known_ids = _extract_known_item_ids(
        raw_items
    )
    (
        relation_count,
        missing_count,
        missing_ids,
        rels_truncated,
    ) = _extract_missing_relation_ids(raw_relations, known_ids)

    return (
        item_count,
        relation_count,
        missing_count,
        item_ids,
        missing_ids,
        items_truncated or rels_truncated,
    )


def build_specification_output_diagnostic(
    response_text: str | None,
    *,
    finish_reason: str | None,
    usage: JsonObject,
    code: str,
) -> JsonObject:
    """Extract bounded, sanitized diagnostic metadata from a generated response."""
    response_bytes: int | None = None
    response_sha256: str | None = None
    if response_text is not None:
        encoded = response_text.encode("utf-8")
        response_bytes = len(encoded)
        response_sha256 = f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    prompt_tokens = usage.get("prompt_token_count")
    candidate_tokens = usage.get("candidates_token_count")
    prompt_token_count: int | None = (
        prompt_tokens if isinstance(prompt_tokens, int) and prompt_tokens >= 0 else None
    )
    candidates_token_count: int | None = (
        candidate_tokens
        if isinstance(candidate_tokens, int) and candidate_tokens >= 0
        else None
    )

    parsed: object = None
    if response_text is not None and response_text.strip():
        try:
            parsed = json.loads(response_text)
        except (json.JSONDecodeError, ValueError):
            parsed = None

    (
        item_count,
        relation_count,
        missing_item_count,
        item_ids,
        missing_item_ids,
        ids_truncated,
    ) = _extract_item_and_relation_ids(parsed)

    return cast(
        "JsonObject",
        {
            "schema_version": _DIAGNOSTIC_SCHEMA_VERSION,
            "stage": "primary",
            "code": code,
            "response_sha256": response_sha256,
            "response_bytes": response_bytes,
            "finish_reason": finish_reason,
            "prompt_token_count": prompt_token_count,
            "candidates_token_count": candidates_token_count,
            "item_count": item_count,
            "relation_count": relation_count,
            "missing_item_count": missing_item_count,
            "item_ids": item_ids,
            "missing_item_ids": missing_item_ids,
            "ids_truncated": ids_truncated,
        },
    )


def validate_specification_response(
    response_text: str | None,
    *,
    finish_reason: str | None,
    usage: JsonObject,
) -> SpecificationStructuringOutput:
    """Validate raw provider response and return model or raise typed error."""
    if response_text is None or not response_text.strip():
        is_truncation = finish_reason in (
            None,
            "STOP",
            "MAX_TOKENS",
        )
        if is_truncation:
            code = WorkflowErrorCode.SPECIFICATION_OUTPUT_INCOMPLETE.value
            message = (
                "Specification structurer returned incomplete output. Increase "
                "SPECIFICATION_STRUCTURER_MAX_TOKENS or select a provider that can "
                "return the complete structured payload, then retry Structure "
                "Specification."
            )
        else:
            code = WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD.value
            message = "Specification structurer returned an invalid v2 payload."
        diagnostic = build_specification_output_diagnostic(
            response_text,
            finish_reason=finish_reason,
            usage=usage,
            code=code,
        )
        raise SpecificationOutputValidationError(
            code=code,
            message=message,
            diagnostic=diagnostic,
        )

    try:
        parsed = json.loads(response_text)
    except (json.JSONDecodeError, ValueError):
        code = WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD.value
        message = "Specification structurer returned an invalid v2 payload."
        diagnostic = build_specification_output_diagnostic(
            response_text,
            finish_reason=finish_reason,
            usage=usage,
            code=code,
        )
        raise SpecificationOutputValidationError(
            code=code,
            message=message,
            diagnostic=diagnostic,
        ) from None

    if not isinstance(parsed, dict):
        code = WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD.value
        message = "Specification structurer returned an invalid v2 payload."
        diagnostic = build_specification_output_diagnostic(
            response_text,
            finish_reason=finish_reason,
            usage=usage,
            code=code,
        )
        raise SpecificationOutputValidationError(
            code=code,
            message=message,
            diagnostic=diagnostic,
        )

    dict_parsed = cast("dict[str, object]", parsed)
    raw_payload = dict_parsed.get("payload")
    payload = (
        cast("dict[str, object]", raw_payload)
        if isinstance(raw_payload, dict)
        else None
    )
    schema_version = (
        payload.get("schema_version") if payload is not None else None
    )
    if isinstance(schema_version, str) and schema_version != SCHEMA_VERSION:
        code = WorkflowErrorCode.UNSUPPORTED_SPECIFICATION_SCHEMA.value
        message = "Specification structurer returned an unsupported schema."
        diagnostic = build_specification_output_diagnostic(
            response_text,
            finish_reason=finish_reason,
            usage=usage,
            code=code,
        )
        raise SpecificationOutputValidationError(
            code=code,
            message=message,
            diagnostic=diagnostic,
        )

    try:
        return SpecificationStructuringOutput.model_validate(parsed)
    except (ValidationError, ValueError):
        code = WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD.value
        diagnostic = build_specification_output_diagnostic(
            response_text,
            finish_reason=finish_reason,
            usage=usage,
            code=code,
        )
        missing_ids = cast("list[str]", diagnostic.get("missing_item_ids") or [])
        raw_missing_count = diagnostic.get("missing_item_count")
        missing_count = (
            raw_missing_count
            if isinstance(raw_missing_count, int)
            else len(missing_ids)
        )
        if missing_ids:
            if missing_count <= _MAX_MESSAGE_ENDPOINT_IDS:
                endpoints_str = ", ".join(missing_ids)
                message = (
                    f"Specification structurer returned an invalid v2 payload. "
                    f"Unknown relation endpoint: {endpoints_str}."
                )
            else:
                first_ids = ", ".join(missing_ids[:_MAX_MESSAGE_ENDPOINT_IDS])
                remaining = missing_count - _MAX_MESSAGE_ENDPOINT_IDS
                message = (
                    f"Specification structurer returned an invalid v2 payload. "
                    f"Unknown relation endpoint: {first_ids} (+{remaining} more)."
                )
        else:
            message = "Specification structurer returned an invalid v2 payload."

        raise SpecificationOutputValidationError(
            code=code,
            message=message,
            diagnostic=diagnostic,
        ) from None


__all__ = [
    "build_specification_output_diagnostic",
    "validate_specification_response",
]
