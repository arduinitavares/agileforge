"""Closed validation for stable references to one accepted Specification."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from utils.agileforge_spec_profile_v2 import (
    RequirementLevel,
    SpecificationItem,
    SpecificationPayload,
    SpecItemType,
    canonical_spec_hash,
    canonical_spec_json,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_NORMATIVE_TYPES = frozenset(
    {
        SpecItemType.REQ,
        SpecItemType.QUALITY,
        SpecItemType.CONSTRAINT,
        SpecItemType.INTERFACE,
        SpecItemType.DATA,
    }
)
_NORMATIVE_LEVELS = frozenset(
    {
        RequirementLevel.MUST,
        RequirementLevel.MUST_NOT,
        RequirementLevel.SHOULD,
        RequirementLevel.MAY,
    }
)
_BACKLOG_ITEM_ID_PATTERN = re.compile(r"^PBI-[0-9]{6}$")
_STORY_ITEM_ID_PATTERN = re.compile(r"^US-[0-9]{4}$")
_MAX_BACKLOG_ITEMS = 999999
_MAX_STORY_ITEMS = 8


class SpecificationReferenceError(ValueError):
    """Bounded actionable failures from one Specification reference pass."""

    def __init__(self, errors: Iterable[str]) -> None:
        """Store deterministic actionable failures for one reference boundary."""
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class AcceptedSpecificationReference(BaseModel):
    """Exact accepted Specification identity and parsed canonical payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version_id: int = Field(gt=0)
    spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    canonical_specification_json: str = Field(min_length=1)
    payload: SpecificationPayload

    @model_validator(mode="after")
    def validate_exact_identity(self) -> Self:
        """Prove the reference owns exactly one canonical payload identity."""
        if canonical_spec_json(self.payload) != self.canonical_specification_json:
            message = "canonical Specification bytes do not match payload"
            raise ValueError(message)
        if canonical_spec_hash(self.payload) != self.spec_hash:
            message = "canonical Specification hash does not match payload"
            raise ValueError(message)
        return self


def validate_accepted_specification_root(
    *,
    spec_hash: str,
    canonical_specification_json: str,
) -> SpecificationPayload:
    """Parse and prove the exact canonical Specification root contract."""
    try:
        payload = SpecificationPayload.model_validate_json(canonical_specification_json)
    except ValidationError as exc:
        message = "accepted Specification JSON is invalid"
        raise ValueError(message) from exc
    if canonical_spec_json(payload) != canonical_specification_json:
        message = "accepted Specification JSON must use exact canonical bytes"
        raise ValueError(message)
    if canonical_spec_hash(payload) != spec_hash:
        message = "accepted Specification hash does not match canonical JSON"
        raise ValueError(message)
    return payload


def require_nonblank_text(value: str, *, field_name: str) -> str:
    """Reject blank canonical text without altering any supplied nonblank bytes."""
    if not value.strip():
        message = f"{field_name} must not be blank"
        raise ValueError(message)
    return value


def validate_backlog_item_id(value: str) -> str:
    """Accept only the host-minted PBI range PBI-000001 through PBI-999999."""
    if not _BACKLOG_ITEM_ID_PATTERN.fullmatch(value) or not 1 <= int(value[4:]) <= (
        _MAX_BACKLOG_ITEMS
    ):
        message = "Backlog item ID must be PBI-000001 through PBI-999999"
        raise ValueError(message)
    return value


def validate_story_item_id(value: str) -> str:
    """Accept only host-minted Story item IDs US-0001 through US-0008."""
    if not _STORY_ITEM_ID_PATTERN.fullmatch(value) or not 1 <= int(value[3:]) <= (
        _MAX_STORY_ITEMS
    ):
        message = "Story item ID must be US-0001 through US-0008"
        raise ValueError(message)
    return value


def validate_canonical_spec_item_ids(spec_item_ids: Iterable[str]) -> tuple[str, ...]:
    """Require a nonempty lexicographically sorted unique canonical evidence set."""
    ids = tuple(spec_item_ids)
    if not ids:
        message = "Specification item IDs must not be empty"
        raise ValueError(message)
    if tuple(sorted(set(ids))) != ids:
        message = "Specification item IDs must be unique and sorted"
        raise ValueError(message)
    return ids


def has_qualifying_normative_evidence(items: Iterable[SpecificationItem]) -> bool:
    """Return whether an evidence set contains one actionable normative item."""
    return any(
        item.type in _NORMATIVE_TYPES and item.level in _NORMATIVE_LEVELS
        for item in items
    )


def canonical_spec_item_ids(
    specification: AcceptedSpecificationReference,
    spec_item_ids: Iterable[str],
    *,
    parent_spec_item_ids: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Validate and canonically sort a bounded evidence set in one pass."""
    ids = tuple(spec_item_ids)
    known_items = {item.id: item for item in specification.payload.items}
    parent_ids: frozenset[str] | None = None
    if parent_spec_item_ids is not None:
        canonical_parent_ids = validate_canonical_spec_item_ids(parent_spec_item_ids)
        canonical_spec_item_ids(specification, canonical_parent_ids)
        parent_ids = frozenset(canonical_parent_ids)
    errors: list[str] = []
    seen: set[str] = set()
    canonical_ids: list[str] = []

    if not ids:
        errors.append("empty Specification item reference set")
    for item_id in ids:
        if item_id in seen:
            errors.append(f"duplicate Specification item ID: {item_id}")
            continue
        seen.add(item_id)
        item = known_items.get(item_id)
        if item is None:
            errors.append(f"unknown Specification item ID: {item_id}")
            continue
        if parent_ids is not None and item_id not in parent_ids:
            errors.append(
                f"Specification item ID outside the parent boundary: {item_id}"
            )
            continue
        canonical_ids.append(item_id)

    if not has_qualifying_normative_evidence(
        known_items[item_id] for item_id in canonical_ids
    ):
        errors.append("Specification references require qualifying normative evidence")
    if errors:
        raise SpecificationReferenceError(errors)
    return tuple(sorted(canonical_ids))


def derived_referenced_spec_item_ids(
    *reference_sets: Iterable[str],
) -> tuple[str, ...]:
    """Return the canonical host-derived union of validated evidence sets."""
    item_ids = {
        item_id for reference_set in reference_sets for item_id in reference_set
    }
    return tuple(sorted(item_ids))


__all__ = [
    "AcceptedSpecificationReference",
    "SpecificationReferenceError",
    "canonical_spec_item_ids",
    "derived_referenced_spec_item_ids",
    "has_qualifying_normative_evidence",
    "require_nonblank_text",
    "validate_accepted_specification_root",
    "validate_backlog_item_id",
    "validate_canonical_spec_item_ids",
    "validate_story_item_id",
]
