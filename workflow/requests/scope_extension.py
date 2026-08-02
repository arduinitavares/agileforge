"""Typed requests for optional Project scope extension."""

from typing import ClassVar, Literal

from workflow.contracts import FrozenModel, JsonObject
from workflow.requests.base import PositionedRequest


class ScopeExtensionArtifactReference(FrozenModel):
    """One downstream accepted artifact reconciled to replacement authority."""

    artifact_type: Literal["vision", "backlog", "roadmap", "story"]
    artifact_id: int
    artifact_fingerprint: str


class StartScopeExtension(PositionedRequest):
    """Open one extension run pinned to the accepted current base spec."""

    kind: Literal["start_scope_extension"] = "start_scope_extension"
    node_id: ClassVar[str] = "scope_extension.start"
    base_spec_version_id: int
    base_spec_hash: str


class RecordExtensionChallenge(PositionedRequest):
    """Record the immutable challenge for one extension run."""

    kind: Literal["record_extension_challenge"] = "record_extension_challenge"
    node_id: ClassVar[str] = "scope_extension.challenge"
    canonical_content: JsonObject
    provenance_path: str | None = None


class RecordExtensionPrd(PositionedRequest):
    """Record one immutable extension PRD version."""

    kind: Literal["record_extension_prd"] = "record_extension_prd"
    node_id: ClassVar[str] = "scope_extension.prd"
    challenge_artifact_id: int
    canonical_content: JsonObject
    supersedes_prd_version_id: int | None = None
    provenance_path: str | None = None


class DecideExtensionPrd(PositionedRequest):
    """Review one exact extension PRD version."""

    kind: Literal["decide_extension_prd"] = "decide_extension_prd"
    node_id: ClassVar[str] = "scope_extension.prd_review"
    prd_version_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    notes: str


class RecordAmendmentSpecDraft(PositionedRequest):
    """Record an amendment draft pinned to its run's base spec."""

    kind: Literal["record_amendment_spec_draft"] = "record_amendment_spec_draft"
    node_id: ClassVar[str] = "scope_extension.spec"
    prd_version_id: int
    canonical_content: JsonObject
    base_spec_version_id: int
    base_spec_hash: str
    supersedes_spec_draft_id: int | None = None
    provenance_path: str | None = None


class DecideAmendmentSpecDraft(PositionedRequest):
    """Review one exact amendment draft."""

    kind: Literal["decide_amendment_spec_draft"] = "decide_amendment_spec_draft"
    node_id: ClassVar[str] = "scope_extension.spec_review"
    spec_draft_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    notes: str


class RegisterScopeExtension(PositionedRequest):
    """Register one accepted amendment as the current approved spec."""

    kind: Literal["register_scope_extension"] = "register_scope_extension"
    node_id: ClassVar[str] = "scope_extension.registration"
    spec_draft_id: int


class ReconcileScopeExtension(PositionedRequest):
    """Record downstream relationships and close one extension run."""

    kind: Literal["reconcile_scope_extension"] = "reconcile_scope_extension"
    node_id: ClassVar[str] = "scope_extension.reconciliation"
    discovery_run_id: int
    replacement_authority_id: int
    replacement_authority_fingerprint: str
    artifact_references: tuple[ScopeExtensionArtifactReference, ...]


class AbandonScopeExtension(PositionedRequest):
    """Close one unresolved extension before replacement authority acceptance."""

    kind: Literal["abandon_scope_extension"] = "abandon_scope_extension"
    node_id: ClassVar[str] = "scope_extension.abandon"
    discovery_run_id: int
    reason: str


__all__ = [
    "AbandonScopeExtension",
    "DecideAmendmentSpecDraft",
    "DecideExtensionPrd",
    "ReconcileScopeExtension",
    "RecordAmendmentSpecDraft",
    "RecordExtensionChallenge",
    "RecordExtensionPrd",
    "RegisterScopeExtension",
    "ScopeExtensionArtifactReference",
    "StartScopeExtension",
]
