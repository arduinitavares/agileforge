# services/specs/profile_content.py
"""Helpers for normalizing structured spec content before registry storage."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
)

STRUCTURED_SPEC_FORMAT: str = "agileforge.spec.v1"
UNSUPPORTED_SPEC_SOURCE_FORMAT: str = "SPEC_SOURCE_FORMAT_UNSUPPORTED"
INVALID_SPEC_FILE: str = "SPEC_FILE_INVALID"


class SpecContentNormalizationError(ValueError):
    """Raised when spec content cannot be normalized for authority compilation."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code: str = error_code


@dataclass(frozen=True)
class NormalizedSpecContent:
    """Normalized spec content ready for SpecRegistry."""

    content: str
    spec_hash: str
    format: str


def normalize_spec_content_for_registry(raw_content: str) -> NormalizedSpecContent:
    """Canonicalize agileforge.spec.v1 JSON and reject every other format."""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise SpecContentNormalizationError(
            "Expected agileforge.spec.v1 JSON; received non-JSON spec content.",
            error_code=UNSUPPORTED_SPEC_SOURCE_FORMAT,
        ) from exc

    if not isinstance(parsed, dict):
        raise SpecContentNormalizationError(
            "Expected agileforge.spec.v1 JSON object.",
            error_code=UNSUPPORTED_SPEC_SOURCE_FORMAT,
        )
    if parsed.get("schema_version") != STRUCTURED_SPEC_FORMAT:
        raise SpecContentNormalizationError(
            "Expected schema_version='agileforge.spec.v1'.",
            error_code=UNSUPPORTED_SPEC_SOURCE_FORMAT,
        )

    try:
        artifact = TechnicalSpecArtifact.model_validate(parsed)
    except ValidationError as exc:
        message = f"Invalid agileforge.spec.v1 content: {exc}"
        raise SpecContentNormalizationError(
            message,
            error_code=INVALID_SPEC_FILE,
        ) from exc
    return NormalizedSpecContent(
        content=canonical_spec_json(artifact),
        spec_hash=canonical_spec_hash(artifact),
        format=STRUCTURED_SPEC_FORMAT,
    )
