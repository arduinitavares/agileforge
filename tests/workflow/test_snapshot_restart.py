"""Workflow fact snapshot restart regression tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.workflow import ChallengeArtifact
from repositories.workflow import WorkflowFactRepository
from tests.workflow.test_workflow_repository import seed_complete_project, sqlite_engine
from workflow.fingerprints import fact_fingerprint

if TYPE_CHECKING:
    from pathlib import Path


def test_snapshot_is_reproducible_after_repository_restart(tmp_path: Path) -> None:
    """Recreated sessions produce the same typed canonical fact snapshot."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)

    with Session(engine) as first_session:
        first = WorkflowFactRepository(first_session).load(project_id)
    engine.dispose()

    restarted_engine = sqlite_engine(tmp_path / "workflow.db")
    with Session(restarted_engine) as second_session:
        second = WorkflowFactRepository(second_session).load(project_id)

    assert first == second
    assert fact_fingerprint(first) == fact_fingerprint(second)


def test_snapshot_uses_database_content_after_provenance_files_change(
    tmp_path: Path,
) -> None:
    """Provenance file drift cannot change persisted canonical facts."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    provenance_path = tmp_path / "challenge-provenance.md"
    provenance_path.write_text("original evidence", encoding="utf-8")
    with Session(engine) as session:
        artifact = session.exec(
            select(ChallengeArtifact).order_by(
                col(ChallengeArtifact.challenge_artifact_id)
            )
        ).first()
        assert artifact is not None
        artifact.provenance_path = str(provenance_path)
        session.add(artifact)
        session.commit()

    with Session(engine) as session:
        first = WorkflowFactRepository(session).load(project_id)

    provenance_path.write_text("changed evidence", encoding="utf-8")
    with Session(engine) as session:
        second = WorkflowFactRepository(session).load(project_id)

    provenance_path.unlink()
    with Session(engine) as session:
        third = WorkflowFactRepository(session).load(project_id)

    assert second == first
    assert fact_fingerprint(second) == fact_fingerprint(first)
    assert third == first
    assert fact_fingerprint(third) == fact_fingerprint(first)
