"""Deep accepted-Specification loading and integrity classification."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from models.product_definition import (
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
)
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowTransitionReceipt
from services.specs.accepted_specification import (
    AcceptedSpecificationIntegrityError,
    load_accepted_specification,
    load_current_accepted_specification,
    require_current_accepted_specification,
)
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    build_candidate_envelope,
    canonical_candidate_json,
    load_candidate_contract,
)
from tests.workflow.test_product_discovery_transitions import (
    NOW,
    _accept_request,
    _domain,
    _payload,
    _ready_project,
    _structure,
)
from utils.agileforge_spec_profile_v2 import canonical_spec_json
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.handlers import product_discovery as handler_module
from workflow.requests import DecideSpecification

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.domain import WorkflowDomain


def _accept_specification(
    engine: Engine,
    tmp_path: Path,
    *,
    key: str,
) -> tuple[int, SpecRegistry, SpecificationCandidate, SpecificationDecision]:
    project_id, *_lineage, probe = _ready_project(engine, tmp_path, name=key)
    domain = _domain(engine, repository_probe=probe)
    structured = _structure(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key=key,
        repository_probe=probe,
    )
    assert structured.ok
    review_domain = _domain(
        engine,
        at=NOW + timedelta(seconds=1),
        repository_probe=probe,
    )
    accepted = review_domain.transition(
        _accept_request(review_domain, project_id=project_id, key=f"{key}-review")
    )
    assert accepted.ok
    with Session(engine) as session:
        registry = session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).one()
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        decision = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).one()
        session.expunge(registry)
        session.expunge(candidate)
        session.expunge(decision)
    return project_id, registry, candidate, decision


def _force_sql(session: Session, statement: str, params: tuple[object, ...]) -> None:
    session.connection().exec_driver_sql("PRAGMA foreign_keys = OFF")
    session.connection().exec_driver_sql(statement, params)
    session.commit()


def _id(value: int | None) -> int:
    assert value is not None
    return value


def _assert_code(code: str, call: Callable[[], object]) -> None:
    with pytest.raises(AcceptedSpecificationIntegrityError) as raised:
        call()
    assert raised.value.code == code


def test_load_exact_and_current_return_decision_grounded_canonical_contract(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Return exact canonical bytes and decision metadata for current loading."""
    project_id, registry, candidate, decision = _accept_specification(
        engine, tmp_path, key="accepted-loader"
    )

    with Session(engine) as session:
        exact = load_accepted_specification(
            session,
            project_id=project_id,
            spec_version_id=_id(registry.spec_version_id),
            spec_hash=registry.spec_hash,
        )
        current = load_current_accepted_specification(session, project_id=project_id)

    assert current == exact
    assert exact.project_id == project_id
    assert exact.status == "approved"
    assert exact.specification_decision_id == decision.specification_decision_id
    assert exact.accepted_at == decision.decided_at
    assert exact.accepted_by == decision.reviewer
    assert exact.acceptance_notes == decision.rationale
    assert (
        exact.source_specification_candidate_id == candidate.specification_candidate_id
    )
    assert exact.source_specification_candidate_fingerprint == (
        candidate.candidate_fingerprint
    )
    assert exact.canonical_specification_json == canonical_spec_json(exact.payload)
    with pytest.raises(FrozenInstanceError):
        exact.status = "superseded"  # ty: ignore[invalid-assignment]


def test_exact_load_accepts_superseded_but_new_planning_rejects_it(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Keep history loadable while rejecting it as a new-planning root."""
    project_id, registry, *_ = _accept_specification(
        engine, tmp_path, key="historical-loader"
    )
    with Session(engine) as session:
        _force_sql(
            session,
            "UPDATE spec_registry SET status = 'superseded' WHERE spec_version_id = ?",
            (registry.spec_version_id,),
        )
    with Session(engine) as session:
        historical = load_accepted_specification(
            session,
            project_id=project_id,
            spec_version_id=_id(registry.spec_version_id),
            spec_hash=registry.spec_hash,
        )
        assert historical.status == "superseded"
        assert (
            load_current_accepted_specification(session, project_id=project_id) is None
        )
        _assert_code(
            "STALE_SPECIFICATION",
            lambda: require_current_accepted_specification(
                session,
                project_id=project_id,
                spec_version_id=_id(registry.spec_version_id),
                spec_hash=registry.spec_hash,
            ),
        )


def _persist_relational_amendment_candidate(
    engine: Engine,
    *,
    project_id: int,
    registry: SpecRegistry,
    candidate: SpecificationCandidate,
    key: str,
) -> int:
    """Persist a relationally valid amendment row for transaction-boundary tests."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )
        original_attempt = session.get(
            WorkflowNodeAttempt,
            candidate.workflow_node_attempt_id,
        )
        assert original_attempt is not None
        attempt_fingerprint = canonical_hash({"race-amendment-attempt": key})
        amendment_attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id=original_attempt.node_id,
            instance_key=original_attempt.instance_key,
            graph_version=original_attempt.graph_version,
            fact_fingerprint=original_attempt.fact_fingerprint,
            business_fact_fingerprint=original_attempt.business_fact_fingerprint,
            decision_fingerprint=original_attempt.decision_fingerprint,
            normalized_input_json=original_attempt.normalized_input_json,
            input_fingerprint=original_attempt.input_fingerprint,
            model_id=original_attempt.model_id,
            execution_settings_json=original_attempt.execution_settings_json,
            idempotency_key=f"{key}-attempt",
            actor=original_attempt.actor,
            correlation_id=f"{key}-correlation",
            started_at=original_attempt.started_at,
            lease_expires_at=original_attempt.lease_expires_at,
            attempt_fingerprint=attempt_fingerprint,
        )
        session.add(amendment_attempt)
        session.flush()
        amendment_attempt_id = _id(amendment_attempt.workflow_node_attempt_id)
        payload, original_envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
        recorded_at = NOW + timedelta(seconds=2)
        envelope = build_candidate_envelope(
            payload=payload,
            metadata=CandidateBuildInput(
                candidate_kind=CandidateKind.AMENDMENT,
                accepted_vision_id=original_envelope.accepted_vision_id,
                accepted_vision_fingerprint=(
                    original_envelope.accepted_vision_fingerprint
                ),
                accepted_product_goal_id=(original_envelope.accepted_product_goal_id),
                accepted_product_goal_fingerprint=(
                    original_envelope.accepted_product_goal_fingerprint
                ),
                registered_source_fingerprint=(
                    original_envelope.registered_source_fingerprint
                ),
                source_producer_capability=(
                    original_envelope.source_producer_capability
                ),
                source_preparation_capability=(
                    original_envelope.source_preparation_capability
                ),
                source_manifest=original_envelope.source_manifest,
                accepted_fact_fingerprint=(original_envelope.accepted_fact_fingerprint),
                producer_input_fingerprint=(
                    original_envelope.producer_input_fingerprint
                ),
                producer_capability=original_envelope.producer_capability,
                producer_version=original_envelope.producer_version,
                model_id=original_envelope.model_id,
                model_configuration_fingerprint=(
                    original_envelope.model_configuration_fingerprint
                ),
                prompt_version=original_envelope.prompt_version,
                prompt_fingerprint=original_envelope.prompt_fingerprint,
                workflow_node_attempt_id=amendment_attempt_id,
                attempt_fingerprint=attempt_fingerprint,
                correlation_id=f"{key}-correlation",
                produced_at=recorded_at,
                base_payload=payload,
                base_specification_id=_id(registry.spec_version_id),
                base_payload_fingerprint=registry.spec_hash,
            ),
        )
        amendment = SpecificationCandidate(
            project_id=project_id,
            candidate_kind="amendment",
            specification_source_id=candidate.specification_source_id,
            specification_source_fingerprint=(
                candidate.specification_source_fingerprint
            ),
            vision_artifact_id=candidate.vision_artifact_id,
            vision_fingerprint=candidate.vision_fingerprint,
            product_goal_artifact_id=candidate.product_goal_artifact_id,
            product_goal_fingerprint=candidate.product_goal_fingerprint,
            base_spec_version_id=_id(registry.spec_version_id),
            base_spec_hash=registry.spec_hash,
            canonical_envelope_json=canonical_candidate_json(payload, envelope),
            payload_fingerprint=envelope.payload_fingerprint,
            source_manifest_fingerprint=envelope.source_manifest_fingerprint,
            producer_input_fingerprint=envelope.producer_input_fingerprint,
            rendered_view_fingerprint=envelope.review_view_fingerprint,
            candidate_fingerprint=envelope.candidate_fingerprint,
            workflow_node_attempt_id=amendment_attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            supersedes_specification_candidate_id=None,
            supersedes_candidate_fingerprint=None,
            recorded_by="transaction-test",
            recorded_at=recorded_at,
        )
        session.add(amendment)
        session.commit()
        return _id(amendment.specification_candidate_id)


def _amendment_review_contract(
    *,
    project_id: int,
    candidate_id: int,
    candidate_fingerprint: str,
    key: str,
) -> tuple[DecideSpecification, NodeDecision]:
    request = DecideSpecification(
        project_id=project_id,
        graph_version="transaction-test",
        fact_fingerprint=canonical_hash({"facts": key}),
        decision_fingerprint=canonical_hash({"decision": key}),
        idempotency_key=f"{key}-race-review",
        actor="operator",
        specification_candidate_id=candidate_id,
        candidate_fingerprint=candidate_fingerprint,
        decision="accepted",
    )
    decision = NodeDecision(
        node_id="specification.review",
        child_graph_id="specification",
        request_kind=request.kind,
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="TRANSACTION_TEST",
        decision_fingerprint=request.decision_fingerprint,
    )
    return request, decision


def _domain_for_review_boundary(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    request: DecideSpecification,
    node_decision: NodeDecision,
    source_fingerprint: str,
) -> WorkflowDomain:
    """Expose one real receipt boundary around the focused review handler."""
    domain = _domain(engine)
    prepared_request = DecideSpecification.model_validate(
        {
            **request.model_dump(mode="json"),
            "repository_source_fingerprint": source_fingerprint,
        }
    )
    position = WorkflowPosition(
        project_id=request.project_id,
        graph_version=request.graph_version,
        fact_fingerprint=request.fact_fingerprint,
        evaluated_at=NOW + timedelta(seconds=2),
        available_nodes=("specification.review",),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=(node_decision,),
    )
    monkeypatch.setattr(domain, "_guarded_decision", lambda *_args: node_decision)
    monkeypatch.setattr(
        domain,
        "_revalidate_specification_acceptance",
        lambda *_args: prepared_request,
    )
    monkeypatch.setattr(domain, "_position_in_session", lambda *_args: position)
    return domain


def _current_unique_race_injector(
    *,
    project_id: int,
    base_id: int,
    evidence: list[tuple[int, str]],
) -> Callable[..., None]:
    """Restore the base only at the new-current flush to force the exact race."""

    def force_current_unique_race(
        flush_session: Session,
        *_args: object,
    ) -> None:
        if evidence or not any(
            isinstance(item, SpecRegistry) for item in flush_session.new
        ):
            return
        assert (
            flush_session.connection()
            .exec_driver_sql("PRAGMA foreign_keys")
            .scalar_one()
            == 1
        )
        decision_count = (
            flush_session.connection()
            .exec_driver_sql(
                "SELECT COUNT(*) FROM specification_decisions WHERE project_id = ?",
                (project_id,),
            )
            .scalar_one()
        )
        persisted_status = (
            flush_session.connection()
            .exec_driver_sql(
                "SELECT status FROM spec_registry WHERE spec_version_id = ?",
                (base_id,),
            )
            .scalar_one()
        )
        evidence.append((decision_count, persisted_status))
        flush_session.connection().exec_driver_sql(
            "UPDATE spec_registry SET status = 'approved' WHERE spec_version_id = ?",
            (base_id,),
        )

    return force_current_unique_race


def _commit_distinct_winner(
    engine: Engine,
    *,
    project_id: int,
    base_id: int,
    candidate_id: int,
    key: str,
) -> tuple[tuple[int, str], tuple[str, int]]:
    """Commit the exact valid amendment after observing loser rollback."""
    with Session(engine) as session:
        base = session.get(SpecRegistry, base_id)
        candidate = session.get(SpecificationCandidate, candidate_id)
        assert base is not None
        assert candidate is not None
        losing_decisions = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id,
                col(SpecificationDecision.specification_candidate_id) == candidate_id,
            )
        ).all()
        rollback_evidence = (base.status, len(losing_decisions))
        winner_decision = SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=candidate_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            decision="accepted",
            rationale="Committed concurrent winner.",
            reviewer="concurrent-operator",
            idempotency_key=f"{key}-winner-review",
            decided_at=NOW + timedelta(seconds=3),
        )
        session.add(winner_decision)
        session.flush()
        base.status = "superseded"
        session.add(base)
        session.flush()
        winner = SpecRegistry(
            project_id=project_id,
            spec_hash=candidate.payload_fingerprint,
            status="approved",
            source_specification_decision_id=_id(
                winner_decision.specification_decision_id
            ),
            source_specification_candidate_id=candidate_id,
            source_specification_candidate_fingerprint=(
                candidate.candidate_fingerprint
            ),
            source_vision_artifact_id=candidate.vision_artifact_id,
            source_vision_fingerprint=candidate.vision_fingerprint,
            source_product_goal_artifact_id=candidate.product_goal_artifact_id,
            source_product_goal_fingerprint=candidate.product_goal_fingerprint,
            supersedes_spec_version_id=base_id,
        )
        session.add(winner)
        session.commit()
        return (_id(winner.spec_version_id), winner.spec_hash), rollback_evidence


def test_expected_current_race_rolls_back_whole_acceptance_and_returns_stale(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback decision and base mutation before stable stale classification."""
    project_id, registry, candidate, _decision = _accept_specification(
        engine,
        tmp_path,
        key="expected-current-race",
    )
    candidate_id = _persist_relational_amendment_candidate(
        engine,
        project_id=project_id,
        registry=registry,
        candidate=candidate,
        key="expected-current-race",
    )
    with Session(engine) as session:
        amendment = session.get(SpecificationCandidate, candidate_id)
        assert amendment is not None
        request, node_decision = _amendment_review_contract(
            project_id=project_id,
            candidate_id=candidate_id,
            candidate_fingerprint=amendment.candidate_fingerprint,
            key="expected-current-race",
        )
        source_fingerprint = amendment.specification_source_fingerprint

    base_id = _id(registry.spec_version_id)
    reloads: list[tuple[int, str] | None] = []
    winner_identities: list[tuple[int, str]] = []
    post_rollback_evidence: list[tuple[str, int]] = []
    real_loader = handler_module.load_current_accepted_specification

    def persisted_target(
        session: Session,
        _request: DecideSpecification,
        _decision: NodeDecision,
    ) -> tuple[SpecificationCandidate, SpecRegistry]:
        persisted_candidate = session.get(SpecificationCandidate, candidate_id)
        persisted_base = session.get(SpecRegistry, base_id)
        assert persisted_candidate is not None
        assert persisted_base is not None
        return persisted_candidate, persisted_base

    def track_reload(session: Session, *, project_id: int) -> object:
        winner_identity, rollback_evidence = _commit_distinct_winner(
            engine,
            project_id=project_id,
            base_id=base_id,
            candidate_id=candidate_id,
            key="expected-current-race",
        )
        winner_identities.append(winner_identity)
        post_rollback_evidence.append(rollback_evidence)
        current = real_loader(session, project_id=project_id)
        reloads.append(
            None if current is None else (current.spec_version_id, current.spec_hash)
        )
        return current

    monkeypatch.setattr(handler_module, "_validated_review_target", persisted_target)
    monkeypatch.setattr(
        handler_module,
        "load_current_accepted_specification",
        track_reload,
    )
    domain = _domain_for_review_boundary(
        engine,
        monkeypatch,
        request=request,
        node_decision=node_decision,
        source_fingerprint=source_fingerprint,
    )
    injection_evidence: list[tuple[int, str]] = []
    force_current_unique_race = _current_unique_race_injector(
        project_id=project_id,
        base_id=base_id,
        evidence=injection_evidence,
    )

    event.listen(Session, "before_flush", force_current_unique_race)
    try:
        result = domain.transition(request)
    finally:
        event.remove(Session, "before_flush", force_current_unique_race)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "STALE_SPECIFICATION"
    assert (injection_evidence, post_rollback_evidence) == (
        [(2, "superseded")],
        [("approved", 0)],
    )
    assert reloads == winner_identities
    assert winner_identities[0] != (base_id, registry.spec_hash)
    replay = domain.transition(request)
    assert replay == result.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )
        rows = session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).all()
        decisions = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).all()
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == request.idempotency_key
            )
        ).all()
    assert [(row.spec_version_id, row.status) for row in rows] == [
        (base_id, "superseded"),
        (winner_identities[0][0], "approved"),
    ]
    assert (len(decisions), len(receipts)) == (2, 1)


def test_unchanged_base_partial_unique_propagates_original_error(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not persist stale when the reloaded amendment base is unchanged."""
    project_id, registry, candidate, _decision = _accept_specification(
        engine,
        tmp_path,
        key="unchanged-base-race",
    )
    candidate_id = _persist_relational_amendment_candidate(
        engine,
        project_id=project_id,
        registry=registry,
        candidate=candidate,
        key="unchanged-base-race",
    )
    with Session(engine) as session:
        amendment = session.get(SpecificationCandidate, candidate_id)
        assert amendment is not None
        request, node_decision = _amendment_review_contract(
            project_id=project_id,
            candidate_id=candidate_id,
            candidate_fingerprint=amendment.candidate_fingerprint,
            key="unchanged-base-race",
        )
        source_fingerprint = amendment.specification_source_fingerprint

    base_id = _id(registry.spec_version_id)
    reloads: list[tuple[int, str] | None] = []
    real_loader = handler_module.load_current_accepted_specification

    def persisted_target(
        session: Session,
        _request: DecideSpecification,
        _decision: NodeDecision,
    ) -> tuple[SpecificationCandidate, SpecRegistry]:
        persisted_candidate = session.get(SpecificationCandidate, candidate_id)
        persisted_base = session.get(SpecRegistry, base_id)
        assert persisted_candidate is not None
        assert persisted_base is not None
        return persisted_candidate, persisted_base

    def track_reload(session: Session, *, project_id: int) -> object:
        current = real_loader(session, project_id=project_id)
        reloads.append(
            None if current is None else (current.spec_version_id, current.spec_hash)
        )
        return current

    monkeypatch.setattr(handler_module, "_validated_review_target", persisted_target)
    monkeypatch.setattr(
        handler_module,
        "load_current_accepted_specification",
        track_reload,
    )
    domain = _domain_for_review_boundary(
        engine,
        monkeypatch,
        request=request,
        node_decision=node_decision,
        source_fingerprint=source_fingerprint,
    )
    injection_evidence: list[tuple[int, str]] = []
    force_unchanged_base_unique_failure = _current_unique_race_injector(
        project_id=project_id,
        base_id=base_id,
        evidence=injection_evidence,
    )

    event.listen(Session, "before_flush", force_unchanged_base_unique_failure)
    try:
        with pytest.raises(
            IntegrityError,
            match=r"UNIQUE constraint failed: spec_registry\.project_id",
        ):
            domain.transition(request)
    finally:
        event.remove(Session, "before_flush", force_unchanged_base_unique_failure)

    assert injection_evidence == [(2, "superseded")]
    assert reloads == [(base_id, registry.spec_hash)]
    with Session(engine) as session:
        rows = session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).all()
        decisions = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).all()
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == request.idempotency_key
            )
        ).all()
    assert [(row.spec_version_id, row.status) for row in rows] == [
        (base_id, "approved")
    ]
    assert len(decisions) == 1
    assert receipts == []


def test_unrelated_integrity_error_propagates_for_outer_rollback(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not relabel an unrelated FK failure as a current-row race."""
    project_id, registry, candidate, _decision = _accept_specification(
        engine,
        tmp_path,
        key="unrelated-integrity",
    )
    candidate_id = _persist_relational_amendment_candidate(
        engine,
        project_id=project_id,
        registry=registry,
        candidate=candidate,
        key="unrelated-integrity",
    )
    with Session(engine) as session:
        amendment = session.get(SpecificationCandidate, candidate_id)
        assert amendment is not None
        request, node_decision = _amendment_review_contract(
            project_id=project_id,
            candidate_id=candidate_id,
            candidate_fingerprint=amendment.candidate_fingerprint,
            key="unrelated-integrity",
        )
        source_fingerprint = amendment.specification_source_fingerprint

    base_id = _id(registry.spec_version_id)

    def persisted_target(
        session: Session,
        _request: DecideSpecification,
        _decision: NodeDecision,
    ) -> tuple[SpecificationCandidate, SpecRegistry]:
        persisted_candidate = session.get(SpecificationCandidate, candidate_id)
        persisted_base = session.get(SpecRegistry, base_id)
        assert persisted_candidate is not None
        assert persisted_base is not None
        return persisted_candidate, persisted_base

    monkeypatch.setattr(handler_module, "_validated_review_target", persisted_target)
    domain = _domain_for_review_boundary(
        engine,
        monkeypatch,
        request=request,
        node_decision=node_decision,
        source_fingerprint=source_fingerprint,
    )
    injection_evidence: list[str] = []

    def force_unrelated_fk_failure(
        flush_session: Session,
        *_args: object,
    ) -> None:
        if injection_evidence or not any(
            isinstance(item, SpecRegistry) for item in flush_session.new
        ):
            return
        assert (
            flush_session.connection()
            .exec_driver_sql("PRAGMA foreign_keys")
            .scalar_one()
            == 1
        )
        persisted_status = (
            flush_session.connection()
            .exec_driver_sql(
                "SELECT status FROM spec_registry WHERE spec_version_id = ?",
                (base_id,),
            )
            .scalar_one()
        )
        injection_evidence.append(persisted_status)
        flush_session.connection().exec_driver_sql(
            "UPDATE spec_registry SET source_vision_artifact_id = -1 "
            "WHERE spec_version_id = ?",
            (base_id,),
        )

    event.listen(Session, "before_flush", force_unrelated_fk_failure)
    try:
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            domain.transition(request)
    finally:
        event.remove(Session, "before_flush", force_unrelated_fk_failure)

    assert injection_evidence == ["superseded"]
    with Session(engine) as session:
        rows = session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).all()
        decisions = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).all()
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == request.idempotency_key
            )
        ).all()
    assert [(row.spec_version_id, row.status) for row in rows] == [
        (base_id, "approved")
    ]
    assert len(decisions) == 1
    assert receipts == []


@pytest.mark.parametrize(
    ("identity", "expected_code"),
    [
        ("missing-version", "SPECIFICATION_NOT_FOUND"),
        ("foreign-project", "SPECIFICATION_NOT_FOUND"),
        ("wrong-hash", "SPECIFICATION_NOT_FOUND"),
    ],
)
def test_exact_load_rejects_missing_or_foreign_identity(
    engine: Engine,
    tmp_path: Path,
    identity: str,
    expected_code: str,
) -> None:
    """Classify every absent or cross-Project exact identity as not found."""
    project_id, registry, *_ = _accept_specification(
        engine, tmp_path, key=f"missing-{identity}"
    )
    requested_project = (
        project_id + 100 if identity == "foreign-project" else project_id
    )
    requested_version = (
        _id(registry.spec_version_id) + 100
        if identity == "missing-version"
        else _id(registry.spec_version_id)
    )
    requested_hash = (
        "sha256:" + "f" * 64 if identity == "wrong-hash" else registry.spec_hash
    )
    with Session(engine) as session:
        _assert_code(
            expected_code,
            lambda: load_accepted_specification(
                session,
                project_id=requested_project,
                spec_version_id=requested_version,
                spec_hash=requested_hash,
            ),
        )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("non-accepted-decision", "SPECIFICATION_NOT_ACCEPTED"),
        ("missing-decision", "SPECIFICATION_NOT_ACCEPTED"),
        ("candidate-identity", "SPECIFICATION_IDENTITY_MISMATCH"),
        ("payload-hash", "SPECIFICATION_IDENTITY_MISMATCH"),
        ("canonical-bytes", "SPECIFICATION_CANONICAL_BYTES_INVALID"),
        ("source-lineage", "SPECIFICATION_LINEAGE_INVALID"),
    ],
)
def test_exact_load_fails_closed_for_corrupt_accepted_contract(
    engine: Engine,
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    """Fail closed with the stable code for each corruption boundary."""
    project_id, registry, candidate, decision = _accept_specification(
        engine, tmp_path, key=f"corrupt-{mutation}"
    )
    statements = {
        "non-accepted-decision": (
            "UPDATE specification_decisions SET decision = 'feedback' "
            "WHERE specification_decision_id = ?",
            (decision.specification_decision_id,),
        ),
        "missing-decision": (
            "UPDATE spec_registry SET source_specification_decision_id = ? "
            "WHERE spec_version_id = ?",
            (
                _id(decision.specification_decision_id) + 100,
                registry.spec_version_id,
            ),
        ),
        "candidate-identity": (
            "UPDATE specification_candidates SET candidate_fingerprint = ? "
            "WHERE specification_candidate_id = ?",
            ("sha256:" + "a" * 64, candidate.specification_candidate_id),
        ),
        "payload-hash": (
            "UPDATE specification_candidates SET payload_fingerprint = ? "
            "WHERE specification_candidate_id = ?",
            ("sha256:" + "b" * 64, candidate.specification_candidate_id),
        ),
        "canonical-bytes": (
            "UPDATE specification_candidates SET canonical_envelope_json = "
            "canonical_envelope_json || ' ' WHERE specification_candidate_id = ?",
            (candidate.specification_candidate_id,),
        ),
        "source-lineage": (
            "UPDATE spec_registry SET source_vision_fingerprint = ? "
            "WHERE spec_version_id = ?",
            ("sha256:" + "c" * 64, registry.spec_version_id),
        ),
    }
    with Session(engine) as session:
        statement, params = statements[mutation]
        _force_sql(session, statement, params)
    with Session(engine) as session:
        _assert_code(
            expected_code,
            lambda: load_accepted_specification(
                session,
                project_id=project_id,
                spec_version_id=_id(registry.spec_version_id),
                spec_hash=registry.spec_hash,
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed",
        "noncanonical",
        "fingerprint-mismatch",
        "repository-lineage",
        "vision-lineage",
        "product-goal-lineage",
    ],
)
def test_exact_load_rejects_corrupt_registered_source_bundle(
    engine: Engine,
    tmp_path: Path,
    mutation: str,
) -> None:
    """Treat every persisted source-bundle corruption as invalid lineage."""
    project_id, registry, candidate, _decision = _accept_specification(
        engine,
        tmp_path,
        key=f"corrupt-source-bundle-{mutation}",
    )
    with Session(engine) as session:
        source = session.get(
            SpecificationSource,
            candidate.specification_source_id,
        )
        assert source is not None
        if mutation == "malformed":
            source.source_bundle_json = "{"
        elif mutation == "noncanonical":
            source.source_bundle_json += " "
        else:
            bundle = json.loads(source.source_bundle_json)
            if mutation == "fingerprint-mismatch":
                bundle["repository_revision"]["branch_name"] = "tampered"
            elif mutation == "repository-lineage":
                bundle["repository_revision"]["head_sha"] = "f" * 40
            elif mutation == "vision-lineage":
                bundle["accepted_vision_fingerprint"] = "sha256:" + "a" * 64
            else:
                bundle["accepted_product_goal_fingerprint"] = "sha256:" + "b" * 64
            source.source_bundle_json = canonical_json(bundle)
        session.add(source)
        session.commit()

    with Session(engine) as session:
        _assert_code(
            "SPECIFICATION_LINEAGE_INVALID",
            lambda: load_accepted_specification(
                session,
                project_id=project_id,
                spec_version_id=_id(registry.spec_version_id),
                spec_hash=registry.spec_hash,
            ),
        )


def test_current_loader_returns_none_only_for_no_current_row(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Return None only when a Project has no approved registry row."""
    project_id, *_ = _ready_project(engine, tmp_path, name="no-current")
    with Session(engine) as session:
        assert (
            load_current_accepted_specification(session, project_id=project_id) is None
        )


def test_current_loader_rejects_ambiguous_approved_rows(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Reject multiple approved registry rows without an ID tie-break."""
    project_id, registry, *_ = _accept_specification(
        engine, tmp_path, key="ambiguous-current"
    )
    with Session(engine) as session:
        _force_sql(
            session,
            "DROP INDEX uq_spec_registry_current_approved",
            (),
        )
    with Session(engine) as session:
        _force_sql(
            session,
            "INSERT INTO spec_registry (project_id, spec_hash, status, created_at, "
            "source_specification_decision_id, source_specification_candidate_id, "
            "source_specification_candidate_fingerprint, source_vision_artifact_id, "
            "source_vision_fingerprint, source_product_goal_artifact_id, "
            "source_product_goal_fingerprint, supersedes_spec_version_id) "
            "SELECT project_id, ?, status, created_at, "
            "source_specification_decision_id + 100, "
            "source_specification_candidate_id + 100, "
            "source_specification_candidate_fingerprint, "
            "source_vision_artifact_id, source_vision_fingerprint, "
            "source_product_goal_artifact_id, source_product_goal_fingerprint, "
            "spec_version_id "
            "FROM spec_registry WHERE spec_version_id = ?",
            ("sha256:" + "d" * 64, registry.spec_version_id),
        )
    with Session(engine) as session:
        _assert_code(
            "CURRENT_SPECIFICATION_AMBIGUOUS",
            lambda: load_current_accepted_specification(session, project_id=project_id),
        )
