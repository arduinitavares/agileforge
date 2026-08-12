"""Provider-free transactional tests for direct Specification authoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session, col, select

from models.core import Project
from models.product_definition import SpecificationCandidate, SpecificationDecision
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.specification_authoring_input import SpecificationAuthoringInputService
from services.specs.candidate_contract import load_candidate_contract
from tests.workflow.lifecycle_fixtures import (
    _seed_accepted_vision_and_goal,
    seed_accepted_specification,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.clock import FixedClock
from workflow.contracts import (
    GRAPH_VERSION,
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
)
from workflow.definitions.product_discovery import PRODUCT_DISCOVERY_NODES
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.graph import ChildGraphSpec, WorkflowGraph
from workflow.handlers.product_discovery import (
    execute_complete_specification_authoring,
    execute_decide_specification,
)
from workflow.requests import (
    CompleteSpecificationAuthoring,
    DecideSpecification,
    StartNodeAttempt,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
EXPECTED_REVISION_CANDIDATES = 2


class _Registry:
    """Expose the sole recipe needed by these domain tests."""

    def require(self, node_id: str) -> object:
        if node_id != "specification.author":
            raise LookupError(node_id)
        return object()


def _domain(engine: Engine, *, at: datetime = NOW) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(
                child_graph_id="specification",
                nodes=PRODUCT_DISCOVERY_NODES,
            ),
        ),
        clock=FixedClock(now_value=at),
        adk_recipe_registry=_Registry(),
    )


def _seed_accepted_goal(engine: Engine, *, name: str) -> tuple[int, int, str, int, str]:
    with Session(engine) as session:
        project = Project(name=name)
        session.add(project)
        session.flush()
        assert project.project_id is not None
        vision, goal = _seed_accepted_vision_and_goal(
            session,
            project_id=project.project_id,
            recorded_at=NOW.replace(hour=10),
        )
        session.commit()
        assert vision.vision_artifact_id is not None
        assert goal.product_goal_artifact_id is not None
        return (
            project.project_id,
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )


def _payload(
    *,
    artifact_id: str = "SPEC.direct-authoring",
    item_id: str = "REQ.persist-candidate",
    source_id: str | None = None,
) -> SpecificationPayload:
    source_notes: list[dict[str, str]] = []
    if source_id is not None:
        source_notes.append(
            {
                "source_id": source_id,
                "kind": "interview",
                "text": "Accepted product-definition evidence.",
            }
        )
    return SpecificationPayload.model_validate(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": artifact_id,
            "title": "Direct authoring",
            "summary": "Persist one exact typed candidate.",
            "problem_statement": "Discovery must not be a persisted gate.",
            "items": [
                {
                    "id": item_id,
                    "type": "REQ",
                    "title": "Persist candidate",
                    "statement": "The host persists one immutable candidate.",
                    "level": "MUST",
                    "verification": "integration-test",
                    "acceptance": ["The exact candidate can be reviewed."],
                    "source_notes": source_notes,
                }
            ],
        }
    )


def _author(
    engine: Engine,
    domain: WorkflowDomain,
    *,
    project_id: int,
    payload: SpecificationPayload,
    key: str,
) -> TransitionResult:
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "specification.author"
    )
    start = domain.transition(
        StartNodeAttempt(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=f"{key}-start",
            actor="worker",
            correlation_id=f"{key}-correlation",
            target_node_id="specification.author",
            target_instance_key=decision.instance_key,
            normalized_input=SpecificationAuthoringInputService(engine=engine).build(
                project_id=project_id,
                decision=decision,
            ),
            model_id="fake/specification-author",
            execution_settings={"temperature": 0},
            lease_seconds=60,
        )
    )
    assert start.ok
    attempt_id = start.output["attempt_id"]
    attempt_fingerprint = start.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    return domain.transition(
        CompleteSpecificationAuthoring(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=f"{key}-complete",
            actor="worker",
            correlation_id=f"{key}-correlation",
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            payload=payload,
        )
    )


def _accept_request(
    domain: WorkflowDomain,
    *,
    project_id: int,
    key: str,
) -> DecideSpecification:
    position = domain.position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "specification.review"
    )
    candidate = next(
        item
        for item in review.fact_references
        if item.fact_type == "specification_candidate"
    )
    return DecideSpecification(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=review.decision_fingerprint,
        idempotency_key=key,
        actor="operator",
        specification_candidate_id=int(candidate.fact_id),
        candidate_fingerprint=candidate.fingerprint,
        decision="accepted",
    )


def test_completion_contract_rejects_provider_owned_envelope_metadata() -> None:
    """The completion boundary accepts semantics, not host lifecycle metadata."""
    with pytest.raises(ValidationError):
        CompleteSpecificationAuthoring.model_validate(
            {
                "kind": "complete_specification_authoring",
                "project_id": 1,
                "graph_version": GRAPH_VERSION,
                "fact_fingerprint": "facts",
                "decision_fingerprint": "decision",
                "idempotency_key": "complete",
                "actor": "worker",
                "attempt_id": 1,
                "attempt_fingerprint": "attempt",
                "payload": _payload().model_dump(mode="json"),
                "producer_version": "forged",
            }
        )


def test_accepted_goal_authors_and_accepts_exact_candidate_without_rewrite(
    engine: Engine,
) -> None:
    """Attempt continuation binds host metadata and acceptance preserves bytes."""
    project_id, vision_id, _vision_fp, goal_id, _goal_fp = _seed_accepted_goal(
        engine,
        name="Direct specification",
    )
    domain = _domain(engine)
    source_id = f"SRC.vision.{vision_id}"
    result = _author(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(source_id=source_id),
        key="initial",
    )

    assert result.ok
    with Session(engine) as session:
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        before = candidate.canonical_envelope_json
        payload, envelope = load_candidate_contract(
            before,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
        assert payload == _payload(source_id=source_id)
        assert envelope.accepted_vision_id == vision_id
        assert envelope.accepted_product_goal_id == goal_id
        assert envelope.workflow_node_attempt_id == candidate.workflow_node_attempt_id
        assert envelope.model_id == "fake/specification-author"

    review_domain = _domain(engine, at=NOW + timedelta(seconds=1))
    accept_request = _accept_request(
        review_domain,
        project_id=project_id,
        key="accept-initial",
    )
    accepted = review_domain.transition(accept_request)
    assert accepted.ok
    replay = review_domain.transition(accept_request)
    assert replay.ok
    assert replay.replayed
    with Session(engine) as session:
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        registry = session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).one()
        assert candidate.canonical_envelope_json == before
        assert registry.spec_hash == candidate.payload_fingerprint
        assert registry.source_specification_candidate_fingerprint == (
            candidate.candidate_fingerprint
        )
        review = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).one()
        session.delete(registry)
        session.commit()
        session.delete(review)
        session.commit()
        session.delete(candidate)
        session.commit()


def test_payload_source_note_must_exist_in_host_manifest(engine: Engine) -> None:
    """A model cannot cite a source absent from the persisted attempt input."""
    project_id, *_lineage = _seed_accepted_goal(
        engine,
        name="Unknown source note",
    )
    result = _author(
        engine,
        _domain(engine),
        project_id=project_id,
        payload=_payload(source_id="SRC.external.missing"),
        key="unknown-source",
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "STALE_SPECIFICATION_INPUT"
    with Session(engine) as session:
        assert not session.exec(select(SpecificationCandidate)).all()
        outcome = session.exec(
            select(WorkflowNodeAttemptOutcome).where(
                col(WorkflowNodeAttemptOutcome.project_id) == project_id,
                col(WorkflowNodeAttemptOutcome.status) == "failure",
            )
        ).one()
        assert outcome.failure_code == "STALE_SPECIFICATION_INPUT"


def test_amendment_pins_base_and_requires_every_removal_justification(
    engine: Engine,
) -> None:
    """A full-result amendment cannot silently remove a stable item."""
    with Session(engine) as session:
        project = Project(name="Specification amendment")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"base":"accepted"}',
            recorded_at=NOW.replace(hour=9),
        )
    decision = NodeDecision(
        node_id="specification.author",
        child_graph_id="product_discovery",
        request_kind="author_specification",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        reason_code="SPECIFICATION_AMENDMENT_REQUIRED",
        fact_references=(
            FactReference(
                fact_type="vision",
                fact_id=str(lineage.vision_artifact_id),
                fingerprint=lineage.vision_fingerprint,
            ),
            FactReference(
                fact_type="product_goal",
                fact_id=str(lineage.product_goal_artifact_id),
                fingerprint=lineage.product_goal_fingerprint,
            ),
            FactReference(
                fact_type="specification",
                fact_id=str(lineage.spec.spec_version_id),
                fingerprint=lineage.spec.spec_hash,
            ),
        ),
        decision_fingerprint=canonical_hash({"decision": "amend"}),
    )
    with Session(engine) as session:
        base_candidate = session.get(
            SpecificationCandidate,
            lineage.specification_candidate_id,
        )
        assert base_candidate is not None
        base_payload, _base_envelope = load_candidate_contract(
            base_candidate.canonical_envelope_json,
            expected_candidate_fingerprint=base_candidate.candidate_fingerprint,
        )
        normalized_input = {
            "schema_version": "agileforge.spec-authoring-input.v2",
            "project_id": project_id,
            "project_name": "Specification amendment",
            "operation": "amendment",
            "accepted_vision": {
                "artifact_id": lineage.vision_artifact_id,
                "fingerprint": lineage.vision_fingerprint,
                "statement": "Accepted fixture Vision.",
                "components": {"purpose": "exercise amendments"},
            },
            "accepted_product_goal": {
                "artifact_id": lineage.product_goal_artifact_id,
                "fingerprint": lineage.product_goal_fingerprint,
                "statement": "Accepted fixture Product Goal.",
            },
            "source_manifest": [
                {
                    "source_id": f"SRC.vision.{lineage.vision_artifact_id}",
                    "kind": "vision",
                    "fingerprint": lineage.vision_fingerprint,
                },
                {
                    "source_id": (
                        "SRC.product-goal."
                        f"{lineage.product_goal_artifact_id}"
                    ),
                    "kind": "product_goal",
                    "fingerprint": lineage.product_goal_fingerprint,
                },
            ],
            "source_context": [
                {
                    "source_id": f"SRC.vision.{lineage.vision_artifact_id}",
                    "kind": "vision",
                    "fingerprint": lineage.vision_fingerprint,
                    "content": {"statement": "Accepted fixture Vision."},
                },
                {
                    "source_id": (
                        "SRC.product-goal."
                        f"{lineage.product_goal_artifact_id}"
                    ),
                    "kind": "product_goal",
                    "fingerprint": lineage.product_goal_fingerprint,
                    "content": {"statement": "Accepted fixture Product Goal."},
                },
            ],
            "base_specification": {
                "spec_version_id": lineage.spec.spec_version_id,
                "payload_fingerprint": lineage.spec.spec_hash,
                "payload": base_payload.model_dump(mode="json"),
            },
            "prior_candidate": None,
        }
        attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id="specification.author",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint=canonical_hash({"facts": "amendment"}),
            business_fact_fingerprint=canonical_hash({"business": "amendment"}),
            decision_fingerprint=decision.decision_fingerprint,
            normalized_input_json=canonical_json(normalized_input),
            input_fingerprint=canonical_hash(normalized_input),
            model_id="fake/specification-author",
            execution_settings_json=canonical_json({"temperature": 0}),
            idempotency_key="amendment-attempt",
            actor="worker",
            correlation_id="amendment-correlation",
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=canonical_hash({"attempt": "amendment"}),
        )
        session.add(attempt)
        session.commit()
        assert attempt.workflow_node_attempt_id is not None
        amended_payload = base_payload.model_copy(
            update={
                "summary": "Amend the accepted base with a normative requirement.",
                "items": _payload().items,
            }
        )
        request = CompleteSpecificationAuthoring(
            project_id=project_id,
            graph_version=GRAPH_VERSION,
            fact_fingerprint=attempt.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key="amendment-without-justification",
            actor="worker",
            correlation_id="amendment-correlation",
            attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            payload=amended_payload,
        )
        missing = execute_complete_specification_authoring(
            session,
            request,
            decision,
            NOW + timedelta(seconds=1),
        )
        assert not missing.ok
        assert missing.error is not None
        assert missing.error.code == "SPECIFICATION_AMENDMENT_MISMATCH"
        old_item_id = base_payload.items[0].id
        accepted = execute_complete_specification_authoring(
            session,
            request.model_copy(
                update={
                    "idempotency_key": "amendment-with-justification",
                    "removal_justifications": {
                        old_item_id: "The normative replacement is more precise."
                    },
                }
            ),
            decision,
            NOW + timedelta(seconds=2),
        )
        assert accepted.ok
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.workflow_node_attempt_id)
                == attempt.workflow_node_attempt_id
            )
        ).one()
        _payload_result, envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
        assert envelope.base_specification_id == lineage.spec.spec_version_id
        assert envelope.base_payload_fingerprint == lineage.spec.spec_hash
        assert envelope.amendment_diff is not None
        assert envelope.amendment_diff.removed_item_ids == (old_item_id,)

        session.delete(candidate)
        session.commit()
        base_registry = session.get(SpecRegistry, lineage.spec.spec_version_id)
        assert base_registry is not None
        session.delete(base_registry)
        session.commit()
        base_decision = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.specification_candidate_id)
                == lineage.specification_candidate_id
            )
        ).one()
        session.delete(base_decision)
        session.commit()
        base_candidate = session.get(
            SpecificationCandidate,
            lineage.specification_candidate_id,
        )
        assert base_candidate is not None
        session.delete(base_candidate)
        session.commit()


def test_rejected_revision_supersedes_exact_candidate(engine: Engine) -> None:
    """Feedback revision preserves initial mode and pins exact candidate lineage."""
    project_id, *_lineage = _seed_accepted_goal(
        engine,
        name="Specification revision",
    )
    domain = _domain(engine)
    first = _author(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key="first",
    )
    assert first.ok
    review_domain = _domain(engine, at=NOW + timedelta(seconds=1))
    position = review_domain.position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "specification.review"
    )
    reference = review.fact_references[0]
    rejected = review_domain.transition(
        DecideSpecification(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key="reject-first",
            actor="operator",
            specification_candidate_id=int(reference.fact_id),
            candidate_fingerprint=reference.fingerprint,
            decision="rejected",
            rationale="Clarify the normative title.",
        )
    )
    assert rejected.ok
    second = _author(
        engine,
        _domain(engine, at=NOW + timedelta(seconds=2)),
        project_id=project_id,
        payload=_payload(item_id="REQ.persist-revised-candidate"),
        key="second",
    )
    assert second.ok

    with Session(engine) as session:
        candidates = session.exec(
            select(SpecificationCandidate)
            .where(col(SpecificationCandidate.project_id) == project_id)
            .order_by(col(SpecificationCandidate.specification_candidate_id))
        ).all()
        assert len(candidates) == EXPECTED_REVISION_CANDIDATES
        assert candidates[1].candidate_kind == "initial"
        assert candidates[1].supersedes_specification_candidate_id == (
            candidates[0].specification_candidate_id
        )
        assert candidates[1].supersedes_candidate_fingerprint == (
            candidates[0].candidate_fingerprint
        )


def test_acceptance_rejects_tampered_candidate_bytes(engine: Engine) -> None:
    """The review handler fails closed before registering a changed candidate."""
    project_id, *_lineage = _seed_accepted_goal(
        engine,
        name="Tampered candidate",
    )
    domain = _domain(engine)
    authored = _author(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key="tamper",
    )
    assert authored.ok
    position = _domain(engine, at=NOW + timedelta(seconds=1)).position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "specification.review"
    )
    reference = review.fact_references[0]
    request = DecideSpecification(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=review.decision_fingerprint,
        idempotency_key="accept-tampered",
        actor="operator",
        specification_candidate_id=int(reference.fact_id),
        candidate_fingerprint=reference.fingerprint,
        decision="accepted",
    )
    with Session(engine) as session:
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        candidate.canonical_envelope_json = "{}"
        session.add(candidate)
        session.commit()
    with Session(engine) as session:
        accepted = execute_decide_specification(
            session,
            request,
            review,
            NOW + timedelta(seconds=1),
        )
    assert not accepted.ok
    assert accepted.error is not None
    assert accepted.error.code == "SPECIFICATION_CANDIDATE_CONFLICT"
    with Session(engine) as session:
        assert not session.exec(select(SpecRegistry)).all()
        assert not session.exec(select(SpecificationDecision)).all()
