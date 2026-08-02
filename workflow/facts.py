"""Named immutable workflow facts used to evaluate the domain graph."""

from __future__ import annotations

import datetime as _datetime
from typing import Literal

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


class PhaseArtifactFact(FrozenModel):
    """Current lifecycle state for a phase artifact."""

    artifact_type: Literal["vision", "backlog", "roadmap", "story_set", "sprint_plan"]
    artifact_id: str
    artifact_fingerprint: str
    status: Literal["draft", "pending_review", "accepted", "rejected", "superseded"]


class SprintFact(FrozenModel):
    """Sprint lifecycle state."""

    sprint_id: int
    status: Literal["planned", "active", "completed"]
    completed_at: _DATETIME | None


class StoryFact(FrozenModel):
    """Story readiness state used for sprint evaluation."""

    story_id: int
    status: str
    sprint_candidate: bool
    readiness_blockers: tuple[str, ...]


class TaskFact(FrozenModel):
    """Task dependency state used for sprint execution evaluation."""

    task_id: int
    sprint_id: int
    story_id: int
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
    repository_baselines: tuple[RepositoryBaselineFact, ...] = ()
    repository_inventories: tuple[RepositoryInventoryFact, ...] = ()
    authorities: tuple[AuthorityFact, ...] = ()
    phase_artifacts: tuple[PhaseArtifactFact, ...] = ()
    sprints: tuple[SprintFact, ...] = ()
    stories: tuple[StoryFact, ...] = ()
    tasks: tuple[TaskFact, ...] = ()
    post_sprint_triage: tuple[PostSprintTriageFact, ...] = ()
    node_attempts: tuple[NodeAttemptFact, ...] = ()
