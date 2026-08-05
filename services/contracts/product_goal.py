"""Strict model contract for Product Goal interviews."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def _required(value: str) -> str:
    value = value.strip()
    if not value:
        message = "Text values must be non-blank."
        raise ValueError(message)
    return value


class ProductGoalComponents(BaseModel):
    """Semantic components needed to accept one Product Goal."""

    model_config = ConfigDict(extra="forbid")

    valuable_future_state: str | None = None
    beneficiary: str | None = None
    value: str | None = None
    success_signals: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()

    @field_validator("valuable_future_state", "beneficiary", "value")
    @classmethod
    def validate_scalar(cls, value: str | None) -> str | None:
        """Normalize supplied scalar values without requiring partial answers."""
        return None if value is None else _required(value)

    @field_validator("success_signals", "boundaries")
    @classmethod
    def validate_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank list entries while preserving the interview order."""
        return tuple(_required(item) for item in value)

    def is_fully_defined(self) -> bool:
        """Return whether all five semantic Goal dimensions are present."""
        return (
            self.valuable_future_state is not None
            and self.beneficiary is not None
            and self.value is not None
            and bool(self.success_signals)
            and bool(self.boundaries)
        )


class ProductGoalInterviewInput(BaseModel):
    """Host-built context supplied to the Product Goal interview agent."""

    model_config = ConfigDict(extra="forbid")

    project_name: str
    accepted_vision_statement: str
    user_response: str
    prior_components: ProductGoalComponents | None

    @field_validator("project_name", "accepted_vision_statement", "user_response")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """Require meaningful host context and human input."""
        return _required(value)

    @classmethod
    def normalize_user_response(cls, value: str) -> str:
        """Normalize public user input before replay lookup."""
        return _required(value)


class ProductGoalInterviewOutput(BaseModel):
    """Strict, provider-neutral Product Goal turn output."""

    model_config = ConfigDict(extra="forbid")

    updated_components: ProductGoalComponents
    product_goal_statement: str
    is_complete: bool
    clarifying_questions: list[str]

    @field_validator("product_goal_statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        """Normalize the candidate Goal statement."""
        return _required(value)

    @field_validator("clarifying_questions")
    @classmethod
    def validate_questions(cls, value: list[str]) -> list[str]:
        """Keep questions focused and non-blank."""
        return [_required(item) for item in value]

    @model_validator(mode="after")
    def validate_completion(self) -> ProductGoalInterviewOutput:
        """Bind completion, component completeness, and questions together."""
        complete = self.updated_components.is_fully_defined()
        if complete != self.is_complete:
            message = "is_complete must match fully defined Goal components."
            raise ValueError(message)
        if complete and self.clarifying_questions:
            message = "Complete Goal output cannot contain clarifying questions."
            raise ValueError(message)
        if not complete and not self.clarifying_questions:
            message = "Incomplete Goal output requires clarifying questions."
            raise ValueError(message)
        return self
