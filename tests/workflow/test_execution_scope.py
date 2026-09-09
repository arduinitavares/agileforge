"""Effective retry execution-scope foundation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from repositories.workflow import WorkflowFactRepository
from tests.workflow.execution_fixtures import (
    seed_started_execution,
    seed_started_execution_with_transitive_dependency,
)
from tests.workflow.test_execution_transitions import (
    _complete_execution_sprint,
    _triage_execution_sprint,
)
from workflow.execution_identity import (
    ExecutionIdentity,
    execution_instance_key,
    parse_execution_instance_key,
)
from workflow.execution_integrity import (
    execution_contract,
    sprint_close_fingerprint,
    sprint_review_fingerprint,
)
from workflow.execution_scope import (
    ExecutionScopeError,
    current_execution_scope,
    resolve_execution_scope,
)
from workflow.facts import (
    SprintClosureFact,
    SprintRetryFact,
    SprintRetryStartFact,
    SprintReviewFact,
)
from workflow.sprint_lineage import (
    current_sprint_stream_artifacts,
    plan_has_matching_sprint_start,
)


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
        status="planned",
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


@pytest.mark.parametrize("kind", ["task", "story", "sprint"])
def test_scoped_instance_roundtrip(kind: str) -> None:
    """Retry keys retain an exact subject without changing original key bytes."""
    assert execution_instance_key(kind, 7) == f"{kind}:7"
    key = execution_instance_key(kind, 7, 2)
    assert key == f"retry:2:{kind}:7"
    assert parse_execution_instance_key(key) == ExecutionIdentity(kind, 7, 2)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "retry:0:task:7",
        "retry:2:task:8:extra",
        "task:-1",
        "retry:02:task:7",
        "retry:2:unknown:7",
    ],
)
def test_invalid_execution_binding_is_rejected(key: str) -> None:
    """Noncanonical keys cannot be replayed across execution subjects."""
    with pytest.raises(ValueError, match="canonical"):
        parse_execution_instance_key(key)


@pytest.mark.parametrize(
    ("kind", "entity_id", "retry_attempt_id"),
    [
        ("task", True, None),
        ("task", 0, None),
        ("task", 7, True),
        ("task", 7, 0),
        ("unknown", 7, None),
    ],
)
def test_execution_identity_constructor_rejects_nonpositive_or_boolean_ids(
    kind: str,
    entity_id: int | bool,
    retry_attempt_id: int | bool | None,
) -> None:
    """Constructor validation prevents noncanonical bindings before serialization."""
    with pytest.raises(ValueError, match=r"positive|invalid"):
        ExecutionIdentity(kind, entity_id, retry_attempt_id)  # type: ignore[arg-type]


def test_current_scope_selects_the_one_active_original_execution() -> None:
    """A valid active original resolves as the current attempt."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    project_id, sprint_id, _story_id, _task_id = seed_started_execution(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    scope = current_execution_scope(snapshot)

    assert scope is not None
    assert scope.sprint_id == sprint_id
    assert scope.retry_attempt_id is None
    assert scope.status == "active"


def test_current_scope_returns_none_for_an_unstarted_original_plan() -> None:
    """Planning keeps ownership of an accepted original until it is started."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    project_id, sprint_id, _story_id, _task_id = seed_started_execution(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    original = next(item for item in snapshot.sprints if item.sprint_id == sprint_id)
    unstarted = snapshot.model_copy(
        update={
            "sprints": (
                original.model_copy(update={"status": "planned", "completed_at": None}),
            ),
            "sprint_starts": (),
        }
    )

    assert current_execution_scope(unstarted) is None


def test_current_scope_rejects_an_original_and_retry_live_at_once() -> None:
    """A live retry cannot overlap the original execution it supersedes."""
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
        story_statuses=((story_id, "To Do"),),
        task_statuses=((task_id, "To Do"),),
    ).model_copy(update={"status": "planned"})

    with pytest.raises(ExecutionScopeError, match="multiple live"):
        current_execution_scope(
            snapshot.model_copy(update={"sprint_retries": (retry,)})
        )


def test_current_scope_selects_the_valid_planned_retry_after_triaged_source() -> None:
    """A planned retry is current only after its source attempt is terminal."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    domain, project_id, sprint_id, story_id, task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    retry = _retry_fact(
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=execution_contract(snapshot, sprint_id).fingerprint,
        story_statuses=((story_id, "To Do"),),
        task_statuses=((task_id, "To Do"),),
    ).model_copy(update={"status": "planned"})

    scope = current_execution_scope(
        snapshot.model_copy(update={"sprint_retries": (retry,)})
    )

    assert scope is not None
    assert scope.retry_attempt_id == retry.retry_attempt_id
    assert scope.status == "planned"


def test_current_scope_rejects_unlinked_retry_ordinal() -> None:
    """A completed source cannot make a gap in retry lineage executable."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    domain, project_id, sprint_id, story_id, task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    retry = _retry_fact(
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=execution_contract(snapshot, sprint_id).fingerprint,
        story_statuses=((story_id, "To Do"),),
        task_statuses=((task_id, "To Do"),),
    ).model_copy(
        update={"status": "planned", "ordinal": 3, "predecessor_retry_attempt_id": 99}
    )

    with pytest.raises(ExecutionScopeError, match="lineage"):
        current_execution_scope(
            snapshot.model_copy(update={"sprint_retries": (retry,)})
        )


def test_retry_scope_hashes_are_attempt_bound_without_mutating_history() -> None:
    """Retry hashes differ while original terminal evidence stays exact."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    domain, project_id, sprint_id, story_id, task_id, original_review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    original_close = snapshot.sprint_closures[0].close_fingerprint
    assert sprint_review_fingerprint(snapshot, sprint_id) == original_review
    assert (
        sprint_close_fingerprint(snapshot, sprint_id, original_review) == original_close
    )
    before = snapshot.model_dump(mode="json")
    contract = execution_contract(snapshot, sprint_id)
    started_at = datetime(2026, 9, 9, 12, tzinfo=UTC)
    provisional = _retry_fact(
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=contract.fingerprint,
        story_statuses=((story_id, "Done"),),
        task_statuses=((task_id, "Done"),),
    ).model_copy(
        update={
            "status": "completed",
            "started_at": started_at,
            "completed_at": started_at + timedelta(minutes=1),
            "start": SprintRetryStartFact(
                start_id=701,
                retry_attempt_id=17,
                contract_fingerprint=contract.fingerprint,
                decision_fingerprint="sha256:retry-start",
                started_by="owner@example.com",
                started_at=started_at,
            ),
            "task_completions": snapshot.task_completions,
            "story_completions": snapshot.story_completions,
            "post_sprint_triage": snapshot.post_sprint_triage,
        }
    )
    retry_snapshot = snapshot.model_copy(update={"sprint_retries": (provisional,)})
    scope = resolve_execution_scope(
        retry_snapshot,
        sprint_id=sprint_id,
        retry_attempt_id=17,
    )
    retry_review = sprint_review_fingerprint(
        retry_snapshot,
        sprint_id,
        scope=scope,
    )
    retry_close = sprint_close_fingerprint(
        retry_snapshot,
        sprint_id,
        retry_review,
        scope=scope,
    )
    complete = provisional.model_copy(
        update={
            "sprint_reviews": (
                SprintReviewFact(
                    review_id=702,
                    sprint_id=sprint_id,
                    review_fingerprint=retry_review,
                ),
            ),
            "sprint_closures": (
                SprintClosureFact(
                    closure_id=703,
                    sprint_id=sprint_id,
                    review_fingerprint=retry_review,
                    close_fingerprint=retry_close,
                ),
            ),
        }
    )

    complete_scope = resolve_execution_scope(
        retry_snapshot.model_copy(update={"sprint_retries": (complete,)}),
        sprint_id=sprint_id,
        retry_attempt_id=17,
    )

    assert retry_review != original_review
    assert retry_close != original_close
    assert complete_scope.sprint_reviews[0].review_fingerprint == retry_review
    assert snapshot.model_dump(mode="json") == before


def test_shared_sprint_lineage_selector_keeps_the_current_started_stream() -> None:
    """The extracted selector preserves planning's accepted started-stream choice."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    project_id, sprint_id, _story_id, _task_id = seed_started_execution(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    spec = next(item for item in snapshot.spec_versions if item.status == "approved")
    artifacts = tuple(
        item
        for item in snapshot.planning_artifacts
        if item.artifact_type == "sprint_plan"
        and item.spec_version_id == spec.spec_version_id
        and item.spec_hash == spec.spec_hash
    )

    stream = current_sprint_stream_artifacts(
        snapshot,
        artifacts,
        spec_identity=(spec.spec_version_id, spec.spec_hash),
    )
    accepted = next(item for item in stream if item.activated_sprint_id == sprint_id)

    assert plan_has_matching_sprint_start(snapshot, accepted)
