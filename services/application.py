"""Production application boundary for the durable workflow graph."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Protocol,
    TypedDict,
    Unpack,
    cast,
)

from pydantic import (
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlmodel import Session, col, select

from adapters.adk.model_roles import AGENTIC_MODEL_ROLES
from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import UserStory
from models.product_definition import ProductGoalArtifact, VisionArtifact
from models.specs import CompiledSpecAuthority, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintPlanArtifact,
    StoryArtifact,
    StoryArtifactDecision,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.authority_compilation_input import AuthorityCompilationInputService
from services.authority_review_projection import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.contracts.backlog import InputSchema as BacklogInput
from services.contracts.backlog import OutputSchema as BacklogOutput
from services.contracts.product_goal import ProductGoalInterviewInput
from services.contracts.roadmap import RoadmapBuilderInput, RoadmapBuilderOutput
from services.contracts.sprint import (
    SprintPlannerInput,
    SprintPlannerStory,
)
from services.contracts.story import UserStoryWriterInput, UserStoryWriterOutput
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    DurableTransitionReplayService,
    NodeAttemptReplayQuery,
    TransitionReplayQuery,
)
from services.phases.sprint_metrics import build_durable_sprint_metrics
from services.product_goal_interview_input import ProductGoalInterviewInputService
from services.project_lifecycle import (
    CreateProjectCommand,
    ProjectLifecycleService,
    RepositoryAttachmentCommand,
    RepositoryBindingReplayCommand,
    RepositoryRefreshCommand,
)
from services.roadmap_runtime import build_roadmap_input_context
from services.sprint_selection import (
    SprintSelectionError,
    derive_group_slot,
    derive_parent_group,
    select_sprint_story_rows,
)
from services.story_linkage import normalize_requirement_key
from services.story_rank import parse_story_rank, story_rank_is_valid
from services.story_runtime import build_story_input_context
from services.vision_input import VisionInputService
from utils.model_config import get_model_id
from utils.spec_schemas import ValidationEvidence
from workflow.contracts import (
    Blocker,
    FactReference,
    FrozenModel,
    JsonObject,
    NodeCategory,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    readiness_fingerprint,
    story_dependency_source_fingerprint,
)
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    sprint_close_fingerprint,
    sprint_review_fingerprint,
    story_completion_eligibility_fingerprint,
)
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import (
    AbandonProductGoal,
    ApplyStoryDependencies,
    BeginVisionRevision,
    CloseSprint,
    CloseStory,
    CompleteTask,
    DecideAuthority,
    DecideBacklog,
    DecideProductGoalReview,
    DecideRoadmap,
    DecideSpecification,
    DecideSprintPlan,
    DecideStory,
    DecideVisionReview,
    FulfillProductGoal,
    RecordAuthorityFeedback,
    RecordDiscoveryArtifact,
    RecordPostSprintTriage,
    RecordSpecificationCandidate,
    RepairStoryReadiness,
    ReviewSprint,
    StartSprint,
)
from workflow.requests.planning import ReviewedDependencyEdge, StoryReadinessUpdate

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent
    from sqlalchemy.engine import Engine

    from adapters.adk.recipes import AdkRecipeRegistry
    from workflow.facts import (
        PostSprintTriageFact,
        StoryDependencyFact,
        StoryFact,
        WorkflowFactSnapshot,
    )
    from workflow.requests import TransitionRequest

_JSON_OBJECT = TypeAdapter(JsonObject)
type DeliveryReviewFactType = Literal[
    "backlog",
    "roadmap",
    "story",
    "sprint_plan",
]


class WorkflowDomainPort(Protocol):
    """Only workflow authority exposed to application adapters."""

    def position(self, project_id: int) -> WorkflowPosition:
        """Derive current position from durable facts."""
        ...

    def transition(self, request: TransitionRequest) -> TransitionResult:
        """Apply one exact typed transition."""
        ...

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        """Load trusted input for a durable node attempt."""
        ...


class _VisionInputPort(Protocol):
    """Host preparation for Project Vision bootstrap and clarification."""

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None: ...

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None: ...

    def build_bootstrap(
        self,
        project_id: int,
        decision: NodeDecision,
    ) -> JsonObject: ...

    def build_clarification(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject: ...


class _ProductGoalInterviewInputPort(Protocol):
    """Host preparation for a Product Goal interview turn."""

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None: ...

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None: ...

    def build(
        self,
        project_id: int,
        decision: NodeDecision,
        user_text: str,
    ) -> JsonObject: ...


class _ProductDiscoverySelectionPort(Protocol):
    """Resolve replacement specification lineage from canonical durable facts."""

    def resolve_specification_supersedes(self, project_id: int) -> int | None: ...


class _AuthorityCompilationInputPort(Protocol):
    """Host preparation for one authority compilation attempt."""

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None: ...

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        compiler_model: str,
    ) -> JsonObject: ...


class _AuthorityReviewSelectionPort(Protocol):
    """Resolve the exact durable authority review token internally."""

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None: ...

    def review_identity(
        self,
        *,
        project_id: int,
    ) -> tuple[int, str, str] | None: ...


class _AuthorityRepairInputPort(Protocol):
    """Host preparation for one authority repair attempt."""

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None: ...

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> JsonObject | None: ...


class SemanticTransitionReplayPort(Protocol):
    """Reusable replay boundary for semantic non-agentic transitions."""

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        """Replay exact semantics without reading the current graph position."""
        ...


class _DeliveryReviewSelectionPort(SemanticTransitionReplayPort, Protocol):
    """Resolve replay and durable artifact identity for delivery reviews."""

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None: ...

    def review_identity(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        fact_type: DeliveryReviewFactType,
    ) -> tuple[int, str] | None: ...


class _DeliveryActionInputPort(Protocol):
    """Host preparation for retained delivery-generation actions."""

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None: ...

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        node_id: str,
    ) -> JsonObject | None: ...


class _PlanningActionSelectionPort(SemanticTransitionReplayPort, Protocol):
    """Derive guarded planning-action identities from exact durable facts."""

    def prepare_story_dependencies(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        selected_story_ids: tuple[int, ...],
    ) -> str | None: ...

    def prepare_story_readiness(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        repair_story_ids: tuple[int, ...],
    ) -> tuple[tuple[int, ...], str] | None: ...

    def prepare_sprint_start(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int, str, str] | None: ...


class _ExecutionActionSelectionPort(SemanticTransitionReplayPort, Protocol):
    """Derive exact execution identities from decisions and durable facts."""

    def prepare_task_completion(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int] | None: ...

    def prepare_story_close(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int, str] | None: ...

    def prepare_sprint_review(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str] | None: ...

    def prepare_sprint_close(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str, str] | None: ...

    def prepare_post_sprint_triage(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str] | None: ...


class _SprintPlanningInputPort(Protocol):
    """Host preparation for one semantic Sprint planning request."""

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None: ...

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        request: SprintPlanningRequest,
    ) -> JsonObject | WorkflowError: ...


@dataclass(frozen=True)
class AuthorityReviewSelectionService:
    """Build one facts-only review identity from durable authority state."""

    engine: Engine

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        """Replay one completed Authority review before reading current facts."""
        return DurableTransitionReplayService(engine=self.engine).replay(query)

    def review_identity(
        self,
        *,
        project_id: int,
    ) -> tuple[int, str, str] | None:
        """Return authority ID, authority fingerprint, and review fingerprint."""
        with Session(self.engine) as session:
            snapshot = build_authority_review_snapshot_in_session(
                session,
                project_id=project_id,
            )
        if not isinstance(snapshot, AuthorityReviewSnapshot):
            return None
        authority_id = snapshot.pending_authority_id
        authority_fingerprint = snapshot.authority_fingerprint
        if authority_id is None or authority_fingerprint is None:
            return None
        return authority_id, authority_fingerprint, snapshot.review_fingerprint


@dataclass(frozen=True)
class AuthorityRepairInputService:
    """Prepare rejected-authority repair input from durable specification facts."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Replay an exact prior repair attempt before reading durable facts."""
        return AuthorityCompilationInputService(engine=self.engine).replay(query)

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> JsonObject | None:
        """Build repair input from the rejected authority's registered spec."""
        reference = _single_fact_reference(decision, "authority")
        if reference is None:
            return None
        try:
            authority_id = int(reference.fact_id)
        except ValueError:
            return None
        with Session(self.engine) as session:
            authority = session.get(CompiledSpecAuthority, authority_id)
            spec = (
                None
                if authority is None
                else session.get(SpecRegistry, authority.spec_version_id)
            )
        if (
            authority is None
            or spec is None
            or spec.project_id != project_id
            or pending_authority_fingerprint(authority) != reference.fingerprint
        ):
            return None
        compile_decision = decision.model_copy(
            update={
                "instance_key": f"spec:{spec.spec_version_id}:{spec.spec_hash}",
                "fact_references": (
                    FactReference(
                        fact_type="spec_version",
                        fact_id=str(spec.spec_version_id),
                        fingerprint=spec.spec_hash,
                    ),
                ),
            }
        )
        payload = AuthorityCompilationInputService(engine=self.engine).build(
            project_id=project_id,
            decision=compile_decision,
            compiler_model=get_model_id(AGENTIC_MODEL_ROLES["authority.repair"]),
        )
        compiler_input = payload.get("compiler_input")
        if not isinstance(compiler_input, dict):
            return None
        return {
            "source_authority_id": authority_id,
            "source_authority_fingerprint": reference.fingerprint,
            "compiler_input": compiler_input,
        }


@dataclass(frozen=True)
class DeliveryReviewSelectionService:
    """Verify graph-selected delivery artifacts against durable rows."""

    engine: Engine

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        """Replay one delivery review before reading its current position."""
        return DurableTransitionReplayService(engine=self.engine).replay(query)

    def review_identity(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        fact_type: DeliveryReviewFactType,
    ) -> tuple[int, str] | None:
        """Return an exact artifact identity only when its durable row matches."""
        target = _integer_fact_reference(decision, fact_type)
        if target is None:
            return None
        artifact_id, reference = target
        with Session(self.engine) as session:
            if fact_type == "backlog":
                artifact = session.get(BacklogArtifact, artifact_id)
                valid = (
                    artifact is not None
                    and artifact.project_id == project_id
                    and artifact.backlog_artifact_id == artifact_id
                    and artifact.content_fingerprint == reference.fingerprint
                )
            elif fact_type == "roadmap":
                artifact = session.get(RoadmapArtifact, artifact_id)
                valid = (
                    artifact is not None
                    and artifact.project_id == project_id
                    and artifact.roadmap_artifact_id == artifact_id
                    and artifact.content_fingerprint == reference.fingerprint
                )
            elif fact_type == "story":
                artifact = session.get(StoryArtifact, artifact_id)
                valid = (
                    artifact is not None
                    and artifact.project_id == project_id
                    and artifact.story_artifact_id == artifact_id
                    and artifact.content_fingerprint == reference.fingerprint
                    and decision.instance_key
                    == f"requirement:{artifact.requirement_id}"
                )
            else:
                artifact = session.get(SprintPlanArtifact, artifact_id)
                valid = (
                    artifact is not None
                    and artifact.project_id == project_id
                    and artifact.sprint_plan_artifact_id == artifact_id
                    and artifact.plan_fingerprint == reference.fingerprint
                )
        return (artifact_id, reference.fingerprint) if valid else None


@dataclass(frozen=True)
class PlanningActionSelectionService:
    """Resolve planning transition guards from graph-selected durable facts."""

    engine: Engine

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        """Replay exact operator semantics before any current fact read."""
        return DurableTransitionReplayService(engine=self.engine).replay(query)

    def prepare_story_dependencies(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        selected_story_ids: tuple[int, ...],
    ) -> str | None:
        """Derive the source fingerprint for the exact selected Story set."""
        snapshot = self._snapshot(project_id)
        if snapshot is None:
            return None
        stories = tuple(
            sorted(
                (item for item in snapshot.stories if item.sprint_candidate),
                key=lambda item: item.story_id,
            )
        )
        expected_ids = tuple(item.story_id for item in stories)
        source_fingerprint = story_dependency_source_fingerprint(stories)
        reference = _single_fact_reference(decision, "story_dependency_source")
        if (
            selected_story_ids != expected_ids
            or reference is None
            or reference.fact_id != str(project_id)
            or reference.fingerprint != source_fingerprint
        ):
            return None
        return source_fingerprint

    def prepare_story_readiness(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        repair_story_ids: tuple[int, ...],
    ) -> tuple[tuple[int, ...], str] | None:
        """Derive exact missing Story IDs and their current readiness guard."""
        snapshot = self._snapshot(project_id)
        if snapshot is None:
            return None
        missing_ids = tuple(
            sorted(
                item.story_id
                for item in snapshot.stories
                if item.sprint_candidate
                and (item.story_points is None or not story_rank_is_valid(item.rank))
            )
        )
        fingerprint = readiness_fingerprint(snapshot.stories)
        reference = _single_fact_reference(decision, "story_readiness")
        if (
            repair_story_ids != missing_ids
            or reference is None
            or reference.fact_id != str(project_id)
            or reference.fingerprint != fingerprint
        ):
            return None
        return missing_ids, fingerprint

    def prepare_sprint_start(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int, str, str] | None:
        """Derive exact accepted plan, Sprint, and candidate identities."""
        snapshot = self._snapshot(project_id)
        if snapshot is None:
            return None
        plan_target = _integer_fact_reference(decision, "sprint_plan")
        candidate_reference = _single_fact_reference(decision, "candidate_set")
        task_reference = _integer_fact_reference(decision, "sprint_plan_tasks")
        if (
            plan_target is None
            or candidate_reference is None
            or candidate_reference.fact_id != str(project_id)
            or task_reference is None
        ):
            return None
        plan_id, plan_reference = plan_target
        sprint_id, task_reference_value = task_reference
        plans = tuple(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
            and item.artifact_id == plan_id
            and item.artifact_fingerprint == plan_reference.fingerprint
            and item.status == "accepted"
        )
        if len(plans) != 1:
            return None
        plan = plans[0]
        stories = tuple(item for item in snapshot.stories if item.sprint_candidate)
        current_candidates = candidate_set_fingerprint(
            stories,
            snapshot.story_dependencies,
        )
        if (
            plan.sprint_id != sprint_id
            or plan.candidate_set_fingerprint != current_candidates
            or candidate_reference.fingerprint != current_candidates
            or plan.task_content_fingerprint != task_reference_value.fingerprint
        ):
            return None
        return plan_id, sprint_id, plan.artifact_fingerprint, current_candidates

    def _snapshot(self, project_id: int) -> WorkflowFactSnapshot | None:
        try:
            with Session(self.engine) as session:
                return WorkflowFactRepository(session).load(project_id)
        except WorkflowFactLoadError:
            return None


@dataclass(frozen=True)
class ExecutionActionSelectionService:
    """Resolve execution targets and guards from exact durable workflow facts."""

    engine: Engine

    def replay_transition(
        self,
        query: TransitionReplayQuery,
    ) -> TransitionResult | None:
        """Replay exact public semantics before reading current execution facts."""
        return DurableTransitionReplayService(engine=self.engine).replay(query)

    def prepare_task_completion(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int] | None:
        """Resolve one selected open Task and its active Sprint."""
        if not execution_action_decision_is_transportable(decision):
            return None
        snapshot = self._snapshot(project_id)
        task_id = _instance_identity(decision, "task")
        active_sprint_id = _single_sprint_id(snapshot, status="active")
        if snapshot is None or task_id is None or active_sprint_id is None:
            return None
        tasks = tuple(
            item
            for item in snapshot.tasks
            if item.task_id == task_id
            and item.sprint_id == active_sprint_id
            and item.status not in {"Done", "Cancelled"}
        )
        reference = _integer_fact_reference(decision, "task")
        if len(tasks) != 1 or reference is None:
            return None
        task = tasks[0]
        referenced_id, fact_reference = reference
        if referenced_id != task_id or fact_reference.fingerprint != canonical_hash(
            task.model_dump(mode="json")
        ):
            return None
        return task_id, active_sprint_id

    def prepare_story_close(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, int, str] | None:
        """Resolve one selected Story and its current completion fingerprint."""
        if not execution_action_decision_is_transportable(decision):
            return None
        snapshot = self._snapshot(project_id)
        story_id = _instance_identity(decision, "story")
        active_sprint_id = _single_sprint_id(snapshot, status="active")
        if snapshot is None or story_id is None or active_sprint_id is None:
            return None
        stories = tuple(
            item
            for item in snapshot.stories
            if item.story_id == story_id
            and active_sprint_id in item.sprint_ids
            and item.status not in {"Done", "Accepted"}
        )
        reference = _integer_fact_reference(decision, "story_completion")
        try:
            fingerprint = story_completion_eligibility_fingerprint(
                snapshot,
                sprint_id=active_sprint_id,
                story_id=story_id,
            )
        except ExecutionIntegrityError:
            return None
        if (
            len(stories) != 1
            or reference is None
            or reference[0] != story_id
            or reference[1].fingerprint != fingerprint
        ):
            return None
        return story_id, active_sprint_id, fingerprint

    def prepare_sprint_review(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str] | None:
        """Resolve the active Sprint and terminal-review fingerprint."""
        if not execution_action_decision_is_transportable(decision):
            return None
        snapshot = self._snapshot(project_id)
        sprint_id = _instance_identity(decision, "sprint")
        active_sprint_id = _single_sprint_id(snapshot, status="active")
        if (
            snapshot is None
            or sprint_id is None
            or sprint_id != active_sprint_id
            or any(item.sprint_id == sprint_id for item in snapshot.sprint_reviews)
        ):
            return None
        try:
            fingerprint = sprint_review_fingerprint(snapshot, sprint_id)
        except ExecutionIntegrityError:
            return None
        reference = _integer_fact_reference(decision, "sprint_review")
        if (
            reference is None
            or reference[0] != sprint_id
            or reference[1].fingerprint != fingerprint
        ):
            return None
        return sprint_id, fingerprint

    def prepare_sprint_close(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str, str] | None:
        """Resolve active Sprint review and close fingerprints."""
        if not execution_action_decision_is_transportable(decision):
            return None
        snapshot = self._snapshot(project_id)
        sprint_id = _instance_identity(decision, "sprint")
        active_sprint_id = _single_sprint_id(snapshot, status="active")
        if snapshot is None or sprint_id is None or sprint_id != active_sprint_id:
            return None
        reviews = tuple(
            item for item in snapshot.sprint_reviews if item.sprint_id == sprint_id
        )
        if len(reviews) != 1 or any(
            item.sprint_id == sprint_id for item in snapshot.sprint_closures
        ):
            return None
        try:
            review_fingerprint = sprint_review_fingerprint(snapshot, sprint_id)
            close_fingerprint = sprint_close_fingerprint(
                snapshot,
                sprint_id,
                review_fingerprint,
            )
        except ExecutionIntegrityError:
            return None
        sprint_reference = _integer_fact_reference(decision, "sprint")
        review_reference = _integer_fact_reference(decision, "sprint_review")
        close_reference = _integer_fact_reference(decision, "sprint_close")
        sprint = next(item for item in snapshot.sprints if item.sprint_id == sprint_id)
        if (
            reviews[0].review_fingerprint != review_fingerprint
            or sprint_reference is None
            or sprint_reference[0] != sprint_id
            or sprint_reference[1].fingerprint
            != canonical_hash(sprint.model_dump(mode="json"))
            or review_reference is None
            or review_reference[0] != sprint_id
            or review_reference[1].fingerprint != review_fingerprint
            or close_reference is None
            or close_reference[0] != sprint_id
            or close_reference[1].fingerprint != close_fingerprint
        ):
            return None
        return sprint_id, review_fingerprint, close_fingerprint

    def prepare_post_sprint_triage(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> tuple[int, str] | None:
        """Resolve one completed Sprint and its exact closure identity."""
        if not execution_action_decision_is_transportable(decision):
            return None
        snapshot = self._snapshot(project_id)
        sprint_id = _instance_identity(decision, "sprint")
        if snapshot is None or sprint_id is None:
            return None
        completed = tuple(
            item
            for item in snapshot.sprints
            if item.sprint_id == sprint_id and item.status == "completed"
        )
        reviews = tuple(
            item for item in snapshot.sprint_reviews if item.sprint_id == sprint_id
        )
        closures = tuple(
            item for item in snapshot.sprint_closures if item.sprint_id == sprint_id
        )
        if len(completed) != 1 or len(reviews) != 1 or len(closures) != 1:
            return None
        try:
            review_fingerprint = sprint_review_fingerprint(snapshot, sprint_id)
            close_fingerprint = sprint_close_fingerprint(
                snapshot,
                sprint_id,
                review_fingerprint,
            )
        except ExecutionIntegrityError:
            return None
        reference = _integer_fact_reference(decision, "sprint_closure")
        triage_reference = _integer_fact_reference(decision, "post_sprint_triage")
        triage_rows = tuple(
            item for item in snapshot.post_sprint_triage if item.sprint_id == sprint_id
        )
        triage_valid, current_triage = _current_post_sprint_triage(triage_rows)
        closure = closures[0]
        if (
            reviews[0].review_fingerprint != review_fingerprint
            or closure.review_fingerprint != review_fingerprint
            or closure.close_fingerprint != close_fingerprint
            or reference is None
            or reference[0] != sprint_id
            or reference[1].fingerprint != close_fingerprint
            or not triage_valid
            or (current_triage is None and triage_reference is not None)
            or (
                current_triage is not None
                and (
                    triage_reference is None
                    or triage_reference[0] != current_triage.triage_id
                    or triage_reference[1].fingerprint
                    != current_triage.payload_fingerprint
                )
            )
        ):
            return None
        return sprint_id, close_fingerprint

    def _snapshot(self, project_id: int) -> WorkflowFactSnapshot | None:
        try:
            with Session(self.engine) as session:
                return WorkflowFactRepository(session).load(project_id)
        except WorkflowFactLoadError:
            return None


@dataclass(frozen=True)
class _DeliveryLineage:
    """Exact durable source rows shared by retained delivery inputs."""

    authority: CompiledSpecAuthority
    goal: ProductGoalArtifact
    spec: SpecRegistry
    vision: VisionArtifact


@dataclass(frozen=True)
class DeliveryActionInputService:
    """Prepare retained delivery model input from exact durable graph facts."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Replay an exact delivery attempt before rebuilding current input."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        node_id: str,
    ) -> JsonObject | None:
        """Build one typed payload, or fail closed when durable input is incomplete."""
        if node_id == "planning.sprint.plan":
            return None
        payload: JsonObject | None = None
        try:
            with Session(self.engine) as session:
                lineage = _delivery_lineage(
                    session,
                    project_id=project_id,
                    decision=decision,
                )
                if lineage is not None:
                    if node_id == "backlog.generate":
                        payload = _backlog_input(session, decision, lineage)
                    elif node_id == "planning.roadmap.generate":
                        payload = _roadmap_input(session, decision, lineage)
                    elif node_id == "planning.story.generate":
                        payload = _story_input(session, decision, lineage)
        except (ValidationError, ValueError):
            return None
        return payload


@dataclass(frozen=True)
class SprintPlanningInputService:
    """Lock exact durable Sprint input before any model execution."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Replay a Sprint attempt using stored guards and candidate identity."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        request: SprintPlanningRequest,
    ) -> JsonObject | WorkflowError:
        """Build an immutable planner envelope from exact current business facts."""
        try:
            with Session(self.engine) as session:
                snapshot = WorkflowFactRepository(session).load(project_id)
                candidates = tuple(
                    item for item in snapshot.stories if item.sprint_candidate
                )
                current_candidate_fingerprint = candidate_set_fingerprint(
                    candidates,
                    snapshot.story_dependencies,
                )
                candidate_reference = _single_fact_reference(
                    decision,
                    "candidate_set",
                )
                if (
                    candidate_reference is None
                    or candidate_reference.fact_id != str(project_id)
                    or candidate_reference.fingerprint != current_candidate_fingerprint
                ):
                    return _sprint_input_error(
                        code="SPRINT_CANDIDATE_SET_STALE",
                        message=(
                            "The Sprint decision does not reference the exact current "
                            "candidate set."
                        ),
                    )
                capacity = _resolve_sprint_capacity(
                    snapshot=snapshot,
                    requested_capacity=request.max_story_points,
                )
                if capacity is None:
                    return _sprint_capacity_error()
                capacity_points, capacity_source, capacity_basis = capacity
                selection_rows = _sprint_selection_rows(
                    session,
                    project_id=project_id,
                    candidates=candidates,
                    dependencies=snapshot.story_dependencies,
                )
                selection = select_sprint_story_rows(
                    selection_rows,
                    max_story_points=capacity_points,
                    selected_story_ids=list(request.selected_story_ids),
                )
                planner_stories = [
                    row["planner_story"] for row in selection.selected_rows
                ]
                planner_input = SprintPlannerInput(
                    available_stories=planner_stories,
                    capacity_points=capacity_points,
                    capacity_source=capacity_source,
                    capacity_basis=capacity_basis,
                    user_context=request.guidance,
                    include_task_decomposition=request.include_task_decomposition,
                )
                valid_parent, parent_reference = _optional_fact_reference(
                    decision,
                    "sprint_plan",
                )
                if not valid_parent:
                    return _sprint_input_error(
                        code="SPRINT_PLAN_PARENT_AMBIGUOUS",
                        message="The Sprint decision has ambiguous plan lineage.",
                    )
                supersedes_id = (
                    None if parent_reference is None else int(parent_reference.fact_id)
                )
                return _JSON_OBJECT.validate_python(
                    {
                        "planner_input": planner_input.model_dump(mode="json"),
                        "capacity_points": capacity_points,
                        "capacity_source": capacity_source,
                        "capacity_basis": capacity_basis,
                        "requested_max_story_points": request.max_story_points,
                        "requested_story_ids": list(request.selected_story_ids),
                        "locked_story_ids": selection.selected_story_ids,
                        "team_name": request.team_name,
                        "include_task_decomposition": (
                            request.include_task_decomposition
                        ),
                        "guidance": request.guidance,
                        "candidate_set_fingerprint": (current_candidate_fingerprint),
                        "supersedes_sprint_plan_artifact_id": supersedes_id,
                    }
                )
        except SprintSelectionError as error:
            return _sprint_input_error(code=error.code, message=str(error))
        except (TypeError, ValueError, ValidationError, WorkflowFactLoadError) as error:
            return _sprint_input_error(
                code="SPRINT_INPUT_INVALID",
                message=str(error) or type(error).__name__,
            )


@dataclass(frozen=True)
class ProductGoalLifecycleServices:
    """Host-owned services for the isolated Product Goal child graph."""

    interview_input: _ProductGoalInterviewInputPort
    discovery_selection: _ProductDiscoverySelectionPort


class _LifecycleServiceOptions(TypedDict, total=False):
    """Optional host-preparation services accepted by the application boundary."""

    vision_input: _VisionInputPort | None
    product_goal_services: ProductGoalLifecycleServices | None
    authority_compilation_input: _AuthorityCompilationInputPort | None
    authority_review_selection: _AuthorityReviewSelectionPort | None
    authority_repair_input: _AuthorityRepairInputPort | None
    delivery_review_selection: _DeliveryReviewSelectionPort | None
    delivery_action_input: _DeliveryActionInputPort | None
    planning_action_selection: _PlanningActionSelectionPort | None
    execution_action_selection: _ExecutionActionSelectionPort | None
    sprint_planning_input: _SprintPlanningInputPort | None


class _ReadProjectionPort(Protocol):
    """Supported non-routing reads exposed to production transports."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

    def repository_status(self, *, project_id: int) -> JsonObject: ...

    def vision_status(self, *, project_id: int) -> JsonObject: ...

    def product_goal_status(self, *, project_id: int) -> JsonObject: ...

    def discovery_status(self, *, project_id: int) -> JsonObject: ...

    def specification_status(self, *, project_id: int) -> JsonObject: ...

    def specification_review(self, *, project_id: int) -> JsonObject: ...

    def authority_status(self, *, project_id: int) -> JsonObject: ...

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject: ...

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
    ) -> JsonObject: ...

    def artifact_history(
        self,
        *,
        project_id: int,
        node_id: str,
        instance_key: str | None = None,
    ) -> JsonObject: ...

    def story_show(self, *, story_id: int) -> JsonObject: ...

    def story_pending(self, *, project_id: int) -> JsonObject: ...

    def story_dependencies_inspect(self, *, project_id: int) -> JsonObject: ...

    def sprint_candidates(self, *, project_id: int) -> JsonObject: ...

    def sprint_history(self, *, project_id: int) -> JsonObject: ...

    def sprint_metrics(self, *, project_id: int) -> JsonObject: ...

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject: ...

    def task_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        task_id: int,
        flavor: str | None = None,
    ) -> JsonObject: ...

    def story_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        story_id: int,
        flavor: str | None = None,
    ) -> JsonObject: ...

    def context_pack(self, *, project_id: int, phase: str) -> JsonObject: ...

    def status(self, *, project_id: int) -> JsonObject: ...


_EXECUTION_SETTINGS: JsonObject = {
    "timeout_seconds": 120,
    "max_attempts": 2,
}
_LEASE_SECONDS = 300
type SemanticText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AgenticActionRequest(FrozenModel):
    """Internal host-prepared request for one agentic graph decision."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    node_id: str
    instance_key: str | None = None
    input_payload: JsonObject
    model_id: str
    idempotency_key: str
    actor: str
    correlation_id: str | None = None


class DeliveryActionRequest(FrozenModel):
    """Semantic delivery request with no caller-owned model input."""

    project_id: int
    instance_key: str | None = None
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class _DeliveryReviewRequest(FrozenModel):
    """Semantic operator input shared by task-specific delivery reviews."""

    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class BacklogReviewRequest(_DeliveryReviewRequest):
    """Operator choice for the graph-selected pending Backlog."""


class RoadmapReviewRequest(_DeliveryReviewRequest):
    """Operator choice for the graph-selected pending Roadmap."""


class StoryReviewRequest(_DeliveryReviewRequest):
    """Operator choice for one exact repeated Story review instance."""

    instance_key: SemanticText


class SprintPlanReviewRequest(_DeliveryReviewRequest):
    """Operator choice for the graph-selected pending Sprint plan."""


class SprintPlanningRequest(FrozenModel):
    """Semantic Sprint planning input with no caller-owned execution evidence."""

    project_id: int
    guidance: str | None = None
    selected_story_ids: tuple[int, ...] = ()
    max_story_points: int | None = Field(default=None, gt=0)
    include_task_decomposition: bool = True
    team_name: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None

    @model_validator(mode="after")
    def validate_selected_story_ids(self) -> SprintPlanningRequest:
        """Reject duplicate or non-positive manual Story identities."""
        if any(story_id <= 0 for story_id in self.selected_story_ids):
            message = "selected_story_ids must contain positive Story IDs."
            raise ValueError(message)
        if len(set(self.selected_story_ids)) != len(self.selected_story_ids):
            message = "selected_story_ids must not contain duplicates."
            raise ValueError(message)
        return self


type PositiveStoryId = Annotated[int, Field(strict=True, gt=0)]


class _PlanningMutationRequest(FrozenModel):
    """Transport metadata shared by task-specific planning action requests."""

    project_id: int
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class StoryDependencyEdgeRequest(FrozenModel):
    """One operator-reviewed typed Story dependency edge."""

    dependent_story_id: PositiveStoryId
    prerequisite_story_id: PositiveStoryId
    reason: SemanticText

    @model_validator(mode="after")
    def reject_self_edge(self) -> StoryDependencyEdgeRequest:
        """Reject a dependency edge from one Story to itself."""
        if self.dependent_story_id == self.prerequisite_story_id:
            message = "A Story dependency cannot reference itself."
            raise ValueError(message)
        return self


class StoryDependenciesApplyRequest(_PlanningMutationRequest):
    """Exact operator-reviewed Story selection and dependency semantics."""

    selected_story_ids: tuple[PositiveStoryId, ...] = Field(min_length=1)
    reviewed_edges: tuple[StoryDependencyEdgeRequest, ...]

    @model_validator(mode="after")
    def validate_dependency_set(self) -> StoryDependenciesApplyRequest:
        """Reject duplicate selections and invalid edge membership."""
        if len(set(self.selected_story_ids)) != len(self.selected_story_ids):
            message = "selected_story_ids must not contain duplicates."
            raise ValueError(message)
        pairs = tuple(
            (item.dependent_story_id, item.prerequisite_story_id)
            for item in self.reviewed_edges
        )
        if len(set(pairs)) != len(pairs):
            message = "reviewed_edges must not contain duplicate Story pairs."
            raise ValueError(message)
        selected = set(self.selected_story_ids)
        if any(left not in selected or right not in selected for left, right in pairs):
            message = "reviewed_edges must remain inside selected_story_ids."
            raise ValueError(message)
        return self


class StoryReadinessRepair(FrozenModel):
    """Operator-supplied readiness values for one Story."""

    story_id: PositiveStoryId
    story_points: Annotated[int, Field(strict=True, gt=0)]
    rank: Annotated[str, Field(strict=True)]

    @field_validator("rank")
    @classmethod
    def validate_rank(cls, value: str) -> str:
        """Require the one durable Story rank representation."""
        parse_story_rank(value)
        return value


class StoryReadinessRepairRequest(_PlanningMutationRequest):
    """Explicit operator readiness repairs without derived guards."""

    repairs: tuple[StoryReadinessRepair, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_story_repairs(self) -> StoryReadinessRepairRequest:
        """Reject multiple readiness repairs for one Story."""
        story_ids = tuple(item.story_id for item in self.repairs)
        if len(set(story_ids)) != len(story_ids):
            message = "repairs must not contain duplicate Story IDs."
            raise ValueError(message)
        return self


class SprintStartRequest(_PlanningMutationRequest):
    """Transport-only request to start the graph-selected accepted Sprint plan."""


class _ExecutionMutationRequest(FrozenModel):
    """Transport metadata shared by strict execution and triage requests."""

    project_id: int
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class CompleteTaskRequest(_ExecutionMutationRequest):
    """Semantic completion evidence for one selected Task."""

    instance_key: SemanticText
    outcome_summary: SemanticText
    artifact_refs: tuple[SemanticText, ...] = Field(min_length=1)
    acceptance_result: Literal["partially_met", "fully_met"]
    checklist_result: dict[SemanticText, SemanticText] = Field(min_length=1)


class CloseStoryRequest(_ExecutionMutationRequest):
    """Semantic closure evidence for one selected Story."""

    instance_key: SemanticText
    resolution: SemanticText
    delivered: SemanticText
    evidence: SemanticText
    known_gaps: SemanticText


class SprintReviewRequest(_ExecutionMutationRequest):
    """Transport metadata only for the graph-selected terminal Sprint review."""

    instance_key: SemanticText


class SprintCloseRequest(_ExecutionMutationRequest):
    """Transport metadata only for the graph-selected reviewed Sprint close."""

    instance_key: SemanticText


class PostSprintTriageRequest(_ExecutionMutationRequest):
    """Semantic post-Sprint impact and canonical payload."""

    instance_key: SemanticText
    impact: Literal["none", "backlog", "specification"]
    canonical_payload: JsonObject


class VisionInterviewRequest(FrozenModel):
    """Transport request for one host-prepared Vision interview attempt."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    user_text: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionBootstrapRequest(FrozenModel):
    """Transport metadata for one host-prepared Vision bootstrap attempt."""

    project_id: int
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionResponseRequest(FrozenModel):
    """Semantic caller input for one Project Vision interview turn."""

    project_id: int
    text: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionReviewRequest(FrozenModel):
    """Transport request for one explicit Vision review decision."""

    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: SemanticText
    expected_candidate_fingerprint: str | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        repr=False,
    )
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionRevisionRequest(FrozenModel):
    """Transport request to open an eligible Vision replacement interview."""

    project_id: int
    reason: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalInterviewRequest(FrozenModel):
    """Transport request for one host-prepared Product Goal interview attempt."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    user_text: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalResponseRequest(FrozenModel):
    """Semantic caller input for one Product Goal interview turn."""

    project_id: int
    text: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalReviewRequest(FrozenModel):
    """Operator choice for the graph-selected pending Product Goal."""

    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: SemanticText
    expected_candidate_fingerprint: str | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        repr=False,
    )
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalOutcomeRequest(FrozenModel):
    """Operator outcome choice for the graph-selected active Product Goal."""

    project_id: int
    outcome: Literal["fulfilled", "abandoned"]
    rationale: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class DiscoveryArtifactRequest(FrozenModel):
    """Semantic discovery content for the graph-selected active Product Goal."""

    project_id: int
    canonical_content: JsonObject
    content_ref: str | None = None
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class SpecificationCandidateRequest(FrozenModel):
    """Semantic specification candidate content with host-derived lineage."""

    project_id: int
    canonical_content: JsonObject
    content_ref: str | None = None
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class SpecificationReviewRequest(FrozenModel):
    """Operator choice for the graph-selected specification candidate."""

    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: SemanticText
    expected_candidate_fingerprint: str | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        repr=False,
    )
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class RepositoryAttachRequest(FrozenModel):
    """Semantic repository attachment input without caller-owned provenance."""

    project_id: int
    path: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class RepositoryRefreshRequest(FrozenModel):
    """Semantic repository refresh input without a caller-owned binding guard."""

    project_id: int
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class AuthorityCompileRequest(FrozenModel):
    """Semantic authority compilation input with host-prepared compiler data."""

    project_id: int
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class AuthorityReviewRequest(FrozenModel):
    """Semantic authority decision with an optional browser review expectation."""

    project_id: int
    decision: Literal["accepted", "rejected"]
    rationale: SemanticText
    expected_candidate_fingerprint: str | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        repr=False,
    )
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class AuthorityFeedbackRequest(FrozenModel):
    """Semantic feedback for the graph-selected rejected authority."""

    project_id: int
    feedback: SemanticText
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class AuthorityRepairRequest(FrozenModel):
    """Semantic authority repair input with no caller-owned compiler payload."""

    project_id: int
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class AgileForgeApplication:
    """Expose the narrow workflow application interface to transports."""

    def __init__(
        self,
        *,
        workflow_domain: WorkflowDomainPort,
        recipe_registry: AdkRecipeRegistry | None = None,
        read_projection: _ReadProjectionPort | None = None,
        **lifecycle_services: Unpack[_LifecycleServiceOptions],
    ) -> None:
        """Retain exactly one workflow authority."""
        self._workflow_domain = workflow_domain
        self._recipe_registry = recipe_registry
        self._read_projection = read_projection
        self._vision_input = lifecycle_services.get("vision_input")
        self._product_goal_services = lifecycle_services.get("product_goal_services")
        self._authority_compilation_input = lifecycle_services.get(
            "authority_compilation_input"
        )
        self._authority_review_selection = lifecycle_services.get(
            "authority_review_selection"
        )
        self._authority_repair_input = lifecycle_services.get("authority_repair_input")
        self._delivery_review_selection = lifecycle_services.get(
            "delivery_review_selection"
        )
        self._delivery_action_input = lifecycle_services.get("delivery_action_input")
        self._planning_action_selection = lifecycle_services.get(
            "planning_action_selection"
        )
        self._execution_action_selection = lifecycle_services.get(
            "execution_action_selection"
        )
        self._sprint_planning_input = lifecycle_services.get("sprint_planning_input")
        self._project_lifecycle: ProjectLifecycleService | None = None

    @property
    def reads(self) -> _ReadProjectionPort:
        """Return the injected durable non-routing projection."""
        if self._read_projection is None:
            message = "Read operations require an injected durable projection."
            raise RuntimeError(message)
        return self._read_projection

    def position(self, *, project_id: int) -> WorkflowPosition:
        """Return the current durable workflow position."""
        return self._workflow_domain.position(project_id)

    def transition(self, request: TransitionRequest) -> TransitionResult:
        """Apply one exact typed workflow request."""
        return self._workflow_domain.transition(request)

    def create_project(self, request: CreateProjectCommand) -> TransitionResult:
        """Create a Project from semantic business input only."""
        return self._project_lifecycle_service().create_project(request)

    def set_project_lifecycle(self, service: ProjectLifecycleService) -> None:
        """Configure the production-only Project lifecycle application service."""
        self._project_lifecycle = service

    def attach_repository(
        self,
        request: RepositoryAttachRequest,
    ) -> TransitionResult:
        """Resolve the active binding once, then attach or replace by path."""
        service = self._project_lifecycle_service()
        replay = service.replay_repository_binding(
            RepositoryBindingReplayCommand(
                project_id=request.project_id,
                operation="attach",
                requested_repository_path=request.path,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
        available, fingerprint = self._active_repository_binding(
            project_id=request.project_id,
            require_active=False,
        )
        if not available:
            return _transition_not_available(None, "repository.attach")
        return service.attach_repository(
            RepositoryAttachmentCommand(
                project_id=request.project_id,
                path=request.path,
                expected_active_binding_fingerprint=fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def refresh_repository(
        self,
        request: RepositoryRefreshRequest,
    ) -> TransitionResult:
        """Resolve the active binding once, then refresh its provenance."""
        service = self._project_lifecycle_service()
        replay = service.replay_repository_binding(
            RepositoryBindingReplayCommand(
                project_id=request.project_id,
                operation="refresh",
                requested_repository_path=None,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
        available, fingerprint = self._active_repository_binding(
            project_id=request.project_id,
            require_active=True,
        )
        if not available or fingerprint is None:
            return _transition_not_available(None, "repository.refresh")
        return service.refresh_repository(
            RepositoryRefreshCommand(
                project_id=request.project_id,
                expected_active_binding_fingerprint=fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def _active_repository_binding(
        self,
        *,
        project_id: int,
        require_active: bool,
    ) -> tuple[bool, str | None]:
        projection = self.reads.repository_status(project_id=project_id)
        if projection.get("ok") is not True:
            return False, None
        data = projection.get("data")
        if not isinstance(data, dict):
            return False, None
        repository = data.get("repository")
        if repository is None:
            return (not require_active), None
        if not isinstance(repository, dict):
            return False, None
        fingerprint = repository.get("binding_fingerprint")
        return (True, fingerprint) if isinstance(fingerprint, str) else (False, None)

    def _project_lifecycle_service(self) -> ProjectLifecycleService:
        service = self._project_lifecycle
        if service is None:
            message = "Project lifecycle operations require an injected service."
            raise RuntimeError(message)
        return service

    def run_agentic_action(
        self,
        request: AgenticActionRequest,
    ) -> TransitionResult:
        """Run one exact available agentic decision through durable ADK attempts."""
        from adapters.adk.runner import (  # noqa: PLC0415
            AdkExecutionConfig,
            AdkRunRequest,
            AdkWorkflowRunner,
        )

        if self._recipe_registry is None:
            msg = "Agentic execution requires a production recipe registry."
            raise RuntimeError(msg)
        runner = AdkWorkflowRunner(
            domain=self._workflow_domain,
            registry=self._recipe_registry,
            config=AdkExecutionConfig(
                project_id=request.project_id,
                model_id=request.model_id,
                execution_settings=_EXECUTION_SETTINGS,
                lease_seconds=_LEASE_SECONDS,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
        )
        return runner.run_request(
            AdkRunRequest(
                graph_version=request.graph_version,
                fact_fingerprint=request.fact_fingerprint,
                decision_fingerprint=request.decision_fingerprint,
                node_id=request.node_id,
                instance_key=request.instance_key,
                input_payload=request.input_payload,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
        )

    def generate_backlog(self, request: DeliveryActionRequest) -> TransitionResult:
        """Generate Backlog from host-prepared durable workflow facts."""
        return self._run_delivery_action(
            request,
            node_id="backlog.generate",
        )

    def generate_roadmap(self, request: DeliveryActionRequest) -> TransitionResult:
        """Generate Roadmap from host-prepared durable workflow facts."""
        return self._run_delivery_action(
            request,
            node_id="planning.roadmap.generate",
        )

    def generate_story(self, request: DeliveryActionRequest) -> TransitionResult:
        """Generate one Story set from its exact durable requirement instance."""
        return self._run_delivery_action(
            request,
            node_id="planning.story.generate",
        )

    def generate_sprint(self, request: SprintPlanningRequest) -> TransitionResult:
        """Plan one Sprint from host-resolved capacity and exact durable candidates."""
        input_service = self._sprint_planning_input
        if input_service is None:
            return _transition_not_available(None, "planning.sprint.plan")
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id="planning.sprint.plan",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                semantic_input=_sprint_replay_input(request),
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "planning.sprint.plan")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "planning.sprint.plan")
        prepared = input_service.build(
            project_id=request.project_id,
            decision=decision,
            request=request,
        )
        if isinstance(prepared, WorkflowError):
            return TransitionResult(ok=False, position=position, error=prepared)
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id="planning.sprint.plan",
                instance_key=decision.instance_key,
                input_payload=prepared,
                model_id=get_model_id(AGENTIC_MODEL_ROLES["planning.sprint.plan"]),
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def decide_backlog(self, request: BacklogReviewRequest) -> TransitionResult:
        """Resolve and review the unique current Backlog artifact internally."""
        selection = self._delivery_review_selection
        if selection is None:
            return _transition_not_available(None, "backlog.review")
        replay = self._replay_delivery_review(
            request_kind="decide_backlog",
            request=request,
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "backlog.review")
        identity = (
            None
            if decision is None
            else selection.review_identity(
                project_id=request.project_id,
                decision=decision,
                fact_type="backlog",
            )
        )
        if decision is None or identity is None:
            return _transition_not_available(position, "backlog.review")
        artifact_id, fingerprint = identity
        return self.transition(
            DecideBacklog(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                backlog_artifact_id=artifact_id,
                artifact_fingerprint=fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def decide_roadmap(self, request: RoadmapReviewRequest) -> TransitionResult:
        """Resolve and review the unique current Roadmap artifact internally."""
        selection = self._delivery_review_selection
        if selection is None:
            return _transition_not_available(None, "planning.roadmap.review")
        replay = self._replay_delivery_review(
            request_kind="decide_roadmap",
            request=request,
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "planning.roadmap.review")
        identity = (
            None
            if decision is None
            else selection.review_identity(
                project_id=request.project_id,
                decision=decision,
                fact_type="roadmap",
            )
        )
        if decision is None or identity is None:
            return _transition_not_available(position, "planning.roadmap.review")
        artifact_id, fingerprint = identity
        return self.transition(
            DecideRoadmap(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                roadmap_artifact_id=artifact_id,
                artifact_fingerprint=fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def decide_story(self, request: StoryReviewRequest) -> TransitionResult:
        """Resolve and review one exact current Story artifact instance."""
        selection = self._delivery_review_selection
        if selection is None:
            return _transition_not_available(None, "planning.story.review")
        replay = self._replay_delivery_review(
            request_kind="decide_story",
            request=request,
            instance_key=request.instance_key,
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(
            position,
            "planning.story.review",
            instance_key=request.instance_key,
        )
        identity = (
            None
            if decision is None
            else selection.review_identity(
                project_id=request.project_id,
                decision=decision,
                fact_type="story",
            )
        )
        instance_key = None if decision is None else decision.instance_key
        prefix = "requirement:"
        if (
            decision is None
            or identity is None
            or instance_key is None
            or not instance_key.startswith(prefix)
            or not instance_key.removeprefix(prefix)
        ):
            return _transition_not_available(position, "planning.story.review")
        artifact_id, fingerprint = identity
        return self.transition(
            DecideStory(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                requirement_id=instance_key.removeprefix(prefix),
                story_artifact_id=artifact_id,
                artifact_fingerprint=fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def decide_sprint_plan(
        self,
        request: SprintPlanReviewRequest,
    ) -> TransitionResult:
        """Resolve and review the unique current Sprint plan internally."""
        selection = self._delivery_review_selection
        if selection is None:
            return _transition_not_available(None, "planning.sprint.review")
        replay = self._replay_delivery_review(
            request_kind="decide_sprint_plan",
            request=request,
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "planning.sprint.review")
        identity = (
            None
            if decision is None
            else selection.review_identity(
                project_id=request.project_id,
                decision=decision,
                fact_type="sprint_plan",
            )
        )
        if decision is None or identity is None:
            return _transition_not_available(position, "planning.sprint.review")
        artifact_id, fingerprint = identity
        return self.transition(
            DecideSprintPlan(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                sprint_plan_artifact_id=artifact_id,
                plan_fingerprint=fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def apply_story_dependencies(
        self,
        request: StoryDependenciesApplyRequest,
    ) -> TransitionResult:
        """Apply reviewed dependency semantics against current Story facts."""
        selection = self._planning_action_selection
        if selection is None:
            return _transition_not_available(None, "planning.story_dependencies")
        selected_story_ids = tuple(sorted(request.selected_story_ids))
        reviewed_edges = _canonical_dependency_edges(request.reviewed_edges)
        replay = self._replay_planning_action(
            request_kind="apply_story_dependencies",
            request=request,
            operator_input={
                "selected_story_ids": list(selected_story_ids),
                "reviewed_edges": [
                    item.model_dump(mode="json") for item in reviewed_edges
                ],
            },
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(
            position,
            "planning.story_dependencies",
        )
        source_fingerprint = (
            None
            if decision is None or decision.category is not NodeCategory.AVAILABLE
            else selection.prepare_story_dependencies(
                project_id=request.project_id,
                decision=decision,
                selected_story_ids=selected_story_ids,
            )
        )
        if decision is None or source_fingerprint is None:
            return _transition_not_available(position, "planning.story_dependencies")
        return self.transition(
            ApplyStoryDependencies(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                selected_story_ids=selected_story_ids,
                reviewed_edges=reviewed_edges,
                source_fingerprint=source_fingerprint,
            )
        )

    def repair_story_readiness(
        self,
        request: StoryReadinessRepairRequest,
    ) -> TransitionResult:
        """Apply explicit repairs against the current missing-readiness set."""
        selection = self._planning_action_selection
        if selection is None:
            return _transition_not_available(None, "planning.story_readiness")
        repairs = _canonical_story_readiness_repairs(request.repairs)
        repair_story_ids = tuple(item.story_id for item in repairs)
        replay = self._replay_planning_action(
            request_kind="repair_story_readiness",
            request=request,
            operator_input={
                "repairs": [item.model_dump(mode="json") for item in repairs]
            },
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "planning.story_readiness")
        target = (
            None
            if decision is None or decision.category is not NodeCategory.AVAILABLE
            else selection.prepare_story_readiness(
                project_id=request.project_id,
                decision=decision,
                repair_story_ids=repair_story_ids,
            )
        )
        if decision is None or target is None:
            return _transition_not_available(position, "planning.story_readiness")
        story_ids, readiness_guard = target
        return self.transition(
            RepairStoryReadiness(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                story_ids=story_ids,
                repairs=repairs,
                expected_readiness_fingerprint=readiness_guard,
            )
        )

    def start_sprint(self, request: SprintStartRequest) -> TransitionResult:
        """Start the accepted current Sprint plan without caller-owned identity."""
        selection = self._planning_action_selection
        if selection is None:
            return _transition_not_available(None, "planning.sprint.start")
        replay = self._replay_planning_action(
            request_kind="start_sprint",
            request=request,
            operator_input={},
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "planning.sprint.start")
        target = (
            None
            if decision is None or decision.category is not NodeCategory.AVAILABLE
            else selection.prepare_sprint_start(
                project_id=request.project_id,
                decision=decision,
            )
        )
        if decision is None or target is None:
            return _transition_not_available(position, "planning.sprint.start")
        plan_id, sprint_id, plan_fingerprint, candidate_fingerprint = target
        return self.transition(
            StartSprint(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                sprint_plan_artifact_id=plan_id,
                sprint_id=sprint_id,
                plan_fingerprint=plan_fingerprint,
                candidate_set_fingerprint=candidate_fingerprint,
            )
        )

    def complete_task(self, request: CompleteTaskRequest) -> TransitionResult:
        """Complete one exact graph-selected Task from semantic evidence."""
        artifact_refs = tuple(sorted(set(request.artifact_refs)))
        checklist_result = _JSON_OBJECT.validate_python(request.checklist_result)
        operator_input = _JSON_OBJECT.validate_python(
            {
                "instance_key": request.instance_key,
                "outcome_summary": request.outcome_summary,
                "artifact_refs": list(artifact_refs),
                "acceptance_result": request.acceptance_result,
                "checklist_result": checklist_result,
            }
        )
        replay = self._replay_execution_action(
            request_kind="complete_task",
            request=request,
            operator_input=operator_input,
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_execution_decision(
            position,
            request_kind="complete_task",
            node_id="execution.task.complete",
            instance_key=request.instance_key,
        )
        selection = self._execution_action_selection
        target = (
            None
            if decision is None
            or decision.category is not NodeCategory.AVAILABLE
            or selection is None
            else selection.prepare_task_completion(
                project_id=request.project_id,
                decision=decision,
            )
        )
        if decision is None or target is None:
            return _transition_not_available(position, "execution.task.complete")
        task_id, _sprint_id = target
        return self.transition(
            CompleteTask(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=request.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                task_id=task_id,
                outcome_summary=request.outcome_summary,
                artifact_refs=artifact_refs,
                acceptance_result=request.acceptance_result,
                checklist_result=checklist_result,
            )
        )

    def close_story(self, request: CloseStoryRequest) -> TransitionResult:
        """Close one exact graph-selected Story from semantic evidence."""
        replay = self._replay_execution_action(
            request_kind="close_story",
            request=request,
            operator_input={
                "instance_key": request.instance_key,
                "resolution": request.resolution,
                "delivered": request.delivered,
                "evidence": request.evidence,
                "known_gaps": request.known_gaps,
            },
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_execution_decision(
            position,
            request_kind="close_story",
            node_id="execution.story.close",
            instance_key=request.instance_key,
        )
        selection = self._execution_action_selection
        target = (
            None
            if decision is None
            or decision.category is not NodeCategory.AVAILABLE
            or selection is None
            else selection.prepare_story_close(
                project_id=request.project_id,
                decision=decision,
            )
        )
        if decision is None or target is None:
            return _transition_not_available(position, "execution.story.close")
        story_id, _sprint_id, _completion_fingerprint = target
        return self.transition(
            CloseStory(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=request.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                story_id=story_id,
                resolution=request.resolution,
                delivered=request.delivered,
                evidence=request.evidence,
                known_gaps=request.known_gaps,
            )
        )

    def review_sprint(self, request: SprintReviewRequest) -> TransitionResult:
        """Review the unique terminal active Sprint from durable facts."""
        replay = self._replay_execution_action(
            request_kind="review_sprint",
            request=request,
            operator_input={"instance_key": request.instance_key},
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_execution_decision(
            position,
            request_kind="review_sprint",
            node_id="execution.sprint.review",
            instance_key=request.instance_key,
        )
        selection = self._execution_action_selection
        target = (
            None
            if decision is None
            or decision.category not in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
            or selection is None
            else selection.prepare_sprint_review(
                project_id=request.project_id,
                decision=decision,
            )
        )
        if decision is None or target is None:
            return _transition_not_available(position, "execution.sprint.review")
        sprint_id, review_fingerprint = target
        return self.transition(
            ReviewSprint(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=request.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        )

    def close_sprint(self, request: SprintCloseRequest) -> TransitionResult:
        """Close the unique reviewed active Sprint from durable facts."""
        replay = self._replay_execution_action(
            request_kind="close_sprint",
            request=request,
            operator_input={"instance_key": request.instance_key},
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_execution_decision(
            position,
            request_kind="close_sprint",
            node_id="execution.sprint.close",
            instance_key=request.instance_key,
        )
        selection = self._execution_action_selection
        target = (
            None
            if decision is None
            or decision.category is not NodeCategory.AVAILABLE
            or selection is None
            else selection.prepare_sprint_close(
                project_id=request.project_id,
                decision=decision,
            )
        )
        if decision is None or target is None:
            return _transition_not_available(position, "execution.sprint.close")
        sprint_id, review_fingerprint, _close_fingerprint = target
        return self.transition(
            CloseSprint(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=request.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        )

    def record_post_sprint_triage(
        self,
        request: PostSprintTriageRequest,
    ) -> TransitionResult:
        """Record semantic triage for one exact graph-selected completed Sprint."""
        canonical_payload = dict(request.canonical_payload)
        replay = self._replay_execution_action(
            request_kind="record_post_sprint_triage",
            request=request,
            operator_input={
                "instance_key": request.instance_key,
                "impact": request.impact,
                "canonical_payload": canonical_payload,
            },
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_execution_decision(
            position,
            request_kind="record_post_sprint_triage",
            node_id="execution.post_sprint_triage",
            instance_key=request.instance_key,
        )
        selection = self._execution_action_selection
        target = (
            None
            if decision is None
            or decision.category is not NodeCategory.AVAILABLE
            or selection is None
            else selection.prepare_post_sprint_triage(
                project_id=request.project_id,
                decision=decision,
            )
        )
        if decision is None or target is None:
            return _transition_not_available(
                position,
                "execution.post_sprint_triage",
            )
        sprint_id, _closure_fingerprint = target
        return self.transition(
            RecordPostSprintTriage(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=request.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                sprint_id=sprint_id,
                impact=request.impact,
                canonical_payload=canonical_payload,
            )
        )

    def _replay_execution_action(
        self,
        *,
        request_kind: str,
        request: _ExecutionMutationRequest,
        operator_input: JsonObject,
    ) -> TransitionResult | None:
        selection = self._execution_action_selection
        if selection is None:
            return None
        return selection.replay_transition(
            TransitionReplayQuery(
                request_kind=request_kind,
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input=operator_input,
            )
        )

    def _replay_planning_action(
        self,
        *,
        request_kind: str,
        request: _PlanningMutationRequest,
        operator_input: JsonObject,
    ) -> TransitionResult | None:
        selection = self._planning_action_selection
        if selection is None:
            return None
        return selection.replay_transition(
            TransitionReplayQuery(
                request_kind=request_kind,
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input=operator_input,
            )
        )

    def _replay_delivery_review(
        self,
        *,
        request_kind: str,
        request: _DeliveryReviewRequest,
        instance_key: str | None = None,
    ) -> TransitionResult | None:
        selection = self._delivery_review_selection
        if selection is None:
            return None
        operator_input: JsonObject = {
            "decision": request.decision,
            "rationale": request.rationale,
        }
        if instance_key is not None:
            operator_input["instance_key"] = instance_key
        return selection.replay_transition(
            TransitionReplayQuery(
                request_kind=request_kind,
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input=operator_input,
            )
        )

    def _run_delivery_action(
        self,
        request: DeliveryActionRequest,
        *,
        node_id: str,
    ) -> TransitionResult:
        input_service = self._delivery_action_input
        if input_service is None:
            return _transition_not_available(None, node_id)
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id=node_id,
                instance_key=request.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
        if node_id == "planning.story.generate" and (
            request.instance_key is None or not request.instance_key.strip()
        ):
            return _transition_not_available(None, node_id)
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(
            position,
            node_id,
            instance_key=request.instance_key,
        )
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, node_id)
        input_payload = input_service.build(
            project_id=request.project_id,
            decision=decision,
            node_id=node_id,
        )
        if input_payload is None:
            return _transition_not_available(position, node_id)
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id=node_id,
                instance_key=decision.instance_key,
                input_payload=input_payload,
                model_id=get_model_id(AGENTIC_MODEL_ROLES[node_id]),
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def respond_to_vision(
        self,
        request: VisionResponseRequest,
    ) -> TransitionResult:
        """Resolve the current Vision interview guards from one position read."""
        input_service = self._vision_input
        if input_service is None:
            message = "Vision interview requires an injected input builder."
            raise RuntimeError(message)
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id="vision.interview",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                user_text=request.text.strip(),
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "vision.interview")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "vision.interview")
        return self._run_vision_interview_at_position(
            VisionInterviewRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                user_text=request.text,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
            position,
            check_replay=False,
        )

    def run_vision_interview(
        self,
        request: VisionInterviewRequest,
    ) -> TransitionResult:
        """Run one host-prepared, replay-safe Project Vision interview turn."""
        replay = self._replay_vision_interview(request)
        if replay is not None:
            return replay
        return self._run_vision_interview_at_position(
            request,
            self.position(project_id=request.project_id),
            check_replay=False,
        )

    def bootstrap_vision(self, request: VisionBootstrapRequest) -> TransitionResult:
        """Run one explicit host-prepared Project Vision bootstrap generation."""
        input_service = self._vision_input
        if input_service is None:
            message = "Vision bootstrap requires an injected input builder."
            raise RuntimeError(message)
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id="vision.bootstrap",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                user_text=None,
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "vision.bootstrap")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "vision.bootstrap")
        input_payload = input_service.build_bootstrap(request.project_id, decision)
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id="vision.bootstrap",
                input_payload=input_payload,
                model_id=get_model_id(AGENTIC_MODEL_ROLES["vision.bootstrap"]),
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def _run_vision_interview_at_position(
        self,
        request: VisionInterviewRequest,
        position: WorkflowPosition,
        *,
        check_replay: bool = True,
    ) -> TransitionResult:
        node_id = "vision.interview"
        input_service = self._vision_input
        if input_service is None:
            message = "Vision interview requires an injected input builder."
            raise RuntimeError(message)
        replay = self._replay_vision_interview(request) if check_replay else None
        if replay is not None:
            return replay
        decision = _guarded_vision_interview_decision(position, request)
        if decision is None:
            return _stale_vision_interview(position)
        input_payload = input_service.build_clarification(
            project_id=request.project_id,
            decision=decision,
            user_text=request.user_text,
        )
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=request.graph_version,
                fact_fingerprint=request.fact_fingerprint,
                decision_fingerprint=request.decision_fingerprint,
                node_id="vision.interview",
                input_payload=input_payload,
                model_id=get_model_id(AGENTIC_MODEL_ROLES[node_id]),
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def _replay_vision_interview(
        self,
        request: VisionInterviewRequest,
    ) -> TransitionResult | None:
        input_service = self._vision_input
        if input_service is None:
            message = "Vision interview requires an injected input builder."
            raise RuntimeError(message)
        return input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=request.graph_version,
                fact_fingerprint=request.fact_fingerprint,
                decision_fingerprint=request.decision_fingerprint,
                node_id="vision.interview",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                user_text=request.user_text.strip(),
            )
        )

    def review_vision(self, request: VisionReviewRequest) -> TransitionResult:
        """Prepare exact pending Vision identity internally before review."""
        input_service = self._vision_input
        if input_service is not None:
            replay = input_service.replay_transition(
                TransitionReplayQuery(
                    request_kind="decide_vision_review",
                    project_id=request.project_id,
                    idempotency_key=request.idempotency_key,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                    operator_input={
                        "decision": request.decision,
                        "rationale": request.rationale,
                    },
                )
            )
            if replay is not None:
                return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "vision.review")
        if decision is None:
            return _transition_not_available(position, "vision.review")
        reference = _single_fact_reference(decision, "vision")
        if reference is None:
            return _transition_not_available(position, "vision.review")
        if not _review_candidate_matches(
            expected=request.expected_candidate_fingerprint,
            current=reference.fingerprint,
        ):
            return _stale_review_candidate(position, "Vision")
        return self.transition(
            DecideVisionReview(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                vision_artifact_id=int(reference.fact_id),
                vision_fingerprint=reference.fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def begin_vision_revision(
        self,
        request: VisionRevisionRequest,
    ) -> TransitionResult:
        """Prepare the accepted Vision identity internally before revision start."""
        input_service = self._vision_input
        if input_service is not None:
            replay = input_service.replay_transition(
                TransitionReplayQuery(
                    request_kind="begin_vision_revision",
                    project_id=request.project_id,
                    idempotency_key=request.idempotency_key,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                    operator_input={"reason": request.reason},
                )
            )
            if replay is not None:
                return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "vision.revision.start")
        if decision is None:
            return _transition_not_available(position, "vision.revision.start")
        reference = _single_fact_reference(decision, "vision")
        if reference is None:
            return _transition_not_available(position, "vision.revision.start")
        return self.transition(
            BeginVisionRevision(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                source_vision_artifact_id=int(reference.fact_id),
                source_vision_fingerprint=reference.fingerprint,
                reason=request.reason,
            )
        )

    def respond_to_product_goal(
        self,
        request: ProductGoalResponseRequest,
    ) -> TransitionResult:
        """Resolve current Product Goal interview guards from one position read."""
        services = self._product_goal_services
        if services is None:
            message = "Product Goal interview requires an injected input builder."
            raise RuntimeError(message)
        replay = services.interview_input.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id="goal.interview",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                user_text=ProductGoalInterviewInput.normalize_user_response(
                    request.text
                ),
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "goal.interview")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "goal.interview")
        return self._run_product_goal_interview_at_position(
            ProductGoalInterviewRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                user_text=request.text,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
            position,
            check_replay=False,
        )

    def run_product_goal_interview(
        self,
        request: ProductGoalInterviewRequest,
    ) -> TransitionResult:
        """Run a replay-safe Goal interview with host-derived durable context."""
        replay = self._replay_product_goal_interview(request)
        if replay is not None:
            return replay
        return self._run_product_goal_interview_at_position(
            request,
            self.position(project_id=request.project_id),
            check_replay=False,
        )

    def _run_product_goal_interview_at_position(
        self,
        request: ProductGoalInterviewRequest,
        position: WorkflowPosition,
        *,
        check_replay: bool = True,
    ) -> TransitionResult:
        node_id = "goal.interview"
        services = self._product_goal_services
        if services is None:
            message = "Product Goal interview requires an injected input builder."
            raise RuntimeError(message)
        input_service = services.interview_input
        replay = self._replay_product_goal_interview(request) if check_replay else None
        if replay is not None:
            return replay
        decision = _guarded_product_goal_interview_decision(position, request)
        if decision is None:
            return _stale_product_goal_interview(position)
        input_payload = input_service.build(
            project_id=request.project_id,
            decision=decision,
            user_text=request.user_text,
        )
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=request.graph_version,
                fact_fingerprint=request.fact_fingerprint,
                decision_fingerprint=request.decision_fingerprint,
                node_id=node_id,
                input_payload=input_payload,
                model_id=get_model_id(AGENTIC_MODEL_ROLES[node_id]),
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def _replay_product_goal_interview(
        self,
        request: ProductGoalInterviewRequest,
    ) -> TransitionResult | None:
        services = self._product_goal_services
        if services is None:
            message = "Product Goal interview requires an injected input builder."
            raise RuntimeError(message)
        return services.interview_input.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=request.graph_version,
                fact_fingerprint=request.fact_fingerprint,
                decision_fingerprint=request.decision_fingerprint,
                node_id="goal.interview",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                user_text=ProductGoalInterviewInput.normalize_user_response(
                    request.user_text
                ),
            )
        )

    def review_product_goal(
        self,
        request: ProductGoalReviewRequest,
    ) -> TransitionResult:
        """Resolve the exact pending Goal identity internally before review."""
        replay = self._replay_product_goal_transition(
            TransitionReplayQuery(
                request_kind="decide_product_goal_review",
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input={
                    "decision": request.decision,
                    "rationale": request.rationale,
                },
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "goal.review")
        reference = (
            None
            if decision is None
            else _single_fact_reference(decision, "product_goal")
        )
        if decision is None or reference is None:
            return _transition_not_available(position, "goal.review")
        if not _review_candidate_matches(
            expected=request.expected_candidate_fingerprint,
            current=reference.fingerprint,
        ):
            return _stale_review_candidate(position, "Product Goal")
        return self.transition(
            DecideProductGoalReview(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                product_goal_artifact_id=int(reference.fact_id),
                product_goal_fingerprint=reference.fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def resolve_product_goal(
        self,
        request: ProductGoalOutcomeRequest,
    ) -> TransitionResult:
        """Resolve the exact active Goal identity internally at terminal outcome."""
        node_id = "goal.fulfill" if request.outcome == "fulfilled" else "goal.abandon"
        request_kind = (
            "fulfill_product_goal"
            if request.outcome == "fulfilled"
            else "abandon_product_goal"
        )
        replay = self._replay_product_goal_transition(
            TransitionReplayQuery(
                request_kind=request_kind,
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input={"rationale": request.rationale},
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, node_id)
        reference = (
            None
            if decision is None
            else _single_fact_reference(decision, "product_goal")
        )
        if decision is None or reference is None:
            return _transition_not_available(position, node_id)
        request_type = (
            FulfillProductGoal if request.outcome == "fulfilled" else AbandonProductGoal
        )
        return self.transition(
            request_type(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                product_goal_artifact_id=int(reference.fact_id),
                product_goal_fingerprint=reference.fingerprint,
                rationale=request.rationale,
            )
        )

    def record_discovery(
        self,
        request: DiscoveryArtifactRequest,
    ) -> TransitionResult:
        """Record semantic discovery content under graph-selected durable parents."""
        replay = self._replay_product_goal_transition(
            TransitionReplayQuery(
                request_kind="record_discovery_artifact",
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input={
                    "canonical_content": request.canonical_content,
                    "content_ref": request.content_ref,
                },
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "discovery.record")
        if decision is None:
            return _transition_not_available(position, "discovery.record")
        return self.transition(
            RecordDiscoveryArtifact(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                canonical_content=request.canonical_content,
                content_ref=request.content_ref,
            )
        )

    def record_specification_candidate(
        self,
        request: SpecificationCandidateRequest,
    ) -> TransitionResult:
        """Record semantic specification content with host-derived lineage."""
        replay = self._replay_product_goal_transition(
            TransitionReplayQuery(
                request_kind="record_specification_candidate",
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input={
                    "canonical_content": request.canonical_content,
                    "content_ref": request.content_ref,
                },
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "specification.record")
        if decision is None:
            return _transition_not_available(position, "specification.record")
        services = self._product_goal_services
        if services is None:
            message = "Specification candidates require an injected lineage selector."
            raise RuntimeError(message)
        return self.transition(
            RecordSpecificationCandidate(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                canonical_content=request.canonical_content,
                content_ref=request.content_ref,
                supersedes_specification_candidate_id=(
                    services.discovery_selection.resolve_specification_supersedes(
                        request.project_id
                    )
                ),
            )
        )

    def review_specification(
        self,
        request: SpecificationReviewRequest,
    ) -> TransitionResult:
        """Resolve the exact pending specification candidate before review."""
        replay = self._replay_product_goal_transition(
            TransitionReplayQuery(
                request_kind="decide_specification",
                project_id=request.project_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                operator_input={
                    "decision": request.decision,
                    "rationale": request.rationale,
                },
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "specification.review")
        reference = (
            None
            if decision is None
            else _single_fact_reference(decision, "specification_candidate")
        )
        if decision is None or reference is None:
            return _transition_not_available(position, "specification.review")
        if not _review_candidate_matches(
            expected=request.expected_candidate_fingerprint,
            current=reference.fingerprint,
        ):
            return _stale_review_candidate(position, "Specification")
        return self.transition(
            DecideSpecification(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                specification_candidate_id=int(reference.fact_id),
                specification_fingerprint=reference.fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def compile_authority(
        self,
        request: AuthorityCompileRequest,
    ) -> TransitionResult:
        """Prepare current guards and compiler input from durable facts once."""
        input_service = self._authority_compilation_input
        if input_service is None:
            message = "Authority compilation requires an injected input builder."
            raise RuntimeError(message)
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id="authority.compile",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "authority.compile")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "authority.compile")
        model_id = get_model_id(AGENTIC_MODEL_ROLES["authority.compile"])
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id="authority.compile",
                instance_key=decision.instance_key,
                input_payload=input_service.build(
                    project_id=request.project_id,
                    decision=decision,
                    compiler_model=model_id,
                ),
                model_id=model_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def decide_authority(
        self,
        request: AuthorityReviewRequest,
    ) -> TransitionResult:
        """Resolve current authority and review fingerprints internally."""
        selection = self._authority_review_selection
        if selection is not None:
            replay = selection.replay_transition(
                TransitionReplayQuery(
                    request_kind="decide_authority",
                    project_id=request.project_id,
                    idempotency_key=request.idempotency_key,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                    operator_input={
                        "decision": request.decision,
                        "rationale": request.rationale,
                    },
                )
            )
            if replay is not None:
                return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "authority.review")
        identity = (
            None
            if decision is None or selection is None
            else selection.review_identity(project_id=request.project_id)
        )
        reference = (
            None if decision is None else _single_fact_reference(decision, "authority")
        )
        if decision is None or identity is None or reference is None:
            return _transition_not_available(position, "authority.review")
        authority_id, authority_fingerprint, review_fingerprint = identity
        if (
            authority_id != int(reference.fact_id)
            or authority_fingerprint != reference.fingerprint
        ):
            return _transition_not_available(position, "authority.review")
        if not _review_candidate_matches(
            expected=request.expected_candidate_fingerprint,
            current=authority_fingerprint,
        ):
            return _stale_review_candidate(position, "Authority")
        return self.transition(
            DecideAuthority(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                pending_authority_id=authority_id,
                authority_fingerprint=authority_fingerprint,
                review_fingerprint=review_fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def record_authority_feedback(
        self,
        request: AuthorityFeedbackRequest,
    ) -> TransitionResult:
        """Record one human feedback statement for the rejected authority."""
        feedback: JsonObject = {"text": request.feedback}
        selection = self._authority_review_selection
        if selection is not None:
            replay = selection.replay_transition(
                TransitionReplayQuery(
                    request_kind="record_authority_feedback",
                    project_id=request.project_id,
                    idempotency_key=request.idempotency_key,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                    operator_input={"feedback": feedback},
                )
            )
            if replay is not None:
                return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "authority.feedback")
        identity = (
            None if decision is None else _integer_fact_reference(decision, "authority")
        )
        if (
            decision is None
            or decision.category is not NodeCategory.AVAILABLE
            or identity is None
        ):
            return _transition_not_available(position, "authority.feedback")
        authority_id, reference = identity
        return self.transition(
            RecordAuthorityFeedback(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                pending_authority_id=authority_id,
                authority_fingerprint=reference.fingerprint,
                feedback=feedback,
            )
        )

    def repair_authority(
        self,
        request: AuthorityRepairRequest,
    ) -> TransitionResult:
        """Prepare current repair guards and compiler input from durable facts."""
        input_service = self._authority_repair_input
        if input_service is None:
            return _transition_not_available(
                self.position(project_id=request.project_id),
                "authority.repair",
            )
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id="authority.repair",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "authority.repair")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "authority.repair")
        input_payload = input_service.build(
            project_id=request.project_id,
            decision=decision,
        )
        if input_payload is None:
            return _transition_not_available(position, "authority.repair")
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id="authority.repair",
                instance_key=decision.instance_key,
                input_payload=input_payload,
                model_id=get_model_id(AGENTIC_MODEL_ROLES["authority.repair"]),
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def _replay_product_goal_transition(
        self, query: TransitionReplayQuery
    ) -> TransitionResult | None:
        """Replay all Product Goal child-graph commands before state reads."""
        services = self._product_goal_services
        if services is None:
            return None
        return services.interview_input.replay_transition(query)


def _guarded_vision_interview_decision(
    position: WorkflowPosition,
    request: VisionInterviewRequest,
) -> NodeDecision | None:
    """Return the exact current Vision interview decision supplied by the caller."""
    if (
        position.graph_version != request.graph_version
        or position.fact_fingerprint != request.fact_fingerprint
    ):
        return None
    return next(
        (
            decision
            for decision in position.decisions
            if decision.node_id == "vision.interview"
            and decision.category is NodeCategory.AVAILABLE
            and decision.decision_fingerprint == request.decision_fingerprint
        ),
        None,
    )


def _guarded_product_goal_interview_decision(
    position: WorkflowPosition,
    request: ProductGoalInterviewRequest,
) -> NodeDecision | None:
    """Return the exact current Goal interview decision supplied by the caller."""
    if (
        position.graph_version != request.graph_version
        or position.fact_fingerprint != request.fact_fingerprint
    ):
        return None
    return next(
        (
            decision
            for decision in position.decisions
            if decision.node_id == "goal.interview"
            and decision.category is NodeCategory.AVAILABLE
            and decision.decision_fingerprint == request.decision_fingerprint
        ),
        None,
    )


def _integer_fact_reference(
    decision: NodeDecision,
    fact_type: str,
) -> tuple[int, FactReference] | None:
    reference = _single_fact_reference(decision, fact_type)
    if reference is None:
        return None
    try:
        return int(reference.fact_id), reference
    except ValueError:
        return None


def _optional_fact_reference(
    decision: NodeDecision,
    fact_type: str,
) -> tuple[bool, FactReference | None]:
    references = tuple(
        item for item in decision.fact_references if item.fact_type == fact_type
    )
    return (len(references) <= 1, references[0] if references else None)


def _delivery_lineage(
    session: Session,
    *,
    project_id: int,
    decision: NodeDecision,
) -> _DeliveryLineage | None:
    authority_target = _integer_fact_reference(decision, "authority")
    goal_target = _integer_fact_reference(decision, "product_goal")
    if authority_target is None or goal_target is None:
        return None
    authority_id, authority_reference = authority_target
    goal_id, goal_reference = goal_target
    authority = session.get(CompiledSpecAuthority, authority_id)
    goal = session.get(ProductGoalArtifact, goal_id)
    spec = (
        None
        if authority is None
        else session.get(SpecRegistry, authority.spec_version_id)
    )
    vision = (
        None if goal is None else session.get(VisionArtifact, goal.vision_artifact_id)
    )
    if (
        authority is None
        or goal is None
        or spec is None
        or vision is None
        or authority.compiled_artifact_json is None
        or pending_authority_fingerprint(authority) != authority_reference.fingerprint
        or goal.project_id != project_id
        or goal.product_goal_artifact_id != goal_id
        or goal.content_fingerprint != goal_reference.fingerprint
        or vision.project_id != project_id
        or vision.vision_artifact_id != goal.vision_artifact_id
        or vision.content_fingerprint != goal.vision_fingerprint
        or spec.project_id != project_id
        or spec.status != "approved"
        or spec.source_vision_artifact_id != goal.vision_artifact_id
        or spec.source_vision_fingerprint != goal.vision_fingerprint
        or spec.source_product_goal_artifact_id != goal_id
        or spec.source_product_goal_fingerprint != goal.content_fingerprint
    ):
        return None
    return _DeliveryLineage(
        authority=authority,
        goal=goal,
        spec=spec,
        vision=vision,
    )


def _canonical_artifact(
    content_json: str,
    fingerprint: str,
) -> JsonObject | None:
    try:
        content = _JSON_OBJECT.validate_json(content_json)
    except ValidationError:
        return None
    if (
        canonical_json(content) != content_json
        or canonical_hash(content) != fingerprint
    ):
        return None
    return content


def _backlog_input(
    session: Session,
    decision: NodeDecision,
    lineage: _DeliveryLineage,
) -> JsonObject | None:
    valid_prior, prior_reference = _optional_fact_reference(decision, "backlog")
    if not valid_prior:
        return None
    prior_state = "NO_HISTORY"
    user_input: str | None = None
    if prior_reference is not None:
        try:
            prior_id = int(prior_reference.fact_id)
        except ValueError:
            return None
        prior = session.get(BacklogArtifact, prior_id)
        if (
            prior is None
            or prior.project_id != lineage.goal.project_id
            or prior.content_fingerprint != prior_reference.fingerprint
            or prior.product_goal_artifact_id != lineage.goal.product_goal_artifact_id
            or prior.product_goal_fingerprint != lineage.goal.content_fingerprint
        ):
            return None
        prior_content = _canonical_artifact(
            prior.canonical_content_json,
            prior.content_fingerprint,
        )
        if prior_content is None:
            return None
        BacklogOutput.model_validate(prior_content)
        prior_state = prior.canonical_content_json
        review = session.exec(
            select(BacklogArtifactDecision).where(
                col(BacklogArtifactDecision.project_id) == prior.project_id,
                col(BacklogArtifactDecision.backlog_artifact_id) == prior_id,
            )
        ).one_or_none()
        if review is not None and review.decision in {"feedback", "rejected"}:
            user_input = review.rationale
    payload = BacklogInput(
        product_vision_statement=lineage.vision.statement,
        product_goal_statement=lineage.goal.statement,
        technical_spec=lineage.spec.content,
        compiled_authority=cast("str", lineage.authority.compiled_artifact_json),
        prior_backlog_state=prior_state,
        user_input=user_input,
    )
    return _JSON_OBJECT.validate_python(payload.model_dump(mode="json"))


def _required_backlog(
    session: Session,
    decision: NodeDecision,
    lineage: _DeliveryLineage,
) -> tuple[BacklogArtifact, BacklogOutput] | None:
    target = _integer_fact_reference(decision, "backlog")
    if target is None:
        return None
    backlog_id, reference = target
    backlog = session.get(BacklogArtifact, backlog_id)
    if (
        backlog is None
        or backlog.project_id != lineage.goal.project_id
        or backlog.content_fingerprint != reference.fingerprint
        or backlog.authority_id != lineage.authority.authority_id
        or backlog.authority_fingerprint
        != pending_authority_fingerprint(lineage.authority)
        or backlog.product_goal_artifact_id != lineage.goal.product_goal_artifact_id
        or backlog.product_goal_fingerprint != lineage.goal.content_fingerprint
    ):
        return None
    content = _canonical_artifact(
        backlog.canonical_content_json,
        backlog.content_fingerprint,
    )
    if content is None:
        return None
    return backlog, BacklogOutput.model_validate(content)


def _required_roadmap(
    session: Session,
    decision: NodeDecision,
    backlog: BacklogArtifact,
) -> tuple[RoadmapArtifact, RoadmapBuilderOutput] | None:
    target = _integer_fact_reference(decision, "roadmap")
    if target is None:
        return None
    roadmap_id, reference = target
    roadmap = session.get(RoadmapArtifact, roadmap_id)
    if (
        roadmap is None
        or roadmap.project_id != backlog.project_id
        or roadmap.content_fingerprint != reference.fingerprint
        or roadmap.backlog_artifact_id != backlog.backlog_artifact_id
        or roadmap.backlog_artifact_fingerprint != backlog.content_fingerprint
    ):
        return None
    content = _canonical_artifact(
        roadmap.canonical_content_json,
        roadmap.content_fingerprint,
    )
    if content is None:
        return None
    return roadmap, RoadmapBuilderOutput.model_validate(content)


def _roadmap_input(
    session: Session,
    decision: NodeDecision,
    lineage: _DeliveryLineage,
) -> JsonObject | None:
    backlog_source = _required_backlog(session, decision, lineage)
    valid_prior, prior_reference = _optional_fact_reference(decision, "roadmap")
    if backlog_source is None or not valid_prior:
        return None
    backlog, backlog_output = backlog_source
    prior_output: RoadmapBuilderOutput | None = None
    user_input: str | None = None
    if prior_reference is not None:
        try:
            prior_id = int(prior_reference.fact_id)
        except ValueError:
            return None
        prior = session.get(RoadmapArtifact, prior_id)
        if (
            prior is None
            or prior.project_id != backlog.project_id
            or prior.content_fingerprint != prior_reference.fingerprint
            or prior.backlog_artifact_id != backlog.backlog_artifact_id
            or prior.backlog_artifact_fingerprint != backlog.content_fingerprint
        ):
            return None
        prior_content = _canonical_artifact(
            prior.canonical_content_json,
            prior.content_fingerprint,
        )
        if prior_content is None:
            return None
        prior_output = RoadmapBuilderOutput.model_validate(prior_content)
        review = session.exec(
            select(RoadmapArtifactDecision).where(
                col(RoadmapArtifactDecision.project_id) == prior.project_id,
                col(RoadmapArtifactDecision.roadmap_artifact_id) == prior_id,
            )
        ).one_or_none()
        if review is not None and review.decision in {"feedback", "rejected"}:
            user_input = review.rationale
    state: dict[str, Any] = {
        "product_vision_assessment": {
            "product_vision_statement": lineage.vision.statement
        },
        "backlog_items": [
            item.model_dump(mode="json") for item in backlog_output.backlog_items
        ],
        "pending_spec_content": lineage.spec.content,
        "compiled_authority_cached": lineage.authority.compiled_artifact_json,
    }
    if prior_output is not None:
        state["roadmap_releases"] = [
            item.model_dump(mode="json") for item in prior_output.roadmap_releases
        ]
    context = build_roadmap_input_context(state, user_input=user_input)
    payload = RoadmapBuilderInput.model_validate(context)
    return _JSON_OBJECT.validate_python(payload.model_dump(mode="json"))


def _story_outputs(
    session: Session,
    *,
    project_id: int,
    roadmap: RoadmapArtifact,
) -> dict[str, object]:
    rows = session.exec(
        select(StoryArtifact)
        .where(col(StoryArtifact.project_id) == project_id)
        .order_by(
            col(StoryArtifact.requirement_id),
            col(StoryArtifact.version_number),
        )
    ).all()
    latest = {row.requirement_id: row for row in rows}
    decisions = session.exec(
        select(StoryArtifactDecision).where(
            col(StoryArtifactDecision.project_id) == project_id
        )
    ).all()
    decisions_by_id = {item.story_artifact_id: item for item in decisions}
    outputs: dict[str, object] = {}
    for row in latest.values():
        artifact_id = row.story_artifact_id
        if (
            artifact_id is None
            or row.roadmap_artifact_id != roadmap.roadmap_artifact_id
            or row.roadmap_artifact_fingerprint != roadmap.content_fingerprint
        ):
            continue
        review = decisions_by_id.get(artifact_id)
        if review is not None and review.decision != "accepted":
            continue
        content = _canonical_artifact(
            row.canonical_content_json,
            row.content_fingerprint,
        )
        if content is None:
            continue
        output = UserStoryWriterOutput.model_validate(content)
        outputs[output.parent_requirement] = output.model_dump(mode="json")
    return outputs


def _story_input(
    session: Session,
    decision: NodeDecision,
    lineage: _DeliveryLineage,
) -> JsonObject | None:
    backlog_source = _required_backlog(session, decision, lineage)
    requirement_reference = _single_fact_reference(decision, "backlog_requirement")
    if backlog_source is None or requirement_reference is None:
        message = "Story input requires exact Backlog and requirement references."
        raise ValueError(message)
    backlog, backlog_output = backlog_source
    roadmap_source = _required_roadmap(session, decision, backlog)
    if roadmap_source is None:
        message = "Story input requires an exact accepted Roadmap reference."
        raise ValueError(message)
    roadmap, roadmap_output = roadmap_source
    requirement_id = requirement_reference.fact_id
    if (
        requirement_reference.fingerprint != backlog.content_fingerprint
        or decision.instance_key != f"requirement:{requirement_id}"
    ):
        message = "Story requirement selection does not match durable Backlog facts."
        raise ValueError(message)
    requirement = next(
        (
            item
            for item in backlog_output.backlog_items
            if normalize_requirement_key(item.requirement) == requirement_id
        ),
        None,
    )
    if requirement is None:
        message = "Story requirement is absent from the durable Backlog."
        raise ValueError(message)
    state: dict[str, Any] = {
        "roadmap_releases": [
            item.model_dump(mode="json") for item in roadmap_output.roadmap_releases
        ],
        "pending_spec_content": lineage.spec.content,
        "compiled_authority_cached": lineage.authority.compiled_artifact_json,
        "story_outputs": _story_outputs(
            session,
            project_id=backlog.project_id,
            roadmap=roadmap,
        ),
    }
    context = build_story_input_context(
        state,
        parent_requirement=requirement.requirement,
    )
    context["requirement_context"] = (
        f"{context['requirement_context']}\n"
        f"Backlog priority: {requirement.priority}\n"
        f"Business justification: {requirement.justification}\n"
        f"Technical note: {requirement.technical_note or 'None'}"
    )
    valid_prior, prior_reference = _optional_fact_reference(decision, "story")
    if not valid_prior:
        message = "Story input has ambiguous prior Story references."
        raise ValueError(message)
    if prior_reference is not None:
        try:
            prior_id = int(prior_reference.fact_id)
        except ValueError as error:
            message = "Prior Story reference is not a durable integer identity."
            raise ValueError(message) from error
        prior = session.get(StoryArtifact, prior_id)
        if (
            prior is None
            or prior.project_id != backlog.project_id
            or prior.requirement_id != requirement_id
            or prior.roadmap_artifact_id != roadmap.roadmap_artifact_id
            or prior.roadmap_artifact_fingerprint != roadmap.content_fingerprint
            or prior.content_fingerprint != prior_reference.fingerprint
        ):
            message = "Prior Story reference does not match durable lineage."
            raise ValueError(message)
        prior_content = _canonical_artifact(
            prior.canonical_content_json,
            prior.content_fingerprint,
        )
        if prior_content is None:
            message = "Prior Story content is not canonical."
            raise ValueError(message)
        UserStoryWriterOutput.model_validate(prior_content)
        review = session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == prior.project_id,
                col(StoryArtifactDecision.story_artifact_id) == prior_id,
            )
        ).one_or_none()
        review_context = (
            ""
            if review is None
            else (
                f"\nReview outcome: {review.decision}"
                f"\nReview rationale: {review.rationale}"
            )
        )
        context["requirement_context"] = (
            f"{context['requirement_context']}\n\n"
            f"Previous durable Story draft: {prior.canonical_content_json}"
            f"{review_context}"
        )
    payload = UserStoryWriterInput.model_validate(context)
    return _JSON_OBJECT.validate_python(payload.model_dump(mode="json"))


type _SprintCapacity = tuple[
    int,
    Literal["user_override", "project_metrics"],
    str,
]


def _resolve_sprint_capacity(
    *,
    snapshot: WorkflowFactSnapshot,
    requested_capacity: int | None,
) -> _SprintCapacity | None:
    """Resolve capacity from an explicit limit or completed-Sprint metrics."""
    if requested_capacity is not None:
        return (
            requested_capacity,
            "user_override",
            f"{requested_capacity} points provided by the operator.",
        )
    metrics = build_durable_sprint_metrics(snapshot)
    recommendation = metrics.get("recommendation")
    if not isinstance(recommendation, dict):
        return None
    points = recommendation.get("recommended_next_sprint_points")
    source_points = recommendation.get("source_completed_points")
    sample_size = recommendation.get("sample_size")
    if (
        isinstance(points, bool)
        or not isinstance(points, int)
        or points <= 0
        or not isinstance(source_points, list)
        or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in source_points
        )
        or isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size <= 0
        or len(source_points) != sample_size
    ):
        return None
    return (
        points,
        "project_metrics",
        (
            f"{points} points, based on the last {sample_size} completed "
            f"Sprints: {', '.join(str(value) for value in source_points)}."
        ),
    )


def _sprint_selection_rows(
    session: Session,
    *,
    project_id: int,
    candidates: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
) -> list[dict[str, Any]]:
    """Convert exact candidate rows and evidence to deterministic selector input."""
    candidate_ids = {item.story_id for item in candidates}
    stories = session.exec(
        select(UserStory).where(
            col(UserStory.project_id) == project_id,
            col(UserStory.story_id).in_(candidate_ids),
        )
    ).all()
    stories_by_id = {
        story.story_id: story for story in stories if story.story_id is not None
    }
    if set(stories_by_id) != candidate_ids:
        message = "Current Sprint candidate rows are incomplete."
        raise ValueError(message)
    prerequisites: dict[int, list[int]] = {story_id: [] for story_id in candidate_ids}
    for dependency in dependencies:
        if dependency.status != "active" or dependency.dependent_story_id not in (
            candidate_ids
        ):
            continue
        prerequisites[dependency.dependent_story_id].append(
            dependency.prerequisite_story_id
        )
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        story = stories_by_id[candidate.story_id]
        if (
            story.project_id != project_id
            or story.is_superseded
            or not story.is_refined
            or story.accepted_spec_version_id is None
            or story.story_points != candidate.story_points
            or story.rank != candidate.rank
            or story.story_points is None
            or story.story_points <= 0
        ):
            message = (
                f"Story {candidate.story_id} no longer matches its candidate facts."
            )
            raise ValueError(message)
        priority = _sprint_story_priority(story)
        evaluated_ids, boundary_summaries = _sprint_validation_evidence(story)
        prerequisite_ids = sorted(set(prerequisites[candidate.story_id]))
        blocked_by_ids = [
            story_id for story_id in prerequisite_ids if story_id in candidate_ids
        ]
        planner_story = SprintPlannerStory(
            story_id=candidate.story_id,
            story_title=story.title,
            priority=priority,
            story_points=story.story_points,
            parent_group=derive_parent_group(priority),
            group_slot=derive_group_slot(priority),
            story_description=story.story_description or story.title,
            acceptance_criteria_items=_sprint_acceptance_items(
                story.acceptance_criteria
            ),
            persona=story.persona,
            source_requirement=story.source_requirement,
            prerequisite_story_ids=prerequisite_ids,
            blocked_by_story_ids=blocked_by_ids,
            dependency_status="blocked" if blocked_by_ids else "ready",
            evaluated_invariant_ids=evaluated_ids,
            story_compliance_boundary_summaries=boundary_summaries,
        )
        rows.append(
            {
                "story_id": candidate.story_id,
                "priority": priority,
                "story_points": story.story_points,
                "blocked_by_story_ids": blocked_by_ids,
                "planner_story": planner_story.model_dump(mode="json"),
            }
        )
    return rows


def _sprint_story_priority(story: UserStory) -> int:
    """Parse one durable Story rank into the selector's numeric priority."""
    try:
        return parse_story_rank(story.rank)
    except ValueError as error:
        message = f"Story {story.story_id} has an invalid rank: {error}"
        raise ValueError(message) from error


def _sprint_validation_evidence(story: UserStory) -> tuple[list[str], list[str]]:
    """Extract model-visible invariant and boundary evidence from one Story row."""
    if story.validation_evidence is None:
        return [], []
    evidence = ValidationEvidence.model_validate_json(story.validation_evidence)
    summaries = [
        item.message
        for item in (*evidence.alignment_warnings, *evidence.alignment_failures)
    ]
    return list(evidence.evaluated_invariant_ids), summaries


def _sprint_acceptance_items(value: str | None) -> list[str]:
    """Return durable acceptance criteria as compact model-visible lines."""
    if value is None:
        return []
    return [
        line.lstrip("-* \t").strip()
        for line in value.splitlines()
        if line.lstrip("-* \t").strip()
    ]


def _sprint_input_error(*, code: str, message: str) -> WorkflowError:
    """Return one structured deterministic host-preparation failure."""
    return WorkflowError(
        code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        message=message,
        blockers=(Blocker(code=code, message=message),),
    )


def _sprint_capacity_error() -> WorkflowError:
    """Return the public remediation when no positive capacity can be resolved."""
    message = (
        "Sprint planning requires --max-story-points because durable completed-"
        "Sprint metrics do not provide a recommendation."
    )
    return WorkflowError(
        code=WorkflowErrorCode.SPRINT_CAPACITY_REQUIRED,
        message=message,
        blockers=(Blocker(code="SPRINT_CAPACITY_REQUIRED", message=message),),
    )


def _canonical_dependency_edges(
    edges: tuple[StoryDependencyEdgeRequest, ...],
) -> tuple[ReviewedDependencyEdge, ...]:
    return tuple(
        ReviewedDependencyEdge(
            dependent_story_id=item.dependent_story_id,
            prerequisite_story_id=item.prerequisite_story_id,
            reason=item.reason,
        )
        for item in sorted(
            edges,
            key=lambda edge: (
                edge.dependent_story_id,
                edge.prerequisite_story_id,
            ),
        )
    )


def _canonical_story_readiness_repairs(
    repairs: tuple[StoryReadinessRepair, ...],
) -> tuple[StoryReadinessUpdate, ...]:
    return tuple(
        StoryReadinessUpdate(
            story_id=item.story_id,
            story_points=item.story_points,
            rank=item.rank,
        )
        for item in sorted(repairs, key=lambda repair: repair.story_id)
    )


def _unique_available_decision(
    position: WorkflowPosition,
    node_id: str,
    *,
    instance_key: str | None = None,
) -> NodeDecision | None:
    """Return one current command decision without accepting an ambiguous position."""
    candidates = tuple(
        item
        for item in position.decisions
        if item.node_id == node_id
        and item.category in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
        and (instance_key is None or item.instance_key == instance_key)
    )
    return candidates[0] if len(candidates) == 1 else None


def _unique_execution_decision(
    position: WorkflowPosition,
    *,
    request_kind: str,
    node_id: str,
    instance_key: str,
) -> NodeDecision | None:
    """Select one exact execution decision by kind, node, and public selector."""
    candidates = tuple(
        item
        for item in position.decisions
        if item.request_kind == request_kind
        and item.node_id == node_id
        and item.instance_key == instance_key
        and item.category in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
    )
    return candidates[0] if len(candidates) == 1 else None


def _instance_identity(decision: NodeDecision, prefix: str) -> int | None:
    """Parse one exact positive numeric semantic instance selector."""
    instance_key = decision.instance_key
    expected_prefix = f"{prefix}:"
    if instance_key is None or not instance_key.startswith(expected_prefix):
        return None
    try:
        identity = int(instance_key.removeprefix(expected_prefix))
    except ValueError:
        return None
    return identity if identity > 0 else None


def _single_sprint_id(
    snapshot: WorkflowFactSnapshot | None,
    *,
    status: Literal["active", "completed"],
) -> int | None:
    """Return one exact durable Sprint identity for a lifecycle status."""
    if snapshot is None:
        return None
    sprint_ids = tuple(
        item.sprint_id for item in snapshot.sprints if item.status == status
    )
    return sprint_ids[0] if len(sprint_ids) == 1 else None


def _current_post_sprint_triage(
    rows: tuple[PostSprintTriageFact, ...],
) -> tuple[bool, PostSprintTriageFact | None]:
    """Return the one append-only triage tip only when the chain is exact."""
    if not rows:
        return True, None
    by_id = {item.triage_id: item for item in rows}
    if len(by_id) != len(rows):
        return False, None
    parent_ids = tuple(
        item.supersedes_triage_id
        for item in rows
        if item.supersedes_triage_id is not None
    )
    if len(set(parent_ids)) != len(parent_ids) or any(
        parent_id not in by_id for parent_id in parent_ids
    ):
        return False, None
    tips = tuple(item for item in rows if item.triage_id not in set(parent_ids))
    if len(tips) != 1:
        return False, None
    seen: set[int] = set()
    current = tips[0]
    while True:
        if current.triage_id in seen:
            return False, None
        seen.add(current.triage_id)
        parent_id = current.supersedes_triage_id
        if parent_id is None:
            break
        current = by_id[parent_id]
    return (len(seen) == len(rows), tips[0] if len(seen) == len(rows) else None)


def _single_fact_reference(
    decision: NodeDecision,
    fact_type: str,
) -> FactReference | None:
    """Return one exact graph reference without accepting an ambiguous target."""
    references = tuple(
        item for item in decision.fact_references if item.fact_type == fact_type
    )
    return references[0] if len(references) == 1 else None


def planning_action_decision_is_transportable(
    project_id: int,
    decision: NodeDecision,
) -> bool:
    """Return whether one planning decision has the references its route needs."""
    if decision.request_kind == "apply_story_dependencies":
        reference = _single_fact_reference(decision, "story_dependency_source")
        return reference is not None and reference.fact_id == str(project_id)
    if decision.request_kind == "repair_story_readiness":
        reference = _single_fact_reference(decision, "story_readiness")
        return reference is not None and reference.fact_id == str(project_id)
    if decision.request_kind == "start_sprint":
        candidate_reference = _single_fact_reference(decision, "candidate_set")
        return (
            _integer_fact_reference(decision, "sprint_plan") is not None
            and candidate_reference is not None
            and candidate_reference.fact_id == str(project_id)
            and _integer_fact_reference(decision, "sprint_plan_tasks") is not None
        )
    return True


_SINGLE_REFERENCE_EXECUTION_ACTIONS = {
    "complete_task": ("task", "task"),
    "close_story": ("story", "story_completion"),
    "review_sprint": ("sprint", "sprint_review"),
}


def execution_action_decision_is_transportable(decision: NodeDecision) -> bool:
    """Return whether one execution decision has an exact public selector target."""
    simple_contract = _SINGLE_REFERENCE_EXECUTION_ACTIONS.get(decision.request_kind)
    if simple_contract is not None:
        return _single_reference_execution_action_is_transportable(
            decision,
            instance_prefix=simple_contract[0],
            fact_type=simple_contract[1],
        )
    if decision.request_kind == "close_sprint":
        return _sprint_close_decision_is_transportable(decision)
    if decision.request_kind == "record_post_sprint_triage":
        return _post_sprint_triage_decision_is_transportable(decision)
    return True


def _single_reference_execution_action_is_transportable(
    decision: NodeDecision,
    *,
    instance_prefix: str,
    fact_type: str,
) -> bool:
    """Match one exact instance selector to one required fact reference."""
    identity = _instance_identity(decision, instance_prefix)
    reference = _integer_fact_reference(decision, fact_type)
    return (
        _fact_reference_shape(decision, required=(fact_type,))
        and identity is not None
        and reference is not None
        and reference[0] == identity
    )


def _sprint_close_decision_is_transportable(decision: NodeDecision) -> bool:
    """Validate the exact Sprint, review, and close references for closure."""
    required = ("sprint", "sprint_review", "sprint_close")
    identity = _instance_identity(decision, "sprint")
    references = tuple(
        _integer_fact_reference(decision, fact_type) for fact_type in required
    )
    return (
        _fact_reference_shape(decision, required=required)
        and identity is not None
        and all(
            reference is not None and reference[0] == identity
            for reference in references
        )
    )


def _post_sprint_triage_decision_is_transportable(
    decision: NodeDecision,
) -> bool:
    """Validate completed-Sprint identity and optional prior triage reference."""
    if not _fact_reference_shape(
        decision,
        required=("sprint_closure",),
        optional=("post_sprint_triage",),
    ):
        return False
    identity = _instance_identity(decision, "sprint")
    reference = _integer_fact_reference(decision, "sprint_closure")
    triage_valid, triage_reference = _optional_fact_reference(
        decision,
        "post_sprint_triage",
    )
    try:
        triage_id = None if triage_reference is None else int(triage_reference.fact_id)
    except ValueError:
        return False
    return (
        triage_valid
        and (triage_id is None or triage_id > 0)
        and identity is not None
        and reference is not None
        and reference[0] == identity
    )


def _fact_reference_shape(
    decision: NodeDecision,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> bool:
    """Require exactly one required reference and at most one optional reference."""
    allowed = frozenset((*required, *optional))
    if any(item.fact_type not in allowed for item in decision.fact_references):
        return False
    counts = Counter(item.fact_type for item in decision.fact_references)
    return all(counts[item] == 1 for item in required) and all(
        counts[item] <= 1 for item in optional
    )


def _transition_not_available(
    position: WorkflowPosition | None,
    node_id: str,
) -> TransitionResult:
    """Return one structured conflict when semantic selection is not unique."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
            message=f"No unique {node_id} transition is currently available.",
        ),
    )


def _review_candidate_matches(*, expected: str | None, current: str) -> bool:
    """Allow semantic callers to omit a guard while binding browser reviews."""
    return expected is None or expected == current


def _stale_review_candidate(
    position: WorkflowPosition,
    subject: str,
) -> TransitionResult:
    """Fail closed when the browser reviewed a replaced candidate."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message=(
                f"The {subject} candidate changed after this review opened. "
                "Reload and review the current candidate."
            ),
        ),
    )


def _sprint_replay_input(request: SprintPlanningRequest) -> JsonObject:
    """Return only caller semantics replaced during durable replay comparison."""
    return {
        "requested_max_story_points": request.max_story_points,
        "requested_story_ids": list(request.selected_story_ids),
        "team_name": request.team_name,
        "include_task_decomposition": request.include_task_decomposition,
        "guidance": request.guidance,
    }


def _stale_vision_interview(position: WorkflowPosition) -> TransitionResult:
    """Reject a Vision attempt whose decision guards no longer match facts."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="The Vision interview position is stale.",
        ),
    )


def _stale_product_goal_interview(position: WorkflowPosition) -> TransitionResult:
    """Reject a Goal interview attempt whose guards no longer match facts."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="The Product Goal interview position is stale.",
        ),
    )


@cache
def production_application() -> AgileForgeApplication:
    """Compose the production domain and exact v2 ADK recipe leaves."""
    from adapters.adk.agents.backlog import root_agent as backlog_agent  # noqa: PLC0415
    from adapters.adk.agents.roadmap import root_agent as roadmap_agent  # noqa: PLC0415
    from adapters.adk.agents.specification import (  # noqa: PLC0415
        build_spec_authority_compiler_agent,
    )
    from adapters.adk.agents.sprint import root_agent as sprint_agent  # noqa: PLC0415
    from adapters.adk.agents.story import (  # noqa: PLC0415
        create_user_story_writer_agent,
    )
    from adapters.adk.agents.vision import (  # noqa: PLC0415
        root_agent as vision_interview_agent,
    )
    from adapters.adk.recipes import (  # noqa: PLC0415
        AgenticRecipeNodes,
        build_agentic_recipe_registry,
    )
    from models.db import ensure_business_db_ready, get_engine  # noqa: PLC0415
    from services.product_discovery_selection import (  # noqa: PLC0415
        ProductDiscoverySelectionService,
    )
    from services.read_projections import (  # noqa: PLC0415
        DurableReadProjectionService,
    )
    from workflow.clock import SystemClock  # noqa: PLC0415
    from workflow.definitions.root import project_graph  # noqa: PLC0415
    from workflow.domain import WorkflowDomain  # noqa: PLC0415

    product_goal_interview_agent = cast(
        "BaseAgent",
        vars(import_module("adapters.adk.agents.product_goal"))["root_agent"],
    )
    vision_repair_agent = cast(
        "BaseAgent",
        vars(import_module("adapters.adk.agents.vision"))["repair_agent"],
    )
    graph = project_graph()
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=build_spec_authority_compiler_agent(),
            authority_repair=build_spec_authority_compiler_agent(),
            vision_interview=vision_interview_agent,
            vision_repair=vision_repair_agent,
            product_goal=product_goal_interview_agent,
            backlog_generation=backlog_agent,
            roadmap_generation=roadmap_agent,
            story_generation=create_user_story_writer_agent(),
            sprint_planning=sprint_agent,
        ),
        execution_settings=_EXECUTION_SETTINGS,
    )
    engine = get_engine()
    ensure_business_db_ready(engine)
    domain = WorkflowDomain(
        engine=engine,
        graph=graph,
        clock=SystemClock(),
        adk_recipe_registry=registry,
    )

    application = AgileForgeApplication(
        workflow_domain=domain,
        recipe_registry=registry,
        read_projection=DurableReadProjectionService(engine=engine),
        vision_input=VisionInputService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ),
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=ProductGoalInterviewInputService(engine=engine),
            discovery_selection=ProductDiscoverySelectionService(engine=engine),
        ),
        authority_compilation_input=AuthorityCompilationInputService(engine=engine),
        authority_review_selection=AuthorityReviewSelectionService(engine=engine),
        authority_repair_input=AuthorityRepairInputService(engine=engine),
        delivery_review_selection=DeliveryReviewSelectionService(engine=engine),
        delivery_action_input=DeliveryActionInputService(engine=engine),
        planning_action_selection=PlanningActionSelectionService(engine=engine),
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
        sprint_planning_input=SprintPlanningInputService(engine=engine),
    )
    application.set_project_lifecycle(
        ProjectLifecycleService(
            engine=engine,
            workflow_domain=domain,
            repository_probe=GitPythonRepositoryProbe(),
        )
    )
    return application


__all__ = [
    "AgenticActionRequest",
    "AgileForgeApplication",
    "AuthorityCompileRequest",
    "AuthorityFeedbackRequest",
    "AuthorityRepairInputService",
    "AuthorityRepairRequest",
    "AuthorityReviewRequest",
    "AuthorityReviewSelectionService",
    "BacklogReviewRequest",
    "CloseStoryRequest",
    "CompleteTaskRequest",
    "CreateProjectCommand",
    "DeliveryActionInputService",
    "DeliveryActionRequest",
    "DeliveryReviewSelectionService",
    "DiscoveryArtifactRequest",
    "ExecutionActionSelectionService",
    "PlanningActionSelectionService",
    "PostSprintTriageRequest",
    "ProductGoalInterviewRequest",
    "ProductGoalLifecycleServices",
    "ProductGoalOutcomeRequest",
    "ProductGoalResponseRequest",
    "ProductGoalReviewRequest",
    "RepositoryAttachRequest",
    "RepositoryAttachmentCommand",
    "RepositoryRefreshCommand",
    "RepositoryRefreshRequest",
    "RoadmapReviewRequest",
    "SemanticTransitionReplayPort",
    "SpecificationCandidateRequest",
    "SpecificationReviewRequest",
    "SprintCloseRequest",
    "SprintPlanReviewRequest",
    "SprintPlanningInputService",
    "SprintPlanningRequest",
    "SprintReviewRequest",
    "SprintStartRequest",
    "StoryDependenciesApplyRequest",
    "StoryDependencyEdgeRequest",
    "StoryReadinessRepair",
    "StoryReadinessRepairRequest",
    "StoryReviewRequest",
    "VisionBootstrapRequest",
    "VisionInterviewRequest",
    "VisionResponseRequest",
    "VisionReviewRequest",
    "VisionRevisionRequest",
    "WorkflowDomainPort",
    "execution_action_decision_is_transportable",
    "planning_action_decision_is_transportable",
    "production_application",
]
