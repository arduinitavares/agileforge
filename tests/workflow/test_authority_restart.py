"""Authority position restart and session-deletion regressions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from models.core import Project
from models.db import set_sqlite_pragma
from models.specs import CompiledSpecAuthority
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)
from workflow.clock import FixedClock
from workflow.definitions.authority import authority_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


def sqlite_engine(path: Path) -> Engine:
    """Create a fresh file-backed SQLite engine for restart tests."""
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    return engine


def _authority_json() -> str:
    return SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationSuccess(
            scope_themes=["Restart"],
            invariants=[
                Invariant(
                    id="INV-0123456789abcdef",
                    type=InvariantType.REQUIRED_FIELD,
                    parameters=RequiredFieldParams(field_name="project_id"),
                )
            ],
            eligible_feature_rules=[],
            gaps=[],
            assumptions=[],
            source_map=[],
            compiler_version="3.0.0",
            prompt_hash="a" * 64,
        )
    ).model_dump_json()


def test_pending_review_survives_restart_without_adk_or_workflow_session(
    tmp_path: Path,
) -> None:
    """Pending review is unchanged after process and session-state loss."""
    database_path = tmp_path / "workflow.db"
    session_path = tmp_path / "adk-sessions.db"
    session_path.write_text("disposable session state", encoding="utf-8")
    engine = sqlite_engine(database_path)
    with Session(engine) as session:
        project = Project(name="Restart authority")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content='{"title":"Restart authority"}',
            recorded_at=EVALUATED_AT,
        )
        spec = lineage.spec
        assert spec.spec_version_id is not None
        session.add(
            CompiledSpecAuthority(
                spec_version_id=spec.spec_version_id,
                compiler_version="3.0.0",
                prompt_hash="a" * 64,
                compiled_at=EVALUATED_AT,
                compiled_artifact_json=_authority_json(),
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
            )
        )
        session.commit()
        project_id = project.project_id

    first_domain = WorkflowDomain(
        engine=engine,
        graph=authority_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )
    first = first_domain.position(project_id)
    engine.dispose()
    session_path.unlink()

    restarted_engine = sqlite_engine(database_path)
    second_domain = WorkflowDomain(
        engine=restarted_engine,
        graph=authority_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )
    second = second_domain.position(project_id)

    assert first.fact_fingerprint == second.fact_fingerprint
    assert first.decisions == second.decisions
    assert second.waiting_nodes == ("authority.review",)
