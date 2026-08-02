"""Greenfield onboarding transition request contracts."""

from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from workflow.contracts import FrozenModel, JsonObject
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


class RepositoryInventoryEntry(FrozenModel):
    """Canonical inventory entry accepted by the brownfield transition."""

    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str | None
    content_status: Literal["hashable", "secret", "oversized", "symlink"]

    @model_validator(mode="after")
    def validate_hash_status(self) -> Self:
        """Require hashes only for content explicitly safe to hash."""
        if (self.content_status == "hashable") != (self.sha256 is not None):
            msg = "Only hashable inventory entries may carry a SHA-256 digest."
            raise ValueError(msg)
        return self


class RecordRepositoryBaseline(PositionedRequest):
    """Record canonical repository identity evidence for a brownfield Project."""

    kind: Literal["record_repository_baseline"] = "record_repository_baseline"
    node_id: ClassVar[str] = "onboarding.brownfield.baseline"
    repository_path: str = Field(min_length=1)
    git_commit: str | None
    dirty: bool
    baseline_fingerprint: str = Field(min_length=1)


class RecordRepositoryInventory(PositionedRequest):
    """Record a complete inventory and separate bounded model selection."""

    kind: Literal["record_repository_inventory"] = "record_repository_inventory"
    node_id: ClassVar[str] = "onboarding.brownfield.inventory"
    repository_baseline_id: int
    files: tuple[RepositoryInventoryEntry, ...]
    selected_for_model: tuple[str, ...]
    total_bytes: int = Field(ge=0)
    inventory_fingerprint: str = Field(min_length=1)


class RecordBrownfieldSpecDraft(PositionedRequest):
    """Record one initial spec draft derived from reviewed repository evidence."""

    kind: Literal["record_brownfield_spec_draft"] = "record_brownfield_spec_draft"
    node_id: ClassVar[str] = "onboarding.brownfield.curation"
    repository_inventory_id: int
    canonical_content: JsonObject
    supersedes_spec_draft_id: int | None = None
    provenance_path: str | None = None


class DecideBrownfieldInitialSpec(PositionedRequest):
    """Append a terminal human decision for one exact brownfield draft."""

    kind: Literal["decide_brownfield_initial_spec"] = "decide_brownfield_initial_spec"
    node_id: ClassVar[str] = "onboarding.brownfield.initial_spec_review"
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
    "DecideBrownfieldInitialSpec",
    "DecideInitialSpecDraft",
    "DecidePrd",
    "RecordBrownfieldSpecDraft",
    "RecordChallengeArtifact",
    "RecordInitialSpecDraft",
    "RecordPrdVersion",
    "RecordRepositoryBaseline",
    "RecordRepositoryInventory",
    "RegisterInitialScope",
    "RepositoryInventoryEntry",
]
