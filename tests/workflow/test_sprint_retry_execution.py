"""Persisted execution lifecycle coverage for Sprint retry attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from sqlalchemy import text as sql_text
from sqlmodel import Session, SQLModel, create_engine

from repositories.workflow import WorkflowFactRepository
from services.application import (
    AgileForgeApplication,
    CloseStoryRequest,
    CompleteTaskRequest,
    ExecutionActionSelectionService,
    PostSprintTriageRequest,
    SprintCloseRequest,
    SprintReviewRequest,
)
from services.sprint_retry import build_sprint_retry_preview
from services.task_execution_service import (
    TaskCompletionInput,
    TaskExecutionServiceError,
    complete_task_in_session,
)
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.execution_retry_support import (
    _complete_execution_sprint,
    _triage_execution_sprint,
)
from tests.workflow.retry_execution_fixtures import (
    CompletedRetrySource,
    record_pending_successor_plan,
    seed_completed_retry_source,
)
from tests.workflow.test_planning_transitions import (
    _apply_current_dependencies,
    _select_for_sprint,
)
from workflow.clock import FixedClock
from workflow.contracts import NodeCategory, NodeDecision
from workflow.definitions.planning import planning_graph
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.execution_scope import (
    current_execution_scope,
    resolve_execution_scope,
    retry_blocks_planning,
)
from workflow.requests import (
    CloseSprint,
    CloseStory,
    CompleteTask,
    RecordPostSprintTriage,
    RetrySprint,
    ReviewSprint,
    StartSprintRetry,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.execution_scope import ExecutionScope


def _file_domain(path: Path) -> tuple[Engine, WorkflowDomain, int, int, int, int]:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    SQLModel.metadata.create_all(engine)
    original, project_id, sprint_id, story_id, task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(original, project_id=project_id, sprint_id=sprint_id)
    return (
        engine,
        WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=datetime(2026, 9, 9, tzinfo=UTC)),
        ),
        project_id,
        sprint_id,
        story_id,
        task_id,
    )


@dataclass(frozen=True)
class _StartedRetry:
    """The saved retry requests needed for strict replay and stale-action checks."""

    retry_id: int
    retry_request: RetrySprint
    start_request: StartSprintRetry | None


def _start_retry(
    engine: Engine,
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
    suffix: str,
) -> int:
    return _start_retry_with_requests(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix=suffix,
    ).retry_id


def _start_retry_with_requests(
    engine: Engine,
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
    suffix: str,
) -> _StartedRetry:
    """Start one retry and retain the exact durable requests for later checks."""
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    retry = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.sprint.retry"
        and decision.instance_key == f"sprint:{sprint_id}"
    )
    retry_request = RetrySprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=retry.decision_fingerprint,
        idempotency_key=f"{suffix}-plan",
        actor="owner@example.com",
        instance_key=retry.instance_key,
        sprint_id=sprint_id,
        confirm=True,
        rationale="Fresh task evidence is required.",
        expected_state_fingerprint=preview.expected_state_fingerprint,
    )
    planned = domain.transition(retry_request)
    assert planned.ok is True
    retry_id = planned.output["retry_attempt_id"]
    assert isinstance(retry_id, int)
    start_position = domain.position(project_id)
    start = next(
        decision
        for decision in start_position.decisions
        if decision.node_id == "execution.sprint.retry.start"
        and decision.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    start_request = StartSprintRetry(
        project_id=project_id,
        graph_version=start_position.graph_version,
        fact_fingerprint=start_position.fact_fingerprint,
        decision_fingerprint=start.decision_fingerprint,
        idempotency_key=f"{suffix}-start",
        actor="owner@example.com",
        instance_key=start.instance_key,
        sprint_id=sprint_id,
        retry_attempt_id=retry_id,
    )
    started = domain.transition(start_request)
    assert started.ok is True
    return _StartedRetry(
        retry_id=retry_id,
        retry_request=retry_request,
        start_request=start_request,
    )


def _plan_retry_with_requests(
    engine: Engine,
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
    suffix: str,
) -> _StartedRetry:
    """Persist retry planning while deliberately leaving execution unstarted."""
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    retry = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.sprint.retry"
        and decision.instance_key == f"sprint:{sprint_id}"
    )
    retry_request = RetrySprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=retry.decision_fingerprint,
        idempotency_key=f"{suffix}-plan",
        actor="owner@example.com",
        instance_key=retry.instance_key,
        sprint_id=sprint_id,
        confirm=True,
        rationale="Fresh task evidence is required.",
        expected_state_fingerprint=preview.expected_state_fingerprint,
    )
    planned = domain.transition(retry_request)
    assert planned.ok is True
    retry_id = planned.output["retry_attempt_id"]
    assert isinstance(retry_id, int)
    return _StartedRetry(
        retry_id=retry_id,
        retry_request=retry_request,
        start_request=None,
    )


def _start_planned_retry(
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
    planned: _StartedRetry,
    suffix: str,
) -> _StartedRetry:
    """Start the exact retry whose planned-only planning lock was already observed."""
    position = domain.position(project_id)
    start = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.sprint.retry.start"
        and decision.instance_key == f"retry:{planned.retry_id}:sprint:{sprint_id}"
    )
    start_request = StartSprintRetry(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=start.decision_fingerprint,
        idempotency_key=f"{suffix}-start",
        actor="owner@example.com",
        instance_key=start.instance_key,
        sprint_id=sprint_id,
        retry_attempt_id=planned.retry_id,
    )
    started = domain.transition(start_request)
    assert started.ok is True
    return _StartedRetry(
        retry_id=planned.retry_id,
        retry_request=planned.retry_request,
        start_request=start_request,
    )


def _raw_rows(engine: Engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Capture all durable rows so a retry can only append attempt-local facts."""
    with engine.connect() as connection:
        return {
            table_name: tuple(
                tuple(row)
                for row in connection.execute(
                    sql_text(f"SELECT * FROM {table_name} ORDER BY rowid")  # noqa: S608
                )
            )
            for table_name in inspect(engine).get_table_names()
        }


def _assert_preexisting_rows_unchanged(
    before: dict[str, tuple[tuple[object, ...], ...]],
    after: dict[str, tuple[tuple[object, ...], ...]],
) -> None:
    """Require exact stored values for every row that predated a transition."""
    assert after.keys() == before.keys()
    for table_name, rows in before.items():
        assert after[table_name][: len(rows)] == rows


def _required_instance_key(value: str | None, label: str) -> str:
    if value is None:
        message = f"{label} has no execution instance key."
        raise AssertionError(message)
    return value


def _fresh_scope(
    engine: Engine,
    *,
    project_id: int,
    sprint_id: int,
    retry_id: int,
) -> ExecutionScope:
    """Reload persistence into a fresh domain fact snapshot for each assertion."""
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    return resolve_execution_scope(
        snapshot, sprint_id=sprint_id, retry_attempt_id=retry_id
    )


def _complete_retry_task(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    task_id: int,
    suffix: str,
) -> CompleteTask:
    position = domain.position(project_id)
    complete = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.task.complete"
        and decision.instance_key == f"retry:{retry_id}:task:{task_id}"
    )
    request = CompleteTask(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=complete.decision_fingerprint,
        idempotency_key=f"{suffix}-task",
        actor="owner@example.com",
        instance_key=_required_instance_key(complete.instance_key, "Task"),
        task_id=task_id,
        outcome_summary="Re-executed the scoped work.",
        artifact_refs=("workflow/definitions/execution.py",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
    )
    result = domain.transition(request)
    assert result.ok is True
    return request


def test_completed_original_task_rejects_before_payload_validation(
    tmp_path: Path,
) -> None:
    """The original closed-Task guard keeps its established error precedence."""
    engine, _domain, project_id, sprint_id, _story_id, task_id = _file_domain(
        tmp_path / "retry-original-error-order.sqlite"
    )
    with Session(engine) as session, pytest.raises(TaskExecutionServiceError) as error:
        complete_task_in_session(
            session,
            TaskCompletionInput(
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
                outcome_summary="   ",
                artifact_refs=("workflow/definitions/execution.py",),
                acceptance_result="fully_met",
                checklist_result={"Run focused tests": "passed"},
                completed_by="owner@example.com",
                completed_at=datetime(2026, 9, 9, tzinfo=UTC),
            ),
        )
    assert error.value.detail == "Task is not open in the active Sprint."


def _reopen_engine(engine: Engine) -> Engine:
    """Force all subsequent assertions to read the durable file-backed database."""
    database_url = str(engine.url)
    engine.dispose()
    return create_engine(database_url)


def _domain_at(engine: Engine, *, minute: int) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, 9, minute, tzinfo=UTC)),
    )


def _close_retry_story(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    story_id: int,
    suffix: str,
) -> CloseStory:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.story.close"
        and item.instance_key == f"retry:{retry_id}:story:{story_id}"
    )
    request = CloseStory(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-story",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Story"),
        story_id=story_id,
        resolution="Completed",
        delivered="Fresh retry Story delivery.",
        evidence="Retry execution evidence.",
        known_gaps="None.",
    )
    assert domain.transition(request).ok is True
    return request


def _retry_review_request(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> ReviewSprint:
    """Build one exact retry review request from the currently actionable binding."""
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.sprint.review"
        and item.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    review_fingerprint = next(
        item.fingerprint
        for item in decision.fact_references
        if item.fact_type == "sprint_review"
    )
    return ReviewSprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-review",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Triage"),
        sprint_id=sprint_id,
        review_fingerprint=review_fingerprint,
    )


def _review_retry_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> ReviewSprint:
    request = _retry_review_request(
        domain,
        project_id=project_id,
        retry_id=retry_id,
        sprint_id=sprint_id,
        suffix=suffix,
    )
    assert domain.transition(request).ok is True
    return request


def _close_retry_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> CloseSprint:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.sprint.close"
        and item.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    review_fingerprint = next(
        item.fingerprint
        for item in decision.fact_references
        if item.fact_type == "sprint_review"
    )
    request = CloseSprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-close",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Triage"),
        sprint_id=sprint_id,
        review_fingerprint=review_fingerprint,
    )
    assert domain.transition(request).ok is True
    return request


def _triage_retry_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    retry_id: int,
    sprint_id: int,
    suffix: str,
) -> RecordPostSprintTriage:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.post_sprint_triage"
        and item.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    request = RecordPostSprintTriage(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=f"{suffix}-triage",
        actor="owner@example.com",
        instance_key=_required_instance_key(decision.instance_key, "Triage"),
        sprint_id=sprint_id,
        impact="none",
        canonical_payload={"summary": "Fresh retry triage complete."},
    )
    assert domain.transition(request).ok is True
    return request


def test_started_retry_exposes_only_attempt_bound_task_action(tmp_path: Path) -> None:
    """A retry must require fresh completion against its own evaluated task key."""
    engine, domain, project_id, sprint_id, _story_id, task_id = _file_domain(
        tmp_path / "retry.sqlite"
    )
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    retry = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.sprint.retry"
        and decision.instance_key == f"sprint:{sprint_id}"
    )
    planned = domain.transition(
        RetrySprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=retry.decision_fingerprint,
            idempotency_key="retry-plan",
            actor="owner@example.com",
            instance_key=retry.instance_key,
            sprint_id=sprint_id,
            confirm=True,
            rationale="Fresh completion evidence is required.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
    )
    assert planned.ok is True
    retry_id = planned.output["retry_attempt_id"]
    assert isinstance(retry_id, int)

    start_position = domain.position(project_id)
    start = next(
        decision
        for decision in start_position.decisions
        if decision.node_id == "execution.sprint.retry.start"
        and decision.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    started = domain.transition(
        StartSprintRetry(
            project_id=project_id,
            graph_version=start_position.graph_version,
            fact_fingerprint=start_position.fact_fingerprint,
            decision_fingerprint=start.decision_fingerprint,
            idempotency_key="retry-start",
            actor="owner@example.com",
            instance_key=start.instance_key,
            sprint_id=sprint_id,
            retry_attempt_id=retry_id,
        )
    )
    assert started.ok is True

    retry_position = domain.position(project_id)
    assert {
        decision.instance_key
        for decision in retry_position.decisions
        if decision.node_id == "execution.task.complete"
    } == {f"retry:{retry_id}:task:{task_id}"}


def test_started_retry_completion_writes_attempt_owned_evidence(tmp_path: Path) -> None:
    """A retry Task completion must not reuse the original completed Task row."""
    engine, domain, project_id, sprint_id, _story_id, task_id = _file_domain(
        tmp_path / "retry-complete.sqlite"
    )
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    retry = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.sprint.retry"
        and decision.instance_key == f"sprint:{sprint_id}"
    )
    planned = domain.transition(
        RetrySprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=retry.decision_fingerprint,
            idempotency_key="retry-complete-plan",
            actor="owner@example.com",
            instance_key=retry.instance_key,
            sprint_id=sprint_id,
            confirm=True,
            rationale="Fresh task evidence is required.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
    )
    retry_id = planned.output["retry_attempt_id"]
    assert isinstance(retry_id, int)
    start_position = domain.position(project_id)
    start = next(
        decision
        for decision in start_position.decisions
        if decision.node_id == "execution.sprint.retry.start"
        and decision.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    assert domain.transition(
        StartSprintRetry(
            project_id=project_id,
            graph_version=start_position.graph_version,
            fact_fingerprint=start_position.fact_fingerprint,
            decision_fingerprint=start.decision_fingerprint,
            idempotency_key="retry-complete-start",
            actor="owner@example.com",
            instance_key=start.instance_key,
            sprint_id=sprint_id,
            retry_attempt_id=retry_id,
        )
    ).ok

    retry_position = domain.position(project_id)
    complete = next(
        decision
        for decision in retry_position.decisions
        if decision.node_id == "execution.task.complete"
        and decision.instance_key == f"retry:{retry_id}:task:{task_id}"
    )
    result = domain.transition(
        CompleteTask(
            project_id=project_id,
            graph_version=retry_position.graph_version,
            fact_fingerprint=retry_position.fact_fingerprint,
            decision_fingerprint=complete.decision_fingerprint,
            idempotency_key="retry-complete-task",
            actor="owner@example.com",
            instance_key=_required_instance_key(complete.instance_key, "Task"),
            task_id=task_id,
            outcome_summary="Re-executed the scoped work.",
            artifact_refs=("workflow/definitions/execution.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    )
    assert result.ok is True
    assert result.output["retry_attempt_id"] == retry_id
    assert result.output["task_id"] == task_id
    assert "task_completion_evidence_id" not in result.output


def test_multistory_retry_scope_keeps_originals_and_candidate_isolated(
    tmp_path: Path,
) -> None:
    """Retry source has B/C fresh work, terminal external A, and unselected D."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'retry-multistory.sqlite').as_posix()}"
    )
    SQLModel.metadata.create_all(engine)
    source = seed_completed_retry_source(engine)

    retry_id = _start_retry(
        engine,
        source.domain,
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        suffix="retry-multistory",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.project_id)
    scope = resolve_execution_scope(
        snapshot,
        sprint_id=source.source_sprint_id,
        retry_attempt_id=retry_id,
    )
    assert tuple(item.story_id for item in scope.stories) == (
        source.first_story_id,
        source.second_story_id,
    )
    assert tuple(item.status for item in scope.stories) == ("To Do", "To Do")
    assert tuple(item.task_id for item in scope.tasks) == (
        source.first_task_id,
        source.second_task_id,
    )
    assert tuple(item.status for item in scope.tasks) == ("To Do", "To Do")
    external = next(
        item
        for item in scope.project_stories
        if item.story_id == source.external_story_id
    )
    assert external.status in {"Done", "Accepted"}
    assert source.candidate_story_id not in {item.story_id for item in scope.stories}


_EXPECTED_THIRD_RETRY_ORDINAL = 3
_RETRY_MUTABLE_TABLES = (
    "sprint_retry_task_states",
    "sprint_retry_story_states",
    "sprint_retry_task_evidence",
    "sprint_retry_story_closures",
    "sprint_retry_reviews",
    "sprint_retry_closures",
    "sprint_retry_triage",
)


@dataclass
class _RetryMatrixState:
    """Mutable state passed between the lifecycle phases of the retry matrix."""

    engine: Engine
    source: CompletedRetrySource
    original_rows: dict[str, tuple[tuple[object, ...], ...]]
    retry2: _StartedRetry
    first_task_request: CompleteTask | None = None
    retry2_review_request: ReviewSprint | None = None
    retry2_rows: dict[str, tuple[tuple[object, ...], ...]] | None = None


def _new_retry_matrix(tmp_path: Path) -> _RetryMatrixState:
    """Seed terminal history, open retry 2, and prove original replay is immutable."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'retry-matrix.sqlite').as_posix()}")
    SQLModel.metadata.create_all(engine)
    source = seed_completed_retry_source(engine)
    original_rows = _raw_rows(engine)
    engine = _reopen_engine(engine)
    retry2 = _start_retry_with_requests(
        engine,
        _domain_at(engine, minute=10),
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        suffix="retry-matrix-2",
    )
    _assert_preexisting_rows_unchanged(original_rows, _raw_rows(engine))
    before_original_replay = _raw_rows(engine)
    original_replay = _domain_at(engine, minute=10).transition(
        source.original_first_task_request
    )
    assert original_replay.ok is True
    assert original_replay.output == source.original_first_task_output
    assert _raw_rows(engine) == before_original_replay
    scope = _fresh_scope(
        engine,
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        retry_id=retry2.retry_id,
    )
    assert tuple(item.status for item in scope.tasks) == ("To Do", "To Do")
    available_keys = {
        item.instance_key
        for item in _domain_at(engine, minute=10).position(source.project_id).decisions
        if item.node_id == "execution.task.complete"
        and item.category is NodeCategory.AVAILABLE
    }
    assert available_keys == {f"retry:{retry2.retry_id}:task:{source.first_task_id}"}
    return _RetryMatrixState(engine, source, original_rows, retry2)


def _complete_first_retry_story(state: _RetryMatrixState) -> None:
    """Complete retry 2's dependency head and expose only its successor Task."""
    state.engine = _reopen_engine(state.engine)
    state.first_task_request = _complete_retry_task(
        _domain_at(state.engine, minute=11),
        project_id=state.source.project_id,
        retry_id=state.retry2.retry_id,
        task_id=state.source.first_task_id,
        suffix="retry-matrix-2-first",
    )
    _assert_preexisting_rows_unchanged(state.original_rows, _raw_rows(state.engine))
    scope = _fresh_scope(
        state.engine,
        project_id=state.source.project_id,
        sprint_id=state.source.source_sprint_id,
        retry_id=state.retry2.retry_id,
    )
    assert tuple(item.status for item in scope.tasks) == ("Done", "To Do")
    assert scope.tasks[1].dependencies_satisfied is False
    position = _domain_at(state.engine, minute=11).position(state.source.project_id)
    blocked_second = next(
        item
        for item in position.decisions
        if item.node_id == "execution.task.complete"
        and item.instance_key
        == f"retry:{state.retry2.retry_id}:task:{state.source.second_task_id}"
    )
    assert blocked_second.category is NodeCategory.BLOCKED
    assert blocked_second.reason_code == "TASK_DEPENDENCY_BLOCKED"
    state.engine = _reopen_engine(state.engine)
    _close_retry_story(
        _domain_at(state.engine, minute=12),
        project_id=state.source.project_id,
        retry_id=state.retry2.retry_id,
        story_id=state.source.first_story_id,
        suffix="retry-matrix-2-first",
    )
    _assert_preexisting_rows_unchanged(state.original_rows, _raw_rows(state.engine))
    scope = _fresh_scope(
        state.engine,
        project_id=state.source.project_id,
        sprint_id=state.source.source_sprint_id,
        retry_id=state.retry2.retry_id,
    )
    assert tuple(item.status for item in scope.stories) == ("Done", "To Do")
    assert scope.tasks[1].dependencies_satisfied is True


def _finish_retry2_matrix(state: _RetryMatrixState) -> None:
    """Finish retry 2 and retain exact requests for stale-action rejection checks."""
    state.engine = _reopen_engine(state.engine)
    _complete_retry_task(
        _domain_at(state.engine, minute=13),
        project_id=state.source.project_id,
        retry_id=state.retry2.retry_id,
        task_id=state.source.second_task_id,
        suffix="retry-matrix-2-second",
    )
    state.engine = _reopen_engine(state.engine)
    _close_retry_story(
        _domain_at(state.engine, minute=14),
        project_id=state.source.project_id,
        retry_id=state.retry2.retry_id,
        story_id=state.source.second_story_id,
        suffix="retry-matrix-2-second",
    )
    state.engine = _reopen_engine(state.engine)
    review_domain = _domain_at(state.engine, minute=15)
    state.retry2_review_request = _retry_review_request(
        review_domain,
        project_id=state.source.project_id,
        retry_id=state.retry2.retry_id,
        sprint_id=state.source.source_sprint_id,
        suffix="retry-matrix-2",
    )
    changed_review = state.retry2_review_request.model_copy(
        update={
            "idempotency_key": "retry-matrix-2-review-changed-binding",
            "review_fingerprint": "0"
            * len(state.retry2_review_request.review_fingerprint),
        }
    )
    _assert_failed_transition_preserves_retry_rows(
        state.engine, review_domain, changed_review
    )
    assert review_domain.transition(state.retry2_review_request).ok is True
    state.engine = _reopen_engine(state.engine)
    _close_retry_sprint(
        _domain_at(state.engine, minute=16),
        project_id=state.source.project_id,
        retry_id=state.retry2.retry_id,
        sprint_id=state.source.source_sprint_id,
        suffix="retry-matrix-2",
    )
    state.engine = _reopen_engine(state.engine)
    _triage_retry_sprint(
        _domain_at(state.engine, minute=17),
        project_id=state.source.project_id,
        retry_id=state.retry2.retry_id,
        sprint_id=state.source.source_sprint_id,
        suffix="retry-matrix-2",
    )
    state.retry2_rows = _raw_rows(state.engine)
    state.engine = _reopen_engine(state.engine)
    assert state.first_task_request is not None
    replay = _domain_at(state.engine, minute=18).transition(state.first_task_request)
    assert replay.ok is True
    _assert_preexisting_rows_unchanged(state.retry2_rows, _raw_rows(state.engine))


def _assert_failed_transition_preserves_retry_rows(
    engine: Engine,
    domain: WorkflowDomain,
    request: ReviewSprint | CompleteTask,
) -> None:
    """Assert a failed new key changes only its failure receipt."""
    before = _raw_rows(engine)
    assert domain.transition(request).ok is False
    after = _raw_rows(engine)
    _assert_preexisting_rows_unchanged(before, after)
    for table_name in _RETRY_MUTABLE_TABLES:
        assert after[table_name] == before[table_name]
    assert len(after["workflow_transition_receipts"]) == (
        len(before["workflow_transition_receipts"]) + 1
    )


def _assert_retry3_rejects_stale_retry2_actions(state: _RetryMatrixState) -> None:
    """Assert opening retry 3 rejects stale retry 2 work without mutation."""
    assert state.retry2_rows is not None
    assert state.retry2_review_request is not None
    assert state.first_task_request is not None
    state.engine = _reopen_engine(state.engine)
    retry3 = _start_retry_with_requests(
        state.engine,
        _domain_at(state.engine, minute=19),
        project_id=state.source.project_id,
        sprint_id=state.source.source_sprint_id,
        suffix="retry-matrix-3",
    )
    _assert_preexisting_rows_unchanged(state.retry2_rows, _raw_rows(state.engine))
    scope = _fresh_scope(
        state.engine,
        project_id=state.source.project_id,
        sprint_id=state.source.source_sprint_id,
        retry_id=retry3.retry_id,
    )
    with Session(state.engine) as session:
        snapshot = WorkflowFactRepository(session).load(state.source.project_id)
    retry3_fact = next(
        item
        for item in snapshot.sprint_retries
        if item.retry_attempt_id == retry3.retry_id
    )
    assert retry3_fact.ordinal == _EXPECTED_THIRD_RETRY_ORDINAL
    assert retry3_fact.predecessor_retry_attempt_id == state.retry2.retry_id
    assert not scope.task_completions
    assert not scope.story_completions
    assert tuple(item.status for item in scope.tasks) == ("To Do", "To Do")
    assert tuple(item.status for item in scope.stories) == ("To Do", "To Do")
    assert state.source.candidate_story_id not in {
        item.story_id for item in scope.stories
    }
    stale_review = state.retry2_review_request.model_copy(
        update={"idempotency_key": "retry-matrix-2-review-after-retry3"}
    )
    _assert_failed_transition_preserves_retry_rows(
        state.engine, _domain_at(state.engine, minute=19), stale_review
    )
    stale_task = state.first_task_request.model_copy(
        update={"idempotency_key": "retry-matrix-2-first-after-retry3"}
    )
    _assert_failed_transition_preserves_retry_rows(
        state.engine, _domain_at(state.engine, minute=19), stale_task
    )


def test_multistory_retries_preserve_history_reload_and_reject_stale_actions(
    tmp_path: Path,
) -> None:
    """B/C retry evidence is append-only, dependency-ordered, and repeatable."""
    state = _new_retry_matrix(tmp_path)
    _complete_first_retry_story(state)
    _finish_retry2_matrix(state)
    _assert_retry3_rejects_stale_retry2_actions(state)


def _planning_decision(domain: WorkflowDomain, project_id: int) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "planning.sprint.plan"
    )


def _assert_retry_planning_lock(domain: WorkflowDomain, project_id: int) -> None:
    decision = _planning_decision(domain, project_id)
    assert decision.category is NodeCategory.BLOCKED
    assert decision.reason_code == "SPRINT_RETRY_LIFECYCLE_ACTIVE"


@pytest.mark.parametrize("stage", ["planned", "active"])
def test_retry_stage_cannot_dispatch_an_original_triage_correction(
    tmp_path: Path,
    stage: str,
) -> None:
    """A retry owns delivery before its own close, so original triage cannot append."""
    engine, domain, project_id, sprint_id, _story_id, _task_id = _file_domain(
        tmp_path / f"retry-original-triage-{stage}.sqlite"
    )
    retry = _plan_retry_with_requests(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix=f"retry-original-triage-{stage}",
    )
    if stage == "active":
        _start_planned_retry(
            domain,
            project_id=project_id,
            sprint_id=sprint_id,
            planned=retry,
            suffix=f"retry-original-triage-{stage}",
        )
    before = _raw_rows(engine)
    decisions = tuple(
        decision
        for decision in domain.position(project_id).decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == f"sprint:{sprint_id}"
        and decision.reason_code == "POST_SPRINT_TRIAGE_CORRECTION_AVAILABLE"
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )
    dispatched = application.record_post_sprint_triage(
        PostSprintTriageRequest(
            project_id=project_id,
            instance_key=f"sprint:{sprint_id}",
            idempotency_key=f"retry-original-triage-{stage}-changed",
            actor="owner@example.com",
            impact="backlog",
            canonical_payload={"summary": "Original triage must stay immutable."},
        )
    )

    assert dispatched.ok is False
    assert decisions == ()
    assert _raw_rows(engine) == before


@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        ("closure", "WORKFLOW_FACT_CONFLICT"),
        ("triage", "POST_SPRINT_TRIAGE_FINGERPRINT_STALE"),
    ],
)
def test_retry_task_selection_keeps_older_completed_history_fail_closed(
    tmp_path: Path,
    corruption: str,
    expected_reason: str,
) -> None:
    """Retry task availability must retain original historical-integrity checks."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / f'retry-history-{corruption}.sqlite').as_posix()}"
    )
    SQLModel.metadata.create_all(engine)
    source = seed_completed_retry_source(engine)
    _start_retry_with_requests(
        engine,
        _domain_at(engine, minute=10),
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        suffix=f"retry-history-{corruption}",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.project_id)
    older_sprint_id = next(
        item.sprint_id
        for item in snapshot.sprints
        if item.sprint_id != source.source_sprint_id and item.status == "completed"
    )
    if corruption == "closure":
        corrupted = snapshot.model_copy(
            update={
                "sprint_closures": tuple(
                    item.model_copy(update={"close_fingerprint": "sha256:corrupt"})
                    if item.sprint_id == older_sprint_id
                    else item
                    for item in snapshot.sprint_closures
                )
            }
        )
    else:
        corrupted = snapshot.model_copy(
            update={
                "post_sprint_triage": tuple(
                    item.model_copy(update={"payload_fingerprint": "sha256:corrupt"})
                    if item.sprint_id == older_sprint_id
                    else item
                    for item in snapshot.post_sprint_triage
                )
            }
        )
    decisions = (
        project_graph()
        .evaluate(
            corrupted,
            datetime(2026, 9, 9, 9, 10, tzinfo=UTC),
        )
        .decisions
    )
    for node_id in (
        "execution.task.complete",
        "execution.story.close",
        "execution.sprint.review",
        "execution.sprint.close",
    ):
        scoped_decisions = tuple(
            decision for decision in decisions if decision.node_id == node_id
        )
        assert not any(
            decision.category is NodeCategory.AVAILABLE for decision in scoped_decisions
        )
        assert any(
            decision.category is NodeCategory.INVALID
            and decision.reason_code == expected_reason
            for decision in scoped_decisions
        )


def test_retry_stage_preserves_required_older_triage_recovery(
    tmp_path: Path,
) -> None:
    """Delivery nodes wait, while triage routes required recovery to older Sprint A."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'retry-missing-history.sqlite').as_posix()}"
    )
    SQLModel.metadata.create_all(engine)
    source = seed_completed_retry_source(engine)
    retry = _start_retry_with_requests(
        engine,
        _domain_at(engine, minute=10),
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        suffix="retry-missing-history",
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.project_id)
    older_sprint_id = next(
        item.sprint_id
        for item in snapshot.sprints
        if item.sprint_id != source.source_sprint_id and item.status == "completed"
    )
    missing_history = snapshot.model_copy(
        update={
            "post_sprint_triage": tuple(
                item
                for item in snapshot.post_sprint_triage
                if item.sprint_id != older_sprint_id
            )
        }
    )
    decisions = (
        project_graph()
        .evaluate(
            missing_history,
            datetime(2026, 9, 9, 9, 10, tzinfo=UTC),
        )
        .decisions
    )
    for node_id in (
        "execution.task.complete",
        "execution.story.close",
        "execution.sprint.review",
        "execution.sprint.close",
    ):
        scoped_decisions = tuple(
            decision for decision in decisions if decision.node_id == node_id
        )
        assert any(
            decision.category is NodeCategory.BLOCKED
            and decision.reason_code == "POST_SPRINT_TRIAGE_REQUIRED"
            for decision in scoped_decisions
        )
    triage = next(
        decision
        for decision in decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == f"sprint:{older_sprint_id}"
    )
    assert triage.category is NodeCategory.AVAILABLE
    assert triage.reason_code == "POST_SPRINT_TRIAGE_REQUIRED"
    assert retry.retry_id > 0


def test_retry_lifecycle_unlocks_a_real_reviewed_next_plan_candidate(
    tmp_path: Path,
) -> None:
    """Candidate D is actionable only after retry 2 has its own terminal triage."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'retry-next-plan.sqlite').as_posix()}"
    )
    SQLModel.metadata.create_all(engine)
    source = seed_completed_retry_source(engine)
    _select_for_sprint(engine, source.candidate_story_id)
    planning_domain = WorkflowDomain(
        engine=engine,
        graph=planning_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, 9, 10, tzinfo=UTC)),
    )
    _apply_current_dependencies(
        engine,
        planning_domain,
        source.project_id,
        idempotency_key="retry-next-plan-dependencies",
    )
    domain = _domain_at(engine, minute=10)
    ready_next_plan = _planning_decision(domain, source.project_id)
    assert ready_next_plan.category is NodeCategory.AVAILABLE
    assert ready_next_plan.reason_code == "NEXT_SPRINT_PLANNING_REQUIRED"

    retry2 = _plan_retry_with_requests(
        engine,
        domain,
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        suffix="retry-next-plan-2",
    )
    _assert_retry_planning_lock(domain, source.project_id)
    retry2 = _start_planned_retry(
        domain,
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
        planned=retry2,
        suffix="retry-next-plan-2",
    )
    _assert_retry_planning_lock(domain, source.project_id)

    _complete_retry_task(
        _domain_at(engine, minute=11),
        project_id=source.project_id,
        retry_id=retry2.retry_id,
        task_id=source.first_task_id,
        suffix="retry-next-plan-2-first",
    )
    _close_retry_story(
        _domain_at(engine, minute=12),
        project_id=source.project_id,
        retry_id=retry2.retry_id,
        story_id=source.first_story_id,
        suffix="retry-next-plan-2-first",
    )
    _complete_retry_task(
        _domain_at(engine, minute=13),
        project_id=source.project_id,
        retry_id=retry2.retry_id,
        task_id=source.second_task_id,
        suffix="retry-next-plan-2-second",
    )
    _close_retry_story(
        _domain_at(engine, minute=14),
        project_id=source.project_id,
        retry_id=retry2.retry_id,
        story_id=source.second_story_id,
        suffix="retry-next-plan-2-second",
    )
    _review_retry_sprint(
        _domain_at(engine, minute=15),
        project_id=source.project_id,
        retry_id=retry2.retry_id,
        sprint_id=source.source_sprint_id,
        suffix="retry-next-plan-2",
    )
    _close_retry_sprint(
        _domain_at(engine, minute=16),
        project_id=source.project_id,
        retry_id=retry2.retry_id,
        sprint_id=source.source_sprint_id,
        suffix="retry-next-plan-2",
    )
    _assert_retry_planning_lock(_domain_at(engine, minute=16), source.project_id)

    _triage_retry_sprint(
        _domain_at(engine, minute=17),
        project_id=source.project_id,
        retry_id=retry2.retry_id,
        sprint_id=source.source_sprint_id,
        suffix="retry-next-plan-2",
    )
    unlocked_next_plan = _planning_decision(
        _domain_at(engine, minute=17), source.project_id
    )
    assert unlocked_next_plan.category is NodeCategory.AVAILABLE
    assert unlocked_next_plan.reason_code == "NEXT_SPRINT_PLANNING_REQUIRED"
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.project_id)
    candidate = next(
        item for item in snapshot.stories if item.story_id == source.candidate_story_id
    )
    assert candidate.sprint_selection_state == "selected"
    assert candidate.structurally_eligible is True
    assert any(
        review.selected_story_ids == (source.candidate_story_id,)
        for review in snapshot.story_dependency_reviews
    )
    assert any(
        reference.fact_type == "candidate_set"
        and reference.fact_id == str(source.project_id)
        for reference in unlocked_next_plan.fact_references
    )


def test_terminal_retry_source_keeps_a_valid_pending_plan_reviewable(
    tmp_path: Path,
) -> None:
    """A fully triaged delivery leaves the untouched fourth candidate reviewable."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'retry-pending-plan.sqlite').as_posix()}"
    )
    SQLModel.metadata.create_all(engine)
    source = seed_completed_retry_source(engine)
    domain, plan_id = record_pending_successor_plan(engine, source)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.project_id)
    assert retry_blocks_planning(snapshot) is False
    candidate = next(
        item for item in snapshot.stories if item.story_id == source.candidate_story_id
    )
    assert candidate.sprint_selection_state == "selected"
    pending_review = next(
        item
        for item in domain.position(source.project_id).decisions
        if item.node_id == "planning.sprint.review"
    )
    assert pending_review.category is NodeCategory.WAITING
    assert pending_review.reason_code == "SPRINT_PLAN_REVIEW_REQUIRED"
    assert any(
        reference.fact_id == str(plan_id)
        for reference in pending_review.fact_references
    )
    assert current_execution_scope(snapshot) is None


def test_retry_task_key_without_a_persisted_attempt_never_falls_back(
    tmp_path: Path,
) -> None:
    """A retry-shaped key must not select the original active Task."""
    database_path = tmp_path / "retry-no-fallback.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    SQLModel.metadata.create_all(engine)
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, tzinfo=UTC)),
    )
    original = next(
        decision
        for decision in domain.position(project_id).decisions
        if decision.node_id == "execution.task.complete"
        and decision.instance_key == f"task:{task_id}"
    )
    selector = ExecutionActionSelectionService(engine=engine)
    assert selector.prepare_task_completion(
        project_id=project_id,
        decision=original,
    ) == (task_id, _sprint_id)

    nonexistent_retry = original.model_copy(
        update={"instance_key": f"retry:999999:task:{task_id}"}
    )
    assert (
        selector.prepare_task_completion(
            project_id=project_id,
            decision=nonexistent_retry,
        )
        is None
    )


def test_invalid_retry_key_never_selects_original_lifecycle_work(
    tmp_path: Path,
) -> None:
    """Every selector rejects an unknown retry attempt instead of using originals."""
    database_path = tmp_path / "retry-lifecycle-no-fallback.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    SQLModel.metadata.create_all(engine)
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, tzinfo=UTC)),
    )
    selector = ExecutionActionSelectionService(engine=engine)

    position = domain.position(project_id)
    task = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.task.complete"
        and decision.instance_key == f"task:{task_id}"
    )
    assert selector.prepare_task_completion(
        project_id=project_id,
        decision=task,
    ) == (task_id, sprint_id)
    assert (
        selector.prepare_task_completion(
            project_id=project_id,
            decision=task.model_copy(
                update={"instance_key": f"retry:999999:task:{task_id}"}
            ),
        )
        is None
    )
    assert domain.transition(
        CompleteTask(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=task.decision_fingerprint,
            idempotency_key="original-no-fallback-task",
            actor="owner@example.com",
            instance_key=_required_instance_key(task.instance_key, "Task"),
            task_id=task_id,
            outcome_summary="Original task positive control.",
            artifact_refs=("workflow/definitions/execution.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    ).ok

    position = domain.position(project_id)
    story = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.story.close"
        and decision.instance_key == f"story:{story_id}"
    )
    story_target = selector.prepare_story_close(project_id=project_id, decision=story)
    assert story_target is not None
    assert story_target[:2] == (story_id, sprint_id)
    assert (
        selector.prepare_story_close(
            project_id=project_id,
            decision=story.model_copy(
                update={"instance_key": f"retry:999999:story:{story_id}"}
            ),
        )
        is None
    )
    assert domain.transition(
        CloseStory(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=story.decision_fingerprint,
            idempotency_key="original-no-fallback-story",
            actor="owner@example.com",
            instance_key=_required_instance_key(story.instance_key, "Story"),
            story_id=story_id,
            resolution="Completed",
            delivered="Original Story positive control.",
            evidence="Focused tests pass.",
            known_gaps="None.",
        )
    ).ok

    position = domain.position(project_id)
    review = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.sprint.review"
        and decision.instance_key == f"sprint:{sprint_id}"
    )
    review_target = selector.prepare_sprint_review(
        project_id=project_id, decision=review
    )
    assert review_target is not None
    assert review_target[0] == sprint_id
    assert (
        selector.prepare_sprint_review(
            project_id=project_id,
            decision=review.model_copy(
                update={"instance_key": f"retry:999999:sprint:{sprint_id}"}
            ),
        )
        is None
    )
    assert domain.transition(
        ReviewSprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key="original-no-fallback-review",
            actor="owner@example.com",
            instance_key=review.instance_key,
            sprint_id=sprint_id,
            review_fingerprint=review_target[1],
        )
    ).ok

    position = domain.position(project_id)
    close = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.sprint.close"
        and decision.instance_key == f"sprint:{sprint_id}"
    )
    close_target = selector.prepare_sprint_close(project_id=project_id, decision=close)
    assert close_target is not None
    assert close_target[0] == sprint_id
    assert (
        selector.prepare_sprint_close(
            project_id=project_id,
            decision=close.model_copy(
                update={"instance_key": f"retry:999999:sprint:{sprint_id}"}
            ),
        )
        is None
    )
    assert domain.transition(
        CloseSprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=close.decision_fingerprint,
            idempotency_key="original-no-fallback-close",
            actor="owner@example.com",
            instance_key=close.instance_key,
            sprint_id=sprint_id,
            review_fingerprint=close_target[1],
        )
    ).ok

    position = domain.position(project_id)
    triage = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == f"sprint:{sprint_id}"
    )
    triage_target = selector.prepare_post_sprint_triage(
        project_id=project_id,
        decision=triage,
    )
    assert triage_target is not None
    assert triage_target[0] == sprint_id
    assert (
        selector.prepare_post_sprint_triage(
            project_id=project_id,
            decision=triage.model_copy(
                update={"instance_key": f"retry:999999:sprint:{sprint_id}"}
            ),
        )
        is None
    )


def test_application_completes_the_exact_retry_bound_task(tmp_path: Path) -> None:
    """Semantic selection must never use original work for a retry key."""
    engine, domain, project_id, sprint_id, _story_id, task_id = _file_domain(
        tmp_path / "retry-application.sqlite"
    )
    retry_id = _start_retry(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix="retry-application",
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )
    result = application.complete_task(
        CompleteTaskRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:task:{task_id}",
            idempotency_key="retry-application-task",
            actor="owner@example.com",
            outcome_summary="Application-selected retry completion.",
            artifact_refs=("workflow/definitions/execution.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    )
    assert result.ok is True
    assert result.output["retry_attempt_id"] == retry_id


def test_application_closes_the_exact_retry_bound_story(tmp_path: Path) -> None:
    """The semantic Story action must use retry-local completion evidence."""
    engine, domain, project_id, sprint_id, story_id, task_id = _file_domain(
        tmp_path / "retry-application-story.sqlite"
    )
    retry_id = _start_retry(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix="retry-application-story",
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )
    assert application.complete_task(
        CompleteTaskRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:task:{task_id}",
            idempotency_key="retry-application-story-task",
            actor="owner@example.com",
            outcome_summary="Application-selected retry completion.",
            artifact_refs=("workflow/definitions/execution.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    ).ok
    closed = application.close_story(
        CloseStoryRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:story:{story_id}",
            idempotency_key="retry-application-story-close",
            actor="owner@example.com",
            resolution="Completed",
            delivered="Application-selected retry closure.",
            evidence="Focused retry tests pass.",
            known_gaps="None.",
        )
    )
    assert closed.ok is True
    assert closed.output["retry_attempt_id"] == retry_id


def test_application_reviews_the_exact_retry_bound_sprint(tmp_path: Path) -> None:
    """The semantic Sprint review must use retry-local Story closure evidence."""
    engine, domain, project_id, sprint_id, story_id, task_id = _file_domain(
        tmp_path / "retry-application-review.sqlite"
    )
    retry_id = _start_retry(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix="retry-application-review",
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )
    assert application.complete_task(
        CompleteTaskRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:task:{task_id}",
            idempotency_key="retry-application-review-task",
            actor="owner@example.com",
            outcome_summary="Application-selected retry completion.",
            artifact_refs=("workflow/definitions/execution.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    ).ok
    assert application.close_story(
        CloseStoryRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:story:{story_id}",
            idempotency_key="retry-application-review-story",
            actor="owner@example.com",
            resolution="Completed",
            delivered="Application-selected retry closure.",
            evidence="Focused retry tests pass.",
            known_gaps="None.",
        )
    ).ok

    reviewed = application.review_sprint(
        SprintReviewRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:sprint:{sprint_id}",
            idempotency_key="retry-application-review-sprint",
            actor="owner@example.com",
        )
    )
    assert reviewed.ok is True
    assert reviewed.output["retry_attempt_id"] == retry_id


def test_application_closes_the_exact_retry_bound_sprint(tmp_path: Path) -> None:
    """The semantic Sprint close must use retry-local review evidence."""
    engine, domain, project_id, sprint_id, story_id, task_id = _file_domain(
        tmp_path / "retry-application-close.sqlite"
    )
    retry_id = _start_retry(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix="retry-application-close",
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )
    assert application.complete_task(
        CompleteTaskRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:task:{task_id}",
            idempotency_key="retry-application-close-task",
            actor="owner@example.com",
            outcome_summary="Application-selected retry completion.",
            artifact_refs=("workflow/definitions/execution.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    ).ok
    assert application.close_story(
        CloseStoryRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:story:{story_id}",
            idempotency_key="retry-application-close-story",
            actor="owner@example.com",
            resolution="Completed",
            delivered="Application-selected retry closure.",
            evidence="Focused retry tests pass.",
            known_gaps="None.",
        )
    ).ok
    assert application.review_sprint(
        SprintReviewRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:sprint:{sprint_id}",
            idempotency_key="retry-application-close-review",
            actor="owner@example.com",
        )
    ).ok

    closed = application.close_sprint(
        SprintCloseRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:sprint:{sprint_id}",
            idempotency_key="retry-application-close-sprint",
            actor="owner@example.com",
        )
    )
    assert closed.ok is True
    assert closed.output["retry_attempt_id"] == retry_id


def test_application_triages_the_exact_retry_bound_sprint(tmp_path: Path) -> None:
    """The semantic triage must use retry-local Sprint closure evidence."""
    engine, domain, project_id, sprint_id, story_id, task_id = _file_domain(
        tmp_path / "retry-application-triage.sqlite"
    )
    retry_id = _start_retry(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix="retry-application-triage",
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )
    assert application.complete_task(
        CompleteTaskRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:task:{task_id}",
            idempotency_key="retry-application-triage-task",
            actor="owner@example.com",
            outcome_summary="Application-selected retry completion.",
            artifact_refs=("workflow/definitions/execution.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    ).ok
    assert application.close_story(
        CloseStoryRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:story:{story_id}",
            idempotency_key="retry-application-triage-story",
            actor="owner@example.com",
            resolution="Completed",
            delivered="Application-selected retry closure.",
            evidence="Focused retry tests pass.",
            known_gaps="None.",
        )
    ).ok
    assert application.review_sprint(
        SprintReviewRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:sprint:{sprint_id}",
            idempotency_key="retry-application-triage-review",
            actor="owner@example.com",
        )
    ).ok
    assert application.close_sprint(
        SprintCloseRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:sprint:{sprint_id}",
            idempotency_key="retry-application-triage-close",
            actor="owner@example.com",
        )
    ).ok

    triaged = application.record_post_sprint_triage(
        PostSprintTriageRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:sprint:{sprint_id}",
            idempotency_key="retry-application-triage-sprint",
            actor="owner@example.com",
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    )
    assert triaged.ok is True
    assert triaged.output["retry_attempt_id"] == retry_id

    duplicate = application.record_post_sprint_triage(
        PostSprintTriageRequest(
            project_id=project_id,
            instance_key=f"retry:{retry_id}:sprint:{sprint_id}",
            idempotency_key="retry-application-triage-duplicate",
            actor="owner@example.com",
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    )
    assert duplicate.ok is False
    correction = next(
        decision
        for decision in domain.position(project_id).decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    assert correction.reason_code == "POST_SPRINT_TRIAGE_CORRECTION_AVAILABLE"
    current = next(
        reference
        for reference in correction.fact_references
        if reference.fact_type == "post_sprint_triage"
    )
    corrected = application.record_post_sprint_triage(
        PostSprintTriageRequest(
            project_id=project_id,
            instance_key=_required_instance_key(correction.instance_key, "Triage"),
            idempotency_key="retry-application-triage-correction",
            actor="owner@example.com",
            impact="backlog",
            canonical_payload={"summary": "Backlog follow-up required."},
        )
    )
    assert corrected.ok is True
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    scope = resolve_execution_scope(
        snapshot,
        sprint_id=sprint_id,
        retry_attempt_id=retry_id,
    )
    expected_triage_rows = 2
    assert len(scope.post_sprint_triage) == expected_triage_rows
    assert scope.post_sprint_triage[-1].supersedes_triage_id == int(current.fact_id)


def test_completed_retry_task_exposes_attempt_bound_story_close(tmp_path: Path) -> None:
    """Fresh retry Task evidence must unlock only its retry-bound Story close."""
    engine, domain, project_id, sprint_id, story_id, task_id = _file_domain(
        tmp_path / "retry-story.sqlite"
    )
    retry_id = _start_retry(
        engine,
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        suffix="retry-story",
    )
    _complete_retry_task(
        domain,
        project_id=project_id,
        retry_id=retry_id,
        task_id=task_id,
        suffix="retry-story",
    )

    position = domain.position(project_id)
    assert {
        decision.instance_key
        for decision in position.decisions
        if decision.node_id == "execution.story.close"
    } == {f"retry:{retry_id}:story:{story_id}"}
    close = next(
        decision
        for decision in position.decisions
        if decision.node_id == "execution.story.close"
        and decision.instance_key == f"retry:{retry_id}:story:{story_id}"
    )
    result = domain.transition(
        CloseStory(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=close.decision_fingerprint,
            idempotency_key="retry-story-close",
            actor="owner@example.com",
            instance_key=_required_instance_key(close.instance_key, "Close"),
            story_id=story_id,
            resolution="Completed",
            delivered="Scoped Story delivered.",
            evidence="Focused retry tests pass.",
            known_gaps="None.",
        )
    )
    assert result.ok is True
    assert result.output["retry_attempt_id"] == retry_id
    assert "story_closure_id" not in result.output
    review_position = domain.position(project_id)
    assert {
        decision.instance_key
        for decision in review_position.decisions
        if decision.node_id == "execution.sprint.review"
    } == {f"retry:{retry_id}:sprint:{sprint_id}"}
    review = next(
        decision
        for decision in review_position.decisions
        if decision.node_id == "execution.sprint.review"
        and decision.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    review_fingerprint = next(
        reference.fingerprint
        for reference in review.fact_references
        if reference.fact_type == "sprint_review"
    )
    reviewed = domain.transition(
        ReviewSprint(
            project_id=project_id,
            graph_version=review_position.graph_version,
            fact_fingerprint=review_position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key="retry-sprint-review",
            actor="owner@example.com",
            instance_key=review.instance_key,
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    )
    assert reviewed.ok is True
    assert reviewed.output["retry_attempt_id"] == retry_id
    assert "sprint_review_id" not in reviewed.output
    close_position = domain.position(project_id)
    assert {
        decision.instance_key
        for decision in close_position.decisions
        if decision.node_id == "execution.sprint.close"
    } == {f"retry:{retry_id}:sprint:{sprint_id}"}
    close_sprint = next(
        decision
        for decision in close_position.decisions
        if decision.node_id == "execution.sprint.close"
        and decision.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    closed = domain.transition(
        CloseSprint(
            project_id=project_id,
            graph_version=close_position.graph_version,
            fact_fingerprint=close_position.fact_fingerprint,
            decision_fingerprint=close_sprint.decision_fingerprint,
            idempotency_key="retry-sprint-close",
            actor="owner@example.com",
            instance_key=close_sprint.instance_key,
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    )
    assert closed.ok is True
    assert closed.output["retry_attempt_id"] == retry_id
    assert "sprint_closure_id" not in closed.output
    triage_position = domain.position(project_id)
    assert {
        decision.instance_key
        for decision in triage_position.decisions
        if decision.node_id == "execution.post_sprint_triage"
    } == {f"retry:{retry_id}:sprint:{sprint_id}"}
    triage = next(
        decision
        for decision in triage_position.decisions
        if decision.node_id == "execution.post_sprint_triage"
        and decision.instance_key == f"retry:{retry_id}:sprint:{sprint_id}"
    )
    triaged = domain.transition(
        RecordPostSprintTriage(
            project_id=project_id,
            graph_version=triage_position.graph_version,
            fact_fingerprint=triage_position.fact_fingerprint,
            decision_fingerprint=triage.decision_fingerprint,
            idempotency_key="retry-sprint-triage",
            actor="owner@example.com",
            instance_key=_required_instance_key(triage.instance_key, "Triage"),
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    )
    assert triaged.ok is True
    assert triaged.output["retry_attempt_id"] == retry_id
    assert "triage_id" not in triaged.output
