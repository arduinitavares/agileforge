"""Backlog refinement schemas and host-owned transformation helpers."""

from __future__ import annotations

import copy
from typing import Annotated, Literal

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


class UnsupportedAuthorityRefError(BacklogRefinementError):
    """Raised when an operation introduces unsupported implementation scope."""


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
        return self


def _validate_single_source_in_place(
    operation: BaseRefinementOperation,
    *,
    operation_name: str,
) -> None:
    if len(operation.source_item_ids) != 1:
        message = f"{operation_name} requires exactly one source_item_id"
        raise ValueError(message)
    if operation.result_item_ids:
        message = f"{operation_name} cannot define result_item_ids"
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
        if len(self.source_item_ids) != 1:
            message = "retitle requires exactly one source_item_id"
            raise ValueError(message)
        return self


class RewriteScopeOperation(BaseRefinementOperation):
    """Change backlog item text fields except identity/order."""

    operation_type: Literal["rewrite_scope"] = "rewrite_scope"
    field_updates: dict[str, object]

    @model_validator(mode="after")
    def _valid_rewrite(self) -> RewriteScopeOperation:
        if len(self.source_item_ids) != 1:
            message = "rewrite_scope requires exactly one source_item_id"
            raise ValueError(message)
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


def _item_fingerprint(item: dict[str, object]) -> str:
    stable_item = copy.deepcopy(item)
    stable_item.pop("item_id", None)
    stable_item.pop("item_fingerprint", None)
    return canonical_hash({"backlog_item": stable_item})


def assign_item_identity(
    artifact: dict[str, object],
    *,
    source_attempt_id: str,
    source_artifact_fingerprint: str,
) -> dict[str, object]:
    """Return a copy of artifact with stable host item identity."""
    normalized = copy.deepcopy(artifact)
    items = normalized.get("backlog_items")
    if not isinstance(items, list):
        normalized["backlog_items"] = []
        return normalized
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        item.setdefault("item_id", f"item-{index:03d}")
        item["source_attempt_id"] = source_attempt_id
        item["source_artifact_fingerprint"] = source_artifact_fingerprint
        item["item_fingerprint"] = _item_fingerprint(item)
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
    artifact: dict[str, object],
) -> list[dict[str, object]]:
    """Return BacklogItem-compatible implementation items from artifact."""
    raw_items = artifact.get("backlog_items")
    if not isinstance(raw_items, list):
        return []
    projected: list[dict[str, object]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        if raw_item.get("classification") == "authority_gap_intake":
            continue
        item_payload = {
            key: copy.deepcopy(value)
            for key, value in raw_item.items()
            if key in BACKLOG_ITEM_KEYS and key not in HOST_ONLY_ITEM_KEYS
        }
        projected.append(BacklogItem.model_validate(item_payload).model_dump())
    return projected


def normalize_refined_artifact(artifact: dict[str, object]) -> dict[str, object]:
    """Normalize priorities and recompute item fingerprints for a refined draft."""
    normalized = copy.deepcopy(artifact)
    raw_items = normalized.get("backlog_items")
    if not isinstance(raw_items, list):
        normalized["backlog_items"] = []
        normalized["is_complete"] = False
        return normalized

    valid_items: list[dict[str, object]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        priority = len(valid_items) + 1
        item["priority"] = priority
        item["item_fingerprint"] = _item_fingerprint(item)
        valid_items.append(item)

    normalized["backlog_items"] = valid_items
    normalized["is_complete"] = len(valid_items) >= MIN_COMPLETE_BACKLOG_ITEMS and (
        not normalized.get("clarifying_questions")
    )
    return normalized
