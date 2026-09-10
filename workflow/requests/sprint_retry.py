"""Guarded requests for retrying and explicitly starting a Sprint retry."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, Self

from pydantic import Field, model_validator

from workflow.requests.base import PositionedRequest


class RetrySprint(PositionedRequest):
    """Confirm creation of one fresh attempt for an exact completed Sprint."""

    kind: Literal["retry_sprint"] = "retry_sprint"
    node_id: ClassVar[str] = "execution.sprint.retry"
    sprint_id: int
    confirm: Annotated[bool, Field(strict=True)]
    rationale: str
    expected_state_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_confirmation_and_words(self) -> Self:
        """Reject every confirmation representation except literal true."""
        if self.confirm is not True:
            message = "Retry Sprint requires literal confirmation true."
            raise ValueError(message)
        if not self.actor.strip() or not self.rationale.strip():
            message = "Retry Sprint requires nonblank actor and rationale."
            raise ValueError(message)
        return self


class StartSprintRetry(PositionedRequest):
    """Explicitly start one already-planned retry attempt."""

    kind: Literal["start_sprint_retry"] = "start_sprint_retry"
    node_id: ClassVar[str] = "execution.sprint.retry.start"
    sprint_id: int
    retry_attempt_id: int

    @model_validator(mode="after")
    def validate_actor(self) -> Self:
        """Require an accountable actor for the immutable retry start."""
        if not self.actor.strip():
            message = "Retry start requires a nonblank actor."
            raise ValueError(message)
        return self
