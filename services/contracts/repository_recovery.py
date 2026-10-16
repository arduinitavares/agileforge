# services/contracts/repository_recovery.py
"""Sanitized recovery guidance for one rejected repository observation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

type RepositoryBindingChangedField = Literal[
    "worktree",
    "git_directory",
    "head",
    "branch",
    "detached_head",
    "working_tree_status",
    "remotes",
]


class RepositoryBindingRecovery(BaseModel):
    """Expose closed categories and saved/check status without repository values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason_code: Literal["REPOSITORY_PROVENANCE_STALE"] = "REPOSITORY_PROVENANCE_STALE"
    cause: Literal[
        "WORKTREE_CHANGED", "REPOSITORY_IDENTITY_CHANGED", "INSPECTION_UNAVAILABLE"
    ]
    recorded_binding_id: int
    recorded_dirty: bool
    observed_dirty: bool | None
    changed_fields: tuple[RepositoryBindingChangedField, ...]
    action: Literal["refresh_repository_binding"] = "refresh_repository_binding"
