"""Production application boundary for the durable workflow graph."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import Field

from adapters.adk.model_roles import AGENTIC_MODEL_ROLES
from services.contracts.product_goal import ProductGoalInterviewInput
from services.contracts.vision import VisionInterviewInput
from services.node_attempt_replay import NodeAttemptReplayQuery, TransitionReplayQuery
from services.product_goal_interview_input import ProductGoalInterviewInputService
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
    DecideProductGoalReview,
    DecideSpecification,
    DecideVisionReview,
    FulfillProductGoal,
    RecordDiscoveryArtifact,
    RecordSpecificationCandidate,
)

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent

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


@dataclass(frozen=True)
class ProductGoalLifecycleServices:
    """Host-owned services for the isolated Product Goal child graph."""

    interview_input: _ProductGoalInterviewInputPort
    discovery_selection: _ProductDiscoverySelectionPort


class _ReadProjectionPort(Protocol):
    """Supported non-routing reads exposed to production transports."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

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


class AgileForgeApplication:
    """Expose the narrow workflow application interface to transports."""

    def __init__(
        self,
        *,
        workflow_domain: WorkflowDomainPort,
        recipe_registry: AdkRecipeRegistry | None = None,
        read_projection: _ReadProjectionPort | None = None,
        vision_interview_input: _VisionInterviewInputPort | None = None,
        product_goal_services: ProductGoalLifecycleServices | None = None,
    ) -> None:
        """Retain exactly one workflow authority."""
        self._workflow_domain = workflow_domain
        self._recipe_registry = recipe_registry
        self._read_projection = read_projection
        self._vision_interview_input = vision_interview_input
        self._product_goal_services = product_goal_services

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

    def run_vision_interview(
        self,
        request: VisionInterviewRequest,
    ) -> TransitionResult:
        """Run one host-prepared, replay-safe Project Vision interview turn."""
        node_id = "vision.interview"
        input_service = self._vision_interview_input
        if input_service is None:
            message = "Vision interview requires an injected input builder."
            raise RuntimeError(message)
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=request.graph_version,
                fact_fingerprint=request.fact_fingerprint,
                decision_fingerprint=request.decision_fingerprint,
                node_id=node_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                user_text=VisionInterviewInput.normalize_user_response(
                    request.user_text
                ),
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
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
                node_id=node_id,
                input_payload=input_payload,
                model_id=get_model_id(AGENTIC_MODEL_ROLES[node_id]),
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
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
            return _stale_vision_review(position)
        reference = _single_fact_reference(decision, "vision")
        if reference is None:
            return _stale_vision_review(position)
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
            return _stale_vision_revision(position)
        reference = _single_fact_reference(decision, "vision")
        if reference is None:
            return _stale_vision_revision(position)
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

    def run_product_goal_interview(
        self,
        request: ProductGoalInterviewRequest,
    ) -> TransitionResult:
        """Run a replay-safe Goal interview with host-derived durable context."""
        node_id = "goal.interview"
        services = self._product_goal_services
        if services is None:
            message = "Product Goal interview requires an injected input builder."
            raise RuntimeError(message)
        input_service = services.interview_input
        replay = input_service.replay(
            NodeAttemptReplayQuery(
                project_id=request.project_id,
                graph_version=request.graph_version,
                fact_fingerprint=request.fact_fingerprint,
                decision_fingerprint=request.decision_fingerprint,
                node_id=node_id,
                idempotency_key=request.idempotency_key,
                actor=request.actor,
                correlation_id=request.correlation_id,
                user_text=ProductGoalInterviewInput.normalize_user_response(
                    request.user_text
                ),
            )
        )
        if replay is not None:
            return replay
        position = self.position(project_id=request.project_id)
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
        reference = None if decision is None else _single_fact_reference(
            decision, "product_goal"
        )
        if decision is None or reference is None:
            return _stale_product_goal_review(position)
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
        reference = None if decision is None else _single_fact_reference(
            decision, "product_goal"
        )
        if decision is None or reference is None:
            return _stale_product_goal_outcome(position)
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
            return _stale_product_discovery(position)
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
            return _stale_product_discovery(position)
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
            return _stale_product_discovery(position)
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


def _stale_product_goal_review(position: WorkflowPosition) -> TransitionResult:
    """Reject a Goal review without one pending graph candidate."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="The Product Goal review position is stale.",
        ),
    )


def _stale_product_goal_outcome(position: WorkflowPosition) -> TransitionResult:
    """Reject a Goal outcome without one active graph candidate."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="The Product Goal outcome position is stale.",
        ),
    )


def _stale_product_discovery(position: WorkflowPosition) -> TransitionResult:
    """Reject discovery or specification commands without one current decision."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="The Product Discovery position is stale.",
        ),
    )


def _stale_vision_review(position: WorkflowPosition) -> TransitionResult:
    """Reject a Vision review without one unique pending graph decision."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="The Vision review position is stale.",
        ),
    )


def _stale_vision_revision(position: WorkflowPosition) -> TransitionResult:
    """Reject a Vision revision that is no longer eligible."""
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.STALE_POSITION,
            message="The Vision revision position is stale.",
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

    return AgileForgeApplication(
        workflow_domain=domain,
        recipe_registry=registry,
        read_projection=DurableReadProjectionService(engine=engine),
        vision_interview_input=VisionInterviewInputService(engine=engine),
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=ProductGoalInterviewInputService(engine=engine),
            discovery_selection=ProductDiscoverySelectionService(engine=engine),
        ),
    )


__all__ = [
    "AgenticActionRequest",
    "AgileForgeApplication",
    "DiscoveryArtifactRequest",
    "ProductGoalInterviewRequest",
    "ProductGoalLifecycleServices",
    "ProductGoalOutcomeRequest",
    "ProductGoalReviewRequest",
    "SpecificationCandidateRequest",
    "SpecificationReviewRequest",
    "VisionInterviewRequest",
    "VisionReviewRequest",
    "VisionRevisionRequest",
    "WorkflowDomainPort",
    "production_application",
]
