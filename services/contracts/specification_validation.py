"""Closed contracts for direct-Specification Story validation."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    canonical_spec_item_ids,
    validate_accepted_specification_root,
    validate_backlog_item_id,
)
from services.contracts.story import CanonicalStoryItem  # noqa: TC001


class StorySpecificationReferences(BaseModel):
    """Canonical derived reference set; never authored by a provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    referenced_spec_item_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_canonical_order(self) -> Self:
        """Keep persisted/hashable derived references deterministic."""
        if not self.referenced_spec_item_ids:
            message = "referenced Specification item IDs must not be empty"
            raise ValueError(message)
        if tuple(sorted(set(self.referenced_spec_item_ids))) != (
            self.referenced_spec_item_ids
        ):
            message = "referenced Specification item IDs must be unique and sorted"
            raise ValueError(message)
        return self


class StorySpecificationFinding(BaseModel):
    """One bounded semantic finding tied to an exact Specification item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "SPEC_ITEM_CONTRADICTION",
        "SPEC_ITEM_OMISSION",
        "SPEC_ITEM_UNTESTABLE",
    ]
    spec_item_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
    ]
    suggested_change: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
        ]
        | None
    ) = None


class StorySpecificationReviewOutput(BaseModel):
    """Strict, complete one-shot semantic review response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agileforge.story-specification-review.v1"]
    compliant: bool
    complete: bool
    findings: tuple[StorySpecificationFinding, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_complete_consistent_unique_result(self) -> Self:
        """Reject incomplete, contradictory, or duplicate provider output."""
        if not self.complete:
            message = "semantic review output must be complete"
            raise ValueError(message)
        if self.compliant != (not self.findings):
            message = "semantic compliant flag must equal not findings"
            raise ValueError(message)
        pairs = tuple((item.spec_item_id, item.code) for item in self.findings)
        if len(pairs) != len(set(pairs)):
            message = "semantic review findings contain duplicate item/code pairs"
            raise ValueError(message)
        return self


class StorySpecificationReviewInput(BaseModel):
    """One accepted Specification root, parent PBI boundary, and Story item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agileforge.story-specification-review-input.v1"]
    accepted_specification_version_id: Annotated[int, Field(gt=0)]
    accepted_specification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    accepted_specification_json: Annotated[str, Field(min_length=1)]
    parent_backlog_item_id: str
    parent_backlog_spec_item_ids: tuple[str, ...]
    story: CanonicalStoryItem

    @model_validator(mode="after")
    def validate_exact_source_bounds(self) -> Self:
        """Prove the exact root and both nested evidence boundaries."""
        validate_backlog_item_id(self.parent_backlog_item_id)
        specification = AcceptedSpecificationReference.model_validate(
            {
                "spec_version_id": self.accepted_specification_version_id,
                "spec_hash": self.accepted_specification_hash,
                "canonical_specification_json": self.accepted_specification_json,
                "payload": validate_accepted_specification_root(
                    spec_hash=self.accepted_specification_hash,
                    canonical_specification_json=self.accepted_specification_json,
                ),
            }
        )
        parent_ids = canonical_spec_item_ids(
            specification,
            self.parent_backlog_spec_item_ids,
        )
        canonical_spec_item_ids(
            specification,
            self.story.spec_item_ids,
            parent_spec_item_ids=parent_ids,
        )
        return self


__all__ = [
    "StorySpecificationFinding",
    "StorySpecificationReferences",
    "StorySpecificationReviewInput",
    "StorySpecificationReviewOutput",
]
