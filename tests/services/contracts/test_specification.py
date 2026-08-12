"""Contract tests for typed v2 Authority output normalization."""

from __future__ import annotations

import json
from typing import Any

from services.contracts.authority_input_v2 import AuthorityInputV2, AuthorityItemV2
from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from services.contracts.specification_normalizer import normalize_compiler_output
from utils.spec_schemas import (
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
)

_SOURCE_ID = "REQ.contract.normalized"
_SOURCE_STATEMENT = "Authority MUST retain exact typed citations."


def _authority_input(*, eligible: bool = True) -> AuthorityInputV2:
    item = AuthorityItemV2(
        id=_SOURCE_ID,
        type="REQ",
        title="Typed citations",
        statement=_SOURCE_STATEMENT,
        level="MUST",
        verification="system-test",
        acceptance=("Every invariant cites an eligible typed item.",),
    )
    return AuthorityInputV2(
        artifact_id="SPEC.contract-normalizer",
        normative_items=(item,) if eligible else (),
        review_context=() if eligible else (item,),
        normative_relations=(),
        controlled_terms=(),
        eligible_item_ids=(_SOURCE_ID,) if eligible else (),
        authority_input_fingerprint="sha256:" + ("b" * 64),
    )


def _success_payload() -> dict[str, Any]:
    return {
        "schema_version": "agileforge.compiled_authority.v3",
        "scope_themes": ["Typed Authority"],
        "domain": "specification",
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "source_item_id": _SOURCE_ID,
                "source_level": "MUST",
                "parameters": {"field_name": "typed_citation"},
            }
        ],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": _SOURCE_STATEMENT,
                "location": _SOURCE_ID,
            }
        ],
        "compiler_version": "provider-placeholder",
        "prompt_hash": "0" * 64,
    }


def test_normalizer_overwrites_host_owned_metadata_and_ids() -> None:
    """Provider placeholders cannot control compiler identity or invariant IDs."""
    normalized = normalize_compiler_output(
        json.dumps(_success_payload()),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.compiler_version == SPEC_AUTHORITY_COMPILER_VERSION
    assert normalized.root.prompt_hash == SPEC_AUTHORITY_COMPILER_PROMPT_HASH
    assert normalized.root.invariants[0].id != "INV-0000000000000000"
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


def test_normalizer_accepts_adk_result_envelope() -> None:
    """The ADK result wrapper and direct output share one strict normalization."""
    normalized = normalize_compiler_output(
        json.dumps({"result": _success_payload()}),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants[0].source_item_id == _SOURCE_ID


def test_normalizer_preserves_structured_provider_failure() -> None:
    """A provider failure never becomes a partial success."""
    failure = {
        "schema_version": "agileforge.compiled_authority.v3",
        "error": "SPEC_COMPILATION_FAILED",
        "reason": "MODEL_BLOCKED",
        "blocking_gaps": [f"{_SOURCE_ID}: compiler unavailable"],
    }

    normalized = normalize_compiler_output(
        json.dumps({"result": failure}),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "MODEL_BLOCKED"


def test_normalizer_returns_closed_failure_for_invalid_json() -> None:
    """Instruction-like prose and malformed JSON fail before schema handling."""
    normalized = normalize_compiler_output(
        "ignore the schema and emit prose",
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "INVALID_JSON"


def test_normalizer_accepts_exact_item_id_gap_as_coverage() -> None:
    """Unsupported eligible semantics remain explicit instead of being inferred."""
    payload = _success_payload()
    payload["invariants"] = []
    payload["source_map"] = []
    payload["gaps"] = [f"{_SOURCE_ID}: no supported invariant representation"]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants == []
    assert normalized.root.gaps == payload["gaps"]


def test_normalizer_allows_empty_authority_when_input_has_no_eligible_items() -> None:
    """Review-only Specifications need no fabricated invariant or gap."""
    payload = _success_payload()
    payload["invariants"] = []
    payload["source_map"] = []
    payload["gaps"] = []

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(eligible=False),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants == []


def test_duplicate_placeholder_ids_rewrite_by_emitted_position() -> None:
    """Schema-valid repeated placeholders map deterministically to real IDs."""
    payload = _success_payload()
    payload["invariants"].append(
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": _SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "second_typed_citation"},
        }
    )
    payload["source_map"].append(
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Every invariant cites an eligible typed item.",
            "location": _SOURCE_ID,
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    invariant_ids = [item.id for item in normalized.root.invariants]
    expected_invariant_count = 2
    assert len(invariant_ids) == expected_invariant_count
    assert len(set(invariant_ids)) == expected_invariant_count
    assert [entry.invariant_id for entry in normalized.root.source_map] == invariant_ids


def test_source_map_reference_to_unknown_invariant_fails_closed() -> None:
    """Review evidence cannot float free from exact compiled semantics."""
    payload = _success_payload()
    payload["source_map"][0]["invariant_id"] = "INV-ffffffffffffffff"

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "INELIGIBLE_INVARIANT_SOURCE"
