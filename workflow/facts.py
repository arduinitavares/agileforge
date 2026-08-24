"""Named immutable workflow facts used to evaluate the domain graph."""

from __future__ import annotations

import datetime as _datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from workflow.contracts import FrozenModel, JsonObject

_DATETIME = _datetime.datetime


def _normalize_utc(value: _DATETIME) -> _DATETIME:
    """Treat SQLite's timezone-free persisted timestamps as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_datetime.UTC)
    return value.astimezone(_datetime.UTC)


class ProjectFact(FrozenModel):
    """Durable Project identity used by the product lifecycle graph."""

    project_id: int
    name: str
    description: str | None = None
    created_at: _DATETIME
    active_repository_binding_id: int | None = None


class ReviewDecisionFact(FrozenModel):
    """Review decision for a versioned workflow artifact."""

    decision_id: int
    artifact_type: Literal[
        "vision",
        "backlog",
        "roadmap",
        "story",
        "sprint",
    ]
    artifact_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    decided_at: _DATETIME


class VisionRevisionIntentFact(FrozenModel):
    """Requested revision of one exact staged Vision artifact."""

    vision_revision_intent_id: int
    source_vision_artifact_id: int
    source_vision_fingerprint: str
    reason: str
    initiated_by: str
    initiated_at: _DATETIME


class VisionEvidenceSnapshotFact(FrozenModel):
    """Immutable evidence used to generate one Vision draft lineage."""

    vision_evidence_snapshot_id: int
    repository_binding_id: int | None
    supersedes_vision_evidence_snapshot_id: int | None
    workflow_node_attempt_id: int
    evidence: JsonObject
    evidence_fingerprint: str
    warnings: tuple[JsonObject, ...]
    created_at: _DATETIME


class VisionInterviewTurnFact(FrozenModel):
    """Immutable Vision generation or clarification turn."""

    vision_interview_turn_id: int
    operation: Literal["bootstrap", "clarification", "revision"]
    turn_number: int
    revision_intent_id: int | None
    vision_evidence_snapshot_id: int
    prior_turn_id: int | None
    user_text: str | None
    components: JsonObject
    vision_statement: str
    is_complete: bool
    clarifying_questions: tuple[JsonObject, ...]
    component_basis: tuple[JsonObject, ...] = ()
    assumptions: tuple[JsonObject, ...] = ()
    conflicts: tuple[JsonObject, ...] = ()
    output_fingerprint: str
    workflow_node_attempt_id: int
    attempt_fingerprint: str
    recorded_at: _DATETIME


class VisionArtifactFact(FrozenModel):
    """Immutable Project Vision built from one complete interview turn."""

    vision_artifact_id: int
    version_number: int
    components: JsonObject
    statement: str
    content_fingerprint: str
    vision_evidence_snapshot_id: int
    component_basis: tuple[JsonObject, ...] = ()
    assumptions: tuple[JsonObject, ...] = ()
    conflicts: tuple[JsonObject, ...] = ()
    supersedes_vision_artifact_id: int | None
    source_interview_turn_id: int
    created_by: str
    created_at: _DATETIME


class VisionArtifactDecisionFact(FrozenModel):
    """Append-only review decision bound to one Vision fingerprint."""

    vision_artifact_decision_id: int
    vision_artifact_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: _DATETIME


class ProductGoalInterviewTurnFact(FrozenModel):
    """Immutable interview turn for one numbered Product Goal revision."""

    product_goal_interview_turn_id: int
    vision_artifact_id: int
    vision_fingerprint: str
    goal_number: int
    revision_number: int
    prior_turn_id: int | None
    user_text: str
    components: JsonObject
    goal_statement: str
    is_complete: bool
    clarifying_questions: tuple[str, ...]
    output_fingerprint: str
    workflow_node_attempt_id: int
    attempt_fingerprint: str
    recorded_at: _DATETIME


class ProductGoalArtifactFact(FrozenModel):
    """Immutable product-goal version with staged Vision lineage."""

    product_goal_artifact_id: int
    vision_artifact_id: int
    vision_fingerprint: str
    goal_number: int
    revision_number: int
    statement: str
    content_fingerprint: str
    supersedes_product_goal_artifact_id: int | None
    source_interview_turn_id: int
    created_by: str
    created_at: _DATETIME


class ProductGoalArtifactDecisionFact(FrozenModel):
    """Immutable review decision recorded against one Product Goal artifact."""

    product_goal_artifact_decision_id: int
    product_goal_artifact_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: _DATETIME


class ProductGoalOutcomeFact(FrozenModel):
    """Durable resolution of one accepted Product Goal."""

    product_goal_outcome_id: int
    product_goal_artifact_id: int
    artifact_fingerprint: str
    outcome: Literal["fulfilled", "abandoned"]
    rationale: str
    decided_by: str
    decided_at: _DATETIME


class SpecificationSourceFact(FrozenModel):
    """Immutable registered external source with exact durable lineage."""

    specification_source_id: int
    source_fingerprint: str
    bundle: JsonObject
    repository_binding_id: int
    repository_head_sha: str
    repository_dirty: bool
    repository_status_fingerprint: str
    vision_artifact_id: int
    vision_fingerprint: str
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    supersedes_specification_source_id: int | None
    supersedes_source_fingerprint: str | None
    registered_by: str
    registered_at: _DATETIME


class SpecificationCandidateFact(FrozenModel):
    """Immutable candidate specification with exact product-definition lineage."""

    specification_candidate_id: int
    candidate_kind: Literal["initial", "amendment"]
    specification_source_id: int
    specification_source_fingerprint: str
    vision_artifact_id: int
    vision_fingerprint: str
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    base_spec_version_id: int | None
    base_spec_hash: str | None
    canonical_envelope: JsonObject = Field(default_factory=dict)
    payload_fingerprint: str
    source_manifest_fingerprint: str
    producer_input_fingerprint: str
    rendered_view_fingerprint: str
    candidate_fingerprint: str
    workflow_node_attempt_id: int
    attempt_fingerprint: str
    supersedes_specification_candidate_id: int | None
    supersedes_candidate_fingerprint: str | None
    recorded_by: str
    recorded_at: _DATETIME

    @field_validator("recorded_at", mode="after")
    @classmethod
    def normalize_recorded_timezone(cls, value: _DATETIME) -> _DATETIME:
        """Normalize the immutable candidate timestamp to UTC."""
        return _normalize_utc(value)


class SpecificationDecisionFact(FrozenModel):
    """Append-only review decision bound to one exact specification candidate."""

    specification_decision_id: int
    specification_candidate_id: int
    candidate_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: _DATETIME

    @field_validator("decided_at", mode="after")
    @classmethod
    def normalize_decided_timezone(cls, value: _DATETIME) -> _DATETIME:
        """Normalize the immutable review timestamp to UTC."""
        return _normalize_utc(value)


class SpecVersionFact(FrozenModel):
    """Approved or superseded registered specification version."""

    spec_version_id: int
    spec_hash: str
    status: Literal["approved", "superseded"]
    source_specification_decision_id: int
    accepted_at: _DATETIME
    accepted_by: str
    acceptance_notes: str
    source_specification_candidate_id: int
    source_specification_candidate_fingerprint: str
    source_vision_artifact_id: int
    source_vision_fingerprint: str
    source_product_goal_artifact_id: int
    source_product_goal_fingerprint: str
    supersedes_spec_version_id: int | None = None

    @field_validator("accepted_at", mode="after")
    @classmethod
    def normalize_accepted_timezone(cls, value: _DATETIME) -> _DATETIME:
        """Normalize the accepted Specification timestamp to UTC."""
        return _normalize_utc(value)


class PhaseArtifactFact(FrozenModel):
    """Immutable Backlog artifact and exact delivery-root lineage."""

    artifact_type: Literal["vision", "backlog"]
    artifact_id: int
    artifact_fingerprint: str
    version_number: int = 1
    spec_version_id: int | None = None
    spec_hash: str | None = None
    product_goal_artifact_id: int | None = None
    product_goal_fingerprint: str | None = None
    supersedes_artifact_id: int | None = None
    status: Literal[
        "draft",
        "pending_review",
        "accepted",
        "rejected",
        "feedback",
        "superseded",
    ]


class BacklogItemFact(FrozenModel):
    """One immutable host-minted item from an exact Backlog artifact."""

    backlog_item_id: str
    backlog_artifact_id: int
    backlog_artifact_fingerprint: str
    item_fingerprint: str
    spec_item_ids: tuple[str, ...]
    priority: int


class PlanningArtifactFact(FrozenModel):
    """Immutable Roadmap, Story-set, or Sprint-plan artifact state."""

    artifact_type: Literal["roadmap", "story", "sprint_plan"]
    artifact_id: int
    artifact_fingerprint: str
    version_number: int = 1
    source_artifact_id: int | None = None
    source_fingerprint: str
    spec_version_id: int | None = None
    spec_hash: str | None = None
    sprint_plan_stream_id: str | None = None
    backlog_artifact_id: int | None = None
    backlog_artifact_fingerprint: str | None = None
    roadmap_artifact_id: int | None = None
    roadmap_artifact_fingerprint: str | None = None
    backlog_item_id: str | None = None
    story_item_ids: tuple[str, ...] = ()
    selected_story_ids: tuple[int, ...] = ()
    activated_sprint_id: int | None = None
    candidate_set_fingerprint: str | None = None
    task_content_fingerprint: str | None = None
    supersedes_artifact_id: int | None = None
    status: Literal[
        "pending_review",
        "accepted",
        "rejected",
        "feedback",
        "superseded",
    ]


class StoryDependencyFact(FrozenModel):
    """One semantic Story dependency edge used by planning joins."""

    dependency_id: int
    dependent_story_id: int
    prerequisite_story_id: int
    status: Literal["proposed", "active", "rejected"]
    source: Literal["story_writer", "dependency_repair", "manual_review"]
    confidence: Literal["explicit", "inferred", "reviewed"]
    reason: str | None = None


class StoryDependencyReviewEdgeFact(FrozenModel):
    """One strictly typed canonical edge captured by dependency review."""

    dependent_story_id: Annotated[int, Field(strict=True)]
    prerequisite_story_id: Annotated[int, Field(strict=True)]
    reason: Annotated[str, Field(strict=True, min_length=1)]

    @model_validator(mode="after")
    def reject_self_edge(self) -> StoryDependencyReviewEdgeFact:
        """Reject a dependency from one Story to itself."""
        if self.dependent_story_id == self.prerequisite_story_id:
            message = "A Story dependency cannot reference itself."
            raise ValueError(message)
        return self


class StoryDependencyReviewFact(FrozenModel):
    """Reviewed dependency-set binding for an exact Story source."""

    review_id: int
    selected_story_ids: tuple[int, ...]
    reviewed_edges: tuple[StoryDependencyReviewEdgeFact, ...]
    source_fingerprint: str
    dependency_fingerprint: str


class SprintFact(FrozenModel):
    """Sprint lifecycle state."""

    sprint_id: int
    status: Literal["planned", "active", "completed"]
    completed_at: _DATETIME | None

    @field_validator("completed_at", mode="after")
    @classmethod
    def normalize_completed_timezone(cls, value: _DATETIME | None) -> _DATETIME | None:
        """Normalize the optional completed Sprint timestamp to UTC."""
        return _normalize_utc(value) if value is not None else None


class SprintStartFact(FrozenModel):
    """Immutable accepted-plan and audit lineage for one Sprint start."""

    start_id: int
    sprint_id: int
    spec_version_id: int
    spec_hash: str
    sprint_plan_artifact_id: int
    sprint_plan_artifact_decision_id: int
    story_dependency_review_id: int
    plan_fingerprint: str
    candidate_set_fingerprint: str
    selected_story_ids: tuple[int, ...]
    task_content_fingerprint: str
    dependency_source_fingerprint: str
    dependency_fingerprint: str
    dependency_rows_fingerprint: str
    decision_fingerprint: str
    audit_event_id: int
    audit_event_fingerprint: str
    started_by: str
    started_at: _DATETIME

    @field_validator("started_at", mode="after")
    @classmethod
    def normalize_started_timezone(cls, value: _DATETIME) -> _DATETIME:
        """Normalize the immutable Sprint-start timestamp to UTC."""
        return _normalize_utc(value)


class StoryFact(FrozenModel):
    """Story readiness state used for sprint evaluation."""

    story_id: int
    source_story_artifact_id: int
    source_story_artifact_fingerprint: str
    source_story_item_id: str
    source_story_item_fingerprint: str
    accepted_spec_version_id: int
    accepted_spec_hash: str
    spec_item_ids: tuple[str, ...]
    content_fingerprint: str | None = None
    content_accepted: bool = False
    story_artifact_id: int | None = None
    backlog_artifact_id: int | None = None
    backlog_artifact_fingerprint: str | None = None
    roadmap_artifact_id: int | None = None
    roadmap_artifact_fingerprint: str | None = None
    backlog_item_id: str | None = None
    status: str
    story_points: int | None = None
    rank: str | None = None
    sprint_ids: tuple[int, ...] = ()
    sprint_candidate: bool
    readiness_blockers: tuple[str, ...]


class TaskFact(FrozenModel):
    """Task dependency state used for sprint execution evaluation."""

    task_id: int
    sprint_id: int
    story_id: int
    description: str
    metadata_json: str
    status: str
    dependencies_satisfied: bool


class TaskCompletionFact(FrozenModel):
    """Immutable Task completion evidence."""

    completion_id: int
    task_id: int
    sprint_id: int
    outcome_summary: str
    artifact_refs: tuple[str, ...]
    acceptance_result: Literal["partially_met", "fully_met"]
    checklist_result: JsonObject
    evidence_fingerprint: str


class StoryCompletionFact(FrozenModel):
    """Immutable Story closure bound to exact Task facts."""

    completion_id: int
    story_id: int
    sprint_id: int
    completion_fingerprint: str
    resolution: str
    delivered: str
    evidence: str
    known_gaps: str


class SprintReviewFact(FrozenModel):
    """Persisted review fingerprint for one Sprint."""

    review_id: int
    sprint_id: int
    review_fingerprint: str


class SprintClosureFact(FrozenModel):
    """Explicit Sprint close fact bound to a persisted review."""

    closure_id: int
    sprint_id: int
    review_fingerprint: str
    close_fingerprint: str


class PostSprintTriageFact(FrozenModel):
    """Post-sprint triage impact record."""

    triage_id: int
    sprint_id: int
    impact: Literal["none", "backlog", "specification"]
    canonical_payload: JsonObject
    payload_fingerprint: str
    supersedes_triage_id: int | None = None


class NodeAttemptFact(FrozenModel):
    """Durable execution attempt tied to an evaluated node decision."""

    attempt_id: int
    node_id: str
    instance_key: str | None
    graph_version: str
    input_fingerprint: str
    fact_fingerprint: str
    business_fact_fingerprint: str
    decision_fingerprint: str
    attempt_fingerprint: str
    model_id: str
    lease_expires_at: _DATETIME
    outcome: Literal["success", "failure", "obsolete"] | None
    failure_code: str | None = None

    @field_validator("lease_expires_at", mode="after")
    @classmethod
    def normalize_lease_timezone(cls, value: _DATETIME) -> _DATETIME:
        """Normalize the lease expiry timestamp to UTC."""
        return _normalize_utc(value)


class WorkflowFactSnapshot(FrozenModel):
    """Complete immutable fact snapshot used to evaluate one project graph."""

    project: ProjectFact
    review_decisions: tuple[ReviewDecisionFact, ...] = ()
    vision_revision_intents: tuple[VisionRevisionIntentFact, ...] = ()
    vision_evidence_snapshots: tuple[VisionEvidenceSnapshotFact, ...] = ()
    vision_interview_turns: tuple[VisionInterviewTurnFact, ...] = ()
    vision_artifacts: tuple[VisionArtifactFact, ...] = ()
    vision_artifact_decisions: tuple[VisionArtifactDecisionFact, ...] = ()
    product_goal_interview_turns: tuple[ProductGoalInterviewTurnFact, ...] = ()
    product_goal_artifacts: tuple[ProductGoalArtifactFact, ...] = ()
    product_goal_artifact_decisions: tuple[ProductGoalArtifactDecisionFact, ...] = ()
    product_goal_outcomes: tuple[ProductGoalOutcomeFact, ...] = ()
    specification_sources: tuple[SpecificationSourceFact, ...] = ()
    specification_candidates: tuple[SpecificationCandidateFact, ...] = ()
    specification_decisions: tuple[SpecificationDecisionFact, ...] = ()
    spec_versions: tuple[SpecVersionFact, ...] = ()
    phase_artifacts: tuple[PhaseArtifactFact, ...] = ()
    backlog_items: tuple[BacklogItemFact, ...] = ()
    planning_artifacts: tuple[PlanningArtifactFact, ...] = ()
    sprints: tuple[SprintFact, ...] = ()
    sprint_starts: tuple[SprintStartFact, ...] = ()
    stories: tuple[StoryFact, ...] = ()
    story_dependencies: tuple[StoryDependencyFact, ...] = ()
    story_dependency_reviews: tuple[StoryDependencyReviewFact, ...] = ()
    tasks: tuple[TaskFact, ...] = ()
    task_completions: tuple[TaskCompletionFact, ...] = ()
    story_completions: tuple[StoryCompletionFact, ...] = ()
    sprint_reviews: tuple[SprintReviewFact, ...] = ()
    sprint_closures: tuple[SprintClosureFact, ...] = ()
    post_sprint_triage: tuple[PostSprintTriageFact, ...] = ()
    node_attempts: tuple[NodeAttemptFact, ...] = ()
