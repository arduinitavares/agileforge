"""Tests for deterministic Spec Authority IR parsing and candidates."""

from __future__ import annotations

from utils.spec_authority_ir import (
    AuthorityTargetKind,
    CoverageStatus,
    MappingProvenance,
    SourceUnitDisposition,
    build_authority_mappings,
    coverage_summary_from_findings,
    derive_review_findings,
    extract_requirement_candidates,
    parse_markdown_sections,
    source_units_from_sections,
)

MAX_TEST_EXCERPT_BYTES = 2_000


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


def test_source_unit_and_candidate_excerpts_are_bounded_by_utf8_bytes() -> None:
    """Non-ASCII source excerpts are bounded by UTF-8 bytes, not characters."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        f"{'Á' * 1_500} must be preserved.\n"
    )
    units = source_units_from_sections(sections)
    candidates = extract_requirement_candidates(units)

    assert len(units[0].text_excerpt.encode("utf-8")) <= MAX_TEST_EXCERPT_BYTES
    assert len(candidates[0].source_quote.encode("utf-8")) <= MAX_TEST_EXCERPT_BYTES


def test_candidate_level_coverage_blocks_partial_units() -> None:
    """Coverage is computed per candidate, not per source unit."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The CLI must emit JSON; it must include guard tokens.\n"
    )
    units = source_units_from_sections(sections)
    candidates = extract_requirement_candidates(units)

    mappings = build_authority_mappings(
        candidates,
        [{"id": "INV-json", "kind": "invariant"}],
        [
            {
                "candidate_id": candidates[0].candidate_id,
                "authority_item_id": "INV-json",
                "source_quote_hash": candidates[0].quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )
    findings = derive_review_findings(units, candidates, mappings, "host_parsed")
    summary = coverage_summary_from_findings(findings, mappings)

    assert mappings[0].mapping_status == CoverageStatus.COVERED
    assert summary["covered_candidate_count"] == 1
    assert summary["uncovered_candidate_count"] == 1
    assert summary["all_candidates_covered"] is False
    assert any(
        finding.code == "AUTHORITY_CANDIDATE_UNCOVERED"
        and finding.candidate_ids == [candidates[1].candidate_id]
        for finding in findings
    )


def test_findings_are_recomputed_from_coverage() -> None:
    """Model-authored empty gaps cannot suppress derived incomplete coverage."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The service must persist audit events.\n"
    )
    units = source_units_from_sections(sections)
    candidates = extract_requirement_candidates(units)

    mappings = build_authority_mappings(candidates, [], [])
    findings = derive_review_findings(
        units,
        candidates,
        mappings,
        {"ir": "model_emitted", "gaps": []},
    )

    assert any(
        finding.code == "AUTHORITY_COVERAGE_INCOMPLETE" for finding in findings
    )
    blocking_count = coverage_summary_from_findings(findings, mappings)[
        "blocking_finding_count"
    ]
    assert isinstance(blocking_count, int)
    assert blocking_count >= 1


def test_exact_quote_and_invariant_mapping_covers_candidate() -> None:
    """Exact quote evidence plus a compatible invariant covers a candidate."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "INV-problem-details", "kind": AuthorityTargetKind.INVARIANT}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "INV-problem-details",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": MappingProvenance.MODEL_QUOTE,
            }
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")

    assert mappings[0].mapping_status == CoverageStatus.COVERED
    assert coverage_summary_from_findings(findings, mappings)[
        "all_candidates_covered"
    ] is True


def test_substring_or_broad_section_mapping_is_weak() -> None:
    """Substring or section-only evidence yields weak mapping."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "INV-problem-details", "kind": "invariant"}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "INV-problem-details",
                "source_quote": "problem details",
                "source_ref": "Requirements",
                "mapping_provenance": "model_quote",
            }
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")

    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING
    assert any(
        finding.code == "AUTHORITY_CANDIDATE_WEAK_MAPPING" for finding in findings
    )


def test_host_repaired_quote_mapping_is_weak_without_override() -> None:
    """Host-repaired exact quotes are source evidence, not accepted coverage."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "INV-problem-details", "kind": "invariant"}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "INV-problem-details",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "host_repaired_quote",
            }
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")

    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING
    assert any(
        finding.code == "AUTHORITY_CANDIDATE_WEAK_MAPPING" for finding in findings
    )


def test_missing_authority_target_cannot_cover_candidate() -> None:
    """Exact quote evidence cannot cover a missing authority target."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "INV-missing",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")

    assert mappings[0].authority_target_kind == AuthorityTargetKind.UNKNOWN
    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING
    assert coverage_summary_from_findings(findings, mappings)[
        "all_candidates_covered"
    ] is False


def test_malformed_authority_target_kind_cannot_be_overridden_by_entry() -> None:
    """Source-map entries cannot launder malformed authority item kinds."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "INV-bad", "kind": "nonsense"}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "INV-bad",
                "authority_target_kind": "invariant",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )

    assert mappings[0].authority_target_kind == AuthorityTargetKind.UNKNOWN
    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING


def test_weak_mapping_blocks_even_when_candidate_has_covered_mapping() -> None:
    """Mixed covered and weak mappings remain review-incomplete."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [
            {"id": "INV-problem-details", "kind": "invariant"},
            {"id": "GAP-validation", "kind": "gap"},
        ],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "INV-problem-details",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            },
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "GAP-validation",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            },
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")
    summary = coverage_summary_from_findings(findings, mappings)

    assert [mapping.mapping_status for mapping in mappings] == [
        CoverageStatus.COVERED,
        CoverageStatus.WEAK_MAPPING,
    ]
    assert any(
        finding.code == "AUTHORITY_CANDIDATE_WEAK_MAPPING" for finding in findings
    )
    assert summary["all_candidates_covered"] is False


def test_exact_quote_with_incompatible_target_kind_blocks() -> None:
    """Exact quote hashes do not cover incompatible authority target kinds."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "GAP-validation", "kind": "gap", "text": "No invariant exists."}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "GAP-validation",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")

    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING
    assert any(
        finding.code == "AUTHORITY_CANDIDATE_WEAK_MAPPING" for finding in findings
    )


def test_quality_attribute_mapped_only_to_assumption_blocks() -> None:
    """Assumptions can explain but cannot cover quality attributes."""
    sections, _diagnostics = parse_markdown_sections(
        "# Quality Attributes\n\n"
        "Responses should complete within 200ms.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"kind": "assumption", "text": "Latency budget is not confirmed."}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_target_kind": "assumption",
                "target_text": "Latency budget is not confirmed.",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")

    assert candidate.classification == "quality_attribute"
    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING
    assert any(finding.severity == "blocking" for finding in findings)


def test_dependency_mapped_only_to_assumption_blocks() -> None:
    """Assumptions cannot cover dependency candidates."""
    sections, _diagnostics = parse_markdown_sections(
        "# Dependencies\n\n"
        "The service must sync with the billing API.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"kind": "assumption", "text": "Billing API access is available."}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_target_kind": "assumption",
                "target_text": "Billing API access is available.",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )

    assert candidate.classification == "dependency"
    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING


def test_acceptance_criterion_mapped_only_to_gap_blocks() -> None:
    """Gaps cannot cover acceptance-criterion candidates."""
    sections, _diagnostics = parse_markdown_sections(
        "# Acceptance Criteria\n\n"
        "Accepted when audit exports include request id.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"kind": "gap", "text": "Export evidence not represented."}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_target_kind": "gap",
                "target_text": "Export evidence not represented.",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )

    assert candidate.classification == "acceptance_criterion"
    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING


def test_normal_constraint_mapped_to_rejected_feature_blocks() -> None:
    """Rejected features only cover forbidden or safety constraints."""
    sections, _diagnostics = parse_markdown_sections(
        "# Security Constraints\n\n"
        "The service must audit export requests.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "RF-audit", "kind": "rejected_feature"}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "RF-audit",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )

    assert candidate.classification == "constraint"
    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING


def test_forbidden_constraint_mapped_to_rejected_feature_covers() -> None:
    """Rejected features can cover forbidden or safety constraints."""
    sections, _diagnostics = parse_markdown_sections(
        "# Security Constraints\n\n"
        "The service must never expose access tokens.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "RF-tokens", "kind": "rejected_feature"}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "RF-tokens",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )

    assert candidate.classification == "constraint"
    assert mappings[0].mapping_status == CoverageStatus.COVERED


def test_open_question_mapped_to_gap_is_classified_but_not_covered() -> None:
    """Open questions mapped to gaps are classified and still block coverage."""
    sections, _diagnostics = parse_markdown_sections(
        "# Open Questions\n\n"
        "Should audit exports include deleted users?\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"kind": "gap", "text": "Audit export deletion policy unresolved."}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_target_kind": "gap",
                "target_text": "Audit export deletion policy unresolved.",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")
    summary = coverage_summary_from_findings(findings, mappings)

    assert mappings[0].mapping_status == CoverageStatus.INTENTIONALLY_CLASSIFIED
    assert summary["all_candidates_covered"] is False
    assert any(
        finding.code == "AUTHORITY_CANDIDATE_INTENTIONALLY_CLASSIFIED"
        for finding in findings
    )


def test_unmapped_and_uncertain_candidates_block() -> None:
    """Unmapped uncertain candidates produce blocking findings."""
    sections, _diagnostics = parse_markdown_sections(
        "# Features\n\n"
        "Users can export audit logs for selected projects.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings([candidate], [], [])
    findings = derive_review_findings(units, [candidate], mappings, "host_parsed")

    assert candidate.classification == "uncertain"
    assert any(finding.code == "AUTHORITY_CANDIDATE_UNCERTAIN" for finding in findings)
    assert any(finding.code == "AUTHORITY_CANDIDATE_UNCOVERED" for finding in findings)


def test_goal_to_eligible_feature_rule_is_weak_without_future_scope() -> None:
    """Eligible feature rules are weak for goals without future-scope signal."""
    sections, _diagnostics = parse_markdown_sections(
        "# Goals\n\n"
        "The product should simplify quarterly planning.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "EFR-planning", "kind": "eligible_feature_rule"}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "EFR-planning",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )

    assert candidate.classification == "goal"
    assert mappings[0].mapping_status == CoverageStatus.WEAK_MAPPING


def test_goal_to_eligible_feature_rule_covers_future_scope_constraint() -> None:
    """Eligible feature rules can cover goals that constrain future scope."""
    sections, _diagnostics = parse_markdown_sections(
        "# Goals\n\n"
        "Future phase support should simplify quarterly planning.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]

    mappings = build_authority_mappings(
        [candidate],
        [{"id": "EFR-planning", "kind": "eligible_feature_rule"}],
        [
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": "EFR-planning",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        ],
    )

    assert candidate.classification == "goal"
    assert mappings[0].mapping_status == CoverageStatus.COVERED


def test_generated_gap_and_assumption_ids_are_stable() -> None:
    """Host-generated gap and assumption target IDs are recomputation-stable."""
    sections, _diagnostics = parse_markdown_sections(
        "# Requirements\n\n"
        "The API must return problem details for validation errors.\n"
    )
    units = source_units_from_sections(sections)
    candidate = extract_requirement_candidates(units)[0]
    source_entries = [
        {
            "candidate_id": candidate.candidate_id,
            "authority_target_kind": "gap",
            "target_text": "No canonical invariant exists.",
            "source_quote_hash": candidate.quote_hash,
            "mapping_provenance": "model_quote",
            "finding_code": "AUTHORITY_CANDIDATE_UNCOVERED",
        },
        {
            "candidate_id": candidate.candidate_id,
            "authority_target_kind": "assumption",
            "target_text": "Clients use RFC 7807.",
            "source_quote_hash": candidate.quote_hash,
            "mapping_provenance": "model_quote",
        },
    ]

    first = build_authority_mappings([candidate], [], source_entries)
    second = build_authority_mappings([candidate], [], source_entries)

    assert [mapping.authority_item_id for mapping in first] == [
        mapping.authority_item_id for mapping in second
    ]
    assert first[0].authority_item_id.startswith("GAP-")
    assert first[1].authority_item_id.startswith("ASM-")

    changed_gap_code = build_authority_mappings(
        [candidate],
        [],
        [
            {
                **source_entries[0],
                "finding_code": "AUTHORITY_CANDIDATE_WEAK_MAPPING",
            }
        ],
    )[0]
    changed_assumption_text = build_authority_mappings(
        [candidate],
        [],
        [
            {
                **source_entries[1],
                "target_text": "Clients use a different error contract.",
            }
        ],
    )[0]
    changed_gap_text = build_authority_mappings(
        [candidate],
        [],
        [
            {
                **source_entries[0],
                "target_text": "Another canonical gap exists.",
            }
        ],
    )[0]
    second_candidate = extract_requirement_candidates(
        source_units_from_sections(
            parse_markdown_sections(
                "# Requirements\n\n"
                "The API must return problem details for validation errors.\n\n"
                "The API must return audit metadata for validation errors.\n"
            )[0]
        )
    )[1]
    changed_candidate = build_authority_mappings(
        [second_candidate],
        [],
        [
            {
                **source_entries[0],
                "candidate_id": second_candidate.candidate_id,
                "source_quote_hash": second_candidate.quote_hash,
            }
        ],
    )[0]

    assert changed_gap_code.authority_item_id != first[0].authority_item_id
    assert changed_gap_text.authority_item_id != first[0].authority_item_id
    assert changed_assumption_text.authority_item_id != first[1].authority_item_id
    assert changed_candidate.authority_item_id != first[0].authority_item_id
