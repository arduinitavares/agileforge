"""Effective retry execution-scope foundation tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine

from repositories.workflow import WorkflowFactRepository
from tests.workflow.execution_fixtures import (
    seed_started_execution,
    seed_started_execution_with_transitive_dependency,
)
from workflow.execution_integrity import execution_contract
from workflow.execution_scope import ExecutionScopeError, resolve_execution_scope
from workflow.facts import SprintRetryFact


def _retry_fact(  # noqa: PLR0913
    *,
    retry_attempt_id: int,
    project_id: int,
    sprint_id: int,
    contract_fingerprint: str,
    story_statuses: tuple[tuple[int, str], ...],
    task_statuses: tuple[tuple[int, str], ...],
) -> SprintRetryFact:
    return SprintRetryFact(
        retry_attempt_id=retry_attempt_id,
        project_id=project_id,
        sprint_id=sprint_id,
        ordinal=2,
        predecessor_retry_attempt_id=None,
        contract_fingerprint=contract_fingerprint,
        created_by="owner@example.com",
        rationale="Fresh evidence is required.",
        creation_fingerprint="sha256:creation",
        creation_receipt_key="scope-test",
        created_at=datetime(2026, 9, 9, tzinfo=UTC),
        status="active",
        story_statuses=story_statuses,
        task_statuses=task_statuses,
    )


def test_retry_scope_has_an_attempt_bound_effective_contract() -> None:
    """Retry progress overlays only selected history and cannot share source hashes."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    contract = execution_contract(snapshot, sprint_id)
    retry = _retry_fact(
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=contract.fingerprint,
        story_statuses=((story_id, "Done"),),
        task_statuses=((task_id, "Done"),),
    )

    scope = resolve_execution_scope(
        snapshot.model_copy(update={"sprint_retries": (retry,)}),
        sprint_id=sprint_id,
        retry_attempt_id=retry.retry_attempt_id,
    )

    assert scope.contract.fingerprint != contract.fingerprint
    assert scope.contract.stories[0].status == "Done"
    assert scope.project_stories[0].status == "Done"
    assert scope.tasks[0].status == "Done"


def test_retry_scope_rejects_duplicate_progress_subjects() -> None:
    """Duplicate durable progress cannot collapse into a dictionary silently."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    retry = _retry_fact(
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=execution_contract(snapshot, sprint_id).fingerprint,
        story_statuses=((story_id, "Done"),),
        task_statuses=((task_id, "Done"), (task_id, "To Do")),
    )

    with pytest.raises(ExecutionScopeError, match="duplicate subjects"):
        resolve_execution_scope(
            snapshot.model_copy(update={"sprint_retries": (retry,)}),
            sprint_id=sprint_id,
            retry_attempt_id=retry.retry_attempt_id,
        )


def test_retry_scope_keeps_an_accepted_external_prerequisite_terminal() -> None:
    """An external prerequisite accepted by the original graph stays terminal."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    (
        project_id,
        sprint_id,
        story_id,
        external_story_id,
        _transitive_story_id,
        task_id,
        _dependency_id,
        _transitive_dependency_id,
    ) = seed_started_execution_with_transitive_dependency(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    retry = _retry_fact(
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=execution_contract(snapshot, sprint_id).fingerprint,
        story_statuses=((story_id, "Done"),),
        task_statuses=((task_id, "Done"),),
    )

    scope = resolve_execution_scope(
        snapshot.model_copy(update={"sprint_retries": (retry,)}),
        sprint_id=sprint_id,
        retry_attempt_id=retry.retry_attempt_id,
    )

    external = next(
        item for item in scope.project_stories if item.story_id == external_story_id
    )
    assert external.status == "Accepted"
    assert scope.tasks[0].dependencies_satisfied is True
