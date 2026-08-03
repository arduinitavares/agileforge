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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard, cast

from pydantic import ValidationError

from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
    compute_invariant_id_from_payload,
)
from utils.agileforge_spec_profile import TechnicalSpecArtifact
from utils.spec_authority_assumptions import (
    FreeTextAssumption,
    GroundingFailure,
    canonical_assumption_key,
    free_text_requires_typed_claim,
    ground_assumption,
    is_structured_assumption,
)
from utils.spec_schemas import (
    AuthorityQualityMergedItem,
    AuthorityQualityReport,
    AuthorityQualitySummary,
    Invariant,
    InvariantParameters,
    InvariantType,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerOutput,
)

logger: logging.Logger = logging.getLogger(name=__name__)

SpecSourceFormat = Literal["agileforge.spec.v1", "plain_text"]


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    """Narrow a runtime dictionary to string keys and object values."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


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

_META_POLICY_ASSUMPTION = FreeTextAssumption(
    kind="free_text",
    text="Excluded non-product policy/admin excerpts from compiled invariants.",
)
_DUPLICATE_INVARIANT_ASSUMPTION = FreeTextAssumption(
    kind="free_text",
    text=(
        "Removed duplicate compiled invariant entries with identical type and "
        "parameters."
    ),
)
_NON_NORMATIVE_SOURCE_ASSUMPTION = FreeTextAssumption(
    kind="free_text",
    text="Excluded non-normative source item from hard forbidden authority.",
)
_FIELD_SUPPORT_RATIO_THRESHOLD = 1.0
_RELATION_SUPPORT_RATIO_THRESHOLD = 0.75
_SUPPORT_RATIO_THRESHOLD = 0.5
_FORBIDDEN_SAFETY_SUPPORT_THRESHOLD = 0.25
_STRUCTURED_SOURCE_EXACT_LOCATION_PRIORITY = 3
_STRUCTURED_ENTRY_EXCERPT_MATCH_PRIORITY = 4
_STRUCTURED_ENTRY_LOCATION_MATCH_PRIORITY = 5
_STRUCTURED_EVIDENCE_MIN_CONCAT_SEGMENTS = 2
_STRUCTURED_FRAGMENT_MAX_TOKEN_GAP = 2
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
_STRUCTURED_ITEM_ID_RE = re.compile(
    r"\b(?:GOAL|NON_GOAL|REQ|QUALITY|CONSTRAINT|INTERFACE|DATA|DECISION|"
    r"ASSUMPTION|RISK|EXAMPLE|OPEN_QUESTION)\.[A-Za-z0-9_-]+"
)
_STRUCTURED_ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…)")
_STRUCTURED_EVIDENCE_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STRUCTURED_EVIDENCE_GLUE_CHARS = frozenset(" \t\n\r\f\v.,;:!?()[]{}<>\"'`-")
_STRICT_INVARIANT_ID_RE = re.compile(r"^INV-[0-9a-f]{16}$")
_PROMPT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
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
_SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP_AND_PROMPT_HASH = (
    _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP - {"prompt_hash"}
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
_BEHAVIORAL_INVARIANT_TYPES = frozenset(
    {
        InvariantType.USER_INTERACTION,
        InvariantType.STATE_TRANSITION,
        InvariantType.DATA_CONTRACT,
        InvariantType.ROUTE_CONTRACT,
        InvariantType.VISIBILITY_RULE,
    }
)
BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED = "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED"
LEGACY_MODALITY_PROMOTION = "LEGACY_MODALITY_PROMOTION"
EXAMPLE_ONLY_SOURCE_EVIDENCE = "EXAMPLE_ONLY_SOURCE_EVIDENCE"
UNKNOWN_SOURCE_ITEM = "UNKNOWN_SOURCE_ITEM"
MISSING_SOURCE_ITEM_ID = "MISSING_SOURCE_ITEM_ID"
SOURCE_LEVEL_MISMATCH = "SOURCE_LEVEL_MISMATCH"


@dataclass(frozen=True)
class _SourceEvidenceCandidate:
    """Candidate source evidence for an invariant."""

    excerpt: str
    location: str | None
    priority: int = 0


def _failure(
    reason: str,
    blocking_gaps: list[str],
    *,
    source_metadata_issues: list[dict[str, object]] | None = None,
) -> SpecAuthorityCompilerOutput:
    return SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationFailure(
            error="SPEC_COMPILATION_FAILED",
            reason=reason,
            blocking_gaps=blocking_gaps,
            source_metadata_issues=source_metadata_issues,
        )
    )


def _append_host_assumption(
    success: SpecAuthorityCompilationSuccess,
    assumption: FreeTextAssumption,
) -> None:
    """Append a host conclusion only when its semantic identity is new."""
    existing = {canonical_assumption_key(item) for item in success.assumptions}
    if canonical_assumption_key(assumption) not in existing:
        success.assumptions.append(assumption)


def _claim_like_assumption_failure(
    exc: ValidationError,
) -> SpecAuthorityCompilerOutput | None:
    """Map the finite free-text cue boundary to actionable retry feedback."""
    for error in exc.errors(include_url=False):
        if error["type"] != "assumption_claim_requires_typed_form":
            continue
        location = ".".join(str(part) for part in error["loc"])
        return _failure(
            reason="ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM",
            blocking_gaps=[
                f"{location}: use item_status, accepted_normative_count, "
                "or accepted_normative_set"
            ],
        )
    return None


def _claim_like_free_text_payload_failure(
    payload: object,
) -> SpecAuthorityCompilerOutput | None:
    """Reject raw string claims before union parsing erases their cue error."""
    if not isinstance(payload, dict):
        return None
    payload_dict = cast("dict[str, object]", payload)
    candidate = payload_dict.get("result", payload_dict)
    if not isinstance(candidate, dict):
        return None
    candidate_dict = cast("dict[str, object]", candidate)
    assumptions = candidate_dict.get("assumptions")
    if not isinstance(assumptions, list):
        return None
    for index, assumption in enumerate(assumptions):
        if not isinstance(assumption, str) or not free_text_requires_typed_claim(
            assumption
        ):
            continue
        return _failure(
            reason="ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM",
            blocking_gaps=[
                f"assumptions.{index}.free_text.text: use item_status, "
                "accepted_normative_count, or accepted_normative_set"
            ],
        )
    return None


def _grounding_failure_output(
    *,
    index: int,
    failure: GroundingFailure,
) -> SpecAuthorityCompilerOutput:
    """Return a stable, non-retryable structured-claim grounding failure."""
    return _failure(
        reason=failure.reason,
        blocking_gaps=[
            f"assumptions.{index - 1}: {failure.claim_kind} does not match "
            "the canonical structured spec"
        ],
    )


def _validated_success_output(
    success: SpecAuthorityCompilationSuccess,
) -> SpecAuthorityCompilerOutput:
    """Revalidate every success after host normalization mutations."""
    validated = SpecAuthorityCompilationSuccess.model_validate(
        success.model_dump(mode="json")
    )
    return SpecAuthorityCompilerOutput(root=validated)


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
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return text
        return text[start : end + 1].strip()
    else:
        return text


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

    payload_dict = cast("dict[str, Any]", payload)
    result = payload_dict.get("result")
    if isinstance(result, dict):
        _default_missing_source_map_for_success_payload(result)

    if "source_map" in payload_dict or "error" in payload_dict:
        return
    if _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP.issubset(payload_dict):
        payload_dict["source_map"] = []


def _drop_deprecated_compact_ir_for_success_payload(payload: object) -> None:
    """Drop legacy compact IR before validating success-shaped payloads."""
    if not isinstance(payload, dict):
        return

    payload_dict = cast("dict[str, Any]", payload)
    result = payload_dict.get("result")
    if isinstance(result, dict):
        _drop_deprecated_compact_ir_for_success_payload(result)

    if "error" in payload_dict:
        return
    if not _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP.issubset(payload_dict):
        return

    for key in _DEPRECATED_COMPACT_IR_KEYS:
        payload_dict.pop(key, None)


def _repair_invalid_prompt_hash_for_validation(payload: object) -> None:
    """Repair invalid prompt_hash before strict success schema validation."""
    if not isinstance(payload, dict):
        return

    payload_dict = cast("dict[str, Any]", payload)
    result = payload_dict.get("result")
    if isinstance(result, dict):
        _repair_invalid_prompt_hash_for_validation(result)

    if "error" in payload_dict:
        return
    if not _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP_AND_PROMPT_HASH.issubset(
        payload_dict
    ):
        return

    prompt_hash = payload_dict.get("prompt_hash")
    if isinstance(prompt_hash, str) and _PROMPT_HASH_RE.fullmatch(prompt_hash):
        return

    payload_dict["prompt_hash"] = SPEC_AUTHORITY_COMPILER_PROMPT_HASH


def _repair_invariant_param_provenance(item: object) -> int:
    """Move provenance from one invariant's parameters to its top level."""
    if not _is_string_object_dict(item):
        return 0
    parameters = item.get("parameters")
    if not _is_string_object_dict(parameters):
        return 0

    repaired_count = 0
    for field_name in ("source_item_id", "source_level"):
        value = parameters.pop(field_name, None)
        if value is not None and item.get(field_name) is None:
            item[field_name] = value
            repaired_count += 1
    return repaired_count


def _repair_param_level_provenance_for_validation(payload: object) -> None:
    """Move misplaced provenance into invariant top-level fields before validation."""
    if not isinstance(payload, dict):
        return

    payload_dict = cast("dict[str, Any]", payload)
    result = payload_dict.get("result")
    if isinstance(result, dict):
        _repair_param_level_provenance_for_validation(result)

    if "error" in payload_dict:
        return
    if not _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP.issubset(payload_dict):
        return

    invariants = payload_dict.get("invariants")
    if not isinstance(invariants, list):
        return

    repaired_count = sum(
        _repair_invariant_param_provenance(item) for item in invariants
    )

    if repaired_count:
        logger.info(
            "Repaired %s misplaced compiler provenance field(s) before validation",
            repaired_count,
        )


def _temporary_invariant_id(index: int) -> str:
    """Return a schema-valid temporary ID used only before semantic rewrite."""
    return f"INV-{index + 1:016x}"


def _is_valid_invariant_id(value: object) -> bool:
    """Return whether a raw value is a schema-valid invariant ID."""
    return isinstance(value, str) and bool(_STRICT_INVARIANT_ID_RE.fullmatch(value))


def _next_temporary_invariant_id(
    index: int,
    *,
    invariant_count: int,
    used_ids: set[str],
) -> str:
    """Return an unused schema-valid temporary invariant ID."""
    replacement = _temporary_invariant_id(index)
    while replacement in used_ids:
        index += invariant_count + 1
        replacement = _temporary_invariant_id(index)
    return replacement


def _repair_invalid_invariant_ids_for_validation(payload: object) -> None:
    """Repair invalid LLM IDs before strict schema validation.

    The compiler contract is deterministic host-side IDs. Some model outputs use
    placeholders such as `INV-xxxxxxxxxxxxxxxx`, which are semantically harmless
    but fail schema validation before the deterministic rewrite can run.
    """
    if not isinstance(payload, dict):
        return

    payload_dict = cast("dict[str, Any]", payload)
    result = payload_dict.get("result")
    if isinstance(result, dict):
        _repair_invalid_invariant_ids_for_validation(result)

    if "error" in payload_dict:
        return
    if not _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP.issubset(payload_dict):
        return

    invariants = payload_dict.get("invariants")
    if not isinstance(invariants, list):
        return

    used_ids = {
        str(item.get("id"))
        for item in invariants
        if isinstance(item, dict) and _is_valid_invariant_id(item.get("id"))
    }
    repaired_count = 0
    for index, item in enumerate(invariants):
        if not isinstance(item, dict):
            continue
        item_dict = cast("dict[str, Any]", item)
        if _is_valid_invariant_id(item_dict.get("id")):
            continue
        replacement = _next_temporary_invariant_id(
            index,
            invariant_count=len(invariants),
            used_ids=used_ids,
        )
        item_dict["id"] = replacement
        used_ids.add(replacement)
        repaired_count += 1

    if repaired_count:
        logger.info(
            "Repaired %s invalid compiler invariant IDs before validation",
            repaired_count,
        )


def _is_meta_policy_source(location: str | None, excerpt: str) -> bool:
    location_text = (location or "").strip()
    excerpt_text = (excerpt or "").strip()
    if location_text and _META_POLICY_LOCATION_RE.search(location_text):
        return True
    return any(
        pattern.search(excerpt_text) for pattern in _META_POLICY_EXCERPT_PATTERNS
    )


def _meta_policy_source_matches(
    success: SpecAuthorityCompilationSuccess,
) -> tuple[dict[int, list[int]], set[int], bool]:
    """Match invariants to source-map entry indexes before policy filtering."""
    indexes_by_id: dict[str, list[int]] = {}
    for entry_index, entry in enumerate(success.source_map):
        indexes_by_id.setdefault(entry.invariant_id, []).append(entry_index)

    original_ids = [invariant.id for invariant in success.invariants]
    use_position = len(set(original_ids)) < len(original_ids) and len(
        success.source_map
    ) >= len(success.invariants)
    matched: dict[int, list[int]] = {}
    matched_source_indexes: set[int] = set()
    same_length = len(success.source_map) == len(success.invariants)
    for invariant_index, invariant in enumerate(success.invariants):
        if use_position and invariant_index < len(success.source_map):
            entry_indexes = [invariant_index]
        else:
            entry_indexes = list(indexes_by_id.get(invariant.id, []))
            if (
                not entry_indexes
                and same_length
                and invariant_index < len(success.source_map)
            ):
                entry_indexes = [invariant_index]
        if entry_indexes:
            matched[invariant_index] = entry_indexes
            matched_source_indexes.update(entry_indexes)
    return matched, matched_source_indexes, use_position


def _partition_meta_policy_invariants(
    success: SpecAuthorityCompilationSuccess,
    matched_entry_indexes: Mapping[int, list[int]],
) -> tuple[list[Invariant], set[int], int]:
    """Partition product invariants from policy-only invariants."""
    kept: list[Invariant] = []
    kept_source_indexes: set[int] = set()
    filtered_count = 0
    for invariant_index, invariant in enumerate(success.invariants):
        entry_indexes = matched_entry_indexes.get(invariant_index, [])
        matched_entries = [success.source_map[index] for index in entry_indexes]
        if matched_entries and all(
            _is_meta_policy_source(entry.location, entry.excerpt)
            for entry in matched_entries
        ):
            filtered_count += 1
            continue
        kept.append(invariant)
        kept_source_indexes.update(
            index
            for index in entry_indexes
            if not _is_meta_policy_source(
                success.source_map[index].location,
                success.source_map[index].excerpt,
            )
        )
    return kept, kept_source_indexes, filtered_count


def _filtered_meta_policy_source_map(
    success: SpecAuthorityCompilationSuccess,
    *,
    kept_invariants: list[Invariant],
    kept_source_indexes: set[int],
    matched_source_indexes: set[int],
    use_positional_matching: bool,
) -> list[SourceMapEntry]:
    """Return source-map entries that remain after policy filtering."""
    kept_ids = {invariant.id for invariant in kept_invariants}
    kept_entries: list[SourceMapEntry] = []
    for entry_index, entry in enumerate(success.source_map):
        is_unmatched_kept_entry = (
            not use_positional_matching
            and entry_index not in matched_source_indexes
            and entry.invariant_id in kept_ids
        )
        if not _is_meta_policy_source(entry.location, entry.excerpt) and (
            entry_index in kept_source_indexes or is_unmatched_kept_entry
        ):
            kept_entries.append(entry)
    return kept_entries


def _filter_meta_policy_invariants(success: SpecAuthorityCompilationSuccess) -> int:
    """Remove invariants sourced only from non-product policy/admin excerpts."""
    if not success.invariants or not success.source_map:
        return 0

    matched, matched_source_indexes, use_position = _meta_policy_source_matches(success)
    kept_invariants, kept_source_indexes, filtered_count = (
        _partition_meta_policy_invariants(success, matched)
    )

    if not filtered_count:
        return 0

    success.invariants = kept_invariants
    success.source_map = _filtered_meta_policy_source_map(
        success,
        kept_invariants=kept_invariants,
        kept_source_indexes=kept_source_indexes,
        matched_source_indexes=matched_source_indexes,
        use_positional_matching=use_position,
    )
    _append_host_assumption(success, _META_POLICY_ASSUMPTION)
    if not success.invariants:
        gap = (
            "No invariants extracted from spec after excluding non-product "
            "policy/admin excerpts"
        )
        if gap not in success.gaps:
            success.gaps.append(gap)

    logger.info(
        "Filtered %s meta-policy/admin invariant(s) from compiler output",
        filtered_count,
    )
    return filtered_count


def _invariant_semantic_key(inv: Invariant) -> tuple[str, str, str, str]:
    """Return stable semantic identity for duplicate invariant removal."""
    level_value = getattr(inv.source_level, "value", inv.source_level)
    return (
        inv.type.value,
        json.dumps(inv.parameters.model_dump(mode="json"), sort_keys=True),
        inv.source_item_id or "",
        str(level_value or ""),
    )


def _deduplicate_semantic_invariants(success: SpecAuthorityCompilationSuccess) -> int:
    """Remove exact duplicate invariant objects before deterministic ID assignment."""
    original_count = len(success.invariants)
    seen: dict[tuple[str, str, str, str], Invariant] = {}
    removed_to_kept: dict[str, str] = {}
    removed_by_kept: dict[str, list[str]] = {}
    kept: list[Invariant] = []
    removed = 0
    for inv in success.invariants:
        key = _invariant_semantic_key(inv)
        existing = seen.get(key)
        if existing is not None:
            removed_to_kept[inv.id] = existing.id
            removed_by_kept.setdefault(existing.id, []).append(inv.id)
            removed += 1
            continue
        seen[key] = inv
        kept.append(inv)
    if not removed:
        return 0
    for entry in success.source_map:
        if entry.invariant_id in removed_to_kept:
            entry.invariant_id = removed_to_kept[entry.invariant_id]
    success.invariants = kept
    _record_duplicate_invariant_quality_report(
        success,
        original_count=original_count,
        removed_by_kept=removed_by_kept,
    )
    _append_host_assumption(success, _DUPLICATE_INVARIANT_ASSUMPTION)
    logger.info("Removed %s duplicate semantic invariant(s)", removed)
    return removed


def _record_duplicate_invariant_quality_report(
    success: SpecAuthorityCompilationSuccess,
    *,
    original_count: int,
    removed_by_kept: dict[str, list[str]],
) -> None:
    """Carry normalizer duplicate merge decisions into host quality metadata."""
    source_counts: dict[str, int] = {}
    for entry in success.source_map:
        source_counts[entry.invariant_id] = source_counts.get(entry.invariant_id, 0) + 1

    merged_items = [
        AuthorityQualityMergedItem(
            merge_id=f"AQ-MERGE-{index:03d}",
            item_kind="invariant",
            kept_id=kept_id,
            removed_ids=removed_ids,
            reason="exact_semantic_duplicate",
            source_evidence_count=source_counts.get(kept_id, 0),
        )
        for index, (kept_id, removed_ids) in enumerate(
            removed_by_kept.items(),
            start=1,
        )
    ]
    success.authority_quality = AuthorityQualityReport(
        summary=AuthorityQualitySummary(
            original_invariant_count=original_count,
            final_invariant_count=len(success.invariants),
            merged_invariant_count=sum(len(item.removed_ids) for item in merged_items),
            merged_assumption_count=0,
            review_group_count=0,
            near_duplicate_group_count=0,
            over_split_group_count=0,
            noisy_assumption_group_count=0,
        ),
        merged_items=merged_items,
        review_groups=[],
    )


def _rewrite_quality_report_invariant_ids(
    success: SpecAuthorityCompilationSuccess,
    original_invariants: list[Invariant],
) -> None:
    """Rewrite kept quality merge IDs after deterministic invariant ID assignment."""
    if success.authority_quality is None:
        return
    original_id_to_new_id = {
        original.id: normalized.id
        for original, normalized in zip(
            original_invariants,
            success.invariants,
            strict=False,
        )
    }
    merged_items = [
        item.model_copy(
            update={
                "kept_id": original_id_to_new_id.get(item.kept_id, item.kept_id),
            }
        )
        if item.item_kind == "invariant"
        else item
        for item in success.authority_quality.merged_items
    ]
    success.authority_quality = success.authority_quality.model_copy(
        update={"merged_items": merged_items}
    )


def _tokenize_support_text(text: str) -> list[str]:
    """Return normalized support tokens for source/invariant comparisons."""
    return [token for token in re.split(r"[^a-zA-Z0-9]+", text.casefold()) if token]


def _support_overlap_ratio(expected: list[str], excerpt: str) -> float:
    """Return how much expected invariant language appears in the excerpt."""
    expected_unique = sorted(set(expected))
    if not expected_unique:
        return 1.0
    excerpt_tokens = set(_tokenize_support_text(excerpt))
    matched = sum(1 for token in expected_unique if token in excerpt_tokens)
    return matched / len(expected_unique)


def _behavioral_support_tokens(parameters: InvariantParameters) -> list[str]:
    """Return semantic tokens from behavioral parameters for evidence ranking."""
    dumped = parameters.model_dump(mode="json")
    text_parts: list[str] = []
    for key, value in dumped.items():
        if key in {"source_item_id", "source_level"}:
            continue
        if isinstance(value, str):
            text_parts.append(value)
            continue
        if isinstance(value, list):
            text_parts.extend(item for item in value if isinstance(item, str))
    return _tokenize_support_text(" ".join(text_parts))


def _is_behavioral_invariant(invariant: Invariant) -> bool:
    """Return whether an invariant uses the behavioral provenance contract."""
    return invariant.type in _BEHAVIORAL_INVARIANT_TYPES


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


def _required_field_support_error(inv: Invariant, excerpt: str) -> str | None:
    """Return a required-field evidence mismatch."""
    parameters = inv.parameters
    field_name = str(getattr(parameters, "field_name", "") or "")
    tokens = _tokenize_support_text(field_name)
    if _support_overlap_ratio(tokens, excerpt) < _FIELD_SUPPORT_RATIO_THRESHOLD:
        return (
            f"source_map excerpt does not mention required field '{field_name}' "
            f"for invariant {inv.id}"
        )
    return None


def _forbidden_capability_support_error(
    inv: Invariant,
    excerpt: str,
) -> str | None:
    """Return a forbidden-capability evidence mismatch."""
    capability = str(getattr(inv.parameters, "capability", "") or "")
    tokens = _tokenize_support_text(capability)
    if _support_overlap_ratio(tokens, excerpt) >= _SUPPORT_RATIO_THRESHOLD:
        return None
    safety_tokens = _forbidden_capability_support_tokens(capability)
    if _FORBIDDEN_SAFETY_CUE_RE.search(excerpt) and (
        _support_overlap_ratio(safety_tokens, excerpt)
        >= _FORBIDDEN_SAFETY_SUPPORT_THRESHOLD
    ):
        return None
    return (
        "source_map excerpt does not mention forbidden capability "
        f"'{capability}' for invariant {inv.id}"
    )


def _max_value_support_error(inv: Invariant, excerpt: str) -> str | None:
    """Return a maximum-value evidence mismatch."""
    parameters = inv.parameters
    field_name = str(getattr(parameters, "field_name", "") or "")
    raw_max_value = getattr(parameters, "max_value", None)
    max_value = "" if raw_max_value is None else str(raw_max_value)
    field_tokens = _tokenize_support_text(field_name)
    excerpt_tokens = set(_tokenize_support_text(excerpt))
    if _support_overlap_ratio(field_tokens, excerpt) < _FIELD_SUPPORT_RATIO_THRESHOLD:
        return (
            "source_map excerpt does not mention max-value field "
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


def _relation_constraint_support_error(
    inv: Invariant,
    excerpt: str,
) -> str | None:
    """Return a relation-constraint evidence mismatch."""
    expression = str(getattr(inv.parameters, "expression", "") or "")
    expression_tokens = [
        token for token in _tokenize_support_text(expression) if not token.isdigit()
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


def _behavioral_support_error(inv: Invariant, excerpt: str) -> str | None:
    """Return a behavioral evidence mismatch."""
    support_tokens = _behavioral_support_tokens(inv.parameters)
    if _support_overlap_ratio(support_tokens, excerpt) < _SUPPORT_RATIO_THRESHOLD:
        return f"source_map excerpt does not support behavioral invariant {inv.id}"
    return None


def _source_map_support_error(inv: Invariant, excerpt: str) -> str | None:
    """Return a mismatch reason if the excerpt cannot directly support invariant."""
    handler = {
        InvariantType.REQUIRED_FIELD: _required_field_support_error,
        InvariantType.FORBIDDEN_CAPABILITY: _forbidden_capability_support_error,
        InvariantType.MAX_VALUE: _max_value_support_error,
        InvariantType.RELATION_CONSTRAINT: _relation_constraint_support_error,
    }.get(inv.type)
    if handler is not None:
        return handler(inv, excerpt)
    if _is_behavioral_invariant(inv):
        return _behavioral_support_error(inv, excerpt)
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
            token for token in _tokenize_support_text(expression) if not token.isdigit()
        ]
        base = _support_overlap_ratio(expression_tokens, excerpt)
        if _relation_operator_supported(expression, excerpt):
            base += 0.1
        return base
    if _is_behavioral_invariant(inv):
        return _support_overlap_ratio(_behavioral_support_tokens(parameters), excerpt)
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


def _append_structured_profile_candidate(
    candidates: list[_SourceEvidenceCandidate],
    seen: set[tuple[str, str | None]],
    excerpt: object,
    location: str | None,
    location_hint: str,
) -> None:
    """Append one unique, non-empty structured profile candidate."""
    if not isinstance(excerpt, str):
        return
    compact = _compact_whitespace(excerpt)
    key = (compact, location)
    if not compact or key in seen:
        return
    seen.add(key)
    priority = 3 if location_hint and location_hint == location else 2
    candidates.append(
        _SourceEvidenceCandidate(
            excerpt=compact,
            location=location,
            priority=priority,
        )
    )


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

    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue

        for field_name in ("statement", "title", "rationale"):
            _append_structured_profile_candidate(
                candidates,
                seen,
                item.get(field_name),
                f"{item_id}.{field_name}",
                normalized_hint,
            )

        acceptance_items = item.get("acceptance")
        if isinstance(acceptance_items, list):
            for index, acceptance in enumerate(acceptance_items):
                _append_structured_profile_candidate(
                    candidates,
                    seen,
                    acceptance,
                    f"{item_id}.acceptance[{index}]",
                    normalized_hint,
                )

    return candidates


def _structured_profile_items_by_id(
    source_text: str,
) -> dict[str, Mapping[str, Any]]:
    """Return structured spec items keyed by item ID."""
    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return {}
    if (
        not isinstance(parsed, dict)
        or parsed.get("schema_version") != "agileforge.spec.v1"
    ):
        return {}

    items = parsed.get("items")
    if not isinstance(items, list):
        return {}

    result: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            result[item_id] = item
    return result


def _structured_item_real_texts(source_item: Mapping[str, Any]) -> tuple[str, ...]:
    """Return normative real-text evidence fields for one structured spec item."""
    texts: list[str] = []
    seen: set[str] = set()

    def append(value: object) -> None:
        if not isinstance(value, str):
            return
        compact = _compact_whitespace(value)
        if compact and compact not in seen:
            seen.add(compact)
            texts.append(compact)

    for field_name in ("statement", "title", "rationale"):
        append(source_item.get(field_name))

    acceptance = source_item.get("acceptance")
    if isinstance(acceptance, list):
        for value in acceptance:
            append(value)

    return tuple(texts)


def _normalize_structured_evidence_text(text: str) -> str:
    """Normalize structured source text for grounding comparisons."""
    return _compact_whitespace(text).casefold()


def _is_structured_evidence_glue(text: str) -> bool:
    """Return whether text is only separator glue between real source segments."""
    return bool(text) and all(char in _STRUCTURED_EVIDENCE_GLUE_CHARS for char in text)


def _has_structured_segment_conflict(
    segment: str,
    *,
    selected: tuple[str, ...],
) -> bool:
    """Return whether selected concat segments contain one another."""
    return any(
        segment in selected_segment or selected_segment in segment
        for selected_segment in selected
    )


@dataclass(frozen=True)
class _StructuredConcatState:
    """Immutable inputs plus mutable memo for structured concat matching."""

    excerpt: str
    ordered_texts: tuple[str, ...]
    memo: set[tuple[int, tuple[str, ...]]]


def _structured_text_can_follow(
    state: _StructuredConcatState,
    real_text: str,
    *,
    position: int,
    selected: tuple[str, ...],
) -> bool:
    """Return whether one real source segment can follow at a position."""
    return (
        real_text not in selected
        and not _has_structured_segment_conflict(real_text, selected=selected)
        and state.excerpt.startswith(real_text, position)
    )


def _structured_glue_end_positions(
    excerpt: str,
    *,
    start: int,
) -> tuple[int, ...]:
    """Return non-terminal positions reachable through separator glue."""
    positions: list[int] = []
    for candidate_position in range(start + 1, len(excerpt) + 1):
        glue = excerpt[start:candidate_position]
        if not _is_structured_evidence_glue(glue):
            break
        if candidate_position < len(excerpt):
            positions.append(candidate_position)
    return tuple(positions)


def _can_cover_structured_concat(
    state: _StructuredConcatState,
    position: int,
    selected: tuple[str, ...],
) -> bool:
    """Recursively match source segments separated only by approved glue."""
    memo_key = (position, selected)
    if memo_key in state.memo:
        return False
    state.memo.add(memo_key)

    for real_text in state.ordered_texts:
        if not _structured_text_can_follow(
            state,
            real_text,
            position=position,
            selected=selected,
        ):
            continue
        next_position = position + len(real_text)
        next_selected = (*selected, real_text)
        if next_position == len(state.excerpt):
            return len(next_selected) >= _STRUCTURED_EVIDENCE_MIN_CONCAT_SEGMENTS
        if any(
            _can_cover_structured_concat(state, glue_end, next_selected)
            for glue_end in _structured_glue_end_positions(
                state.excerpt,
                start=next_position,
            )
        ):
            return True
    return False


def _controlled_structured_concat_match(
    normalized_excerpt: str,
    normalized_texts: tuple[str, ...],
) -> bool:
    """Return whether excerpt is fully covered by real text plus separator glue."""
    if not normalized_excerpt:
        return False

    ordered_texts = tuple(
        sorted(
            {text for text in normalized_texts if text},
            key=lambda value: (-len(value), value),
        )
    )
    state = _StructuredConcatState(
        excerpt=normalized_excerpt,
        ordered_texts=ordered_texts,
        memo=set(),
    )
    return _can_cover_structured_concat(state, 0, ())


def _strip_structured_fragment_glue(fragment: str) -> str:
    """Remove only separator characters around an ellipsis fragment."""
    return fragment.strip("".join(sorted(_STRUCTURED_EVIDENCE_GLUE_CHARS)))


def _structured_evidence_token_positions(text: str) -> tuple[tuple[str, int, int], ...]:
    """Return normalized evidence tokens with character positions."""
    return tuple(
        (match.group(0), match.start(), match.end())
        for match in _STRUCTURED_EVIDENCE_TOKEN_RE.finditer(text)
    )


def _structured_fragment_token_subsequence_end(
    corpus_tokens: tuple[tuple[str, int, int], ...],
    fragment_tokens: list[str],
    *,
    position: int,
) -> int | None:
    """Return end offset for a bounded in-order token match, if any."""
    if not fragment_tokens:
        return None

    for start_index, (token, _start, end) in enumerate(corpus_tokens):
        if end <= position or token != fragment_tokens[0]:
            continue

        current_index = start_index
        current_end = end
        matched = True
        for fragment_token in fragment_tokens[1:]:
            next_index: int | None = None
            max_index = min(
                len(corpus_tokens),
                current_index + _STRUCTURED_FRAGMENT_MAX_TOKEN_GAP + 2,
            )
            for candidate_index in range(current_index + 1, max_index):
                if corpus_tokens[candidate_index][0] == fragment_token:
                    next_index = candidate_index
                    break
            if next_index is None:
                matched = False
                break
            current_index = next_index
            current_end = corpus_tokens[current_index][2]

        if matched:
            return current_end

    return None


def _ellipsis_structured_fragment_match(
    normalized_excerpt: str,
    normalized_texts: tuple[str, ...],
) -> bool:
    """Return whether ellipses omit only in-order structured source text."""
    if not _STRUCTURED_ELLIPSIS_RE.search(normalized_excerpt):
        return False

    fragments = [
        _strip_structured_fragment_glue(fragment)
        for fragment in _STRUCTURED_ELLIPSIS_RE.split(normalized_excerpt)
    ]
    fragments = [fragment for fragment in fragments if fragment]
    if not fragments:
        return False

    ordered_source_corpus = _compact_whitespace(" ".join(normalized_texts))
    corpus_tokens = _structured_evidence_token_positions(ordered_source_corpus)
    position = 0
    for fragment in fragments:
        match_position = ordered_source_corpus.find(fragment, position)
        if match_position >= 0:
            position = match_position + len(fragment)
            continue

        fragment_tokens = _tokenize_support_text(fragment)
        token_end = _structured_fragment_token_subsequence_end(
            corpus_tokens,
            fragment_tokens,
            position=position,
        )
        if token_end is None:
            return False
        position = token_end

    return True


def _excerpt_matches_structured_real_text(
    compact_excerpt: str,
    real_texts: tuple[str, ...],
) -> bool:
    """Return whether a source_map excerpt is grounded in structured source text."""
    normalized_excerpt = _normalize_structured_evidence_text(compact_excerpt)
    normalized_texts = tuple(
        _normalize_structured_evidence_text(real_text)
        for real_text in real_texts
        if real_text
    )
    if not normalized_excerpt or not normalized_texts:
        return False
    if normalized_excerpt in normalized_texts:
        return True
    if any(normalized_excerpt in real_text for real_text in normalized_texts):
        return True
    if _ellipsis_structured_fragment_match(normalized_excerpt, normalized_texts):
        return True
    return _controlled_structured_concat_match(
        normalized_excerpt,
        normalized_texts,
    )


def _structured_item_id_from_reference(reference: str | None) -> str | None:
    """Extract a structured spec item ID from a source reference string."""
    if not reference:
        return None
    match = _STRUCTURED_ITEM_ID_RE.search(reference)
    return match.group(0) if match else None


def _structured_reference_is_source_note(reference: str | None) -> bool:
    """Return whether a structured source reference points at source_notes."""
    return ".source_notes" in (reference or "")


def _source_map_item_ids_by_invariant(
    success: SpecAuthorityCompilationSuccess,
) -> dict[str, set[str]]:
    """Return source item IDs from source_map entries by invariant ID."""
    by_invariant: dict[str, set[str]] = {}
    for entry in success.source_map:
        source_item_id = _source_map_entry_item_id(entry)
        if source_item_id is None:
            continue
        by_invariant.setdefault(entry.invariant_id, set()).add(source_item_id)
    return by_invariant


def _source_map_entry_item_id(entry: SourceMapEntry) -> str | None:
    """Return the structured source item ID referenced by one source_map entry."""
    return _structured_item_id_from_reference(entry.location)


def _source_map_entries_by_invariant(
    entries: list[SourceMapEntry],
) -> dict[str, list[SourceMapEntry]]:
    """Return source_map entries grouped by invariant ID."""
    grouped: dict[str, list[SourceMapEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.invariant_id, []).append(entry)
    return grouped


def _grounded_structured_entry_excerpts(
    entries: list[SourceMapEntry],
    *,
    source_item_id: str,
    real_texts: tuple[str, ...],
) -> list[str] | None:
    """Return grounded excerpts, or None if any existing entry is not grounded."""
    grounded_excerpts: list[str] = []
    for entry in entries:
        entry_item_id = _source_map_entry_item_id(entry)
        if entry_item_id != source_item_id:
            return None
        compact_excerpt = _compact_whitespace(entry.excerpt)
        if not _excerpt_matches_structured_real_text(compact_excerpt, real_texts):
            return None
        grounded_excerpts.append(compact_excerpt)
    return grounded_excerpts


def _behavioral_source_excerpts_support_invariant(
    invariant: Invariant,
    excerpts: list[str],
) -> bool:
    """Return whether one or more grounded excerpts support an invariant."""
    for excerpt in excerpts:
        if _source_map_support_error(invariant, excerpt) is None:
            return True

    if len(excerpts) > 1:
        combined_excerpt = _compact_whitespace(" ".join(excerpts))
        return _source_map_support_error(invariant, combined_excerpt) is None

    return False


def _entries_are_only_non_normative_sources(
    entries: list[SourceMapEntry],
    source_items: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether every entry resolves to a known non-normative item."""
    if not entries:
        return False
    source_item_ids = [_source_map_entry_item_id(entry) for entry in entries]
    if any(source_item_id is None for source_item_id in source_item_ids):
        return False
    known_items = [
        source_items[source_item_id]
        for source_item_id in source_item_ids
        if source_item_id in source_items
    ]
    return len(known_items) == len(source_item_ids) and all(
        _is_non_normative_source_item(item) for item in known_items
    )


@dataclass(frozen=True)
class _NonNormativeFilterContext:
    """Evidence collections used while filtering non-normative hard bans."""

    source_items: Mapping[str, Mapping[str, Any]]
    current_entries: Mapping[str, list[SourceMapEntry]]
    original_entries: Mapping[str, list[SourceMapEntry]]
    original_invariants: list[Invariant]
    original_source_map: list[SourceMapEntry]


def _hard_ban_is_non_normative_only(
    context: _NonNormativeFilterContext,
    invariant: Invariant,
    index: int,
) -> bool:
    """Return whether current and original evidence are non-normative only."""
    if not _entries_are_only_non_normative_sources(
        context.current_entries.get(invariant.id, []),
        context.source_items,
    ):
        return False
    if not context.original_source_map:
        return False
    original_id = (
        context.original_invariants[index].id
        if index < len(context.original_invariants)
        else invariant.id
    )
    source_entries = context.original_entries.get(original_id)
    if source_entries is None and index < len(context.original_source_map):
        source_entries = [context.original_source_map[index]]
    return _entries_are_only_non_normative_sources(
        source_entries or [],
        context.source_items,
    )


def _filter_non_normative_source_hard_bans(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
    original_invariants: list[Invariant] | None = None,
    original_source_map: list[SourceMapEntry] | None = None,
) -> int:
    """Remove hard bans sourced only from non-normative source items."""
    source_items = _structured_profile_items_by_id(source_text)
    if not source_items or not success.invariants:
        return 0

    original_invariants = original_invariants or []
    original_source_map = original_source_map or []
    context = _NonNormativeFilterContext(
        source_items=source_items,
        current_entries=_source_map_entries_by_invariant(success.source_map),
        original_entries=_source_map_entries_by_invariant(
            original_source_map or success.source_map
        ),
        original_invariants=original_invariants,
        original_source_map=original_source_map,
    )
    removed_ids: set[str] = set()
    kept_invariants: list[Invariant] = []

    for index, invariant in enumerate(success.invariants):
        should_remove = invariant.type == InvariantType.FORBIDDEN_CAPABILITY and (
            _hard_ban_is_non_normative_only(context, invariant, index)
        )
        if should_remove:
            removed_ids.add(invariant.id)
        else:
            kept_invariants.append(invariant)

    if not removed_ids:
        return 0

    success.invariants = kept_invariants
    success.source_map = [
        entry for entry in success.source_map if entry.invariant_id not in removed_ids
    ]
    _append_host_assumption(success, _NON_NORMATIVE_SOURCE_ASSUMPTION)
    return len(removed_ids)


def _is_non_normative_source_item(source_item: Mapping[str, Any]) -> bool:
    """Return whether a profile item is not terminal authority by itself."""
    return source_item.get("type") in {"DECISION", "OPEN_QUESTION"} and (
        source_item.get("level") is None
    )


def _behavior_invariant_coverage(
    invariant: Invariant,
    *,
    source_items: Mapping[str, Mapping[str, Any]],
    source_entries: Mapping[str, list[SourceMapEntry]],
) -> tuple[bool, bool]:
    """Return whether an item is covered and whether this invariant is repairable."""
    source_item_id = invariant.source_item_id
    if not source_item_id:
        return False, False
    source_item = source_items.get(source_item_id)
    entries = source_entries.get(invariant.id, [])
    grounded = False
    if source_item is not None and invariant.source_level == source_item.get("level"):
        grounded_excerpts = _grounded_structured_entry_excerpts(
            entries,
            source_item_id=source_item_id,
            real_texts=_structured_item_real_texts(source_item),
        )
        grounded = grounded_excerpts is not None and (
            _behavioral_source_excerpts_support_invariant(
                invariant,
                grounded_excerpts,
            )
        )
    if not _is_behavioral_invariant(invariant):
        return grounded, False
    issues = _behavioral_source_metadata_issues(
        invariant,
        source_items=source_items,
        source_entries=entries,
    )
    if not issues:
        return True, False
    repairable = all(
        issue.get("subcode") == BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED
        and issue.get("repairable") is True
        and issue.get("source_item_id") == source_item_id
        for issue in issues
    )
    return grounded, repairable


def _drop_redundant_unsupported_behavior_invariants(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> int:
    """Drop repairable unsupported behavior invariants for already-covered items."""
    source_items = _structured_profile_items_by_id(source_text)
    if not source_items or not success.invariants:
        return 0

    source_entries = _source_map_entries_by_invariant(success.source_map)
    covered_source_item_ids: set[str] = set()
    repairable_candidates: list[Invariant] = []

    for invariant in success.invariants:
        covered, repairable = _behavior_invariant_coverage(
            invariant,
            source_items=source_items,
            source_entries=source_entries,
        )
        if covered and invariant.source_item_id:
            covered_source_item_ids.add(invariant.source_item_id)
        if repairable:
            repairable_candidates.append(invariant)

    removable_ids = {
        invariant.id
        for invariant in repairable_candidates
        if invariant.source_item_id in covered_source_item_ids
    }
    if not removable_ids:
        return 0

    dropped_invariants: list[Invariant] = []
    kept_invariants: list[Invariant] = []
    for invariant in success.invariants:
        if invariant.id in removable_ids:
            dropped_invariants.append(invariant)
            continue
        kept_invariants.append(invariant)

    success.invariants = kept_invariants
    success.source_map = [
        entry for entry in success.source_map if entry.invariant_id not in removable_ids
    ]
    for dropped in dropped_invariants:
        gap = f"Dropped unsupported compiler invariant: {_invariant_text(dropped)}"
        if gap not in success.gaps:
            success.gaps.append(gap)
    return len(dropped_invariants)


@dataclass(frozen=True)
class _SourceMetadataIssueDetails:
    """Optional source metadata attached to one structured issue."""

    source_item_id: str | None = None
    expected_source_level: str | None = None
    observed_source_level: str | None = None
    repairable: bool = False


def _source_metadata_issue(
    *,
    subcode: str,
    message: str,
    invariant_id: str,
    details: _SourceMetadataIssueDetails | None = None,
) -> dict[str, object]:
    """Build a structured source metadata issue for compiler failure details."""
    details = details or _SourceMetadataIssueDetails()
    issue: dict[str, object] = {
        "subcode": subcode,
        "message": message,
        "invariant_id": invariant_id,
        "repairable": details.repairable,
    }
    if details.source_item_id:
        issue["source_item_id"] = details.source_item_id
    if details.expected_source_level:
        issue["expected_source_level"] = details.expected_source_level
    if details.observed_source_level:
        issue["observed_source_level"] = details.observed_source_level
    return issue


def _structured_authority_metadata_issues(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> list[dict[str, object]]:
    """Validate model-emitted authority metadata against structured source."""
    source_items = _structured_profile_items_by_id(source_text)
    if not source_items:
        return []

    issues: list[dict[str, object]] = []
    source_map_ids = _source_map_item_ids_by_invariant(success)
    source_map_entries = _source_map_entries_by_invariant(success.source_map)
    for invariant in success.invariants:
        issues.extend(
            _behavioral_source_metadata_issues(
                invariant,
                source_items=source_items,
                source_entries=source_map_entries.get(invariant.id, []),
            )
        )
        issues.extend(
            _legacy_modality_promotion_issues(
                invariant,
                source_items=source_items,
                source_item_ids=source_map_ids.get(invariant.id, set()),
            )
        )
        issues.extend(
            _example_only_source_issues(
                invariant,
                source_items=source_items,
                source_item_ids=source_map_ids.get(invariant.id, set()),
            )
        )
    return issues


def _structured_authority_metadata_errors(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> list[str]:
    """Return legacy string source metadata errors."""
    return [
        str(issue["message"])
        for issue in _structured_authority_metadata_issues(
            success,
            source_text=source_text,
        )
    ]


def _behavioral_source_metadata_issues(
    invariant: Invariant,
    *,
    source_items: Mapping[str, Mapping[str, Any]],
    source_entries: list[SourceMapEntry],
) -> list[dict[str, object]]:
    """Return behavioral source metadata mismatch errors for an invariant."""
    if not _is_behavioral_invariant(invariant):
        return []

    source_item_id = invariant.source_item_id
    source_level = invariant.source_level
    issue: dict[str, object]
    if not source_item_id:
        message = f"{invariant.id} is missing source_item_id."
        issue = _source_metadata_issue(
            subcode=MISSING_SOURCE_ITEM_ID,
            message=message,
            invariant_id=invariant.id,
        )
    elif not source_level:
        message = f"{invariant.id} is missing source_level."
        issue = _source_metadata_issue(
            subcode=SOURCE_LEVEL_MISMATCH,
            message=message,
            invariant_id=invariant.id,
            details=_SourceMetadataIssueDetails(source_item_id=source_item_id),
        )
    else:
        source_item = source_items.get(source_item_id)
        if source_item is None:
            message = (
                f"{invariant.id} references unknown source_item_id {source_item_id}."
            )
            issue = _source_metadata_issue(
                subcode=UNKNOWN_SOURCE_ITEM,
                message=message,
                invariant_id=invariant.id,
                details=_SourceMetadataIssueDetails(
                    source_item_id=source_item_id,
                    expected_source_level=source_level,
                ),
            )
        elif source_item.get("level") != source_level:
            actual_level = source_item.get("level")
            message = (
                f"{invariant.id} source_item_id {source_item_id} "
                f"source_level {source_level} does not match {actual_level}."
            )
            issue = _source_metadata_issue(
                subcode=SOURCE_LEVEL_MISMATCH,
                message=message,
                invariant_id=invariant.id,
                details=_SourceMetadataIssueDetails(
                    source_item_id=source_item_id,
                    expected_source_level=source_level,
                    observed_source_level=(
                        str(actual_level) if actual_level is not None else None
                    ),
                ),
            )
        else:
            grounded_excerpts = _grounded_structured_entry_excerpts(
                source_entries,
                source_item_id=source_item_id,
                real_texts=_structured_item_real_texts(source_item),
            )
            if grounded_excerpts is not None and (
                _behavioral_source_excerpts_support_invariant(
                    invariant,
                    grounded_excerpts,
                )
            ):
                return []
            message = (
                f"{invariant.id} source_item_id {source_item_id} lacks supporting "
                "real source_map evidence."
            )
            issue = _source_metadata_issue(
                subcode=BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED,
                message=message,
                invariant_id=invariant.id,
                details=_SourceMetadataIssueDetails(
                    source_item_id=source_item_id,
                    expected_source_level=source_level,
                    repairable=True,
                ),
            )
    return [issue]


def _legacy_modality_promotion_issues(
    invariant: Invariant,
    *,
    source_items: Mapping[str, Mapping[str, Any]],
    source_item_ids: set[str],
) -> list[dict[str, object]]:
    """Return errors for legacy hard bans sourced from non-hard guidance."""
    if invariant.type != InvariantType.FORBIDDEN_CAPABILITY:
        return []

    issues: list[dict[str, object]] = []
    for source_item_id in sorted(source_item_ids):
        source_item = source_items.get(source_item_id)
        if source_item is None:
            continue
        if source_item.get("type") == "NON_GOAL":
            continue
        source_level = source_item.get("level")
        if source_level in {"MUST", "MUST_NOT"}:
            continue
        message = (
            f"{invariant.id} FORBIDDEN_CAPABILITY over-promotes "
            f"{source_item_id} source level {source_level}."
        )
        issues.append(
            _source_metadata_issue(
                subcode=LEGACY_MODALITY_PROMOTION,
                message=message,
                invariant_id=invariant.id,
                details=_SourceMetadataIssueDetails(
                    source_item_id=source_item_id,
                    observed_source_level=(
                        str(source_level) if source_level is not None else None
                    ),
                ),
            )
        )
    return issues


def _example_only_source_issues(
    invariant: Invariant,
    *,
    source_items: Mapping[str, Mapping[str, Any]],
    source_item_ids: set[str],
) -> list[dict[str, object]]:
    """Return errors when illustrative examples are sole invariant evidence."""
    if not source_item_ids:
        return []

    known_source_items = [
        source_items[source_item_id]
        for source_item_id in source_item_ids
        if source_item_id in source_items
    ]
    if not known_source_items:
        return []

    if any(source_item.get("type") != "EXAMPLE" for source_item in known_source_items):
        return []

    sorted_source_item_ids = sorted(source_item_ids)
    message = (
        f"{invariant.id} {invariant.type.value} uses only EXAMPLE source "
        f"evidence: {', '.join(sorted_source_item_ids)}."
    )
    issue = _source_metadata_issue(
        subcode=EXAMPLE_ONLY_SOURCE_EVIDENCE,
        message=message,
        invariant_id=invariant.id,
        details=_SourceMetadataIssueDetails(
            source_item_id=(
                sorted_source_item_ids[0] if len(sorted_source_item_ids) == 1 else None
            )
        ),
    )
    if len(sorted_source_item_ids) > 1:
        issue["source_item_ids"] = sorted_source_item_ids
    return [issue]


def _behavioral_source_metadata_errors(
    invariant: Invariant,
    *,
    source_items: Mapping[str, Mapping[str, Any]],
    source_entries: list[SourceMapEntry],
) -> list[str]:
    """Return legacy behavioral source metadata mismatch errors."""
    return [
        str(issue["message"])
        for issue in _behavioral_source_metadata_issues(
            invariant,
            source_items=source_items,
            source_entries=source_entries,
        )
    ]


def _legacy_modality_promotion_errors(
    invariant: Invariant,
    *,
    source_items: Mapping[str, Mapping[str, Any]],
    source_item_ids: set[str],
) -> list[str]:
    """Return legacy hard-ban promotion mismatch errors."""
    return [
        str(issue["message"])
        for issue in _legacy_modality_promotion_issues(
            invariant,
            source_items=source_items,
            source_item_ids=source_item_ids,
        )
    ]


def _example_only_source_errors(
    invariant: Invariant,
    *,
    source_items: Mapping[str, Mapping[str, Any]],
    source_item_ids: set[str],
) -> list[str]:
    """Return legacy example-only source mismatch errors."""
    return [
        str(issue["message"])
        for issue in _example_only_source_issues(
            invariant,
            source_items=source_items,
            source_item_ids=source_item_ids,
        )
    ]


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
    best_matches = [invariant for invariant, score in scored if score == best_score]
    if len(best_matches) != 1:
        return None
    return best_matches[0]


def _best_supported_source_candidate(
    invariant: Invariant,
    candidates: list[_SourceEvidenceCandidate],
) -> _SourceEvidenceCandidate | None:
    """Select the most specific source-text candidate supporting an invariant."""
    if _is_behavioral_invariant(invariant) and invariant.source_item_id:
        item_candidates = [
            candidate
            for candidate in candidates
            if (
                _structured_item_id_from_reference(candidate.location)
                == invariant.source_item_id
                and not _structured_reference_is_source_note(candidate.location)
            )
        ]
        if item_candidates:
            candidates = item_candidates

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


def _structured_source_map_entry_has_valid_location(
    entry: SourceMapEntry,
    *,
    source_text: str,
) -> bool:
    """Return whether a structured source_map entry cites a real profile field."""
    source_item_id = _source_map_entry_item_id(entry)
    if source_item_id is None:
        return False
    source_item = _structured_profile_items_by_id(source_text).get(source_item_id)
    if source_item is None:
        return False
    real_texts = _structured_item_real_texts(source_item)
    if not _excerpt_matches_structured_real_text(
        _compact_whitespace(entry.excerpt),
        real_texts,
    ):
        return False
    return any(
        candidate.location == entry.location
        and _structured_item_id_from_reference(candidate.location) == source_item_id
        for candidate in _structured_profile_source_candidates(
            source_text,
            location_hint=entry.location,
        )
    )


def _structured_source_map_entry_has_grounded_item_ref(
    entry: SourceMapEntry,
    invariant: Invariant,
    *,
    source_text: str,
) -> bool:
    """Return whether a bare item reference has grounded supporting evidence."""
    source_item_id = _source_map_entry_item_id(entry)
    if source_item_id is None or entry.location != source_item_id:
        return False
    source_item = _structured_profile_items_by_id(source_text).get(source_item_id)
    if source_item is None:
        return False
    compact_excerpt = _compact_whitespace(entry.excerpt)
    if not _excerpt_matches_structured_real_text(
        compact_excerpt,
        _structured_item_real_texts(source_item),
    ):
        return False
    return _source_map_support_error(invariant, compact_excerpt) is None


def _structured_source_map_entry_has_canonical_location(
    entry: SourceMapEntry,
    *,
    source_text: str,
) -> bool:
    """Return whether a source_map entry already names a structured field."""
    source_item_id = _source_map_entry_item_id(entry)
    if source_item_id is None:
        return False
    return any(
        candidate.location == entry.location
        and _structured_item_id_from_reference(candidate.location) == source_item_id
        for candidate in _structured_profile_source_candidates(
            source_text,
            location_hint=entry.location,
        )
    )


@dataclass(frozen=True)
class _StructuredBehaviorRepairContext:
    """State used while repairing behavioral source-map evidence."""

    success: SpecAuthorityCompilationSuccess
    source_items: Mapping[str, Mapping[str, Any]]
    existing_by_invariant: dict[str, list[SourceMapEntry]]
    source_text: str


def _structured_behavior_candidates(
    context: _StructuredBehaviorRepairContext,
    invariant: Invariant,
) -> list[_SourceEvidenceCandidate]:
    """Return candidates restricted to an invariant's structured source item."""
    return [
        candidate
        for candidate in _structured_profile_source_candidates(
            context.source_text,
            location_hint=invariant.source_item_id,
        )
        if _structured_item_id_from_reference(candidate.location)
        == invariant.source_item_id
    ]


def _append_supported_behavior_candidates(
    context: _StructuredBehaviorRepairContext,
    invariant: Invariant,
    entries: list[SourceMapEntry],
    candidates: list[_SourceEvidenceCandidate],
    grounded_excerpts: list[str],
) -> bool:
    """Append the smallest candidate set that supports one behavioral invariant."""
    existing_keys = {
        (_compact_whitespace(entry.excerpt), entry.location) for entry in entries
    }
    appended_entries: list[SourceMapEntry] = []
    draft_excerpts = list(grounded_excerpts)
    for candidate in candidates:
        candidate_key = (_compact_whitespace(candidate.excerpt), candidate.location)
        if candidate_key in existing_keys:
            continue
        entry = SourceMapEntry(
            invariant_id=invariant.id,
            excerpt=candidate.excerpt,
            location=candidate.location,
        )
        appended_entries.append(entry)
        draft_excerpts.append(_compact_whitespace(candidate.excerpt))
        if _behavioral_source_excerpts_support_invariant(invariant, draft_excerpts):
            break
    if not _behavioral_source_excerpts_support_invariant(invariant, draft_excerpts):
        return False
    context.success.source_map.extend(appended_entries)
    context.existing_by_invariant.setdefault(invariant.id, []).extend(appended_entries)
    return bool(appended_entries)


def _repair_one_structured_behavior_source_map(
    context: _StructuredBehaviorRepairContext,
    invariant: Invariant,
) -> bool:
    """Repair behavioral evidence for one invariant."""
    source_item_id = invariant.source_item_id
    if not _is_behavioral_invariant(invariant) or not source_item_id:
        return False
    source_item = context.source_items.get(source_item_id)
    if source_item is None:
        return False
    entries = context.existing_by_invariant.get(invariant.id, [])
    candidates = _structured_behavior_candidates(context, invariant)
    if not entries:
        matched = _best_supported_source_candidate(invariant, candidates)
        if matched is None:
            return False
        entry = SourceMapEntry(
            invariant_id=invariant.id,
            excerpt=matched.excerpt,
            location=matched.location,
        )
        context.success.source_map.append(entry)
        context.existing_by_invariant.setdefault(invariant.id, []).append(entry)
        return True

    grounded_excerpts = _grounded_structured_entry_excerpts(
        entries,
        source_item_id=source_item_id,
        real_texts=_structured_item_real_texts(source_item),
    )
    if grounded_excerpts is None or _behavioral_source_excerpts_support_invariant(
        invariant,
        grounded_excerpts,
    ):
        return False
    return _append_supported_behavior_candidates(
        context,
        invariant,
        entries,
        candidates,
        grounded_excerpts,
    )


def _repair_structured_behavior_source_map_entries(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> bool:
    """Add exact entries for missing or insufficient grounded behavioral evidence."""
    source_items = _structured_profile_items_by_id(source_text)
    if not source_items:
        return False

    context = _StructuredBehaviorRepairContext(
        success=success,
        source_items=source_items,
        existing_by_invariant=_source_map_entries_by_invariant(success.source_map),
        source_text=source_text,
    )
    changed = False
    for invariant in success.invariants:
        changed = (
            _repair_one_structured_behavior_source_map(context, invariant) or changed
        )
    return changed


def _repair_structured_source_map_from_source_text(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> bool:
    """Repair profile JSON source_map excerpts without rejecting weak evidence."""
    repaired: list[SourceMapEntry] = []
    changed = False

    for index, entry in enumerate(success.source_map):
        invariant = _entry_invariant_for_source_map(
            success,
            entry,
            index,
            evidence_candidates=_structured_entry_match_candidates(
                entry,
                source_text=source_text,
            ),
        )
        if (
            invariant is not None
            and _is_behavioral_invariant(invariant)
            and _structured_reference_is_source_note(entry.location)
        ):
            repaired.append(entry)
            continue
        if invariant is not None and _structured_source_map_entry_has_grounded_item_ref(
            entry,
            invariant,
            source_text=source_text,
        ):
            repaired.append(entry)
            continue
        if (
            invariant is not None
            and _is_behavioral_invariant(invariant)
            and _structured_source_map_entry_has_valid_location(
                entry,
                source_text=source_text,
            )
        ):
            repaired.append(entry)
            continue
        if (
            invariant is not None
            and _is_behavioral_invariant(invariant)
            and _structured_source_map_entry_has_canonical_location(
                entry,
                source_text=source_text,
            )
        ):
            repaired.append(entry)
            continue

        candidates = [
            _SourceEvidenceCandidate(
                excerpt=_compact_whitespace(entry.excerpt),
                location=entry.location,
            )
        ]
        candidates.extend(
            _structured_profile_source_candidates(
                source_text,
                location_hint=entry.location,
            )
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
    return (
        _repair_structured_behavior_source_map_entries(
            success,
            source_text=source_text,
        )
        or changed
    )


def _plain_source_evidence_candidates(
    success: SpecAuthorityCompilationSuccess,
    source_text: str,
) -> list[_SourceEvidenceCandidate]:
    """Collect source evidence candidates for unstructured source text."""
    candidates = [
        candidate
        for entry in success.source_map
        for candidate in _candidate_evidence_from_source_text(
            entry,
            source_text=source_text,
        )
    ]
    candidates.extend(_source_text_line_candidates(source_text))
    return candidates


def _invariant_id_counts(invariants: list[Invariant]) -> dict[str, int]:
    """Count invariant IDs while preserving duplicate placeholder semantics."""
    counts: dict[str, int] = {}
    for invariant in invariants:
        counts[invariant.id] = counts.get(invariant.id, 0) + 1
    return counts


@dataclass
class _PlainSourceRepairState:
    """Mutable collections used during plain-text source-map repair."""

    original_source_map: list[SourceMapEntry]
    original_invariants: list[Invariant]
    original_id_counts: dict[str, int]
    evidence_candidates: list[_SourceEvidenceCandidate]
    repaired_by_entry_index: dict[int, SourceMapEntry]
    appended_repaired: list[SourceMapEntry]
    retained_invariants: list[Invariant]
    retained_invariant_indexes: set[int]
    dropped_invariants: list[Invariant]
    used_entry_indexes: set[int]


def _primary_source_entry_index(
    state: _PlainSourceRepairState,
    invariant: Invariant,
    index: int,
) -> int | None:
    """Return the first unused original source entry for an invariant."""
    for entry_index, entry in enumerate(state.original_source_map):
        if (
            entry_index not in state.used_entry_indexes
            and entry.invariant_id == invariant.id
        ):
            return entry_index
    if index < len(state.original_source_map) and index not in state.used_entry_indexes:
        return index
    return None


def _repair_plain_source_invariant(
    state: _PlainSourceRepairState,
    invariant: Invariant,
    index: int,
) -> None:
    """Match and retain one invariant against plain-text evidence."""
    supported = [
        candidate
        for candidate in state.evidence_candidates
        if _source_map_support_error(invariant, candidate.excerpt) is None
    ]
    if not supported:
        state.dropped_invariants.append(invariant)
        return
    matched = max(
        supported,
        key=lambda candidate: _source_map_support_score(invariant, candidate.excerpt),
    )
    state.retained_invariants.append(invariant)
    state.retained_invariant_indexes.add(index)
    repaired_entry = SourceMapEntry(
        invariant_id=invariant.id,
        excerpt=matched.excerpt,
        location=matched.location,
    )
    entry_index = _primary_source_entry_index(state, invariant, index)
    if entry_index is None:
        state.appended_repaired.append(repaired_entry)
    else:
        state.used_entry_indexes.add(entry_index)
        state.repaired_by_entry_index[entry_index] = repaired_entry


def _preserve_original_source_entry(
    state: _PlainSourceRepairState,
    retained_id_counts: Mapping[str, int],
    entry_index: int,
    entry: SourceMapEntry,
) -> bool:
    """Return whether an unmatched original source entry should remain."""
    original_id_count = state.original_id_counts.get(entry.invariant_id, 0)
    if original_id_count == 1:
        return entry.invariant_id in retained_id_counts
    if entry_index < len(state.original_invariants):
        return entry_index in state.retained_invariant_indexes
    return retained_id_counts.get(entry.invariant_id, 0) == original_id_count


def _apply_dropped_plain_source_invariants(
    success: SpecAuthorityCompilationSuccess,
    state: _PlainSourceRepairState,
) -> None:
    """Apply retained invariants and append gaps for dropped unsupported entries."""
    if not state.dropped_invariants:
        return
    success.invariants = state.retained_invariants
    for dropped in state.dropped_invariants:
        gap = f"Dropped unsupported compiler invariant: {_invariant_text(dropped)}"
        if gap not in success.gaps:
            success.gaps.append(gap)


def _repaired_plain_source_map(
    state: _PlainSourceRepairState,
) -> list[SourceMapEntry]:
    """Build the repaired source map in original entry order."""
    retained_id_counts = _invariant_id_counts(state.retained_invariants)
    repaired: list[SourceMapEntry] = []
    for entry_index, entry in enumerate(state.original_source_map):
        replacement = state.repaired_by_entry_index.get(entry_index)
        if replacement is not None:
            repaired.append(replacement)
        elif _preserve_original_source_entry(
            state,
            retained_id_counts,
            entry_index,
            entry,
        ):
            repaired.append(entry)
    repaired.extend(state.appended_repaired)
    return repaired


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

    evidence_candidates = _plain_source_evidence_candidates(success, source_text)
    if not evidence_candidates:
        return False

    original_source_map = list(success.source_map)
    original_invariants = list(success.invariants)
    state = _PlainSourceRepairState(
        original_source_map=original_source_map,
        original_invariants=original_invariants,
        original_id_counts=_invariant_id_counts(original_invariants),
        evidence_candidates=evidence_candidates,
        repaired_by_entry_index={},
        appended_repaired=[],
        retained_invariants=[],
        retained_invariant_indexes=set(),
        dropped_invariants=[],
        used_entry_indexes=set(),
    )

    for invariant_index, invariant in enumerate(success.invariants):
        _repair_plain_source_invariant(state, invariant, invariant_index)

    if not state.retained_invariants:
        return False
    _apply_dropped_plain_source_invariants(success, state)
    success.source_map = _repaired_plain_source_map(state)
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


@dataclass(frozen=True)
class _SourceMapIdRewriteContext:
    """Mappings used to rewrite source-map invariant IDs."""

    success: SpecAuthorityCompilationSuccess
    normalized_ids: set[str]
    original_id_counts: Mapping[str, int]
    original_id_to_new_id: Mapping[str, str]


def _positional_normalized_id(
    context: _SourceMapIdRewriteContext,
    index: int,
) -> str | None:
    """Return the normalized invariant ID at a stable positional fallback."""
    invariants = context.success.invariants
    if index < len(invariants):
        return invariants[index].id
    if invariants:
        return invariants[index % len(invariants)].id
    return None


def _support_matched_normalized_id(
    context: _SourceMapIdRewriteContext,
    entry: SourceMapEntry,
    positional_id: str | None,
) -> str | None:
    """Return a unique better-supported normalized invariant ID."""
    scored = [
        (invariant, _source_map_support_score(invariant, entry.excerpt))
        for invariant in context.success.invariants
        if _source_map_support_error(invariant, entry.excerpt) is None
    ]
    if not scored:
        return None
    best_score = max(score for _, score in scored)
    positional_scores = [
        score for invariant, score in scored if invariant.id == positional_id
    ]
    if positional_scores and positional_scores[0] >= best_score:
        return None
    best_matches = [invariant for invariant, score in scored if score == best_score]
    if len(best_matches) != 1:
        return None
    return best_matches[0].id


def _fallback_normalized_id(
    context: _SourceMapIdRewriteContext,
    entry: SourceMapEntry,
    index: int,
) -> str | None:
    """Return evidence-matched or positional normalized ID fallback."""
    positional_id = _positional_normalized_id(context, index)
    return (
        _support_matched_normalized_id(context, entry, positional_id) or positional_id
    )


def _rewrite_source_map_entry_id(
    context: _SourceMapIdRewriteContext,
    entry: SourceMapEntry,
    index: int,
) -> None:
    """Rewrite one source-map entry ID while preserving duplicate semantics."""
    if entry.invariant_id in context.normalized_ids:
        return
    original_count = context.original_id_counts.get(entry.invariant_id, 0)
    if original_count == 1 and entry.invariant_id in context.original_id_to_new_id:
        entry.invariant_id = context.original_id_to_new_id[entry.invariant_id]
        return
    fallback_id = _fallback_normalized_id(context, entry, index)
    if fallback_id is not None:
        entry.invariant_id = fallback_id


def _rewrite_source_map_invariant_ids(
    success: SpecAuthorityCompilationSuccess,
    original_invariants: list[Invariant],
) -> None:
    """Rewrite source-map IDs without collapsing duplicate original placeholders."""
    normalized_ids = {invariant.id for invariant in success.invariants}
    original_id_counts = _invariant_id_counts(original_invariants)

    original_id_to_new_id: dict[str, str] = {}
    for original, normalized in zip(
        original_invariants,
        success.invariants,
        strict=False,
    ):
        if original_id_counts[original.id] == 1:
            original_id_to_new_id[original.id] = normalized.id

    context = _SourceMapIdRewriteContext(
        success=success,
        normalized_ids=normalized_ids,
        original_id_counts=original_id_counts,
        original_id_to_new_id=original_id_to_new_id,
    )
    for index, entry in enumerate(success.source_map):
        _rewrite_source_map_entry_id(context, entry, index)

    success.source_map = [
        entry for entry in success.source_map if entry.invariant_id in normalized_ids
    ]


def _decode_compiler_json(
    raw_json: str,
) -> tuple[object | None, SpecAuthorityCompilerOutput | None]:
    """Decode raw compiler JSON or return a structured invalid-JSON failure."""
    try:
        return json.loads(_extract_json_candidate(raw_json)), None
    except json.JSONDecodeError as exc:
        logger.exception("Spec authority compiler returned invalid JSON")
        return None, _failure(
            reason="INVALID_JSON",
            blocking_gaps=[str(exc)],
        )


def _parse_enveloped_compiler_payload(
    payload: object,
    validation_gaps: list[str],
) -> SpecAuthorityCompilerOutput | None:
    """Try the supported envelope and nested-result payload variants."""
    if (
        not _is_string_object_dict(payload)
        or "result" not in payload
        or "error" in payload
    ):
        return None
    try:
        envelope = SpecAuthorityCompilerEnvelope.model_validate(payload)
        logger.info("Parsed compiler output as SpecAuthorityCompilerEnvelope")
        return SpecAuthorityCompilerOutput(root=envelope.result)
    except ValidationError as envelope_exc:
        validation_gaps.append(_summarize_validation_error("envelope", envelope_exc))
    try:
        parsed = SpecAuthorityCompilerOutput.model_validate(payload.get("result"))
    except ValidationError as result_exc:
        validation_gaps.append(
            _summarize_validation_error("envelope.result", result_exc)
        )
        return None
    else:
        logger.info("Parsed compiler output using envelope.result payload")
        return parsed


def _parse_compiler_payload(payload: object) -> SpecAuthorityCompilerOutput:
    """Validate one decoded compiler payload across supported schema variants."""
    _drop_deprecated_compact_ir_for_success_payload(payload)
    _repair_invalid_prompt_hash_for_validation(payload)
    _default_missing_source_map_for_success_payload(payload)
    _repair_param_level_provenance_for_validation(payload)
    _repair_invalid_invariant_ids_for_validation(payload)
    claim_failure = _claim_like_free_text_payload_failure(payload)
    if claim_failure is not None:
        return claim_failure

    validation_gaps: list[str] = []
    try:
        parsed = SpecAuthorityCompilerOutput.model_validate(payload)
    except ValidationError as output_exc:
        claim_failure = _claim_like_assumption_failure(output_exc)
        if claim_failure is not None:
            return claim_failure
        validation_gaps.append(_summarize_validation_error("output", output_exc))
    else:
        logger.info("Parsed compiler output as SpecAuthorityCompilerOutput")
        return parsed

    parsed = _parse_enveloped_compiler_payload(payload, validation_gaps)
    if parsed is not None:
        return parsed
    logger.error("Spec authority compiler JSON schema validation failed")
    for gap in validation_gaps:
        logger.error("%s", gap)
    return _failure(
        reason="JSON_VALIDATION_FAILED",
        blocking_gaps=validation_gaps or ["No schema variant matched"],
    )


def _ground_structured_claims(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_format: SpecSourceFormat,
    source_text: str | None,
) -> SpecAuthorityCompilerOutput | None:
    """Validate structured assumptions against their canonical source artifact."""
    if not any(is_structured_assumption(value) for value in success.assumptions):
        return None
    if source_format != "agileforge.spec.v1" or not source_text:
        return _failure(
            reason="ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE",
            blocking_gaps=["Structured assumption claims require canonical spec JSON."],
        )
    try:
        spec_artifact = TechnicalSpecArtifact.model_validate_json(source_text)
    except ValidationError as exc:
        return _failure(
            reason="ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE",
            blocking_gaps=[_summarize_validation_error("structured source", exc)],
        )
    for index, assumption in enumerate(success.assumptions, start=1):
        grounded = ground_assumption(assumption, spec_artifact)
        if isinstance(grounded, GroundingFailure):
            return _grounding_failure_output(index=index, failure=grounded)
    return None


def _repair_success_source_map(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str | None,
    source_format: SpecSourceFormat,
) -> list[SourceMapEntry]:
    """Repair source evidence and return its pre-repair snapshot."""
    original_source_map = list(success.source_map)
    should_repair = bool(success.source_map) or (
        source_format == "agileforge.spec.v1" and bool(source_text)
    )
    if (
        should_repair
        and _repair_source_map_from_source_text(
            success,
            source_text=source_text,
            source_format=source_format,
        )
        and original_source_map != success.source_map
    ):
        logger.info("Repaired source_map entries from source text")
    return original_source_map


def _assign_deterministic_invariant_ids(
    success: SpecAuthorityCompilationSuccess,
) -> tuple[list[Invariant], SpecAuthorityCompilerOutput | None]:
    """Assign deterministic IDs and report duplicate normalized IDs."""
    original_invariants = [
        invariant.model_copy(deep=True) for invariant in success.invariants
    ]
    for invariant in success.invariants:
        invariant.id = compute_invariant_id_from_payload(
            invariant.type,
            invariant.parameters,
            source_item_id=invariant.source_item_id,
            source_level=invariant.source_level,
        )
    _rewrite_quality_report_invariant_ids(success, original_invariants)
    normalized_ids = [invariant.id for invariant in success.invariants]
    if len(set(normalized_ids)) != len(normalized_ids):
        logger.error("Spec authority compiler produced duplicate invariant IDs")
        return original_invariants, _failure(
            reason="DUPLICATE_INVARIANT_IDS",
            blocking_gaps=["Normalized invariant IDs must be unique"],
        )
    if success.source_map:
        _rewrite_source_map_invariant_ids(success, original_invariants)
    return original_invariants, None


def _validate_structured_source_metadata(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
    original_invariants: list[Invariant],
    original_source_map: list[SourceMapEntry],
) -> SpecAuthorityCompilerOutput | None:
    """Apply structured-source filters and return any metadata failure."""
    _filter_non_normative_source_hard_bans(
        success,
        source_text=source_text,
        original_invariants=original_invariants,
        original_source_map=original_source_map,
    )
    _drop_redundant_unsupported_behavior_invariants(
        success,
        source_text=source_text,
    )
    metadata_issues = _structured_authority_metadata_issues(
        success,
        source_text=source_text,
    )
    if not metadata_issues:
        return None
    return _failure(
        reason="SOURCE_METADATA_MISMATCH",
        blocking_gaps=[str(issue["message"]) for issue in metadata_issues],
        source_metadata_issues=metadata_issues,
    )


def _normalize_parsed_compiler_output(
    parsed: SpecAuthorityCompilerOutput,
    *,
    source_text: str | None,
    source_format: SpecSourceFormat,
) -> SpecAuthorityCompilerOutput:
    """Normalize one parsed compiler result into its deterministic final form."""
    if isinstance(parsed.root, SpecAuthorityCompilationFailure):
        logger.error(
            "Spec authority compiler returned failure: %s", parsed.root.model_dump()
        )
        return parsed

    success: SpecAuthorityCompilationSuccess = parsed.root
    _filter_meta_policy_invariants(success)
    _deduplicate_semantic_invariants(success)
    success.prompt_hash = SPEC_AUTHORITY_COMPILER_PROMPT_HASH
    success.compiler_version = SPEC_AUTHORITY_COMPILER_VERSION

    grounding_failure = _ground_structured_claims(
        success,
        source_format=source_format,
        source_text=source_text,
    )
    if grounding_failure is not None:
        return grounding_failure

    if not success.invariants:
        logger.warning("No invariants extracted from spec authority compiler output")
        if "No invariants extracted from spec" not in success.gaps:
            success.gaps.append("No invariants extracted from spec")
        _clear_compact_ir(success)
        return _validated_success_output(success)

    original_source_map = _repair_success_source_map(
        success,
        source_text=source_text,
        source_format=source_format,
    )
    original_invariants, id_failure = _assign_deterministic_invariant_ids(success)
    if id_failure is not None:
        return id_failure

    if source_format == "agileforge.spec.v1" and source_text:
        metadata_failure = _validate_structured_source_metadata(
            success,
            source_text=source_text,
            original_invariants=original_invariants,
            original_source_map=original_source_map,
        )
        if metadata_failure is not None:
            return metadata_failure

    _clear_compact_ir(success)

    return _validated_success_output(success)


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
    detected_format = source_format or _detect_source_format(source_text)
    payload, decode_failure = _decode_compiler_json(raw_json)
    if decode_failure is not None:
        return decode_failure
    parsed = _parse_compiler_payload(payload)
    return _normalize_parsed_compiler_output(
        parsed,
        source_text=source_text,
        source_format=detected_format,
    )
