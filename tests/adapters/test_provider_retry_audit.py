# tests/adapters/test_provider_retry_audit.py
"""Durability and integrity at the provider audit repository seam."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlmodel import Session, SQLModel, col, create_engine, select

from models.core import Project
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import WorkflowNodeAttempt
from repositories.provider_attempts import ProviderAttemptAuditRepository
from repositories.workflow import WorkflowFactRepository
from services.contracts.provider_retry import (
    ProviderAuditError,
    ProviderFailureSummary,
    ProviderRetryConfig,
    ProviderTryAuditRecord,
)
from tests.adapters.test_provider_boundary_routes import (
    BackoffClock,
    boundary,
    error,
    invoke,
)
from workflow.fingerprints import business_fact_fingerprint, fact_fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
_PAIR_EVENT_COUNT = 2
_TWO_TRIES_EVENT_COUNT = 4
_LARGE_HISTORY_ROWS: int = 12000
_MAX_TRIES: int = 10


def _assert_prepared_plan(statement: str, details: list[str]) -> str:
    assert not any(
        "ix_workflow_events_event_type" in detail
        or "SCAN workflow_events" in detail
        or "TEMP B-TREE" in detail
        for detail in details
    ), details
    if "UNION ALL" in statement:
        for identity in ("action", "call"):
            for ownership in ("<?", ">?", "=?"):
                assert any(
                    f"ix_workflow_events_provider_{identity}" in detail
                    and f"<expr>=? AND project_id{ownership}" in detail
                    for detail in details
                ), details
        return "global"
    if "$.action_id" in statement or "$.call_id" in statement:
        identity = "action" if "$.action_id" in statement else "call"
        assert any(
            f"ix_workflow_events_provider_{identity}" in detail
            and "<expr>=? AND project_id=?" in detail
            for detail in details
        ), details
        return identity
    assert any(
        "ix_workflow_events_provider_invalid" in detail and "project_id=?" in detail
        for detail in details
    ), details
    return "invalid"


@pytest.mark.parametrize(
    "operation", ["start", "finish", "start_replay", "finish_replay", "terminal"]
)
@pytest.mark.parametrize(
    ("history_size", "history_identity"),
    [(0, "empty")]
    + [
        (size, kind)
        for size in (1000, 4000, 12000)
        for kind in ("unrelated", "same_action")
    ],
)
def test_provider_operations_use_identity_constrained_prepared_queries(
    audit_database: tuple[Engine, int],
    operation: str,
    history_size: int,
    history_identity: str,
) -> None:
    """Every prepared audit lookup seeks identity and owner, never provider history."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    finished = _finish(started)
    if history_size:
        _seed_provider_history(
            engine,
            project_id,
            history_size,
            same_action=history_identity == "same_action",
        )
    if operation != "start":
        repository.append_started(started)
    if operation in {"finish_replay", "terminal"}:
        repository.append_finished(finished)
    captured: list[tuple[str, tuple[object, ...]]] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.startswith("SELECT") and "json_valid(" in statement:
            captured.append((statement, cast("tuple[object, ...]", parameters)))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        if operation in {"start", "start_replay"}:
            repository.append_started(started)
        elif operation in {"finish", "finish_replay"}:
            repository.append_finished(finished)
        else:
            assert (
                repository.terminal_failure(
                    project_id=project_id, action_id="action-a", call_id="call-a"
                )
                is not None
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert captured
    found: set[str] = set()
    with engine.connect() as connection:
        for statement, parameters in captured:
            details = [
                str(row[3])
                for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                ).all()
            ]
            found.add(_assert_prepared_plan(statement, details))
    expected = {"global", "call", "invalid"}
    if operation != "terminal":
        expected.add("action")
    assert found == expected


def _seed_provider_history(
    engine: Engine, project_id: int, size: int, *, same_action: bool = False
) -> None:
    """Populate synthetic old calls without sending to a provider."""
    template = _start(project_id).model_dump(mode="json")
    rows = [
        (
            "PROVIDER_TRY_STARTED",
            project_id,
            json.dumps(
                {
                    **template,
                    "action_id": "action-a"
                    if same_action
                    else f"history-action-{ordinal}",
                    "call_id": f"history-call-{ordinal}",
                }
            ),
        )
        for ordinal in range(size)
    ]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO workflow_events (event_type, project_id, event_metadata) "
            "VALUES (?, ?, ?)",
            rows,
        )


@pytest.mark.parametrize("identity", ["action", "call"])
@pytest.mark.parametrize("owner", ["foreign", "null"])
def test_old_foreign_identity_is_never_hidden_by_later_history(
    audit_database: tuple[Engine, int], identity: str, owner: str
) -> None:
    """Old action/call collisions, including NULL owners, survive growing history."""
    engine, project_id = audit_database
    with Session(engine) as session:
        foreign = Project(name="Foreign old audit")
        session.add(foreign)
        session.commit()
        assert foreign.project_id is not None
        foreign_id = foreign.project_id
    collision = _start(
        foreign_id,
        action_id="action-a" if identity == "action" else "foreign-action",
        call_id="call-a" if identity == "call" else "foreign-call",
        started_at=NOW - timedelta(days=3650),
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO workflow_events (event_type, project_id, event_metadata) "
            "VALUES (?, ?, ?)",
            (
                "PROVIDER_TRY_STARTED",
                foreign_id if owner == "foreign" else None,
                collision.model_dump_json(),
            ),
        )
    _seed_provider_history(engine, project_id, _LARGE_HISTORY_ROWS)
    repository = ProviderAttemptAuditRepository(engine)
    with pytest.raises(ProviderAuditError):
        repository.append_started(_start(project_id))
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM workflow_events"
            ).scalar_one()
            == _LARGE_HISTORY_ROWS + 1
        )


@pytest.mark.parametrize("metadata", [None, "{late malformed JSON"])
def test_late_malformed_local_evidence_blocks_a_complete_twenty_row_call(
    audit_database: tuple[Engine, int], metadata: str | None
) -> None:
    """Damaged local history after a full ten-try call still fails every operation."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    config = ProviderRetryConfig(max_attempts=_MAX_TRIES)
    first: ProviderTryAuditRecord | None = None
    last: ProviderTryAuditRecord | None = None
    for ordinal in range(1, _MAX_TRIES + 1):
        started = _start(
            project_id,
            try_ordinal=ordinal,
            max_attempts=_MAX_TRIES,
            retry_config=config,
            started_at=NOW + timedelta(seconds=(ordinal - 1) * 2),
        )
        if first is None:
            first = started
        finished = _finish(started)
        if ordinal < _MAX_TRIES:
            finished = _finish(
                started,
                disposition="retry_scheduled",
                termination_reason=None,
                selected_delay_seconds=1.0,
                next_eligible_retry_at=started.started_at + timedelta(seconds=2),
            )
        repository.append_started(started)
        repository.append_finished(finished)
        last = finished
    assert first is not None
    assert last is not None
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO workflow_events (event_type, project_id, event_metadata) "
            "VALUES (?, ?, ?)",
            ("PROVIDER_TRY_STARTED", project_id, metadata),
        )
    with pytest.raises(ProviderAuditError):
        repository.append_started(first)
    with pytest.raises(ProviderAuditError):
        repository.append_finished(last)
    with pytest.raises(ProviderAuditError):
        repository.append_started(_start(project_id, action_id="new", call_id="new"))
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )


@pytest.mark.parametrize("operation", ["start", "terminal"])
@pytest.mark.parametrize("index_state", ["missing", "drifted"])
def test_unprepared_supplied_engine_fails_before_provider_operation(
    audit_database: tuple[Engine, int], operation: str, index_state: str
) -> None:
    """An explicit unprepared engine fails locally without installing or scanning."""
    engine, project_id = audit_database
    with engine.begin() as connection:
        if index_state == "missing":
            for name in ("action", "call", "invalid"):
                connection.exec_driver_sql(
                    f"DROP INDEX ix_workflow_events_provider_{name}"
                )
        else:
            connection.exec_driver_sql("DROP INDEX ix_workflow_events_provider_action")
            connection.exec_driver_sql(
                "CREATE INDEX ix_workflow_events_provider_action "
                "ON workflow_events(project_id, event_id)"
            )
    repository = ProviderAttemptAuditRepository(engine)
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        if operation == "start":
            with pytest.raises(ProviderAuditError):
                repository.append_started(_start(project_id))
        else:
            with pytest.raises(ProviderAuditError):
                repository.terminal_failure(
                    project_id=project_id, action_id="action-a", call_id="call-a"
                )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("json_valid(" in sql or "CREATE " in sql for sql in statements)
    assert _events(engine) == []


@pytest.fixture
def audit_database(tmp_path: Path) -> Iterator[tuple[Engine, int]]:
    """Own a disposable file database with genuinely independent connections."""
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="Audit project")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    try:
        yield engine, project_id
    finally:
        engine.dispose()


def _start(project_id: int, **overrides: object) -> ProviderTryAuditRecord:
    return ProviderTryAuditRecord.model_validate(
        {
            "project_id": project_id,
            "action_id": "action-a",
            "call_id": "call-a",
            "model_id": "openrouter/test/model",
            "request_fingerprint": "a" * 64,
            "try_ordinal": 1,
            "max_attempts": 1,
            "retry_config": ProviderRetryConfig(max_attempts=1),
            "started_at": NOW,
            **overrides,
        }
    )


def _finish(
    started: ProviderTryAuditRecord, **overrides: object
) -> ProviderTryAuditRecord:
    return ProviderTryAuditRecord.model_validate(
        {
            **started.model_dump(),
            "finished_at": started.started_at + timedelta(seconds=1),
            "duration_seconds": 1.0,
            "http_status": 429,
            "retry_classification": "rate_limited",
            "disposition": "exhausted",
            "termination_reason": "attempts_exhausted",
            **overrides,
        }
    )


def _events(engine: Engine) -> list[WorkflowEvent]:
    with Session(engine) as session:
        return list(
            session.exec(select(WorkflowEvent).order_by(col(WorkflowEvent.event_id)))
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 429])
async def test_backoff_cancellation_persists_an_unsent_cancelled_pair(
    audit_database: tuple[Engine, int], status: int
) -> None:
    """Cancellation preserves the scheduled send and durably closes its retry."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    clock = BackoffClock()
    sends: list[dict[str, object]] = []

    async def send(**kwargs: object) -> object:
        """Return a retryable response from one injected provider send."""
        sends.append(kwargs)
        raise error(status)

    module = boundary()
    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, high: high
    )
    context = module.ProviderActionContext(
        project_id=project_id,
        action_id="cancelled-backoff-action",
        policy=ProviderRetryConfig(max_attempts=3),
        audit=repository,
        action_deadline=clock.value + 120.0,
        pre_try_check=lambda: None,
        idempotency_key_digest="b" * 64,
    )
    with module.bind_provider_action(context):
        task = asyncio.create_task(invoke(client))
        await asyncio.wait_for(clock.backoff_started.wait(), timeout=1.0)
        scheduled_events = _events(engine)
        scheduled_metadata = [event.event_metadata for event in scheduled_events]
        clock.value += 7.0
        task.cancel("synthetic backoff cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task

    assert caught.value is clock.cancellation
    assert len(sends) == 1
    assert clock.waits == [1.0]
    events = _events(engine)
    assert [event.event_metadata for event in events[:2]] == scheduled_metadata
    assert [event.event_type for event in events] == [
        WorkflowEventType.PROVIDER_TRY_STARTED,
        WorkflowEventType.PROVIDER_TRY_FINISHED,
        WorkflowEventType.PROVIDER_TRY_STARTED,
        WorkflowEventType.PROVIDER_TRY_FINISHED,
    ]
    first, scheduled, cancelled_start, cancelled_finish = [
        ProviderTryAuditRecord.model_validate_json(event.event_metadata or "")
        for event in events
    ]
    assert scheduled.disposition == "retry_scheduled"
    assert scheduled.http_status == status
    assert cancelled_start == ProviderTryAuditRecord.model_validate(
        first.model_dump() | {"try_ordinal": 2, "started_at": clock.utc_now()}
    )
    assert cancelled_finish == ProviderTryAuditRecord.model_validate(
        cancelled_start.model_dump()
        | {
            "finished_at": clock.utc_now(),
            "duration_seconds": 0.0,
            "retry_classification": "cancelled",
            "disposition": "cancelled",
        }
    )
    assert (
        repository.terminal_failure(
            project_id=project_id,
            action_id=context.action_id,
            call_id=first.call_id,
        )
        is None
    )


def test_committed_pair_and_terminal_summary_survive_database_reopen(
    tmp_path: Path,
) -> None:
    """Physical sends have durable matching evidence beyond a recipe lifetime."""
    url = f"sqlite:///{tmp_path / 'audit.sqlite3'}"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="Provider audit")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    started = ProviderTryAuditRecord(
        project_id=project_id,
        action_id="action-a",
        call_id="call-a",
        model_id="openrouter/test/model",
        request_fingerprint="a" * 64,
        try_ordinal=1,
        max_attempts=1,
        retry_config=ProviderRetryConfig(max_attempts=1),
        started_at=NOW,
    )
    finished = ProviderTryAuditRecord.model_validate(
        {
            **started.model_dump(),
            "finished_at": NOW + timedelta(seconds=1),
            "duration_seconds": 1.0,
            "http_status": 429,
            "retry_classification": "rate_limited",
            "disposition": "exhausted",
            "termination_reason": "attempts_exhausted",
        }
    )
    repository = ProviderAttemptAuditRepository(engine)
    repository.append_started(started)
    repository.append_finished(finished)
    engine.dispose()

    reopened = create_engine(url)
    try:
        assert ProviderAttemptAuditRepository(reopened).terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        ) == ProviderFailureSummary(
            reason="rate_limited",
            termination_reason="attempts_exhausted",
            http_status=429,
            call_id="call-a",
            attempts=1,
            max_attempts=1,
            manual_retry_requires_new_key=False,
        )
    finally:
        reopened.dispose()


def test_independent_writers_append_identical_records_once(
    audit_database: tuple[Engine, int],
) -> None:
    """Serialized independent writers preserve exactly one logical try pair."""
    engine, project_id = audit_database
    other = create_engine(engine.url)
    started = _start(project_id)
    finished = _finish(started)
    barrier = Barrier(2)

    def append(connection_engine: Engine) -> None:
        repository = ProviderAttemptAuditRepository(connection_engine)
        barrier.wait(timeout=5)
        repository.append_started(started)
        repository.append_finished(finished)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(append, current) for current in (engine, other)]
            for future in futures:
                future.result(timeout=10)
        assert [event.event_type for event in _events(engine)] == [
            WorkflowEventType.PROVIDER_TRY_STARTED,
            WorkflowEventType.PROVIDER_TRY_FINISHED,
        ]
        with pytest.raises(ProviderAuditError):
            ProviderAttemptAuditRepository(other).append_finished(
                _finish(started, http_status=503, retry_classification="unavailable")
            )
        with pytest.raises(ProviderAuditError):
            ProviderAttemptAuditRepository(other).append_started(
                _start(project_id, request_fingerprint="b" * 64)
            )
        assert len(_events(engine)) == _PAIR_EVENT_COUNT
    finally:
        other.dispose()


def test_host_action_cannot_cross_projects_even_with_a_new_call(
    audit_database: tuple[Engine, int],
) -> None:
    """A provider call cannot rebind its host action to an unrelated Project."""
    engine, project_id = audit_database
    with Session(engine) as session:
        other_project = Project(name="Unrelated")
        session.add(other_project)
        session.commit()
        assert other_project.project_id is not None
        other_project_id = other_project.project_id
    repository = ProviderAttemptAuditRepository(engine)
    repository.append_started(_start(project_id))
    with pytest.raises(ProviderAuditError):
        repository.append_started(_start(other_project_id))
    with pytest.raises(ProviderAuditError):
        repository.append_started(_start(other_project_id, call_id="call-b"))
    assert len(_events(engine)) == 1


def test_host_node_identity_is_validated_before_writing(
    audit_database: tuple[Engine, int],
) -> None:
    """Project, attempt, node, instance and existing key remain host-owned."""
    engine, project_id = audit_database
    with Session(engine) as session:
        attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id="backlog.generate",
            instance_key="requirement:REQ-A",
            graph_version="test",
            fact_fingerprint="b" * 64,
            business_fact_fingerprint="c" * 64,
            decision_fingerprint="d" * 64,
            normalized_input_json="{}",
            input_fingerprint="e" * 64,
            model_id="openrouter/test/model",
            execution_settings_json="{}",
            idempotency_key="existing-key",
            actor="host",
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint="f" * 64,
        )
        session.add(attempt)
        session.commit()
        assert attempt.workflow_node_attempt_id is not None
        attempt_id = attempt.workflow_node_attempt_id
    association: dict[str, object] = {
        "workflow_node_attempt_id": attempt_id,
        "attempt_fingerprint": "f" * 64,
        "node_id": "backlog.generate",
        "instance_key": "requirement:REQ-A",
        "idempotency_key_digest": sha256(b"existing-key").hexdigest(),
    }
    repository = ProviderAttemptAuditRepository(engine)
    for field, wrong_value in (
        ("workflow_node_attempt_id", attempt_id + 100),
        ("attempt_fingerprint", "a" * 64),
        ("node_id", "vision.generate"),
        ("instance_key", "requirement:REQ-B"),
        ("idempotency_key_digest", "a" * 64),
    ):
        with pytest.raises(ProviderAuditError):
            repository.append_started(
                _start(project_id, **{**association, field: wrong_value})
            )
    assert _events(engine) == []
    started = _start(project_id, **association)
    repository.append_started(started)
    repository.append_finished(_finish(started))
    summary = repository.terminal_failure(
        project_id=project_id, action_id="action-a", call_id="call-a"
    )
    assert summary is not None
    assert summary.manual_retry_requires_new_key is True


def test_audit_survives_unrelated_recipe_rollback_without_business_changes(
    audit_database: tuple[Engine, int],
) -> None:
    """Recipe rollback preserves audit and both workflow fact fingerprints."""
    engine, project_id = audit_database
    with Session(engine) as session:
        before = WorkflowFactRepository(session).load(project_id)
    with Session(engine) as recipe:
        recipe.get(Project, project_id)
        recipe.add(Project(name="Unpublished recipe work"))
        repository = ProviderAttemptAuditRepository(engine)
        started = _start(project_id)
        repository.append_started(started)
        repository.append_finished(_finish(started))
        recipe.rollback()
    with Session(engine) as session:
        after = WorkflowFactRepository(session).load(project_id)
        assert len(session.exec(select(Project)).all()) == 1
    assert business_fact_fingerprint(after) == business_fact_fingerprint(before)
    assert fact_fingerprint(after) == fact_fingerprint(before)
    assert len(_events(engine)) == _PAIR_EVENT_COUNT


def test_budget_capped_final_try_keeps_prior_same_call_confirmed_status(
    audit_database: tuple[Engine, int],
) -> None:
    """A truthful null final status still proves two actual sends and prior 503."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    config = ProviderRetryConfig(max_attempts=3)
    first = _start(project_id, max_attempts=3, retry_config=config)
    repository.append_started(first)
    repository.append_finished(
        _finish(
            first,
            http_status=503,
            retry_classification="unavailable",
            disposition="retry_scheduled",
            termination_reason=None,
            selected_delay_seconds=4.0,
            retry_after_seconds=4.0,
            next_eligible_retry_at=NOW + timedelta(seconds=5),
        )
    )
    second = _start(
        project_id,
        try_ordinal=2,
        max_attempts=3,
        retry_config=config,
        started_at=NOW + timedelta(seconds=5),
    )
    repository.append_started(second)
    repository.append_finished(
        _finish(
            second,
            http_status=None,
            retry_classification=None,
            termination_reason="retry_budget_exhausted",
        )
    )
    assert repository.terminal_failure(
        project_id=project_id, action_id="action-a", call_id="call-a"
    ) == ProviderFailureSummary(
        reason="unavailable",
        termination_reason="retry_budget_exhausted",
        http_status=503,
        call_id="call-a",
        attempts=2,
        max_attempts=3,
        retry_after_seconds=4.0,
        manual_retry_requires_new_key=False,
    )
    assert len(_events(engine)) == _TWO_TRIES_EVENT_COUNT


@pytest.mark.parametrize("finished", [False, True])
def test_audit_write_failures_are_local_and_preserve_committed_evidence(
    audit_database: tuple[Engine, int],
    *,
    finished: bool,
) -> None:
    """A database refusing either write yields safe ProviderAuditError evidence."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    if finished:
        repository.append_started(started)
    event_name = "PROVIDER_TRY_FINISHED" if finished else "PROVIDER_TRY_STARTED"
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER refuse_audit BEFORE INSERT ON workflow_events "
            f"WHEN NEW.event_type = '{event_name}' BEGIN "
            "SELECT RAISE(ABORT, 'private simulated failure'); END"
        )
    append = repository.append_finished if finished else repository.append_started
    record = _finish(started) if finished else started
    with pytest.raises(ProviderAuditError) as caught:
        append(record)
    assert (
        str(caught.value) == "Provider attempt audit could not be recorded or verified."
    )
    assert len(_events(engine)) == int(finished)
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        is None
    )


def test_incomplete_try_is_retained_and_cannot_be_skipped(
    audit_database: tuple[Engine, int],
) -> None:
    """A crash's started record is evidence, never fabricated completion."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    config = ProviderRetryConfig(max_attempts=3)
    started = _start(project_id, max_attempts=3, retry_config=config)
    repository.append_started(started)
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        is None
    )
    with pytest.raises(ProviderAuditError):
        repository.append_started(
            _start(project_id, try_ordinal=2, max_attempts=3, retry_config=config)
        )
    assert len(_events(engine)) == 1


def test_metadata_is_frozen_and_rejects_unallowlisted_content(
    audit_database: tuple[Engine, int],
) -> None:
    """Prompts, responses, headers, exception text and endpoint queries stay out."""
    engine, project_id = audit_database
    started = _start(project_id)
    for forbidden in ("prompt", "response", "headers", "exception_text", "endpoint"):
        with pytest.raises(ValidationError):
            _start(project_id, **{forbidden: "synthetic private content"})
    with pytest.raises(ValidationError):
        _start(project_id, model_id="openrouter/model?api_key=synthetic")
    with pytest.raises(ValidationError):
        started.call_id = "other"  # type: ignore[misc]
    ProviderAttemptAuditRepository(engine).append_started(started)
    metadata = _events(engine)[0].event_metadata
    assert metadata is not None
    assert "synthetic private content" not in metadata
    assert ProviderTryAuditRecord.model_validate_json(metadata) == started


@pytest.mark.parametrize("missing_ordinal", [1, 2])
def test_terminal_summary_rejects_missing_started_evidence(
    audit_database: tuple[Engine, int],
    missing_ordinal: int,
) -> None:
    """Summary send counts require every actual try's durable matching start."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    config = ProviderRetryConfig(max_attempts=2)
    first = _start(project_id, max_attempts=2, retry_config=config)
    repository.append_started(first)
    repository.append_finished(
        _finish(
            first,
            disposition="retry_scheduled",
            termination_reason=None,
            selected_delay_seconds=1.0,
            next_eligible_retry_at=NOW + timedelta(seconds=2),
        )
    )
    second = _start(
        project_id,
        try_ordinal=2,
        max_attempts=2,
        retry_config=config,
        started_at=NOW + timedelta(seconds=2),
    )
    repository.append_started(second)
    repository.append_finished(_finish(second))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM workflow_events WHERE event_type='PROVIDER_TRY_STARTED' "
            "AND json_extract(event_metadata, '$.try_ordinal') = ?",
            (missing_ordinal,),
        )
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )


def test_null_terminal_status_cannot_borrow_confirmation_from_another_call(
    audit_database: tuple[Engine, int],
) -> None:
    """Only same-call provider response evidence can justify temporary exhaustion."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    other = _start(project_id, call_id="call-other")
    repository.append_started(other)
    repository.append_finished(_finish(other))
    started = _start(project_id)
    repository.append_started(started)
    repository.append_finished(
        _finish(
            started,
            http_status=None,
            retry_classification=None,
            termination_reason="retry_budget_exhausted",
        )
    )
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )


def test_terminal_summary_rejects_invented_attempt_exhaustion_count(
    audit_database: tuple[Engine, int],
) -> None:
    """One physical send cannot be represented as three-attempt exhaustion."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(
        project_id, max_attempts=3, retry_config=ProviderRetryConfig(max_attempts=3)
    )
    repository.append_started(started)
    repository.append_finished(_finish(started))
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )


def test_persisted_metadata_has_only_pinned_safe_fields(
    audit_database: tuple[Engine, int],
) -> None:
    """Stored metadata exposes no free-form provider response or credential carrier."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    repository.append_started(started)
    metadata = _events(engine)[0].event_metadata
    assert metadata is not None
    assert set(json.loads(metadata)) == {
        "schema_version",
        "provider",
        "project_id",
        "action_id",
        "call_id",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "node_id",
        "instance_key",
        "idempotency_key_digest",
        "model_id",
        "request_fingerprint",
        "try_ordinal",
        "max_attempts",
        "retry_config",
        "started_at",
        "finished_at",
        "duration_seconds",
        "http_status",
        "retry_classification",
        "disposition",
        "termination_reason",
        "retry_after_seconds",
        "selected_delay_seconds",
        "next_eligible_retry_at",
    }


def test_terminal_summary_rejects_finish_committed_before_start(
    audit_database: tuple[Engine, int],
) -> None:
    """Matching content alone cannot prove that a send had prior durable evidence."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    finished = _finish(started)
    repository.append_started(started)
    repository.append_finished(finished)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE workflow_events SET event_type = ?, event_metadata = ? "
            "WHERE event_id = ?",
            [
                ("PROVIDER_TRY_FINISHED", finished.model_dump_json(), 1),
                ("PROVIDER_TRY_STARTED", started.model_dump_json(), 2),
            ],
        )
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )


def test_each_append_releases_its_write_transaction_before_return(
    audit_database: tuple[Engine, int],
) -> None:
    """The caller can wait while another connection owns the SQLite writer."""
    engine, project_id = audit_database
    independent = create_engine(engine.url)
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    try:
        repository.append_started(started)
        with independent.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.rollback()
        repository.append_finished(_finish(started))
        with independent.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.rollback()
        assert len(_events(engine)) == _PAIR_EVENT_COUNT
    finally:
        independent.dispose()


@pytest.mark.parametrize(
    ("disposition", "classification"),
    [("success", None), ("non_retryable", "non_retryable"), ("cancelled", "cancelled")],
)
def test_ordinary_terminal_outcomes_do_not_become_transient_summaries(
    audit_database: tuple[Engine, int],
    disposition: str,
    classification: str | None,
) -> None:
    """Success, permanent failure and cancellation retain their truthful audit."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    repository.append_started(started)
    repository.append_finished(
        _finish(
            started,
            http_status=None,
            retry_classification=classification,
            disposition=disposition,
            termination_reason=None,
        )
    )
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        is None
    )
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="wrong-action", call_id="call-a"
        )


@pytest.mark.parametrize("classification", ["cancelled", "non_retryable"])
def test_null_status_exhaustion_rejects_contradictory_failure_classification(
    audit_database: tuple[Engine, int],
    classification: str,
) -> None:
    """Cancellation and permanent failure cannot become temporary exhaustion."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    config = ProviderRetryConfig(max_attempts=3)
    first = _start(project_id, max_attempts=3, retry_config=config)
    repository.append_started(first)
    repository.append_finished(
        _finish(
            first,
            http_status=503,
            retry_classification="unavailable",
            disposition="retry_scheduled",
            termination_reason=None,
            selected_delay_seconds=1.0,
            next_eligible_retry_at=NOW + timedelta(seconds=2),
        )
    )
    second = _start(
        project_id,
        try_ordinal=2,
        max_attempts=3,
        retry_config=config,
        started_at=NOW + timedelta(seconds=2),
    )
    repository.append_started(second)
    truthful = _finish(
        second,
        http_status=None,
        retry_classification=None,
        termination_reason="retry_budget_exhausted",
    )
    contradictory = truthful.model_copy(update={"retry_classification": classification})
    with pytest.raises(ValidationError):
        ProviderTryAuditRecord.model_validate(contradictory.model_dump())
    with pytest.raises(ProviderAuditError):
        repository.append_finished(contradictory)
    repository.append_finished(
        _finish(
            second,
            http_status=None,
            retry_classification="cancelled",
            disposition="cancelled",
            termination_reason=None,
        )
    )
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        is None
    )
    with Session(engine) as session:
        event = session.exec(
            select(WorkflowEvent).order_by(col(WorkflowEvent.event_id).desc())
        ).first()
        assert event is not None
        event.event_metadata = contradictory.model_dump_json()
        session.add(event)
        session.commit()
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )


def test_unrelated_project_malformed_metadata_cannot_block_intact_or_new_calls(
    audit_database: tuple[Engine, int],
) -> None:
    """A Project's provider audit stays usable when another Project has damaged JSON."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    repository.append_started(started)
    repository.append_finished(_finish(started))
    before = repository.terminal_failure(
        project_id=project_id, action_id="action-a", call_id="call-a"
    )
    assert before is not None
    with Session(engine) as session:
        unrelated = Project(name="Damaged unrelated audit")
        session.add(unrelated)
        session.commit()
        assert unrelated.project_id is not None
        session.add(
            WorkflowEvent(
                project_id=unrelated.project_id,
                event_type=WorkflowEventType.PROVIDER_TRY_STARTED,
                event_metadata="{malformed synthetic unrelated metadata",
            )
        )
        session.commit()
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        == before
    )
    new = _start(project_id, action_id="new-action", call_id="new-call")
    repository.append_started(new)
    repository.append_finished(_finish(new))
    after = repository.terminal_failure(
        project_id=project_id, action_id="new-action", call_id="new-call"
    )
    assert after is not None
    assert after.call_id == "new-call"


def test_own_call_malformed_metadata_still_fails_locally(
    audit_database: tuple[Engine, int],
) -> None:
    """Safe extraction retains corrupted same-Project evidence as a local error."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    repository.append_started(started)
    repository.append_finished(_finish(started))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE workflow_events SET event_metadata = ? "
            "WHERE event_type = 'PROVIDER_TRY_FINISHED'",
            ("{malformed own-call evidence",),
        )
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
    with pytest.raises(ProviderAuditError):
        repository.append_finished(_finish(started))


def test_call_identity_cannot_cross_projects_under_a_different_action(
    audit_database: tuple[Engine, int],
) -> None:
    """Safe global collision checks still reject an intact foreign call identity."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    repository.append_started(_start(project_id))
    with Session(engine) as session:
        other = Project(name="Foreign identity")
        session.add(other)
        session.commit()
        assert other.project_id is not None
        other_id = other.project_id
    with pytest.raises(ProviderAuditError):
        repository.append_started(_start(other_id, action_id="different-action"))
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=other_id, action_id="different-action", call_id="call-a"
        )


@pytest.mark.parametrize(
    ("http_status", "classification", "disposition", "termination"),
    [
        (503, "unavailable", "success", None),
        (429, "rate_limited", "cancelled", None),
        (503, "unavailable", "non_retryable", None),
        (None, None, "cancelled", None),
        (None, None, "non_retryable", None),
        (None, "unavailable", "exhausted", "retry_budget_exhausted"),
        (None, None, "exhausted", "attempts_exhausted"),
    ],
)
def test_finish_disposition_classification_and_status_must_agree(
    audit_database: tuple[Engine, int],
    http_status: int | None,
    classification: str | None,
    disposition: str,
    termination: str | None,
) -> None:
    """Audit combinations distinguish success, cancellation and temporary errors."""
    _, project_id = audit_database
    started = _start(project_id)
    with pytest.raises(ValidationError):
        _finish(
            started,
            http_status=http_status,
            retry_classification=classification,
            disposition=disposition,
            termination_reason=termination,
        )


def _scheduled_call(
    engine: Engine, project_id: int
) -> tuple[ProviderAttemptAuditRepository, ProviderTryAuditRecord]:
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(
        project_id,
        max_attempts=3,
        retry_config=ProviderRetryConfig(max_attempts=3),
        idempotency_key_digest=sha256(b"existing-helper-key").hexdigest(),
    )
    repository.append_started(started)
    repository.append_finished(
        _finish(
            started,
            http_status=503,
            retry_classification="unavailable",
            disposition="retry_scheduled",
            termination_reason=None,
            selected_delay_seconds=4.0,
            retry_after_seconds=4.0,
            next_eligible_retry_at=NOW + timedelta(seconds=5),
        )
    )
    return repository, started


def _budget_summary() -> ProviderFailureSummary:
    return ProviderFailureSummary(
        reason="unavailable",
        termination_reason="retry_budget_exhausted",
        http_status=503,
        call_id="call-a",
        attempts=1,
        max_attempts=3,
        retry_after_seconds=4.0,
        manual_retry_requires_new_key=True,
    )


def test_carried_budget_summary_verifies_a_completed_scheduled_call(
    audit_database: tuple[Engine, int],
) -> None:
    """Oversleep stops after one real send without rewriting its scheduled finish."""
    engine, project_id = audit_database
    repository, _ = _scheduled_call(engine, project_id)
    original_metadata = [event.event_metadata for event in _events(engine)]
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        is None
    )
    assert (
        repository.terminal_failure(
            project_id=project_id,
            action_id="action-a",
            call_id="call-a",
            expected_summary=_budget_summary(),
        )
        == _budget_summary()
    )
    assert [event.event_metadata for event in _events(engine)] == original_metadata
    assert len(_events(engine)) == _PAIR_EVENT_COUNT


def _second_start(first: ProviderTryAuditRecord) -> ProviderTryAuditRecord:
    return ProviderTryAuditRecord.model_validate(
        {
            **first.model_dump(),
            "try_ordinal": 2,
            "started_at": NOW + timedelta(seconds=5),
        }
    )


def _not_sent(
    started: ProviderTryAuditRecord, **overrides: object
) -> ProviderTryAuditRecord:
    return _finish(
        started,
        **{
            "http_status": None,
            "retry_classification": None,
            "disposition": "not_sent",
            "termination_reason": "retry_budget_exhausted",
            **overrides,
        },
    )


def test_unsent_final_reservation_excludes_itself_from_actual_send_count(
    audit_database: tuple[Engine, int],
) -> None:
    """A retry start write can exhaust budget without inventing a second send."""
    engine, project_id = audit_database
    repository, first = _scheduled_call(engine, project_id)
    second = _second_start(first)
    repository.append_started(second)
    finished = _not_sent(second)
    repository.append_finished(finished)
    repository.append_finished(finished)
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        == _budget_summary()
    )
    assert (
        repository.terminal_failure(
            project_id=project_id,
            action_id="action-a",
            call_id="call-a",
            expected_summary=_budget_summary(),
        )
        == _budget_summary()
    )
    third = ProviderTryAuditRecord.model_validate(
        {
            **first.model_dump(),
            "try_ordinal": 3,
        }
    )
    with pytest.raises(ProviderAuditError):
        repository.append_started(third)
    assert len(_events(engine)) == _TWO_TRIES_EVENT_COUNT


@pytest.mark.parametrize(
    "mismatch",
    [
        {"http_status": 429, "reason": "rate_limited"},
        {"call_id": "different-call"},
        {"attempts": 2},
        {"max_attempts": 2},
        {"retry_after_seconds": None},
        {"manual_retry_requires_new_key": False},
        {"termination_reason": "attempts_exhausted"},
        {"termination_reason": "retry_after_exceeds_budget"},
        {"category": "invented-category"},
    ],
)
def test_carried_summary_requires_exact_derived_facts_and_revalidation(
    audit_database: tuple[Engine, int],
    mismatch: dict[str, object],
) -> None:
    """Only the approved budget stopping reason comes from the carried summary."""
    engine, project_id = audit_database
    repository, _ = _scheduled_call(engine, project_id)
    forged = _budget_summary().model_copy(update=mismatch)
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id,
            action_id="action-a",
            call_id="call-a",
            expected_summary=forged,
        )


@pytest.mark.parametrize(
    "evidence", ["missing", "incomplete", "success", "cancelled", "non_retryable"]
)
def test_carried_summary_rejects_missing_or_non_temporary_terminal_evidence(
    audit_database: tuple[Engine, int],
    evidence: str,
) -> None:
    """A supplied summary cannot turn absence or an ordinary outcome into exhaustion."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    started = _start(project_id)
    if evidence != "missing":
        repository.append_started(started)
    if evidence not in {"missing", "incomplete"}:
        repository.append_finished(
            _finish(
                started,
                disposition=evidence,
                http_status=None,
                retry_classification=None if evidence == "success" else evidence,
                termination_reason=None,
            )
        )
    expected = ProviderFailureSummary(
        reason="unavailable",
        termination_reason="retry_budget_exhausted",
        http_status=503,
        call_id="call-a",
        attempts=1,
        max_attempts=1,
        manual_retry_requires_new_key=False,
    )
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id,
            action_id="action-a",
            call_id="call-a",
            expected_summary=expected,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        {"try_ordinal": 1},
        {"http_status": 503},
        {"retry_classification": "cancelled"},
        {"retry_after_seconds": 0.0},
        {"selected_delay_seconds": 0.0},
        {"next_eligible_retry_at": NOW + timedelta(seconds=6)},
        {"termination_reason": "retry_after_exceeds_budget"},
    ],
)
def test_unsent_reservation_requires_final_retry_budget_facts_only(
    audit_database: tuple[Engine, int],
    invalid: dict[str, object],
) -> None:
    """Unsent reservations cannot claim initial-send, response or timing facts."""
    engine, project_id = audit_database
    _, first = _scheduled_call(engine, project_id)
    with pytest.raises(ValidationError):
        _not_sent(_second_start(first), **invalid)


@pytest.mark.parametrize("prior", ["incomplete", "success", "non_retryable"])
def test_unsent_reservation_requires_a_complete_prior_eligible_scheduled_pair(
    audit_database: tuple[Engine, int],
    prior: str,
) -> None:
    """Corrupt persisted unsent evidence cannot bypass the ordered prior-send proof."""
    engine, project_id = audit_database
    repository = ProviderAttemptAuditRepository(engine)
    first = _start(
        project_id, max_attempts=3, retry_config=ProviderRetryConfig(max_attempts=3)
    )
    repository.append_started(first)
    if prior != "incomplete":
        repository.append_finished(
            _finish(
                first,
                disposition=prior,
                http_status=None,
                retry_classification=None if prior == "success" else "non_retryable",
                termination_reason=None,
            )
        )
    second = _second_start(first)
    with pytest.raises(ProviderAuditError):
        repository.append_started(second)
    with Session(engine) as session:
        session.add_all(
            [
                WorkflowEvent(
                    project_id=project_id,
                    event_type=WorkflowEventType.PROVIDER_TRY_STARTED,
                    event_metadata=second.model_dump_json(),
                ),
                WorkflowEvent(
                    project_id=project_id,
                    event_type=WorkflowEventType.PROVIDER_TRY_FINISHED,
                    event_metadata=_not_sent(second).model_dump_json(),
                ),
            ]
        )
        session.commit()
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )


def test_unsent_reservation_finish_failure_stays_local_and_incomplete(
    audit_database: tuple[Engine, int],
) -> None:
    """A refused unsent finish cannot publish a carried temporary-provider failure."""
    engine, project_id = audit_database
    repository, first = _scheduled_call(engine, project_id)
    second = _second_start(first)
    repository.append_started(second)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER refuse_unsent_finish BEFORE INSERT ON workflow_events "
            "WHEN NEW.event_type='PROVIDER_TRY_FINISHED' "
            "AND json_extract(NEW.event_metadata, '$.try_ordinal') = 2 BEGIN "
            "SELECT RAISE(ABORT, 'synthetic local failure'); END"
        )
    with pytest.raises(ProviderAuditError):
        repository.append_finished(_not_sent(second))
    assert [event.event_type for event in _events(engine)] == [
        WorkflowEventType.PROVIDER_TRY_STARTED,
        WorkflowEventType.PROVIDER_TRY_FINISHED,
        WorkflowEventType.PROVIDER_TRY_STARTED,
    ]
    assert (
        repository.terminal_failure(
            project_id=project_id, action_id="action-a", call_id="call-a"
        )
        is None
    )
    with pytest.raises(ProviderAuditError):
        repository.terminal_failure(
            project_id=project_id,
            action_id="action-a",
            call_id="call-a",
            expected_summary=_budget_summary(),
        )


@pytest.mark.parametrize("prefix", ["", "sha256:"])
def test_attempt_fingerprint_accepts_durable_canonical_and_legacy_hex(
    prefix: str,
) -> None:
    """Real canonical attempt hashes and retained legacy hashes remain exact."""
    record = _start(
        1,
        workflow_node_attempt_id=1,
        attempt_fingerprint=prefix + "f" * 64,
        node_id="backlog.generate",
        idempotency_key_digest="b" * 64,
    )
    assert record.attempt_fingerprint == prefix + "f" * 64


@pytest.mark.parametrize(
    "fingerprint", ["sha256:" + "F" * 64, "sha256:" + "f" * 63, "sha512:" + "f" * 64]
)
def test_attempt_fingerprint_rejects_malformed_canonical_hash(fingerprint: str) -> None:
    """The canonical prefix does not relax digest shape or accepted algorithms."""
    with pytest.raises(ValidationError):
        _start(
            1,
            workflow_node_attempt_id=1,
            attempt_fingerprint=fingerprint,
            node_id="backlog.generate",
            idempotency_key_digest="b" * 64,
        )


@pytest.mark.parametrize("field", ["request_fingerprint", "idempotency_key_digest"])
def test_other_audit_digests_reject_canonical_prefix(field: str) -> None:
    """Only the actual attempt fingerprint uses the durable prefixed format."""
    association: dict[str, object] = {
        "workflow_node_attempt_id": 1,
        "attempt_fingerprint": "f" * 64,
        "node_id": "backlog.generate",
        "idempotency_key_digest": "b" * 64,
    }
    association[field] = "sha256:" + "b" * 64
    with pytest.raises(ValidationError, match=field):
        _start(1, **association)
