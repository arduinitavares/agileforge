"""Typed-only compiler input contract for accepted Specification v2 payloads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.authority_input_v2 import (
    AuthorityInputV2,
    build_authority_input_v2,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from utils.spec_schemas import SpecAuthorityCompilerInput


def _authority_input() -> AuthorityInputV2:
    payload = SpecificationPayload.model_validate(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.compiler-input",
            "title": "Typed compiler input",
            "summary": "Compile accepted semantic content only.",
            "problem_statement": "Raw text can bypass exact human review.",
            "items": [
                {
                    "id": "REQ.compiler.typed",
                    "type": "REQ",
                    "title": "Typed input",
                    "statement": "Authority MUST consume typed input.",
                    "level": "MUST",
                    "verification": "system-test",
                    "acceptance": ["No raw source field reaches the compiler."],
                }
            ],
        }
    )
    return build_authority_input_v2(payload)


def test_compiler_input_contains_only_host_built_typed_authority_data() -> None:
    """The ADK compiler receives no raw text, path, or format selector."""
    compiler_input = SpecAuthorityCompilerInput(
        authority_input=_authority_input(),
        project_id=7,
        spec_version_id=11,
        specification_fingerprint="sha256:" + "a" * 64,
    )

    assert compiler_input.schema_version == "agileforge.authority-compiler-input.v2"
    assert compiler_input.authority_input.eligible_item_ids == ("REQ.compiler.typed",)
    assert {
        "spec_source",
        "spec_content_ref",
        "domain_hint",
        "spec_source_format",
    }.isdisjoint(SpecAuthorityCompilerInput.model_fields)


@pytest.mark.parametrize(
    "field",
    ["spec_source", "spec_content_ref", "content"],
)
def test_compiler_input_rejects_raw_source_bypasses(field: str) -> None:
    """Raw or file-backed source cannot be smuggled into typed compiler input."""
    data = {
        "authority_input": _authority_input().model_dump(mode="json"),
        "project_id": 7,
        "spec_version_id": 11,
        "specification_fingerprint": "sha256:" + "a" * 64,
        field: "# mutable prose",
    }

    with pytest.raises(ValidationError):
        SpecAuthorityCompilerInput.model_validate(data)
