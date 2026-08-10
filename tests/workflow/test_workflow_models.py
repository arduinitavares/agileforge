"""Fresh-schema contract tests for retained workflow persistence models."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import SQLModel

from models.workflow import (
    SprintClosure,
    SprintStart,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)

EXPECTED_FIELDS: dict[type[SQLModel], set[str]] = {
    SprintStart: {
        "sprint_start_id",
        "project_id",
        "sprint_id",
        "sprint_plan_artifact_id",
        "sprint_plan_artifact_decision_id",
        "story_dependency_review_id",
        "plan_fingerprint",
        "candidate_set_fingerprint",
        "selected_story_ids_json",
        "task_content_fingerprint",
        "dependency_source_fingerprint",
        "dependency_fingerprint",
        "dependency_rows_fingerprint",
        "decision_fingerprint",
        "audit_event_id",
        "started_by",
        "started_at",
    },
    SprintClosure: {
        "sprint_closure_id",
        "project_id",
        "sprint_id",
        "review_fingerprint",
        "close_fingerprint",
        "closed_by",
        "closed_at",
    },
    WorkflowNodeAttempt: {
        "workflow_node_attempt_id",
        "project_id",
        "node_id",
        "instance_key",
        "graph_version",
        "fact_fingerprint",
        "business_fact_fingerprint",
        "decision_fingerprint",
        "normalized_input_json",
        "input_fingerprint",
        "model_id",
        "execution_settings_json",
        "idempotency_key",
        "actor",
        "correlation_id",
        "started_at",
        "lease_expires_at",
        "attempt_fingerprint",
    },
    WorkflowNodeAttemptOutcome: {
        "workflow_node_attempt_outcome_id",
        "project_id",
        "workflow_node_attempt_id",
        "status",
        "output_fingerprint",
        "output_json",
        "failure_code",
        "failure_message",
        "recorded_at",
    },
    WorkflowTransitionReceipt: {
        "workflow_transition_receipt_id",
        "request_kind",
        "idempotency_key",
        "request_fingerprint",
        "request_json",
        "result_json",
        "started_at",
        "completed_at",
    },
}

EXPECTED_TABLE_NAMES: dict[type[SQLModel], str] = {
    SprintClosure: "sprint_closures",
    SprintStart: "sprint_starts",
    WorkflowNodeAttemptOutcome: "workflow_node_attempt_outcomes",
    WorkflowNodeAttempt: "workflow_node_attempts",
    WorkflowTransitionReceipt: "workflow_transition_receipts",
}

TEXT_FIELDS: dict[type[SQLModel], set[str]] = {
    SprintStart: {"selected_story_ids_json"},
    WorkflowNodeAttempt: {"normalized_input_json", "execution_settings_json"},
    WorkflowNodeAttemptOutcome: {"output_json", "failure_message"},
    WorkflowTransitionReceipt: {"request_json", "result_json"},
}


def test_workflow_models_have_exact_persisted_fields() -> None:
    """Retained workflow rows expose exactly the approved persisted fields."""
    for model, expected_fields in EXPECTED_FIELDS.items():
        assert set(model.model_fields) == expected_fields


def test_workflow_models_use_named_tables() -> None:
    """Retained workflow rows use the approved durable table names."""
    actual_names = {
        SQLModel.metadata.tables[table_name].name
        for table_name in EXPECTED_TABLE_NAMES.values()
    }

    assert actual_names == set(EXPECTED_TABLE_NAMES.values())


def test_payload_and_error_fields_use_text_columns() -> None:
    """Unbounded payload and error fields use Text."""
    for model, field_names in TEXT_FIELDS.items():
        table = SQLModel.metadata.tables[EXPECTED_TABLE_NAMES[model]]
        for field_name in field_names:
            assert isinstance(table.c[field_name].type, Text)
