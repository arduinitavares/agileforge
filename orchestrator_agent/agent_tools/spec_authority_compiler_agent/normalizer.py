"""Host-side normalizer/validator for spec_authority_compiler_agent output.

This enforces compiler semantics on the host side:
- prompt_hash is anchored to SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
- invariant IDs are deterministically computed from invariant.type and
  invariant.parameters

The caller MUST use the normalized output downstream.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from orchestrator_agent.agent_tools.spec_authority_compiler_agent.compiler_contract import (
    compute_invariant_id_from_payload,
    compute_prompt_hash,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerOutput,
)

logger: logging.Logger = logging.getLogger(name=__name__)

SpecSourceFormat = Literal["agileforge.spec.v1", "plain_text"]

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
_STRUCTURED_SOURCE_EXACT_LOCATION_PRIORITY = 3
_STRUCTURED_ENTRY_EXCERPT_MATCH_PRIORITY = 4
_STRUCTURED_ENTRY_LOCATION_MATCH_PRIORITY = 5
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
_SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP = frozenset(
    {
        "scope_themes",
        "invariants",
        "eligible_feature_rules",
        "gaps",
        "assumptions",
        "compiler_version",
        "prompt_hash",
    }
)
_DEPRECATED_COMPACT_IR_KEYS = frozenset(
    {
        "ir_schema_version",
        "ir_provenance",
        "source_units",
        "requirement_candidates",
        "authority_mappings",
        "ir_packet_limits",
    }
)


@dataclass(frozen=True)
class _SourceEvidenceCandidate:
    """Candidate source evidence for an invariant."""

    excerpt: str
    location: str | None
    priority: int = 0


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
        return "plain_text"
    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return "plain_text"
    if (
        isinstance(parsed, dict)
        and parsed.get("schema_version") == "agileforge.spec.v1"
    ):
        return "agileforge.spec.v1"
    return "plain_text"


def _default_missing_source_map_for_success_payload(payload: object) -> None:
    """Default omitted source_map only for otherwise success-shaped payloads."""
    if not isinstance(payload, dict):
        return

    result = payload.get("result")
    if isinstance(result, dict):
        _default_missing_source_map_for_success_payload(result)

    if "source_map" in payload or "error" in payload:
        return
    if _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP.issubset(payload):
        payload["source_map"] = []


def _drop_deprecated_compact_ir_for_success_payload(payload: object) -> None:
    """Drop legacy compact IR before validating success-shaped payloads."""
    if not isinstance(payload, dict):
        return

    result = payload.get("result")
    if isinstance(result, dict):
        _drop_deprecated_compact_ir_for_success_payload(result)

    if "error" in payload:
        return
    if not _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP.issubset(payload):
        return

    for key in _DEPRECATED_COMPACT_IR_KEYS:
        payload.pop(key, None)


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
                    priority=1,
                )
            )
    return candidates


def _structured_profile_source_candidates(
    source_text: str,
    *,
    location_hint: str | None = None,
) -> list[_SourceEvidenceCandidate]:
    """Return item-field evidence candidates from canonical profile JSON."""
    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return []
    if (
        not isinstance(parsed, dict)
        or parsed.get("schema_version") != "agileforge.spec.v1"
    ):
        return []

    items = parsed.get("items")
    if not isinstance(items, list):
        return []

    candidates: list[_SourceEvidenceCandidate] = []
    seen: set[tuple[str, str | None]] = set()
    normalized_hint = (location_hint or "").strip()

    def append(excerpt: object, location: str | None) -> None:
        if not isinstance(excerpt, str):
            return
        compact = _compact_whitespace(excerpt)
        if not compact:
            return
        key = (compact, location)
        if key in seen:
            return
        seen.add(key)
        priority = 3 if normalized_hint and normalized_hint == location else 2
        candidates.append(
            _SourceEvidenceCandidate(
                excerpt=compact,
                location=location,
                priority=priority,
            )
        )

    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue

        for field_name in ("statement", "title", "rationale"):
            append(item.get(field_name), f"{item_id}.{field_name}")

        acceptance_items = item.get("acceptance")
        if isinstance(acceptance_items, list):
            for index, acceptance in enumerate(acceptance_items):
                append(acceptance, f"{item_id}.acceptance[{index}]")

        source_notes = item.get("source_notes")
        if isinstance(source_notes, list):
            for index, source_note in enumerate(source_notes):
                append(source_note, f"{item_id}.source_notes[{index}]")

    return candidates


def _candidate_evidence_from_source_text(
    entry: SourceMapEntry,
    *,
    source_text: str | None,
) -> list[_SourceEvidenceCandidate]:
    """Build deduplicated evidence candidates from LLM source_map plus source text."""
    candidates: list[_SourceEvidenceCandidate] = []
    seen: set[tuple[str, str | None, int]] = set()

    def append(excerpt: str, location: str | None, *, priority: int = 0) -> None:
        compact = _compact_whitespace(excerpt)
        if not compact:
            return
        key = (compact, location, priority)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            _SourceEvidenceCandidate(
                excerpt=compact,
                location=location,
                priority=priority,
            )
        )

    append(entry.excerpt, entry.location)
    if not source_text:
        return candidates

    location_text = entry.location or ""
    combined_hint = f"{location_text}\n{entry.excerpt}"
    for fr_id in _fr_ids_from_text(combined_hint):
        for line in _source_text_lines_for_fr(source_text, fr_id):
            append(line, entry.location or fr_id, priority=1)
    for line in _source_text_lines_containing(source_text, entry.excerpt):
        append(line, entry.location, priority=1)
    return candidates


def _entry_invariant_for_source_map(
    success: SpecAuthorityCompilationSuccess,
    entry: SourceMapEntry,
    entry_index: int,
    *,
    evidence_candidates: list[_SourceEvidenceCandidate] | None = None,
) -> Invariant | None:
    """Return the invariant most likely referenced by a source_map entry."""
    matching_invariants = [
        invariant
        for invariant in success.invariants
        if invariant.id == entry.invariant_id
    ]
    if len(matching_invariants) == 1:
        return matching_invariants[0]
    if evidence_candidates:
        supported_match = _support_matched_source_map_invariant(
            matching_invariants or success.invariants,
            evidence_candidates,
        )
        if supported_match is not None:
            return supported_match
    if entry_index < len(success.invariants):
        return success.invariants[entry_index]
    return None


def _support_matched_source_map_invariant(
    invariants: list[Invariant],
    evidence_candidates: list[_SourceEvidenceCandidate],
) -> Invariant | None:
    """Return a unique invariant match from entry-local evidence, if clear."""
    scored: list[tuple[Invariant, tuple[int, float]]] = []
    for invariant in invariants:
        matched = _best_supported_source_candidate(invariant, evidence_candidates)
        if matched is None:
            continue
        scored.append(
            (
                invariant,
                (
                    matched.priority,
                    _source_map_support_score(invariant, matched.excerpt),
                ),
            )
        )
    if not scored:
        return None

    best_score = max(score for _, score in scored)
    best_matches = [
        invariant for invariant, score in scored if score == best_score
    ]
    if len(best_matches) != 1:
        return None
    return best_matches[0]


def _best_supported_source_candidate(
    invariant: Invariant,
    candidates: list[_SourceEvidenceCandidate],
) -> _SourceEvidenceCandidate | None:
    """Select the most specific source-text candidate supporting an invariant."""
    supported = [
        candidate
        for candidate in candidates
        if _source_map_support_error(invariant, candidate.excerpt) is None
    ]
    if not supported:
        return None
    return max(
        supported,
        key=lambda candidate: (
            candidate.priority,
            _source_map_support_score(invariant, candidate.excerpt),
            -len(candidate.excerpt),
            candidate.location or "",
            candidate.excerpt,
        ),
    )


def _structured_entry_match_candidates(
    entry: SourceMapEntry,
    *,
    source_text: str,
) -> list[_SourceEvidenceCandidate]:
    """Return entry-local evidence used to disambiguate duplicate IDs."""
    candidates: list[_SourceEvidenceCandidate] = []
    compact_excerpt = _compact_whitespace(entry.excerpt)
    if compact_excerpt:
        candidates.append(
            _SourceEvidenceCandidate(
                excerpt=compact_excerpt,
                location=entry.location,
                priority=_STRUCTURED_ENTRY_EXCERPT_MATCH_PRIORITY,
            )
        )

    candidates.extend(
        _SourceEvidenceCandidate(
            excerpt=candidate.excerpt,
            location=candidate.location,
            priority=_STRUCTURED_ENTRY_LOCATION_MATCH_PRIORITY,
        )
        for candidate in _structured_profile_source_candidates(
            source_text,
            location_hint=entry.location,
        )
        if candidate.priority == _STRUCTURED_SOURCE_EXACT_LOCATION_PRIORITY
    )
    return candidates


def _repair_structured_source_map_from_source_text(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> bool:
    """Repair profile JSON source_map excerpts without rejecting weak evidence."""
    repaired: list[SourceMapEntry] = []
    changed = False

    for index, entry in enumerate(success.source_map):
        candidates = _candidate_evidence_from_source_text(entry, source_text=source_text)
        candidates.extend(
            _structured_profile_source_candidates(
                source_text,
                location_hint=entry.location,
            )
        )
        candidates.extend(_source_text_line_candidates(source_text))
        invariant = _entry_invariant_for_source_map(
            success,
            entry,
            index,
            evidence_candidates=_structured_entry_match_candidates(
                entry,
                source_text=source_text,
            ),
        )
        if invariant is None:
            repaired.append(entry)
            continue

        matched = _best_supported_source_candidate(invariant, candidates)
        if matched is None:
            repaired.append(entry)
            continue

        repaired_entry = SourceMapEntry(
            invariant_id=entry.invariant_id,
            excerpt=matched.excerpt,
            location=matched.location,
        )
        repaired.append(repaired_entry)
        changed = changed or repaired_entry != entry

    success.source_map = repaired
    return changed


def _repair_source_map_from_source_text(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str | None,
    source_format: SpecSourceFormat,
) -> bool:
    """Repair weak LLM source maps from the current source text."""
    if not source_text:
        return False
    if source_format == "agileforge.spec.v1":
        return _repair_structured_source_map_from_source_text(
            success,
            source_text=source_text,
        )

    evidence_candidates: list[_SourceEvidenceCandidate] = []
    for entry in success.source_map:
        evidence_candidates.extend(
            _candidate_evidence_from_source_text(entry, source_text=source_text)
        )
    evidence_candidates.extend(_source_text_line_candidates(source_text))

    if not evidence_candidates:
        return False

    original_source_map = list(success.source_map)
    original_invariants = list(success.invariants)
    original_id_counts: dict[str, int] = {}
    for invariant in original_invariants:
        original_id_counts[invariant.id] = original_id_counts.get(invariant.id, 0) + 1

    repaired_by_entry_index: dict[int, SourceMapEntry] = {}
    appended_repaired: list[SourceMapEntry] = []
    retained_invariants: list[Invariant] = []
    retained_invariant_indexes: set[int] = set()
    dropped_invariants: list[Invariant] = []
    used_entry_indexes: set[int] = set()

    def primary_entry_index_for_invariant(invariant: Invariant, index: int) -> int | None:
        for entry_index, entry in enumerate(original_source_map):
            if entry_index in used_entry_indexes:
                continue
            if entry.invariant_id == invariant.id:
                return entry_index
        if index < len(original_source_map) and index not in used_entry_indexes:
            return index
        return None

    for inv_index, inv in enumerate(success.invariants):
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
        retained_invariant_indexes.add(inv_index)
        repaired_entry = SourceMapEntry(
            invariant_id=inv.id,
            excerpt=matched.excerpt,
            location=matched.location,
        )
        entry_index = primary_entry_index_for_invariant(inv, inv_index)
        if entry_index is None:
            appended_repaired.append(repaired_entry)
        else:
            used_entry_indexes.add(entry_index)
            repaired_by_entry_index[entry_index] = repaired_entry

    if not retained_invariants:
        return False
    if dropped_invariants:
        success.invariants = retained_invariants
        for dropped in dropped_invariants:
            gap = f"Dropped unsupported compiler invariant: {_invariant_text(dropped)}"
            if gap not in success.gaps:
                success.gaps.append(gap)

    retained_id_counts: dict[str, int] = {}
    for invariant in retained_invariants:
        retained_id_counts[invariant.id] = retained_id_counts.get(invariant.id, 0) + 1

    def preserve_original_entry(entry_index: int, entry: SourceMapEntry) -> bool:
        original_id_count = original_id_counts.get(entry.invariant_id, 0)
        if original_id_count == 1:
            return entry.invariant_id in retained_id_counts
        if entry_index < len(original_invariants):
            return entry_index in retained_invariant_indexes
        return retained_id_counts.get(entry.invariant_id, 0) == original_id_count

    repaired_source_map: list[SourceMapEntry] = []
    for entry_index, entry in enumerate(original_source_map):
        if entry_index in repaired_by_entry_index:
            repaired_source_map.append(repaired_by_entry_index[entry_index])
        elif preserve_original_entry(entry_index, entry):
            repaired_source_map.append(entry)
    repaired_source_map.extend(appended_repaired)
    success.source_map = repaired_source_map
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


def _clear_compact_ir(success: SpecAuthorityCompilationSuccess) -> None:
    """Clear legacy compact IR fields; structured authority has no host semantic IR."""
    success.ir_schema_version = None
    success.ir_provenance = None
    success.source_units = []
    success.requirement_candidates = []
    success.authority_mappings = []
    success.ir_packet_limits = None


def _rewrite_source_map_invariant_ids(
    success: SpecAuthorityCompilationSuccess,
    original_invariants: list[Invariant],
) -> None:
    """Rewrite source-map IDs without collapsing duplicate original placeholders."""
    normalized_ids = {inv.id for inv in success.invariants}
    original_id_counts: dict[str, int] = {}
    for original in original_invariants:
        original_id_counts[original.id] = original_id_counts.get(original.id, 0) + 1

    original_id_to_new_id: dict[str, str] = {}
    for original, normalized in zip(
        original_invariants,
        success.invariants,
        strict=False,
    ):
        if original_id_counts[original.id] == 1:
            original_id_to_new_id[original.id] = normalized.id

    def positional_normalized_id(index: int) -> str | None:
        if index < len(success.invariants):
            return success.invariants[index].id
        if success.invariants:
            return success.invariants[index % len(success.invariants)].id
        return None

    def support_matched_normalized_id(
        entry: SourceMapEntry,
        positional_id: str | None,
    ) -> str | None:
        scored: list[tuple[Invariant, float]] = []
        positional_score: float | None = None
        for invariant in success.invariants:
            if _source_map_support_error(invariant, entry.excerpt) is not None:
                continue
            score = _source_map_support_score(invariant, entry.excerpt)
            scored.append((invariant, score))
            if invariant.id == positional_id:
                positional_score = score

        if not scored:
            return None

        best_score = max(score for _, score in scored)
        best_matches = [
            invariant for invariant, score in scored if score == best_score
        ]
        if positional_score is not None and positional_score >= best_score:
            return None
        if len(best_matches) != 1:
            return None
        return best_matches[0].id

    def fallback_normalized_id(entry: SourceMapEntry, index: int) -> str | None:
        positional_id = positional_normalized_id(index)
        supported_id = support_matched_normalized_id(entry, positional_id)
        if supported_id is not None:
            return supported_id
        return positional_id

    for index, entry in enumerate(success.source_map):
        original_id_count = original_id_counts.get(entry.invariant_id, 0)
        if entry.invariant_id in normalized_ids:
            continue
        if original_id_count > 1:
            fallback_id = fallback_normalized_id(entry, index)
            if fallback_id is not None:
                entry.invariant_id = fallback_id
        elif entry.invariant_id in original_id_to_new_id:
            entry.invariant_id = original_id_to_new_id[entry.invariant_id]
        else:
            fallback_id = fallback_normalized_id(entry, index)
            if fallback_id is not None:
                entry.invariant_id = fallback_id

    success.source_map = [
        entry for entry in success.source_map if entry.invariant_id in normalized_ids
    ]


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
    _drop_deprecated_compact_ir_for_success_payload(payload)
    _default_missing_source_map_for_success_payload(payload)

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
        _clear_compact_ir(success)
        return SpecAuthorityCompilerOutput(root=success)

    original_source_map = list(success.source_map)
    if success.source_map:
        repaired_source_map = _repair_source_map_from_source_text(
            success,
            source_text=source_text,
            source_format=source_format,
        )
        if repaired_source_map and original_source_map != success.source_map:
            logger.info("Repaired source_map entries from source text")

    original_invariants = [
        invariant.model_copy(deep=True) for invariant in success.invariants
    ]

    for inv in success.invariants:
        inv.id = compute_invariant_id_from_payload(inv.type, inv.parameters)

    normalized_ids = [inv.id for inv in success.invariants]
    if len(set(normalized_ids)) != len(normalized_ids):
        logger.error("Spec authority compiler produced duplicate invariant IDs")
        return _failure(
            reason="DUPLICATE_INVARIANT_IDS",
            blocking_gaps=["Normalized invariant IDs must be unique"],
        )

    if success.source_map:
        _rewrite_source_map_invariant_ids(success, original_invariants)

    _clear_compact_ir(success)

    return SpecAuthorityCompilerOutput(root=success)
