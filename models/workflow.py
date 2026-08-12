"""Durable SQLModel records for the domain workflow graph."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel

from models.product_definition import VisionArtifact, VisionArtifactDecision

__all__ = ["VisionArtifact", "VisionArtifactDecision"]


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class BacklogArtifact(SQLModel, table=True):
    """Immutable Backlog artifact bound to one Goal and accepted authority."""

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
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    authority_id: int = Field(
        foreign_key="compiled_spec_authority.authority_id",
        index=True,
    )
    authority_fingerprint: str = Field(index=True)
    product_goal_artifact_id: int = Field(index=True)
    product_goal_fingerprint: str = Field(index=True)
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


class RoadmapArtifact(SQLModel, table=True):
    """Immutable Roadmap artifact bound to one accepted Backlog artifact."""

    __tablename__ = "roadmap_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "roadmap_artifact_id", name="uq_roadmap_project"
        ),
        UniqueConstraint(
            "project_id",
            "roadmap_artifact_id",
            "content_fingerprint",
            name="uq_roadmap_review_parent",
        ),
        UniqueConstraint("project_id", "version_number", name="uq_roadmap_version"),
        UniqueConstraint(
            "project_id",
            "content_fingerprint",
            name="uq_roadmap_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "backlog_artifact_id", "backlog_artifact_fingerprint"],
            [
                "backlog_artifacts.project_id",
                "backlog_artifacts.backlog_artifact_id",
                "backlog_artifacts.content_fingerprint",
            ],
            name="fk_roadmap_backlog",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_roadmap_artifact_id"],
            ["roadmap_artifacts.project_id", "roadmap_artifacts.roadmap_artifact_id"],
            name="fk_roadmap_supersedes",
        ),
    )

    roadmap_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    backlog_artifact_id: int = Field(index=True)
    backlog_artifact_fingerprint: str = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_roadmap_artifact_id: int | None = Field(default=None, index=True)
    created_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class RoadmapArtifactDecision(SQLModel, table=True):
    """Append-only terminal review for one immutable Roadmap artifact."""

    __tablename__ = "roadmap_artifact_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "roadmap_artifact_id", name="uq_roadmap_decision"
        ),
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'feedback')",
            name="ck_roadmap_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "roadmap_artifact_id", "artifact_fingerprint"],
            [
                "roadmap_artifacts.project_id",
                "roadmap_artifacts.roadmap_artifact_id",
                "roadmap_artifacts.content_fingerprint",
            ],
            name="fk_roadmap_decision_parent",
        ),
    )

    roadmap_artifact_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    roadmap_artifact_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class StoryArtifact(SQLModel, table=True):
    """Immutable Story-set artifact for one accepted Backlog requirement."""

    __tablename__ = "story_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "story_artifact_id", name="uq_story_artifact_project"
        ),
        UniqueConstraint(
            "project_id",
            "story_artifact_id",
            "content_fingerprint",
            name="uq_story_artifact_review_parent",
        ),
        UniqueConstraint(
            "project_id",
            "requirement_id",
            "version_number",
            name="uq_story_artifact_version",
        ),
        UniqueConstraint(
            "project_id",
            "requirement_id",
            "content_fingerprint",
            name="uq_story_artifact_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "roadmap_artifact_id", "roadmap_artifact_fingerprint"],
            [
                "roadmap_artifacts.project_id",
                "roadmap_artifacts.roadmap_artifact_id",
                "roadmap_artifacts.content_fingerprint",
            ],
            name="fk_story_artifact_roadmap",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_story_artifact_id"],
            ["story_artifacts.project_id", "story_artifacts.story_artifact_id"],
            name="fk_story_artifact_supersedes",
        ),
    )

    story_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    requirement_id: str = Field(index=True)
    roadmap_artifact_id: int = Field(index=True)
    roadmap_artifact_fingerprint: str = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    story_ids_json: str = Field(sa_type=Text)
    supersedes_story_artifact_id: int | None = Field(default=None, index=True)
    created_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class StoryArtifactDecision(SQLModel, table=True):
    """Append-only terminal review for one immutable Story artifact."""

    __tablename__ = "story_artifact_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "story_artifact_id",
            name="uq_story_artifact_decision",
        ),
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'feedback')",
            name="ck_story_artifact_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "story_artifact_id", "artifact_fingerprint"],
            [
                "story_artifacts.project_id",
                "story_artifacts.story_artifact_id",
                "story_artifacts.content_fingerprint",
            ],
            name="fk_story_artifact_decision_parent",
        ),
    )

    story_artifact_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    story_artifact_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class StoryDependencyReview(SQLModel, table=True):
    """Append-only audit of one reviewed Story dependency set."""

    __tablename__ = "story_dependency_reviews"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "source_fingerprint",
            name="uq_story_dependency_review_source",
        ),
    )

    story_dependency_review_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    selected_story_ids_json: str = Field(sa_type=Text)
    reviewed_edges_json: str = Field(sa_type=Text)
    source_fingerprint: str = Field(index=True)
    dependency_fingerprint: str = Field(index=True)
    reviewed_by: str = Field(index=True)
    reviewed_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintPlanArtifact(SQLModel, table=True):
    """Immutable canonical Sprint plan bound to one candidate-set fingerprint."""

    __tablename__ = "sprint_plan_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "sprint_plan_artifact_id",
            name="uq_sprint_plan_project",
        ),
        UniqueConstraint(
            "project_id",
            "sprint_plan_artifact_id",
            "plan_fingerprint",
            name="uq_sprint_plan_review_parent",
        ),
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_sprint_plan_version",
        ),
        UniqueConstraint(
            "project_id",
            "plan_fingerprint",
            name="uq_sprint_plan_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_sprint_plan_artifact_id"],
            [
                "sprint_plan_artifacts.project_id",
                "sprint_plan_artifacts.sprint_plan_artifact_id",
            ],
            name="fk_sprint_plan_supersedes",
        ),
    )

    sprint_plan_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    version_number: int
    selected_story_ids_json: str = Field(sa_type=Text)
    canonical_task_plan_json: str = Field(sa_type=Text)
    plan_fingerprint: str = Field(index=True)
    candidate_set_fingerprint: str = Field(index=True)
    supersedes_sprint_plan_artifact_id: int | None = Field(default=None, index=True)
    created_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintPlanArtifactDecision(SQLModel, table=True):
    """Append-only terminal review for one immutable Sprint plan."""

    __tablename__ = "sprint_plan_artifact_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "sprint_plan_artifact_id",
            name="uq_sprint_plan_decision",
        ),
        UniqueConstraint(
            "project_id",
            "sprint_plan_artifact_id",
            "sprint_plan_artifact_decision_id",
            name="uq_sprint_plan_decision_lineage",
        ),
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'feedback')",
            name="ck_sprint_plan_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "sprint_plan_artifact_id", "plan_fingerprint"],
            [
                "sprint_plan_artifacts.project_id",
                "sprint_plan_artifacts.sprint_plan_artifact_id",
                "sprint_plan_artifacts.plan_fingerprint",
            ],
            name="fk_sprint_plan_decision_parent",
        ),
    )

    sprint_plan_artifact_decision_id: int | None = Field(
        default=None,
        primary_key=True,
    )
    project_id: int = Field(index=True)
    sprint_plan_artifact_id: int = Field(index=True)
    plan_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    rationale: str = Field(sa_type=Text)
    reviewer: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class SprintStart(SQLModel, table=True):
    """Immutable StartSprint lineage for one accepted Sprint plan."""

    __tablename__ = "sprint_starts"
    __table_args__ = (
        UniqueConstraint("sprint_id", name="uq_sprint_start"),
        UniqueConstraint("audit_event_id", name="uq_sprint_start_audit_event"),
        ForeignKeyConstraint(
            [
                "project_id",
                "sprint_plan_artifact_id",
                "sprint_plan_artifact_decision_id",
            ],
            [
                "sprint_plan_artifact_decisions.project_id",
                "sprint_plan_artifact_decisions.sprint_plan_artifact_id",
                "sprint_plan_artifact_decisions.sprint_plan_artifact_decision_id",
            ],
            name="fk_sprint_start_accepted_plan",
        ),
    )

    sprint_start_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    sprint_plan_artifact_id: int = Field(index=True)
    sprint_plan_artifact_decision_id: int = Field(index=True)
    story_dependency_review_id: int = Field(
        foreign_key="story_dependency_reviews.story_dependency_review_id",
        index=True,
    )
    plan_fingerprint: str = Field(index=True)
    candidate_set_fingerprint: str = Field(index=True)
    selected_story_ids_json: str = Field(sa_type=Text)
    task_content_fingerprint: str = Field(index=True)
    dependency_source_fingerprint: str = Field(index=True)
    dependency_fingerprint: str = Field(index=True)
    dependency_rows_fingerprint: str = Field(index=True)
    decision_fingerprint: str = Field(index=True)
    audit_event_id: int = Field(foreign_key="workflow_events.event_id", index=True)
    started_by: str = Field(index=True)
    started_at: datetime = Field(nullable=False)


class TaskCompletionEvidence(SQLModel, table=True):
    """Immutable close evidence for one Task in one Sprint."""

    __tablename__ = "task_completion_evidence"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "sprint_id",
            name="uq_task_completion_evidence",
        ),
        CheckConstraint(
            "acceptance_result IN ('partially_met', 'fully_met')",
            name="ck_task_completion_acceptance",
        ),
    )

    task_completion_evidence_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    task_id: int = Field(foreign_key="tasks.task_id", index=True)
    outcome_summary: str = Field(sa_type=Text)
    artifact_refs_json: str = Field(sa_type=Text)
    acceptance_result: str = Field(index=True)
    checklist_result_json: str = Field(sa_type=Text)
    evidence_fingerprint: str = Field(index=True)
    completed_by: str = Field(index=True)
    completed_at: datetime = Field(nullable=False)


class StoryClosure(SQLModel, table=True):
    """Immutable Story closure bound to exact Task completion facts."""

    __tablename__ = "story_closures"
    __table_args__ = (
        UniqueConstraint("story_id", "sprint_id", name="uq_story_closure"),
    )

    story_closure_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    completion_fingerprint: str = Field(index=True)
    resolution: str
    delivered: str = Field(sa_type=Text)
    evidence: str = Field(sa_type=Text)
    known_gaps: str = Field(sa_type=Text)
    closed_by: str = Field(index=True)
    closed_at: datetime = Field(nullable=False)


class SprintReview(SQLModel, table=True):
    """Persisted review of one exact terminal Sprint work set."""

    __tablename__ = "sprint_reviews"
    __table_args__ = (UniqueConstraint("sprint_id", name="uq_sprint_review"),)

    sprint_review_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    review_fingerprint: str = Field(index=True)
    reviewed_by: str = Field(index=True)
    reviewed_at: datetime = Field(nullable=False)


class SprintClosure(SQLModel, table=True):
    """Explicit Sprint close fact bound to its persisted review."""

    __tablename__ = "sprint_closures"
    __table_args__ = (UniqueConstraint("sprint_id", name="uq_sprint_closure"),)

    sprint_closure_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    review_fingerprint: str = Field(index=True)
    close_fingerprint: str = Field(index=True)
    closed_by: str = Field(index=True)
    closed_at: datetime = Field(nullable=False)


class PostSprintTriage(SQLModel, table=True):
    """Append-only current-or-corrected triage for one completed Sprint."""

    __tablename__ = "post_sprint_triage"
    __table_args__ = (
        CheckConstraint(
            "impact IN ('none', 'backlog', 'specification')",
            name="ck_post_sprint_triage_impact",
        ),
        UniqueConstraint(
            "project_id",
            "sprint_id",
            "supersedes_triage_id",
            name="uq_post_sprint_triage_correction",
        ),
    )

    triage_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    sprint_id: int = Field(foreign_key="sprints.sprint_id", index=True)
    impact: str = Field(index=True)
    canonical_payload_json: str = Field(sa_type=Text)
    payload_fingerprint: str = Field(index=True)
    supersedes_triage_id: int | None = Field(
        default=None,
        foreign_key="post_sprint_triage.triage_id",
        index=True,
    )
    recorded_by: str = Field(index=True)
    recorded_at: datetime = Field(nullable=False)


class WorkflowNodeAttempt(SQLModel, table=True):
    """Durable lease and input identity for one agentic node execution."""

    __tablename__ = "workflow_node_attempts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "workflow_node_attempt_id",
            name="uq_workflow_attempt_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "workflow_node_attempt_id",
            "attempt_fingerprint",
            name="uq_workflow_attempt_identity",
        ),
        CheckConstraint(
            "lease_expires_at > started_at",
            name="ck_workflow_attempt_lease",
        ),
    )

    workflow_node_attempt_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
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
