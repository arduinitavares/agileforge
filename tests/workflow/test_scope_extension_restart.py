"""File-backed restart coverage for scope-extension facts."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sqlmodel import Session, SQLModel, col, create_engine, select

from models.workflow import DiscoveryRun, ScopeExtensionReconciliation
from tests.workflow.test_issue_193_regression import (
    _accepted_replacement,
    _reconcile_request,
)
from tests.workflow.test_scope_extension_transitions import (
    _decision,
    _domain,
    accept_amendment_draft,
    register_amendment,
    seed_terminal_project,
    start_extension,
)
from workflow.contracts import WorkflowErrorCode

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
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


def test_wrong_reconciliation_authority_is_stable_across_file_backed_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an old authority without closing or drifting after restart."""
    database_path = tmp_path / "task-13-wrong-authority-restart.db"
    first_engine = _file_engine(database_path)
    context = _accepted_replacement(
        first_engine,
        monkeypatch,
        provenance_path=None,
    )
    before = context.domain.position(context.project_id)

    rejected = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.old_authority_id,
            authority_fingerprint=context.old_authority_fingerprint,
            idempotency_key="task-13-file-backed-wrong-authority",
        )
    )

    assert rejected.ok is False
    assert rejected.error is not None
    assert rejected.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    first_engine.dispose()

    restarted_engine = _file_engine(database_path)
    restarted_domain = _domain(restarted_engine)
    restarted = restarted_domain.position(context.project_id)
    assert restarted.fact_fingerprint == before.fact_fingerprint
    assert tuple(item.decision_fingerprint for item in restarted.decisions) == tuple(
        item.decision_fingerprint for item in before.decisions
    )
    with Session(restarted_engine) as session:
        run = session.get(DiscoveryRun, context.run_id)
        assert run is not None
        assert run.closed_at is None
        assert (
            session.exec(
                select(ScopeExtensionReconciliation).where(
                    col(ScopeExtensionReconciliation.project_id) == context.project_id
                )
            ).all()
            == []
        )

    restarted_context = replace(
        context,
        domain=restarted_domain,
        reconciliation=_decision(
            restarted,
            "scope_extension.reconciliation",
            f"run:{context.run_id}",
        ),
    )
    reconciled = restarted_domain.transition(
        _reconcile_request(
            restarted_context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key="task-13-file-backed-correct-authority",
        )
    )
    assert reconciled.ok is True
    terminal = restarted_domain.position(context.project_id)
    assert terminal.terminal is True
    restarted_engine.dispose()

    final_engine = _file_engine(database_path)
    final_position = _domain(final_engine).position(context.project_id)
    assert final_position.fact_fingerprint == terminal.fact_fingerprint
    final_decisions = tuple(
        item.decision_fingerprint for item in final_position.decisions
    )
    terminal_decisions = tuple(item.decision_fingerprint for item in terminal.decisions)
    assert final_decisions == terminal_decisions
    assert final_position.terminal is True
    final_engine.dispose()
