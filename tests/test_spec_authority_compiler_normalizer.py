"""Unit tests for host-side normalization of spec authority compiler outputs."""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator_agent.agent_tools.spec_authority_compiler_agent.compiler_contract import (  # noqa: E501
    compute_invariant_id,
    compute_prompt_hash,
)
from utils.spec_schemas import (
    InvariantType,
    RequiredFieldParams,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
)


def _structured_spec_source() -> str:
    """Return canonical AgileForge profile JSON with two normative items."""
    from utils.agileforge_spec_profile import (  # noqa: PLC0415
        TechnicalSpecArtifact,
        canonical_spec_json,
    )

    artifact = TechnicalSpecArtifact.model_validate(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.normalizer",
            "title": "Normalizer Structured Spec",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-05-19",
            "updated_at": "2026-05-19",
            "summary": "Exercise structured spec authority normalization.",
            "problem_statement": "Structured specs need profile-aware traceability.",
            "items": [
                {
                    "id": "REQ.audit-evidence",
                    "type": "REQ",
                    "status": "accepted",
                    "title": "Audit evidence",
                    "statement": "The system MUST record audit evidence.",
                    "level": "MUST",
                    "verification": "system-test",
                    "acceptance": [
                        "Audit evidence is stored for each operation."
                    ],
                },
                {
                    "id": "REQ.review-token",
                    "type": "REQ",
                    "status": "accepted",
                    "title": "Review token",
                    "statement": "The system MUST include review token evidence.",
                    "level": "MUST",
                    "verification": "inspection",
                    "acceptance": [
                        "Review packets include review token evidence."
                    ],
                },
            ],
        }
    )
    return canonical_spec_json(artifact)


def _legacy_success_payload() -> dict[str, Any]:
    return {
        "scope_themes": ["payload validation"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-aaaaaaaaaaaaaaaa",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-aaaaaaaaaaaaaaaa",
                "excerpt": "The payload must include user_id.",
                "location": "spec:line:1",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }


def _compact_ir_success_payload() -> dict[str, Any]:
    payload = _legacy_success_payload()
    quote_hash = "sha256:" + ("1" * 64)
    payload.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "source_units": [
                {
                    "unit_id": "SRC-aaaaaaaaaaaa-bbbbbbbbbbbb-1",
                    "section_id": "S1",
                    "heading_path": ["Requirements"],
                    "kind": "paragraph",
                    "line_start": 1,
                    "line_end": 1,
                    "text_hash": "sha256:" + ("2" * 64),
                    "text_excerpt": "The payload must include user_id.",
                    "disposition": "candidate_extracted",
                    "disposition_reason": "normative requirement",
                }
            ],
            "requirement_candidates": [
                {
                    "candidate_id": "REQ-aaaaaaaaaaaaaaaa",
                    "source_unit_id": "SRC-aaaaaaaaaaaa-bbbbbbbbbbbb-1",
                    "statement": "The payload must include user_id.",
                    "source_quote": "The payload must include user_id.",
                    "quote_hash": quote_hash,
                    "line_start": 1,
                    "line_end": 1,
                    "classification": "requirement",
                    "provenance": "model_emitted",
                }
            ],
            "authority_mappings": [
                {
                    "candidate_id": "REQ-aaaaaaaaaaaaaaaa",
                    "authority_item_id": "INV-aaaaaaaaaaaaaaaa",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": (
                        "Exact quote maps to required field invariant."
                    ),
                    "source_quote_hash": quote_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
            "ir_packet_limits": {
                "max_candidates": 50,
                "max_findings": 50,
                "max_excerpt_bytes": 2000,
                "truncated": False,
            },
        }
    )
    return payload


def _test_compact_ir_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _test_generated_gap_id(
    candidate_id: str,
    finding_code: str,
    text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "finding_code": finding_code,
        "normalized_gap_text": " ".join(text.strip().split()),
    }
    return f"GAP-{_test_compact_ir_hash(payload)}"


def _test_generated_assumption_id(candidate_id: str, text: str) -> str:
    payload = {
        "candidate_id": candidate_id,
        "normalized_assumption_text": " ".join(text.strip().split()),
        "target_kind": "assumption",
    }
    return f"ASM-{_test_compact_ir_hash(payload)}"


def _test_generated_target_id(
    prefix: str,
    candidate_id: str,
    target_kind: str,
    text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "normalized_text": " ".join(text.strip().split()),
        "target_kind": target_kind,
    }
    return f"{prefix}-{_test_compact_ir_hash(payload)}"


def test_legacy_success_without_ir_stays_valid() -> None:
    """Historical compiled authority JSON without compact IR remains loadable."""
    success = SpecAuthorityCompilationSuccess.model_validate(_legacy_success_payload())

    assert success.rejected_features == []
    assert success.ir_schema_version is None
    assert success.ir_provenance is None
    assert success.source_units == []
    assert success.requirement_candidates == []
    assert success.authority_mappings == []
    assert success.ir_packet_limits is None


def test_success_schema_accepts_compact_ir_with_provenance() -> None:
    """Model-emitted compact IR with explicit provenance loads successfully."""
    success = SpecAuthorityCompilationSuccess.model_validate(
        _compact_ir_success_payload()
    )

    assert success.ir_schema_version == "authority-ir-v1"
    assert success.ir_provenance == "model_emitted"
    assert success.source_units[0].unit_id == "SRC-aaaaaaaaaaaa-bbbbbbbbbbbb-1"
    assert success.requirement_candidates[0].source_unit_id == (
        "SRC-aaaaaaaaaaaa-bbbbbbbbbbbb-1"
    )
    assert success.authority_mappings[0].authority_item_id == "INV-aaaaaaaaaaaaaaaa"


def test_success_schema_rejects_mapping_with_missing_candidate_id() -> None:
    """Mappings must reference requirement candidates in the same artifact."""
    payload = _compact_ir_success_payload()
    payload["authority_mappings"][0]["candidate_id"] = "REQ-missing"

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_schema_accepts_manifest_mapping_without_repeated_candidate() -> None:
    """Raw model hints may point at host manifest candidates not repeated in output."""
    payload = _compact_ir_success_payload()
    payload["source_units"] = []
    payload["requirement_candidates"] = []
    payload["authority_mappings"][0]["candidate_id"] = "REQ-host-manifest"

    success = SpecAuthorityCompilationSuccess.model_validate(payload)

    assert success.authority_mappings[0].candidate_id == "REQ-host-manifest"


def test_success_schema_rejects_candidate_with_missing_source_unit_id() -> None:
    """Candidates must reference source units in the same artifact."""
    payload = _compact_ir_success_payload()
    payload["requirement_candidates"][0]["source_unit_id"] = "SRC-missing"

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_success_schema_rejects_mapping_with_missing_authority_item_id() -> None:
    """Mappings must reference authority items in the compiled artifact."""
    payload = _compact_ir_success_payload()
    payload["authority_mappings"][0]["authority_item_id"] = "INV-bbbbbbbbbbbbbbbb"

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_success_schema_allows_mapping_target_kind_mismatch_for_review_gate() -> None:
    """Target-kind mismatches are review findings, not schema-fatal errors."""
    payload = _compact_ir_success_payload()
    payload["authority_mappings"][0]["authority_target_kind"] = "gap"

    success = SpecAuthorityCompilationSuccess.model_validate(payload)

    assert success.authority_mappings[0].authority_target_kind == "gap"


def test_success_schema_accepts_rejected_feature_mapping() -> None:
    """Rejected feature mappings validate against rejected feature targets."""
    payload = _compact_ir_success_payload()
    payload["rejected_features"] = ["Do not expose access tokens."]
    payload["authority_mappings"][0]["authority_item_id"] = "RF-1"
    payload["authority_mappings"][0]["authority_target_kind"] = "rejected_feature"

    success = SpecAuthorityCompilationSuccess.model_validate(payload)

    assert success.rejected_features == ["Do not expose access tokens."]
    assert success.authority_mappings[0].authority_item_id == "RF-1"


def test_success_schema_accepts_generated_non_invariant_mapping_ids() -> None:
    """Content-derived non-invariant target IDs validate independent of order."""
    payload = _compact_ir_success_payload()
    candidate_id = payload["requirement_candidates"][0]["candidate_id"]
    gap_text = "No invariant exists for the requirement."
    assumption_text = "Operators configure this outside AgileForge."
    eligible_rule = "Future phase support is allowed after validation."
    rejected_feature = "Do not expose access tokens."
    payload["gaps"] = [gap_text]
    payload["assumptions"] = [assumption_text]
    payload["eligible_feature_rules"] = [{"rule": eligible_rule}]
    payload["rejected_features"] = [rejected_feature]
    payload["authority_mappings"] = [
        {
            "candidate_id": candidate_id,
            "authority_item_id": _test_generated_gap_id(
                candidate_id,
                "AUTHORITY_CANDIDATE_UNCOVERED",
                gap_text,
            ),
            "authority_target_kind": "gap",
            "mapping_status": "weak_mapping",
            "mapping_rationale": "Gap records missing canonical authority.",
            "source_quote_hash": None,
            "mapping_provenance": "model_quote",
        },
        {
            "candidate_id": candidate_id,
            "authority_item_id": _test_generated_assumption_id(
                candidate_id,
                assumption_text,
            ),
            "authority_target_kind": "assumption",
            "mapping_status": "intentionally_classified",
            "mapping_rationale": "Assumption records unresolved context.",
            "source_quote_hash": None,
            "mapping_provenance": "model_quote",
        },
        {
            "candidate_id": candidate_id,
            "authority_item_id": _test_generated_target_id(
                "EFR",
                candidate_id,
                "eligible_feature_rule",
                eligible_rule,
            ),
            "authority_target_kind": "eligible_feature_rule",
            "mapping_status": "covered",
            "mapping_rationale": "Eligible feature rule constrains future scope.",
            "source_quote_hash": None,
            "mapping_provenance": "model_quote",
        },
        {
            "candidate_id": candidate_id,
            "authority_item_id": _test_generated_target_id(
                "RF",
                candidate_id,
                "rejected_feature",
                rejected_feature,
            ),
            "authority_target_kind": "rejected_feature",
            "mapping_status": "covered",
            "mapping_rationale": "Rejected feature blocks unsafe scope.",
            "source_quote_hash": None,
            "mapping_provenance": "model_quote",
        },
    ]

    success = SpecAuthorityCompilationSuccess.model_validate(payload)

    assert len(success.authority_mappings) == 4  # noqa: PLR2004


def test_compact_ir_fields_do_not_require_or_persist_full_source_text() -> None:
    """Compact IR keeps excerpts and rejects full source text fields."""
    success = SpecAuthorityCompilationSuccess.model_validate(
        _compact_ir_success_payload()
    )
    dumped = success.model_dump(mode="json")

    assert "source_text" not in dumped["source_units"][0]
    assert "full_source_text" not in dumped["source_units"][0]

    payload = _compact_ir_success_payload()
    payload["source_units"][0]["source_text"] = "The full specification source."

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_success_schema_rejects_compact_ir_without_provenance() -> None:
    """Any compact IR payload requires explicit IR provenance."""
    payload = _compact_ir_success_payload()
    payload.pop("ir_provenance")

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_success_schema_rejects_trusted_review_findings() -> None:
    """Compiler output cannot persist trusted review findings."""
    payload = _compact_ir_success_payload()
    payload["review_findings"] = [
        {
            "code": "AUTHORITY_COVERAGE_COMPLETE",
            "severity": "info",
            "message": "Model says coverage is complete.",
        }
    ]

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_success_schema_rejects_unbounded_compact_ir_excerpt() -> None:
    """Compact IR excerpts must stay bounded and not store full source text."""
    payload = _compact_ir_success_payload()
    payload["source_units"][0]["text_excerpt"] = "x" * 2_001

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_normalizer_rewrites_bad_ids_from_llm() -> None:
    """Normalizer must rewrite bad prompt_hash and invariant IDs deterministically."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
        SPEC_AUTHORITY_COMPILER_VERSION,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    excerpt = "The payload must include user_id."

    raw: dict[str, Any] = {
        "scope_themes": [],
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt,
                "location": "spec:line:1",
            }
        ],
        "compiler_version": "0.0.0",
        "prompt_hash": "b" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)

    expected_prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
    assert normalized.root.prompt_hash == expected_prompt_hash
    assert normalized.root.compiler_version == SPEC_AUTHORITY_COMPILER_VERSION

    assert len(normalized.root.invariants) == 1
    inv = normalized.root.invariants[0]
    assert inv.type == InvariantType.REQUIRED_FIELD
    assert isinstance(inv.parameters, RequiredFieldParams)
    assert inv.parameters.field_name == "user_id"

    # ID must be derived from the source_map excerpt
    assert len(normalized.root.source_map) == 1
    sm = normalized.root.source_map[0]
    expected_id = compute_invariant_id(sm.excerpt, inv.type, inv.parameters)
    assert inv.id == expected_id
    assert sm.invariant_id == expected_id
    assert re.match(r"^INV-[0-9a-f]{16}$", inv.id)


def test_exact_legacy_source_map_quote_stays_host_inferred() -> None:
    """Legacy source_map quotes do not become trusted model IR by themselves."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = "\n".join(
        [
            "# Requirements",
            "- The payload must include user_id.",
            "- The payload must include account_id.",
        ]
    )
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = "- The payload must include user_id."

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert success.ir_provenance == "host_parsed"
    assert success.ir_provenance != "model_emitted"
    assert len(success.requirement_candidates) == 2  # noqa: PLR2004
    assert {mapping.mapping_provenance for mapping in success.authority_mappings} == {
        "host_inferred"
    }
    assert {mapping.mapping_status for mapping in success.authority_mappings} == {
        "weak_mapping"
    }
    mapped_candidate_ids = {
        mapping.candidate_id for mapping in success.authority_mappings
    }
    assert len(mapped_candidate_ids) == 1
    assert any(
        candidate.candidate_id not in mapped_candidate_ids
        for candidate in success.requirement_candidates
    )


def test_legacy_without_source_text_marks_ir_absent_not_model_emitted() -> None:
    """Legacy compiler output without current source cannot become model IR."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    normalized = normalize_compiler_output(json.dumps(_legacy_success_payload()))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.ir_provenance == "legacy_absent"
    assert normalized.root.ir_provenance != "model_emitted"
    assert normalized.root.source_units == []
    assert normalized.root.requirement_candidates == []
    assert normalized.root.authority_mappings == []


def test_unrelated_source_refs_become_weak_mappings() -> None:
    """Host-repaired mappings stay weak when the model cited unrelated source."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = "\n".join(
        [
            "# Requirements",
            "- The payload must include user_id.",
            "- The payload must include account_id.",
        ]
    )
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = "- The payload must include account_id."

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert success.ir_provenance == "host_parsed"
    assert success.source_map[0].excerpt == "- The payload must include user_id."
    assert len(success.authority_mappings) == 1
    mapping = success.authority_mappings[0]
    assert mapping.mapping_provenance == "host_repaired_quote"
    assert mapping.mapping_provenance != "model_quote"
    assert mapping.mapping_status == "weak_mapping"


def test_normalizer_keeps_repairable_invariants_and_drops_unsupported_ones() -> None:
    """One unsupported invariant must not prevent review of repairable authority."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = "\n".join(
        [
            "| ID | Requirement | Acceptance Criteria |",
            "| FR-001 | The system must recommend one live squad. | "
            "A live run outputs exactly one selected squad, formation, and captain. |",
            "| FR-003 | The live squad must stay within budget. | "
            "budget_used <= budget. |",
        ]
    )
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-1111111111111111",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "selected squad"},
        },
        {
            "id": "INV-2222222222222222",
            "type": "MAX_VALUE",
            "parameters": {"field_name": "budget_used", "max_value": 0},
        },
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-1111111111111111",
            "excerpt": "FR-001 | The system must recommend one live squad.",
            "location": "FR-001",
        },
        {
            "invariant_id": "INV-2222222222222222",
            "excerpt": "FR-003 | budget_used <= budget.",
            "location": "FR-003",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert [inv.type for inv in success.invariants] == [InvariantType.REQUIRED_FIELD]
    assert success.source_map[0].excerpt.endswith(
        "selected squad, formation, and captain. |"
    )
    assert any("Dropped unsupported compiler invariant" in gap for gap in success.gaps)
    assert "0" in " ".join(success.gaps)


def test_normalizer_treats_model_target_kind_mismatch_as_untrusted_ir_hint() -> None:
    """Invalid model mapping hints should not make compiler invocation fail."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_quote = "- The payload must include user_id."
    quote_hash = f"sha256:{hashlib.sha256(source_quote.encode()).hexdigest()}"
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = source_quote
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "source_units": [
                {
                    "unit_id": "SRC-model",
                    "section_id": "SEC-model",
                    "heading_path": ["Requirements"],
                    "kind": "list_item",
                    "line_start": 2,
                    "line_end": 2,
                    "text_hash": quote_hash,
                    "text_excerpt": source_quote,
                    "disposition": "candidate_extracted",
                    "disposition_reason": "model supplied",
                }
            ],
            "requirement_candidates": [
                {
                    "candidate_id": "CAND-model",
                    "source_unit_id": "SRC-model",
                    "statement": source_quote,
                    "source_quote": source_quote,
                    "quote_hash": quote_hash,
                    "line_start": 2,
                    "line_end": 2,
                    "classification": "requirement",
                    "provenance": "model_emitted",
                }
            ],
            "authority_mappings": [
                {
                    "candidate_id": "CAND-model",
                    "authority_item_id": "INV-aaaaaaaaaaaaaaaa",
                    "authority_target_kind": "eligible_feature_rule",
                    "mapping_status": "covered",
                    "mapping_rationale": "model kind typo",
                    "source_quote_hash": quote_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text="\n".join(["# Requirements", source_quote]),
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.authority_mappings
    assert {
        mapping.mapping_provenance for mapping in normalized.root.authority_mappings
    } != {"model_quote"}


def test_model_emitted_exact_quote_mapping_is_validated_against_host_ir() -> None:
    """Exact model quote hints are re-keyed to host candidates and normalized IDs."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_quote = "- The payload must include user_id."
    source_text = "\n".join(["# Requirements", source_quote])
    quote_hash = f"sha256:{hashlib.sha256(source_quote.encode()).hexdigest()}"
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = source_quote
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "source_units": [
                {
                    "unit_id": "SRC-model",
                    "section_id": "S1",
                    "heading_path": ["Requirements"],
                    "kind": "list_item",
                    "line_start": 2,
                    "line_end": 2,
                    "text_hash": quote_hash,
                    "text_excerpt": source_quote,
                    "disposition": "candidate_extracted",
                    "disposition_reason": None,
                }
            ],
            "requirement_candidates": [
                {
                    "candidate_id": "REQ-model",
                    "source_unit_id": "SRC-model",
                    "statement": source_quote,
                    "source_quote": source_quote,
                    "quote_hash": quote_hash,
                    "line_start": 2,
                    "line_end": 2,
                    "classification": "requirement",
                    "provenance": "model_emitted",
                }
            ],
            "authority_mappings": [
                {
                    "candidate_id": "REQ-model",
                    "authority_item_id": "INV-aaaaaaaaaaaaaaaa",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Exact quote maps to required field.",
                    "source_quote_hash": quote_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
            "ir_packet_limits": {
                "max_candidates": 1,
                "max_findings": 0,
                "max_excerpt_bytes": 2000,
                "truncated": False,
            },
        }
    )

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert success.ir_provenance == "mixed"
    assert len(success.authority_mappings) == 1
    mapping = success.authority_mappings[0]
    assert mapping.authority_item_id == success.invariants[0].id
    assert mapping.mapping_provenance == "model_quote"
    assert mapping.mapping_status == "covered"


def test_structured_profile_ir_uses_typed_items_not_canonical_json_blob() -> None:
    """Profile JSON should become item-level IR, not one legacy Markdown blob."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = _structured_spec_source()
    source_quote = "The system MUST record audit evidence."
    raw = _legacy_success_payload()
    raw["invariants"][0]["parameters"] = {"field_name": "audit_evidence"}
    raw["source_map"][0]["excerpt"] = source_quote
    raw["source_map"][0]["location"] = "REQ.audit-evidence.statement"

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert len(success.source_units) > 1
    assert len(success.requirement_candidates) > 1
    assert all(
        not candidate.source_quote.lstrip().startswith("{")
        for candidate in success.requirement_candidates
    )
    assert success.source_map[0].excerpt == source_quote
    assert success.source_map[0].location == "REQ.audit-evidence.statement"
    assert success.authority_mappings[0].mapping_provenance == "model_quote"
    assert success.authority_mappings[0].mapping_status == "covered"


def test_structured_profile_json_blob_source_map_repairs_to_item_quote() -> None:
    """A model JSON-blob citation is repaired to profile evidence but stays weak."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = _structured_spec_source()
    raw = _legacy_success_payload()
    raw["invariants"][0]["parameters"] = {"field_name": "audit_evidence"}
    raw["source_map"][0]["excerpt"] = source_text
    raw["source_map"][0]["location"] = "spec.json"

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert success.source_map[0].excerpt == "The system MUST record audit evidence."
    assert not success.source_map[0].excerpt.lstrip().startswith("{")
    assert success.authority_mappings[0].mapping_provenance == "host_repaired_quote"
    assert success.authority_mappings[0].mapping_status == "weak_mapping"


def test_structured_profile_allows_missing_source_map() -> None:
    """Structured authority no longer requires source_map for IDs."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _compact_ir_success_payload()
    del raw["source_map"]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map == []
    assert normalized.root.ir_schema_version is None
    assert normalized.root.ir_provenance is None
    assert normalized.root.source_units == []
    assert normalized.root.requirement_candidates == []
    assert normalized.root.authority_mappings == []
    assert normalized.root.ir_packet_limits is None
    assert normalized.root.invariants[0].id.startswith("INV-")


def test_structured_profile_duplicate_placeholder_source_map_rewrites_by_position() -> (
    None
):
    """Duplicate original source-map IDs preserve positional invariant evidence."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    placeholder_id = "INV-0000000000000000"
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": placeholder_id,
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "audit_evidence"},
        },
        {
            "id": placeholder_id,
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "review_token"},
        },
    ]
    raw["source_map"] = [
        {
            "invariant_id": placeholder_id,
            "excerpt": "The system MUST record audit evidence.",
            "location": "REQ.audit-evidence.statement",
        },
        {
            "invariant_id": placeholder_id,
            "excerpt": "The system MUST include review token evidence.",
            "location": "REQ.review-token.statement",
        },
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    normalized_invariant_ids = [inv.id for inv in normalized.root.invariants]
    source_map_ids = [entry.invariant_id for entry in normalized.root.source_map]
    assert len(source_map_ids) == 2  # noqa: PLR2004
    assert source_map_ids == normalized_invariant_ids
    assert len(set(source_map_ids)) == 2  # noqa: PLR2004


def test_structured_profile_keeps_unrelated_source_map_as_review_evidence() -> None:
    """Structured mode does not reject semantically weak source excerpts."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = "This sentence is review evidence only."
    raw["source_map"][0]["location"] = "REQ.audit-evidence.statement"

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map[0].location == "REQ.audit-evidence.statement"
    assert (
        normalized.root.source_map[0].excerpt
        == "This sentence is review evidence only."
    )
    assert normalized.root.requirement_candidates == []
    assert normalized.root.authority_mappings == []


def test_structured_profile_invalid_source_ref_is_review_finding_not_compile_failure() -> (  # noqa: E501
    None
):
    """Normalizer preserves invalid source refs so review can block structurally."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["source_map"][0]["location"] = "REQ.missing.statement"

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map[0].location == "REQ.missing.statement"


def test_model_emitted_manifest_mapping_does_not_require_source_units() -> None:
    """Model candidate/mapping hints can bind to host source units by exact quote."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )
    from utils.spec_authority_ir import (  # noqa: PLC0415
        extract_requirement_candidates,
        parse_markdown_sections,
        source_units_from_sections,
    )

    source_quote = "- The payload must include user_id."
    source_text = "\n".join(["# Requirements", source_quote])
    sections, _diagnostics = parse_markdown_sections(source_text)
    host_candidates = extract_requirement_candidates(
        source_units_from_sections(sections)
    )
    candidate = host_candidates[0]
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = source_quote
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "requirement_candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "source_unit_id": candidate.source_unit_id,
                    "statement": candidate.statement,
                    "source_quote": candidate.source_quote,
                    "quote_hash": candidate.quote_hash,
                    "line_start": candidate.line_start,
                    "line_end": candidate.line_end,
                    "classification": candidate.classification,
                    "provenance": "model_emitted",
                }
            ],
            "authority_mappings": [
                {
                    "candidate_id": candidate.candidate_id,
                    "authority_item_id": "INV-aaaaaaaaaaaaaaaa",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Exact manifest candidate maps to invariant.",
                    "source_quote_hash": candidate.quote_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert success.ir_provenance == "mixed"
    assert success.source_units
    assert success.requirement_candidates[0].provenance == "model_emitted"
    assert success.authority_mappings[0].mapping_provenance == "model_quote"
    assert success.authority_mappings[0].mapping_status == "covered"


def test_model_mapping_can_reference_host_manifest_candidate_directly() -> None:
    """Model mapping hints can use host candidate IDs without repeating candidates."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )
    from utils.spec_authority_ir import (  # noqa: PLC0415
        extract_requirement_candidates,
        parse_markdown_sections,
        source_units_from_sections,
    )

    source_quote = "- The payload must include user_id."
    source_text = "\n".join(["# Requirements", source_quote])
    sections, _diagnostics = parse_markdown_sections(source_text)
    host_candidates = extract_requirement_candidates(
        source_units_from_sections(sections)
    )
    candidate = host_candidates[0]
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = "payload must include user_id"
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "source_units": [],
            "requirement_candidates": [],
            "authority_mappings": [
                {
                    "candidate_id": candidate.candidate_id,
                    "authority_item_id": "INV-aaaaaaaaaaaaaaaa",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Exact manifest candidate maps to invariant.",
                    "source_quote_hash": candidate.quote_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
        }
    )

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert success.authority_mappings[0].mapping_provenance == "model_quote"
    assert success.authority_mappings[0].mapping_status == "covered"


def test_model_emitted_candidate_without_mapping_marks_root_mixed() -> None:
    """Root provenance reflects retained model candidates even without mappings."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_quote = "- The payload must include user_id."
    source_text = "\n".join(["# Requirements", source_quote])
    quote_hash = f"sha256:{hashlib.sha256(source_quote.encode()).hexdigest()}"
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = source_quote
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "source_units": [
                {
                    "unit_id": "SRC-model",
                    "section_id": "S1",
                    "heading_path": ["Requirements"],
                    "kind": "list_item",
                    "line_start": 2,
                    "line_end": 2,
                    "text_hash": quote_hash,
                    "text_excerpt": source_quote,
                    "disposition": "candidate_extracted",
                    "disposition_reason": None,
                }
            ],
            "requirement_candidates": [
                {
                    "candidate_id": "REQ-model",
                    "source_unit_id": "SRC-model",
                    "statement": source_quote,
                    "source_quote": source_quote,
                    "quote_hash": quote_hash,
                    "line_start": 2,
                    "line_end": 2,
                    "classification": "requirement",
                    "provenance": "model_emitted",
                }
            ],
            "authority_mappings": [],
            "ir_packet_limits": {
                "max_candidates": 1,
                "max_findings": 0,
                "max_excerpt_bytes": 2000,
                "truncated": False,
            },
        }
    )

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.ir_provenance == "mixed"
    assert any(
        candidate.provenance == "model_emitted"
        for candidate in normalized.root.requirement_candidates
    )


def test_swapped_legacy_authority_id_cannot_launder_model_quote_mapping() -> None:
    """Model mappings cannot use swapped legacy source-map IDs as aliases."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    user_quote = "- The payload must include user_id."
    account_quote = "- The payload must include account_id."
    source_text = "\n".join(["# Requirements", user_quote, account_quote])
    user_hash = f"sha256:{hashlib.sha256(user_quote.encode()).hexdigest()}"
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-1111111111111111",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "user_id"},
        },
        {
            "id": "INV-2222222222222222",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "account_id"},
        },
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-2222222222222222",
            "excerpt": user_quote,
            "location": "line 2",
        },
        {
            "invariant_id": "INV-1111111111111111",
            "excerpt": account_quote,
            "location": "line 3",
        },
    ]
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "source_units": [
                {
                    "unit_id": "SRC-model",
                    "section_id": "S1",
                    "heading_path": ["Requirements"],
                    "kind": "list_item",
                    "line_start": 2,
                    "line_end": 2,
                    "text_hash": user_hash,
                    "text_excerpt": user_quote,
                    "disposition": "candidate_extracted",
                    "disposition_reason": None,
                }
            ],
            "requirement_candidates": [
                {
                    "candidate_id": "REQ-model-user",
                    "source_unit_id": "SRC-model",
                    "statement": user_quote,
                    "source_quote": user_quote,
                    "quote_hash": user_hash,
                    "line_start": 2,
                    "line_end": 2,
                    "classification": "requirement",
                    "provenance": "model_emitted",
                }
            ],
            "authority_mappings": [
                {
                    "candidate_id": "REQ-model-user",
                    "authority_item_id": "INV-2222222222222222",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Swapped legacy ID should not survive.",
                    "source_quote_hash": user_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
            "ir_packet_limits": {
                "max_candidates": 1,
                "max_findings": 0,
                "max_excerpt_bytes": 2000,
                "truncated": False,
            },
        }
    )

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    user_mappings = [
        mapping
        for mapping in normalized.root.authority_mappings
        if mapping.source_quote_hash == user_hash
    ]
    assert user_mappings
    assert all(
        mapping.mapping_provenance != "model_quote" for mapping in user_mappings
    )
    assert all(mapping.mapping_status != "covered" for mapping in user_mappings)


def test_model_quote_requires_model_quote_text_to_match_hash() -> None:
    """A supplied quote hash cannot launder stale model quote text."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_quote = "- The payload must include user_id."
    stale_quote = "- The payload must include stale_id."
    source_text = "\n".join(["# Requirements", source_quote])
    quote_hash = f"sha256:{hashlib.sha256(source_quote.encode()).hexdigest()}"
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = source_quote
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "model_emitted",
            "source_units": [
                {
                    "unit_id": "SRC-model",
                    "section_id": "S1",
                    "heading_path": ["Requirements"],
                    "kind": "list_item",
                    "line_start": 2,
                    "line_end": 2,
                    "text_hash": quote_hash,
                    "text_excerpt": stale_quote,
                    "disposition": "candidate_extracted",
                    "disposition_reason": None,
                }
            ],
            "requirement_candidates": [
                {
                    "candidate_id": "REQ-model",
                    "source_unit_id": "SRC-model",
                    "statement": stale_quote,
                    "source_quote": stale_quote,
                    "quote_hash": quote_hash,
                    "line_start": 2,
                    "line_end": 2,
                    "classification": "requirement",
                    "provenance": "model_emitted",
                }
            ],
            "authority_mappings": [
                {
                    "candidate_id": "REQ-model",
                    "authority_item_id": "INV-aaaaaaaaaaaaaaaa",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Stale model quote should not be trusted.",
                    "source_quote_hash": quote_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
            "ir_packet_limits": {
                "max_candidates": 1,
                "max_findings": 0,
                "max_excerpt_bytes": 2000,
                "truncated": False,
            },
        }
    )

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    assert success.ir_provenance == "host_parsed"
    assert all(
        candidate.provenance != "model_emitted"
        for candidate in success.requirement_candidates
    )
    assert success.authority_mappings[0].mapping_provenance != "model_quote"
    assert success.authority_mappings[0].mapping_status == "weak_mapping"


def test_host_parsed_model_hints_do_not_promote_candidate_provenance() -> None:
    """Only explicitly model-emitted compact IR can preserve model provenance."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_quote = "- The payload must include user_id."
    source_text = "\n".join(["# Requirements", source_quote])
    quote_hash = f"sha256:{hashlib.sha256(source_quote.encode()).hexdigest()}"
    raw = _legacy_success_payload()
    raw["source_map"][0]["excerpt"] = source_quote
    raw.update(
        {
            "ir_schema_version": "authority-ir-v1",
            "ir_provenance": "host_parsed",
            "source_units": [
                {
                    "unit_id": "SRC-host",
                    "section_id": "S1",
                    "heading_path": ["Requirements"],
                    "kind": "list_item",
                    "line_start": 2,
                    "line_end": 2,
                    "text_hash": quote_hash,
                    "text_excerpt": source_quote,
                    "disposition": "candidate_extracted",
                    "disposition_reason": None,
                }
            ],
            "requirement_candidates": [
                {
                    "candidate_id": "REQ-host",
                    "source_unit_id": "SRC-host",
                    "statement": source_quote,
                    "source_quote": source_quote,
                    "quote_hash": quote_hash,
                    "line_start": 2,
                    "line_end": 2,
                    "classification": "requirement",
                    "provenance": "host_parsed",
                }
            ],
            "authority_mappings": [
                {
                    "candidate_id": "REQ-host",
                    "authority_item_id": "INV-aaaaaaaaaaaaaaaa",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Host parsed hint should stay host parsed.",
                    "source_quote_hash": quote_hash,
                    "mapping_provenance": "model_quote",
                }
            ],
            "ir_packet_limits": {
                "max_candidates": 1,
                "max_findings": 0,
                "max_excerpt_bytes": 2000,
                "truncated": False,
            },
        }
    )

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.ir_provenance == "host_parsed"
    assert all(
        candidate.provenance != "model_emitted"
        for candidate in normalized.root.requirement_candidates
    )
    assert normalized.root.authority_mappings[0].mapping_provenance != "model_quote"


def test_swapped_legacy_source_refs_become_host_repaired_quotes() -> None:
    """Repaired provenance is based on source-map position, not global text reuse."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = "\n".join(
        [
            "# Requirements",
            "- The payload must include user_id.",
            "- The payload must include account_id.",
        ]
    )
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-1111111111111111",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "user_id"},
        },
        {
            "id": "INV-2222222222222222",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "account_id"},
        },
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-1111111111111111",
            "excerpt": "- The payload must include account_id.",
            "location": "line 3",
        },
        {
            "invariant_id": "INV-2222222222222222",
            "excerpt": "- The payload must include user_id.",
            "location": "line 2",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.authority_mappings) == 2  # noqa: PLR2004
    repaired_provenance = {
        mapping.mapping_provenance
        for mapping in normalized.root.authority_mappings
    }
    assert repaired_provenance == {"host_repaired_quote"}
    assert all(
        mapping.mapping_status == "weak_mapping"
        for mapping in normalized.root.authority_mappings
    )


def test_normalizer_fails_when_source_map_missing_or_unmatchable() -> None:
    """Normalizer must fail deterministically if source_map cannot support ID mapping."""  # noqa: E501
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": [],
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "1.0.0",
        "prompt_hash": "a" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))
    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.error == "SPEC_COMPILATION_FAILED"
    assert "source_map" in normalized.root.reason.lower() or any(
        "source_map" in gap.lower() for gap in normalized.root.blocking_gaps
    )


def test_normalizer_returns_failure_for_invalid_json() -> None:
    """Normalizer must return structured failure if raw output is not valid JSON."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    normalized = normalize_compiler_output("{not-json")
    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.error == "SPEC_COMPILATION_FAILED"
    assert "json" in normalized.root.reason.lower()


def test_normalizer_handles_duplicate_placeholder_invariant_ids() -> None:
    """Normalizer must correctly handle when LLM returns duplicate placeholder IDs.

    This is a common scenario where the LLM returns INV-0000000000000000 for all
    invariants instead of generating unique IDs. The normalizer must use positional
    matching to assign correct types to source_map entries.
    """
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    excerpt1 = "The payload must include user_id."
    excerpt2 = "The system must not use OAuth1 authentication."

    # LLM returns same placeholder ID for both invariants
    raw: dict[str, Any] = {
        "scope_themes": ["payload validation", "authentication security"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            },
            {
                "id": "INV-0000000000000000",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "OAuth1"},
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt1,
                "location": None,
            },
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt2,
                "location": None,
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    # Must succeed, not fail with SOURCE_MAP_INVARIANT_MISMATCH
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess), (
        f"Expected success but got failure: {normalized.root}"
    )

    # Two distinct invariants with deterministic IDs
    assert len(normalized.root.invariants) == 2  # noqa: PLR2004
    inv_ids = [inv.id for inv in normalized.root.invariants]
    assert len(set(inv_ids)) == 2, "Invariant IDs must be unique after normalization"  # noqa: PLR2004

    # Source map entries must match invariant IDs
    source_map_ids = {entry.invariant_id for entry in normalized.root.source_map}
    invariant_ids = {inv.id for inv in normalized.root.invariants}
    assert source_map_ids == invariant_ids, (
        f"Source map IDs {source_map_ids} must match invariant IDs {invariant_ids}"
    )


def test_normalizer_ids_include_parameters_when_excerpt_and_type_repeat() -> None:
    """Different invariants from one source excerpt must still get unique IDs."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    excerpt = (
        "Post-round review records actual captain-aware points, oracle gap when "
        "available, baseline comparisons, and UTC ISO-8601 timestamps."
    )
    raw: dict[str, Any] = {
        "scope_themes": ["post-round review"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "actual captain-aware points"},
            },
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "oracle gap"},
            },
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "baseline comparisons"},
            },
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "UTC ISO-8601 timestamps"},
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt,
                "location": "FR-010",
            }
            for _ in range(4)
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    invariant_ids = [invariant.id for invariant in normalized.root.invariants]
    assert len(set(invariant_ids)) == len(invariant_ids)
    assert {entry.invariant_id for entry in normalized.root.source_map} == set(
        invariant_ids
    )


def test_normalizer_rejects_source_map_that_does_not_support_field() -> None:
    """A direct source map must mention the field or capability it supports."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["squad constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "captain"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": (
                    "The live squad must stay within the operator's current budget."
                ),
                "location": "FR-003",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_MAP_INVARIANT_MISMATCH"
    assert any("captain" in gap for gap in normalized.root.blocking_gaps)


def test_normalizer_allows_forbidden_capability_safety_guard_excerpt() -> None:
    """Explicit safety guards can support forbidden capabilities."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["submission safety"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "real authenticated Cartola submission"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": (
                    "Any --confirm-submit invocation exits with CONTRACT_UNVERIFIED "
                    "before reading tokens or constructing a POST request."
                ),
                "location": "FR-016",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants[0].id != "INV-0000000000000000"


def test_normalizer_repairs_table_row_evidence_from_source_text() -> None:
    """Broad FR source snippets are expanded from the source spec before ID checks."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = (
        "\n"
        "| ID | Requirement | Acceptance Criteria | Priority |\n"
        "| --- | --- | --- | --- |\n"
        "| FR-002 | The live squad must satisfy Cartola roster rules. | "
        "The selected squad contains exactly 12 rows, exactly one tecnico, "
        "11 non-tecnico players, one non-tecnico captain, and one official "
        "formation. | Must |\n"
    )
    raw: dict[str, Any] = {
        "scope_themes": ["roster constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "formation"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "FR-002 | The live squad must satisfy Cartola roster rules.",
                "location": "6.2 Functional Requirements / FR-002",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert "official formation" in normalized.root.source_map[0].excerpt


def test_normalizer_removes_duplicate_semantic_invariants() -> None:
    """Exact duplicate invariants should not survive as duplicate IDs."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["budget constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "RELATION_CONSTRAINT",
                "parameters": {"expression": "budget_used <= budget"},
            },
            {
                "id": "INV-0000000000000000",
                "type": "RELATION_CONSTRAINT",
                "parameters": {"expression": "budget_used <= budget"},
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "Acceptance Criteria: budget_used <= budget.",
                "location": "FR-003",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.invariants) == 1
    assert len({inv.id for inv in normalized.root.invariants}) == 1


def test_normalizer_rejects_max_value_when_excerpt_lacks_bound() -> None:
    """Do not let dynamic relationships become unsupported hard constants."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["budget constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "MAX_VALUE",
                "parameters": {"field_name": "budget_used", "max_value": 100},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "Acceptance Criteria: budget_used <= budget.",
                "location": "FR-003",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_MAP_INVARIANT_MISMATCH"
    assert any("100" in gap for gap in normalized.root.blocking_gaps)


def test_normalizer_drops_max_value_from_command_example() -> None:
    """Example command budgets are sample inputs, not global authority limits."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = "\n".join(
        [
            "## Functional Requirements",
            "| ID | Requirement | Acceptance Criteria | Priority |",
            "| --- | --- | --- | --- |",
            "| FR-003 | The live squad must stay within budget. | "
            "`budget_used <= budget` in artifacts. | Must |",
            "## Interfaces",
            "```bash",
            "python scripts/run_live_round.py --budget 100",
            "```",
        ]
    )
    raw: dict[str, Any] = {
        "scope_themes": ["budget constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "MAX_VALUE",
                "parameters": {"field_name": "budget_used", "max_value": 100},
            },
            {
                "id": "INV-0000000000000001",
                "type": "RELATION_CONSTRAINT",
                "parameters": {"expression": "budget_used <= budget"},
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "python scripts/run_live_round.py --budget 100",
                "location": "Interfaces",
            },
            {
                "invariant_id": "INV-0000000000000001",
                "excerpt": "FR-003 | budget_used <= budget.",
                "location": "FR-003",
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert [inv.type for inv in normalized.root.invariants] == [
        InvariantType.RELATION_CONSTRAINT
    ]
    assert any("budget_used 100" in gap for gap in normalized.root.gaps)


def test_normalizer_rejects_zero_max_value_when_excerpt_lacks_zero_bound() -> None:
    """A zero max value must be source-supported, not treated as missing."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["budget constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "MAX_VALUE",
                "parameters": {"field_name": "budget_used", "max_value": 0},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "Acceptance Criteria: budget_used <= budget.",
                "location": "FR-003",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_MAP_INVARIANT_MISMATCH"
    assert any("0" in gap for gap in normalized.root.blocking_gaps)


def test_normalizer_preserves_relation_constraint_for_dynamic_budget() -> None:
    """Dynamic relationships need a non-numeric relation invariant type."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["budget constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "RELATION_CONSTRAINT",
                "parameters": {"expression": "budget_used <= budget"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "Acceptance Criteria: budget_used <= budget.",
                "location": "FR-003",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants[0].type == InvariantType.RELATION_CONSTRAINT


def test_normalizer_rejects_relation_constraint_without_operator_evidence() -> None:
    """A field mention alone cannot support a dynamic relation constraint."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["budget constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "RELATION_CONSTRAINT",
                "parameters": {"expression": "budget_used <= budget"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "A live run outputs budget used in recommendation metadata.",
                "location": "FR-001",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_MAP_INVARIANT_MISMATCH"
    assert any("operator" in gap for gap in normalized.root.blocking_gaps)


def test_normalizer_handles_duplicate_ids_different_types_length_mismatch() -> None:
    """Normalizer must succeed when LLM returns duplicate IDs with different types.

    AND the number of source_map entries doesn't match the number of invariants.

    Regression: original_id_to_type dict loses type information for duplicate IDs
    because dict construction keeps only the last value per key.  When
    use_positional_matching is False (length mismatch), source_map entries all
    resolve to the last-wins type, producing an ID that covers only one invariant
    while leaving the other as SOURCE_MAP_INVARIANT_MISMATCH.
    """
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    excerpt1 = "The payload must include user_id."
    excerpt2 = "The system must not use OAuth1 authentication."
    # Extra source_map entry for the same invariant (different excerpt location)
    excerpt3 = "user_id is mandatory in all API payloads."

    raw: dict[str, Any] = {
        "scope_themes": ["payload validation", "authentication security"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            },
            {
                "id": "INV-0000000000000000",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "OAuth1"},
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt1,
                "location": "spec:section:1",
            },
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt2,
                "location": "spec:section:2",
            },
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt3,
                "location": "spec:section:1:para:2",
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    # Must succeed — not fail with SOURCE_MAP_INVARIANT_MISMATCH
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess), (
        f"Expected success but got failure: {normalized.root}"
    )

    # Two distinct invariants with deterministic IDs
    assert len(normalized.root.invariants) == 2  # noqa: PLR2004
    inv_ids = [inv.id for inv in normalized.root.invariants]
    assert len(set(inv_ids)) == 2, "Invariant IDs must be unique after normalization"  # noqa: PLR2004

    # Every invariant must have at least one source_map entry
    source_map_ids = {entry.invariant_id for entry in normalized.root.source_map}
    invariant_ids = {inv.id for inv in normalized.root.invariants}
    assert invariant_ids.issubset(source_map_ids), (
        f"Every invariant ID must be covered by source_map. "
        f"Missing: {sorted(invariant_ids - source_map_ids)}"
    )


def test_normalize_zero_invariants_returns_success_with_warning() -> None:
    """Verify normalize zero invariants returns success with warning."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["notes-only"],
        "domain": None,
        "invariants": [],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "No normative requirements found.",
                "location": "spec:notes",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants == []
    assert "No invariants extracted from spec" in normalized.root.gaps


def test_normalize_empty_invariants_allows_empty_source_map() -> None:
    """Verify normalize empty invariants allows empty source map."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": [],
        "domain": None,
        "invariants": [],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants == []
    assert normalized.root.source_map == []


def test_normalizer_extracts_json_from_wrapped_text() -> None:
    """Verify normalizer extracts json from wrapped text."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    excerpt = "The payload must include project_name."
    raw: dict[str, Any] = {
        "scope_themes": ["project setup"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "project_name"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt,
                "location": "spec:line:1",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    wrapped = (
        "Here is the compiled payload.\n"
        "```json\n"
        f"{json.dumps(raw)}\n"
        "```\n"
        "Use this result."
    )

    normalized = normalize_compiler_output(wrapped)
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.invariants) == 1


def test_normalizer_accepts_enveloped_result_payload() -> None:
    """Verify normalizer accepts enveloped result payload."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    excerpt = "The system must not expose provider settings."
    result_payload: dict[str, Any] = {
        "scope_themes": ["ui policy"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "provider selection"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": excerpt,
                "location": "spec:line:2",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps({"result": result_payload}))
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.source_map) == 1


def test_normalizer_filters_meta_policy_invariant_from_plagiarism_section() -> None:
    """Verify normalizer filters meta policy invariant from plagiarism section."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["assignment policy"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "plagiarism"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": (
                    "Knowingly representing the works of others as one's own or "
                    "referencing the works of others without appropriate citation is prohibited."  # noqa: E501
                ),
                "location": "Plagiarism Policy",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.invariants == []
    assert normalized.root.source_map == []
    assert (
        "Excluded non-product policy/admin excerpts from compiled invariants."
        in normalized.root.assumptions
    )


def test_normalizer_preserves_real_product_forbidden_capability() -> None:
    """Verify normalizer preserves real product forbidden capability."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["interface constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "web dashboard"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "The system must not include a web dashboard.",
                "location": "Product Constraints",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.invariants) == 1
    assert normalized.root.invariants[0].type == InvariantType.FORBIDDEN_CAPABILITY
    assert len(normalized.root.source_map) == 1


def test_normalizer_does_not_filter_product_invariant_from_api_references_section() -> (
    None
):
    """Verify normalizer does not filter product invariant from api references section."""  # noqa: E501
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["api constraints"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "web dashboard"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-0000000000000000",
                "excerpt": "The product must not include a web dashboard.",
                "location": "API References",
            }
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.invariants) == 1
    assert len(normalized.root.source_map) == 1
