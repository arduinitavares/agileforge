"""Internal prepared requests for Project and repository lifecycle mutations."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from workflow.contracts import FrozenModel, JsonObject


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
    repository_binding: RepositoryBindingInput | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None


class RecordRepositoryBinding(FrozenModel):
    """Append one repository observation without changing graph facts."""

    kind: Literal["record_repository_binding"] = "record_repository_binding"
    project_id: int
    graph_version: str = Field(min_length=1)
    fact_fingerprint: str = Field(min_length=1)
    expected_active_binding_fingerprint: str | None = None
    binding: RepositoryBindingInput
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None


__all__ = [
    "CreateProject",
    "RecordRepositoryBinding",
    "RepositoryBindingInput",
]
