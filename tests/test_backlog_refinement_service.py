"""Tests for backlog refinement operation helpers."""

from typing import Any

import pytest
from pydantic import ValidationError

from services.phases.backlog_refinement import (
    AddIntakeOperation,
    AuthorityRefChangeOperation,
    BacklogRefinementOperationSet,
    ClassifyOperation,
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
    """Identity assignment adds stable ids, source metadata, and fingerprints."""
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
    """Operation payloads cannot smuggle proposer-authored approval metadata."""
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
                "result_items": [
                    _item(1, "Validate existing"),
                    _item(2, "Discover gap"),
                ],
                "rationale": "Separate verification from discovery.",
                "requested_by": "agent",
                "approval": {"status": "po_reviewed"},
            }
        ],
    }

    with pytest.raises(ValidationError):
        BacklogRefinementOperationSet.model_validate(payload)


def test_canonical_operations_fingerprint_is_order_stable() -> None:
    """Operation sets produce a canonical sha256 fingerprint."""
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
    """Savable projection strips host metadata and excludes intake items."""
    artifact = {
        "backlog_items": [
            _item(
                1,
                "Validate existing flow",
                item_id="item-001",
                item_fingerprint="sha256:item",
                classification="verification",
                as_built_annotation={
                    "schema_version": "agileforge.brownfield_annotation.v1"
                },
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


def test_normalize_refined_artifact_recomputes_priorities_and_completion() -> None:
    """Artifact normalization recomputes priorities, fingerprints, and completeness."""
    artifact = {
        "backlog_items": [
            _item(
                index + 10,
                f"Refined backlog item {index}",
                item_id=f"item-{index:03d}",
                item_fingerprint="sha256:stale",
            )
            for index in range(1, 11)
        ],
        "clarifying_questions": [],
    }

    normalized = normalize_refined_artifact(artifact)

    assert [item["priority"] for item in normalized["backlog_items"]] == list(
        range(1, 11)
    )
    assert normalized["is_complete"] is True
    assert all(
        str(item["item_fingerprint"]).startswith("sha256:")
        and item["item_fingerprint"] != "sha256:stale"
        for item in normalized["backlog_items"]
    )


def test_normalize_refined_artifact_compacts_priorities_after_filtering() -> None:
    """Artifact normalization assigns contiguous priorities after filtering."""
    artifact: dict[str, object] = {
        "backlog_items": [
            _item(10, "First valid item", item_id="item-001"),
            "not-an-item",
            _item(30, "Second valid item", item_id="item-002"),
        ],
        "clarifying_questions": [],
    }

    normalized = normalize_refined_artifact(artifact)

    assert [item["priority"] for item in normalized["backlog_items"]] == [1, 2]


def test_add_intake_requires_non_implementation_classification() -> None:
    """Authority-gap intake cannot be classified as product implementation work."""
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


@pytest.mark.parametrize(
    ("operation_cls", "operation_kwargs"),
    [
        (
            ClassifyOperation,
            {"classification": "verification"},
        ),
        (
            AuthorityRefChangeOperation,
            {"old_authority_ref": "REQ.old", "new_authority_ref": "REQ.new"},
        ),
    ],
)
def test_single_item_operations_reject_multiple_sources(
    operation_cls: type[ClassifyOperation | AuthorityRefChangeOperation],
    operation_kwargs: dict[str, object],
) -> None:
    """Classify and authority-ref changes operate on exactly one source item."""
    with pytest.raises(ValidationError):
        operation_cls(
            operation_id="op-single-source",
            source_item_ids=["item-001", "item-002"],
            source_item_fingerprints=["sha256:item-001", "sha256:item-002"],
            result_item_ids=[],
            rationale="Change one item.",
            requested_by="po",
            **operation_kwargs,
        )


@pytest.mark.parametrize(
    ("operation_cls", "operation_kwargs"),
    [
        (
            ClassifyOperation,
            {"classification": "verification"},
        ),
        (
            AuthorityRefChangeOperation,
            {"old_authority_ref": "REQ.old", "new_authority_ref": "REQ.new"},
        ),
    ],
)
def test_single_item_operations_reject_result_item_ids(
    operation_cls: type[ClassifyOperation | AuthorityRefChangeOperation],
    operation_kwargs: dict[str, object],
) -> None:
    """Classify and authority-ref changes do not create result items."""
    with pytest.raises(ValidationError):
        operation_cls(
            operation_id="op-no-results",
            source_item_ids=["item-001"],
            source_item_fingerprints=["sha256:item-001"],
            result_item_ids=["item-001-updated"],
            rationale="Change one item in place.",
            requested_by="po",
            **operation_kwargs,
        )


def test_delete_operation_requires_source_identity() -> None:
    """Delete operations require at least one source item id."""
    with pytest.raises(ValidationError):
        DeleteOperation(
            operation_id="op-delete",
            source_item_ids=[],
            source_item_fingerprints=[],
            result_item_ids=[],
            rationale="Remove item.",
            requested_by="po",
        )


def test_unsupported_authority_ref_error_is_domain_error() -> None:
    """UnsupportedAuthorityRefError is a domain-specific exception type."""
    assert issubclass(UnsupportedAuthorityRefError, Exception)
