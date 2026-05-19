"""Host-side normalizer/validator for spec_authority_compiler_agent output.

This enforces compiler semantics on the host side:
- prompt_hash is anchored to SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
- invariant IDs are deterministically computed from source_map excerpt,
  invariant.type, and invariant.parameters

The caller MUST use the normalized output downstream.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import ValidationError

from orchestrator_agent.agent_tools.spec_authority_compiler_agent.compiler_contract import (
    compute_invariant_id,
    compute_prompt_hash,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from utils.agileforge_spec_profile import (
    AgileForgeSpecStatus,
    AgileForgeSpecType,
    SpecItem,
    TechnicalSpecArtifact,
)
from utils.spec_authority_ir import (
    AuthorityTargetKind,
    IrProvenance,
    MappingProvenance,
    RequirementCandidate,
    SourceUnit,
    SourceUnitDisposition,
    build_authority_mappings,
    extract_requirement_candidates,
    parse_markdown_sections,
    source_units_from_sections,
)
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerOutput,
    SpecAuthorityIrPacketLimits,
    SpecAuthorityMapping,
    SpecAuthorityRequirementCandidate,
    SpecAuthoritySourceUnit,
)

logger: logging.Logger = logging.getLogger(name=__name__)

SpecSourceFormat = Literal["agileforge.spec.v1", "agileforge.spec_legacy_markdown.v1"]

_META_POLICY_LOCATION_RE = re.compile(
    r"\b("
    r"plagiarism|academic integrity|citation|bibliography|"
    r"rubric|grading|marking|assessment criteria|submission instructions?|"
    r"submission requirements?|deliverables?|course policy|integrity policy"
    r")\b",
    flags=re.IGNORECASE,
)

_META_POLICY_EXCERPT_PATTERNS = (
    re.compile(r"\bplagiarism policy\b", flags=re.IGNORECASE),
    re.compile(r"\bacademic integrity\b", flags=re.IGNORECASE),
    re.compile(r"\bwithout appropriate citation\b", flags=re.IGNORECASE),
    re.compile(r"\bappropriate(?:ly)? cited?\b", flags=re.IGNORECASE),
    re.compile(r"\breferencing the work(?:s)? of others\b", flags=re.IGNORECASE),
    re.compile(
        r"\brepresenting the work(?:s)? of others as (?:one'?s|your) own\b",
        flags=re.IGNORECASE,
    ),
    re.compile(r"\bgrading rubric\b", flags=re.IGNORECASE),
    re.compile(r"\bassessment criteria\b", flags=re.IGNORECASE),
    re.compile(r"\bsubmission instructions?\b", flags=re.IGNORECASE),
    re.compile(r"\bsubmission requirements?\b", flags=re.IGNORECASE),
)

_META_POLICY_ASSUMPTION = (
    "Excluded non-product policy/admin excerpts from compiled invariants."
)
_DUPLICATE_INVARIANT_ASSUMPTION = (
    "Removed duplicate compiled invariant entries with identical type and parameters."
)
_FIELD_SUPPORT_RATIO_THRESHOLD = 1.0
_RELATION_SUPPORT_RATIO_THRESHOLD = 0.75
_SUPPORT_RATIO_THRESHOLD = 0.5
_FORBIDDEN_SAFETY_SUPPORT_THRESHOLD = 0.25
_FORBIDDEN_SAFETY_CUE_RE = re.compile(
    r"\b("
    r"must\s+not|do\s+not|never|forbidden|prohibited|disallow|deny|"
    r"omit|suppress|exits?|contract_unverified|without"
    r")\b|\bbefore\s+(?:reading|constructing)\b",
    flags=re.IGNORECASE,
)
_MAX_VALUE_CUE_RE = re.compile(
    r"(<=|\b(?:max(?:imum)?|at most|no more than|must not exceed|"
    r"less than or equal|cap|limit)\b)",
    flags=re.IGNORECASE,
)
_FORBIDDEN_CAPABILITY_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "authenticated": ("api", "post", "token", "tokens"),
    "authentication": ("api", "post", "token", "tokens"),
    "submission": ("post", "request", "submit"),
    "submissions": ("post", "request", "submit"),
    "submit": ("post", "request", "submission"),
}


@dataclass(frozen=True)
class _SourceEvidenceCandidate:
    """Candidate source evidence for an invariant."""

    excerpt: str
    location: str | None


@dataclass(frozen=True)
class _CompactIrModelHints:
    """Raw compact IR hints supplied by the model before host normalization."""

    ir_provenance: IrProvenance | None
    candidates: list[SpecAuthorityRequirementCandidate]
    mappings: list[SpecAuthorityMapping]


@dataclass(frozen=True)
class _ProfileSourceFragment:
    """One explicit source fragment from an AgileForge profile item."""

    item: SpecItem
    location: str
    text: str
    classification: str | None
    requirement_bearing: bool


def _failure(reason: str, blocking_gaps: list[str]) -> SpecAuthorityCompilerOutput:
    return SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationFailure(
            error="SPEC_COMPILATION_FAILED",
            reason=reason,
            blocking_gaps=blocking_gaps,
        )
    )


def _strip_markdown_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if not lines:
        return text

    lines = lines[1:]
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_candidate(raw_text: str) -> str:
    text = _strip_markdown_fence(raw_text)
    if not text:
        return text

    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return text
        return text[start : end + 1].strip()


def _summarize_validation_error(label: str, exc: ValidationError) -> str:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(part) for part in first.get("loc", []))
        msg = first.get("msg", "validation error")
        if loc:
            return f"{label}: {loc}: {msg}"
        return f"{label}: {msg}"
    return f"{label}: {exc}"


def _detect_source_format(source_text: str | None) -> SpecSourceFormat:
    """Detect whether the source is canonical AgileForge profile JSON."""
    if not source_text:
        return "agileforge.spec_legacy_markdown.v1"
    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return "agileforge.spec_legacy_markdown.v1"
    if (
        isinstance(parsed, dict)
        and parsed.get("schema_version") == "agileforge.spec.v1"
    ):
        return "agileforge.spec.v1"
    return "agileforge.spec_legacy_markdown.v1"


def _profile_artifact(source_text: str | None) -> TechnicalSpecArtifact | None:
    """Parse a structured AgileForge spec profile source if available."""
    if not source_text:
        return None
    try:
        return TechnicalSpecArtifact.model_validate_json(source_text)
    except ValidationError:
        return None


def _bounded_compact_ir_text(text: str, *, limit: int = 2_000) -> str:
    """Return text bounded to compact IR excerpt limits."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _profile_item_classification(item: SpecItem) -> str | None:
    """Return the compact IR candidate classification for a typed profile item."""
    item_type = item.type
    if item_type in {
        AgileForgeSpecType.REQ,
        AgileForgeSpecType.DATA,
        AgileForgeSpecType.INTERFACE,
    }:
        return "requirement"
    if item_type == AgileForgeSpecType.QUALITY:
        return "quality_attribute"
    if item_type == AgileForgeSpecType.CONSTRAINT:
        return "constraint"
    if item_type == AgileForgeSpecType.GOAL:
        return "goal"
    if item_type == AgileForgeSpecType.NON_GOAL:
        return "non_goal"
    if item_type == AgileForgeSpecType.ASSUMPTION:
        return "assumption"
    if item_type == AgileForgeSpecType.OPEN_QUESTION:
        return "open_question"
    return None


def _profile_item_is_active(item: SpecItem) -> bool:
    """Return whether a profile item is active review input."""
    return item.status not in {
        AgileForgeSpecStatus.REJECTED,
        AgileForgeSpecStatus.SUPERSEDED,
    }


def _profile_source_fragments(
    artifact: TechnicalSpecArtifact,
) -> list[_ProfileSourceFragment]:
    """Return explicit profile item fragments used for evidence and IR."""
    fragments: list[_ProfileSourceFragment] = []
    for item in artifact.items:
        classification = _profile_item_classification(item)
        active = _profile_item_is_active(item)
        requirement_bearing = active and classification is not None
        fragments.append(
            _ProfileSourceFragment(
                item=item,
                location=f"{item.id}.statement",
                text=item.statement,
                classification=classification,
                requirement_bearing=requirement_bearing,
            )
        )
        if not active or item.type == AgileForgeSpecType.EXAMPLE:
            continue
        for index, acceptance in enumerate(item.acceptance, start=1):
            fragments.append(
                _ProfileSourceFragment(
                    item=item,
                    location=f"{item.id}.acceptance[{index}]",
                    text=acceptance,
                    classification="acceptance_criterion",
                    requirement_bearing=classification is not None,
                )
            )
    return fragments


def _profile_evidence_candidates(
    source_text: str | None,
) -> list[_SourceEvidenceCandidate]:
    """Return source evidence candidates from structured spec items."""
    artifact = _profile_artifact(source_text)
    if artifact is None:
        return []
    return [
        _SourceEvidenceCandidate(
            excerpt=_compact_whitespace(fragment.text),
            location=fragment.location,
        )
        for fragment in _profile_source_fragments(artifact)
        if _compact_whitespace(fragment.text)
    ]


def _profile_source_units_and_candidates(
    source_text: str | None,
) -> tuple[list[SourceUnit], list[RequirementCandidate]] | None:
    """Build compact IR source units from typed AgileForge profile items."""
    artifact = _profile_artifact(source_text)
    if artifact is None:
        return None

    units: list[SourceUnit] = []
    candidates: list[RequirementCandidate] = []
    for index, fragment in enumerate(_profile_source_fragments(artifact), start=1):
        normalized_location = _compact_whitespace(fragment.location).casefold()
        location_hash = hashlib.sha256(
            normalized_location.encode("utf-8")
        ).hexdigest()[:12]
        source_quote = _compact_whitespace(fragment.text)
        text_hash = _source_quote_hash(source_quote)
        unit_id = f"SRC-AF-{location_hash}"
        if not _profile_item_is_active(fragment.item):
            disposition = SourceUnitDisposition.NON_REQUIREMENT
            disposition_reason = "profile item status is not active"
        elif fragment.requirement_bearing:
            disposition = SourceUnitDisposition.CANDIDATE_EXTRACTED
            disposition_reason = None
        else:
            disposition = SourceUnitDisposition.INTENTIONALLY_CLASSIFIED
            disposition_reason = "profile item type is non-normative context"

        units.append(
            SourceUnit(
                unit_id=unit_id,
                section_id=fragment.item.id,
                heading_path=(fragment.item.type.value, fragment.item.id),
                kind=f"agileforge_profile_{fragment.location.rsplit('.', 1)[-1]}",
                line_start=index,
                line_end=index,
                text_hash=text_hash,
                text_excerpt=_bounded_compact_ir_text(source_quote),
                requirement_bearing=fragment.requirement_bearing,
                disposition=disposition,
                disposition_reason=disposition_reason,
            )
        )
        if not fragment.requirement_bearing or fragment.classification is None:
            continue
        candidate_hash = hashlib.sha256(
            f"{fragment.location}|{text_hash}".encode()
        ).hexdigest()[:16]
        candidates.append(
            RequirementCandidate(
                candidate_id=f"CAND-{candidate_hash}",
                source_unit_id=unit_id,
                statement=source_quote,
                source_quote=_bounded_compact_ir_text(source_quote),
                quote_hash=text_hash,
                line_start=index,
                line_end=index,
                classification=fragment.classification,
            )
        )
    return units, candidates


def _is_meta_policy_source(location: str | None, excerpt: str) -> bool:
    location_text = (location or "").strip()
    excerpt_text = (excerpt or "").strip()
    if location_text and _META_POLICY_LOCATION_RE.search(location_text):
        return True
    return any(
        pattern.search(excerpt_text) for pattern in _META_POLICY_EXCERPT_PATTERNS
    )


def _filter_meta_policy_invariants(success: SpecAuthorityCompilationSuccess) -> int:
    """Remove invariants sourced only from non-product policy/admin excerpts."""
    if not success.invariants or not success.source_map:
        return 0

    source_map_indexes_by_id: dict[str, list[int]] = {}
    for entry_index, entry in enumerate(success.source_map):
        source_map_indexes_by_id.setdefault(entry.invariant_id, []).append(entry_index)

    original_ids = [inv.id for inv in success.invariants]
    has_duplicate_ids = len(set(original_ids)) < len(original_ids)
    use_positional_matching = has_duplicate_ids and len(success.source_map) >= len(
        success.invariants
    )

    matched_entry_indexes: dict[int, list[int]] = {}
    matched_source_indexes: set[int] = set()
    for inv_index, inv in enumerate(success.invariants):
        entry_indexes: list[int] = []
        if use_positional_matching and inv_index < len(success.source_map):
            entry_indexes.append(inv_index)
        else:
            entry_indexes.extend(source_map_indexes_by_id.get(inv.id, []))
            if not entry_indexes and len(success.source_map) == len(success.invariants):
                if inv_index < len(success.source_map):
                    entry_indexes.append(inv_index)
        if entry_indexes:
            matched_entry_indexes[inv_index] = entry_indexes
            matched_source_indexes.update(entry_indexes)

    kept_invariants = []
    kept_source_indexes: set[int] = set()
    filtered_count = 0

    for inv_index, inv in enumerate(success.invariants):
        entry_indexes = matched_entry_indexes.get(inv_index, [])
        if not entry_indexes:
            kept_invariants.append(inv)
            continue

        matched_entries = [success.source_map[idx] for idx in entry_indexes]
        if all(
            _is_meta_policy_source(entry.location, entry.excerpt)
            for entry in matched_entries
        ):
            filtered_count += 1
            continue

        kept_invariants.append(inv)
        for idx in entry_indexes:
            entry = success.source_map[idx]
            if not _is_meta_policy_source(entry.location, entry.excerpt):
                kept_source_indexes.add(idx)

    if not filtered_count:
        return 0

    kept_invariant_ids = {inv.id for inv in kept_invariants}
    filtered_source_map = []
    for entry_index, entry in enumerate(success.source_map):
        if _is_meta_policy_source(entry.location, entry.excerpt):
            continue
        if entry_index in kept_source_indexes:
            filtered_source_map.append(entry)
            continue
        if (
            not use_positional_matching
            and entry_index not in matched_source_indexes
            and entry.invariant_id in kept_invariant_ids
        ):
            filtered_source_map.append(entry)

    success.invariants = kept_invariants
    success.source_map = filtered_source_map
    if _META_POLICY_ASSUMPTION not in success.assumptions:
        success.assumptions.append(_META_POLICY_ASSUMPTION)
    if not success.invariants:
        gap = "No invariants extracted from spec after excluding non-product policy/admin excerpts"
        if gap not in success.gaps:
            success.gaps.append(gap)

    logger.info(
        "Filtered %s meta-policy/admin invariant(s) from compiler output",
        filtered_count,
    )
    return filtered_count


def _invariant_semantic_key(inv: Invariant) -> tuple[str, str]:
    """Return stable semantic identity for duplicate invariant removal."""
    return (
        inv.type.value,
        json.dumps(inv.parameters.model_dump(mode="json"), sort_keys=True),
    )


def _deduplicate_semantic_invariants(success: SpecAuthorityCompilationSuccess) -> int:
    """Remove exact duplicate invariant objects before deterministic ID assignment."""
    seen: set[tuple[str, str]] = set()
    kept: list[Invariant] = []
    removed = 0
    for inv in success.invariants:
        key = _invariant_semantic_key(inv)
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(inv)
    if not removed:
        return 0
    success.invariants = kept
    if _DUPLICATE_INVARIANT_ASSUMPTION not in success.assumptions:
        success.assumptions.append(_DUPLICATE_INVARIANT_ASSUMPTION)
    logger.info("Removed %s duplicate semantic invariant(s)", removed)
    return removed


def _tokenize_support_text(text: str) -> list[str]:
    """Return normalized support tokens for source/invariant comparisons."""
    return [
        token
        for token in re.split(r"[^a-zA-Z0-9]+", text.casefold())
        if token
    ]


def _support_overlap_ratio(expected: list[str], excerpt: str) -> float:
    """Return how much expected invariant language appears in the excerpt."""
    expected_unique = sorted(set(expected))
    if not expected_unique:
        return 1.0
    excerpt_tokens = set(_tokenize_support_text(excerpt))
    matched = sum(1 for token in expected_unique if token in excerpt_tokens)
    return matched / len(expected_unique)


def _forbidden_capability_support_tokens(capability: str) -> list[str]:
    """Return capability tokens plus narrow aliases for explicit safety guards."""
    tokens = _tokenize_support_text(capability)
    expanded: set[str] = set(tokens)
    for token in tokens:
        expanded.update(_FORBIDDEN_CAPABILITY_TOKEN_ALIASES.get(token, ()))
    return sorted(expanded)


def _relation_operator_supported(expression: str, excerpt: str) -> bool:
    """Return whether an excerpt preserves the relation operator semantics."""
    text = excerpt.casefold()
    if "<=" in expression:
        return "<=" in excerpt or "less than or equal" in text or "at most" in text
    if ">=" in expression:
        return ">=" in excerpt or "greater than or equal" in text or "at least" in text
    if "<" in expression:
        return "<" in excerpt or "less than" in text or "before" in text
    if ">" in expression:
        return ">" in excerpt or "greater than" in text or "after" in text
    if "==" in expression:
        return "==" in excerpt or "equal" in text or "exactly" in text
    return True


def _source_map_support_error(inv: Invariant, excerpt: str) -> str | None:
    """Return a mismatch reason if the excerpt cannot directly support invariant."""
    parameters = inv.parameters
    if inv.type == InvariantType.REQUIRED_FIELD:
        field_name = str(getattr(parameters, "field_name", "") or "")
        tokens = _tokenize_support_text(field_name)
        if _support_overlap_ratio(tokens, excerpt) < _FIELD_SUPPORT_RATIO_THRESHOLD:
            return (
                f"source_map excerpt does not mention required field '{field_name}' "
                f"for invariant {inv.id}"
            )
        return None

    if inv.type == InvariantType.FORBIDDEN_CAPABILITY:
        capability = str(getattr(parameters, "capability", "") or "")
        tokens = _tokenize_support_text(capability)
        if _support_overlap_ratio(tokens, excerpt) < _SUPPORT_RATIO_THRESHOLD:
            safety_tokens = _forbidden_capability_support_tokens(capability)
            if _FORBIDDEN_SAFETY_CUE_RE.search(
                excerpt
            ) and (
                _support_overlap_ratio(safety_tokens, excerpt)
                >= _FORBIDDEN_SAFETY_SUPPORT_THRESHOLD
            ):
                return None
            return (
                "source_map excerpt does not mention forbidden capability "
                f"'{capability}' for invariant {inv.id}"
            )
        return None

    if inv.type == InvariantType.MAX_VALUE:
        field_name = str(getattr(parameters, "field_name", "") or "")
        raw_max_value = getattr(parameters, "max_value", None)
        max_value = "" if raw_max_value is None else str(raw_max_value)
        field_tokens = _tokenize_support_text(field_name)
        excerpt_tokens = set(_tokenize_support_text(excerpt))
        if _support_overlap_ratio(field_tokens, excerpt) < _FIELD_SUPPORT_RATIO_THRESHOLD:
            return (
                f"source_map excerpt does not mention max-value field "
                f"'{field_name}' for invariant {inv.id}"
            )
        if max_value and max_value.casefold() not in excerpt_tokens:
            return (
                f"source_map excerpt does not mention max value '{max_value}' "
                f"for invariant {inv.id}"
            )
        if not _MAX_VALUE_CUE_RE.search(excerpt):
            return (
                "source_map excerpt does not describe a maximum/limit "
                f"for invariant {inv.id}"
            )
        return None

    if inv.type == InvariantType.RELATION_CONSTRAINT:
        expression = str(getattr(parameters, "expression", "") or "")
        expression_tokens = [
            token
            for token in _tokenize_support_text(expression)
            if not token.isdigit()
        ]
        if not _relation_operator_supported(expression, excerpt):
            return (
                "source_map excerpt does not preserve relation operator "
                f"'{expression}' for invariant {inv.id}"
            )
        if (
            _support_overlap_ratio(expression_tokens, excerpt)
            < _RELATION_SUPPORT_RATIO_THRESHOLD
        ):
            return (
                "source_map excerpt does not mention relation expression "
                f"'{expression}' for invariant {inv.id}"
            )
        return None

    return None


def _source_map_support_score(inv: Invariant, excerpt: str) -> float:
    """Return a ranking score for valid source evidence candidates."""
    parameters = inv.parameters
    if inv.type == InvariantType.REQUIRED_FIELD:
        field_name = str(getattr(parameters, "field_name", "") or "")
        return _support_overlap_ratio(_tokenize_support_text(field_name), excerpt)
    if inv.type == InvariantType.FORBIDDEN_CAPABILITY:
        capability = str(getattr(parameters, "capability", "") or "")
        base = _support_overlap_ratio(_tokenize_support_text(capability), excerpt)
        if _FORBIDDEN_SAFETY_CUE_RE.search(excerpt):
            base += 0.1
        return base
    if inv.type == InvariantType.MAX_VALUE:
        field_name = str(getattr(parameters, "field_name", "") or "")
        return _support_overlap_ratio(_tokenize_support_text(field_name), excerpt)
    if inv.type == InvariantType.RELATION_CONSTRAINT:
        expression = str(getattr(parameters, "expression", "") or "")
        expression_tokens = [
            token
            for token in _tokenize_support_text(expression)
            if not token.isdigit()
        ]
        base = _support_overlap_ratio(expression_tokens, excerpt)
        if _relation_operator_supported(expression, excerpt):
            base += 0.1
        return base
    return 0.0


def _compact_whitespace(text: str) -> str:
    """Collapse whitespace for source-text matching."""
    return " ".join(text.split())


def _fr_ids_from_text(text: str) -> list[str]:
    """Return functional requirement IDs mentioned in compiler locations/excerpts."""
    return sorted(set(re.findall(r"\bFR-\d{3}\b", text, flags=re.IGNORECASE)))


def _source_text_lines_for_fr(source_text: str, fr_id: str) -> list[str]:
    """Return source lines that define a functional requirement ID."""
    pattern = re.compile(rf"\|\s*{re.escape(fr_id)}\s*\|", flags=re.IGNORECASE)
    return [line.strip() for line in source_text.splitlines() if pattern.search(line)]


def _source_text_lines_containing(source_text: str, excerpt: str) -> list[str]:
    """Return exact source lines containing a compiler-provided excerpt."""
    needle = _compact_whitespace(excerpt).casefold()
    if not needle:
        return []
    matches: list[str] = []
    for line in source_text.splitlines():
        compact_line = _compact_whitespace(line)
        if needle in compact_line.casefold():
            matches.append(line.strip())
    return matches


def _source_text_line_candidates(source_text: str) -> list[_SourceEvidenceCandidate]:
    """Return every non-empty source line as fallback evidence candidates."""
    candidates: list[_SourceEvidenceCandidate] = []
    for line_number, line in enumerate(source_text.splitlines(), start=1):
        compact = _compact_whitespace(line)
        if compact:
            candidates.append(
                _SourceEvidenceCandidate(
                    excerpt=compact,
                    location=f"line {line_number}",
                )
            )
    return candidates


def _candidate_evidence_from_source_text(
    entry: SourceMapEntry,
    *,
    source_text: str | None,
) -> list[_SourceEvidenceCandidate]:
    """Build deduplicated evidence candidates from LLM source_map plus source text."""
    candidates: list[_SourceEvidenceCandidate] = []
    seen: set[tuple[str, str | None]] = set()

    def append(excerpt: str, location: str | None) -> None:
        compact = _compact_whitespace(excerpt)
        if not compact:
            return
        key = (compact, location)
        if key in seen:
            return
        seen.add(key)
        candidates.append(_SourceEvidenceCandidate(excerpt=compact, location=location))

    append(entry.excerpt, entry.location)
    if not source_text:
        return candidates

    location_text = entry.location or ""
    combined_hint = f"{location_text}\n{entry.excerpt}"
    for fr_id in _fr_ids_from_text(combined_hint):
        for line in _source_text_lines_for_fr(source_text, fr_id):
            append(line, entry.location or fr_id)
    for line in _source_text_lines_containing(source_text, entry.excerpt):
        append(line, entry.location)
    return candidates


def _repair_source_map_from_source_text(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str | None,
    source_format: SpecSourceFormat,
) -> bool:
    """Replace weak LLM source maps with one supported source entry per invariant."""
    if not source_text:
        return False

    evidence_candidates: list[_SourceEvidenceCandidate] = []
    if source_format == "agileforge.spec.v1":
        evidence_candidates.extend(_profile_evidence_candidates(source_text))
    else:
        for entry in success.source_map:
            evidence_candidates.extend(
                _candidate_evidence_from_source_text(entry, source_text=source_text)
            )
        evidence_candidates.extend(_source_text_line_candidates(source_text))

    if not evidence_candidates:
        return False

    repaired: list[SourceMapEntry] = []
    retained_invariants: list[Invariant] = []
    dropped_invariants: list[Invariant] = []
    for inv in success.invariants:
        supported = [
            candidate
            for candidate in evidence_candidates
            if _source_map_support_error(inv, candidate.excerpt) is None
        ]
        if not supported:
            dropped_invariants.append(inv)
            continue
        matched = max(
            supported,
            key=lambda candidate: _source_map_support_score(inv, candidate.excerpt),
        )
        retained_invariants.append(inv)
        repaired.append(
            SourceMapEntry(
                invariant_id=inv.id,
                excerpt=matched.excerpt,
                location=matched.location,
            )
        )

    if not retained_invariants:
        return False
    if dropped_invariants:
        success.invariants = retained_invariants
        for dropped in dropped_invariants:
            gap = f"Dropped unsupported compiler invariant: {_invariant_text(dropped)}"
            if gap not in success.gaps:
                success.gaps.append(gap)
    success.source_map = repaired
    return True


def _invariant_text(invariant: Invariant) -> str:
    """Return searchable text for an invariant authority item."""
    parameters = invariant.parameters
    if invariant.type == InvariantType.REQUIRED_FIELD:
        return f"required field {getattr(parameters, 'field_name', '')}"
    if invariant.type == InvariantType.FORBIDDEN_CAPABILITY:
        return f"forbidden capability {getattr(parameters, 'capability', '')}"
    if invariant.type == InvariantType.MAX_VALUE:
        return (
            f"maximum {getattr(parameters, 'field_name', '')} "
            f"{getattr(parameters, 'max_value', '')}"
        )
    if invariant.type == InvariantType.RELATION_CONSTRAINT:
        return f"relation constraint {getattr(parameters, 'expression', '')}"
    return invariant.id


def _authority_items(success: SpecAuthorityCompilationSuccess) -> list[dict[str, str]]:
    """Build authority item dictionaries understood by the shared IR mapper."""
    return [
        {
            "id": invariant.id,
            "authority_target_kind": AuthorityTargetKind.INVARIANT.value,
            "text": _invariant_text(invariant),
        }
        for invariant in success.invariants
    ]


def _candidate_for_source_entry(
    entry: SourceMapEntry,
    invariant: Invariant | None,
    candidates: list[RequirementCandidate],
) -> RequirementCandidate | None:
    """Choose the candidate referenced by a normalized source map entry."""
    excerpt = _compact_whitespace(entry.excerpt)
    exact_matches = [
        candidate
        for candidate in candidates
        if excerpt
        and excerpt
        in {
            _compact_whitespace(candidate.source_quote),
            _compact_whitespace(candidate.statement),
        }
    ]
    if exact_matches:
        return exact_matches[0]
    if invariant is None:
        return None
    supported = [
        candidate
        for candidate in candidates
        if _source_map_support_error(invariant, candidate.statement) is None
    ]
    if not supported:
        return None
    return max(
        supported,
        key=lambda candidate: _source_map_support_score(invariant, candidate.statement),
    )


def _legacy_mapping_provenance(
    entry: SourceMapEntry,
    entry_index: int,
    original_source_map: list[SourceMapEntry],
) -> MappingProvenance:
    """Classify source-map evidence generated from legacy fields."""
    if entry_index < len(original_source_map):
        original = original_source_map[entry_index]
        if _compact_whitespace(original.excerpt) == _compact_whitespace(entry.excerpt):
            return MappingProvenance.HOST_INFERRED
    return MappingProvenance.HOST_REPAIRED_QUOTE


def _source_entry_mapping_provenance(
    entry: SourceMapEntry,
    entry_index: int,
    original_source_map: list[SourceMapEntry],
    candidate: RequirementCandidate,
    *,
    source_format: SpecSourceFormat,
) -> MappingProvenance:
    """Classify provenance for a normalized source-map to candidate mapping."""
    if entry_index >= len(original_source_map):
        return MappingProvenance.HOST_REPAIRED_QUOTE
    original = original_source_map[entry_index]
    original_excerpt_matches = _compact_whitespace(
        original.excerpt
    ) == _compact_whitespace(candidate.source_quote)
    normalized_excerpt_matches = _compact_whitespace(
        entry.excerpt
    ) == _compact_whitespace(candidate.source_quote)
    if (
        source_format == "agileforge.spec.v1"
        and original_excerpt_matches
        and normalized_excerpt_matches
    ):
        return MappingProvenance.MODEL_QUOTE
    return _legacy_mapping_provenance(entry, entry_index, original_source_map)


def _source_quote_hash(text: str) -> str:
    """Return the canonical hash for exact source quote bytes."""
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _model_to_host_candidate_ids(
    model_ir_provenance: IrProvenance | None,
    model_candidates: list[SpecAuthorityRequirementCandidate],
    host_candidates: list[RequirementCandidate],
) -> dict[str, str]:
    """Return model candidate IDs that exactly match one host candidate quote."""
    if model_ir_provenance not in {IrProvenance.MODEL_EMITTED, IrProvenance.MIXED}:
        return {}
    host_by_id = {candidate.candidate_id: candidate for candidate in host_candidates}
    host_by_quote_hash: dict[str, list[RequirementCandidate]] = {}
    for candidate in host_candidates:
        host_by_quote_hash.setdefault(candidate.quote_hash, []).append(candidate)

    model_to_host: dict[str, str] = {}
    for candidate in model_candidates:
        if candidate.provenance != IrProvenance.MODEL_EMITTED:
            continue
        if _source_quote_hash(candidate.source_quote) != candidate.quote_hash:
            continue
        direct_host = host_by_id.get(candidate.candidate_id)
        if (
            direct_host is not None
            and direct_host.quote_hash == candidate.quote_hash
            and direct_host.source_quote == candidate.source_quote
        ):
            model_to_host[candidate.candidate_id] = direct_host.candidate_id
            continue
        matching_hosts = [
            host
            for host in host_by_quote_hash.get(candidate.quote_hash, [])
            if host.source_quote == candidate.source_quote
        ]
        if len(matching_hosts) == 1:
            model_to_host[candidate.candidate_id] = matching_hosts[0].candidate_id
    return model_to_host


def _model_quote_mapping_keys(
    model_ir_provenance: IrProvenance | None,
    model_mappings: list[SpecAuthorityMapping],
    model_candidates: list[SpecAuthorityRequirementCandidate],
    host_candidates: list[RequirementCandidate],
    authority_id_aliases: dict[str, set[str]],
) -> set[tuple[str, str]]:
    """Return host candidate/authority pairs backed by exact model quote hints."""
    model_to_host_candidate_id = _model_to_host_candidate_ids(
        model_ir_provenance,
        model_candidates,
        host_candidates,
    )
    host_candidate_ids = {candidate.candidate_id for candidate in host_candidates}
    host_quote_hash_by_id = {
        candidate.candidate_id: candidate.quote_hash for candidate in host_candidates
    }
    alias_to_current_id = {
        alias: current_id
        for current_id, aliases in authority_id_aliases.items()
        for alias in aliases
    }
    keys: set[tuple[str, str]] = set()
    for mapping in model_mappings:
        if mapping.mapping_provenance != MappingProvenance.MODEL_QUOTE:
            continue
        if mapping.authority_target_kind != AuthorityTargetKind.INVARIANT:
            continue
        host_candidate_id = model_to_host_candidate_id.get(mapping.candidate_id)
        if (
            host_candidate_id is None
            and mapping.candidate_id in host_candidate_ids
        ):
            host_candidate_id = mapping.candidate_id
        if host_candidate_id is None:
            continue
        host_quote_hash = host_quote_hash_by_id[host_candidate_id]
        if mapping.source_quote_hash != host_quote_hash:
            continue
        current_authority_id = alias_to_current_id.get(mapping.authority_item_id)
        if current_authority_id is not None:
            keys.add((host_candidate_id, current_authority_id))
    return keys


def _authority_id_aliases(
    success: SpecAuthorityCompilationSuccess,
    original_invariant_ids: list[str],
) -> dict[str, set[str]]:
    """Map normalized authority IDs to model/legacy IDs that referred to them."""
    aliases: dict[str, set[str]] = {
        invariant.id: {invariant.id} for invariant in success.invariants
    }
    original_id_counts: dict[str, int] = {}
    for original_id in original_invariant_ids:
        original_id_counts[original_id] = original_id_counts.get(original_id, 0) + 1
    for index, invariant in enumerate(success.invariants):
        if index >= len(original_invariant_ids):
            continue
        original_id = original_invariant_ids[index]
        if original_id_counts.get(original_id) != 1:
            continue
        aliases.setdefault(invariant.id, {invariant.id}).add(original_id)
    return aliases


def _mapping_entries_for_compact_ir(  # noqa: PLR0913
    success: SpecAuthorityCompilationSuccess,
    candidates: list[RequirementCandidate],
    *,
    source_format: SpecSourceFormat,
    model_hints: _CompactIrModelHints,
    original_source_map: list[SourceMapEntry],
    original_invariant_ids: list[str],
) -> list[dict[str, str | None]]:
    """Convert normalized source_map entries into shared IR mapping inputs."""
    invariants_by_id = {invariant.id: invariant for invariant in success.invariants}
    model_quote_keys = _model_quote_mapping_keys(
        model_hints.ir_provenance,
        model_hints.mappings,
        model_hints.candidates,
        candidates,
        _authority_id_aliases(success, original_invariant_ids),
    )
    entries: list[dict[str, str | None]] = []
    for entry_index, source_entry in enumerate(success.source_map):
        invariant = invariants_by_id.get(source_entry.invariant_id)
        candidate = _candidate_for_source_entry(source_entry, invariant, candidates)
        if candidate is None:
            continue
        provenance = _source_entry_mapping_provenance(
            source_entry,
            entry_index,
            original_source_map,
            candidate,
            source_format=source_format,
        )
        if (
            candidate.candidate_id,
            source_entry.invariant_id,
        ) in model_quote_keys:
            provenance = MappingProvenance.MODEL_QUOTE
        entries.append(
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": source_entry.invariant_id,
                "authority_target_kind": AuthorityTargetKind.INVARIANT.value,
                "source_quote": candidate.source_quote,
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": provenance.value,
            }
        )
    return entries


def _set_compact_ir(  # noqa: PLR0913
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str | None,
    source_format: SpecSourceFormat,
    original_source_map: list[SourceMapEntry],
    original_invariant_ids: list[str],
    model_hints: _CompactIrModelHints,
) -> None:
    """Populate compact authority IR without treating host parsing as acceptance."""
    if not source_text:
        success.ir_schema_version = None
        success.ir_provenance = IrProvenance.LEGACY_ABSENT
        success.source_units = []
        success.requirement_candidates = []
        success.authority_mappings = []
        success.ir_packet_limits = None
        return

    if source_format == "agileforge.spec.v1":
        profile_ir = _profile_source_units_and_candidates(source_text)
        if profile_ir is None:
            source_units = []
            candidates = []
        else:
            source_units, candidates = profile_ir
    else:
        sections, diagnostics = parse_markdown_sections(source_text)
        if diagnostics:
            logger.info("Spec authority IR parser diagnostics: %s", diagnostics)
        source_units = source_units_from_sections(sections)
        candidates = extract_requirement_candidates(source_units)
    model_host_candidate_ids = set(
        _model_to_host_candidate_ids(
            model_hints.ir_provenance,
            model_hints.candidates,
            candidates,
        ).values()
    )
    candidates = [
        replace(candidate, provenance=IrProvenance.MODEL_EMITTED)
        if candidate.candidate_id in model_host_candidate_ids
        else candidate
        for candidate in candidates
    ]
    mapping_entries = _mapping_entries_for_compact_ir(
        success,
        candidates,
        source_format=source_format,
        model_hints=model_hints,
        original_source_map=original_source_map,
        original_invariant_ids=original_invariant_ids,
    )
    authority_mappings = build_authority_mappings(
        candidates,
        _authority_items(success),
        mapping_entries,
    )
    mapped_model_quotes = any(
        mapping.mapping_provenance == MappingProvenance.MODEL_QUOTE
        for mapping in authority_mappings
    )
    model_candidate_hints = any(
        candidate.provenance == IrProvenance.MODEL_EMITTED for candidate in candidates
    )
    success.ir_schema_version = "authority-ir-v1"
    success.ir_provenance = (
        IrProvenance.MIXED
        if mapped_model_quotes or model_candidate_hints
        else IrProvenance.HOST_PARSED
    )
    success.source_units = [
        SpecAuthoritySourceUnit(
            unit_id=unit.unit_id,
            section_id=unit.section_id,
            heading_path=list(unit.heading_path),
            kind=unit.kind,
            line_start=unit.line_start,
            line_end=unit.line_end,
            text_hash=unit.text_hash,
            text_excerpt=unit.text_excerpt,
            disposition=unit.disposition,
            disposition_reason=unit.disposition_reason,
        )
        for unit in source_units
    ]
    success.requirement_candidates = [
        SpecAuthorityRequirementCandidate(
            candidate_id=candidate.candidate_id,
            source_unit_id=candidate.source_unit_id,
            statement=candidate.statement,
            source_quote=candidate.source_quote,
            quote_hash=candidate.quote_hash,
            line_start=candidate.line_start,
            line_end=candidate.line_end,
            classification=candidate.classification,
            provenance=candidate.provenance,
        )
        for candidate in candidates
    ]
    success.authority_mappings = [
        SpecAuthorityMapping(
            candidate_id=mapping.candidate_id,
            authority_item_id=mapping.authority_item_id,
            authority_target_kind=mapping.authority_target_kind,
            mapping_status=mapping.mapping_status,
            mapping_rationale=mapping.mapping_rationale,
            source_quote_hash=mapping.source_quote_hash,
            mapping_provenance=mapping.mapping_provenance,
        )
        for mapping in authority_mappings
    ]
    success.ir_packet_limits = SpecAuthorityIrPacketLimits(
        max_candidates=len(success.requirement_candidates),
        max_findings=0,
        truncated=False,
    )


def normalize_compiler_output(
    raw_json: str,
    *,
    source_text: str | None = None,
    source_format: SpecSourceFormat | None = None,
) -> SpecAuthorityCompilerOutput:
    """Normalize a raw agent JSON string into a deterministic compiler artifact.

    Args:
        raw_json: Raw JSON string emitted by the agent.
        source_text: Optional source spec text used to repair broad/short source
            excerpts into exact source rows or lines before deterministic ID checks.
        source_format: Optional explicit source format. When omitted, it is detected
            from source_text.

    Returns:
        SpecAuthorityCompilerOutput (success or failure). On success, prompt_hash and
        invariant/source_map IDs are rewritten deterministically.
    """
    logger.info("Normalizing spec authority compiler output")
    source_format = source_format or _detect_source_format(source_text)

    raw_json = _extract_json_candidate(raw_json)

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.error("Spec authority compiler returned invalid JSON: %s", exc)
        return _failure(
            reason="INVALID_JSON",
            blocking_gaps=[str(exc)],
        )

    parsed: SpecAuthorityCompilerOutput | None = None
    validation_gaps: list[str] = []

    try:
        parsed = SpecAuthorityCompilerOutput.model_validate(payload)
        logger.info("Parsed compiler output as SpecAuthorityCompilerOutput")
    except ValidationError as output_exc:
        validation_gaps.append(_summarize_validation_error("output", output_exc))

        if isinstance(payload, dict) and "result" in payload:
            try:
                envelope = SpecAuthorityCompilerEnvelope.model_validate(payload)
                parsed = SpecAuthorityCompilerOutput(root=envelope.result)
                logger.info("Parsed compiler output as SpecAuthorityCompilerEnvelope")
            except ValidationError as envelope_exc:
                validation_gaps.append(
                    _summarize_validation_error("envelope", envelope_exc)
                )
                try:
                    parsed = SpecAuthorityCompilerOutput.model_validate(
                        payload.get("result")
                    )
                    logger.info("Parsed compiler output using envelope.result payload")
                except ValidationError as result_exc:
                    validation_gaps.append(
                        _summarize_validation_error("envelope.result", result_exc)
                    )

    if parsed is None:
        logger.error("Spec authority compiler JSON schema validation failed")
        for gap in validation_gaps:
            logger.error("%s", gap)
        return _failure(
            reason="JSON_VALIDATION_FAILED",
            blocking_gaps=validation_gaps or ["No schema variant matched"],
        )

    if isinstance(parsed.root, SpecAuthorityCompilationFailure):
        logger.error(
            "Spec authority compiler returned failure: %s", parsed.root.model_dump()
        )
        return parsed

    success: SpecAuthorityCompilationSuccess = parsed.root
    model_hints = _CompactIrModelHints(
        ir_provenance=success.ir_provenance,
        candidates=list(success.requirement_candidates),
        mappings=list(success.authority_mappings),
    )

    _filter_meta_policy_invariants(success)
    _deduplicate_semantic_invariants(success)

    expected_prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
    if not success.prompt_hash or not re.match(r"^[0-9a-f]{64}$", success.prompt_hash):
        success.prompt_hash = expected_prompt_hash
    else:
        success.prompt_hash = expected_prompt_hash
    success.compiler_version = SPEC_AUTHORITY_COMPILER_VERSION

    if not success.invariants:
        logger.warning("No invariants extracted from spec authority compiler output")
        if "No invariants extracted from spec" not in success.gaps:
            success.gaps.append("No invariants extracted from spec")
        _set_compact_ir(
            success,
            source_text=source_text,
            source_format=source_format,
            original_source_map=list(success.source_map),
            original_invariant_ids=[],
            model_hints=model_hints,
        )
        return SpecAuthorityCompilerOutput(root=success)

    if not success.source_map:
        logger.error("Spec authority compiler output is missing source_map")
        return _failure(
            reason="MISSING_SOURCE_MAP",
            blocking_gaps=["Missing source_map required for deterministic IDs"],
        )

    original_source_map = list(success.source_map)
    _repair_source_map_from_source_text(
        success,
        source_text=source_text,
        source_format=source_format,
    )

    # Snapshot original invariant IDs/types before rewriting
    original_invariants = list(success.invariants)
    original_invariant_ids = [invariant.id for invariant in success.invariants]
    # Multi-map: an original ID may appear on several invariants with
    # different types or parameters (common LLM behaviour).  A plain dict loses
    # all but the last invariant; we keep them all so source_map rewriting can
    # try each candidate.
    original_id_to_invariants: dict[str, list[Invariant]] = {}
    for _inv in original_invariants:
        original_id_to_invariants.setdefault(_inv.id, []).append(_inv)

    # Check if all invariants have duplicate/placeholder IDs (common LLM behavior)
    original_ids = [inv.id for inv in original_invariants]
    has_duplicate_ids = len(set(original_ids)) < len(original_ids)

    # When IDs are duplicated, prefer positional matching.
    # This is safe when source_map has at least as many entries as invariants
    # (the first N source_map entries align with the N invariants; extras are
    # additional evidence for the same invariants).
    use_positional_matching = has_duplicate_ids and len(success.source_map) >= len(
        success.invariants
    )

    id_to_excerpt: dict[str, str] = {}
    for entry in success.source_map:
        if entry.invariant_id and entry.excerpt and entry.excerpt.strip():
            id_to_excerpt[entry.invariant_id] = entry.excerpt

    def choose_excerpt(invariant_index: int, invariant_id: str) -> str | None:
        # Prefer positional matching when IDs are duplicated
        if use_positional_matching:
            return success.source_map[invariant_index].excerpt
        if invariant_id and invariant_id in id_to_excerpt:
            # Guard against last-wins collision: when multiple invariants
            # share a placeholder ID, id_to_excerpt holds only the last
            # excerpt.  Fall back to positional if the index is in range.
            if has_duplicate_ids and invariant_index < len(success.source_map):
                return success.source_map[invariant_index].excerpt
            return id_to_excerpt[invariant_id]
        if invariant_index < len(success.source_map):
            return success.source_map[invariant_index].excerpt
        if len(success.invariants) == 1 and len(success.source_map) >= 1:
            return success.source_map[0].excerpt
        return None

    # Rewrite invariant IDs deterministically
    for idx, inv in enumerate(success.invariants):
        excerpt = choose_excerpt(idx, inv.id)
        if not excerpt or not excerpt.strip():
            logger.error("Spec authority compiler invariant/source_map mismatch")
            return _failure(
                reason="SOURCE_MAP_INVARIANT_MISMATCH",
                blocking_gaps=[
                    "Cannot choose deterministic excerpt for invariant",
                    f"invariant_index={idx}",
                ],
            )
        support_error = _source_map_support_error(inv, excerpt)
        if support_error is not None:
            logger.error("Spec authority compiler source_map support mismatch")
            return _failure(
                reason="SOURCE_MAP_INVARIANT_MISMATCH",
                blocking_gaps=[support_error],
            )
        inv.id = compute_invariant_id(excerpt, inv.type, inv.parameters)

    normalized_ids = [inv.id for inv in success.invariants]
    if len(set(normalized_ids)) != len(normalized_ids):
        logger.error("Spec authority compiler produced duplicate invariant IDs")
        return _failure(
            reason="DUPLICATE_INVARIANT_IDS",
            blocking_gaps=["Normalized invariant IDs must be unique"],
        )

    # Build the set of already-rewritten invariant IDs so that the
    # source_map loop can disambiguate duplicate-ID / different-type cases.
    normalized_inv_ids = {inv.id for inv in success.invariants}

    # Rewrite source_map invariant_id deterministically
    # use_positional_matching is already computed above
    for entry_index, entry in enumerate(success.source_map):
        excerpt = (entry.excerpt or "").strip()
        if not excerpt:
            logger.error("Spec authority compiler source_map entry has empty excerpt")
            return _failure(
                reason="SOURCE_MAP_INVARIANT_MISMATCH",
                blocking_gaps=["source_map entry has empty excerpt"],
            )

        inv_type = None
        inv_parameters = None

        # Prefer positional matching when IDs are duplicated/placeholder
        if use_positional_matching and entry_index < len(original_invariants):
            matched_invariant = original_invariants[entry_index]
            inv_type = matched_invariant.type
            inv_parameters = matched_invariant.parameters
        elif use_positional_matching:
            # Extra source_map entry beyond invariant count.
            # Try each candidate type and pick the one whose computed ID
            # matches a known (already-rewritten) invariant.
            candidate_invariants = original_id_to_invariants.get(entry.invariant_id, [])
            for candidate in candidate_invariants:
                if _source_map_support_error(candidate, excerpt) is not None:
                    continue
                if (
                    compute_invariant_id(
                        excerpt,
                        candidate.type,
                        candidate.parameters,
                    )
                    in normalized_inv_ids
                ):
                    inv_type = candidate.type
                    inv_parameters = candidate.parameters
                    break
            if inv_type is None and candidate_invariants:
                candidate = candidate_invariants[0]
                inv_type = candidate.type
                inv_parameters = candidate.parameters
            elif inv_type is None:
                inv_type = original_invariants[0].type
                inv_parameters = original_invariants[0].parameters
        else:
            candidate_invariants = original_id_to_invariants.get(entry.invariant_id, [])
            if len(candidate_invariants) == 1:
                inv_type = candidate_invariants[0].type
                inv_parameters = candidate_invariants[0].parameters
            elif len(candidate_invariants) > 1:
                # Multiple invariants share this LLM-generated ID with
                # different types/parameters. Try each candidate and pick the
                # one whose computed ID matches a known invariant.
                for candidate in candidate_invariants:
                    if _source_map_support_error(candidate, excerpt) is not None:
                        continue
                    if (
                        compute_invariant_id(
                            excerpt,
                            candidate.type,
                            candidate.parameters,
                        )
                        in normalized_inv_ids
                    ):
                        inv_type = candidate.type
                        inv_parameters = candidate.parameters
                        break
                if inv_type is None:
                    # Fallback: use the first candidate invariant
                    inv_type = candidate_invariants[0].type
                    inv_parameters = candidate_invariants[0].parameters
            # No matching invariant for this source_map entry
            elif len(success.invariants) == 1:
                inv_type = success.invariants[0].type
                inv_parameters = success.invariants[0].parameters
            elif len(success.source_map) == len(original_invariants):
                inv_type = original_invariants[entry_index].type
                inv_parameters = original_invariants[entry_index].parameters
            else:
                logger.error("Cannot match source_map entry to invariant type")
                return _failure(
                    reason="SOURCE_MAP_INVARIANT_MISMATCH",
                    blocking_gaps=[
                        "Cannot match source_map entry to an invariant type",
                        f"source_map_index={entry_index}",
                    ],
                )

        if inv_parameters is None:
            candidate = original_invariants[min(entry_index, len(original_invariants) - 1)]
            inv_parameters = candidate.parameters
        entry.invariant_id = compute_invariant_id(excerpt, inv_type, inv_parameters)

    # Verify auditability: every invariant has at least one source_map entry
    normalized_ids = {inv.id for inv in success.invariants}
    source_map_ids = {entry.invariant_id for entry in success.source_map}
    missing = sorted(normalized_ids - source_map_ids)
    if missing:
        logger.warning(
            "Spec authority compiler output has %s invariant(s) without source_map coverage: %s",
            len(missing),
            missing,
        )

    _set_compact_ir(
        success,
        source_text=source_text,
        source_format=source_format,
        original_source_map=original_source_map,
        original_invariant_ids=original_invariant_ids,
        model_hints=model_hints,
    )

    return SpecAuthorityCompilerOutput(root=success)
