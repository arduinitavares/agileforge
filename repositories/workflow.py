"""Read canonical durable facts for one workflow Project."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

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
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding
from models.specs import SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    PostSprintTriage,
    RoadmapArtifact,
    RoadmapArtifactDecision,
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
from services.contracts.specification_authoring import (
    SpecificationStructuringInput,
    specification_structuring_fact_fingerprint,
    specification_structuring_input_fingerprint,
)
from services.contracts.specification_source import (
    SpecificationSourceBundle,
    source_bundle_fingerprint,
)
from services.contracts.vision_evidence import VisionEvidenceBundle
from services.planning_artifact_content import (
    load_bound_sprint_plan_envelope,
    load_stored_backlog_planning_content,
    load_stored_roadmap_planning_content,
)
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageError,
    accepted_ancestor_ids,
)
from services.planning_lineage import (
    Decision as PlanningLineageDecision,
)
from services.specs.accepted_specification import (
    AcceptedSpecificationIntegrityError,
    load_accepted_specification,
)
from services.specs.candidate_contract import (
    SpecificationCandidateEnvelope,
    canonical_candidate_json,
    load_candidate_contract,
)
from services.specs.story_validation_service import (
    StoryValidationReadinessError,
    require_story_ready_for_sprint,
)
from utils.agileforge_spec_profile_v2 import (
    SpecificationPayload,
    canonical_spec_hash,
)
from utils.spec_schemas import ValidationEvidence
from utils.task_metadata import parse_task_metadata, serialize_task_metadata
from workflow.contracts import JsonValue
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
    BacklogItemFact,
    NodeAttemptFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    PostSprintTriageFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProductGoalInterviewTurnFact,
    ProductGoalOutcomeFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecificationCandidateFact,
    SpecificationDecisionFact,
    SpecificationSourceFact,
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
    VisionEvidenceSnapshotFact,
    VisionInterviewTurnFact,
    VisionRevisionIntentFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
    vision_interview_output_fingerprint,
    workflow_node_attempt_fingerprint,
)
from workflow.planning_integrity import (
    dependency_edges_are_canonical,
    dependency_edges_have_cycle,
    dependency_edges_payload,
    dependency_review_fingerprint,
    planned_task_content_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from services.contracts.backlog import BacklogOutput

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_JSON_OBJECT_LIST = TypeAdapter(list[dict[str, JsonValue]])
_STRING_LIST = TypeAdapter(list[str])
_INT_LIST = TypeAdapter(list[int])
_DEPENDENCY_EDGE_LIST = TypeAdapter(list[StoryDependencyReviewEdgeFact])
type _AttemptOutcome = Literal["success", "failure", "obsolete"]
type _ReviewArtifactType = Literal[
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
type _SprintFactStatus = Literal["planned", "active", "completed"]
type _VisionOperation = Literal["bootstrap", "clarification", "revision"]
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
class _PhaseArtifactLoad:
    """Project-definition artifact facts and exact review facts."""

    facts: tuple[PhaseArtifactFact, ...]
    reviews: tuple[ReviewDecisionFact, ...]


@dataclass(frozen=True)
class _ProductDefinitionFactLoad:
    """Validated immutable product-definition facts from one Project."""

    revision_intents: tuple[VisionRevisionIntentFact, ...]
    evidence_snapshots: tuple[VisionEvidenceSnapshotFact, ...]
    interview_turns: tuple[VisionInterviewTurnFact, ...]
    visions: tuple[VisionArtifactFact, ...]
    vision_decisions: tuple[VisionArtifactDecisionFact, ...]
    goal_interview_turns: tuple[ProductGoalInterviewTurnFact, ...]
    product_goals: tuple[ProductGoalArtifactFact, ...]
    goal_decisions: tuple[ProductGoalArtifactDecisionFact, ...]
    goal_outcomes: tuple[ProductGoalOutcomeFact, ...]
    specification_sources: tuple[SpecificationSourceFact, ...]
    specification_candidates: tuple[SpecificationCandidateFact, ...]
    specification_decisions: tuple[SpecificationDecisionFact, ...]


@dataclass(frozen=True)
class _VisionFactLoad:
    """Validated immutable Vision lineage and its durable source evidence."""

    revision_intents: tuple[VisionRevisionIntentFact, ...]
    evidence_snapshots: tuple[VisionEvidenceSnapshotFact, ...]
    interview_turns: tuple[VisionInterviewTurnFact, ...]
    visions: tuple[VisionArtifactFact, ...]
    vision_decisions: tuple[VisionArtifactDecisionFact, ...]


@dataclass(frozen=True)
class _SpecificationCandidateSources:
    """Validated parent maps required to reload specification candidates."""

    visions: dict[int, str]
    goals: dict[int, ProductGoalArtifactFact]
    decisions: dict[int, ProductGoalArtifactDecisionFact]
    outcomes: dict[int, ProductGoalOutcomeFact]
    specification_sources: dict[int, SpecificationSourceFact]
    spec_versions: dict[int, str]
    attempts: dict[int, NodeAttemptFact]


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

    def load_vision_snapshot(self, project_id: int) -> WorkflowFactSnapshot:
        """Load only Project identity and validated immutable Vision lineage."""
        self._identity_token = object()
        with self._session.no_autoflush:
            project = self._project(project_id)
            vision_load = self._vision_definition(
                project_id,
                self._node_attempts(project_id),
            )
            return WorkflowFactSnapshot(
                project=project,
                vision_evidence_snapshots=vision_load.evidence_snapshots,
                vision_artifacts=vision_load.visions,
                vision_artifact_decisions=vision_load.vision_decisions,
            )

    def _load(self, project_id: int) -> WorkflowFactSnapshot:
        """Build one snapshot inside the read-only session query boundary."""
        project = self._project(project_id)
        spec_version_facts = self._spec_versions(project_id)
        spec_versions = {
            item.spec_version_id: item.spec_hash for item in spec_version_facts
        }
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
        phase_load = self._phase_artifacts(project_id)
        planning_load = self._planning_artifacts(project_id)
        sprints = self._sprints(project_id)
        stories = self._stories(
            project_id,
            spec_versions,
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
            planning_load.facts,
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
            review_decisions=tuple(
                sorted(
                    (
                        *phase_load.reviews,
                        *planning_load.reviews,
                    ),
                    key=lambda item: (
                        item.decided_at,
                        item.artifact_type,
                        item.artifact_id,
                        item.decision_id,
                    ),
                )
            ),
            vision_revision_intents=product_definition.revision_intents,
            vision_evidence_snapshots=product_definition.evidence_snapshots,
            vision_interview_turns=product_definition.interview_turns,
            vision_artifacts=product_definition.visions,
            vision_artifact_decisions=product_definition.vision_decisions,
            product_goal_interview_turns=product_definition.goal_interview_turns,
            product_goal_artifacts=product_definition.product_goals,
            product_goal_artifact_decisions=product_definition.goal_decisions,
            product_goal_outcomes=product_definition.goal_outcomes,
            specification_sources=product_definition.specification_sources,
            specification_candidates=product_definition.specification_candidates,
            specification_decisions=product_definition.specification_decisions,
            spec_versions=spec_version_facts,
            phase_artifacts=phase_load.facts,
            backlog_items=self._backlog_items(
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
        return ProjectFact(
            project_id=row.project_id,
            name=row.name,
            description=row.description,
            created_at=row.created_at,
            active_repository_binding_id=row.active_repository_binding_id,
        )

    def _spec_versions(self, project_id: int) -> tuple[SpecVersionFact, ...]:
        rows = self._session.exec(
            select(SpecRegistry)
            .where(col(SpecRegistry.project_id) == project_id)
            .order_by(col(SpecRegistry.spec_version_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[SpecVersionFact] = []
        for row in rows:
            spec_version_id = self._required_id(
                row.spec_version_id,
                "specification registry row",
            )
            try:
                accepted = load_accepted_specification(
                    self._session,
                    project_id=project_id,
                    spec_version_id=spec_version_id,
                    spec_hash=row.spec_hash,
                )
            except AcceptedSpecificationIntegrityError as exc:
                message = f"{exc.code}: {exc}"
                raise self._error(message) from exc
            facts.append(
                SpecVersionFact(
                    spec_version_id=accepted.spec_version_id,
                    spec_hash=accepted.spec_hash,
                    status=accepted.status,
                    source_specification_decision_id=(
                        accepted.specification_decision_id
                    ),
                    accepted_at=accepted.accepted_at,
                    accepted_by=accepted.accepted_by,
                    acceptance_notes=accepted.acceptance_notes,
                    source_specification_candidate_id=(
                        accepted.source_specification_candidate_id
                    ),
                    source_specification_candidate_fingerprint=(
                        accepted.source_specification_candidate_fingerprint
                    ),
                    source_vision_artifact_id=row.source_vision_artifact_id,
                    source_vision_fingerprint=row.source_vision_fingerprint,
                    source_product_goal_artifact_id=(
                        row.source_product_goal_artifact_id
                    ),
                    source_product_goal_fingerprint=(
                        row.source_product_goal_fingerprint
                    ),
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
        vision_load = self._vision_definition(project_id, node_attempts)
        visions = {item.vision_artifact_id: item for item in vision_load.visions}
        attempt_fingerprints = {
            item.attempt_id: item.attempt_fingerprint for item in node_attempts
        }
        vision_fingerprints = {
            item_id: item.content_fingerprint for item_id, item in visions.items()
        }
        goal_turns = self._product_goal_interview_turns(
            project_id, vision_fingerprints, attempt_fingerprints
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
        specification_sources = self._specification_sources(
            project_id,
            visions=vision_fingerprints,
            vision_decisions=vision_load.vision_decisions,
            goals=goals,
            goal_decisions=decisions,
            outcomes=outcomes,
        )
        candidates = self._specification_candidates(
            project_id,
            _SpecificationCandidateSources(
                visions=vision_fingerprints,
                goals=goals,
                decisions=decisions,
                outcomes=outcomes,
                specification_sources=specification_sources,
                spec_versions=spec_versions,
                attempts={item.attempt_id: item for item in node_attempts},
            ),
        )
        specification_decisions = self._specification_decisions(project_id, candidates)
        return _ProductDefinitionFactLoad(
            revision_intents=vision_load.revision_intents,
            evidence_snapshots=vision_load.evidence_snapshots,
            interview_turns=vision_load.interview_turns,
            visions=vision_load.visions,
            vision_decisions=vision_load.vision_decisions,
            goal_interview_turns=tuple(goal_turns.values()),
            product_goals=tuple(goals.values()),
            goal_decisions=tuple(decisions.values()),
            goal_outcomes=tuple(outcomes.values()),
            specification_sources=tuple(specification_sources.values()),
            specification_candidates=tuple(candidates.values()),
            specification_decisions=tuple(specification_decisions.values()),
        )

    def _vision_definition(
        self,
        project_id: int,
        node_attempts: tuple[NodeAttemptFact, ...],
    ) -> _VisionFactLoad:
        """Load the Vision subset with the same source validation as full facts."""
        visions, decisions = self._vision_artifacts(project_id)
        accepted_visions = {
            item.vision_artifact_id: item.artifact_fingerprint
            for item in decisions.values()
            if item.decision == "accepted"
        }
        attempts = {item.attempt_id: item.attempt_fingerprint for item in node_attempts}
        revisions = self._vision_revision_intents(project_id, accepted_visions)
        snapshots = self._vision_evidence_snapshots(project_id, attempts)
        turns = self._vision_interview_turns(project_id, revisions, attempts)
        self._validate_vision_artifact_sources(visions, turns)
        return _VisionFactLoad(
            revision_intents=tuple(revisions.values()),
            evidence_snapshots=snapshots,
            interview_turns=tuple(turns.values()),
            visions=tuple(visions.values()),
            vision_decisions=tuple(decisions.values()),
        )

    def _vision_evidence_snapshots(
        self,
        project_id: int,
        attempts: dict[int, str],
    ) -> tuple[VisionEvidenceSnapshotFact, ...]:
        """Load exact, validated evidence snapshots for one Project."""
        binding_ids = frozenset(
            self._required_id(row.repository_binding_id, "repository binding")
            for row in self._session.exec(
                select(RepositoryBinding)
                .where(col(RepositoryBinding.project_id) == project_id)
                .order_by(col(RepositoryBinding.repository_binding_id)),
                execution_options=self._query_options(),
            ).all()
        )
        rows = self._session.exec(
            select(VisionEvidenceSnapshot)
            .where(col(VisionEvidenceSnapshot.project_id) == project_id)
            .order_by(col(VisionEvidenceSnapshot.vision_evidence_snapshot_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[VisionEvidenceSnapshotFact] = []
        facts_by_id: dict[int, VisionEvidenceSnapshotFact] = {}
        superseded_ids: set[int] = set()
        for row in rows:
            identifier = self._required_id(
                row.vision_evidence_snapshot_id,
                "Vision evidence snapshot",
            )
            self._require_product_condition(
                row.workflow_node_attempt_id in attempts,
                "Vision evidence snapshot references a missing or cross-Project "
                "workflow attempt.",
            )
            if row.repository_binding_id is not None:
                self._require_product_condition(
                    row.repository_binding_id in binding_ids,
                    "Vision evidence snapshot references a missing or cross-Project "
                    "repository binding.",
                )
            supersedes_id = row.supersedes_vision_evidence_snapshot_id
            self._require_product_condition(
                supersedes_id is None or supersedes_id in facts_by_id,
                "Vision evidence snapshot supersedes an unknown or later snapshot.",
            )
            self._require_product_condition(
                supersedes_id is None or supersedes_id not in superseded_ids,
                "Vision evidence snapshot supersession chain branches.",
            )
            if supersedes_id is not None:
                superseded_ids.add(supersedes_id)
            try:
                evidence_json = _JSON_OBJECT.validate_json(row.evidence_json)
                evidence = VisionEvidenceBundle.model_validate(evidence_json)
            except ValidationError as exc:
                message = f"Vision evidence snapshot {identifier} JSON is invalid."
                raise self._error(message) from exc
            if canonical_json(evidence_json) != row.evidence_json:
                message = (
                    f"Vision evidence snapshot {identifier} JSON is not canonical."
                )
                raise self._error(message)
            if evidence.evidence_fingerprint != row.evidence_fingerprint:
                message = f"Vision evidence snapshot {identifier} fingerprint changed."
                raise self._error(message)
            try:
                warnings = _JSON_OBJECT_LIST.validate_json(row.warnings_json)
            except ValidationError as exc:
                message = (
                    f"Vision evidence snapshot {identifier} warnings JSON is invalid."
                )
                raise self._error(message) from exc
            if canonical_json(warnings) != row.warnings_json:
                message = (
                    f"Vision evidence snapshot {identifier} warnings JSON is not "
                    "canonical."
                )
                raise self._error(message)
            evidence_warnings = [
                warning.model_dump(mode="json") for warning in evidence.warnings
            ]
            if warnings != evidence_warnings:
                message = f"Vision evidence snapshot {identifier} warnings changed."
                raise self._error(message)
            fact = VisionEvidenceSnapshotFact(
                vision_evidence_snapshot_id=identifier,
                repository_binding_id=row.repository_binding_id,
                supersedes_vision_evidence_snapshot_id=supersedes_id,
                workflow_node_attempt_id=row.workflow_node_attempt_id,
                evidence=evidence_json,
                evidence_fingerprint=row.evidence_fingerprint,
                warnings=tuple(warnings),
                created_at=row.created_at,
            )
            facts.append(fact)
            facts_by_id[identifier] = fact
        return tuple(facts)

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
            component_basis = self._canonical_json_object_list(
                row.component_basis_json,
                "Vision artifact component basis",
            )
            assumptions = self._canonical_json_object_list(
                row.assumptions_json,
                "Vision artifact assumptions",
            )
            conflicts = self._canonical_json_object_list(
                row.conflicts_json,
                "Vision artifact conflicts",
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
                vision_evidence_snapshot_id=row.vision_evidence_snapshot_id,
                component_basis=tuple(component_basis),
                assumptions=tuple(assumptions),
                conflicts=tuple(conflicts),
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
                and turn.vision_evidence_snapshot_id
                == artifact.vision_evidence_snapshot_id
                and turn.components == artifact.components
                and turn.vision_statement == artifact.statement
                and turn.component_basis == artifact.component_basis
                and turn.assumptions == artifact.assumptions
                and turn.conflicts == artifact.conflicts,
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
            message = f"Vision turn {identifier} has invalid operation."
            self._require_product_condition(
                row.operation in {"bootstrap", "clarification", "revision"},
                message,
            )
            operation = self._vision_operation(row.operation)
            if operation == "bootstrap":
                self._require_product_condition(
                    row.revision_intent_id is None,
                    "Bootstrap Vision turn cannot have a revision intent.",
                )
            elif operation == "revision":
                self._require_product_condition(
                    row.revision_intent_id in revisions,
                    "Revision Vision turn requires an exact Project revision intent.",
                )
            elif row.revision_intent_id is not None:
                self._require_product_condition(
                    row.revision_intent_id in revisions,
                    "Revision-lineage Vision turn requires an exact Project "
                    "revision intent.",
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
                self._require_product_condition(
                    operation in {"bootstrap", "revision"},
                    "Clarification Vision turn requires a prior turn.",
                )
            else:
                self._require_product_condition(
                    prior_turn.vision_evidence_snapshot_id
                    == row.vision_evidence_snapshot_id
                    and prior_turn.revision_intent_id == row.revision_intent_id,
                    "Vision turn prior turn has a different evidence or revision "
                    "chain.",
                )
                self._require_product_condition(
                    prior_turn.turn_number + 1 == row.turn_number,
                    "Vision turn prior turn is not sequential.",
                )
            if operation == "bootstrap":
                self._require_product_condition(
                    row.user_text is None,
                    "Bootstrap Vision turn cannot have user text.",
                )
            else:
                self._require_product_condition(
                    row.user_text is not None,
                    "Clarification and revision Vision turns require user text.",
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
            clarifying_questions = self._canonical_json_object_list(
                row.clarifying_questions_json,
                "Vision turn clarifying questions",
            )
            component_basis = self._canonical_json_object_list(
                row.component_basis_json,
                "Vision turn component basis",
            )
            assumptions = self._canonical_json_object_list(
                row.assumptions_json,
                "Vision turn assumptions",
            )
            conflicts = self._canonical_json_object_list(
                row.conflicts_json,
                "Vision turn conflicts",
            )
            self._require_product_condition(
                row.output_fingerprint
                == vision_interview_output_fingerprint(
                    components,
                    row.vision_statement,
                    row.is_complete,
                    clarifying_questions,
                    {
                        "component_basis": component_basis,
                        "assumptions": assumptions,
                        "conflicts": conflicts,
                    },
                ),
                "Vision turn output fingerprint changed.",
            )
            facts[identifier] = VisionInterviewTurnFact(
                vision_interview_turn_id=identifier,
                operation=operation,
                turn_number=row.turn_number,
                revision_intent_id=row.revision_intent_id,
                vision_evidence_snapshot_id=row.vision_evidence_snapshot_id,
                prior_turn_id=row.prior_turn_id,
                user_text=row.user_text,
                components=components,
                vision_statement=row.vision_statement,
                is_complete=row.is_complete,
                clarifying_questions=clarifying_questions,
                component_basis=tuple(component_basis),
                assumptions=tuple(assumptions),
                conflicts=tuple(conflicts),
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

    def _specification_candidates(
        self,
        project_id: int,
        sources: _SpecificationCandidateSources,
    ) -> dict[int, SpecificationCandidateFact]:
        rows = self._session.exec(
            select(SpecificationCandidate)
            .where(col(SpecificationCandidate.project_id) == project_id)
            .order_by(col(SpecificationCandidate.specification_candidate_id)),
            execution_options=self._query_options(),
        ).all()
        facts: dict[int, SpecificationCandidateFact] = {}
        goal_fingerprints = {
            identifier: item.content_fingerprint
            for identifier, item in sources.goals.items()
        }
        accepted_decisions_by_goal = self._accepted_goal_decisions_by_goal(
            sources.decisions.values()
        )
        outcomes_by_goal = {
            item.product_goal_artifact_id: item for item in sources.outcomes.values()
        }
        for row in rows:
            identifier = self._required_id(
                row.specification_candidate_id,
                "specification candidate",
            )
            source = sources.specification_sources.get(row.specification_source_id)
            self._require_product_condition(
                source is not None
                and source.source_fingerprint == row.specification_source_fingerprint,
                "Specification candidate source registration changed.",
            )
            if source is not None:
                self._require_product_condition(
                    (
                        source.vision_artifact_id,
                        source.vision_fingerprint,
                        source.product_goal_artifact_id,
                        source.product_goal_fingerprint,
                    )
                    == (
                        row.vision_artifact_id,
                        row.vision_fingerprint,
                        row.product_goal_artifact_id,
                        row.product_goal_fingerprint,
                    )
                    and source.registered_at < row.recorded_at,
                    "Specification candidate source lineage changed.",
                )
            self._require_fingerprint_reference(
                row.vision_artifact_id,
                row.vision_fingerprint,
                sources.visions,
                "specification candidate Vision",
            )
            self._require_fingerprint_reference(
                row.product_goal_artifact_id,
                row.product_goal_fingerprint,
                goal_fingerprints,
                "specification candidate Product Goal",
            )
            goal = sources.goals.get(row.product_goal_artifact_id)
            accepted_decisions = accepted_decisions_by_goal.get(
                row.product_goal_artifact_id,
                (),
            )
            self._require_product_condition(
                any(item.decided_at <= row.recorded_at for item in accepted_decisions),
                "Specification candidate Product Goal was not accepted when recorded.",
            )
            outcome = outcomes_by_goal.get(row.product_goal_artifact_id)
            self._require_product_condition(
                outcome is None or row.recorded_at < outcome.decided_at,
                "Specification candidate was recorded after its Product Goal outcome.",
            )
            if goal is not None:
                self._require_product_condition(
                    (goal.vision_artifact_id, goal.vision_fingerprint)
                    == (row.vision_artifact_id, row.vision_fingerprint),
                    "Specification candidate Vision does not match its Product Goal.",
                )
            self._require_product_condition(
                row.candidate_kind in {"initial", "amendment"},
                "Specification candidate kind is invalid.",
            )
            base_is_complete = row.base_spec_version_id is not None and (
                row.base_spec_hash is not None
            )
            self._require_product_condition(
                (row.candidate_kind == "initial" and not base_is_complete)
                or (row.candidate_kind == "amendment" and base_is_complete),
                "Specification candidate base specification is invalid.",
            )
            if base_is_complete:
                self._require_fingerprint_reference(
                    row.base_spec_version_id,
                    row.base_spec_hash,
                    sources.spec_versions,
                    "specification candidate base specification",
                )
            attempt = sources.attempts.get(row.workflow_node_attempt_id)
            self._require_product_condition(
                attempt is not None
                and attempt.attempt_fingerprint == row.attempt_fingerprint,
                "Specification candidate attempt changed.",
            )
            if attempt is None:
                continue
            canonical_envelope, payload, envelope = (
                self._canonical_specification_envelope(
                    row.canonical_envelope_json,
                    expected_candidate_fingerprint=row.candidate_fingerprint,
                )
            )
            self._require_product_condition(
                row.payload_fingerprint
                == canonical_spec_hash(payload)
                == envelope.payload_fingerprint,
                "Specification candidate payload fingerprint changed.",
            )
            self._require_product_condition(
                row.source_manifest_fingerprint == envelope.source_manifest_fingerprint,
                "Specification candidate source manifest fingerprint changed.",
            )
            source_manifest_lineage = {
                (item.kind.value, item.fingerprint) for item in envelope.source_manifest
            }
            self._require_product_condition(
                {
                    ("vision", row.vision_fingerprint),
                    ("product_goal", row.product_goal_fingerprint),
                }
                <= source_manifest_lineage,
                "Specification candidate source manifest omits direct lineage.",
            )
            self._require_product_condition(
                row.producer_input_fingerprint == envelope.producer_input_fingerprint,
                "Specification candidate producer input fingerprint changed.",
            )
            self._require_product_condition(
                row.rendered_view_fingerprint == envelope.review_view_fingerprint,
                "Specification candidate rendered view fingerprint changed.",
            )
            self._require_product_condition(
                row.candidate_fingerprint == envelope.candidate_fingerprint,
                "Specification candidate fingerprint changed.",
            )
            self._require_product_condition(
                row.candidate_kind == envelope.candidate_kind.value,
                "Specification candidate kind changed.",
            )
            self._require_product_condition(
                attempt.node_id == "specification.structure",
                "Specification candidate attempt uses the wrong workflow node.",
            )
            structuring_input = self._specification_candidate_structuring_input(
                project_id,
                row,
                attempt,
            )
            self._require_product_condition(
                envelope.accepted_fact_fingerprint
                == specification_structuring_fact_fingerprint(structuring_input),
                "Specification candidate accepted facts changed after its attempt.",
            )
            self._require_product_condition(
                envelope.producer_input_fingerprint
                == specification_structuring_input_fingerprint(structuring_input),
                "Specification candidate producer input changed after its attempt.",
            )
            self._require_product_condition(
                (
                    row.specification_source_id,
                    row.specification_source_fingerprint,
                    envelope.registered_source_fingerprint,
                    envelope.source_producer_capability,
                    envelope.source_preparation_capability,
                )
                == (
                    structuring_input.registered_source.specification_source_id,
                    structuring_input.registered_source.source_fingerprint,
                    structuring_input.registered_source.source_fingerprint,
                    structuring_input.registered_source.producer_capability,
                    structuring_input.registered_source.preparation_capability,
                ),
                "Specification candidate registered source changed after its attempt.",
            )
            self._require_product_condition(
                envelope.model_id == attempt.model_id,
                "Specification candidate model changed after its attempt.",
            )
            self._require_product_condition(
                (
                    row.vision_artifact_id,
                    row.vision_fingerprint,
                    row.product_goal_artifact_id,
                    row.product_goal_fingerprint,
                    row.base_spec_version_id,
                    row.base_spec_hash,
                    row.workflow_node_attempt_id,
                    row.attempt_fingerprint,
                )
                == (
                    envelope.accepted_vision_id,
                    envelope.accepted_vision_fingerprint,
                    envelope.accepted_product_goal_id,
                    envelope.accepted_product_goal_fingerprint,
                    envelope.base_specification_id,
                    envelope.base_payload_fingerprint,
                    envelope.workflow_node_attempt_id,
                    envelope.attempt_fingerprint,
                ),
                "Specification candidate envelope lineage changed.",
            )
            supersedes_is_complete = (
                row.supersedes_specification_candidate_id is not None
                and row.supersedes_candidate_fingerprint is not None
            )
            self._require_product_condition(
                supersedes_is_complete
                or (
                    row.supersedes_specification_candidate_id is None
                    and row.supersedes_candidate_fingerprint is None
                ),
                "Specification candidate supersession is invalid.",
            )
            if supersedes_is_complete:
                self._require_fingerprint_reference(
                    row.supersedes_specification_candidate_id,
                    row.supersedes_candidate_fingerprint,
                    {
                        item_id: item.candidate_fingerprint
                        for item_id, item in facts.items()
                    },
                    "specification candidate supersession",
                )
            facts[identifier] = SpecificationCandidateFact(
                specification_candidate_id=identifier,
                candidate_kind=(
                    "initial" if row.candidate_kind == "initial" else "amendment"
                ),
                specification_source_id=row.specification_source_id,
                specification_source_fingerprint=(row.specification_source_fingerprint),
                vision_artifact_id=row.vision_artifact_id,
                vision_fingerprint=row.vision_fingerprint,
                product_goal_artifact_id=row.product_goal_artifact_id,
                product_goal_fingerprint=row.product_goal_fingerprint,
                base_spec_version_id=row.base_spec_version_id,
                base_spec_hash=row.base_spec_hash,
                canonical_envelope=canonical_envelope,
                payload_fingerprint=row.payload_fingerprint,
                source_manifest_fingerprint=row.source_manifest_fingerprint,
                producer_input_fingerprint=row.producer_input_fingerprint,
                rendered_view_fingerprint=row.rendered_view_fingerprint,
                candidate_fingerprint=row.candidate_fingerprint,
                workflow_node_attempt_id=row.workflow_node_attempt_id,
                attempt_fingerprint=row.attempt_fingerprint,
                supersedes_specification_candidate_id=(
                    row.supersedes_specification_candidate_id
                ),
                supersedes_candidate_fingerprint=(row.supersedes_candidate_fingerprint),
                recorded_by=row.recorded_by,
                recorded_at=row.recorded_at,
            )
        return facts

    def _specification_sources(  # noqa: PLR0913
        self,
        project_id: int,
        *,
        visions: dict[int, str],
        vision_decisions: tuple[VisionArtifactDecisionFact, ...],
        goals: dict[int, ProductGoalArtifactFact],
        goal_decisions: dict[int, ProductGoalArtifactDecisionFact],
        outcomes: dict[int, ProductGoalOutcomeFact],
    ) -> dict[int, SpecificationSourceFact]:
        """Load canonical registered sources and fail closed on lineage drift."""
        bindings = {
            self._required_id(row.repository_binding_id, "repository binding"): row
            for row in self._session.exec(
                select(RepositoryBinding)
                .where(col(RepositoryBinding.project_id) == project_id)
                .order_by(col(RepositoryBinding.repository_binding_id)),
                execution_options=self._query_options(),
            ).all()
        }
        rows = self._session.exec(
            select(SpecificationSource)
            .where(col(SpecificationSource.project_id) == project_id)
            .order_by(col(SpecificationSource.specification_source_id)),
            execution_options=self._query_options(),
        ).all()
        accepted_vision_decisions: dict[
            int, tuple[VisionArtifactDecisionFact, ...]
        ] = {}
        for decision in vision_decisions:
            if decision.decision == "accepted":
                accepted_vision_decisions.setdefault(
                    decision.vision_artifact_id,
                    (),
                )
                accepted_vision_decisions[decision.vision_artifact_id] += (decision,)
        accepted_goal_decisions = self._accepted_goal_decisions_by_goal(
            goal_decisions.values()
        )
        outcomes_by_goal = {
            outcome.product_goal_artifact_id: outcome for outcome in outcomes.values()
        }
        facts: dict[int, SpecificationSourceFact] = {}
        superseded_ids: set[int] = set()
        for row in rows:
            identifier = self._required_id(
                row.specification_source_id,
                "Specification source",
            )
            self._require_fingerprint_reference(
                row.vision_artifact_id,
                row.vision_fingerprint,
                visions,
                "Specification source Vision",
            )
            goal_fingerprints = {
                item_id: item.content_fingerprint for item_id, item in goals.items()
            }
            self._require_fingerprint_reference(
                row.product_goal_artifact_id,
                row.product_goal_fingerprint,
                goal_fingerprints,
                "Specification source Product Goal",
            )
            goal = goals.get(row.product_goal_artifact_id)
            self._require_product_condition(
                goal is not None
                and (goal.vision_artifact_id, goal.vision_fingerprint)
                == (row.vision_artifact_id, row.vision_fingerprint),
                "Specification source Vision does not match its Product Goal.",
            )
            self._require_product_condition(
                any(
                    decision.decided_at <= row.registered_at
                    for decision in accepted_vision_decisions.get(
                        row.vision_artifact_id,
                        (),
                    )
                ),
                "Specification source Vision was not accepted when registered.",
            )
            self._require_product_condition(
                any(
                    decision.decided_at <= row.registered_at
                    for decision in accepted_goal_decisions.get(
                        row.product_goal_artifact_id,
                        (),
                    )
                ),
                "Specification source Product Goal was not accepted when registered.",
            )
            outcome = outcomes_by_goal.get(row.product_goal_artifact_id)
            self._require_product_condition(
                outcome is None or row.registered_at < outcome.decided_at,
                "Specification source was registered after its Product Goal outcome.",
            )
            binding = bindings.get(row.repository_binding_id)
            self._require_product_condition(
                binding is not None
                and binding.inspected_at <= row.registered_at
                and (
                    binding.head_sha,
                    binding.dirty,
                    binding.status_fingerprint,
                )
                == (
                    row.repository_head_sha,
                    row.repository_dirty,
                    row.repository_status_fingerprint,
                ),
                "Specification source repository revision changed.",
            )
            try:
                bundle = SpecificationSourceBundle.model_validate_json(
                    row.source_bundle_json
                )
            except ValidationError as error:
                message = f"Specification source {identifier} bundle is invalid."
                raise self._error(message) from error
            canonical_bundle = canonical_json(bundle.model_dump(mode="json"))
            self._require_product_condition(
                canonical_bundle == row.source_bundle_json,
                "Specification source bundle is not canonical.",
            )
            self._require_product_condition(
                source_bundle_fingerprint(bundle) == row.source_fingerprint,
                "Specification source fingerprint changed.",
            )
            self._require_product_condition(
                (
                    bundle.repository_revision.head_sha,
                    bundle.repository_revision.dirty,
                    bundle.repository_revision.status_fingerprint,
                    bundle.accepted_vision_fingerprint,
                    bundle.accepted_product_goal_fingerprint,
                )
                == (
                    row.repository_head_sha,
                    row.repository_dirty,
                    row.repository_status_fingerprint,
                    row.vision_fingerprint,
                    row.product_goal_fingerprint,
                ),
                "Specification source bundle lineage changed.",
            )
            parent_is_complete = (
                row.supersedes_specification_source_id is not None
                and row.supersedes_source_fingerprint is not None
            )
            self._require_product_condition(
                parent_is_complete
                or (
                    row.supersedes_specification_source_id is None
                    and row.supersedes_source_fingerprint is None
                ),
                "Specification source supersession is invalid.",
            )
            if parent_is_complete:
                parent_id = row.supersedes_specification_source_id
                self._require_product_condition(
                    parent_id not in superseded_ids,
                    "Specification source supersession chain branches.",
                )
                self._require_fingerprint_reference(
                    parent_id,
                    row.supersedes_source_fingerprint,
                    {
                        item_id: item.source_fingerprint
                        for item_id, item in facts.items()
                    },
                    "Specification source supersession",
                )
                if parent_id is not None:
                    parent = facts.get(parent_id)
                    self._require_product_condition(
                        parent is not None and parent.registered_at < row.registered_at,
                        "Specification source successor must follow its parent.",
                    )
                    superseded_ids.add(parent_id)
            facts[identifier] = SpecificationSourceFact(
                specification_source_id=identifier,
                source_fingerprint=row.source_fingerprint,
                bundle=bundle.model_dump(mode="json"),
                repository_binding_id=row.repository_binding_id,
                repository_head_sha=row.repository_head_sha,
                repository_dirty=row.repository_dirty,
                repository_status_fingerprint=row.repository_status_fingerprint,
                vision_artifact_id=row.vision_artifact_id,
                vision_fingerprint=row.vision_fingerprint,
                product_goal_artifact_id=row.product_goal_artifact_id,
                product_goal_fingerprint=row.product_goal_fingerprint,
                supersedes_specification_source_id=(
                    row.supersedes_specification_source_id
                ),
                supersedes_source_fingerprint=row.supersedes_source_fingerprint,
                registered_by=row.registered_by,
                registered_at=row.registered_at,
            )
        return facts

    def _specification_candidate_structuring_input(
        self,
        project_id: int,
        candidate: SpecificationCandidate,
        attempt: NodeAttemptFact,
    ) -> SpecificationStructuringInput:
        """Reload and verify the exact DB-local attempt behind one candidate."""
        attempt_row = self._session.get(
            WorkflowNodeAttempt,
            candidate.workflow_node_attempt_id,
        )
        if attempt_row is None:
            message = "Specification candidate producer attempt is missing."
            raise self._error(message)
        try:
            normalized_input = _JSON_OBJECT.validate_json(
                attempt_row.normalized_input_json
            )
            execution_settings = _JSON_OBJECT.validate_json(
                attempt_row.execution_settings_json
            )
            structuring_input = SpecificationStructuringInput.model_validate_json(
                attempt_row.normalized_input_json
            )
        except ValidationError as exc:
            message = "Specification candidate producer input is invalid."
            raise self._error(message) from exc
        identity = {
            "attempt_id": candidate.workflow_node_attempt_id,
            "project_id": attempt_row.project_id,
            "node_id": attempt_row.node_id,
            "instance_key": attempt_row.instance_key,
            "graph_version": attempt_row.graph_version,
            "fact_fingerprint": attempt_row.fact_fingerprint,
            "business_fact_fingerprint": attempt_row.business_fact_fingerprint,
            "decision_fingerprint": attempt_row.decision_fingerprint,
            "normalized_input": normalized_input,
            "input_fingerprint": attempt_row.input_fingerprint,
            "model_id": attempt_row.model_id,
            "execution_settings": execution_settings,
            "idempotency_key": attempt_row.idempotency_key,
            "actor": attempt_row.actor,
            "correlation_id": attempt_row.correlation_id,
            "started_at": attempt_row.started_at,
            "lease_expires_at": attempt_row.lease_expires_at,
        }
        self._require_product_condition(
            attempt_row.project_id == project_id
            and attempt_row.attempt_fingerprint
            == workflow_node_attempt_fingerprint(identity)
            and canonical_hash(normalized_input) == attempt.input_fingerprint
            and attempt_row.input_fingerprint == attempt.input_fingerprint
            and canonical_json(structuring_input.model_dump(mode="json"))
            == attempt_row.normalized_input_json,
            "Specification candidate producer input changed after its attempt.",
        )
        return structuring_input

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
            item_id: item.candidate_fingerprint for item_id, item in candidates.items()
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
                row.candidate_fingerprint,
                candidate_fingerprints,
                "Specification decision",
            )
            fact = SpecificationDecisionFact(
                specification_decision_id=identifier,
                specification_candidate_id=row.specification_candidate_id,
                candidate_fingerprint=row.candidate_fingerprint,
                decision=self._product_goal_decision(row.decision),
                rationale=row.rationale,
                reviewer=row.reviewer,
                idempotency_key=row.idempotency_key,
                decided_at=row.decided_at,
            )
            candidate = candidates.get(row.specification_candidate_id)
            self._require_product_condition(
                candidate is not None and candidate.recorded_at < fact.decided_at,
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
            facts[identifier] = fact
        return facts

    def _spec_versions_with_lineage(
        self,
        spec_versions: tuple[SpecVersionFact, ...],
        product_definition: _ProductDefinitionFactLoad,
    ) -> tuple[SpecVersionFact, ...]:
        """Validate each registry fact against its exact accepted decision fact."""
        candidates = {
            item.specification_candidate_id: item
            for item in product_definition.specification_candidates
        }
        decisions = {
            item.specification_decision_id: item
            for item in product_definition.specification_decisions
        }
        for item in spec_versions:
            candidate = candidates.get(item.source_specification_candidate_id)
            decision = decisions.get(item.source_specification_decision_id)
            self._require_product_condition(
                candidate is not None,
                "Specification registry source candidate is missing.",
            )
            self._require_product_condition(
                decision is not None
                and decision.decision == "accepted"
                and decision.specification_candidate_id
                == item.source_specification_candidate_id
                and decision.candidate_fingerprint
                == item.source_specification_candidate_fingerprint,
                "Specification registry accepted decision changed.",
            )
            self._require_product_condition(
                candidate is not None
                and item.source_specification_candidate_fingerprint
                == candidate.candidate_fingerprint
                and item.spec_hash == candidate.payload_fingerprint,
                "Specification registry candidate identity changed.",
            )
        return spec_versions

    def _phase_artifacts(
        self,
        project_id: int,
    ) -> _PhaseArtifactLoad:
        """Load immutable Backlog artifacts bound directly to Specifications."""
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

        superseded_backlog_ids = self._superseded_accepted_ids(
            tuple(
                ArtifactLineageNode(
                    artifact_id=artifact_id,
                    chain_key=(
                        project_id,
                        row.product_goal_artifact_id,
                        row.product_goal_fingerprint,
                        row.spec_version_id,
                        row.spec_hash,
                    ),
                    version_number=row.version_number,
                    supersedes_artifact_id=row.supersedes_backlog_artifact_id,
                    decision=self._lineage_decision(
                        None
                        if artifact_id not in backlog_decisions_by_id
                        else backlog_decisions_by_id[artifact_id].decision
                    ),
                )
                for artifact_id, row in backlog_by_id.items()
            )
        )
        facts: list[PhaseArtifactFact] = []
        for artifact_id, row in backlog_by_id.items():
            self._validate_phase_artifact(
                artifact_id=artifact_id,
                artifact=row,
                known_artifact_ids=frozenset(backlog_by_id),
                label="Backlog",
            )
            decision = backlog_decisions_by_id.get(artifact_id)
            facts.append(
                PhaseArtifactFact(
                    artifact_type="backlog",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.content_fingerprint,
                    version_number=row.version_number,
                    spec_version_id=row.spec_version_id,
                    spec_hash=row.spec_hash,
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

    def _validate_phase_artifact(
        self,
        *,
        artifact_id: int,
        artifact: BacklogArtifact,
        known_artifact_ids: frozenset[int],
        label: str,
    ) -> None:
        self._validated_backlog_artifact_content(artifact, label=label)
        supersedes_artifact_id = artifact.supersedes_backlog_artifact_id
        if supersedes_artifact_id is not None and (
            supersedes_artifact_id not in known_artifact_ids
            or supersedes_artifact_id >= artifact_id
        ):
            message = f"{label} artifact supersession is invalid."
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _superseded_accepted_ids(
        nodes: tuple[ArtifactLineageNode, ...],
    ) -> frozenset[int]:
        """Delegate accepted-history displacement to the lineage service."""
        try:
            return accepted_ancestor_ids(nodes)
        except PlanningLineageError as exc:
            message = "Stored planning artifact lineage is invalid."
            raise WorkflowFactRepository._error(message) from exc

    @staticmethod
    def _lineage_decision(decision: str | None) -> PlanningLineageDecision:
        """Narrow one persisted terminal outcome for lineage projection."""
        if decision is None or decision in {"accepted", "feedback", "rejected"}:
            return cast("PlanningLineageDecision", decision)
        message = "Stored planning artifact decision is invalid."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _phase_status(decision: str | None, *, superseded: bool) -> _PhaseStatus:
        if superseded:
            return "superseded"
        if decision is None:
            return "pending_review"
        return WorkflowFactRepository._review_outcome(decision)

    def _backlog_items(
        self,
        project_id: int,
        phase_artifacts: tuple[PhaseArtifactFact, ...],
    ) -> tuple[BacklogItemFact, ...]:
        """Load immutable host-minted Backlog item identities without prose keys."""
        phase_by_id = {
            item.artifact_id: item
            for item in phase_artifacts
            if item.artifact_type == "backlog"
        }
        rows = self._session.exec(
            select(BacklogArtifact)
            .where(col(BacklogArtifact.project_id) == project_id)
            .order_by(col(BacklogArtifact.backlog_artifact_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[BacklogItemFact] = []
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
                message = "Backlog items do not match their artifact fact."
                raise self._error(message)
            content = self._validated_backlog_artifact_content(
                row,
                label="Backlog",
            )
            facts.extend(
                BacklogItemFact(
                    backlog_item_id=item.backlog_item_id,
                    backlog_artifact_id=artifact_id,
                    backlog_artifact_fingerprint=row.content_fingerprint,
                    item_fingerprint=canonical_hash(item.model_dump(mode="json")),
                    spec_item_ids=item.spec_item_ids,
                    priority=item.priority,
                )
                for item in content.backlog_items
            )
        return tuple(
            sorted(
                facts,
                key=lambda item: (
                    item.backlog_artifact_id,
                    item.backlog_item_id,
                ),
            )
        )

    def _validated_backlog_artifact_content(
        self,
        artifact: BacklogArtifact,
        *,
        label: str,
    ) -> BacklogOutput:
        """Load one exact pinned Backlog and its strict host-owned content."""
        try:
            specification = load_accepted_specification(
                self._session,
                project_id=artifact.project_id,
                spec_version_id=artifact.spec_version_id,
                spec_hash=artifact.spec_hash,
            )
        except AcceptedSpecificationIntegrityError as exc:
            message = f"Stored canonical {label} artifact content is invalid."
            raise self._error(message) from exc
        registry = self._session.get(SpecRegistry, artifact.spec_version_id)
        if registry is None or (
            registry.project_id,
            registry.spec_hash,
            registry.source_product_goal_artifact_id,
            registry.source_product_goal_fingerprint,
        ) != (
            artifact.project_id,
            artifact.spec_hash,
            artifact.product_goal_artifact_id,
            artifact.product_goal_fingerprint,
        ):
            message = f"Stored canonical {label} artifact root lineage is invalid."
            raise self._error(message)
        try:
            _canonical_content, content = load_stored_backlog_planning_content(
                artifact.canonical_content_json,
                expected_fingerprint=artifact.content_fingerprint,
                specification=specification,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            message = f"Stored canonical {label} artifact content is invalid."
            raise self._error(message) from exc
        return content

    @staticmethod
    def _canonical_specification_envelope(
        canonical_envelope_json: str,
        *,
        expected_candidate_fingerprint: str,
    ) -> tuple[
        dict[str, JsonValue],
        SpecificationPayload,
        SpecificationCandidateEnvelope,
    ]:
        """Validate one canonical v2 payload/envelope wrapper and its identity."""
        try:
            payload, envelope = load_candidate_contract(
                canonical_envelope_json,
                expected_candidate_fingerprint=expected_candidate_fingerprint,
            )
            stored = _JSON_OBJECT.validate_json(canonical_envelope_json)
        except (TypeError, ValueError, ValidationError) as exc:
            message = "Stored canonical specification candidate envelope is invalid."
            raise WorkflowFactRepository._error(message) from exc
        if canonical_candidate_json(payload, envelope) != canonical_envelope_json:
            message = "Stored canonical specification candidate envelope changed."
            raise WorkflowFactRepository._error(message)
        return stored, payload, envelope

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
        indexes: _PlanningIndexes,
        decisions: _PlanningDecisionLoad,
    ) -> tuple[PlanningArtifactFact, ...]:
        superseded = self._superseded_accepted_ids(
            tuple(
                ArtifactLineageNode(
                    artifact_id=artifact_id,
                    chain_key=(
                        row.project_id,
                        row.backlog_artifact_id,
                        row.backlog_artifact_fingerprint,
                    ),
                    version_number=row.version_number,
                    supersedes_artifact_id=row.supersedes_roadmap_artifact_id,
                    decision=self._lineage_decision(
                        None
                        if artifact_id not in decisions.roadmaps
                        else decisions.roadmaps[artifact_id].decision
                    ),
                )
                for artifact_id, row in indexes.roadmaps.items()
            )
        )
        facts: list[PlanningArtifactFact] = []
        for artifact_id, row in indexes.roadmaps.items():
            backlog = indexes.backlogs.get(row.backlog_artifact_id)
            if (
                backlog is None
                or backlog.project_id != row.project_id
                or backlog.content_fingerprint != row.backlog_artifact_fingerprint
            ):
                message = "Roadmap artifact source Backlog changed."
                raise self._error(message)
            backlog_content = self._validated_backlog_artifact_content(
                backlog,
                label="Roadmap source Backlog",
            )
            try:
                load_stored_roadmap_planning_content(
                    row.canonical_content_json,
                    expected_fingerprint=row.content_fingerprint,
                    parent_backlog_item_ids=tuple(
                        item.backlog_item_id for item in backlog_content.backlog_items
                    ),
                )
            except (TypeError, ValidationError, ValueError) as exc:
                message = "Stored canonical Roadmap artifact content is invalid."
                raise self._error(message) from exc
            decision = decisions.roadmaps.get(artifact_id)
            facts.append(
                PlanningArtifactFact(
                    artifact_type="roadmap",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.content_fingerprint,
                    source_artifact_id=row.backlog_artifact_id,
                    source_fingerprint=row.backlog_artifact_fingerprint,
                    version_number=row.version_number,
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
        indexes: _PlanningIndexes,
        decisions: _PlanningDecisionLoad,
    ) -> tuple[PlanningArtifactFact, ...]:
        superseded = self._superseded_accepted_ids(
            tuple(
                ArtifactLineageNode(
                    artifact_id=artifact_id,
                    chain_key=(
                        row.project_id,
                        row.source_backlog_artifact_id,
                        row.backlog_item_id,
                    ),
                    version_number=row.version_number,
                    supersedes_artifact_id=row.supersedes_story_artifact_id,
                    decision=self._lineage_decision(
                        None
                        if artifact_id not in decisions.stories
                        else decisions.stories[artifact_id].decision
                    ),
                )
                for artifact_id, row in indexes.stories.items()
            )
        )
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
            story_item_ids = self._canonical_string_list(
                row.story_item_ids_json, "Story artifact item IDs"
            )
            decision = decisions.stories.get(artifact_id)
            facts.append(
                PlanningArtifactFact(
                    artifact_type="story",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.content_fingerprint,
                    source_artifact_id=row.roadmap_artifact_id,
                    source_fingerprint=row.roadmap_artifact_fingerprint,
                    version_number=row.version_number,
                    backlog_artifact_id=row.source_backlog_artifact_id,
                    backlog_artifact_fingerprint=(
                        row.source_backlog_artifact_fingerprint
                    ),
                    roadmap_artifact_id=row.roadmap_artifact_id,
                    roadmap_artifact_fingerprint=row.roadmap_artifact_fingerprint,
                    backlog_item_id=row.backlog_item_id,
                    story_item_ids=story_item_ids,
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

    def _canonical_ordered_story_ids(
        self,
        raw_json: str,
        label: str,
    ) -> tuple[int, ...]:
        try:
            story_ids = tuple(_INT_LIST.validate_json(raw_json))
        except ValidationError as exc:
            message = f"{label} IDs are invalid."
            raise self._error(message) from exc
        if (
            not story_ids
            or len(story_ids) != len(set(story_ids))
            or canonical_json(list(story_ids)) != raw_json
        ):
            message = f"{label} IDs are not canonical."
            raise self._error(message)
        return story_ids

    def _sprint_planning_facts(
        self,
        indexes: _PlanningIndexes,
        decisions: _PlanningDecisionLoad,
    ) -> tuple[PlanningArtifactFact, ...]:
        superseded = self._superseded_accepted_ids(
            tuple(
                ArtifactLineageNode(
                    artifact_id=artifact_id,
                    chain_key=(
                        row.project_id,
                        row.spec_version_id,
                        row.spec_hash,
                        row.sprint_plan_stream_id,
                    ),
                    version_number=row.version_number,
                    supersedes_artifact_id=row.supersedes_sprint_plan_artifact_id,
                    decision=self._lineage_decision(
                        None
                        if artifact_id not in decisions.sprint_plans
                        else decisions.sprint_plans[artifact_id].decision
                    ),
                )
                for artifact_id, row in indexes.sprint_plans.items()
            )
        )
        facts: list[PlanningArtifactFact] = []
        for artifact_id, row in indexes.sprint_plans.items():
            try:
                envelope = load_bound_sprint_plan_envelope(
                    row.canonical_task_plan_json,
                    expected_fingerprint=row.plan_fingerprint,
                    spec_version_id=row.spec_version_id,
                    spec_hash=row.spec_hash,
                    candidate_set_fingerprint=row.candidate_set_fingerprint,
                    selected_story_ids_json=row.selected_story_ids_json,
                )
            except (ValidationError, ValueError) as exc:
                message = "Sprint plan task content is invalid."
                raise self._error(message) from exc
            story_ids = self._canonical_ordered_story_ids(
                row.selected_story_ids_json,
                "Sprint plan selected Story",
            )
            if (
                tuple(
                    item.story_id for item in envelope.planner_output.selected_stories
                )
                != story_ids
            ):
                message = "Sprint plan envelope does not match durable columns."
                raise self._error(message)
            decision = decisions.sprint_plans.get(artifact_id)
            facts.append(
                PlanningArtifactFact(
                    artifact_type="sprint_plan",
                    artifact_id=artifact_id,
                    artifact_fingerprint=row.plan_fingerprint,
                    version_number=row.version_number,
                    source_fingerprint=row.candidate_set_fingerprint,
                    spec_version_id=row.spec_version_id,
                    spec_hash=row.spec_hash,
                    sprint_plan_stream_id=row.sprint_plan_stream_id,
                    selected_story_ids=story_ids,
                    activated_sprint_id=(
                        None if decision is None else decision.activated_sprint_id
                    ),
                    candidate_set_fingerprint=row.candidate_set_fingerprint,
                    task_content_fingerprint=planned_task_content_fingerprint(
                        envelope.planner_output,
                        spec_version_id=row.spec_version_id,
                        spec_hash=row.spec_hash,
                        sprint_plan_stream_id=row.sprint_plan_stream_id,
                        sprint_plan_artifact_id=artifact_id,
                        sprint_plan_fingerprint=row.plan_fingerprint,
                    ),
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
            *self._roadmap_planning_facts(indexes, decisions),
            *self._story_planning_facts(indexes, decisions),
            *self._sprint_planning_facts(indexes, decisions),
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
                or plan.activated_sprint_id != row.sprint_id
                or plan.artifact_fingerprint != row.plan_fingerprint
                or plan.candidate_set_fingerprint != row.candidate_set_fingerprint
                or plan.selected_story_ids != selected_story_ids
                or plan.task_content_fingerprint != row.task_content_fingerprint
                or decision is None
                or decision.project_id != project_id
                or decision.sprint_plan_artifact_id != row.sprint_plan_artifact_id
                or decision.plan_fingerprint != row.plan_fingerprint
                or decision.decision != "accepted"
                or decision.activated_sprint_id != row.sprint_id
                or plan.spec_version_id is None
                or plan.spec_hash is None
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
                    spec_version_id=plan.spec_version_id,
                    spec_hash=plan.spec_hash,
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
        accepted_by_id: dict[int, PlanningArtifactFact] = {}
        for artifact in planning_artifacts:
            if artifact.artifact_type != "story" or artifact.status != "accepted":
                continue
            if artifact.backlog_item_id is None or not artifact.story_item_ids:
                message = "Accepted Story artifact has no immutable item identity."
                raise self._error(message)
            if artifact.artifact_id in accepted_by_id:
                message = "Accepted Story artifact identity is duplicated."
                raise self._error(message)
            accepted_by_id[artifact.artifact_id] = artifact
        return accepted_by_id

    def _validate_story_relationships(
        self,
        rows: tuple[UserStory, ...],
        dependencies: tuple[UserStoryDependency, ...],
        story_ids: frozenset[int],
        spec_versions: dict[int, str],
    ) -> None:
        for row in rows:
            self._require_fingerprint_reference(
                row.accepted_spec_version_id,
                row.accepted_spec_hash,
                spec_versions,
                "story accepted specification",
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
        validation_status: Literal["validated", "failed", "unvalidated"] = "unvalidated"
        validation_failures: tuple[JsonObject, ...] = ()
        if row.validation_evidence is not None:
            try:
                ev = ValidationEvidence.model_validate_json(row.validation_evidence)
                if not blockers and ev.ready_for_sprint:
                    validation_status = "validated"
                else:
                    validation_status = "failed"
                    validation_failures = tuple(
                        f.model_dump(mode="json") for f in ev.structural_failures
                    )
            except (ValidationError, ValueError):
                validation_status = "failed"

        return StoryFact(
            story_id=story_id,
            source_story_artifact_id=row.source_story_artifact_id,
            source_story_artifact_fingerprint=row.source_story_artifact_fingerprint,
            source_story_item_id=row.source_story_item_id,
            source_story_item_fingerprint=row.source_story_item_fingerprint,
            accepted_spec_version_id=row.accepted_spec_version_id,
            accepted_spec_hash=row.accepted_spec_hash,
            spec_item_ids=WorkflowFactRepository._canonical_string_list(
                row.spec_item_ids_json, "Story Specification item IDs"
            ),
            content_fingerprint=row.source_story_item_fingerprint,
            content_accepted=artifact is not None,
            story_artifact_id=row.source_story_artifact_id,
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
            backlog_item_id=(
                artifact.backlog_item_id if artifact is not None else None
            ),
            status=row.status.value,
            story_points=row.story_points,
            rank=row.rank,
            sprint_ids=sprint_ids,
            sprint_candidate=not blockers,
            readiness_blockers=blockers,
            validation_status=validation_status,
            validation_failures=validation_failures,
        )

    def _stories(
        self,
        project_id: int,
        spec_versions: dict[int, str],
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
        accepted_artifacts = self._accepted_story_artifacts(planning_artifacts)
        self._validate_story_relationships(
            rows,
            dependencies,
            story_ids,
            spec_versions,
        )
        blockers = self._story_readiness_blockers(rows, dependencies, stories_by_id)
        return tuple(
            self._story_fact(
                row,
                story_id,
                (
                    artifact
                    if (
                        (
                            artifact := accepted_artifacts.get(
                                row.source_story_artifact_id
                            )
                        )
                        is not None
                        and row.source_story_item_id in artifact.story_item_ids
                        and row.source_story_artifact_fingerprint
                        == artifact.artifact_fingerprint
                    )
                    else None
                ),
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
        planning_artifacts: tuple[PlanningArtifactFact, ...],
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
        sprint_plans = {
            item.artifact_id: item
            for item in planning_artifacts
            if item.artifact_type == "sprint_plan"
        }
        for task, _story in rows:
            task_id = self._required_id(task.task_id, "task")
            if not task.description.strip():
                message = f"Task {task_id} has an empty description."
                raise self._error(message)
            if task.metadata_json is None:
                message = f"Task {task_id} has no canonical metadata."
                raise self._error(message)
            try:
                metadata = parse_task_metadata(task.metadata_json)
            except (ValidationError, ValueError) as exc:
                message = f"Task {task_id} metadata is invalid."
                raise self._error(message) from exc
            canonical_metadata = serialize_task_metadata(metadata)
            if canonical_metadata != task.metadata_json:
                message = f"Task {task_id} metadata is not canonical."
                raise self._error(message)
            task_sprint_ids = sprint_ids_by_story[task.story_id]
            plan = sprint_plans.get(metadata.sprint_plan_artifact_id)
            if (
                plan is None
                or plan.activated_sprint_id is None
                or plan.activated_sprint_id not in task_sprint_ids
                or plan.spec_version_id != metadata.spec_version_id
                or plan.spec_hash != metadata.spec_hash
                or plan.sprint_plan_stream_id != metadata.sprint_plan_stream_id
                or plan.artifact_fingerprint != metadata.sprint_plan_fingerprint
                or task.story_id not in plan.selected_story_ids
            ):
                message = f"Task {task_id} metadata plan lineage is invalid."
                raise self._error(message)
            if not task_sprint_ids:
                message = (
                    "Forced relationship corruption in task sprint relationship: "
                    f"task {task_id} has no sprint membership."
                )
                raise self._error(message)
            story = stories_by_id[task.story_id]
            facts.append(
                TaskFact(
                    task_id=task_id,
                    sprint_id=plan.activated_sprint_id,
                    story_id=task.story_id,
                    description=task.description,
                    metadata_json=canonical_metadata,
                    status=task.status.value,
                    dependencies_satisfied=not story.readiness_blockers,
                )
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
                failure_code=(
                    None
                    if (
                        outcome := outcomes_by_attempt.get(
                            self._required_id(
                                row.workflow_node_attempt_id,
                                "workflow node attempt",
                            )
                        )
                    )
                    is None
                    else outcome.failure_code
                ),
            )
            for row in attempts
        )

    def _node_attempt_lookup(self, project_id: int) -> dict[int, str]:
        """Return exact attempt fingerprints for narrow input projections."""
        return {
            self._required_id(row.workflow_node_attempt_id, "workflow node attempt"): (
                row.attempt_fingerprint
            )
            for row in self._session.exec(
                select(WorkflowNodeAttempt)
                .where(col(WorkflowNodeAttempt.project_id) == project_id)
                .order_by(col(WorkflowNodeAttempt.workflow_node_attempt_id)),
                execution_options=self._query_options(),
            ).all()
        }

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
    def _canonical_json_object_list(
        content: str,
        label: str,
    ) -> tuple[dict[str, JsonValue], ...]:
        """Decode one canonical JSON object list retained without a content hash."""
        try:
            value = _JSON_OBJECT_LIST.validate_json(content)
        except ValidationError as exc:
            message = f"{label} JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc
        if canonical_json(value) != content:
            message = f"{label} JSON is not canonical."
            raise WorkflowFactRepository._error(message)
        return tuple(value)

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
    def _vision_operation(value: str) -> _VisionOperation:
        if value == "bootstrap":
            return "bootstrap"
        if value == "clarification":
            return "clarification"
        if value == "revision":
            return "revision"
        message = f"Invalid Vision operation {value!r}."
        raise WorkflowFactRepository._error(message)

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

    def _story_readiness_blockers(
        self,
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
            try:
                require_story_ready_for_sprint(self._session, story=story)
            except StoryValidationReadinessError:
                blockers[story_id].append("STORY_VALIDATION_REQUIRED")
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
    evidence_snapshots: tuple[VisionEvidenceSnapshotFact, ...]
    interview_turns: tuple[VisionInterviewTurnFact, ...]


@dataclass(frozen=True)
class VisionInputSelection:
    """Current Vision chain state without expanding to a workflow snapshot."""

    generation_operation: _VisionOperation
    accepted_vision: VisionArtifactFact | None
    revision_intent_id: int | None
    prior_turn: VisionInterviewTurnFact | None
    evidence_snapshot: VisionEvidenceSnapshotFact | None


class VisionInputFactRepository(WorkflowFactRepository):
    """Read the narrow, canonical fact projection used by Vision host input."""

    def load_context(self, project_id: int) -> VisionInputContext:
        """Load Project identity and validated durable Vision rows."""
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
            attempts = self._node_attempt_lookup(project_id)
            snapshots = self._vision_evidence_snapshots(project_id, attempts)
            turns = self._vision_interview_turns(project_id, revisions, attempts)
            self._validate_vision_artifact_sources(visions, turns)
        return VisionInputContext(
            project=project,
            project_description=project_description,
            vision_artifacts=tuple(visions.values()),
            vision_decisions=tuple(decisions.values()),
            revision_intents=tuple(revisions.values()),
            evidence_snapshots=snapshots,
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


def _active_vision_snapshot_descendant(
    snapshots: tuple[VisionEvidenceSnapshotFact, ...],
    root_id: int,
) -> int:
    """Follow the explicit supersession chain to one active snapshot leaf."""
    children = {
        item.supersedes_vision_evidence_snapshot_id: item.vision_evidence_snapshot_id
        for item in snapshots
        if item.supersedes_vision_evidence_snapshot_id is not None
    }
    current = root_id
    visited: set[int] = set()
    while current in children:
        if current in visited:
            message = "Vision evidence snapshot supersession is cyclic."
            raise WorkflowFactLoadError(message)
        visited.add(current)
        current = children[current]
    return current


@dataclass(frozen=True)
class _VisionGenerationLineage:
    """Exact generation lineage selected from durable Vision facts."""

    operation: _VisionOperation
    accepted_vision: VisionArtifactFact | None
    revision_intent_id: int | None
    snapshot_id: int | None


def _current_vision_artifact(
    context: VisionInputContext,
) -> VisionArtifactFact | None:
    children = {
        item.supersedes_vision_artifact_id
        for item in context.vision_artifacts
        if item.supersedes_vision_artifact_id is not None
    }
    current = tuple(
        item
        for item in context.vision_artifacts
        if item.vision_artifact_id not in children
    )
    if len(current) > 1:
        message = "Vision facts are ambiguous."
        raise WorkflowFactLoadError(message)
    return current[0] if current else None


def _vision_decisions_by_artifact(
    context: VisionInputContext,
) -> dict[int, VisionArtifactDecisionFact]:
    decisions = {item.vision_artifact_id: item for item in context.vision_decisions}
    if len(decisions) != len(context.vision_decisions):
        message = "Vision facts are ambiguous."
        raise WorkflowFactLoadError(message)
    return decisions


def _open_vision_revision(
    context: VisionInputContext,
) -> VisionRevisionIntentFact | None:
    completed_turn_ids = {
        item.source_interview_turn_id for item in context.vision_artifacts
    }
    open_intents = tuple(
        item
        for item in context.revision_intents
        if not any(
            turn.revision_intent_id == item.vision_revision_intent_id
            and turn.vision_interview_turn_id in completed_turn_ids
            for turn in context.interview_turns
        )
    )
    if len(open_intents) > 1:
        message = "Vision facts are ambiguous."
        raise WorkflowFactLoadError(message)
    return open_intents[0] if open_intents else None


def _reviewed_vision_lineage(
    context: VisionInputContext,
    *,
    artifacts_by_id: dict[int, VisionArtifactFact],
    artifact: VisionArtifactFact,
) -> _VisionGenerationLineage:
    source_turn = next(
        (
            item
            for item in context.interview_turns
            if item.vision_interview_turn_id == artifact.source_interview_turn_id
        ),
        None,
    )
    if source_turn is None:
        message = "Vision reviewed artifact source turn is missing."
        raise WorkflowFactLoadError(message)
    intent_id = source_turn.revision_intent_id
    accepted_vision = None
    if intent_id is not None:
        revision = next(
            (
                item
                for item in context.revision_intents
                if item.vision_revision_intent_id == intent_id
            ),
            None,
        )
        if revision is None:
            message = "Vision reviewed artifact revision intent is missing."
            raise WorkflowFactLoadError(message)
        accepted_vision = artifacts_by_id[revision.source_vision_artifact_id]
    return _VisionGenerationLineage(
        operation="revision" if intent_id is not None else "bootstrap",
        accepted_vision=accepted_vision,
        revision_intent_id=intent_id,
        snapshot_id=_active_vision_snapshot_descendant(
            context.evidence_snapshots,
            source_turn.vision_evidence_snapshot_id,
        ),
    )


def _vision_generation_lineage(
    context: VisionInputContext,
    *,
    artifacts_by_id: dict[int, VisionArtifactFact],
    artifact: VisionArtifactFact | None,
    decisions: dict[int, VisionArtifactDecisionFact],
    revision: VisionRevisionIntentFact | None,
) -> _VisionGenerationLineage:
    if revision is not None:
        return _VisionGenerationLineage(
            operation="revision",
            accepted_vision=artifacts_by_id[revision.source_vision_artifact_id],
            revision_intent_id=revision.vision_revision_intent_id,
            snapshot_id=None,
        )
    if artifact is None:
        return _VisionGenerationLineage("bootstrap", None, None, None)
    decision = decisions.get(artifact.vision_artifact_id)
    if decision is None:
        return _VisionGenerationLineage("bootstrap", None, None, None)
    if decision.decision == "accepted":
        message = "Accepted Vision requires an explicit revision intent."
        raise WorkflowFactLoadError(message)
    return _reviewed_vision_lineage(
        context,
        artifacts_by_id=artifacts_by_id,
        artifact=artifact,
    )


def _vision_lineage_leaf(
    context: VisionInputContext,
    lineage: _VisionGenerationLineage,
) -> VisionInterviewTurnFact | None:
    superseded_snapshot_ids = {
        item.supersedes_vision_evidence_snapshot_id
        for item in context.evidence_snapshots
        if item.supersedes_vision_evidence_snapshot_id is not None
    }
    turns = tuple(
        item
        for item in context.interview_turns
        if item.revision_intent_id == lineage.revision_intent_id
        and item.vision_evidence_snapshot_id not in superseded_snapshot_ids
        and (
            lineage.snapshot_id is None
            or item.vision_evidence_snapshot_id == lineage.snapshot_id
        )
    )
    prior_ids = {item.prior_turn_id for item in turns if item.prior_turn_id is not None}
    leaves = tuple(
        item for item in turns if item.vision_interview_turn_id not in prior_ids
    )
    if len(leaves) > 1:
        message = "Vision interview turn chain is ambiguous."
        raise WorkflowFactLoadError(message)
    return leaves[0] if leaves else None


def _vision_lineage_snapshot(
    context: VisionInputContext,
    prior_turn: VisionInterviewTurnFact | None,
) -> VisionEvidenceSnapshotFact | None:
    if prior_turn is None:
        return None
    matches = tuple(
        item
        for item in context.evidence_snapshots
        if item.vision_evidence_snapshot_id == prior_turn.vision_evidence_snapshot_id
    )
    if len(matches) != 1:
        message = "Vision evidence snapshot is ambiguous."
        raise WorkflowFactLoadError(message)
    return matches[0]


def select_vision_input(context: VisionInputContext) -> VisionInputSelection:
    """Select one current grounded Vision chain from its narrow fact projection."""
    artifacts_by_id = {
        item.vision_artifact_id: item for item in context.vision_artifacts
    }
    artifact = _current_vision_artifact(context)
    decisions = _vision_decisions_by_artifact(context)
    revision = _open_vision_revision(context)
    lineage = _vision_generation_lineage(
        context,
        artifacts_by_id=artifacts_by_id,
        artifact=artifact,
        decisions=decisions,
        revision=revision,
    )
    prior_turn = _vision_lineage_leaf(context, lineage)
    return VisionInputSelection(
        generation_operation=lineage.operation,
        accepted_vision=lineage.accepted_vision,
        revision_intent_id=lineage.revision_intent_id,
        prior_turn=prior_turn,
        evidence_snapshot=_vision_lineage_snapshot(context, prior_turn),
    )
