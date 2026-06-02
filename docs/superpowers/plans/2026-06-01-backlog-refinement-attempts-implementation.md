# Backlog Refinement Attempts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Phase 1 backlog refinement attempts: typed operation validation, host-mediated approval, canonical refined attempts, import canonicalization, savable projection, and stale downstream markers without adding a second Product Backlog persistence path.

**Architecture:** Add a host-owned refinement service under `services/phases/` and route it through the existing Backlog phase runner, application facade, CLI, and command registry. The refiner agent remains proposal-only; Phase 1 implements deterministic typed operation handling and host guards. Existing `backlog save` remains the only persistence command and only saves refined attempts when the existing replacement guard allows it.

**Tech Stack:** Python 3.13, Pydantic v2, SQLModel workflow events, AgileForge workflow state dicts, pytest, ruff, ty, bandit.

---

## Source Spec

Accepted spec:

- `docs/superpowers/specs/2026-06-01-backlog-refinement-attempts-design.md`

Key constraints:

- No `save-edited` command.
- Agent/proposer operation payloads cannot self-certify PO approval.
- `refine-record` creates canonical attempts and active draft state, but does not persist Product Backlog rows.
- `backlog save` remains the only Product Backlog persistence command and may still be blocked by existing downstream replacement guards.
- Host always re-derives annotations/warnings and always strips host-only metadata before calling `save_backlog_tool`.

## File Structure

- Create `services/phases/backlog_refinement.py`
  - Pydantic schemas for refinement operations, approval records, item identity, projected save payloads.
  - Pure helpers for canonical item ids/fingerprints, operation fingerprints, operation application, completeness recompute, savable projection, and stale marker updates.

- Create `services/agent_workbench/backlog_refinement_events.py`
  - Append-only approval event recording/replay.
  - Reads/writes `WorkflowEvent` rows using a new `WorkflowEventType.BACKLOG_REFINEMENT_APPROVED`.

- Modify `models/enums.py`
  - Add `BACKLOG_REFINEMENT_APPROVED = "backlog_refinement_approved"`.

- Modify `services/phases/backlog_service.py`
  - Add `preview_backlog_refinement`, `record_backlog_refinement`, `import_backlog_refinement`, and `approve_backlog_refinement` service functions or thin service adapters.
  - Add savable projection into `save_backlog_draft` before `SaveBacklogInput`.

- Modify `services/agent_workbench/backlog_phase.py`
  - Expose runner methods for `refine_preview`, `refine_record`, `refine_import`, and `approve`.

- Modify `services/agent_workbench/application.py`
  - Extend `_BacklogPhaseRunner` protocol and facade methods.
  - Add workflow-next suggestions for `BACKLOG_REVIEW` and `SPRINT_COMPLETE`.
  - Add stale marker block/readiness messaging for downstream phases if implemented there.

- Modify `services/agent_workbench/command_registry.py`
  - Register new commands with idempotency and input contracts.

- Modify `cli/main.py`
  - Add CLI subcommands and route handlers.

- Modify downstream phase services as needed:
  - `services/phases/roadmap_service.py`
  - `services/agent_workbench/story_phase.py`
  - `services/agent_workbench/sprint_phase.py`
  - Block or request review when `workflow_state["downstream_backlog_stale"]` is true.

- Tests:
  - Create `tests/test_backlog_refinement_service.py`
  - Modify `tests/test_backlog_phase_service.py`
  - Modify `tests/test_agent_workbench_backlog_phase.py`
  - Modify `tests/test_agent_workbench_application.py`
  - Modify `tests/test_agent_workbench_cli.py`
  - Modify `tests/test_agent_workbench_command_schema.py`
  - Add/update downstream stale marker tests in roadmap/story/sprint test files.

---

### Task 1: Define Refinement Schemas and Pure Helpers

**Files:**
- Create: `services/phases/backlog_refinement.py`
- Test: `tests/test_backlog_refinement_service.py`

- [ ] **Step 1: Write failing tests for operation schema, item identity, and projections**

Add to `tests/test_backlog_refinement_service.py`:

```python
"""Tests for backlog refinement operation helpers."""

from typing import Any

import pytest
from pydantic import ValidationError

from services.phases.backlog_refinement import (
    AddIntakeOperation,
    BacklogRefinementOperationSet,
    DeleteOperation,
    SplitOperation,
    UnsupportedAuthorityRefError,
    assign_item_identity,
    canonical_operations_fingerprint,
    normalize_refined_artifact,
    project_savable_backlog_items,
)


def _item(priority: int, requirement: str, **extra: object) -> dict[str, object]:
    return {
        "priority": priority,
        "requirement": requirement,
        "authority_ref": extra.pop("authority_ref", "REQ.example"),
        "capability_hint": extra.pop("capability_hint", None),
        "value_driver": extra.pop("value_driver", "Strategic"),
        "justification": extra.pop("justification", "Valuable backlog work."),
        "estimated_effort": extra.pop("estimated_effort", "M"),
        "technical_note": extra.pop("technical_note", None),
        **extra,
    }


def test_assign_item_identity_adds_stable_ids_and_fingerprints() -> None:
    artifact = {"backlog_items": [_item(1, "Validate existing flow")]}

    normalized = assign_item_identity(
        artifact,
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )

    item = normalized["backlog_items"][0]
    assert item["item_id"] == "item-001"
    assert str(item["item_fingerprint"]).startswith("sha256:")
    assert item["source_attempt_id"] == "backlog-attempt-1"
    assert item["source_artifact_fingerprint"] == "sha256:source"


def test_operation_set_rejects_agent_authored_approval() -> None:
    payload: dict[str, Any] = {
        "source_attempt_id": "backlog-attempt-1",
        "source_artifact_fingerprint": "sha256:source",
        "authority_fingerprint": "sha256:authority",
        "as_built_cache_fingerprint": "sha256:as-built",
        "operations": [
            {
                "operation_id": "op-1",
                "operation_type": "split",
                "source_item_ids": ["item-001"],
                "source_item_fingerprints": ["sha256:item"],
                "result_item_ids": ["item-001a", "item-001b"],
                "result_items": [_item(1, "Validate existing"), _item(2, "Discover gap")],
                "rationale": "Separate verification from discovery.",
                "requested_by": "agent",
                "approval": {"status": "po_reviewed"},
            }
        ],
    }

    with pytest.raises(ValidationError):
        BacklogRefinementOperationSet.model_validate(payload)


def test_canonical_operations_fingerprint_is_order_stable() -> None:
    operation_set = BacklogRefinementOperationSet(
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
        operations=[
            SplitOperation(
                operation_id="op-1",
                source_item_ids=["item-001"],
                source_item_fingerprints=["sha256:item"],
                result_item_ids=["item-001a", "item-001b"],
                result_items=[
                    _item(1, "Validate existing"),
                    _item(2, "Discover missing gap"),
                ],
                rationale="Separate verification from discovery.",
                requested_by="po",
            )
        ],
    )

    assert canonical_operations_fingerprint(operation_set).startswith("sha256:")


def test_project_savable_backlog_items_strips_host_only_fields_and_intake() -> None:
    artifact = {
        "backlog_items": [
            _item(
                1,
                "Validate existing flow",
                item_id="item-001",
                item_fingerprint="sha256:item",
                classification="verification",
                as_built_annotation={"schema_version": "agileforge.brownfield_annotation.v1"},
            )
        ],
        "backlog_intake_items": [
            _item(
                2,
                "Discover unsupported authority gap",
                classification="authority_gap_intake",
            )
        ],
    }

    projected = project_savable_backlog_items(artifact)

    assert projected == [
        {
            "priority": 1,
            "requirement": "Validate existing flow",
            "authority_ref": "REQ.example",
            "capability_hint": None,
            "value_driver": "Strategic",
            "justification": "Valuable backlog work.",
            "estimated_effort": "M",
            "technical_note": None,
        }
    ]


def test_add_intake_requires_non_implementation_classification() -> None:
    with pytest.raises(ValidationError):
        AddIntakeOperation(
            operation_id="op-intake",
            source_item_ids=[],
            source_item_fingerprints=[],
            result_item_ids=["item-new"],
            result_item=_item(3, "Build unsupported feature"),
            authority_gap_ref="REQ.new-gap",
            rationale="Unsupported gap.",
            requested_by="agent",
            classification="product_new_work",
        )


def test_delete_operation_requires_source_identity() -> None:
    with pytest.raises(ValidationError):
        DeleteOperation(
            operation_id="op-delete",
            source_item_ids=[],
            source_item_fingerprints=[],
            result_item_ids=[],
            rationale="Remove item.",
            requested_by="po",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'services.phases.backlog_refinement'`.

- [ ] **Step 3: Implement schemas and helper skeleton**

Create `services/phases/backlog_refinement.py`:

```python
"""Backlog refinement schemas and host-owned transformation helpers."""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator_agent.agent_tools.backlog_primer.schemes import BacklogItem
from services.agent_workbench.fingerprints import canonical_hash

BacklogClassification = Literal[
    "verification",
    "discovery",
    "product_new_work",
    "unchanged",
    "authority_gap_intake",
]
OperationType = Literal[
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
            raise ValueError("source_item_fingerprints must match source_item_ids")
        return self


class SplitOperation(BaseRefinementOperation):
    """Replace one source item with multiple result items."""

    operation_type: Literal["split"] = "split"
    result_items: list[dict[str, object]]

    @model_validator(mode="after")
    def _valid_split(self) -> SplitOperation:
        if len(self.source_item_ids) != 1:
            raise ValueError("split requires exactly one source_item_id")
        if len(self.result_items) < 2:
            raise ValueError("split requires at least two result_items")
        if len(self.result_item_ids) != len(self.result_items):
            raise ValueError("result_item_ids must match result_items")
        return self


class MergeOperation(BaseRefinementOperation):
    """Combine multiple source items into one result item."""

    operation_type: Literal["merge"] = "merge"
    result_item: dict[str, object]

    @model_validator(mode="after")
    def _valid_merge(self) -> MergeOperation:
        if len(self.source_item_ids) < 2:
            raise ValueError("merge requires at least two source_item_ids")
        if len(self.result_item_ids) != 1:
            raise ValueError("merge requires exactly one result_item_id")
        return self


class RetitleOperation(BaseRefinementOperation):
    """Change only the backlog item title/requirement."""

    operation_type: Literal["retitle"] = "retitle"
    new_requirement: Annotated[str, Field(min_length=3)]

    @model_validator(mode="after")
    def _valid_retitle(self) -> RetitleOperation:
        if len(self.source_item_ids) != 1:
            raise ValueError("retitle requires exactly one source_item_id")
        return self


class RewriteScopeOperation(BaseRefinementOperation):
    """Change backlog item text fields except identity/order."""

    operation_type: Literal["rewrite_scope"] = "rewrite_scope"
    field_updates: dict[str, object]

    @model_validator(mode="after")
    def _valid_rewrite(self) -> RewriteScopeOperation:
        if len(self.source_item_ids) != 1:
            raise ValueError("rewrite_scope requires exactly one source_item_id")
        invalid = set(self.field_updates) - {
            "justification",
            "technical_note",
            "value_driver",
            "estimated_effort",
            "capability_hint",
        }
        if invalid:
            raise ValueError(f"unsupported rewrite_scope fields: {sorted(invalid)}")
        return self


class ReorderOperation(BaseRefinementOperation):
    """Replace item priority order with an explicit ordered id list."""

    operation_type: Literal["reorder"] = "reorder"
    ordered_item_ids: list[str]


class ClassifyOperation(BaseRefinementOperation):
    """Change host classification for one item."""

    operation_type: Literal["classify"] = "classify"
    classification: BacklogClassification


class AuthorityRefChangeOperation(BaseRefinementOperation):
    """Change authority reference on one item."""

    operation_type: Literal["authority_ref_change"] = "authority_ref_change"
    old_authority_ref: str | None = None
    new_authority_ref: str | None = None


class DeleteOperation(BaseRefinementOperation):
    """Delete one or more source items from the draft."""

    operation_type: Literal["delete"] = "delete"

    @model_validator(mode="after")
    def _valid_delete(self) -> DeleteOperation:
        if not self.source_item_ids:
            raise ValueError("delete requires at least one source_item_id")
        if self.result_item_ids:
            raise ValueError("delete cannot define result_item_ids")
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
            raise ValueError("add_intake must not have source_item_ids")
        if len(self.result_item_ids) != 1:
            raise ValueError("add_intake requires one result_item_id")
        requirement = str(self.result_item.get("requirement", "")).strip().lower()
        forbidden_prefixes = ("build ", "add ", "implement ", "create ")
        if requirement.startswith(forbidden_prefixes):
            raise ValueError("authority_gap_intake cannot be implementation work")
        return self


RefinementOperation = (
    SplitOperation
    | MergeOperation
    | RetitleOperation
    | RewriteScopeOperation
    | ReorderOperation
    | ClassifyOperation
    | AuthorityRefChangeOperation
    | DeleteOperation
    | AddIntakeOperation
)


class BacklogRefinementOperationSet(BaseModel):
    """A host-validated operation set against one source backlog attempt."""

    model_config = ConfigDict(extra="forbid")

    source_attempt_id: Annotated[str, Field(min_length=1)]
    source_artifact_fingerprint: Annotated[str, Field(min_length=1)]
    authority_fingerprint: Annotated[str, Field(min_length=1)]
    as_built_cache_fingerprint: Annotated[str, Field(min_length=1)]
    operations: list[RefinementOperation] = Field(discriminator="operation_type")


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
            if key in BACKLOG_ITEM_KEYS
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
    for priority, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        item["priority"] = priority
        item["item_fingerprint"] = _item_fingerprint(item)
        valid_items.append(item)
    normalized["backlog_items"] = valid_items
    normalized["is_complete"] = len(valid_items) >= 10 and not normalized.get(
        "clarifying_questions"
    )
    return normalized
```

- [ ] **Step 4: Run tests to verify schema/helper pass**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py -q
```

Expected: tests pass except any operation-apply tests not added yet.

- [ ] **Step 5: Commit**

```bash
git add services/phases/backlog_refinement.py tests/test_backlog_refinement_service.py
git commit -m "feat: add backlog refinement schemas"
```

---

### Task 2: Implement Operation Application and Refined Artifact Normalization

**Files:**
- Modify: `services/phases/backlog_refinement.py`
- Test: `tests/test_backlog_refinement_service.py`

- [ ] **Step 1: Add failing operation-application tests**

Append to `tests/test_backlog_refinement_service.py`:

```python
from services.phases.backlog_refinement import (
    AuthorityRefChangeOperation,
    RetitleOperation,
    apply_refinement_operations,
)


def test_apply_split_replaces_one_item_with_two_results() -> None:
    source = assign_item_identity(
        {
            "backlog_items": [_item(1, "Mixed captain optimizer work")],
            "is_complete": False,
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = BacklogRefinementOperationSet(
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
        operations=[
            SplitOperation(
                operation_id="op-split",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=["item-001a", "item-001b"],
                result_items=[
                    _item(1, "Validate Captain-Aware Optimization Contract"),
                    _item(2, "Discover Captain Floor-Guard Requirements"),
                ],
                rationale="Separate verification and discovery.",
                requested_by="po",
            )
        ],
    )

    refined = apply_refinement_operations(source, operation_set)

    assert [item["requirement"] for item in refined["backlog_items"]] == [
        "Validate Captain-Aware Optimization Contract",
        "Discover Captain Floor-Guard Requirements",
    ]
    assert refined["backlog_items"][0]["refinement_provenance"]["operation_id"] == (
        "op-split"
    )


def test_apply_retitle_changes_requirement_only() -> None:
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Build Promotion Gate")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = BacklogRefinementOperationSet(
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
        operations=[
            RetitleOperation(
                operation_id="op-retitle",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[source_item["item_id"]],
                new_requirement="Formalize/Verify Frozen Promotion Gate Evidence",
                rationale="Retitle as verification.",
                requested_by="po",
            )
        ],
    )

    refined = apply_refinement_operations(source, operation_set)

    assert refined["backlog_items"][0]["requirement"] == (
        "Formalize/Verify Frozen Promotion Gate Evidence"
    )
    assert refined["backlog_items"][0]["priority"] == 1


def test_apply_authority_ref_change_rejects_unbacked_ref_without_intake() -> None:
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Validate existing")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = BacklogRefinementOperationSet(
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
        operations=[
            AuthorityRefChangeOperation(
                operation_id="op-authority",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[source_item["item_id"]],
                old_authority_ref="REQ.example",
                new_authority_ref="REQ.unsupported",
                rationale="Unsupported authority.",
                requested_by="agent",
            )
        ],
    )

    with pytest.raises(UnsupportedAuthorityRefError):
        apply_refinement_operations(
            source,
            operation_set,
            supported_authority_refs={"REQ.example"},
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py::test_apply_split_replaces_one_item_with_two_results tests/test_backlog_refinement_service.py::test_apply_retitle_changes_requirement_only tests/test_backlog_refinement_service.py::test_apply_authority_ref_change_rejects_unbacked_ref_without_intake -q
```

Expected: fail with missing `apply_refinement_operations`.

- [ ] **Step 3: Implement operation application**

Add to `services/phases/backlog_refinement.py`:

```python
def _items_by_id(artifact: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_items = artifact.get("backlog_items")
    if not isinstance(raw_items, list):
        return {}
    return {
        str(item["item_id"]): item
        for item in raw_items
        if isinstance(item, dict) and item.get("item_id")
    }


def _assert_source_matches(
    items_by_id: dict[str, dict[str, object]],
    operation: BaseRefinementOperation,
) -> None:
    for source_id, expected_fingerprint in zip(
        operation.source_item_ids,
        operation.source_item_fingerprints,
        strict=True,
    ):
        item = items_by_id.get(source_id)
        if item is None:
            raise BacklogRefinementError(f"source item not found: {source_id}")
        if item.get("item_fingerprint") != expected_fingerprint:
            raise BacklogRefinementError(f"source item stale: {source_id}")


def _provenance(
    operation: BaseRefinementOperation,
    *,
    source_artifact_fingerprint: str,
    authority_fingerprint: str,
    as_built_cache_fingerprint: str,
) -> dict[str, object]:
    return {
        "operation_id": operation.operation_id,
        "operation_type": operation.operation_type,
        "source_item_ids": list(operation.source_item_ids),
        "source_item_fingerprints": list(operation.source_item_fingerprints),
        "source_artifact_fingerprint": source_artifact_fingerprint,
        "authority_fingerprint": authority_fingerprint,
        "as_built_cache_fingerprint": as_built_cache_fingerprint,
        "rationale": operation.rationale,
        "warnings": copy.deepcopy(operation.warnings),
    }


def _with_result_identity(
    item: dict[str, object],
    *,
    result_item_id: str,
    operation: BaseRefinementOperation,
    source_artifact_fingerprint: str,
    authority_fingerprint: str,
    as_built_cache_fingerprint: str,
) -> dict[str, object]:
    result = copy.deepcopy(item)
    result["item_id"] = result_item_id
    result["source_attempt_id"] = operation.source_attempt_id if hasattr(
        operation, "source_attempt_id"
    ) else None
    result["refinement_provenance"] = _provenance(
        operation,
        source_artifact_fingerprint=source_artifact_fingerprint,
        authority_fingerprint=authority_fingerprint,
        as_built_cache_fingerprint=as_built_cache_fingerprint,
    )
    return result


def apply_refinement_operations(
    source_artifact: dict[str, object],
    operation_set: BacklogRefinementOperationSet,
    *,
    supported_authority_refs: set[str] | None = None,
) -> dict[str, object]:
    """Apply typed operations to a source artifact and return a refined copy."""
    refined = copy.deepcopy(source_artifact)
    raw_items = refined.get("backlog_items")
    if not isinstance(raw_items, list):
        raw_items = []
    items: list[dict[str, object]] = [
        copy.deepcopy(item) for item in raw_items if isinstance(item, dict)
    ]
    items_by_id = _items_by_id({"backlog_items": items})
    for operation in operation_set.operations:
        _assert_source_matches(items_by_id, operation)
        if isinstance(operation, SplitOperation):
            source_id = operation.source_item_ids[0]
            replacement = [
                _with_result_identity(
                    item,
                    result_item_id=result_id,
                    operation=operation,
                    source_artifact_fingerprint=operation_set.source_artifact_fingerprint,
                    authority_fingerprint=operation_set.authority_fingerprint,
                    as_built_cache_fingerprint=operation_set.as_built_cache_fingerprint,
                )
                for result_id, item in zip(
                    operation.result_item_ids,
                    operation.result_items,
                    strict=True,
                )
            ]
            next_items: list[dict[str, object]] = []
            for item in items:
                if item.get("item_id") == source_id:
                    next_items.extend(replacement)
                else:
                    next_items.append(item)
            items = next_items
        elif isinstance(operation, RetitleOperation):
            item = items_by_id[operation.source_item_ids[0]]
            item["requirement"] = operation.new_requirement
            item["refinement_provenance"] = _provenance(
                operation,
                source_artifact_fingerprint=operation_set.source_artifact_fingerprint,
                authority_fingerprint=operation_set.authority_fingerprint,
                as_built_cache_fingerprint=operation_set.as_built_cache_fingerprint,
            )
        elif isinstance(operation, AuthorityRefChangeOperation):
            if (
                supported_authority_refs is not None
                and operation.new_authority_ref not in supported_authority_refs
            ):
                raise UnsupportedAuthorityRefError(
                    f"unsupported authority ref: {operation.new_authority_ref}"
                )
            item = items_by_id[operation.source_item_ids[0]]
            item["authority_ref"] = operation.new_authority_ref
        elif isinstance(operation, DeleteOperation):
            deleted = set(operation.source_item_ids)
            items = [item for item in items if item.get("item_id") not in deleted]
        elif isinstance(operation, AddIntakeOperation):
            intake = copy.deepcopy(operation.result_item)
            intake["item_id"] = operation.result_item_ids[0]
            intake["classification"] = "authority_gap_intake"
            intake["intake_metadata"] = {
                "authority_gap_ref": operation.authority_gap_ref,
                "operation_id": operation.operation_id,
            }
            refined.setdefault("backlog_intake_items", [])
            intake_items = refined["backlog_intake_items"]
            if isinstance(intake_items, list):
                intake_items.append(intake)
        items_by_id = _items_by_id({"backlog_items": items})
    refined["backlog_items"] = items
    return normalize_refined_artifact(refined)
```

- [ ] **Step 4: Run tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/phases/backlog_refinement.py tests/test_backlog_refinement_service.py
git commit -m "feat: apply backlog refinement operations"
```

---

### Task 3: Add Host-Mediated Approval Event Recording

**Files:**
- Modify: `models/enums.py`
- Create: `services/agent_workbench/backlog_refinement_events.py`
- Test: `tests/test_backlog_refinement_service.py`

- [ ] **Step 1: Write failing tests for append-only approval replay and guard**

Append to `tests/test_backlog_refinement_service.py`:

```python
from sqlmodel import Session, SQLModel, create_engine

from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench.backlog_refinement_events import (
    BacklogRefinementApprovalRequest,
    record_backlog_refinement_approval,
)


def test_record_backlog_refinement_approval_writes_append_only_event() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    request = BacklogRefinementApprovalRequest(
        project_id=7,
        source_attempt_id="backlog-attempt-1",
        operation_set_fingerprint="sha256:operations",
        approved_artifact_fingerprint="sha256:artifact",
        approved_operation_ids=["op-1"],
        approval_source="cli",
        idempotency_key="approve-1",
        approved_by="po",
    )

    with Session(engine) as session:
        result = record_backlog_refinement_approval(
            session,
            request=request,
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )
        events = session.query(WorkflowEvent).all()

    assert result["approval_id"].startswith("approval:")
    assert events[0].event_type == WorkflowEventType.BACKLOG_REFINEMENT_APPROVED


def test_record_backlog_refinement_approval_replays_same_key() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    request = BacklogRefinementApprovalRequest(
        project_id=7,
        source_attempt_id="backlog-attempt-1",
        operation_set_fingerprint="sha256:operations",
        approved_artifact_fingerprint="sha256:artifact",
        approved_operation_ids=["op-1"],
        approval_source="cli",
        idempotency_key="approve-1",
        approved_by="po",
    )

    with Session(engine) as session:
        first = record_backlog_refinement_approval(
            session,
            request=request,
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )
        second = record_backlog_refinement_approval(
            session,
            request=request,
            now_iso=lambda: "2026-06-01T00:00:01Z",
        )

    assert second["idempotent_replay"] is True
    assert second["approval_id"] == first["approval_id"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py::test_record_backlog_refinement_approval_writes_append_only_event tests/test_backlog_refinement_service.py::test_record_backlog_refinement_approval_replays_same_key -q
```

Expected: fail with missing enum/module.

- [ ] **Step 3: Add enum**

Modify `models/enums.py`:

```python
class WorkflowEventType(StrEnum):
    """Types of workflow events for metrics tracking."""

    VISION_SAVED = "vision_saved"
    SPEC_COMPILED = "spec_compiled"
    BACKLOG_SAVED = "backlog_saved"
    BACKLOG_REFINEMENT_APPROVED = "backlog_refinement_approved"
    EVIDENCE_COLLECTED = "evidence_collected"
```

- [ ] **Step 4: Implement approval event module**

Create `services/agent_workbench/backlog_refinement_events.py`:

```python
"""Append-only approval events for backlog refinement attempts."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench.fingerprints import canonical_hash


class BacklogRefinementApprovalError(Exception):
    """Raised when backlog refinement approval cannot be recorded."""


class BacklogRefinementApprovalRequest(BaseModel):
    """Host-mediated approval request for a refined backlog artifact."""

    model_config = ConfigDict(extra="forbid")

    project_id: Annotated[int, Field(gt=0)]
    source_attempt_id: str | None = None
    attempt_id: str | None = None
    operation_set_fingerprint: str | None = None
    approved_artifact_fingerprint: Annotated[str, Field(min_length=1)]
    approved_operation_ids: list[str] = Field(default_factory=list)
    approval_source: Annotated[str, Field(min_length=1)] = "cli"
    idempotency_key: Annotated[str, Field(min_length=1)]
    approved_by: Annotated[str, Field(min_length=1)] = "po"


def approval_request_fingerprint(
    request: BacklogRefinementApprovalRequest,
) -> str:
    """Return canonical request fingerprint for approval idempotency."""
    return canonical_hash(
        {
            "command": "agileforge backlog approve",
            "request": request.model_dump(mode="json"),
        }
    )


def _event_metadata(event: WorkflowEvent) -> dict[str, object]:
    if not event.event_metadata:
        return {}
    try:
        payload = json.loads(event.event_metadata)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _approval_events(session: Session, project_id: int) -> list[WorkflowEvent]:
    return list(
        session.exec(
            select(WorkflowEvent)
            .where(WorkflowEvent.product_id == project_id)
            .where(
                WorkflowEvent.event_type
                == WorkflowEventType.BACKLOG_REFINEMENT_APPROVED
            )
        )
    )


def record_backlog_refinement_approval(
    session: Session,
    *,
    request: BacklogRefinementApprovalRequest,
    now_iso: Callable[[], str],
) -> dict[str, object]:
    """Record or replay a host-mediated refinement approval event."""
    request_fingerprint = approval_request_fingerprint(request)
    for event in _approval_events(session, request.project_id):
        metadata = _event_metadata(event)
        if metadata.get("idempotency_key") != request.idempotency_key:
            continue
        if metadata.get("request_fingerprint") != request_fingerprint:
            raise BacklogRefinementApprovalError(
                "Idempotency key reused with different approval inputs."
            )
        return {
            "approval_id": metadata["approval_id"],
            "request_fingerprint": request_fingerprint,
            "idempotent_replay": True,
        }

    approval_id = "approval:" + request_fingerprint.removeprefix("sha256:")[:16]
    metadata = {
        "action": "backlog_refinement_approved",
        "approval_id": approval_id,
        "idempotency_key": request.idempotency_key,
        "request_fingerprint": request_fingerprint,
        "approved_at": now_iso(),
        **request.model_dump(mode="json"),
    }
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_REFINEMENT_APPROVED,
            product_id=request.project_id,
            event_metadata=json.dumps(metadata, sort_keys=True),
        )
    )
    session.commit()
    return {
        "approval_id": approval_id,
        "request_fingerprint": request_fingerprint,
        "idempotent_replay": False,
    }
```

- [ ] **Step 5: Run tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add models/enums.py services/agent_workbench/backlog_refinement_events.py tests/test_backlog_refinement_service.py
git commit -m "feat: record backlog refinement approvals"
```

---

### Task 4: Add Refine-Preview and Refine-Record Service Functions

**Files:**
- Modify: `services/phases/backlog_service.py`
- Modify: `services/phases/backlog_refinement.py`
- Test: `tests/test_backlog_phase_service.py`

- [ ] **Step 1: Write failing service tests for preview and record**

Append to `tests/test_backlog_phase_service.py`:

`AUTO_SOURCE_ITEM_FINGERPRINT` is a command/test sentinel that means "resolve
the exact current source item fingerprint from the canonical source artifact
before applying operations." It must never be stored in a recorded attempt.

```python
from services.phases.backlog_service import (
    preview_backlog_refinement,
    record_backlog_refinement,
)


def _source_refinement_state() -> JsonDict:
    output = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Build Promotion Gate",
                "authority_ref": "REQ.default-promotion-gate",
                "capability_hint": "Default Promotion Gate",
                "value_driver": "Strategic",
                "justification": "Promotion gate matters.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": False,
        "clarifying_questions": [],
    }
    fingerprint = _backlog_artifact_fingerprint(output)
    output["attempt_id"] = "backlog-attempt-1"
    output["artifact_fingerprint"] = fingerprint
    return {
        "fsm_state": "SPRINT_COMPLETE",
        "product_backlog_assessment": output,
        "backlog_attempts": [
            {
                "attempt_id": "backlog-attempt-1",
                "artifact_fingerprint": fingerprint,
                "output_artifact": output,
            }
        ],
        "compiled_authority_fingerprint": "sha256:authority",
        "as_built_assessment_cache_meta": {
            "assessment_fingerprint": "sha256:as-built",
        },
    }


@pytest.mark.asyncio
async def test_preview_backlog_refinement_does_not_mutate_state() -> None:
    state = _source_refinement_state()
    original = dict(state)
    operations = {
        "source_attempt_id": "backlog-attempt-1",
        "source_artifact_fingerprint": state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        "authority_fingerprint": "sha256:authority",
        "as_built_cache_fingerprint": "sha256:as-built",
        "operations": [
            {
                "operation_id": "op-retitle",
                "operation_type": "retitle",
                "source_item_ids": ["item-001"],
                "source_item_fingerprints": ["AUTO_SOURCE_ITEM_FINGERPRINT"],
                "result_item_ids": ["item-001"],
                "new_requirement": "Formalize/Verify Frozen Promotion Gate Evidence",
                "rationale": "Make this verification work.",
                "requested_by": "po",
            }
        ],
    }

    payload = await preview_backlog_refinement(
        project_id=7,
        load_state=lambda: _async_value(state),
        operations_payload=operations,
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    assert payload["persisted"] is False
    assert state == original
    assert payload["output_artifact"]["backlog_items"][0]["requirement"] == (
        "Formalize/Verify Frozen Promotion Gate Evidence"
    )


@pytest.mark.asyncio
async def test_record_backlog_refinement_sets_active_draft_and_review_state() -> None:
    state = _source_refinement_state()
    saved: JsonDict = {}
    operations = {
        "source_attempt_id": "backlog-attempt-1",
        "source_artifact_fingerprint": state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        "authority_fingerprint": "sha256:authority",
        "as_built_cache_fingerprint": "sha256:as-built",
        "operations": [
            {
                "operation_id": "op-retitle",
                "operation_type": "retitle",
                "source_item_ids": ["item-001"],
                "source_item_fingerprints": ["AUTO_SOURCE_ITEM_FINGERPRINT"],
                "result_item_ids": ["item-001"],
                "new_requirement": "Formalize/Verify Frozen Promotion Gate Evidence",
                "rationale": "Make this verification work.",
                "requested_by": "po",
            }
        ],
    }

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = updated

    payload = await record_backlog_refinement(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        operations_payload=operations,
        expected_source_fingerprint=state["backlog_attempts"][0][
            "artifact_fingerprint"
        ],
        expected_state="SPRINT_COMPLETE",
        idempotency_key="refine-1",
        now_iso=lambda: "2026-06-01T00:00:00Z",
    )

    assert payload["fsm_state"] == "BACKLOG_REVIEW"
    assert payload["persisted"] is False
    assert payload["attempt_id"] == "backlog-attempt-2"
    assert saved["state"]["product_backlog_assessment"]["attempt_id"] == (
        "backlog-attempt-2"
    )
    assert saved["state"]["backlog_review_origin"] == "next_cycle_refinement"
    assert saved["state"]["downstream_backlog_stale"] is True
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py::test_preview_backlog_refinement_does_not_mutate_state tests/test_backlog_phase_service.py::test_record_backlog_refinement_sets_active_draft_and_review_state -q
```

Expected: fail with missing functions.

- [ ] **Step 3: Add service functions**

Modify imports in `services/phases/backlog_service.py`:

```python
from services.phases.backlog_refinement import (
    BacklogRefinementOperationSet,
    apply_refinement_operations,
    assign_item_identity,
    canonical_operations_fingerprint,
    normalize_refined_artifact,
)
```

Add functions before `save_backlog_draft`:

```python
def _source_attempt_for_refinement(
    state: dict[str, Any],
    source_attempt_id: str,
) -> dict[str, Any]:
    attempt = _find_backlog_attempt(state, source_attempt_id)
    if attempt is None:
        raise BacklogPhaseError(f"Source backlog attempt not found: {source_attempt_id}")
    output_artifact = attempt.get("output_artifact")
    if not isinstance(output_artifact, dict):
        raise BacklogPhaseError("Source backlog attempt has no output artifact")
    return attempt


def _prepare_source_for_refinement(
    state: dict[str, Any],
    operation_set: BacklogRefinementOperationSet,
) -> dict[str, object]:
    source_attempt = _source_attempt_for_refinement(
        state,
        operation_set.source_attempt_id,
    )
    if source_attempt.get("artifact_fingerprint") != (
        operation_set.source_artifact_fingerprint
    ):
        raise BacklogPhaseError("Source backlog attempt fingerprint mismatch")
    source_artifact = cast("dict[str, object]", source_attempt["output_artifact"])
    normalized = assign_item_identity(
        source_artifact,
        source_attempt_id=operation_set.source_attempt_id,
        source_artifact_fingerprint=operation_set.source_artifact_fingerprint,
    )
    return normalized


def _fill_source_fingerprints(
    operation_set: BacklogRefinementOperationSet,
    source_artifact: dict[str, object],
) -> BacklogRefinementOperationSet:
    items = source_artifact.get("backlog_items")
    by_id = {
        str(item["item_id"]): item
        for item in items
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict) and item.get("item_id")
    }
    payload = operation_set.model_dump(mode="json")
    for operation in payload["operations"]:
        if operation.get("source_item_fingerprints") == ["AUTO_SOURCE_ITEM_FINGERPRINT"]:
            operation["source_item_fingerprints"] = [
                by_id[source_id]["item_fingerprint"]
                for source_id in operation.get("source_item_ids", [])
            ]
    return BacklogRefinementOperationSet.model_validate(payload)


async def preview_backlog_refinement(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    operations_payload: dict[str, Any],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    _ = project_id, now_iso
    state = await load_state()
    operation_set = BacklogRefinementOperationSet.model_validate(operations_payload)
    source_artifact = _prepare_source_for_refinement(state, operation_set)
    operation_set = _fill_source_fingerprints(operation_set, source_artifact)
    refined = apply_refinement_operations(source_artifact, operation_set)
    refined = normalize_refined_artifact(refined)
    artifact_fingerprint = _backlog_artifact_fingerprint(refined)
    return {
        "fsm_state": _normalize_fsm_state(cast("str | None", state.get("fsm_state"))),
        "persisted": False,
        "attempt_id": None,
        "artifact_fingerprint": artifact_fingerprint,
        "operation_set_fingerprint": canonical_operations_fingerprint(operation_set),
        "output_artifact": refined,
        "warnings": refined.get("brownfield_warnings", []),
    }


async def record_backlog_refinement(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    operations_payload: dict[str, Any],
    expected_source_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    _ = project_id
    state = await load_state()
    fsm_state = _normalize_fsm_state(cast("str | None", state.get("fsm_state")))
    if fsm_state != expected_state:
        raise BacklogPhaseError(
            f"Backlog refinement stale state: expected {expected_state}, got {fsm_state}"
        )
    operation_set = BacklogRefinementOperationSet.model_validate(operations_payload)
    if operation_set.source_artifact_fingerprint != expected_source_fingerprint:
        raise BacklogPhaseError("Source artifact fingerprint mismatch")
    source_artifact = _prepare_source_for_refinement(state, operation_set)
    operation_set = _fill_source_fingerprints(operation_set, source_artifact)
    refined = normalize_refined_artifact(
        apply_refinement_operations(source_artifact, operation_set)
    )
    artifact_fingerprint = _backlog_artifact_fingerprint(refined)
    attempt_count = record_backlog_attempt(
        state,
        trigger="refine_record",
        input_context={
            "operation_set": operation_set.model_dump(mode="json"),
            "operation_set_fingerprint": canonical_operations_fingerprint(
                operation_set
            ),
        },
        output_artifact=refined,
        is_complete=bool(refined.get("is_complete")),
        created_at=now_iso(),
    )
    attempt_id = f"backlog-attempt-{attempt_count}"
    _attach_attempt_guards(
        state,
        attempt_id=attempt_id,
        artifact_fingerprint=artifact_fingerprint,
    )
    attempts = ensure_backlog_attempts(state)
    attempts[-1]["attempt_kind"] = "refinement"
    attempts[-1]["source_attempt_id"] = operation_set.source_attempt_id
    attempts[-1]["source_artifact_fingerprint"] = expected_source_fingerprint
    attempts[-1]["operation_set_fingerprint"] = canonical_operations_fingerprint(
        operation_set
    )
    state["fsm_state"] = OrchestratorState.BACKLOG_REVIEW.value
    state["fsm_state_entered_at"] = now_iso()
    if fsm_state == OrchestratorState.SPRINT_COMPLETE.value:
        state["backlog_review_origin"] = "next_cycle_refinement"
        state["downstream_backlog_stale"] = True
        state["stale_backlog_reason"] = "refined_backlog_recorded"
        state["stale_since_backlog_attempt_id"] = attempt_id
    save_state(state)
    return {
        "fsm_state": OrchestratorState.BACKLOG_REVIEW.value,
        "persisted": False,
        "attempt_id": attempt_id,
        "artifact_fingerprint": artifact_fingerprint,
        "operation_set_fingerprint": canonical_operations_fingerprint(operation_set),
        "output_artifact": refined,
        "idempotency_key": idempotency_key,
    }
```

Fix the helper comprehension if ruff flags it:

```python
items = source_artifact.get("backlog_items")
by_id: dict[str, dict[str, object]] = {}
if isinstance(items, list):
    by_id = {
        str(item["item_id"]): item
        for item in items
        if isinstance(item, dict) and item.get("item_id")
    }
```

- [ ] **Step 4: Export functions**

Add to `__all__` in `services/phases/backlog_service.py`:

```python
"preview_backlog_refinement",
"record_backlog_refinement",
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py::test_preview_backlog_refinement_does_not_mutate_state tests/test_backlog_phase_service.py::test_record_backlog_refinement_sets_active_draft_and_review_state -q
```

Expected: pass.

- [ ] **Step 6: Run service suite**

Run:

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py tests/test_backlog_refinement_service.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add services/phases/backlog_service.py services/phases/backlog_refinement.py tests/test_backlog_phase_service.py tests/test_backlog_refinement_service.py
git commit -m "feat: record refined backlog attempts"
```

---

### Task 5: Add Savable Projection to Existing Backlog Save

**Files:**
- Modify: `services/phases/backlog_service.py`
- Test: `tests/test_backlog_phase_service.py`

- [ ] **Step 1: Write failing save projection test**

Append to `tests/test_backlog_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_save_backlog_draft_projects_refined_items_before_tool() -> None:
    output_artifact = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Validate existing flow",
                "authority_ref": "REQ.example",
                "capability_hint": "Example",
                "value_driver": "Strategic",
                "justification": "Validate existing flow.",
                "estimated_effort": "M",
                "technical_note": None,
                "item_id": "item-001",
                "item_fingerprint": "sha256:item",
                "as_built_annotation": {"source": "host_derived"},
                "classification": "verification",
            }
        ],
        "backlog_intake_items": [
            {
                "priority": 2,
                "requirement": "Discover intake gap",
                "value_driver": "Strategic",
                "justification": "Intake only.",
                "estimated_effort": "S",
                "classification": "authority_gap_intake",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    state = _review_state_for_artifact(output_artifact)
    captured: JsonDict = {}

    def fake_save_backlog_tool(
        save_input: SaveBacklogInput, _context: object
    ) -> JsonDict:
        captured["items"] = save_input.backlog_items
        return {"success": True, "saved_count": len(save_input.backlog_items)}

    payload = await save_backlog_draft(
        project_id=7,
        project_name="Example",
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=state["product_backlog_assessment"][
            "artifact_fingerprint"
        ],
        expected_state="BACKLOG_REVIEW",
        idempotency_key="save-refined-1",
        save_state=lambda updated: None,
        now_iso=lambda: "2026-06-01T00:00:00Z",
        hydrate_context=lambda: _async_value(SimpleNamespace(state=state)),
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert payload["save_result"]["saved_count"] == 1
    assert captured["items"] == [
        {
            "priority": 1,
            "requirement": "Validate existing flow",
            "authority_ref": "REQ.example",
            "capability_hint": "Example",
            "value_driver": "Strategic",
            "justification": "Validate existing flow.",
            "estimated_effort": "M",
            "technical_note": None,
        }
    ]
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py::test_save_backlog_draft_projects_refined_items_before_tool -q
```

Expected: fail because host-only fields are passed to `SaveBacklogInput` or intake item is included.

- [ ] **Step 3: Use projection in save**

Modify imports in `services/phases/backlog_service.py`:

```python
from services.phases.backlog_refinement import (
    BacklogRefinementOperationSet,
    apply_refinement_operations,
    assign_item_identity,
    canonical_operations_fingerprint,
    normalize_refined_artifact,
    project_savable_backlog_items,
)
```

Modify `save_backlog_draft` before `SaveBacklogInput`:

```python
    projected_items = project_savable_backlog_items(assessment)
    if not projected_items:
        raise BacklogPhaseError("Backlog items are empty")

    result = save_backlog_tool(
        SaveBacklogInput(
            product_id=project_id,
            backlog_items=projected_items,
            idempotency_key=idempotency_key,
        ),
        build_tool_context(context),
    )
```

Remove or update the older `items = assessment.get("backlog_items")` empty check so it checks `projected_items`.

- [ ] **Step 4: Run focused save tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py::test_save_backlog_draft_projects_refined_items_before_tool tests/test_backlog_phase_service.py::test_save_backlog_draft_persists_persistence_state tests/test_backlog_phase_service.py::test_save_backlog_draft_rejects_empty_items -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/phases/backlog_service.py tests/test_backlog_phase_service.py
git commit -m "feat: project refined backlog items for save"
```

---

### Task 6: Wire Runner, Application Facade, Command Registry, and CLI

**Files:**
- Modify: `services/agent_workbench/backlog_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `cli/main.py`
- Test: `tests/test_agent_workbench_backlog_phase.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Add failing command-schema expectations**

Modify `tests/test_agent_workbench_command_schema.py`:

```python
EXPECTED_PHASE_2D_COMMAND_NAMES = {
    # existing entries...
    "agileforge backlog refine-preview",
    "agileforge backlog refine-record",
    "agileforge backlog approve",
    "agileforge backlog refine-import",
}
```

Add:

```python
def test_backlog_refinement_commands_publish_input_contracts() -> None:
    refine_record = command_schema_payload("agileforge backlog refine-record")
    assert refine_record["mutates"] is True
    assert refine_record["idempotency_required"] is True
    assert refine_record["input"]["required"] == [
        "project_id",
        "source_attempt_id",
        "operations_file",
        "expected_source_fingerprint",
        "expected_state",
        "idempotency_key",
    ]
    assert "approval_id" in refine_record["input"]["optional"]

    approve = command_schema_payload("agileforge backlog approve")
    assert approve["mutates"] is True
    assert approve["idempotency_required"] is True

    preview = command_schema_payload("agileforge backlog refine-preview")
    assert preview["mutates"] is False
```

- [ ] **Step 2: Add failing CLI route test**

Add to `tests/test_agent_workbench_cli.py` fake application methods:

```python
def backlog_refine_preview(
    self,
    *,
    project_id: int,
    source_attempt_id: str | None,
    operations_file: str | None,
    source_artifact: str | None,
    user_input: str | None,
) -> JsonObject:
    self.calls.append(
        (
            "backlog_refine_preview",
            {
                "project_id": project_id,
                "source_attempt_id": source_attempt_id,
                "operations_file": operations_file,
                "source_artifact": source_artifact,
                "user_input": user_input,
            },
        )
    )
    return self.results.get("backlog_refine_preview") or {
        "data": {"persisted": False}
    }
```

Add route test:

```python
def test_cli_routes_backlog_refinement_commands(capsys: pytest.CaptureFixture[str]) -> None:
    app = _FakeApplication()
    exit_code = main(
        [
            "backlog",
            "refine-preview",
            "--project-id",
            "7",
            "--source-attempt-id",
            "backlog-attempt-1",
            "--operations-file",
            "ops.json",
        ],
        application=app,
    )

    assert exit_code == 0
    _ = _stdout_payload(capsys)
    assert app.calls[-1] == (
        "backlog_refine_preview",
        {
            "project_id": 7,
            "source_attempt_id": "backlog-attempt-1",
            "operations_file": "ops.json",
            "source_artifact": None,
            "user_input": None,
        },
    )
```

- [ ] **Step 3: Register command metadata**

Modify `services/agent_workbench/command_registry.py` after backlog reconcile:

```python
CommandMetadata(
    name="agileforge backlog refine-preview",
    mutates=False,
    phase="phase_2d",
    input_required=("project_id",),
    input_optional=("source_attempt_id", "operations_file", "source_artifact", "input"),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ErrorCode.MUTATION_FAILED.value,
    ),
),
CommandMetadata(
    name="agileforge backlog refine-record",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=(
        "project_id",
        "source_attempt_id",
        "operations_file",
        "expected_source_fingerprint",
        "expected_state",
        "idempotency_key",
    ),
    input_optional=("approval_id",),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ErrorCode.MUTATION_FAILED.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
    ),
),
CommandMetadata(
    name="agileforge backlog approve",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=(
        "project_id",
        "approved_artifact_fingerprint",
        "idempotency_key",
    ),
    input_optional=(
        "source_attempt_id",
        "attempt_id",
        "operation_set_fingerprint",
        "approved_operation_id",
    ),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.MUTATION_FAILED.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
    ),
),
CommandMetadata(
    name="agileforge backlog refine-import",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=(
        "project_id",
        "source_artifact",
        "edited_file",
        "expected_source_fingerprint",
        "idempotency_key",
    ),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.MUTATION_FAILED.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
    ),
),
```

- [ ] **Step 4: Add runner/application methods**

Modify `_BacklogPhaseRunner` in `services/agent_workbench/application.py`:

```python
def refine_preview(
    self,
    *,
    project_id: int,
    source_attempt_id: str | None,
    operations_file: str | None,
    source_artifact: str | None,
    user_input: str | None = None,
) -> dict[str, Any]: ...

def refine_record(
    self,
    *,
    project_id: int,
    source_attempt_id: str,
    operations_file: str,
    expected_source_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
    approval_id: str | None = None,
) -> dict[str, Any]: ...

def approve(
    self,
    *,
    project_id: int,
    approved_artifact_fingerprint: str,
    idempotency_key: str,
    source_attempt_id: str | None = None,
    attempt_id: str | None = None,
    operation_set_fingerprint: str | None = None,
    approved_operation_ids: list[str] | None = None,
) -> dict[str, Any]: ...
```

Add facade methods mirroring existing `backlog_preview`/`backlog_save`.

Modify `services/agent_workbench/backlog_phase.py` to add public runner methods and async methods. Use `_data_envelope`, `_phase_error`, and `_workflow_error` exactly like existing generate/preview/save.

- [ ] **Step 5: Add CLI parsers and handlers**

Modify `cli/main.py` backlog parser:

```python
backlog_refine_preview = backlog_sub.add_parser(
    "refine-preview",
    help="Preview typed Backlog refinement operations without persistence.",
)
backlog_refine_preview.add_argument("--project-id", type=int, required=True)
backlog_refine_preview.add_argument("--source-attempt-id")
backlog_refine_preview.add_argument("--operations-file")
backlog_refine_preview.add_argument("--source-artifact")
backlog_refine_preview.add_argument("--input", dest="user_input")
backlog_refine_preview.set_defaults(command_handler=_backlog_refine_preview)

backlog_refine_record = backlog_sub.add_parser(
    "refine-record",
    help="Record a canonical refined Backlog attempt.",
)
backlog_refine_record.add_argument("--project-id", type=int, required=True)
backlog_refine_record.add_argument("--source-attempt-id", required=True)
backlog_refine_record.add_argument("--operations-file", required=True)
backlog_refine_record.add_argument("--approval-id")
backlog_refine_record.add_argument("--expected-source-fingerprint", required=True)
backlog_refine_record.add_argument("--expected-state", required=True)
backlog_refine_record.add_argument("--idempotency-key", required=True)
backlog_refine_record.set_defaults(command_handler=_backlog_refine_record)

backlog_approve = backlog_sub.add_parser(
    "approve",
    help="Record host-mediated PO approval for a refined Backlog artifact.",
)
backlog_approve.add_argument("--project-id", type=int, required=True)
backlog_approve.add_argument("--source-attempt-id")
backlog_approve.add_argument("--attempt-id")
backlog_approve.add_argument("--operation-set-fingerprint")
backlog_approve.add_argument("--approved-artifact-fingerprint", required=True)
backlog_approve.add_argument("--approved-operation-id", action="append", default=[])
backlog_approve.add_argument("--idempotency-key", required=True)
backlog_approve.set_defaults(command_handler=_backlog_approve)
```

Add handlers near `_backlog_preview`:

```python
def _backlog_refine_preview(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog refine-preview to the application facade."""
    return "agileforge backlog refine-preview", application.backlog_refine_preview(
        project_id=args.project_id,
        source_attempt_id=args.source_attempt_id,
        operations_file=args.operations_file,
        source_artifact=args.source_artifact,
        user_input=args.user_input,
    )
```

Add `_backlog_refine_record` and `_backlog_approve` similarly.

- [ ] **Step 6: Run command/facade tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_application.py tests/test_agent_workbench_backlog_phase.py -q
```

Expected: pass after adding fake runner methods and assertions.

- [ ] **Step 7: Commit**

```bash
git add services/agent_workbench/command_registry.py services/agent_workbench/application.py services/agent_workbench/backlog_phase.py cli/main.py tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_application.py tests/test_agent_workbench_backlog_phase.py
git commit -m "feat: expose backlog refinement commands"
```

---

### Task 7: Implement Refine-Import Deterministic Diff Contract

**Files:**
- Modify: `services/phases/backlog_refinement.py`
- Modify: `services/phases/backlog_service.py`
- Modify: `services/agent_workbench/backlog_phase.py`
- Modify: `cli/main.py`
- Test: `tests/test_backlog_refinement_service.py`
- Test: `tests/test_backlog_phase_service.py`

- [ ] **Step 1: Write failing diff tests**

Append to `tests/test_backlog_refinement_service.py`:

```python
from services.phases.backlog_refinement import (
    AmbiguousRefinementDiffError,
    operations_from_edited_artifact,
)


def test_operations_from_edited_artifact_detects_retitle() -> None:
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Build Gate")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    edited = {
        "backlog_items": [
            {
                **source_item,
                "source_item_id": source_item["item_id"],
                "requirement": "Formalize/Verify Frozen Promotion Gate Evidence",
            }
        ]
    }

    operation_set = operations_from_edited_artifact(
        source,
        edited,
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
    )

    assert operation_set.operations[0].operation_type == "retitle"


def test_operations_from_edited_artifact_rejects_missing_source_id() -> None:
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Build Gate")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    edited = {"backlog_items": [_item(1, "Edited without source id")]}

    with pytest.raises(AmbiguousRefinementDiffError):
        operations_from_edited_artifact(
            source,
            edited,
            source_attempt_id="backlog-attempt-1",
            source_artifact_fingerprint="sha256:source",
            authority_fingerprint="sha256:authority",
            as_built_cache_fingerprint="sha256:as-built",
        )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py::test_operations_from_edited_artifact_detects_retitle tests/test_backlog_refinement_service.py::test_operations_from_edited_artifact_rejects_missing_source_id -q
```

Expected: fail with missing function/error.

- [ ] **Step 3: Implement edited artifact diff**

Add to `services/phases/backlog_refinement.py`:

```python
class AmbiguousRefinementDiffError(BacklogRefinementError):
    """Raised when edited artifact cannot map cleanly to source operations."""


def operations_from_edited_artifact(
    source_artifact: dict[str, object],
    edited_artifact: dict[str, object],
    *,
    source_attempt_id: str,
    source_artifact_fingerprint: str,
    authority_fingerprint: str,
    as_built_cache_fingerprint: str,
) -> BacklogRefinementOperationSet:
    """Derive typed operations from a source artifact and edited artifact."""
    source_items = _items_by_id(source_artifact)
    edited_items = edited_artifact.get("backlog_items")
    if not isinstance(edited_items, list):
        raise AmbiguousRefinementDiffError("edited artifact has no backlog_items")
    operations: list[RefinementOperation] = []
    seen_source_ids: set[str] = set()
    for index, edited in enumerate(edited_items, start=1):
        if not isinstance(edited, dict):
            raise AmbiguousRefinementDiffError(f"invalid edited item at {index}")
        source_item_id = edited.get("source_item_id") or edited.get("item_id")
        if not isinstance(source_item_id, str):
            raise AmbiguousRefinementDiffError(
                f"edited item {index} missing source_item_id"
            )
        source = source_items.get(source_item_id)
        if source is None:
            raise AmbiguousRefinementDiffError(
                f"edited item {index} references unknown source item"
            )
        seen_source_ids.add(source_item_id)
        source_requirement = source.get("requirement")
        edited_requirement = edited.get("requirement")
        if edited_requirement != source_requirement:
            operations.append(
                RetitleOperation(
                    operation_id=f"op-{len(operations) + 1:03d}",
                    source_item_ids=[source_item_id],
                    source_item_fingerprints=[str(source["item_fingerprint"])],
                    result_item_ids=[source_item_id],
                    new_requirement=str(edited_requirement),
                    rationale="Imported edit changed backlog item title.",
                    requested_by="po",
                )
            )
    deleted_ids = sorted(set(source_items) - seen_source_ids)
    for deleted_id in deleted_ids:
        source = source_items[deleted_id]
        operations.append(
            DeleteOperation(
                operation_id=f"op-{len(operations) + 1:03d}",
                source_item_ids=[deleted_id],
                source_item_fingerprints=[str(source["item_fingerprint"])],
                result_item_ids=[],
                rationale="Imported edit removed source backlog item.",
                requested_by="po",
            )
        )
    return BacklogRefinementOperationSet(
        source_attempt_id=source_attempt_id,
        source_artifact_fingerprint=source_artifact_fingerprint,
        authority_fingerprint=authority_fingerprint,
        as_built_cache_fingerprint=as_built_cache_fingerprint,
        operations=operations,
    )
```

- [ ] **Step 4: Add `import_backlog_refinement` service function**

In `services/phases/backlog_service.py`, add function that reads already-loaded JSON payloads first. File reading can happen in the runner:

```python
async def import_backlog_refinement(
    *,
    project_id: int,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    source_artifact: dict[str, Any],
    edited_artifact: dict[str, Any],
    expected_source_fingerprint: str,
    idempotency_key: str,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    _ = project_id
    state = await load_state()
    source_attempt_id = f"backlog-attempt-{len(ensure_backlog_attempts(state)) + 1}"
    canonical_source = assign_item_identity(
        source_artifact,
        source_attempt_id=source_attempt_id,
        source_artifact_fingerprint=expected_source_fingerprint,
    )
    source_count = record_backlog_attempt(
        state,
        trigger="refine_import_source",
        input_context={"source": "imported_preview_source"},
        output_artifact=canonical_source,
        is_complete=bool(canonical_source.get("is_complete")),
        created_at=now_iso(),
    )
    canonical_source_attempt_id = f"backlog-attempt-{source_count}"
    _attach_attempt_guards(
        state,
        attempt_id=canonical_source_attempt_id,
        artifact_fingerprint=expected_source_fingerprint,
    )
    operations = operations_from_edited_artifact(
        canonical_source,
        edited_artifact,
        source_attempt_id=canonical_source_attempt_id,
        source_artifact_fingerprint=expected_source_fingerprint,
        authority_fingerprint=str(state.get("compiled_authority_fingerprint") or ""),
        as_built_cache_fingerprint=str(
            (state.get("as_built_assessment_cache_meta") or {}).get(
                "assessment_fingerprint", ""
            )
        ),
    )
    return await record_backlog_refinement(
        project_id=project_id,
        load_state=lambda: _async_dict(state),
        save_state=save_state,
        operations_payload=operations.model_dump(mode="json"),
        expected_source_fingerprint=expected_source_fingerprint,
        expected_state=_normalize_fsm_state(cast("str | None", state.get("fsm_state"))),
        idempotency_key=idempotency_key,
        now_iso=now_iso,
    )
```

If `_async_dict` does not exist, avoid adding it; pass an `async def load_import_state() -> dict[str, Any]: return state`.

- [ ] **Step 5: Run tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py tests/test_backlog_phase_service.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add services/phases/backlog_refinement.py services/phases/backlog_service.py tests/test_backlog_refinement_service.py tests/test_backlog_phase_service.py
git commit -m "feat: import edited backlog refinements"
```

---

### Task 8: Block Downstream Generation on Coarse Stale Markers

**Files:**
- Modify: `services/phases/roadmap_service.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Test: `tests/test_roadmap_phase_service.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_sprint_phase.py`

- [ ] **Step 1: Write failing roadmap stale marker test**

Add to `tests/test_roadmap_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_generate_roadmap_blocks_when_backlog_lineage_stale() -> None:
    state: JsonDict = {
        "fsm_state": "BACKLOG_REVIEW",
        "downstream_backlog_stale": True,
        "stale_backlog_reason": "refined_backlog_recorded",
        "stale_since_backlog_attempt_id": "backlog-attempt-9",
    }

    async def load_state() -> JsonDict:
        return state

    with pytest.raises(RoadmapPhaseError, match="downstream backlog is stale"):
        await generate_roadmap_draft(
            project_id=7,
            load_state=load_state,
            save_state=lambda updated: None,
            now_iso=lambda: "2026-06-01T00:00:00Z",
            run_roadmap_agent=lambda *_args, **_kwargs: _async_value({}),
            user_input=None,
        )
```

Adapt to the exact `generate_roadmap_draft` signature in the file.

- [ ] **Step 2: Add helper**

Create or add to `services/phases/workflow_state.py`:

```python
def assert_downstream_backlog_not_stale(state: dict[str, Any]) -> None:
    """Raise ValueError when downstream artifacts need backlog-lineage review."""
    if state.get("downstream_backlog_stale") is True:
        reason = state.get("stale_backlog_reason") or "unknown"
        attempt = state.get("stale_since_backlog_attempt_id") or "unknown"
        raise ValueError(
            f"downstream backlog is stale: {reason} since {attempt}"
        )
```

If phase services prefer domain exceptions, call this helper and translate `ValueError` into `RoadmapPhaseError`, Story phase error, or Sprint phase error.

- [ ] **Step 3: Guard downstream generation entry points**

At the top of roadmap/story/sprint generation functions after loading state:

```python
try:
    workflow_state.assert_downstream_backlog_not_stale(state)
except ValueError as exc:
    raise RoadmapPhaseError(str(exc)) from exc
```

Use the matching phase error type in each service.

- [ ] **Step 4: Keep stale marker clearing out of Phase 1**

Do not add a broad acknowledgement command in this implementation. Phase 1 sets
the coarse marker and blocks downstream generation with an explicit
`DOWNSTREAM_ARTIFACT_STALE`/phase-error response. Add a test asserting that a
blocked downstream generation attempt does not clear these keys:

```python
assert state["downstream_backlog_stale"] is True
assert state["stale_backlog_reason"] == "refined_backlog_recorded"
assert state["stale_since_backlog_attempt_id"] == "backlog-attempt-9"
```

This keeps the implementation aligned with the accepted scope: refinement can
record a canonical next-cycle attempt, but downstream reconciliation or
versioned persistence remains a separate slice.

- [ ] **Step 5: Run downstream tests**

Run:

```bash
uv run --frozen pytest tests/test_roadmap_phase_service.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_sprint_phase.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add services/phases/workflow_state.py services/phases/roadmap_service.py services/agent_workbench/story_phase.py services/agent_workbench/sprint_phase.py tests/test_roadmap_phase_service.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_sprint_phase.py
git commit -m "feat: block downstream work on stale refined backlog"
```

---

### Task 9: Workflow-Next Guidance and Failure Envelope Polish

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/error_codes.py` if new codes are needed
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add failing workflow-next tests**

Add to `tests/test_agent_workbench_application.py`:

```python
class _SprintCompleteWithBacklogAttemptsReadProjection(_FakeReadProjection):
    """Workflow state after Sprint completion with a backlog attempt to refine."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        return _data_envelope(
            {
                "project_id": project_id,
                "state": {
                    "fsm_state": "SPRINT_COMPLETE",
                    "backlog_attempts": [
                        {
                            "attempt_id": "backlog-attempt-7",
                            "artifact_fingerprint": "sha256:preview",
                        }
                    ],
                },
            }
        )


def test_application_workflow_next_routes_sprint_complete_to_backlog_refinement() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteWithBacklogAttemptsReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    commands = result["data"]["next_valid_commands"]
    assert any("agileforge backlog refine-preview" in command for command in commands)
    assert any("agileforge backlog refine-record" in command for command in commands)
```

- [ ] **Step 2: Update workflow-next helper**

Modify `_backlog_workflow_next` or the sprint-complete handler in `services/agent_workbench/application.py` so `SPRINT_COMPLETE` with backlog attempts exposes review-safe commands:

```python
(
    "agileforge backlog refine-preview",
    (
        f"agileforge backlog refine-preview --project-id {project_id} "
        "--source-attempt-id <attempt_id> --operations-file <operations_file>"
    ),
),
(
    "agileforge backlog refine-record",
    (
        f"agileforge backlog refine-record --project-id {project_id} "
        "--source-attempt-id <attempt_id> "
        "--operations-file <operations_file> "
        "--expected-source-fingerprint <source_fingerprint> "
        "--expected-state SPRINT_COMPLETE "
        "--idempotency-key <idempotency_key>"
    ),
),
```

Do not expose `backlog save` from `SPRINT_COMPLETE`.

- [ ] **Step 3: Run tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_application_workflow_next_routes_sprint_complete_to_backlog_refinement -q
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "feat: guide next-cycle backlog refinement"
```

---

### Task 10: End-to-End CLI Smoke Tests

**Files:**
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Add runner-level refine-record smoke**

In `tests/test_agent_workbench_backlog_phase.py`, add a test using `BacklogPhaseRunner` fake workflow service:

```python
def test_backlog_runner_refine_record_returns_envelope() -> None:
    workflow = _FakeWorkflowService(
        {
            "fsm_state": "SPRINT_COMPLETE",
            "backlog_attempts": [
                {
                    "attempt_id": "backlog-attempt-1",
                    "artifact_fingerprint": "sha256:source",
                    "output_artifact": {
                        "backlog_items": [
                            {
                                "priority": 1,
                                "requirement": "Build Gate",
                                "authority_ref": "REQ.example",
                                "capability_hint": None,
                                "value_driver": "Strategic",
                                "justification": "Useful.",
                                "estimated_effort": "M",
                                "technical_note": None,
                            }
                        ],
                        "is_complete": False,
                        "clarifying_questions": [],
                    },
                }
            ],
            "compiled_authority_fingerprint": "sha256:authority",
            "as_built_assessment_cache_meta": {"assessment_fingerprint": "sha256:as-built"},
        }
    )
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepository(),
        workflow_service=workflow,
    )

    result = runner.refine_record(
        project_id=PROJECT_ID,
        source_attempt_id="backlog-attempt-1",
        operations_file="tests/fixtures/backlog_refine_ops_retitle.json",
        expected_source_fingerprint="sha256:source",
        expected_state="SPRINT_COMPLETE",
        idempotency_key="refine-smoke-1",
        approval_id=None,
    )

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "BACKLOG_REVIEW"
```

Create fixture file `tests/fixtures/backlog_refine_ops_retitle.json` if the runner reads files in tests:

```json
{
  "source_attempt_id": "backlog-attempt-1",
  "source_artifact_fingerprint": "sha256:source",
  "authority_fingerprint": "sha256:authority",
  "as_built_cache_fingerprint": "sha256:as-built",
  "operations": [
    {
      "operation_id": "op-retitle",
      "operation_type": "retitle",
      "source_item_ids": ["item-001"],
      "source_item_fingerprints": ["AUTO_SOURCE_ITEM_FINGERPRINT"],
      "result_item_ids": ["item-001"],
      "new_requirement": "Formalize/Verify Frozen Promotion Gate Evidence",
      "rationale": "Retitle as verification.",
      "requested_by": "po"
    }
  ]
}
```

- [ ] **Step 2: Add CLI schema smoke**

Run existing command schema via CLI in `tests/test_agent_workbench_cli.py`:

```python
def test_cli_command_schema_exposes_backlog_refine_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = _FakeApplication()

    exit_code = main(
        ["command", "schema", "agileforge backlog refine-record"],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["name"] == "agileforge backlog refine-record"
    assert payload["data"]["idempotency_required"] is True
```

- [ ] **Step 3: Run all related tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py tests/test_backlog_phase_service.py tests/test_agent_workbench_backlog_phase.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py -q
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent_workbench_backlog_phase.py tests/test_agent_workbench_cli.py tests/fixtures/backlog_refine_ops_retitle.json
git commit -m "test: cover backlog refinement cli smoke"
```

---

### Task 11: Documentation and Command Examples

**Files:**
- Modify: `docs/superpowers/specs/2026-06-01-backlog-refinement-attempts-design.md` only if implementation discovers a spec mismatch
- Modify: `README.md` or command docs only if this repo has an existing CLI docs section
- Test: command schema tests from Task 10

- [ ] **Step 1: Add command examples to CLI help if missing**

Modify the examples block in `cli/main.py`:

```python
  agileforge backlog refine-preview --project-id 1 --source-attempt-id <attempt_id> --operations-file refinement_ops.json
  agileforge backlog refine-record --project-id 1 --source-attempt-id <attempt_id> --operations-file refinement_ops.json --expected-source-fingerprint <fingerprint> --expected-state SPRINT_COMPLETE --idempotency-key refine-backlog-001
  agileforge backlog approve --project-id 1 --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --idempotency-key approve-refinement-001
```

- [ ] **Step 2: Run CLI help smoke**

Run:

```bash
uv run --frozen python -m cli.main --help >/tmp/agileforge-help.txt
rg "backlog refine-record|backlog approve" /tmp/agileforge-help.txt
```

Expected: both examples found.

- [ ] **Step 3: Commit**

```bash
git add cli/main.py README.md docs/superpowers/specs/2026-06-01-backlog-refinement-attempts-design.md
git commit -m "docs: document backlog refinement commands"
```

If only `cli/main.py` changed, stage only that file.

---

### Task 12: Final Verification

**Files:**
- No code edits unless verification finds a defect.

- [ ] **Step 1: Run focused suite**

Run:

```bash
uv run --frozen pytest tests/test_backlog_refinement_service.py tests/test_backlog_phase_service.py tests/test_agent_workbench_backlog_phase.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py -q
```

Expected: all pass.

- [ ] **Step 2: Run full repository check**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected:

- ruff passes;
- annotations pass;
- ty passes;
- bandit passes;
- pytest passes.

- [ ] **Step 3: Manual smoke against caRtola only after merge**

Do not run this before merge unless user asks. After merge, ask the caRtola agent to run:

```bash
agileforge backlog refine-preview \
  --project-id 2 \
  --source-artifact /Users/aaat/projects/caRtola/backlog-preview-host-annotations-20260601154652.json \
  --operations-file /Users/aaat/projects/caRtola/refinement_ops.json
```

Expected:

- `ok=true`;
- `data.persisted=false`;
- original backlog history unchanged;
- output artifact shows split/retitle changes;
- no `backlog save` is run.

- [ ] **Step 4: Final status**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: clean or only intentionally uncommitted files.

---

## Self-Review

Spec coverage:

- Typed operations: Tasks 1, 2, 7.
- Host-mediated approval: Task 3 and Task 6 CLI/facade.
- Canonical refined attempt: Task 4.
- Active draft postcondition: Task 4.
- Savable projection: Task 5.
- No second save path: Task 6 only routes existing `backlog save`.
- File import canonicalization: Task 7.
- Coarse stale markers: Task 4 and Task 8.
- Existing replacement guard honesty: Task 5 preserves current save guard and Task 12 smoke avoids save.
- Command discoverability: Task 6 and Task 9.

Known implementation constraints:

- Phase 1 does not implement versioned backlog persistence for completed projects.
- Phase 1 does not add a full Backlog Refiner LLM agent; it supports typed operations first.
- If `refine-import` needs broader split/merge inference than Task 7, add it behind deterministic `source_item_id` rules only.
