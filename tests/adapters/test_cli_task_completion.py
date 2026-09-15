"""Exercise lossless checklist transport against real synthetic workflow state."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, SQLModel, col, create_engine, select

from cli.main import main
from cli.workflow_commands import render_workflow_next
from models.core import Task
from models.enums import TaskStatus
from models.events import TaskExecutionLog
from models.workflow import TaskCompletionEvidence, WorkflowTransitionReceipt
from services.application import AgileForgeApplication, ExecutionActionSelectionService
from tests.adapters.sprint_retry_fixtures import durable_rows, preserves_existing_rows
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.clock import SystemClock
from workflow.definitions.execution import execution_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

_CHECKLIST: dict[str, str] = {
    "Run the documented --ignore=tests/e2e invocation": "exit=0; tests=passed",
    "Confirm A=B=C in the output": "observed=A=B=C",
}
_IDEMPOTENCY_KEY = "cli-equals-completion"
_ARGUMENT_ERROR = 2


def _application(engine: Engine) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=WorkflowDomain(
            engine=engine, graph=execution_graph(), clock=SystemClock()
        ),
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )


@pytest.fixture
def completion_engine(tmp_path: Path) -> Iterator[tuple[Engine, int, int]]:
    """Accept the equals-bearing checklist before starting a disposable Sprint."""
    engine = create_engine(f"sqlite:///{tmp_path / 'completion.db'}")
    SQLModel.metadata.create_all(engine)
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(
        engine, checklist_items=tuple(_CHECKLIST)
    )
    try:
        yield engine, project_id, task_id
    finally:
        engine.dispose()


def _command(project_id: int, task_id: int, checklist_file: Path) -> list[str]:
    return [
        "sprint",
        "task",
        "complete",
        "--project-id",
        str(project_id),
        "--instance-key",
        f"task:{task_id}",
        "--outcome-summary",
        "The documented invocation executed the tests.",
        "--artifact-ref",
        "test-output.txt",
        "--acceptance-result",
        "fully_met",
        "--checklist-file",
        str(checklist_file),
        "--idempotency-key",
        _IDEMPOTENCY_KEY,
        "--actor",
        "synthetic-operator",
    ]


def test_rendered_cli_completion_persists_exact_evidence_and_replays_after_restart(
    completion_engine: tuple[Engine, int, int],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Lossy transport or replay without durable receipts breaks this round trip."""
    engine, project_id, task_id = completion_engine
    application = _application(engine)
    checklist_file = tmp_path / "checklist.json"
    checklist_file.write_text(json.dumps(_CHECKLIST), encoding="utf-8")
    advertised = next(
        item
        for item in render_workflow_next(application.position(project_id=project_id))[
            "commands"
        ]
        if item["request_kind"] == "complete_task"
    )
    replacements = {
        "<outcome-summary>": "The documented invocation executed the tests.",
        "<artifact-ref>": "test-output.txt",
        "<acceptance-result>": "fully_met",
        "<checklist-file>": str(checklist_file),
        "<idempotency-key>": _IDEMPOTENCY_KEY,
        "<actor>": "synthetic-operator",
    }
    command = [
        replacements.get(token, token)
        for token in shlex.split(advertised["command"])[1:]
    ]
    assert main(command, application=application) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] is True
    assert applied["replayed"] is False
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.DONE
        evidence = session.exec(select(TaskCompletionEvidence)).one()
        assert json.loads(evidence.checklist_result_json) == _CHECKLIST
        assert len(session.exec(select(TaskExecutionLog)).all()) == 1
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == _IDEMPOTENCY_KEY
            )
        ).all()
        assert len(receipts) == 1
    after = durable_rows(engine)
    fingerprint = application.position(project_id=project_id).fact_fingerprint
    engine.dispose()
    application = _application(engine)
    # JSON formatting and object order are transport details, not new evidence.
    checklist_file.write_text(
        json.dumps(dict(reversed(tuple(_CHECKLIST.items()))), indent=2),
        encoding="utf-8",
    )
    assert main(command, application=application) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["replayed"] is True
    assert replay["output"] == applied["output"]
    assert durable_rows(engine) == after
    changed = {**_CHECKLIST, "Confirm A=B=C in the output": "observed=different"}
    checklist_file.write_text(json.dumps(changed), encoding="utf-8")
    assert main(command, application=application) == 1
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error"]["code"] == "WORKFLOW_FACT_CONFLICT"
    assert "idempotency" in rejected["error"]["message"].lower()
    assert durable_rows(engine) == after
    assert application.position(project_id=project_id).fact_fingerprint == fingerprint


@pytest.mark.parametrize(
    "checklist",
    [
        {
            "Run the documented --ignore": "tests/e2e invocation=passed",
            "Confirm A=B=C in the output": "passed",
        },
        {"Run the documented --ignore=tests/e2e invocation": "passed"},
        {**_CHECKLIST, "Unexpected extra criterion": "passed"},
    ],
)
def test_cli_rejects_inexact_checklist_coverage_without_mutation(
    checklist: dict[str, str],
    completion_engine: tuple[Engine, int, int],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Truncated, missing, or extra keys must never satisfy accepted Task text."""
    engine, project_id, task_id = completion_engine
    application = _application(engine)
    before = durable_rows(engine)
    position = application.position(project_id=project_id)
    checklist_file = tmp_path / "checklist.json"
    checklist_file.write_text(json.dumps(checklist), encoding="utf-8")
    assert (
        main(_command(project_id, task_id, checklist_file), application=application)
        == 1
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error"]["code"] == "WORKFLOW_FACT_CONFLICT"
    assert "every executable checklist item" in rejected["error"]["message"]
    after = durable_rows(engine)
    assert preserves_existing_rows(before, after)
    for table_name, rows in before.tables.items():
        if table_name != "workflow_transition_receipts":
            assert after.tables[table_name] == rows
    # The existing domain persists rejection receipts; it must not complete work.
    assert len(after.tables["workflow_transition_receipts"]) == (
        len(before.tables["workflow_transition_receipts"]) + 1
    )
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == _IDEMPOTENCY_KEY
            )
        ).one()
        assert receipt.result_json is not None
        assert json.loads(receipt.result_json)["ok"] is False
    assert (
        application.position(project_id=project_id).fact_fingerprint
        == position.fact_fingerprint
    )
    assert (
        main(_command(project_id, task_id, checklist_file), application=application)
        == 1
    )
    assert json.loads(capsys.readouterr().out)["replayed"] is True
    assert durable_rows(engine) == after


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        "[]",
        "null",
        "{broken",
        '{"check": false}',
        '{" ": "passed"}',
        '{"check": " "}',
        '{"check": "passed", "check": "failed"}',
        '{"check": "passed", " check ": "failed"}',
    ],
)
def test_cli_rejects_invalid_checklist_file_without_durable_mutation(
    content: str,
    completion_engine: tuple[Engine, int, int],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid JSON mappings must be rejected before any workflow write."""
    engine, project_id, task_id = completion_engine
    before = durable_rows(engine)
    checklist_file = tmp_path / "checklist.json"
    checklist_file.write_text(content, encoding="utf-8")
    result = main(
        _command(project_id, task_id, checklist_file), application=_application(engine)
    )
    assert result == _ARGUMENT_ERROR
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert durable_rows(engine) == before
