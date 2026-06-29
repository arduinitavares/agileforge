"""Scope Discovery artifact recording services."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from models.agent_workbench import DiscoveryChallengeArtifact
from models.core import Product
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash, canonical_json

CHALLENGE_RECORD_COMMAND: str = "agileforge discovery challenge record"
CHALLENGE_PRODUCER: str = "grill-with-docs"
CHALLENGE_READINESS_VALUES: frozenset[str] = frozenset(
    {"blocked", "needs_answers", "ready_for_prd"}
)


class ChallengeArtifactRecordRequest(BaseModel):
    """Validated request for recording a Challenge Artifact."""

    project_id: int
    artifact_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"


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


def _validate_minimal_challenge_payload(
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
    if str(payload["producer"]) != CHALLENGE_PRODUCER:
        return _error(
            ErrorCode.CHALLENGE_PRODUCER_INVALID,
            details={
                "producer": str(payload["producer"]),
                "required_producer": CHALLENGE_PRODUCER,
            },
            remediation=["Run grill-with-docs and record that Challenge Artifact."],
        )
    if str(payload["readiness"]) not in CHALLENGE_READINESS_VALUES:
        return _error(
            ErrorCode.CHALLENGE_ARTIFACT_INVALID,
            details={
                "readiness": str(payload["readiness"]),
                "allowed": sorted(CHALLENGE_READINESS_VALUES),
            },
            remediation=["Use blocked, needs_answers, or ready_for_prd readiness."],
        )
    if not str(payload["original_idea"]).strip():
        return _error(
            ErrorCode.CHALLENGE_ARTIFACT_INVALID,
            details={"blank": ["original_idea"]},
            remediation=["Include a non-blank original_idea."],
        )
    return None


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
