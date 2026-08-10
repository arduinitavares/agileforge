"""Fresh-schema Project deletion tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session

from models.core import Project
from models.repository import RepositoryBinding
from repositories.project import ProjectRepository

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_REPOSITORY_PATH = "repository"
_COMMIT_FAILURE = "injected commit failure"


def _repository_binding(project_id: int) -> RepositoryBinding:
    return RepositoryBinding(
        project_id=project_id,
        worktree_path=_REPOSITORY_PATH,
        common_git_dir=f"{_REPOSITORY_PATH}/.git",
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_fingerprint="status-1",
        remotes_json="[]",
        warnings_json="[]",
        probe_version="agileforge.repository-probe.v1",
        inspected_at=datetime(2026, 8, 9, 12, tzinfo=UTC),
        recorded_by="operator@example.com",
    )


def test_delete_project_removes_active_repository_binding(engine: Engine) -> None:
    """Remove the Project pointer and immutable repository observations together."""
    with Session(engine) as session:
        project = Project(name="Repository deletion")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        binding = _repository_binding(project_id)
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        project.active_repository_binding_id = binding_id
        session.add(project)
        session.commit()

        assert ProjectRepository(session).delete_project(project_id) is True
        assert session.get(Project, project_id) is None
        assert session.get(RepositoryBinding, binding_id) is None


def test_delete_project_rolls_back_repository_rows_when_commit_fails(
    engine: Engine,
) -> None:
    """Leave both Project and active binding intact when the write cannot commit."""
    with Session(engine) as session:
        project = Project(name="Repository rollback")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        binding = _repository_binding(project_id)
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        project.active_repository_binding_id = binding_id
        session.add(project)
        session.commit()

        def fail_commit(_session: Session) -> None:
            raise RuntimeError(_COMMIT_FAILURE)

        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match=_COMMIT_FAILURE):
                ProjectRepository(session).delete_project(project_id)
        finally:
            event.remove(session, "before_commit", fail_commit)

        assert session.get(Project, project_id) is not None
        assert session.get(RepositoryBinding, binding_id) is not None
