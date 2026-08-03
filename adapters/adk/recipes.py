"""Execution-only ADK 2 recipes for domain graph nodes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from google.adk import Context, Workflow
from google.adk.workflow import START, JoinNode, RetryConfig, node
from pydantic import BaseModel, ConfigDict, TypeAdapter

from services.contracts.brownfield import (
    BrownfieldCurationInput,
    BrownfieldCurationOutput,
)
from services.specs.profile_content import normalize_spec_content_for_registry
from utils.agileforge_spec_profile import canonical_spec_json
from utils.spec_schemas import (
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerInput,
)
from workflow.contracts import JsonObject
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
from workflow.requests.base import PositionedRequest

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent

_JSON_OBJECT = TypeAdapter(JsonObject)


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


class _BrownfieldRecipePayload(BaseModel):
    """Trusted host metadata kept outside model-controlled output."""

    model_config = ConfigDict(extra="forbid")
    curation_input: BrownfieldCurationInput
    supersedes_spec_draft_id: int | None = None
    provenance_path: str | None = None


class _CompileAuthorityRecipePayload(BaseModel):
    """Normalized host guards and retained compiler input."""

    model_config = ConfigDict(extra="forbid")
    spec_version_id: int
    expected_spec_hash: str
    compiler_model: str = "openrouter/openai/gpt-5.6-luna"
    compiler_input: SpecAuthorityCompilerInput


class _RepairAuthorityRecipePayload(BaseModel):
    """Normalized rejected-authority guards and retained compiler input."""

    model_config = ConfigDict(extra="forbid")
    source_authority_id: int
    source_authority_fingerprint: str
    compiler_input: SpecAuthorityCompilerInput


type _AuthorityRecipePayload = (
    _CompileAuthorityRecipePayload | _RepairAuthorityRecipePayload
)


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

    brownfield_curator: BaseAgent | Workflow
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


class _AuthorityCompilerFailureError(RuntimeError):
    """Raised when a retained compiler returns its typed failure variant."""


class AdkRecipeRegistry:
    """Map stable domain node IDs to execution-only ADK recipes."""

    def __init__(
        self,
        recipes: tuple[AdkRecipe, ...],
        *,
        required_node_ids: tuple[str, ...] | None = None,
    ) -> None:
        """Index recipes while rejecting ambiguous duplicate node IDs."""
        self._recipes = {recipe.node_id: recipe for recipe in recipes}
        if len(self._recipes) != len(recipes):
            message = "ADK recipe node IDs must be unique"
            raise ValueError(message)
        if (
            required_node_ids is not None
            and tuple(self._recipes) != required_node_ids
        ):
            message = "ADK recipes must exactly match the domain agentic catalog"
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


def _build_brownfield_curation_workflow(
    *,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Run pre-authority curation and emit one canonical draft payload."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name="execute_brownfield_curator",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_brownfield_curator(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        payload = _BrownfieldRecipePayload.model_validate(node_input.payload)
        generated = await context.run_node(
            leaf_agent,
            node_input=payload.curation_input.model_dump(mode="json"),
        )
        curated = BrownfieldCurationOutput.model_validate(generated)
        normalized = normalize_spec_content_for_registry(
            canonical_spec_json(curated.canonical_spec)
        )
        inventory = payload.curation_input.inventory
        return RecipeOutput(
            payload={
                "repository_inventory_id": inventory.repository_inventory_id,
                "repository_inventory_fingerprint": (
                    inventory.repository_inventory_fingerprint
                ),
                "canonical_content": _JSON_OBJECT.validate_json(
                    normalized.content
                ),
                "supersedes_spec_draft_id": payload.supersedes_spec_draft_id,
                "provenance_path": payload.provenance_path,
            }
        )

    return Workflow(
        name="brownfield_curation",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_brownfield_curator)],
    )


def _authority_completion_payload(
    payload: _AuthorityRecipePayload,
    compiled_authority: SpecAuthorityCompilationSuccess,
) -> JsonObject:
    if isinstance(payload, _CompileAuthorityRecipePayload):
        return {
            "spec_version_id": payload.spec_version_id,
            "expected_spec_hash": payload.expected_spec_hash,
            "compiler_model": payload.compiler_model,
            "compiled_authority": compiled_authority.model_dump(mode="json"),
        }
    return {
        "source_authority_id": payload.source_authority_id,
        "source_authority_fingerprint": payload.source_authority_fingerprint,
        "compiled_authority": compiled_authority.model_dump(mode="json"),
    }


def _build_authority_workflow(
    *,
    workflow_name: str,
    execution_node_name: str,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
    repair: bool,
) -> Workflow:
    """Invoke one retained compiler and emit strict precomputed authority."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name=execution_node_name,
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_authority_leaf(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        payload: _AuthorityRecipePayload
        if repair:
            payload = _RepairAuthorityRecipePayload.model_validate(
                node_input.payload
            )
        else:
            payload = _CompileAuthorityRecipePayload.model_validate(
                node_input.payload
            )
        generated = await context.run_node(
            leaf_agent,
            node_input=payload.compiler_input.model_dump(mode="json"),
        )
        envelope = SpecAuthorityCompilerEnvelope.model_validate(generated)
        if isinstance(envelope.result, SpecAuthorityCompilationFailure):
            message = (
                f"Authority compiler failed: {envelope.result.error}: "
                f"{envelope.result.reason}"
            )
            raise _AuthorityCompilerFailureError(message)
        return RecipeOutput(
            payload=_authority_completion_payload(payload, envelope.result)
        )

    return Workflow(
        name=workflow_name,
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_authority_leaf)],
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
                node_id="onboarding.brownfield.curation",
                workflow=_build_brownfield_curation_workflow(
                    leaf_agent=nodes.brownfield_curator,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(RecordBrownfieldSpecDraft),
            ),
            AdkRecipe(
                node_id="authority.compile",
                workflow=_build_authority_workflow(
                    workflow_name="authority_compilation",
                    execution_node_name="execute_authority_compiler",
                    leaf_agent=nodes.authority_compile,
                    execution_settings=execution_settings,
                    repair=False,
                ),
                output_adapter=_request_output_adapter(CompileAuthority),
            ),
            AdkRecipe(
                node_id="authority.repair",
                workflow=_build_authority_workflow(
                    workflow_name="authority_repair",
                    execution_node_name="execute_authority_repair",
                    leaf_agent=nodes.authority_repair,
                    execution_settings=execution_settings,
                    repair=True,
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
        ),
        required_node_ids=ROOT_GRAPH.agentic_node_ids,
    )


__all__ = [
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
