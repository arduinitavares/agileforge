"""Production application boundary for the durable workflow graph."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, Unpack, cast

from pydantic import Field
from sqlmodel import Session

from adapters.adk.model_roles import AGENTIC_MODEL_ROLES
from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.authority_compilation_input import AuthorityCompilationInputService
from services.authority_review_projection import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.contracts.product_goal import ProductGoalInterviewInput
from services.contracts.vision import VisionInterviewInput
from services.node_attempt_replay import NodeAttemptReplayQuery, TransitionReplayQuery
from services.product_goal_interview_input import ProductGoalInterviewInputService
from services.project_lifecycle import (
    CreateProjectCommand,
    ProjectLifecycleService,
    RepositoryAttachmentCommand,
    RepositoryRefreshCommand,
)
from services.vision_interview_input import VisionInterviewInputService
from utils.model_config import get_model_id
from workflow.contracts import (
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
from workflow.requests import (
    AbandonProductGoal,
    BeginVisionRevision,
    DecideAuthority,
    DecideProductGoalReview,
    DecideSpecification,
    DecideVisionReview,
    FulfillProductGoal,
    RecordDiscoveryArtifact,
    RecordSpecificationCandidate,
)

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent
    from sqlalchemy.engine import Engine

    from adapters.adk.recipes import AdkRecipeRegistry
    from workflow.requests import TransitionRequest


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


class _VisionInterviewInputPort(Protocol):
    """Host preparation for a Project Vision interview turn."""

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


@dataclass(frozen=True)
class AuthorityReviewSelectionService:
    """Build one facts-only review identity from durable authority state."""

    engine: Engine

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
class ProductGoalLifecycleServices:
    """Host-owned services for the isolated Product Goal child graph."""

    interview_input: _ProductGoalInterviewInputPort
    discovery_selection: _ProductDiscoverySelectionPort


class _LifecycleServiceOptions(TypedDict, total=False):
    """Optional host-preparation services accepted by the application boundary."""

    vision_interview_input: _VisionInterviewInputPort | None
    product_goal_services: ProductGoalLifecycleServices | None
    authority_compilation_input: _AuthorityCompilationInputPort | None
    authority_review_selection: _AuthorityReviewSelectionPort | None
    authority_repair_input: _AuthorityRepairInputPort | None


class _ReadProjectionPort(Protocol):
    """Supported non-routing reads exposed to production transports."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

    def repository_status(self, *, project_id: int) -> JsonObject: ...

    def project_initial_spec(self, *, project_id: int) -> JsonObject: ...

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


class AgenticActionRequest(FrozenModel):
    """Exact transport-supplied request for one agentic graph decision."""

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


class VisionInterviewRequest(FrozenModel):
    """Transport request for one host-prepared Vision interview attempt."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    user_text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionResponseRequest(FrozenModel):
    """Semantic caller input for one Project Vision interview turn."""

    project_id: int
    text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionReviewRequest(FrozenModel):
    """Transport request for one explicit Vision review decision."""

    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionRevisionRequest(FrozenModel):
    """Transport request to open an eligible Vision replacement interview."""

    project_id: int
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalInterviewRequest(FrozenModel):
    """Transport request for one host-prepared Product Goal interview attempt."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    user_text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalResponseRequest(FrozenModel):
    """Semantic caller input for one Product Goal interview turn."""

    project_id: int
    text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalReviewRequest(FrozenModel):
    """Operator choice for the graph-selected pending Product Goal."""

    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalOutcomeRequest(FrozenModel):
    """Operator outcome choice for the graph-selected active Product Goal."""

    project_id: int
    outcome: Literal["fulfilled", "abandoned"]
    rationale: str = Field(min_length=1)
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
    rationale: str
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
    """Semantic authority decision without caller-owned review identity."""

    project_id: int
    decision: Literal["accepted", "rejected"]
    rationale: str = Field(min_length=1)
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
        self._vision_interview_input = lifecycle_services.get("vision_interview_input")
        self._product_goal_services = lifecycle_services.get("product_goal_services")
        self._authority_compilation_input = lifecycle_services.get(
            "authority_compilation_input"
        )
        self._authority_review_selection = lifecycle_services.get(
            "authority_review_selection"
        )
        self._authority_repair_input = lifecycle_services.get("authority_repair_input")
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
        available, fingerprint = self._active_repository_binding(
            project_id=request.project_id,
            require_active=False,
        )
        if not available:
            return _transition_not_available(None, "repository.attach")
        return self._project_lifecycle_service().attach_repository(
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
        available, fingerprint = self._active_repository_binding(
            project_id=request.project_id,
            require_active=True,
        )
        if not available or fingerprint is None:
            return _transition_not_available(None, "repository.refresh")
        return self._project_lifecycle_service().refresh_repository(
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

    def respond_to_vision(
        self,
        request: VisionResponseRequest,
    ) -> TransitionResult:
        """Resolve the current Vision interview guards from one position read."""
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

    def _run_vision_interview_at_position(
        self,
        request: VisionInterviewRequest,
        position: WorkflowPosition,
        *,
        check_replay: bool = True,
    ) -> TransitionResult:
        node_id = "vision.interview"
        input_service = self._vision_interview_input
        if input_service is None:
            message = "Vision interview requires an injected input builder."
            raise RuntimeError(message)
        replay = self._replay_vision_interview(request) if check_replay else None
        if replay is not None:
            return replay
        decision = _guarded_vision_interview_decision(position, request)
        if decision is None:
            return _stale_vision_interview(position)
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
        input_service = self._vision_interview_input
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
                user_text=VisionInterviewInput.normalize_user_response(
                    request.user_text
                ),
            )
        )

    def review_vision(self, request: VisionReviewRequest) -> TransitionResult:
        """Prepare exact pending Vision identity internally before review."""
        input_service = self._vision_interview_input
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
        input_service = self._vision_interview_input
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
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "authority.compile")
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return _transition_not_available(position, "authority.compile")
        input_service = self._authority_compilation_input
        if input_service is None:
            message = "Authority compilation requires an injected input builder."
            raise RuntimeError(message)
        model_id = get_model_id(AGENTIC_MODEL_ROLES["authority.compile"])
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id="authority.compile",
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
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
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "authority.review")
        selection = self._authority_review_selection
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

    def repair_authority(
        self,
        request: AuthorityRepairRequest,
    ) -> TransitionResult:
        """Prepare current repair guards and compiler input from durable facts."""
        position = self.position(project_id=request.project_id)
        decision = _unique_available_decision(position, "authority.repair")
        input_service = self._authority_repair_input
        if (
            decision is None
            or decision.category is not NodeCategory.AVAILABLE
            or input_service is None
        ):
            return _transition_not_available(position, "authority.repair")
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                node_id="authority.repair",
                instance_key=decision.instance_key,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
        )
        if replay is not None:
            return replay
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


def _unique_available_decision(
    position: WorkflowPosition,
    node_id: str,
) -> NodeDecision | None:
    """Return one current command decision without accepting an ambiguous position."""
    candidates = tuple(
        item
        for item in position.decisions
        if item.node_id == node_id
        and item.category in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
    )
    return candidates[0] if len(candidates) == 1 else None


def _single_fact_reference(
    decision: NodeDecision,
    fact_type: str,
) -> FactReference | None:
    """Return one exact graph reference without accepting an ambiguous target."""
    references = tuple(
        item for item in decision.fact_references if item.fact_type == fact_type
    )
    return references[0] if len(references) == 1 else None


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
    graph = project_graph()
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=build_spec_authority_compiler_agent(),
            authority_repair=build_spec_authority_compiler_agent(),
            vision_interview=vision_interview_agent,
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
        vision_interview_input=VisionInterviewInputService(engine=engine),
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=ProductGoalInterviewInputService(engine=engine),
            discovery_selection=ProductDiscoverySelectionService(engine=engine),
        ),
        authority_compilation_input=AuthorityCompilationInputService(engine=engine),
        authority_review_selection=AuthorityReviewSelectionService(engine=engine),
        authority_repair_input=AuthorityRepairInputService(engine=engine),
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
    "AuthorityRepairInputService",
    "AuthorityRepairRequest",
    "AuthorityReviewRequest",
    "AuthorityReviewSelectionService",
    "CreateProjectCommand",
    "DiscoveryArtifactRequest",
    "ProductGoalInterviewRequest",
    "ProductGoalLifecycleServices",
    "ProductGoalOutcomeRequest",
    "ProductGoalResponseRequest",
    "ProductGoalReviewRequest",
    "RepositoryAttachRequest",
    "RepositoryAttachmentCommand",
    "RepositoryRefreshCommand",
    "RepositoryRefreshRequest",
    "SpecificationCandidateRequest",
    "SpecificationReviewRequest",
    "VisionInterviewRequest",
    "VisionResponseRequest",
    "VisionReviewRequest",
    "VisionRevisionRequest",
    "WorkflowDomainPort",
    "production_application",
]
