# services/specs/profile_content.py
"""Helpers for normalizing structured spec content before registry storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Self

from pydantic import ValidationError

from utils.agileforge_spec_profile_v2 import (
    SCHEMA_VERSION,
    SpecificationPayload,
    canonical_spec_hash,
    canonical_spec_json,
)

STRUCTURED_SPEC_FORMAT: str = SCHEMA_VERSION
UNSUPPORTED_SPECIFICATION_SCHEMA: str = "UNSUPPORTED_SPECIFICATION_SCHEMA"
INVALID_SPECIFICATION_PAYLOAD: str = "INVALID_SPECIFICATION_PAYLOAD"


class SpecContentNormalizationError(ValueError):
    """Raised when spec content cannot be normalized for registry storage."""

    def __init__(self, message: str, *, error_code: str) -> None:
        """Store the normalization error message and stable error code."""
        super().__init__(message)
        self.error_code: str = error_code

    @classmethod
    def non_json(cls) -> Self:
        """Build the canonical unsupported non-JSON source error."""
        message = "Expected agileforge.spec.v2 JSON; received non-JSON content."
        return cls(message, error_code=UNSUPPORTED_SPECIFICATION_SCHEMA)

    @classmethod
    def non_object(cls) -> Self:
        """Build the canonical unsupported JSON value error."""
        message = "Expected agileforge.spec.v2 JSON object."
        return cls(message, error_code=UNSUPPORTED_SPECIFICATION_SCHEMA)

    @classmethod
    def unsupported_schema_version(cls) -> Self:
        """Build the canonical unsupported schema-version error."""
        message = "Expected schema_version='agileforge.spec.v2'."
        return cls(message, error_code=UNSUPPORTED_SPECIFICATION_SCHEMA)


@dataclass(frozen=True)
class NormalizedSpecContent:
    """Normalized spec content ready for SpecRegistry."""

    content: str
    spec_hash: str
    format: str


def normalize_spec_content_for_registry(raw_content: str) -> NormalizedSpecContent:
    """Canonicalize agileforge.spec.v2 JSON and reject every other format."""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise SpecContentNormalizationError.non_json() from exc

    if not isinstance(parsed, dict):
        raise SpecContentNormalizationError.non_object()
    if parsed.get("schema_version") != STRUCTURED_SPEC_FORMAT:
        raise SpecContentNormalizationError.unsupported_schema_version()

    try:
        artifact = SpecificationPayload.model_validate(parsed)
    except ValidationError as exc:
        message = f"Invalid agileforge.spec.v2 content: {exc}"
        raise SpecContentNormalizationError(
            message,
            error_code=INVALID_SPECIFICATION_PAYLOAD,
        ) from exc
    return NormalizedSpecContent(
        content=canonical_spec_json(artifact),
        spec_hash=canonical_spec_hash(artifact),
        format=STRUCTURED_SPEC_FORMAT,
    )
