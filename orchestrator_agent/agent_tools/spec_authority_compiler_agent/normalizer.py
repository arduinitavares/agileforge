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
    if source_format == "agileforge.spec.v1":
        return False

    evidence_candidates: list[_SourceEvidenceCandidate] = []
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


def _clear_compact_ir(success: SpecAuthorityCompilationSuccess) -> None:
    """Clear legacy compact IR fields; structured authority has no host semantic IR."""
    success.ir_schema_version = None
    success.ir_provenance = None
    success.source_units = []
    success.requirement_candidates = []
    success.authority_mappings = []
    success.ir_packet_limits = None


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

    normalized_ids = {inv.id for inv in success.invariants}
    if success.source_map:
        original_id_to_new_id: dict[str, str] = {}
        for original, normalized in zip(
            original_invariants,
            success.invariants,
            strict=False,
        ):
            original_id_to_new_id[original.id] = normalized.id
        for index, entry in enumerate(success.source_map):
            if entry.invariant_id in original_id_to_new_id:
                entry.invariant_id = original_id_to_new_id[entry.invariant_id]
            elif index < len(success.invariants):
                entry.invariant_id = success.invariants[index].id
        success.source_map = [
            entry for entry in success.source_map if entry.invariant_id in normalized_ids
        ]

    _clear_compact_ir(success)

    return SpecAuthorityCompilerOutput(root=success)
