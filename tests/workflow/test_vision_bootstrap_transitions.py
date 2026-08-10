"""Atomic Vision bootstrap transition boundaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.product_definition import (
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from services.contracts.vision import VisionAgentInput, VisionRevisionInput
from services.vision_input import VisionInputService
from tests.workflow.test_vision_interview_transitions import (
    COMPONENTS,
    _basis,
    _decision,
    _domain,
    _evidence,
    _record,
    _RecordRequest,
    _review_vision,
    _start,
    _VisionReview,
)
from workflow.contracts import JsonObject, RecommendationKind, WorkflowErrorCode
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    BeginVisionRevision,
    FailNodeAttempt,
    GenerateVisionBootstrap,
    RecordVisionInterviewTurn,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.domain import WorkflowDomain

REVISION_TURN_COUNT = 2


@dataclass(frozen=True)
class _OpenRevision:
    """Inputs needed to open one revision intent in transition tests."""

    artifact_id: int
    fingerprint: str
    idempotency_key: str
    reason: str


def _project(engine: Engine, name: str = "Vision bootstrap transitions") -> int:
    with Session(engine) as session:
        project = Project(name=name)
        session.add(project)
        session.commit()
        assert project.project_id is not None
        return project.project_id


def _open_revision(
    domain: WorkflowDomain,
    project_id: int,
    request: _OpenRevision,
) -> int:
    """Open one revision intent and return its durable identity."""
    position = domain.position(project_id)
    decision = _decision(domain, project_id, "vision.revision.start")
    result = domain.transition(
        BeginVisionRevision(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=request.idempotency_key,
            actor="operator@example.com",
            source_vision_artifact_id=request.artifact_id,
            source_vision_fingerprint=request.fingerprint,
            reason=request.reason,
        )
    )
    assert result.ok
    revision_intent_id = result.output["vision_revision_intent_id"]
    assert isinstance(revision_intent_id, int)
    return revision_intent_id


def _try_build_revision_bootstrap(
    engine: Engine,
    domain: WorkflowDomain,
    project_id: int,
) -> tuple[JsonObject | None, ValueError | None]:
    """Build host input while preserving cleanup after an assertion failure."""
    try:
        decision = _decision(domain, project_id, "vision.bootstrap")
        payload = VisionInputService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).build_bootstrap(project_id, decision)
    except ValueError as error:
        return None, error
    return payload, None


def test_bootstrap_persists_snapshot_and_turn_atomically(engine: Engine) -> None:
    """Incomplete bootstrap stores one snapshot-backed turn and no artifact."""
    project_id = _project(engine)
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "bootstrap-atomic")

    result = _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=False, key="bootstrap-atomic-record"),
    )

    assert result.ok
    with Session(engine) as session:
        snapshots = session.exec(select(VisionEvidenceSnapshot)).all()
        turns = session.exec(select(VisionInterviewTurn)).all()
        assert len(snapshots) == 1
        assert len(turns) == 1
        assert turns[0].operation == "bootstrap"
        assert turns[0].vision_evidence_snapshot_id == (
            snapshots[0].vision_evidence_snapshot_id
        )
        assert session.exec(select(VisionArtifact)).all() == []


def test_complete_bootstrap_persists_artifact_snapshot(engine: Engine) -> None:
    """Complete bootstrap materializes the review artifact in the same lineage."""
    project_id = _project(engine, "Vision bootstrap complete")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "bootstrap-complete")

    result = _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="bootstrap-complete-record"),
    )

    assert result.ok
    with Session(engine) as session:
        turn = session.exec(select(VisionInterviewTurn)).one()
        artifact = session.exec(select(VisionArtifact)).one()
        assert artifact.source_interview_turn_id == turn.vision_interview_turn_id
        assert artifact.vision_evidence_snapshot_id == turn.vision_evidence_snapshot_id
        assert artifact.component_basis_json == turn.component_basis_json
        assert artifact.assumptions_json == turn.assumptions_json
        assert artifact.conflicts_json == turn.conflicts_json
        assert turn.output_fingerprint == canonical_hash(
            {
                "components_json": json.loads(turn.components_json),
                "vision_statement": turn.vision_statement,
                "is_complete": turn.is_complete,
                "clarifying_questions_json": json.loads(
                    turn.clarifying_questions_json
                ),
                "component_basis_json": json.loads(turn.component_basis_json),
                "assumptions_json": json.loads(turn.assumptions_json),
                "conflicts_json": json.loads(turn.conflicts_json),
            }
        )


def test_clarification_reuses_bootstrap_snapshot(engine: Engine) -> None:
    """Clarification must not silently recollect or replace draft evidence."""
    project_id = _project(engine, "Vision clarification snapshot")
    domain = _domain(engine)
    first_start, first_attempt = _start(domain, project_id, "snapshot-first")
    assert _record(
        engine,
        domain,
        first_start,
        first_attempt,
        request=_RecordRequest(complete=False, key="snapshot-first-record"),
    ).ok
    second_start, second_attempt = _start(
        domain,
        project_id,
        "snapshot-second",
        node_id="vision.interview",
        operation="clarification",
    )

    result = _record(
        engine,
        domain,
        second_start,
        second_attempt,
        request=_RecordRequest(complete=True, key="snapshot-second-record"),
    )

    assert result.ok
    with Session(engine) as session:
        snapshots = session.exec(select(VisionEvidenceSnapshot)).all()
        turns = session.exec(
            select(VisionInterviewTurn).order_by(col(VisionInterviewTurn.turn_number))
        ).all()
        assert len(snapshots) == 1
        assert [turn.vision_evidence_snapshot_id for turn in turns] == [
            snapshots[0].vision_evidence_snapshot_id,
            snapshots[0].vision_evidence_snapshot_id,
        ]


def test_revision_bootstrap_supersedes_accepted_vision(engine: Engine) -> None:
    """A complete revision artifact points at the accepted Vision it replaces."""
    project_id = _project(engine, "Vision revision supersession")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "revision-seed")
    initial = _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="revision-seed-record"),
    )
    artifact_id = initial.output["vision_artifact_id"]
    fingerprint = initial.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    assert _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=artifact_id,
            fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept before revision.",
            idempotency_key="revision-seed-accept",
        ),
    ).ok
    position = domain.position(project_id)
    revision = _decision(domain, project_id, "vision.revision.start")
    assert domain.transition(
        BeginVisionRevision(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=revision.decision_fingerprint,
            idempotency_key="revision-open",
            actor="operator@example.com",
            source_vision_artifact_id=artifact_id,
            source_vision_fingerprint=fingerprint,
            reason="Direction changed.",
        )
    ).ok
    revision_start, revision_attempt = _start(
        domain,
        project_id,
        "revision-bootstrap",
        node_id="vision.bootstrap",
        operation="revision",
    )

    revised = _record(
        engine,
        domain,
        revision_start,
        revision_attempt,
        request=_RecordRequest(
            complete=True,
            key="revision-bootstrap-record",
            operation="revision",
            components=COMPONENTS | {"differentiator": "Revision lineage"},
            statement="A revised trusted workflow tool.",
        ),
    )

    assert revised.ok
    with Session(engine) as session:
        artifacts = session.exec(
            select(VisionArtifact).order_by(col(VisionArtifact.version_number))
        ).all()
        assert [item.supersedes_vision_artifact_id for item in artifacts] == [
            None,
            artifact_id,
        ]
        for turn in session.exec(
            select(VisionInterviewTurn).where(
                VisionInterviewTurn.operation == "revision"
            )
        ).all():
            turn.revision_intent_id = None
            session.add(turn)
        for intent in session.exec(select(VisionRevisionIntent)).all():
            session.delete(intent)
        session.commit()


def test_revision_clarification_reuses_revision_snapshot(engine: Engine) -> None:
    """Incomplete revision bootstrap can continue with revision clarification."""
    project_id = _project(engine, "Vision revision clarification")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "revision-clarification-seed")
    initial = _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="revision-clarification-record"),
    )
    artifact_id = initial.output["vision_artifact_id"]
    fingerprint = initial.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    assert _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=artifact_id,
            fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept before revision.",
            idempotency_key="revision-clarification-accept",
        ),
    ).ok
    revision_intent_id = _open_revision(
        domain,
        project_id,
        _OpenRevision(
            artifact_id=artifact_id,
            fingerprint=fingerprint,
            idempotency_key="revision-clarification-open",
            reason="Direction changed.",
        ),
    )
    revision_start, revision_attempt = _start(
        domain,
        project_id,
        "revision-clarification-bootstrap",
        node_id="vision.bootstrap",
        operation="revision",
    )
    assert _record(
        engine,
        domain,
        revision_start,
        revision_attempt,
        request=_RecordRequest(
            complete=False,
            key="revision-clarification-bootstrap-record",
            operation="revision",
            components=COMPONENTS | {"differentiator": "Revision lineage"},
        ),
    ).ok
    clarification_start, clarification_attempt = _start(
        domain,
        project_id,
        "revision-clarification-followup",
        node_id="vision.interview",
        operation="clarification",
    )

    clarified = _record(
        engine,
        domain,
        clarification_start,
        clarification_attempt,
        request=_RecordRequest(
            complete=True,
            key="revision-clarification-followup-record",
            components=COMPONENTS | {"differentiator": "Clarified revision lineage"},
        ),
    )
    assert clarified.ok
    revised_artifact_id = clarified.output["vision_artifact_id"]
    revised_fingerprint = clarified.output["vision_fingerprint"]
    assert isinstance(revised_artifact_id, int)
    assert isinstance(revised_fingerprint, str)
    assert _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=revised_artifact_id,
            fingerprint=revised_fingerprint,
            decision="accepted",
            rationale="Accept clarified revision.",
            idempotency_key="revision-clarification-revised-accept",
        ),
    ).ok
    _open_revision(
        domain,
        project_id,
        _OpenRevision(
            artifact_id=revised_artifact_id,
            fingerprint=revised_fingerprint,
            idempotency_key="revision-clarification-second-open",
            reason="Revise the clarified direction again.",
        ),
    )
    second_payload, selection_error = _try_build_revision_bootstrap(
        engine,
        domain,
        project_id,
    )

    revision_operations: list[str] = []
    revision_snapshot_ids: set[int | None] = set()
    clarification_prior_turn_id: int | None = None
    revision_first_turn_id: int | None = None
    with Session(engine) as session:
        revision_turns = session.exec(
            select(VisionInterviewTurn)
            .where(VisionInterviewTurn.revision_intent_id == revision_intent_id)
            .order_by(col(VisionInterviewTurn.turn_number))
        ).all()
        revision_operations = [turn.operation for turn in revision_turns]
        revision_snapshot_ids = {
            turn.vision_evidence_snapshot_id for turn in revision_turns
        }
        clarification_prior_turn_id = revision_turns[1].prior_turn_id
        revision_first_turn_id = revision_turns[0].vision_interview_turn_id
        for turn in session.exec(select(VisionInterviewTurn)).all():
            if turn.revision_intent_id is not None:
                turn.revision_intent_id = None
                session.add(turn)
        for intent in session.exec(select(VisionRevisionIntent)).all():
            session.delete(intent)
        session.commit()

    assert selection_error is None
    assert second_payload is not None
    second_request = VisionAgentInput.model_validate(second_payload).request
    assert isinstance(second_request, VisionRevisionInput)
    assert second_request.revision_reason == "Revise the clarified direction again."
    assert len(revision_operations) == REVISION_TURN_COUNT
    assert revision_operations == [
        "revision",
        "clarification",
    ]
    assert len(revision_snapshot_ids) == 1
    assert clarification_prior_turn_id == revision_first_turn_id


def test_cross_project_attempt_rejected(engine: Engine) -> None:
    """A bootstrap continuation cannot borrow another Project's attempt."""
    project_id = _project(engine, "Vision attempt owner")
    other_project_id = _project(engine, "Vision attempt borrower")
    domain = _domain(engine)
    _start_request, attempt = _start(domain, project_id, "cross-project-attempt")
    attempt_id = attempt.output["attempt_id"]
    attempt_fingerprint = attempt.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    other_position = domain.position(other_project_id)
    other_decision = _decision(domain, other_project_id, "vision.bootstrap")

    evidence = _evidence()
    result = domain.transition(
        GenerateVisionBootstrap(
            project_id=other_project_id,
            graph_version=other_position.graph_version,
            fact_fingerprint=other_position.fact_fingerprint,
            decision_fingerprint=other_decision.decision_fingerprint,
            idempotency_key="cross-project-attempt-record",
            actor="operator@example.com",
            operation="bootstrap",
            evidence=evidence,
            evidence_fingerprint=str(evidence["evidence_fingerprint"]),
            evidence_warnings=(),
            repository_binding_id=None,
            updated_components=COMPONENTS,
            project_vision_statement="A trusted workflow tool.",
            is_complete=True,
            clarifying_questions=(),
            component_basis=_basis(COMPONENTS),
            assumptions=(),
            conflicts=(),
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE


def test_cross_project_snapshot_rejected(engine: Engine) -> None:
    """Clarification cannot bind to a snapshot from another Project."""
    project_id = _project(engine, "Vision snapshot owner")
    other_project_id = _project(engine, "Vision snapshot borrower")
    domain = _domain(engine)
    first_start, first_attempt = _start(domain, project_id, "owner-first")
    assert _record(
        engine,
        domain,
        first_start,
        first_attempt,
        request=_RecordRequest(complete=False, key="owner-first-record"),
    ).ok
    other_start, other_attempt = _start(domain, other_project_id, "borrower-first")
    assert _record(
        engine,
        domain,
        other_start,
        other_attempt,
        request=_RecordRequest(complete=False, key="borrower-first-record"),
    ).ok
    with Session(engine) as session:
        owner_snapshot_id = session.exec(
            select(VisionInterviewTurn.vision_evidence_snapshot_id).where(
                VisionInterviewTurn.project_id == project_id
            )
        ).one()
        owner_snapshot = session.get(VisionEvidenceSnapshot, owner_snapshot_id)
        assert owner_snapshot is not None
    borrower_start, borrower_attempt = _start(
        domain,
        other_project_id,
        "borrower-clarification",
        node_id="vision.interview",
        operation="clarification",
    )

    attempt_id = borrower_attempt.output["attempt_id"]
    attempt_fingerprint = borrower_attempt.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    result = domain.transition(
        RecordVisionInterviewTurn(
            project_id=other_project_id,
            graph_version=borrower_start.graph_version,
            fact_fingerprint=borrower_start.fact_fingerprint,
            decision_fingerprint=borrower_start.decision_fingerprint,
            instance_key=borrower_start.target_instance_key,
            idempotency_key="borrower-clarification-record",
            actor="operator@example.com",
            vision_evidence_snapshot_id=owner_snapshot_id,
            evidence_fingerprint=owner_snapshot.evidence_fingerprint,
            user_text="Build a tool.",
            addressed_question_ids=("question:target-user",),
            updated_components=COMPONENTS,
            project_vision_statement="A trusted workflow tool.",
            is_complete=True,
            clarifying_questions=(),
            component_basis=_basis(COMPONENTS),
            assumptions=(),
            conflicts=(),
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


def test_invalid_basis_references_fail_without_business_facts(engine: Engine) -> None:
    """Semantic output validation must reject untrusted evidence references."""
    project_id = _project(engine, "Vision invalid basis")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "invalid-basis")
    attempt_id = attempt.output["attempt_id"]
    attempt_fingerprint = attempt.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    evidence = _evidence()

    result = domain.transition(
        GenerateVisionBootstrap(
            project_id=project_id,
            graph_version=start.graph_version,
            fact_fingerprint=start.fact_fingerprint,
            decision_fingerprint=start.decision_fingerprint,
            idempotency_key="invalid-basis-record",
            actor="operator@example.com",
            operation="bootstrap",
            evidence=evidence,
            evidence_fingerprint=str(evidence["evidence_fingerprint"]),
            evidence_warnings=(),
            repository_binding_id=None,
            updated_components=COMPONENTS,
            project_vision_statement="A trusted workflow tool.",
            is_complete=True,
            clarifying_questions=(),
            component_basis=(
                {
                    "component": "project_name",
                    "source_kinds": ["evidence"],
                    "evidence_ids": ["missing:evidence"],
                    "assumption_ids": [],
                },
                *_basis(
                    {
                        key: value
                        for key, value in COMPONENTS.items()
                        if key != "project_name"
                    }
                ),
            ),
            assumptions=(),
            conflicts=(),
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert session.exec(select(VisionEvidenceSnapshot)).all() == []
        assert session.exec(select(VisionInterviewTurn)).all() == []
        assert session.exec(select(VisionArtifact)).all() == []


def test_vision_evidence_stale_failure_recovers_to_bootstrap(engine: Engine) -> None:
    """A durable stale-evidence failure restarts from explicit bootstrap."""
    project_id = _project(engine, "Vision stale recovery")
    domain = _domain(engine)
    first_start, first_attempt = _start(domain, project_id, "stale-first")
    assert _record(
        engine,
        domain,
        first_start,
        first_attempt,
        request=_RecordRequest(complete=False, key="stale-first-record"),
    ).ok
    _interview_start, interview_attempt = _start(
        domain,
        project_id,
        "stale-clarification",
        node_id="vision.interview",
        operation="clarification",
    )
    attempt_id = interview_attempt.output["attempt_id"]
    attempt_fingerprint = interview_attempt.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)

    failed = domain.transition(
        FailNodeAttempt(
            project_id=project_id,
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            failure_code=WorkflowErrorCode.VISION_EVIDENCE_STALE.value,
            failure_message="Vision evidence changed before provider invocation.",
            idempotency_key="stale-clarification-failed",
            actor="operator@example.com",
        )
    )

    assert failed.ok
    position = domain.position(project_id)
    bootstrap = _decision(domain, project_id, "vision.bootstrap")
    assert bootstrap.node_id in position.available_nodes
    assert bootstrap.recommendation_kind is RecommendationKind.RECOVERY
    assert "vision.interview" not in position.available_nodes
    with Session(engine) as session:
        assert len(session.exec(select(VisionArtifactDecision)).all()) == 0
