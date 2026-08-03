"""Run one ADK recipe inside durable domain attempt boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from google.adk.workflow import NodeTimeoutError
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai import types
from pydantic import TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from adapters.adk.recipes import (
    AdkRecipe,
    AdkRecipeRegistry,
    AttemptCompletionContext,
    RecipeInput,
    RecipeOutput,
)
from utils.runtime_config import (
    ADK_EXECUTION_TRACE_IDENTITY,
    RunnerIdentity,
    get_adk_execution_trace_db_target,
)
from workflow.contracts import (
    JsonObject,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.requests import FailNodeAttempt, StartNodeAttempt, TransitionRequest

if TYPE_CHECKING:
    from workflow.domain import WorkflowDomain

_TRANSITION_REQUEST = TypeAdapter(TransitionRequest)


@dataclass(frozen=True)
class AdkExecutionConfig:
    """Stable execution metadata applied to every attempt from one runner."""

    project_id: int
    model_id: str
    execution_settings: JsonObject
    lease_seconds: int
    actor: str
    correlation_id: str | None = None
    identity: RunnerIdentity = ADK_EXECUTION_TRACE_IDENTITY


class AdkWorkflowRunner:
    """Execute an available decision while durable facts remain authoritative.

    Recovery starts a replacement attempt only after the prior lease expires.
    Provider execution is therefore at least once: a crash after provider work
    but before durable outcome recording can repeat provider cost. ADK session
    state remains optional execution trace and is never recovery authority.
    """

    def __init__(
        self,
        *,
        domain: WorkflowDomain,
        registry: AdkRecipeRegistry,
        config: AdkExecutionConfig,
        session_service: BaseSessionService | None = None,
    ) -> None:
        """Retain domain, recipe, execution, and trace-store dependencies."""
        self._domain = domain
        self._registry = registry
        self._config = config
        self._session_service = session_service or DatabaseSessionService(
            db_url=get_adk_execution_trace_db_target().async_sqlite_url
        )

    def run(
        self,
        decision: NodeDecision,
        input_payload: JsonObject,
    ) -> TransitionResult:
        """Run one recipe outside domain transactions and submit its continuation."""
        position = self._domain.position(self._config.project_id)
        current = next(
            (
                item
                for item in position.decisions
                if item.node_id == decision.node_id
                and item.instance_key == decision.instance_key
            ),
            None,
        )
        if current != decision:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=WorkflowErrorCode.STALE_POSITION,
                    message="The supplied node decision is no longer current.",
                ),
            )
        start_key = f"adk-start:{uuid4()}"
        start_request = StartNodeAttempt(
            project_id=self._config.project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=start_key,
            actor=self._config.actor,
            correlation_id=self._config.correlation_id,
            target_node_id=decision.node_id,
            target_instance_key=decision.instance_key,
            normalized_input=input_payload,
            model_id=self._config.model_id,
            execution_settings=self._config.execution_settings,
            lease_seconds=self._config.lease_seconds,
        )
        started = self._domain.transition(start_request)
        if not started.ok:
            return started
        attempt_id = started.output.get("attempt_id")
        attempt_fingerprint = started.output.get("attempt_fingerprint")
        if not isinstance(attempt_id, int) or not isinstance(
            attempt_fingerprint, str
        ):
            msg = "StartNodeAttempt returned an invalid durable receipt."
            raise TypeError(msg)
        context = AttemptCompletionContext(
            project_id=start_request.project_id,
            graph_version=start_request.graph_version,
            fact_fingerprint=start_request.fact_fingerprint,
            decision_fingerprint=start_request.decision_fingerprint,
            instance_key=start_request.target_instance_key,
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            idempotency_key=f"{start_request.idempotency_key}:completion",
            actor=start_request.actor,
            correlation_id=start_request.correlation_id,
        )
        try:
            recipe = self._registry.require(decision.node_id)
            output = asyncio.run(
                self._run_recipe(
                    recipe,
                    attempt_id=attempt_id,
                    input_payload=input_payload,
                )
            )
            completion = recipe.output_adapter(output, context)
        except (
            DynamicNodeFailError,
            NodeTimeoutError,
            KeyError,
            OSError,
            RuntimeError,
            SQLAlchemyError,
            TypeError,
            ValueError,
        ) as error:
            failed = self._domain.transition(
                FailNodeAttempt(
                    project_id=self._config.project_id,
                    attempt_id=attempt_id,
                    attempt_fingerprint=attempt_fingerprint,
                    failure_code="ADK_EXECUTION_FAILED",
                    failure_message=str(error) or type(error).__name__,
                    idempotency_key=f"{start_key}:failure",
                    actor=self._config.actor,
                    correlation_id=self._config.correlation_id,
                )
            )
            if (
                failed.error is not None
                and failed.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
            ):
                return failed
            return TransitionResult(
                ok=False,
                position=failed.position,
                error=WorkflowError(
                    code=WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED,
                    message="ADK recipe execution or output validation failed.",
                ),
            )
        return self._domain.transition(
            _TRANSITION_REQUEST.validate_python(completion)
        )

    async def _run_recipe(
        self,
        recipe: AdkRecipe,
        *,
        attempt_id: int,
        input_payload: JsonObject,
    ) -> RecipeOutput:
        session_id = str(attempt_id)
        await self._session_service.create_session(
            app_name=self._config.identity.app_name,
            user_id=self._config.identity.user_id,
            session_id=session_id,
        )
        app = App(
            name=self._config.identity.app_name,
            root_agent=recipe.workflow,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        runner = Runner(app=app, session_service=self._session_service)
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=RecipeInput(payload=input_payload).model_dump_json()
                )
            ],
        )
        output: object | None = None
        async for event in runner.run_async(
            user_id=self._config.identity.user_id,
            session_id=session_id,
            new_message=message,
        ):
            if event.output is not None:
                output = event.output
        if output is None:
            msg = "ADK recipe completed without structured output."
            raise ValueError(msg)
        return RecipeOutput.model_validate(output)


__all__ = ["AdkExecutionConfig", "AdkWorkflowRunner"]
