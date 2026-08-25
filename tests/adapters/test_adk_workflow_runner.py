"""Domain-bounded ADK workflow runner tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from google.adk import Workflow as AdkWorkflow
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.sessions import Session as AdkSession
from google.adk.workflow import START, node
from google.genai import types
from openai import OpenAIError
from pydantic import Field, TypeAdapter
from sqlmodel import Session, col, select

from adapters.adk.agents import story as story_agents
from adapters.adk.agents.backlog import root_agent as backlog_agent
from adapters.adk.agents.roadmap import root_agent as roadmap_agent
from adapters.adk.agents.sprint import root_agent as sprint_agent
from adapters.adk.agents.story import (
    create_user_story_patch_agent,
)
from adapters.adk.agents.story import (
    root_agent as story_agent,
)
from adapters.adk.errors import VisionAgenticPreflightError
from adapters.adk.model_roles import AGENTIC_MODEL_ROLES
from adapters.adk.recipes import (
    AGENTIC_NODE_IDS,
    AdkRecipe,
    AdkRecipeRegistry,
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeInput,
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
from models.core import Project, UserStory
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    VisionInterviewTurn,
)
from models.workflow import (
    BacklogArtifact,
    StoryArtifact,
    StoryArtifactDecision,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from services.application import DeliveryActionInputService
from services.contracts.backlog import (
    BacklogAgentOutput,
    BacklogBuilderInput,
    BacklogItem,
    BacklogOutput,
)
from services.contracts.product_goal import (
    ProductGoalInterviewInput,
    ProductGoalInterviewOutput,
)
from services.contracts.roadmap import RoadmapBuilderInput, RoadmapBuilderOutput
from services.contracts.sprint import (
    SprintPlannerInput,
    SprintPlannerOutput,
    SprintPlannerStory,
)
from services.contracts.story import (
    CanonicalStoryOutput,
    UserStoryWriterInput,
    UserStoryWriterOutput,
)
from services.contracts.vision import VisionDraftOutput, VisionModelInput
from services.specs.accepted_specification import (
    require_current_accepted_specification,
)
from tests.test_create_user_story import _seed_story_parent
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from utils.runtime_config import ADK_EXECUTION_TRACE_IDENTITY
from workflow.clock import FixedClock
from workflow.contracts import (
    JsonObject,
    JsonValue,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    DecideStory,
    RecordBacklogDraft,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    StartNodeAttempt,
    TransitionRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.models.llm_request import LlmRequest
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)
LEASE_SECONDS = 60
EXPECTED_RECOVERY_ATTEMPT_COUNT = 2
NEXT_GOAL_NUMBER = 2
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}
JSON_OBJECT = TypeAdapter(JsonObject)
GOLD_SPECIFICATION_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "issue_210"
    / "gold"
    / "canonical-specification.json"
)
GOLD_SPECIFICATION_HASH = (
    "sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
)
GOLD_SPECIFICATION_ITEM_IDS = {
    "ASSUMPTION.001",
    "CONSTRAINT.001",
    "CONSTRAINT.002",
    "DATA.001",
    "DATA.002",
    "DECISION.001",
    "DECISION.002",
    "DECISION.003",
    "EXAMPLE.001",
    "GOAL.001",
    "GOAL.002",
    "INTERFACE.001",
    "INTERFACE.002",
    "NON_GOAL.001",
    "NON_GOAL.002",
    "NON_GOAL.003",
    "NON_GOAL.004",
    "OPEN_QUESTION.001",
    "QUALITY.001",
    "REQ.001",
    "REQ.002",
    "REQ.003",
    "REQ.004",
    "REQ.005",
    "REQ.006",
    "REQ.007",
    "REQ.008",
    "REQ.009",
    "REQ.010",
    "REQ.011",
    "REQ.012",
    "REQ.013",
    "REQ.014",
    "REQ.015",
    "RISK.001",
    "RISK.002",
    "RISK.003",
}


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


class SequenceLeafAgent(BaseAgent):
    """Provider-free leaf returning exact structured responses in order."""

    responses: list[object]
    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        response = self.responses[len(self.calls)]
        self.calls.append("provider")
        yield Event(author=self.name, output=response)


class SequenceStoryLlm(BaseLlm):
    """Provider-free Story model returning exact responses in order."""

    response_texts: list[str]
    request_texts: list[str] = Field(default_factory=list)
    requested_output_schemas: list[object] = Field(default_factory=list, exclude=True)

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Capture one request and return the next deterministic response."""
        del stream
        self.request_texts.append(
            "\n".join(
                part.text
                for content in llm_request.contents
                if content.parts is not None
                for part in content.parts
                if part.text is not None
            )
        )
        self.requested_output_schemas.append(llm_request.config.response_schema)
        response_text = self.response_texts[len(self.request_texts) - 1]
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=response_text)],
            )
        )


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
    spec_version_id: int
    spec_hash: str
    canonical_specification_json: str


def _gold_agent_inputs() -> tuple[dict[str, object], ...]:
    canonical_json = GOLD_SPECIFICATION_PATH.read_text(encoding="utf-8")
    root = {
        "accepted_specification_version_id": 11,
        "accepted_specification_hash": GOLD_SPECIFICATION_HASH,
        "accepted_specification_json": canonical_json,
    }
    backlog_item = BacklogItem(
        backlog_item_id="PBI-000001",
        priority=1,
        requirement="Implement the accepted calculator operation",
        spec_item_ids=("DATA.001", "REQ.001"),
        value_driver="Strategic",
        justification="It realizes the accepted first release.",
        estimated_effort="M",
    )
    story = SprintPlannerStory(
        story_id=41,
        story_item_id="US-0001",
        story_title="Implement the accepted calculator operation",
        statement=(
            "As a calculator user, I want the accepted operation, so that I can "
            "obtain the specified result."
        ),
        persona="calculator user",
        acceptance_criteria=("Verify the result against DATA.001.",),
        spec_item_ids=("DATA.001", "REQ.001"),
        story_points=3,
        rank="1.1",
    )
    return (
        {
            **root,
            "product_vision_statement": "Ship one bounded calculator release.",
            "product_goal_statement": "Deliver the accepted first release.",
            "prior_backlog_state": "NO_HISTORY",
            "user_input": None,
        },
        {
            **root,
            "backlog_items": [backlog_item.model_dump(mode="json")],
            "product_vision": "Ship one bounded calculator release.",
            "time_increment": "Milestone-based",
            "prior_roadmap_state": "NO_HISTORY",
            "user_input": "",
        },
        {
            **root,
            "parent_backlog_item_id": "PBI-000001",
            "parent_backlog_spec_item_ids": ["DATA.001", "REQ.001"],
            "roadmap_context": "Release 1",
            "user_input": None,
        },
        {
            **root,
            "available_stories": [story.model_dump(mode="json")],
            "capacity_points": 3,
            "capacity_source": "user_override",
            "capacity_basis": "Three operator-provided points.",
            "user_context": None,
        },
    )


def _invest_assessment_payload() -> JsonObject:
    return {
        "independent": {
            "result": "pass",
            "rationale": "Delivers self-contained increment.",
            "evidence": "No unbuilt dependencies.",
        },
        "negotiable": {
            "result": "pass",
            "rationale": "Implementation details open to refinement.",
            "evidence": "Focuses on user outcome.",
        },
        "valuable": {
            "result": "pass",
            "rationale": "Directly delivers user capability.",
            "evidence": "Addresses requirement.",
        },
        "estimable": {
            "result": "pass",
            "rationale": "Scope is clear and bounded.",
            "evidence": "Discrete criteria.",
        },
        "small": {
            "result": "pass",
            "rationale": "Sized for single iteration.",
            "evidence": "Effort is S.",
        },
        "testable": {
            "result": "pass",
            "rationale": "Verifiable pass/fail criteria.",
            "evidence": "Observable verification steps.",
        },
    }


def _story_provider_output(
    *,
    spec_item_ids: tuple[str, ...] = ("DATA.001", "REQ.001"),
    title: str = "Deliver the accepted operation",
) -> JsonObject:
    return {
        "user_stories": [
            {
                "story_title": title,
                "statement": (
                    "As an operator, I want the accepted operation, so that I can "
                    "obtain its specified result."
                ),
                "acceptance_criteria": ["Verify the accepted result."],
                "spec_item_ids": list(spec_item_ids),
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "S",
                "effort_rationale": "Single straightforward calculation operation.",
                "order_rationale": "First priority increment in sequence.",
                "produced_artifacts": [],
                "research_caveats": [],
                "dependency_candidates": [],
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _invalid_story_provider_output(
    *,
    spec_item_ids: tuple[str, ...] = ("DATA.001", "REQ.001"),
) -> JsonObject:
    output = _story_provider_output(spec_item_ids=spec_item_ids)
    stories = output["user_stories"]
    assert isinstance(stories, list)
    item = stories[0]
    assert isinstance(item, dict)
    item.pop("invest_assessment")
    return output


def _gold_story_recipe_payload() -> JsonObject:
    _backlog, _roadmap, story_input, _sprint = _gold_agent_inputs()
    return JSON_OBJECT.validate_python(
        {
            "writer_input": story_input,
            "source_backlog_artifact_id": 5,
            "source_backlog_artifact_fingerprint": "sha256:backlog",
            "roadmap_artifact_id": 7,
            "roadmap_artifact_fingerprint": "sha256:roadmap",
            "supersedes_story_artifact_id": None,
        }
    )


def test_live_recipe_and_model_role_catalogs_equal_graph_tuple_exactly() -> None:
    """Keep recipe and model-role order equal to the eight live graph actions."""
    expected = ROOT_GRAPH.agentic_node_ids

    assert len(expected) == 8  # noqa: PLR2004
    assert expected == AGENTIC_NODE_IDS
    assert tuple(AGENTIC_MODEL_ROLES) == expected


def test_delivery_agent_schemas_accept_the_complete_gold_root_without_aliases() -> None:
    """Validate complete gold bytes through each retained delivery agent schema."""
    agents_and_contracts = (
        (backlog_agent, BacklogBuilderInput, BacklogAgentOutput),
        (roadmap_agent, RoadmapBuilderInput, RoadmapBuilderOutput),
        (story_agent, UserStoryWriterInput, UserStoryWriterOutput),
        (create_user_story_patch_agent(), UserStoryWriterInput, UserStoryWriterOutput),
        (sprint_agent, SprintPlannerInput, SprintPlannerOutput),
    )
    backlog_input, roadmap_input, story_input, sprint_input = _gold_agent_inputs()
    inputs = (backlog_input, roadmap_input, story_input, story_input, sprint_input)
    canonical_json = GOLD_SPECIFICATION_PATH.read_text(encoding="utf-8")
    assert "sha256:" + hashlib.sha256(canonical_json.encode()).hexdigest() == (
        GOLD_SPECIFICATION_HASH
    )

    for ordinal, (agent, input_contract, output_contract) in enumerate(
        agents_and_contracts
    ):
        payload = inputs[ordinal]
        assert agent.input_schema is input_contract
        if input_contract is UserStoryWriterInput:
            assert output_contract is UserStoryWriterOutput
            assert agent.output_schema is None
            assert (
                agent.before_model_callback is story_agents.preserve_story_output_schema
            )
        else:
            assert agent.output_schema is output_contract
        parsed = input_contract.model_validate(payload)
        dumped = parsed.model_dump(mode="json")
        assert dumped["accepted_specification_json"] == canonical_json
        assert dumped["accepted_specification_hash"] == GOLD_SPECIFICATION_HASH
        assert not {"technical_spec", "invariants"} & dumped.keys()
        specification = SpecificationPayload.model_validate_json(
            dumped["accepted_specification_json"]
        )
        assert {item.id for item in specification.items} == GOLD_SPECIFICATION_ITEM_IDS
        assert "DATA.001" in GOLD_SPECIFICATION_ITEM_IDS


def test_delivery_agent_prompts_keep_direct_source_and_human_review_semantics() -> None:
    """Keep every delivery prompt on the direct source and human review boundary."""
    instructions = (
        backlog_agent.instruction,
        roadmap_agent.instruction,
        story_agent.instruction,
        create_user_story_patch_agent().instruction,
        sprint_agent.instruction,
    )

    for instruction in instructions:
        assert isinstance(instruction, str)
        assert "accepted Specification" in instruction
        assert "human reviewer" in instruction
        lowered = instruction.casefold()
        assert "technical_spec" not in lowered
        assert "invariant" not in lowered
        assert "generic gap" in lowered


def _seed(engine: Engine) -> _BacklogLineage:
    with Session(engine) as session:
        project = Project(name="Runner")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content=json.dumps(
                {
                    "schema_version": "agileforge.spec.v2",
                    "artifact_id": "SPEC.runner",
                    "title": "Runner",
                    "summary": "Exercise provider-free runner behavior.",
                    "problem_statement": "Runner output needs exact evidence.",
                    "items": [
                        {
                            "id": "REQ.runner",
                            "type": "REQ",
                            "title": "Runner execution",
                            "statement": "The runner must execute one recipe.",
                            "level": "MUST",
                            "verification": "integration-test",
                            "acceptance": ["One recipe result is durable."],
                        }
                    ],
                }
            ),
            recorded_at=EVALUATED_AT - timedelta(minutes=1),
        )
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        accepted = require_current_accepted_specification(
            session,
            project_id=project.project_id,
            spec_version_id=spec_version_id,
            spec_hash=lineage.spec.spec_hash,
        )
        session.commit()
        return _BacklogLineage(
            project_id=project.project_id,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            spec_version_id=spec_version_id,
            spec_hash=lineage.spec.spec_hash,
            canonical_specification_json=accepted.canonical_specification_json,
        )


def _backlog_payload() -> JsonObject:
    return {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Execute graph nodes through ADK",
                "spec_item_ids": ["REQ.runner"],
                "value_driver": "Strategic",
                "justification": "Keep durable facts authoritative.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _backlog_response() -> JsonObject:
    return _backlog_payload()


def _backlog_recipe_input(lineage: _BacklogLineage) -> JsonObject:
    return {
        "builder_input": {
            "accepted_specification_version_id": lineage.spec_version_id,
            "accepted_specification_hash": lineage.spec_hash,
            "accepted_specification_json": lineage.canonical_specification_json,
            "product_vision_statement": "Deliver a trusted runner.",
            "product_goal_statement": "Complete the runner Backlog.",
            "prior_backlog_state": "NO_HISTORY",
            "user_input": None,
        },
        "product_goal_artifact_id": lineage.product_goal_artifact_id,
        "product_goal_fingerprint": lineage.product_goal_fingerprint,
        "supersedes_backlog_artifact_id": None,
    }


def _unused_leaf(name: str) -> FakeLeafAgent:
    return FakeLeafAgent(name=name, response={})


def _story_registry(
    story_leaf: BaseAgent | AdkWorkflow,
    *,
    correction_leaf: BaseAgent | AdkWorkflow | None = None,
    execution_settings: JsonObject | None = None,
) -> AdkRecipeRegistry:
    settings = EXECUTION_SETTINGS if execution_settings is None else execution_settings
    return build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            vision_interview=_unused_leaf("unused_story_vision"),
            vision_repair=_unused_leaf("unused_story_vision_repair"),
            product_goal=_unused_leaf("unused_story_goal"),
            specification_structurer=_unused_leaf("unused_story_specification"),
            backlog_generation=_unused_leaf("unused_story_backlog"),
            roadmap_generation=_unused_leaf("unused_story_roadmap"),
            story_generation=story_leaf,
            story_correction=correction_leaf,
            sprint_planning=_unused_leaf("unused_story_sprint"),
        ),
        execution_settings=settings,
    )


@dataclass(frozen=True)
class _StoryRunnerSystem:
    runner: AdkWorkflowRunner
    domain: WorkflowDomain
    project_id: int
    roadmap_id: int
    payload: JsonObject


def _story_decision(domain: WorkflowDomain, project_id: int) -> NodeDecision:
    decisions = tuple(
        decision
        for decision in domain.position(project_id).decisions
        if decision.node_id == "planning.story.generate"
        and decision.category is NodeCategory.AVAILABLE
        and decision.instance_key == "backlog_item:PBI-000001"
    )
    assert len(decisions) == 1
    return decisions[0]


def _story_runner_system(
    engine: Engine,
    *,
    leaf: BaseAgent | AdkWorkflow,
    execution_settings: JsonObject | None = None,
    requirements: tuple[str, ...] = ("Plan immutable work",),
) -> _StoryRunnerSystem:
    settings = EXECUTION_SETTINGS if execution_settings is None else execution_settings
    project_id, roadmap_id = _seed_story_parent(engine, requirements=requirements)
    registry = _story_registry(leaf, execution_settings=settings)
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    decision = _story_decision(domain, project_id)
    prepared = DeliveryActionInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
        node_id="planning.story.generate",
    )
    assert isinstance(prepared, dict)
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=TrackingSessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/story",
            execution_settings=settings,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
            correlation_id="issue-214-story",
        ),
    )
    return _StoryRunnerSystem(
        runner=runner,
        domain=domain,
        project_id=project_id,
        roadmap_id=roadmap_id,
        payload=prepared,
    )


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


async def _run_provider_free_workflow(
    workflow: AdkWorkflow,
    payload: JsonObject,
    *,
    session_id: str,
) -> RecipeOutput:
    sessions = InMemorySessionService()
    await sessions.create_session(
        app_name="task6_delivery_recipes",
        user_id="task6_provider_free",
        session_id=session_id,
    )
    runner = Runner(
        app=App(
            name="task6_delivery_recipes",
            root_agent=workflow,
            resumability_config=ResumabilityConfig(is_resumable=True),
        ),
        session_service=sessions,
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text=RecipeInput(payload=payload).model_dump_json())],
    )
    output: object | None = None
    async for event in runner.run_async(
        user_id="task6_provider_free",
        session_id=session_id,
        new_message=message,
    ):
        if event.output is not None:
            output = event.output
    if output is None:
        message = "delivery recipe produced no structured output"
        raise AssertionError(message)
    return RecipeOutput.model_validate(output)


@pytest.mark.asyncio
async def test_delivery_recipe_fakes_use_production_contracts_and_canonicalizers() -> (
    None
):
    """Run all delivery recipes provider-free through their host canonicalizers."""
    backlog_input, roadmap_input, story_input, sprint_input = _gold_agent_inputs()
    roadmap_output: JsonObject = {
        "roadmap_releases": [
            {
                "release_name": "Release 1",
                "theme": "Accepted calculator operation",
                "focus_area": "User Value",
                "backlog_item_ids": ["PBI-000001"],
                "reasoning": "Deliver the exact accepted parent.",
            }
        ],
        "roadmap_summary": "One exact reviewed release.",
        "is_complete": True,
        "clarifying_questions": [],
    }
    story_output: JsonObject = {
        "user_stories": [
            {
                "story_title": "Deliver the accepted calculator operation",
                "statement": (
                    "As a calculator user, I want the accepted operation, so that "
                    "I can obtain its specified result."
                ),
                "acceptance_criteria": ["Verify the result against DATA.001."],
                "spec_item_ids": ["DATA.001", "REQ.001"],
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "S",
                "effort_rationale": "Single straightforward calculation operation.",
                "order_rationale": "First priority increment in sequence.",
                "produced_artifacts": [],
                "research_caveats": [],
                "dependency_candidates": [],
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    sprint_output: JsonObject = {
        "sprint_goal": "Deliver the accepted calculator operation.",
        "selected_stories": [
            {
                "story_id": 41,
                "story_item_id": "US-0001",
                "tasks": [
                    {
                        "description": "Implement the accepted operation.",
                        "relevant_spec_item_ids": ["DATA.001", "REQ.001"],
                        "task_kind": "implementation",
                        "artifact_targets": ["calculation service"],
                        "workstream_tags": ["backend"],
                        "checklist_items": ["Produce the specified result."],
                    }
                ],
                "reason_for_selection": "The host locked this Story.",
            }
        ],
    }
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            vision_interview=_unused_leaf("unused_vision"),
            vision_repair=_unused_leaf("unused_vision_repair"),
            product_goal=_unused_leaf("unused_goal"),
            specification_structurer=_unused_leaf("unused_specification"),
            backlog_generation=FakeLeafAgent(
                name="fake_backlog_contract",
                response={
                    "backlog_items": [
                        {
                            "priority": 1,
                            "requirement": "Deliver the accepted operation",
                            "spec_item_ids": ["DATA.001", "REQ.001"],
                            "value_driver": "Strategic",
                            "justification": "It realizes the accepted release.",
                            "estimated_effort": "M",
                            "technical_note": None,
                        }
                    ],
                    "is_complete": True,
                    "clarifying_questions": [],
                },
            ),
            roadmap_generation=FakeLeafAgent(
                name="fake_roadmap_contract",
                response=roadmap_output,
            ),
            story_generation=FakeLeafAgent(
                name="fake_story_contract",
                response=story_output,
            ),
            story_correction=_unused_leaf("unused_story_correction"),
            sprint_planning=FakeLeafAgent(
                name="fake_sprint_contract",
                response=sprint_output,
            ),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )
    envelopes: tuple[tuple[str, JsonObject], ...] = (
        (
            "backlog.generate",
            JSON_OBJECT.validate_python(
                {
                    "builder_input": backlog_input,
                    "product_goal_artifact_id": 3,
                    "product_goal_fingerprint": "sha256:goal",
                    "supersedes_backlog_artifact_id": None,
                }
            ),
        ),
        (
            "planning.roadmap.generate",
            JSON_OBJECT.validate_python(
                {
                    "builder_input": roadmap_input,
                    "backlog_artifact_id": 5,
                    "backlog_artifact_fingerprint": "sha256:backlog",
                    "supersedes_roadmap_artifact_id": None,
                }
            ),
        ),
        (
            "planning.story.generate",
            JSON_OBJECT.validate_python(
                {
                    "writer_input": story_input,
                    "source_backlog_artifact_id": 5,
                    "source_backlog_artifact_fingerprint": "sha256:backlog",
                    "roadmap_artifact_id": 7,
                    "roadmap_artifact_fingerprint": "sha256:roadmap",
                    "supersedes_story_artifact_id": None,
                }
            ),
        ),
        (
            "planning.sprint.plan",
            JSON_OBJECT.validate_python(
                {
                    "planner_input": sprint_input,
                    "capacity_points": 3,
                    "capacity_source": "user_override",
                    "capacity_basis": "Three operator-provided points.",
                    "requested_max_story_points": 3,
                    "requested_story_ids": [41],
                    "locked_story_ids": [41],
                    "team_name": "Platform",
                    "guidance": None,
                    "candidate_set_fingerprint": "sha256:candidates",
                }
            ),
        ),
    )

    results = [
        await _run_provider_free_workflow(
            registry.require(node_id).workflow,
            payload,
            session_id=str(ordinal),
        )
        for ordinal, (node_id, payload) in enumerate(envelopes, start=1)
    ]

    backlog_result = BacklogOutput.model_validate(results[0].payload)
    assert backlog_result.backlog_items[0].backlog_item_id == "PBI-000001"
    assert results[1].payload == roadmap_output
    story_items = results[2].payload["story_items"]
    assert isinstance(story_items, list)
    story_envelope = story_items[0]
    assert isinstance(story_envelope, dict)
    story_item = story_envelope["item"]
    assert isinstance(story_item, dict)
    assert story_item["story_item_id"] == "US-0001"
    assert story_envelope["item_fingerprint"]
    assert results[3].payload == sprint_output
    requests = [
        registry.require(node_id).output_adapter(
            result,
            AttemptCompletionContext(
                project_id=17,
                graph_version="agileforge.workflow.v2",
                fact_fingerprint="sha256:facts",
                decision_fingerprint=f"sha256:decision-{ordinal}",
                instance_key=(
                    "backlog_item:PBI-000001"
                    if node_id == "planning.story.generate"
                    else None
                ),
                attempt_id=ordinal,
                attempt_fingerprint=f"sha256:attempt-{ordinal}",
                idempotency_key=f"task6-delivery-{ordinal}",
                actor="operator@example.com",
                correlation_id=None,
                normalized_input=payload,
            ),
        )
        for ordinal, ((node_id, payload), result) in enumerate(
            zip(envelopes, results, strict=True),
            start=1,
        )
    ]
    assert isinstance(requests[0], RecordBacklogDraft)
    assert isinstance(requests[1], RecordRoadmapDraft)
    assert isinstance(requests[2], RecordStoryDraft)
    assert isinstance(requests[3], RecordSprintPlan)
    assert requests[0].spec_hash == GOLD_SPECIFICATION_HASH
    assert requests[2].backlog_item_id == "PBI-000001"
    assert requests[3].spec_hash == GOLD_SPECIFICATION_HASH
    for result in results:
        serialized = json.dumps(result.payload, sort_keys=True)
        assert "technical_spec" not in serialized


@pytest.mark.asyncio
async def test_production_story_recipe_preserves_valid_first_explicit_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept required explicit null on the first raw structured response."""
    model = SequenceStoryLlm(
        model="provider-free-story-valid-first",
        response_texts=[json.dumps(_story_provider_output())],
    )
    monkeypatch.setattr(
        story_agents,
        "_create_story_writer_model",
        lambda: model,
    )
    registry = _story_registry(story_agents.create_user_story_writer_agent())

    result = await _run_provider_free_workflow(
        registry.require("planning.story.generate").workflow,
        _gold_story_recipe_payload(),
        session_id="story-valid-first-explicit-null",
    )

    canonical = CanonicalStoryOutput.model_validate(result.payload)
    assert len(model.request_texts) == 1
    assert model.requested_output_schemas[0] is not None
    assert canonical.story_items[0].item.invest_assessment.independent.result == "pass"


@pytest.mark.asyncio
async def test_production_story_recipe_repairs_one_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair one provider-owned schema failure through the production recipe."""
    responses = [
        json.dumps(_invalid_story_provider_output()),
        json.dumps(_story_provider_output()),
    ]
    model = SequenceStoryLlm(
        model="provider-free-story-sequence",
        response_texts=responses,
    )
    monkeypatch.setattr(
        story_agents,
        "_create_story_writer_model",
        lambda: model,
    )
    writer = story_agents.create_user_story_writer_agent()
    registry = _story_registry(
        writer,
        correction_leaf=_unused_leaf("unused_story_correction_repair"),
    )

    result = await _run_provider_free_workflow(
        registry.require("planning.story.generate").workflow,
        _gold_story_recipe_payload(),
        session_id="story-schema-repair-production-boundary",
    )

    canonical = CanonicalStoryOutput.model_validate(result.payload)
    assert len(model.request_texts) == EXPECTED_RECOVERY_ATTEMPT_COUNT
    assert "SYSTEM_FEEDBACK" not in model.request_texts[0]
    assert "SYSTEM_FEEDBACK" in model.request_texts[1]
    assert all(GOLD_SPECIFICATION_HASH in request for request in model.request_texts)
    assert canonical.story_items[0].item.invest_assessment.independent.result == "pass"


@pytest.mark.asyncio
async def test_production_story_recipe_repairs_out_of_parent_spec_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair out-of-parent specification citations through the production recipe."""
    responses = [
        json.dumps(_story_provider_output(spec_item_ids=("DATA.001", "REQ.002"))),
        json.dumps(_story_provider_output(spec_item_ids=("DATA.001", "REQ.001"))),
    ]
    model = SequenceStoryLlm(
        model="provider-free-story-out-of-parent",
        response_texts=responses,
    )
    monkeypatch.setattr(
        story_agents,
        "_create_story_writer_model",
        lambda: model,
    )
    writer = story_agents.create_user_story_writer_agent()
    registry = _story_registry(
        writer,
        correction_leaf=_unused_leaf("unused_story_correction_repair"),
    )

    result = await _run_provider_free_workflow(
        registry.require("planning.story.generate").workflow,
        _gold_story_recipe_payload(),
        session_id="story-out-of-parent-boundary-repair",
    )

    canonical = CanonicalStoryOutput.model_validate(result.payload)
    assert len(model.request_texts) == EXPECTED_RECOVERY_ATTEMPT_COUNT
    repair_request_payload = json.loads(model.request_texts[1])
    assert "SYSTEM_FEEDBACK" not in model.request_texts[0]
    assert "SYSTEM_FEEDBACK" in model.request_texts[1]
    assert (
        "Specification item ID outside the parent boundary: REQ.002"
        in repair_request_payload["user_input"]
    )
    assert (
        'ALLOWED_PARENT_SPEC_ITEM_IDS: ["DATA.001", "REQ.001"]'
        in repair_request_payload["user_input"]
    )
    assert all(GOLD_SPECIFICATION_HASH in request for request in model.request_texts)
    assert canonical.story_items[0].item.spec_item_ids == ("DATA.001", "REQ.001")
    assert canonical.story_items[0].item.invest_assessment.independent.result == "pass"


@pytest.mark.asyncio
async def test_story_correction_recipe_repairs_then_merges_and_remints_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair the patch response, then merge one complete ordinary Story draft."""
    _backlog, _roadmap, story_input, _sprint = _gold_agent_inputs()

    def item(title: str, statement: str) -> JsonObject:
        return {
            "story_title": title,
            "statement": statement,
            "acceptance_criteria": [f"Verify {title}."],
            "spec_item_ids": ["DATA.001", "REQ.001"],
            "invest_assessment": _invest_assessment_payload(),
            "estimated_effort": "S",
            "effort_rationale": "Single straightforward calculation operation.",
            "order_rationale": f"Sequential step for {title}.",
            "produced_artifacts": [],
            "research_caveats": [],
            "dependency_candidates": [],
        }

    source_items = [
        item("First", "As a calculator user, I want first, so that it works."),
        item("Middle", "As a calculator user, I want middle, so that it works."),
        item("Last", "As a calculator user, I want last, so that it works."),
    ]
    regular_leaf = CountingLeafAgent(
        name="regular_story_leaf",
        response={
            "user_stories": source_items,
            "is_complete": True,
            "clarifying_questions": [],
        },
        calls=[],
    )
    corrected_item = item(
        "Corrected middle",
        "As a calculator user, I want corrected middle, so that it works.",
    )
    invalid_corrected_item = dict(corrected_item)
    invalid_corrected_item.pop("invest_assessment")
    correction_model = SequenceStoryLlm(
        model="provider-free-story-correction",
        response_texts=[
            json.dumps(
                {
                    "user_stories": [invalid_corrected_item],
                    "is_complete": True,
                    "clarifying_questions": [],
                }
            ),
            json.dumps(
                {
                    "user_stories": [corrected_item],
                    "is_complete": True,
                    "clarifying_questions": [],
                }
            ),
        ],
    )
    monkeypatch.setattr(
        story_agents,
        "_create_story_writer_model",
        lambda: correction_model,
    )
    correction_leaf = story_agents.create_user_story_patch_agent()
    registry = _story_registry(
        regular_leaf,
        correction_leaf=correction_leaf,
    )
    workflow = registry.require("planning.story.generate").workflow
    base_payload = JSON_OBJECT.validate_python(
        {
            "writer_input": story_input,
            "source_backlog_artifact_id": 5,
            "source_backlog_artifact_fingerprint": "sha256:backlog",
            "roadmap_artifact_id": 7,
            "roadmap_artifact_fingerprint": "sha256:roadmap",
            "supersedes_story_artifact_id": None,
        }
    )
    source = await _run_provider_free_workflow(
        workflow,
        base_payload,
        session_id="story-correction-source",
    )
    source_story_items = source.payload["story_items"]
    assert isinstance(source_story_items, list)
    middle = source_story_items[1]
    assert isinstance(middle, dict)
    middle_item = middle["item"]
    assert isinstance(middle_item, dict)
    correction_payload = JSON_OBJECT.validate_python(
        {
            **base_payload,
            "writer_input": {
                **story_input,
                "user_input": (
                    "Selected accepted Story:\n"
                    + json.dumps(middle_item, ensure_ascii=False)
                    + "\nHuman guidance:\nCorrect the middle only."
                ),
            },
            "supersedes_story_artifact_id": 91,
            "correction": {
                "story_id": 42,
                "guidance": "Correct the middle only.",
                "source_story_artifact_id": 91,
                "source_story_artifact_fingerprint": canonical_hash(source.payload),
                "source_story_item_id": "US-0002",
                "source_story_item_fingerprint": middle["item_fingerprint"],
            },
            "correction_source": source.payload,
        }
    )
    corrected = await _run_provider_free_workflow(
        workflow,
        correction_payload,
        session_id="story-correction-target",
    )

    corrected_content = CanonicalStoryOutput.model_validate(corrected.payload)
    source_content = CanonicalStoryOutput.model_validate(source.payload)
    corrected_items = corrected_content.story_items
    assert regular_leaf.calls == ["provider"]
    assert len(correction_model.request_texts) == EXPECTED_RECOVERY_ATTEMPT_COUNT
    assert "SYSTEM_FEEDBACK" not in correction_model.request_texts[0]
    assert "SYSTEM_FEEDBACK" in correction_model.request_texts[1]
    assert "Return exactly one user_stories item" in correction_model.request_texts[1]
    assert [entry.item.story_item_id for entry in corrected_items] == [
        "US-0001",
        "US-0002",
        "US-0003",
    ]
    assert corrected_items[0] == source_content.story_items[0]
    assert corrected_items[2] == source_content.story_items[2]
    assert corrected_items[1].item.story_title == "Corrected middle"
    assert (
        corrected_items[1].item_fingerprint
        != source_content.story_items[1].item_fingerprint
    )
    request = registry.require("planning.story.generate").output_adapter(
        corrected,
        AttemptCompletionContext(
            project_id=17,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            instance_key="backlog_item:PBI-000001",
            attempt_id=1,
            attempt_fingerprint="sha256:attempt",
            idempotency_key="story-correction",
            actor="operator@example.com",
            correlation_id=None,
            normalized_input=correction_payload,
        ),
    )
    assert isinstance(request, RecordStoryDraft)
    assert request.canonical_content == corrected.payload


@pytest.mark.asyncio
async def test_story_correction_recipe_repairs_out_of_parent_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair out-of-parent citation on patch response then merge valid item."""
    _backlog, _roadmap, story_input, _sprint = _gold_agent_inputs()

    def item(
        title: str,
        statement: str,
        spec_item_ids: tuple[str, ...] = ("DATA.001", "REQ.001"),
    ) -> JsonObject:
        return {
            "story_title": title,
            "statement": statement,
            "acceptance_criteria": [f"Verify {title}."],
            "spec_item_ids": list(spec_item_ids),
            "invest_assessment": _invest_assessment_payload(),
            "estimated_effort": "S",
            "effort_rationale": "Single straightforward calculation operation.",
            "order_rationale": f"Sequential step for {title}.",
            "produced_artifacts": [],
            "research_caveats": [],
            "dependency_candidates": [],
        }

    source_items = [
        item("First", "As a calculator user, I want first, so that it works."),
        item("Middle", "As a calculator user, I want middle, so that it works."),
        item("Last", "As a calculator user, I want last, so that it works."),
    ]
    regular_leaf = CountingLeafAgent(
        name="regular_story_leaf",
        response={
            "user_stories": source_items,
            "is_complete": True,
            "clarifying_questions": [],
        },
        calls=[],
    )
    invalid_out_of_parent_item = item(
        "Corrected middle",
        "As a calculator user, I want corrected middle, so that it works.",
        spec_item_ids=("DATA.001", "REQ.002"),
    )
    valid_corrected_item = item(
        "Corrected middle",
        "As a calculator user, I want corrected middle, so that it works.",
        spec_item_ids=("DATA.001", "REQ.001"),
    )
    correction_model = SequenceStoryLlm(
        model="provider-free-story-correction-out-of-parent",
        response_texts=[
            json.dumps(
                {
                    "user_stories": [invalid_out_of_parent_item],
                    "is_complete": True,
                    "clarifying_questions": [],
                }
            ),
            json.dumps(
                {
                    "user_stories": [valid_corrected_item],
                    "is_complete": True,
                    "clarifying_questions": [],
                }
            ),
        ],
    )
    monkeypatch.setattr(
        story_agents,
        "_create_story_writer_model",
        lambda: correction_model,
    )
    correction_leaf = story_agents.create_user_story_patch_agent()
    registry = _story_registry(
        regular_leaf,
        correction_leaf=correction_leaf,
    )
    workflow = registry.require("planning.story.generate").workflow
    base_payload = JSON_OBJECT.validate_python(
        {
            "writer_input": story_input,
            "source_backlog_artifact_id": 5,
            "source_backlog_artifact_fingerprint": "sha256:backlog",
            "roadmap_artifact_id": 7,
            "roadmap_artifact_fingerprint": "sha256:roadmap",
            "supersedes_story_artifact_id": None,
        }
    )
    source = await _run_provider_free_workflow(
        workflow,
        base_payload,
        session_id="story-correction-out-of-parent-source",
    )
    source_story_items = source.payload["story_items"]
    assert isinstance(source_story_items, list)
    middle = source_story_items[1]
    assert isinstance(middle, dict)
    middle_item = middle["item"]
    assert isinstance(middle_item, dict)

    correction_payload = JSON_OBJECT.validate_python(
        {
            **base_payload,
            "writer_input": {
                **story_input,
                "user_input": (
                    "Selected accepted Story:\n"
                    + json.dumps(middle_item, ensure_ascii=False)
                    + "\nHuman guidance:\nCorrect the middle only."
                ),
            },
            "supersedes_story_artifact_id": 91,
            "correction": {
                "story_id": 42,
                "guidance": "Correct the middle only.",
                "source_story_artifact_id": 91,
                "source_story_artifact_fingerprint": canonical_hash(source.payload),
                "source_story_item_id": "US-0002",
                "source_story_item_fingerprint": middle["item_fingerprint"],
            },
            "correction_source": source.payload,
        }
    )
    corrected = await _run_provider_free_workflow(
        workflow,
        correction_payload,
        session_id="story-correction-out-of-parent-target",
    )

    corrected_content = CanonicalStoryOutput.model_validate(corrected.payload)
    source_content = CanonicalStoryOutput.model_validate(source.payload)
    corrected_items = corrected_content.story_items
    assert regular_leaf.calls == ["provider"]
    repair_request_payload = json.loads(correction_model.request_texts[1])
    assert "SYSTEM_FEEDBACK" not in correction_model.request_texts[0]
    assert "SYSTEM_FEEDBACK" in correction_model.request_texts[1]
    assert (
        "Specification item ID outside the parent boundary: REQ.002"
        in repair_request_payload["user_input"]
    )
    assert (
        'ALLOWED_PARENT_SPEC_ITEM_IDS: ["DATA.001", "REQ.001"]'
        in repair_request_payload["user_input"]
    )
    assert [entry.item.story_item_id for entry in corrected_items] == [
        "US-0001",
        "US-0002",
        "US-0003",
    ]
    assert corrected_items[0] == source_content.story_items[0]
    assert corrected_items[2] == source_content.story_items[2]
    assert corrected_items[1].item.story_title == "Corrected middle"
    assert corrected_items[1].item.spec_item_ids == ("DATA.001", "REQ.001")
    assert (
        corrected_items[1].item_fingerprint
        != source_content.story_items[1].item_fingerprint
    )
    request = registry.require("planning.story.generate").output_adapter(
        corrected,
        AttemptCompletionContext(
            project_id=17,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            instance_key="backlog_item:PBI-000001",
            attempt_id=1,
            attempt_fingerprint="sha256:attempt",
            idempotency_key="story-correction",
            actor="operator@example.com",
            correlation_id=None,
            normalized_input=correction_payload,
        ),
    )
    assert isinstance(request, RecordStoryDraft)
    assert request.canonical_content == corrected.payload


def test_story_runner_valid_first_persists_one_draft_and_replays_without_provider(
    engine: Engine,
) -> None:
    """Persist one exact Story candidate while leaving human review untouched."""
    leaf = CountingLeafAgent(
        name="valid_first_story",
        response=_story_provider_output(spec_item_ids=("REQ.planning-1",)),
        calls=[],
    )
    system = _story_runner_system(
        engine,
        leaf=leaf,
    )
    position = system.domain.position(system.project_id)
    decision = _story_decision(system.domain, system.project_id)
    guards = AdkRunGuards(
        position=position,
        idempotency_key="issue-214-story-replay",
        actor="operator@example.com",
        correlation_id="issue-214-story-replay",
    )

    first = system.runner.run(decision, system.payload, guards=guards)
    replay = system.runner.run(decision, system.payload, guards=guards)

    assert first.ok is True
    assert replay == first.model_copy(update={"replayed": True})
    assert leaf.calls == ["provider"]
    writer_input = system.payload["writer_input"]
    assert isinstance(writer_input, dict)
    with Session(engine) as session:
        artifacts = session.exec(select(StoryArtifact)).all()
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert (
            artifact.source_backlog_artifact_id
            == system.payload["source_backlog_artifact_id"]
        )
        assert (
            artifact.source_backlog_artifact_fingerprint
            == system.payload["source_backlog_artifact_fingerprint"]
        )
        assert artifact.roadmap_artifact_id == system.roadmap_id
        assert (
            artifact.roadmap_artifact_fingerprint
            == system.payload["roadmap_artifact_fingerprint"]
        )
        assert artifact.backlog_item_id == writer_input["parent_backlog_item_id"]
        persisted = json.loads(artifact.canonical_content_json)
        assert "invest_assessment" in persisted["story_items"][0]["item"]
        assert session.exec(select(StoryArtifactDecision)).all() == []
        assert session.exec(select(UserStory)).all() == []
        attempts = _node_attempts(session, "planning.story.generate")
        outcomes = _node_outcomes(session, "planning.story.generate")
        assert len(attempts) == 1
        assert len(outcomes) == 1
        assert attempts[0].instance_key == "backlog_item:PBI-000001"
        assert attempts[0].fact_fingerprint == position.fact_fingerprint
        assert attempts[0].decision_fingerprint == decision.decision_fingerprint
        assert json.loads(attempts[0].normalized_input_json) == system.payload
        assert outcomes[0].status == "success"


def _issue_222_story_item(
    *,
    title: str,
    effort: str,
    effort_rationale: str,
    order_rationale: str,
    dependency_candidates: list[JsonValue],
) -> JsonObject:
    """Build one literal Story item for the precise-feedback regression."""
    return {
        "story_title": title,
        "statement": (
            f"As an operator, I want {title}, so that its outcome is available."
        ),
        "acceptance_criteria": [f"Verify {title}."],
        "spec_item_ids": ["REQ.planning-1"],
        "invest_assessment": _invest_assessment_payload(),
        "estimated_effort": effort,
        "effort_rationale": effort_rationale,
        "order_rationale": order_rationale,
        "produced_artifacts": [],
        "research_caveats": [],
        "dependency_candidates": dependency_candidates,
    }


def _assert_issue_222_successor(
    engine: Engine,
    *,
    source_artifact_id: int,
    feedback_text: str,
) -> None:
    """Assert exact successor content, lineage, and pre-acceptance isolation."""
    with Session(engine) as session:
        artifacts = session.exec(
            select(StoryArtifact).order_by(col(StoryArtifact.story_artifact_id))
        ).all()
        assert len(artifacts) == 2  # noqa: PLR2004
        assert [artifact.version_number for artifact in artifacts] == [1, 2]
        replacement = artifacts[1]
        assert replacement.supersedes_story_artifact_id == source_artifact_id
        replacement_content = CanonicalStoryOutput.model_validate_json(
            replacement.canonical_content_json
        )
        replacement_items = replacement_content.story_items
        assert [item.item.story_title for item in replacement_items] == [
            "Story A",
            "Story B",
        ]
        assert [item.item.estimated_effort for item in replacement_items] == [
            "S",
            "M",
        ]
        assert all(
            not item.item.dependency_candidates for item in replacement_items
        )
        decisions = session.exec(select(StoryArtifactDecision)).all()
        assert len(decisions) == 1
        assert decisions[0].story_artifact_id == source_artifact_id
        assert decisions[0].decision == "feedback"
        assert decisions[0].rationale == feedback_text
        assert session.exec(select(UserStory)).all() == []


def test_story_runner_precise_feedback_creates_superseding_exact_candidate(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry exact feedback through generation into one reviewable successor."""
    source_output: JsonObject = {
        "user_stories": [
            _issue_222_story_item(
                title="Story B",
                effort="M",
                effort_rationale="Moderate second-outcome work.",
                order_rationale="Initially proposed before Story A.",
                dependency_candidates=[],
            ),
            _issue_222_story_item(
                title="Story A",
                effort="M",
                effort_rationale="Moderate first-outcome work.",
                order_rationale="Initially proposed after Story B.",
                dependency_candidates=[
                    {
                        "prerequisite_ref": "dependency X",
                        "reason": "The initial proposal expected dependency X.",
                        "confidence": "explicit",
                    }
                ],
            ),
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    revised_output: JsonObject = {
        "user_stories": [
            _issue_222_story_item(
                title="Story A",
                effort="S",
                effort_rationale="Reduced to S after the requested refinement.",
                order_rationale="Moved before Story B as requested.",
                dependency_candidates=[],
            ),
            _issue_222_story_item(
                title="Story B",
                effort="M",
                effort_rationale="Moderate second-outcome work.",
                order_rationale="Moved after Story A as requested.",
                dependency_candidates=[],
            ),
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    model = SequenceStoryLlm(
        model="provider-free-precise-story-feedback",
        response_texts=[
            json.dumps(source_output),
            json.dumps(revised_output),
        ],
    )
    monkeypatch.setattr(story_agents, "_create_story_writer_model", lambda: model)
    system = _story_runner_system(
        engine,
        leaf=story_agents.create_user_story_writer_agent(),
    )

    first_position = system.domain.position(system.project_id)
    first_decision = _story_decision(system.domain, system.project_id)
    first = system.runner.run(
        first_decision,
        system.payload,
        guards=AdkRunGuards(
            position=first_position,
            idempotency_key="issue-222-story-source",
            actor="operator@example.com",
            correlation_id="issue-222-story-source",
        ),
    )
    assert first.ok is True
    with Session(engine) as session:
        source_artifact = session.exec(select(StoryArtifact)).one()
        source_artifact_id = int(source_artifact.story_artifact_id or 0)
        source_fingerprint = source_artifact.content_fingerprint
        source_content_json = source_artifact.canonical_content_json

    feedback_text = (
        "Change Story A effort to S, move Story A before Story B, and remove "
        "dependency X."
    )
    review_position = system.domain.position(system.project_id)
    review_decision = next(
        decision
        for decision in review_position.decisions
        if decision.node_id == "planning.story.review"
        and decision.instance_key == first_decision.instance_key
    )
    feedback = system.domain.transition(
        DecideStory(
            project_id=system.project_id,
            graph_version=review_position.graph_version,
            fact_fingerprint=review_position.fact_fingerprint,
            decision_fingerprint=review_decision.decision_fingerprint,
            instance_key=review_decision.instance_key,
            idempotency_key="issue-222-story-feedback",
            actor="operator@example.com",
            correlation_id="issue-222-story-feedback",
            backlog_item_id="PBI-000001",
            story_artifact_id=source_artifact_id,
            artifact_fingerprint=source_fingerprint,
            decision="feedback",
            rationale=feedback_text,
        )
    )
    assert feedback.ok is True

    successor_position = system.domain.position(system.project_id)
    successor_decision = _story_decision(system.domain, system.project_id)
    successor_payload = DeliveryActionInputService(engine=engine).build(
        project_id=system.project_id,
        decision=successor_decision,
        node_id="planning.story.generate",
    )
    assert isinstance(successor_payload, dict)
    assert successor_payload["supersedes_story_artifact_id"] == source_artifact_id
    writer_input = successor_payload["writer_input"]
    assert isinstance(writer_input, dict)
    persisted_feedback = writer_input["user_input"]
    assert isinstance(persisted_feedback, str)
    assert source_content_json in persisted_feedback
    assert "Review outcome: feedback" in persisted_feedback
    assert f"Review rationale: {feedback_text}" in persisted_feedback

    successor = system.runner.run(
        successor_decision,
        successor_payload,
        guards=AdkRunGuards(
            position=successor_position,
            idempotency_key="issue-222-story-successor",
            actor="operator@example.com",
            correlation_id="issue-222-story-successor",
        ),
    )
    assert successor.ok is True
    assert len(model.request_texts) == 2  # noqa: PLR2004
    provider_successor_input = json.loads(model.request_texts[1])
    assert provider_successor_input["user_input"] == persisted_feedback

    _assert_issue_222_successor(
        engine,
        source_artifact_id=source_artifact_id,
        feedback_text=feedback_text,
    )


def test_story_runner_concurrent_duplicate_never_enters_provider_twice(
    engine: Engine,
) -> None:
    """Bind one live Story attempt and replay a concurrent duplicate start."""
    started = threading.Event()
    release = threading.Event()
    leaf = BlockingLeafAgent(
        name="blocking_story",
        response=_story_provider_output(spec_item_ids=("REQ.planning-1",)),
        calls=[],
        started=started,
        release=release,
    )
    system = _story_runner_system(
        engine,
        leaf=leaf,
    )
    position = system.domain.position(system.project_id)
    decision = _story_decision(system.domain, system.project_id)
    guards = AdkRunGuards(
        position=position,
        idempotency_key="issue-214-story-concurrent",
        actor="operator@example.com",
        correlation_id="issue-214-story-concurrent",
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            system.runner.run,
            decision,
            system.payload,
            guards=guards,
        )
        assert started.wait(timeout=5)
        try:
            duplicate = system.runner.run(
                decision,
                system.payload,
                guards=guards,
            )
            assert duplicate.ok is True
            assert duplicate.replayed is True
            assert leaf.calls == ["provider"]
        finally:
            release.set()
        first = first_future.result(timeout=5)

    assert first.ok is True
    with Session(engine) as session:
        assert len(_node_attempts(session, "planning.story.generate")) == 1
        assert len(_node_outcomes(session, "planning.story.generate")) == 1
        assert len(session.exec(select(StoryArtifact)).all()) == 1
        assert session.exec(select(StoryArtifactDecision)).all() == []
        assert session.exec(select(UserStory)).all() == []


def test_story_runner_double_schema_failure_has_zero_partial_persistence(
    engine: Engine,
) -> None:
    """Stop after two invalid responses and retain one durable failure only."""
    invalid = _invalid_story_provider_output(spec_item_ids=("REQ.planning-1",))
    leaf = SequenceLeafAgent(
        name="double_invalid_story",
        responses=[invalid, invalid],
        calls=[],
    )
    system = _story_runner_system(
        engine,
        leaf=leaf,
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 3},
    )

    result = system.runner.run(
        _story_decision(system.domain, system.project_id),
        system.payload,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert leaf.calls == ["provider", "provider"]
    with Session(engine) as session:
        attempts = _node_attempts(session, "planning.story.generate")
        outcomes = _node_outcomes(session, "planning.story.generate")
        assert len(attempts) == 1
        assert len(outcomes) == 1
        assert outcomes[0].status == "failure"
        assert outcomes[0].failure_message is not None
        assert "INITIAL_VALIDATION_ERRORS" in outcomes[0].failure_message
        assert "REPAIR_VALIDATION_ERRORS" in outcomes[0].failure_message
        assert session.exec(select(StoryArtifact)).all() == []
        assert session.exec(select(StoryArtifactDecision)).all() == []
        assert session.exec(select(UserStory)).all() == []


def test_story_runner_double_reference_failure_has_zero_partial_persistence(
    engine: Engine,
) -> None:
    """Stop after two out-of-parent citation responses and retain one failure only."""
    out_of_parent = _story_provider_output(
        spec_item_ids=("REQ.planning-1", "REQ.planning-2")
    )
    leaf = SequenceLeafAgent(
        name="double_out_of_parent_story",
        responses=[out_of_parent, out_of_parent],
        calls=[],
    )
    system = _story_runner_system(
        engine,
        leaf=leaf,
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 3},
        requirements=("Plan immutable work", "Plan second requirement"),
    )

    result = system.runner.run(
        _story_decision(system.domain, system.project_id),
        system.payload,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert leaf.calls == ["provider", "provider"]
    with Session(engine) as session:
        attempts = _node_attempts(session, "planning.story.generate")
        outcomes = _node_outcomes(session, "planning.story.generate")
        assert len(attempts) == 1
        assert len(outcomes) == 1
        assert outcomes[0].status == "failure"
        assert outcomes[0].failure_message is not None
        assert "INITIAL_VALIDATION_ERRORS" in outcomes[0].failure_message
        assert "REPAIR_VALIDATION_ERRORS" in outcomes[0].failure_message
        assert (
            "Specification item ID outside the parent boundary"
            in outcomes[0].failure_message
        )
        assert session.exec(select(StoryArtifact)).all() == []
        assert session.exec(select(StoryArtifactDecision)).all() == []
        assert session.exec(select(UserStory)).all() == []


def _attempt_16_sanitized_responses(
    *,
    valid_spec_id: str = "REQ.planning-1",
    numeric_spelling_spec_id: str = "REQ.planning-2",
    unbounded_spec_id: str = "REQ.planning-3",
) -> tuple[JsonObject, JsonObject]:
    """Return sanitized 3-Story response payloads captured during Attempt 16."""
    response_1: JsonObject = {
        "user_stories": [
            {
                "story_title": "Expose the public Python calculator operation",
                "statement": (
                    "As a software developer, I want to call the calculator through "
                    "the public `string_calculator.add` operation, so that I can "
                    "calculate supported Number Lists without depending on internal "
                    "implementation details."
                ),
                "acceptance_criteria": [
                    (
                        "`from string_calculator import add` succeeds and exposes an "
                        "operation accepting one string argument and returning an "
                        "integer for successful supported input, as required by "
                        "REQ.python-public-add."
                    ),
                    (
                        "Calling `add(\"\")` returns integer `0`, as required by "
                        "REQ.empty-input-zero."
                    ),
                    (
                        "The public operation is provided as the direct calculation "
                        "seam, consistent with DECISION.one-public-calculation-seam."
                    ),
                    (
                        "The project targets Python 3.13 or newer, as required by "
                        "CONSTRAINT.python-runtime."
                    ),
                ],
                "spec_item_ids": [valid_spec_id],
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "S",
                "effort_rationale": "Small public package interface definition.",
                "order_rationale": "Entrypoint definition for calculator.",
                "produced_artifacts": [
                    "Public Python package interface exposing "
                    "`add(numbers: str) -> int`"
                ],
                "research_caveats": [],
                "dependency_candidates": [],
            },
            {
                "story_title": "Accept the supported Number List language",
                "statement": (
                    "As a software developer, I want the calculator to accept the "
                    "defined Integer Token and Delimiter syntax, so that supported "
                    "comma-separated and line-feed-separated inputs can be processed "
                    "consistently."
                ),
                "acceptance_criteria": [
                    (
                        "An empty string is accepted as a Number List, consistent "
                        "with REQ.number-list-language."
                    ),
                    (
                        "ASCII decimal digits with an optional leading minus sign are "
                        "recognized as Integer Tokens, while whitespace and a leading "
                        "plus sign are not part of an Integer Token, as required by "
                        "REQ.integer-token-language."
                    ),
                    (
                        "One or more valid Integer Tokens separated by exactly one "
                        "comma or one actual line-feed are accepted, and comma and "
                        "actual line-feed Delimiters may be mixed, as required by "
                        "REQ.number-list-language."
                    ),
                    (
                        "A valid Number List is not rejected because it exceeds an "
                        "arbitrary product-level token-count limit, as required by "
                        "REQ.number-list-language and "
                        "CONSTRAINT.standard-library-preference."
                    ),
                    (
                        "The implementation remains within the bounded "
                        "calculator scope and does not add later or unrelated "
                        "capabilities, as constrained by NON_GOAL.later-capabilities."
                    ),
                ],
                "spec_item_ids": [valid_spec_id],
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "M",
                "effort_rationale": "Moderate complexity delimiter and token parsing.",
                "order_rationale": "Grammar support required before summation.",
                "produced_artifacts": ["Supported Number List parsing behavior"],
                "research_caveats": [],
                "dependency_candidates": [],
            },
            {
                "story_title": "Sum supported non-negative Number Lists",
                "statement": (
                    "As a software developer, I want supported non-negative Number "
                    "Lists to be summed according to their numeric values, so that I "
                    "can obtain predictable arithmetic results from the calculator."
                ),
                "acceptance_criteria": [
                    (
                        "A single non-negative Integer Token returns its "
                        "numeric value, as required by REQ.sum-nonnegative-values."
                    ),
                    (
                        "Comma-separated, actual-line-feed-separated, and "
                        "mixed-delimiter non-negative Number Lists return the "
                        "arithmetic sum of all parsed Integer Tokens, as required by "
                        "REQ.sum-nonnegative-values."
                    ),
                    (
                        "Leading zeros do not change an Integer Token's "
                        "numeric meaning, as required by REQ.numeric-spelling."
                    ),
                    (
                        "Negative-zero spellings are treated as numeric zero and "
                        "contribute zero rather than changing the result, as required "
                        "by REQ.numeric-spelling."
                    ),
                    (
                        "The operation supports the complete bounded Number List "
                        "language without imposing an arbitrary product-level "
                        "token-count ceiling, as required by REQ.number-list-language."
                    ),
                ],
                "spec_item_ids": [valid_spec_id, numeric_spelling_spec_id],
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "M",
                "effort_rationale": "Arithmetic reduction across parsed tokens.",
                "order_rationale": "Core calculation built on grammar parser.",
                "produced_artifacts": [
                    "Public calculation behavior for supported "
                    "non-negative Number Lists"
                ],
                "research_caveats": [],
                "dependency_candidates": [],
            },
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    response_2: JsonObject = {
        "user_stories": [
            {
                "story_title": "Accept supported Number Lists",
                "statement": (
                    "As a software developer, I want the calculator to recognize "
                    "the supported Number List language, so that valid integer lists "
                    "can be processed consistently."
                ),
                "acceptance_criteria": [
                    "An empty string is accepted as a Number List.",
                    (
                        "Valid Integer Tokens consist of ASCII decimal digits with an "
                        "optional leading minus sign, without whitespace or a leading "
                        "plus sign."
                    ),
                    "Valid Integer Tokens separated by exactly one comma are accepted.",
                    (
                        "Valid Integer Tokens separated by exactly one actual "
                        "line-feed are accepted."
                    ),
                    (
                        "Comma and actual line-feed Delimiters may coexist in one "
                        "Number List."
                    ),
                    (
                        "No arbitrary product-level maximum number of Integer "
                        "Tokens is imposed."
                    ),
                    (
                        "Custom or later calculator capabilities are not promised "
                        "as part of this release."
                    ),
                ],
                "spec_item_ids": [valid_spec_id],
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "M",
                "effort_rationale": "Moderate complexity token parsing.",
                "order_rationale": "Input grammar recognition.",
                "produced_artifacts": ["Supported Number List parsing behavior"],
                "research_caveats": [],
                "dependency_candidates": [],
            },
            {
                "story_title": "Calculate sums for supported input",
                "statement": (
                    "As a software developer, I want the calculator to return the "
                    "arithmetic sum of a supported Number List, so that I can obtain a "
                    "predictable result from valid input."
                ),
                "acceptance_criteria": [
                    (
                        "The public operation returns integer zero for an empty "
                        "Number List."
                    ),
                    "One non-negative Integer Token returns its value.",
                    "Comma-separated non-negative values return their arithmetic sum.",
                    (
                        "Actual-line-feed-separated non-negative values return their "
                        "arithmetic sum."
                    ),
                    (
                        "Mixed comma- and actual-line-feed-separated non-negative "
                        "values return their arithmetic sum."
                    ),
                    (
                        "A valid Number List is accepted without an arbitrary "
                        "product-level maximum token count."
                    ),
                ],
                "spec_item_ids": [valid_spec_id, unbounded_spec_id],
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "S",
                "effort_rationale": "Straightforward summation logic.",
                "order_rationale": "Arithmetic computation layer.",
                "produced_artifacts": [
                    "Supported non-negative Number List summation behavior"
                ],
                "research_caveats": [],
                "dependency_candidates": [],
            },
            {
                "story_title": "Expose the public Python calculator operation",
                "statement": (
                    "As a software developer, I want a small public Python operation "
                    "for supported calculator input, so that I can use the calculator "
                    "without depending on internal implementation details."
                ),
                "acceptance_criteria": [
                    "`from string_calculator import add` succeeds.",
                    (
                        "The public `add` operation has the signature "
                        "`add(numbers: str) -> int`."
                    ),
                    (
                        "The operation accepts one string argument and returns an "
                        "integer for successful supported input."
                    ),
                    "The implementation targets Python 3.13 or newer.",
                    (
                        "The implementation prefers the Python standard library unless "
                        "a concrete unmet requirement justifies a runtime dependency."
                    ),
                ],
                "spec_item_ids": [valid_spec_id],
                "invest_assessment": _invest_assessment_payload(),
                "estimated_effort": "S",
                "effort_rationale": "Public package wrapper and export.",
                "order_rationale": "Public interface presentation.",
                "produced_artifacts": [
                    "Public `string_calculator.add` Python interface"
                ],
                "research_caveats": [],
                "dependency_candidates": [],
            },
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    return response_1, response_2


def test_story_runner_attempt_16_reproduction_fails_closed_without_partial_persistence(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce Attempt 16 with 3-Story responses and fail-closed persistence.

    Demonstrates that two responses with out-of-parent citations trigger bounded
    repair with the complete exact allow-list, terminate after 2 attempts, and
    fail closed with zero partial persistence.
    """
    response_1, response_2 = _attempt_16_sanitized_responses()
    model = SequenceStoryLlm(
        model="provider-free-attempt-16",
        response_texts=[json.dumps(response_1), json.dumps(response_2)],
    )
    monkeypatch.setattr(
        story_agents,
        "_create_story_writer_model",
        lambda: model,
    )
    writer = story_agents.create_user_story_writer_agent()
    system = _story_runner_system(
        engine,
        leaf=writer,
        requirements=(
            "Plan immutable work",
            "numeric spelling",
            "unbounded token count",
        ),
    )

    result = system.runner.run(
        _story_decision(system.domain, system.project_id),
        system.payload,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert len(model.request_texts) == EXPECTED_RECOVERY_ATTEMPT_COUNT
    assert "SYSTEM_FEEDBACK" not in model.request_texts[0]

    repair_payload = json.loads(model.request_texts[1])
    assert repair_payload.get("user_input") is not None
    repair_user_input = repair_payload["user_input"]
    assert (
        "SYSTEM_FEEDBACK: Your previous User Story response failed schema or "
        "reference validation." in repair_user_input
    )
    assert (
        "ERROR: Specification item ID outside the parent boundary: "
        "REQ.planning-2" in repair_user_input
    )
    assert (
        'VALIDATION_ERRORS: ["Specification item ID outside the parent boundary: '
        'REQ.planning-2"]' in repair_user_input
    )
    assert 'ALLOWED_PARENT_SPEC_ITEM_IDS: ["REQ.planning-1"]' in repair_user_input
    assert (
        "Every user story spec_item_ids list must contain non-empty IDs selected "
        "strictly from ALLOWED_PARENT_SPEC_ITEM_IDS." in repair_user_input
    )
    assert (
        "Return JSON only. Match UserStoryWriterOutput exactly. Required fields "
        "are user_stories, is_complete, and clarifying_questions. "
        "Do not add wrapper fields." in repair_user_input
    )

    with Session(engine) as session:
        attempts = _node_attempts(session, "planning.story.generate")
        outcomes = _node_outcomes(session, "planning.story.generate")
        assert len(attempts) == 1
        assert len(outcomes) == 1
        assert outcomes[0].status == "failure"
        assert outcomes[0].failure_message is not None
        assert (
            "Story schema repair failed after two provider responses."
            in outcomes[0].failure_message
        )
        assert "INITIAL_VALIDATION_ERRORS" in outcomes[0].failure_message
        assert "REQ.planning-2" in outcomes[0].failure_message
        assert "REPAIR_VALIDATION_ERRORS" in outcomes[0].failure_message
        assert "REQ.planning-3" in outcomes[0].failure_message
        assert session.exec(select(StoryArtifact)).all() == []
        assert session.exec(select(StoryArtifactDecision)).all() == []
        assert session.exec(select(UserStory)).all() == []


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
    envelope = JSON_OBJECT.validate_python(context.normalized_input)
    builder_input = BacklogBuilderInput.model_validate(envelope["builder_input"])
    product_goal_artifact_id = envelope["product_goal_artifact_id"]
    product_goal_fingerprint = envelope["product_goal_fingerprint"]
    assert isinstance(product_goal_artifact_id, int)
    assert isinstance(product_goal_fingerprint, str)
    content = BacklogOutput.model_validate(recipe_output.payload).model_dump(
        mode="json"
    )
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
        spec_version_id=builder_input.accepted_specification_version_id,
        spec_hash=builder_input.accepted_specification_hash,
        product_goal_artifact_id=product_goal_artifact_id,
        product_goal_fingerprint=product_goal_fingerprint,
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
        leaf=FakeLeafAgent(name="fake_backlog", response=_backlog_response()),
        sessions=sessions,
    )

    result = runner.run(
        _decision(domain, lineage.project_id),
        _backlog_recipe_input(lineage),
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
        response=_backlog_response(),
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
        _backlog_recipe_input(lineage),
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
        response=_backlog_response(),
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

    first = runner.run(decision, _backlog_recipe_input(lineage), guards=guards)
    replay = runner.run(decision, _backlog_recipe_input(lineage), guards=guards)

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
        response=_backlog_response(),
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
    normalized_input: JsonObject = _backlog_recipe_input(lineage)

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
        _backlog_recipe_input(lineage),
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
        response=_backlog_response(),
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
        _backlog_recipe_input(lineage),
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
        _backlog_recipe_input(lineage),
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
        _backlog_recipe_input(lineage),
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
    normalized_input: JsonObject = _backlog_recipe_input(lineage)
    clock = MutableClock(EVALUATED_AT)
    sessions = TrackingSessionService()
    runner, domain = _build_runner(
        engine,
        project_id=lineage.project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response=_backlog_response()),
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
