"""Read-only Spec Authority projections for the agent workbench."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, Final, cast

from sqlmodel import Session, select

from models import db as model_db
from models.authority_curation import AuthorityCurationAttempt, AuthorityFeedbackAttempt
from models.core import Project
from models.product_definition import SpecificationCandidate
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.envelope import (
    WorkbenchWarning,
    error_envelope,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.contracts.specification import render_invariant_summary
from services.specs.authority_selection import (
    accepted_compiled_authority,
    compiled_authority_for_acceptance,
    latest_compiled_authority,
)
from services.specs.authority_selection import (
    pending_authority_fingerprint as canonical_pending_authority_fingerprint,
)
from services.specs.candidate_contract import (
    SpecificationCandidateEnvelope,
    load_candidate_contract,
)
from services.specs.compiler_service import (
    CompiledAuthorityReadFailure,
    compiled_authority_read_failure,
    load_compiled_artifact,
)
from utils.agileforge_spec_profile_v2 import canonical_spec_json

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from utils.agileforge_spec_profile_v2 import SpecificationPayload
    from utils.spec_schemas import SpecAuthorityCompilationSuccess

JsonDict = dict[str, Any]

AUTHORITY_STATUS_COMMAND: Final[str] = "agileforge authority status"
AUTHORITY_INVARIANTS_COMMAND: Final[str] = "agileforge authority invariants"

@dataclass(frozen=True)
class _AuthoritySelection:
    """Accepted-authority lookup result for a project."""

    specs: list[SpecRegistry]
    latest_spec: SpecRegistry | None
    accepted: SpecAuthorityAcceptance | None
    rejected: SpecAuthorityAcceptance | None
    accepted_spec: SpecRegistry | None
    authority: CompiledSpecAuthority | None
    pending_authority: CompiledSpecAuthority | None
    authority_trusted: bool = True


@dataclass(frozen=True)
class _StatusClassification:
    """Machine-readable status classification."""

    status: str
    reason: str
    stale_reason: str | None


@dataclass(frozen=True)
class _StatusContext:
    """Stable inputs used to render and fingerprint authority status."""

    project_id: int
    project: Project
    selection: _AuthoritySelection
    specification_source: JsonDict
    classification: _StatusClassification
    invariant_count: int
    feedback_curation: JsonDict


@dataclass(frozen=True)
class _InvariantsSelection:
    """Selected invariants spec version plus default acceptance context."""

    spec_version_id: int
    accepted: SpecAuthorityAcceptance | None


def _success(
    data: JsonDict,
    warnings: list[WorkbenchWarning] | None = None,
) -> JsonDict:
    """Return a successful projection envelope-like payload."""
    return {
        "ok": True,
        "data": data,
        "warnings": [warning.to_dict() for warning in warnings or []],
        "errors": [],
    }


def _project_not_found_error(command: str, project_id: int) -> JsonDict:
    """Return a structured project lookup error."""
    return error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.PROJECT_NOT_FOUND,
            message=f"Project {project_id} was not found.",
            details={"project_id": project_id},
            remediation=["agileforge project list"],
        ),
    )


def _authority_not_accepted_error(project_id: int) -> JsonDict:
    """Return the default invariants error when no authority is accepted."""
    return error_envelope(
        command=AUTHORITY_INVARIANTS_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_NOT_ACCEPTED,
            message="No accepted authority exists for this project.",
            details={"project_id": project_id},
            remediation=["Accept a compiled authority before using the default view."],
        ),
    )


def _authority_not_compiled_error(project_id: int, spec_version_id: int) -> JsonDict:
    """Return a structured missing-compiled-authority error."""
    return error_envelope(
        command=AUTHORITY_INVARIANTS_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_NOT_COMPILED,
            message=f"Spec version {spec_version_id} has no compiled authority.",
            details={"project_id": project_id, "spec_version_id": spec_version_id},
            remediation=["Compile authority for the selected spec version."],
        ),
    )


def _authority_acceptance_mismatch_error(
    *,
    project_id: int,
    accepted: SpecAuthorityAcceptance,
    authority: CompiledSpecAuthority,
) -> JsonDict:
    """Return a structured error for unaccepted recompile output."""
    return error_envelope(
        command=AUTHORITY_INVARIANTS_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_ACCEPTANCE_MISMATCH,
            message=(
                "Compiled authority provenance does not match the accepted "
                "authority decision."
            ),
            details={
                "project_id": project_id,
                "spec_version_id": accepted.spec_version_id,
                "accepted_compiler_version": accepted.compiler_version,
                "accepted_prompt_hash": accepted.prompt_hash,
                "compiled_compiler_version": authority.compiler_version,
                "compiled_prompt_hash": authority.prompt_hash,
            },
            remediation=[
                "Accept the recompiled authority or restore the accepted compiled "
                "artifact."
            ],
        ),
    )


def _compiled_authority_read_error(
    *,
    command: str,
    failure: CompiledAuthorityReadFailure,
    data: JsonDict,
    warnings: list[WorkbenchWarning] | None = None,
) -> JsonDict:
    """Return a structured central read failure with stable projection data."""
    envelope = error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode(failure.error_code),
            message=failure.message,
            details=dict(failure.details),
            remediation=list(failure.remediation),
        ),
        warnings=warnings,
    )
    envelope["data"] = data
    return envelope


def _spec_version_not_found_error(project_id: int, spec_version_id: int) -> JsonDict:
    """Return a structured invalid-spec-version error."""
    return error_envelope(
        command=AUTHORITY_INVARIANTS_COMMAND,
        error=workbench_error(
            ErrorCode.SPEC_VERSION_NOT_FOUND,
            message=(
                f"Spec version {spec_version_id} was not found for project "
                f"{project_id}."
            ),
            details={"project_id": project_id, "spec_version_id": spec_version_id},
            remediation=["Choose a spec version that belongs to this project."],
        ),
    )


def _specification_source_warning(
    specification_source: JsonDict,
) -> WorkbenchWarning | None:
    """Return a warning when the exact accepted candidate cannot be loaded."""
    status = specification_source["status"]
    if status == "missing":
        return WorkbenchWarning(
            code="SPECIFICATION_SOURCE_MISSING",
            message="The registry's exact specification candidate is unavailable.",
            details={
                "spec_version_id": specification_source["spec_version_id"],
                "source_specification_candidate_id": specification_source[
                    "source_specification_candidate_id"
                ],
            },
            remediation=["Restore the exact accepted specification candidate."],
        )
    if status == "invalid":
        return WorkbenchWarning(
            code="SPECIFICATION_SOURCE_INVALID",
            message="The registry's specification candidate is invalid.",
            details={
                "spec_version_id": specification_source["spec_version_id"],
                "source_specification_candidate_id": specification_source[
                    "source_specification_candidate_id"
                ],
                "error": specification_source["error"],
            },
            remediation=["Restore the exact canonical candidate envelope."],
        )
    return None


def _iso_z(value: datetime | None) -> str | None:
    """Serialize datetimes as UTC ISO-8601 strings with a Z suffix."""
    if value is None:
        return None
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_field_for_fingerprint(raw: str | None) -> object:
    """Return a canonical JSON field value without unstable object reprs."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except JSONDecodeError:
        return {"malformed_json": raw}


def _invariant_count(
    authority: CompiledSpecAuthority | None,
) -> tuple[int, list[WorkbenchWarning]]:
    """Return invariant count only from a canonical typed artifact."""
    if authority is None:
        return 0, []
    loaded = load_compiled_artifact(authority)
    if not loaded.ok or loaded.artifact is None:
        return 0, []
    return len(loaded.artifact.invariants), []


def _authority_fingerprint_payload(
    authority: CompiledSpecAuthority,
) -> JsonDict:
    """Return deterministic compiled authority fields for fingerprinting."""
    return {
        "authority_id": authority.authority_id,
        "spec_version_id": authority.spec_version_id,
        "compiler_version": authority.compiler_version,
        "prompt_hash": authority.prompt_hash,
        "compiled_at": authority.compiled_at,
        "compiled_artifact_json": _json_field_for_fingerprint(
            authority.compiled_artifact_json
        ),
    }


def _accepted_fingerprint_payload(accepted: SpecAuthorityAcceptance) -> JsonDict:
    """Return deterministic acceptance fields for fingerprinting."""
    return {
        "id": accepted.id,
        "project_id": accepted.project_id,
        "spec_version_id": accepted.spec_version_id,
        "status": accepted.status,
        "policy": accepted.policy,
        "decided_by": accepted.decided_by,
        "decided_at": accepted.decided_at,
        "compiler_version": accepted.compiler_version,
        "prompt_hash": accepted.prompt_hash,
        "spec_hash": accepted.spec_hash,
    }


def _authority_status_fingerprint(
    context: _StatusContext,
) -> str | None:
    """Return the full stable authority status fingerprint when available."""
    selection = context.selection
    accepted = selection.accepted
    authority = selection.authority
    if accepted is None or authority is None:
        return None
    return canonical_hash(
        {
            "command": AUTHORITY_STATUS_COMMAND,
            "project_id": context.project_id,
            "project": {
                "project_id": context.project.project_id,
                "updated_at": context.project.updated_at,
            },
            "status": context.classification.status,
            "reason": context.classification.reason,
            "stale_reason": context.classification.stale_reason,
            "latest_spec": _spec_fingerprint_payload(selection.latest_spec),
            "accepted": _accepted_fingerprint_payload(accepted),
            "compiled": _authority_fingerprint_payload(authority),
            "specification_source": context.specification_source,
            "invariant_count": context.invariant_count,
        }
    )


def _pending_authority_fingerprint(
    authority: CompiledSpecAuthority | None,
) -> str | None:
    """Return a stable fingerprint for a pending compiled authority."""
    return canonical_pending_authority_fingerprint(authority)


def pending_authority_fingerprint(
    authority: CompiledSpecAuthority | None,
) -> str | None:
    """Return the stable public fingerprint for a pending compiled authority."""
    return canonical_pending_authority_fingerprint(authority)


def _spec_fingerprint_payload(spec: SpecRegistry | None) -> JsonDict | None:
    """Return deterministic spec fields for status fingerprinting."""
    if spec is None:
        return None
    return {
        "spec_version_id": spec.spec_version_id,
        "project_id": spec.project_id,
        "spec_hash": spec.spec_hash,
        "status": spec.status,
        "source_specification_candidate_id": (
            spec.source_specification_candidate_id
        ),
        "source_specification_candidate_fingerprint": (
            spec.source_specification_candidate_fingerprint
        ),
        "source_vision_artifact_id": spec.source_vision_artifact_id,
        "source_vision_fingerprint": spec.source_vision_fingerprint,
        "source_product_goal_artifact_id": (
            spec.source_product_goal_artifact_id
        ),
        "source_product_goal_fingerprint": (
            spec.source_product_goal_fingerprint
        ),
        "supersedes_spec_version_id": spec.supersedes_spec_version_id,
        "created_at": spec.created_at,
        "approved_at": spec.approved_at,
    }


def _resolve_status(
    *,
    session: Session,
    project_id: int,
    project: Project,
    selection: _AuthoritySelection,
    specification_source: JsonDict,
) -> JsonDict:
    """Classify status using accepted, compiled, and candidate-source state."""
    data, warnings = _build_status_data(
        session=session,
        project_id=project_id,
        project=project,
        selection=selection,
        specification_source=specification_source,
    )
    return _success(data, warnings)


def _build_status_data(
    *,
    session: Session,
    project_id: int,
    project: Project,
    selection: _AuthoritySelection,
    specification_source: JsonDict,
) -> tuple[JsonDict, list[WorkbenchWarning]]:
    """Build the stable status payload and warnings."""
    classification = _classify_status(
        selection=selection,
        specification_source=specification_source,
    )
    invariant_count, warnings = _invariant_count(selection.authority)
    source_warning = _specification_source_warning(specification_source)
    if source_warning is not None:
        warnings.append(source_warning)
    feedback_curation = _feedback_curation_for_selection(
        session,
        project_id=project_id,
        selection=selection,
    )
    context = _StatusContext(
        project_id=project_id,
        project=project,
        selection=selection,
        specification_source=specification_source,
        classification=classification,
        invariant_count=invariant_count,
        feedback_curation=feedback_curation,
    )
    return _status_data(context), warnings


def _feedback_curation_defaults() -> JsonDict:
    """Return stable feedback and curation defaults."""
    return {
        "has_blocking_feedback": False,
        "latest_feedback_attempt_id": None,
        "latest_feedback_source_authority_fingerprint": None,
        "latest_curation_attempt_id": None,
        "latest_curation_status": None,
        "latest_curation_candidate_authority_id": None,
        "latest_curation_candidate_authority_fingerprint": None,
        "latest_curation_failure_artifact_id": None,
        "latest_curation_trace_artifact_id": None,
        "latest_curation_last_step": None,
        "latest_curation_last_status": None,
        "curation_available": False,
        "curation_in_progress": False,
    }


def _authority_curation_trace_summary(*, mutation_event_id: int) -> JsonDict:
    """Return fail-open trace summary metadata for an authority curation."""
    try:
        from utils.authority_curation_trace import summarize_trace  # noqa: PLC0415

        return dict(summarize_trace(mutation_event_id=mutation_event_id))
    except (OSError, UnicodeError):
        return {}


def _feedback_curation_for_selection(
    session: Session,
    *,
    project_id: int,
    selection: _AuthoritySelection,
) -> JsonDict:
    """Return feedback/curation metadata for the current review lineage."""
    pending_authority_id = _pending_authority_id(selection.pending_authority)
    rejected_source_authority_id = _rejected_curation_source_authority_id(selection)
    if rejected_source_authority_id is not None:
        rejected_curation = _latest_feedback_and_curation(
            session,
            project_id=project_id,
            authority_id=rejected_source_authority_id,
        )
        if (
            pending_authority_id is None
            or rejected_curation.get("latest_curation_candidate_authority_id")
            == pending_authority_id
        ):
            return rejected_curation
    return _latest_feedback_and_curation(
        session,
        project_id=project_id,
        authority_id=pending_authority_id,
    )


def _pending_authority_id(
    pending_authority: CompiledSpecAuthority | None,
) -> int | None:
    """Return a pending authority row id when present."""
    if pending_authority is not None and pending_authority.authority_id is not None:
        return pending_authority.authority_id
    return None


def _rejected_curation_source_authority_id(
    selection: _AuthoritySelection,
) -> int | None:
    """Return rejected source id only when it belongs to the latest spec."""
    rejected = selection.rejected
    latest_spec = selection.latest_spec
    if (
        rejected is None
        or latest_spec is None
        or rejected.spec_version_id != latest_spec.spec_version_id
    ):
        return None
    return rejected.pending_authority_id


def _latest_feedback_and_curation(
    session: Session,
    *,
    project_id: int,
    authority_id: int | None,
) -> JsonDict:
    """Return bounded feedback and curation status for the authority status view."""
    if authority_id is None:
        return _feedback_curation_defaults()

    feedback = session.exec(
        select(AuthorityFeedbackAttempt)
        .where(AuthorityFeedbackAttempt.project_id == project_id)
        .where(AuthorityFeedbackAttempt.source_authority_id == authority_id)
        .order_by(
            cast("Any", AuthorityFeedbackAttempt.created_at).desc(),
            cast("Any", AuthorityFeedbackAttempt.feedback_row_id).desc(),
        )
    ).first()
    running_curation = session.exec(
        select(AuthorityCurationAttempt)
        .where(AuthorityCurationAttempt.project_id == project_id)
        .where(AuthorityCurationAttempt.source_authority_id == authority_id)
        .where(AuthorityCurationAttempt.status == "running")
        .order_by(
            cast("Any", AuthorityCurationAttempt.created_at).desc(),
            cast("Any", AuthorityCurationAttempt.curation_row_id).desc(),
        )
    ).first()
    curation = running_curation
    if feedback is not None and curation is None:
        curation = session.exec(
            select(AuthorityCurationAttempt)
            .where(AuthorityCurationAttempt.project_id == project_id)
            .where(AuthorityCurationAttempt.source_authority_id == authority_id)
            .where(
                AuthorityCurationAttempt.feedback_attempt_id
                == feedback.feedback_attempt_id
            )
            .order_by(
                cast("Any", AuthorityCurationAttempt.created_at).desc(),
                cast("Any", AuthorityCurationAttempt.curation_row_id).desc(),
            )
        ).first()
    has_blocking = bool(feedback and feedback.has_blocking_feedback)
    curation_status = None if curation is None else curation.status
    curation_in_progress = running_curation is not None
    mutation_event_id = None if curation is None else curation.mutation_event_id
    trace_summary: JsonDict = {}
    if mutation_event_id is not None:
        trace_summary = _authority_curation_trace_summary(
            mutation_event_id=mutation_event_id
        )
    return {
        "has_blocking_feedback": has_blocking,
        "latest_feedback_attempt_id": (
            None if feedback is None else feedback.feedback_attempt_id
        ),
        "latest_feedback_source_authority_fingerprint": (
            None if feedback is None else feedback.source_authority_fingerprint
        ),
        "latest_curation_attempt_id": (
            None if curation is None else curation.curation_attempt_id
        ),
        "latest_curation_status": curation_status,
        "latest_curation_candidate_authority_id": (
            None if curation is None else curation.candidate_authority_id
        ),
        "latest_curation_candidate_authority_fingerprint": (
            None if curation is None else curation.candidate_authority_fingerprint
        ),
        "latest_curation_failure_artifact_id": (
            None if curation is None else curation.failure_artifact_id
        ),
        "latest_curation_trace_artifact_id": (
            None
            if mutation_event_id is None
            else trace_summary.get("trace_artifact_id")
        ),
        "latest_curation_last_step": trace_summary.get("last_trace_step"),
        "latest_curation_last_status": trace_summary.get("last_trace_status"),
        "curation_available": has_blocking
        and not curation_in_progress
        and curation_status != "succeeded",
        "curation_in_progress": curation_in_progress,
    }


def _classify_status(
    *,
    selection: _AuthoritySelection,
    specification_source: JsonDict,
) -> _StatusClassification:
    """Return the current authority status and reason."""
    if (
        selection.rejected is not None
        and selection.latest_spec is not None
        and selection.rejected.spec_version_id == selection.latest_spec.spec_version_id
        and selection.pending_authority is None
        and _rejection_supersedes_acceptance(selection)
    ):
        status = "rejected"
        reason = "latest_authority_rejected"
        stale_reason = None
    elif selection.accepted is None:
        if not selection.specs:
            status = "missing"
            reason = "no_spec_versions"
            stale_reason = None
        else:
            status = "pending_acceptance"
            reason = "spec_versions_without_accepted_authority"
            stale_reason = None
    elif selection.accepted_spec is None or selection.latest_spec is None:
        status = "stale"
        reason = "accepted_spec_missing"
        stale_reason = reason
    elif selection.authority is None:
        status = "not_compiled"
        reason = "accepted_authority_not_compiled"
        stale_reason = None
    elif (
        selection.authority.compiler_version != selection.accepted.compiler_version
        or selection.authority.prompt_hash != selection.accepted.prompt_hash
    ):
        status = "stale"
        reason = "accepted_compiler_prompt_mismatch"
        stale_reason = reason
    elif _normalize_hash(selection.latest_spec.spec_hash) != _normalize_hash(
        selection.accepted.spec_hash
    ):
        status = "stale"
        reason = "latest_spec_hash_mismatch"
        stale_reason = reason
    elif (
        source_classification := _specification_source_stale_classification(
            specification_source
        )
    ) is not None:
        return source_classification
    elif not selection.authority_trusted:
        status = "stale"
        reason = "accepted_authority_identity_mismatch"
        stale_reason = reason
    else:
        status = "current"
        reason = "accepted_authority_current"
        stale_reason = None
    return _StatusClassification(
        status=status,
        reason=reason,
        stale_reason=stale_reason,
    )


def _rejection_supersedes_acceptance(selection: _AuthoritySelection) -> bool:
    """Return whether the latest rejection remains terminal for current status."""
    rejected = selection.rejected
    accepted = selection.accepted
    if rejected is None or accepted is None:
        return True
    if accepted.spec_version_id != rejected.spec_version_id:
        return True
    return _decision_sort_key(rejected) > _decision_sort_key(accepted)


def _decision_sort_key(decision: SpecAuthorityAcceptance) -> tuple[datetime, int]:
    """Return the same stable decision ordering used by latest-decision queries."""
    decided_at = decision.decided_at
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=UTC)
    return decided_at, decision.id or 0


def _specification_source_stale_classification(
    specification_source: JsonDict,
) -> _StatusClassification | None:
    """Return stale classification for missing or invalid canonical source."""
    reason: str | None = None
    if specification_source["status"] == "missing":
        reason = "specification_source_missing"
    elif specification_source["status"] == "invalid":
        reason = "specification_source_invalid"
    elif specification_source["matches_accepted"] is False:
        reason = "specification_source_hash_mismatch"
    if reason is None:
        return None
    return _StatusClassification(status="stale", reason=reason, stale_reason=reason)


def _status_data(context: _StatusContext) -> JsonDict:
    """Build the stable status data payload."""
    selection = context.selection
    accepted = selection.accepted
    rejected = selection.rejected
    authority = selection.authority
    pending_authority = selection.pending_authority
    latest_spec = selection.latest_spec
    pending_invariant_count, _pending_warnings = _invariant_count(pending_authority)
    data = {
        "project_id": context.project_id,
        "status": context.classification.status,
        "reason": context.classification.reason,
        "stale_reason": context.classification.stale_reason,
        "latest_spec_version_id": (
            latest_spec.spec_version_id if latest_spec is not None else None
        ),
        "latest_spec_hash": latest_spec.spec_hash if latest_spec is not None else None,
        "accepted_decision_id": accepted.id if accepted is not None else None,
        "accepted_decided_at": _iso_z(accepted.decided_at) if accepted else None,
        "accepted_spec_version_id": (
            accepted.spec_version_id if accepted is not None else None
        ),
        "accepted_spec_hash": accepted.spec_hash if accepted is not None else None,
        "spec_hash": accepted.spec_hash if accepted is not None else None,
        "latest_rejected_decision_id": rejected.id if rejected is not None else None,
        "latest_rejected_decided_at": _iso_z(rejected.decided_at) if rejected else None,
        "rejected_spec_version_id": (
            rejected.spec_version_id if rejected is not None else None
        ),
        "rejected_pending_authority_id": (
            rejected.pending_authority_id if rejected is not None else None
        ),
        "rejection_reason": rejected.rationale if rejected is not None else None,
        "authority_id": authority.authority_id if authority is not None else None,
        "compiled_spec_version_id": (
            authority.spec_version_id if authority is not None else None
        ),
        "compiled_at": _iso_z(authority.compiled_at) if authority else None,
        "compiler_version": (
            authority.compiler_version if authority is not None else None
        ),
        "prompt_hash": authority.prompt_hash if authority is not None else None,
        "invariant_count": context.invariant_count,
        "pending_authority_id": (
            pending_authority.authority_id if pending_authority is not None else None
        ),
        "pending_compiled_spec_version_id": (
            pending_authority.spec_version_id if pending_authority is not None else None
        ),
        "pending_compiled_at": (
            _iso_z(pending_authority.compiled_at)
            if pending_authority is not None
            else None
        ),
        "pending_compiler_version": (
            pending_authority.compiler_version
            if pending_authority is not None
            else None
        ),
        "pending_prompt_hash": (
            pending_authority.prompt_hash if pending_authority is not None else None
        ),
        "pending_invariant_count": pending_invariant_count,
        "pending_authority_fingerprint": _pending_authority_fingerprint(
            pending_authority
        ),
        "specification_source": context.specification_source,
        "authority_fingerprint": _authority_status_fingerprint(context),
    }
    data.update(context.feedback_curation)
    return data


def _first_unusable_status_authority(
    selection: _AuthoritySelection,
    *,
    project_id: int,
) -> tuple[CompiledSpecAuthority, CompiledAuthorityReadFailure] | None:
    """Return the first unusable authority row relevant to status projection."""
    for authority in (selection.pending_authority, selection.authority):
        if authority is None:
            continue
        load_result = load_compiled_artifact(authority)
        failure = compiled_authority_read_failure(
            load_result,
            project_id=project_id,
            spec_version_id=authority.spec_version_id,
            authority_id=authority.authority_id,
        )
        if failure is not None:
            return authority, failure
    return None


def _project_specs(session: Session, project_id: int) -> list[SpecRegistry]:
    """Return project spec versions newest first."""
    return list(
        session.exec(
            select(SpecRegistry)
            .where(SpecRegistry.project_id == project_id)
            .order_by(cast("Any", SpecRegistry.spec_version_id).desc())
        ).all()
    )


def _latest_accepted(
    session: Session,
    project_id: int,
) -> SpecAuthorityAcceptance | None:
    """Return the latest accepted authority decision for a project."""
    return session.exec(
        select(SpecAuthorityAcceptance)
        .where(
            SpecAuthorityAcceptance.project_id == project_id,
            SpecAuthorityAcceptance.status == "accepted",
        )
        .order_by(
            cast("Any", SpecAuthorityAcceptance.decided_at).desc(),
            cast("Any", SpecAuthorityAcceptance.id).desc(),
        )
    ).first()


def _latest_rejected(
    session: Session,
    project_id: int,
) -> SpecAuthorityAcceptance | None:
    """Return the latest rejected authority decision for a project."""
    return session.exec(
        select(SpecAuthorityAcceptance)
        .where(
            SpecAuthorityAcceptance.project_id == project_id,
            SpecAuthorityAcceptance.status == "rejected",
        )
        .order_by(
            cast("Any", SpecAuthorityAcceptance.decided_at).desc(),
            cast("Any", SpecAuthorityAcceptance.id).desc(),
        )
    ).first()


def _load_authority_selection(
    session: Session,
    *,
    project_id: int,
) -> _AuthoritySelection:
    """Load all read-only rows needed for authority status."""
    specs = _project_specs(session, project_id)
    accepted = _latest_accepted(session, project_id)
    rejected = _latest_rejected(session, project_id)
    accepted_spec = (
        session.get(SpecRegistry, accepted.spec_version_id)
        if accepted is not None
        else None
    )
    authority = (
        compiled_authority_for_acceptance(
            session,
            acceptance=accepted,
        )
        if accepted is not None
        else None
    )
    trusted_authority = (
        accepted_compiled_authority(
            session,
            project_id=project_id,
            spec_version_id=accepted.spec_version_id,
        )
        if accepted is not None
        else None
    )
    latest_spec = specs[0] if specs else None
    pending_authority = _pending_authority(
        session=session,
        latest_spec=latest_spec,
        accepted=accepted,
        rejected=rejected,
    )
    return _AuthoritySelection(
        specs=specs,
        latest_spec=latest_spec,
        accepted=accepted,
        rejected=rejected,
        accepted_spec=accepted_spec,
        authority=authority,
        pending_authority=pending_authority,
        authority_trusted=(
            authority is not None
            and trusted_authority is not None
            and authority.authority_id == trusted_authority.authority_id
        ),
    )


def _pending_authority(
    *,
    session: Session,
    latest_spec: SpecRegistry | None,
    accepted: SpecAuthorityAcceptance | None,
    rejected: SpecAuthorityAcceptance | None,
) -> CompiledSpecAuthority | None:
    """Return the latest compiled authority awaiting acceptance, if any."""
    if latest_spec is None or latest_spec.spec_version_id is None:
        return None
    candidate = latest_compiled_authority(
        session,
        spec_version_id=latest_spec.spec_version_id,
    )
    if candidate is None or candidate.authority_id is None:
        return None
    if (
        accepted is not None
        and accepted.spec_version_id == latest_spec.spec_version_id
        and accepted.pending_authority_id == candidate.authority_id
    ):
        return None
    if (
        rejected is not None
        and rejected.spec_version_id == latest_spec.spec_version_id
        and rejected.pending_authority_id == candidate.authority_id
    ):
        return None
    return candidate


class AuthorityProjectionService:
    """Read-only Spec Authority projection service."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        repo_root: Path | None = None,
    ) -> None:
        """Initialize the projection with a read-only target engine."""
        self._engine = engine or model_db.get_engine()
        _ = repo_root

    def status(self, *, project_id: int) -> JsonDict:
        """Return authority status for a project."""
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                return _project_not_found_error(AUTHORITY_STATUS_COMMAND, project_id)

            selection = _load_authority_selection(session, project_id=project_id)
            specification_source = _resolve_specification_source(
                session=session,
                spec=_status_spec(selection=selection),
                accepted_hash=(
                    selection.accepted.spec_hash
                    if selection.accepted is not None
                    else None
                ),
            )
            data, warnings = _build_status_data(
                session=session,
                project_id=project_id,
                project=project,
                selection=selection,
                specification_source=specification_source,
            )
            unusable = _first_unusable_status_authority(
                selection,
                project_id=project_id,
            )
            if unusable is not None:
                _authority, failure = unusable
                authority_status = (
                    "unsupported_schema"
                    if failure.error_code
                    == ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value
                    else "invalid"
                )
                data.update(
                    {
                        "status": authority_status,
                        "authority_status": authority_status,
                        "current": False,
                        "accepted_current": False,
                        "invariant_count": 0,
                    }
                )
                return _compiled_authority_read_error(
                    command=AUTHORITY_STATUS_COMMAND,
                    failure=failure,
                    data=data,
                    warnings=warnings,
                )
            return _success(data, warnings)

    def invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonDict:
        """Return invariants for accepted or explicitly requested authority."""
        with Session(self._engine) as session:
            return self._invariants_from_session(
                session=session,
                project_id=project_id,
                spec_version_id=spec_version_id,
            )

    def _select_invariants_selection(
        self,
        *,
        session: Session,
        project_id: int,
        spec_version_id: int | None,
    ) -> _InvariantsSelection | JsonDict:
        """Select explicit or latest accepted spec version for invariants."""
        if spec_version_id is not None:
            return _InvariantsSelection(
                spec_version_id=spec_version_id,
                accepted=None,
            )
        accepted = _latest_accepted(session, project_id)
        if accepted is None:
            return _authority_not_accepted_error(project_id)
        return _InvariantsSelection(
            spec_version_id=accepted.spec_version_id,
            accepted=accepted,
        )

    def _invariants_from_session(  # noqa: PLR0911
        self,
        *,
        session: Session,
        project_id: int,
        spec_version_id: int | None,
    ) -> JsonDict:
        """Return invariants using an already opened read-only session."""
        project = session.get(Project, project_id)
        if project is None:
            return _project_not_found_error(
                AUTHORITY_INVARIANTS_COMMAND,
                project_id,
            )

        selection = self._select_invariants_selection(
            session=session,
            project_id=project_id,
            spec_version_id=spec_version_id,
        )
        if isinstance(selection, _InvariantsSelection):
            selected_id = selection.spec_version_id
        else:
            return selection

        spec_version = session.get(SpecRegistry, selected_id)
        if spec_version is None or spec_version.project_id != project_id:
            return _spec_version_not_found_error(project_id, selected_id)

        authority = (
            compiled_authority_for_acceptance(
                session,
                acceptance=selection.accepted,
            )
            if selection.accepted is not None
            else latest_compiled_authority(
                session,
                spec_version_id=selected_id,
            )
        )
        if authority is None:
            return _authority_not_compiled_error(project_id, selected_id)
        if (
            selection.accepted is not None
            and accepted_compiled_authority(
                session,
                project_id=project_id,
                spec_version_id=selected_id,
            )
            is None
        ):
            return _authority_acceptance_mismatch_error(
                project_id=project_id,
                accepted=selection.accepted,
                authority=authority,
            )
        load_result = load_compiled_artifact(authority)
        failure = compiled_authority_read_failure(
            load_result,
            project_id=project_id,
            spec_version_id=authority.spec_version_id,
            authority_id=authority.authority_id,
        )
        if failure is not None:
            authority_status = (
                "unsupported_schema"
                if failure.error_code
                == ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value
                else "invalid"
            )
            return _compiled_authority_read_error(
                command=AUTHORITY_INVARIANTS_COMMAND,
                failure=failure,
                data={
                    "project_id": project_id,
                    "spec_version_id": authority.spec_version_id,
                    "authority_id": authority.authority_id,
                    "authority_status": authority_status,
                    "current": False,
                    "accepted_current": False,
                    "invariants": [],
                    "count": 0,
                    "authority_fingerprint": None,
                },
            )
        return _invariants_success(
            project_id=project_id,
            authority=authority,
            artifact=cast("SpecAuthorityCompilationSuccess", load_result.artifact),
        )


def _status_spec(*, selection: _AuthoritySelection) -> SpecRegistry | None:
    """Return the registry row whose exact candidate source status must inspect."""
    return selection.accepted_spec or selection.latest_spec


def _resolve_specification_source(
    *,
    session: Session,
    spec: SpecRegistry | None,
    accepted_hash: str | None,
) -> JsonDict:
    """Resolve the exact registry candidate and validate its canonical contract."""
    if spec is None:
        return _specification_source_payload(
            spec=None,
            accepted_hash=accepted_hash,
            status="not_available",
        )
    candidate = session.exec(
        select(SpecificationCandidate).where(
            SpecificationCandidate.project_id == spec.project_id,
            SpecificationCandidate.specification_candidate_id
            == spec.source_specification_candidate_id,
            SpecificationCandidate.candidate_fingerprint
            == spec.source_specification_candidate_fingerprint,
            SpecificationCandidate.payload_fingerprint == spec.spec_hash,
        )
    ).one_or_none()
    if candidate is None:
        return _specification_source_payload(
            spec=spec,
            accepted_hash=accepted_hash,
            status="missing",
        )
    try:
        payload, envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
    except (TypeError, ValueError) as exc:
        return _specification_source_payload(
            spec=spec,
            accepted_hash=accepted_hash,
            status="invalid",
            candidate=candidate,
            error=str(exc),
        )
    if not _candidate_matches_registry(
        session=session,
        spec=spec,
        candidate=candidate,
        envelope=envelope,
    ):
        return _specification_source_payload(
            spec=spec,
            accepted_hash=accepted_hash,
            status="invalid",
            candidate=candidate,
            error="registry, candidate, and canonical envelope identities differ",
        )
    return _specification_source_payload(
        spec=spec,
        accepted_hash=accepted_hash,
        status="valid",
        candidate=candidate,
        payload=payload,
        envelope=envelope,
    )


def _candidate_matches_registry(
    *,
    session: Session,
    spec: SpecRegistry,
    candidate: SpecificationCandidate,
    envelope: SpecificationCandidateEnvelope,
) -> bool:
    """Return whether registry, candidate, envelope, and amendment base agree."""
    if not (
        candidate.candidate_kind == envelope.candidate_kind.value
        and candidate.payload_fingerprint
        == spec.spec_hash
        == envelope.payload_fingerprint
        and candidate.source_manifest_fingerprint
        == envelope.source_manifest_fingerprint
        and candidate.producer_input_fingerprint
        == envelope.producer_input_fingerprint
        and candidate.rendered_view_fingerprint
        == envelope.review_view_fingerprint
        and candidate.workflow_node_attempt_id == envelope.workflow_node_attempt_id
        and candidate.attempt_fingerprint == envelope.attempt_fingerprint
        and candidate.vision_artifact_id
        == spec.source_vision_artifact_id
        == envelope.accepted_vision_id
        and candidate.vision_fingerprint
        == spec.source_vision_fingerprint
        == envelope.accepted_vision_fingerprint
        and candidate.product_goal_artifact_id
        == spec.source_product_goal_artifact_id
        == envelope.accepted_product_goal_id
        and candidate.product_goal_fingerprint
        == spec.source_product_goal_fingerprint
        == envelope.accepted_product_goal_fingerprint
        and candidate.base_spec_version_id == envelope.base_specification_id
        and candidate.base_spec_hash == envelope.base_payload_fingerprint
        and spec.supersedes_spec_version_id == envelope.base_specification_id
    ):
        return False
    if envelope.base_specification_id is None:
        return True
    base = session.get(SpecRegistry, envelope.base_specification_id)
    return bool(
        base is not None
        and base.project_id == spec.project_id
        and base.spec_hash == envelope.base_payload_fingerprint
    )


def _specification_source_payload(  # noqa: PLR0913
    *,
    spec: SpecRegistry | None,
    accepted_hash: str | None,
    status: str,
    candidate: SpecificationCandidate | None = None,
    payload: SpecificationPayload | None = None,
    envelope: SpecificationCandidateEnvelope | None = None,
    error: str | None = None,
) -> JsonDict:
    """Return stable canonical specification-source status data."""
    payload_fingerprint = (
        envelope.payload_fingerprint
        if envelope is not None
        else (candidate.payload_fingerprint if candidate is not None else None)
    )
    return {
        "status": status,
        "spec_version_id": None if spec is None else spec.spec_version_id,
        "source_specification_candidate_id": (
            None if spec is None else spec.source_specification_candidate_id
        ),
        "candidate_fingerprint": (
            envelope.candidate_fingerprint
            if envelope is not None
            else (
                candidate.candidate_fingerprint
                if candidate is not None
                else (
                    None
                    if spec is None
                    else spec.source_specification_candidate_fingerprint
                )
            )
        ),
        "payload_fingerprint": payload_fingerprint,
        "source_manifest_fingerprint": (
            None if envelope is None else envelope.source_manifest_fingerprint
        ),
        "review_view_fingerprint": (
            None if envelope is None else envelope.review_view_fingerprint
        ),
        "matches_accepted": (
            _normalize_hash(payload_fingerprint) == _normalize_hash(accepted_hash)
            if payload_fingerprint is not None and accepted_hash is not None
            else None
        ),
        "canonical_payload": (
            None if payload is None else json.loads(canonical_spec_json(payload))
        ),
        "candidate_envelope": (
            None if envelope is None else envelope.model_dump(mode="json")
        ),
        "error": error,
    }


def _normalize_hash(value: str | None) -> str | None:
    """Normalize legacy and prefixed SHA-256 values for comparison."""
    if value is None:
        return None
    stripped = value.strip().lower()
    if stripped.startswith("sha256:"):
        return f"sha256:{stripped.removeprefix('sha256:')}"
    return f"sha256:{stripped}"


def _invariants_success(
    *,
    project_id: int,
    authority: CompiledSpecAuthority,
    artifact: SpecAuthorityCompilationSuccess,
) -> JsonDict:
    """Return invariants rendered from the canonical typed artifact."""
    invariants = [
        {"id": invariant.id, "text": render_invariant_summary(invariant)}
        for invariant in artifact.invariants
    ]
    return _success(
        {
            "project_id": project_id,
            "spec_version_id": authority.spec_version_id,
            "authority_id": authority.authority_id,
            "invariants": invariants,
            "count": len(invariants),
            "authority_fingerprint": canonical_hash(
                {"compiled": _authority_fingerprint_payload(authority)}
            ),
        }
    )
