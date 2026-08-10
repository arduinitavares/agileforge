"""Read canonical durable facts for one workflow Project."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.authority_curation import AuthorityFeedbackAttempt
from models.core import (
    Project,
    Sprint,
    SprintStory,
    Task,
    UserStory,
    UserStoryDependency,
)
from models.enums import SprintStatus, StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    BacklogAuthorityReconciliation,
    ChallengeArtifact,
    DiscoveryRun,
    DiscoveryRunAbandonment,
    InitialScopeRegistration,
    PostSprintTriage,
    PrdDecision,
    PrdVersion,
    ProjectAbandonment,
    RepositoryBaseline,
    RepositoryInventory,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    ScopeExtensionReconciliation,
    ScopeExtensionRegistration,
    SpecDraft,
    SpecDraftDecision,
    SprintClosure,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintReview,
    SprintStart,
    StoryArtifact,
    StoryArtifactDecision,
    StoryClosure,
    StoryDependencyReview,
    TaskCompletionEvidence,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
)
from services.contracts.sprint import (
    SprintPlannerOutput,
)
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.contracts import FactReference, JsonObject, JsonValue
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    SprintStartAudit,
    StoryClosurePayload,
    TaskEvidencePayload,
    sprint_start_audit_metadata,
    story_completion_fingerprint,
    task_evidence_fingerprint,
    triage_payload_fingerprint,
)
from workflow.facts import (
    AuthorityFact,
    AuthorityFeedbackFact,
    BacklogReconciliationFact,
    BacklogRequirementFact,
    ChallengeArtifactFact,
    DiscoveryArtifactFact,
    DiscoveryRunAbandonmentFact,
    DiscoveryRunFact,
    InitialScopeRegistrationFact,
    NodeAttemptFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    PostSprintTriageFact,
    PrdVersionFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProductGoalInterviewTurnFact,
    ProductGoalOutcomeFact,
    ProjectAbandonmentFact,
    ProjectFact,
    RepositoryBaselineFact,
    RepositoryInventoryFact,
    ReviewDecisionFact,
    ScopeExtensionReconciliationFact,
    ScopeExtensionRegistrationFact,
    SpecDraftFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SpecVersionFact,
    SprintClosureFact,
    SprintFact,
    SprintReviewFact,
    SprintStartFact,
    StoryCompletionFact,
    StoryDependencyFact,
    StoryDependencyReviewEdgeFact,
    StoryDependencyReviewFact,
    StoryFact,
    TaskCompletionFact,
    TaskFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    VisionInterviewTurnFact,
    VisionRevisionIntentFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
)
from workflow.planning_integrity import (
    dependency_edges_are_canonical,
    dependency_edges_have_cycle,
    dependency_edges_payload,
    dependency_review_fingerprint,
    planned_task_content_fingerprint,
)
from workflow.reconciliation_audit import (
    BACKLOG_RECONCILIATION_ACTION,
    reconciliation_audit_event_fingerprint,
    reconciliation_audit_metadata,
)
from workflow.repository_inventory import (
    decode_repository_path,
    encode_repository_paths,
    inventory_binding_fingerprint,
    repository_path_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_STRING_LIST = TypeAdapter(list[str])
_INT_LIST = TypeAdapter(list[int])
_DEPENDENCY_EDGE_LIST = TypeAdapter(list[StoryDependencyReviewEdgeFact])
_FACT_REFERENCE_LIST = TypeAdapter(list[FactReference])
type _AuthorityStatus = Literal["pending_review", "accepted", "rejected", "stale"]
type _AttemptOutcome = Literal["success", "failure", "obsolete"]
type _DiscoveryPurpose = Literal["initial", "extension"]
type _ProjectOrigin = Literal["greenfield", "brownfield"]
type _ReviewArtifactType = Literal[
    "prd",
    "spec_draft",
    "authority",
    "vision",
    "backlog",
    "roadmap",
    "story",
    "sprint",
]
type _ReviewOutcome = Literal["accepted", "rejected", "feedback"]
type _PhaseStatus = Literal[
    "pending_review",
    "accepted",
    "rejected",
    "feedback",
    "superseded",
]
type _SpecDraftKind = Literal["initial", "amendment"]
type _SprintFactStatus = Literal["planned", "active", "completed"]
type _VisionMode = Literal["initial", "revision"]
type _ProductGoalOutcome = Literal["fulfilled", "abandoned"]
type _ProductGoalDecision = Literal["accepted", "rejected", "feedback"]

_SPRINT_STATUSES: dict[SprintStatus, _SprintFactStatus] = {
    SprintStatus.PLANNED: "planned",
    SprintStatus.ACTIVE: "active",
    SprintStatus.COMPLETED: "completed",
}
_DONE_STORY_STATUSES: frozenset[StoryStatus] = frozenset(
    {StoryStatus.DONE, StoryStatus.ACCEPTED}
)


@dataclass(frozen=True)
class _ReviewDecisionSource:
    """Validated persistence values needed to build one review fact."""

    decision_id: int
    artifact_type: _ReviewArtifactType
    artifact_id: int
    artifact_fingerprint: str
    decision: str
    decided_at: datetime


@dataclass(frozen=True)
class _AuthorityAcceptanceSource:
    """Validated authority decision tied to one exact compiled artifact."""

    decision_id: int
    authority_id: int
    authority_fingerprint: str
    status: str
    decided_at: datetime


@dataclass(frozen=True)
class _AuthorityLoad:
    """Authority facts and their validated review facts from one row set."""

    facts: tuple[AuthorityFact, ...]
    reviews: tuple[ReviewDecisionFact, ...]


@dataclass(frozen=True)
class _PhaseArtifactLoad:
    """Project-definition artifact facts and exact review facts."""

    facts: tuple[PhaseArtifactFact, ...]
    reviews: tuple[ReviewDecisionFact, ...]


@dataclass(frozen=True)
class _ProductDefinitionFactLoad:
    """Validated immutable product-definition facts from one Project."""

    revision_intents: tuple[VisionRevisionIntentFact, ...]
    interview_turns: tuple[VisionInterviewTurnFact, ...]
    visions: tuple[VisionArtifactFact, ...]
    vision_decisions: tuple[VisionArtifactDecisionFact, ...]
    goal_interview_turns: tuple[ProductGoalInterviewTurnFact, ...]
    product_goals: tuple[ProductGoalArtifactFact, ...]
    goal_decisions: tuple[ProductGoalArtifactDecisionFact, ...]
    goal_outcomes: tuple[ProductGoalOutcomeFact, ...]
    discoveries: tuple[DiscoveryArtifactFact, ...]
    specification_candidates: tuple[SpecificationCandidateFact, ...]
    specification_decisions: tuple[SpecificationDecisionFact, ...]


@dataclass(frozen=True)
class _PlanningArtifactLoad:
    """Planning artifact facts and their validated append-only decisions."""

    facts: tuple[PlanningArtifactFact, ...]
    reviews: tuple[ReviewDecisionFact, ...]


@dataclass(frozen=True)
class _PlanningRows:
    """Canonical planning persistence rows for one Project."""

    backlogs: tuple[BacklogArtifact, ...]
    roadmaps: tuple[RoadmapArtifact, ...]
    stories: tuple[StoryArtifact, ...]
    sprint_plans: tuple[SprintPlanArtifact, ...]
    roadmap_decisions: tuple[RoadmapArtifactDecision, ...]
    story_decisions: tuple[StoryArtifactDecision, ...]
    sprint_decisions: tuple[SprintPlanArtifactDecision, ...]


@dataclass(frozen=True)
class _PlanningIndexes:
    """Planning artifact rows addressable by durable identity."""

    backlogs: dict[int, BacklogArtifact]
    roadmaps: dict[int, RoadmapArtifact]
    stories: dict[int, StoryArtifact]
    sprint_plans: dict[int, SprintPlanArtifact]


@dataclass(frozen=True)
class _PlanningDecisionLoad:
    """Validated planning decisions and their review facts."""

    roadmaps: dict[int, RoadmapArtifactDecision]
    stories: dict[int, StoryArtifactDecision]
    sprint_plans: dict[int, SprintPlanArtifactDecision]
    reviews: tuple[ReviewDecisionFact, ...]


@dataclass(frozen=True)
class _ScopeReconciliationSources:
    """Loaded facts that can support a scope-extension reconciliation."""

    registrations: tuple[ScopeExtensionRegistrationFact, ...]
    authorities: tuple[AuthorityFact, ...]
    phase_artifacts: tuple[PhaseArtifactFact, ...]
    planning_artifacts: tuple[PlanningArtifactFact, ...]
    dependency_reviews: tuple[StoryDependencyReviewFact, ...]
    sprint_starts: tuple[SprintStartFact, ...]


@dataclass(frozen=True)
class _ScopeReconciliationReferenceIndex:
    """Canonical immutable reference targets for reconciliation loading."""

    phase: dict[tuple[str, str, str], str]
    planning: dict[tuple[str, str, str], str]
    dependency_reviews: frozenset[tuple[str, str]]
    sprint_starts: frozenset[tuple[str, str]]


class WorkflowFactLoadError(RuntimeError):
    """Raised when stored rows cannot form one consistent Project snapshot."""


class WorkflowFactRepository:
    """Map caller-owned-session rows into immutable workflow facts."""

    def __init__(self, session: Session) -> None:
        """Retain the session whose transaction lifecycle the caller owns."""
        self._session = session
        self._identity_token: object = object()

    def load(self, project_id: int) -> WorkflowFactSnapshot:
        """Load every currently persisted workflow fact for one Project."""
        self._identity_token = object()
        with self._session.no_autoflush:
            return self._load(project_id)

    def load_product_goal_interview_snapshot(
        self, project_id: int
    ) -> WorkflowFactSnapshot:
        """Load only the durable facts needed to prepare a Goal interview."""
        self._identity_token = object()
        with self._session.no_autoflush:
            project = self._project(project_id)
            visions, vision_decisions = self._vision_artifacts(project_id)
            vision_fingerprints = {
                identifier: item.content_fingerprint
                for identifier, item in visions.items()
            }
            turns = self._product_goal_interview_turns(
                project_id,
                vision_fingerprints,
                attempts=None,
            )
            goals = self._product_goals(project_id, vision_fingerprints, turns)
            decisions = self._product_goal_decisions(project_id, goals)
            outcomes = self._product_goal_outcomes(project_id, goals, decisions)
            self._active_accepted_product_goal_ids(goals, decisions, outcomes)
            return WorkflowFactSnapshot(
                project=project,
                vision_artifacts=tuple(visions.values()),
                vision_artifact_decisions=tuple(vision_decisions.values()),
                product_goal_interview_turns=tuple(turns.values()),
                product_goal_artifacts=tuple(goals.values()),
                product_goal_artifact_decisions=tuple(decisions.values()),
                product_goal_outcomes=tuple(outcomes.values()),
            )

    def _load(self, project_id: int) -> WorkflowFactSnapshot:
        """Build one snapshot inside the read-only session query boundary."""
        project = self._project(project_id)
        spec_version_facts = self._spec_versions(project_id)
        spec_versions = {
            item.spec_version_id: item.spec_hash for item in spec_version_facts
        }
        discovery_runs = self._discovery_runs(project_id, spec_versions)
        discovery_run_ids = frozenset(item.discovery_run_id for item in discovery_runs)
        prd_versions = self._prd_versions(project_id, discovery_run_ids)
        spec_drafts = self._spec_drafts(
            project_id,
            discovery_run_ids,
            spec_versions,
        )
        extension_registrations = self._extension_registrations(
            project_id,
            discovery_runs,
            spec_drafts,
            spec_versions,
        )
        authority_load = self._authorities(project_id, spec_versions)
        phase_load = self._phase_artifacts(
            project_id,
            {item.authority_id: item for item in authority_load.facts},
        )
        node_attempts = self._node_attempts(project_id)
        product_definition = self._product_definition(
            project_id,
            spec_versions,
            node_attempts,
        )
        spec_version_facts = self._spec_versions_with_lineage(
            spec_version_facts,
            product_definition,
        )
        spec_versions = {
            item.spec_version_id: item.spec_hash for item in spec_version_facts
        }
        planning_load = self._planning_artifacts(project_id)
        repository_baselines = self._repository_baselines(project_id)
        sprints = self._sprints(project_id)
        stories = self._stories(
            project_id,
            frozenset(spec_versions),
            planning_load.facts,
            frozenset(item.sprint_id for item in sprints),
        )
        story_dependencies = self._story_dependencies(project_id)
        story_dependency_reviews = self._story_dependency_reviews(
            project_id,
            stories,
        )
        tasks = self._tasks(
            project_id,
            frozenset(item.sprint_id for item in sprints),
            stories,
        )
        sprint_starts = self._sprint_starts(
            project_id,
            sprints,
            planning_load.facts,
            story_dependency_reviews,
        )
        execution_snapshot = WorkflowFactSnapshot(
            project=project,
            review_decisions=planning_load.reviews,
            planning_artifacts=planning_load.facts,
            sprints=sprints,
            sprint_starts=sprint_starts,
            stories=stories,
            story_dependencies=story_dependencies,
            story_dependency_reviews=story_dependency_reviews,
            tasks=tasks,
        )
        task_completions = self._task_completions(
            project_id,
            execution_snapshot,
        )
        execution_snapshot = execution_snapshot.model_copy(
            update={"task_completions": task_completions}
        )
        story_completions = self._story_completions(
            project_id,
            execution_snapshot,
        )
        execution_snapshot = execution_snapshot.model_copy(
            update={"story_completions": story_completions}
        )
        sprint_reviews = self._sprint_reviews(
            project_id,
            sprints,
        )
        execution_snapshot = execution_snapshot.model_copy(
            update={"sprint_reviews": sprint_reviews}
        )
        sprint_closures = self._sprint_closures(
            project_id,
            sprints,
        )

        return WorkflowFactSnapshot(
            project=project,
            project_abandonments=self._project_abandonments(project_id),
            discovery_runs=discovery_runs,
            discovery_run_abandonments=self._discovery_run_abandonments(
                project_id,
                discovery_run_ids,
            ),
            challenge_artifacts=self._challenge_artifacts(
                project_id,
                discovery_run_ids,
            ),
            prd_versions=prd_versions,
            review_decisions=self._review_decisions(
                project_id,
                discovery_run_ids,
                {
                    item.prd_version_id: (
                        item.discovery_run_id,
                        item.content_fingerprint,
                    )
                    for item in prd_versions
                },
                {
                    item.spec_draft_id: (
                        item.discovery_run_id,
                        item.content_fingerprint,
                    )
                    for item in spec_drafts
                },
                (
                    *authority_load.reviews,
                    *phase_load.reviews,
                    *planning_load.reviews,
                ),
            ),
            spec_drafts=spec_drafts,
            initial_registrations=self._initial_registrations(
                project_id,
                discovery_run_ids,
                {item.spec_draft_id: item.discovery_run_id for item in spec_drafts},
                spec_versions,
            ),
            extension_registrations=extension_registrations,
            scope_extension_reconciliations=self._scope_extension_reconciliations(
                project_id,
                discovery_runs,
                _ScopeReconciliationSources(
                    registrations=extension_registrations,
                    authorities=authority_load.facts,
                    phase_artifacts=phase_load.facts,
                    planning_artifacts=planning_load.facts,
                    dependency_reviews=story_dependency_reviews,
                    sprint_starts=sprint_starts,
                ),
            ),
            vision_revision_intents=product_definition.revision_intents,
            vision_interview_turns=product_definition.interview_turns,
            vision_artifacts=product_definition.visions,
            vision_artifact_decisions=product_definition.vision_decisions,
            product_goal_interview_turns=product_definition.goal_interview_turns,
            product_goal_artifacts=product_definition.product_goals,
            product_goal_artifact_decisions=product_definition.goal_decisions,
            product_goal_outcomes=product_definition.goal_outcomes,
            discovery_artifacts=product_definition.discoveries,
            specification_candidates=product_definition.specification_candidates,
            specification_decisions=product_definition.specification_decisions,
            spec_versions=spec_version_facts,
            repository_baselines=repository_baselines,
            repository_inventories=self._repository_inventories(
                project_id,
                {item.repository_baseline_id: item for item in repository_baselines},
            ),
            authorities=authority_load.facts,
            authority_feedback=self._authority_feedback(
                project_id,
                {item.authority_id: item for item in authority_load.facts},
            ),
            phase_artifacts=phase_load.facts,
            backlog_reconciliations=self._backlog_reconciliations(
                project_id,
                {item.authority_id: item for item in authority_load.facts},
                phase_load.facts,
            ),
            backlog_requirements=self._backlog_requirements(
                project_id,
                phase_load.facts,
            ),
            planning_artifacts=planning_load.facts,
            sprints=sprints,
            sprint_starts=sprint_starts,
            stories=stories,
            story_dependencies=story_dependencies,
            story_dependency_reviews=story_dependency_reviews,
            tasks=tasks,
            task_completions=task_completions,
            story_completions=story_completions,
            sprint_reviews=sprint_reviews,
            sprint_closures=sprint_closures,
            post_sprint_triage=self._post_sprint_triage(project_id, sprints),
            node_attempts=node_attempts,
        )

    def _query_options(self) -> dict[str, object]:
        """Isolate canonical reads from pending caller identity-map state."""
        return {
            "autoflush": False,
            "identity_token": self._identity_token,
        }

    def _project(self, project_id: int) -> ProjectFact:
        row = self._session.exec(
            select(Project)
            .where(col(Project.project_id) == project_id)
            .order_by(col(Project.project_id)),
            execution_options=self._query_options(),
        ).one_or_none()
        if row is None:
            message = f"Project {project_id} does not exist."
            raise self._error(message)
        if row.project_id is None:
            message = "Project row has no project_id."
            raise self._error(message)
        if row.origin not in {"greenfield", "brownfield"}:
            message = f"Project {project_id} has invalid origin {row.origin!r}."
            raise self._error(message)
        return ProjectFact(
            project_id=row.project_id,
            name=row.name,
            origin=self._project_origin(row.origin),
            created_at=row.created_at,
        )

    def _project_abandonments(
        self,
        project_id: int,
    ) -> tuple[ProjectAbandonmentFact, ...]:
        rows = self._session.exec(
            select(ProjectAbandonment)
            .where(col(ProjectAbandonment.project_id) == project_id)
            .order_by(
                col(ProjectAbandonment.abandoned_at),
                col(ProjectAbandonment.project_abandonment_id),
            ),
            execution_options=self._query_options(),
        ).all()
        return tuple(
            ProjectAbandonmentFact(
                project_abandonment_id=self._required_id(
                    row.project_abandonment_id,
                    "project abandonment",
                ),
                project_id=row.project_id,
                reason=row.reason,
                abandoned_by=row.abandoned_by,
                abandoned_at=row.abandoned_at,
            )
            for row in rows
        )

    def _discovery_runs(
        self,
        project_id: int,
        spec_versions: dict[int, str],
    ) -> tuple[DiscoveryRunFact, ...]:
        rows = self._session.exec(
            select(DiscoveryRun)
            .where(col(DiscoveryRun.project_id) == project_id)
            .order_by(col(DiscoveryRun.ordinal), col(DiscoveryRun.discovery_run_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[DiscoveryRunFact] = []
        for row in rows:
            if row.purpose not in {"initial", "extension"}:
                message = (
                    f"Discovery run {row.discovery_run_id} has invalid purpose "
                    f"{row.purpose!r}."
                )
                raise self._error(message)
            if row.purpose == "initial":
                if (
                    row.base_spec_version_id is not None
                    or row.base_spec_hash is not None
                ):
                    message = "Initial discovery run has an amendment base."
                    raise self._error(message)
            elif (
                row.base_spec_version_id is None
                or row.base_spec_hash is None
                or spec_versions.get(row.base_spec_version_id) != row.base_spec_hash
            ):
                message = "Extension discovery run has a stale or missing base spec."
                raise self._error(message)
            facts.append(
                DiscoveryRunFact(
                    discovery_run_id=self._required_id(
                        row.discovery_run_id,
                        "discovery run",
                    ),
                    project_id=row.project_id,
                    purpose=self._discovery_purpose(row.purpose),
                    ordinal=row.ordinal,
                    created_at=row.created_at,
                    closed_at=row.closed_at,
                    base_spec_version_id=row.base_spec_version_id,
                    base_spec_hash=row.base_spec_hash,
                )
            )
        return tuple(facts)

    def _discovery_run_abandonments(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
    ) -> tuple[DiscoveryRunAbandonmentFact, ...]:
        rows = self._session.exec(
            select(DiscoveryRunAbandonment)
            .where(col(DiscoveryRunAbandonment.project_id) == project_id)
            .order_by(
                col(DiscoveryRunAbandonment.abandoned_at),
                col(DiscoveryRunAbandonment.discovery_run_abandonment_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[DiscoveryRunAbandonmentFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "discovery-run abandonment",
            )
            facts.append(
                DiscoveryRunAbandonmentFact(
                    discovery_run_abandonment_id=self._required_id(
                        row.discovery_run_abandonment_id,
                        "discovery-run abandonment",
                    ),
                    project_id=row.project_id,
                    discovery_run_id=row.discovery_run_id,
                    reason=row.reason,
                    abandoned_by=row.abandoned_by,
                    abandoned_at=row.abandoned_at,
                )
            )
        return tuple(facts)

    def _challenge_artifacts(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
    ) -> tuple[ChallengeArtifactFact, ...]:
        rows = self._session.exec(
            select(ChallengeArtifact)
            .where(col(ChallengeArtifact.project_id) == project_id)
            .order_by(
                col(ChallengeArtifact.discovery_run_id),
                col(ChallengeArtifact.version_number),
                col(ChallengeArtifact.challenge_artifact_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[ChallengeArtifactFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "challenge artifact",
            )
            artifact_id = self._required_id(
                row.challenge_artifact_id,
                "challenge artifact",
            )
            self._validate_canonical_json(
                row.canonical_content_json,
                "challenge artifact",
                artifact_id,
            )
            facts.append(
                ChallengeArtifactFact(
                    challenge_artifact_id=artifact_id,
                    discovery_run_id=row.discovery_run_id,
                    content_fingerprint=row.content_fingerprint,
                    supersedes_id=row.supersedes_challenge_artifact_id,
                )
            )
        runs_by_artifact = {
            item.challenge_artifact_id: item.discovery_run_id for item in facts
        }
        for item in facts:
            self._require_same_run_reference(
                item.supersedes_id,
                item.discovery_run_id,
                runs_by_artifact,
                "challenge artifact supersession",
            )
        return tuple(facts)

    def _prd_versions(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
    ) -> tuple[PrdVersionFact, ...]:
        rows = self._session.exec(
            select(PrdVersion)
            .where(col(PrdVersion.project_id) == project_id)
            .order_by(
                col(PrdVersion.discovery_run_id),
                col(PrdVersion.version_number),
                col(PrdVersion.prd_version_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[PrdVersionFact] = []
        for row in rows:
            self._require_project_run(row.discovery_run_id, discovery_run_ids, "PRD")
            prd_version_id = self._required_id(row.prd_version_id, "PRD")
            self._validate_canonical_json(
                row.canonical_content_json,
                "PRD",
                prd_version_id,
            )
            facts.append(
                PrdVersionFact(
                    prd_version_id=prd_version_id,
                    discovery_run_id=row.discovery_run_id,
                    content_fingerprint=row.content_fingerprint,
                    supersedes_id=row.supersedes_prd_version_id,
                )
            )
        runs_by_prd = {item.prd_version_id: item.discovery_run_id for item in facts}
        for item in facts:
            self._require_same_run_reference(
                item.supersedes_id,
                item.discovery_run_id,
                runs_by_prd,
                "PRD supersession",
            )
        return tuple(facts)

    def _spec_drafts(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
        spec_versions: dict[int, str],
    ) -> tuple[SpecDraftFact, ...]:
        rows = self._session.exec(
            select(SpecDraft)
            .where(col(SpecDraft.project_id) == project_id)
            .order_by(
                col(SpecDraft.discovery_run_id),
                col(SpecDraft.version_number),
                col(SpecDraft.spec_draft_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[SpecDraftFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "specification draft",
            )
            if row.kind not in {"initial", "amendment"}:
                message = (
                    f"Specification draft {row.spec_draft_id} has invalid kind "
                    f"{row.kind!r}."
                )
                raise self._error(message)
            self._validate_spec_draft_base(row, spec_versions)
            spec_draft_id = self._required_id(
                row.spec_draft_id,
                "specification draft",
            )
            self._validate_canonical_json(
                row.canonical_content_json,
                "specification draft",
                spec_draft_id,
            )
            facts.append(
                SpecDraftFact(
                    spec_draft_id=spec_draft_id,
                    discovery_run_id=row.discovery_run_id,
                    kind=self._spec_draft_kind(row.kind),
                    content_fingerprint=row.content_fingerprint,
                    base_spec_version_id=row.base_spec_version_id,
                    base_spec_hash=row.base_spec_hash,
                    supersedes_id=row.supersedes_spec_draft_id,
                )
            )
        runs_by_draft = {item.spec_draft_id: item.discovery_run_id for item in facts}
        for item in facts:
            self._require_same_run_reference(
                item.supersedes_id,
                item.discovery_run_id,
                runs_by_draft,
                "specification draft supersession",
            )
        return tuple(facts)

    def _spec_versions(self, project_id: int) -> tuple[SpecVersionFact, ...]:
        rows = self._session.exec(
            select(SpecRegistry)
            .where(col(SpecRegistry.project_id) == project_id)
            .order_by(col(SpecRegistry.spec_version_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[SpecVersionFact] = []
        for row in rows:
            self._require_product_condition(
                row.status in {"approved", "superseded"},
                "Specification registry has an invalid status.",
            )
            status: Literal["approved", "superseded"] = (
                "approved" if row.status == "approved" else "superseded"
            )
            facts.append(
                SpecVersionFact(
                    spec_version_id=self._required_id(
                        row.spec_version_id,
                        "specification registry row",
                    ),
                    spec_hash=row.spec_hash,
                    status=status,
                    approved_at=row.approved_at,
                    source_specification_candidate_id=(
                        row.source_specification_candidate_id
                    ),
                    source_vision_artifact_id=row.source_vision_artifact_id,
                    source_vision_fingerprint=row.source_vision_fingerprint,
                    source_product_goal_artifact_id=(
                        row.source_product_goal_artifact_id
                    ),
                    source_product_goal_fingerprint=(
                        row.source_product_goal_fingerprint
                    ),
                    source_discovery_artifact_id=row.source_discovery_artifact_id,
                    source_discovery_fingerprint=row.source_discovery_fingerprint,
                    supersedes_spec_version_id=row.supersedes_spec_version_id,
                )
            )
        return tuple(facts)

    def _product_definition(
        self,
        project_id: int,
        spec_versions: dict[int, str],
        node_attempts: tuple[NodeAttemptFact, ...],
    ) -> _ProductDefinitionFactLoad:
        """Load staged product-definition records without ADK session state."""
        visions, vision_decisions = self._vision_artifacts(project_id)
        vision_fingerprints = {
            item_id: item.content_fingerprint for item_id, item in visions.items()
        }
        accepted_visions = {
            item.vision_artifact_id: item.artifact_fingerprint
            for item in vision_decisions.values()
            if item.decision == "accepted"
        }
        attempts = {item.attempt_id: item.attempt_fingerprint for item in node_attempts}
        revisions = self._vision_revision_intents(project_id, accepted_visions)
        turns = self._vision_interview_turns(project_id, revisions, attempts)
        self._validate_vision_artifact_sources(visions, turns)
        goal_turns = self._product_goal_interview_turns(
            project_id, vision_fingerprints, attempts
        )
        goals = self._product_goals(project_id, vision_fingerprints, goal_turns)
        decisions = self._product_goal_decisions(project_id, goals)
        outcomes = self._product_goal_outcomes(
            project_id,
            goals,
            decisions,
        )
        self._active_accepted_product_goal_ids(
            goals,
            decisions,
            outcomes,
        )
        discoveries = self._discovery_artifacts(
            project_id,
            vision_fingerprints,
            goals,
            decisions,
            outcomes,
        )
        candidates = self._specification_candidates(
            project_id, vision_fingerprints, goals, discoveries, spec_versions
        )
        specification_decisions = self._specification_decisions(project_id, candidates)
        return _ProductDefinitionFactLoad(
            revision_intents=tuple(revisions.values()),
            interview_turns=tuple(turns.values()),
            visions=tuple(visions.values()),
            vision_decisions=tuple(vision_decisions.values()),
            goal_interview_turns=tuple(goal_turns.values()),
            product_goals=tuple(goals.values()),
            goal_decisions=tuple(decisions.values()),
            goal_outcomes=tuple(outcomes.values()),
            discoveries=tuple(discoveries.values()),
            specification_candidates=tuple(candidates.values()),
            specification_decisions=tuple(specification_decisions.values()),
        )

    def _vision_artifacts(
        self,
        project_id: int,
    ) -> tuple[
        dict[int, VisionArtifactFact],
        dict[int, VisionArtifactDecisionFact],
    ]:
        """Load the sole typed source of immutable Vision facts and decisions."""
        rows = self._session.exec(
            select(VisionArtifact)
            .where(col(VisionArtifact.project_id) == project_id)
            .order_by(col(VisionArtifact.vision_artifact_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, VisionArtifactFact] = {}
        children: set[int] = set()
        for row in rows:
            identifier = self._required_id(row.vision_artifact_id, "Vision artifact")
            components = self._canonical_json_object(
                row.components_json,
                "Vision artifact components",
            )
            self._require_product_condition(
                canonical_hash({"components": components, "statement": row.statement})
                == row.content_fingerprint,
                "Vision artifact fingerprint changed.",
            )
            parent_id = row.supersedes_vision_artifact_id
            self._require_product_condition(
                parent_id is None or parent_id in facts,
                "Vision artifact supersedes an unknown or later artifact.",
            )
            if parent_id is not None:
                self._require_product_condition(
                    parent_id not in children,
                    "Vision artifact chain branches.",
                )
                children.add(parent_id)
            facts[identifier] = VisionArtifactFact(
                vision_artifact_id=identifier,
                version_number=row.version_number,
                components=components,
                statement=row.statement,
                content_fingerprint=row.content_fingerprint,
                supersedes_vision_artifact_id=parent_id,
                source_interview_turn_id=row.source_interview_turn_id,
                created_by=row.created_by,
                created_at=row.created_at,
            )
        decision_rows = self._session.exec(
            select(VisionArtifactDecision)
            .where(col(VisionArtifactDecision.project_id) == project_id)
            .order_by(col(VisionArtifactDecision.vision_artifact_decision_id)),
            execution_options=self._query_options(),
        ).all()
        decisions: dict[int, VisionArtifactDecisionFact] = {}
        by_vision: set[int] = set()
        for row in decision_rows:
            identifier = self._required_id(
                row.vision_artifact_decision_id,
                "Vision artifact decision",
            )
            artifact = facts.get(row.vision_artifact_id)
            self._require_product_condition(
                artifact is not None
                and artifact.content_fingerprint == row.artifact_fingerprint,
                "Vision decision does not match its artifact.",
            )
            self._require_product_condition(
                row.vision_artifact_id not in by_vision,
                "Vision artifact has contradictory decisions.",
            )
            self._require_product_condition(
                row.decision in {"accepted", "rejected", "feedback"},
                "Vision decision has an invalid value.",
            )
            by_vision.add(row.vision_artifact_id)
            decisions[identifier] = VisionArtifactDecisionFact(
                vision_artifact_decision_id=identifier,
                vision_artifact_id=row.vision_artifact_id,
                artifact_fingerprint=row.artifact_fingerprint,
                decision=self._product_goal_decision(row.decision),
                rationale=row.rationale,
                reviewer=row.reviewer,
                idempotency_key=row.idempotency_key,
                decided_at=row.decided_at,
            )
        return facts, decisions

    def _validate_vision_artifact_sources(
        self,
        visions: dict[int, VisionArtifactFact],
        turns: dict[int, VisionInterviewTurnFact],
    ) -> None:
        """Require every Vision to preserve the exact complete turn that produced it."""
        for artifact in visions.values():
            turn = turns.get(artifact.source_interview_turn_id)
            self._require_product_condition(
                turn is not None
                and turn.is_complete
                and turn.components == artifact.components
                and turn.vision_statement == artifact.statement,
                "Vision artifact does not match a complete source interview turn.",
            )

    def _vision_revision_intents(
        self,
        project_id: int,
        visions: dict[int, str],
    ) -> dict[int, VisionRevisionIntentFact]:
        rows = self._session.exec(
            select(VisionRevisionIntent)
            .where(col(VisionRevisionIntent.project_id) == project_id)
            .order_by(col(VisionRevisionIntent.vision_revision_intent_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, VisionRevisionIntentFact] = {}
        for row in rows:
            identifier = self._required_id(
                row.vision_revision_intent_id,
                "Vision revision intent",
            )
            self._require_fingerprint_reference(
                row.source_vision_artifact_id,
                row.source_vision_fingerprint,
                visions,
                "Vision revision intent source",
            )
            facts[identifier] = VisionRevisionIntentFact(
                vision_revision_intent_id=identifier,
                source_vision_artifact_id=row.source_vision_artifact_id,
                source_vision_fingerprint=row.source_vision_fingerprint,
                reason=row.reason,
                initiated_by=row.initiated_by,
                initiated_at=row.initiated_at,
            )
        return facts

    def _vision_interview_turns(
        self,
        project_id: int,
        revisions: dict[int, VisionRevisionIntentFact],
        attempts: dict[int, str] | None,
    ) -> dict[int, VisionInterviewTurnFact]:
        rows = self._session.exec(
            select(VisionInterviewTurn)
            .where(col(VisionInterviewTurn.project_id) == project_id)
            .order_by(
                col(VisionInterviewTurn.turn_number),
                col(VisionInterviewTurn.vision_interview_turn_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, VisionInterviewTurnFact] = {}
        for row in rows:
            identifier = self._required_id(row.vision_interview_turn_id, "Vision turn")
            message = f"Vision turn {identifier} has invalid mode."
            self._require_product_condition(
                row.mode in {"initial", "revision"},
                message,
            )
            mode = self._vision_mode(row.mode)
            if mode == "initial":
                self._require_product_condition(
                    row.revision_intent_id is None,
                    "Initial Vision turn cannot have a revision intent.",
                )
            else:
                self._require_product_condition(
                    row.revision_intent_id in revisions,
                    "Revision Vision turn requires an exact Project revision intent.",
                )
            prior_turn = (
                None if row.prior_turn_id is None else facts.get(row.prior_turn_id)
            )
            self._require_product_condition(
                row.prior_turn_id is None or prior_turn is not None,
                "Vision turn prior turn is not owned by this Project.",
            )
            if prior_turn is None:
                self._require_product_condition(
                    row.turn_number == 1,
                    "First Vision interview turn must have turn number one.",
                )
            else:
                self._require_product_condition(
                    (prior_turn.mode, prior_turn.revision_intent_id)
                    == (mode, row.revision_intent_id),
                    "Vision turn prior turn has a different mode or revision chain.",
                )
                self._require_product_condition(
                    prior_turn.turn_number + 1 == row.turn_number,
                    "Vision turn prior turn is not sequential.",
                )
            if attempts is not None:
                self._require_fingerprint_reference(
                    row.workflow_node_attempt_id,
                    row.attempt_fingerprint,
                    attempts,
                    "Vision turn workflow attempt",
                )
            components = self._canonical_json_object(
                row.components_json,
                "Vision turn components",
            )
            clarifying_questions = self._canonical_string_list(
                row.clarifying_questions_json,
                "Vision turn clarifying questions",
            )
            self._require_product_condition(
                row.output_fingerprint
                == self._vision_interview_output_fingerprint(
                    components,
                    row.vision_statement,
                    row.is_complete,
                    clarifying_questions,
                ),
                "Vision turn output fingerprint changed.",
            )
            facts[identifier] = VisionInterviewTurnFact(
                vision_interview_turn_id=identifier,
                mode=mode,
                turn_number=row.turn_number,
                revision_intent_id=row.revision_intent_id,
                prior_turn_id=row.prior_turn_id,
                user_text=row.user_text,
                components=components,
                vision_statement=row.vision_statement,
                is_complete=row.is_complete,
                clarifying_questions=clarifying_questions,
                output_fingerprint=row.output_fingerprint,
                workflow_node_attempt_id=row.workflow_node_attempt_id,
                attempt_fingerprint=row.attempt_fingerprint,
                recorded_at=row.recorded_at,
            )
        return facts

    def _product_goal_interview_turns(
        self,
        project_id: int,
        visions: dict[int, str],
        attempts: dict[int, str] | None,
    ) -> dict[int, ProductGoalInterviewTurnFact]:
        rows = self._session.exec(
            select(ProductGoalInterviewTurn)
            .where(col(ProductGoalInterviewTurn.project_id) == project_id)
            .order_by(
                col(ProductGoalInterviewTurn.goal_number),
                col(ProductGoalInterviewTurn.revision_number),
                col(ProductGoalInterviewTurn.product_goal_interview_turn_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, ProductGoalInterviewTurnFact] = {}
        last_turn_by_identity: dict[tuple[int, str, int, int], int] = {}
        for row in rows:
            identifier = self._required_id(
                row.product_goal_interview_turn_id,
                "Product Goal interview turn",
            )
            self._require_fingerprint_reference(
                row.vision_artifact_id,
                row.vision_fingerprint,
                visions,
                "Product Goal interview Vision",
            )
            identity = (
                row.vision_artifact_id,
                row.vision_fingerprint,
                row.goal_number,
                row.revision_number,
            )
            prior_turn = (
                None if row.prior_turn_id is None else facts.get(row.prior_turn_id)
            )
            self._require_product_condition(
                row.prior_turn_id is None or prior_turn is not None,
                "Product Goal interview prior turn is not owned by this Project.",
            )
            if prior_turn is None:
                self._require_product_condition(
                    identity not in last_turn_by_identity,
                    "Product Goal interview chain cannot restart.",
                )
            else:
                self._require_product_condition(
                    (
                        prior_turn.vision_artifact_id,
                        prior_turn.vision_fingerprint,
                        prior_turn.goal_number,
                        prior_turn.revision_number,
                    )
                    == (
                        row.vision_artifact_id,
                        row.vision_fingerprint,
                        row.goal_number,
                        row.revision_number,
                    ),
                    "Product Goal interview prior turn has different identity.",
                )
                self._require_product_condition(
                    last_turn_by_identity.get(identity) == row.prior_turn_id,
                    "Product Goal interview prior turn is not sequential.",
                )
            if attempts is not None:
                self._require_fingerprint_reference(
                    row.workflow_node_attempt_id,
                    row.attempt_fingerprint,
                    attempts,
                    "Product Goal interview workflow attempt",
                )
            components = self._canonical_json_object(
                row.components_json,
                "Product Goal interview components",
            )
            clarifying_questions = self._canonical_string_list(
                row.clarifying_questions_json,
                "Product Goal interview clarifying questions",
            )
            self._require_product_condition(
                row.output_fingerprint
                == product_goal_interview_output_fingerprint(
                    components,
                    row.goal_statement,
                    row.is_complete,
                    clarifying_questions,
                ),
                "Product Goal interview output fingerprint changed.",
            )
            facts[identifier] = ProductGoalInterviewTurnFact(
                product_goal_interview_turn_id=identifier,
                vision_artifact_id=row.vision_artifact_id,
                vision_fingerprint=row.vision_fingerprint,
                goal_number=row.goal_number,
                revision_number=row.revision_number,
                prior_turn_id=row.prior_turn_id,
                user_text=row.user_text,
                components=components,
                goal_statement=row.goal_statement,
                is_complete=row.is_complete,
                clarifying_questions=clarifying_questions,
                output_fingerprint=row.output_fingerprint,
                workflow_node_attempt_id=row.workflow_node_attempt_id,
                attempt_fingerprint=row.attempt_fingerprint,
                recorded_at=row.recorded_at,
            )
            last_turn_by_identity[identity] = identifier
        return facts

    def _product_goals(
        self,
        project_id: int,
        visions: dict[int, str],
        turns: dict[int, ProductGoalInterviewTurnFact] | None,
    ) -> dict[int, ProductGoalArtifactFact]:
        rows = self._session.exec(
            select(ProductGoalArtifact)
            .where(col(ProductGoalArtifact.project_id) == project_id)
            .order_by(col(ProductGoalArtifact.product_goal_artifact_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, ProductGoalArtifactFact] = {}
        for row in rows:
            identifier = self._required_id(row.product_goal_artifact_id, "Product Goal")
            self._require_fingerprint_reference(
                row.vision_artifact_id,
                row.vision_fingerprint,
                visions,
                "Product Goal Vision",
            )
            source_turn = (
                None if turns is None else turns.get(row.source_interview_turn_id)
            )
            if turns is not None:
                self._require_product_condition(
                    source_turn is not None,
                    "Product Goal source turn is not owned by this Project.",
                )
            if source_turn is not None:
                self._require_product_condition(
                    row.statement == source_turn.goal_statement,
                    "Product Goal statement differs from its source interview.",
                )
                self._require_product_condition(
                    source_turn.is_complete,
                    "Product Goal source interview must be complete.",
                )
                self._require_product_condition(
                    product_goal_artifact_fingerprint(
                        source_turn.components, row.statement
                    )
                    == row.content_fingerprint,
                    "Product Goal artifact fingerprint changed.",
                )
                self._require_product_condition(
                    (
                        source_turn.vision_artifact_id,
                        source_turn.vision_fingerprint,
                        source_turn.goal_number,
                        source_turn.revision_number,
                    )
                    == (
                        row.vision_artifact_id,
                        row.vision_fingerprint,
                        row.goal_number,
                        row.revision_number,
                    ),
                    "Product Goal source interview has different identity.",
                )
            self._require_product_condition(
                row.supersedes_product_goal_artifact_id is None
                or row.supersedes_product_goal_artifact_id in facts,
                "Product Goal supersession is invalid.",
            )
            facts[identifier] = ProductGoalArtifactFact(
                product_goal_artifact_id=identifier,
                vision_artifact_id=row.vision_artifact_id,
                vision_fingerprint=row.vision_fingerprint,
                goal_number=row.goal_number,
                revision_number=row.revision_number,
                statement=row.statement,
                content_fingerprint=row.content_fingerprint,
                supersedes_product_goal_artifact_id=(
                    row.supersedes_product_goal_artifact_id
                ),
                source_interview_turn_id=row.source_interview_turn_id,
                created_by=row.created_by,
                created_at=row.created_at,
            )
        return facts

    def _discovery_artifacts(
        self,
        project_id: int,
        visions: dict[int, str],
        goals: dict[int, ProductGoalArtifactFact],
        decisions: dict[int, ProductGoalArtifactDecisionFact],
        outcomes: dict[int, ProductGoalOutcomeFact],
    ) -> dict[int, DiscoveryArtifactFact]:
        rows = self._session.exec(
            select(DiscoveryArtifact)
            .where(col(DiscoveryArtifact.project_id) == project_id)
            .order_by(col(DiscoveryArtifact.discovery_artifact_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, DiscoveryArtifactFact] = {}
        goal_fingerprints = {
            identifier: item.content_fingerprint for identifier, item in goals.items()
        }
        accepted_decisions_by_goal = self._accepted_goal_decisions_by_goal(
            decisions.values()
        )
        outcomes_by_goal = {
            item.product_goal_artifact_id: item for item in outcomes.values()
        }
        for row in rows:
            identifier = self._required_id(row.discovery_artifact_id, "discovery")
            self._require_fingerprint_reference(
                row.vision_artifact_id,
                row.vision_fingerprint,
                visions,
                "discovery Vision",
            )
            self._require_fingerprint_reference(
                row.product_goal_artifact_id,
                row.product_goal_fingerprint,
                goal_fingerprints,
                "discovery Product Goal",
            )
            goal = goals.get(row.product_goal_artifact_id)
            accepted_decisions = accepted_decisions_by_goal.get(
                row.product_goal_artifact_id,
                (),
            )
            self._require_product_condition(
                any(item.decided_at <= row.recorded_at for item in accepted_decisions),
                "Discovery Product Goal was not accepted when discovery was recorded.",
            )
            outcome = outcomes_by_goal.get(row.product_goal_artifact_id)
            self._require_product_condition(
                outcome is None or row.recorded_at < outcome.decided_at,
                "Discovery was recorded after its Product Goal outcome.",
            )
            if goal is not None:
                self._require_product_condition(
                    (goal.vision_artifact_id, goal.vision_fingerprint)
                    == (row.vision_artifact_id, row.vision_fingerprint),
                    "Discovery Vision does not match its Product Goal Vision.",
                )
            canonical_content = self._canonical_object(
                row.canonical_content_json,
                row.content_fingerprint,
                "discovery",
            )
            self._require_product_condition(
                row.supersedes_discovery_artifact_id is None
                or row.supersedes_discovery_artifact_id in facts,
                "Discovery supersession is invalid.",
            )
            facts[identifier] = DiscoveryArtifactFact(
                discovery_artifact_id=identifier,
                vision_artifact_id=row.vision_artifact_id,
                vision_fingerprint=row.vision_fingerprint,
                product_goal_artifact_id=row.product_goal_artifact_id,
                product_goal_fingerprint=row.product_goal_fingerprint,
                canonical_content=canonical_content,
                content_fingerprint=row.content_fingerprint,
                content_ref=row.content_ref,
                producer=row.producer,
                supersedes_discovery_artifact_id=(row.supersedes_discovery_artifact_id),
                recorded_by=row.recorded_by,
                recorded_at=row.recorded_at,
            )
        return facts

    def _specification_candidates(
        self,
        project_id: int,
        visions: dict[int, str],
        goals: dict[int, ProductGoalArtifactFact],
        discoveries: dict[int, DiscoveryArtifactFact],
        spec_versions: dict[int, str],
    ) -> dict[int, SpecificationCandidateFact]:
        rows = self._session.exec(
            select(SpecificationCandidate)
            .where(col(SpecificationCandidate.project_id) == project_id)
            .order_by(col(SpecificationCandidate.specification_candidate_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, SpecificationCandidateFact] = {}
        goal_fingerprints = {
            identifier: item.content_fingerprint for identifier, item in goals.items()
        }
        discovery_fingerprints = {
            identifier: item.content_fingerprint
            for identifier, item in discoveries.items()
        }
        for row in rows:
            identifier = self._required_id(
                row.specification_candidate_id,
                "specification candidate",
            )
            self._require_fingerprint_reference(
                row.vision_artifact_id,
                row.vision_fingerprint,
                visions,
                "specification candidate Vision",
            )
            self._require_fingerprint_reference(
                row.product_goal_artifact_id,
                row.product_goal_fingerprint,
                goal_fingerprints,
                "specification candidate Product Goal",
            )
            self._require_fingerprint_reference(
                row.discovery_artifact_id,
                row.discovery_fingerprint,
                discovery_fingerprints,
                "specification candidate discovery",
            )
            goal = goals.get(row.product_goal_artifact_id)
            discovery = discoveries.get(row.discovery_artifact_id)
            if goal is not None and discovery is not None:
                self._require_product_condition(
                    (goal.vision_artifact_id, goal.vision_fingerprint)
                    == (row.vision_artifact_id, row.vision_fingerprint)
                    == (
                        discovery.vision_artifact_id,
                        discovery.vision_fingerprint,
                    ),
                    "Specification candidate Vision does not match its chain.",
                )
                self._require_product_condition(
                    (
                        row.product_goal_artifact_id,
                        row.product_goal_fingerprint,
                    )
                    == (
                        discovery.product_goal_artifact_id,
                        discovery.product_goal_fingerprint,
                    ),
                    "Specification candidate Product Goal does not match discovery.",
                )
            base_is_complete = (
                row.base_spec_version_id is not None and row.base_spec_hash is not None
            )
            self._require_product_condition(
                base_is_complete
                or (row.base_spec_version_id is None and row.base_spec_hash is None),
                "Specification candidate base specification is invalid.",
            )
            if base_is_complete:
                self._require_fingerprint_reference(
                    row.base_spec_version_id,
                    row.base_spec_hash,
                    spec_versions,
                    "specification candidate base specification",
                )
            canonical_content = self._canonical_object(
                row.canonical_content_json,
                row.content_fingerprint,
                "specification candidate",
            )
            self._require_product_condition(
                row.supersedes_specification_candidate_id is None
                or row.supersedes_specification_candidate_id in facts,
                "Specification candidate supersession is invalid.",
            )
            facts[identifier] = SpecificationCandidateFact(
                specification_candidate_id=identifier,
                vision_artifact_id=row.vision_artifact_id,
                vision_fingerprint=row.vision_fingerprint,
                product_goal_artifact_id=row.product_goal_artifact_id,
                product_goal_fingerprint=row.product_goal_fingerprint,
                discovery_artifact_id=row.discovery_artifact_id,
                discovery_fingerprint=row.discovery_fingerprint,
                base_spec_version_id=row.base_spec_version_id,
                base_spec_hash=row.base_spec_hash,
                canonical_content=canonical_content,
                content_fingerprint=row.content_fingerprint,
                content_ref=row.content_ref,
                supersedes_specification_candidate_id=(
                    row.supersedes_specification_candidate_id
                ),
                recorded_by=row.recorded_by,
                recorded_at=row.recorded_at,
            )
        return facts

    def _product_goal_decisions(
        self,
        project_id: int,
        goals: dict[int, ProductGoalArtifactFact],
    ) -> dict[int, ProductGoalArtifactDecisionFact]:
        rows = self._session.exec(
            select(ProductGoalArtifactDecision)
            .where(col(ProductGoalArtifactDecision.project_id) == project_id)
            .order_by(
                col(ProductGoalArtifactDecision.product_goal_artifact_decision_id)
            ),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, ProductGoalArtifactDecisionFact] = {}
        goal_fingerprints = {
            item_id: item.content_fingerprint for item_id, item in goals.items()
        }
        for row in rows:
            identifier = self._required_id(
                row.product_goal_artifact_decision_id,
                "Product Goal decision",
            )
            self._require_product_condition(
                row.decision in {"accepted", "rejected", "feedback"},
                "Product Goal decision has an invalid value.",
            )
            self._require_fingerprint_reference(
                row.product_goal_artifact_id,
                row.artifact_fingerprint,
                goal_fingerprints,
                "Product Goal decision",
            )
            goal = goals.get(row.product_goal_artifact_id)
            self._require_product_condition(
                goal is not None and goal.created_at < row.decided_at,
                "Product Goal decision must follow Product Goal artifact creation.",
            )
            facts[identifier] = ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=identifier,
                product_goal_artifact_id=row.product_goal_artifact_id,
                artifact_fingerprint=row.artifact_fingerprint,
                decision=self._product_goal_decision(row.decision),
                rationale=row.rationale,
                reviewer=row.reviewer,
                idempotency_key=row.idempotency_key,
                decided_at=row.decided_at,
            )
        return facts

    def _active_accepted_product_goal_ids(
        self,
        goals: dict[int, ProductGoalArtifactFact],
        decisions: dict[int, ProductGoalArtifactDecisionFact],
        outcomes: dict[int, ProductGoalOutcomeFact],
    ) -> frozenset[int]:
        """Return active accepted Goals after rejecting competing Goal selections."""
        accepted_goal_ids = {
            item.product_goal_artifact_id
            for item in decisions.values()
            if item.decision == "accepted"
        }
        outcome_goal_ids = {item.product_goal_artifact_id for item in outcomes.values()}
        unresolved_goal_ids = frozenset(accepted_goal_ids - outcome_goal_ids)
        pending_goal_ids = frozenset(goals) - {
            item.product_goal_artifact_id for item in decisions.values()
        }
        self._require_product_condition(
            len(unresolved_goal_ids) + len(pending_goal_ids) <= 1,
            "Project has more than one unresolved Product Goal selection.",
        )
        return unresolved_goal_ids

    @staticmethod
    def _accepted_goal_decisions_by_goal(
        decisions: Iterable[ProductGoalArtifactDecisionFact],
    ) -> dict[int, tuple[ProductGoalArtifactDecisionFact, ...]]:
        """Group accepted immutable Goal decisions for causal validation."""
        accepted: dict[int, list[ProductGoalArtifactDecisionFact]] = {}
        for item in decisions:
            if item.decision == "accepted":
                accepted.setdefault(item.product_goal_artifact_id, []).append(item)
        return {identifier: tuple(items) for identifier, items in accepted.items()}

    def _product_goal_outcomes(
        self,
        project_id: int,
        goals: dict[int, ProductGoalArtifactFact],
        decisions: dict[int, ProductGoalArtifactDecisionFact],
    ) -> dict[int, ProductGoalOutcomeFact]:
        rows = self._session.exec(
            select(ProductGoalOutcome)
            .where(col(ProductGoalOutcome.project_id) == project_id)
            .order_by(col(ProductGoalOutcome.product_goal_outcome_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, ProductGoalOutcomeFact] = {}
        outcomes_by_goal: set[int] = set()
        accepted_decisions_by_goal = self._accepted_goal_decisions_by_goal(
            decisions.values()
        )
        goal_fingerprints = {
            item_id: item.content_fingerprint for item_id, item in goals.items()
        }
        for row in rows:
            identifier = self._required_id(
                row.product_goal_outcome_id,
                "Product Goal outcome",
            )
            self._require_product_condition(
                row.outcome in {"fulfilled", "abandoned"},
                "Product Goal outcome has an invalid value.",
            )
            self._require_fingerprint_reference(
                row.product_goal_artifact_id,
                row.artifact_fingerprint,
                goal_fingerprints,
                "Product Goal outcome",
            )
            self._require_product_condition(
                any(
                    item.decided_at < row.decided_at
                    for item in accepted_decisions_by_goal.get(
                        row.product_goal_artifact_id,
                        (),
                    )
                ),
                "Product Goal outcome requires a prior accepted Product Goal.",
            )
            self._require_product_condition(
                row.product_goal_artifact_id not in outcomes_by_goal,
                "Product Goal has multiple outcomes.",
            )
            outcomes_by_goal.add(row.product_goal_artifact_id)
            facts[identifier] = ProductGoalOutcomeFact(
                product_goal_outcome_id=identifier,
                product_goal_artifact_id=row.product_goal_artifact_id,
                artifact_fingerprint=row.artifact_fingerprint,
                outcome=self._product_goal_outcome(row.outcome),
                rationale=row.rationale,
                decided_by=row.decided_by,
                decided_at=row.decided_at,
            )
        return facts

    def _specification_decisions(
        self,
        project_id: int,
        candidates: dict[int, SpecificationCandidateFact],
    ) -> dict[int, SpecificationDecisionFact]:
        rows = self._session.exec(
            select(SpecificationDecision)
            .where(col(SpecificationDecision.project_id) == project_id)
            .order_by(col(SpecificationDecision.specification_decision_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, SpecificationDecisionFact] = {}
        decided_candidates: set[int] = set()
        candidate_fingerprints = {
            item_id: item.content_fingerprint for item_id, item in candidates.items()
        }
        for row in rows:
            identifier = self._required_id(
                row.specification_decision_id,
                "Specification decision",
            )
            self._require_product_condition(
                row.decision in {"accepted", "rejected", "feedback"},
                "Specification decision has an invalid value.",
            )
            self._require_fingerprint_reference(
                row.specification_candidate_id,
                row.artifact_fingerprint,
                candidate_fingerprints,
                "Specification decision",
            )
            candidate = candidates.get(row.specification_candidate_id)
            self._require_product_condition(
                candidate is not None and candidate.recorded_at < row.decided_at,
                "Specification decision must follow candidate creation.",
            )
            self._require_product_condition(
                row.specification_candidate_id not in decided_candidates,
                "Specification candidate has multiple terminal review decisions.",
            )
            self._require_product_condition(
                row.decision == "accepted" or bool(row.rationale.strip()),
                "Rejected or feedback specification decisions require rationale.",
            )
            decided_candidates.add(row.specification_candidate_id)
            facts[identifier] = SpecificationDecisionFact(
                specification_decision_id=identifier,
                specification_candidate_id=row.specification_candidate_id,
                artifact_fingerprint=row.artifact_fingerprint,
                decision=self._product_goal_decision(row.decision),
                rationale=row.rationale,
                reviewer=row.reviewer,
                idempotency_key=row.idempotency_key,
                decided_at=row.decided_at,
            )
        return facts

    def _spec_versions_with_lineage(
        self,
        spec_versions: tuple[SpecVersionFact, ...],
        product_definition: _ProductDefinitionFactLoad,
    ) -> tuple[SpecVersionFact, ...]:
        """Validate staging lineage and retain only the current Vision spec line."""
        candidates = {
            item.specification_candidate_id: item
            for item in product_definition.specification_candidates
        }
        versions_by_id = {item.spec_version_id: item for item in spec_versions}
        accepted_candidate_ids = {
            decision.specification_candidate_id
            for decision in product_definition.specification_decisions
            if decision.decision == "accepted"
        }
        current_vision = self._accepted_current_vision(product_definition)
        enriched: list[SpecVersionFact] = []
        for item in spec_versions:
            candidate = candidates.get(item.source_specification_candidate_id)
            source_values = (
                item.source_vision_artifact_id,
                item.source_vision_fingerprint,
                item.source_product_goal_artifact_id,
                item.source_product_goal_fingerprint,
                item.source_discovery_artifact_id,
                item.source_discovery_fingerprint,
            )
            self._require_product_condition(
                candidate is not None,
                "Specification registry source candidate is missing.",
            )
            if candidate is None:
                continue
            expected = (
                candidate.vision_artifact_id,
                candidate.vision_fingerprint,
                candidate.product_goal_artifact_id,
                candidate.product_goal_fingerprint,
                candidate.discovery_artifact_id,
                candidate.discovery_fingerprint,
            )
            self._require_product_condition(
                source_values == expected,
                "Specification registry source lineage changed.",
            )
            self._require_product_condition(
                candidate.specification_candidate_id in accepted_candidate_ids,
                "Specification registry requires an accepted candidate decision.",
            )
            self._require_product_condition(
                item.spec_hash == candidate.content_fingerprint,
                "Specification registry hash does not match its accepted candidate.",
            )
            candidate_fingerprint = candidate.content_fingerprint
            if (
                item.supersedes_spec_version_id is not None
                and item.supersedes_spec_version_id not in versions_by_id
            ):
                self._require_product_condition(
                    False,
                    "Specification registry supersession is invalid.",
                )
            enriched.append(
                item.model_copy(
                    update={
                        "source_specification_candidate_fingerprint": (
                            candidate_fingerprint
                        ),
                        "status": (
                            "superseded"
                            if self._specification_precedes_current_vision(
                                item,
                                current_vision,
                            )
                            else item.status
                        ),
                    }
                )
            )
        return tuple(enriched)

    @staticmethod
    def _accepted_current_vision(
        product_definition: _ProductDefinitionFactLoad,
    ) -> VisionArtifactFact | None:
        """Return one accepted Vision chain leaf without selecting by timestamp."""
        artifacts = product_definition.visions
        by_id = {item.vision_artifact_id: item for item in artifacts}
        superseded_ids = {
            item.supersedes_vision_artifact_id
            for item in artifacts
            if item.supersedes_vision_artifact_id is not None
        }
        current = [
            item for item in artifacts if item.vision_artifact_id not in superseded_ids
        ]
        if len(current) != 1:
            return None
        artifact = current[0]
        decisions = [
            item
            for item in product_definition.vision_decisions
            if item.vision_artifact_id == artifact.vision_artifact_id
        ]
        if (
            artifact.vision_artifact_id not in by_id
            or len(decisions) != 1
            or decisions[0].decision != "accepted"
            or decisions[0].artifact_fingerprint != artifact.content_fingerprint
        ):
            return None
        return artifact

    @staticmethod
    def _specification_precedes_current_vision(
        spec: SpecVersionFact,
        current_vision: VisionArtifactFact | None,
    ) -> bool:
        """Keep staged legacy specifications while retiring replaced Vision lines."""
        return (
            current_vision is not None
            and spec.status == "approved"
            and spec.source_vision_artifact_id is not None
            and (
                spec.source_vision_artifact_id,
                spec.source_vision_fingerprint,
            )
            != (
                current_vision.vision_artifact_id,
                current_vision.content_fingerprint,
            )
        )

    def _authority_feedback(
        self,
        project_id: int,
        authorities: dict[int, AuthorityFact],
    ) -> tuple[AuthorityFeedbackFact, ...]:
        rows = self._session.exec(
            select(AuthorityFeedbackAttempt)
            .where(col(AuthorityFeedbackAttempt.project_id) == project_id)
            .order_by(
                col(AuthorityFeedbackAttempt.created_at),
                col(AuthorityFeedbackAttempt.feedback_row_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[AuthorityFeedbackFact] = []
        for row in rows:
            authority = authorities.get(row.source_authority_id)
            if (
                authority is None
                or authority.authority_fingerprint != row.source_authority_fingerprint
            ):
                message = (
                    "Forced relationship corruption in authority feedback: "
                    f"source authority {row.source_authority_id} does not match."
                )
                raise self._error(message)
            facts.append(
                AuthorityFeedbackFact(
                    feedback_id=self._required_id(
                        row.feedback_row_id,
                        "authority feedback",
                    ),
                    source_authority_id=row.source_authority_id,
                    source_authority_fingerprint=row.source_authority_fingerprint,
                    feedback_fingerprint=row.feedback_fingerprint,
                    recorded_at=row.created_at,
                )
            )
        return tuple(facts)

    def _phase_artifacts(
        self,
        project_id: int,
        authorities: dict[int, AuthorityFact],
    ) -> _PhaseArtifactLoad:
        """Load generic authority-derived phase artifacts, excluding Vision."""
        backlog_rows = self._session.exec(
            select(BacklogArtifact)
            .where(col(BacklogArtifact.project_id) == project_id)
            .order_by(col(BacklogArtifact.backlog_artifact_id)),
            execution_options=self._query_options(),
        ).all()
        backlog_decisions = self._session.exec(
            select(BacklogArtifactDecision)
            .where(col(BacklogArtifactDecision.project_id) == project_id)
            .order_by(col(BacklogArtifactDecision.backlog_artifact_decision_id)),
            execution_options=self._query_options(),
        ).all()

        backlog_by_id = {
            self._required_id(row.backlog_artifact_id, "Backlog artifact"): row
            for row in backlog_rows
        }
        review_facts: list[ReviewDecisionFact] = []

        backlog_decisions_by_id: dict[int, BacklogArtifactDecision] = {}
        for row in backlog_decisions:
            artifact = backlog_by_id.get(row.backlog_artifact_id)
            if (
                artifact is None
                or artifact.content_fingerprint != row.artifact_fingerprint
            ):
                message = "Backlog decision does not match its artifact."
                raise self._error(message)
            if row.backlog_artifact_id in backlog_decisions_by_id:
                message = "Backlog artifact has contradictory decisions."
                raise self._error(message)
            backlog_decisions_by_id[row.backlog_artifact_id] = row
            review_facts.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.backlog_artifact_decision_id,
                            "Backlog decision",
                        ),
                        artifact_type="backlog",
                        artifact_id=row.backlog_artifact_id,
                        artifact_fingerprint=row.artifact_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )

        superseded_backlog_ids = {
            row.supersedes_backlog_artifact_id
            for row in backlog_rows
            if row.supersedes_backlog_artifact_id is not None
        }
        facts: list[PhaseArtifactFact] = []
        for artifact_id, row in backlog_by_id.items():
            self._validate_phase_artifact(
                artifact_id=artifact_id,
                canonical_content_json=row.canonical_content_json,
                content_fingerprint=row.content_fingerprint,
                authority_id=row.authority_id,
                authority_fingerprint=row.authority_fingerprint,
                authorities=authorities,
                supersedes_artifact_id=row.supersedes_backlog_artifact_id,
                known_artifact_ids=frozenset(backlog_by_id),
                label="Backlog",
            )
            decision = backlog_decisions_by_id.get(artifact_id)
            facts.append(
                PhaseArtifactFact(
                    artifact_type="backlog",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.content_fingerprint,
                    authority_id=row.authority_id,
                    authority_fingerprint=row.authority_fingerprint,
                    product_goal_artifact_id=row.product_goal_artifact_id,
                    product_goal_fingerprint=row.product_goal_fingerprint,
                    supersedes_artifact_id=row.supersedes_backlog_artifact_id,
                    status=self._phase_status(
                        None if decision is None else decision.decision,
                        superseded=artifact_id in superseded_backlog_ids,
                    ),
                )
            )
        return _PhaseArtifactLoad(
            facts=tuple(sorted(facts, key=lambda item: int(item.artifact_id))),
            reviews=tuple(
                sorted(
                    review_facts, key=lambda item: (item.decided_at, item.decision_id)
                )
            ),
        )

    def _backlog_reconciliations(
        self,
        project_id: int,
        authorities: dict[int, AuthorityFact],
        artifacts: tuple[PhaseArtifactFact, ...],
    ) -> tuple[BacklogReconciliationFact, ...]:
        rows = self._session.exec(
            select(BacklogAuthorityReconciliation)
            .where(col(BacklogAuthorityReconciliation.project_id) == project_id)
            .order_by(
                col(BacklogAuthorityReconciliation.reconciled_at),
                col(BacklogAuthorityReconciliation.backlog_authority_reconciliation_id),
            ),
            execution_options=self._query_options(),
        ).all()
        artifact_ids = {
            int(item.artifact_id)
            for item in artifacts
            if isinstance(item.artifact_id, int)
        }
        audit_events = self._session.exec(
            select(WorkflowEvent)
            .where(col(WorkflowEvent.project_id) == project_id)
            .where(col(WorkflowEvent.event_type) == WorkflowEventType.BACKLOG_SAVED)
            .order_by(col(WorkflowEvent.event_id)),
            execution_options=self._query_options(),
        ).all()
        audit_events_by_id = {
            self._required_id(event.event_id, "Workflow event"): event
            for event in audit_events
        }
        facts: list[BacklogReconciliationFact] = []
        for row in rows:
            authority = authorities.get(row.replacement_authority_id)
            if (
                authority is None
                or authority.authority_fingerprint
                != row.replacement_authority_fingerprint
            ):
                message = "Backlog reconciliation authority does not match."
                raise self._error(message)
            try:
                affected_ids = tuple(
                    _INT_LIST.validate_json(row.affected_artifact_ids_json)
                )
            except ValidationError as exc:
                message = "Backlog reconciliation artifact IDs are invalid."
                raise self._error(message) from exc
            if (
                not affected_ids
                or affected_ids != tuple(sorted(set(affected_ids)))
                or not set(affected_ids) <= artifact_ids
            ):
                message = (
                    "Backlog reconciliation does not reference exact Project artifacts."
                )
                raise self._error(message)
            expected_fingerprint = canonical_hash(
                {
                    "replacement_authority_id": row.replacement_authority_id,
                    "replacement_authority_fingerprint": (
                        row.replacement_authority_fingerprint
                    ),
                    "affected_artifact_ids": affected_ids,
                }
            )
            if row.affected_artifacts_fingerprint != expected_fingerprint:
                message = "Backlog reconciliation fingerprint changed."
                raise self._error(message)
            reconciliation_id = self._required_id(
                row.backlog_authority_reconciliation_id,
                "Backlog authority reconciliation",
            )
            if row.audit_event_id is None:
                message = "Backlog reconciliation audit event is missing."
                raise self._error(message)
            event = audit_events_by_id.get(row.audit_event_id)
            if event is None or event.timestamp != row.reconciled_at:
                message = "Backlog reconciliation audit event does not match."
                raise self._error(message)
            metadata = reconciliation_audit_metadata(
                reconciliation_id=reconciliation_id,
                reconciled_by=row.reconciled_by,
                replacement_authority_id=row.replacement_authority_id,
                replacement_authority_fingerprint=(
                    row.replacement_authority_fingerprint
                ),
                affected_artifact_ids=affected_ids,
                affected_artifacts_fingerprint=expected_fingerprint,
            )
            if event.event_metadata != canonical_json(metadata):
                message = "Backlog reconciliation audit content changed."
                raise self._error(message)
            expected_audit_fingerprint = reconciliation_audit_event_fingerprint(
                event_id=row.audit_event_id,
                event_type=event.event_type.value,
                project_id=project_id,
                timestamp=event.timestamp,
                metadata=metadata,
            )
            if row.audit_event_fingerprint != expected_audit_fingerprint:
                message = "Backlog reconciliation audit fingerprint changed."
                raise self._error(message)
            facts.append(
                BacklogReconciliationFact(
                    reconciliation_id=reconciliation_id,
                    replacement_authority_id=row.replacement_authority_id,
                    replacement_authority_fingerprint=(
                        row.replacement_authority_fingerprint
                    ),
                    affected_artifact_ids=affected_ids,
                    affected_artifacts_fingerprint=expected_fingerprint,
                    reconciled_by=row.reconciled_by,
                    audit_event_id=row.audit_event_id,
                    audit_event_action=BACKLOG_RECONCILIATION_ACTION,
                    audit_event_fingerprint=expected_audit_fingerprint,
                    reconciled_at=row.reconciled_at,
                )
            )
        return tuple(facts)

    @staticmethod
    def _validate_phase_artifact(  # noqa: PLR0913
        *,
        artifact_id: int,
        canonical_content_json: str,
        content_fingerprint: str,
        authority_id: int,
        authority_fingerprint: str,
        authorities: dict[int, AuthorityFact],
        supersedes_artifact_id: int | None,
        known_artifact_ids: frozenset[int],
        label: str,
    ) -> None:
        authority = authorities.get(authority_id)
        if (
            authority is None
            or authority.authority_fingerprint != authority_fingerprint
        ):
            message = f"{label} artifact authority does not match."
            raise WorkflowFactRepository._error(message)
        try:
            canonical_content = _JSON_OBJECT.validate_json(canonical_content_json)
        except ValidationError as exc:
            message = f"Stored canonical {label} artifact JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc
        if canonical_hash(canonical_content) != content_fingerprint:
            message = f"Stored canonical {label} artifact fingerprint changed."
            raise WorkflowFactRepository._error(message)
        if supersedes_artifact_id is not None and (
            supersedes_artifact_id not in known_artifact_ids
            or supersedes_artifact_id >= artifact_id
        ):
            message = f"{label} artifact supersession is invalid."
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _phase_status(decision: str | None, *, superseded: bool) -> _PhaseStatus:
        if superseded:
            return "superseded"
        if decision is None:
            return "pending_review"
        return WorkflowFactRepository._review_outcome(decision)

    def _backlog_requirements(
        self,
        project_id: int,
        phase_artifacts: tuple[PhaseArtifactFact, ...],
    ) -> tuple[BacklogRequirementFact, ...]:
        """Load stable normalized requirement identities from Backlog content."""
        phase_by_id = {
            int(item.artifact_id): item
            for item in phase_artifacts
            if item.artifact_type == "backlog" and isinstance(item.artifact_id, int)
        }
        rows = self._session.exec(
            select(BacklogArtifact)
            .where(col(BacklogArtifact.project_id) == project_id)
            .order_by(col(BacklogArtifact.backlog_artifact_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[BacklogRequirementFact] = []
        for row in rows:
            artifact_id = self._required_id(
                row.backlog_artifact_id,
                "Backlog artifact",
            )
            artifact = phase_by_id.get(artifact_id)
            if (
                artifact is None
                or artifact.artifact_fingerprint != row.content_fingerprint
            ):
                message = "Backlog requirements do not match their artifact fact."
                raise self._error(message)
            content = self._canonical_object(
                row.canonical_content_json,
                row.content_fingerprint,
                "Backlog",
            )
            raw_items = content.get("backlog_items")
            if not isinstance(raw_items, list):
                message = "Canonical Backlog artifact has no backlog_items list."
                raise self._error(message)
            seen: set[str] = set()
            for fallback_rank, raw_item in enumerate(raw_items, start=1):
                if not isinstance(raw_item, dict):
                    message = "Canonical Backlog artifact contains an invalid item."
                    raise self._error(message)
                requirement = raw_item.get("requirement")
                if not isinstance(requirement, str) or not requirement.strip():
                    message = "Canonical Backlog item has no requirement text."
                    raise self._error(message)
                requirement_id = " ".join(requirement.strip().lower().split())
                if requirement_id in seen:
                    message = "Canonical Backlog artifact has duplicate requirements."
                    raise self._error(message)
                seen.add(requirement_id)
                raw_rank = raw_item.get("priority")
                rank = (
                    raw_rank
                    if isinstance(raw_rank, int) and raw_rank > 0
                    else fallback_rank
                )
                facts.append(
                    BacklogRequirementFact(
                        requirement_id=requirement_id,
                        backlog_artifact_id=artifact_id,
                        backlog_artifact_fingerprint=row.content_fingerprint,
                        requirement=requirement.strip(),
                        rank=rank,
                    )
                )
        return tuple(
            sorted(
                facts,
                key=lambda item: (
                    item.backlog_artifact_id,
                    item.requirement_id,
                ),
            )
        )

    @staticmethod
    def _canonical_object(
        canonical_content_json: str,
        expected_fingerprint: str,
        label: str,
    ) -> dict[str, JsonValue]:
        try:
            content = _JSON_OBJECT.validate_json(canonical_content_json)
        except ValidationError as exc:
            message = f"Stored canonical {label} artifact JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc
        if canonical_json(content) != canonical_content_json:
            message = f"Stored canonical {label} artifact JSON changed."
            raise WorkflowFactRepository._error(message)
        if canonical_hash(content) != expected_fingerprint:
            message = f"Stored canonical {label} artifact fingerprint changed."
            raise WorkflowFactRepository._error(message)
        return content

    def _planning_rows(self, project_id: int) -> _PlanningRows:
        return _PlanningRows(
            backlogs=tuple(
                self._session.exec(
                    select(BacklogArtifact)
                    .where(col(BacklogArtifact.project_id) == project_id)
                    .order_by(col(BacklogArtifact.backlog_artifact_id)),
                    execution_options=self._query_options(),
                ).all()
            ),
            roadmaps=tuple(
                self._session.exec(
                    select(RoadmapArtifact)
                    .where(col(RoadmapArtifact.project_id) == project_id)
                    .order_by(col(RoadmapArtifact.roadmap_artifact_id)),
                    execution_options=self._query_options(),
                ).all()
            ),
            stories=tuple(
                self._session.exec(
                    select(StoryArtifact)
                    .where(col(StoryArtifact.project_id) == project_id)
                    .order_by(col(StoryArtifact.story_artifact_id)),
                    execution_options=self._query_options(),
                ).all()
            ),
            sprint_plans=tuple(
                self._session.exec(
                    select(SprintPlanArtifact)
                    .where(col(SprintPlanArtifact.project_id) == project_id)
                    .order_by(col(SprintPlanArtifact.sprint_plan_artifact_id)),
                    execution_options=self._query_options(),
                ).all()
            ),
            roadmap_decisions=tuple(
                self._session.exec(
                    select(RoadmapArtifactDecision)
                    .where(col(RoadmapArtifactDecision.project_id) == project_id)
                    .order_by(
                        col(RoadmapArtifactDecision.roadmap_artifact_decision_id)
                    ),
                    execution_options=self._query_options(),
                ).all()
            ),
            story_decisions=tuple(
                self._session.exec(
                    select(StoryArtifactDecision)
                    .where(col(StoryArtifactDecision.project_id) == project_id)
                    .order_by(col(StoryArtifactDecision.story_artifact_decision_id)),
                    execution_options=self._query_options(),
                ).all()
            ),
            sprint_decisions=tuple(
                self._session.exec(
                    select(SprintPlanArtifactDecision)
                    .where(col(SprintPlanArtifactDecision.project_id) == project_id)
                    .order_by(
                        col(SprintPlanArtifactDecision.sprint_plan_artifact_decision_id)
                    ),
                    execution_options=self._query_options(),
                ).all()
            ),
        )

    def _planning_indexes(self, rows: _PlanningRows) -> _PlanningIndexes:
        return _PlanningIndexes(
            backlogs={
                self._required_id(row.backlog_artifact_id, "Backlog artifact"): row
                for row in rows.backlogs
            },
            roadmaps={
                self._required_id(row.roadmap_artifact_id, "Roadmap artifact"): row
                for row in rows.roadmaps
            },
            stories={
                self._required_id(row.story_artifact_id, "Story artifact"): row
                for row in rows.stories
            },
            sprint_plans={
                self._required_id(
                    row.sprint_plan_artifact_id,
                    "Sprint plan artifact",
                ): row
                for row in rows.sprint_plans
            },
        )

    def _roadmap_planning_decisions(
        self,
        rows: _PlanningRows,
        indexes: _PlanningIndexes,
    ) -> tuple[dict[int, RoadmapArtifactDecision], tuple[ReviewDecisionFact, ...]]:
        decisions: dict[int, RoadmapArtifactDecision] = {}
        reviews: list[ReviewDecisionFact] = []
        for row in rows.roadmap_decisions:
            artifact = indexes.roadmaps.get(row.roadmap_artifact_id)
            if (
                artifact is None
                or artifact.content_fingerprint != row.artifact_fingerprint
            ):
                message = "Roadmap decision does not match its artifact."
                raise self._error(message)
            if row.roadmap_artifact_id in decisions:
                message = "Roadmap artifact has contradictory decisions."
                raise self._error(message)
            decisions[row.roadmap_artifact_id] = row
            reviews.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.roadmap_artifact_decision_id,
                            "Roadmap decision",
                        ),
                        artifact_type="roadmap",
                        artifact_id=row.roadmap_artifact_id,
                        artifact_fingerprint=row.artifact_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )
        return decisions, tuple(reviews)

    def _story_planning_decisions(
        self,
        rows: _PlanningRows,
        indexes: _PlanningIndexes,
    ) -> tuple[dict[int, StoryArtifactDecision], tuple[ReviewDecisionFact, ...]]:
        decisions: dict[int, StoryArtifactDecision] = {}
        reviews: list[ReviewDecisionFact] = []
        for row in rows.story_decisions:
            artifact = indexes.stories.get(row.story_artifact_id)
            if (
                artifact is None
                or artifact.content_fingerprint != row.artifact_fingerprint
            ):
                message = "Story decision does not match its artifact."
                raise self._error(message)
            if row.story_artifact_id in decisions:
                message = "Story artifact has contradictory decisions."
                raise self._error(message)
            decisions[row.story_artifact_id] = row
            reviews.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.story_artifact_decision_id,
                            "Story decision",
                        ),
                        artifact_type="story",
                        artifact_id=row.story_artifact_id,
                        artifact_fingerprint=row.artifact_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )
        return decisions, tuple(reviews)

    def _sprint_planning_decisions(
        self,
        rows: _PlanningRows,
        indexes: _PlanningIndexes,
    ) -> tuple[dict[int, SprintPlanArtifactDecision], tuple[ReviewDecisionFact, ...]]:
        decisions: dict[int, SprintPlanArtifactDecision] = {}
        reviews: list[ReviewDecisionFact] = []
        for row in rows.sprint_decisions:
            artifact = indexes.sprint_plans.get(row.sprint_plan_artifact_id)
            if artifact is None or artifact.plan_fingerprint != row.plan_fingerprint:
                message = "Sprint plan decision does not match its artifact."
                raise self._error(message)
            if row.sprint_plan_artifact_id in decisions:
                message = "Sprint plan artifact has contradictory decisions."
                raise self._error(message)
            decisions[row.sprint_plan_artifact_id] = row
            reviews.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.sprint_plan_artifact_decision_id,
                            "Sprint plan decision",
                        ),
                        artifact_type="sprint",
                        artifact_id=row.sprint_plan_artifact_id,
                        artifact_fingerprint=row.plan_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )
        return decisions, tuple(reviews)

    def _planning_decisions(
        self,
        rows: _PlanningRows,
        indexes: _PlanningIndexes,
    ) -> _PlanningDecisionLoad:
        roadmaps, roadmap_reviews = self._roadmap_planning_decisions(rows, indexes)
        stories, story_reviews = self._story_planning_decisions(rows, indexes)
        sprint_plans, sprint_reviews = self._sprint_planning_decisions(rows, indexes)
        reviews = (*roadmap_reviews, *story_reviews, *sprint_reviews)
        return _PlanningDecisionLoad(
            roadmaps=roadmaps,
            stories=stories,
            sprint_plans=sprint_plans,
            reviews=tuple(
                sorted(
                    reviews,
                    key=lambda item: (
                        item.artifact_type,
                        item.artifact_id,
                        item.decision_id,
                    ),
                )
            ),
        )

    def _roadmap_planning_facts(
        self,
        rows: _PlanningRows,
        indexes: _PlanningIndexes,
        decisions: _PlanningDecisionLoad,
    ) -> tuple[PlanningArtifactFact, ...]:
        superseded = {
            row.supersedes_roadmap_artifact_id
            for row in rows.roadmaps
            if row.supersedes_roadmap_artifact_id is not None
        }
        facts: list[PlanningArtifactFact] = []
        for artifact_id, row in indexes.roadmaps.items():
            self._canonical_object(
                row.canonical_content_json,
                row.content_fingerprint,
                "Roadmap",
            )
            backlog = indexes.backlogs.get(row.backlog_artifact_id)
            if (
                backlog is None
                or backlog.content_fingerprint != row.backlog_artifact_fingerprint
            ):
                message = "Roadmap artifact source Backlog changed."
                raise self._error(message)
            decision = decisions.roadmaps.get(artifact_id)
            facts.append(
                PlanningArtifactFact(
                    artifact_type="roadmap",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.content_fingerprint,
                    source_artifact_id=row.backlog_artifact_id,
                    source_fingerprint=row.backlog_artifact_fingerprint,
                    authority_id=backlog.authority_id,
                    authority_fingerprint=backlog.authority_fingerprint,
                    backlog_artifact_id=row.backlog_artifact_id,
                    backlog_artifact_fingerprint=row.backlog_artifact_fingerprint,
                    roadmap_artifact_id=artifact_id,
                    roadmap_artifact_fingerprint=row.content_fingerprint,
                    supersedes_artifact_id=row.supersedes_roadmap_artifact_id,
                    status=self._phase_status(
                        None if decision is None else decision.decision,
                        superseded=artifact_id in superseded,
                    ),
                )
            )
        return tuple(facts)

    def _story_planning_facts(
        self,
        rows: _PlanningRows,
        indexes: _PlanningIndexes,
        decisions: _PlanningDecisionLoad,
    ) -> tuple[PlanningArtifactFact, ...]:
        superseded = {
            row.supersedes_story_artifact_id
            for row in rows.stories
            if row.supersedes_story_artifact_id is not None
        }
        facts: list[PlanningArtifactFact] = []
        for artifact_id, row in indexes.stories.items():
            self._canonical_object(
                row.canonical_content_json,
                row.content_fingerprint,
                "Story",
            )
            roadmap = indexes.roadmaps.get(row.roadmap_artifact_id)
            if (
                roadmap is None
                or roadmap.content_fingerprint != row.roadmap_artifact_fingerprint
            ):
                message = "Story artifact source Roadmap changed."
                raise self._error(message)
            backlog = indexes.backlogs.get(roadmap.backlog_artifact_id)
            if (
                backlog is None
                or backlog.content_fingerprint != roadmap.backlog_artifact_fingerprint
            ):
                message = "Story artifact source Backlog changed."
                raise self._error(message)
            story_ids = self._canonical_story_ids(
                row.story_ids_json,
                "Story artifact",
            )
            decision = decisions.stories.get(artifact_id)
            facts.append(
                PlanningArtifactFact(
                    artifact_type="story",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.content_fingerprint,
                    source_artifact_id=row.roadmap_artifact_id,
                    source_fingerprint=row.roadmap_artifact_fingerprint,
                    authority_id=backlog.authority_id,
                    authority_fingerprint=backlog.authority_fingerprint,
                    backlog_artifact_id=roadmap.backlog_artifact_id,
                    backlog_artifact_fingerprint=roadmap.backlog_artifact_fingerprint,
                    roadmap_artifact_id=row.roadmap_artifact_id,
                    roadmap_artifact_fingerprint=row.roadmap_artifact_fingerprint,
                    requirement_id=row.requirement_id,
                    story_ids=story_ids,
                    supersedes_artifact_id=row.supersedes_story_artifact_id,
                    status=self._phase_status(
                        None if decision is None else decision.decision,
                        superseded=artifact_id in superseded,
                    ),
                )
            )
        return tuple(facts)

    def _canonical_story_ids(self, raw_json: str, label: str) -> tuple[int, ...]:
        try:
            story_ids = tuple(_INT_LIST.validate_json(raw_json))
        except ValidationError as exc:
            message = f"{label} IDs are invalid."
            raise self._error(message) from exc
        if (
            story_ids != tuple(sorted(set(story_ids)))
            or canonical_json(list(story_ids)) != raw_json
        ):
            message = f"{label} IDs are not canonical."
            raise self._error(message)
        return story_ids

    def _sprint_planning_facts(
        self,
        rows: _PlanningRows,
        indexes: _PlanningIndexes,
        decisions: _PlanningDecisionLoad,
    ) -> tuple[PlanningArtifactFact, ...]:
        superseded = {
            row.supersedes_sprint_plan_artifact_id
            for row in rows.sprint_plans
            if row.supersedes_sprint_plan_artifact_id is not None
        }
        facts: list[PlanningArtifactFact] = []
        for artifact_id, row in indexes.sprint_plans.items():
            canonical_plan = self._canonical_object(
                row.canonical_task_plan_json,
                row.plan_fingerprint,
                "Sprint plan",
            )
            try:
                plan = SprintPlannerOutput.model_validate(canonical_plan)
            except ValidationError as exc:
                message = "Sprint plan task content is invalid."
                raise self._error(message) from exc
            story_ids = self._canonical_story_ids(
                row.selected_story_ids_json,
                "Sprint plan selected Story",
            )
            decision = decisions.sprint_plans.get(artifact_id)
            facts.append(
                PlanningArtifactFact(
                    artifact_type="sprint_plan",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.plan_fingerprint,
                    source_fingerprint=row.candidate_set_fingerprint,
                    story_ids=story_ids,
                    sprint_id=row.sprint_id,
                    candidate_set_fingerprint=row.candidate_set_fingerprint,
                    task_content_fingerprint=planned_task_content_fingerprint(plan),
                    supersedes_artifact_id=row.supersedes_sprint_plan_artifact_id,
                    status=self._phase_status(
                        None if decision is None else decision.decision,
                        superseded=artifact_id in superseded,
                    ),
                )
            )
        return tuple(facts)

    def _planning_artifacts(self, project_id: int) -> _PlanningArtifactLoad:
        """Load immutable planning artifacts and exact append-only decisions."""
        rows = self._planning_rows(project_id)
        indexes = self._planning_indexes(rows)
        decisions = self._planning_decisions(rows, indexes)
        facts = (
            *self._roadmap_planning_facts(rows, indexes, decisions),
            *self._story_planning_facts(rows, indexes, decisions),
            *self._sprint_planning_facts(rows, indexes, decisions),
        )
        return _PlanningArtifactLoad(
            facts=tuple(
                sorted(
                    facts,
                    key=lambda item: (item.artifact_type, item.artifact_id),
                )
            ),
            reviews=decisions.reviews,
        )

    def _review_decisions(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
        prd_versions: dict[int, tuple[int, str]],
        spec_drafts: dict[int, tuple[int, str]],
        authority_reviews: tuple[ReviewDecisionFact, ...],
    ) -> tuple[ReviewDecisionFact, ...]:
        decisions: list[ReviewDecisionFact] = list(authority_reviews)
        prd_rows = self._session.exec(
            select(PrdDecision)
            .where(col(PrdDecision.project_id) == project_id)
            .order_by(
                col(PrdDecision.decided_at),
                col(PrdDecision.prd_decision_id),
            ),
            execution_options=self._query_options(),
        ).all()
        for row in prd_rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "PRD decision",
            )
            self._require_artifact_parent(
                row.prd_version_id,
                row.discovery_run_id,
                row.artifact_fingerprint,
                prd_versions,
                "PRD decision",
            )
            decisions.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.prd_decision_id,
                            "PRD decision",
                        ),
                        artifact_type="prd",
                        artifact_id=row.prd_version_id,
                        artifact_fingerprint=row.artifact_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )
        draft_rows = self._session.exec(
            select(SpecDraftDecision)
            .where(col(SpecDraftDecision.project_id) == project_id)
            .order_by(
                col(SpecDraftDecision.decided_at),
                col(SpecDraftDecision.spec_draft_decision_id),
            ),
            execution_options=self._query_options(),
        ).all()
        for row in draft_rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "specification draft decision",
            )
            self._require_artifact_parent(
                row.spec_draft_id,
                row.discovery_run_id,
                row.artifact_fingerprint,
                spec_drafts,
                "specification draft decision",
            )
            decisions.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.spec_draft_decision_id,
                            "specification draft decision",
                        ),
                        artifact_type="spec_draft",
                        artifact_id=row.spec_draft_id,
                        artifact_fingerprint=row.artifact_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )
        return tuple(
            sorted(
                decisions,
                key=lambda item: (
                    item.decided_at,
                    item.artifact_type,
                    item.artifact_id,
                    item.decision_id,
                ),
            )
        )

    def _repository_baselines(
        self,
        project_id: int,
    ) -> tuple[RepositoryBaselineFact, ...]:
        rows = self._session.exec(
            select(RepositoryBaseline)
            .where(col(RepositoryBaseline.project_id) == project_id)
            .order_by(col(RepositoryBaseline.repository_baseline_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[RepositoryBaselineFact] = []
        for row in rows:
            expected_fingerprint = canonical_hash(
                {
                    "repository_path": row.repository_path,
                    "git_commit": row.git_commit,
                    "dirty": row.dirty,
                }
            )
            if row.content_fingerprint != expected_fingerprint:
                message = (
                    "Forced relationship corruption in repository baseline: "
                    f"baseline {row.repository_baseline_id} fingerprint mismatch."
                )
                raise self._error(message)
            facts.append(
                RepositoryBaselineFact(
                    repository_baseline_id=self._required_id(
                        row.repository_baseline_id,
                        "repository baseline",
                    ),
                    repository_path=row.repository_path,
                    git_commit=row.git_commit,
                    dirty=row.dirty,
                    content_fingerprint=row.content_fingerprint,
                )
            )
        return tuple(facts)

    def _repository_inventories(
        self,
        project_id: int,
        repository_baselines: dict[int, RepositoryBaselineFact],
    ) -> tuple[RepositoryInventoryFact, ...]:
        rows = self._session.exec(
            select(RepositoryInventory)
            .where(col(RepositoryInventory.project_id) == project_id)
            .order_by(col(RepositoryInventory.repository_inventory_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[RepositoryInventoryFact] = []
        for row in rows:
            baseline = repository_baselines.get(row.repository_baseline_id)
            if baseline is None:
                message = (
                    "Forced relationship corruption in repository inventory: "
                    f"inventory {row.repository_inventory_id} has no Project baseline."
                )
                raise self._error(message)
            try:
                payload = _JSON_OBJECT.validate_json(row.canonical_inventory_json)
                encoded_selected = _STRING_LIST.validate_json(
                    row.selected_for_model_json
                )
                selected = [decode_repository_path(path) for path in encoded_selected]
            except (ValidationError, ValueError) as exc:
                message = (
                    "Forced relationship corruption in repository inventory: "
                    f"inventory {row.repository_inventory_id} contains invalid JSON."
                )
                raise self._error(message) from exc
            if (
                canonical_json(payload) != row.canonical_inventory_json
                or canonical_json(encode_repository_paths(selected))
                != row.selected_for_model_json
                or inventory_binding_fingerprint(payload, selected)
                != row.content_fingerprint
            ):
                message = (
                    "Forced relationship corruption in repository inventory: "
                    f"inventory {row.repository_inventory_id} has a canonical "
                    "binding mismatch."
                )
                raise self._error(message)
            files = payload.get("files")
            total_bytes = payload.get("total_bytes")
            git_available = payload.get("git_available")
            commit = payload.get("commit")
            dirty = payload.get("dirty")
            truncated = payload.get("truncated")
            hashable_paths, measured_bytes = self._validate_inventory_files(
                files,
                inventory_id=row.repository_inventory_id,
            )
            if (
                set(payload)
                != {
                    "commit",
                    "dirty",
                    "files",
                    "git_available",
                    "total_bytes",
                    "truncated",
                }
                or not isinstance(git_available, bool)
                or (commit is not None and not isinstance(commit, str))
                or not isinstance(dirty, bool)
                or truncated is not False
                or commit != baseline.git_commit
                or dirty != baseline.dirty
                or (not git_available and commit is not None)
                or not isinstance(files, list)
                or isinstance(total_bytes, bool)
                or not isinstance(total_bytes, int)
                or len(files) != row.file_count
                or total_bytes != row.total_bytes
                or measured_bytes != row.total_bytes
                or any(path not in hashable_paths for path in selected)
                or len(selected) != len(set(selected))
            ):
                message = (
                    "Forced relationship corruption in repository inventory: "
                    f"inventory {row.repository_inventory_id} summary mismatch."
                )
                raise self._error(message)
            facts.append(
                RepositoryInventoryFact(
                    repository_inventory_id=self._required_id(
                        row.repository_inventory_id,
                        "repository inventory",
                    ),
                    repository_baseline_id=row.repository_baseline_id,
                    content_fingerprint=row.content_fingerprint,
                    file_count=row.file_count,
                    total_bytes=row.total_bytes,
                    selected_for_model=tuple(selected),
                )
            )
        return tuple(facts)

    def _validate_inventory_files(
        self,
        files: JsonValue | None,
        *,
        inventory_id: int | None,
    ) -> tuple[frozenset[str], int]:
        if not isinstance(files, list):
            message = (
                "Forced relationship corruption in repository inventory: "
                f"inventory {inventory_id} has no file list."
            )
            raise self._error(message)
        hashable_paths: set[str] = set()
        previous_path: bytes | None = None
        measured_bytes = 0
        for item in files:
            if not isinstance(item, dict):
                raise self._inventory_entry_error(inventory_id)
            path = item.get("path")
            size_bytes = item.get("size_bytes")
            digest = item.get("sha256")
            status = item.get("content_status")
            if (
                set(item) != {"content_status", "path", "sha256", "size_bytes"}
                or not isinstance(path, str)
                or not path
                or isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes < 0
                or status not in {"hashable", "secret", "oversized", "symlink"}
                or (status == "hashable") != isinstance(digest, str)
                or (digest is not None and not isinstance(digest, str))
            ):
                raise self._inventory_entry_error(inventory_id)
            try:
                decoded_path = decode_repository_path(path)
            except ValueError as exc:
                raise self._inventory_entry_error(inventory_id) from exc
            encoded_path = repository_path_bytes(decoded_path)
            if previous_path is not None and encoded_path <= previous_path:
                raise self._inventory_entry_error(inventory_id)
            previous_path = encoded_path
            measured_bytes += size_bytes
            if status == "hashable":
                hashable_paths.add(decoded_path)
        return frozenset(hashable_paths), measured_bytes

    def _inventory_entry_error(self, inventory_id: int | None) -> WorkflowFactLoadError:
        message = (
            "Forced relationship corruption in repository inventory: "
            f"inventory {inventory_id} contains an invalid entry."
        )
        return self._error(message)

    def _initial_registrations(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
        spec_drafts: dict[int, int],
        spec_versions: dict[int, str],
    ) -> tuple[InitialScopeRegistrationFact, ...]:
        rows = self._session.exec(
            select(InitialScopeRegistration)
            .where(col(InitialScopeRegistration.project_id) == project_id)
            .order_by(col(InitialScopeRegistration.initial_scope_registration_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[InitialScopeRegistrationFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "initial-scope registration",
            )
            self._require_same_run_reference(
                row.spec_draft_id,
                row.discovery_run_id,
                spec_drafts,
                "initial-scope registration",
            )
            self._require_fingerprint_reference(
                row.spec_version_id,
                row.spec_hash,
                spec_versions,
                "initial-scope registration",
            )
            facts.append(
                InitialScopeRegistrationFact(
                    registration_id=self._required_id(
                        row.initial_scope_registration_id,
                        "initial-scope registration",
                    ),
                    discovery_run_id=row.discovery_run_id,
                    spec_draft_id=row.spec_draft_id,
                    spec_version_id=row.spec_version_id,
                    spec_hash=row.spec_hash,
                )
            )
        return tuple(facts)

    def _extension_registrations(
        self,
        project_id: int,
        discovery_runs: tuple[DiscoveryRunFact, ...],
        spec_drafts: tuple[SpecDraftFact, ...],
        spec_versions: dict[int, str],
    ) -> tuple[ScopeExtensionRegistrationFact, ...]:
        rows = self._session.exec(
            select(ScopeExtensionRegistration)
            .where(col(ScopeExtensionRegistration.project_id) == project_id)
            .order_by(col(ScopeExtensionRegistration.scope_extension_registration_id)),
            execution_options=self._query_options(),
        ).all()
        runs = {item.discovery_run_id: item for item in discovery_runs}
        drafts = {item.spec_draft_id: item for item in spec_drafts}
        facts: list[ScopeExtensionRegistrationFact] = []
        for row in rows:
            run = runs.get(row.discovery_run_id)
            draft = drafts.get(row.spec_draft_id)
            if (
                run is None
                or run.purpose != "extension"
                or draft is None
                or draft.discovery_run_id != row.discovery_run_id
                or draft.kind != "amendment"
            ):
                message = "Scope-extension registration relationship is invalid."
                raise self._error(message)
            self._require_fingerprint_reference(
                row.spec_version_id,
                row.spec_hash,
                spec_versions,
                "scope-extension registration",
            )
            facts.append(
                ScopeExtensionRegistrationFact(
                    registration_id=self._required_id(
                        row.scope_extension_registration_id,
                        "scope-extension registration",
                    ),
                    discovery_run_id=row.discovery_run_id,
                    spec_draft_id=row.spec_draft_id,
                    spec_version_id=row.spec_version_id,
                    spec_hash=row.spec_hash,
                )
            )
        return tuple(facts)

    def _scope_extension_reconciliations(
        self,
        project_id: int,
        discovery_runs: tuple[DiscoveryRunFact, ...],
        sources: _ScopeReconciliationSources,
    ) -> tuple[ScopeExtensionReconciliationFact, ...]:
        rows = self._session.exec(
            select(ScopeExtensionReconciliation)
            .where(col(ScopeExtensionReconciliation.project_id) == project_id)
            .order_by(
                col(ScopeExtensionReconciliation.scope_extension_reconciliation_id)
            ),
            execution_options=self._query_options(),
        ).all()
        runs = {item.discovery_run_id: item for item in discovery_runs}
        reference_index = self._scope_reconciliation_reference_index(sources)
        facts: list[ScopeExtensionReconciliationFact] = []
        for row in rows:
            self._validate_scope_reconciliation_relationship(
                project_id,
                row,
                runs,
                sources,
            )
            references = self._load_scope_reconciliation_references(
                project_id,
                row,
                reference_index,
            )
            facts.append(
                ScopeExtensionReconciliationFact(
                    reconciliation_id=self._required_id(
                        row.scope_extension_reconciliation_id,
                        "scope-extension reconciliation",
                    ),
                    discovery_run_id=row.discovery_run_id,
                    replacement_authority_id=row.replacement_authority_id,
                    replacement_authority_fingerprint=(
                        row.replacement_authority_fingerprint
                    ),
                    artifact_references=references,
                    reconciled_at=row.reconciled_at,
                )
            )
        return tuple(facts)

    @staticmethod
    def _scope_reconciliation_reference_index(
        sources: _ScopeReconciliationSources,
    ) -> _ScopeReconciliationReferenceIndex:
        phase: dict[tuple[str, str, str], str] = {}
        for item in sources.phase_artifacts:
            if item.artifact_type in {"vision", "backlog"}:
                phase[
                    (
                        item.artifact_type,
                        str(item.artifact_id),
                        item.artifact_fingerprint,
                    )
                ] = item.status
        planning: dict[tuple[str, str, str], str] = {}
        for item in sources.planning_artifacts:
            planning[
                (
                    item.artifact_type,
                    str(item.artifact_id),
                    item.artifact_fingerprint,
                )
            ] = item.status
        return _ScopeReconciliationReferenceIndex(
            phase=phase,
            planning=planning,
            dependency_reviews=frozenset(
                (
                    str(item.review_id),
                    canonical_hash(item.model_dump(mode="json")),
                )
                for item in sources.dependency_reviews
            ),
            sprint_starts=frozenset(
                (
                    str(item.start_id),
                    canonical_hash(item.model_dump(mode="json")),
                )
                for item in sources.sprint_starts
            ),
        )

    def _validate_scope_reconciliation_relationship(
        self,
        project_id: int,
        row: ScopeExtensionReconciliation,
        runs: dict[int, DiscoveryRunFact],
        sources: _ScopeReconciliationSources,
    ) -> None:
        run = runs.get(row.discovery_run_id)
        registration = next(
            (
                item
                for item in sources.registrations
                if item.discovery_run_id == row.discovery_run_id
            ),
            None,
        )
        authority = next(
            (
                item
                for item in sources.authorities
                if item.authority_id == row.replacement_authority_id
            ),
            None,
        )
        if (
            run is None
            or run.purpose != "extension"
            or run.closed_at is None
            or registration is None
            or authority is None
            or authority.spec_version_id != registration.spec_version_id
            or authority.authority_fingerprint != row.replacement_authority_fingerprint
        ):
            message = "Scope-extension reconciliation relationship is invalid."
            raise self._error(message)
        acceptances = self._session.exec(
            select(SpecAuthorityAcceptance).where(
                col(SpecAuthorityAcceptance.project_id) == project_id,
                col(SpecAuthorityAcceptance.spec_version_id)
                == registration.spec_version_id,
                col(SpecAuthorityAcceptance.pending_authority_id)
                == row.replacement_authority_id,
                col(SpecAuthorityAcceptance.authority_fingerprint)
                == row.replacement_authority_fingerprint,
                col(SpecAuthorityAcceptance.status) == "accepted",
            ),
            execution_options=self._query_options(),
        ).all()
        if len(acceptances) != 1:
            message = "Scope-extension reconciliation relationship is invalid."
            raise self._error(message)

    def _load_scope_reconciliation_references(
        self,
        project_id: int,
        row: ScopeExtensionReconciliation,
        index: _ScopeReconciliationReferenceIndex,
    ) -> tuple[FactReference, ...]:
        try:
            references = tuple(
                _FACT_REFERENCE_LIST.validate_json(row.artifact_references_json)
            )
        except ValidationError as error:
            message = "Scope-extension reconciliation references are invalid."
            raise self._error(message) from error
        payload = [item.model_dump(mode="json") for item in references]
        if any(not item.fact_id.isdigit() for item in references):
            message = "Scope-extension reconciliation references are invalid."
            raise self._error(message)
        canonical_references = tuple(
            sorted(
                set(references),
                key=lambda item: (item.fact_type, int(item.fact_id)),
            )
        )
        if (
            row.artifact_references_json != canonical_json(payload)
            or row.artifact_references_fingerprint != canonical_hash(payload)
            or references != canonical_references
        ):
            message = "Scope-extension reconciliation references changed."
            raise self._error(message)
        self._validate_scope_reconciliation_reference_set(
            project_id,
            references,
            index,
        )
        return references

    def _validate_scope_reconciliation_reference_set(
        self,
        project_id: int,
        references: tuple[FactReference, ...],
        index: _ScopeReconciliationReferenceIndex,
    ) -> None:
        reference_types = tuple(item.fact_type for item in references)
        required_singletons = {
            "vision",
            "backlog",
            "roadmap",
            "sprint_plan",
            "story_readiness",
            "sprint_start",
            "scope_downstream_state",
        }
        invalid_shape = (
            any(reference_types.count(item) != 1 for item in required_singletons)
            or reference_types.count("story") < 1
            or reference_types.count("story_dependency_review") > 1
            or any(not item.fingerprint.startswith("sha256:") for item in references)
        )
        if invalid_shape:
            message = "Scope-extension reconciliation references are invalid."
            raise self._error(message)
        if any(
            not self._scope_reconciliation_reference_is_valid(
                project_id,
                reference,
                index,
            )
            for reference in references
        ):
            message = (
                "Scope-extension reconciliation reference relationship is invalid."
            )
            raise self._error(message)

    @staticmethod
    def _scope_reconciliation_reference_is_valid(
        project_id: int,
        reference: FactReference,
        index: _ScopeReconciliationReferenceIndex,
    ) -> bool:
        key = (
            reference.fact_type,
            reference.fact_id,
            reference.fingerprint,
        )
        if reference.fact_type in {"vision", "backlog"}:
            return index.phase.get(key) in {"accepted", "superseded"}
        if reference.fact_type in {"roadmap", "story", "sprint_plan"}:
            return index.planning.get(key) in {"accepted", "superseded"}
        if reference.fact_type == "story_dependency_review":
            return (
                reference.fact_id,
                reference.fingerprint,
            ) in index.dependency_reviews
        if reference.fact_type == "sprint_start":
            return (
                reference.fact_id,
                reference.fingerprint,
            ) in index.sprint_starts
        return reference.fact_type in {
            "story_readiness",
            "scope_downstream_state",
        } and reference.fact_id == str(project_id)

    def _authorities(
        self,
        project_id: int,
        spec_versions: dict[int, str],
    ) -> _AuthorityLoad:
        rows = self._session.exec(
            select(CompiledSpecAuthority, SpecRegistry)
            .join(
                SpecRegistry,
                col(CompiledSpecAuthority.spec_version_id)
                == col(SpecRegistry.spec_version_id),
            )
            .where(col(SpecRegistry.project_id) == project_id)
            .order_by(col(CompiledSpecAuthority.authority_id)),
            execution_options=self._query_options(),
        ).all()
        authority_records: list[
            tuple[int, CompiledSpecAuthority, SpecRegistry, str]
        ] = []
        authorities_by_id: dict[
            int,
            tuple[CompiledSpecAuthority, SpecRegistry, str],
        ] = {}
        for authority, spec in rows:
            authority_id = self._required_id(authority.authority_id, "authority")
            self._require_fingerprint_reference(
                authority.spec_version_id,
                spec.spec_hash,
                spec_versions,
                "authority specification",
            )
            self._validate_authority_json(
                authority.compiled_artifact_json,
                authority_id,
                authority.compiler_version,
                authority.prompt_hash,
            )
            authority_fingerprint = pending_authority_fingerprint(authority)
            if authority_fingerprint is None:
                message = (
                    "Forced relationship corruption in authority: "
                    f"compiled authority {authority_id} has no fingerprint."
                )
                raise self._error(message)
            record = (authority, spec, authority_fingerprint)
            authorities_by_id[authority_id] = record
            authority_records.append((authority_id, *record))

        acceptance_rows = self._session.exec(
            select(SpecAuthorityAcceptance)
            .where(col(SpecAuthorityAcceptance.project_id) == project_id)
            .order_by(
                col(SpecAuthorityAcceptance.decided_at),
                col(SpecAuthorityAcceptance.id),
            ),
            execution_options=self._query_options(),
        ).all()
        acceptances = tuple(
            self._authority_acceptance_source(
                row,
                spec_versions,
                authorities_by_id,
            )
            for row in acceptance_rows
        )
        facts: list[AuthorityFact] = []
        for authority_id, authority, spec, authority_fingerprint in authority_records:
            acceptance = self._latest_acceptance(authority_id, acceptances)
            status, decided_at = self._authority_state(
                spec.status,
                acceptance,
            )
            facts.append(
                AuthorityFact(
                    authority_id=authority_id,
                    spec_version_id=authority.spec_version_id,
                    authority_fingerprint=authority_fingerprint,
                    status=status,
                    decided_at=decided_at,
                )
            )
        reviews = tuple(
            self._review_decision_fact(
                _ReviewDecisionSource(
                    decision_id=item.decision_id,
                    artifact_type="authority",
                    artifact_id=item.authority_id,
                    artifact_fingerprint=item.authority_fingerprint,
                    decision=item.status,
                    decided_at=item.decided_at,
                )
            )
            for item in acceptances
        )
        return _AuthorityLoad(facts=tuple(facts), reviews=reviews)

    def _sprints(self, project_id: int) -> tuple[SprintFact, ...]:
        rows = self._session.exec(
            select(Sprint)
            .where(col(Sprint.project_id) == project_id)
            .order_by(col(Sprint.completed_at), col(Sprint.sprint_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[SprintFact] = []
        for row in rows:
            status = _SPRINT_STATUSES.get(row.status)
            if status is None:
                message = f"Sprint {row.sprint_id} has invalid status {row.status!r}."
                raise self._error(message)
            facts.append(
                SprintFact(
                    sprint_id=self._required_id(row.sprint_id, "sprint"),
                    status=status,
                    completed_at=row.completed_at,
                )
            )
        return tuple(facts)

    def _sprint_starts(
        self,
        project_id: int,
        sprints: tuple[SprintFact, ...],
        planning_artifacts: tuple[PlanningArtifactFact, ...],
        dependency_reviews: tuple[StoryDependencyReviewFact, ...],
    ) -> tuple[SprintStartFact, ...]:
        sprint_ids = frozenset(item.sprint_id for item in sprints)
        linked_rows = (
            self._session.exec(
                select(SprintStart)
                .where(col(SprintStart.sprint_id).in_(sprint_ids))
                .order_by(col(SprintStart.sprint_start_id)),
                execution_options=self._query_options(),
            ).all()
            if sprint_ids
            else []
        )
        if any(row.project_id != project_id for row in linked_rows):
            message = "Sprint start lineage crosses Project ownership."
            raise self._error(message)
        rows = self._session.exec(
            select(SprintStart)
            .where(col(SprintStart.project_id) == project_id)
            .order_by(col(SprintStart.sprint_start_id)),
            execution_options=self._query_options(),
        ).all()
        plans = {
            item.artifact_id: item
            for item in planning_artifacts
            if item.artifact_type == "sprint_plan"
        }
        reviews = {item.review_id: item for item in dependency_reviews}
        facts: list[SprintStartFact] = []
        for row in rows:
            self._require_member(row.sprint_id, sprint_ids, "Sprint start")
            sprint = self._session.get(Sprint, row.sprint_id)
            plan = plans.get(row.sprint_plan_artifact_id)
            decision = self._session.get(
                SprintPlanArtifactDecision,
                row.sprint_plan_artifact_decision_id,
            )
            dependency_review = reviews.get(row.story_dependency_review_id)
            dependency_review_row = self._session.get(
                StoryDependencyReview,
                row.story_dependency_review_id,
            )
            event = self._session.get(WorkflowEvent, row.audit_event_id)
            try:
                selected_story_ids = tuple(
                    _INT_LIST.validate_json(row.selected_story_ids_json)
                )
            except ValidationError as exc:
                message = "Sprint start selected Story IDs are invalid."
                raise self._error(message) from exc
            if (
                selected_story_ids != tuple(sorted(set(selected_story_ids)))
                or canonical_json(list(selected_story_ids))
                != row.selected_story_ids_json
            ):
                message = "Sprint start selected Story IDs are not canonical."
                raise self._error(message)
            if (
                sprint is None
                or sprint.project_id != project_id
                or sprint.status not in {SprintStatus.ACTIVE, SprintStatus.COMPLETED}
                or sprint.started_at != row.started_at
                or plan is None
                or plan.status not in {"accepted", "superseded"}
                or plan.sprint_id != row.sprint_id
                or plan.artifact_fingerprint != row.plan_fingerprint
                or plan.candidate_set_fingerprint != row.candidate_set_fingerprint
                or plan.story_ids != selected_story_ids
                or plan.task_content_fingerprint != row.task_content_fingerprint
                or decision is None
                or decision.project_id != project_id
                or decision.sprint_plan_artifact_id != row.sprint_plan_artifact_id
                or decision.plan_fingerprint != row.plan_fingerprint
                or decision.decision != "accepted"
                or dependency_review is None
                or dependency_review_row is None
                or dependency_review_row.project_id != project_id
                or dependency_review.source_fingerprint
                != row.dependency_source_fingerprint
                or dependency_review.dependency_fingerprint
                != row.dependency_fingerprint
                or event is None
                or event.event_type is not WorkflowEventType.SPRINT_STARTED
                or event.project_id != project_id
                or event.sprint_id != row.sprint_id
                or event.timestamp != row.started_at
                or event.duration_seconds != 0.0
                or event.event_metadata is None
            ):
                message = "Sprint start accepted-plan or audit lineage changed."
                raise self._error(message)
            expected_metadata = sprint_start_audit_metadata(
                SprintStartAudit(
                    sprint_id=row.sprint_id,
                    team_id=sprint.team_id,
                    sprint_plan_artifact_id=row.sprint_plan_artifact_id,
                    sprint_plan_artifact_decision_id=(
                        row.sprint_plan_artifact_decision_id
                    ),
                    story_dependency_review_id=row.story_dependency_review_id,
                    plan_fingerprint=row.plan_fingerprint,
                    candidate_set_fingerprint=row.candidate_set_fingerprint,
                    selected_story_ids=selected_story_ids,
                    task_content_fingerprint=row.task_content_fingerprint,
                    dependency_source_fingerprint=(row.dependency_source_fingerprint),
                    dependency_fingerprint=row.dependency_fingerprint,
                    dependency_rows_fingerprint=row.dependency_rows_fingerprint,
                    decision_fingerprint=row.decision_fingerprint,
                    started_by=row.started_by,
                )
            )
            if event.event_metadata != canonical_json(expected_metadata):
                message = "Sprint start audit metadata changed."
                raise self._error(message)
            facts.append(
                SprintStartFact(
                    start_id=self._required_id(row.sprint_start_id, "Sprint start"),
                    sprint_id=row.sprint_id,
                    sprint_plan_artifact_id=row.sprint_plan_artifact_id,
                    sprint_plan_artifact_decision_id=(
                        row.sprint_plan_artifact_decision_id
                    ),
                    story_dependency_review_id=row.story_dependency_review_id,
                    plan_fingerprint=row.plan_fingerprint,
                    candidate_set_fingerprint=row.candidate_set_fingerprint,
                    selected_story_ids=selected_story_ids,
                    task_content_fingerprint=row.task_content_fingerprint,
                    dependency_source_fingerprint=(row.dependency_source_fingerprint),
                    dependency_fingerprint=row.dependency_fingerprint,
                    dependency_rows_fingerprint=row.dependency_rows_fingerprint,
                    decision_fingerprint=row.decision_fingerprint,
                    audit_event_id=row.audit_event_id,
                    audit_event_fingerprint=canonical_hash(expected_metadata),
                    started_by=row.started_by,
                    started_at=row.started_at,
                )
            )
        return tuple(facts)

    def _accepted_story_artifacts(
        self,
        planning_artifacts: tuple[PlanningArtifactFact, ...],
    ) -> dict[int, PlanningArtifactFact]:
        accepted_content_by_story: dict[int, PlanningArtifactFact] = {}
        for artifact in planning_artifacts:
            if artifact.artifact_type != "story" or artifact.status != "accepted":
                continue
            if artifact.requirement_id is None:
                message = "Accepted Story artifact has no requirement identity."
                raise self._error(message)
            for story_id in artifact.story_ids:
                if story_id in accepted_content_by_story:
                    message = "Story row belongs to multiple accepted artifacts."
                    raise self._error(message)
                accepted_content_by_story[story_id] = artifact
        return accepted_content_by_story

    def _validate_story_relationships(
        self,
        rows: tuple[UserStory, ...],
        dependencies: tuple[UserStoryDependency, ...],
        story_ids: frozenset[int],
        spec_version_ids: frozenset[int],
    ) -> None:
        for row in rows:
            if row.accepted_spec_version_id is not None:
                self._require_member(
                    row.accepted_spec_version_id,
                    spec_version_ids,
                    "story accepted specification",
                )
            if row.superseded_by_story_id is not None:
                self._require_member(
                    row.superseded_by_story_id,
                    story_ids,
                    "story supersession",
                )
        for dependency in dependencies:
            self._require_member(
                dependency.dependent_story_id,
                story_ids,
                "story dependency",
            )
            self._require_member(
                dependency.prerequisite_story_id,
                story_ids,
                "story dependency",
            )

    @staticmethod
    def _story_fact(
        row: UserStory,
        story_id: int,
        artifact: PlanningArtifactFact | None,
        blockers: tuple[str, ...],
        sprint_ids: tuple[int, ...],
    ) -> StoryFact:
        return StoryFact(
            story_id=story_id,
            requirement_id=(
                artifact.requirement_id
                if artifact is not None
                else row.source_requirement
            ),
            content_fingerprint=(
                artifact.artifact_fingerprint if artifact is not None else None
            ),
            content_accepted=artifact is not None,
            story_artifact_id=(artifact.artifact_id if artifact is not None else None),
            authority_id=(artifact.authority_id if artifact is not None else None),
            authority_fingerprint=(
                artifact.authority_fingerprint if artifact is not None else None
            ),
            backlog_artifact_id=(
                artifact.backlog_artifact_id if artifact is not None else None
            ),
            backlog_artifact_fingerprint=(
                artifact.backlog_artifact_fingerprint if artifact is not None else None
            ),
            roadmap_artifact_id=(
                artifact.roadmap_artifact_id if artifact is not None else None
            ),
            roadmap_artifact_fingerprint=(
                artifact.roadmap_artifact_fingerprint if artifact is not None else None
            ),
            status=row.status.value,
            story_points=row.story_points,
            rank=row.rank,
            sprint_ids=sprint_ids,
            sprint_candidate=not blockers,
            readiness_blockers=blockers,
        )

    def _stories(
        self,
        project_id: int,
        spec_version_ids: frozenset[int],
        planning_artifacts: tuple[PlanningArtifactFact, ...],
        sprint_ids: frozenset[int],
    ) -> tuple[StoryFact, ...]:
        rows = tuple(
            self._session.exec(
                select(UserStory)
                .where(col(UserStory.project_id) == project_id)
                .order_by(col(UserStory.rank), col(UserStory.story_id)),
                execution_options=self._query_options(),
            ).all()
        )
        dependencies = tuple(
            self._session.exec(
                select(UserStoryDependency)
                .where(col(UserStoryDependency.project_id) == project_id)
                .order_by(
                    col(UserStoryDependency.dependent_story_id),
                    col(UserStoryDependency.prerequisite_story_id),
                    col(UserStoryDependency.dependency_id),
                ),
                execution_options=self._query_options(),
            ).all()
        )
        stories_by_id = {self._required_id(row.story_id, "story"): row for row in rows}
        story_ids = frozenset(stories_by_id)
        memberships = (
            self._session.exec(
                select(SprintStory)
                .where(col(SprintStory.story_id).in_(story_ids))
                .order_by(col(SprintStory.story_id), col(SprintStory.sprint_id)),
                execution_options=self._query_options(),
            ).all()
            if story_ids
            else []
        )
        sprint_ids_by_story: dict[int, list[int]] = {
            story_id: [] for story_id in story_ids
        }
        for membership in memberships:
            self._require_member(
                membership.sprint_id,
                sprint_ids,
                "task sprint relationship",
            )
            sprint_ids_by_story[membership.story_id].append(membership.sprint_id)
        accepted_content_by_story = self._accepted_story_artifacts(planning_artifacts)
        self._validate_story_relationships(
            rows,
            dependencies,
            story_ids,
            spec_version_ids,
        )
        blockers = self._story_readiness_blockers(rows, dependencies, stories_by_id)
        return tuple(
            self._story_fact(
                row,
                story_id,
                accepted_content_by_story.get(story_id),
                blockers[story_id],
                tuple(sprint_ids_by_story[story_id]),
            )
            for story_id, row in stories_by_id.items()
        )

    def _story_dependencies(
        self,
        project_id: int,
    ) -> tuple[StoryDependencyFact, ...]:
        rows = self._session.exec(
            select(UserStoryDependency)
            .where(col(UserStoryDependency.project_id) == project_id)
            .order_by(
                col(UserStoryDependency.dependent_story_id),
                col(UserStoryDependency.prerequisite_story_id),
                col(UserStoryDependency.dependency_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[StoryDependencyFact] = []
        for row in rows:
            if row.status not in {"proposed", "active", "rejected"}:
                message = f"Story dependency {row.dependency_id} has invalid status."
                raise self._error(message)
            if row.source not in {"story_writer", "dependency_repair", "manual_review"}:
                message = f"Story dependency {row.dependency_id} has invalid source."
                raise self._error(message)
            if row.confidence not in {"explicit", "inferred", "reviewed"}:
                message = (
                    f"Story dependency {row.dependency_id} has invalid confidence."
                )
                raise self._error(message)
            facts.append(
                StoryDependencyFact.model_validate(
                    {
                        "dependency_id": self._required_id(
                            row.dependency_id,
                            "story dependency",
                        ),
                        "dependent_story_id": row.dependent_story_id,
                        "prerequisite_story_id": row.prerequisite_story_id,
                        "status": row.status,
                        "source": row.source,
                        "confidence": row.confidence,
                        "reason": row.reason,
                    }
                )
            )
        return tuple(facts)

    def _story_dependency_reviews(
        self,
        project_id: int,
        stories: tuple[StoryFact, ...],
    ) -> tuple[StoryDependencyReviewFact, ...]:
        rows = self._session.exec(
            select(StoryDependencyReview)
            .where(col(StoryDependencyReview.project_id) == project_id)
            .order_by(col(StoryDependencyReview.story_dependency_review_id)),
            execution_options=self._query_options(),
        ).all()
        project_story_ids = frozenset(item.story_id for item in stories)
        facts: list[StoryDependencyReviewFact] = []
        for row in rows:
            try:
                story_ids = tuple(_INT_LIST.validate_json(row.selected_story_ids_json))
            except ValidationError as exc:
                message = "Story dependency review IDs are invalid."
                raise self._error(message) from exc
            if (
                story_ids != tuple(sorted(set(story_ids)))
                or canonical_json(list(story_ids)) != row.selected_story_ids_json
            ):
                message = "Story dependency review IDs are not canonical."
                raise self._error(message)
            if not story_ids or any(
                story_id not in project_story_ids for story_id in story_ids
            ):
                message = "Story dependency review contains unknown Project Stories."
                raise self._error(message)
            try:
                reviewed_edges = tuple(
                    _DEPENDENCY_EDGE_LIST.validate_json(row.reviewed_edges_json)
                )
            except ValidationError as exc:
                message = "Story dependency review edges are invalid."
                raise self._error(message) from exc
            if (
                not dependency_edges_are_canonical(reviewed_edges)
                or canonical_json(dependency_edges_payload(reviewed_edges))
                != row.reviewed_edges_json
            ):
                message = "Story dependency review edges are not canonical."
                raise self._error(message)
            selected = set(story_ids)
            if any(
                edge.dependent_story_id not in selected
                or edge.prerequisite_story_id not in selected
                for edge in reviewed_edges
            ):
                message = "Story dependency review edges leave the selected set."
                raise self._error(message)
            if dependency_edges_have_cycle(reviewed_edges):
                message = "Story dependency review edges contain a semantic cycle."
                raise self._error(message)
            if (
                dependency_review_fingerprint(reviewed_edges)
                != row.dependency_fingerprint
            ):
                message = "Story dependency review fingerprint changed."
                raise self._error(message)
            facts.append(
                StoryDependencyReviewFact(
                    review_id=self._required_id(
                        row.story_dependency_review_id,
                        "story dependency review",
                    ),
                    selected_story_ids=story_ids,
                    reviewed_edges=reviewed_edges,
                    source_fingerprint=row.source_fingerprint,
                    dependency_fingerprint=row.dependency_fingerprint,
                )
            )
        return tuple(facts)

    def _tasks(
        self,
        project_id: int,
        sprint_ids: frozenset[int],
        stories: tuple[StoryFact, ...],
    ) -> tuple[TaskFact, ...]:
        rows = self._session.exec(
            select(Task, UserStory)
            .join(UserStory, col(Task.story_id) == col(UserStory.story_id))
            .where(col(UserStory.project_id) == project_id)
            .order_by(col(Task.task_id)),
            execution_options=self._query_options(),
        ).all()
        stories_by_id = {item.story_id: item for item in stories}
        story_ids = frozenset(stories_by_id)
        memberships = (
            self._session.exec(
                select(SprintStory)
                .where(col(SprintStory.story_id).in_(story_ids))
                .order_by(col(SprintStory.sprint_id), col(SprintStory.story_id)),
                execution_options=self._query_options(),
            ).all()
            if story_ids
            else []
        )
        sprint_ids_by_story: dict[int, list[int]] = {
            story_id: [] for story_id in story_ids
        }
        for membership in memberships:
            self._require_member(
                membership.sprint_id,
                sprint_ids,
                "task sprint relationship",
            )
            sprint_ids_by_story[membership.story_id].append(membership.sprint_id)
        facts: list[TaskFact] = []
        for task, _story in rows:
            task_id = self._required_id(task.task_id, "task")
            if not task.description.strip():
                message = f"Task {task_id} has an empty description."
                raise self._error(message)
            if task.metadata_json is None:
                message = f"Task {task_id} has no canonical metadata."
                raise self._error(message)
            try:
                metadata = TaskMetadata.model_validate_json(task.metadata_json)
            except ValidationError as exc:
                message = f"Task {task_id} metadata is invalid."
                raise self._error(message) from exc
            canonical_metadata = serialize_task_metadata(metadata)
            if canonical_metadata != task.metadata_json:
                message = f"Task {task_id} metadata is not canonical."
                raise self._error(message)
            task_sprint_ids = sprint_ids_by_story[task.story_id]
            if not task_sprint_ids:
                message = (
                    "Forced relationship corruption in task sprint relationship: "
                    f"task {task_id} has no sprint membership."
                )
                raise self._error(message)
            story = stories_by_id[task.story_id]
            facts.extend(
                TaskFact(
                    task_id=task_id,
                    sprint_id=sprint_id,
                    story_id=task.story_id,
                    description=task.description,
                    metadata_json=canonical_metadata,
                    status=task.status.value,
                    dependencies_satisfied=not story.readiness_blockers,
                )
                for sprint_id in task_sprint_ids
            )
        return tuple(sorted(facts, key=lambda item: (item.sprint_id, item.task_id)))

    def _task_completions(
        self,
        project_id: int,
        snapshot: WorkflowFactSnapshot,
    ) -> tuple[TaskCompletionFact, ...]:
        rows = self._session.exec(
            select(TaskCompletionEvidence)
            .where(col(TaskCompletionEvidence.project_id) == project_id)
            .order_by(col(TaskCompletionEvidence.task_completion_evidence_id)),
            execution_options=self._query_options(),
        ).all()
        tasks_by_key = {(item.sprint_id, item.task_id): item for item in snapshot.tasks}
        facts: list[TaskCompletionFact] = []
        for row in rows:
            key = (row.sprint_id, row.task_id)
            task = tasks_by_key.get(key)
            if task is None:
                message = "Task completion evidence targets a cross-Project task."
                raise self._error(message)
            try:
                artifact_refs = tuple(
                    _STRING_LIST.validate_json(row.artifact_refs_json)
                )
                checklist_result = _JSON_OBJECT.validate_json(row.checklist_result_json)
            except ValidationError as exc:
                message = "Task completion evidence JSON is invalid."
                raise self._error(message) from exc
            if (
                artifact_refs != tuple(sorted(set(artifact_refs)))
                or canonical_json(list(artifact_refs)) != row.artifact_refs_json
                or canonical_json(checklist_result) != row.checklist_result_json
                or row.acceptance_result not in {"partially_met", "fully_met"}
            ):
                message = "Task completion evidence is not canonical."
                raise self._error(message)
            fact = TaskCompletionFact.model_validate(
                {
                    "completion_id": self._required_id(
                        row.task_completion_evidence_id,
                        "task completion evidence",
                    ),
                    "task_id": row.task_id,
                    "sprint_id": row.sprint_id,
                    "outcome_summary": row.outcome_summary,
                    "artifact_refs": artifact_refs,
                    "acceptance_result": row.acceptance_result,
                    "checklist_result": checklist_result,
                    "evidence_fingerprint": row.evidence_fingerprint,
                }
            )
            try:
                expected = task_evidence_fingerprint(
                    snapshot,
                    task,
                    evidence=TaskEvidencePayload(
                        outcome_summary=fact.outcome_summary,
                        artifact_refs=fact.artifact_refs,
                        acceptance_result=fact.acceptance_result,
                        checklist_result=fact.checklist_result,
                    ),
                )
            except ExecutionIntegrityError as exc:
                message = "Task completion execution contract changed."
                raise self._error(message) from exc
            if expected != fact.evidence_fingerprint:
                message = "Task completion evidence fingerprint changed."
                raise self._error(message)
            facts.append(fact)
        return tuple(facts)

    def _story_completions(
        self,
        project_id: int,
        snapshot: WorkflowFactSnapshot,
    ) -> tuple[StoryCompletionFact, ...]:
        rows = self._session.exec(
            select(StoryClosure)
            .where(col(StoryClosure.project_id) == project_id)
            .order_by(col(StoryClosure.story_closure_id)),
            execution_options=self._query_options(),
        ).all()
        stories_by_id = {item.story_id: item for item in snapshot.stories}
        facts: list[StoryCompletionFact] = []
        for row in rows:
            story = stories_by_id.get(row.story_id)
            if story is None or row.sprint_id not in story.sprint_ids:
                message = "Story closure targets a cross-Project Sprint Story."
                raise self._error(message)
            try:
                expected = story_completion_fingerprint(
                    snapshot,
                    sprint_id=row.sprint_id,
                    story_id=row.story_id,
                    closure=StoryClosurePayload(
                        resolution=row.resolution,
                        delivered=row.delivered,
                        evidence=row.evidence,
                        known_gaps=row.known_gaps,
                    ),
                )
            except ExecutionIntegrityError as exc:
                message = "Story closure execution contract changed."
                raise self._error(message) from exc
            if expected != row.completion_fingerprint:
                message = "Story closure fingerprint changed."
                raise self._error(message)
            facts.append(
                StoryCompletionFact(
                    completion_id=self._required_id(
                        row.story_closure_id,
                        "story closure",
                    ),
                    story_id=row.story_id,
                    sprint_id=row.sprint_id,
                    completion_fingerprint=row.completion_fingerprint,
                    resolution=row.resolution,
                    delivered=row.delivered,
                    evidence=row.evidence,
                    known_gaps=row.known_gaps,
                )
            )
        return tuple(facts)

    def _sprint_reviews(
        self,
        project_id: int,
        sprints: tuple[SprintFact, ...],
    ) -> tuple[SprintReviewFact, ...]:
        sprint_ids = frozenset(item.sprint_id for item in sprints)
        linked_rows = (
            self._session.exec(
                select(SprintReview)
                .where(col(SprintReview.sprint_id).in_(sprint_ids))
                .order_by(col(SprintReview.sprint_review_id)),
                execution_options=self._query_options(),
            ).all()
            if sprint_ids
            else []
        )
        if any(row.project_id != project_id for row in linked_rows):
            message = "Sprint review crosses Project ownership."
            raise self._error(message)
        rows = self._session.exec(
            select(SprintReview)
            .where(col(SprintReview.project_id) == project_id)
            .order_by(col(SprintReview.sprint_review_id)),
            execution_options=self._query_options(),
        ).all()
        return tuple(
            SprintReviewFact(
                review_id=self._required_id(row.sprint_review_id, "sprint review"),
                sprint_id=self._required_member_id(
                    row.sprint_id,
                    sprint_ids,
                    "sprint review",
                ),
                review_fingerprint=row.review_fingerprint,
            )
            for row in rows
        )

    def _sprint_closures(
        self,
        project_id: int,
        sprints: tuple[SprintFact, ...],
    ) -> tuple[SprintClosureFact, ...]:
        sprint_ids = frozenset(item.sprint_id for item in sprints)
        completed_ids = frozenset(
            item.sprint_id for item in sprints if item.status == "completed"
        )
        linked_rows = (
            self._session.exec(
                select(SprintClosure)
                .where(col(SprintClosure.sprint_id).in_(sprint_ids))
                .order_by(col(SprintClosure.sprint_closure_id)),
                execution_options=self._query_options(),
            ).all()
            if sprint_ids
            else []
        )
        if any(row.project_id != project_id for row in linked_rows):
            message = "Sprint closure crosses Project ownership."
            raise self._error(message)
        rows = self._session.exec(
            select(SprintClosure)
            .where(col(SprintClosure.project_id) == project_id)
            .order_by(col(SprintClosure.sprint_closure_id)),
            execution_options=self._query_options(),
        ).all()
        return tuple(
            SprintClosureFact(
                closure_id=self._required_id(row.sprint_closure_id, "sprint closure"),
                sprint_id=self._required_member_id(
                    row.sprint_id,
                    completed_ids,
                    "sprint closure",
                ),
                review_fingerprint=row.review_fingerprint,
                close_fingerprint=row.close_fingerprint,
            )
            for row in rows
        )

    def _post_sprint_triage(
        self,
        project_id: int,
        sprints: tuple[SprintFact, ...],
    ) -> tuple[PostSprintTriageFact, ...]:
        sprint_ids = frozenset(item.sprint_id for item in sprints)
        completed_ids = frozenset(
            item.sprint_id for item in sprints if item.status == "completed"
        )
        linked_rows = (
            self._session.exec(
                select(PostSprintTriage)
                .where(col(PostSprintTriage.sprint_id).in_(sprint_ids))
                .order_by(col(PostSprintTriage.triage_id)),
                execution_options=self._query_options(),
            ).all()
            if sprint_ids
            else []
        )
        if any(row.project_id != project_id for row in linked_rows):
            message = "Post-sprint triage crosses Project ownership."
            raise self._error(message)
        rows = self._session.exec(
            select(PostSprintTriage)
            .where(col(PostSprintTriage.project_id) == project_id)
            .order_by(col(PostSprintTriage.triage_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[PostSprintTriageFact] = []
        rows_by_id = {
            self._required_id(row.triage_id, "post-sprint triage"): row for row in rows
        }
        for row in rows:
            triage_id = self._required_id(row.triage_id, "post-sprint triage")
            self._require_member(row.sprint_id, completed_ids, "post-sprint triage")
            if row.supersedes_triage_id is not None:
                parent = rows_by_id.get(row.supersedes_triage_id)
                if parent is None:
                    message = "Post-sprint triage correction parent is missing."
                    raise self._error(message)
                if parent.sprint_id != row.sprint_id:
                    message = "Post-sprint triage correction crosses Sprints."
                    raise self._error(message)
            try:
                payload = _JSON_OBJECT.validate_json(row.canonical_payload_json)
            except ValidationError as exc:
                message = "Post-sprint triage payload is invalid."
                raise self._error(message) from exc
            if (
                canonical_json(payload) != row.canonical_payload_json
                or row.impact not in {"none", "backlog", "specification"}
                or triage_payload_fingerprint(row.impact, payload)
                != row.payload_fingerprint
            ):
                message = "Post-sprint triage payload fingerprint changed."
                raise self._error(message)
            facts.append(
                PostSprintTriageFact.model_validate(
                    {
                        "triage_id": triage_id,
                        "sprint_id": row.sprint_id,
                        "impact": row.impact,
                        "canonical_payload": payload,
                        "payload_fingerprint": row.payload_fingerprint,
                        "supersedes_triage_id": row.supersedes_triage_id,
                    }
                )
            )
        return tuple(sorted(facts, key=lambda item: item.triage_id))

    def _node_attempts(self, project_id: int) -> tuple[NodeAttemptFact, ...]:
        attempts = self._session.exec(
            select(WorkflowNodeAttempt)
            .where(col(WorkflowNodeAttempt.project_id) == project_id)
            .order_by(
                col(WorkflowNodeAttempt.started_at),
                col(WorkflowNodeAttempt.workflow_node_attempt_id),
            ),
            execution_options=self._query_options(),
        ).all()
        outcomes = self._session.exec(
            select(WorkflowNodeAttemptOutcome)
            .where(col(WorkflowNodeAttemptOutcome.project_id) == project_id)
            .order_by(
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id),
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_outcome_id),
            ),
            execution_options=self._query_options(),
        ).all()
        attempt_ids = frozenset(
            self._required_id(row.workflow_node_attempt_id, "workflow node attempt")
            for row in attempts
        )
        outcomes_by_attempt: dict[int, WorkflowNodeAttemptOutcome] = {}
        for outcome in outcomes:
            self._require_member(
                outcome.workflow_node_attempt_id,
                attempt_ids,
                "workflow node attempt outcome",
            )
            self._attempt_outcome(outcome.status)
            outcomes_by_attempt[outcome.workflow_node_attempt_id] = outcome
        return tuple(
            NodeAttemptFact(
                attempt_id=self._required_id(
                    row.workflow_node_attempt_id,
                    "workflow node attempt",
                ),
                node_id=row.node_id,
                instance_key=row.instance_key,
                graph_version=row.graph_version,
                input_fingerprint=row.input_fingerprint,
                fact_fingerprint=row.fact_fingerprint,
                business_fact_fingerprint=row.business_fact_fingerprint,
                decision_fingerprint=row.decision_fingerprint,
                attempt_fingerprint=row.attempt_fingerprint,
                model_id=row.model_id,
                lease_expires_at=row.lease_expires_at,
                outcome=self._outcome_for_attempt(row, outcomes_by_attempt),
            )
            for row in attempts
        )

    @staticmethod
    def _required_id(value: int | None, label: str) -> int:
        if value is None:
            message = f"Stored {label} has no primary key."
            raise WorkflowFactRepository._error(message)
        return value

    @staticmethod
    def _require_product_condition(condition: bool, message: str) -> None:
        """Raise a fact-load error when a durable product condition is false."""
        if not condition:
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_project_run(
        discovery_run_id: int,
        discovery_run_ids: frozenset[int],
        label: str,
    ) -> None:
        if discovery_run_id not in discovery_run_ids:
            message = (
                f"Forced cross-project corruption in {label}: "
                f"discovery run {discovery_run_id} is not owned by this Project."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_member(value: int, values: frozenset[int], label: str) -> None:
        if value not in values:
            message = (
                f"Forced cross-project corruption in {label}: "
                f"reference {value} is not owned by this Project."
            )
            raise WorkflowFactRepository._error(message)

    def _required_member_id(
        self,
        value: int,
        values: frozenset[int],
        label: str,
    ) -> int:
        """Validate and return one Project-owned durable identity."""
        self._require_member(value, values, label)
        return value

    @staticmethod
    def _require_same_run_reference(
        value: int | None,
        discovery_run_id: int,
        runs_by_id: dict[int, int],
        label: str,
    ) -> None:
        if value is None:
            return
        referenced_run_id = runs_by_id.get(value)
        if referenced_run_id != discovery_run_id:
            message = (
                f"Forced relationship corruption in {label}: reference {value} "
                f"does not belong to discovery run {discovery_run_id}."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_fingerprint_reference(
        value: int,
        fingerprint: str,
        fingerprints_by_id: dict[int, str],
        label: str,
    ) -> None:
        if fingerprints_by_id.get(value) != fingerprint:
            message = (
                f"Forced relationship corruption in {label}: reference {value} "
                "does not match its persisted fingerprint."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_artifact_parent(
        value: int,
        discovery_run_id: int,
        fingerprint: str,
        artifacts_by_id: dict[int, tuple[int, str]],
        label: str,
    ) -> None:
        if artifacts_by_id.get(value) != (discovery_run_id, fingerprint):
            message = (
                f"Forced relationship corruption in {label}: artifact {value} "
                "does not match its discovery run and fingerprint."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _validate_spec_draft_base(
        row: SpecDraft,
        spec_versions: dict[int, str],
    ) -> None:
        if row.kind == "initial":
            if row.base_spec_version_id is None and row.base_spec_hash is None:
                return
        elif row.base_spec_version_id is not None and row.base_spec_hash is not None:
            WorkflowFactRepository._require_fingerprint_reference(
                row.base_spec_version_id,
                row.base_spec_hash,
                spec_versions,
                "specification draft base",
            )
            return
        message = (
            "Forced relationship corruption in specification draft base: "
            f"draft {row.spec_draft_id} has an invalid base relationship."
        )
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _validate_canonical_json(content: str, label: str, identifier: int) -> None:
        try:
            _JSON_OBJECT.validate_json(content)
        except ValidationError as exc:
            message = f"Stored canonical {label} {identifier} JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc

    @staticmethod
    def _canonical_json_object(content: str, label: str) -> dict[str, JsonValue]:
        """Decode one canonical JSON object retained without a content hash field."""
        try:
            value = _JSON_OBJECT.validate_json(content)
        except ValidationError as exc:
            message = f"{label} JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc
        if canonical_json(value) != content:
            message = f"{label} JSON is not canonical."
            raise WorkflowFactRepository._error(message)
        return value

    @staticmethod
    def _canonical_string_list(content: str, label: str) -> tuple[str, ...]:
        """Decode one canonical JSON string list retained without a content hash."""
        try:
            value = _STRING_LIST.validate_json(content)
        except ValidationError as exc:
            message = f"{label} JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc
        if canonical_json(value) != content:
            message = f"{label} JSON is not canonical."
            raise WorkflowFactRepository._error(message)
        return tuple(value)

    @staticmethod
    def _validate_authority_json(
        content: str | None,
        authority_id: int,
        compiler_version: str,
        prompt_hash: str,
    ) -> None:
        if content is None:
            message = f"Stored canonical authority {authority_id} JSON is missing."
            raise WorkflowFactRepository._error(message)
        try:
            artifact = SpecAuthorityCompilationSuccess.model_validate_json(content)
        except ValidationError as exc:
            message = f"Stored canonical authority {authority_id} JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc
        duplicated_provenance = (
            ("compiler_version", artifact.compiler_version, compiler_version),
            ("prompt_hash", artifact.prompt_hash, prompt_hash),
        )
        for field_name, artifact_value, authoritative_value in duplicated_provenance:
            if artifact_value != authoritative_value:
                message = (
                    f"Stored canonical authority {authority_id} {field_name} "
                    "does not match the authoritative compiled authority row."
                )
                raise WorkflowFactRepository._error(message)

    @staticmethod
    def _review_decision_fact(source: _ReviewDecisionSource) -> ReviewDecisionFact:
        return ReviewDecisionFact(
            decision_id=source.decision_id,
            artifact_type=source.artifact_type,
            artifact_id=source.artifact_id,
            artifact_fingerprint=source.artifact_fingerprint,
            decision=WorkflowFactRepository._review_outcome(source.decision),
            decided_at=source.decided_at,
        )

    @staticmethod
    def _authority_acceptance_source(
        row: SpecAuthorityAcceptance,
        spec_versions: dict[int, str],
        authorities_by_id: dict[
            int,
            tuple[CompiledSpecAuthority, SpecRegistry, str],
        ],
    ) -> _AuthorityAcceptanceSource:
        decision_id = WorkflowFactRepository._required_id(
            row.id,
            "authority acceptance",
        )
        WorkflowFactRepository._require_fingerprint_reference(
            row.spec_version_id,
            row.spec_hash,
            spec_versions,
            "authority acceptance specification",
        )
        authority_id = row.pending_authority_id
        if authority_id is None:
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"decision {decision_id} has no compiled authority."
            )
            raise WorkflowFactRepository._error(message)
        authority_record = authorities_by_id.get(authority_id)
        if authority_record is None:
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"authority {authority_id} is not owned by this Project."
            )
            raise WorkflowFactRepository._error(message)
        authority, spec, expected_fingerprint = authority_record
        if (
            authority.spec_version_id != row.spec_version_id
            or spec.spec_version_id != row.spec_version_id
            or authority.compiler_version != row.compiler_version
            or authority.prompt_hash != row.prompt_hash
        ):
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"decision {decision_id} does not match authority {authority_id}."
            )
            raise WorkflowFactRepository._error(message)
        if row.authority_fingerprint != expected_fingerprint:
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"decision {decision_id} has the wrong authority fingerprint."
            )
            raise WorkflowFactRepository._error(message)
        WorkflowFactRepository._authority_status(row.status)
        return _AuthorityAcceptanceSource(
            decision_id=decision_id,
            authority_id=authority_id,
            authority_fingerprint=expected_fingerprint,
            status=row.status,
            decided_at=row.decided_at,
        )

    @staticmethod
    def _latest_acceptance(
        authority_id: int,
        acceptances: Iterable[_AuthorityAcceptanceSource],
    ) -> _AuthorityAcceptanceSource | None:
        matching = (item for item in acceptances if item.authority_id == authority_id)
        return next(reversed(tuple(matching)), None)

    @staticmethod
    def _authority_state(
        spec_status: str,
        acceptance: _AuthorityAcceptanceSource | None,
    ) -> tuple[_AuthorityStatus, datetime | None]:
        if spec_status == "superseded":
            return "stale", None
        if acceptance is None:
            return "pending_review", None
        return (
            WorkflowFactRepository._authority_status(acceptance.status),
            acceptance.decided_at,
        )

    @staticmethod
    def _review_outcome(value: str) -> _ReviewOutcome:
        if value == "accepted":
            return "accepted"
        if value == "rejected":
            return "rejected"
        if value == "feedback":
            return "feedback"
        message = f"Invalid review decision {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _project_origin(value: str) -> _ProjectOrigin:
        if value == "greenfield":
            return "greenfield"
        if value == "brownfield":
            return "brownfield"
        message = f"Project has invalid origin {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _discovery_purpose(value: str) -> _DiscoveryPurpose:
        if value == "initial":
            return "initial"
        if value == "extension":
            return "extension"
        message = f"Discovery run has invalid purpose {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _spec_draft_kind(value: str) -> _SpecDraftKind:
        if value == "initial":
            return "initial"
        if value == "amendment":
            return "amendment"
        message = f"Specification draft has invalid kind {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _vision_mode(value: str) -> _VisionMode:
        if value == "initial":
            return "initial"
        if value == "revision":
            return "revision"
        message = f"Invalid Vision interview mode {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _vision_interview_output_fingerprint(
        components: JsonObject,
        vision_statement: str,
        is_complete: bool,
        clarifying_questions: tuple[str, ...],
    ) -> str:
        """Hash the canonical Vision model output without user-input trace data."""
        return canonical_hash(
            {
                "components_json": components,
                "vision_statement": vision_statement,
                "is_complete": is_complete,
                "clarifying_questions_json": list(clarifying_questions),
            }
        )

    @staticmethod
    def _product_goal_outcome(value: str) -> _ProductGoalOutcome:
        if value == "fulfilled":
            return "fulfilled"
        if value == "abandoned":
            return "abandoned"
        message = f"Invalid Product Goal outcome {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _product_goal_decision(value: str) -> _ProductGoalDecision:
        if value == "accepted":
            return "accepted"
        if value == "rejected":
            return "rejected"
        if value == "feedback":
            return "feedback"
        message = f"Invalid Product Goal decision {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _authority_status(value: str) -> Literal["accepted", "rejected"]:
        if value == "accepted":
            return "accepted"
        if value == "rejected":
            return "rejected"
        message = f"Authority acceptance has invalid status {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _attempt_outcome(value: str) -> _AttemptOutcome:
        if value == "success":
            return "success"
        if value == "failure":
            return "failure"
        if value == "obsolete":
            return "obsolete"
        message = f"Workflow node attempt outcome has invalid status {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _outcome_for_attempt(
        attempt: WorkflowNodeAttempt,
        outcomes_by_attempt: dict[int, WorkflowNodeAttemptOutcome],
    ) -> _AttemptOutcome | None:
        attempt_id = WorkflowFactRepository._required_id(
            attempt.workflow_node_attempt_id,
            "workflow node attempt",
        )
        outcome = outcomes_by_attempt.get(attempt_id)
        return (
            WorkflowFactRepository._attempt_outcome(outcome.status)
            if outcome is not None
            else None
        )

    @staticmethod
    def _error(message: str) -> WorkflowFactLoadError:
        return WorkflowFactLoadError(message)

    @staticmethod
    def _story_readiness_blockers(
        stories: Iterable[UserStory],
        dependencies: Iterable[UserStoryDependency],
        stories_by_id: dict[int, UserStory],
    ) -> dict[int, tuple[str, ...]]:
        blockers: dict[int, list[str]] = {story_id: [] for story_id in stories_by_id}
        for story in stories:
            story_id = WorkflowFactRepository._required_id(story.story_id, "story")
            if story.is_superseded:
                blockers[story_id].append("STORY_SUPERSEDED")
            if story.accepted_spec_version_id is None:
                blockers[story_id].append("SPECIFICATION_NOT_ACCEPTED")
        for dependency in dependencies:
            if dependency.status != "active":
                continue
            prerequisite = stories_by_id[dependency.prerequisite_story_id]
            if prerequisite.status not in _DONE_STORY_STATUSES:
                blockers[dependency.dependent_story_id].append(
                    f"PREREQUISITE_STORY_{dependency.prerequisite_story_id}_INCOMPLETE"
                )
        return {
            story_id: tuple(values) for story_id, values in sorted(blockers.items())
        }


@dataclass(frozen=True)
class VisionInputContext:
    """Canonical durable rows required to prepare one Vision interview input."""

    project: ProjectFact
    project_description: str | None
    vision_artifacts: tuple[VisionArtifactFact, ...]
    vision_decisions: tuple[VisionArtifactDecisionFact, ...]
    revision_intents: tuple[VisionRevisionIntentFact, ...]
    interview_turns: tuple[VisionInterviewTurnFact, ...]


@dataclass(frozen=True)
class VisionInputSelection:
    """Current Vision chain state without expanding to a workflow snapshot."""

    mode: _VisionMode
    accepted_vision_statement: str | None
    revision_intent_id: int | None
    prior_turn: VisionInterviewTurnFact | None


class VisionInputFactRepository(WorkflowFactRepository):
    """Read the narrow, canonical fact projection used by Vision host input."""

    def load_context(self, project_id: int) -> VisionInputContext:
        """Load Project identity and Vision rows without attempts or other domains."""
        self._identity_token = object()
        with self._session.no_autoflush:
            project = self._project(project_id)
            project_description = self._session.exec(
                select(Project.description)
                .where(col(Project.project_id) == project_id)
                .order_by(col(Project.project_id)),
                execution_options=self._query_options(),
            ).one()
            visions, decisions = self._vision_artifacts(project_id)
            accepted_visions = {
                item.vision_artifact_id: item.artifact_fingerprint
                for item in decisions.values()
                if item.decision == "accepted"
            }
            revisions = self._vision_revision_intents(project_id, accepted_visions)
            turns = self._vision_interview_turns(project_id, revisions, None)
            self._validate_vision_artifact_sources(visions, turns)
        return VisionInputContext(
            project=project,
            project_description=project_description,
            vision_artifacts=tuple(visions.values()),
            vision_decisions=tuple(decisions.values()),
            revision_intents=tuple(revisions.values()),
            interview_turns=tuple(turns.values()),
        )

    def has_active_product_goal(self, context: VisionInputContext) -> bool:
        """Validate only Goal rows required to gate a pending Vision revision."""
        project_id = context.project.project_id
        vision_fingerprints = {
            item.vision_artifact_id: item.content_fingerprint
            for item in context.vision_artifacts
        }
        with self._session.no_autoflush:
            goal_turns = self._product_goal_interview_turns(
                project_id,
                vision_fingerprints,
                None,
            )
            goals = self._product_goals(project_id, vision_fingerprints, goal_turns)
            decisions = self._product_goal_decisions(project_id, goals)
            outcomes = self._product_goal_outcomes(project_id, goals, decisions)
        return bool(self._active_accepted_product_goal_ids(goals, decisions, outcomes))


def select_vision_interview_input(context: VisionInputContext) -> VisionInputSelection:
    """Select one current Vision interview chain from its narrow fact projection."""
    artifacts_by_id = {
        item.vision_artifact_id: item for item in context.vision_artifacts
    }
    children = {
        item.supersedes_vision_artifact_id
        for item in context.vision_artifacts
        if item.supersedes_vision_artifact_id is not None
    }
    current = [
        item
        for item in context.vision_artifacts
        if item.vision_artifact_id not in children
    ]
    if len(current) > 1:
        message = "Vision facts are ambiguous."
        raise WorkflowFactLoadError(message)
    artifact = current[0] if current else None
    decisions_by_artifact = {
        item.vision_artifact_id: item for item in context.vision_decisions
    }
    if len(decisions_by_artifact) != len(context.vision_decisions):
        message = "Vision facts are ambiguous."
        raise WorkflowFactLoadError(message)
    completed_turn_ids = {
        item.source_interview_turn_id for item in context.vision_artifacts
    }
    open_intents = [
        item
        for item in context.revision_intents
        if not any(
            turn.mode == "revision"
            and turn.revision_intent_id == item.vision_revision_intent_id
            and turn.vision_interview_turn_id in completed_turn_ids
            for turn in context.interview_turns
        )
    ]
    if len(open_intents) > 1:
        message = "Vision facts are ambiguous."
        raise WorkflowFactLoadError(message)
    revision = open_intents[0] if open_intents else None
    if revision is not None:
        source = artifacts_by_id[revision.source_vision_artifact_id]
        mode: _VisionMode = "revision"
        accepted_statement = source.statement
        intent_id = revision.vision_revision_intent_id
    else:
        mode = "initial"
        accepted_statement = None
        intent_id = None
        artifact_decision = (
            None
            if artifact is None
            else decisions_by_artifact.get(artifact.vision_artifact_id)
        )
        if artifact_decision is not None and artifact_decision.decision == "accepted":
            message = "Accepted Vision requires an explicit revision intent."
            raise WorkflowFactLoadError(message)
    turns = tuple(
        item
        for item in context.interview_turns
        if item.mode == mode and item.revision_intent_id == intent_id
    )
    prior_ids = {item.prior_turn_id for item in turns if item.prior_turn_id is not None}
    leaves = [item for item in turns if item.vision_interview_turn_id not in prior_ids]
    if len(leaves) > 1:
        message = "Vision interview turn chain is ambiguous."
        raise WorkflowFactLoadError(message)
    return VisionInputSelection(
        mode=mode,
        accepted_vision_statement=accepted_statement,
        revision_intent_id=intent_id,
        prior_turn=leaves[0] if leaves else None,
    )
