"""Deterministic diff helpers for authority curation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JsonDict = dict[str, Any]
_DIFF_COLLECTIONS = ("invariants", "assumptions", "gaps")


@dataclass(frozen=True)
class _DiffIds:
    """Grouped invariant id sets for one authority diff."""

    changed_ids: list[str]
    removed_ids: list[str]
    added_ids: list[str]


class AuthorityDiffValidationError(ValueError):
    """Raised when authority JSON cannot be diffed safely."""

    def __init__(self, validation_errors: list[JsonDict]) -> None:
        """Initialize with sanitized validation errors."""
        super().__init__("Authority diff input validation failed.")
        self.validation_errors = validation_errors


def build_authority_diff(
    *,
    source_authority_json: JsonDict,
    candidate_authority_json: JsonDict,
    targeted_source_item_ids: set[str],
    targeted_collection_keys: dict[str, set[str]] | None = None,
) -> JsonDict:
    """Return bounded diff and lineage for a curation candidate."""
    source_by_id = _invariants_by_id(
        source_authority_json,
        authority="source",
    )
    candidate_by_id = _invariants_by_id(
        candidate_authority_json,
        authority="candidate",
    )
    source_ids = set(source_by_id)
    candidate_ids = set(candidate_by_id)
    common_ids = source_ids & candidate_ids

    changed_ids = sorted(
        invariant_id
        for invariant_id in common_ids
        if _canonical_payload(source_by_id[invariant_id])
        != _canonical_payload(candidate_by_id[invariant_id])
    )
    unchanged_ids = sorted(common_ids - set(changed_ids))
    removed_ids = sorted(source_ids - candidate_ids)
    added_ids = sorted(candidate_ids - source_ids)
    diff_ids = _DiffIds(
        changed_ids=changed_ids,
        removed_ids=removed_ids,
        added_ids=added_ids,
    )
    lineage_json = _build_lineage(
        source_by_id=source_by_id,
        candidate_by_id=candidate_by_id,
        diff_ids=diff_ids,
    )
    untargeted_changes = _untargeted_changes(
        source_by_id=source_by_id,
        candidate_by_id=candidate_by_id,
        diff_ids=diff_ids,
        targeted_source_item_ids=targeted_source_item_ids,
    )
    collections = _collection_diffs(
        source_authority_json=source_authority_json,
        candidate_authority_json=candidate_authority_json,
    )
    collection_untargeted_changes = _untargeted_collection_changes(
        collections=collections,
        targeted_collection_keys=targeted_collection_keys,
    )
    all_untargeted_changes = [
        *untargeted_changes,
        *collection_untargeted_changes,
    ]

    return {
        "summary": {
            "unchanged_count": len(unchanged_ids),
            "changed_count": len(changed_ids) + len(removed_ids),
            "removed_count": len(removed_ids),
            "added_count": len(added_ids),
            "untargeted_change_count": len(all_untargeted_changes),
        },
        "unchanged_ids": unchanged_ids,
        "changed_ids": changed_ids,
        "removed_ids": removed_ids,
        "added_ids": added_ids,
        "lineage_json": lineage_json,
        "untargeted_changes": all_untargeted_changes,
        "collections": collections,
    }


def _invariants_by_id(
    authority_json: JsonDict,
    *,
    authority: str,
) -> dict[str, JsonDict]:
    """Return invariant objects keyed by stable id."""
    invariants = authority_json.get("invariants")
    if not isinstance(invariants, list):
        raise AuthorityDiffValidationError(
            [
                {
                    "authority": authority,
                    "reason": "invariants_not_list",
                }
            ]
        )
    result: dict[str, JsonDict] = {}
    first_indexes: dict[str, int] = {}
    validation_errors: list[JsonDict] = []
    for index, item in enumerate(invariants):
        if not isinstance(item, dict):
            validation_errors.append(
                {
                    "authority": authority,
                    "index": index,
                    "reason": "invalid_invariant_object",
                }
            )
            continue
        item_payload = {str(key): value for key, value in item.items()}
        item_id = item_payload.get("id")
        if not isinstance(item_id, str) or not item_id:
            validation_errors.append(
                {
                    "authority": authority,
                    "index": index,
                    "reason": "missing_or_invalid_id",
                }
            )
            continue
        if item_id in result:
            validation_errors.append(
                {
                    "authority": authority,
                    "duplicate_id": item_id,
                    "first_index": first_indexes[item_id],
                    "index": index,
                    "reason": "duplicate_id",
                }
            )
            continue
        first_indexes[item_id] = index
        result[item_id] = item_payload
    if validation_errors:
        raise AuthorityDiffValidationError(validation_errors)
    return result


def _collection_diffs(
    *,
    source_authority_json: JsonDict,
    candidate_authority_json: JsonDict,
) -> dict[str, JsonDict]:
    """Return per-collection changed/added/removed ids."""
    result: dict[str, JsonDict] = {}
    for collection in _DIFF_COLLECTIONS:
        source_by_key = _collection_by_key(
            source_authority_json,
            authority="source",
            collection=collection,
        )
        candidate_by_key = _collection_by_key(
            candidate_authority_json,
            authority="candidate",
            collection=collection,
        )
        source_keys = set(source_by_key)
        candidate_keys = set(candidate_by_key)
        common_keys = source_keys & candidate_keys
        changed_ids = sorted(
            key
            for key in common_keys
            if _canonical_collection_payload(source_by_key[key])
            != _canonical_collection_payload(candidate_by_key[key])
        )
        result[collection] = {
            "unchanged_ids": sorted(common_keys - set(changed_ids)),
            "changed_ids": changed_ids,
            "removed_ids": sorted(source_keys - candidate_keys),
            "added_ids": sorted(candidate_keys - source_keys),
        }
    return result


def _collection_by_key(
    authority_json: JsonDict,
    *,
    authority: str,
    collection: str,
) -> dict[str, JsonDict]:
    """Return one authority collection keyed by review-visible id."""
    if collection == "invariants":
        return _invariants_by_id(authority_json, authority=authority)

    value = authority_json.get(collection, [])
    if not isinstance(value, list):
        raise AuthorityDiffValidationError(
            [
                {
                    "authority": authority,
                    "collection": collection,
                    "reason": "collection_not_list",
                }
            ]
        )
    result: dict[str, JsonDict] = {}
    first_indexes: dict[str, int] = {}
    validation_errors: list[JsonDict] = []
    for index, item in enumerate(value):
        keyed = _collection_item_key_and_payload(
            item,
            collection=collection,
            index=index,
        )
        if keyed is None:
            validation_errors.append(
                {
                    "authority": authority,
                    "collection": collection,
                    "index": index,
                    "reason": "invalid_collection_item",
                }
            )
            continue
        key, payload = keyed
        if key in result:
            validation_errors.append(
                {
                    "authority": authority,
                    "collection": collection,
                    "duplicate_id": key,
                    "first_index": first_indexes[key],
                    "index": index,
                    "reason": "duplicate_id",
                }
            )
            continue
        first_indexes[key] = index
        result[key] = payload
    if validation_errors:
        raise AuthorityDiffValidationError(validation_errors)
    return result


def _collection_item_key_and_payload(
    item: object,
    *,
    collection: str,
    index: int,
) -> tuple[str, JsonDict] | None:
    """Return review-visible key and payload for one collection item."""
    prefix = _collection_review_prefix(collection)
    fallback_key = f"{prefix}-{index + 1}"
    if isinstance(item, str):
        return fallback_key, {"text": item}
    if not isinstance(item, dict):
        return None
    payload = {str(key): value for key, value in item.items()}
    key_fields = (
        ("assumption_id", "id")
        if collection == "assumptions"
        else ("gap_id", "id")
    )
    for field_name in key_fields:
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            return value, payload
    return fallback_key, payload


def _collection_review_prefix(collection: str) -> str:
    """Return review-visible prefix for a non-invariant collection."""
    return "ASM" if collection == "assumptions" else "GAP"


def _canonical_payload(invariant: JsonDict) -> object:
    """Return payload fields used to decide whether a same-id item changed."""
    return _canonical_json_value(
        {key: value for key, value in invariant.items() if key != "id"}
    )


def _canonical_collection_payload(item: JsonDict) -> object:
    """Return payload fields used to decide same-key collection changes."""
    return _canonical_json_value(
        {
            key: value
            for key, value in item.items()
            if key not in {"id", "assumption_id", "gap_id"}
        }
    )


def _canonical_json_value(value: object) -> object:
    """Normalize JSON-like values for deterministic equality checks."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    return value


def _build_lineage(
    *,
    source_by_id: dict[str, JsonDict],
    candidate_by_id: dict[str, JsonDict],
    diff_ids: _DiffIds,
) -> JsonDict:
    """Build old invariant id to candidate invariant id lineage."""
    lineage: JsonDict = {}
    unused_added_ids = set(diff_ids.added_ids)
    for old_id in diff_ids.removed_ids:
        old_source_item_id = _source_item_id(source_by_id[old_id])
        replacement_id = _next_replacement_id(
            candidate_by_id=candidate_by_id,
            added_ids=sorted(unused_added_ids),
            source_item_id=old_source_item_id,
        )
        if replacement_id is not None:
            unused_added_ids.remove(replacement_id)
        lineage[old_id] = {
            "old_id": old_id,
            "new_id": replacement_id,
            "source_item_id": old_source_item_id,
            "reason": "targeted_repair" if replacement_id else "removed_by_repair",
        }
    for invariant_id in diff_ids.changed_ids:
        source_item_id = _source_item_id(source_by_id[invariant_id])
        lineage[invariant_id] = {
            "old_id": invariant_id,
            "new_id": invariant_id,
            "source_item_id": source_item_id,
            "reason": "modified_in_place",
        }
    return lineage


def _next_replacement_id(
    *,
    candidate_by_id: dict[str, JsonDict],
    added_ids: list[str],
    source_item_id: str,
) -> str | None:
    """Return the first deterministic added invariant for the same source item."""
    if not source_item_id:
        return None
    for candidate_id in added_ids:
        if _source_item_id(candidate_by_id[candidate_id]) == source_item_id:
            return candidate_id
    return None


def _untargeted_changes(
    *,
    source_by_id: dict[str, JsonDict],
    candidate_by_id: dict[str, JsonDict],
    diff_ids: _DiffIds,
    targeted_source_item_ids: set[str],
) -> list[JsonDict]:
    """Return changes whose source item ids are outside the feedback target set."""
    changes: list[JsonDict] = []
    for old_id in diff_ids.removed_ids:
        source_item_id = _source_item_id(source_by_id[old_id])
        if source_item_id not in targeted_source_item_ids:
            changes.append(
                {
                    "change_type": "removed",
                    "id": old_id,
                    "source_item_id": source_item_id,
                }
            )
    for invariant_id in diff_ids.changed_ids:
        old_source_item_id = _source_item_id(source_by_id[invariant_id])
        new_source_item_id = _source_item_id(candidate_by_id[invariant_id])
        if not _source_ids_are_targeted(
            {old_source_item_id, new_source_item_id},
            targeted_source_item_ids=targeted_source_item_ids,
        ):
            changes.append(
                {
                    "change_type": "changed",
                    "id": invariant_id,
                    "source_item_id": old_source_item_id,
                    "candidate_source_item_id": new_source_item_id,
                }
            )
    for new_id in diff_ids.added_ids:
        source_item_id = _source_item_id(candidate_by_id[new_id])
        if source_item_id not in targeted_source_item_ids:
            changes.append(
                {
                    "change_type": "added",
                    "id": new_id,
                    "source_item_id": source_item_id,
                }
            )
    return changes


def _untargeted_collection_changes(
    *,
    collections: dict[str, JsonDict],
    targeted_collection_keys: dict[str, set[str]] | None,
) -> list[JsonDict]:
    """Return non-invariant collection changes outside targeted review ids."""
    targeted = targeted_collection_keys or {}
    changes: list[JsonDict] = []
    for collection in ("assumptions", "gaps"):
        collection_diff = collections.get(collection, {})
        allowed_keys = targeted.get(collection, set())
        for change_type, field_name in (
            ("changed", "changed_ids"),
            ("removed", "removed_ids"),
            ("added", "added_ids"),
        ):
            ids = collection_diff.get(field_name)
            if not isinstance(ids, list):
                continue
            for item_id in ids:
                if not isinstance(item_id, str) or item_id in allowed_keys:
                    continue
                changes.append(
                    {
                        "collection": collection,
                        "change_type": change_type,
                        "id": item_id,
                    }
                )
    return changes


def _source_ids_are_targeted(
    source_item_ids: set[str],
    *,
    targeted_source_item_ids: set[str],
) -> bool:
    """Return whether all non-empty source item ids are targeted."""
    return bool(source_item_ids) and all(
        source_item_id in targeted_source_item_ids
        for source_item_id in source_item_ids
    )


def _source_item_id(invariant: JsonDict) -> str:
    """Return an invariant source item id as a deterministic string."""
    source_item_id = invariant.get("source_item_id")
    if isinstance(source_item_id, str):
        return source_item_id
    return ""
