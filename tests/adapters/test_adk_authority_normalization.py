# tests/adapters/test_adk_authority_normalization.py
"""Provider-free regression coverage for the Authority compiler boundary."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from sqlmodel import Session, select

from adapters.adk.prompts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from adapters.adk.recipes import (
    AgenticRecipeNodes,
    RecipeInput,
    RecipeOutput,
    build_agentic_recipe_registry,
)
from adapters.adk.runner import (
    AdkExecutionConfig,
    AdkRunGuards,
    AdkWorkflowRunner,
)
from models.core import Project
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.application import AuthorityRepairInputService
from services.authority_compilation_input import AuthorityCompilationInputService
from services.authority_review_projection import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.contracts.authority_input_v2 import AuthorityInputV2, AuthorityItemV2
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.spec_schemas import (
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerInput,
)
from workflow.clock import FixedClock
from workflow.definitions.authority import authority_graph
from workflow.domain import WorkflowDomain
from workflow.requests import DecideAuthority, RecordAuthorityFeedback

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from typing import Any

    from sqlalchemy.engine import Engine

    from adapters.adk.recipes import AdkRecipeRegistry
    from workflow.contracts import (
        JsonObject,
        NodeDecision,
        TransitionResult,
        WorkflowPosition,
    )

EVALUATED_AT = datetime(2026, 8, 14, 12, tzinfo=UTC)
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 2}
LEASE_SECONDS = 60
EXPECTED_REPAIRED_AUTHORITY_COUNT = 2
SOURCE_ID = "REQ.issue-205.authority-boundary"
SOURCE_STATEMENT = "Authority output MUST preserve typed requirements."
NON_FINITE_SOURCE = "The score MUST be at most NaN."


class CountingAuthorityLeaf(BaseAgent):
    """Return one deterministic provider-free value and record executions."""

    response: object
    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append(self.name)
        yield Event(author=self.name, output=self.response)


def _unused_leaf(name: str) -> CountingAuthorityLeaf:
    return CountingAuthorityLeaf(name=name, response={}, calls=[])


def _counting_leaf(
    *,
    name: str,
    response: object,
    calls: list[str],
) -> CountingAuthorityLeaf:
    """Keep the caller-owned observation list instead of Pydantic's copy."""
    leaf = CountingAuthorityLeaf(name=name, response=response, calls=calls)
    leaf.calls = calls
    return leaf


def _authority_input() -> AuthorityInputV2:
    item = AuthorityItemV2(
        id=SOURCE_ID,
        type="REQ",
        statement=SOURCE_STATEMENT,
        level="MUST",
        acceptance=(
            "Host normalization remains authoritative.",
            NON_FINITE_SOURCE,
        ),
    )
    return AuthorityInputV2(
        artifact_id="SPEC.issue-205",
        normative_items=(item,),
        normative_relations=(),
        eligible_item_ids=(SOURCE_ID,),
        authority_input_fingerprint="sha256:" + ("a" * 64),
    )


def _compiler_input() -> SpecAuthorityCompilerInput:
    return SpecAuthorityCompilerInput(
        authority_input=_authority_input(),
        project_id=1,
        spec_version_id=2,
        specification_fingerprint="sha256:" + ("b" * 64),
    )


def _success_payload(*, label: str = "compile") -> dict[str, Any]:
    return {
        "schema_version": "agileforge.compiled_authority.v3",
        "scope_themes": [f"Issue 205 {label}"],
        "domain": "authority compiler boundary",
        "invariants": [],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [f"{SOURCE_ID}: represented by a provider-free gap."],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "provider-placeholder",
        "prompt_hash": "0" * 64,
    }


def _ambiguous_payload() -> dict[str, Any]:
    payload = _success_payload(label="ambiguous")
    payload["gaps"] = []
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "first_field"},
        },
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "second_field"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": SOURCE_STATEMENT,
            "location": SOURCE_ID,
        },
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Host normalization remains authoritative.",
            "location": SOURCE_ID,
        },
    ]
    return payload


def _typed_source_violation_payload() -> dict[str, Any]:
    payload = _success_payload(label="typed-source")
    payload["gaps"] = []
    payload["invariants"] = [
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.outside-authority-input",
            "source_level": "MUST",
            "parameters": {"field_name": "outside_field"},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000001",
            "excerpt": "An outside requirement MUST include outside_field.",
            "location": "REQ.outside-authority-input",
        }
    ]
    return payload


def _coverage_gap_payload() -> dict[str, Any]:
    payload = _success_payload(label="coverage")
    payload["gaps"] = []
    return payload


def _non_finite_payload() -> dict[str, Any]:
    payload = _success_payload(label="non-finite")
    payload["gaps"] = []
    payload["invariants"] = [
        {
            "id": "INV-0000000000000002",
            "type": "MAX_VALUE",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "score", "max_value": float("nan")},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000002",
            "excerpt": NON_FINITE_SOURCE,
            "location": SOURCE_ID,
        }
    ]
    return payload


def _recipe_payload(node_id: str) -> JsonObject:
    compiler_input = _compiler_input().model_dump(mode="json")
    if node_id == "authority.compile":
        return {
            "project_id": 1,
            "spec_version_id": 2,
            "expected_spec_hash": "sha256:" + ("b" * 64),
            "compiler_model": "fake/compiler",
            "compiler_input": compiler_input,
        }
    return {
        "source_authority_id": 7,
        "source_authority_fingerprint": "sha256:" + ("c" * 64),
        "compiler_input": compiler_input,
    }


def _registry(
    *,
    compile_leaf: BaseAgent,
    repair_leaf: BaseAgent,
) -> AdkRecipeRegistry:
    return build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=compile_leaf,
            authority_repair=repair_leaf,
            vision_interview=_unused_leaf("unused_vision_interview"),
            vision_repair=_unused_leaf("unused_vision_repair"),
            product_goal=_unused_leaf("unused_product_goal"),
            specification_structurer=_unused_leaf("unused_specification_structurer"),
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )


async def _run_recipe_async(
    node_id: str,
    *,
    response: object,
    calls: list[str],
) -> RecipeOutput:
    target_leaf = _counting_leaf(
        name=node_id.replace(".", "_"),
        response=response,
        calls=calls,
    )
    registry = _registry(
        compile_leaf=(
            target_leaf if node_id == "authority.compile" else _unused_leaf("compile")
        ),
        repair_leaf=(
            target_leaf if node_id == "authority.repair" else _unused_leaf("repair")
        ),
    )
    recipe = registry.require(node_id)
    session_service = InMemorySessionService()
    session_id = node_id.replace(".", "-")
    await session_service.create_session(
        app_name="authority_normalization_recipe_test",
        user_id="authority_normalization_recipe_user",
        session_id=session_id,
    )
    app = App(
        name="authority_normalization_recipe_test",
        root_agent=recipe.workflow,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    runner = Runner(app=app, session_service=session_service)
    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=RecipeInput(payload=_recipe_payload(node_id)).model_dump_json()
            )
        ],
    )
    output: object | None = None
    async for event in runner.run_async(
        user_id="authority_normalization_recipe_user",
        session_id=session_id,
        new_message=message,
    ):
        if event.output is not None:
            output = event.output
    if output is None:
        message = "Authority recipe produced no output."
        raise AssertionError(message)
    return RecipeOutput.model_validate(output)


def _run_recipe(
    node_id: str,
    *,
    response: object,
    calls: list[str],
) -> RecipeOutput:
    return asyncio.run(_run_recipe_async(node_id, response=response, calls=calls))


def _success_response(kind: str) -> object:
    payload = _success_payload()
    if kind == "direct_json":
        return json.dumps(payload)
    if kind == "envelope_json":
        return json.dumps({"result": payload})
    if kind == "decoded_envelope":
        return {"result": payload}
    if kind == "model_envelope":
        return SpecAuthorityCompilerEnvelope(
            result=SpecAuthorityCompilationSuccess.model_validate(payload)
        )
    raise AssertionError(kind)


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
@pytest.mark.parametrize(
    "response_kind",
    ["direct_json", "envelope_json", "decoded_envelope", "model_envelope"],
)
def test_authority_recipes_normalize_supported_compiler_output_shapes(
    node_id: str,
    response_kind: str,
) -> None:
    """Compile and repair share one raw and structured normalization boundary."""
    calls: list[str] = []

    output = _run_recipe(
        node_id,
        response=_success_response(response_kind),
        calls=calls,
    )

    compiled = output.payload["compiled_authority"]
    assert isinstance(compiled, dict)
    assert compiled["compiler_version"] == SPEC_AUTHORITY_COMPILER_VERSION
    assert compiled["prompt_hash"] == SPEC_AUTHORITY_COMPILER_PROMPT_HASH
    assert calls == [node_id.replace(".", "_")]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
@pytest.mark.parametrize(
    ("response", "reason", "detail"),
    [
        ("not JSON", "INVALID_JSON", "not valid JSON"),
        (
            json.dumps(
                {
                    "schema_version": "agileforge.compiled_authority.v3",
                    "unexpected": True,
                }
            ),
            "JSON_VALIDATION_FAILED",
            "schema error",
        ),
        (
            json.dumps(_typed_source_violation_payload()),
            "INELIGIBLE_INVARIANT_SOURCE",
            "outside-authority-input",
        ),
        (
            json.dumps(_coverage_gap_payload()),
            "INCOMPLETE_NORMATIVE_COVERAGE",
            SOURCE_ID,
        ),
        (
            json.dumps(_ambiguous_payload()),
            "JSON_VALIDATION_FAILED",
            "ambiguous repeated invariant identity",
        ),
        pytest.param(
            json.dumps(_non_finite_payload()),
            "INVALID_JSON",
            "not valid JSON",
            id="raw-non-finite-number",
        ),
        pytest.param(
            _non_finite_payload(),
            "INVALID_JSON",
            "not valid JSON",
            id="structured-non-finite-number",
        ),
    ],
)
def test_authority_recipes_raise_stable_domain_failures_after_normalization(
    node_id: str,
    response: object,
    reason: str,
    detail: str,
) -> None:
    """Every closed normalizer failure stays actionable in compile and repair."""
    calls: list[str] = []

    with pytest.raises(RuntimeError) as captured:
        _run_recipe(node_id, response=response, calls=calls)

    error = captured.value
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    assert type(error).__name__ == "AuthorityAgenticExecutionError"
    assert code_value == "AUTHORITY_COMPILATION_FAILED"
    assert reason in str(error)
    assert detail in str(error)
    assert "validation error for SpecAuthorityCompilerEnvelope" not in str(error)
    assert calls == [node_id.replace(".", "_")]


def _seed_compile_target(engine: Engine) -> tuple[int, int, str]:
    content = json.dumps(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.issue-205-runner",
            "title": "Issue 205 runner boundary",
            "summary": "Normalize compiler output before Authority persistence.",
            "problem_statement": "Raw compiler JSON must reach host normalization.",
            "items": [
                {
                    "id": SOURCE_ID,
                    "type": "REQ",
                    "title": "Normalize Authority output",
                    "statement": SOURCE_STATEMENT,
                    "level": "MUST",
                    "verification": "integration-test",
                    "acceptance": ["Host normalization remains authoritative."],
                }
            ],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
        }
    )
    with Session(engine) as session:
        project = Project(name="Issue 205 Authority boundary")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content=content,
            recorded_at=EVALUATED_AT - timedelta(minutes=1),
        )
        assert lineage.spec.spec_version_id is not None
        return (
            project.project_id,
            lineage.spec.spec_version_id,
            lineage.spec.spec_hash,
        )


def _build_runner(
    engine: Engine,
    *,
    project_id: int,
    compile_response: object,
    repair_response: object,
    calls: list[str],
) -> tuple[AdkWorkflowRunner, WorkflowDomain]:
    registry = _registry(
        compile_leaf=_counting_leaf(
            name="compile_provider",
            response=compile_response,
            calls=calls,
        ),
        repair_leaf=_counting_leaf(
            name="repair_provider",
            response=repair_response,
            calls=calls,
        ),
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=authority_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=InMemorySessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/compiler",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
        ),
    )
    return runner, domain


def _decision(position: WorkflowPosition, node_id: str) -> NodeDecision:
    return next(item for item in position.decisions if item.node_id == node_id)


def _run_compile(
    engine: Engine,
    *,
    runner: AdkWorkflowRunner,
    domain: WorkflowDomain,
    project_id: int,
    idempotency_key: str,
) -> tuple[TransitionResult, NodeDecision, JsonObject, AdkRunGuards]:
    position = domain.position(project_id)
    decision = _decision(position, "authority.compile")
    compiler_input = AuthorityCompilationInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
        compiler_model="fake/compiler",
    )
    guards = AdkRunGuards(
        position=position,
        idempotency_key=idempotency_key,
        actor="operator@example.com",
        correlation_id="issue-205",
    )
    return (
        runner.run(decision, compiler_input, guards=guards),
        decision,
        compiler_input,
        guards,
    )


def _reject_and_record_feedback(
    engine: Engine,
    *,
    domain: WorkflowDomain,
    project_id: int,
) -> NodeDecision:
    review_position = domain.position(project_id)
    with Session(engine) as session:
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
    assert isinstance(review, AuthorityReviewSnapshot)
    assert review.pending_authority_id is not None
    assert review.authority_fingerprint is not None
    review_decision = _decision(review_position, "authority.review")
    rejected = domain.transition(
        DecideAuthority(
            project_id=project_id,
            graph_version=review_position.graph_version,
            fact_fingerprint=review_position.fact_fingerprint,
            decision_fingerprint=review_decision.decision_fingerprint,
            instance_key=review_decision.instance_key,
            idempotency_key="issue-205-reject",
            actor="operator@example.com",
            correlation_id="issue-205",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            review_fingerprint=review.review_fingerprint,
            decision="rejected",
            rationale="Exercise the repair compiler boundary.",
        )
    )
    assert rejected.ok is True
    assert rejected.position is not None
    feedback_decision = _decision(rejected.position, "authority.feedback")
    feedback = domain.transition(
        RecordAuthorityFeedback(
            project_id=project_id,
            graph_version=rejected.position.graph_version,
            fact_fingerprint=rejected.position.fact_fingerprint,
            decision_fingerprint=feedback_decision.decision_fingerprint,
            instance_key=feedback_decision.instance_key,
            idempotency_key="issue-205-feedback",
            actor="operator@example.com",
            correlation_id="issue-205",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            feedback={"summary": "Recompile through the same host boundary."},
        )
    )
    assert feedback.ok is True
    assert feedback.position is not None
    return _decision(feedback.position, "authority.repair")


def _run_repair(
    engine: Engine,
    *,
    runner: AdkWorkflowRunner,
    domain: WorkflowDomain,
    project_id: int,
    decision: NodeDecision,
) -> tuple[TransitionResult, JsonObject, AdkRunGuards]:
    position = domain.position(project_id)
    payload = AuthorityRepairInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
    )
    assert payload is not None
    guards = AdkRunGuards(
        position=position,
        idempotency_key="issue-205-repair-attempt",
        actor="operator@example.com",
        correlation_id="issue-205",
    )
    return runner.run(decision, payload, guards=guards), payload, guards


def _assert_authority_failure(result: TransitionResult) -> None:
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "AUTHORITY_COMPILATION_FAILED"
    assert "JSON_VALIDATION_FAILED" in result.error.message
    assert "ambiguous repeated invariant identity" in result.error.message
    assert "validation error for SpecAuthorityCompilerEnvelope" not in (
        result.error.message
    )


def test_raw_json_compile_and_repair_persist_one_pending_authority_and_replay(
    engine: Engine,
) -> None:
    """Valid raw JSON persists one current review target per lifecycle step."""
    project_id, _spec_version_id, _spec_hash = _seed_compile_target(engine)
    calls: list[str] = []
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        compile_response=json.dumps(_success_payload(label="compile")),
        repair_response=json.dumps(_success_payload(label="repair")),
        calls=calls,
    )

    compile_result, compile_decision, compile_input, compile_guards = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-205-compile",
    )
    compile_replay = runner.run(
        compile_decision,
        compile_input,
        guards=compile_guards,
    )

    assert compile_result.ok is True
    assert compile_replay == compile_result.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert len(session.exec(select(CompiledSpecAuthority)).all()) == 1
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []

    repair_decision = _reject_and_record_feedback(
        engine,
        domain=domain,
        project_id=project_id,
    )
    repair_result, repair_input, repair_guards = _run_repair(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        decision=repair_decision,
    )
    repair_replay = runner.run(
        repair_decision,
        repair_input,
        guards=repair_guards,
    )

    assert repair_result.ok is True
    assert repair_replay == repair_result.model_copy(update={"replayed": True})
    assert calls == ["compile_provider", "repair_provider"]
    with Session(engine) as session:
        authorities = session.exec(select(CompiledSpecAuthority)).all()
        decisions = session.exec(select(SpecAuthorityAcceptance)).all()
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert len(authorities) == EXPECTED_REPAIRED_AUTHORITY_COUNT
        assert len(decisions) == 1
        assert decisions[0].status == "rejected"
        assert isinstance(review, AuthorityReviewSnapshot)
        assert review.pending_authority_id == authorities[-1].authority_id


def test_ambiguous_raw_compile_records_actionable_failure_without_authority(
    engine: Engine,
) -> None:
    """Captured-like direct JSON fails after normalization without partial state."""
    project_id, _spec_version_id, _spec_hash = _seed_compile_target(engine)
    calls: list[str] = []
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        compile_response=json.dumps(_ambiguous_payload()),
        repair_response=json.dumps(_success_payload(label="unused-repair")),
        calls=calls,
    )

    result, decision, compiler_input, guards = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-205-ambiguous-compile",
    )
    replay = runner.run(decision, compiler_input, guards=guards)

    _assert_authority_failure(result)
    assert replay == result.model_copy(update={"replayed": True})
    assert calls == ["compile_provider"]
    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []
        attempts = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.node_id == "authority.compile"
            )
        ).all()
        attempt_ids = {
            attempt.workflow_node_attempt_id
            for attempt in attempts
            if attempt.workflow_node_attempt_id is not None
        }
        outcomes = [
            outcome
            for outcome in session.exec(select(WorkflowNodeAttemptOutcome)).all()
            if outcome.workflow_node_attempt_id in attempt_ids
        ]
        assert len(attempts) == 1
        assert len(outcomes) == 1
        assert outcomes[0].failure_code == "AUTHORITY_COMPILATION_FAILED"
        assert "ambiguous repeated invariant identity" in (
            outcomes[0].failure_message or ""
        )


def test_ambiguous_raw_repair_records_actionable_failure_without_replacement(
    engine: Engine,
) -> None:
    """Repair uses the same boundary and never persists a partial replacement."""
    project_id, _spec_version_id, _spec_hash = _seed_compile_target(engine)
    calls: list[str] = []
    structured_compile = SpecAuthorityCompilerEnvelope(
        result=SpecAuthorityCompilationSuccess.model_validate(
            _success_payload(label="structured-setup")
        )
    )
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        compile_response=structured_compile,
        repair_response=json.dumps(_ambiguous_payload()),
        calls=calls,
    )
    compile_result, _decision_value, _input, _guards_value = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-205-repair-setup",
    )
    assert compile_result.ok is True
    repair_decision = _reject_and_record_feedback(
        engine,
        domain=domain,
        project_id=project_id,
    )

    result, repair_input, guards = _run_repair(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        decision=repair_decision,
    )
    replay = runner.run(repair_decision, repair_input, guards=guards)

    _assert_authority_failure(result)
    assert replay == result.model_copy(update={"replayed": True})
    assert calls == ["compile_provider", "repair_provider"]
    with Session(engine) as session:
        authorities = session.exec(select(CompiledSpecAuthority)).all()
        decisions = session.exec(select(SpecAuthorityAcceptance)).all()
        repair_attempts = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.node_id == "authority.repair"
            )
        ).all()
        repair_outcomes = [
            outcome
            for outcome in session.exec(select(WorkflowNodeAttemptOutcome)).all()
            if outcome.workflow_node_attempt_id
            in {attempt.workflow_node_attempt_id for attempt in repair_attempts}
        ]
        assert len(authorities) == 1
        assert len(decisions) == 1
        assert decisions[0].status == "rejected"
        assert len(repair_attempts) == 1
        assert len(repair_outcomes) == 1
        assert repair_outcomes[0].failure_code == "AUTHORITY_COMPILATION_FAILED"
