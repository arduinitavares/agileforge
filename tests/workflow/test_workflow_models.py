"""Fresh-schema contract tests for durable workflow persistence models."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import SQLModel

from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    DiscoveryRunAbandonment,
    InitialScopeRegistration,
    PrdDecision,
    PrdVersion,
    ProjectAbandonment,
    RepositoryBaseline,
    RepositoryInventory,
    ScopeExtensionReconciliation,
    ScopeExtensionRegistration,
    SpecDraft,
    SpecDraftDecision,
    SprintClosure,
    SprintStart,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)

EXPECTED_FIELDS: dict[type[SQLModel], set[str]] = {
    DiscoveryRun: {
        "discovery_run_id",
        "project_id",
        "purpose",
        "ordinal",
        "base_spec_version_id",
        "base_spec_hash",
        "created_at",
        "closed_at",
    },
    ChallengeArtifact: {
        "challenge_artifact_id",
        "project_id",
        "discovery_run_id",
        "version_number",
        "canonical_content_json",
        "content_fingerprint",
        "supersedes_challenge_artifact_id",
        "provenance_path",
        "created_at",
    },
    PrdVersion: {
        "prd_version_id",
        "project_id",
        "discovery_run_id",
        "version_number",
        "canonical_content_json",
        "content_fingerprint",
        "supersedes_prd_version_id",
        "provenance_path",
        "created_at",
    },
    PrdDecision: {
        "prd_decision_id",
        "project_id",
        "discovery_run_id",
        "prd_version_id",
        "artifact_fingerprint",
        "decision",
        "reviewer",
        "notes",
        "idempotency_key",
        "decided_at",
    },
    SpecDraft: {
        "spec_draft_id",
        "project_id",
        "discovery_run_id",
        "kind",
        "version_number",
        "canonical_content_json",
        "content_fingerprint",
        "base_spec_version_id",
        "base_spec_hash",
        "supersedes_spec_draft_id",
        "provenance_path",
        "created_at",
    },
    SpecDraftDecision: {
        "spec_draft_decision_id",
        "project_id",
        "discovery_run_id",
        "spec_draft_id",
        "artifact_fingerprint",
        "decision",
        "reviewer",
        "notes",
        "idempotency_key",
        "decided_at",
    },
    InitialScopeRegistration: {
        "initial_scope_registration_id",
        "project_id",
        "discovery_run_id",
        "spec_draft_id",
        "spec_version_id",
        "spec_hash",
        "registered_by",
        "registered_at",
    },
    ScopeExtensionRegistration: {
        "scope_extension_registration_id",
        "project_id",
        "discovery_run_id",
        "spec_draft_id",
        "spec_version_id",
        "spec_hash",
        "registered_by",
        "registered_at",
    },
    ScopeExtensionReconciliation: {
        "scope_extension_reconciliation_id",
        "project_id",
        "discovery_run_id",
        "replacement_authority_id",
        "replacement_authority_fingerprint",
        "artifact_references_json",
        "artifact_references_fingerprint",
        "reconciled_by",
        "reconciled_at",
    },
    ProjectAbandonment: {
        "project_abandonment_id",
        "project_id",
        "reason",
        "abandoned_by",
        "abandoned_at",
    },
    DiscoveryRunAbandonment: {
        "discovery_run_abandonment_id",
        "project_id",
        "discovery_run_id",
        "reason",
        "abandoned_by",
        "abandoned_at",
    },
    RepositoryBaseline: {
        "repository_baseline_id",
        "project_id",
        "repository_path",
        "git_commit",
        "dirty",
        "content_fingerprint",
        "version_number",
        "recorded_at",
    },
    RepositoryInventory: {
        "repository_inventory_id",
        "project_id",
        "repository_baseline_id",
        "canonical_inventory_json",
        "selected_for_model_json",
        "content_fingerprint",
        "version_number",
        "file_count",
        "total_bytes",
        "recorded_at",
    },
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
    ChallengeArtifact: "challenge_artifacts",
    DiscoveryRunAbandonment: "discovery_run_abandonments",
    DiscoveryRun: "discovery_runs",
    InitialScopeRegistration: "initial_scope_registrations",
    PrdDecision: "prd_decisions",
    PrdVersion: "prd_versions",
    ProjectAbandonment: "project_abandonments",
    RepositoryBaseline: "repository_baselines",
    RepositoryInventory: "repository_inventories",
    ScopeExtensionReconciliation: "scope_extension_reconciliations",
    ScopeExtensionRegistration: "scope_extension_registrations",
    SpecDraftDecision: "spec_draft_decisions",
    SpecDraft: "spec_drafts",
    SprintClosure: "sprint_closures",
    SprintStart: "sprint_starts",
    WorkflowNodeAttemptOutcome: "workflow_node_attempt_outcomes",
    WorkflowNodeAttempt: "workflow_node_attempts",
    WorkflowTransitionReceipt: "workflow_transition_receipts",
}

TEXT_FIELDS: dict[type[SQLModel], set[str]] = {
    ChallengeArtifact: {"canonical_content_json", "provenance_path"},
    PrdVersion: {"canonical_content_json", "provenance_path"},
    PrdDecision: {"notes"},
    SpecDraft: {"canonical_content_json", "provenance_path"},
    SpecDraftDecision: {"notes"},
    ProjectAbandonment: {"reason"},
    DiscoveryRunAbandonment: {"reason"},
    RepositoryBaseline: {"repository_path"},
    RepositoryInventory: {
        "canonical_inventory_json",
        "selected_for_model_json",
    },
    ScopeExtensionReconciliation: {"artifact_references_json"},
    SprintStart: {"selected_story_ids_json"},
    WorkflowNodeAttempt: {"normalized_input_json", "execution_settings_json"},
    WorkflowNodeAttemptOutcome: {"output_json", "failure_message"},
    WorkflowTransitionReceipt: {"request_json", "result_json"},
}


def test_workflow_models_have_exact_persisted_fields() -> None:
    """All workflow rows expose exactly the approved persisted fields."""
    for model, expected_fields in EXPECTED_FIELDS.items():
        assert set(model.model_fields) == expected_fields


def test_workflow_models_use_named_tables() -> None:
    """All workflow rows use the approved durable table names."""
    actual_names = {
        SQLModel.metadata.tables[table_name].name
        for table_name in EXPECTED_TABLE_NAMES.values()
    }

    assert actual_names == set(EXPECTED_TABLE_NAMES.values())


def test_payload_and_error_fields_use_text_columns() -> None:
    """Unbounded JSON, payload, path, note, and error fields use Text."""
    for model, field_names in TEXT_FIELDS.items():
        table = SQLModel.metadata.tables[EXPECTED_TABLE_NAMES[model]]
        for field_name in field_names:
            assert isinstance(table.c[field_name].type, Text)
