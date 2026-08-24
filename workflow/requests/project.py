"""Internal prepared requests for Project and repository lifecycle mutations."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from workflow.contracts import FrozenModel, JsonObject
from workflow.fingerprints import canonical_hash


class RepositoryBindingInput(FrozenModel):
    """Trusted repository observation prepared by the application boundary."""

    worktree_path: str
    common_git_dir: str
    head_sha: str = Field(min_length=40, max_length=40)
    branch_name: str | None
    detached_head: bool
    dirty: bool
    status_entries: tuple[JsonObject, ...]
    status_fingerprint: str = Field(min_length=1)
    remotes: tuple[str, ...]
    probe_version: Literal["agileforge.repository-probe.v1"]
    inspected_at: AwareDatetime
    warnings: tuple[JsonObject, ...]
    recorded_by: str = Field(min_length=1, max_length=200)

    @classmethod
    def from_probe(
        cls,
        result: object,
        *,
        recorded_by: str,
    ) -> RepositoryBindingInput:
        """Preserve a completed probe result without re-reading the repository."""
        model_dump = getattr(result, "model_dump", None)
        if not callable(model_dump):
            msg = "Repository probe result must provide model_dump()."
            raise TypeError(msg)
        payload = model_dump(mode="python")
        if not isinstance(payload, dict):
            msg = "Repository probe result did not produce an object payload."
            raise TypeError(msg)
        return cls(**payload, recorded_by=recorded_by)


class CreateProject(FrozenModel):
    """Create one Project and optional immutable repository binding."""

    kind: Literal["create_project"] = "create_project"
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    requested_repository_path: str | None = None
    repository_binding: RepositoryBindingInput | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        """Normalize Project identity before validation and persistence."""
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_repository_input(self) -> CreateProject:
        """Require caller path and completed probe input to be present together."""
        if (self.requested_repository_path is None) != (
            self.repository_binding is None
        ):
            message = "Repository-backed creation requires path and probe input."
            raise ValueError(message)
        return self

    @classmethod
    def semantic_fingerprint_for(
        cls,
        *,
        name: str,
        description: str | None,
        requested_repository_path: str | None,
        actor: str,
        correlation_id: str | None,
    ) -> str:
        """Hash caller-owned creation input without volatile probe output."""
        return canonical_hash(
            {
                "kind": "create_project",
                "name": name,
                "description": description,
                "requested_repository_path": requested_repository_path,
                "actor": actor,
                "correlation_id": correlation_id,
            }
        )

    def semantic_fingerprint(self) -> str:
        """Return the exact semantic receipt fingerprint for this request."""
        return self.semantic_fingerprint_for(
            name=self.name,
            description=self.description,
            requested_repository_path=self.requested_repository_path,
            actor=self.actor,
            correlation_id=self.correlation_id,
        )


class RepositoryBindingSemanticInput(FrozenModel):
    """Caller-owned binding input that defines one idempotent operation."""

    project_id: int
    operation: Literal["attach", "refresh"]
    requested_repository_path: str | None
    actor: str
    correlation_id: str | None


class RecordRepositoryBinding(FrozenModel):
    """Append one repository observation without changing graph facts."""

    kind: Literal["record_repository_binding"] = "record_repository_binding"
    project_id: int
    operation: Literal["attach", "refresh"]
    requested_repository_path: str | None = None
    graph_version: str = Field(min_length=1)
    fact_fingerprint: str = Field(min_length=1)
    expected_active_binding_fingerprint: str | None = None
    binding: RepositoryBindingInput
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None

    @model_validator(mode="after")
    def validate_operation_guards(self) -> RecordRepositoryBinding:
        """Require complete caller semantics for attach and refresh operations."""
        if self.operation == "attach" and self.requested_repository_path is None:
            message = "Repository attachment requires the requested caller path."
            raise ValueError(message)
        if self.operation == "refresh" and (
            self.requested_repository_path is not None
            or self.expected_active_binding_fingerprint is None
        ):
            message = "Repository refresh requires only the active binding guard."
            raise ValueError(message)
        return self

    @classmethod
    def semantic_fingerprint_for(
        cls,
        input: RepositoryBindingSemanticInput,
    ) -> str:
        """Hash caller-owned binding input without volatile probe output."""
        return canonical_hash(
            {
                "kind": "record_repository_binding",
                "project_id": input.project_id,
                "operation": input.operation,
                "requested_repository_path": input.requested_repository_path,
                "actor": input.actor,
                "correlation_id": input.correlation_id,
            }
        )

    def semantic_fingerprint(self) -> str:
        """Return the exact semantic receipt fingerprint for this request."""
        return self.semantic_fingerprint_for(
            RepositoryBindingSemanticInput(
                project_id=self.project_id,
                operation=self.operation,
                requested_repository_path=self.requested_repository_path,
                actor=self.actor,
                correlation_id=self.correlation_id,
            )
        )


__all__ = [
    "CreateProject",
    "RecordRepositoryBinding",
    "RepositoryBindingInput",
    "RepositoryBindingSemanticInput",
]
