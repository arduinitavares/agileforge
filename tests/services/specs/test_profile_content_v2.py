"""Hard-break normalization tests for canonical Specification v2 content."""

from __future__ import annotations

import json

import pytest

from services.specs.profile_content import (
    INVALID_SPECIFICATION_PAYLOAD,
    STRUCTURED_SPEC_FORMAT,
    UNSUPPORTED_SPECIFICATION_SCHEMA,
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)


def _v2_payload() -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v2",
        "artifact_id": "SPEC.issue-199",
        "title": "Single to-spec boundary",
        "summary": "Author one typed candidate.",
        "problem_statement": "Discovery must not be a persisted gate.",
        "items": [
            {
                "id": "REQ.issue-199.typed",
                "type": "REQ",
                "title": "Typed payload",
                "statement": "The candidate MUST use Specification v2.",
                "level": "MUST",
                "verification": "system-test",
                "acceptance": ["A v2 payload is stored canonically."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
    }


def test_registry_normalizer_accepts_only_canonical_v2_json() -> None:
    """Valid v2 producer output receives one canonical byte representation."""
    normalized = normalize_spec_content_for_registry(
        json.dumps(_v2_payload(), indent=2)
    )

    assert normalized.format == STRUCTURED_SPEC_FORMAT == "agileforge.spec.v2"
    assert normalized.content.startswith('{"artifact_id":"SPEC.issue-199"')
    assert normalized.spec_hash.startswith("sha256:")
    assert normalize_spec_content_for_registry(normalized.content) == normalized


@pytest.mark.parametrize(
    "raw_content",
    [
        "# Markdown is a projection, never canonical input",
        json.dumps({"schema_version": "agileforge.spec.v1"}),
        json.dumps(["agileforge.spec.v2"]),
    ],
)
def test_registry_normalizer_rejects_non_v2_sources(raw_content: str) -> None:
    """Frozen v1, prose, and non-object JSON fail at the hard-break boundary."""
    with pytest.raises(SpecContentNormalizationError) as error:
        normalize_spec_content_for_registry(raw_content)

    assert error.value.error_code == UNSUPPORTED_SPECIFICATION_SCHEMA


def test_registry_normalizer_distinguishes_malformed_v2_payload() -> None:
    """A declared v2 object with invalid semantics gets a typed payload error."""
    invalid = _v2_payload()
    invalid["items"] = []
    invalid["status"] = "accepted"

    with pytest.raises(SpecContentNormalizationError) as error:
        normalize_spec_content_for_registry(json.dumps(invalid))

    assert error.value.error_code == INVALID_SPECIFICATION_PAYLOAD
