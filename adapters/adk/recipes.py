"""Execution-only ADK 2 recipes for domain graph nodes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from google.adk import Context, Workflow
from google.adk.workflow import START, JoinNode, RetryConfig, node
from pydantic import BaseModel, ConfigDict, TypeAdapter

from workflow.contracts import JsonObject
from workflow.requests import (
    CompileAuthority,
    RecordBacklogDraft,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RecordVisionDraft,
    RepairAuthority,
)
from workflow.requests.base import PositionedRequest

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent

_JSON_OBJECT = TypeAdapter(JsonObject)
AGENTIC_NODE_IDS = (
    "authority.compile",
    "authority.repair",
    "vision.generate",
    "backlog.generate",
    "planning.roadmap.generate",
    "planning.story.generate",
    "planning.sprint.plan",
)


class RecipeInput(BaseModel):
    """Strict workflow envelope for normalized domain input."""

    model_config = ConfigDict(extra="forbid")
    payload: JsonObject


class RecipeOutput(BaseModel):
    """Strict workflow envelope for validated structured output."""

    model_config = ConfigDict(extra="forbid")
    payload: JsonObject


class _UnvalidatedRecipeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: object


class _JoinedBacklogValidations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validate_structure: RecipeOutput
    validate_round_trip: RecipeOutput


@dataclass(frozen=True)
class AttemptCompletionContext:
    """Exact durable attempt guards supplied to one output adapter."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    attempt_id: int
    attempt_fingerprint: str
    idempotency_key: str
    actor: str
    correlation_id: str | None


type OutputAdapter = Callable[
    [object, AttemptCompletionContext],
    PositionedRequest,
]


@dataclass(frozen=True)
class AdkRecipe:
    """Execution recipe for one stable domain node ID."""

    node_id: str
    workflow: Workflow
    output_adapter: OutputAdapter


@dataclass(frozen=True)
class AgenticRecipeNodes:
    """Injected retained execution nodes used to compose the complete registry."""

    authority_compile: BaseAgent | Workflow
    authority_repair: BaseAgent | Workflow
    vision_generation: BaseAgent | Workflow
    backlog_generation: BaseAgent | Workflow
    roadmap_generation: BaseAgent | Workflow
    story_generation: BaseAgent | Workflow
    sprint_planning: BaseAgent | Workflow


class UnknownAdkRecipeError(LookupError):
    """Raised when no execution recipe exists for a stable graph node."""

    def __init__(self, node_id: str) -> None:
        """Retain the missing stable node ID in the error message."""
        super().__init__(f"No ADK recipe is registered for node {node_id!r}.")


class AdkRecipeRegistry:
    """Map stable domain node IDs to execution-only ADK recipes."""

    def __init__(self, recipes: tuple[AdkRecipe, ...]) -> None:
        """Index recipes while rejecting ambiguous duplicate node IDs."""
        self._recipes = {recipe.node_id: recipe for recipe in recipes}
        if len(self._recipes) != len(recipes):
            message = "ADK recipe node IDs must be unique"
            raise ValueError(message)

    @property
    def node_ids(self) -> tuple[str, ...]:
        """Return registered stable node IDs in deterministic declaration order."""
        return tuple(self._recipes)

    def require(self, node_id: str) -> AdkRecipe:
        """Return the recipe for a stable node or fail closed."""
        try:
            return self._recipes[node_id]
        except KeyError as exc:
            raise UnknownAdkRecipeError(node_id) from exc


def _execution_limits(execution_settings: JsonObject) -> tuple[float, int]:
    timeout_value = execution_settings.get("timeout_seconds")
    attempts_value = execution_settings.get("max_attempts")
    if (
        isinstance(timeout_value, bool)
        or not isinstance(timeout_value, int | float)
        or timeout_value <= 0
    ):
        msg = "execution_settings.timeout_seconds must be a positive number."
        raise ValueError(msg)
    if (
        isinstance(attempts_value, bool)
        or not isinstance(attempts_value, int)
        or attempts_value < 1
    ):
        msg = "execution_settings.max_attempts must be a positive integer."
        raise ValueError(msg)
    return float(timeout_value), attempts_value


def validate_structured_output(output: object) -> JsonObject:
    """Validate a leaf output as one JSON object at the adapter boundary."""
    return _JSON_OBJECT.validate_python(output)


def _request_output_adapter(
    request_type: type[PositionedRequest],
) -> OutputAdapter:
    """Build one schema-only adapter from recipe output to a guarded request."""

    def adapt(
        output: object,
        context: AttemptCompletionContext,
    ) -> PositionedRequest:
        payload = dict(RecipeOutput.model_validate(output).payload)
        payload.update(
            {
                "project_id": context.project_id,
                "graph_version": context.graph_version,
                "fact_fingerprint": context.fact_fingerprint,
                "decision_fingerprint": context.decision_fingerprint,
                "instance_key": context.instance_key,
                "attempt_id": context.attempt_id,
                "attempt_fingerprint": context.attempt_fingerprint,
                "idempotency_key": context.idempotency_key,
                "actor": context.actor,
                "correlation_id": context.correlation_id,
            }
        )
        return request_type.model_validate(payload)

    return adapt


def _build_single_leaf_workflow(
    *,
    workflow_name: str,
    execution_node_name: str,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Build one bounded single-leaf recipe without domain routing policy."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name=execution_node_name,
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_leaf(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        generated = await context.run_node(
            leaf_agent,
            node_input=node_input.payload,
        )
        return RecipeOutput(payload=validate_structured_output(generated))

    return Workflow(
        name=workflow_name,
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_leaf)],
    )


def build_backlog_generation_workflow(
    *,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Generate once, validate in parallel, and join one Backlog artifact."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name="generate_backlog",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def generate_backlog(
        context: Context,
        node_input: RecipeInput,
    ) -> _UnvalidatedRecipeOutput:
        generated = await context.run_node(
            leaf_agent,
            node_input=node_input.payload,
        )
        return _UnvalidatedRecipeOutput(payload=generated)

    @node(
        name="validate_structure",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def validate_structure(
        node_input: _UnvalidatedRecipeOutput,
    ) -> RecipeOutput:
        return RecipeOutput(payload=validate_structured_output(node_input.payload))

    @node(
        name="validate_round_trip",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def validate_round_trip(
        node_input: _UnvalidatedRecipeOutput,
    ) -> RecipeOutput:
        validated = RecipeOutput(
            payload=validate_structured_output(node_input.payload)
        )
        return RecipeOutput.model_validate_json(validated.model_dump_json())

    join_validations = JoinNode(
        name="join_backlog_validations",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
        output_schema=_JoinedBacklogValidations,
    )

    @node(
        name="emit_validated_backlog",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def emit_validated_backlog(
        node_input: _JoinedBacklogValidations,
    ) -> RecipeOutput:
        if node_input.validate_structure != node_input.validate_round_trip:
            msg = "Parallel Backlog validations produced different output."
            raise ValueError(msg)
        return node_input.validate_structure

    return Workflow(
        name="backlog_generation",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[
            (
                START,
                generate_backlog,
                (validate_structure, validate_round_trip),
                join_validations,
                emit_validated_backlog,
            )
        ],
    )


def build_agentic_recipe_registry(
    *,
    nodes: AgenticRecipeNodes,
    execution_settings: JsonObject,
) -> AdkRecipeRegistry:
    """Compose exactly one execution-only recipe for every agentic domain node."""
    return AdkRecipeRegistry(
        (
            AdkRecipe(
                node_id="authority.compile",
                workflow=_build_single_leaf_workflow(
                    workflow_name="authority_compilation",
                    execution_node_name="execute_authority_compiler",
                    leaf_agent=nodes.authority_compile,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(CompileAuthority),
            ),
            AdkRecipe(
                node_id="authority.repair",
                workflow=_build_single_leaf_workflow(
                    workflow_name="authority_repair",
                    execution_node_name="execute_authority_repair",
                    leaf_agent=nodes.authority_repair,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(RepairAuthority),
            ),
            AdkRecipe(
                node_id="vision.generate",
                workflow=_build_single_leaf_workflow(
                    workflow_name="vision_generation",
                    execution_node_name="execute_vision_generator",
                    leaf_agent=nodes.vision_generation,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(RecordVisionDraft),
            ),
            AdkRecipe(
                node_id="backlog.generate",
                workflow=build_backlog_generation_workflow(
                    leaf_agent=nodes.backlog_generation,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(RecordBacklogDraft),
            ),
            AdkRecipe(
                node_id="planning.roadmap.generate",
                workflow=_build_single_leaf_workflow(
                    workflow_name="roadmap_generation",
                    execution_node_name="execute_roadmap_generator",
                    leaf_agent=nodes.roadmap_generation,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(RecordRoadmapDraft),
            ),
            AdkRecipe(
                node_id="planning.story.generate",
                workflow=_build_single_leaf_workflow(
                    workflow_name="story_generation",
                    execution_node_name="execute_story_generator",
                    leaf_agent=nodes.story_generation,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(RecordStoryDraft),
            ),
            AdkRecipe(
                node_id="planning.sprint.plan",
                workflow=_build_single_leaf_workflow(
                    workflow_name="sprint_planning",
                    execution_node_name="execute_sprint_planner",
                    leaf_agent=nodes.sprint_planning,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(RecordSprintPlan),
            ),
        )
    )


__all__ = [
    "AGENTIC_NODE_IDS",
    "AdkRecipe",
    "AdkRecipeRegistry",
    "AgenticRecipeNodes",
    "AttemptCompletionContext",
    "OutputAdapter",
    "RecipeInput",
    "RecipeOutput",
    "UnknownAdkRecipeError",
    "build_agentic_recipe_registry",
    "build_backlog_generation_workflow",
    "validate_structured_output",
]
