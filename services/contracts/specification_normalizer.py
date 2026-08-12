# services/contracts/specification_normalizer.py
"""Host normalization for typed Specification Authority compiler output."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, TypeGuard

from pydantic import BaseModel, ValidationError

from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
    compute_invariant_id_from_payload,
)
from utils.spec_authority_assumptions import canonical_assumption_key
from utils.spec_authority_ir import AuthorityTargetKind
from utils.spec_schemas import (
    Invariant,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerOutput,
    SpecAuthorityMapping,
)

if TYPE_CHECKING:
    from services.contracts.authority_input_v2 import AuthorityInputV2, AuthorityItemV2

logger: logging.Logger = logging.getLogger(name=__name__)
_MIN_PLURAL_TOKEN_LENGTH = 3
_ORDINAL_MAPPING_PREFIXES: dict[AuthorityTargetKind, tuple[str, ...]] = {
    AuthorityTargetKind.ELIGIBLE_FEATURE_RULE: ("ELIG", "EFR"),
    AuthorityTargetKind.REJECTED_FEATURE: ("REJ", "RF"),
    AuthorityTargetKind.GAP: ("GAP",),
    AuthorityTargetKind.ASSUMPTION: ("ASM",),
}
type _OrdinalMappingCollections = dict[
    AuthorityTargetKind,
    tuple[tuple[str, ...], tuple[str, ...]],
]


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
        return _validation_failure(error, subject="Compiler output")


def _validation_failure(
    error: ValidationError,
    *,
    subject: str,
) -> SpecAuthorityCompilerOutput:
    """Convert one schema-validation error into the public failure envelope."""
    detail = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in detail.get("loc", ()))
    message = str(detail.get("msg", "invalid compiler output"))
    return _failure(
        reason="JSON_VALIDATION_FAILED",
        blocking_gaps=[f"{subject} schema error at {location}: {message}"],
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _text_sort_key(value: str) -> tuple[str, str]:
    """Return normalized semantics plus exact bytes as a total ordering key."""
    return _normalize_text(value), value


def _model_sort_key(value: BaseModel) -> str:
    """Return exact canonical model bytes for deterministic tie-breaking."""
    return json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _optional_text_sort_key(value: str | None) -> tuple[bool, str, str]:
    """Order nullable text without collapsing null and empty-string identities."""
    return value is not None, _normalize_text(value or ""), value or ""


def _semantic_source_texts(item: AuthorityItemV2) -> tuple[str, ...]:
    """Return only semantic item fields that may support an invariant citation."""
    return tuple(
        text
        for text in (item.statement, *item.acceptance)
        if text.strip()
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


def _semantic_token(value: str) -> str:
    """Normalize a semantic token while tolerating simple plural inflection."""
    normalized = value.casefold()
    if len(normalized) > _MIN_PLURAL_TOKEN_LENGTH and normalized.endswith("s"):
        return normalized[:-1]
    return normalized


def _semantic_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        _semantic_token(token)
        for token in re.findall(r"[a-z0-9]+", value.casefold())
    )


def _parameter_values(value: object) -> tuple[str, ...]:
    """Flatten provider-controlled parameter scalars for source substantiation."""
    if isinstance(value, dict):
        return tuple(
            scalar
            for nested in value.values()
            for scalar in _parameter_values(nested)
        )
    if isinstance(value, list | tuple):
        return tuple(
            scalar for nested in value for scalar in _parameter_values(nested)
        )
    if isinstance(value, str | int | float) and not isinstance(value, bool):
        return (str(value),)
    return ()


_INVARIANT_TYPE_CUES: dict[str, tuple[str, ...]] = {
    "FORBIDDEN_CAPABILITY": ("must not", "never", "forbid", "prohibit"),
    "REQUIRED_FIELD": ("include", "field", "must have", "required"),
    "MAX_VALUE": ("<=", "maximum", "at most", "no more than", "max "),
    "RELATION_CONSTRAINT": ("<=", ">=", "==", "depends", "relation", "equal"),
    "USER_INTERACTION": (
        "click",
        "select",
        "submit",
        "keyboard",
        "mouse",
        "user ",
    ),
    "STATE_TRANSITION": ("state", "status", "transition", "after ", "when "),
    "DATA_CONTRACT": ("data", "record", "payload", "schema", "persist", "storage"),
    "ROUTE_CONTRACT": ("route", "endpoint", "url", "path"),
    "VISIBILITY_RULE": ("visible", "hidden", "show", "display", "remove"),
}


def _parameters_are_semantic(invariant: Invariant, item: AuthorityItemV2) -> bool:
    """Require all parameter values to occur in one eligible source field."""
    source_texts = _semantic_source_texts(item)
    values = _parameter_values(invariant.parameters.model_dump(mode="json"))
    if not values:
        return False
    value_phrases = tuple(" ".join(_semantic_tokens(value)) for value in values)
    if any(not phrase for phrase in value_phrases):
        return False
    supporting_texts = tuple(
        text
        for text in source_texts
        if all(
            phrase in " ".join(_semantic_tokens(text)) for phrase in value_phrases
        )
    )
    if not supporting_texts:
        return False
    cues = _INVARIANT_TYPE_CUES[invariant.type.value]
    if invariant.type.value == "ROUTE_CONTRACT":
        route = invariant.parameters.model_dump(mode="json").get("route")
        if not isinstance(route, str) or not any(
            route.casefold() in text.casefold() for text in supporting_texts
        ):
            return False
    return any(
        cue in text.casefold() for text in supporting_texts for cue in cues
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


def _require_invariant_source(
    invariant: Invariant,
    *,
    eligible_items: dict[str, AuthorityItemV2],
    entries_by_invariant: dict[str, list[SourceMapEntry]],
) -> None:
    """Validate one invariant against only its exact eligible item semantics."""
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
    if not _parameters_are_semantic(invariant, item):
        message = (
            f"Invariant {invariant.id} parameters are not authorized by "
            f"{item.id} semantics."
        )
        raise _IneligibleInvariantSourceError(message)
    entries = entries_by_invariant.get(invariant.id, [])
    if not entries:
        message = f"Invariant {invariant.id} has no typed source-map citation."
        raise _IneligibleInvariantSourceError(message)
    for entry in entries:
        if entry.location != item.id or not _excerpt_is_semantic(entry.excerpt, item):
            message = (
                f"Invariant {invariant.id} is not supported by {item.id} semantics."
            )
            raise _IneligibleInvariantSourceError(message)


def _rewrite_invariant_ids(
    success: SpecAuthorityCompilationSuccess,
) -> tuple[dict[str, str] | None, list[Invariant]]:
    """Compute IDs while rejecting one provider ID with distinct semantics."""
    rewritten: list[Invariant] = []
    identities: dict[str, str] = {}
    for invariant in success.invariants:
        invariant_id = compute_invariant_id_from_payload(
            invariant.type,
            invariant.parameters,
            source_item_id=invariant.source_item_id,
            source_level=invariant.source_level,
        )
        existing = identities.get(invariant.id)
        if existing is not None and existing != invariant_id:
            return None, rewritten
        identities[invariant.id] = invariant_id
        rewritten.append(invariant.model_copy(update={"id": invariant_id}))
    return identities, rewritten


def _rewrite_source_map_ids(
    entries: list[SourceMapEntry],
    *,
    old_to_new: dict[str, str],
) -> list[SourceMapEntry] | None:
    """Rewrite provider IDs only through one semantically unique target."""
    rewritten: list[SourceMapEntry] = []
    for entry in entries:
        invariant_id = old_to_new.get(entry.invariant_id)
        if invariant_id is None:
            return None
        rewritten.append(entry.model_copy(update={"invariant_id": invariant_id}))
    return rewritten


def _rewrite_authority_mapping_ids(
    mappings: list[SpecAuthorityMapping],
    *,
    old_to_new: dict[str, str],
) -> list[SpecAuthorityMapping] | None:
    """Rewrite compact-IR invariant targets through host-owned identities."""
    rewritten: list[SpecAuthorityMapping] = []
    for mapping in mappings:
        if mapping.authority_target_kind is not AuthorityTargetKind.INVARIANT:
            rewritten.append(mapping)
            continue
        authority_item_id = old_to_new.get(mapping.authority_item_id)
        if authority_item_id is None:
            return None
        rewritten.append(
            mapping.model_copy(update={"authority_item_id": authority_item_id})
        )
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
        _require_invariant_source(
            invariant,
            eligible_items=eligible_items,
            entries_by_invariant=entries_by_invariant,
        )


def _sort_success(success: SpecAuthorityCompilationSuccess) -> None:
    """Canonicalize set-like compiler collections after validation."""
    success.scope_themes = sorted(set(success.scope_themes), key=_text_sort_key)
    success.invariants.sort(
        key=lambda invariant: (invariant.id, _model_sort_key(invariant))
    )
    success.source_map.sort(
        key=lambda entry: (
            entry.invariant_id,
            _optional_text_sort_key(entry.location),
            *_text_sort_key(entry.excerpt),
            _model_sort_key(entry),
        )
    )
    success.eligible_feature_rules.sort(
        key=lambda rule: (*_text_sort_key(rule.rule), _model_sort_key(rule))
    )
    success.rejected_features = sorted(
        set(success.rejected_features),
        key=_text_sort_key,
    )
    success.gaps = sorted(set(success.gaps), key=_text_sort_key)
    success.assumptions.sort(
        key=lambda assumption: (
            canonical_assumption_key(assumption),
            _model_sort_key(assumption),
        )
    )


def _ordinal_mapping_collections(
    success: SpecAuthorityCompilationSuccess,
) -> _OrdinalMappingCollections:
    """Capture collection identities whose public mapping IDs are ordinal."""
    return {
        AuthorityTargetKind.ELIGIBLE_FEATURE_RULE: (
            _ORDINAL_MAPPING_PREFIXES[AuthorityTargetKind.ELIGIBLE_FEATURE_RULE],
            tuple(rule.rule for rule in success.eligible_feature_rules),
        ),
        AuthorityTargetKind.REJECTED_FEATURE: (
            _ORDINAL_MAPPING_PREFIXES[AuthorityTargetKind.REJECTED_FEATURE],
            tuple(success.rejected_features),
        ),
        AuthorityTargetKind.GAP: (
            _ORDINAL_MAPPING_PREFIXES[AuthorityTargetKind.GAP],
            tuple(success.gaps),
        ),
        AuthorityTargetKind.ASSUMPTION: (
            _ORDINAL_MAPPING_PREFIXES[AuthorityTargetKind.ASSUMPTION],
            tuple(canonical_assumption_key(item) for item in success.assumptions),
        ),
    }


def _ordinal_target(
    authority_item_id: str,
    *,
    prefixes: tuple[str, ...],
) -> tuple[str, int] | None:
    """Parse only canonical positive ordinal target IDs, not hashed IDs."""
    for prefix in prefixes:
        marker = f"{prefix}-"
        if not authority_item_id.startswith(marker):
            continue
        suffix = authority_item_id.removeprefix(marker)
        if suffix.isdecimal() and suffix == str(int(suffix)) and int(suffix) > 0:
            return prefix, int(suffix)
    return None


def _rebind_ordinal_mappings(
    mappings: list[SpecAuthorityMapping],
    *,
    original: _OrdinalMappingCollections,
    canonical: _OrdinalMappingCollections,
) -> list[SpecAuthorityMapping] | None:
    """Keep ordinal mappings on the same unique semantic item after sorting."""
    rewritten: list[SpecAuthorityMapping] = []
    for mapping in mappings:
        collection = original.get(mapping.authority_target_kind)
        if collection is None:
            rewritten.append(mapping)
            continue
        prefixes, original_identities = collection
        ordinal_target = _ordinal_target(
            mapping.authority_item_id,
            prefixes=prefixes,
        )
        if ordinal_target is None:
            rewritten.append(mapping)
            continue
        prefix, ordinal = ordinal_target
        if ordinal > len(original_identities):
            return None
        identity = original_identities[ordinal - 1]
        canonical_identities = canonical[mapping.authority_target_kind][1]
        if (
            original_identities.count(identity) != 1
            or canonical_identities.count(identity) != 1
        ):
            return None
        canonical_ordinal = canonical_identities.index(identity) + 1
        rewritten.append(
            mapping.model_copy(
                update={"authority_item_id": f"{prefix}-{canonical_ordinal}"}
            )
        )
    return rewritten


def _validated_output(
    success: SpecAuthorityCompilationSuccess,
) -> SpecAuthorityCompilerOutput:
    """Revalidate the fully normalized model and fail closed on host drift."""
    try:
        return SpecAuthorityCompilerOutput(root=success)
    except ValidationError as error:
        return _validation_failure(error, subject="Normalized compiler output")


def _canonicalized_output(
    success: SpecAuthorityCompilationSuccess,
    *,
    original_ordinal_collections: _OrdinalMappingCollections,
) -> SpecAuthorityCompilerOutput:
    """Sort set-like fields while preserving exact compact-IR mapping targets."""
    _sort_success(success)
    canonical_collections = _ordinal_mapping_collections(success)
    rewritten_mappings = _rebind_ordinal_mappings(
        success.authority_mappings,
        original=original_ordinal_collections,
        canonical=canonical_collections,
    )
    if rewritten_mappings is None:
        return _failure(
            reason="JSON_VALIDATION_FAILED",
            blocking_gaps=[
                "Normalized compiler output schema error: ordinal compact-IR "
                "mapping identity is ambiguous after canonicalization."
            ],
        )
    success.authority_mappings = rewritten_mappings
    return _validated_output(success)


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
    original_ordinal_collections = _ordinal_mapping_collections(success)
    old_to_new, rewritten_invariants = _rewrite_invariant_ids(success)
    reference_failure: SpecAuthorityCompilerOutput | None = None
    if old_to_new is None:
        reference_failure = _failure(
            reason="JSON_VALIDATION_FAILED",
            blocking_gaps=[
                "Normalized compiler output schema error: ambiguous repeated "
                "invariant identity."
            ],
        )
    else:
        rewritten_source_map = _rewrite_source_map_ids(
            success.source_map,
            old_to_new=old_to_new,
        )
        rewritten_authority_mappings = _rewrite_authority_mapping_ids(
            success.authority_mappings,
            old_to_new=old_to_new,
        )
        if rewritten_source_map is None:
            reference_failure = _source_failure(
                "Compiler source-map identities do not match emitted invariants."
            )
        elif rewritten_authority_mappings is None:
            reference_failure = _failure(
                reason="JSON_VALIDATION_FAILED",
                blocking_gaps=[
                    "Normalized compiler output schema error: compact-IR invariant "
                    "mapping identities do not match emitted invariants."
                ],
            )
        else:
            success.source_map = rewritten_source_map
            success.authority_mappings = rewritten_authority_mappings
    if reference_failure is not None:
        return reference_failure
    success.invariants = rewritten_invariants
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
    return _canonicalized_output(
        success,
        original_ordinal_collections=original_ordinal_collections,
    )


__all__ = ["normalize_compiler_output"]
