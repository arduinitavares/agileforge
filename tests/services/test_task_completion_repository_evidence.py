# tests/services/test_task_completion_repository_evidence.py
"""Completion captures server-owned revision evidence without changing Git."""

from __future__ import annotations

import json
import sqlite3
import warnings
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from errno import EACCES
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
from git import Repo
from sqlalchemy import event
from sqlmodel import Session, col, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project, Task
from models.enums import TaskStatus
from models.events import TaskExecutionLog
from models.repository import RepositoryBinding
from models.sprint_retry import SprintRetryTaskEvidence, SprintRetryTaskState
from models.workflow import TaskCompletionEvidence, WorkflowTransitionReceipt
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.task_repository_evidence import (
    CapturedTaskRepositoryEvidence,
    UnavailableTaskRepositoryEvidence,
)
from services.repository_probe import (
    RepositoryProbeError,
    RepositoryProbeErrorCode,
    RepositoryProbeResult,
    RepositoryRevisionProbeResult,
)
from services.task_execution_service import (
    TaskCompletionInput,
    TaskExecutionServiceError,
    complete_task_in_session,
)
from services.task_repository_evidence import (
    TaskRepositoryVerificationTimeout,
    prepare_task_repository_evidence,
    task_repository_warnings,
    verify_task_repository_evidence,
)
from tests.conftest import fresh_test_engine
from tests.workflow import execution_fixtures
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.execution_retry_support import (
    _close_execution_sprint,
    _triage_execution_sprint,
)
from tests.workflow.test_sprint_retry_execution import _start_retry
from workflow.clock import FixedClock
from workflow.definitions.execution import execution_graph
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_json
from workflow.requests import CompleteTask

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.engine import Connection, Engine, ExecutionContext
    from sqlalchemy.engine.interfaces import DBAPICursor

    from services.task_repository_evidence import PreparedTaskRepositoryEvidence

EVALUATED_AT = datetime(2026, 10, 7, 12, tzinfo=UTC)
_MAX_SAFE_SUMMARY: int = 240


@pytest.fixture
def timeout_file_engine(tmp_path: Path) -> Iterator[Engine]:
    """Own a file DB so the timeout test can verify a separate SQLite writer."""
    database_path = tmp_path / "completion-timeout.sqlite3"
    with fresh_test_engine(f"sqlite:///{database_path}") as test_engine:
        yield test_engine


def _repository(tmp_path: Path) -> Path:
    """Create a disposable committed target with local test identity."""
    root = tmp_path / "repository"
    root.mkdir()
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Completion Evidence Test")
            config.set_value("user", "email", "completion@example.com")
        (root / "tracked.txt").write_text("attachment\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        repo.index.commit("attachment")
    return root


def _bind(engine: Engine, project_id: int, root: Path) -> int:
    """Persist the existing probe observation as an immutable active binding."""
    observed = GitPythonRepositoryProbe().inspect(root)
    with Session(engine) as session:
        binding = RepositoryBinding(
            project_id=project_id,
            **observed.model_dump(exclude={"status_entries", "remotes", "warnings"}),
            status_entries_json=canonical_json(
                [entry.model_dump(mode="json") for entry in observed.status_entries]
            ),
            remotes_json=canonical_json(list(observed.remotes)),
            warnings_json=canonical_json(
                [warning.model_dump(mode="json") for warning in observed.warnings]
            ),
            recorded_by="completion-test",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        project = session.get(Project, project_id)
        assert project is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()
        return binding.repository_binding_id


def _domain(engine: Engine, probe: _ObservedProbe | None = None) -> WorkflowDomain:
    """Exercise the real transactional workflow with a deterministic clock."""
    return WorkflowDomain(
        engine=engine,
        graph=execution_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
        _task_repository_probe=probe,
    )


def _request(
    domain: WorkflowDomain, project_id: int, task_id: int, retry_id: int | None = None
) -> CompleteTask:
    """Select the actual guarded completion action."""
    position = domain.position(project_id)
    instance_key = (
        f"task:{task_id}" if retry_id is None else f"retry:{retry_id}:task:{task_id}"
    )
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "execution.task.complete"
        and item.instance_key == instance_key
    )
    assert decision.instance_key is not None
    return CompleteTask(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        instance_key=decision.instance_key,
        idempotency_key="completion-repository-evidence",
        actor="completion@example.com",
        task_id=task_id,
        outcome_summary="Implemented the delivery.",
        artifact_refs=("tracked.txt",),
        acceptance_result="partially_met",
        checklist_result={"Run focused tests": "passed"},
    )


def test_completion_records_current_clean_commit_after_older_binding(
    engine: Engine, tmp_path: Path
) -> None:
    """Omitting live capture would record no revision or the stale binding HEAD."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    binding_id = _bind(engine, project_id, root)
    with Repo(root) as repo:
        attachment_head = repo.head.commit.hexsha
        (root / "tracked.txt").write_text("delivery\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        delivery_head = repo.index.commit("delivery").hexsha
    before = GitPythonRepositoryProbe().inspect(root)
    domain = _domain(engine)

    result = domain.transition(_request(domain, project_id, task_id))

    assert result.ok is True
    with Session(engine) as session:
        row = session.exec(select(TaskCompletionEvidence)).one()
        assert row.repository_evidence_json is not None
        captured = json.loads(row.repository_evidence_json)
        assert captured["state"] == "captured"
        assert captured["repository_binding_id"] == binding_id
        assert captured["head_sha"] == delivery_head
        assert captured["head_sha"] != attachment_head
        assert captured["dirty"] is False
        assert captured["dirty_path_count"] == 0
        assert (
            result.model_dump(mode="json")["output"]["repository_evidence"] == captured
        )
    after = GitPythonRepositoryProbe().inspect(root)
    assert after.model_dump(exclude={"inspected_at"}) == before.model_dump(
        exclude={"inspected_at"}
    )


class _ObservedProbe:
    """Observe real Git I/O and inject one bounded race or typed failure."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = [] if events is None else events
        self.adapter = GitPythonRepositoryProbe()
        self.before_revision: Callable[[], None] | None = None
        self.inspect_error: RepositoryProbeErrorCode | None = None
        self.identity_error: RepositoryProbeErrorCode | None = None
        self.revision_error: RepositoryProbeErrorCode | None = None

    def inspect_common_git_dir(self, path: Path | str) -> str:
        self.events.append("identity")
        if self.identity_error is not None:
            raise RepositoryProbeError(self.identity_error, str(path))
        return self.adapter.inspect_common_git_dir(path)

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        self.events.append("full")
        if self.inspect_error is not None:
            raise RepositoryProbeError(self.inspect_error, str(path))
        return self.adapter.inspect(path)

    def has_other_worktrees(self, path: Path | str) -> bool:
        self.events.append("topology")
        return self.adapter.has_other_worktrees(path)

    def inspect_revision(self, path: Path | str) -> RepositoryRevisionProbeResult:
        self.events.append("cheap")
        if self.before_revision is not None:
            self.before_revision()
        if self.revision_error is not None:
            raise RepositoryProbeError(self.revision_error, str(path))
        return self.adapter.inspect_revision(path)


def _prepared(
    engine: Engine,
    project_id: int,
    probe: _ObservedProbe,
    *,
    selected: Path | None = None,
    acknowledged: bool = False,
) -> PreparedTaskRepositoryEvidence:
    """Prepare explicitly before the caller-owned writer transaction."""
    with Session(engine) as session:
        return prepare_task_repository_evidence(
            session,
            project_id=project_id,
            repository_probe=probe,
            worktree_path=None if selected is None else str(selected),
            uncommitted=acknowledged,
        )


def _stored(engine: Engine) -> dict[str, object]:
    """Read the actual canonical persisted payload."""
    with Session(engine) as session:
        row = session.exec(select(TaskCompletionEvidence)).one()
        assert row.repository_evidence_json is not None
        return json.loads(row.repository_evidence_json)


def test_full_capture_precedes_writer_and_replay_skips_all_git_io(
    engine: Engine, tmp_path: Path
) -> None:
    """Moving capture under the writer lock or probing replay breaks this contract."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    observed: list[str] = []
    probe = _ObservedProbe(observed)
    domain = _domain(engine, probe)
    request = _request(domain, project_id, task_id)

    def record_writer(
        _conn: Connection,
        _cursor: DBAPICursor,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _many: bool,
    ) -> None:
        if statement == "BEGIN IMMEDIATE":
            observed.append("writer")

    event.listen(engine, "before_cursor_execute", record_writer)
    try:
        first = domain.transition(request)
        assert first.ok is True
        assert observed == ["identity", "full", "topology", "writer", "cheap"]
        observed.clear()
        replay = domain.transition(request)
        assert replay.ok is True
        assert replay.replayed is True
        assert replay.output == first.output
        assert observed == []
    finally:
        event.remove(engine, "before_cursor_execute", record_writer)


@pytest.mark.parametrize(("extra_paths", "truncated"), [(0, False), (55, True)])
def test_dirty_paths_are_distinct_sorted_and_bounded(
    engine: Engine, tmp_path: Path, extra_paths: int, *, truncated: bool
) -> None:
    """Counting status entries or failing to truncate corrupts the recorded paths."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    with Repo(root) as repo:
        (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
    (root / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    for number in range(extra_paths):
        (root / f"extra-{number:02}.txt").write_text("extra\n", encoding="utf-8")
    before = GitPythonRepositoryProbe().inspect(root)
    domain = _domain(engine)
    result = domain.transition(_request(domain, project_id, task_id))
    assert result.ok is True
    captured = _stored(engine)
    expected = sorted(
        ["tracked.txt", "untracked.txt"]
        + [f"extra-{number:02}.txt" for number in range(extra_paths)]
    )
    assert captured["dirty"] is True
    assert captured["dirty_path_count"] == 2 + extra_paths
    assert captured["dirty_paths"] == expected[:50]
    assert captured["dirty_paths_truncated"] is truncated
    assert (
        "UNCOMMITTED_WORKTREE"
        in result.model_dump(mode="json")["output"]["repository_warnings"]
    )
    after = GitPythonRepositoryProbe().inspect(root)
    assert before.status_fingerprint == after.status_fingerprint


def test_unbound_completion_records_explicit_state_without_git(engine: Engine) -> None:
    """A new unbound completion must differ from historical absent evidence."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.active_repository_binding_id = None
        session.add(project)
        session.commit()
    probe = _ObservedProbe()
    domain = _domain(engine, probe)
    result = domain.transition(_request(domain, project_id, task_id))
    assert result.ok is True
    assert _stored(engine) == {
        "version": "agileforge.task-repository-evidence.v1",
        "state": "not_bound",
        "uncommitted_acknowledged": False,
    }
    assert probe.events == []
    assert result.output["repository_warnings"] == ("REPOSITORY_NOT_BOUND",)


@pytest.mark.parametrize("failure_phase", ["preparation", "verification"])
def test_typed_probe_failure_becomes_safe_unavailable_evidence(
    engine: Engine, tmp_path: Path, failure_phase: str
) -> None:
    """Treating a failure as clean or propagating arbitrary stderr loses evidence."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    probe = _ObservedProbe()
    if failure_phase == "preparation":
        probe.inspect_error = RepositoryProbeErrorCode.PATH_MISSING
    else:
        probe.revision_error = RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    domain = _domain(engine, probe)
    result = domain.transition(_request(domain, project_id, task_id))
    assert result.ok is True
    captured = _stored(engine)
    assert captured["state"] == "unavailable"
    assert captured["reason_code"] == "REPOSITORY_UNAVAILABLE"
    assert "head_sha" not in captured
    assert "dirty" not in captured
    assert len(str(captured["error_summary"])) <= _MAX_SAFE_SUMMARY
    assert result.output["repository_warnings"] == ("REPOSITORY_UNAVAILABLE",)


@pytest.mark.parametrize("change", ["head", "dirty", "binding", "probe_race"])
def test_observed_revision_or_binding_change_refuses_without_business_writes(
    engine: Engine, tmp_path: Path, change: str
) -> None:
    """Stale prepared HEAD, dirty state, or binding must not bind false evidence."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    probe = _ObservedProbe()
    domain = _domain(engine, probe)
    request = _request(domain, project_id, task_id)
    prepared = _prepared(engine, project_id, probe)
    if change == "head":
        with Repo(root) as repo:
            (root / "tracked.txt").write_text("new revision\n", encoding="utf-8")
            repo.index.add(["tracked.txt"])
            repo.index.commit("changed after preparation")
    elif change == "dirty":
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif change == "binding":
        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.active_repository_binding_id = None
            session.add(project)
            session.commit()
        request = _request(domain, project_id, task_id)
    else:
        probe.revision_error = RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE
    with Session(engine) as session:
        result = domain.transition_in_session(
            session, request, prepared_repository_evidence=prepared
        )
        session.commit()
    assert result.ok is False
    assert result.error is not None
    assert (
        "REPOSITORY_BINDING_CHANGED" in result.error.message
        if change == "binding"
        else ("REPOSITORY_REVISION_CHANGED" in result.error.message)
    )
    assert "new idempotency key" in result.error.message
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.IN_PROGRESS
        assert session.exec(select(TaskCompletionEvidence)).all() == []
        assert (
            len(
                session.exec(
                    select(WorkflowTransitionReceipt).where(
                        col(WorkflowTransitionReceipt.request_kind) == "complete_task"
                    )
                ).all()
            )
            == 1
        )


def test_linked_dirty_target_and_clean_bound_topology_are_distinct(
    engine: Engine, tmp_path: Path
) -> None:
    """Inspecting only the main checkout or scanning other status confuses targets."""
    project_id, _sprint_id, _story_id, _task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    linked = tmp_path / "linked"
    with Repo(root) as repo:
        repo.git.worktree("add", "--detach", str(linked), "HEAD")
    (linked / "untracked.txt").write_text("linked only\n", encoding="utf-8")
    probe = _ObservedProbe()
    default = _prepared(engine, project_id, probe)
    explicit = _prepared(engine, project_id, probe, selected=linked)
    assert default.evidence is not None
    assert explicit.evidence is not None
    assert isinstance(default.evidence, CapturedTaskRepositoryEvidence)
    assert isinstance(explicit.evidence, CapturedTaskRepositoryEvidence)
    assert default.evidence.dirty is False
    assert default.evidence.other_worktrees_present is True
    assert explicit.evidence.dirty is True
    assert explicit.evidence.detached_head is True
    assert explicit.evidence.worktree_path == str(linked.resolve())
    assert explicit.evidence.probed_path_matches_binding is False
    assert explicit.evidence.other_worktrees_present is False
    assert probe.events == ["identity", "full", "topology", "identity", "full"]


@pytest.mark.parametrize("foreign_state", ["unborn", "parser_failure"])
def test_foreign_identity_refuses_before_full_capture_failure(
    engine: Engine,
    tmp_path: Path,
    foreign_state: str,
) -> None:
    """Full metadata failure must not conceal an observable foreign identity."""
    project_id, _sprint_id, _story_id, _task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    parent = tmp_path / "foreign-identity"
    parent.mkdir()
    if foreign_state == "unborn":
        foreign = parent / "repository"
        with Repo.init(foreign):
            pass
    else:
        foreign = _repository(parent)
    probe = _ObservedProbe()
    if foreign_state == "parser_failure":
        probe.inspect_error = RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    prepared = _prepared(engine, project_id, probe, selected=foreign, acknowledged=True)
    assert prepared.refusal is not None
    assert prepared.refusal.rule == "WORKTREE_REPOSITORY_MISMATCH"
    assert "new idempotency key" in prepared.refusal.message
    assert probe.events == ["identity"]


@pytest.mark.parametrize("failed_phase", ["identity", "full"])
def test_same_repository_metadata_failure_remains_unavailable(
    engine: Engine,
    tmp_path: Path,
    failed_phase: str,
) -> None:
    """Unreadable or same-repository metadata is unavailable, not a known mismatch."""
    project_id, _sprint_id, _story_id, _task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    probe = _ObservedProbe()
    if failed_phase == "identity":
        probe.identity_error = RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    else:
        probe.inspect_error = RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    prepared = _prepared(engine, project_id, probe, selected=root, acknowledged=True)
    assert prepared.refusal is None
    assert isinstance(prepared.evidence, UnavailableTaskRepositoryEvidence)
    assert prepared.evidence.probe_error_code.value == "WORKTREE_PATH_UNUSABLE"
    assert (
        prepared.evidence.error_summary
        == "The selected worktree path could not be used."
    )
    assert prepared.evidence.uncommitted_acknowledged is True
    assert probe.events == (
        ["identity"] if failed_phase == "identity" else ["identity", "full"]
    )


@pytest.mark.parametrize("bound", [False, True])
def test_explicit_unverifiable_target_refusal_is_durable(
    engine: Engine, tmp_path: Path, *, bound: bool
) -> None:
    """Bypassing the receipt for outside mismatches breaks refusal replay."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    if bound:
        bound_parent = tmp_path / "bound"
        bound_parent.mkdir()
        _bind(engine, project_id, _repository(bound_parent))
    else:
        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.active_repository_binding_id = None
            session.add(project)
            session.commit()
    unrelated_parent = tmp_path / "unrelated"
    unrelated_parent.mkdir()
    unrelated = _repository(unrelated_parent)
    probe = _ObservedProbe()
    domain = _domain(engine, probe)
    request = _request(domain, project_id, task_id)
    prepared = _prepared(
        engine, project_id, probe, selected=unrelated, acknowledged=True
    )
    with Session(engine) as session:
        result = domain.transition_in_session(
            session, request, prepared_repository_evidence=prepared
        )
        session.commit()
    assert result.ok is False
    assert result.error is not None
    rule = "WORKTREE_REPOSITORY_MISMATCH" if bound else "WORKTREE_REQUIRES_BINDING"
    assert rule in result.error.message
    assert "new idempotency key" in result.error.message
    probe.events.clear()
    replay = domain.transition(request)
    assert replay.replayed is True
    assert replay.error == result.error
    assert probe.events == []
    with Session(engine) as session:
        assert session.exec(select(TaskCompletionEvidence)).all() == []
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.IN_PROGRESS


def test_bound_direct_service_requires_preparation_without_full_scan(
    engine: Engine, tmp_path: Path
) -> None:
    """Direct callers must not trigger full capture while holding their writer lock."""
    project_id, sprint_id, _story_id, task_id = seed_started_execution(engine)
    _bind(engine, project_id, _repository(tmp_path))
    command = TaskCompletionInput(
        project_id=project_id,
        sprint_id=sprint_id,
        task_id=task_id,
        outcome_summary="Done.",
        artifact_refs=("tracked.txt",),
        acceptance_result="partially_met",
        checklist_result={"Run focused tests": "passed"},
        completed_by="completion@example.com",
        completed_at=EVALUATED_AT,
    )
    with Session(engine) as session, pytest.raises(TaskExecutionServiceError) as caught:
        complete_task_in_session(session, command)
    assert "REPOSITORY_EVIDENCE_PREPARATION_REQUIRED" in caught.value.detail


def test_retry_completion_captures_its_own_current_revision(
    engine: Engine, tmp_path: Path
) -> None:
    """A retry must persist a new capture without replacing original history."""
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    original_domain = _domain(engine)
    _close_execution_sprint(
        original_domain,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    _triage_execution_sprint(
        original_domain, project_id=project_id, sprint_id=sprint_id
    )
    with Session(engine) as session:
        original = session.exec(select(TaskCompletionEvidence)).one()
        original_values = original.model_dump()
    domain = WorkflowDomain(
        engine=engine, graph=project_graph(), clock=FixedClock(now_value=EVALUATED_AT)
    )
    retry_id = _start_retry(
        engine, domain, project_id=project_id, sprint_id=sprint_id, suffix="revision"
    )
    with Repo(root) as repo:
        (root / "tracked.txt").write_text("retry delivery\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        retry_head = repo.index.commit("retry delivery").hexsha
    result = domain.transition(_request(domain, project_id, task_id, retry_id))
    assert result.ok is True
    with Session(engine) as session:
        retry = session.exec(select(SprintRetryTaskEvidence)).one()
        assert retry.repository_evidence_json is not None
        assert json.loads(retry.repository_evidence_json)["head_sha"] == retry_head
        assert (
            result.output["sprint_retry_task_evidence_id"]
            == retry.sprint_retry_task_evidence_id
        )
        assert (
            session.exec(select(TaskCompletionEvidence)).one().model_dump()
            == original_values
        )
        snapshot = WorkflowFactRepository(session).load(project_id)
        assert (
            snapshot.sprint_retries[0].task_completions[0].repository_evidence
            is not None
        )


def test_preparation_head_race_refusal_uses_durable_receipt(
    engine: Engine, tmp_path: Path
) -> None:
    """A preparation race must be refused durably rather than treated as unavailable."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    _bind(engine, project_id, _repository(tmp_path))
    probe = _ObservedProbe()
    probe.inspect_error = RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE
    domain = _domain(engine, probe)
    request = _request(domain, project_id, task_id)
    first = domain.transition(request)
    assert first.ok is False
    assert first.error is not None
    assert "REPOSITORY_REVISION_CHANGED" in first.error.message
    probe.events.clear()
    replay = domain.transition(request)
    assert replay.replayed is True
    assert replay.error == first.error
    assert probe.events == []


@pytest.mark.parametrize("selected", ["missing", "file", "non_git", "malformed"])
def test_selected_unreachable_path_has_safe_typed_unavailable_evidence(
    engine: Engine, tmp_path: Path, selected: str
) -> None:
    """Unreachable targets must preserve the selected path without fabricated HEAD."""
    project_id, _sprint_id, _story_id, _task_id = seed_started_execution(engine)
    _bind(engine, project_id, _repository(tmp_path))
    target = tmp_path / "selected-target"
    if selected == "file":
        target.write_text("ordinary file\n", encoding="utf-8")
    elif selected == "non_git":
        target.mkdir()
    with Session(engine) as session:
        prepared = prepare_task_repository_evidence(
            session,
            project_id=project_id,
            repository_probe=GitPythonRepositoryProbe(),
            worktree_path="\0" if selected == "malformed" else str(target),
            uncommitted=True,
        )
    assert prepared.evidence is not None
    assert isinstance(prepared.evidence, UnavailableTaskRepositoryEvidence)
    assert prepared.evidence.probe_error_code.value == "WORKTREE_PATH_UNUSABLE"
    assert (
        prepared.evidence.error_summary
        == "The selected worktree path could not be used."
    )
    assert prepared.evidence.uncommitted_acknowledged is True
    assert prepared.evidence.probed_path_matches_binding is False


def _policy_command(
    engine: Engine, tmp_path: Path, *, retry: bool = False
) -> tuple[TaskCompletionInput, Path, _ObservedProbe]:
    """Open a real original/retry scope against one disposable clean binding."""
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    retry_id: int | None = None
    if retry:
        original_domain = _domain(engine)
        _close_execution_sprint(
            original_domain,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
            task_id=task_id,
        )
        _triage_execution_sprint(
            original_domain, project_id=project_id, sprint_id=sprint_id
        )
        domain = WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=EVALUATED_AT),
        )
        retry_id = _start_retry(
            engine, domain, project_id=project_id, sprint_id=sprint_id, suffix="policy"
        )
    command = TaskCompletionInput(
        project_id=project_id,
        sprint_id=sprint_id,
        task_id=task_id,
        outcome_summary="Delivered with explicit repository policy.",
        artifact_refs=("tracked.txt",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
        completed_by="completion@example.com",
        completed_at=EVALUATED_AT,
        retry_attempt_id=retry_id,
    )
    return command, root, _ObservedProbe()


def _policy_observation(root: Path, probe: _ObservedProbe, state: str) -> None:
    """Set the actual dirty target or inject a closed failure at the Git boundary."""
    if state == "dirty":
        (root / "tracked.txt").write_text("uncommitted delivery\n", encoding="utf-8")
    elif state == "unavailable_preparation":
        probe.inspect_error = RepositoryProbeErrorCode.PATH_MISSING
    else:
        probe.revision_error = RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE


def _completion_business_state(engine: Engine) -> dict[str, object]:
    """Snapshot all rows the shared completion service is allowed to mutate."""
    with Session(engine) as session:
        return {
            model.__name__: [
                row.model_dump() for row in session.exec(select(model)).all()
            ]
            for model in (
                Task,
                TaskCompletionEvidence,
                TaskExecutionLog,
                SprintRetryTaskState,
                SprintRetryTaskEvidence,
            )
        }


def _deny_target_preflight(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_check: Literal["exists", "is_dir"],
) -> None:
    """Fail only the selected target's real filesystem preflight operation."""
    target = root.resolve()
    original_check = getattr(Path, path_check)

    def denied_check(path: Path) -> bool:
        if path == target:
            raise PermissionError(EACCES, "filesystem preflight denied", str(target))
        return original_check(path)

    monkeypatch.setattr(Path, path_check, denied_check)


@pytest.mark.parametrize("failure_phase", ["preparation", "verification"])
@pytest.mark.parametrize("path_check", ["exists", "is_dir"])
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_filesystem_preflight_failure_persists_typed_completion_evidence(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    path_check: Literal["exists", "is_dir"],
    *,
    retry: bool,
    explicit: bool,
) -> None:
    """A raw filesystem denial must reach original/retry persistence as unavailable."""
    command, root, probe = _policy_command(engine, tmp_path, retry=retry)
    command = replace(command, acceptance_result="partially_met")
    if failure_phase == "preparation":
        _deny_target_preflight(root, monkeypatch, path_check)
    prepared = _prepared(
        engine, command.project_id, probe, selected=root if explicit else None
    )
    if failure_phase == "verification":
        assert isinstance(prepared.evidence, CapturedTaskRepositoryEvidence)
        _deny_target_preflight(root, monkeypatch, path_check)
    original_before = _completion_business_state(engine)["TaskCompletionEvidence"]
    with Session(engine) as session:
        row = complete_task_in_session(
            session,
            command,
            prepared_repository_evidence=prepared,
            repository_probe=probe,
        )
        session.commit()
        session.refresh(row)
        evidence = json.loads(row.repository_evidence_json or "null")
        assert evidence["state"] == "unavailable"
        assert evidence["probe_error_code"] == (
            "WORKTREE_PATH_UNUSABLE" if explicit else "GIT_METADATA_UNREADABLE"
        )
        assert evidence["error_summary"] == (
            "The selected worktree path could not be used."
            if explicit
            else "Git metadata could not be read."
        )
        assert evidence["worktree_path"] == str(root)
        assert evidence["uncommitted_acknowledged"] is False
        assert "head_sha" not in evidence
        facts = WorkflowFactRepository(session).load(command.project_id)
        completions = (
            facts.sprint_retries[0].task_completions
            if retry
            else facts.task_completions
        )
        stored = completions[0].repository_evidence
        assert stored is not None
        assert stored.model_dump(mode="json") == evidence
    if retry:
        assert (
            _completion_business_state(engine)["TaskCompletionEvidence"]
            == original_before
        )


@pytest.mark.parametrize(
    ("probe_error", "legacy_summary"),
    [
        (RepositoryProbeErrorCode.PATH_MISSING, "Repository path does not exist."),
        (
            RepositoryProbeErrorCode.PATH_NOT_DIRECTORY,
            "Repository path is not a directory.",
        ),
        (
            RepositoryProbeErrorCode.NOT_GIT_WORKTREE,
            "Repository path is not a Git worktree.",
        ),
        (
            RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
            "Git metadata could not be read.",
        ),
        (RepositoryProbeErrorCode.MALFORMED_PATH, "Repository path is malformed."),
    ],
)
@pytest.mark.parametrize("failure_phase", ["preparation", "verification"])
@pytest.mark.parametrize(
    ("retry", "explicit"), [(False, False), (False, True), (True, True)]
)
def test_unusable_explicit_target_redacts_original_and_retry_evidence(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    probe_error: RepositoryProbeErrorCode,
    legacy_summary: str,
    failure_phase: str,
    *,
    retry: bool,
    explicit: bool,
) -> None:
    """Path equality or a later failure must not reveal an explicit target's subtype."""
    command, root, probe = _policy_command(engine, tmp_path, retry=retry)
    command = replace(command, acceptance_result="partially_met")
    if failure_phase == "preparation":
        probe.identity_error = probe_error
    prepared = _prepared(
        engine, command.project_id, probe, selected=root if explicit else None
    )
    if failure_phase == "verification":
        assert isinstance(prepared.evidence, CapturedTaskRepositoryEvidence)
        probe.revision_error = probe_error
    original_before = _completion_business_state(engine)["TaskCompletionEvidence"]
    with Session(engine) as session:
        evidence = complete_task_in_session(
            session,
            command,
            prepared_repository_evidence=prepared,
            repository_probe=probe,
        )
        session.commit()
        session.refresh(evidence)
        assert evidence.repository_evidence_json is not None
        captured = json.loads(evidence.repository_evidence_json)
        assert set(captured) == {
            "version",
            "state",
            "repository_binding_id",
            "repository_binding_fingerprint",
            "worktree_path",
            "probed_path_matches_binding",
            "uncommitted_acknowledged",
            "reason_code",
            "probe_error_code",
            "error_summary",
        }
        assert captured["state"] == "unavailable"
        assert captured["worktree_path"] == str(root)
        assert captured["probed_path_matches_binding"] is True
        assert captured["uncommitted_acknowledged"] is False
        assert captured["probe_error_code"] == (
            "WORKTREE_PATH_UNUSABLE" if explicit else probe_error.value
        )
        expected_summary = (
            "The selected worktree path could not be used."
            if explicit
            else legacy_summary
        )
        assert captured["error_summary"] == expected_summary
        snapshot = WorkflowFactRepository(session).load(command.project_id)
        completions = (
            snapshot.sprint_retries[0].task_completions
            if retry
            else snapshot.task_completions
        )
        stored = completions[0].repository_evidence
        assert stored is not None
        assert stored.model_dump(mode="json") == captured
        assert task_repository_warnings(stored) == (
            ["REPOSITORY_UNAVAILABLE"],
            [
                "Repository evidence was unavailable at Task completion: "
                + expected_summary
            ],
        )
    if retry:
        assert (
            _completion_business_state(engine)["TaskCompletionEvidence"]
            == original_before
        )


@pytest.mark.parametrize("failure_phase", ["preparation", "verification"])
@pytest.mark.parametrize(
    ("probe_error", "summary"),
    [
        (
            RepositoryProbeErrorCode.UNBORN_HEAD,
            "Repository HEAD does not reference a commit.",
        ),
        (RepositoryProbeErrorCode.PROBE_TIMED_OUT, "Repository probe timed out."),
    ],
)
def test_validated_explicit_target_retains_bound_git_state_and_timeout(
    engine: Engine,
    tmp_path: Path,
    failure_phase: str,
    probe_error: RepositoryProbeErrorCode,
    summary: str,
) -> None:
    """Known bound Git state stays typed; inside timeout must still require rollback."""
    command, root, probe = _policy_command(engine, tmp_path)
    if failure_phase == "preparation":
        probe.inspect_error = probe_error
    prepared = _prepared(engine, command.project_id, probe, selected=root)
    if failure_phase == "verification":
        probe.revision_error = probe_error
    with Session(engine) as session:
        if (
            failure_phase == "verification"
            and probe_error is RepositoryProbeErrorCode.PROBE_TIMED_OUT
        ):
            with pytest.raises(
                TaskRepositoryVerificationTimeout,
                match="REPOSITORY_VERIFICATION_TIMEOUT",
            ) as caught:
                verify_task_repository_evidence(
                    session,
                    project_id=command.project_id,
                    prepared=prepared,
                    repository_probe=probe,
                )
            assert caught.value.retryable is True
            return
        observed = verify_task_repository_evidence(
            session,
            project_id=command.project_id,
            prepared=prepared,
            repository_probe=probe,
        )
    assert isinstance(observed, UnavailableTaskRepositoryEvidence)
    assert observed.probe_error_code is probe_error
    assert observed.error_summary == summary


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize(
    ("state", "rule"),
    [
        ("dirty", "UNCOMMITTED_ACKNOWLEDGEMENT_REQUIRED"),
        ("unavailable_preparation", "REPOSITORY_UNAVAILABLE_ACKNOWLEDGEMENT_REQUIRED"),
        ("unavailable_verification", "REPOSITORY_UNAVAILABLE_ACKNOWLEDGEMENT_REQUIRED"),
    ],
)
def test_full_acceptance_requires_ack_before_original_or_retry_writes(
    engine: Engine, tmp_path: Path, state: str, rule: str, *, retry: bool
) -> None:
    """Skipping shared policy must fail before changing any completion business row."""
    command, root, probe = _policy_command(engine, tmp_path, retry=retry)
    _policy_observation(root, probe, state)
    prepared = _prepared(engine, command.project_id, probe)
    before = _completion_business_state(engine)
    with Session(engine) as session:
        with pytest.raises(TaskExecutionServiceError) as caught:
            complete_task_in_session(
                session,
                command,
                prepared_repository_evidence=prepared,
                repository_probe=probe,
            )
        session.commit()
    assert caught.value.status_code == HTTPStatus.CONFLICT
    assert rule in caught.value.detail
    assert "A changed request requires a new idempotency key." in caught.value.detail
    assert _completion_business_state(engine) == before


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize(
    "state", ["dirty", "unavailable_preparation", "unavailable_verification"]
)
@pytest.mark.parametrize("acknowledged", [False, True])
def test_partial_without_ack_and_full_with_ack_persist_exact_intent(
    engine: Engine, tmp_path: Path, state: str, *, retry: bool, acknowledged: bool
) -> None:
    """Overblocking partials or dropping caller acknowledgement loses valid evidence."""
    command, root, probe = _policy_command(engine, tmp_path, retry=retry)
    if acknowledged:
        command = replace(command, uncommitted=True)
    else:
        command = replace(command, acceptance_result="partially_met")
    _policy_observation(root, probe, state)
    prepared = _prepared(engine, command.project_id, probe, acknowledged=acknowledged)
    before = _completion_business_state(engine)
    with Session(engine) as session:
        evidence = complete_task_in_session(
            session,
            command,
            prepared_repository_evidence=prepared,
            repository_probe=probe,
        )
        session.commit()
        session.refresh(evidence)
        assert evidence.repository_evidence_json is not None
        captured = json.loads(evidence.repository_evidence_json)
        assert captured["uncommitted_acknowledged"] is acknowledged
        assert captured["state"] == ("captured" if state == "dirty" else "unavailable")
        if state == "dirty":
            assert captured["dirty"] is True
            assert captured["dirty_paths"] == ["tracked.txt"]
        snapshot = WorkflowFactRepository(session).load(command.project_id)
        completions = (
            snapshot.sprint_retries[0].task_completions
            if retry
            else snapshot.task_completions
        )
        assert completions[0].repository_evidence is not None
        assert (
            completions[0].repository_evidence.uncommitted_acknowledged is acknowledged
        )
        codes, messages = task_repository_warnings(completions[0].repository_evidence)
        assert codes == [
            "UNCOMMITTED_WORKTREE" if state == "dirty" else "REPOSITORY_UNAVAILABLE"
        ]
        assert messages
        task = session.get(Task, command.task_id)
        assert task is not None
        assert task.status is TaskStatus.DONE
        if retry:
            assert (
                _completion_business_state(engine)["TaskCompletionEvidence"]
                == before["TaskCompletionEvidence"]
            )
        if acknowledged:
            # Changing only acknowledgement must invalidate the same fingerprint.
            evidence.repository_evidence_json = canonical_json(
                captured | {"uncommitted_acknowledged": False}
            )
            session.add(evidence)
            session.commit()
            with pytest.raises(WorkflowFactLoadError):
                WorkflowFactRepository(session).load(command.project_id)


def test_clean_detached_full_acceptance_without_ack_warns(
    engine: Engine, tmp_path: Path
) -> None:
    """Treating detached HEAD as unavailable would reject a valid clean revision."""
    command, root, probe = _policy_command(engine, tmp_path)
    with Repo(root) as repo:
        repo.git.checkout("--detach")
    prepared = _prepared(engine, command.project_id, probe)
    with Session(engine) as session:
        evidence = complete_task_in_session(
            session,
            command,
            prepared_repository_evidence=prepared,
            repository_probe=probe,
        )
        session.commit()
        assert evidence.repository_evidence_json is not None
        captured = json.loads(evidence.repository_evidence_json)
        assert captured["detached_head"] is True
        assert captured["branch_name"] is None
        assert captured["dirty"] is False
        assert captured["uncommitted_acknowledged"] is False
        assert prepared.evidence is not None
        assert task_repository_warnings(prepared.evidence)[0] == ["DETACHED_HEAD"]


@pytest.mark.parametrize("acknowledged", [False, True])
def test_prepared_ack_must_agree_with_command_in_both_directions(
    engine: Engine, tmp_path: Path, *, acknowledged: bool
) -> None:
    """Server preparation cannot grant intent absent from the completion command."""
    command, _root, probe = _policy_command(engine, tmp_path)
    command = replace(command, uncommitted=acknowledged)
    prepared = _prepared(
        engine, command.project_id, probe, acknowledged=not acknowledged
    )
    before = _completion_business_state(engine)
    with Session(engine) as session:
        with pytest.raises(TaskExecutionServiceError) as caught:
            complete_task_in_session(
                session,
                command,
                prepared_repository_evidence=prepared,
                repository_probe=probe,
            )
        session.commit()
    assert "UNCOMMITTED_ACKNOWLEDGEMENT_MISMATCH" in caught.value.detail
    assert _completion_business_state(engine) == before


@pytest.mark.parametrize("value", [1, 0, "true", "false", None, [], {}])
def test_direct_service_ack_rejects_non_bool_without_writes(
    engine: Engine, tmp_path: Path, value: object
) -> None:
    """Truthy/falsy coercion must not grant or persist malformed acknowledgement."""
    command, _root, probe = _policy_command(engine, tmp_path)
    prepared = _prepared(engine, command.project_id, probe)
    before = _completion_business_state(engine)
    malformed = replace(command, uncommitted=cast("bool", value))
    with Session(engine) as session:
        with pytest.raises(TaskExecutionServiceError) as caught:
            complete_task_in_session(
                session,
                malformed,
                prepared_repository_evidence=prepared,
                repository_probe=probe,
            )
        session.commit()
    assert "UNCOMMITTED_ACKNOWLEDGEMENT_INVALID" in caught.value.detail
    assert _completion_business_state(engine) == before


@pytest.mark.parametrize("prepared", [False, True])
def test_unbound_full_acceptance_preserves_ack_intent(
    engine: Engine, tmp_path: Path, *, prepared: bool
) -> None:
    """The direct unbound fallback must record true intent instead of resetting it."""
    command, _root, probe = _policy_command(engine, tmp_path)
    command = replace(command, uncommitted=True)
    with Session(engine) as session:
        project = session.get(Project, command.project_id)
        assert project is not None
        project.active_repository_binding_id = None
        session.add(project)
        session.commit()
    observation = (
        _prepared(engine, command.project_id, probe, acknowledged=True)
        if prepared
        else None
    )
    with Session(engine) as session:
        evidence = complete_task_in_session(
            session,
            command,
            prepared_repository_evidence=observation,
            repository_probe=probe,
        )
        session.commit()
        assert evidence.repository_evidence_json is not None
        assert json.loads(evidence.repository_evidence_json) == {
            "version": "agileforge.task-repository-evidence.v1",
            "state": "not_bound",
            "uncommitted_acknowledged": True,
        }


@pytest.mark.parametrize("invalid", ["outcome", "artifacts", "checklist"])
def test_ack_does_not_bypass_existing_completion_rules(
    engine: Engine, tmp_path: Path, invalid: str
) -> None:
    """An explicit acknowledgement must only relax repository acceptance policy."""
    command, root, probe = _policy_command(engine, tmp_path)
    command = replace(command, uncommitted=True)
    _policy_observation(root, probe, "dirty")
    if invalid == "outcome":
        command = replace(command, outcome_summary="  ")
    elif invalid == "artifacts":
        with Session(engine) as session:
            task = session.get(Task, command.task_id)
            assert task is not None
            assert task.metadata_json is not None
            metadata = json.loads(task.metadata_json)
            metadata["artifact_targets"] = ["tracked.txt"]
            task.metadata_json = canonical_json(metadata)
            session.add(task)
            session.commit()
        command = replace(command, artifact_refs=())
    else:
        command = replace(command, checklist_result={})
    prepared = _prepared(engine, command.project_id, probe, acknowledged=True)
    before = _completion_business_state(engine)
    with Session(engine) as session:
        with pytest.raises(TaskExecutionServiceError) as caught:
            complete_task_in_session(
                session,
                command,
                prepared_repository_evidence=prepared,
                repository_probe=probe,
            )
        session.commit()
    expected = {
        "outcome": "requires an outcome summary",
        "artifacts": "requires artifact references",
        "checklist": "cover every executable checklist item",
    }
    assert expected[invalid] in caught.value.detail
    assert _completion_business_state(engine) == before


@pytest.mark.parametrize("change", ["head", "dirty", "binding", "common_dir"])
def test_ack_does_not_bypass_repository_identity_or_revision_change(
    engine: Engine, tmp_path: Path, change: str
) -> None:
    """Acknowledgement must preserve the independent binding/membership/race guards."""
    command, root, probe = _policy_command(engine, tmp_path)
    command = replace(command, uncommitted=True)
    selected = None
    if change == "common_dir":
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        selected = _repository(unrelated)
    prepared = _prepared(
        engine, command.project_id, probe, selected=selected, acknowledged=True
    )
    if change == "head":
        with Repo(root) as repo:
            (root / "tracked.txt").write_text("later commit\n", encoding="utf-8")
            repo.index.add(["tracked.txt"])
            repo.index.commit("later delivery")
    elif change == "dirty":
        (root / "tracked.txt").write_text("late dirty state\n", encoding="utf-8")
    elif change == "binding":
        with Session(engine) as session:
            project = session.get(Project, command.project_id)
            assert project is not None
            project.active_repository_binding_id = None
            session.add(project)
            session.commit()
    before = _completion_business_state(engine)
    with Session(engine) as session:
        with pytest.raises(TaskExecutionServiceError) as caught:
            complete_task_in_session(
                session,
                command,
                prepared_repository_evidence=prepared,
                repository_probe=probe,
            )
        session.commit()
    rule = {
        "head": "REPOSITORY_REVISION_CHANGED",
        "dirty": "REPOSITORY_REVISION_CHANGED",
        "binding": "REPOSITORY_BINDING_CHANGED",
        "common_dir": "WORKTREE_REPOSITORY_MISMATCH",
    }[change]
    assert rule in caught.value.detail
    assert _completion_business_state(engine) == before


def test_missing_ack_full_refusal_is_durable_and_replays_without_git(
    engine: Engine, tmp_path: Path
) -> None:
    """Policy refusal must use the established receipt path and never auto-resubmit."""
    command, root, probe = _policy_command(engine, tmp_path)
    _policy_observation(root, probe, "dirty")
    domain = _domain(engine, probe)
    request = _request(domain, command.project_id, command.task_id).model_copy(
        update={"acceptance_result": "fully_met"},
    )
    before = _completion_business_state(engine)
    result = domain.transition(request)
    assert result.ok is False
    assert result.error is not None
    assert "UNCOMMITTED_ACKNOWLEDGEMENT_REQUIRED" in result.error.message
    assert "A changed request requires a new idempotency key." in result.error.message
    assert _completion_business_state(engine) == before
    with Repo(root) as repo:
        repo.git.checkout("--", "tracked.txt")
    probe.events.clear()
    replay = domain.transition(request)
    assert replay.replayed is True
    assert replay.error == result.error
    assert probe.events == []


def test_execution_fixture_unbinds_only_synthetic_target_and_retains_lineage(
    engine: Engine, tmp_path: Path
) -> None:
    """Unbinding fake targets must preserve historical and real bindings."""
    project_id, _sprint_id, _story_id, _task_id = seed_started_execution(engine)
    with Session(engine) as session:
        project = session.get_one(Project, project_id)
        assert project.active_repository_binding_id is None
        synthetic = session.exec(select(RepositoryBinding)).one()
        assert synthetic.worktree_path == "repository"
        historical_binding = synthetic.model_dump()
        assert WorkflowFactRepository(session).load(project_id).sprints
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        execution_fixtures.unbind_synthetic_execution_repository(engine, project_id)
    assert observed == []
    real_binding_id = _bind(engine, project_id, _repository(tmp_path))
    execution_fixtures.unbind_synthetic_execution_repository(engine, project_id)
    with Session(engine) as session:
        project = session.get_one(Project, project_id)
        assert project.active_repository_binding_id == real_binding_id
        assert (
            session.get_one(
                RepositoryBinding, synthetic.repository_binding_id
            ).model_dump()
            == historical_binding
        )


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_verification_timeout_rolls_back_claim_and_allows_unchanged_key_retry(
    timeout_file_engine: Engine,
    tmp_path: Path,
    *,
    retry: bool,
    acknowledged: bool,
) -> None:
    """Timeout must release the SQLite writer without persisting a refusal claim."""
    engine = timeout_file_engine
    command, _root, probe = _policy_command(engine, tmp_path, retry=retry)
    domain = _domain(engine, probe)
    request = _request(
        domain, command.project_id, command.task_id, command.retry_attempt_id
    ).model_copy(
        update={
            "uncommitted": acknowledged,
            "acceptance_result": "fully_met" if acknowledged else "partially_met",
        },
    )
    before = _completion_business_state(engine)
    with Session(engine) as session:
        receipts_before = [
            row.model_dump()
            for row in session.exec(select(WorkflowTransitionReceipt)).all()
        ]
    probe.revision_error = RepositoryProbeErrorCode.PROBE_TIMED_OUT
    result = domain.transition(request)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "WORKFLOW_FACT_CONFLICT"
    assert "REPOSITORY_VERIFICATION_TIMEOUT" in result.error.message
    assert "unchanged request with the same idempotency key" in result.error.message
    assert "A changed request requires a new idempotency key." in result.error.message
    assert _completion_business_state(engine) == before
    with Session(engine) as session:
        assert [
            row.model_dump()
            for row in session.exec(select(WorkflowTransitionReceipt)).all()
        ] == receipts_before
        primary_connection = session.connection().connection.driver_connection
    assert engine.url.database is not None
    with closing(sqlite3.connect(engine.url.database, timeout=0)) as second_writer:
        assert second_writer is not primary_connection
        assert (
            second_writer.execute("PRAGMA database_list").fetchone()[2]
            == engine.url.database
        )
        second_writer.execute("BEGIN IMMEDIATE")
        second_writer.rollback()
    probe.revision_error = None
    successful = domain.transition(request)
    assert successful.ok is True
    assert successful.replayed is False
    assert domain.transition(request).replayed is True


def test_direct_completion_timeout_remains_a_typed_retryable_exception(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """A caller-owned transaction must receive the signal that requires rollback."""
    command, _root, probe = _policy_command(engine, tmp_path)
    command = replace(command, uncommitted=True)
    prepared = _prepared(engine, command.project_id, probe, acknowledged=True)
    probe.revision_error = RepositoryProbeErrorCode.PROBE_TIMED_OUT
    before = _completion_business_state(engine)
    with Session(engine) as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError) as caught:
            complete_task_in_session(
                session,
                command,
                prepared_repository_evidence=prepared,
                repository_probe=probe,
            )
        assert type(caught.value).__name__ == "TaskRepositoryVerificationTimeout"
        assert getattr(caught.value, "retryable", False) is True
        session.rollback()
    assert _completion_business_state(engine) == before


@pytest.mark.parametrize("phase", ["identity", "full", "topology"])
@pytest.mark.parametrize(
    ("acceptance", "acknowledged", "allowed"),
    [
        ("partially_met", False, True),
        ("fully_met", False, False),
        ("fully_met", True, True),
    ],
)
def test_outside_timeout_follows_unavailable_acceptance_policy(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    acceptance: str,
    *,
    acknowledged: bool,
    allowed: bool,
) -> None:
    """An outside deadline is honest unavailable evidence, never clean capture."""
    command, _root, probe = _policy_command(engine, tmp_path)
    if phase == "identity":
        probe.identity_error = RepositoryProbeErrorCode.PROBE_TIMED_OUT
    elif phase == "full":
        probe.inspect_error = RepositoryProbeErrorCode.PROBE_TIMED_OUT
    else:

        def topology_timeout(path: Path | str) -> bool:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.PROBE_TIMED_OUT, str(path)
            )

        monkeypatch.setattr(probe, "has_other_worktrees", topology_timeout)
    domain = _domain(engine, probe)
    request = _request(domain, command.project_id, command.task_id).model_copy(
        update={"uncommitted": acknowledged, "acceptance_result": acceptance},
    )
    result = domain.transition(request)
    assert result.ok is allowed
    if allowed:
        captured = _stored(engine)
        assert captured["state"] == "unavailable"
        assert captured["probe_error_code"] == "PROBE_TIMED_OUT"
        assert captured["error_summary"] == "Repository probe timed out."
        assert "head_sha" not in captured
    else:
        assert result.error is not None
        assert "REPOSITORY_UNAVAILABLE_ACKNOWLEDGEMENT_REQUIRED" in result.error.message
