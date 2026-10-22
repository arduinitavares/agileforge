"""Renewable host ownership preserves immutable attempts and bounded recovery."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event, local
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, select

from models.core import Project
from models.workflow import (
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowNodeAttemptOwnership,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactRepository
from tests.workflow.test_agentic_node_lifecycle import (
    EVALUATED_AT,
    LEASE_SECONDS,
    CatalogRecipeRegistry,
    MutableClock,
    _agentic_graph,
    _decision,
    _seed_project,
    _start_request,
)
from tests.workflow.test_sprint_retry_schema import _file_engine
from workflow.attempt_ownership import AttemptOwnershipLostError, bind_attempt_ownership
from workflow.contracts import (
    NodeCategory,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
)
from workflow.domain import WorkflowDomain
from workflow.fingerprints import business_fact_fingerprint
from workflow.requests import FailNodeAttempt, StartNodeAttempt

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.attempt_ownership import AttemptOwnershipGuard

NODE = "goal.interview"


def _system(
    engine: Engine,
) -> tuple[
    WorkflowDomain,
    MutableClock,
    int,
    StartNodeAttempt,
    TransitionResult,
    AttemptOwnershipGuard,
]:
    project_id = _seed_project(engine)
    clock = MutableClock(EVALUATED_AT)
    graph = _agentic_graph(NODE)
    domain = WorkflowDomain(
        engine=engine,
        graph=graph,
        clock=clock,
        adk_recipe_registry=CatalogRecipeRegistry(graph.agentic_node_ids),
    )
    request = _start_request(domain, project_id, NODE, idempotency_key="owned-start")
    started = domain.transition(request)
    assert started.ok
    attempt_id = started.output["attempt_id"]
    fingerprint = started.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(fingerprint, str)
    guard = domain.claim_provider_attempt(
        project_id=project_id,
        attempt_id=attempt_id,
        attempt_fingerprint=fingerprint,
    )
    return domain, clock, project_id, request, started, guard


def _immutable_history(engine: Engine) -> tuple[str, str]:
    with Session(engine) as session:
        attempt = session.exec(select(WorkflowNodeAttempt)).one()
        receipt = session.exec(select(WorkflowTransitionReceipt)).one()
        return attempt.model_dump_json(), receipt.model_dump_json()


def _fail(
    project_id: int, guard: AttemptOwnershipGuard, key: str = "owned-failure"
) -> FailNodeAttempt:
    return FailNodeAttempt(
        project_id=project_id,
        attempt_id=guard.attempt_id,
        attempt_fingerprint=guard.attempt_fingerprint,
        failure_code="GENERATION_OUTPUT_INCOMPLETE",
        failure_message="No complete output.",
        idempotency_key=key,
        actor="operator@example.com",
        correlation_id="ownership",
    )


def test_renewal_keeps_healthy_owner_waiting_beyond_original_lease(
    engine: Engine,
) -> None:
    """Renewal keeps healthy owner waiting beyond original lease."""
    domain, clock, project_id, request, _started, guard = _system(engine)
    before = _immutable_history(engine)
    with Session(engine) as session:
        business = business_fact_fingerprint(
            WorkflowFactRepository(session).load(project_id)
        )
    clock.now_value += timedelta(seconds=40)
    domain.renew_provider_attempt(guard)
    clock.now_value += timedelta(seconds=40)
    waiting = _decision(domain, project_id, NODE)
    assert waiting.category is NodeCategory.WAITING
    assert waiting.valid_until == EVALUATED_AT + timedelta(seconds=100)
    assert _immutable_history(engine) == before
    with Session(engine) as session:
        assert (
            business_fact_fingerprint(WorkflowFactRepository(session).load(project_id))
            == business
        )
    with bind_attempt_ownership(guard):
        assert (
            domain.check_provider_attempt(
                project_id=project_id,
                attempt_id=guard.attempt_id,
                attempt_fingerprint=guard.attempt_fingerprint,
            )
            is None
        )
        result = domain.transition(_fail(project_id, guard))
    assert result.ok is True
    assert result.output["status"] == "failure"
    replay = domain.replay_provider_attempt(request)
    assert replay is not None
    assert replay.error is not None
    assert replay.replayed is True
    assert replay.error.code is WorkflowErrorCode.GENERATION_OUTPUT_INCOMPLETE


def test_dead_owner_expires_and_old_generation_cannot_renew_after_replacement(
    engine: Engine,
) -> None:
    """Dead owner expires and old generation cannot renew after replacement."""
    domain, clock, project_id, _, _started, guard = _system(engine)
    clock.now_value += timedelta(seconds=LEASE_SECONDS)
    with pytest.raises(AttemptOwnershipLostError):
        domain.renew_provider_attempt(guard)
    recovery = _decision(domain, project_id, NODE)
    assert recovery.recommendation_kind is RecommendationKind.RECOVERY
    replacement = domain.transition(
        _start_request(domain, project_id, NODE, idempotency_key="replacement")
    )
    assert replacement.ok
    assert replacement.output["attempt_id"] != guard.attempt_id
    with pytest.raises(AttemptOwnershipLostError):
        domain.renew_provider_attempt(guard)
    with Session(engine) as session:
        outcomes = session.exec(select(WorkflowNodeAttemptOutcome)).all()
        assert len(outcomes) == 1
        assert outcomes[0].status == "obsolete"


def test_wrong_owner_cannot_renew_or_obsolete_the_live_owner(engine: Engine) -> None:
    """Wrong owner cannot renew or obsolete the live owner."""
    domain, _, project_id, _, _started, guard = _system(engine)
    wrong = replace(guard, owner_generation="foreign-generation")
    with pytest.raises(AttemptOwnershipLostError):
        domain.renew_provider_attempt(wrong)
    with bind_attempt_ownership(wrong):
        rejected = domain.transition(_fail(project_id, guard, key="foreign-failure"))
    assert rejected.ok is False
    assert rejected.error is not None
    assert rejected.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
    assert _decision(domain, project_id, NODE).category is NodeCategory.WAITING
    with Session(engine) as session:
        assert session.exec(select(WorkflowNodeAttemptOutcome)).all() == []
    domain.renew_provider_attempt(guard)


@pytest.mark.parametrize("renew_first", [True, False])
def test_renewal_and_terminal_failure_serialize_without_resurrection(
    engine: Engine, renew_first: bool
) -> None:
    """Renewal and terminal failure serialize without resurrection."""
    domain, clock, project_id, _, _started, guard = _system(engine)
    clock.now_value += timedelta(seconds=20)
    if renew_first:
        domain.renew_provider_attempt(guard)
    with bind_attempt_ownership(guard):
        terminal = domain.transition(_fail(project_id, guard))
    assert terminal.ok is True
    assert terminal.output["status"] == "failure"
    with pytest.raises(AttemptOwnershipLostError):
        domain.renew_provider_attempt(guard)
    with Session(engine) as session:
        assert len(session.exec(select(WorkflowNodeAttemptOutcome)).all()) == 1


@pytest.mark.parametrize("change", ["business", "policy", "graph"])
def test_renewal_rejects_changed_authority(engine: Engine, change: str) -> None:
    """Renewal rejects changed authority."""
    domain, _, project_id, _, _, guard = _system(engine)
    if change == "graph":
        domain._graph = replace(domain._graph, graph_version="different-graph")
    else:
        with Session(engine) as session:
            if change == "business":
                row = session.get(Project, project_id)
                assert row is not None
                row.name = "Changed business authority"
            else:
                row = session.exec(select(WorkflowNodeAttempt)).one()
                settings = json.loads(row.execution_settings_json)
                settings["reasoning_effort"] = "low"
                row.execution_settings_json = json.dumps(settings)
            session.add(row)
            session.commit()
    with pytest.raises(AttemptOwnershipLostError):
        domain.renew_provider_attempt(guard)


@pytest.mark.parametrize("renew_first", [True, False])
def test_two_connections_serialize_renewal_and_terminal_outcome(
    tmp_path: Path, renew_first: bool
) -> None:
    """The second writer sees the committed winner before deciding authority."""
    database = _file_engine(tmp_path / "owner-race.sqlite")
    SQLModel.metadata.create_all(database)
    try:
        domain, clock, project_id, _, _, guard = _system(database)
        before = _immutable_history(database)[0]
        clock.now_value += timedelta(seconds=20)
        first_locked = Event()
        second_entering = Event()
        release_first = Event()
        labels = local()

        def before_begin(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _many: bool,
        ) -> None:
            if (
                statement == "BEGIN IMMEDIATE"
                and getattr(labels, "name", None) == "second"
            ):
                second_entering.set()

        def after_begin(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _many: bool,
        ) -> None:
            if (
                statement == "BEGIN IMMEDIATE"
                and getattr(labels, "name", None) == "first"
            ):
                first_locked.set()
                assert release_first.wait(5)

        event.listen(database, "before_cursor_execute", before_begin)
        event.listen(database, "after_cursor_execute", after_begin)

        def act(label: str, renew: bool) -> str | TransitionResult:
            labels.name = label
            if renew:
                try:
                    domain.renew_provider_attempt(guard)
                except AttemptOwnershipLostError:
                    return "lost"
                return "renewed"
            with bind_attempt_ownership(guard):
                return domain.transition(_fail(project_id, guard))

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(act, "first", renew_first)
            assert first_locked.wait(5)
            second = workers.submit(act, "second", not renew_first)
            assert second_entering.wait(5)
            release_first.set()
            outcomes = (first.result(timeout=5), second.result(timeout=5))
        assert ("renewed" if renew_first else "lost") in outcomes
        terminal = outcomes[1] if renew_first else outcomes[0]
        assert isinstance(terminal, TransitionResult)
        assert terminal.ok
        with Session(database) as session:
            assert len(session.exec(select(WorkflowNodeAttemptOutcome)).all()) == 1
            assert (
                session.exec(select(WorkflowNodeAttempt)).one().model_dump_json()
                == before
            )
        with pytest.raises(AttemptOwnershipLostError):
            domain.renew_provider_attempt(guard)
    finally:
        database.dispose()


def test_failed_renewal_transaction_cannot_extend_dead_owner_recovery(
    tmp_path: Path,
) -> None:
    """An exception after UPDATE rolls back expiry and revision together."""
    database = _file_engine(tmp_path / "failed-renewal.sqlite")
    SQLModel.metadata.create_all(database)
    try:
        domain, clock, project_id, _, _, guard = _system(database)
        clock.now_value += timedelta(seconds=20)
        with Session(database) as session:
            before = session.get(WorkflowNodeAttemptOwnership, guard.attempt_id)
            assert before is not None
            original = before.model_dump_json()

        def fail_after_update(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _many: bool,
        ) -> None:
            if statement.startswith("UPDATE workflow_node_attempt_ownership"):
                message = "injected renewal transaction failure"
                raise RuntimeError(message)

        event.listen(database, "after_cursor_execute", fail_after_update)
        with pytest.raises(RuntimeError, match="injected renewal transaction failure"):
            domain.renew_provider_attempt(guard)
        event.remove(database, "after_cursor_execute", fail_after_update)
        with Session(database) as session:
            after = session.get(WorkflowNodeAttemptOwnership, guard.attempt_id)
            assert after is not None
            assert after.model_dump_json() == original
        clock.now_value = EVALUATED_AT + timedelta(seconds=LEASE_SECONDS)
        assert (
            _decision(domain, project_id, NODE).recommendation_kind
            is RecommendationKind.RECOVERY
        )
    finally:
        database.dispose()


def test_locally_revoked_owner_cannot_renew_or_record_an_outcome(
    engine: Engine,
) -> None:
    """An unexpired database lease cannot restore revoked local authority."""
    domain, _clock, project_id, _request, _started, guard = _system(engine)
    before = _immutable_history(engine)
    guard.revoke()
    with bind_attempt_ownership(guard):
        with pytest.raises(AttemptOwnershipLostError):
            domain.renew_provider_attempt(guard)
        refused = domain.transition(_fail(project_id, guard))
        assert refused.error is not None
        assert refused.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
    with Session(engine) as session:
        attempt = session.get(WorkflowNodeAttempt, guard.attempt_id)
        assert attempt is not None
        assert attempt.model_dump_json() == before[0]
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.idempotency_key == "owned-start"
            )
        ).one()
        assert receipt.model_dump_json() == before[1]
        assert not session.exec(select(WorkflowNodeAttemptOutcome)).all()
