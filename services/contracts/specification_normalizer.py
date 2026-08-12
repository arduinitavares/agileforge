# services/contracts/specification_normalizer.py
"""Host normalization for typed Specification Authority compiler output."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import TYPE_CHECKING, TypeGuard

from pydantic import ValidationError

from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
    compute_invariant_id_from_payload,
)
from utils.spec_authority_assumptions import canonical_assumption_key
from utils.spec_schemas import (
    Invariant,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerOutput,
)

if TYPE_CHECKING:
    from services.contracts.authority_input_v2 import AuthorityInputV2, AuthorityItemV2

logger: logging.Logger = logging.getLogger(name=__name__)


def _failure(
    *,
    reason: str,
    blocking_gaps: list[str],
) -> SpecAuthorityCompilerOutput:
    """Build one closed compiler-failure envelope."""
    return SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationFailure(
            error="SPEC_COMPILATION_FAILED",
            reason=reason,
            blocking_gaps=blocking_gaps,
        )
    )


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _decode(raw_json: str) -> object | SpecAuthorityCompilerOutput:
    """Decode untrusted provider JSON without accepting prose or Markdown fences."""
    try:
        return json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as error:
        return _failure(
            reason="INVALID_JSON",
            blocking_gaps=[f"Compiler output is not valid JSON: {error}"],
        )


def _parse(payload: object) -> SpecAuthorityCompilerOutput:
    """Validate a direct output or the ADK result envelope."""
    try:
        if _is_string_object_dict(payload) and "result" in payload:
            result = SpecAuthorityCompilerEnvelope.model_validate(payload).result
            return SpecAuthorityCompilerOutput(root=result)
        return SpecAuthorityCompilerOutput.model_validate(payload)
    except ValidationError as error:
        detail = error.errors(include_url=False)[0]
        location = ".".join(str(part) for part in detail.get("loc", ()))
        message = str(detail.get("msg", "invalid compiler output"))
        return _failure(
            reason="JSON_VALIDATION_FAILED",
            blocking_gaps=[f"Compiler output schema error at {location}: {message}"],
        )


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _semantic_source_texts(item: AuthorityItemV2) -> tuple[str, ...]:
    """Return only semantic item fields that may support an invariant citation."""
    optional = (item.rationale, item.verification)
    return tuple(
        text
        for text in (item.title, item.statement, *optional, *item.acceptance)
        if text is not None and text.strip()
    )


def _excerpt_is_semantic(excerpt: str, item: AuthorityItemV2) -> bool:
    """Require an excerpt to be copied from one eligible semantic field."""
    normalized_excerpt = _normalize_text(excerpt)
    if not normalized_excerpt:
        return False
    return any(
        normalized_excerpt in _normalize_text(candidate)
        for candidate in _semantic_source_texts(item)
    )


def _source_failure(
    message: str,
) -> SpecAuthorityCompilerOutput:
    return _failure(
        reason="INELIGIBLE_INVARIANT_SOURCE",
        blocking_gaps=[message],
    )


def _coverage_failure(missing_item_ids: list[str]) -> SpecAuthorityCompilerOutput:
    return _failure(
        reason="INCOMPLETE_NORMATIVE_COVERAGE",
        blocking_gaps=[
            "Compiler output omitted eligible items: " + ", ".join(missing_item_ids)
        ],
    )


class _IneligibleInvariantSourceError(ValueError):
    """Raised internally when one output citation crosses the typed boundary."""


def _rewrite_invariant_ids(
    success: SpecAuthorityCompilationSuccess,
) -> tuple[dict[str, tuple[str, ...]], list[Invariant]]:
    """Compute IDs from typed semantics and retain old-to-new identities."""
    rewritten: list[Invariant] = []
    identities: dict[str, list[str]] = {}
    for invariant in success.invariants:
        invariant_id = compute_invariant_id_from_payload(
            invariant.type,
            invariant.parameters,
            source_item_id=invariant.source_item_id,
            source_level=invariant.source_level,
        )
        identities.setdefault(invariant.id, []).append(invariant_id)
        rewritten.append(invariant.model_copy(update={"id": invariant_id}))
    return (
        {old_id: tuple(new_ids) for old_id, new_ids in identities.items()},
        rewritten,
    )


def _rewrite_source_map_ids(
    entries: list[SourceMapEntry],
    *,
    old_to_new: dict[str, tuple[str, ...]],
) -> list[SourceMapEntry] | None:
    """Rewrite provider IDs, using positional mapping for repeated placeholders."""
    uses = Counter(entry.invariant_id for entry in entries)
    positions: Counter[str] = Counter()
    rewritten: list[SourceMapEntry] = []
    for entry in entries:
        candidates = old_to_new.get(entry.invariant_id)
        if not candidates:
            return None
        if len(candidates) == 1:
            invariant_id = candidates[0]
        elif uses[entry.invariant_id] == len(candidates):
            position = positions[entry.invariant_id]
            invariant_id = candidates[position]
            positions[entry.invariant_id] += 1
        else:
            return None
        rewritten.append(entry.model_copy(update={"invariant_id": invariant_id}))
    return rewritten


def _require_typed_sources(
    success: SpecAuthorityCompilationSuccess,
    *,
    authority_input: AuthorityInputV2,
) -> None:
    """Require every invariant to cite exact eligible item semantics."""
    eligible_items = {item.id: item for item in authority_input.normative_items}
    if tuple(sorted(eligible_items)) != tuple(authority_input.eligible_item_ids):
        message = "Authority input eligible_item_ids do not match normative_items."
        raise _IneligibleInvariantSourceError(message)

    invariants = {invariant.id: invariant for invariant in success.invariants}
    entries_by_invariant: dict[str, list[SourceMapEntry]] = {}
    for entry in success.source_map:
        entries_by_invariant.setdefault(entry.invariant_id, []).append(entry)
        if entry.invariant_id not in invariants:
            message = f"Source map references unknown invariant {entry.invariant_id}."
            raise _IneligibleInvariantSourceError(message)

    for invariant in success.invariants:
        source_item_id = invariant.source_item_id
        item = eligible_items.get(source_item_id or "")
        if item is None:
            message = (
                f"Invariant {invariant.id} cites non-eligible item {source_item_id!r}."
            )
            raise _IneligibleInvariantSourceError(message)
        if invariant.source_level is None or invariant.source_level != item.level:
            message = f"Invariant {invariant.id} source level does not match {item.id}."
            raise _IneligibleInvariantSourceError(message)
        entries = entries_by_invariant.get(invariant.id, [])
        if not entries:
            message = f"Invariant {invariant.id} has no typed source-map citation."
            raise _IneligibleInvariantSourceError(message)
        for entry in entries:
            if entry.location != item.id or not _excerpt_is_semantic(
                entry.excerpt,
                item,
            ):
                message = (
                    f"Invariant {invariant.id} is not supported by {item.id} semantics."
                )
                raise _IneligibleInvariantSourceError(message)


def _sort_success(success: SpecAuthorityCompilationSuccess) -> None:
    """Canonicalize set-like compiler collections after validation."""
    success.scope_themes = sorted(set(success.scope_themes), key=str.casefold)
    success.invariants.sort(key=lambda invariant: invariant.id)
    success.source_map.sort(
        key=lambda entry: (
            entry.invariant_id,
            entry.location or "",
            _normalize_text(entry.excerpt),
        )
    )
    success.eligible_feature_rules.sort(key=lambda rule: _normalize_text(rule.rule))
    success.rejected_features = sorted(set(success.rejected_features), key=str.casefold)
    success.gaps = sorted(set(success.gaps), key=str.casefold)
    success.assumptions.sort(key=canonical_assumption_key)


def _missing_eligible_item_ids(
    success: SpecAuthorityCompilationSuccess,
    *,
    authority_input: AuthorityInputV2,
) -> list[str]:
    """Return eligible IDs absent from both typed invariants and explicit gaps."""
    covered = {
        invariant.source_item_id
        for invariant in success.invariants
        if invariant.source_item_id is not None
    }
    for item_id in authority_input.eligible_item_ids:
        if any(gap.strip().startswith(f"{item_id}:") for gap in success.gaps):
            covered.add(item_id)
    return sorted(set(authority_input.eligible_item_ids) - covered)


def normalize_compiler_output(
    raw_json: str,
    *,
    authority_input: AuthorityInputV2,
) -> SpecAuthorityCompilerOutput:
    """Normalize provider output against one host-built typed Authority input."""
    logger.info(
        "Normalizing typed Authority output for %s",
        authority_input.authority_input_fingerprint,
    )
    payload = _decode(raw_json)
    if isinstance(payload, SpecAuthorityCompilerOutput):
        return payload
    parsed = _parse(payload)
    if isinstance(parsed.root, SpecAuthorityCompilationFailure):
        return parsed

    success = parsed.root
    success.prompt_hash = SPEC_AUTHORITY_COMPILER_PROMPT_HASH
    success.compiler_version = SPEC_AUTHORITY_COMPILER_VERSION
    old_to_new, rewritten_invariants = _rewrite_invariant_ids(success)
    rewritten_source_map = _rewrite_source_map_ids(
        success.source_map,
        old_to_new=old_to_new,
    )
    if rewritten_source_map is None:
        return _source_failure(
            "Compiler source-map identities do not match emitted invariants."
        )
    success.invariants = rewritten_invariants
    success.source_map = rewritten_source_map
    try:
        _require_typed_sources(success, authority_input=authority_input)
    except _IneligibleInvariantSourceError as error:
        return _source_failure(str(error))
    missing_item_ids = _missing_eligible_item_ids(
        success,
        authority_input=authority_input,
    )
    if missing_item_ids:
        return _coverage_failure(missing_item_ids)
    _sort_success(success)
    return SpecAuthorityCompilerOutput(root=success)


__all__ = ["normalize_compiler_output"]
