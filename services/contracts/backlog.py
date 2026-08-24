"""Provider and host contracts for canonical Backlog items."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    canonical_spec_item_ids,
    require_nonblank_text,
    validate_accepted_specification_root,
    validate_backlog_item_id,
    validate_canonical_spec_item_ids,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_MAX_BACKLOG_ITEMS = 999999


def normalize_backlog_requirement(value: str) -> str:
    """Normalize only for duplicate detection; punctuation intentionally remains."""
    return " ".join(value.casefold().split())


class BacklogBuilderInput(BaseModel):
    """Backlog invocation root carrying one accepted Specification contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted_specification_version_id: Annotated[int, Field(gt=0)]
    accepted_specification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    accepted_specification_json: Annotated[str, Field(min_length=1)]
    product_vision_statement: Annotated[str, Field(min_length=1)]
    product_goal_statement: Annotated[str, Field(min_length=1)]
    prior_backlog_state: Annotated[str, Field(min_length=1)] = "NO_HISTORY"
    user_input: str | None = None

    @field_validator(
        "product_vision_statement",
        "product_goal_statement",
        "prior_backlog_state",
    )
    @classmethod
    def validate_nonblank_context(cls, value: str) -> str:
        """Reject blank delivery context without rewriting valid bytes."""
        return require_nonblank_text(value, field_name="Backlog input context")

    @model_validator(mode="after")
    def validate_specification_root(self) -> Self:
        """Prove the sole accepted Specification root bytes and hash."""
        validate_accepted_specification_root(
            spec_hash=self.accepted_specification_hash,
            canonical_specification_json=self.accepted_specification_json,
        )
        return self


class BacklogAgentItem(BaseModel):
    """ID-free provider-owned output awaiting host canonicalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: Annotated[int, Field(ge=1)]
    requirement: Annotated[str, Field(min_length=3)]
    spec_item_ids: tuple[str, ...]
    value_driver: Literal["Revenue", "Customer Satisfaction", "Strategic"]
    justification: Annotated[str, Field(min_length=3)]
    estimated_effort: Literal["S", "M", "L", "XL"]
    technical_note: str | None = None


class BacklogItem(BaseModel):
    """Closed host-owned Backlog item with an artifact-scoped stable ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backlog_item_id: str
    priority: Annotated[int, Field(ge=1, le=999999)]
    requirement: Annotated[str, Field(min_length=3)]
    spec_item_ids: tuple[str, ...]
    value_driver: Literal["Revenue", "Customer Satisfaction", "Strategic"]
    justification: Annotated[str, Field(min_length=3)]
    estimated_effort: Literal["S", "M", "L", "XL"]
    technical_note: str | None = None

    @field_validator("backlog_item_id")
    @classmethod
    def validate_host_item_id(cls, value: str) -> str:
        """Reject impossible host-minted Backlog IDs during deserialization."""
        return validate_backlog_item_id(value)

    @field_validator("requirement", "justification")
    @classmethod
    def validate_nonblank_content(cls, value: str) -> str:
        """Preserve valid bytes while rejecting blank canonical Backlog text."""
        return require_nonblank_text(value, field_name="Backlog item content")

    @field_validator("spec_item_ids")
    @classmethod
    def validate_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject structurally noncanonical evidence before persistence or hashing."""
        return validate_canonical_spec_item_ids(value)


class BacklogAgentOutput(BaseModel):
    """Provider output whose durable IDs and ordering are host-controlled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backlog_items: tuple[BacklogAgentItem, ...] = Field(max_length=_MAX_BACKLOG_ITEMS)
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


class BacklogOutput(BaseModel):
    """Canonical host envelope persisted and consumed by later phases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backlog_items: tuple[BacklogItem, ...]
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


def canonicalize_backlog_items(
    specification: AcceptedSpecificationReference,
    agent_items: Iterable[BacklogAgentItem],
) -> tuple[BacklogItem, ...]:
    """Reject ambiguous provider output, then sort and mint stable PBI IDs."""
    items = tuple(agent_items)
    if len(items) > _MAX_BACKLOG_ITEMS:
        message = "Backlog contains more than 999999 items"
        raise ValueError(message)

    priorities: set[int] = set()
    normalized_requirements: set[str] = set()
    for item in items:
        if item.priority in priorities:
            message = f"duplicate backlog priority: {item.priority}"
            raise ValueError(message)
        priorities.add(item.priority)
        normalized_requirement = normalize_backlog_requirement(item.requirement)
        if normalized_requirement in normalized_requirements:
            message = "duplicate normalized requirement text"
            raise ValueError(message)
        normalized_requirements.add(normalized_requirement)

    return tuple(
        BacklogItem(
            backlog_item_id=f"PBI-{ordinal:06d}",
            priority=item.priority,
            requirement=item.requirement,
            spec_item_ids=canonical_spec_item_ids(specification, item.spec_item_ids),
            value_driver=item.value_driver,
            justification=item.justification,
            estimated_effort=item.estimated_effort,
            technical_note=item.technical_note,
        )
        for ordinal, item in enumerate(
            sorted(items, key=lambda item: item.priority), start=1
        )
    )


__all__ = [
    "BacklogAgentItem",
    "BacklogAgentOutput",
    "BacklogBuilderInput",
    "BacklogItem",
    "BacklogOutput",
    "canonicalize_backlog_items",
    "normalize_backlog_requirement",
]
