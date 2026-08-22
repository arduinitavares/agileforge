"""Tests for delete_project script."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from agile_sqlmodel import (
    Project,
    ProjectTeam,
    SpecRegistry,
    Sprint,
    SprintStory,
    StoryCompletionLog,
    StoryStatus,
    Task,
    UserStory,
)
from models.core import Epic, Feature, Team, Theme
from models.product_definition import SpecificationDecision
from models.workflow import BacklogArtifact, RoadmapArtifact, StoryArtifact
from scripts.delete_project import delete_project, resolve_db_path
from tests.typing_helpers import require_id
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.runtime_config import RuntimeConfigError, clear_runtime_config_cache
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from sqlalchemy.engine import Engine


def _create_sqlite_engine(db_path: Path) -> Engine:
    """Create a SQLite engine with foreign keys enabled."""
    engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: Connection, _connection_record: object
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(autouse=True)
def _clear_runtime_cache() -> Iterator[None]:
    clear_runtime_config_cache()
    yield
    clear_runtime_config_cache()


def test_delete_project_removes_sprints_and_story_logs(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Ensure delete_project clears sprints and story completion logs."""
    db_path = tmp_path / "delete_project_test.db"
    engine = _create_sqlite_engine(db_path)

    with Session(engine) as session:
        project = Project(name="Test Project")
        team = Team(name="Test Team")
        session.add(project)
        session.add(team)
        session.flush()
        project_id = require_id(project.project_id, "project_id")
        team_id = require_id(team.team_id, "team_id")

        session.add(ProjectTeam(project_id=project_id, team_id=team_id))

        theme = Theme(title="Theme", project_id=project_id)
        session.add(theme)
        session.flush()
        theme_id = require_id(theme.theme_id, "theme_id")

        epic = Epic(title="Epic", theme_id=theme_id)
        session.add(epic)
        session.flush()
        epic_id = require_id(epic.epic_id, "epic_id")

        feature = Feature(title="Feature", epic_id=epic_id)
        session.add(feature)
        session.flush()

        def bind_current_specification_decision(
            flush_session: Session,
            *_args: object,
        ) -> None:
            decisions = [
                row
                for row in flush_session.new
                if isinstance(row, SpecificationDecision)
            ]
            registries = [
                row for row in flush_session.new if isinstance(row, SpecRegistry)
            ]
            if len(decisions) != 1 or len(registries) != 1:
                return
            decision = decisions[0]
            registry = registries[0]
            if decision.specification_decision_id is None:
                decision.specification_decision_id = 1_000_000 + project_id
            registry.source_specification_decision_id = (
                decision.specification_decision_id
            )

        event.listen(session, "before_flush", bind_current_specification_decision)
        try:
            lineage = seed_accepted_specification(
                session,
                project_id=project_id,
                content='{"title":"Accepted specification"}',
            )
        finally:
            event.remove(session, "before_flush", bind_current_specification_decision)

        spec_id = require_id(lineage.spec.spec_version_id, "spec_version_id")
        backlog_fingerprint = canonical_hash({"artifact": "backlog"})
        backlog = BacklogArtifact(
            project_id=project_id,
            spec_version_id=spec_id,
            spec_hash=lineage.spec.spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            version_number=1,
            canonical_content_json='{"items":["BACKLOG-1"]}',
            content_fingerprint=backlog_fingerprint,
            created_by="fixture",
        )
        session.add(backlog)
        session.flush()
        backlog_id = require_id(backlog.backlog_artifact_id, "backlog_artifact_id")

        roadmap_fingerprint = canonical_hash({"artifact": "roadmap"})
        roadmap = RoadmapArtifact(
            project_id=project_id,
            backlog_artifact_id=backlog_id,
            backlog_artifact_fingerprint=backlog_fingerprint,
            version_number=1,
            canonical_content_json='{"items":["ROADMAP-1"]}',
            content_fingerprint=roadmap_fingerprint,
            created_by="fixture",
        )
        session.add(roadmap)
        session.flush()
        roadmap_id = require_id(roadmap.roadmap_artifact_id, "roadmap_artifact_id")

        story_artifact_fingerprint = canonical_hash({"artifact": "story"})
        story_artifact = StoryArtifact(
            project_id=project_id,
            source_backlog_artifact_id=backlog_id,
            source_backlog_artifact_fingerprint=backlog_fingerprint,
            backlog_item_id="BACKLOG-1",
            roadmap_artifact_id=roadmap_id,
            roadmap_artifact_fingerprint=roadmap_fingerprint,
            version_number=1,
            canonical_content_json='{"items":["STORY-1"]}',
            content_fingerprint=story_artifact_fingerprint,
            story_item_ids_json='["STORY-1"]',
            created_by="fixture",
        )
        session.add(story_artifact)
        session.flush()
        story_artifact_id = require_id(
            story_artifact.story_artifact_id,
            "story_artifact_id",
        )

        story = UserStory(
            project_id=project_id,
            source_story_artifact_id=story_artifact_id,
            source_story_artifact_fingerprint=story_artifact_fingerprint,
            source_story_item_id="STORY-1",
            source_story_item_fingerprint=canonical_hash({"story": "STORY-1"}),
            accepted_spec_version_id=spec_id,
            accepted_spec_hash=lineage.spec.spec_hash,
            spec_item_ids_json='["GOAL.fixture.accepted-specification"]',
            title="Story",
            story_description="As a user, I want deletion coverage.",
            acceptance_criteria_json='["Every dependent row is deleted."]',
            persona="user",
        )
        session.add(story)
        session.flush()
        story_id = require_id(story.story_id, "story_id")

        session.add(
            Task(
                description="Task",
                metadata_json='{"version":"task_metadata.v2"}',
                story_id=story_id,
            )
        )

        sprint = Sprint(
            goal="Goal",
            start_date=date.today(),  # noqa: DTZ011
            end_date=date.today() + timedelta(days=7),  # noqa: DTZ011
            project_id=project_id,
            team_id=team_id,
        )
        session.add(sprint)
        session.flush()
        sprint_id = require_id(sprint.sprint_id, "sprint_id")

        session.add(SprintStory(sprint_id=sprint_id, story_id=story_id))
        session.add(
            StoryCompletionLog(
                story_id=story_id,
                old_status=StoryStatus.TO_DO,
                new_status=StoryStatus.DONE,
            )
        )
        session.commit()

    delete_project(project_id, str(db_path))

    with Session(engine) as session:
        project_exists = session.exec(
            select(Project).where(Project.project_id == project_id)
        ).first()
        assert project_exists is None
        sprint_exists = session.exec(
            select(Sprint).where(Sprint.project_id == project_id)
        ).first()
        assert sprint_exists is None
        assert session.exec(select(UserStory)).first() is None
        assert session.exec(select(StoryCompletionLog)).first() is None
        assert session.exec(select(Task)).first() is None
        assert session.exec(select(Feature)).first() is None
        assert session.exec(select(Epic)).first() is None
        assert session.exec(select(Theme)).first() is None


def test_resolve_db_path_prefers_explicit_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve db path prefers explicit argument."""
    explicit_path = tmp_path / "explicit.db"
    monkeypatch.setenv("AGILEFORGE_DB_URL", "sqlite:///./db/from-env.db")
    resolved = resolve_db_path(str(explicit_path))
    assert resolved == str(explicit_path.resolve())


def test_resolve_db_path_requires_config_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve db path requires config when missing."""
    monkeypatch.delenv("AGILEFORGE_DB_URL", raising=False)
    with pytest.raises(RuntimeConfigError, match="AGILEFORGE_DB_URL"):
        resolve_db_path(None)
