"""Repository binding workflow request contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from git import Repo
from pydantic import ValidationError
from sqlmodel import Session, col, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.repository import RepositoryBinding, repository_binding_fingerprint
from services.application import (
    AgileForgeApplication,
    RepositoryAttachRequest,
    RepositoryRefreshRequest,
)
from services.project_lifecycle import CreateProjectCommand, ProjectLifecycleService
from services.read_projections import DurableReadProjectionService
from services.repository_probe import RepositoryProbeResult, RepositoryStatusEntry
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.requests import RecordRepositoryBinding, RepositoryBindingInput

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

_REPOSITORY_PATH = "repository"


def test_repository_binding_request_has_no_decision_fingerprint() -> None:
    """Keep repository attachment outside graph decision guards."""
    probe = RepositoryProbeResult(
        worktree_path=_REPOSITORY_PATH,
        common_git_dir=f"{_REPOSITORY_PATH}/.git",
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_entries=(),
        status_fingerprint="status-1",
        remotes=(),
        probe_version="agileforge.repository-probe.v1",
        inspected_at=datetime(2026, 8, 9, 12, tzinfo=UTC),
        warnings=(),
    )

    binding = RepositoryBindingInput.from_probe(
        probe,
        recorded_by="operator@example.com",
    )
    request = RecordRepositoryBinding(
        project_id=1,
        operation="attach",
        requested_repository_path=_REPOSITORY_PATH,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="fact-1",
        expected_active_binding_fingerprint=None,
        binding=binding,
        idempotency_key="attach-1",
        actor="operator@example.com",
    )

    assert "decision_fingerprint" not in request.model_dump()
    assert request.binding.worktree_path == _REPOSITORY_PATH

    payload = request.model_dump()
    payload["requested_repository_path"] = None
    with pytest.raises(ValidationError):
        RecordRepositoryBinding.model_validate(payload)

    payload["operation"] = "refresh"
    with pytest.raises(ValidationError):
        RecordRepositoryBinding.model_validate(payload)


_EXPECTED_BINDING_COUNT: int = 2


def _attachment_repository(
    root: Path, count: int
) -> tuple[str, str, list[RepositoryStatusEntry]]:
    """Build known renamed, modified and NUL-sensitive attachment evidence."""
    root.mkdir()
    names = tuple(f"old-{number:04d}.txt" for number in range(count))
    renamed = tuple(name.replace("old", "new") for name in names)
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Attachment Probe Test")
            config.set_value("user", "email", "attachment-probe@example.com")
        for number, name in enumerate(names):
            (root / name).write_text(f"original {number}\n", encoding="utf-8")
        repo.index.add(list(names))
        repo.index.commit("attachment probe fixture")
        expected_head = repo.head.commit.hexsha
        expected_branch = repo.active_branch.name
        for old, new in zip(names, renamed, strict=True):
            (root / old).rename(root / new)
        repo.index.remove(list(names))
        repo.index.add(list(renamed))
    expected_entries = []
    for old, new in zip(names, renamed, strict=True):
        (root / new).write_text("worktree change\n", encoding="utf-8")
        expected_entries.extend(
            (
                RepositoryStatusEntry(
                    area="index", change="renamed", path=new, previous_path=old
                ),
                RepositoryStatusEntry(area="worktree", change="modified", path=new),
            )
        )
    unusual = "space and\nnewline.txt"
    (root / unusual).write_text("untracked\n", encoding="utf-8")
    expected_entries.append(
        RepositoryStatusEntry(area="untracked", change="added", path=unusual)
    )
    return expected_head, expected_branch, expected_entries


@pytest.mark.parametrize("count", [3, 1500], ids=["representative", "large"])
def test_real_attachment_and_refresh_preserve_bulk_repository_observations(
    tmp_path: Path, engine: Engine, count: int
) -> None:
    """Persist complete bulk evidence through actual attach and refresh consumers."""
    root = tmp_path / "repository"
    expected_head, expected_branch, expected_entries = _attachment_repository(
        root, count
    )
    index = root / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 10, 7, 12, tzinfo=UTC)),
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        read_projection=DurableReadProjectionService(engine=engine),
    )
    application.set_project_lifecycle(
        ProjectLifecycleService(
            engine=engine,
            workflow_domain=domain,
            repository_probe=GitPythonRepositoryProbe(),
        )
    )
    created = application.create_project(
        CreateProjectCommand(
            name="Repository attachment parity",
            idempotency_key="create-parity",
            actor="probe-test",
        )
    )
    assert created.ok is True
    project_id = created.output["project_id"]
    assert isinstance(project_id, int)
    attached = application.attach_repository(
        RepositoryAttachRequest(
            project_id=project_id,
            path=str(root),
            idempotency_key="attach-parity",
            actor="probe-test",
        )
    )
    assert attached.ok is True
    with Session(engine) as session:
        first = session.exec(select(RepositoryBinding)).one()
        first_dump = first.model_dump()
        assert first.head_sha == expected_head
        assert first.branch_name == expected_branch
        assert first.dirty is True
        assert {
            RepositoryStatusEntry.model_validate(entry)
            for entry in json.loads(first.status_entries_json)
        } == set(expected_entries)
        assert attached.output["repository_binding_fingerprint"] == (
            repository_binding_fingerprint(first)
        )
    (root / "fresh-untracked.txt").write_text("fresh\n", encoding="utf-8")
    expected_entries.append(
        RepositoryStatusEntry(
            area="untracked", change="added", path="fresh-untracked.txt"
        )
    )
    refreshed = application.refresh_repository(
        RepositoryRefreshRequest(
            project_id=project_id,
            idempotency_key="refresh-parity",
            actor="probe-test",
        )
    )
    assert refreshed.ok is True
    with Session(engine) as session:
        bindings = session.exec(
            select(RepositoryBinding).order_by(
                col(RepositoryBinding.repository_binding_id)
            )
        ).all()
        assert len(bindings) == _EXPECTED_BINDING_COUNT
        assert bindings[0].model_dump() == first_dump
        assert (
            bindings[1].supersedes_repository_binding_id == first.repository_binding_id
        )
        assert {
            RepositoryStatusEntry.model_validate(entry)
            for entry in json.loads(bindings[1].status_entries_json)
        } == set(expected_entries)
        assert refreshed.output["repository_binding_fingerprint"] == (
            repository_binding_fingerprint(bindings[1])
        )
        assert bindings[1].status_fingerprint != bindings[0].status_fingerprint
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
