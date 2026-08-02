"""Commit-time database constraint tests for durable workflow records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import UniqueConstraint
from sqlmodel import Session, SQLModel, create_engine

from models.core import Product
from models.db import set_sqlite_pragma
from models.specs import SpecRegistry
from models.workflow import (
    DiscoveryRun,
    InitialScopeRegistration,
    PrdVersion,
    SpecDraft,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class _SeededProject:
    """Database identities created by a workflow test seed."""

    project_id: int
    discovery_run_id: int


def _create_fresh_engine(*, foreign_keys: bool = True) -> Engine:
    """Create an isolated fresh-schema SQLite engine for one test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    if not foreign_keys:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    return engine


@pytest.fixture
def workflow_engine() -> Iterator[Engine]:
    """Provide a fresh workflow schema with SQLite FK enforcement enabled."""
    engine = _create_fresh_engine()
    yield engine
    engine.dispose()


def seed_project_with_initial_run(
    session: Session,
    *,
    name: str,
) -> _SeededProject:
    """Persist a greenfield Project shell and its initial discovery run."""
    project = Product(name=name, origin="greenfield")
    session.add(project)
    session.flush()
    assert project.product_id is not None

    discovery_run = DiscoveryRun(
        project_id=project.product_id,
        purpose="initial",
        ordinal=1,
    )
    session.add(discovery_run)
    session.commit()
    session.refresh(discovery_run)
    assert discovery_run.discovery_run_id is not None
    return _SeededProject(
        project_id=project.product_id,
        discovery_run_id=discovery_run.discovery_run_id,
    )


def seed_initial_registration(
    session: Session,
    *,
    name: str,
) -> InitialScopeRegistration:
    """Persist one complete initial-scope registration identity."""
    seeded = seed_project_with_initial_run(session, name=name)
    draft = SpecDraft(
        project_id=seeded.project_id,
        discovery_run_id=seeded.discovery_run_id,
        kind="initial",
        version_number=1,
        canonical_content_json="{}",
        content_fingerprint=f"sha256:{name}:draft",
        base_spec_version_id=None,
        base_spec_hash=None,
        supersedes_spec_draft_id=None,
        provenance_path=None,
    )
    spec = SpecRegistry(
        product_id=seeded.project_id,
        spec_hash=f"sha256:{name}:spec",
        content=f"# {name}",
    )
    session.add(draft)
    session.add(spec)
    session.flush()
    assert draft.spec_draft_id is not None
    assert spec.spec_version_id is not None

    registration = InitialScopeRegistration(
        project_id=seeded.project_id,
        discovery_run_id=seeded.discovery_run_id,
        spec_draft_id=draft.spec_draft_id,
        spec_version_id=spec.spec_version_id,
        spec_hash=spec.spec_hash,
        registered_by="tester",
    )
    session.add(registration)
    session.commit()
    session.refresh(registration)
    return registration


def seed_attempt(session: Session, *, project_id: int) -> WorkflowNodeAttempt:
    """Persist one leased workflow node attempt."""
    started_at = datetime.now(UTC)
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="onboarding.greenfield.prd",
        instance_key=None,
        graph_version="workflow.v1",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint="sha256:business",
        decision_fingerprint="sha256:decision",
        normalized_input_json="{}",
        input_fingerprint="sha256:input",
        model_id="test-model",
        execution_settings_json="{}",
        idempotency_key="attempt-1",
        actor="tester",
        correlation_id=None,
        started_at=started_at,
        lease_expires_at=started_at + timedelta(minutes=5),
        attempt_fingerprint="sha256:attempt",
    )
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    assert attempt.workflow_node_attempt_id is not None
    return attempt


def test_test_engine_enables_sqlite_foreign_keys(workflow_engine: Engine) -> None:
    """The fresh-schema test engine enables SQLite FK enforcement."""
    with workflow_engine.connect() as connection:
        enabled = connection.execute(text("PRAGMA foreign_keys")).scalar_one()

    assert enabled == 1


def test_cross_project_prd_requires_sqlite_foreign_keys() -> None:
    """The cross-Project rejection depends on SQLite FK enforcement."""
    engine = _create_fresh_engine(foreign_keys=False)
    try:
        with Session(engine) as session:
            first = seed_project_with_initial_run(session, name="first-disabled")
            second = seed_project_with_initial_run(session, name="second-disabled")
            session.add(
                PrdVersion(
                    project_id=second.project_id,
                    discovery_run_id=first.discovery_run_id,
                    version_number=1,
                    canonical_content_json="{}",
                    content_fingerprint="sha256:disabled",
                    supersedes_prd_version_id=None,
                    provenance_path=None,
                )
            )
            session.commit()
    finally:
        engine.dispose()


def test_two_initial_discovery_runs_are_rejected(
    workflow_engine: Engine,
) -> None:
    """A Project cannot own two initial discovery runs."""
    with Session(workflow_engine) as session:
        seeded = seed_project_with_initial_run(session, name="initial-cardinality")
        session.add(
            DiscoveryRun(
                project_id=seeded.project_id,
                purpose="initial",
                ordinal=2,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_two_open_extension_runs_are_rejected(workflow_engine: Engine) -> None:
    """A Project cannot own two unresolved extension discovery runs."""
    with Session(workflow_engine) as session:
        registration = seed_initial_registration(
            session,
            name="extension-cardinality",
        )
        session.add(
            DiscoveryRun(
                project_id=registration.project_id,
                purpose="extension",
                ordinal=2,
                base_spec_version_id=registration.spec_version_id,
                base_spec_hash=registration.spec_hash,
            )
        )
        session.commit()
        session.add(
            DiscoveryRun(
                project_id=registration.project_id,
                purpose="extension",
                ordinal=3,
                base_spec_version_id=registration.spec_version_id,
                base_spec_hash=registration.spec_hash,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_cross_project_prd_is_rejected(workflow_engine: Engine) -> None:
    """A PRD cannot reference another Project's discovery run."""
    with Session(workflow_engine) as session:
        first = seed_project_with_initial_run(session, name="first")
        second = seed_project_with_initial_run(session, name="second")
        session.add(
            PrdVersion(
                project_id=second.project_id,
                discovery_run_id=first.discovery_run_id,
                version_number=1,
                canonical_content_json="{}",
                content_fingerprint="sha256:prd",
                supersedes_prd_version_id=None,
                provenance_path=None,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_initial_spec_draft_with_base_spec_is_rejected(
    workflow_engine: Engine,
) -> None:
    """An initial specification draft cannot declare a base spec."""
    with Session(workflow_engine) as session:
        seeded = seed_project_with_initial_run(session, name="initial-base")
        spec = SpecRegistry(
            product_id=seeded.project_id,
            spec_hash="sha256:base",
            content="# Base",
        )
        session.add(spec)
        session.flush()
        assert spec.spec_version_id is not None
        session.add(
            SpecDraft(
                project_id=seeded.project_id,
                discovery_run_id=seeded.discovery_run_id,
                kind="initial",
                version_number=1,
                canonical_content_json="{}",
                content_fingerprint="sha256:draft",
                base_spec_version_id=spec.spec_version_id,
                base_spec_hash=spec.spec_hash,
                supersedes_spec_draft_id=None,
                provenance_path=None,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_amendment_spec_draft_without_base_spec_is_rejected(
    workflow_engine: Engine,
) -> None:
    """An amendment specification draft must declare its base identity."""
    with Session(workflow_engine) as session:
        seeded = seed_project_with_initial_run(session, name="amendment-base")
        session.add(
            SpecDraft(
                project_id=seeded.project_id,
                discovery_run_id=seeded.discovery_run_id,
                kind="amendment",
                version_number=1,
                canonical_content_json="{}",
                content_fingerprint="sha256:draft",
                base_spec_version_id=None,
                base_spec_hash=None,
                supersedes_spec_draft_id=None,
                provenance_path=None,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_two_initial_registrations_for_one_project_are_rejected(
    workflow_engine: Engine,
) -> None:
    """A Project cannot have two initial-scope registrations."""
    with Session(workflow_engine) as session:
        registration = seed_initial_registration(session, name="registration")
        session.add(
            InitialScopeRegistration(
                project_id=registration.project_id,
                discovery_run_id=registration.discovery_run_id,
                spec_draft_id=registration.spec_draft_id,
                spec_version_id=registration.spec_version_id,
                spec_hash=registration.spec_hash,
                registered_by="tester-again",
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_initial_registration_identity_columns_are_independently_unique() -> None:
    """Each initial registration identity component is independently unique."""
    table = SQLModel.metadata.tables["initial_scope_registrations"]
    unique_column_sets = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert {
        ("project_id",),
        ("discovery_run_id",),
        ("spec_draft_id",),
        ("spec_version_id",),
    } <= unique_column_sets


def test_two_outcomes_for_one_attempt_are_rejected(
    workflow_engine: Engine,
) -> None:
    """A workflow node attempt cannot have two terminal outcomes."""
    with Session(workflow_engine) as session:
        seeded = seed_project_with_initial_run(session, name="attempt-outcome")
        attempt = seed_attempt(session, project_id=seeded.project_id)
        recorded_at = datetime.now(UTC)
        session.add(
            WorkflowNodeAttemptOutcome(
                project_id=seeded.project_id,
                workflow_node_attempt_id=attempt.workflow_node_attempt_id,
                status="success",
                output_fingerprint="sha256:output",
                output_json="{}",
                failure_code=None,
                failure_message=None,
                recorded_at=recorded_at,
            )
        )
        session.commit()
        session.add(
            WorkflowNodeAttemptOutcome(
                project_id=seeded.project_id,
                workflow_node_attempt_id=attempt.workflow_node_attempt_id,
                status="obsolete",
                output_fingerprint=None,
                output_json=None,
                failure_code=None,
                failure_message=None,
                recorded_at=recorded_at,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_transition_receipt_key_is_unique_within_request_kind(
    workflow_engine: Engine,
) -> None:
    """A request kind cannot reuse the same transition idempotency key."""
    started_at = datetime.now(UTC)
    with Session(workflow_engine) as session:
        session.add(
            WorkflowTransitionReceipt(
                request_kind="open_project_shell",
                idempotency_key="request-1",
                request_fingerprint="sha256:first",
                request_json="{}",
                result_json=None,
                started_at=started_at,
                completed_at=None,
            )
        )
        session.commit()
        session.add(
            WorkflowTransitionReceipt(
                request_kind="open_project_shell",
                idempotency_key="request-1",
                request_fingerprint="sha256:second",
                request_json="{}",
                result_json=None,
                started_at=started_at,
                completed_at=None,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()
