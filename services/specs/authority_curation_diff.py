"""Deterministic diff helpers for authority curation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JsonDict = dict[str, Any]


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

    return {
        "summary": {
            "unchanged_count": len(unchanged_ids),
            "changed_count": len(changed_ids) + len(removed_ids),
            "removed_count": len(removed_ids),
            "added_count": len(added_ids),
            "untargeted_change_count": len(untargeted_changes),
        },
        "unchanged_ids": unchanged_ids,
        "changed_ids": changed_ids,
        "removed_ids": removed_ids,
        "added_ids": added_ids,
        "lineage_json": lineage_json,
        "untargeted_changes": untargeted_changes,
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
        item_id = item.get("id")
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
        result[item_id] = dict(item)
    if validation_errors:
        raise AuthorityDiffValidationError(validation_errors)
    return result


def _canonical_payload(invariant: JsonDict) -> object:
    """Return payload fields used to decide whether a same-id item changed."""
    return _canonical_json_value(
        {key: value for key, value in invariant.items() if key != "id"}
    )


def _canonical_json_value(value: object) -> object:
    """Normalize JSON-like values for deterministic equality checks."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_json_value(value[key])
            for key in sorted(value, key=str)
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
