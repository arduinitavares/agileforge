# tests/workflow/test_task_repository_evidence_integrity.py
"""Completion revision evidence must bind immutable history without backfill."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from models.core import Project
from models.db import ensure_business_db_ready
from models.enums import TaskStatus
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.sprint_retry import SprintRetryTaskEvidence, SprintRetryTaskState
from models.workflow import TaskCompletionEvidence
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.test_execution_transitions import _file_engine
from tests.workflow.test_sprint_retry_execution import _file_domain, _start_retry
from tests.workflow.test_sprint_retry_schema import _frozen_completed_history
from workflow.definitions.execution import execution_graph
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    TaskEvidencePayload,
    canonical_task_evidence_payload,
    task_evidence_fingerprint,
)
from workflow.execution_scope import resolve_execution_scope
from workflow.fingerprints import (
    business_fact_fingerprint,
    canonical_json,
    fact_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.execution_scope import ExecutionScope
    from workflow.facts import TaskFact, WorkflowFactSnapshot


_ORIGINAL_PAYLOAD: str = (
    '{"acceptance_result":"fully_met",'
    '"artifact_refs":["workflow/definitions/execution.py"],'
    '"checklist_result":{"Run focused tests":"passed"},"completion_id":1,'
    '"evidence_fingerprint":"sha256:6b06e5ad147f200f8c6b716e6865ddfadf393d3a24486b2149cf96d5511c828f",'
    '"outcome_summary":"Implemented execution graph.","sprint_id":1,"task_id":1}'
)
_RETRY_PAYLOAD: str = (
    '{"acceptance_result":"fully_met",'
    '"artifact_refs":["workflow/definitions/execution.py"],'
    '"checklist_result":{"Run focused tests":"passed"},"completion_id":1,'
    '"evidence_fingerprint":"sha256:bd164103cfbcf5b7afc076989c82987442fd99962e309c3c2f4ab67144b94310",'
    '"outcome_summary":"Re-executed the scoped work.","sprint_id":1,"task_id":1}'
)


def _load(engine: Engine) -> WorkflowFactSnapshot:
    """Exercise both original and retry readers through their public boundary."""
    with Session(engine) as session:
        return WorkflowFactRepository(session).load(1)


@pytest.fixture
def legacy_engine(tmp_path: Path) -> Engine:
    """Use frozen real history, never a revised completion writer."""
    engine = _frozen_completed_history(tmp_path / "legacy.sqlite")
    ensure_business_db_ready(engine)
    return engine


def test_legacy_completion_and_nested_snapshot_dumps_preserve_bytes(
    legacy_engine: Engine,
) -> None:
    """Adding a nullable fact field must not change any old canonical bytes."""
    snapshot = _load(legacy_engine)
    for fact, expected in (
        (snapshot.task_completions[0], _ORIGINAL_PAYLOAD),
        (snapshot.sprint_retries[0].task_completions[0], _RETRY_PAYLOAD),
    ):
        assert canonical_json(fact.model_dump(mode="json")) == expected
        assert canonical_json(fact.model_dump(mode="python")) == expected
        assert canonical_json(json.loads(fact.model_dump_json())) == expected
    for mode in ("python", "json"):
        dumped = snapshot.model_dump(mode=mode)
        assert "repository_evidence" not in dumped["task_completions"][0]
        assert (
            "repository_evidence"
            not in dumped["sprint_retries"][0]["task_completions"][0]
        )
    assert hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest() == (
        "d0c59608ef9c2e13f31b073e209ca5be98cda1d9fe14b4024eb6d8f09dcbd6b0"
    )
    assert fact_fingerprint(snapshot) == (
        "sha256:04c6e73691aeb3dc8b25a5833f450bfe3df4d8c5c105a59fc11603bda5386238"
    )
    assert business_fact_fingerprint(snapshot) == (
        "sha256:e9cedc6713db90cb299bd686e6e471428d866bd0e115c4878906c16d33bf56a7"
    )


def test_legacy_story_and_sprint_history_remains_valid(legacy_engine: Engine) -> None:
    """Null evidence must retain the old completion and downstream closure hashes."""
    snapshot = _load(legacy_engine)
    assert snapshot.story_completions[0].completion_fingerprint == (
        "sha256:9019a41072840a028362cf1c25918c31d07ae9e1a2fc982acca32074adc9dc22"
    )
    assert snapshot.sprint_closures[0].review_fingerprint == (
        "sha256:a6d37ad8378225368916267fe7208d124eda632f7c21b7c2725bf8a7b7a99fb2"
    )
    assert snapshot.sprint_retries[0].story_completions
    assert snapshot.sprint_retries[0].sprint_closures
    assert snapshot.sprint_retries[0].post_sprint_triage


@pytest.mark.parametrize("retry", [False, True])
def test_tampered_original_and_retry_repository_payload_fails_closed(
    legacy_engine: Engine, *, retry: bool
) -> None:
    """A newly injected payload must invalidate the independently frozen hash."""
    with Session(legacy_engine) as session:
        row = (
            session.exec(select(SprintRetryTaskEvidence)).one()
            if retry
            else session.exec(select(TaskCompletionEvidence)).one()
        )
        row.repository_evidence_json = canonical_json(
            {
                "version": "agileforge.task-repository-evidence.v1",
                "state": "not_bound",
                "uncommitted_acknowledged": False,
            }
        )
        session.add(row)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        _load(legacy_engine)


def _captured_payload() -> dict[str, object]:
    """Literal observation with a newer revision than attachment-time HEAD."""
    return {
        "version": "agileforge.task-repository-evidence.v1",
        "state": "captured",
        "repository_binding_id": 2,
        "repository_binding_fingerprint": "sha256:" + "a" * 64,
        "worktree_path": "/example/repository",
        "common_git_dir": "/example/repository/.git",
        "head_sha": "b" * 40,
        "branch_name": "delivery",
        "detached_head": False,
        "dirty": True,
        "dirty_path_count": 2,
        "dirty_paths": ["src/main.py", "tests/test_main.py"],
        "dirty_paths_truncated": False,
        "probed_path_matches_binding": True,
        "other_worktrees_present": False,
        "status_fingerprint": "sha256:" + "c" * 64,
        "probe_version": "agileforge.repository-probe.v1",
        "inspected_at": "2026-10-07T12:00:00Z",
        "uncommitted_acknowledged": True,
    }


def _unavailable_payload() -> dict[str, object]:
    """Unavailable observations contain identity and typed failure only."""
    return {
        "version": "agileforge.task-repository-evidence.v1",
        "state": "unavailable",
        "repository_binding_id": 2,
        "repository_binding_fingerprint": "sha256:" + "a" * 64,
        "worktree_path": "/example/repository",
        "probed_path_matches_binding": True,
        "uncommitted_acknowledged": True,
        "reason_code": "REPOSITORY_UNAVAILABLE",
        "probe_error_code": "PATH_MISSING",
        "error_summary": "Repository path does not exist.",
    }


def _decode(payload_json: str) -> TaskEvidencePayload:
    return canonical_task_evidence_payload(
        outcome_summary="Implemented execution graph.",
        artifact_refs_json='["workflow/definitions/execution.py"]',
        acceptance_result="fully_met",
        checklist_result_json='{"Run focused tests":"passed"}',
        repository_evidence_json=payload_json,
    )


@pytest.mark.parametrize("state", ["captured", "not_bound", "unavailable"])
def test_repository_evidence_canonical_roundtrip(state: str) -> None:
    """Every closed variant retains the complete immutable observation."""
    payload = (
        _captured_payload()
        if state == "captured"
        else _unavailable_payload()
        if state == "unavailable"
        else {
            "version": "agileforge.task-repository-evidence.v1",
            "state": "not_bound",
            "uncommitted_acknowledged": False,
        }
    )
    evidence = _decode(canonical_json(payload)).repository_evidence
    assert evidence is not None
    assert evidence.model_dump(mode="json") == payload
    with pytest.raises(ValidationError, match="frozen_instance"):
        evidence.uncommitted_acknowledged = False


@pytest.mark.parametrize(
    "changes",
    [
        {"branch_name": None},
        {"detached_head": True},
        {"branch_name": ""},
        {"dirty": False},
        {"dirty_path_count": 1},
        {"dirty_path_count": -1},
        {"dirty_path_count": True},
        {"dirty_path_count": "2"},
        {"dirty_paths": ["tests/test_main.py", "src/main.py"]},
        {"dirty_paths": ["src/main.py", "src/main.py"]},
        {"dirty_paths_truncated": True},
        {"dirty_paths": ["", "tests/test_main.py"]},
        {"head_sha": "short"},
        {"dirty": 1},
        {"detached_head": "false"},
        {"other_worktrees_present": 0},
        {"probed_path_matches_binding": "true"},
        {"uncommitted_acknowledged": "false"},
        {"repository_binding_id": True},
        {"repository_binding_id": 0},
        {"inspected_at": "2026-10-07T12:00:00"},
        {"head_sha": "g" * 40},
        {"extra": "forbidden"},
    ],
)
def test_captured_repository_evidence_rejects_inconsistent_or_coerced_values(
    changes: dict[str, object],
) -> None:
    """Invalid identity, count, branch, and machine values must fail closed."""
    payload = _captured_payload() | changes
    with pytest.raises(ExecutionIntegrityError):
        _decode(canonical_json(payload))


def test_captured_repository_evidence_bounds_distinct_paths() -> None:
    """Truncation preserves fifty sorted distinct paths and the full count."""
    payload = _captured_payload() | {
        "dirty_path_count": 51,
        "dirty_paths": [f"src/file-{index:02}.py" for index in range(50)],
        "dirty_paths_truncated": True,
        "detached_head": True,
        "branch_name": None,
    }
    evidence = _decode(canonical_json(payload)).repository_evidence
    assert evidence is not None
    assert evidence.model_dump(mode="json") == payload
    for changes in (
        {"dirty_paths_truncated": False},
        {"dirty_paths": [f"src/file-{index:02}.py" for index in range(51)]},
        {"dirty_paths": ["src/file-00.py"]},
    ):
        with pytest.raises(ExecutionIntegrityError):
            _decode(canonical_json(payload | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "b" * 40},
        {"dirty": False},
        {"probe_error_code": "RAW_ERROR"},
        {"reason_code": "OTHER"},
        {"error_summary": "x" * 241},
        {"error_summary": "raw\nstderr"},
        {"error_summary": "unsafe\x1b[31m"},
        {"uncommitted_acknowledged": 1},
    ],
)
def test_unavailable_repository_evidence_rejects_invented_or_unsafe_values(
    changes: dict[str, object],
) -> None:
    """A failed probe cannot masquerade as a captured clean revision."""
    with pytest.raises(ExecutionIntegrityError):
        _decode(canonical_json(_unavailable_payload() | changes))


@pytest.mark.parametrize("payload_json", ["{", "null", "[]", "{}"])
def test_repository_evidence_rejects_malformed_or_undiscriminated_json(
    payload_json: str,
) -> None:
    """Stored evidence requires a recognized, complete discriminated object."""
    with pytest.raises(ExecutionIntegrityError):
        _decode(payload_json)


def test_repository_evidence_rejects_noncanonical_json() -> None:
    """Whitespace, key-order changes, and duplicate keys cannot hide tampering."""
    payload = _captured_payload()
    for encoded in (
        json.dumps(payload),
        canonical_json(payload) + "\n",
        canonical_json(payload).replace(
            '"state":"captured"', '"state":"captured","state":"captured"'
        ),
    ):
        with pytest.raises(ExecutionIntegrityError):
            _decode(encoded)


def test_new_repository_payload_is_in_completion_hash(legacy_engine: Engine) -> None:
    """Every observed field, including acknowledgement, changes the old hash."""
    snapshot = _load(legacy_engine)
    task = snapshot.tasks[0]
    payload = _captured_payload()
    recorded = task_evidence_fingerprint(
        snapshot, task, evidence=_decode(canonical_json(payload))
    )
    assert recorded != snapshot.task_completions[0].evidence_fingerprint
    for changes in (
        {"head_sha": "d" * 40},
        {"branch_name": "release"},
        {"detached_head": True, "branch_name": None},
        {"dirty": False, "dirty_path_count": 0, "dirty_paths": []},
        {"dirty_path_count": 1, "dirty_paths": ["src/main.py"]},
        {"repository_binding_id": 3},
        {"repository_binding_fingerprint": "sha256:" + "d" * 64},
        {"worktree_path": "/example/linked"},
        {"common_git_dir": "/example/other/.git"},
        {"status_fingerprint": "sha256:" + "d" * 64},
        {"inspected_at": "2026-10-07T12:00:01Z"},
        {"uncommitted_acknowledged": False},
        {"probed_path_matches_binding": False},
        {"other_worktrees_present": True},
    ):
        changed = task_evidence_fingerprint(
            snapshot, task, evidence=_decode(canonical_json(payload | changes))
        )
        assert changed != recorded


@dataclass(frozen=True)
class _CompletedRecord:
    """An isolated completion with no downstream closure to obscure its error."""

    engine: Engine
    snapshot: WorkflowFactSnapshot
    scope: ExecutionScope
    task: TaskFact
    retry: bool
    binding_id: int
    payload: dict[str, object]


def _row(
    session: Session, *, retry: bool
) -> TaskCompletionEvidence | SprintRetryTaskEvidence:
    return (
        session.exec(select(SprintRetryTaskEvidence)).one()
        if retry
        else session.exec(select(TaskCompletionEvidence)).one()
    )


@pytest.fixture(params=[False, True], ids=["original", "retry"])
def completed_record(
    tmp_path: Path, request: pytest.FixtureRequest
) -> _CompletedRecord:
    """Construct valid recorded evidence without invoking live capture."""
    retry = request.param is True
    if retry:
        engine, domain, project_id, sprint_id, _story_id, task_id = _file_domain(
            tmp_path / "retry.sqlite"
        )
        retry_id = _start_retry(
            engine,
            domain,
            project_id=project_id,
            sprint_id=sprint_id,
            suffix="revision-integrity",
        )
        with Session(engine) as session:
            state = session.exec(select(SprintRetryTaskState)).one()
            state.status = TaskStatus.DONE.value
            session.add(state)
            session.commit()
    else:
        engine = _file_engine(tmp_path / "original.sqlite")
        project_id, sprint_id, _story_id, task_id = seed_started_execution(
            engine, task_status=TaskStatus.DONE
        )
        retry_id = None
    binding = RepositoryBinding(
        project_id=project_id,
        worktree_path=str(tmp_path / "repository"),
        common_git_dir=str(tmp_path / "repository" / ".git"),
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_fingerprint="sha256:" + "e" * 64,
        status_entries_json="[]",
        remotes_json="[]",
        warnings_json="[]",
        probe_version="agileforge.repository-probe.v1",
        inspected_at=datetime(2026, 10, 7, 11, tzinfo=UTC),
        recorded_by="integrity-test",
    )
    with Session(engine) as session:
        session.add(binding)
        session.commit()
        session.refresh(binding)
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        fingerprint = repository_binding_fingerprint(binding)
    snapshot = _load(engine)
    scope = resolve_execution_scope(
        snapshot, sprint_id=sprint_id, retry_attempt_id=retry_id
    )
    task = next(item for item in scope.tasks if item.task_id == task_id)
    payload = _captured_payload() | {
        "repository_binding_id": binding_id,
        "repository_binding_fingerprint": fingerprint,
        "worktree_path": binding.worktree_path,
        "common_git_dir": binding.common_git_dir,
    }
    evidence = _decode(canonical_json(payload))
    fields: dict[str, object] = {
        "project_id": project_id,
        "sprint_id": sprint_id,
        "task_id": task_id,
        "outcome_summary": evidence.outcome_summary,
        "artifact_refs_json": canonical_json(evidence.artifact_refs),
        "acceptance_result": evidence.acceptance_result,
        "checklist_result_json": canonical_json(evidence.checklist_result),
        "evidence_fingerprint": task_evidence_fingerprint(
            snapshot, task, evidence=evidence, scope=scope
        ),
        "repository_evidence_json": canonical_json(payload),
        "completed_by": "integrity-test",
        "completed_at": datetime(2026, 10, 7, 12, tzinfo=UTC),
    }
    row = (
        SprintRetryTaskEvidence(**fields, retry_attempt_id=retry_id)
        if retry
        else TaskCompletionEvidence(**fields)
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
    return _CompletedRecord(engine, snapshot, scope, task, retry, binding_id, payload)


def _rewrite_payload(
    record: _CompletedRecord, payload: dict[str, object], *, rehash: bool = False
) -> None:
    with Session(record.engine) as session:
        row = _row(session, retry=record.retry)
        row.repository_evidence_json = canonical_json(payload)
        if rehash:
            row.evidence_fingerprint = task_evidence_fingerprint(
                record.snapshot,
                record.task,
                evidence=_decode(row.repository_evidence_json),
                scope=record.scope,
            )
        session.add(row)
        session.commit()


def test_valid_captured_original_and_retry_records_load(
    completed_record: _CompletedRecord,
) -> None:
    """Captured live HEAD/status may differ from immutable attachment values."""
    snapshot = _load(completed_record.engine)
    facts = (
        snapshot.sprint_retries[0].task_completions
        if completed_record.retry
        else snapshot.task_completions
    )
    evidence = facts[0].repository_evidence
    assert evidence is not None
    assert evidence.model_dump(mode="json") == completed_record.payload
    nested = snapshot.model_dump(mode="json")
    fact_payload = (
        nested["sprint_retries"][0]["task_completions"][0]
        if completed_record.retry
        else nested["task_completions"][0]
    )
    assert fact_payload["repository_evidence"] == completed_record.payload


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "d" * 40},
        {"uncommitted_acknowledged": False},
        {"dirty_path_count": 1, "dirty_paths": ["src/main.py"]},
        {"repository_binding_id": 999},
        {"branch_name": "tampered"},
    ],
)
def test_captured_payload_tampering_fails_both_readers(
    completed_record: _CompletedRecord, changes: dict[str, object]
) -> None:
    """A valid structural payload still requires its original completion hash."""
    _rewrite_payload(completed_record, completed_record.payload | changes)
    with pytest.raises(WorkflowFactLoadError):
        _load(completed_record.engine)


@pytest.mark.parametrize(
    "changes",
    [
        {"repository_binding_id": 999},
        {"repository_binding_fingerprint": "sha256:" + "f" * 64},
        {"common_git_dir": "/example/unrelated/.git"},
        {"probed_path_matches_binding": False},
    ],
)
def test_historical_binding_mismatch_fails_even_with_recomputed_hash(
    completed_record: _CompletedRecord, changes: dict[str, object]
) -> None:
    """Forging the completion hash cannot bypass retained binding identity."""
    _rewrite_payload(completed_record, completed_record.payload | changes, rehash=True)
    with pytest.raises(WorkflowFactLoadError):
        _load(completed_record.engine)


def test_cross_project_historical_binding_fails_even_with_recomputed_hash(
    completed_record: _CompletedRecord,
) -> None:
    """An otherwise valid binding owned by another Project cannot be reused."""
    with Session(completed_record.engine) as session:
        other_project = Project(name="Other evidence owner")
        session.add(other_project)
        session.flush()
        binding = session.get(RepositoryBinding, completed_record.binding_id)
        assert binding is not None
        values = binding.model_dump(exclude={"repository_binding_id"}) | {
            "project_id": other_project.project_id
        }
        other_binding = RepositoryBinding(**values)
        session.add(other_binding)
        session.commit()
        session.refresh(other_binding)
        payload = completed_record.payload | {
            "repository_binding_id": other_binding.repository_binding_id,
            "repository_binding_fingerprint": repository_binding_fingerprint(
                other_binding
            ),
        }
    _rewrite_payload(completed_record, payload, rehash=True)
    with pytest.raises(WorkflowFactLoadError):
        _load(completed_record.engine)


def test_refreshed_active_binding_preserves_historical_evidence(
    completed_record: _CompletedRecord,
) -> None:
    """Immutable historical evidence remains valid after a new active attachment."""
    with Session(completed_record.engine) as session:
        historical = session.get(RepositoryBinding, completed_record.binding_id)
        assert historical is not None
        values = historical.model_dump(exclude={"repository_binding_id"}) | {
            "head_sha": "f" * 40,
            "status_fingerprint": "sha256:" + "f" * 64,
            "inspected_at": datetime(2026, 10, 7, 13, tzinfo=UTC),
            "supersedes_repository_binding_id": completed_record.binding_id,
        }
        refreshed = RepositoryBinding(**values)
        session.add(refreshed)
        session.flush()
        project = session.get(Project, 1)
        assert project is not None
        project.active_repository_binding_id = refreshed.repository_binding_id
        session.add(project)
        session.commit()
    snapshot = _load(completed_record.engine)
    facts = (
        snapshot.sprint_retries[0].task_completions
        if completed_record.retry
        else snapshot.task_completions
    )
    evidence = facts[0].repository_evidence
    assert evidence is not None
    assert evidence.model_dump(mode="json") == completed_record.payload


def test_linked_worktree_evidence_preserves_common_repository_identity(
    completed_record: _CompletedRecord,
) -> None:
    """A different selected checkout may share the retained common Git directory."""
    payload = completed_record.payload | {
        "worktree_path": "/example/linked",
        "probed_path_matches_binding": False,
    }
    _rewrite_payload(completed_record, payload, rehash=True)
    snapshot = _load(completed_record.engine)
    facts = (
        snapshot.sprint_retries[0].task_completions
        if completed_record.retry
        else snapshot.task_completions
    )
    assert facts[0].repository_evidence is not None
    assert facts[0].repository_evidence.model_dump(mode="json") == payload


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_not_bound_requires_strict_acknowledgement(value: object) -> None:
    """Not-bound evidence records explicit bool intent without coercion."""
    with pytest.raises(ExecutionIntegrityError):
        _decode(
            canonical_json(
                {
                    "version": "agileforge.task-repository-evidence.v1",
                    "state": "not_bound",
                    "uncommitted_acknowledged": value,
                }
            )
        )


def test_not_bound_payload_is_distinct_from_legacy_absence(
    legacy_engine: Engine,
) -> None:
    """An explicit new not-bound completion and its acknowledgement change the hash."""
    snapshot = _load(legacy_engine)
    payload = {
        "version": "agileforge.task-repository-evidence.v1",
        "state": "not_bound",
        "uncommitted_acknowledged": False,
    }
    recorded = task_evidence_fingerprint(
        snapshot, snapshot.tasks[0], evidence=_decode(canonical_json(payload))
    )
    assert recorded != snapshot.task_completions[0].evidence_fingerprint
    acknowledged = task_evidence_fingerprint(
        snapshot,
        snapshot.tasks[0],
        evidence=_decode(canonical_json(payload | {"uncommitted_acknowledged": True})),
    )
    assert acknowledged != recorded
    with pytest.raises(ExecutionIntegrityError):
        _decode(canonical_json(payload | {"dirty": False}))


def test_unavailable_history_validates_retained_binding(
    completed_record: _CompletedRecord,
) -> None:
    """Typed failure history binds the retained identity without probing its path."""
    payload = _unavailable_payload() | {
        key: completed_record.payload[key]
        for key in (
            "repository_binding_id",
            "repository_binding_fingerprint",
            "worktree_path",
            "probed_path_matches_binding",
        )
    }
    _rewrite_payload(completed_record, payload, rehash=True)
    snapshot = _load(completed_record.engine)
    facts = (
        snapshot.sprint_retries[0].task_completions
        if completed_record.retry
        else snapshot.task_completions
    )
    assert facts[0].repository_evidence is not None
    assert facts[0].repository_evidence.model_dump(mode="json") == payload
    _rewrite_payload(
        completed_record,
        payload | {"repository_binding_fingerprint": "sha256:" + "f" * 64},
        rehash=True,
    )
    with pytest.raises(WorkflowFactLoadError):
        _load(completed_record.engine)


def test_changing_retained_binding_row_invalidates_evidence(
    completed_record: _CompletedRecord,
) -> None:
    """An attachment-row mutation cannot silently rewrite completion provenance."""
    with Session(completed_record.engine) as session:
        binding = session.get(RepositoryBinding, completed_record.binding_id)
        assert binding is not None
        binding.head_sha = "f" * 40
        session.add(binding)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        _load(completed_record.engine)


def test_execution_rules_recompute_complete_repository_payload(
    completed_record: _CompletedRecord,
) -> None:
    """New valid evidence keeps the completed Task eligible for Story closure."""
    snapshot = _load(completed_record.engine)
    instance_key = (
        f"retry:{completed_record.scope.retry_attempt_id}:story:{completed_record.task.story_id}"
        if completed_record.retry
        else f"story:{completed_record.task.story_id}"
    )
    position = execution_graph().evaluate(
        snapshot, datetime(2026, 10, 7, 12, tzinfo=UTC)
    )
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.story.close" and item.instance_key == instance_key
    )
    assert decision.reason_code == "STORY_READY_TO_CLOSE"
    assert decision.category.value == "available"
