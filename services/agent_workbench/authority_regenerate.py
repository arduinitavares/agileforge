"""Regenerate compiled authority for an approved spec version."""

# ruff: noqa: SIM300

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session, select

from models.agent_workbench import CliMutationLedger
from models.core import Product
from models.db import get_engine
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.envelope import error_envelope, success_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    IDEMPOTENCY_KEY_REUSED,
    MUTATION_IN_PROGRESS,
    MUTATION_RECOVERY_REQUIRED,
    MUTATION_RESUME_CONFLICT,
    MutationLedgerRepository,
    MutationStatus,
)
from services.specs.compiler_service import (
    COMPILED_AUTHORITY_SCHEMA_VERSION,
    compile_spec_authority_for_version_with_engine,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

AUTHORITY_REGENERATE_COMMAND: str = "agileforge authority regenerate"
AUTHORITY_REGENERATE_LEASE_SECONDS: int = 300
_ACCEPTANCE_STATUS: Any = SpecAuthorityAcceptance.status
_LEDGER_MUTATION_EVENT_ID: Any = CliMutationLedger.mutation_event_id
_LEDGER_STATUS: Any = CliMutationLedger.status
_LEDGER_LEASE_OWNER: Any = CliMutationLedger.lease_owner
_LEDGER_LEASE_EXPIRES_AT: Any = CliMutationLedger.lease_expires_at


class AuthorityRegenerateRequest(BaseModel):
    """CLI request for authority regeneration."""

    project_id: int
    spec_version_id: int
    idempotency_key: str | None = None
    changed_by: str = "cli-agent"
    dry_run: bool = False


@dataclass
class AuthorityRegenerateRunner:
    """Mutation runner for approved-spec authority regeneration."""

    engine: Engine

    @dataclass(frozen=True)
    class _ActiveMutation:
        """Active regeneration mutation with a fenced ledger lease."""

        ledger: MutationLedgerRepository
        lease_owner: str
        mutation_event_id: int

    def regenerate(self, request: AuthorityRegenerateRequest) -> dict[str, Any]:
        """Regenerate authority and stop at pending review."""
        spec_error = self._validate_request(request)
        if spec_error is not None:
            return spec_error
        dry_run_response = self._dry_run_response(request)
        if dry_run_response is not None:
            return dry_run_response

        active_mutation = self._start_mutation(request)
        if not isinstance(active_mutation, AuthorityRegenerateRunner._ActiveMutation):
            return cast("dict[str, Any]", active_mutation)

        compile_result = self._compile_authority(
            request=request,
            active_mutation=active_mutation,
        )
        if compile_result.get("success") is not True:
            return self._compile_failure_response(
                request=request,
                active_mutation=active_mutation,
                compile_result=compile_result,
            )

        authority = self._publish_pending_authority_candidate(request)
        if authority is None or authority.authority_id is None:
            return self._missing_authority_response(
                request=request,
                active_mutation=active_mutation,
                compile_result=compile_result,
            )

        return self._success_response(
            request=request,
            active_mutation=active_mutation,
            authority=authority,
        )

    def _dry_run_response(
        self, request: AuthorityRegenerateRequest
    ) -> dict[str, Any] | None:
        """Return the non-mutating dry-run response when requested."""
        if not request.dry_run:
            return None
        return success_envelope(
            command=AUTHORITY_REGENERATE_COMMAND,
            data={
                "status": "dry_run",
                "would_regenerate": True,
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
                "compiled_authority_schema_version": (
                    COMPILED_AUTHORITY_SCHEMA_VERSION
                ),
            },
        )

    def _start_mutation(
        self, request: AuthorityRegenerateRequest
    ) -> _ActiveMutation | dict[str, Any]:
        """Acquire the mutation lease or return the existing deterministic result."""
        if request.idempotency_key is None:
            return error_envelope(
                command=AUTHORITY_REGENERATE_COMMAND,
                error=workbench_error(
                    ErrorCode.INVALID_COMMAND,
                    message=(
                        "idempotency_key is required for authority regeneration."
                    ),
                    details={
                        "project_id": request.project_id,
                        "spec_version_id": request.spec_version_id,
                    },
                ),
            )

        now = datetime.now(UTC)
        lease_owner = (
            "agileforge-cli:authority-regenerate:"
            f"{request.idempotency_key}:{uuid4()}"
        )
        ledger = MutationLedgerRepository(engine=self.engine)
        loaded = ledger.create_or_load(
            command=AUTHORITY_REGENERATE_COMMAND,
            idempotency_key=request.idempotency_key,
            request_hash=canonical_hash(
                {
                    "command": AUTHORITY_REGENERATE_COMMAND,
                    "project_id": request.project_id,
                    "spec_version_id": request.spec_version_id,
                }
            ),
            project_id=request.project_id,
            correlation_id=str(uuid4()),
            changed_by=request.changed_by,
            lease_owner=lease_owner,
            now=now,
            lease_seconds=AUTHORITY_REGENERATE_LEASE_SECONDS,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _ledger_error_response(
                error_code=loaded.error_code,
                mutation_event_id=loaded.ledger.mutation_event_id,
            )
        return self._ActiveMutation(
            ledger=ledger,
            lease_owner=lease_owner,
            mutation_event_id=_required_mutation_event_id(
                loaded.ledger.mutation_event_id
            ),
        )

    def _compile_authority(
        self,
        *,
        request: AuthorityRegenerateRequest,
        active_mutation: _ActiveMutation,
    ) -> dict[str, Any]:
        """Compile the approved authority with ledger-backed fence callbacks."""

        def lease_guard(_boundary: str) -> bool:
            return active_mutation.ledger.require_active_owner(
                mutation_event_id=active_mutation.mutation_event_id,
                lease_owner=active_mutation.lease_owner,
                now=datetime.now(UTC),
                lease_seconds=AUTHORITY_REGENERATE_LEASE_SECONDS,
            )

        def record_progress(boundary: str) -> bool:
            return active_mutation.ledger.mark_step_complete(
                mutation_event_id=active_mutation.mutation_event_id,
                lease_owner=active_mutation.lease_owner,
                step=boundary,
                next_step=boundary,
                now=datetime.now(UTC),
            )

        return compile_spec_authority_for_version_with_engine(
            engine=self.engine,
            spec_version_id=request.spec_version_id,
            force_recompile=True,
            lease_guard=lease_guard,
            record_progress=record_progress,
        )

    def _compile_failure_response(
        self,
        *,
        request: AuthorityRegenerateRequest,
        active_mutation: _ActiveMutation,
        compile_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the bounded response for an unsuccessful compile attempt."""
        compile_error_code = _compiler_error_code(compile_result)
        if compile_error_code is not None:
            return _ledger_error_response(
                error_code=compile_error_code,
                mutation_event_id=active_mutation.mutation_event_id,
            )
        response = error_envelope(
            command=AUTHORITY_REGENERATE_COMMAND,
            error=workbench_error(
                ErrorCode.SPEC_COMPILE_FAILED,
                message="Authority regeneration failed during compilation.",
                details={
                    "project_id": request.project_id,
                    "spec_version_id": request.spec_version_id,
                    "compile_result": compile_result,
                },
                remediation=["Fix the compile failure, then rerun regenerate."],
            ),
        )
        return self._finalize_failure_response(
            active_mutation=active_mutation,
            response=response,
        )

    def _missing_authority_response(
        self,
        *,
        request: AuthorityRegenerateRequest,
        active_mutation: _ActiveMutation,
        compile_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the bounded response when compile did not persist authority."""
        response = error_envelope(
            command=AUTHORITY_REGENERATE_COMMAND,
            error=workbench_error(
                ErrorCode.MUTATION_FAILED,
                message=(
                    "Authority regeneration compiled successfully but did not "
                    "persist a compiled authority row."
                ),
                details={
                    "project_id": request.project_id,
                    "spec_version_id": request.spec_version_id,
                    "compile_result": compile_result,
                },
            ),
        )
        return self._finalize_failure_response(
            active_mutation=active_mutation,
            response=response,
        )

    def _finalize_failure_response(
        self,
        *,
        active_mutation: _ActiveMutation,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        """Fence a terminal failure or surface a truthful mutation conflict."""
        finalized = _finalize_mutation_status(
            engine=self.engine,
            mutation_event_id=active_mutation.mutation_event_id,
            lease_owner=active_mutation.lease_owner,
            status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
            response=response,
        )
        if finalized:
            return response
        return _ledger_error_response(
            error_code=MUTATION_RESUME_CONFLICT,
            mutation_event_id=active_mutation.mutation_event_id,
        )

    def _success_response(
        self,
        *,
        request: AuthorityRegenerateRequest,
        active_mutation: _ActiveMutation,
        authority: CompiledSpecAuthority,
    ) -> dict[str, Any]:
        """Fence the successful regenerate result or surface a mutation conflict."""
        response = success_envelope(
            command=AUTHORITY_REGENERATE_COMMAND,
            data={
                "status": "authority_pending_review",
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
                "authority_id": authority.authority_id,
                "pending_authority_id": authority.authority_id,
                "mutation_event_id": active_mutation.mutation_event_id,
                "compiled_authority_schema_version": (
                    COMPILED_AUTHORITY_SCHEMA_VERSION
                ),
                "compiler_version": authority.compiler_version,
                "authority_fingerprint": pending_authority_fingerprint(authority),
                "accepted_authority_id": None,
                "next_actions": [
                    {
                        "command": "agileforge authority review",
                        "args": {
                            "project_id": request.project_id,
                            "open": True,
                        },
                        "reason": (
                            "Review regenerated compiled authority before acceptance."
                        ),
                    }
                ],
            },
        )
        finalized = active_mutation.ledger.finalize_success(
            mutation_event_id=active_mutation.mutation_event_id,
            lease_owner=active_mutation.lease_owner,
            after={
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
                "authority_id": authority.authority_id,
                "compiled_authority_schema_version": (
                    COMPILED_AUTHORITY_SCHEMA_VERSION
                ),
            },
            response=response,
            now=datetime.now(UTC),
        )
        if finalized:
            return response
        return _ledger_error_response(
            error_code=MUTATION_RESUME_CONFLICT,
            mutation_event_id=active_mutation.mutation_event_id,
        )

    def _validate_request(
        self, request: AuthorityRegenerateRequest
    ) -> dict[str, Any] | None:
        """Validate project/spec ownership and approval guards."""
        with Session(self.engine) as session:
            spec_version = session.get(SpecRegistry, request.spec_version_id)
            if (
                spec_version is None
                or spec_version.product_id != request.project_id
            ):
                return error_envelope(
                    command=AUTHORITY_REGENERATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.SPEC_VERSION_NOT_FOUND,
                        message=(
                            f"Spec version {request.spec_version_id} was not found "
                            f"for project {request.project_id}."
                        ),
                        details={
                            "project_id": request.project_id,
                            "spec_version_id": request.spec_version_id,
                        },
                    ),
                )
            if spec_version.status != "approved":
                return error_envelope(
                    command=AUTHORITY_REGENERATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_REVIEW_REQUIRED,
                        message=(
                            "Authority can only be regenerated for an approved spec."
                        ),
                        details={
                            "project_id": request.project_id,
                            "spec_version_id": request.spec_version_id,
                            "spec_status": spec_version.status,
                        },
                        remediation=["Approve the spec before regenerating authority."],
                    ),
                )
        return None

    def _publish_pending_authority_candidate(
        self, request: AuthorityRegenerateRequest
    ) -> CompiledSpecAuthority | None:
        """Return a pending authority candidate not bound to a terminal decision."""
        with Session(self.engine) as session:
            authority = _latest_compiled_authority(
                session,
                spec_version_id=request.spec_version_id,
            )
            if authority is None or authority.authority_id is None:
                return None
            if not _has_terminal_decision_for_authority(
                session=session,
                request=request,
                authority_id=authority.authority_id,
            ):
                return authority
            clone = CompiledSpecAuthority(
                spec_version_id=authority.spec_version_id,
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                compiled_at=datetime.now(UTC),
                compiled_artifact_json=authority.compiled_artifact_json,
                scope_themes=authority.scope_themes,
                invariants=authority.invariants,
                eligible_feature_ids=authority.eligible_feature_ids,
                rejected_features=authority.rejected_features,
                spec_gaps=authority.spec_gaps,
            )
            session.add(clone)
            product = session.get(Product, request.project_id)
            if product is not None:
                product.compiled_authority_json = clone.compiled_artifact_json
                session.add(product)
            session.commit()
            session.refresh(clone)
            return clone


def _latest_compiled_authority(
    session: Session,
    *,
    spec_version_id: int,
) -> CompiledSpecAuthority | None:
    """Return the newest compiled authority candidate for a spec version."""
    return session.exec(
        select(CompiledSpecAuthority)
        .where(CompiledSpecAuthority.spec_version_id == spec_version_id)
        .order_by(cast("Any", CompiledSpecAuthority.authority_id).desc())
    ).first()


def _has_terminal_decision_for_authority(
    *,
    session: Session,
    request: AuthorityRegenerateRequest,
    authority_id: int,
) -> bool:
    """Return whether a compiled authority id was already accepted or rejected."""
    return (
        session.exec(
            select(SpecAuthorityAcceptance)
            .where(SpecAuthorityAcceptance.product_id == request.project_id)
            .where(
                SpecAuthorityAcceptance.spec_version_id
                == request.spec_version_id
            )
            .where(SpecAuthorityAcceptance.pending_authority_id == authority_id)
            .where(_ACCEPTANCE_STATUS.in_(("accepted", "rejected")))
        ).first()
        is not None
    )


def _ledger_error_response(
    *,
    error_code: str,
    mutation_event_id: int | None,
) -> dict[str, Any]:
    """Map ledger startup errors into command envelopes."""
    mapped_code = {
        IDEMPOTENCY_KEY_REUSED: ErrorCode.IDEMPOTENCY_KEY_REUSED,
        MUTATION_IN_PROGRESS: ErrorCode.MUTATION_IN_PROGRESS,
        MUTATION_RECOVERY_REQUIRED: ErrorCode.MUTATION_RECOVERY_REQUIRED,
        MUTATION_RESUME_CONFLICT: ErrorCode.MUTATION_RESUME_CONFLICT,
    }.get(error_code, ErrorCode.MUTATION_FAILED)
    return error_envelope(
        command=AUTHORITY_REGENERATE_COMMAND,
        error=workbench_error(
            mapped_code,
            message="Authority regeneration mutation cannot start.",
            details={"mutation_event_id": mutation_event_id},
            remediation=["Inspect the mutation ledger before retrying regenerate."],
        ),
    )


def _compiler_error_code(compile_result: dict[str, Any]) -> str | None:
    """Return a ledger-style compiler error code when present."""
    error_code = compile_result.get("error_code")
    if isinstance(error_code, str) and error_code in {
        IDEMPOTENCY_KEY_REUSED,
        MUTATION_IN_PROGRESS,
        MUTATION_RECOVERY_REQUIRED,
        MUTATION_RESUME_CONFLICT,
    }:
        return error_code
    return None


def _required_mutation_event_id(mutation_event_id: int | None) -> int:
    """Require a persisted mutation event identifier."""
    if mutation_event_id is None:
        message = "Mutation event id was not persisted."
        raise RuntimeError(message)
    return mutation_event_id


def _finalize_mutation_status(
    *,
    engine: Engine,
    mutation_event_id: int | None,
    lease_owner: str,
    status: MutationStatus,
    response: dict[str, Any],
) -> bool:
    """Persist a terminal non-success ledger response while the lease is active."""
    if mutation_event_id is None:
        return False
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as session:
        result = session.exec(
            update(CliMutationLedger)
            .where(_LEDGER_MUTATION_EVENT_ID == mutation_event_id)
            .where(_LEDGER_STATUS == MutationStatus.PENDING.value)
            .where(_LEDGER_LEASE_OWNER == lease_owner)
            .where(_LEDGER_LEASE_EXPIRES_AT > now)
            .values(
                status=status.value,
                response_json=json.dumps(
                    response,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                lease_owner=None,
                lease_acquired_at=None,
                last_heartbeat_at=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def default_authority_regenerate_runner() -> AuthorityRegenerateRunner:
    """Build the default authority regenerate runner."""
    return AuthorityRegenerateRunner(engine=get_engine())
