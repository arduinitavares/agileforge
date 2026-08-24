# tools/spec_tools.py
"""Explicit direct-Specification validation tool."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from services.specs.story_validation_service import (
    StorySemanticReview,
    ValidateStoryInput,
)
from services.specs.story_validation_service import (
    validate_story_with_specification as _validate_story_with_specification,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from google.adk.tools import ToolContext

    from services.contracts.specification_validation import (
        StorySpecificationReviewInput,
    )


def _run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run one async adapter invocation from synchronous tool code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return cast("T", executor.submit(asyncio.run, coroutine).result())


def _production_semantic_review(review_input: StorySpecificationReviewInput) -> str:
    """Invoke the retained semantic leaf exactly once for one explicit action."""
    agent_module = import_module("adapters.adk.agents.spec_validator")
    runner_module = import_module("utils.adk_runner")
    runtime_module = import_module("utils.runtime_config")
    return _run_async(
        runner_module.invoke_agent_to_text(
            agent=agent_module.root_agent,
            runner_identity=runtime_module.SPEC_VALIDATOR_IDENTITY,
            payload_json=canonical_json(review_input.model_dump(mode="json")),
            no_text_error="Story semantic review returned no complete JSON object.",
        )
    )


def validate_story_with_specification(
    params: dict[str, Any] | ValidateStoryInput,
    tool_context: ToolContext | None = None,
    *,
    semantic_review: StorySemanticReview | None = None,
) -> dict[str, Any]:
    """Validate one accepted Story with a safe structural default.

    Every explicit hybrid retry is a new paid action. A prior call may already
    have been billed, and a completed retry replaces the prior stored snapshot.
    """
    del tool_context
    parsed = ValidateStoryInput.model_validate(params)
    adapter = (
        semantic_review
        if semantic_review is not None
        else (_production_semantic_review if parsed.mode == "hybrid" else None)
    )
    result = _validate_story_with_specification(parsed, semantic_review=adapter)
    if parsed.mode == "hybrid":
        result["paid_retry_warning"] = (
            "A prior call may already have been billed. A retry may incur another "
            "charge and replaces, rather than appends to, the prior snapshot."
        )
    return cast("dict[str, Any]", result)


__all__ = [
    "ValidateStoryInput",
    "validate_story_with_specification",
]
