"""Durable node-attempt lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from google.adk import Context, Workflow
from google.adk.workflow import node
from sqlmodel import Session, select

from adapters.adk.recipes import (
    AdkRecipe,
    AdkRecipeRegistry,
    AttemptCompletionContext,
    RecipeInput,
    RecipeOutput,
)
from models.core import Project
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.contracts import (
    JsonObject,
    NodeDecision,
    TransitionResult,
    WorkflowErrorCode,
)
from workflow.definitions.product_definition import product_definition_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import FailNodeAttempt, RecordBacklogDraft, StartNodeAttempt

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)
LEASE_SECONDS = 60
MODEL_ID = "fake/model"
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}


@dataclass
class MutableClock:
    """Clock controlled by one lifecycle test."""

    now_value: datetime

    def now(self) -> datetime:
        """Return the test-controlled current time."""
        return self.now_value


def _authority_artifact() -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Durable attempts"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )


def _seed_accepted_authority(engine: Engine) -> tuple[int, int, str]:
    artifact = _authority_artifact()
    with Session(engine) as session:
        project = Project(name="Task 15", origin="greenfield")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        spec = SpecRegistry(
            project_id=project.project_id,
            spec_hash="sha256:task-15-spec",
            content='{"scope":"task-15"}',
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="operator@example.com",
        )
        session.add(spec)
        session.flush()
        assert spec.spec_version_id is not None
        authority = CompiledSpecAuthority(
            spec_version_id=spec.spec_version_id,
            compiler_version=artifact.compiler_version,
            prompt_hash=artifact.prompt_hash,
            compiled_at=EVALUATED_AT,
            compiled_artifact_json=artifact.model_dump_json(),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.flush()
        assert authority.authority_id is not None
        authority_fingerprint = pending_authority_fingerprint(authority)
        assert authority_fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                project_id=project.project_id,
                spec_version_id=spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT,
                rationale="Accepted for attempt tests.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority.authority_id,
                authority_fingerprint=authority_fingerprint,
                review_fingerprint="sha256:review",
                terminal_decision_key="task-15-authority",
            )
        )
        session.commit()
        return project.project_id, authority.authority_id, authority_fingerprint


def _registry() -> AdkRecipeRegistry:
    @node(name="unused", rerun_on_resume=True, timeout=5.0)
    async def unused(_context: Context, node_input: RecipeInput) -> RecipeOutput:
        return RecipeOutput(payload=node_input.payload)

    workflow = Workflow(
        name="unused_backlog_recipe",
        timeout=5.0,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[("START", unused)],
    )

    def unused_adapter(
        _output: object,
        _context: AttemptCompletionContext,
    ) -> RecordBacklogDraft:
        message = "Domain lifecycle tests do not execute ADK recipes."
        raise AssertionError(message)

    return AdkRecipeRegistry(
        (
            AdkRecipe(
                node_id="backlog.generate",
                workflow=workflow,
                output_adapter=unused_adapter,
            ),
        )
    )


def _domain(
    engine: Engine,
    clock: MutableClock,
    registry: AdkRecipeRegistry,
) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=product_definition_graph(),
        clock=clock,
        adk_recipe_registry=registry,
    )


def _decision(domain: WorkflowDomain, project_id: int) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "backlog.generate"
    )


def _start_request(
    domain: WorkflowDomain,
    project_id: int,
    *,
    idempotency_key: str = "start-backlog",
) -> StartNodeAttempt:
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "backlog.generate"
    )
    return StartNodeAttempt(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=idempotency_key,
        actor="operator@example.com",
        correlation_id="task-15",
        target_node_id=decision.node_id,
        target_instance_key=decision.instance_key,
        normalized_input={"authority_id": 1},
        model_id=MODEL_ID,
        execution_settings=EXECUTION_SETTINGS,
        lease_seconds=LEASE_SECONDS,
    )


def _attempt_identity(result: object) -> tuple[int, str]:
    assert isinstance(result, TransitionResult)
    attempt_id = result.output["attempt_id"]
    attempt_fingerprint = result.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    return attempt_id, attempt_fingerprint


def _completion_request(
    *,
    start_request: StartNodeAttempt,
    attempt_id: int,
    attempt_fingerprint: str,
    authority: tuple[int, str],
    idempotency_key: str = "complete-backlog",
) -> RecordBacklogDraft:
    authority_id, authority_fingerprint = authority
    content: JsonObject = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Persist durable node attempts",
                "authority_ref": "REQ.task-15",
                "capability_hint": None,
                "value_driver": "Strategic",
                "justification": "Execution trace cannot own workflow position.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    return RecordBacklogDraft(
        project_id=start_request.project_id,
        graph_version=start_request.graph_version,
        fact_fingerprint=start_request.fact_fingerprint,
        decision_fingerprint=start_request.decision_fingerprint,
        instance_key=start_request.target_instance_key,
        attempt_id=attempt_id,
        attempt_fingerprint=attempt_fingerprint,
        idempotency_key=idempotency_key,
        actor=start_request.actor,
        correlation_id=start_request.correlation_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        canonical_content=content,
        content_fingerprint=canonical_hash(content),
    )


def test_start_persists_attempt_and_returns_minimal_receipt(engine: Engine) -> None:
    """Persist immutable attempt input and return only its durable receipt."""
    project_id, _authority_id, _authority_fingerprint = _seed_accepted_authority(engine)
    clock = MutableClock(EVALUATED_AT)
    domain = _domain(engine, clock, _registry())

    result = domain.transition(_start_request(domain, project_id))

    assert result.ok is True
    assert set(result.output) == {
        "attempt_id",
        "attempt_fingerprint",
        "lease_expires_at",
    }
    with Session(engine) as session:
        attempt = session.exec(select(WorkflowNodeAttempt)).one()
        assert attempt.node_id == "backlog.generate"
        assert attempt.normalized_input_json == '{"authority_id":1}'
        assert attempt.execution_settings_json == (
            '{"max_attempts":1,"timeout_seconds":5.0}'
        )
        assert attempt.attempt_fingerprint == result.output["attempt_fingerprint"]


def test_duplicate_start_replays_without_second_attempt(engine: Engine) -> None:
    """Replay a duplicate start from its receipt without a second lease."""
    project_id, _authority_id, _authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine, MutableClock(EVALUATED_AT), _registry())
    request = _start_request(domain, project_id)

    first = domain.transition(request)
    replay = domain.transition(request)

    assert first.ok is True
    assert replay == first.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert len(session.exec(select(WorkflowNodeAttempt)).all()) == 1


def test_live_attempt_changes_target_to_waiting(engine: Engine) -> None:
    """Render a target waiting until its live lease expires."""
    project_id, _authority_id, _authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine, MutableClock(EVALUATED_AT), _registry())
    started = domain.transition(_start_request(domain, project_id))
    assert started.ok is True

    decision = _decision(domain, project_id)

    assert decision.category.value == "waiting"
    assert decision.valid_until == EVALUATED_AT + timedelta(seconds=LEASE_SECONDS)


def test_expired_attempt_is_obsoleted_when_recovery_starts(engine: Engine) -> None:
    """Obsolete the expired attempt atomically with its replacement."""
    project_id, _authority_id, _authority_fingerprint = _seed_accepted_authority(engine)
    clock = MutableClock(EVALUATED_AT)
    domain = _domain(engine, clock, _registry())
    first = domain.transition(_start_request(domain, project_id))
    first_id, _first_fingerprint = _attempt_identity(first)
    clock.now_value += timedelta(seconds=LEASE_SECONDS)

    recovery = _decision(domain, project_id)
    attempt_reference = next(
        item for item in recovery.fact_references if item.fact_type == "node_attempt"
    )
    second = domain.transition(
        _start_request(domain, project_id, idempotency_key="recover-backlog")
    )

    assert recovery.category.value == "available"
    assert recovery.recommendation_kind.value == "recovery"
    assert attempt_reference.fact_id == str(first_id)
    assert second.ok is True
    second_id, _second_fingerprint = _attempt_identity(second)
    assert second_id != first_id
    with Session(engine) as session:
        outcomes = session.exec(select(WorkflowNodeAttemptOutcome)).all()
        assert [(item.workflow_node_attempt_id, item.status) for item in outcomes] == [
            (first_id, "obsolete")
        ]


def test_completion_writes_business_fact_and_success_outcome_atomically(
    engine: Engine,
) -> None:
    """Commit downstream artifact and success outcome in one transaction."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine, MutableClock(EVALUATED_AT), _registry())
    start_request = _start_request(domain, project_id)
    started = domain.transition(start_request)
    attempt_id, attempt_fingerprint = _attempt_identity(started)

    completed = domain.transition(
        _completion_request(
            start_request=start_request,
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            authority=(authority_id, authority_fingerprint),
        )
    )

    assert completed.ok is True
    with Session(engine) as session:
        assert session.exec(select(BacklogArtifact)).one() is not None
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "success"
        assert outcome.output_json is not None
        assert outcome.output_fingerprint is not None


def test_completed_attempt_updates_start_receipt_with_terminal_command_result(
    engine: Engine,
) -> None:
    """Replay the prior command result through the original transport key."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine, MutableClock(EVALUATED_AT), _registry())
    start_request = _start_request(domain, project_id)
    started = domain.transition(start_request)
    attempt_id, attempt_fingerprint = _attempt_identity(started)
    completed = domain.transition(
        _completion_request(
            start_request=start_request,
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            authority=(authority_id, authority_fingerprint),
        )
    )

    replay = domain.transition(start_request)

    assert completed.ok is True
    assert replay == completed.model_copy(update={"replayed": True})
    with Session(engine) as session:
        start_receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.request_kind == "start_node_attempt"
            )
        ).one()
        assert start_receipt.result_json is not None
        persisted = TransitionResult.model_validate_json(start_receipt.result_json)
        assert persisted == completed


def test_process_crash_before_outcome_leaves_recoverable_active_attempt(
    engine: Engine,
) -> None:
    """Keep a crash-abandoned live lease visible after domain restart."""
    project_id, _authority_id, _authority_fingerprint = _seed_accepted_authority(engine)
    registry = _registry()
    started = _domain(engine, MutableClock(EVALUATED_AT), registry).transition(
        _start_request(
            _domain(engine, MutableClock(EVALUATED_AT), registry),
            project_id,
        )
    )
    assert started.ok is True

    restarted = _domain(engine, MutableClock(EVALUATED_AT), registry)
    assert _decision(restarted, project_id).category.value == "waiting"
    with Session(engine) as session:
        assert session.exec(select(WorkflowNodeAttemptOutcome)).all() == []


def test_failure_records_terminal_failure_without_business_fact(engine: Engine) -> None:
    """Record provider failure without granting downstream authority."""
    project_id, _authority_id, _authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine, MutableClock(EVALUATED_AT), _registry())
    start_request = _start_request(domain, project_id)
    started = domain.transition(start_request)
    attempt_id, attempt_fingerprint = _attempt_identity(started)

    failed = domain.transition(
        FailNodeAttempt(
            project_id=project_id,
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            failure_code="PROVIDER_UNAVAILABLE",
            failure_message="Fake provider failed.",
            idempotency_key="fail-backlog",
            actor="operator@example.com",
            correlation_id="task-15",
        )
    )

    assert failed.ok is True
    with Session(engine) as session:
        assert session.exec(select(BacklogArtifact)).all() == []
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "failure"
        assert outcome.failure_code == "PROVIDER_UNAVAILABLE"


def test_late_model_result_is_recorded_obsolete_without_authority_fact(
    engine: Engine,
) -> None:
    """Obsolete late model output after any business fact changes."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine, MutableClock(EVALUATED_AT), _registry())
    start_request = _start_request(domain, project_id)
    started = domain.transition(start_request)
    attempt_id, attempt_fingerprint = _attempt_identity(started)
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.name = "Task 15 changed"
        session.add(project)
        session.commit()

    result = domain.transition(
        _completion_request(
            start_request=start_request,
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            authority=(authority_id, authority_fingerprint),
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
    with Session(engine) as session:
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "obsolete"
        assert session.exec(select(BacklogArtifact)).all() == []
