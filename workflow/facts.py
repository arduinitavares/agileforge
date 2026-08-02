"""Named immutable workflow facts used to evaluate the domain graph."""

from __future__ import annotations

import datetime as _datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from workflow.contracts import FrozenModel

_DATETIME = _datetime.datetime


class ProjectFact(FrozenModel):
    """Durable project identity and origin."""

    project_id: int
    name: str
    origin: Literal["greenfield", "brownfield"]
    created_at: _DATETIME


class ProjectAbandonmentFact(FrozenModel):
    """Recorded abandonment of a project."""

    project_abandonment_id: int
    project_id: int
    reason: str
    abandoned_by: str
    abandoned_at: _DATETIME


class DiscoveryRunFact(FrozenModel):
    """Discovery run lifecycle state."""

    discovery_run_id: int
    project_id: int
    purpose: Literal["initial", "extension"]
    ordinal: int
    created_at: _DATETIME
    closed_at: _DATETIME | None


class DiscoveryRunAbandonmentFact(FrozenModel):
    """Recorded abandonment of a discovery run."""

    discovery_run_abandonment_id: int
    project_id: int
    discovery_run_id: int
    reason: str
    abandoned_by: str
    abandoned_at: _DATETIME


class ChallengeArtifactFact(FrozenModel):
    """Immutable challenge artifact version."""

    challenge_artifact_id: int
    discovery_run_id: int
    content_fingerprint: str
    supersedes_id: int | None


class PrdVersionFact(FrozenModel):
    """Immutable PRD version."""

    prd_version_id: int
    discovery_run_id: int
    content_fingerprint: str
    supersedes_id: int | None


class ReviewDecisionFact(FrozenModel):
    """Review decision for a versioned workflow artifact."""

    decision_id: int
    artifact_type: Literal[
        "prd",
        "spec_draft",
        "authority",
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


class SpecDraftFact(FrozenModel):
    """Immutable specification draft version."""

    spec_draft_id: int
    discovery_run_id: int
    kind: Literal["initial", "amendment"]
    content_fingerprint: str
    base_spec_version_id: int | None
    base_spec_hash: str | None
    supersedes_id: int | None


class InitialScopeRegistrationFact(FrozenModel):
    """Registration of the accepted initial scope."""

    registration_id: int
    discovery_run_id: int
    spec_draft_id: int
    spec_version_id: int
    spec_hash: str


class SpecVersionFact(FrozenModel):
    """Approved or superseded registered specification version."""

    spec_version_id: int
    spec_hash: str
    status: Literal["approved", "superseded"]
    approved_at: _DATETIME | None


class RepositoryBaselineFact(FrozenModel):
    """Versioned repository identity captured for brownfield onboarding."""

    repository_baseline_id: int
    repository_path: str
    git_commit: str | None
    dirty: bool
    content_fingerprint: str


class RepositoryInventoryFact(FrozenModel):
    """Complete repository inventory and separate bounded model selection."""

    repository_inventory_id: int
    repository_baseline_id: int
    content_fingerprint: str
    file_count: int
    total_bytes: int
    selected_for_model: tuple[str, ...]


class AuthorityFact(FrozenModel):
    """Compiled authority associated with a specification version."""

    authority_id: int
    spec_version_id: int
    authority_fingerprint: str
    status: Literal["pending_review", "accepted", "rejected", "stale"]
    decided_at: _DATETIME | None


class AuthorityFeedbackFact(FrozenModel):
    """Immutable feedback recorded against one compiled authority."""

    feedback_id: int
    source_authority_id: int
    source_authority_fingerprint: str
    feedback_fingerprint: str
    recorded_at: _DATETIME


class PhaseArtifactFact(FrozenModel):
    """Current lifecycle state for a phase artifact."""

    artifact_type: Literal["vision", "backlog", "roadmap", "story_set", "sprint_plan"]
    artifact_id: int | str
    artifact_fingerprint: str
    authority_id: int | None = None
    authority_fingerprint: str | None = None
    supersedes_artifact_id: int | None = None
    status: Literal[
        "draft",
        "pending_review",
        "accepted",
        "rejected",
        "feedback",
        "superseded",
    ]


class BacklogReconciliationFact(FrozenModel):
    """Explicit reconciliation of stale product-definition artifacts."""

    reconciliation_id: int
    replacement_authority_id: int
    replacement_authority_fingerprint: str
    affected_artifact_ids: tuple[int, ...]
    affected_artifacts_fingerprint: str
    reconciled_by: str
    audit_event_id: int
    audit_event_action: Literal["backlog_authority_reconciled"]
    audit_event_fingerprint: str
    reconciled_at: _DATETIME


class BacklogRequirementFact(FrozenModel):
    """One stable requirement from the accepted current Backlog artifact."""

    requirement_id: str
    backlog_artifact_id: int
    backlog_artifact_fingerprint: str
    requirement: str
    rank: int


class PlanningArtifactFact(FrozenModel):
    """Immutable Roadmap, Story-set, or Sprint-plan artifact state."""

    artifact_type: Literal["roadmap", "story", "sprint_plan"]
    artifact_id: int
    artifact_fingerprint: str
    source_artifact_id: int | None = None
    source_fingerprint: str
    authority_id: int | None = None
    authority_fingerprint: str | None = None
    backlog_artifact_id: int | None = None
    backlog_artifact_fingerprint: str | None = None
    roadmap_artifact_id: int | None = None
    roadmap_artifact_fingerprint: str | None = None
    requirement_id: str | None = None
    story_ids: tuple[int, ...] = ()
    sprint_id: int | None = None
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


class StoryFact(FrozenModel):
    """Story readiness state used for sprint evaluation."""

    story_id: int
    requirement_id: str | None = None
    content_fingerprint: str | None = None
    content_accepted: bool = False
    story_artifact_id: int | None = None
    authority_id: int | None = None
    authority_fingerprint: str | None = None
    backlog_artifact_id: int | None = None
    backlog_artifact_fingerprint: str | None = None
    roadmap_artifact_id: int | None = None
    roadmap_artifact_fingerprint: str | None = None
    status: str
    story_points: int | None = None
    rank: str | None = None
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


class PostSprintTriageFact(FrozenModel):
    """Post-sprint triage impact record."""

    sprint_id: int
    impact: Literal["none", "backlog", "specification"]
    payload_fingerprint: str


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


class WorkflowFactSnapshot(FrozenModel):
    """Complete immutable fact snapshot used to evaluate one project graph."""

    project: ProjectFact
    project_abandonments: tuple[ProjectAbandonmentFact, ...] = ()
    discovery_runs: tuple[DiscoveryRunFact, ...] = ()
    discovery_run_abandonments: tuple[DiscoveryRunAbandonmentFact, ...] = ()
    challenge_artifacts: tuple[ChallengeArtifactFact, ...] = ()
    prd_versions: tuple[PrdVersionFact, ...] = ()
    review_decisions: tuple[ReviewDecisionFact, ...] = ()
    spec_drafts: tuple[SpecDraftFact, ...] = ()
    initial_registrations: tuple[InitialScopeRegistrationFact, ...] = ()
    spec_versions: tuple[SpecVersionFact, ...] = ()
    repository_baselines: tuple[RepositoryBaselineFact, ...] = ()
    repository_inventories: tuple[RepositoryInventoryFact, ...] = ()
    authorities: tuple[AuthorityFact, ...] = ()
    authority_feedback: tuple[AuthorityFeedbackFact, ...] = ()
    phase_artifacts: tuple[PhaseArtifactFact, ...] = ()
    backlog_reconciliations: tuple[BacklogReconciliationFact, ...] = ()
    backlog_requirements: tuple[BacklogRequirementFact, ...] = ()
    planning_artifacts: tuple[PlanningArtifactFact, ...] = ()
    sprints: tuple[SprintFact, ...] = ()
    stories: tuple[StoryFact, ...] = ()
    story_dependencies: tuple[StoryDependencyFact, ...] = ()
    story_dependency_reviews: tuple[StoryDependencyReviewFact, ...] = ()
    tasks: tuple[TaskFact, ...] = ()
    post_sprint_triage: tuple[PostSprintTriageFact, ...] = ()
    node_attempts: tuple[NodeAttemptFact, ...] = ()
