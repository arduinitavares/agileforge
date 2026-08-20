"""Contract tests for the typed Specification Authority compiler adapter."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from adapters.adk.prompts.specification import (
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from services.contracts.authority_input_v2 import (
    AuthorityInputV2,
    AuthorityItemV2,
    build_authority_input_v2,
)
from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH as CONTRACT_PROMPT_HASH,
)
from services.contracts.specification import (
    compute_invariant_id_from_payload,
    compute_prompt_hash,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from utils.spec_schemas import (
    DataContractParams,
    Invariant,
    InvariantType,
    MaxValueParams,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerInput,
    SpecAuthorityCompilerOutput,
    SpecAuthorityMapping,
)

_SOURCE_ID = "REQ.adapter.typed"
_SOURCE_STATEMENT = "The payload MUST include account_id."
_NON_NORMATIVE_SENTINEL = "NON_NORMATIVE_SENTINEL_MUST_NEVER_REACH_AUTHORITY"
_REGISTERED_SOURCE_SENTINELS: tuple[str, ...] = (
    "TO_SPEC_SOURCE_MARKDOWN_MUST_NEVER_REACH_AUTHORITY_PROVIDER",
    "CONTEXT_MD_MUST_NEVER_REACH_AUTHORITY_PROVIDER",
    "ADR_PROSE_MUST_NEVER_REACH_AUTHORITY_PROVIDER",
    "REPOSITORY_EVIDENCE_MUST_NEVER_REACH_AUTHORITY_PROVIDER",
)


def _authority_input() -> AuthorityInputV2:
    item = AuthorityItemV2(
        id=_SOURCE_ID,
        type="REQ",
        statement=_SOURCE_STATEMENT,
        level="MUST",
        acceptance=("Every accepted request has an account_id.",),
    )
    return AuthorityInputV2(
        artifact_id="SPEC.compiler-adapter",
        normative_items=(item,),
        normative_relations=(),
        eligible_item_ids=(_SOURCE_ID,),
        authority_input_fingerprint="sha256:" + ("b" * 64),
    )


def _success_payload() -> dict[str, Any]:
    return {
        "schema_version": "agileforge.compiled_authority.v3",
        "scope_themes": ["Typed Authority"],
        "domain": "specification",
        "invariants": [
            {
                "id": "INV-0123456789abcdef",
                "type": "REQUIRED_FIELD",
                "source_item_id": _SOURCE_ID,
                "source_level": "MUST",
                "parameters": {"field_name": "account_id"},
            }
        ],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0123456789abcdef",
                "excerpt": _SOURCE_STATEMENT,
                "location": _SOURCE_ID,
            }
        ],
        "compiler_version": SPEC_AUTHORITY_COMPILER_VERSION,
        "prompt_hash": SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    }


def _specification_with_non_normative_sentinel() -> SpecificationPayload:
    """Return full Specification prose containing one provider-exclusion marker."""
    return SpecificationPayload.model_validate(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.compiler-boundary",
            "title": "Compiler boundary",
            "summary": _NON_NORMATIVE_SENTINEL,
            "problem_statement": _NON_NORMATIVE_SENTINEL,
            "items": [
                {
                    "id": "GOAL.compiler.context",
                    "type": "GOAL",
                    "title": "Review context",
                    "statement": _NON_NORMATIVE_SENTINEL,
                    "rationale": _NON_NORMATIVE_SENTINEL,
                },
                {
                    "id": _SOURCE_ID,
                    "type": "REQ",
                    "title": "Account identity",
                    "statement": _SOURCE_STATEMENT,
                    "level": "MUST",
                    "verification": "integration-test",
                    "acceptance": ["Every accepted request has an account_id."],
                },
            ],
        }
    )


def test_prompt_and_service_contract_are_synchronized() -> None:
    """The adapter loads the exact host-hashed prompt and active version."""
    assert SPEC_AUTHORITY_COMPILER_VERSION == "4.0.2"
    assert SPEC_AUTHORITY_COMPILER_PROMPT_HASH == CONTRACT_PROMPT_HASH
    assert compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS) == (
        CONTRACT_PROMPT_HASH
    )


def test_prompt_documents_the_closed_typed_input_boundary() -> None:
    """Prompt semantics match the v2 DTO and its source-eligibility rules."""
    instructions = SPEC_AUTHORITY_COMPILER_INSTRUCTIONS

    assert '"agileforge.authority-compiler-input.v2"' in instructions
    assert '"agileforge.authority_input.v2"' in instructions
    assert "Only normative_items may authorize invariants" in instructions
    assert "review_context_ids" not in instructions
    assert "No non-normative prose or non-normative item identity" in instructions
    assert "eligible_item_ids as exhaustive" in instructions
    assert "Every source_map.location MUST equal" in instructions


def test_provider_contract_has_executable_tooling_constraint_guidance() -> None:
    """Prompt and schema make the unsupported tooling boundary unambiguous."""
    instructions = SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    normalized_instructions = " ".join(instructions.split())
    authority_item_schema = AuthorityItemV2.model_json_schema()["properties"]
    data_contract_schema = DataContractParams.model_json_schema()
    max_value_schema = MaxValueParams.model_json_schema()
    success_schema = SpecAuthorityCompilationSuccess.model_json_schema()[
        "properties"
    ]

    assert (
        "Tooling-only CONSTRAINT example (must become a gap)"
        in normalized_instructions
    )
    assert (
        "The implementation MUST target Python 3.13 or newer and manage the "
        "project exclusively with uv." in normalized_instructions
    )
    assert (
        '"CONSTRAINT.001: unsupported tooling requirement; enforce outside '
        'compiled Authority."' in normalized_instructions
    )
    assert (
        "Measurable CONSTRAINT example (may become MAX_VALUE)"
        in normalized_instructions
    )
    assert "The request limit MUST be at most 100." in normalized_instructions
    assert (
        "Every parameter string in the invariant is copied verbatim from its "
        "source item, except for the allowed identifier-style snake_case "
        "normalization." in normalized_instructions
    )
    assert "does not itself authorize an invariant type" in authority_item_schema[
        "type"
    ]["description"]
    assert "Do not use DATA_CONTRACT for tooling" in data_contract_schema[
        "description"
    ]
    assert "literal numeric maximum" in max_value_schema["description"]
    assert "begin with the exact eligible item ID" in success_schema["gaps"][
        "description"
    ]


def test_provider_contract_requires_distinct_temporary_invariant_references() -> None:
    """Provider-visible contracts prevent ambiguous multi-invariant references."""
    instructions = SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    invariant_id = Invariant.model_json_schema()["properties"]["id"]["description"]
    source_map_id = SourceMapEntry.model_json_schema()["properties"]["invariant_id"][
        "description"
    ]
    mapping_id = SpecAuthorityMapping.model_json_schema()["properties"][
        "authority_item_id"
    ].get("description", "")

    assert (
        "Every semantically distinct invariant MUST use a distinct temporary ID."
        in instructions
    )
    assert (
        "Every source_map.invariant_id and invariant-target "
        "authority_mappings.authority_item_id MUST use the exact temporary ID"
        in instructions
    )
    assert instructions.count('"id": "INV-0000000000000001"') == 1
    assert instructions.count('"invariant_id": "INV-0000000000000001"') == 1
    assert "Provider output must use a distinct temporary identity" in invariant_id
    assert "final deterministic identity" in invariant_id
    assert "temporary provider identity before host normalization" in source_map_id
    assert "final host identity afterward" in source_map_id
    assert "temporary provider identity before host normalization" in mapping_id
    assert "final host identity afterward" in mapping_id


@pytest.mark.parametrize(
    "invariant_type",
    [
        "FORBIDDEN_CAPABILITY",
        "REQUIRED_FIELD",
        "MAX_VALUE",
        "RELATION_CONSTRAINT",
        "USER_INTERACTION",
        "STATE_TRANSITION",
        "DATA_CONTRACT",
        "ROUTE_CONTRACT",
        "VISIBILITY_RULE",
    ],
)
def test_prompt_keeps_the_supported_authority_type_matrix(
    invariant_type: str,
) -> None:
    """The compiler still receives every supported Authority representation."""
    assert invariant_type in SPEC_AUTHORITY_COMPILER_INSTRUCTIONS


def test_compiler_input_accepts_the_typed_authority_projection() -> None:
    """The adapter input contains one canonical v2 projection plus host identity."""
    payload = SpecAuthorityCompilerInput(
        authority_input=_authority_input(),
        project_id=17,
        spec_version_id=23,
        specification_fingerprint="sha256:" + ("c" * 64),
    )

    assert payload.schema_version == "agileforge.authority-compiler-input.v2"
    assert payload.authority_input.schema_version == "agileforge.authority_input.v2"
    assert payload.authority_input.eligible_item_ids == (_SOURCE_ID,)


def test_serialized_compiler_input_excludes_non_normative_specification_prose() -> None:
    """Send only accepted typed clauses across the Authority provider boundary."""
    specification = _specification_with_non_normative_sentinel()
    structuring_only_provenance = {
        "registered_source": _REGISTERED_SOURCE_SENTINELS[0],
        "context": _REGISTERED_SOURCE_SENTINELS[1],
        "adrs": [_REGISTERED_SOURCE_SENTINELS[2]],
        "repository_evidence": _REGISTERED_SOURCE_SENTINELS[3],
    }
    authority_input = build_authority_input_v2(specification)
    provider_input = SpecAuthorityCompilerInput(
        authority_input=authority_input,
        project_id=17,
        spec_version_id=23,
        specification_fingerprint="sha256:" + ("c" * 64),
    )

    serialized = provider_input.model_dump_json()

    assert _NON_NORMATIVE_SENTINEL in specification.model_dump_json()
    assert all(
        sentinel in json.dumps(structuring_only_provenance)
        for sentinel in _REGISTERED_SOURCE_SENTINELS
    )
    assert _NON_NORMATIVE_SENTINEL not in serialized
    assert all(sentinel not in serialized for sentinel in _REGISTERED_SOURCE_SENTINELS)
    assert all(
        field not in serialized
        for field in ("registered_source", "context", "adrs", "repository_evidence")
    )
    assert "GOAL.compiler.context" not in serialized
    assert set(authority_input.model_dump(mode="json")) == {
        "schema_version",
        "artifact_id",
        "normative_items",
        "normative_relations",
        "eligible_item_ids",
        "authority_input_fingerprint",
    }
    assert "review_context_ids" not in serialized
    assert authority_input.normative_items == (
        AuthorityItemV2(
            id=_SOURCE_ID,
            type="REQ",
            statement=_SOURCE_STATEMENT,
            level="MUST",
            acceptance=("Every accepted request has an account_id.",),
        ),
    )


def test_compiler_input_is_closed_to_unknown_fields() -> None:
    """The agent-facing request cannot acquire an unreviewed side channel."""
    request = {
        "authority_input": _authority_input().model_dump(mode="json", by_alias=True),
        "project_id": 17,
        "spec_version_id": 23,
        "specification_fingerprint": "sha256:" + ("c" * 64),
        "unexpected": "not accepted",
    }

    with pytest.raises(ValidationError):
        SpecAuthorityCompilerInput.model_validate(request)


def test_success_and_envelope_schemas_accept_typed_authority() -> None:
    """Provider success remains compatible with stored compiled Authority v3."""
    direct = SpecAuthorityCompilerOutput.model_validate_json(
        json.dumps(_success_payload())
    )
    enveloped = SpecAuthorityCompilerEnvelope.model_validate(
        {"result": _success_payload()}
    )

    assert isinstance(direct.root, SpecAuthorityCompilationSuccess)
    assert isinstance(enveloped.result, SpecAuthorityCompilationSuccess)
    assert direct.root.invariants[0].source_item_id == _SOURCE_ID


def test_failure_schema_remains_structured_and_closed() -> None:
    """Impossible compilation returns the reviewable failure contract."""
    failure = {
        "schema_version": "agileforge.compiled_authority.v3",
        "error": "SPEC_COMPILATION_FAILED",
        "reason": "MODEL_BLOCKED",
        "blocking_gaps": [f"{_SOURCE_ID}: compiler unavailable"],
    }

    parsed = SpecAuthorityCompilerOutput.model_validate(failure)

    assert isinstance(parsed.root, SpecAuthorityCompilationFailure)
    with pytest.raises(ValidationError):
        SpecAuthorityCompilerOutput.model_validate({**failure, "unexpected": True})


def test_invariant_parameters_must_match_the_declared_type() -> None:
    """The output schema rejects semantically mismatched parameter objects."""
    payload = _success_payload()
    payload["invariants"][0]["type"] = "MAX_VALUE"

    with pytest.raises(ValidationError):
        SpecAuthorityCompilerOutput.model_validate(payload)


def test_semantic_id_changes_with_the_typed_source_identity() -> None:
    """Host-owned IDs distinguish identical semantics from different sources."""
    parameters = RequiredFieldParams(field_name="account_id")
    first = compute_invariant_id_from_payload(
        InvariantType.REQUIRED_FIELD,
        parameters,
        source_item_id="REQ.first",
        source_level="MUST",
    )
    second = compute_invariant_id_from_payload(
        InvariantType.REQUIRED_FIELD,
        parameters,
        source_item_id="REQ.second",
        source_level="MUST",
    )

    assert first != second
    assert first == compute_invariant_id_from_payload(
        InvariantType.REQUIRED_FIELD,
        parameters,
        source_item_id="REQ.first",
        source_level="MUST",
    )
