"""Read-only Spec Authority projections for the agent workbench."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from sqlmodel import Session, select

from models import db as model_db
from models.core import Product
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
from services.agent_workbench.schema_readiness import (
    SchemaReadiness,
    SchemaRequirement,
    check_schema_readiness,
)
from services.specs.compiler_service import (
    compiled_authority_schema_unsupported_details,
    compiled_authority_schema_unsupported_remediation,
    load_compiled_artifact,
)
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

JsonDict = dict[str, Any]

AUTHORITY_STATUS_COMMAND: Final[str] = "agileforge authority status"
AUTHORITY_INVARIANTS_COMMAND: Final[str] = "agileforge authority invariants"

_AUTHORITY_REQUIREMENTS: Final[tuple[SchemaRequirement, ...]] = (
    SchemaRequirement(
        "products",
        ("product_id", "name", "spec_file_path", "updated_at"),
    ),
    SchemaRequirement(
        "spec_registry",
        (
            "spec_version_id",
            "product_id",
            "spec_hash",
            "content",
            "content_ref",
            "status",
            "created_at",
            "approved_at",
        ),
    ),
    SchemaRequirement(
        "compiled_spec_authority",
        (
            "authority_id",
            "spec_version_id",
            "compiler_version",
            "prompt_hash",
            "compiled_at",
            "compiled_artifact_json",
            "scope_themes",
            "invariants",
            "eligible_feature_ids",
            "rejected_features",
            "spec_gaps",
        ),
    ),
    SchemaRequirement(
        "spec_authority_acceptance",
        (
            "id",
            "product_id",
            "spec_version_id",
            "status",
            "policy",
            "decided_by",
            "decided_at",
            "compiler_version",
            "prompt_hash",
            "spec_hash",
        ),
    ),
)


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
    product: Product
    selection: _AuthoritySelection
    disk_spec: JsonDict
    classification: _StatusClassification
    invariant_count: int


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


def _schema_error(command: str, readiness: SchemaReadiness) -> JsonDict:
    """Return the stable schema-not-ready error envelope."""
    return error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.SCHEMA_NOT_READY,
            message=(
                "Database schema is missing required tables or columns for this "
                "read-only command."
            ),
            details={"missing": readiness.missing},
            remediation=[
                "Run the application startup or migration command before using the CLI."
            ],
        ),
    )


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


def _unsupported_compiled_authority_error(
    *,
    command: str,
    project_id: int,
    spec_version_id: int | None,
    authority_id: int | None,
    observed_schema_version: str | None,
    data: JsonDict,
) -> JsonDict:
    """Return a structured unsupported-artifact error with stable data."""
    envelope = error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED,
            details=compiled_authority_schema_unsupported_details(
                project_id=project_id,
                spec_version_id=spec_version_id,
                observed_schema_version=observed_schema_version,
            )
            | {"authority_id": authority_id},
            remediation=compiled_authority_schema_unsupported_remediation(
                project_id=project_id,
                spec_version_id=spec_version_id,
            ),
        ),
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


def _invalid_invariants_error(
    *,
    authority: CompiledSpecAuthority,
    reason: str,
) -> JsonDict:
    """Return a structured invalid-invariants JSON error."""
    return error_envelope(
        command=AUTHORITY_INVARIANTS_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_INVARIANTS_INVALID,
            message="Compiled authority invariants JSON is invalid.",
            details={
                "authority_id": authority.authority_id,
                "spec_version_id": authority.spec_version_id,
                "reason": reason,
            },
            remediation=["Inspect the compiled authority row and regenerate it."],
        ),
    )


def _invalid_invariants_warning(
    *,
    authority: CompiledSpecAuthority,
    reason: str,
) -> WorkbenchWarning:
    """Return a status warning for malformed invariants JSON."""
    return WorkbenchWarning(
        code="AUTHORITY_INVARIANTS_INVALID",
        message="Compiled authority invariants JSON could not be parsed.",
        details={
            "authority_id": authority.authority_id,
            "spec_version_id": authority.spec_version_id,
            "reason": reason,
        },
        remediation=["Inspect the compiled authority row and regenerate it."],
    )


def _disk_spec_warning(disk_spec: JsonDict) -> WorkbenchWarning | None:
    """Return a structured warning for missing or unreadable disk specs."""
    status = disk_spec["status"]
    if status == "missing":
        return WorkbenchWarning(
            code="DISK_SPEC_MISSING",
            message="Stored specification path could not be found on disk.",
            details={
                "path": disk_spec["path"],
                "resolved_path": disk_spec["resolved_path"],
            },
            remediation=["Restore the specification file or update the stored path."],
        )
    if status == "unreadable":
        return WorkbenchWarning(
            code="DISK_SPEC_UNREADABLE",
            message="Stored specification path could not be read.",
            details={
                "path": disk_spec["path"],
                "resolved_path": disk_spec["resolved_path"],
                "error": disk_spec["error"],
            },
            remediation=[
                "Check file permissions or update the stored specification path."
            ],
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


def _parse_invariants(raw: str | None) -> tuple[list[Any] | None, str | None]:
    """Parse invariants JSON as a list, returning a structured failure reason."""
    if not raw:
        return [], None
    try:
        parsed = json.loads(raw)
    except JSONDecodeError as exc:
        return None, str(exc)
    if not isinstance(parsed, list):
        return None, f"expected list, got {type(parsed).__name__}"
    return parsed, None


def _invariant_count(
    authority: CompiledSpecAuthority | None,
) -> tuple[int, list[WorkbenchWarning]]:
    """Return invariant count plus warnings without raising on malformed JSON."""
    if authority is None:
        return 0, []
    invariants, reason = _parse_invariants(authority.invariants)
    if reason is not None:
        return 0, [_invalid_invariants_warning(authority=authority, reason=reason)]
    return len(invariants or []), []


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
        "scope_themes": _json_field_for_fingerprint(authority.scope_themes),
        "invariants": _json_field_for_fingerprint(authority.invariants),
        "eligible_feature_ids": _json_field_for_fingerprint(
            authority.eligible_feature_ids
        ),
        "rejected_features": _json_field_for_fingerprint(authority.rejected_features),
        "spec_gaps": _json_field_for_fingerprint(authority.spec_gaps),
    }


def _accepted_fingerprint_payload(accepted: SpecAuthorityAcceptance) -> JsonDict:
    """Return deterministic acceptance fields for fingerprinting."""
    return {
        "id": accepted.id,
        "product_id": accepted.product_id,
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
            "product": {
                "product_id": context.product.product_id,
                "updated_at": context.product.updated_at,
            },
            "status": context.classification.status,
            "reason": context.classification.reason,
            "stale_reason": context.classification.stale_reason,
            "latest_spec": _spec_fingerprint_payload(selection.latest_spec),
            "accepted": _accepted_fingerprint_payload(accepted),
            "compiled": _authority_fingerprint_payload(authority),
            "disk_spec": context.disk_spec,
            "invariant_count": context.invariant_count,
        }
    )


def _pending_authority_fingerprint(
    authority: CompiledSpecAuthority | None,
) -> str | None:
    """Return a stable fingerprint for a pending compiled authority."""
    if authority is None:
        return None
    return canonical_hash(
        {
            "command": AUTHORITY_STATUS_COMMAND,
            "pending_compiled": _authority_fingerprint_payload(authority),
        }
    )


def pending_authority_fingerprint(
    authority: CompiledSpecAuthority | None,
) -> str | None:
    """Return the stable public fingerprint for a pending compiled authority."""
    return _pending_authority_fingerprint(authority)


def _spec_fingerprint_payload(spec: SpecRegistry | None) -> JsonDict | None:
    """Return deterministic spec fields for status fingerprinting."""
    if spec is None:
        return None
    return {
        "spec_version_id": spec.spec_version_id,
        "product_id": spec.product_id,
        "spec_hash": spec.spec_hash,
        "status": spec.status,
        "content_ref": spec.content_ref,
        "created_at": spec.created_at,
        "approved_at": spec.approved_at,
    }


def _resolve_status(
    *,
    project_id: int,
    product: Product,
    selection: _AuthoritySelection,
    disk_spec: JsonDict,
) -> JsonDict:
    """Classify status according to accepted, compiled, latest, and disk state."""
    classification = _classify_status(selection=selection, disk_spec=disk_spec)
    invariant_count, warnings = _invariant_count(selection.authority)
    disk_warning = _disk_spec_warning(disk_spec)
    if disk_warning is not None:
        warnings.append(disk_warning)
    context = _StatusContext(
        project_id=project_id,
        product=product,
        selection=selection,
        disk_spec=disk_spec,
        classification=classification,
        invariant_count=invariant_count,
    )
    data = _status_data(context)
    return _success(data, warnings)


def _classify_status(
    *,
    selection: _AuthoritySelection,
    disk_spec: JsonDict,
) -> _StatusClassification:
    """Return the current authority status and reason."""
    if (
        selection.rejected is not None
        and selection.latest_spec is not None
        and selection.rejected.spec_version_id == selection.latest_spec.spec_version_id
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
    elif (disk_classification := _disk_stale_classification(disk_spec)) is not None:
        return disk_classification
    else:
        status = "current"
        reason = "accepted_authority_current"
        stale_reason = None
    return _StatusClassification(
        status=status,
        reason=reason,
        stale_reason=stale_reason,
    )


def _disk_stale_classification(disk_spec: JsonDict) -> _StatusClassification | None:
    """Return disk-spec stale classification when disk state invalidates authority."""
    reason: str | None = None
    if disk_spec["status"] == "missing":
        reason = "disk_spec_missing"
    elif disk_spec["status"] == "unreadable":
        reason = "disk_spec_unreadable"
    elif disk_spec["matches_accepted"] is False:
        reason = "disk_spec_hash_mismatch"
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
    return {
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
            pending_authority.authority_id
            if pending_authority is not None
            else None
        ),
        "pending_compiled_spec_version_id": (
            pending_authority.spec_version_id
            if pending_authority is not None
            else None
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
            pending_authority.prompt_hash
            if pending_authority is not None
            else None
        ),
        "pending_invariant_count": pending_invariant_count,
        "pending_authority_fingerprint": _pending_authority_fingerprint(
            pending_authority
        ),
        "disk_spec": context.disk_spec,
        "authority_fingerprint": _authority_status_fingerprint(context),
    }


def _project_specs(session: Session, project_id: int) -> list[SpecRegistry]:
    """Return project spec versions newest first."""
    return list(
        session.exec(
            select(SpecRegistry)
            .where(SpecRegistry.product_id == project_id)
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
            SpecAuthorityAcceptance.product_id == project_id,
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
            SpecAuthorityAcceptance.product_id == project_id,
            SpecAuthorityAcceptance.status == "rejected",
        )
        .order_by(
            cast("Any", SpecAuthorityAcceptance.decided_at).desc(),
            cast("Any", SpecAuthorityAcceptance.id).desc(),
        )
    ).first()


def _compiled_authority(
    session: Session,
    spec_version_id: int,
) -> CompiledSpecAuthority | None:
    """Return compiled authority for a spec version."""
    return session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()


def _authority_matches_acceptance(
    *,
    authority: CompiledSpecAuthority,
    accepted: SpecAuthorityAcceptance,
) -> bool:
    """Return whether compiled authority provenance matches the acceptance."""
    return (
        authority.compiler_version == accepted.compiler_version
        and authority.prompt_hash == accepted.prompt_hash
    )


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
        _compiled_authority(session, accepted.spec_version_id)
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
    if accepted is not None and accepted.spec_version_id == latest_spec.spec_version_id:
        return None
    if rejected is not None and rejected.spec_version_id == latest_spec.spec_version_id:
        return None
    return _compiled_authority(session, latest_spec.spec_version_id)


class AuthorityProjectionService:
    """Read-only Spec Authority projection service."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        repo_root: Path | None = None,
    ) -> None:
        """Initialize the projection with a read-only target engine and repo root."""
        self._engine = engine or model_db.get_engine()
        self._repo_root = repo_root or Path(__file__).resolve().parents[2]

    def status(self, *, project_id: int) -> JsonDict:
        """Return authority status for a project."""
        schema_error = self._check_schema(AUTHORITY_STATUS_COMMAND)
        if schema_error is not None:
            return schema_error

        with Session(self._engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                return _project_not_found_error(AUTHORITY_STATUS_COMMAND, project_id)

            selection = _load_authority_selection(session, project_id=project_id)
            status_authority = selection.authority or selection.pending_authority
            if status_authority is not None:
                load_result = load_compiled_artifact(status_authority)
                if load_result.unsupported:
                    return _unsupported_compiled_authority_error(
                        command=AUTHORITY_STATUS_COMMAND,
                        project_id=project_id,
                        spec_version_id=status_authority.spec_version_id,
                        authority_id=status_authority.authority_id,
                        observed_schema_version=load_result.observed_schema_version,
                        data={
                            "project_id": project_id,
                            "authority_status": "unsupported_schema",
                            "status": "unsupported_schema",
                            "current": False,
                            "accepted_current": False,
                            "authority_id": status_authority.authority_id,
                            "accepted_spec_version_id": (
                                selection.accepted.spec_version_id
                                if selection.accepted is not None
                                else None
                            ),
                            "spec_version_id": status_authority.spec_version_id,
                        },
                    )
            disk_spec = self._resolve_spec_path(
                _status_spec_path(product=product, selection=selection),
                accepted_hash=(
                    selection.accepted.spec_hash
                    if selection.accepted is not None
                    else None
                ),
            )
            return _resolve_status(
                project_id=project_id,
                product=product,
                selection=selection,
                disk_spec=disk_spec,
            )

    def invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonDict:
        """Return invariants for accepted or explicitly requested authority."""
        schema_error = self._check_schema(AUTHORITY_INVARIANTS_COMMAND)
        if schema_error is not None:
            return schema_error

        with Session(self._engine) as session:
            return self._invariants_from_session(
                session=session,
                project_id=project_id,
                spec_version_id=spec_version_id,
            )

    def _check_schema(self, command: str) -> JsonDict | None:
        """Return a schema error envelope when required tables are absent."""
        readiness = check_schema_readiness(self._engine, _AUTHORITY_REQUIREMENTS)
        if readiness.ok:
            return None
        return _schema_error(command, readiness)

    def _resolve_spec_path(
        self,
        value: str | None,
        *,
        accepted_hash: str | None,
    ) -> JsonDict:
        """Resolve and hash a disk spec path relative to the repository root."""
        if not value:
            return _disk_spec_payload(
                value,
                None,
                None,
                accepted_hash,
            )

        path = Path(value)
        candidate = path if path.is_absolute() else self._repo_root / path
        resolved = candidate.resolve()
        if not resolved.is_file():
            return _disk_spec_payload(
                value,
                resolved,
                None,
                accepted_hash,
                status="missing",
            )

        try:
            raw_bytes = resolved.read_bytes()
        except OSError as exc:
            payload = _disk_spec_payload(
                value,
                resolved,
                None,
                accepted_hash,
                status="unreadable",
            )
            payload["error"] = str(exc)
            return payload
        try:
            raw_content = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            payload = _disk_spec_payload(
                value,
                resolved,
                None,
                accepted_hash,
                status="unreadable",
            )
            payload["error"] = str(exc)
            return payload
        raw_digest = _legacy_sha256(raw_bytes)
        try:
            normalized = normalize_spec_content_for_registry(raw_content)
        except SpecContentNormalizationError as exc:
            payload = _disk_spec_payload(
                value,
                resolved,
                None,
                accepted_hash,
                status="unreadable",
            )
            payload["error"] = str(exc)
            return payload
        return _disk_spec_payload(
            value,
            resolved,
            _display_disk_hash(
                raw_digest=raw_digest,
                normalized_hash=normalized.spec_hash,
            ),
            accepted_hash,
            status="readable",
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

    def _invariants_from_session(
        self,
        *,
        session: Session,
        project_id: int,
        spec_version_id: int | None,
    ) -> JsonDict:
        """Return invariants using an already opened read-only session."""
        product = session.get(Product, project_id)
        if product is None:
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
        if spec_version is None or spec_version.product_id != project_id:
            return _spec_version_not_found_error(project_id, selected_id)

        authority = _compiled_authority(session, selected_id)
        if authority is None:
            return _authority_not_compiled_error(project_id, selected_id)
        load_result = load_compiled_artifact(authority)
        if load_result.unsupported:
            return _unsupported_compiled_authority_error(
                command=AUTHORITY_INVARIANTS_COMMAND,
                project_id=project_id,
                spec_version_id=authority.spec_version_id,
                authority_id=authority.authority_id,
                observed_schema_version=load_result.observed_schema_version,
                data={
                    "project_id": project_id,
                    "spec_version_id": authority.spec_version_id,
                    "authority_id": authority.authority_id,
                    "authority_status": "unsupported_schema",
                    "current": False,
                    "accepted_current": False,
                    "invariants": [],
                    "count": 0,
                    "authority_fingerprint": None,
                },
            )
        if selection.accepted is not None and not _authority_matches_acceptance(
            authority=authority,
            accepted=selection.accepted,
        ):
            return _authority_acceptance_mismatch_error(
                project_id=project_id,
                accepted=selection.accepted,
                authority=authority,
            )

        return _invariants_success(project_id=project_id, authority=authority)


def _status_spec_path(
    *,
    product: Product,
    selection: _AuthoritySelection,
) -> str | None:
    """Return the disk path to inspect for status drift."""
    if product.spec_file_path:
        return product.spec_file_path
    if selection.accepted_spec is not None and selection.accepted_spec.content_ref:
        return selection.accepted_spec.content_ref
    if selection.latest_spec is not None:
        return selection.latest_spec.content_ref
    return None


def _disk_spec_payload(
    raw_path: str | None,
    resolved: Path | None,
    digest: str | None,
    accepted_hash: str | None,
    *,
    status: str = "not_configured",
) -> JsonDict:
    """Return a stable disk spec hash payload."""
    exists = status in {"readable", "unreadable"}
    return {
        "path": raw_path,
        "resolved_path": str(resolved) if resolved is not None else None,
        "exists": exists,
        "status": status,
        "sha256": digest,
        "matches_accepted": (
            _normalize_hash(digest) == _normalize_hash(accepted_hash)
            if digest is not None and accepted_hash
            else None
        ),
        "error": None,
    }


def _normalize_hash(value: str | None) -> str | None:
    """Normalize legacy and prefixed SHA-256 values for comparison."""
    if value is None:
        return None
    stripped = value.strip().lower()
    if stripped.startswith("sha256:"):
        return f"sha256:{stripped.removeprefix('sha256:')}"
    return f"sha256:{stripped}"


def _legacy_sha256(data: bytes) -> str:
    """Return the legacy unprefixed disk SHA-256 digest."""
    return hashlib.sha256(data).hexdigest()


def _display_disk_hash(*, raw_digest: str, normalized_hash: str) -> str:
    """Preserve legacy disk hash shape unless structured canonicalization differs."""
    if _normalize_hash(raw_digest) == _normalize_hash(normalized_hash):
        return raw_digest
    return _normalize_hash(normalized_hash) or normalized_hash


def _invariants_success(
    *,
    project_id: int,
    authority: CompiledSpecAuthority,
) -> JsonDict:
    """Return parsed invariants for a compiled authority."""
    invariants, reason = _parse_invariants(authority.invariants)
    if reason is not None or invariants is None:
        return _invalid_invariants_error(
            authority=authority,
            reason=reason or "unknown parse error",
        )

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
