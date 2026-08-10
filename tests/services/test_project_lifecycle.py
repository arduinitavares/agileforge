"""Application-level tests for atomic Project and repository lifecycle changes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, select

from models.core import Project
from models.repository import RepositoryBinding
from models.workflow import WorkflowTransitionReceipt
from services.project_lifecycle import (
    CreateProjectCommand,
    ProjectLifecycleService,
    RepositoryAttachmentCommand,
    RepositoryRefreshCommand,
)
from services.repository_probe import (
    RepositoryProbeError,
    RepositoryProbeErrorCode,
    RepositoryProbeResult,
)
from workflow.clock import FixedClock
from workflow.contracts import TransitionResult, WorkflowErrorCode
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
REPOSITORY_PATH = "repository"
INJECTED_FAILURE = "injected failure"
EXPECTED_BINDING_COUNT = 2


class _Probe:
    def __init__(self, result: RepositoryProbeResult | Exception) -> None:
        self.result = result
        self.paths: list[str] = []

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        self.paths.append(str(path))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _probe_result(
    *,
    path: str = REPOSITORY_PATH,
    inspected_at: datetime = NOW,
) -> RepositoryProbeResult:
    """Build one deterministic probe result without filesystem access."""
    return RepositoryProbeResult(
        worktree_path=path,
        common_git_dir=f"{path}/.git",
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_entries=(),
        status_fingerprint="status-1",
        remotes=("https://example.invalid/repository.git",),
        probe_version="agileforge.repository-probe.v1",
        inspected_at=inspected_at,
        warnings=(),
    )


def _service(
    engine: Engine,
    probe: _Probe,
) -> tuple[ProjectLifecycleService, WorkflowDomain]:
    """Build the real lifecycle service around a deterministic v2 domain."""
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=NOW),
    )
    return (
        ProjectLifecycleService(
            engine=engine,
            workflow_domain=domain,
            repository_probe=probe,
        ),
        domain,
    )


def _create_command(**overrides: str | None) -> CreateProjectCommand:
    payload: dict[str, str | None] = {
        "name": "Lifecycle Project",
        "description": None,
        "repository_path": None,
        "idempotency_key": "create-1",
        "actor": "operator@example.com",
    }
    payload.update(overrides)
    return CreateProjectCommand(**payload)


def _project_id(result: TransitionResult) -> int:
    """Extract a returned durable Project identity."""
    output = result.output
    project_id = output["project_id"]
    assert isinstance(project_id, int)
    return project_id


def test_create_name_only_commits_project_and_opens_vision(engine: Engine) -> None:
    """Create the minimal Project and evaluate the initial Vision action."""
    service, _domain = _service(engine, _Probe(_probe_result()))

    result = service.create_project(_create_command())

    assert result.ok is True
    assert result.position is not None
    assert any(
        item.node_id == "vision.interview" and item.category.value == "available"
        for item in result.position.decisions
    )
    with Session(engine) as session:
        project = session.get(Project, _project_id(result))
        assert project is not None
        assert project.description is None
        assert session.exec(select(RepositoryBinding)).all() == []
        assert len(session.exec(select(WorkflowTransitionReceipt)).all()) == 1


def test_create_with_repository_commits_project_binding_and_receipt_together(
    engine: Engine,
) -> None:
    """Persist semantic creation input and the repository observation together."""
    service, _domain = _service(engine, _Probe(_probe_result()))

    result = service.create_project(
        _create_command(
            description="Stored as input only.",
            repository_path=REPOSITORY_PATH,
        )
    )

    assert result.ok is True
    with Session(engine) as session:
        project = session.get(Project, _project_id(result))
        assert project is not None
        assert project.description == "Stored as input only."
        assert project.active_repository_binding_id is not None
        binding = session.get(RepositoryBinding, project.active_repository_binding_id)
        assert binding is not None
        assert binding.project_id == project.project_id
        assert len(session.exec(select(WorkflowTransitionReceipt)).all()) == 1


def test_probe_failure_writes_no_project_binding_or_receipt(engine: Engine) -> None:
    """Reject a failed probe before opening any lifecycle write transaction."""
    failure = RepositoryProbeError(RepositoryProbeErrorCode.PATH_MISSING, "/missing")
    service, _domain = _service(engine, _Probe(failure))

    with pytest.raises(RepositoryProbeError):
        service.create_project(_create_command(repository_path="/missing"))

    with Session(engine) as session:
        assert session.exec(select(Project)).all() == []
        assert session.exec(select(RepositoryBinding)).all() == []
        assert session.exec(select(WorkflowTransitionReceipt)).all() == []


def test_failure_after_binding_rolls_back_project_binding_and_receipt(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back all creation state when work fails after the binding insert."""
    service, domain = _service(engine, _Probe(_probe_result()))
    original = domain._complete_receipt

    def fail_after_binding(
        session: Session,
        receipt: WorkflowTransitionReceipt,
        result: TransitionResult,
        evaluated_at: datetime,
    ) -> None:
        original(session, receipt, result, evaluated_at)
        raise RuntimeError(INJECTED_FAILURE)

    monkeypatch.setattr(domain, "_complete_receipt", fail_after_binding)

    with pytest.raises(RuntimeError, match=INJECTED_FAILURE):
        service.create_project(_create_command(repository_path=REPOSITORY_PATH))

    with Session(engine) as session:
        assert session.exec(select(Project)).all() == []
        assert session.exec(select(RepositoryBinding)).all() == []
        assert session.exec(select(WorkflowTransitionReceipt)).all() == []


def test_create_replays_same_idempotency_and_conflicts_on_changed_input(
    engine: Engine,
) -> None:
    """Replay exact creation input and reject changed semantic input."""
    service, _domain = _service(engine, _Probe(_probe_result()))
    first = service.create_project(_create_command())
    replay = service.create_project(_create_command())
    conflict = service.create_project(_create_command(description="changed"))

    assert replay == first.model_copy(update={"replayed": True})
    assert conflict.ok is False
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1


def test_attachment_is_orthogonal_and_refresh_appends_observation(
    engine: Engine,
) -> None:
    """Leave graph facts untouched while append-only repository state changes."""
    probe = _Probe(_probe_result())
    service, domain = _service(engine, probe)
    created = service.create_project(_create_command())
    project_id = _project_id(created)
    before = domain.position(project_id)

    attached = service.attach_repository(
        RepositoryAttachmentCommand(
            project_id=project_id,
            path=REPOSITORY_PATH,
            expected_active_binding_fingerprint=None,
            idempotency_key="attach-1",
            actor="operator@example.com",
        )
    )
    assert attached.ok is True
    after = domain.position(project_id)
    assert after.fact_fingerprint == before.fact_fingerprint
    assert after.decisions == before.decisions

    active_fingerprint = str(attached.output["repository_binding_fingerprint"])
    probe.result = _probe_result(inspected_at=NOW + timedelta(seconds=1))
    refreshed = service.refresh_repository(
        RepositoryRefreshCommand(
            project_id=project_id,
            expected_active_binding_fingerprint=active_fingerprint,
            idempotency_key="refresh-1",
            actor="operator@example.com",
        )
    )
    assert refreshed.ok is True
    with Session(engine) as session:
        bindings = session.exec(select(RepositoryBinding)).all()
        assert len(bindings) == EXPECTED_BINDING_COUNT
        assert (
            bindings[-1].supersedes_repository_binding_id
            == bindings[0].repository_binding_id
        )


def test_failed_attachment_preserves_active_binding_pointer(engine: Engine) -> None:
    """Preserve the active pointer when a replacement probe fails."""
    probe = _Probe(_probe_result())
    service, _domain = _service(engine, probe)
    project_id = _project_id(service.create_project(_create_command()))
    attached = service.attach_repository(
        RepositoryAttachmentCommand(
            project_id=project_id,
            path=REPOSITORY_PATH,
            expected_active_binding_fingerprint=None,
            idempotency_key="attach-1",
            actor="operator@example.com",
        )
    )
    binding_id = attached.output["repository_binding_id"]
    assert isinstance(binding_id, int)
    original_pointer = binding_id
    probe.result = RepositoryProbeError(
        RepositoryProbeErrorCode.PATH_MISSING,
        "/missing",
    )

    with pytest.raises(RepositoryProbeError):
        service.attach_repository(
            RepositoryAttachmentCommand(
                project_id=project_id,
                path="/missing",
                expected_active_binding_fingerprint=str(
                    attached.output["repository_binding_fingerprint"]
                ),
                idempotency_key="attach-2",
                actor="operator@example.com",
            )
        )

    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert project.active_repository_binding_id == original_pointer
