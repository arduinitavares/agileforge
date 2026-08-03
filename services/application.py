"""Production application boundary for the durable workflow graph."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Protocol

from workflow.contracts import (
    FrozenModel,
    JsonObject,
    TransitionResult,
    WorkflowPosition,
)

if TYPE_CHECKING:
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


class _ReadProjectionPort(Protocol):
    """Supported non-routing reads exposed to production transports."""

    def project_list(self) -> JsonObject: ...

    def project_show(self, *, project_id: int) -> JsonObject: ...

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


class AgileForgeApplication:
    """Expose the narrow workflow application interface to transports."""

    def __init__(
        self,
        *,
        workflow_domain: WorkflowDomainPort,
        recipe_registry: AdkRecipeRegistry | None = None,
        read_projection: _ReadProjectionPort | None = None,
    ) -> None:
        """Retain exactly one workflow authority."""
        self._workflow_domain = workflow_domain
        self._recipe_registry = recipe_registry
        self._read_projection = read_projection

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


@cache
def production_application() -> AgileForgeApplication:
    """Compose the production domain and all eight ADK recipe leaves."""
    from adapters.adk.agents.backlog import root_agent as backlog_agent  # noqa: PLC0415
    from adapters.adk.agents.brownfield import (  # noqa: PLC0415
        build_brownfield_curator_agent,
    )
    from adapters.adk.agents.roadmap import root_agent as roadmap_agent  # noqa: PLC0415
    from adapters.adk.agents.specification import (  # noqa: PLC0415
        build_spec_authority_compiler_agent,
    )
    from adapters.adk.agents.sprint import root_agent as sprint_agent  # noqa: PLC0415
    from adapters.adk.agents.story import (  # noqa: PLC0415
        create_user_story_writer_agent,
    )
    from adapters.adk.agents.vision import root_agent as vision_agent  # noqa: PLC0415
    from adapters.adk.recipes import (  # noqa: PLC0415
        AgenticRecipeNodes,
        build_agentic_recipe_registry,
    )
    from models.db import ensure_business_db_ready, get_engine  # noqa: PLC0415
    from services.read_projections import (  # noqa: PLC0415
        DurableReadProjectionService,
    )
    from workflow.clock import SystemClock  # noqa: PLC0415
    from workflow.definitions.root import project_graph  # noqa: PLC0415
    from workflow.domain import WorkflowDomain  # noqa: PLC0415

    graph = project_graph()
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            brownfield_curator=build_brownfield_curator_agent(),
            authority_compile=build_spec_authority_compiler_agent(),
            authority_repair=build_spec_authority_compiler_agent(),
            vision_generation=vision_agent,
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
    )


__all__ = [
    "AgenticActionRequest",
    "AgileForgeApplication",
    "WorkflowDomainPort",
    "production_application",
]
