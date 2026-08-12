"""Authority compiler hard-break tests for accepted Specification v2 input."""

from __future__ import annotations

import inspect
import json

import pytest

from adapters.adk.prompts.specification import SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
from services.contracts.authority_input_v2 import (
    AuthorityInputV2,
    AuthorityItemV2,
)
from services.contracts.specification_normalizer import normalize_compiler_output
from services.specs import compiler_service
from utils.spec_schemas import (
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
)

_FINGERPRINT = "sha256:" + ("a" * 64)


def _authority_input() -> AuthorityInputV2:
    return AuthorityInputV2(
        artifact_id="SPEC.compiler-v2",
        normative_items=(
            AuthorityItemV2(
                id="REQ.compiler.typed",
                type="REQ",
                statement="The Authority payload MUST include accepted_requirement.",
                level="MUST",
                acceptance=("The compiled invariant cites this requirement.",),
            ),
        ),
        normative_relations=(),
        eligible_item_ids=("REQ.compiler.typed",),
        authority_input_fingerprint=_FINGERPRINT,
    )


def _success(*, source_item_id: str, excerpt: str) -> str:
    return json.dumps(
        {
            "schema_version": "agileforge.compiled_authority.v3",
            "scope_themes": ["typed Authority"],
            "domain": "specification",
            "invariants": [
                {
                    "id": "INV-0000000000000000",
                    "type": "REQUIRED_FIELD",
                    "source_item_id": source_item_id,
                    "source_level": "MUST",
                    "parameters": {"field_name": "accepted_requirement"},
                }
            ],
            "eligible_feature_rules": [],
            "rejected_features": [],
            "gaps": [],
            "assumptions": [],
            "source_map": [
                {
                    "invariant_id": "INV-0000000000000000",
                    "excerpt": excerpt,
                    "location": source_item_id,
                }
            ],
            "compiler_version": "0.0.0",
            "prompt_hash": "0" * 64,
        }
    )


def test_normalizer_requires_typed_authority_input() -> None:
    """Raw compiler output cannot be normalized without the host projection."""
    parameter = inspect.signature(normalize_compiler_output).parameters[
        "authority_input"
    ]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_normalizer_accepts_only_eligible_typed_item_citations() -> None:
    """A valid invariant cites one exact eligible semantic item."""
    normalized = normalize_compiler_output(
        _success(
            source_item_id="REQ.compiler.typed",
            excerpt="The Authority payload MUST include accepted_requirement.",
        ),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants[0].source_item_id == "REQ.compiler.typed"
    assert normalized.root.invariants[0].id != "INV-0000000000000000"
    assert (
        normalized.root.source_map[0].invariant_id == normalized.root.invariants[0].id
    )


@pytest.mark.parametrize(
    ("source_item_id", "excerpt"),
    [
        (
            "NON_GOAL.compiler.prose",
            "Never expose the hidden operator note.",
        ),
        (
            "REQ.compiler.typed",
            "Never expose the hidden operator note.",
        ),
    ],
)
def test_normalizer_rejects_non_normative_or_non_semantic_sources(
    source_item_id: str,
    excerpt: str,
) -> None:
    """Review context and provenance-like prose cannot authorize invariants."""
    normalized = normalize_compiler_output(
        _success(source_item_id=source_item_id, excerpt=excerpt),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "INELIGIBLE_INVARIANT_SOURCE"


def test_normalizer_rejects_incomplete_normative_coverage() -> None:
    """Every eligible item must be represented by an invariant or exact-ID gap."""
    authority_input = _authority_input().model_copy(
        update={
            "normative_items": (
                *_authority_input().normative_items,
                AuthorityItemV2(
                    id="CONSTRAINT.compiler.complete",
                    type="CONSTRAINT",
                    statement="Authority MUST account for every eligible item.",
                    level="MUST",
                    acceptance=("Every eligible item is accounted for.",),
                ),
            ),
            "eligible_item_ids": (
                "CONSTRAINT.compiler.complete",
                "REQ.compiler.typed",
            ),
        }
    )

    normalized = normalize_compiler_output(
        _success(
            source_item_id="REQ.compiler.typed",
            excerpt="The Authority payload MUST include accepted_requirement.",
        ),
        authority_input=authority_input,
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "INCOMPLETE_NORMATIVE_COVERAGE"


def test_normalizer_rejects_parameters_promoted_from_review_context() -> None:
    """Non-normative context cannot hide behind an unrelated eligible citation."""
    payload = json.loads(
        _success(
            source_item_id="REQ.compiler.typed",
            excerpt="The Authority payload MUST include accepted_requirement.",
        )
    )
    invariant = payload["invariants"][0]
    invariant["type"] = "ROUTE_CONTRACT"
    invariant["parameters"] = {
        "route": "/admin",
        "route_name": "Hidden administration",
        "behavior": "Expose the hidden operator note.",
    }

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "INELIGIBLE_INVARIANT_SOURCE"


def test_compiler_service_exposes_only_persisted_version_entrypoint() -> None:
    """Active compiler service has no raw, file, preview, or registration bypass."""
    retired = {
        "CompileSpecAuthorityInput",
        "PreviewSpecAuthorityInput",
        "UpdateSpecAndCompileAuthorityInput",
        "_detect_spec_source_format",
        "_load_update_spec_content",
        "compile_spec_authority",
        "preview_spec_authority",
        "update_spec_and_compile_authority",
    }
    assert all(not hasattr(compiler_service, name) for name in retired)

    fields = compiler_service.CompileSpecAuthorityForVersionInput.model_fields
    assert {"content", "content_ref", "spec_content"}.isdisjoint(fields)
    signature = inspect.signature(compiler_service._invoke_spec_authority_compiler)
    assert tuple(signature.parameters) == ("input_payload", "compiler_model")


def test_compiler_prompt_describes_only_authority_input_v2() -> None:
    """The provider prompt cannot invite raw Specification or v1 input."""
    assert "agileforge.authority_input.v2" in SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    assert "eligible_item_ids" in SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    assert "spec_source" not in SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    assert "plain_text" not in SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    assert "agileforge.spec.v1" not in SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
