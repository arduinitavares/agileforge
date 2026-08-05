"""Append-only durable records for product-definition workflow evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Index, text
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
        ),
    )

    vision_revision_intent_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    source_vision_artifact_id: int = Field(index=True)
    source_vision_fingerprint: str = Field(index=True)
    reason: str = Field(sa_type=Text)
    initiated_by: str = Field(index=True)
    initiated_at: datetime = Field(default_factory=utc_now, nullable=False)


class VisionInterviewTurn(SQLModel, table=True):
    """One immutable user/agent interview turn for initial or revision Vision work."""

    __tablename__ = "vision_interview_turns"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vision_interview_turn_id",
            name="uq_vision_interview_turn_project_id",
        ),
        Index(
            "uq_vision_interview_initial_turn_number",
            "project_id",
            "turn_number",
            unique=True,
            sqlite_where=text("mode = 'initial'"),
        ),
        Index(
            "uq_vision_interview_revision_turn_number",
            "project_id",
            "revision_intent_id",
            "turn_number",
            unique=True,
            sqlite_where=text("mode = 'revision'"),
        ),
        CheckConstraint(
            "mode IN ('initial', 'revision')",
            name="ck_vision_interview_turn_mode",
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
    mode: str = Field(index=True)
    turn_number: int
    revision_intent_id: int | None = Field(default=None, index=True)
    prior_turn_id: int | None = Field(default=None, index=True)
    user_text: str = Field(sa_type=Text)
    components_json: str = Field(sa_type=Text)
    vision_statement: str = Field(sa_type=Text)
    is_complete: bool
    clarifying_questions_json: str = Field(sa_type=Text)
    output_fingerprint: str = Field(index=True)
    workflow_node_attempt_id: int = Field(index=True)
    attempt_fingerprint: str = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


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


class DiscoveryArtifact(SQLModel, table=True):
    """Immutable discovery output with durable Vision and goal lineage."""

    __tablename__ = "discovery_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "discovery_artifact_id",
            name="uq_discovery_artifact_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "discovery_artifact_id",
            "content_fingerprint",
            name="uq_discovery_artifact_parent",
        ),
        ForeignKeyConstraint(
            ["project_id", "vision_artifact_id", "vision_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_discovery_artifact_vision",
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
            name="fk_discovery_artifact_goal",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_discovery_artifact_id"],
            [
                "discovery_artifacts.project_id",
                "discovery_artifacts.discovery_artifact_id",
            ],
            name="fk_discovery_artifact_supersedes",
        ),
    )

    discovery_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    vision_artifact_id: int = Field(index=True)
    vision_fingerprint: str = Field(index=True)
    product_goal_artifact_id: int = Field(index=True)
    product_goal_fingerprint: str = Field(index=True)
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    content_ref: str | None = Field(default=None, sa_type=Text)
    producer: str = Field(index=True)
    supersedes_discovery_artifact_id: int | None = Field(default=None, index=True)
    recorded_by: str = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecificationCandidate(SQLModel, table=True):
    """Immutable candidate specification with complete product lineage."""

    __tablename__ = "specification_candidates"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "specification_candidate_id",
            name="uq_specification_candidate_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "specification_candidate_id",
            "content_fingerprint",
            name="uq_specification_candidate_parent",
        ),
        CheckConstraint(
            "(base_spec_version_id IS NULL AND base_spec_hash IS NULL) OR "
            "(base_spec_version_id IS NOT NULL AND base_spec_hash IS NOT NULL)",
            name="ck_specification_candidate_base_spec",
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
            ["project_id", "discovery_artifact_id", "discovery_fingerprint"],
            [
                "discovery_artifacts.project_id",
                "discovery_artifacts.discovery_artifact_id",
                "discovery_artifacts.content_fingerprint",
            ],
            name="fk_specification_candidate_discovery",
        ),
        ForeignKeyConstraint(
            ["project_id", "base_spec_version_id", "base_spec_hash"],
            [
                "spec_registry.project_id",
                "spec_registry.spec_version_id",
                "spec_registry.spec_hash",
            ],
            name="fk_specification_candidate_base_spec",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_specification_candidate_id"],
            [
                "specification_candidates.project_id",
                "specification_candidates.specification_candidate_id",
            ],
            name="fk_specification_candidate_supersedes",
        ),
    )

    specification_candidate_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    vision_artifact_id: int = Field(index=True)
    vision_fingerprint: str = Field(index=True)
    product_goal_artifact_id: int = Field(index=True)
    product_goal_fingerprint: str = Field(index=True)
    discovery_artifact_id: int = Field(index=True)
    discovery_fingerprint: str = Field(index=True)
    base_spec_version_id: int | None = Field(default=None, index=True)
    base_spec_hash: str | None = Field(default=None, index=True)
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    content_ref: str | None = Field(default=None, sa_type=Text)
    supersedes_specification_candidate_id: int | None = Field(default=None, index=True)
    recorded_by: str = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecificationDecision(SQLModel, table=True):
    """Append-only acceptance or rejection of one candidate specification."""

    __tablename__ = "specification_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('accepted', 'rejected')",
            name="ck_specification_decision",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_specification_decision_idempotency",
        ),
        ForeignKeyConstraint(
            ["project_id", "specification_candidate_id", "artifact_fingerprint"],
            [
                "specification_candidates.project_id",
                "specification_candidates.specification_candidate_id",
                "specification_candidates.content_fingerprint",
            ],
            name="fk_specification_decision_parent",
        ),
    )

    specification_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    specification_candidate_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)
