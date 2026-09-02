"""Failure boundaries for durable Specification structuring attempts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from git import Repo
from google.adk.agents import Agent, BaseAgent, InvocationContext
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import (
    BaseSessionService,
    DatabaseSessionService,
    InMemorySessionService,
)
from google.adk.sessions import Session as AdkSession
from google.genai import types
from openai import OpenAIError
from pydantic import Field
from sqlmodel import Session, col, select

from adapters.adk.agents.specification_author import (
    reject_incomplete_specification_output,
    validate_specification_output,
)
from adapters.adk.errors import SpecificationAgenticExecutionError
from adapters.adk.recipes import (
    AgenticRecipeNodes,
    build_agentic_recipe_registry,
)
from adapters.adk.runner import (
    AdkExecutionConfig,
    AdkRunGuards,
    AdkWorkflowRunner,
    SpecificationSourceCheck,
)
from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.product_definition import SpecificationCandidate
from models.repository import RepositoryBinding
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.contracts.specification_authoring import (
    SpecificationStructuringInput,
    SpecificationStructuringOutput,
)
from services.specification_authoring_input import SpecificationStructuringInputService
from services.specification_source_registration import (
    SpecificationSourceRegistrationRequest,
    SpecificationSourceRegistrationService,
)
from services.specs.candidate_contract import load_candidate_contract
from tests.workflow.lifecycle_fixtures import _seed_accepted_vision_and_goal
from utils.agileforge_spec_profile_v2 import canonical_spec_json
from utils.runtime_config import ADK_EXECUTION_TRACE_IDENTITY
from workflow.contracts import TransitionResult, WorkflowError, WorkflowErrorCode
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import RegisterSpecificationSource, RevalidateNodeAttempt

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from sqlalchemy.engine import Engine

    from workflow.contracts import (
        JsonObject,
        NodeDecision,
    )
    from workflow.requests import TransitionRequest

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}


def _issue_200_fixture_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


ISSUE_200_SOURCE: Path = (
    Path(__file__).parents[1] / "fixtures" / "issue_200" / "to-spec-source.md"
)
ISSUE_200_CONTEXT: Path = (
    Path(__file__).parents[1] / "fixtures" / "issue_200" / "CONTEXT.md"
)
ISSUE_200_OUTPUT: Path = (
    Path(__file__).parents[1]
    / "fixtures"
    / "issue_200"
    / "complete-provider-output.json"
)
ISSUE_200_MAX_OUTPUT_TOKENS: int = 32_768
ISSUE_200_EXECUTION_SETTINGS: JsonObject = {
    "timeout_seconds": 5.0,
    "max_attempts": 1,
    "generation_config": {"max_output_tokens": ISSUE_200_MAX_OUTPUT_TOKENS},
}
ISSUE_202_NEGATIVE_CONTRACT_BYTES: bytes = (
    b"- Reject the entire Number List when any parsed value is below zero. The public\n"
    b"  Python operation raises `ValueError` rather than returning a partial sum.\n"
    b"- Format rejection text as `negative numbers not allowed: ` followed by every\n"
    b"  canonical negative value in encounter order, separated by comma and space.\n"
    b"  Preserve duplicate occurrences.\n"
    b"- Install the `string-calculator` command with one positional Number List for\n"
    b"  supported invocations.\n"
    b"- On success, write only the decimal sum and one trailing newline to standard\n"
    b"  output, write nothing to standard error, and exit zero.\n"
    b"- On negative-number rejection, write the Python error text and one trailing\n"
    b"  newline to standard error, write no sum to standard output, and exit nonzero.\n"
)
ISSUE_200_OBSERVED_TRUNCATION_CHARS: int = 20_069
ISSUE_200_SOURCE_BYTES: int = 8_726
ISSUE_200_CONTEXT_BYTES: int = 1_297
ISSUE_200_MIN_NORMALIZED_INPUT_CHARS: int = 13_000
ISSUE_200_MIN_CANONICAL_OUTPUT_CHARS: int = 40_000


class _CountingSpecificationLeaf(BaseAgent):
    """Return one provider result while recording external invocations."""

    response: object
    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append("provider")
        yield Event(author=self.name, output=self.response)


class _FailingSpecificationLeaf(BaseAgent):
    """Raise one provider SDK failure while recording the invocation."""

    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append("provider")
        message = "provider routing failed"
        raise OpenAIError(message)
        yield


class _TypedFailingSpecificationLeaf(BaseAgent):
    """Raise one adapter-owned typed failure with a controlled open code."""

    failure_code: str
    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append("provider")
        raise SpecificationAgenticExecutionError(
            code=self.failure_code,
            message="Untrusted typed Specification failure.",
        )
        yield


class _SpecificationResponseLlm(BaseLlm):
    """Return one deterministic response while capturing the real ADK request."""

    response_text: str
    finish_reason: types.FinishReason
    calls: list[str] = Field(default_factory=list)
    requests: list[object] = Field(default_factory=list, exclude=True)

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Yield a provider response after retaining its exact request contract."""
        del stream
        self.calls.append("provider")
        self.requests.append(llm_request)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=self.response_text)],
            ),
            finish_reason=self.finish_reason,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=4_200,
                candidates_token_count=4_096,
                total_token_count=8_296,
            ),
        )


class _PostProviderDriftingSpecificationLeaf(BaseAgent):
    """Change external source state after provider work returns."""

    response: object
    calls: list[str]
    source_state: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append("provider")
        self.source_state.append("changed")
        yield Event(author=self.name, output=self.response)


class _DriftingSessionService(InMemorySessionService):
    """Change one business fact after trace setup but before the provider call."""

    def __init__(self, *, engine: Engine, project_id: int) -> None:
        super().__init__()
        self._engine = engine
        self._project_id = project_id

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> AdkSession:
        created = await super().create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )
        with Session(self._engine) as session:
            project = session.get(Project, self._project_id)
            assert project is not None
            project.description = "Changed during trace setup."
            session.add(project)
            session.commit()
        return created


class _TestClock:
    """Allow source registration and candidate production at distinct instants."""

    def __init__(self, now_value: datetime) -> None:
        self.now_value = now_value

    def now(self) -> datetime:
        return self.now_value


def _unused_leaf(name: str) -> _CountingSpecificationLeaf:
    return _CountingSpecificationLeaf(name=name, response={}, calls=[])


def _valid_output() -> JsonObject:
    return {
        "payload": {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.attempt-boundary",
            "title": "Attempt boundary",
            "summary": "Bind provider work to current durable facts.",
            "problem_statement": "Facts can drift after an attempt starts.",
            "items": [
                {
                    "id": "REQ.attempt-boundary",
                    "type": "REQ",
                    "title": "Revalidate authority",
                    "statement": (
                        "The host MUST revalidate the attempt before provider work."
                    ),
                    "level": "MUST",
                    "verification": "integration-test",
                    "acceptance": ["Stale attempts make zero provider calls."],
                }
            ],
        }
    }


def _invalid_model_output(
    provider_output: JsonObject,
) -> object:
    """Bypass the production schema to exercise recipe failure classification."""
    return provider_output


def _system(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    leaf: BaseAgent,
    *,
    session_service: BaseSessionService | None = None,
    source_check: SpecificationSourceCheck | None = None,
    execution_settings: JsonObject = EXECUTION_SETTINGS,
    source_bytes: bytes = b"# Exact external Specification\n",
    context_bytes: bytes | None = None,
    structuring_time: datetime = NOW,
) -> tuple[
    AdkWorkflowRunner,
    WorkflowDomain,
    int,
    NodeDecision,
    JsonObject,
    AdkRunGuards,
]:
    repository = tmp_path / "registered-specification-source"
    repository.mkdir()
    (repository / "SPECIFICATION.md").write_bytes(source_bytes)
    tracked_paths = ["SPECIFICATION.md"]
    if context_bytes is not None:
        (repository / "CONTEXT.md").write_bytes(context_bytes)
        tracked_paths.append("CONTEXT.md")
    with Repo.init(repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Structuring Attempt Test")
            config.set_value("user", "email", "structuring@example.test")
        repo.index.add(tracked_paths)
        repo.index.commit("registered source")
    probe = GitPythonRepositoryProbe()
    observed = probe.inspect(repository)
    with Session(engine) as session:
        project = Project(name="Specification attempt boundary")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        _seed_accepted_vision_and_goal(
            session,
            project_id=project_id,
            recorded_at=NOW - timedelta(minutes=1),
        )
        binding = RepositoryBinding(
            project_id=project_id,
            worktree_path=observed.worktree_path,
            common_git_dir=observed.common_git_dir,
            head_sha=observed.head_sha,
            branch_name=observed.branch_name,
            detached_head=observed.detached_head,
            dirty=observed.dirty,
            status_fingerprint=observed.status_fingerprint,
            status_entries_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.status_entries]
            ),
            remotes_json=canonical_json(list(observed.remotes)),
            warnings_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.warnings]
            ),
            probe_version=observed.probe_version,
            inspected_at=NOW - timedelta(seconds=30),
            recorded_by="operator@example.test",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            vision_interview=_unused_leaf("unused_vision_interview"),
            vision_repair=_unused_leaf("unused_vision_repair"),
            product_goal=_unused_leaf("unused_product_goal"),
            specification_structurer=leaf,
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=execution_settings,
    )
    structuring_input_service = SpecificationStructuringInputService(
        engine=engine,
        repository_probe=probe,
    )
    runner_source_check = (
        structuring_input_service.revalidate_sources
        if source_check is None
        else source_check
    )
    clock = _TestClock(NOW)
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=clock,
        adk_recipe_registry=registry,
        specification_registration_check=lambda _prepared: None,
        specification_source_check=source_check,
    )
    prepared = SpecificationSourceRegistrationService(
        engine=engine,
        repository_probe=probe,
    ).prepare(
        SpecificationSourceRegistrationRequest(
            project_id=project_id,
            source_path="SPECIFICATION.md",
            preparation_capability="grill-with-docs",
            idempotency_key="register-source-attempt-boundary",
            actor="operator@example.test",
        )
    )
    source_position = domain.position(project_id)
    source_decision = next(
        item
        for item in source_position.decisions
        if item.node_id == "specification.source.register"
    )
    registered = domain.transition(
        RegisterSpecificationSource(
            project_id=project_id,
            graph_version=source_position.graph_version,
            fact_fingerprint=source_position.fact_fingerprint,
            decision_fingerprint=source_decision.decision_fingerprint,
            idempotency_key="register-source-attempt-boundary",
            actor="operator@example.test",
            accepted_vision_artifact_id=prepared.accepted_vision_artifact_id,
            accepted_product_goal_artifact_id=(
                prepared.accepted_product_goal_artifact_id
            ),
            repository_binding_id=prepared.repository_binding_id,
            repository_binding_fingerprint=(prepared.repository_binding_fingerprint),
            capture_request_fingerprint=prepared.request_fingerprint,
            source_fingerprint=prepared.source_fingerprint,
            bundle=prepared.bundle,
        )
    )
    assert registered.ok is True
    clock.now_value = structuring_time
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=(
            InMemorySessionService()
            if session_service is None
            else session_service
        ),
        specification_source_check=runner_source_check,
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/specification-structurer",
            execution_settings=execution_settings,
            lease_seconds=60,
            actor="operator@example.com",
            correlation_id="issue-199-attempt-boundary",
        ),
    )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "specification.structure"
    )
    normalized_input = structuring_input_service.build(
        project_id=project_id,
        decision=decision,
    )
    guards = AdkRunGuards(
        position=position,
        idempotency_key="structure-specification-attempt-boundary",
        actor="operator@example.com",
        correlation_id="issue-199-attempt-boundary",
    )
    return runner, domain, project_id, decision, normalized_input, guards


def _latest_outcome(
    session: Session,
    *,
    project_id: int,
) -> WorkflowNodeAttemptOutcome:
    attempt = session.exec(
        select(WorkflowNodeAttempt)
        .where(
            col(WorkflowNodeAttempt.project_id) == project_id,
            col(WorkflowNodeAttempt.node_id) == "specification.structure",
        )
        .order_by(col(WorkflowNodeAttempt.workflow_node_attempt_id).desc())
    ).one()
    assert attempt.workflow_node_attempt_id is not None
    return session.exec(
        select(WorkflowNodeAttemptOutcome).where(
            col(WorkflowNodeAttemptOutcome.project_id) == project_id,
            col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id)
            == attempt.workflow_node_attempt_id,
        )
    ).one()


def _latest_attempt(
    session: Session,
    *,
    project_id: int,
) -> WorkflowNodeAttempt:
    return session.exec(
        select(WorkflowNodeAttempt)
        .where(
            col(WorkflowNodeAttempt.project_id) == project_id,
            col(WorkflowNodeAttempt.node_id) == "specification.structure",
        )
        .order_by(col(WorkflowNodeAttempt.workflow_node_attempt_id).desc())
    ).one()


def _dangling_output() -> JsonObject:
    output = deepcopy(_valid_output())
    payload = cast("JsonObject", output["payload"])
    payload["relations"] = [
        {"from": "REQ.attempt-boundary", "type": "tracks", "to": "RISK.missing-source"}
    ]
    return output


def test_real_leaf_dangling_endpoint_is_invalid_payload(
    engine: Engine, tmp_path: Path
) -> None:
    """Classify a dangling relation graph as an invalid payload error."""
    model = _SpecificationResponseLlm(
        model="fake/issue-245",
        response_text=json.dumps(_dangling_output()),
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="issue_245_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return the supplied synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )
    runner, _, project_id, decision, frozen, guards = _system(engine, tmp_path, leaf)
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code.value == "INVALID_SPECIFICATION_PAYLOAD"
    assert model.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "INVALID_SPECIFICATION_PAYLOAD"
        assert not session.exec(select(SpecificationCandidate)).all()


@pytest.mark.parametrize(
    (
        "response_text",
        "finish_reason",
        "callback",
        "expected_code",
        "expected_message_contains",
    ),
    [
        pytest.param(
            json.dumps(_dangling_output()),
            types.FinishReason.STOP,
            validate_specification_output,
            WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD,
            "Unknown relation endpoint: RISK.missing-source.",
            id="dangling-relation-endpoint",
        ),
        pytest.param(
            (
                '{"payload": {"schema_version": "agileforge.spec.v2", '
                '"title": "Missing fields"}}'
            ),
            types.FinishReason.STOP,
            validate_specification_output,
            WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD,
            "Specification structurer returned an invalid v2 payload.",
            id="missing-required-fields",
        ),
        pytest.param(
            '{"payload": invalid}',
            types.FinishReason.STOP,
            validate_specification_output,
            WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD,
            "Specification structurer returned an invalid v2 payload.",
            id="malformed-json-non-eof",
        ),
        pytest.param(
            '{"payload": {"schema_version": "agileforge.spec.v1"}}',
            types.FinishReason.STOP,
            validate_specification_output,
            WorkflowErrorCode.UNSUPPORTED_SPECIFICATION_SCHEMA,
            "Specification structurer returned an unsupported schema.",
            id="explicit-v1-schema",
        ),
        pytest.param(
            '{"payload":',
            types.FinishReason.STOP,
            reject_incomplete_specification_output,
            WorkflowErrorCode.SPECIFICATION_OUTPUT_INCOMPLETE,
            "Specification structurer returned incomplete output.",
            id="cut-off-json-old-callback",
        ),
        pytest.param(
            '{"payload":',
            types.FinishReason.STOP,
            validate_specification_output,
            WorkflowErrorCode.SPECIFICATION_OUTPUT_INCOMPLETE,
            "Specification structurer returned incomplete output.",
            id="cut-off-json-new-callback",
        ),
        pytest.param(
            json.dumps(_valid_output()),
            types.FinishReason.MAX_TOKENS,
            reject_incomplete_specification_output,
            WorkflowErrorCode.SPECIFICATION_OUTPUT_INCOMPLETE,
            "Specification structurer returned incomplete output.",
            id="valid-json-max-tokens-old-callback",
        ),
        pytest.param(
            json.dumps(_valid_output()),
            types.FinishReason.MAX_TOKENS,
            validate_specification_output,
            WorkflowErrorCode.SPECIFICATION_OUTPUT_INCOMPLETE,
            "Specification structurer returned incomplete output.",
            id="valid-json-max-tokens-new-callback",
        ),
        pytest.param(
            "",
            types.FinishReason.SAFETY,
            validate_specification_output,
            WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD,
            "Specification structurer returned an invalid v2 payload.",
            id="empty-response-safety",
        ),
        pytest.param(
            "",
            types.FinishReason.OTHER,
            validate_specification_output,
            WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD,
            "Specification structurer returned an invalid v2 payload.",
            id="empty-response-other",
        ),
    ],
)
def test_real_runner_output_validation_classification_matrix(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    response_text: str,
    finish_reason: types.FinishReason,
    callback: Callable[[CallbackContext, LlmResponse], None],
    expected_code: WorkflowErrorCode,
    expected_message_contains: str,
) -> None:
    """Validate real-runner behavior across all generated-output failure classes."""
    model = _SpecificationResponseLlm(
        model="fake/classification-matrix",
        response_text=response_text,
        finish_reason=finish_reason,
    )
    leaf = Agent(
        name="matrix_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=callback,
    )
    runner, _, project_id, decision, frozen, guards = _system(engine, tmp_path, leaf)
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is expected_code
    assert expected_message_contains in result.error.message
    assert model.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == expected_code.value
        assert not session.exec(select(SpecificationCandidate)).all()


def test_fake_leaf_unallowlisted_typed_code_falls_back_to_producer_failed(
    engine: Engine, tmp_path: Path
) -> None:
    """An unallowlisted typed error must fall back to producer-failed."""

    class _UnallowlistedErrorLeaf(BaseAgent):
        def __init__(self) -> None:
            super().__init__(name="unallowlisted_leaf")

        async def _run_async_impl(
            self, ctx: InvocationContext
        ) -> AsyncGenerator[Event, None]:
            del ctx
            raise SpecificationAgenticExecutionError(
                code="UNKNOWN_CUSTOM_CODE",
                message="Something strange happened.",
            )
            yield Event()  # pragma: no cover

    runner, _, project_id, decision, frozen, guards = _system(
        engine, tmp_path, _UnallowlistedErrorLeaf()
    )
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.SPECIFICATION_PRODUCER_FAILED
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "SPECIFICATION_PRODUCER_FAILED"


def test_real_runner_max_attempts_two_makes_single_dispatch_on_invalid_output(
    engine: Engine, tmp_path: Path
) -> None:
    """Configured max_attempts=2 must not cause retry on terminal output error."""
    model = _SpecificationResponseLlm(
        model="fake/max-attempts-two",
        response_text=json.dumps(_dangling_output()),
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="max_attempts_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )
    settings: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 2}
    runner, _, _project_id, decision, frozen, guards = _system(
        engine, tmp_path, leaf, execution_settings=settings
    )
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD
    assert model.calls == ["provider"]


def test_source_drift_after_invalid_output_post_call_revalidation_wins(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-call revalidation must detect fact drift and obsolete the attempt."""
    model = _SpecificationResponseLlm(
        model="fake/drift-after-invalid",
        response_text=json.dumps(_dangling_output()),
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="drift_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )
    runner, domain, project_id, decision, frozen, guards = _system(
        engine, tmp_path, leaf
    )
    transition = domain.transition
    drifted = False

    def drift_after_provider(request: TransitionRequest) -> TransitionResult:
        nonlocal drifted
        if (
            request.kind == "revalidate_node_attempt"
            and bool(model.calls)
            and not drifted
        ):
            with Session(engine) as session:
                project = session.get(Project, project_id)
                assert project is not None
                project.description = "Drifted after model returned."
                session.add(project)
                session.commit()
            drifted = True
        return transition(request)

    monkeypatch.setattr(domain, "transition", drift_after_provider)
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    assert model.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


@pytest.mark.parametrize(
    "raw_bytes",
    [
        b"# Source\nDescription.\n",
        b"# Source\r\nDescription.\r\n",
    ],
)
def test_real_runner_preserves_exact_source_bytes_lf_and_crlf(
    engine: Engine, tmp_path: Path, raw_bytes: bytes
) -> None:
    """Input assembly must preserve exact LF and CRLF source bytes."""
    leaf = _unused_leaf("unused_structurer")
    _, _, _, _, frozen, _ = _system(
        engine, tmp_path, leaf, source_bytes=raw_bytes
    )
    parsed_input = SpecificationStructuringInput.model_validate(frozen)
    assert parsed_input.registered_source.source.text.encode("utf-8") == raw_bytes


def test_fact_drift_after_start_obsoletes_attempt_before_provider(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revalidate current business facts immediately before provider work."""
    leaf = _CountingSpecificationLeaf(
        name="counting_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    runner, domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )
    transition = domain.transition
    drifted = False

    def drift_before_preflight(request: TransitionRequest) -> TransitionResult:
        nonlocal drifted
        if request.kind == "revalidate_node_attempt" and not drifted:
            with Session(engine) as session:
                project = session.get(Project, project_id)
                assert project is not None
                project.description = "Changed after StartNodeAttempt committed."
                session.add(project)
                session.commit()
            drifted = True
        return transition(request)

    monkeypatch.setattr(domain, "transition", drift_before_preflight)

    result = runner.run(
        decision,
        normalized_input,
        guards=guards,
    )
    replay = runner.run(
        decision,
        normalized_input,
        guards=guards,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    assert replay == result.model_copy(update={"replayed": True})
    assert leaf.calls == []
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


def test_provider_failure_uses_specification_producer_code_durably(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Keep Specification producer failures distinct from generic ADK failures."""
    leaf = _FailingSpecificationLeaf(
        name="failing_specification_structurer",
        calls=[],
    )
    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )

    result = runner.run(decision, normalized_input, guards=guards)
    replay = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "SPECIFICATION_PRODUCER_FAILED"
    assert replay == result.model_copy(update={"replayed": True})
    assert leaf.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "SPECIFICATION_PRODUCER_FAILED"
        assert not session.exec(select(SpecificationCandidate)).all()


@pytest.mark.parametrize(
    "failure_code",
    ["UNKNOWN_SPECIFICATION_FAILURE", WorkflowErrorCode.PROJECT_NOT_FOUND.value],
)
def test_unapproved_typed_failure_code_fails_closed_and_replays_exactly(
    engine: Engine,
    tmp_path: Path,
    failure_code: str,
) -> None:
    """Close attempts generically when an adapter emits an unapproved open code."""
    leaf = _TypedFailingSpecificationLeaf(
        name="typed_failing_specification_structurer",
        failure_code=failure_code,
        calls=[],
    )
    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )

    result = runner.run(decision, normalized_input, guards=guards)
    replay = runner.run(decision, normalized_input, guards=guards)

    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.SPECIFICATION_PRODUCER_FAILED
    assert result.error.message == "Specification structurer provider execution failed."
    assert replay == result.model_copy(update={"replayed": True})
    assert leaf.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "SPECIFICATION_PRODUCER_FAILED"
        assert outcome.failure_message == result.error.message
        assert not session.exec(select(SpecificationCandidate)).all()


@pytest.mark.parametrize(
    ("truncate", "finish_reason"),
    [
        pytest.param(True, types.FinishReason.MAX_TOKENS, id="length-metadata"),
        pytest.param(True, types.FinishReason.STOP, id="syntactic-incompleteness"),
        pytest.param(False, types.FinishReason.MAX_TOKENS, id="valid-json-at-limit"),
    ],
)
def test_incomplete_realistic_response_uses_actionable_durable_failure(
    engine: Engine,
    tmp_path: Path,
    truncate: bool,
    finish_reason: types.FinishReason,
) -> None:
    """Classify truncation before closed-schema validation can hide its evidence."""
    complete_output = ISSUE_200_OUTPUT.read_text(encoding="utf-8")
    response_text = (
        complete_output[:ISSUE_200_OBSERVED_TRUNCATION_CHARS]
        if truncate
        else complete_output
    )
    if truncate:
        assert len(response_text) == ISSUE_200_OBSERVED_TRUNCATION_CHARS
    else:
        SpecificationStructuringOutput.model_validate_json(response_text)
    model = _SpecificationResponseLlm(
        model="fake/length-limited",
        response_text=response_text,
        finish_reason=finish_reason,
    )
    leaf = Agent(
        name="length_limited_specification_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return one complete canonical Specification payload.",
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=ISSUE_200_MAX_OUTPUT_TOKENS
        ),
        mode="single_turn",
        after_model_callback=reject_incomplete_specification_output,
    )
    source_bytes = _issue_200_fixture_bytes(ISSUE_200_SOURCE)
    context_bytes = _issue_200_fixture_bytes(ISSUE_200_CONTEXT)
    assert len(source_bytes) == ISSUE_200_SOURCE_BYTES
    assert hashlib.sha256(source_bytes).hexdigest() == (
        "7d1cb963d06f9e40c82204bc32093b505f6b10bc46027162104b05b4a0ba507a"
    )
    assert len(context_bytes) == ISSUE_200_CONTEXT_BYTES
    assert hashlib.sha256(context_bytes).hexdigest() == (
        "7f3d98698f2741a3a200a7558c98ee0415bbd670c8184c406cc854db44de64d7"
    )
    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
        execution_settings=ISSUE_200_EXECUTION_SETTINGS,
        source_bytes=source_bytes,
        context_bytes=context_bytes,
        structuring_time=NOW + timedelta(seconds=1),
    )
    assert len(canonical_json(normalized_input)) >= ISSUE_200_MIN_NORMALIZED_INPUT_CHARS

    result = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "SPECIFICATION_OUTPUT_INCOMPLETE"
    assert result.error.message == (
        "Specification structurer returned incomplete output. Increase "
        "SPECIFICATION_STRUCTURER_MAX_TOKENS or select a provider that can return "
        "the complete structured payload, then retry Structure Specification."
    )
    assert model.calls == ["provider"]

    replayed = runner.run(decision, normalized_input, guards=guards)

    assert replayed == result.model_copy(update={"replayed": True})
    assert model.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "SPECIFICATION_OUTPUT_INCOMPLETE"
        assert outcome.failure_message == result.error.message
        attempt = _latest_attempt(session, project_id=project_id)
        assert attempt.execution_settings_json == canonical_json(
            ISSUE_200_EXECUTION_SETTINGS
        )
        assert not session.exec(select(SpecificationCandidate)).all()


def test_complete_realistic_response_persists_one_exact_canonical_candidate(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Structure the unshortened issue fixture through the actual ADK schema path."""
    source_bytes = _issue_200_fixture_bytes(ISSUE_200_SOURCE)
    context_bytes = _issue_200_fixture_bytes(ISSUE_200_CONTEXT)
    output_bytes = _issue_200_fixture_bytes(ISSUE_200_OUTPUT)
    expected = SpecificationStructuringOutput.model_validate_json(output_bytes)
    assert len(output_bytes) > ISSUE_200_OBSERVED_TRUNCATION_CHARS
    assert (
        len(canonical_json(expected.model_dump(mode="json")))
        > ISSUE_200_MIN_CANONICAL_OUTPUT_CHARS
    )
    model = _SpecificationResponseLlm(
        model="fake/complete-specification",
        response_text=output_bytes.decode("utf-8"),
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="complete_specification_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return one complete canonical Specification payload.",
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=ISSUE_200_MAX_OUTPUT_TOKENS
        ),
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )

    def unchanged_source(_project_id: int, _input: JsonObject) -> None:
        """Avoid a nested SQLite-memory session after exact input construction."""

    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
        source_check=unchanged_source,
        execution_settings=ISSUE_200_EXECUTION_SETTINGS,
        source_bytes=source_bytes,
        context_bytes=context_bytes,
        structuring_time=NOW + timedelta(seconds=1),
    )
    contract = SpecificationStructuringInput.model_validate(normalized_input)
    structuring_source_bytes = contract.registered_source.source.text.encode("utf-8")
    assert ISSUE_202_NEGATIVE_CONTRACT_BYTES in source_bytes
    assert structuring_source_bytes == source_bytes
    assert ISSUE_202_NEGATIVE_CONTRACT_BYTES in structuring_source_bytes
    assert contract.registered_source.context.document is not None
    assert (
        contract.registered_source.context.document.text.encode("utf-8")
        == context_bytes
    )

    result = runner.run(decision, normalized_input, guards=guards)

    assert result.ok
    assert model.calls == ["provider"]
    assert len(model.requests) == 1
    request = cast("LlmRequest", model.requests[0])
    assert request.config.max_output_tokens == ISSUE_200_MAX_OUTPUT_TOKENS
    assert request.config.response_mime_type == "application/json"
    assert request.config.response_schema is SpecificationStructuringOutput
    with Session(engine) as session:
        candidates = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).all()
        assert len(candidates) == 1
        candidate = candidates[0]
        payload, envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
        assert canonical_spec_json(payload) == canonical_spec_json(expected.payload)
        attempt = _latest_attempt(session, project_id=project_id)
        assert attempt.execution_settings_json == canonical_json(
            ISSUE_200_EXECUTION_SETTINGS
        )
        assert envelope.attempt_fingerprint == attempt.attempt_fingerprint
        assert envelope.model_configuration_fingerprint == canonical_hash(
            {
                "model_id": attempt.model_id,
                "execution_settings": ISSUE_200_EXECUTION_SETTINGS,
            }
        )


def test_trace_setup_drift_is_revalidated_immediately_before_provider(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Trace setup cannot open a stale-call window after the authority check."""
    leaf = _CountingSpecificationLeaf(
        name="trace_drift_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )
    runner._session_service = _DriftingSessionService(
        engine=engine,
        project_id=project_id,
    )

    result = runner.run(decision, normalized_input, guards=guards)

    assert leaf.calls == []
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


def test_input_validation_drift_is_revalidated_at_leaf_boundary(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recheck after host validation and immediately before the leaf call."""
    leaf = _CountingSpecificationLeaf(
        name="leaf_boundary_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )
    original_validate = SpecificationStructuringInput.model_validate
    drifted = False

    def validate_then_drift(
        cls: type[SpecificationStructuringInput],
        value: object,
    ) -> SpecificationStructuringInput:
        del cls
        nonlocal drifted
        validated = original_validate(value)
        if not drifted:
            with Session(engine) as session:
                project = session.get(Project, project_id)
                assert project is not None
                project.description = "Changed during host input validation."
                session.add(project)
                session.commit()
            drifted = True
        return validated

    monkeypatch.setattr(
        SpecificationStructuringInput,
        "model_validate",
        classmethod(validate_then_drift),
    )

    result = runner.run(decision, normalized_input, guards=guards)

    assert leaf.calls == []
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    with Session(engine) as session:
        assert _latest_outcome(session, project_id=project_id).status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


@pytest.mark.parametrize("provider_fails", [False, True])
def test_late_revalidation_replays_terminal_attempt_without_overwrite(
    engine: Engine,
    tmp_path: Path,
    provider_fails: bool,
) -> None:
    """A late check must preserve both successful and failed terminal truth."""
    leaf: BaseAgent
    if provider_fails:
        leaf = _FailingSpecificationLeaf(
            name="terminal_failure_specification_structurer",
            calls=[],
        )
    else:
        leaf = _CountingSpecificationLeaf(
            name="terminal_success_specification_structurer",
            response=_valid_output(),
            calls=[],
        )
    runner, domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )
    terminal = runner.run(decision, normalized_input, guards=guards)
    with Session(engine) as session:
        attempt = _latest_attempt(session, project_id=project_id)
        assert attempt.workflow_node_attempt_id is not None
        attempt_id = attempt.workflow_node_attempt_id
        attempt_fingerprint = attempt.attempt_fingerprint
        outcome_status = _latest_outcome(session, project_id=project_id).status

    late = domain.transition(
        RevalidateNodeAttempt(
            project_id=project_id,
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            idempotency_key=f"late-revalidation-{provider_fails}",
            actor="review@example.com",
        )
    )
    replay = runner.run(decision, normalized_input, guards=guards)

    expected_replay = terminal.model_copy(update={"replayed": True})
    assert late == expected_replay
    assert replay == expected_replay
    with Session(engine) as session:
        assert _latest_outcome(session, project_id=project_id).status == outcome_status


def test_terminal_revalidation_replay_never_reenters_provider(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-terminal success result stops this worker at the leaf boundary."""
    leaf = _CountingSpecificationLeaf(
        name="terminal_replay_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    source_checks: list[str] = []

    def source_check(
        project_id: int,
        persisted_input: JsonObject,
    ) -> None:
        del project_id, persisted_input
        source_checks.append("source")

    runner, domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
        source_check=source_check,
    )
    transition = domain.transition

    def terminal_replay(request: TransitionRequest) -> TransitionResult:
        if request.kind == "revalidate_node_attempt":
            return TransitionResult(
                ok=True,
                replayed=True,
                applied_node_id="specification.structure",
                output={"status": "success"},
                position=domain.position(project_id),
            )
        return transition(request)

    monkeypatch.setattr(domain, "transition", terminal_replay)

    result = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is True
    assert result.replayed is True
    assert source_checks == []
    assert leaf.calls == []


def test_revalidation_exception_records_replayable_generic_external_failure(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authority-store failure is not a Specification producer failure."""
    leaf = _CountingSpecificationLeaf(
        name="revalidation_exception_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    runner, domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )
    transition = domain.transition

    def fail_revalidation(request: TransitionRequest) -> TransitionResult:
        if request.kind == "revalidate_node_attempt":
            message = "authority store unavailable"
            raise RuntimeError(message)
        return transition(request)

    monkeypatch.setattr(domain, "transition", fail_revalidation)

    result = runner.run(decision, normalized_input, guards=guards)
    replay = runner.run(decision, normalized_input, guards=guards)

    assert leaf.calls == []
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "EXTERNAL_EXECUTION_FAILED"
    assert replay == result.model_copy(update={"replayed": True})
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "ADK_EXECUTION_FAILED"
        assert not session.exec(select(SpecificationCandidate)).all()


def test_source_check_stale_error_obsoletes_attempt_before_provider(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Close the exact attempt when the leaf-boundary repository re-probe is stale."""
    leaf = _CountingSpecificationLeaf(
        name="stale_source_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    source_checks: list[tuple[int, JsonObject]] = []

    def stale_source(
        project_id: int,
        persisted_input: JsonObject,
    ) -> WorkflowError:
        source_checks.append((project_id, persisted_input))
        return WorkflowError(
            code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            message="Repository evidence changed before provider invocation.",
        )

    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
        source_check=stale_source,
    )

    result = runner.run(decision, normalized_input, guards=guards)
    replay = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    assert replay == result.model_copy(update={"replayed": True})
    assert source_checks == [(project_id, normalized_input)]
    assert leaf.calls == []
    with Session(engine) as session:
        assert _latest_outcome(session, project_id=project_id).status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


def test_source_check_exception_records_generic_failure_without_provider(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Do not misclassify a repository re-probe exception as producer failure."""
    leaf = _CountingSpecificationLeaf(
        name="source_exception_specification_structurer",
        response=_valid_output(),
        calls=[],
    )

    def fail_source_check(
        project_id: int,
        persisted_input: JsonObject,
    ) -> None:
        del project_id, persisted_input
        message = "repository probe unavailable"
        raise RuntimeError(message)

    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
        source_check=fail_source_check,
    )

    result = runner.run(decision, normalized_input, guards=guards)
    replay = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "EXTERNAL_EXECUTION_FAILED"
    assert replay == result.model_copy(update={"replayed": True})
    assert leaf.calls == []
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "ADK_EXECUTION_FAILED"


def test_post_provider_source_drift_obsoletes_before_candidate_completion(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Re-probe sources after one provider call and before business completion."""
    leaf = _PostProviderDriftingSpecificationLeaf(
        name="post_provider_drift_specification_structurer",
        response=_valid_output(),
        calls=[],
        source_state=[],
    )
    source_checks: list[str | None] = []

    def source_check(
        checked_project_id: int,
        persisted_input: JsonObject,
    ) -> WorkflowError | None:
        del checked_project_id, persisted_input
        observed = None if not leaf.source_state else leaf.source_state[-1]
        source_checks.append(observed)
        if observed == "changed":
            return WorkflowError(
                code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                message="Repository evidence changed during provider execution.",
            )
        return None

    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
        source_check=source_check,
    )
    result = runner.run(decision, normalized_input, guards=guards)
    replay = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    assert replay == result.model_copy(update={"replayed": True})
    assert source_checks == [None, "changed"]
    assert leaf.calls == ["provider"]
    with Session(engine) as session:
        assert _latest_outcome(session, project_id=project_id).status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


def test_provider_retry_revalidates_facts_before_second_leaf_call(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider retry cannot reuse a successful earlier authority check."""
    leaf = _CountingSpecificationLeaf(
        name="retry_drift_specification_structurer",
        response={"payload": {"schema_version": "agileforge.spec.v2"}},
        calls=[],
    )
    retry_settings: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 2}
    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
        execution_settings=retry_settings,
    )
    original_validate = SpecificationStructuringOutput.model_validate
    drifted = False

    def validate_after_fact_drift(
        cls: type[SpecificationStructuringOutput],
        value: object,
    ) -> SpecificationStructuringOutput:
        del cls
        nonlocal drifted
        if not drifted:
            with Session(engine) as session:
                project = session.get(Project, project_id)
                assert project is not None
                project.description = "Changed after invalid provider output."
                session.add(project)
                session.commit()
            drifted = True
        return original_validate(value)

    monkeypatch.setattr(
        SpecificationStructuringOutput,
        "model_validate",
        classmethod(validate_after_fact_drift),
    )

    result = runner.run(decision, normalized_input, guards=guards)
    replay = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    assert replay == result.model_copy(update={"replayed": True})
    assert leaf.calls == ["provider"]
    with Session(engine) as session:
        assert _latest_outcome(session, project_id=project_id).status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


def test_persisted_input_tampering_obsoletes_before_provider(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored input bytes must still match their start-time fingerprint."""
    leaf = _CountingSpecificationLeaf(
        name="tampered_input_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    runner, domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )
    transition = domain.transition

    def tamper_before_preflight(request: TransitionRequest) -> TransitionResult:
        if request.kind == "revalidate_node_attempt":
            with Session(engine) as session:
                attempt = session.exec(
                    select(WorkflowNodeAttempt).where(
                        col(WorkflowNodeAttempt.project_id) == project_id,
                        col(WorkflowNodeAttempt.node_id) == "specification.structure",
                    )
                ).one()
                attempt.normalized_input_json = '{"tampered":true}'
                session.add(attempt)
                session.commit()
        return transition(request)

    monkeypatch.setattr(domain, "transition", tamper_before_preflight)

    result = runner.run(decision, normalized_input, guards=guards)

    assert leaf.calls == []
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    with Session(engine) as session:
        assert _latest_outcome(session, project_id=project_id).status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


def test_newer_competing_attempt_obsoletes_old_attempt_before_provider(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the latest exact node-instance attempt retains provider authority."""
    leaf = _CountingSpecificationLeaf(
        name="competing_attempt_specification_structurer",
        response=_valid_output(),
        calls=[],
    )
    runner, domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )
    transition = domain.transition

    def compete_before_preflight(request: TransitionRequest) -> TransitionResult:
        if request.kind == "revalidate_node_attempt":
            with Session(engine) as session:
                original = session.exec(
                    select(WorkflowNodeAttempt).where(
                        col(WorkflowNodeAttempt.project_id) == project_id,
                        col(WorkflowNodeAttempt.node_id) == "specification.structure",
                    )
                ).one()
                session.add(
                    WorkflowNodeAttempt(
                        project_id=original.project_id,
                        node_id=original.node_id,
                        instance_key=original.instance_key,
                        graph_version=original.graph_version,
                        fact_fingerprint=original.fact_fingerprint,
                        business_fact_fingerprint=original.business_fact_fingerprint,
                        decision_fingerprint=original.decision_fingerprint,
                        normalized_input_json=original.normalized_input_json,
                        input_fingerprint=original.input_fingerprint,
                        model_id=original.model_id,
                        execution_settings_json=original.execution_settings_json,
                        idempotency_key="competing-structuring-attempt",
                        actor=original.actor,
                        correlation_id="competing-structuring-attempt",
                        started_at=original.started_at,
                        lease_expires_at=original.lease_expires_at,
                        attempt_fingerprint="sha256:" + ("f" * 64),
                    )
                )
                session.commit()
        return transition(request)

    monkeypatch.setattr(domain, "transition", compete_before_preflight)

    result = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "STALE_SPECIFICATION_INPUT"
    assert leaf.calls == []
    with Session(engine) as session:
        original = session.exec(
            select(WorkflowNodeAttempt)
            .where(col(WorkflowNodeAttempt.node_id) == "specification.structure")
            .order_by(col(WorkflowNodeAttempt.workflow_node_attempt_id))
        ).first()
        assert original is not None
        assert original.workflow_node_attempt_id is not None
        outcome = session.exec(
            select(WorkflowNodeAttemptOutcome).where(
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id)
                == original.workflow_node_attempt_id
            )
        ).one()
        assert outcome.status == "obsolete"
        assert not session.exec(select(SpecificationCandidate)).all()


@pytest.mark.parametrize(
    ("provider_output", "expected_code"),
    [
        (
            {"payload": {"schema_version": "agileforge.spec.v1"}},
            "UNSUPPORTED_SPECIFICATION_SCHEMA",
        ),
        (
            {"payload": {"schema_version": "agileforge.spec.v2"}},
            "INVALID_SPECIFICATION_PAYLOAD",
        ),
    ],
)
def test_provider_schema_and_payload_failures_keep_stable_codes(
    engine: Engine,
    tmp_path: Path,
    provider_output: JsonObject,
    expected_code: str,
) -> None:
    """Persist exact schema-versus-payload diagnostics without a candidate."""
    leaf = _CountingSpecificationLeaf(
        name="invalid_specification_structurer",
        response=_invalid_model_output(provider_output),
        calls=[],
    )
    runner, _domain, project_id, decision, normalized_input, guards = _system(
        engine,
        tmp_path,
        leaf,
    )

    result = runner.run(decision, normalized_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == expected_code
    assert leaf.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == expected_code
        assert not session.exec(select(SpecificationCandidate)).all()


def test_output_validation_failure_persists_correlated_diagnostic_in_memory(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Safe diagnostic is appended with correlated invocation ID and prose redaction."""
    sentinel = "PRIVATE_RESPONSE_SENTINEL_245"
    dangling_with_sentinel: JsonObject = {
        "payload": {
            "schema_version": "agileforge.spec.v2",
            "items": [
                {
                    "id": "REQ.item-one",
                    "title": f"Title {sentinel}",
                    "statement": f"Statement {sentinel}",
                    "rationale": f"Rationale {sentinel}",
                    "level": "SHOULD",
                    "kind": "CAPABILITY",
                    "scope": "TARGET",
                    "status": "DRAFT",
                    "applicability": "CORE",
                    "criticality": "LOW",
                    "provenance": {"source": "spec.md"},
                }
            ],
            "relations": [
                {
                    "from": "REQ.item-one",
                    "type": "tracks",
                    "to": "REQ.missing-target",
                }
            ],
        }
    }
    raw_response = json.dumps(dangling_with_sentinel)
    model = _SpecificationResponseLlm(
        model="fake/diagnostic-in-memory",
        response_text=raw_response,
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="diagnostic_in_memory_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )
    session_service = InMemorySessionService()
    runner, _, project_id, decision, frozen, guards = _system(
        engine, tmp_path, leaf, session_service=session_service
    )
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD
    assert sentinel not in str(result.error)
    assert sentinel not in repr(result.error)
    assert sentinel not in result.error.message

    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "INVALID_SPECIFICATION_PAYLOAD"
        assert outcome.failure_message is not None
        assert sentinel not in outcome.failure_message
        attempt = _latest_attempt(session, project_id=project_id)
        session_id = attempt.attempt_fingerprint
        assert not session.exec(select(SpecificationCandidate)).all()

    adk_session = asyncio.run(
        session_service.get_session(
            app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
            user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
            session_id=session_id,
        )
    )
    assert adk_session is not None
    assert len(adk_session.events) >= 2  # noqa: PLR2004
    user_event = next(ev for ev in adk_session.events if ev.author == "user")
    assert user_event.invocation_id

    diag_event = next(
        ev for ev in adk_session.events if ev.author == "specification_output_validator"
    )
    assert diag_event.invocation_id == user_event.invocation_id
    assert diag_event.output is None
    assert diag_event.actions is not None
    diagnostic = diag_event.actions.state_delta["specification_output_diagnostic"]
    assert (
        diagnostic["schema_version"]
        == "agileforge.specification-output-diagnostic.v1"
    )
    assert diagnostic["stage"] == "primary"
    assert diagnostic["code"] == "INVALID_SPECIFICATION_PAYLOAD"
    assert diagnostic["missing_item_count"] == 1
    assert diagnostic["missing_item_ids"] == ["REQ.missing-target"]
    assert diagnostic["item_count"] == 1
    assert diagnostic["relation_count"] == 1
    assert diagnostic["item_ids"] == ["REQ.item-one"]
    assert diagnostic["response_bytes"] == len(raw_response.encode("utf-8"))
    assert (
        diagnostic["response_sha256"]
        == f"sha256:{hashlib.sha256(raw_response.encode('utf-8')).hexdigest()}"
    )
    assert sentinel not in json.dumps(diagnostic)
    assert adk_session.state.get("specification_output_diagnostic") == diagnostic


def test_output_validation_failure_persists_correlated_diagnostic_sqlite(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Safe diagnostic persists to a real disposable SQLite trace database."""
    sentinel = "PRIVATE_SQLITE_SENTINEL_245"
    dangling_payload: JsonObject = {
        "payload": {
            "schema_version": "agileforge.spec.v2",
            "items": [
                {
                    "id": "REQ.item-one",
                    "statement": f"Do not leak {sentinel}",
                }
            ],
            "relations": [
                {
                    "from": "REQ.item-one",
                    "type": "tracks",
                    "to": "REQ.missing-target",
                }
            ],
        }
    }
    raw_response = json.dumps(dangling_payload)
    model = _SpecificationResponseLlm(
        model="fake/diagnostic-sqlite",
        response_text=raw_response,
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="diagnostic_sqlite_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )
    db_file = tmp_path / "disposable_adk_trace.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    session_service = DatabaseSessionService(db_url=db_url)
    runner, _, project_id, decision, frozen, guards = _system(
        engine, tmp_path, leaf, session_service=session_service
    )
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD

    with Session(engine) as session:
        attempt = _latest_attempt(session, project_id=project_id)
        session_id = attempt.attempt_fingerprint

    async def read_back_diagnostic() -> JsonObject:
        verify_service = DatabaseSessionService(db_url=db_url)
        try:
            persisted_session = await verify_service.get_session(
                app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
                user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
                session_id=session_id,
            )
            assert persisted_session is not None
            diag_event = next(
                ev
                for ev in persisted_session.events
                if ev.author == "specification_output_validator"
            )
            assert diag_event.invocation_id
            assert diag_event.output is None
            diag = persisted_session.state["specification_output_diagnostic"]
            assert diag["code"] == "INVALID_SPECIFICATION_PAYLOAD"
            assert diag["missing_item_ids"] == ["REQ.missing-target"]
            assert sentinel not in json.dumps(diag)
            return diag
        finally:
            await verify_service.close()
            await session_service.close()

    diagnostic = asyncio.run(read_back_diagnostic())
    assert diagnostic["missing_item_count"] == 1


class _SyntheticAppendDiagnosticError(RuntimeError):
    """Synthetic error injected into session service during test."""


class _FailingDiagnosticAppendSessionService(InMemorySessionService):
    """Fail only when appending the specification_output_diagnostic event."""

    async def append_event(self, session: AdkSession, event: Event) -> Event:
        if (
            event.actions
            and "specification_output_diagnostic" in event.actions.state_delta
        ):
            msg = "Synthetic failure appending diagnostic event."
            raise _SyntheticAppendDiagnosticError(msg)
        return await super().append_event(session=session, event=event)


def test_specification_output_diagnostic_append_failure_preserves_business_failure(
    engine: Engine,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Append failure emits a fixed safe warning and preserves the original failure."""
    model = _SpecificationResponseLlm(
        model="fake/failing-diagnostic-append",
        response_text=json.dumps(_dangling_output()),
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="failing_append_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )
    session_service = _FailingDiagnosticAppendSessionService()
    runner, _, project_id, decision, frozen, guards = _system(
        engine, tmp_path, leaf, session_service=session_service
    )
    with caplog.at_level(logging.WARNING):
        result = runner.run(decision, frozen, guards=guards)

    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD
    assert model.calls == ["provider"]

    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "INVALID_SPECIFICATION_PAYLOAD"
        attempt = _latest_attempt(session, project_id=project_id)
        session_id = attempt.attempt_fingerprint
        assert not session.exec(select(SpecificationCandidate)).all()

    matching_records = [
        rec
        for rec in caplog.records
        if "Specification output diagnostic could not be appended" in rec.message
    ]
    assert len(matching_records) == 1
    warning_record = matching_records[0]
    assert session_id in warning_record.message
    assert warning_record.exc_info is None
    assert "Synthetic failure appending diagnostic event" not in warning_record.message


def test_precedence_post_call_revalidation_supersedes_output_diagnostic(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-call source drift obsoletes attempt and supersedes output diagnostic."""
    model = _SpecificationResponseLlm(
        model="fake/precedence-drift",
        response_text=json.dumps(_dangling_output()),
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="precedence_drift_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=validate_specification_output,
    )
    session_service = InMemorySessionService()
    runner, domain, project_id, decision, frozen, guards = _system(
        engine, tmp_path, leaf, session_service=session_service
    )
    transition = domain.transition
    drifted = False

    def drift_after_provider(request: TransitionRequest) -> TransitionResult:
        nonlocal drifted
        if (
            request.kind == "revalidate_node_attempt"
            and bool(model.calls)
            and not drifted
        ):
            with Session(engine) as session:
                project = session.get(Project, project_id)
                assert project is not None
                project.description = "Drifted after provider returned."
                session.add(project)
                session.commit()
            drifted = True
        return transition(request)

    monkeypatch.setattr(domain, "transition", drift_after_provider)
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    assert model.calls == ["provider"]

    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "obsolete"
        attempt = _latest_attempt(session, project_id=project_id)
        session_id = attempt.attempt_fingerprint
        assert not session.exec(select(SpecificationCandidate)).all()

    adk_session = asyncio.run(
        session_service.get_session(
            app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
            user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
            session_id=session_id,
        )
    )
    assert adk_session is not None
    diagnostic_events = [
        ev
        for ev in adk_session.events
        if ev.author == "specification_output_validator"
    ]
    assert len(diagnostic_events) == 0
