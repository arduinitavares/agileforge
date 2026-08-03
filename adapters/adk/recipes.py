"""Execution-only ADK 2 recipes for domain graph nodes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from google.adk import Context, Workflow
from google.adk.workflow import RetryConfig, node
from pydantic import BaseModel, ConfigDict, TypeAdapter

from workflow.contracts import JsonObject
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


def build_backlog_generation_workflow(
    *,
    leaf_agent: BaseAgent,
    execution_settings: JsonObject,
) -> Workflow:
    """Build one artifact-generation recipe without domain prerequisites."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name="generate_and_validate",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def generate_and_validate(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        generated = await context.run_node(
            leaf_agent,
            node_input=node_input.payload,
        )
        return RecipeOutput(payload=validate_structured_output(generated))

    return Workflow(
        name="backlog_generation",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[("START", generate_and_validate)],
    )


__all__ = [
    "AdkRecipe",
    "AdkRecipeRegistry",
    "AttemptCompletionContext",
    "OutputAdapter",
    "RecipeInput",
    "RecipeOutput",
    "UnknownAdkRecipeError",
    "build_backlog_generation_workflow",
    "validate_structured_output",
]
