"""Domain-bounded ADK workflow runner tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
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
from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from adapters.adk.recipes import (
    AdkRecipe,
    AdkRecipeRegistry,
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    build_agentic_recipe_registry,
    build_backlog_generation_workflow,
)
from adapters.adk.runner import AdkExecutionConfig, AdkRunGuards, AdkWorkflowRunner
from models.core import Project
from models.product_definition import VisionInterviewTurn
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    RepositoryInventory,
    SpecDraft,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from services.contracts.brownfield import (
    BrownfieldCurationInput,
    BrownfieldCurationOutput,
)
from services.specs import compiler_service
from services.specs.authority_selection import pending_authority_fingerprint
from services.specs.profile_content import normalize_spec_content_for_registry
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_json,
)
from utils.runtime_config import ADK_EXECUTION_TRACE_IDENTITY
from utils.spec_schemas import (
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
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
from workflow.definitions.product_definition import product_definition_graph
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.repository_inventory import (
    canonical_inventory_payload,
    inventory_binding_fingerprint,
)
from workflow.requests import (
    OpenProjectShell,
    RecordBacklogDraft,
    RecordRepositoryBaseline,
    RecordRepositoryInventory,
    StartNodeAttempt,
    TransitionRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)
LEASE_SECONDS = 60
EXPECTED_RECOVERY_ATTEMPT_COUNT = 2
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}
JSON_OBJECT = TypeAdapter(JsonObject)


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


def _seed(engine: Engine) -> tuple[int, int, str]:
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
        project = Project(name="Runner", origin="greenfield")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        spec = SpecRegistry(
            project_id=project.project_id,
            spec_hash="sha256:runner-spec",
            content='{"scope":"runner"}',
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="operator@example.com",
        )
        session.add(spec)
        session.flush()
        assert spec.spec_version_id is not None
        authority = CompiledSpecAuthority(
            spec_version_id=spec.spec_version_id,
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
                spec_version_id=spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT,
                rationale="Accepted.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority.authority_id,
                authority_fingerprint=fingerprint,
                review_fingerprint="sha256:review",
                terminal_decision_key="runner-authority",
            )
        )
        session.commit()
        return project.project_id, authority.authority_id, fingerprint


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


def _authority_artifact() -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Runner authority"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version=compiler_service.SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=compiler_service.compute_prompt_hash(
            compiler_service.SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
        ),
    )


def _seed_authority_compile_target(engine: Engine) -> tuple[int, int, str]:
    with Session(engine) as session:
        project = Project(name="Runner compile", origin="greenfield")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        spec = SpecRegistry(
            project_id=project.project_id,
            spec_hash="sha256:runner-compile-spec",
            content='{"scope":"runner compile"}',
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="operator@example.com",
        )
        session.add(spec)
        session.commit()
        assert spec.spec_version_id is not None
        return project.project_id, spec.spec_version_id, spec.spec_hash


def _unused_leaf(name: str) -> FakeLeafAgent:
    return FakeLeafAgent(name=name, response={})


def _brownfield_spec_artifact() -> TechnicalSpecArtifact:
    return TechnicalSpecArtifact.model_validate(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.brownfield.runner",
            "title": "Brownfield Initial Scope",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-08-03",
            "updated_at": "2026-08-03",
            "summary": "Initial scope curated from selected repository evidence.",
            "problem_statement": "Existing behavior needs reviewed authority.",
            "items": [
                {
                    "id": "REQ.brownfield.runner",
                    "type": "REQ",
                    "status": "proposed",
                    "title": "Preserve reviewed behavior",
                    "statement": "The system MUST preserve reviewed behavior.",
                    "level": "MUST",
                    "verification": "system-test",
                    "acceptance": ["The reviewed behavior remains available."],
                }
            ],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
            "rendering": {
                "markdown_profile": "agileforge.spec_markdown.v1",
                "rendered_markdown_sha256": None,
            },
        }
    )


def _validating_brownfield_leaf(
    observations: list[BrownfieldCurationInput],
) -> AdkWorkflow:
    """Build a provider-free leaf that consumes only pre-authority evidence."""

    @node(name="validate_brownfield_input", rerun_on_resume=True)
    async def validate_brownfield_input(
        node_input: BrownfieldCurationInput,
    ) -> BrownfieldCurationOutput:
        dumped = node_input.model_dump(mode="json")
        assert "compiled_authority" not in dumped
        assert "accepted_authority" not in dumped
        assert node_input.inventory.selected_for_model == ("README.md",)
        assert node_input.selected_evidence[0].path == "README.md"
        observations.append(node_input)
        return BrownfieldCurationOutput(canonical_spec=_brownfield_spec_artifact())

    return AdkWorkflow(
        name="fake_brownfield_curator",
        input_schema=BrownfieldCurationInput,
        output_schema=BrownfieldCurationOutput,
        edges=[(START, validate_brownfield_input)],
    )


def _brownfield_registry(
    leaf: BaseAgent | AdkWorkflow,
) -> AdkRecipeRegistry:
    return build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            brownfield_curator=leaf,
            authority_compile=_unused_leaf("unused_authority_compile"),
            authority_repair=_unused_leaf("unused_authority_repair"),
            vision_generation=_unused_leaf("unused_vision"),
            vision_interview=_unused_leaf("unused_vision_interview"),
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )


def _positioned_guards(
    domain: WorkflowDomain,
    *,
    project_id: int,
    node_id: str,
    idempotency_key: str,
) -> dict[str, object]:
    position = domain.position(project_id)
    decision = next(item for item in position.decisions if item.node_id == node_id)
    assert decision.category is NodeCategory.AVAILABLE
    return {
        "project_id": project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "instance_key": decision.instance_key,
        "idempotency_key": idempotency_key,
        "actor": "operator@example.com",
        "correlation_id": "task-15-brownfield",
    }


def _brownfield_inventory_fingerprint() -> str:
    inventory = canonical_inventory_payload(
        git_available=True,
        commit="b" * 40,
        dirty=False,
        files=(
            (".env", 8, None, "secret"),
            ("README.md", 64, f"sha256:{'c' * 64}", "hashable"),
        ),
        total_bytes=72,
    )
    return inventory_binding_fingerprint(inventory, ("README.md",))


def _seed_brownfield(domain: WorkflowDomain) -> tuple[int, int, str]:
    opened = domain.transition(
        OpenProjectShell(
            name="Runner Brownfield",
            origin="brownfield",
            idempotency_key="open-runner-brownfield",
            actor="operator@example.com",
        )
    )
    project_id = opened.output.get("project_id")
    assert opened.ok is True
    assert isinstance(project_id, int)
    baseline_fingerprint = canonical_hash(
        {
            "repository_path": "/evidence/runner-brownfield",
            "git_commit": "b" * 40,
            "dirty": False,
        }
    )
    baseline = domain.transition(
        RecordRepositoryBaseline.model_validate(
            {
                **_positioned_guards(
                    domain,
                    project_id=project_id,
                    node_id=RecordRepositoryBaseline.node_id,
                    idempotency_key="runner-brownfield-baseline",
                ),
                "repository_path": "/evidence/runner-brownfield",
                "git_commit": "b" * 40,
                "dirty": False,
                "baseline_fingerprint": baseline_fingerprint,
            }
        )
    )
    baseline_id = baseline.output.get("repository_baseline_id")
    assert baseline.ok is True
    assert isinstance(baseline_id, int)
    inventory_fingerprint = _brownfield_inventory_fingerprint()
    inventory = domain.transition(
        RecordRepositoryInventory.model_validate(
            {
                **_positioned_guards(
                    domain,
                    project_id=project_id,
                    node_id=RecordRepositoryInventory.node_id,
                    idempotency_key="runner-brownfield-inventory",
                ),
                "repository_baseline_id": baseline_id,
                "git_available": True,
                "files": [
                    {
                        "path": ".env",
                        "size_bytes": 8,
                        "sha256": None,
                        "content_status": "secret",
                    },
                    {
                        "path": "README.md",
                        "size_bytes": 64,
                        "sha256": f"sha256:{'c' * 64}",
                        "content_status": "hashable",
                    },
                ],
                "selected_for_model": ["README.md"],
                "total_bytes": 72,
                "inventory_fingerprint": inventory_fingerprint,
            }
        )
    )
    inventory_id = inventory.output.get("repository_inventory_id")
    assert inventory.ok is True
    assert isinstance(inventory_id, int)
    return project_id, inventory_id, inventory_fingerprint


def _brownfield_runner_system(
    engine: Engine,
    leaf: BaseAgent | AdkWorkflow,
) -> tuple[AdkWorkflowRunner, WorkflowDomain, int, int, str]:
    registry = _brownfield_registry(leaf)
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    project_id, inventory_id, inventory_fingerprint = _seed_brownfield(domain)
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=TrackingSessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/brownfield-curator",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
            correlation_id="task-15-brownfield",
        ),
    )
    return runner, domain, project_id, inventory_id, inventory_fingerprint


def _brownfield_runner_input(
    *,
    inventory_id: int,
    inventory_fingerprint: str,
) -> JsonObject:
    content = "# Existing product\n\nThe service preserves reviewed behavior.\n"
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "curation_input": {
            "inventory": {
                "repository_inventory_id": inventory_id,
                "repository_inventory_fingerprint": inventory_fingerprint,
                "file_count": 2,
                "total_bytes": 72,
                "selected_for_model": ["README.md"],
            },
            "selected_evidence": [
                {
                    "path": "README.md",
                    "content": content,
                    "content_sha256": f"sha256:{content_digest}",
                }
            ],
        },
        "supersedes_spec_draft_id": None,
        "provenance_path": f"repository-inventory:{inventory_id}",
    }


def _brownfield_decision(
    domain: WorkflowDomain,
    project_id: int,
) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "onboarding.brownfield.curation"
    )


def _adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordBacklogDraft:
    recipe_output = RecipeOutput.model_validate(output)
    payload = JSON_OBJECT.validate_python(recipe_output.payload)
    authority_id = payload.pop("authority_id")
    authority_fingerprint = payload.pop("authority_fingerprint")
    content = JSON_OBJECT.validate_python(payload.pop("content"))
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
        graph=product_definition_graph(),
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


def test_runner_loads_vision_input_from_persisted_attempt(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion ignores a mutated in-memory start request after persistence."""
    with Session(engine) as session:
        project = Project(name="Persisted Vision", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    vision_response: JsonObject = {
        "updated_components": {
            "project_name": "Persisted Vision",
            "target_user": "Operators",
            "problem": "State drift",
            "product_category": "Tool",
            "key_benefit": "Trust",
            "competitors": "Spreadsheets",
            "differentiator": "Durable facts",
        },
        "project_vision_statement": "A trusted workflow tool.",
        "is_complete": True,
        "clarifying_questions": [],
    }
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            brownfield_curator=_unused_leaf("unused_brownfield_curator"),
            authority_compile=_unused_leaf("unused_authority_compile"),
            authority_repair=_unused_leaf("unused_authority_repair"),
            vision_generation=_unused_leaf("unused_legacy_vision"),
            vision_interview=FakeLeafAgent(
                name="vision_interview",
                response=vision_response,
            ),
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=product_definition_graph(),
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

    def persist_then_tamper(request: TransitionRequest) -> TransitionResult:
        result = transition(request)
        if isinstance(request, StartNodeAttempt):
            request.normalized_input["user_response"] = "Tampered in memory."
        return result

    monkeypatch.setattr(domain, "transition", persist_then_tamper)
    decision = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "vision.interview"
    )

    result = runner.run(
        decision,
        {"mode": "initial", "user_response": "Trusted persisted answer."},
    )

    assert result.ok
    with Session(engine) as session:
        turn = session.exec(select(VisionInterviewTurn)).one()
        assert turn.user_text == "Trusted persisted answer."


def test_runner_executes_fake_leaf_and_commits_validated_output(engine: Engine) -> None:
    """Run a provider-free recipe and commit its fact and success outcome."""
    project_id, authority_id, authority_fingerprint = _seed(engine)
    response: JsonObject = {
        "authority_id": authority_id,
        "authority_fingerprint": authority_fingerprint,
        "content": _backlog_payload(),
    }
    sessions = TrackingSessionService()
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response=response),
        sessions=sessions,
    )

    result = runner.run(_decision(domain, project_id), {"prompt": "build backlog"})

    assert result.ok is True
    with Session(engine) as session:
        attempt = session.exec(select(WorkflowNodeAttempt)).one()
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "success"
        assert session.exec(select(BacklogArtifact)).one() is not None
        assert sessions.created_session_ids == [str(attempt.workflow_node_attempt_id)]


def test_sequential_transport_retry_replays_terminal_result_without_provider(
    engine: Engine,
) -> None:
    """Return the completed command receipt for the same transport key."""
    project_id, authority_id, authority_fingerprint = _seed(engine)
    response: JsonObject = {
        "authority_id": authority_id,
        "authority_fingerprint": authority_fingerprint,
        "content": _backlog_payload(),
    }
    calls: list[str] = []
    leaf = CountingLeafAgent(
        name="counting_backlog",
        response=response,
        calls=calls,
    )
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=leaf,
        sessions=TrackingSessionService(),
    )
    position = domain.position(project_id)
    decision = _decision(domain, project_id)
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
        assert len(session.exec(select(WorkflowNodeAttempt)).all()) == 1
        assert len(session.exec(select(WorkflowNodeAttemptOutcome)).all()) == 1


def test_concurrent_duplicate_start_never_enters_provider_twice(
    engine: Engine,
) -> None:
    """Short-circuit a replay while the live attempt is outside its transaction."""
    project_id, authority_id, authority_fingerprint = _seed(engine)
    response: JsonObject = {
        "authority_id": authority_id,
        "authority_fingerprint": authority_fingerprint,
        "content": _backlog_payload(),
    }
    calls: list[str] = []
    started = threading.Event()
    release = threading.Event()
    leaf = BlockingLeafAgent(
        name="blocking_backlog",
        response=response,
        calls=calls,
        started=started,
        release=release,
    )
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=leaf,
        sessions=TrackingSessionService(),
    )
    position = domain.position(project_id)
    decision = _decision(domain, project_id)
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
        assert len(session.exec(select(WorkflowNodeAttempt)).all()) == 1
        assert len(session.exec(select(WorkflowNodeAttemptOutcome)).all()) == 1


def test_provider_failure_records_failure_and_returns_external_error(
    engine: Engine,
) -> None:
    """Translate a fake provider failure after recording its durable outcome."""
    project_id, _authority_id, _authority_fingerprint = _seed(engine)
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=FakeLeafAgent(
            name="fake_backlog",
            response={},
            failure_message="provider unavailable",
        ),
        sessions=TrackingSessionService(),
    )

    result = runner.run(_decision(domain, project_id), {"prompt": "build backlog"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "failure"
        assert session.exec(select(BacklogArtifact)).all() == []


def test_output_validation_failure_records_failure_without_business_fact(
    engine: Engine,
) -> None:
    """Reject scalar leaf output and persist no downstream artifact."""
    project_id, _authority_id, _authority_fingerprint = _seed(engine)
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response="not-an-object"),
        sessions=TrackingSessionService(),
    )

    result = runner.run(_decision(domain, project_id), {"prompt": "build backlog"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        assert session.exec(select(BacklogArtifact)).all() == []
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "failure"


def test_brownfield_runner_persists_exact_inventory_bound_canonical_spec(
    engine: Engine,
) -> None:
    """Execute one pre-authority curator and persist its canonical draft."""
    observations: list[BrownfieldCurationInput] = []
    runner, domain, project_id, inventory_id, inventory_fingerprint = (
        _brownfield_runner_system(
            engine,
            _validating_brownfield_leaf(observations),
        )
    )
    normalized_input = _brownfield_runner_input(
        inventory_id=inventory_id,
        inventory_fingerprint=inventory_fingerprint,
    )

    result = runner.run(
        _brownfield_decision(domain, project_id),
        normalized_input,
    )

    assert result.ok is True
    assert len(observations) == 1
    observed = observations[0]
    assert observed.inventory.repository_inventory_id == inventory_id
    assert observed.inventory.repository_inventory_fingerprint == inventory_fingerprint
    with Session(engine) as session:
        inventory = session.exec(select(RepositoryInventory)).one()
        draft = session.exec(select(SpecDraft)).one()
        attempt = session.exec(select(WorkflowNodeAttempt)).one()
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        completion_receipt = next(
            receipt
            for receipt in receipts
            if receipt.request_kind == "record_brownfield_spec_draft"
        )
        completion_request = json.loads(completion_receipt.request_json)
        attempt_input = json.loads(attempt.normalized_input_json)
        normalized = normalize_spec_content_for_registry(draft.canonical_content_json)
        expected = normalize_spec_content_for_registry(
            canonical_spec_json(_brownfield_spec_artifact())
        )

        assert inventory.repository_inventory_id == inventory_id
        assert inventory.content_fingerprint == inventory_fingerprint
        assert completion_request["repository_inventory_id"] == inventory_id
        assert (
            completion_request["repository_inventory_fingerprint"]
            == inventory_fingerprint
        )
        assert (
            attempt_input["curation_input"]["inventory"][
                "repository_inventory_fingerprint"
            ]
            == inventory_fingerprint
        )
        assert normalized.content == expected.content
        assert normalized.spec_hash == expected.spec_hash
        assert outcome.status == "success"


def test_brownfield_runner_rejects_missing_inventory_binding_before_leaf(
    engine: Engine,
) -> None:
    """Record failure without invoking the curator when trusted input is incomplete."""
    observations: list[BrownfieldCurationInput] = []
    runner, domain, project_id, inventory_id, inventory_fingerprint = (
        _brownfield_runner_system(
            engine,
            _validating_brownfield_leaf(observations),
        )
    )
    normalized_input = _brownfield_runner_input(
        inventory_id=inventory_id,
        inventory_fingerprint=inventory_fingerprint,
    )
    curation_input = normalized_input["curation_input"]
    assert isinstance(curation_input, dict)
    inventory = curation_input["inventory"]
    assert isinstance(inventory, dict)
    inventory.pop("repository_inventory_fingerprint")

    result = runner.run(
        _brownfield_decision(domain, project_id),
        normalized_input,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert observations == []
    with Session(engine) as session:
        assert session.exec(select(SpecDraft)).all() == []
        assert session.exec(select(WorkflowNodeAttemptOutcome)).one().status == (
            "failure"
        )


@pytest.mark.parametrize(
    "generated",
    [
        {"assessment_summary": "post-authority As-Built output"},
        {
            "canonical_spec": {
                "schema_version": "agileforge.spec.v0",
                "title": "Noncanonical",
            }
        },
    ],
)
def test_brownfield_runner_rejects_noncanonical_leaf_output(
    engine: Engine,
    generated: dict[str, object],
) -> None:
    """Persist a failed attempt and no draft for invalid model output."""
    runner, domain, project_id, inventory_id, inventory_fingerprint = (
        _brownfield_runner_system(
            engine,
            FakeLeafAgent(name="invalid_brownfield_curator", response=generated),
        )
    )

    result = runner.run(
        _brownfield_decision(domain, project_id),
        _brownfield_runner_input(
            inventory_id=inventory_id,
            inventory_fingerprint=inventory_fingerprint,
        ),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        assert session.exec(select(SpecDraft)).all() == []
        assert session.exec(select(WorkflowNodeAttemptOutcome)).one().status == (
            "failure"
        )


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
    project_id, authority_id, authority_fingerprint = _seed(engine)
    response: JsonObject = {
        "authority_id": authority_id,
        "authority_fingerprint": authority_fingerprint,
        "content": _backlog_payload(),
    }
    normalized_input: JsonObject = {"prompt": "build backlog"}
    clock = MutableClock(EVALUATED_AT)
    sessions = TrackingSessionService()
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response=response),
        sessions=sessions,
        clock=clock,
    )
    position = domain.position(project_id)
    initial_decision = _decision(domain, project_id)
    crashed = domain.transition(
        StartNodeAttempt(
            project_id=project_id,
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
    waiting = _decision(domain, project_id)
    assert waiting.category is NodeCategory.WAITING
    assert waiting.valid_until == EVALUATED_AT + timedelta(seconds=LEASE_SECONDS)
    asyncio.run(_create_then_delete_old_trace(sessions, attempt_id=old_attempt_id))
    clock.now_value += timedelta(seconds=LEASE_SECONDS)

    recovery = _decision(domain, project_id)
    old_reference = next(
        item for item in recovery.fact_references if item.fact_type == "node_attempt"
    )
    result = runner.run(recovery, normalized_input)

    assert recovery.category is NodeCategory.AVAILABLE
    assert recovery.recommendation_kind is RecommendationKind.RECOVERY
    assert old_reference.fact_id == str(old_attempt_id)
    assert result.ok is True
    with Session(engine) as session:
        attempts = session.exec(
            select(WorkflowNodeAttempt).order_by(
                col(WorkflowNodeAttempt.workflow_node_attempt_id)
            )
        ).all()
        outcomes = session.exec(select(WorkflowNodeAttemptOutcome)).all()
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
    project_id, spec_version_id, spec_hash = _seed_authority_compile_target(engine)
    calls: list[tuple[str, ...]] = []
    observer = ReceiptObserver(engine=engine, calls=calls)
    compiler_leaf = TransactionObservingLeafAgent(
        name="observing_authority_compiler",
        response=SpecAuthorityCompilerEnvelope(result=_authority_artifact()).model_dump(
            mode="json"
        ),
        observer=observer,
    )
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            brownfield_curator=_unused_leaf("unused_brownfield_curator"),
            authority_compile=compiler_leaf,
            authority_repair=_unused_leaf("unused_authority_repair"),
            vision_generation=_unused_leaf("unused_vision"),
            vision_interview=_unused_leaf("unused_vision_interview"),
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
    normalized_input: JsonObject = {
        "spec_version_id": spec_version_id,
        "expected_spec_hash": spec_hash,
        "compiler_model": "fake/compiler",
        "compiler_input": {
            "spec_source": '{"scope":"runner compile"}',
            "spec_content_ref": None,
            "domain_hint": None,
            "project_id": project_id,
            "spec_version_id": spec_version_id,
            "spec_source_format": "agileforge.spec.v1",
        },
    }

    result = runner.run(decision, normalized_input)

    assert result.ok is True
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
        attempt = session.exec(select(WorkflowNodeAttempt)).one()
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        assert authority.spec_version_id == spec_version_id
        assert outcome.workflow_node_attempt_id == attempt.workflow_node_attempt_id
        assert outcome.status == "success"
        assert {receipt.request_kind for receipt in receipts} == {
            "start_node_attempt",
            "compile_authority",
        }
