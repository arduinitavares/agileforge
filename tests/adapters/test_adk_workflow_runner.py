"""Domain-bounded ADK workflow runner tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from google.adk import Workflow as AdkWorkflow
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.adk.sessions import Session as AdkSession
from google.adk.workflow import START, node
from openai import OpenAIError
from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from adapters.adk.errors import VisionAgenticPreflightError
from adapters.adk.prompts.specification import SPEC_AUTHORITY_COMPILER_PROMPT_HASH
from adapters.adk.recipes import (
    AdkRecipe,
    AdkRecipeRegistry,
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    build_agentic_recipe_registry,
    build_backlog_generation_workflow,
)
from adapters.adk.runner import (
    AdkExecutionConfig,
    AdkRunGuards,
    AdkRunRequest,
    AdkWorkflowRunner,
)
from models.core import Project
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    VisionInterviewTurn,
)
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from models.workflow import (
    BacklogArtifact,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from services.authority_compilation_input import AuthorityCompilationInputService
from services.contracts.product_goal import (
    ProductGoalInterviewInput,
    ProductGoalInterviewOutput,
)
from services.contracts.vision import VisionDraftOutput, VisionModelInput
from services.specs import compiler_service
from services.specs.authority_selection import pending_authority_fingerprint
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.runtime_config import ADK_EXECUTION_TRACE_IDENTITY
from utils.spec_schemas import (
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerInput,
)
from workflow.clock import FixedClock
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
)
from workflow.definitions.authority import authority_graph
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    RecordBacklogDraft,
    StartNodeAttempt,
    TransitionRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)
LEASE_SECONDS = 60
EXPECTED_RECOVERY_ATTEMPT_COUNT = 2
NEXT_GOAL_NUMBER = 2
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}
JSON_OBJECT = TypeAdapter(JsonObject)
_AUTHORITY_SOURCE_ID = "REQ.runner.authority-boundary"
_NON_NORMATIVE_SENTINEL = "NON_NORMATIVE_SENTINEL_MUST_NEVER_REACH_AUTHORITY"


@dataclass
class MutableClock:
    """Clock advanced across an expiry-recovery runner test."""

    now_value: datetime

    def now(self) -> datetime:
        """Return the controlled current time."""
        return self.now_value


class FakeLeafAgent(BaseAgent):
    """Provider-free leaf with deterministic output or failure."""

    response: object
    failure_message: str | None = None

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        if self.failure_message is not None:
            raise RuntimeError(self.failure_message)
        yield Event(author=self.name, output=self.response)


class ProviderSdkFailureLeafAgent(BaseAgent):
    """Provider-free leaf reproducing one OpenAI-compatible SDK failure."""

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        message = "provider routing failed"
        raise OpenAIError(message)
        yield


class CountingLeafAgent(BaseAgent):
    """Provider-free leaf recording each external execution."""

    response: object
    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append("provider")
        yield Event(author=self.name, output=self.response)


class BlockingLeafAgent(BaseAgent):
    """Hold the first provider call open while a duplicate start arrives."""

    response: object
    calls: list[str]
    started: threading.Event
    release: threading.Event

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append("provider")
        if len(self.calls) == 1:
            self.started.set()
            await asyncio.to_thread(self.release.wait)
        yield Event(author=self.name, output=self.response)


class ReceiptObserver:
    """Record durable transition receipts visible at provider-call time."""

    def __init__(self, engine: Engine, calls: list[tuple[str, ...]]) -> None:
        """Retain the database and external observation sink."""
        self._engine = engine
        self._calls = calls
        self.events: list[str] = []

    def record(self) -> None:
        """Capture committed receipt kinds using an independent session."""
        self.events.append("provider")
        with Session(self._engine) as session:
            receipts = session.exec(
                select(WorkflowTransitionReceipt).order_by(
                    col(WorkflowTransitionReceipt.workflow_transition_receipt_id)
                )
            ).all()
        self._calls.append(tuple(receipt.request_kind for receipt in receipts))


class TransactionObservingLeafAgent(BaseAgent):
    """Fake compiler recording durable state visible during external work."""

    response: object
    observer: ReceiptObserver

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.observer.record()
        yield Event(author=self.name, output=self.response)


class TrackingSessionService(InMemorySessionService):
    """In-memory ADK trace store recording created session IDs."""

    def __init__(self) -> None:
        """Initialize the trace store and its session-ID observations."""
        super().__init__()
        self.created_session_ids: list[str] = []

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> AdkSession:
        """Record explicit attempt-keyed IDs before creating sessions."""
        if session_id is not None:
            self.created_session_ids.append(session_id)
        return await super().create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )


class NumericIdCollisionSessionService(TrackingSessionService):
    """ADK trace store containing every reusable numeric session identity."""

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> AdkSession:
        """Reject numeric IDs as stale while accepting durable fingerprints."""
        if session_id is not None and session_id.isdecimal():
            await super().create_session(
                app_name=app_name,
                user_id=user_id,
                state=state,
                session_id=session_id,
            )
        return await super().create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )


class CollidingSessionService(TrackingSessionService):
    """ADK trace store that creates then collides on the requested session."""

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> AdkSession:
        """Create the trace once, then reproduce ADK's duplicate-ID failure."""
        await super().create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )
        return await super().create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )


@dataclass(frozen=True)
class _BacklogLineage:
    project_id: int
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    authority_id: int
    authority_fingerprint: str


def _seed(engine: Engine) -> _BacklogLineage:
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["Runner"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    with Session(engine) as session:
        project = Project(name="Runner")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content='{"scope":"runner"}',
            recorded_at=EVALUATED_AT - timedelta(minutes=1),
        )
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version=artifact.compiler_version,
            prompt_hash=artifact.prompt_hash,
            compiled_at=EVALUATED_AT,
            compiled_artifact_json=artifact.model_dump_json(),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.flush()
        assert authority.authority_id is not None
        fingerprint = pending_authority_fingerprint(authority)
        assert fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                project_id=project.project_id,
                spec_version_id=spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT,
                rationale="Accepted.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=lineage.spec.spec_hash,
                pending_authority_id=authority.authority_id,
                authority_fingerprint=fingerprint,
                review_fingerprint="sha256:review",
                terminal_decision_key="runner-authority",
            )
        )
        session.commit()
        return _BacklogLineage(
            project_id=project.project_id,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            authority_id=authority.authority_id,
            authority_fingerprint=fingerprint,
        )


def _backlog_payload() -> JsonObject:
    return {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Execute graph nodes through ADK",
                "authority_ref": "REQ.runner",
                "capability_hint": None,
                "value_driver": "Strategic",
                "justification": "Keep durable facts authoritative.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _backlog_response(lineage: _BacklogLineage) -> JsonObject:
    return {
        "product_goal_artifact_id": lineage.product_goal_artifact_id,
        "product_goal_fingerprint": lineage.product_goal_fingerprint,
        "authority_id": lineage.authority_id,
        "authority_fingerprint": lineage.authority_fingerprint,
        "content": _backlog_payload(),
    }


def _authority_artifact() -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Runner authority"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[f"{_AUTHORITY_SOURCE_ID}: provider-free boundary regression."],
        assumptions=[],
        source_map=[],
        compiler_version=compiler_service.SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    )


def _seed_authority_compile_target(engine: Engine) -> tuple[int, int, str]:
    with Session(engine) as session:
        project = Project(name="Runner compile")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content=json.dumps(
                {
                    "schema_version": "agileforge.spec.v2",
                    "artifact_id": "SPEC.runner-authority-boundary",
                    "title": "Runner Authority boundary",
                    "summary": _NON_NORMATIVE_SENTINEL,
                    "problem_statement": _NON_NORMATIVE_SENTINEL,
                    "items": [
                        {
                            "id": "GOAL.runner.review-context",
                            "type": "GOAL",
                            "title": "Review context",
                            "statement": _NON_NORMATIVE_SENTINEL,
                        },
                        {
                            "id": _AUTHORITY_SOURCE_ID,
                            "type": "REQ",
                            "title": "Provider boundary",
                            "statement": "Authority input MUST remain typed.",
                            "level": "MUST",
                            "verification": "integration-test",
                            "acceptance": [
                                "The provider receives only typed Authority input."
                            ],
                        },
                    ],
                }
            ),
            recorded_at=EVALUATED_AT - timedelta(minutes=1),
        )
        assert lineage.spec.spec_version_id is not None
        return (
            project.project_id,
            lineage.spec.spec_version_id,
            lineage.spec.spec_hash,
        )


def _observing_authority_leaf(
    *,
    observer: ReceiptObserver,
    provider_inputs: list[str],
) -> AdkWorkflow:
    """Capture the exact compiler DTO crossing the nested ADK node boundary."""

    @node(name="observe_authority_compiler_input", rerun_on_resume=True)
    async def observe_authority_compiler_input(
        node_input: SpecAuthorityCompilerInput,
    ) -> SpecAuthorityCompilerEnvelope:
        observer.record()
        provider_inputs.append(node_input.model_dump_json())
        return SpecAuthorityCompilerEnvelope(result=_authority_artifact())

    return AdkWorkflow(
        name="observing_authority_compiler",
        input_schema=SpecAuthorityCompilerInput,
        output_schema=SpecAuthorityCompilerEnvelope,
        edges=[(START, observe_authority_compiler_input)],
    )


def _unused_leaf(name: str) -> FakeLeafAgent:
    return FakeLeafAgent(name=name, response={})


def _goal_output() -> ProductGoalInterviewOutput:
    return ProductGoalInterviewOutput.model_validate(
        {
            "updated_components": {
                "valuable_future_state": "A second increment is accepted",
                "beneficiary": "Operators",
                "value": "Predictable delivery",
                "success_signals": ["The second increment reaches triage"],
                "boundaries": ["No provider calls"],
            },
            "product_goal_statement": "Complete a second accepted increment.",
            "is_complete": True,
            "clarifying_questions": [],
        }
    )


def _validating_goal_leaf(
    observations: list[ProductGoalInterviewInput],
) -> AdkWorkflow:
    """Build a provider-free leaf that consumes only prepared Goal context."""

    @node(name="validate_product_goal_input", rerun_on_resume=True)
    async def validate_product_goal_input(
        node_input: ProductGoalInterviewInput,
    ) -> ProductGoalInterviewOutput:
        dumped = node_input.model_dump(mode="json")
        assert "compiled_authority" not in dumped
        assert "specification" not in dumped
        assert node_input.accepted_vision_statement
        observations.append(node_input)
        return _goal_output()

    return AdkWorkflow(
        name="fake_product_goal_interviewer",
        input_schema=ProductGoalInterviewInput,
        output_schema=ProductGoalInterviewOutput,
        edges=[(START, validate_product_goal_input)],
    )


def _observing_vision_leaf(
    observations: list[VisionModelInput],
    response: JsonObject,
) -> AdkWorkflow:
    """Build a provider-free Vision leaf that records its exact prepared input."""

    @node(name="observe_vision_input", rerun_on_resume=True)
    async def observe_vision_input(
        node_input: VisionModelInput,
    ) -> VisionDraftOutput:
        observations.append(node_input)
        return VisionDraftOutput.model_validate(response)

    return AdkWorkflow(
        name="fake_vision_interviewer",
        input_schema=VisionModelInput,
        output_schema=VisionDraftOutput,
        edges=[(START, observe_vision_input)],
    )


def _goal_registry(
    leaf: BaseAgent | AdkWorkflow,
    *,
    vision_leaf: BaseAgent | None = None,
) -> AdkRecipeRegistry:
    return build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=_unused_leaf("unused_authority_compile"),
            authority_repair=_unused_leaf("unused_authority_repair"),
            authority_compile_validation_repair=_unused_leaf(
                "unused_authority_compile_validation_repair"
            ),
            authority_repair_validation_repair=_unused_leaf(
                "unused_authority_repair_validation_repair"
            ),
            vision_interview=vision_leaf or _unused_leaf("unused_vision_interview"),
            vision_repair=_unused_leaf("unused_vision_repair"),
            product_goal=leaf,
            specification_structurer=_unused_leaf("unused_specification_structurer"),
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )


def _vision_preflight_runner(
    engine: Engine,
    clock: MutableClock,
    provider_calls: list[str],
) -> tuple[AdkWorkflowRunner, WorkflowDomain, int]:
    """Build one provider-free Vision runner with a mutable attempt clock."""
    with Session(engine) as session:
        project = Project(name="Vision preflight", description="Original")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    vision_leaf = CountingLeafAgent(
        name="counting_vision_provider",
        response={},
        calls=provider_calls,
    )
    registry = _goal_registry(
        _unused_leaf("unused_product_goal"),
        vision_leaf=vision_leaf,
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=clock,
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=TrackingSessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/vision",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
        ),
    )
    return runner, domain, project_id


def _goal_runner_system(
    engine: Engine,
    leaf: BaseAgent | AdkWorkflow,
) -> tuple[AdkWorkflowRunner, WorkflowDomain, int]:
    with Session(engine) as session:
        project = Project(name="Runner Goal")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"scope":"resolved first goal"}',
            recorded_at=EVALUATED_AT - timedelta(minutes=1),
        )
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=lineage.product_goal_artifact_id,
                artifact_fingerprint=lineage.product_goal_fingerprint,
                outcome="fulfilled",
                rationale="The first Goal is complete.",
                decided_by="operator@example.com",
                idempotency_key="runner-first-goal-fulfilled",
                decided_at=EVALUATED_AT,
            )
        )
        session.commit()
    registry = _goal_registry(leaf)
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=TrackingSessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/product-goal",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
            correlation_id="task-15-product-goal",
        ),
    )
    return runner, domain, project_id


def _goal_runner_input() -> JsonObject:
    return {
        "project_name": "Runner Goal",
        "accepted_vision_statement": "Deliver one verified product increment.",
        "user_response": "Define the next valuable Product Goal.",
        "prior_components": None,
    }


def _goal_decision(
    domain: WorkflowDomain,
    project_id: int,
) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "goal.interview"
    )


def _adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordBacklogDraft:
    recipe_output = RecipeOutput.model_validate(output)
    payload = JSON_OBJECT.validate_python(recipe_output.payload)
    product_goal_artifact_id = payload.pop("product_goal_artifact_id")
    product_goal_fingerprint = payload.pop("product_goal_fingerprint")
    authority_id = payload.pop("authority_id")
    authority_fingerprint = payload.pop("authority_fingerprint")
    content = JSON_OBJECT.validate_python(payload.pop("content"))
    assert isinstance(product_goal_artifact_id, int)
    assert isinstance(product_goal_fingerprint, str)
    assert isinstance(authority_id, int)
    assert isinstance(authority_fingerprint, str)
    return RecordBacklogDraft(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        product_goal_artifact_id=product_goal_artifact_id,
        product_goal_fingerprint=product_goal_fingerprint,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        canonical_content=content,
        content_fingerprint=canonical_hash(content),
    )


def _build_runner(
    engine: Engine,
    *,
    project_id: int,
    leaf: BaseAgent | AdkWorkflow,
    sessions: TrackingSessionService,
    clock: MutableClock | None = None,
) -> tuple[AdkWorkflowRunner, WorkflowDomain]:
    recipe = AdkRecipe(
        node_id="backlog.generate",
        workflow=build_backlog_generation_workflow(
            leaf_agent=leaf,
            execution_settings=EXECUTION_SETTINGS,
        ),
        output_adapter=_adapter,
    )
    registry = AdkRecipeRegistry((recipe,))
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=clock or FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=sessions,
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/model",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
            correlation_id="task-15",
        ),
    )
    return runner, domain


def _decision(domain: WorkflowDomain, project_id: int) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "backlog.generate"
    )


def _node_attempts(session: Session, node_id: str) -> list[WorkflowNodeAttempt]:
    return list(
        session.exec(
            select(WorkflowNodeAttempt)
            .where(col(WorkflowNodeAttempt.node_id) == node_id)
            .order_by(col(WorkflowNodeAttempt.workflow_node_attempt_id))
        ).all()
    )


def _node_outcomes(
    session: Session,
    node_id: str,
) -> list[WorkflowNodeAttemptOutcome]:
    attempt_ids = {
        attempt.workflow_node_attempt_id
        for attempt in _node_attempts(session, node_id)
        if attempt.workflow_node_attempt_id is not None
    }
    return [
        outcome
        for outcome in session.exec(select(WorkflowNodeAttemptOutcome)).all()
        if outcome.workflow_node_attempt_id in attempt_ids
    ]


def test_stale_preflight_lease_expiry_returns_durable_obsolete_result(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the committed obsolete transition when stale preflight loses its lease."""
    clock = MutableClock(EVALUATED_AT)
    provider_calls: list[str] = []
    runner, domain, project_id = _vision_preflight_runner(
        engine,
        clock,
        provider_calls,
    )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )
    preflight_runs: list[str] = []

    async def expire_before_failure(
        _recipe: AdkRecipe,
        *,
        attempt_fingerprint: str,
        input_payload: JsonObject,
    ) -> RecipeOutput:
        del attempt_fingerprint, input_payload
        preflight_runs.append("preflight")
        clock.now_value += timedelta(seconds=LEASE_SECONDS)
        raise VisionAgenticPreflightError(
            code=WorkflowErrorCode.VISION_EVIDENCE_STALE,
            message="Vision evidence changed before provider execution.",
        )

    monkeypatch.setattr(runner, "_run_recipe", expire_before_failure)
    guards = AdkRunGuards(
        position=position,
        idempotency_key="vision-preflight-expired",
        actor="operator@example.com",
    )

    first = runner.run(decision, {}, guards=guards)
    replayed = runner.run(decision, {}, guards=guards)

    assert first.error is not None
    assert first.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
    assert replayed.replayed is True
    assert replayed.error is not None
    assert replayed.error.code is first.error.code
    assert replayed.error.message == first.error.message
    assert preflight_runs == ["preflight"]
    assert provider_calls == []


def test_stale_preflight_fact_change_returns_durable_obsolete_result(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return committed obsolescence when business facts change before failure."""
    clock = MutableClock(EVALUATED_AT)
    provider_calls: list[str] = []
    runner, domain, project_id = _vision_preflight_runner(
        engine,
        clock,
        provider_calls,
    )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )
    preflight_runs: list[str] = []

    async def change_fact_before_failure(
        _recipe: AdkRecipe,
        *,
        attempt_fingerprint: str,
        input_payload: JsonObject,
    ) -> RecipeOutput:
        del attempt_fingerprint, input_payload
        preflight_runs.append("preflight")
        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.description = "Changed during stale preflight."
            session.add(project)
            session.commit()
        raise VisionAgenticPreflightError(
            code=WorkflowErrorCode.VISION_EVIDENCE_STALE,
            message="Vision evidence changed before provider execution.",
        )

    monkeypatch.setattr(runner, "_run_recipe", change_fact_before_failure)
    guards = AdkRunGuards(
        position=position,
        idempotency_key="vision-preflight-fact-change",
        actor="operator@example.com",
    )

    first = runner.run(decision, {}, guards=guards)
    replayed = runner.run(decision, {}, guards=guards)

    assert first.error is not None
    assert first.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
    assert replayed.replayed is True
    assert replayed.error is not None
    assert replayed.error.code is first.error.code
    assert replayed.error.message == first.error.message
    assert preflight_runs == ["preflight"]
    assert provider_calls == []


def test_runner_loads_vision_input_from_persisted_attempt(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion ignores a mutated in-memory start request after persistence."""
    with Session(engine) as session:
        project = Project(name="Persisted Vision")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    evidence_item: JsonObject = {
        "evidence_id": "project:metadata",
        "kind": "project_metadata",
        "relative_path": None,
        "content_fingerprint": canonical_hash(
            {"name": "Persisted Vision", "description": None}
        ),
        "trust": "operator_provided",
        "content": {"name": "Persisted Vision", "description": None},
        "truncated": False,
    }
    evidence: JsonObject = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [evidence_item],
        "warnings": [],
        "evidence_fingerprint": canonical_hash(
            {
                "schema_version": "agileforge.vision-evidence.v1",
                "items": [evidence_item],
                "warnings": [],
            }
        ),
    }
    components: JsonObject = {
        "project_name": "Persisted Vision",
        "target_user": "Operators",
        "problem": "State drift",
        "product_category": "Tool",
        "key_benefit": "Trust",
        "competitors": "Spreadsheets",
        "differentiator": "Durable facts",
    }
    vision_response: JsonObject = {
        "schema_version": "agileforge.vision-draft.v1",
        "components": components,
        "component_basis": [
            {
                "component": name,
                "source_kinds": ["evidence"],
                "evidence_ids": ["project:metadata"],
                "assumption_ids": [],
            }
            for name in components
        ],
        "draft_statement": "A trusted workflow tool.",
        "assumptions": [],
        "conflicts": [],
        "is_complete": True,
        "clarifying_questions": [],
    }
    observations: list[VisionModelInput] = []
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=_unused_leaf("unused_authority_compile"),
            authority_repair=_unused_leaf("unused_authority_repair"),
            authority_compile_validation_repair=_unused_leaf(
                "unused_authority_compile_validation_repair"
            ),
            authority_repair_validation_repair=_unused_leaf(
                "unused_authority_repair_validation_repair"
            ),
            vision_interview=_observing_vision_leaf(observations, vision_response),
            vision_repair=_unused_leaf("unused_vision_repair"),
            product_goal=_unused_leaf("unused_product_goal"),
            specification_structurer=_unused_leaf("unused_specification_structurer"),
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=TrackingSessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/vision",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
        ),
    )
    transition = domain.transition

    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )
    caller_request = AdkRunRequest(
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        node_id=decision.node_id,
        instance_key=decision.instance_key,
        input_payload={
            "request": {
                "schema_version": "agileforge.vision-input.v1",
                "operation": "bootstrap",
                "project_name": "Persisted Vision",
                "project_description": None,
                "evidence": evidence,
            },
            "preflight": None,
        },
        idempotency_key="persisted-vision-input",
        actor="operator@example.com",
    )

    def persist_then_tamper(request: TransitionRequest) -> TransitionResult:
        result = transition(request)
        if isinstance(request, StartNodeAttempt):
            normalized_request = caller_request.input_payload.get("request")
            if not isinstance(normalized_request, dict):
                message = "Vision attempt input did not include a request object."
                raise TypeError(message)
            normalized_request["project_name"] = "Tampered"
        return result

    monkeypatch.setattr(domain, "transition", persist_then_tamper)

    result = runner.run_request(caller_request)

    assert result.ok
    assert len(observations) == 1
    assert observations[0].request.project_name == "Persisted Vision"
    with Session(engine) as session:
        turn = session.exec(select(VisionInterviewTurn)).one()
        assert turn.operation == "bootstrap"
        assert turn.user_text is None


def test_runner_executes_product_goal_recipe_through_record_goal_turn(
    engine: Engine,
) -> None:
    """The v2 root recipe adapts trusted Goal output end to end."""
    runner, domain, project_id = _goal_runner_system(
        engine,
        FakeLeafAgent(
            name="product_goal",
            response=_goal_output().model_dump(mode="json"),
        ),
    )

    result = runner.run(_goal_decision(domain, project_id), _goal_runner_input())

    assert result.ok
    with Session(engine) as session:
        turn = session.exec(
            select(ProductGoalInterviewTurn).order_by(
                col(ProductGoalInterviewTurn.product_goal_interview_turn_id).desc()
            )
        ).first()
        artifact = session.exec(
            select(ProductGoalArtifact).order_by(
                col(ProductGoalArtifact.product_goal_artifact_id).desc()
            )
        ).first()
        assert turn is not None
        assert artifact is not None
        assert turn.user_text == "Define the next valuable Product Goal."
        assert artifact.goal_number == NEXT_GOAL_NUMBER
        assert artifact.statement == _goal_output().product_goal_statement


def test_runner_executes_fake_leaf_and_commits_validated_output(engine: Engine) -> None:
    """Run a provider-free recipe and commit its fact and success outcome."""
    lineage = _seed(engine)
    sessions = TrackingSessionService()
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response=_backlog_response(lineage)),
        sessions=sessions,
    )

    result = runner.run(
        _decision(domain, lineage.project_id),
        {"prompt": "build backlog"},
    )

    assert result.ok is True
    with Session(engine) as session:
        attempt = _node_attempts(session, "backlog.generate")[0]
        outcome = _node_outcomes(session, "backlog.generate")[0]
        assert outcome.status == "success"
        assert session.exec(select(BacklogArtifact)).one() is not None
        assert sessions.created_session_ids == [attempt.attempt_fingerprint]


def test_runner_ignores_stale_trace_with_reused_numeric_attempt_id(
    engine: Engine,
) -> None:
    """Key new traces by durable identity when a numeric ID is already present."""
    lineage = _seed(engine)
    sessions = NumericIdCollisionSessionService()
    calls: list[str] = []
    leaf = CountingLeafAgent(
        name="counting_backlog",
        response=_backlog_response(lineage),
        calls=calls,
    )
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=leaf,
        sessions=sessions,
    )

    result = runner.run(
        _decision(domain, lineage.project_id),
        {"prompt": "build backlog"},
    )

    assert result.ok is True
    assert leaf.calls == ["provider"]
    with Session(engine) as session:
        attempt = _node_attempts(session, "backlog.generate")[0]
        assert sessions.created_session_ids == [attempt.attempt_fingerprint]


def test_sequential_transport_retry_replays_terminal_result_without_provider(
    engine: Engine,
) -> None:
    """Return the completed command receipt for the same transport key."""
    lineage = _seed(engine)
    calls: list[str] = []
    leaf = CountingLeafAgent(
        name="counting_backlog",
        response=_backlog_response(lineage),
        calls=calls,
    )
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=leaf,
        sessions=TrackingSessionService(),
    )
    position = domain.position(lineage.project_id)
    decision = _decision(domain, lineage.project_id)
    guards = AdkRunGuards(
        position=position,
        idempotency_key="dashboard-retry-41",
        actor="dashboard-user",
        correlation_id="retry-41",
    )

    first = runner.run(decision, {"prompt": "build backlog"}, guards=guards)
    replay = runner.run(decision, {"prompt": "build backlog"}, guards=guards)

    assert first.ok is True
    assert replay == first.model_copy(update={"replayed": True})
    assert leaf.calls == ["provider"]
    with Session(engine) as session:
        assert len(_node_attempts(session, "backlog.generate")) == 1
        assert len(_node_outcomes(session, "backlog.generate")) == 1


def test_concurrent_duplicate_start_never_enters_provider_twice(
    engine: Engine,
) -> None:
    """Short-circuit a replay while the live attempt is outside its transaction."""
    lineage = _seed(engine)
    calls: list[str] = []
    started = threading.Event()
    release = threading.Event()
    leaf = BlockingLeafAgent(
        name="blocking_backlog",
        response=_backlog_response(lineage),
        calls=calls,
        started=started,
        release=release,
    )
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=leaf,
        sessions=TrackingSessionService(),
    )
    position = domain.position(lineage.project_id)
    decision = _decision(domain, lineage.project_id)
    guards = AdkRunGuards(
        position=position,
        idempotency_key="dashboard-concurrent-41",
        actor="dashboard-user",
        correlation_id="concurrent-41",
    )
    normalized_input: JsonObject = {"prompt": "build backlog"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            runner.run,
            decision,
            normalized_input,
            guards=guards,
        )
        assert started.wait(timeout=5)
        try:
            replay = runner.run(
                decision,
                normalized_input,
                guards=guards,
            )
            assert replay.ok is True
            assert replay.replayed is True
            assert leaf.calls == ["provider"]
        finally:
            release.set()
        first = first_future.result(timeout=5)

    assert first.ok is True
    with Session(engine) as session:
        assert len(_node_attempts(session, "backlog.generate")) == 1
        assert len(_node_outcomes(session, "backlog.generate")) == 1


def test_provider_failure_records_failure_and_returns_external_error(
    engine: Engine,
) -> None:
    """Translate a fake provider failure after recording its durable outcome."""
    lineage = _seed(engine)
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=FakeLeafAgent(
            name="fake_backlog",
            response={},
            failure_message="provider unavailable",
        ),
        sessions=TrackingSessionService(),
    )

    result = runner.run(
        _decision(domain, lineage.project_id),
        {"prompt": "build backlog"},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        outcome = _node_outcomes(session, "backlog.generate")[0]
        assert outcome.status == "failure"
        assert session.exec(select(BacklogArtifact)).all() == []


def test_trace_session_collision_records_failure_without_provider(
    engine: Engine,
) -> None:
    """Close the durable attempt when ADK rejects a duplicate trace session."""
    lineage = _seed(engine)
    calls: list[str] = []
    leaf = CountingLeafAgent(
        name="counting_backlog",
        response=_backlog_response(lineage),
        calls=calls,
    )
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=leaf,
        sessions=CollidingSessionService(),
    )

    result = runner.run(
        _decision(domain, lineage.project_id),
        {"prompt": "build backlog"},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert leaf.calls == []
    with Session(engine) as session:
        outcome = _node_outcomes(session, "backlog.generate")[0]
        assert outcome.status == "failure"
        assert outcome.failure_code == "ADK_EXECUTION_FAILED"
        assert session.exec(select(BacklogArtifact)).all() == []


def test_provider_sdk_failure_records_failure_and_returns_external_error(
    engine: Engine,
) -> None:
    """Close the durable attempt when an OpenAI-compatible SDK call fails."""
    lineage = _seed(engine)
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=ProviderSdkFailureLeafAgent(name="provider_sdk_failure"),
        sessions=TrackingSessionService(),
    )

    result = runner.run(
        _decision(domain, lineage.project_id),
        {"prompt": "build backlog"},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        outcome = _node_outcomes(session, "backlog.generate")[0]
        assert outcome.status == "failure"
        assert session.exec(select(BacklogArtifact)).all() == []


def test_output_validation_failure_records_failure_without_business_fact(
    engine: Engine,
) -> None:
    """Reject scalar leaf output and persist no downstream artifact."""
    lineage = _seed(engine)
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response="not-an-object"),
        sessions=TrackingSessionService(),
    )

    result = runner.run(
        _decision(domain, lineage.project_id),
        {"prompt": "build backlog"},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        assert session.exec(select(BacklogArtifact)).all() == []
        outcome = _node_outcomes(session, "backlog.generate")[0]
        assert outcome.status == "failure"


def test_goal_runner_persists_exact_vision_bound_goal(
    engine: Engine,
) -> None:
    """Execute one Goal interview and persist its trusted Vision-bound output."""
    observations: list[ProductGoalInterviewInput] = []
    runner, domain, project_id = _goal_runner_system(
        engine,
        _validating_goal_leaf(observations),
    )
    normalized_input = _goal_runner_input()

    result = runner.run(
        _goal_decision(domain, project_id),
        normalized_input,
    )

    assert result.ok is True
    assert len(observations) == 1
    observed = observations[0]
    assert observed.accepted_vision_statement == (
        "Deliver one verified product increment."
    )
    assert observed.user_response == "Define the next valuable Product Goal."
    with Session(engine) as session:
        attempt = session.exec(
            select(WorkflowNodeAttempt)
            .where(col(WorkflowNodeAttempt.node_id) == "goal.interview")
            .order_by(col(WorkflowNodeAttempt.workflow_node_attempt_id).desc())
        ).first()
        assert attempt is not None
        attempt_id = attempt.workflow_node_attempt_id
        assert attempt_id is not None
        outcome = session.exec(
            select(WorkflowNodeAttemptOutcome).where(
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id) == attempt_id
            )
        ).one()
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        completion_receipt = next(
            receipt
            for receipt in receipts
            if receipt.request_kind == "record_product_goal_interview_turn"
        )
        completion_request = json.loads(completion_receipt.request_json)
        attempt_input = json.loads(attempt.normalized_input_json)
        latest_goal = session.exec(
            select(ProductGoalArtifact).order_by(
                col(ProductGoalArtifact.product_goal_artifact_id).desc()
            )
        ).first()
        assert latest_goal is not None
        assert completion_request["user_text"] == normalized_input["user_response"]
        assert attempt_input == normalized_input
        assert latest_goal.goal_number == NEXT_GOAL_NUMBER
        assert latest_goal.statement == _goal_output().product_goal_statement
        assert outcome.status == "success"


def test_goal_runner_rejects_missing_vision_binding_before_leaf(
    engine: Engine,
) -> None:
    """Record failure without invoking the Goal leaf for incomplete host input."""
    observations: list[ProductGoalInterviewInput] = []
    runner, domain, project_id = _goal_runner_system(
        engine,
        _validating_goal_leaf(observations),
    )
    normalized_input = _goal_runner_input()
    normalized_input.pop("accepted_vision_statement")

    result = runner.run(
        _goal_decision(domain, project_id),
        normalized_input,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert observations == []
    with Session(engine) as session:
        assert len(session.exec(select(ProductGoalArtifact)).all()) == 1
        outcomes = session.exec(
            select(WorkflowNodeAttemptOutcome).order_by(
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_outcome_id).desc()
            )
        ).all()
        assert outcomes[0].status == "failure"


@pytest.mark.parametrize(
    "generated",
    [
        {"assessment_summary": "not a Product Goal output"},
        {
            "updated_components": {},
            "product_goal_statement": "Incomplete Goal.",
            "is_complete": True,
            "clarifying_questions": [],
        },
    ],
)
def test_goal_runner_rejects_invalid_leaf_output(
    engine: Engine,
    generated: dict[str, object],
) -> None:
    """Persist a failed attempt and no new Goal for invalid model output."""
    runner, domain, project_id = _goal_runner_system(
        engine,
        FakeLeafAgent(name="invalid_product_goal", response=generated),
    )

    result = runner.run(
        _goal_decision(domain, project_id),
        _goal_runner_input(),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        assert len(session.exec(select(ProductGoalArtifact)).all()) == 1
        outcomes = session.exec(
            select(WorkflowNodeAttemptOutcome).order_by(
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_outcome_id).desc()
            )
        ).all()
        assert outcomes[0].status == "failure"


async def _create_then_delete_old_trace(
    sessions: TrackingSessionService,
    *,
    attempt_id: int,
) -> None:
    """Delete the optional ADK trace for a crashed attempt."""
    session_id = str(attempt_id)
    await sessions.create_session(
        app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
        user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
        session_id=session_id,
    )
    await sessions.delete_session(
        app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
        user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
        session_id=session_id,
    )
    assert (
        await sessions.get_session(
            app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
            user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
            session_id=session_id,
        )
        is None
    )


def test_runner_replaces_expired_crash_attempt_after_old_trace_deletion(
    engine: Engine,
) -> None:
    """Recover at least once after expiry without relying on old ADK trace."""
    lineage = _seed(engine)
    normalized_input: JsonObject = {"prompt": "build backlog"}
    clock = MutableClock(EVALUATED_AT)
    sessions = TrackingSessionService()
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response=_backlog_response(lineage)),
        sessions=sessions,
        clock=clock,
    )
    position = domain.position(lineage.project_id)
    initial_decision = _decision(domain, lineage.project_id)
    crashed = domain.transition(
        StartNodeAttempt(
            project_id=lineage.project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=initial_decision.decision_fingerprint,
            idempotency_key="crashed-backlog-attempt",
            actor="operator@example.com",
            correlation_id="task-15-recovery",
            target_node_id=initial_decision.node_id,
            target_instance_key=initial_decision.instance_key,
            normalized_input=normalized_input,
            model_id="fake/model",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
        )
    )
    old_attempt_id = crashed.output.get("attempt_id")
    assert crashed.ok is True
    assert isinstance(old_attempt_id, int)
    waiting = _decision(domain, lineage.project_id)
    assert waiting.category is NodeCategory.WAITING
    assert waiting.valid_until == EVALUATED_AT + timedelta(seconds=LEASE_SECONDS)
    asyncio.run(_create_then_delete_old_trace(sessions, attempt_id=old_attempt_id))
    clock.now_value += timedelta(seconds=LEASE_SECONDS)

    recovery = _decision(domain, lineage.project_id)
    old_reference = next(
        item for item in recovery.fact_references if item.fact_type == "node_attempt"
    )
    result = runner.run(recovery, normalized_input)

    assert recovery.category is NodeCategory.AVAILABLE
    assert recovery.recommendation_kind is RecommendationKind.RECOVERY
    assert old_reference.fact_id == str(old_attempt_id)
    assert result.ok is True
    with Session(engine) as session:
        attempts = _node_attempts(session, "backlog.generate")
        outcomes = _node_outcomes(session, "backlog.generate")
        assert len(attempts) == EXPECTED_RECOVERY_ATTEMPT_COUNT
        replacement_id = attempts[1].workflow_node_attempt_id
        assert replacement_id is not None
        assert replacement_id != old_attempt_id
        assert {item.workflow_node_attempt_id: item.status for item in outcomes} == {
            old_attempt_id: "obsolete",
            replacement_id: "success",
        }
        assert session.exec(select(BacklogArtifact)).one() is not None


def test_authority_runner_executes_provider_once_before_completion_transaction(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist precomputed authority after one provider-free external call."""
    project_id, spec_version_id, _spec_hash = _seed_authority_compile_target(engine)
    calls: list[tuple[str, ...]] = []
    provider_inputs: list[str] = []
    observer = ReceiptObserver(engine=engine, calls=calls)
    compiler_leaf = _observing_authority_leaf(
        observer=observer,
        provider_inputs=provider_inputs,
    )
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=compiler_leaf,
            authority_repair=_unused_leaf("unused_authority_repair"),
            authority_compile_validation_repair=_unused_leaf(
                "unused_authority_compile_validation_repair"
            ),
            authority_repair_validation_repair=_unused_leaf(
                "unused_authority_repair_validation_repair"
            ),
            vision_interview=_unused_leaf("unused_vision_interview"),
            vision_repair=_unused_leaf("unused_vision_repair"),
            product_goal=_unused_leaf("unused_product_goal"),
            specification_structurer=_unused_leaf("unused_specification_structurer"),
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=authority_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=TrackingSessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/compiler",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
        ),
    )
    transition = domain.transition

    def observe_transition(request: TransitionRequest) -> TransitionResult:
        observer.events.append(f"enter:{request.kind}")
        try:
            return transition(request)
        finally:
            observer.events.append(f"exit:{request.kind}")

    monkeypatch.setattr(domain, "transition", observe_transition)

    def provider_must_not_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("completion transaction invoked the legacy compiler provider")

    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        provider_must_not_run,
    )
    decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "authority.compile"
    )
    normalized_input = AuthorityCompilationInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
        compiler_model="fake/compiler",
    )

    result = runner.run(decision, normalized_input)

    assert result.ok is True
    assert len(provider_inputs) == 1
    assert json.loads(provider_inputs[0]) == normalized_input["compiler_input"]
    assert _NON_NORMATIVE_SENTINEL not in provider_inputs[0]
    assert "GOAL.runner.review-context" not in provider_inputs[0]
    provider_payload = json.loads(provider_inputs[0])
    assert isinstance(provider_payload, dict)
    authority_input = provider_payload["authority_input"]
    assert isinstance(authority_input, dict)
    assert set(authority_input) == {
        "schema_version",
        "artifact_id",
        "normative_items",
        "normative_relations",
        "eligible_item_ids",
        "authority_input_fingerprint",
    }
    assert "review_context_ids" not in authority_input
    assert calls == [("start_node_attempt",)]
    assert observer.events == [
        "enter:start_node_attempt",
        "exit:start_node_attempt",
        "provider",
        "enter:compile_authority",
        "exit:compile_authority",
    ]
    with Session(engine) as session:
        authority = session.exec(select(CompiledSpecAuthority)).one()
        attempt = _node_attempts(session, "authority.compile")[0]
        outcome = _node_outcomes(session, "authority.compile")[0]
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        assert authority.spec_version_id == spec_version_id
        assert outcome.workflow_node_attempt_id == attempt.workflow_node_attempt_id
        assert outcome.status == "success"
        assert {receipt.request_kind for receipt in receipts} == {
            "start_node_attempt",
            "compile_authority",
        }
