"""Append-only durable records for product-definition workflow evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class VisionRevisionIntent(SQLModel, table=True):
    """One requested revision of an immutable staged Vision artifact."""

    __tablename__ = "vision_revision_intents"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vision_revision_intent_id",
            name="uq_vision_revision_intent_project_id",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_vision_artifact_id", "source_vision_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_vision_revision_intent_source_vision",
            use_alter=True,
        ),
    )

    vision_revision_intent_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    source_vision_artifact_id: int = Field(index=True)
    source_vision_fingerprint: str = Field(index=True)
    reason: str = Field(sa_type=Text)
    initiated_by: str = Field(index=True)
    initiated_at: datetime = Field(default_factory=utc_now, nullable=False)


class VisionEvidenceSnapshot(SQLModel, table=True):
    """Immutable collector evidence used by one successful Vision generation."""

    __tablename__ = "vision_evidence_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vision_evidence_snapshot_id",
            name="uq_vision_evidence_snapshot_project_id",
        ),
        ForeignKeyConstraint(
            ["project_id", "workflow_node_attempt_id"],
            [
                "workflow_node_attempts.project_id",
                "workflow_node_attempts.workflow_node_attempt_id",
            ],
            name="fk_vision_evidence_snapshot_attempt",
        ),
        ForeignKeyConstraint(
            ["project_id", "repository_binding_id"],
            [
                "repository_bindings.project_id",
                "repository_bindings.repository_binding_id",
            ],
            name="fk_vision_evidence_snapshot_repository_binding",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_vision_evidence_snapshot_id"],
            [
                "vision_evidence_snapshots.project_id",
                "vision_evidence_snapshots.vision_evidence_snapshot_id",
            ],
            name="fk_vision_evidence_snapshot_supersedes",
        ),
    )

    vision_evidence_snapshot_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    repository_binding_id: int | None = Field(default=None, index=True)
    supersedes_vision_evidence_snapshot_id: int | None = Field(
        default=None,
        index=True,
    )
    workflow_node_attempt_id: int = Field(index=True)
    evidence_json: str = Field(sa_type=Text)
    evidence_fingerprint: str = Field(index=True)
    warnings_json: str = Field(sa_type=Text)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class VisionInterviewTurn(SQLModel, table=True):
    """One immutable generation or clarification turn for Project Vision work."""

    __tablename__ = "vision_interview_turns"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vision_interview_turn_id",
            name="uq_vision_interview_turn_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "vision_evidence_snapshot_id",
            "turn_number",
            name="uq_vision_interview_snapshot_turn_number",
        ),
        CheckConstraint(
            "operation IN ('bootstrap', 'clarification', 'revision')",
            name="ck_vision_interview_turn_operation",
        ),
        CheckConstraint(
            "((operation = 'bootstrap' AND user_text IS NULL) "
            "OR (operation IN ('clarification', 'revision') "
            "AND user_text IS NOT NULL))",
            name="ck_vision_interview_turn_user_text_operation",
        ),
        ForeignKeyConstraint(
            ["project_id", "revision_intent_id"],
            [
                "vision_revision_intents.project_id",
                "vision_revision_intents.vision_revision_intent_id",
            ],
            name="fk_vision_interview_turn_revision_intent",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_evidence_snapshot_id"],
            [
                "vision_evidence_snapshots.project_id",
                "vision_evidence_snapshots.vision_evidence_snapshot_id",
            ],
            name="fk_vision_interview_turn_evidence_snapshot",
        ),
        ForeignKeyConstraint(
            ["project_id", "prior_turn_id"],
            [
                "vision_interview_turns.project_id",
                "vision_interview_turns.vision_interview_turn_id",
            ],
            name="fk_vision_interview_turn_prior_turn",
        ),
        ForeignKeyConstraint(
            ["project_id", "workflow_node_attempt_id"],
            [
                "workflow_node_attempts.project_id",
                "workflow_node_attempts.workflow_node_attempt_id",
            ],
            name="fk_vision_interview_turn_attempt",
        ),
    )

    vision_interview_turn_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    operation: str = Field(index=True)
    turn_number: int
    revision_intent_id: int | None = Field(default=None, index=True)
    vision_evidence_snapshot_id: int = Field(index=True)
    prior_turn_id: int | None = Field(default=None, index=True)
    user_text: str | None = Field(default=None, sa_type=Text)
    components_json: str = Field(sa_type=Text)
    vision_statement: str = Field(sa_type=Text)
    is_complete: bool
    clarifying_questions_json: str = Field(sa_type=Text)
    component_basis_json: str = Field(sa_type=Text)
    assumptions_json: str = Field(sa_type=Text)
    conflicts_json: str = Field(sa_type=Text)
    output_fingerprint: str = Field(index=True)
    workflow_node_attempt_id: int = Field(index=True)
    attempt_fingerprint: str = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class VisionArtifact(SQLModel, table=True):
    """One immutable Project Vision assembled from a complete interview turn."""

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
            name="uq_vision_artifact_decision_parent",
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
        ForeignKeyConstraint(
            ["project_id", "source_interview_turn_id"],
            [
                "vision_interview_turns.project_id",
                "vision_interview_turns.vision_interview_turn_id",
            ],
            name="fk_vision_artifact_source_turn",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_evidence_snapshot_id"],
            [
                "vision_evidence_snapshots.project_id",
                "vision_evidence_snapshots.vision_evidence_snapshot_id",
            ],
            name="fk_vision_artifact_evidence_snapshot",
        ),
    )

    vision_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    version_number: int
    components_json: str = Field(sa_type=Text)
    statement: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    vision_evidence_snapshot_id: int = Field(index=True)
    component_basis_json: str = Field(sa_type=Text)
    assumptions_json: str = Field(sa_type=Text)
    conflicts_json: str = Field(sa_type=Text)
    supersedes_vision_artifact_id: int | None = Field(default=None, index=True)
    source_interview_turn_id: int = Field(index=True)
    created_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class VisionArtifactDecision(SQLModel, table=True):
    """One immutable operator decision for one precise Vision artifact."""

    __tablename__ = "vision_artifact_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vision_artifact_id",
            name="uq_vision_artifact_decision",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_vision_artifact_decision_idempotency",
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


class ProductGoalInterviewTurn(SQLModel, table=True):
    """One immutable interview turn for one numbered Product Goal revision."""

    __tablename__ = "product_goal_interview_turns"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "product_goal_interview_turn_id",
            name="uq_product_goal_interview_turn_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "goal_number",
            "revision_number",
            "product_goal_interview_turn_id",
            name="uq_product_goal_interview_turn_identity",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_artifact_id", "vision_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_product_goal_interview_turn_vision",
        ),
        ForeignKeyConstraint(
            ["project_id", "prior_turn_id"],
            [
                "product_goal_interview_turns.project_id",
                "product_goal_interview_turns.product_goal_interview_turn_id",
            ],
            name="fk_product_goal_interview_turn_prior_turn",
        ),
        ForeignKeyConstraint(
            ["project_id", "workflow_node_attempt_id"],
            [
                "workflow_node_attempts.project_id",
                "workflow_node_attempts.workflow_node_attempt_id",
            ],
            name="fk_product_goal_interview_turn_attempt",
        ),
    )

    product_goal_interview_turn_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    vision_artifact_id: int = Field(index=True)
    vision_fingerprint: str = Field(index=True)
    goal_number: int
    revision_number: int
    prior_turn_id: int | None = Field(default=None, index=True)
    user_text: str = Field(sa_type=Text)
    components_json: str = Field(sa_type=Text)
    goal_statement: str = Field(sa_type=Text)
    is_complete: bool
    clarifying_questions_json: str = Field(sa_type=Text)
    output_fingerprint: str = Field(index=True)
    workflow_node_attempt_id: int = Field(index=True)
    attempt_fingerprint: str = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class ProductGoalArtifact(SQLModel, table=True):
    """Immutable product-goal version anchored to one staged Vision artifact."""

    __tablename__ = "product_goal_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "product_goal_artifact_id",
            name="uq_product_goal_artifact_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "product_goal_artifact_id",
            "content_fingerprint",
            name="uq_product_goal_artifact_parent",
        ),
        UniqueConstraint(
            "project_id",
            "goal_number",
            "revision_number",
            name="uq_product_goal_artifact_version",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_artifact_id", "vision_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_product_goal_artifact_vision",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_product_goal_artifact_id"],
            [
                "product_goal_artifacts.project_id",
                "product_goal_artifacts.product_goal_artifact_id",
            ],
            name="fk_product_goal_artifact_supersedes",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_interview_turn_id"],
            [
                "product_goal_interview_turns.project_id",
                "product_goal_interview_turns.product_goal_interview_turn_id",
            ],
            name="fk_product_goal_artifact_source_turn",
        ),
    )

    product_goal_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    vision_artifact_id: int = Field(index=True)
    vision_fingerprint: str = Field(index=True)
    goal_number: int
    revision_number: int
    statement: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_product_goal_artifact_id: int | None = Field(default=None, index=True)
    source_interview_turn_id: int = Field(index=True)
    created_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class ProductGoalArtifactDecision(SQLModel, table=True):
    """Append-only review state recorded against one product-goal artifact."""

    __tablename__ = "product_goal_artifact_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'feedback')",
            name="ck_product_goal_artifact_decision",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_product_goal_artifact_decision_idempotency",
        ),
        ForeignKeyConstraint(
            ["project_id", "product_goal_artifact_id", "artifact_fingerprint"],
            [
                "product_goal_artifacts.project_id",
                "product_goal_artifacts.product_goal_artifact_id",
                "product_goal_artifacts.content_fingerprint",
            ],
            name="fk_product_goal_artifact_decision_parent",
        ),
    )

    product_goal_artifact_decision_id: int | None = Field(
        default=None,
        primary_key=True,
    )
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    product_goal_artifact_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class ProductGoalOutcome(SQLModel, table=True):
    """One terminal fulfillment or abandonment outcome for an accepted Goal."""

    __tablename__ = "product_goal_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('fulfilled', 'abandoned')",
            name="ck_product_goal_outcome",
        ),
        UniqueConstraint(
            "project_id",
            "product_goal_artifact_id",
            name="uq_product_goal_outcome_artifact",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_product_goal_outcome_idempotency",
        ),
        ForeignKeyConstraint(
            ["project_id", "product_goal_artifact_id", "artifact_fingerprint"],
            [
                "product_goal_artifacts.project_id",
                "product_goal_artifacts.product_goal_artifact_id",
                "product_goal_artifacts.content_fingerprint",
            ],
            name="fk_product_goal_outcome_parent",
        ),
    )

    product_goal_outcome_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    product_goal_artifact_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    outcome: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    decided_by: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecificationSource(SQLModel, table=True):
    """Immutable registered external to-spec source and exact lineage."""

    __tablename__ = "specification_sources"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "specification_source_id",
            "source_fingerprint",
            name="uq_specification_source_identity",
        ),
        UniqueConstraint(
            "project_id",
            "supersedes_specification_source_id",
            name="uq_specification_source_successor",
        ),
        CheckConstraint(
            "(supersedes_specification_source_id IS NULL "
            "AND supersedes_source_fingerprint IS NULL) OR "
            "(supersedes_specification_source_id IS NOT NULL "
            "AND supersedes_source_fingerprint IS NOT NULL)",
            name="ck_specification_source_supersedes",
        ),
        ForeignKeyConstraint(
            ["project_id", "repository_binding_id"],
            [
                "repository_bindings.project_id",
                "repository_bindings.repository_binding_id",
            ],
            name="fk_specification_source_repository_binding",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_artifact_id", "vision_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_specification_source_vision",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "product_goal_artifact_id",
                "product_goal_fingerprint",
            ],
            [
                "product_goal_artifacts.project_id",
                "product_goal_artifacts.product_goal_artifact_id",
                "product_goal_artifacts.content_fingerprint",
            ],
            name="fk_specification_source_goal",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "supersedes_specification_source_id",
                "supersedes_source_fingerprint",
            ],
            [
                "specification_sources.project_id",
                "specification_sources.specification_source_id",
                "specification_sources.source_fingerprint",
            ],
            name="fk_specification_source_supersedes",
        ),
    )

    specification_source_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    source_bundle_json: str = Field(sa_type=Text)
    source_fingerprint: str = Field(index=True)
    repository_binding_id: int = Field(index=True)
    repository_head_sha: str = Field(index=True, min_length=40, max_length=40)
    repository_dirty: bool
    repository_status_fingerprint: str = Field(index=True)
    vision_artifact_id: int = Field(index=True)
    vision_fingerprint: str = Field(index=True)
    product_goal_artifact_id: int = Field(index=True)
    product_goal_fingerprint: str = Field(index=True)
    supersedes_specification_source_id: int | None = Field(default=None, index=True)
    supersedes_source_fingerprint: str | None = Field(default=None, index=True)
    registered_by: str = Field(index=True)
    registered_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecificationCandidate(SQLModel, table=True):
    """Immutable candidate specification with complete product lineage."""

    __tablename__ = "specification_candidates"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "specification_candidate_id",
            "candidate_fingerprint",
            name="uq_specification_candidate_identity",
        ),
        UniqueConstraint(
            "project_id",
            "specification_candidate_id",
            "candidate_fingerprint",
            "payload_fingerprint",
            name="uq_specification_candidate_payload_identity",
        ),
        UniqueConstraint(
            "project_id",
            "workflow_node_attempt_id",
            name="uq_specification_candidate_attempt",
        ),
        UniqueConstraint(
            "project_id",
            "supersedes_specification_candidate_id",
            name="uq_specification_candidate_successor",
        ),
        CheckConstraint(
            "candidate_kind IN ('initial', 'amendment')",
            name="ck_specification_candidate_kind",
        ),
        CheckConstraint(
            "(candidate_kind = 'initial' AND base_spec_version_id IS NULL "
            "AND base_spec_hash IS NULL) OR (candidate_kind = 'amendment' "
            "AND base_spec_version_id IS NOT NULL AND base_spec_hash IS NOT NULL)",
            name="ck_specification_candidate_base_spec",
        ),
        CheckConstraint(
            "(supersedes_specification_candidate_id IS NULL "
            "AND supersedes_candidate_fingerprint IS NULL) OR "
            "(supersedes_specification_candidate_id IS NOT NULL "
            "AND supersedes_candidate_fingerprint IS NOT NULL)",
            name="ck_specification_candidate_supersedes",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "specification_source_id",
                "specification_source_fingerprint",
            ],
            [
                "specification_sources.project_id",
                "specification_sources.specification_source_id",
                "specification_sources.source_fingerprint",
            ],
            name="fk_specification_candidate_source",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_artifact_id", "vision_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_specification_candidate_vision",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "product_goal_artifact_id",
                "product_goal_fingerprint",
            ],
            [
                "product_goal_artifacts.project_id",
                "product_goal_artifacts.product_goal_artifact_id",
                "product_goal_artifacts.content_fingerprint",
            ],
            name="fk_specification_candidate_goal",
        ),
        ForeignKeyConstraint(
            ["project_id", "base_spec_version_id", "base_spec_hash"],
            [
                "spec_registry.project_id",
                "spec_registry.spec_version_id",
                "spec_registry.spec_hash",
            ],
            name="fk_specification_candidate_base_spec",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["project_id", "workflow_node_attempt_id", "attempt_fingerprint"],
            [
                "workflow_node_attempts.project_id",
                "workflow_node_attempts.workflow_node_attempt_id",
                "workflow_node_attempts.attempt_fingerprint",
            ],
            name="fk_specification_candidate_attempt",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "supersedes_specification_candidate_id",
                "supersedes_candidate_fingerprint",
            ],
            [
                "specification_candidates.project_id",
                "specification_candidates.specification_candidate_id",
                "specification_candidates.candidate_fingerprint",
            ],
            name="fk_specification_candidate_supersedes",
        ),
    )

    specification_candidate_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    candidate_kind: str = Field(index=True)
    specification_source_id: int = Field(index=True)
    specification_source_fingerprint: str = Field(index=True)
    vision_artifact_id: int = Field(index=True)
    vision_fingerprint: str = Field(index=True)
    product_goal_artifact_id: int = Field(index=True)
    product_goal_fingerprint: str = Field(index=True)
    base_spec_version_id: int | None = Field(default=None, index=True)
    base_spec_hash: str | None = Field(default=None, index=True)
    canonical_envelope_json: str = Field(sa_type=Text)
    payload_fingerprint: str = Field(index=True)
    source_manifest_fingerprint: str = Field(index=True)
    producer_input_fingerprint: str = Field(index=True)
    rendered_view_fingerprint: str = Field(index=True)
    candidate_fingerprint: str = Field(index=True)
    workflow_node_attempt_id: int = Field(index=True)
    attempt_fingerprint: str = Field(index=True)
    supersedes_specification_candidate_id: int | None = Field(default=None, index=True)
    supersedes_candidate_fingerprint: str | None = Field(default=None, index=True)
    recorded_by: str = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecificationDecision(SQLModel, table=True):
    """Append-only acceptance or rejection of one candidate specification."""

    __tablename__ = "specification_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'feedback')",
            name="ck_specification_decision",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_specification_decision_idempotency",
        ),
        UniqueConstraint(
            "project_id",
            "specification_candidate_id",
            name="uq_specification_decision_candidate",
        ),
        ForeignKeyConstraint(
            ["project_id", "specification_candidate_id", "candidate_fingerprint"],
            [
                "specification_candidates.project_id",
                "specification_candidates.specification_candidate_id",
                "specification_candidates.candidate_fingerprint",
            ],
            name="fk_specification_decision_parent",
        ),
    )

    specification_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    specification_candidate_id: int = Field(index=True)
    candidate_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)
