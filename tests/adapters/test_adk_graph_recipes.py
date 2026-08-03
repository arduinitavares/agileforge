"""ADK 2 graph recipe boundary tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.events import Event

from adapters.adk.recipes import (
    AdkRecipe,
    AdkRecipeRegistry,
    AttemptCompletionContext,
    UnknownAdkRecipeError,
    build_backlog_generation_workflow,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from workflow.requests import RecordBacklogDraft

RECIPE_TIMEOUT_SECONDS = 7.0
RECIPE_MAX_ATTEMPTS = 2


class FakeLeafAgent(BaseAgent):
    """Provider-free leaf agent returning deterministic structured output."""

    response: dict[str, object]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        yield Event(author=self.name, output=self.response)


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
