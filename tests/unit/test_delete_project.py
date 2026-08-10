"""Tests for delete_project script."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Project,
    ProjectTeam,
    SpecAuthorityAcceptance,
    SpecRegistry,
    Sprint,
    SprintStory,
    StoryCompletionLog,
    StoryStatus,
    Task,
    UserStory,
)
from models.core import Epic, Feature, Team, Theme
from repositories.project import ProjectDeletionConflictError
from scripts.delete_project import delete_project, resolve_db_path
from tests.typing_helpers import require_id
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.runtime_config import RuntimeConfigError, clear_runtime_config_cache

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


def test_delete_project_removes_sprints_and_story_logs(tmp_path: Path) -> None:
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
        feature_id = require_id(feature.feature_id, "feature_id")

        story = UserStory(
            title="Story",
            project_id=project_id,
            feature_id=feature_id,
        )
        session.add(story)
        session.flush()
        story_id = require_id(story.story_id, "story_id")

        session.add(Task(description="Task", story_id=story_id))

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


def test_delete_project_allows_pre_acceptance_authority_shell(tmp_path: Path) -> None:
    """Allow script deletion before any authority has been accepted."""
    db_path = tmp_path / "delete_project_spec.db"
    engine = _create_sqlite_engine(db_path)

    with Session(engine) as session:
        project = Project(name="Spec Project")
        session.add(project)
        session.flush()
        project_id = require_id(project.project_id, "project_id")

        spec = seed_accepted_specification(
            session,
            project_id=project_id,
            content=json.dumps({"title": "Pre-authority specification"}),
        ).spec
        spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

        session.add(
            CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version="1.0.0",
                prompt_hash="prompt-hash",
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
            )
        )
        session.commit()

    delete_project(project_id, str(db_path))

    with Session(engine) as session:
        assert session.get(Project, project_id) is None
        assert session.exec(select(CompiledSpecAuthority)).first() is None
        assert session.exec(select(SpecRegistry)).first() is None


def test_delete_project_preserves_historically_accepted_authority(
    tmp_path: Path,
) -> None:
    """Refuse script deletion before mutating accepted authority evidence."""
    db_path = tmp_path / "delete_project_accepted_authority.db"
    engine = _create_sqlite_engine(db_path)

    with Session(engine) as session:
        project = Project(name="Accepted Authority Project")
        session.add(project)
        session.flush()
        project_id = require_id(project.project_id, "project_id")

        spec = seed_accepted_specification(
            session,
            project_id=project_id,
            content=json.dumps({"title": "Accepted authority specification"}),
        ).spec
        spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="accepted-prompt-hash",
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
        )
        session.add(authority)
        session.flush()
        authority_id = require_id(authority.authority_id, "authority_id")

        acceptance = SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="reviewer",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=authority_id,
        )
        session.add(acceptance)
        session.commit()
        acceptance_id = require_id(acceptance.id, "acceptance_id")

    with pytest.raises(ProjectDeletionConflictError) as exc_info:
        delete_project(project_id, str(db_path))

    assert exc_info.value.references == ("spec_authority_acceptance.status",)
    with Session(engine) as session:
        assert session.get(Project, project_id) is not None
        assert session.get(SpecRegistry, spec_version_id) is not None
        assert session.get(CompiledSpecAuthority, authority_id) is not None
        assert session.get(SpecAuthorityAcceptance, acceptance_id) is not None


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
