"""Tests for deterministic Spec Authority IR parsing and candidates."""

from __future__ import annotations

from utils.spec_authority_ir import (
    SourceUnitDisposition,
    extract_requirement_candidates,
    parse_markdown_sections,
    source_units_from_sections,
)


def test_parse_markdown_sections_preserves_blocks_and_diagnostics() -> None:
    """Markdown parsing keeps existing section/block behavior available."""
    sections, diagnostics = parse_markdown_sections(
        "# Overview\n\n"
        "The system must emit one JSON envelope.\n\n"
        "## Examples\n\n"
        "```text\n"
        "unterminated\n"
    )

    assert [section.section_id for section in sections] == ["S1", "S2"]
    assert sections[0].heading == "Overview"
    assert sections[0].blocks[0].text == "The system must emit one JSON envelope."
    assert sections[0].blocks[0].requirement_bearing is True
    assert sections[1].heading == "Examples"
    assert diagnostics == [
        {
            "section_id": "S2",
            "code": "MARKDOWN_FENCE_UNCLOSED",
            "message": "Fenced code block was not closed before end of file.",
        }
    ]


def test_atomic_candidates_split_multi_clause_unit() -> None:
    """One source unit with multiple normative clauses yields atomic candidates."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The CLI must emit JSON; it must include guard tokens.\n"
    )
    units = source_units_from_sections(sections)

    candidates = extract_requirement_candidates(units)

    assert [candidate.statement for candidate in candidates] == [
        "The CLI must emit JSON",
        "it must include guard tokens.",
    ]
    assert {candidate.source_unit_id for candidate in candidates} == {
        units[0].unit_id
    }
    assert all(candidate.classification == "requirement" for candidate in candidates)
    assert units[0].disposition == SourceUnitDisposition.CANDIDATE_EXTRACTED


def test_uncertain_units_block_silent_false_negatives() -> None:
    """Ordinary product prose is uncertain instead of silently ignored."""
    sections, _diagnostics = parse_markdown_sections(
        "# Features\n\n"
        "Users can export audit logs for selected projects.\n"
    )
    units = source_units_from_sections(sections)

    candidates = extract_requirement_candidates(units)

    assert len(candidates) == 1
    assert candidates[0].classification == "uncertain"
    assert (
        candidates[0].statement
        == "Users can export audit logs for selected projects."
    )
    assert units[0].disposition == SourceUnitDisposition.UNCERTAIN


def test_positive_non_requirement_requires_exact_rule() -> None:
    """Background/example prose is non-requirement only with positive evidence."""
    sections, _diagnostics = parse_markdown_sections(
        "# Background\n\n"
        "Example: Existing operators use spreadsheets today.\n"
    )
    units = source_units_from_sections(sections)

    candidates = extract_requirement_candidates(units)

    assert candidates == []
    assert units[0].disposition == SourceUnitDisposition.NON_REQUIREMENT
    assert units[0].disposition_reason in {
        "non_requirement_heading:background",
        "non_requirement_marker:Example:",
    }


def test_positive_non_requirement_blocks_product_capability_text() -> None:
    """Background headings do not hide product capability requirements."""
    sections, _diagnostics = parse_markdown_sections(
        "# Background\n\n"
        "Users can export audit logs for selected projects.\n"
    )
    units = source_units_from_sections(sections)

    candidates = extract_requirement_candidates(units)

    assert len(candidates) == 1
    assert candidates[0].classification == "uncertain"
    assert units[0].disposition == SourceUnitDisposition.UNCERTAIN
