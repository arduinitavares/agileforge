"""Authority feedback and curation mutation service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.authority_curation import AuthorityFeedbackAttempt
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.envelope import (
    WorkbenchError,
    error_envelope,
    success_envelope,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

AUTHORITY_FEEDBACK_RECORD_COMMAND = "agileforge authority feedback record"

FeedbackTargetKind = Literal[
    "invariant",
    "gap",
    "assumption",
    "quality_group",
    "source_item",
    "authority_candidate",
]
FeedbackIssueType = Literal[
    "overstrong_invariant",
    "understrong_invariant",
    "materially_wrong_invariant",
    "duplicate_invariant",
    "near_duplicate_invariant",
    "over_split_group",
    "brittle_wording",
    "missing_invariant",
    "invalid_gap",
    "invalid_assumption",
    "source_map_error",
    "coverage_gap",
]
FeedbackSeverity = Literal["blocking", "non_blocking"]
TargetIndex = dict[str, set[str]]


@dataclass(frozen=True)
class _AuthorityGuardResult:
    """Loaded authority plus any guard error envelope."""

    authority: CompiledSpecAuthority | None
    authority_fingerprint: str | None
    error: dict[str, Any] | None


class _StrictModel(BaseModel):
    """Base model for strict authority curation payloads."""

    model_config = ConfigDict(extra="forbid")


class AuthorityFeedbackItem(_StrictModel):
    """One structured feedback item targeted at authority content."""

    feedback_id: str = Field(min_length=1)
    target_kind: FeedbackTargetKind
    target_id: str | None = Field(default=None, min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    issue_type: FeedbackIssueType
    severity: FeedbackSeverity
    instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_concrete_target(self) -> AuthorityFeedbackItem:
        if self.target_id is None and self.source_item_id is None:
            msg = "target_id or source_item_id is required"
            raise ValueError(msg)
        return self


class AuthorityFeedbackFile(_StrictModel):
    """Canonical feedback file schema."""

    schema_version: Literal["agileforge.authority_feedback.v1"]
    authority_id: int
    feedback_items: list[AuthorityFeedbackItem] = Field(min_length=1)


class AuthorityFeedbackRecordRequest(_StrictModel):
    """CLI request for feedback recording."""

    project_id: int
    pending_authority_id: int
    expected_authority_fingerprint: str = Field(min_length=1)
    feedback_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"
    correlation_id: str | None = None


class AuthorityCurationRunner:
    """Run authority feedback and curation commands."""

    def __init__(self, *, engine: Engine, workflow: object | None = None) -> None:
        """Initialize the curation runner."""
        self._engine = engine
        self._workflow = workflow

    def feedback_record(
        self,
        request: AuthorityFeedbackRecordRequest,
    ) -> dict[str, Any]:
        """Record structured feedback for a pending authority."""
        feedback = _load_feedback_file(request.feedback_file)
        if not isinstance(feedback, AuthorityFeedbackFile):
            return feedback
        if feedback.authority_id != request.pending_authority_id:
            return _feedback_schema_invalid(
                message="Feedback authority_id does not match request.",
                details={
                    "feedback_authority_id": feedback.authority_id,
                    "pending_authority_id": request.pending_authority_id,
                },
            )

        with Session(self._engine) as session:
            return _record_feedback_in_session(
                session=session,
                request=request,
                feedback=feedback,
            )


def _record_feedback_in_session(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
    feedback: AuthorityFeedbackFile,
) -> dict[str, Any]:
    """Record validated feedback inside one database session."""
    payload = feedback.model_dump(mode="json")
    feedback_fingerprint = canonical_hash(payload)
    request_hash = _request_hash(
        request=request,
        feedback_fingerprint=feedback_fingerprint,
    )
    replay = _idempotency_replay(
        session=session,
        request=request,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    guard = _authority_guard(session=session, request=request)
    if guard.error is not None:
        return guard.error
    authority = cast("CompiledSpecAuthority", guard.authority)

    target_error = _feedback_target_error(feedback=feedback, authority=authority)
    if target_error is not None:
        return error_envelope(
            command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
            error=target_error,
            correlation_id=request.correlation_id,
        )

    row = _build_feedback_attempt(
        request=request,
        actual_fingerprint=guard.authority_fingerprint or "",
        feedback=feedback,
        feedback_fingerprint=feedback_fingerprint,
        request_hash=request_hash,
    )
    commit_conflict = _commit_feedback_attempt(
        session=session,
        request=request,
        request_hash=request_hash,
        row=row,
    )
    if commit_conflict is not None:
        return commit_conflict

    return success_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        data=_feedback_attempt_response(row),
        correlation_id=request.correlation_id,
    )


def _commit_feedback_attempt(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
    request_hash: str,
    row: AuthorityFeedbackAttempt,
) -> dict[str, Any] | None:
    """Commit a feedback row, replaying durable idempotency conflicts."""
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        replay = _idempotency_replay(
            session=session,
            request=request,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        return error_envelope(
            command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
            error=workbench_error(
                ErrorCode.MUTATION_FAILED,
                message="Authority feedback record conflicted during commit.",
                details={"idempotency_key": request.idempotency_key},
            ),
            correlation_id=request.correlation_id,
        )
    return None


def _authority_guard(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
) -> _AuthorityGuardResult:
    """Load authority and validate ownership plus expected fingerprint."""
    authority = session.get(CompiledSpecAuthority, request.pending_authority_id)
    if authority is None:
        return _AuthorityGuardResult(
            authority=None,
            authority_fingerprint=None,
            error=_authority_not_pending_error(
                request=request,
                message="Pending authority was not found.",
                details={"authority_id": request.pending_authority_id},
            ),
        )

    spec = session.get(SpecRegistry, authority.spec_version_id)
    authority_project_id = spec.product_id if spec is not None else None
    if authority_project_id != request.project_id:
        return _AuthorityGuardResult(
            authority=None,
            authority_fingerprint=None,
            error=_authority_not_pending_error(
                request=request,
                message="Pending authority does not belong to project.",
                details={
                    "project_id": request.project_id,
                    "authority_id": request.pending_authority_id,
                    "authority_project_id": authority_project_id,
                },
            ),
        )

    actual_fingerprint = pending_authority_fingerprint(authority)
    if actual_fingerprint != request.expected_authority_fingerprint:
        return _AuthorityGuardResult(
            authority=None,
            authority_fingerprint=None,
            error=error_envelope(
                command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
                error=workbench_error(
                    ErrorCode.STALE_AUTHORITY_VERSION,
                    message="Authority fingerprint changed.",
                    details={
                        "expected": request.expected_authority_fingerprint,
                        "actual": actual_fingerprint,
                    },
                ),
                correlation_id=request.correlation_id,
            ),
        )
    return _AuthorityGuardResult(
        authority=authority,
        authority_fingerprint=actual_fingerprint,
        error=None,
    )


def _authority_not_pending_error(
    *,
    request: AuthorityFeedbackRecordRequest,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Return an authority-not-pending error envelope."""
    return error_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_NOT_PENDING,
            message=message,
            details=details,
        ),
        correlation_id=request.correlation_id,
    )


def _feedback_target_error(
    *,
    feedback: AuthorityFeedbackFile,
    authority: CompiledSpecAuthority,
) -> WorkbenchError | None:
    """Return the first target validation error for feedback."""
    targets = _authority_targets_by_kind(authority)
    for item in feedback.feedback_items:
        target_error = _validate_feedback_target(item=item, targets=targets)
        if target_error is not None:
            return target_error
    return None


def _build_feedback_attempt(
    *,
    request: AuthorityFeedbackRecordRequest,
    actual_fingerprint: str,
    feedback: AuthorityFeedbackFile,
    feedback_fingerprint: str,
    request_hash: str,
) -> AuthorityFeedbackAttempt:
    """Build a feedback attempt row."""
    payload = feedback.model_dump(mode="json")
    now = datetime.now(UTC)
    return AuthorityFeedbackAttempt(
        project_id=request.project_id,
        feedback_attempt_id=f"feedback-{uuid4()}",
        source_authority_id=request.pending_authority_id,
        source_authority_fingerprint=actual_fingerprint,
        feedback_fingerprint=feedback_fingerprint,
        has_blocking_feedback=any(
            item.severity == "blocking" for item in feedback.feedback_items
        ),
        feedback_json=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        request_hash=request_hash,
        idempotency_key=request.idempotency_key,
        changed_by=request.changed_by,
        created_at=now,
        updated_at=now,
    )


def _load_feedback_file(path: str) -> AuthorityFeedbackFile | dict[str, Any]:
    """Load and validate a feedback file from disk."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return AuthorityFeedbackFile.model_validate(payload)
    except ValidationError as exc:
        return _feedback_schema_invalid(
            message="Authority feedback payload is invalid.",
            details={"validation_errors": _validation_error_details(exc)},
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _feedback_schema_invalid(
            message="Authority feedback payload is invalid.",
            details={"error": str(exc)},
        )


def _feedback_schema_invalid(
    *,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Return a structured invalid feedback payload error."""
    return error_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID,
            message=message,
            details=details,
        ),
    )


def _validation_error_details(exc: ValidationError) -> list[dict[str, Any]]:
    """Return Pydantic validation errors without raw input values."""
    return exc.errors(
        include_input=False,
        include_context=False,
        include_url=False,
    )


def _idempotency_replay(
    *,
    session: Session,
    request: AuthorityFeedbackRecordRequest,
    request_hash: str,
) -> dict[str, Any] | None:
    """Return replay/conflict envelope for an existing idempotency key."""
    existing = session.exec(
        select(AuthorityFeedbackAttempt)
        .where(AuthorityFeedbackAttempt.project_id == request.project_id)
        .where(AuthorityFeedbackAttempt.idempotency_key == request.idempotency_key)
    ).first()
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        return error_envelope(
            command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
            error=workbench_error(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                message="Idempotency key was reused with a different request.",
                details={"idempotency_key": request.idempotency_key},
            ),
            correlation_id=request.correlation_id,
        )
    return success_envelope(
        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
        data=_feedback_attempt_response(existing),
        correlation_id=request.correlation_id,
    )


def _feedback_attempt_response(row: AuthorityFeedbackAttempt) -> dict[str, Any]:
    """Return the feedback record success payload for a stored row."""
    return {
        "status": "authority_feedback_recorded",
        "project_id": row.project_id,
        "feedback_attempt_id": row.feedback_attempt_id,
        "source_authority_id": row.source_authority_id,
        "source_authority_fingerprint": row.source_authority_fingerprint,
        "feedback_fingerprint": row.feedback_fingerprint,
        "has_blocking_feedback": row.has_blocking_feedback,
    }


def _authority_targets_by_kind(authority: CompiledSpecAuthority) -> TargetIndex:
    """Return known target ids grouped by feedback target kind."""
    compiled = _json_from_column(authority.compiled_artifact_json)
    invariant_json = _json_from_column(authority.invariants)
    gap_json = _json_from_column(authority.spec_gaps)
    targets: TargetIndex = {
        "invariant": set(),
        "gap": set(),
        "assumption": set(),
        "quality_group": set(),
        "source_item": set(),
        "authority_candidate": {f"authority:{authority.authority_id}"},
    }
    targets["invariant"].update(
        _collect_ids_from_paths(
            [invariant_json, _dict_value(compiled, "invariants")],
            keys=("id", "invariant_id"),
        )
    )
    targets["gap"].update(
        _collect_ids_from_paths(
            [gap_json, _dict_value(compiled, "gaps")],
            keys=("id", "gap_id"),
        )
    )
    targets["assumption"].update(
        _collect_ids_from_paths(
            [_dict_value(compiled, "assumptions")],
            keys=("id", "assumption_id"),
        )
    )
    targets["quality_group"].update(
        _collect_ids_from_paths(
            [
                _dict_value(compiled, "quality_groups"),
                _dict_value(compiled, "review_groups"),
            ],
            keys=("id", "group_id"),
        )
    )
    targets["source_item"].update(_collect_source_item_ids(invariant_json))
    targets["source_item"].update(_collect_source_item_ids(compiled))
    return targets


def _json_from_column(raw_value: object) -> object:
    """Parse JSON stored in an authority text column."""
    if not raw_value:
        return None
    try:
        return json.loads(str(raw_value))
    except json.JSONDecodeError:
        return None


def _dict_value(value: object, key: str) -> object:
    """Return a dictionary value when the parsed JSON is an object."""
    if isinstance(value, dict):
        return value.get(key)
    return None


def _collect_ids_from_paths(
    values: list[object],
    *,
    keys: tuple[str, ...],
) -> set[str]:
    """Collect target ids from selected JSON branches."""
    found: set[str] = set()
    for value in values:
        found.update(_collect_ids(value, keys=keys))
    return found


def _validate_feedback_target(
    *,
    item: AuthorityFeedbackItem,
    targets: TargetIndex,
) -> WorkbenchError | None:
    """Validate target_id and source_item_id against kind-scoped target ids."""
    if item.target_id is not None and item.target_id not in targets[item.target_kind]:
        return workbench_error(
            ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND,
            message="Feedback target does not exist.",
            details={
                "target_kind": item.target_kind,
                "target_id": item.target_id,
            },
        )
    if (
        item.source_item_id is not None
        and item.source_item_id not in targets["source_item"]
    ):
        return workbench_error(
            ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND,
            message="Feedback source item does not exist.",
            details={"source_item_id": item.source_item_id},
        )
    return None


def _collect_ids(value: object, *, keys: tuple[str, ...]) -> set[str]:
    """Collect id-like strings from nested JSON."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key in keys:
            item_id = value.get(key)
            if isinstance(item_id, str) and item_id:
                found.add(item_id)
        for child in value.values():
            found.update(_collect_ids(child, keys=keys))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_ids(child, keys=keys))
    return found


def _collect_source_item_ids(value: object) -> set[str]:
    """Collect source item ids from nested authority JSON."""
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(_collect_direct_source_ids(value))
        source_map = value.get("source_map")
        if isinstance(source_map, list):
            for source_entry in source_map:
                found.update(_collect_source_map_ids(source_entry))
        found.update(_collect_child_source_ids(value))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_source_item_ids(child))
    return found


def _collect_direct_source_ids(value: dict[str, object]) -> set[str]:
    """Collect source item ids from direct source-related fields."""
    found: set[str] = set()
    for key in ("source_item_id", "spec_item_id", "item_id", "source_id"):
        item_id = value.get(key)
        if isinstance(item_id, str) and _looks_like_source_item_id(item_id):
            found.add(item_id)
    for key in ("location", "locations", "source_ref"):
        found.update(_collect_source_location_ids(value.get(key)))
    return found


def _collect_child_source_ids(value: dict[str, object]) -> set[str]:
    """Collect source item ids from child nodes except source_map."""
    found: set[str] = set()
    for child_key, child in value.items():
        if child_key != "source_map":
            found.update(_collect_source_item_ids(child))
    return found


def _collect_source_map_ids(value: object) -> set[str]:
    """Collect source item ids from source_map entries."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key in ("source_item_id", "spec_item_id", "item_id", "source_id", "id"):
            item_id = value.get(key)
            if isinstance(item_id, str) and _looks_like_source_item_id(item_id):
                found.add(item_id)
        for key in ("location", "locations", "source_ref"):
            found.update(_collect_source_location_ids(value.get(key)))
    return found


def _collect_source_location_ids(value: object) -> set[str]:
    """Collect source item ids from location values."""
    if isinstance(value, str) and _looks_like_source_item_id(value):
        return {value}
    if isinstance(value, list):
        return {
            item
            for item in value
            if isinstance(item, str) and _looks_like_source_item_id(item)
        }
    return set()


def _looks_like_source_item_id(value: str) -> bool:
    """Return whether a string looks like an AgileForge spec item id."""
    prefixes = (
        "SRC",
        "SPEC.",
        "REQ",
        "DECISION",
        "NON_GOAL",
        "RISK",
        "OPEN_QUESTION",
    )
    return value.startswith(prefixes)


def _request_hash(
    *,
    request: AuthorityFeedbackRecordRequest,
    feedback_fingerprint: str,
) -> str:
    """Return the stable request hash for feedback recording."""
    return canonical_hash(
        {
            "command": AUTHORITY_FEEDBACK_RECORD_COMMAND,
            "project_id": request.project_id,
            "pending_authority_id": request.pending_authority_id,
            "expected_authority_fingerprint": request.expected_authority_fingerprint,
            "feedback_fingerprint": feedback_fingerprint,
            "changed_by": request.changed_by,
        }
    )
