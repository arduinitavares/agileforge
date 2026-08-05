"""ADK execution-trace storage independence tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from google.adk.sessions import DatabaseSessionService
from sqlmodel import Session

from adapters.adk.recipes import AdkRecipeRegistry
from models.core import Project
from utils.runtime_config import (
    ADK_EXECUTION_TRACE_IDENTITY,
    clear_runtime_config_cache,
    get_adk_execution_trace_db_target,
)
from workflow.clock import FixedClock
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)


async def _create_session(service: DatabaseSessionService) -> None:
    await service.create_session(
        app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
        user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
        session_id="999",
    )
def test_deleting_adk_trace_database_does_not_change_domain_position(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove execution-trace deletion cannot alter fact-derived position."""
    trace_path = tmp_path / "adk-execution-trace.sqlite3"
    monkeypatch.setenv(
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        f"sqlite:///{trace_path.as_posix()}",
    )
    clear_runtime_config_cache()
    with Session(engine) as session:
        project = Project(name="Trace independent", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=AdkRecipeRegistry(()),
    )
    before = domain.position(project_id)
    target = get_adk_execution_trace_db_target()
    service = DatabaseSessionService(db_url=target.async_sqlite_url)
    asyncio.run(_create_session(service))
    assert trace_path.exists()
    with_trace = domain.position(project_id)
    trace_path.unlink()

    after = domain.position(project_id)

    assert with_trace == before
    assert after == before
