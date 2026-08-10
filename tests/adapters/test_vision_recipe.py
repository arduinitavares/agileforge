"""Bounded ADK Vision recipe behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import TypeAdapter
from sqlmodel import Session, select

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
from models.core import Project
from models.product_definition import (
    VisionArtifact,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
)
from tests.adapters.test_adk_workflow_runner import TrackingSessionService
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, JsonObject, WorkflowErrorCode
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import RecordVisionInterviewTurn

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk import Workflow
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


def _draft(*, complete: bool = True, source_kind: str = "evidence") -> JsonObject:
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
        "draft_statement": "A trusted workflow tool.",
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
        },
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


def _registry(primary: SequenceLeaf, repair: SequenceLeaf) -> AdkRecipeRegistry:
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


def _leaf(name: str, outputs: list[object]) -> SequenceLeaf:
    return SequenceLeaf(name=name, outputs=outputs, calls=[])


def test_prompt_contract_uses_dedicated_repair_prompt() -> None:
    """Keep the repair prompt separate from the primary prompt contract."""
    prompt = Path("adapters/adk/prompts/vision.txt").read_text(encoding="utf-8")
    repair = Path("adapters/adk/prompts/vision_repair.txt")

    assert "Do not infer Vision from repository contents" not in prompt
    assert "repository evidence as unreviewed context" in prompt
    assert repair.exists()
    assert "repair" in repair.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("payload", [_bootstrap_input(), _clarification_input()])
def test_valid_bootstrap_and_clarification(payload: JsonObject) -> None:
    """Accept valid output for both supported request shapes."""
    primary = _leaf("primary", [_draft()])

    result = _run_recipe(payload, primary=primary)

    assert result.payload == _draft()
    assert len(primary.calls) <= 1


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
