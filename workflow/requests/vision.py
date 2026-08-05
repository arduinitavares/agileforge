"""Typed commands for the isolated Project Vision lifecycle."""

from typing import ClassVar, Literal

from pydantic import Field

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest


class RecordVisionInterviewTurn(PositionedRequest):
    """Append one host-validated Vision interview result."""

    kind: Literal["record_vision_interview_turn"] = "record_vision_interview_turn"
    node_id: ClassVar[str] = "vision.interview"
    mode: Literal["initial", "revision"]
    user_text: str = Field(min_length=1)
    updated_components: JsonObject
    project_vision_statement: str = Field(min_length=1)
    is_complete: bool
    clarifying_questions: tuple[str, ...]
    attempt_id: int
    attempt_fingerprint: str = Field(min_length=1)


class DecideVisionReview(PositionedRequest):
    """Record one operator decision for the waiting Vision artifact."""

    kind: Literal["decide_vision_review"] = "decide_vision_review"
    node_id: ClassVar[str] = "vision.review"
    vision_artifact_id: int
    vision_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str = Field(min_length=1)


class BeginVisionRevision(PositionedRequest):
    """Open an explicit replacement interview for one accepted Vision."""

    kind: Literal["begin_vision_revision"] = "begin_vision_revision"
    node_id: ClassVar[str] = "vision.revision.start"
    source_vision_artifact_id: int
    source_vision_fingerprint: str = Field(min_length=1)
    reason: str = Field(min_length=1)


__all__ = [
    "BeginVisionRevision",
    "DecideVisionReview",
    "RecordVisionInterviewTurn",
]
