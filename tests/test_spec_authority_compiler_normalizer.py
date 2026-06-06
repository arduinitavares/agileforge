"""Unit tests for host-side normalization of spec authority compiler outputs."""

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator_agent.agent_tools.spec_authority_compiler_agent.compiler_contract import (  # noqa: E501
    compute_invariant_id_from_payload,
    compute_prompt_hash,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
    normalize_compiler_output,
)
from utils.spec_schemas import (
    DataContractParams,
    InvariantType,
    RequiredFieldParams,
    RouteContractParams,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    StateTransitionParams,
    UserInteractionParams,
    VisibilityRuleParams,
)

EXPECTED_SOURCE_DISTINCT_INVARIANT_COUNT: int = 2


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


def _structured_behavior_spec_source() -> str:
    """Return canonical structured spec JSON with behavioral authority items."""
    from utils.agileforge_spec_profile import (  # noqa: PLC0415
        TechnicalSpecArtifact,
        canonical_spec_json,
    )

    artifact = TechnicalSpecArtifact.model_validate(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.behavior",
            "title": "Behavioral Structured Spec",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-05-20",
            "updated_at": "2026-05-20",
            "summary": "Exercise behavioral authority normalization.",
            "problem_statement": "Behavioral specs need source metadata checks.",
            "items": [
                {
                    "id": "REQ.item-interactions",
                    "type": "REQ",
                    "status": "accepted",
                    "title": "Todo item interactions",
                    "statement": (
                        "Each todo item must support checkbox completion and "
                        "label double-click editing activation."
                    ),
                    "level": "MUST",
                    "verification": "system-test",
                    "acceptance": [
                        "Clicking a todo checkbox updates the todo completed value.",
                        "Double-clicking a todo label enters editing mode.",
                    ],
                },
                {
                    "id": "CONSTRAINT.html-css-js-style",
                    "type": "CONSTRAINT",
                    "status": "accepted",
                    "title": "Template style guidance",
                    "statement": (
                        "The implementation should avoid Sass, CoffeeScript, "
                        "or other preprocessors unless a reviewer records a "
                        "framework-specific reason."
                    ),
                    "level": "SHOULD",
                    "verification": "inspection",
                    "acceptance": [
                        "The implementation avoids Sass, CoffeeScript, or "
                        "other preprocessors unless a reviewer records a "
                        "framework-specific reason."
                    ],
                },
                {
                    "id": "DATA.todo-record",
                    "type": "DATA",
                    "status": "accepted",
                    "title": "Todo record",
                    "statement": (
                        "When possible, each persisted todo item should use "
                        "the keys id, title, and completed."
                    ),
                    "level": "SHOULD",
                    "verification": "inspection",
                    "acceptance": [
                        "Persisted todo records use id, title, and completed "
                        "keys when possible."
                    ],
                },
                {
                    "id": "DATA.editing-state",
                    "type": "DATA",
                    "status": "accepted",
                    "title": "Editing state",
                    "statement": "Editing mode must not be persisted.",
                    "level": "MUST_NOT",
                    "verification": "system-test",
                    "acceptance": ["Editing mode is not restored after reload."],
                },
                {
                    "id": "NON_GOAL.customized-visual-design",
                    "type": "NON_GOAL",
                    "status": "accepted",
                    "title": "Customized visual design",
                    "statement": (
                        "The app is not intended to introduce a distinct "
                        "visual design beyond minimal app.css changes."
                    ),
                },
                {
                    "id": "EXAMPLE.package-json",
                    "type": "EXAMPLE",
                    "status": "accepted",
                    "title": "package.json dependency example",
                    "statement": (
                        "A package.json may include framework dependencies "
                        "alongside todomvc-app-css and todomvc-common."
                    ),
                },
            ],
        }
    )
    return canonical_spec_json(artifact)


def _asa_like_structured_spec_source() -> str:
    """Return structured spec JSON reproducing ASA source-evidence shapes."""
    from utils.agileforge_spec_profile import (  # noqa: PLC0415
        TechnicalSpecArtifact,
        canonical_spec_json,
    )

    artifact = TechnicalSpecArtifact.model_validate(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.asa-normalizer",
            "title": "ASA Normalizer Spec",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-06-06",
            "updated_at": "2026-06-06",
            "summary": "Exercise ASA authority source-map evidence.",
            "problem_statement": "Authority evidence must be real source text.",
            "items": [
                {
                    "id": "REQ.tech-stack-model-research",
                    "type": "REQ",
                    "status": "accepted",
                    "title": "Technology and model research spike",
                    "statement": (
                        "The project must include an early research spike that "
                        "evaluates current technology stack and modeling options "
                        "before selecting an advisory or reinforcement-learning "
                        "approach."
                    ),
                    "level": "MUST",
                    "verification": "inspection",
                    "acceptance": [
                        (
                            "The research output compares at least a supervised "
                            "dynamics model, constrained candidate-action search "
                            "or MPC-style optimization, offline reinforcement "
                            "learning, and deterministic policy-gradient "
                            "approaches such as DDPG, TD3, or SAC when relevant."
                        ),
                        (
                            "The research output evaluates Python runtime choice, "
                            "uv project management, package compatibility, "
                            "data-processing stack, model-training stack, "
                            "experiment tracking, validation approach, deployment "
                            "constraints, and safety-review implications."
                        ),
                        (
                            "The research output records the selected first "
                            "implementation approach, rejected alternatives, "
                            "evidence used, unresolved assumptions, and criteria "
                            "that would trigger revisiting the decision."
                        ),
                    ],
                },
                {
                    "id": "CONSTRAINT.uv-managed",
                    "type": "CONSTRAINT",
                    "status": "accepted",
                    "title": "uv-managed Python project",
                    "statement": (
                        "The project must use uv as the Python project and "
                        "dependency manager."
                    ),
                    "level": "MUST",
                    "verification": "inspection",
                    "acceptance": [
                        (
                            "Project dependencies, development dependencies, "
                            "package metadata, and Python runtime constraints "
                            "are declared in pyproject.toml."
                        ),
                        (
                            "The repository contains uv.lock and documented setup "
                            "commands use uv sync or uv run rather than pip, "
                            "poetry, pipenv, or ad hoc virtual-environment "
                            "commands."
                        ),
                        (
                            "Quality checks, tests, and project scripts are "
                            "runnable through uv-managed commands."
                        ),
                    ],
                    "source_notes": [
                        {
                            "kind": "external_summary",
                            "text": (
                                "Astral uv documentation describes uv init, uv "
                                "add, uv lock, uv sync, and uv run as the "
                                "project-management workflow."
                            ),
                        }
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


def _base_success_payload() -> dict[str, object]:
    return {
        "schema_version": "agileforge.compiled_authority.v2",
        "scope_themes": ["Payments"],
        "domain": None,
        "invariants": [],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "2.0.0",
        "prompt_hash": "a" * 64,
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


def _assert_compact_ir_cleared(
    success: SpecAuthorityCompilationSuccess,
) -> None:
    assert success.ir_schema_version is None
    assert success.ir_provenance is None
    assert success.source_units == []
    assert success.requirement_candidates == []
    assert success.authority_mappings == []
    assert success.ir_packet_limits is None


def _assert_semantic_invariant_ids(
    success: SpecAuthorityCompilationSuccess,
) -> None:
    expected_ids = [
        compute_invariant_id_from_payload(
            invariant.type,
            invariant.parameters,
            source_item_id=invariant.source_item_id,
            source_level=invariant.source_level,
        )
        for invariant in success.invariants
    ]
    assert [invariant.id for invariant in success.invariants] == expected_ids


def test_legacy_success_without_ir_stays_valid() -> None:
    """Historical compiled authority JSON without compact IR remains loadable."""
    success = SpecAuthorityCompilationSuccess.model_validate(_legacy_success_payload())

    assert success.rejected_features == []
    _assert_compact_ir_cleared(success)


def test_normalizer_invariant_ids_include_source_provenance() -> None:
    """Same rule shape from different source items keeps distinct IDs."""
    payload = _base_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.beta",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Alpha requires email.",
            "location": "REQ.alpha.statement",
        },
        {
            "invariant_id": "INV-0000000000000001",
            "excerpt": "Beta requires email.",
            "location": "REQ.beta.statement",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    ids = [invariant.id for invariant in normalized.root.invariants]
    assert len(ids) == EXPECTED_SOURCE_DISTINCT_INVARIANT_COUNT
    assert len(set(ids)) == EXPECTED_SOURCE_DISTINCT_INVARIANT_COUNT
    assert {
        entry.invariant_id for entry in normalized.root.source_map
    } == set(ids)


def test_normalizer_merges_only_same_provenance_exact_duplicates() -> None:
    """Normalizer pre-cleanup does not collapse source-distinct same-shaped rules."""
    payload = _base_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000002",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.beta",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
    ]
    payload["source_map"] = []

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert (
        len(normalized.root.invariants) == EXPECTED_SOURCE_DISTINCT_INVARIANT_COUNT
    )
    assert {
        invariant.source_item_id for invariant in normalized.root.invariants
    } == {"REQ.alpha", "REQ.beta"}


def test_normalizer_exact_duplicate_cleanup_preserves_source_map_entries() -> None:
    """Exact duplicate cleanup keeps every supporting source-map entry."""
    payload = _base_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Alpha requires email.",
            "location": "REQ.alpha.statement",
        },
        {
            "invariant_id": "INV-0000000000000001",
            "excerpt": "Email is required for alpha.",
            "location": "REQ.alpha.acceptance[0]",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.invariants) == 1
    kept_id = normalized.root.invariants[0].id
    assert [entry.invariant_id for entry in normalized.root.source_map] == [
        kept_id,
        kept_id,
    ]


def test_behavioral_invariants_accept_top_level_source_metadata() -> None:
    """Behavioral invariants keep source metadata at the invariant level."""
    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "USER_INTERACTION",
            "source_item_id": "REQ.item-interactions",
            "source_level": "MUST",
            "parameters": {
                "trigger": "double-click label",
                "target": "todo item label",
                "expected_response": "parent li enters editing mode",
            },
        },
        {
            "id": "INV-bbbbbbbbbbbbbbbb",
            "type": "STATE_TRANSITION",
            "source_item_id": "REQ.editing",
            "source_level": "MUST",
            "parameters": {
                "state": "editing",
                "trigger": "Escape key",
                "outcome": "editing exits and unsaved changes are discarded",
            },
        },
        {
            "id": "INV-cccccccccccccccc",
            "type": "DATA_CONTRACT",
            "source_item_id": "DATA.todo-record",
            "source_level": "SHOULD",
            "parameters": {
                "subject": "persisted todo record",
                "fields": ["id", "title", "completed"],
                "rule": "records use id, title, and completed keys when possible",
            },
        },
        {
            "id": "INV-dddddddddddddddd",
            "type": "ROUTE_CONTRACT",
            "source_item_id": "REQ.routing",
            "source_level": "MUST",
            "parameters": {
                "route": "#/active",
                "route_name": "active",
                "behavior": "shows active todos",
            },
        },
        {
            "id": "INV-eeeeeeeeeeeeeeee",
            "type": "VISIBILITY_RULE",
            "source_item_id": "REQ.empty-state-visibility",
            "source_level": "SHOULD",
            "parameters": {
                "target": "#main",
                "condition": "todo list is empty",
                "visibility": "hidden",
            },
        },
    ]

    success = SpecAuthorityCompilationSuccess.model_validate(payload)

    assert isinstance(success.invariants[0].parameters, UserInteractionParams)
    assert isinstance(success.invariants[1].parameters, StateTransitionParams)
    assert isinstance(success.invariants[2].parameters, DataContractParams)
    assert isinstance(success.invariants[3].parameters, RouteContractParams)
    assert isinstance(success.invariants[4].parameters, VisibilityRuleParams)
    assert success.invariants[0].source_item_id == "REQ.item-interactions"
    assert success.invariants[0].source_level == "MUST"
    assert success.invariants[2].parameters.fields == ["id", "title", "completed"]


def test_behavioral_invariant_parameters_must_match_declared_type() -> None:
    """A behavioral invariant type cannot reuse an unrelated parameter shape."""
    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "USER_INTERACTION",
            "source_item_id": "REQ.item-interactions",
            "source_level": "MUST",
            "parameters": {
                "state": "editing",
                "trigger": "Escape key",
                "outcome": "editing exits and changes are discarded",
            },
        }
    ]

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_normalizer_repairs_fresh_behavioral_provenance_before_validation() -> None:
    """Fresh compiler output moves behavioral provenance to top-level fields."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "USER_INTERACTION",
            "parameters": {
                "source_item_id": "REQ.item-interactions",
                "source_level": "MUST",
                "trigger": "checkbox click",
                "target": "todo checkbox",
                "expected_response": "todo completed value toggles",
            },
        }
    ]

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    invariant = normalized.root.invariants[0]
    assert isinstance(invariant.parameters, UserInteractionParams)
    assert invariant.id == compute_invariant_id_from_payload(
        InvariantType.USER_INTERACTION,
        UserInteractionParams(
            trigger="checkbox click",
            target="todo checkbox",
            expected_response="todo completed value toggles",
        ),
        source_item_id="REQ.item-interactions",
        source_level="MUST",
    )
    assert invariant.source_item_id == "REQ.item-interactions"
    assert invariant.source_level == "MUST"
    assert invariant.parameters.model_dump(mode="json") == {
        "trigger": "checkbox click",
        "target": "todo checkbox",
        "expected_response": "todo completed value toggles",
    }


def test_saved_failure_placeholder_invariant_ids_repair_to_v2() -> None:
    """Saved project-create placeholder IDs are repaired before validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"][0]["id"] = "INV-xxxxxxxxxxxxxxxx"
    payload["source_map"][0]["invariant_id"] = "INV-xxxxxxxxxxxxxxxx"

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    invariant = normalized.root.invariants[0]
    assert isinstance(invariant.parameters, RequiredFieldParams)
    expected_id = compute_invariant_id_from_payload(
        InvariantType.REQUIRED_FIELD,
        invariant.parameters,
        source_item_id=invariant.source_item_id,
        source_level=invariant.source_level,
    )
    assert invariant.id == expected_id
    assert re.fullmatch(r"INV-[0-9a-f]{16}", invariant.id)
    assert normalized.root.source_map[0].invariant_id == expected_id


def test_saved_failure_invalid_prompt_hash_repairs_to_compiler_prompt_hash() -> None:
    """Saved project-create prompt_hash failures are repaired before validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["prompt_hash"] = "not-a-valid-hash"

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )


def test_saved_failure_param_level_source_item_repairs_to_top_level() -> None:
    """Saved project-create param-level source metadata is lifted off params."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-xxxxxxxxxxxxxxxx",
            "type": "USER_INTERACTION",
            "parameters": {
                "source_item_id": "REQ.item-interactions",
                "source_level": "MUST",
                "trigger": "checkbox click",
                "target": "todo checkbox",
                "expected_response": "todo completed value toggles",
            },
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-xxxxxxxxxxxxxxxx",
            "excerpt": "Clicking a todo checkbox updates the todo completed value.",
            "location": "REQ.item-interactions.acceptance[0]",
        }
    ]

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    invariant = normalized.root.invariants[0]
    assert isinstance(invariant.parameters, UserInteractionParams)
    assert invariant.source_item_id == "REQ.item-interactions"
    assert invariant.source_level == "MUST"
    assert invariant.parameters.model_dump(mode="json") == {
        "trigger": "checkbox click",
        "target": "todo checkbox",
        "expected_response": "todo completed value toggles",
    }
    assert normalized.root.source_map[0].invariant_id == invariant.id


def test_normalizer_semantic_ids_include_provenance_fields() -> None:
    """Top-level provenance affects deterministic invariant IDs."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload_a = _legacy_success_payload()
    payload_a["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "USER_INTERACTION",
            "source_item_id": "REQ.item-interactions",
            "source_level": "MUST",
            "parameters": {
                "trigger": "checkbox click",
                "target": "todo checkbox",
                "expected_response": "todo completed value toggles",
            },
        }
    ]
    payload_b = json.loads(json.dumps(payload_a))
    payload_b["invariants"][0]["source_item_id"] = "REQ.other-item"
    payload_b["invariants"][0]["source_level"] = "SHOULD"

    normalized_a = normalize_compiler_output(json.dumps(payload_a))
    normalized_b = normalize_compiler_output(json.dumps(payload_b))

    assert isinstance(normalized_a.root, SpecAuthorityCompilationSuccess)
    assert isinstance(normalized_b.root, SpecAuthorityCompilationSuccess)
    assert normalized_a.root.invariants[0].id != normalized_b.root.invariants[0].id


def test_normalizer_rejects_behavioral_source_level_mismatch() -> None:
    """Behavioral params must match source item level in structured specs."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "USER_INTERACTION",
            "parameters": {
                "source_item_id": "REQ.item-interactions",
                "source_level": "SHOULD",
                "trigger": "checkbox click",
                "target": "todo checkbox",
                "expected_response": "todo completed value toggles",
            },
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_behavior_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"
    assert "REQ.item-interactions" in normalized.root.blocking_gaps[0]
    assert "source_level SHOULD does not match MUST" in normalized.root.blocking_gaps[0]


def test_normalizer_rejects_behavioral_invariant_without_real_source_text() -> None:
    """Top-level provenance alone is not enough without real structured evidence."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "USER_INTERACTION",
            "source_item_id": "REQ.review-token",
            "source_level": "MUST",
            "parameters": {
                "trigger": "checkbox click",
                "target": "todo checkbox",
                "expected_response": "todo completed value toggles",
            },
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": "fake excerpt",
            "location": "REQ.review-token.statement",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_normalizer_rejects_structured_item_id_embedded_only_in_excerpt() -> None:
    """Structured proof cannot come from an item ID typed into excerpt text."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "FORBIDDEN_CAPABILITY",
            "parameters": {"capability": "Sass"},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": (
                "REQ.item-interactions: Each todo item must support checkbox "
                "completion and label double-click editing activation."
            ),
            "location": "architecture notes",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_behavior_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_normalizer_rejects_forbidden_capability_from_should_source() -> None:
    """SHOULD guidance cannot become a hard forbidden capability."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "FORBIDDEN_CAPABILITY",
            "parameters": {"capability": "Sass"},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": (
                "The implementation avoids Sass, CoffeeScript, or other "
                "preprocessors unless a reviewer records a framework-specific "
                "reason."
            ),
            "location": "CONSTRAINT.html-css-js-style.acceptance[0]",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_behavior_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"
    assert "FORBIDDEN_CAPABILITY" in normalized.root.blocking_gaps[0]
    assert "CONSTRAINT.html-css-js-style" in normalized.root.blocking_gaps[0]


def test_normalizer_filters_non_normative_decision_hard_ban() -> None:
    """DECISION rationale must not become a hard forbidden authority invariant."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )
    from utils.agileforge_spec_profile import (  # noqa: PLC0415
        TechnicalSpecArtifact,
        canonical_spec_json,
    )

    source_text = canonical_spec_json(
        TechnicalSpecArtifact.model_validate(
            {
                "schema_version": "agileforge.spec.v1",
                "artifact_id": "SPEC.decision-filter",
                "title": "Decision Filter Spec",
                "status": "draft",
                "version": "0.1",
                "created_at": "2026-06-05",
                "updated_at": "2026-06-05",
                "summary": "Exercise decision filtering.",
                "problem_statement": "Research decisions should not become hard bans.",
                "items": [
                    {
                        "id": "DECISION.research-before-algorithm",
                        "type": "DECISION",
                        "status": "accepted",
                        "title": "Research before algorithm",
                        "statement": (
                            "Research the best model and stack before deciding "
                            "the final algorithm."
                        ),
                    },
                    {
                        "id": "REQ.include-review-token",
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
    )
    raw: dict[str, Any] = {
        "scope_themes": ["research safety"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-1111111111111111",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {
                    "capability": "final algorithm selection before research"
                },
            },
            {
                "id": "INV-2222222222222222",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "review token evidence"},
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-1111111111111111",
                "excerpt": (
                    "Research the best model and stack before deciding "
                    "the final algorithm."
                ),
                "location": "DECISION.research-before-algorithm.statement",
            },
            {
                "invariant_id": "INV-2222222222222222",
                "excerpt": "The system MUST include review token evidence.",
                "location": "REQ.include-review-token.statement",
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert [invariant.type for invariant in normalized.root.invariants] == [
        InvariantType.REQUIRED_FIELD
    ]
    assert all(
        "DECISION.research-before-algorithm" not in (entry.location or "")
        for entry in normalized.root.source_map
    )
    assert normalized.root.assumptions.count(
        "Excluded non-normative DECISION item from hard forbidden authority."
    ) == 1


def test_normalizer_keeps_decision_hard_ban_with_unknown_source_ref() -> None:
    """Unknown mixed evidence must not be discarded by DECISION hard-ban filter."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )
    from utils.agileforge_spec_profile import (  # noqa: PLC0415
        TechnicalSpecArtifact,
        canonical_spec_json,
    )

    source_text = canonical_spec_json(
        TechnicalSpecArtifact.model_validate(
            {
                "schema_version": "agileforge.spec.v1",
                "artifact_id": "SPEC.decision-filter-unknown",
                "title": "Decision Filter Unknown Spec",
                "status": "draft",
                "version": "0.1",
                "created_at": "2026-06-05",
                "updated_at": "2026-06-05",
                "summary": "Exercise decision filter safety.",
                "problem_statement": "Unknown source refs must stay fail-closed.",
                "items": [
                    {
                        "id": "DECISION.research-before-algorithm",
                        "type": "DECISION",
                        "status": "accepted",
                        "title": "Research before algorithm",
                        "statement": "Research before deciding final algorithm.",
                    }
                ],
            }
        )
    )
    raw: dict[str, Any] = {
        "scope_themes": ["research safety"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-1111111111111111",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "final algorithm"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-1111111111111111",
                "excerpt": "Research before deciding final algorithm.",
                "location": "DECISION.research-before-algorithm.statement",
            },
            {
                "invariant_id": "INV-1111111111111111",
                "excerpt": "Unknown hard ban requirement.",
                "location": "REQ.unknown-hard-ban.statement",
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_normalizer_keeps_decision_hard_ban_with_unparseable_source_ref() -> None:
    """Unparseable mixed evidence must not be discarded by DECISION filter."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )
    from utils.agileforge_spec_profile import (  # noqa: PLC0415
        TechnicalSpecArtifact,
        canonical_spec_json,
    )

    source_text = canonical_spec_json(
        TechnicalSpecArtifact.model_validate(
            {
                "schema_version": "agileforge.spec.v1",
                "artifact_id": "SPEC.decision-filter-unparseable",
                "title": "Decision Filter Unparseable Spec",
                "status": "draft",
                "version": "0.1",
                "created_at": "2026-06-05",
                "updated_at": "2026-06-05",
                "summary": "Exercise decision filter safety.",
                "problem_statement": "Unparseable refs must stay fail-closed.",
                "items": [
                    {
                        "id": "DECISION.research-before-algorithm",
                        "type": "DECISION",
                        "status": "accepted",
                        "title": "Research before algorithm",
                        "statement": "Research before deciding final algorithm.",
                    }
                ],
            }
        )
    )
    raw: dict[str, Any] = {
        "scope_themes": ["research safety"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-1111111111111111",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "final algorithm"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-1111111111111111",
                "excerpt": "Research before deciding final algorithm.",
                "location": "DECISION.research-before-algorithm.statement",
            },
            {
                "invariant_id": "INV-1111111111111111",
                "excerpt": "Unstructured hard ban evidence.",
                "location": "architecture notes",
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_normalizer_allows_forbidden_capability_from_non_goal_source() -> None:
    """Accepted NON_GOAL items are hard exclusion evidence without a level."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "FORBIDDEN_CAPABILITY",
            "parameters": {"capability": "distinct visual design"},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": (
                "The app is not intended to introduce a distinct visual "
                "design beyond minimal app.css changes."
            ),
            "location": "NON_GOAL.customized-visual-design.statement",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_behavior_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)


def test_normalizer_rejects_invariant_sourced_only_from_example() -> None:
    """Illustrative EXAMPLE items cannot be sole invariant evidence."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "package.json"},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": (
                "A package.json may include framework dependencies alongside "
                "todomvc-app-css and todomvc-common."
            ),
            "location": "EXAMPLE.package-json.statement",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_behavior_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"
    assert "EXAMPLE.package-json" in normalized.root.blocking_gaps[0]


def test_normalizer_validates_source_metadata_after_placeholder_id_rewrite() -> None:
    """Duplicate model placeholder IDs do not smear source-map metadata."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "DATA_CONTRACT",
            "parameters": {
                "source_item_id": "DATA.todo-record",
                "source_level": "SHOULD",
                "subject": "persisted todo record",
                "fields": ["id", "title", "completed"],
                "rule": "records use id, title, and completed keys when possible",
            },
        },
        {
            "id": "INV-0000000000000000",
            "type": "FORBIDDEN_CAPABILITY",
            "parameters": {"capability": "persist editing mode"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": (
                "When possible, each persisted todo item should use the keys "
                "id, title, and completed."
            ),
            "location": "DATA.todo-record",
        },
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Editing mode must not be persisted.",
            "location": "DATA.editing-state",
        },
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_behavior_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    source_refs_by_type = {
        invariant.type: [
            entry.location
            for entry in normalized.root.source_map
            if entry.invariant_id == invariant.id
            and isinstance(entry.location, str)
        ]
        for invariant in normalized.root.invariants
    }
    source_item_ids_by_type = {
        invariant_type: [".".join(location.split(".")[:2]) for location in locations]
        for invariant_type, locations in source_refs_by_type.items()
    }
    assert source_item_ids_by_type[InvariantType.DATA_CONTRACT] == [
        "DATA.todo-record"
    ]
    assert source_item_ids_by_type[InvariantType.FORBIDDEN_CAPABILITY] == [
        "DATA.editing-state"
    ]
    invariant_by_type = {
        invariant.type: invariant for invariant in normalized.root.invariants
    }
    assert invariant_by_type[InvariantType.DATA_CONTRACT].source_item_id == (
        "DATA.todo-record"
    )
    assert invariant_by_type[InvariantType.DATA_CONTRACT].source_level == "SHOULD"


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

    # ID must be derived from invariant semantics and provenance
    assert len(normalized.root.source_map) == 1
    sm = normalized.root.source_map[0]
    expected_id = compute_invariant_id_from_payload(
        inv.type,
        inv.parameters,
        source_item_id=inv.source_item_id,
        source_level=inv.source_level,
    )
    assert inv.id == expected_id
    assert sm.invariant_id == expected_id
    assert re.match(r"^INV-[0-9a-f]{16}$", inv.id)


def test_normalizer_repairs_invalid_prompt_hash_before_validation() -> None:
    """Invalid prompt_hash should be repaired before strict schema validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["payload validation"],
        "domain": None,
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
        "prompt_hash": "not-a-valid-hash",
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )


def test_normalizer_repairs_missing_prompt_hash_before_validation() -> None:
    """Missing prompt_hash should be repaired before strict schema validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["payload validation"],
        "domain": None,
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
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )


def test_normalizer_repairs_missing_hash_and_source_map_before_validation() -> None:
    """Missing prompt_hash plus source_map should still normalize as success."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["payload validation"],
        "domain": None,
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
        "compiler_version": "1.0.0",
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )
    assert normalized.root.source_map == []


def test_normalizer_repairs_invalid_envelope_prompt_hash_before_validation() -> None:
    """Envelope result prompt_hash should be repaired before validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    result_payload: dict[str, Any] = {
        "scope_themes": ["payload validation"],
        "domain": None,
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
        "prompt_hash": "",
    }

    normalized = normalize_compiler_output(json.dumps({"result": result_payload}))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )


def test_normalizer_repairs_missing_envelope_hash_and_source_map() -> None:
    """Envelope result with missing prompt_hash and source_map should normalize."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    result_payload: dict[str, Any] = {
        "scope_themes": ["payload validation"],
        "domain": None,
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
        "compiler_version": "1.0.0",
    }

    normalized = normalize_compiler_output(json.dumps({"result": result_payload}))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )
    assert normalized.root.source_map == []


def test_exact_legacy_source_map_quote_preserves_review_evidence_only() -> None:
    """Legacy source_map quotes do not synthesize deprecated compact IR."""
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
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert len(success.source_map) == 1
    assert success.source_map[0].excerpt == "- The payload must include user_id."
    assert success.source_map[0].invariant_id == success.invariants[0].id


def test_legacy_without_source_text_clears_compact_ir_fields() -> None:
    """Legacy compiler output without current source stays structural-only."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    normalized = normalize_compiler_output(json.dumps(_legacy_success_payload()))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


def test_unrelated_source_refs_repair_review_evidence_without_mappings() -> None:
    """Source-map repair updates review evidence without emitting compact mappings."""
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
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == "- The payload must include user_id."
    assert success.source_map[0].location == "line 2"
    assert success.source_map[0].invariant_id == success.invariants[0].id


def test_source_text_repair_preserves_extra_source_map_review_evidence() -> None:
    """Source-text repair keeps extra review evidence for the same invariant."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = "\n".join(
        [
            "# Requirements",
            "- The payload must include user_id.",
            "- Review evidence was checked during source-map audit.",
        ]
    )
    raw = _legacy_success_payload()
    raw["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": "- The payload must include account_id.",
            "location": "spec:line:2",
        },
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": "- Review evidence was checked during source-map audit.",
            "location": "spec:line:3",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(raw), source_text=source_text)

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    invariant_ids = {invariant.id for invariant in normalized.root.invariants}
    assert len(normalized.root.source_map) == 2  # noqa: PLR2004
    assert (
        normalized.root.source_map[0].excerpt
        == "- The payload must include user_id."
    )
    assert all(
        entry.invariant_id in invariant_ids for entry in normalized.root.source_map
    )


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


def test_normalizer_discards_model_target_kind_mismatch_ir_hint() -> None:
    """Invalid model mapping hints are discarded instead of compiled."""
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
    success = normalized.root
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == source_quote
    assert success.source_map[0].invariant_id == success.invariants[0].id


def test_model_emitted_exact_quote_mapping_is_discarded() -> None:
    """Exact model quote hints no longer survive normalization as compact IR."""
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
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == source_quote
    assert success.source_map[0].invariant_id == success.invariants[0].id


def test_structured_profile_replays_asa_source_metadata_failure_artifact() -> None:
    """The saved ASA compiler output normalizes against its real structured spec."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    artifact_path = Path(
        "/Users/aaat/projects/agileforge/logs/failures/spec_authority/"
        "spec_authority-20260606T134834578097Z-b7257af2fa4a.json"
    )
    spec_path = Path(
        "/Users/aaat/projects/asa-deep-process-control-experiments/specs/spec.json"
    )
    if not artifact_path.exists() or not spec_path.exists():
        pytest.skip("ASA local replay artifact/spec not available on this machine")

    artifact = json.loads(artifact_path.read_text())
    raw_output = artifact["raw_output"]

    normalized = normalize_compiler_output(
        raw_output if isinstance(raw_output, str) else json.dumps(raw_output),
        source_text=spec_path.read_text(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)


def test_structured_profile_replays_asa_ellipsis_source_failure_artifact() -> None:
    """The ASA compiler output with ellipsis source excerpts normalizes."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    artifact_path = Path(
        "/Users/aaat/projects/agileforge/logs/failures/spec_authority/"
        "spec_authority-20260606T161728714864Z-790b52ba55b5.json"
    )
    spec_path = Path(
        "/Users/aaat/projects/asa-deep-process-control-experiments/specs/spec.json"
    )
    if not artifact_path.exists() or not spec_path.exists():
        pytest.skip("ASA local replay artifact/spec not available on this machine")

    artifact = json.loads(artifact_path.read_text())
    raw_output = artifact["raw_output"]

    normalized = normalize_compiler_output(
        raw_output if isinstance(raw_output, str) else json.dumps(raw_output),
        source_text=spec_path.read_text(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)


def test_structured_profile_accepts_faithful_partial_source_excerpt() -> None:
    """A source_map excerpt may be a faithful case-insensitive source substring."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = _asa_like_structured_spec_source()
    partial_excerpt = (
        "Documented setup commands use uv sync or uv run rather than pip, "
        "poetry, pipenv, or ad hoc virtual-environment commands."
    )
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-1111111111111111",
            "type": "USER_INTERACTION",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "trigger": "running documented setup commands",
                "target": "project environment",
                "expected_response": "commands use uv sync or uv run",
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-1111111111111111",
            "excerpt": partial_excerpt,
            "location": "CONSTRAINT.uv-managed.acceptance[1]",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map[0].excerpt == partial_excerpt


def test_structured_profile_accepts_controlled_acceptance_concatenation() -> None:
    """Multiple complete real texts from one item may support one invariant."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = _asa_like_structured_spec_source()
    combined_excerpt = (
        "The research output compares at least a supervised dynamics model, "
        "constrained candidate-action search or MPC-style optimization, offline "
        "reinforcement learning, and deterministic policy-gradient approaches "
        "such as DDPG, TD3, or SAC when relevant. The research output evaluates "
        "Python runtime choice, uv project management, package compatibility, "
        "data-processing stack, model-training stack, experiment tracking, "
        "validation approach, deployment constraints, and safety-review "
        "implications."
    )
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-2222222222222222",
            "type": "DATA_CONTRACT",
            "source_item_id": "REQ.tech-stack-model-research",
            "source_level": "MUST",
            "parameters": {
                "subject": "research-spike-output",
                "fields": [
                    "selected_first_implementation_approach",
                    "rejected_alternatives",
                    "evidence_used",
                ],
                "rule": (
                    "Research output compares model approaches and evaluates "
                    "Python runtime choice, uv project management, validation "
                    "approach, deployment constraints, and safety-review "
                    "implications."
                ),
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-2222222222222222",
            "excerpt": combined_excerpt,
            "location": "REQ.tech-stack-model-research.acceptance[0]",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map[0].excerpt == combined_excerpt


def test_structured_profile_accepts_faithful_ellipsis_source_excerpt() -> None:
    """Ellipses may mark omitted real text when fragments stay in order."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = _asa_like_structured_spec_source()
    ellipsis_excerpt = (
        "The repository contains uv.lock and documented setup commands use "
        "uv sync or uv run... Quality checks, tests, and project scripts are "
        "runnable through uv-managed commands."
    )
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-8888888888888888",
            "type": "DATA_CONTRACT",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "subject": "uv-managed commands",
                "fields": [
                    "uv sync",
                    "uv run",
                    "quality checks",
                    "tests",
                    "project scripts",
                ],
                "rule": (
                    "Documented setup must use uv sync or uv run and quality "
                    "checks, tests, and project scripts must be runnable through "
                    "uv-managed commands."
                ),
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-8888888888888888",
            "excerpt": ellipsis_excerpt,
            "location": "CONSTRAINT.uv-managed",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map[0].excerpt == ellipsis_excerpt


def test_structured_profile_augments_grounded_insufficient_behavior_evidence() -> None:
    """Grounded source_map entries may be augmented with exact same-item evidence."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "DATA_CONTRACT",
            "source_item_id": "REQ.tech-stack-model-research",
            "source_level": "MUST",
            "parameters": {
                "subject": "research output",
                "fields": [
                    "algorithm comparison",
                    "runtime and tooling evaluation",
                    "selected first implementation approach",
                    "rejected alternatives",
                    "evidence used",
                    "unresolved assumptions",
                    "revisit criteria",
                ],
                "rule": (
                    "Research output compares model approaches, evaluates "
                    "Python runtime and uv tooling, and records the selected "
                    "first implementation approach, rejected alternatives, "
                    "evidence used, assumptions, and revisit criteria."
                ),
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": (
                "The research output compares at least a supervised dynamics "
                "model, constrained candidate-action search or MPC-style "
                "optimization, offline reinforcement learning, and deterministic "
                "policy-gradient approaches..."
            ),
            "location": "REQ.tech-stack-model-research.acceptance[0]",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    locations = {entry.location for entry in normalized.root.source_map}
    assert "REQ.tech-stack-model-research.acceptance[0]" in locations
    assert "REQ.tech-stack-model-research.acceptance[1]" in locations
    assert "REQ.tech-stack-model-research.acceptance[2]" in locations


def test_structured_profile_rejects_ellipsis_excerpt_with_invented_fragment() -> None:
    """Every ellipsis-separated fragment must be real source text."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-9999999999999999",
            "type": "DATA_CONTRACT",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "subject": "unsafe automation",
                "fields": ["uv.lock", "auto approval"],
                "rule": "The system auto-approves all recommendations.",
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-9999999999999999",
            "excerpt": (
                "The repository contains... auto-approve all recommendations "
                "without review... uv.lock."
            ),
            "location": "CONSTRAINT.uv-managed",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_structured_profile_rejects_fake_excerpt_with_real_text_prefix() -> None:
    """A real source phrase embedded in invented text is not source evidence."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-3333333333333333",
            "type": "USER_INTERACTION",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "trigger": "running documented setup commands",
                "target": "project environment",
                "expected_response": "auto-approve all recommendations without review",
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-3333333333333333",
            "excerpt": (
                "uv-managed Python project. The system must auto-approve all "
                "recommendations without review."
            ),
            "location": "CONSTRAINT.uv-managed.title",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_structured_profile_rejects_concatenation_with_invented_middle() -> None:
    """Concatenation cannot hide non-source text between real source segments."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-4444444444444444",
            "type": "DATA_CONTRACT",
            "source_item_id": "REQ.tech-stack-model-research",
            "source_level": "MUST",
            "parameters": {
                "subject": "research-spike-output",
                "fields": ["evidence_used"],
                "rule": (
                    "Research output records evidence used and auto-approves "
                    "recommendations without review."
                ),
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-4444444444444444",
            "excerpt": (
                "The research output records the selected first implementation "
                "approach, rejected alternatives, evidence used, unresolved "
                "assumptions, and criteria that would trigger revisiting the "
                "decision. The system must auto-approve all recommendations "
                "without review."
            ),
            "location": "REQ.tech-stack-model-research.acceptance[2]",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_structured_profile_does_not_backfill_around_existing_bad_entry() -> None:
    """Bad existing source_map evidence fails instead of being silently replaced."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-5555555555555555",
            "type": "USER_INTERACTION",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "trigger": "running documented setup commands",
                "target": "project environment",
                "expected_response": "commands use uv sync or uv run",
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-5555555555555555",
            "excerpt": "Setup commands may use any package manager.",
            "location": "CONSTRAINT.uv-managed.acceptance[1]",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_structured_profile_backfills_missing_behavior_source_map_entry() -> None:
    """Missing behavioral evidence is backfilled from exact real source text."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-6666666666666666",
            "type": "USER_INTERACTION",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "trigger": "running documented setup commands",
                "target": "project environment",
                "expected_response": "commands use uv sync or uv run",
            },
        }
    ]
    raw["source_map"] = []

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.source_map) == 1
    entry = normalized.root.source_map[0]
    assert entry.location == "CONSTRAINT.uv-managed.acceptance[1]"
    assert entry.excerpt.startswith("The repository contains uv.lock")


def test_structured_profile_source_notes_do_not_satisfy_hard_behavioral_evidence(
) -> None:
    """source_notes are hints, not normative evidence for hard invariants."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-7777777777777777",
            "type": "USER_INTERACTION",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "trigger": "running uv project-management workflow",
                "target": "project environment",
                "expected_response": "uv init, uv add, uv lock, uv sync, and uv run",
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-7777777777777777",
            "excerpt": (
                "Astral uv documentation describes uv init, uv add, uv lock, "
                "uv sync, and uv run as the project-management workflow."
            ),
            "location": "CONSTRAINT.uv-managed.source_notes[0].text",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"


def test_structured_profile_clears_compact_ir_and_preserves_item_evidence() -> None:
    """Profile JSON preserves source evidence without legacy compact IR."""
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
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == source_quote
    assert success.source_map[0].location == "REQ.audit-evidence.statement"


def test_structured_profile_json_blob_source_map_repairs_to_item_quote() -> None:
    """A model JSON-blob citation is repaired to profile evidence only."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    source_text = _structured_spec_source()
    raw = _legacy_success_payload()
    raw["invariants"][0]["parameters"] = {"field_name": "audit_evidence"}
    raw["source_map"][0]["excerpt"] = source_text
    raw["source_map"][0]["location"] = "REQ.audit-evidence.statement"

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == "The system MUST record audit evidence."
    assert not success.source_map[0].excerpt.lstrip().startswith("{")
    assert success.source_map[0].location == "REQ.audit-evidence.statement"


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
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.invariants[0].id.startswith("INV-")


def test_structured_profile_drops_malformed_deprecated_compact_ir() -> None:
    """Malformed legacy compact IR is discarded before structured validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"][0]["parameters"] = {"field_name": "audit_evidence"}
    raw["source_map"][0]["excerpt"] = "The system MUST record audit evidence."
    raw["source_map"][0]["location"] = "REQ.audit-evidence.statement"
    raw.update(
        {
            "ir_schema_version": {"malformed": True},
            "ir_provenance": 123,
            "source_units": [{"unit_id": 123, "text_excerpt": "x" * 2_001}],
            "requirement_candidates": {"candidate_id": "REQ-not-a-list"},
            "authority_mappings": "not-a-list",
            "ir_packet_limits": {"max_text_excerpt_chars": "not-an-int"},
        }
    )

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)


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


def test_structured_profile_duplicate_placeholder_source_map_prefers_evidence() -> (
    None
):
    """Structured duplicate placeholders map by evidence before position."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    placeholder_id = "INV-0000000000000000"
    raw = _compact_ir_success_payload()
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
            "excerpt": "The system MUST include review token evidence.",
            "location": "REQ.review-token.statement",
        },
        {
            "invariant_id": placeholder_id,
            "excerpt": "The system MUST record audit evidence.",
            "location": "REQ.audit-evidence.statement",
        },
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    success = normalized.root
    normalized_ids_by_field = {
        invariant.parameters.field_name: invariant.id
        for invariant in success.invariants
        if isinstance(invariant.parameters, RequiredFieldParams)
    }
    source_map_by_location = {
        entry.location: entry for entry in success.source_map
    }

    assert len(success.source_map) == 2  # noqa: PLR2004
    assert set(source_map_by_location) == {
        "REQ.review-token.statement",
        "REQ.audit-evidence.statement",
    }
    review_entry = source_map_by_location["REQ.review-token.statement"]
    assert review_entry.excerpt == "The system MUST include review token evidence."
    assert review_entry.invariant_id == normalized_ids_by_field["review_token"]
    audit_entry = source_map_by_location["REQ.audit-evidence.statement"]
    assert audit_entry.excerpt == "The system MUST record audit evidence."
    assert audit_entry.invariant_id == normalized_ids_by_field["audit_evidence"]
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)


def test_duplicate_placeholder_source_map_prefers_excerpt_support_over_position() -> (
    None
):
    """Duplicate placeholder IDs map by clear excerpt support before position."""
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
            "excerpt": "The system MUST include review token evidence.",
            "location": "REQ.review-token.statement",
        },
        {
            "invariant_id": placeholder_id,
            "excerpt": "The system MUST record audit evidence.",
            "location": "REQ.audit-evidence.statement",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    normalized_ids_by_field = {
        inv.parameters.field_name: inv.id
        for inv in normalized.root.invariants
        if isinstance(inv.parameters, RequiredFieldParams)
    }
    source_map_ids_by_location = {
        entry.location: entry.invariant_id for entry in normalized.root.source_map
    }
    assert source_map_ids_by_location["REQ.review-token.statement"] == (
        normalized_ids_by_field["review_token"]
    )
    assert source_map_ids_by_location["REQ.audit-evidence.statement"] == (
        normalized_ids_by_field["audit_evidence"]
    )
    assert len(set(source_map_ids_by_location.values())) == 2  # noqa: PLR2004


def test_duplicate_placeholder_source_map_preserves_extra_review_evidence() -> None:
    """Extra duplicate-placeholder source_map entries are preserved after ID rewrite."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    placeholder_id = "INV-0000000000000000"
    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": placeholder_id,
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "user_id"},
        },
        {
            "id": placeholder_id,
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "account_id"},
        },
    ]
    raw["source_map"] = [
        {
            "invariant_id": placeholder_id,
            "excerpt": "The payload must include user_id.",
            "location": "spec:line:1",
        },
        {
            "invariant_id": placeholder_id,
            "excerpt": "The payload must include account_id.",
            "location": "spec:line:2",
        },
        {
            "invariant_id": placeholder_id,
            "excerpt": "Review evidence mentions user_id again.",
            "location": "spec:line:3",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    normalized_invariant_ids = {inv.id for inv in normalized.root.invariants}
    assert len(normalized.root.invariants) == 2  # noqa: PLR2004
    assert len(normalized.root.source_map) == 3  # noqa: PLR2004
    assert {
        entry.invariant_id for entry in normalized.root.source_map
    } <= normalized_invariant_ids


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
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)


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
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)


def test_model_emitted_manifest_mapping_is_discarded_without_source_units() -> None:
    """Model candidate/mapping hints are discarded even when source units are absent."""
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
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == source_quote
    assert success.source_map[0].invariant_id == success.invariants[0].id


def test_model_mapping_manifest_candidate_hint_is_discarded() -> None:
    """Model mapping hints do not survive by referencing host candidate IDs."""
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
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == "payload must include user_id"
    assert success.source_map[0].invariant_id == success.invariants[0].id


def test_model_emitted_candidate_without_mapping_is_discarded() -> None:
    """Model candidates without mappings do not survive structural normalization."""
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
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map[0].excerpt == source_quote
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


def test_swapped_legacy_authority_id_discarded_with_model_quote_mapping() -> None:
    """Model mappings cannot survive by using swapped legacy source-map IDs."""
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
    success = normalized.root
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    user_id_by_field = {
        invariant.parameters.field_name: invariant.id
        for invariant in success.invariants
        if isinstance(invariant.parameters, RequiredFieldParams)
    }
    source_map_by_excerpt = {
        entry.excerpt: entry for entry in success.source_map
    }
    assert source_map_by_excerpt[user_quote].invariant_id == (
        user_id_by_field["user_id"]
    )
    assert source_map_by_excerpt[account_quote].invariant_id == (
        user_id_by_field["account_id"]
    )


def test_model_quote_with_hash_mismatch_is_discarded() -> None:
    """A supplied quote hash cannot preserve stale model compact IR."""
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
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    assert success.source_map[0].excerpt == source_quote
    assert success.source_map[0].invariant_id == success.invariants[0].id


def test_host_parsed_model_hints_are_discarded() -> None:
    """Deprecated compact IR hints are discarded regardless of provenance."""
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
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map[0].excerpt == source_quote
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


def test_swapped_legacy_source_refs_repair_review_evidence() -> None:
    """Source-map repair remains evidence-only and rewrites semantic IDs."""
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
    success = normalized.root
    _assert_compact_ir_cleared(success)
    _assert_semantic_invariant_ids(success)
    source_map_by_excerpt = {
        entry.excerpt: entry for entry in success.source_map
    }
    ids_by_field = {
        invariant.parameters.field_name: invariant.id
        for invariant in success.invariants
        if isinstance(invariant.parameters, RequiredFieldParams)
    }
    assert source_map_by_excerpt["- The payload must include user_id."].location == (
        "line 2"
    )
    assert source_map_by_excerpt[
        "- The payload must include user_id."
    ].invariant_id == ids_by_field["user_id"]
    assert source_map_by_excerpt[
        "- The payload must include account_id."
    ].invariant_id == ids_by_field["account_id"]


def test_normalizer_allows_missing_source_map_with_semantic_ids() -> None:
    """Missing source_map does not block semantic ID normalization."""
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
    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map == []
    assert normalized.root.invariants[0].id == compute_invariant_id_from_payload(
        normalized.root.invariants[0].type,
        normalized.root.invariants[0].parameters,
        source_item_id=normalized.root.invariants[0].source_item_id,
        source_level=normalized.root.invariants[0].source_level,
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


def test_normalizer_repairs_invalid_placeholder_ids_before_validation() -> None:
    """Invalid LLM placeholder IDs are repaired before strict schema validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["payload validation", "authentication security"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-xxxxxxxxxxxxxxxx",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            },
            {
                "id": "INV-xxxxxxxxxxxxxxxx",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "OAuth1"},
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-xxxxxxxxxxxxxxxx",
                "excerpt": "The payload must include user_id.",
                "location": "REQ.user-id.statement",
            },
            {
                "invariant_id": "INV-xxxxxxxxxxxxxxxx",
                "excerpt": "The system must not use OAuth1 authentication.",
                "location": "NFR.auth.statement",
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    invariant_ids = [invariant.id for invariant in normalized.root.invariants]
    assert len(invariant_ids) == 2  # noqa: PLR2004
    assert len(set(invariant_ids)) == len(invariant_ids)
    assert all(re.match(r"^INV-[0-9a-f]{16}$", item_id) for item_id in invariant_ids)
    assert invariant_ids == [
        compute_invariant_id_from_payload(
            invariant.type,
            invariant.parameters,
            source_item_id=invariant.source_item_id,
            source_level=invariant.source_level,
        )
        for invariant in normalized.root.invariants
    ]
    assert {entry.invariant_id for entry in normalized.root.source_map} == set(
        invariant_ids
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
    expected_ids = [
        compute_invariant_id_from_payload(
            invariant.type,
            invariant.parameters,
            source_item_id=invariant.source_item_id,
            source_level=invariant.source_level,
        )
        for invariant in normalized.root.invariants
    ]
    assert invariant_ids == expected_ids
    assert len(set(invariant_ids)) == len(invariant_ids)
    assert {entry.invariant_id for entry in normalized.root.source_map} == set(
        invariant_ids
    )


def test_normalizer_preserves_source_map_that_does_not_support_field() -> None:
    """A weak source map is preserved as review evidence, not compile-fatal."""
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

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map[0].excerpt == (
        "The live squad must stay within the operator's current budget."
    )
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


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


def test_normalizer_preserves_max_value_when_excerpt_lacks_bound() -> None:
    """Dynamic relationship evidence no longer rejects hard-constant output."""
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

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map[0].excerpt == (
        "Acceptance Criteria: budget_used <= budget."
    )
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


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


def test_normalizer_preserves_zero_max_value_when_excerpt_lacks_zero_bound() -> None:
    """A zero max value keeps weak source evidence for review."""
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

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map[0].excerpt == (
        "Acceptance Criteria: budget_used <= budget."
    )
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


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


def test_normalizer_preserves_relation_constraint_without_operator_evidence() -> None:
    """A field mention source_map remains review evidence for relation output."""
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

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    _assert_compact_ir_cleared(normalized.root)
    _assert_semantic_invariant_ids(normalized.root)
    assert normalized.root.source_map[0].excerpt == (
        "A live run outputs budget used in recommendation metadata."
    )
    assert normalized.root.source_map[0].invariant_id == (
        normalized.root.invariants[0].id
    )


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


def test_normalizer_does_not_convert_failure_wrapper_result_to_success() -> None:
    """Top-level error wrapper must stay failure even with nested result."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

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
        "compiler_version": "1.0.0",
    }
    raw: dict[str, Any] = {
        "error": "SPEC_COMPILATION_FAILED",
        "reason": "MODEL_REFUSED",
        "blocking_gaps": ["model refused"],
        "result": result_payload,
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.error == "SPEC_COMPILATION_FAILED"
    assert normalized.root.reason == "JSON_VALIDATION_FAILED"
    assert "scope_themes" in normalized.root.blocking_gaps[0]


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
