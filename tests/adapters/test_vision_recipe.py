"""Bounded ADK Vision recipe behavior."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from git import Repo
from google.adk.agents import Agent, BaseAgent, InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field, TypeAdapter
from sqlmodel import Session, col, select

from adapters.adk.recipes import (
    AdkRecipeRegistry,
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeInput,
    RecipeOutput,
    _vision_interview_output_adapter,
    build_agentic_recipe_registry,
    build_vision_workflow,
)
from adapters.adk.runner import AdkExecutionConfig, AdkRunGuards, AdkWorkflowRunner
from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.product_definition import (
    VisionArtifact,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from services.contracts.vision import (
    VisionAgentInput,
    VisionDraftOutput,
    VisionModelInput,
)
from services.vision_input import VisionInputService
from tests.adapters.test_adk_workflow_runner import TrackingSessionService
from tests.services.test_vision_evidence import _bind_repository
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, JsonObject, WorkflowErrorCode
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    BeginVisionRevision,
    DecideVisionReview,
    RecordVisionInterviewTurn,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from google.adk import Workflow
    from google.adk.models.llm_request import LlmRequest
    from sqlalchemy.engine import Engine

EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 3}
NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
TRUSTED_SNAPSHOT_ID = 4
_JSON_OBJECT = TypeAdapter(JsonObject)


class SequenceLeaf(BaseAgent):
    """Provider-free leaf returning deterministic outputs in order."""

    outputs: list[object]
    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        output = self.outputs[len(self.calls)]
        self.calls.append(self.name)
        yield Event(author=self.name, output=output)


class CapturingLlm(BaseLlm):
    """Provider-free model that records the exact ADK request."""

    output: JsonObject
    request_texts: list[str] = Field(default_factory=list)

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Return one deterministic response after recording provider input."""
        del stream
        self.request_texts.append(_provider_request_text(llm_request))
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=json.dumps(self.output))],
            )
        )


def _evidence(name: str = "Vision") -> JsonObject:
    content: JsonObject = {"name": name, "description": None}
    item: JsonObject = {
        "evidence_id": "project:metadata",
        "kind": "project_metadata",
        "relative_path": None,
        "content_fingerprint": canonical_hash(content),
        "trust": "operator_provided",
        "content": content,
        "truncated": False,
    }
    return _JSON_OBJECT.validate_python({
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [item],
        "warnings": [],
        "evidence_fingerprint": canonical_hash(
            {
                "schema_version": "agileforge.vision-evidence.v1",
                "items": [item],
                "warnings": [],
            }
        ),
    })


def _components(*, complete: bool = True) -> JsonObject:
    return _JSON_OBJECT.validate_python({
        "project_name": "Vision",
        "target_user": "Operators" if complete else None,
        "problem": "State drift",
        "product_category": "Tool",
        "key_benefit": "Trust",
        "competitors": "Spreadsheets",
        "differentiator": "Facts",
    })


def _draft(
    *,
    complete: bool = True,
    source_kind: str = "evidence",
    statement: str = "A trusted workflow tool.",
) -> JsonObject:
    components = _components(complete=complete)
    basis: list[JsonObject] = []
    for name, value in components.items():
        if value is None:
            continue
        basis.append(
            {
                "component": name,
                "source_kinds": [source_kind],
                "evidence_ids": (
                    ["project:metadata"] if source_kind == "evidence" else []
                ),
                "assumption_ids": ["assumption:one"]
                if source_kind == "inference"
                else [],
            }
        )
    return _JSON_OBJECT.validate_python({
        "schema_version": "agileforge.vision-draft.v1",
        "components": components,
        "component_basis": basis,
        "draft_statement": statement,
        "assumptions": (
            [
                {
                    "assumption_id": "assumption:one",
                    "text": "Operators need durable workflow facts.",
                    "affected_components": ["target_user"],
                }
            ]
            if source_kind == "inference"
            else []
        ),
        "conflicts": [],
        "clarifying_questions": []
        if complete
        else [
            {
                "question_id": "question:target",
                "text": "Who uses it?",
                "affected_components": ["target_user"],
                "conflict_ids": [],
            }
        ],
        "is_complete": complete,
    })


def _bootstrap_input(*, evidence: JsonObject | None = None) -> JsonObject:
    return _JSON_OBJECT.validate_python({
        "request": {
            "schema_version": "agileforge.vision-input.v1",
            "operation": "bootstrap",
            "project_name": "Vision",
            "project_description": None,
            "evidence": _evidence() if evidence is None else evidence,
        },
        "preflight": None,
    })


def _clarification_input(
    *,
    stored_evidence: JsonObject | None = None,
    observed_evidence: JsonObject | None = None,
) -> JsonObject:
    """Build a clarification envelope with explicit stored and observed evidence."""
    evidence = _evidence() if stored_evidence is None else stored_evidence
    observed = evidence if observed_evidence is None else observed_evidence
    return _JSON_OBJECT.validate_python({
        "request": {
            "schema_version": "agileforge.vision-input.v1",
            "operation": "clarification",
            "project_name": "Vision",
            "project_description": None,
            "vision_evidence_snapshot_id": TRUSTED_SNAPSHOT_ID,
            "evidence": evidence,
            "current_components": _components(complete=False),
            "current_statement": "A draft.",
            "current_component_basis": [],
            "current_assumptions": [],
            "current_conflicts": [],
            "current_questions": [
                {
                    "question_id": "question:target",
                    "text": "Who uses it?",
                    "affected_components": ["target_user"],
                    "conflict_ids": [],
                }
            ],
            "human_response": "Operators use it.",
            "addressed_question_ids": ["question:target"],
        },
        "preflight": {
            "expected_evidence_fingerprint": evidence["evidence_fingerprint"],
            "observed_evidence": observed,
        }
    })


async def _run_recipe_async(
    payload: JsonObject,
    *,
    primary: SequenceLeaf,
    repair: SequenceLeaf | None = None,
) -> RecipeOutput:
    workflow = build_vision_workflow(
        primary_leaf=primary,
        repair_leaf=repair,
        execution_settings=EXECUTION_SETTINGS,
    )
    return await _run_workflow_async(workflow, payload)


async def _run_workflow_async(
    workflow: Workflow,
    payload: JsonObject,
) -> RecipeOutput:
    """Execute one provider-free Vision recipe workflow."""
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="vision_recipe_test",
        user_id="vision_recipe_user",
        session_id="1",
    )
    app = App(
        name="vision_recipe_test",
        root_agent=workflow,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    runner = Runner(app=app, session_service=session_service)
    message = types.Content(
        role="user",
        parts=[types.Part(text=RecipeInput(payload=payload).model_dump_json())],
    )
    output: object | None = None
    async for event in runner.run_async(
        user_id="vision_recipe_user",
        session_id="1",
        new_message=message,
    ):
        if event.output is not None:
            output = event.output
    if output is None:
        message = "recipe produced no output"
        raise AssertionError(message)
    return RecipeOutput.model_validate(output)


def _run_recipe(
    payload: JsonObject,
    *,
    primary: SequenceLeaf,
    repair: SequenceLeaf | None = None,
) -> RecipeOutput:
    return asyncio.run(_run_recipe_async(payload, primary=primary, repair=repair))


def _registry(primary: BaseAgent, repair: SequenceLeaf) -> AdkRecipeRegistry:
    """Build the production recipe catalog with deterministic Vision leaves."""
    unused = _leaf("unused", [{}])
    return build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=unused,
            authority_repair=unused,
            vision_interview=primary,
            vision_repair=repair,
            product_goal=unused,
            backlog_generation=unused,
            roadmap_generation=unused,
            story_generation=unused,
            sprint_planning=unused,
        ),
        execution_settings=EXECUTION_SETTINGS,
    )


def _leaf(
    name: str,
    outputs: Sequence[object],
    *,
    input_schema: type[BaseModel] | None = None,
) -> SequenceLeaf:
    return SequenceLeaf(
        name=name,
        outputs=list(outputs),
        calls=[],
        input_schema=input_schema,
    )


def _provider_leaf(
    name: str,
    output: JsonObject,
) -> Agent:
    """Build an actual LLM node with a provider-free model."""
    return Agent(
        name=name,
        model=CapturingLlm(model="capturing-vision", output=output),
        input_schema=VisionModelInput,
        output_schema=VisionDraftOutput,
        instruction="Return the supplied deterministic Vision draft.",
        mode="single_turn",
    )


def _provider_request_text(request: LlmRequest) -> str:
    return "\n".join(
        part.text
        for content in request.contents
        if content.parts is not None
        for part in content.parts
        if part.text is not None
    )


def test_prompt_contract_uses_dedicated_repair_prompt() -> None:
    """Keep the repair prompt separate from the primary prompt contract."""
    prompt = Path("adapters/adk/prompts/vision.txt").read_text(encoding="utf-8")
    repair = Path("adapters/adk/prompts/vision_repair.txt")

    assert "repository evidence as unreviewed context" in prompt
    assert repair.exists()
    assert "repair" in repair.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("payload", [_bootstrap_input(), _clarification_input()])
def test_schema_bearing_leaf_accepts_bootstrap_and_clarification(
    payload: JsonObject,
) -> None:
    """Reach a strict provider boundary for both supported request shapes."""
    primary = _provider_leaf("primary", _draft())

    workflow = build_vision_workflow(
        primary_leaf=primary,
        execution_settings=EXECUTION_SETTINGS,
    )
    result = asyncio.run(_run_workflow_async(workflow, payload))

    assert result.payload == _draft()
    assert isinstance(primary.model, CapturingLlm)
    assert len(primary.model.request_texts) == 1
    request_text = primary.model.request_texts[0]
    assert '"request"' in request_text
    assert '"preflight"' not in request_text
    assert '"host"' not in request_text


@pytest.mark.parametrize("complete", [False, True])
def test_incomplete_and_complete_outputs(complete: bool) -> None:
    """Preserve complete and incomplete model decisions without extra calls."""
    primary = _leaf("primary", [_draft(complete=complete)])

    result = _run_recipe(_bootstrap_input(), primary=primary)

    assert result.payload["is_complete"] is complete
    assert len(primary.calls) <= 1


def test_semantic_invalid_then_one_repair_succeeds() -> None:
    """Repair one semantically invalid primary result exactly once."""
    primary = _leaf("primary", [_draft(source_kind="human")])
    repair = _leaf("repair", [_draft()])

    result = _run_recipe(_bootstrap_input(), primary=primary, repair=repair)

    assert result.payload == _draft()
    assert len(primary.calls) == 1
    assert len(repair.calls) == 1


def test_registry_wires_the_dedicated_vision_repair_leaf() -> None:
    """Production recipes retain the one-repair semantic recovery boundary."""
    primary = _leaf("primary", [_draft(source_kind="human")])
    repair = _leaf("repair", [_draft()])
    registry = _registry(primary, repair)

    workflow = registry.require("vision.bootstrap").workflow
    result = asyncio.run(
        _run_workflow_async(workflow, _bootstrap_input())
    )

    assert result.payload == _draft()
    assert len(primary.calls) == 1
    assert len(repair.calls) == 1


def test_semantic_invalid_then_one_repair_fails() -> None:
    """Propagate a second semantic failure without a third provider call."""
    primary = _leaf("primary", [_draft(source_kind="human")])
    repair = _leaf("repair", [_draft(source_kind="human")])

    with pytest.raises(Exception, match="human basis"):
        _run_recipe(_bootstrap_input(), primary=primary, repair=repair)

    assert len(primary.calls) == 1
    assert len(repair.calls) == 1


def test_preflight_failure_zero_leaf_calls() -> None:
    """Reject stale evidence before either Vision provider leaf runs."""
    trusted = _evidence("Trusted")
    changed = _evidence("Changed")
    payload = _clarification_input(
        stored_evidence=trusted,
        observed_evidence=changed,
    )
    primary = _leaf("primary", [_draft()])
    repair = _leaf("repair", [_draft()])

    with pytest.raises(Exception, match=r"Vision evidence|Root node"):
        _run_recipe(payload, primary=primary, repair=repair)

    assert len(primary.calls) == 0
    assert len(repair.calls) == 0


def _prepare_recipe_revision(
    domain: WorkflowDomain,
    runner: AdkWorkflowRunner,
    service: VisionInputService,
    project_id: int,
) -> None:
    """Persist one accepted Vision and open its first revision intent."""
    position = domain.position(project_id)
    bootstrap = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )
    initial = runner.run(
        bootstrap,
        service.build_bootstrap(project_id, bootstrap),
        guards=AdkRunGuards(
            position=position,
            idempotency_key="stale-revision-initial",
            actor="operator@example.com",
        ),
    )
    assert initial.ok
    artifact_id = initial.output["vision_artifact_id"]
    fingerprint = initial.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    position = domain.position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "vision.review"
    )
    accepted = domain.transition(
        DecideVisionReview(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key="stale-revision-accept",
            actor="operator@example.com",
            vision_artifact_id=artifact_id,
            vision_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept for revision.",
        )
    )
    assert accepted.ok
    position = domain.position(project_id)
    revision = next(
        item for item in position.decisions if item.node_id == "vision.revision.start"
    )
    opened = domain.transition(
        BeginVisionRevision(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=revision.decision_fingerprint,
            idempotency_key="stale-revision-open",
            actor="operator@example.com",
            source_vision_artifact_id=artifact_id,
            source_vision_fingerprint=fingerprint,
            reason="Re-evaluate the direction.",
        )
    )
    assert opened.ok


def _stale_recipe_runtime(
    engine: Engine,
    lineage: Literal["initial", "revision"],
) -> tuple[int, SequenceLeaf, WorkflowDomain, AdkWorkflowRunner, VisionInputService]:
    """Build one provider-free runtime at the stale-preflight boundary."""
    project = Project(name=f"Stale {lineage} Vision", description="Original")
    with Session(engine) as session:
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    outputs = (
        [_draft(complete=False), _draft(complete=False)]
        if lineage == "initial"
        else [
            _draft(),
            _draft(complete=False),
            _draft(statement="A revised trusted workflow tool."),
        ]
    )
    primary = _leaf("primary", outputs, input_schema=VisionModelInput)
    registry = _registry(primary, _leaf("repair", [_draft()]))
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=NOW),
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
            lease_seconds=60,
            actor="operator@example.com",
        ),
    )
    service = VisionInputService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )
    if lineage == "revision":
        _prepare_recipe_revision(domain, runner, service, project_id)
    return project_id, primary, domain, runner, service


@pytest.mark.parametrize("lineage", ["initial", "revision"])
def test_stale_preflight_persists_explicit_replacement_lineage(
    engine: Engine,
    lineage: Literal["initial", "revision"],
) -> None:
    """Recover stale initial and revision drafts through a replacement snapshot."""
    project_id, primary, domain, runner, service = _stale_recipe_runtime(
        engine,
        lineage,
    )

    position = domain.position(project_id)
    bootstrap = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )
    seeded = runner.run(
        bootstrap,
        service.build_bootstrap(project_id, bootstrap),
        guards=AdkRunGuards(
            position=position,
            idempotency_key=f"stale-{lineage}-seed",
            actor="operator@example.com",
        ),
    )
    assert seeded.ok
    with Session(engine) as session:
        stale_snapshot = session.exec(
            select(VisionEvidenceSnapshot).order_by(
                col(VisionEvidenceSnapshot.vision_evidence_snapshot_id)
            )
        ).all()[-1]
        assert stale_snapshot.vision_evidence_snapshot_id is not None
        stale_snapshot_id = stale_snapshot.vision_evidence_snapshot_id
        stored_project = session.get(Project, project_id)
        assert stored_project is not None
        stored_project.description = "Changed"
        session.add(stored_project)
        session.commit()

    position = domain.position(project_id)
    interview = next(
        item for item in position.decisions if item.node_id == "vision.interview"
    )
    stale_payload = service.build_clarification(
        project_id,
        interview,
        "Keep the intended direction.",
    )
    calls_before_stale = len(primary.calls)
    stale = runner.run(
        interview,
        stale_payload,
        guards=AdkRunGuards(
            position=position,
            idempotency_key=f"stale-{lineage}-preflight",
            actor="operator@example.com",
        ),
    )

    assert stale.ok is False
    assert stale.error is not None
    assert stale.error.code is WorkflowErrorCode.VISION_EVIDENCE_STALE
    assert len(primary.calls) == calls_before_stale
    recovery_position = domain.position(project_id)
    recovery = next(
        item
        for item in recovery_position.decisions
        if item.node_id == "vision.bootstrap"
    )
    assert recovery.reason_code == "VISION_EVIDENCE_STALE"

    recovered = runner.run(
        recovery,
        service.build_bootstrap(project_id, recovery),
        guards=AdkRunGuards(
            position=recovery_position,
            idempotency_key=f"stale-{lineage}-recovery",
            actor="operator@example.com",
        ),
    )

    assert recovered.ok
    with Session(engine) as session:
        snapshots = session.exec(
            select(VisionEvidenceSnapshot).order_by(
                col(VisionEvidenceSnapshot.vision_evidence_snapshot_id)
            )
        ).all()
        replacement = snapshots[-1]
        assert session.get(VisionEvidenceSnapshot, stale_snapshot_id) is not None
        assert replacement.supersedes_vision_evidence_snapshot_id == stale_snapshot_id
    final_position = domain.position(project_id)
    if lineage == "initial":
        assert "vision.interview" in final_position.available_nodes
    else:
        assert "vision.review" in final_position.waiting_nodes
    assert "vision.bootstrap" not in final_position.available_nodes
    if lineage == "revision":
        with Session(engine) as session:
            for turn in session.exec(select(VisionInterviewTurn)).all():
                if turn.revision_intent_id is not None:
                    turn.revision_intent_id = None
                    session.add(turn)
            session.flush()
            for intent in session.exec(select(VisionRevisionIntent)).all():
                session.delete(intent)
            session.commit()


def test_repository_bootstrap_persists_exact_binding_without_model_exposure(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Bind the collected repository identity only in trusted host metadata."""
    repository = tmp_path / "binding-repository"
    repository.mkdir()
    with Repo.init(repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Binding Test")
            config.set_value("user", "email", "binding@example.com")
        (repository / "README.md").write_text("# Binding evidence\n", encoding="utf-8")
        repo.index.add(["README.md"])
        repo.index.commit("binding evidence")
    project = Project(name="Repository binding Vision")
    with Session(engine) as session:
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    _bind_repository(engine, project_id=project_id, repository=repository)
    with Session(engine) as session:
        stored_project = session.get(Project, project_id)
        assert stored_project is not None
        binding_id = stored_project.active_repository_binding_id
        assert isinstance(binding_id, int)
    primary = _provider_leaf("primary", _draft())
    repair = _leaf("repair", [_draft()])
    registry = _registry(primary, repair)
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=NOW),
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
            lease_seconds=60,
            actor="operator@example.com",
        ),
    )
    service = VisionInputService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )
    position = domain.position(project_id)
    bootstrap = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )
    payload = service.build_bootstrap(project_id, bootstrap)

    result = runner.run(
        bootstrap,
        payload,
        guards=AdkRunGuards(
            position=position,
            idempotency_key="repository-binding-bootstrap",
            actor="operator@example.com",
        ),
    )

    assert result.ok
    envelope = VisionAgentInput.model_validate(payload)
    assert getattr(envelope.host, "repository_binding_id", None) == binding_id
    assert isinstance(primary.model, CapturingLlm)
    assert len(primary.model.request_texts) == 1
    assert "repository_binding_id" not in primary.model.request_texts[0]
    with Session(engine) as session:
        snapshot = session.exec(select(VisionEvidenceSnapshot)).one()
        assert snapshot.repository_binding_id == binding_id


def test_binding_switch_after_input_build_blocks_provider_execution(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Make a positioned Vision request stale when active repository selection moves."""
    repositories: list[Path] = []
    for name in ("binding-a", "binding-b"):
        repository = tmp_path / name
        repository.mkdir()
        with Repo.init(repository) as repo:
            with repo.config_writer() as config:
                config.set_value("user", "name", "Binding Race Test")
                config.set_value("user", "email", "binding-race@example.com")
            (repository / "README.md").write_text(
                f"# {name}\n",
                encoding="utf-8",
            )
            repo.index.add(["README.md"])
            repo.index.commit(f"{name} evidence")
        repositories.append(repository)
    project = Project(name="Repository binding race")
    with Session(engine) as session:
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    first_binding_id = _bind_repository(
        engine,
        project_id=project_id,
        repository=repositories[0],
    )
    second_binding_id = _bind_repository(
        engine,
        project_id=project_id,
        repository=repositories[1],
        activate=False,
    )
    primary = _provider_leaf("primary", _draft())
    registry = _registry(primary, _leaf("repair", [_draft()]))
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=NOW),
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
            lease_seconds=60,
            actor="operator@example.com",
        ),
    )
    service = VisionInputService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )
    position = domain.position(project_id)
    bootstrap = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )
    payload = service.build_bootstrap(project_id, bootstrap)
    envelope = VisionAgentInput.model_validate(payload)
    assert getattr(envelope.host, "repository_binding_id", None) == first_binding_id
    with Session(engine) as session:
        stored_project = session.get(Project, project_id)
        assert stored_project is not None
        stored_project.active_repository_binding_id = second_binding_id
        session.add(stored_project)
        session.commit()

    result = runner.run(
        bootstrap,
        payload,
        guards=AdkRunGuards(
            position=position,
            idempotency_key="repository-binding-race",
            actor="operator@example.com",
        ),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert isinstance(primary.model, CapturingLlm)
    assert primary.model.request_texts == []


def test_output_adapter_binds_only_trusted_attempt_input() -> None:
    """Bind clarification provenance from persisted host input, not model output."""
    output = RecipeOutput(payload=_draft())
    completion = _vision_interview_output_adapter(
        output,
        AttemptCompletionContext(
            project_id=1,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            instance_key=None,
            attempt_id=2,
            attempt_fingerprint="sha256:attempt",
            idempotency_key="vision:complete",
            actor="operator@example.com",
            correlation_id=None,
            normalized_input=_clarification_input(),
        ),
    )

    assert isinstance(completion, RecordVisionInterviewTurn)
    assert completion.vision_evidence_snapshot_id == TRUSTED_SNAPSHOT_ID
    assert completion.addressed_question_ids == ("question:target",)
    assert completion.user_text == "Operators use it."


def test_schema_failure_records_no_vision_facts(engine: Engine) -> None:
    """Reject malformed model output without persisting Vision business facts."""
    project = Project(name="Vision recipe schema failure")
    with Session(engine) as session:
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    failing = _leaf("failing_vision", ["not an object"])
    unused = _leaf("unused", [{}])
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=unused,
            authority_repair=unused,
            vision_interview=failing,
            vision_repair=unused,
            product_goal=unused,
            backlog_generation=unused,
            roadmap_generation=unused,
            story_generation=unused,
            sprint_planning=unused,
        ),
        execution_settings=EXECUTION_SETTINGS,
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=NOW),
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
            lease_seconds=60,
            actor="operator@example.com",
        ),
    )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "vision.bootstrap"
    )

    result = runner.run(
        decision,
        _bootstrap_input(),
        guards=AdkRunGuards(
            position=position,
            idempotency_key="schema-failure",
            actor="operator@example.com",
        ),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert len(failing.calls) <= 1
    with Session(engine) as session:
        assert session.exec(select(VisionEvidenceSnapshot)).all() == []
        assert session.exec(select(VisionInterviewTurn)).all() == []
        assert session.exec(select(VisionArtifact)).all() == []
