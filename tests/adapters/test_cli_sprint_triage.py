# tests/adapters/test_cli_sprint_triage.py
"""Verify lossless triage transport against cloned disposable workflow state."""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, cast

import pytest
from sqlmodel import Session, SQLModel, col, create_engine, select

from cli.main import main
from models.core import Project
from models.sprint_retry import SprintRetryTriage
from models.workflow import PostSprintTriage, WorkflowTransitionReceipt
from services.application import (
    AgileForgeApplication,
    CloseStoryRequest,
    CompleteTaskRequest,
    ExecutionActionSelectionService,
    SprintCloseRequest,
    SprintReviewRequest,
)
from tests.adapters.sprint_retry_fixtures import durable_rows, preserves_existing_rows
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.execution_retry_support import (
    _complete_execution_sprint,
    _triage_execution_sprint,
)
from tests.workflow.test_sprint_retry_execution import _start_retry
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.execution_integrity import triage_payload_fingerprint
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject

_NOW: datetime = datetime(2026, 9, 9, tzinfo=UTC)
_SUMMARY: str = ' \tRésumé "ação"\nA=B=C 🌱\n '
_KEY: str = "cli-triage-equivalence"
_ARGUMENT_ERROR: int = 2


@dataclass(frozen=True)
class _TriageSeed:
    """A closed database file and the exact selected execution identity."""

    database: Path
    project_id: int
    sprint_id: int
    instance_key: str


@dataclass(frozen=True)
class _StoredTriage:
    """Full stored payload and receipt values, without dropping timestamps."""

    canonical_payload_json: str
    payload_fingerprint: str
    receipt: dict[str, object]


class _RetryMetadata(TypedDict):
    """Common typed metadata for the retry setup's semantic request classes."""

    project_id: int
    actor: str
    correlation_id: str


@contextmanager
def _database(database: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{database}")
    try:
        yield engine
    finally:
        engine.dispose()


def _application(engine: Engine) -> AgileForgeApplication:
    """Build a real provider-free application with a deterministic clock."""
    return AgileForgeApplication(
        workflow_domain=WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=_NOW),
        ),
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )


@pytest.fixture
def triage_seed(tmp_path: Path) -> _TriageSeed:
    """Seed once, dispose, and clone this exact file for equality comparisons."""
    database = tmp_path / "closed-seed.sqlite"
    with _database(database) as engine:
        SQLModel.metadata.create_all(engine)
        _, project_id, sprint_id, _, _, _ = _complete_execution_sprint(engine)
    return _TriageSeed(database, project_id, sprint_id, f"sprint:{sprint_id}")


def _source(tmp_path: Path, source_kind: str, summary: str = _SUMMARY) -> list[str]:
    if source_kind == "summary":
        return ["--summary", summary]
    payload_path = tmp_path / "triage.json"
    payload_path.write_text(json.dumps({"summary": summary}), encoding="utf-8")
    return ["--file", str(payload_path)]


def _arguments(  # noqa: PLR0913
    seed: _TriageSeed,
    source: list[str],
    *,
    key: str = _KEY,
    impact: str = "none",
    project_id: int | None = None,
    instance_key: str | None = None,
) -> list[str]:
    return [
        "sprint",
        "triage",
        "--project-id",
        str(seed.project_id if project_id is None else project_id),
        "--instance-key",
        seed.instance_key if instance_key is None else instance_key,
        "--impact",
        impact,
        *source,
        "--idempotency-key",
        key,
        "--actor",
        "operator@example.com",
        "--correlation-id",
        "triage-correlation",
    ]


def _run(
    application: AgileForgeApplication,
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, JsonObject]:
    exit_code = main(arguments, application=application)
    return exit_code, cast("JsonObject", json.loads(capsys.readouterr().out))


def _stored(engine: Engine, *, retry: bool = False) -> _StoredTriage:
    with Session(engine) as session:
        triage = (
            session.exec(select(SprintRetryTriage)).one()
            if retry
            else session.exec(select(PostSprintTriage)).one()
        )
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == _KEY
            )
        ).one()
        return _StoredTriage(
            triage.canonical_payload_json,
            triage.payload_fingerprint,
            receipt.model_dump(mode="json"),
        )


def test_cli_triage_persists_identical_payload_and_receipt_for_both_sources(
    triage_seed: _TriageSeed,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Compare entire cloned databases, including events and full receipt bytes."""
    stored = []
    outputs = []
    rows = []
    for source_kind in ("file", "summary"):
        database = tmp_path / f"{source_kind}.sqlite"
        shutil.copyfile(triage_seed.database, database)
        with _database(database) as engine:
            before = durable_rows(engine)
            exit_code, result = _run(
                _application(engine),
                _arguments(triage_seed, _source(tmp_path, source_kind)),
                capsys,
            )
            assert exit_code == 0
            assert result["ok"] is True
            assert result["replayed"] is False
            persisted = _stored(engine)
            assert persisted.canonical_payload_json == canonical_json(
                {"summary": _SUMMARY}
            )
            assert persisted.payload_fingerprint == triage_payload_fingerprint(
                "none", {"summary": _SUMMARY}
            )
            request = json.loads(cast("str", persisted.receipt["request_json"]))
            assert request["canonical_payload"] == {"summary": _SUMMARY}
            assert request["instance_key"] == triage_seed.instance_key
            assert request["project_id"] == triage_seed.project_id
            assert request["impact"] == "none"
            assert request["actor"] == "operator@example.com"
            assert request["correlation_id"] == "triage-correlation"
            assert request["idempotency_key"] == _KEY
            assert json.loads(cast("str", persisted.receipt["result_json"])) == result
            after = durable_rows(engine)
            assert preserves_existing_rows(before, after)
            stored.append(persisted)
            outputs.append(result)
            rows.append(after)

    assert stored[0] == stored[1]
    assert outputs[0] == outputs[1]
    assert rows[0].tables["workflow_events"] == rows[1].tables["workflow_events"]
    assert rows[0] == rows[1]


@pytest.mark.parametrize("first_source", ["file", "summary"])
def test_cli_triage_cross_source_replay_survives_restart(
    triage_seed: _TriageSeed,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    first_source: str,
) -> None:
    """Dispose and reopen before replaying the same key through the other source."""
    with _database(triage_seed.database) as engine:
        exit_code, original = _run(
            _application(engine),
            _arguments(triage_seed, _source(tmp_path, first_source)),
            capsys,
        )
        assert exit_code == 0
        before = durable_rows(engine)
    other_source = "summary" if first_source == "file" else "file"
    with _database(triage_seed.database) as restarted:
        exit_code, replay = _run(
            _application(restarted),
            _arguments(triage_seed, _source(tmp_path, other_source)),
            capsys,
        )
        assert exit_code == 0
        assert replay == {**original, "replayed": True}
        assert durable_rows(restarted) == before


@pytest.mark.parametrize("first_source", ["file", "summary"])
def test_cli_triage_changed_summary_with_same_key_preserves_durable_rows(
    triage_seed: _TriageSeed,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    first_source: str,
) -> None:
    """Changed text conflicts without overwriting payload, events, or receipt."""
    with _database(triage_seed.database) as engine:
        exit_code, _ = _run(
            _application(engine),
            _arguments(triage_seed, _source(tmp_path, first_source)),
            capsys,
        )
        assert exit_code == 0
        before = durable_rows(engine)
    other_source = "summary" if first_source == "file" else "file"
    with _database(triage_seed.database) as restarted:
        exit_code, conflict = _run(
            _application(restarted),
            _arguments(
                triage_seed, _source(tmp_path, other_source, _SUMMARY + "changed")
            ),
            capsys,
        )
        assert exit_code == 1
        assert conflict["ok"] is False
        assert conflict["replayed"] is False
        assert cast("JsonObject", conflict["error"])["code"] == "WORKFLOW_FACT_CONFLICT"
        assert durable_rows(restarted) == before


def test_cli_triage_same_payload_with_new_key_remains_rejected(
    triage_seed: _TriageSeed,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A new key is a correction attempt, not a semantic replay of old triage."""
    with _database(triage_seed.database) as engine:
        application = _application(engine)
        exit_code, _ = _run(
            application,
            _arguments(triage_seed, _source(tmp_path, "file")),
            capsys,
        )
        assert exit_code == 0
        before = durable_rows(engine)
        exit_code, rejection = _run(
            application,
            _arguments(triage_seed, _source(tmp_path, "summary"), key="new-key"),
            capsys,
        )
        assert exit_code == 1
        assert rejection["ok"] is False
        assert rejection["replayed"] is False
        after = durable_rows(engine)
        assert preserves_existing_rows(before, after)
        assert after.tables["post_sprint_triage"] == before.tables["post_sprint_triage"]
        assert after.tables["workflow_events"] == before.tables["workflow_events"]
        assert len(after.tables["workflow_transition_receipts"]) == (
            len(before.tables["workflow_transition_receipts"]) + 1
        )


@pytest.mark.parametrize(
    "changed_field",
    ["payload", "impact", "project", "instance", "actor", "correlation"],
)
def test_cli_triage_changed_request_with_same_key_preserves_all_durable_rows(
    triage_seed: _TriageSeed,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changed_field: str,
) -> None:
    """Retain the existing semantic replay identity across payload and metadata."""
    with _database(triage_seed.database) as engine:
        exit_code, _ = _run(
            _application(engine),
            _arguments(triage_seed, _source(tmp_path, "summary")),
            capsys,
        )
        assert exit_code == 0
        before = durable_rows(engine)
    source = _source(tmp_path, "file")
    arguments = _arguments(triage_seed, source)
    changes = {
        "impact": ("--impact", "backlog"),
        "project": ("--project-id", str(triage_seed.project_id + 1)),
        "instance": ("--instance-key", f"retry:999:sprint:{triage_seed.sprint_id}"),
        "actor": ("--actor", "different@example.com"),
        "correlation": ("--correlation-id", "different-correlation"),
    }
    if changed_field == "payload":
        (tmp_path / "triage.json").write_text(
            json.dumps({"summary": _SUMMARY, "nested": {"changed": True}}),
            encoding="utf-8",
        )
    else:
        flag, value = changes[changed_field]
        arguments[arguments.index(flag) + 1] = value
    with _database(triage_seed.database) as restarted:
        exit_code, conflict = _run(_application(restarted), arguments, capsys)
        assert exit_code == 1
        assert conflict["ok"] is False
        assert conflict["replayed"] is False
        assert cast("JsonObject", conflict["error"])["code"] == "WORKFLOW_FACT_CONFLICT"
        assert durable_rows(restarted) == before


@pytest.mark.parametrize(
    ("source", "impact"),
    [
        (["--summary", ""], "none"),
        (["--summary", " \t\n"], "none"),
        (["--summary", "No change.", "--summary", "No change."], "none"),
        (["--summary", "Follow-up."], "backlog"),
        (["--summary", "Follow-up."], "specification"),
        (["--summary", "No change."], "task"),
    ],
)
def test_cli_triage_invalid_inline_preserves_all_durable_rows(
    triage_seed: _TriageSeed,
    capsys: pytest.CaptureFixture[str],
    source: list[str],
    impact: str,
) -> None:
    """Adapter errors cannot create a rejected receipt or any workflow mutation."""
    with _database(triage_seed.database) as engine:
        before = durable_rows(engine)
        exit_code, result = _run(
            _application(engine),
            _arguments(triage_seed, source, impact=impact),
            capsys,
        )
        assert exit_code == _ARGUMENT_ERROR
        assert result["ok"] is False
        assert durable_rows(engine) == before


@pytest.mark.parametrize("wrong_scope", ["project", "sprint", "task", "retry"])
def test_cli_triage_wrong_scope_preserves_all_durable_rows(
    triage_seed: _TriageSeed,
    capsys: pytest.CaptureFixture[str],
    wrong_scope: str,
) -> None:
    """Summary input cannot bypass real application project and instance selection."""
    project_id = None
    instance_key = {
        "sprint": f"sprint:{triage_seed.sprint_id + 1}",
        "task": "task:1",
        "retry": f"retry:999:sprint:{triage_seed.sprint_id}",
    }.get(wrong_scope)
    with _database(triage_seed.database) as engine:
        if wrong_scope == "project":
            with Session(engine) as session:
                other_project = Project(name="Other triage project")
                session.add(other_project)
                session.commit()
                session.refresh(other_project)
                assert other_project.project_id is not None
                project_id = other_project.project_id
            assert project_id != triage_seed.project_id
        before = durable_rows(engine)
        exit_code, result = _run(
            _application(engine),
            _arguments(
                triage_seed,
                ["--summary", _SUMMARY],
                project_id=project_id,
                instance_key=instance_key,
            ),
            capsys,
        )
        assert exit_code == 1
        assert result["ok"] is False
        assert durable_rows(engine) == before


def test_cli_triage_missing_closure_preserves_all_durable_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A started Sprint without closure remains unavailable to real triage."""
    database = tmp_path / "unclosed.sqlite"
    with _database(database) as engine:
        SQLModel.metadata.create_all(engine)
        project_id, sprint_id, _, _ = seed_started_execution(engine)
        seed = _TriageSeed(database, project_id, sprint_id, f"sprint:{sprint_id}")
        before = durable_rows(engine)
        exit_code, result = _run(
            _application(engine), _arguments(seed, ["--summary", _SUMMARY]), capsys
        )
        assert exit_code == 1
        assert result["ok"] is False
        assert durable_rows(engine) == before


def _retry_seed(database: Path, *, close: bool) -> _TriageSeed:
    """Close the exact retry through the established semantic application pattern."""
    with _database(database) as engine:
        SQLModel.metadata.create_all(engine)
        original, project_id, sprint_id, story_id, task_id, _ = (
            _complete_execution_sprint(engine)
        )
        _triage_execution_sprint(original, project_id=project_id, sprint_id=sprint_id)
        application = _application(engine)
        domain = WorkflowDomain(
            engine=engine, graph=project_graph(), clock=FixedClock(now_value=_NOW)
        )
        retry_id = _start_retry(
            engine,
            domain,
            project_id=project_id,
            sprint_id=sprint_id,
            suffix="cli-triage",
        )
        metadata: _RetryMetadata = {
            "project_id": project_id,
            "actor": "operator@example.com",
            "correlation_id": "retry-seed",
        }
        assert application.complete_task(
            CompleteTaskRequest(
                **metadata,
                instance_key=f"retry:{retry_id}:task:{task_id}",
                idempotency_key="retry-triage-task",
                outcome_summary="Retry completion.",
                artifact_refs=("workflow/definitions/execution.py",),
                acceptance_result="fully_met",
                checklist_result={"Run focused tests": "passed"},
            )
        ).ok
        assert application.close_story(
            CloseStoryRequest(
                **metadata,
                instance_key=f"retry:{retry_id}:story:{story_id}",
                idempotency_key="retry-triage-story",
                resolution="Completed",
                delivered="Retry closure.",
                evidence="Focused retry tests pass.",
                known_gaps="None.",
            )
        ).ok
        instance_key = f"retry:{retry_id}:sprint:{sprint_id}"
        assert application.review_sprint(
            SprintReviewRequest(
                **metadata,
                instance_key=instance_key,
                idempotency_key="retry-triage-review",
            )
        ).ok
        if close:
            assert application.close_sprint(
                SprintCloseRequest(
                    **metadata,
                    instance_key=instance_key,
                    idempotency_key="retry-triage-close",
                )
            ).ok
    return _TriageSeed(database, project_id, sprint_id, instance_key)


def test_cli_retry_triage_summary_preserves_retry_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Compare retry receipts and rows while retaining original triage lineage."""
    seed = _retry_seed(tmp_path / "retry-seed.sqlite", close=True)
    stored = []
    rows = []
    outputs = []
    for source_kind in ("file", "summary"):
        database = tmp_path / f"retry-{source_kind}.sqlite"
        shutil.copyfile(seed.database, database)
        with _database(database) as engine:
            before = durable_rows(engine)
            exit_code, result = _run(
                _application(engine),
                _arguments(seed, _source(tmp_path, source_kind)),
                capsys,
            )
            assert exit_code == 0
            assert result["ok"] is True
            assert result["replayed"] is False
            output = cast("JsonObject", result["output"])
            assert output["retry_attempt_id"] == int(seed.instance_key.split(":")[1])
            persisted = _stored(engine, retry=True)
            assert persisted.canonical_payload_json == canonical_json(
                {"summary": _SUMMARY}
            )
            assert persisted.payload_fingerprint == triage_payload_fingerprint(
                "none", {"summary": _SUMMARY}
            )
            request = json.loads(cast("str", persisted.receipt["request_json"]))
            assert request["instance_key"] == seed.instance_key
            assert request["sprint_id"] == seed.sprint_id
            assert request["canonical_payload"] == {"summary": _SUMMARY}
            assert json.loads(cast("str", persisted.receipt["result_json"])) == result
            after = durable_rows(engine)
            assert preserves_existing_rows(before, after)
            assert (
                after.tables["post_sprint_triage"]
                == before.tables["post_sprint_triage"]
            )
            assert (
                after.tables["sprint_retry_closures"]
                == before.tables["sprint_retry_closures"]
            )
            stored.append(persisted)
            rows.append(after)
            outputs.append(result)

    assert stored[0] == stored[1]
    assert outputs[0] == outputs[1]
    assert rows[0] == rows[1]


def test_cli_retry_triage_requires_its_own_closure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The original Sprint closure cannot authorize an unclosed retry triage."""
    seed = _retry_seed(tmp_path / "retry-unclosed.sqlite", close=False)
    with _database(seed.database) as engine:
        before = durable_rows(engine)
        exit_code, result = _run(
            _application(engine), _arguments(seed, ["--summary", _SUMMARY]), capsys
        )
        assert exit_code == 1
        assert result["ok"] is False
        assert durable_rows(engine) == before
