"""Typed requests for Sprint execution and post-Sprint triage facts."""

from __future__ import annotations

from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest


class CompleteTask(PositionedRequest):
    """Complete the exact dependency-safe Task selected by the graph."""

    kind: Literal["complete_task"] = "complete_task"
    node_id: ClassVar[str] = "execution.task.complete"
    instance_key: str
    task_id: int
    outcome_summary: str = Field(min_length=1)
    artifact_refs: tuple[str, ...]
    acceptance_result: Literal["partially_met", "fully_met"]
    checklist_result: JsonObject

    @model_validator(mode="after")
    def validate_task_instance(self) -> Self:
        """Require the instance guard to bind the exact Task."""
        expected = f"task:{self.task_id}"
        if self.instance_key != expected:
            message = f"instance_key must be exactly {expected!r}."
            raise ValueError(message)
        return self


class CloseStory(PositionedRequest):
    """Close one Story against its exact terminal Task fingerprint."""

    kind: Literal["close_story"] = "close_story"
    node_id: ClassVar[str] = "execution.story.close"
    instance_key: str
    story_id: int
    resolution: str = Field(min_length=1)
    delivered: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    known_gaps: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_story_instance(self) -> Self:
        """Require the instance guard to bind the exact Story."""
        expected = f"story:{self.story_id}"
        if self.instance_key != expected:
            message = f"instance_key must be exactly {expected!r}."
            raise ValueError(message)
        return self


class ReviewSprint(PositionedRequest):
    """Persist review of the exact terminal Story set for one Sprint."""

    kind: Literal["review_sprint"] = "review_sprint"
    node_id: ClassVar[str] = "execution.sprint.review"
    sprint_id: int
    review_fingerprint: str = Field(min_length=1)


class CloseSprint(PositionedRequest):
    """Close one Sprint against its exact persisted review fingerprint."""

    kind: Literal["close_sprint"] = "close_sprint"
    node_id: ClassVar[str] = "execution.sprint.close"
    sprint_id: int
    review_fingerprint: str = Field(min_length=1)


class RecordPostSprintTriage(PositionedRequest):
    """Record or append a correction to completed-Sprint triage."""

    kind: Literal["record_post_sprint_triage"] = "record_post_sprint_triage"
    node_id: ClassVar[str] = "execution.post_sprint_triage"
    sprint_id: int
    impact: Literal["none", "backlog", "specification"]
    canonical_payload: JsonObject


CompleteTask.model_rebuild(_types_namespace={"JsonObject": JsonObject})
RecordPostSprintTriage.model_rebuild(_types_namespace={"JsonObject": JsonObject})


__all__ = [
    "CloseSprint",
    "CloseStory",
    "CompleteTask",
    "RecordPostSprintTriage",
    "ReviewSprint",
]
