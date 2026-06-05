"""Regenerate compiled authority for an approved spec version."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session, select

from models.agent_workbench import CliMutationLedger
from models.db import get_engine
from models.specs import CompiledSpecAuthority, SpecRegistry
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

    def regenerate(self, request: AuthorityRegenerateRequest) -> dict[str, Any]:
        """Regenerate authority and stop at pending review."""
        spec_error = self._validate_request(request)
        if spec_error is not None:
            return spec_error

        if request.dry_run:
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
            lease_seconds=300,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _ledger_error_response(
                error_code=loaded.error_code,
                mutation_event_id=loaded.ledger.mutation_event_id,
            )

        compile_result = compile_spec_authority_for_version_with_engine(
            engine=self.engine,
            spec_version_id=request.spec_version_id,
            force_recompile=True,
        )
        if compile_result.get("success") is not True:
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
            _finalize_mutation_status(
                engine=self.engine,
                mutation_event_id=loaded.ledger.mutation_event_id,
                lease_owner=lease_owner,
                status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
                response=response,
            )
            return response

        authority = self._load_compiled_authority(request.spec_version_id)
        if authority is None or authority.authority_id is None:
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
            _finalize_mutation_status(
                engine=self.engine,
                mutation_event_id=loaded.ledger.mutation_event_id,
                lease_owner=lease_owner,
                status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS,
                response=response,
            )
            return response

        response = success_envelope(
            command=AUTHORITY_REGENERATE_COMMAND,
            data={
                "status": "authority_pending_review",
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
                "authority_id": authority.authority_id,
                "pending_authority_id": authority.authority_id,
                "mutation_event_id": loaded.ledger.mutation_event_id,
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
        finalized = ledger.finalize_success(
            mutation_event_id=loaded.ledger.mutation_event_id,
            lease_owner=lease_owner,
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
        if not finalized:
            return _ledger_error_response(
                error_code=MUTATION_RESUME_CONFLICT,
                mutation_event_id=loaded.ledger.mutation_event_id,
            )
        return response

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

    def _load_compiled_authority(
        self, spec_version_id: int
    ) -> CompiledSpecAuthority | None:
        """Load the persisted compiled authority for a spec version."""
        with Session(self.engine) as session:
            return session.exec(
                select(CompiledSpecAuthority).where(
                    CompiledSpecAuthority.spec_version_id == spec_version_id
                )
            ).first()


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


def _finalize_mutation_status(
    *,
    engine: Engine,
    mutation_event_id: int | None,
    lease_owner: str,
    status: MutationStatus,
    response: dict[str, Any],
) -> None:
    """Persist a terminal non-success ledger response while the lease is active."""
    if mutation_event_id is None:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as session:
        session.exec(
            update(CliMutationLedger)
            .where(CliMutationLedger.mutation_event_id == mutation_event_id)
            .where(CliMutationLedger.status == MutationStatus.PENDING.value)
            .where(CliMutationLedger.lease_owner == lease_owner)
            .where(CliMutationLedger.lease_expires_at > now)
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


def default_authority_regenerate_runner() -> AuthorityRegenerateRunner:
    """Build the default authority regenerate runner."""
    return AuthorityRegenerateRunner(engine=get_engine())
