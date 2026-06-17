"""Tests for CLI mutation ledger state transitions."""

# ruff: noqa: SIM300

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Update
from sqlmodel import Session, SQLModel, select

import services.agent_workbench.mutation_ledger as mutation_ledger_mod
import utils.authority_curation_trace as trace_mod
from models.agent_workbench import CliMutationLedger
from services.agent_workbench.mutation_ledger import (
    IDEMPOTENCY_KEY_REUSED,
    MUTATION_IN_PROGRESS,
    MUTATION_RECOVERY_REQUIRED,
    MUTATION_RESUME_CONFLICT,
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
)
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

PROJECT_ID: Final[int] = 7
_LEDGER_MUTATION_EVENT_ID: Any = CliMutationLedger.mutation_event_id
TRACE_METADATA_KEYS: Final[tuple[str, ...]] = (
    "trace_artifact_id",
    "trace_artifact_present",
    "last_trace_step",
    "last_trace_status",
)


def _repo(engine: Engine) -> MutationLedgerRepository:
    SQLModel.metadata.create_all(engine)
    return MutationLedgerRepository(engine=engine)


def _db_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def test_create_pending_row_records_request_hash_and_lease(engine: Engine) -> None:
    """Verify a new mutation row records request identity and lease fields."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    )

    assert result.replayed is False
    assert result.error_code is None
    assert result.ledger.command == "agileforge fake mutate"
    assert result.ledger.idempotency_key == "fake-key-001"
    assert result.ledger.request_hash == "sha256:req"
    assert result.ledger.project_id == PROJECT_ID
    assert result.ledger.correlation_id == "corr-1"
    assert result.ledger.changed_by == "cli-agent"
    assert result.ledger.status == MutationStatus.PENDING.value
    assert result.ledger.lease_owner == "worker-1"
    assert result.ledger.lease_acquired_at == _db_time(now)
    assert result.ledger.last_heartbeat_at == _db_time(now)
    assert result.ledger.lease_expires_at == _db_time(now + timedelta(seconds=30))
    assert result.ledger.current_step == "start"

    with Session(engine) as session:
        rows = session.exec(select(CliMutationLedger)).all()
    assert len(rows) == 1


def test_offset_aware_create_timestamps_are_stored_as_utc_instants(
    engine: Engine,
) -> None:
    """Verify offset-aware timestamps are stored as naive UTC DB instants."""
    repo = _repo(engine)
    offset_now = datetime(2026, 5, 15, 15, 0, tzinfo=timezone(timedelta(hours=3)))

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=offset_now,
        lease_seconds=30,
    )

    assert result.ledger.lease_acquired_at == _db_time(
        datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    )
    assert result.ledger.last_heartbeat_at == _db_time(
        datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    )
    assert result.ledger.lease_expires_at == _db_time(
        datetime(2026, 5, 15, 12, 0, 30, tzinfo=UTC)
    )


def test_create_or_load_handles_unique_constraint_insert_race(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify insert races load the competing row instead of duplicating it."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    original_commit = mutation_ledger_mod.Session.commit
    raced = False
    inserting_competing_row = False

    def commit_with_insert_race(session: Session) -> None:
        nonlocal raced, inserting_competing_row
        if (
            not raced
            and not inserting_competing_row
            and any(isinstance(item, CliMutationLedger) for item in session.new)
        ):
            raced = True
            inserting_competing_row = True
            with Session(engine) as competing_session:
                competing_session.add(
                    CliMutationLedger(
                        command="agileforge fake mutate",
                        idempotency_key="fake-key-001",
                        request_hash="sha256:req",
                        project_id=PROJECT_ID,
                        correlation_id="corr-race",
                        changed_by="cli-agent",
                        status=MutationStatus.PENDING.value,
                        lease_owner="worker-race",
                        lease_acquired_at=_db_time(now),
                        last_heartbeat_at=_db_time(now),
                        lease_expires_at=_db_time(now + timedelta(seconds=30)),
                        created_at=_db_time(now),
                        updated_at=_db_time(now),
                    )
                )
                original_commit(competing_session)
            inserting_competing_row = False
            insert_race = "insert race"
            unique = "unique"
            raise IntegrityError(insert_race, params=None, orig=Exception(unique))
        original_commit(session)

    monkeypatch.setattr(mutation_ledger_mod.Session, "commit", commit_with_insert_race)

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now + timedelta(seconds=1),
        lease_seconds=30,
    )

    assert result.error_code == MUTATION_IN_PROGRESS
    assert result.ledger.lease_owner == "worker-race"
    with Session(engine) as session:
        rows = session.exec(select(CliMutationLedger)).all()
    assert len(rows) == 1


def test_same_key_same_request_replays_success_without_new_row(
    engine: Engine,
) -> None:
    """Verify completed mutations replay without inserting another row."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger

    assert row.mutation_event_id is not None
    assert repo.finalize_success(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        after={"step": "done"},
        response={"ok": True, "data": {"result": "done"}},
        now=now,
    )

    replay = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=now + timedelta(seconds=1),
        lease_seconds=30,
    )

    assert replay.replayed is True
    assert replay.ledger.mutation_event_id == row.mutation_event_id
    assert replay.ledger.status == MutationStatus.SUCCEEDED.value
    assert replay.response == {"ok": True, "data": {"result": "done"}}
    with Session(engine) as session:
        rows = session.exec(select(CliMutationLedger)).all()
    assert len(rows) == 1


def test_same_key_different_request_returns_reuse_error(engine: Engine) -> None:
    """Verify idempotency keys cannot be reused with different requests."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    )

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:different",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=now,
        lease_seconds=30,
    )

    assert result.error_code == IDEMPOTENCY_KEY_REUSED
    with Session(engine) as session:
        rows = session.exec(select(CliMutationLedger)).all()
    assert len(rows) == 1


def test_fresh_pending_row_reports_in_progress(engine: Engine) -> None:
    """Verify an active pending row reports in-progress on replay."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    )

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=now + timedelta(seconds=5),
        lease_seconds=30,
    )

    assert result.error_code == MUTATION_IN_PROGRESS


def test_stale_pending_becomes_recovery_required_with_structured_error(
    engine: Engine,
) -> None:
    """Verify stale pending rows move to recovery with structured details."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None
    assert repo.mark_step_complete(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        step="business_marker",
        next_step="session_marker",
        now=now + timedelta(seconds=1),
    )

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=now + timedelta(seconds=45),
        lease_seconds=30,
    )

    assert result.error_code == MUTATION_RECOVERY_REQUIRED
    with Session(engine) as session:
        stored = session.exec(select(CliMutationLedger)).one()
    assert stored.status == MutationStatus.RECOVERY_REQUIRED.value
    assert stored.lease_owner is None
    assert stored.lease_expires_at is None
    assert stored.recovery_action == RecoveryAction.RECONCILE_THEN_RESUME.value
    assert stored.recovery_safe_to_auto_resume is False
    assert stored.last_error_json is not None
    last_error = json.loads(stored.last_error_json)
    assert last_error == {
        "code": "STALE_PENDING",
        "message": "Pending mutation lease expired.",
        "details": {"current_step": "session_marker"},
        "retryable": True,
        "recorded_at": (now + timedelta(seconds=45)).isoformat(),
    }


def test_stale_pending_error_timestamp_uses_utc_instant_for_offset_now(
    engine: Engine,
) -> None:
    """Verify stale-pending errors record offset-aware inputs as UTC instants."""
    repo = _repo(engine)
    created_at = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    offset_now = datetime(2026, 5, 15, 15, 0, 45, tzinfo=timezone(timedelta(hours=3)))
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=created_at,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=offset_now,
        lease_seconds=30,
    )

    assert result.error_code == MUTATION_RECOVERY_REQUIRED
    with Session(engine) as session:
        stored = session.exec(select(CliMutationLedger)).one()
    assert stored.last_error_json is not None
    last_error = json.loads(stored.last_error_json)
    assert last_error["recorded_at"] == "2026-05-15T12:00:45+00:00"


def test_repair_setup_recovery_target_repairs_expired_pending_create(
    engine: Engine,
) -> None:
    """Verify setup retry can repair an expired project-create row."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None

    repaired = repo.repair_setup_recovery_target(
        mutation_event_id=row.mutation_event_id,
        expected_command="agileforge project create",
        expected_project_id=PROJECT_ID,
        now=now + timedelta(seconds=1),
    )

    assert repaired.error_code is None
    assert repaired.ledger.status == MutationStatus.RECOVERY_REQUIRED.value
    assert repaired.ledger.recovery_action == RecoveryAction.RECONCILE_THEN_RESUME.value
    assert repaired.ledger.recovery_safe_to_auto_resume is False
    assert repaired.ledger.lease_owner is None
    assert repaired.ledger.lease_expires_at is None
    assert repaired.ledger.last_error_json is not None
    assert json.loads(repaired.ledger.last_error_json)["code"] == "STALE_PENDING"


def test_repair_setup_recovery_target_rejects_active_pending_create(
    engine: Engine,
) -> None:
    """Verify setup retry leaves active project-create rows in progress."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=300,
    ).ledger
    assert row.mutation_event_id is not None

    result = repo.repair_setup_recovery_target(
        mutation_event_id=row.mutation_event_id,
        expected_command="agileforge project create",
        expected_project_id=PROJECT_ID,
        now=now,
    )

    assert result.error_code == MUTATION_IN_PROGRESS
    assert result.ledger.status == MutationStatus.PENDING.value


def test_repair_setup_recovery_target_reports_in_progress_on_lease_race(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify setup repair classifies lease-refresh races as in progress."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    original_exec = mutation_ledger_mod.Session.exec
    raced = False
    refreshing_lease = False

    def exec_with_lease_race(session: Session, statement: object) -> object:
        nonlocal raced, refreshing_lease
        if (
            not raced
            and not refreshing_lease
            and isinstance(statement, Update)
        ):
            raced = True
            refreshing_lease = True
            with Session(engine) as competing_session:
                competing_session.exec(
                    update(CliMutationLedger)
                    .where(_LEDGER_MUTATION_EVENT_ID == row.mutation_event_id)
                    .values(
                        lease_owner="worker-race",
                        lease_acquired_at=_db_time(now + timedelta(seconds=1)),
                        last_heartbeat_at=_db_time(now + timedelta(seconds=1)),
                        lease_expires_at=_db_time(now + timedelta(seconds=60)),
                        updated_at=_db_time(now + timedelta(seconds=1)),
                    )
                )
                competing_session.commit()
            refreshing_lease = False
        return cast("Any", original_exec)(session, statement)

    monkeypatch.setattr(mutation_ledger_mod.Session, "exec", exec_with_lease_race)

    result = repo.repair_setup_recovery_target(
        mutation_event_id=row.mutation_event_id,
        expected_command="agileforge project create",
        expected_project_id=PROJECT_ID,
        now=now + timedelta(seconds=1),
    )

    assert result.error_code == MUTATION_IN_PROGRESS
    assert result.ledger.status == MutationStatus.PENDING.value
    assert result.ledger.lease_owner == "worker-race"
    assert result.ledger.lease_expires_at == _db_time(now + timedelta(seconds=60))


def test_repair_setup_recovery_target_fences_snapshot_lease_owner(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify setup repair misses when the expired lease owner changes."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    original_exec = mutation_ledger_mod.Session.exec
    raced = False
    changing_owner = False

    def exec_with_owner_race(session: Session, statement: object) -> object:
        nonlocal raced, changing_owner
        if (
            not raced
            and not changing_owner
            and isinstance(statement, Update)
        ):
            raced = True
            changing_owner = True
            with Session(engine) as competing_session:
                competing_session.exec(
                    update(CliMutationLedger)
                    .where(_LEDGER_MUTATION_EVENT_ID == row.mutation_event_id)
                    .values(
                        lease_owner="worker-race",
                        updated_at=_db_time(now + timedelta(seconds=1)),
                    )
                )
                competing_session.commit()
            changing_owner = False
        return cast("Any", original_exec)(session, statement)

    monkeypatch.setattr(mutation_ledger_mod.Session, "exec", exec_with_owner_race)

    result = repo.repair_setup_recovery_target(
        mutation_event_id=row.mutation_event_id,
        expected_command="agileforge project create",
        expected_project_id=PROJECT_ID,
        now=now + timedelta(seconds=1),
    )

    assert result.error_code == MUTATION_RESUME_CONFLICT
    assert result.ledger.status == MutationStatus.PENDING.value
    assert result.ledger.lease_owner == "worker-race"


def test_stale_recovery_loses_race_when_same_owner_refreshes_lease(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify stale recovery detects a concurrent owner lease refresh."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None
    recovery_attempt_at = now + timedelta(seconds=45)
    refreshed_expires_at = recovery_attempt_at + timedelta(seconds=60)
    original_exec = mutation_ledger_mod.Session.exec
    race_injected = False

    def exec_with_lease_refresh_race(
        session: Session,
        statement: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal race_injected
        if isinstance(statement, Update) and not race_injected:
            race_injected = True
            original_exec(
                session,
                update(CliMutationLedger)
                .where(_LEDGER_MUTATION_EVENT_ID == row.mutation_event_id)
                .values(
                    last_heartbeat_at=_db_time(recovery_attempt_at),
                    lease_expires_at=_db_time(refreshed_expires_at),
                    updated_at=_db_time(recovery_attempt_at),
                ),
            )
        return cast("Any", original_exec)(session, statement, *args, **kwargs)

    monkeypatch.setattr(
        mutation_ledger_mod.Session,
        "exec",
        exec_with_lease_refresh_race,
    )

    result = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=recovery_attempt_at,
        lease_seconds=30,
    )

    assert result.error_code == MUTATION_IN_PROGRESS
    with Session(engine) as session:
        stored = session.exec(select(CliMutationLedger)).one()
    assert stored.status == MutationStatus.PENDING.value
    assert stored.lease_owner == "worker-1"
    assert stored.lease_expires_at == _db_time(refreshed_expires_at)
    assert stored.last_error_json is None


def test_resume_requires_compare_and_set_from_recovery_required(
    engine: Engine,
) -> None:
    """Verify resume lease acquisition is a compare-and-set transition."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    assert not hasattr(repo, "force_recovery_required")
    repo._force_recovery_required_for_test(
        mutation_event_id=row.mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={
            "code": "CRASHED",
            "message": "Simulated crash.",
            "details": {},
            "retryable": True,
            "recorded_at": now.isoformat(),
        },
        now=now,
    )

    acquired = repo.acquire_resume_lease(
        mutation_event_id=row.mutation_event_id,
        lease_owner="resume-1",
        now=now + timedelta(seconds=2),
        lease_seconds=30,
    )
    conflicted = repo.acquire_resume_lease(
        mutation_event_id=row.mutation_event_id,
        lease_owner="resume-2",
        now=now + timedelta(seconds=3),
        lease_seconds=30,
    )

    assert acquired.error_code is None
    assert acquired.ledger.status == MutationStatus.PENDING.value
    assert acquired.ledger.lease_owner == "resume-1"
    assert acquired.ledger.lease_acquired_at == _db_time(
        now + timedelta(seconds=2)
    )
    assert acquired.ledger.lease_expires_at == _db_time(now + timedelta(seconds=32))
    assert conflicted.error_code == MUTATION_RESUME_CONFLICT


def test_show_event_returns_json_friendly_row_payload(engine: Engine) -> None:
    """Expose one mutation ledger row with parsed JSON blobs."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None
    with Session(engine) as session:
        session.exec(
            update(CliMutationLedger)
            .where(_LEDGER_MUTATION_EVENT_ID == row.mutation_event_id)
            .values(
                completed_steps_json='["business_marker"]',
                guard_inputs_json='{"expected_state":"SPRINT_SETUP"}',
                before_json='{"state":"before"}',
                after_json='{"state":"after"}',
                response_json='{"ok":true,"data":{"result":"done"}}',
                last_error_json='{"code":"STALE_PENDING"}',
            )
        )
        session.commit()

    result = repo.show_event(mutation_event_id=row.mutation_event_id)

    assert result["ok"] is True
    assert result["warnings"] == []
    assert result["errors"] == []
    assert result["data"] == {
        "mutation_event_id": row.mutation_event_id,
        "command": "agileforge fake mutate",
        "idempotency_key": "fake-key-001",
        "request_hash": "sha256:req",
        "project_id": PROJECT_ID,
        "correlation_id": "corr-1",
        "changed_by": "cli-agent",
        "status": MutationStatus.PENDING.value,
        "current_step": "start",
        "completed_steps": ["business_marker"],
        "guard_inputs": {"expected_state": "SPRINT_SETUP"},
        "before": {"state": "before"},
        "after": {"state": "after"},
        "response": {"ok": True, "data": {"result": "done"}},
        "recovery_action": RecoveryAction.NONE.value,
        "recovery_safe_to_auto_resume": False,
        "lease_owner": "worker-1",
        "lease_acquired_at": now.isoformat(),
        "last_heartbeat_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(seconds=30)).isoformat(),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "last_error": {"code": "STALE_PENDING"},
    }


def test_mutation_show_includes_authority_curation_trace_metadata(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation show enriches authority curation rows with trace summary."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    repo = _repo(engine)
    loaded = repo.create_or_load(
        command="agileforge authority curate",
        idempotency_key="curate-show-trace",
        request_hash="sha256:trace",
        project_id=3,
        correlation_id="corr",
        changed_by="test",
        lease_owner="lease",
        now=datetime(2026, 6, 16, 12, tzinfo=UTC),
    )
    mutation_event_id = require_id(
        loaded.ledger.mutation_event_id,
        "mutation_event_id",
    )
    trace_mod.append_trace_event(
        mutation_event_id=mutation_event_id,
        project_id=3,
        step="adk_invocation_started",
        status="started",
    )

    result = repo.show_event(mutation_event_id=mutation_event_id)

    assert result["ok"] is True
    data = result["data"]
    assert data["trace_artifact_id"] == (
        f"authority_curation_trace-{mutation_event_id}"
    )
    assert data["trace_artifact_present"] is True
    assert data["last_trace_step"] == "adk_invocation_started"
    assert data["last_trace_status"] == "started"


def test_mutation_show_trace_summary_failure_is_fail_open(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trace summary failures must not block mutation show payloads."""
    calls = 0

    def fail_summary(*, mutation_event_id: int) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert mutation_event_id > 0
        message = "trace summary failed"
        raise OSError(message)

    monkeypatch.setattr(trace_mod, "summarize_trace", fail_summary)
    repo = _repo(engine)
    row = repo.create_or_load(
        command="agileforge authority curate",
        idempotency_key="curate-show-trace-fail-open",
        request_hash="sha256:trace-fail-open",
        project_id=3,
        correlation_id="corr",
        changed_by="test",
        lease_owner="lease",
        now=datetime(2026, 6, 16, 12, tzinfo=UTC),
    ).ledger
    mutation_event_id = require_id(row.mutation_event_id, "mutation_event_id")

    result = repo.show_event(mutation_event_id=mutation_event_id)

    assert calls == 1
    assert result["ok"] is True
    assert result["data"]["mutation_event_id"] == mutation_event_id
    assert result["data"]["command"] == "agileforge authority curate"
    for key in TRACE_METADATA_KEYS:
        assert key in result["data"]
        assert result["data"][key] is None


def test_mutation_show_trace_programmer_error_is_not_swallowed(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation show propagates non-operational trace summary bugs."""

    def broken_summary(*, mutation_event_id: int) -> dict[str, object]:
        assert mutation_event_id > 0
        message = "programmer bug"
        raise TypeError(message)

    monkeypatch.setattr(trace_mod, "summarize_trace", broken_summary)
    repo = _repo(engine)
    row = repo.create_or_load(
        command="agileforge authority curate",
        idempotency_key="curate-show-trace-type-error",
        request_hash="sha256:trace-type-error",
        project_id=3,
        correlation_id="corr",
        changed_by="test",
        lease_owner="lease",
        now=datetime(2026, 6, 16, 12, tzinfo=UTC),
    ).ledger
    mutation_event_id = require_id(row.mutation_event_id, "mutation_event_id")

    with pytest.raises(TypeError, match="programmer bug"):
        repo.show_event(mutation_event_id=mutation_event_id)


def test_show_event_returns_registered_not_found_error(engine: Engine) -> None:
    """Return a registered not-found error for missing mutation rows."""
    repo = _repo(engine)

    result = repo.show_event(mutation_event_id=999)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["warnings"] == []
    assert result["errors"] == [
        {
            "code": "MUTATION_NOT_FOUND",
            "message": "The requested mutation was not found.",
            "details": {"mutation_event_id": 999},
            "remediation": ["agileforge mutation list"],
            "exit_code": 4,
            "retryable": False,
        }
    ]


def test_list_events_filters_by_project_and_status(engine: Engine) -> None:
    """List mutation ledger rows with optional project and status filters."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    first = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req-1",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
    ).ledger
    second = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-002",
        request_hash="sha256:req-2",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=now + timedelta(seconds=1),
    ).ledger
    third = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-003",
        request_hash="sha256:req-3",
        project_id=8,
        correlation_id="corr-3",
        changed_by="cli-agent",
        lease_owner="worker-3",
        now=now + timedelta(seconds=2),
    ).ledger
    assert first.mutation_event_id is not None
    assert second.mutation_event_id is not None
    assert third.mutation_event_id is not None
    repo._force_recovery_required_for_test(
        mutation_event_id=second.mutation_event_id,
        recovery_action=RecoveryAction.RECONCILE_THEN_RESUME,
        safe_to_auto_resume=False,
        last_error={"code": "CRASHED"},
        now=now + timedelta(seconds=3),
    )

    result = repo.list_events(
        project_id=PROJECT_ID,
        status=MutationStatus.PENDING.value,
    )

    assert result["ok"] is True
    assert result["warnings"] == []
    assert result["errors"] == []
    assert len(result["data"]["items"]) == 1
    assert result["data"]["items"][0]["mutation_event_id"] == first.mutation_event_id
    assert result["data"]["items"][0]["project_id"] == PROJECT_ID
    assert result["data"]["items"][0]["status"] == MutationStatus.PENDING.value


def test_list_events_omits_authority_curation_trace_metadata(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation list keeps authority curation trace metadata show-only."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    repo = _repo(engine)
    row = repo.create_or_load(
        command="agileforge authority curate",
        idempotency_key="curate-list-trace",
        request_hash="sha256:trace-list",
        project_id=PROJECT_ID,
        correlation_id="corr",
        changed_by="test",
        lease_owner="lease",
        now=datetime(2026, 6, 16, 12, tzinfo=UTC),
    ).ledger
    mutation_event_id = require_id(row.mutation_event_id, "mutation_event_id")
    trace_mod.append_trace_event(
        mutation_event_id=mutation_event_id,
        project_id=PROJECT_ID,
        step="adk_invocation_started",
        status="started",
    )

    result = repo.list_events(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert len(result["data"]["items"]) == 1
    item = result["data"]["items"][0]
    assert item["mutation_event_id"] == mutation_event_id
    for key in TRACE_METADATA_KEYS:
        assert key not in item


def test_resume_event_acquires_recovery_lease_without_domain_recovery(
    engine: Engine,
) -> None:
    """Resume only acquires a recovery lease and leaves domain effects to commands."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    repo._force_recovery_required_for_test(
        mutation_event_id=row.mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "CRASHED"},
        now=now + timedelta(seconds=2),
    )

    result = repo.resume_event(
        mutation_event_id=row.mutation_event_id,
        correlation_id="corr-resume",
    )

    assert result["ok"] is True
    assert result["warnings"] == []
    assert result["errors"] == []
    assert result["data"]["status"] == MutationStatus.PENDING.value
    assert result["data"]["lease_owner"] == "agileforge-cli:mutation-resume:corr-resume"
    assert result["data"]["recovery"] == {
        "acquired": True,
        "domain_resume_required": True,
    }
    assert result["data"]["response"] is None


def test_resume_event_omits_authority_curation_trace_metadata(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation resume keeps authority curation trace metadata show-only."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    repo = _repo(engine)
    now = datetime(2026, 6, 16, 12, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge authority curate",
        idempotency_key="curate-resume-trace",
        request_hash="sha256:trace-resume",
        project_id=PROJECT_ID,
        correlation_id="corr",
        changed_by="test",
        lease_owner="lease",
        now=now,
        lease_seconds=1,
    ).ledger
    mutation_event_id = require_id(row.mutation_event_id, "mutation_event_id")
    trace_mod.append_trace_event(
        mutation_event_id=mutation_event_id,
        project_id=PROJECT_ID,
        step="adk_invocation_started",
        status="started",
    )
    repo._force_recovery_required_for_test(
        mutation_event_id=mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "CRASHED"},
        now=now + timedelta(seconds=2),
    )

    result = repo.resume_event(
        mutation_event_id=mutation_event_id,
        correlation_id="corr-resume",
    )

    assert result["ok"] is True
    assert result["data"]["mutation_event_id"] == mutation_event_id
    assert result["data"]["recovery"] == {
        "acquired": True,
        "domain_resume_required": True,
    }
    for key in TRACE_METADATA_KEYS:
        assert key not in result["data"]


def test_resume_event_returns_structured_conflict_error(engine: Engine) -> None:
    """Return the registered conflict when a recovery lease is unavailable."""
    repo = _repo(engine)
    now = datetime.now(UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    result = repo.resume_event(mutation_event_id=row.mutation_event_id)

    assert result["ok"] is False
    assert result["data"]["mutation_event_id"] == row.mutation_event_id
    assert result["errors"] == [
        {
            "code": "MUTATION_RESUME_CONFLICT",
            "message": "Another worker acquired recovery.",
            "details": {"mutation_event_id": row.mutation_event_id},
            "remediation": [
                f"agileforge mutation show --mutation-event-id {row.mutation_event_id}"
            ],
            "exit_code": 1,
            "retryable": True,
        }
    ]


def test_mutation_resume_hands_project_create_recovery_to_setup_retry(
    engine: Engine,
) -> None:
    """Verify project-create recovery is handed to setup retry."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    repo._force_recovery_required_for_test(
        mutation_event_id=row.mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "MUTATION_RECOVERY_REQUIRED"},
        now=now + timedelta(seconds=2),
    )

    result = repo.resume_event(
        mutation_event_id=row.mutation_event_id,
        correlation_id="corr-resume",
    )

    assert result["ok"] is True
    assert result["data"]["status"] == MutationStatus.RECOVERY_REQUIRED.value
    assert result["data"]["lease_owner"] is None
    assert result["data"]["recovery"] == {
        "acquired": False,
        "domain_resume_required": False,
        "handoff_command": "agileforge project setup retry",
        "reason": "Project creation recovery is resumed by setup retry.",
    }
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.status == MutationStatus.RECOVERY_REQUIRED.value
    assert stored.lease_owner is None


def test_resume_event_repairs_expired_resume_lease_before_reacquiring(
    engine: Engine,
) -> None:
    """Recover a stale generic resume lease before acquiring a fresh one."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    repo._force_recovery_required_for_test(
        mutation_event_id=row.mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "CRASHED"},
        now=now + timedelta(seconds=2),
    )
    first = repo.resume_event(
        mutation_event_id=row.mutation_event_id,
        correlation_id="corr-first",
    )
    assert first["ok"] is True
    assert first["data"]["lease_owner"] == "agileforge-cli:mutation-resume:corr-first"
    with Session(engine) as session:
        session.exec(
            update(CliMutationLedger)
            .where(_LEDGER_MUTATION_EVENT_ID == row.mutation_event_id)
            .values(
                lease_acquired_at=datetime(2000, 1, 1, tzinfo=UTC),
                last_heartbeat_at=datetime(2000, 1, 1, tzinfo=UTC),
                lease_expires_at=datetime(2000, 1, 1, 0, 5, tzinfo=UTC),
            )
        )
        session.commit()

    second = repo.resume_event(
        mutation_event_id=row.mutation_event_id,
        correlation_id="corr-second",
    )

    assert second["ok"] is True
    assert second["data"]["status"] == MutationStatus.PENDING.value
    assert second["data"]["lease_owner"] == (
        "agileforge-cli:mutation-resume:corr-second"
    )
    assert second["data"]["recovery"] == {
        "acquired": True,
        "domain_resume_required": True,
    }
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.status == MutationStatus.PENDING.value
    assert stored.lease_owner == "agileforge-cli:mutation-resume:corr-second"


def test_expired_owner_cannot_heartbeat(engine: Engine) -> None:
    """Verify expired owners cannot refresh their lease heartbeat."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    refreshed = repo.require_active_owner(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        now=now + timedelta(seconds=30),
        lease_seconds=30,
    )

    assert refreshed is False
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.last_heartbeat_at == _db_time(now)
    assert stored.lease_expires_at == _db_time(now + timedelta(seconds=30))


def test_expired_owner_cannot_mark_step_complete(engine: Engine) -> None:
    """Verify expired owners cannot record completed mutation steps."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    marked = repo.mark_step_complete(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        step="business_marker",
        next_step="session_marker",
        now=now + timedelta(seconds=30),
    )

    assert marked is False
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.completed_steps_json == "[]"
    assert stored.current_step == "start"


def test_mark_step_complete_loses_race_when_row_changes_before_commit(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify step completion returns false when another update wins."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None
    original_exec = mutation_ledger_mod.Session.exec
    race_injected = False

    def exec_after_recovery_race(
        session: Session,
        statement: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal race_injected
        if isinstance(statement, Update) and not race_injected:
            race_injected = True
            original_exec(
                session,
                update(CliMutationLedger)
                .where(_LEDGER_MUTATION_EVENT_ID == row.mutation_event_id)
                .values(
                    status=MutationStatus.RECOVERY_REQUIRED.value,
                    recovery_action=RecoveryAction.RECONCILE_THEN_RESUME.value,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=_db_time(now + timedelta(seconds=5)),
                ),
            )
        return cast("Any", original_exec)(session, statement, *args, **kwargs)

    monkeypatch.setattr(mutation_ledger_mod.Session, "exec", exec_after_recovery_race)

    marked = repo.mark_step_complete(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        step="business_marker",
        next_step="session_marker",
        now=now + timedelta(seconds=1),
    )

    assert marked is False
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.status == MutationStatus.RECOVERY_REQUIRED.value
    assert stored.completed_steps_json == "[]"
    assert stored.current_step == "start"


def test_expired_owner_cannot_finalize_success(engine: Engine) -> None:
    """Verify expired owners cannot finalize successful mutations."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    finalized = repo.finalize_success(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        after={"step": "done"},
        response={"ok": True},
        now=now + timedelta(seconds=30),
    )

    assert finalized is False
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.status == MutationStatus.PENDING.value
    assert stored.after_json is None
    assert stored.response_json is None
    assert stored.lease_owner == "worker-1"


def test_transition_status_enforces_expected_status_and_owner(
    engine: Engine,
) -> None:
    """Verify generic status transitions require expected status and owner."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    wrong_owner = repo.transition_status(
        mutation_event_id=row.mutation_event_id,
        expected_status=MutationStatus.PENDING,
        expected_lease_owner="worker-2",
        new_status=MutationStatus.RECOVERY_REQUIRED,
        new_lease_owner=None,
        now=now + timedelta(seconds=1),
    )
    wrong_status = repo.transition_status(
        mutation_event_id=row.mutation_event_id,
        expected_status=MutationStatus.RECOVERY_REQUIRED,
        expected_lease_owner="worker-1",
        new_status=MutationStatus.SUCCEEDED,
        new_lease_owner=None,
        now=now + timedelta(seconds=1),
    )
    transitioned = repo.transition_status(
        mutation_event_id=row.mutation_event_id,
        expected_status=MutationStatus.PENDING,
        expected_lease_owner="worker-1",
        new_status=MutationStatus.RECOVERY_REQUIRED,
        new_lease_owner=None,
        now=now + timedelta(seconds=2),
    )

    assert wrong_owner.error_code == MUTATION_RESUME_CONFLICT
    assert wrong_status.error_code == MUTATION_RESUME_CONFLICT
    assert transitioned.error_code is None
    assert transitioned.ledger.status == MutationStatus.RECOVERY_REQUIRED.value
    assert transitioned.ledger.lease_owner is None


def test_require_active_owner_refreshes_only_active_owner(engine: Engine) -> None:
    """Verify only the current active owner can refresh a lease."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    wrong_owner = repo.require_active_owner(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-2",
        now=now + timedelta(seconds=5),
        lease_seconds=30,
    )
    active_owner = repo.require_active_owner(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        now=now + timedelta(seconds=10),
        lease_seconds=30,
    )

    assert wrong_owner is False
    assert active_owner is True
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.last_heartbeat_at == _db_time(now + timedelta(seconds=10))
    assert stored.lease_expires_at == _db_time(now + timedelta(seconds=40))


def test_finalize_success_requires_pending_active_owner_and_stores_result(
    engine: Engine,
) -> None:
    """Verify success finalization stores response and rejects later rewrites."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None

    assert not repo.finalize_success(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-2",
        after={"step": "wrong"},
        response={"ok": False},
        now=now + timedelta(seconds=1),
    )
    assert repo.finalize_success(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        after={"step": "done"},
        response={"ok": True, "data": {"result": "done"}},
        now=now + timedelta(seconds=2),
    )
    assert not repo.finalize_success(
        mutation_event_id=row.mutation_event_id,
        lease_owner="worker-1",
        after={"step": "again"},
        response={"ok": True},
        now=now + timedelta(seconds=3),
    )

    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.status == MutationStatus.SUCCEEDED.value
    assert json.loads(stored.after_json or "{}") == {"step": "done"}
    assert json.loads(stored.response_json or "{}") == {
        "ok": True,
        "data": {"result": "done"},
    }
    assert stored.lease_owner is None
    assert stored.lease_expires_at is None
    assert stored.recovery_action == RecoveryAction.NONE.value
