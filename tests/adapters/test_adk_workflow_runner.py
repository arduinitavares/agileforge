"""Domain-bounded ADK workflow runner tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from google.adk.agents import BaseAgent, InvocationContext
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.adk.sessions import Session as AdkSession
from pydantic import TypeAdapter
from sqlmodel import Session, select

from adapters.adk.recipes import (
    AdkRecipe,
    AdkRecipeRegistry,
    AttemptCompletionContext,
    RecipeOutput,
    build_backlog_generation_workflow,
)
from adapters.adk.runner import AdkExecutionConfig, AdkWorkflowRunner
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
)
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.clock import FixedClock
from workflow.contracts import JsonObject, NodeDecision, WorkflowErrorCode
from workflow.definitions.product_definition import product_definition_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import RecordBacklogDraft

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)
JSON_OBJECT = TypeAdapter(JsonObject)


class FakeLeafAgent(BaseAgent):
    """Provider-free leaf with deterministic output or failure."""

    response: object
    failure_message: str | None = None

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        if self.failure_message is not None:
            raise RuntimeError(self.failure_message)
        yield Event(author=self.name, output=self.response)


class TrackingSessionService(InMemorySessionService):
    """In-memory ADK trace store recording created session IDs."""

    def __init__(self) -> None:
        """Initialize the trace store and its session-ID observations."""
        super().__init__()
        self.created_session_ids: list[str] = []

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> AdkSession:
        """Record explicit attempt-keyed IDs before creating sessions."""
        if session_id is not None:
            self.created_session_ids.append(session_id)
        return await super().create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )


def _seed(engine: Engine) -> tuple[int, int, str]:
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["Runner"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    with Session(engine) as session:
        project = Product(name="Runner", origin="greenfield")
        session.add(project)
        session.flush()
        assert project.product_id is not None
        spec = SpecRegistry(
            product_id=project.product_id,
            spec_hash="sha256:runner-spec",
            content='{"scope":"runner"}',
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
        fingerprint = pending_authority_fingerprint(authority)
        assert fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=project.product_id,
                spec_version_id=spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT,
                rationale="Accepted.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority.authority_id,
                authority_fingerprint=fingerprint,
                review_fingerprint="sha256:review",
                terminal_decision_key="runner-authority",
            )
        )
        session.commit()
        return project.product_id, authority.authority_id, fingerprint


def _backlog_payload() -> JsonObject:
    return {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Execute graph nodes through ADK",
                "authority_ref": "REQ.runner",
                "capability_hint": None,
                "value_driver": "Strategic",
                "justification": "Keep durable facts authoritative.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordBacklogDraft:
    recipe_output = RecipeOutput.model_validate(output)
    payload = JSON_OBJECT.validate_python(recipe_output.payload)
    authority_id = payload.pop("authority_id")
    authority_fingerprint = payload.pop("authority_fingerprint")
    content = JSON_OBJECT.validate_python(payload.pop("content"))
    assert isinstance(authority_id, int)
    assert isinstance(authority_fingerprint, str)
    return RecordBacklogDraft(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        canonical_content=content,
        content_fingerprint=canonical_hash(content),
    )


def _build_runner(
    engine: Engine,
    *,
    project_id: int,
    leaf: FakeLeafAgent,
    sessions: TrackingSessionService,
) -> tuple[AdkWorkflowRunner, WorkflowDomain]:
    settings: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}
    recipe = AdkRecipe(
        node_id="backlog.generate",
        workflow=build_backlog_generation_workflow(
            leaf_agent=leaf,
            execution_settings=settings,
        ),
        output_adapter=_adapter,
    )
    registry = AdkRecipeRegistry((recipe,))
    domain = WorkflowDomain(
        engine=engine,
        graph=product_definition_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=sessions,
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/model",
            execution_settings=settings,
            lease_seconds=60,
            actor="operator@example.com",
            correlation_id="task-15",
        ),
    )
    return runner, domain


def _decision(domain: WorkflowDomain, project_id: int) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "backlog.generate"
    )


def test_runner_executes_fake_leaf_and_commits_validated_output(engine: Engine) -> None:
    """Run a provider-free recipe and commit its fact and success outcome."""
    project_id, authority_id, authority_fingerprint = _seed(engine)
    response: JsonObject = {
        "authority_id": authority_id,
        "authority_fingerprint": authority_fingerprint,
        "content": _backlog_payload(),
    }
    sessions = TrackingSessionService()
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response=response),
        sessions=sessions,
    )

    result = runner.run(_decision(domain, project_id), {"prompt": "build backlog"})

    assert result.ok is True
    with Session(engine) as session:
        attempt = session.exec(select(WorkflowNodeAttempt)).one()
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "success"
        assert session.exec(select(BacklogArtifact)).one() is not None
        assert sessions.created_session_ids == [
            str(attempt.workflow_node_attempt_id)
        ]


def test_provider_failure_records_failure_and_returns_external_error(
    engine: Engine,
) -> None:
    """Translate a fake provider failure after recording its durable outcome."""
    project_id, _authority_id, _authority_fingerprint = _seed(engine)
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=FakeLeafAgent(
            name="fake_backlog",
            response={},
            failure_message="provider unavailable",
        ),
        sessions=TrackingSessionService(),
    )

    result = runner.run(_decision(domain, project_id), {"prompt": "build backlog"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "failure"
        assert session.exec(select(BacklogArtifact)).all() == []


def test_output_validation_failure_records_failure_without_business_fact(
    engine: Engine,
) -> None:
    """Reject scalar leaf output and persist no downstream artifact."""
    project_id, _authority_id, _authority_fingerprint = _seed(engine)
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        leaf=FakeLeafAgent(name="fake_backlog", response="not-an-object"),
        sessions=TrackingSessionService(),
    )

    result = runner.run(_decision(domain, project_id), {"prompt": "build backlog"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    with Session(engine) as session:
        assert session.exec(select(BacklogArtifact)).all() == []
        outcome = session.exec(select(WorkflowNodeAttemptOutcome)).one()
        assert outcome.status == "failure"
