"""Greenfield onboarding transition request contracts."""

from typing import ClassVar, Literal

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest


class RecordChallengeArtifact(PositionedRequest):
    """Record the immutable challenge artifact for initial discovery."""

    kind: Literal["record_challenge_artifact"] = "record_challenge_artifact"
    node_id: ClassVar[str] = "onboarding.greenfield.challenge"
    canonical_content: JsonObject
    provenance_path: str | None = None


class RecordPrdVersion(PositionedRequest):
    """Record one immutable PRD version derived from the challenge."""

    kind: Literal["record_prd_version"] = "record_prd_version"
    node_id: ClassVar[str] = "onboarding.greenfield.prd"
    challenge_artifact_id: int
    canonical_content: JsonObject
    supersedes_prd_version_id: int | None = None
    provenance_path: str | None = None


class DecidePrd(PositionedRequest):
    """Append a terminal human decision for one exact PRD version."""

    kind: Literal["decide_prd"] = "decide_prd"
    node_id: ClassVar[str] = "onboarding.greenfield.prd_review"
    prd_version_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    notes: str


class RecordInitialSpecDraft(PositionedRequest):
    """Record one immutable initial specification draft."""

    kind: Literal["record_initial_spec_draft"] = "record_initial_spec_draft"
    node_id: ClassVar[str] = "onboarding.greenfield.initial_spec"
    prd_version_id: int
    canonical_content: JsonObject
    supersedes_spec_draft_id: int | None = None
    provenance_path: str | None = None


class DecideInitialSpecDraft(PositionedRequest):
    """Append a terminal human decision for one exact initial draft."""

    kind: Literal["decide_initial_spec_draft"] = "decide_initial_spec_draft"
    node_id: ClassVar[str] = "onboarding.greenfield.initial_spec_review"
    spec_draft_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    notes: str


class RegisterInitialScope(PositionedRequest):
    """Bind one accepted initial draft to the first specification version."""

    kind: Literal["register_initial_scope"] = "register_initial_scope"
    node_id: ClassVar[str] = "onboarding.initial_scope_registration"
    spec_draft_id: int


__all__ = [
    "DecideInitialSpecDraft",
    "DecidePrd",
    "RecordChallengeArtifact",
    "RecordInitialSpecDraft",
    "RecordPrdVersion",
    "RegisterInitialScope",
]
