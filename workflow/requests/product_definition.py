"""Typed requests for immutable Vision and Backlog workflow facts."""

from __future__ import annotations

from typing import ClassVar, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest

_JSON_OBJECT = TypeAdapter(JsonObject)


class RecordVisionDraft(PositionedRequest):
    """Record canonical Vision output for the exact accepted authority."""

    kind: Literal["record_vision_draft"] = "record_vision_draft"
    node_id: ClassVar[str] = "vision.generate"
    authority_id: int
    authority_fingerprint: str = Field(min_length=1)
    canonical_content: JsonObject
    content_fingerprint: str = Field(min_length=1)
    supersedes_vision_artifact_id: int | None = None
    user_text: str = Field(default="Legacy Vision generation.", min_length=1)


class DecideVision(PositionedRequest):
    """Append a decision bound to one exact immutable Vision artifact."""

    kind: Literal["decide_vision"] = "decide_vision"
    node_id: ClassVar[str] = "vision.review"
    vision_artifact_id: int
    artifact_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str = Field(min_length=1)


class RecordBacklogDraft(PositionedRequest):
    """Record canonical Backlog output for the exact accepted authority."""

    kind: Literal["record_backlog_draft"] = "record_backlog_draft"
    node_id: ClassVar[str] = "backlog.generate"
    authority_id: int
    authority_fingerprint: str = Field(min_length=1)
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
    rationale: str = Field(min_length=1)


class ReconcileBacklog(PositionedRequest):
    """Acknowledge exact stale artifacts under replacement authority."""

    kind: Literal["reconcile_backlog"] = "reconcile_backlog"
    node_id: ClassVar[str] = "backlog.reconcile"
    replacement_authority_id: int
    replacement_authority_fingerprint: str = Field(min_length=1)
    affected_artifact_ids: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_affected_artifact_ids(self) -> Self:
        """Require one canonical exact set for stable reconciliation guards."""
        if self.affected_artifact_ids != tuple(sorted(set(self.affected_artifact_ids))):
            message = "affected_artifact_ids must be sorted and unique."
            raise ValueError(message)
        return self


__all__ = [
    "DecideBacklog",
    "DecideVision",
    "ReconcileBacklog",
    "RecordBacklogDraft",
    "RecordVisionDraft",
]
