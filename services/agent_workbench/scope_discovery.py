"""Scope Discovery artifact recording services."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from models.agent_workbench import (
    DiscoveryChallengeArtifact,
    DiscoveryPrd,
    DiscoverySpecAmendmentDraft,
    GreenfieldDiscoveryChallengeArtifact,
    GreenfieldDiscoveryContext,
    GreenfieldDiscoveryPrd,
    GreenfieldDiscoverySpecAmendmentDraft,
)
from models.core import Product
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash, canonical_json
from services.agent_workbench.scope_extension import (
    ScopeExtensionRunner,
    ScopeExtensionValidateRequest,
    load_structured_spec_file,
)

CHALLENGE_RECORD_COMMAND: str = "agileforge discovery challenge record"
PRD_DRAFT_RECORD_COMMAND: str = "agileforge discovery prd draft record"
PRD_ACCEPT_COMMAND: str = "agileforge discovery prd accept"
PRD_REJECT_COMMAND: str = "agileforge discovery prd reject"
SPEC_AMENDMENT_DRAFT_RECORD_COMMAND: str = (
    "agileforge discovery spec-amendment draft record"
)
SPEC_AMENDMENT_ACCEPT_COMMAND: str = "agileforge discovery spec-amendment accept"
SPEC_AMENDMENT_REJECT_COMMAND: str = "agileforge discovery spec-amendment reject"
GREENFIELD_CHALLENGE_RECORD_COMMAND: str = (
    "agileforge discovery greenfield challenge record"
)
GREENFIELD_PRD_DRAFT_RECORD_COMMAND: str = (
    "agileforge discovery greenfield prd draft record"
)
GREENFIELD_PRD_ACCEPT_COMMAND: str = "agileforge discovery greenfield prd accept"
GREENFIELD_PRD_REJECT_COMMAND: str = "agileforge discovery greenfield prd reject"
GREENFIELD_SPEC_AMENDMENT_DRAFT_RECORD_COMMAND: str = (
    "agileforge discovery greenfield spec-amendment draft record"
)
GREENFIELD_SPEC_AMENDMENT_ACCEPT_COMMAND: str = (
    "agileforge discovery greenfield spec-amendment accept"
)
GREENFIELD_SPEC_AMENDMENT_REJECT_COMMAND: str = (
    "agileforge discovery greenfield spec-amendment reject"
)
CHALLENGE_PRODUCER: str = "grill-with-docs"
PRD_PRODUCER: str = "to-prd"
PRD_STATUS_DRAFT: str = "draft"
PRD_STATUS_ACCEPTED: str = "accepted"
PRD_STATUS_REJECTED: str = "rejected"
SPEC_AMENDMENT_DRAFT_READY: str = "ready_for_amendment_acceptance"
SPEC_AMENDMENT_DRAFT_VALIDATION_FAILED: str = "validation_failed"
SPEC_AMENDMENT_DRAFT_ACCEPTED: str = "accepted"
SPEC_AMENDMENT_DRAFT_REJECTED: str = "rejected"
SPEC_AMENDMENT_DRAFT_INVALID_REMEDIATION: tuple[str, ...] = (
    "Revise the Spec Amendment Draft so it only adds new accepted source items.",
)
CHALLENGE_READINESS_VALUES: frozenset[str] = frozenset(
    {"blocked", "needs_answers", "ready_for_prd"}
)
READY_FOR_PRD_REQUIRED_CONTENT_FIELDS: tuple[str, ...] = (
    "questions",
    "reviewed_evidence",
    "evidence_conflicts",
    "assumptions",
    "non_goals",
    "risks",
    "open_questions",
    "glossary_changes",
)
type ChallengePayloadValidator = Callable[[Mapping[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class _PrdDraftRecordInputs:
    """Validated PRD draft record inputs."""

    payload: Mapping[str, Any]
    superseded_prd: DiscoveryPrd | None


class ChallengeArtifactRecordRequest(BaseModel):
    """Validated request for recording a Challenge Artifact."""

    project_id: int
    artifact_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class PrdDraftRecordRequest(BaseModel):
    """Validated request for recording a PRD draft."""

    project_id: int
    challenge_artifact_id: int
    prd_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    supersedes_prd_id: int | None = None
    changed_by: str = "cli-agent"


class PrdReviewRequest(BaseModel):
    """Validated request for accepting or rejecting a PRD."""

    project_id: int
    prd_id: int
    reviewer: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class SpecAmendmentDraftRecordRequest(BaseModel):
    """Validated request for recording a Spec Amendment Draft."""

    project_id: int
    prd_id: int
    amendment_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    base_spec_version_id: int | None = None
    changed_by: str = "cli-agent"


class SpecAmendmentReviewRequest(BaseModel):
    """Validated request for reviewing a Spec Amendment Draft."""

    project_id: int
    spec_amendment_draft_id: int
    reviewer: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class GreenfieldChallengeArtifactRecordRequest(BaseModel):
    """Validated request for recording a greenfield Challenge Artifact."""

    context_key: str = Field(min_length=1)
    artifact_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class GreenfieldPrdDraftRecordRequest(BaseModel):
    """Validated request for recording a greenfield PRD draft."""

    context_key: str = Field(min_length=1)
    challenge_artifact_id: int
    prd_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class GreenfieldPrdReviewRequest(BaseModel):
    """Validated request for accepting or rejecting a greenfield PRD."""

    context_key: str = Field(min_length=1)
    prd_id: int
    reviewer: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class GreenfieldSpecAmendmentDraftRecordRequest(BaseModel):
    """Validated request for recording a greenfield initial spec draft."""

    context_key: str = Field(min_length=1)
    prd_id: int
    amendment_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class GreenfieldSpecAmendmentReviewRequest(BaseModel):
    """Validated request for reviewing a greenfield initial spec draft."""

    context_key: str = Field(min_length=1)
    spec_amendment_draft_id: int
    reviewer: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


class _ReadOnlyScopeExtensionWorkflowService:
    """Minimal workflow adapter for read-only scope-extension validation."""

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        _ = session_id
        return {}

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        _ = session_id
        _ = partial_update
        message = "Spec Amendment Draft recording cannot mutate workflow state."
        raise RuntimeError(message)


class ScopeDiscoveryRunner:
    """Persist Scope Discovery artifacts."""

    def __init__(self, *, session: Session) -> None:
        """Initialize the runner with an active session."""
        self._session = session

    def record_challenge_artifact(
        self,
        request: ChallengeArtifactRecordRequest,
    ) -> dict[str, Any]:
        """Record a minimal grill-with-docs Challenge Artifact."""
        payload, load_error = _load_challenge_payload(request.artifact_file)
        if load_error is not None:
            return load_error
        if payload is None:
            return _error(
                ErrorCode.CHALLENGE_ARTIFACT_INVALID,
                details={"artifact_file": request.artifact_file},
                remediation=["Pass a valid JSON Challenge Artifact file."],
            )

        project = self._session.get(Product, request.project_id)
        if project is None:
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": request.project_id},
                remediation=["Pass an existing --project-id."],
            )

        validation_error = _validate_minimal_challenge_payload(payload)
        if validation_error is not None:
            return validation_error

        producer = str(payload["producer"])
        readiness = str(payload["readiness"])
        original_idea = str(payload["original_idea"]).strip()
        content_json = canonical_json(payload)
        artifact_fingerprint = canonical_hash(payload)
        request_hash = canonical_hash(
            {
                "command": CHALLENGE_RECORD_COMMAND,
                "project_id": request.project_id,
                "artifact_fingerprint": artifact_fingerprint,
                "changed_by": request.changed_by,
            }
        )
        existing = self._session.exec(
            select(DiscoveryChallengeArtifact).where(
                DiscoveryChallengeArtifact.project_id == request.project_id,
                DiscoveryChallengeArtifact.idempotency_key == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            return _existing_artifact_result(
                artifact=existing,
                request=request,
                request_hash=request_hash,
            )

        artifact = DiscoveryChallengeArtifact(
            project_id=request.project_id,
            producer=producer,
            readiness=readiness,
            original_idea=original_idea,
            content_json=content_json,
            artifact_fingerprint=artifact_fingerprint,
            request_hash=request_hash,
            idempotency_key=request.idempotency_key,
            changed_by=request.changed_by,
        )
        self._session.add(artifact)
        self._session.commit()
        self._session.refresh(artifact)

        return _success(_artifact_data(artifact))

    def record_prd_draft(
        self,
        request: PrdDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record a draft PRD produced by to-prd."""
        inputs, validation_error = self._validated_prd_draft_record_inputs(request)
        if validation_error is not None:
            return validation_error
        inputs = cast("_PrdDraftRecordInputs", inputs)
        payload = inputs.payload

        producer = str(payload["producer"])
        title = str(payload["title"]).strip()
        content_json = canonical_json(payload)
        artifact_fingerprint = canonical_hash(payload)
        request_hash = _prd_draft_request_hash(
            request=request,
            artifact_fingerprint=artifact_fingerprint,
        )
        existing = self._session.exec(
            select(DiscoveryPrd).where(
                DiscoveryPrd.project_id == request.project_id,
                DiscoveryPrd.idempotency_key == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            return _existing_prd_result(
                prd=existing,
                request=request,
                request_hash=request_hash,
            )

        prd = DiscoveryPrd(
            project_id=request.project_id,
            challenge_artifact_id=request.challenge_artifact_id,
            producer=producer,
            status=PRD_STATUS_DRAFT,
            version=_next_prd_version(inputs.superseded_prd),
            title=title,
            content_json=content_json,
            supersedes_prd_id=request.supersedes_prd_id,
            artifact_fingerprint=artifact_fingerprint,
            request_hash=request_hash,
            idempotency_key=request.idempotency_key,
            changed_by=request.changed_by,
        )
        self._session.add(prd)
        self._session.commit()
        self._session.refresh(prd)

        return _success(_prd_data(prd))

    def record_spec_amendment_draft(
        self,
        request: SpecAmendmentDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record and validate an agent-generated Spec Amendment Draft."""
        prd, source_error = self._validated_spec_amendment_source_prd(request)
        if source_error is not None:
            return source_error
        prd = cast("DiscoveryPrd", prd)

        content_json, amended_spec_hash, load_error = _load_spec_amendment_content(
            request.amendment_file
        )
        if load_error is not None:
            return load_error
        content_json = cast("str", content_json)
        amended_spec_hash = cast("str", amended_spec_hash)

        validation_result = ScopeExtensionRunner(
            session=self._session,
            workflow_service=_ReadOnlyScopeExtensionWorkflowService(),
        ).validate(
            ScopeExtensionValidateRequest(
                project_id=request.project_id,
                spec_file=request.amendment_file,
                base_spec_version_id=request.base_spec_version_id,
            )
        )
        if not validation_result["ok"]:
            return validation_result

        validation = cast("dict[str, Any]", validation_result["data"])
        status = (
            SPEC_AMENDMENT_DRAFT_READY
            if validation["valid"]
            else SPEC_AMENDMENT_DRAFT_VALIDATION_FAILED
        )
        artifact_fingerprint = amended_spec_hash
        request_hash = _spec_amendment_draft_request_hash(
            request=request,
            prd=prd,
            artifact_fingerprint=artifact_fingerprint,
        )
        existing = self._session.exec(
            select(DiscoverySpecAmendmentDraft).where(
                DiscoverySpecAmendmentDraft.project_id == request.project_id,
                DiscoverySpecAmendmentDraft.idempotency_key == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            return _existing_spec_amendment_draft_result(
                draft=existing,
                request=request,
                request_hash=request_hash,
            )

        draft = DiscoverySpecAmendmentDraft(
            project_id=request.project_id,
            prd_id=request.prd_id,
            challenge_artifact_id=prd.challenge_artifact_id,
            status=status,
            amendment_file=request.amendment_file,
            content_json=content_json,
            validation_json=canonical_json(validation),
            artifact_fingerprint=artifact_fingerprint,
            request_hash=request_hash,
            idempotency_key=request.idempotency_key,
            base_spec_version_id=cast("int | None", validation["base_spec_version_id"]),
            base_spec_hash=cast("str | None", validation["base_spec_hash"]),
            amended_spec_hash=amended_spec_hash,
            changed_by=request.changed_by,
        )
        self._session.add(draft)
        self._session.commit()
        self._session.refresh(draft)

        return _success(_spec_amendment_draft_data(draft))

    def accept_spec_amendment(
        self,
        request: SpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Accept a validated Spec Amendment Draft."""
        return self._review_spec_amendment(
            request=request,
            command=SPEC_AMENDMENT_ACCEPT_COMMAND,
            target_status=SPEC_AMENDMENT_DRAFT_ACCEPTED,
        )

    def reject_spec_amendment(
        self,
        request: SpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Reject a validated Spec Amendment Draft."""
        return self._review_spec_amendment(
            request=request,
            command=SPEC_AMENDMENT_REJECT_COMMAND,
            target_status=SPEC_AMENDMENT_DRAFT_REJECTED,
        )

    def record_greenfield_challenge_artifact(
        self,
        request: GreenfieldChallengeArtifactRecordRequest,
    ) -> dict[str, Any]:
        """Record a greenfield grill-with-docs Challenge Artifact."""
        payload, load_error = _load_challenge_payload(request.artifact_file)
        if load_error is not None:
            return load_error
        if payload is None:
            return _error(
                ErrorCode.CHALLENGE_ARTIFACT_INVALID,
                details={"artifact_file": request.artifact_file},
                remediation=["Pass a valid JSON Challenge Artifact file."],
            )

        validation_error = _validate_minimal_challenge_payload(payload)
        if validation_error is not None:
            return validation_error

        context = self._greenfield_context_for_key(request)
        producer = str(payload["producer"])
        readiness = str(payload["readiness"])
        original_idea = str(payload["original_idea"]).strip()
        content_json = canonical_json(payload)
        artifact_fingerprint = canonical_hash(payload)
        request_hash = canonical_hash(
            {
                "command": GREENFIELD_CHALLENGE_RECORD_COMMAND,
                "context_key": request.context_key,
                "artifact_fingerprint": artifact_fingerprint,
                "changed_by": request.changed_by,
            }
        )
        existing = self._session.exec(
            select(GreenfieldDiscoveryChallengeArtifact).where(
                GreenfieldDiscoveryChallengeArtifact.greenfield_context_id
                == context.greenfield_context_id,
                GreenfieldDiscoveryChallengeArtifact.idempotency_key
                == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            return _existing_greenfield_artifact_result(
                artifact=existing,
                context=context,
                request_hash=request_hash,
            )

        artifact = GreenfieldDiscoveryChallengeArtifact(
            greenfield_context_id=int(context.greenfield_context_id or 0),
            producer=producer,
            readiness=readiness,
            original_idea=original_idea,
            content_json=content_json,
            artifact_fingerprint=artifact_fingerprint,
            request_hash=request_hash,
            idempotency_key=request.idempotency_key,
            changed_by=request.changed_by,
        )
        self._session.add(artifact)
        self._session.commit()
        self._session.refresh(artifact)

        return _success(_greenfield_artifact_data(artifact, context=context))

    def record_greenfield_prd_draft(
        self,
        request: GreenfieldPrdDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record a greenfield draft PRD produced by to-prd."""
        context, challenge, payload, validation_error = (
            self._validated_greenfield_prd_inputs(request)
        )
        if validation_error is not None:
            return validation_error
        context = cast("GreenfieldDiscoveryContext", context)
        challenge = cast("GreenfieldDiscoveryChallengeArtifact", challenge)
        payload = cast("Mapping[str, Any]", payload)

        producer = str(payload["producer"])
        title = str(payload["title"]).strip()
        content_json = canonical_json(payload)
        artifact_fingerprint = canonical_hash(payload)
        request_hash = canonical_hash(
            {
                "command": GREENFIELD_PRD_DRAFT_RECORD_COMMAND,
                "context_key": request.context_key,
                "challenge_artifact_id": request.challenge_artifact_id,
                "artifact_fingerprint": artifact_fingerprint,
                "changed_by": request.changed_by,
            }
        )
        existing = self._session.exec(
            select(GreenfieldDiscoveryPrd).where(
                GreenfieldDiscoveryPrd.greenfield_context_id
                == context.greenfield_context_id,
                GreenfieldDiscoveryPrd.idempotency_key == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            return _existing_greenfield_prd_result(
                prd=existing,
                context=context,
                request_hash=request_hash,
            )

        prd = GreenfieldDiscoveryPrd(
            greenfield_context_id=int(context.greenfield_context_id or 0),
            challenge_artifact_id=int(challenge.challenge_artifact_id or 0),
            producer=producer,
            status=PRD_STATUS_DRAFT,
            version="1",
            title=title,
            content_json=content_json,
            artifact_fingerprint=artifact_fingerprint,
            request_hash=request_hash,
            idempotency_key=request.idempotency_key,
            changed_by=request.changed_by,
        )
        self._session.add(prd)
        self._session.commit()
        self._session.refresh(prd)

        return _success(_greenfield_prd_data(prd, context=context))

    def accept_greenfield_prd(
        self,
        request: GreenfieldPrdReviewRequest,
    ) -> dict[str, Any]:
        """Accept a greenfield draft PRD."""
        return self._review_greenfield_prd(
            request=request,
            command=GREENFIELD_PRD_ACCEPT_COMMAND,
            target_status=PRD_STATUS_ACCEPTED,
        )

    def reject_greenfield_prd(
        self,
        request: GreenfieldPrdReviewRequest,
    ) -> dict[str, Any]:
        """Reject a greenfield draft PRD."""
        return self._review_greenfield_prd(
            request=request,
            command=GREENFIELD_PRD_REJECT_COMMAND,
            target_status=PRD_STATUS_REJECTED,
        )

    def record_greenfield_spec_amendment_draft(
        self,
        request: GreenfieldSpecAmendmentDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record and validate a greenfield initial structured spec draft."""
        context, prd, source_error = self._validated_greenfield_spec_source_prd(
            request,
        )
        if source_error is not None:
            return source_error
        context = cast("GreenfieldDiscoveryContext", context)
        prd = cast("GreenfieldDiscoveryPrd", prd)

        content_json, amended_spec_hash, load_error = _load_spec_amendment_content(
            request.amendment_file
        )
        if load_error is not None:
            return load_error
        content_json = cast("str", content_json)
        amended_spec_hash = cast("str", amended_spec_hash)

        validation = {
            "valid": True,
            "mode": "greenfield_initial_spec",
            "blocking_issues": [],
        }
        request_hash = canonical_hash(
            {
                "command": GREENFIELD_SPEC_AMENDMENT_DRAFT_RECORD_COMMAND,
                "context_key": request.context_key,
                "prd_id": request.prd_id,
                "artifact_fingerprint": amended_spec_hash,
                "changed_by": request.changed_by,
            }
        )
        existing = self._session.exec(
            select(GreenfieldDiscoverySpecAmendmentDraft).where(
                GreenfieldDiscoverySpecAmendmentDraft.greenfield_context_id
                == context.greenfield_context_id,
                GreenfieldDiscoverySpecAmendmentDraft.idempotency_key
                == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            return _existing_greenfield_spec_amendment_draft_result(
                draft=existing,
                context=context,
                request_hash=request_hash,
            )

        draft = GreenfieldDiscoverySpecAmendmentDraft(
            greenfield_context_id=int(context.greenfield_context_id or 0),
            prd_id=int(prd.prd_id or 0),
            challenge_artifact_id=prd.challenge_artifact_id,
            status=SPEC_AMENDMENT_DRAFT_READY,
            amendment_file=request.amendment_file,
            content_json=content_json,
            validation_json=canonical_json(validation),
            artifact_fingerprint=amended_spec_hash,
            request_hash=request_hash,
            idempotency_key=request.idempotency_key,
            amended_spec_hash=amended_spec_hash,
            changed_by=request.changed_by,
        )
        self._session.add(draft)
        self._session.commit()
        self._session.refresh(draft)

        return _success(_greenfield_spec_amendment_draft_data(draft, context=context))

    def accept_greenfield_spec_amendment(
        self,
        request: GreenfieldSpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Accept a greenfield validated initial spec draft."""
        return self._review_greenfield_spec_amendment(
            request=request,
            command=GREENFIELD_SPEC_AMENDMENT_ACCEPT_COMMAND,
            target_status=SPEC_AMENDMENT_DRAFT_ACCEPTED,
        )

    def reject_greenfield_spec_amendment(
        self,
        request: GreenfieldSpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Reject a greenfield validated initial spec draft."""
        return self._review_greenfield_spec_amendment(
            request=request,
            command=GREENFIELD_SPEC_AMENDMENT_REJECT_COMMAND,
            target_status=SPEC_AMENDMENT_DRAFT_REJECTED,
        )

    def _greenfield_context_for_key(
        self,
        request: GreenfieldChallengeArtifactRecordRequest,
    ) -> GreenfieldDiscoveryContext:
        context = self._session.exec(
            select(GreenfieldDiscoveryContext).where(
                GreenfieldDiscoveryContext.context_key == request.context_key
            )
        ).first()
        if context is not None:
            return context
        context = GreenfieldDiscoveryContext(
            context_key=request.context_key,
            status="discovery",
            request_hash=canonical_hash(
                {
                    "command": GREENFIELD_CHALLENGE_RECORD_COMMAND,
                    "context_key": request.context_key,
                    "changed_by": request.changed_by,
                }
            ),
            idempotency_key=request.idempotency_key,
            changed_by=request.changed_by,
        )
        self._session.add(context)
        self._session.commit()
        self._session.refresh(context)
        return context

    def _greenfield_context_by_key(
        self,
        context_key: str,
    ) -> GreenfieldDiscoveryContext | None:
        return self._session.exec(
            select(GreenfieldDiscoveryContext).where(
                GreenfieldDiscoveryContext.context_key == context_key
            )
        ).first()

    def _validated_greenfield_prd_inputs(
        self,
        request: GreenfieldPrdDraftRecordRequest,
    ) -> tuple[
        GreenfieldDiscoveryContext | None,
        GreenfieldDiscoveryChallengeArtifact | None,
        Mapping[str, Any] | None,
        dict[str, Any] | None,
    ]:
        context = self._greenfield_context_by_key(request.context_key)
        if context is None:
            return (
                None,
                None,
                None,
                _error(
                    ErrorCode.PRD_SOURCE_CHALLENGE_NOT_FOUND,
                    details={"context_key": request.context_key},
                    remediation=["Record a greenfield Challenge Artifact first."],
                ),
            )
        challenge = self._session.get(
            GreenfieldDiscoveryChallengeArtifact,
            request.challenge_artifact_id,
        )
        if (
            challenge is None
            or challenge.greenfield_context_id != context.greenfield_context_id
        ):
            return (
                context,
                None,
                None,
                _error(
                    ErrorCode.PRD_SOURCE_CHALLENGE_NOT_FOUND,
                    details={
                        "context_key": request.context_key,
                        "challenge_artifact_id": request.challenge_artifact_id,
                    },
                    remediation=[
                        "Record the PRD against a Challenge Artifact in the same "
                        "greenfield context."
                    ],
                ),
            )
        if challenge.readiness != "ready_for_prd":
            return (
                context,
                challenge,
                None,
                _error(
                    ErrorCode.PRD_SOURCE_CHALLENGE_NOT_READY,
                    details={
                        "challenge_artifact_id": request.challenge_artifact_id,
                        "readiness": challenge.readiness,
                    },
                    remediation=["Continue grill-with-docs until ready_for_prd."],
                ),
            )
        payload, load_error = _load_prd_payload(request.prd_file)
        if load_error is not None:
            return context, challenge, None, load_error
        payload = cast("Mapping[str, Any]", payload)
        validation_error = _validate_greenfield_prd_payload(payload, request)
        if validation_error is not None:
            return context, challenge, payload, validation_error
        return context, challenge, payload, None

    def _review_greenfield_prd(
        self,
        *,
        request: GreenfieldPrdReviewRequest,
        command: str,
        target_status: str,
    ) -> dict[str, Any]:
        context = self._greenfield_context_by_key(request.context_key)
        prd = self._session.get(GreenfieldDiscoveryPrd, request.prd_id)
        if (
            context is None
            or prd is None
            or prd.greenfield_context_id != context.greenfield_context_id
        ):
            return _error(
                ErrorCode.PRD_NOT_FOUND,
                details={"context_key": request.context_key, "prd_id": request.prd_id},
                remediation=["Pass an existing greenfield PRD ID for this context."],
            )
        if prd.status != PRD_STATUS_DRAFT:
            return _error(
                ErrorCode.PRD_REVIEW_STATE_INVALID,
                details={"prd_id": request.prd_id, "status": prd.status},
                remediation=["Review only draft PRDs."],
            )
        request_hash = canonical_hash(
            {
                "command": command,
                "context_key": request.context_key,
                "prd_id": request.prd_id,
                "reviewer": request.reviewer,
                "notes": request.notes,
                "changed_by": request.changed_by,
            }
        )
        now = datetime.now(UTC)
        prd.status = target_status
        prd.reviewed_by = request.reviewer
        prd.review_notes = request.notes
        prd.reviewed_at = now
        prd.review_request_hash = request_hash
        prd.review_idempotency_key = request.idempotency_key
        prd.changed_by = request.changed_by
        prd.updated_at = now
        self._session.add(prd)
        self._session.commit()
        self._session.refresh(prd)
        return _success(_greenfield_prd_data(prd, context=context))

    def _validated_greenfield_spec_source_prd(
        self,
        request: GreenfieldSpecAmendmentDraftRecordRequest,
    ) -> tuple[
        GreenfieldDiscoveryContext | None,
        GreenfieldDiscoveryPrd | None,
        dict[str, Any] | None,
    ]:
        context = self._greenfield_context_by_key(request.context_key)
        prd = self._session.get(GreenfieldDiscoveryPrd, request.prd_id)
        if (
            context is None
            or prd is None
            or prd.greenfield_context_id != context.greenfield_context_id
        ):
            return (
                context,
                None,
                _error(
                    ErrorCode.PRD_NOT_FOUND,
                    details={
                        "context_key": request.context_key,
                        "prd_id": request.prd_id,
                    },
                    remediation=[
                        "Accept a greenfield PRD before recording the initial "
                        "spec draft."
                    ],
                ),
            )
        if prd.status != PRD_STATUS_ACCEPTED:
            return (
                context,
                prd,
                _error(
                    ErrorCode.SPEC_AMENDMENT_SOURCE_PRD_NOT_ACCEPTED,
                    details={"prd_id": request.prd_id, "status": prd.status},
                    remediation=[
                        "Accept the greenfield PRD before recording the initial "
                        "spec draft."
                    ],
                ),
            )
        return context, prd, None

    def _review_greenfield_spec_amendment(
        self,
        *,
        request: GreenfieldSpecAmendmentReviewRequest,
        command: str,
        target_status: str,
    ) -> dict[str, Any]:
        context = self._greenfield_context_by_key(request.context_key)
        draft = self._session.get(
            GreenfieldDiscoverySpecAmendmentDraft,
            request.spec_amendment_draft_id,
        )
        if (
            context is None
            or draft is None
            or draft.greenfield_context_id != context.greenfield_context_id
        ):
            return _error(
                ErrorCode.SPEC_AMENDMENT_NOT_FOUND,
                details={
                    "context_key": request.context_key,
                    "spec_amendment_draft_id": request.spec_amendment_draft_id,
                },
                remediation=[
                    "Pass an existing greenfield Spec Amendment Draft ID for "
                    "this context."
                ],
            )
        if draft.status != SPEC_AMENDMENT_DRAFT_READY:
            return _error(
                ErrorCode.SPEC_AMENDMENT_REVIEW_STATE_INVALID,
                details={
                    "spec_amendment_draft_id": request.spec_amendment_draft_id,
                    "status": draft.status,
                },
                remediation=["Review only validated Spec Amendment Drafts."],
            )
        request_hash = canonical_hash(
            {
                "command": command,
                "context_key": request.context_key,
                "spec_amendment_draft_id": request.spec_amendment_draft_id,
                "reviewer": request.reviewer,
                "notes": request.notes,
                "changed_by": request.changed_by,
            }
        )
        now = datetime.now(UTC)
        draft.status = target_status
        draft.reviewed_by = request.reviewer
        draft.review_notes = request.notes
        draft.reviewed_at = now
        draft.review_request_hash = request_hash
        draft.review_idempotency_key = request.idempotency_key
        draft.changed_by = request.changed_by
        draft.updated_at = now
        self._session.add(draft)
        self._session.commit()
        self._session.refresh(draft)
        return _success(_greenfield_spec_amendment_draft_data(draft, context=context))

    def _review_spec_amendment(
        self,
        *,
        request: SpecAmendmentReviewRequest,
        command: str,
        target_status: str,
    ) -> dict[str, Any]:
        project = self._session.get(Product, request.project_id)
        if project is None:
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": request.project_id},
                remediation=["Pass an existing --project-id."],
            )

        request_hash = canonical_hash(
            {
                "command": command,
                "project_id": request.project_id,
                "spec_amendment_draft_id": request.spec_amendment_draft_id,
                "reviewer": request.reviewer,
                "notes": request.notes,
                "changed_by": request.changed_by,
            }
        )
        idempotency_error = self._spec_amendment_review_idempotency_error(
            request=request,
            request_hash=request_hash,
        )
        if idempotency_error is not None:
            return idempotency_error

        draft = self._session.get(
            DiscoverySpecAmendmentDraft,
            request.spec_amendment_draft_id,
        )
        if draft is None or draft.project_id != request.project_id:
            return _error(
                ErrorCode.SPEC_AMENDMENT_NOT_FOUND,
                details={
                    "project_id": request.project_id,
                    "spec_amendment_draft_id": request.spec_amendment_draft_id,
                },
                remediation=[
                    "Pass an existing Spec Amendment Draft ID for this project."
                ],
            )
        if draft.status != SPEC_AMENDMENT_DRAFT_READY:
            return _error(
                ErrorCode.SPEC_AMENDMENT_REVIEW_STATE_INVALID,
                details={
                    "spec_amendment_draft_id": request.spec_amendment_draft_id,
                    "status": draft.status,
                },
                remediation=[
                    "Review only validated Spec Amendment Drafts "
                    "that are ready for acceptance."
                ],
            )

        now = datetime.now(UTC)
        draft.status = target_status
        draft.reviewed_by = request.reviewer
        draft.review_notes = request.notes
        draft.reviewed_at = now
        draft.review_request_hash = request_hash
        draft.review_idempotency_key = request.idempotency_key
        draft.changed_by = request.changed_by
        draft.updated_at = now
        self._session.add(draft)
        self._session.commit()
        self._session.refresh(draft)

        return _success(_spec_amendment_draft_data(draft))

    def _spec_amendment_review_idempotency_error(
        self,
        *,
        request: SpecAmendmentReviewRequest,
        request_hash: str,
    ) -> dict[str, Any] | None:
        existing = self._session.exec(
            select(DiscoverySpecAmendmentDraft).where(
                DiscoverySpecAmendmentDraft.project_id == request.project_id,
                DiscoverySpecAmendmentDraft.review_idempotency_key
                == request.idempotency_key,
            )
        ).first()
        if existing is None:
            return None
        if existing.review_request_hash != request_hash:
            return _error(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                details={
                    "project_id": request.project_id,
                    "idempotency_key": request.idempotency_key,
                },
                remediation=["Retry with a fresh --idempotency-key."],
            )
        return _success(_spec_amendment_draft_data(existing))

    def _validated_prd_draft_record_inputs(
        self,
        request: PrdDraftRecordRequest,
    ) -> tuple[_PrdDraftRecordInputs | None, dict[str, Any] | None]:
        payload, error = _load_prd_payload(request.prd_file)
        superseded_prd: DiscoveryPrd | None = None
        if error is None:
            payload = cast("Mapping[str, Any]", payload)
            error = self._validate_prd_draft_record_state(payload, request)
        if error is None:
            superseded_prd, error = self._validated_superseded_prd(request)
        if error is not None:
            return None, error
        return (
            _PrdDraftRecordInputs(
                payload=cast("Mapping[str, Any]", payload),
                superseded_prd=superseded_prd,
            ),
            None,
        )

    def _validated_spec_amendment_source_prd(
        self,
        request: SpecAmendmentDraftRecordRequest,
    ) -> tuple[DiscoveryPrd | None, dict[str, Any] | None]:
        project_error = _project_exists_error(self._session, request.project_id)
        if project_error is not None:
            return None, project_error

        prd = self._session.get(DiscoveryPrd, request.prd_id)
        if prd is None or prd.project_id != request.project_id:
            return (
                None,
                _error(
                    ErrorCode.PRD_NOT_FOUND,
                    details={
                        "project_id": request.project_id,
                        "prd_id": request.prd_id,
                    },
                    remediation=[
                        "Accept a PRD before recording a Spec Amendment Draft."
                    ],
                ),
            )
        if prd.status != PRD_STATUS_ACCEPTED:
            return (
                None,
                _error(
                    ErrorCode.SPEC_AMENDMENT_SOURCE_PRD_NOT_ACCEPTED,
                    details={
                        "project_id": request.project_id,
                        "prd_id": request.prd_id,
                        "status": prd.status,
                    },
                    remediation=[
                        "Accept the PRD before recording a Spec Amendment Draft."
                    ],
                ),
            )
        return prd, None

    def _validate_prd_draft_record_state(
        self,
        payload: Mapping[str, Any],
        request: PrdDraftRecordRequest,
    ) -> dict[str, Any] | None:
        validators: tuple[Callable[[], dict[str, Any] | None], ...] = (
            lambda: _project_exists_error(self._session, request.project_id),
            lambda: _validate_prd_source_challenge(
                challenge=self._session.get(
                    DiscoveryChallengeArtifact,
                    request.challenge_artifact_id,
                ),
                request=request,
            ),
            lambda: _validate_prd_payload(payload, request),
            lambda: self._validate_prd_record_target(payload, request),
        )
        for validator in validators:
            error = validator()
            if error is not None:
                return error
        return None

    def accept_prd(self, request: PrdReviewRequest) -> dict[str, Any]:
        """Accept a draft PRD."""
        return self._review_prd(
            request=request,
            command=PRD_ACCEPT_COMMAND,
            target_status=PRD_STATUS_ACCEPTED,
        )

    def reject_prd(self, request: PrdReviewRequest) -> dict[str, Any]:
        """Reject a draft PRD."""
        return self._review_prd(
            request=request,
            command=PRD_REJECT_COMMAND,
            target_status=PRD_STATUS_REJECTED,
        )

    def _review_prd(
        self,
        *,
        request: PrdReviewRequest,
        command: str,
        target_status: str,
    ) -> dict[str, Any]:
        project = self._session.get(Product, request.project_id)
        if project is None:
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": request.project_id},
                remediation=["Pass an existing --project-id."],
            )

        request_hash = canonical_hash(
            {
                "command": command,
                "project_id": request.project_id,
                "prd_id": request.prd_id,
                "reviewer": request.reviewer,
                "notes": request.notes,
                "changed_by": request.changed_by,
            }
        )
        idempotency_error = self._prd_review_idempotency_error(
            request=request,
            request_hash=request_hash,
        )
        if idempotency_error is not None:
            return idempotency_error

        prd = self._session.get(DiscoveryPrd, request.prd_id)
        if prd is None or prd.project_id != request.project_id:
            return _error(
                ErrorCode.PRD_NOT_FOUND,
                details={"project_id": request.project_id, "prd_id": request.prd_id},
                remediation=["Pass an existing PRD ID for this project."],
            )
        if prd.status != PRD_STATUS_DRAFT:
            return _error(
                ErrorCode.PRD_REVIEW_STATE_INVALID,
                details={"prd_id": request.prd_id, "status": prd.status},
                remediation=["Review only draft PRDs."],
            )

        now = datetime.now(UTC)
        prd.status = target_status
        prd.reviewed_by = request.reviewer
        prd.review_notes = request.notes
        prd.reviewed_at = now
        prd.review_request_hash = request_hash
        prd.review_idempotency_key = request.idempotency_key
        prd.changed_by = request.changed_by
        prd.updated_at = now
        self._session.add(prd)
        self._session.commit()
        self._session.refresh(prd)

        return _success(_prd_data(prd))

    def _prd_review_idempotency_error(
        self,
        *,
        request: PrdReviewRequest,
        request_hash: str,
    ) -> dict[str, Any] | None:
        existing = self._session.exec(
            select(DiscoveryPrd).where(
                DiscoveryPrd.project_id == request.project_id,
                DiscoveryPrd.review_idempotency_key == request.idempotency_key,
            )
        ).first()
        if existing is None:
            return None
        if existing.review_request_hash != request_hash:
            return _error(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                details={
                    "project_id": request.project_id,
                    "idempotency_key": request.idempotency_key,
                },
                remediation=["Retry with a fresh --idempotency-key."],
            )
        return _success(_prd_data(existing))

    def _validate_prd_record_target(
        self,
        payload: Mapping[str, Any],
        request: PrdDraftRecordRequest,
    ) -> dict[str, Any] | None:
        if "prd_id" not in payload:
            return None
        try:
            target_prd_id = int(str(payload["prd_id"]))
        except ValueError:
            return _error(
                ErrorCode.PRD_DRAFT_INVALID,
                details={"field": "prd_id", "reason": "not_integer"},
                remediation=["Omit prd_id when recording a new PRD draft."],
            )
        target = self._session.get(DiscoveryPrd, target_prd_id)
        if (
            target is not None
            and target.project_id == request.project_id
            and target.status == PRD_STATUS_ACCEPTED
        ):
            return _error(
                ErrorCode.PRD_ACCEPTED_IMMUTABLE,
                details={"prd_id": target_prd_id, "status": target.status},
                remediation=[
                    "Create a new draft with --supersedes-prd-id instead."
                ],
            )
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={"field": "prd_id", "reason": "in_place_target_forbidden"},
            remediation=["Omit prd_id when recording a new PRD draft."],
        )

    def _validated_superseded_prd(
        self,
        request: PrdDraftRecordRequest,
    ) -> tuple[DiscoveryPrd | None, dict[str, Any] | None]:
        if request.supersedes_prd_id is None:
            return None, None
        superseded = self._session.get(DiscoveryPrd, request.supersedes_prd_id)
        if superseded is None or superseded.project_id != request.project_id:
            return (
                None,
                _error(
                    ErrorCode.PRD_SUPERSEDES_NOT_FOUND,
                    details={
                        "project_id": request.project_id,
                        "supersedes_prd_id": request.supersedes_prd_id,
                    },
                    remediation=["Pass an accepted PRD from the same project."],
                ),
            )
        if superseded.status != PRD_STATUS_ACCEPTED:
            return (
                None,
                _error(
                    ErrorCode.PRD_SUPERSEDES_NOT_ACCEPTED,
                    details={
                        "supersedes_prd_id": request.supersedes_prd_id,
                        "status": superseded.status,
                    },
                    remediation=["Only accepted PRDs can be superseded."],
                ),
            )
        return superseded, None


def _existing_artifact_result(
    *,
    artifact: DiscoveryChallengeArtifact,
    request: ChallengeArtifactRecordRequest,
    request_hash: str,
) -> dict[str, Any]:
    if artifact.request_hash != request_hash:
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            details={
                "project_id": request.project_id,
                "idempotency_key": request.idempotency_key,
            },
            remediation=["Retry with a fresh --idempotency-key."],
        )
    return _success(_artifact_data(artifact))


def _existing_prd_result(
    *,
    prd: DiscoveryPrd,
    request: PrdDraftRecordRequest,
    request_hash: str,
) -> dict[str, Any]:
    if prd.request_hash != request_hash:
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            details={
                "project_id": request.project_id,
                "idempotency_key": request.idempotency_key,
            },
            remediation=["Retry with a fresh --idempotency-key."],
        )
    return _success(_prd_data(prd))


def _existing_spec_amendment_draft_result(
    *,
    draft: DiscoverySpecAmendmentDraft,
    request: SpecAmendmentDraftRecordRequest,
    request_hash: str,
) -> dict[str, Any]:
    if draft.request_hash != request_hash:
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            details={
                "project_id": request.project_id,
                "idempotency_key": request.idempotency_key,
            },
            remediation=["Retry with a fresh --idempotency-key."],
        )
    return _success(_spec_amendment_draft_data(draft))


def _existing_greenfield_artifact_result(
    *,
    artifact: GreenfieldDiscoveryChallengeArtifact,
    context: GreenfieldDiscoveryContext,
    request_hash: str,
) -> dict[str, Any]:
    if artifact.request_hash != request_hash:
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            details={
                "context_key": context.context_key,
                "idempotency_key": artifact.idempotency_key,
            },
            remediation=["Retry with a fresh --idempotency-key."],
        )
    return _success(_greenfield_artifact_data(artifact, context=context))


def _existing_greenfield_prd_result(
    *,
    prd: GreenfieldDiscoveryPrd,
    context: GreenfieldDiscoveryContext,
    request_hash: str,
) -> dict[str, Any]:
    if prd.request_hash != request_hash:
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            details={
                "context_key": context.context_key,
                "idempotency_key": prd.idempotency_key,
            },
            remediation=["Retry with a fresh --idempotency-key."],
        )
    return _success(_greenfield_prd_data(prd, context=context))


def _existing_greenfield_spec_amendment_draft_result(
    *,
    draft: GreenfieldDiscoverySpecAmendmentDraft,
    context: GreenfieldDiscoveryContext,
    request_hash: str,
) -> dict[str, Any]:
    if draft.request_hash != request_hash:
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            details={
                "context_key": context.context_key,
                "idempotency_key": draft.idempotency_key,
            },
            remediation=["Retry with a fresh --idempotency-key."],
        )
    return _success(_greenfield_spec_amendment_draft_data(draft, context=context))


def _prd_draft_request_hash(
    *,
    request: PrdDraftRecordRequest,
    artifact_fingerprint: str,
) -> str:
    request_data: dict[str, object] = {
        "command": PRD_DRAFT_RECORD_COMMAND,
        "project_id": request.project_id,
        "challenge_artifact_id": request.challenge_artifact_id,
        "artifact_fingerprint": artifact_fingerprint,
        "changed_by": request.changed_by,
    }
    if request.supersedes_prd_id is not None:
        request_data["supersedes_prd_id"] = request.supersedes_prd_id
    return canonical_hash(request_data)


def _spec_amendment_draft_request_hash(
    *,
    request: SpecAmendmentDraftRecordRequest,
    prd: DiscoveryPrd,
    artifact_fingerprint: str,
) -> str:
    request_data: dict[str, object] = {
        "command": SPEC_AMENDMENT_DRAFT_RECORD_COMMAND,
        "project_id": request.project_id,
        "prd_id": request.prd_id,
        "challenge_artifact_id": prd.challenge_artifact_id,
        "artifact_fingerprint": artifact_fingerprint,
        "changed_by": request.changed_by,
    }
    if request.base_spec_version_id is not None:
        request_data["base_spec_version_id"] = request.base_spec_version_id
    return canonical_hash(request_data)


def _load_challenge_payload(
    artifact_file: str,
) -> tuple[Mapping[str, Any] | None, dict[str, Any] | None]:
    path = Path(artifact_file).expanduser()
    if not path.exists():
        return (
            None,
            _error(
                ErrorCode.CHALLENGE_ARTIFACT_FILE_NOT_FOUND,
                details={"artifact_file": artifact_file},
                remediation=["Pass an existing --artifact-file path."],
            ),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            None,
            _error(
                ErrorCode.CHALLENGE_ARTIFACT_INVALID,
                details={"artifact_file": artifact_file, "error": str(exc)},
                remediation=["Pass a valid JSON Challenge Artifact file."],
            ),
        )
    if not isinstance(payload, Mapping):
        return (
            None,
            _error(
                ErrorCode.CHALLENGE_ARTIFACT_INVALID,
                details={"artifact_file": artifact_file},
                remediation=["Pass a JSON object Challenge Artifact file."],
            ),
        )
    return payload, None


def _load_prd_payload(
    prd_file: str,
) -> tuple[Mapping[str, Any] | None, dict[str, Any] | None]:
    path = Path(prd_file).expanduser()
    if not path.exists():
        return (
            None,
            _error(
                ErrorCode.PRD_FILE_NOT_FOUND,
                details={"prd_file": prd_file},
                remediation=["Pass an existing --prd-file path."],
            ),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            None,
            _error(
                ErrorCode.PRD_DRAFT_INVALID,
                details={"prd_file": prd_file, "error": str(exc)},
                remediation=["Pass a valid JSON PRD draft file."],
            ),
        )
    if not isinstance(payload, Mapping):
        return (
            None,
            _error(
                ErrorCode.PRD_DRAFT_INVALID,
                details={"prd_file": prd_file},
                remediation=["Pass a JSON object PRD draft file."],
            ),
        )
    return payload, None


def _load_spec_amendment_content(
    amendment_file: str,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    try:
        _artifact, content_json, spec_hash = load_structured_spec_file(amendment_file)
    except FileNotFoundError:
        return (
            None,
            None,
            _error(
                ErrorCode.SPEC_FILE_NOT_FOUND,
                details={"amendment_file": amendment_file},
                remediation=["Pass an existing --amendment-file path."],
            ),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return (
            None,
            None,
            _error(
                ErrorCode.SPEC_FILE_INVALID,
                details={"amendment_file": amendment_file, "error": str(exc)},
                remediation=["Pass a valid structured AgileForge spec file."],
            ),
        )
    return content_json, spec_hash, None


def _validate_prd_source_challenge(
    *,
    challenge: DiscoveryChallengeArtifact | None,
    request: PrdDraftRecordRequest,
) -> dict[str, Any] | None:
    if challenge is None or challenge.project_id != request.project_id:
        return _error(
            ErrorCode.PRD_SOURCE_CHALLENGE_NOT_FOUND,
            details={
                "project_id": request.project_id,
                "challenge_artifact_id": request.challenge_artifact_id,
            },
            remediation=["Record a ready Challenge Artifact before recording a PRD."],
        )
    if challenge.readiness != "ready_for_prd":
        return _error(
            ErrorCode.PRD_SOURCE_CHALLENGE_NOT_READY,
            details={
                "challenge_artifact_id": request.challenge_artifact_id,
                "readiness": challenge.readiness,
            },
            remediation=["Continue discovery until the artifact is ready_for_prd."],
        )
    return None


def _project_exists_error(session: Session, project_id: int) -> dict[str, Any] | None:
    if session.get(Product, project_id) is None:
        return _error(
            ErrorCode.PROJECT_NOT_FOUND,
            details={"project_id": project_id},
            remediation=["Pass an existing --project-id."],
        )
    return None


def _validate_prd_payload(
    payload: Mapping[str, Any],
    request: PrdDraftRecordRequest,
) -> dict[str, Any] | None:
    missing = [
        field
        for field in ("producer", "source_challenge_artifact_id", "title", "content")
        if field not in payload
    ]
    if missing:
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={"missing": missing},
            remediation=[
                "Include producer, source_challenge_artifact_id, title, and content."
            ],
        )
    if str(payload["producer"]) != PRD_PRODUCER:
        return _error(
            ErrorCode.PRD_PRODUCER_INVALID,
            details={
                "producer": str(payload["producer"]),
                "required_producer": PRD_PRODUCER,
            },
            remediation=["Run to-prd and record that PRD draft output."],
        )
    if payload["source_challenge_artifact_id"] != request.challenge_artifact_id:
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={
                "source_challenge_artifact_id": payload[
                    "source_challenge_artifact_id"
                ],
                "expected_challenge_artifact_id": request.challenge_artifact_id,
            },
            remediation=[
                "Record the PRD against its declared source Challenge Artifact."
            ],
        )
    if not str(payload["title"]).strip():
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={"blank": ["title"]},
            remediation=["Include a non-blank PRD title."],
        )
    if not isinstance(payload["content"], Mapping):
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={"field": "content", "reason": "not_object"},
            remediation=["Include a JSON object in content."],
        )
    return None


def _validate_greenfield_prd_payload(
    payload: Mapping[str, Any],
    request: GreenfieldPrdDraftRecordRequest,
) -> dict[str, Any] | None:
    missing = [
        field
        for field in ("producer", "source_challenge_artifact_id", "title", "content")
        if field not in payload
    ]
    if missing:
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={"missing": missing},
            remediation=[
                "Include producer, source_challenge_artifact_id, title, and content."
            ],
        )
    if str(payload["producer"]) != PRD_PRODUCER:
        return _error(
            ErrorCode.PRD_PRODUCER_INVALID,
            details={
                "producer": str(payload["producer"]),
                "required_producer": PRD_PRODUCER,
            },
            remediation=["Run to-prd and record that PRD draft output."],
        )
    if payload["source_challenge_artifact_id"] != request.challenge_artifact_id:
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={
                "source_challenge_artifact_id": payload[
                    "source_challenge_artifact_id"
                ],
                "expected_challenge_artifact_id": request.challenge_artifact_id,
            },
            remediation=[
                "Record the PRD against its declared source Challenge Artifact."
            ],
        )
    if not str(payload["title"]).strip():
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={"blank": ["title"]},
            remediation=["Include a non-blank PRD title."],
        )
    if not isinstance(payload["content"], Mapping):
        return _error(
            ErrorCode.PRD_DRAFT_INVALID,
            details={"field": "content", "reason": "not_object"},
            remediation=["Include a JSON object in content."],
        )
    return None


def _validate_minimal_challenge_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    validators: tuple[ChallengePayloadValidator, ...] = (
        _validate_required_challenge_fields,
        _validate_challenge_producer,
        _validate_challenge_readiness,
        _validate_original_idea,
        _validate_challenge_content,
        _validate_ready_for_prd_content,
    )
    for validator in validators:
        validation_error = validator(payload)
        if validation_error is not None:
            return validation_error
    return None


def _validate_required_challenge_fields(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    missing = [
        field
        for field in ("producer", "readiness", "original_idea", "content")
        if field not in payload
    ]
    if missing:
        return _error(
            ErrorCode.CHALLENGE_ARTIFACT_INVALID,
            details={"missing": missing},
            remediation=["Include producer, readiness, original_idea, and content."],
        )
    return None


def _validate_challenge_producer(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(payload["producer"]) != CHALLENGE_PRODUCER:
        return _error(
            ErrorCode.CHALLENGE_PRODUCER_INVALID,
            details={
                "producer": str(payload["producer"]),
                "required_producer": CHALLENGE_PRODUCER,
            },
            remediation=["Run grill-with-docs and record that Challenge Artifact."],
        )
    return None


def _validate_challenge_readiness(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(payload["readiness"]) not in CHALLENGE_READINESS_VALUES:
        return _error(
            ErrorCode.CHALLENGE_ARTIFACT_INVALID,
            details={
                "readiness": str(payload["readiness"]),
                "allowed": sorted(CHALLENGE_READINESS_VALUES),
            },
            remediation=["Use blocked, needs_answers, or ready_for_prd readiness."],
        )
    return None


def _validate_original_idea(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if not str(payload["original_idea"]).strip():
        return _error(
            ErrorCode.CHALLENGE_ARTIFACT_INVALID,
            details={"blank": ["original_idea"]},
            remediation=["Include a non-blank original_idea."],
        )
    return None


def _validate_challenge_content(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    content = payload["content"]
    if not isinstance(content, Mapping):
        return _error(
            ErrorCode.CHALLENGE_ARTIFACT_INVALID,
            details={"field": "content", "reason": "not_object"},
            remediation=["Include a JSON object in content."],
        )
    return None


def _validate_ready_for_prd_content(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if str(payload["readiness"]) == "ready_for_prd":
        content = cast("Mapping[str, Any]", payload["content"])
        blockers = _ready_for_prd_blockers(content)
        if blockers:
            return _error(
                ErrorCode.CHALLENGE_ARTIFACT_INVALID,
                details={"blockers": blockers},
                remediation=[
                    "Resolve every blocker before marking the artifact ready_for_prd."
                ],
            )
    return None


def _ready_for_prd_blockers(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers = _required_rich_field_blockers(content)
    blockers.extend(_question_blockers(content))
    blockers.extend(_reviewed_evidence_blockers(content))
    blockers.extend(_open_question_blockers(content))
    blockers.extend(_evidence_conflict_blockers(content))
    blockers.extend(_glossary_change_blockers(content))
    return blockers


def _required_rich_field_blockers(
    content: Mapping[str, Any],
) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for field in READY_FOR_PRD_REQUIRED_CONTENT_FIELDS:
        value = content.get(field)
        if field not in content:
            blockers.append(_blocker(f"content.{field}", "missing"))
        elif not isinstance(value, list):
            blockers.append(_blocker(f"content.{field}", "not_list"))
    return blockers


def _question_blockers(content: Mapping[str, Any]) -> list[dict[str, object]]:
    questions = _list_field(content, "questions")
    if questions is None:
        return []
    if not questions:
        return [_blocker("content.questions", "missing_answered_questions")]
    if any(
        not _mapping_has_required_text(item, ("question", "answer"))
        for item in questions
    ):
        return [_blocker("content.questions", "question_or_answer_missing")]
    return []


def _reviewed_evidence_blockers(
    content: Mapping[str, Any],
) -> list[dict[str, object]]:
    reviewed_evidence = _list_field(content, "reviewed_evidence")
    if reviewed_evidence is None:
        return []
    if not reviewed_evidence:
        return [_blocker("content.reviewed_evidence", "missing_reviewed_evidence")]
    if any(not _reviewed_evidence_is_complete(item) for item in reviewed_evidence):
        return [_blocker("content.reviewed_evidence", "source_or_summary_missing")]
    return []


def _open_question_blockers(content: Mapping[str, Any]) -> list[dict[str, object]]:
    open_questions = _list_field(content, "open_questions")
    if open_questions:
        return [
            _blocker(
                "content.open_questions",
                "unresolved_open_questions",
                count=len(open_questions),
            )
        ]
    return []


def _evidence_conflict_blockers(
    content: Mapping[str, Any],
) -> list[dict[str, object]]:
    conflicts = _list_field(content, "evidence_conflicts") or []
    unresolved = [
        conflict
        for conflict in conflicts
        if not _evidence_conflict_is_resolved(conflict)
    ]
    if unresolved:
        return [
            _blocker(
                "content.evidence_conflicts",
                "unresolved_evidence_conflicts",
                count=len(unresolved),
            )
        ]
    return []


def _glossary_change_blockers(
    content: Mapping[str, Any],
) -> list[dict[str, object]]:
    glossary_changes = _list_field(content, "glossary_changes") or []
    uncommitted = [
        change
        for change in glossary_changes
        if not _glossary_change_is_committed(change)
    ]
    if uncommitted:
        return [
            _blocker(
                "content.glossary_changes",
                "project_glossary_update_missing",
                count=len(uncommitted),
            )
        ]
    return []


def _list_field(content: Mapping[str, Any], field: str) -> list[Any] | None:
    value = content.get(field)
    if isinstance(value, list):
        return value
    return None


def _object_mapping(item: object) -> Mapping[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    return cast("Mapping[str, object]", item)


def _mapping_has_required_text(item: object, keys: tuple[str, ...]) -> bool:
    mapping = _object_mapping(item)
    if mapping is None:
        return False
    return all(str(mapping.get(key, "")).strip() for key in keys)


def _reviewed_evidence_is_complete(item: object) -> bool:
    mapping = _object_mapping(item)
    if mapping is None:
        return False
    has_source = bool(str(mapping.get("source", "")).strip())
    has_summary = bool(
        str(mapping.get("summary", "")).strip()
        or str(mapping.get("finding", "")).strip()
    )
    return has_source and has_summary


def _evidence_conflict_is_resolved(conflict: object) -> bool:
    mapping = _object_mapping(conflict)
    if mapping is None:
        return False
    return mapping.get("resolved") is True or bool(
        str(mapping.get("resolution", "")).strip()
    )


def _glossary_change_is_committed(change: object) -> bool:
    mapping = _object_mapping(change)
    if mapping is None:
        return False
    return mapping.get("committed_to_project_glossary") is True and bool(
        str(mapping.get("evidence", "")).strip()
    )


def _blocker(field: str, reason: str, **extra: object) -> dict[str, object]:
    return {"field": field, "reason": reason, **extra}


def _artifact_data(artifact: DiscoveryChallengeArtifact) -> dict[str, Any]:
    return {
        "challenge_artifact_id": artifact.challenge_artifact_id,
        "project_id": artifact.project_id,
        "producer": artifact.producer,
        "readiness": artifact.readiness,
        "artifact_fingerprint": artifact.artifact_fingerprint,
        "next_action": (
            "record_prd"
            if artifact.readiness == "ready_for_prd"
            else "continue_challenge"
        ),
    }


def _prd_data(prd: DiscoveryPrd) -> dict[str, Any]:
    return {
        "prd_id": prd.prd_id,
        "project_id": prd.project_id,
        "challenge_artifact_id": prd.challenge_artifact_id,
        "producer": prd.producer,
        "status": prd.status,
        "version": prd.version,
        "supersedes_prd_id": prd.supersedes_prd_id,
        "superseded_by_prd_id": None,
        "artifact_fingerprint": prd.artifact_fingerprint,
        "next_action": _prd_next_action(prd.status),
    }


def _prd_next_action(status: str) -> str:
    if status == PRD_STATUS_ACCEPTED:
        return "record_spec_amendment_draft"
    if status == PRD_STATUS_REJECTED:
        return "revise_prd"
    return "accept_prd"


def _spec_amendment_draft_data(
    draft: DiscoverySpecAmendmentDraft,
) -> dict[str, Any]:
    validation = json.loads(draft.validation_json)
    blocking_issues = validation.get("blocking_issues", [])
    remediation = (
        SPEC_AMENDMENT_DRAFT_INVALID_REMEDIATION
        if draft.status == SPEC_AMENDMENT_DRAFT_VALIDATION_FAILED
        else []
    )
    return {
        "spec_amendment_draft_id": draft.spec_amendment_draft_id,
        "project_id": draft.project_id,
        "prd_id": draft.prd_id,
        "challenge_artifact_id": draft.challenge_artifact_id,
        "status": draft.status,
        "amendment_file": draft.amendment_file,
        "artifact_fingerprint": draft.artifact_fingerprint,
        "base_spec_version_id": draft.base_spec_version_id,
        "base_spec_hash": draft.base_spec_hash,
        "amended_spec_hash": draft.amended_spec_hash,
        "validation": validation,
        "blocking_issues": blocking_issues,
        "remediation": list(remediation),
        "next_action": _spec_amendment_draft_next_action(draft.status),
    }


def _spec_amendment_draft_next_action(status: str) -> str:
    if status == SPEC_AMENDMENT_DRAFT_ACCEPTED:
        return "start_scope_extension"
    if status == SPEC_AMENDMENT_DRAFT_READY:
        return "accept_spec_amendment"
    return "revise_spec_amendment_draft"


def _greenfield_artifact_data(
    artifact: GreenfieldDiscoveryChallengeArtifact,
    *,
    context: GreenfieldDiscoveryContext,
) -> dict[str, Any]:
    return {
        "challenge_artifact_id": artifact.challenge_artifact_id,
        "greenfield_context_id": context.greenfield_context_id,
        "context_key": context.context_key,
        "project_id": context.project_id,
        "producer": artifact.producer,
        "readiness": artifact.readiness,
        "artifact_fingerprint": artifact.artifact_fingerprint,
        "next_action": (
            "record_greenfield_prd"
            if artifact.readiness == "ready_for_prd"
            else "continue_challenge"
        ),
    }


def _greenfield_prd_data(
    prd: GreenfieldDiscoveryPrd,
    *,
    context: GreenfieldDiscoveryContext,
) -> dict[str, Any]:
    return {
        "prd_id": prd.prd_id,
        "greenfield_context_id": context.greenfield_context_id,
        "context_key": context.context_key,
        "project_id": context.project_id,
        "challenge_artifact_id": prd.challenge_artifact_id,
        "producer": prd.producer,
        "status": prd.status,
        "version": prd.version,
        "artifact_fingerprint": prd.artifact_fingerprint,
        "next_action": _prd_next_action(prd.status),
    }


def _greenfield_spec_amendment_draft_data(
    draft: GreenfieldDiscoverySpecAmendmentDraft,
    *,
    context: GreenfieldDiscoveryContext,
) -> dict[str, Any]:
    validation = json.loads(draft.validation_json)
    return {
        "spec_amendment_draft_id": draft.spec_amendment_draft_id,
        "greenfield_context_id": context.greenfield_context_id,
        "context_key": context.context_key,
        "project_id": context.project_id,
        "prd_id": draft.prd_id,
        "challenge_artifact_id": draft.challenge_artifact_id,
        "status": draft.status,
        "amendment_file": draft.amendment_file,
        "artifact_fingerprint": draft.artifact_fingerprint,
        "amended_spec_hash": draft.amended_spec_hash,
        "validation": validation,
        "blocking_issues": validation.get("blocking_issues", []),
        "remediation": [],
        "next_action": _spec_amendment_draft_next_action(draft.status),
    }


def _next_prd_version(superseded_prd: DiscoveryPrd | None) -> str:
    if superseded_prd is None:
        return "1"
    try:
        return str(int(superseded_prd.version) + 1)
    except ValueError:
        return f"{superseded_prd.version}.1"


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error(
    code: ErrorCode,
    *,
    details: dict[str, Any],
    remediation: list[str],
) -> dict[str, Any]:
    error = workbench_error(code, details=details, remediation=remediation)
    return {"ok": False, "data": None, "warnings": [], "errors": [error.to_dict()]}
