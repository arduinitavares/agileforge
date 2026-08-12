"""Contract tests for typed v2 Authority output normalization."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

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
_SOURCE_STATEMENT = "The Authority payload MUST include typed_citation."


def _authority_input(*, eligible: bool = True) -> AuthorityInputV2:
    item = AuthorityItemV2(
        id=_SOURCE_ID,
        type="REQ",
        statement=_SOURCE_STATEMENT,
        level="MUST",
        acceptance=("Authority MUST include eligible_typed_item.",),
    )
    return AuthorityInputV2(
        artifact_id="SPEC.contract-normalizer",
        normative_items=(item,) if eligible else (),
        normative_relations=(),
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


def test_duplicate_placeholder_ids_with_unique_semantics_rewrite() -> None:
    """Repeated provider IDs may resolve only to one host semantic identity."""
    payload = _success_payload()
    payload["invariants"].append(
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": _SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "typed_citation"},
        }
    )
    payload["source_map"].append(
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": _SOURCE_STATEMENT,
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
    assert len(set(invariant_ids)) == 1
    assert [entry.invariant_id for entry in normalized.root.source_map] == invariant_ids


def test_ambiguous_duplicate_invariant_ids_fail_stably_under_permutation() -> None:
    """Unordered collections cannot positionally resolve one repeated provider ID."""
    forward = _success_payload()
    forward["invariants"].append(
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": _SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "eligible_typed_item"},
        }
    )
    forward["source_map"].append(
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Authority MUST include eligible_typed_item.",
            "location": _SOURCE_ID,
        }
    )
    reverse = deepcopy(forward)
    reverse["invariants"] = list(reversed(reverse["invariants"]))

    normalized_forward = normalize_compiler_output(
        json.dumps(forward),
        authority_input=_authority_input(),
    )
    normalized_reverse = normalize_compiler_output(
        json.dumps(reverse),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized_forward.root, SpecAuthorityCompilationFailure)
    assert isinstance(normalized_reverse.root, SpecAuthorityCompilationFailure)
    assert normalized_forward == normalized_reverse
    assert normalized_forward.root.reason == "JSON_VALIDATION_FAILED"
    assert "ambiguous repeated invariant identity" in (
        normalized_forward.root.blocking_gaps[0]
    )


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


def test_normalizer_rewrites_compact_ir_invariant_mapping_ids() -> None:
    """Compact IR keeps exact invariant lineage after host-owned ID rewriting."""
    payload = _success_payload()
    payload.update(
        {
            "ir_schema_version": "provider.ir.v1",
            "ir_provenance": "model_emitted",
            "authority_mappings": [
                {
                    "candidate_id": "CAND-provider",
                    "authority_item_id": "INV-0000000000000000",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "The candidate maps to the typed invariant.",
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.authority_mappings[0].authority_item_id == (
        normalized.root.invariants[0].id
    )


def test_normalizer_returns_closed_failure_when_canonicalization_is_invalid() -> None:
    """Host collection canonicalization cannot leak a model-validation exception."""
    payload = _success_payload()
    payload.update(
        {
            "rejected_features": ["Deferred export", "Deferred export"],
            "ir_schema_version": "provider.ir.v1",
            "ir_provenance": "model_emitted",
            "authority_mappings": [
                {
                    "candidate_id": "CAND-provider",
                    "authority_item_id": "REJ-2",
                    "authority_target_kind": "rejected_feature",
                    "mapping_status": "intentionally_classified",
                    "mapping_rationale": "The duplicate exclusion is classified.",
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "JSON_VALIDATION_FAILED"
    assert "ambiguous" in normalized.root.blocking_gaps[0]


@pytest.mark.parametrize(
    ("field", "values", "target_kind", "provider_id", "canonical_id"),
    [
        (
            "eligible_feature_rules",
            [{"rule": "Zulu eligibility"}, {"rule": "Alpha eligibility"}],
            "eligible_feature_rule",
            "EFR-1",
            "EFR-2",
        ),
        (
            "rejected_features",
            ["Zulu exclusion", "Alpha exclusion"],
            "rejected_feature",
            "REJ-1",
            "REJ-2",
        ),
        (
            "gaps",
            ["Zulu gap", "Alpha gap"],
            "gap",
            "GAP-1",
            "GAP-2",
        ),
        (
            "assumptions",
            [
                {"kind": "free_text", "text": "Zulu assumption"},
                {"kind": "free_text", "text": "Alpha assumption"},
            ],
            "assumption",
            "ASM-1",
            "ASM-2",
        ),
    ],
)
def test_normalizer_preserves_ordinal_mapping_semantics_across_sorting(
    field: str,
    values: list[object],
    target_kind: str,
    provider_id: str,
    canonical_id: str,
) -> None:
    """Set-like ordering cannot silently retarget provider-owned mappings."""
    payload = _success_payload()
    payload[field] = values
    payload.update(
        {
            "ir_schema_version": "provider.ir.v1",
            "ir_provenance": "model_emitted",
            "authority_mappings": [
                {
                    "candidate_id": "CAND-provider",
                    "authority_item_id": provider_id,
                    "authority_target_kind": target_kind,
                    "mapping_status": "covered",
                    "mapping_rationale": "The candidate maps to the Zulu item.",
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.authority_mappings[0].authority_item_id == canonical_id


def test_normalizer_is_identical_under_set_like_permutation_ties() -> None:
    """Case and whitespace ties cannot preserve provider collection order."""
    forward = _success_payload()
    forward.update(
        {
            "scope_themes": ["Alpha  theme", " alpha theme "],
            "eligible_feature_rules": [
                {"rule": "Alpha  eligibility"},
                {"rule": " alpha eligibility "},
            ],
            "rejected_features": ["Alpha  exclusion", " alpha exclusion "],
            "gaps": ["Alpha  gap", " alpha gap "],
            "assumptions": [
                {"kind": "free_text", "text": "Alpha assumption"},
                {"kind": "free_text", "text": "alpha assumption"},
            ],
        }
    )
    forward["invariants"].append(
        {
            "id": "INV-1111111111111111",
            "type": "REQUIRED_FIELD",
            "source_item_id": _SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "eligible_typed_item"},
        }
    )
    forward["source_map"].append(
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": " the authority payload must include typed_citation. ",
            "location": _SOURCE_ID,
        }
    )
    forward["source_map"].append(
        {
            "invariant_id": "INV-1111111111111111",
            "excerpt": "Authority MUST include eligible_typed_item.",
            "location": _SOURCE_ID,
        }
    )
    reverse = deepcopy(forward)
    for field in (
        "scope_themes",
        "invariants",
        "eligible_feature_rules",
        "rejected_features",
        "gaps",
        "assumptions",
        "source_map",
    ):
        reverse[field] = list(reversed(reverse[field]))

    normalized_forward = normalize_compiler_output(
        json.dumps(forward),
        authority_input=_authority_input(),
    )
    normalized_reverse = normalize_compiler_output(
        json.dumps(reverse),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized_forward.root, SpecAuthorityCompilationSuccess)
    assert isinstance(normalized_reverse.root, SpecAuthorityCompilationSuccess)
    assert normalized_forward.root.model_dump(
        mode="json"
    ) == normalized_reverse.root.model_dump(mode="json")


def test_normalizer_rejects_ambiguous_duplicate_ordinal_mapping() -> None:
    """A mapped duplicate cannot be assigned an arbitrary canonical ordinal."""
    payload = _success_payload()
    payload.update(
        {
            "gaps": ["Repeated gap", "Repeated gap"],
            "ir_schema_version": "provider.ir.v1",
            "ir_provenance": "model_emitted",
            "authority_mappings": [
                {
                    "candidate_id": "CAND-provider",
                    "authority_item_id": "GAP-1",
                    "authority_target_kind": "gap",
                    "mapping_status": "covered",
                    "mapping_rationale": "The candidate maps to one repeated gap.",
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "JSON_VALIDATION_FAILED"
    assert "ambiguous" in normalized.root.blocking_gaps[0]


def test_normalizer_rejects_mapping_target_kind_mismatch() -> None:
    """A provider cannot label a rejected feature as a gap mapping."""
    payload = _success_payload()
    payload.update(
        {
            "rejected_features": ["Deferred export"],
            "gaps": ["Missing documentation"],
            "ir_schema_version": "provider.ir.v1",
            "ir_provenance": "model_emitted",
            "authority_mappings": [
                {
                    "candidate_id": "CAND-provider",
                    "authority_item_id": "REJ-1",
                    "authority_target_kind": "gap",
                    "mapping_status": "covered",
                    "mapping_rationale": "The provider mislabels this exclusion.",
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(payload),
        authority_input=_authority_input(),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "JSON_VALIDATION_FAILED"
    assert "target kind gap is incompatible with REJ-1" in (
        normalized.root.blocking_gaps[0]
    )
