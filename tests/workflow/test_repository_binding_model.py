"""Repository binding persistence model tests."""

from __future__ import annotations

from sqlalchemy.schema import UniqueConstraint
from sqlmodel import SQLModel

from models import repository
from models.db import _CURRENT_MODEL_MODULES


def test_repository_binding_registers_immutable_persisted_fields() -> None:
    """Expose the exact lifecycle binding row and storage types."""
    table = SQLModel.metadata.tables["repository_bindings"]

    assert table.name == "repository_bindings"
    assert set(table.columns.keys()) == {
        "repository_binding_id",
        "project_id",
        "worktree_path",
        "common_git_dir",
        "head_sha",
        "branch_name",
        "detached_head",
        "dirty",
        "status_fingerprint",
        "remotes_json",
        "warnings_json",
        "probe_version",
        "inspected_at",
        "supersedes_repository_binding_id",
        "recorded_by",
    }
    assert table.c.worktree_path.type.__class__.__name__ == "Text"
    assert table.c.common_git_dir.type.__class__.__name__ == "Text"
    assert table.c.remotes_json.type.__class__.__name__ == "Text"
    assert table.c.warnings_json.type.__class__.__name__ == "Text"
    assert repository in _CURRENT_MODEL_MODULES


def test_repository_binding_enforces_project_scoped_lineage_and_identity() -> None:
    """Keep replacement rows and replay identity within one project."""
    table = SQLModel.metadata.tables["repository_bindings"]
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    foreign_keys = {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }

    assert ("project_id", "repository_binding_id") in unique_columns
    assert ("project_id", "status_fingerprint", "inspected_at") in unique_columns
    assert (
        ("project_id", "supersedes_repository_binding_id"),
        (
            "repository_bindings.project_id",
            "repository_bindings.repository_binding_id",
        ),
    ) in foreign_keys
