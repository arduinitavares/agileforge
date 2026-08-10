"""Durable Vision projection regressions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from models.core import Project
from models.db import set_sqlite_pragma
from models.product_definition import VisionInterviewTurn
from services.vision_projection import VisionLineageError, load_current_accepted_vision
from tests.vision_lineage_fixtures import (
    seed_accepted_vision,
    seed_accepted_vision_revision,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _project_id(project: Project) -> int:
    assert project.project_id is not None
    return project.project_id


def test_projection_rejects_incomplete_source_turn(engine: Engine) -> None:
    """Fail closed when an accepted Vision no longer has a complete source turn."""
    with Session(engine) as session:
        project = Project(name="Incomplete Vision Source")
        session.add(project)
        session.commit()
        artifact = seed_accepted_vision(
            session,
            project_id=_project_id(project),
            statement="Use durable source facts.",
        )
        turn = session.get(VisionInterviewTurn, artifact.source_interview_turn_id)
        assert turn is not None
        turn.is_complete = False
        turn.output_fingerprint = canonical_hash(
            {
                "components_json": json.loads(turn.components_json),
                "vision_statement": turn.vision_statement,
                "is_complete": False,
                "clarifying_questions_json": json.loads(
                    turn.clarifying_questions_json
                ),
            }
        )
        session.add(turn)
        session.commit()

        with pytest.raises(VisionLineageError, match="complete source interview turn"):
            load_current_accepted_vision(session, project_id=_project_id(project))


def test_projection_returns_the_accepted_superseding_leaf(tmp_path: Path) -> None:
    """Return the sole accepted leaf after a valid durable Vision revision."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vision-projection.db'}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="Revised Vision")
        session.add(project)
        session.commit()
        initial = seed_accepted_vision(
            session,
            project_id=_project_id(project),
            statement="Initial durable Vision.",
        )
        revised = seed_accepted_vision_revision(
            session,
            project_id=_project_id(project),
            superseded_vision=initial,
            statement="Revised durable Vision.",
        )

        accepted = load_current_accepted_vision(
            session,
            project_id=_project_id(project),
        )
        revised_id = revised.vision_artifact_id
        revised_fingerprint = revised.content_fingerprint

    assert accepted is not None
    assert accepted.vision_artifact_id == revised_id
    assert accepted.fingerprint == revised_fingerprint
    assert accepted.statement == "Revised durable Vision."
    engine.dispose()
