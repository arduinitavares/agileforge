"""File-backed restart coverage for scope-extension facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import SQLModel, create_engine

from tests.workflow.test_scope_extension_transitions import (
    _decision,
    _domain,
    accept_amendment_draft,
    register_amendment,
    seed_terminal_project,
    start_extension,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _file_engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def test_accepted_amendment_position_survives_file_backed_restart(
    tmp_path: Path,
) -> None:
    """Preserve accepted amendment decisions across a database restart."""
    database_path = tmp_path / "task-13-restart.db"
    first_engine = _file_engine(database_path)
    first_domain, project_id = seed_terminal_project(first_engine)
    _start, run_id = start_extension(first_domain, first_engine, project_id)
    draft_id, _content = accept_amendment_draft(
        first_domain,
        first_engine,
        project_id,
        run_id,
    )
    before = first_domain.position(project_id)
    registration_before = _decision(
        before,
        "scope_extension.registration",
        f"run:{run_id}",
    )
    first_engine.dispose()

    restarted_engine = _file_engine(database_path)
    restarted_domain = _domain(restarted_engine)
    after = restarted_domain.position(project_id)
    registration_after = _decision(
        after,
        "scope_extension.registration",
        f"run:{run_id}",
    )

    assert after.fact_fingerprint == before.fact_fingerprint
    assert (
        registration_after.decision_fingerprint
        == registration_before.decision_fingerprint
    )
    register_amendment(restarted_domain, project_id, run_id, draft_id)
    restarted_engine.dispose()
