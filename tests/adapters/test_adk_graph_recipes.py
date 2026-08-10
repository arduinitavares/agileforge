"""ADK 2 graph recipe boundary tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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
    AGENTIC_NODE_IDS,
    AdkRecipe,
    AdkRecipeRegistry,
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    UnknownAdkRecipeError,
    build_agentic_recipe_registry,
    build_backlog_generation_workflow,
)
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.definitions.root import ROOT_GRAPH
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    CompileAuthority,
    RecordBacklogDraft,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
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
    graph_version="agileforge.workflow.v2",
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


REQUEST_CASES: tuple[
    tuple[str, type[PositionedRequest], JsonObject],
    ...,
] = (
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
        "backlog.generate",
        RecordBacklogDraft,
        {
            "authority_id": 5,
            "authority_fingerprint": "sha256:authority",
            "product_goal_artifact_id": 3,
            "product_goal_fingerprint": "sha256:goal",
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
        authority_compile=FakeLeafAgent(name="fake_authority_compile", response={}),
        authority_repair=FakeLeafAgent(name="fake_authority_repair", response={}),
        vision_interview=FakeLeafAgent(
            name="fake_vision_interview",
            response={},
        ),
        product_goal=FakeLeafAgent(name="fake_product_goal", response={}),
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

    assert set(AGENTIC_NODE_IDS) == set(ROOT_GRAPH.agentic_node_ids)
    assert set(ROOT_GRAPH.agentic_node_ids) <= graph_node_ids
    assert registry.node_ids == AGENTIC_NODE_IDS
    assert len(registry.node_ids) == len(set(registry.node_ids))
    registered_recipe_ids = tuple(
        registry.require(node_id).node_id for node_id in registry.node_ids
    )
    assert registered_recipe_ids == registry.node_ids
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
            required_node_ids=AGENTIC_NODE_IDS,
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


def _sprint_attempt_input() -> JsonObject:
    planner_input: JsonObject = {
        "available_stories": [
            {
                "story_id": 11,
                "story_title": "First locked Story",
                "priority": 1,
                "story_points": 2,
                "story_description": "Deliver the first locked Story.",
            },
            {
                "story_id": 12,
                "story_title": "Second locked Story",
                "priority": 2,
                "story_points": 3,
                "story_description": "Deliver the second locked Story.",
            },
        ],
        "capacity_points": 5,
        "capacity_source": "user_override",
        "capacity_basis": "5 points provided by the operator.",
        "user_context": "Keep the cohort exact.",
        "include_task_decomposition": False,
    }
    return {
        "planner_input": planner_input,
        "capacity_points": 5,
        "capacity_source": "user_override",
        "capacity_basis": "5 points provided by the operator.",
        "requested_max_story_points": 5,
        "requested_story_ids": [11, 12],
        "locked_story_ids": [11, 12],
        "team_name": "Platform",
        "include_task_decomposition": False,
        "guidance": "Keep the cohort exact.",
        "candidate_set_fingerprint": "sha256:candidates",
        "supersedes_sprint_plan_artifact_id": 7,
    }


def _sprint_output() -> JsonObject:
    return {
        "sprint_goal": "Deliver the exact locked cohort.",
        "sprint_number": 2,
        "selected_stories": [
            {
                "story_id": 11,
                "story_title": "First locked Story",
                "tasks": [],
                "reason_for_selection": "Host locked this Story.",
            },
            {
                "story_id": 12,
                "story_title": "Second locked Story",
                "tasks": [],
                "reason_for_selection": "Host locked this Story.",
            },
        ],
        "deselected_stories": [],
        "capacity_analysis": {
            "capacity_points": 5,
            "capacity_source": "user_override",
            "capacity_basis": "5 points provided by the operator.",
            "selected_count": 2,
            "story_points_used": 5,
            "remaining_capacity_points": 0,
            "commitment_note": "The locked cohort fits.",
            "reasoning": "The exact host-selected Stories consume five points.",
        },
    }


def test_sprint_recipe_builds_record_request_from_host_owned_envelope() -> None:
    """Bind only validated model planning content to trusted host evidence."""
    context = replace(
        COMPLETION_CONTEXT,
        normalized_input=_sprint_attempt_input(),
    )
    output = _sprint_output()

    request = (
        _complete_registry()
        .require("planning.sprint.plan")
        .output_adapter(RecipeOutput(payload=output), context)
    )

    assert isinstance(request, RecordSprintPlan)
    assert request.team_name == "Platform"
    assert request.selected_story_ids == (11, 12)
    assert request.candidate_set_fingerprint == "sha256:candidates"
    assert (
        request.supersedes_sprint_plan_artifact_id
        == _sprint_attempt_input()["supersedes_sprint_plan_artifact_id"]
    )
    assert request.canonical_task_plan == output
    assert request.plan_fingerprint == canonical_hash(output)


@pytest.mark.parametrize(
    "mutation",
    [
        "added_story",
        "dropped_story",
        "reordered_stories",
        "wrong_capacity",
        "model_candidate_fingerprint",
        "model_team_name",
        "unexpected_tasks",
    ],
)
def test_sprint_recipe_rejects_model_owned_or_changed_host_facts(
    mutation: str,
) -> None:
    """Reject any model drift from the locked cohort and host planning policy."""
    envelope = _sprint_attempt_input()
    output = deepcopy(_sprint_output())
    selected = output["selected_stories"]
    assert isinstance(selected, list)
    if mutation == "added_story":
        selected.append(
            {
                "story_id": 13,
                "story_title": "Added Story",
                "tasks": [],
                "reason_for_selection": "Model added this Story.",
            }
        )
    elif mutation == "dropped_story":
        selected.pop()
    elif mutation == "reordered_stories":
        selected.reverse()
    elif mutation == "wrong_capacity":
        analysis = output["capacity_analysis"]
        assert isinstance(analysis, dict)
        analysis["capacity_points"] = 6
    elif mutation == "model_candidate_fingerprint":
        output["candidate_set_fingerprint"] = "model-owned"
    elif mutation == "model_team_name":
        output["team_name"] = "Model Team"
    else:
        first = selected[0]
        assert isinstance(first, dict)
        first["tasks"] = [
            {
                "description": "Unexpected decomposition",
                "task_kind": "implementation",
                "artifact_targets": ["planning module"],
                "workstream_tags": ["workflow"],
                "relevant_invariant_ids": [],
                "checklist_items": ["Run focused tests"],
            }
        ]
    context = replace(
        COMPLETION_CONTEXT,
        normalized_input=envelope,
    )

    with pytest.raises((TypeError, ValueError)):
        _complete_registry().require("planning.sprint.plan").output_adapter(
            RecipeOutput(payload=output),
            context,
        )


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
    edges = {(edge.from_node.name, edge.to_node.name) for edge in workflow.graph.edges}

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
