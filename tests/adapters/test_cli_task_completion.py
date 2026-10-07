"""Exercise lossless checklist transport against real synthetic workflow state."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from sqlmodel import Session, SQLModel, col, create_engine, select

from cli.main import main
from cli.workflow_commands import render_workflow_next
from models.core import Project, Task
from models.enums import TaskStatus
from models.events import TaskExecutionLog
from models.workflow import TaskCompletionEvidence, WorkflowTransitionReceipt
from services.application import (
    AgileForgeApplication,
    CompleteTaskRequest,
    ExecutionActionSelectionService,
)
from services.read_projections import DurableReadProjectionService
from tests.adapters.sprint_retry_fixtures import (
    durable_rows,
    preserves_existing_rows,
    retry_transport_fixture,
)
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.planning_fixtures import planning_guards
from tests.workflow.retry_execution_fixtures import (
    close_retry_sprint,
    close_retry_story,
    complete_retry_task,
    record_pending_successor_plan,
    review_retry_sprint,
    triage_retry_sprint,
)
from tests.workflow.test_sprint_retry_execution import _start_retry
from workflow.clock import SystemClock
from workflow.definitions.execution import execution_graph
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.requests import DecideSprintPlan, StartSprint

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

    from tests.workflow.retry_execution_fixtures import CompletedRetrySource
    from workflow.contracts import JsonObject

_CHECKLIST: dict[str, str] = {
    "Run the documented --ignore=tests/e2e invocation": "exit=0; tests=passed",
    "Confirm A=B=C in the output": "observed=A=B=C",
}
_IDEMPOTENCY_KEY = "cli-equals-completion"
_ARGUMENT_ERROR = 2


def _application(
    engine: Engine, *, domain: WorkflowDomain | None = None
) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=domain
        or WorkflowDomain(engine=engine, graph=execution_graph(), clock=SystemClock()),
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
        read_projection=DurableReadProjectionService(engine=engine),
    )


@pytest.fixture
def completion_engine(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[Engine, int, int]]:
    """Accept the equals-bearing checklist before starting a disposable Sprint."""
    engine = create_engine(f"sqlite:///{tmp_path / 'completion.db'}")
    SQLModel.metadata.create_all(engine)
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(
        engine,
        checklist_items=cast(
            "tuple[str, ...]", getattr(request, "param", tuple(_CHECKLIST))
        ),
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


def _inline_command(project_id: int, task_id: int, *items: str) -> list[str]:
    arguments = _command(project_id, task_id, Path("unused.json"))
    source_offset = arguments.index("--checklist-file")
    arguments[source_offset : source_offset + 2] = [
        f"--checklist-item={item}" for item in items
    ]
    return arguments


def test_cli_index_completion_persists_exact_checklist(
    completion_engine: tuple[Engine, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordinal transport must persist only the accepted exact-text mapping."""
    engine, project_id, task_id = completion_engine
    expected = {
        "Run the documented --ignore=tests/e2e invocation": "met",
        "Confirm A=B=C in the output": "observed=A=B=C",
    }
    assert (
        main(
            _inline_command(project_id, task_id, "1=met", "2=observed=A=B=C"),
            application=_application(engine),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True
    with Session(engine) as session:
        evidence = session.exec(select(TaskCompletionEvidence)).one()
        assert json.loads(evidence.checklist_result_json) == expected
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == _IDEMPOTENCY_KEY
            )
        ).one()
        request = json.loads(receipt.request_json)
        assert request["checklist_result"] == expected
        assert "checklist_items" not in request
        assert "checklist_files" not in request


@pytest.mark.parametrize(
    "items",
    [
        ("0=met",),
        ("-1=met",),
        ("+1=met",),
        ("01=met",),
        ("1.0=met",),
        (".1=met",),
        ("1.=met",),
        ("\u0661=met",),
        ("\uff11=met",),
        ("+\u0661.\u0662=met",),
        ("3=met",),
        ("9" * 5000 + "=met",),
        ("1=met", " 1 =failed"),
        ("1=met", "Unknown criterion=met"),
    ],
)
def test_cli_index_checklist_rejects_invalid_or_ambiguous_entries(
    items: tuple[str, ...],
    completion_engine: tuple[Engine, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed ordinal sources must stop before any durable rejection receipt."""
    engine, project_id, task_id = completion_engine
    before = durable_rows(engine)
    assert (
        main(
            _inline_command(project_id, task_id, *items),
            application=_application(engine),
        )
        == _ARGUMENT_ERROR
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error"].endswith(
        "Valid checklist items:\n"
        "1. Run the documented --ignore=tests/e2e invocation\n"
        "2. Confirm A=B=C in the output"
    )
    assert durable_rows(engine) == before


def test_cli_valid_incomplete_index_coverage_reaches_durable_service_rejection(
    completion_engine: tuple[Engine, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Normalization must never fill missing results or bypass domain coverage."""
    engine, project_id, task_id = completion_engine
    before = durable_rows(engine)
    assert (
        main(
            _inline_command(project_id, task_id, "1=met"),
            application=_application(engine),
        )
        == 1
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error"]["code"] == "WORKFLOW_FACT_CONFLICT"
    assert rejected["error"]["message"].startswith(
        "Checklist result must cover every executable checklist item."
    )
    after = durable_rows(engine)
    for table_name, rows in before.tables.items():
        if table_name != "workflow_transition_receipts":
            assert after.tables[table_name] == rows
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == _IDEMPOTENCY_KEY
            )
        ).one()
        assert json.loads(receipt.request_json)["checklist_result"] == {
            "Run the documented --ignore=tests/e2e invocation": "met"
        }


@pytest.mark.parametrize(
    ("completion_engine", "items", "expected"),
    [
        (("2", "1"), ("1=a", "2=b"), None),
        (("Run tests", "1"), ("1=met", "2=met"), None),
        (("Run tests", "1"), ("Run tests=met", "2=met"), None),
        (("1", "2"), ("1=a", "2=b"), {"1": "a", "2": "b"}),
        (("2", "1"), ("1=met", "2=met"), {"2": "met", "1": "met"}),
        (
            ("Run tests", "1"),
            ("Run tests=failed", "1=met"),
            {"Run tests": "failed", "1": "met"},
        ),
        (("02", "1"), ("02=failed", "1=met"), {"02": "failed", "1": "met"}),
        (
            ("-1", "+2", "1.0", "\u0661"),
            ("-1=a", "+2=b", "1.0=c", "\u0661=d"),
            {"-1": "a", "+2": "b", "1.0": "c", "\u0661": "d"},
        ),
        (("Run tests", "1"), ("1=met",), None),
    ],
    indirect=["completion_engine"],
)
def test_cli_numeric_literal_checklist_preserves_exact_form(
    completion_engine: tuple[Engine, int, int],
    items: tuple[str, ...],
    expected: dict[str, str] | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Complete literal coverage wins except differing complete index readings."""
    engine, project_id, task_id = completion_engine
    before = durable_rows(engine)
    result = main(
        _inline_command(project_id, task_id, *items), application=_application(engine)
    )
    output = json.loads(capsys.readouterr().out)
    if expected is None:
        assert result == _ARGUMENT_ERROR
        assert "--checklist-file" in output["error"]
        assert "Valid checklist items:\n" in output["error"]
        assert durable_rows(engine) == before
    else:
        assert result == 0
        with Session(engine) as session:
            evidence = session.exec(select(TaskCompletionEvidence)).one()
            assert json.loads(evidence.checklist_result_json) == expected


@pytest.mark.parametrize(
    ("completion_engine", "checklist", "expected_exit"),
    [
        (("2", "1"), {"1": "a", "2": "b"}, 0),
        (("Run tests", "1"), {"Run tests": "failed", "1": "met"}, 0),
        (("Run tests", "Check output"), {"1": "met", "2": "met"}, 1),
    ],
    indirect=["completion_engine"],
)
def test_cli_checklist_file_never_interprets_numeric_keys(
    completion_engine: tuple[Engine, int, int],
    checklist: dict[str, str],
    expected_exit: int,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A numeric file key is always literal, even when it resembles an ordinal."""
    engine, project_id, task_id = completion_engine
    checklist_file = tmp_path / "numeric.json"
    checklist_file.write_text(json.dumps(checklist), encoding="utf-8")
    assert (
        main(
            _command(project_id, task_id, checklist_file),
            application=_application(engine),
        )
        == expected_exit
    )
    assert json.loads(capsys.readouterr().out)["ok"] is (expected_exit == 0)
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == _IDEMPOTENCY_KEY
            )
        ).one()
        assert json.loads(receipt.request_json)["checklist_result"] == checklist


@pytest.mark.parametrize(
    ("completion_engine", "expected", "first_form", "replay_form"),
    [
        (tuple(_CHECKLIST), _CHECKLIST, "index", "file"),
        (tuple(_CHECKLIST), _CHECKLIST, "file", "index"),
        (
            ("Run tests", "Check output"),
            {"Run tests": "met", "Check output": "expected=actual"},
            "index",
            "exact",
        ),
        (
            ("Run tests", "Check output"),
            {"Run tests": "met", "Check output": "expected=actual"},
            "exact",
            "index",
        ),
    ],
    indirect=["completion_engine"],
)
def test_cli_completion_cross_form_replay_survives_restart(  # noqa: PLR0913
    completion_engine: tuple[Engine, int, int],
    expected: dict[str, str],
    first_form: str,
    replay_form: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Source-form changes must replay equal evidence after a fresh connection."""
    engine, project_id, task_id = completion_engine
    checklist_file = tmp_path / "replay.json"
    checklist_file.write_text(json.dumps(expected), encoding="utf-8")
    commands = {
        "file": _command(project_id, task_id, checklist_file),
        "index": _inline_command(
            project_id,
            task_id,
            *(
                f"{index}={value}"
                for index, value in enumerate(expected.values(), start=1)
            ),
        ),
        "exact": _inline_command(
            project_id, task_id, *(f"{key}={value}" for key, value in expected.items())
        ),
    }
    for command in commands.values():
        command.extend(("--correlation-id", "cross-form-correlation"))
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert task is not None
        metadata = task.metadata_json
    assert main(commands[first_form], application=_application(engine)) == 0
    applied = json.loads(capsys.readouterr().out)
    after = durable_rows(engine)
    engine.dispose()
    reopened_engine = create_engine(str(engine.url))
    try:
        application = _application(reopened_engine)
        assert main(commands[replay_form], application=application) == 0
        replay = json.loads(capsys.readouterr().out)
        assert replay["replayed"] is True
        assert replay["output"] == applied["output"]
        assert durable_rows(reopened_engine) == after
        with Session(reopened_engine) as session:
            task = session.get(Task, task_id)
            assert task is not None
            assert task.metadata_json == metadata
        changed = [
            "--checklist-item=1=different"
            if token.startswith("--checklist-item=1=")
            else token
            for token in commands["index"]
        ]
        assert main(changed, application=application) == 1
        rejected = json.loads(capsys.readouterr().out)
        assert rejected["error"]["code"] == "WORKFLOW_FACT_CONFLICT"
        assert rejected["replayed"] is False
        assert durable_rows(reopened_engine) == after
    finally:
        reopened_engine.dispose()


def test_cli_pre_upgrade_index_rejection_conflicts_after_normalization(
    completion_engine: tuple[Engine, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An old literal rejection cannot become a successful same-key replay."""
    engine, project_id, task_id = completion_engine
    application = _application(engine)
    rejected = application.complete_task(
        CompleteTaskRequest(
            project_id=project_id,
            instance_key=f"task:{task_id}",
            outcome_summary="The documented invocation executed the tests.",
            artifact_refs=("test-output.txt",),
            acceptance_result="fully_met",
            checklist_result={"1": "met"},
            idempotency_key=_IDEMPOTENCY_KEY,
            actor="synthetic-operator",
        )
    )
    assert rejected.ok is False
    assert rejected.error is not None
    assert rejected.error.message.startswith(
        "Checklist result must cover every executable checklist item."
    )
    before = durable_rows(engine)
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == _IDEMPOTENCY_KEY
            )
        ).one()
        assert json.loads(receipt.request_json)["checklist_result"] == {"1": "met"}
    engine.dispose()
    application = _application(engine)
    command = _inline_command(project_id, task_id, "1=met")
    assert main(command, application=application) == 1
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["error"]["code"] == "WORKFLOW_FACT_CONFLICT"
    assert "idempotency" in conflict["error"]["message"].lower()
    assert conflict["replayed"] is False
    assert durable_rows(engine) == before
    command = _inline_command(project_id, task_id, "1=met", "2=observed=A=B=C")
    command[command.index("--idempotency-key") + 1] = "fresh-normalized-completion"
    assert main(command, application=application) == 0
    assert json.loads(capsys.readouterr().out)["replayed"] is False


def test_cli_retry_and_historical_index_replay_uses_retained_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Old original/retry receipts must replay through later active Sprint scope."""
    engine = create_engine(f"sqlite:///{tmp_path / 'historical-completion.db'}")
    SQLModel.metadata.create_all(engine)
    try:
        fixture = retry_transport_fixture(engine)
        source = fixture.source
        application = fixture.application
        original_command = _original_completion_command(source)
        metadata = _projected_task(
            application, source.project_id, source.first_task_id
        )["metadata_json"]
        closed_rows = durable_rows(engine)
        assert main(original_command, application=application) == 0
        closed_replay = json.loads(capsys.readouterr().out)
        assert closed_replay["replayed"] is True
        assert closed_replay["output"] == source.original_first_task_output
        assert durable_rows(engine) == closed_rows

        retry_id = _start_retry(
            engine,
            source.domain,
            project_id=source.project_id,
            sprint_id=source.source_sprint_id,
            suffix="cli-index-retry",
        )
        retry_command = _inline_command(
            source.project_id, source.first_task_id, "1=passed"
        )
        retry_command[retry_command.index("--instance-key") + 1] = (
            f"retry:{retry_id}:task:{source.first_task_id}"
        )
        retry_command[retry_command.index("--idempotency-key") + 1] = (
            "cli-retry-index-completion"
        )
        retry_command.extend(("--correlation-id", "retry-correlation"))
        assert main(retry_command, application=application) == 0
        retry_applied = json.loads(capsys.readouterr().out)
        assert retry_applied["output"]["retry_attempt_id"] == retry_id
        _finish_retry_and_start_later_sprint(engine, source, retry_id)
        after = durable_rows(engine)
        engine.dispose()
        application = _application(
            engine,
            domain=WorkflowDomain(
                engine=engine, graph=project_graph(), clock=SystemClock()
            ),
        )
        historical = _projected_task(
            application, source.project_id, source.first_task_id
        )
        assert historical["metadata_json"] == metadata
        assert historical["sprint_id"] == source.source_sprint_id
        assert main(original_command, application=application) == 0
        original_replay = json.loads(capsys.readouterr().out)
        assert original_replay["replayed"] is True
        assert original_replay["output"] == source.original_first_task_output
        assert durable_rows(engine) == after
        checklist_file = tmp_path / "retry-replay.json"
        checklist_file.write_text('{"Run focused tests": "passed"}', encoding="utf-8")
        retry_file_command = [
            token
            for token in retry_command
            if not token.startswith("--checklist-item=")
        ]
        retry_file_command.extend(("--checklist-file", str(checklist_file)))
        for command in (retry_file_command, retry_command):
            assert main(command, application=application) == 0
            replay = json.loads(capsys.readouterr().out)
            assert replay["replayed"] is True
            assert replay["output"] == retry_applied["output"]
            assert durable_rows(engine) == after
    finally:
        engine.dispose()


def _original_completion_command(source: CompletedRetrySource) -> list[str]:
    """Replay the fixture's original receipt using its same semantic metadata."""
    original = source.original_first_task_request
    command = _inline_command(source.project_id, source.first_task_id, "1=passed")
    for flag, value in {
        "--outcome-summary": original.outcome_summary,
        "--artifact-ref": original.artifact_refs[0],
        "--acceptance-result": original.acceptance_result,
        "--idempotency-key": original.idempotency_key,
        "--actor": original.actor,
    }.items():
        command[command.index(flag) + 1] = value
    if original.correlation_id is not None:
        command.extend(("--correlation-id", original.correlation_id))
    return command


def _projected_task(
    application: AgileForgeApplication, project_id: int, task_id: int
) -> JsonObject:
    """Assert the selected projection contains one successful typed Task record."""
    result = application.reads.sprint_task_show(project_id=project_id, task_id=task_id)
    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    task = data["task"]
    assert isinstance(task, dict)
    return task


def _finish_retry_and_start_later_sprint(
    engine: Engine, source: CompletedRetrySource, retry_id: int
) -> None:
    """Advance only normal fixture lifecycle transitions to a distinct active Sprint."""
    close_retry_story(
        source.domain,
        project_id=source.project_id,
        retry_id=retry_id,
        story_id=source.first_story_id,
        suffix="cli-index-retry-first",
    )
    complete_retry_task(
        source.domain,
        project_id=source.project_id,
        retry_id=retry_id,
        task_id=source.second_task_id,
        suffix="cli-index-retry-second",
    )
    close_retry_story(
        source.domain,
        project_id=source.project_id,
        retry_id=retry_id,
        story_id=source.second_story_id,
        suffix="cli-index-retry-second",
    )
    review_retry_sprint(
        source.domain,
        project_id=source.project_id,
        retry_id=retry_id,
        sprint_id=source.source_sprint_id,
        suffix="cli-index-retry",
    )
    close_retry_sprint(
        source.domain,
        project_id=source.project_id,
        retry_id=retry_id,
        sprint_id=source.source_sprint_id,
        suffix="cli-index-retry",
    )
    triage_retry_sprint(
        source.domain,
        project_id=source.project_id,
        retry_id=retry_id,
        sprint_id=source.source_sprint_id,
        suffix="cli-index-retry",
    )
    planning_domain, plan_id = record_pending_successor_plan(engine, source)
    position = planning_domain.position(source.project_id)
    review = next(
        item for item in position.decisions if item.node_id == "planning.sprint.review"
    )
    plan_fingerprint = next(
        ref.fingerprint
        for ref in review.fact_references
        if ref.fact_type == "sprint_plan"
    )
    accepted = planning_domain.transition(
        DecideSprintPlan(
            **planning_guards(position, "planning.sprint.review"),
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Accepted later replay fixture Sprint.",
            idempotency_key="cli-index-later-plan-accept",
        )
    )
    assert accepted.ok is True
    assert accepted.output["activated_sprint_id"] != source.source_sprint_id
    assert (
        planning_domain.transition(
            StartSprint(
                **planning_guards(
                    planning_domain.position(source.project_id),
                    "planning.sprint.start",
                ),
                idempotency_key="cli-index-later-sprint-start",
            )
        ).ok
        is True
    )


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
        {
            "Run  the documented --ignore=tests/e2e invocation": "passed",
            "Confirm A=B=C in the output": "passed",
        },
    ],
)
def test_completion_coverage_error_lists_valid_items(
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
    assert rejected["error"]["message"].startswith(
        "Checklist result must cover every executable checklist item."
    )
    assert rejected["error"]["message"].endswith(
        "Valid checklist items:\n"
        "1. Run the documented --ignore=tests/e2e invocation\n"
        "2. Confirm A=B=C in the output"
    )
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
    "items",
    [
        ("missing-separator",),
        (" =passed",),
        ("check= ",),
        ("check=passed", " check =failed"),
    ],
)
def test_cli_malformed_checklist_item_lists_valid_items(
    items: tuple[str, ...],
    completion_engine: tuple[Engine, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Syntax diagnostics may read accepted text but must never persist work."""
    engine, project_id, task_id = completion_engine
    before = durable_rows(engine)
    assert (
        main(
            _inline_command(project_id, task_id, *items),
            application=_application(engine),
        )
        == _ARGUMENT_ERROR
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error"].endswith(
        "Valid checklist items:\n"
        "1. Run the documented --ignore=tests/e2e invocation\n"
        "2. Confirm A=B=C in the output"
    )
    assert durable_rows(engine) == before


def test_cli_malformed_checklist_wrong_project_does_not_leak_items(
    completion_engine: tuple[Engine, int, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real scoped projection must not expose a Task from another project."""
    engine, project_id, task_id = completion_engine
    with Session(engine) as session:
        other_project = Project(name="Other diagnostic project")
        session.add(other_project)
        session.commit()
        session.refresh(other_project)
        assert other_project.project_id is not None
        other_project_id = other_project.project_id
    assert other_project_id != project_id
    before = durable_rows(engine)
    result = main(
        _inline_command(other_project_id, task_id, "missing-separator"),
        application=_application(engine),
    )
    assert result == _ARGUMENT_ERROR
    output = json.loads(capsys.readouterr().out)
    assert "nonblank KEY=VALUE pair" in output["error"]
    assert "Valid checklist items" not in output["error"]
    for item in _CHECKLIST:
        assert item not in output["error"]
    assert durable_rows(engine) == before


def test_checklist_diagnostic_caps_reach_cli_and_durable_service_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both transport paths cap long accepted metadata without mutating work."""
    checklist_items = ("é" * 161, *(f"Criterion {number}" for number in range(2, 23)))
    engine = create_engine(f"sqlite:///{tmp_path / 'capped-completion.db'}")
    SQLModel.metadata.create_all(engine)
    try:
        project_id, _sprint_id, _story_id, task_id = seed_started_execution(
            engine, checklist_items=checklist_items
        )
        application = _application(engine)
        before = durable_rows(engine)
        assert (
            main(
                _inline_command(project_id, task_id, "missing-separator"),
                application=application,
            )
            == _ARGUMENT_ERROR
        )
        syntax_error = json.loads(capsys.readouterr().out)["error"]
        assert durable_rows(engine) == before
        assert (
            main(
                _inline_command(project_id, task_id, "23=met"),
                application=application,
            )
            == _ARGUMENT_ERROR
        )
        index_error = json.loads(capsys.readouterr().out)["error"]
        assert durable_rows(engine) == before
        checklist_file = tmp_path / "incomplete.json"
        checklist_file.write_text('{"Unknown criterion": "met"}', encoding="utf-8")
        assert (
            main(_command(project_id, task_id, checklist_file), application=application)
            == 1
        )
        coverage_error = json.loads(capsys.readouterr().out)["error"]["message"]
        for message in (syntax_error, index_error, coverage_error):
            displayed = message.split("Valid checklist items:\n", maxsplit=1)[1]
            lines = displayed.splitlines()
            assert lines[0] == "1. " + "é" * 159 + "…"
            assert lines[19] == "20. Criterion 20"
            assert lines[20] == "… and 2 more"
            assert len(lines) == 21  # noqa: PLR2004
            assert "Criterion 21" not in message
            assert "é" * 160 not in message
        after = durable_rows(engine)
        for table_name, rows in before.tables.items():
            if table_name != "workflow_transition_receipts":
                assert after.tables[table_name] == rows
        with Session(engine) as session:
            receipt = session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.idempotency_key) == _IDEMPOTENCY_KEY
                )
            ).one()
            assert receipt.result_json is not None
            assert json.loads(receipt.result_json)["error"]["message"] == coverage_error
    finally:
        engine.dispose()


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
