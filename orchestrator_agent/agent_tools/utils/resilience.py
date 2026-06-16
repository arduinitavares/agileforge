"""Resilience utilities for ADK agents.

Provides wrappers for automatic retry on transient failures:
- ValidationError: Pydantic schema failures (retry with feedback)
- ZDR routing: Privacy/compliance provider unavailable
- Rate Limit (429): Service online but at capacity
- Provider Error (5xx): Service may be down/unhealthy

Retry Strategy (Best Practice for Unstable LLM APIs):
-----------------------------------------------------
1. ORTHOGONAL ERROR HANDLING: Each error type has its own CONSECUTIVE counter.
2. THE "ALIVE" SIGNAL: A 429 proves the upstream is ONLINE. Reset provider error counter.
3. RESET LOGIC: Only fail if we hit a limit of CONSECUTIVE errors of the same type.
"""

import asyncio
import logging
import re
import secrets
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from pydantic import Field, ValidationError

from utils.model_config import (
    ZDR_MAX_BACKOFF_SECONDS,
    ZDR_MAX_RETRIES,
    is_zdr_routing_error,
)

logger = logging.getLogger(__name__)

# --- Retry Configuration ---
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_MIN_BACKOFF_SECONDS = 10
RATE_LIMIT_MAX_BACKOFF_SECONDS = 30
PROVIDER_ERROR_MAX_RETRIES = 3
PROVIDER_ERROR_MAX_BACKOFF_SECONDS = 5
_BACKOFF_RANDOM = secrets.SystemRandom()


def _is_rate_limit_error(error: Exception) -> bool:
    """Return True when the error indicates a rate limit response (429).

    A 429 is proof that the upstream service is ONLINE (availability=100%),
    even if capacity is temporarily 0%.
    """
    return "RateLimitError" in str(error)


def _is_provider_error(error: Exception) -> bool:
    """Return True when the error indicates a transient provider issue (5xx).

    These errors indicate the upstream service may be DOWN or unhealthy.
    """
    message = str(error)
    return "Unable to get json response" in message or "OpenrouterException" in message


def _extract_retry_after(error: Exception) -> float | None:
    """Extract retry-after header value from rate limit error if available."""
    message = str(error)
    match = re.search(r"retry[- ]?after[:\s]+(\d+(?:\.\d+)?)", message, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


class SelfHealingAgent(BaseAgent):
    """A wrapper that catches transient failures and retries with appropriate backoff.

    Handles four error categories with orthogonal counters:
    - ValidationError: Pydantic schema failures (retry with feedback injected)
    - ZDR routing: Privacy/compliance provider unavailable (random backoff)
    - Rate Limit (429): Service online but at capacity (prefer retry-after header)
    - Provider Error (5xx): Service may be down (exponential backoff)

    Key behavior: A 429 proves the service is ALIVE and resets the provider error counter.
    """

    agent: BaseAgent = Field(description="The inner agent to wrap.")
    max_retries: int = Field(
        default=3, description="Number of retries on validation failure."
    )
    max_zdr_retries: int = Field(
        default=ZDR_MAX_RETRIES, description="Number of retries on ZDR routing failure."
    )
    max_rate_limit_retries: int = Field(
        default=RATE_LIMIT_MAX_RETRIES, description="Number of retries on rate limit."
    )
    max_provider_error_retries: int = Field(
        default=PROVIDER_ERROR_MAX_RETRIES,
        description="Number of retries on provider errors.",
    )
    name: str = Field(default="SelfHealingAgent", description="Name of the agent")
    description: str = Field(
        default="Wrapper for resilience", description="Description of the agent"
    )
    output_key: str | None = Field(
        default=None, description="Output key for pipeline compatibility"
    )
    output_schema: Any | None = Field(
        default=None, description="Output schema for pipeline compatibility"
    )

    def __init__(
        self,
        agent: BaseAgent,
        max_retries: int = 3,
        max_zdr_retries: int = ZDR_MAX_RETRIES,
        max_rate_limit_retries: int = RATE_LIMIT_MAX_RETRIES,
        max_provider_error_retries: int = PROVIDER_ERROR_MAX_RETRIES,
        name: str | None = None,  # Make name overrideable
    ):
        # Determine name and description from inner agent if possible
        if name is None:
            name = f"SelfHealing_{agent.name}"
            sanitized_name = "".join(
                c if c.isalnum() or c == "_" else "_" for c in name
            )
        else:
            sanitized_name = name

        init_data: dict[str, Any] = {
            "name": sanitized_name,
            "description": getattr(agent, "description", "Wrapper for resilience"),
            "sub_agents": [agent],
            "agent": agent,
            "max_retries": max_retries,
            "max_zdr_retries": max_zdr_retries,
            "max_rate_limit_retries": max_rate_limit_retries,
            "max_provider_error_retries": max_provider_error_retries,
            "output_key": getattr(agent, "output_key", None),
            "output_schema": getattr(agent, "output_schema", None),
        }
        super().__init__(**init_data)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Any, None]:
        """Override the ADK extension point for execution with orthogonal retry logic."""
        # Orthogonal counters - each error type tracked separately
        validation_consecutive = 0
        zdr_consecutive = 0
        rate_limit_consecutive = 0
        provider_error_consecutive = 0

        while True:
            try:
                async for event in self.agent.run_async(ctx):
                    yield event
                # SUCCESS: All counters implicitly reset on next call
                return

            except ValidationError as e:
                validation_consecutive += 1
                if validation_consecutive > self.max_retries:
                    logger.error(
                        f"[{self.name}] Failed validation after {self.max_retries} consecutive retries."
                    )
                    raise

                # Log the actual error (truncate to 800 chars to avoid log flooding)
                error_str = str(e)[:800]
                logger.warning(
                    f"[{self.name}] Validation error (Attempt {validation_consecutive}/{self.max_retries}): {error_str}"
                )

                # Inject feedback into state
                error_msg = (
                    f"Validation Error on attempt {validation_consecutive}: {e!s}"
                )
                feedback_msg = (
                    f"\nSYSTEM_FEEDBACK: The previous output failed validation.\n"
                    f"ERROR: {error_msg}\n"
                    f"INSTRUCTION: Fix the JSON structure or logic based on the error above."
                )

                # Access session state directly via context
                if ctx.session and ctx.session.state is not None:
                    ctx.session.state["validation_feedback"] = feedback_msg
                    history = ctx.session.state.get("_validation_history", [])
                    ctx.session.state["_validation_history"] = list(history) + [
                        error_msg
                    ]

                # ADK agents re-read state from session/context on next run

            except Exception as e:
                # --- ZDR/PRIVACY ROUTING FAILURE ---
                if is_zdr_routing_error(e):
                    zdr_consecutive += 1
                    if zdr_consecutive > self.max_zdr_retries:
                        logger.error(
                            f"[{self.name}] ZDR routing failed after {self.max_zdr_retries} consecutive retries."
                        )
                        raise

                    backoff = _BACKOFF_RANDOM.uniform(0, ZDR_MAX_BACKOFF_SECONDS)
                    logger.warning(
                        f"[{self.name}] ZDR routing failure (Attempt {zdr_consecutive}/{self.max_zdr_retries}). "
                        f"Retrying in {backoff:.1f}s..."
                    )
                    await asyncio.sleep(backoff)
                    continue

                # --- RATE LIMIT (429): Service is ONLINE but at capacity ---
                if _is_rate_limit_error(e):
                    rate_limit_consecutive += 1

                    # CRITICAL: A 429 proves the service is ALIVE.
                    # Reset the provider error counter - the service responded!
                    if provider_error_consecutive > 0:
                        logger.info(
                            f"[{self.name}] Rate limit received after {provider_error_consecutive} provider errors - "
                            "service is ALIVE, resetting provider error counter."
                        )
                        provider_error_consecutive = 0

                    if rate_limit_consecutive > self.max_rate_limit_retries:
                        logger.error(
                            f"[{self.name}] Rate limit retries exceeded ({self.max_rate_limit_retries} consecutive)."
                        )
                        raise

                    # Prefer retry-after header, fall back to configured backoff
                    retry_after = _extract_retry_after(e)
                    if retry_after:
                        backoff = retry_after
                        logger.info(
                            f"[{self.name}] Using retry-after header: {backoff:.1f}s"
                        )
                    else:
                        backoff = _BACKOFF_RANDOM.uniform(
                            RATE_LIMIT_MIN_BACKOFF_SECONDS,
                            RATE_LIMIT_MAX_BACKOFF_SECONDS,
                        )

                    logger.warning(
                        f"[{self.name}] Rate limit (429) retry {rate_limit_consecutive}/{self.max_rate_limit_retries} - "
                        f"waiting {backoff:.1f}s..."
                    )
                    await asyncio.sleep(backoff)
                    continue

                # --- PROVIDER ERROR (5xx): Service may be DOWN ---
                if _is_provider_error(e):
                    provider_error_consecutive += 1

                    if provider_error_consecutive > self.max_provider_error_retries:
                        logger.error(
                            f"[{self.name}] Provider error retries exceeded ({self.max_provider_error_retries} consecutive)."
                        )
                        raise

                    backoff = _BACKOFF_RANDOM.uniform(
                        0, PROVIDER_ERROR_MAX_BACKOFF_SECONDS
                    )
                    logger.warning(
                        f"[{self.name}] Provider error (5xx) retry {provider_error_consecutive}/{self.max_provider_error_retries} - "
                        f"waiting {backoff:.1f}s..."
                    )
                    await asyncio.sleep(backoff)
                    continue

                # Unknown error - re-raise
                raise


# Legacy ADK 1.x helper. New ADK 2.0 workflows must use graph or dynamic
# workflow nodes and must not depend on _run_async_impl for curation control.
class ConditionalLoopAgent(BaseAgent):
    """
    Executes an inner agent in a loop until a condition is met or max_iterations reached.
    """

    agent: BaseAgent = Field(description="The agent to run in a loop.")
    max_iterations: int = Field(default=4, description="Maximum number of iterations.")
    exit_condition_key: str = Field(
        default="refinement_result.is_valid",
        description="State key path to check for True to exit loop.",
    )
    name: str = Field(
        default="ConditionalLoopAgent", description="Name of the loop agent"
    )
    description: str = Field(
        default="Loops until condition met", description="Description of the loop agent"
    )

    def __init__(
        self,
        agent: BaseAgent,
        max_iterations: int = 4,
        exit_condition_key: str = "refinement_result.is_valid",
        **kwargs,
    ):
        init_data: dict[str, Any] = {
            "name": kwargs.get("name", "ConditionalLoopAgent"),
            "description": kwargs.get("description", "Loops until condition met"),
            "sub_agents": [agent],
            "agent": agent,
            "max_iterations": max_iterations,
            "exit_condition_key": exit_condition_key,
        }
        super().__init__(**init_data)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Any, None]:
        for i in range(self.max_iterations):
            logger.debug(f"[{self.name}] Iteration {i}")
            async for event in self.agent.run_async(ctx):
                yield event

            if (
                ctx.session
                and ctx.session.state
                and self._should_exit(ctx.session.state)
            ):
                logger.debug(f"[{self.name}] Exit condition met")
                break

    def _should_exit(self, state: dict[str, Any]) -> bool:
        refinement = state.get("refinement_result")
        if not refinement:
            return False

        if hasattr(refinement, "is_valid"):
            return refinement.is_valid
        if isinstance(refinement, dict):
            return refinement.get("is_valid", False)

        return False
