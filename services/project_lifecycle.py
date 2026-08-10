"""Application boundary for atomic Project identity and repository provenance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, field_validator
from sqlmodel import Session

from models.core import Project
from models.repository import RepositoryBinding, repository_binding_fingerprint
from workflow.contracts import (
    FrozenModel,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.requests.project import (
    CreateProject,
    RecordRepositoryBinding,
    RepositoryBindingInput,
    RepositoryBindingSemanticInput,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbe
    from workflow.contracts import WorkflowPosition


class CreateProjectCommand(FrozenModel):
    """Business input for creating one Project."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    repository_path: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        """Normalize Project identity before any probe or write can occur."""
        return value.strip() if isinstance(value, str) else value


class RepositoryAttachmentCommand(FrozenModel):
    """Business input for attaching or replacing a Project repository."""

    project_id: int
    path: str = Field(min_length=1)
    expected_active_binding_fingerprint: str | None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None


class RepositoryRefreshCommand(FrozenModel):
    """Business input for recording a fresh observation of the active repository."""

    project_id: int
    expected_active_binding_fingerprint: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None


class _WorkflowDomainPort(Protocol):
    def position(self, project_id: int) -> WorkflowPosition: ...

    def transition_in_session(
        self,
        session: Session,
        request: CreateProject | RecordRepositoryBinding,
    ) -> TransitionResult: ...

    def replay_project_transition(
        self,
        *,
        request_kind: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> TransitionResult | None: ...


class ProjectLifecycleService:
    """Probe first, then perform each Project lifecycle mutation in one transaction."""

    def __init__(
        self,
        *,
        engine: Engine,
        workflow_domain: _WorkflowDomainPort,
        repository_probe: RepositoryProbe,
    ) -> None:
        """Retain the transaction authority and repository probe."""
        self._engine = engine
        self._workflow_domain = workflow_domain
        self._repository_probe = repository_probe

    def create_project(self, command: CreateProjectCommand) -> TransitionResult:
        """Probe an optional repository before opening the Project write transaction."""
        request_fingerprint = CreateProject.semantic_fingerprint_for(
            name=command.name,
            description=command.description,
            requested_repository_path=command.repository_path,
            actor=command.actor,
            correlation_id=command.correlation_id,
        )
        replay = self._workflow_domain.replay_project_transition(
            request_kind="create_project",
            idempotency_key=command.idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay
        binding = (
            None
            if command.repository_path is None
            else RepositoryBindingInput.from_probe(
                self._repository_probe.inspect(command.repository_path),
                recorded_by=command.actor,
            )
        )
        return self._transition(
            CreateProject(
                name=command.name,
                description=command.description,
                requested_repository_path=command.repository_path,
                repository_binding=binding,
                idempotency_key=command.idempotency_key,
                actor=command.actor,
                correlation_id=command.correlation_id,
            )
        )

    def attach_repository(
        self,
        command: RepositoryAttachmentCommand,
    ) -> TransitionResult:
        """Probe a requested path before atomically appending its observation."""
        operation = _RepositoryBindingOperation(
            project_id=command.project_id,
            operation="attach",
            requested_repository_path=command.path,
            expected_active_binding_fingerprint=(
                command.expected_active_binding_fingerprint
            ),
            idempotency_key=command.idempotency_key,
            actor=command.actor,
            correlation_id=command.correlation_id,
        )
        replay = self._binding_replay(operation)
        if replay is not None:
            return replay
        binding = RepositoryBindingInput.from_probe(
            self._repository_probe.inspect(command.path),
            recorded_by=command.actor,
        )
        return self._record_binding(operation, binding)

    def refresh_repository(self, command: RepositoryRefreshCommand) -> TransitionResult:
        """Probe the currently active repository path before appending a new record."""
        operation = _RepositoryBindingOperation(
            project_id=command.project_id,
            operation="refresh",
            requested_repository_path=None,
            expected_active_binding_fingerprint=(
                command.expected_active_binding_fingerprint
            ),
            idempotency_key=command.idempotency_key,
            actor=command.actor,
            correlation_id=command.correlation_id,
        )
        replay = self._binding_replay(operation)
        if replay is not None:
            return replay
        with Session(self._engine) as session:
            project = session.get(Project, command.project_id)
            if project is None or project.active_repository_binding_id is None:
                return self._conflict("The Project has no active repository binding.")
            active = session.get(
                RepositoryBinding,
                project.active_repository_binding_id,
            )
            if active is None:
                return self._conflict("The active repository binding is unavailable.")
            if (
                repository_binding_fingerprint(active)
                != command.expected_active_binding_fingerprint
            ):
                return self._conflict("The active repository binding changed.")
            path = active.worktree_path
        binding = RepositoryBindingInput.from_probe(
            self._repository_probe.inspect(path),
            recorded_by=command.actor,
        )
        return self._record_binding(operation, binding)

    def _record_binding(
        self,
        operation: _RepositoryBindingOperation,
        binding: RepositoryBindingInput,
    ) -> TransitionResult:
        position = self._workflow_domain.position(operation.project_id)
        return self._transition(
            RecordRepositoryBinding(
                project_id=operation.project_id,
                operation=operation.operation,
                requested_repository_path=operation.requested_repository_path,
                graph_version=position.graph_version,
                fact_fingerprint=position.fact_fingerprint,
                expected_active_binding_fingerprint=(
                    operation.expected_active_binding_fingerprint
                ),
                binding=binding,
                idempotency_key=operation.idempotency_key,
                actor=operation.actor,
                correlation_id=operation.correlation_id,
            )
        )

    def _binding_replay(
        self,
        operation: _RepositoryBindingOperation,
    ) -> TransitionResult | None:
        request_fingerprint = RecordRepositoryBinding.semantic_fingerprint_for(
            RepositoryBindingSemanticInput(
                project_id=operation.project_id,
                operation=operation.operation,
                requested_repository_path=operation.requested_repository_path,
                expected_active_binding_fingerprint=(
                    operation.expected_active_binding_fingerprint
                ),
                actor=operation.actor,
                correlation_id=operation.correlation_id,
            )
        )
        return self._workflow_domain.replay_project_transition(
            request_kind="record_repository_binding",
            idempotency_key=operation.idempotency_key,
            request_fingerprint=request_fingerprint,
        )

    def _transition(
        self,
        request: CreateProject | RecordRepositoryBinding,
    ) -> TransitionResult:
        with Session(self._engine) as session:
            try:
                result = self._workflow_domain.transition_in_session(session, request)
                session.commit()
            except Exception:
                session.rollback()
                raise
            return result

    @staticmethod
    def _conflict(message: str) -> TransitionResult:
        return TransitionResult(
            ok=False,
            error=WorkflowError(
                code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
                message=message,
            ),
        )


class _RepositoryBindingOperation(FrozenModel):
    """Shared trusted metadata for one repository observation append."""

    project_id: int
    operation: Literal["attach", "refresh"]
    requested_repository_path: str | None
    expected_active_binding_fingerprint: str | None
    idempotency_key: str
    actor: str
    correlation_id: str | None


__all__ = [
    "CreateProjectCommand",
    "ProjectLifecycleService",
    "RepositoryAttachmentCommand",
    "RepositoryRefreshCommand",
]
