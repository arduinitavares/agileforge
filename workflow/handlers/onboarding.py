"""Transactional handlers for greenfield and brownfield onboarding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.specs import SpecRegistry
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    InitialScopeRegistration,
    PrdDecision,
    PrdVersion,
    RepositoryBaseline,
    RepositoryInventory,
    SpecDraft,
    SpecDraftDecision,
)
from services.specs.lifecycle_service import (
    ApprovedCanonicalSpec,
    register_approved_spec_from_canonical_json,
)
from workflow.contracts import (
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
from workflow.repository_inventory import (
    InventoryFingerprintEntry,
    canonical_inventory_payload,
    encode_repository_paths,
    inventory_binding_fingerprint,
    repository_path_bytes,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.requests import (
        DecideBrownfieldInitialSpec,
        DecideInitialSpecDraft,
        DecidePrd,
        RecordBrownfieldSpecDraft,
        RecordChallengeArtifact,
        RecordInitialSpecDraft,
        RecordPrdVersion,
        RecordRepositoryBaseline,
        RecordRepositoryInventory,
        RegisterInitialScope,
    )


def _required_id(value: int | None, label: str) -> int:
    if value is None:
        msg = f"{label} identity was not assigned after flush."
        raise RuntimeError(msg)
    return value


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


def _success(
    decision: NodeDecision,
    output: dict[str, object],
) -> TransitionResult:
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output=output,
    )


def _initial_run(session: Session, project_id: int) -> DiscoveryRun | None:
    rows = session.exec(
        select(DiscoveryRun).where(
            col(DiscoveryRun.project_id) == project_id,
            col(DiscoveryRun.purpose) == "initial",
        )
    ).all()
    return rows[0] if len(rows) == 1 and rows[0].closed_at is None else None


def _prd_rows(
    session: Session,
    project_id: int,
    run_id: int,
) -> list[PrdVersion]:
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
    referenced = {
        row.supersedes_prd_version_id
        for row in rows
        if row.supersedes_prd_version_id is not None
    }
    leaves = tuple(
        row for row in rows if _required_id(row.prd_version_id, "PRD") not in referenced
    )
    return leaves[0] if len(leaves) == 1 else None


def _prd_decision(
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


def _prd_replacement_error(
    session: Session,
    project_id: int,
    rows: list[PrdVersion],
    requested_parent_id: int | None,
) -> str | None:
    if not rows:
        return (
            "The first PRD version cannot supersede another PRD."
            if requested_parent_id is not None
            else None
        )
    active = _active_prd(rows)
    if active is None:
        return "The persisted PRD version chain is ambiguous."
    active_id = _required_id(active.prd_version_id, "PRD")
    terminal = _prd_decision(session, project_id, active_id)
    if terminal is None or terminal.decision == "accepted":
        return "Only a rejected or feedback PRD can be replaced."
    if requested_parent_id != active_id:
        return "The replacement must supersede the exact reviewed PRD."
    return None


def _spec_rows(
    session: Session,
    project_id: int,
    run_id: int,
) -> list[SpecDraft]:
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
    referenced = {
        row.supersedes_spec_draft_id
        for row in rows
        if row.supersedes_spec_draft_id is not None
    }
    leaves = tuple(
        row
        for row in rows
        if _required_id(row.spec_draft_id, "specification draft") not in referenced
    )
    return leaves[0] if len(leaves) == 1 else None


def _spec_decision(
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


def _spec_replacement_error(
    session: Session,
    project_id: int,
    rows: list[SpecDraft],
    requested_parent_id: int | None,
) -> str | None:
    if not rows:
        return (
            "The first initial draft cannot supersede another draft."
            if requested_parent_id is not None
            else None
        )
    active = _active_spec(rows)
    if active is None:
        return "The persisted initial-draft chain is ambiguous."
    active_id = _required_id(active.spec_draft_id, "specification draft")
    terminal = _spec_decision(session, project_id, active_id)
    if terminal is None or terminal.decision == "accepted":
        return "Only a rejected or feedback draft can be replaced."
    if requested_parent_id != active_id:
        return "The replacement must supersede the exact reviewed draft."
    return None


def execute_record_repository_baseline(
    session: Session,
    request: RecordRepositoryBaseline,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Store one canonical repository baseline without changing Project identity."""
    expected_fingerprint = canonical_hash(
        {
            "repository_path": request.repository_path,
            "git_commit": request.git_commit,
            "dirty": request.dirty,
        }
    )
    if request.baseline_fingerprint != expected_fingerprint:
        return _conflict(
            "The repository baseline fingerprint does not match its input."
        )
    rows = session.exec(
        select(RepositoryBaseline).where(
            col(RepositoryBaseline.project_id) == request.project_id
        )
    ).all()
    if rows:
        return _conflict("A repository baseline already exists for this Project.")
    baseline = RepositoryBaseline(
        project_id=request.project_id,
        repository_path=request.repository_path,
        git_commit=request.git_commit,
        dirty=request.dirty,
        content_fingerprint=expected_fingerprint,
        version_number=1,
        recorded_at=evaluated_at,
    )
    session.add(baseline)
    session.flush()
    baseline_id = _required_id(
        baseline.repository_baseline_id,
        "repository baseline",
    )
    return _success(
        decision,
        {
            "repository_baseline_id": baseline_id,
            "content_fingerprint": baseline.content_fingerprint,
        },
    )


def execute_record_repository_inventory(
    session: Session,
    request: RecordRepositoryInventory,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Store complete inventory independently from bounded model selection."""
    baseline = session.exec(
        select(RepositoryBaseline).where(
            col(RepositoryBaseline.project_id) == request.project_id,
            col(RepositoryBaseline.repository_baseline_id)
            == request.repository_baseline_id,
        )
    ).one_or_none()
    if baseline is None:
        return _conflict("The inventory does not target this Project's baseline.")
    existing = session.exec(
        select(RepositoryInventory).where(
            col(RepositoryInventory.project_id) == request.project_id
        )
    ).all()
    if existing:
        return _conflict("A repository inventory already exists for this Project.")

    paths = [item.path for item in request.files]
    expected_paths = sorted(paths, key=repository_path_bytes)
    hashable_paths = {
        item.path for item in request.files if item.content_status == "hashable"
    }
    if (
        paths != expected_paths
        or len(paths) != len(set(paths))
        or sum(item.size_bytes for item in request.files) != request.total_bytes
        or len(request.selected_for_model) != len(set(request.selected_for_model))
        or any(path not in hashable_paths for path in request.selected_for_model)
        or (not request.git_available and baseline.git_commit is not None)
    ):
        return _conflict("The repository inventory payload is not canonical.")
    fingerprint_entries: tuple[InventoryFingerprintEntry, ...] = tuple(
        (
            item.path,
            item.size_bytes,
            item.sha256,
            item.content_status,
        )
        for item in request.files
    )
    inventory_payload = canonical_inventory_payload(
        git_available=request.git_available,
        commit=baseline.git_commit,
        dirty=baseline.dirty,
        files=fingerprint_entries,
        total_bytes=request.total_bytes,
    )
    expected_fingerprint = inventory_binding_fingerprint(
        inventory_payload,
        request.selected_for_model,
    )
    if request.inventory_fingerprint != expected_fingerprint:
        return _conflict(
            "The repository inventory fingerprint does not match its input."
        )

    inventory = RepositoryInventory(
        project_id=request.project_id,
        repository_baseline_id=request.repository_baseline_id,
        canonical_inventory_json=canonical_json(inventory_payload),
        selected_for_model_json=canonical_json(
            encode_repository_paths(request.selected_for_model)
        ),
        content_fingerprint=expected_fingerprint,
        version_number=1,
        file_count=len(request.files),
        total_bytes=request.total_bytes,
        recorded_at=evaluated_at,
    )
    session.add(inventory)
    session.flush()
    inventory_id = _required_id(
        inventory.repository_inventory_id,
        "repository inventory",
    )
    return _success(
        decision,
        {
            "repository_inventory_id": inventory_id,
            "content_fingerprint": inventory.content_fingerprint,
        },
    )


def execute_record_brownfield_spec_draft(
    session: Session,
    request: RecordBrownfieldSpecDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append an initial spec draft derived from one Project inventory."""
    inventory = session.exec(
        select(RepositoryInventory).where(
            col(RepositoryInventory.project_id) == request.project_id,
            col(RepositoryInventory.repository_inventory_id)
            == request.repository_inventory_id,
        )
    ).one_or_none()
    if inventory is None:
        return _conflict("The draft does not target this Project's inventory.")
    if inventory.content_fingerprint != request.repository_inventory_fingerprint:
        return _conflict("The draft does not target the exact repository inventory.")
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    rows = _spec_rows(session, request.project_id, run_id)
    replacement_error = _spec_replacement_error(
        session,
        request.project_id,
        rows,
        request.supersedes_spec_draft_id,
    )
    if replacement_error is not None:
        return _conflict(replacement_error)
    draft = SpecDraft(
        project_id=request.project_id,
        discovery_run_id=run_id,
        kind="initial",
        version_number=max((row.version_number for row in rows), default=0) + 1,
        canonical_content_json=canonical_json(request.canonical_content),
        content_fingerprint=canonical_hash(request.canonical_content),
        base_spec_version_id=None,
        base_spec_hash=None,
        supersedes_spec_draft_id=request.supersedes_spec_draft_id,
        provenance_path=request.provenance_path,
        created_at=evaluated_at,
    )
    session.add(draft)
    session.flush()
    draft_id = _required_id(draft.spec_draft_id, "specification draft")
    return _success(
        decision,
        {
            "spec_draft_id": draft_id,
            "content_fingerprint": draft.content_fingerprint,
        },
    )


def execute_decide_brownfield_initial_spec(
    session: Session,
    request: DecideBrownfieldInitialSpec,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append a terminal decision bound to the exact brownfield draft."""
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    active = _active_spec(_spec_rows(session, request.project_id, run_id))
    if active is None:
        return _conflict("The persisted initial-draft chain is ambiguous.")
    active_id = _required_id(active.spec_draft_id, "specification draft")
    if (
        request.spec_draft_id != active_id
        or request.artifact_fingerprint != active.content_fingerprint
    ):
        return _conflict("The decision does not target the exact active draft.")
    if _spec_decision(session, request.project_id, active_id) is not None:
        return _conflict("The exact initial draft already has a terminal decision.")
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
    review_id = _required_id(
        review.spec_draft_decision_id,
        "specification draft decision",
    )
    return _success(decision, {"spec_draft_decision_id": review_id})


def execute_record_challenge_artifact(
    session: Session,
    request: RecordChallengeArtifact,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Store the initial challenge artifact as canonical immutable JSON."""
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    existing = session.exec(
        select(ChallengeArtifact).where(
            col(ChallengeArtifact.project_id) == request.project_id,
            col(ChallengeArtifact.discovery_run_id) == run_id,
        )
    ).all()
    if existing:
        return _conflict("The initial challenge artifact already exists.")

    artifact = ChallengeArtifact(
        project_id=request.project_id,
        discovery_run_id=run_id,
        version_number=1,
        canonical_content_json=canonical_json(request.canonical_content),
        content_fingerprint=canonical_hash(request.canonical_content),
        supersedes_challenge_artifact_id=None,
        provenance_path=request.provenance_path,
        created_at=evaluated_at,
    )
    session.add(artifact)
    session.flush()
    artifact_id = _required_id(artifact.challenge_artifact_id, "challenge artifact")
    return _success(
        decision,
        {
            "challenge_artifact_id": artifact_id,
            "content_fingerprint": artifact.content_fingerprint,
        },
    )


def execute_record_prd_version(
    session: Session,
    request: RecordPrdVersion,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append a canonical PRD version linked to the initial challenge."""
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    challenge = session.exec(
        select(ChallengeArtifact).where(
            col(ChallengeArtifact.project_id) == request.project_id,
            col(ChallengeArtifact.discovery_run_id) == run_id,
            col(ChallengeArtifact.challenge_artifact_id)
            == request.challenge_artifact_id,
        )
    ).one_or_none()
    if challenge is None:
        return _conflict("The request does not target the initial challenge artifact.")

    rows = _prd_rows(session, request.project_id, run_id)
    replacement_error = _prd_replacement_error(
        session,
        request.project_id,
        rows,
        request.supersedes_prd_version_id,
    )
    if replacement_error is not None:
        return _conflict(replacement_error)

    version = PrdVersion(
        project_id=request.project_id,
        discovery_run_id=run_id,
        version_number=max((row.version_number for row in rows), default=0) + 1,
        canonical_content_json=canonical_json(request.canonical_content),
        content_fingerprint=canonical_hash(request.canonical_content),
        supersedes_prd_version_id=request.supersedes_prd_version_id,
        provenance_path=request.provenance_path,
        created_at=evaluated_at,
    )
    session.add(version)
    session.flush()
    version_id = _required_id(version.prd_version_id, "PRD")
    return _success(
        decision,
        {
            "prd_version_id": version_id,
            "content_fingerprint": version.content_fingerprint,
        },
    )


def execute_decide_prd(
    session: Session,
    request: DecidePrd,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append one terminal decision bound to the active PRD fingerprint."""
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    active = _active_prd(_prd_rows(session, request.project_id, run_id))
    if active is None:
        return _conflict("The persisted PRD version chain is ambiguous.")
    active_id = _required_id(active.prd_version_id, "PRD")
    if (
        request.prd_version_id != active_id
        or request.artifact_fingerprint != active.content_fingerprint
    ):
        return _conflict("The decision does not target the exact active PRD.")
    if _prd_decision(session, request.project_id, active_id) is not None:
        return _conflict("The exact PRD already has a terminal decision.")

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
    review_id = _required_id(review.prd_decision_id, "PRD decision")
    return _success(decision, {"prd_decision_id": review_id})


def execute_record_initial_spec_draft(
    session: Session,
    request: RecordInitialSpecDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append a canonical initial draft with both amendment bases absent."""
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    active_prd = _active_prd(_prd_rows(session, request.project_id, run_id))
    if active_prd is None:
        return _conflict("The persisted PRD version chain is ambiguous.")
    active_prd_id = _required_id(active_prd.prd_version_id, "PRD")
    prd_review = _prd_decision(session, request.project_id, active_prd_id)
    if (
        request.prd_version_id != active_prd_id
        or prd_review is None
        or prd_review.decision != "accepted"
        or prd_review.artifact_fingerprint != active_prd.content_fingerprint
    ):
        return _conflict("The draft does not target the exact accepted PRD.")

    rows = _spec_rows(session, request.project_id, run_id)
    replacement_error = _spec_replacement_error(
        session,
        request.project_id,
        rows,
        request.supersedes_spec_draft_id,
    )
    if replacement_error is not None:
        return _conflict(replacement_error)

    draft = SpecDraft(
        project_id=request.project_id,
        discovery_run_id=run_id,
        kind="initial",
        version_number=max((row.version_number for row in rows), default=0) + 1,
        canonical_content_json=canonical_json(request.canonical_content),
        content_fingerprint=canonical_hash(request.canonical_content),
        base_spec_version_id=None,
        base_spec_hash=None,
        supersedes_spec_draft_id=request.supersedes_spec_draft_id,
        provenance_path=request.provenance_path,
        created_at=evaluated_at,
    )
    session.add(draft)
    session.flush()
    draft_id = _required_id(draft.spec_draft_id, "specification draft")
    return _success(
        decision,
        {
            "spec_draft_id": draft_id,
            "content_fingerprint": draft.content_fingerprint,
        },
    )


def execute_decide_initial_spec_draft(
    session: Session,
    request: DecideInitialSpecDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Append one terminal decision bound to the active initial draft."""
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    active = _active_spec(_spec_rows(session, request.project_id, run_id))
    if active is None:
        return _conflict("The persisted initial-draft chain is ambiguous.")
    active_id = _required_id(active.spec_draft_id, "specification draft")
    if (
        active.kind != "initial"
        or active.base_spec_version_id is not None
        or active.base_spec_hash is not None
    ):
        return _conflict("Initial registration cannot review an amendment draft.")
    if (
        request.spec_draft_id != active_id
        or request.artifact_fingerprint != active.content_fingerprint
    ):
        return _conflict("The decision does not target the exact active draft.")
    if _spec_decision(session, request.project_id, active_id) is not None:
        return _conflict("The exact initial draft already has a terminal decision.")

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
    review_id = _required_id(
        review.spec_draft_decision_id,
        "specification draft decision",
    )
    return _success(decision, {"spec_draft_decision_id": review_id})


def execute_register_initial_scope(
    session: Session,
    request: RegisterInitialScope,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Atomically register one accepted stored draft as the first approved spec."""
    run = _initial_run(session, request.project_id)
    if run is None:
        return _conflict("The Project does not have exactly one open initial run.")
    run_id = _required_id(run.discovery_run_id, "initial discovery run")
    active = _active_spec(_spec_rows(session, request.project_id, run_id))
    if active is None:
        return _conflict("The persisted initial-draft chain is ambiguous.")
    active_id = _required_id(active.spec_draft_id, "specification draft")
    review = _spec_decision(session, request.project_id, active_id)
    if (
        request.spec_draft_id != active_id
        or active.kind != "initial"
        or active.base_spec_version_id is not None
        or active.base_spec_hash is not None
        or review is None
        or review.decision != "accepted"
    ):
        return _conflict("Registration requires the exact accepted initial draft.")
    try:
        stored_fingerprint = canonical_stored_json_hash(active.canonical_content_json)
    except (TypeError, ValueError):
        stored_fingerprint = None
    if (
        stored_fingerprint is None
        or stored_fingerprint != active.content_fingerprint
        or stored_fingerprint != review.artifact_fingerprint
    ):
        message = (
            "The accepted initial draft contains malformed stored JSON."
            if stored_fingerprint is None
            else (
                "The accepted initial draft content does not match its terminal review."
            )
        )
        return _conflict(message)
    existing_registrations = session.exec(
        select(InitialScopeRegistration).where(
            col(InitialScopeRegistration.project_id) == request.project_id
        )
    ).all()
    existing_specs = session.exec(
        select(SpecRegistry).where(col(SpecRegistry.product_id) == request.project_id)
    ).all()
    if existing_registrations or existing_specs:
        return _conflict("Initial scope has already been registered.")

    spec = register_approved_spec_from_canonical_json(
        session,
        ApprovedCanonicalSpec(
            product_id=request.project_id,
            canonical_content_json=active.canonical_content_json,
            content_ref=active.provenance_path,
            approved_at=evaluated_at,
            approved_by=request.actor,
            approval_notes="Initial scope registration",
        ),
    )
    spec_version_id = _required_id(spec.spec_version_id, "specification version")
    registration = InitialScopeRegistration(
        project_id=request.project_id,
        discovery_run_id=run_id,
        spec_draft_id=active_id,
        spec_version_id=spec_version_id,
        spec_hash=spec.spec_hash,
        registered_by=request.actor,
        registered_at=evaluated_at,
    )
    session.add(registration)
    session.flush()
    registration_id = _required_id(
        registration.initial_scope_registration_id,
        "initial-scope registration",
    )
    return _success(
        decision,
        {
            "initial_scope_registration_id": registration_id,
            "spec_version_id": spec_version_id,
            "spec_hash": spec.spec_hash,
        },
    )


__all__ = [
    "execute_decide_brownfield_initial_spec",
    "execute_decide_initial_spec_draft",
    "execute_decide_prd",
    "execute_record_brownfield_spec_draft",
    "execute_record_challenge_artifact",
    "execute_record_initial_spec_draft",
    "execute_record_prd_version",
    "execute_record_repository_baseline",
    "execute_record_repository_inventory",
    "execute_register_initial_scope",
]
