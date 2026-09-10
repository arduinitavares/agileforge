"""Durable storage contracts for isolated Sprint retry attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from models.sprint_retry import (
    SprintRetryAttempt,
    SprintRetryClosure,
    SprintRetryReview,
    SprintRetryStart,
    SprintRetryStoryClosure,
    SprintRetryStoryState,
    SprintRetryTaskEvidence,
    SprintRetryTaskState,
    SprintRetryTriage,
)
from repositories.sprint_retry import SprintRetryFactLoadError, load_sprint_retry_facts
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.execution_integrity import (
    StoryClosurePayload,
    TaskEvidencePayload,
    execution_contract,
    story_completion_fingerprint,
    task_evidence_fingerprint,
    triage_payload_fingerprint,
)
from workflow.execution_scope import resolve_execution_scope
from workflow.facts import SprintRetryFact, SprintRetryStartFact, TaskCompletionFact

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def _file_engine(path: Path) -> Engine:
    """Create a disposable file database with real SQLite FK enforcement."""
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


@dataclass(frozen=True)
class RetryRows:
    """Two independent live retry rows for the same synthetic Project."""

    planned: SprintRetryAttempt
    another_planned: SprintRetryAttempt


@pytest.fixture
def retry_subjects(tmp_path: Path) -> Iterator[tuple[Engine, int, int, int, int]]:
    """Seed a complete synthetic Sprint with actual Project-owned work."""
    engine = _file_engine(tmp_path / "retry-models.sqlite")
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    try:
        yield engine, project_id, sprint_id, story_id, task_id
    finally:
        engine.dispose()


@pytest.fixture
def retry_rows(retry_subjects: tuple[Engine, int, int, int, int]) -> RetryRows:
    """Build rows whose concurrent live states must be rejected by SQLite."""
    _engine, project_id, sprint_id, _story_id, _task_id = retry_subjects
    return RetryRows(
        planned=_attempt(project_id, sprint_id, ordinal=2, receipt="retry-one"),
        another_planned=_attempt(
            project_id,
            sprint_id,
            ordinal=3,
            receipt="retry-two",
        ),
    )


def _attempt(  # noqa: PLR0913
    project_id: int,
    sprint_id: int,
    *,
    ordinal: int,
    receipt: str,
    status: str = "Planned",
    predecessor_retry_attempt_id: int | None = None,
) -> SprintRetryAttempt:
    return SprintRetryAttempt(
        project_id=project_id,
        sprint_id=sprint_id,
        ordinal=ordinal,
        predecessor_retry_attempt_id=predecessor_retry_attempt_id,
        contract_fingerprint=f"sha256:contract-{ordinal}",
        created_by="owner@example.com",
        rationale="Repeat verified work with fresh evidence.",
        creation_fingerprint=f"sha256:creation-{ordinal}",
        creation_receipt_key=receipt,
        created_at=NOW,
        status=status,
    )


def test_two_live_attempts_in_a_project_are_rejected(
    retry_subjects: tuple[Engine, int, int, int, int], retry_rows: RetryRows
) -> None:
    """Dropping the live-attempt index would permit conflicting retry work."""
    engine, _project_id, _sprint_id, _story_id, _task_id = retry_subjects
    with Session(engine) as session:
        session.add(retry_rows.planned)
        session.flush()
        session.add(retry_rows.another_planned)
        with pytest.raises(IntegrityError):
            session.flush()


def test_retry_evidence_rejects_a_progress_row_from_another_attempt(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Removing the composite progress FK could attach prior-attempt proof."""
    engine, project_id, sprint_id, _story_id, task_id = retry_subjects
    with Session(engine) as session:
        first = _attempt(
            project_id, sprint_id, ordinal=2, receipt="first", status="Completed"
        )
        session.add(first)
        session.flush()
        assert first.retry_attempt_id is not None
        session.add(
            SprintRetryTaskState(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=first.retry_attempt_id,
                task_id=task_id,
                status="To Do",
            )
        )
        second = _attempt(
            project_id,
            sprint_id,
            ordinal=3,
            receipt="second",
            predecessor_retry_attempt_id=first.retry_attempt_id,
        )
        session.add(second)
        session.flush()
        assert second.retry_attempt_id is not None
        session.add(
            SprintRetryTaskEvidence(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=second.retry_attempt_id,
                task_id=task_id,
                outcome_summary="Fresh proof.",
                artifact_refs_json='["proof.txt"]',
                acceptance_result="fully_met",
                checklist_result_json='{"check":"passed"}',
                evidence_fingerprint="sha256:evidence",
                completed_by="owner@example.com",
                completed_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_retry_start_and_evidence_are_singletons_per_attempt_subject(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Removing retry-local uniqueness would duplicate immutable evidence."""
    engine, project_id, sprint_id, _story_id, task_id = retry_subjects
    with Session(engine) as session:
        attempt = _attempt(project_id, sprint_id, ordinal=2, receipt="singleton")
        session.add(attempt)
        session.flush()
        assert attempt.retry_attempt_id is not None
        session.add(
            SprintRetryTaskState(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                task_id=task_id,
                status="To Do",
            )
        )
        session.add(
            SprintRetryStart(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                contract_fingerprint="sha256:contract-2",
                decision_fingerprint="sha256:decision",
                started_by="owner@example.com",
                started_at=NOW,
            )
        )
        session.flush()
        session.add(
            SprintRetryStart(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                contract_fingerprint="sha256:contract-2",
                decision_fingerprint="sha256:decision-duplicate",
                started_by="owner@example.com",
                started_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_completed_retry_can_become_the_predecessor_of_one_new_attempt(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Removing predecessor cardinality would fork immutable retry history."""
    engine, project_id, sprint_id, _story_id, _task_id = retry_subjects
    with Session(engine) as session:
        first = _attempt(
            project_id, sprint_id, ordinal=2, receipt="chain-one", status="Completed"
        )
        session.add(first)
        session.flush()
        assert first.retry_attempt_id is not None
        second = _attempt(
            project_id,
            sprint_id,
            ordinal=3,
            receipt="chain-two",
            predecessor_retry_attempt_id=first.retry_attempt_id,
        )
        session.add(second)
        session.flush()
        assert second.retry_attempt_id is not None
        assert second.predecessor_retry_attempt_id == first.retry_attempt_id


def test_retry_task_evidence_is_unique_within_its_attempt_subject(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Removing evidence uniqueness would make one retry Task ambiguous."""
    engine, project_id, sprint_id, _story_id, task_id = retry_subjects
    with Session(engine) as session:
        attempt = _attempt(project_id, sprint_id, ordinal=2, receipt="evidence-unique")
        session.add(attempt)
        session.flush()
        assert attempt.retry_attempt_id is not None
        session.add(
            SprintRetryTaskState(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                task_id=task_id,
                status="To Do",
            )
        )
        session.flush()
        evidence = {
            "project_id": project_id,
            "sprint_id": sprint_id,
            "retry_attempt_id": attempt.retry_attempt_id,
            "task_id": task_id,
            "outcome_summary": "Fresh proof.",
            "artifact_refs_json": '["proof.txt"]',
            "acceptance_result": "fully_met",
            "checklist_result_json": '{"check":"passed"}',
            "evidence_fingerprint": "sha256:evidence",
            "completed_by": "owner@example.com",
            "completed_at": NOW,
        }
        session.add(SprintRetryTaskEvidence(**evidence))
        session.flush()
        session.add(SprintRetryTaskEvidence(**evidence))
        with pytest.raises(IntegrityError):
            session.flush()


def test_retry_triage_correction_stays_within_its_attempt(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Dropping the retry-local correction FK would allow cross-attempt edits."""
    engine, project_id, sprint_id, _story_id, _task_id = retry_subjects
    with Session(engine) as session:
        attempt = _attempt(project_id, sprint_id, ordinal=2, receipt="triage-chain")
        session.add(attempt)
        session.flush()
        assert attempt.retry_attempt_id is not None
        original = SprintRetryTriage(
            project_id=project_id,
            sprint_id=sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
            impact="none",
            canonical_payload_json='{"summary":"Original."}',
            payload_fingerprint="sha256:original",
            recorded_by="owner@example.com",
            recorded_at=NOW,
        )
        session.add(original)
        session.flush()
        assert original.sprint_retry_triage_id is not None
        correction = SprintRetryTriage(
            project_id=project_id,
            sprint_id=sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
            impact="backlog",
            canonical_payload_json='{"summary":"Correction."}',
            payload_fingerprint="sha256:correction",
            supersedes_sprint_retry_triage_id=original.sprint_retry_triage_id,
            recorded_by="owner@example.com",
            recorded_at=NOW,
        )
        session.add(correction)
        session.flush()
        assert (
            correction.supersedes_sprint_retry_triage_id
            == original.sprint_retry_triage_id
        )


def test_retry_story_closure_requires_its_own_attempt_progress(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """A closure cannot borrow an identically scoped Story state from attempt two."""
    engine, project_id, sprint_id, story_id, _task_id = retry_subjects
    with Session(engine) as session:
        first = _attempt(
            project_id,
            sprint_id,
            ordinal=2,
            receipt="story-state-one",
            status="Completed",
        )
        session.add(first)
        session.flush()
        assert first.retry_attempt_id is not None
        session.add(
            SprintRetryStoryState(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=first.retry_attempt_id,
                story_id=story_id,
                status="Done",
            )
        )
        session.flush()
        second = _attempt(
            project_id,
            sprint_id,
            ordinal=3,
            receipt="story-state-two",
            predecessor_retry_attempt_id=first.retry_attempt_id,
        )
        session.add(second)
        session.flush()
        assert second.retry_attempt_id is not None
        session.add(
            SprintRetryStoryClosure(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=second.retry_attempt_id,
                story_id=story_id,
                completion_fingerprint="sha256:cross-attempt",
                resolution="Completed",
                delivered="Wrong attempt.",
                evidence="proof.txt",
                known_gaps="None.",
                closed_by="owner@example.com",
                closed_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_retry_triage_correction_cannot_supersede_another_attempt(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """The correction parent is composite-owned by its exact retry attempt."""
    engine, project_id, sprint_id, _story_id, _task_id = retry_subjects
    with Session(engine) as session:
        first = _attempt(
            project_id, sprint_id, ordinal=2, receipt="triage-one", status="Completed"
        )
        session.add(first)
        session.flush()
        assert first.retry_attempt_id is not None
        original = SprintRetryTriage(
            project_id=project_id,
            sprint_id=sprint_id,
            retry_attempt_id=first.retry_attempt_id,
            impact="none",
            canonical_payload_json='{"summary":"Original."}',
            payload_fingerprint="sha256:original",
            recorded_by="owner@example.com",
            recorded_at=NOW,
        )
        session.add(original)
        session.flush()
        assert original.sprint_retry_triage_id is not None
        second = _attempt(
            project_id,
            sprint_id,
            ordinal=3,
            receipt="triage-two",
            predecessor_retry_attempt_id=first.retry_attempt_id,
        )
        session.add(second)
        session.flush()
        assert second.retry_attempt_id is not None
        session.add(
            SprintRetryTriage(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=second.retry_attempt_id,
                impact="none",
                canonical_payload_json='{"summary":"Wrong parent."}',
                payload_fingerprint="sha256:cross-attempt",
                supersedes_sprint_retry_triage_id=original.sprint_retry_triage_id,
                recorded_by="owner@example.com",
                recorded_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_retry_loader_rejects_changed_triage_payload_fingerprint(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Skipping triage fingerprint validation would accept altered retry impact."""
    engine, project_id, sprint_id, _story_id, _task_id = retry_subjects
    with Session(engine) as session:
        base = WorkflowFactRepository(session).load(project_id)
        attempt = _attempt(
            project_id,
            sprint_id,
            ordinal=2,
            receipt="bad-triage",
            status="Completed",
        )
        session.add(attempt)
        session.flush()
        assert attempt.retry_attempt_id is not None
        session.add(
            SprintRetryTriage(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                impact="none",
                canonical_payload_json='{"summary":"Tampered."}',
                payload_fingerprint="sha256:not-the-canonical-value",
                recorded_by="owner@example.com",
                recorded_at=NOW,
            )
        )
        session.commit()

        with pytest.raises(SprintRetryFactLoadError, match="payload fingerprint"):
            load_sprint_retry_facts(session, project_id=project_id, snapshot=base)


def test_retry_loader_does_not_flush_or_read_pending_retry_rows(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Retry facts have the same persisted-only read boundary as workflow facts."""
    engine, project_id, sprint_id, _story_id, _task_id = retry_subjects
    with Session(engine) as session:
        base = WorkflowFactRepository(session).load(project_id)
        session.add(_attempt(project_id, sprint_id, ordinal=2, receipt="unflushed"))
        assert (
            load_sprint_retry_facts(session, project_id=project_id, snapshot=base) == ()
        )


def test_workflow_loader_rejects_tampered_retry_task_and_story_fingerprints(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """Retry evidence must use the original loader's scoped fingerprint checks."""
    engine, project_id, sprint_id, story_id, task_id = retry_subjects
    with Session(engine) as session:
        base = WorkflowFactRepository(session).load(project_id)
        contract = execution_contract(base, sprint_id)
        attempt = _attempt(
            project_id,
            sprint_id,
            ordinal=2,
            receipt="verified-fingerprints",
            status="Completed",
        )
        attempt.contract_fingerprint = contract.fingerprint
        attempt.started_at = NOW
        attempt.completed_at = NOW
        session.add(attempt)
        session.flush()
        assert attempt.retry_attempt_id is not None
        retry_start = SprintRetryStart(
            project_id=project_id,
            sprint_id=sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
            contract_fingerprint=contract.fingerprint,
            decision_fingerprint="sha256:retry-start",
            started_by="owner@example.com",
            started_at=NOW,
        )
        session.add(retry_start)
        session.flush()
        assert retry_start.sprint_retry_start_id is not None
        session.add_all(
            (
                SprintRetryStoryState(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=attempt.retry_attempt_id,
                    story_id=story_id,
                    status="Done",
                ),
                SprintRetryTaskState(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=attempt.retry_attempt_id,
                    task_id=task_id,
                    status="Done",
                ),
            )
        )
        session.flush()
        provisional = SprintRetryFact(
            retry_attempt_id=attempt.retry_attempt_id,
            project_id=project_id,
            sprint_id=sprint_id,
            ordinal=2,
            predecessor_retry_attempt_id=None,
            contract_fingerprint=contract.fingerprint,
            created_by=attempt.created_by,
            rationale=attempt.rationale,
            creation_fingerprint=attempt.creation_fingerprint,
            creation_receipt_key=attempt.creation_receipt_key,
            created_at=attempt.created_at,
            status="completed",
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
            story_statuses=((story_id, "Done"),),
            task_statuses=((task_id, "Done"),),
            start=SprintRetryStartFact(
                start_id=retry_start.sprint_retry_start_id,
                retry_attempt_id=attempt.retry_attempt_id,
                contract_fingerprint=contract.fingerprint,
                decision_fingerprint=retry_start.decision_fingerprint,
                started_by=retry_start.started_by,
                started_at=retry_start.started_at,
            ),
        )
        retry_snapshot = base.model_copy(update={"sprint_retries": (provisional,)})
        scope = resolve_execution_scope(
            retry_snapshot,
            sprint_id=sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
        )
        task = next(item for item in scope.tasks if item.task_id == task_id)
        payload = TaskEvidencePayload(
            outcome_summary="Fresh proof.",
            artifact_refs=("proof.txt",),
            acceptance_result="fully_met",
            checklist_result={"check": "passed"},
        )
        task_fingerprint = task_evidence_fingerprint(
            retry_snapshot, task, evidence=payload, scope=scope
        )
        evidence = SprintRetryTaskEvidence(
            project_id=project_id,
            sprint_id=sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
            task_id=task_id,
            outcome_summary=payload.outcome_summary,
            artifact_refs_json='["proof.txt"]',
            acceptance_result=payload.acceptance_result,
            checklist_result_json='{"check":"passed"}',
            evidence_fingerprint=task_fingerprint,
            completed_by="owner@example.com",
            completed_at=NOW,
        )
        session.add(evidence)
        session.flush()
        assert evidence.sprint_retry_task_evidence_id is not None
        task_fact = TaskCompletionFact(
            completion_id=evidence.sprint_retry_task_evidence_id,
            task_id=task_id,
            sprint_id=sprint_id,
            outcome_summary=payload.outcome_summary,
            artifact_refs=payload.artifact_refs,
            acceptance_result=payload.acceptance_result,
            checklist_result=payload.checklist_result,
            evidence_fingerprint=task_fingerprint,
        )
        retry_snapshot = base.model_copy(
            update={
                "sprint_retries": (
                    provisional.model_copy(update={"task_completions": (task_fact,)}),
                )
            }
        )
        scope = resolve_execution_scope(
            retry_snapshot,
            sprint_id=sprint_id,
            retry_attempt_id=attempt.retry_attempt_id,
        )
        closure = StoryClosurePayload(
            resolution="Completed",
            delivered="Delivered.",
            evidence="proof.txt",
            known_gaps="None.",
        )
        story_fingerprint = story_completion_fingerprint(
            retry_snapshot,
            sprint_id=sprint_id,
            story_id=story_id,
            closure=closure,
            scope=scope,
        )
        session.add(
            SprintRetryStoryClosure(
                project_id=project_id,
                sprint_id=sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                story_id=story_id,
                completion_fingerprint=story_fingerprint,
                resolution=closure.resolution,
                delivered=closure.delivered,
                evidence=closure.evidence,
                known_gaps=closure.known_gaps,
                closed_by="owner@example.com",
                closed_at=NOW,
            )
        )
        session.commit()
        assert WorkflowFactRepository(session).load(project_id).sprint_retries
        evidence.evidence_fingerprint = "sha256:tampered-task"
        session.add(evidence)
        session.commit()

        with pytest.raises(WorkflowFactLoadError, match="Task evidence fingerprint"):
            WorkflowFactRepository(session).load(project_id)

        evidence.evidence_fingerprint = task_fingerprint
        session.add(evidence)
        closure_row = session.exec(select(SprintRetryStoryClosure)).one()
        closure_row.completion_fingerprint = "sha256:tampered-story"
        session.add(closure_row)
        session.commit()
        with pytest.raises(WorkflowFactLoadError, match="Story closure fingerprint"):
            WorkflowFactRepository(session).load(project_id)


def test_retry_loader_returns_deterministic_typed_history(
    retry_subjects: tuple[Engine, int, int, int, int],
) -> None:
    """A loader that drops or reorders retry rows loses durable execution facts."""
    engine, project_id, sprint_id, story_id, task_id = retry_subjects
    with Session(engine) as session:
        base = WorkflowFactRepository(session).load(project_id)
        first = _attempt(
            project_id, sprint_id, ordinal=2, receipt="load-one", status="Completed"
        )
        session.add(first)
        session.flush()
        assert first.retry_attempt_id is not None
        session.add_all(
            [
                SprintRetryStoryState(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=first.retry_attempt_id,
                    story_id=story_id,
                    status="Done",
                ),
                SprintRetryTaskState(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=first.retry_attempt_id,
                    task_id=task_id,
                    status="Done",
                ),
                SprintRetryStart(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=first.retry_attempt_id,
                    contract_fingerprint="sha256:contract-2",
                    decision_fingerprint="sha256:decision",
                    started_by="owner@example.com",
                    started_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                SprintRetryReview(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=first.retry_attempt_id,
                    review_fingerprint="sha256:review",
                    reviewed_by="owner@example.com",
                    reviewed_at=NOW,
                ),
                SprintRetryClosure(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=first.retry_attempt_id,
                    review_fingerprint="sha256:review",
                    close_fingerprint="sha256:closure",
                    closed_by="owner@example.com",
                    closed_at=NOW,
                ),
                SprintRetryTriage(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    retry_attempt_id=first.retry_attempt_id,
                    impact="none",
                    canonical_payload_json='{"summary":"No downstream change."}',
                    payload_fingerprint=triage_payload_fingerprint(
                        "none", {"summary": "No downstream change."}
                    ),
                    recorded_by="owner@example.com",
                    recorded_at=NOW,
                ),
            ]
        )
        session.commit()
        facts = load_sprint_retry_facts(session, project_id=project_id, snapshot=base)

    assert [fact.ordinal for fact in facts] == [2]
    assert facts[0].status == "completed"
    assert facts[0].story_statuses == ((story_id, "Done"),)
    assert facts[0].task_statuses == ((task_id, "Done"),)
    assert facts[0].start is not None
    assert facts[0].task_completions == ()
    assert facts[0].post_sprint_triage[0].canonical_payload == {
        "summary": "No downstream change."
    }
