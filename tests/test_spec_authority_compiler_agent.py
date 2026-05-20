# ruff: noqa: E501
"""
Contract tests for spec_authority_compiler_agent.

These tests lock schema shape, invariant typing, and deterministic ID rules.
"""

import json
import re
from typing import Any

import pytest
from google.genai import types
from pydantic import ValidationError

from orchestrator_agent.agent_tools.spec_authority_compiler_agent.compiler_contract import (
    classify_invariant_from_text,
    compute_invariant_id,
    compute_invariant_id_from_payload,
    compute_prompt_hash,
    compute_spec_hash,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
    normalize_compiler_output,
)
from utils.spec_schemas import (
    ForbiddenCapabilityParams,
    InvariantType,
    MaxValueParams,
    RequiredFieldParams,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerInput,
    SpecAuthorityCompilerOutput,
)


def _compiler_instructions() -> str:
    return SPEC_AUTHORITY_COMPILER_INSTRUCTIONS


def test_compiler_instructions_do_not_require_candidate_manifest() -> None:
    """Compiler prompt must not depend on host candidate extraction."""
    instructions = _compiler_instructions()

    assert "candidate_manifest" not in instructions
    assert "requirement_candidates" not in instructions
    assert "authority_mappings" not in instructions
    assert "agileforge.spec.v1" in instructions
    assert "Do not infer authority from Markdown narrative" in instructions
    assert "source_map is review evidence" in instructions


def test_compiler_instructions_document_structured_spec_support_matrix() -> None:
    """Compiler prompt must document structured source handling."""
    instructions = _compiler_instructions()

    assert "agileforge.spec.v1" in instructions
    assert "AgileForge authority support matrix" in instructions
    assert (
        "REQ/DATA/INTERFACE become REQUIRED_FIELD or RELATION_CONSTRAINT"
        in instructions
    )
    assert "Unsupported normative items become gaps" in instructions
    assert "agileforge.spec_legacy_markdown.v1" not in instructions


def test_compiler_instructions_document_behavioral_authority_types() -> None:
    """Compiler prompt must teach behavioral authority generation."""
    instructions = _compiler_instructions()

    for invariant_type in (
        "USER_INTERACTION",
        "STATE_TRANSITION",
        "DATA_CONTRACT",
        "ROUTE_CONTRACT",
        "VISIBILITY_RULE",
    ):
        assert invariant_type in instructions

    assert "source_item_id" in instructions
    assert "source_level" in instructions
    assert "Do not compress event-driven behavior into REQUIRED_FIELD" in instructions


def test_compiler_instructions_document_host_semantic_ids() -> None:
    """Compiler prompt must align with host semantic-only invariant IDs."""
    instructions = _compiler_instructions()

    assert (
        "semantic-only IDs from invariant type and canonical parameters"
        in instructions
    )
    assert "normalize(excerpt)" not in instructions


class TestSpecAuthorityCompilerInput:
    """Validate input schema rules (exactly one source field)."""

    def test_requires_exactly_one_source_field(self) -> None:
        """Verify requires exactly one source field."""
        with pytest.raises(ValidationError):
            SpecAuthorityCompilerInput(
                spec_source="raw text",
                spec_content_ref="specs/spec.md",
                domain_hint=None,
                product_id=None,
                spec_version_id=None,
            )

        with pytest.raises(ValidationError):
            SpecAuthorityCompilerInput(
                spec_source=None,
                spec_content_ref=None,
                domain_hint=None,
                product_id=None,
                spec_version_id=None,
            )

    def test_accepts_spec_source_only(self) -> None:
        """Verify accepts spec source only."""
        payload = SpecAuthorityCompilerInput(
            spec_source="Raw spec text",
            spec_content_ref=None,
            domain_hint=None,
            product_id=None,
            spec_version_id=None,
        )
        assert payload.spec_source == "Raw spec text"
        assert payload.spec_content_ref is None

    def test_accepts_spec_source_format_with_spec_source(self) -> None:
        """Verify compiler input carries source format metadata."""
        payload = SpecAuthorityCompilerInput(
            spec_source="The payload must include user_id.",
            spec_content_ref=None,
            domain_hint=None,
            product_id=None,
            spec_version_id=None,
            spec_source_format="agileforge.spec.v1",
        )

        assert payload.spec_source_format == "agileforge.spec.v1"
        assert not hasattr(payload, "candidate_manifest")

    def test_rejects_unknown_spec_source_format(self) -> None:
        """Verify compiler input only allows known source formats."""
        with pytest.raises(ValidationError):
            SpecAuthorityCompilerInput.model_validate(
                {
                    "spec_source": "Raw spec text",
                    "spec_content_ref": None,
                    "domain_hint": None,
                    "product_id": None,
                    "spec_version_id": None,
                    "spec_source_format": "not-a-real-format",
                }
            )

        with pytest.raises(ValidationError):
            SpecAuthorityCompilerInput.model_validate(
                {
                    "spec_source": "Raw spec text",
                    "spec_content_ref": None,
                    "domain_hint": None,
                    "product_id": None,
                    "spec_version_id": None,
                    "spec_source_format": "agileforge.spec_legacy_markdown.v1",
                }
            )

    def test_rejects_stale_candidate_manifest_key(self) -> None:
        """Verify stale host candidate manifest keys are not accepted."""
        with pytest.raises(ValidationError):
            SpecAuthorityCompilerInput.model_validate(
                {
                    "spec_source": "Raw spec text",
                    "spec_content_ref": None,
                    "domain_hint": None,
                    "product_id": None,
                    "spec_version_id": None,
                    "candidate_manifest": [],
                }
            )

    def test_accepts_spec_content_ref_only(self) -> None:
        """Verify accepts spec content ref only."""
        payload = SpecAuthorityCompilerInput(
            spec_source=None,
            spec_content_ref="specs/spec.md",
            domain_hint="payments",
            product_id=1,
            spec_version_id=10,
        )
        assert payload.spec_content_ref == "specs/spec.md"
        assert payload.domain_hint == "payments"


class TestCompilerOutputSchema:
    """Schema / contract tests for compiler output."""

    def test_schema_closes_eligible_feature_rule_items(self) -> None:
        """Verify schema closes eligible feature rule items."""
        schema = SpecAuthorityCompilerEnvelope.model_json_schema()
        items = schema["$defs"]["SpecAuthorityCompilationSuccess"]["properties"][
            "eligible_feature_rules"
        ]["items"]
        if "$ref" in items:
            ref_name = items["$ref"].split("/")[-1]
            items = schema["$defs"][ref_name]

        assert items["additionalProperties"] is False
        assert "rule" in items["properties"]

    def test_success_payload_valid_json(self) -> None:
        """Verify success payload valid json."""
        payload: dict[str, Any] = {
            "scope_themes": ["API"],
            "invariants": [
                {
                    "id": "INV-2f7c9b3d4a1e5c8b",
                    "type": "FORBIDDEN_CAPABILITY",
                    "parameters": {"capability": "redis"},
                }
            ],
            "eligible_feature_rules": [],
            "gaps": [],
            "assumptions": [],
            "source_map": [
                {
                    "invariant_id": "INV-2f7c9b3d4a1e5c8b",
                    "excerpt": "Must not use Redis.",
                    "location": "spec:line:1",
                }
            ],
            "compiler_version": "1.0.0",
            "prompt_hash": "a" * 64,
        }

        parsed = SpecAuthorityCompilerOutput.model_validate_json(json.dumps(payload))
        assert isinstance(parsed.root, SpecAuthorityCompilationSuccess)
        assert parsed.root.compiler_version == "1.0.0"

    def test_failure_payload_valid_json(self) -> None:
        """Verify failure payload valid json."""
        payload: dict[str, Any] = {
            "error": "SPEC_COMPILATION_FAILED",
            "reason": "Missing required sections",
            "blocking_gaps": ["No invariants section"],
        }

        parsed = SpecAuthorityCompilerOutput.model_validate_json(json.dumps(payload))
        assert isinstance(parsed.root, SpecAuthorityCompilationFailure)
        assert parsed.root.error == "SPEC_COMPILATION_FAILED"

    def test_envelope_accepts_success_payload(self) -> None:
        """Verify envelope accepts success payload."""
        payload: dict[str, Any] = {
            "scope_themes": ["API"],
            "invariants": [
                {
                    "id": "INV-2f7c9b3d4a1e5c8b",
                    "type": "FORBIDDEN_CAPABILITY",
                    "parameters": {"capability": "redis"},
                }
            ],
            "eligible_feature_rules": [],
            "gaps": [],
            "assumptions": [],
            "source_map": [
                {
                    "invariant_id": "INV-2f7c9b3d4a1e5c8b",
                    "excerpt": "Must not use Redis.",
                    "location": "spec:line:1",
                }
            ],
            "compiler_version": "1.0.0",
            "prompt_hash": "a" * 64,
        }

        parsed = SpecAuthorityCompilerEnvelope.model_validate_json(
            json.dumps({"result": payload})
        )
        assert isinstance(parsed.result, SpecAuthorityCompilationSuccess)

    def test_envelope_accepts_failure_payload(self) -> None:
        """Verify envelope accepts failure payload."""
        payload: dict[str, Any] = {
            "error": "SPEC_COMPILATION_FAILED",
            "reason": "Missing required sections",
            "blocking_gaps": ["No invariants section"],
        }

        parsed = SpecAuthorityCompilerEnvelope.model_validate_json(
            json.dumps({"result": payload})
        )
        assert isinstance(parsed.result, SpecAuthorityCompilationFailure)

    def test_extra_keys_are_forbidden(self) -> None:
        """Verify extra keys are forbidden."""
        payload: dict[str, Any] = {
            "error": "SPEC_COMPILATION_FAILED",
            "reason": "Bad output",
            "blocking_gaps": [],
            "extra": "not allowed",
        }

        with pytest.raises(ValidationError):
            SpecAuthorityCompilerOutput.model_validate_json(json.dumps(payload))

    def test_invariant_type_must_be_allowed_enum(self) -> None:
        """Verify invariant type must be allowed enum."""
        payload: dict[str, Any] = {
            "scope_themes": [],
            "invariants": [
                {
                    "id": "INV-2f7c9b3d4a1e5c8b",
                    "type": "UNKNOWN_TYPE",
                    "parameters": {"capability": "x"},
                }
            ],
            "eligible_feature_rules": [],
            "gaps": [],
            "assumptions": [],
            "source_map": [],
            "compiler_version": "1.0.0",
            "prompt_hash": "a" * 64,
        }

        with pytest.raises(ValidationError):
            SpecAuthorityCompilerOutput.model_validate_json(json.dumps(payload))

    def test_invariant_id_format(self) -> None:
        """Verify invariant id format."""
        invariant_id = compute_invariant_id(
            excerpt="Must not use Redis.",
            invariant_type=InvariantType.FORBIDDEN_CAPABILITY,
        )
        assert re.match(r"^INV-[0-9a-f]{16}$", invariant_id)

    def test_normalizer_overwrites_placeholder_hash_and_ids(self) -> None:
        """Verify normalizer overwrites placeholder hash and ids."""
        payload: dict[str, Any] = {
            "scope_themes": ["payload"],
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
                    "excerpt": "The payload must include user_id.",
                    "location": "Requirements",
                }
            ],
            "compiler_version": "1.0.0",
            "prompt_hash": "0" * 64,
        }

        normalized = normalize_compiler_output(json.dumps(payload))
        assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)

        expected_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
        assert normalized.root.prompt_hash == expected_hash

        expected_id = compute_invariant_id_from_payload(
            InvariantType.REQUIRED_FIELD,
            normalized.root.invariants[0].parameters,
        )
        assert normalized.root.invariants[0].id == expected_id


class TestInvariantClassificationRules:
    """Mapping rules for deterministic invariant extraction."""

    def test_forbidden_capability_mapping(self) -> None:
        """Verify forbidden capability mapping."""
        invariant = classify_invariant_from_text("System must not use Redis.")
        assert invariant is not None
        assert invariant.type == InvariantType.FORBIDDEN_CAPABILITY
        assert isinstance(invariant.parameters, ForbiddenCapabilityParams)
        assert invariant.parameters.capability == "redis"

    def test_required_field_mapping(self) -> None:
        """Verify required field mapping."""
        invariant = classify_invariant_from_text("Requests must include auth_token.")
        assert invariant is not None
        assert invariant.type == InvariantType.REQUIRED_FIELD
        assert isinstance(invariant.parameters, RequiredFieldParams)
        assert invariant.parameters.field_name == "auth_token"

    def test_max_value_mapping(self) -> None:
        """Verify max value mapping."""
        invariant = classify_invariant_from_text("Latency must be <= 200 ms.")
        assert invariant is not None
        assert invariant.type == InvariantType.MAX_VALUE
        assert isinstance(invariant.parameters, MaxValueParams)
        assert invariant.parameters.field_name == "latency"
        assert invariant.parameters.max_value == 200  # noqa: PLR2004


class TestReproducibilityGuards:
    """Deterministic hashing and ID stability checks."""

    def test_spec_hash_stable_for_same_input(self) -> None:
        """Verify spec hash stable for same input."""
        content = "Spec v1\nMust not use Redis."
        assert compute_spec_hash(content) == compute_spec_hash(content)

    def test_prompt_hash_changes_when_prompt_changes(self) -> None:
        """Verify prompt hash changes when prompt changes."""
        hash_one = compute_prompt_hash("Prompt A")
        hash_two = compute_prompt_hash("Prompt B")
        assert hash_one != hash_two

    def test_invariant_id_stable_for_same_input(self) -> None:
        """Verify invariant id stable for same input."""
        excerpt = "Requests must include auth_token."
        invariant_id_1 = compute_invariant_id(
            excerpt=excerpt,
            invariant_type=InvariantType.REQUIRED_FIELD,
        )
        invariant_id_2 = compute_invariant_id(
            excerpt=excerpt,
            invariant_type=InvariantType.REQUIRED_FIELD,
        )
        assert invariant_id_1 == invariant_id_2


class TestCorrectedExpectedOutput:
    """
    Corrected TEST CASE 1: expected output matching the enforced schema.

    Input: "The payload must include user_id."
    """

    def test_case_1_expected_output_matches_schema(self) -> None:
        """Validate that the corrected expected output passes schema validation."""
        input_sentence = "The payload must include user_id."

        # Compute deterministic values from contract helpers
        expected_invariant = classify_invariant_from_text(input_sentence)
        assert expected_invariant is not None
        expected_id = expected_invariant.id

        # Build expected output using actual computed values
        expected_output: dict[str, Any] = {
            "scope_themes": [],
            "invariants": [
                {
                    "id": expected_id,
                    "type": "REQUIRED_FIELD",
                    "parameters": {"field_name": "user_id"},
                }
            ],
            "eligible_feature_rules": [],
            "gaps": [],
            "assumptions": [],
            "source_map": [
                {
                    "invariant_id": expected_id,
                    "excerpt": input_sentence,
                    "location": "spec:line:1",
                }
            ],
            "compiler_version": "1.0.0",
            "prompt_hash": "a"
            * 64,  # Schema requires 64 hex chars; actual value from agent
        }

        # Validate against schema
        parsed = SpecAuthorityCompilerOutput.model_validate_json(
            json.dumps(expected_output)
        )
        assert isinstance(parsed.root, SpecAuthorityCompilationSuccess)
        assert len(parsed.root.invariants) == 1
        assert parsed.root.invariants[0].id == expected_id
        assert parsed.root.invariants[0].type == InvariantType.REQUIRED_FIELD
        assert isinstance(parsed.root.invariants[0].parameters, RequiredFieldParams)
        assert parsed.root.invariants[0].parameters.field_name == "user_id"

    def test_case_1_invariant_id_is_deterministic(self) -> None:
        """Verify invariant ID is stable across multiple computations."""
        input_sentence = "The payload must include user_id."

        inv_1 = classify_invariant_from_text(input_sentence)
        inv_2 = classify_invariant_from_text(input_sentence)

        assert inv_1 is not None
        assert inv_2 is not None
        assert inv_1.id == inv_2.id
        assert re.match(r"^INV-[0-9a-f]{16}$", inv_1.id)


@pytest.mark.integration
class TestSpecAuthorityCompilerAgentIntegration:
    """
    Integration tests that call the actual LLM agent.

    Run with: pytest -m integration tests/test_spec_authority_compiler_agent.py -v
    Skip with: pytest -m "not integration" tests/test_spec_authority_compiler_agent.py -v
    """

    @pytest.mark.asyncio
    async def test_agent_output_is_normalized_and_deterministic_ids(self) -> None:
        """
        Integration test: agent output is normalized to deterministic IDs/prompt_hash.

        Assertions limited to deterministic contracts (after normalization):
        - prompt_hash == compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
        - invariant.id == compute_invariant_id_from_payload(invariant.type)
        - source_map ties invariant_id to excerpt
        """
        from google.adk.runners import Runner  # noqa: PLC0415
        from google.adk.sessions import InMemorySessionService  # noqa: PLC0415

        from orchestrator_agent.agent_tools.spec_authority_compiler_agent.agent import (  # noqa: PLC0415
            root_agent,
        )
        from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: PLC0415
            SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
        )
        from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: PLC0415
            normalize_compiler_output,
        )

        input_sentence = "The payload must include user_id."
        input_payload = SpecAuthorityCompilerInput(
            spec_source=input_sentence,
            spec_content_ref=None,
            domain_hint=None,
            product_id=None,
            spec_version_id=None,
        )

        session_service = InMemorySessionService()
        runner = Runner(
            agent=root_agent,
            app_name="test_compiler",
            session_service=session_service,
        )

        session = await session_service.create_session(
            app_name="test_compiler",
            user_id="test_user",
        )

        # Run agent with JSON input
        events = []
        new_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=input_payload.model_dump_json())],
        )

        async for event in runner.run_async(
            user_id="test_user",
            session_id=session.id,
            new_message=new_message,
        ):
            events.append(event)  # noqa: PERF401

        # Extract final response text
        final_event = events[-1] if events else None
        assert final_event is not None, "Agent returned no events"

        response_text = None
        if hasattr(final_event, "content") and final_event.content:
            for part in final_event.content.parts:
                if hasattr(part, "text") and part.text:
                    response_text = part.text
                    break

        assert response_text is not None, "Agent returned no text response"

        normalized = normalize_compiler_output(response_text)
        assert isinstance(normalized.root, SpecAuthorityCompilationSuccess), (
            f"Expected success, got failure: {normalized.root}"
        )

        expected_prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
        assert normalized.root.prompt_hash == expected_prompt_hash

        # Validate invariant matches deterministic classification
        expected_invariant = classify_invariant_from_text(input_sentence)
        assert expected_invariant is not None

        assert len(normalized.root.invariants) == 1
        inv = normalized.root.invariants[0]
        assert inv.type == InvariantType.REQUIRED_FIELD
        assert isinstance(inv.parameters, RequiredFieldParams)
        assert inv.parameters.field_name == "user_id"

        assert len(normalized.root.source_map) >= 1
        # Find the source_map entry that corresponds to this invariant
        sm_entry = next(
            (e for e in normalized.root.source_map if e.invariant_id == inv.id), None
        )
        assert sm_entry is not None, "No source_map entry found for invariant"
        assert sm_entry.excerpt

        expected_id = compute_invariant_id_from_payload(inv.type, inv.parameters)
        assert inv.id == expected_id
        assert re.match(r"^INV-[0-9a-f]{16}$", inv.id)
