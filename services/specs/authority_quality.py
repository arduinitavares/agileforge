"""Project-agnostic quality gate for compiled authority artifacts."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable

from utils.spec_schemas import (
    AuthorityQualityMergedItem,
    AuthorityQualityReport,
    AuthorityQualityReviewGroup,
    AuthorityQualitySummary,
    Invariant,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
)

NEAR_DUPLICATE_INVARIANT_THRESHOLD: float = 0.72
NOISY_ASSUMPTION_THRESHOLD: float = 0.82
OVER_SPLIT_SOURCE_ITEM_THRESHOLD: int = 5
OVER_SPLIT_SUBJECT_THRESHOLD: int = 3
MAX_REVIEW_GROUPS: int = 40
MAX_GROUP_MEMBERS: int = 12

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "or",
        "the",
        "to",
        "with",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")
_TERMINAL_PUNCTUATION_RE = re.compile(r"[\s.,;:!?]+$")


def apply_authority_quality_gate(
    success: SpecAuthorityCompilationSuccess,
) -> SpecAuthorityCompilationSuccess:
    """Return a copy with exact duplicates merged and quality report attached."""
    gated = success.model_copy(deep=True)
    original_invariant_count = len(gated.invariants)
    original_assumption_count = len(gated.assumptions)

    merged_items: list[AuthorityQualityMergedItem] = []
    review_groups: list[AuthorityQualityReviewGroup] = []

    _merge_exact_invariants(gated, merged_items)
    _merge_exact_assumptions(gated, merged_items)
    review_groups.extend(_related_source_variant_groups(gated.invariants))
    review_groups.extend(_near_duplicate_invariant_groups(gated.invariants))
    review_groups.extend(_over_split_groups(gated.invariants))
    review_groups.extend(_noisy_assumption_groups(gated.assumptions))
    review_groups = _dedupe_and_cap_groups(review_groups)

    summary = AuthorityQualitySummary(
        original_invariant_count=original_invariant_count,
        final_invariant_count=len(gated.invariants),
        merged_invariant_count=original_invariant_count - len(gated.invariants),
        merged_assumption_count=original_assumption_count - len(gated.assumptions),
        review_group_count=len(review_groups),
        near_duplicate_group_count=sum(
            1
            for group in review_groups
            if group.group_type == "near_duplicate_invariants"
        ),
        over_split_group_count=sum(
            1
            for group in review_groups
            if group.group_type == "over_split_invariants"
        ),
        noisy_assumption_group_count=sum(
            1
            for group in review_groups
            if group.group_type == "noisy_assumptions"
        ),
    )
    gated.authority_quality = AuthorityQualityReport(
        summary=summary,
        merged_items=merged_items,
        review_groups=review_groups,
    )
    return gated


def _invariant_exact_key(invariant: Invariant) -> tuple[str, str, str, str]:
    level_value = getattr(invariant.source_level, "value", invariant.source_level)
    return (
        invariant.type.value,
        json.dumps(
            invariant.parameters.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
        invariant.source_item_id or "",
        str(level_value or ""),
    )


def _merge_exact_invariants(
    success: SpecAuthorityCompilationSuccess,
    merged_items: list[AuthorityQualityMergedItem],
) -> None:
    seen: dict[tuple[str, str, str, str], Invariant] = {}
    removed_to_kept: dict[str, str] = {}
    kept: list[Invariant] = []
    removed_by_kept: dict[str, list[str]] = defaultdict(list)
    for invariant in success.invariants:
        key = _invariant_exact_key(invariant)
        existing = seen.get(key)
        if existing is None:
            seen[key] = invariant
            kept.append(invariant)
            continue
        removed_to_kept[invariant.id] = existing.id
        removed_by_kept[existing.id].append(invariant.id)
    if not removed_to_kept:
        return
    success.invariants = kept
    success.source_map = _remap_source_map(success.source_map, removed_to_kept)
    source_counts = _source_count_by_invariant(success.source_map)
    for index, (kept_id, removed_ids) in enumerate(removed_by_kept.items(), start=1):
        merged_items.append(
            AuthorityQualityMergedItem(
                merge_id=f"AQ-MERGE-{index:03d}",
                item_kind="invariant",
                kept_id=kept_id,
                removed_ids=removed_ids,
                reason="exact_semantic_duplicate",
                source_evidence_count=source_counts.get(kept_id, 0),
            )
        )


def _merge_exact_assumptions(
    success: SpecAuthorityCompilationSuccess,
    merged_items: list[AuthorityQualityMergedItem],
) -> None:
    seen: dict[str, int] = {}
    kept: list[str] = []
    removed_indexes_by_kept: dict[int, list[str]] = defaultdict(list)
    for index, assumption in enumerate(success.assumptions, start=1):
        key = _normalize_exact_assumption(assumption)
        kept_index = seen.get(key)
        if kept_index is None:
            seen[key] = len(kept) + 1
            kept.append(assumption)
            continue
        removed_indexes_by_kept[kept_index].append(f"ASM-{index}")
    if not removed_indexes_by_kept:
        return
    success.assumptions = kept
    base = len(merged_items)
    for offset, (kept_index, removed_ids) in enumerate(
        removed_indexes_by_kept.items(),
        start=1,
    ):
        merged_items.append(
            AuthorityQualityMergedItem(
                merge_id=f"AQ-MERGE-{base + offset:03d}",
                item_kind="assumption",
                kept_id=f"ASM-{kept_index}",
                removed_ids=removed_ids,
                reason="exact_assumption_duplicate",
                source_evidence_count=0,
            )
        )


def _remap_source_map(
    entries: Iterable[SourceMapEntry],
    removed_to_kept: dict[str, str],
) -> list[SourceMapEntry]:
    seen: set[tuple[str, str, str | None]] = set()
    remapped: list[SourceMapEntry] = []
    for entry in entries:
        invariant_id = removed_to_kept.get(entry.invariant_id, entry.invariant_id)
        key = (invariant_id, entry.excerpt, entry.location)
        if key in seen:
            continue
        seen.add(key)
        remapped.append(
            SourceMapEntry(
                invariant_id=invariant_id,
                excerpt=entry.excerpt,
                location=entry.location,
            )
        )
    return remapped


def _source_count_by_invariant(entries: Iterable[SourceMapEntry]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        counts[entry.invariant_id] += 1
    return dict(counts)


def _related_source_variant_groups(
    invariants: list[Invariant],
) -> list[AuthorityQualityReviewGroup]:
    buckets: dict[tuple[str, str], list[Invariant]] = defaultdict(list)
    for invariant in invariants:
        params = json.dumps(
            invariant.parameters.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        buckets[(invariant.type.value, params)].append(invariant)
    groups: list[AuthorityQualityReviewGroup] = []
    for members in buckets.values():
        provenance_keys = {
            (
                member.source_item_id or "",
                str(getattr(member.source_level, "value", member.source_level) or ""),
            )
            for member in members
        }
        if len(members) > 1 and len(provenance_keys) > 1:
            groups.append(
                _group(
                    "related_source_variants",
                    members,
                    "same invariant shape appears under different source provenance",
                )
            )
    return groups


def _near_duplicate_invariant_groups(
    invariants: list[Invariant],
) -> list[AuthorityQualityReviewGroup]:
    buckets: dict[tuple[str, str, str, str], list[Invariant]] = defaultdict(list)
    for invariant in invariants:
        buckets[_near_duplicate_bucket(invariant)].append(invariant)
    groups: list[AuthorityQualityReviewGroup] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        member_texts = {member.id: _invariant_text(member) for member in members}
        near_members: set[str] = set()
        for left_index, left in enumerate(members):
            for right in members[left_index + 1 :]:
                if (
                    _jaccard(member_texts[left.id], member_texts[right.id])
                    >= NEAR_DUPLICATE_INVARIANT_THRESHOLD
                ):
                    near_members.update({left.id, right.id})
        selected = [member for member in members if member.id in near_members]
        if len(selected) > 1:
            groups.append(
                _group(
                    "near_duplicate_invariants",
                    selected,
                    "high token overlap in same source/type bucket",
                )
            )
    return groups


def _near_duplicate_bucket(invariant: Invariant) -> tuple[str, str, str, str]:
    level_value = getattr(invariant.source_level, "value", invariant.source_level)
    return (
        invariant.type.value,
        invariant.source_item_id or "",
        str(level_value or ""),
        _subject_like_parameter(invariant),
    )


def _over_split_groups(invariants: list[Invariant]) -> list[AuthorityQualityReviewGroup]:
    groups: list[AuthorityQualityReviewGroup] = []
    by_source: dict[str, list[Invariant]] = defaultdict(list)
    by_subject: dict[tuple[str, str, str], list[Invariant]] = defaultdict(list)
    for invariant in invariants:
        if not invariant.source_item_id:
            continue
        by_source[invariant.source_item_id].append(invariant)
        by_subject[
            (
                invariant.source_item_id,
                invariant.type.value,
                _subject_like_parameter(invariant),
            )
        ].append(invariant)
    for members in by_source.values():
        if len(members) >= OVER_SPLIT_SOURCE_ITEM_THRESHOLD:
            groups.append(
                _group(
                    "over_split_invariants",
                    members,
                    "one source item produced many invariants",
                )
            )
    for members in by_subject.values():
        if len(members) >= OVER_SPLIT_SUBJECT_THRESHOLD:
            groups.append(
                _group(
                    "over_split_invariants",
                    members,
                    "same source/type/subject cluster produced many invariants",
                )
            )
    return groups


def _noisy_assumption_groups(assumptions: list[str]) -> list[AuthorityQualityReviewGroup]:
    members = [f"ASM-{index}" for index in range(1, len(assumptions) + 1)]
    selected: set[str] = set()
    for left_index, left in enumerate(assumptions):
        for right_index, right in enumerate(
            assumptions[left_index + 1 :],
            start=left_index + 2,
        ):
            if _jaccard(left, right) >= NOISY_ASSUMPTION_THRESHOLD:
                selected.update({f"ASM-{left_index + 1}", f"ASM-{right_index}"})
    if len(selected) < 2:
        return []
    ordered = [member for member in members if member in selected]
    return [
        AuthorityQualityReviewGroup(
            group_id="AQ-GROUP-001",
            group_type="noisy_assumptions",
            severity="warning",
            member_ids=ordered[:MAX_GROUP_MEMBERS],
            reason="compiler assumptions have high token overlap",
            merge_allowed=False,
            truncated=len(ordered) > MAX_GROUP_MEMBERS,
        )
    ]


def _group(
    group_type: str,
    members: list[Invariant],
    reason: str,
) -> AuthorityQualityReviewGroup:
    return AuthorityQualityReviewGroup(
        group_id="AQ-GROUP-000",
        group_type=group_type,
        severity="warning",
        member_ids=[member.id for member in members[:MAX_GROUP_MEMBERS]],
        reason=reason,
        merge_allowed=False,
        truncated=len(members) > MAX_GROUP_MEMBERS,
    )


def _dedupe_and_cap_groups(
    groups: list[AuthorityQualityReviewGroup],
) -> list[AuthorityQualityReviewGroup]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    deduped: list[AuthorityQualityReviewGroup] = []
    for group in groups:
        key = (group.group_type, tuple(group.member_ids))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(group)
        if len(deduped) >= MAX_REVIEW_GROUPS:
            break
    return [
        group.model_copy(update={"group_id": f"AQ-GROUP-{index:03d}"})
        for index, group in enumerate(deduped, start=1)
    ]


def _subject_like_parameter(invariant: Invariant) -> str:
    dumped = invariant.parameters.model_dump(mode="json")
    for key in ("subject", "field_name", "state", "route", "target", "capability"):
        value = dumped.get(key)
        if isinstance(value, str) and value:
            return _normalize_phrase(value)
    return ""


def _invariant_text(invariant: Invariant) -> str:
    dumped = invariant.parameters.model_dump(mode="json")
    parts = [invariant.type.value]
    for key in sorted(dumped):
        value = dumped[key]
        if isinstance(value, list):
            parts.append(" ".join(str(item) for item in value))
        else:
            parts.append(str(value))
    return " ".join(parts)


def _normalize_phrase(text: str) -> str:
    return " ".join(_tokens(text))


def _normalize_exact_assumption(text: str) -> str:
    compacted = _WHITESPACE_RE.sub(" ", text.casefold().strip())
    return _TERMINAL_PUNCTUATION_RE.sub("", compacted)


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN_RE.findall(text.casefold())
        if token and token not in _STOPWORDS
    ]


def _jaccard(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
