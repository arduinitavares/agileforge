"""Strict host-prepared contracts for the Vision interview agent."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def _strip_required(value: str, label: str) -> str:
    """Normalize one required human or model-facing string."""
    normalized = value.strip()
    if not normalized:
        message = f"{label} must not be blank."
        raise ValueError(message)
    return normalized


class VisionComponents(BaseModel):
    """The complete set of human-defined product Vision components."""

    model_config = ConfigDict(extra="forbid")

    project_name: str | None
    target_user: str | None
    problem: str | None
    product_category: str | None
    key_benefit: str | None
    competitors: str | None
    differentiator: str | None

    @field_validator("*", mode="before")
    @classmethod
    def normalize_component(cls, value: object, info: object) -> object:
        """Strip provided component strings and reject ambiguous blanks."""
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return _strip_required(value, str(getattr(info, "field_name", "component")))

    def is_fully_defined(self) -> bool:
        """Return whether every Vision component has one substantive answer."""
        return all(
            isinstance(value, str) and bool(value)
            for value in self.model_dump().values()
        )


class VisionInterviewInput(BaseModel):
    """All and only the host-prepared information passed to the Vision model."""

    model_config = ConfigDict(extra="forbid")

    project_name: str
    project_description: str | None
    mode: Literal["initial", "revision"]
    user_response: str
    prior_components: VisionComponents | None
    accepted_vision_statement: str | None

    @field_validator("project_name", "user_response")
    @classmethod
    def normalize_required(cls, value: str, info: object) -> str:
        """Reject blank required input before invoking any provider."""
        return _strip_required(value, str(getattr(info, "field_name", "value")))

    @field_validator("project_description", "accepted_vision_statement")
    @classmethod
    def normalize_optional(cls, value: str | None, info: object) -> str | None:
        """Normalize optional strings while preserving absent context."""
        if value is None:
            return None
        return _strip_required(value, str(getattr(info, "field_name", "value")))


class VisionInterviewOutput(BaseModel):
    """Strict model result persisted as one immutable Vision interview turn."""

    model_config = ConfigDict(extra="forbid")

    updated_components: VisionComponents
    project_vision_statement: str
    is_complete: bool
    clarifying_questions: list[str]

    @field_validator("project_vision_statement")
    @classmethod
    def normalize_statement(cls, value: str) -> str:
        """Require a substantive statement on every interview turn."""
        return _strip_required(value, "project_vision_statement")

    @field_validator("clarifying_questions")
    @classmethod
    def normalize_questions(cls, value: list[str]) -> list[str]:
        """Strip every question and reject blank questions individually."""
        return [_strip_required(item, "clarifying_questions") for item in value]

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        """Keep completion and the next human question internally coherent."""
        if self.is_complete != self.updated_components.is_fully_defined():
            message = "is_complete must match updated_components."
            raise ValueError(message)
        if not self.is_complete and not self.clarifying_questions:
            message = "Incomplete Vision output requires a clarifying question."
            raise ValueError(message)
        return self


class InputSchema(BaseModel):
    """Legacy root-graph contract retained until the Task 5 cutover."""

    model_config = ConfigDict(extra="forbid")

    user_raw_text: str
    specification_content: str
    prior_vision_state: str
    compiled_authority: str


class OutputSchema(BaseModel):
    """Legacy root-graph output retained until the Task 5 cutover."""

    model_config = ConfigDict(extra="forbid")

    updated_components: VisionComponents
    product_vision_statement: str
    is_complete: bool
    clarifying_questions: list[str]


__all__ = [
    "InputSchema",
    "OutputSchema",
    "VisionComponents",
    "VisionInterviewInput",
    "VisionInterviewOutput",
]
