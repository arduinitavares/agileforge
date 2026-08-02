"""Durable SQLModel records for the domain workflow graph."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.schema import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class DiscoveryRun(SQLModel, table=True):
    """One initial or extension discovery sequence for a Project shell."""

    __tablename__ = "discovery_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            name="uq_discovery_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "purpose",
            "ordinal",
            name="uq_discovery_purpose_ordinal",
        ),
        CheckConstraint(
            "purpose IN ('initial', 'extension')",
            name="ck_discovery_purpose",
        ),
        Index(
            "uq_initial_discovery_per_project",
            "project_id",
            unique=True,
            sqlite_where=text("purpose = 'initial'"),
        ),
        Index(
            "uq_open_extension_per_project",
            "project_id",
            unique=True,
            sqlite_where=text("purpose = 'extension' AND closed_at IS NULL"),
        ),
    )

    discovery_run_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    purpose: str = Field(index=True)
    ordinal: int
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    closed_at: datetime | None = Field(default=None)


class ProjectAbandonment(SQLModel, table=True):
    """Typed abandonment record for an unactivated Project shell."""

    __tablename__ = "project_abandonments"
    __table_args__ = (UniqueConstraint("project_id", name="uq_project_abandonment"),)

    project_abandonment_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    reason: str = Field(sa_type=Text)
    abandoned_by: str = Field(index=True)
    abandoned_at: datetime = Field(default_factory=utc_now, nullable=False)


class DiscoveryRunAbandonment(SQLModel, table=True):
    """Typed abandonment record for one discovery run."""

    __tablename__ = "discovery_run_abandonments"
    __table_args__ = (
        UniqueConstraint(
            "discovery_run_id",
            name="uq_discovery_run_abandonment",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id"],
            ["discovery_runs.project_id", "discovery_runs.discovery_run_id"],
            name="fk_discovery_run_abandonment_run",
        ),
    )

    discovery_run_abandonment_id: int | None = Field(
        default=None,
        primary_key=True,
    )
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    reason: str = Field(sa_type=Text)
    abandoned_by: str = Field(index=True)
    abandoned_at: datetime = Field(default_factory=utc_now, nullable=False)


class ChallengeArtifact(SQLModel, table=True):
    """Versioned challenge artifact produced during discovery."""

    __tablename__ = "challenge_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "challenge_artifact_id",
            name="uq_challenge_project_run_id",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "version_number",
            name="uq_challenge_version",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "content_fingerprint",
            name="uq_challenge_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id"],
            ["discovery_runs.project_id", "discovery_runs.discovery_run_id"],
            name="fk_challenge_discovery_run",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "discovery_run_id",
                "supersedes_challenge_artifact_id",
            ],
            [
                "challenge_artifacts.project_id",
                "challenge_artifacts.discovery_run_id",
                "challenge_artifacts.challenge_artifact_id",
            ],
            name="fk_challenge_supersedes",
        ),
    )

    challenge_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_challenge_artifact_id: int | None = Field(default=None)
    provenance_path: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class PrdVersion(SQLModel, table=True):
    """Versioned PRD artifact produced during discovery."""

    __tablename__ = "prd_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "prd_version_id",
            name="uq_prd_project_run_id",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "prd_version_id",
            "content_fingerprint",
            name="uq_prd_review_parent",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "version_number",
            name="uq_prd_version",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "content_fingerprint",
            name="uq_prd_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id"],
            ["discovery_runs.project_id", "discovery_runs.discovery_run_id"],
            name="fk_prd_discovery_run",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id", "supersedes_prd_version_id"],
            [
                "prd_versions.project_id",
                "prd_versions.discovery_run_id",
                "prd_versions.prd_version_id",
            ],
            name="fk_prd_supersedes",
        ),
    )

    prd_version_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_prd_version_id: int | None = Field(default=None)
    provenance_path: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class PrdDecision(SQLModel, table=True):
    """Review decision for one immutable PRD version."""

    __tablename__ = "prd_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "prd_version_id",
            name="uq_prd_decision_per_version",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id"],
            ["discovery_runs.project_id", "discovery_runs.discovery_run_id"],
            name="fk_prd_decision_discovery_run",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "discovery_run_id",
                "prd_version_id",
                "artifact_fingerprint",
            ],
            [
                "prd_versions.project_id",
                "prd_versions.discovery_run_id",
                "prd_versions.prd_version_id",
                "prd_versions.content_fingerprint",
            ],
            name="fk_prd_decision_version",
        ),
    )

    prd_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    prd_version_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    reviewer: str = Field(index=True)
    notes: str = Field(sa_type=Text)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecDraft(SQLModel, table=True):
    """Versioned initial or amendment specification draft."""

    __tablename__ = "spec_drafts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "spec_draft_id",
            name="uq_spec_draft_project_run_id",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "spec_draft_id",
            "content_fingerprint",
            name="uq_spec_draft_review_parent",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "version_number",
            name="uq_spec_draft_version",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_run_id",
            "content_fingerprint",
            name="uq_spec_draft_fingerprint",
        ),
        CheckConstraint(
            "(kind = 'initial' AND base_spec_version_id IS NULL "
            "AND base_spec_hash IS NULL) OR "
            "(kind = 'amendment' AND base_spec_version_id IS NOT NULL "
            "AND base_spec_hash IS NOT NULL)",
            name="ck_spec_draft_base",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id"],
            ["discovery_runs.project_id", "discovery_runs.discovery_run_id"],
            name="fk_spec_draft_discovery_run",
        ),
        ForeignKeyConstraint(
            ["project_id", "base_spec_version_id", "base_spec_hash"],
            [
                "spec_registry.product_id",
                "spec_registry.spec_version_id",
                "spec_registry.spec_hash",
            ],
            name="fk_spec_draft_base_spec",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id", "supersedes_spec_draft_id"],
            [
                "spec_drafts.project_id",
                "spec_drafts.discovery_run_id",
                "spec_drafts.spec_draft_id",
            ],
            name="fk_spec_draft_supersedes",
        ),
    )

    spec_draft_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    kind: str = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    base_spec_version_id: int | None = Field(default=None, index=True)
    base_spec_hash: str | None = Field(default=None, index=True)
    supersedes_spec_draft_id: int | None = Field(default=None)
    provenance_path: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecDraftDecision(SQLModel, table=True):
    """Review decision for one immutable specification draft."""

    __tablename__ = "spec_draft_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "spec_draft_id",
            name="uq_spec_draft_decision_per_version",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id"],
            ["discovery_runs.project_id", "discovery_runs.discovery_run_id"],
            name="fk_spec_draft_decision_discovery_run",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "discovery_run_id",
                "spec_draft_id",
                "artifact_fingerprint",
            ],
            [
                "spec_drafts.project_id",
                "spec_drafts.discovery_run_id",
                "spec_drafts.spec_draft_id",
                "spec_drafts.content_fingerprint",
            ],
            name="fk_spec_draft_decision_version",
        ),
    )

    spec_draft_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    spec_draft_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    reviewer: str = Field(index=True)
    notes: str = Field(sa_type=Text)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class InitialScopeRegistration(SQLModel, table=True):
    """One accepted initial scope identity for a Project."""

    __tablename__ = "initial_scope_registrations"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_initial_registration_project"),
        UniqueConstraint(
            "discovery_run_id",
            name="uq_initial_registration_discovery_run",
        ),
        UniqueConstraint(
            "spec_draft_id",
            name="uq_initial_registration_spec_draft",
        ),
        UniqueConstraint(
            "spec_version_id",
            name="uq_initial_registration_spec_version",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id"],
            ["discovery_runs.project_id", "discovery_runs.discovery_run_id"],
            name="fk_initial_registration_discovery_run",
        ),
        ForeignKeyConstraint(
            ["project_id", "discovery_run_id", "spec_draft_id"],
            [
                "spec_drafts.project_id",
                "spec_drafts.discovery_run_id",
                "spec_drafts.spec_draft_id",
            ],
            name="fk_initial_registration_spec_draft",
        ),
        ForeignKeyConstraint(
            ["project_id", "spec_version_id", "spec_hash"],
            [
                "spec_registry.product_id",
                "spec_registry.spec_version_id",
                "spec_registry.spec_hash",
            ],
            name="fk_initial_registration_spec",
        ),
    )

    initial_scope_registration_id: int | None = Field(
        default=None,
        primary_key=True,
    )
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    spec_draft_id: int = Field(index=True)
    spec_version_id: int = Field(index=True)
    spec_hash: str = Field(index=True)
    registered_by: str = Field(index=True)
    registered_at: datetime = Field(default_factory=utc_now, nullable=False)


class RepositoryBaseline(SQLModel, table=True):
    """Versioned repository identity captured for brownfield onboarding."""

    __tablename__ = "repository_baselines"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "repository_baseline_id",
            name="uq_repository_baseline_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_repository_baseline_version",
        ),
        UniqueConstraint(
            "project_id",
            "content_fingerprint",
            name="uq_repository_baseline_fingerprint",
        ),
    )

    repository_baseline_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    repository_path: str = Field(sa_type=Text)
    git_commit: str | None = Field(default=None, index=True)
    dirty: bool
    content_fingerprint: str = Field(index=True)
    version_number: int
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class RepositoryInventory(SQLModel, table=True):
    """Versioned inventory derived from one same-Project baseline."""

    __tablename__ = "repository_inventories"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_repository_inventory_version",
        ),
        UniqueConstraint(
            "project_id",
            "content_fingerprint",
            name="uq_repository_inventory_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "repository_baseline_id"],
            [
                "repository_baselines.project_id",
                "repository_baselines.repository_baseline_id",
            ],
            name="fk_repository_inventory_baseline",
        ),
    )

    repository_inventory_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    repository_baseline_id: int = Field(index=True)
    canonical_inventory_json: str = Field(sa_type=Text)
    selected_for_model_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    version_number: int
    file_count: int
    total_bytes: int
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class VisionArtifact(SQLModel, table=True):
    """Immutable Vision artifact bound to one accepted authority."""

    __tablename__ = "vision_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vision_artifact_id",
            name="uq_vision_artifact_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "vision_artifact_id",
            "content_fingerprint",
            name="uq_vision_artifact_review_parent",
        ),
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_vision_artifact_version",
        ),
        UniqueConstraint(
            "project_id",
            "content_fingerprint",
            name="uq_vision_artifact_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_vision_artifact_id"],
            ["vision_artifacts.project_id", "vision_artifacts.vision_artifact_id"],
            name="fk_vision_artifact_supersedes",
        ),
    )

    vision_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    authority_id: int = Field(
        foreign_key="compiled_spec_authority.authority_id",
        index=True,
    )
    authority_fingerprint: str = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_vision_artifact_id: int | None = Field(default=None, index=True)
    created_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class VisionArtifactDecision(SQLModel, table=True):
    """Append-only review decision for one immutable Vision artifact."""

    __tablename__ = "vision_artifact_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vision_artifact_id",
            name="uq_vision_artifact_decision",
        ),
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'feedback')",
            name="ck_vision_artifact_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_artifact_id", "artifact_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_vision_artifact_decision_parent",
        ),
    )

    vision_artifact_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    vision_artifact_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class BacklogArtifact(SQLModel, table=True):
    """Immutable Backlog artifact bound to one accepted authority."""

    __tablename__ = "backlog_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "backlog_artifact_id",
            name="uq_backlog_artifact_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "backlog_artifact_id",
            "content_fingerprint",
            name="uq_backlog_artifact_review_parent",
        ),
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_backlog_artifact_version",
        ),
        UniqueConstraint(
            "project_id",
            "content_fingerprint",
            name="uq_backlog_artifact_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_backlog_artifact_id"],
            [
                "backlog_artifacts.project_id",
                "backlog_artifacts.backlog_artifact_id",
            ],
            name="fk_backlog_artifact_supersedes",
        ),
    )

    backlog_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    authority_id: int = Field(
        foreign_key="compiled_spec_authority.authority_id",
        index=True,
    )
    authority_fingerprint: str = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_backlog_artifact_id: int | None = Field(default=None, index=True)
    created_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class BacklogArtifactDecision(SQLModel, table=True):
    """Append-only review decision for one immutable Backlog artifact."""

    __tablename__ = "backlog_artifact_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "backlog_artifact_id",
            name="uq_backlog_artifact_decision",
        ),
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'feedback')",
            name="ck_backlog_artifact_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "backlog_artifact_id", "artifact_fingerprint"],
            [
                "backlog_artifacts.project_id",
                "backlog_artifacts.backlog_artifact_id",
                "backlog_artifacts.content_fingerprint",
            ],
            name="fk_backlog_artifact_decision_parent",
        ),
    )

    backlog_artifact_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    backlog_artifact_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class BacklogAuthorityReconciliation(SQLModel, table=True):
    """Explicit audit record for stale artifacts under replacement authority."""

    __tablename__ = "backlog_authority_reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "replacement_authority_id",
            "replacement_authority_fingerprint",
            name="uq_backlog_authority_reconciliation",
        ),
    )

    backlog_authority_reconciliation_id: int | None = Field(
        default=None,
        primary_key=True,
    )
    project_id: int = Field(foreign_key="products.product_id", index=True)
    replacement_authority_id: int = Field(
        foreign_key="compiled_spec_authority.authority_id",
        index=True,
    )
    replacement_authority_fingerprint: str = Field(index=True)
    affected_artifact_ids_json: str = Field(sa_type=Text)
    affected_artifacts_fingerprint: str = Field(index=True)
    reconciled_by: str = Field(index=True)
    reconciled_at: datetime = Field(default_factory=utc_now, nullable=False)


class WorkflowNodeAttempt(SQLModel, table=True):
    """Durable lease and input identity for one agentic node execution."""

    __tablename__ = "workflow_node_attempts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "workflow_node_attempt_id",
            name="uq_workflow_attempt_project_id",
        ),
        CheckConstraint(
            "lease_expires_at > started_at",
            name="ck_workflow_attempt_lease",
        ),
    )

    workflow_node_attempt_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    node_id: str = Field(index=True)
    instance_key: str | None = Field(default=None, index=True)
    graph_version: str
    fact_fingerprint: str
    business_fact_fingerprint: str
    decision_fingerprint: str
    normalized_input_json: str = Field(sa_type=Text)
    input_fingerprint: str
    model_id: str
    execution_settings_json: str = Field(sa_type=Text)
    idempotency_key: str = Field(index=True)
    actor: str
    correlation_id: str | None = Field(default=None)
    started_at: datetime
    lease_expires_at: datetime
    attempt_fingerprint: str = Field(index=True)


class WorkflowNodeAttemptOutcome(SQLModel, table=True):
    """Single terminal outcome for one same-Project node attempt."""

    __tablename__ = "workflow_node_attempt_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "workflow_node_attempt_id",
            name="uq_workflow_attempt_outcome",
        ),
        CheckConstraint(
            "status IN ('success', 'failure', 'obsolete')",
            name="ck_workflow_attempt_outcome_status",
        ),
        CheckConstraint(
            "(status = 'success' AND output_fingerprint IS NOT NULL "
            "AND output_json IS NOT NULL AND failure_code IS NULL "
            "AND failure_message IS NULL) OR "
            "(status = 'failure' AND output_fingerprint IS NULL "
            "AND output_json IS NULL AND failure_code IS NOT NULL "
            "AND failure_message IS NOT NULL) OR "
            "(status = 'obsolete' AND output_fingerprint IS NULL "
            "AND output_json IS NULL AND failure_code IS NULL "
            "AND failure_message IS NULL)",
            name="ck_workflow_attempt_outcome_shape",
        ),
        ForeignKeyConstraint(
            ["project_id", "workflow_node_attempt_id"],
            [
                "workflow_node_attempts.project_id",
                "workflow_node_attempts.workflow_node_attempt_id",
            ],
            name="fk_workflow_attempt_outcome_attempt",
        ),
    )

    workflow_node_attempt_outcome_id: int | None = Field(
        default=None,
        primary_key=True,
    )
    project_id: int = Field(index=True)
    workflow_node_attempt_id: int = Field(index=True)
    status: str = Field(index=True)
    output_fingerprint: str | None = Field(default=None)
    output_json: str | None = Field(default=None, sa_type=Text)
    failure_code: str | None = Field(default=None)
    failure_message: str | None = Field(default=None, sa_type=Text)
    recorded_at: datetime


class WorkflowTransitionReceipt(SQLModel, table=True):
    """Idempotency persistence for transitions, separate from graph facts."""

    __tablename__ = "workflow_transition_receipts"
    __table_args__ = (
        UniqueConstraint(
            "request_kind",
            "idempotency_key",
            name="uq_workflow_transition_receipt",
        ),
    )

    workflow_transition_receipt_id: int | None = Field(
        default=None,
        primary_key=True,
    )
    request_kind: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    request_fingerprint: str
    request_json: str = Field(sa_type=Text)
    result_json: str | None = Field(default=None, sa_type=Text)
    started_at: datetime
    completed_at: datetime | None = Field(default=None)
