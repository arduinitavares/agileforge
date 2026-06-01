"""Prompt contract checks for the Backlog Primer agent."""

from pathlib import Path

INSTRUCTIONS_PATH = Path(
    "orchestrator_agent/agent_tools/backlog_primer/instructions.txt"
)


def _instructions() -> str:
    return INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def test_backlog_prompt_defines_model_owned_brownfield_output_fields() -> None:
    """Prompt must name only model-owned brownfield helper fields."""
    text = _instructions()

    assert '"authority_ref"' in text
    assert '"capability_hint"' in text
    assert '"capability_name"' not in text
    assert '"as_built_status"' not in text
    assert '"recommended_backlog_treatment"' not in text


def test_backlog_prompt_declares_host_derived_brownfield_annotations() -> None:
    """Prompt must reserve brownfield annotations for host derivation."""
    text = _instructions()

    assert "host derives" in text
    assert "as_built_annotation" in text
    assert "brownfield_warnings" in text
    assert "model must omit" in text


def test_backlog_prompt_prohibits_copying_host_owned_fields() -> None:
    """Prompt must not tell the model to copy host-owned brownfield fields."""
    text = _instructions()

    assert "copy capability_assessments[]" not in text
    assert "copy host fields" not in text
    assert "copy host-owned fields" not in text


def test_backlog_prompt_keeps_as_built_scoping_guidance() -> None:
    """Prompt must use As-Built statuses for scoping without copying fields."""
    text = _instructions()

    assert "observed -> verify, document, monitor, or preserve" in text
    assert (
        "observed_with_missing_evidence -> validate, harden, formalize, "
        "or add evidence"
    ) in text
    assert "not_observed -> build, add, implement, create" in text
    assert "discovery" in text


def test_backlog_prompt_uses_title_guidance_not_validator_prefixes() -> None:
    """Prompt should guide brownfield wording without requiring copied treatment."""
    text = _instructions()

    assert "Use As-Built context to scope work" in text
    assert "Do not copy host-derived As-Built fields into the JSON output" in text
