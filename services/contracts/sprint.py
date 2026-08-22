"""Direct-Specification Sprint and task contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    canonical_spec_item_ids,
    require_nonblank_text,
    validate_accepted_specification_root,
    validate_canonical_spec_item_ids,
    validate_story_item_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class SprintPlannerStory(BaseModel):
    """One exact accepted Story projected for a Sprint planning request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: Annotated[int, Field(gt=0)]
    story_item_id: str
    story_title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    persona: Annotated[str, Field(min_length=1, max_length=100)]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    spec_item_ids: tuple[str, ...]
    story_points: Annotated[int | None, Field(ge=1)] = None
    rank: str | None = None

    @field_validator("story_item_id")
    @classmethod
    def validate_host_story_item_id(cls, value: str) -> str:
        """Reject impossible host Story IDs before building a Sprint input."""
        return validate_story_item_id(value)

    @field_validator("story_title", "statement", "persona")
    @classmethod
    def validate_nonblank_content(cls, value: str) -> str:
        """Reject blank canonical Story projections without rewriting valid bytes."""
        return require_nonblank_text(value, field_name="Sprint Story content")

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank criteria while preserving exact valid bytes and ordering."""
        if any(not criterion.strip() for criterion in value):
            message = "acceptance criterion must not be empty or whitespace-only"
            raise ValueError(message)
        return value

    @field_validator("spec_item_ids")
    @classmethod
    def validate_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require structural canonical evidence before the root loads its payload."""
        return validate_canonical_spec_item_ids(value)

    @model_validator(mode="after")
    def validate_derived_persona(self) -> Self:
        """Require the projected persona to equal the single shared parser output."""
        from services.contracts.story import parse_story_persona  # noqa: PLC0415

        if self.persona != parse_story_persona(self.statement):
            message = "Sprint Story persona must equal the parsed statement persona"
            raise ValueError(message)
        return self


class StructuredTaskSpec(BaseModel):
    """Provider task content with stable Specification evidence IDs only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: Annotated[str, Field(min_length=1)]
    relevant_spec_item_ids: tuple[str, ...]
    task_kind: Literal["implementation", "test", "documentation", "research"]
    artifact_targets: tuple[Annotated[str, Field(min_length=1)], ...]
    workstream_tags: tuple[Annotated[str, Field(min_length=1)], ...]
    checklist_items: tuple[Annotated[str, Field(min_length=1)], ...] = Field(
        min_length=1
    )

    @field_validator("description")
    @classmethod
    def validate_nonblank_description(cls, value: str) -> str:
        """Reject blank task descriptions without altering valid provider content."""
        return require_nonblank_text(value, field_name="Task description")

    @field_validator("relevant_spec_item_ids")
    @classmethod
    def canonicalize_provider_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate evidence and persist provider permutations canonically."""
        if not value:
            message = "relevant Specification item IDs must not be empty"
            raise ValueError(message)
        if len(set(value)) != len(value):
            message = "duplicate Specification item ID in task evidence"
            raise ValueError(message)
        return tuple(sorted(value))


class SprintPlannerSelectedStory(BaseModel):
    """One selected immutable Story and its proposed task decomposition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: Annotated[int, Field(gt=0)]
    story_item_id: str
    tasks: tuple[StructuredTaskSpec, ...] = Field(min_length=1)
    reason_for_selection: Annotated[str, Field(min_length=1)]

    @field_validator("story_item_id")
    @classmethod
    def validate_selected_story_item_id(cls, value: str) -> str:
        """Reject impossible host Story IDs in the immutable selected projection."""
        return validate_story_item_id(value)

    @field_validator("reason_for_selection")
    @classmethod
    def validate_nonblank_reason(cls, value: str) -> str:
        """Reject blank selection rationale without changing valid provider bytes."""
        return require_nonblank_text(value, field_name="Sprint selection reason")


class SprintPlannerOutput(BaseModel):
    """Provider Sprint output without Authority or invariant compatibility fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sprint_goal: Annotated[str, Field(min_length=1)]
    selected_stories: tuple[SprintPlannerSelectedStory, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_story_ids(self) -> Self:
        """Require one complete Task proposal for each selected Story identity."""
        identities = tuple(item.story_id for item in self.selected_stories)
        if len(set(identities)) != len(identities):
            message = "Sprint selected Story IDs must be unique."
            raise ValueError(message)
        return self


class SprintPlannerInput(BaseModel):
    """One exact Specification is supplied once at the Sprint invocation root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted_specification_version_id: Annotated[int, Field(gt=0)]
    accepted_specification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    accepted_specification_json: Annotated[str, Field(min_length=1)]
    available_stories: tuple[SprintPlannerStory, ...]
    capacity_points: Annotated[int, Field(ge=0)]
    capacity_source: Literal["user_override", "project_metrics"]
    capacity_basis: Annotated[str, Field(min_length=1)]
    user_context: str | None = None

    @model_validator(mode="after")
    def validate_specification_root_and_story_evidence(self) -> Self:
        """Prove root bytes/hash and known normative Story evidence once."""
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
        for story in self.available_stories:
            canonical_spec_item_ids(specification, story.spec_item_ids)
        return self


def validate_task_spec_references(
    specification: AcceptedSpecificationReference,
    task: StructuredTaskSpec,
    *,
    parent_story_spec_item_ids: Iterable[str],
) -> tuple[str, ...]:
    """Require a task's evidence to be a qualifying subset of its parent Story."""
    return canonical_spec_item_ids(
        specification,
        task.relevant_spec_item_ids,
        parent_spec_item_ids=parent_story_spec_item_ids,
    )


__all__ = [
    "SprintPlannerInput",
    "SprintPlannerOutput",
    "SprintPlannerSelectedStory",
    "SprintPlannerStory",
    "StructuredTaskSpec",
    "validate_task_spec_references",
]
