"""Production application boundary for the durable workflow graph."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
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
    assert_never,
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
from models.product_definition import (
    ProductGoalArtifact,
    VisionArtifact,
)
from models.specs import SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintPlanArtifact,
    StoryArtifact,
    StoryArtifactDecision,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.story_phase import (
    load_story_correction_target_in_session,
)
from services.contracts.backlog import BacklogBuilderInput, BacklogOutput
from services.contracts.product_goal import ProductGoalInterviewInput
from services.contracts.roadmap import RoadmapBuilderInput, RoadmapBuilderOutput
from services.contracts.sprint import (
    SprintPlannerInput,
    SprintPlannerStory,
)
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    UserStoryWriterInput,
)
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
from services.specification_source_registration import (
    PreparedSpecificationSourceRegistration,
    SpecificationSourceRegistrationError,
    SpecificationSourceRegistrationErrorCode,
    SpecificationSourceRegistrationRequest,
)
from services.specs.accepted_specification import (
    AcceptedSpecification,
    AcceptedSpecificationIntegrityError,
    require_current_accepted_specification,
)
from services.specs.story_validation_service import (
    StoryValidationReadinessError,
    ValidateStoryInput,
    require_story_ready_for_sprint,
    validate_story_with_specification_in_session,
)
from services.sprint_selection import (
    SprintSelectionError,
    select_sprint_story_rows,
)
from services.story_rank import parse_story_rank, story_rank_is_valid
from services.story_runtime import build_story_input_context
from services.vision_evidence import (
    VisionEvidenceCollectionError,
    VisionEvidenceErrorCode,
)
from services.vision_input import VisionInputService
from utils.model_config import get_model_id
from utils.runtime_config import get_specification_structurer_generation_config
from workflow.contracts import (
    Blocker,
    FactReference,
    FrozenModel,
    JsonObject,
    JsonValue,
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
    DecideBacklog,
    DecideProductGoalReview,
    DecideRoadmap,
    DecideSpecification,
    DecideSprintPlan,
    DecideStory,
    DecideVisionReview,
    FulfillProductGoal,
    RecordPostSprintTriage,
    RegisterSpecificationSource,
    RepairStoryReadiness,
    ReviewSprint,
    StartSprint,
)
from workflow.requests.planning import ReviewedDependencyEdge, StoryReadinessUpdate

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """Workflow transition boundary exposed to application adapters."""

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


class _SpecificationStructuringInputPort(Protocol):
    """Host preparation for one exact Specification structuring attempt."""

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None: ...

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
    ) -> JsonObject: ...

    def revalidate_sources(
        self,
        project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None: ...


class _SpecificationSourceRegistrationPort(Protocol):
    """Capture one byte-exact external source before its guarded transition."""

    def prepare(
        self,
        request: SpecificationSourceRegistrationRequest,
    ) -> PreparedSpecificationSourceRegistration: ...


class _SpecificationSourceReplayPort(Protocol):
    """Recover a completed source command before recapturing repository bytes."""

    def replay(self, query: TransitionReplayQuery) -> TransitionResult | None: ...


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
    ) -> JsonObject | WorkflowError | None: ...

    def build_story_correction(
        self,
        *,
        project_id: int,
        decisions: tuple[NodeDecision, ...],
        request: StoryCorrectionRequest,
    ) -> tuple[NodeDecision, JsonObject] | WorkflowError | None: ...


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
                    == f"backlog_item:{artifact.backlog_item_id}"
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

    accepted_specification: AcceptedSpecification
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
    ) -> JsonObject | WorkflowError | None:
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
        except AcceptedSpecificationIntegrityError as error:
            return _accepted_specification_input_error(error)
        except (ValidationError, ValueError):
            return None
        return payload

    def build_story_correction(
        self,
        *,
        project_id: int,
        decisions: tuple[NodeDecision, ...],
        request: StoryCorrectionRequest,
    ) -> tuple[NodeDecision, JsonObject] | WorkflowError | None:
        """Resolve one accepted operational row to one exact correction decision."""
        try:
            with Session(self.engine) as session:
                target = load_story_correction_target_in_session(
                    session,
                    project_id=project_id,
                    story_id=request.story_id,
                )
                artifact = target.artifact
                instance_key = f"backlog_item:{artifact.backlog_item_id}"
                candidates = tuple(
                    decision
                    for decision in decisions
                    if decision.node_id == "planning.story.generate"
                    and decision.category is NodeCategory.AVAILABLE
                    and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
                    and decision.instance_key == instance_key
                )
                if len(candidates) != 1:
                    return None
                decision = candidates[0]
                lineage = _delivery_lineage(
                    session,
                    project_id=project_id,
                    decision=decision,
                )
                if lineage is None:
                    return None
                prepared = _story_input(session, decision, lineage)
                story_reference = _single_fact_reference(decision, "story")
                if (
                    prepared is None
                    or story_reference is None
                    or story_reference.fact_id != str(artifact.story_artifact_id)
                    or story_reference.fingerprint != artifact.content_fingerprint
                ):
                    return None
                writer_input = UserStoryWriterInput.model_validate(
                    prepared["writer_input"]
                )
                selected = target.item.item.model_dump(mode="json")
                writer_input = writer_input.model_copy(
                    update={
                        "user_input": (
                            "Selected accepted Story:\n"
                            f"{canonical_json(selected)}\n"
                            "Human guidance:\n"
                            f"{request.guidance}"
                        )
                    }
                )
                correction = {
                    "story_id": request.story_id,
                    "guidance": request.guidance,
                    "source_story_artifact_id": artifact.story_artifact_id,
                    "source_story_artifact_fingerprint": artifact.content_fingerprint,
                    "source_story_item_id": target.item.item.story_item_id,
                    "source_story_item_fingerprint": target.item.item_fingerprint,
                }
                return decision, _JSON_OBJECT.validate_python(
                    {
                        **prepared,
                        "writer_input": writer_input.model_dump(mode="json"),
                        "correction": correction,
                        "correction_source": target.content.model_dump(mode="json"),
                    }
                )
        except AcceptedSpecificationIntegrityError as error:
            return _accepted_specification_input_error(error)
        except (ValidationError, ValueError):
            return WorkflowError(
                code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
                message="The Story correction target no longer matches durable facts.",
            )


@dataclass(frozen=True)
class SprintPlanningInputService:
    """Lock exact durable Sprint input before any model execution."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Replay a Sprint attempt using stored guards and candidate identity."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def build(  # noqa: PLR0911
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        request: SprintPlanningRequest,
    ) -> JsonObject | WorkflowError:
        """Build an immutable planner envelope from exact current business facts."""
        try:
            with Session(self.engine) as session:
                lineage = _delivery_lineage(
                    session,
                    project_id=project_id,
                    decision=decision,
                )
                if lineage is None:
                    return _sprint_input_error(
                        code="SPRINT_SPECIFICATION_STALE",
                        message=(
                            "Sprint planning requires the exact current accepted "
                            "Specification lineage."
                        ),
                    )
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
                    accepted_specification=lineage.accepted_specification,
                    candidates=candidates,
                    dependencies=snapshot.story_dependencies,
                )
                selection = select_sprint_story_rows(
                    selection_rows,
                    max_story_points=capacity_points,
                    selected_story_ids=list(request.selected_story_ids),
                )
                planner_stories = tuple(
                    SprintPlannerStory.model_validate(row["planner_story"])
                    for row in selection.selected_rows
                )
                planner_input = SprintPlannerInput(
                    accepted_specification_version_id=(
                        lineage.accepted_specification.spec_version_id
                    ),
                    accepted_specification_hash=(
                        lineage.accepted_specification.spec_hash
                    ),
                    accepted_specification_json=(
                        lineage.accepted_specification.canonical_specification_json
                    ),
                    available_stories=planner_stories,
                    capacity_points=capacity_points,
                    capacity_source=capacity_source,
                    capacity_basis=capacity_basis,
                    user_context=request.guidance,
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
                        "guidance": request.guidance,
                        "candidate_set_fingerprint": (current_candidate_fingerprint),
                    }
                )
        except SprintSelectionError as error:
            return _sprint_input_error(code=error.code, message=str(error))
        except StoryValidationReadinessError as error:
            return _sprint_input_error(
                code="SPRINT_STORY_VALIDATION_STALE",
                message=str(error),
            )
        except AcceptedSpecificationIntegrityError as error:
            return _accepted_specification_input_error(error)
        except (
            TypeError,
            ValueError,
            ValidationError,
            WorkflowFactLoadError,
        ) as error:
            return _sprint_input_error(
                code="SPRINT_INPUT_INVALID",
                message=str(error) or type(error).__name__,
            )


@dataclass(frozen=True)
class ProductGoalLifecycleServices:
    """Host-owned services for the isolated Product Goal child graph."""

    interview_input: _ProductGoalInterviewInputPort


class _LifecycleServiceOptions(TypedDict, total=False):
    """Optional host-preparation services accepted by the application boundary."""

    vision_input: _VisionInputPort | None
    product_goal_services: ProductGoalLifecycleServices | None
    specification_structuring_input: _SpecificationStructuringInputPort | None
    specification_source_registration: _SpecificationSourceRegistrationPort | None
    specification_source_replay: _SpecificationSourceReplayPort | None
    delivery_review_selection: _DeliveryReviewSelectionPort | None
    delivery_action_input: _DeliveryActionInputPort | None
    planning_action_selection: _PlanningActionSelectionPort | None
    execution_action_selection: _ExecutionActionSelectionPort | None
    sprint_planning_input: _SprintPlanningInputPort | None
    specification_generation_config: JsonObject | None


class _ReadProjectionPort(Protocol):
    """Supported non-routing reads exposed to production transports."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

    def repository_status(self, *, project_id: int) -> JsonObject: ...

    def vision_status(self, *, project_id: int) -> JsonObject: ...

    def product_goal_status(self, *, project_id: int) -> JsonObject: ...

    def specification_status(self, *, project_id: int) -> JsonObject: ...

    def specification_review(self, *, project_id: int) -> JsonObject: ...

    def backlog_review(
        self, *, project_id: int, backlog_artifact_id: int
    ) -> JsonObject: ...

    def roadmap_review(
        self, *, project_id: int, roadmap_artifact_id: int
    ) -> JsonObject: ...

    def story_review(
        self, *, project_id: int, story_artifact_id: int
    ) -> JsonObject: ...

    def sprint_plan_review(
        self, *, project_id: int, sprint_plan_artifact_id: int
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


def _agentic_execution_settings(
    node_id: str,
    *,
    specification_generation_config: JsonObject | None = None,
) -> JsonObject:
    """Return effective non-secret settings included in attempt identity."""
    settings: JsonObject = dict(_EXECUTION_SETTINGS)
    if node_id == "specification.structure":
        generation_config = _JSON_OBJECT.validate_python(
            get_specification_structurer_generation_config()
            if specification_generation_config is None
            else specification_generation_config
        )
        settings = {**settings, "generation_config": generation_config}
    return settings


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


class StoryCorrectionRequest(FrozenModel):
    """Correct one host-selected accepted Story through full artifact review."""

    project_id: int
    story_id: int = Field(gt=0)
    guidance: SemanticText
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
    """Operator choice for one hidden machine-bound Story review instance."""


class SprintPlanReviewRequest(_DeliveryReviewRequest):
    """Operator choice for the graph-selected pending Sprint plan."""


class ExpectedPlanningReviewBinding(FrozenModel):
    """Machine-only graph binding captured beside one planning review read."""

    decision_fingerprint: SemanticText
    instance_key: str | None = None


class SprintPlanningRequest(FrozenModel):
    """Semantic Sprint planning input with no caller-owned execution evidence."""

    project_id: int
    guidance: str | None = None
    selected_story_ids: tuple[int, ...] = ()
    max_story_points: int | None = Field(default=None, gt=0)
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


class StoryValidationRequest(FrozenModel):
    """Explicit operator request to structurally validate one accepted Story."""

    project_id: int
    story_id: PositiveStoryId
    mode: Literal["structural"] = "structural"
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


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


class SpecificationStructuringRequest(FrozenModel):
    """Transport metadata for one host-prepared structuring attempt."""

    project_id: int
    expected_decision_fingerprint: str | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        repr=False,
    )
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class SpecificationReviewRequest(FrozenModel):
    """Operator choice for the graph-selected specification candidate."""

    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: SemanticText
    expected_candidate_fingerprint: str = Field(
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


def _vision_evidence_workflow_error_code(
    code: VisionEvidenceErrorCode,
) -> WorkflowErrorCode:
    """Map every closed evidence failure to its exact transport code."""
    match code:
        case VisionEvidenceErrorCode.PROJECT_NOT_FOUND:
            return WorkflowErrorCode.PROJECT_NOT_FOUND
        case VisionEvidenceErrorCode.REPOSITORY_BINDING_INVALID:
            return WorkflowErrorCode.REPOSITORY_BINDING_INVALID
        case VisionEvidenceErrorCode.REPOSITORY_PROVENANCE_STALE:
            return WorkflowErrorCode.REPOSITORY_PROVENANCE_STALE
        case VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION:
            return WorkflowErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    assert_never(code)


def _source_registration_workflow_error_code(
    code: SpecificationSourceRegistrationErrorCode,
) -> WorkflowErrorCode:
    """Map closed capture failures onto the retained workflow error surface."""
    if code is SpecificationSourceRegistrationErrorCode.PROJECT_NOT_FOUND:
        return WorkflowErrorCode.PROJECT_NOT_FOUND
    if code is SpecificationSourceRegistrationErrorCode.REPOSITORY_BINDING_REQUIRED:
        return WorkflowErrorCode.REPOSITORY_BINDING_INVALID
    if code is SpecificationSourceRegistrationErrorCode.REPOSITORY_PROVENANCE_STALE:
        return WorkflowErrorCode.REPOSITORY_PROVENANCE_STALE
    return WorkflowErrorCode.STALE_SPECIFICATION_INPUT


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
        """Retain the workflow and read boundaries."""
        self._workflow_domain = workflow_domain
        self._recipe_registry = recipe_registry
        self._read_projection = read_projection
        self._vision_input = lifecycle_services.get("vision_input")
        self._product_goal_services = lifecycle_services.get("product_goal_services")
        self._specification_structuring_input = lifecycle_services.get(
            "specification_structuring_input"
        )
        self._specification_source_registration = lifecycle_services.get(
            "specification_source_registration"
        )
        self._specification_source_replay = lifecycle_services.get(
            "specification_source_replay"
        )
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
        self._specification_generation_config = lifecycle_services.get(
            "specification_generation_config"
        )
        self._project_lifecycle: ProjectLifecycleService | None = None

    @property
    def reads(self) -> _ReadProjectionPort:
        """Return the injected durable non-routing projection."""
        if self._read_projection is None:
            message = "Read operations require an injected durable projection."
            raise RuntimeError(message)
        return self._read_projection

    def _engine(self) -> Engine:
        """Return the backing database engine from domain or read projection."""
        engine = getattr(self._workflow_domain, "_engine", None)
        if engine is not None:
            return cast("Engine", engine)
        read_engine = getattr(self._read_projection, "_engine", None)
        if read_engine is not None:
            return cast("Engine", read_engine)
        from models.db import get_engine  # noqa: PLC0415

        return get_engine()

    def position(self, *, project_id: int) -> WorkflowPosition:
        """Return the current durable workflow position."""
        return self._workflow_domain.position(project_id)

    def backlog_review(self, project_id: int) -> JsonObject:
        """Read the graph-selected unique Backlog review and hidden binding."""
        return self._unique_planning_review(
            project_id=project_id,
            node_id="backlog.review",
            fact_type="backlog",
            projection=self.reads.backlog_review,
            id_argument="backlog_artifact_id",
        )

    def roadmap_review(self, project_id: int) -> JsonObject:
        """Read the graph-selected unique Roadmap review and hidden binding."""
        return self._unique_planning_review(
            project_id=project_id,
            node_id="planning.roadmap.review",
            fact_type="roadmap",
            projection=self.reads.roadmap_review,
            id_argument="roadmap_artifact_id",
        )

    def sprint_plan_review(self, project_id: int) -> JsonObject:
        """Read the graph-selected unique Sprint-plan review and hidden binding."""
        return self._unique_planning_review(
            project_id=project_id,
            node_id="planning.sprint.review",
            fact_type="sprint_plan",
            projection=self.reads.sprint_plan_review,
            id_argument="sprint_plan_artifact_id",
        )

    def story_reviews(self, project_id: int) -> JsonObject:
        """Read every distinct pending Story review in stable instance order."""
        selection = self._delivery_review_selection
        if selection is None:
            return _planning_review_read_error("Story review selection is unavailable.")
        position = self.position(project_id=project_id)
        decisions = tuple(
            sorted(
                (
                    item
                    for item in position.decisions
                    if item.node_id == "planning.story.review"
                    and item.category in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
                ),
                key=lambda item: item.instance_key or "",
            )
        )
        keys = tuple(item.instance_key for item in decisions)
        if any(key is None or not key for key in keys) or len(set(keys)) != len(keys):
            return _planning_review_read_error("Story review selection is conflicting.")
        items: list[JsonValue] = []
        for decision in decisions:
            identity = selection.review_identity(
                project_id=project_id,
                decision=decision,
                fact_type="story",
            )
            if identity is None:
                return _planning_review_read_error("Story review selection is invalid.")
            artifact_id, _fingerprint = identity
            projection = self.reads.story_review(
                project_id=project_id,
                story_artifact_id=artifact_id,
            )
            if projection.get("ok") is not True:
                return projection
            items.append(
                {
                    "binding": {
                        "decision_fingerprint": decision.decision_fingerprint,
                        "instance_key": decision.instance_key,
                    },
                    "review": projection.get("data"),
                }
            )
        return _planning_review_read_success({"items": items})

    def _unique_planning_review(
        self,
        *,
        project_id: int,
        node_id: str,
        fact_type: DeliveryReviewFactType,
        projection: Callable[..., JsonObject],
        id_argument: str,
    ) -> JsonObject:
        """Resolve one unique graph decision into its typed artifact projection."""
        selection = self._delivery_review_selection
        if selection is None:
            return _planning_review_read_error(
                "Planning review selection is unavailable."
            )
        position = self.position(project_id=project_id)
        candidates = _available_decisions(position, node_id)
        if not candidates:
            return _planning_review_read_error(
                "No planning review is currently available.",
                code="PLANNING_REVIEW_NOT_AVAILABLE",
            )
        if len(candidates) != 1:
            return _planning_review_read_error(
                "Planning review selection is not unique."
            )
        decision = candidates[0]
        identity = selection.review_identity(
            project_id=project_id,
            decision=decision,
            fact_type=fact_type,
        )
        if identity is None:
            return _planning_review_read_error("Planning review selection is invalid.")
        artifact_id, _fingerprint = identity
        result = projection(project_id=project_id, **{id_argument: artifact_id})
        if result.get("ok") is not True:
            return result
        return _planning_review_read_success(
            {
                "binding": {
                    "decision_fingerprint": decision.decision_fingerprint,
                    "instance_key": decision.instance_key,
                },
                "review": result.get("data"),
            }
        )

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
                execution_settings=_agentic_execution_settings(
                    request.node_id,
                    specification_generation_config=(
                        self._specification_generation_config
                    ),
                ),
                lease_seconds=_LEASE_SECONDS,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
            specification_source_check=(
                self._specification_structuring_input.revalidate_sources
                if request.node_id == "specification.structure"
                and self._specification_structuring_input is not None
                else None
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

    def correct_story(self, request: StoryCorrectionRequest) -> TransitionResult:
        """Correct one accepted Story item through the existing artifact workflow."""
        input_service = self._delivery_action_input
        node_id = "planning.story.generate"
        if input_service is None:
            return _transition_not_available(None, node_id)
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id=node_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                semantic_input={
                    "correction": {
                        "story_id": request.story_id,
                        "guidance": request.guidance,
                    }
                },
                reuse_stored_instance_key=True,
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decisions = tuple(
            decision
            for decision in position.decisions
            if decision.node_id == node_id
            and decision.category is NodeCategory.AVAILABLE
        )
        prepared = input_service.build_story_correction(
            project_id=request.project_id,
            decisions=decisions,
            request=request,
        )
        if isinstance(prepared, WorkflowError):
            return TransitionResult(ok=False, position=position, error=prepared)
        if prepared is None:
            return _transition_not_available(position, node_id)
        decision, input_payload = prepared
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

    def decide_backlog(
        self,
        request: BacklogReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
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
        if not _planning_review_binding_matches(decision, expected):
            return _stale_review_candidate(position, "Backlog")
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

    def decide_roadmap(
        self,
        request: RoadmapReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
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
        if not _planning_review_binding_matches(decision, expected):
            return _stale_review_candidate(position, "Roadmap")
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

    def decide_story(
        self,
        request: StoryReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
    ) -> TransitionResult:
        """Resolve and review one exact current Story artifact instance."""
        selection = self._delivery_review_selection
        if selection is None:
            return _transition_not_available(None, "planning.story.review")
        replay = self._replay_delivery_review(
            request_kind="decide_story",
            request=request,
            instance_key=expected.instance_key,
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(
            position,
            "planning.story.review",
            instance_key=expected.instance_key,
        )
        if not _planning_review_binding_matches(decision, expected):
            return _stale_review_candidate(position, "Story")
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
        prefix = "backlog_item:"
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
                backlog_item_id=instance_key.removeprefix(prefix),
                story_artifact_id=artifact_id,
                artifact_fingerprint=fingerprint,
                decision=request.decision,
                rationale=request.rationale,
            )
        )

    def decide_sprint_plan(
        self,
        request: SprintPlanReviewRequest,
        *,
        expected: ExpectedPlanningReviewBinding,
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
        if not _planning_review_binding_matches(decision, expected):
            return _stale_review_candidate(position, "Sprint plan")
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

    def _execute_story_validation_in_session(
        self,
        session: Session,
        request: StoryValidationRequest,
    ) -> JsonObject:
        """Execute structural story validation against state in session."""
        project_result = self.reads.project_show(project_id=request.project_id)
        if not project_result.get("ok"):
            return project_result
        story_result = self.reads.story_show(story_id=request.story_id)
        if not story_result.get("ok"):
            return story_result
        story_data = story_result.get("data")
        if (
            not isinstance(story_data, dict)
            or story_data.get("project_id") != request.project_id
        ):
            return {
                "ok": False,
                "data": {
                    "story_id": request.story_id,
                    "project_id": request.project_id,
                },
                "errors": [
                    {
                        "code": "STORY_NOT_FOUND",
                        "message": (
                            f"Story {request.story_id} was not found in project"
                            f" {request.project_id}."
                        ),
                        "details": {
                            "story_id": request.story_id,
                            "project_id": request.project_id,
                        },
                    }
                ],
            }
        eval_result = validate_story_with_specification_in_session(
            session,
            ValidateStoryInput(story_id=request.story_id, mode=request.mode),
        )
        if not eval_result.get("success", False):
            error_code = cast(
                "str", eval_result.get("error_code") or "STORY_VALIDATION_FAILED"
            )
            message = cast(
                "str", eval_result.get("message") or "Story validation failed."
            )
            return {
                "ok": False,
                "data": eval_result,
                "errors": [
                    {
                        "code": error_code,
                        "message": message,
                        "details": eval_result,
                    }
                ],
            }
        return {"ok": True, "data": eval_result, "errors": []}

    def validate_story(
        self,
        request: StoryValidationRequest,
    ) -> JsonObject:
        """Structurally validate one accepted Story provider-free."""
        request_payload: JsonObject = {
            "project_id": request.project_id,
            "story_id": request.story_id,
            "mode": request.mode,
            "actor": request.actor,
            "correlation_id": request.correlation_id,
        }
        request_fingerprint = canonical_hash(request_payload)
        engine = self._engine()

        with Session(engine, expire_on_commit=False) as session:
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")

            existing = session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.request_kind) == "validate_story",
                    col(WorkflowTransitionReceipt.idempotency_key)
                    == request.idempotency_key,
                )
            ).one_or_none()

            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    return {
                        "ok": False,
                        "status": "error",
                        "errors": [
                            {
                                "code": "IDEMPOTENCY_CONFLICT",
                                "message": (
                                    "The idempotency key was already used for"
                                    " different input."
                                ),
                            }
                        ],
                        "warnings": [],
                    }
                if existing.result_json is not None:
                    return _JSON_OBJECT.validate_json(existing.result_json)

            response = self._execute_story_validation_in_session(session, request)

            started_at = datetime.now(tz=UTC)
            receipt = WorkflowTransitionReceipt(
                request_kind="validate_story",
                idempotency_key=request.idempotency_key,
                request_fingerprint=request_fingerprint,
                request_json=canonical_json(request_payload),
                result_json=canonical_json(response),
                started_at=started_at,
                completed_at=started_at,
            )
            session.add(receipt)
            session.commit()
            return response

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
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            stale = tuple(
                item
                for item in position.decisions
                if item.node_id == "planning.sprint.start"
                and item.category is NodeCategory.INVALID
                and item.reason_code == "STALE_SPECIFICATION"
            )
            if len(stale) == 1:
                message = "Sprint start requires the current accepted Specification."
                blocker = Blocker(code="STALE_SPECIFICATION", message=message)
                return TransitionResult(
                    ok=False,
                    position=position,
                    error=WorkflowError(
                        code=WorkflowErrorCode.STALE_SPECIFICATION,
                        message=message,
                        blockers=(blocker,),
                    ),
                )
            blocked = tuple(
                item
                for item in position.decisions
                if item.node_id == "planning.sprint.start"
                and item.category is NodeCategory.BLOCKED
                and len(item.blockers) == 1
                and item.blockers[0].code == "ACTIVE_SPRINT_EXISTS"
            )
            if len(blocked) == 1:
                blocker = blocked[0].blockers[0]
                return TransitionResult(
                    ok=False,
                    position=position,
                    error=WorkflowError(
                        code=WorkflowErrorCode.ACTIVE_SPRINT_EXISTS,
                        message=blocker.message,
                        blockers=(blocker,),
                    ),
                )
            return _transition_not_available(position, "planning.sprint.start")
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

    def _run_delivery_action(  # noqa: PLR0911
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
        if (
            node_id == "planning.story.generate"
            and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
        ):
            return _transition_not_available(position, node_id)
        input_payload = input_service.build(
            project_id=request.project_id,
            decision=decision,
            node_id=node_id,
        )
        if isinstance(input_payload, WorkflowError):
            return TransitionResult(ok=False, position=position, error=input_payload)
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
        try:
            input_payload = input_service.build_bootstrap(request.project_id, decision)
        except VisionEvidenceCollectionError as error:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=_vision_evidence_workflow_error_code(error.code),
                    message=str(error),
                ),
            )
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
                instance_key=decision.instance_key,
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
                instance_key=decision.instance_key,
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
                    "candidate_fingerprint": request.expected_candidate_fingerprint,
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

    def structure_specification(
        self,
        request: SpecificationStructuringRequest,
    ) -> TransitionResult:
        """Run one exact host-prepared structuring attempt for the current source."""
        input_service = self._specification_structuring_input
        if input_service is None:
            message = "Specification structuring requires an injected input builder."
            raise RuntimeError(message)
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id="specification.structure",
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "specification.structure")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "specification.structure")
        if (
            request.expected_decision_fingerprint is not None
            and request.expected_decision_fingerprint != decision.decision_fingerprint
        ):
            return _stale_specification_structuring_action(position)
        model_id = get_model_id(AGENTIC_MODEL_ROLES["specification.structure"])
        try:
            input_payload = input_service.build(
                project_id=request.project_id,
                decision=decision,
            )
        except (ValueError, VisionEvidenceCollectionError) as error:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=(
                        _vision_evidence_workflow_error_code(error.code)
                        if isinstance(error, VisionEvidenceCollectionError)
                        else WorkflowErrorCode.STALE_SPECIFICATION_INPUT
                    ),
                    message=str(error),
                ),
            )
        return self.run_agentic_action(
            AgenticActionRequest(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id="specification.structure",
                instance_key=decision.instance_key,
                input_payload=input_payload,
                model_id=model_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )

    def register_specification_source(
        self,
        request: SpecificationSourceRegistrationRequest,
    ) -> TransitionResult:
        """Capture semantic file selection, then submit one host-only command."""
        registration = self._specification_source_registration
        if registration is None:
            message = "Specification source registration requires an injected service."
            raise RuntimeError(message)
        replay_service = self._specification_source_replay
        replay = (
            None
            if replay_service is None
            else replay_service.replay(
                TransitionReplayQuery(
                    request_kind="register_specification_source",
                    project_id=request.project_id,
                    idempotency_key=request.idempotency_key,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                    operator_input={
                        "capture_request_fingerprint": request.semantic_fingerprint()
                    },
                )
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(
            position,
            "specification.source.register",
        )
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "specification.source.register")
        if (
            request.expected_decision_fingerprint is not None
            and request.expected_decision_fingerprint != decision.decision_fingerprint
        ):
            return _stale_specification_source_registration_action(position)
        try:
            prepared = registration.prepare(request)
        except SpecificationSourceRegistrationError as error:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=_source_registration_workflow_error_code(error.code),
                    message=str(error),
                ),
            )
        if prepared.project_id != request.project_id:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                    message="Prepared Specification source belongs to another Project.",
                ),
            )
        return self.transition(
            RegisterSpecificationSource(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                accepted_vision_artifact_id=(prepared.accepted_vision_artifact_id),
                accepted_product_goal_artifact_id=(
                    prepared.accepted_product_goal_artifact_id
                ),
                repository_binding_id=prepared.repository_binding_id,
                repository_binding_fingerprint=(
                    prepared.repository_binding_fingerprint
                ),
                capture_request_fingerprint=prepared.request_fingerprint,
                source_fingerprint=prepared.source_fingerprint,
                bundle=prepared.bundle,
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
                    "candidate_fingerprint": request.expected_candidate_fingerprint,
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
                candidate_fingerprint=reference.fingerprint,
                decision=request.decision,
                rationale=request.rationale,
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
    specification_target = _integer_fact_reference(decision, "specification")
    goal_target = _integer_fact_reference(decision, "product_goal")
    if specification_target is None or goal_target is None:
        return None
    spec_version_id, specification_reference = specification_target
    goal_id, goal_reference = goal_target
    accepted = require_current_accepted_specification(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
        spec_hash=specification_reference.fingerprint,
    )
    spec = session.get(SpecRegistry, accepted.spec_version_id)
    goal = session.get(ProductGoalArtifact, goal_id)
    vision = (
        None if goal is None else session.get(VisionArtifact, goal.vision_artifact_id)
    )
    if (
        goal is None
        or spec is None
        or vision is None
        or accepted.project_id != project_id
        or accepted.spec_version_id != spec_version_id
        or accepted.spec_hash != specification_reference.fingerprint
        or goal.project_id != project_id
        or goal.product_goal_artifact_id != goal_id
        or goal.content_fingerprint != goal_reference.fingerprint
        or vision.project_id != project_id
        or vision.vision_artifact_id != goal.vision_artifact_id
        or vision.content_fingerprint != goal.vision_fingerprint
        or spec.project_id != project_id
        or spec.status != "approved"
        or spec.spec_version_id != accepted.spec_version_id
        or spec.spec_hash != accepted.spec_hash
        or spec.source_vision_artifact_id != vision.vision_artifact_id
        or spec.source_vision_fingerprint != vision.content_fingerprint
        or spec.source_product_goal_artifact_id != goal_id
        or spec.source_product_goal_fingerprint != goal.content_fingerprint
    ):
        return None
    return _DeliveryLineage(
        accepted_specification=accepted,
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
    supersedes_id: int | None = None
    if prior_reference is not None:
        try:
            prior_id = int(prior_reference.fact_id)
        except ValueError:
            return None
        supersedes_id = prior_id
        prior = session.get(BacklogArtifact, prior_id)
        if (
            prior is None
            or prior.project_id != lineage.goal.project_id
            or prior.content_fingerprint != prior_reference.fingerprint
            or prior.spec_version_id != lineage.accepted_specification.spec_version_id
            or prior.spec_hash != lineage.accepted_specification.spec_hash
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
    accepted = lineage.accepted_specification
    payload = BacklogBuilderInput(
        accepted_specification_version_id=accepted.spec_version_id,
        accepted_specification_hash=accepted.spec_hash,
        accepted_specification_json=accepted.canonical_specification_json,
        product_vision_statement=lineage.vision.statement,
        product_goal_statement=lineage.goal.statement,
        prior_backlog_state=prior_state,
        user_input=user_input,
    )
    return _JSON_OBJECT.validate_python(
        {
            "builder_input": payload.model_dump(mode="json"),
            "product_goal_artifact_id": lineage.goal.product_goal_artifact_id,
            "product_goal_fingerprint": lineage.goal.content_fingerprint,
            "supersedes_backlog_artifact_id": supersedes_id,
        }
    )


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
        or backlog.spec_version_id != lineage.accepted_specification.spec_version_id
        or backlog.spec_hash != lineage.accepted_specification.spec_hash
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
    supersedes_id: int | None = None
    if prior_reference is not None:
        try:
            prior_id = int(prior_reference.fact_id)
        except ValueError:
            return None
        supersedes_id = prior_id
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
    accepted = lineage.accepted_specification
    state: dict[str, Any] = {
        "accepted_specification_version_id": accepted.spec_version_id,
        "accepted_specification_hash": accepted.spec_hash,
        "accepted_specification_json": accepted.canonical_specification_json,
        "backlog_items": [
            item.model_dump(mode="json") for item in backlog_output.backlog_items
        ],
        "product_vision": lineage.vision.statement,
        "prior_roadmap_state": (
            "NO_HISTORY"
            if prior_output is None
            else prior_output.model_dump_json(exclude_none=True)
        ),
    }
    context = build_roadmap_input_context(state, user_input=user_input)
    payload = RoadmapBuilderInput.model_validate(context)
    return _JSON_OBJECT.validate_python(
        {
            "builder_input": payload.model_dump(mode="json"),
            "backlog_artifact_id": backlog.backlog_artifact_id,
            "backlog_artifact_fingerprint": backlog.content_fingerprint,
            "supersedes_roadmap_artifact_id": supersedes_id,
        }
    )


def _story_input(
    session: Session,
    decision: NodeDecision,
    lineage: _DeliveryLineage,
) -> JsonObject | None:
    backlog_source = _required_backlog(session, decision, lineage)
    item_reference = _single_fact_reference(decision, "backlog_item")
    if backlog_source is None or item_reference is None:
        message = "Story input requires exact Backlog and Backlog-item references."
        raise ValueError(message)
    backlog, backlog_output = backlog_source
    roadmap_source = _required_roadmap(session, decision, backlog)
    if roadmap_source is None:
        message = "Story input requires an exact accepted Roadmap reference."
        raise ValueError(message)
    roadmap, roadmap_output = roadmap_source
    backlog_item_id = item_reference.fact_id
    if decision.instance_key != f"backlog_item:{backlog_item_id}":
        message = "Story item selection does not match durable Backlog facts."
        raise ValueError(message)
    backlog_item = next(
        (
            item
            for item in backlog_output.backlog_items
            if item.backlog_item_id == backlog_item_id
        ),
        None,
    )
    if (
        backlog_item is None
        or canonical_hash(backlog_item.model_dump(mode="json"))
        != item_reference.fingerprint
    ):
        message = "Story Backlog item is absent or changed in the durable parent."
        raise ValueError(message)
    accepted = lineage.accepted_specification
    state: dict[str, Any] = {
        "accepted_specification_version_id": accepted.spec_version_id,
        "accepted_specification_hash": accepted.spec_hash,
        "accepted_specification_json": accepted.canonical_specification_json,
        "parent_backlog_item_id": backlog_item.backlog_item_id,
        "parent_backlog_spec_item_ids": backlog_item.spec_item_ids,
        "roadmap_context": roadmap_output.model_dump_json(exclude_none=True),
    }
    valid_prior, prior_reference = _optional_fact_reference(decision, "story")
    if not valid_prior:
        message = "Story input has ambiguous prior Story references."
        raise ValueError(message)
    supersedes_id: int | None = None
    if prior_reference is not None:
        try:
            prior_id = int(prior_reference.fact_id)
        except ValueError as error:
            message = "Prior Story reference is not a durable integer identity."
            raise ValueError(message) from error
        supersedes_id = prior_id
        prior = session.get(StoryArtifact, prior_id)
        if (
            prior is None
            or prior.project_id != backlog.project_id
            or prior.source_backlog_artifact_id != backlog.backlog_artifact_id
            or prior.source_backlog_artifact_fingerprint != backlog.content_fingerprint
            or prior.backlog_item_id != backlog_item_id
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
        CanonicalStoryOutput.model_validate(prior_content)
        review = session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == prior.project_id,
                col(StoryArtifactDecision.story_artifact_id) == prior_id,
            )
        ).one_or_none()
        state["user_input"] = (
            "Previous reviewed Story artifact:\n"
            f"{prior.canonical_content_json}"
            + (
                ""
                if review is None
                else (
                    f"\nReview outcome: {review.decision}"
                    f"\nReview rationale: {review.rationale}"
                )
            )
        )
    context = build_story_input_context(state)
    payload = UserStoryWriterInput.model_validate(context)
    return _JSON_OBJECT.validate_python(
        {
            "writer_input": payload.model_dump(mode="json"),
            "source_backlog_artifact_id": backlog.backlog_artifact_id,
            "source_backlog_artifact_fingerprint": backlog.content_fingerprint,
            "roadmap_artifact_id": roadmap.roadmap_artifact_id,
            "roadmap_artifact_fingerprint": roadmap.content_fingerprint,
            "supersedes_story_artifact_id": supersedes_id,
        }
    )


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
    accepted_specification: AcceptedSpecification,
    candidates: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
) -> list[dict[str, Any]]:
    """Convert exact candidate rows and evidence to deterministic selector input."""
    for candidate in candidates:
        if (
            candidate.accepted_spec_version_id != accepted_specification.spec_version_id
            or candidate.accepted_spec_hash != accepted_specification.spec_hash
        ):
            message = (
                f"Story {candidate.story_id} does not match the current accepted "
                "Specification."
            )
            raise ValueError(message)
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
    for story in stories:
        require_story_ready_for_sprint(session, story=story)
    artifact_ids = {item.source_story_artifact_id for item in candidates}
    artifacts = session.exec(
        select(StoryArtifact).where(
            col(StoryArtifact.project_id) == project_id,
            col(StoryArtifact.story_artifact_id).in_(artifact_ids),
        )
    ).all()
    artifacts_by_id = {
        artifact.story_artifact_id: artifact
        for artifact in artifacts
        if artifact.story_artifact_id is not None
    }
    decisions = session.exec(
        select(StoryArtifactDecision).where(
            col(StoryArtifactDecision.project_id) == project_id,
            col(StoryArtifactDecision.story_artifact_id).in_(artifact_ids),
        )
    ).all()
    decisions_by_artifact_id = {
        decision.story_artifact_id: decision for decision in decisions
    }
    outputs_by_artifact_id = {
        artifact_id: _canonical_story_output(artifact)
        for artifact_id, artifact in artifacts_by_id.items()
    }
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
            or not candidate.content_accepted
            or candidate.readiness_blockers
            or story.source_story_artifact_id != candidate.source_story_artifact_id
            or story.source_story_artifact_fingerprint
            != candidate.source_story_artifact_fingerprint
            or story.source_story_item_id != candidate.source_story_item_id
            or story.source_story_item_fingerprint
            != candidate.source_story_item_fingerprint
            or story.accepted_spec_version_id != candidate.accepted_spec_version_id
            or story.accepted_spec_hash != candidate.accepted_spec_hash
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
        prerequisite_ids = sorted(set(prerequisites[candidate.story_id]))
        blocked_by_ids = [
            story_id for story_id in prerequisite_ids if story_id in candidate_ids
        ]
        artifact = artifacts_by_id.get(candidate.source_story_artifact_id)
        output = outputs_by_artifact_id.get(candidate.source_story_artifact_id)
        if artifact is None or output is None:
            message = f"Story {story.story_id} immutable Story artifact is unavailable."
            raise ValueError(message)
        canonical_item = _canonical_story_item(
            story,
            candidate,
            artifact,
            decisions_by_artifact_id.get(candidate.source_story_artifact_id),
            output,
        )
        planner_story = _sprint_planner_story(story, candidate, canonical_item)
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


def _sprint_planner_story(
    story: UserStory,
    candidate: StoryFact,
    canonical_item: CanonicalStoryItem,
) -> SprintPlannerStory:
    """Project one exact accepted Story item without legacy compatibility."""
    if (
        story.title != canonical_item.story_title
        or story.story_description != canonical_item.statement
        or story.persona != canonical_item.persona
        or story.acceptance_criteria_json
        != canonical_json(list(canonical_item.acceptance_criteria))
        or story.spec_item_ids_json
        != canonical_json(list(canonical_item.spec_item_ids))
        or canonical_item.spec_item_ids != candidate.spec_item_ids
    ):
        message = f"Story {story.story_id} no longer matches its immutable Story item."
        raise ValueError(message)
    return SprintPlannerStory(
        story_id=candidate.story_id,
        story_item_id=canonical_item.story_item_id,
        story_title=canonical_item.story_title,
        statement=canonical_item.statement,
        persona=canonical_item.persona,
        acceptance_criteria=canonical_item.acceptance_criteria,
        spec_item_ids=canonical_item.spec_item_ids,
        story_points=story.story_points,
        rank=story.rank,
    )


def _canonical_story_output(artifact: StoryArtifact) -> CanonicalStoryOutput:
    """Deserialize one persisted Story envelope once for the Sprint input root."""
    content = _canonical_artifact(
        artifact.canonical_content_json,
        artifact.content_fingerprint,
    )
    if content is None:
        message = "Immutable Story artifact is not canonical."
        raise ValueError(message)
    output = CanonicalStoryOutput.model_validate(content)
    item_ids = tuple(item.item.story_item_id for item in output.story_items)
    try:
        stored_item_ids = TypeAdapter(tuple[str, ...]).validate_json(
            artifact.story_item_ids_json
        )
    except ValidationError as error:
        message = "Immutable Story item IDs are invalid."
        raise ValueError(message) from error
    if (
        canonical_json(list(stored_item_ids)) != artifact.story_item_ids_json
        or stored_item_ids != item_ids
    ):
        message = "Immutable Story item IDs changed."
        raise ValueError(message)
    return output


def _canonical_story_item(
    story: UserStory,
    candidate: StoryFact,
    artifact: StoryArtifact,
    decision: StoryArtifactDecision | None,
    output: CanonicalStoryOutput,
) -> CanonicalStoryItem:
    """Load and prove the exact immutable Story item backing one planner row."""
    if (
        artifact.project_id != story.project_id
        or artifact.story_artifact_id != candidate.source_story_artifact_id
        or artifact.content_fingerprint != candidate.source_story_artifact_fingerprint
    ):
        message = f"Story {story.story_id} immutable Story artifact is unavailable."
        raise ValueError(message)
    if (
        decision is None
        or decision.project_id != story.project_id
        or decision.story_artifact_id != artifact.story_artifact_id
        or decision.artifact_fingerprint != artifact.content_fingerprint
        or decision.decision != "accepted"
    ):
        message = f"Story {story.story_id} has no exact accepted Story decision."
        raise ValueError(message)
    matches = tuple(
        item
        for item in output.story_items
        if item.item.story_item_id == candidate.source_story_item_id
        and item.item_fingerprint == candidate.source_story_item_fingerprint
    )
    if len(matches) != 1:
        message = f"Story {story.story_id} immutable Story item is unavailable."
        raise ValueError(message)
    return matches[0].item


def _sprint_input_error(*, code: str, message: str) -> WorkflowError:
    """Return one structured deterministic host-preparation failure."""
    return WorkflowError(
        code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        message=message,
        blockers=(Blocker(code=code, message=message),),
    )


def _accepted_specification_input_error(
    error: AcceptedSpecificationIntegrityError,
) -> WorkflowError:
    """Preserve accepted-Specification integrity detail at application boundaries."""
    message = str(error)
    return WorkflowError(
        code=(
            WorkflowErrorCode.STALE_SPECIFICATION
            if error.code == "STALE_SPECIFICATION"
            else WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
        ),
        message=message,
        blockers=(Blocker(code=error.code, message=message),),
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


def _available_decisions(
    position: WorkflowPosition,
    node_id: str,
    *,
    instance_key: str | None = None,
) -> tuple[NodeDecision, ...]:
    """Return current command candidates without collapsing absence and conflict."""
    return tuple(
        item
        for item in position.decisions
        if item.node_id == node_id
        and item.category in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
        and (instance_key is None or item.instance_key == instance_key)
    )


def _unique_available_decision(
    position: WorkflowPosition,
    node_id: str,
    *,
    instance_key: str | None = None,
) -> NodeDecision | None:
    """Return one current command decision without accepting an ambiguous position."""
    candidates = _available_decisions(
        position,
        node_id,
        instance_key=instance_key,
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


def _planning_review_binding_matches(
    decision: NodeDecision | None,
    expected: ExpectedPlanningReviewBinding,
) -> bool:
    """Require the exact decision fingerprint and repeated-instance identity."""
    return bool(
        decision is not None
        and decision.decision_fingerprint == expected.decision_fingerprint
        and decision.instance_key == expected.instance_key
    )


def _planning_review_read_success(data: JsonObject) -> JsonObject:
    """Return the exact four-field success envelope for a selected review."""
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _planning_review_read_error(
    message: str,
    *,
    code: str = WorkflowErrorCode.WORKFLOW_FACT_CONFLICT.value,
) -> JsonObject:
    """Fail closed when a graph-selected planning review is not exact."""
    details: JsonObject = {}
    return {
        "ok": False,
        "data": details,
        "warnings": [],
        "errors": [
            {
                "code": code,
                "message": message,
                "details": details,
            }
        ],
    }


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


def _stale_specification_structuring_action(
    position: WorkflowPosition,
) -> TransitionResult:
    """Reject a browser structuring action whose exact position changed."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message=(
                "The Specification structuring action changed after it was shown. "
                "Reload and choose from the current source state."
            ),
        ),
    )


def _stale_specification_source_registration_action(
    position: WorkflowPosition,
) -> TransitionResult:
    """Reject a browser source choice whose exact position changed."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message=(
                "The Specification source choice changed after it was shown. "
                "Reload and choose from the current source state."
            ),
        ),
    )


def _sprint_replay_input(request: SprintPlanningRequest) -> JsonObject:
    """Return only caller semantics replaced during durable replay comparison."""
    return {
        "requested_max_story_points": request.max_story_points,
        "requested_story_ids": list(request.selected_story_ids),
        "team_name": request.team_name,
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
    from adapters.adk.agents.specification_author import (  # noqa: PLC0415
        root_agent as specification_structurer_agent,
    )
    from adapters.adk.agents.sprint import root_agent as sprint_agent  # noqa: PLC0415
    from adapters.adk.agents.story import (  # noqa: PLC0415
        create_user_story_patch_agent,
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
    from services.read_projections import (  # noqa: PLC0415
        DurableReadProjectionService,
    )
    from services.specification_authoring_input import (  # noqa: PLC0415
        SpecificationStructuringInputService,
    )
    from services.specification_source_registration import (  # noqa: PLC0415
        SpecificationSourceRegistrationService,
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
    configured_generation = specification_structurer_agent.generate_content_config
    if configured_generation is None:
        message = "Specification structurer generation configuration is required."
        raise RuntimeError(message)
    specification_generation_config = cast(
        "JsonObject",
        configured_generation.model_dump(
            mode="json",
            exclude_none=True,
        ),
    )
    graph = project_graph()
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            vision_interview=vision_interview_agent,
            vision_repair=vision_repair_agent,
            product_goal=product_goal_interview_agent,
            specification_structurer=specification_structurer_agent,
            backlog_generation=backlog_agent,
            roadmap_generation=roadmap_agent,
            story_generation=create_user_story_writer_agent(),
            story_correction=create_user_story_patch_agent(),
            sprint_planning=sprint_agent,
        ),
        execution_settings=_EXECUTION_SETTINGS,
    )
    engine = get_engine()
    ensure_business_db_ready(engine)
    specification_structuring_input = SpecificationStructuringInputService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )
    specification_source_registration = SpecificationSourceRegistrationService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=graph,
        clock=SystemClock(),
        adk_recipe_registry=registry,
        specification_source_check=(specification_structuring_input.revalidate_sources),
        specification_registration_check=(
            specification_source_registration.verify_prepared
        ),
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
        ),
        specification_structuring_input=specification_structuring_input,
        specification_generation_config=specification_generation_config,
        specification_source_registration=specification_source_registration,
        specification_source_replay=DurableTransitionReplayService(engine=engine),
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
    "BacklogReviewRequest",
    "CloseStoryRequest",
    "CompleteTaskRequest",
    "CreateProjectCommand",
    "DeliveryActionInputService",
    "DeliveryActionRequest",
    "DeliveryReviewSelectionService",
    "ExecutionActionSelectionService",
    "ExpectedPlanningReviewBinding",
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
    "SpecificationReviewRequest",
    "SpecificationSourceRegistrationRequest",
    "SpecificationStructuringRequest",
    "SprintCloseRequest",
    "SprintPlanReviewRequest",
    "SprintPlanningInputService",
    "SprintPlanningRequest",
    "SprintReviewRequest",
    "SprintStartRequest",
    "StoryCorrectionRequest",
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
