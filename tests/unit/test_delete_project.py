"""Tests for delete_project script."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from agile_sqlmodel import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecApproval,
    BrownfieldSpecDraftAttempt,
    CompiledSpecAuthority,
    Product,
    ProductTeam,
    SpecRegistry,
    Sprint,
    SprintStory,
    StoryCompletionLog,
    StoryStatus,
    Task,
    UserStory,
)
from models.core import Epic, Feature, Team, Theme
from scripts.delete_project import delete_project, resolve_db_path
from tests.typing_helpers import require_id
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
        product = Product(name="Test Product")
        team = Team(name="Test Team")
        session.add(product)
        session.add(team)
        session.flush()
        product_id = require_id(product.product_id, "product_id")
        team_id = require_id(team.team_id, "team_id")

        session.add(ProductTeam(product_id=product_id, team_id=team_id))

        theme = Theme(title="Theme", product_id=product_id)
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
            product_id=product_id,
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
            product_id=product_id,
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

    delete_project(product_id, str(db_path))

    with Session(engine) as session:
        product_exists = session.exec(
            select(Product).where(Product.product_id == product_id)
        ).first()
        assert product_exists is None
        sprint_exists = session.exec(
            select(Sprint).where(Sprint.product_id == product_id)
        ).first()
        assert sprint_exists is None
        assert session.exec(select(UserStory)).first() is None
        assert session.exec(select(StoryCompletionLog)).first() is None
        assert session.exec(select(Task)).first() is None
        assert session.exec(select(Feature)).first() is None
        assert session.exec(select(Epic)).first() is None
        assert session.exec(select(Theme)).first() is None


def test_delete_project_removes_compiled_spec_authority(tmp_path: Path) -> None:
    """Ensure delete_project clears compiled spec authority records."""
    db_path = tmp_path / "delete_project_spec.db"
    engine = _create_sqlite_engine(db_path)

    with Session(engine) as session:
        product = Product(name="Spec Product")
        session.add(product)
        session.flush()
        product_id = require_id(product.product_id, "product_id")

        spec = SpecRegistry(
            product_id=product_id,
            spec_hash="spec-hash",
            content="# Spec",
            status="approved",
        )
        session.add(spec)
        session.flush()
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

    delete_project(product_id, str(db_path))

    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).first() is None
        assert session.exec(select(SpecRegistry)).first() is None


def test_delete_project_removes_brownfield_artifacts(tmp_path: Path) -> None:
    """Ensure delete_project clears brownfield artifact records before product."""
    db_path = tmp_path / "delete_project_brownfield.db"
    engine = _create_sqlite_engine(db_path)

    with Session(engine) as session:
        product = Product(name="Brownfield Product")
        session.add(product)
        session.flush()
        product_id = require_id(product.product_id, "product_id")

        session.add(
            BrownfieldSourceArtifact(
                project_id=product_id,
                attempt_id="source-attempt-1",
                artifact_fingerprint="source-fingerprint-1",
                request_hash="source-request-hash-1",
            )
        )
        session.add(
            BrownfieldScanAttempt(
                project_id=product_id,
                attempt_id="scan-attempt-1",
                artifact_fingerprint="scan-artifact-fingerprint-1",
                source_fingerprint="source-fingerprint-1",
                repo_path=str(tmp_path / "repo"),
                request_hash="scan-request-hash-1",
            )
        )
        session.add(
            BrownfieldSpecDraftAttempt(
                project_id=product_id,
                attempt_id="draft-attempt-1",
                artifact_fingerprint="draft-artifact-fingerprint-1",
                origin="scan",
                source_fingerprint="source-fingerprint-1",
                scan_attempt_id="scan-attempt-1",
                scan_fingerprint="scan-fingerprint-1",
                request_hash="draft-request-hash-1",
            )
        )
        session.add(
            BrownfieldSpecApproval(
                project_id=product_id,
                approval_attempt_id="approval-attempt-1",
                approval_fingerprint="approval-fingerprint-1",
                draft_attempt_id="draft-attempt-1",
                draft_fingerprint="draft-fingerprint-1",
                scan_fingerprint="scan-fingerprint-1",
                source_fingerprint="source-fingerprint-1",
                spec_hash="spec-hash-1",
            )
        )
        session.commit()

    delete_project(product_id, str(db_path))

    with Session(engine) as session:
        assert session.exec(select(Product)).first() is None
        assert session.exec(select(BrownfieldSpecApproval)).first() is None
        assert session.exec(select(BrownfieldSpecDraftAttempt)).first() is None
        assert session.exec(select(BrownfieldScanAttempt)).first() is None
        assert session.exec(select(BrownfieldSourceArtifact)).first() is None


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
