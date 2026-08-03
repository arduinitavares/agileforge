"""Tests for spec validator agent schema and instruction guardrails."""

from pathlib import Path

from orchestrator_agent.agent_tools.spec_validator_agent.tools import (
    _DEFAULT_MAX_TOKENS,
)
from services.contracts.specification_validation import (
    SpecValidationResult,
)


def test_instructions_txt_does_not_contain_force_refinement() -> None:
    """Verify instructions txt does not contain force refinement."""
    instructions_path = (
        Path("orchestrator_agent")
        / "agent_tools"
        / "spec_validator_agent"
        / "instructions.txt"
    )
    text = instructions_path.read_text(encoding="utf-8")
    assert "Force at least ONE refinement pass" not in text


def test_spec_validation_result_schema_roundtrip() -> None:
    """Verify spec validation result schema roundtrip."""
    payload = SpecValidationResult(
        is_compliant=True,
        issues=[],
        suggestions=[],
        domain_compliance=None,
        verdict="Compliant for scoped invariants.",
    )

    serialized = payload.model_dump_json()
    parsed = SpecValidationResult.model_validate_json(serialized)

    assert parsed.is_compliant is True
    assert parsed.issues == []
    assert parsed.suggestions == []
    assert parsed.domain_compliance is None
    assert parsed.verdict == "Compliant for scoped invariants."


def test_max_tokens_default_is_at_least_4096() -> None:
    """Verify max tokens default is at least 4096."""
    assert _DEFAULT_MAX_TOKENS >= 4096  # noqa: PLR2004
