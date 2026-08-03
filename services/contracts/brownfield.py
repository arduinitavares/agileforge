"""Strict pre-authority Brownfield curation contracts."""

from __future__ import annotations

import hashlib
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from utils.agileforge_spec_profile import (
    AgileForgeSpecStatus,
    TechnicalSpecArtifact,
)

MAX_SELECTED_EVIDENCE_FILES: int = 500
MAX_SELECTED_EVIDENCE_BYTES: int = 2_000_000
MAX_REPOSITORY_PATH_CHARS: int = 4_096
type Sha256Fingerprint = Annotated[
    str,
    Field(pattern=r"^sha256:[0-9a-f]{64}$"),
]
type RepositoryEvidencePath = Annotated[
    str,
    Field(min_length=1, max_length=MAX_REPOSITORY_PATH_CHARS),
]


class _StrictModel(BaseModel):
    """Forbid model-controlled extension fields at every contract level."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BrownfieldRepositoryInventory(_StrictModel):
    """Trusted identity and bounded selection for one durable inventory."""

    repository_inventory_id: int = Field(gt=0)
    repository_inventory_fingerprint: Sha256Fingerprint
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    selected_for_model: tuple[RepositoryEvidencePath, ...] = Field(
        min_length=1,
        max_length=MAX_SELECTED_EVIDENCE_FILES,
    )

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        """Require one unique selection that can belong to the inventory."""
        if len(self.selected_for_model) != len(set(self.selected_for_model)):
            msg = "Repository inventory selection paths must be unique."
            raise ValueError(msg)
        if len(self.selected_for_model) > self.file_count:
            msg = "Repository inventory selection exceeds its file count."
            raise ValueError(msg)
        return self


class BrownfieldSelectedEvidence(_StrictModel):
    """One bounded text artifact selected outside model-controlled output."""

    path: RepositoryEvidencePath
    content: str = Field(max_length=MAX_SELECTED_EVIDENCE_BYTES)
    content_sha256: Sha256Fingerprint

    @model_validator(mode="after")
    def validate_content_fingerprint(self) -> Self:
        """Bind the supplied text to its exact host-computed digest."""
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 != f"sha256:{digest}":
            msg = "Selected repository evidence fingerprint does not match content."
            raise ValueError(msg)
        return self


class BrownfieldCurationInput(_StrictModel):
    """Exact pre-authority inventory and evidence supplied to the curator."""

    inventory: BrownfieldRepositoryInventory
    selected_evidence: tuple[BrownfieldSelectedEvidence, ...] = Field(
        min_length=1,
        max_length=MAX_SELECTED_EVIDENCE_FILES,
    )

    @model_validator(mode="after")
    def validate_evidence_selection(self) -> Self:
        """Require evidence to exactly match the durable ordered selection."""
        selected_paths = tuple(item.path for item in self.selected_evidence)
        if selected_paths != self.inventory.selected_for_model:
            msg = "Brownfield selected evidence must match the inventory selection."
            raise ValueError(msg)
        evidence_bytes = sum(
            len(item.content.encode("utf-8")) for item in self.selected_evidence
        )
        if evidence_bytes > MAX_SELECTED_EVIDENCE_BYTES:
            msg = "Brownfield selected evidence exceeds the aggregate byte limit."
            raise ValueError(msg)
        return self


class BrownfieldCurationOutput(_StrictModel):
    """Model-owned canonical initial specification content only."""

    canonical_spec: TechnicalSpecArtifact

    @model_validator(mode="after")
    def validate_initial_draft(self) -> Self:
        """Keep pre-authority generated content in draft state."""
        if self.canonical_spec.status is not AgileForgeSpecStatus.DRAFT:
            msg = "Brownfield curation output must be a draft specification."
            raise ValueError(msg)
        return self


__all__ = [
    "MAX_SELECTED_EVIDENCE_BYTES",
    "MAX_SELECTED_EVIDENCE_FILES",
    "BrownfieldCurationInput",
    "BrownfieldCurationOutput",
    "BrownfieldRepositoryInventory",
    "BrownfieldSelectedEvidence",
]
