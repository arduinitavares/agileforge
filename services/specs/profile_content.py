# services/specs/profile_content.py
"""Helpers for normalizing spec content before registry storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError

from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
)

STRUCTURED_SPEC_FORMAT: str = "agileforge.spec.v1"
LEGACY_MARKDOWN_FORMAT: str = "agileforge.spec_legacy_markdown.v1"


class SpecContentNormalizationError(ValueError):
    """Raised when structured spec content cannot be normalized."""


@dataclass(frozen=True)
class NormalizedSpecContent:
    """Normalized spec content ready for SpecRegistry."""

    content: str
    spec_hash: str
    format: str


def normalize_spec_content_for_registry(raw_content: str) -> NormalizedSpecContent:
    """Canonicalize structured specs and preserve legacy Markdown behavior."""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return _legacy(raw_content)

    if (
        not isinstance(parsed, dict)
        or parsed.get("schema_version") != STRUCTURED_SPEC_FORMAT
    ):
        return _legacy(raw_content)

    try:
        artifact = TechnicalSpecArtifact.model_validate(parsed)
    except ValidationError as exc:
        message = f"Invalid agileforge.spec.v1 content: {exc}"
        raise SpecContentNormalizationError(message) from exc
    return NormalizedSpecContent(
        content=canonical_spec_json(artifact),
        spec_hash=canonical_spec_hash(artifact),
        format=STRUCTURED_SPEC_FORMAT,
    )


def _legacy(raw_content: str) -> NormalizedSpecContent:
    return NormalizedSpecContent(
        content=raw_content,
        spec_hash=hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
        format=LEGACY_MARKDOWN_FORMAT,
    )
