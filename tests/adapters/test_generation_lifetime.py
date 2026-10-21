"""Healthy generation has renewable ownership, not an application deadline."""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from sqlmodel import Session, select

from adapters.adk import provider_retry
from adapters.adk.agents.product_goal import root_agent
from adapters.adk.provider_transport import OpenRouterOneSendCompletion
from adapters.adk.recipes import AGENTIC_NODE_IDS, build_agentic_recipe_registry
from adapters.adk.runner import AdkExecutionConfig, AdkRunGuards, AdkWorkflowRunner
from models.product_definition import ProductGoalInterviewTurn
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOwnership
from tests.adapters.test_adk_graph_recipes import _agentic_nodes
from tests.adapters.test_adk_workflow_runner import (
    EVALUATED_AT,
    TrackingSessionService,
    _goal_decision,
    _goal_output,
    _goal_registry,
    _goal_runner_input,
    _goal_runner_system,
)
from tests.adapters.test_provider_boundary_routes import (
    Audit,
    Clock,
    context,
    error,
    invoke,
)
from tests.adapters.test_provider_transport_contract import SUCCESS, Wire
from tests.adapters.test_vision_recipe import RawVisionLlm
from tests.workflow.test_agentic_node_lifecycle import MutableClock
from workflow.contracts import WorkflowErrorCode

EXPECTED_SENDS = 2
MINIMUM_WAIT_CYCLES = 4
MINIMUM_RENEWALS = 3

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable

    import httpx
    from google.adk.models.llm_request import LlmRequest
    from sqlalchemy.engine import Engine

    from workflow.attempt_ownership import AttemptOwnershipGuard


@pytest.mark.parametrize("node_id", AGENTIC_NODE_IDS)
def test_legacy_deadlines_cannot_cut_off_a_healthy_workflow(node_id: str) -> None:
    """Both shared and dedicated historical settings must lose their timer."""
    registry = build_agentic_recipe_registry(
        nodes=_agentic_nodes(),
        execution_settings={"timeout_seconds": 120.0, "max_attempts": 1},
        vision_execution_settings={"timeout_seconds": 600.0, "max_attempts": 1},
    )
    assert registry.require(node_id).workflow.timeout is None


@pytest.mark.asyncio
async def test_actual_http_request_has_unlimited_generation_but_bounded_connect() -> (
    None
):
    """Pinned SDK defaults cannot silently restore read/write limits on the wire."""
    clock = Clock()
    audit = Audit(clock)
    sends: list[httpx.Request] = []
    closed: list[bool] = []
    completion = OpenRouterOneSendCompletion(
        transport_factory=lambda: Wire([(200, SUCCESS, {})], sends, closed)
    )
    client = provider_retry.RetryingOpenRouterClient(completion=completion, clock=clock)
    with provider_retry.bind_provider_action(
        context(clock, audit, action_deadline=None)
    ):
        result = await client.acompletion(
            model="openrouter/test-model",
            messages=[{"role": "user", "content": "safe"}],
            tools=[],
            api_key="synthetic-key",
            timeout=3,
        )
    assert len(result.choices) == 1
    assert len(sends) == 1
    assert closed == [True]
    limits = sends[0].extensions["timeout"]
    assert limits["read"] is None
    assert limits["write"] is None
    assert limits["connect"] > 0
    assert limits["pool"] > 0


@pytest.mark.asyncio
async def test_healthy_retry_may_finish_after_retry_scheduling_budget() -> None:
    """Elapsed retry budget decides admission; it must not discard healthy output."""
    clock = Clock()
    audit = Audit(clock)
    sends = []

    async def send(**kwargs: object) -> object:
        sends.append(kwargs)
        if len(sends) == 1:
            raise error(503)
        clock.value += 1000
        return "complete"

    async def unexpected_timeout(
        awaitable: Awaitable[object], _seconds: float
    ) -> object:
        # A cancellation boundary may wrap explicit legacy calls only.
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        message = "A healthy generation acquired an application total deadline"
        raise AssertionError(message)

    client = provider_retry.RetryingOpenRouterClient(
        completion=send,
        clock=clock,
        uniform=lambda _low, _high: 0,
        timeout_runner=unexpected_timeout,
    )
    with provider_retry.bind_provider_action(
        context(clock, audit, action_deadline=None)
    ):
        assert await invoke(client) == "complete"
    assert len(sends) == EXPECTED_SENDS
    assert sends[0]["timeout"] is None
    assert sends[1]["timeout"] is None
    assert audit.finishes[-1].disposition == "success"


@pytest.mark.parametrize("lose_ownership", [False, True])
def test_real_runner_renews_and_cancels_its_provider_scope(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, lose_ownership: bool
) -> None:
    """A live owner finishes beyond the old lease; lost authority cancels local work."""
    mutable = MutableClock(EVALUATED_AT)
    waits = []
    cancelled = []

    class RenewalClock(Clock):
        async def sleep(self, seconds: float) -> None:
            waits.append(seconds)
            mutable.now_value += timedelta(seconds=seconds)
            self.value += seconds
            await asyncio.sleep(0)

    class WaitingLlm(RawVisionLlm):
        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            del llm_request, stream
            self.calls += 1
            try:
                for _ in range(100):
                    await asyncio.sleep(0)
                    if not lose_ownership and len(waits) >= MINIMUM_WAIT_CYCLES:
                        yield self.response
                        return
                message = "Runner did not renew/cancel the waiting provider"
                raise AssertionError(message)
            finally:
                cancelled.append(True)

    model = WaitingLlm(
        model="waiting-goal",
        response=LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text=_goal_output().model_dump_json())]
            ),
            finish_reason=types.FinishReason.STOP,
        ),
    )
    agent = root_agent.model_copy(update={"model": model})
    _, domain, project_id = _goal_runner_system(engine, agent)
    monkeypatch.setattr(domain, "_clock", mutable)
    registry = _goal_registry(agent)
    if lose_ownership:

        def fail_renewal(_guard: AttemptOwnershipGuard) -> None:
            message = "synthetic ownership storage failure"
            raise OSError(message)

        monkeypatch.setattr(domain, "renew_provider_attempt", fail_renewal)
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=TrackingSessionService(),
        ownership_sleep=RenewalClock().sleep,
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="waiting-goal",
            execution_settings={"timeout_seconds": None, "max_attempts": 1},
            lease_seconds=60,
            actor="operator@example.com",
        ),
    )
    position = domain.position(project_id)
    result = runner.run(
        _goal_decision(domain, project_id),
        _goal_runner_input(),
        guards=AdkRunGuards(
            position=position, idempotency_key="long-goal", actor="operator@example.com"
        ),
    )
    assert cancelled == [True]
    assert model.calls == 1
    assert result.ok is not lose_ownership
    with Session(engine) as session:
        attempt = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.idempotency_key == "long-goal"
            )
        ).one()
        turns = session.exec(
            select(ProductGoalInterviewTurn).where(
                ProductGoalInterviewTurn.attempt_fingerprint
                == attempt.attempt_fingerprint
            )
        ).all()
        if lose_ownership:
            assert turns == []
            assert result.error is not None
            assert result.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
        else:
            assert len(turns) == 1
            owner = session.get(
                WorkflowNodeAttemptOwnership, attempt.workflow_node_attempt_id
            )
            assert owner is not None
            assert owner.renewal_revision >= MINIMUM_RENEWALS
            assert mutable.now_value.replace(tzinfo=None) > attempt.lease_expires_at
