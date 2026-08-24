"""Closed Roadmap contracts that reference exact Backlog items."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.contracts.backlog import BacklogItem  # noqa: TC001
from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    canonical_spec_item_ids,
    require_nonblank_text,
    validate_accepted_specification_root,
    validate_backlog_item_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class RoadmapBuilderInput(BaseModel):
    """Roadmap invocation root carrying one exact Backlog and Specification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted_specification_version_id: Annotated[int, Field(gt=0)]
    accepted_specification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    accepted_specification_json: Annotated[str, Field(min_length=1)]
    backlog_items: tuple[BacklogItem, ...]
    product_vision: Annotated[str, Field(min_length=1)]
    time_increment: Annotated[str, Field(min_length=1)] = "Milestone-based"
    prior_roadmap_state: Annotated[str, Field(min_length=1)] = "NO_HISTORY"
    user_input: str = ""

    @field_validator("product_vision", "time_increment", "prior_roadmap_state")
    @classmethod
    def validate_nonblank_context(cls, value: str) -> str:
        """Reject blank Roadmap context without rewriting valid bytes."""
        return require_nonblank_text(value, field_name="Roadmap input context")

    @model_validator(mode="after")
    def validate_specification_root_and_backlog_evidence(self) -> Self:
        """Prove root bytes/hash and every exact Backlog evidence reference."""
        payload = validate_accepted_specification_root(
            spec_hash=self.accepted_specification_hash,
            canonical_specification_json=self.accepted_specification_json,
        )
        specification = AcceptedSpecificationReference(
            spec_version_id=self.accepted_specification_version_id,
            spec_hash=self.accepted_specification_hash,
            canonical_specification_json=self.accepted_specification_json,
            payload=payload,
        )
        for item in self.backlog_items:
            canonical_spec_item_ids(specification, item.spec_item_ids)
        return self


class RoadmapRelease(BaseModel):
    """One ordered release containing exact parent Backlog item IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    release_name: Annotated[str, Field(min_length=1)]
    theme: Annotated[str, Field(min_length=1)]
    focus_area: Literal["Technical Foundation", "User Value", "Scale", "Other"]
    backlog_item_ids: tuple[str, ...]
    reasoning: Annotated[str, Field(min_length=1)]

    @field_validator("release_name", "theme", "reasoning")
    @classmethod
    def validate_nonblank_content(cls, value: str) -> str:
        """Preserve valid release prose while rejecting blank canonical text."""
        return require_nonblank_text(value, field_name="Roadmap release content")

    @field_validator("backlog_item_ids")
    @classmethod
    def validate_backlog_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject impossible host Backlog IDs before Roadmap coverage validation."""
        return tuple(validate_backlog_item_id(item_id) for item_id in value)


class RoadmapBuilderOutput(BaseModel):
    """Provider output whose references are checked against one Backlog parent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    roadmap_releases: tuple[RoadmapRelease, ...]
    roadmap_summary: Annotated[str, Field(min_length=1)]
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


def validate_roadmap_backlog_coverage(
    roadmap: RoadmapBuilderOutput,
    parent_backlog_item_ids: Iterable[str],
) -> None:
    """Require every exact parent Backlog item once across all releases."""
    parent_ids = tuple(parent_backlog_item_ids)
    parent_id_set = set(parent_ids)
    if len(parent_id_set) != len(parent_ids):
        message = "parent Backlog item IDs must be unique"
        raise ValueError(message)

    seen: set[str] = set()
    for release in roadmap.roadmap_releases:
        for backlog_item_id in release.backlog_item_ids:
            if backlog_item_id in seen:
                message = f"duplicate backlog item ID: {backlog_item_id}"
                raise ValueError(message)
            seen.add(backlog_item_id)
            if backlog_item_id not in parent_id_set:
                message = f"unknown backlog item ID: {backlog_item_id}"
                raise ValueError(message)
    if seen != parent_id_set:
        missing = ", ".join(sorted(parent_id_set - seen))
        message = (
            f"Roadmap must reference every parent Backlog item exactly once: {missing}"
        )
        raise ValueError(message)


__all__ = [
    "RoadmapBuilderInput",
    "RoadmapBuilderOutput",
    "RoadmapRelease",
    "validate_roadmap_backlog_coverage",
]
