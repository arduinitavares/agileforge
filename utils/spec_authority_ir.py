# utils/spec_authority_ir.py
"""Deterministic parser and candidate extraction for Spec Authority IR."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

JsonDict = dict[str, Any]

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


def _candidate_classification(unit: SourceUnit) -> str:
    if _heading_leaf(unit) in _OPEN_QUESTION_HEADINGS:
        return "open_question"
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
    return text if len(text) <= limit else text[:limit]


def _sha256_prefixed(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _slug_hash(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).casefold().encode("utf-8")).hexdigest()[
        :12
    ]


def _candidate_id(unit_id: str, quote_hash: str, index: int) -> str:
    seed = f"{unit_id}:{quote_hash}:{index}".encode()
    return f"REQ-{hashlib.sha256(seed).hexdigest()[:16]}"
