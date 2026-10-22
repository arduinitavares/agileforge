"""Product Goal generation regressions using real ADK and isolated domain state."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from litellm import ModelResponse
from sqlmodel import Session, col, select

from adapters.adk.agents.product_goal import root_agent
from adapters.adk.errors import ProductGoalOutputValidationError
from adapters.adk.product_goal_output import validate_product_goal_output
from adapters.adk.runner import AdkRunGuards, AdkWorkflowRunner
from cli import main as cli_main
from cli.workflow_commands import render_workflow_next
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    VisionArtifact,
    VisionArtifactDecision,
)
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.application import _agentic_execution_settings
from services.read_projections import DurableReadProjectionService
from tests.adapters.test_adk_workflow_runner import (
    TrackingSessionService,
    _goal_decision,
    _goal_runner_input,
    _goal_runner_system,
)
from tests.adapters.test_vision_recipe import RawVisionLlm
from utils.runtime_config import ADK_EXECUTION_TRACE_IDENTITY
from workflow.contracts import NodeCategory

if TYPE_CHECKING:
    from pathlib import Path

    from google.adk.events import Event
    from google.adk.sessions import Session as AdkSession
    from sqlalchemy.engine import Engine

PROMPT_TOKENS = 1_411
EXHAUSTED_COMPLETION_TOKENS = 4_096
RECOVERY_PROVIDER_CALLS = 2


def _complete_goal() -> dict[str, object]:
    return {
        "updated_components": {
            "valuable_future_state": "A predictable delivery flow",
            "beneficiary": "Operators",
            "value": "Complete evidence " * 5_000,
            "success_signals": ["Every decision is auditable"],
            "boundaries": ["No provider call in tests"],
        },
        "product_goal_statement": "Operators trust lifecycle evidence.",
        "is_complete": True,
        "clarifying_questions": [],
    }


def _goal_failure(session: Session, project_id: int) -> WorkflowNodeAttemptOutcome:
    return session.exec(
        select(WorkflowNodeAttemptOutcome)
        .join(WorkflowNodeAttempt)
        .where(
            WorkflowNodeAttempt.project_id == project_id,
            WorkflowNodeAttempt.node_id == "goal.interview",
            WorkflowNodeAttemptOutcome.status == "failure",
        )
    ).one()


def _latest_goal_attempt(session: Session, project_id: int) -> WorkflowNodeAttempt:
    attempt = session.exec(
        select(WorkflowNodeAttempt)
        .where(
            WorkflowNodeAttempt.project_id == project_id,
            WorkflowNodeAttempt.node_id == "goal.interview",
        )
        .order_by(col(WorkflowNodeAttempt.workflow_node_attempt_id).desc())
    ).first()
    assert attempt is not None
    return attempt


@pytest.mark.parametrize(
    ("text", "finish_reason", "code"),
    [
        (None, types.FinishReason.MAX_TOKENS, "PRODUCT_GOAL_OUTPUT_INCOMPLETE"),
        ("", types.FinishReason.STOP, "PRODUCT_GOAL_OUTPUT_INCOMPLETE"),
        (" \n", types.FinishReason.STOP, "PRODUCT_GOAL_OUTPUT_INCOMPLETE"),
        (
            '{"private_requirements":',
            types.FinishReason.STOP,
            "PRODUCT_GOAL_OUTPUT_INCOMPLETE",
        ),
        (
            "sensitive-output: not JSON",
            types.FinishReason.STOP,
            "INVALID_PRODUCT_GOAL_PAYLOAD",
        ),
        ("{}", types.FinishReason.STOP, "INVALID_PRODUCT_GOAL_PAYLOAD"),
        (
            json.dumps(_complete_goal()),
            types.FinishReason.MAX_TOKENS,
            "PRODUCT_GOAL_OUTPUT_INCOMPLETE",
        ),
    ],
    ids=[
        "empty-max-tokens",
        "empty-stop",
        "blank-stop",
        "eof",
        "malformed",
        "invalid-schema",
        "complete-json-max-tokens",
    ],
)
def test_incomplete_goal_has_durable_diagnostics_without_business_changes(
    engine: Engine, text: str | None, finish_reason: types.FinishReason, code: str
) -> None:
    """Empty or length-limited output must not become a generic schema failure."""
    raw = RawVisionLlm(
        model="raw-product-goal",
        response=LlmResponse(
            content=types.Content(
                role="model", parts=[] if text is None else [types.Part(text=text)]
            ),
            finish_reason=finish_reason,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=PROMPT_TOKENS,
                candidates_token_count=EXHAUSTED_COMPLETION_TOKENS,
                thoughts_token_count=EXHAUSTED_COMPLETION_TOKENS,
                total_token_count=5_507,
            ),
        ),
    )
    agent = root_agent.model_copy(update={"model": raw})
    runner, domain, project_id = _goal_runner_system(engine, agent)
    with Session(engine) as session:
        visions = [item.model_dump() for item in session.exec(select(VisionArtifact))]
        decisions = [
            item.model_dump() for item in session.exec(select(VisionArtifactDecision))
        ]
        goals_before = len(session.exec(select(ProductGoalArtifact)).all())
        turns_before = len(session.exec(select(ProductGoalInterviewTurn)).all())
    position = domain.position(project_id)
    decision = _goal_decision(domain, project_id)
    guards = AdkRunGuards(
        position=position, idempotency_key="incomplete-goal", actor="operator"
    )

    result = runner.run(decision, _goal_runner_input(), guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == code
    assert "dict_type" not in result.error.message
    assert "private_requirements" not in result.error.message
    assert "sensitive-output" not in result.error.message
    with Session(engine) as session:
        assert len(session.exec(select(ProductGoalArtifact)).all()) == goals_before
        assert len(session.exec(select(ProductGoalInterviewTurn)).all()) == turns_before
        assert [
            item.model_dump() for item in session.exec(select(VisionArtifact))
        ] == visions
        assert [
            item.model_dump() for item in session.exec(select(VisionArtifactDecision))
        ] == decisions
    trace = runner._session_service
    assert isinstance(trace, TrackingSessionService)
    recorded = asyncio.run(
        trace.get_session(
            app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
            user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
            session_id=trace.created_session_ids[0],
        )
    )
    assert recorded is not None
    diagnostic = recorded.state["product_goal_output_diagnostic"]
    assert diagnostic["finish_reason"] == finish_reason.value
    assert diagnostic["prompt_token_count"] == PROMPT_TOKENS
    assert diagnostic["candidates_token_count"] == EXHAUSTED_COMPLETION_TOKENS
    assert diagnostic["thoughts_token_count"] == EXHAUSTED_COMPLETION_TOKENS
    encoded = None if text is None else text.encode("utf-8")
    assert diagnostic["response_bytes"] == (None if encoded is None else len(encoded))
    assert diagnostic["response_sha256"] == (
        None if encoded is None else "sha256:" + hashlib.sha256(encoded).hexdigest()
    )
    # A new process/configuration must replay the stored precise failure first.
    fresh_runner = AdkWorkflowRunner(
        domain=domain,
        registry=runner._registry,
        session_service=TrackingSessionService(),
        config=replace(
            runner._config,
            execution_settings={"timeout_seconds": 9, "max_attempts": 1},
        ),
    )
    replay = fresh_runner.run(decision, _goal_runner_input(), guards=guards)
    assert replay == result.model_copy(update={"replayed": True})
    assert raw.calls == 1
    with Session(engine) as session:
        outcome = _goal_failure(session, project_id)
        assert outcome.failure_code == code
        assert outcome.failure_message == result.error.message
    recovery = _goal_decision(domain, project_id)
    assert recovery.reason_code == "PRODUCT_GOAL_INTERVIEW_FAILED"


def _failed_goal_model() -> RawVisionLlm:
    return RawVisionLlm(
        model="raw-product-goal",
        response=LlmResponse(
            content=types.Content(role="model", parts=[]),
            finish_reason=types.FinishReason.MAX_TOKENS,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1_411,
                candidates_token_count=4_096,
                thoughts_token_count=4_096,
                total_token_count=5_507,
            ),
        ),
    )


def test_goal_diagnostic_is_persisted_with_the_real_invocation(
    engine: Engine, tmp_path: Path
) -> None:
    """Missing user-event capture must not silently discard durable diagnostics."""
    raw = _failed_goal_model()
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": raw})
    )
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'goal-trace.sqlite3'}"
    writer = DatabaseSessionService(db_url=db_url)
    runner._session_service = writer

    result = runner.run(_goal_decision(domain, project_id), _goal_runner_input())

    assert not result.ok
    with Session(engine) as session:
        attempt = _latest_goal_attempt(session, project_id)
        session_id = attempt.attempt_fingerprint

    async def read_with_new_connection() -> None:
        await writer.close()
        reader = DatabaseSessionService(db_url=db_url)
        try:
            stored = await reader.get_session(
                app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
                user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
                session_id=session_id,
            )
            assert stored is not None
            event = next(
                e for e in stored.events if e.author == "product_goal_output_validator"
            )
            user = next(e for e in stored.events if e.author == "user")
            assert event.invocation_id == user.invocation_id
            assert event.invocation_id
            diagnostic = stored.state["product_goal_output_diagnostic"]
            assert (
                event.actions.state_delta["product_goal_output_diagnostic"]
                == diagnostic
            )
            assert diagnostic["finish_reason"] == "MAX_TOKENS"
            assert diagnostic["thoughts_token_count"] == EXHAUSTED_COMPLETION_TOKENS
            assert "generation_settings" in diagnostic
        finally:
            await reader.close()

    asyncio.run(read_with_new_connection())


class _UnavailableDiagnosticTrace(TrackingSessionService):
    async def append_event(self, session: AdkSession, event: Event) -> Event:
        if "product_goal_output_diagnostic" in event.actions.state_delta:
            message = "private-trace-writer-failure"
            raise OSError(message)
        return await super().append_event(session=session, event=event)


def test_goal_trace_writer_failure_preserves_safe_durable_failure(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """An optional trace failure must not replace the actual workflow failure."""
    raw = _failed_goal_model()
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": raw})
    )
    runner._session_service = _UnavailableDiagnosticTrace()

    result = runner.run(_goal_decision(domain, project_id), _goal_runner_input())

    assert result.error is not None
    assert result.error.code.value == "PRODUCT_GOAL_OUTPUT_INCOMPLETE"
    assert "MAX_TOKENS" in result.error.message
    assert "4096" in result.error.message
    assert "private-trace-writer-failure" not in caplog.text
    with Session(engine) as session:
        outcome = _goal_failure(session, project_id)
        assert outcome.failure_code == "PRODUCT_GOAL_OUTPUT_INCOMPLETE"
        assert outcome.failure_message == result.error.message


def test_goal_safe_diagnostic_rejects_untrusted_metadata() -> None:
    """Provider counts/reasons/settings must not smuggle text into diagnostics."""
    with pytest.raises(ProductGoalOutputValidationError) as captured:
        validate_product_goal_output(
            "secret-output",
            finish_reason="untrusted-private-reason",
            usage={
                # These are adversarial count values, not credentials.
                "prompt_token_count": True,  # nosec B105
                "thoughts_token_count": -1,
                "candidates_token_count": "secret-count",  # nosec B105
                "total_token_count": 0,  # nosec B105
            },
            generation_settings={
                "api_key": "secret-key",
                "generation_config": {"max_completion_tokens": 4096},
            },
        )
    diagnostic = captured.value.diagnostic
    assert diagnostic["finish_reason"] is None
    assert diagnostic["prompt_token_count"] is None
    assert diagnostic["thoughts_token_count"] is None
    assert diagnostic["candidates_token_count"] is None
    assert diagnostic["total_token_count"] == 0
    assert "secret" not in json.dumps(diagnostic)
    assert "secret" not in str(captured.value)


def test_failed_goal_keeps_public_status_and_recovery_command(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The typed failure leaves the supported public Goal route usable."""
    raw = _failed_goal_model()
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": raw})
    )
    result = runner.run(_goal_decision(domain, project_id), _goal_runner_input())
    assert result.error is not None
    assert result.error.code.value == "PRODUCT_GOAL_OUTPUT_INCOMPLETE"
    commands = render_workflow_next(domain.position(project_id))["commands"]
    command = next(item for item in commands if item["node_id"] == "goal.interview")
    assert command["reason_code"] == "PRODUCT_GOAL_INTERVIEW_FAILED"
    assert f"goal respond --project-id {project_id}" in command["command"]

    monkeypatch.setattr(cli_main, "runtime_access", lambda **_: nullcontext())
    monkeypatch.setattr(cli_main, "configure_logging", lambda **_: None)
    application = SimpleNamespace(reads=DurableReadProjectionService(engine=engine))
    assert (
        cli_main.main(
            ["goal", "status", "--project-id", str(project_id)], application=application
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["active"] is None
    assert payload["data"]["candidate"] is None
    assert payload["data"]["transcript"] == []
    assert payload["data"]["accepted_vision"] is not None
    assert raw.calls == 1


def test_reasoning_heavy_complete_goal_reaches_the_existing_review_gate(
    engine: Engine,
) -> None:
    """A complete large result retains schema, lineage and human acceptance."""
    raw = RawVisionLlm(
        model="raw-product-goal",
        response=LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text=json.dumps(_complete_goal()))]
            ),
            finish_reason=types.FinishReason.STOP,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1_411,
                candidates_token_count=20_000,
                thoughts_token_count=8_000,
                total_token_count=21_411,
            ),
        ),
    )
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": raw})
    )

    result = runner.run(_goal_decision(domain, project_id), _goal_runner_input())

    assert result.ok is True
    assert raw.calls == 1
    assert any(
        item.node_id == "goal.review" and item.category is NodeCategory.WAITING
        for item in domain.position(project_id).decisions
    )


class _CaptureClient(LiteLLMClient):
    """Replace only the external completion to inspect the composed request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def acompletion(
        self, model: object, messages: object, tools: object, **kwargs: object
    ) -> ModelResponse:
        self.requests.append(
            {"model": model, "messages": messages, "tools": tools, **kwargs}
        )
        return ModelResponse(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ]
        )


def test_goal_attempt_records_the_actual_composed_generation_settings(
    engine: Engine,
) -> None:
    """Configuration provenance must describe the model request, not a live getter."""
    assert isinstance(root_agent.model, LiteLlm)
    client = _CaptureClient()
    model = root_agent.model.model_copy(update={"llm_client": client})
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": model})
    )
    runner._config = replace(
        runner._config, execution_settings=_agentic_execution_settings("goal.interview")
    )

    runner.run(_goal_decision(domain, project_id), _goal_runner_input())

    assert len(client.requests) == 1
    with Session(engine) as session:
        attempt = _latest_goal_attempt(session, project_id)
        config = json.loads(attempt.execution_settings_json)["generation_config"]
    actual = client.requests[0]
    assert config == {
        key: actual[key]
        for key in ("max_tokens", "max_completion_tokens")
        if key in actual
    }


def test_new_goal_recovery_key_can_produce_a_reviewable_candidate(
    engine: Engine,
) -> None:
    """A durable failed receipt must not poison an authorized fresh-key recovery."""
    raw = _failed_goal_model()
    runner, domain, project_id = _goal_runner_system(
        engine, root_agent.model_copy(update={"model": raw})
    )
    failed = runner.run(_goal_decision(domain, project_id), _goal_runner_input())
    assert not failed.ok
    raw.response = LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text=json.dumps(_complete_goal()))]
        ),
        finish_reason=types.FinishReason.STOP,
    )

    result = runner.run(_goal_decision(domain, project_id), _goal_runner_input())

    assert result.ok
    assert raw.calls == RECOVERY_PROVIDER_CALLS
    assert any(
        item.node_id == "goal.review" and item.category is NodeCategory.WAITING
        for item in domain.position(project_id).decisions
    )


@pytest.mark.asyncio
async def test_product_goal_does_not_supply_a_fixed_application_output_cap() -> None:
    """The interviewer must delegate output allowance to the shared provider policy."""
    assert isinstance(root_agent.model, LiteLlm)
    client = _CaptureClient()
    model = root_agent.model.model_copy(update={"llm_client": client})
    request = LlmRequest(
        model=model.model,
        contents=[types.Content(role="user", parts=[types.Part(text="input")])],
        config=root_agent.generate_content_config or types.GenerateContentConfig(),
    )

    responses = [item async for item in model.generate_content_async(request)]

    assert len(responses) == 1
    assert len(client.requests) == 1
    assert client.requests[0].get("max_tokens") is None
    assert client.requests[0].get("max_completion_tokens") is None
