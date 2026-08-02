"""One-shot transactional Initial Scope Registration tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import pytest
from sqlmodel import Session, col, select

import workflow.handlers.onboarding as onboarding_handlers
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecRegistry
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    InitialScopeRegistration,
    PrdDecision,
    PrdVersion,
    SpecDraft,
    SpecDraftDecision,
    WorkflowTransitionReceipt,
)
from services.specs.lifecycle_service import (
    ApprovedCanonicalSpec,
    register_approved_spec_from_canonical_json,
)
from workflow import RegisterInitialScope, WorkflowDomain
from workflow.clock import FixedClock
from workflow.contracts import (
    NodeCategory,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 16, tzinfo=UTC)
ACTOR = "operator@example.com"


@dataclass(frozen=True)
class _AcceptedDraft:
    """Persisted identities for one accepted initial specification draft."""

    project_id: int
    discovery_run_id: int
    spec_draft_id: int
    canonical_content_json: str
    provenance_path: str | None


class _RegistrationProbeError(RuntimeError):
    """Injected failure after the low-level spec insert."""


@pytest.fixture
def domain(engine: Engine) -> WorkflowDomain:
    """Build a deterministic workflow domain."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _required_id(value: int | None) -> int:
    assert value is not None
    return value


def _seed_accepted_initial_draft(
    engine: Engine,
    *,
    name: str,
    canonical_content: Mapping[str, object],
    provenance_path: str | None,
) -> _AcceptedDraft:
    canonical_content_json = canonical_json(canonical_content)
    with Session(engine) as session:
        project = Product(
            name=name,
            origin="greenfield",
            created_at=EVALUATED_AT,
            updated_at=EVALUATED_AT,
        )
        session.add(project)
        session.flush()
        project_id = _required_id(project.product_id)
        run = DiscoveryRun(
            project_id=project_id,
            purpose="initial",
            ordinal=1,
            created_at=EVALUATED_AT,
        )
        session.add(run)
        session.flush()
        run_id = _required_id(run.discovery_run_id)
        challenge = ChallengeArtifact(
            project_id=project_id,
            discovery_run_id=run_id,
            version_number=1,
            canonical_content_json=canonical_json({"challenge": name}),
            content_fingerprint=canonical_hash({"challenge": name}),
            supersedes_challenge_artifact_id=None,
            provenance_path=None,
            created_at=EVALUATED_AT,
        )
        session.add(challenge)
        session.flush()
        prd = PrdVersion(
            project_id=project_id,
            discovery_run_id=run_id,
            version_number=1,
            canonical_content_json=canonical_json({"prd": name}),
            content_fingerprint=canonical_hash({"prd": name}),
            supersedes_prd_version_id=None,
            provenance_path=None,
            created_at=EVALUATED_AT,
        )
        session.add(prd)
        session.flush()
        prd_id = _required_id(prd.prd_version_id)
        session.add(
            PrdDecision(
                project_id=project_id,
                discovery_run_id=run_id,
                prd_version_id=prd_id,
                artifact_fingerprint=prd.content_fingerprint,
                decision="accepted",
                reviewer=ACTOR,
                notes="Accepted",
                idempotency_key=f"{name}-prd-decision",
                decided_at=EVALUATED_AT,
            )
        )
        draft = SpecDraft(
            project_id=project_id,
            discovery_run_id=run_id,
            kind="initial",
            version_number=1,
            canonical_content_json=canonical_content_json,
            content_fingerprint=canonical_hash(canonical_content),
            base_spec_version_id=None,
            base_spec_hash=None,
            supersedes_spec_draft_id=None,
            provenance_path=provenance_path,
            created_at=EVALUATED_AT,
        )
        session.add(draft)
        session.flush()
        draft_id = _required_id(draft.spec_draft_id)
        session.add(
            SpecDraftDecision(
                project_id=project_id,
                discovery_run_id=run_id,
                spec_draft_id=draft_id,
                artifact_fingerprint=draft.content_fingerprint,
                decision="accepted",
                reviewer=ACTOR,
                notes="Accepted",
                idempotency_key=f"{name}-spec-decision",
                decided_at=EVALUATED_AT,
            )
        )
        session.commit()
    return _AcceptedDraft(
        project_id=project_id,
        discovery_run_id=run_id,
        spec_draft_id=draft_id,
        canonical_content_json=canonical_content_json,
        provenance_path=provenance_path,
    )


def _registration_request(
    position: WorkflowPosition,
    spec_draft_id: int,
    *,
    key: str,
) -> RegisterInitialScope:
    decision = next(
        item
        for item in position.decisions
        if item.node_id == RegisterInitialScope.node_id
    )
    assert decision.category is NodeCategory.AVAILABLE
    return RegisterInitialScope(
        project_id=position.project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=key,
        actor=ACTOR,
        correlation_id="task-7-registration",
        instance_key=decision.instance_key,
        spec_draft_id=spec_draft_id,
    )


def _register(
    domain: WorkflowDomain,
    accepted: _AcceptedDraft,
    *,
    key: str,
) -> TransitionResult:
    return domain.transition(
        _registration_request(
            domain.position(accepted.project_id),
            accepted.spec_draft_id,
            key=key,
        )
    )


def test_registration_uses_stored_content_after_source_mutation_and_deletion(
    domain: WorkflowDomain,
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Treat the provenance file as non-authoritative after draft review."""
    source = tmp_path / "accepted-spec.json"
    source.write_text('{"scope":"original file"}', encoding="utf-8")
    canonical_content = {
        "scope": ["stored", "accepted"],
        "constraints": {"raw_spec_bypass": False},
    }
    accepted = _seed_accepted_initial_draft(
        engine,
        name="Stored Registration",
        canonical_content=canonical_content,
        provenance_path=str(source),
    )
    source.write_text('{"scope":"mutated file"}', encoding="utf-8")
    source.unlink()

    result = _register(domain, accepted, key="register-stored")

    assert result.ok is True
    spec_version_id = result.output.get("spec_version_id")
    spec_hash = result.output.get("spec_hash")
    assert isinstance(spec_version_id, int)
    assert spec_hash == canonical_hash(canonical_content)
    assert result.position is not None
    with Session(engine) as session:
        spec = session.get(SpecRegistry, spec_version_id)
        assert spec is not None
        assert spec.content == accepted.canonical_content_json
        assert spec.spec_hash == canonical_hash(canonical_content)
        assert spec.content_ref == str(source)
        assert spec.status == "approved"
        assert spec.approved_at == EVALUATED_AT.replace(tzinfo=None)
        assert spec.approved_by == ACTOR
        registration = session.exec(
            select(InitialScopeRegistration).where(
                col(InitialScopeRegistration.project_id) == accepted.project_id
            )
        ).one()
        assert registration.spec_draft_id == accepted.spec_draft_id
        assert registration.spec_version_id == spec_version_id
        assert registration.spec_hash == spec_hash
        assert session.exec(select(CompiledSpecAuthority)).all() == []
    assert "authority.compile" in result.position.available_nodes
    assert all(
        not node.startswith("backlog.") for node in result.position.available_nodes
    )


@pytest.mark.parametrize(
    "tampered_content_json",
    [
        pytest.param(
            canonical_json({"scope": ["post-review tamper"]}),
            id="reviewed-fingerprint-mismatch",
        ),
        pytest.param('{"scope":', id="malformed-json"),
    ],
)
def test_registration_rejects_post_review_stored_content_tampering_without_writes(
    domain: WorkflowDomain,
    engine: Engine,
    tampered_content_json: str,
) -> None:
    """Re-bind stored draft bytes to the exact terminal review at mutation time."""
    reviewed_content = {"scope": ["reviewed and accepted"]}
    reviewed_fingerprint = canonical_hash(reviewed_content)
    accepted = _seed_accepted_initial_draft(
        engine,
        name=f"Registration Tamper {tampered_content_json}",
        canonical_content=reviewed_content,
        provenance_path=None,
    )
    request = _registration_request(
        domain.position(accepted.project_id),
        accepted.spec_draft_id,
        key=f"register-tamper-{canonical_hash(tampered_content_json)}",
    )
    with Session(engine) as session:
        draft = session.get(SpecDraft, accepted.spec_draft_id)
        assert draft is not None
        assert draft.content_fingerprint == reviewed_fingerprint
        draft.canonical_content_json = tampered_content_json
        session.add(draft)
        session.commit()

    result = domain.transition(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert session.exec(select(SpecRegistry)).all() == []
        assert session.exec(select(InitialScopeRegistration)).all() == []
        draft = session.get(SpecDraft, accepted.spec_draft_id)
        assert draft is not None
        assert draft.canonical_content_json == tampered_content_json
        assert draft.content_fingerprint == reviewed_fingerprint
        review = session.exec(
            select(SpecDraftDecision).where(
                col(SpecDraftDecision.spec_draft_id) == accepted.spec_draft_id
            )
        ).one()
        assert review.artifact_fingerprint == reviewed_fingerprint


def test_registration_replay_cannot_create_duplicate_spec_or_binding(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Return the persisted result when the registration key is replayed."""
    accepted = _seed_accepted_initial_draft(
        engine,
        name="Registration Replay",
        canonical_content={"scope": ["one-shot"]},
        provenance_path=None,
    )
    request = _registration_request(
        domain.position(accepted.project_id),
        accepted.spec_draft_id,
        key="register-replay",
    )

    first = domain.transition(request)
    replay = domain.transition(request)

    assert first.ok is True
    assert replay == first.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert len(session.exec(select(SpecRegistry)).all()) == 1
        assert len(session.exec(select(InitialScopeRegistration)).all()) == 1


def test_low_level_registration_uses_caller_owned_session_without_commit(
    engine: Engine,
) -> None:
    """Allow the domain transaction to own commit or rollback."""
    with Session(engine) as session:
        project = Product(name="Caller Session", origin="greenfield")
        session.add(project)
        session.flush()
        project_id = _required_id(project.product_id)
        spec = register_approved_spec_from_canonical_json(
            session,
            ApprovedCanonicalSpec(
                product_id=project_id,
                canonical_content_json=canonical_json({"scope": ["caller-owned"]}),
                content_ref=None,
                approved_at=EVALUATED_AT,
                approved_by=ACTOR,
                approval_notes="Initial scope registration",
            ),
        )
        spec_id = _required_id(spec.spec_version_id)
        session.rollback()

    with Session(engine) as session:
        assert session.get(SpecRegistry, spec_id) is None
        assert session.get(Product, project_id) is None


def test_registration_failure_rolls_back_spec_receipt_and_binding_atomically(
    domain: WorkflowDomain,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back every registration write when a later handler step fails."""
    accepted = _seed_accepted_initial_draft(
        engine,
        name="Registration Rollback",
        canonical_content={"scope": ["atomic"]},
        provenance_path=None,
    )
    request = _registration_request(
        domain.position(accepted.project_id),
        accepted.spec_draft_id,
        key="register-failure",
    )
    real_register = onboarding_handlers.register_approved_spec_from_canonical_json

    def fail_after_spec_insert(
        session: Session,
        approved: ApprovedCanonicalSpec,
    ) -> NoReturn:
        real_register(session, approved)
        raise _RegistrationProbeError

    monkeypatch.setattr(
        onboarding_handlers,
        "register_approved_spec_from_canonical_json",
        fail_after_spec_insert,
    )

    with pytest.raises(_RegistrationProbeError):
        domain.transition(request)

    with Session(engine) as session:
        assert session.exec(select(SpecRegistry)).all() == []
        assert session.exec(select(InitialScopeRegistration)).all() == []
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == "register-failure"
            )
        ).all()
        assert receipts == []
        assert session.get(SpecDraft, accepted.spec_draft_id) is not None
