"""Scope Discovery artifact recording services."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

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
