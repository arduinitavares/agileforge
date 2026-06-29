"""Validation and structured spec loading helpers for scope amendments."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from models.agent_workbench import DiscoverySpecAmendmentDraft
from models.core import Sprint, UserStory
from models.enums import SprintStatus, StoryStatus
from models.specs import SpecAuthorityAcceptance, SpecRegistry
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.specs.pending_authority_service import (
    PendingAuthorityResult,
    ensure_pending_spec_version_for_project,
)
from services.specs.profile_content import normalize_spec_content_for_registry
from utils.agileforge_spec_profile import TechnicalSpecArtifact

SCOPE_EXTENSION_AVAILABLE: str = "project_scope_extension_available"
SCOPE_EXTENSION_BLOCKED: str = "project_scope_extension_blocked"
SCOPE_EXTENSION_VALID: str = "project_scope_extension_valid"
SCOPE_EXTENSION_INVALID: str = "project_scope_extension_invalid"
SCOPE_EXTENSION_STARTED: str = "project_scope_extension_started"
AUTHORITY_COMPILE_REQUIRED: str = "authority_compile_required"

ERR_SCOPE_EXTENSION_NOT_AVAILABLE: str = "SCOPE_EXTENSION_NOT_AVAILABLE"
ERR_SCOPE_EXTENSION_NOT_ADDITIVE: str = "SCOPE_EXTENSION_NOT_ADDITIVE"
ERR_SCOPE_EXTENSION_NO_ADDED_ITEMS: str = "SCOPE_EXTENSION_NO_ADDED_ITEMS"
ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH: str = "SCOPE_EXTENSION_BASE_SPEC_MISMATCH"
ERR_SCOPE_EXTENSION_UNRESOLVED_WORK: str = "SCOPE_EXTENSION_UNRESOLVED_WORK"
DUPLICATE_SOURCE_ITEM_ID: str = "DUPLICATE_SOURCE_ITEM_ID"
DUPLICATE_RELATION_KEY: str = "DUPLICATE_RELATION_KEY"

ScopeExtensionArtifact = Mapping[str, Any] | TechnicalSpecArtifact
_ACCEPTANCE_PRODUCT_ID: Any = SpecAuthorityAcceptance.product_id
_ACCEPTANCE_STATUS: Any = SpecAuthorityAcceptance.status
_ACCEPTANCE_DECIDED_AT: Any = SpecAuthorityAcceptance.decided_at
_ACCEPTANCE_ID: Any = SpecAuthorityAcceptance.id
_SPEC_PRODUCT_ID: Any = SpecRegistry.product_id
_SPEC_VERSION_ID: Any = SpecRegistry.spec_version_id
_PENDING_APPROVAL_NOTES: str = (
    "Required compiler precondition for pending authority generation"
)
_RECOVERY_MARKER_PREFIX: str = "scope_extension_start_recovery="
_UNRESOLVED_WORK_BLOCKERS: frozenset[str] = frozenset(
    {"OPEN_STORY_EXISTS", "SPRINT_CANDIDATES_EXIST"}
)

SprintCandidateCountResolver = Callable[[int], int]


class ScopeExtensionValidateRequest(BaseModel):
    """Validated request for read-only scope-extension validation."""

    project_id: int
    spec_file: str = Field(min_length=1)
    base_spec_version_id: int | None = None


class ScopeExtensionStartRequest(BaseModel):
    """Validated request for guarded scope-extension start."""

    project_id: int
    spec_file: str | None = Field(default=None, min_length=1)
    base_spec_version_id: int | None = None
    spec_amendment_draft_id: int | None = None
    expected_state: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"

    def normalized_request_hash(self) -> str:
        """Return a stable hash for idempotent extension start."""
        request_data: dict[str, object] = {
            "command": "agileforge scope extension start",
            "project_id": self.project_id,
            "expected_state": self.expected_state,
            "changed_by": self.changed_by,
        }
        if self.spec_amendment_draft_id is not None:
            request_data["spec_amendment_draft_id"] = self.spec_amendment_draft_id
        if self.spec_file is not None:
            request_data["spec_file"] = str(Path(self.spec_file).expanduser())
        if self.base_spec_version_id is not None:
            request_data["base_spec_version_id"] = self.base_spec_version_id
        return canonical_hash(request_data)


@dataclass(frozen=True)
class ScopeExtensionIssue:
    """One deterministic scope-extension validation issue."""

    code: str
    message: str
    source_item_id: str | None = None
    relation_key: str | None = None


@dataclass(frozen=True)
class ScopeExtensionValidation:
    """Result of additive spec amendment validation."""

    ok: bool
    added_source_item_ids: list[str]
    removed_source_item_ids: list[str]
    modified_source_item_ids: list[str]
    blocking_issues: list[ScopeExtensionIssue]


@dataclass(frozen=True)
class ScopeExtensionPreconditions:
    """Read-only scope-extension availability decision."""

    status: str
    available: bool
    blocking_reason: str | None = None


@dataclass(frozen=True)
class _ScopeExtensionRecoveryMarkerMetadata:
    """Base metadata stored in the scope-extension recovery marker."""

    base_spec_version_id: int
    base_spec_hash: str
    added_source_item_ids: list[str]


@dataclass(frozen=True)
class _ResolvedScopeExtensionStart:
    """Resolved start inputs after optional discovery artifact lookup."""

    spec_file: str
    base_spec_version_id: int
    spec_amendment: DiscoverySpecAmendmentDraft | None = None


class ScopeExtensionWorkflowPort(Protocol):
    """Workflow operations required by scope extension start."""

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return workflow session state."""
        raise NotImplementedError

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge a partial workflow state update."""
        raise NotImplementedError


def _zero_sprint_candidate_count(_project_id: int) -> int:
    """Return no remaining Sprint candidates for direct runner use."""
    return 0


class ScopeExtensionRunner:
    """Validate and start additive project scope extensions."""

    def __init__(
        self,
        *,
        session: Session,
        workflow_service: ScopeExtensionWorkflowPort,
        sprint_candidate_count_resolver: SprintCandidateCountResolver | None = None,
    ) -> None:
        """Initialize the runner with storage and workflow ports."""
        self._session = session
        self._workflow_service = workflow_service
        self._sprint_candidate_count_resolver = (
            sprint_candidate_count_resolver or _zero_sprint_candidate_count
        )

    def validate(self, request: ScopeExtensionValidateRequest) -> dict[str, Any]:
        """Validate an amended spec against the latest accepted base spec."""
        base_spec = self._latest_accepted_base_spec(request.project_id)
        if base_spec is None:
            return _error(
                ErrorCode.SPEC_VERSION_NOT_FOUND.value,
                details={"project_id": request.project_id},
                remediation=[
                    "Accept a base project spec authority before extending scope."
                ],
            )

        if (
            request.base_spec_version_id is not None
            and request.base_spec_version_id != base_spec.spec_version_id
        ):
            return _error(
                ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH,
                details={
                    "project_id": request.project_id,
                    "expected_base_spec_version_id": request.base_spec_version_id,
                    "latest_base_spec_version_id": base_spec.spec_version_id,
                },
                remediation=["Refresh the latest accepted base spec before retrying."],
            )

        amended = self._load_amended_spec(request)
        if isinstance(amended, dict):
            return amended
        amended_artifact, amended_spec_hash = amended
        base_artifact = TechnicalSpecArtifact.model_validate_json(base_spec.content)
        validation = validate_additive_scope_extension(
            base_artifact,
            amended_artifact,
        )
        data = _validation_data(
            project_id=request.project_id,
            base_spec=base_spec,
            amended_spec_hash=amended_spec_hash,
            validation=validation,
        )
        return _success(data)

    def start(self, request: ScopeExtensionStartRequest) -> dict[str, Any]:
        """Start guarded setup for a valid additive scope extension."""
        session_id = str(request.project_id)
        workflow_state = self._workflow_service.get_session_status(session_id) or {}
        resolved, resolve_error = self._resolve_start_request(request)
        if resolve_error is None:
            resolved = cast("_ResolvedScopeExtensionStart", resolved)
            validation_result = self.validate(
                ScopeExtensionValidateRequest(
                    project_id=request.project_id,
                    spec_file=resolved.spec_file,
                    base_spec_version_id=resolved.base_spec_version_id,
                )
            )
            validation_data, validation_response = _start_validation_data_or_response(
                validation_result=validation_result,
                request=request,
                workflow_state=workflow_state,
            )
        else:
            validation_data = {}
            validation_response = resolve_error
        if validation_response is not None:
            return validation_response
        resolved = cast("_ResolvedScopeExtensionStart", resolved)

        current_state = str(workflow_state.get("fsm_state") or "")
        if current_state != request.expected_state:
            return _error(
                ErrorCode.STALE_STATE.value,
                details={
                    "project_id": request.project_id,
                    "expected_state": request.expected_state,
                    "actual_state": current_state,
                },
                remediation=["Refresh scope extension stale guards before retrying."],
            )

        preconditions = evaluate_scope_extension_preconditions(
            session=self._session,
            product_id=request.project_id,
            workflow_state=workflow_state,
            sprint_candidate_count=self._sprint_candidate_count_resolver(
                request.project_id
            ),
        )
        start_error = _start_precondition_error(preconditions, request)

        resolved_spec_path = Path(resolved.spec_file).expanduser().resolve()
        request_fingerprint = _start_request_fingerprint(
            request,
            amended_spec_hash=str(validation_data["amended_spec_hash"]),
        )
        if start_error is None:
            start_error = self._recovery_marker_conflict(
                request=request,
                request_fingerprint=request_fingerprint,
            )
        if start_error is not None:
            return start_error

        pending = ensure_pending_spec_version_for_project(
            session=self._session,
            product_id=request.project_id,
            spec_path=resolved_spec_path,
            approved_by=request.changed_by,
            lease_guard=lambda _boundary: True,
            record_progress=lambda _boundary: True,
        )
        pending_error = _pending_start_error(pending, request)
        if pending_error is not None:
            return pending_error
        spec_hash = pending.spec_hash or ""
        spec_version_id = pending.spec_version_id or 0
        self._persist_recovery_marker(
            request=request,
            spec_version_id=spec_version_id,
            resolved_spec_path=resolved_spec_path,
            request_fingerprint=request_fingerprint,
            metadata=_ScopeExtensionRecoveryMarkerMetadata(
                base_spec_version_id=int(validation_data["base_spec_version_id"]),
                base_spec_hash=str(validation_data["base_spec_hash"]),
                added_source_item_ids=[
                    str(item_id)
                    for item_id in validation_data["added_source_item_ids"]
                ],
            ),
        )

        context = {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": validation_data["base_spec_version_id"],
            "base_spec_hash": validation_data["base_spec_hash"],
            "amended_spec_version_id": spec_version_id,
            "amended_spec_hash": spec_hash,
            "added_source_item_ids": validation_data["added_source_item_ids"],
            "idempotency_key": request.idempotency_key,
            "request_fingerprint": request_fingerprint,
            "spec_file": str(resolved_spec_path),
        }
        if resolved.spec_amendment is not None:
            context.update(
                {
                    "spec_amendment_draft_id": (
                        resolved.spec_amendment.spec_amendment_draft_id
                    ),
                    "prd_id": resolved.spec_amendment.prd_id,
                    "challenge_artifact_id": (
                        resolved.spec_amendment.challenge_artifact_id
                    ),
                }
            )
        next_actions = [
            _authority_compile_action(
                project_id=request.project_id,
                spec_version_id=spec_version_id,
                spec_hash=spec_hash,
                expected_setup_status=AUTHORITY_COMPILE_REQUIRED,
            )
        ]
        workflow_update = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": AUTHORITY_COMPILE_REQUIRED,
            "setup_error": None,
            "setup_spec_file_path": str(resolved_spec_path),
            "setup_spec_hash": spec_hash,
            "setup_spec_version_id": spec_version_id,
            "setup_next_actions": next_actions,
            "scope_extension_context": context,
        }
        try:
            self._workflow_service.update_session_status(session_id, workflow_update)
        except Exception as exc:  # noqa: BLE001
            return _error(
                ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
                details={
                    "project_id": request.project_id,
                    "spec_version_id": spec_version_id,
                    "spec_hash": spec_hash,
                    "workflow_error": str(exc),
                },
                remediation=["Retry the same scope extension start request."],
            )
        return _started_response(
            project_id=request.project_id,
            setup_status=AUTHORITY_COMPILE_REQUIRED,
            spec_version_id=spec_version_id,
            context=context,
            next_actions=next_actions,
        )

    def _resolve_start_request(
        self,
        request: ScopeExtensionStartRequest,
    ) -> tuple[_ResolvedScopeExtensionStart | None, dict[str, Any] | None]:
        if request.spec_amendment_draft_id is not None:
            return self._resolve_spec_amendment_start_request(request)
        if request.spec_file is None or request.base_spec_version_id is None:
            return (
                None,
                _error(
                    ErrorCode.INVALID_COMMAND.value,
                    details={
                        "required_alternatives": [
                            ["spec_amendment_draft_id"],
                            ["spec_file", "base_spec_version_id"],
                        ]
                    },
                    remediation=[
                        "Start from an accepted Spec Amendment Draft "
                        "in the guided flow."
                    ],
                ),
            )
        return (
            _ResolvedScopeExtensionStart(
                spec_file=request.spec_file,
                base_spec_version_id=request.base_spec_version_id,
            ),
            None,
        )

    def _resolve_spec_amendment_start_request(
        self,
        request: ScopeExtensionStartRequest,
    ) -> tuple[_ResolvedScopeExtensionStart | None, dict[str, Any] | None]:
        draft = self._session.get(
            DiscoverySpecAmendmentDraft,
            request.spec_amendment_draft_id,
        )
        if draft is None or draft.project_id != request.project_id:
            return (
                None,
                _error(
                    ErrorCode.SPEC_AMENDMENT_NOT_FOUND.value,
                    details={
                        "project_id": request.project_id,
                        "spec_amendment_draft_id": request.spec_amendment_draft_id,
                    },
                    remediation=[
                        "Pass an accepted Spec Amendment Draft for this project."
                    ],
                ),
            )
        if draft.status != "accepted":
            return (
                None,
                _error(
                    ErrorCode.SPEC_AMENDMENT_NOT_ACCEPTED.value,
                    details={
                        "project_id": request.project_id,
                        "spec_amendment_draft_id": request.spec_amendment_draft_id,
                        "status": draft.status,
                    },
                    remediation=[
                        "Accept the validated Spec Amendment Draft "
                        "before starting scope extension."
                    ],
                ),
            )
        if draft.base_spec_version_id is None:
            return (
                None,
                _error(
                    ErrorCode.SPEC_AMENDMENT_NOT_ACCEPTED.value,
                    details={
                        "project_id": request.project_id,
                        "spec_amendment_draft_id": request.spec_amendment_draft_id,
                        "reason": "missing_base_spec_version_id",
                    },
                    remediation=[
                        "Record and accept a validated Spec Amendment Draft "
                        "with base spec provenance."
                    ],
                ),
            )
        if (
            request.spec_file is not None
            and Path(request.spec_file).expanduser().resolve()
            != Path(draft.amendment_file).expanduser().resolve()
        ):
            return (
                None,
                _error(
                    ErrorCode.INVALID_COMMAND.value,
                    details={
                        "spec_amendment_draft_id": request.spec_amendment_draft_id,
                        "spec_file": request.spec_file,
                        "amendment_file": draft.amendment_file,
                    },
                    remediation=[
                        "Omit --spec-file when starting from a Spec Amendment Draft."
                    ],
                ),
            )
        if (
            request.base_spec_version_id is not None
            and request.base_spec_version_id != draft.base_spec_version_id
        ):
            return (
                None,
                _error(
                    ErrorCode.SCOPE_EXTENSION_BASE_SPEC_MISMATCH.value,
                    details={
                        "project_id": request.project_id,
                        "expected_base_spec_version_id": request.base_spec_version_id,
                        "spec_amendment_base_spec_version_id": (
                            draft.base_spec_version_id
                        ),
                    },
                    remediation=[
                        "Omit --base-spec-version-id when starting "
                        "from a Spec Amendment Draft."
                    ],
                ),
            )
        return (
            _ResolvedScopeExtensionStart(
                spec_file=draft.amendment_file,
                base_spec_version_id=draft.base_spec_version_id,
                spec_amendment=draft,
            ),
            None,
        )

    def _latest_accepted_base_spec(self, project_id: int) -> SpecRegistry | None:
        accepted = self._session.exec(
            select(SpecAuthorityAcceptance)
            .where(project_id == _ACCEPTANCE_PRODUCT_ID)
            .where(_ACCEPTANCE_STATUS == "accepted")
            .order_by(_ACCEPTANCE_DECIDED_AT.desc(), _ACCEPTANCE_ID.desc())
        ).first()
        if accepted is None:
            return None
        return self._session.get(SpecRegistry, accepted.spec_version_id)

    def _load_amended_spec(
        self,
        request: ScopeExtensionValidateRequest,
    ) -> tuple[TechnicalSpecArtifact, str] | dict[str, Any]:
        try:
            artifact, _content, spec_hash = load_structured_spec_file(
                request.spec_file
            )
        except FileNotFoundError:
            return _error(
                ErrorCode.SPEC_FILE_NOT_FOUND.value,
                details={"spec_file": request.spec_file},
                remediation=["Provide an existing amended spec file path."],
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return _error(
                ErrorCode.SPEC_FILE_INVALID.value,
                details={"spec_file": request.spec_file, "error": str(exc)},
                remediation=["Provide a valid structured AgileForge spec file."],
            )
        return artifact, spec_hash

    def _recovery_marker_conflict(
        self,
        *,
        request: ScopeExtensionStartRequest,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        marker = _latest_recovery_marker(
            self._session,
            project_id=request.project_id,
            idempotency_key=request.idempotency_key,
        )
        if marker is None or marker.get("request_fingerprint") == request_fingerprint:
            return None
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            details={
                "project_id": request.project_id,
                "idempotency_key": request.idempotency_key,
            },
            remediation=["Use a new idempotency key for changed inputs."],
        )

    def _persist_recovery_marker(
        self,
        *,
        request: ScopeExtensionStartRequest,
        spec_version_id: int,
        resolved_spec_path: Path,
        request_fingerprint: str,
        metadata: _ScopeExtensionRecoveryMarkerMetadata,
    ) -> None:
        spec = self._session.get(SpecRegistry, spec_version_id)
        if spec is None:
            return
        marker = {
            "idempotency_key": request.idempotency_key,
            "request_fingerprint": request_fingerprint,
            "spec_file": str(resolved_spec_path),
            "base_spec_version_id": metadata.base_spec_version_id,
            "base_spec_hash": metadata.base_spec_hash,
            "added_source_item_ids": list(metadata.added_source_item_ids),
        }
        spec.approval_notes = _recovery_notes(marker)
        self._session.add(spec)
        self._session.commit()


def _available_scope_extension_preconditions() -> ScopeExtensionPreconditions:
    return ScopeExtensionPreconditions(
        status=SCOPE_EXTENSION_AVAILABLE,
        available=True,
    )


def _blocked_scope_extension_preconditions(
    reason: str,
) -> ScopeExtensionPreconditions:
    return ScopeExtensionPreconditions(
        status=SCOPE_EXTENSION_BLOCKED,
        available=False,
        blocking_reason=reason,
    )


def _sprint_exists(
    session: Session,
    product_id: int,
    status: SprintStatus,
) -> bool:
    return (
        session.exec(
            select(Sprint).where(
                Sprint.product_id == product_id,
                Sprint.status == status,
            )
        ).first()
        is not None
    )


def _story_is_terminal(story: UserStory) -> bool:
    return (
        story.status in {StoryStatus.DONE, StoryStatus.ACCEPTED}
        or story.is_superseded
        or story.archived_reason is not None
    )


def _open_story_exists(session: Session, product_id: int) -> bool:
    stories = session.exec(
        select(UserStory).where(UserStory.product_id == product_id)
    ).all()
    return any(not _story_is_terminal(story) for story in stories)


def evaluate_scope_extension_preconditions(
    *,
    session: Session,
    product_id: int,
    workflow_state: Mapping[str, Any],
    sprint_candidate_count: int,
) -> ScopeExtensionPreconditions:
    """Return whether project scope extension is available from current state."""
    if workflow_state.get("fsm_state") != "SPRINT_COMPLETE":
        return _blocked_scope_extension_preconditions("FSM_STATE_NOT_SPRINT_COMPLETE")

    if _sprint_exists(session, product_id, SprintStatus.ACTIVE):
        return _blocked_scope_extension_preconditions("ACTIVE_SPRINT_EXISTS")

    if _sprint_exists(session, product_id, SprintStatus.PLANNED):
        return _blocked_scope_extension_preconditions("PLANNED_SPRINT_EXISTS")

    if _open_story_exists(session, product_id):
        return _blocked_scope_extension_preconditions("OPEN_STORY_EXISTS")

    if sprint_candidate_count != 0:
        return _blocked_scope_extension_preconditions("SPRINT_CANDIDATES_EXIST")

    return _available_scope_extension_preconditions()


def _fingerprint_value(value: object) -> object:
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            normalized_item = _fingerprint_value(item)
            if _is_absent_equivalent(normalized_item):
                continue
            normalized[str(key)] = normalized_item
        return normalized
    if isinstance(value, list):
        return [_fingerprint_value(item) for item in value]
    return value


def _is_absent_equivalent(value: object) -> bool:
    return value is None or value in ([], {})


def _canonical_item_fingerprint(item: Mapping[str, Any]) -> str:
    return canonical_hash(_fingerprint_value(item))


def _relation_key(relation: Mapping[str, Any]) -> str:
    return "::".join(
        [
            str(relation.get("from", "")),
            str(relation.get("type", "")),
            str(relation.get("to", "")),
        ]
    )


def _canonical_relation_fingerprint(relation: Mapping[str, Any]) -> str:
    return canonical_hash(_fingerprint_value(relation))


def _artifact_mapping(artifact: ScopeExtensionArtifact) -> Mapping[str, Any]:
    if isinstance(artifact, TechnicalSpecArtifact):
        return artifact.model_dump(mode="json", by_alias=True)
    return artifact


def _items_by_id(artifact: ScopeExtensionArtifact) -> dict[str, Mapping[str, Any]]:
    items = _artifact_mapping(artifact).get("items")
    if not isinstance(items, list):
        return {}
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            by_id.setdefault(str(item["id"]), item)
    return by_id


def _duplicate_source_item_ids(artifact: ScopeExtensionArtifact) -> list[str]:
    items = _artifact_mapping(artifact).get("items")
    if not isinstance(items, list):
        return []

    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        item_id = str(item["id"])
        if item_id in seen:
            duplicates.add(item_id)
        seen.add(item_id)
    return sorted(duplicates)


def _relations_by_key(artifact: ScopeExtensionArtifact) -> dict[str, Mapping[str, Any]]:
    relations = _artifact_mapping(artifact).get("relations")
    if not isinstance(relations, list):
        return {}
    by_key: dict[str, Mapping[str, Any]] = {}
    for relation in relations:
        if isinstance(relation, Mapping):
            by_key.setdefault(_relation_key(relation), relation)
    return by_key


def _duplicate_relation_keys(artifact: ScopeExtensionArtifact) -> list[str]:
    relations = _artifact_mapping(artifact).get("relations")
    if not isinstance(relations, list):
        return []

    seen: set[str] = set()
    duplicates: set[str] = set()
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        key = _relation_key(relation)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return sorted(duplicates)


def validate_additive_scope_extension(
    base_artifact: ScopeExtensionArtifact,
    amended_artifact: ScopeExtensionArtifact,
) -> ScopeExtensionValidation:
    """Validate that amended spec only adds source items and relations."""
    base_items = _items_by_id(base_artifact)
    amended_items = _items_by_id(amended_artifact)
    base_relations = _relations_by_key(base_artifact)
    amended_relations = _relations_by_key(amended_artifact)

    added = sorted(set(amended_items) - set(base_items))
    removed = sorted(set(base_items) - set(amended_items))
    modified = sorted(
        item_id
        for item_id in set(base_items) & set(amended_items)
        if _canonical_item_fingerprint(base_items[item_id])
        != _canonical_item_fingerprint(amended_items[item_id])
    )

    issues: list[ScopeExtensionIssue] = []
    issues.extend(
        ScopeExtensionIssue(
            code=DUPLICATE_SOURCE_ITEM_ID,
            message="Base spec contains duplicate source item IDs.",
            source_item_id=item_id,
        )
        for item_id in _duplicate_source_item_ids(base_artifact)
    )
    issues.extend(
        ScopeExtensionIssue(
            code=DUPLICATE_SOURCE_ITEM_ID,
            message="Amended spec contains duplicate source item IDs.",
            source_item_id=item_id,
        )
        for item_id in _duplicate_source_item_ids(amended_artifact)
    )
    issues.extend(
        ScopeExtensionIssue(
            code=DUPLICATE_RELATION_KEY,
            message="Base spec contains duplicate relation identity keys.",
            relation_key=key,
        )
        for key in _duplicate_relation_keys(base_artifact)
    )
    issues.extend(
        ScopeExtensionIssue(
            code=DUPLICATE_RELATION_KEY,
            message="Amended spec contains duplicate relation identity keys.",
            relation_key=key,
        )
        for key in _duplicate_relation_keys(amended_artifact)
    )
    issues.extend(
        ScopeExtensionIssue(
            code="EXISTING_SOURCE_ITEM_REMOVED",
            message="Existing accepted source item is missing from amended spec.",
            source_item_id=item_id,
        )
        for item_id in removed
    )
    issues.extend(
        ScopeExtensionIssue(
            code="EXISTING_SOURCE_ITEM_MODIFIED",
            message="Existing accepted source item changed in amended spec.",
            source_item_id=item_id,
        )
        for item_id in modified
    )
    issues.extend(
        ScopeExtensionIssue(
            code="EXISTING_RELATION_REMOVED",
            message="Existing relation is missing from amended spec.",
            relation_key=key,
        )
        for key in sorted(set(base_relations) - set(amended_relations))
    )
    issues.extend(
        ScopeExtensionIssue(
            code="EXISTING_RELATION_MODIFIED",
            message="Existing relation changed in amended spec.",
            relation_key=key,
        )
        for key in sorted(set(base_relations) & set(amended_relations))
        if _canonical_relation_fingerprint(base_relations[key])
        != _canonical_relation_fingerprint(amended_relations[key])
    )
    if not added:
        issues.append(
            ScopeExtensionIssue(
                code="NO_ADDED_SOURCE_ITEMS",
                message="Scope extension must add at least one source item.",
            )
        )

    return ScopeExtensionValidation(
        ok=not issues,
        added_source_item_ids=added,
        removed_source_item_ids=removed,
        modified_source_item_ids=modified,
        blocking_issues=issues,
    )


def load_structured_spec_file(path: str) -> tuple[TechnicalSpecArtifact, str, str]:
    """Load, normalize, and parse a structured AgileForge spec file."""
    raw = Path(path).expanduser().resolve().read_text(encoding="utf-8")
    normalized = normalize_spec_content_for_registry(raw)
    payload = json.loads(normalized.content)
    return (
        TechnicalSpecArtifact.model_validate(payload),
        normalized.content,
        normalized.spec_hash,
    )


def _validation_data(
    *,
    project_id: int,
    base_spec: SpecRegistry,
    amended_spec_hash: str,
    validation: ScopeExtensionValidation,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "status": SCOPE_EXTENSION_VALID if validation.ok else SCOPE_EXTENSION_INVALID,
        "base_spec_version_id": base_spec.spec_version_id,
        "base_spec_hash": base_spec.spec_hash,
        "amended_spec_hash": amended_spec_hash,
        "added_source_item_ids": validation.added_source_item_ids,
        "removed_source_item_ids": validation.removed_source_item_ids,
        "modified_source_item_ids": validation.modified_source_item_ids,
        "blocking_issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "source_item_id": issue.source_item_id,
                "relation_key": issue.relation_key,
            }
            for issue in validation.blocking_issues
        ],
        "valid": validation.ok,
    }


def _authority_compile_action(
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
    expected_setup_status: str,
) -> dict[str, Any]:
    return {
        "command": "agileforge authority compile",
        "args": {
            "project_id": project_id,
            "spec_version_id": spec_version_id,
            "expected_spec_hash": spec_hash,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": expected_setup_status,
        },
        "reason": "Compile pending authority before authority review.",
    }


def _scope_extension_replay_response(
    *,
    request: ScopeExtensionStartRequest,
    workflow_state: Mapping[str, Any],
    amended_spec_hash: str,
) -> dict[str, Any] | None:
    context = workflow_state.get("scope_extension_context")
    if not isinstance(context, Mapping):
        return None
    if context.get("idempotency_key") != request.idempotency_key:
        return None
    request_fingerprint = _start_request_fingerprint(
        request,
        amended_spec_hash=amended_spec_hash,
    )
    if context.get("request_fingerprint") != request_fingerprint:
        return _error(
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            details={
                "project_id": request.project_id,
                "idempotency_key": request.idempotency_key,
            },
            remediation=["Use a new idempotency key for changed inputs."],
        )

    spec_version_id = workflow_state.get("setup_spec_version_id")
    if spec_version_id is None:
        spec_version_id = context.get("amended_spec_version_id")
    setup_status = str(workflow_state.get("setup_status") or AUTHORITY_COMPILE_REQUIRED)
    next_actions = workflow_state.get("setup_next_actions")
    return _started_response(
        project_id=request.project_id,
        setup_status=setup_status,
        spec_version_id=int(str(spec_version_id)),
        context=dict(context),
        next_actions=next_actions if isinstance(next_actions, list) else [],
    )


def _start_validation_data_or_response(
    *,
    validation_result: dict[str, Any],
    request: ScopeExtensionStartRequest,
    workflow_state: Mapping[str, Any],
) -> tuple[Mapping[str, Any], dict[str, Any] | None]:
    if not validation_result["ok"]:
        return {}, validation_result
    validation_data = validation_result["data"]
    replay = _scope_extension_replay_response(
        request=request,
        workflow_state=workflow_state,
        amended_spec_hash=str(validation_data["amended_spec_hash"]),
    )
    if replay is not None:
        return {}, replay
    if not validation_data["valid"]:
        return {}, _invalid_start_error(validation_data)
    return validation_data, None


def _start_request_fingerprint(
    request: ScopeExtensionStartRequest,
    *,
    amended_spec_hash: str,
) -> str:
    return canonical_hash(
        {
            "request": request.normalized_request_hash(),
            "amended_spec_hash": amended_spec_hash,
        }
    )


def _latest_recovery_marker(
    session: Session,
    *,
    project_id: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    rows = session.exec(
        select(SpecRegistry)
        .where(project_id == _SPEC_PRODUCT_ID)
        .order_by(_SPEC_VERSION_ID.desc())
    ).all()
    for row in rows:
        marker = _recovery_marker_from_notes(row.approval_notes)
        if marker.get("idempotency_key") == idempotency_key:
            return marker
    return None


def _recovery_marker_from_notes(notes: str | None) -> dict[str, Any]:
    if not notes:
        return {}
    for line in notes.splitlines():
        if line.startswith(_RECOVERY_MARKER_PREFIX):
            try:
                payload = json.loads(line.removeprefix(_RECOVERY_MARKER_PREFIX))
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _recovery_notes(marker: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            _PENDING_APPROVAL_NOTES,
            _RECOVERY_MARKER_PREFIX
            + json.dumps(dict(marker), sort_keys=True, separators=(",", ":")),
        ]
    )


def _invalid_start_error(validation_data: Mapping[str, Any]) -> dict[str, Any]:
    raw_issues = validation_data.get("blocking_issues")
    issues = raw_issues if raw_issues is not None else []
    issue_codes = {
        str(issue.get("code"))
        for issue in issues
        if isinstance(issue, Mapping) and issue.get("code") is not None
    }
    code = (
        ERR_SCOPE_EXTENSION_NO_ADDED_ITEMS
        if issue_codes == {"NO_ADDED_SOURCE_ITEMS"}
        else ERR_SCOPE_EXTENSION_NOT_ADDITIVE
    )
    return _error(
        code,
        details={
            "project_id": validation_data.get("project_id"),
            "base_spec_version_id": validation_data.get("base_spec_version_id"),
            "blocking_issues": list(issues) if isinstance(issues, list) else [],
        },
        remediation=["Provide an amended spec that only adds accepted scope items."],
    )


def _start_precondition_error(
    preconditions: ScopeExtensionPreconditions,
    request: ScopeExtensionStartRequest,
) -> dict[str, Any] | None:
    if preconditions.available:
        return None
    blocking_reason = preconditions.blocking_reason or "UNKNOWN"
    unresolved_work = blocking_reason in _UNRESOLVED_WORK_BLOCKERS
    return _error(
        (
            ERR_SCOPE_EXTENSION_UNRESOLVED_WORK
            if unresolved_work
            else ERR_SCOPE_EXTENSION_NOT_AVAILABLE
        ),
        details={
            "project_id": request.project_id,
            "status": preconditions.status,
            "blocking_reason": blocking_reason,
        },
        remediation=_start_precondition_remediation(
            blocking_reason,
            unresolved_work=unresolved_work,
        ),
    )


def _start_precondition_remediation(
    blocking_reason: str,
    *,
    unresolved_work: bool,
) -> list[str]:
    if blocking_reason == "OPEN_STORY_EXISTS":
        return [
            (
                "Reconcile each open Story first, for example: agileforge story "
                "reconcile --project-id <project_id> --story-id <story_id> "
                "--action archive --reason <reason> --idempotency-key <key>."
            )
        ]
    if unresolved_work:
        return [
            "Complete, archive, or refine unresolved work before starting a "
            "scope extension."
        ]
    return [
        "Refresh workflow state and retry after current Sprint activity is complete."
    ]


def _pending_start_error(
    pending: PendingAuthorityResult,
    request: ScopeExtensionStartRequest,
) -> dict[str, Any] | None:
    if not pending.ok:
        error_code = pending.error_code or ErrorCode.MUTATION_FAILED.value
        if error_code == "PRODUCT_NOT_FOUND":
            error_code = ErrorCode.PROJECT_NOT_FOUND.value
        return _error(
            error_code,
            details={
                "project_id": request.project_id,
                "spec_file": pending.spec_path,
                "spec_hash": pending.spec_hash,
            },
            remediation=["Fix the pending spec registration error and retry."],
        )
    if pending.spec_hash is None or pending.spec_version_id is None:
        return _error(
            ErrorCode.MUTATION_FAILED.value,
            details={"project_id": request.project_id},
            remediation=["Retry scope extension start."],
        )
    return None


def _started_response(
    *,
    project_id: int,
    setup_status: str,
    spec_version_id: int,
    context: dict[str, Any],
    next_actions: list[Any],
) -> dict[str, Any]:
    return _success(
        {
            "project_id": project_id,
            "status": SCOPE_EXTENSION_STARTED,
            "setup_status": setup_status,
            "spec_version_id": spec_version_id,
            "scope_extension_context": context,
            "next_actions": next_actions,
        }
    )


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error(
    code: str,
    *,
    details: dict[str, Any],
    remediation: list[str],
) -> dict[str, Any]:
    try:
        error = workbench_error(
            code,
            details=details,
            remediation=remediation,
        ).to_dict()
    except (KeyError, ValueError):
        error = {
            "code": code,
            "message": "Command failed.",
            "details": details,
            "remediation": remediation,
            "exit_code": 1,
            "retryable": False,
        }
    return {"ok": False, "data": None, "warnings": [], "errors": [error]}
