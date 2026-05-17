"""Unit tests for host-side normalization of spec authority compiler outputs."""

import json
import re
from typing import Any

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
