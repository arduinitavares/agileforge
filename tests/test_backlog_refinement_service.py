"""Tests for backlog refinement operation helpers."""

import json
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from models.agent_workbench import CliMutationLedger
from models.core import Product
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench.backlog_refinement_events import (
    BACKLOG_REFINEMENT_APPROVE_COMMAND,
    BacklogRefinementApprovalError,
    BacklogRefinementApprovalRequest,
    approval_request_fingerprint,
    record_backlog_refinement_approval,
)
from services.agent_workbench.mutation_ledger import (
    LedgerLoadResult,
    MutationLedgerRepository,
    MutationStatus,
)
from services.phases.backlog_refinement import (
    AddIntakeOperation,
    AmbiguousRefinementDiffError,
    AuthorityRefChangeOperation,
    BacklogRefinementError,
    BacklogRefinementOperationSet,
    ClassifyOperation,
    DeleteOperation,
    MergeOperation,
    ReorderOperation,
    RetitleOperation,
    RewriteScopeOperation,
    SplitOperation,
    UnsupportedAuthorityRefError,
    apply_refinement_operations,
    assign_item_identity,
    canonical_operations_fingerprint,
    normalize_refined_artifact,
    operations_from_edited_artifact,
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


def _operation_set(operations: list[Any]) -> BacklogRefinementOperationSet:
    return BacklogRefinementOperationSet(
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
        operations=operations,
    )


def _approval_request(**overrides: object) -> BacklogRefinementApprovalRequest:
    payload: dict[str, object] = {
        "project_id": 7,
        "source_attempt_id": "backlog-attempt-1",
        "operation_set_fingerprint": "sha256:operations",
        "approved_artifact_fingerprint": "sha256:artifact",
        "approved_operation_ids": ["op-1"],
        "approval_source": "cli",
        "idempotency_key": "approve-1",
        "approved_by": "po",
    }
    payload.update(overrides)
    return BacklogRefinementApprovalRequest.model_validate(payload)


def _approval_event_engine() -> Engine:
    assert Product.__tablename__ == "products"
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Product(product_id=7, name="Backlog refinement test"))
        session.commit()
    return engine


def _workflow_events(session: Session) -> list[WorkflowEvent]:
    return list(session.exec(select(WorkflowEvent)).all())


def _ledger_rows(session: Session) -> list[CliMutationLedger]:
    return list(session.exec(select(CliMutationLedger)).all())


ORPHAN_AND_RECORDED_APPROVAL_EVENT_COUNT = 2


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


def test_operations_from_edited_artifact_detects_retitle_by_source_item_id() -> None:
    """Edited artifacts retitle source-linked items deterministically."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Validate existing flow")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    edited = {
        "backlog_items": [
            {
                **_item(1, "Validate canonical existing flow"),
                "source_item_id": "item-001",
            }
        ]
    }

    operation_set = operations_from_edited_artifact(
        source,
        edited,
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
    )

    assert operation_set.source_attempt_id == "backlog-attempt-1"
    assert operation_set.source_artifact_fingerprint == "sha256:source"
    operation = operation_set.operations[0]
    assert isinstance(operation, RetitleOperation)
    assert operation.source_item_ids == ["item-001"]
    assert operation.source_item_fingerprints == [
        source["backlog_items"][0]["item_fingerprint"]
    ]
    assert operation.result_item_ids == ["item-001"]
    assert operation.new_requirement == "Validate canonical existing flow"


def test_operations_from_edited_artifact_rejects_unlinked_new_items() -> None:
    """Edited implementation items without source identity fail closed."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Validate existing flow")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    edited = {"backlog_items": [_item(1, "Build unrelated new work")]}

    with pytest.raises(AmbiguousRefinementDiffError):
        operations_from_edited_artifact(
            source,
            edited,
            authority_fingerprint="sha256:authority",
            as_built_cache_fingerprint="sha256:as-built",
        )


def test_operations_from_edited_artifact_detects_deleted_source_items() -> None:
    """Missing source-linked items become deterministic delete operations."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "Keep existing flow"),
                _item(2, "Remove obsolete flow"),
            ]
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    edited = {
        "backlog_items": [
            {
                **_item(1, "Keep existing flow"),
                "source_item_id": "item-001",
            }
        ]
    }

    operation_set = operations_from_edited_artifact(
        source,
        edited,
        authority_fingerprint="sha256:authority",
        as_built_cache_fingerprint="sha256:as-built",
    )

    operation = operation_set.operations[0]
    assert isinstance(operation, DeleteOperation)
    assert operation.source_item_ids == ["item-002"]
    assert operation.source_item_fingerprints == [
        source["backlog_items"][1]["item_fingerprint"]
    ]


def test_operations_from_edited_artifact_rejects_reordered_priority_only_edit() -> None:
    """Edited artifacts cannot silently reorder source-linked items in Task 7."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "Keep first flow"),
                _item(2, "Keep second flow"),
            ]
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    edited = {
        "backlog_items": [
            {
                **_item(1, "Keep second flow"),
                "source_item_id": "item-002",
            },
            {
                **_item(2, "Keep first flow"),
                "source_item_id": "item-001",
            },
        ]
    }

    with pytest.raises(AmbiguousRefinementDiffError):
        operations_from_edited_artifact(
            source,
            edited,
            authority_fingerprint="sha256:authority",
            as_built_cache_fingerprint="sha256:as-built",
        )


def test_operations_from_edited_artifact_rejects_no_op_import() -> None:
    """Identical source-linked edits fail closed instead of producing zero ops."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Validate existing flow")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    edited = {
        "backlog_items": [
            {
                **_item(1, "Validate existing flow"),
                "source_item_id": "item-001",
            }
        ]
    }

    with pytest.raises(AmbiguousRefinementDiffError) as exc_info:
        operations_from_edited_artifact(
            source,
            edited,
            authority_fingerprint="sha256:authority",
            as_built_cache_fingerprint="sha256:as-built",
        )

    assert "no-op" in str(exc_info.value)


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


def test_approval_request_rejects_proposer_authored_metadata() -> None:
    """Approval requests reject proposer-authored approval metadata."""
    with pytest.raises(ValidationError):
        BacklogRefinementApprovalRequest.model_validate(
            {
                "project_id": 7,
                "source_attempt_id": "backlog-attempt-1",
                "operation_set_fingerprint": "sha256:operations",
                "approved_artifact_fingerprint": "sha256:artifact",
                "approved_operation_ids": ["op-1"],
                "approval_source": "cli",
                "idempotency_key": "approve-1",
                "approved_by": "po",
                "approval": {"status": "po_reviewed"},
            }
        )


def test_approval_request_fingerprint_canonicalizes_operation_ids() -> None:
    """Approval fingerprints treat operation ids as a canonical set."""
    first = _approval_request(approved_operation_ids=["op-2", "op-1"])
    second = _approval_request(approved_operation_ids=["op-1", "op-2"])

    assert approval_request_fingerprint(first) == approval_request_fingerprint(second)


def test_record_backlog_refinement_approval_writes_append_only_event() -> None:
    """Host approval writes a WorkflowEvent with canonical metadata."""
    engine = _approval_event_engine()
    request = _approval_request()

    with Session(engine) as session:
        result = record_backlog_refinement_approval(
            session,
            request=request,
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )
        events = _workflow_events(session)
        ledgers = _ledger_rows(session)

    metadata = json.loads(events[0].event_metadata or "{}")
    ledger_response = json.loads(ledgers[0].response_json or "{}")
    assert result["approval_id"].startswith("approval:")
    assert result["idempotent_replay"] is False
    assert result["approval_id"] == metadata["approval_id"]
    assert events[0].event_type == WorkflowEventType.BACKLOG_REFINEMENT_APPROVED
    assert events[0].product_id == request.project_id
    assert metadata["command"] == "agileforge backlog approve"
    assert metadata["source_attempt_id"] == "backlog-attempt-1"
    assert metadata["approved_operation_ids"] == ["op-1"]
    assert "approval" not in metadata
    assert ledgers[0].command == BACKLOG_REFINEMENT_APPROVE_COMMAND
    assert ledgers[0].idempotency_key == request.idempotency_key
    assert ledgers[0].request_hash == result["request_fingerprint"]
    assert ledgers[0].status == MutationStatus.SUCCEEDED.value
    assert ledger_response["approval_id"] == result["approval_id"]
    assert ledger_response["idempotent_replay"] is True


def test_record_backlog_refinement_approval_replays_same_key() -> None:
    """Same idempotency key and request fingerprint replays the first result."""
    engine = _approval_event_engine()
    request = _approval_request()

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
        events = _workflow_events(session)
        ledgers = _ledger_rows(session)

    assert second["idempotent_replay"] is True
    assert second["approval_id"] == first["approval_id"]
    assert second["request_fingerprint"] == first["request_fingerprint"]
    assert len(events) == 1
    assert len(ledgers) == 1


def test_record_approval_rejects_reused_key_for_changed_request() -> None:
    """Same idempotency key with different approval inputs fails closed."""
    engine = _approval_event_engine()
    request = _approval_request()
    changed_request = _approval_request(approved_operation_ids=["op-2"])

    with Session(engine) as session:
        record_backlog_refinement_approval(
            session,
            request=request,
            now_iso=lambda: "2026-06-01T00:00:00Z",
        )
        with pytest.raises(BacklogRefinementApprovalError):
            record_backlog_refinement_approval(
                session,
                request=changed_request,
                now_iso=lambda: "2026-06-01T00:00:01Z",
            )
        events = _workflow_events(session)
        ledgers = _ledger_rows(session)

    assert len(events) == 1
    assert len(ledgers) == 1


def test_record_approval_does_not_replay_from_workflow_events_without_ledger() -> None:
    """An orphan WorkflowEvent cannot satisfy approval idempotency replay."""
    engine = _approval_event_engine()
    request = _approval_request()
    request_fingerprint = approval_request_fingerprint(request)
    orphan_metadata = {
        "action": "backlog_refinement_approved",
        "approval_id": "approval:orphan",
        "request_fingerprint": request_fingerprint,
        "approved_at": "2026-06-01T00:00:00Z",
        "command": BACKLOG_REFINEMENT_APPROVE_COMMAND,
        "project_id": request.project_id,
        "source_attempt_id": request.source_attempt_id,
        "attempt_id": request.attempt_id,
        "operation_set_fingerprint": request.operation_set_fingerprint,
        "approved_artifact_fingerprint": request.approved_artifact_fingerprint,
        "approved_operation_ids": request.approved_operation_ids,
        "approval_source": request.approval_source,
        "idempotency_key": request.idempotency_key,
        "approved_by": request.approved_by,
    }

    with Session(engine) as session:
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.BACKLOG_REFINEMENT_APPROVED,
                product_id=request.project_id,
                session_id=str(request.project_id),
                event_metadata=json.dumps(orphan_metadata, sort_keys=True),
            )
        )
        session.commit()
        result = record_backlog_refinement_approval(
            session,
            request=request,
            now_iso=lambda: "2026-06-01T00:00:01Z",
        )
        events = _workflow_events(session)
        ledgers = _ledger_rows(session)

    assert result["idempotent_replay"] is False
    assert result["approval_id"] != "approval:orphan"
    assert len(events) == ORPHAN_AND_RECORDED_APPROVAL_EVENT_COUNT
    assert len(ledgers) == 1
    assert ledgers[0].status == MutationStatus.SUCCEEDED.value


def test_record_approval_fails_closed_when_ledger_finalization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approval event rolls back if ledger success cannot be stored."""
    engine = _approval_event_engine()
    request = _approval_request()
    original_create_or_load = MutationLedgerRepository.create_or_load

    def create_then_steal_lease(
        self: MutationLedgerRepository,
        **kwargs: object,
    ) -> LedgerLoadResult:
        loaded = original_create_or_load(self, **kwargs)
        event_id = loaded.ledger.mutation_event_id
        assert event_id is not None
        with Session(engine) as ledger_session:
            ledger = ledger_session.get(CliMutationLedger, event_id)
            assert ledger is not None
            ledger.lease_owner = "other-worker"
            ledger_session.add(ledger)
            ledger_session.commit()
        return loaded

    monkeypatch.setattr(
        MutationLedgerRepository,
        "create_or_load",
        create_then_steal_lease,
    )

    with Session(engine) as session:
        with pytest.raises(BacklogRefinementApprovalError):
            record_backlog_refinement_approval(
                session,
                request=request,
                now_iso=lambda: "2026-06-01T00:00:00Z",
            )
        events = _workflow_events(session)
        ledgers = _ledger_rows(session)

    assert events == []
    assert len(ledgers) == 1
    assert ledgers[0].status == MutationStatus.PENDING.value


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
    """Classify and authority-ref changes reject bogus result item ids."""
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


def test_plan_shaped_retitle_operation_accepts_source_result_item_id() -> None:
    """Retitle operations accept plan-shaped in-place result item ids."""
    operation = RetitleOperation(
        operation_id="op-retitle",
        source_item_ids=["item-001"],
        source_item_fingerprints=["sha256:item-001"],
        result_item_ids=["item-001"],
        new_requirement="Clarify existing verification item",
        rationale="Retitle in place.",
        requested_by="po",
    )

    assert operation.result_item_ids == ["item-001"]


def test_plan_shaped_authority_ref_change_accepts_source_result_item_id() -> None:
    """Authority-ref changes accept plan-shaped in-place result item ids."""
    operation = AuthorityRefChangeOperation(
        operation_id="op-authority",
        source_item_ids=["item-001"],
        source_item_fingerprints=["sha256:item-001"],
        result_item_ids=["item-001"],
        old_authority_ref="REQ.old",
        new_authority_ref="REQ.new",
        rationale="Change authority ref in place.",
        requested_by="po",
    )

    assert operation.result_item_ids == ["item-001"]


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


def test_split_operation_rejects_duplicate_result_item_ids() -> None:
    """Split operations cannot produce duplicate result item ids."""
    with pytest.raises(ValidationError):
        SplitOperation(
            operation_id="op-split",
            source_item_ids=["item-001"],
            source_item_fingerprints=["sha256:item-001"],
            result_item_ids=["item-new", "item-new"],
            result_items=[
                _item(1, "Validate existing behavior"),
                _item(2, "Discover missing behavior"),
            ],
            rationale="Split into distinct items.",
            requested_by="po",
        )


def test_merge_operation_rejects_duplicate_source_item_ids() -> None:
    """Merge operations cannot consume the same source item more than once."""
    with pytest.raises(ValidationError):
        MergeOperation(
            operation_id="op-merge",
            source_item_ids=["item-001", "item-001"],
            source_item_fingerprints=["sha256:item-001", "sha256:item-001"],
            result_item_ids=["item-merged"],
            result_item=_item(1, "Merged backlog item"),
            rationale="Merge distinct items.",
            requested_by="po",
        )


def test_retitle_operation_rejects_result_item_ids() -> None:
    """Retitle operations reject bogus result item ids."""
    with pytest.raises(ValidationError):
        RetitleOperation(
            operation_id="op-retitle",
            source_item_ids=["item-001"],
            source_item_fingerprints=["sha256:item-001"],
            result_item_ids=["item-bogus"],
            new_requirement="Clarify existing verification item",
            rationale="Retitle in place.",
            requested_by="po",
        )


def test_rewrite_scope_operation_rejects_result_item_ids() -> None:
    """Rewrite-scope operations reject bogus result item ids."""
    with pytest.raises(ValidationError):
        RewriteScopeOperation(
            operation_id="op-rewrite",
            source_item_ids=["item-001"],
            source_item_fingerprints=["sha256:item-001"],
            result_item_ids=["item-bogus"],
            field_updates={"technical_note": "Clarify existing scope."},
            rationale="Rewrite in place.",
            requested_by="po",
        )


def test_unsupported_authority_ref_error_is_domain_error() -> None:
    """UnsupportedAuthorityRefError is a domain-specific exception type."""
    assert issubclass(UnsupportedAuthorityRefError, Exception)


def test_apply_split_replaces_one_item_with_two_results() -> None:
    """Split operations replace one source item with multiple refined items."""
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


def test_apply_split_rejects_result_id_colliding_with_retained_item() -> None:
    """Split result item ids cannot collide with retained item ids."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "Mixed optimizer work"),
                _item(2, "Retained promotion gate work"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    retained_item = source["backlog_items"][1]
    operation_set = _operation_set(
        [
            SplitOperation(
                operation_id="op-split",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[retained_item["item_id"], "item-new"],
                result_items=[
                    _item(1, "Validate optimizer work"),
                    _item(2, "Discover optimizer gaps"),
                ],
                rationale="Split into distinct items.",
                requested_by="po",
            )
        ]
    )

    with pytest.raises(BacklogRefinementError):
        apply_refinement_operations(source, operation_set)


def test_apply_split_rejects_duplicate_source_artifact_item_ids() -> None:
    """Operation application fails closed when source artifact item ids duplicate."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "First duplicated optimizer work"),
                _item(2, "Second duplicated optimizer work"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    first_item = source["backlog_items"][0]
    source["backlog_items"][1]["item_id"] = first_item["item_id"]
    source["backlog_items"][1]["item_fingerprint"] = first_item["item_fingerprint"]
    operation_set = _operation_set(
        [
            SplitOperation(
                operation_id="op-split",
                source_item_ids=[first_item["item_id"]],
                source_item_fingerprints=[first_item["item_fingerprint"]],
                result_item_ids=["item-new-a", "item-new-b"],
                result_items=[
                    _item(1, "Validate optimizer work"),
                    _item(2, "Discover optimizer gaps"),
                ],
                rationale="Split into distinct items.",
                requested_by="po",
            )
        ]
    )

    with pytest.raises(BacklogRefinementError):
        apply_refinement_operations(source, operation_set)


def test_apply_rejects_duplicate_source_artifact_item_ids_without_operations() -> None:
    """Operation application validates source artifact item ids before replay."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "First duplicated optimizer work"),
                _item(2, "Second duplicated optimizer work"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source["backlog_items"][1]["item_id"] = source["backlog_items"][0]["item_id"]
    operation_set = _operation_set([])

    with pytest.raises(BacklogRefinementError):
        apply_refinement_operations(source, operation_set)


def test_apply_split_rejects_result_id_reusing_source_item_id() -> None:
    """Split result item ids cannot reuse any current source item id."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Mixed optimizer work")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = _operation_set(
        [
            SplitOperation(
                operation_id="op-split",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[source_item["item_id"], "item-new"],
                result_items=[
                    _item(1, "Validate optimizer work"),
                    _item(2, "Discover optimizer gaps"),
                ],
                rationale="Split into distinct items.",
                requested_by="po",
            )
        ]
    )

    with pytest.raises(BacklogRefinementError):
        apply_refinement_operations(source, operation_set)


def test_apply_retitle_changes_requirement_only() -> None:
    """Retitle operations change the requirement while preserving item position."""
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
                result_item_ids=[],
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


def test_apply_retitle_accepts_plan_shaped_source_result_item_id() -> None:
    """Retitle operations apply when result_item_ids names the same source item."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Build Promotion Gate")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = _operation_set(
        [
            RetitleOperation(
                operation_id="op-retitle",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[source_item["item_id"]],
                new_requirement="Formalize/Verify Frozen Promotion Gate Evidence",
                rationale="Retitle as verification.",
                requested_by="po",
            )
        ]
    )

    refined = apply_refinement_operations(source, operation_set)

    assert refined["backlog_items"][0]["item_id"] == source_item["item_id"]
    assert refined["backlog_items"][0]["requirement"] == (
        "Formalize/Verify Frozen Promotion Gate Evidence"
    )


def test_apply_retitle_rejects_stale_content_with_stored_fingerprint() -> None:
    """Source matching recomputes fingerprints from current item content."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Original promotion gate")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    stale_fingerprint = source_item["item_fingerprint"]
    source_item["requirement"] = "Changed promotion gate"
    source_item["item_fingerprint"] = stale_fingerprint
    operation_set = _operation_set(
        [
            RetitleOperation(
                operation_id="op-retitle",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[stale_fingerprint],
                result_item_ids=[],
                new_requirement="Formalize/Verify Frozen Promotion Gate Evidence",
                rationale="Retitle stale source.",
                requested_by="po",
            )
        ]
    )

    with pytest.raises(BacklogRefinementError):
        apply_refinement_operations(source, operation_set)


def test_apply_merge_replaces_multiple_items_with_one_result() -> None:
    """Merge operations replace multiple sources with one refined item."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "Validate imported injuries"),
                _item(2, "Validate late scratches"),
                _item(3, "Keep optimizer stable"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    first_item = source["backlog_items"][0]
    second_item = source["backlog_items"][1]
    operation_set = _operation_set(
        [
            MergeOperation(
                operation_id="op-merge",
                source_item_ids=[first_item["item_id"], second_item["item_id"]],
                source_item_fingerprints=[
                    first_item["item_fingerprint"],
                    second_item["item_fingerprint"],
                ],
                result_item_ids=["item-merged"],
                result_item=_item(1, "Validate late-player availability feed"),
                rationale="Keep related verification together.",
                requested_by="po",
            )
        ]
    )

    refined = apply_refinement_operations(source, operation_set)

    assert [item["requirement"] for item in refined["backlog_items"]] == [
        "Validate late-player availability feed",
        "Keep optimizer stable",
    ]
    assert refined["backlog_items"][0]["item_id"] == "item-merged"


def test_apply_merge_rejects_result_id_colliding_with_retained_item() -> None:
    """Merge result item ids cannot collide with retained item ids."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "Validate imported injuries"),
                _item(2, "Validate late scratches"),
                _item(3, "Retained optimizer work"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    first_item = source["backlog_items"][0]
    second_item = source["backlog_items"][1]
    retained_item = source["backlog_items"][2]
    operation_set = _operation_set(
        [
            MergeOperation(
                operation_id="op-merge",
                source_item_ids=[first_item["item_id"], second_item["item_id"]],
                source_item_fingerprints=[
                    first_item["item_fingerprint"],
                    second_item["item_fingerprint"],
                ],
                result_item_ids=[retained_item["item_id"]],
                result_item=_item(1, "Validate late-player availability feed"),
                rationale="Keep related verification together.",
                requested_by="po",
            )
        ]
    )

    with pytest.raises(BacklogRefinementError):
        apply_refinement_operations(source, operation_set)


def test_apply_merge_rejects_result_id_reusing_source_item_id() -> None:
    """Merge result item ids cannot reuse any current source item id."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "Validate imported injuries"),
                _item(2, "Validate late scratches"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    first_item = source["backlog_items"][0]
    second_item = source["backlog_items"][1]
    operation_set = _operation_set(
        [
            MergeOperation(
                operation_id="op-merge",
                source_item_ids=[first_item["item_id"], second_item["item_id"]],
                source_item_fingerprints=[
                    first_item["item_fingerprint"],
                    second_item["item_fingerprint"],
                ],
                result_item_ids=[first_item["item_id"]],
                result_item=_item(1, "Validate late-player availability feed"),
                rationale="Keep related verification together.",
                requested_by="po",
            )
        ]
    )

    with pytest.raises(BacklogRefinementError):
        apply_refinement_operations(source, operation_set)


def test_apply_rewrite_scope_updates_allowed_fields() -> None:
    """Rewrite-scope operations update validated mutable backlog fields."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Verify optimizer contract")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = _operation_set(
        [
            RewriteScopeOperation(
                operation_id="op-rewrite",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[],
                field_updates={
                    "technical_note": "Verify only; do not add optimizer scope.",
                    "estimated_effort": "S",
                },
                rationale="Clarify verification boundary.",
                requested_by="po",
            )
        ]
    )

    refined = apply_refinement_operations(source, operation_set)

    assert refined["backlog_items"][0]["requirement"] == "Verify optimizer contract"
    assert refined["backlog_items"][0]["technical_note"] == (
        "Verify only; do not add optimizer scope."
    )
    assert refined["backlog_items"][0]["estimated_effort"] == "S"


def test_apply_reorder_recomputes_priorities() -> None:
    """Reorder operations reorder by item id and normalize priority values."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "First item"),
                _item(2, "Second item"),
                _item(3, "Third item"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    operation_set = _operation_set(
        [
            ReorderOperation(
                operation_id="op-reorder",
                source_item_ids=[],
                source_item_fingerprints=[],
                result_item_ids=[],
                ordered_item_ids=["item-003", "item-001", "item-002"],
                rationale="Prioritize the third item.",
                requested_by="po",
            )
        ]
    )

    refined = apply_refinement_operations(source, operation_set)

    assert [item["item_id"] for item in refined["backlog_items"]] == [
        "item-003",
        "item-001",
        "item-002",
    ]
    assert [item["priority"] for item in refined["backlog_items"]] == [1, 2, 3]


def test_apply_classify_updates_classification() -> None:
    """Classify operations update host classification for one item."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Verify generated output")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = _operation_set(
        [
            ClassifyOperation(
                operation_id="op-classify",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[],
                classification="verification",
                rationale="This is existing behavior verification.",
                requested_by="po",
            )
        ]
    )

    refined = apply_refinement_operations(source, operation_set)

    assert refined["backlog_items"][0]["classification"] == "verification"


def test_apply_delete_removes_item_and_compacts_priorities() -> None:
    """Delete operations remove source items before normalization."""
    source = assign_item_identity(
        {
            "backlog_items": [
                _item(1, "Keep this item"),
                _item(2, "Remove this item"),
                _item(3, "Keep this later item"),
            ],
            "clarifying_questions": [],
        },
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][1]
    operation_set = _operation_set(
        [
            DeleteOperation(
                operation_id="op-delete",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[],
                rationale="Remove duplicate work.",
                requested_by="po",
            )
        ]
    )

    refined = apply_refinement_operations(source, operation_set)

    assert [item["requirement"] for item in refined["backlog_items"]] == [
        "Keep this item",
        "Keep this later item",
    ]
    assert [item["priority"] for item in refined["backlog_items"]] == [1, 2]


def test_apply_add_intake_adds_non_savable_intake_item() -> None:
    """Add-intake operations append authority-gap intake outside backlog items."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Validate existing")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    operation_set = _operation_set(
        [
            AddIntakeOperation(
                operation_id="op-intake",
                source_item_ids=[],
                source_item_fingerprints=[],
                result_item_ids=["item-intake"],
                result_item=_item(2, "Discover unsupported promotion rule"),
                authority_gap_ref="REQ.new-gap",
                rationale="Capture unsupported authority gap as intake.",
                requested_by="agent",
            )
        ]
    )

    refined = apply_refinement_operations(source, operation_set)

    assert [item["requirement"] for item in refined["backlog_items"]] == [
        "Validate existing"
    ]
    assert refined["backlog_intake_items"][0]["item_id"] == "item-intake"
    assert refined["backlog_intake_items"][0]["classification"] == (
        "authority_gap_intake"
    )


def test_apply_authority_ref_change_rejects_unbacked_ref_without_intake() -> None:
    """Authority-ref changes fail closed when the new ref is unsupported."""
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
                result_item_ids=[],
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


def test_apply_authority_ref_change_accepts_plan_shaped_source_result_item_id() -> None:
    """Authority-ref changes apply when result_item_ids names the source item."""
    source = assign_item_identity(
        {"backlog_items": [_item(1, "Validate existing")]},
        source_attempt_id="backlog-attempt-1",
        source_artifact_fingerprint="sha256:source",
    )
    source_item = source["backlog_items"][0]
    operation_set = _operation_set(
        [
            AuthorityRefChangeOperation(
                operation_id="op-authority",
                source_item_ids=[source_item["item_id"]],
                source_item_fingerprints=[source_item["item_fingerprint"]],
                result_item_ids=[source_item["item_id"]],
                old_authority_ref="REQ.example",
                new_authority_ref="REQ.supported",
                rationale="Supported authority change.",
                requested_by="po",
            )
        ]
    )

    refined = apply_refinement_operations(
        source,
        operation_set,
        supported_authority_refs={"REQ.supported"},
    )

    assert refined["backlog_items"][0]["item_id"] == source_item["item_id"]
    assert refined["backlog_items"][0]["authority_ref"] == "REQ.supported"
