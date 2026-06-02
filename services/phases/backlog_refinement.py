"""Backlog refinement schemas and host-owned transformation helpers."""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal, Never, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator_agent.agent_tools.backlog_primer.schemes import BacklogItem
from services.agent_workbench.fingerprints import canonical_hash

type BacklogClassification = Literal[
    "verification",
    "discovery",
    "product_new_work",
    "unchanged",
    "authority_gap_intake",
]
type JsonDict = dict[str, Any]
type OperationType = Literal[
    "split",
    "merge",
    "retitle",
    "rewrite_scope",
    "reorder",
    "classify",
    "authority_ref_change",
    "delete",
    "add_intake",
]

MIN_SPLIT_RESULT_ITEMS = 2
MIN_MERGE_SOURCE_ITEMS = 2
MIN_COMPLETE_BACKLOG_ITEMS = 10

HOST_ONLY_ITEM_KEYS: frozenset[str] = frozenset(
    {
        "item_id",
        "item_fingerprint",
        "source_attempt_id",
        "source_artifact_fingerprint",
        "as_built_annotation",
        "brownfield_warnings",
        "classification",
        "split_merge_provenance",
        "refinement_provenance",
        "intake_metadata",
    }
)
BACKLOG_ITEM_KEYS: frozenset[str] = frozenset(BacklogItem.model_fields)


class BacklogRefinementError(Exception):
    """Raised when a refinement operation cannot be applied safely."""


class AmbiguousRefinementDiffError(BacklogRefinementError):
    """Raised when edited artifacts cannot be mapped to deterministic operations."""


class UnsupportedAuthorityRefError(BacklogRefinementError):
    """Raised when an operation introduces unsupported implementation scope."""


def _raise_ambiguous_diff(message: str) -> Never:
    raise AmbiguousRefinementDiffError(message)


class BaseRefinementOperation(BaseModel):
    """Common host-validated refinement operation fields."""

    model_config = ConfigDict(extra="forbid")

    operation_id: Annotated[str, Field(min_length=1)]
    operation_type: OperationType
    source_item_ids: list[str] = Field(default_factory=list)
    source_item_fingerprints: list[str] = Field(default_factory=list)
    result_item_ids: list[str] = Field(default_factory=list)
    rationale: Annotated[str, Field(min_length=1)]
    requested_by: Literal["po", "agent", "developer"] = "agent"
    approval_request: dict[str, object] = Field(default_factory=dict)
    warnings: list[dict[str, object]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _source_fingerprints_match_source_ids(self) -> BaseRefinementOperation:
        if len(self.source_item_fingerprints) != len(self.source_item_ids):
            message = "source_item_fingerprints must match source_item_ids"
            raise ValueError(message)
        if len(set(self.result_item_ids)) != len(self.result_item_ids):
            message = "result_item_ids must be unique"
            raise ValueError(message)
        return self


def _validate_single_source_in_place(
    operation: BaseRefinementOperation,
    *,
    operation_name: str,
) -> None:
    if len(operation.source_item_ids) != 1:
        message = f"{operation_name} requires exactly one source_item_id"
        raise ValueError(message)
    if (
        operation.result_item_ids
        and operation.result_item_ids != operation.source_item_ids
    ):
        message = (
            f"{operation_name} result_item_ids must be empty or match source_item_ids"
        )
        raise ValueError(message)


class SplitOperation(BaseRefinementOperation):
    """Replace one source item with multiple result items."""

    operation_type: Literal["split"] = "split"
    result_items: list[dict[str, object]]

    @model_validator(mode="after")
    def _valid_split(self) -> SplitOperation:
        if len(self.source_item_ids) != 1:
            message = "split requires exactly one source_item_id"
            raise ValueError(message)
        if len(self.result_items) < MIN_SPLIT_RESULT_ITEMS:
            message = "split requires at least two result_items"
            raise ValueError(message)
        if len(self.result_item_ids) != len(self.result_items):
            message = "result_item_ids must match result_items"
            raise ValueError(message)
        return self


class MergeOperation(BaseRefinementOperation):
    """Combine multiple source items into one result item."""

    operation_type: Literal["merge"] = "merge"
    result_item: dict[str, object]

    @model_validator(mode="after")
    def _valid_merge(self) -> MergeOperation:
        if len(self.source_item_ids) < MIN_MERGE_SOURCE_ITEMS:
            message = "merge requires at least two source_item_ids"
            raise ValueError(message)
        if len(set(self.source_item_ids)) != len(self.source_item_ids):
            message = "merge source_item_ids must be unique"
            raise ValueError(message)
        if len(self.result_item_ids) != 1:
            message = "merge requires exactly one result_item_id"
            raise ValueError(message)
        return self


class RetitleOperation(BaseRefinementOperation):
    """Change only the backlog item title/requirement."""

    operation_type: Literal["retitle"] = "retitle"
    new_requirement: Annotated[str, Field(min_length=3)]

    @model_validator(mode="after")
    def _valid_retitle(self) -> RetitleOperation:
        _validate_single_source_in_place(self, operation_name="retitle")
        return self


class RewriteScopeOperation(BaseRefinementOperation):
    """Change backlog item text fields except identity/order."""

    operation_type: Literal["rewrite_scope"] = "rewrite_scope"
    field_updates: dict[str, object]

    @model_validator(mode="after")
    def _valid_rewrite(self) -> RewriteScopeOperation:
        _validate_single_source_in_place(self, operation_name="rewrite_scope")
        invalid = set(self.field_updates) - {
            "justification",
            "technical_note",
            "value_driver",
            "estimated_effort",
            "capability_hint",
        }
        if invalid:
            message = f"unsupported rewrite_scope fields: {sorted(invalid)}"
            raise ValueError(message)
        return self


class ReorderOperation(BaseRefinementOperation):
    """Replace item priority order with an explicit ordered id list."""

    operation_type: Literal["reorder"] = "reorder"
    ordered_item_ids: list[str]


class ClassifyOperation(BaseRefinementOperation):
    """Change host classification for one item."""

    operation_type: Literal["classify"] = "classify"
    classification: BacklogClassification

    @model_validator(mode="after")
    def _valid_classify(self) -> ClassifyOperation:
        _validate_single_source_in_place(self, operation_name="classify")
        return self


class AuthorityRefChangeOperation(BaseRefinementOperation):
    """Change authority reference on one item."""

    operation_type: Literal["authority_ref_change"] = "authority_ref_change"
    old_authority_ref: str | None = None
    new_authority_ref: str | None = None

    @model_validator(mode="after")
    def _valid_authority_ref_change(self) -> AuthorityRefChangeOperation:
        _validate_single_source_in_place(
            self,
            operation_name="authority_ref_change",
        )
        return self


class DeleteOperation(BaseRefinementOperation):
    """Delete one or more source items from the draft."""

    operation_type: Literal["delete"] = "delete"

    @model_validator(mode="after")
    def _valid_delete(self) -> DeleteOperation:
        if not self.source_item_ids:
            message = "delete requires at least one source_item_id"
            raise ValueError(message)
        if self.result_item_ids:
            message = "delete cannot define result_item_ids"
            raise ValueError(message)
        return self


class AddIntakeOperation(BaseRefinementOperation):
    """Add non-implementation authority-gap intake work."""

    operation_type: Literal["add_intake"] = "add_intake"
    result_item: dict[str, object]
    authority_gap_ref: Annotated[str, Field(min_length=1)]
    classification: Literal["authority_gap_intake"] = "authority_gap_intake"

    @model_validator(mode="after")
    def _valid_add_intake(self) -> AddIntakeOperation:
        if self.source_item_ids:
            message = "add_intake must not have source_item_ids"
            raise ValueError(message)
        if len(self.result_item_ids) != 1:
            message = "add_intake requires one result_item_id"
            raise ValueError(message)
        requirement = str(self.result_item.get("requirement", "")).strip().lower()
        forbidden_prefixes = ("build ", "add ", "implement ", "create ")
        if requirement.startswith(forbidden_prefixes):
            message = "authority_gap_intake cannot be implementation work"
            raise ValueError(message)
        return self


type RefinementOperation = Annotated[
    SplitOperation
    | MergeOperation
    | RetitleOperation
    | RewriteScopeOperation
    | ReorderOperation
    | ClassifyOperation
    | AuthorityRefChangeOperation
    | DeleteOperation
    | AddIntakeOperation,
    Field(discriminator="operation_type"),
]


class BacklogRefinementOperationSet(BaseModel):
    """A host-validated operation set against one source backlog attempt."""

    model_config = ConfigDict(extra="forbid")

    source_attempt_id: Annotated[str, Field(min_length=1)]
    source_artifact_fingerprint: Annotated[str, Field(min_length=1)]
    authority_fingerprint: Annotated[str, Field(min_length=1)]
    as_built_cache_fingerprint: Annotated[str, Field(min_length=1)]
    operations: list[RefinementOperation]


def _item_fingerprint(item: JsonDict) -> str:
    stable_item = copy.deepcopy(item)
    stable_item.pop("item_id", None)
    stable_item.pop("item_fingerprint", None)
    return canonical_hash({"backlog_item": stable_item})


def assign_item_identity(
    artifact: JsonDict,
    *,
    source_attempt_id: str,
    source_artifact_fingerprint: str,
) -> JsonDict:
    """Return a copy of artifact with stable host item identity."""
    normalized = copy.deepcopy(artifact)
    items = normalized.get("backlog_items")
    if not isinstance(items, list):
        normalized["backlog_items"] = []
        return normalized
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        item_data = cast("JsonDict", item)
        if not item_data.get("item_id"):
            item_data["item_id"] = f"item-{index:03d}"
        item_data["source_attempt_id"] = source_attempt_id
        item_data["source_artifact_fingerprint"] = source_artifact_fingerprint
        item_data["item_fingerprint"] = _item_fingerprint(item_data)
    return normalized


def canonical_operations_fingerprint(
    operation_set: BacklogRefinementOperationSet,
) -> str:
    """Return canonical fingerprint for a refinement operation set."""
    return canonical_hash(
        {
            "schema_version": "agileforge.backlog_refinement_operations.v1",
            "operation_set": operation_set.model_dump(mode="json"),
        }
    )


def project_savable_backlog_items(
    artifact: JsonDict,
) -> list[JsonDict]:
    """Return BacklogItem-compatible implementation items from artifact."""
    raw_items = artifact.get("backlog_items")
    if not isinstance(raw_items, list):
        return []
    projected: list[JsonDict] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item = cast("JsonDict", raw_item)
        if item.get("classification") == "authority_gap_intake":
            continue
        item_payload = {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key in BACKLOG_ITEM_KEYS and key not in HOST_ONLY_ITEM_KEYS
        }
        projected.append(BacklogItem.model_validate(item_payload).model_dump())
    return projected


def normalize_refined_artifact(artifact: JsonDict) -> JsonDict:
    """Normalize priorities and recompute item fingerprints for a refined draft."""
    normalized = copy.deepcopy(artifact)
    raw_items = normalized.get("backlog_items")
    if not isinstance(raw_items, list):
        normalized["backlog_items"] = []
        normalized["is_complete"] = False
        return normalized

    valid_items: list[JsonDict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_data = cast("JsonDict", item)
        priority = len(valid_items) + 1
        item_data["priority"] = priority
        item_data["item_fingerprint"] = _item_fingerprint(item_data)
        valid_items.append(item_data)

    normalized["backlog_items"] = valid_items
    normalized["is_complete"] = len(valid_items) >= MIN_COMPLETE_BACKLOG_ITEMS and (
        not normalized.get("clarifying_questions")
    )
    return normalized


def operations_from_edited_artifact(
    source_artifact: JsonDict,
    edited_artifact: JsonDict,
    *,
    authority_fingerprint: str,
    as_built_cache_fingerprint: str,
) -> BacklogRefinementOperationSet:
    """Derive deterministic refinement operations from source-linked edits."""
    source_items = _source_items_for_diff(source_artifact)
    edited_items = _edited_items_for_diff(edited_artifact)
    source_attempt_id = _source_attempt_id_for_diff(source_items)
    source_artifact_fingerprint = _source_artifact_fingerprint_for_diff(source_items)
    edited_by_source_id = _edited_items_by_source_id(edited_items, source_items)
    _assert_no_order_or_priority_edit(source_items, edited_items)

    operations: list[RefinementOperation] = []
    operation_index = 1
    for source_id, source_item in source_items.items():
        edited_item = edited_by_source_id.get(source_id)
        if edited_item is None:
            operations.append(
                DeleteOperation(
                    operation_id=f"op-{operation_index:03d}-delete-{source_id}",
                    source_item_ids=[source_id],
                    source_item_fingerprints=[
                        _source_item_fingerprint_for_diff(source_item)
                    ],
                    rationale="Source-linked item removed from edited artifact.",
                    requested_by="po",
                )
            )
            operation_index += 1
            continue

        field_updates = _diff_backlog_item_fields(source_item, edited_item)
        if not field_updates:
            continue
        if set(field_updates) == {"requirement"}:
            operations.append(
                RetitleOperation(
                    operation_id=f"op-{operation_index:03d}-retitle-{source_id}",
                    source_item_ids=[source_id],
                    source_item_fingerprints=[
                        _source_item_fingerprint_for_diff(source_item)
                    ],
                    result_item_ids=[source_id],
                    new_requirement=str(field_updates["requirement"]),
                    rationale=(
                        "Source-linked item requirement changed in edited artifact."
                    ),
                    requested_by="po",
                )
            )
            operation_index += 1
            continue
        message = (
            "ambiguous edited artifact diff for "
            f"{source_id}: unsupported field changes {sorted(field_updates)}"
        )
        raise AmbiguousRefinementDiffError(message)
    if not operations:
        _raise_ambiguous_diff(
            "ambiguous edited artifact: no-op import has no deterministic changes",
        )

    return BacklogRefinementOperationSet(
        source_attempt_id=source_attempt_id,
        source_artifact_fingerprint=source_artifact_fingerprint,
        authority_fingerprint=authority_fingerprint,
        as_built_cache_fingerprint=as_built_cache_fingerprint,
        operations=operations,
    )


def _source_items_for_diff(
    source_artifact: JsonDict,
) -> dict[str, JsonDict]:
    raw_items = source_artifact.get("backlog_items")
    if not isinstance(raw_items, list):
        _raise_ambiguous_diff(
            "ambiguous source artifact: backlog_items must be a list",
        )
    source_items: dict[str, JsonDict] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            _raise_ambiguous_diff(
                "ambiguous source artifact: backlog_items must contain objects",
            )
        item = cast("JsonDict", raw_item)
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            _raise_ambiguous_diff(
                "ambiguous source artifact: source items require item_id",
            )
        if item_id in source_items:
            _raise_ambiguous_diff(
                f"ambiguous source artifact: duplicate item_id {item_id}",
            )
        source_items[item_id] = item
    return source_items


def _edited_items_for_diff(
    edited_artifact: JsonDict,
) -> list[JsonDict]:
    raw_items = edited_artifact.get("backlog_items")
    if not isinstance(raw_items, list):
        _raise_ambiguous_diff(
            "ambiguous edited artifact: backlog_items must be a list",
        )
    edited_items: list[JsonDict] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            _raise_ambiguous_diff(
                "ambiguous edited artifact: backlog_items must contain objects",
            )
        edited_items.append(cast("JsonDict", raw_item))
    return edited_items


def _source_attempt_id_for_diff(source_items: dict[str, JsonDict]) -> str:
    source_attempt_ids = {
        str(item.get("source_attempt_id"))
        for item in source_items.values()
        if isinstance(item.get("source_attempt_id"), str)
        and str(item.get("source_attempt_id")).strip()
    }
    if len(source_attempt_ids) != 1:
        _raise_ambiguous_diff(
            "ambiguous source artifact: source_attempt_id is missing or inconsistent",
        )
    return next(iter(source_attempt_ids))


def _source_artifact_fingerprint_for_diff(
    source_items: dict[str, JsonDict],
) -> str:
    source_fingerprints = {
        str(item.get("source_artifact_fingerprint"))
        for item in source_items.values()
        if isinstance(item.get("source_artifact_fingerprint"), str)
        and str(item.get("source_artifact_fingerprint")).strip()
    }
    if len(source_fingerprints) != 1:
        _raise_ambiguous_diff(
            "ambiguous source artifact: source_artifact_fingerprint is missing "
            "or inconsistent",
        )
    return next(iter(source_fingerprints))


def _edited_items_by_source_id(
    edited_items: list[JsonDict],
    source_items: dict[str, JsonDict],
) -> dict[str, JsonDict]:
    edited_by_source_id: dict[str, JsonDict] = {}
    for edited_item in edited_items:
        source_item_id = edited_item.get("source_item_id")
        if not isinstance(source_item_id, str) or not source_item_id.strip():
            _raise_ambiguous_diff(
                "ambiguous edited artifact: edited items must preserve source_item_id",
            )
        if source_item_id not in source_items:
            _raise_ambiguous_diff(
                f"ambiguous edited artifact: unknown source_item_id {source_item_id}",
            )
        if source_item_id in edited_by_source_id:
            _raise_ambiguous_diff(
                f"ambiguous edited artifact: duplicate source_item_id {source_item_id}",
            )
        edited_by_source_id[source_item_id] = edited_item
    return edited_by_source_id


def _assert_no_order_or_priority_edit(
    source_items: dict[str, JsonDict],
    edited_items: list[JsonDict],
) -> None:
    source_order = list(source_items)
    edited_order = [str(item["source_item_id"]) for item in edited_items]
    source_positions = {
        source_id: index for index, source_id in enumerate(source_order)
    }
    edited_positions = [source_positions[source_id] for source_id in edited_order]
    if edited_positions != sorted(edited_positions):
        _raise_ambiguous_diff(
            "ambiguous edited artifact: reorder edits are not supported",
        )
    if len(edited_order) != len(source_order):
        return
    for index, source_id in enumerate(edited_order, start=1):
        edited_priority = edited_items[index - 1].get("priority")
        source_priority = source_items[source_id].get("priority")
        if edited_priority != source_priority:
            _raise_ambiguous_diff(
                "ambiguous edited artifact: priority edits are not supported",
            )


def _source_item_fingerprint_for_diff(item: JsonDict) -> str:
    fingerprint = item.get("item_fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return fingerprint
    return _item_fingerprint(item)


def _diff_backlog_item_fields(
    source_item: JsonDict,
    edited_item: JsonDict,
) -> JsonDict:
    field_updates: JsonDict = {}
    for key in sorted(BACKLOG_ITEM_KEYS):
        if key == "priority":
            continue
        if source_item.get(key) != edited_item.get(key):
            field_updates[key] = copy.deepcopy(edited_item.get(key))
    return field_updates


def _items_by_id(artifact: JsonDict) -> dict[str, JsonDict]:
    raw_items = artifact.get("backlog_items")
    if not isinstance(raw_items, list):
        return {}
    items_by_id: dict[str, JsonDict] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item = cast("JsonDict", raw_item)
        item_id = item.get("item_id")
        if item_id:
            items_by_id[str(item_id)] = item
    return items_by_id


def _assert_unique_item_ids(items: list[JsonDict]) -> None:
    seen_item_ids: set[str] = set()
    duplicate_item_ids: set[str] = set()
    for item in items:
        item_id = item.get("item_id")
        if not item_id:
            continue
        canonical_item_id = str(item_id)
        if canonical_item_id in seen_item_ids:
            duplicate_item_ids.add(canonical_item_id)
        seen_item_ids.add(canonical_item_id)
    if duplicate_item_ids:
        message = f"duplicate source artifact item_ids: {sorted(duplicate_item_ids)}"
        raise BacklogRefinementError(message)


def _assert_source_matches(
    items_by_id: dict[str, JsonDict],
    operation: BaseRefinementOperation,
) -> None:
    for source_id, expected_fingerprint in zip(
        operation.source_item_ids,
        operation.source_item_fingerprints,
        strict=True,
    ):
        item = items_by_id.get(source_id)
        if item is None:
            message = f"source item not found: {source_id}"
            raise BacklogRefinementError(message)
        if _item_fingerprint(item) != expected_fingerprint:
            message = f"source item stale: {source_id}"
            raise BacklogRefinementError(message)


def _provenance(
    operation: BaseRefinementOperation,
    operation_set: BacklogRefinementOperationSet,
) -> dict[str, object]:
    return {
        "operation_id": operation.operation_id,
        "operation_type": operation.operation_type,
        "source_item_ids": list(operation.source_item_ids),
        "source_item_fingerprints": list(operation.source_item_fingerprints),
        "source_artifact_fingerprint": operation_set.source_artifact_fingerprint,
        "authority_fingerprint": operation_set.authority_fingerprint,
        "as_built_cache_fingerprint": operation_set.as_built_cache_fingerprint,
        "rationale": operation.rationale,
        "warnings": copy.deepcopy(operation.warnings),
    }


def _mark_provenance(
    item: JsonDict,
    operation: BaseRefinementOperation,
    operation_set: BacklogRefinementOperationSet,
) -> None:
    item["refinement_provenance"] = _provenance(operation, operation_set)


def _with_result_identity(
    item: dict[str, object],
    *,
    result_item_id: str,
    operation: BaseRefinementOperation,
    operation_set: BacklogRefinementOperationSet,
) -> JsonDict:
    result = cast("JsonDict", copy.deepcopy(item))
    result["item_id"] = result_item_id
    result["source_attempt_id"] = operation_set.source_attempt_id
    result["source_artifact_fingerprint"] = operation_set.source_artifact_fingerprint
    _mark_provenance(result, operation, operation_set)
    return result


def _assert_result_ids_do_not_reuse_current_item_ids(
    items: list[JsonDict],
    operation: BaseRefinementOperation,
) -> None:
    current_item_ids = {str(item["item_id"]) for item in items if item.get("item_id")}
    collisions = set(operation.result_item_ids) & current_item_ids
    if collisions:
        message = f"result_item_ids collide with current item ids: {sorted(collisions)}"
        raise BacklogRefinementError(message)


def _apply_split(
    items: list[JsonDict],
    operation: SplitOperation,
    operation_set: BacklogRefinementOperationSet,
) -> list[JsonDict]:
    source_id = operation.source_item_ids[0]
    _assert_result_ids_do_not_reuse_current_item_ids(items, operation)
    replacements = [
        _with_result_identity(
            result_item,
            result_item_id=result_item_id,
            operation=operation,
            operation_set=operation_set,
        )
        for result_item_id, result_item in zip(
            operation.result_item_ids,
            operation.result_items,
            strict=True,
        )
    ]

    refined_items: list[JsonDict] = []
    for item in items:
        if item.get("item_id") == source_id:
            refined_items.extend(replacements)
        else:
            refined_items.append(item)
    return refined_items


def _apply_merge(
    items: list[JsonDict],
    operation: MergeOperation,
    operation_set: BacklogRefinementOperationSet,
) -> list[JsonDict]:
    source_ids = set(operation.source_item_ids)
    _assert_result_ids_do_not_reuse_current_item_ids(items, operation)
    replacement = _with_result_identity(
        operation.result_item,
        result_item_id=operation.result_item_ids[0],
        operation=operation,
        operation_set=operation_set,
    )

    inserted = False
    refined_items: list[JsonDict] = []
    for item in items:
        if item.get("item_id") not in source_ids:
            refined_items.append(item)
            continue
        if not inserted:
            refined_items.append(replacement)
            inserted = True
    return refined_items


def _apply_retitle(
    items_by_id: dict[str, JsonDict],
    operation: RetitleOperation,
    operation_set: BacklogRefinementOperationSet,
) -> None:
    item = items_by_id[operation.source_item_ids[0]]
    item["requirement"] = operation.new_requirement
    _mark_provenance(item, operation, operation_set)


def _apply_rewrite_scope(
    items_by_id: dict[str, JsonDict],
    operation: RewriteScopeOperation,
    operation_set: BacklogRefinementOperationSet,
) -> None:
    item = items_by_id[operation.source_item_ids[0]]
    item.update(copy.deepcopy(operation.field_updates))
    _mark_provenance(item, operation, operation_set)


def _apply_reorder(
    items: list[JsonDict],
    operation: ReorderOperation,
) -> list[JsonDict]:
    items_by_id = _items_by_id({"backlog_items": items})
    current_item_ids = list(items_by_id)
    ordered_item_ids = operation.ordered_item_ids
    if len(ordered_item_ids) != len(current_item_ids) or set(ordered_item_ids) != set(
        current_item_ids
    ):
        message = "ordered_item_ids must match current backlog item ids"
        raise BacklogRefinementError(message)
    return [items_by_id[item_id] for item_id in ordered_item_ids]


def _apply_classify(
    items_by_id: dict[str, JsonDict],
    operation: ClassifyOperation,
    operation_set: BacklogRefinementOperationSet,
) -> None:
    item = items_by_id[operation.source_item_ids[0]]
    item["classification"] = operation.classification
    _mark_provenance(item, operation, operation_set)


def _apply_authority_ref_change(
    items_by_id: dict[str, JsonDict],
    operation: AuthorityRefChangeOperation,
    operation_set: BacklogRefinementOperationSet,
    supported_authority_refs: set[str] | None,
) -> None:
    item = items_by_id[operation.source_item_ids[0]]
    if (
        operation.old_authority_ref is not None
        and item.get("authority_ref") != operation.old_authority_ref
    ):
        message = f"authority ref stale: {operation.source_item_ids[0]}"
        raise BacklogRefinementError(message)
    if (
        supported_authority_refs is not None
        and operation.new_authority_ref is not None
        and operation.new_authority_ref not in supported_authority_refs
    ):
        message = f"unsupported authority ref: {operation.new_authority_ref}"
        raise UnsupportedAuthorityRefError(message)
    item["authority_ref"] = operation.new_authority_ref
    _mark_provenance(item, operation, operation_set)


def _apply_add_intake(
    refined: JsonDict,
    operation: AddIntakeOperation,
    operation_set: BacklogRefinementOperationSet,
) -> None:
    intake = _with_result_identity(
        operation.result_item,
        result_item_id=operation.result_item_ids[0],
        operation=operation,
        operation_set=operation_set,
    )
    intake["classification"] = "authority_gap_intake"
    intake["intake_metadata"] = {
        "authority_gap_ref": operation.authority_gap_ref,
        "operation_id": operation.operation_id,
    }

    raw_intake_items = refined.get("backlog_intake_items")
    if not isinstance(raw_intake_items, list):
        raw_intake_items = []
        refined["backlog_intake_items"] = raw_intake_items
    cast("list[JsonDict]", raw_intake_items).append(intake)


def _apply_refinement_operation(
    refined: JsonDict,
    items: list[JsonDict],
    operation: RefinementOperation,
    operation_set: BacklogRefinementOperationSet,
    supported_authority_refs: set[str] | None,
) -> list[JsonDict]:
    _assert_unique_item_ids(items)
    items_by_id = _items_by_id({"backlog_items": items})
    _assert_source_matches(items_by_id, operation)

    if isinstance(operation, SplitOperation):
        return _apply_split(items, operation, operation_set)
    if isinstance(operation, MergeOperation):
        return _apply_merge(items, operation, operation_set)
    if isinstance(operation, RetitleOperation):
        _apply_retitle(items_by_id, operation, operation_set)
    elif isinstance(operation, RewriteScopeOperation):
        _apply_rewrite_scope(items_by_id, operation, operation_set)
    elif isinstance(operation, ReorderOperation):
        return _apply_reorder(items, operation)
    elif isinstance(operation, ClassifyOperation):
        _apply_classify(items_by_id, operation, operation_set)
    elif isinstance(operation, AuthorityRefChangeOperation):
        _apply_authority_ref_change(
            items_by_id,
            operation,
            operation_set,
            supported_authority_refs,
        )
    elif isinstance(operation, DeleteOperation):
        deleted_ids = set(operation.source_item_ids)
        return [item for item in items if item.get("item_id") not in deleted_ids]
    elif isinstance(operation, AddIntakeOperation):
        _apply_add_intake(refined, operation, operation_set)
    else:
        message = f"unsupported refinement operation: {operation.operation_type}"
        raise BacklogRefinementError(message)
    return items


def apply_refinement_operations(
    source_artifact: JsonDict,
    operation_set: BacklogRefinementOperationSet,
    *,
    supported_authority_refs: set[str] | None = None,
) -> JsonDict:
    """Apply typed refinement operations and normalize the refined artifact."""
    refined = copy.deepcopy(source_artifact)
    raw_items = refined.get("backlog_items")
    if not isinstance(raw_items, list):
        raw_items = []

    items: list[JsonDict] = [
        cast("JsonDict", copy.deepcopy(item))
        for item in raw_items
        if isinstance(item, dict)
    ]
    _assert_unique_item_ids(items)
    for operation in operation_set.operations:
        items = _apply_refinement_operation(
            refined,
            items,
            operation,
            operation_set,
            supported_authority_refs,
        )

    refined["backlog_items"] = items
    return normalize_refined_artifact(refined)
