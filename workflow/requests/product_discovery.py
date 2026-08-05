"""Typed discovery and specification lifecycle transition requests."""

from typing import ClassVar, Literal

from pydantic import Field

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest


class RecordDiscoveryArtifact(PositionedRequest):
    """Persist canonical discovery output for the active Product Goal."""

    kind: Literal["record_discovery_artifact"] = "record_discovery_artifact"
    node_id: ClassVar[str] = "discovery.record"
    canonical_content: JsonObject
    content_ref: str | None = None


class RecordSpecificationCandidate(PositionedRequest):
    """Persist a canonical specification candidate for current discovery."""

    kind: Literal["record_specification_candidate"] = "record_specification_candidate"
    node_id: ClassVar[str] = "specification.record"
    canonical_content: JsonObject
    content_ref: str | None = None
    supersedes_specification_candidate_id: int | None = None


class DecideSpecification(PositionedRequest):
    """Record one exact specification candidate review decision."""

    kind: Literal["decide_specification"] = "decide_specification"
    node_id: ClassVar[str] = "specification.review"
    specification_candidate_id: int
    specification_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str = ""
