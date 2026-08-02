"""Transactional handlers for optional Project scope extension."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from sqlmodel import Session, col, select

from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    DiscoveryRunAbandonment,
    PrdDecision,
    PrdVersion,
    ScopeExtensionReconciliation,
    ScopeExtensionRegistration,
    SpecDraft,
    SpecDraftDecision,
)
from services.specs.lifecycle_service import (
    ApprovedCanonicalSpec,
    register_approved_spec_from_canonical_json,
)
from services.specs.profile_content import normalize_spec_content_for_registry
from workflow.contracts import (
    FactReference,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    canonical_stored_json_hash,
)
from workflow.requests import (
    AbandonScopeExtension,
    DecideAmendmentSpecDraft,
    DecideExtensionPrd,
    ReconcileScopeExtension,
    RecordAmendmentSpecDraft,
    RecordExtensionChallenge,
    RecordExtensionPrd,
    RegisterScopeExtension,
    StartScopeExtension,
)

if TYPE_CHECKING:
    from datetime import datetime

    type ScopeExtensionRequest = (
        StartScopeExtension
        | RecordExtensionChallenge
        | RecordExtensionPrd
        | DecideExtensionPrd
        | RecordAmendmentSpecDraft
        | DecideAmendmentSpecDraft
        | RegisterScopeExtension
        | ReconcileScopeExtension
        | AbandonScopeExtension
    )


@dataclass(frozen=True)
class _RegistrationInputs:
    """Validated facts required to register one accepted amendment."""

    run_id: int
    active_id: int
    active: SpecDraft
    stored_hash: str


def _required_id(value: int | None, label: str) -> int:
    if value is None:
        message = f"{label} did not receive a durable identity."
        raise RuntimeError(message)
    return value


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


def _success(decision: NodeDecision, output: dict[str, object]) -> TransitionResult:
    return TransitionResult(ok=True, applied_node_id=decision.node_id, output=output)


def _open_run(session: Session, project_id: int) -> DiscoveryRun | None:
    rows = session.exec(
        select(DiscoveryRun).where(
            col(DiscoveryRun.project_id) == project_id,
            col(DiscoveryRun.purpose) == "extension",
            col(DiscoveryRun.closed_at).is_(None),
        )
    ).all()
    return rows[0] if len(rows) == 1 else None


def _run_matches_decision(run: DiscoveryRun, decision: NodeDecision) -> bool:
    return decision.instance_key == f"run:{run.discovery_run_id}"


def _prd_rows(session: Session, project_id: int, run_id: int) -> list[PrdVersion]:
    return list(
        session.exec(
            select(PrdVersion)
            .where(
                col(PrdVersion.project_id) == project_id,
                col(PrdVersion.discovery_run_id) == run_id,
            )
            .order_by(col(PrdVersion.version_number))
        ).all()
    )


def _active_prd(rows: list[PrdVersion]) -> PrdVersion | None:
    parents = {
        item.supersedes_prd_version_id
        for item in rows
        if item.supersedes_prd_version_id is not None
    }
    leaves = tuple(
        item for item in rows if _required_id(item.prd_version_id, "PRD") not in parents
    )
    return leaves[0] if len(leaves) == 1 else None


def _prd_review(
    session: Session,
    project_id: int,
    prd_version_id: int,
) -> PrdDecision | None:
    return session.exec(
        select(PrdDecision).where(
            col(PrdDecision.project_id) == project_id,
            col(PrdDecision.prd_version_id) == prd_version_id,
        )
    ).one_or_none()


def _spec_rows(session: Session, project_id: int, run_id: int) -> list[SpecDraft]:
    return list(
        session.exec(
            select(SpecDraft)
            .where(
                col(SpecDraft.project_id) == project_id,
                col(SpecDraft.discovery_run_id) == run_id,
            )
            .order_by(col(SpecDraft.version_number))
        ).all()
    )


def _active_spec(rows: list[SpecDraft]) -> SpecDraft | None:
    parents = {
        item.supersedes_spec_draft_id
        for item in rows
        if item.supersedes_spec_draft_id is not None
    }
    leaves = tuple(
        item
        for item in rows
        if _required_id(item.spec_draft_id, "specification draft") not in parents
    )
    return leaves[0] if len(leaves) == 1 else None


def _spec_review(
    session: Session,
    project_id: int,
    spec_draft_id: int,
) -> SpecDraftDecision | None:
    return session.exec(
        select(SpecDraftDecision).where(
            col(SpecDraftDecision.project_id) == project_id,
            col(SpecDraftDecision.spec_draft_id) == spec_draft_id,
        )
    ).one_or_none()


def _replacement_parent_error(
    *,
    active_id: int | None,
    active_decision: str | None,
    requested_parent_id: int | None,
    label: str,
) -> str | None:
    if active_id is None:
        return (
            f"The first {label} cannot supersede another version."
            if requested_parent_id is not None
            else None
        )
    if active_decision not in {"rejected", "feedback"}:
        return f"Only a rejected or feedback {label} can be replaced."
    if requested_parent_id != active_id:
        return f"The replacement must supersede the exact reviewed {label}."
    return None


def _execute_start(
    session: Session,
    request: StartScopeExtension,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    approved = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.product_id) == request.project_id,
            col(SpecRegistry.status) == "approved",
        )
    ).all()
    if (
        len(approved) != 1
        or approved[0].spec_version_id != request.base_spec_version_id
        or approved[0].spec_hash != request.base_spec_hash
        or any(
            reference.fact_type == "spec_version"
            and (
                int(reference.fact_id) != request.base_spec_version_id
                or reference.fingerprint != request.base_spec_hash
            )
            for reference in decision.fact_references
        )
    ):
        return _conflict("Scope extension does not target the accepted base spec.")
    if _open_run(session, request.project_id) is not None:
        return _conflict("One unresolved scope-extension run already exists.")
    ordinals = session.exec(
        select(DiscoveryRun.ordinal).where(
            col(DiscoveryRun.project_id) == request.project_id
        )
    ).all()
    run = DiscoveryRun(
        project_id=request.project_id,
        purpose="extension",
        ordinal=max(ordinals, default=0) + 1,
        base_spec_version_id=request.base_spec_version_id,
        base_spec_hash=request.base_spec_hash,
        created_at=evaluated_at,
    )
    session.add(run)
    session.flush()
    run_id = _required_id(run.discovery_run_id, "scope-extension run")
    return _success(
        decision,
        {
            "discovery_run_id": run_id,
            "base_spec_version_id": request.base_spec_version_id,
            "base_spec_hash": request.base_spec_hash,
        },
    )


def _execute_challenge(
    session: Session,
    request: RecordExtensionChallenge,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    run = _open_run(session, request.project_id)
    if run is None or not _run_matches_decision(run, decision):
        return _conflict("The extension challenge does not target the open run.")
    run_id = _required_id(run.discovery_run_id, "scope-extension run")
    existing = session.exec(
        select(ChallengeArtifact).where(
            col(ChallengeArtifact.project_id) == request.project_id,
            col(ChallengeArtifact.discovery_run_id) == run_id,
        )
    ).all()
    if existing:
        return _conflict("The extension challenge already exists.")
    artifact = ChallengeArtifact(
        project_id=request.project_id,
        discovery_run_id=run_id,
        version_number=1,
        canonical_content_json=canonical_json(request.canonical_content),
        content_fingerprint=canonical_hash(request.canonical_content),
        provenance_path=request.provenance_path,
        created_at=evaluated_at,
    )
    session.add(artifact)
    session.flush()
    return _success(
        decision,
        {
            "challenge_artifact_id": _required_id(
                artifact.challenge_artifact_id,
                "challenge artifact",
            ),
            "content_fingerprint": artifact.content_fingerprint,
        },
    )


def _execute_prd(
    session: Session,
    request: RecordExtensionPrd,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    run = _open_run(session, request.project_id)
    if run is None or not _run_matches_decision(run, decision):
        return _conflict("The extension PRD does not target the open run.")
    run_id = _required_id(run.discovery_run_id, "scope-extension run")
    challenge = session.exec(
        select(ChallengeArtifact).where(
            col(ChallengeArtifact.project_id) == request.project_id,
            col(ChallengeArtifact.discovery_run_id) == run_id,
            col(ChallengeArtifact.challenge_artifact_id)
            == request.challenge_artifact_id,
        )
    ).one_or_none()
    if challenge is None:
        return _conflict("The extension PRD does not target the run challenge.")
    rows = _prd_rows(session, request.project_id, run_id)
    active = _active_prd(rows)
    active_id = None if active is None else _required_id(active.prd_version_id, "PRD")
    review = (
        None
        if active_id is None
        else _prd_review(session, request.project_id, active_id)
    )
    error = _replacement_parent_error(
        active_id=active_id,
        active_decision=None if review is None else review.decision,
        requested_parent_id=request.supersedes_prd_version_id,
        label="extension PRD",
    )
    if error is not None:
        return _conflict(error)
    version = PrdVersion(
        project_id=request.project_id,
        discovery_run_id=run_id,
        version_number=max((item.version_number for item in rows), default=0) + 1,
        canonical_content_json=canonical_json(request.canonical_content),
        content_fingerprint=canonical_hash(request.canonical_content),
        supersedes_prd_version_id=request.supersedes_prd_version_id,
        provenance_path=request.provenance_path,
        created_at=evaluated_at,
    )
    session.add(version)
    session.flush()
    return _success(
        decision,
        {
            "prd_version_id": _required_id(version.prd_version_id, "PRD"),
            "content_fingerprint": version.content_fingerprint,
        },
    )


def _execute_prd_review(
    session: Session,
    request: DecideExtensionPrd,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    run = _open_run(session, request.project_id)
    if run is None:
        return _conflict("There is no open extension PRD to review.")
    run_id = _required_id(run.discovery_run_id, "scope-extension run")
    active = _active_prd(_prd_rows(session, request.project_id, run_id))
    if active is None:
        return _conflict("The extension PRD chain is ambiguous.")
    active_id = _required_id(active.prd_version_id, "PRD")
    if (
        decision.instance_key != f"prd:{active_id}"
        or request.prd_version_id != active_id
        or request.artifact_fingerprint != active.content_fingerprint
        or _prd_review(session, request.project_id, active_id) is not None
    ):
        return _conflict("The decision does not target the exact extension PRD.")
    review = PrdDecision(
        project_id=request.project_id,
        discovery_run_id=run_id,
        prd_version_id=active_id,
        artifact_fingerprint=active.content_fingerprint,
        decision=request.decision,
        reviewer=request.actor,
        notes=request.notes,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(review)
    session.flush()
    return _success(
        decision,
        {"prd_decision_id": _required_id(review.prd_decision_id, "PRD decision")},
    )


def _execute_spec(
    session: Session,
    request: RecordAmendmentSpecDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    run = _open_run(session, request.project_id)
    if run is None or not _run_matches_decision(run, decision):
        return _conflict("The amendment draft does not target the open run.")
    run_id = _required_id(run.discovery_run_id, "scope-extension run")
    active_prd = _active_prd(_prd_rows(session, request.project_id, run_id))
    if active_prd is None:
        return _conflict("The extension PRD chain is ambiguous.")
    prd_id = _required_id(active_prd.prd_version_id, "PRD")
    prd_review = _prd_review(session, request.project_id, prd_id)
    if (
        request.prd_version_id != prd_id
        or prd_review is None
        or prd_review.decision != "accepted"
        or request.base_spec_version_id != run.base_spec_version_id
        or request.base_spec_hash != run.base_spec_hash
    ):
        return _conflict("The amendment draft does not match accepted run inputs.")
    rows = _spec_rows(session, request.project_id, run_id)
    active = _active_spec(rows)
    active_id = (
        None
        if active is None
        else _required_id(active.spec_draft_id, "specification draft")
    )
    review = (
        None
        if active_id is None
        else _spec_review(session, request.project_id, active_id)
    )
    error = _replacement_parent_error(
        active_id=active_id,
        active_decision=None if review is None else review.decision,
        requested_parent_id=request.supersedes_spec_draft_id,
        label="amendment draft",
    )
    if error is not None:
        return _conflict(error)
    try:
        normalized = normalize_spec_content_for_registry(
            canonical_json(request.canonical_content)
        )
    except ValueError as error:
        return _conflict(f"The amendment draft is not a valid specification: {error}")
    draft = SpecDraft(
        project_id=request.project_id,
        discovery_run_id=run_id,
        kind="amendment",
        version_number=max((item.version_number for item in rows), default=0) + 1,
        canonical_content_json=normalized.content,
        content_fingerprint=normalized.spec_hash,
        base_spec_version_id=request.base_spec_version_id,
        base_spec_hash=request.base_spec_hash,
        supersedes_spec_draft_id=request.supersedes_spec_draft_id,
        provenance_path=request.provenance_path,
        created_at=evaluated_at,
    )
    session.add(draft)
    session.flush()
    return _success(
        decision,
        {
            "spec_draft_id": _required_id(
                draft.spec_draft_id,
                "specification draft",
            ),
            "content_fingerprint": draft.content_fingerprint,
        },
    )


def _execute_spec_review(
    session: Session,
    request: DecideAmendmentSpecDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    run = _open_run(session, request.project_id)
    if run is None:
        return _conflict("There is no open amendment draft to review.")
    run_id = _required_id(run.discovery_run_id, "scope-extension run")
    active = _active_spec(_spec_rows(session, request.project_id, run_id))
    if active is None:
        return _conflict("The amendment draft chain is ambiguous.")
    active_id = _required_id(active.spec_draft_id, "specification draft")
    if (
        decision.instance_key != f"spec:{active_id}"
        or request.spec_draft_id != active_id
        or request.artifact_fingerprint != active.content_fingerprint
        or active.kind != "amendment"
        or _spec_review(session, request.project_id, active_id) is not None
    ):
        return _conflict("The decision does not target the exact amendment draft.")
    review = SpecDraftDecision(
        project_id=request.project_id,
        discovery_run_id=run_id,
        spec_draft_id=active_id,
        artifact_fingerprint=active.content_fingerprint,
        decision=request.decision,
        reviewer=request.actor,
        notes=request.notes,
        idempotency_key=request.idempotency_key,
        decided_at=evaluated_at,
    )
    session.add(review)
    session.flush()
    return _success(
        decision,
        {
            "spec_draft_decision_id": _required_id(
                review.spec_draft_decision_id,
                "specification draft decision",
            )
        },
    )


def _registration_inputs(
    session: Session,
    request: RegisterScopeExtension,
    decision: NodeDecision,
) -> _RegistrationInputs | TransitionResult:
    run = _open_run(session, request.project_id)
    if run is None or not _run_matches_decision(run, decision):
        return _conflict("Scope registration does not target the open run.")
    run_id = _required_id(run.discovery_run_id, "scope-extension run")
    active = _active_spec(_spec_rows(session, request.project_id, run_id))
    if active is None:
        return _conflict("The amendment draft chain is ambiguous.")
    active_id = _required_id(active.spec_draft_id, "specification draft")
    review = _spec_review(session, request.project_id, active_id)
    if (
        request.spec_draft_id != active_id
        or active.kind != "amendment"
        or active.base_spec_version_id != run.base_spec_version_id
        or active.base_spec_hash != run.base_spec_hash
        or review is None
        or review.decision != "accepted"
    ):
        return _conflict("Registration requires the exact accepted amendment.")
    try:
        stored_hash = canonical_stored_json_hash(active.canonical_content_json)
    except (TypeError, ValueError):
        return _conflict("The accepted amendment contains malformed stored JSON.")
    if (
        stored_hash != active.content_fingerprint
        or stored_hash != review.artifact_fingerprint
    ):
        return _conflict("The accepted amendment content or review changed.")
    return _RegistrationInputs(
        run_id=run_id,
        active_id=active_id,
        active=active,
        stored_hash=stored_hash,
    )


def _registration_is_duplicate(
    session: Session,
    project_id: int,
    inputs: _RegistrationInputs,
) -> TransitionResult | None:
    if (
        session.exec(
            select(ScopeExtensionRegistration).where(
                col(ScopeExtensionRegistration.discovery_run_id) == inputs.run_id
            )
        ).one_or_none()
        is not None
    ):
        return _conflict("The extension run is already registered.")
    if session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.product_id) == project_id,
            col(SpecRegistry.spec_hash) == inputs.stored_hash,
        )
    ).all():
        return _conflict("An applied specification cannot be registered again.")
    return None


def _execute_registration(
    session: Session,
    request: RegisterScopeExtension,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    inputs = _registration_inputs(session, request, decision)
    if isinstance(inputs, TransitionResult):
        return inputs
    duplicate = _registration_is_duplicate(session, request.project_id, inputs)
    if duplicate is not None:
        return duplicate
    base = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.product_id) == request.project_id,
            col(SpecRegistry.spec_version_id) == inputs.active.base_spec_version_id,
            col(SpecRegistry.spec_hash) == inputs.active.base_spec_hash,
            col(SpecRegistry.status) == "approved",
        )
    ).one_or_none()
    if base is None:
        return _conflict("The extension run base spec is no longer current.")
    base.status = "superseded"
    session.add(base)
    spec = register_approved_spec_from_canonical_json(
        session,
        ApprovedCanonicalSpec(
            product_id=request.project_id,
            canonical_content_json=inputs.active.canonical_content_json,
            content_ref=inputs.active.provenance_path,
            approved_at=evaluated_at,
            approved_by=request.actor,
            approval_notes="Scope extension registration",
        ),
    )
    spec_id = _required_id(spec.spec_version_id, "specification version")
    registration = ScopeExtensionRegistration(
        project_id=request.project_id,
        discovery_run_id=inputs.run_id,
        spec_draft_id=inputs.active_id,
        spec_version_id=spec_id,
        spec_hash=spec.spec_hash,
        registered_by=request.actor,
        registered_at=evaluated_at,
    )
    session.add(registration)
    session.flush()
    return _success(
        decision,
        {
            "scope_extension_registration_id": _required_id(
                registration.scope_extension_registration_id,
                "scope-extension registration",
            ),
            "spec_version_id": spec_id,
            "spec_hash": spec.spec_hash,
        },
    )


def _authority_is_accepted(
    session: Session,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
) -> bool:
    authority = session.get(CompiledSpecAuthority, authority_id)
    acceptance = session.exec(
        select(SpecAuthorityAcceptance).where(
            col(SpecAuthorityAcceptance.product_id) == project_id,
            col(SpecAuthorityAcceptance.pending_authority_id) == authority_id,
            col(SpecAuthorityAcceptance.status) == "accepted",
        )
    ).one_or_none()
    return (
        authority is not None
        and acceptance is not None
        and acceptance.authority_fingerprint == authority_fingerprint
    )


def _execute_reconciliation(
    session: Session,
    request: ReconcileScopeExtension,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    run = _open_run(session, request.project_id)
    if (
        run is None
        or run.discovery_run_id != request.discovery_run_id
        or not _run_matches_decision(run, decision)
        or not _authority_is_accepted(
            session,
            project_id=request.project_id,
            authority_id=request.replacement_authority_id,
            authority_fingerprint=request.replacement_authority_fingerprint,
        )
    ):
        return _conflict("Reconciliation does not target accepted replacement facts.")
    expected = tuple(
        item
        for item in decision.fact_references
        if item.fact_type in {"vision", "backlog", "roadmap", "story"}
    )
    supplied = tuple(
        FactReference(
            fact_type=item.artifact_type,
            fact_id=str(item.artifact_id),
            fingerprint=item.artifact_fingerprint,
        )
        for item in request.artifact_references
    )
    if supplied != expected or len(supplied) != len(set(supplied)):
        return _conflict("Reconciliation artifact relationships changed.")
    payload = [item.model_dump(mode="json") for item in supplied]
    row = ScopeExtensionReconciliation(
        project_id=request.project_id,
        discovery_run_id=request.discovery_run_id,
        replacement_authority_id=request.replacement_authority_id,
        replacement_authority_fingerprint=(request.replacement_authority_fingerprint),
        artifact_references_json=canonical_json(payload),
        artifact_references_fingerprint=canonical_hash(payload),
        reconciled_by=request.actor,
        reconciled_at=evaluated_at,
    )
    session.add(row)
    run.closed_at = evaluated_at
    session.add(run)
    session.flush()
    return _success(
        decision,
        {
            "scope_extension_reconciliation_id": _required_id(
                row.scope_extension_reconciliation_id,
                "scope-extension reconciliation",
            ),
            "discovery_run_id": request.discovery_run_id,
        },
    )


def _execute_abandonment(
    session: Session,
    request: AbandonScopeExtension,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    run = _open_run(session, request.project_id)
    if (
        run is None
        or run.discovery_run_id != request.discovery_run_id
        or not _run_matches_decision(run, decision)
    ):
        return _conflict("Abandonment does not target the open extension run.")
    registration = session.exec(
        select(ScopeExtensionRegistration).where(
            col(ScopeExtensionRegistration.discovery_run_id) == request.discovery_run_id
        )
    ).one_or_none()
    if registration is not None:
        accepted = session.exec(
            select(SpecAuthorityAcceptance).where(
                col(SpecAuthorityAcceptance.product_id) == request.project_id,
                col(SpecAuthorityAcceptance.status) == "accepted",
            )
        ).all()
        compiled_ids = set(
            session.exec(
                select(CompiledSpecAuthority.authority_id).where(
                    col(CompiledSpecAuthority.spec_version_id)
                    == registration.spec_version_id
                )
            ).all()
        )
        if any(item.pending_authority_id in compiled_ids for item in accepted):
            return _conflict("Accepted replacement authority cannot be abandoned.")
        replacement = session.get(SpecRegistry, registration.spec_version_id)
        base = session.get(SpecRegistry, run.base_spec_version_id)
        if replacement is None or base is None or base.spec_hash != run.base_spec_hash:
            return _conflict("Registered extension specs cannot be restored safely.")
        replacement.status = "superseded"
        base.status = "approved"
        session.add(replacement)
        session.add(base)
    abandonment = DiscoveryRunAbandonment(
        project_id=request.project_id,
        discovery_run_id=request.discovery_run_id,
        reason=request.reason,
        abandoned_by=request.actor,
        abandoned_at=evaluated_at,
    )
    session.add(abandonment)
    run.closed_at = evaluated_at
    session.add(run)
    session.flush()
    return _success(
        decision,
        {
            "discovery_run_abandonment_id": _required_id(
                abandonment.discovery_run_abandonment_id,
                "discovery-run abandonment",
            ),
            "discovery_run_id": request.discovery_run_id,
        },
    )


def execute_scope_extension_request(
    session: Session,
    request: ScopeExtensionRequest,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Dispatch the closed scope-extension transition family."""
    if isinstance(request, StartScopeExtension):
        result = _execute_start(session, request, decision, evaluated_at)
    elif isinstance(request, RecordExtensionChallenge):
        result = _execute_challenge(session, request, decision, evaluated_at)
    elif isinstance(request, RecordExtensionPrd):
        result = _execute_prd(session, request, decision, evaluated_at)
    elif isinstance(request, DecideExtensionPrd):
        result = _execute_prd_review(session, request, decision, evaluated_at)
    elif isinstance(request, RecordAmendmentSpecDraft):
        result = _execute_spec(session, request, decision, evaluated_at)
    elif isinstance(request, DecideAmendmentSpecDraft):
        result = _execute_spec_review(session, request, decision, evaluated_at)
    elif isinstance(request, RegisterScopeExtension):
        result = _execute_registration(session, request, decision, evaluated_at)
    elif isinstance(request, ReconcileScopeExtension):
        result = _execute_reconciliation(session, request, decision, evaluated_at)
    elif isinstance(request, AbandonScopeExtension):
        result = _execute_abandonment(session, request, decision, evaluated_at)
    else:
        assert_never(request)
    return result


__all__ = ["execute_scope_extension_request"]
