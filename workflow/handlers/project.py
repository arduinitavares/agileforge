"""Transactional handlers for Project identity and repository observations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from models.core import Project
from models.repository import RepositoryBinding
from repositories.workflow import WorkflowFactRepository
from workflow.contracts import (
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

    from workflow.graph import WorkflowGraph
    from workflow.requests.project import (
        CreateProject,
        RecordRepositoryBinding,
        RepositoryBindingInput,
    )


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


def _binding(
    *,
    project_id: int,
    input: RepositoryBindingInput,
    supersedes_repository_binding_id: int | None,
) -> RepositoryBinding:
    """Translate one trusted probe observation into immutable persistence."""
    return RepositoryBinding(
        project_id=project_id,
        worktree_path=input.worktree_path,
        common_git_dir=input.common_git_dir,
        head_sha=input.head_sha,
        branch_name=input.branch_name,
        detached_head=input.detached_head,
        dirty=input.dirty,
        status_fingerprint=input.status_fingerprint,
        remotes_json=canonical_json(list(input.remotes)),
        warnings_json=canonical_json(list(input.warnings)),
        probe_version=input.probe_version,
        inspected_at=input.inspected_at,
        supersedes_repository_binding_id=supersedes_repository_binding_id,
        recorded_by=input.recorded_by,
    )


def _result(
    *,
    session: Session,
    graph: WorkflowGraph,
    project_id: int,
    output: dict[str, object],
    evaluated_at: datetime,
) -> TransitionResult:
    """Evaluate the durable v2 graph from rows written in this transaction."""
    snapshot = WorkflowFactRepository(session).load(project_id)
    position = graph.evaluate(snapshot, evaluated_at)
    return TransitionResult(ok=True, output=output, position=position)


def execute_create_project(
    session: Session,
    request: CreateProject,
    graph: WorkflowGraph,
    evaluated_at: datetime,
) -> TransitionResult:
    """Insert Project, optional binding, and evaluated position atomically."""
    project = Project(name=request.name.strip(), description=request.description)
    session.add(project)
    session.flush()
    if project.project_id is None:
        return _conflict("Project creation did not assign a durable identity.")

    output: dict[str, object] = {"project_id": project.project_id}
    if request.repository_binding is not None:
        binding = _binding(
            project_id=project.project_id,
            input=request.repository_binding,
            supersedes_repository_binding_id=None,
        )
        session.add(binding)
        session.flush()
        if binding.repository_binding_id is None:
            return _conflict("Repository binding did not assign a durable identity.")
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        output.update(
            repository_binding_id=binding.repository_binding_id,
            repository_binding_fingerprint=binding.status_fingerprint,
        )
    session.flush()
    return _result(
        session=session,
        graph=graph,
        project_id=project.project_id,
        output=output,
        evaluated_at=evaluated_at,
    )


def execute_record_repository_binding(
    session: Session,
    request: RecordRepositoryBinding,
    graph: WorkflowGraph,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append a binding only when the expected active observation still matches."""
    project = session.get(Project, request.project_id)
    if project is None:
        return _conflict("The Project does not exist.")
    active = (
        None
        if project.active_repository_binding_id is None
        else session.get(RepositoryBinding, project.active_repository_binding_id)
    )
    active_fingerprint = None if active is None else active.status_fingerprint
    if active_fingerprint != request.expected_active_binding_fingerprint:
        return _conflict("The active repository binding changed.")

    binding = _binding(
        project_id=request.project_id,
        input=request.binding,
        supersedes_repository_binding_id=(
            None if active is None else active.repository_binding_id
        ),
    )
    session.add(binding)
    session.flush()
    if binding.repository_binding_id is None:
        return _conflict("Repository binding did not assign a durable identity.")
    project.active_repository_binding_id = binding.repository_binding_id
    session.add(project)
    session.flush()
    return _result(
        session=session,
        graph=graph,
        project_id=request.project_id,
        output={
            "project_id": request.project_id,
            "repository_binding_id": binding.repository_binding_id,
            "repository_binding_fingerprint": binding.status_fingerprint,
        },
        evaluated_at=evaluated_at,
    )


__all__ = ["execute_create_project", "execute_record_repository_binding"]
