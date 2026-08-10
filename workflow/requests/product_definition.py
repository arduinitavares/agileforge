"""Typed requests for immutable Vision and Backlog workflow facts."""

from typing import ClassVar, Literal

from pydantic import Field

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest, ReviewRationale


class RecordBacklogDraft(PositionedRequest):
    """Record canonical Backlog output for the exact Goal and Authority."""

    kind: Literal["record_backlog_draft"] = "record_backlog_draft"
    node_id: ClassVar[str] = "backlog.generate"
    authority_id: int
    authority_fingerprint: str = Field(min_length=1)
    product_goal_artifact_id: int
    product_goal_fingerprint: str = Field(min_length=1)
    canonical_content: JsonObject
    content_fingerprint: str = Field(min_length=1)
    supersedes_backlog_artifact_id: int | None = None


class DecideBacklog(PositionedRequest):
    """Append a decision bound to one exact immutable Backlog artifact."""

    kind: Literal["decide_backlog"] = "decide_backlog"
    node_id: ClassVar[str] = "backlog.review"
    backlog_artifact_id: int
    artifact_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: ReviewRationale


__all__ = [
    "DecideBacklog",
    "RecordBacklogDraft",
]
