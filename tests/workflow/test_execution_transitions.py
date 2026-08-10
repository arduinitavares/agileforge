"""Persisted execution transition contracts and guard tests."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, get_args

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import event
from sqlmodel import Session, SQLModel, col, create_engine, select

import services.agent_workbench.post_sprint_triage as triage_service
import services.agent_workbench.sprint_phase as sprint_service
import services.story_close_service as story_service
import services.task_execution_service as task_service
from models.core import (
    Project,
    Sprint,
    SprintStory,
    Task,
    Team,
    UserStory,
    UserStoryDependency,
)
from models.enums import SprintStatus, StoryStatus, TaskStatus
from models.events import StoryCompletionLog, TaskExecutionLog
from models.workflow import (
    PostSprintTriage,
    SprintClosure,
    SprintReview,
    SprintStart,
    StoryClosure,
    TaskCompletionEvidence,
)
from repositories.workflow import WorkflowFactLoadError
from tests.workflow.execution_fixtures import (
    seed_started_execution,
    seed_started_execution_with_unselected_story,
)
from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.clock import FixedClock
from workflow.contracts import JsonObject, NodeDecision, WorkflowErrorCode
from workflow.definitions.execution import execution_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    CloseSprint,
    CloseStory,
    CompleteTask,
    RecordPostSprintTriage,
    ReviewSprint,
    TransitionRequest,
)
from workflow.requests.base import PositionedRequest

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
EXPECTED_REQUEST_VARIANT_COUNT = 35
EXECUTION_REQUESTS = (
    CompleteTask,
    CloseStory,
    ReviewSprint,
    CloseSprint,
    RecordPostSprintTriage,
)


class _PositionGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    actor: str


class _RequestBase(_PositionGuards):
    idempotency_key: str


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=execution_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _file_engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_active_task(engine: Engine) -> tuple[int, int, int, int]:
    return seed_started_execution(engine)


def _seed_unlineaged_active_task(engine: Engine) -> tuple[int, int, int, int]:
    with Session(engine) as session:
        project = Project(name="Task 12")
        team = Team(name="Task 12 Team")
        session.add(project)
        session.add(team)
        session.flush()
        assert project.project_id is not None
        assert team.team_id is not None
        sprint = Sprint(
            project_id=project.project_id,
            team_id=team.team_id,
            status=SprintStatus.ACTIVE,
            started_at=EVALUATED_AT,
        )
        story = UserStory(
            project_id=project.project_id,
            title="Execute graph work",
            status=StoryStatus.TO_DO,
            is_refined=True,
            story_points=3,
            rank="1",
        )
        session.add(sprint)
        session.add(story)
        session.flush()
        assert sprint.sprint_id is not None
        assert story.story_id is not None
        session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))
        task = Task(
            story_id=story.story_id,
            description="Implement execution graph",
            metadata_json=serialize_task_metadata(
                TaskMetadata(
                    task_kind="implementation",
                    artifact_targets=["workflow/definitions/execution.py"],
                    checklist_items=["Focused tests pass"],
                )
            ),
            status=TaskStatus.IN_PROGRESS,
        )
        session.add(task)
        session.commit()
        assert task.task_id is not None
        return project.project_id, sprint.sprint_id, story.story_id, task.task_id


def _decision(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    instance_key: str | None = None,
) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )


def _guards(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    instance_key: str | None = None,
) -> _PositionGuards:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )
    return {
        "project_id": project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "actor": "operator@example.com",
    }


def _complete_task(
    domain: WorkflowDomain,
    project_id: int,
    task_id: int,
    *,
    idempotency_key: str = "complete-task",
) -> CompleteTask:
    return CompleteTask(
        **_guards(domain, project_id, "execution.task.complete", f"task:{task_id}"),
        instance_key=f"task:{task_id}",
        idempotency_key=idempotency_key,
        task_id=task_id,
        outcome_summary="Implemented execution graph.",
        artifact_refs=("workflow/definitions/execution.py",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
    )


def _complete_execution_sprint(
    engine: Engine,
) -> tuple[WorkflowDomain, int, int, int, int, str]:
    project_id, sprint_id, story_id, task_id = _seed_active_task(engine)
    domain = _domain(engine)
    review_fingerprint = _close_execution_sprint(
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    return domain, project_id, sprint_id, story_id, task_id, review_fingerprint


def _close_execution_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
    task_id: int,
) -> str:
    """Complete one normalized single-Story Sprint through explicit close."""
    assert domain.transition(_complete_task(domain, project_id, task_id)).ok is True
    assert (
        domain.transition(
            CloseStory(
                **_guards(
                    domain, project_id, "execution.story.close", f"story:{story_id}"
                ),
                instance_key=f"story:{story_id}",
                idempotency_key="close-story",
                story_id=story_id,
                resolution="Completed",
                delivered="Execution graph delivered.",
                evidence="Focused tests pass.",
                known_gaps="None.",
            )
        ).ok
        is True
    )
    review_decision = _decision(
        domain,
        project_id,
        "execution.sprint.review",
        f"sprint:{sprint_id}",
    )
    review_fingerprint = next(
        ref.fingerprint
        for ref in review_decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    assert (
        domain.transition(
            ReviewSprint(
                **_guards(
                    domain,
                    project_id,
                    "execution.sprint.review",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key="review-sprint",
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        ).ok
        is True
    )
    assert (
        domain.transition(
            CloseSprint(
                **_guards(
                    domain,
                    project_id,
                    "execution.sprint.close",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key="close-sprint",
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        ).ok
        is True
    )
    return review_fingerprint


def _complete_execution_sprint_with_unselected_story(
    engine: Engine,
) -> tuple[WorkflowDomain, int, int, int, int, int, int, str]:
    (
        project_id,
        sprint_id,
        story_id,
        future_story_id,
        task_id,
        dependency_id,
    ) = seed_started_execution_with_unselected_story(engine)
    domain = _domain(engine)
    review_fingerprint = _close_execution_sprint(
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    return (
        domain,
        project_id,
        sprint_id,
        story_id,
        future_story_id,
        task_id,
        dependency_id,
        review_fingerprint,
    )


def test_execution_request_union_is_closed_and_fixed() -> None:
    """Keep the discriminated request union closed over execution variants."""
    variants = get_args(TransitionRequest.__value__)
    assert len(variants) == EXPECTED_REQUEST_VARIANT_COUNT
    assert set(EXECUTION_REQUESTS) <= set(variants)
    adapter = TypeAdapter(TransitionRequest)
    payload = {
        "kind": "complete_task",
        "project_id": 1,
        "graph_version": "graph",
        "fact_fingerprint": "facts",
        "decision_fingerprint": "decision",
        "idempotency_key": "key",
        "actor": "operator",
        "instance_key": "task:7",
        "task_id": 7,
        "outcome_summary": "Done.",
        "artifact_refs": [],
        "acceptance_result": "fully_met",
        "checklist_result": {"check": "passed"},
    }
    assert isinstance(adapter.validate_python(payload), CompleteTask)
    with pytest.raises(ValidationError):
        adapter.validate_python({**payload, "kind": "unknown_execution"})


@pytest.mark.parametrize("request_type", EXECUTION_REQUESTS)
def test_execution_requests_use_positioned_guards_without_expected_state(
    request_type: type[PositionedRequest],
) -> None:
    """Use position fingerprints rather than scalar expected-state guards."""
    assert issubclass(request_type, PositionedRequest)
    assert "expected_state" not in request_type.model_fields


def test_task_story_and_triage_instance_guards_are_exact() -> None:
    """Reject Task, Story, and Sprint requests with mismatched instance keys."""
    common: _RequestBase = {
        "project_id": 1,
        "graph_version": "graph",
        "fact_fingerprint": "facts",
        "decision_fingerprint": "decision",
        "idempotency_key": "key",
        "actor": "operator",
    }
    task = CompleteTask(
        **common,
        instance_key="task:7",
        task_id=7,
        outcome_summary="Done.",
        artifact_refs=(),
        acceptance_result="fully_met",
        checklist_result={"check": "passed"},
    )
    story = CloseStory(
        **common,
        instance_key="story:9",
        story_id=9,
        resolution="Completed",
        delivered="Delivered.",
        evidence="Tests pass.",
        known_gaps="None.",
    )
    triage = RecordPostSprintTriage(
        **common,
        instance_key="sprint:11",
        sprint_id=11,
        impact="none",
        canonical_payload={"summary": "No impact."},
    )
    assert task.decision_instance_key() == "task:7"
    assert story.decision_instance_key() == "story:9"
    assert triage.decision_instance_key() == "sprint:11"
    with pytest.raises(ValidationError):
        task.model_copy(update={"instance_key": "task:8"}).model_validate(
            {**task.model_dump(), "instance_key": "task:8"}
        )
    with pytest.raises(ValidationError):
        RecordPostSprintTriage.model_validate(
            {**triage.model_dump(), "instance_key": "sprint:12"}
        )


def test_execution_service_mutations_use_only_caller_owned_session() -> None:
    """Keep execution mutations inside the handler-owned transaction."""
    functions = (
        task_service.complete_task_in_session,
        story_service.close_story_in_session,
        sprint_service.start_sprint_in_session,
        sprint_service.review_sprint_in_session,
        sprint_service.close_sprint_in_session,
        triage_service.record_post_sprint_triage_in_session,
    )
    for function in functions:
        assert "session" in inspect.signature(function).parameters
        source = inspect.getsource(function)
        tree = ast.parse(source)
        assert "get_engine" not in source
        assert ("fsm" + "_state") not in source
        assert "active_sprint_id" not in source
        assert "latest_completed_sprint_id" not in source
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                assert "Session(" not in ast.unparse(node)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {"commit", "rollback", "close"}
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in {"Session", "get_engine"}


@pytest.mark.parametrize("field", ["candidate_set_fingerprint", "plan_fingerprint"])
def test_active_sprint_start_lineage_tamper_is_loader_invalid(
    engine: Engine,
    field: str,
) -> None:
    """Reject stale accepted-plan values in durable StartSprint lineage."""
    project_id, _sprint_id, _story_id, _task_id = _seed_active_task(engine)
    with Session(engine) as session:
        start = session.exec(select(SprintStart)).one()
        setattr(start, field, "sha256:stale-lineage")
        session.add(start)
        session.commit()

    with pytest.raises(WorkflowFactLoadError):
        _domain(engine).position(project_id)


def test_cross_project_sprint_start_lineage_is_loader_invalid(engine: Engine) -> None:
    """Reject a StartSprint row whose Project differs from its Sprint."""
    project_id, _sprint_id, _story_id, _task_id = _seed_active_task(engine)
    with Session(engine) as session:
        other = Project(name="Task 12 lineage owner")
        session.add(other)
        session.commit()
        assert other.project_id is not None
        other_project_id = other.project_id
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "UPDATE sprint_starts SET project_id = ?",
            (other_project_id,),
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    with pytest.raises(WorkflowFactLoadError):
        _domain(engine).position(project_id)


def test_manually_activated_sprint_is_invalid_and_exposes_no_task(
    engine: Engine,
) -> None:
    """Reject an active Sprint without accepted plan and StartSprint lineage."""
    project_id, _sprint_id, _story_id, _task_id = _seed_unlineaged_active_task(engine)

    position = _domain(engine).position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "execution.task.complete"
    )

    assert decision.category.value == "invalid"
    assert decision.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert not any(
        item.node_id == "execution.task.complete" and item.category.value == "available"
        for item in position.decisions
    )


def test_complete_task_persists_status_audit_and_immutable_evidence(
    engine: Engine,
) -> None:
    """Persist Task status, audit, and immutable completion evidence."""
    project_id, sprint_id, _story_id, task_id = _seed_active_task(engine)
    domain = _domain(engine)
    result = domain.transition(_complete_task(domain, project_id, task_id))
    assert result.ok is True
    with Session(engine) as session:
        task = session.get(Task, task_id)
        evidence = session.exec(
            select(TaskCompletionEvidence).where(
                col(TaskCompletionEvidence.task_id) == task_id
            )
        ).one()
        audit = session.exec(
            select(TaskExecutionLog).where(col(TaskExecutionLog.task_id) == task_id)
        ).one()
        assert task is not None
        assert task.status is TaskStatus.DONE
        assert evidence.sprint_id == sprint_id
        assert audit.new_status is TaskStatus.DONE


def test_complete_task_replay_is_exact_and_second_key_cannot_mutate_evidence(
    engine: Engine,
) -> None:
    """Replay the exact completion and prevent later evidence mutation."""
    project_id, _sprint_id, _story_id, task_id = _seed_active_task(engine)
    domain = _domain(engine)
    request = _complete_task(domain, project_id, task_id)
    first = domain.transition(request)
    replay = domain.transition(request)
    assert first.ok is True
    assert replay.ok is True
    assert replay.replayed is True
    assert replay.output == first.output
    position = domain.position(project_id)
    assert not any(
        item.node_id == "execution.task.complete"
        and item.instance_key == f"task:{task_id}"
        and item.category.value == "available"
        for item in position.decisions
    )


def test_stale_task_decision_and_cross_project_links_fail_closed(
    engine: Engine,
) -> None:
    """Fail stale guards and cross-project Sprint links closed."""
    project_id, sprint_id, _story_id, task_id = _seed_active_task(engine)
    with Session(engine) as session:
        other = Project(name="Task 12 other")
        session.add(other)
        session.commit()
        assert other.project_id is not None
        other_project = other.project_id
    domain = _domain(engine)
    request = _complete_task(domain, project_id, task_id)
    with Session(engine) as session:
        sprint = session.get(Sprint, sprint_id)
        assert sprint is not None
        sprint.project_id = other_project
        session.add(sprint)
        session.commit()
    stale = domain.transition(request)
    assert stale.ok is False
    assert stale.error is not None
    assert stale.error.code in {
        WorkflowErrorCode.STALE_POSITION,
        WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
    }


def test_story_review_close_and_triage_persist_distinct_facts(engine: Engine) -> None:
    """Persist Story, review, close, and triage as distinct facts."""
    project_id, sprint_id, story_id, task_id = _seed_active_task(engine)
    domain = _domain(engine)
    assert domain.transition(_complete_task(domain, project_id, task_id)).ok is True
    close_story = CloseStory(
        **_guards(domain, project_id, "execution.story.close", f"story:{story_id}"),
        instance_key=f"story:{story_id}",
        idempotency_key="close-story",
        story_id=story_id,
        resolution="Completed",
        delivered="Execution graph delivered.",
        evidence="Focused tests pass.",
        known_gaps="None.",
    )
    assert domain.transition(close_story).ok is True
    review_decision = _decision(
        domain,
        project_id,
        "execution.sprint.review",
        f"sprint:{sprint_id}",
    )
    review_fingerprint = next(
        ref.fingerprint
        for ref in review_decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    assert (
        domain.transition(
            ReviewSprint(
                **_guards(
                    domain,
                    project_id,
                    "execution.sprint.review",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key="review-sprint",
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        ).ok
        is True
    )
    assert (
        domain.transition(
            CloseSprint(
                **_guards(
                    domain,
                    project_id,
                    "execution.sprint.close",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key="close-sprint",
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        ).ok
        is True
    )
    payload: JsonObject = {"summary": "No downstream change."}
    assert (
        domain.transition(
            RecordPostSprintTriage(
                **_guards(
                    domain,
                    project_id,
                    "execution.post_sprint_triage",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key="triage-sprint",
                sprint_id=sprint_id,
                impact="none",
                canonical_payload=payload,
            )
        ).ok
        is True
    )
    with Session(engine) as session:
        assert session.exec(select(StoryClosure)).one().story_id == story_id
        assert session.exec(select(StoryCompletionLog)).one().story_id == story_id
        assert session.exec(select(SprintReview)).one().sprint_id == sprint_id
        assert session.exec(select(SprintClosure)).one().sprint_id == sprint_id
        assert session.exec(select(PostSprintTriage)).one().sprint_id == sprint_id


def test_stale_review_fingerprint_and_duplicate_triage_fail_closed(
    engine: Engine,
) -> None:
    """Reject stale review fingerprints and duplicate triage facts."""
    project_id, sprint_id, story_id, task_id = _seed_active_task(engine)
    domain = _domain(engine)
    assert domain.transition(_complete_task(domain, project_id, task_id)).ok is True
    assert (
        domain.transition(
            CloseStory(
                **_guards(
                    domain, project_id, "execution.story.close", f"story:{story_id}"
                ),
                instance_key=f"story:{story_id}",
                idempotency_key="close-story",
                story_id=story_id,
                resolution="Completed",
                delivered="Delivered.",
                evidence="Verified.",
                known_gaps="None.",
            )
        ).ok
        is True
    )
    decision = _decision(
        domain,
        project_id,
        "execution.sprint.review",
        f"sprint:{sprint_id}",
    )
    fingerprint = next(
        ref.fingerprint
        for ref in decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    stale_review = domain.transition(
        ReviewSprint(
            **_guards(
                domain,
                project_id,
                "execution.sprint.review",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="stale-review",
            sprint_id=sprint_id,
            review_fingerprint="sha256:stale",
        )
    )
    assert stale_review.ok is False
    assert stale_review.error is not None
    assert stale_review.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert (
        domain.transition(
            ReviewSprint(
                **_guards(
                    domain,
                    project_id,
                    "execution.sprint.review",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key="review",
                sprint_id=sprint_id,
                review_fingerprint=fingerprint,
            )
        ).ok
        is True
    )


def test_triage_correction_is_append_only_and_fingerprint_guarded(
    engine: Engine,
) -> None:
    """Append a changed triage correction while preserving prior facts."""
    project_id, sprint_id, story_id, task_id = _seed_active_task(engine)
    domain = _domain(engine)
    assert domain.transition(_complete_task(domain, project_id, task_id)).ok
    assert domain.transition(
        CloseStory(
            **_guards(domain, project_id, "execution.story.close", f"story:{story_id}"),
            instance_key=f"story:{story_id}",
            idempotency_key="story",
            story_id=story_id,
            resolution="Completed",
            delivered="Delivered.",
            evidence="Verified.",
            known_gaps="None.",
        )
    ).ok
    review_decision = _decision(
        domain,
        project_id,
        "execution.sprint.review",
        f"sprint:{sprint_id}",
    )
    review_fingerprint = next(
        ref.fingerprint
        for ref in review_decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    assert domain.transition(
        ReviewSprint(
            **_guards(
                domain,
                project_id,
                "execution.sprint.review",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="review",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok
    assert domain.transition(
        CloseSprint(
            **_guards(
                domain,
                project_id,
                "execution.sprint.close",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="close",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok
    first = RecordPostSprintTriage(
        **_guards(
            domain,
            project_id,
            "execution.post_sprint_triage",
            f"sprint:{sprint_id}",
        ),
        instance_key=f"sprint:{sprint_id}",
        idempotency_key="triage-1",
        sprint_id=sprint_id,
        impact="backlog",
        canonical_payload={"requirements": ["REQ-1"]},
    )
    assert domain.transition(first).ok
    duplicate = domain.transition(
        RecordPostSprintTriage(
            **_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="triage-duplicate",
            sprint_id=sprint_id,
            impact="backlog",
            canonical_payload={"requirements": ["REQ-1"]},
        )
    )
    assert duplicate.ok is False
    correction = domain.transition(
        RecordPostSprintTriage(
            **_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key="triage-2",
            sprint_id=sprint_id,
            impact="specification",
            canonical_payload={"requirements": ["REQ-1"], "reason": "Spec gap"},
        )
    )
    assert correction.ok is True
    with Session(engine) as session:
        rows = session.exec(
            select(PostSprintTriage).order_by(col(PostSprintTriage.triage_id))
        ).all()
        expected_row_count = 2
        assert len(rows) == expected_row_count
        assert rows[1].supersedes_triage_id == rows[0].triage_id
        assert canonical_hash(rows[0].canonical_payload_json) != canonical_hash(
            rows[1].canonical_payload_json
        )


def test_story_closure_evidence_tamper_is_loader_invalid(engine: Engine) -> None:
    """Bind the immutable Story closure hash to delivery evidence and gaps."""
    project_id, _sprint_id, story_id, task_id = _seed_active_task(engine)
    domain = _domain(engine)
    assert domain.transition(_complete_task(domain, project_id, task_id)).ok is True
    assert (
        domain.transition(
            CloseStory(
                **_guards(
                    domain, project_id, "execution.story.close", f"story:{story_id}"
                ),
                instance_key=f"story:{story_id}",
                idempotency_key="close-story",
                story_id=story_id,
                resolution="Completed",
                delivered="Original delivery evidence.",
                evidence="Original test evidence.",
                known_gaps="None.",
            )
        ).ok
        is True
    )
    with Session(engine) as session:
        closure = session.exec(select(StoryClosure)).one()
        closure.delivered = "Tampered after close."
        session.add(closure)
        session.commit()

    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)


@pytest.mark.parametrize("fingerprint", ["review", "close"])
def test_post_close_stale_terminal_fingerprint_is_invalid(
    engine: Engine,
    fingerprint: str,
) -> None:
    """Recompute persisted Sprint review and close hashes before triage."""
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    with Session(engine) as session:
        review = session.exec(select(SprintReview)).one()
        closure = session.exec(select(SprintClosure)).one()
        if fingerprint == "review":
            review.review_fingerprint = "sha256:stale-terminal"
            closure.review_fingerprint = "sha256:stale-terminal"
            session.add(review)
        else:
            closure.close_fingerprint = "sha256:stale-terminal"
        session.add(closure)
        session.commit()

    position = domain.position(project_id)
    triage = next(
        item
        for item in position.decisions
        if item.node_id == "execution.post_sprint_triage"
        and item.instance_key == f"sprint:{sprint_id}"
    )
    assert triage.category.value == "invalid"
    assert triage.reason_code == "WORKFLOW_FACT_CONFLICT"


def test_closed_sprint_ignores_unrelated_future_rejected_dependency(
    engine: Engine,
) -> None:
    """Keep Sprint A triage stable when future-only rejected rows are added."""
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    baseline = _decision(
        domain,
        project_id,
        "execution.post_sprint_triage",
        f"sprint:{sprint_id}",
    )
    with Session(engine) as session:
        future_stories = [
            UserStory(
                project_id=project_id,
                title=f"Future Story {index}",
                status=StoryStatus.TO_DO,
                is_refined=False,
                rank=f"9.{index}",
            )
            for index in (1, 2)
        ]
        session.add_all(future_stories)
        session.flush()
        future_ids = tuple(item.story_id for item in future_stories)
        assert all(item is not None for item in future_ids)
        first_id, second_id = future_ids
        assert first_id is not None
        assert second_id is not None
        session.add(
            UserStoryDependency(
                project_id=project_id,
                dependent_story_id=first_id,
                prerequisite_story_id=second_id,
                status="rejected",
                source="manual_review",
                confidence="reviewed",
                reason="Rejected future-only dependency.",
            )
        )
        session.commit()

    recovered = _decision(
        domain,
        project_id,
        "execution.post_sprint_triage",
        f"sprint:{sprint_id}",
    )
    assert recovered.category == baseline.category
    assert recovered.reason_code == baseline.reason_code
    assert recovered.fact_references == baseline.fact_references


def test_closed_sprint_ignores_unselected_story_moved_to_later_sprint(
    engine: Engine,
) -> None:
    """Keep Sprint A terminal hashes scoped to its attached Story set."""
    (
        domain,
        project_id,
        sprint_id,
        _story_id,
        future_story_id,
        _task_id,
        _dependency_id,
        _review,
    ) = _complete_execution_sprint_with_unselected_story(engine)
    baseline = _decision(
        domain,
        project_id,
        "execution.post_sprint_triage",
        f"sprint:{sprint_id}",
    )
    with Session(engine) as session:
        sprint_a = session.get(Sprint, sprint_id)
        future_story = session.get(UserStory, future_story_id)
        assert sprint_a is not None
        assert future_story is not None
        sprint_b = Sprint(
            project_id=project_id,
            team_id=sprint_a.team_id,
            status=SprintStatus.PLANNED,
        )
        session.add(sprint_b)
        session.flush()
        assert sprint_b.sprint_id is not None
        session.add(
            SprintStory(
                sprint_id=sprint_b.sprint_id,
                story_id=future_story_id,
            )
        )
        future_story.status = StoryStatus.DONE
        session.add(future_story)
        session.commit()

    recovered = _decision(
        domain,
        project_id,
        "execution.post_sprint_triage",
        f"sprint:{sprint_id}",
    )
    assert recovered.category == baseline.category
    assert recovered.reason_code == baseline.reason_code
    assert recovered.fact_references == baseline.fact_references


def test_closed_sprint_rejects_selected_scope_dependency_tamper(
    engine: Engine,
) -> None:
    """Bind Sprint A completion facts to relevant dependency row semantics."""
    (
        domain,
        project_id,
        _sprint_id,
        _story_id,
        _future_story_id,
        _task_id,
        dependency_id,
        _review,
    ) = _complete_execution_sprint_with_unselected_story(engine)
    with Session(engine) as session:
        dependency = session.get(UserStoryDependency, dependency_id)
        assert dependency is not None
        dependency.reason = "Tampered selected-scope dependency semantics."
        session.add(dependency)
        session.commit()

    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)


def test_closed_sprint_rejects_attached_task_content_tamper(
    engine: Engine,
) -> None:
    """Bind Sprint A review and closure to exact attached Task content."""
    domain, project_id, _sprint_id, _story_id, task_id, _review = (
        _complete_execution_sprint(engine)
    )
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.description = "Tampered attached Task content."
        session.add(task)
        session.commit()

    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)


def test_cross_project_historical_triage_is_loader_invalid(engine: Engine) -> None:
    """Do not hide a completed Sprint triage row under another Project."""
    domain, project_id, sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    assert (
        domain.transition(
            RecordPostSprintTriage(
                **_guards(
                    domain,
                    project_id,
                    "execution.post_sprint_triage",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key="triage-cross-project",
                sprint_id=sprint_id,
                impact="none",
                canonical_payload={"summary": "No downstream change."},
            )
        ).ok
        is True
    )
    with Session(engine) as session:
        other = Project(name="Task 12 triage owner")
        session.add(other)
        session.commit()
        assert other.project_id is not None
        other_project_id = other.project_id
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "UPDATE post_sprint_triage SET project_id = ?",
            (other_project_id,),
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)


def test_reversed_repository_triage_rows_preserve_position(
    engine: Engine,
) -> None:
    """Keep execution decisions stable under reversed normalized row order."""
    (
        domain,
        project_id,
        sprint_id,
        _story_id,
        _future_story_id,
        _task_id,
        _dependency_id,
        _review,
    ) = _complete_execution_sprint_with_unselected_story(engine)
    first = RecordPostSprintTriage(
        **_guards(
            domain,
            project_id,
            "execution.post_sprint_triage",
            f"sprint:{sprint_id}",
        ),
        instance_key=f"sprint:{sprint_id}",
        idempotency_key="triage-first",
        sprint_id=sprint_id,
        impact="backlog",
        canonical_payload={"requirements": ["REQ-1"]},
    )
    assert domain.transition(first).ok is True
    correction = RecordPostSprintTriage(
        **_guards(
            domain,
            project_id,
            "execution.post_sprint_triage",
            f"sprint:{sprint_id}",
        ),
        instance_key=f"sprint:{sprint_id}",
        idempotency_key="triage-correction",
        sprint_id=sprint_id,
        impact="specification",
        canonical_payload={"requirements": ["REQ-1"], "reason": "Spec gap"},
    )
    assert domain.transition(correction).ok is True
    baseline = domain.position(project_id)
    replacements = {
        "stories": (
            " ORDER BY user_stories.rank, user_stories.story_id",
            " ORDER BY user_stories.rank DESC, user_stories.story_id DESC",
        ),
        "dependencies": (
            " ORDER BY user_story_dependencies.dependent_story_id, "
            "user_story_dependencies.prerequisite_story_id, "
            "user_story_dependencies.dependency_id",
            " ORDER BY user_story_dependencies.dependent_story_id DESC, "
            "user_story_dependencies.prerequisite_story_id DESC, "
            "user_story_dependencies.dependency_id DESC",
        ),
        "dependency_reviews": (
            " ORDER BY story_dependency_reviews.story_dependency_review_id",
            " ORDER BY story_dependency_reviews.story_dependency_review_id DESC",
        ),
        "story_memberships": (
            " ORDER BY sprint_stories.story_id, sprint_stories.sprint_id",
            " ORDER BY sprint_stories.story_id DESC, sprint_stories.sprint_id DESC",
        ),
        "task_memberships": (
            " ORDER BY sprint_stories.sprint_id, sprint_stories.story_id",
            " ORDER BY sprint_stories.sprint_id DESC, sprint_stories.story_id DESC",
        ),
        "tasks": (
            " ORDER BY tasks.task_id",
            " ORDER BY tasks.task_id DESC",
        ),
        "task_completions": (
            " ORDER BY task_completion_evidence.task_completion_evidence_id",
            " ORDER BY task_completion_evidence.task_completion_evidence_id DESC",
        ),
        "story_closures": (
            " ORDER BY story_closures.story_closure_id",
            " ORDER BY story_closures.story_closure_id DESC",
        ),
        "sprint_reviews": (
            " ORDER BY sprint_reviews.sprint_review_id",
            " ORDER BY sprint_reviews.sprint_review_id DESC",
        ),
        "sprint_closures": (
            " ORDER BY sprint_closures.sprint_closure_id",
            " ORDER BY sprint_closures.sprint_closure_id DESC",
        ),
        "triage": (
            " ORDER BY post_sprint_triage.triage_id",
            " ORDER BY post_sprint_triage.triage_id DESC",
        ),
    }
    reversed_queries = dict.fromkeys(replacements, 0)

    def reverse_triage_order(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        for name, (ascending, descending) in replacements.items():
            if ascending in statement:
                reversed_queries[name] += 1
                statement = statement.replace(ascending, descending)
        return statement, parameters

    event.listen(engine, "before_cursor_execute", reverse_triage_order, retval=True)
    try:
        reversed_position = domain.position(project_id)
    finally:
        event.remove(engine, "before_cursor_execute", reverse_triage_order)

    assert all(count > 0 for count in reversed_queries.values())
    assert reversed_position.fact_fingerprint == baseline.fact_fingerprint
    assert reversed_position.decisions == baseline.decisions


def test_multi_sprint_restart_preserves_scoped_historical_position(
    tmp_path: Path,
) -> None:
    """Recover Sprint A integrity after Sprint B membership and a full restart."""
    first_engine = _file_engine(tmp_path / "execution-multi-sprint-restart.db")
    (
        first_domain,
        project_id,
        sprint_id,
        _story_id,
        future_story_id,
        _task_id,
        _dependency_id,
        _review,
    ) = _complete_execution_sprint_with_unselected_story(first_engine)
    with Session(first_engine) as session:
        sprint_a = session.get(Sprint, sprint_id)
        future_story = session.get(UserStory, future_story_id)
        assert sprint_a is not None
        assert future_story is not None
        sprint_b = Sprint(
            project_id=project_id,
            team_id=sprint_a.team_id,
            status=SprintStatus.PLANNED,
            created_at=EVALUATED_AT,
            updated_at=EVALUATED_AT,
        )
        session.add(sprint_b)
        session.flush()
        assert sprint_b.sprint_id is not None
        session.add(
            SprintStory(
                sprint_id=sprint_b.sprint_id,
                story_id=future_story_id,
                added_at=EVALUATED_AT,
            )
        )
        future_story.status = StoryStatus.DONE
        future_story.updated_at = EVALUATED_AT
        session.add(future_story)
        session.commit()
    baseline = first_domain.position(project_id)
    baseline_triage = next(
        item
        for item in baseline.decisions
        if item.node_id == "execution.post_sprint_triage"
        and item.instance_key == f"sprint:{sprint_id}"
    )
    assert baseline_triage.category.value == "available"
    first_engine.dispose()

    restarted_engine = _file_engine(tmp_path / "execution-multi-sprint-restart.db")
    restarted = _domain(restarted_engine).position(project_id)
    assert restarted.fact_fingerprint == baseline.fact_fingerprint
    assert restarted.decisions == baseline.decisions
    restarted_engine.dispose()
