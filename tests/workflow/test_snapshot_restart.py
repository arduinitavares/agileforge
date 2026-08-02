"""Workflow fact snapshot restart regression tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from sqlmodel import Session, col, select

from models.workflow import ChallengeArtifact
from repositories.workflow import WorkflowFactRepository
from tests.workflow.test_workflow_repository import seed_complete_project, sqlite_engine
from workflow.fingerprints import fact_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.facts import WorkflowFactSnapshot


def _load_without_provenance_reads(
    engine: Engine,
    project_id: int,
) -> WorkflowFactSnapshot:
    """Load while making common provenance file reads fail immediately."""
    blocked = AssertionError("Workflow fact loading read a provenance file.")
    with (
        patch("builtins.open", side_effect=blocked),
        patch.object(Path, "open", side_effect=blocked),
        patch.object(Path, "read_text", side_effect=blocked),
        patch.object(Path, "read_bytes", side_effect=blocked),
        Session(engine) as session,
    ):
        return WorkflowFactRepository(session).load(project_id)


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

    first = _load_without_provenance_reads(engine, project_id)

    provenance_path.write_text("changed evidence", encoding="utf-8")
    second = _load_without_provenance_reads(engine, project_id)

    provenance_path.unlink()
    third = _load_without_provenance_reads(engine, project_id)

    assert second == first
    assert fact_fingerprint(second) == fact_fingerprint(first)
    assert third == first
    assert fact_fingerprint(third) == fact_fingerprint(first)
