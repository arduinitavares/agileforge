"""Prompt contract checks for the Backlog Primer agent."""

from pathlib import Path

INSTRUCTIONS_PATH = Path(
    "orchestrator_agent/agent_tools/backlog_primer/instructions.txt"
)


def _instructions() -> str:
    return INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def test_backlog_prompt_defines_brownfield_output_fields() -> None:
    """Prompt must name every brownfield metadata output field."""
    text = _instructions()

    assert '"capability_name"' in text
    assert '"authority_ref"' in text
    assert '"as_built_status"' in text
    assert '"recommended_backlog_treatment"' in text


def test_backlog_prompt_separates_work_item_title_from_capability() -> None:
    """Prompt must distinguish requirement title from capability identity."""
    text = _instructions()

    assert "requirement is the action-oriented work item title" in text
    assert "capability_name is the product capability" in text


def test_backlog_prompt_requires_as_built_metadata_when_capability_maps() -> None:
    """Prompt must tell model to emit metadata for mapped As-Built items."""
    text = _instructions()

    assert "When a backlog item maps to an As-Built capability" in text
    assert "If requirement or technical_note uses As-Built capability terms" in text
    assert (
        "must include capability_name, authority_ref, as_built_status, "
        "and recommended_backlog_treatment"
    ) in text


def test_backlog_prompt_names_treatment_tokens_and_nullable_metadata() -> None:
    """Prompt must handle strict treatment tokens and nullable metadata."""
    text = _instructions()

    assert (
        "copy capability_assessments[].recommended_backlog_treatment unchanged"
    ) in text
    assert "skip_new_implementation" in text
    assert "create_verification_item" in text
    assert "create_hardening_item" in text
    assert "create_authority_conflict_item" in text
    assert "create_discovery_item" in text
    assert "create_product_item" in text
    assert "po_review_required" in text
    assert "required fields must be filled" in text
    assert (
        "optional brownfield metadata fields must be present as null when unmapped"
    ) in text


def test_backlog_prompt_requires_exact_brownfield_title_prefixes() -> None:
    """Prompt must describe status title guidance as validator-enforced prefixes."""
    text = _instructions()

    assert "must start with one of the exact prefixes" in text
    assert "recommended treatment" in text
    assert "Do not use noun-only capability titles" in text
    assert "create_discovery_item -> Discover, Investigate, Clarify, Define" in text
