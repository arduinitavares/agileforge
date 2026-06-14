"""Validation and structured spec loading helpers for scope amendments."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from models.core import Sprint, UserStory
from models.enums import SprintStatus, StoryStatus
from services.agent_workbench.fingerprints import canonical_hash
from services.specs.profile_content import normalize_spec_content_for_registry
from utils.agileforge_spec_profile import TechnicalSpecArtifact

SCOPE_EXTENSION_AVAILABLE: str = "project_scope_extension_available"
SCOPE_EXTENSION_BLOCKED: str = "project_scope_extension_blocked"
SCOPE_EXTENSION_VALID: str = "project_scope_extension_valid"
SCOPE_EXTENSION_INVALID: str = "project_scope_extension_invalid"
SCOPE_EXTENSION_STARTED: str = "project_scope_extension_started"

ERR_SCOPE_EXTENSION_NOT_AVAILABLE: str = "SCOPE_EXTENSION_NOT_AVAILABLE"
ERR_SCOPE_EXTENSION_NOT_ADDITIVE: str = "SCOPE_EXTENSION_NOT_ADDITIVE"
ERR_SCOPE_EXTENSION_NO_ADDED_ITEMS: str = "SCOPE_EXTENSION_NO_ADDED_ITEMS"
ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH: str = "SCOPE_EXTENSION_BASE_SPEC_MISMATCH"
ERR_SCOPE_EXTENSION_UNRESOLVED_WORK: str = "SCOPE_EXTENSION_UNRESOLVED_WORK"
DUPLICATE_SOURCE_ITEM_ID: str = "DUPLICATE_SOURCE_ITEM_ID"
DUPLICATE_RELATION_KEY: str = "DUPLICATE_RELATION_KEY"

ScopeExtensionArtifact = Mapping[str, Any] | TechnicalSpecArtifact


class ScopeExtensionValidateRequest(BaseModel):
    """Validated request for read-only scope-extension validation."""

    project_id: int
    spec_file: str = Field(min_length=1)
    base_spec_version_id: int | None = None


class ScopeExtensionStartRequest(BaseModel):
    """Validated request for guarded scope-extension start."""

    project_id: int
    spec_file: str = Field(min_length=1)
    base_spec_version_id: int
    expected_state: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"

    def normalized_request_hash(self) -> str:
        """Return a stable hash for idempotent extension start."""
        return canonical_hash(
            {
                "command": "agileforge scope extension start",
                "project_id": self.project_id,
                "spec_file": str(Path(self.spec_file).expanduser()),
                "base_spec_version_id": self.base_spec_version_id,
                "expected_state": self.expected_state,
                "changed_by": self.changed_by,
            }
        )


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
