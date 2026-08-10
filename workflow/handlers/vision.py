"""Transactional handlers for the isolated Project Vision lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session, col, func, select

from models.product_definition import (
    ProductGoalArtifactDecision,
    ProductGoalOutcome,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.workflow import WorkflowNodeAttempt
from services.contracts.vision import (
    VisionAssumption,
    VisionBootstrapInput,
    VisionClarificationInput,
    VisionClarifyingQuestion,
    VisionComponentBasis,
    VisionComponents,
    VisionConflict,
    VisionDraftOutput,
    VisionRevisionInput,
)
from services.contracts.vision_evidence import VisionEvidenceBundle
from services.vision_output_validation import (
    VisionDraftValidationError,
    validate_vision_draft,
)
from workflow.contracts import (
    JsonObject,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests.vision import GenerateVisionBootstrap, RecordVisionInterviewTurn

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.requests.vision import (
        BeginVisionRevision,
        DecideVisionReview,
    )


@dataclass(frozen=True)
class _GenerationContext:
    """Validated request context before durable Vision rows are inserted."""

    input_payload: (
        VisionBootstrapInput | VisionClarificationInput | VisionRevisionInput
    )
    snapshot: VisionEvidenceSnapshot | None
    revision: VisionRevisionIntent | None
    revision_intent_id: int | None


def _json_object_tuple(values: tuple[JsonObject, ...]) -> list[JsonObject]:
    return [dict(item) for item in values]


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT, message=message
        ),
    )


def _success(decision: NodeDecision, output: dict[str, object]) -> TransitionResult:
    return TransitionResult(ok=True, applied_node_id=decision.node_id, output=output)


def _has_reference(
    decision: NodeDecision, *, fact_type: str, fact_id: int, fingerprint: str
) -> bool:
    return any(
        reference.fact_type == fact_type
        and reference.fact_id == str(fact_id)
        and reference.fingerprint == fingerprint
        for reference in decision.fact_references
    )


def _active_goal_exists(session: Session, project_id: int) -> bool:
    """Return whether an accepted Product Goal has no durable outcome."""
    accepted_ids = {
        row.product_goal_artifact_id
        for row in session.exec(
            select(ProductGoalArtifactDecision).where(
                col(ProductGoalArtifactDecision.project_id) == project_id,
                col(ProductGoalArtifactDecision.decision) == "accepted",
            )
        ).all()
    }
    outcome_ids = {
        row.product_goal_artifact_id
        for row in session.exec(
            select(ProductGoalOutcome).where(
                col(ProductGoalOutcome.project_id) == project_id
            )
        ).all()
    }
    return bool(accepted_ids - outcome_ids)


def _open_revision_intent(
    session: Session, project_id: int
) -> VisionRevisionIntent | None:
    """Return the one revision intent that has not yet produced a Vision artifact."""
    intents = session.exec(
        select(VisionRevisionIntent)
        .where(col(VisionRevisionIntent.project_id) == project_id)
        .order_by(col(VisionRevisionIntent.vision_revision_intent_id))
    ).all()
    completed_turn_ids = {
        artifact.source_interview_turn_id
        for artifact in session.exec(
            select(VisionArtifact).where(col(VisionArtifact.project_id) == project_id)
        ).all()
    }
    open_intents: list[VisionRevisionIntent] = []
    for intent in intents:
        turns = session.exec(
            select(VisionInterviewTurn).where(
                col(VisionInterviewTurn.project_id) == project_id,
                col(VisionInterviewTurn.revision_intent_id)
                == intent.vision_revision_intent_id,
            )
        ).all()
        if not any(
            turn.vision_interview_turn_id in completed_turn_ids for turn in turns
        ):
            open_intents.append(intent)
    if len(open_intents) != 1:
        return None
    return open_intents[0]


def _load_attempt(
    session: Session,
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
) -> WorkflowNodeAttempt | None:
    return session.exec(
        select(WorkflowNodeAttempt).where(
            col(WorkflowNodeAttempt.project_id) == request.project_id,
            col(WorkflowNodeAttempt.workflow_node_attempt_id) == request.attempt_id,
        )
    ).one_or_none()


def _validated_draft(
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
    input_payload: (
        VisionBootstrapInput | VisionClarificationInput | VisionRevisionInput
    ),
) -> VisionDraftOutput | TransitionResult:
    try:
        draft = VisionDraftOutput(
            schema_version="agileforge.vision-draft.v1",
            components=VisionComponents.model_validate(request.updated_components),
            component_basis=tuple(
                VisionComponentBasis.model_validate(item)
                for item in request.component_basis
            ),
            draft_statement=request.project_vision_statement,
            assumptions=tuple(
                VisionAssumption.model_validate(item) for item in request.assumptions
            ),
            conflicts=tuple(
                VisionConflict.model_validate(item) for item in request.conflicts
            ),
            clarifying_questions=tuple(
                VisionClarifyingQuestion.model_validate(item)
                for item in request.clarifying_questions
            ),
            is_complete=request.is_complete,
        )
        validate_vision_draft(draft, input_payload)
    except (ValueError, VisionDraftValidationError) as error:
        return _conflict(str(error))
    return draft


def _existing_snapshot(
    session: Session,
    request: RecordVisionInterviewTurn,
) -> VisionEvidenceSnapshot | TransitionResult:
    snapshot = session.exec(
        select(VisionEvidenceSnapshot).where(
            col(VisionEvidenceSnapshot.project_id) == request.project_id,
            col(VisionEvidenceSnapshot.vision_evidence_snapshot_id)
            == request.vision_evidence_snapshot_id,
        )
    ).one_or_none()
    if (
        snapshot is None
        or snapshot.evidence_fingerprint != request.evidence_fingerprint
    ):
        return _conflict(
            "Vision clarification does not target the active evidence snapshot."
        )
    return snapshot


def _create_snapshot(
    session: Session,
    request: GenerateVisionBootstrap,
    evaluated_at: datetime,
    *,
    revision_intent_id: int | None,
) -> VisionEvidenceSnapshot | TransitionResult:
    try:
        evidence = VisionEvidenceBundle.model_validate(request.evidence)
    except ValueError as error:
        return _conflict(str(error))
    if evidence.evidence_fingerprint != request.evidence_fingerprint:
        return _conflict("Vision bootstrap evidence fingerprint changed.")
    warnings_json = canonical_json(_json_object_tuple(request.evidence_warnings))
    expected_warnings = canonical_json(
        [warning.model_dump(mode="json") for warning in evidence.warnings]
    )
    if warnings_json != expected_warnings:
        return _conflict("Vision bootstrap evidence warnings changed.")
    supersedes_id = request.supersedes_vision_evidence_snapshot_id
    if supersedes_id is not None:
        superseded = session.exec(
            select(VisionEvidenceSnapshot).where(
                col(VisionEvidenceSnapshot.project_id) == request.project_id,
                col(VisionEvidenceSnapshot.vision_evidence_snapshot_id)
                == supersedes_id,
            )
        ).one_or_none()
        existing_child = session.exec(
            select(VisionEvidenceSnapshot).where(
                col(VisionEvidenceSnapshot.project_id) == request.project_id,
                col(VisionEvidenceSnapshot.supersedes_vision_evidence_snapshot_id)
                == supersedes_id,
            )
        ).one_or_none()
        lineage_turns = session.exec(
            select(VisionInterviewTurn).where(
                col(VisionInterviewTurn.project_id) == request.project_id,
                col(VisionInterviewTurn.vision_evidence_snapshot_id) == supersedes_id,
            )
        ).all()
        if (
            superseded is None
            or existing_child is not None
            or not lineage_turns
            or {turn.revision_intent_id for turn in lineage_turns}
            != {revision_intent_id}
        ):
            return _conflict(
                "Vision replacement does not target the active same-lineage snapshot."
            )
    snapshot = VisionEvidenceSnapshot(
        project_id=request.project_id,
        repository_binding_id=request.repository_binding_id,
        supersedes_vision_evidence_snapshot_id=supersedes_id,
        workflow_node_attempt_id=request.attempt_id,
        evidence_json=canonical_json(evidence.model_dump(mode="json")),
        evidence_fingerprint=evidence.evidence_fingerprint,
        warnings_json=warnings_json,
        created_at=evaluated_at,
    )
    session.add(snapshot)
    session.flush()
    if snapshot.vision_evidence_snapshot_id is None:
        return _conflict("Vision evidence snapshot did not receive a durable identity.")
    return snapshot


def _validate_new_evidence(
    request: GenerateVisionBootstrap,
) -> VisionEvidenceBundle | TransitionResult:
    try:
        evidence = VisionEvidenceBundle.model_validate(request.evidence)
    except ValueError as error:
        return _conflict(str(error))
    if evidence.evidence_fingerprint != request.evidence_fingerprint:
        return _conflict("Vision bootstrap evidence fingerprint changed.")
    warnings_json = canonical_json(_json_object_tuple(request.evidence_warnings))
    expected_warnings = canonical_json(
        [warning.model_dump(mode="json") for warning in evidence.warnings]
    )
    if warnings_json != expected_warnings:
        return _conflict("Vision bootstrap evidence warnings changed.")
    return evidence


def _prior_turn(
    session: Session,
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
    *,
    revision_intent_id: int | None,
    snapshot_id: int,
) -> VisionInterviewTurn | None:
    return session.exec(
        select(VisionInterviewTurn)
        .where(
            col(VisionInterviewTurn.project_id) == request.project_id,
            col(VisionInterviewTurn.vision_evidence_snapshot_id) == snapshot_id,
            col(VisionInterviewTurn.revision_intent_id) == revision_intent_id,
        )
        .order_by(col(VisionInterviewTurn.turn_number).desc())
    ).first()


def _revision_for_generation(
    session: Session,
    request: GenerateVisionBootstrap,
) -> VisionRevisionIntent | TransitionResult | None:
    """Resolve whether this request belongs to the open revision lineage."""
    revision = _open_revision_intent(session, request.project_id)
    if request.operation == "revision" and revision is None:
        return _conflict("Vision revision does not have one open revision intent.")
    if request.operation == "revision":
        return revision
    if revision is not None:
        return _conflict("Initial Vision turn is invalid while a revision is open.")
    return None


def _revision_for_snapshot(
    session: Session,
    *,
    project_id: int,
    snapshot_id: int,
) -> VisionRevisionIntent | TransitionResult | None:
    """Derive exact revision identity from one selected snapshot lineage."""
    turns = session.exec(
        select(VisionInterviewTurn).where(
            col(VisionInterviewTurn.project_id) == project_id,
            col(VisionInterviewTurn.vision_evidence_snapshot_id) == snapshot_id,
        )
    ).all()
    revision_ids = {turn.revision_intent_id for turn in turns}
    if not turns or len(revision_ids) != 1:
        return _conflict("Vision snapshot lineage has ambiguous revision identity.")
    revision_id = revision_ids.pop()
    if revision_id is None:
        return None
    revision = session.exec(
        select(VisionRevisionIntent).where(
            col(VisionRevisionIntent.project_id) == project_id,
            col(VisionRevisionIntent.vision_revision_intent_id) == revision_id,
        )
    ).one_or_none()
    if revision is None:
        return _conflict("Vision snapshot references a missing revision intent.")
    return revision


def _bootstrap_input(evidence: VisionEvidenceBundle) -> VisionBootstrapInput:
    """Build the trusted bootstrap validator input."""
    return VisionBootstrapInput(
        schema_version="agileforge.vision-input.v1",
        operation="bootstrap",
        project_name="Project",
        project_description=None,
        evidence=evidence,
    )


def _revision_input(
    session: Session,
    evidence: VisionEvidenceBundle,
    revision: VisionRevisionIntent,
) -> VisionRevisionInput | TransitionResult:
    """Build the trusted revision validator input from the accepted source."""
    source = session.get(VisionArtifact, revision.source_vision_artifact_id)
    if source is None:
        return _conflict("Vision revision source artifact is missing.")
    return VisionRevisionInput(
        schema_version="agileforge.vision-input.v1",
        operation="revision",
        project_name="Project",
        project_description=None,
        evidence=evidence,
        accepted_components=VisionComponents.model_validate(
            json.loads(source.components_json)
        ),
        accepted_statement=source.statement,
        accepted_vision_fingerprint=source.content_fingerprint,
        revision_reason=revision.reason,
        active_product_goal_status="none",
        prior_review_feedback=None,
    )


def _clarification_input(
    session: Session,
    request: RecordVisionInterviewTurn,
    *,
    snapshot: VisionEvidenceSnapshot,
    revision_intent_id: int | None,
) -> VisionClarificationInput | TransitionResult:
    """Build the trusted clarification validator input from the active draft."""
    evidence = VisionEvidenceBundle.model_validate_json(snapshot.evidence_json)
    prior_seed = _prior_turn(
        session,
        request,
        revision_intent_id=revision_intent_id,
        snapshot_id=snapshot.vision_evidence_snapshot_id or 0,
    )
    if prior_seed is None:
        return _conflict("Vision clarification requires an existing draft turn.")
    return VisionClarificationInput(
        schema_version="agileforge.vision-input.v1",
        operation="clarification",
        project_name="Project",
        project_description=None,
        vision_evidence_snapshot_id=request.vision_evidence_snapshot_id,
        evidence=evidence,
        current_components=VisionComponents.model_validate(
            json.loads(prior_seed.components_json)
        ),
        current_statement=prior_seed.vision_statement,
        current_component_basis=tuple(json.loads(prior_seed.component_basis_json)),
        current_assumptions=tuple(json.loads(prior_seed.assumptions_json)),
        current_conflicts=tuple(json.loads(prior_seed.conflicts_json)),
        current_questions=tuple(json.loads(prior_seed.clarifying_questions_json)),
        human_response=request.user_text,
        addressed_question_ids=request.addressed_question_ids,
    )


def _generation_context(
    session: Session,
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
) -> _GenerationContext | TransitionResult:
    """Validate request lineage and prepare draft validation input."""
    if isinstance(request, GenerateVisionBootstrap):
        revision = _revision_for_generation(session, request)
        if isinstance(revision, TransitionResult):
            return revision
        return _bootstrap_generation_context(session, request, revision)
    return _clarification_generation_context(session, request)


def _bootstrap_generation_context(
    session: Session,
    request: GenerateVisionBootstrap,
    revision: VisionRevisionIntent | None,
) -> _GenerationContext | TransitionResult:
    """Prepare a new bootstrap or revision lineage for persistence."""
    revision_intent_id = (
        None if revision is None else revision.vision_revision_intent_id
    )
    evidence = _validate_new_evidence(request)
    if isinstance(evidence, TransitionResult):
        return evidence
    if request.operation == "bootstrap":
        input_payload = _bootstrap_input(evidence)
    elif revision is None:
        return _conflict("Vision revision does not have one open revision intent.")
    else:
        input_payload = _revision_input(session, evidence, revision)
        if isinstance(input_payload, TransitionResult):
            return input_payload
    return _GenerationContext(input_payload, None, revision, revision_intent_id)


def _clarification_generation_context(
    session: Session,
    request: RecordVisionInterviewTurn,
) -> _GenerationContext | TransitionResult:
    """Prepare one clarification against its existing evidence snapshot."""
    snapshot = _existing_snapshot(session, request)
    if isinstance(snapshot, TransitionResult):
        return snapshot
    snapshot_id = snapshot.vision_evidence_snapshot_id
    if snapshot_id is None:
        return _conflict("Vision clarification snapshot has no durable identity.")
    revision = _revision_for_snapshot(
        session,
        project_id=request.project_id,
        snapshot_id=snapshot_id,
    )
    if isinstance(revision, TransitionResult):
        return revision
    revision_intent_id = (
        None if revision is None else revision.vision_revision_intent_id
    )
    input_payload = _clarification_input(
        session,
        request,
        snapshot=snapshot,
        revision_intent_id=revision_intent_id,
    )
    if isinstance(input_payload, TransitionResult):
        return input_payload
    return _GenerationContext(input_payload, snapshot, revision, revision_intent_id)


def _request_user_text(
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
    revision: VisionRevisionIntent | None,
) -> str | None:
    """Return the durable user text for one generated Vision turn."""
    if isinstance(request, RecordVisionInterviewTurn):
        return request.user_text
    if request.operation == "revision" and revision is not None:
        return revision.reason
    return None


def _materialize_artifact(
    session: Session,
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
    *,
    context: _GenerationContext,
    turn: VisionInterviewTurn,
    evaluated_at: datetime,
) -> dict[str, object] | TransitionResult:
    """Create the reviewable Vision artifact for a complete turn."""
    if context.snapshot is None or context.snapshot.vision_evidence_snapshot_id is None:
        return _conflict("Vision artifact requires a durable evidence snapshot.")
    artifacts = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == request.project_id
        )
    ).all()
    superseded_artifact_ids = {
        artifact.supersedes_vision_artifact_id
        for artifact in artifacts
        if artifact.supersedes_vision_artifact_id is not None
    }
    artifact_leaves = [
        artifact
        for artifact in artifacts
        if artifact.vision_artifact_id not in superseded_artifact_ids
    ]
    if len(artifact_leaves) > 1:
        return _conflict("Vision artifact replacement lineage is ambiguous.")
    parent_id = None if not artifact_leaves else artifact_leaves[0].vision_artifact_id
    version_number = (
        session.exec(
            select(func.max(VisionArtifact.version_number)).where(
                col(VisionArtifact.project_id) == request.project_id
            )
        ).one()
        or 0
    ) + 1
    fingerprint = canonical_hash(
        {
            "components": request.updated_components,
            "statement": request.project_vision_statement.strip(),
        }
    )
    artifact = VisionArtifact(
        project_id=request.project_id,
        version_number=version_number,
        components_json=turn.components_json,
        statement=request.project_vision_statement.strip(),
        content_fingerprint=fingerprint,
        vision_evidence_snapshot_id=context.snapshot.vision_evidence_snapshot_id,
        component_basis_json=turn.component_basis_json,
        assumptions_json=turn.assumptions_json,
        conflicts_json=turn.conflicts_json,
        supersedes_vision_artifact_id=parent_id,
        source_interview_turn_id=turn.vision_interview_turn_id,
        created_by=request.actor,
        created_at=evaluated_at,
    )
    session.add(artifact)
    session.flush()
    if artifact.vision_artifact_id is None:
        return _conflict("Vision artifact did not receive a durable identity.")
    return {
        "vision_artifact_id": artifact.vision_artifact_id,
        "vision_fingerprint": artifact.content_fingerprint,
    }


def _persist_vision_turn(
    session: Session,
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
    *,
    context: _GenerationContext,
    evaluated_at: datetime,
) -> VisionInterviewTurn | TransitionResult:
    """Insert one immutable Vision interview turn."""
    if context.snapshot is None or context.snapshot.vision_evidence_snapshot_id is None:
        return _conflict("Vision turn requires a durable evidence snapshot.")
    snapshot_id = context.snapshot.vision_evidence_snapshot_id
    prior = _prior_turn(
        session,
        request,
        revision_intent_id=context.revision_intent_id,
        snapshot_id=snapshot_id,
    )
    components_json = canonical_json(request.updated_components)
    turn = VisionInterviewTurn(
        project_id=request.project_id,
        operation=request.operation,
        turn_number=1 if prior is None else prior.turn_number + 1,
        revision_intent_id=context.revision_intent_id,
        vision_evidence_snapshot_id=snapshot_id,
        prior_turn_id=None if prior is None else prior.vision_interview_turn_id,
        user_text=_request_user_text(request, context.revision),
        components_json=components_json,
        vision_statement=request.project_vision_statement.strip(),
        is_complete=request.is_complete,
        clarifying_questions_json=canonical_json(
            _json_object_tuple(request.clarifying_questions)
        ),
        component_basis_json=canonical_json(_json_object_tuple(request.component_basis)),
        assumptions_json=canonical_json(_json_object_tuple(request.assumptions)),
        conflicts_json=canonical_json(_json_object_tuple(request.conflicts)),
        output_fingerprint=canonical_hash(
            {
                "components_json": request.updated_components,
                "vision_statement": request.project_vision_statement.strip(),
                "is_complete": request.is_complete,
                "clarifying_questions_json": _json_object_tuple(
                    request.clarifying_questions
                ),
            }
        ),
        workflow_node_attempt_id=request.attempt_id,
        attempt_fingerprint=request.attempt_fingerprint,
        recorded_at=evaluated_at,
    )
    session.add(turn)
    session.flush()
    if turn.vision_interview_turn_id is None:
        return _conflict("Vision interview turn did not receive a durable identity.")
    return turn


def _prepared_generation(
    session: Session,
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
    evaluated_at: datetime,
) -> _GenerationContext | TransitionResult:
    """Validate the live attempt, draft, and evidence snapshot before inserts."""
    attempt = _load_attempt(session, request)
    if attempt is None or attempt.attempt_fingerprint != request.attempt_fingerprint:
        return _conflict(
            "Vision generation does not reference a live same-Project attempt."
        )
    context = _generation_context(session, request)
    if isinstance(context, TransitionResult):
        return context
    draft = _validated_draft(request, context.input_payload)
    if isinstance(draft, TransitionResult):
        return draft
    snapshot = context.snapshot
    if isinstance(request, GenerateVisionBootstrap):
        snapshot = _create_snapshot(
            session,
            request,
            evaluated_at,
            revision_intent_id=context.revision_intent_id,
        )
        if isinstance(snapshot, TransitionResult):
            return snapshot
    if snapshot is None:
        return _conflict("Vision evidence snapshot is missing.")
    return _GenerationContext(
        context.input_payload,
        snapshot,
        context.revision,
        context.revision_intent_id,
    )


def _persist_vision_generation(
    session: Session,
    request: GenerateVisionBootstrap | RecordVisionInterviewTurn,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist one Vision generation and atomically materialize complete artifacts."""
    context = _prepared_generation(session, request, evaluated_at)
    if isinstance(context, TransitionResult):
        return context
    turn = _persist_vision_turn(
        session,
        request,
        context=context,
        evaluated_at=evaluated_at,
    )
    if isinstance(turn, TransitionResult):
        return turn
    output: dict[str, object] = {
        "vision_interview_turn_id": turn.vision_interview_turn_id
    }
    if request.is_complete:
        artifact_output = _materialize_artifact(
            session,
            request,
            context=context,
            turn=turn,
            evaluated_at=evaluated_at,
        )
        if isinstance(artifact_output, TransitionResult):
            return artifact_output
        output.update(artifact_output)
    return _success(decision, output)


def execute_generate_vision_bootstrap(
    session: Session,
    request: GenerateVisionBootstrap,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist one bootstrap or revision generation."""
    return _persist_vision_generation(session, request, decision, evaluated_at)


def execute_record_vision_interview_turn(
    session: Session,
    request: RecordVisionInterviewTurn,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Persist one clarification generation."""
    return _persist_vision_generation(session, request, decision, evaluated_at)


def execute_decide_vision_review(
    session: Session,
    request: DecideVisionReview,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append exactly one decision for the graph-selected pending Vision."""
    artifact = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == request.project_id,
            col(VisionArtifact.vision_artifact_id) == request.vision_artifact_id,
        )
    ).one_or_none()
    if (
        artifact is None
        or artifact.content_fingerprint != request.vision_fingerprint
        or not _has_reference(
            decision,
            fact_type="vision",
            fact_id=request.vision_artifact_id,
            fingerprint=request.vision_fingerprint,
        )
    ):
        return _conflict("Vision review does not target the waiting artifact.")
    if (
        session.exec(
            select(VisionArtifactDecision).where(
                col(VisionArtifactDecision.project_id) == request.project_id,
                col(VisionArtifactDecision.vision_artifact_id)
                == request.vision_artifact_id,
            )
        ).one_or_none()
        is not None
    ):
        return _conflict("Vision artifact already has a terminal review decision.")
    if request.decision == "accepted" and _active_goal_exists(
        session, request.project_id
    ):
        return _conflict(
            "Vision revision acceptance is blocked while a Product Goal is active."
        )
    row = VisionArtifactDecision(
        project_id=request.project_id,
        vision_artifact_id=request.vision_artifact_id,
        artifact_fingerprint=request.vision_fingerprint,
        decision=request.decision,
        rationale=request.rationale.strip(),
        reviewer=request.actor,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(row)
    session.flush()
    if row.vision_artifact_decision_id is None:
        return _conflict("Vision decision did not receive a durable identity.")
    return _success(
        decision,
        {"vision_artifact_decision_id": row.vision_artifact_decision_id},
    )


def execute_begin_vision_revision(
    session: Session,
    request: BeginVisionRevision,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Open a Vision replacement only after every accepted Goal is resolved."""
    artifact = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == request.project_id,
            col(VisionArtifact.vision_artifact_id) == request.source_vision_artifact_id,
        )
    ).one_or_none()
    accepted = session.exec(
        select(VisionArtifactDecision).where(
            col(VisionArtifactDecision.project_id) == request.project_id,
            col(VisionArtifactDecision.vision_artifact_id)
            == request.source_vision_artifact_id,
            col(VisionArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if (
        artifact is None
        or accepted is None
        or artifact.content_fingerprint != request.source_vision_fingerprint
        or _active_goal_exists(session, request.project_id)
        or not _has_reference(
            decision,
            fact_type="vision",
            fact_id=request.source_vision_artifact_id,
            fingerprint=request.source_vision_fingerprint,
        )
    ):
        return _conflict("Vision revision does not target an eligible accepted Vision.")
    if _open_revision_intent(session, request.project_id) is not None:
        return _conflict("Vision revision is already open.")
    row = VisionRevisionIntent(
        project_id=request.project_id,
        source_vision_artifact_id=request.source_vision_artifact_id,
        source_vision_fingerprint=request.source_vision_fingerprint,
        reason=request.reason.strip(),
        initiated_by=request.actor,
        initiated_at=evaluated_at,
    )
    session.add(row)
    session.flush()
    if row.vision_revision_intent_id is None:
        return _conflict("Vision revision intent did not receive a durable identity.")
    return _success(
        decision,
        {"vision_revision_intent_id": row.vision_revision_intent_id},
    )


__all__ = [
    "execute_begin_vision_revision",
    "execute_decide_vision_review",
    "execute_record_vision_interview_turn",
]
