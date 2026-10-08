# tests/services/test_task_completion_repository_projection.py
"""Completion reads present stored repository observations without live enrichment."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

import pytest
from git import Repo
from sqlalchemy import event
from sqlmodel import Session

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.db import ensure_business_db_ready
from repositories.workflow import WorkflowFactRepository
from services.read_projections import DurableReadProjectionService
from tests.adapters.sprint_retry_fixtures import durable_rows
from tests.services.test_task_completion_repository_evidence import (
    EVALUATED_AT,
    _bind,
    _domain,
    _repository,
    _request,
)
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.execution_retry_support import (
    _close_execution_sprint,
    _triage_execution_sprint,
)
from tests.workflow.test_sprint_retry_execution import _start_retry
from tests.workflow.test_sprint_retry_schema import _frozen_completed_history
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Never

    from sqlalchemy.engine import Connection, Engine, ExecutionContext
    from sqlalchemy.engine.interfaces import DBAPICursor

    from workflow.facts import WorkflowFactSnapshot

_DIRTY_PATH_LIMIT: int = 50
_ERROR_SUMMARY_LIMIT: int = 240


def _load(engine: Engine, project_id: int) -> WorkflowFactSnapshot:
    with Session(engine) as session:
        return WorkflowFactRepository(session).load(project_id)


def _completion_views(
    engine: Engine,
    project_id: int,
    sprint_id: int,
    task_id: int,
    *,
    retry_id: int | None = None,
) -> dict[str, dict[str, object]]:
    """Exercise the existing detail, Task history and original Sprint history."""
    reads = DurableReadProjectionService(engine=engine)
    detail = reads.sprint_task_show(
        project_id=project_id, sprint_id=sprint_id, task_id=task_id
    )
    history = reads.sprint_task_history(
        project_id=project_id, sprint_id=sprint_id, task_id=task_id
    )
    sprint_history = reads.sprint_history(project_id=project_id)
    assert detail["ok"] is history["ok"] is sprint_history["ok"] is True
    detail_data = cast("dict[str, object]", detail["data"])
    history_data = cast("dict[str, object]", history["data"])
    sprint_data = cast("dict[str, object]", sprint_history["data"])
    attempts = cast("list[dict[str, object]]", sprint_data["execution_attempts"])
    original = next(
        item
        for item in attempts
        if item["sprint_id"] == sprint_id and item["retry_attempt_id"] is None
    )
    completion = next(
        item
        for item in cast("list[dict[str, object]]", original["task_completions"])
        if item["task_id"] == task_id
    )
    views = {
        "detail": cast("dict[str, object]", detail_data["completion"]),
        "original_detail": cast(
            "dict[str, object]", detail_data["original_completion"]
        ),
        "task_history": cast("dict[str, object]", history_data["completion"]),
        "original_task_history": cast(
            "dict[str, object]", history_data["original_completion"]
        ),
        "sprint_history": completion,
    }
    if retry_id is not None:
        retry = next(item for item in attempts if item["retry_attempt_id"] == retry_id)
        views["retry_sprint_history"] = next(
            item
            for item in cast("list[dict[str, object]]", retry["task_completions"])
            if item["task_id"] == task_id
        )
    return views


def _assert_projection(
    completion: dict[str, object],
    canonical: dict[str, object],
    *,
    recording: str,
    warnings: tuple[str, ...],
) -> None:
    """Assert the entire Fact plus exactly its four presentation fields."""
    messages = completion["repository_warning_messages"]
    assert isinstance(messages, list)
    assert len(messages) == len(warnings)
    assert all(isinstance(message, str) and message.strip() for message in messages)
    assert completion == {
        **canonical,
        "repository_evidence": canonical.get("repository_evidence"),
        "revision_recording": recording,
        "repository_warnings": list(warnings),
        "repository_warning_messages": messages,
    }


def _reject_write(
    _connection: Connection,
    _cursor: DBAPICursor,
    statement: str,
    _parameters: object,
    _context: ExecutionContext,
    _executemany: bool,
) -> None:
    if statement.lstrip().split(maxsplit=1)[0].upper() in {
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "ALTER",
        "DROP",
        "REPLACE",
    }:
        msg = f"Projection attempted a database write: {statement[:80]}"
        raise AssertionError(msg)


def _reject_probe(*_args: object, **_kwargs: object) -> Never:
    msg = "Completion reads must not inspect the live repository."
    raise AssertionError(msg)


@contextmanager
def _read_only(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for method in (
        "inspect",
        "inspect_revision",
        "inspect_common_git_dir",
        "has_other_worktrees",
    ):
        monkeypatch.setattr(GitPythonRepositoryProbe, method, _reject_probe)
    event.listen(engine, "before_cursor_execute", _reject_write)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", _reject_write)


def test_clean_completion_is_presented_on_every_original_read_surface(
    engine: Engine, tmp_path: Path
) -> None:
    """The real stored capture gains presentation fields without changing its Fact."""
    project_id, sprint_id, _story_id, task_id = seed_started_execution(engine)
    target = _repository(tmp_path)
    binding_id = _bind(engine, project_id, target)
    domain = _domain(engine)
    assert domain.transition(_request(domain, project_id, task_id)).ok is True
    snapshot = _load(engine, project_id)
    fact = snapshot.task_completions[0]
    assert fact.repository_evidence is not None
    canonical = fact.model_dump(mode="json")
    with Repo(target) as repo:
        head = repo.head.commit.hexsha
    stored = fact.repository_evidence.model_dump(mode="json")
    assert stored["head_sha"] == head
    assert stored["repository_binding_id"] == binding_id
    assert stored["worktree_path"] == str(target.resolve())
    assert stored["dirty"] is False
    assert stored["dirty_paths"] == []

    for completion in _completion_views(
        engine, project_id, sprint_id, task_id
    ).values():
        assert completion == {
            **canonical,
            "repository_evidence": stored,
            "revision_recording": "recorded",
            "repository_warnings": [],
            "repository_warning_messages": [],
        }
    assert fact.model_dump(mode="json") == canonical


@pytest.mark.parametrize(
    ("scenario", "expected_warnings"),
    [
        ("clean_acknowledged", ()),
        ("dirty", ("UNCOMMITTED_WORKTREE",)),
        ("detached", ("DETACHED_HEAD",)),
        ("bounded_dirty", ("UNCOMMITTED_WORKTREE",)),
        ("other_worktrees", ("OTHER_WORKTREES_PRESENT",)),
        ("linked_override", ("UNCOMMITTED_WORKTREE", "DETACHED_HEAD")),
    ],
)
def test_captured_states_keep_exact_completion_observations(
    engine: Engine,
    tmp_path: Path,
    scenario: str,
    expected_warnings: tuple[str, ...],
) -> None:
    """Present stored SHA, path and status with the recorded bounded preview."""
    project_id, sprint_id, _story_id, task_id = seed_started_execution(engine)
    target = _repository(tmp_path)
    _bind(engine, project_id, target)
    selected = target
    if scenario in {"other_worktrees", "linked_override"}:
        selected = tmp_path / "linked"
        with Repo(target) as repo:
            repo.git.worktree("add", "--detach", str(selected), "HEAD")
        if scenario == "other_worktrees":
            selected = target
    if scenario == "detached":
        with Repo(selected) as repo:
            repo.git.checkout("--detach", "HEAD")
    dirty_paths = (
        [f"untracked-{index:02d}.txt" for index in range(55)]
        if scenario == "bounded_dirty"
        else ["tracked.txt"]
        if scenario in {"dirty", "linked_override"}
        else []
    )
    for relative in dirty_paths:
        (selected / relative).write_text("uncommitted delivery\n", encoding="utf-8")
    domain = _domain(engine)
    request = _request(domain, project_id, task_id).model_copy(
        update={
            "uncommitted": True,
            "worktree_path": str(selected) if scenario == "linked_override" else None,
        }
    )
    assert domain.transition(request).ok is True
    fact = _load(engine, project_id).task_completions[0]
    assert fact.repository_evidence is not None
    captured = fact.repository_evidence.model_dump(mode="json")
    with Repo(selected) as repo:
        assert captured["head_sha"] == repo.head.commit.hexsha
        assert captured["branch_name"] == (
            None if repo.head.is_detached else repo.active_branch.name
        )
        assert captured["detached_head"] is repo.head.is_detached
    assert captured["worktree_path"] == str(selected.resolve())
    assert captured["probed_path_matches_binding"] is (selected == target)
    assert captured["dirty"] is bool(dirty_paths)
    assert captured["dirty_path_count"] == len(dirty_paths)
    assert captured["dirty_paths"] == dirty_paths[:_DIRTY_PATH_LIMIT]
    assert captured["dirty_paths_truncated"] is (len(dirty_paths) > _DIRTY_PATH_LIMIT)
    assert captured["uncommitted_acknowledged"] is True
    assert captured["other_worktrees_present"] is (scenario == "other_worktrees")
    canonical = fact.model_dump(mode="json")
    for completion in _completion_views(
        engine, project_id, sprint_id, task_id
    ).values():
        _assert_projection(
            completion, canonical, recording="recorded", warnings=expected_warnings
        )


@pytest.mark.parametrize("bound", [False, True])
def test_nonrevision_observations_remain_explicit(
    engine: Engine, tmp_path: Path, *, bound: bool
) -> None:
    """Unavailable and explicitly unbound observations do not fabricate Git fields."""
    project_id, sprint_id, _story_id, task_id = seed_started_execution(engine)
    target = _repository(tmp_path)
    binding_id: int | None = None
    if bound:
        binding_id = _bind(engine, project_id, target)
        target.rename(tmp_path / "moved-repository")
    domain = _domain(engine)
    assert (
        domain.transition(
            _request(domain, project_id, task_id).model_copy(
                update={"uncommitted": True}
            )
        ).ok
        is True
    )
    fact = _load(engine, project_id).task_completions[0]
    assert fact.repository_evidence is not None
    recorded = fact.repository_evidence.model_dump(mode="json")
    state = "unavailable" if bound else "not_bound"
    assert recorded["state"] == state
    assert recorded["uncommitted_acknowledged"] is True
    assert {
        "head_sha",
        "branch_name",
        "dirty",
        "dirty_path_count",
        "dirty_paths",
    }.isdisjoint(recorded)
    if bound:
        assert recorded["repository_binding_id"] == binding_id
        assert recorded["worktree_path"] == str(target.resolve())
        assert recorded["probe_error_code"] == "PATH_MISSING"
        assert isinstance(recorded["error_summary"], str)
        assert 0 < len(recorded["error_summary"]) <= _ERROR_SUMMARY_LIMIT
    canonical = fact.model_dump(mode="json")
    warning = "REPOSITORY_UNAVAILABLE" if bound else "REPOSITORY_NOT_BOUND"
    for completion in _completion_views(
        engine, project_id, sprint_id, task_id
    ).values():
        _assert_projection(completion, canonical, recording=state, warnings=(warning,))


def test_unfinished_task_keeps_completion_null(engine: Engine) -> None:
    """Absence of completion remains distinct from legacy completion evidence."""
    project_id, sprint_id, _story_id, task_id = seed_started_execution(engine)
    reads = DurableReadProjectionService(engine=engine)
    for result in (
        reads.sprint_task_show(
            project_id=project_id, sprint_id=sprint_id, task_id=task_id
        ),
        reads.sprint_task_history(
            project_id=project_id, sprint_id=sprint_id, task_id=task_id
        ),
    ):
        assert result["ok"] is True
        data = cast("dict[str, object]", result["data"])
        assert data["completion"] is None
        assert data["original_completion"] is None


def test_legacy_null_is_presented_without_changing_canonical_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen pre-289 original and retry hashes remain unchanged on reads."""
    engine = _frozen_completed_history(tmp_path / "legacy.sqlite")
    ensure_business_db_ready(engine)
    try:
        before = _load(engine, 1)
        canonical = before.model_dump(mode="json")
        python_dump = before.model_dump(mode="python")
        rows = durable_rows(engine)
        facts = (
            before.task_completions[0],
            before.sprint_retries[0].task_completions[0],
        )
        for fact in facts:
            assert fact.repository_evidence is None
            assert "repository_evidence" not in fact.model_dump(mode="json")
        with _read_only(engine, monkeypatch):
            views = _completion_views(engine, 1, 1, 1, retry_id=1)
            for name, completion in views.items():
                is_retry = name in {"detail", "task_history", "retry_sprint_history"}
                expected = facts[1] if is_retry else facts[0]
                _assert_projection(
                    completion,
                    expected.model_dump(mode="json"),
                    recording="not_recorded",
                    warnings=(),
                )
            after = _load(engine, 1)
            assert after.model_dump(mode="json") == canonical
            assert after.model_dump(mode="python") == python_dump
        assert durable_rows(engine) == rows
    finally:
        engine.dispose()


def test_original_and_retry_snapshots_survive_live_changes_and_new_binding(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each attempt retains its own Git observation and reads never scan or write."""
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    target = _repository(tmp_path)
    original_binding = _bind(engine, project_id, target)
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
        engine=engine, graph=project_graph(), clock=FixedClock(EVALUATED_AT)
    )
    retry_id = _start_retry(
        engine, domain, project_id=project_id, sprint_id=sprint_id, suffix="projection"
    )
    reads = DurableReadProjectionService(engine=engine)
    pending = cast(
        "dict[str, object]",
        reads.sprint_task_show(
            project_id=project_id, sprint_id=sprint_id, task_id=task_id
        )["data"],
    )
    assert pending["completion"] is None
    assert pending["original_completion"] is not None
    selected = tmp_path / "retry-checkout"
    with Repo(target) as repo:
        repo.git.worktree("add", "--detach", str(selected), "HEAD")
    with Repo(selected) as repo:
        (selected / "tracked.txt").write_text("retry commit\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        repo.index.commit("retry completion")
    (selected / "tracked.txt").write_text("retry uncommitted\n", encoding="utf-8")
    assert (
        domain.transition(
            _request(domain, project_id, task_id, retry_id).model_copy(
                update={"uncommitted": True, "worktree_path": str(selected)}
            )
        ).ok
        is True
    )
    snapshot = _load(engine, project_id)
    original = snapshot.task_completions[0].model_dump(mode="json")
    retried = snapshot.sprint_retries[0].task_completions[0].model_dump(mode="json")
    assert (
        original["repository_evidence"]["head_sha"]
        != retried["repository_evidence"]["head_sha"]
    )
    assert original["repository_evidence"]["worktree_path"] == str(target.resolve())
    assert retried["repository_evidence"]["worktree_path"] == str(selected.resolve())
    with Repo(selected) as repo:
        repo.index.add(["tracked.txt"])
        repo.index.commit("later unrelated state")
    new_binding = _bind(engine, project_id, selected)
    assert new_binding != original_binding
    target.rename(tmp_path / "gone-after-completion")
    selected.rename(tmp_path / "gone-retry-after-completion")
    before = _load(engine, project_id)
    canonical = before.model_dump(mode="json")
    rows = durable_rows(engine)
    with _read_only(engine, monkeypatch):
        views = _completion_views(
            engine, project_id, sprint_id, task_id, retry_id=retry_id
        )
        for name, completion in views.items():
            is_retry = name in {"detail", "task_history", "retry_sprint_history"}
            _assert_projection(
                completion,
                retried if is_retry else original,
                recording="recorded",
                warnings=("UNCOMMITTED_WORKTREE", "DETACHED_HEAD") if is_retry else (),
            )
        assert _load(engine, project_id).model_dump(mode="json") == canonical
    assert durable_rows(engine) == rows
