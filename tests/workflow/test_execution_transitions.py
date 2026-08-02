"""Persisted execution transition contracts and guard tests."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, get_args

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

import services.agent_workbench.post_sprint_triage as triage_service
import services.agent_workbench.sprint_phase as sprint_service
import services.story_close_service as story_service
import services.task_execution_service as task_service
from models.core import Product, Sprint, SprintStory, Task, Team, UserStory
from models.enums import SprintStatus, StoryStatus, TaskStatus
from models.events import StoryCompletionLog, TaskExecutionLog
from models.workflow import (
    PostSprintTriage,
    SprintClosure,
    SprintReview,
    StoryClosure,
    TaskCompletionEvidence,
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


def _seed_active_task(engine: Engine) -> tuple[int, int, int, int]:
    with Session(engine) as session:
        project = Product(name="Task 12", origin="greenfield")
        team = Team(name="Task 12 Team")
        session.add(project)
        session.add(team)
        session.flush()
        assert project.product_id is not None
        assert team.team_id is not None
        sprint = Sprint(
            product_id=project.product_id,
            team_id=team.team_id,
            status=SprintStatus.ACTIVE,
            started_at=EVALUATED_AT,
        )
        story = UserStory(
            product_id=project.product_id,
            title="Execute graph work",
            status=StoryStatus.TO_DO,
            is_refined=True,
            story_points=3,
            rank="1.1",
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
        return project.product_id, sprint.sprint_id, story.story_id, task.task_id


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
        checklist_result={"Focused tests pass": "passed"},
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


def test_task_and_story_instance_guards_are_exact() -> None:
    """Reject Task and Story requests with mismatched instance keys."""
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
    assert task.decision_instance_key() == "task:7"
    assert story.decision_instance_key() == "story:9"
    with pytest.raises(ValidationError):
        task.model_copy(update={"instance_key": "task:8"}).model_validate(
            {**task.model_dump(), "instance_key": "task:8"}
        )


def test_execution_service_mutations_use_only_caller_owned_session() -> None:
    """Keep execution mutations inside the handler-owned transaction."""
    functions = (
        task_service.complete_task_in_session,
        story_service.close_story_in_session,
        sprint_service.review_sprint_in_session,
        sprint_service.close_sprint_in_session,
        triage_service.record_post_sprint_triage_in_session,
    )
    for function in functions:
        assert "session" in inspect.signature(function).parameters
        source = inspect.getsource(function)
        tree = ast.parse(source)
        assert "fsm_state" not in source
        assert "active_sprint_id" not in source
        assert "latest_completed_sprint_id" not in source
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                assert "Session(" not in ast.unparse(node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"commit", "rollback", "close"}


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
        other = Product(name="Task 12 other", origin="greenfield")
        session.add(other)
        session.commit()
        assert other.product_id is not None
        other_project = other.product_id
    domain = _domain(engine)
    request = _complete_task(domain, project_id, task_id)
    with Session(engine) as session:
        sprint = session.get(Sprint, sprint_id)
        assert sprint is not None
        sprint.product_id = other_project
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
    review_decision = _decision(domain, project_id, "execution.sprint.review")
    review_fingerprint = next(
        ref.fingerprint
        for ref in review_decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    assert domain.transition(
        ReviewSprint(
            **_guards(domain, project_id, "execution.sprint.review"),
            idempotency_key="review-sprint",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok is True
    assert domain.transition(
        CloseSprint(
            **_guards(domain, project_id, "execution.sprint.close"),
            idempotency_key="close-sprint",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok is True
    payload: JsonObject = {"summary": "No downstream change."}
    assert domain.transition(
        RecordPostSprintTriage(
            **_guards(domain, project_id, "execution.post_sprint_triage"),
            idempotency_key="triage-sprint",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload=payload,
        )
    ).ok is True
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
    assert domain.transition(
        CloseStory(
            **_guards(domain, project_id, "execution.story.close", f"story:{story_id}"),
            instance_key=f"story:{story_id}",
            idempotency_key="close-story",
            story_id=story_id,
            resolution="Completed",
            delivered="Delivered.",
            evidence="Verified.",
            known_gaps="None.",
        )
    ).ok is True
    decision = _decision(domain, project_id, "execution.sprint.review")
    fingerprint = next(
        ref.fingerprint
        for ref in decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    stale_review = domain.transition(
        ReviewSprint(
            **_guards(domain, project_id, "execution.sprint.review"),
            idempotency_key="stale-review",
            sprint_id=sprint_id,
            review_fingerprint="sha256:stale",
        )
    )
    assert stale_review.ok is False
    assert stale_review.error is not None
    assert stale_review.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert domain.transition(
        ReviewSprint(
            **_guards(domain, project_id, "execution.sprint.review"),
            idempotency_key="review",
            sprint_id=sprint_id,
            review_fingerprint=fingerprint,
        )
    ).ok is True


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
    review_decision = _decision(domain, project_id, "execution.sprint.review")
    review_fingerprint = next(
        ref.fingerprint
        for ref in review_decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    assert domain.transition(
        ReviewSprint(
            **_guards(domain, project_id, "execution.sprint.review"),
            idempotency_key="review",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok
    assert domain.transition(
        CloseSprint(
            **_guards(domain, project_id, "execution.sprint.close"),
            idempotency_key="close",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    ).ok
    first = RecordPostSprintTriage(
        **_guards(domain, project_id, "execution.post_sprint_triage"),
        idempotency_key="triage-1",
        sprint_id=sprint_id,
        impact="backlog",
        canonical_payload={"requirements": ["REQ-1"]},
    )
    assert domain.transition(first).ok
    duplicate = domain.transition(
        RecordPostSprintTriage(
            **_guards(domain, project_id, "execution.post_sprint_triage"),
            idempotency_key="triage-duplicate",
            sprint_id=sprint_id,
            impact="backlog",
            canonical_payload={"requirements": ["REQ-1"]},
        )
    )
    assert duplicate.ok is False
    correction = domain.transition(
        RecordPostSprintTriage(
            **_guards(domain, project_id, "execution.post_sprint_triage"),
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
