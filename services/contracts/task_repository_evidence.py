# services/contracts/task_repository_evidence.py
"""Closed immutable repository observations recorded with Task completion."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from services.repository_probe import (
    RepositoryProbeErrorCode,  # noqa: TC001  # Pydantic evaluates this field at runtime.
)

_MAX_DIRTY_PATHS: int = 50
_FINGERPRINT_PATTERN: str = r"^sha256:[0-9a-f]{64}$"


class _TaskRepositoryEvidence(BaseModel):
    """A frozen contract independent of persistence and workflow packages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["agileforge.task-repository-evidence.v1"]
    uncommitted_acknowledged: StrictBool


class _BoundTaskRepositoryEvidence(_TaskRepositoryEvidence):
    """Retained binding identity and the selected inspection target."""

    repository_binding_id: StrictInt = Field(gt=0)
    repository_binding_fingerprint: str = Field(pattern=_FINGERPRINT_PATTERN)
    worktree_path: str = Field(min_length=1)
    probed_path_matches_binding: StrictBool


class CapturedTaskRepositoryEvidence(_BoundTaskRepositoryEvidence):
    """One successful server observation, distinct from attachment-time state."""

    state: Literal["captured"]
    common_git_dir: str = Field(min_length=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch_name: str | None
    detached_head: StrictBool
    dirty: StrictBool
    dirty_path_count: StrictInt = Field(ge=0)
    dirty_paths: tuple[str, ...] = Field(max_length=_MAX_DIRTY_PATHS)
    dirty_paths_truncated: StrictBool
    other_worktrees_present: StrictBool
    status_fingerprint: str = Field(pattern=_FINGERPRINT_PATTERN)
    probe_version: Literal["agileforge.repository-probe.v1"]
    inspected_at: datetime

    @field_validator("inspected_at")
    @classmethod
    def require_utc_observation(cls, value: datetime) -> datetime:
        """Require an explicit timezone and retain one canonical UTC instant."""
        if value.tzinfo is None:
            message = "Repository observation timestamp requires a timezone."
            raise ValueError(message)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_consistent_observation(self) -> Self:
        """Reject contradictory branches, dirty counts, and bounded path lists."""
        if self.detached_head != (self.branch_name is None) or (
            self.branch_name is not None and not self.branch_name.strip()
        ):
            message = "Repository branch and detached HEAD state disagree."
            raise ValueError(message)
        if (
            self.dirty != (self.dirty_path_count > 0)
            or self.dirty_paths != tuple(sorted(set(self.dirty_paths)))
            or any(not path for path in self.dirty_paths)
            or len(self.dirty_paths) != min(self.dirty_path_count, _MAX_DIRTY_PATHS)
            or self.dirty_paths_truncated != (self.dirty_path_count > _MAX_DIRTY_PATHS)
        ):
            message = "Repository dirty state, count, and bounded paths disagree."
            raise ValueError(message)
        return self


class UnboundTaskRepositoryEvidence(_TaskRepositoryEvidence):
    """Explicit new completion with no bound repository observation."""

    state: Literal["not_bound"]


class UnavailableTaskRepositoryEvidence(_BoundTaskRepositoryEvidence):
    """A typed probe failure without an invented revision or clean state."""

    state: Literal["unavailable"]
    reason_code: Literal["REPOSITORY_UNAVAILABLE"]
    probe_error_code: RepositoryProbeErrorCode
    error_summary: str = Field(min_length=1, max_length=240)

    @field_validator("error_summary")
    @classmethod
    def require_safe_error_summary(cls, value: str) -> str:
        """Prevent multiline/control output from entering immutable evidence."""
        if any(
            not character.isprintable() or not character.isascii()
            for character in value
        ):
            message = "Repository probe error summary contains unsafe characters."
            raise ValueError(message)
        return value


TaskRepositoryEvidence = Annotated[
    CapturedTaskRepositoryEvidence
    | UnboundTaskRepositoryEvidence
    | UnavailableTaskRepositoryEvidence,
    Field(discriminator="state"),
]
_REPOSITORY_EVIDENCE: TypeAdapter[TaskRepositoryEvidence] = TypeAdapter(
    TaskRepositoryEvidence
)


def canonical_task_repository_evidence_json(evidence: TaskRepositoryEvidence) -> str:
    """Encode exactly the closed payload, independently of workflow hashing."""
    return json.dumps(
        evidence.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def parse_task_repository_evidence_json(payload_json: str) -> TaskRepositoryEvidence:
    """Reject malformed, inconsistent, extra-field, or noncanonical storage."""
    evidence = _REPOSITORY_EVIDENCE.validate_json(payload_json)
    if canonical_task_repository_evidence_json(evidence) != payload_json:
        message = "Task repository evidence JSON is not canonical."
        raise ValueError(message)
    return evidence


__all__ = [
    "CapturedTaskRepositoryEvidence",
    "TaskRepositoryEvidence",
    "UnavailableTaskRepositoryEvidence",
    "UnboundTaskRepositoryEvidence",
    "canonical_task_repository_evidence_json",
    "parse_task_repository_evidence_json",
]
