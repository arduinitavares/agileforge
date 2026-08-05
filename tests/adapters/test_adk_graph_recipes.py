"""ADK 2 graph recipe boundary tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.events import Event
from google.adk.workflow import START

from adapters.adk.prompts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from adapters.adk.recipes import (
    AdkRecipe,
    AdkRecipeRegistry,
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    UnknownAdkRecipeError,
    build_agentic_recipe_registry,
    build_backlog_generation_workflow,
)
from services.contracts.brownfield import BrownfieldCurationOutput
from utils.agileforge_spec_profile import TechnicalSpecArtifact
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.definitions.root import ROOT_GRAPH
from workflow.requests import (
    CompileAuthority,
    RecordBacklogDraft,
    RecordBrownfieldSpecDraft,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RecordVisionDraft,
    RepairAuthority,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from workflow.contracts import JsonObject
    from workflow.requests.base import PositionedRequest

RECIPE_TIMEOUT_SECONDS = 7.0
RECIPE_MAX_ATTEMPTS = 2
COMPLETION_CONTEXT = AttemptCompletionContext(
    project_id=17,
    graph_version="agileforge.workflow.v1",
    fact_fingerprint="sha256:facts",
    decision_fingerprint="sha256:decision",
    instance_key=None,
    attempt_id=23,
    attempt_fingerprint="sha256:attempt",
    idempotency_key="complete-agentic-node",
    actor="operator@example.com",
    correlation_id="task-15-review",
    normalized_input={},
)


def _compiled_authority_payload() -> JsonObject:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Task 15"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    ).model_dump(mode="json")


def _brownfield_spec_payload() -> JsonObject:
    return TechnicalSpecArtifact.model_validate(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.brownfield.recipe",
            "title": "Brownfield Initial Scope",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-08-03",
            "updated_at": "2026-08-03",
            "summary": "Initial scope curated from repository evidence.",
            "problem_statement": "Existing behavior needs reviewed authority.",
            "items": [],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
            "rendering": {
                "markdown_profile": "agileforge.spec_markdown.v1",
                "rendered_markdown_sha256": None,
            },
        }
    ).model_dump(mode="json", by_alias=True)


REQUEST_CASES: tuple[
    tuple[str, type[PositionedRequest], JsonObject],
    ...,
] = (
    (
        "onboarding.brownfield.curation",
        RecordBrownfieldSpecDraft,
        {
            "repository_inventory_id": 2,
            "repository_inventory_fingerprint": f"sha256:{'b' * 64}",
            "canonical_content": _brownfield_spec_payload(),
            "supersedes_spec_draft_id": None,
            "provenance_path": "repository-inventory:2",
        },
    ),
    (
        "authority.compile",
        CompileAuthority,
        {
            "spec_version_id": 3,
            "expected_spec_hash": "sha256:spec",
            "compiler_model": "fake/compiler",
            "compiled_authority": _compiled_authority_payload(),
        },
    ),
    (
        "authority.repair",
        RepairAuthority,
        {
            "source_authority_id": 5,
            "source_authority_fingerprint": "sha256:authority",
            "compiled_authority": _compiled_authority_payload(),
        },
    ),
    (
        "vision.generate",
        RecordVisionDraft,
        {
            "authority_id": 5,
            "authority_fingerprint": "sha256:authority",
            "canonical_content": {"vision": "Focused"},
            "content_fingerprint": "sha256:vision",
            "supersedes_vision_artifact_id": None,
        },
    ),
    (
        "backlog.generate",
        RecordBacklogDraft,
        {
            "authority_id": 5,
            "authority_fingerprint": "sha256:authority",
            "canonical_content": {"backlog_items": []},
            "content_fingerprint": "sha256:backlog",
            "supersedes_backlog_artifact_id": None,
        },
    ),
    (
        "planning.roadmap.generate",
        RecordRoadmapDraft,
        {
            "backlog_artifact_id": 7,
            "backlog_artifact_fingerprint": "sha256:backlog",
            "canonical_content": {"releases": []},
            "content_fingerprint": "sha256:roadmap",
            "supersedes_roadmap_artifact_id": None,
        },
    ),
    (
        "planning.story.generate",
        RecordStoryDraft,
        {
            "requirement_id": "REQ-1",
            "roadmap_artifact_id": 11,
            "roadmap_artifact_fingerprint": "sha256:roadmap",
            "canonical_content": {"stories": []},
            "content_fingerprint": "sha256:story",
            "supersedes_story_artifact_id": None,
        },
    ),
    (
        "planning.sprint.plan",
        RecordSprintPlan,
        {
            "team_name": "Platform",
            "selected_story_ids": [13],
            "canonical_task_plan": {"tasks": []},
            "plan_fingerprint": "sha256:plan",
            "candidate_set_fingerprint": "sha256:candidates",
            "supersedes_sprint_plan_artifact_id": None,
        },
    ),
)


class FakeLeafAgent(BaseAgent):
    """Provider-free leaf agent returning deterministic structured output."""

    response: dict[str, object]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        yield Event(author=self.name, output=self.response)


def _agentic_nodes() -> AgenticRecipeNodes:
    """Build a complete provider-free retained-node replacement set."""
    return AgenticRecipeNodes(
        brownfield_curator=FakeLeafAgent(
            name="fake_brownfield_curator",
            response=BrownfieldCurationOutput(
                canonical_spec=TechnicalSpecArtifact.model_validate(
                    _brownfield_spec_payload()
                )
            ).model_dump(mode="json"),
        ),
        authority_compile=FakeLeafAgent(name="fake_authority_compile", response={}),
        authority_repair=FakeLeafAgent(name="fake_authority_repair", response={}),
        vision_generation=FakeLeafAgent(name="fake_vision", response={}),
        backlog_generation=FakeLeafAgent(name="fake_backlog", response={}),
        roadmap_generation=FakeLeafAgent(name="fake_roadmap", response={}),
        story_generation=FakeLeafAgent(name="fake_story", response={}),
        sprint_planning=FakeLeafAgent(name="fake_sprint", response={}),
    )


def _complete_registry() -> AdkRecipeRegistry:
    return build_agentic_recipe_registry(
        nodes=_agentic_nodes(),
        execution_settings={
            "timeout_seconds": RECIPE_TIMEOUT_SECONDS,
            "max_attempts": RECIPE_MAX_ATTEMPTS,
        },
    )


def _adapter(
    _output: object,
    _context: AttemptCompletionContext,
) -> RecordBacklogDraft:
    message = "Registry tests do not invoke output adapters."
    raise AssertionError(message)


def test_recipe_registry_requires_unique_stable_node_ids() -> None:
    """Reject duplicate execution recipes for one stable domain node."""
    workflow = build_backlog_generation_workflow(
        leaf_agent=FakeLeafAgent(name="fake", response={"ok": True}),
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
    )
    recipe = AdkRecipe(
        node_id="backlog.generate",
        workflow=workflow,
        output_adapter=_adapter,
    )

    with pytest.raises(ValueError, match="must be unique"):
        AdkRecipeRegistry((recipe, recipe))


def test_recipe_registry_fails_closed_for_unknown_node() -> None:
    """Fail closed when a domain decision has no execution recipe."""
    registry = AdkRecipeRegistry(())

    with pytest.raises(UnknownAdkRecipeError):
        registry.require("authority.review")


def test_recipe_registry_covers_each_stable_agentic_domain_node_once() -> None:
    """Keep the declared execution inventory complete, unique, and graph-bound."""
    graph_node_ids = {node.node_id for node in ROOT_GRAPH.root.iter_nodes()}
    registry = _complete_registry()

    assert set(ROOT_GRAPH.agentic_node_ids) <= graph_node_ids
    assert set(ROOT_GRAPH.agentic_node_ids) <= set(registry.node_ids)
    assert "vision.interview" in registry.node_ids
    assert len(registry.node_ids) == len(set(registry.node_ids))
    registered_recipe_ids = tuple(
        registry.require(node_id).node_id for node_id in registry.node_ids
    )
    assert registered_recipe_ids == registry.node_ids
    brownfield_graph = registry.require("onboarding.brownfield.curation").workflow.graph
    assert brownfield_graph is not None
    assert "execute_brownfield_curator" in {
        node.name for node in brownfield_graph.nodes
    }
    for node_id in registry.node_ids:
        recipe = registry.require(node_id)
        assert recipe.workflow.timeout == RECIPE_TIMEOUT_SECONDS
        assert recipe.workflow.retry_config is not None
        assert recipe.workflow.retry_config.max_attempts == RECIPE_MAX_ATTEMPTS
        assert not hasattr(recipe, "prerequisites")
        assert not hasattr(recipe, "next_command")


def test_recipe_registry_rejects_any_domain_catalog_gap() -> None:
    """Fail construction when a graph-marked agentic node lacks a recipe."""
    workflow = build_backlog_generation_workflow(
        leaf_agent=FakeLeafAgent(name="fake", response={"ok": True}),
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
    )
    recipe = AdkRecipe(
        node_id="backlog.generate",
        workflow=workflow,
        output_adapter=_adapter,
    )

    with pytest.raises(ValueError, match="domain agentic catalog"):
        AdkRecipeRegistry(
            (recipe,),
            required_node_ids=ROOT_GRAPH.agentic_node_ids,
        )


@pytest.mark.parametrize(("node_id", "request_type", "payload"), REQUEST_CASES)
def test_complete_registry_adapts_each_output_to_its_typed_request(
    node_id: str,
    request_type: type[PositionedRequest],
    payload: JsonObject,
) -> None:
    """Bind validated leaf output to one node-specific positioned request."""
    recipe = _complete_registry().require(node_id)

    request = recipe.output_adapter(RecipeOutput(payload=payload), COMPLETION_CONTEXT)

    assert isinstance(request, request_type)
    assert request.project_id == COMPLETION_CONTEXT.project_id
    assert request.attempt_id == COMPLETION_CONTEXT.attempt_id
    assert request.attempt_fingerprint == COMPLETION_CONTEXT.attempt_fingerprint


def test_backlog_recipe_fans_out_and_joins_before_validated_output() -> None:
    """Run bounded parallel validation branches before one terminal output."""
    workflow = build_backlog_generation_workflow(
        leaf_agent=FakeLeafAgent(name="fake", response={"ok": True}),
        execution_settings={
            "timeout_seconds": RECIPE_TIMEOUT_SECONDS,
            "max_attempts": RECIPE_MAX_ATTEMPTS,
        },
    )
    assert workflow.graph is not None
    edges = {
        (edge.from_node.name, edge.to_node.name) for edge in workflow.graph.edges
    }

    assert edges == {
        (START.name, "generate_backlog"),
        ("generate_backlog", "validate_structure"),
        ("generate_backlog", "validate_round_trip"),
        ("validate_structure", "join_backlog_validations"),
        ("validate_round_trip", "join_backlog_validations"),
        ("join_backlog_validations", "emit_validated_backlog"),
    }


def test_recipe_contains_execution_only_without_business_prerequisites() -> None:
    """Keep graph authority and next-command rules out of recipes."""
    workflow = build_backlog_generation_workflow(
        leaf_agent=FakeLeafAgent(name="fake", response={"ok": True}),
        execution_settings={
            "timeout_seconds": RECIPE_TIMEOUT_SECONDS,
            "max_attempts": RECIPE_MAX_ATTEMPTS,
        },
    )
    recipe = AdkRecipe(
        node_id="backlog.generate",
        workflow=workflow,
        output_adapter=_adapter,
    )

    assert set(recipe.__dataclass_fields__) == {
        "node_id",
        "workflow",
        "output_adapter",
    }
    assert workflow.timeout == RECIPE_TIMEOUT_SECONDS
    assert workflow.retry_config is not None
    assert workflow.retry_config.max_attempts == RECIPE_MAX_ATTEMPTS
    assert not hasattr(recipe, "prerequisites")
    assert not hasattr(recipe, "next_command")
