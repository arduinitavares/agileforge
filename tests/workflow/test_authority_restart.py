"""Authority position restart and session-deletion regressions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session

from models.core import Product
from models.specs import CompiledSpecAuthority, SpecRegistry
from tests.workflow.test_workflow_repository import sqlite_engine
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

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


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
        project = Product(name="Restart authority", origin="greenfield")
        session.add(project)
        session.flush()
        assert project.product_id is not None
        spec = SpecRegistry(
            product_id=project.product_id,
            spec_hash="sha256:restart-spec",
            content="# Restart scope",
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="reviewer",
        )
        session.add(spec)
        session.flush()
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
        project_id = project.product_id

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
