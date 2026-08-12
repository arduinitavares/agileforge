"""Failure boundaries for durable Specification structuring attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from git import Repo
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.adk.sessions import Session as AdkSession
from openai import OpenAIError
from sqlmodel import Session, col, select

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
from tests.workflow.lifecycle_fixtures import _seed_accepted_vision_and_goal
from workflow.clock import FixedClock
from workflow.contracts import TransitionResult, WorkflowError, WorkflowErrorCode
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_json
from workflow.requests import RegisterSpecificationSource, RevalidateNodeAttempt

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import (
        JsonObject,
        NodeDecision,
    )
    from workflow.requests import TransitionRequest

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}


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


def _system(
    engine: Engine,
    tmp_path: Path,
    leaf: BaseAgent,
    *,
    source_check: SpecificationSourceCheck | None = None,
    execution_settings: JsonObject = EXECUTION_SETTINGS,
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
    (repository / "SPECIFICATION.md").write_text(
        "# Exact external Specification\n",
        encoding="utf-8",
    )
    with Repo.init(repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Structuring Attempt Test")
            config.set_value("user", "email", "structuring@example.test")
        repo.index.add(["SPECIFICATION.md"])
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
            authority_compile=_unused_leaf("unused_authority_compile"),
            authority_repair=_unused_leaf("unused_authority_repair"),
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
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=NOW),
        adk_recipe_registry=registry,
        specification_registration_check=lambda _prepared: None,
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
    structuring_input_service = SpecificationStructuringInputService(
        engine=engine,
        repository_probe=probe,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=InMemorySessionService(),
        specification_source_check=(
            structuring_input_service.revalidate_sources
            if source_check is None
            else source_check
        ),
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
