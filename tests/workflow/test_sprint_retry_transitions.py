"""Guarded Sprint retry preview and transition coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from models.core import Project, UserStoryDependency
from models.repository import RepositoryBinding
from models.sprint_retry import (
    SprintRetryAttempt,
    SprintRetryStoryState,
    SprintRetryTaskState,
)
from models.workflow import (
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactRepository
from services.agent_workbench.sprint_phase import (
    ActiveSprintRetryExistsError,
    RecordSprintPlanInput,
    SprintStartInput,
    record_sprint_plan_in_session,
    start_sprint_in_session,
)
from services.sprint_retry import (
    build_sprint_retry_preview,
    retry_sprint_in_session,
    start_sprint_retry_in_session,
)
from services.story_dependencies import (
    ApplyStoryDependenciesInput,
    StoryDependencyGraphError,
    apply_story_dependencies_in_session,
)
from tests.workflow.execution_fixtures import (
    seed_started_execution_with_transitive_dependency,
)
from tests.workflow.test_execution_transitions import (
    _close_execution_sprint,
    _complete_execution_sprint,
    _triage_execution_sprint,
)
from workflow.clock import FixedClock
from workflow.definitions.planning import (
    dependency_review_lifecycle_locked,
    planning_graph,
)
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import (
    business_fact_fingerprint,
    canonical_hash,
    canonical_json,
    fact_fingerprint,
)
from workflow.requests import RecordPostSprintTriage, RetrySprint, StartSprintRetry

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.contracts.sprint import SprintPlannerOutput


FIRST_RETRY_ORDINAL = 2


def _file_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    SQLModel.metadata.create_all(engine)
    return engine


def test_retry_preview_is_read_only_for_exact_completed_triaged_sprint(
    tmp_path: Path,
) -> None:
    """Preview exposes only the selected completed Sprint without persistence."""
    engine = _file_engine(tmp_path / "retry-preview.sqlite")
    domain, project_id, sprint_id, story_id, task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        before = snapshot.model_dump(mode="json")
        preview = build_sprint_retry_preview(
            session,
            snapshot=snapshot,
            sprint_id=sprint_id,
        )
        after = WorkflowFactRepository(session).load(project_id).model_dump(mode="json")
    assert preview.blockers == ()
    assert preview.project_id == project_id
    assert preview.sprint_id == sprint_id
    assert preview.next_ordinal == FIRST_RETRY_ORDINAL
    assert preview.story_ids == (story_id,)
    assert preview.task_ids == (task_id,)
    assert before == after


def test_confirmed_retry_creates_planned_scope_then_requires_explicit_start(
    tmp_path: Path,
) -> None:
    """A confirmed retry is atomic and the new attempt does not auto-start."""
    engine = _file_engine(tmp_path / "retry-transition.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    retry_domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, tzinfo=UTC)),
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        preview = build_sprint_retry_preview(
            session, snapshot=snapshot, sprint_id=sprint_id
        )
    position = retry_domain.position(project_id)
    retry_decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.sprint.retry"
        and item.instance_key == f"sprint:{sprint_id}"
    )
    applied = retry_domain.transition(
        RetrySprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=retry_decision.decision_fingerprint,
            idempotency_key="retry-sprint-once",
            actor="owner@example.com",
            instance_key=f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            confirm=True,
            rationale="Re-run approved work with fresh evidence.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
    )
    assert applied.ok is True
    retry_id = applied.output["retry_attempt_id"]
    assert isinstance(retry_id, int)
    replayed = retry_domain.transition(
        RetrySprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=retry_decision.decision_fingerprint,
            idempotency_key="retry-sprint-once",
            actor="owner@example.com",
            instance_key=f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            confirm=True,
            rationale="Re-run approved work with fresh evidence.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
    )
    assert replayed.ok is True
    assert replayed.replayed is True
    assert replayed.output["retry_attempt_id"] == retry_id
    changed_replay = retry_domain.transition(
        RetrySprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=retry_decision.decision_fingerprint,
            idempotency_key="retry-sprint-once",
            actor="owner@example.com",
            instance_key=f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            confirm=True,
            rationale="Changed rationale must not replay.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
    )
    assert changed_replay.ok is False
    assert changed_replay.error is not None
    assert "idempotency key" in changed_replay.error.message
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(SprintRetryAttempt).where(
                        SprintRetryAttempt.project_id == project_id
                    )
                ).all()
            )
            == 1
        )
    start_position = retry_domain.position(project_id)
    start_decision = next(
        item
        for item in start_position.decisions
        if item.node_id == "execution.sprint.retry.start"
    )
    started = retry_domain.transition(
        StartSprintRetry(
            project_id=project_id,
            graph_version=start_position.graph_version,
            fact_fingerprint=start_position.fact_fingerprint,
            decision_fingerprint=start_decision.decision_fingerprint,
            idempotency_key="start-retry-once",
            actor="owner@example.com",
            instance_key=start_decision.instance_key,
            sprint_id=sprint_id,
            retry_attempt_id=retry_id,
        )
    )
    assert started.ok is True
    assert started.output["status"] == "Active"


def test_distinct_retry_keys_from_one_preview_create_one_attempt(
    tmp_path: Path,
) -> None:
    """The Project write lock and preview recheck admit one of two racing retries."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    engine = _file_engine(tmp_path / "retry-distinct-key-race.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "execution.sprint.retry"
    )

    def apply(key: str) -> bool:
        worker = WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=datetime(2026, 9, 9, tzinfo=UTC)),
        )
        return worker.transition(
            RetrySprint(
                project_id=project_id,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                idempotency_key=key,
                actor="owner@example.com",
                instance_key=f"sprint:{sprint_id}",
                sprint_id=sprint_id,
                confirm=True,
                rationale="Fresh evidence requires a retry.",
                expected_state_fingerprint=preview.expected_state_fingerprint,
            )
        ).ok

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(apply, ("retry-race-left", "retry-race-right")))
    assert sum(results) == 1
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(SprintRetryAttempt).where(
                        SprintRetryAttempt.project_id == project_id
                    )
                ).all()
            )
            == 1
        )


def test_retry_failure_after_progress_rolls_back_receipt_and_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-progress exception rolls back every retry row and receipt."""
    engine = _file_engine(tmp_path / "retry-domain-rollback.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "execution.sprint.retry"
    )
    request = RetrySprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key="domain-rollback-after-progress",
        actor="owner@example.com",
        instance_key=f"sprint:{sprint_id}",
        sprint_id=sprint_id,
        confirm=True,
        rationale="Fresh evidence requires a retry.",
        expected_state_fingerprint=preview.expected_state_fingerprint,
    )
    import workflow.handlers.sprint_retry as retry_handler  # noqa: PLC0415

    original = retry_handler.retry_sprint_in_session
    error_message = "Injected failure after retry progress insertion."

    def fail_after_progress(*args: object, **kwargs: object) -> object:
        original(*args, **kwargs)
        raise RuntimeError(error_message)

    monkeypatch.setattr(retry_handler, "retry_sprint_in_session", fail_after_progress)
    with pytest.raises(RuntimeError, match="after retry progress"):
        domain.transition(request)
    with Session(engine) as session:
        assert (
            session.exec(
                select(SprintRetryAttempt).where(
                    SprintRetryAttempt.project_id == project_id
                )
            ).all()
            == []
        )
        assert (
            session.exec(
                select(SprintRetryStoryState).where(
                    SprintRetryStoryState.project_id == project_id
                )
            ).all()
            == []
        )
        assert (
            session.exec(
                select(SprintRetryTaskState).where(
                    SprintRetryTaskState.project_id == project_id
                )
            ).all()
            == []
        )
        assert (
            session.exec(
                select(WorkflowTransitionReceipt).where(
                    WorkflowTransitionReceipt.idempotency_key == request.idempotency_key
                )
            ).all()
            == []
        )

def test_retry_success_clears_only_its_receipt_marker(tmp_path: Path) -> None:
    """Successful retry leaves session marker state clean for the next receipt."""
    engine = _file_engine(tmp_path / "retry-marker-cleanup.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "execution.sprint.retry"
    )
    request = RetrySprint(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key="marker-cleanup-success",
        actor="owner@example.com",
        instance_key=f"sprint:{sprint_id}",
        sprint_id=sprint_id,
        confirm=True,
        rationale="Fresh evidence requires a retry.",
        expected_state_fingerprint=preview.expected_state_fingerprint,
    )
    with Session(engine) as session:
        result = domain.transition_in_session(session, request)
        assert result.ok is True
        assert "agileforge.active_transition_receipt" not in session.info
        session.commit()


def test_retry_blocks_direct_planning_and_dependency_writers(tmp_path: Path) -> None:
    """Direct writers cannot bypass a planned retry's delivery ownership."""
    engine = _file_engine(tmp_path / "retry-direct-writers.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "execution.sprint.retry"
    )
    applied = domain.transition(
        RetrySprint(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key="direct-writers-retry",
            actor="owner@example.com",
            instance_key=f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            confirm=True,
            rationale="Fresh evidence requires a retry.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
    )
    assert applied.ok is True
    with Session(engine) as session:
        correction = next(
            item
            for item in planning_graph()
            .evaluate(
                WorkflowFactRepository(session).load(project_id),
                datetime(2026, 9, 9, tzinfo=UTC),
            )
            .decisions
            if item.node_id == "planning.story.generate"
        )
    assert correction.reason_code == "SPRINT_RETRY_LIFECYCLE_ACTIVE"
    with Session(engine) as session:
        with pytest.raises(ActiveSprintRetryExistsError):
            record_sprint_plan_in_session(
                session,
                inputs=RecordSprintPlanInput(
                    project_id=project_id,
                    spec_version_id=0,
                    spec_hash="unused",
                    team_name="unused",
                    planner_output=cast("SprintPlannerOutput", object()),
                    actor="owner@example.com",
                    recorded_at=datetime(2026, 9, 9, tzinfo=UTC),
                ),
            )
        with pytest.raises(ActiveSprintRetryExistsError):
            start_sprint_in_session(
                session,
                SprintStartInput(
                    project_id=project_id,
                    expected_sprint_id=sprint_id,
                    expected_task_content_fingerprint="unused",
                    decision_fingerprint="unused",
                    started_by="owner@example.com",
                    started_at=datetime(2026, 9, 9, tzinfo=UTC),
                ),
            )
        with pytest.raises(StoryDependencyGraphError, match="LIFECYCLE_LOCKED"):
            apply_story_dependencies_in_session(
                session,
                inputs=ApplyStoryDependenciesInput(
                    project_id=project_id,
                    selected_story_ids=(),
                    reviewed_edges=(),
                    source_fingerprint="unused",
                    reviewer="owner@example.com",
                    reviewed_at=datetime(2026, 9, 9, tzinfo=UTC),
                ),
            )


def test_dependency_lifecycle_lock_covers_every_unresolved_retry_state(
    tmp_path: Path,
) -> None:
    """Planning stays locked for planned, active, and untriaged completed retries."""
    engine = _file_engine(tmp_path / "retry-dependency-lock.sqlite")
    domain, project_id, _sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    # The original Sprint still needs triage for the fixture. Isolate the retry
    # predicate by replacing its validated facts with a terminally triaged copy.
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=_sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    assert snapshot.sprint_retries == ()
    from workflow.facts import SprintRetryFact  # noqa: PLC0415

    base = SprintRetryFact(
        retry_attempt_id=1,
        project_id=project_id,
        sprint_id=_sprint_id,
        ordinal=2,
        predecessor_retry_attempt_id=None,
        contract_fingerprint="contract",
        created_by="owner@example.com",
        rationale="Fresh evidence.",
        creation_fingerprint="creation",
        creation_receipt_key="key",
        created_at=datetime(2026, 9, 9, tzinfo=UTC),
        status="planned",
    )
    assert dependency_review_lifecycle_locked(
        snapshot.model_copy(update={"sprint_retries": (base,)})
    )
    assert dependency_review_lifecycle_locked(
        snapshot.model_copy(
            update={"sprint_retries": (base.model_copy(update={"status": "active"}),)}
        )
    )
    assert dependency_review_lifecycle_locked(
        snapshot.model_copy(
            update={
                "sprint_retries": (base.model_copy(update={"status": "completed"}),)
            }
        )
    )


@pytest.mark.parametrize("confirmation", [False, 0, 1, "true", "yes"])
def test_retry_request_requires_literal_boolean_confirmation(
    confirmation: object,
) -> None:
    """Invalid confirmation fails while constructing the request before any receipt."""
    payload = {
        "project_id": 1,
        "graph_version": "graph",
        "fact_fingerprint": "facts",
        "decision_fingerprint": "decision",
        "idempotency_key": "key",
        "actor": "owner@example.com",
        "instance_key": "sprint:1",
        "sprint_id": 1,
        "confirm": confirmation,
        "rationale": "Fresh evidence.",
        "expected_state_fingerprint": "expected",
    }
    with pytest.raises(ValidationError):
        RetrySprint.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("actor", "   "), ("rationale", "\t")],
)
def test_retry_request_requires_nonblank_accountability(field: str, value: str) -> None:
    """Blank audit fields fail before a transition receipt can be claimed."""
    payload: dict[str, object] = {
        "project_id": 1,
        "graph_version": "graph",
        "fact_fingerprint": "facts",
        "decision_fingerprint": "decision",
        "idempotency_key": "key",
        "actor": "owner@example.com",
        "instance_key": "sprint:1",
        "sprint_id": 1,
        "confirm": True,
        "rationale": "Fresh evidence.",
        "expected_state_fingerprint": "expected",
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        RetrySprint.model_validate(payload)


def test_retry_start_rechecks_live_selected_requirements(tmp_path: Path) -> None:
    """A changed selected requirement must not start an already planned retry."""
    engine = _file_engine(tmp_path / "retry-live-start.sqlite")
    domain, project_id, sprint_id, story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        preview = build_sprint_retry_preview(
            session, snapshot=snapshot, sprint_id=sprint_id
        )
        request = RetrySprint(
            project_id=project_id,
            graph_version="graph",
            fact_fingerprint="facts",
            decision_fingerprint="decision",
            idempotency_key="live-start-retry",
            actor="owner@example.com",
            instance_key=f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            confirm=True,
            rationale="Fresh evidence.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
        created = retry_sprint_in_session(
            session,
            request=request,
            snapshot=snapshot,
            now=datetime(2026, 9, 9, tzinfo=UTC),
        )
        session.commit()
    with Session(engine) as session:
        current = WorkflowFactRepository(session).load(project_id)
        changed = current.model_copy(
            update={
                "stories": tuple(
                    item.model_copy(update={"is_superseded": True})
                    if item.story_id == story_id
                    else item
                    for item in current.stories
                )
            }
        )
        with pytest.raises(ValueError, match="selected requirements"):
            start_sprint_retry_in_session(
                session,
                request=StartSprintRetry(
                    project_id=project_id,
                    graph_version="graph",
                    fact_fingerprint="facts",
                    decision_fingerprint="decision",
                    idempotency_key="live-start",
                    actor="owner@example.com",
                    instance_key=f"retry:{created.retry_attempt_id}:sprint:{sprint_id}",
                    sprint_id=sprint_id,
                    retry_attempt_id=created.retry_attempt_id,
                ),
                snapshot=changed,
                now=datetime(2026, 9, 9, tzinfo=UTC),
            )


def test_preview_reads_descriptive_persisted_repository_provenance(
    tmp_path: Path,
) -> None:
    """Preview ignores a dirty caller identity-map binding without flushing it."""
    engine = _file_engine(tmp_path / "retry-repository-preview.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        binding = RepositoryBinding(
            project_id=project_id,
            worktree_path="C:/persisted",
            common_git_dir="C:/git",
            head_sha="a" * 40,
            branch_name="main",
            detached_head=False,
            dirty=False,
            status_fingerprint="sha256:" + "a" * 64,
            remotes_json="[]",
            warnings_json="[]",
            probe_version="test",
            recorded_by="owner@example.com",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        project = session.get(Project, project_id)
        assert project is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()
    with Session(engine) as session:
        binding = session.get(RepositoryBinding, 1)
        assert binding is not None
        binding.worktree_path = "C:/dirty-caller-state"
        snapshot = WorkflowFactRepository(session).load(project_id)
        preview = build_sprint_retry_preview(
            session,
            snapshot=snapshot,
            sprint_id=sprint_id,
        )
        assert preview.repository_provenance is not None
        assert preview.repository_provenance["worktree_path"] == "C:/persisted"
        assert binding.worktree_path == "C:/dirty-caller-state"


def test_retry_create_rechecks_live_transitive_source_dependencies(
    tmp_path: Path,
) -> None:
    """A dependency edit after preview blocks creation as well as later start."""
    engine = _file_engine(tmp_path / "retry-live-create.sqlite")
    (
        project_id,
        sprint_id,
        story_id,
        _external_story_id,
        _transitive_story_id,
        task_id,
        _dependency_ab_id,
        dependency_bc_id,
    ) = seed_started_execution_with_transitive_dependency(engine)
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, tzinfo=UTC)),
    )
    _close_execution_sprint(
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        preview = build_sprint_retry_preview(
            session, snapshot=snapshot, sprint_id=sprint_id
        )
        assert preview.blockers == ()
        dependency = session.get(UserStoryDependency, dependency_bc_id)
        assert dependency is not None
        dependency.reason = "Changed after the source Sprint completed."
        session.add(dependency)
        session.commit()
    with Session(engine) as session:
        current = WorkflowFactRepository(session).load(project_id)
        changed_preview = build_sprint_retry_preview(
            session, snapshot=current, sprint_id=sprint_id
        )
        assert {item.code for item in changed_preview.blockers} >= {
            "SOURCE_CONTRACT_CHANGED"
        }
        request = RetrySprint(
            project_id=project_id,
            graph_version="graph",
            fact_fingerprint="facts",
            decision_fingerprint="decision",
            idempotency_key="changed-contract-retry",
            actor="owner@example.com",
            instance_key=f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            confirm=True,
            rationale="Re-run approved work with fresh evidence.",
            expected_state_fingerprint=changed_preview.expected_state_fingerprint,
        )
        with pytest.raises(ValueError, match="stale or blocked"):
            retry_sprint_in_session(
                session,
                request=request,
                snapshot=current,
                now=datetime(2026, 9, 9, tzinfo=UTC),
            )


def test_generation_guard_classifies_canonical_plan_output(tmp_path: Path) -> None:
    """A plan-generation success is linked only through its canonical output."""
    engine = _file_engine(tmp_path / "retry-generation-guard.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    now = datetime(2026, 9, 9, tzinfo=UTC)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        plan = next(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
        )
        attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id="planning.sprint.plan",
            instance_key=None,
            graph_version="test",
            fact_fingerprint="facts",
            business_fact_fingerprint=business_fact_fingerprint(snapshot),
            decision_fingerprint="decision",
            normalized_input_json="{}",
            input_fingerprint="input",
            model_id="test",
            execution_settings_json="{}",
            idempotency_key="guard-plan-attempt",
            actor="owner@example.com",
            started_at=now,
            lease_expires_at=datetime(2026, 9, 10, tzinfo=UTC),
            attempt_fingerprint="attempt",
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        attempt_id = attempt.workflow_node_attempt_id
        output = {
            "sprint_plan_artifact_id": plan.artifact_id,
            "plan_fingerprint": plan.artifact_fingerprint,
        }
        outcome = WorkflowNodeAttemptOutcome(
            project_id=project_id,
            workflow_node_attempt_id=attempt_id,
            status="success",
            output_json=canonical_json(output),
            output_fingerprint=canonical_hash(output),
            recorded_at=now,
        )
        session.add(outcome)
        session.commit()
    with Session(engine) as session:
        loaded = WorkflowFactRepository(session).load(project_id)
        guard = loaded.sprint_plan_generation_guards[0]
        assert guard.integrity == "linked"
        assert guard.generated_plan_artifact_id == plan.artifact_id
        outcome = session.exec(
            select(WorkflowNodeAttemptOutcome).where(
                WorkflowNodeAttemptOutcome.workflow_node_attempt_id == attempt_id
            )
        ).one()
        outcome.output_fingerprint = "tampered"
        session.add(outcome)
        session.commit()
    with Session(engine) as session:
        loaded = WorkflowFactRepository(session).load(project_id)
        guard = loaded.sprint_plan_generation_guards[0]
        assert guard.integrity == "malformed"
        assert {
            item.code
            for item in build_sprint_retry_preview(
                session, snapshot=loaded, sprint_id=sprint_id
            ).blockers
        } >= {"SPRINT_PLAN_GENERATION_UNRESOLVED"}


def test_pending_receipts_are_typed_guard_facts_and_own_marker_is_exact(
    tmp_path: Path,
) -> None:
    """Only a canonical own pending receipt is omitted from retry guard facts."""
    engine = _file_engine(tmp_path / "retry-pending-receipt.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    now = datetime(2026, 9, 9, tzinfo=UTC)
    request = RetrySprint(
        project_id=project_id,
        graph_version="graph",
        fact_fingerprint="facts",
        decision_fingerprint="decision",
        idempotency_key="pending-retry",
        actor="owner@example.com",
        instance_key=f"sprint:{sprint_id}",
        sprint_id=sprint_id,
        confirm=True,
        rationale="Fresh evidence.",
        expected_state_fingerprint="expected",
    )
    request_json = canonical_json(request.model_dump(mode="json"))
    request_fingerprint = canonical_hash(request.model_dump(mode="json"))
    with Session(engine) as session:
        receipt = WorkflowTransitionReceipt(
            request_kind=request.kind,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_fingerprint,
            request_json=request_json,
            started_at=now,
        )
        session.add(receipt)
        session.commit()
        assert receipt.workflow_transition_receipt_id is not None
    with Session(engine) as session:
        loaded = WorkflowFactRepository(session).load(project_id)
        assert loaded.incomplete_transitions[0].integrity == "linked"
        assert {
            item.code
            for item in build_sprint_retry_preview(
                session, snapshot=loaded, sprint_id=sprint_id
            ).blockers
        } >= {"INCOMPLETE_TRANSITION"}
        assert fact_fingerprint(loaded) == fact_fingerprint(
            loaded.model_copy(update={"incomplete_transitions": ()})
        )
        assert business_fact_fingerprint(loaded) == business_fact_fingerprint(
            loaded.model_copy(update={"incomplete_transitions": ()})
        )
        session.info["agileforge.active_transition_receipt"] = {
            "receipt_id": receipt.workflow_transition_receipt_id,
            "request_kind": receipt.request_kind,
            "request_fingerprint": receipt.request_fingerprint,
        }
        assert (
            WorkflowFactRepository(session).load(project_id).incomplete_transitions
            == ()
        )
        session.info.pop("agileforge.active_transition_receipt")
        receipt.request_json = "{}"
        session.add(receipt)
        session.commit()
    with Session(engine) as session:
        loaded = WorkflowFactRepository(session).load(project_id)
        assert loaded.incomplete_transitions[0].integrity == "unassignable"
        assert {
            item.code
            for item in build_sprint_retry_preview(
                session, snapshot=loaded, sprint_id=sprint_id
            ).blockers
        } >= {"INCOMPLETE_TRANSITION"}


def test_retry_preview_hash_changes_for_valid_triage_correction(tmp_path: Path) -> None:
    """A valid terminal-evidence correction requires a fresh retry preview."""
    engine = _file_engine(tmp_path / "retry-triage-preview.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        initial = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.post_sprint_triage"
    )
    corrected = domain.transition(
        RecordPostSprintTriage(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key="correct-triage-for-retry-preview",
            actor="owner@example.com",
            instance_key=decision.instance_key or f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "Correction with the same impact."},
        )
    )
    assert corrected.ok is True
    with Session(engine) as session:
        after = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    assert initial.blockers == after.blockers == ()
    assert initial.expected_state_fingerprint != after.expected_state_fingerprint


@pytest.mark.parametrize(
    ("kind", "later", "current_identity", "expected_code"),
    [
        ("malformed", False, False, None),
        ("unlinked", False, False, None),
        ("failure", False, True, "UNRESOLVED_SPRINT_PLAN_GENERATION_FAILURE"),
        ("obsolete", False, True, "UNRESOLVED_SPRINT_PLAN_GENERATION_FAILURE"),
        ("linked", True, False, "LATER_SPRINT_PLAN_GENERATION"),
        ("unlinked", True, False, "SPRINT_PLAN_GENERATION_UNRESOLVED"),
        ("failure", True, False, "UNRESOLVED_SPRINT_PLAN_GENERATION_FAILURE"),
        ("in_flight", True, False, "IN_FLIGHT_SPRINT_PLAN_GENERATION"),
    ],
)
def test_retry_generation_guards_only_block_relevant_unresolved_work(
    tmp_path: Path,
    kind: str,
    later: bool,
    current_identity: bool,
    expected_code: str | None,
) -> None:
    """Only current or competing guard provenance can block the exact retry."""
    engine = _file_engine(tmp_path / f"retry-generation-{kind}-{later}.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        source = next(item for item in snapshot.sprints if item.sprint_id == sprint_id)
        assert source.completed_at is not None
        started_at = (
            source.completed_at if later else source.completed_at.replace(year=2025)
        )
        attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id="planning.sprint.plan",
            instance_key=None,
            graph_version="test",
            fact_fingerprint="facts",
            business_fact_fingerprint=(
                business_fact_fingerprint(snapshot)
                if current_identity
                else "sha256:historical-business"
            ),
            decision_fingerprint="decision",
            normalized_input_json="{}",
            input_fingerprint="input",
            model_id="test",
            execution_settings_json="{}",
            idempotency_key=f"relevance-{kind}-{later}",
            actor="owner@example.com",
            started_at=started_at,
            lease_expires_at=started_at.replace(year=started_at.year + 1),
            attempt_fingerprint=f"attempt-{kind}-{later}",
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        if kind != "in_flight":
            outcome_kwargs: dict[str, object] = {
                "project_id": project_id,
                "workflow_node_attempt_id": attempt.workflow_node_attempt_id,
                "status": kind if kind in {"failure", "obsolete"} else "success",
                "recorded_at": started_at,
            }
            if kind == "failure":
                outcome_kwargs.update(
                    failure_code="failed", failure_message="Synthetic failure."
                )
            elif kind == "linked":
                plan = next(
                    item
                    for item in snapshot.planning_artifacts
                    if item.artifact_type == "sprint_plan"
                )
                output = {
                    "sprint_plan_artifact_id": plan.artifact_id,
                    "plan_fingerprint": plan.artifact_fingerprint,
                }
                outcome_kwargs.update(
                    output_json=canonical_json(output),
                    output_fingerprint=canonical_hash(output),
                )
            elif kind == "unlinked":
                outcome_kwargs.update(
                    output_json=canonical_json({}),
                    output_fingerprint=canonical_hash({}),
                )
            elif kind == "malformed":
                output = {"sprint_plan_artifact_id": 999_999, "plan_fingerprint": "x"}
                outcome_kwargs.update(
                    output_json=canonical_json(output),
                    output_fingerprint=canonical_hash(output),
                )
            session.add(WorkflowNodeAttemptOutcome(**outcome_kwargs))
        session.commit()
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    codes = {item.code for item in preview.blockers}
    if expected_code is None:
        assert codes == set()
    else:
        assert expected_code in codes


def test_current_provider_generation_requires_canonical_recovery(
    tmp_path: Path,
) -> None:
    """Current Story generation failures block retry until exact canonical recovery."""
    engine = _file_engine(tmp_path / "retry-provider-generation.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        source = next(item for item in snapshot.sprints if item.sprint_id == sprint_id)
        assert source.completed_at is not None
        business = business_fact_fingerprint(snapshot)
        started_at = source.completed_at
        common = {
            "project_id": project_id,
            "node_id": "planning.story.generate",
            "instance_key": "backlog_item:PBI-000001",
            "graph_version": "test",
            "fact_fingerprint": "facts",
            "business_fact_fingerprint": business,
            "decision_fingerprint": "decision",
            "normalized_input_json": "{}",
            "input_fingerprint": "sha256:provider-input",
            "model_id": "test",
            "execution_settings_json": "{}",
            "actor": "owner@example.com",
            "lease_expires_at": started_at + timedelta(days=1),
        }
        failed = WorkflowNodeAttempt(
            **common,
            idempotency_key="failed-provider-generation",
            started_at=started_at,
            attempt_fingerprint="failed-provider-generation",
        )
        session.add(failed)
        session.flush()
        assert failed.workflow_node_attempt_id is not None
        failed_at = started_at + timedelta(seconds=1)
        session.add(
            WorkflowNodeAttemptOutcome(
                project_id=project_id,
                workflow_node_attempt_id=failed.workflow_node_attempt_id,
                status="failure",
                failure_code="failed",
                failure_message="Synthetic failure.",
                recorded_at=failed_at,
            )
        )
        session.commit()
    with Session(engine) as session:
        blocked = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    assert {item.code for item in blocked.blockers} >= {
        "UNRESOLVED_PROVIDER_GENERATION"
    }
    with Session(engine) as session:
        recovered_at = failed_at + timedelta(seconds=1)
        recovery = WorkflowNodeAttempt(
            **(common | {"lease_expires_at": recovered_at + timedelta(days=1)}),
            idempotency_key="recovered-provider-generation",
            started_at=recovered_at,
            attempt_fingerprint="recovered-provider-generation",
        )
        session.add(recovery)
        session.flush()
        assert recovery.workflow_node_attempt_id is not None
        output = {"schema_version": "test", "recovered": True}
        session.add(
            WorkflowNodeAttemptOutcome(
                project_id=project_id,
                workflow_node_attempt_id=recovery.workflow_node_attempt_id,
                status="success",
                output_json=canonical_json(output),
                output_fingerprint=canonical_hash(output),
                recorded_at=recovered_at,
            )
        )
        session.commit()
    with Session(engine) as session:
        recovered = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    assert recovered.blockers == ()


def test_retry_forged_late_success_cannot_recover_the_completed_source_plan(
    tmp_path: Path,
) -> None:
    """A late output naming the completed source remains competing provenance."""
    engine = _file_engine(tmp_path / "retry-generation-recovery.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        source = next(item for item in snapshot.sprints if item.sprint_id == sprint_id)
        assert source.completed_at is not None
        business_identity = business_fact_fingerprint(snapshot)
        started_at = source.completed_at
        common = {
            "project_id": project_id,
            "node_id": "planning.sprint.plan",
            "instance_key": None,
            "graph_version": "test",
            "fact_fingerprint": "facts",
            "business_fact_fingerprint": business_identity,
            "decision_fingerprint": "decision",
            "normalized_input_json": "{}",
            "input_fingerprint": "sha256:matching-input",
            "model_id": "test",
            "execution_settings_json": "{}",
            "actor": "owner@example.com",
            "lease_expires_at": started_at + timedelta(days=1),
        }
        obsolete = WorkflowNodeAttempt(
            **common,
            idempotency_key="obsolete-generation",
            started_at=started_at,
            attempt_fingerprint="attempt-obsolete-generation",
        )
        session.add(obsolete)
        session.flush()
        assert obsolete.workflow_node_attempt_id is not None
        session.add(
            WorkflowNodeAttemptOutcome(
                project_id=project_id,
                workflow_node_attempt_id=obsolete.workflow_node_attempt_id,
                status="obsolete",
                recorded_at=started_at,
            )
        )
        recovered_at = started_at + timedelta(seconds=1)
        recovery = WorkflowNodeAttempt(
            **(common | {"lease_expires_at": recovered_at + timedelta(days=1)}),
            idempotency_key="recovered-generation",
            started_at=recovered_at,
            attempt_fingerprint="attempt-recovered-generation",
        )
        session.add(recovery)
        session.flush()
        assert recovery.workflow_node_attempt_id is not None
        plan = next(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
        )
        output = {
            "sprint_plan_artifact_id": plan.artifact_id,
            "plan_fingerprint": plan.artifact_fingerprint,
        }
        session.add(
            WorkflowNodeAttemptOutcome(
                project_id=project_id,
                workflow_node_attempt_id=recovery.workflow_node_attempt_id,
                status="success",
                output_json=canonical_json(output),
                output_fingerprint=canonical_hash(output),
                recorded_at=recovered_at,
            )
        )
        session.commit()
    with Session(engine) as session:
        preview = build_sprint_retry_preview(
            session,
            snapshot=WorkflowFactRepository(session).load(project_id),
            sprint_id=sprint_id,
        )
    assert {item.code for item in preview.blockers} >= {
        "UNRESOLVED_SPRINT_PLAN_GENERATION_FAILURE",
        "LATER_SPRINT_PLAN_GENERATION",
    }


def test_retry_progress_write_failure_rolls_back_the_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after attempt insertion leaves no partial retry state behind."""
    engine = _file_engine(tmp_path / "retry-progress-rollback.sqlite")
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        preview = build_sprint_retry_preview(
            session, snapshot=snapshot, sprint_id=sprint_id
        )
        request = RetrySprint(
            project_id=project_id,
            graph_version="graph",
            fact_fingerprint="facts",
            decision_fingerprint="decision",
            idempotency_key="rollback-after-progress",
            actor="owner@example.com",
            instance_key=f"sprint:{sprint_id}",
            sprint_id=sprint_id,
            confirm=True,
            rationale="Fresh evidence.",
            expected_state_fingerprint=preview.expected_state_fingerprint,
        )
        original_flush = session.flush
        flushes = 0
        progress_flush = 2
        error_message = "Injected progress write failure."

        def fail_after_progress() -> None:
            nonlocal flushes
            flushes += 1
            if flushes == progress_flush:
                raise RuntimeError(error_message)
            original_flush()

        monkeypatch.setattr(session, "flush", fail_after_progress)
        with pytest.raises(RuntimeError, match="Injected progress"):
            retry_sprint_in_session(
                session,
                request=request,
                snapshot=snapshot,
                now=datetime(2026, 9, 9, tzinfo=UTC),
            )
        session.rollback()
    with Session(engine) as session:
        assert (
            session.exec(
                select(SprintRetryAttempt).where(
                    SprintRetryAttempt.project_id == project_id
                )
            ).all()
            == []
        )
