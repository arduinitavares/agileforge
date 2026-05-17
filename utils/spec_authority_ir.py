# utils/spec_authority_ir.py
"""Deterministic parser and candidate extraction for Spec Authority IR."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, cast

JsonDict = dict[str, object]

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NORMATIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"must|required|shall|should|only|never|cannot|forbidden|accepted when|"
    r"rejected when|input|output|schema|field|constraint|deve|obrigatorio|"
    r"obrigat" "\u00f3" r"rio|proibido|nao deve|n" "\u00e3" r"o deve"
    r")\b",
    re.IGNORECASE,
)
_REQUIREMENT_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"requirements|invariants|rules|acceptance|security|scope|out of scope|"
    r"schema|contract"
    r")\b",
    re.IGNORECASE,
)
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^(`{3,}|~{3,})")
_CAPABILITY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"can|allow|allows|support|supports|create|export|import|submit|approve|"
    r"reject|configure|select|generate|validate"
    r")\b",
    re.IGNORECASE,
)
_ACCEPTANCE_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(acceptance|accepted when|rejected when|given|when|then)\b",
    re.IGNORECASE,
)
_CONSTRAINT_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(security|compliance|constraint|privacy|audit|permission|role)\b",
    re.IGNORECASE,
)
_QUALITY_ATTRIBUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"quality|performance|latency|availability|reliability|scalability|"
    r"within\s+\d+\s*(?:ms|milliseconds|s|seconds)|complete within|respond within"
    r")\b",
    re.IGNORECASE,
)
_GOAL_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(goals?|objectives?|outcomes?)\b",
    re.IGNORECASE,
)
_ASSUMPTION_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(assumptions?)\b",
    re.IGNORECASE,
)
_DEPENDENCY_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(dependencies|dependency)\b",
    re.IGNORECASE,
)
_FUTURE_SCOPE_CONSTRAINT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"future scope|future phase|later phase|deferred|not in scope|out of scope|"
    r"next phase|post[- ]?mvp"
    r")\b",
    re.IGNORECASE,
)
_NON_REQUIREMENT_HEADINGS: Final[frozenset[str]] = frozenset(
    {
        "background",
        "context",
        "glossary",
        "terminology",
        "example",
        "examples",
        "rationale",
        "notes",
        "changelog",
        "revision history",
        "implementation notes",
    }
)
_NON_REQUIREMENT_PREFIXES: Final[tuple[str, ...]] = (
    "Note:",
    "Example:",
    "Rationale:",
)
_OPEN_QUESTION_HEADINGS: Final[frozenset[str]] = frozenset(
    {"open question", "open questions", "questions"}
)


class IrProvenance(StrEnum):
    """Authority IR provenance."""

    MODEL_EMITTED = "model_emitted"
    HOST_PARSED = "host_parsed"
    MIXED = "mixed"
    LEGACY_ABSENT = "legacy_absent"


class SourceUnitDisposition(StrEnum):
    """Disposition assigned to each parsed source unit."""

    CANDIDATE_EXTRACTED = "candidate_extracted"
    INTENTIONALLY_CLASSIFIED = "intentionally_classified"
    NON_REQUIREMENT = "non_requirement"
    UNCERTAIN = "uncertain"


class CoverageStatus(StrEnum):
    """Candidate-level authority coverage status."""

    COVERED = "covered"
    PARTIAL = "partial"
    INTENTIONALLY_CLASSIFIED = "intentionally_classified"
    UNCOVERED = "uncovered"
    UNCERTAIN = "uncertain"
    WEAK_MAPPING = "weak_mapping"


class MappingProvenance(StrEnum):
    """Provenance for candidate-to-authority mappings."""

    MODEL_QUOTE = "model_quote"
    HOST_REPAIRED_QUOTE = "host_repaired_quote"
    HOST_INFERRED = "host_inferred"
    LEGACY_ABSENT = "legacy_absent"


class AuthorityTargetKind(StrEnum):
    """Authority item kinds that mappings may target."""

    INVARIANT = "invariant"
    ELIGIBLE_FEATURE_RULE = "eligible_feature_rule"
    REJECTED_FEATURE = "rejected_feature"
    GAP = "gap"
    ASSUMPTION = "assumption"
    UNKNOWN = "unknown"


_ACCEPTABLE_MAPPING_PROVENANCE: Final[frozenset[MappingProvenance]] = frozenset(
    {MappingProvenance.MODEL_QUOTE}
)


class OverrideScope(StrEnum):
    """Incomplete review override scopes."""

    CANDIDATE = "candidate"


@dataclass(frozen=True)
class ContentBlock:
    """A parsed Markdown content block within a section."""

    text: str
    line_start: int
    line_end: int
    requirement_bearing: bool
    kind: str = "paragraph"


@dataclass
class Section:
    """A Markdown section with parsed content blocks."""

    section_id: str
    heading: str | None
    line_start: int
    line_end: int
    blocks: list[ContentBlock] = field(default_factory=list)


@dataclass
class SourceUnit:
    """A parsed source unit used as a candidate extraction container."""

    unit_id: str
    section_id: str
    heading_path: tuple[str, ...]
    kind: str
    line_start: int
    line_end: int
    text_hash: str
    text_excerpt: str
    requirement_bearing: bool
    disposition: SourceUnitDisposition = SourceUnitDisposition.UNCERTAIN
    disposition_reason: str | None = None


@dataclass(frozen=True)
class RequirementCandidate:
    """An atomic candidate extracted from a source unit."""

    candidate_id: str
    source_unit_id: str
    statement: str
    source_quote: str
    quote_hash: str
    line_start: int
    line_end: int
    classification: str
    provenance: IrProvenance = IrProvenance.HOST_PARSED


@dataclass(frozen=True)
class AuthorityMapping:
    """A validated mapping from a requirement candidate to an authority target."""

    candidate_id: str
    authority_item_id: str
    authority_target_kind: AuthorityTargetKind
    mapping_status: CoverageStatus
    mapping_rationale: str
    source_quote_hash: str | None
    mapping_provenance: MappingProvenance


@dataclass(frozen=True)
class AuthorityReviewFinding:
    """Host-derived authority review finding."""

    finding_id: str
    severity: str
    code: str
    message: str
    candidate_ids: list[str]
    source_unit_ids: list[str]
    override_allowed: bool


@dataclass(frozen=True)
class IncompleteReviewOverride:
    """Candidate-scoped override for an incomplete authority review finding."""

    candidate_id: str
    finding_code: str
    rationale: str
    scope: OverrideScope = OverrideScope.CANDIDATE


@dataclass(frozen=True)
class _FenceMarker:
    """Markdown fenced-code marker family and length."""

    character: str
    length: int

    def closes(self, opener: _FenceMarker) -> bool:
        """Return whether this marker closes the given opener."""
        return self.character == opener.character and self.length >= opener.length


def parse_markdown_sections(text: str) -> tuple[list[Section], list[JsonDict]]:
    """Parse Markdown into sections and blocks using authority review semantics."""
    lines = text.splitlines()
    sections: list[Section] = []
    current = Section("ROOT", None, 1, max(len(lines), 1))
    content_before_heading = False
    section_number = 0
    fence: _FenceMarker | None = None
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        marker = _fence_marker(stripped)
        if marker is not None and (fence is None or marker.closes(fence)):
            fence = None if fence is not None else marker
            content_before_heading = True
            continue
        if fence is not None:
            if stripped:
                content_before_heading = True
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            if stripped:
                content_before_heading = True
            continue
        if current.section_id != "ROOT" or content_before_heading:
            current.line_end = index - 1
            sections.append(current)
        section_number += 1
        current = Section(
            section_id=f"S{section_number}",
            heading=match.group(2).strip(),
            line_start=index,
            line_end=len(lines),
        )
        content_before_heading = False
    if current.section_id != "ROOT" or content_before_heading or not sections:
        current.line_end = len(lines) if lines else 1
        sections.append(current)

    diagnostics: list[JsonDict] = []
    for section in sections:
        blocks, section_diagnostics = _parse_section_blocks(lines, section)
        section.blocks = blocks
        diagnostics.extend(section_diagnostics)
    diagnostics.sort(
        key=lambda item: (item["section_id"], item["code"], item["message"])
    )
    return sections, diagnostics


def source_units_from_sections(sections: list[Section]) -> list[SourceUnit]:
    """Build stable source units from parsed Markdown sections."""
    units: list[SourceUnit] = []
    occurrence_counts: dict[tuple[str, str], int] = {}
    for section in sections:
        heading_path = (section.heading,) if section.heading else ()
        section_key = _slug_hash("/".join(heading_path) or "root")
        for block in section.blocks:
            normalized = _normalize_text(block.text)
            text_hash = _sha256_prefixed(block.text.encode("utf-8"))
            short_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
            occurrence_key = (section.section_id, short_hash)
            occurrence = occurrence_counts.get(occurrence_key, 0) + 1
            occurrence_counts[occurrence_key] = occurrence
            units.append(
                SourceUnit(
                    unit_id=f"SRC-{section_key}-{short_hash}-{occurrence}",
                    section_id=section.section_id,
                    heading_path=heading_path,
                    kind=block.kind,
                    line_start=block.line_start,
                    line_end=block.line_end,
                    text_hash=text_hash,
                    text_excerpt=_bounded(block.text),
                    requirement_bearing=block.requirement_bearing,
                )
            )
    return units


def extract_requirement_candidates(
    source_units: list[SourceUnit],
) -> list[RequirementCandidate]:
    """Extract deterministic atomic requirement candidates from source units."""
    candidates: list[RequirementCandidate] = []
    for unit in source_units:
        reason = _positive_non_requirement_reason(unit)
        if reason is not None:
            unit.disposition = SourceUnitDisposition.NON_REQUIREMENT
            unit.disposition_reason = reason
            continue

        classification = _candidate_classification(unit)
        clauses = _candidate_clauses(unit)
        if not clauses:
            clauses = [_normalize_text(unit.text_excerpt)]
            classification = "uncertain"

        unit.disposition = (
            SourceUnitDisposition.CANDIDATE_EXTRACTED
            if classification != "uncertain"
            else SourceUnitDisposition.UNCERTAIN
        )
        unit.disposition_reason = None
        for index, clause in enumerate(clauses, start=1):
            quote_hash = _sha256_prefixed(clause.encode("utf-8"))
            candidates.append(
                RequirementCandidate(
                    candidate_id=_candidate_id(unit.unit_id, quote_hash, index),
                    source_unit_id=unit.unit_id,
                    statement=clause,
                    source_quote=_bounded(clause),
                    quote_hash=quote_hash,
                    line_start=unit.line_start,
                    line_end=unit.line_end,
                    classification=classification,
                )
            )
    return candidates


def build_authority_mappings(
    candidates: list[RequirementCandidate],
    authority_items: Sequence[object],
    source_map_entries: Sequence[object],
) -> list[AuthorityMapping]:
    """Build candidate-level mappings with conservative coverage status."""
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    authority_by_id = {
        str(item_id): item
        for item in authority_items
        if (item_id := _field_value(item, "id", "authority_item_id")) is not None
    }
    mappings: list[AuthorityMapping] = []
    for entry in source_map_entries:
        candidate_id = _string_field(entry, "candidate_id")
        if candidate_id is None or candidate_id not in candidates_by_id:
            continue
        candidate = candidates_by_id[candidate_id]
        item = _authority_item_for_entry(entry, authority_by_id)
        target_kind = _target_kind(entry, item)
        target_text = _target_text(entry, item)
        authority_item_id = _authority_item_id(
            entry,
            item,
            target_kind,
            target_text,
        )
        source_quote_hash = _source_quote_hash(entry, candidate)
        mapping_provenance = _mapping_provenance(entry)
        status, rationale = _mapping_status(
            candidate,
            target_kind,
            source_quote_hash,
            mapping_provenance,
            _target_resolved(item, target_kind, target_text),
        )
        mappings.append(
            AuthorityMapping(
                candidate_id=candidate_id,
                authority_item_id=authority_item_id,
                authority_target_kind=target_kind,
                mapping_status=status,
                mapping_rationale=rationale,
                source_quote_hash=source_quote_hash,
                mapping_provenance=mapping_provenance,
            )
        )
    return mappings


def derive_review_findings(  # noqa: C901
    source_units: list[SourceUnit],
    candidates: list[RequirementCandidate],
    mappings: list[AuthorityMapping],
    provenance: object,
) -> list[AuthorityReviewFinding]:
    """Derive host-owned blocking review findings from candidate coverage."""
    del provenance
    findings: list[AuthorityReviewFinding] = []
    mappings_by_candidate: dict[str, list[AuthorityMapping]] = {}
    for mapping in mappings:
        mappings_by_candidate.setdefault(mapping.candidate_id, []).append(mapping)

    incomplete_candidate_ids: set[str] = set()
    incomplete_source_unit_ids: set[str] = set()
    for candidate in candidates:
        candidate_mappings = mappings_by_candidate.get(candidate.candidate_id, [])
        source_unit_id = candidate.source_unit_id
        if candidate.classification == "uncertain":
            findings.append(
                _finding(
                    "AUTHORITY_CANDIDATE_UNCERTAIN",
                    "Candidate classification is uncertain.",
                    [candidate.candidate_id],
                    [source_unit_id],
                )
            )
            incomplete_candidate_ids.add(candidate.candidate_id)
            incomplete_source_unit_ids.add(source_unit_id)
        if not candidate_mappings:
            findings.append(
                _finding(
                    "AUTHORITY_CANDIDATE_UNCOVERED",
                    "Candidate has no authority mapping.",
                    [candidate.candidate_id],
                    [source_unit_id],
                )
            )
            incomplete_candidate_ids.add(candidate.candidate_id)
            incomplete_source_unit_ids.add(source_unit_id)
            continue
        if any(
            mapping.mapping_status == CoverageStatus.INTENTIONALLY_CLASSIFIED
            for mapping in candidate_mappings
        ):
            findings.append(
                _finding(
                    "AUTHORITY_CANDIDATE_INTENTIONALLY_CLASSIFIED",
                    "Candidate is classified to a gap or assumption and "
                    "remains not covered.",
                    [candidate.candidate_id],
                    [source_unit_id],
                )
            )
            incomplete_candidate_ids.add(candidate.candidate_id)
            incomplete_source_unit_ids.add(source_unit_id)
        if any(
            mapping.mapping_status == CoverageStatus.WEAK_MAPPING
            for mapping in candidate_mappings
        ):
            findings.append(
                _finding(
                    "AUTHORITY_CANDIDATE_WEAK_MAPPING",
                    "Candidate mapping lacks acceptable source evidence or "
                    "compatible authority kind.",
                    [candidate.candidate_id],
                    [source_unit_id],
                )
            )
            incomplete_candidate_ids.add(candidate.candidate_id)
            incomplete_source_unit_ids.add(source_unit_id)
        if any(
            mapping.mapping_status == CoverageStatus.PARTIAL
            for mapping in candidate_mappings
        ):
            findings.append(
                _finding(
                    "AUTHORITY_CANDIDATE_PARTIAL",
                    "Candidate has only partial authority coverage.",
                    [candidate.candidate_id],
                    [source_unit_id],
                )
            )
            incomplete_candidate_ids.add(candidate.candidate_id)
            incomplete_source_unit_ids.add(source_unit_id)
        if not _candidate_has_covered_mapping(candidate_mappings):
            incomplete_candidate_ids.add(candidate.candidate_id)
            incomplete_source_unit_ids.add(source_unit_id)

    for unit in source_units:
        candidate_extracted_incomplete = (
            unit.disposition == SourceUnitDisposition.CANDIDATE_EXTRACTED
            and not any(
                candidate.source_unit_id == unit.unit_id
                and _candidate_has_covered_mapping(
                    mappings_by_candidate.get(candidate.candidate_id, [])
                )
                for candidate in candidates
            )
        )
        if (
            unit.disposition == SourceUnitDisposition.UNCERTAIN
            or candidate_extracted_incomplete
        ):
            incomplete_source_unit_ids.add(unit.unit_id)

    if incomplete_candidate_ids or incomplete_source_unit_ids:
        findings.append(
            _finding(
                "AUTHORITY_COVERAGE_INCOMPLETE",
                "Authority coverage is incomplete for one or more candidates.",
                sorted(incomplete_candidate_ids),
                sorted(incomplete_source_unit_ids),
                override_allowed=False,
            )
        )
    findings.sort(key=lambda finding: finding.finding_id)
    return findings


def coverage_summary_from_findings(
    findings: list[AuthorityReviewFinding],
    mappings: list[AuthorityMapping],
) -> JsonDict:
    """Summarize candidate coverage from host-derived findings and mappings."""
    mapped_candidate_ids = {mapping.candidate_id for mapping in mappings}
    covered_candidate_ids = {
        mapping.candidate_id
        for mapping in mappings
        if mapping.mapping_status == CoverageStatus.COVERED
    }
    intentionally_classified_ids = {
        mapping.candidate_id
        for mapping in mappings
        if mapping.mapping_status == CoverageStatus.INTENTIONALLY_CLASSIFIED
    }
    weak_candidate_ids = {
        mapping.candidate_id
        for mapping in mappings
        if mapping.mapping_status == CoverageStatus.WEAK_MAPPING
    }
    finding_candidate_ids = {
        candidate_id
        for finding in findings
        for candidate_id in finding.candidate_ids
        if candidate_id
    }
    uncovered_candidate_ids = finding_candidate_ids - mapped_candidate_ids
    blocking_finding_count = sum(
        1 for finding in findings if finding.severity == "blocking"
    )
    incomplete = any(
        finding.code == "AUTHORITY_COVERAGE_INCOMPLETE" for finding in findings
    )
    return {
        "candidate_count": len(mapped_candidate_ids | finding_candidate_ids),
        "covered_candidate_count": len(covered_candidate_ids),
        "weak_mapping_candidate_count": len(weak_candidate_ids),
        "intentionally_classified_candidate_count": len(
            intentionally_classified_ids
        ),
        "uncovered_candidate_count": len(uncovered_candidate_ids),
        "blocking_finding_count": blocking_finding_count,
        "all_candidates_covered": not incomplete and blocking_finding_count == 0,
    }

def _parse_section_blocks(  # noqa: C901, PLR0912, PLR0915
    lines: list[str],
    section: Section,
) -> tuple[list[ContentBlock], list[JsonDict]]:
    blocks: list[ContentBlock] = []
    diagnostics: list[JsonDict] = []
    paragraph: list[tuple[int, str]] = []
    fence_lines: list[tuple[int, str]] = []
    fence: _FenceMarker | None = None
    fence_start = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return
        line_start = paragraph[0][0]
        line_end = paragraph[-1][0]
        block_text = "\n".join(line for _line_no, line in paragraph).strip()
        blocks.append(
            _content_block(block_text, line_start, line_end, section.heading)
        )
        paragraph.clear()

    for line_number in range(section.line_start, section.line_end + 1):
        line = lines[line_number - 1] if 0 <= line_number - 1 < len(lines) else ""
        if line_number == section.line_start and _HEADING_RE.match(line):
            continue
        stripped = line.strip()
        marker = _fence_marker(stripped)
        if marker is not None and (fence is None or marker.closes(fence)):
            flush_paragraph()
            if fence is not None:
                if fence_lines:
                    block_text = "\n".join(line for _line_no, line in fence_lines)
                    blocks.append(
                        _content_block(
                            block_text,
                            fence_start,
                            line_number,
                            section.heading,
                            kind="fenced_block",
                        )
                    )
                    fence_lines.clear()
                fence = None
            else:
                fence = marker
                fence_start = line_number
            continue
        if fence is not None:
            if stripped:
                fence_lines.append((line_number, line))
            continue
        if not stripped:
            flush_paragraph()
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            flush_paragraph()
            blocks.append(
                _content_block(
                    stripped,
                    line_number,
                    line_number,
                    section.heading,
                    kind="list_item",
                )
            )
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            blocks.append(
                _content_block(
                    stripped,
                    line_number,
                    line_number,
                    section.heading,
                    kind="table_row",
                )
            )
            continue
        paragraph.append((line_number, line))
    flush_paragraph()
    if fence is not None:
        if fence_lines:
            block_text = "\n".join(line for _line_no, line in fence_lines)
            blocks.append(
                _content_block(
                    block_text,
                    fence_start,
                    section.line_end,
                    section.heading,
                    kind="fenced_block",
                )
            )
        diagnostics.append(
            {
                "section_id": section.section_id,
                "code": "MARKDOWN_FENCE_UNCLOSED",
                "message": "Fenced code block was not closed before end of file.",
            }
        )
    return blocks, diagnostics


def _fence_marker(stripped: str) -> _FenceMarker | None:
    match = _FENCE_RE.match(stripped)
    if match is None:
        return None
    marker = match.group(1)
    return _FenceMarker(character=marker[0], length=len(marker))


def _content_block(
    text: str,
    line_start: int,
    line_end: int,
    heading: str | None,
    *,
    kind: str = "paragraph",
) -> ContentBlock:
    requirement_bearing = bool(_NORMATIVE_RE.search(text)) or bool(
        heading and _REQUIREMENT_HEADING_RE.search(heading)
    )
    return ContentBlock(
        text=text,
        line_start=line_start,
        line_end=line_end,
        requirement_bearing=requirement_bearing,
        kind=kind,
    )


def _positive_non_requirement_reason(unit: SourceUnit) -> str | None:
    if _has_blocker_signal(unit):
        return None
    heading_leaf = _heading_leaf(unit)
    if heading_leaf in _NON_REQUIREMENT_HEADINGS:
        return f"non_requirement_heading:{heading_leaf}"
    for prefix in _NON_REQUIREMENT_PREFIXES:
        if unit.text_excerpt.startswith(prefix):
            return f"non_requirement_marker:{prefix}"
    return None


def _has_blocker_signal(unit: SourceUnit) -> bool:
    text = unit.text_excerpt
    heading_text = " ".join(unit.heading_path)
    combined = f"{heading_text} {text}"
    return bool(
        _NORMATIVE_RE.search(combined)
        or _CAPABILITY_RE.search(combined)
        or _ACCEPTANCE_MARKER_RE.search(combined)
        or _CONSTRAINT_MARKER_RE.search(combined)
    )


def _candidate_classification(unit: SourceUnit) -> str:  # noqa: PLR0911
    heading_text = " ".join(unit.heading_path)
    heading_leaf = _heading_leaf(unit)
    if heading_leaf in _OPEN_QUESTION_HEADINGS:
        return "open_question"
    if _ASSUMPTION_HEADING_RE.search(heading_text):
        return "assumption"
    if _DEPENDENCY_HEADING_RE.search(heading_text):
        return "dependency"
    if _GOAL_HEADING_RE.search(heading_text):
        return "goal"
    if _ACCEPTANCE_MARKER_RE.search(unit.text_excerpt) or _ACCEPTANCE_MARKER_RE.search(
        heading_text
    ):
        return "acceptance_criterion"
    if _QUALITY_ATTRIBUTE_RE.search(unit.text_excerpt) or _QUALITY_ATTRIBUTE_RE.search(
        heading_text
    ):
        return "quality_attribute"
    if _CONSTRAINT_MARKER_RE.search(heading_text) or (
        _NORMATIVE_RE.search(unit.text_excerpt)
        and _CONSTRAINT_MARKER_RE.search(unit.text_excerpt)
    ):
        return "constraint"
    if _NORMATIVE_RE.search(unit.text_excerpt):
        return "requirement"
    if unit.requirement_bearing and _REQUIREMENT_HEADING_RE.search(
        " ".join(unit.heading_path)
    ):
        return "requirement"
    return "uncertain"


def _candidate_clauses(unit: SourceUnit) -> list[str]:
    pieces = [unit.text_excerpt]
    if unit.kind == "table_row":
        pieces = [
            piece.strip()
            for piece in unit.text_excerpt.strip("|").split("|")
            if piece.strip()
        ]
    clauses: list[str] = []
    for piece in pieces:
        for clause in re.split(r";\s+|\n(?=\s*(?:\d+\.|-|\*|\+)\s+)", piece):
            normalized = _normalize_text(clause)
            if normalized:
                clauses.append(normalized)
    return clauses


def _heading_leaf(unit: SourceUnit) -> str:
    if not unit.heading_path:
        return ""
    return _normalize_text(unit.heading_path[-1]).casefold()


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def _bounded(text: str, limit: int = 2_000) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _sha256_prefixed(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _slug_hash(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).casefold().encode("utf-8")).hexdigest()[
        :12
    ]


def _candidate_id(unit_id: str, quote_hash: str, index: int) -> str:
    seed = f"{unit_id}:{quote_hash}:{index}".encode()
    return f"REQ-{hashlib.sha256(seed).hexdigest()[:16]}"


def _field_value(item: object, *names: str) -> object | None:
    if isinstance(item, Mapping):
        data = cast("Mapping[str, object]", item)
        for name in names:
            if name in data:
                return data.get(name)
        return None
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _string_field(item: object, *names: str) -> str | None:
    value = _field_value(item, *names)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _authority_item_for_entry(
    entry: object,
    authority_by_id: dict[str, object],
) -> object | None:
    item_id = _string_field(entry, "authority_item_id", "target_id", "id")
    if item_id is None:
        return None
    return authority_by_id.get(item_id)


def _target_kind(entry: object, item: object | None) -> AuthorityTargetKind:
    if item is not None:
        item_value = _field_value(
            item,
            "authority_target_kind",
            "target_kind",
            "kind",
            "type",
        )
        return _coerce_target_kind(item_value)
    entry_value = _field_value(
        entry,
        "authority_target_kind",
        "target_kind",
        "kind",
        "type",
    )
    return _coerce_target_kind(entry_value)


def _coerce_target_kind(value: object | None) -> AuthorityTargetKind:
    if isinstance(value, AuthorityTargetKind):
        return value
    if value is None:
        return AuthorityTargetKind.UNKNOWN
    try:
        return AuthorityTargetKind(str(value))
    except ValueError:
        return AuthorityTargetKind.UNKNOWN


def _authority_item_id(
    entry: object,
    item: object | None,
    target_kind: AuthorityTargetKind,
    target_text: str,
) -> str:
    item_id = _string_field(entry, "authority_item_id", "target_id", "id")
    if item_id is not None:
        return item_id
    item_id = _string_field(item, "id", "authority_item_id")
    if item_id is not None:
        return item_id
    candidate_id = _string_field(entry, "candidate_id") or ""
    finding_code = (
        _string_field(entry, "finding_code") or "AUTHORITY_CANDIDATE_UNCOVERED"
    )
    generated_ids = {
        AuthorityTargetKind.GAP: _generated_gap_id(
            candidate_id,
            finding_code,
            target_text,
        ),
        AuthorityTargetKind.ASSUMPTION: _generated_assumption_id(
            candidate_id,
            target_kind,
            target_text,
        ),
        AuthorityTargetKind.ELIGIBLE_FEATURE_RULE: _generated_target_id(
            "EFR",
            candidate_id,
            target_kind,
            target_text,
        ),
        AuthorityTargetKind.REJECTED_FEATURE: _generated_target_id(
            "RF",
            candidate_id,
            target_kind,
            target_text,
        ),
        AuthorityTargetKind.INVARIANT: _generated_target_id(
            "INV",
            candidate_id,
            target_kind,
            target_text,
        ),
        AuthorityTargetKind.UNKNOWN: _generated_target_id(
            "UNK",
            candidate_id,
            target_kind,
            target_text,
        ),
    }
    return generated_ids[target_kind]


def _target_text(entry: object, item: object | None) -> str:
    return (
        _string_field(entry, "target_text", "text", "mapping_rationale")
        or _string_field(item, "text", "statement", "description", "rationale")
        or ""
    )


def _source_quote_hash(
    entry: object,
    candidate: RequirementCandidate,
) -> str | None:
    source_quote_hash = _string_field(entry, "source_quote_hash", "quote_hash")
    if source_quote_hash is not None:
        return source_quote_hash
    source_quote = _string_field(entry, "source_quote", "quote")
    if source_quote in {candidate.source_quote, candidate.statement}:
        return candidate.quote_hash
    if source_quote is not None:
        return _sha256_prefixed(source_quote.encode("utf-8"))
    return None


def _mapping_provenance(entry: object) -> MappingProvenance:
    value = _field_value(entry, "mapping_provenance", "provenance")
    if isinstance(value, MappingProvenance):
        return value
    try:
        return MappingProvenance(str(value))
    except ValueError:
        return MappingProvenance.LEGACY_ABSENT


def _mapping_status(  # noqa: PLR0911
    candidate: RequirementCandidate,
    target_kind: AuthorityTargetKind,
    source_quote_hash: str | None,
    mapping_provenance: MappingProvenance,
    target_resolved: bool,
) -> tuple[CoverageStatus, str]:
    if candidate.classification == "uncertain":
        return CoverageStatus.UNCERTAIN, "candidate classification is uncertain"
    if source_quote_hash != candidate.quote_hash:
        return CoverageStatus.WEAK_MAPPING, "mapping lacks exact source quote hash"
    if mapping_provenance not in _ACCEPTABLE_MAPPING_PROVENANCE:
        return CoverageStatus.WEAK_MAPPING, "mapping provenance is not accepted"
    if not target_resolved:
        return CoverageStatus.WEAK_MAPPING, "authority target is missing or unknown"
    if _is_intentional_classification(candidate.classification, target_kind):
        return (
            CoverageStatus.INTENTIONALLY_CLASSIFIED,
            "candidate is intentionally classified but not covered",
        )
    if not _target_kind_compatible(candidate, target_kind):
        return CoverageStatus.WEAK_MAPPING, "authority target kind is incompatible"
    if (
        candidate.classification == "constraint"
        and target_kind == AuthorityTargetKind.REJECTED_FEATURE
        and not _is_forbidden_safety_constraint(candidate)
    ):
        return (
            CoverageStatus.WEAK_MAPPING,
            "rejected feature mapping requires forbidden or safety constraint",
        )
    if (
        candidate.classification == "goal"
        and target_kind == AuthorityTargetKind.ELIGIBLE_FEATURE_RULE
        and not _is_future_scope_constraint(candidate)
    ):
        return (
            CoverageStatus.WEAK_MAPPING,
            "eligible feature rule mapping is weak without future scope constraint",
        )
    return CoverageStatus.COVERED, "exact quote and compatible authority target"


def _target_resolved(
    item: object | None,
    target_kind: AuthorityTargetKind,
    target_text: str,
) -> bool:
    if target_kind == AuthorityTargetKind.UNKNOWN:
        return False
    if item is not None:
        return True
    return bool(target_text) and target_kind in {
        AuthorityTargetKind.ASSUMPTION,
        AuthorityTargetKind.ELIGIBLE_FEATURE_RULE,
        AuthorityTargetKind.GAP,
        AuthorityTargetKind.REJECTED_FEATURE,
    }


def _is_intentional_classification(
    classification: str,
    target_kind: AuthorityTargetKind,
) -> bool:
    if classification == "open_question" and target_kind == AuthorityTargetKind.GAP:
        return True
    return (
        classification == "assumption"
        and target_kind == AuthorityTargetKind.ASSUMPTION
    )


def _target_kind_compatible(
    candidate: RequirementCandidate,
    target_kind: AuthorityTargetKind,
) -> bool:
    compatible: dict[str, frozenset[AuthorityTargetKind]] = {
        "requirement": frozenset({AuthorityTargetKind.INVARIANT}),
        "acceptance_criterion": frozenset({AuthorityTargetKind.INVARIANT}),
        "constraint": frozenset(
            {AuthorityTargetKind.INVARIANT, AuthorityTargetKind.REJECTED_FEATURE}
        ),
        "quality_attribute": frozenset({AuthorityTargetKind.INVARIANT}),
        "dependency": frozenset({AuthorityTargetKind.INVARIANT}),
        "goal": frozenset(
            {AuthorityTargetKind.INVARIANT, AuthorityTargetKind.ELIGIBLE_FEATURE_RULE}
        ),
        "non_goal": frozenset(
            {AuthorityTargetKind.REJECTED_FEATURE, AuthorityTargetKind.INVARIANT}
        ),
    }
    return target_kind in compatible.get(candidate.classification, frozenset())


def _is_future_scope_constraint(candidate: RequirementCandidate) -> bool:
    return bool(_FUTURE_SCOPE_CONSTRAINT_RE.search(candidate.statement))


def _is_forbidden_safety_constraint(candidate: RequirementCandidate) -> bool:
    return bool(
        re.search(
            r"\b("
            r"forbidden|never|cannot|must not|not allowed|"
            r"proibido|nao deve|n" "\u00e3" r"o deve|safety|unsafe"
            r")\b",
            candidate.statement,
            re.IGNORECASE,
        )
    )


def _candidate_has_covered_mapping(mappings: list[AuthorityMapping]) -> bool:
    return any(
        mapping.mapping_status == CoverageStatus.COVERED for mapping in mappings
    )


def _finding(
    code: str,
    message: str,
    candidate_ids: list[str],
    source_unit_ids: list[str],
    *,
    override_allowed: bool = True,
) -> AuthorityReviewFinding:
    payload = {
        "candidate_ids": sorted(candidate_ids),
        "code": code,
        "source_unit_ids": sorted(source_unit_ids),
    }
    finding_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return AuthorityReviewFinding(
        finding_id=f"ARF-{finding_hash}",
        severity="blocking",
        code=code,
        message=message,
        candidate_ids=sorted(candidate_ids),
        source_unit_ids=sorted(source_unit_ids),
        override_allowed=override_allowed,
    )


def _generated_gap_id(
    candidate_id: str,
    finding_code: str,
    normalized_gap_text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "finding_code": finding_code,
        "normalized_gap_text": _normalize_text(normalized_gap_text),
    }
    return f"GAP-{_canonical_hash(payload)}"


def _generated_assumption_id(
    candidate_id: str,
    target_kind: AuthorityTargetKind,
    normalized_assumption_text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "normalized_assumption_text": _normalize_text(normalized_assumption_text),
        "target_kind": target_kind.value,
    }
    return f"ASM-{_canonical_hash(payload)}"


def _generated_target_id(
    prefix: str,
    candidate_id: str,
    target_kind: AuthorityTargetKind,
    text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "normalized_text": _normalize_text(text),
        "target_kind": target_kind.value,
    }
    return f"{prefix}-{_canonical_hash(payload)}"


def _canonical_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
