"""Provider and host contracts for immutable, evidence-bound Story items."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    canonical_spec_item_ids,
    require_nonblank_text,
    validate_accepted_specification_root,
    validate_backlog_item_id,
    validate_canonical_spec_item_ids,
    validate_story_item_id,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Iterable


_PERSONA_PATTERN = re.compile(
    r"^as (?:a|an|the) (?P<persona>.+?)(?:,)? i want ", re.IGNORECASE
)
_MAX_PERSONA_LENGTH = 100
_MAX_STORY_ITEMS = 8


def parse_story_persona(statement: str) -> str:
    """Derive the sole canonical persona from the approved Story prefix."""
    match = _PERSONA_PATTERN.match(statement.strip().replace("*", ""))
    if match is None:
        message = "statement must start with 'As a|an|the <persona>,? I want '"
        raise ValueError(message)
    persona = match.group("persona").strip()
    if not 1 <= len(persona) <= _MAX_PERSONA_LENGTH:
        message = "Story persona must contain one through 100 characters"
        raise ValueError(message)
    return persona


class StoryDependencyCandidate(BaseModel):
    """One provider-proposed dependency retained in immutable Story content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prerequisite_ref: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    confidence: Literal["explicit", "inferred"]


class UserStoryAgentItem(BaseModel):
    """ID-free provider Story output before host validation and canonicalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    spec_item_ids: tuple[str, ...]
    invest_score: Literal["High", "Medium", "Low"]
    estimated_effort: Literal["XS", "S", "M", "L", "XL"]
    produced_artifacts: tuple[str, ...]
    research_caveats: tuple[str, ...]
    decomposition_warning: str | None
    dependency_candidates: tuple[StoryDependencyCandidate, ...]

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_exact_nonblank_criteria(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Reject blank criteria without altering other bytes or ordering."""
        if any(not criterion.strip() for criterion in value):
            message = "acceptance criterion must not be empty or whitespace-only"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def validate_statement_persona(self) -> Self:
        """Use the sole host persona parser at the provider-output boundary."""
        parse_story_persona(self.statement)
        return self


class CanonicalStoryItem(BaseModel):
    """Closed host Story content; its fingerprint is stored only in an envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_item_id: str
    story_title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    persona: Annotated[str, Field(min_length=1, max_length=_MAX_PERSONA_LENGTH)]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    spec_item_ids: tuple[str, ...]
    invest_score: Literal["High", "Medium", "Low"]
    estimated_effort: Literal["XS", "S", "M", "L", "XL"]
    produced_artifacts: tuple[str, ...]
    research_caveats: tuple[str, ...]
    decomposition_warning: str | None
    dependency_candidates: tuple[StoryDependencyCandidate, ...]

    @field_validator("story_item_id")
    @classmethod
    def validate_host_item_id(cls, value: str) -> str:
        """Reject impossible host Story IDs during canonical-item deserialization."""
        return validate_story_item_id(value)

    @field_validator("story_title", "statement", "persona")
    @classmethod
    def validate_nonblank_content(cls, value: str) -> str:
        """Reject blank host Story content without rewriting valid bytes."""
        return require_nonblank_text(value, field_name="Story item content")

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_host_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank criteria while preserving exact valid criteria bytes/order."""
        if any(not criterion.strip() for criterion in value):
            message = "acceptance criterion must not be empty or whitespace-only"
            raise ValueError(message)
        return value

    @field_validator("spec_item_ids")
    @classmethod
    def validate_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject noncanonical evidence in persisted/hashable host Story content."""
        return validate_canonical_spec_item_ids(value)

    @model_validator(mode="after")
    def validate_derived_persona(self) -> Self:
        """Require the host persona to equal the sole parser result exactly."""
        if self.persona != parse_story_persona(self.statement):
            message = "Story persona must equal the parsed statement persona"
            raise ValueError(message)
        return self


class StoryItemEnvelope(BaseModel):
    """Canonical Story item beside its non-recursive immutable fingerprint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: CanonicalStoryItem
    item_fingerprint: str

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        """Ensure the fingerprint covers the complete closed item and nothing else."""
        if self.item_fingerprint != canonical_hash(self.item.model_dump(mode="json")):
            message = "Story item fingerprint does not match canonical item"
            raise ValueError(message)
        return self


class CanonicalStoryOutput(BaseModel):
    """Closed host Story envelope persisted for later review and planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_items: tuple[StoryItemEnvelope, ...] = Field(
        min_length=1,
        max_length=_MAX_STORY_ITEMS,
    )
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


class UserStoryWriterOutput(BaseModel):
    """Provider output with no provider-owned Story item identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_stories: tuple[UserStoryAgentItem, ...] = Field(
        min_length=1, max_length=_MAX_STORY_ITEMS
    )
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


class UserStoryWriterInput(BaseModel):
    """Story invocation root carrying one exact accepted Specification contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted_specification_version_id: Annotated[int, Field(gt=0)]
    accepted_specification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    accepted_specification_json: Annotated[str, Field(min_length=1)]
    parent_backlog_item_id: str
    parent_backlog_spec_item_ids: tuple[str, ...]
    roadmap_context: str = ""
    user_input: str | None = None

    @field_validator("parent_backlog_item_id")
    @classmethod
    def validate_parent_backlog_item_id(cls, value: str) -> str:
        """Reject impossible parent IDs before the Story provider is invoked."""
        return validate_backlog_item_id(value)

    @model_validator(mode="after")
    def validate_specification_root_and_parent_evidence(self) -> Self:
        """Prove root bytes/hash and canonical qualifying parent evidence once."""
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
        parent_ids = validate_canonical_spec_item_ids(self.parent_backlog_spec_item_ids)
        canonical_spec_item_ids(specification, parent_ids)
        return self


def canonicalize_story_items(
    specification: AcceptedSpecificationReference,
    *,
    parent_backlog_spec_item_ids: Iterable[str],
    agent_items: Iterable[UserStoryAgentItem],
) -> tuple[StoryItemEnvelope, ...]:
    """Validate provider items, preserve their order, and mint host Story IDs."""
    items = tuple(agent_items)
    if not 1 <= len(items) <= _MAX_STORY_ITEMS:
        message = "Story output must contain one through eight items"
        raise ValueError(message)
    parent_ids = tuple(parent_backlog_spec_item_ids)
    return tuple(
        StoryItemEnvelope(
            item=(
                canonical_item := CanonicalStoryItem(
                    story_item_id=f"US-{ordinal:04d}",
                    story_title=item.story_title,
                    statement=item.statement,
                    persona=parse_story_persona(item.statement),
                    acceptance_criteria=item.acceptance_criteria,
                    spec_item_ids=canonical_spec_item_ids(
                        specification,
                        item.spec_item_ids,
                        parent_spec_item_ids=parent_ids,
                    ),
                    invest_score=item.invest_score,
                    estimated_effort=item.estimated_effort,
                    produced_artifacts=item.produced_artifacts,
                    research_caveats=item.research_caveats,
                    decomposition_warning=item.decomposition_warning,
                    dependency_candidates=item.dependency_candidates,
                )
            ),
            item_fingerprint=canonical_hash(canonical_item.model_dump(mode="json")),
        )
        for ordinal, item in enumerate(items, start=1)
    )


__all__ = [
    "CanonicalStoryItem",
    "CanonicalStoryOutput",
    "StoryDependencyCandidate",
    "StoryItemEnvelope",
    "UserStoryAgentItem",
    "UserStoryWriterInput",
    "UserStoryWriterOutput",
    "canonicalize_story_items",
    "parse_story_persona",
]
