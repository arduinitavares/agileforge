"""Effective retry execution-scope foundation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

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
    ExecutionKind,
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
    WorkflowFactSnapshot,
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


def _started_retry(  # noqa: PLR0913
    snapshot: WorkflowFactSnapshot,
    *,
    retry_attempt_id: int,
    project_id: int,
    sprint_id: int,
    story_id: int,
    task_id: int,
    ordinal: int = 2,
    predecessor_retry_attempt_id: int | None = None,
) -> SprintRetryFact:
    """Build one retry with the immutable start required for active lifecycle."""
    contract = execution_contract(snapshot, sprint_id)
    started_at = datetime(2026, 9, 9, 12, tzinfo=UTC)
    return _retry_fact(
        retry_attempt_id=retry_attempt_id,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=contract.fingerprint,
        story_statuses=((story_id, "Done"),),
        task_statuses=((task_id, "Done"),),
    ).model_copy(
        update={
            "ordinal": ordinal,
            "predecessor_retry_attempt_id": predecessor_retry_attempt_id,
            "status": "active",
            "started_at": started_at,
            "start": SprintRetryStartFact(
                start_id=700 + retry_attempt_id,
                retry_attempt_id=retry_attempt_id,
                contract_fingerprint=contract.fingerprint,
                decision_fingerprint=f"sha256:retry-start-{retry_attempt_id}",
                started_by="owner@example.com",
                started_at=started_at,
            ),
            "task_completions": tuple(
                item
                for item in snapshot.task_completions
                if item.sprint_id == sprint_id
            ),
            "story_completions": tuple(
                item
                for item in snapshot.story_completions
                if item.sprint_id == sprint_id
            ),
        }
    )


def _closed_retry(
    snapshot: WorkflowFactSnapshot,
    retry: SprintRetryFact,
    *,
    include_triage: bool,
) -> SprintRetryFact:
    """Bind review and close facts to one retry without using future graph code."""
    retry_snapshot = snapshot.model_copy(update={"sprint_retries": (retry,)})
    scope = resolve_execution_scope(
        retry_snapshot,
        sprint_id=retry.sprint_id,
        retry_attempt_id=retry.retry_attempt_id,
    )
    review_fingerprint = sprint_review_fingerprint(
        retry_snapshot,
        retry.sprint_id,
        scope=scope,
    )
    close_fingerprint = sprint_close_fingerprint(
        retry_snapshot,
        retry.sprint_id,
        review_fingerprint,
        scope=scope,
    )
    assert retry.started_at is not None
    return retry.model_copy(
        update={
            "status": "completed",
            "completed_at": retry.started_at + timedelta(minutes=1),
            "sprint_reviews": (
                SprintReviewFact(
                    review_id=800 + retry.retry_attempt_id,
                    sprint_id=retry.sprint_id,
                    review_fingerprint=review_fingerprint,
                ),
            ),
            "sprint_closures": (
                SprintClosureFact(
                    closure_id=900 + retry.retry_attempt_id,
                    sprint_id=retry.sprint_id,
                    review_fingerprint=review_fingerprint,
                    close_fingerprint=close_fingerprint,
                ),
            ),
            "post_sprint_triage": (
                tuple(
                    item
                    for item in snapshot.post_sprint_triage
                    if item.sprint_id == retry.sprint_id
                )
                if include_triage
                else ()
            ),
        }
    )


def _reviewed_retry(
    snapshot: WorkflowFactSnapshot,
    retry: SprintRetryFact,
) -> SprintRetryFact:
    """Add the one canonical review allowed before explicit Sprint close."""
    retry_snapshot = snapshot.model_copy(update={"sprint_retries": (retry,)})
    scope = resolve_execution_scope(
        retry_snapshot,
        sprint_id=retry.sprint_id,
        retry_attempt_id=retry.retry_attempt_id,
    )
    return retry.model_copy(
        update={
            "sprint_reviews": (
                SprintReviewFact(
                    review_id=800 + retry.retry_attempt_id,
                    sprint_id=retry.sprint_id,
                    review_fingerprint=sprint_review_fingerprint(
                        retry_snapshot,
                        retry.sprint_id,
                        scope=scope,
                    ),
                ),
            ),
        }
    )


def _completed_triaged_snapshot() -> tuple[WorkflowFactSnapshot, int, int, int, int]:
    """Load a real original terminal execution as the immutable retry source."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    domain, project_id, sprint_id, story_id, task_id, _review = (
        _complete_execution_sprint(engine)
    )
    _triage_execution_sprint(domain, project_id=project_id, sprint_id=sprint_id)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    return snapshot, project_id, sprint_id, story_id, task_id


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
def test_scoped_instance_roundtrip(kind: ExecutionKind) -> None:
    """Retry keys retain an exact subject without changing original key bytes."""
    assert execution_instance_key(kind, 7) == f"{kind}:7"
    key = execution_instance_key(kind, 7, 2)
    assert key == f"retry:2:{kind}:7"
    assert parse_execution_instance_key(key) == ExecutionIdentity(kind, 7, 2)


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
        ExecutionIdentity(cast("ExecutionKind", kind), entity_id, retry_attempt_id)


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


def test_current_scope_allows_one_valid_review_on_an_active_retry() -> None:
    """Review is a valid current state until the separate close transition runs."""
    snapshot, project_id, sprint_id, story_id, task_id = _completed_triaged_snapshot()
    retry = _started_retry(
        snapshot,
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    reviewed = _reviewed_retry(snapshot, retry)

    scope = current_execution_scope(
        snapshot.model_copy(update={"sprint_retries": (reviewed,)})
    )

    assert scope is not None
    assert scope.retry_attempt_id == reviewed.retry_attempt_id
    assert scope.status == "active"
    assert scope.sprint_reviews == reviewed.sprint_reviews


def test_current_scope_allows_a_validly_closed_original_or_retry_before_triage(
) -> None:
    """Close and triage are separate current states for original and retry work."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _domain, project_id, _sprint_id, _story_id, _task_id, _review = (
        _complete_execution_sprint(engine)
    )
    with Session(engine) as session:
        untriaged_original = WorkflowFactRepository(session).load(project_id)

    original_scope = current_execution_scope(untriaged_original)

    retry_snapshot, retry_project_id, retry_sprint_id, retry_story_id, retry_task_id = (
        _completed_triaged_snapshot()
    )
    retry = _started_retry(
        retry_snapshot,
        retry_attempt_id=17,
        project_id=retry_project_id,
        sprint_id=retry_sprint_id,
        story_id=retry_story_id,
        task_id=retry_task_id,
    )
    closed_retry = _closed_retry(
        retry_snapshot,
        retry,
        include_triage=False,
    )
    retry_scope = current_execution_scope(
        retry_snapshot.model_copy(update={"sprint_retries": (closed_retry,)})
    )

    assert original_scope is not None
    assert original_scope.retry_attempt_id is None
    assert original_scope.status == "completed"
    assert retry_scope is not None
    assert retry_scope.retry_attempt_id == closed_retry.retry_attempt_id
    assert retry_scope.status == "completed"


@pytest.mark.parametrize("predecessor_state", ["completed_only", "untriaged"])
def test_current_scope_rejects_unfinalized_retry_predecessor(
    predecessor_state: str,
) -> None:
    """A planned successor requires review, close, and resolved triage from retry 2."""
    snapshot, project_id, sprint_id, story_id, task_id = _completed_triaged_snapshot()
    retry_two = _started_retry(
        snapshot,
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    if predecessor_state == "completed_only":
        assert retry_two.started_at is not None
        retry_two = retry_two.model_copy(
            update={
                "status": "completed",
                "completed_at": retry_two.started_at + timedelta(minutes=1),
            }
        )
    else:
        retry_two = _closed_retry(snapshot, retry_two, include_triage=False)
    retry_three = _retry_fact(
        retry_attempt_id=18,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=execution_contract(snapshot, sprint_id).fingerprint,
        story_statuses=((story_id, "To Do"),),
        task_statuses=((task_id, "To Do"),),
    ).model_copy(
        update={
            "ordinal": 3,
            "predecessor_retry_attempt_id": retry_two.retry_attempt_id,
        }
    )

    with pytest.raises(ExecutionScopeError, match=r"triage|incomplete"):
        current_execution_scope(
            snapshot.model_copy(update={"sprint_retries": (retry_two, retry_three)})
        )


def test_current_scope_selects_retry_three_after_finalized_retry_two() -> None:
    """A full review/close/triage chain permits the next planned retry."""
    snapshot, project_id, sprint_id, story_id, task_id = _completed_triaged_snapshot()
    retry_two = _closed_retry(
        snapshot,
        _started_retry(
            snapshot,
            retry_attempt_id=17,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
            task_id=task_id,
        ),
        include_triage=True,
    )
    retry_three = _retry_fact(
        retry_attempt_id=18,
        project_id=project_id,
        sprint_id=sprint_id,
        contract_fingerprint=execution_contract(snapshot, sprint_id).fingerprint,
        story_statuses=((story_id, "To Do"),),
        task_statuses=((task_id, "To Do"),),
    ).model_copy(
        update={
            "ordinal": 3,
            "predecessor_retry_attempt_id": retry_two.retry_attempt_id,
        }
    )

    scope = current_execution_scope(
        snapshot.model_copy(update={"sprint_retries": (retry_two, retry_three)})
    )

    assert scope is not None
    assert scope.retry_attempt_id == retry_three.retry_attempt_id
    assert scope.status == "planned"


@pytest.mark.parametrize("invalid_evidence", ["duplicate_review", "close", "triage"])
def test_current_scope_rejects_active_retry_close_or_triage(
    invalid_evidence: str,
) -> None:
    """An active retry can have only its single validated review evidence."""
    snapshot, project_id, sprint_id, story_id, task_id = _completed_triaged_snapshot()
    retry = _started_retry(
        snapshot,
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    reviewed = _reviewed_retry(snapshot, retry)
    closed = _closed_retry(snapshot, retry, include_triage=True)
    invalid = retry.model_copy(
        update={
            "sprint_reviews": (
                (*reviewed.sprint_reviews, reviewed.sprint_reviews[0])
                if invalid_evidence == "duplicate_review"
                else ()
            ),
            "sprint_closures": (
                closed.sprint_closures if invalid_evidence == "close" else ()
            ),
            "post_sprint_triage": (
                closed.post_sprint_triage if invalid_evidence == "triage" else ()
            ),
        }
    )

    with pytest.raises(ExecutionScopeError, match="Active retry"):
        current_execution_scope(
            snapshot.model_copy(update={"sprint_retries": (invalid,)})
        )


def test_current_scope_rejects_changed_present_review_or_triage() -> None:
    """Present lifecycle evidence must remain canonical even when optional now."""
    snapshot, project_id, sprint_id, story_id, task_id = _completed_triaged_snapshot()
    retry = _started_retry(
        snapshot,
        retry_attempt_id=17,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    reviewed = _reviewed_retry(snapshot, retry).model_copy(
        update={
            "sprint_reviews": (
                SprintReviewFact(
                    review_id=817,
                    sprint_id=sprint_id,
                    review_fingerprint="sha256:changed-review",
                ),
            ),
        }
    )
    closed = _closed_retry(snapshot, retry, include_triage=True).model_copy(
        update={
            "post_sprint_triage": (
                snapshot.post_sprint_triage[0].model_copy(
                    update={"payload_fingerprint": "sha256:changed-triage"}
                ),
            ),
        }
    )

    with pytest.raises(ExecutionScopeError, match="fingerprint changed"):
        current_execution_scope(
            snapshot.model_copy(update={"sprint_retries": (reviewed,)})
        )
    with pytest.raises(ExecutionScopeError, match="triage fingerprint changed"):
        current_execution_scope(
            snapshot.model_copy(update={"sprint_retries": (closed,)})
        )


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
